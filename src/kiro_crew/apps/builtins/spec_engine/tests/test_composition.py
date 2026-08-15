"""The composition root: what a surface gets, and what it cannot get.

Every claim here is about the *construction*, not about the libraries being
constructed. Each library in the graph already has its own suite and passes it
while nothing builds it — that is the defect this module exists to remove — so
these tests are written to fail when a construction is deleted from
:func:`build_engine` even though the library it constructs is untouched.
"""

from __future__ import annotations

import dataclasses
import inspect
from pathlib import Path
from typing import Any, cast

import pytest

from kiro_crew.apps.builtins.spec_engine.engine import seeder as seeder_module
from kiro_crew.apps.builtins.spec_engine.engine.analysis import (
    AnalysisEngine,
    AnalysisReport,
    RecordingFindingsSink,
    StateFindingsSink,
)
from kiro_crew.apps.builtins.spec_engine.engine.audit import AuditLog
from kiro_crew.apps.builtins.spec_engine.engine.autonomy import AutonomyDecision, AutonomyLevel
from kiro_crew.apps.builtins.spec_engine.engine.budget.ceiling import guard_for
from kiro_crew.apps.builtins.spec_engine.engine.budget.ledger import RunAccounting, RunCostSink
from kiro_crew.apps.builtins.spec_engine.engine.capabilities.builtins import (
    AUTHORING_PROVIDER,
    IMPLEMENTATION_PROVIDER,
    MODEL_CATALOG_PROVIDER,
    REVIEW_PROVIDER,
    register_builtins,
)
from kiro_crew.apps.builtins.spec_engine.engine.capabilities.contracts import ProviderNature
from kiro_crew.apps.builtins.spec_engine.engine.capabilities.registry import (
    CapabilityRegistry,
    RecordingCostSink,
)
from kiro_crew.apps.builtins.spec_engine.engine.composition import (
    DURABLE_FINDINGS,
    EngineGraph,
    IncompleteEngineGraph,
    RunPrevented,
    build_engine,
    build_run_engine,
)
from kiro_crew.apps.builtins.spec_engine.engine.config import DASHBOARD_SURFACE
from kiro_crew.apps.builtins.spec_engine.engine.delivery import resolve_authority
from kiro_crew.apps.builtins.spec_engine.engine.notify.routing import HostNotifier
from kiro_crew.apps.builtins.spec_engine.engine.orchestrator import (
    ReviewVerdict,
    RunContext,
    TaskResult,
)
from kiro_crew.apps.builtins.spec_engine.engine.prerequisites import AUDIT_PREREQUISITE_UNMET
from kiro_crew.apps.builtins.spec_engine.engine.roles import Dispatch
from kiro_crew.apps.builtins.spec_engine.engine.runs import (
    RUN_TRANSITIONED_EVENT,
    RunMachine,
    RunState,
)
from kiro_crew.apps.builtins.spec_engine.engine.seeder import (
    AWAITING_REVIEW_EVENT,
    AWAITING_REVIEW_NOTIFY_FAILED_EVENT,
    SessionSeeder,
)
from kiro_crew.apps.builtins.spec_engine.engine.state import SpecRef, StateStore

from .test_analysis_wiring import author_spec

PROJECT = "acme"
SOURCE = "manual"
MODELS = ("host-model-a", "host-model-b")


class CountingFindingsSink:
    """A durable sink stands here: what matters is that it is the one wired in.

    Deliberately not a second spelling of :class:`RecordingFindingsSink` — a stub
    identical to the thing under test would pass whether or not the seam carried
    it, since the default is that thing.
    """

    def __init__(self) -> None:
        self.reports: list[tuple[str, str]] = []

    def record(self, ref: SpecRef, *, run: str, report: AnalysisReport) -> None:
        self.reports.append((ref.name, run))


def models() -> tuple[str, ...]:
    return MODELS


