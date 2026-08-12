"""The conformance runner, and the negative evidence that it checks anything.

A conformance runner is the one component in this package whose failure mode is
worse than being absent. Absent, nobody believes a provider was verified; broken,
everybody does. So the load-bearing tests here are not the ones showing that the
bundled providers pass. They are the ones showing that a provider which is wrong
in exactly one way **fails**, one deliberately non-conforming stub per assertion
class:

* a stub whose response is not schema-valid,
* a stub that declares the document processed and reports nothing,
* stubs whose coverage block contradicts itself, omits a reason, or is empty,
* a stub that answers past its deadline,
* a stub whose two identical calls differ.

Each of those failures is asserted to be *the* failure — the other checks on the
same run still pass — because a runner that fails everything is as uninformative
as one that passes everything.

Two further properties get their own tests. A report is not passing merely for
having no failures: it must also have executed every fixture and every assertion
class its suite declared, so a runner that lost its fixtures reports a gap rather
than success. And the escape hatch that lets a provider decline a document has to
be distinguishable from having examined it: the skip-declaring builtin passes the
planted-defect check by declaring, while the silent processor above fails it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from kiro_crew.apps.builtins.spec_engine.engine.capabilities import (
    CHECK_CLASSES,
    CHECK_DECLARED_COVERAGE,
    CHECK_PLANTED_DEFECT,
    CHECK_REPEATABILITY,
    CHECK_SCHEMA_VALIDITY,
    CHECK_TIMEOUT_HONORING,
    CURRENT_SCHEMA_VERSION,
    DEFECT_REPORTING_CAPABILITIES,
    DOCUMENT_CAPABILITIES,
    FIXTURE_COVERAGE_HOLE,
    FIXTURE_MALFORMED_RESPONSE,
    FIXTURE_MINIMAL_REQUEST,
    FIXTURE_OVERSIZED_DOCUMENT,
    FIXTURE_PLANTED_AMBIGUITY,
    OVERSIZED_MIN_CHARS,
    TRANSPORT_COMMAND,
    ArtifactRef,
    BuiltinCandidate,
    CapabilityRequest,
    CapabilityResponse,
    ConformanceFixture,
    ConformanceReport,
    ConformanceRunner,
    Coverage,
    EngineFloorViolation,
    PlantedDefect,
    SkippedItem,
    TransportCandidate,
    UnknownCapability,
    Untrusted,
    default_builtins,
    oversized_requirements,
    suite_for,
    verify,
    verify_builtin,
)
from kiro_crew.apps.builtins.spec_engine.engine.capabilities.fixtures import (
    AMBIGUITY_REQUIREMENTS,
    CONTRADICTION_REQUIREMENTS,
    COVERAGE_HOLE_REQUIREMENTS,
    FIXTURE_FILENAMES,
    MALFORMED_RESPONSE_REQUIREMENTS,
)
from kiro_crew.apps.builtins.spec_engine.engine.config import DELEGABLE_CAPABILITIES
from kiro_crew.apps.builtins.spec_engine.engine.local_analyzer import LocalAnalyzer

CAPABILITY = "analysis"

#: The runner used everywhere a wall-clock bound is not the point. The deadline
#: is generous and the grace is the shipped default, so a busy machine never
#: turns a passing provider into a failure.
RUNNER = ConformanceRunner()


# --- Stubs ----------------------------------------------------------------
#
# Each stub is wrong in exactly one way. That is the point: a runner is only
# useful if the check that fails names the defect, so every stub below is
# asserted to fail its own class and pass the others.


def valid_payload(
    *,
    findings: Sequence[Mapping[str, Any]] = (),
    processed: Sequence[str] = ("document:requirements",),
    skipped: Sequence[Mapping[str, str]] = (),
    depth: str = "structural",
) -> dict[str, Any]:
    """A schema-valid analysis response, as a provider would put it on the wire."""
    return {
        "schema_version": CURRENT_SCHEMA_VERSION,
        "capability": CAPABILITY,
        "provider": {"name": "stub"},
        "coverage": {"processed": list(processed), "skipped": [dict(s) for s in skipped]},
        "findings": [dict(finding) for finding in findings],
        "cost": {"credits": 0.0},
        "result": {"depth": depth},
    }


def finding_on(refs: Sequence[str], *, message: str = "the planted defect") -> dict[str, Any]:
    return {
        "kind": "stub.finding",
        "severity": "warning",
        "message": message,
        "refs": list(refs),
    }


#: References the bundled defect fixtures plant, so a conforming stub can answer
#: all three from one table instead of inspecting documents it does not parse.
PLANTED_REFS: tuple[str, ...] = ("1.1", "1.2", "2", "2.1")


@dataclass
class ConformingStub:
    """A stub that satisfies every class, by answering about the planted refs.

    The positive control. Without it, a suite in which every stub fails would be
    equally consistent with a runner that fails everything.
    """

    name: str = "conforming-stub"
    calls: int = 0

    def respond(self, request: CapabilityRequest) -> Any:
        self.calls += 1
        return valid_payload(findings=[finding_on(PLANTED_REFS)])


@dataclass
class SchemaBreakingStub:
    """Wrong in one way: ``findings`` is a string where an array belongs."""

    name: str = "schema-breaking-stub"

    def respond(self, request: CapabilityRequest) -> Any:
        payload = valid_payload(findings=[finding_on(PLANTED_REFS)])
        payload["findings"] = "none"
        return payload


@dataclass
class SilentProcessorStub:
    """Wrong in one way: declares the documents processed and reports nothing.

    The single most important stub in this module. It is schema-valid, declares
    coverage, answers instantly, and answers identically every time — so every
    check but one passes, and the one that fails is the one that decides whether
    a clean report means the spec is clean.
    """

    name: str = "silent-processor-stub"

    def respond(self, request: CapabilityRequest) -> Any:
        return valid_payload(
            processed=[f"document:{artifact.kind}" for artifact in request.artifacts]
            or ["nothing"],
        )


@dataclass
class MuteCoverageStub:
    """Wrong in one way: declares neither what it processed nor what it skipped."""

    name: str = "mute-coverage-stub"

    def respond(self, request: CapabilityRequest) -> Any:
        return valid_payload(processed=[], skipped=[])


@dataclass
class ContradictoryCoverageStub:
    """Wrong in one way: claims one item as both processed and skipped."""

    name: str = "contradictory-coverage-stub"

    def respond(self, request: CapabilityRequest) -> Any:
        return valid_payload(
            processed=["document:requirements"],
            skipped=[{"item": "document:requirements", "reason": "both, somehow"}],
        )


@dataclass
class UnexplainedSkipStub:
    """Wrong in one way: skips a document without saying why.

    Declares the skip, so it satisfies the planted-defect check honestly; the
    reason is empty, which is what "surface skipped items to the user" cannot be
    done with.
    """

    name: str = "unexplained-skip-stub"

    def respond(self, request: CapabilityRequest) -> Any:
        return valid_payload(
            processed=[],
            skipped=[{"item": "document:requirements", "reason": "  "}],
        )


@dataclass
class SlowStub:
    """Wrong in one way: answers well past the deadline the request carried."""

    sleep_s: float
    name: str = "slow-stub"

    def respond(self, request: CapabilityRequest) -> Any:
        time.sleep(self.sleep_s)
        return valid_payload(findings=[finding_on(PLANTED_REFS)])


@dataclass
class DriftingStub:
    """Wrong in one way: its answer changes between two identical calls."""

    name: str = "drifting-stub"
    calls: int = 0

    def respond(self, request: CapabilityRequest) -> Any:
        self.calls += 1
        return valid_payload(
            findings=[finding_on(PLANTED_REFS, message=f"pass number {self.calls}")]
        )


@dataclass
class RaisingStub:
    """Wrong in one way: it crashes."""

    name: str = "raising-stub"

    def respond(self, request: CapabilityRequest) -> Any:
        raise RuntimeError("no answer today")


@dataclass
class HostileTokenStub:
    """Declares coverage tokens carrying control characters.

    Provider-authored strings reach a report's detail text, and a carriage return
    in one rewrites the line printed before it in whatever is reading the report.
    """

    name: str = "hostile-token-stub"

    def respond(self, request: CapabilityRequest) -> Any:
        return valid_payload(
            processed=[],
            skipped=[{"item": "document:requirements\r\x1b[2Kforged", "reason": ""}],
        )


@dataclass
class StubTransport:
    """A capability transport that answers from a table, spawning nothing."""

    payload: Any
    seen: list[CapabilityRequest] = field(default_factory=list)

    @property
    def transport(self) -> str:
        return TRANSPORT_COMMAND

    def invoke(self, request: CapabilityRequest, *, timeout_s: int) -> Mapping[str, Any]:
        self.seen.append(request)
        return dict(self.payload)


# --- Helpers ---------------------------------------------------------------


def results_for(report: ConformanceReport, check: str) -> tuple[Any, ...]:
    return tuple(result for result in report.results if result.check == check)


def failed_checks(report: ConformanceReport) -> frozenset[str]:
    return frozenset(result.check for result in report.failures)


def timeout_fixture(*, deadline_s: int = 1) -> ConformanceFixture:
    """A fixture that asks only whether the candidate answered in time.

    Deliberately document-free and single-check: proving a wall-clock bound needs
    a real sleep, and the suite pays for every second of it.
    """
    return ConformanceFixture(
        name="deadline-only",
        capability=CAPABILITY,
        checks=(CHECK_TIMEOUT_HONORING,),
        deadline_s=deadline_s,
        rationale="a real deadline, measured against a real clock",
    )


def one_fixture(name: str) -> ConformanceFixture:
    return next(fixture for fixture in suite_for(CAPABILITY) if fixture.name == name)


# --- The suites themselves -------------------------------------------------


class TestSuites:
    def test_every_delegable_capability_has_a_suite(self) -> None:
        for capability in DELEGABLE_CAPABILITIES:
            assert suite_for(capability), capability

    def test_an_engine_floor_capability_has_no_suite(self) -> None:
        # The floor is not bindable, so there is no candidate to conform.
        with pytest.raises(EngineFloorViolation):
            suite_for("phase_gates")

    def test_an_unknown_capability_has_no_suite(self) -> None:
        with pytest.raises(UnknownCapability):
            suite_for("telepathy")

    def test_the_document_capabilities_get_all_five_fixtures(self) -> None:
        for capability in DOCUMENT_CAPABILITIES:
            assert len(suite_for(capability)) == 5, capability

    def test_defect_detection_is_required_of_the_reporting_capabilities(self) -> None:
        for capability in DELEGABLE_CAPABILITIES:
            declared = {check for fixture in suite_for(capability) for check in fixture.checks}
            required = capability in DEFECT_REPORTING_CAPABILITIES
            assert (CHECK_PLANTED_DEFECT in declared) is required, capability

    def test_the_analysis_suite_exercises_every_assertion_class(self) -> None:
        declared = {check for fixture in suite_for(CAPABILITY) for check in fixture.checks}
        assert declared == set(CHECK_CLASSES)

    def test_a_fixture_declaring_no_checks_is_refused(self) -> None:
        # A fixture that asserts nothing is a fixture that always passes.
        with pytest.raises(ValueError):
            ConformanceFixture(name="empty", capability=CAPABILITY, checks=())

    def test_a_planted_defect_needs_a_reference(self) -> None:
        with pytest.raises(ValueError):
            PlantedDefect(label="nothing to match on", artifact="requirements", refs=())

    def test_the_defect_fixtures_plant_the_defect_they_name(self) -> None:
        # Read the documents rather than trusting the fixture's own label: a
        # fixture whose defect was edited out makes the whole suite vacuous, and
        # every check would still report a pass.
        assert "quickly" in AMBIGUITY_REQUIREMENTS
        assert "SHALL NOT halt further dispatch" in CONTRADICTION_REQUIREMENTS
        assert "SHALL halt further dispatch" in CONTRADICTION_REQUIREMENTS
        assert "### Requirement 2:" in COVERAGE_HOLE_REQUIREMENTS
        assert "_Requirements: 2" not in COVERAGE_HOLE_REQUIREMENTS
        assert '"schema_version": "one"' in MALFORMED_RESPONSE_REQUIREMENTS

    def test_the_oversized_fixture_is_oversized_and_deterministic(self) -> None:
        first = oversized_requirements()
        assert len(first) >= OVERSIZED_MIN_CHARS
        # Generated twice, identical: a fixture that varies between two runs
        # cannot be used to check repeatability.
        assert first == oversized_requirements()

    def test_a_fixture_names_only_native_artifacts(self) -> None:
        for capability in DOCUMENT_CAPABILITIES:
            for fixture in suite_for(capability):
                assert set(fixture.documents) <= set(FIXTURE_FILENAMES), fixture.name

    def test_an_unknown_artifact_kind_is_refused(self) -> None:
        with pytest.raises(ValueError):
            ConformanceFixture(
                name="bad-artifact",
                capability=CAPABILITY,
                documents={"changelog": "# nope\n"},
                checks=(CHECK_SCHEMA_VALIDITY,),
            )


# --- The bundled providers pass -------------------------------------------


class TestBundledProviders:
    def test_every_builtin_passes_its_own_suite(self) -> None:
        for capability, provider in default_builtins().items():
            report = verify_builtin(provider, capability)
            assert report.passed, report.report_text()

    def test_the_local_analyzer_passes_the_analysis_suite(self) -> None:
        report = verify_builtin(LocalAnalyzer(), CAPABILITY)
        assert report.passed, report.report_text()

    def test_the_local_analyzer_detects_rather_than_declining(self) -> None:
        # The distinction the escape hatch makes, from the other side. The
        # analyzer processes the requirements document, so its pass has to come
        # from findings; a pass earned by declaring the document skipped would
        # mean the reference implementation was never exercised.
        report = verify_builtin(LocalAnalyzer(), CAPABILITY)
        detections = results_for(report, CHECK_PLANTED_DEFECT)
        assert detections
        for result in detections:
            assert result.passed
            assert result.detail.startswith("detected")

    def test_the_skip_declaring_builtin_passes_by_declaring(self) -> None:
        report = verify_builtin(default_builtins()[CAPABILITY], CAPABILITY)
        detections = results_for(report, CHECK_PLANTED_DEFECT)
        assert detections
        for result in detections:
            assert result.passed
            assert "declared skipped rather than examined" in result.detail

    def test_a_builtin_is_judged_on_the_payload_it_would_send(self) -> None:
        # The candidate adapter renders the response the way the wire carries it,
        # so a builtin cannot pass on an engine object the schema never saw.
        analyzer = LocalAnalyzer()
        request = CapabilityRequest(capability=CAPABILITY, spec_type="feature")
        payload = BuiltinCandidate(provider=analyzer).respond(request)
        assert payload == analyzer.serve(request).to_wire()
        assert isinstance(payload, Mapping)


# --- One stub per assertion class ------------------------------------------


class TestSchemaValidity:
    def test_an_invalid_response_fails_the_schema_check(self) -> None:
        report = RUNNER.run(SchemaBreakingStub(), CAPABILITY)
        assert not report.passed
        assert CHECK_SCHEMA_VALIDITY in failed_checks(report)
        for result in results_for(report, CHECK_SCHEMA_VALIDITY):
            assert not result.passed
            assert "fails the published schema" in result.detail

    def test_an_invalid_response_is_refused_whole(self) -> None:
        # Not partially read: a response that failed validation carries no
        # findings and no coverage the runner is willing to report on, so every
        # payload-derived check fails with that one reason.
        report = RUNNER.run(
            SchemaBreakingStub(), CAPABILITY, fixtures=[one_fixture(FIXTURE_PLANTED_AMBIGUITY)]
        )
        payload_checks = (CHECK_DECLARED_COVERAGE, CHECK_PLANTED_DEFECT, CHECK_REPEATABILITY)
        for check in payload_checks:
            results = results_for(report, check)
            assert results, check
            for result in results:
                assert not result.passed
                assert "nothing in it can be read" in result.detail

    def test_the_deadline_is_still_judged_on_an_invalid_response(self) -> None:
        # Arrival time is a property of the call, not of the payload, so a
        # provider that answered promptly with nonsense is reported as prompt.
        report = RUNNER.run(SchemaBreakingStub(), CAPABILITY)
        timings = results_for(report, CHECK_TIMEOUT_HONORING)
        assert timings
        assert all(result.passed for result in timings)


class TestPlantedDefectDetection:
    def test_a_silent_processor_fails_detection(self) -> None:
        report = RUNNER.run(SilentProcessorStub(), CAPABILITY)
        assert not report.passed
        detections = results_for(report, CHECK_PLANTED_DEFECT)
        assert len(detections) == 3
        for result in detections:
            assert not result.passed
            assert "did not declare requirements skipped" in result.detail

    def test_a_silent_processor_fails_only_detection(self) -> None:
        # The isolation claim: everything else about this stub is conforming, so
        # a runner reporting more than one failing class would be reporting
        # something other than what it found.
        report = RUNNER.run(SilentProcessorStub(), CAPABILITY)
        assert failed_checks(report) == {CHECK_PLANTED_DEFECT}

    def test_detection_is_matched_on_references_not_on_a_kind_vocabulary(self) -> None:
        # A candidate brings its own finding kinds. What makes a finding evidence
        # is the criterion it names.
        report = RUNNER.run(ConformingStub(), CAPABILITY)
        assert report.passed, report.report_text()

    def test_a_finding_about_something_else_is_not_detection(self) -> None:
        @dataclass
        class WrongRefStub:
            name: str = "wrong-ref-stub"

            def respond(self, request: CapabilityRequest) -> Any:
                return valid_payload(findings=[finding_on(["9.9"])])

        report = RUNNER.run(WrongRefStub(), CAPABILITY)
        assert failed_checks(report) == {CHECK_PLANTED_DEFECT}

    def test_a_declared_skip_is_an_honest_answer(self) -> None:
        @dataclass
        class SkippingStub:
            name: str = "skipping-stub"

            def respond(self, request: CapabilityRequest) -> Any:
                return valid_payload(
                    processed=[],
                    skipped=[
                        {"item": "requirements", "reason": "this provider reads no requirements"}
                    ],
                )

        report = RUNNER.run(SkippingStub(), CAPABILITY)
        assert report.passed, report.report_text()

    def test_detection_cannot_be_asked_of_a_fixture_with_nothing_planted(self) -> None:
        # A suite that asked for detection against a clean fixture would pass
        # every candidate. The runner reports that as its own failure.
        fixture = ConformanceFixture(
            name="nothing-planted",
            capability=CAPABILITY,
            checks=(CHECK_PLANTED_DEFECT,),
            rationale="a suite mistake, deliberately made",
        )
        report = RUNNER.run(ConformingStub(), CAPABILITY, fixtures=[fixture])
        assert not report.passed
        assert "declares no planted defect" in report.failures[0].detail


class TestDeclaredCoverage:
    def test_an_empty_coverage_block_fails(self) -> None:
        report = RUNNER.run(
            MuteCoverageStub(), CAPABILITY, fixtures=[one_fixture(FIXTURE_MALFORMED_RESPONSE)]
        )
        assert failed_checks(report) == {CHECK_DECLARED_COVERAGE}
        assert "nothing-wrong from nothing-examined" in report.failures[0].detail

    def test_claiming_an_item_both_processed_and_skipped_fails(self) -> None:
        report = RUNNER.run(ContradictoryCoverageStub(), CAPABILITY)
        assert failed_checks(report) == {CHECK_DECLARED_COVERAGE}
        assert "both processed and skipped" in report.failures[0].detail

    def test_a_skip_without_a_reason_fails(self) -> None:
        report = RUNNER.run(UnexplainedSkipStub(), CAPABILITY)
        assert failed_checks(report) == {CHECK_DECLARED_COVERAGE}
        assert "skipped with no reason" in report.failures[0].detail

    def test_a_hostile_coverage_token_is_sanitized_into_the_report(self) -> None:
        report = RUNNER.run(HostileTokenStub(), CAPABILITY)
        text = report.report_text()
        assert "\r" not in text
        assert "\x1b" not in text


class TestTimeoutHonoring:
    def test_a_candidate_that_answers_past_its_deadline_fails(self) -> None:
        # A real sleep against a real clock. Faking the clock here would leave
        # the wall-clock bound itself unexercised, which is the whole check.
        runner = ConformanceRunner(deadline_s=1, grace_s=0.0)
        report = runner.run(SlowStub(sleep_s=1.3), CAPABILITY, fixtures=[timeout_fixture()])
        assert not report.passed
        assert failed_checks(report) == {CHECK_TIMEOUT_HONORING}
        assert "ignored its deadline" in report.failures[0].detail

    def test_a_prompt_candidate_passes_the_same_bound(self) -> None:
        runner = ConformanceRunner(deadline_s=1, grace_s=0.0)
        report = runner.run(ConformingStub(), CAPABILITY, fixtures=[timeout_fixture()])
        assert report.passed, report.report_text()

    def test_the_request_carries_the_deadline_the_runner_measures(self) -> None:
        transport = StubTransport(payload=valid_payload(findings=[finding_on(PLANTED_REFS)]))
        runner = ConformanceRunner(deadline_s=7)
        report = runner.run(
            TransportCandidate(transport=transport, label="stub-transport"),
            CAPABILITY,
            fixtures=[one_fixture(FIXTURE_PLANTED_AMBIGUITY)],
        )
        assert report.passed, report.report_text()
        assert transport.seen
        # The bound is not a runner-private number: the candidate is told what it
        # is being held to, which is what makes honoring it possible.
        assert all(request.deadline_s == 7 for request in transport.seen)

    def test_a_nonsense_runner_bound_is_refused(self) -> None:
        with pytest.raises(ValueError):
            ConformanceRunner(deadline_s=0)
        with pytest.raises(ValueError):
            ConformanceRunner(grace_s=-1.0)


class TestRepeatability:
    def test_a_drifting_candidate_fails(self) -> None:
        stub = DriftingStub()
        report = RUNNER.run(stub, CAPABILITY)
        assert not report.passed
        assert failed_checks(report) == {CHECK_REPEATABILITY}
        assert "differed in: findings" in report.failures[0].detail
        # Actually called twice per repeatable fixture, so the comparison had two
        # answers to compare rather than one answer compared with itself.
        assert stub.calls > len(results_for(report, CHECK_REPEATABILITY))

    def test_drift_in_coverage_fails(self) -> None:
        @dataclass
        class DriftingCoverageStub:
            name: str = "drifting-coverage-stub"
            calls: int = 0

            def respond(self, request: CapabilityRequest) -> Any:
                self.calls += 1
                return valid_payload(
                    findings=[finding_on(PLANTED_REFS)],
                    processed=[f"document:requirements/{self.calls}"],
                )

        report = RUNNER.run(DriftingCoverageStub(), CAPABILITY)
        assert failed_checks(report) == {CHECK_REPEATABILITY}
        assert "processed coverage" in report.failures[0].detail

    def test_an_answer_with_nothing_in_it_is_not_repeatable(self) -> None:
        # Two empty answers compare equal. The analyzer declares its blind spots
        # on every pass precisely so that a response is never empty, and a
        # candidate whose response says nothing has not demonstrated anything by
        # saying it twice.
        @dataclass
        class EmptyStub:
            name: str = "empty-stub"

            def respond(self, request: CapabilityRequest) -> Any:
                payload = valid_payload(processed=[], skipped=[])
                payload["result"] = {"depth": "structural"}
                return payload

        report = RUNNER.run(
            EmptyStub(), CAPABILITY, fixtures=[one_fixture(FIXTURE_OVERSIZED_DOCUMENT)]
        )
        repeats = results_for(report, CHECK_REPEATABILITY)
        assert repeats and not repeats[0].passed
        assert "would compare equal without saying anything" in repeats[0].detail

    def test_a_second_call_that_breaks_fails_repeatability(self) -> None:
        @dataclass
        class SecondCallBreaksStub:
            name: str = "second-call-breaks-stub"
            calls: int = 0

            def respond(self, request: CapabilityRequest) -> Any:
                self.calls += 1
                payload = valid_payload(findings=[finding_on(PLANTED_REFS)])
                if self.calls % 2 == 0:
                    payload["findings"] = "none"
                return payload

        report = RUNNER.run(SecondCallBreaksStub(), CAPABILITY)
        assert failed_checks(report) == {CHECK_REPEATABILITY}
        assert "fails the published schema where the first passed" in report.failures[0].detail

    def test_the_report_names_the_difference_without_quoting_it(self) -> None:
        # Two responses differ in provider-authored prose. The report says which
        # component moved; it does not paste the prose, which is untrusted text
        # heading for a terminal.
        report = RUNNER.run(DriftingStub(), CAPABILITY)
        assert "pass number" not in report.report_text()


class TestCandidateFailure:
    def test_a_crashing_candidate_fails_rather_than_raising(self) -> None:
        report = RUNNER.run(RaisingStub(), CAPABILITY)
        assert not report.passed
        assert all(
            not result.passed for result in report.results if result.check != CHECK_TIMEOUT_HONORING
        )
        assert "raised RuntimeError" in report.failures[0].detail

    def test_a_crash_does_not_stop_the_remaining_fixtures(self) -> None:
        report = RUNNER.run(RaisingStub(), CAPABILITY)
        assert set(report.executed_fixtures) == set(report.declared_fixtures)


# --- The runner's own honesty ---------------------------------------------


class TestReportHonesty:
    def test_a_report_with_no_results_does_not_pass(self) -> None:
        report = ConformanceReport(
            capability=CAPABILITY,
            candidate="nobody",
            declared_fixtures=(FIXTURE_PLANTED_AMBIGUITY,),
            declared_checks=(CHECK_SCHEMA_VALIDITY,),
            results=(),
        )
        assert not report.passed
        assert "produced no results at all" in report.gaps[0]

    def test_an_empty_suite_does_not_pass(self) -> None:
        report = RUNNER.run(ConformingStub(), CAPABILITY, fixtures=[])
        assert not report.passed
        assert report.gaps

    def test_a_declared_fixture_that_never_ran_is_a_gap(self) -> None:
        report = ConformanceReport(
            capability=CAPABILITY,
            candidate="partial",
            declared_fixtures=(FIXTURE_PLANTED_AMBIGUITY, FIXTURE_COVERAGE_HOLE),
            declared_checks=(CHECK_SCHEMA_VALIDITY,),
            results=RUNNER.run(
                ConformingStub(), CAPABILITY, fixtures=[one_fixture(FIXTURE_PLANTED_AMBIGUITY)]
            ).results,
        )
        assert not report.passed
        assert any(FIXTURE_COVERAGE_HOLE in gap for gap in report.gaps)

    def test_a_declared_check_that_never_ran_is_a_gap(self) -> None:
        report = ConformanceReport(
            capability=CAPABILITY,
            candidate="partial",
            declared_fixtures=(FIXTURE_MINIMAL_REQUEST,),
            declared_checks=CHECK_CLASSES,
            results=RUNNER.run(
                ConformingStub(),
                CAPABILITY,
                fixtures=[
                    ConformanceFixture(
                        name=FIXTURE_MINIMAL_REQUEST,
                        capability=CAPABILITY,
                        checks=(CHECK_SCHEMA_VALIDITY,),
                        rationale="one check only",
                    )
                ],
            ).results,
        )
        assert not report.passed
        assert any(CHECK_REPEATABILITY in gap for gap in report.gaps)

    def test_a_gap_is_not_reported_as_a_failure(self) -> None:
        report = RUNNER.run(ConformingStub(), CAPABILITY, fixtures=[])
        assert not report.failures
        assert report.gaps

    def test_a_full_run_declares_and_executes_the_same_sets(self) -> None:
        report = RUNNER.run(ConformingStub(), CAPABILITY)
        assert set(report.executed_fixtures) == set(report.declared_fixtures)
        assert set(report.executed_checks) == set(report.declared_checks)
        assert not report.gaps

    def test_the_report_serializes_for_a_surface(self) -> None:
        record = RUNNER.run(SilentProcessorStub(), CAPABILITY).to_json_object()
        assert record["passed"] is False
        assert record["capability"] == CAPABILITY
        assert any(entry["check"] == CHECK_PLANTED_DEFECT for entry in record["results"])

    def test_the_summary_counts_what_ran(self) -> None:
        report = RUNNER.run(ConformingStub(), CAPABILITY)
        assert "conforms" in report.summary()
        assert f"{len(report.results)}/{len(report.results)}" in report.summary()


class TestFixtureMaterialization:
    def test_documents_are_written_where_the_request_points(self, tmp_path: Path) -> None:
        seen: list[tuple[ArtifactRef, ...]] = []

        @dataclass
        class RecordingStub:
            name: str = "recording-stub"

            def respond(self, request: CapabilityRequest) -> Any:
                seen.append(request.artifacts)
                return valid_payload(findings=[finding_on(PLANTED_REFS)])

        report = RUNNER.run(
            RecordingStub(),
            CAPABILITY,
            fixtures=[one_fixture(FIXTURE_PLANTED_AMBIGUITY)],
            root=tmp_path,
        )
        assert report.passed, report.report_text()
        assert seen
        for artifacts in seen:
            assert artifacts
            for artifact in artifacts:
                path = Path(artifact.path)
                # Absolute, present, and readable: a provider is a separate
                # program with its own working directory.
                assert path.is_absolute()
                assert path.read_text(encoding="utf-8")

    def test_a_temporary_root_is_cleaned_up(self) -> None:
        seen: list[Path] = []

        @dataclass
        class PathRecordingStub:
            name: str = "path-recording-stub"

            def respond(self, request: CapabilityRequest) -> Any:
                for artifact in request.artifacts:
                    seen.append(Path(artifact.path))
                return valid_payload(findings=[finding_on(PLANTED_REFS)])

        verify(PathRecordingStub(), CAPABILITY)
        assert seen
        assert not any(path.exists() for path in seen)


# --- A property over the gap machinery ------------------------------------


@st.composite
def _check_subsets(draw: st.DrawFn) -> tuple[str, ...]:
    """A non-empty subset of the assertion classes, in canonical order."""
    chosen = draw(
        st.lists(
            st.sampled_from(CHECK_CLASSES), min_size=1, max_size=len(CHECK_CLASSES), unique=True
        )
    )
    return tuple(check for check in CHECK_CLASSES if check in chosen)


class TestGapProperty:
    @settings(max_examples=40, deadline=None)
    @given(checks=_check_subsets())
    def test_a_pass_means_every_declared_class_was_evaluated(self, checks: tuple[str, ...]) -> None:
        """However a suite is composed, passing implies nothing went unevaluated.

        The property that keeps a partially-executed run from reading as a clean
        one. It is stated over arbitrary compositions because the failure it
        guards against is a check class quietly dropping out of a path taken by
        only some suites.
        """
        planted = (
            (PlantedDefect(label="planted", artifact="requirements", refs=PLANTED_REFS),)
            if CHECK_PLANTED_DEFECT in checks
            else ()
        )
        fixture = ConformanceFixture(
            name="generated",
            capability=CAPABILITY,
            documents={"requirements": AMBIGUITY_REQUIREMENTS},
            checks=checks,
            planted=planted,
            deadline_s=5,
            rationale="composed by a property test",
        )
        report = RUNNER.run(ConformingStub(), CAPABILITY, fixtures=[fixture])
        assert report.passed, report.report_text()
        assert set(report.executed_checks) == set(checks)
        assert report.declared_checks == checks

    @settings(max_examples=40, deadline=None)
    @given(checks=_check_subsets())
    def test_a_dropped_result_is_always_a_gap(self, checks: tuple[str, ...]) -> None:
        """Losing any single result turns a passing report into a gapped one."""
        results = tuple(
            result
            for result in RUNNER.run(
                ConformingStub(),
                CAPABILITY,
                fixtures=[
                    ConformanceFixture(
                        name="generated",
                        capability=CAPABILITY,
                        documents={"requirements": AMBIGUITY_REQUIREMENTS},
                        checks=checks,
                        planted=(
                            (
                                PlantedDefect(
                                    label="planted", artifact="requirements", refs=PLANTED_REFS
                                ),
                            )
                            if CHECK_PLANTED_DEFECT in checks
                            else ()
                        ),
                        deadline_s=5,
                        rationale="composed by a property test",
                    )
                ],
            ).results
        )
        for dropped in range(len(results)):
            thinned = results[:dropped] + results[dropped + 1 :]
            report = ConformanceReport(
                capability=CAPABILITY,
                candidate="thinned",
                declared_fixtures=("generated",),
                declared_checks=checks,
                results=thinned,
            )
            assert not report.passed


# --- Response rendering ----------------------------------------------------


class TestResponseWire:
    def test_a_response_renders_the_payload_its_schema_describes(self) -> None:
        response = CapabilityResponse(
            capability=CAPABILITY,
            provider_name="stub",
            coverage=Coverage(
                processed=("document:requirements",),
                skipped=(SkippedItem(item="document:design", reason=Untrusted("not supplied")),),
            ),
            findings=(),
            cost_credits=1.5,
            result={"depth": "structural"},
            provider_version="3",
        )
        payload = response.to_wire()
        assert payload["provider"] == {"name": "stub", "version": "3"}
        assert payload["cost"] == {"credits": 1.5}
        assert payload["coverage"]["skipped"][0]["reason"] == "not supplied"

    def test_an_undeclared_version_is_absent_rather_than_empty(self) -> None:
        response = CapabilityResponse(capability=CAPABILITY, provider_name="stub")
        assert "version" not in response.to_wire()["provider"]
