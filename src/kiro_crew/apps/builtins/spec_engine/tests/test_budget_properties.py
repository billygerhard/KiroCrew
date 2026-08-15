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

from datetime import date, timedelta
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
    RunSessions,
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


# --- Attribution completeness over a run's whole session tree ---------------
#
# The property above generates flat session keys split between two runs. A real
# run does not have flat sessions: it has an authoring session, an orchestrator
# session, and one subagent session per dispatched task, and credits are spent in
# all of them plus, for a delegated capability provider, outside every host
# session. "Every credit spent by any session belonging to a run is attributed to
# that run" is a claim about that whole tree, and the failure it catches is spend
# that lands nowhere and so never counts against a ceiling.

#: The session roles one run creates. Named rather than generated, because the
#: claim is that no ROLE is missed; which arbitrary string a session key holds is
#: already covered by the generated keys above.
_ROLES = ("authoring", "orchestrator", "subagent-0", "subagent-1", "review")

#: Credits per session per role, including zero: a session that recorded a turn
#: costing nothing is still a session the total must account for.
_ROLE_SPEND = st.lists(
    st.tuples(st.sampled_from(_ROLES), CREDITS),
    min_size=0,
    max_size=8,
)

#: Days before the run row that a shard can carry turns for. Kept inside the
#: scan window so this property measures attribution rather than the window; the
#: window itself is the subject of the xfail below.
_TODAY_ONLY = 0


@_SETTINGS
@given(spend=_ROLE_SPEND, declared=CREDITS, orphan=CREDITS)
def test_every_session_of_a_run_is_attributed_and_no_other_session_is(
    tmp_path_factory: Any,
    spend: list[tuple[str, float]],
    declared: float,
    orphan: float,
) -> None:
    """Authoring, orchestrator and subagent spend all land on the run.

    Also pins the two directions a total can be wrong: a session belonging to no
    run must not be charged to this one, and a delegated provider's declared
    credits -- spent in another process, so appearing in no shard -- must be
    included, because a ledger-only total treats a delegated provider as free.
    """
    store, _machine, ref, ledger_path = _tree(Path(tmp_path_factory.mktemp("tree")))
    accounting = RunAccounting(store, ledger=MeteringLedger(ledger_path))
    store.create_run("run-a", ref, state=RunState.EXECUTING.value)
    store.create_run("run-b", ref, state=RunState.EXECUTING.value)

    rows: list[dict[str, Any]] = []
    expected = 0.0
    for role, credits in spend:
        session = f"run-a-{role}"
        accounting.stamp("run-a", session)
        rows.append(turn(session, credits))
        expected += credits
    # A session no run ever stamped. Its spend is real and belongs to nobody, so
    # it must not be absorbed by whichever run happens to be asked first.
    rows.append(turn("unstamped-session", orphan))
    # A session of another run, so "this run's sessions" is doing work.
    accounting.stamp("run-b", "run-b-orchestrator")
    rows.append(turn("run-b-orchestrator", orphan))
    seed_shard(ledger_path, _shard_date(store, "run-a", _TODAY_ONLY), rows)
    if declared > 0:
        accounting.cost_sink.attribute(
            run="run-a", capability="analysis", provider="external", credits=declared
        )

    result = accounting.spend("run-a")

    assert result.metered_credits == pytest.approx(expected, rel=1e-9, abs=1e-9)
    assert result.declared_credits == pytest.approx(declared, rel=1e-9, abs=1e-9)
    # The ceiling compares this number, so it is the one that has to be whole.
    assert result.total_credits == pytest.approx(expected + declared, rel=1e-9, abs=1e-9)
    assert set(result.sessions) == {f"run-a-{role}" for role, _ in spend}
    assert "unstamped-session" not in result.sessions
    assert "run-b-orchestrator" not in result.sessions


@_SETTINGS
@given(roles=st.lists(st.sampled_from(_ROLES), min_size=1, max_size=3, unique=True))
def test_an_unstamped_session_is_attributed_to_no_run_at_all(
    tmp_path_factory: Any, roles: list[str]
) -> None:
    """Spend with no stamp is charged to nobody, rather than to someone.

    The dangerous shape is not "unattributed"; it is "attributed to whichever run
    asked", because that spends another run's ceiling.
    """
    store, _machine, ref, ledger_path = _tree(Path(tmp_path_factory.mktemp("orphan")))
    accounting = RunAccounting(store, ledger=MeteringLedger(ledger_path))
    store.create_run("run-a", ref, state=RunState.EXECUTING.value)
    rows = [turn(f"nobody-{role}", 3.0) for role in roles]
    seed_shard(ledger_path, _shard_date(store, "run-a", _TODAY_ONLY), rows)

    result = accounting.spend("run-a")

    assert result.metered_credits == 0.0
    assert result.sessions == ()
    for role in roles:
        assert RunSessions(store).run_for(f"nobody-{role}") is None


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Known defect: RunAccounting.spend bounds the shard scan at the run row's "
        "creation date minus one day, so a session that recorded turns before that "
        "window has its credits dropped from the run's total and they never count "
        "against a ceiling. Shards are named by LOCAL date while created_ts is UTC, "
        "so the one-day margin is also consumed entirely by the skew in a "
        "negative-offset timezone during the evening -- the case the margin exists "
        "for. Reported, not fixed: this property belongs to the test task and "
        "engine/budget/ledger.py belongs to another."
    ),
)
@_SETTINGS
@given(credits=st.floats(min_value=0.5, max_value=10.0), days_back=st.integers(2, 6))
def test_spend_recorded_before_the_run_row_existed_is_still_attributed(
    tmp_path_factory: Any, credits: float, days_back: int
) -> None:
    """A session may predate the run row it belongs to.

    An authoring session is open before the run is created, so its turns land in
    a shard older than the run row. Those credits were spent by a session the run
    owns, so the run's total has to include them -- otherwise the first thing a
    ceiling is asked about is a number that is missing the work that already
    happened.

    ``days_back`` starts at two so the case is timezone-independent: one day is
    inside the margin in some zones and outside it in others, and a property that
    passed or failed by timezone would be a flake rather than a finding.
    """
    store, _machine, ref, ledger_path = _tree(Path(tmp_path_factory.mktemp("early")))
    accounting = RunAccounting(store, ledger=MeteringLedger(ledger_path))
    store.create_run("run-a", ref, state=RunState.EXECUTING.value)
    accounting.stamp("run-a", "run-a-authoring")
    seed_shard(
        ledger_path,
        _shard_date(store, "run-a", days_back),
        [turn("run-a-authoring", credits)],
    )

    result = accounting.spend("run-a")

    assert result.metered_credits == pytest.approx(credits, rel=1e-9, abs=1e-9)


def _shard_date(store: StateStore, run_id: str, days_back: int) -> date:
    """A shard date *days_back* days before today, as the ledger names shards.

    Shards are named for the local day, so this is derived from ``date.today()``
    rather than from the run row's UTC timestamp: naming a shard by a UTC date
    would put the fixture and the reader on different calendars.
    """
    assert store.get_run(run_id) is not None
    return date.today() - timedelta(days=days_back)
