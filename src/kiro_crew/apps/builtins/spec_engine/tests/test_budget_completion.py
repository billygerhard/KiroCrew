"""Completion reporting: what a finished run cost, said once, and recorded.

A halt already tells an operator the amount. A run that finished normally spent
money too, and being told the number only when something went wrong makes an
expensive success indistinguishable from a cheap one. The claims:

* the notification names the run's total consumption across every session it
  created, and the audit entry carries the same number as its cost;
* it is sent once per run, however many callers notice the completion;
* the cached total on the run row and the quoted amount are the same number.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.apps.builtins.spec_engine.engine.audit import AuditLog
from kiro_crew.apps.builtins.spec_engine.engine.budget import (
    AUDIT_EVENT_COMPLETED,
    KillSwitch,
    MeteringLedger,
    RecordingNotifier,
    RunAccounting,
    format_credits,
    guard_for,
)
from kiro_crew.apps.builtins.spec_engine.engine.config import ConfigStore
from kiro_crew.apps.builtins.spec_engine.engine.runs import RunState
from kiro_crew.apps.builtins.spec_engine.engine.state import SpecRef, StateStore

from .test_budget_ledger import seed_shard, turn

RUN = "run-1"


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


def finished_run(
    store: StateStore,
    ref: SpecRef,
    accounting: RunAccounting,
    ledger_path: Path,
    *amounts: float,
    state: RunState = RunState.DONE,
) -> None:
    store.create_run(RUN, ref, state=state.value)
    rows: list[dict[str, Any]] = []
    for index, amount in enumerate(amounts):
        session = f"{RUN}-session-{index}"
        accounting.stamp(RUN, session)
        rows.append(turn(session, amount))
    if rows:
        seed_shard(ledger_path, date.today(), rows)


class TestCompletionCarriesTheAmount:
    def test_the_notification_names_the_total_across_every_session(
        self,
        store: StateStore,
        ref: SpecRef,
        config: ConfigStore,
        accounting: RunAccounting,
        ledger_path: Path,
        switch: KillSwitch,
    ) -> None:
        finished_run(store, ref, accounting, ledger_path, 1.25, 0.75, 2.0)
        notifier = RecordingNotifier()

        report = guard_for(
            RUN,
            ref,
            state=store,
            config=config,
            accounting=accounting,
            notifier=notifier,
            kill_switch=switch,
        ).report_completion()

        assert report.consumed_credits == pytest.approx(4.0)
        assert report.final_state is RunState.DONE
        assert report.notified is True
        # The phrase pins the number as the amount consumed rather than as any
        # other figure that might appear beside it.
        assert f"after consuming {format_credits(4.0)} credits" in notifier.messages()[0]
        assert "ended as done" in notifier.messages()[0]

    def test_the_audit_entry_records_the_cost_in_the_specs_own_log(
        self,
        store: StateStore,
        ref: SpecRef,
        config: ConfigStore,
        accounting: RunAccounting,
        ledger_path: Path,
        switch: KillSwitch,
        tmp_path: Path,
    ) -> None:
        finished_run(store, ref, accounting, ledger_path, 2.5)
        log = AuditLog(tmp_path / "audit")

        guard_for(
            RUN,
            ref,
            state=store,
            config=config,
            accounting=accounting,
            audit=log,
            kill_switch=switch,
        ).report_completion()

        events = [event for event in log.read(ref) if event.event == AUDIT_EVENT_COMPLETED]
        assert len(events) == 1
        assert events[0].cost == pytest.approx(2.5)
        assert events[0].run == RUN
        detail = events[0].detail or {}
        assert detail["consumed_credits"] == pytest.approx(2.5)
        assert detail["final_state"] == RunState.DONE.value

    def test_a_failed_run_reports_what_it_spent_before_failing(
        self,
        store: StateStore,
        ref: SpecRef,
        config: ConfigStore,
        accounting: RunAccounting,
        ledger_path: Path,
        switch: KillSwitch,
    ) -> None:
        finished_run(store, ref, accounting, ledger_path, 3.0, state=RunState.FAILED)
        notifier = RecordingNotifier()

        report = guard_for(
            RUN,
            ref,
            state=store,
            config=config,
            accounting=accounting,
            notifier=notifier,
            kill_switch=switch,
        ).report_completion()

        assert report.final_state is RunState.FAILED
        assert f"after consuming {format_credits(3.0)} credits" in notifier.messages()[0]

    def test_a_run_that_spent_nothing_still_reports_a_number(
        self,
        store: StateStore,
        ref: SpecRef,
        config: ConfigStore,
        accounting: RunAccounting,
        switch: KillSwitch,
    ) -> None:
        store.create_run(RUN, ref, state=RunState.DONE.value)
        notifier = RecordingNotifier()

        report = guard_for(
            RUN,
            ref,
            state=store,
            config=config,
            accounting=accounting,
            notifier=notifier,
            kill_switch=switch,
        ).report_completion()

        assert report.consumed_credits == pytest.approx(0.0)
        assert f"after consuming {format_credits(0.0)} credits" in notifier.messages()[0]

    def test_the_completion_is_reported_once_however_many_callers_notice(
        self,
        store: StateStore,
        ref: SpecRef,
        config: ConfigStore,
        accounting: RunAccounting,
        ledger_path: Path,
        switch: KillSwitch,
        tmp_path: Path,
    ) -> None:
        finished_run(store, ref, accounting, ledger_path, 1.0)
        notifier = RecordingNotifier()
        log = AuditLog(tmp_path / "audit")

        def report() -> Any:
            return guard_for(
                RUN,
                ref,
                state=store,
                config=config,
                accounting=accounting,
                notifier=notifier,
                audit=log,
                kill_switch=switch,
            ).report_completion()

        first = report()
        # A second guard stands in for a resumed run or a second surface: the claim
        # is in the ledger, not in the object that sent the first message.
        second = report()

        assert first.notified is True
        assert second.notified is False
        assert len(notifier.sent) == 1
        assert len([e for e in log.read(ref) if e.event == AUDIT_EVENT_COMPLETED]) == 1

    def test_the_quoted_amount_and_the_cached_total_are_the_same_number(
        self,
        store: StateStore,
        ref: SpecRef,
        config: ConfigStore,
        accounting: RunAccounting,
        ledger_path: Path,
        switch: KillSwitch,
    ) -> None:
        finished_run(store, ref, accounting, ledger_path, 0.5, 1.75)
        notifier = RecordingNotifier()

        guard_for(
            RUN,
            ref,
            state=store,
            config=config,
            accounting=accounting,
            notifier=notifier,
            kill_switch=switch,
        ).report_completion()

        record = store.get_run(RUN)
        assert record is not None
        quoted = json.loads(json.dumps(notifier.sent[0]["detail"]))
        assert quoted["consumed_credits"] == pytest.approx(record.cost_credits)
        assert record.cost_credits == pytest.approx(2.25)
