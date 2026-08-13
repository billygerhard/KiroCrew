"""Tests for the executed mutation probe.

The probe is the thing that catches a test which cannot fail, so its own tests
are held to the shape it screens for: none of them assert on a value the failure
path also produces. A survived mutation is asserted as ``SURVIVED`` specifically
(never merely "not caught", which the error path also satisfies); restoration is
asserted on the FILE bytes (never merely that an exception was raised); and "only
the covering checks ran" is asserted on the exact argvs recorded, not on the fact
that the runner was called.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Sequence

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from kiro_crew.apps.builtins.spec_engine.engine.delivery import CommandOutcome
from kiro_crew.apps.builtins.spec_engine.engine.mutation_probe import (
    CoveringCheck,
    Mutation,
    ProbeOutcome,
    run_probe,
)

PASSED = CommandOutcome(exit_code=0)
FAILED = CommandOutcome(exit_code=1)
UNRUNNABLE = CommandOutcome(exit_code=None, start_error="pytest is not installed")


class RecordingRunner:
    """A command runner that records every argv and answers from a table.

    The recording is the point: several tests prove the probe ran ONLY the argvs
    the caller declared cover the behaviour, which is only checkable if every call
    is captured. Unlisted argvs default to a pass, so a test that expects nothing
    to run and asserts an empty record cannot be fooled by a lenient default.
    """

    def __init__(self, responses: dict[tuple[str, ...], CommandOutcome] | None = None) -> None:
        self.responses = responses or {}
        self.calls: list[tuple[str, ...]] = []

    def __call__(
        self, argv: Sequence[str], *, cwd: Path, timeout_s: int
    ) -> CommandOutcome:
        recorded = tuple(argv)
        self.calls.append(recorded)
        return self.responses.get(recorded, PASSED)


class RaisingRunner:
    """A runner that raises, standing in for an interrupted probe."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def __call__(
        self, argv: Sequence[str], *, cwd: Path, timeout_s: int
    ) -> CommandOutcome:
        self.calls.append(tuple(argv))
        raise RuntimeError("the covering check blew up mid-run")


def _mechanism(tree: Path, body: str = "return n % 2 == 0\n") -> Path:
    """Write a tiny mechanism file into *tree* and return its path."""
    path = tree / "mechanism.py"
    path.write_text(f"def is_even(n):\n    {body}", encoding="utf-8")
    return path


# --- the real caller: a genuine mutation, a genuine test, a genuine run -------


def test_real_probe_catches_a_neutered_mechanism(tmp_path: Path) -> None:
    """End-to-end through the real command runner: mutate a real file, run a real
    test, and observe the covering test go red — then the file is byte-identical.

    This is the construction proof: nothing here is stubbed, so a probe that were
    wired to nothing could not pass it.
    """
    mechanism = tmp_path / "widget.py"
    mechanism.write_text("def is_even(n):\n    return n % 2 == 0\n", encoding="utf-8")
    test_file = tmp_path / "test_widget.py"
    test_file.write_text(
        "from widget import is_even\n\n\ndef test_odd_is_not_even():\n    assert not is_even(3)\n",
        encoding="utf-8",
    )
    before = mechanism.read_bytes()

    covering = CoveringCheck(
        name="odd-is-not-even",
        argv=(sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(test_file)),
    )
    mutation = Mutation(
        behaviour="is_even reports parity",
        path=mechanism,
        original="n % 2 == 0",
        replacement="True",
        covering=(covering,),
    )

    result = run_probe(mutation, tree_root=tmp_path, timeout_s=120)

    assert result.outcome is ProbeOutcome.CAUGHT
    assert result.caught_by == ("odd-is-not-even",)
    assert result.gate_failure_reason() is None
    # The mechanism is back exactly as it was, bytes for bytes.
    assert mechanism.read_bytes() == before


# --- the gate semantics: a survived mutation is a named FAILURE ---------------


