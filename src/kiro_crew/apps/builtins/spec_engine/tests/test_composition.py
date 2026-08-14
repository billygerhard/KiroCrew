"""The composition root: what a surface gets, and what it cannot get.

Every claim here is about the *construction*, not about the libraries being
constructed. Each library in the graph already has its own suite and passes it
while nothing builds it — that is the defect this module exists to remove — so
these tests are written to fail when a construction is deleted from
:func:`build_engine` even though the library it constructs is untouched.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.apps.builtins.spec_engine.engine.analysis import (
    AnalysisReport,
    RecordingFindingsSink,
)
from kiro_crew.apps.builtins.spec_engine.engine.autonomy import AutonomyDecision, AutonomyLevel
from kiro_crew.apps.builtins.spec_engine.engine.budget.ledger import RunCostSink
from kiro_crew.apps.builtins.spec_engine.engine.capabilities.builtins import (
    AUTHORING_PROVIDER,
    IMPLEMENTATION_PROVIDER,
    MODEL_CATALOG_PROVIDER,
    REVIEW_PROVIDER,
)
from kiro_crew.apps.builtins.spec_engine.engine.capabilities.contracts import ProviderNature
from kiro_crew.apps.builtins.spec_engine.engine.capabilities.registry import RecordingCostSink
from kiro_crew.apps.builtins.spec_engine.engine.composition import EngineGraph, build_engine
from kiro_crew.apps.builtins.spec_engine.engine.delivery import resolve_authority
from kiro_crew.apps.builtins.spec_engine.engine.notify.routing import HostNotifier
from kiro_crew.apps.builtins.spec_engine.engine.orchestrator import (
    ReviewVerdict,
    RunContext,
    TaskResult,
)
from kiro_crew.apps.builtins.spec_engine.engine.roles import Dispatch
from kiro_crew.apps.builtins.spec_engine.engine.runs import RunMachine
from kiro_crew.apps.builtins.spec_engine.engine.state import SpecRef

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


def build(tmp_path: Path, **overrides: Any) -> EngineGraph:
    """A graph on temporary roots, with every required seam supplied."""
    kwargs: dict[str, Any] = {
        "model_resolver": models,
        "findings_sink": CountingFindingsSink(),
        "host_state": None,
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
    """The seam that has no durable implementation yet, kept from defaulting.

    The durable ``analysis_findings`` table is not built, so the graph cannot
    hand :class:`AnalysisEngine` a durable sink today. What it can do is refuse
    to choose one silently, so that when the table lands there is exactly one
    place to pass it.
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


def worker(*, task: str, dispatch: Dispatch, context: RunContext) -> TaskResult:
    return TaskResult(ok=True)


def reviewer(*, task: str, dispatch: Dispatch, context: RunContext) -> ReviewVerdict:
    return ReviewVerdict(approved=True)
