"""The bundled analyzer's five checks, its honesty, and its cost.

Three claims are load-bearing here and none of them is "the checks work".

**Calibration.** The repository's own spec is the corpus, and the assertion on it
is exact rather than a bound: two findings, both named. A structural check that
fires on format-clean prose trains an author to ignore the analyzer, so a check
that becomes noisier is a regression even when the new findings are arguable.

**Honesty.** A clean pass must still declare what the depth cannot see. The tests
assert the coverage block carries the blind spots on a pass with zero findings,
because that is the case where a reader is most likely to finish the sentence
themselves.

**Cost.** Zero credits and no network are asserted structurally: the module's own
imports are audited for anything that could reach a model or a socket, and a pass
runs with the socket constructors replaced by traps. Asserting only that the
declared cost field is zero would pass for a provider that quietly called out.
"""

from __future__ import annotations

import ast
import socket
from pathlib import Path
from typing import Any, Iterable, Mapping

import pytest

from kiro_crew.apps.builtins.spec_engine.engine import local_analyzer as analyzer
from kiro_crew.apps.builtins.spec_engine.engine.capabilities import (
    CURRENT_SCHEMA_VERSION,
    ArtifactRef,
    CapabilityRegistry,
    CapabilityRequest,
    CapabilityResponse,
    FindingSeverity,
    ProviderFinding,
    ProviderKind,
    ProviderNature,
    validate_response,
)
from kiro_crew.apps.builtins.spec_engine.engine.config import ConfigStore
from kiro_crew.apps.builtins.spec_engine.engine.local_analyzer import (
    Corpus,
    LocalAnalyzer,
    analyze,
)

#: The repository's own spec, which doubles as the corpus the checks must stay
#: quiet on.
_SPEC_DIR = (
    # tests -> spec_engine -> builtins -> apps -> kiro_crew -> src -> repository
    Path(__file__).resolve().parents[6]
    / ".kiro"
    / "specs"
    / "agent-agnostic-spec-engine"
)

#: Modules that would let a deterministic provider reach a network, a child
#: process, or a model. None may appear in the analyzer's import graph.
_FORBIDDEN_IMPORTS: tuple[str, ...] = (
    "socket",
    "ssl",
    "http",
    "urllib",
    "requests",
    "httpx",
    "ftplib",
    "smtplib",
    "telnetlib",
    "asyncio",
    "subprocess",
    "multiprocessing",
    "webbrowser",
)

#: Engine and host modules that reach a model or a session. A structural
#: analyzer that imported one of these could dispatch a turn, and the guarantee
#: is that it cannot.
_FORBIDDEN_NAMES: tuple[str, ...] = (
    "acp",
    "session",
    "subagent",
    "agent",
    "model_registry",
    "gateway",
    "transports",
    "budget",
)


def _header(title: str) -> str:
    return f"# {title}\n\n"


def _requirements(criteria: Iterable[str], *, glossary: Iterable[str] = ()) -> str:
    """A minimal, format-shaped requirements document declaring *criteria*."""
    lines = [
        "# Requirements Document",
        "",
        "## Introduction",
        "",
        "One requirement, written to carry the planted defect under test.",
        "",
    ]
    entries = list(glossary)
    if entries:
        lines += ["## Glossary", ""]
        lines += [f"- **{term}**: a term this document defines." for term in entries]
        lines += [""]
    lines += [
        "## Requirements",
        "",
        "### Requirement 1: The planted case",
        "",
        "**User Story:** As a reader, I want the case stated, so that it is testable.",
        "",
        "#### Acceptance Criteria",
        "",
    ]
    lines += [f"{number}. {text}" for number, text in enumerate(criteria, start=1)]
    return "\n".join(lines) + "\n"


