"""The wave loop: order, in-wave parallelism, persisted status, and the wiring.

Four claims here are about the loop itself:

* leaves run wave by wave, and a wave starts only once the previous one is done;
* within a wave, the configured cap is both a floor and a ceiling on how many run
  at once — the tasks really do overlap, and never more than the cap of them;
* every task status change is persisted as it happens, so an interrupted run
  resumes without paying again for finished work;
* a batch of statuses that settles together is written under **one** held spec
  lock, because the store refuses a conflicting writer rather than queueing, and
  a refused status that is dropped is finished work a resumed run buys twice.

The remaining claims are about wiring, which is a different kind of test. A
library nothing constructs passes every test it has: the workspace broker, the
role resolver and the completion reporter were each written, tested, and then
never built by production code. So the assertions below are made through the
factory a real caller uses, and each one fails if the construction is removed
even though the library it constructs is untouched.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable, Iterable, Sequence

import pytest

from kiro_crew.apps.builtins.spec_engine.engine.audit import AuditLog
from kiro_crew.apps.builtins.spec_engine.engine.autonomy import (
    AUTONOMY_FIELD,
    AutonomyDecision,
    AutonomyLevel,
)
from kiro_crew.apps.builtins.spec_engine.engine.budget import (
    AUDIT_EVENT_COMPLETED,
    Budget,
    BudgetGuard,
    KillSwitch,
    MeteringLedger,
    RecordingNotifier,
    RunAccounting,
    format_credits,
)
from kiro_crew.apps.builtins.spec_engine.engine.config import DASHBOARD_SURFACE, ConfigStore
from kiro_crew.apps.builtins.spec_engine.engine.delivery import (
    ISOLATE_STAGE,
    CommandOutcome,
    DeliveryAuthority,
    RunContext,
    StageOutcome,
    resolve_authority,
)
from kiro_crew.apps.builtins.spec_engine.engine.orchestrator import (
    WAVE_CONCURRENCY_SETTING,
    WORKSPACES_DIRNAME,
    ExecutionOutcome,
    RunCompletion,
    ScheduleProblem,
    TaskResult,
    WaveRunner,
    orchestrator_for,
    read_schedule,
    workspace_root,
)
from kiro_crew.apps.builtins.spec_engine.engine.roles import (
    ROLE_IMPLEMENT,
    Dispatch,
    RolePlan,
    SessionDefault,
)
from kiro_crew.apps.builtins.spec_engine.engine.runs import (
    RunMachine,
    RunState,
    TaskStatus,
    task_statuses,
)
from kiro_crew.apps.builtins.spec_engine.engine.state import (
    DEFAULT_LOCK_TTL_S,
    SpecLock,
    SpecLocked,
    SpecRef,
    StatePersistenceError,
    StateStore,
)

from .conftest import make_spec_dir
from .test_budget_ledger import seed_shard, turn

PROJECT = "acme"
SOURCE = "tracker"
BASE = "main"
RUN = "run-orchestrated"
OTHER_RUN = "run-second"
SESSION = "run-orchestrated-session-0"

#: A model no session default and no other role uses, so an assertion naming it
#: cannot be satisfied by the fallback path it exists to distinguish from.
IMPLEMENT_MODEL = "implement-model"
REVIEW_MODEL = "review-model"
SESSION_MODEL = "session-model"

#: Barrier and gate waits are bounded so a loop that dispatches fewer tasks than
#: the cap fails the test instead of hanging it.
GATE_TIMEOUT_S = 10.0

#: How long the cap observer waits for a task beyond the cap to appear. Only the
#: broken case ends this wait early, so a generous window costs one pause per
#: parametrized case and never a false failure.
EXCEED_WINDOW_S = 0.75


class CountingStore(StateStore):
    """A real state store that records every lock acquisition.

    A counter rather than a mock: the batching claim is about how many times the
    lock is *taken*, and a fake store would answer that question about itself.
    """

    def __init__(self, root: Path) -> None:
        super().__init__(root=root)
        self.acquisitions: list[str] = []

    def acquire_lock(
        self, ref: SpecRef, *, owner: str, ttl_s: float = DEFAULT_LOCK_TTL_S
    ) -> SpecLock:
        lock = super().acquire_lock(ref, owner=owner, ttl_s=ttl_s)
        self.acquisitions.append(owner)
        return lock


class CapGate:
    """Holds every dispatched task live until the test has observed the overlap.

    A plain barrier cannot answer this question. A barrier of *cap* parties
    releases in groups of *cap*, so it serialises the arrivals into exactly the
    shape the cap would have produced anyway and the observed peak comes out at
    the cap whether the loop honoured it or not. Here every task blocks until an
    observer has looked, so the number of live tasks is the loop's answer rather
    than the gate's.
    """

    def __init__(self, cap: int) -> None:
        self.cap = cap
        self.reached = threading.Event()
        self.exceeded = threading.Event()
        self.release = threading.Event()
        self.observed: list[int] = []

    def arrived(self, live: int) -> None:
        if live >= self.cap:
            self.reached.set()
        if live > self.cap:
            self.exceeded.set()

    def hold(self) -> None:
        if not self.release.wait(GATE_TIMEOUT_S):
            raise AssertionError("a dispatched task was never released by the observer")

    def observe(self, worker: "Worker") -> None:
        """Record how many tasks were live once the cap was reached."""
        try:
            if self.reached.wait(GATE_TIMEOUT_S):
                # Bounded and one-directional: it returns at once when more than
                # the cap is live and times out when the cap holds, so the
                # correct case waits and the broken case is caught immediately.
                self.exceeded.wait(EXCEED_WINDOW_S)
            self.observed.append(worker.live)
        finally:
            self.release.set()


class Worker:
    """A task worker that records what it was handed and how much overlapped."""

    def __init__(
        self,
        *,
        barrier: threading.Barrier | None = None,
        cap_gate: CapGate | None = None,
        fail: Iterable[str] = (),
        during: Callable[[str], None] | None = None,
    ) -> None:
        self.dispatched: list[str] = []
        self.routed: dict[str, Dispatch] = {}
        self.contexts: list[RunContext] = []
        self.events: list[tuple[str, str]] = []
        self.peak = 0
        self._live = 0
        self._lock = threading.Lock()
        self._barrier = barrier
        self._cap_gate = cap_gate
        self._fail = set(fail)
        self._during = during

    @property
    def live(self) -> int:
        """Tasks inside the worker right now."""
        with self._lock:
            return self._live

    def __call__(self, *, task: str, dispatch: Dispatch, context: RunContext) -> TaskResult:
        with self._lock:
            self._live += 1
            live = self._live
            self.peak = max(self.peak, self._live)
            self.dispatched.append(task)
            self.routed[task] = dispatch
            self.contexts.append(context)
            self.events.append(("start", task))
        try:
            # Observed on arrival, before any gate: what the loop recorded before
            # it dispatched this task is a fact about the dispatch batch, and
            # reading it after a gate would race the first task to settle.
            if self._during is not None:
                self._during(task)
            if self._cap_gate is not None:
                self._cap_gate.arrived(live)
                self._cap_gate.hold()
            if self._barrier is not None:
                self._barrier.wait()
        finally:
            with self._lock:
                self._live -= 1
                self.events.append(("end", task))
        if task in self._fail:
            return TaskResult(ok=False, reason=f"task {task} did not finish")
        return TaskResult(ok=True)

    def started_before(self, task: str) -> set[str]:
        """Tasks that had already finished when *task* started."""
        index = self.events.index(("start", task))
        return {name for kind, name in self.events[:index] if kind == "end"}


@dataclass(frozen=True)
class Harness:
    """A project, a spec, and the engine state a run of it needs."""

    project: Path
    ref: SpecRef
    config: ConfigStore
    state: CountingStore
    audit: AuditLog
    notifier: RecordingNotifier
    accounting: RunAccounting
    ledger_path: Path
    switch: KillSwitch
    commands: list[tuple[str, ...]] = field(default_factory=list)

    @property
    def machine(self) -> RunMachine:
        return RunMachine(self.state, self.config, project=PROJECT, audit=self.audit)

    def runner(self, argv: Sequence[str], *, cwd: Path, timeout_s: int) -> CommandOutcome:
        """Stand in for the isolate stage's commands, recording the argv."""
        self.commands.append(tuple(argv))
        return CommandOutcome(exit_code=0)

    def start_run(self, run_id: str = RUN) -> str:
        """Create a run and put it in the state tasks are dispatched from."""
        machine = self.machine
        machine.create(self.ref, run_id=run_id)
        machine.transition(self.ref, run_id, RunState.EXECUTING)
        return run_id

    def spend(self, credits: float, *, session: str = SESSION, run_id: str = RUN) -> None:
        """Attribute *credits* of metered consumption to the run."""
        self.accounting.stamp(run_id, session)
        seed_shard(self.ledger_path, date.today(), [turn(session, credits)])

    def detail_statuses(self, run_id: str = RUN) -> dict[str, TaskStatus]:
        record = self.state.get_run(run_id)
        assert record is not None
        return task_statuses(record)


