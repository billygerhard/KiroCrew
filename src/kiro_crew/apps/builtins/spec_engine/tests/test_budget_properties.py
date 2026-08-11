"""Property: a run's reported cost is the whole ledger, and headless runs are bounded.

Two universally quantified claims, over generated session sets and turn amounts:

* the cost a run reports equals the sum of the metering records for the sessions
  stamped with its identifier — no session dropped, no other run's session
  included, whatever the arrangement of sessions and turns;
* a headless run always executes under a finite ceiling, or it does not execute.

The generators deliberately let two runs reach for overlapping session keys, so
the failure these catch is the one that matters: a total that quietly includes or
excludes the wrong session.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given
from hypothesis import settings as hyp_settings
from hypothesis import strategies as st

from kiro_crew.apps.builtins.spec_engine.engine.budget import (
    Budget,
    BudgetGuard,
    DispatchOutcome,
    MeteringLedger,
    RecordingNotifier,
    RunAccounting,
    SessionAttributionConflict,
)
from kiro_crew.apps.builtins.spec_engine.engine.config import ConfigStore
from kiro_crew.apps.builtins.spec_engine.engine.runs import RunMachine, RunState
from kiro_crew.apps.builtins.spec_engine.engine.state import SpecRef, StateStore

from .conftest import make_spec_dir
from .test_budget_ledger import seed_shard, turn

#: Credit amounts a turn can carry: fractional, finite, never negative.
CREDITS = st.floats(min_value=0.0, max_value=50.0, allow_nan=False, allow_infinity=False)

#: A small session-key alphabet, so two runs collide often enough for the
#: one-session-one-run rule to be exercised rather than assumed.
SESSION_KEYS = st.text(alphabet="abcd-", min_size=1, max_size=4)

#: Turns for one run: enough sessions that a single-session total is obviously
#: wrong, and an empty list because a run that has spent nothing is also a case.
TURNS = st.lists(st.tuples(SESSION_KEYS, CREDITS), min_size=0, max_size=10)

_SETTINGS = hyp_settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)


def _tree(root: Path) -> tuple[StateStore, RunMachine, SpecRef, Path]:
    """A state store, a run machine, a spec to audit against, and an empty ledger."""
    project = root / "project"
    project.mkdir(parents=True, exist_ok=True)
    make_spec_dir(project, "example")
    store = StateStore(root=root / "state")
    machine = RunMachine(store, ConfigStore(root / "config"))
    return store, machine, SpecRef.of(project, "example"), root / "tokens"


@_SETTINGS
@given(first=TURNS, second=TURNS)
def test_reported_cost_is_the_ledger_sum_over_a_runs_own_sessions(
    tmp_path_factory: Any,
    first: list[tuple[str, float]],
    second: list[tuple[str, float]],
) -> None:
    store, _machine, ref, ledger_path = _tree(Path(tmp_path_factory.mktemp("attribution")))
    accounting = RunAccounting(store, ledger=MeteringLedger(ledger_path))
    store.create_run("run-a", ref, state=RunState.EXECUTING.value)
    store.create_run("run-b", ref, state=RunState.EXECUTING.value)

    # A session belongs to whichever run stamped it first, so the expected totals
    # are accumulated from the stamps that were accepted, not from the split the
    # generator produced.
    owner: dict[str, str] = {}
    expected = {"run-a": 0.0, "run-b": 0.0}
    rows: list[dict[str, Any]] = []
    for run_id, entries in (("run-a", first), ("run-b", second)):
        for session, credits in entries:
            try:
                accounting.stamp(run_id, session)
            except SessionAttributionConflict:
                pass
            owner.setdefault(session, run_id)
            rows.append(turn(session, credits))
            expected[owner[session]] += credits
    if rows:
        seed_shard(ledger_path, date.today(), rows)

    for run_id in ("run-a", "run-b"):
        spend = accounting.spend(run_id)
        assert spend.total_credits == pytest.approx(expected[run_id], rel=1e-9, abs=1e-9)
        # Every session the run holds a stamp for, and nothing else.
        assert set(spend.sessions) == {key for key, held in owner.items() if held == run_id}


@_SETTINGS
@given(ceiling=st.sampled_from([0.0, 0.01, 1.0, 5.0, float("inf")]), turns=TURNS)
def test_a_headless_run_either_runs_under_a_finite_ceiling_or_not_at_all(
    tmp_path_factory: Any, ceiling: float, turns: list[tuple[str, float]]
) -> None:
    store, machine, ref, ledger_path = _tree(Path(tmp_path_factory.mktemp("bounded")))
    accounting = RunAccounting(store, ledger=MeteringLedger(ledger_path))
    store.create_run("headless", ref, state=RunState.EXECUTING.value)
    rows: list[dict[str, Any]] = []
    for session, credits in turns:
        accounting.stamp("headless", session)
        rows.append(turn(session, credits))
    if rows:
        seed_shard(ledger_path, date.today(), rows)

    budget = Budget(ceiling_credits=ceiling)
    decision = BudgetGuard(
        "headless",
        ref,
        budget,
        state=store,
        machine=machine,
        accounting=accounting,
        notifier=RecordingNotifier(),
        headless=True,
    ).authorize_dispatch()

    if not budget.bounded:
        assert decision.outcome is DispatchOutcome.UNBOUNDED
        return
    if decision.spend.total_credits >= ceiling:
        assert decision.outcome is DispatchOutcome.HALTED
    else:
        assert decision.outcome is DispatchOutcome.ALLOWED
        assert decision.remaining_credits > 0