def _tasks(references: Iterable[str]) -> str:
    """A tasks document whose single leaf claims *references*."""
    joined = ", ".join(references)
    return (
        "# Implementation Plan\n\n"
        "## Tasks\n\n"
        "- [ ] 1. Deliver the requirement\n"
        "  - [ ] 1.1 Do the work\n"
        "    - Implement what the criteria describe\n"
        f"    - _Requirements: {joined}_\n\n"
        "## Task Dependency Graph\n\n"
        '```json\n{"waves": [{"id": 0, "tasks": ["1.1"]}]}\n```\n'
    )


def _kinds(findings: Iterable[ProviderFinding]) -> tuple[str, ...]:
    return tuple(finding.kind for finding in findings)


def _wire(response: CapabilityResponse) -> dict[str, Any]:
    """The response as a provider would put it on the wire.

    Built here rather than in the engine because the builtin never serialises:
    the point of the assertion is that what the builtin returns would satisfy the
    published contract if it did, so the reference implementation is held to the
    same schema an external provider is.
    """
    return {
        "schema_version": CURRENT_SCHEMA_VERSION,
        "capability": response.capability,
        "provider": {"name": response.provider_name, "version": response.provider_version},
        "coverage": response.coverage.to_json_object(),
        "findings": [finding.to_json_object() for finding in response.findings],
        "cost": {"credits": response.cost_credits},
        "result": dict(response.result),
    }


def _corpus_dir() -> Path:
    if not (_SPEC_DIR / "requirements.md").is_file():
        pytest.skip(f"{_SPEC_DIR} is not present in this checkout")
    return _SPEC_DIR


def _corpus_paths() -> Mapping[str, Path]:
    spec = _corpus_dir()
    return {
        "requirements": spec / "requirements.md",
        "design": spec / "design.md",
        "tasks": spec / "tasks.md",
    }


# --- Glossary terms --------------------------------------------------------


class TestGlossaryTerms:
    def test_a_term_used_but_not_defined_is_reported(self) -> None:
        text = _requirements(
            [
                "THE Spec_Engine SHALL record the outcome of every gate decision.",
                "THE Ghost_Component SHALL be reachable from the recorded outcome.",
            ],
            glossary=["Spec_Engine"],
        )
        findings = analyze(Corpus(requirements=text)).of_kind(analyzer.KIND_TERM_UNDEFINED)
        assert len(findings) == 1
        assert "Ghost_Component" in findings[0].message.for_display()
        # Addressed to the criterion that used it, so a driver can route the
        # finding to a line rather than to the document.
        assert findings[0].refs == ("1.2",)

    def test_a_defined_term_is_not_reported(self) -> None:
        text = _requirements(
            ["THE Spec_Engine SHALL record the outcome of every gate decision."],
            glossary=["Spec_Engine"],
        )
        assert analyze(Corpus(requirements=text)).of_kind(analyzer.KIND_TERM_UNDEFINED) == ()

    def test_the_plural_of_a_defined_term_is_defined(self) -> None:
        text = _requirements(
            [
                "THE Spec_Engine SHALL resolve every Delegable_Capability from configuration.",
                "THE Spec_Engine SHALL report the Delegable_Capabilities it resolved.",
                "THE Spec_Engine SHALL list the Cost_Profiles a project may select.",
            ],
            glossary=["Spec_Engine", "Delegable_Capability", "Cost_Profile"],
        )
        assert analyze(Corpus(requirements=text)).of_kind(analyzer.KIND_TERM_UNDEFINED) == ()

    def test_a_screaming_case_constant_is_not_a_glossary_term(self) -> None:
        text = _requirements(
            ["THE Spec_Engine SHALL read ROLE_MODEL_KEYS from the host configuration."],
            glossary=["Spec_Engine"],
        )
        assert analyze(Corpus(requirements=text)).of_kind(analyzer.KIND_TERM_UNDEFINED) == ()

    def test_a_term_quoted_as_code_is_not_a_glossary_term(self) -> None:
        text = _requirements(
            ["THE Spec_Engine SHALL call `Some_Function` on the host, once per run."],
            glossary=["Spec_Engine"],
        )
        assert analyze(Corpus(requirements=text)).of_kind(analyzer.KIND_TERM_UNDEFINED) == ()

    def test_a_term_used_only_in_the_design_is_reported(self) -> None:
        requirements = _requirements(
            ["THE Spec_Engine SHALL record the outcome of every gate decision."],
            glossary=["Spec_Engine"],
        )
        design = _header("Design Document") + "The Phantom_Layer holds the recorded outcome.\n"
        findings = analyze(Corpus(requirements=requirements, design=design)).of_kind(
            analyzer.KIND_TERM_UNDEFINED
        )
        assert [f.message.for_display().split()[0] for f in findings] == ["Phantom_Layer"]
        # No criterion used it, so the finding carries no criterion reference
        # rather than inventing one.
        assert findings[0].refs == ()

    def test_no_glossary_section_skips_the_check_instead_of_reporting_every_term(self) -> None:
        text = _requirements(
            [
                "THE Spec_Engine SHALL record the outcome of every gate decision.",
                "THE Review_Gate SHALL start execution only from a human action.",
            ]
        )
        outcome = analyze(Corpus(requirements=text))
        assert outcome.of_kind(analyzer.KIND_TERM_UNDEFINED) == ()
        skipped = {item.item: item.reason.for_display() for item in outcome.coverage.skipped}
        key = f"{analyzer.CHECK_PREFIX}{analyzer.KIND_TERM_UNDEFINED}"
        assert key in skipped
        assert "glossary" in skipped[key]
        assert key not in outcome.coverage.processed


