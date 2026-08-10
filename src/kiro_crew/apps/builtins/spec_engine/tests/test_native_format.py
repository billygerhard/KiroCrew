"""Native-format validator rules.

Each negative case mutates a format-clean fixture in exactly one way and asserts
the rule identifier and the line the violation lands on. Asserting the identifier
rather than the message is deliberate: the identifier is the published contract
that drivers route on, and a test that matched on wording would pass while the
contract broke.
"""

from __future__ import annotations

import pytest

from kiro_crew.apps.builtins.spec_engine.engine import (
    DocumentKind,
    Severity,
    kind_for_filename,
    rules,
    validate_document,
    validate_document_text,
    validate_documents,
)
from kiro_crew.apps.builtins.spec_engine.engine.findings import Location

VALID_REQUIREMENTS = """\
# Requirements Document

## Introduction

The engine validates native spec documents.

## Requirements

### Requirement 1: Rules as code

**User Story:** As a developer, I want rules as code, so that every driver agrees.

#### Acceptance Criteria

1. WHEN a document is submitted, THE Spec_Engine SHALL validate its native format.
2. IF a document fails validation, THEN THE Spec_Engine SHALL report every violation.
"""

VALID_DESIGN = """\
# Design Document

## Overview

One validator, many drivers.

## Architecture

The engine owns the rules; drivers call it.

## Components and Interfaces

A validator module returning a report.

## Data Models

A violation carries a file, a location, and a rule identifier.

## Error Handling

Validation collects violations rather than raising on the first.

## Testing Strategy

Fixtures for every rule, plus the real artifacts.
"""

VALID_TASKS = """\
# Implementation Plan

## Tasks

- [ ] 1. Validator
  - [ ] 1.1 Parse the document
    - Split headings from fenced blocks
    - _Requirements: 1.1_
  - [ ] 1.2 Report the violations
    - _Requirements: 1.1, 1.2_
"""


def _replace(text: str, old: str, new: str) -> str:
    """Substitute exactly one occurrence, failing loudly if the anchor moved."""
    assert text.count(old) == 1, f"fixture anchor {old!r} is not unique"
    return text.replace(old, new)


def _lines(text: str) -> list[str]:
    return text.splitlines()


def _line_of(text: str, needle: str, *, exact: bool = False) -> int:
    """Return the 1-based line number of the single line matching ``needle``.

    ``exact`` compares whole lines, which is what a needle like ``"## "`` needs:
    as a substring it occurs on every section heading.
    """
    hits = [
        i
        for i, line in enumerate(_lines(text), start=1)
        if (line == needle if exact else needle in line)
    ]
    assert len(hits) == 1, f"{needle!r} appears on lines {hits}"
    return hits[0]


def _check(text: str, kind: DocumentKind):
    return validate_document_text(text, kind=kind, file=kind.filename)


def _assert_violation(report, rule: str, line: int) -> None:
    matches = report.for_rule(rule)
    assert matches, f"expected {rule}, got {sorted(report.rule_ids)}"
    assert [v.location.line for v in matches] == [
        line
    ], f"{rule} landed on {[v.location.line for v in matches]}, expected {line}"


# --- The fixtures themselves are clean ------------------------------------


@pytest.mark.parametrize(
    "text,kind",
    [
        (VALID_REQUIREMENTS, DocumentKind.REQUIREMENTS),
        (VALID_DESIGN, DocumentKind.DESIGN),
        (VALID_TASKS, DocumentKind.TASKS),
    ],
    ids=["requirements", "design", "tasks"],
)
def test_clean_fixtures_report_nothing(text, kind):
    report = _check(text, kind)
    assert report.ok
    assert len(report) == 0
    assert not report


# --- Document-wide rules ---------------------------------------------------


@pytest.mark.parametrize("text", ["", "   \n\n\t\n"], ids=["empty", "whitespace"])
def test_empty_document(text):
    report = _check(text, DocumentKind.REQUIREMENTS)
    _assert_violation(report, rules.DOCUMENT_EMPTY, 1)
    assert not report.ok


def test_missing_title_reports_at_the_top_of_the_file():
    text = _replace(VALID_REQUIREMENTS, "# Requirements Document\n\n", "")
    _assert_violation(_check(text, DocumentKind.REQUIREMENTS), rules.DOCUMENT_TITLE_MISSING, 1)


