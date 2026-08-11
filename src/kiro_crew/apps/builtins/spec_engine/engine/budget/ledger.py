"""Run attribution: which sessions belong to a run, and what they spent.

A ceiling is only as good as the arithmetic under it, and the arithmetic here has
one rule: **a run's spend is the sum over every session the run created**, not the
one session an operator happens to be looking at. A run authors documents in one
session, orchestrates in another, and fans out to a subagent session per task, so
counting the authoring session alone would report a fraction of the real spend and
a ceiling built on it would never fire.

Two things make that sum possible:

* **The stamp.** Every session the engine creates for a run is recorded against
  the run identifier in the state store's claim ledger. The claim's primary key
  is the session key, so a session belongs to at most one run — a session counted
  under two runs would inflate both totals and is refused at the point of
  stamping rather than discovered in a reconciliation later.
* **The metering ledger.** The host gateway already writes one record per turn
  under ``<data home>/usage/tokens/<date>.jsonl``, keyed by the session that ran
  the turn. This module reads that store; it does not keep a parallel count of
  turns. A second accounting path would be a second answer to "what did this run
  cost", and the two would disagree the first time a turn was recorded through
  only one of them.

The metering ledger sees turns that ran inside host sessions. A capability served
by an external provider — a coding agent behind the ``command`` transport, an MCP
child — spends outside any host session, so its cost reaches the run through
:class:`RunCostSink`, the sink the capability registry attributes declared cost
to. :meth:`RunAccounting.spend` adds the two, and keeps them separately labelled
so an operator can see which side an unexpected total came from.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

from kiro_crew.config.paths import data_home

from ..state import StateStore

logger = logging.getLogger(__name__)

#: Path of the host's per-turn metering ledger, relative to the data home. The
#: gateway writes it; the engine only reads it.
LEDGER_SUBPATH: tuple[str, ...] = ("usage", "tokens")

#: Suffix of one daily shard. Shard names are local dates (``YYYY-MM-DD``), which
#: is what makes a date-bounded scan possible without opening every file.
SHARD_SUFFIX = ".jsonl"

#: Shard fields this reader depends on. ``slot`` is the session a turn ran in and
#: ``credits`` is what kiro-cli billed for it.
FIELD_SLOT = "slot"
FIELD_CREDITS = "credits"
FIELD_TURNS = "turns"

#: Claim coordinates for a session stamp. The subject is the session key and the
#: scope is a fixed literal, so the ledger's primary key spans the session key
#: alone: one session, one owning run, enforced by the database rather than by
#: whoever remembers to check.
SESSION_CLAIM_KIND = "session"
SESSION_CLAIM_SCOPE = "attribution"

#: Run-detail keys holding spend that never appears in the host metering ledger,
#: because it was spent inside an external provider's own process.
DECLARED_CREDITS_KEY = "declared_credits"
DECLARED_CALLS_KEY = "declared_calls"


class SessionAttributionConflict(Exception):
    """A session already belongs to a different run.

    Raised rather than silently re-stamping: the second run would have the
    session's turns added to its total while the first run kept counting them
    too, so the same credits would be spent twice against two ceilings.
    """

    def __init__(self, session_key: str, owner: str, claimant: str) -> None:
        super().__init__(
            f"session {session_key!r} is already attributed to run {owner!r}; "
            f"run {claimant!r} cannot claim it"
        )
        self.session_key = session_key
        self.owner = owner
        self.claimant = claimant


def ledger_dir() -> Path:
    """The host's per-turn metering ledger directory.

    Resolved per call through :func:`data_home`, never captured at import, so a
    ``KIROCREW_HOME`` override set after this module loaded is still honoured.
    """
    return data_home().joinpath(*LEDGER_SUBPATH)


def normalize_session_key(key: str) -> str:
    """Return *key* in the form the metering ledger files a session under.

    A dashboard conversation runs as ``dashboard:chat-7-1785905004`` but records
    its turns under the bare ``chat-7-1785905004``, so a direct string match
    against a stamped session key finds nothing. The host owns that identity rule
    (``spend_key_for_slot``); this delegates to it rather than re-deriving the
    prefix, because two owners of an identity rule is exactly how a spend join
    silently starts returning zero.
    """
    # Imported lazily: the host module pulls in the dashboard's aiohttp handlers,
    # and the engine library is importable without them.
    from kiro_crew.dashboard.handlers.usage import spend_key_for_slot

    return spend_key_for_slot(key)


@dataclass(frozen=True)
class LedgerTotal:
    """What the metering ledger holds for one set of sessions."""

    credits: float
    turns: int
    #: Sessions that contributed at least one record. A stamped session with no
    #: record is absent here, which is how "stamped but never ran" stays visible.
    sessions: tuple[str, ...] = ()
    #: Records that were summed. Zero credits over many records is a real answer
    #: (a provider that bills nothing); zero records means nothing was measured.
    records: int = 0


class MeteringLedger:
    """Reads the host's per-turn metering ledger.

    Deliberately uncached. The dashboard's aggregate of the same store is cached
    for a minute because it feeds a poll; this feeds a dispatch decision, and a
    ceiling that reads a minute-old total authorizes a minute of turns it has
    already paid for.
    """

    def __init__(self, directory: Path | None = None) -> None:
        self._directory = Path(directory) if directory is not None else None

    @property
    def directory(self) -> Path:
        """The shard directory, resolved against the live data home when unset."""
        return self._directory if self._directory is not None else ledger_dir()

    def shards(self, *, since: date | None = None) -> list[Path]:
        """Daily shards on or after *since*, oldest first.

        Filtering by the shard's own date rather than by file modification time:
        the name is the day the turns happened, and a shard rewritten by an
        editor or restored from a backup would otherwise be judged by when the
        copy was made.
        """
        directory = self.directory
        if not directory.is_dir():
            return []
        selected: list[tuple[date, Path]] = []
        for path in directory.iterdir():
            if not path.is_file() or path.suffix != SHARD_SUFFIX:
                continue
            try:
                shard_date = datetime.strptime(path.stem, "%Y-%m-%d").date()
            except ValueError:
                continue
            if since is not None and shard_date < since:
                continue
            selected.append((shard_date, path))
        return [path for _, path in sorted(selected)]

    def total_for(
        self,
        session_keys: Iterable[str],
        *,
        since: date | None = None,
    ) -> LedgerTotal:
        """Sum every record belonging to *session_keys*.

        An empty session set totals zero without reading a file: a run with no
        stamped session has spent nothing through the host, and scanning the
        whole ledger to prove it would be the most expensive way to learn that.
        """
        wanted = {normalize_session_key(key) for key in session_keys if key}
        if not wanted:
            return LedgerTotal(credits=0.0, turns=0)
        credits = 0.0
        turns = 0
        records = 0
        seen: set[str] = set()
        for path in self.shards(since=since):
            for row in _rows_in(path):
                slot = row.get(FIELD_SLOT)
                if not isinstance(slot, str) or not slot:
                    continue
                key = normalize_session_key(slot)
                if key not in wanted:
                    continue
                credits += _credits_of(row, path)
                turns += _int_of(row.get(FIELD_TURNS))
                records += 1
                seen.add(key)
        return LedgerTotal(
            credits=credits,
            turns=turns,
            sessions=tuple(sorted(seen)),
            records=records,
        )


def _rows_in(path: Path) -> Iterable[dict[str, Any]]:
    """Yield each JSON object in a shard, skipping what cannot be read.

    A shard is appended to while it is read, so a torn final line is ordinary
    rather than corruption; skipping it loses one turn from the total until the
    write lands, whereas raising would take the whole enforcement path down.
    """
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if isinstance(row, dict):
                    yield row
    except OSError as exc:
        logger.warning("cannot read metering shard %s: %s", path, exc)


def _credits_of(row: dict[str, Any], path: Path) -> float:
    """The credit amount on one record, ``0.0`` when it is not a usable number.

    A non-finite value is dropped and named. The host sanitizes these on write,
    so one arriving here means something else produced the row — and a NaN added
    into a running total makes every later comparison against the ceiling false,
    which reads as an unbounded run rather than as a bad record.
    """
    value = row.get(FIELD_CREDITS, 0.0)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    amount = float(value)
    if not math.isfinite(amount):
        logger.warning("dropping a non-finite credit value from metering shard %s", path)
        return 0.0
    return amount


def _int_of(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    if isinstance(value, float) and not math.isfinite(value):
        return 0
    return int(value)


class RunSessions:
    """The run stamp on every session the engine creates for a run.

    Stamping is idempotent for its owner and refused for anyone else, so a
    resumed run re-stamping its sessions is a no-op while a second run reaching
    for the same session fails loudly.
    """

    def __init__(self, state: StateStore) -> None:
        self._state = state

    def stamp(self, run_id: str, session_key: str) -> bool:
        """Attribute *session_key* to *run_id*. True when this call created it.

        Raises :class:`SessionAttributionConflict` when another run owns the
        session.
        """
        if not run_id:
            raise ValueError("a session stamp needs a run identifier")
        if not session_key:
            raise ValueError("a session stamp needs a session key")
        if self._state.claim(SESSION_CLAIM_KIND, SESSION_CLAIM_SCOPE, session_key, run_id=run_id):
            return True
        owner = self.run_for(session_key)
        if owner == run_id:
            return False
        raise SessionAttributionConflict(session_key, owner or "", run_id)

    def run_for(self, session_key: str) -> str | None:
        """The run a session is attributed to, or ``None`` when unstamped."""
        record = self._state.get_claim(SESSION_CLAIM_KIND, SESSION_CLAIM_SCOPE, session_key)
        return record.run_id if record is not None else None

    def sessions_for(self, run_id: str) -> tuple[str, ...]:
        """Every session stamped with *run_id*, in claim order."""
        return tuple(
            record.subject
            for record in self._state.list_claims(
                kind=SESSION_CLAIM_KIND, scope=SESSION_CLAIM_SCOPE
            )
            if record.run_id == run_id
        )


class RunCostSink:
    """Where a capability provider's declared cost lands.

    Satisfies the capability registry's cost-sink seam. The registry reports what
    a provider said it spent and this records it against the run, because that
    spend happened in the provider's own process: no host session ran it, so no
    metering record exists and a ledger-only total would treat a delegated
    provider as free.

    The tally is persisted on the run row so it survives a restart. One writer per
    run is assumed — the run's own dispatcher — matching how a run's other state
    is written.
    """

    def __init__(self, state: StateStore) -> None:
        self._state = state

    def attribute(self, *, run: str, capability: str, provider: str, credits: float) -> None:
        if credits <= 0:
            return
        if not run:
            # Nothing to charge it to. Loud rather than dropped: unattributable
            # spend still left the account, and silence here reads as free work.
            logger.warning(
                "capability %s reported %.4f credits with no run to attribute them to",
                capability,
                credits,
            )
            return
        record = self._state.get_run(run)
        if record is None:
            logger.warning(
                "capability %s reported %.4f credits for unknown run %s",
                capability,
                credits,
                run,
            )
            return
        detail = record.detail
        total = _float_of(detail.get(DECLARED_CREDITS_KEY)) + float(credits)
        calls = _int_of(detail.get(DECLARED_CALLS_KEY)) + 1
        self._state.update_run(
            run,
            detail={DECLARED_CREDITS_KEY: total, DECLARED_CALLS_KEY: calls},
        )
        logger.debug(
            "attributed %.4f credits from %s (%s) to run %s", credits, capability, provider, run
        )

    def total_for(self, run: str) -> float:
        """Declared credits recorded against *run* so far."""
        record = self._state.get_run(run)
        if record is None:
            return 0.0
        return _float_of(record.detail.get(DECLARED_CREDITS_KEY))


def _float_of(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    amount = float(value)
    return amount if math.isfinite(amount) else 0.0


@dataclass(frozen=True)
class RunSpend:
    """One run's consumption, and where each part of it was measured."""

    run_id: str
    #: Credits from the host metering ledger, summed across every stamped session.
    metered_credits: float
    #: Credits an external capability provider declared, spent outside any host
    #: session.
    declared_credits: float
    turns: int
    #: Sessions stamped to the run, whether or not they recorded a turn.
    sessions: tuple[str, ...] = ()
    #: Sessions that recorded at least one turn.
    metered_sessions: tuple[str, ...] = ()
    records: int = 0

    @property
    def total_credits(self) -> float:
        """Everything the run consumed. This is the number a ceiling compares."""
        return self.metered_credits + self.declared_credits


