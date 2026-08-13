"""A watched item cancelled while its run is in flight cascades: cancel + archive.

The cascade primitive (cancel the runs, archive the spec, audit it, under one
lock) lives in the review queue; this proves the watcher consumes a derived
cancellation into it -- and only while a run is actually in flight, never for a
spec whose runs already finished, and never off a poll that merely failed.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from kiro_crew.apps.builtins.spec_engine.engine.audit import AuditLog
from kiro_crew.apps.builtins.spec_engine.engine.config import ConfigStore
from kiro_crew.apps.builtins.spec_engine.engine.review_queue import ReviewQueue
from kiro_crew.apps.builtins.spec_engine.engine.runs import RunMachine, RunState
from kiro_crew.apps.builtins.spec_engine.engine.state import SpecRef, StateStore
from kiro_crew.apps.builtins.spec_engine.engine.watch import (
    HealthReason,
    PollOutcome,
    PollStatus,
    WatchedItem,
    cascade_cancellations,
    diff_poll,
    record_snapshot,
)
from kiro_crew.apps.builtins.spec_engine.engine.watch.dispatch import CascadeStatus

SOURCE = "tracker"
ITEM = "5"


@pytest.fixture()
def config(tmp_path: Path) -> ConfigStore:
    return ConfigStore(root=tmp_path / "config")


@pytest.fixture()
def state(tmp_path: Path) -> Iterator[StateStore]:
    store = StateStore(root=tmp_path / "state")
    yield store
    store.close()


@pytest.fixture()
def audit(tmp_path: Path) -> AuditLog:
    return AuditLog(root=tmp_path / "state")


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    return root


@pytest.fixture()
def ref(project: Path) -> SpecRef:
    return SpecRef.of(project, "spec-one")


@pytest.fixture()
def queue(state: StateStore, config: ConfigStore, audit: AuditLog) -> ReviewQueue:
    return ReviewQueue(RunMachine(state, config, audit=audit))


def item(identifier: str, *, state_text: str) -> WatchedItem:
    return WatchedItem(source=SOURCE, identifier=identifier, state=state_text)


def polled(*items: WatchedItem) -> PollOutcome:
    return PollOutcome(
        source=SOURCE, status=PollStatus.OK, items=items, program="tracker-cli", exit_code=0
    )


def open_then_closed(state: StateStore):
    """Snapshot the item open, then diff a poll reporting it closed."""
    record_snapshot(state, diff_poll(state, polled(item(ITEM, state_text="open"))))
    return diff_poll(state, polled(item(ITEM, state_text="closed")))


def make_run(state: StateStore, config: ConfigStore, audit: AuditLog, ref: SpecRef) -> str:
    machine = RunMachine(state, config, audit=audit)
    record = machine.create(ref, source=SOURCE, item_id=ITEM)
    return record.run_id


class TestCascade:
    def test_an_in_flight_run_is_cancelled_and_its_spec_archived(
        self, state, config, audit, ref, queue
    ) -> None:
        run_id = make_run(state, config, audit, ref)
        diff = open_then_closed(state)

        results = cascade_cancellations(state, diff, cascade=queue)

        assert [r.status for r in results] == [CascadeStatus.CASCADED]
        assert results[0].archived_specs == ("spec-one",)
        assert state.get_run(run_id).state == RunState.CANCELLED.value
        assert queue.is_archived(ref) is True

    def test_no_in_flight_run_leaves_the_spec_alone(self, state, config, audit, ref, queue) -> None:
        """A cancelled item whose run already finished is history, not work to stop."""
        run_id = make_run(state, config, audit, ref)
        state.update_run(run_id, state=RunState.DONE.value)
        diff = open_then_closed(state)

        results = cascade_cancellations(state, diff, cascade=queue)

        assert [r.status for r in results] == [CascadeStatus.NO_INFLIGHT_RUN]
        assert results[0].archived_specs == ()
        assert state.get_run(run_id).state == RunState.DONE.value
        assert queue.is_archived(ref) is False

    def test_a_failed_poll_cascades_nothing(self, state, config, audit, ref, queue) -> None:
        """A poll that did not run must not read as every open item cancelled."""
        make_run(state, config, audit, ref)
        unhealthy = PollOutcome(
            source=SOURCE,
            status=PollStatus.UNHEALTHY,
            reason=HealthReason.PROGRAM_UNAVAILABLE,
            detail="tracker-cli is not on PATH",
            program="tracker-cli",
        )
        diff = diff_poll(state, unhealthy)

        assert cascade_cancellations(state, diff, cascade=queue) == ()
        assert queue.is_archived(ref) is False

    def test_a_diff_with_no_cancellations_cascades_nothing(
        self, state, config, audit, ref, queue
    ) -> None:
        make_run(state, config, audit, ref)
        diff = diff_poll(state, polled(item(ITEM, state_text="open")))  # new, not cancelled

        assert cascade_cancellations(state, diff, cascade=queue) == ()
        assert queue.is_archived(ref) is False