def test_a_section_heading_first_is_a_missing_title_not_a_mismatch():
    text = "## Introduction\n\nText.\n\n## Requirements\n\n### Requirement 1: A\n"
    report = _check(text, DocumentKind.REQUIREMENTS)
    _assert_violation(report, rules.DOCUMENT_TITLE_MISSING, 1)
    assert not report.for_rule(rules.DOCUMENT_TITLE_MISMATCH)


def test_wrong_title_reports_a_mismatch():
    text = _replace(VALID_DESIGN, "# Design Document", "# Some Other Document")
    _assert_violation(_check(text, DocumentKind.DESIGN), rules.DOCUMENT_TITLE_MISMATCH, 1)


def test_a_title_may_name_its_subject():
    text = _replace(VALID_TASKS, "# Implementation Plan", "# Implementation Plan: Validator")
    assert _check(text, DocumentKind.TASKS).ok


def test_second_level_one_heading_is_a_duplicate_title():
    text = VALID_DESIGN + "\n# Appendix\n\nMore.\n"
    _assert_violation(
        _check(text, DocumentKind.DESIGN),
        rules.DOCUMENT_TITLE_DUPLICATE,
        _line_of(text, "# Appendix"),
    )


@pytest.mark.parametrize(
    "heading",
    ["##Requirements", "## ", "####### Requirements"],
    ids=["no-space", "no-text", "too-deep"],
)
def test_malformed_heading(heading):
    text = _replace(VALID_REQUIREMENTS, "## Requirements", heading)
    report = _check(text, DocumentKind.REQUIREMENTS)
    _assert_violation(report, rules.HEADING_MALFORMED, _line_of(text, heading, exact=True))
    # The broken heading no longer declares the section, so both defects surface
    # from one edit -- the validator does not stop at the first.
    assert report.for_rule(rules.SECTION_MISSING)


def test_missing_required_section():
    text = _replace(
        VALID_DESIGN,
        "## Data Models\n\nA violation carries a file, a location, and a rule identifier.\n\n",
        "",
    )
    report = _check(text, DocumentKind.DESIGN)
    _assert_violation(report, rules.SECTION_MISSING, 1)
    assert "Data Models" in report.for_rule(rules.SECTION_MISSING)[0].message


def test_duplicate_required_section():
    text = VALID_DESIGN + "\n## Overview\n\nSaid twice.\n"
    # The appended heading follows every line of the fixture plus one blank.
    _assert_violation(
        _check(text, DocumentKind.DESIGN),
        rules.SECTION_DUPLICATE,
        len(_lines(VALID_DESIGN)) + 2,
    )


def test_empty_required_section_is_a_warning_not_an_error():
    text = _replace(VALID_REQUIREMENTS, "The engine validates native spec documents.\n", "")
    report = _check(text, DocumentKind.REQUIREMENTS)
    empty = report.for_rule(rules.SECTION_EMPTY)
    assert [v.severity for v in empty] == [Severity.WARNING]
    # A structurally readable document with only warnings still passes the gate.
    assert report.ok
    assert report.warnings == empty


def test_headings_inside_a_fenced_block_are_not_document_structure():
    fenced = _replace(
        VALID_DESIGN,
        "The engine owns the rules; drivers call it.",
        "```text\n# Not a title\n## Not a section\n```",
    )
    assert _check(fenced, DocumentKind.DESIGN).ok


def test_a_longer_fence_closes_only_on_a_matching_run():
    fenced = _replace(
        VALID_DESIGN,
        "The engine owns the rules; drivers call it.",
        "````text\n```\n# Still inside\n````",
    )
    assert _check(fenced, DocumentKind.DESIGN).ok


# --- requirements.md -------------------------------------------------------


def test_no_requirements_declared():
    text = _replace(
        VALID_REQUIREMENTS,
        VALID_REQUIREMENTS[VALID_REQUIREMENTS.index("### Requirement 1") :],
        "Nothing here yet.\n",
    )
    _assert_violation(
        _check(text, DocumentKind.REQUIREMENTS),
        rules.REQUIREMENTS_NONE,
        _line_of(text, "## Requirements"),
    )