def analysable_spec(tmp_path: Path) -> SpecRef:
    """A spec the bundled analyzer has something to report about.

    Its tasks document claims no requirement, so the structural analyzer returns
    the uncovered criteria. That matters for the durability assertion: an analysis
    that found nothing would agree with a sink that recorded nothing.
    """
    project = tmp_path / "analysed"
    author_spec(project / ".kiro" / "specs" / "example")
    return SpecRef.of(project, "example")


class RecordingOpener:
    """Stands in for the host session manager, and records what it was asked for.

    Echoes the requested posture, which is the honest host: it applies what the
    engine resolved. Deliberately not a no-op returning ``None`` — the seeder is
    supposed to read a session key and a posture back, and a stub that returned
    neither would let a graph that never seeds anything look wired.
    """

    def __init__(self) -> None:
        self.requests: list[Any] = []

    def __call__(self, request: Any) -> Any:
        self.requests.append(request)
        return OpenedChat(f"chat-{len(self.requests)}", request.posture)


@dataclasses.dataclass(frozen=True)
class OpenedChat:
    session_key: str
    applied_posture: str


@dataclasses.dataclass(frozen=True)
class SeedForTest:
    """A resolved run, carrying only what the seeder reads off one."""

    run_id: str
    ref: SpecRef
    project: str
    working_tree: Path
    autonomy: AutonomyDecision = dataclasses.field(
        default_factory=lambda: AutonomyDecision(
            level=AutonomyLevel.AUTHORING,
            source=SOURCE,
            spec_type="feature",
            submitter_class="maintainer",
        )
    )

    def seed_text(self) -> str:
        return "author this spec"


def build(tmp_path: Path, **overrides: Any) -> EngineGraph:
    """A graph on temporary roots, with every required seam supplied."""
    kwargs: dict[str, Any] = {
        "model_resolver": models,
        "findings_sink": CountingFindingsSink(),
        "host_state": None,
        "session_opener": RecordingOpener(),
        "project": PROJECT,
        "state_root": tmp_path / "state",
        "audit_root": tmp_path / "audit",
        "config_root": tmp_path / "config",
    }
    kwargs.update(overrides)
    return build_engine(**kwargs)


class TestBuiltinsAreRegistered:
    """The registration a running engine's capability resolution depends on.

    Deleting ``register_builtins(...)`` from the composition root leaves every
    library test green: the registry still refuses to exist without a builtin per
    capability, and ``register_builtins`` still registers what it is told to.
    These assertions are the ones that notice.
    """

    def test_the_model_backed_capabilities_resolve_to_the_engine_paths(
        self, tmp_path: Path
    ) -> None:
        graph = build(tmp_path)
        expected = {
            "authoring": AUTHORING_PROVIDER,
            "review": REVIEW_PROVIDER,
            "implementation": IMPLEMENTATION_PROVIDER,
        }
        for capability, provider_name in expected.items():
            identity = graph.registry.builtin(capability).identity
            assert identity.name == provider_name
            # The shipped default is deterministic. Reading MODEL_BACKED here is
            # only possible because the composition root registered over it, and
            # it is what tells an operator the path spends credits.
            assert identity.nature is ProviderNature.MODEL_BACKED

    def test_the_model_catalog_resolves_through_the_hosts_resolver(self, tmp_path: Path) -> None:
        graph = build(tmp_path)
        provider = graph.registry.builtin("model_catalog")
        assert provider.identity.name == MODEL_CATALOG_PROVIDER
        resolver = getattr(provider, "resolver", None)
        assert resolver is not None, "the host catalog builtin was not the registered provider"
        assert tuple(resolver()) == MODELS

    def test_a_model_resolver_is_not_optional(self) -> None:
        """No default may mean "register nothing": the keyword is required."""
        parameter = inspect.signature(build_engine).parameters["model_resolver"]
        assert parameter.default is inspect.Parameter.empty


