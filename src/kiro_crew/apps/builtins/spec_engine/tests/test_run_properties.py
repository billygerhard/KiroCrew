"""Property-based tests for the run lifecycle.

**Transition soundness.** Whatever sequence of moves a caller attempts, the run
ends in a state some legal path from the entry state reaches, and every rejected
move leaves the run exactly where it was. Scripted cases cover the paths someone
thought to write down; the failure this guards against is a sequence nobody
imagined leaving a run in a state the table cannot explain — which afterwards
looks like a corrupt row rather than a machine that let it happen.

**Resume never skips or repeats.** Whatever mix of leaf completions a run
persisted, the leaf it resumes at is incomplete, and no completed leaf is ever
offered. Skipping an incomplete leaf reports success for work nobody did;
offering a complete one bills a model turn twice.
"""

from __future__ import annotations

from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from kiro_crew.apps.builtins.spec_engine.engine import runs
from kiro_crew.apps.builtins.spec_engine.engine.config import ConfigStore
from kiro_crew.apps.builtins.spec_engine.engine.runs import (
    IllegalTransition,
    RunMachine,
    RunState,
    TaskStatus,
)
from kiro_crew.apps.builtins.spec_engine.engine.state import SpecRef, StateStore

from .conftest import make_spec_dir

#: Sequences are short but the store is on disk, so keep the count modest.
MAX_EXAMPLES = 60

_STATES = st.sampled_from(list(RunState))
_SEQUENCES = st.lists(_STATES, min_size=1, max_size=8)

#: Leaf numbers of the generated tasks document, in document order.
_LEAVES = ("1", "2", "3", "4")
_COMPLETIONS = st.sets(st.sampled_from(_LEAVES), max_size=len(_LEAVES))
_CHECKED = st.sets(st.sampled_from(_LEAVES), max_size=len(_LEAVES))


def _machine(tmp_path: Path, name: str) -> tuple[RunMachine, SpecRef]:
    project = tmp_path / name
    project.mkdir(parents=True, exist_ok=True)
    make_spec_dir(project, "example")
    store = StateStore(root=tmp_path / f"{name}-state")
    config = ConfigStore(root=tmp_path / f"{name}-config")
    return RunMachine(store, config), SpecRef.of(project, "example")


def _tasks_document(checked: set[str]) -> str:
    lines = ["# Implementation Plan", "", "## Tasks", ""]
    for index, leaf in enumerate(_LEAVES, start=1):
        mark = "x" if leaf in checked else " "
        lines.append(f"- [{mark}] {leaf}. Unit {leaf}")
        lines.append(f"  - _Requirements: 1.{index}_")
    return "\n".join(lines) + "\n"


@settings(max_examples=MAX_EXAMPLES, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(_SEQUENCES, st.integers(min_value=0, max_value=10_000))
def test_a_run_only_ever_holds_a_state_the_table_can_reach(
    tmp_path_factory, sequence: list[RunState], seed: int
) -> None:
    machine, ref = _machine(tmp_path_factory.mktemp("prop"), f"p{seed}")
    machine.create(ref, run_id="run-1")
    for target in sequence:
        before = machine.state_of("run-1")
        try:
            machine.transition(ref, "run-1", target)
        except IllegalTransition:
            # A refusal is inert: the row must be exactly what it was.
            assert machine.state_of("run-1") is before
            continue
        assert machine.state_of("run-1") is target
        assert runs.is_legal(before, target)
    assert machine.state_of("run-1") in runs.reachable_states()


@settings(max_examples=MAX_EXAMPLES, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(_SEQUENCES, st.integers(min_value=0, max_value=10_000))
def test_a_finished_run_is_never_moved_again(
    tmp_path_factory, sequence: list[RunState], seed: int
) -> None:
    machine, ref = _machine(tmp_path_factory.mktemp("prop"), f"f{seed}")
    machine.create(ref, run_id="run-1")
    finished = False
    for target in sequence:
        try:
            machine.transition(ref, "run-1", target)
        except IllegalTransition:
            continue
        assert not finished, "a terminal run accepted a further move"
        finished = machine.state_of("run-1") in runs.TERMINAL_STATES


@settings(max_examples=MAX_EXAMPLES, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(_COMPLETIONS, _CHECKED, st.integers(min_value=0, max_value=10_000))
def test_resume_offers_an_incomplete_leaf_and_never_a_complete_one(
    tmp_path_factory, recorded: set[str], checked: set[str], seed: int
) -> None:
    machine, ref = _machine(tmp_path_factory.mktemp("prop"), f"r{seed}")
    (ref.spec_dir / "tasks.md").write_text(_tasks_document(checked), encoding="utf-8")
    machine.create(ref, run_id="run-1")
    machine.transition(ref, "run-1", RunState.AUTHORING)
    machine.transition(ref, "run-1", RunState.EXECUTING)
    for leaf in sorted(recorded):
        machine.record_task_status(ref, "run-1", leaf, TaskStatus.COMPLETE)

    point = machine.resume_point(ref, "run-1")
    complete = recorded | checked

    assert set(point.completed_tasks) == complete
    if complete == set(_LEAVES):
        assert point.task is None
    else:
        assert point.task is not None
        assert point.task not in complete
        # Document order: everything before the resume point is finished, so no
        # incomplete leaf is skipped past.
        earlier = _LEAVES[: _LEAVES.index(point.task)]
        assert set(earlier) <= complete