def test_malformed_requirement_heading():
    text = _replace(VALID_REQUIREMENTS, "### Requirement 1: Rules as code", "### Rules as code")
    report = _check(text, DocumentKind.REQUIREMENTS)
    _assert_violation(report, rules.REQUIREMENT_HEADING_MALFORMED, _line_of(text, "### Rules"))
    # An unreadable heading declares no requirement, so the section is empty too.
    assert report.for_rule(rules.REQUIREMENTS_NONE)


def test_requirement_heading_without_a_title():
    text = _replace(VALID_REQUIREMENTS, "### Requirement 1: Rules as code", "### Requirement 1:")
    _assert_violation(
        _check(text, DocumentKind.REQUIREMENTS),
        rules.REQUIREMENT_TITLE_MISSING,
        _line_of(text, "### Requirement 1:"),
    )


def test_requirement_numbering_must_start_at_one():
    text = _replace(VALID_REQUIREMENTS, "### Requirement 1:", "### Requirement 2:")
    _assert_violation(
        _check(text, DocumentKind.REQUIREMENTS),
        rules.REQUIREMENT_NUMBER_NOT_SEQUENTIAL,
        _line_of(text, "### Requirement 2:"),
    )


def test_requirement_numbering_must_not_skip():
    text = VALID_REQUIREMENTS + (
        "\n### Requirement 3: Second rule\n\n"
        "**User Story:** As a user, I want a second rule, so that more is covered.\n\n"
        "#### Acceptance Criteria\n\n"
        "1. THE Spec_Engine SHALL do the second thing.\n"
    )
    _assert_violation(
        _check(text, DocumentKind.REQUIREMENTS),
        rules.REQUIREMENT_NUMBER_NOT_SEQUENTIAL,
        _line_of(text, "### Requirement 3:"),
    )


def test_missing_user_story_reports_on_the_requirement_heading():
    text = _replace(
        VALID_REQUIREMENTS,
        "**User Story:** As a developer, I want rules as code, so that every driver agrees.\n\n",
        "",
    )
    _assert_violation(
        _check(text, DocumentKind.REQUIREMENTS),
        rules.USER_STORY_MISSING,
        _line_of(text, "### Requirement 1:"),
    )


def test_malformed_user_story():
    text = _replace(
        VALID_REQUIREMENTS,
        "As a developer, I want rules as code, so that every driver agrees.",
        "Rules should be code.",
    )
    _assert_violation(
        _check(text, DocumentKind.REQUIREMENTS),
        rules.USER_STORY_MALFORMED,
        _line_of(text, "Rules should be code."),
    )


def test_a_role_beginning_with_a_vowel_is_a_valid_user_story():
    text = _replace(
        VALID_REQUIREMENTS,
        "As a developer, I want rules as code,",
        "As an operator, I want rules as code,",
    )
    assert _check(text, DocumentKind.REQUIREMENTS).ok


def test_missing_acceptance_criteria_heading():
    text = _replace(VALID_REQUIREMENTS, "#### Acceptance Criteria\n\n", "")
    _assert_violation(
        _check(text, DocumentKind.REQUIREMENTS),
        rules.CRITERIA_SECTION_MISSING,
        _line_of(text, "### Requirement 1:"),
    )


def test_acceptance_criteria_heading_with_no_criteria():
    text = VALID_REQUIREMENTS[: VALID_REQUIREMENTS.index("1. WHEN a document")]
    _assert_violation(
        _check(text, DocumentKind.REQUIREMENTS),
        rules.CRITERIA_EMPTY,
        _line_of(text, "#### Acceptance Criteria"),
    )


def test_criterion_numbering_must_be_sequential():
    text = _replace(VALID_REQUIREMENTS, "2. IF a document fails", "3. IF a document fails")
    _assert_violation(
        _check(text, DocumentKind.REQUIREMENTS),
        rules.CRITERION_NUMBER_NOT_SEQUENTIAL,
        _line_of(text, "3. IF a document fails"),
    )


def test_criterion_without_an_ears_keyword():
    text = _replace(
        VALID_REQUIREMENTS,
        "1. WHEN a document is submitted, THE Spec_Engine SHALL validate its native format.",
        "1. The engine validates the document somehow.",
    )
    report = _check(text, DocumentKind.REQUIREMENTS)
    _assert_violation(
        report,
        rules.CRITERION_KEYWORD_MISSING,
        _line_of(text, "1. The engine validates"),
    )
    # The keyword is what the rest of the shape is read against, so no further
    # criterion rule fires on the same line.
    assert not report.for_rule(rules.CRITERION_SHALL_MISSING)


