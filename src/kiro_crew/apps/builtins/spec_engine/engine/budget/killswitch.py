"""The kill switch as one operator action: stop every unattended thing.

A per-run ceiling bounds one run and a per-source cap bounds one source. Neither
answers the question an operator asks when something is visibly wrong: *stop*.
This module is that answer, and its design follows from one failure mode — a stop
that reaches only the work it happened to enumerate.

**The flag is the mechanism; parking runs is the cleanup.** Engaging persists the
flag in :mod:`.switch`, and every place that could start new unattended work reads
that flag per attempt: the watch tick before it polls, the dispatch gate before it
claims an item, and the budget guard before it opens a turn. Nothing holds a list
of what to stop, so a watch source added to configuration after the switch was
thrown is stopped too, and a run created a second later cannot open its first
turn. Walking the run table and parking each run is the *second* step, and it is
bookkeeping: it makes a surface able to say why the work stopped. If it failed for
every run, the work would still be stopped.

**The order is flag first, runs second.** A run that starts between the two steps
is not missed, because it reads the flag at its first turn. Parking first and
flagging second would leave open exactly the gap the switch exists to close.

**In-flight turns settle.** Parking a run stops the *next* dispatch. A turn
already sent is left to finish, for the same reason the ceiling leaves it: the
tokens are already spent, and killing the turn loses the work as well as the
money.

Releasing the switch resumes nothing. A parked run resumes through the run
lifecycle, one run at a time, because the person who stopped everything decides
what starts again and in what order.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ..config import ConfigStore
from ..runs import PARKED_STATES, TERMINAL_STATES, RunMachine, RunState
from ..state import RunRecord, SpecLock, SpecRef, StateStore
from .ceiling import AuditSink, Notifier, guard_for
from .ledger import RunAccounting
from .switch import KillSwitch, KillSwitchState

logger = logging.getLogger(__name__)

#: Event recorded when the switch is released. The engage's own event
#: (:data:`~.ceiling.AUDIT_EVENT_STOPPED`) is written per halted run by the budget
#: guard; this one is the mirror for the direction that restores spending.
AUDIT_EVENT_RELEASED = "budget.kill_switch_released"

#: States a run may be stopped from: everything that is neither finished nor
#: already parked. Derived from the lifecycle's own sets rather than listed, so a
#: state added to the lifecycle is stopped by this switch without an edit here — a
#: hand-written list is how a stop starts missing things.
STOPPABLE_STATES: tuple[RunState, ...] = tuple(
    state for state in RunState if state not in TERMINAL_STATES and state not in PARKED_STATES
)


@dataclass(frozen=True)
class HaltedRun:
    """One run the switch stopped, and what it had consumed."""

    run_id: str
    ref: SpecRef
    consumed_credits: float
    #: Whether the run is IN the parked state now, which is not the same as this
    #: call having moved it: a run already halted for budget when the switch was
    #: thrown short-circuits the park and still reads true here. False when
    #: another writer held the spec or the run could not be moved; neither changes
    #: the fact that no further turn may open, which the flag decides. The audit
    #: record's own ``parked`` detail is the one that says whether a row moved.
    parked: bool
    message: str = ""


@dataclass(frozen=True)
class KillSwitchReport:
    """What one engage did: the record in force, and every run it stopped."""

    state: KillSwitchState
    halted: tuple[HaltedRun, ...] = ()
    #: True when the switch was already engaged before this call.
    already_engaged: bool = False

    @property
    def total_credits(self) -> float:
        """Credits consumed by the runs this call stopped."""
        return sum(run.consumed_credits for run in self.halted)

    @property
    def parked(self) -> tuple[HaltedRun, ...]:
        return tuple(run for run in self.halted if run.parked)

    def describe(self) -> str:
        return (
            f"{self.state.describe()}; {len(self.parked)} of {len(self.halted)} run(s) parked, "
            f"{self.total_credits:.2f} credits consumed"
        )


def stoppable_runs(state: StateStore) -> tuple[RunRecord, ...]:
    """Every run the switch has something to do about.

    Terminal runs are finished and parked runs are already stopped, so the set is
    everything else — read from the run table rather than from a marker a
    dispatcher was supposed to set, because a marker nobody wrote would be a stop
    that halts nothing.
    """
    return tuple(state.list_runs(states=[run_state.value for run_state in STOPPABLE_STATES]))


def engage_kill_switch(
    *,
    state: StateStore,
    config: ConfigStore,
    initiator: str,
    reason: str = "",
    switch: KillSwitch | None = None,
    accounting: RunAccounting | None = None,
    notifier: Notifier | None = None,
    audit: AuditSink | None = None,
    machine: RunMachine | None = None,
    project: str | None = None,
    lock: SpecLock | None = None,
) -> KillSwitchReport:
    """Stop every unattended thing, and report what was stopped.

    The flag lands first and the runs are parked second, so a run created between
    the two steps is still stopped: it reads the flag before its first turn. A
    persistence failure on the flag raises, and nothing is parked — a stop that
    was not recorded must not be reported as one.

    *lock* is a spec lock the caller already holds, forwarded to the park of the
    run on that spec. The store's lock is not re-entrant, so an operator action
    taken from inside a longer locked operation would otherwise be rejected by
    itself, and the park silently skipped for the one spec the caller was already
    working on.
    """
    resolved_switch = switch if switch is not None else KillSwitch(state.root)
    before = resolved_switch.read()
    record = resolved_switch.engage(initiator=initiator, reason=reason)

    resolved_accounting = accounting if accounting is not None else RunAccounting(state)
    resolved_machine = (
        machine if machine is not None else RunMachine(state, config, project=project)
    )
    # Archived specs included: archival says a person stopped looking at the spec,
    # not that a run of it may keep spending.
    refs = {spec.spec_key: spec.ref for spec in state.list_specs(include_archived=True)}

    halted: list[HaltedRun] = []
    for run in stoppable_runs(state):
        ref = refs.get(run.spec_key)
        if ref is None:
            # Parking goes through the lifecycle, which needs the spec to lock. A
            # run whose spec row is gone cannot be parked; the flag still stops it.
            logger.warning(
                "run %s has no spec row, so only the kill switch flag stops it", run.run_id
            )
            continue
        guard = guard_for(
            run.run_id,
            ref,
            state=state,
            config=config,
            project=project,
            accounting=resolved_accounting,
            notifier=notifier,
            audit=audit,
            machine=resolved_machine,
            kill_switch=resolved_switch,
        )
        forwarded = lock if lock is not None and lock.ref == ref else None
        decision = guard.halt_for_kill_switch(reason=reason, lock=forwarded)
        halted.append(
            HaltedRun(
                run_id=run.run_id,
                ref=ref,
                consumed_credits=decision.consumed_credits,
                parked=guard.halted,
                message=decision.message,
            )
        )
    report = KillSwitchReport(
        state=record, halted=tuple(halted), already_engaged=before.engaged
    )
    logger.warning("%s", report.describe())
    return report


def release_kill_switch(
    *,
    state: StateStore,
    initiator: str = "",
    switch: KillSwitch | None = None,
    audit: AuditSink | None = None,
) -> bool:
    """Release the switch so unattended work may start again. Resumes nothing.

    *audit* records the release before it happens, which is the opposite order
    from the engage and deliberately so. The engage writes its flag first because
    the fail-safe direction there is *stopped*: a stop that could not be recorded
    must still stop. Releasing runs the other way -- the fail-safe direction is
    *stays stopped* -- so the trail is written first and a trail that cannot land
    raises with the switch still engaged. This mirrors the queue's refusal to
    release held feedback into a run machine that records to nowhere: a release
    with no trail is the one direction of this control that spends money, and it
    does not happen unattributably.

    The engine's audit log is per spec by construction, so the entry lands on
    every spec holding a run the stop parked -- the mirror of the engage, which
    also records per halted run's spec. A release while nothing is parked
    concerns no spec and writes no engine entry; the surface that took the
    request records that case (the dashboard route emits a SEL event naming the
    authenticated operator), because an engine-wide event has no per-spec log to
    live in.

    The residual: an audit entry followed by a release that then fails to unlink
    the flag raises to the caller, so the operator is told the release failed and
    the recorded entry reads as an attempt. That is the accepted trade against an
    unrecorded release, which nothing would tell anyone about.
    """
    resolved = switch if switch is not None else KillSwitch(state.root)
    record = resolved.read()
    # Nothing engaged is nothing released: recording a release of a switch that
    # was already off would put a spending event in the trail for a no-op.
    if audit is not None and record.engaged:
        _record_release(state, audit, record=record, initiator=initiator)
    return resolved.release(initiator=initiator)


def _record_release(
    state: StateStore,
    audit: AuditSink,
    *,
    record: KillSwitchState,
    initiator: str,
) -> None:
    """Append the release entry to every spec holding a run the stop parked."""
    parked = state.list_runs(states=[run_state.value for run_state in PARKED_STATES])
    if not parked:
        return
    runs_by_spec: dict[str, list[str]] = {}
    for run in parked:
        runs_by_spec.setdefault(run.spec_key, []).append(run.run_id)
    refs = {spec.spec_key: spec.ref for spec in state.list_specs(include_archived=True)}
    for spec_key, run_ids in runs_by_spec.items():
        ref = refs.get(spec_key)
        if ref is None:  # pragma: no cover - a run row without its spec row
            continue
        audit.append(
            ref,
            AUDIT_EVENT_RELEASED,
            initiator=initiator or None,
            detail={
                # Who stopped it and why, carried onto the release: the two halves
                # of the decision are read together or not at all.
                "engaged_by": record.initiator,
                "engaged_ts": record.engaged_ts,
                "engaged_reason": record.reason,
                "unreadable": record.unreadable,
                # Named because the release does NOT resume them.
                "parked_runs": sorted(run_ids),
            },
        )