class TestFindingsSinkIsRequired:
    """The seam a caller must state, and the one a run path is not asked about.

    ``build_engine`` refuses to choose a sink silently, so the durable one has
    exactly one place to be passed. ``build_run_engine`` goes further and removes
    the choice, which is the subject of
    :class:`TestARunPathGraphRecordsFindingsDurably`.
    """

    def test_the_supplied_sink_is_the_one_the_analysis_engine_holds(self, tmp_path: Path) -> None:
        sink = CountingFindingsSink()
        graph = build(tmp_path, findings_sink=sink)
        assert graph.analysis.findings_sink is sink
        assert not isinstance(graph.analysis.findings_sink, RecordingFindingsSink)

    def test_a_findings_sink_is_not_optional(self) -> None:
        parameter = inspect.signature(build_engine).parameters["findings_sink"]
        assert parameter.default is inspect.Parameter.empty

    def test_a_sink_of_none_is_refused_rather_than_replaced_with_memory(
        self, tmp_path: Path
    ) -> None:
        """An untyped caller passing ``None`` must not land on the memory default."""
        with pytest.raises(ValueError):
            build(tmp_path, findings_sink=None)


class TestARunPathGraphRecordsFindingsDurably:
    """The construction obligation for the first graph that drives runs.

    Nothing here is a repair: ``build_engine`` already requires the sink and the
    graph invariant already refuses the in-memory one by type. What was missing was
    the *construction* — a run path with the durable sink actually passed — and a
    test that fails if a later change swaps it for either of the two sinks that
    look plausible on that path: ``RecordingFindingsSink``, which is what
    ``AnalysisEngine`` defaults to, and the engine-MCP surface's refusing sink,
    which is the only non-test spelling in the tree and therefore the one a reader
    copies.

    The durability claim is stated the way the in-memory sink cannot satisfy it: a
    second store opened over the same database reads the rows back. The report is
    asserted non-empty first, because an analysis that found nothing would agree
    with a sink that recorded nothing.
    """

    def run_graph(self, tmp_path: Path) -> EngineGraph:
        return build_run_engine(
            model_resolver=models,
            host_state=None,
            session_opener=RecordingOpener(),
            project=PROJECT,
            state_root=tmp_path / "state",
            audit_root=tmp_path / "audit",
            config_root=tmp_path / "config",
        )

    def test_the_run_path_graph_holds_the_durable_sink_over_its_own_store(
        self, tmp_path: Path
    ) -> None:
        graph = self.run_graph(tmp_path)

        sink = graph.analysis.findings_sink
        assert isinstance(sink, StateFindingsSink)
        # Over the graph's OWN store, not a second one opened on the same path: two
        # stores is two connections and two ideas of where a run's rows live.
        assert getattr(sink, "_state") is graph.state

    def test_the_findings_a_run_path_graph_records_survive_a_reopened_store(
        self, tmp_path: Path
    ) -> None:
        graph = self.run_graph(tmp_path)
        ref = analysable_spec(tmp_path)

        report = graph.analysis.analyze(ref, run="run-1")

        assert report.result.findings, "the analyzer found nothing, so nothing was recorded"
        reopened = StateStore(root=graph.state.root)
        stored = reopened.list_analysis_findings(run="run-1")
        assert [record.to_review_row() for record in stored] == list(report.review_rows("run-1"))

    def test_the_run_path_construction_offers_no_sink_to_get_wrong(self) -> None:
        """The parameter is absent, not defaulted: there is nothing to pass."""
        assert "findings_sink" not in inspect.signature(build_run_engine).parameters

    def test_the_marker_is_not_itself_a_sink(self) -> None:
        # If the marker ever grew a ``record`` it could be mistaken for a sink and
        # travel into a graph, which would record nothing while type-checking.
        assert not hasattr(DURABLE_FINDINGS, "record")
        assert not isinstance(DURABLE_FINDINGS, RecordingFindingsSink)


