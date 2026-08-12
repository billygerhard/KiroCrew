"""The kill switch: one action that stops everything, including what comes later.

The claims here are safety claims, so each is asserted as a *stop* rather than as a
recorded intention to stop:

* no new turn opens for any run while the switch is engaged, including a run
  created after it was thrown and a run whose park could not be written;
* a turn already in flight settles, because that spend has already happened;
* every watcher stops, including a source added to configuration afterwards, and
  the stop is asserted through the scheduler's own entry point rather than through
  the function it happens to call;
* the halt notification and the audit entry carry what the run consumed.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.apps.builtins.spec_engine.engine.audit import AuditLog
from kiro_crew.apps.builtins.spec_engine.engine.budget import (
    AUDIT_EVENT_STOPPED,
    KILL_SWITCH_FILENAME,
    KILL_SWITCH_INITIATOR,
    STOPPABLE_STATES,
    BudgetHalted,
    DispatchOutcome,
    KillSwitch,
    MeteringLedger,
    RecordingNotifier,
    RunAccounting,
    engage_kill_switch,
    format_credits,
    guard_for,
    release_kill_switch,
    stoppable_runs,
)
from kiro_crew.apps.builtins.spec_engine.engine.config import (
    DASHBOARD_SURFACE,
    ConfigStore,
)
from kiro_crew.apps.builtins.spec_engine.engine.runs import (
    PARKED_STATES,
    TERMINAL_STATES,
    RunState,
)
from kiro_crew.apps.builtins.spec_engine.engine.state import (
    SpecRef,
    StatePersistenceError,
    StateStore,
)
from kiro_crew.apps.builtins.spec_engine.engine.watch import poll_tick, run_tick_script

from .conftest import make_spec_dir
from .test_budget_ledger import seed_shard, turn

OPERATOR = "operator-1"


@pytest.fixture()
def config(tmp_path: Path) -> ConfigStore:
    return ConfigStore(tmp_path / "config")


@pytest.fixture()
def ledger_path(tmp_path: Path) -> Path:
    return tmp_path / "usage" / "tokens"


@pytest.fixture()
def accounting(store: StateStore, ledger_path: Path) -> RunAccounting:
    return RunAccounting(store, ledger=MeteringLedger(ledger_path))


@pytest.fixture()
def switch(tmp_path: Path) -> KillSwitch:
    return KillSwitch(tmp_path / "switch-root")


def spend_credits(
    accounting: RunAccounting, ledger_path: Path, run_id: str, *amounts: float
) -> None:
    """Record *amounts* as turns in as many distinct sessions of the run."""
    rows: list[dict[str, Any]] = []
    for index, amount in enumerate(amounts):
        session = f"{run_id}-session-{index}"
        accounting.stamp(run_id, session)
        rows.append(turn(session, amount))
    seed_shard(ledger_path, date.today(), rows)


def make_run(
    store: StateStore,
    ref: SpecRef,
    run_id: str,
    *,
    state: RunState = RunState.EXECUTING,
    source: str | None = None,
) -> str:
    store.create_run(run_id, ref, state=state.value, source=source)
    return run_id


class TestTheFlag:
    def test_nothing_is_stopped_until_somebody_engages_it(self, switch: KillSwitch) -> None:
        assert switch.engaged is False
        assert switch.read().describe() == "kill switch: released"
        assert not switch.path.exists()

    def test_engaging_records_who_stopped_the_engine_and_why(self, switch: KillSwitch) -> None:
        record = switch.engage(initiator=OPERATOR, reason="runaway watcher")

        assert record.engaged is True
        assert record.initiator == OPERATOR
        assert record.reason == "runaway watcher"
        assert record.engaged_ts
        assert switch.engaged is True
        assert switch.path.name == KILL_SWITCH_FILENAME

    def test_the_stop_survives_the_process_that_threw_it(
        self, tmp_path: Path, switch: KillSwitch
    ) -> None:
        switch.engage(initiator=OPERATOR)
        # A second object over the same root is a stand-in for the next process:
        # the flag is on disk, not in the object that wrote it.
        assert KillSwitch(switch.root).engaged is True

    def test_a_second_engage_keeps_the_first_engagement_on_record(
        self, switch: KillSwitch
    ) -> None:
        first = switch.engage(initiator=OPERATOR, reason="first")
        again = switch.engage(initiator="someone-else", reason="second")

        assert again == first
        assert again.initiator == OPERATOR
        assert again.reason == "first"

    def test_releasing_lets_work_start_again(self, switch: KillSwitch) -> None:
        switch.engage(initiator=OPERATOR)

        assert switch.release(initiator=OPERATOR) is True
        assert switch.engaged is False
        assert not switch.path.exists()
        # Releasing what was never engaged is not an error, and says so.
        assert switch.release(initiator=OPERATOR) is False

    def test_a_flag_that_cannot_be_read_reads_as_engaged(self, switch: KillSwitch) -> None:
        switch.path.parent.mkdir(parents=True, exist_ok=True)
        switch.path.write_text("{ this is not json", encoding="utf-8")

        state = switch.read()

        # Doubt is not evidence of release: the only state that means "go" is no
        # file at all.
        assert state.engaged is True
        assert state.unreadable is True
        assert "could not be read" in state.describe()

    def test_a_flag_holding_something_other_than_an_object_reads_as_engaged(
        self, switch: KillSwitch
    ) -> None:
        switch.path.parent.mkdir(parents=True, exist_ok=True)
        switch.path.write_text(json.dumps(["engaged"]), encoding="utf-8")

        assert switch.read().engaged is True

    def test_a_flag_that_says_released_is_released(self, switch: KillSwitch) -> None:
        switch.path.parent.mkdir(parents=True, exist_ok=True)
        switch.path.write_text(json.dumps({"engaged": False}), encoding="utf-8")

        assert switch.engaged is False

    def test_a_stop_that_cannot_be_persisted_fails_the_operation(self, tmp_path: Path) -> None:
        # A file where the root directory has to go: the flag cannot be written,
        # and a switch that reported success would read as released next start.
        blocked = tmp_path / "not-a-directory"
        blocked.write_text("", encoding="utf-8")

        with pytest.raises(StatePersistenceError):
            KillSwitch(blocked).engage(initiator=OPERATOR)

    def test_the_flag_never_lands_in_a_spec_directory(self, project: Path) -> None:
        with pytest.raises(StatePersistenceError):
            KillSwitch(project / ".kiro" / "specs" / "example")


class TestNoNewTurnOpens:
    def test_a_turn_may_not_open_while_the_switch_is_engaged(
        self,
        store: StateStore,
        ref: SpecRef,
        config: ConfigStore,
        accounting: RunAccounting,
        switch: KillSwitch,
    ) -> None:
        make_run(store, ref, "run-1")
        guard = guard_for(
            "run-1", ref, state=store, config=config, accounting=accounting, kill_switch=switch
        )
        assert guard.stopped is False
        guard.open_turn()
        guard.settle_turn()

        switch.engage(initiator=OPERATOR)

        assert guard.stopped is True
        with pytest.raises(BudgetHalted, match="kill switch"):
            guard.open_turn()
        assert guard.in_flight == 0

    def test_an_in_flight_turn_settles_and_no_further_turn_opens(
        self,
        store: StateStore,
        ref: SpecRef,
        config: ConfigStore,
        accounting: RunAccounting,
        switch: KillSwitch,
    ) -> None:
        make_run(store, ref, "run-1")
        guard = guard_for(
            "run-1", ref, state=store, config=config, accounting=accounting, kill_switch=switch
        )
        guard.open_turn()

        switch.engage(initiator=OPERATOR)

        # The turn that was already sent is allowed to finish: its tokens are
        # already spent, and killing it would lose the work as well as the money.
        assert guard.draining is True
        guard.settle_turn()
        assert guard.in_flight == 0
        with pytest.raises(BudgetHalted):
            guard.open_turn()

    def test_a_run_created_after_the_switch_was_thrown_is_stopped_too(
        self,
        store: StateStore,
        ref: SpecRef,
        config: ConfigStore,
        accounting: RunAccounting,
        switch: KillSwitch,
    ) -> None:
        switch.engage(initiator=OPERATOR)
        # Nothing enumerated this run when the switch was thrown; it did not exist.
        make_run(store, ref, "run-late")

        guard = guard_for(
            "run-late", ref, state=store, config=config, accounting=accounting, kill_switch=switch
        )

        with pytest.raises(BudgetHalted, match="kill switch"):
            guard.open_turn()

    def test_dispatch_is_refused_with_the_amount_consumed_so_far(
        self,
        store: StateStore,
        ref: SpecRef,
        config: ConfigStore,
        accounting: RunAccounting,
        ledger_path: Path,
        switch: KillSwitch,
    ) -> None:
        make_run(store, ref, "run-1")
        spend_credits(accounting, ledger_path, "run-1", 0.75, 0.5)
        notifier = RecordingNotifier()
        switch.engage(initiator=OPERATOR, reason="spending too fast")

        decision = guard_for(
            "run-1",
            ref,
            state=store,
            config=config,
            accounting=accounting,
            notifier=notifier,
            kill_switch=switch,
        ).authorize_dispatch()

        assert decision.outcome is DispatchOutcome.STOPPED
        assert not decision.allowed
        assert decision.consumed_credits == pytest.approx(1.25)
        assert f"after consuming {format_credits(1.25)} credits" in decision.message
        assert "kill switch" in decision.message

    def test_releasing_the_switch_lets_a_turn_open_again(
        self,
        store: StateStore,
        ref: SpecRef,
        config: ConfigStore,
        accounting: RunAccounting,
        switch: KillSwitch,
    ) -> None:
        make_run(store, ref, "run-1")
        guard = guard_for(
            "run-1", ref, state=store, config=config, accounting=accounting, kill_switch=switch
        )
        switch.engage(initiator=OPERATOR)
        with pytest.raises(BudgetHalted):
            guard.open_turn()

        release_kill_switch(state=store, initiator=OPERATOR, switch=switch)

        # The run was parked by nothing, so releasing the flag is enough for it.
        guard.open_turn()
        assert guard.in_flight == 1


class TestOneActionStopsEveryRun:
    def test_every_stoppable_run_is_parked_and_reported(
        self,
        store: StateStore,
        project: Path,
        ref: SpecRef,
        config: ConfigStore,
        accounting: RunAccounting,
        ledger_path: Path,
        switch: KillSwitch,
        tmp_path: Path,
    ) -> None:
        make_spec_dir(project, "second")
        other = SpecRef.of(project, "second")
        make_run(store, ref, "run-authoring", state=RunState.AUTHORING)
        make_run(store, other, "run-executing", state=RunState.EXECUTING)
        spend_credits(accounting, ledger_path, "run-authoring", 1.0)
        spend_credits(accounting, ledger_path, "run-executing", 2.5)
        notifier = RecordingNotifier()
        log = AuditLog(tmp_path / "audit")

        report = engage_kill_switch(
            state=store,
            config=config,
            initiator=OPERATOR,
            reason="stop everything",
            switch=switch,
            accounting=accounting,
            notifier=notifier,
            audit=log,
        )

        assert {run.run_id for run in report.parked} == {"run-authoring", "run-executing"}
        for run_id in ("run-authoring", "run-executing"):
            record = store.get_run(run_id)
            assert record is not None
            assert record.state == RunState.HALTED_BUDGET.value
        assert report.total_credits == pytest.approx(3.5)
        # Each run's own amount, in its own message and its own spec's log.
        amounts = sorted(entry["detail"]["consumed_credits"] for entry in notifier.sent)
        assert amounts == pytest.approx([1.0, 2.5])
        for spec, expected in ((ref, 1.0), (other, 2.5)):
            events = [event for event in log.read(spec) if event.event == AUDIT_EVENT_STOPPED]
            assert len(events) == 1
            assert events[0].cost == pytest.approx(expected)
            assert events[0].initiator is None or events[0].initiator == KILL_SWITCH_INITIATOR

    def test_the_halt_notification_names_the_amount_consumed(
        self,
        store: StateStore,
        ref: SpecRef,
        config: ConfigStore,
        accounting: RunAccounting,
        ledger_path: Path,
        switch: KillSwitch,
    ) -> None:
        make_run(store, ref, "run-1")
        spend_credits(accounting, ledger_path, "run-1", 3.25)
        notifier = RecordingNotifier()

        engage_kill_switch(
            state=store,
            config=config,
            initiator=OPERATOR,
            switch=switch,
            accounting=accounting,
            notifier=notifier,
        )

        message = notifier.messages()[0]
        # The phrase pins which number is which: a message that merely contained
        # the digits would still pass if the amount and a limit were swapped.
        assert f"after consuming {format_credits(3.25)} credits" in message

    def test_a_run_created_after_the_stop_is_not_missed(
        self,
        store: StateStore,
        ref: SpecRef,
        config: ConfigStore,
        accounting: RunAccounting,
        switch: KillSwitch,
    ) -> None:
        engage_kill_switch(
            state=store,
            config=config,
            initiator=OPERATOR,
            switch=switch,
            accounting=accounting,
        )
        make_run(store, ref, "run-late")

        # The walk over runs could not have parked this one, and does not need to:
        # the flag is what refuses its first turn.
        guard = guard_for(
            "run-late", ref, state=store, config=config, accounting=accounting, kill_switch=switch
        )
        assert guard.stopped is True
        with pytest.raises(BudgetHalted):
            guard.open_turn()

    def test_a_finished_run_is_left_alone(
        self,
        store: StateStore,
        ref: SpecRef,
        config: ConfigStore,
        accounting: RunAccounting,
        switch: KillSwitch,
    ) -> None:
        make_run(store, ref, "run-done", state=RunState.DONE)

        report = engage_kill_switch(
            state=store,
            config=config,
            initiator=OPERATOR,
            switch=switch,
            accounting=accounting,
        )

        assert report.halted == ()
        record = store.get_run("run-done")
        assert record is not None
        assert record.state == RunState.DONE.value

    def test_the_stoppable_set_is_every_state_that_is_neither_over_nor_parked(self) -> None:
        # Derived rather than listed, so a state added to the lifecycle is stopped
        # without an edit to the switch — a hand-written list is how a stop starts
        # missing things.
        assert set(STOPPABLE_STATES) == set(RunState) - set(TERMINAL_STATES) - set(PARKED_STATES)
        assert RunState.QUEUED in STOPPABLE_STATES
        assert RunState.HALTED_BUDGET not in STOPPABLE_STATES

    def test_a_queued_run_that_never_started_is_stopped(
        self,
        store: StateStore,
        ref: SpecRef,
        config: ConfigStore,
        accounting: RunAccounting,
        switch: KillSwitch,
    ) -> None:
        make_run(store, ref, "run-queued", state=RunState.QUEUED)

        report = engage_kill_switch(
            state=store,
            config=config,
            initiator=OPERATOR,
            switch=switch,
            accounting=accounting,
        )

        assert [run.run_id for run in report.parked] == ["run-queued"]
        assert stoppable_runs(store) == ()

    def test_the_caller_s_own_lock_does_not_reject_the_park(
        self,
        store: StateStore,
        ref: SpecRef,
        config: ConfigStore,
        accounting: RunAccounting,
        switch: KillSwitch,
    ) -> None:
        make_run(store, ref, "run-1")

        with store.lock(ref, owner=OPERATOR) as handle:
            report = engage_kill_switch(
                state=store,
                config=config,
                initiator=OPERATOR,
                switch=switch,
                accounting=accounting,
                lock=handle,
            )

        # The store's lock is not re-entrant, so a stop taken from inside a locked
        # operation is rejected by its own caller unless the handle is forwarded.
        assert [run.run_id for run in report.parked] == ["run-1"]
        record = store.get_run("run-1")
        assert record is not None
        assert record.state == RunState.HALTED_BUDGET.value

    def test_a_spec_another_writer_holds_is_still_stopped_by_the_flag(
        self,
        store: StateStore,
        ref: SpecRef,
        config: ConfigStore,
        accounting: RunAccounting,
        switch: KillSwitch,
    ) -> None:
        make_run(store, ref, "run-1")

        with store.lock(ref, owner="somebody-else"):
            report = engage_kill_switch(
                state=store,
                config=config,
                initiator=OPERATOR,
                switch=switch,
                accounting=accounting,
            )
            assert report.parked == ()
            guard = guard_for(
                "run-1",
                ref,
                state=store,
                config=config,
                accounting=accounting,
                kill_switch=switch,
            )
            # The park is bookkeeping; the flag is the stop, so the run cannot open
            # a turn even though its state column still says executing.
            with pytest.raises(BudgetHalted):
                guard.open_turn()

    def test_a_stop_whose_park_was_refused_still_notifies_and_records(
        self,
        store: StateStore,
        ref: SpecRef,
        config: ConfigStore,
        accounting: RunAccounting,
        ledger_path: Path,
        switch: KillSwitch,
        tmp_path: Path,
    ) -> None:
        """The case the design calls expected is the case an operator needs told.

        A park refused by another writer leaves the run stopped with its state
        column still reading as running. That is precisely when someone has to be
        told, and told what it cost, so the report cannot hang off the bookkeeping
        the same module says may fail.
        """
        make_run(store, ref, "run-1")
        spend_credits(accounting, ledger_path, "run-1", 3.0)
        notifier = RecordingNotifier()
        log = AuditLog(tmp_path / "audit")

        with store.lock(ref, owner="somebody-else"):
            guard = guard_for(
                "run-1",
                ref,
                state=store,
                config=config,
                accounting=accounting,
                kill_switch=switch,
                notifier=notifier,
                audit=log,
            )
            switch.engage(initiator=OPERATOR)
            decision = guard.authorize_dispatch()

        assert decision.outcome is DispatchOutcome.STOPPED
        assert notifier.messages(), "a stop nobody is told about is the failure"
        assert format_credits(3.0) in notifier.messages()[0]
        recorded = [event for event in log.read(ref) if event.event == AUDIT_EVENT_STOPPED]
        assert len(recorded) == 1
        assert recorded[0].detail is not None
        assert recorded[0].detail["kill_switch"] is True
        # The row's fate is what is conditional, and the record says which way.
        assert recorded[0].detail["parked"] is False

    def test_engaging_twice_notifies_once_per_run(
        self,
        store: StateStore,
        ref: SpecRef,
        config: ConfigStore,
        accounting: RunAccounting,
        ledger_path: Path,
        switch: KillSwitch,
    ) -> None:
        make_run(store, ref, "run-1")
        spend_credits(accounting, ledger_path, "run-1", 1.0)
        notifier = RecordingNotifier()

        first = engage_kill_switch(
            state=store,
            config=config,
            initiator=OPERATOR,
            switch=switch,
            accounting=accounting,
            notifier=notifier,
        )
        second = engage_kill_switch(
            state=store,
            config=config,
            initiator=OPERATOR,
            switch=switch,
            accounting=accounting,
            notifier=notifier,
        )

        assert first.already_engaged is False
        assert second.already_engaged is True
        assert len(notifier.sent) == 1


class TestEveryWatcherIsPaused:
    def test_a_tick_polls_nothing_while_the_switch_is_engaged(
        self, tmp_path: Path, switch: KillSwitch
    ) -> None:
        config = ConfigStore(tmp_path / "watch-config")
        config.write(
            {"sources": {"tracker": {"enabled": True, "poll": ["tracker-cli"]}}},
            surface=DASHBOARD_SURFACE,
        )
        calls: list[tuple[str, ...]] = []

        def runner(argv: Any, *, cwd: Path, timeout_s: int) -> Any:  # pragma: no cover - unused
            calls.append(tuple(argv))
            raise AssertionError("a paused watcher must not run its poll command")

        switch.engage(initiator=OPERATOR, reason="stop the watchers")
        report = poll_tick(config, runner=runner, kill_switch=switch)

        assert calls == []
        assert report.outcomes == ()
        assert "kill switch: engaged" in report.paused
        assert report.summary() == report.paused

    def test_the_scheduler_s_own_entry_point_polls_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Driven through the function the cron actually calls, with the switch
        # resolved the way it is in production: from the data home. A pause that
        # only held when a test passed the switch in would not pause anything.
        from kiro_crew.cron_script import Skip

        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(home))
        marker = tmp_path / "polled.txt"
        poll = _marker_command(tmp_path, marker)
        config = ConfigStore(tmp_path / "watch-config")
        config.write(
            {"sources": {"tracker": {"enabled": True, "poll": poll}}},
            surface=DASHBOARD_SURFACE,
        )
        monkeypatch.setattr(
            "kiro_crew.apps.builtins.spec_engine.engine.watch.tick.ConfigStore",
            lambda *args, **kwargs: config,
        )
        store = StateStore()

        engage_kill_switch(state=store, config=config, initiator=OPERATOR)
        with pytest.raises(Skip):
            run_tick_script(_Job("tracker"))

        assert not marker.exists()

        release_kill_switch(state=store, initiator=OPERATOR)
        with pytest.raises(Skip):
            run_tick_script(_Job("tracker"))

        assert marker.exists(), "the same tick polls once the switch is released"

    def test_a_source_added_after_the_stop_is_paused_as_well(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.cron_script import Skip

        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(home))
        config = ConfigStore(tmp_path / "watch-config")
        monkeypatch.setattr(
            "kiro_crew.apps.builtins.spec_engine.engine.watch.tick.ConfigStore",
            lambda *args, **kwargs: config,
        )
        store = StateStore()
        engage_kill_switch(state=store, config=config, initiator=OPERATOR)

        # Configured after the switch was thrown, so nothing could have enumerated
        # it: the stop has to be a condition every tick reads, not a list of jobs.
        marker = tmp_path / "late.txt"
        config.write(
            {"sources": {"late": {"enabled": True, "poll": _marker_command(tmp_path, marker)}}},
            surface=DASHBOARD_SURFACE,
        )

        with pytest.raises(Skip):
            run_tick_script(_Job("late"))

        assert not marker.exists()


class _Job:
    """Stands in for the scheduler's job object: only ``message`` is read."""

    def __init__(self, message: str = "") -> None:
        self.message = message


def _marker_command(tmp_path: Path, marker: Path) -> list[str]:
    """An argv that records that it ran, then prints an empty item list."""
    import sys

    script = tmp_path / f"poll_{marker.stem}.py"
    script.write_text(
        "import pathlib, sys\n"
        f"pathlib.Path({str(marker)!r}).write_text('ran')\n"
        "sys.stdout.write('[]')\n",
        encoding="utf-8",
    )
    return [sys.executable, str(script)]
