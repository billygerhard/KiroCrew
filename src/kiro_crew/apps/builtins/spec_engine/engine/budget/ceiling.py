"""The per-run ceiling: what a run may spend, and what happens when it is reached.

The ceiling is a hard stop with three properties that each exist because its
absence is how autonomous spend runs away:

* **It is bounded by default.** The ceiling resolves from the setting registry,
  whose bundled default is finite, so an install that configures nothing still
  runs under a limit. A headless run — nobody watching, nobody to notice — is
  refused outright when no ceiling is in force, rather than started in the hope
  that someone stops it.
* **It stops dispatch, not turns in flight.** Reaching the ceiling halts the
  *next* dispatch. Turns already sent are allowed to settle: killing a turn
  mid-flight loses the work and still pays for the tokens, so the stop is placed
  where it saves money instead of where it merely feels immediate.
* **It says the amount.** "Budget exceeded" tells an operator nothing they can
  act on. Every halt and warning carries the consumed amount and the ceiling it
  was measured against, so the next decision (raise it, or find out what the run
  is doing) can be made from the notification alone.

This ceiling is independent of any per-source spending cap. A cap answers "has
this watch source spent too much this period" and stops new *runs*; the ceiling
answers "has this run spent too much" and stops *this* run. A run therefore halts
on its own ceiling even when no cap was configured and no cap was reached.

Warnings are the one soft edge: a run crossing the warning threshold notifies and
keeps going. While a run is halted it sends no warnings at all — an operator who
has already been told the run stopped does not need to hear that it is
approaching the number it stopped at.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol

from ..config import ConfigStore, ValueOrigin
from ..runs import IllegalTransition, RunMachine, RunState, run_state_of
from ..state import SpecLock, SpecLocked, SpecRef, StateStore
from .ledger import MeteringLedger, RunAccounting, RunSpend
from .switch import KillSwitch

logger = logging.getLogger(__name__)

#: Settings this module reads. The registry owns the numbers; naming the keys here
#: keeps the literals out of the enforcement path.
CEILING_SETTING = "budget.run_ceiling_credits"
WARN_FRACTION_SETTING = "budget.warn_fraction"

#: Channel setting a notification routes to when the caller names none.
CHANNEL_SETTING = "notify.channel"

#: The state a halted run holds. The run lifecycle owns the vocabulary and the
#: transition table; this is an alias so a reader of the budget path does not have
#: to know which module spells the state, and so there is still only one spelling.
RUN_STATE_HALTED_BUDGET = RunState.HALTED_BUDGET

#: Initiator recorded against a halt: the ceiling acted, not a person.
HALT_INITIATOR = "budget"

#: Initiator recorded against a halt the kill switch caused. A person threw that
#: switch, and a run parked by it is distinguishable in the audit log from one the
#: arithmetic stopped.
KILL_SWITCH_INITIATOR = "kill-switch"

#: Run-detail keys this module writes when it parks a run, namespaced because the
#: detail object is shared with every other writer of the run row.
DETAIL_CONSUMED_CREDITS = "budget_consumed_credits"
DETAIL_CEILING_CREDITS = "budget_ceiling_credits"

#: Audit events this module records.
AUDIT_EVENT_HALTED = "budget.halted"
AUDIT_EVENT_WARNING = "budget.warning"
AUDIT_EVENT_REFUSED = "budget.refused"
AUDIT_EVENT_STOPPED = "budget.kill_switch"
#: Marks a halt an operator caused rather than a ceiling. Carried on the run row's
#: detail as well as the audit record, because the run state is shared with a
#: ceiling halt and a reader of the row alone could not otherwise tell them apart.
DETAIL_KILL_SWITCH = "kill_switch"
AUDIT_EVENT_COMPLETED = "budget.completed"

#: Claim coordinates for a one-shot notification. Scope is the run and subject the
#: notification kind, so a resumed run that re-reads a total already past the
#: ceiling does not re-notify: the ledger, not process memory, is what makes the
#: "notify once" hold across a restart.
NOTIFY_CLAIM_KIND = "notify"
NOTIFY_HALTED = "budget_halted"
NOTIFY_WARNING = "budget_warning"
NOTIFY_UNBOUNDED = "budget_unbounded"
NOTIFY_STOPPED = "budget_kill_switch"
NOTIFY_COMPLETED = "budget_completed"


class BudgetHalted(Exception):
    """A turn was dispatched for a run whose ceiling has already been reached.

    The backstop behind :meth:`BudgetGuard.authorize_dispatch`. A caller is meant
    to ask before dispatching, so reaching this means a dispatch path skipped the
    check — which is a bug worth raising on rather than a spend worth allowing.
    """


def format_credits(value: float) -> str:
    """Format a credit amount for an operator-facing message."""
    return f"{value:.2f}"


@dataclass(frozen=True)
class Budget:
    """The limit in force for one run, and where the number came from."""

    ceiling_credits: float
    warn_fraction: float = 0.0
    ceiling_origin: ValueOrigin = ValueOrigin.BUNDLED_DEFAULT
    #: Dotted configuration path the ceiling was declared at, empty for the
    #: bundled default.
    declared_at: str = ""

    @property
    def bounded(self) -> bool:
        """Whether a finite, positive ceiling is in force.

        A ceiling of zero or one that is not finite is not a smaller or larger
        limit, it is the absence of one: nothing can ever be under it, or nothing
        can ever exceed it.
        """
        return math.isfinite(self.ceiling_credits) and self.ceiling_credits > 0

    @property
    def warn_at(self) -> float | None:
        """Consumption at which a warning fires, ``None`` when none is configured.

        A fraction of the ceiling rather than an absolute amount, so raising the
        ceiling moves the warning with it instead of leaving it where it would
        fire immediately.
        """
        if not self.bounded or self.warn_fraction <= 0:
            return None
        return self.ceiling_credits * self.warn_fraction


def resolve_budget(store: ConfigStore, *, project: str | None = None) -> Budget:
    """Resolve the ceiling and warning threshold in force for *project*."""
    ceiling = store.effective(CEILING_SETTING, project=project)
    warn = store.effective(WARN_FRACTION_SETTING, project=project)
    return Budget(
        ceiling_credits=float(ceiling.value),
        warn_fraction=float(warn.value),
        ceiling_origin=ceiling.origin,
        declared_at=ceiling.declared_at,
    )


class Notifier(Protocol):
    """Where a budget notification is delivered.

    Narrow on purpose: this module decides *that* an operator must be told and
    what the message says, never how a channel is reached.
    """

    def notify(self, *, channel: str, message: str, detail: dict[str, Any]) -> None: ...


@dataclass
class RecordingNotifier:
    """Collects notifications instead of delivering them. The default.

    A budget that dropped its notification silently would halt a run nobody hears
    about, so the absence of a wired channel still leaves a record.
    """

    sent: list[dict[str, Any]] = field(default_factory=list)

    def notify(self, *, channel: str, message: str, detail: dict[str, Any]) -> None:
        self.sent.append({"channel": channel, "message": message, "detail": detail})

    def messages(self) -> tuple[str, ...]:
        return tuple(str(entry["message"]) for entry in self.sent)


class AuditSink(Protocol):
    """Where a budget decision is recorded. ``AuditLog`` satisfies it."""

    def append(
        self,
        ref: SpecRef,
        event: str,
        *,
        run: str | None = None,
        initiator: str | None = None,
        detail: dict[str, Any] | None = None,
        cost: float | None = None,
    ) -> Any: ...


class DispatchOutcome(Enum):
    """What the budget says about the next dispatch."""

    #: Under the ceiling; dispatch may proceed.
    ALLOWED = "allowed"
    #: The ceiling has been reached. No further dispatch; in-flight turns settle.
    HALTED = "halted"
    #: A headless run with no ceiling in force. It never starts.
    UNBOUNDED = "unbounded"
    #: The kill switch is engaged. Nothing new dispatches for any run.
    STOPPED = "stopped"


@dataclass(frozen=True)
class DispatchDecision:
    """The answer to "may this run dispatch more work", with the numbers behind it."""

    outcome: DispatchOutcome
    spend: RunSpend
    ceiling_credits: float
    message: str = ""
    #: Whether this decision crossed the warning threshold for the first time.
    warned: bool = False
    #: Turns already dispatched and not yet settled at the moment of the decision.
    in_flight: int = 0

    @property
    def allowed(self) -> bool:
        return self.outcome is DispatchOutcome.ALLOWED

    @property
    def consumed_credits(self) -> float:
        return self.spend.total_credits

    @property
    def remaining_credits(self) -> float:
        """Headroom left, never negative. Zero once the ceiling is reached."""
        return max(0.0, self.ceiling_credits - self.spend.total_credits)

    @property
    def draining(self) -> bool:
        """Halted with turns still settling: dispatch has stopped, spend has not."""
        return self.outcome in (DispatchOutcome.HALTED, DispatchOutcome.STOPPED) and (
            self.in_flight > 0
        )


@dataclass(frozen=True)
class CompletionReport:
    """What a finished run consumed, as reported to an operator and the audit log.

    Not a :class:`DispatchDecision`: a completed run is not asking whether it may
    dispatch, and answering that question here would invite a caller to treat the
    report as an authorization.
    """

    run_id: str
    spend: RunSpend
    #: The state the run ended in, ``None`` when its row is gone.
    final_state: RunState | None
    message: str
    #: Whether this call was the one that sent the notification. False for a
    #: second caller: the claim makes the message once per run.
    notified: bool = False

    @property
    def consumed_credits(self) -> float:
        """The run's total consumption, across every session it created."""
        return self.spend.total_credits