class RunAccounting:
    """One run's spend, from every place spend is recorded.

    The façade the enforcement path talks to, so a caller asks a run what it has
    consumed rather than knowing that the answer comes from a shard directory plus
    a run-detail key.
    """

    def __init__(
        self,
        state: StateStore,
        *,
        ledger: MeteringLedger | None = None,
        sessions: RunSessions | None = None,
        cost_sink: RunCostSink | None = None,
    ) -> None:
        self._state = state
        self._ledger = ledger if ledger is not None else MeteringLedger()
        self._sessions = sessions if sessions is not None else RunSessions(state)
        self._cost_sink = cost_sink if cost_sink is not None else RunCostSink(state)

    @property
    def ledger(self) -> MeteringLedger:
        return self._ledger

    @property
    def cost_sink(self) -> RunCostSink:
        """The sink to hand the capability registry, so declared cost lands here."""
        return self._cost_sink

    def stamp(self, run_id: str, session_key: str) -> bool:
        """Attribute a session to a run. See :meth:`RunSessions.stamp`."""
        return self._sessions.stamp(run_id, session_key)

    def sessions_for(self, run_id: str) -> tuple[str, ...]:
        return self._sessions.sessions_for(run_id)

    def spend(self, run_id: str) -> RunSpend:
        """Sum *run_id*'s consumption across every session it created.

        The scan starts at the run's creation date, so an install with years of
        shards is not re-read to answer for a run that started today. The bound
        reaches back one day past the run's own date because shards are named for
        the local day while run timestamps are UTC, and a run created either side
        of local midnight would otherwise miss its first turns.
        """
        sessions = self._sessions.sessions_for(run_id)
        total = self._ledger.total_for(sessions, since=self._since(run_id))
        return RunSpend(
            run_id=run_id,
            metered_credits=total.credits,
            declared_credits=self._cost_sink.total_for(run_id),
            turns=total.turns,
            sessions=sessions,
            metered_sessions=total.sessions,
            records=total.records,
        )

    def _since(self, run_id: str) -> date | None:
        record = self._state.get_run(run_id)
        if record is None:
            return None
        try:
            created = datetime.fromisoformat(record.created_ts)
        except ValueError:
            return None
        return date.fromordinal(created.date().toordinal() - 1)
