"""Run attribution: the stamp, and summing the metering ledger across sessions.

The claim these tests exist for is that a run's cost is the sum over *every*
session it created. A ceiling that counts the authoring session alone is not a
ceiling, so the multi-session sum is asserted directly against a seeded ledger
rather than inferred from a total that happens to look right.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.apps.builtins.spec_engine.engine.budget import (
    DECLARED_CREDITS_KEY,
    MeteringLedger,
    RunAccounting,
    RunCostSink,
    RunSessions,
    SessionAttributionConflict,
    ledger_dir,
    normalize_session_key,
)
from kiro_crew.apps.builtins.spec_engine.engine.runs import RunState
from kiro_crew.apps.builtins.spec_engine.engine.state import SpecRef, StateStore

#: A state work is dispatched from. These tests assert on cost, not lifecycle.
EXECUTING = RunState.EXECUTING.value


def seed_shard(directory: Path, day: date, rows: list[dict[str, Any]]) -> Path:
    """Write one daily metering shard in the host's format."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{day.isoformat()}.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")
    return path


def turn(slot: str, credits: float, *, turns: int = 1) -> dict[str, Any]:
    """One per-turn metering record, shaped like the ones the gateway writes."""
    return {
        "_type": "tokens",
        "ts": datetime.now(timezone.utc).isoformat(),
        "slot": slot,
        "provider": "acp",
        "model": "auto",
        "credits": credits,
        "turns": turns,
    }


def backdate_run(store: StateStore, run_id: str, started: datetime) -> None:
    """Move a run's creation timestamp back, to stand in for a multi-day run.

    Written against the database rather than through the store because there is no
    API for it: creation time is set once, by the run starting.
    """
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "UPDATE runs SET created_ts = ? WHERE run_id = ?",
            (started.replace(microsecond=0).isoformat(), run_id),
        )
        conn.commit()


@pytest.fixture()
def ledger_path(tmp_path: Path) -> Path:
    return tmp_path / "usage" / "tokens"


@pytest.fixture()
def run(store: StateStore, ref: SpecRef) -> str:
    store.create_run("run-1", ref, state=EXECUTING)
    return "run-1"


@pytest.fixture()
def accounting(store: StateStore, ledger_path: Path) -> RunAccounting:
    return RunAccounting(store, ledger=MeteringLedger(ledger_path))


class TestLedgerLocation:
    def test_the_ledger_is_the_hosts_per_turn_usage_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(home))
        assert ledger_dir() == home / "usage" / "tokens"
        # Resolved per call, so an override set later is still honoured.
        assert MeteringLedger().directory == ledger_dir()

    def test_an_absent_ledger_directory_reads_as_no_spend(self, tmp_path: Path) -> None:
        ledger = MeteringLedger(tmp_path / "never-created")
        assert ledger.shards() == []
        assert ledger.total_for(["chat-1-1"]).credits == 0.0


class TestSessionStamping:
    def test_a_session_is_stamped_once_and_re_stamping_is_idempotent(
        self, store: StateStore, run: str
    ) -> None:
        sessions = RunSessions(store)
        assert sessions.stamp(run, "spec-engine:authoring-1") is True
        assert sessions.stamp(run, "spec-engine:authoring-1") is False
        assert sessions.sessions_for(run) == ("spec-engine:authoring-1",)

    def test_every_session_kind_a_run_creates_is_stamped_to_it(
        self, store: StateStore, run: str
    ) -> None:
        sessions = RunSessions(store)
        for key in ("authoring-1", "orchestrator-1", "subagent-1", "subagent-2"):
            sessions.stamp(run, key)
        assert sessions.sessions_for(run) == (
            "authoring-1",
            "orchestrator-1",
            "subagent-1",
            "subagent-2",
        )
        assert sessions.run_for("subagent-2") == run

    def test_another_runs_session_cannot_be_claimed(
        self, store: StateStore, ref: SpecRef, run: str
    ) -> None:
        store.create_run("run-2", ref, state=EXECUTING)
        sessions = RunSessions(store)
        sessions.stamp(run, "shared-session")
        with pytest.raises(SessionAttributionConflict) as raised:
            sessions.stamp("run-2", "shared-session")
        assert raised.value.owner == run
        # The stamp is unchanged, so the credits stay charged to one run only.
        assert sessions.run_for("shared-session") == run
        assert sessions.sessions_for("run-2") == ()

    def test_an_empty_run_or_session_is_refused(self, store: StateStore, run: str) -> None:
        sessions = RunSessions(store)
        with pytest.raises(ValueError):
            sessions.stamp("", "session-1")
        with pytest.raises(ValueError):
            sessions.stamp(run, "")

    def test_a_dashboard_session_key_matches_the_bare_slot_the_ledger_files(self) -> None:
        # The dashboard runs `dashboard:chat-7-1785905004` and records
        # `chat-7-1785905004`; both must normalize to the same key or the join
        # silently finds nothing.
        assert normalize_session_key("chat-7-1785905004") == normalize_session_key(
            "dashboard:chat-7-1785905004"
        )
        assert normalize_session_key("subagent-1") == "subagent-1"