def write_tasks(
    spec_dir: Path,
    waves: Sequence[Sequence[str]],
    *,
    complete: Iterable[str] = (),
    graph: str | None = None,
) -> None:
    """Write a tasks document whose checklist and graph schedule *waves*."""
    finished = set(complete)
    lines = ["# Implementation Plan", "", "## Tasks", ""]
    for number in [task for wave in waves for task in wave]:
        mark = "x" if number in finished else " "
        lines.extend([f"- [{mark}] {number} Task {number}", "    - _Requirements: 1.1_"])
    body = graph
    if body is None:
        body = json.dumps(
            {"waves": [{"id": index, "tasks": list(tasks)} for index, tasks in enumerate(waves)]}
        )
    lines.extend(["", "## Task Dependency Graph", "", "```json", body, "```", ""])
    (spec_dir / "tasks.md").write_text("\n".join(lines), encoding="utf-8")


@pytest.fixture()
def harness(tmp_path: Path) -> Harness:
    project = tmp_path / "project"
    project.mkdir()
    spec_dir = make_spec_dir(project, "example")
    write_tasks(spec_dir, [["1.1", "1.2"], ["2.1"]])
    ledger_path = tmp_path / "usage" / "tokens"
    state = CountingStore(tmp_path / "engine-state")
    config = ConfigStore(tmp_path / "config")
    config.write(
        {
            "cost_profiles": {
                "thrifty": {
                    "roles": {
                        "implement": {"model": IMPLEMENT_MODEL},
                        "review": {"model": REVIEW_MODEL},
                    }
                }
            },
            "projects": {
                PROJECT: {
                    "path": str(project),
                    "base_branch": BASE,
                    "cost_profile": "thrifty",
                    "workflow": {"stages": {ISOLATE_STAGE: [["git", "worktree", "add"]]}},
                }
            },
        },
        surface=DASHBOARD_SURFACE,
    )
    return Harness(
        project=project,
        ref=SpecRef.of(project, "example"),
        config=config,
        state=state,
        audit=AuditLog(tmp_path / "audit"),
        notifier=RecordingNotifier(),
        accounting=RunAccounting(state, ledger=MeteringLedger(ledger_path)),
        ledger_path=ledger_path,
        switch=KillSwitch(tmp_path / "switch-root"),
    )


