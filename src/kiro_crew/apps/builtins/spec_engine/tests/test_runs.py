"""Run lifecycle behaviour: the transition table, phase timeouts, and resume.

Three classes carry the invariants the rest of the engine leans on.
``TestIllegalTransitions`` pins that a move outside the table is refused and
leaves the row untouched — a state applied and then reported illegal cannot be
undone, because nothing recorded what the run had been. ``TestPhaseTimeouts``
pins that an overrunning phase becomes stalled and notifies, which is the only
thing that distinguishes a run that died mid-phase from one still working.
``TestResume`` pins the two granularities: authoring resumes at its outstanding
document gate, execution at the next incomplete leaf and never at a completed
one.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from kiro_crew.apps.builtins.spec_engine.engine import phases, runs
from kiro_crew.apps.builtins.spec_engine.engine.audit import AuditLog
from kiro_crew.apps.builtins.spec_engine.engine.config import DASHBOARD_SURFACE, ConfigStore
from kiro_crew.apps.builtins.spec_engine.engine.runs import (
    ACTIVE_PHASES,
    INITIAL_STATE,
    PARKED_STATES,
    TERMINAL_STATES,
    TRANSITIONS,
    IllegalTransition,
    ResumeGranularity,
    RunError,
    RunMachine,
    RunState,
    StallNotice,
    TaskStatus,
    UnknownRun,
)
from kiro_crew.apps.builtins.spec_engine.engine.state import SpecRef, StateStore

from .conftest import NATIVE_SPEC_FILES, spec_dir_snapshot

#: Documents in a spec with two leaf tasks, the first already checked off.
TASKS_DOC = """# Implementation Plan

## Tasks

- [x] 1. First unit
  - _Requirements: 1.1_
- [ ] 2. Second unit
  - _Requirements: 1.2_
