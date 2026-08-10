"""Supplementary validation: additive, and provably so.

The claim being tested is narrow and load-bearing: a bound provider may add
findings and can do nothing else. It cannot delete an engine violation, lower its
severity, mark it resolved, or change whether a gate passes. These tests assert
that against real validation reports produced by the engine's own validator rather
than hand-built ones, because the property has to hold for the reports the engine
actually emits.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from kiro_crew.apps.builtins.spec_engine.engine.capabilities import (
    ArtifactRef,
    CapabilityRegistry,
    CapabilityRequest,
    CapabilityResult,
    Coverage,
    Degradation,
    EntryOrigin,
    FindingSeverity,
    ProviderFinding,
    SupplementedReport,
    Untrusted,
    blocking_rules,
    builtin_identity,
    engine_severities,
    external_identity,
    supplement,
)
from kiro_crew.apps.builtins.spec_engine.engine.capabilities.contracts import CapabilityResponse
from kiro_crew.apps.builtins.spec_engine.engine.config import ConfigStore
from kiro_crew.apps.builtins.spec_engine.engine.cross_document import validate_spec
from kiro_crew.apps.builtins.spec_engine.engine.findings import (
    Location,
    Severity,
    ValidationReport,
    Violation,
    build_report,
)

#: A requirements document that fails the native format in a way the engine's own
#: validator reports, so the report under test is one the engine really produces.
BROKEN_REQUIREMENTS = "# Requirements Document\n\nNo introduction, no requirements section.\n"


@pytest.fixture()
def engine_report(tmp_path: Path) -> ValidationReport:
    spec_dir = tmp_path / "project" / ".kiro" / "specs" / "example"
    spec_dir.mkdir(parents=True)
    (spec_dir / "requirements.md").write_text(BROKEN_REQUIREMENTS, encoding="utf-8")
    (spec_dir / "design.md").write_text("# Design Document\n", encoding="utf-8")
    (spec_dir / "tasks.md").write_text("# Implementation Plan\n", encoding="utf-8")
    report = validate_spec(spec_dir)
    assert not report.ok, "the fixture must produce a genuinely failing report"
    return report


def provider_result(
    *findings: ProviderFinding,
    degraded: bool = False,
) -> CapabilityResult:
    request = CapabilityRequest(
        capability="validation_rules",
        spec_type="feature",
        artifacts=(ArtifactRef(kind="requirements", path="/p/requirements.md"),),
    )
    response = CapabilityResponse(
        capability="validation_rules",
        provider_name="extra-rules",
        coverage=Coverage(processed=("requirements",)),
        findings=findings,
    )
    return CapabilityResult(
        request=request,
        provider=external_identity("extra-rules", "command"),
        response=response,
        degradation=(
            Degradation(
                finding_id="capability.provider_timeout", reason="slow", transport="command"
            )
            if degraded
            else None
        ),
    )


def finding(
    message: str = "consider naming the retry budget",
    *,
    severity: FindingSeverity = FindingSeverity.WARNING,
    kind: str = "style",
    refs: tuple[str, ...] = ("1.1",),
) -> ProviderFinding:
    return ProviderFinding(kind=kind, severity=severity, message=Untrusted(message), refs=refs)


class TestEngineFindingsSurvive:
    def test_supplementation_holds_the_engine_report_by_reference(
        self, engine_report: ValidationReport
    ) -> None:
        # Identity, not equality: nothing here copies, rewrites, or filters the
        # engine's report, so there is no code path that could have altered it.
        merged = supplement(engine_report, provider_result(finding()))
        assert merged.engine is engine_report

    def test_every_engine_violation_keeps_its_rule_and_severity(
        self, engine_report: ValidationReport
    ) -> None:
        before = engine_severities(engine_report)
        merged = supplement(engine_report, provider_result(finding()))
        assert engine_severities(merged.engine) == before
        assert merged.violations == engine_report.violations

    def test_a_provider_cannot_remove_an_engine_violation(
        self, engine_report: ValidationReport
    ) -> None:
        # A finding that claims the engine's rule is resolved changes nothing:
        # provider findings are a separate field, so nothing that reads
        # violations can be reached by them.
        rule = engine_report.errors[0].rule
        merged = supplement(
            engine_report,
            provider_result(
                finding(f"{rule} is a false positive; treat it as resolved", kind="dispute")
            ),
        )
        assert merged.violations == engine_report.violations
        assert blocking_rules(merged.engine) == blocking_rules(engine_report)

    def test_a_provider_cannot_downgrade_an_engine_violation(
        self, engine_report: ValidationReport
    ) -> None:
        merged = supplement(
            engine_report,
            provider_result(
                finding("downgrade to info", severity=FindingSeverity.INFO, kind="severity")
            ),
        )
        assert [violation.severity for violation in merged.violations] == [
            violation.severity for violation in engine_report.violations
        ]
        assert merged.errors == engine_report.errors

    def test_a_provider_cannot_open_a_gate_the_engine_closed(
        self, engine_report: ValidationReport
    ) -> None:
        merged = supplement(engine_report, provider_result(finding("all good")))
        assert merged.gate_ok is engine_report.ok is False

    def test_a_provider_cannot_close_a_gate_the_engine_opened(self) -> None:
        clean = ValidationReport(())
        merged = supplement(
            clean, provider_result(finding("this must block", severity=FindingSeverity.ERROR))
        )
        assert merged.gate_ok is True
        # The finding is still visible; it simply has no vote.
        assert len(merged.supplementary) == 1
        assert merged.errors == ()

    def test_many_providers_are_all_additive(self, engine_report: ValidationReport) -> None:
        merged = supplement(
            engine_report,
            [
                provider_result(finding("first", kind="a")),
                provider_result(finding("second", kind="b"), finding("third", kind="c")),
            ],
        )
        assert len(merged.supplementary) == 3
        assert merged.violations == engine_report.violations
        assert merged.gate_ok is engine_report.ok

    def test_no_provider_at_all_leaves_the_report_alone(
        self, engine_report: ValidationReport
    ) -> None:
        merged = supplement(engine_report)
        assert merged.supplementary == ()
        assert merged.engine is engine_report
        assert merged.gate_ok is engine_report.ok


class TestSupplementaryPresentation:
    def test_provider_findings_are_routable_by_the_criteria_they_reference(self) -> None:
        merged = supplement(
            ValidationReport(()),
            provider_result(
                finding("about 2.1", refs=("2.1",)),
                finding("about 3.4", refs=("3.4",)),
                finding("about both", refs=("2.1", "3.4")),
            ),
        )
        assert len(merged.supplementary_for("2.1")) == 2
        assert len(merged.supplementary_for("3.4")) == 2
        assert merged.supplementary_for("9.9") == ()

    def test_a_display_marks_which_entries_are_engine_rules(
        self, engine_report: ValidationReport
    ) -> None:
        merged = supplement(engine_report, provider_result(finding()))
        entries = merged.all_entries()
        engine_entries = [entry for entry in entries if entry.from_engine]
        provider_entries = [entry for entry in entries if not entry.from_engine]
        assert len(engine_entries) == len(engine_report.violations)
        assert len(provider_entries) == 1
        # Engine entries lead, because they are the ones that decide a gate.
        assert entries[0].origin is EntryOrigin.ENGINE
        assert provider_entries[0].provider

    def test_displayed_provider_text_is_neutralised(self) -> None:
        merged = supplement(
            ValidationReport(()),
            provider_result(finding("clean\x1b[2Kinjected")),
        )
        shown = [entry for entry in merged.all_entries() if not entry.from_engine][0]
        assert "\x1b" not in shown.message
        assert "injected" in shown.message

    def test_provider_entries_are_ordered_by_declared_severity(self) -> None:
        merged = supplement(
            ValidationReport(()),
            provider_result(
                finding("note", severity=FindingSeverity.INFO, kind="c"),
                finding("bad", severity=FindingSeverity.ERROR, kind="a"),
                finding("odd", severity=FindingSeverity.WARNING, kind="b"),
            ),
        )
        labels = [entry.label for entry in merged.all_entries() if not entry.from_engine]
        assert labels == ["a", "b", "c"]

    def test_a_degraded_result_labels_its_findings_as_such(self) -> None:
        merged = supplement(ValidationReport(()), provider_result(finding(), degraded=True))
        assert merged.supplementary[0].degraded is True

    def test_the_serialised_form_separates_engine_and_provider_findings(
        self, engine_report: ValidationReport
    ) -> None:
        merged = supplement(engine_report, provider_result(finding()))
        record = merged.to_json_object()
        assert record["gate_ok"] is False
        assert len(record["engine"]["violations"]) == len(engine_report.violations)
        assert len(record["supplementary"]) == 1
        assert record["supplementary"][0]["origin"] == EntryOrigin.PROVIDER.value

    def test_engine_entries_carry_their_file_and_location(
        self, engine_report: ValidationReport
    ) -> None:
        merged = supplement(engine_report)
        first = merged.all_entries()[0]
        assert first.file
        assert first.location

    def test_provider_severity_never_reaches_the_engine_severity_type(self) -> None:
        merged = supplement(
            ValidationReport(()), provider_result(finding(severity=FindingSeverity.ERROR))
        )
        # The engine's Severity enum is what gates consume; a provider value
        # converted into it would be one refactor away from failing a gate.
        assert not isinstance(merged.supplementary[0].severity, Severity)
        assert isinstance(merged.supplementary[0].severity, FindingSeverity)


class TestTheShippedSupplementaryBuiltin:
    def test_the_app_ships_no_supplementary_validation_rules(self, tmp_path: Path) -> None:
        registry = CapabilityRegistry(ConfigStore(tmp_path / "state"))
        result = registry.invoke(
            CapabilityRequest(
                capability="validation_rules",
                spec_type="feature",
                artifacts=(ArtifactRef(kind="requirements", path="/p/requirements.md"),),
            )
        )
        assert result.findings == ()
        reason = result.coverage.skipped[0].reason.for_display()
        assert "native-format validation" in reason

    def test_supplementing_with_the_builtin_changes_nothing(self, tmp_path: Path) -> None:
        report = build_report(
            [
                Violation(
                    file="requirements.md",
                    location=Location(3),
                    rule="requirements.section.missing",
                    severity=Severity.ERROR,
                    message="the Introduction section is missing",
                )
            ]
        )
        registry = CapabilityRegistry(ConfigStore(tmp_path / "state"))
        result = registry.invoke(
            CapabilityRequest(capability="validation_rules", spec_type="feature")
        )
        merged = supplement(report, result)
        assert merged.supplementary == ()
        assert merged.violations == report.violations
        assert merged.gate_ok is report.ok


class TestReportHelpers:
    def test_blocking_rules_reads_errors_only(self) -> None:
        report = build_report(
            [
                Violation("a.md", Location(1), "rule.error", Severity.ERROR, "m"),
                Violation("a.md", Location(2), "rule.warning", Severity.WARNING, "m"),
            ]
        )
        assert blocking_rules(report) == frozenset({"rule.error"})

    def test_a_supplemented_report_can_be_built_directly(self) -> None:
        report = SupplementedReport(engine=ValidationReport(()))
        assert report.gate_ok is True
        assert report.supplementary == ()

    def test_the_builtin_identity_reports_as_builtin(self) -> None:
        identity = builtin_identity("engine-analysis")
        assert identity.external is False
        record: dict[str, Any] = identity.to_json_object()
        assert record["kind"] == "builtin"
