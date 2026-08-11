"""Budget enforcement: run attribution and the per-run ceiling.

Import from this package rather than its modules; the split between attribution
and enforcement is an implementation detail.

    from ...engine.budget import guard_for

    guard = guard_for(run_id, ref, state=state, config=config, headless=True)
    guard.stamp_session(session_key)      # every session the run creates
    decision = guard.authorize_dispatch()
    if not decision.allowed:
        return decision.message           # carries the consumed amount

The ceiling is per run and independent of any watch-source spending cap: a run
halts on its own ceiling whether or not a cap ever stopped its dispatch.
"""

from __future__ import annotations

from .ceiling import (
    AUDIT_EVENT_HALTED,
    AUDIT_EVENT_REFUSED,
    AUDIT_EVENT_WARNING,
    CEILING_SETTING,
    CHANNEL_SETTING,
    DETAIL_CEILING_CREDITS,
    DETAIL_CONSUMED_CREDITS,
    HALT_INITIATOR,
    NOTIFY_CLAIM_KIND,
    NOTIFY_HALTED,
    NOTIFY_UNBOUNDED,
    NOTIFY_WARNING,
    RUN_STATE_HALTED_BUDGET,
    WARN_FRACTION_SETTING,
    AuditSink,
    Budget,
    BudgetGuard,
    BudgetHalted,
    DispatchDecision,
    DispatchOutcome,
    Notifier,
    RecordingNotifier,
    format_credits,
    guard_for,
    resolve_budget,
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

__all__ = [
    "AUDIT_EVENT_HALTED",
    "AUDIT_EVENT_REFUSED",
    "AUDIT_EVENT_WARNING",
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
    "LEDGER_SUBPATH",
    "NOTIFY_CLAIM_KIND",
    "NOTIFY_HALTED",
    "NOTIFY_UNBOUNDED",
    "NOTIFY_WARNING",
    "RUN_STATE_HALTED_BUDGET",
    "SESSION_CLAIM_KIND",
    "SESSION_CLAIM_SCOPE",
    "SHARD_SUFFIX",
    "WARN_FRACTION_SETTING",
    "AuditSink",
    "Budget",
    "BudgetGuard",
    "BudgetHalted",
    "DispatchDecision",
    "DispatchOutcome",
    "LedgerTotal",
    "MeteringLedger",
    "Notifier",
    "RecordingNotifier",
    "RunAccounting",
    "RunCostSink",
    "RunSessions",
    "RunSpend",
    "SessionAttributionConflict",
    "format_credits",
    "guard_for",
    "ledger_dir",
    "normalize_session_key",
    "resolve_budget",
]