# --- Unquantified qualifiers ----------------------------------------------


class TestQualifiers:
    @pytest.mark.parametrize(
        "qualifier",
        ["fast", "reliable", "appropriate", "scalable", "minimal", "user-friendly"],
    )
    def test_a_qualifier_with_no_bound_is_reported(self, qualifier: str) -> None:
        text = _requirements([f"THE Spec_Engine SHALL keep the review queue {qualifier}."])
        findings = analyze(Corpus(requirements=text)).of_kind(analyzer.KIND_QUALIFIER_UNQUANTIFIED)
        assert len(findings) == 1
        assert qualifier in findings[0].message.for_display()
        assert findings[0].refs == ("1.1",)

    def test_a_numeric_bound_in_the_criterion_suppresses_the_finding(self) -> None:
        text = _requirements(
            ["THE Spec_Engine SHALL render the review queue fast, in under 200 milliseconds."]
        )
        assert (
            analyze(Corpus(requirements=text)).of_kind(analyzer.KIND_QUALIFIER_UNQUANTIFIED) == ()
        )

    def test_a_configured_bound_suppresses_the_finding(self) -> None:
        text = _requirements(
            [
                "THE Orchestrator SHALL dispatch tasks in parallel up to a configured "
                "concurrency cap, so that throughput stays reasonable."
            ]
        )
        assert (
            analyze(Corpus(requirements=text)).of_kind(analyzer.KIND_QUALIFIER_UNQUANTIFIED) == ()
        )

    def test_a_word_that_merely_contains_a_qualifier_is_not_one(self) -> None:
        # "capacity" contains "cap" and "manyfold" contains "many"; a substring
        # match would report both, and a check that fires on the letters rather
        # than the word is one an author learns to disbelieve.
        text = _requirements(
            ["THE Watcher_Dispatcher SHALL queue items when capacity is exhausted."]
        )
        assert (
            analyze(Corpus(requirements=text)).of_kind(analyzer.KIND_QUALIFIER_UNQUANTIFIED) == ()
        )

    def test_the_question_names_the_bound_and_recommends_one(self) -> None:
        text = _requirements(["THE Spec_Engine SHALL keep the review queue fast."])
        finding = analyze(Corpus(requirements=text)).of_kind(analyzer.KIND_QUALIFIER_UNQUANTIFIED)[
            0
        ]
        question = finding.question
        assert question is not None
        assert "fast" in question.question.for_display()
        assert len(question.choices) >= 2
        assert len(question.consequences) == len(question.choices)
        assert question.recommended is not None
        assert question.recommended.for_display()


