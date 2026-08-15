"""What a run may spend, and what the deterministic half of the engine spends.

Two cost guarantees are asserted here end to end, against a real metering ledger
and a real state store. Nothing is stubbed except the model: the ledger rows are
files on disk in the host's own shard format, the attribution claims are rows in
the state database, and the halt travels through the real run lifecycle machine.

**The ceiling.** A run is driven into its limit through the wave loop rather than
by asking the guard directly, so what stops it is the loop's own dispatch
decision. Two defects this engine shipped are asserted against by name: spend
that reached the ledger under a session the run had not stamped escaped the
ceiling, and a shard scan bounded by the LOCAL date against UTC timestamps
dropped most of a run's spend in a negative-offset timezone. The first is asserted
as "the ceiling sees the sum over every stamped session, including one stamped
after its turn was metered"; the second by running the sum under fixed timezones
on both sides of UTC over shards named for days either side of today.

**Zero model invocations for the deterministic paths.** This is an absence, so it
is asserted as one: no turn was opened, no host session appeared, no session was
stamped to the run, and the metering ledger holds no shard at all. A threshold
would pass whenever the threshold was wrong. The observer is then shown to be
awake -- the same object, over the same store and the same ledger directory,
records all four when a real turn is dispatched through it -- because an observer
that cannot see an invocation proves nothing by seeing none.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.apps.builtins.spec_engine.engine import local_analyzer
from kiro_crew.apps.builtins.spec_engine.engine.analysis import (
    SemanticTurnRequest,
    SemanticTurnResponse,
)
from kiro_crew.apps.builtins.spec_engine.engine.audit import AuditLog
from kiro_crew.apps.builtins.spec_engine.engine.autonomy import (
    AUTONOMY_FIELD,
    AutonomyDecision,
    AutonomyLevel,
)
from kiro_crew.apps.builtins.spec_engine.engine.budget import (
    CEILING_SETTING,
    DETAIL_CEILING_CREDITS,
    DETAIL_CONSUMED_CREDITS,
    SESSION_CLAIM_KIND,
    SESSION_CLAIM_SCOPE,
    MeteringLedger,
    RecordingNotifier,
    RunAccounting,
    format_credits,
)
from kiro_crew.apps.builtins.spec_engine.engine.capabilities import (
    ArtifactRef,
    CapabilityRequest,
)
from kiro_crew.apps.builtins.spec_engine.engine.config import (
    DASHBOARD_SURFACE,
    ConfigStore,
)
from kiro_crew.apps.builtins.spec_engine.engine.delivery import (
    PUBLISH_STAGE,
    SUBMIT_STAGE,
    TEARDOWN_STAGE,
    VERIFY_STAGE,
    DeliveryOutcome,
    DeliveryPipeline,
    RunContext,
    StageExecutor,
    StageResult,
    WorkspaceBroker,
    WorkspaceJanitor,
    resolve_authority,
)
from kiro_crew.apps.builtins.spec_engine.engine.orchestrator import (
    ExecutionOutcome,
    ReviewVerdict,
    TaskResult,
    WaveRunner,
    orchestrator_for,
    workspace_root,
)
from kiro_crew.apps.builtins.spec_engine.engine.roles import Dispatch, SessionDefault
from kiro_crew.apps.builtins.spec_engine.engine.runs import RunMachine, RunState
from kiro_crew.apps.builtins.spec_engine.engine.state import SpecRef, StateStore
from kiro_crew.apps.builtins.spec_engine.engine.turns import (
    DispatchedSemanticProvider,
    HostTurn,
    TurnOutcome,
    TurnRequest,
)
from kiro_crew.apps.builtins.spec_engine.engine.watch import poll_tick

from .conftest import make_spec_dir
from .test_budget_killswitch import spend_credits
from .test_budget_ledger import seed_shard, turn
from .test_e2e_delivery_lifecycle import script
from .test_orchestrator_waves import write_tasks

PROJECT = "acme"
SOURCE = "tracker"
RUN = "run-cost"
OTHER_RUN = "run-sibling"
SPEC = "example"

#: The ceiling every ceiling test runs under. Small enough that seeded amounts
#: are readable, and finite so a bounded budget is really in force.
CEILING = 1.0

#: What the observing turn host records as the cost of one dispatched turn.
TURN_CREDITS = 0.25


@dataclass(frozen=True)
class Harness:
    """A spec, the engine state around it, and the real metering ledger."""

    project: Path
    ref: SpecRef
    config: ConfigStore
    state: StateStore
    audit: AuditLog
    notifier: RecordingNotifier
    accounting: RunAccounting
    ledger_path: Path
    bin_dir: Path

    @property
    def machine(self) -> RunMachine:
        return RunMachine(self.state, self.config, project=PROJECT, audit=self.audit)

    def start_run(self, run_id: str = RUN) -> str:
        machine = self.machine
        machine.create(self.ref, run_id=run_id)
        machine.transition(self.ref, run_id, RunState.EXECUTING)
        return run_id

    def context(self, **overrides: str) -> RunContext:
        values: dict[str, str] = {
            "spec_name": SPEC,
            "spec_type": "feature",
            "workspace_path": str(self.project),
            "base_branch": "main",
            "branch_name": "spec/cost",
            "review_title": "Cost guarantees",
            "review_summary": "No model was asked anything.",
        }
        values.update(overrides)
        return RunContext(**values)

    def run_detail(self, run_id: str = RUN) -> dict[str, Any]:
        record = self.state.get_run(run_id)
        assert record is not None
        return dict(record.detail)

    def ceiling(self, credits: float) -> None:
        section, key = CEILING_SETTING.split(".", 1)
        self.config.write({section: {key: credits}}, surface=DASHBOARD_SURFACE)

    def stages(self, **stages: list[list[str]]) -> None:
        self.config.write(
            {"projects": {PROJECT: {"path": str(self.project), "workflow": {"stages": stages}}}},
            surface=DASHBOARD_SURFACE,
        )


@pytest.fixture()
def harness(tmp_path: Path) -> Harness:
    project = tmp_path / "project"
    project.mkdir()
    spec_dir = make_spec_dir(project, SPEC)
    write_tasks(spec_dir, [["1.1"], ["2.1"]])
    state = StateStore(root=tmp_path / "engine-state")
    config = ConfigStore(tmp_path / "config")
    config.write(
        {"projects": {PROJECT: {"path": str(project), "base_branch": "main"}}},
        surface=DASHBOARD_SURFACE,
    )
    ledger_path = tmp_path / "usage" / "tokens"
    return Harness(
        project=project,
        ref=SpecRef.of(project, SPEC),
        config=config,
        state=state,
        audit=AuditLog(tmp_path / "audit"),
        notifier=RecordingNotifier(),
        accounting=RunAccounting(state, ledger=MeteringLedger(ledger_path)),
        ledger_path=ledger_path,
        bin_dir=tmp_path / "bin",
    )


class RecordingWorker:
    """Records every leaf it was handed, and optionally meters what it spent.

    Stands in for the model, and *only* for the model: when a test gives it an
    amount it writes a real row into the real shard directory under the session
    the run stamped, which is how the ledger learns about a turn in production.
    """

    def __init__(
        self,
        harness: Harness | None = None,
        *,
        spends: float = 0.0,
        run_id: str = RUN,
    ) -> None:
        self._harness = harness
        self._spends = spends
        self._run_id = run_id
        self.dispatched: list[str] = []

    def __call__(self, *, task: str, dispatch: Dispatch, context: RunContext) -> TaskResult:
        self.dispatched.append(task)
        if self._harness is not None and self._spends:
            spend_credits(
                self._harness.accounting,
                self._harness.ledger_path,
                self._run_id,
                self._spends,
            )
        return TaskResult(ok=True)


class ApprovingReviewer:
    def __call__(self, *, task: str, dispatch: Dispatch, context: RunContext) -> ReviewVerdict:
        return ReviewVerdict(approved=True, reason=f"{task} approved")


def runner_for(
    harness: Harness,
    worker: RecordingWorker,
    *,
    run_id: str = RUN,
    level: AutonomyLevel = AutonomyLevel.EXECUTION,
) -> WaveRunner:
    return orchestrator_for(
        harness.ref,
        run_id,
        state=harness.state,
        config=harness.config,
        authority=resolve_authority(
            harness.config,
            decision=AutonomyDecision(
                level=level,
                source=SOURCE,
                spec_type="feature",
                submitter_class="maintainer",
                declared_at=f"sources.{SOURCE}.{AUTONOMY_FIELD}.maintainer.feature",
            ),
            project=PROJECT,
            base_branch="main",
        ),
        worker=worker,
        reviewer=ApprovingReviewer(),
        project=PROJECT,
        session_default=SessionDefault(model="session-model"),
        audit=harness.audit,
        notifier=harness.notifier,
        accounting=harness.accounting,
    )


class TestTheCeilingHalts:
    """A seeded ledger, and a run driven into the number it may not pass."""

    def test_a_run_already_over_its_ceiling_dispatches_nothing(self, harness: Harness) -> None:
        harness.ceiling(CEILING)
        harness.start_run()
        # Two sessions, because a run authors in one and orchestrates in another:
        # a ceiling that counted either alone would let this run through.
        spend_credits(harness.accounting, harness.ledger_path, RUN, 0.9, 0.6)
        worker = RecordingWorker()

        report = runner_for(harness, worker).execute(harness.context())

        assert report.outcome is ExecutionOutcome.HALTED
        assert worker.dispatched == [], "a halted run dispatched work anyway"
        record = harness.state.get_run(RUN)
        assert record is not None
        assert record.state == RunState.HALTED_BUDGET.value
        # The parked row and the amount that parked it agree, and both agree with
        # the sum the accounting reports.
        detail = harness.run_detail()
        assert detail[DETAIL_CONSUMED_CREDITS] == pytest.approx(1.5)
        assert detail[DETAIL_CEILING_CREDITS] == pytest.approx(CEILING)
        assert record.cost_credits == pytest.approx(1.5)
        assert harness.accounting.spend(RUN).total_credits == pytest.approx(1.5)
        assert f"consuming {format_credits(1.5)} of {format_credits(CEILING)}" in report.reason

    def test_a_run_under_its_ceiling_runs_every_wave(self, harness: Harness) -> None:
        # The direction that makes the halt above mean something: the same
        # machinery, one seeded amount lower, dispatches everything.
        harness.ceiling(CEILING)
        harness.start_run()
        spend_credits(harness.accounting, harness.ledger_path, RUN, 0.4)
        worker = RecordingWorker()

        report = runner_for(harness, worker).execute(harness.context())

        assert report.outcome is ExecutionOutcome.COMPLETED, report.reason
        assert worker.dispatched == ["1.1", "2.1"]
        record = harness.state.get_run(RUN)
        assert record is not None
        assert record.state != RunState.HALTED_BUDGET.value

    def test_the_ceiling_stops_the_leaves_that_have_not_started_yet(self, harness: Harness) -> None:
        """Spend that happens mid-run halts the rest of the run.

        The seeded-ledger cases above start over the line. This one crosses it
        while running, which is the case the dispatch decision exists for: the
        first leaf's own turn is what puts the run past its ceiling, and the
        second wave must not be dispatched.
        """
        harness.ceiling(CEILING)
        harness.start_run()
        worker = RecordingWorker(harness, spends=1.4)

        report = runner_for(harness, worker).execute(harness.context())

        assert worker.dispatched == ["1.1"], "the wave after the ceiling was dispatched"
        assert report.outcome is ExecutionOutcome.HALTED
        # The second wave was reached and its leaf was refused, which is a
        # different record from a wave the loop never got to.
        assert report.waves[-1].not_dispatched == ("2.1",)
        assert harness.state.get_run(RUN) is not None
        assert harness.run_detail()[DETAIL_CONSUMED_CREDITS] == pytest.approx(1.4)

    def test_a_sibling_runs_spend_is_not_charged_to_this_run(self, harness: Harness) -> None:
        """The halt is the run's OWN attributed total, not the ledger's contents.

        Both runs write into the same shard directory, because in production they
        write into the same one. Only the sessions this run stamped may count.
        """
        harness.ceiling(CEILING)
        harness.start_run()
        harness.start_run(OTHER_RUN)
        spend_credits(harness.accounting, harness.ledger_path, OTHER_RUN, 5.0)
        spend_credits(harness.accounting, harness.ledger_path, RUN, 0.3)
        worker = RecordingWorker()

        report = runner_for(harness, worker).execute(harness.context())

        assert report.outcome is ExecutionOutcome.COMPLETED, report.reason
        assert harness.accounting.spend(RUN).total_credits == pytest.approx(0.3)
        assert harness.accounting.spend(OTHER_RUN).total_credits == pytest.approx(5.0)

    def test_spend_metered_before_its_session_was_stamped_still_counts(
        self, harness: Harness
    ) -> None:
        """The attribution defect, asserted from the ledger's side.

        A turn that spent and only afterwards had its session attributed used to
        escape the ceiling entirely. Attribution is by stamp, not by ordering, so
        a row already on disk counts from the moment the session is stamped -- and
        it has to, because that is the shape every late stamp has.
        """
        harness.ceiling(CEILING)
        harness.start_run()
        late = "screening-session"
        seed_shard(harness.ledger_path, date.today(), [turn(late, 1.25)])
        assert harness.accounting.spend(RUN).total_credits == pytest.approx(0.0)

        harness.accounting.stamp(RUN, late)

        assert harness.accounting.spend(RUN).total_credits == pytest.approx(1.25)
        report = runner_for(harness, RecordingWorker()).execute(harness.context())
        assert report.outcome is ExecutionOutcome.HALTED

    def test_declared_provider_cost_counts_against_the_same_ceiling(self, harness: Harness) -> None:
        # Spend inside an external provider's own process never reaches the
        # metering ledger, so a ceiling reading only the ledger would treat a
        # delegated capability as free.
        harness.ceiling(CEILING)
        harness.start_run()
        spend_credits(harness.accounting, harness.ledger_path, RUN, 0.5)
        harness.accounting.cost_sink.attribute(
            run=RUN, capability="analysis", provider="external", credits=0.7
        )

        spend = harness.accounting.spend(RUN)

        assert spend.metered_credits == pytest.approx(0.5)
        assert spend.declared_credits == pytest.approx(0.7)
        assert spend.total_credits == pytest.approx(1.2)
        report = runner_for(harness, RecordingWorker()).execute(harness.context())
        assert report.outcome is ExecutionOutcome.HALTED


#: Timezones on both sides of UTC, and one of them is far enough west that the
#: local date is behind the UTC date for half of every day. The shard names are
#: local dates and the rows carry UTC timestamps, so a scan bounded by either
#: one drops real spend here.
@pytest.mark.parametrize("zone", ["Etc/GMT+12", "Etc/GMT-14", "UTC"])
class TestTheSumIsNotBoundedByADate:
    def test_every_shard_of_a_run_counts_whatever_day_it_is_named_for(
        self, harness: Harness, monkeypatch: pytest.MonkeyPatch, zone: str
    ) -> None:
        monkeypatch.setenv("TZ", zone)
        time.tzset()
        harness.start_run()
        session = "run-cost-session"
        harness.accounting.stamp(RUN, session)
        utc_today = datetime.now(timezone.utc).date()
        # Deduplicated, because the local and UTC dates coincide for part of
        # every day: which of these names differ is a function of the clock, and
        # that is precisely why a scan bounded by either date is unsound.
        days = sorted(
            {
                date.today() - timedelta(days=3),
                date.today(),
                utc_today,
                utc_today + timedelta(days=1),
            }
        )
        assert len(days) >= 3, "the fixture stopped covering days either side of today"
        for day in days:
            seed_shard(harness.ledger_path, day, [turn(session, 0.5)])

        spend = harness.accounting.spend(RUN)

        assert spend.metered_credits == pytest.approx(0.5 * len(days))
        assert spend.records == len(days)
        assert spend.metered_sessions == (session,)
        assert len(harness.accounting.ledger.shards()) == len(days)


class TurnObserver:
    """Everything a model invocation leaves behind, watched through one object.

    Satisfies :class:`~..engine.turns.TurnHost`, so it can be handed to the
    engine's own dispatch path. When a turn really runs it does what the gateway
    does -- writes a metering row for the session it opened -- so the ledger the
    engine reads is the ledger this fills.

    The four observations are deliberately of different kinds: a call this object
    saw, a claim in the state database, a file in the shard directory, and the
    accounting sum over the run. A path that reached a model while defeating all
    four would have to be trying.
    """

    def __init__(self, harness: Harness, *, credits: float = TURN_CREDITS) -> None:
        self._harness = harness
        self._credits = credits
        self.opened: list[TurnRequest] = []
        self.prompts: list[str] = []
        self.closed = 0

    # --- TurnHost ----------------------------------------------------------

    def open_turn(self, request: TurnRequest) -> HostTurn:
        self.opened.append(request)
        return _ObservedTurn(self, f"spec-analysis-{len(self.opened)}")

    # --- observations ------------------------------------------------------

    @property
    def invocations(self) -> int:
        return len(self.opened)

    def sessions_opened(self) -> list[str]:
        return [f"spec-analysis-{index + 1}" for index in range(len(self.opened))]

    def sessions_stamped(self) -> list[str]:
        return [
            record.subject
            for record in self._harness.state.list_claims(
                kind=SESSION_CLAIM_KIND, scope=SESSION_CLAIM_SCOPE
            )
        ]

    def shards(self) -> list[Path]:
        return self._harness.accounting.ledger.shards()

    def metered_rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for path in self.shards():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    rows.append(json.loads(line))
        return rows

    def credits_recorded(self, run_id: str = RUN) -> float:
        return self._harness.accounting.spend(run_id).total_credits

    def meter(self, session_key: str) -> None:
        """Record the turn's cost the way the host's metering ledger does."""
        seed_shard(self._harness.ledger_path, date.today(), [turn(session_key, self._credits)])

    def assert_nothing_was_invoked(self, run_id: str = RUN) -> None:
        """Every observation, asserted as an absence rather than as a bound."""
        assert self.invocations == 0, f"a model turn was opened: {self.opened}"
        assert self.sessions_stamped() == [], "a host session was stamped to a run"
        assert self.shards() == [], "the metering ledger holds a shard"
        assert self.metered_rows() == [], "a credit was recorded"
        assert self.credits_recorded(run_id) == 0.0


@dataclass
class _ObservedTurn:
    """One opened host session, and the turn that may run in it."""

    observer: TurnObserver
    key: str
    payload: dict[str, Any] = field(
        default_factory=lambda: {"findings": [], "coverage": {"processed": [], "skipped": []}}
    )

    @property
    def session_key(self) -> str:
        return self.key

    def run(self, prompt: str, *, deadline_s: int) -> TurnOutcome:
        self.observer.prompts.append(prompt)
        self.observer.meter(self.key)
        return TurnOutcome(text=json.dumps(self.payload), model="a-model", effort="")

    def close(self) -> None:
        self.observer.closed += 1


def stage_script(harness: Harness, name: str, *, prints: str = "") -> list[str]:
    """A deterministic stage command: it records that it ran and prints *prints*."""
    body = 'echo "ran" > "$1"\n'
    if prints:
        body += f'echo "{prints}"\n'
    marker = harness.bin_dir / f"{name}.marker"
    return [str(script(harness.bin_dir, f"stage-{name}", body)), str(marker)]


class TestDeterministicStagesInvokeNoModel:
    """The engine's central cost guarantee, asserted as an absence."""

    def test_the_delivery_flow_spawns_processes_and_asks_no_model(self, harness: Harness) -> None:
        harness.stages(
            **{
                SUBMIT_STAGE: [stage_script(harness, SUBMIT_STAGE, prints="https://r.invalid/1")],
                VERIFY_STAGE: [stage_script(harness, VERIFY_STAGE)],
                PUBLISH_STAGE: [stage_script(harness, PUBLISH_STAGE, prints="https://d.invalid/1")],
            }
        )
        harness.start_run()
        observer = TurnObserver(harness)
        pipeline = DeliveryPipeline(
            harness.config,
            authority=resolve_authority(
                harness.config,
                decision=AutonomyDecision(
                    level=AutonomyLevel.DELIVERY,
                    source=SOURCE,
                    spec_type="feature",
                    submitter_class="maintainer",
                    declared_at=f"sources.{SOURCE}.{AUTONOMY_FIELD}.maintainer.feature",
                ),
                project=PROJECT,
                base_branch="main",
            ),
            project=PROJECT,
            isolation=WorkspaceBroker(harness.state, root=workspace_root(harness.state)),
        )
        pipeline.isolate(harness.context(), run_id=RUN)

        run = pipeline.deliver(harness.context())

        assert run.outcome is DeliveryOutcome.PASSED, run.reason
        assert run.deployment_addresses == ("https://d.invalid/1",)
        assert (harness.bin_dir / f"{PUBLISH_STAGE}.marker").exists(), "no stage actually ran"
        observer.assert_nothing_was_invoked()

    def test_teardown_removes_a_deployment_and_asks_no_model(self, harness: Harness) -> None:
        harness.stages(**{TEARDOWN_STAGE: [stage_script(harness, TEARDOWN_STAGE)]})
        harness.start_run()
        broker = WorkspaceBroker(harness.state, root=workspace_root(harness.state))
        broker.record_deployment(RUN, address="https://d.invalid/2")
        observer = TurnObserver(harness)
        executor = StageExecutor(harness.config, project=PROJECT)

        def run_stage(run_id: str) -> StageResult:
            return executor.run(TEARDOWN_STAGE, harness.context())

        report = WorkspaceJanitor(
            harness.state,
            root=workspace_root(harness.state),
            stage=run_stage,
        ).archive_run(RUN)

        assert report.complete is True
        assert (harness.bin_dir / f"{TEARDOWN_STAGE}.marker").exists()
        observer.assert_nothing_was_invoked()

    def test_a_watch_poll_tick_asks_no_model(self, harness: Harness) -> None:
        # The canonical deterministic stage: a configured argv, run on a
        # schedule, forever. A poll that cost a turn would be the cost of a
        # watcher multiplied by its interval and divided by nothing.
        poll = script(harness.bin_dir, "poll", "echo '[]'\n")
        harness.config.write(
            {"sources": {SOURCE: {"enabled": True, "poll": [str(poll)]}}},
            surface=DASHBOARD_SURFACE,
        )
        harness.start_run()
        observer = TurnObserver(harness)

        report = poll_tick(harness.config)

        assert [outcome.source for outcome in report.outcomes] == [SOURCE]
        assert report.outcomes[0].healthy, report.outcomes[0].detail
        observer.assert_nothing_was_invoked()

    def test_the_bundled_structural_analyzer_asks_no_model(self, harness: Harness) -> None:
        harness.start_run()
        observer = TurnObserver(harness)
        spec_dir = harness.ref.spec_dir

        response = local_analyzer.LocalAnalyzer().serve(
            CapabilityRequest(
                capability=local_analyzer.CAPABILITY,
                spec_type="feature",
                artifacts=tuple(
                    ArtifactRef.of(kind, spec_dir / f"{kind}.md")
                    for kind in ("requirements", "design", "tasks")
                ),
                run=RUN,
            )
        )

        assert response.cost_credits == 0.0
        assert response.coverage.processed, "the analyzer read nothing, so it proved nothing"
        observer.assert_nothing_was_invoked()

    def test_a_run_whose_leaves_are_deterministic_records_no_spend(self, harness: Harness) -> None:
        # The whole wave loop, including the guard, the isolate decision and the
        # terminal sweep: a run costs what its turns cost, and a run that took no
        # turn costs nothing.
        harness.ceiling(CEILING)
        harness.start_run()
        observer = TurnObserver(harness)
        worker = RecordingWorker()

        report = runner_for(harness, worker).run(harness.context())

        assert report.outcome is ExecutionOutcome.COMPLETED, report.reason
        assert worker.dispatched == ["1.1", "2.1"]
        observer.assert_nothing_was_invoked()
        assert report.completion is not None
        assert report.completion.report is not None
        assert report.completion.report.consumed_credits == 0.0