class TestSpendSumsEverySession:
    def test_spend_is_the_ledger_total_across_all_sessions_not_one_share(
        self, accounting: RunAccounting, ledger_path: Path, run: str
    ) -> None:
        today = date.today()
        seed_shard(
            ledger_path,
            today,
            [
                turn("authoring-1", 0.5),
                turn("orchestrator-1", 0.25),
                turn("subagent-1", 1.5, turns=2),
                turn("subagent-2", 0.75),
            ],
        )
        for key in ("authoring-1", "orchestrator-1", "subagent-1", "subagent-2"):
            accounting.stamp(run, key)

        spend = accounting.spend(run)

        ledger_total = 0.5 + 0.25 + 1.5 + 0.75
        assert spend.metered_credits == pytest.approx(ledger_total)
        assert spend.total_credits == pytest.approx(ledger_total)
        # The authoring session's share alone would be 0.5 — a quarter of the real
        # spend, and the number a single-session ceiling would enforce against.
        assert spend.metered_credits > 0.5
        assert spend.turns == 5
        assert spend.records == 4
        assert set(spend.metered_sessions) == set(spend.sessions)

    def test_a_dashboard_started_runs_own_session_is_counted(
        self, accounting: RunAccounting, ledger_path: Path, run: str
    ) -> None:
        """The join across the two spellings of one session, exercised end to end.

        The dashboard stamps ``dashboard:chat-N-TS`` and the metering rows are
        filed under the bare ``chat-N-TS``, so the sum only finds them because both
        sides are normalized. Asserting the normalizer's algebra does not exercise
        that: every other key in these tests normalizes to itself, so the join
        matches identically with the normalization deleted.

        This is the authoring session of every dashboard-started run, which is
        usually its largest, and an unmetered stamped session is legal -- so the
        miss reports nothing and the ceiling simply fires late or not at all.
        """
        slot = "chat-7-1785905004"
        seed_shard(ledger_path, date.today(), [turn(slot, 2.5, turns=3)])
        accounting.stamp(run, f"dashboard:{slot}")

        spend = accounting.spend(run)

        assert spend.metered_credits == pytest.approx(2.5)
        assert spend.turns == 3

    def test_a_turn_in_yesterdays_shard_still_counts(
        self, store: StateStore, accounting: RunAccounting, ledger_path: Path, run: str
    ) -> None:
        """The reason the scan reaches one day further back than the run's date.

        Shards are named for the local day and run timestamps are UTC, so a run
        created just after UTC midnight in a western offset writes its first turns
        into what is still yesterday locally. Without the grace those credits are
        below the scan bound and vanish from the sum, and the ceiling then
        authorizes spend the run has already made.

        Every other test seeds shards on or after the run's own date, where the
        grace is not load-bearing -- so dropping it changes nothing they can see.
        """
        started = datetime.now(timezone.utc)
        backdate_run(store, run, started)
        yesterday = date.fromordinal(started.date().toordinal() - 1)
        seed_shard(ledger_path, yesterday, [turn("authoring-1", 1.25)])
        accounting.stamp(run, "authoring-1")

        spend = accounting.spend(run)

        assert spend.metered_credits == pytest.approx(1.25)

    def test_another_runs_turns_are_not_counted(
        self,
        accounting: RunAccounting,
        ledger_path: Path,
        store: StateStore,
        ref: SpecRef,
        run: str,
    ) -> None:
        store.create_run("run-2", ref, state=EXECUTING)
        seed_shard(ledger_path, date.today(), [turn("mine", 1.0), turn("theirs", 9.0)])
        accounting.stamp(run, "mine")
        accounting.stamp("run-2", "theirs")
        assert accounting.spend(run).total_credits == pytest.approx(1.0)
        assert accounting.spend("run-2").total_credits == pytest.approx(9.0)

    def test_a_run_with_no_stamped_session_has_spent_nothing(
        self, accounting: RunAccounting, ledger_path: Path, run: str
    ) -> None:
        seed_shard(ledger_path, date.today(), [turn("somebody-else", 3.0)])
        spend = accounting.spend(run)
        assert spend.total_credits == 0.0
        assert spend.sessions == ()

    def test_turns_from_earlier_days_of_a_long_run_still_count(
        self, accounting: RunAccounting, ledger_path: Path, store: StateStore, ref: SpecRef
    ) -> None:
        # A run that started three days ago must include the shards from those
        # days, not only today's.
        started = datetime.now(timezone.utc) - timedelta(days=3)
        store.create_run("long-run", ref, state=EXECUTING)
        backdate_run(store, "long-run", started)
        accounting.stamp("long-run", "session-a")
        seed_shard(ledger_path, started.date(), [turn("session-a", 2.0)])
        seed_shard(ledger_path, date.today(), [turn("session-a", 1.0)])
        assert accounting.spend("long-run").total_credits == pytest.approx(3.0)

    def test_a_torn_or_unparseable_row_is_skipped_rather_than_fatal(
        self, accounting: RunAccounting, ledger_path: Path, run: str
    ) -> None:
        accounting.stamp(run, "session-a")
        path = seed_shard(ledger_path, date.today(), [turn("session-a", 1.0)])
        with path.open("a", encoding="utf-8") as handle:
            handle.write('{"_type": "tokens", "slot": "session-a", "cred')  # torn append
        assert accounting.spend(run).total_credits == pytest.approx(1.0)

    def test_a_non_finite_credit_value_is_dropped_not_propagated(
        self, accounting: RunAccounting, ledger_path: Path, run: str
    ) -> None:
        accounting.stamp(run, "session-a")
        path = seed_shard(ledger_path, date.today(), [turn("session-a", 2.0)])
        with path.open("a", encoding="utf-8") as handle:
            handle.write('{"_type": "tokens", "slot": "session-a", "credits": NaN}\n')
        total = accounting.spend(run).total_credits
        # NaN would make every later comparison against the ceiling false, which
        # reads as an unbounded run.
        assert total == pytest.approx(2.0)

    def test_a_shard_that_is_not_a_date_is_ignored(
        self, accounting: RunAccounting, ledger_path: Path, run: str
    ) -> None:
        ledger_path.mkdir(parents=True, exist_ok=True)
        (ledger_path / "notes.jsonl").write_text(
            json.dumps(turn("session-a", 50.0)) + "\n", encoding="utf-8"
        )
        (ledger_path / "2026-08-11.txt").write_text("ignored", encoding="utf-8")
        accounting.stamp(run, "session-a")
        assert accounting.spend(run).total_credits == 0.0