# --- Independent testability ----------------------------------------------


class TestTestability:
    @pytest.mark.parametrize(
        "phrase",
        [
            "where possible",
            "as appropriate",
            "on a best effort basis",
            "and so on",
            "to be determined",
            "correctly",
        ],
    )
    def test_untestable_language_is_reported(self, phrase: str) -> None:
        text = _requirements(
            [f"THE Spec_Engine SHALL record the initiator of every run, {phrase}."]
        )
        findings = analyze(Corpus(requirements=text)).of_kind(analyzer.KIND_NOT_TESTABLE)
        assert len(findings) == 1
        assert findings[0].refs == ("1.1",)
        assert phrase.strip() in findings[0].message.for_display()

    def test_a_criterion_is_reported_once_however_many_families_it_trips(self) -> None:
        # One criterion is one defect. Reporting it per matched family would
        # make the count a property of the vocabulary rather than of the spec.
        text = _requirements(
            ["THE Spec_Engine SHALL record initiators where possible, and so on, " "correctly."]
        )
        assert len(analyze(Corpus(requirements=text)).of_kind(analyzer.KIND_NOT_TESTABLE)) == 1

    def test_a_plain_obligation_is_not_reported(self) -> None:
        text = _requirements(
            [
                "WHEN execution starts, THE Spec_Engine SHALL record the initiator and a "
                "timestamp in the spec's audit log."
            ]
        )
        assert analyze(Corpus(requirements=text)).of_kind(analyzer.KIND_NOT_TESTABLE) == ()

    def test_an_illustrative_enumeration_is_not_open_ended(self) -> None:
        # "such as" and "including" are how this format introduces examples of a
        # stated obligation. Treating them as open-ended would fire on most of a
        # clean document.
        text = _requirements(
            [
                "THE Delivery_Pipeline SHALL produce an isolated workspace, such as a "
                "feature branch or a separate working copy, including its own checkout."
            ]
        )
        assert analyze(Corpus(requirements=text)).of_kind(analyzer.KIND_NOT_TESTABLE) == ()


# --- Coverage -------------------------------------------------------------


class TestCoverage:
    def test_a_requirement_no_task_claims_is_reported(self) -> None:
        text = _requirements(["THE Spec_Engine SHALL record the initiator of every run."])
        outcome = analyze(Corpus(requirements=text, tasks=_tasks(["2.1"])))
        findings = outcome.of_kind(analyzer.KIND_REQUIREMENT_UNCOVERED)
        assert len(findings) == 1
        assert findings[0].refs == ("1",)
        # An unbuilt requirement is a hole in the plan, not a stylistic note.
        assert findings[0].severity is FindingSeverity.ERROR

    def test_a_criterion_no_task_claims_is_reported_under_a_covered_requirement(self) -> None:
        text = _requirements(
            [
                "THE Spec_Engine SHALL record the initiator of every run.",
                "THE Spec_Engine SHALL record a timestamp beside the initiator.",
            ]
        )
        findings = analyze(Corpus(requirements=text, tasks=_tasks(["1.1"]))).of_kind(
            analyzer.KIND_CRITERION_UNCOVERED
        )
        assert [finding.refs for finding in findings] == [("1.2",)]

    def test_a_fully_covered_plan_reports_nothing(self) -> None:
        text = _requirements(["THE Spec_Engine SHALL record the initiator of every run."])
        outcome = analyze(Corpus(requirements=text, tasks=_tasks(["1.1"])))
        assert outcome.of_kind(analyzer.KIND_REQUIREMENT_UNCOVERED) == ()
        assert outcome.of_kind(analyzer.KIND_CRITERION_UNCOVERED) == ()

    def test_absent_tasks_skip_the_coverage_checks_rather_than_reporting_a_hole(self) -> None:
        text = _requirements(["THE Spec_Engine SHALL record the initiator of every run."])
        outcome = analyze(Corpus(requirements=text))
        assert outcome.of_kind(analyzer.KIND_REQUIREMENT_UNCOVERED) == ()
        skipped = {item.item for item in outcome.coverage.skipped}
        assert f"{analyzer.CHECK_PREFIX}{analyzer.KIND_REQUIREMENT_UNCOVERED}" in skipped
        assert f"{analyzer.CHECK_PREFIX}{analyzer.KIND_CRITERION_UNCOVERED}" in skipped