class BudgetGuard:
    """Enforces one run's spend controls: its ceiling, and the global stop.

    Per run rather than per engine, because the ceiling, the spec it audits
    against, and the halt state are all properties of a single run. The
    accounting it reads is shared, so several guards see the same totals, and the
    kill switch it reads is global, so one operator action reaches every guard —
    including one built for a run that did not exist when the switch was thrown.

    The halt itself is applied through the run lifecycle machine rather than
    written here. That machine owns the transition table, the resume point a
    parked run comes back to, and the spec lock — a second writer of the state
    column would produce a halted run that could not be resumed.
    """

    def __init__(
        self,
        run_id: str,
        ref: SpecRef,
        budget: Budget,
        *,
        state: StateStore,
        machine: RunMachine,
        accounting: RunAccounting | None = None,
        notifier: Notifier | None = None,
        audit: AuditSink | None = None,
        channel: str = "",
        headless: bool = False,
        kill_switch: KillSwitch | None = None,
    ) -> None:
        if state.get_run(run_id) is None:
            raise KeyError(f"unknown run: {run_id!r}")
        self._run_id = run_id
        self._ref = ref
        self._budget = budget
        self._state = state
        self._machine = machine
        self._accounting = accounting if accounting is not None else RunAccounting(state)
        self._notifier: Notifier = notifier if notifier is not None else RecordingNotifier()
        self._audit = audit
        self._channel = channel
        self._headless = headless
        # Defaulted from the state store's own root, so a guard's switch and its
        # run state always live in the same place: a switch resolved from
        # elsewhere would be a stop this run could not see.
        self._kill_switch = kill_switch if kill_switch is not None else KillSwitch(state.root)
        self._in_flight = 0

    @property
    def run_id(self) -> str:
        return self._run_id

    @property
    def budget(self) -> Budget:
        return self._budget

    @property
    def headless(self) -> bool:
        return self._headless

    @property
    def in_flight(self) -> int:
        """Turns dispatched and not yet settled."""
        return self._in_flight

    @property
    def halted(self) -> bool:
        """Whether the run is halted for budget, read from persisted state."""
        record = self._state.get_run(self._run_id)
        return record is not None and run_state_of(record) is RunState.HALTED_BUDGET

    @property
    def stopped(self) -> bool:
        """Whether the kill switch is engaged. Read per call, never cached.

        Cached, this would answer for the moment the guard was built, and a guard
        outlives an operator's decision to stop everything.
        """
        return self._kill_switch.engaged

    @property
    def draining(self) -> bool:
        """Halted with turns still in flight. They finish; nothing new is sent."""
        return (self.halted or self.stopped) and self._in_flight > 0

    # --- attribution -------------------------------------------------------

    def stamp_session(self, session_key: str) -> bool:
        """Attribute a session this run created to the run.

        Every session, not just the authoring one: the orchestrator's session and
        each subagent's session spend against the same ceiling, and one left
        unstamped is spend the ceiling cannot see.
        """
        return self._accounting.stamp(self._run_id, session_key)

    @property
    def sessions(self) -> tuple[str, ...]:
        """Every session stamped to this run."""
        return self._accounting.sessions_for(self._run_id)

    def spend(self) -> RunSpend:
        """This run's consumption, summed fresh across every stamped session."""
        return self._accounting.spend(self._run_id)

    # --- dispatch control --------------------------------------------------

    def authorize_dispatch(self, *, lock: SpecLock | None = None) -> DispatchDecision:
        """Decide whether the run may dispatch more work, and act on the answer.

        Reaching the ceiling is not an observation this returns for a caller to
        act on later: the halt is performed here — state parked, operator notified
        with the amount, audit written — because a decision that only reported the
        number would leave the run running whenever a caller forgot to react to
        it.

        *lock* is the spec lock the caller already holds, if any. The halt is a
        state change on the run's spec, and the store's lock is not re-entrant, so
        a dispatcher deciding this from inside a longer locked operation passes
        its handle instead of being rejected by itself.
        """
        spend = self.spend()
        self._cache_cost(spend)
        if self.stopped:
            # Ahead of every other check, including the halted one: the operator's
            # stop is the answer they expect to read, and it is the only refusal
            # here that is about the whole engine rather than about this run.
            return self._stop(spend, lock)
        if self._headless and not self._budget.bounded:
            return self._refuse_unbounded(spend, lock)
        if self.halted:
            return DispatchDecision(
                outcome=DispatchOutcome.HALTED,
                spend=spend,
                ceiling_credits=self._budget.ceiling_credits,
                message=self._halt_message(spend),
                in_flight=self._in_flight,
            )
        if self._budget.bounded and spend.total_credits >= self._budget.ceiling_credits:
            return self._halt(spend, lock)
        return self._allow(spend)

    def open_turn(self) -> None:
        """Record a turn as dispatched.

        Refuses once the kill switch is engaged or the run is halted. The check in
        :meth:`authorize_dispatch` is where a well-behaved caller stops; this is
        what makes the stop hold for one that does not.

        The kill switch and unboundedness are both refused on the facts rather
        than on the parked state. Each is knowable without reading the run row —
        an operator has stopped the engine, or this run is unattended with no
        finite ceiling — whereas the halted flag only becomes true after a park
        that persisted. Depending on the park meant any failure to record it left
        an unattended run with nothing to stop it, which is the single outcome
        this class exists to prevent, so the cheaper test is also the correct one.
        """
        if self.stopped:
            raise BudgetHalted(
                f"run {self._run_id} may not dispatch a turn: the kill switch is engaged"
            )
        if self._headless and not self._budget.bounded:
            raise BudgetHalted(
                f"run {self._run_id} may not dispatch a turn: it is unattended and no "
                "budget ceiling is in force"
            )
        if self.halted:
            raise BudgetHalted(
                f"run {self._run_id} is halted for budget; no further turns may be dispatched"
            )
        self._in_flight += 1

    def settle_turn(self) -> None:
        """Record a dispatched turn as finished.

        Allowed while halted or stopped, and that is the point: the turns that
        were already in flight when the ceiling was reached, or when the kill
        switch was thrown, are the ones the stop deliberately lets finish.
        """
        if self._in_flight > 0:
            self._in_flight -= 1

    # --- the global stop ---------------------------------------------------

    def halt_for_kill_switch(
        self, *, reason: str = "", lock: SpecLock | None = None
    ) -> DispatchDecision:
        """Park this run because the kill switch was thrown, and say what it cost.

        Called by the engine-wide stop for each run it walks. Parking is what makes
        a surface able to explain why the work stopped; the flag is what actually
        stops it, so a park this cannot apply — a terminal run, a spec another
        writer holds — leaves the run stopped all the same.

        *lock* is forwarded to the park for the same reason the ceiling forwards
        it: the store's lock is not re-entrant, and an operator action taken from
        inside a locked operation must not be rejected by its own caller.
        """
        spend = self.spend()
        self._cache_cost(spend)
        return self._stop(spend, lock, reason=reason)

    def _stop(
        self, spend: RunSpend, lock: SpecLock | None = None, *, reason: str = ""
    ) -> DispatchDecision:
        message = self._stop_message(spend, reason)
        detail = self._detail(spend)
        detail[DETAIL_KILL_SWITCH] = True
        if reason:
            detail["kill_switch_reason"] = reason
        parked = self.halted or self._park(
            message, spend, lock, initiator=KILL_SWITCH_INITIATOR
        )
        # The flag is the stop, so it is already durable here whether or not the
        # row moved. A park refused by another writer is expected and survivable,
        # and it is the case an operator most needs told: the run is stopped while
        # its state column still reads as running. Gating the notice on the park
        # would drop the report exactly there, so only the row's fate is
        # conditional and the record says which way it went.
        detail["parked"] = parked
        if self._state.claim(NOTIFY_CLAIM_KIND, self._run_id, NOTIFY_STOPPED):
            self._deliver(message, detail)
            self._record(AUDIT_EVENT_STOPPED, detail, cost=spend.total_credits)
        logger.warning("%s", message)
        return DispatchDecision(
            outcome=DispatchOutcome.STOPPED,
            spend=spend,
            ceiling_credits=self._budget.ceiling_credits,
            message=message,
            in_flight=self._in_flight,
        )

    # --- completion --------------------------------------------------------

    def report_completion(self) -> CompletionReport:
        """Notify and audit what the run consumed, now that it has stopped running.

        The counterpart to the halt: a run that finished normally cost money too,
        and an operator told the amount only when something went wrong cannot tell
        an expensive success from a cheap one. The amount is the run's total across
        every session it created, the same sum the ceiling compares.

        Claimed once per run, so a resumed run, a retried caller, or two surfaces
        both noticing the same completion send one message rather than three. The
        cached total is written before the message, so a channel that is
        unreachable loses the notification rather than the number.
        """
        spend = self.spend()
        self._cache_cost(spend)
        record = self._state.get_run(self._run_id)
        state = run_state_of(record) if record is not None else None
        message = self._completion_message(spend, state)
        detail = self._detail(spend)
        detail["final_state"] = state.value if state is not None else ""
        notified = self._state.claim(NOTIFY_CLAIM_KIND, self._run_id, NOTIFY_COMPLETED)
        if notified:
            self._deliver(message, detail)
            self._record(AUDIT_EVENT_COMPLETED, detail, cost=spend.total_credits)
        logger.info("%s", message)
        return CompletionReport(
            run_id=self._run_id,
            spend=spend,
            final_state=state,
            message=message,
            notified=notified,
        )

    # --- outcomes ----------------------------------------------------------

    def _allow(self, spend: RunSpend) -> DispatchDecision:
        warned = self._maybe_warn(spend)
        return DispatchDecision(
            outcome=DispatchOutcome.ALLOWED,
            spend=spend,
            ceiling_credits=self._budget.ceiling_credits,
            warned=warned,
            in_flight=self._in_flight,
        )

    def _halt(self, spend: RunSpend, lock: SpecLock | None = None) -> DispatchDecision:
        message = self._halt_message(spend)
        detail = self._detail(spend)
        parked = self._park(message, spend, lock)
        # Claimed before delivery, so a retried dispatch cannot notify twice; the
        # halt state itself is already persisted, so a claim held by a
        # notification that then failed loses one message rather than the stop.
        if parked and self._state.claim(NOTIFY_CLAIM_KIND, self._run_id, NOTIFY_HALTED):
            self._deliver(message, detail)
        if parked:
            self._record(AUDIT_EVENT_HALTED, detail, cost=spend.total_credits)
        logger.warning("%s", message)
        return DispatchDecision(
            outcome=DispatchOutcome.HALTED,
            spend=spend,
            ceiling_credits=self._budget.ceiling_credits,
            message=message,
            in_flight=self._in_flight,
        )

    def _park(
        self,
        reason: str,
        spend: RunSpend,
        lock: SpecLock | None,
        *,
        initiator: str = HALT_INITIATOR,
    ) -> bool:
        """Move the run into the halted state through the lifecycle machine.

        Returns whether the run is now parked. Two failures are survivable and
        neither changes the answer to "may this dispatch", which is already no:

        * A run the transition table cannot move — one that already finished,
          failed, or was cancelled — is logged rather than forced. Rewriting a
          terminal run's state would be the more damaging of the two outcomes.
        * A spec another writer holds right now is left to the next check. The
          decision has already refused the dispatch, so nothing is spent while
          the park waits.
        """
        try:
            self._machine.transition(
                self._ref,
                self._run_id,
                RunState.HALTED_BUDGET,
                initiator=initiator,
                reason=reason,
                detail={
                    DETAIL_CONSUMED_CREDITS: spend.total_credits,
                    DETAIL_CEILING_CREDITS: self._budget.ceiling_credits,
                    # The state is shared with a ceiling halt, deliberately: the
                    # run states are enumerated and none of them means "an
                    # operator stopped this". So the cause travels in the detail,
                    # or a surface reading the row shows "halted for budget"
                    # beside a total well under its ceiling and only the audit log
                    # says why.
                    **({DETAIL_KILL_SWITCH: True} if initiator == KILL_SWITCH_INITIATOR else {}),
                },
                lock=lock,
            )
        except (IllegalTransition, SpecLocked) as exc:
            logger.warning("run %s could not be parked: %s", self._run_id, exc)
            return False
        return True

    def _refuse_unbounded(self, spend: RunSpend, lock: SpecLock | None = None) -> DispatchDecision:
        message = (
            f"run {self._run_id} will not execute headless: no budget ceiling is in force, "
            "and an unattended run without a ceiling has nothing to stop it"
        )
        detail = self._detail(spend)
        # Parked rather than left queued: the run is real, it cannot proceed, and
        # parking it is what lets an operator who configures a ceiling resume the
        # work instead of finding a run that sat still with no state saying why.
        #
        # The caller's lock is forwarded for the same reason the halt path
        # forwards it. Dropping it here made the park conflict with the very
        # dispatcher that asked: the store's lock is not re-entrant, so the park
        # was rejected by its own caller, the rejection was swallowed, and the run
        # stayed queued -- the one state this refusal exists to replace.
        self._park(message, spend, lock)
        if self._state.claim(NOTIFY_CLAIM_KIND, self._run_id, NOTIFY_UNBOUNDED):
            self._deliver(message, detail)
            self._record(AUDIT_EVENT_REFUSED, detail)
        logger.warning("%s", message)
        return DispatchDecision(
            outcome=DispatchOutcome.UNBOUNDED,
            spend=spend,
            ceiling_credits=self._budget.ceiling_credits,
            message=message,
            in_flight=self._in_flight,
        )

    def _maybe_warn(self, spend: RunSpend) -> bool:
        """Notify once when consumption crosses the warning threshold.

        Returns whether this call sent the warning. Nothing about the run changes:
        a warning that halted anything would be a second ceiling.
        """
        threshold = self._budget.warn_at
        if threshold is None or spend.total_credits < threshold:
            return False
        if self.halted:
            # An operator already knows this run stopped at the number the
            # warning would be pointing at.
            return False
        if not self._state.claim(NOTIFY_CLAIM_KIND, self._run_id, NOTIFY_WARNING):
            return False
        message = (
            f"run {self._run_id} has consumed {format_credits(spend.total_credits)} of "
            f"{format_credits(self._budget.ceiling_credits)} credits and is still running"
        )
        detail = self._detail(spend)
        detail["warn_at_credits"] = threshold
        self._deliver(message, detail)
        self._record(AUDIT_EVENT_WARNING, detail, cost=spend.total_credits)
        return True

    def _halt_message(self, spend: RunSpend) -> str:
        return (
            f"run {self._run_id} halted for budget after consuming "
            f"{format_credits(spend.total_credits)} of "
            f"{format_credits(self._budget.ceiling_credits)} credits"
        )

    def _stop_message(self, spend: RunSpend, reason: str = "") -> str:
        """The kill-switch halt message.

        Carries the consumed amount and not the ceiling: the run did not reach its
        ceiling, and printing a limit it never hit beside a total it did would
        invite the reader to conclude the arithmetic stopped it.
        """
        because = f" ({reason})" if reason else ""
        return (
            f"run {self._run_id} halted by the kill switch{because} after consuming "
            f"{format_credits(spend.total_credits)} credits"
        )

    def _completion_message(self, spend: RunSpend, state: RunState | None) -> str:
        ended = state.value if state is not None else "gone"
        return (
            f"run {self._run_id} ended as {ended} after consuming "
            f"{format_credits(spend.total_credits)} credits"
        )

    def _detail(self, spend: RunSpend) -> dict[str, Any]:
        return {
            "run": self._run_id,
            "consumed_credits": spend.total_credits,
            "metered_credits": spend.metered_credits,
            "declared_credits": spend.declared_credits,
            "ceiling_credits": self._budget.ceiling_credits,
            "ceiling_origin": self._budget.ceiling_origin.value,
            "sessions": list(spend.sessions),
            "turns": spend.turns,
            "headless": self._headless,
            "in_flight_turns": self._in_flight,
        }

    def _deliver(self, message: str, detail: dict[str, Any]) -> None:
        """Deliver a notification without letting its failure unwind the run.

        Run state is primary and delivery is best-effort: a halt that rolled back
        because a channel was unreachable would keep spending.
        """
        try:
            self._notifier.notify(channel=self._channel, message=message, detail=dict(detail))
        except Exception:
            logger.exception("budget notification for run %s could not be delivered", self._run_id)

    def _record(self, event: str, detail: dict[str, Any], *, cost: float | None = None) -> None:
        if self._audit is None:
            return
        try:
            self._audit.append(self._ref, event, run=self._run_id, detail=dict(detail), cost=cost)
        except Exception:
            logger.exception("budget event %s for run %s could not be audited", event, self._run_id)

    def _cache_cost(self, spend: RunSpend) -> None:
        """Persist the computed total on the run row.

        The row's cost column is a cache of this sum, so a surface reading a run
        does not re-scan the metering ledger, and a notification quoting the
        amount and a dashboard reading the row cannot disagree.
        """
        try:
            self._state.update_run(self._run_id, cost_credits=spend.total_credits)
        except KeyError:
            logger.warning("run %s vanished while caching its cost", self._run_id)


def guard_for(
    run_id: str,
    ref: SpecRef,
    *,
    state: StateStore,
    config: ConfigStore,
    project: str | None = None,
    accounting: RunAccounting | None = None,
    notifier: Notifier | None = None,
    audit: AuditSink | None = None,
    headless: bool = False,
    ledger: MeteringLedger | None = None,
    machine: RunMachine | None = None,
    kill_switch: KillSwitch | None = None,
) -> BudgetGuard:
    """Build a guard with the ceiling and channel configuration puts in force."""
    if accounting is None:
        accounting = RunAccounting(state, ledger=ledger)
    if machine is None:
        machine = RunMachine(state, config, project=project)
    return BudgetGuard(
        run_id,
        ref,
        resolve_budget(config, project=project),
        state=state,
        machine=machine,
        accounting=accounting,
        notifier=notifier,
        audit=audit,
        channel=str(config.effective(CHANNEL_SETTING, project=project).value),
        headless=headless,
        kill_switch=kill_switch,
    )
