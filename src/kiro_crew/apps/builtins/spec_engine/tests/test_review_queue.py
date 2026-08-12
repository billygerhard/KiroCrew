"""The Review_Queue projection and the archival rules.

Two of these classes exist to pin properties that are stated as absences, which
means each of their tests has to fail if the forbidden mechanism is ever added.
``TestNoTimeBasedArchival`` pins that no clock can archive anything: the cause
set is closed and asserted closed, every time-flavoured cause is refused, and a
clock driven a year forward through the sweep archives nothing.
``TestArchivedStaysArchived`` pins that the flag survives every operation the
engine has except an explicit unarchive, and that a refused archival moves it in
neither direction. ``TestReversible`` pins the other half: unarchiving puts a run
back in the queue when it belongs at a human-reserved gate, and does not invent a
place for one that does not.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from kiro_crew.apps.builtins.spec_engine.engine import review_queue
from kiro_crew.apps.builtins.spec_engine.engine.audit import AuditLog
from kiro_crew.apps.builtins.spec_engine.engine.config import ConfigStore
from kiro_crew.apps.builtins.spec_engine.engine.phases import content_hash
from kiro_crew.apps.builtins.spec_engine.engine.review_queue import (
    ArchivalRefused,
    ArchiveCause,
    QueueEntry,
    ReviewQueue,
    WaitingOn,
    resolve_cause,
)
from kiro_crew.apps.builtins.spec_engine.engine.runs import (
    RunMachine,
    RunState,
    StallNotice,
    TaskStatus,
)
from kiro_crew.apps.builtins.spec_engine.engine.state import (
    SpecLocked,
    SpecRef,
    StatePersistenceError,
    StateStore,
)

from .conftest import make_spec_dir

#: A tasks document with one leaf still open, so a resumed execution has a target.
TASKS_DOC = """# Implementation Plan

## Tasks

- [ ] 1. Only unit
  - _Requirements: 1.1_
