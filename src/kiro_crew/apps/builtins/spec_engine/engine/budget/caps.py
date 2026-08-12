"""Per-source spending caps: how much one watch source may start per period.

A ceiling bounds a run. A cap bounds a *source*, and the two answer different
questions that both need answering: a source that files fifty issues a day with a
generous per-run ceiling stays under every ceiling and still spends fifty times
what the operator expected. The cap is the limit on the aggregate.

Three decisions make the cap behave the way an operator expects.

**It is per source and per period, so it refuses selectively.** A source at its
cap stops dispatching; every other source keeps going, including one whose cap is
larger and not yet reached. A control that stopped everything the moment any one
source filled up would be a kill switch with a confusing name.

**It stops new dispatches, not turns in a run already running.** The cap decides
whether *new* work starts for a source; a run already under way is bounded by its
own ceiling. Halting mid-run on an aggregate would discard work that had already
been paid for, which is the outcome the ceiling's own design rejects.

**A refused item is not consumed.** The dispatch claim is not taken for a capped
source, so the item stays a dispatch candidate and is picked up on a later poll
once the period rolls. Claiming and then declining would burn the item's
generation and lose the work permanently — the claim ledger has no memory of
"claimed but never run".

The window is the last ``period_days`` days, and what counts inside it is the
spend of the runs the source *started* in that window, summed by the same
accounting the ceiling uses. Runs from an earlier period do not count against the
current one, which is what makes a cap a rate rather than a lifetime total.

The gate also answers for the kill switch, because both are answers to "may this
source dispatch right now" and a caller that had to ask twice would eventually
ask once.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Callable, Mapping

from ..config import ConfigStore
from ..state import StateStore
from .ceiling import format_credits
from .ledger import RunAccounting
from .switch import KillSwitch

logger = logging.getLogger(__name__)

#: Configuration section holding watch sources, and the per-source field holding
#: its cap. The config schema owns the vocabulary; these name the entries the cap
#: reads, so the literals stay out of the enforcement path.
SOURCES_SECTION = "sources"
SPEND_CAP_FIELD = "spend_cap"
CAP_CREDITS_KEY = "credits"
CAP_PERIOD_DAYS_KEY = "period_days"


@dataclass(frozen=True)
class SourceCap:
    """The cap configured for one watch source."""

    source: str
    credits: float
    period_days: int

    @property
    def bounded(self) -> bool:
        """Whether this is a limit at all.

        A cap of zero or a period of zero days is not a smaller limit, it is a
        malformed one: nothing could ever be under the first and no window exists
        for the second. The config schema refuses both; this is the second gate,
        because a document written before a schema rule existed still has to be
        read safely.
        """
        return self.credits > 0 and self.period_days > 0


@dataclass(frozen=True)
class SourceSpend:
    """What a source's runs consumed inside the current period."""

    source: str
    credits: float
    period_days: int
    #: Start of the window, inclusive, as an ISO-8601 UTC timestamp.
    window_start: str
    #: Runs counted, oldest first. A run outside the window is absent.
    runs: tuple[str, ...] = ()


class CapOutcome(Enum):
    """What the gate says about dispatching new work for a source."""

    #: Under its cap, or no cap configured. New work may be dispatched.
    ALLOWED = "allowed"
    #: The source has reached its cap for the current period.
    CAPPED = "capped"
    #: The kill switch is engaged. No source dispatches anything.
    PAUSED = "paused"


@dataclass(frozen=True)
class CapDecision:
    """The answer to "may this source dispatch new work", with the numbers behind it."""

    outcome: CapOutcome
    source: str
    #: The cap in force, ``None`` when the source has none configured.
    cap: SourceCap | None = None
    spend: SourceSpend | None = None
    message: str = ""

    @property
    def allowed(self) -> bool:
        return self.outcome is CapOutcome.ALLOWED

    @property
    def consumed_credits(self) -> float:
        return self.spend.credits if self.spend is not None else 0.0

    @property
    def remaining_credits(self) -> float:
        """Headroom left in the period, never negative, zero without a cap."""
        if self.cap is None or not self.cap.bounded:
            return 0.0
        return max(0.0, self.cap.credits - self.consumed_credits)


def resolve_source_cap(config: ConfigStore, source: str) -> SourceCap | None:
    """The cap configured for *source*, or ``None`` when it has none.

    Read straight from the document rather than through the settings registry: a
    cap is a property of one source entry, not a setting with a bundled default,
    and inventing a default cap would silently throttle every install that never
    asked for one.
    """
    entry = _source_entry(config, source)
    raw = entry.get(SPEND_CAP_FIELD) if entry is not None else None
    if not isinstance(raw, Mapping):
        return None
    credits = _number(raw.get(CAP_CREDITS_KEY))
    period_days = int(_number(raw.get(CAP_PERIOD_DAYS_KEY)))
    if credits <= 0 or period_days <= 0:
        logger.warning(
            "watch source %r has a spend cap of %s credits over %s day(s), which is not a "
            "limit; treating the source as uncapped",
            source,
            credits,
            period_days,
        )
        return None
    return SourceCap(source=source, credits=credits, period_days=period_days)