def authority_for(
    harness: Harness, level: AutonomyLevel = AutonomyLevel.EXECUTION
) -> DeliveryAuthority:
    return resolve_authority(
        harness.config,
        decision=AutonomyDecision(
            level=level,
            source=SOURCE,
            spec_type="feature",
            submitter_class="maintainer",
            declared_at=f"sources.{SOURCE}.{AUTONOMY_FIELD}.maintainer.feature",
        ),
        project=PROJECT,
        base_branch=BASE,
    )


def context_for(harness: Harness, **overrides: str) -> RunContext:
    values: dict[str, str] = {
        "spec_name": "example",
        "spec_type": "feature",
        "workspace_path": str(harness.project),
        "base_branch": BASE,
    }
    values.update(overrides)
    return RunContext(**values)


def runner_for(
    harness: Harness,
    worker: Worker,
    *,
    run_id: str = RUN,
    level: AutonomyLevel = AutonomyLevel.DELIVERY,
    session_default: SessionDefault = SessionDefault(model=SESSION_MODEL),
    headless: bool = False,
) -> WaveRunner:
    """Build the runner the way a real caller does: through the factory."""
    return orchestrator_for(
        harness.ref,
        run_id,
        state=harness.state,
        config=harness.config,
        authority=authority_for(harness, level),
        worker=worker,
        project=PROJECT,
        session_default=session_default,
        audit=harness.audit,
        headless=headless,
        notifier=harness.notifier,
        accounting=harness.accounting,
        kill_switch=harness.switch,
        runner=harness.runner,
    )


def set_cap(harness: Harness, cap: int) -> None:
    harness.config.write(
        {"concurrency": {WAVE_CONCURRENCY_SETTING.split(".", 1)[1]: cap}},
        surface=DASHBOARD_SURFACE,
    )


class TestWavesRunInOrder:
    def test_a_later_waves_task_starts_only_after_every_earlier_one_finished(
        self, harness: Harness
    ) -> None:
        harness.start_run()
        worker = Worker()

        report = runner_for(harness, worker).execute(context_for(harness))

        assert report.outcome is ExecutionOutcome.COMPLETED, report.reason
        assert [wave.wave for wave in report.waves] == [0, 1]
        assert sorted(worker.dispatched) == ["1.1", "1.2", "2.1"]
        # Not "1.1 came before 2.1 in the call list": that would also hold for a
        # loop that started them all at once. The wave-1 task starts only once
        # both wave-0 tasks have *ended*.
        assert worker.started_before("2.1") == {"1.1", "1.2"}

    def test_a_wave_whose_leaves_are_all_finished_is_reported_not_re_run(
        self, harness: Harness
    ) -> None:
        write_tasks(harness.ref.spec_dir, [["1.1"], ["2.1"]], complete=["1.1"])
        harness.start_run()
        worker = Worker()

        report = runner_for(harness, worker).execute(context_for(harness))

        assert worker.dispatched == ["2.1"]
        assert report.waves[0].already_complete == ("1.1",)
        assert report.waves[0].attempts == ()


class TestInWaveParallelism:
    @pytest.mark.parametrize("cap", [1, 2, 3])
    def test_exactly_the_configured_number_of_leaves_are_live_at_once(
        self, harness: Harness, cap: int
    ) -> None:
        """Twice *cap* leaves in one wave, and the live count is read mid-wave.

        Both directions matter and one assertion covers both: the count observed
        while the wave is running is *equal* to the cap, so a serial loop (fewer
        live) and a loop that ignored the cap (more live) each fail. Every
        dispatched task is held until the observer has looked, so the number is
        the loop's answer and not an artefact of how the tasks were paced.
        """
        leaves = [f"1.{index}" for index in range(1, cap * 2 + 1)]
        write_tasks(harness.ref.spec_dir, [leaves])
        set_cap(harness, cap)
        harness.start_run()
        gate = CapGate(cap)
        worker = Worker(cap_gate=gate)
        observer = threading.Thread(target=gate.observe, args=(worker,), daemon=True)
        observer.start()

        report = runner_for(harness, worker).execute(context_for(harness))
        observer.join(GATE_TIMEOUT_S)

        assert report.outcome is ExecutionOutcome.COMPLETED, report.reason
        assert gate.observed == [cap]
        assert worker.peak == cap
        assert sorted(worker.dispatched) == sorted(leaves)