"""


class FakeClock:
    """A clock the test advances, so a timeout fires without a sleep."""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now = self._now + timedelta(seconds=seconds)


class RecordingNotifier:
    """Collects notices instead of reaching a channel."""

    def __init__(self, *, fail: bool = False) -> None:
        self.notices: list[StallNotice] = []
        self._fail = fail

    def __call__(self, notice: StallNotice) -> None:
        self.notices.append(notice)
        if self._fail:
            raise RuntimeError("channel unavailable")


@pytest.fixture()
def config(tmp_path: Path) -> ConfigStore:
    return ConfigStore(root=tmp_path / "config")


@pytest.fixture()
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture()
def notifier() -> RecordingNotifier:
    return RecordingNotifier()


@pytest.fixture()
def audit(state_dir: Path) -> AuditLog:
    return AuditLog(root=state_dir)


@pytest.fixture()
def machine(
    store: StateStore,
    config: ConfigStore,
    clock: FakeClock,
    notifier: RecordingNotifier,
    audit: AuditLog,
) -> RunMachine:
    return RunMachine(store, config, audit=audit, notifier=notifier, clock=clock)


def timeout_of(config: ConfigStore, state: RunState) -> int:
    key = runs.timeout_setting(state)
    assert key is not None
    return int(config.effective(key).value)


def drive(machine: RunMachine, ref: SpecRef, run_id: str, *states: RunState) -> None:
    """Walk a run through *states*, which must be a legal path."""
    for state in states:
        machine.transition(ref, run_id, state)


class TestTransitionTable:
    def test_every_state_appears_as_a_key(self) -> None:
        assert set(TRANSITIONS) == set(RunState)

    def test_every_state_is_reachable_from_the_entry_state(self) -> None:
        # A stranded state is a case surfaces render and operators are warned
        # about that a run can never actually be in.
        assert runs.reachable_states() == frozenset(RunState)

    def test_a_terminal_state_has_no_way_out(self) -> None:
        for state in TERMINAL_STATES:
            assert runs.allowed_transitions(state) == frozenset()

    def test_every_non_terminal_state_can_move_somewhere(self) -> None:
        for state in RunState:
            if state in TERMINAL_STATES:
                continue
            assert runs.allowed_transitions(state), state

    def test_no_state_transitions_to_itself(self) -> None:
        # A no-op is not a transition; recording one would put an unexplained
        # entry in the audit log and reset the phase clock.
        for state, targets in TRANSITIONS.items():
            assert state not in targets

    def test_cancellation_is_reachable_from_every_unfinished_state(self) -> None:
        for state in RunState:
            if state in TERMINAL_STATES:
                continue
            assert RunState.CANCELLED in runs.allowed_transitions(state), state

    def test_a_parked_run_cannot_complete_without_resuming(self) -> None:
        for state in PARKED_STATES:
            assert RunState.DONE not in runs.allowed_transitions(state)
            assert ACTIVE_PHASES[0] in runs.allowed_transitions(state)

    def test_a_queued_run_is_never_stalled(self) -> None:
        # Waiting behind a concurrency cap is the design working, so queued has
        # no timeout and no path into stalled.
        assert RunState.STALLED not in runs.allowed_transitions(RunState.QUEUED)
        assert runs.timeout_setting(RunState.QUEUED) is None

    def test_every_active_phase_has_a_registered_timeout(self) -> None:
        for phase in ACTIVE_PHASES:
            key = runs.timeout_setting(phase)
            assert key is not None
            assert key in runs.PHASE_TIMEOUT_SETTINGS.values()

    def test_only_active_phases_have_timeouts(self) -> None:
        assert set(runs.PHASE_TIMEOUT_SETTINGS) == set(ACTIVE_PHASES)


class TestCreate:
    def test_a_new_run_starts_queued_and_records_when_it_did(
        self, machine: RunMachine, ref: SpecRef, clock: FakeClock
    ) -> None:
        record = machine.create(ref)
        assert record.state == INITIAL_STATE.value
        assert runs.phase_entered_ts(record) == clock().replace(microsecond=0).isoformat()

    def test_a_generated_run_id_is_recognisable(self, machine: RunMachine, ref: SpecRef) -> None:
        record = machine.create(ref)
        assert record.run_id.startswith(runs.RUN_ID_PREFIX)

    def test_an_explicit_run_id_and_item_are_kept(self, machine: RunMachine, ref: SpecRef) -> None:
        record = machine.create(ref, run_id="run-42", source="tracker", item_id="ITEM-9")
        assert (record.run_id, record.source, record.item_id) == ("run-42", "tracker", "ITEM-9")

    def test_creating_a_run_writes_nothing_into_the_spec_directory(
        self, machine: RunMachine, ref: SpecRef
    ) -> None:
        before = spec_dir_snapshot(ref.spec_dir)
        machine.create(ref)
        assert spec_dir_snapshot(ref.spec_dir) == before
        assert set(before) == set(NATIVE_SPEC_FILES)

    def test_creation_is_audited_with_its_initiator(
        self, machine: RunMachine, ref: SpecRef, audit: AuditLog
    ) -> None:
        machine.create(ref, run_id="run-1", initiator="ada")
        events = [event for event in audit.read(ref) if event.event == runs.RUN_CREATED_EVENT]
        assert [event.initiator for event in events] == ["ada"]


class TestIllegalTransitions:
    def test_an_illegal_move_raises_and_names_what_was_legal(
        self, machine: RunMachine, ref: SpecRef
    ) -> None:
        machine.create(ref, run_id="run-1")
        with pytest.raises(IllegalTransition) as caught:
            machine.transition(ref, "run-1", RunState.DELIVERING)
        assert caught.value.from_state is RunState.QUEUED
        assert caught.value.to_state is RunState.DELIVERING
        assert RunState.AUTHORING in caught.value.allowed

    def test_a_refused_move_leaves_the_run_in_its_previous_state(
        self, machine: RunMachine, ref: SpecRef
    ) -> None:
        machine.create(ref, run_id="run-1")
        with pytest.raises(IllegalTransition):
            machine.transition(ref, "run-1", RunState.DONE)
        assert machine.state_of("run-1") is RunState.QUEUED

    def test_a_refused_move_is_audited_rather_than_silently_dropped(
        self, machine: RunMachine, ref: SpecRef, audit: AuditLog
    ) -> None:
        machine.create(ref, run_id="run-1")
        with pytest.raises(IllegalTransition):
            machine.transition(ref, "run-1", RunState.DONE, initiator="ada")
        refusals = [
            event for event in audit.read(ref) if event.event == runs.RUN_TRANSITION_REFUSED_EVENT
        ]
        assert len(refusals) == 1
        assert refusals[0].detail == {
            "from": "queued",
            "to": "done",
            "allowed": sorted(state.value for state in runs.allowed_transitions(RunState.QUEUED)),
            "reason": "",
        }

    def test_a_finished_run_refuses_every_further_move(
        self, machine: RunMachine, ref: SpecRef
    ) -> None:
        machine.create(ref, run_id="run-1")
        drive(machine, ref, "run-1", RunState.AUTHORING, RunState.EXECUTING, RunState.DONE)
        for state in RunState:
            with pytest.raises(IllegalTransition):
                machine.transition(ref, "run-1", state)

    def test_a_run_cannot_transition_to_its_own_state(
        self, machine: RunMachine, ref: SpecRef
    ) -> None:
        machine.create(ref, run_id="run-1")
        with pytest.raises(IllegalTransition):
            machine.transition(ref, "run-1", RunState.QUEUED)

    def test_an_unknown_run_is_named(self, machine: RunMachine, ref: SpecRef) -> None:
        with pytest.raises(UnknownRun):
            machine.transition(ref, "run-nope", RunState.AUTHORING)

    def test_a_row_holding_a_foreign_state_is_not_guessed_at(
        self, machine: RunMachine, ref: SpecRef, store: StateStore
    ) -> None:
        machine.create(ref, run_id="run-1")
        store.update_run("run-1", state="mystery")
        with pytest.raises(RunError):
            machine.transition(ref, "run-1", RunState.AUTHORING)


class TestLegalPaths:
    def test_the_ordinary_path_runs_end_to_end(self, machine: RunMachine, ref: SpecRef) -> None:
        machine.create(ref, run_id="run-1")
        drive(
            machine,
            ref,
            "run-1",
            RunState.AUTHORING,
            RunState.AWAITING_REVIEW,
            RunState.EXECUTING,
            RunState.DELIVERING,
            RunState.DONE,
        )
        assert machine.state_of("run-1") is RunState.DONE

    def test_a_changes_required_verdict_returns_to_authoring(
        self, machine: RunMachine, ref: SpecRef
    ) -> None:
        machine.create(ref, run_id="run-1")
        drive(machine, ref, "run-1", RunState.AUTHORING, RunState.AWAITING_REVIEW)
        machine.transition(ref, "run-1", RunState.AUTHORING)
        assert machine.state_of("run-1") is RunState.AUTHORING

    def test_a_failing_verify_stage_returns_delivery_to_execution(
        self, machine: RunMachine, ref: SpecRef
    ) -> None:
        machine.create(ref, run_id="run-1")
        drive(
            machine,
            ref,
            "run-1",
            RunState.AUTHORING,
            RunState.EXECUTING,
            RunState.DELIVERING,
            RunState.EXECUTING,
        )
        assert machine.state_of("run-1") is RunState.EXECUTING

    def test_entering_a_phase_restarts_that_phase_clock(
        self, machine: RunMachine, ref: SpecRef, clock: FakeClock
    ) -> None:
        machine.create(ref, run_id="run-1")
        clock.advance(60)
        machine.transition(ref, "run-1", RunState.AUTHORING)
        assert machine.elapsed_in_phase_s(machine.get("run-1")) == 0.0

    def test_a_transition_records_both_ends_in_the_audit_log(
        self, machine: RunMachine, ref: SpecRef, audit: AuditLog
    ) -> None:
        machine.create(ref, run_id="run-1")
        machine.transition(ref, "run-1", RunState.AUTHORING, initiator="ada", reason="seeded")
        moves = [event for event in audit.read(ref) if event.event == runs.RUN_TRANSITIONED_EVENT]
        assert moves[-1].detail == {"from": "queued", "to": "authoring", "reason": "seeded"}


class TestPhaseTimeouts:
    def test_a_phase_within_its_ceiling_is_left_alone(
        self, machine: RunMachine, ref: SpecRef, config: ConfigStore, clock: FakeClock
    ) -> None:
        machine.create(ref, run_id="run-1")
        machine.transition(ref, "run-1", RunState.AUTHORING)
        clock.advance(timeout_of(config, RunState.AUTHORING) - 1)
        assert machine.sweep_stalled() == ()
        assert machine.state_of("run-1") is RunState.AUTHORING

    def test_a_phase_past_its_ceiling_becomes_stalled_and_notifies(
        self,
        machine: RunMachine,
        ref: SpecRef,
        config: ConfigStore,
        clock: FakeClock,
        notifier: RecordingNotifier,
    ) -> None:
        machine.create(ref, run_id="run-1")
        machine.transition(ref, "run-1", RunState.AUTHORING)
        limit = timeout_of(config, RunState.AUTHORING)
        clock.advance(limit + 5)

        notices = machine.sweep_stalled()

        assert machine.state_of("run-1") is RunState.STALLED
        assert [notice.run_id for notice in notices] == ["run-1"]
        assert notices[0].phase is RunState.AUTHORING
        assert notices[0].timeout_s == limit
        assert notices[0].elapsed_s == pytest.approx(limit + 5)
        assert notices[0].notified is True
        assert [notice.run_id for notice in notifier.notices] == ["run-1"]

    def test_each_phase_is_judged_against_its_own_setting(
        self, machine: RunMachine, ref: SpecRef, config: ConfigStore, clock: FakeClock
    ) -> None:
        machine.create(ref, run_id="run-1")
        drive(machine, ref, "run-1", RunState.AUTHORING, RunState.EXECUTING)
        authoring_limit = timeout_of(config, RunState.AUTHORING)
        executing_limit = timeout_of(config, RunState.EXECUTING)
        assert executing_limit > authoring_limit
        # Past the authoring ceiling but inside the executing one: the run is
        # executing, so the authoring ceiling has nothing to say about it.
        clock.advance(authoring_limit + 1)
        assert machine.sweep_stalled() == ()
        clock.advance(executing_limit)
        assert [notice.phase for notice in machine.sweep_stalled()] == [RunState.EXECUTING]

    def test_a_configured_ceiling_is_honoured_over_the_bundled_one(
        self, store: StateStore, ref: SpecRef, tmp_path: Path, clock: FakeClock
    ) -> None:
        config = ConfigStore(root=tmp_path / "config")
        config.write(
            {"timeouts": {"authoring_s": 30}},
            surface=DASHBOARD_SURFACE,
        )
        machine = RunMachine(store, config, clock=clock, notifier=RecordingNotifier())
        machine.create(ref, run_id="run-1")
        machine.transition(ref, "run-1", RunState.AUTHORING)
        clock.advance(31)
        assert [notice.timeout_s for notice in machine.sweep_stalled()] == [30]

    def test_a_stall_is_a_notification_and_never_an_archival(
        self, machine: RunMachine, ref: SpecRef, config: ConfigStore, clock: FakeClock
    ) -> None:
        machine.create(ref, run_id="run-1")
        drive(machine, ref, "run-1", RunState.AUTHORING, RunState.AWAITING_REVIEW)
        clock.advance(timeout_of(config, RunState.AWAITING_REVIEW) * 10)
        machine.sweep_stalled()
        record = machine.get("run-1")
        assert record.state == RunState.STALLED.value
        spec = machine._store.get_spec(ref)
        assert spec is not None and spec.archived is False

    def test_an_already_stalled_run_is_not_swept_again(
        self, machine: RunMachine, ref: SpecRef, config: ConfigStore, clock: FakeClock
    ) -> None:
        machine.create(ref, run_id="run-1")
        machine.transition(ref, "run-1", RunState.AUTHORING)
        clock.advance(timeout_of(config, RunState.AUTHORING) + 1)
        assert len(machine.sweep_stalled()) == 1
        clock.advance(timeout_of(config, RunState.AUTHORING) + 1)
        assert machine.sweep_stalled() == ()

    def test_a_failed_notification_leaves_the_run_stalled_and_is_recorded(
        self,
        store: StateStore,
        config: ConfigStore,
        ref: SpecRef,
        clock: FakeClock,
        audit: AuditLog,
    ) -> None:
        machine = RunMachine(
            store, config, audit=audit, notifier=RecordingNotifier(fail=True), clock=clock
        )
        machine.create(ref, run_id="run-1")
        machine.transition(ref, "run-1", RunState.AUTHORING)
        clock.advance(timeout_of(config, RunState.AUTHORING) + 1)

        notices = machine.sweep_stalled()

        # State is primary: the messaging host being down must not make a dead
        # run look healthy.
        assert machine.state_of("run-1") is RunState.STALLED
        assert notices[0].notified is False
        assert "channel unavailable" in notices[0].error
        failures = [
            event for event in audit.read(ref) if event.event == runs.RUN_NOTIFY_FAILED_EVENT
        ]
        assert len(failures) == 1

    def test_the_notice_names_the_configured_channel(
        self, store: StateStore, ref: SpecRef, tmp_path: Path, clock: FakeClock
    ) -> None:
        config = ConfigStore(root=tmp_path / "config")
        config.write({"notify": {"channel": "slack"}}, surface=DASHBOARD_SURFACE)
        machine = RunMachine(store, config, clock=clock, notifier=RecordingNotifier())
        machine.create(ref, run_id="run-1")
        machine.transition(ref, "run-1", RunState.AUTHORING)
        clock.advance(timeout_of(config, RunState.AUTHORING) + 1)
        assert [notice.channel for notice in machine.sweep_stalled()] == ["slack"]

    def test_a_spec_held_by_another_writer_is_skipped_not_waited_for(
        self, machine: RunMachine, ref: SpecRef, config: ConfigStore, clock: FakeClock
    ) -> None:
        machine.create(ref, run_id="run-1")
        machine.transition(ref, "run-1", RunState.AUTHORING)
        clock.advance(timeout_of(config, RunState.AUTHORING) + 1)
        with machine._store.lock(ref, owner="orchestrator"):
            assert machine.sweep_stalled() == ()
            assert machine.state_of("run-1") is RunState.AUTHORING
        # The ceiling is still exceeded once the writer is gone.
        assert len(machine.sweep_stalled()) == 1

    def test_an_archived_spec_is_left_out_of_the_sweep(
        self, machine: RunMachine, ref: SpecRef, config: ConfigStore, clock: FakeClock
    ) -> None:
        machine.create(ref, run_id="run-1")
        machine.transition(ref, "run-1", RunState.AUTHORING)
        machine._store.set_archived(ref, True)
        clock.advance(timeout_of(config, RunState.AUTHORING) + 1)
        assert machine.sweep_stalled() == ()

    def test_a_clock_that_moved_backwards_does_not_stall_a_fresh_phase(
        self, machine: RunMachine, ref: SpecRef, clock: FakeClock
    ) -> None:
        machine.create(ref, run_id="run-1")
        machine.transition(ref, "run-1", RunState.AUTHORING)
        clock.advance(-3600)
        assert machine.elapsed_in_phase_s(machine.get("run-1")) == 0.0
        assert machine.sweep_stalled() == ()

    def test_the_stall_records_the_phase_and_elapsed_time_in_the_audit_log(
        self,
        machine: RunMachine,
        ref: SpecRef,
        config: ConfigStore,
        clock: FakeClock,
        audit: AuditLog,
    ) -> None:
        machine.create(ref, run_id="run-1")
        machine.transition(ref, "run-1", RunState.EXECUTING)
        limit = timeout_of(config, RunState.EXECUTING)
        clock.advance(limit + 10)
        machine.sweep_stalled()
        stalls = [event for event in audit.read(ref) if event.event == runs.RUN_STALLED_EVENT]
        assert len(stalls) == 1
        assert stalls[0].detail is not None
        assert stalls[0].detail["phase"] == "executing"
        assert stalls[0].detail["timeout_s"] == limit
        assert stalls[0].detail["elapsed_s"] == pytest.approx(limit + 10)


class TestTaskProgress:
    def test_a_recorded_status_survives_a_fresh_store(
        self, machine: RunMachine, ref: SpecRef, state_dir: Path, config: ConfigStore
    ) -> None:
        machine.create(ref, run_id="run-1")
        drive(machine, ref, "run-1", RunState.AUTHORING, RunState.EXECUTING)
        machine.record_task_status(ref, "run-1", "1", TaskStatus.COMPLETE)
        reopened = RunMachine(StateStore(root=state_dir), config)
        assert runs.task_statuses(reopened.get("run-1")) == {"1": TaskStatus.COMPLETE}

    def test_recording_one_task_does_not_drop_another(
        self, machine: RunMachine, ref: SpecRef
    ) -> None:
        machine.create(ref, run_id="run-1")
        drive(machine, ref, "run-1", RunState.AUTHORING, RunState.EXECUTING)
        machine.record_task_status(ref, "run-1", "1", TaskStatus.COMPLETE)
        machine.record_task_status(ref, "run-1", "2", TaskStatus.IN_PROGRESS)
        assert runs.task_statuses(machine.get("run-1")) == {
            "1": TaskStatus.COMPLETE,
            "2": TaskStatus.IN_PROGRESS,
        }

    def test_progress_on_a_finished_run_is_refused(self, machine: RunMachine, ref: SpecRef) -> None:
        machine.create(ref, run_id="run-1")
        drive(machine, ref, "run-1", RunState.AUTHORING, RunState.EXECUTING, RunState.DONE)
        with pytest.raises(RunError):
            machine.record_task_status(ref, "run-1", "1", TaskStatus.COMPLETE)

    def test_an_unparseable_stored_status_is_ignored_rather_than_trusted(
        self, machine: RunMachine, ref: SpecRef
    ) -> None:
        machine.create(ref, run_id="run-1")
        machine._store.update_run("run-1", detail={runs.DETAIL_TASKS: {"1": "finished-ish"}})
        assert runs.task_statuses(machine.get("run-1")) == {}


class TestResume:
    def test_execution_resumes_at_the_next_incomplete_leaf(
        self,
        machine: RunMachine,
        ref: SpecRef,
        config: ConfigStore,
        clock: FakeClock,
    ) -> None:
        _write_tasks(ref, TASKS_DOC)
        machine.create(ref, run_id="run-1")
        drive(machine, ref, "run-1", RunState.AUTHORING, RunState.EXECUTING)
        machine.record_task_status(ref, "run-1", "1", TaskStatus.COMPLETE)
        clock.advance(timeout_of(config, RunState.EXECUTING) + 1)
        machine.sweep_stalled()

        point = machine.resume(ref, "run-1")

        assert machine.state_of("run-1") is RunState.EXECUTING
        assert point.granularity is ResumeGranularity.TASK
        assert point.task == "2"
        assert point.completed_tasks == ("1",)

    def test_a_completed_leaf_is_never_offered_again(
        self, machine: RunMachine, ref: SpecRef
    ) -> None:
        _write_tasks(ref, TASKS_DOC)
        machine.create(ref, run_id="run-1")
        drive(machine, ref, "run-1", RunState.AUTHORING, RunState.EXECUTING)
        machine.record_task_status(ref, "run-1", "1", TaskStatus.COMPLETE)
        machine.record_task_status(ref, "run-1", "2", TaskStatus.COMPLETE)
        assert machine.next_incomplete_task(ref, "run-1") is None
        point = machine.resume_point(ref, "run-1")
        assert point.task is None
        assert point.completed_tasks == ("1", "2")
        assert point.reason

    def test_a_leaf_checked_off_in_the_document_counts_as_complete(
        self, machine: RunMachine, ref: SpecRef
    ) -> None:
        # The checkbox is written by the IDE and by people too, so honouring only
        # the engine's own record would re-run work someone already finished.
        _write_tasks(ref, TASKS_DOC)
        machine.create(ref, run_id="run-1")
        drive(machine, ref, "run-1", RunState.AUTHORING, RunState.EXECUTING)
        assert machine.completed_tasks(ref, "run-1") == ("1",)
        assert machine.next_incomplete_task(ref, "run-1") == "2"

    def test_a_failed_leaf_is_incomplete_and_offered_again(
        self, machine: RunMachine, ref: SpecRef
    ) -> None:
        _write_tasks(ref, TASKS_DOC)
        machine.create(ref, run_id="run-1")
        drive(machine, ref, "run-1", RunState.AUTHORING, RunState.EXECUTING)
        machine.record_task_status(ref, "run-1", "2", TaskStatus.FAILED)
        assert machine.next_incomplete_task(ref, "run-1") == "2"

    def test_an_interrupted_run_resumes_without_restarting_the_wave(
        self, machine: RunMachine, ref: SpecRef, state_dir: Path, config: ConfigStore
    ) -> None:
        # The interruption is a process restart: a second machine over the same
        # database is all the resumed run has to work from.
        _write_tasks(ref, TASKS_DOC)
        machine.create(ref, run_id="run-1")
        drive(machine, ref, "run-1", RunState.AUTHORING, RunState.EXECUTING)
        machine.record_task_status(ref, "run-1", "1", TaskStatus.COMPLETE)
        machine.record_task_status(ref, "run-1", "2", TaskStatus.IN_PROGRESS)

        restarted = RunMachine(StateStore(root=state_dir), config)
        point = restarted.resume_point(ref, "run-1")
        assert point.state is RunState.EXECUTING
        assert point.task == "2"
        assert "1" in point.completed_tasks

    def test_authoring_resumes_at_the_outstanding_document_gate(
        self, machine: RunMachine, ref: SpecRef, config: ConfigStore, clock: FakeClock
    ) -> None:
        machine.create(ref, run_id="run-1")
        machine.transition(ref, "run-1", RunState.AUTHORING)
        clock.advance(timeout_of(config, RunState.AUTHORING) + 1)
        machine.sweep_stalled()

        point = machine.resume(ref, "run-1")

        assert machine.state_of("run-1") is RunState.AUTHORING
        assert point.granularity is ResumeGranularity.PHASE
        # Nothing is approved, so the first gate of the plan is still the one
        # being worked on: resume re-enters it rather than the whole run.
        assert point.gate == "requirements"
        assert point.task is None

    def test_authoring_resumes_past_gates_that_are_settled(
        self, machine: RunMachine, ref: SpecRef
    ) -> None:
        machine.create(ref, run_id="run-1")
        machine.transition(ref, "run-1", RunState.AUTHORING)
        _approve(machine, ref, "requirements")

        point = machine.resume_point(ref, "run-1")

        assert point.granularity is ResumeGranularity.PHASE
        assert point.gate == "design"

    def test_a_run_resumes_into_the_state_it_parked_from(
        self, machine: RunMachine, ref: SpecRef
    ) -> None:
        machine.create(ref, run_id="run-1")
        drive(machine, ref, "run-1", RunState.AUTHORING, RunState.EXECUTING)
        machine.transition(ref, "run-1", RunState.HALTED_BUDGET)
        assert runs.parked_from(machine.get("run-1")) is RunState.EXECUTING
        point = machine.resume(ref, "run-1")
        assert point.state is RunState.EXECUTING
        assert machine.state_of("run-1") is RunState.EXECUTING

    def test_leaving_a_park_clears_where_it_would_have_resumed_to(
        self, machine: RunMachine, ref: SpecRef
    ) -> None:
        machine.create(ref, run_id="run-1")
        machine.transition(ref, "run-1", RunState.AUTHORING)
        machine.transition(ref, "run-1", RunState.STALLED)
        machine.resume(ref, "run-1")
        assert runs.parked_from(machine.get("run-1")) is None

    def test_only_a_parked_run_resumes(self, machine: RunMachine, ref: SpecRef) -> None:
        machine.create(ref, run_id="run-1")
        machine.transition(ref, "run-1", RunState.AUTHORING)
        with pytest.raises(RunError):
            machine.resume(ref, "run-1")

    def test_a_finished_run_has_no_resume_point(self, machine: RunMachine, ref: SpecRef) -> None:
        machine.create(ref, run_id="run-1")
        drive(machine, ref, "run-1", RunState.AUTHORING, RunState.DONE)
        with pytest.raises(RunError):
            machine.resume_point(ref, "run-1")

    def test_a_park_with_no_recorded_origin_is_not_guessed_at(
        self, machine: RunMachine, ref: SpecRef
    ) -> None:
        machine.create(ref, run_id="run-1")
        machine.transition(ref, "run-1", RunState.AUTHORING)
        machine.transition(ref, "run-1", RunState.STALLED)
        machine._store.update_run("run-1", detail={runs.DETAIL_PARKED_FROM: ""})
        with pytest.raises(RunError):
            machine.resume_point(ref, "run-1")

    def test_resume_restarts_the_phase_clock_so_the_run_is_not_instantly_stalled(
        self, machine: RunMachine, ref: SpecRef, config: ConfigStore, clock: FakeClock
    ) -> None:
        machine.create(ref, run_id="run-1")
        machine.transition(ref, "run-1", RunState.AUTHORING)
        clock.advance(timeout_of(config, RunState.AUTHORING) + 1)
        machine.sweep_stalled()
        machine.resume(ref, "run-1")
        assert machine.sweep_stalled() == ()
        assert machine.state_of("run-1") is RunState.AUTHORING

    def test_resume_is_audited_with_where_it_picked_up(
        self, machine: RunMachine, ref: SpecRef, audit: AuditLog
    ) -> None:
        _write_tasks(ref, TASKS_DOC)
        machine.create(ref, run_id="run-1")
        drive(machine, ref, "run-1", RunState.AUTHORING, RunState.EXECUTING)
        machine.transition(ref, "run-1", RunState.STALLED)
        machine.resume(ref, "run-1", initiator="ada")
        resumed = [event for event in audit.read(ref) if event.event == runs.RUN_RESUMED_EVENT]
        assert len(resumed) == 1
        assert resumed[0].detail is not None
        assert resumed[0].detail["granularity"] == "task"
        # Task 1 is checked off in the document, so the run picks up at task 2.
        assert resumed[0].detail["target"] == "2"

    def test_a_missing_tasks_document_yields_no_task_to_resume_at(
        self, machine: RunMachine, ref: SpecRef
    ) -> None:
        (ref.spec_dir / "tasks.md").unlink()
        machine.create(ref, run_id="run-1")
        drive(machine, ref, "run-1", RunState.AUTHORING, RunState.EXECUTING)
        point = machine.resume_point(ref, "run-1")
        assert point.granularity is ResumeGranularity.TASK
        assert point.task is None

    def test_resuming_writes_nothing_into_the_spec_directory(
        self, machine: RunMachine, ref: SpecRef
    ) -> None:
        machine.create(ref, run_id="run-1")
        machine.transition(ref, "run-1", RunState.AUTHORING)
        machine.transition(ref, "run-1", RunState.STALLED)
        before = spec_dir_snapshot(ref.spec_dir)
        machine.resume(ref, "run-1")
        assert spec_dir_snapshot(ref.spec_dir) == before


def _write_tasks(ref: SpecRef, text: str) -> None:
    (ref.spec_dir / "tasks.md").write_text(text, encoding="utf-8")


def _approve(machine: RunMachine, ref: SpecRef, gate: str) -> None:
    """Record a live approval for *gate* the way the phase machine would."""
    kind = next(item for item in phases.document_plan("feature") if item.value == gate)
    text = phases.read_document(ref.spec_dir, kind)
    assert text is not None
    machine._store.record_approval(ref, gate=gate, actor="ada", doc_hash=phases.content_hash(text))