# --- Overlap and contradiction --------------------------------------------


class TestCollisions:
    def test_opposite_obligations_under_one_condition_contradict(self) -> None:
        text = _requirements(
            [
                "WHEN execution is requested, THE Review_Gate SHALL start execution.",
                "WHEN execution is requested, THE Review_Gate SHALL NOT start execution.",
            ]
        )
        findings = analyze(Corpus(requirements=text)).of_kind(analyzer.KIND_CRITERIA_CONTRADICT)
        assert len(findings) == 1
        assert findings[0].refs == ("1.1", "1.2")
        assert findings[0].severity is FindingSeverity.ERROR

    def test_the_same_obligation_twice_under_one_condition_overlaps(self) -> None:
        text = _requirements(
            [
                "WHEN a run completes, THE Spec_Engine SHALL record the outcome.",
                "WHEN a run completes, THE Spec_Engine SHALL record the outcome in the log.",
            ]
        )
        findings = analyze(Corpus(requirements=text)).of_kind(analyzer.KIND_CRITERIA_OVERLAP)
        assert len(findings) == 1
        assert findings[0].refs == ("1.1", "1.2")
        assert findings[0].severity is FindingSeverity.WARNING

    def test_unconditional_obligations_collide_too(self) -> None:
        text = _requirements(
            [
                "THE Spec_Engine SHALL record the initiator of every run.",
                "THE Spec_Engine SHALL NOT record the initiator of every run.",
            ]
        )
        assert (
            len(analyze(Corpus(requirements=text)).of_kind(analyzer.KIND_CRITERIA_CONTRADICT)) == 1
        )

    def test_different_conditions_do_not_collide(self) -> None:
        text = _requirements(
            [
                "WHERE the policy reserves execution for a human, THE Review_Gate SHALL "
                "start execution only from a human action.",
                "WHERE the policy authorizes autonomous execution, THE Review_Gate SHALL "
                "start execution when the gates pass.",
            ]
        )
        outcome = analyze(Corpus(requirements=text))
        assert outcome.of_kind(analyzer.KIND_CRITERIA_CONTRADICT) == ()
        assert outcome.of_kind(analyzer.KIND_CRITERIA_OVERLAP) == ()

    def test_different_subjects_under_one_condition_do_not_collide(self) -> None:
        text = _requirements(
            [
                "WHEN a run completes, THE Spec_Engine SHALL record the outcome.",
                "WHEN a run completes, THE Spec_App SHALL record the outcome.",
            ]
        )
        assert analyze(Corpus(requirements=text)).of_kind(analyzer.KIND_CRITERIA_OVERLAP) == ()

    def test_different_obligations_on_one_subject_do_not_collide(self) -> None:
        text = _requirements(
            [
                "WHEN a run completes, THE Spec_Engine SHALL record the outcome.",
                "WHEN a run completes, THE Spec_Engine SHALL notify the configured channel.",
            ]
        )
        assert analyze(Corpus(requirements=text)).of_kind(analyzer.KIND_CRITERIA_OVERLAP) == ()

    def test_criteria_in_different_requirements_are_not_compared(self) -> None:
        # A trigger is written in the context of its own requirement, so the
        # same words in two requirements are frequently two situations.
        text = (
            _requirements(
                ["WHEN a run completes, THE Spec_Engine SHALL record the outcome."]
            ).rstrip("\n")
            + "\n\n### Requirement 2: A second requirement\n\n"
            "**User Story:** As a reader, I want a second case, so that scope is clear.\n\n"
            "#### Acceptance Criteria\n\n"
            "1. WHEN a run completes, THE Spec_Engine SHALL NOT record the outcome.\n"
        )
        outcome = analyze(Corpus(requirements=text))
        assert outcome.of_kind(analyzer.KIND_CRITERIA_CONTRADICT) == ()
        assert outcome.of_kind(analyzer.KIND_CRITERIA_OVERLAP) == ()