class TestTheObserverIsNotBlind:
    """The same harness, watching a turn that really happens.

    Without this every assertion above holds for an observer wired to nothing.
    """

    def test_a_dispatched_analysis_turn_moves_all_four_observations(self, harness: Harness) -> None:
        harness.start_run()
        observer = TurnObserver(harness)
        observer.assert_nothing_was_invoked()

        response = DispatchedSemanticProvider(observer).analyze(
            SemanticTurnRequest(
                run=RUN,
                ref=harness.ref,
                spec_type="feature",
                format_version="1",
                guidance="Analyse the documents below.",
                documents=(("requirements", "# Requirements Document\n"),),
                turn_options={"model": "a-model"},
                deadline_s=30,
                stamp=harness.accounting.dispatch_stamp(RUN),
            )
        )

        assert isinstance(response, SemanticTurnResponse)
        # One observation at a time, because each is what a different absence
        # above was asserting.
        assert observer.invocations == 1
        assert observer.sessions_opened() == [response.session_key]
        assert observer.sessions_stamped() == [response.session_key]
        assert len(observer.shards()) == 1
        assert len(observer.metered_rows()) == 1
        assert observer.credits_recorded() == pytest.approx(TURN_CREDITS)
        # And the run it was dispatched for owns the session, so the spend the
        # ceiling compares is the spend this turn caused.
        assert harness.accounting.sessions_for(RUN) == (response.session_key,)
        with pytest.raises(AssertionError):
            observer.assert_nothing_was_invoked()

    def test_the_turn_the_observer_saw_carried_the_run_and_the_documents(
        self, harness: Harness
    ) -> None:
        harness.start_run()
        observer = TurnObserver(harness)

        DispatchedSemanticProvider(observer).analyze(
            SemanticTurnRequest(
                run=RUN,
                ref=harness.ref,
                spec_type="feature",
                format_version="1",
                guidance="Analyse the documents below.",
                documents=(("design", "# Design Document\n"),),
                turn_options={"model": "a-model"},
                deadline_s=30,
                stamp=harness.accounting.dispatch_stamp(RUN),
            )
        )

        assert observer.opened[0].run_id == RUN
        assert "# Design Document" in observer.prompts[0]
        assert observer.closed == 1
