"""The review binding: consuming the analysis report, not producing a second one.

Task 17.4 built the routing that keys a provider's findings to real acceptance
criteria; this is the production consumer of it. The claims under test: a routed
report reaches a findings sink on every analysis rather than being a value the
caller may forget; the persistable rows carry engine identifiers and
display-contract prose, so a crafted finding can neither overwrite the line above
it in a terminal nor forge a criterion; and the additive-only rule the review
binding relies on holds — a supplementary provider adds findings beside the
engine's and cannot suppress, downgrade, or answer a gate.

The persistence itself has no durable home yet: the state store and the
Review_Queue projection own the table and the human-facing surface, and those
files belong to other tasks this wave. The default sink records in memory so a
routed report is never dropped, and the row shape here is exactly what a durable
sink would write.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from kiro_crew.apps.builtins.spec_engine.engine.analysis import (
    AnalysisEngine,
    RecordingFindingsSink,
)
from kiro_crew.apps.builtins.spec_engine.engine.capabilities import (
    TRANSPORT_COMMAND,
    CapabilityRegistry,
    CapabilityRequest,
    CapabilityResponse,
    CapabilityResult,
    FindingSeverity,
    ProviderFinding,
    Untrusted,
    external_identity,
    supplement,
)
from kiro_crew.apps.builtins.spec_engine.engine.config import DASHBOARD_SURFACE, ConfigStore
from kiro_crew.apps.builtins.spec_engine.engine.findings import (
    Location,
    Severity,
    Violation,
    build_report,
)
from kiro_crew.apps.builtins.spec_engine.engine.state import SpecRef

from .test_analysis_wiring import StubTransport, author_spec
from .test_capability_schemas import response_payload


@pytest.fixture()
def ref(tmp_path: Path) -> SpecRef:
    project = tmp_path / "project"
    author_spec(project / ".kiro" / "specs" / "example")
    return SpecRef.of(project, "example")


@pytest.fixture()
def config_store(tmp_path: Path) -> ConfigStore:
    return ConfigStore(tmp_path / "config")


def bind_analysis(store: ConfigStore) -> None:
    store.write(
        {"capabilities": {"analysis": {"transport": TRANSPORT_COMMAND, "command": ["analyzer"]}}},
        surface=DASHBOARD_SURFACE,
    )


def engine_with(store: ConfigStore, transport: StubTransport, **kwargs: Any) -> AnalysisEngine:
    registry = CapabilityRegistry(store, transports={transport.transport: transport})
    return AnalysisEngine(registry, **kwargs)


# --- the report reaches a sink ---------------------------------------------


class TestTheReportReachesASink:
    def test_analyze_records_the_routed_report_to_the_default_sink(
        self, ref: SpecRef, config_store: ConfigStore
    ) -> None:
        bind_analysis(config_store)
        transport = StubTransport(
            payload=response_payload(
                "analysis",
                findings=[
                    {"kind": "ambiguity", "severity": "warning", "message": "1.1 unclear",
                     "refs": ["1.1"]},
                ],
            )
        )
        engine = engine_with(config_store, transport)
        report = engine.analyze(ref, run="run-1")
        sink = engine.findings_sink
        assert isinstance(sink, RecordingFindingsSink)
        rows = sink.rows_for("run-1")
        # The rows the sink stored are exactly the report's rows, plus the spec
        # they belong to. Deleting the record() call in analyze() drops these.
        assert len(rows) == len(report.review_rows("run-1")) == 1
        assert rows[0]["project"] == ref.project
        assert rows[0]["spec"] == ref.name
        assert rows[0]["criterion"] == "1.1"
        assert rows[0]["keyed"] is True

    def test_a_supplied_sink_is_the_one_that_receives_the_report(
        self, ref: SpecRef, config_store: ConfigStore
    ) -> None:
        bind_analysis(config_store)
        transport = StubTransport(payload=response_payload("analysis"))
        sink = RecordingFindingsSink()
        engine = engine_with(config_store, transport, findings_sink=sink)
        engine.analyze(ref, run="run-7")
        # No findings in this payload, so no rows — but the call still reached
        # the supplied sink, which is the seam a durable store plugs into.
        assert engine.findings_sink is sink
        assert sink.rows_for("run-7") == ()


# --- the rows key to criteria and escape prose -----------------------------


class TestRowsKeyAndEscape:
    def test_a_finding_naming_no_real_criterion_is_recorded_unkeyed(
        self, ref: SpecRef, config_store: ConfigStore
    ) -> None:
        bind_analysis(config_store)
        transport = StubTransport(
            payload=response_payload(
                "analysis",
                findings=[
                    {"kind": "invention", "severity": "error", "message": "9.9 broken",
                     "refs": ["9.9"]},
                ],
            )
        )
        report = engine_with(config_store, transport).analyze(ref, run="run-1")
        rows = report.review_rows("run-1")
        assert len(rows) == 1
        # A finding the provider could not key to a declared criterion is kept,
        # not dropped, and it does not forge a criterion the document lacks.
        assert rows[0]["criterion"] is None
        assert rows[0]["keyed"] is False

    def test_prose_and_identifier_fields_are_escaped_in_the_stored_row(
        self, ref: SpecRef, config_store: ConfigStore
    ) -> None:
        # The display contract, at the surface that persists a finding. A lone
        # carriage return in the message would return the cursor and overwrite
        # the engine-authored line above it in a terminal reading the row; the
        # kind is identifier-shaped and keeps no line breaks at all. Prose keeps
        # its legitimate newline.
        bind_analysis(config_store)
        transport = StubTransport(
            payload=response_payload(
                "analysis",
                findings=[
                    {
                        "kind": "kind\rforged",
                        "severity": "warning",
                        "message": "line one\rline two\nline three\x07",
                        "refs": ["1.1"],
                    },
                ],
            )
        )
        report = engine_with(config_store, transport).analyze(ref, run="run-1")
        finding = report.review_rows("run-1")[0]["finding"]
        assert "\r" not in finding["kind"]  # identifier: no breaks survive
        message = finding["message"]
        assert "\r" not in message  # the overwrite character is gone
        assert "\x07" not in message  # control character stripped
        assert "\n" in message  # prose keeps a legitimate break

    def test_rows_carry_no_provider_authored_text_outside_the_finding(
        self, ref: SpecRef, config_store: ConfigStore
    ) -> None:
        bind_analysis(config_store)
        transport = StubTransport(
            payload=response_payload(
                "analysis",
                provider={"name": "candidate\rforged"},
                findings=[
                    {"kind": "k", "severity": "info", "message": "m", "refs": ["1.1"]},
                ],
            )
        )
        report = engine_with(config_store, transport).analyze(ref, run="run-1")
        row = report.review_rows("run-1")[0]
        # provider is the engine-resolved identity's display name (already put
        # through the display path when the identity was built), and criterion is
        # an engine identifier; neither is raw provider text.
        assert "\r" not in row["provider"]
        assert row["criterion"] == "1.1"


# --- supplementary providers may only add ----------------------------------


class TestSupplementaryMayOnlyAdd:
    def _engine_report(self):
        return build_report(
            [
                Violation(
                    file="requirements.md",
                    location=Location(1),
                    rule="native.missing_section",
                    severity=Severity.ERROR,
                    message="the requirements document is missing its introduction",
                )
            ]
        )

    def _provider_result(self, findings: tuple[ProviderFinding, ...]) -> CapabilityResult:
        request = CapabilityRequest(capability="validation_rules", spec_type="feature", run="run-1")
        response = CapabilityResponse(
            capability="validation_rules",
            provider_name="supplementary",
            findings=findings,
        )
        return CapabilityResult(
            request=request,
            provider=external_identity("supplementary", TRANSPORT_COMMAND),
            response=response,
            configured_transport=TRANSPORT_COMMAND,
            configured_provider="supplementary",
        )

    def test_a_provider_cannot_resolve_or_downgrade_the_engine_finding(self) -> None:
        engine = self._engine_report()
        # A provider that would like the engine's blocking violation to go away:
        # it reports an "info" finding claiming the section is fine and names the
        # same rule. The additive design gives it no way to act on that wish.
        result = self._provider_result(
            (
                ProviderFinding(
                    kind="native.missing_section",
                    severity=FindingSeverity.INFO,
                    message=Untrusted("actually the introduction is present"),
                    refs=("1.1",),
                ),
            )
        )
        supplemented = supplement(engine, result)
        # The gate reads the engine report alone, so a blocking violation still
        # blocks; the engine's violations are byte-for-byte what they were; and
        # the provider finding appears only as an additive, provider-marked entry.
        assert supplemented.gate_ok is False
        assert supplemented.gate_ok == engine.ok
        assert supplemented.violations == engine.violations
        provider_entries = [e for e in supplemented.all_entries() if not e.from_engine]
        assert len(provider_entries) == 1
        assert provider_entries[0].provider.startswith("supplementary")

    def test_provider_findings_never_enter_the_blocking_set(self) -> None:
        engine = build_report(())  # a clean engine report
        result = self._provider_result(
            (
                ProviderFinding(
                    kind="opinion",
                    severity=FindingSeverity.ERROR,
                    message=Untrusted("I think this should fail"),
                    refs=("1.1",),
                ),
            )
        )
        supplemented = supplement(engine, result)
        # A provider ERROR is a provider opinion, not an engine violation: it
        # cannot make a clean report fail a gate.
        assert supplemented.gate_ok is True
        assert supplemented.errors == ()