class SourceCaps:
    """The dispatch gate: whether a source may start new work right now.

    Reads spend through the same :class:`~.ledger.RunAccounting` the ceiling uses,
    so a cap and a ceiling can never disagree about what a run cost. The clock is
    injected because a period is a window over wall time, and a test that could
    not move the window would have to sleep through one.
    """

    def __init__(
        self,
        state: StateStore,
        config: ConfigStore,
        *,
        accounting: RunAccounting | None = None,
        kill_switch: KillSwitch | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._state = state
        self._config = config
        self._accounting = accounting if accounting is not None else RunAccounting(state)
        # Defaulted from the state store's root so the gate and the run state it
        # reads live in the same place.
        self._kill_switch = kill_switch if kill_switch is not None else KillSwitch(state.root)
        self._clock: Callable[[], datetime] = clock if clock is not None else _utc_now

    @property
    def kill_switch(self) -> KillSwitch:
        return self._kill_switch

    def cap_for(self, source: str) -> SourceCap | None:
        """The cap in force for *source*, or ``None`` when it has none."""
        return resolve_source_cap(self._config, source)

    def spend_for(self, source: str, *, period_days: int) -> SourceSpend:
        """What *source*'s runs consumed in the last *period_days* days.

        Counts the runs the source started inside the window, each summed across
        every session it created. A run from an earlier period is excluded, which
        is what makes the cap a rate: without the window every source would
        eventually be permanently capped by its own history.
        """
        window_start = self._clock() - timedelta(days=max(0, period_days))
        credits = 0.0
        counted: list[str] = []
        for record in self._state.list_runs():
            if (record.source or "") != source:
                continue
            started = _parse_ts(record.created_ts)
            if started is None or started < window_start:
                continue
            credits += self._accounting.spend(record.run_id).total_credits
            counted.append(record.run_id)
        return SourceSpend(
            source=source,
            credits=credits,
            period_days=period_days,
            window_start=window_start.replace(microsecond=0).isoformat(),
            runs=tuple(counted),
        )

    def authorize_dispatch(self, source: str) -> CapDecision:
        """Decide whether *source* may have new work dispatched for it.

        The kill switch is checked first and without reading any spend: it stops
        every source regardless of what any of them has consumed, so summing a
        ledger to reach that answer would be work whose result cannot change it.
        """
        if self._kill_switch.engaged:
            return CapDecision(
                outcome=CapOutcome.PAUSED,
                source=source,
                message=(
                    f"watch source {source!r} will not dispatch: the kill switch is engaged"
                ),
            )
        cap = self.cap_for(source)
        if cap is None:
            return CapDecision(outcome=CapOutcome.ALLOWED, source=source)
        spend = self.spend_for(source, period_days=cap.period_days)
        if spend.credits >= cap.credits:
            message = (
                f"watch source {source!r} has consumed {format_credits(spend.credits)} "
                f"against its cap of {format_credits(cap.credits)} credits per "
                f"{cap.period_days} day(s); no new run will be dispatched for it until the "
                "period rolls"
            )
            logger.warning("%s", message)
            return CapDecision(
                outcome=CapOutcome.CAPPED,
                source=source,
                cap=cap,
                spend=spend,
                message=message,
            )
        return CapDecision(outcome=CapOutcome.ALLOWED, source=source, cap=cap, spend=spend)

    def dispatch_allowed(self, source: str) -> bool:
        """Whether *source* may dispatch new work. The dispatch path's own question.

        The boolean the watch lifecycle's gate seam calls. A caller that needs the
        numbers or the reason asks :meth:`authorize_dispatch` instead; a caller
        deciding whether to claim an item needs neither.
        """
        return self.authorize_dispatch(source).allowed


def caps_for(
    state: StateStore,
    config: ConfigStore,
    *,
    accounting: RunAccounting | None = None,
    kill_switch: KillSwitch | None = None,
) -> SourceCaps:
    """Build the dispatch gate a watcher hands to the claim path."""
    return SourceCaps(state, config, accounting=accounting, kill_switch=kill_switch)


def _source_entry(config: ConfigStore, source: str) -> Mapping[str, Any] | None:
    sources = config.document().get(SOURCES_SECTION)
    if not isinstance(sources, Mapping):
        return None
    entry = sources.get(source)
    return entry if isinstance(entry, Mapping) else None


def _number(value: Any) -> float:
    """A configured number, ``0.0`` for anything that is not usable as one."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    amount = float(value)
    return amount if amount == amount and abs(amount) != float("inf") else 0.0


def _parse_ts(value: str) -> datetime | None:
    """Parse a persisted timestamp, assuming UTC when it carries no zone."""
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)
