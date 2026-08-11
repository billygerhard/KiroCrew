"""The per-run ceiling: the halt, the amount in the notification, and the warning.

Four claims are load-bearing here, and each is asserted rather than inferred:

* an install that configures nothing still runs under a finite ceiling, and a
  headless run with no ceiling in force does not execute at all;
* the halt stops the next dispatch and lets turns already in flight settle;
* every halt and warning message carries the consumed amount, not just the fact;
* the ceiling fires on its own, with no watch-source cap involved.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.apps.builtins.spec_engine.engine.audit import AuditLog
from kiro_crew.apps.builtins.spec_engine.engine.budget import (
    AUDIT_EVENT_HALTED,
    AUDIT_EVENT_REFUSED,
    AUDIT_EVENT_WARNING,
    CEILING_SETTING,
    RUN_STATE_HALTED_BUDGET,
    WARN_FRACTION_SETTING,
    Budget,
    BudgetGuard,
    BudgetHalted,
    DispatchOutcome,
    MeteringLedger,
    RecordingNotifier,
    RunAccounting,
    format_credits,
    guard_for,
    resolve_budget,
)
from kiro_crew.apps.builtins.spec_engine.engine.capabilities import TRANSPORT_COMMAND
from kiro_crew.apps.builtins.spec_engine.engine.config import (
    DASHBOARD_SURFACE,
    SETTINGS,
    ConfigStore,
    ValueOrigin,
)
from kiro_crew.apps.builtins.spec_engine.engine.runs import RunMachine, RunState
from kiro_crew.apps.builtins.spec_engine.engine.state import SpecRef, StateStore

from .test_budget_ledger import seed_shard, turn
from .test_capability_registry import StubTransport, bind, registry_with, request_for
from .test_capability_schemas import response_payload

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
def notifier() -> RecordingNotifier:
    return RecordingNotifier()


@pytest.fixture()
def machine(store: StateStore, config: ConfigStore) -> RunMachine:
    return RunMachine(store, config)


def make_run(store: StateStore, ref: SpecRef, run_id: str = RUN) -> str:
    """A run in a state work can be dispatched from."""
    store.create_run(run_id, ref, state=RunState.EXECUTING.value)
    return run_id


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


def guard(
    store: StateStore,
    ref: SpecRef,
    accounting: RunAccounting,
    machine: RunMachine,
    *,
    ceiling: float = 5.0,
    warn_fraction: float = 0.0,
    notifier: RecordingNotifier | None = None,
    audit: AuditLog | None = None,
    headless: bool = False,
    run_id: str = RUN,
) -> BudgetGuard:
    return BudgetGuard(
        run_id,
        ref,
        Budget(ceiling_credits=ceiling, warn_fraction=warn_fraction),
        state=store,
        machine=machine,
        accounting=accounting,
        notifier=notifier,
        audit=audit,
        headless=headless,
    )


class TestCeilingResolution:
    def test_an_unconfigured_install_still_has_a_finite_ceiling(self, config: ConfigStore) -> None:
        budget = resolve_budget(config)
        assert budget.ceiling_origin is ValueOrigin.BUNDLED_DEFAULT
        assert budget.bounded
        assert budget.ceiling_credits == SETTINGS[CEILING_SETTING].default

    def test_configuration_overrides_the_ceiling_and_reports_where_from(
        self, config: ConfigStore
    ) -> None:
        config.write({"budget": {"run_ceiling_credits": 12.5}}, surface=DASHBOARD_SURFACE)
        budget = resolve_budget(config)
        assert budget.ceiling_credits == pytest.approx(12.5)
        assert budget.ceiling_origin is ValueOrigin.APP_CONFIG
        assert budget.declared_at == "budget.run_ceiling_credits"

    def test_a_project_ceiling_beats_the_app_one(self, config: ConfigStore) -> None:
        config.write(
            {
                "budget": {"run_ceiling_credits": 12.5},
                "projects": {"acme": {"path": "/srv/acme", "budget": {"run_ceiling_credits": 2.0}}},
            },
            surface=DASHBOARD_SURFACE,
        )
        assert resolve_budget(config, project="acme").ceiling_credits == pytest.approx(2.0)
        assert resolve_budget(config).ceiling_credits == pytest.approx(12.5)

    def test_a_ceiling_that_is_not_finite_or_positive_is_no_ceiling(self) -> None:
        assert not Budget(ceiling_credits=float("inf")).bounded
        assert not Budget(ceiling_credits=0.0).bounded
        assert Budget(ceiling_credits=0.01).bounded

    def test_the_warning_threshold_is_a_fraction_of_the_ceiling(self, config: ConfigStore) -> None:
        default_fraction = SETTINGS[WARN_FRACTION_SETTING].default
        budget = resolve_budget(config)
        assert budget.warn_at == pytest.approx(budget.ceiling_credits * default_fraction)
        # Raising the ceiling moves the warning with it rather than leaving it
        # where it would fire at once.
        raised = Budget(ceiling_credits=100.0, warn_fraction=default_fraction)
        assert raised.warn_at == pytest.approx(100.0 * default_fraction)

    def test_no_warning_fraction_means_no_warning_threshold(self) -> None:
        assert Budget(ceiling_credits=5.0, warn_fraction=0.0).warn_at is None


class TestHeadlessRunsNeedACeiling:
    def test_a_headless_run_with_no_ceiling_in_force_does_not_execute(
        self,
        store: StateStore,
        ref: SpecRef,
        accounting: RunAccounting,
        machine: RunMachine,
        notifier: RecordingNotifier,
    ) -> None:
        make_run(store, ref)
        decision = guard(
            store,
            ref,
            accounting,
            machine,
            ceiling=float("inf"),
            notifier=notifier,
            headless=True,
        ).authorize_dispatch()
        assert decision.outcome is DispatchOutcome.UNBOUNDED
        assert not decision.allowed
        assert "no budget ceiling is in force" in decision.message
        assert notifier.sent, "an operator is told why the run did not start"

    def test_the_refused_run_is_parked_and_the_operator_told_once(
        self,
        store: StateStore,
        ref: SpecRef,
        accounting: RunAccounting,
        machine: RunMachine,
        notifier: RecordingNotifier,
    ) -> None:
        store.create_run(RUN, ref, state=RunState.QUEUED.value)
        refusing = guard(
            store,
            ref,
            accounting,
            machine,
            ceiling=float("inf"),
            notifier=notifier,
            headless=True,
        )
        for _ in range(3):
            assert refusing.authorize_dispatch().outcome is DispatchOutcome.UNBOUNDED
        # Parked, so configuring a ceiling later resumes the work rather than
        # leaving a run sitting still with no state saying why.
        record = store.get_run(RUN)
        assert record is not None
        assert record.state == RUN_STATE_HALTED_BUDGET.value
        assert len(notifier.sent) == 1

    def test_a_headless_run_under_the_bundled_default_ceiling_executes(
        self,
        store: StateStore,
        ref: SpecRef,
        config: ConfigStore,
        accounting: RunAccounting,
    ) -> None:
        # Nothing configured: the bundled default is what keeps the run bounded.
        make_run(store, ref)
        headless = guard_for(
            RUN,
            ref,
            state=store,
            config=config,
            accounting=accounting,
            headless=True,
        )
        assert headless.budget.ceiling_origin is ValueOrigin.BUNDLED_DEFAULT
        assert headless.authorize_dispatch().allowed

    def test_an_attended_run_without_a_ceiling_is_not_refused(
        self, store: StateStore, ref: SpecRef, accounting: RunAccounting, machine: RunMachine
    ) -> None:
        # The headless refusal exists because nobody is watching; with a human
        # driving, the absence of a ceiling is theirs to own.
        make_run(store, ref)
        decision = guard(
            store, ref, accounting, machine, ceiling=float("inf"), headless=False
        ).authorize_dispatch()
        assert decision.allowed

    def test_a_guard_for_an_unknown_run_is_refused(
        self, store: StateStore, ref: SpecRef, accounting: RunAccounting, machine: RunMachine
    ) -> None:
        with pytest.raises(KeyError):
            guard(store, ref, accounting, machine, run_id="never-created")


class TestHaltAtTheCeiling:
    def test_reaching_the_ceiling_halts_dispatch_and_marks_the_run(
        self,
        store: StateStore,
        ref: SpecRef,
        accounting: RunAccounting,
        machine: RunMachine,
        ledger_path: Path,
        notifier: RecordingNotifier,
    ) -> None:
        make_run(store, ref)
        spend_credits(accounting, ledger_path, RUN, 3.0, 2.5)
        decision = guard(store, ref, accounting, machine, ceiling=5.0, notifier=notifier)
        outcome = decision.authorize_dispatch()
        assert outcome.outcome is DispatchOutcome.HALTED
        assert not outcome.allowed
        record = store.get_run(RUN)
        assert record is not None
        assert record.state == RUN_STATE_HALTED_BUDGET.value
        assert record.cost_credits == pytest.approx(5.5)

    def test_the_notification_carries_the_consumed_amount_and_the_ceiling(
        self,
        store: StateStore,
        ref: SpecRef,
        accounting: RunAccounting,
        machine: RunMachine,
        ledger_path: Path,
        notifier: RecordingNotifier,
    ) -> None:
        make_run(store, ref)
        spend_credits(accounting, ledger_path, RUN, 4.0, 2.0)
        outcome = guard(
            store, ref, accounting, machine, ceiling=5.0, notifier=notifier
        ).authorize_dispatch()
        message = notifier.messages()[0]
        # "Budget exceeded" with no number tells an operator nothing actionable.
        assert format_credits(6.0) in message
        assert format_credits(5.0) in message
        assert message == outcome.message
        detail = notifier.sent[0]["detail"]
        assert detail["consumed_credits"] == pytest.approx(6.0)
        assert detail["ceiling_credits"] == pytest.approx(5.0)
        assert len(detail["sessions"]) == 2

    def test_the_halt_is_notified_once_however_often_dispatch_is_asked(
        self,
        store: StateStore,
        ref: SpecRef,
        accounting: RunAccounting,
        machine: RunMachine,
        ledger_path: Path,
        notifier: RecordingNotifier,
    ) -> None:
        make_run(store, ref)
        spend_credits(accounting, ledger_path, RUN, 6.0)
        halting = guard(store, ref, accounting, machine, ceiling=5.0, notifier=notifier)
        for _ in range(3):
            assert halting.authorize_dispatch().outcome is DispatchOutcome.HALTED
        assert len(notifier.sent) == 1

    def test_a_restarted_guard_re_reads_the_halt_without_re_notifying(
        self,
        store: StateStore,
        ref: SpecRef,
        accounting: RunAccounting,
        machine: RunMachine,
        ledger_path: Path,
    ) -> None:
        make_run(store, ref)
        spend_credits(accounting, ledger_path, RUN, 6.0)
        first = RecordingNotifier()
        guard(store, ref, accounting, machine, ceiling=5.0, notifier=first).authorize_dispatch()
        resumed = RecordingNotifier()
        again = guard(store, ref, accounting, machine, ceiling=5.0, notifier=resumed)
        assert again.halted
        assert again.authorize_dispatch().outcome is DispatchOutcome.HALTED
        assert resumed.sent == []
        assert len(first.sent) == 1

    def test_in_flight_turns_settle_after_the_halt_while_new_ones_are_refused(
        self,
        store: StateStore,
        ref: SpecRef,
        accounting: RunAccounting,
        machine: RunMachine,
        ledger_path: Path,
    ) -> None:
        make_run(store, ref)
        under = guard(store, ref, accounting, machine, ceiling=5.0)
        under.open_turn()
        under.open_turn()
        assert under.in_flight == 2

        # The ceiling is crossed while those two turns are still running.
        spend_credits(accounting, ledger_path, RUN, 5.5)
        outcome = under.authorize_dispatch()
        assert outcome.outcome is DispatchOutcome.HALTED
        assert outcome.in_flight == 2
        assert outcome.draining
        assert under.draining

        with pytest.raises(BudgetHalted):
            under.open_turn()

        under.settle_turn()
        under.settle_turn()
        assert under.in_flight == 0
        assert under.halted
        assert not under.draining

    def test_spend_below_the_ceiling_is_allowed_and_reports_headroom(
        self,
        store: StateStore,
        ref: SpecRef,
        accounting: RunAccounting,
        machine: RunMachine,
        ledger_path: Path,
    ) -> None:
        make_run(store, ref)
        spend_credits(accounting, ledger_path, RUN, 1.0, 0.5)
        decision = guard(store, ref, accounting, machine, ceiling=5.0).authorize_dispatch()
        assert decision.allowed
        assert decision.consumed_credits == pytest.approx(1.5)
        assert decision.remaining_credits == pytest.approx(3.5)

    def test_the_ceiling_counts_declared_provider_cost_too(
        self,
        store: StateStore,
        ref: SpecRef,
        accounting: RunAccounting,
        machine: RunMachine,
        ledger_path: Path,
    ) -> None:
        make_run(store, ref)
        spend_credits(accounting, ledger_path, RUN, 4.0)
        accounting.cost_sink.attribute(
            run=RUN, capability="analysis", provider="external", credits=1.5
        )
        outcome = guard(store, ref, accounting, machine, ceiling=5.0).authorize_dispatch()
        assert outcome.outcome is DispatchOutcome.HALTED
        assert outcome.consumed_credits == pytest.approx(5.5)

    def test_the_halt_is_recorded_in_the_specs_audit_log(
        self,
        store: StateStore,
        ref: SpecRef,
        accounting: RunAccounting,
        machine: RunMachine,
        ledger_path: Path,
        tmp_path: Path,
    ) -> None:
        make_run(store, ref)
        spend_credits(accounting, ledger_path, RUN, 7.0)
        log = AuditLog(tmp_path / "audit")
        guard(store, ref, accounting, machine, ceiling=5.0, audit=log).authorize_dispatch()
        events = [event for event in log.read(ref) if event.event == AUDIT_EVENT_HALTED]
        assert len(events) == 1
        assert events[0].run == RUN
        assert events[0].cost == pytest.approx(7.0)

    def test_a_failing_notification_does_not_unwind_the_halt(
        self,
        store: StateStore,
        ref: SpecRef,
        accounting: RunAccounting,
        machine: RunMachine,
        ledger_path: Path,
    ) -> None:
        class BrokenNotifier:
            def notify(self, *, channel: str, message: str, detail: dict[str, Any]) -> None:
                raise RuntimeError("channel unreachable")

        make_run(store, ref)
        spend_credits(accounting, ledger_path, RUN, 6.0)
        halting = BudgetGuard(
            RUN,
            ref,
            Budget(ceiling_credits=5.0),
            state=store,
            machine=machine,
            accounting=accounting,
            notifier=BrokenNotifier(),
        )
        assert halting.authorize_dispatch().outcome is DispatchOutcome.HALTED
        assert halting.halted

    def test_the_halt_uses_a_lock_the_caller_already_holds(
        self,
        store: StateStore,
        ref: SpecRef,
        accounting: RunAccounting,
        machine: RunMachine,
        ledger_path: Path,
    ) -> None:
        # The store's lock is not re-entrant, so a dispatcher deciding this from
        # inside its own locked operation has to be able to lend its handle.
        make_run(store, ref)
        spend_credits(accounting, ledger_path, RUN, 6.0)
        halting = guard(store, ref, accounting, machine, ceiling=5.0)
        with store.lock(ref, owner="dispatcher") as held:
            decision = halting.authorize_dispatch(lock=held)
        assert decision.outcome is DispatchOutcome.HALTED
        assert halting.halted

    def test_a_spec_another_writer_holds_leaves_the_park_for_the_next_check(
        self,
        store: StateStore,
        ref: SpecRef,
        accounting: RunAccounting,
        machine: RunMachine,
        ledger_path: Path,
        notifier: RecordingNotifier,
    ) -> None:
        make_run(store, ref)
        spend_credits(accounting, ledger_path, RUN, 6.0)
        halting = guard(store, ref, accounting, machine, ceiling=5.0, notifier=notifier)
        with store.lock(ref, owner="somebody-else"):
            blocked = halting.authorize_dispatch()
        # Dispatch is refused either way; nothing is spent while the park waits.
        assert blocked.outcome is DispatchOutcome.HALTED
        assert not halting.halted
        assert notifier.sent == []
        # Once the writer is done the halt lands, and notifies then.
        assert halting.authorize_dispatch().outcome is DispatchOutcome.HALTED
        assert halting.halted
        assert len(notifier.sent) == 1

    def test_a_terminal_run_is_not_rewritten_by_the_ceiling(
        self,
        store: StateStore,
        ref: SpecRef,
        accounting: RunAccounting,
        machine: RunMachine,
        ledger_path: Path,
    ) -> None:
        store.create_run(RUN, ref, state=RunState.DONE.value)
        spend_credits(accounting, ledger_path, RUN, 6.0)
        decision = guard(store, ref, accounting, machine, ceiling=5.0).authorize_dispatch()
        assert decision.outcome is DispatchOutcome.HALTED
        record = store.get_run(RUN)
        assert record is not None
        assert record.state == RunState.DONE.value


class TestIndependenceFromSourceCaps:
    def test_a_run_halts_on_its_own_ceiling_with_no_source_cap_configured(
        self,
        store: StateStore,
        ref: SpecRef,
        config: ConfigStore,
        accounting: RunAccounting,
        ledger_path: Path,
        notifier: RecordingNotifier,
    ) -> None:
        # A source entry whose own spending cap is generous and nowhere near being
        # reached: the run must still stop on its own number.
        config.write(
            {
                "budget": {"run_ceiling_credits": 2.0},
                "sources": {
                    "tracker": {
                        "enabled": True,
                        "poll": ["tracker-cli", "list"],
                        "spend_cap": {"credits": 1000.0, "period_days": 30},
                    }
                },
            },
            surface=DASHBOARD_SURFACE,
        )
        make_run(store, ref)
        spend_credits(accounting, ledger_path, RUN, 1.5, 1.0)
        halting = guard_for(
            RUN,
            ref,
            state=store,
            config=config,
            accounting=accounting,
            notifier=notifier,
        )
        outcome = halting.authorize_dispatch()
        assert outcome.outcome is DispatchOutcome.HALTED
        assert outcome.consumed_credits == pytest.approx(2.5)


class TestWarningThreshold:
    def test_crossing_the_threshold_notifies_without_halting(
        self,
        store: StateStore,
        ref: SpecRef,
        accounting: RunAccounting,
        machine: RunMachine,
        ledger_path: Path,
        notifier: RecordingNotifier,
    ) -> None:
        make_run(store, ref)
        spend_credits(accounting, ledger_path, RUN, 4.5)
        decision = guard(
            store, ref, accounting, machine, ceiling=5.0, warn_fraction=0.8, notifier=notifier
        ).authorize_dispatch()
        assert decision.allowed
        assert decision.warned
        record = store.get_run(RUN)
        assert record is not None
        assert record.state == RunState.EXECUTING.value
        message = notifier.messages()[0]
        assert format_credits(4.5) in message
        assert format_credits(5.0) in message

    def test_the_warning_is_sent_once_not_on_every_dispatch(
        self,
        store: StateStore,
        ref: SpecRef,
        accounting: RunAccounting,
        machine: RunMachine,
        ledger_path: Path,
        notifier: RecordingNotifier,
    ) -> None:
        make_run(store, ref)
        spend_credits(accounting, ledger_path, RUN, 4.5)
        warning = guard(
            store, ref, accounting, machine, ceiling=5.0, warn_fraction=0.8, notifier=notifier
        )
        assert warning.authorize_dispatch().warned
        assert not warning.authorize_dispatch().warned
        assert len(notifier.sent) == 1

    def test_below_the_threshold_nothing_is_sent(
        self,
        store: StateStore,
        ref: SpecRef,
        accounting: RunAccounting,
        machine: RunMachine,
        ledger_path: Path,
        notifier: RecordingNotifier,
    ) -> None:
        make_run(store, ref)
        spend_credits(accounting, ledger_path, RUN, 1.0)
        decision = guard(
            store, ref, accounting, machine, ceiling=5.0, warn_fraction=0.8, notifier=notifier
        ).authorize_dispatch()
        assert decision.allowed
        assert not decision.warned
        assert notifier.sent == []

    def test_a_halted_run_sends_no_warnings(
        self,
        store: StateStore,
        ref: SpecRef,
        accounting: RunAccounting,
        machine: RunMachine,
        ledger_path: Path,
        notifier: RecordingNotifier,
    ) -> None:
        make_run(store, ref)
        spend_credits(accounting, ledger_path, RUN, 9.0)
        halting = guard(
            store, ref, accounting, machine, ceiling=5.0, warn_fraction=0.5, notifier=notifier
        )
        assert halting.authorize_dispatch().outcome is DispatchOutcome.HALTED
        assert not halting.authorize_dispatch().warned
        kinds = [entry["message"] for entry in notifier.sent]
        assert len(kinds) == 1
        assert "halted for budget" in kinds[0]

    def test_no_warning_threshold_means_only_the_halt_notifies(
        self,
        store: StateStore,
        ref: SpecRef,
        accounting: RunAccounting,
        machine: RunMachine,
        ledger_path: Path,
        notifier: RecordingNotifier,
    ) -> None:
        make_run(store, ref)
        spend_credits(accounting, ledger_path, RUN, 4.9)
        quiet = guard(
            store, ref, accounting, machine, ceiling=5.0, warn_fraction=0.0, notifier=notifier
        )
        assert quiet.authorize_dispatch().allowed
        assert notifier.sent == []

    def test_a_warning_is_audited_without_a_state_change(
        self,
        store: StateStore,
        ref: SpecRef,
        accounting: RunAccounting,
        machine: RunMachine,
        ledger_path: Path,
        tmp_path: Path,
    ) -> None:
        make_run(store, ref)
        spend_credits(accounting, ledger_path, RUN, 4.2)
        log = AuditLog(tmp_path / "audit")
        guard(store, ref, accounting, machine, ceiling=5.0, warn_fraction=0.8, audit=log)
        guard(
            store, ref, accounting, machine, ceiling=5.0, warn_fraction=0.8, audit=log
        ).authorize_dispatch()
        events = [event for event in log.read(ref) if event.event == AUDIT_EVENT_WARNING]
        assert len(events) == 1
        assert not [event for event in log.read(ref) if event.event == AUDIT_EVENT_HALTED]


class TestRefusalIsRecorded:
    def test_a_headless_refusal_is_audited_with_the_reason(
        self,
        store: StateStore,
        ref: SpecRef,
        accounting: RunAccounting,
        machine: RunMachine,
        tmp_path: Path,
    ) -> None:
        make_run(store, ref)
        log = AuditLog(tmp_path / "audit")
        guard(
            store, ref, accounting, machine, ceiling=float("inf"), audit=log, headless=True
        ).authorize_dispatch()
        events = [event for event in log.read(ref) if event.event == AUDIT_EVENT_REFUSED]
        assert len(events) == 1
        detail = events[0].detail or {}
        assert detail["headless"] is True
        assert detail["run"] == RUN


class TestStampingThroughTheGuard:
    def test_the_guard_stamps_every_session_and_sums_them(
        self,
        store: StateStore,
        ref: SpecRef,
        accounting: RunAccounting,
        machine: RunMachine,
        ledger_path: Path,
    ) -> None:
        make_run(store, ref)
        bounded = guard(store, ref, accounting, machine, ceiling=5.0)
        for key in ("authoring", "orchestrator", "subagent-a", "subagent-b"):
            assert bounded.stamp_session(key)
        seed_shard(
            ledger_path,
            date.today(),
            [
                turn("authoring", 0.5),
                turn("orchestrator", 0.5),
                turn("subagent-a", 1.0),
                turn("subagent-b", 1.0),
            ],
        )
        assert bounded.sessions == ("authoring", "orchestrator", "subagent-a", "subagent-b")
        assert bounded.spend().total_credits == pytest.approx(3.0)


class TestRegistryCostReachesTheBudget:
    def test_a_delegated_providers_declared_cost_counts_against_the_ceiling(
        self,
        store: StateStore,
        ref: SpecRef,
        accounting: RunAccounting,
        machine: RunMachine,
        tmp_path: Path,
    ) -> None:
        # The registry's cost seam, wired to this budget: a provider that spends
        # in its own process leaves no metering record, so the ceiling would
        # otherwise treat it as free.
        make_run(store, ref)
        config = ConfigStore(tmp_path / "capability-config")
        bind(config, "analysis", transport=TRANSPORT_COMMAND, command=["paid-analyzer"])
        transport = StubTransport(payload=response_payload("analysis", cost={"credits": 2.5}))
        registry = registry_with(config, transport, cost_sink=accounting.cost_sink)

        registry.invoke(request_for(run=RUN))

        assert accounting.spend(RUN).declared_credits == pytest.approx(2.5)
        outcome = guard(store, ref, accounting, machine, ceiling=2.0).authorize_dispatch()
        assert outcome.outcome is DispatchOutcome.HALTED
        assert format_credits(2.5) in outcome.message


class TestPersistedCostCache:
    def test_the_run_row_caches_the_computed_total(
        self,
        store: StateStore,
        ref: SpecRef,
        accounting: RunAccounting,
        machine: RunMachine,
        ledger_path: Path,
    ) -> None:
        make_run(store, ref)
        spend_credits(accounting, ledger_path, RUN, 1.25, 0.75)
        guard(store, ref, accounting, machine, ceiling=5.0).authorize_dispatch()
        record = store.get_run(RUN)
        assert record is not None
        assert record.cost_credits == pytest.approx(2.0)

    def test_the_cached_total_matches_what_a_notification_would_quote(
        self,
        store: StateStore,
        ref: SpecRef,
        accounting: RunAccounting,
        machine: RunMachine,
        ledger_path: Path,
        notifier: RecordingNotifier,
    ) -> None:
        make_run(store, ref)
        spend_credits(accounting, ledger_path, RUN, 5.5)
        guard(store, ref, accounting, machine, ceiling=5.0, notifier=notifier).authorize_dispatch()
        record = store.get_run(RUN)
        assert record is not None
        quoted = json.loads(json.dumps(notifier.sent[0]["detail"]))
        assert quoted["consumed_credits"] == pytest.approx(record.cost_credits)