def test_criterion_keyword_column_points_at_the_criterion_body():
    text = _replace(
        VALID_REQUIREMENTS,
        "1. WHEN a document is submitted, THE Spec_Engine SHALL validate its native format.",
        "1. validation happens.",
    )
    violation = _check(text, DocumentKind.REQUIREMENTS).for_rule(rules.CRITERION_KEYWORD_MISSING)[0]
    assert violation.location.column == len("1. ") + 1


def test_criterion_without_shall():
    text = _replace(
        VALID_REQUIREMENTS,
        "THE Spec_Engine SHALL validate its native format.",
        "THE Spec_Engine validates its native format.",
    )
    _assert_violation(
        _check(text, DocumentKind.REQUIREMENTS),
        rules.CRITERION_SHALL_MISSING,
        _line_of(text, "THE Spec_Engine validates"),
    )


def test_a_universal_invariant_needs_no_shall():
    text = _replace(
        VALID_REQUIREMENTS,
        "1. WHEN a document is submitted, THE Spec_Engine SHALL validate its native format.",
        "1. FOR ALL documents, the reported violations are the complete set.",
    )
    assert _check(text, DocumentKind.REQUIREMENTS).ok


def test_conditional_criterion_without_a_consequence():
    text = _replace(
        VALID_REQUIREMENTS,
        "2. IF a document fails validation, THEN THE Spec_Engine SHALL report every violation.",
        "2. IF a document fails validation, THE Spec_Engine SHALL report every violation.",
    )
    _assert_violation(
        _check(text, DocumentKind.REQUIREMENTS),
        rules.CRITERION_IF_WITHOUT_THEN,
        _line_of(text, "2. IF a document fails"),
    )


# --- tasks.md --------------------------------------------------------------


def test_no_tasks_declared():
    text = "# Implementation Plan\n\n## Tasks\n\nStill planning.\n"
    _assert_violation(_check(text, DocumentKind.TASKS), rules.TASKS_NONE, 3)


@pytest.mark.parametrize(
    "item",
    [
        "  - [] 1.1 Parse the document",
        "  - [ ]1.1 Parse the document",
        "  -[ ] 1.1 Parse the document",
        "  * [ ] 1.1 Parse the document",
    ],
    ids=["no-mark", "no-space", "no-dash-space", "wrong-bullet"],
)
def test_malformed_checkbox(item):
    text = _replace(VALID_TASKS, "  - [ ] 1.1 Parse the document", item)
    report = _check(text, DocumentKind.TASKS)
    _assert_violation(report, rules.TASK_CHECKBOX_MALFORMED, _line_of(text, "Parse the document"))
    # A malformed box still yields a usable task, so its number is not also
    # reported as missing and its leaf reference is still credited.
    assert not report.for_rule(rules.TASK_NUMBER_MISSING)
    assert not report.for_rule(rules.TASK_REFERENCE_MISSING)


@pytest.mark.parametrize("mark", ["x", "X", "-"], ids=["done", "done-upper", "in-progress"])
def test_completed_and_in_progress_marks_are_native(mark):
    text = _replace(VALID_TASKS, "  - [ ] 1.1 Parse", f"  - [{mark}] 1.1 Parse")
    assert _check(text, DocumentKind.TASKS).ok


def test_task_without_a_number():
    text = _replace(VALID_TASKS, "  - [ ] 1.1 Parse the document", "  - [ ] Parse the document")
    report = _check(text, DocumentKind.TASKS)
    _assert_violation(report, rules.TASK_NUMBER_MISSING, _line_of(text, "Parse the document"))
    assert not report.for_rule(rules.TASK_TITLE_MISSING)


def test_task_number_nested_too_deep():
    text = _replace(VALID_TASKS, "  - [ ] 1.1 Parse", "  - [ ] 1.1.1 Parse")
    report = _check(text, DocumentKind.TASKS)
    _assert_violation(report, rules.TASK_NUMBER_DEPTH, _line_of(text, "1.1.1 Parse"))
    # The depth rule already covers it; indentation is not blamed twice.
    assert not report.for_rule(rules.TASK_NUMBER_DEPTH_MISMATCH)