# --- Declared depth and honest coverage ------------------------------------


class TestHonesty:
    def test_the_declared_depth_is_structural(self) -> None:
        response = LocalAnalyzer().serve(_request(_corpus_paths()))
        assert response.result == {"depth": analyzer.DEPTH_STRUCTURAL}

    def test_a_clean_pass_still_declares_what_the_depth_cannot_see(self) -> None:
        # The case that matters. A pass with no findings and an empty coverage
        # block reads as "the spec is correct", which is the one conclusion this
        # depth cannot support.
        text = _requirements(
            ["THE Spec_Engine SHALL record the initiator of every run."],
            glossary=["Spec_Engine"],
        )
        outcome = analyze(Corpus(requirements=text, tasks=_tasks(["1.1"])))
        assert outcome.findings == ()
        declared = {item.item for item in outcome.coverage.skipped}
        for item, _reason in analyzer.STRUCTURAL_BLIND_SPOTS:
            assert f"{analyzer.BLIND_SPOT_PREFIX}{item}" in declared
        assert not outcome.coverage.complete

    def test_every_blind_spot_carries_a_reason(self) -> None:
        outcome = analyze(Corpus(requirements=_requirements(["THE X SHALL do the thing."])))
        blind = [
            item
            for item in outcome.coverage.skipped
            if item.item.startswith(analyzer.BLIND_SPOT_PREFIX)
        ]
        assert len(blind) == len(analyzer.STRUCTURAL_BLIND_SPOTS)
        assert all(item.reason.for_display().strip() for item in blind)

    def test_a_check_that_ran_is_named_as_processed(self) -> None:
        text = _requirements(
            ["THE Spec_Engine SHALL record the initiator of every run."],
            glossary=["Spec_Engine"],
        )
        outcome = analyze(Corpus(requirements=text, tasks=_tasks(["1.1"])))
        for kind in analyzer.ALL_KINDS:
            assert f"{analyzer.CHECK_PREFIX}{kind}" in outcome.coverage.processed

    def test_every_check_is_either_processed_or_skipped_never_neither(self) -> None:
        # Silence from a check that ran is evidence; silence from one that did
        # not is not. A check missing from both lists is indistinguishable.
        outcome = analyze(Corpus(requirements=_requirements(["THE X SHALL do the thing."])))
        skipped = {item.item for item in outcome.coverage.skipped}
        for kind in analyzer.ALL_KINDS:
            entry = f"{analyzer.CHECK_PREFIX}{kind}"
            assert (entry in outcome.coverage.processed) != (entry in skipped)

    def test_an_empty_corpus_declares_every_check_skipped(self) -> None:
        outcome = analyze(Corpus())
        assert outcome.findings == ()
        skipped = {item.item for item in outcome.coverage.skipped}
        for kind in analyzer.ALL_KINDS:
            assert f"{analyzer.CHECK_PREFIX}{kind}" in skipped
        assert not any(
            entry.startswith(analyzer.DOCUMENT_PREFIX) for entry in outcome.coverage.processed
        )


# --- The provider ---------------------------------------------------------


