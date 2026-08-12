"""Taking the disposable half of a run's workspace back, and nothing else.

A run leaves two kinds of thing behind, and they have opposite lifetimes. The
**checkout** is scaffolding: a worktree or a copied tree that exists so the run
had somewhere to work, worth nothing once the run is over, and the reason disk
fills up if nobody removes it. The **branch and its commits** are the work
itself, and they are why the run happened at all.

So this module deletes the first and is structurally unable to delete the
second. That is not a policy it checks at the end; it is the whole shape:

* A worktree is removed by ``git worktree remove``, which unlinks the checkout
  and the metadata pointing at it and leaves the ref exactly where it was. No
  code path here spells ``branch -D``, ``push --delete``, ``reset``, ``gc``, or
  ``update-ref``, and the argv is built from a literal tuple, so a removal cannot
  turn into a ref deletion by way of a variable.
* A directory is deleted only when the ledger says the engine created it as a
  disposable copy **and** it sits under the disposable root. A ledger row is a
  location the engine wrote down, not a location it is licensed to erase: a row
  pointing at the project's own tree, or at anywhere else outside the root, is
  kept and reported rather than removed. The one case worth naming is the one
  where a deletion would be catastrophic and a bug that reached it would look
  ordinary from the inside.
* A **deployment** row is not a path at all, so nothing here tries to delete it.
  Somewhere the change was published is dismantled by the workflow's own
  teardown commands, which is why archive runs the stage before it touches the
  ledger, and why a deployment's row is only marked cleaned when those commands
  actually passed. Marking it cleaned on a failed teardown would drop the only
  record of a live environment nobody is going to look for again.

**Removal is keyed to one run.** Every query names a run identifier, so tearing
down one run cannot reach a sibling run's tree. Two runs of one spec are
independent workspaces on independent branches, and the sibling is very often
still working.

**A dirty checkout survives the terminal-state sweep and not the archive.** They
are different statements. A run reaching a terminal state is the engine's own
bookkeeping, and uncommitted edits there are usually the evidence of why a run
failed, so the sweep leaves the tree and says so. Archiving is a person saying
they are finished with the spec, which is the only authority that discards
uncommitted work — and even then only the working copy: the branch and every
commit on it stay.

**Cleanup is idempotent.** ``mark_workspace_cleaned`` is one-way and matches only
an uncleaned row, and a location that is already gone counts as removed. Archive
is reversible and re-archiving is not an error, so teardown has to be safe to run
twice.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol, Sequence

from ..state import WorkspaceRecord
from .isolation import DEPLOYMENT_KIND, DISPOSABLE_KINDS, WORKTREE_KIND
from .stages import CommandOutcome, StageResult, run_argv

logger = logging.getLogger(__name__)

#: The stage whose configured commands remove a run's dedicated deployments.
TEARDOWN_STAGE = "teardown"

#: Audit event carrying one teardown's outcome.
EVENT_TEARDOWN = "delivery.teardown"

#: Wall clock for one removal command. A worktree removal is a directory unlink,
#: so a minute is generous; the ceiling exists because the command is spawned
#: unattended and a hung git leaves the sweep stuck behind it.
REMOVAL_TIMEOUT_S = 60

#: Removing a worktree, as a literal argv. The path is the only variable part and
#: it arrives as one element, so a location carrying spaces or metacharacters is
#: an argument rather than syntax. ``--force`` is appended by the caller when a
#: person has authorized discarding uncommitted work, never by default.
_WORKTREE_REMOVE: tuple[str, ...] = ("git", "worktree", "remove")

#: Refusal reasons, named so a caller and a test agree on the wording.
REASON_ALREADY_GONE = "the location no longer exists, so there was nothing to remove"
REASON_NOT_DISPOSABLE = "the ledger records this as a deployment, which teardown commands remove"
REASON_OUTSIDE_ROOT = (
    "the location is not under the disposable workspace root, so the engine will not delete it"
)
REASON_NO_ROOT = (
    "no disposable workspace root is configured, so the engine will not delete a copied tree"
)
REASON_NO_STAGE = "no teardown command runner is wired, so configured teardown commands did not run"


class Ledger(Protocol):
    """The workspace ledger teardown reads and closes out."""

    def list_workspaces(
        self, *, run_id: str | None = ..., include_cleaned: bool = ...
    ) -> list[WorkspaceRecord]: ...

    def mark_workspace_cleaned(self, workspace_id: int) -> bool: ...


class StageRunner(Protocol):
    """Runs one run's configured teardown stage.

    A callable rather than a pipeline: this module knows *when* teardown commands
    run and what their outcome licenses, and nothing about resolving a project's
    workflow or its variables. The caller that has the run's context supplies
    that, and :class:`~.stages.StageExecutor` satisfies the shape.
    """

    def __call__(self, run_id: str) -> StageResult: ...


class CommandRunner(Protocol):
    """Runs one already-built argv. The seam the tests substitute."""

    def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        timeout_s: int,
    ) -> CommandOutcome: ...


#: Receives one audit-shaped event. The janitor does not own a spec identity, so
#: recording is a callable a driver supplies.
AuditRecorder = Callable[[str, dict[str, Any]], None]


@dataclass(frozen=True)
class WorkspaceCleanup:
    """What became of one ledger row."""

    workspace_id: int
    run_id: str
    kind: str
    location: str
    address: str | None
    removed: bool
    #: Why a row was left alone, or how it was removed. Always populated: a
    #: cleanup nobody can explain afterwards is the one people distrust.
    reason: str = ""

    def to_json_object(self) -> dict[str, Any]:
        return {
            "workspace_id": self.workspace_id,
            "run_id": self.run_id,
            "kind": self.kind,
            "location": self.location,
            "address": self.address,
            "removed": self.removed,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class TeardownReport:
    """One teardown: the stage that ran, and what happened to each ledger row."""

    run_id: str
    cleanups: tuple[WorkspaceCleanup, ...] = ()
    #: The configured teardown stage's result, when a runner was wired.
    stage: StageResult | None = None
    #: Why the stage did not run, when it did not.
    stage_reason: str = ""
    forced: bool = False
    kept_reasons: tuple[str, ...] = field(default_factory=tuple)

    @property
    def removed(self) -> tuple[WorkspaceCleanup, ...]:
        return tuple(cleanup for cleanup in self.cleanups if cleanup.removed)

    @property
    def kept(self) -> tuple[WorkspaceCleanup, ...]:
        return tuple(cleanup for cleanup in self.cleanups if not cleanup.removed)

    @property
    def stage_ok(self) -> bool:
        """Whether the teardown stage left nothing behind it.

        A stage nobody could run is not ``ok``: the deployments it would have
        removed are still there, and reporting that as success is how an
        environment outlives every record of itself.
        """
        return self.stage is not None and self.stage.ok

    @property
    def complete(self) -> bool:
        """Whether every row for this run is closed out."""
        return not self.kept and (self.stage is None or self.stage.ok)

    def to_json_object(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "forced": self.forced,
            "removed": [cleanup.to_json_object() for cleanup in self.removed],
            "kept": [cleanup.to_json_object() for cleanup in self.kept],
            "stage": self.stage.outcome.value if self.stage is not None else None,
            "stage_reason": self.stage_reason,
        }


class WorkspaceJanitor:
    """Removes a run's disposable materializations and closes its ledger rows.

    Constructed with the ledger it reads and the root it is allowed to delete
    under. The root is the licence: without one, a copied tree is reported and
    kept rather than deleted, because a ledger location is a fact the engine
    recorded and not permission to erase whatever it points at.
    """

    def __init__(
        self,
        ledger: Ledger,
        *,
        root: str | Path | None = None,
        runner: CommandRunner | None = None,
        stage: StageRunner | None = None,
        audit: AuditRecorder | None = None,
    ) -> None:
        self._ledger = ledger
        self._root = Path(root) if root is not None else None
        self._runner: CommandRunner = runner if runner is not None else run_argv
        self._stage = stage
        self._audit = audit

    @property
    def root(self) -> Path | None:
        """The only directory tree the janitor will delete a copy from under."""
        return self._root

    # --- entry points ------------------------------------------------------

    def retire_run(self, run_id: str) -> TeardownReport:
        """Remove *run_id*'s disposable materializations at a terminal state.

        Deployments are left alone: a finished run's published change is still
        published, and the workflow's teardown commands remove it at archive.
        Uncommitted edits are left alone too — see the module docstring for why
        the terminal-state sweep is the one caller that does not force.
        """
        return self._teardown(run_id, force=False, run_stage=False)

    def archive_run(self, run_id: str) -> TeardownReport:
        """Run *run_id*'s teardown commands, then clean up its ledger rows.

        Stage first, ledger second. The commands are what remove a dedicated
        deployment, and a deployment row is closed out only when they passed: the
        row is the sole record that the environment exists, so dropping it on a
        failed teardown loses the environment rather than the record of a
        problem.
        """
        return self._teardown(run_id, force=True, run_stage=True)

    def clean_workspace(self, workspace_id: int, *, force: bool = False) -> WorkspaceCleanup | None:
        """Remove one ledger-recorded materialization on request.

        The manual action behind a surface's cleanup button, and the way a tree
        the sweep kept for its uncommitted edits is finally released. Returns
        ``None`` when no active row has that identifier, so a double click is
        answered rather than mistaken for a removal.
        """
        for record in self._ledger.list_workspaces():
            if record.workspace_id == workspace_id:
                cleanup = self._remove(record, force=force)
                self._record(
                    TeardownReport(
                        run_id=record.run_id,
                        cleanups=(cleanup,),
                        forced=force,
                    )
                )
                return cleanup
        return None

    # --- the sweep ---------------------------------------------------------

    def _teardown(self, run_id: str, *, force: bool, run_stage: bool) -> TeardownReport:
        run = run_id.strip()
        if not run:
            raise ValueError("tearing down a workspace needs a run identifier")
        stage: StageResult | None = None
        stage_reason = ""
        if run_stage:
            stage, stage_reason = self._run_stage(run)
        cleanups = [
            self._close(record, force=force, stage=stage)
            # Snapshot the rows before removing any of them: the ledger query is
            # keyed to this run, so a sibling run's tree is not in the list at
            # all, and iterating a live query while closing rows out is how a
            # row gets skipped.
            for record in self._active(run)
        ]
        report = TeardownReport(
            run_id=run,
            cleanups=tuple(cleanups),
            stage=stage,
            stage_reason=stage_reason,
            forced=force,
            kept_reasons=tuple(
                cleanup.reason for cleanup in cleanups if not cleanup.removed and cleanup.reason
            ),
        )
        self._record(report)
        return report

    def _active(self, run_id: str) -> list[WorkspaceRecord]:
        return list(self._ledger.list_workspaces(run_id=run_id))

    def _run_stage(self, run_id: str) -> tuple[StageResult | None, str]:
        if self._stage is None:
            return None, REASON_NO_STAGE
        result = self._stage(run_id)
        if not result.ok:
            logger.info("teardown commands for run %s ended as %s", run_id, result.outcome.value)
        return result, ""

    def _close(
        self,
        record: WorkspaceRecord,
        *,
        force: bool,
        stage: StageResult | None,
    ) -> WorkspaceCleanup:
        """Decide what happens to one row, then do it."""
        if record.kind == DEPLOYMENT_KIND or not record.disposable:
            return self._close_deployment(record, stage=stage)
        return self._remove(record, force=force)

    def _close_deployment(
        self, record: WorkspaceRecord, *, stage: StageResult | None
    ) -> WorkspaceCleanup:
        """Close a deployment row out, but only behind passing teardown commands."""
        if stage is None or not stage.ok:
            reason = (
                REASON_NO_STAGE
                if stage is None
                else f"the teardown stage ended as {stage.outcome.value}, so the deployment stands"
            )
            return self._cleanup(record, removed=False, reason=reason)
        self._ledger.mark_workspace_cleaned(record.workspace_id)
        return self._cleanup(
            record,
            removed=True,
            reason="the configured teardown commands removed this deployment",
        )

    def _remove(self, record: WorkspaceRecord, *, force: bool) -> WorkspaceCleanup:
        """Remove one disposable materialization, or say why it was kept."""
        if record.kind not in DISPOSABLE_KINDS:
            return self._cleanup(record, removed=False, reason=REASON_NOT_DISPOSABLE)
        location = Path(record.location).expanduser()
        if not location.exists():
            # Already gone by hand, or a claim that never materialized. The row
            # is what leaks here, not the disk.
            self._ledger.mark_workspace_cleaned(record.workspace_id)
            return self._cleanup(record, removed=True, reason=REASON_ALREADY_GONE)
        if record.kind == WORKTREE_KIND:
            return self._remove_worktree(record, location, force=force)
        return self._remove_copy(record, location)

    def _remove_worktree(
        self, record: WorkspaceRecord, location: Path, *, force: bool
    ) -> WorkspaceCleanup:
        """Unlink a git worktree, leaving its branch and commits untouched.

        ``git worktree remove`` is the whole mechanism. It removes the checkout
        and the administrative directory that points at it, and it does not touch
        the ref: the branch stays, every commit on it stays reachable, and the
        run's work is retrievable from the repository afterwards. Running it from
        inside the worktree means the repository is found without the ledger
        having to have recorded where the main checkout lives.
        """
        argv = [*_WORKTREE_REMOVE, *(("--force",) if force else ()), str(location)]
        outcome = self._runner(argv, cwd=location, timeout_s=REMOVAL_TIMEOUT_S)
        if outcome.exit_code == 0 and not outcome.timed_out:
            self._ledger.mark_workspace_cleaned(record.workspace_id)
            return self._cleanup(
                record,
                removed=True,
                reason="the worktree was removed; its branch and commits are untouched",
            )
        detail = _first_line(outcome.stderr) or _first_line(outcome.start_error)
        if outcome.timed_out:
            detail = "the removal command exceeded its timeout and was killed"
        return self._cleanup(
            record,
            removed=False,
            reason=(
                "the worktree was kept: "
                + (detail or f"git exited {outcome.exit_code}")
                + ("" if force else " (uncommitted work is discarded only on archive)")
            ),
        )

    def _remove_copy(self, record: WorkspaceRecord, location: Path) -> WorkspaceCleanup:
        """Delete a copied working tree, and only under the disposable root."""
        if self._root is None:
            return self._cleanup(record, removed=False, reason=REASON_NO_ROOT)
        if not _under(location, self._root):
            logger.warning(
                "refusing to delete %s: it is not under the disposable workspace root",
                record.location,
            )
            return self._cleanup(record, removed=False, reason=REASON_OUTSIDE_ROOT)
        try:
            shutil.rmtree(location)
        except OSError as exc:
            return self._cleanup(
                record, removed=False, reason=f"the copied tree could not be deleted: {exc}"
            )
        self._ledger.mark_workspace_cleaned(record.workspace_id)
        return self._cleanup(record, removed=True, reason="the copied working tree was deleted")

    def _cleanup(self, record: WorkspaceRecord, *, removed: bool, reason: str) -> WorkspaceCleanup:
        return WorkspaceCleanup(
            workspace_id=record.workspace_id,
            run_id=record.run_id,
            kind=record.kind,
            location=record.location,
            address=record.address,
            removed=removed,
            reason=reason,
        )

    def _record(self, report: TeardownReport) -> None:
        if self._audit is None:
            return
        self._audit(EVENT_TEARDOWN, report.to_json_object())


def _under(location: Path, root: Path) -> bool:
    """Whether *location* sits inside *root*, and is not *root* itself.

    Resolved on both sides so a symlink pointing out of the root cannot present
    itself as inside it. The root itself is excluded: deleting it would take
    every other run's workspace with it.
    """
    resolved = location.expanduser().resolve()
    base = root.expanduser().resolve()
    return resolved != base and resolved.is_relative_to(base)


def _first_line(text: str) -> str:
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


__all__ = [
    "DEPLOYMENT_KIND",
    "DISPOSABLE_KINDS",
    "EVENT_TEARDOWN",
    "REASON_ALREADY_GONE",
    "REASON_NOT_DISPOSABLE",
    "REASON_NO_ROOT",
    "REASON_NO_STAGE",
    "REASON_OUTSIDE_ROOT",
    "REMOVAL_TIMEOUT_S",
    "TEARDOWN_STAGE",
    "AuditRecorder",
    "CommandRunner",
    "Ledger",
    "StageRunner",
    "TeardownReport",
    "WorkspaceCleanup",
    "WorkspaceJanitor",
]
