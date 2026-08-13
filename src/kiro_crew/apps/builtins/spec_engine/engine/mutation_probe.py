"""The executed mutation probe: proof a behaviour's tests CAN fail.

Reading a test to decide whether it is adequate is not evidence. A test that
cannot fail is green whether the behaviour is right or wrong, and the only way to
know it fails when the behaviour is wrong is to make the behaviour wrong and watch
the test go red. This module does exactly that and nothing else: it neuters one
mechanism in the engine tree, runs the *named* set of checks that CLAIM to cover
it, and requires at least one of them to fail. A mechanism whose covering checks
stay green under mutation is not covered — that is a gate failure named here, not
a comment a reader may discount.

The probe is necessary, not sufficient. It proves a test *can* fail; it cannot
show the test asked the right question, nor see an equivalent second path that
bypasses the one under test. A verdict rests on this probe *and* a reader with the
whole tree in view — see :mod:`.review_criteria` for the questions the reader owns.

Five hazards this probe is built to refuse rather than paper over:

* **A mutation that does not land proves nothing.** A pattern that matches no text
  produces a clean run that looks like weak coverage but measures nothing. The
  runner verifies the edit actually changed the file before it runs a single
  check, and treats "text not found" as an error, never as a pass.
* **An ambiguous pattern is a defect.** If the text to mutate occurs more than
  once, a single replace neuters an arbitrary one. The runner refuses rather than
  guessing which.
* **Restore by reverting the edit, never ``git checkout``.** ``git checkout``
  discards every co-located uncommitted change and restores nothing on an
  untracked file. The mutation is undone the same way it was applied — the inverse
  edit — and the file is asserted byte-identical to its pre-mutation content. The
  restore runs in a ``finally`` so an interrupted probe cannot leave a neutered
  mechanism behind across a turn boundary.
* **A mutation caught by the wrong check is a false pass.** The result names which
  covering artefact failed, so a mutation the *claiming* check missed and an
  unrelated check happened to catch is distinguishable from one the claiming check
  caught.
* **Only the covering checks run, never the whole suite.** A second prober's
  mutation in the same tree would make a suite-wide result evidence about neither
  mechanism, so the probe runs only the argvs the caller declared cover the
  behaviour, serialized against the tree it mutates.
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Iterator

from kiro_crew import platform_compat

from .delivery.stages import CommandOutcome, CommandRunner, run_argv

logger = logging.getLogger(__name__)

#: Per-check wall-clock ceiling for a probe run. A covering check is a test or a
#: repo-wide guard; either finishes in the time a delivery stage command gets.
DEFAULT_PROBE_TIMEOUT_S = 600

#: Name of the per-tree lock file. It lives beside the tree it guards because the
#: serialization is per tree: one prober per tree, or a tree per prober.
LOCK_FILENAME = ".spec_engine_mutation_probe.lock"


@dataclass(frozen=True)
class CoveringCheck:
    """One named artefact that claims to cover a behaviour.

    ``name`` is the artefact the gate records as the catcher — a test node id, or
    a repo-wide static guard such as a linter rule. ``argv`` is exactly what runs
    to exercise it, through the same command runner a delivery stage uses, so the
    probe has no second way to spawn a process.
    """

    name: str
    argv: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("a covering check needs a name to be recorded as the catcher")
        if not self.argv:
            raise ValueError(f"covering check {self.name!r} has no command to run")


@dataclass(frozen=True)
class Mutation:
    """One neutering edit and the checks that claim to catch it.

    ``original`` is the exact text to replace and ``replacement`` neuters the
    mechanism. ``covering`` is the named set of checks that CLAIM to cover the
    behaviour; the probe runs precisely these, never a wider suite.
    """

    behaviour: str
    path: Path
    original: str
    replacement: str
    covering: tuple[CoveringCheck, ...]

    def __post_init__(self) -> None:
        if not self.behaviour.strip():
            raise ValueError("a mutation must name the behaviour it neuters")
        if not self.original:
            raise ValueError("a mutation with empty original text lands nothing")
        if self.original == self.replacement:
            raise ValueError("a mutation whose replacement equals its original neuters nothing")
        if not self.covering:
            # A behaviour with no covering artefact cannot be probed. Refusing here
            # is the difference between "not covered" and "not checked": the gate
            # must not read an unprobed behaviour as passing.
            raise ValueError(
                f"behaviour {self.behaviour!r} declares no covering check to run under mutation"
            )


class ProbeOutcome(str, Enum):
    """What running one mutation established."""

    #: The mutation landed and at least one covering check failed. The behaviour
    #: is covered. This is the only outcome the gate reads as passing.
    CAUGHT = "caught"
    #: The mutation landed and every covering check still passed. The behaviour is
    #: NOT covered — a gate failure, not a comment.
    SURVIVED = "survived"
    #: The probe could not gather evidence: the text was absent or ambiguous, the
    #: edit did not change the file, a covering check could not be started, or the
    #: file could not be restored. Never read as passing — an inconclusive probe
    #: proves nothing, and the two false "no failures" results this project drew
    #: from a pattern that matched nothing came from treating this as a pass.
    ERROR = "error"


@dataclass(frozen=True)
class ProbeResult:
    """What one mutation established, and which artefact caught it."""

    mutation: Mutation
    outcome: ProbeOutcome
    #: Names of the covering checks that failed under the mutation, in run order.
    caught_by: tuple[str, ...] = ()
    #: Names of the covering checks actually run, in declared order.
    ran: tuple[str, ...] = ()
    #: Cause for an :attr:`ProbeOutcome.ERROR`.
    reason: str = ""

    @property
    def caught(self) -> bool:
        """Whether a covering check failed under the mutation."""
        return self.outcome is ProbeOutcome.CAUGHT

    def gate_failure_reason(self) -> str | None:
        """The gate failure this result carries, or ``None`` when it passes.

        Returning a concrete reason for anything but :attr:`ProbeOutcome.CAUGHT`
        is what makes a still-green suite a FAILURE rather than a warning: a caller
        cannot read approval past a truthy reason, and a survived mutation names
        the behaviour that is not covered rather than logging a note beside a pass.
        """
        if self.outcome is ProbeOutcome.CAUGHT:
            return None
        if self.outcome is ProbeOutcome.SURVIVED:
            return (
                f"the checks covering {self.mutation.behaviour!r} still passed when the "
                "mechanism was neutered: the behaviour is not actually covered"
            )
        return f"the mutation probe for {self.mutation.behaviour!r} proved nothing: {self.reason}"

    def to_json_object(self) -> dict[str, object]:
        return {
            "behaviour": self.mutation.behaviour,
            "outcome": self.outcome.value,
            "caught_by": list(self.caught_by),
            "ran": list(self.ran),
            "reason": self.reason,
        }


class _CheckStatus(str, Enum):
    """How one covering check ended under the mutation."""

    PASSED = "passed"
    FAILED = "failed"
    UNRUNNABLE = "unrunnable"


def _classify(produced: CommandOutcome) -> _CheckStatus:
    """Read a covering check's run as pass, fail, or could-not-run.

    A check that could not be *started* is inconclusive, not a catch: a probe that
    counted "pytest is missing" as the behaviour being covered would report the
    opposite of the truth, so that case is kept distinct from a real red check.
    """
    if produced.start_error:
        return _CheckStatus.UNRUNNABLE
    if produced.timed_out:
        return _CheckStatus.FAILED
    if produced.exit_code is None or produced.exit_code != 0:
        return _CheckStatus.FAILED
    return _CheckStatus.PASSED


@contextmanager
def _tree_lock(tree_root: Path) -> Iterator[None]:
    """Serialize probers against *tree_root* for the duration of the block.

    A second prober mutating the same tree at the same time would make any run
    evidence about neither mutation, so the whole mutate-run-restore is held under
    one exclusive lock keyed to the tree. The lock file lives beside the tree so
    two processes agree on the same lock; it is left in place because unlinking it
    would open the very race it exists to close.
    """
    lock_path = tree_root / LOCK_FILENAME
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        with platform_compat.file_lock(fd, exclusive=True):
            yield
    finally:
        os.close(fd)


def _resolve_target(mutation: Mutation, tree_root: Path) -> Path | None:
    """The file to mutate, or ``None`` when it escapes *tree_root*.

    The probe neuters a mechanism *in the engine tree*; a path that resolves
    outside the tree is refused rather than mutated, so a probe cannot reach a
    file it was never scoped to touch.
    """
    root = tree_root.resolve()
    target = mutation.path if mutation.path.is_absolute() else tree_root / mutation.path
    resolved = target.resolve()
    if resolved != root and root not in resolved.parents:
        return None
    return resolved


def run_probe(
    mutation: Mutation,
    *,
    tree_root: Path,
    runner: CommandRunner = run_argv,
    timeout_s: int = DEFAULT_PROBE_TIMEOUT_S,
) -> ProbeResult:
    """Neuter *mutation*'s mechanism, run its covering checks, and restore.

    Applies exactly one edit, verifies it landed, runs each declared covering
    check through *runner* in *tree_root*, then reverts the edit and asserts the
    file is byte-identical to its pre-mutation content. Returns a result saying
    whether the mutation was caught and which artefact caught it. Never runs a
    check the caller did not declare, and never runs the whole suite.
    """
    target = _resolve_target(mutation, tree_root)
    if target is None:
        return ProbeResult(
            mutation=mutation,
            outcome=ProbeOutcome.ERROR,
            reason=f"the file to mutate is not inside the tree {str(tree_root)!r}",
        )
    try:
        original_bytes = target.read_bytes()
    except OSError as exc:
        return ProbeResult(
            mutation=mutation, outcome=ProbeOutcome.ERROR, reason=f"cannot read {target}: {exc}"
        )
    try:
        text = original_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        return ProbeResult(
            mutation=mutation, outcome=ProbeOutcome.ERROR, reason=f"{target} is not utf-8: {exc}"
        )

    # Screen the pattern before touching the file. A pattern that matches nothing
    # is an error, never a pass (a clean run then measures nothing); a pattern that
    # matches twice is ambiguous, and a single replace would neuter an arbitrary
    # occurrence. Either way, refuse before mutating.
    occurrences = text.count(mutation.original)
    if occurrences == 0:
        return ProbeResult(
            mutation=mutation,
            outcome=ProbeOutcome.ERROR,
            reason=f"the text to mutate was not found in {target}",
        )
    if occurrences > 1:
        return ProbeResult(
            mutation=mutation,
            outcome=ProbeOutcome.ERROR,
            reason=(
                f"the text to mutate occurs {occurrences} times in {target}; "
                "refusing to guess which to neuter"
            ),
        )
    # The inverse edit must be unambiguous too, so that restoring reverts precisely
    # the one occurrence introduced. If the replacement text already appears, the
    # revert could not tell the introduced copy from a pre-existing one.
    if mutation.replacement and mutation.replacement in text:
        return ProbeResult(
            mutation=mutation,
            outcome=ProbeOutcome.ERROR,
            reason=(
                f"the replacement text already appears in {target}; "
                "the inverse edit would be ambiguous"
            ),
        )

    mutated = text.replace(mutation.original, mutation.replacement, 1)

    with _tree_lock(tree_root):
        target.write_bytes(mutated.encode("utf-8"))
        try:
            landed = target.read_bytes()
            if landed == original_bytes or mutation.replacement not in landed.decode("utf-8"):
                # The edit did not change the file. Anything measured now measures
                # nothing, so this is an error rather than a survived mutation.
                return ProbeResult(
                    mutation=mutation,
                    outcome=ProbeOutcome.ERROR,
                    reason=f"the mutation did not change {target}",
                )
            return _run_checks(mutation, tree_root=tree_root, runner=runner, timeout_s=timeout_s)
        finally:
            # Restore no matter how the run ended — a raised check or an
            # interrupted runner must not leave the mechanism neutered. Revert the
            # edit (the inverse replace), then assert byte-identity; the captured
            # bytes are a belt, not the method, and byte-identity is asserted even
            # when the belt is used.
            _restore(target, original_bytes, mutation)


def _run_checks(
    mutation: Mutation,
    *,
    tree_root: Path,
    runner: CommandRunner,
    timeout_s: int,
) -> ProbeResult:
    """Run every declared covering check once and read the result.

    Runs only ``mutation.covering`` — the argvs the caller declared cover the
    behaviour — so a suite-wide result never enters. A failed check names the
    artefact that caught the mutation; if none failed but a check could not run,
    the run is inconclusive rather than a survived mutation.
    """
    ran: list[str] = []
    caught_by: list[str] = []
    unrunnable: list[str] = []
    for check in mutation.covering:
        ran.append(check.name)
        produced = runner(list(check.argv), cwd=tree_root, timeout_s=timeout_s)
        status = _classify(produced)
        if status is _CheckStatus.FAILED:
            caught_by.append(check.name)
        elif status is _CheckStatus.UNRUNNABLE:
            unrunnable.append(check.name)
    if caught_by:
        return ProbeResult(
            mutation=mutation,
            outcome=ProbeOutcome.CAUGHT,
            caught_by=tuple(caught_by),
            ran=tuple(ran),
        )
    if unrunnable:
        return ProbeResult(
            mutation=mutation,
            outcome=ProbeOutcome.ERROR,
            ran=tuple(ran),
            reason=f"covering checks could not be run: {', '.join(unrunnable)}",
        )
    return ProbeResult(
        mutation=mutation,
        outcome=ProbeOutcome.SURVIVED,
        ran=tuple(ran),
    )


def _restore(target: Path, original_bytes: bytes, mutation: Mutation) -> None:
    """Revert the neutering edit and assert the file is byte-identical.

    The inverse edit is the method; ``original_bytes`` is a fallback used only when
    the inverse edit does not reproduce the exact pre-mutation bytes. Either way
    byte-identity is asserted, and a file that cannot be restored raises loudly
    rather than being left neutered across a turn boundary.
    """
    try:
        current = target.read_bytes().decode("utf-8")
        reverted = current.replace(mutation.replacement, mutation.original, 1)
        target.write_bytes(reverted.encode("utf-8"))
    except (OSError, UnicodeDecodeError):
        target.write_bytes(original_bytes)
    if target.read_bytes() != original_bytes:
        # The inverse edit did not reproduce the original. Fall back to the
        # captured bytes (the belt) and assert byte-identity once more; a tree we
        # cannot restore is worse than a failed probe.
        target.write_bytes(original_bytes)
        if target.read_bytes() != original_bytes:
            raise RuntimeError(
                f"could not restore {target} after probing {mutation.behaviour!r}; "
                "the mechanism may be left neutered"
            )