class TestTaskStatusIsPersisted:
    def test_a_task_is_recorded_in_progress_while_it_runs(self, harness: Harness) -> None:
        """Read from inside the worker, which is the only moment it can be seen.

        Asserting on the final statuses would pass for a loop that wrote nothing
        until the wave ended, which is exactly the interruption resume exists for.
        """
        write_tasks(harness.ref.spec_dir, [["1.1"]])
        harness.start_run()
        observed: dict[str, TaskStatus | None] = {}

        def observe(task: str) -> None:
            observed[task] = harness.detail_statuses().get(task)

        worker = Worker(during=observe)
        runner_for(harness, worker).execute(context_for(harness))

        assert observed == {"1.1": TaskStatus.IN_PROGRESS}
        assert harness.detail_statuses() == {"1.1": TaskStatus.COMPLETE}

    def test_a_failed_task_is_recorded_failed_and_the_rest_still_run(
        self, harness: Harness
    ) -> None:
        harness.start_run()
        worker = Worker(fail=["1.1"])

        report = runner_for(harness, worker).execute(context_for(harness))

        assert report.outcome is ExecutionOutcome.FAILED
        assert report.failed_tasks == ("1.1",)
        assert harness.detail_statuses() == {
            "1.1": TaskStatus.FAILED,
            "1.2": TaskStatus.COMPLETE,
            "2.1": TaskStatus.COMPLETE,
        }

    def test_a_worker_that_raises_fails_only_its_own_task(self, harness: Harness) -> None:
        harness.start_run()

        def explode(task: str) -> None:
            if task == "1.2":
                raise RuntimeError("the subagent host went away")

        worker = Worker(during=explode)
        report = runner_for(harness, worker).execute(context_for(harness))

        assert report.failed_tasks == ("1.2",)
        assert harness.detail_statuses()["1.1"] is TaskStatus.COMPLETE
        assert harness.detail_statuses()["2.1"] is TaskStatus.COMPLETE

    def test_a_task_recorded_complete_is_not_dispatched_again(self, harness: Harness) -> None:
        run_id = harness.start_run()
        harness.machine.record_task_status(harness.ref, run_id, "1.1", TaskStatus.COMPLETE)
        worker = Worker()

        report = runner_for(harness, worker).execute(context_for(harness))

        assert "1.1" not in worker.dispatched
        assert report.waves[0].already_complete == ("1.1",)

    def test_a_status_that_cannot_be_persisted_fails_the_execution(self, harness: Harness) -> None:
        """Another writer holding the spec is a refusal, not a silent skip.

        The recorded status is what a resumed run reads, so a batch that could
        not be written must fail the operation rather than leave the loop
        reporting progress nothing recorded.
        """
        harness.start_run()
        harness.state.acquire_lock(harness.ref, owner="someone-else")
        worker = Worker()

        with pytest.raises(SpecLocked):
            runner_for(harness, worker).execute(context_for(harness))

        assert worker.dispatched == []
        assert harness.detail_statuses() == {}


