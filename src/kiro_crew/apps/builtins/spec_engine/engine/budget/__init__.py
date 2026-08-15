"""Budget enforcement: run attribution, the per-run ceiling, caps, and the stop.

Import from this package rather than its modules; the split between attribution
and enforcement is an implementation detail.

    from ...engine.budget import caps_for, engage_kill_switch, guard_for

    guard = guard_for(run_id, ref, state=state, config=config, headless=True)
    guard.stamp_session(session_key)      # every session the run creates
    decision = guard.authorize_dispatch()
    if not decision.allowed:
        return decision.message           # carries the consumed amount

    gate = caps_for(state, config)        # per-source cap + the kill switch
    if not gate.dispatch_allowed(source):
        return                            # no new run for that source

    engage_kill_switch(state=state, config=config, initiator="operator")

Three controls, deliberately independent:

* the **ceiling** is per run — a run halts on its own number whether or not a cap
  ever stopped its dispatch;
* a **cap** is per source and per period — it stops new dispatches for one source
  while every other source keeps going;
* the **kill switch** is one flag every one of those paths reads per attempt, so a
  single operator action reaches work that did not exist when it was thrown.
"""

from __future__ import annotations

from .caps import (
    CAP_CREDITS_KEY,
    CAP_PERIOD_DAYS_KEY,
    SOURCES_SECTION,
    SPEND_CAP_FIELD,
    CapDecision,
    CapOutcome,
    SourceCap,
    SourceCaps,
    SourceSpend,
    caps_for,
    resolve_source_cap,
)
from .ceiling import (
    AUDIT_EVENT_COMPLETED,
    AUDIT_EVENT_HALTED,
    AUDIT_EVENT_REFUSED,
    AUDIT_EVENT_STOPPED,
    AUDIT_EVENT_WARNING,
    CEILING_SETTING,
    CHANNEL_SETTING,
    DETAIL_CEILING_CREDITS,
    DETAIL_CONSUMED_CREDITS,
    DETAIL_KILL_SWITCH,
    HALT_INITIATOR,
    KILL_SWITCH_INITIATOR,
    NOTIFY_CLAIM_KIND,
    NOTIFY_COMPLETED,
    NOTIFY_HALTED,
    NOTIFY_STOPPED,
    NOTIFY_UNBOUNDED,
    NOTIFY_WARNING,
    RUN_STATE_HALTED_BUDGET,
    WARN_FRACTION_SETTING,
    AuditSink,
    Budget,
    BudgetGuard,
    BudgetHalted,
    CompletionReport,
    DispatchDecision,
    DispatchOutcome,
    Notifier,
    RecordingNotifier,
    format_credits,
    guard_for,
    resolve_budget,
)
from .killswitch import (
    AUDIT_EVENT_RELEASED,
    STOPPABLE_STATES,
    HaltedRun,
    KillSwitchReport,
    engage_kill_switch,
    release_kill_switch,
    stoppable_runs,
)
from .ledger import (
    DECLARED_CALLS_KEY,
    DECLARED_CREDITS_KEY,
    FIELD_CREDITS,
    FIELD_SLOT,
    FIELD_TURNS,
    LEDGER_SUBPATH,
    SESSION_CLAIM_KIND,
    SESSION_CLAIM_SCOPE,
    SHARD_SUFFIX,
    LedgerTotal,
    MeteringLedger,
    RunAccounting,
    RunCostSink,
    RunSessions,
    RunSpend,
    SessionAttributionConflict,
    ledger_dir,
    normalize_session_key,
)
from .switch import (
    KILL_SWITCH_FILENAME,
    KillSwitch,
    KillSwitchState,
)

__all__ = [
    "AUDIT_EVENT_COMPLETED",
    "AUDIT_EVENT_HALTED",
    "AUDIT_EVENT_REFUSED",
    "AUDIT_EVENT_RELEASED",
    "AUDIT_EVENT_STOPPED",
    "DETAIL_KILL_SWITCH",
    "AUDIT_EVENT_WARNING",
    "CAP_CREDITS_KEY",
    "CAP_PERIOD_DAYS_KEY",
    "CEILING_SETTING",
    "CHANNEL_SETTING",
    "DECLARED_CALLS_KEY",
    "DECLARED_CREDITS_KEY",
    "DETAIL_CEILING_CREDITS",
    "DETAIL_CONSUMED_CREDITS",
    "FIELD_CREDITS",
    "FIELD_SLOT",
    "FIELD_TURNS",
    "HALT_INITIATOR",
    "KILL_SWITCH_FILENAME",
    "KILL_SWITCH_INITIATOR",
    "LEDGER_SUBPATH",
    "NOTIFY_CLAIM_KIND",
    "NOTIFY_COMPLETED",
    "NOTIFY_HALTED",
    "NOTIFY_STOPPED",
    "NOTIFY_UNBOUNDED",
    "NOTIFY_WARNING",
    "RUN_STATE_HALTED_BUDGET",
    "SESSION_CLAIM_KIND",
    "SESSION_CLAIM_SCOPE",
    "SHARD_SUFFIX",
    "SOURCES_SECTION",
    "SPEND_CAP_FIELD",
    "STOPPABLE_STATES",
    "WARN_FRACTION_SETTING",
    "AuditSink",
    "Budget",
    "BudgetGuard",
    "BudgetHalted",
    "CapDecision",
    "CapOutcome",
    "CompletionReport",
    "DispatchDecision",
    "DispatchOutcome",
    "HaltedRun",
    "KillSwitch",
    "KillSwitchReport",
    "KillSwitchState",
    "LedgerTotal",
    "MeteringLedger",
    "Notifier",
    "RecordingNotifier",
    "RunAccounting",
    "RunCostSink",
    "RunSessions",
    "RunSpend",
    "SessionAttributionConflict",
    "SourceCap",
    "SourceCaps",
    "SourceSpend",
    "caps_for",
    "engage_kill_switch",
    "format_credits",
    "guard_for",
    "ledger_dir",
    "normalize_session_key",
    "release_kill_switch",
    "resolve_budget",
    "resolve_source_cap",
    "stoppable_runs",
]
