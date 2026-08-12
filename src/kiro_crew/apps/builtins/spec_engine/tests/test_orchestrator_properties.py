"""Property-based tests for the wave loop.

**Wave ordering safety.** Whatever shape the dependency graph has, whatever the
concurrency cap is, and however the individual tasks end, no task is dispatched
before every task in every prior wave has reached a terminal state. Scripted
cases cover the graphs somebody thought to write down; the failure this guards
against is a shape nobody imagined — a wave of one behind a wave of four, a cap
larger than the wave, a failure in the middle — letting a later task start
against work that is still being written. Afterwards that reads as a flaky
implementation rather than as a scheduler that ran things out of order.

The terminal-state half of the property is checked against what was *persisted*
at the moment a task started, not against the loop's own bookkeeping: the
recorded status is what a resumed run reads, so a loop whose memory is ordered
correctly and whose record is not would still make a resume redo finished work.
"""

from __future__ import annotations

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from kiro_crew.apps.builtins.spec_engine.engine.audit import AuditLog
from kiro_crew.apps.builtins.spec_engine.engine.autonomy import AutonomyDecision, AutonomyLevel
from kiro_crew.apps.builtins.spec_engine.engine.budget import MeteringLedger, RunAccounting
from kiro_crew.apps.builtins.spec_engine.engine.config import DASHBOARD_SURFACE, ConfigStore
from kiro_crew.apps.builtins.spec_engine.engine.delivery import RunContext, resolve_authority
from kiro_crew.apps.builtins.spec_engine.engine.orchestrator import (
    ExecutionOutcome,
    TaskResult,
    orchestrator_for,
)
from kiro_crew.apps.builtins.spec_engine.engine.roles import Dispatch
from kiro_crew.apps.builtins.spec_engine.engine.runs import (
    RunMachine,
    RunState,
    TaskStatus,
    task_statuses,
)
from kiro_crew.apps.builtins.spec_engine.engine.state import SpecRef, StateStore

from .conftest import make_spec_dir
from .test_orchestrator_waves import PROJECT, RUN, write_tasks

#: The loop spawns threads and writes to a real store, so keep the count modest.
MAX_EXAMPLES = 30

#: Wave shapes: between two and four waves holding one to three leaves each. Two
#: waves at minimum, because a single wave has no prior wave to order against.
_SHAPES = st.lists(st.integers(min_value=1, max_value=3), min_size=2, max_size=4)
_CAPS = st.integers(min_value=1, max_value=4)
_SEEDS = st.integers(min_value=0, max_value=10_000)

#: Terminal statuses for one leaf. A task that is still in progress is not one.
_TERMINAL = (TaskStatus.COMPLETE, TaskStatus.FAILED)


def _leaves(shape: list[int]) -> list[list[str]]:
    return [[f"{wave + 1}.{index + 1}" for index in range(size)] for wave, size in enumerate(shape)]


class _Recorder:
    """A worker that records, per dispatch, what had been persisted so far."""

    def __init__(self, store: StateStore, failing: frozenset[str]) -> None:
        self.observed: list[tuple[str, dict[str, TaskStatus]]] = []
        self._store = store
        self._failing = failing

    def __call__(self, *, task: str, dispatch: Dispatch, context: RunContext) -> TaskResult:
        record = self._store.get_run(RUN)
        assert record is not None
        self.observed.append((task, task_statuses(record)))
        return TaskResult(ok=task not in self._failing, reason="generated failure")