class TestCostAttributionIsDurable:
    """Declared capability spend reaches the run row, not a list in memory."""

    def test_the_registry_attributes_cost_through_the_run_row(self, tmp_path: Path) -> None:
        graph = build(tmp_path)
        assert isinstance(graph.registry.cost_sink, RunCostSink)
        assert not isinstance(graph.registry.cost_sink, RecordingCostSink)


class TestOneRunMachinePerStore:
    """``RunMachine.transition`` stays the only writer of a run's state column."""

    def test_the_review_queue_runs_over_the_graphs_machine(self, tmp_path: Path) -> None:
        graph = build(tmp_path)
        assert getattr(graph.review_queue, "_machine") is graph.machine

    def test_the_orchestrator_runs_over_the_graphs_machine(
        self, tmp_path: Path, ref: SpecRef
    ) -> None:
        graph = build(tmp_path)
        # Created through the graph's machine, which is also the point: the run
        # row the orchestrator's guard reads was written by the one writer.
        record = graph.machine.create(ref, source=SOURCE)
        runner = graph.orchestrator(
            ref,
            record.run_id,
            authority=self.authority(graph),
            worker=worker,
            reviewer=reviewer,
        )
        # orchestrator_for builds its own machine when passed none, which over
        # this store would be a second writer of the state column.
        assert getattr(runner, "_machine") is graph.machine

    def test_the_budget_guard_runs_over_the_same_machine_as_the_runner(
        self, tmp_path: Path, ref: SpecRef
    ) -> None:
        """The other half of the forwarding, which nothing asserted.

        ``orchestrator_for`` passes its machine to the wave runner AND to
        ``guard_for``, whose own ``machine`` parameter defaults to a fresh
        machine. A drop on this half is invisible to the runner assertion above:
        the run still moves, so the halt looks applied.
        """
        graph = build(tmp_path)
        record = graph.machine.create(ref, source=SOURCE)
        runner = graph.orchestrator(
            ref,
            record.run_id,
            authority=self.authority(graph),
            worker=worker,
            reviewer=reviewer,
        )
        guard = getattr(runner, "_guard")
        assert getattr(guard, "_machine") is graph.machine

    def test_a_guard_over_a_second_machine_parks_the_run_with_no_audit_trail(
        self, tmp_path: Path, ref: SpecRef
    ) -> None:
        """What the forwarding assertion is worth: the probe that makes it matter.

        A second machine over the same store still moves the row, so nothing
        obvious breaks — which is exactly why this needs pinning. What it loses is
        the audit log and the host notifier the graph's machine carries: a
        kill-switch stop applied through one records nothing, and "the run halted
        and nobody can say why" is the failure the single-writer rule prevents.
        """
        graph = build(tmp_path)
        through_graph = graph.machine.create(ref, source=SOURCE)
        through_second = graph.machine.create(ref, source=SOURCE)
        for run in (through_graph, through_second):
            graph.machine.transition(ref, run.run_id, RunState.AUTHORING)

        second = RunMachine(graph.state, graph.config, project=graph.project)
        guards = {
            through_graph.run_id: graph.machine,
            through_second.run_id: second,
        }
        for run_id, machine in guards.items():
            guard_for(
                run_id,
                ref,
                state=graph.state,
                config=graph.config,
                project=graph.project,
                audit=graph.audit,
                machine=machine,
            ).halt_for_kill_switch(reason="probe")

        assert graph.machine.state_of(through_second.run_id) is RunState.HALTED_BUDGET
        moved = {
            entry.run
            for entry in graph.audit.read(ref)
            if entry.event == RUN_TRANSITIONED_EVENT
            and (entry.detail or {}).get("to") == RunState.HALTED_BUDGET.value
        }
        assert through_graph.run_id in moved
        assert through_second.run_id not in moved, (
            "the second machine recorded the halt after all; if this ever passes, "
            "the single-writer rule is documentation and this probe is the proof"
        )

    def test_no_collaborator_the_graph_owns_can_be_substituted_per_run(self) -> None:
        """The shared seams are absent from the accessor, not merely defaulted.

        A ``machine=`` parameter here would be a route to the second writer, and
        a notifier parameter would be a route to a run that tells nobody.
        """
        parameters = set(inspect.signature(EngineGraph.orchestrator).parameters)
        assert not parameters & {
            "machine",
            "state",
            "config",
            "audit",
            "notifier",
            "delivery_notifier",
        }

    @staticmethod
    def authority(graph: EngineGraph) -> Any:
        decision = AutonomyDecision(
            level=AutonomyLevel.EXECUTION,
            source=SOURCE,
            spec_type="feature",
            submitter_class="maintainer",
            declared_at=f"sources.{SOURCE}.autonomy.maintainer.feature",
        )
        return resolve_authority(graph.config, decision=decision, project=PROJECT)