class TestOneLockPerBatch:
    def test_a_batch_of_statuses_costs_one_lock_acquisition(self, harness: Harness) -> None:
        """Three statuses, one acquisition, and all three landed.

        The store refuses a conflicting writer instead of waiting, so an
        acquisition per write is an acquisition per write that can be refused —
        and a refused status that is then dropped makes a resumed run pay again
        for work that finished. The write count is asserted beside the lock count,
        because "few locks" is only correct if every status still landed: passing
        the handle down is what makes the writes work at all, since the lock is
        not re-entrant and a nested acquisition is refused by its own caller.
        """
        harness.start_run()
        runner = runner_for(harness, Worker())
        harness.state.acquisitions.clear()

        runner.record_statuses(
            {
                "1.1": TaskStatus.COMPLETE,
                "1.2": TaskStatus.FAILED,
                "2.1": TaskStatus.IN_PROGRESS,
            }
        )

        assert harness.state.acquisitions == [RUN]
        assert harness.detail_statuses() == {
            "1.1": TaskStatus.COMPLETE,
            "1.2": TaskStatus.FAILED,
            "2.1": TaskStatus.IN_PROGRESS,
        }

    def test_a_waves_leaves_are_marked_in_progress_under_one_acquisition(
        self, harness: Harness
    ) -> None:
        """Read on arrival, where the dispatch batch is observable.

        A wave of three with a cap of three is one dispatch batch, so by the time
        any of its tasks starts all three are recorded and exactly one lock was
        taken to record them. The barrier keeps the tasks alive until all three
        have looked, so the count is not raced by the first task to settle.
        """
        write_tasks(harness.ref.spec_dir, [["1.1", "1.2", "1.3"]])
        set_cap(harness, 3)
        harness.start_run()
        seen: list[tuple[int, dict[str, TaskStatus]]] = []

        def observe(task: str) -> None:
            seen.append((len(harness.state.acquisitions), harness.detail_statuses()))

        worker = Worker(barrier=threading.Barrier(3, timeout=GATE_TIMEOUT_S), during=observe)
        harness.state.acquisitions.clear()
        report = runner_for(harness, worker).execute(context_for(harness))

        assert report.outcome is ExecutionOutcome.COMPLETED, report.reason
        in_progress = {task: TaskStatus.IN_PROGRESS for task in ("1.1", "1.2", "1.3")}
        assert seen == [(1, in_progress)] * 3

    def test_no_spec_lock_is_held_while_a_task_runs(self, harness: Harness) -> None:
        """The lock serialises writers of one spec; a task can take minutes.

        Held across a dispatch, it would block the stall sweep, a cancellation,
        and every other run of the same spec for as long as the model turn takes.
        """
        write_tasks(harness.ref.spec_dir, [["1.1"]])
        harness.start_run()
        acquired: list[bool] = []

        def probe(task: str) -> None:
            try:
                lock = harness.state.acquire_lock(harness.ref, owner="observer")
            except SpecLocked:
                acquired.append(False)
                return
            acquired.append(True)
            harness.state.release_lock(lock)

        report = runner_for(harness, Worker(during=probe)).execute(context_for(harness))

        assert acquired == [True]
        assert report.outcome is ExecutionOutcome.COMPLETED, report.reason


class TestTheWorkspaceBrokerIsWired:
    def test_a_second_run_asking_for_a_held_branch_is_refused_and_dispatches_nothing(
        self, harness: Harness
    ) -> None:
        """The refusal only exists if something constructs the broker.

        A pipeline built without one isolates exactly the same way and never
        refuses, so this is the assertion that fails when the construction is
        removed even though the isolation library is untouched.
        """
        shared = "review/example-17"
        first = runner_for(harness, Worker(), run_id=harness.start_run())
        first_result = first.execute(context_for(harness, branch_name=shared))
        assert first_result.isolation is not None
        assert first_result.isolation.outcome is StageOutcome.PASSED, first_result.reason

        harness.start_run(OTHER_RUN)
        worker = Worker()
        second = runner_for(harness, worker, run_id=OTHER_RUN).execute(
            context_for(harness, branch_name=shared)
        )

        assert second.outcome is ExecutionOutcome.REFUSED
        assert RUN in second.reason and shared in second.reason
        assert worker.dispatched == []
        assert harness.state.list_workspaces(run_id=OTHER_RUN) == []

    def test_the_run_that_claimed_the_tree_holds_it_in_the_ledger(self, harness: Harness) -> None:
        run_id = harness.start_run()

        runner_for(harness, Worker()).execute(context_for(harness))

        held = harness.state.list_workspaces(run_id=run_id)
        assert len(held) == 1
        assert held[0].location.startswith(str(workspace_root(harness.state)))
        assert WORKSPACES_DIRNAME in held[0].location

    def test_a_run_that_may_not_deliver_works_in_the_project_tree(self, harness: Harness) -> None:
        """Isolation is for delivery-authorized runs; nothing is claimed below it.

        Claiming a path for a run that will never push would hold it against
        later runs for a tree that is never created.
        """
        harness.start_run()

        report = runner_for(harness, Worker(), level=AutonomyLevel.AUTHORING).execute(
            context_for(harness)
        )

        assert report.isolation is not None
        assert report.isolation.outcome is StageOutcome.SKIPPED
        assert harness.state.list_workspaces() == []
        assert report.outcome is ExecutionOutcome.COMPLETED, report.reason


class TestTheRoleResolverIsWired:
    def test_every_dispatch_carries_the_implement_roles_agent_model_and_effort(
        self, harness: Harness
    ) -> None:
        """Resolved per dispatch, and not from the session default.

        The host's own role-model map is a closed allowlist that drops a key it
        does not know, so a spec role can only reach a dispatch as a per-call
        value. The assertion names the profile's model rather than merely "not
        empty": an unwired dispatch resolves to the session default, which is
        also not empty.
        """
        harness.start_run()
        worker = Worker()

        runner_for(harness, worker).execute(context_for(harness))

        assert set(worker.routed) == {"1.1", "1.2", "2.1"}
        for task, dispatch in worker.routed.items():
            assert dispatch.role == ROLE_IMPLEMENT, task
            assert dispatch.model == IMPLEMENT_MODEL, task
            assert dispatch.spawn_options() == {"model": IMPLEMENT_MODEL}
            # A subagent inherits the run's assignment rather than resolving one.
            assert dispatch.subagent is True

    def test_the_recorded_attempt_names_the_model_the_task_ran_on(self, harness: Harness) -> None:
        harness.start_run()

        report = runner_for(harness, Worker()).execute(context_for(harness))

        assert {attempt.model for attempt in report.attempts} == {IMPLEMENT_MODEL}
        assert {attempt.role for attempt in report.attempts} == {ROLE_IMPLEMENT}

    def test_an_unassigned_role_falls_back_to_the_session_default_with_a_report(
        self, harness: Harness
    ) -> None:
        """The fallback is the case the report exists for, so it is asserted too.

        A run whose implement role was never assigned runs on whatever the
        session defaults to, and without the report the only evidence is work
        that looks exactly like work on the intended model.
        """
        harness.config.write(
            {"projects": {PROJECT: {"cost_profile": "not-defined"}}},
            surface=DASHBOARD_SURFACE,
        )
        harness.start_run()
        worker = Worker()

        runner = runner_for(harness, worker)
        runner.execute(context_for(harness))

        assert worker.routed["1.1"].model == SESSION_MODEL
        assert any("not-defined" in report for report in runner.role_plan.reports)