@settings(max_examples=MAX_EXAMPLES, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(_SHAPES, _CAPS, st.integers(min_value=0, max_value=7), _SEEDS)
def test_no_task_starts_before_every_earlier_wave_reached_a_terminal_state(
    tmp_path_factory, shape: list[int], cap: int, failure_mask: int, seed: int
) -> None:
    root = tmp_path_factory.mktemp("wave-prop")
    waves = _leaves(shape)
    flat = [task for wave in waves for task in wave]
    # A deterministic failure subset drawn from the mask, so a counterexample is
    # reproducible from the reported arguments alone.
    failing = frozenset(task for index, task in enumerate(flat) if failure_mask >> index & 1)

    project = root / f"project-{seed}"
    project.mkdir(parents=True, exist_ok=True)
    spec_dir = make_spec_dir(project, "example")
    write_tasks(spec_dir, waves)
    state = StateStore(root=root / f"state-{seed}")
    config = ConfigStore(root=root / f"config-{seed}")
    config.write(
        {
            "concurrency": {"wave_max_tasks": cap},
            "projects": {PROJECT: {"path": str(project), "base_branch": "main"}},
        },
        surface=DASHBOARD_SURFACE,
    )
    ref = SpecRef.of(project, "example")
    machine = RunMachine(state, config, project=PROJECT)
    machine.create(ref, run_id=RUN)
    machine.transition(ref, RUN, RunState.EXECUTING)

    worker = _Recorder(state, failing)
    report = orchestrator_for(
        ref,
        RUN,
        state=state,
        config=config,
        authority=resolve_authority(
            config,
            decision=AutonomyDecision(
                level=AutonomyLevel.EXECUTION,
                source="tracker",
                spec_type="feature",
                submitter_class="maintainer",
                declared_at="sources.tracker.autonomy.maintainer.feature",
            ),
            project=PROJECT,
            base_branch="main",
        ),
        worker=worker,
        project=PROJECT,
        audit=AuditLog(root / f"audit-{seed}"),
        accounting=RunAccounting(state, ledger=MeteringLedger(root / f"usage-{seed}")),
        machine=machine,
    ).execute(
        RunContext(
            spec_name="example",
            spec_type="feature",
            workspace_path=str(project),
            base_branch="main",
        )
    )

    assert report.outcome in (ExecutionOutcome.COMPLETED, ExecutionOutcome.FAILED), report.reason
    wave_of = {task: index for index, wave in enumerate(waves) for task in wave}
    assert {task for task, _ in worker.observed} == set(flat)
    for task, persisted in worker.observed:
        earlier = [other for other in flat if wave_of[other] < wave_of[task]]
        for other in earlier:
            assert persisted.get(other) in _TERMINAL, (
                f"{task} started while {other} of an earlier wave was " f"{persisted.get(other)}"
            )
        # And nothing from a later wave has started yet, which is the same
        # ordering read from the other end: a later leaf that is already in
        # progress means the loop overlapped two waves.
        later = [other for other in flat if wave_of[other] > wave_of[task]]
        assert not [other for other in later if other in persisted]


@settings(max_examples=MAX_EXAMPLES, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(_SHAPES, _CAPS, _SEEDS)
def test_every_leaf_the_graph_schedules_ends_recorded_terminal(
    tmp_path_factory, shape: list[int], cap: int, seed: int
) -> None:
    """A finished pass leaves no leaf without a recorded outcome.

    The persisted record is what a resumed run reads, so a leaf the loop ran and
    did not record is work the resume pays for a second time.
    """
    root = tmp_path_factory.mktemp("wave-record")
    waves = _leaves(shape)
    flat = [task for wave in waves for task in wave]
    project = root / f"project-{seed}"
    project.mkdir(parents=True, exist_ok=True)
    write_tasks(make_spec_dir(project, "example"), waves)
    state = StateStore(root=root / f"state-{seed}")
    config = ConfigStore(root=root / f"config-{seed}")
    config.write(
        {
            "concurrency": {"wave_max_tasks": cap},
            "projects": {PROJECT: {"path": str(project), "base_branch": "main"}},
        },
        surface=DASHBOARD_SURFACE,
    )
    ref = SpecRef.of(project, "example")
    machine = RunMachine(state, config, project=PROJECT)
    machine.create(ref, run_id=RUN)
    machine.transition(ref, RUN, RunState.EXECUTING)

    worker = _Recorder(state, frozenset())
    orchestrator_for(
        ref,
        RUN,
        state=state,
        config=config,
        authority=resolve_authority(
            config,
            decision=AutonomyDecision(
                level=AutonomyLevel.EXECUTION,
                source="tracker",
                spec_type="feature",
                submitter_class="maintainer",
                declared_at="sources.tracker.autonomy.maintainer.feature",
            ),
            project=PROJECT,
            base_branch="main",
        ),
        worker=worker,
        project=PROJECT,
        accounting=RunAccounting(state, ledger=MeteringLedger(root / f"usage-{seed}")),
        machine=machine,
    ).execute(
        RunContext(
            spec_name="example",
            spec_type="feature",
            workspace_path=str(project),
            base_branch="main",
        )
    )

    record = state.get_run(RUN)
    assert record is not None
    assert set(task_statuses(record)) == set(flat)
    assert all(status is TaskStatus.COMPLETE for status in task_statuses(record).values())
