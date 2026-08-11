"""One working tree per run: planning it, claiming it, and refusing a second run.

Two runs in one working tree is not a degraded version of two runs in two trees.
The second run's uncommitted edits are staged and committed by the first, so one
run pushes a change nobody wrote and the other loses work it reported as done —
and both commands exit zero, because from git's point of view nothing went
wrong. That is what makes it worth machinery rather than a note in a document:
the failure is silent, and the artifacts it produces look finished.

So a run's workspace is **planned, then claimed, then created**, in that order.

**Planned.** The destination is derived from the run identifier, so two runs
cannot resolve to one path however they are configured or however close together
they start. That is also why the destination is a run context variable
(``isolated_path``) rather than something a workflow spells out: a literal in
configuration is the same path for every run, which is precisely the shared tree
this module exists to prevent, and the run's own tree (``workspace_path``) is the
tree being isolated *from* — creating a worktree inside it puts a second checkout
under the first one's status output, where the files read as untracked additions
to whatever the parent run is committing.

**Claimed.** The claim is a row in the workspace ledger, written before the
isolate stage spawns anything. A check with no record behind it answers for the
instant it ran; the row is what makes the answer hold while the tree is in use,
survives a Gateway restart, and gives teardown something to find. A claim that
turns out not to be needed — the stage failed, or the workflow has no isolate
commands — is released rather than left holding a path.

**Created** by the workflow's own commands, not by this module. The bundled git
preset fetches the base branch and then cuts the new branch from the *fetched*
ref, in that order: cutting from a local base that a clone left behind starts the
run's work from a commit the base branch moved past hours ago, and every conflict
that follows is attributed to the change rather than to the stale start.

Git enforces the same exclusivity underneath, and deliberately so: it refuses a
worktree at an existing non-empty path and refuses to check one branch out in two
worktrees. The engine's refusal comes first because it can name the run that
holds the tree, which a git error message cannot.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

from ..state import WorkspaceRecord
from .variables import RunContext

logger = logging.getLogger(__name__)

#: Ledger ``kind`` for a materialized working tree. Distinguishes a disposable
#: checkout from a deployment recorded against the same run.
WORKTREE_KIND = "worktree"

#: Run context variable naming the workspace the isolate stage must create.
ISOLATED_PATH_VARIABLE = "isolated_path"

#: Namespace for engine-cut branches, so a project's own branches and a run's
#: branch are never the same name by coincidence.
BRANCH_PREFIX = "spec/"

#: Characters kept from a spec name when deriving a path and ref segment.
#: Deliberately narrow: dots are dropped rather than escaped, which removes
#: ``..`` and a trailing ``.lock`` — both invalid in a git ref — by construction
#: instead of by a check that has to remember them.
_SLUG_ALLOWED = re.compile(r"[^a-z0-9]+")

#: Cap on the derived segment. A spec name is free text and a path has a limit.
MAX_SLUG_CHARS = 40

#: Used when a spec name has no characters that survive slugging.
_SLUG_FALLBACK = "spec"

#: The isolate stage of the bundled git preset. Two commands, and the order is
#: the point: the fetch is what makes ``origin/{base_branch}`` the base branch as
#: it is now rather than as it was when the repository was last pulled.
GIT_ISOLATE_COMMANDS: tuple[tuple[str, ...], ...] = (
    ("git", "fetch", "--prune", "origin", "{base_branch}"),
    (
        "git",
        "worktree",
        "add",
        "{" + ISOLATED_PATH_VARIABLE + "}",
        "-b",
        "{branch_name}",
        "origin/{base_branch}",
    ),
)


class WorkspaceLedger(Protocol):
    """The workspace ledger this module needs. The state store satisfies it."""

    def record_workspace(
        self,
        run_id: str,
        *,
        kind: str,
        location: str | Path,
        address: str | None = ...,
        disposable: bool = ...,
    ) -> WorkspaceRecord: ...

    def list_workspaces(
        self, *, run_id: str | None = ..., include_cleaned: bool = ...
    ) -> list[WorkspaceRecord]: ...

    def mark_workspace_cleaned(self, workspace_id: int) -> bool: ...


@dataclass(frozen=True)
class WorkspacePlan:
    """Where one run's working tree goes, and on which branch."""

    run_id: str
    location: Path
    branch_name: str
    kind: str = WORKTREE_KIND


@dataclass(frozen=True)
class WorkspaceClaim:
    """The outcome of asking for a working tree of one's own.

    A refusal is a value rather than an exception: it is reported as the isolate
    stage refusing, alongside the stages that did run, and the reason names the
    run holding the tree so an operator is not left comparing paths by hand.
    """

    granted: bool
    plan: WorkspacePlan | None = None
    #: Ledger row backing a granted claim, for release and for teardown.
    workspace_id: int = 0
    reason: str = ""

    @property
    def location(self) -> Path | None:
        return self.plan.location if self.plan is not None else None

    @property
    def branch_name(self) -> str:
        return self.plan.branch_name if self.plan is not None else ""


def slugify(name: str) -> str:
    """Reduce *name* to the characters a path segment and a git ref both accept."""
    collapsed = _SLUG_ALLOWED.sub("-", name.strip().lower()).strip("-")
    return collapsed[:MAX_SLUG_CHARS].strip("-") or _SLUG_FALLBACK


def plan_workspace(root: Path, *, run_id: str, context: RunContext) -> WorkspacePlan:
    """Plan the workspace for one run under *root*.

    The run identifier is in both the directory name and the branch, which is
    what makes two plans distinct without consulting anything: uniqueness that
    depends on a lookup is uniqueness that two runs starting at the same moment
    can both pass. The spec name rides along because a human reads these paths.

    A branch the caller already named is kept. A run dispatched from a tracker
    may carry the branch its review artifact expects, and renaming it here would
    push the change somewhere the rest of the workflow is not looking.
    """
    if not run_id.strip():
        raise ValueError("planning a workspace needs a run identifier")
    segment = f"{slugify(context.spec_name)}-{run_id.strip()}"
    return WorkspacePlan(
        run_id=run_id.strip(),
        location=root / segment,
        branch_name=context.branch_name.strip() or f"{BRANCH_PREFIX}{segment}",
    )