class TestTheCompletionReporterIsWired:
    def test_the_run_ends_and_its_total_consumption_is_reported_once(
        self, harness: Harness
    ) -> None:
        """Run completion lives in the orchestrator, so the orchestrator reports it.

        Nothing else calls the reporter: the budget module halts a run but has no
        idea when one finished.
        """
        harness.start_run()
        harness.spend(2.5)

        report = runner_for(harness, Worker()).run(context_for(harness))

        assert report.outcome is ExecutionOutcome.COMPLETED, report.reason
        completion = report.completion
        assert isinstance(completion, RunCompletion)
        assert completion.final_state is RunState.DONE
        assert completion.transitioned is True
        assert completion.report is not None
        assert completion.report.consumed_credits == pytest.approx(2.5)
        # The phrase pins the amount as consumption on a run that ended, which
        # the halt message (a different amount, a different state) does not carry.
        assert any(
            f"ended as done after consuming {format_credits(2.5)} credits" in message
            for message in harness.notifier.messages()
        )
        assert harness.machine.state_of(RUN) is RunState.DONE

    def test_the_consumption_is_recorded_against_the_spec_with_its_cost(
        self, harness: Harness
    ) -> None:
        harness.start_run()
        harness.spend(1.25)

        runner_for(harness, Worker()).run(context_for(harness))

        completed = [
            event
            for event in harness.audit.read(harness.ref)
            if event.event == AUDIT_EVENT_COMPLETED
        ]
        assert len(completed) == 1
        assert completed[0].cost == pytest.approx(1.25)
        assert (completed[0].detail or {})["final_state"] == RunState.DONE.value

    def test_a_run_whose_tasks_failed_ends_failed_and_still_reports_its_cost(
        self, harness: Harness
    ) -> None:
        harness.start_run()
        harness.spend(3.0)

        report = runner_for(harness, Worker(fail=["2.1"])).run(context_for(harness))

        assert report.completion is not None
        assert report.completion.final_state is RunState.FAILED
        assert report.completion.report is not None
        assert report.completion.report.consumed_credits == pytest.approx(3.0)
        assert harness.machine.state_of(RUN) is RunState.FAILED

    def test_a_run_cancelled_under_the_loop_keeps_the_state_it_ended_in(
        self, harness: Harness
    ) -> None:
        """A finished run's history is not rewritten by a late writer.

        Cancellation is terminal, so the completion reports what the run cost and
        names cancelled — it does not move the run to done or failed on the way.
        """
        harness.start_run()
        harness.spend(0.5)
        machine = harness.machine
        runner = runner_for(harness, Worker())
        execution = runner.execute(context_for(harness))
        machine.transition(harness.ref, RUN, RunState.CANCELLED)

        report = runner.finish(execution)

        assert report.completion is not None
        assert report.completion.final_state is RunState.CANCELLED
        assert report.completion.transitioned is False
        assert report.completion.report is not None
        assert report.completion.report.consumed_credits == pytest.approx(0.5)
        assert machine.state_of(RUN) is RunState.CANCELLED

    def test_a_run_that_never_started_is_not_declared_finished(self, harness: Harness) -> None:
        """Queued is not a state a run finishes from, so nothing is reported.

        A run whose leaves were all already checked off has nothing to dispatch,
        but it also never started: moving it to done is not in the lifecycle
        table, and reporting a completion for it would spend the once-per-run
        notice on a run that has not run.
        """
        write_tasks(harness.ref.spec_dir, [["1.1"]], complete=["1.1"])
        harness.machine.create(harness.ref, run_id=RUN)

        report = runner_for(harness, Worker()).run(context_for(harness))

        assert report.outcome is ExecutionOutcome.COMPLETED
        assert report.completion is not None
        assert report.completion.report is None
        assert report.completion.transitioned is False
        assert "cannot move from queued" in report.completion.reason
        assert harness.machine.state_of(RUN) is RunState.QUEUED
        assert harness.notifier.messages() == ()