def test_survived_mutation_is_a_named_gate_failure(tmp_path: Path) -> None:
    mechanism = _mechanism(tmp_path)
    before = mechanism.read_bytes()
    runner = RecordingRunner()  # every check passes
    mutation = Mutation(
        behaviour="parity check",
        path=mechanism,
        original="n % 2 == 0",
        replacement="True",
        covering=(CoveringCheck(name="parity-test", argv=("pytest", "parity")),),
    )

    result = run_probe(mutation, tree_root=tmp_path, runner=runner)

    # SURVIVED specifically, not merely "not caught" — the error path is also not
    # caught, and conflating them would make this test pass on a probe that never
    # ran anything.
    assert result.outcome is ProbeOutcome.SURVIVED
    reason = result.gate_failure_reason()
    assert reason is not None
    assert "parity check" in reason
    assert result.caught_by == ()
    assert mechanism.read_bytes() == before


def test_caught_mutation_names_the_failing_check(tmp_path: Path) -> None:
    mechanism = _mechanism(tmp_path)
    argv = ("pytest", "parity")
    runner = RecordingRunner({argv: FAILED})
    mutation = Mutation(
        behaviour="parity check",
        path=mechanism,
        original="n % 2 == 0",
        replacement="True",
        covering=(CoveringCheck(name="parity-test", argv=argv),),
    )

    result = run_probe(mutation, tree_root=tmp_path, runner=runner)

    assert result.outcome is ProbeOutcome.CAUGHT
    assert result.caught_by == ("parity-test",)
    assert result.gate_failure_reason() is None


def test_a_catch_by_an_unrelated_check_is_distinguished(tmp_path: Path) -> None:
    """The claiming check passes; an unrelated check catches the mutation.

    The result must name only the check that actually failed, so a caller can see
    that the check CLAIMING the behaviour did not catch it — a false pass if the
    two were merged into a single "the suite went red".
    """
    mechanism = _mechanism(tmp_path)
    claiming = CoveringCheck(name="parity-test", argv=("pytest", "parity"))
    unrelated = CoveringCheck(name="import-smoke-guard", argv=("pytest", "smoke"))
    runner = RecordingRunner({unrelated.argv: FAILED})  # claiming still passes
    mutation = Mutation(
        behaviour="parity check",
        path=mechanism,
        original="n % 2 == 0",
        replacement="True",
        covering=(claiming, unrelated),
    )

    result = run_probe(mutation, tree_root=tmp_path, runner=runner)

    assert result.outcome is ProbeOutcome.CAUGHT
    assert result.caught_by == ("import-smoke-guard",)
    assert "parity-test" not in result.caught_by


def test_a_static_guard_is_named_as_the_catcher(tmp_path: Path) -> None:
    """A repo-wide guard, not a unit test, catches the mutation — and is named."""
    mechanism = _mechanism(tmp_path)
    guard = CoveringCheck(name="flake8 repo-wide guard", argv=("flake8", str(mechanism)))
    runner = RecordingRunner({guard.argv: FAILED})
    mutation = Mutation(
        behaviour="parity check",
        path=mechanism,
        original="n % 2 == 0",
        replacement="True",
        covering=(guard,),
    )

    result = run_probe(mutation, tree_root=tmp_path, runner=runner)

    assert result.outcome is ProbeOutcome.CAUGHT
    assert result.caught_by == ("flake8 repo-wide guard",)


# --- hazard A: a mutation that does not land proves nothing -------------------


def test_absent_pattern_is_an_error_and_runs_no_checks(tmp_path: Path) -> None:
    """The single most important property: a pattern that matches nothing is an
    error, never a clean pass, and nothing is run on the strength of it."""
    mechanism = _mechanism(tmp_path)
    before = mechanism.read_bytes()
    runner = RecordingRunner()
    mutation = Mutation(
        behaviour="parity check",
        path=mechanism,
        original="this text is nowhere in the file",
        replacement="neutered",
        covering=(CoveringCheck(name="parity-test", argv=("pytest", "parity")),),
    )

    result = run_probe(mutation, tree_root=tmp_path, runner=runner)

    assert result.outcome is ProbeOutcome.ERROR
    assert "not found" in result.reason
    # Nothing ran: a clean run on an unlanded mutation is exactly the false
    # "no failures" this project was burned by twice.
    assert runner.calls == []
    assert mechanism.read_bytes() == before