def test_task_number_depth_must_match_indentation():
    text = _replace(VALID_TASKS, "  - [ ] 1.1 Parse", "  - [ ] 2. Parse")
    _assert_violation(
        _check(text, DocumentKind.TASKS),
        rules.TASK_NUMBER_DEPTH_MISMATCH,
        _line_of(text, "2. Parse"),
    )


def test_duplicate_task_number():
    text = _replace(VALID_TASKS, "  - [ ] 1.2 Report", "  - [ ] 1.1 Report")
    report = _check(text, DocumentKind.TASKS)
    _assert_violation(report, rules.TASK_NUMBER_DUPLICATE, _line_of(text, "1.1 Report"))
    assert (
        str(_line_of(text, "1.1 Parse")) in report.for_rule(rules.TASK_NUMBER_DUPLICATE)[0].message
    )


def test_task_without_a_title():
    text = _replace(VALID_TASKS, "  - [ ] 1.1 Parse the document", "  - [ ] 1.1")
    _assert_violation(
        _check(text, DocumentKind.TASKS),
        rules.TASK_TITLE_MISSING,
        _line_of(text, "- [ ] 1.1"),
    )


def test_invalid_task_indentation():
    text = _replace(VALID_TASKS, "  - [ ] 1.1 Parse", "   - [ ] 1.1 Parse")
    report = _check(text, DocumentKind.TASKS)
    _assert_violation(report, rules.TASK_INDENT_INVALID, _line_of(text, "1.1 Parse"))
    # Indentation is unusable, so depth is not measured against it.
    assert not report.for_rule(rules.TASK_NUMBER_DEPTH_MISMATCH)


def test_subtask_naming_an_undeclared_parent():
    text = _replace(VALID_TASKS, "  - [ ] 1.1 Parse", "  - [ ] 4.1 Parse")
    _assert_violation(
        _check(text, DocumentKind.TASKS),
        rules.TASK_PARENT_UNKNOWN,
        _line_of(text, "4.1 Parse"),
    )


def test_parentage_is_read_from_the_number_not_document_order():
    """A late subtask under an earlier parent is ordinary, not an error."""
    text = VALID_TASKS + (
        "- [ ] 2. Phases\n"
        "  - [ ] 2.1 Derive the phase\n"
        "    - _Requirements: 2.1_\n"
        "  - [ ] 1.3 A late leaf under task one\n"
        "    - _Requirements: 1.2_\n"
    )
    assert _check(text, DocumentKind.TASKS).ok


def test_leaf_task_without_a_criteria_reference():
    text = _replace(VALID_TASKS, "    - _Requirements: 1.1, 1.2_\n", "")
    _assert_violation(
        _check(text, DocumentKind.TASKS),
        rules.TASK_REFERENCE_MISSING,
        _line_of(text, "1.2 Report"),
    )


def test_a_parent_task_needs_no_criteria_reference():
    assert not _check(VALID_TASKS, DocumentKind.TASKS).for_rule(rules.TASK_REFERENCE_MISSING)


def test_a_top_level_task_with_no_children_is_a_leaf():
    text = VALID_TASKS + "- [ ] 2. A standalone task\n    - Some detail\n"
    _assert_violation(
        _check(text, DocumentKind.TASKS),
        rules.TASK_REFERENCE_MISSING,
        _line_of(text, "2. A standalone task"),
    )


@pytest.mark.parametrize(
    "reference",
    [
        "    - _Requirements: one point one_",
        "    - _Requirements: _",
        "    - _Requirements: 1.1",
        "    _Requirements: 1.1_",
    ],
    ids=["words", "empty", "unclosed", "no-bullet"],
)
def test_malformed_criteria_reference(reference):
    original = "    - _Requirements: 1.1_"
    text = _replace(VALID_TASKS, original, reference)
    report = _check(text, DocumentKind.TASKS)
    _assert_violation(
        report,
        rules.TASK_REFERENCE_MALFORMED,
        _line_of(VALID_TASKS, original, exact=True),
    )
    # A reference that is present but wrong is malformed, never also missing.
    assert not report.for_rule(rules.TASK_REFERENCE_MISSING)


def test_a_bullet_without_the_underscore_marker_is_not_a_reference():
    """The underscores are the annotation's syntax, not decoration.

    A detail bullet that merely mentions requirements is prose, so the leaf is
    reported as unreferenced rather than as carrying a broken reference.
    """
    text = _replace(VALID_TASKS, "    - _Requirements: 1.1_", "    - Requirements to gather")
    report = _check(text, DocumentKind.TASKS)
    assert report.rule_ids == (rules.TASK_REFERENCE_MISSING,)


