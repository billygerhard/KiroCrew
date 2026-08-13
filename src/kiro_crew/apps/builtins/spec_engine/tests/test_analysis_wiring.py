"""Analysis capability wiring: the request, the keying, and the honest fallback.

The claims under test are the ones specific to analysis, on top of what the
capability registry already guarantees and its own suite already proves. The
request a provider receives carries where the documents are, what type the spec
is, and which format version it speaks. Findings are keyed to the criteria the
document actually declares, so a provider cannot conjure a criterion by naming
one. An unavailable, slow, or wrong provider degrades to the bundled analyzer —
not to the shipped no-coverage default — which is the wiring this module owns.
And a provider's declared cost reaches the run's budget through the ledger's own
sink, because the request carries the run to charge.

The transports are substituted here: the process boundary is tested with real
children in :mod:`test_capability_transports`, and what matters at this layer is
what the analysis wiring does with a request, a response, and a failure.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Mapping

import pytest

from kiro_crew.apps.builtins.spec_engine.engine.analysis import (
    ANALYSIS_CAPABILITY,
    AnalysisEngine,
    SpecTypeUnrecorded,
    declared_criteria,
    route_findings,
)
from kiro_crew.apps.builtins.spec_engine.engine.audit import AuditLog
from kiro_crew.apps.builtins.spec_engine.engine.budget.ledger import RunCostSink
from kiro_crew.apps.builtins.spec_engine.engine.capabilities import (
    AUDIT_EVENT_CAPABILITY,
    FINDING_PROVIDER_TIMEOUT,
    FINDING_PROVIDER_UNAVAILABLE,
    FINDING_RESPONSE_INVALID,
    NATIVE_FORMAT_VERSION,
    TRANSPORT_COMMAND,
    CapabilityRegistry,
    CapabilityRequest,
    ProviderKind,
    ProviderNature,
    TransportFailure,
)
from kiro_crew.apps.builtins.spec_engine.engine.config import DASHBOARD_SURFACE, ConfigStore
from kiro_crew.apps.builtins.spec_engine.engine.local_analyzer import (
    DEPTH_STRUCTURAL,
)
from kiro_crew.apps.builtins.spec_engine.engine.local_analyzer import PROVIDER_NAME as ANALYZER_NAME
from kiro_crew.apps.builtins.spec_engine.engine.state import SpecRef, StateStore

from .test_capability_schemas import response_payload

# --- fixtures and helpers --------------------------------------------------


def _requirements(criteria: Iterable[str]) -> str:
    """A format-shaped requirements document declaring criteria 1.1, 1.2, ...."""
    lines = [
        "# Requirements Document",
        "",
        "## Introduction",
        "",
        "One requirement, written so its criteria parse.",
        "",
        "## Requirements",
        "",
        "### Requirement 1: The case",
        "",
        "**User Story:** As a reader, I want the case stated, so that it is testable.",
        "",
        "#### Acceptance Criteria",
        "",
    ]
    lines += [f"{number}. {text}" for number, text in enumerate(criteria, start=1)]
    return "\n".join(lines) + "\n"


def author_spec(spec_dir: Path, *, spec_type: str | None = "feature", design: bool = True) -> None:
    """Write a spec directory whose requirements declare two criteria."""
    spec_dir.mkdir(parents=True, exist_ok=True)
    (spec_dir / "requirements.md").write_text(
        _requirements(["THE system SHALL do the first thing.", "THE system SHALL do the second."]),
        encoding="utf-8",
    )
    if design:
        (spec_dir / "design.md").write_text("# Design Document\n\n## Overview\n\nx\n", "utf-8")
    (spec_dir / "tasks.md").write_text("# Implementation Plan\n\n## Tasks\n\n- [ ] 1. x\n", "utf-8")
    sidecar: dict[str, Any] = {"specId": spec_dir.name}
    if spec_type is not None:
        sidecar["specType"] = spec_type
    (spec_dir / ".config.kiro").write_text(json.dumps(sidecar), encoding="utf-8")


@pytest.fixture()
def ref(tmp_path: Path) -> SpecRef:
    project = tmp_path / "project"
    author_spec(project / ".kiro" / "specs" / "example")
    return SpecRef.of(project, "example")


@pytest.fixture()
def config_store(tmp_path: Path) -> ConfigStore:
    return ConfigStore(tmp_path / "config")


@pytest.fixture()
def state_store(tmp_path: Path) -> StateStore:
    return StateStore(root=tmp_path / "state")


def bind_analysis(store: ConfigStore, **binding: Any) -> None:
    store.write({"capabilities": {ANALYSIS_CAPABILITY: binding}}, surface=DASHBOARD_SURFACE)


_UNSET = object()


class StubTransport:
    """A transport whose one call answers with a payload or raises a failure."""

    def __init__(
        self,
        transport: str = TRANSPORT_COMMAND,
        *,
        payload: Any = _UNSET,
        failure: Exception | None = None,
    ) -> None:
        self._transport = transport
        self._payload = payload
        self._failure = failure
        self.calls: list[CapabilityRequest] = []

    @property
    def transport(self) -> str:
        return self._transport

    def invoke(self, request: CapabilityRequest, *, timeout_s: int) -> Mapping[str, Any]:
        self.calls.append(request)
        if self._failure is not None:
            raise self._failure
        assert self._payload is not _UNSET, "stub given neither a payload nor a failure"
        return self._payload  # type: ignore[no-any-return]


def registry(
    config_store: ConfigStore,
    *,
    transport: StubTransport | None = None,
    **kwargs: Any,
) -> CapabilityRegistry:
    transports = {transport.transport: transport} if transport is not None else None
    return CapabilityRegistry(config_store, transports=transports, **kwargs)


def analysis_payload(**overrides: Any) -> dict[str, Any]:
    return response_payload("analysis", **overrides)


# --- the request -----------------------------------------------------------


class TestRequestCarriesWhatAProviderNeeds:
    def test_locations_spec_type_and_format_version_are_carried(
        self, ref: SpecRef, config_store: ConfigStore
    ) -> None:
        request = AnalysisEngine(registry(config_store)).build_request(ref, run="run-1")
        assert request.capability == ANALYSIS_CAPABILITY
        assert request.spec_type == "feature"
        assert request.format_version == NATIVE_FORMAT_VERSION
        assert request.run == "run-1"
        located = {artifact.kind: artifact for artifact in request.artifacts}
        assert set(located) == {"requirements", "design", "tasks", "config"}
        assert located["requirements"].path.endswith("/example/requirements.md")
        # A revision is carried so a finding can be checked against the bytes it
        # was about; it is the content hash, not an empty placeholder.
        assert located["requirements"].revision.startswith("sha256:")

    def test_a_missing_document_is_left_out_rather_than_pointed_at(self, tmp_path: Path) -> None:
        project = tmp_path / "project"
        spec_dir = project / ".kiro" / "specs" / "example"
        author_spec(spec_dir, design=False)
        ref = SpecRef.of(project, "example")
        request = AnalysisEngine(registry(ConfigStore(tmp_path / "config"))).build_request(ref)
        assert "design" not in request.artifact_kinds
        assert "requirements" in request.artifact_kinds

    def test_an_unrecorded_spec_type_refuses_to_build_a_request(self, tmp_path: Path) -> None:
        project = tmp_path / "project"
        spec_dir = project / ".kiro" / "specs" / "typeless"
        author_spec(spec_dir, spec_type=None)
        ref = SpecRef.of(project, "typeless")
        engine = AnalysisEngine(registry(ConfigStore(tmp_path / "config")))
        with pytest.raises(SpecTypeUnrecorded):
            engine.build_request(ref)


# --- keying findings to criteria -------------------------------------------


class TestFindingsAreKeyedToCriteria:
    def test_a_finding_is_keyed_under_the_criterion_it_names(
        self, ref: SpecRef, config_store: ConfigStore
    ) -> None:
        bind_analysis(config_store, transport=TRANSPORT_COMMAND, command=["analyzer"])
        transport = StubTransport(
            payload=analysis_payload(
                findings=[
                    {
                        "kind": "ambiguity",
                        "severity": "warning",
                        "message": "criterion 1.1 is unclear",
                        "refs": ["1.1"],
                    }
                ]
            )
        )
        report = AnalysisEngine(registry(config_store, transport=transport)).analyze(ref)
        assert not report.degraded
        assert set(report.by_criterion) == {"1.1"}
        assert report.by_criterion["1.1"][0].kind == "ambiguity"
        assert report.unkeyed == ()

    def test_a_finding_naming_no_real_criterion_is_unkeyed_not_forged(
        self, ref: SpecRef, config_store: ConfigStore
    ) -> None:
        # The document declares 1.1 and 1.2 only. A finding that names 9.9 must
        # not create a 9.9 key: provider references are attacker-controlled, and
        # a criterion the document does not declare cannot be conjured into the
        # routing a reviewer reads.
        bind_analysis(config_store, transport=TRANSPORT_COMMAND, command=["analyzer"])
        transport = StubTransport(
            payload=analysis_payload(
                findings=[
                    {
                        "kind": "invention",
                        "severity": "error",
                        "message": "criterion 9.9 has a problem",
                        "refs": ["9.9"],
                    }
                ]
            )
        )
        report = AnalysisEngine(registry(config_store, transport=transport)).analyze(ref)
        assert report.by_criterion == {}
        assert len(report.unkeyed) == 1
        assert report.unkeyed[0].kind == "invention"

    def test_the_keys_a_surface_sees_are_engine_identifiers_only(
        self, ref: SpecRef, config_store: ConfigStore
    ) -> None:
        bind_analysis(config_store, transport=TRANSPORT_COMMAND, command=["analyzer"])
        transport = StubTransport(
            payload=analysis_payload(
                findings=[
                    {"kind": "a", "severity": "info", "message": "on 1.2", "refs": ["1.2"]},
                    {"kind": "b", "severity": "info", "message": "on 1.1", "refs": ["1.1"]},
                ]
            )
        )
        report = AnalysisEngine(registry(config_store, transport=transport)).analyze(ref)
        items = report.to_review_items()
        # Sorted by criterion, and every key is one the document declares.
        assert [item["criterion"] for item in items] == ["1.1", "1.2"]
        assert declared_criteria(ref) == {"1.1", "1.2"}

    def test_untrusted_identifier_fields_reach_the_surface_sanitized(
        self, ref: SpecRef, config_store: ConfigStore
    ) -> None:
        # The prose of a message is allowed its line breaks — it is prose. The
        # identifier-shaped fields are not: a carriage return in a finding kind
        # is how a value rewrites the line printed before it in a terminal
        # reading the queue, so the render strips it. And the item's key is the
        # engine criterion identifier regardless of anything the finding says.
        bind_analysis(config_store, transport=TRANSPORT_COMMAND, command=["analyzer"])
        transport = StubTransport(
            payload=analysis_payload(
                findings=[
                    {
                        "kind": "kind\rforged",
                        "severity": "info",
                        "message": "prose keeps its own newlines",
                        "refs": ["1.1"],
                    }
                ]
            )
        )
        report = AnalysisEngine(registry(config_store, transport=transport)).analyze(ref)
        item = report.to_review_items()[0]
        assert item["criterion"] == "1.1"
        assert "\r" not in item["findings"][0]["kind"]


# --- the honest fallback ---------------------------------------------------


class TestUnusableProviderDegradesToTheAnalyzer:
    def test_nothing_configured_answers_from_the_bundled_analyzer(
        self, ref: SpecRef, config_store: ConfigStore
    ) -> None:
        report = AnalysisEngine(registry(config_store)).analyze(ref)
        assert not report.degraded
        assert report.provider.name == ANALYZER_NAME
        assert report.provider.nature is ProviderNature.DETERMINISTIC
        assert report.result.response.result == {"depth": DEPTH_STRUCTURAL}

    def test_an_unavailable_provider_falls_back_with_a_degraded_reason(
        self, ref: SpecRef, config_store: ConfigStore
    ) -> None:
        bind_analysis(config_store, transport=TRANSPORT_COMMAND, command=["analyzer"])
        transport = StubTransport(
            failure=TransportFailure(FINDING_PROVIDER_UNAVAILABLE, "child would not start")
        )
        report = AnalysisEngine(registry(config_store, transport=transport)).analyze(ref)
        assert report.degraded
        assert report.degradation is not None
        assert report.degradation.finding_id == FINDING_PROVIDER_UNAVAILABLE
        assert report.degradation.reason
        # The fallback is the analyzer, not the shipped no-coverage default: it
        # reports structural depth and its blind spots, which the default never
        # does.
        assert report.provider.name == ANALYZER_NAME
        assert report.result.response.result == {"depth": DEPTH_STRUCTURAL}

    def test_a_schema_invalid_response_falls_back_to_the_analyzer(
        self, ref: SpecRef, config_store: ConfigStore
    ) -> None:
        bind_analysis(config_store, transport=TRANSPORT_COMMAND, command=["analyzer"])
        # A response missing coverage and result is not a valid analysis
        # response, so it must not be accepted — the invalid case must differ
        # from the happy path in the field that matters.
        transport = StubTransport(payload={"schema_version": 1, "capability": "analysis"})
        report = AnalysisEngine(registry(config_store, transport=transport)).analyze(ref)
        assert report.degraded
        assert report.degradation is not None
        assert report.degradation.finding_id == FINDING_RESPONSE_INVALID
        assert report.provider.name == ANALYZER_NAME

    def test_a_timed_out_provider_falls_back_with_the_timeout_reason(
        self, ref: SpecRef, config_store: ConfigStore
    ) -> None:
        bind_analysis(config_store, transport=TRANSPORT_COMMAND, command=["analyzer"])
        transport = StubTransport(
            failure=TransportFailure(FINDING_PROVIDER_TIMEOUT, "exceeded its deadline")
        )
        report = AnalysisEngine(registry(config_store, transport=transport)).analyze(ref)
        assert report.degraded
        assert report.degradation is not None
        assert report.degradation.finding_id == FINDING_PROVIDER_TIMEOUT
        assert report.provider.name == ANALYZER_NAME


# --- coverage, cost, and the audit record ----------------------------------


class TestCoverageCostAndAudit:
    def test_declared_skipped_coverage_is_surfaced(
        self, ref: SpecRef, config_store: ConfigStore
    ) -> None:
        bind_analysis(config_store, transport=TRANSPORT_COMMAND, command=["analyzer"])
        transport = StubTransport(
            payload=analysis_payload(
                coverage={
                    "processed": ["requirements"],
                    "skipped": [{"item": "design", "reason": "not read this pass"}],
                }
            )
        )
        report = AnalysisEngine(registry(config_store, transport=transport)).analyze(ref)
        skipped = {item.item: item.reason.for_display() for item in report.skipped}
        assert skipped == {"design": "not read this pass"}

    def test_a_declared_cost_is_attributed_to_the_run(
        self, ref: SpecRef, config_store: ConfigStore, state_store: StateStore
    ) -> None:
        state_store.register_spec(ref, spec_type="feature")
        state_store.create_run("run-1", ref, state="authoring")
        bind_analysis(config_store, transport=TRANSPORT_COMMAND, command=["analyzer"])
        transport = StubTransport(payload=analysis_payload(cost={"credits": 3.5}))
        sink = RunCostSink(state_store)
        engine = AnalysisEngine(registry(config_store, transport=transport, cost_sink=sink))
        report = engine.analyze(ref, run="run-1")
        assert report.cost_credits == pytest.approx(3.5)
        assert sink.total_for("run-1") == pytest.approx(3.5)

    def test_the_call_is_recorded_with_provider_and_coverage(
        self, ref: SpecRef, config_store: ConfigStore, tmp_path: Path
    ) -> None:
        audit = AuditLog(tmp_path / "audit")
        bind_analysis(config_store, transport=TRANSPORT_COMMAND, command=["analyzer"])
        transport = StubTransport(payload=analysis_payload(provider={"name": "candidate"}))
        engine = AnalysisEngine(registry(config_store, transport=transport, audit=audit))
        engine.analyze(ref, run="run-1", initiator="tester")
        events = [e for e in audit.read(ref) if e.event == AUDIT_EVENT_CAPABILITY]
        assert len(events) == 1
        detail = events[0].detail or {}
        assert detail["capability"] == ANALYSIS_CAPABILITY
        assert detail["transport"] == TRANSPORT_COMMAND
        assert detail["provider"]["kind"] == ProviderKind.EXTERNAL.value
        assert "coverage" in detail


# --- the pure routing function ---------------------------------------------


class TestRouteFindings:
    def test_routing_with_no_known_criteria_leaves_everything_unkeyed(
        self, ref: SpecRef, config_store: ConfigStore
    ) -> None:
        # route_findings is the mechanism analyze() constructs; exercising it
        # directly pins that an empty criteria set keys nothing rather than
        # raising, which is the state of a spec with no requirements yet.
        bind_analysis(config_store, transport=TRANSPORT_COMMAND, command=["analyzer"])
        transport = StubTransport(
            payload=analysis_payload(
                findings=[{"kind": "k", "severity": "info", "message": "m", "refs": ["1.1"]}]
            )
        )
        result = registry(config_store, transport=transport).invoke(
            AnalysisEngine(registry(config_store, transport=transport)).build_request(ref)
        )
        report = route_findings(result, frozenset())
        assert report.by_criterion == {}
        assert len(report.unkeyed) == 1