def _request(paths: Mapping[str, Path], *, run: str = "run-1") -> CapabilityRequest:
    return CapabilityRequest(
        capability=analyzer.CAPABILITY,
        spec_type="feature",
        artifacts=tuple(ArtifactRef.of(kind, path) for kind, path in paths.items()),
        run=run,
    )


class TestProvider:
    def test_the_response_satisfies_the_published_analysis_schema(self) -> None:
        response = LocalAnalyzer().serve(_request(_corpus_paths()))
        assert validate_response(analyzer.CAPABILITY, _wire(response)) == ()

    def test_the_identity_declares_a_deterministic_builtin(self) -> None:
        identity = LocalAnalyzer().identity
        assert identity.name == analyzer.PROVIDER_NAME
        assert identity.kind is ProviderKind.BUILTIN
        # A deterministic pass and a model-backed pass make different claims;
        # a surface that showed them identically would invite reading the first
        # as the second.
        assert identity.nature is ProviderNature.DETERMINISTIC
        assert identity.version

    def test_it_registers_as_the_analysis_builtin(self, tmp_path: Path) -> None:
        registry = CapabilityRegistry(ConfigStore(root=tmp_path / "config"))
        bound = analyzer.register(registry)
        assert registry.builtin(analyzer.CAPABILITY) is bound
        served = registry.builtin(analyzer.CAPABILITY).serve(_request(_corpus_paths()))
        assert served.result == {"depth": analyzer.DEPTH_STRUCTURAL}

    def test_an_unreadable_document_is_declared_rather_than_raised(self, tmp_path: Path) -> None:
        # The analyzer is what every other provider degrades to, so it answers
        # with what it could read instead of failing the run behind it.
        paths = dict(_corpus_paths())
        paths["design"] = tmp_path / "absent.md"
        response = LocalAnalyzer().serve(_request(paths))
        declared = {item.item: item.reason.for_display() for item in response.coverage.skipped}
        entry = f"{analyzer.DOCUMENT_PREFIX}design"
        assert entry in declared
        assert "design.md" in declared[entry]
        assert validate_response(analyzer.CAPABILITY, _wire(response)) == ()

    def test_the_sidecar_is_not_read_as_a_document(self, tmp_path: Path) -> None:
        sidecar = tmp_path / ".config.kiro"
        sidecar.write_text('{"specType": "feature"}', encoding="utf-8")
        request = CapabilityRequest(
            capability=analyzer.CAPABILITY,
            spec_type="feature",
            artifacts=(ArtifactRef.of("config", sidecar),),
        )
        response = LocalAnalyzer().serve(request)
        assert response.findings == ()
        assert not any(
            entry.startswith(analyzer.DOCUMENT_PREFIX) for entry in response.coverage.processed
        )

    def test_the_same_documents_produce_the_same_findings(self) -> None:
        # Repeatability is part of what a deterministic provider claims: a
        # caller comparing two passes needs a difference to mean the documents
        # changed.
        request = _request(_corpus_paths())
        first = LocalAnalyzer().serve(request)
        second = LocalAnalyzer().serve(request)
        assert _wire(first) == _wire(second)


# --- The real corpus ------------------------------------------------------