def test_a_whole_requirement_may_be_referenced():
    text = _replace(VALID_TASKS, "    - _Requirements: 1.1_", "    - _Requirements: 1_")
    assert _check(text, DocumentKind.TASKS).ok


# --- Completeness ----------------------------------------------------------


def test_every_violation_in_one_document_is_reported():
    """Three independent defects yield three findings from one pass."""
    text = VALID_REQUIREMENTS
    text = _replace(text, "### Requirement 1:", "### Requirement 5:")
    text = _replace(
        text,
        "**User Story:** As a developer, I want rules as code, so that every driver agrees.",
        "**User Story:** Rules as code would be nice.",
    )
    text = _replace(
        text,
        "2. IF a document fails validation, THEN THE Spec_Engine SHALL report every violation.",
        "2. IF a document fails validation, the engine says so.",
    )
    report = _check(text, DocumentKind.REQUIREMENTS)
    assert set(report.rule_ids) == {
        rules.REQUIREMENT_NUMBER_NOT_SEQUENTIAL,
        rules.USER_STORY_MALFORMED,
        rules.CRITERION_SHALL_MISSING,
        rules.CRITERION_IF_WITHOUT_THEN,
    }


def test_violations_are_ordered_down_the_document():
    text = _replace(VALID_REQUIREMENTS, "### Requirement 1:", "### Requirement 9:")
    text = _replace(text, "THE Spec_Engine SHALL validate", "THE Spec_Engine validates")
    report = _check(text, DocumentKind.REQUIREMENTS)
    assert [v.location.line for v in report] == sorted(v.location.line for v in report)


# --- Report and location surface ------------------------------------------


def test_report_partitions_errors_and_warnings():
    text = _replace(VALID_REQUIREMENTS, "The engine validates native spec documents.\n", "")
    text = _replace(text, "### Requirement 1:", "### Requirement 4:")
    report = _check(text, DocumentKind.REQUIREMENTS)
    assert [v.rule for v in report.errors] == [rules.REQUIREMENT_NUMBER_NOT_SEQUENTIAL]
    assert [v.rule for v in report.warnings] == [rules.SECTION_EMPTY]
    assert not report.ok


def test_rule_ids_deduplicate_in_first_seen_order():
    # Two parents that each keep a real child, so the only defect is the pair of
    # orphaned subtask numbers.
    text = (
        "# Implementation Plan\n\n## Tasks\n\n"
        "- [ ] 1. Validator\n"
        "  - [ ] 1.1 Parse\n"
        "    - _Requirements: 1.1_\n"
        "  - [ ] 5.1 First orphan\n"
        "    - _Requirements: 1.1_\n"
        "- [ ] 2. Phases\n"
        "  - [ ] 2.1 Derive\n"
        "    - _Requirements: 2.1_\n"
        "  - [ ] 6.1 Second orphan\n"
        "    - _Requirements: 2.1_\n"
    )
    report = _check(text, DocumentKind.TASKS)
    assert report.rule_ids == (rules.TASK_PARENT_UNKNOWN,)
    assert len(report.for_rule(rules.TASK_PARENT_UNKNOWN)) == 2


def test_violation_renders_file_location_rule_and_message():
    text = _replace(VALID_TASKS, "  - [ ] 1.1 Parse", "  - [ ] 4.1 Parse")
    report = _check(text, DocumentKind.TASKS)
    line = _line_of(text, "4.1 Parse")
    assert str(report.violations[0]) == (
        f"tasks.md:{line}: error: native.tasks.parent-unknown: "
        "Task 4.1 names parent task 4, which is not declared."
    )


@pytest.mark.parametrize("line,column", [(0, None), (-1, None), (1, 0)])
def test_locations_are_one_based(line, column):
    with pytest.raises(ValueError):
        Location(line=line, column=column)


def test_location_renders_with_and_without_a_column():
    assert str(Location(line=7)) == "7"
    assert str(Location(line=7, column=3)) == "7:3"


# --- Path entry points -----------------------------------------------------


@pytest.mark.parametrize(
    "name,kind",
    [
        ("requirements.md", DocumentKind.REQUIREMENTS),
        ("design.md", DocumentKind.DESIGN),
        ("tasks.md", DocumentKind.TASKS),
        ("Tasks.MD", DocumentKind.TASKS),
        ("notes.md", None),
    ],
)
def test_kind_for_filename(name, kind):
    assert kind_for_filename(name) is kind


