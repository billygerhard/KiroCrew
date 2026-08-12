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
) -> bool:
    """Release the switch so unattended work may start again. Resumes nothing."""
    resolved = switch if switch is not None else KillSwitch(state.root)
    return resolved.release(initiator=initiator)