class TestDeclaredProviderCost:
    def test_declared_cost_lands_on_the_run_and_adds_to_the_ledger_total(
        self, accounting: RunAccounting, ledger_path: Path, store: StateStore, run: str
    ) -> None:
        accounting.stamp(run, "session-a")
        seed_shard(ledger_path, date.today(), [turn("session-a", 1.0)])
        accounting.cost_sink.attribute(
            run=run, capability="analysis", provider="external", credits=0.75
        )
        spend = accounting.spend(run)
        assert spend.metered_credits == pytest.approx(1.0)
        assert spend.declared_credits == pytest.approx(0.75)
        assert spend.total_credits == pytest.approx(1.75)
        # Persisted, so a restart does not forget what an external provider spent.
        record = store.get_run(run)
        assert record is not None
        assert record.detail[DECLARED_CREDITS_KEY] == pytest.approx(0.75)

    def test_declared_costs_accumulate(self, store: StateStore, run: str) -> None:
        sink = RunCostSink(store)
        sink.attribute(run=run, capability="analysis", provider="a", credits=0.5)
        sink.attribute(run=run, capability="review", provider="b", credits=0.25)
        assert sink.total_for(run) == pytest.approx(0.75)

    def test_zero_cost_and_unknown_runs_record_nothing(self, store: StateStore, run: str) -> None:
        sink = RunCostSink(store)
        sink.attribute(run=run, capability="analysis", provider="a", credits=0.0)
        sink.attribute(run="", capability="analysis", provider="a", credits=1.0)
        sink.attribute(run="ghost", capability="analysis", provider="a", credits=1.0)
        assert sink.total_for(run) == 0.0
        assert sink.total_for("ghost") == 0.0