def test_validate_document_infers_the_kind_from_the_filename(tmp_path):
    path = tmp_path / "requirements.md"
    path.write_text(VALID_REQUIREMENTS, encoding="utf-8")
    report = validate_document(path)
    assert report.ok
    assert report.for_file(str(path)) == report.violations


def test_validate_document_refuses_a_name_it_cannot_classify(tmp_path):
    path = tmp_path / "notes.md"
    path.write_text(VALID_REQUIREMENTS, encoding="utf-8")
    with pytest.raises(ValueError, match="notes.md"):
        validate_document(path)


def test_validate_document_accepts_an_explicit_kind(tmp_path):
    path = tmp_path / "notes.md"
    path.write_text(VALID_REQUIREMENTS, encoding="utf-8")
    assert validate_document(path, kind=DocumentKind.REQUIREMENTS).ok


def test_validate_documents_merges_reports_grouped_by_file(tmp_path):
    (tmp_path / "requirements.md").write_text(
        _replace(VALID_REQUIREMENTS, "### Requirement 1:", "### Requirement 2:"),
        encoding="utf-8",
    )
    (tmp_path / "tasks.md").write_text(
        _replace(VALID_TASKS, "    - _Requirements: 1.1, 1.2_\n", ""), encoding="utf-8"
    )
    report = validate_documents([tmp_path / "requirements.md", tmp_path / "tasks.md"])
    assert set(report.rule_ids) == {
        rules.REQUIREMENT_NUMBER_NOT_SEQUENTIAL,
        rules.TASK_REFERENCE_MISSING,
    }
    assert [v.file for v in report] == sorted(v.file for v in report)


# --- The published rule vocabulary ----------------------------------------


def test_every_emitted_rule_is_registered():
    """Pins the published identifier set.

    A validator that emitted an unregistered identifier would produce findings
    no driver could describe, and silently renaming one would break every caller
    that routes on it. Both are made deliberate by having to edit this list.
    """
    assert sorted(rules.ALL_RULES) == [
        "native.coverage.criterion-uncovered",
        "native.coverage.requirement-uncovered",
        "native.criterion.if-without-then",
        "native.criterion.keyword-missing",
        "native.criterion.shall-missing",
        "native.document.empty",
        "native.document.title-duplicate",
        "native.document.title-mismatch",
        "native.document.title-missing",
        "native.graph.block-missing",
        "native.graph.cycle",
        "native.graph.dependencies-invalid",
        "native.graph.dependency-order",
        "native.graph.dependency-unknown",
        "native.graph.json-malformed",
        "native.graph.root-invalid",
        "native.graph.task-duplicate",
        "native.graph.task-id-malformed",
        "native.graph.task-not-leaf",
        "native.graph.task-unassigned",
        "native.graph.task-unknown",
        "native.graph.wave-id-not-sequential",
        "native.graph.wave-invalid",
        "native.graph.waves-empty",
        "native.heading.malformed",
        "native.requirements.criteria-empty",
        "native.requirements.criteria-section-missing",
        "native.requirements.criterion-number-not-sequential",
        "native.requirements.heading-malformed",
        "native.requirements.none",
        "native.requirements.number-not-sequential",
        "native.requirements.title-missing",
        "native.requirements.user-story-malformed",
        "native.requirements.user-story-missing",
        "native.section.duplicate",
        "native.section.empty",
        "native.section.missing",
        "native.tasks.checkbox-malformed",
        "native.tasks.indent-invalid",
        "native.tasks.none",
        "native.tasks.number-depth",
        "native.tasks.number-depth-mismatch",
        "native.tasks.number-duplicate",
        "native.tasks.number-missing",
        "native.tasks.parent-unknown",
        "native.tasks.requirements-ref-criterion-unknown",
        "native.tasks.requirements-ref-malformed",
        "native.tasks.requirements-ref-missing",
        "native.tasks.requirements-ref-requirement-unknown",
        "native.tasks.title-missing",
    ]


def test_every_registered_rule_carries_a_requirement_statement():
    assert all(rules.describe(rule).strip() for rule in rules.ALL_RULES)
    assert rules.describe("native.not.a.rule") == ""