# --- hazard C: an ambiguous pattern is a defect, not a convenience ------------


def test_ambiguous_pattern_refuses_and_runs_nothing(tmp_path: Path) -> None:
    mechanism = tmp_path / "mechanism.py"
    mechanism.write_text("a = flag\nb = flag\n", encoding="utf-8")
    before = mechanism.read_bytes()
    runner = RecordingRunner()
    mutation = Mutation(
        behaviour="the flag",
        path=mechanism,
        original="flag",
        replacement="True",
        covering=(CoveringCheck(name="flag-test", argv=("pytest", "flag")),),
    )

    result = run_probe(mutation, tree_root=tmp_path, runner=runner)

    assert result.outcome is ProbeOutcome.ERROR
    assert "occurs 2 times" in result.reason
    assert runner.calls == []
    assert mechanism.read_bytes() == before


def test_replacement_already_present_refuses(tmp_path: Path) -> None:
    """The inverse edit must be unambiguous, so a replacement already in the file
    is refused rather than making the restore guess which copy it introduced."""
    mechanism = tmp_path / "mechanism.py"
    mechanism.write_text("value = original\nother = True\n", encoding="utf-8")
    before = mechanism.read_bytes()
    runner = RecordingRunner()
    mutation = Mutation(
        behaviour="the value",
        path=mechanism,
        original="original",
        replacement="True",  # already present as `other = True`
        covering=(CoveringCheck(name="value-test", argv=("pytest", "value")),),
    )

    result = run_probe(mutation, tree_root=tmp_path, runner=runner)

    assert result.outcome is ProbeOutcome.ERROR
    assert "already appears" in result.reason
    assert runner.calls == []
    assert mechanism.read_bytes() == before


# --- hazard: an inconclusive run is not a survived mutation -------------------


def test_a_check_that_cannot_run_is_an_error_not_a_survival(tmp_path: Path) -> None:
    mechanism = _mechanism(tmp_path)
    argv = ("pytest", "parity")
    runner = RecordingRunner({argv: UNRUNNABLE})
    mutation = Mutation(
        behaviour="parity check",
        path=mechanism,
        original="n % 2 == 0",
        replacement="True",
        covering=(CoveringCheck(name="parity-test", argv=argv),),
    )

    result = run_probe(mutation, tree_root=tmp_path, runner=runner)

    assert result.outcome is ProbeOutcome.ERROR
    assert "could not be run" in result.reason


# --- correctness property (a): restore is byte-identical however the run ends -


def test_restore_survives_a_raising_check(tmp_path: Path) -> None:
    """A check that RAISES must not leave the mechanism neutered.

    The assertion is on the FILE, not on the exception: proving the probe raised
    would be the short-circuit shape the screen warns about. The property is that
    the mechanism is restored regardless.
    """
    mechanism = _mechanism(tmp_path)
    before = mechanism.read_bytes()
    runner = RaisingRunner()
    mutation = Mutation(
        behaviour="parity check",
        path=mechanism,
        original="n % 2 == 0",
        replacement="True",
        covering=(CoveringCheck(name="parity-test", argv=("pytest", "parity")),),
    )

    with pytest.raises(RuntimeError):
        run_probe(mutation, tree_root=tmp_path, runner=runner)

    assert mechanism.read_bytes() == before


# --- correctness property (b): only the covering checks run, never the suite --