class TestTheBudgetStopsDispatch:
    def test_dispatched_turns_are_in_flight_while_they_run_and_settled_after(
        self, harness: Harness
    ) -> None:
        """The in-flight count is what "halted, still draining" is read from.

        A ceiling reached with turns outstanding stops new dispatch and lets those
        turns finish, and a surface can only say so if the count reflects what is
        actually running. Counted on the loop's own thread, so two workers never
        increment it at once.
        """
        write_tasks(harness.ref.spec_dir, [["1.1", "1.2"]])
        set_cap(harness, 2)
        harness.start_run()
        gate = CapGate(2)
        worker = Worker(cap_gate=gate)
        runner = runner_for(harness, worker)
        observed: list[int] = []

        def observe() -> None:
            try:
                gate.reached.wait(GATE_TIMEOUT_S)
                observed.append(runner.guard.in_flight)
            finally:
                gate.release.set()

        watcher = threading.Thread(target=observe, daemon=True)
        watcher.start()
        report = runner.execute(context_for(harness))
        watcher.join(GATE_TIMEOUT_S)

        assert report.outcome is ExecutionOutcome.COMPLETED, report.reason
        assert observed == [2]
        assert runner.guard.in_flight == 0

    def test_reaching_the_ceiling_mid_wave_stops_the_leaves_that_had_not_started(
        self, harness: Harness
    ) -> None:
        """The ceiling is consulted before each dispatch, not once per wave.

        The first task's own consumption crosses the ceiling, so the leaves behind
        it must not start — and the one that finished must still be recorded, or a
        resumed run pays for it twice.
        """
        write_tasks(harness.ref.spec_dir, [["1.1", "1.2"], ["2.1"]])
        set_cap(harness, 1)
        harness.start_run()
        harness.accounting.stamp(RUN, SESSION)

        def overspend(task: str) -> None:
            if task == "1.1":
                seed_shard(harness.ledger_path, date.today(), [turn(SESSION, 9.0)])

        worker = Worker(during=overspend)
        report = runner_for(harness, worker).execute(context_for(harness))

        assert report.outcome is ExecutionOutcome.HALTED
        assert worker.dispatched == ["1.1"]
        assert report.waves[0].not_dispatched == ("1.2",)
        assert report.not_reached == (1,)
        assert harness.detail_statuses() == {"1.1": TaskStatus.COMPLETE}
        assert harness.machine.state_of(RUN) is RunState.HALTED_BUDGET

    def test_a_run_parked_for_budget_is_not_reported_as_finished(self, harness: Harness) -> None:
        """A halted run is resumable, so it has not ended.

        Reporting it as finished would spend the once-per-run completion notice
        on a run that is going to keep going, leaving the real completion silent.
        """
        harness.start_run()
        harness.spend(9.0)

        report = runner_for(harness, Worker()).run(context_for(harness))

        assert report.outcome is ExecutionOutcome.HALTED
        assert report.completion is not None
        assert report.completion.final_state is RunState.HALTED_BUDGET
        assert report.completion.report is None
        assert "resumable" in report.completion.reason
        assert not any("ended as" in message for message in harness.notifier.messages())

    def test_the_factory_carries_the_unattended_posture_to_the_guard(
        self, harness: Harness
    ) -> None:
        """The one argument on this path that nothing else checks.

        Every enforcement here is reachable only because the factory passes it
        on, and this is the argument whose omission is invisible: configuration
        cannot express an unbounded ceiling, so the refusal it feeds is
        unreachable through the factory today and becomes reachable the moment a
        real unattended caller exists. Asserting the posture arrived is what
        keeps the passthrough from being decoration.
        """
        harness.start_run()
        runner = runner_for(harness, Worker(), headless=True)

        assert runner.guard.headless is True
        assert runner_for(harness, Worker()).guard.headless is False

    def test_an_unattended_run_with_no_ceiling_dispatches_nothing(self, harness: Harness) -> None:
        """Engine_Floor: a headless run with no finite ceiling never executes.

        The guard is handed in with an unbounded budget, which configuration
        cannot express, and the loop must refuse on the guard's answer rather
        than on a ceiling it re-derives.
        """
        harness.start_run()
        worker = Worker()
        machine = harness.machine
        unbounded = BudgetGuard(
            RUN,
            harness.ref,
            Budget(ceiling_credits=0.0),
            state=harness.state,
            machine=machine,
            accounting=harness.accounting,
            notifier=harness.notifier,
            headless=True,
            kill_switch=harness.switch,
        )
        runner = WaveRunner(
            harness.ref,
            RUN,
            machine=machine,
            config=harness.config,
            plan=RolePlan.for_run(harness.config, project=PROJECT),
            guard=unbounded,
            pipeline=runner_for(harness, worker).pipeline,
            worker=worker,
            project=PROJECT,
            audit=harness.audit,
        )

        report = runner.execute(context_for(harness))

        assert report.outcome is ExecutionOutcome.HALTED
        assert worker.dispatched == []
        assert harness.detail_statuses() == {}

    def test_the_kill_switch_stops_dispatch_before_the_first_task(self, harness: Harness) -> None:
        harness.start_run()
        harness.switch.engage(initiator="operator", reason="operator stopped everything")
        worker = Worker()

        report = runner_for(harness, worker).execute(context_for(harness))

        assert report.outcome is ExecutionOutcome.HALTED
        assert worker.dispatched == []