def isolated_context(context: RunContext, claim: WorkspaceClaim) -> RunContext:
    """Return *context* carrying the claimed workspace path and branch.

    The commands still run in the project's own tree — a worktree is added by the
    repository that will hold it — so ``workspace_path`` is untouched and the new
    location arrives as its own variable.
    """
    if claim.plan is None:
        return context
    return replace(
        context,
        isolated_path=str(claim.plan.location),
        branch_name=claim.plan.branch_name,
    )


class WorkspaceBroker:
    """Grants each run exclusive use of one working tree, and records it.

    Exclusivity is enforced against the ledger rather than against the file
    system alone. A path that does not exist yet is not free: a run that claimed
    it a second ago has not created it yet either, and "not there" would hand the
    same path to both.
    """

    def __init__(self, ledger: WorkspaceLedger, *, root: str | Path) -> None:
        self._ledger = ledger
        self._root = Path(root)

    @property
    def root(self) -> Path:
        """Directory the run workspaces are created under."""
        return self._root

    def claim(self, *, run_id: str, context: RunContext) -> WorkspaceClaim:
        """Claim a working tree for *run_id*, or refuse and say what holds it."""
        if not run_id.strip():
            return WorkspaceClaim(
                granted=False,
                reason="a run needs an identifier before it can be given a workspace",
            )
        project_tree = context.workspace_path.strip()
        if not project_tree:
            return WorkspaceClaim(
                granted=False,
                reason="the run names no project tree, so there is no repository to isolate from",
            )
        plan = plan_workspace(self._root, run_id=run_id, context=context)
        refusal = self._conflict(plan, project_tree=Path(project_tree))
        if refusal:
            logger.info("refusing an isolated workspace for run %s: %s", plan.run_id, refusal)
            return WorkspaceClaim(granted=False, plan=plan, reason=refusal)
        # Recorded before the stage runs. The row is the claim: a check that
        # passed and then waited for git to finish is a check two runs pass.
        record = self._ledger.record_workspace(
            plan.run_id,
            kind=plan.kind,
            location=str(plan.location),
            # The ref the tree holds. Teardown removes the checkout and must
            # leave this branch alone, and the exclusivity check above reads it
            # to refuse a second tree on one branch before git has to.
            address=plan.branch_name,
            disposable=True,
        )
        return WorkspaceClaim(granted=True, plan=plan, workspace_id=record.workspace_id)

    def release(self, claim: WorkspaceClaim) -> bool:
        """Drop a claim whose workspace was never created.

        Marking it cleaned is the honest record: nothing was materialized, so
        there is nothing left to remove, and leaving the row active would hold a
        path against every later run for a tree that does not exist.
        """
        if not claim.granted or not claim.workspace_id:
            return False
        return self._ledger.mark_workspace_cleaned(claim.workspace_id)

    def workspace_for(self, run_id: str) -> WorkspaceRecord | None:
        """The active working tree recorded for *run_id*, if it holds one.

        Read from the ledger rather than recomputed, so a run resumed in a later
        process finds the tree it was actually given instead of the one today's
        configuration would have planned.
        """
        for record in self._ledger.list_workspaces(run_id=run_id):
            if record.kind == WORKTREE_KIND:
                return record
        return None

    # --- exclusivity -------------------------------------------------------

    def _conflict(self, plan: WorkspacePlan, *, project_tree: Path) -> str:
        """Why *plan* cannot be granted, or an empty string when it can."""
        location = _resolved(plan.location)
        project = _resolved(project_tree)
        if _shares_tree(location, project):
            return (
                "an isolated workspace must not overlap the project's own working tree "
                f"({project}), where a second checkout reads as untracked files in the first"
            )
        if location.exists() and not _is_empty_dir(location):
            return (
                f"{location} already holds files, so a workspace there would not be the run's own"
            )
        return self._ledger_conflict(plan, location=location)

    def _ledger_conflict(self, plan: WorkspacePlan, *, location: Path) -> str:
        for record in self._ledger.list_workspaces():
            if record.run_id == plan.run_id:
                continue
            if _shares_tree(_resolved(Path(record.location)), location):
                return (
                    f"run {record.run_id} is already working in {record.location}, "
                    "and no two active runs share a working tree"
                )
            if record.address and record.address == plan.branch_name:
                return (
                    f"run {record.run_id} already holds branch {plan.branch_name} in "
                    f"{record.location}; one branch cannot be checked out in two working trees"
                )
        return ""


def _shares_tree(left: Path, right: Path) -> bool:
    """Whether two locations are one working tree, including one inside the other."""
    return left == right or left.is_relative_to(right) or right.is_relative_to(left)


def _resolved(path: Path) -> Path:
    """Absolute form of *path*, whether or not it exists yet."""
    return path.expanduser().resolve()


def _is_empty_dir(path: Path) -> bool:
    """Whether *path* is a directory holding nothing.

    An empty directory is not a conflict: git creates a worktree in one, and a
    run whose isolate stage failed after the directory appeared should be able to
    try again rather than be refused by its own leftovers.
    """
    if not path.is_dir():
        return False
    return not any(path.iterdir())


def git_isolate_commands() -> list[list[str]]:
    """The git preset's isolate commands, shaped as configuration supplies them."""
    return [list(command) for command in GIT_ISOLATE_COMMANDS]