class TestSharedCollaborators:
    """One store, one audit log, one notifier — reached from the graph."""

    def test_one_notifier_satisfies_both_of_the_orchestrators_notifier_seams(
        self, tmp_path: Path
    ) -> None:
        graph = build(tmp_path)
        assert isinstance(graph.notifier, HostNotifier)
        # The budget ceiling's shape and the delivery pipeline's shape, on one
        # object: this is why the two parameters take the same notifier.
        assert callable(getattr(graph.notifier, "notify"))
        assert callable(getattr(graph.notifier, "send"))

    def test_the_capability_registry_records_through_the_graphs_audit_log(
        self, tmp_path: Path
    ) -> None:
        """One log, not a second handle: a capability call is auditable evidence.

        The registry's audit seam defaults to ``None``, which records a completed
        capability call nowhere.
        """
        graph = build(tmp_path)
        assert getattr(graph.registry, "_audit") is graph.audit

    def test_the_machine_and_the_audit_log_are_rooted_where_the_state_is(
        self, tmp_path: Path
    ) -> None:
        graph = build(tmp_path)
        assert isinstance(graph.machine, RunMachine)
        assert graph.state.root == tmp_path / "state"
        assert graph.audit.root.parent == tmp_path / "audit"

    def test_the_graph_cannot_be_rewired_after_construction(self, tmp_path: Path) -> None:
        graph = build(tmp_path)
        with pytest.raises(Exception):
            graph.machine = RunMachine(graph.state, graph.config)  # type: ignore[misc]