class TestRealSpec:
    def test_the_repository_spec_reports_exactly_its_known_defects(self) -> None:
        # Exact rather than a bound. This spec is format-clean prose, so a check
        # that grows noisier on it is a regression even when the new findings
        # are arguable: an analyzer an author learns to skip earns nothing.
        outcome = analyze(_read_corpus())
        reported = sorted((finding.kind, finding.refs) for finding in outcome.findings)
        assert reported == [
            # Known coverage gap: no task references this criterion.
            (analyzer.KIND_CRITERION_UNCOVERED, ("13.14",)),
            # A conformance obligation excused "where applicable" without saying
            # when it applies.
            (analyzer.KIND_NOT_TESTABLE, ("26.15",)),
        ]

    def test_no_requirement_of_the_repository_spec_is_wholly_uncovered(self) -> None:
        outcome = analyze(_read_corpus())
        assert outcome.of_kind(analyzer.KIND_REQUIREMENT_UNCOVERED) == ()

    def test_its_glossary_covers_every_term_it_uses(self) -> None:
        outcome = analyze(_read_corpus())
        assert outcome.of_kind(analyzer.KIND_TERM_UNDEFINED) == ()

    def test_no_criteria_in_one_requirement_collide(self) -> None:
        outcome = analyze(_read_corpus())
        assert outcome.of_kind(analyzer.KIND_CRITERIA_OVERLAP) == ()
        assert outcome.of_kind(analyzer.KIND_CRITERIA_CONTRADICT) == ()

    def test_every_finding_addresses_a_criterion_that_exists(self) -> None:
        from kiro_crew.apps.builtins.spec_engine.engine import parse_requirements

        index = parse_requirements((_corpus_dir() / "requirements.md").read_text(encoding="utf-8"))
        known = {str(requirement.number) for requirement in index} | {
            criterion.identifier for requirement in index for criterion in requirement.criteria
        }
        for finding in analyze(_read_corpus()).findings:
            assert finding.refs
            assert set(finding.refs) <= known

    def test_every_finding_carries_an_answerable_question(self) -> None:
        for finding in analyze(_read_corpus()).findings:
            question = finding.question
            assert question is not None
            assert question.question.for_display()
            assert len(question.choices) >= 2
            # A choice with no stated cost is not a decision, it is a menu.
            assert len(question.consequences) == len(question.choices)
            assert question.recommended is not None
            assert question.recommended.for_display()


def _read_corpus() -> Corpus:
    corpus, unread = analyzer.read_corpus(dict(_corpus_paths()))
    assert unread == ()
    return corpus


# --- Zero credits, no network --------------------------------------------


class TestCostAndReach:
    def test_the_declared_cost_is_zero(self) -> None:
        response = LocalAnalyzer().serve(_request(_corpus_paths()))
        assert response.cost_credits == 0.0

    def test_the_module_imports_nothing_that_could_reach_a_model_or_a_socket(self) -> None:
        # The structural half of the claim. A cost field reading zero is what a
        # provider that called out anyway would also report, so the assertion
        # that carries weight is that the code has no way to call out.
        source = Path(analyzer.__file__).read_text(encoding="utf-8")
        imported: set[str] = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
                imported.update(f"{node.module or ''}.{alias.name}" for alias in node.names)
        for name in imported:
            head = name.lstrip(".").split(".")[0]
            assert head not in _FORBIDDEN_IMPORTS, f"{name} could reach outside the process"
            parts = {part for part in name.lstrip(".").split(".") if part}
            assert not parts & set(_FORBIDDEN_NAMES), f"{name} could reach a model or a session"

    def test_a_full_pass_makes_no_network_call(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The runtime half. Every socket constructor becomes a trap, so a call
        # made through any transitive import fails the test rather than
        # succeeding quietly.
        def refuse(*args: object, **kwargs: object) -> None:
            raise AssertionError("the analyzer opened a socket")

        monkeypatch.setattr(socket, "socket", refuse)
        monkeypatch.setattr(socket, "create_connection", refuse)
        monkeypatch.setattr(socket, "getaddrinfo", refuse)
        response = LocalAnalyzer().serve(_request(_corpus_paths()))
        assert response.result == {"depth": analyzer.DEPTH_STRUCTURAL}
        assert response.cost_credits == 0.0

    def test_findings_are_wrapped_as_untrusted_text(self) -> None:
        # The analyzer's own text is engine-authored, but it travels the same
        # path an external provider's does; keeping the wrapper means a display
        # surface has one type to handle rather than two.
        text = _requirements(["THE Spec_Engine SHALL keep the review queue fast."])
        finding = analyze(Corpus(requirements=text)).findings[0]
        assert not isinstance(finding.message, str)
        assert finding.message.for_display()