"""

#: Causes a retention sweep, a cache expiry, or a tidy-up job would plausibly
#: pass. None of them may be accepted, whatever they are spelled.
TIME_FLAVOURED_CAUSES = (
    "expired",
    "expiry",
    "elapsed",
    "stale",
    "retention",
    "timeout",
    "age",
    "old",
    "swept",
)


class FakeClock:
    """A clock the test advances, so elapsed time needs no sleep."""

    def __init__(self) -> None:
        self._now = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now = self._now + timedelta(seconds=seconds)


class SilentNotifier:
    """Swallows stall notices; delivery is not what these tests are about."""

    def __init__(self) -> None:
        self.notices: list[StallNotice] = []

    def __call__(self, notice: StallNotice) -> None:
        self.notices.append(notice)


@pytest.fixture()
def config(tmp_path: Path) -> ConfigStore:
    return ConfigStore(root=tmp_path / "config")


@pytest.fixture()
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture()
def audit(state_dir: Path) -> AuditLog:
    return AuditLog(root=state_dir)


@pytest.fixture()
def machine(
    store: StateStore, config: ConfigStore, clock: FakeClock, audit: AuditLog
) -> RunMachine:
    return RunMachine(store, config, audit=audit, notifier=SilentNotifier(), clock=clock)


@pytest.fixture()
def queue(machine: RunMachine) -> ReviewQueue:
    return ReviewQueue(machine)


def park(
    machine: RunMachine,
    ref: SpecRef,
    run_id: str,
    state: RunState,
    *,
    item_id: str | None = None,
) -> None:
    """Create *run_id* and walk it to *state* by legal moves."""
    machine.create(ref, run_id=run_id, item_id=item_id, source="github")
    for step in _PATHS[state]:
        machine.transition(ref, run_id, step)


#: A legal route to each state, so a test naming a state need not spell the walk.
_PATHS: dict[RunState, tuple[RunState, ...]] = {
    RunState.QUEUED: (),
    RunState.AUTHORING: (RunState.AUTHORING,),
    RunState.AWAITING_REVIEW: (RunState.AUTHORING, RunState.AWAITING_REVIEW),
    RunState.EXECUTING: (RunState.AUTHORING, RunState.EXECUTING),
    RunState.DELIVERING: (RunState.AUTHORING, RunState.EXECUTING, RunState.DELIVERING),
    RunState.DONE: (RunState.AUTHORING, RunState.DONE),
    RunState.FAILED: (RunState.FAILED,),
    RunState.CANCELLED: (RunState.CANCELLED,),
    RunState.STALLED: (RunState.AUTHORING, RunState.STALLED),
    RunState.HALTED_BUDGET: (RunState.HALTED_BUDGET,),
}


class TestQueueProjection:
    def test_only_runs_waiting_on_a_person_are_in_the_queue(
        self, machine: RunMachine, queue: ReviewQueue, ref: SpecRef
    ) -> None:
        for state in RunState:
            park(machine, ref, f"run-{state.value}", state)

        held = set(queue.snapshot().run_ids)

        # Spelled out rather than derived from HUMAN_RESERVED_STATES: an expected
        # set built from the mapping under test moves with it, so widening the
        # mapping to a state the engine drives itself would satisfy the assertion
        # instead of failing it. Every state is parked, so this pins membership in
        # both directions.
        assert held == {"run-awaiting_review", "run-halted_budget", "run-stalled"}

    def test_each_entry_says_what_the_person_has_to_do(
        self, machine: RunMachine, queue: ReviewQueue, ref: SpecRef
    ) -> None:
        for state in RunState:
            park(machine, ref, f"run-{state.value}", state)

        waiting = {entry.run_id: entry.waiting_on for entry in queue.snapshot()}

        assert waiting == {
            "run-awaiting_review": WaitingOn.REVIEW,
            "run-halted_budget": WaitingOn.BUDGET,
            "run-stalled": WaitingOn.STALL,
        }

    def test_the_longest_wait_comes_first(
        self, machine: RunMachine, queue: ReviewQueue, ref: SpecRef, clock: FakeClock
    ) -> None:
        park(machine, ref, "run-old", RunState.AWAITING_REVIEW)
        clock.advance(600)
        park(machine, ref, "run-new", RunState.AWAITING_REVIEW)
        clock.advance(60)

        snapshot = queue.snapshot()

        # Ordering is asserted through the waiting times themselves rather than
        # only through the sequence: a projection that ordered correctly while
        # computing the waits from the wrong timestamp would satisfy the sequence
        # alone.
        assert snapshot.run_ids == ("run-old", "run-new")
        assert [round(entry.waiting_s) for entry in snapshot] == [660, 60]

    def test_the_queue_groups_by_run_state_for_a_rendering_driver(
        self, machine: RunMachine, queue: ReviewQueue, ref: SpecRef
    ) -> None:
        park(machine, ref, "run-a", RunState.AWAITING_REVIEW)
        park(machine, ref, "run-b", RunState.AWAITING_REVIEW)
        park(machine, ref, "run-c", RunState.HALTED_BUDGET)

        grouped = queue.snapshot().grouped()

        assert {state: tuple(e.run_id for e in group) for state, group in grouped.items()} == {
            RunState.AWAITING_REVIEW: ("run-a", "run-b"),
            RunState.HALTED_BUDGET: ("run-c",),
        }
        # An empty group is omitted, not rendered as a permanent empty heading.
        assert RunState.STALLED not in grouped

    def test_a_project_filter_narrows_the_queue_to_that_project(
        self, machine: RunMachine, queue: ReviewQueue, ref: SpecRef, tmp_path: Path
    ) -> None:
        other_project = tmp_path / "other"
        other_project.mkdir()
        make_spec_dir(other_project, "example")
        other = SpecRef.of(other_project, "example")
        park(machine, ref, "run-here", RunState.AWAITING_REVIEW)
        park(machine, other, "run-there", RunState.AWAITING_REVIEW)

        assert queue.snapshot(project=ref.project).run_ids == ("run-here",)
        assert queue.snapshot(project=other.project).run_ids == ("run-there",)
        assert set(queue.snapshot().run_ids) == {"run-here", "run-there"}

    def test_an_entry_names_the_document_gate_the_reviewer_must_look_at(
        self, machine: RunMachine, queue: ReviewQueue, ref: SpecRef
    ) -> None:
        park(machine, ref, "run-1", RunState.AWAITING_REVIEW)

        first = queue.snapshot().entries[0]
        assert first.gate == "requirements"

        # Approving the outstanding gate moves the reviewer's target on, which is
        # what makes this the gate and not a constant.
        machine.store.record_approval(
            ref,
            gate="requirements",
            actor="user:someone",
            doc_hash=_hash_of(ref, "requirements.md"),
        )
        assert queue.snapshot().entries[0].gate == "design"

    def test_the_snapshot_renders_as_json_for_any_driver(
        self, machine: RunMachine, queue: ReviewQueue, ref: SpecRef, clock: FakeClock
    ) -> None:
        park(machine, ref, "run-1", RunState.AWAITING_REVIEW, item_id="42")
        clock.advance(90)

        rendered = json.loads(json.dumps(queue.snapshot().to_json_object()))

        assert rendered["total"] == 1
        entry = rendered["entries"][0]
        assert entry["run_id"] == "run-1"
        assert entry["state"] == "awaiting_review"
        assert entry["waiting_on"] == "review"
        assert entry["item_id"] == "42"
        assert entry["waiting_s"] == pytest.approx(90.0)
        assert list(rendered["grouped"]) == ["awaiting_review"]

    def test_the_queue_reports_whether_it_holds_a_run(
        self, machine: RunMachine, queue: ReviewQueue, ref: SpecRef
    ) -> None:
        park(machine, ref, "run-waiting", RunState.AWAITING_REVIEW)
        park(machine, ref, "run-working", RunState.EXECUTING)

        assert queue.holds("run-waiting") is True
        assert queue.holds("run-working") is False


class TestNoTimeBasedArchival:
    def test_the_only_accepted_causes_are_a_person_and_a_cancelled_item(self) -> None:
        # A closed set asserted closed. Adding a cause — an expiry, a retention
        # window, a tidy-up — fails here, which is the point: the refusal below
        # only holds while nothing has been added for it to resolve to.
        assert set(ArchiveCause) == {ArchiveCause.USER, ArchiveCause.ITEM_CANCELLED}

    @pytest.mark.parametrize("cause", TIME_FLAVOURED_CAUSES)
    def test_a_cause_meaning_time_passed_is_refused_and_recorded(
        self, queue: ReviewQueue, ref: SpecRef, audit: AuditLog, cause: str
    ) -> None:
        with pytest.raises(ArchivalRefused):
            queue.archive(ref, cause=cause)

        assert queue.is_archived(ref) is False
        refusals = [
            event
            for event in audit.read(ref)
            if event.event == review_queue.SPEC_ARCHIVE_REFUSED_EVENT
        ]
        assert len(refusals) == 1

    def test_resolve_cause_accepts_only_the_named_causes(self) -> None:
        assert resolve_cause("user") is ArchiveCause.USER
        assert resolve_cause(ArchiveCause.ITEM_CANCELLED) is ArchiveCause.ITEM_CANCELLED
        with pytest.raises(ArchivalRefused):
            resolve_cause("")

    def test_a_year_on_the_clock_with_sweeps_running_archives_nothing(
        self,
        machine: RunMachine,
        queue: ReviewQueue,
        ref: SpecRef,
        clock: FakeClock,
        tmp_path: Path,
    ) -> None:
        second_project = tmp_path / "second"
        second_project.mkdir()
        make_spec_dir(second_project, "example")
        second = SpecRef.of(second_project, "example")
        park(machine, ref, "run-review", RunState.AWAITING_REVIEW)
        park(machine, ref, "run-budget", RunState.HALTED_BUDGET)
        park(machine, second, "run-executing", RunState.EXECUTING)
        park(machine, second, "run-queued", RunState.QUEUED)

        a_year = 365 * 24 * 60 * 60
        for _ in range(4):
            clock.advance(a_year // 4)
            machine.sweep_stalled()
            queue.snapshot()

        # Neither spec archived, and not one run left for a terminal state. The
        # sweep may legitimately have stalled the executing run; nothing may have
        # cancelled, failed, or discarded anything on elapsed time.
        assert queue.is_archived(ref) is False
        assert queue.is_archived(second) is False
        states = {record.run_id: record.state for record in machine.store.list_runs()}
        assert states == {
            "run-review": RunState.STALLED.value,
            "run-budget": RunState.HALTED_BUDGET.value,
            "run-executing": RunState.STALLED.value,
            "run-queued": RunState.QUEUED.value,
        }
        # Still listed, still resumable: stalled is a notification, not an expiry.
        assert set(queue.snapshot().run_ids) == {"run-review", "run-budget", "run-executing"}


class TestArchivedStaysArchived:
    def test_a_lock_held_for_another_spec_is_refused_rather_than_accepted(
        self,
        store: StateStore,
        queue: ReviewQueue,
        ref: SpecRef,
        tmp_path: Path,
    ) -> None:
        """A valid handle is not the same as a handle for THIS spec.

        The store validates a handle against the row for the handle's own ref, so
        a caller holding a genuine lock on a different spec would pass that check
        and then write here with nothing held. The cascade that cancels runs and
        archives in one block is only atomic because the lock covers it, and a
        foreign handle turns that into no guarantee at all.
        """
        other_project = tmp_path / "elsewhere"
        make_spec_dir(other_project, "example")
        other = SpecRef.of(other_project, "example")

        with store.lock(other, owner="user:someone") as foreign:
            with pytest.raises(StatePersistenceError):
                queue.archive(ref, actor="user:someone", lock=foreign)

        assert queue.is_archived(ref) is False

    def test_no_operation_but_an_explicit_unarchive_brings_a_spec_back(
        self,
        machine: RunMachine,
        queue: ReviewQueue,
        ref: SpecRef,
        clock: FakeClock,
    ) -> None:
        (ref.spec_dir / "tasks.md").write_text(TASKS_DOC, encoding="utf-8")
        park(machine, ref, "run-1", RunState.EXECUTING, item_id="42")
        park(machine, ref, "run-2", RunState.STALLED)
        queue.archive(ref, actor="user:someone")

        # Every operation the engine can perform on this spec, each of which
        # writes the spec row or a run row under the same lock.
        machine.transition(ref, "run-1", RunState.AWAITING_REVIEW)
        assert queue.is_archived(ref) is True
        machine.record_task_status(ref, "run-1", "1", TaskStatus.COMPLETE)
        assert queue.is_archived(ref) is True
        machine.resume(ref, "run-2")
        assert queue.is_archived(ref) is True
        clock.advance(10 * 24 * 60 * 60)
        machine.sweep_stalled()
        assert queue.is_archived(ref) is True
        queue.snapshot()
        assert queue.is_archived(ref) is True
        queue.archive(ref, actor="user:someone")
        assert queue.is_archived(ref) is True
        machine.store.register_spec(ref, phase="design")
        assert queue.is_archived(ref) is True
        machine.create(ref, run_id="run-3")
        assert queue.is_archived(ref) is True

        result = queue.unarchive(ref, actor="user:someone")

        assert result.archived is False
        assert result.changed is True
        assert queue.is_archived(ref) is False

    def test_a_refused_archival_moves_the_flag_in_neither_direction(
        self, queue: ReviewQueue, ref: SpecRef
    ) -> None:
        # Refused against a live spec: the cause is checked before the write, so
        # a rejected archival cannot have archived anything.
        with pytest.raises(ArchivalRefused):
            queue.archive(ref, cause="expired")
        assert queue.is_archived(ref) is False

        queue.archive(ref, actor="user:someone")
        with pytest.raises(ArchivalRefused):
            queue.archive(ref, cause="expired")
        assert queue.is_archived(ref) is True

    def test_archiving_an_archived_spec_reports_no_change(
        self, queue: ReviewQueue, ref: SpecRef
    ) -> None:
        assert queue.archive(ref, actor="user:someone").changed is True
        second = queue.archive(ref, actor="user:another")
        assert second.changed is False
        assert second.archived is True

    def test_unarchiving_a_live_spec_reports_no_change(
        self, queue: ReviewQueue, ref: SpecRef
    ) -> None:
        result = queue.unarchive(ref, actor="user:someone")
        assert result.changed is False
        assert queue.is_archived(ref) is False


class TestReversible:
    def test_archiving_drops_the_run_from_the_queue_and_unarchiving_restores_it(
        self, machine: RunMachine, queue: ReviewQueue, ref: SpecRef
    ) -> None:
        park(machine, ref, "run-1", RunState.AWAITING_REVIEW, item_id="42")
        before = queue.snapshot().entries[0]

        queue.archive(ref, actor="user:someone")

        # Two independent observables: the stored flag and the projection. A bug
        # that moved only one of them would satisfy an assertion on either alone.
        assert queue.is_archived(ref) is True
        assert queue.snapshot().run_ids == ()

        queue.unarchive(ref, actor="user:someone")

        assert queue.is_archived(ref) is False
        restored = queue.snapshot().entries[0]
        assert _identity(restored) == _identity(before)
        # Nothing was deleted on the way through: the run row is the same row.
        assert machine.state_of("run-1") is RunState.AWAITING_REVIEW

    def test_unarchiving_does_not_queue_a_run_that_waits_on_no_person(
        self, machine: RunMachine, queue: ReviewQueue, ref: SpecRef
    ) -> None:
        park(machine, ref, "run-1", RunState.EXECUTING)
        queue.archive(ref, actor="user:someone")
        queue.unarchive(ref, actor="user:someone")

        # Reversible means the spec comes back, not that archival invents a queue
        # place the run never had.
        assert queue.is_archived(ref) is False
        assert queue.snapshot().run_ids == ()

    def test_both_directions_are_recorded_with_their_initiator(
        self, queue: ReviewQueue, ref: SpecRef, audit: AuditLog
    ) -> None:
        queue.archive(ref, actor="user:someone")
        queue.unarchive(ref, actor="user:another")

        recorded = [
            (event.event, event.initiator)
            for event in audit.read(ref)
            if event.event in (review_queue.SPEC_ARCHIVED_EVENT, review_queue.SPEC_UNARCHIVED_EVENT)
        ]
        assert recorded == [
            (review_queue.SPEC_ARCHIVED_EVENT, "user:someone"),
            (review_queue.SPEC_UNARCHIVED_EVENT, "user:another"),
        ]


class TestCancelledItemArchival:
    def test_a_cancelled_item_cancels_its_runs_and_archives_the_spec(
        self, machine: RunMachine, queue: ReviewQueue, ref: SpecRef
    ) -> None:
        park(machine, ref, "run-1", RunState.AWAITING_REVIEW, item_id="42")

        result = queue.archive_cancelled_item(ref, item_id="42", actor="watcher:github")

        assert result.cause is ArchiveCause.ITEM_CANCELLED
        assert result.cancelled_runs == ("run-1",)
        assert machine.state_of("run-1") is RunState.CANCELLED
        assert queue.is_archived(ref) is True
        assert queue.snapshot().run_ids == ()

    def test_a_run_from_a_different_item_is_left_running(
        self, machine: RunMachine, queue: ReviewQueue, ref: SpecRef
    ) -> None:
        park(machine, ref, "run-42", RunState.AWAITING_REVIEW, item_id="42")
        park(machine, ref, "run-43", RunState.AWAITING_REVIEW, item_id="43")

        result = queue.archive_cancelled_item(ref, item_id="42")

        assert result.cancelled_runs == ("run-42",)
        assert machine.state_of("run-42") is RunState.CANCELLED
        assert machine.state_of("run-43") is RunState.AWAITING_REVIEW

    def test_a_finished_run_is_not_rewritten_to_cancelled(
        self, machine: RunMachine, queue: ReviewQueue, ref: SpecRef
    ) -> None:
        park(machine, ref, "run-done", RunState.DONE, item_id="42")

        result = queue.archive_cancelled_item(ref, item_id="42")

        assert result.cancelled_runs == ()
        assert machine.state_of("run-done") is RunState.DONE

    def test_the_cascade_takes_the_lock_once_and_accepts_a_caller_holding_it(
        self, machine: RunMachine, queue: ReviewQueue, ref: SpecRef
    ) -> None:
        park(machine, ref, "run-1", RunState.AWAITING_REVIEW, item_id="42")

        with machine.store.lock(ref, owner="watcher") as handle:
            # Re-locking under a lock the caller already holds is refused by the
            # store rather than waited for, so the handle has to be passed
            # through every nested write.
            with pytest.raises(SpecLocked):
                queue.archive_cancelled_item(ref, item_id="42")
            result = queue.archive_cancelled_item(ref, item_id="42", lock=handle)

        assert result.cancelled_runs == ("run-1",)
        assert queue.is_archived(ref) is True

    def test_claiming_a_cancelled_item_without_naming_it_is_refused(
        self, queue: ReviewQueue, ref: SpecRef
    ) -> None:
        with pytest.raises(ArchivalRefused):
            queue.archive(ref, cause=ArchiveCause.ITEM_CANCELLED)
        with pytest.raises(ArchivalRefused):
            queue.archive_cancelled_item(ref, item_id="   ")
        assert queue.is_archived(ref) is False


def _identity(entry: QueueEntry) -> tuple[object, ...]:
    """The parts of an entry that describe the run rather than the moment."""
    return (
        entry.run_id,
        entry.project,
        entry.spec,
        entry.spec_type,
        entry.state,
        entry.waiting_on,
        entry.entered_ts,
        entry.source,
        entry.item_id,
        entry.gate,
    )


def _hash_of(ref: SpecRef, filename: str) -> str:
    return content_hash((ref.spec_dir / filename).read_text(encoding="utf-8"))