class TestTheGatePrecedesTheFirstCredit:
    """No starter, and therefore no dispatch, until the prerequisite gate passes.

    The order is the guarantee. A dispatch entry point cannot run without a
    ``RunStarter`` — ``start=`` is required at every one of them — and
    :meth:`EngineGraph.begin_run` is the only way to obtain the engine's, so an
    unmet prerequisite is discovered before the claim, the spec, the intake
    screening and the session rather than at the phase that needed the missing
    thing. These tests assert absences: nothing opened, nothing spent.
    """

    def refusing_config(self, graph: EngineGraph) -> None:
        """Configure a delivery stage whose program this host does not have."""
        graph.config.write(
            {
                "budget": {"run_ceiling_credits": 50.0},
                "workflow": {"stages": {"submit": [["definitely-not-on-path", "--push"]]}},
                "projects": {PROJECT: {"path": f"/w/{PROJECT}", "base_branch": "main"}},
            },
            surface=DASHBOARD_SURFACE,
        )

    def test_a_run_whose_delivery_program_is_missing_never_reaches_a_session(
        self, tmp_path: Path, ref: SpecRef
    ) -> None:
        opener = RecordingOpener()
        graph = build(tmp_path, session_opener=opener)
        self.refusing_config(graph)

        with pytest.raises(RunPrevented) as caught:
            graph.begin_run(ref, AutonomyLevel.DELIVERY, which=lambda program: None)

        # The absences, not a small number: no session was opened, so no turn ran
        # and nothing was attributed to a run.
        assert opener.requests == []
        assert graph.state.list_runs() == []
        assert caught.value.refusal.unmet

    def test_the_refusal_is_recorded_before_anything_else_happens(
        self, tmp_path: Path, ref: SpecRef
    ) -> None:
        """The reason survives: a prevented run that logged nothing is unusable."""
        graph = build(tmp_path)
        self.refusing_config(graph)
        with pytest.raises(RunPrevented):
            graph.begin_run(ref, AutonomyLevel.DELIVERY, which=lambda program: None)
        events = [entry.event for entry in graph.audit.read(ref)]
        assert AUDIT_PREREQUISITE_UNMET in events

    def test_an_authoring_run_is_not_refused_for_a_delivery_program(
        self, tmp_path: Path, ref: SpecRef
    ) -> None:
        """The gate refuses only what the run's rung reaches.

        Without this, the check would be indistinguishable from one that refuses
        everything, and every assertion above would pass for the wrong reason.
        """
        graph = build(tmp_path)
        self.refusing_config(graph)
        starter = graph.begin_run(ref, AutonomyLevel.AUTHORING, which=lambda program: None)
        assert starter is not None

    def test_the_starter_is_the_seeder_the_dispatcher_accepts(self, tmp_path: Path) -> None:
        """What ``begin_run`` returns is what ``start=`` takes: one seeder."""
        graph = build(tmp_path)
        ref_ = SpecRef.of(str(tmp_path / "p"), "s")
        starter = graph.begin_run(ref_, AutonomyLevel.AUTHORING)
        assert isinstance(starter, SessionSeeder)
        assert starter is getattr(graph, "_seeder")
        assert callable(starter)

    def test_the_seeder_is_not_reachable_without_passing_the_gate(self) -> None:
        """A public seeder would be an equivalent second path to ``start=``.

        ``begin_run`` would then be advice rather than a gate: a surface could
        hand the seeder straight to a dispatch entry point.
        """
        public = {name for name in vars(EngineGraph).get("__annotations__", {})}
        public |= {f.name for f in dataclasses.fields(EngineGraph) if not f.name.startswith("_")}
        assert "seeder" not in public
        assert not hasattr(build_engine, "seeder")

    def test_the_starter_the_gate_hands_back_really_does_open_a_session(
        self, tmp_path: Path, ref: SpecRef, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The positive control for the absence asserted above.

        Without this, "the opener was never called" could be true because the
        seeder in the graph is inert. Here the same object, obtained through a
        gate that passed, opens a session and stamps it — so the empty opener in
        the refusal case is the gate's doing and nothing else's.
        """
        monkeypatch.setattr(seeder_module, "session_posture", lambda app: "auto")
        monkeypatch.setattr(seeder_module, "verify_session_posture", lambda app, applied: None)
        opener = RecordingOpener()
        graph = build(tmp_path, session_opener=opener)
        starter = graph.begin_run(ref, AutonomyLevel.AUTHORING)

        seed = SeedForTest(run_id="run-1", ref=ref, project=PROJECT, working_tree=tmp_path)
        # Called the way a dispatcher calls a RunStarter. The seam's annotation
        # names the watcher's concrete RunSeed while the seeder reads only the
        # structural SeededRun this stands in for, so the cast is the nominal
        # gap, not a shortcut around a real mismatch.
        starter(cast(Any, seed))

        assert [request.run_id for request in opener.requests] == ["run-1"]
        assert opener.requests[0].posture == "auto"
        # Attributed, which is what makes the session's turns visible to the
        # ceiling: an unstamped session is spend nothing counts.
        assert RunAccounting(graph.state).sessions_for("run-1") == ("chat-1",)

    def test_a_refused_run_leaves_the_seeder_with_nothing_to_stamp(
        self, tmp_path: Path, ref: SpecRef
    ) -> None:
        """The other half of the pair: same graph, same seeder, gate refuses."""
        opener = RecordingOpener()
        graph = build(tmp_path, session_opener=opener)
        self.refusing_config(graph)
        with pytest.raises(RunPrevented):
            graph.begin_run(ref, AutonomyLevel.DELIVERY, which=lambda program: None)
        assert opener.requests == []
        assert RunAccounting(graph.state).sessions_for("run-1") == ()

    def test_the_same_gate_can_be_asked_without_starting_anything(
        self, tmp_path: Path, ref: SpecRef
    ) -> None:
        """Doctor-style reporting reads the refusal; it gets no starter with it."""
        graph = build(tmp_path)
        self.refusing_config(graph)
        refusal = graph.prerequisite_refusal(
            ref, AutonomyLevel.DELIVERY, which=lambda program: None
        )
        assert refusal is not None
        assert refusal.unmet
        assert graph.prerequisite_refusal(ref, AutonomyLevel.AUTHORING) is None


class TestAParkedRunAnnouncesItself:
    """The awaiting-review notice has a caller, and it is the one state writer.

    ``notify_awaiting_review`` passed its own tests while nothing called it, so
    these tests are about the *call*: a run parked through the graph's machine
    reaches the seeder's announcement, with the graph's project scope.
    """

    def test_the_machine_announces_through_the_graphs_seeder(self, tmp_path: Path) -> None:
        graph = build(tmp_path)
        announcer = getattr(graph.machine, "_review_announcer")
        assert announcer is not None
        assert getattr(announcer, "__self__") is getattr(graph, "_seeder")
        assert announcer.__func__ is SessionSeeder.notify_awaiting_review

    def test_parking_a_run_reaches_the_notification_path(
        self, tmp_path: Path, ref: SpecRef
    ) -> None:
        """End to end through the graph: the notice is attempted and recorded.

        There is no bus in this process, so the notifier cannot deliver — what is
        asserted is that the announcement ran and its outcome was written to the
        run's audit log, which is what makes a lost notice diagnosable.
        """
        graph = build(tmp_path)
        record = graph.machine.create(ref, source=SOURCE)
        graph.machine.transition(ref, record.run_id, RunState.AUTHORING)
        graph.machine.transition(ref, record.run_id, RunState.AWAITING_REVIEW)
        events = [entry.event for entry in graph.audit.read(ref)]
        assert AWAITING_REVIEW_EVENT in events or AWAITING_REVIEW_NOTIFY_FAILED_EVENT in events

    def test_replace_cannot_leave_a_machine_that_announces_to_nobody(self, tmp_path: Path) -> None:
        graph = build(tmp_path)
        silent = RunMachine(graph.state, graph.config, project=graph.project, audit=graph.audit)
        with pytest.raises(IncompleteEngineGraph) as caught:
            dataclasses.replace(graph, machine=silent)
        assert "tell nobody" in str(caught.value)


class TestAPartialGraphIsUnconstructable:
    """The module claims completeness "by construction"; this is what enforces it.

    Both spellings below type-check and both were demonstrated to produce a graph
    with no builtins registered — one by calling the dataclass, one by the
    ``replace()`` an ordinary refactor of a frozen dataclass reaches for. The
    field types cannot tell them apart from a built graph, so the invariant is
    checked on the instance.
    """

    def bare_registry(self, graph: EngineGraph) -> CapabilityRegistry:
        """A registry as valid as the graph's, minus the builtin registration."""
        return CapabilityRegistry(
            graph.config,
            project=graph.project,
            audit=graph.audit,
            cost_sink=RunCostSink(graph.state),
        )

    def test_direct_construction_with_an_unregistered_registry_is_refused(
        self, tmp_path: Path
    ) -> None:
        graph = build(tmp_path)
        with pytest.raises(IncompleteEngineGraph) as caught:
            EngineGraph(
                project=graph.project,
                state=graph.state,
                config=graph.config,
                audit=graph.audit,
                registry=self.bare_registry(graph),
                analysis=graph.analysis,
                notifier=graph.notifier,
                machine=graph.machine,
                review_queue=graph.review_queue,
                _seeder=getattr(graph, "_seeder"),
            )
        assert "authoring" in str(caught.value)

    def test_replace_cannot_swap_in_an_unregistered_registry(self, tmp_path: Path) -> None:
        """``replace()`` re-runs ``__post_init__``, which is why it is caught."""
        graph = build(tmp_path)
        with pytest.raises(IncompleteEngineGraph):
            dataclasses.replace(graph, registry=self.bare_registry(graph))

    def test_replace_cannot_swap_in_a_registry_that_attributes_cost_to_memory(
        self, tmp_path: Path
    ) -> None:
        graph = build(tmp_path)
        registry = CapabilityRegistry(graph.config, project=graph.project, audit=graph.audit)
        register_builtins(registry, model_resolver=models)
        with pytest.raises(IncompleteEngineGraph) as caught:
            dataclasses.replace(graph, registry=registry)
        assert "durable run row" in str(caught.value)

    def test_replace_cannot_swap_in_a_second_writer_of_the_state_column(
        self, tmp_path: Path
    ) -> None:
        """A second machine over the same store is the invariant's whole point."""
        graph = build(tmp_path)
        second = RunMachine(graph.state, graph.config, project=graph.project, audit=graph.audit)
        with pytest.raises(IncompleteEngineGraph) as caught:
            dataclasses.replace(graph, machine=second)
        assert "second writer" in str(caught.value)

    def test_replace_cannot_move_the_machine_to_another_store(self, tmp_path: Path) -> None:
        graph = build(tmp_path)
        elsewhere = StateStore(root=tmp_path / "elsewhere")
        with pytest.raises(IncompleteEngineGraph):
            dataclasses.replace(
                graph,
                machine=RunMachine(elsewhere, graph.config, audit=graph.audit),
            )

    def test_replace_cannot_route_findings_back_into_memory(self, tmp_path: Path) -> None:
        graph = build(tmp_path)
        with pytest.raises(IncompleteEngineGraph) as caught:
            dataclasses.replace(
                graph,
                analysis=AnalysisEngine(graph.registry, findings_sink=RecordingFindingsSink()),
            )
        assert "memory" in str(caught.value)

    def test_replace_cannot_detach_the_audit_log_from_the_registry(self, tmp_path: Path) -> None:
        """A capability call recorded nowhere is the audit seam's default."""
        graph = build(tmp_path)
        with pytest.raises(IncompleteEngineGraph) as caught:
            dataclasses.replace(graph, audit=AuditLog(root=tmp_path / "second-audit"))
        assert "audit log" in str(caught.value)

    def test_replace_cannot_leave_the_graph_scoped_to_another_project(
        self, tmp_path: Path
    ) -> None:
        """A graph's project has to agree with the one its collaborators baked in.

        ``replace(graph, project=...)`` rebuilds nothing: the machine, registry and
        notifier keep the project they were constructed with. The result gates and
        notifies under one project while auditing and metering under another, and
        because every setting is resolved per project that is two effective
        configurations for one run.
        """
        graph = build(tmp_path)
        with pytest.raises(IncompleteEngineGraph) as caught:
            dataclasses.replace(graph, project="other")
        assert "not this graph's" in str(caught.value)

    def test_the_built_graph_passes_its_own_invariant(self, tmp_path: Path) -> None:
        """The check is not vacuous in the other direction: replace() still works.

        A ``replace`` that changes nothing the invariant covers has to succeed, or
        the check would be a ban on ``replace`` rather than on a partial graph. The
        control passes the values already in place — previously it passed a
        different ``project``, which is not a no-op at all but the incoherent graph
        the test above now pins.
        """
        graph = build(tmp_path)
        unchanged = dataclasses.replace(graph, project=graph.project, state=graph.state)
        assert unchanged.registry is graph.registry
        assert unchanged.machine is graph.machine


def worker(*, task: str, dispatch: Dispatch, context: RunContext) -> TaskResult:
    return TaskResult(ok=True)


def reviewer(*, task: str, dispatch: Dispatch, context: RunContext) -> ReviewVerdict:
    return ReviewVerdict(approved=True)