class TestTheScheduleIsReadNotGuessed:
    @pytest.mark.parametrize(
        ("graph", "problem"),
        [
            ("{not json", ScheduleProblem.MALFORMED_GRAPH),
            (json.dumps({"stages": []}), ScheduleProblem.MALFORMED_GRAPH),
            (json.dumps({"waves": []}), ScheduleProblem.MALFORMED_GRAPH),
            (json.dumps({"waves": [{"id": 0, "tasks": []}]}), ScheduleProblem.MALFORMED_WAVE),
            (json.dumps({"waves": [{"tasks": ["1.1"]}]}), ScheduleProblem.MALFORMED_WAVE),
            (
                json.dumps({"waves": [{"id": 1, "tasks": ["1.1"]}]}),
                ScheduleProblem.NON_SEQUENTIAL_WAVES,
            ),
            (
                json.dumps({"waves": [{"id": 0, "tasks": ["9.9"]}]}),
                ScheduleProblem.UNSCHEDULABLE_TASK,
            ),
            (
                json.dumps({"waves": [{"id": 0, "tasks": ["1.1"]}, {"id": 1, "tasks": ["1.1"]}]}),
                ScheduleProblem.UNSCHEDULABLE_TASK,
            ),
        ],
    )
    def test_an_unusable_graph_refuses_rather_than_scheduling_something_else(
        self, harness: Harness, graph: str, problem: ScheduleProblem
    ) -> None:
        """Order is the product, so a graph with no order yields no dispatch.

        Falling back to document order would look like working: every task runs,
        and the ones whose inputs were never built fail for reasons that read as
        implementation defects.
        """
        write_tasks(harness.ref.spec_dir, [["1.1", "1.2"]], graph=graph)
        harness.start_run()
        worker = Worker()

        schedule = read_schedule(harness.ref.spec_dir)
        report = runner_for(harness, worker).execute(context_for(harness))

        assert schedule.problem is problem, schedule.reason
        assert schedule.usable is False
        assert report.outcome is ExecutionOutcome.REFUSED
        assert report.reason == schedule.reason
        assert worker.dispatched == []

    def test_a_document_with_no_graph_section_schedules_nothing(self, harness: Harness) -> None:
        (harness.ref.spec_dir / "tasks.md").write_text(
            "# Implementation Plan\n\n- [ ] 1.1 Task\n", encoding="utf-8"
        )

        schedule = read_schedule(harness.ref.spec_dir)

        assert schedule.problem is ScheduleProblem.NO_GRAPH
        assert schedule.waves == ()

    def test_a_graph_naming_a_parent_task_is_refused(self, harness: Harness) -> None:
        """A parent groups work; scheduling it would dispatch its subtasks twice."""
        (harness.ref.spec_dir / "tasks.md").write_text(
            "\n".join(
                [
                    "# Implementation Plan",
                    "",
                    "- [ ] 1 Parent",
                    "- [ ] 1.1 Child",
                    "    - _Requirements: 1.1_",
                    "",
                    "## Task Dependency Graph",
                    "",
                    "```json",
                    json.dumps({"waves": [{"id": 0, "tasks": ["1"]}]}),
                    "```",
                    "",
                ]
            ),
            encoding="utf-8",
        )

        schedule = read_schedule(harness.ref.spec_dir)

        assert schedule.problem is ScheduleProblem.UNSCHEDULABLE_TASK
        assert "1" in schedule.reason

    def test_a_usable_graph_yields_the_waves_in_declared_order(self, harness: Harness) -> None:
        write_tasks(harness.ref.spec_dir, [["1.1"], ["1.2", "2.1"]])

        schedule = read_schedule(harness.ref.spec_dir)

        assert schedule.usable is True
        assert [(wave.identifier, wave.tasks) for wave in schedule.waves] == [
            (0, ("1.1",)),
            (1, ("1.2", "2.1")),
        ]
        assert schedule.scheduled_tasks == ("1.1", "1.2", "2.1")


class TestEngineStateStaysOutOfTheSpecTree:
    def test_the_workspace_root_is_under_the_state_root(self, harness: Harness) -> None:
        assert workspace_root(harness.state) == harness.state.root / WORKSPACES_DIRNAME

    def test_a_workspace_root_inside_a_spec_tree_is_refused(self, harness: Harness) -> None:
        with pytest.raises(StatePersistenceError):
            workspace_root(harness.state, harness.ref.spec_dir / "workspaces")

    def test_execution_writes_nothing_into_the_spec_directory(self, harness: Harness) -> None:
        harness.start_run()
        before = _snapshot(harness.ref.spec_dir)

        runner_for(harness, Worker()).run(context_for(harness))

        assert _snapshot(harness.ref.spec_dir) == before


def _snapshot(spec_dir: Path) -> dict[str, str]:
    return {
        str(path.relative_to(spec_dir)): path.read_text(encoding="utf-8")
        for path in sorted(spec_dir.rglob("*"))
        if path.is_file()
    }