def test_only_the_declared_covering_argvs_run(tmp_path: Path) -> None:
    mechanism = _mechanism(tmp_path)
    first = CoveringCheck(name="one", argv=("pytest", "one"))
    second = CoveringCheck(name="two", argv=("pytest", "two"))
    runner = RecordingRunner()
    mutation = Mutation(
        behaviour="parity check",
        path=mechanism,
        original="n % 2 == 0",
        replacement="True",
        covering=(first, second),
    )

    run_probe(mutation, tree_root=tmp_path, runner=runner)

    # Exactly the declared argvs, in order — nothing wider. A whole-suite argv
    # (a pytest invocation with no node id) never appears because the probe only
    # ever iterates the declared covering set.
    assert runner.calls == [first.argv, second.argv]


def test_a_file_outside_the_tree_is_refused(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    tree.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("secret = 1\n", encoding="utf-8")
    before = outside.read_bytes()
    runner = RecordingRunner()
    mutation = Mutation(
        behaviour="something outside",
        path=outside,
        original="secret = 1",
        replacement="secret = 2",
        covering=(CoveringCheck(name="c", argv=("pytest", "c")),),
    )

    result = run_probe(mutation, tree_root=tree, runner=runner)

    assert result.outcome is ProbeOutcome.ERROR
    assert "not inside the tree" in result.reason
    assert runner.calls == []
    assert outside.read_bytes() == before


# --- record shape validation --------------------------------------------------


def test_a_behaviour_with_no_covering_check_is_refused() -> None:
    with pytest.raises(ValueError, match="no covering check"):
        Mutation(
            behaviour="uncovered",
            path=Path("mechanism.py"),
            original="x",
            replacement="y",
            covering=(),
        )


def test_a_noop_mutation_is_refused() -> None:
    with pytest.raises(ValueError, match="neuters nothing"):
        Mutation(
            behaviour="noop",
            path=Path("mechanism.py"),
            original="same",
            replacement="same",
            covering=(CoveringCheck(name="c", argv=("pytest", "c")),),
        )


def test_an_empty_original_is_refused() -> None:
    with pytest.raises(ValueError, match="lands nothing"):
        Mutation(
            behaviour="empty",
            path=Path("mechanism.py"),
            original="",
            replacement="y",
            covering=(CoveringCheck(name="c", argv=("pytest", "c")),),
        )


def test_a_covering_check_needs_a_command() -> None:
    with pytest.raises(ValueError, match="no command to run"):
        CoveringCheck(name="c", argv=())


# --- property-based: byte-identity under every exit path ----------------------

_SAFE = st.text(alphabet=st.characters(blacklist_characters="@", max_codepoint=0x2FFF), max_size=80)


@settings(max_examples=150)
@given(prefix=_SAFE, suffix=_SAFE)
def test_restore_is_byte_identical_for_any_surrounding_content(
    prefix: str, suffix: str, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """For any content, mutating a unique marker and running the probe — whether
    the checks pass or the runner raises — leaves the file byte-identical.

    The marker tokens contain the one character the surrounding text is generated
    to exclude, so the marker occurs exactly once and the replacement is absent:
    the two conditions the probe requires before it will mutate.
    """
    tree = tmp_path_factory.mktemp("probe")
    target = tree / "mechanism.py"
    content = f"{prefix}@@MARK@@{suffix}"
    target.write_bytes(content.encode("utf-8"))
    before = target.read_bytes()
    covering = (CoveringCheck(name="c", argv=("pytest", "c")),)

    passing = Mutation(
        behaviour="marker",
        path=target,
        original="@@MARK@@",
        replacement="@@REPL@@",
        covering=covering,
    )
    result = run_probe(passing, tree_root=tree, runner=RecordingRunner())
    assert result.outcome is ProbeOutcome.SURVIVED
    assert target.read_bytes() == before

    raising = Mutation(
        behaviour="marker",
        path=target,
        original="@@MARK@@",
        replacement="@@REPL@@",
        covering=covering,
    )
    with pytest.raises(RuntimeError):
        run_probe(raising, tree_root=tree, runner=RaisingRunner())
    assert target.read_bytes() == before
