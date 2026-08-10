"""Cross-document rules: task links, requirement coverage, and the waves graph.

Each case mutates a clean pair of documents in exactly one way and asserts the
rule identifier and the line the finding lands on. The identifier is the
published contract a driver routes on, so asserting it rather than the wording is
what makes these tests hold while messages are reworded.

Findings are read off the cross-document checks directly, so a case is not
confused by the format findings its mutation may also produce; the entry-point
cases at the end cover the two layers arriving in one report.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew.apps.builtins.spec_engine.engine import (
    Severity,
    check_cross_document,
    parse_requirements,
    parse_tasks,
    rules,
    validate_spec,
    validate_tasks,
)

REQUIREMENTS = """\
# Requirements Document

## Introduction

The engine validates spec documents.

## Requirements

### Requirement 1: Rules as code

**User Story:** As a developer, I want rules as code, so that drivers agree.

#### Acceptance Criteria

1. WHEN a document is submitted, THE Spec_Engine SHALL validate its format.
2. IF a document fails validation, THEN THE Spec_Engine SHALL report violations.

### Requirement 2: Coverage

**User Story:** As an owner, I want coverage reported, so that nothing is lost.

#### Acceptance Criteria

1. WHEN tasks are validated, THE Spec_Engine SHALL report uncovered requirements.
"""

TASKS = """\
# Implementation Plan

## Tasks

- [x] 1. Parser
  - [x] 1.1 Read the documents
    - _Requirements: 1.1_
- [ ] 2. Checks
  - [ ] 2.1 Resolve the links
    - _Requirements: 1.2_
  - [ ] 2.2 Report the coverage
    - _Requirements: 2.1_

## Task Dependency Graph

```json
{"waves": [
  {"id": 0, "tasks": ["1.1", "2.1"]},
  {"id": 1, "tasks": ["2.2"]}
]}
```
"""

#: Lines the fixture's findings are asserted against, so a fixture edit that
#: shifts them fails loudly here instead of quietly moving every expectation.
LINE_REQUIREMENT_2 = 18
LINE_CRITERION_1_2 = 16
LINE_CRITERION_2_1 = 24
LINE_TASK_2_2 = 11
LINE_REFERENCE_1_2 = 10
LINE_GRAPH_HEADING = 14
LINE_GRAPH_BODY = 17
LINE_WAVE_0 = 18
LINE_WAVE_1 = 19


def _replace(text: str, old: str, new: str) -> str:
    """Substitute exactly one occurrence, failing loudly if the anchor moved."""
    assert text.count(old) == 1, f"fixture anchor {old!r} is not unique"
    return text.replace(old, new)


def _found(requirements: str = REQUIREMENTS, tasks: str = TASKS):
    """Run the cross-document checks over a pair of documents."""
    return check_cross_document(
        parse_requirements(requirements),
        parse_tasks(tasks),
        requirements_file="requirements.md",
        tasks_file="tasks.md",
    )


def _reported(requirements: str = REQUIREMENTS, tasks: str = TASKS):
    return [(v.rule, v.file, v.location.line) for v in _found(requirements, tasks)]


def _block(body: str) -> str:
    """Replace the fixture's graph block body with ``body``."""
    start = TASKS.index("```json\n") + len("```json\n")
    end = TASKS.index("```\n", start)
    return TASKS[:start] + body + "\n" + TASKS[end:]


def _graph(waves: object, **extra: object) -> str:
    """Rewrite the graph block, keeping one wave per line as the fixture has it.

    The layout matters: findings address the line a wave or a task sits on, so a
    reformatted block would move them.
    """
    assert isinstance(waves, list)
    entries = ",\n  ".join(json.dumps(wave) for wave in waves)
    body = '{"waves": [\n  ' + entries + "\n ]"
    for key, value in extra.items():
        body += f',\n "{key}": ' + json.dumps(value)
    return _block(body + "\n}")


def test_clean_documents_report_nothing():
    assert _reported() == []


def test_the_fixture_lines_are_where_the_cases_say_they_are():
    """Anchor the line constants, so a fixture edit fails here first."""
    requirements = REQUIREMENTS.splitlines()
    tasks = TASKS.splitlines()
    assert requirements[LINE_REQUIREMENT_2 - 1].startswith("### Requirement 2:")
    assert requirements[LINE_CRITERION_1_2 - 1].startswith("2. IF a document fails")
    assert requirements[LINE_CRITERION_2_1 - 1].startswith("1. WHEN tasks are validated")
    assert "2.2 Report the coverage" in tasks[LINE_TASK_2_2 - 1]
    assert tasks[LINE_REFERENCE_1_2 - 1].strip() == "- _Requirements: 1.2_"
    assert tasks[LINE_GRAPH_HEADING - 1] == "## Task Dependency Graph"
    assert tasks[LINE_GRAPH_BODY - 1].startswith('{"waves"')


# --- Task links ------------------------------------------------------------


def test_a_reference_to_an_undeclared_requirement_is_unresolvable():
    tasks = _replace(TASKS, "_Requirements: 1.2_", "_Requirements: 9.1_")
    assert _reported(tasks=tasks) == [
        (rules.TASK_REFERENCE_REQUIREMENT_UNKNOWN, "tasks.md", LINE_REFERENCE_1_2),
        (rules.COVERAGE_CRITERION_UNCOVERED, "requirements.md", LINE_CRITERION_1_2),
    ]


def test_a_reference_to_an_undeclared_criterion_is_unresolvable():
    tasks = _replace(TASKS, "_Requirements: 1.2_", "_Requirements: 1.9_")
    reported = _reported(tasks=tasks)
    assert (rules.TASK_REFERENCE_CRITERION_UNKNOWN, "tasks.md", LINE_REFERENCE_1_2) in reported


def test_an_unresolvable_criterion_names_the_criteria_that_do_exist():
    tasks = _replace(TASKS, "_Requirements: 1.2_", "_Requirements: 1.9_")
    violation = next(v for v in _found(tasks=tasks))
    assert violation.rule == rules.TASK_REFERENCE_CRITERION_UNKNOWN
    assert "criteria: 1, 2" in violation.message


def test_a_reference_naming_only_a_requirement_covers_all_of_it():
    """A task claiming a whole requirement leaves none of its criteria open."""
    tasks = _replace(TASKS, "_Requirements: 1.2_", "_Requirements: 1_")
    assert _reported(tasks=tasks) == []


def test_every_unresolvable_reference_is_reported_not_just_the_first():
    tasks = _replace(TASKS, "_Requirements: 1.2_", "_Requirements: 8.1, 9.1_")
    unresolved = [
        v for v in _found(tasks=tasks) if v.rule == rules.TASK_REFERENCE_REQUIREMENT_UNKNOWN
    ]
    assert len(unresolved) == 2


# --- Coverage --------------------------------------------------------------


def test_a_requirement_no_task_claims_is_an_error():
    tasks = _replace(TASKS, "_Requirements: 2.1_", "_Requirements: 1.1_")
    violation = next(v for v in _found(tasks=tasks))
    assert violation.rule == rules.COVERAGE_REQUIREMENT_UNCOVERED
    assert violation.file == "requirements.md"
    assert violation.location.line == LINE_REQUIREMENT_2
    assert violation.severity is Severity.ERROR


def test_a_wholly_uncovered_requirement_is_not_also_reported_per_criterion():
    """The requirement is the finding; repeating it per criterion buries it."""
    tasks = _replace(TASKS, "_Requirements: 2.1_", "_Requirements: 1.1_")
    assert len(_found(tasks=tasks)) == 1


def test_an_uncovered_criterion_of_a_covered_requirement_is_a_warning():
    tasks = _replace(TASKS, "_Requirements: 1.2_", "_Requirements: 1.1_")
    violation = next(v for v in _found(tasks=tasks))
    assert violation.rule == rules.COVERAGE_CRITERION_UNCOVERED
    assert violation.location.line == LINE_CRITERION_1_2
    assert violation.severity is Severity.WARNING


def test_coverage_counts_a_reference_from_any_task_not_only_a_leaf():
    tasks = _replace(
        TASKS,
        "- [ ] 2. Checks\n",
        "- [ ] 2. Checks\n  - _Requirements: 2.1_\n",
    )
    tasks = _replace(tasks, "    - _Requirements: 2.1_\n", "    - _Requirements: 1.1_\n")
    assert [rule for rule, _, _ in _reported(tasks=tasks)] == []


# --- Dependency graph: structure -------------------------------------------


def test_a_graph_section_with_no_block_is_reported():
    tasks = TASKS[: TASKS.index("```json")]
    assert _reported(tasks=tasks) == [
        (rules.GRAPH_BLOCK_MISSING, "tasks.md", LINE_GRAPH_HEADING),
    ]


def test_a_document_with_no_graph_section_is_not_a_graph_defect():
    """The graph is an addition to the native document, not a requirement."""
    tasks = TASKS[: TASKS.index("## Task Dependency Graph")]
    assert _reported(tasks=tasks) == []


def test_unreadable_json_is_reported_at_the_line_it_breaks_on():
    tasks = _replace(TASKS, '{"id": 1, "tasks": ["2.2"]}', '{"id": 1 "tasks": ["2.2"]}')
    assert _reported(tasks=tasks) == [
        (rules.GRAPH_JSON_MALFORMED, "tasks.md", LINE_WAVE_1),
    ]


@pytest.mark.parametrize(
    "body",
    ['["1.1"]', '{"stages": []}', '{"waves": {"0": ["1.1"]}}', "null"],
    ids=["list", "wrong-key", "waves-not-a-list", "null"],
)
def test_a_graph_that_is_not_an_object_with_waves_is_reported(body):
    assert (rules.GRAPH_ROOT_INVALID, "tasks.md", LINE_GRAPH_BODY) in _reported(tasks=_block(body))


def test_a_graph_declaring_no_waves_is_reported():
    assert (rules.GRAPH_WAVES_EMPTY, "tasks.md", LINE_GRAPH_BODY) in _reported(tasks=_graph([]))


@pytest.mark.parametrize(
    "wave",
    [
        ["1.1"],
        {"tasks": ["1.1"]},
        {"id": 0},
        {"id": 0, "tasks": []},
        {"id": "0", "tasks": ["1.1"]},
        {"id": True, "tasks": ["1.1"]},
        {"id": 0, "tasks": "1.1"},
    ],
    ids=["not-an-object", "no-id", "no-tasks", "empty", "id-string", "id-bool", "tasks-string"],
)
def test_a_wave_that_is_not_a_wave_is_reported(wave):
    reported = _reported(tasks=_graph([wave, {"id": 1, "tasks": ["2.1", "2.2"]}]))
    assert rules.GRAPH_WAVE_INVALID in {rule for rule, _, _ in reported}


def test_wave_identifiers_must_count_from_zero_without_gaps():
    reported = _reported(
        tasks=_graph([{"id": 1, "tasks": ["1.1", "2.1"]}, {"id": 3, "tasks": ["2.2"]}])
    )
    assert [rule for rule, _, _ in reported] == [
        rules.GRAPH_WAVE_ID_NOT_SEQUENTIAL,
        rules.GRAPH_WAVE_ID_NOT_SEQUENTIAL,
    ]


def test_a_repeated_wave_identifier_breaks_the_sequence():
    reported = _reported(
        tasks=_graph([{"id": 0, "tasks": ["1.1", "2.1"]}, {"id": 0, "tasks": ["2.2"]}])
    )
    assert [rule for rule, _, _ in reported] == [rules.GRAPH_WAVE_ID_NOT_SEQUENTIAL]


@pytest.mark.parametrize("entry", [2, None, "", "2.", "two", "1.1 ", ["2.2"]])
def test_a_task_entry_that_is_not_a_task_number_is_reported(entry):
    reported = _reported(
        tasks=_graph([{"id": 0, "tasks": ["1.1", "2.1"]}, {"id": 1, "tasks": [entry, "2.2"]}])
    )
    assert rules.GRAPH_TASK_ID_MALFORMED in {rule for rule, _, _ in reported}


# --- Dependency graph: schedule -------------------------------------------


def test_scheduling_a_task_the_plan_does_not_declare_is_reported():
    reported = _reported(
        tasks=_graph([{"id": 0, "tasks": ["1.1", "2.1"]}, {"id": 1, "tasks": ["2.2", "9.9"]}])
    )
    assert (rules.GRAPH_TASK_UNKNOWN, "tasks.md", LINE_WAVE_1) in reported


def test_scheduling_a_parent_task_is_reported():
    reported = _reported(
        tasks=_graph([{"id": 0, "tasks": ["1.1", "2.1"]}, {"id": 1, "tasks": ["2.2", "2"]}])
    )
    assert rules.GRAPH_TASK_NOT_LEAF in {rule for rule, _, _ in reported}


def test_a_task_scheduled_twice_is_reported_at_its_second_wave():
    reported = _reported(
        tasks=_graph([{"id": 0, "tasks": ["1.1", "2.1"]}, {"id": 1, "tasks": ["2.1", "2.2"]}])
    )
    assert (rules.GRAPH_TASK_DUPLICATE, "tasks.md", LINE_WAVE_1) in reported


def test_an_unfinished_leaf_in_no_wave_is_reported_at_the_task():
    reported = _reported(tasks=_graph([{"id": 0, "tasks": ["1.1", "2.1"]}]))
    assert reported == [(rules.GRAPH_TASK_UNASSIGNED, "tasks.md", LINE_TASK_2_2)]


def test_a_finished_leaf_needs_no_wave():
    """The graph schedules remaining work, so a done task may be left out."""
    assert _reported(tasks=_graph([{"id": 0, "tasks": ["2.1"]}, {"id": 1, "tasks": ["2.2"]}])) == []


def test_a_parent_task_needs_no_wave():
    """Only leaves are units of work, so an unscheduled parent is not a hole."""
    reported = _reported()
    assert rules.GRAPH_TASK_UNASSIGNED not in {rule for rule, _, _ in reported}


# --- Dependency graph: declared edges -------------------------------------


def test_dependencies_pointing_at_earlier_waves_report_nothing():
    tasks = _graph(
        [{"id": 0, "tasks": ["1.1", "2.1"]}, {"id": 1, "tasks": ["2.2"]}],
        dependencies={"2.2": ["1.1", "2.1"]},
    )
    assert _reported(tasks=tasks) == []


def test_a_dependency_no_wave_schedules_is_reported():
    tasks = _graph(
        [{"id": 0, "tasks": ["1.1", "2.1"]}, {"id": 1, "tasks": ["2.2"]}],
        dependencies={"2.2": ["9.9"]},
    )
    assert rules.GRAPH_DEPENDENCY_UNKNOWN in {rule for rule, _, _ in _reported(tasks=tasks)}


def test_a_task_declaring_dependencies_but_scheduled_nowhere_is_reported():
    tasks = _graph(
        [{"id": 0, "tasks": ["1.1", "2.1"]}, {"id": 1, "tasks": ["2.2"]}],
        dependencies={"9.9": ["1.1"]},
    )
    assert rules.GRAPH_DEPENDENCY_UNKNOWN in {rule for rule, _, _ in _reported(tasks=tasks)}


@pytest.mark.parametrize("needed", ["2.2", "1.1"], ids=["same-wave", "later-wave"])
def test_a_dependency_that_does_not_run_earlier_is_reported(needed):
    tasks = _graph(
        [{"id": 0, "tasks": ["2.1"]}, {"id": 1, "tasks": ["1.1", "2.2"]}],
        dependencies={"2.2": [needed]},
    )
    assert rules.GRAPH_DEPENDENCY_ORDER in {rule for rule, _, _ in _reported(tasks=tasks)}


@pytest.mark.parametrize(
    "dependencies",
    ["[]", '{"2.2": "1.1"}', '{"2.2": {"needs": "1.1"}}'],
    ids=["list", "string", "object"],
)
def test_dependencies_that_are_not_a_task_to_task_mapping_are_reported(dependencies):
    body = '{"waves": [\n  {"id": 0, "tasks": ["1.1", "2.1", "2.2"]}\n ],\n'
    body += f' "dependencies": {dependencies}\n}}'
    assert rules.GRAPH_DEPENDENCIES_INVALID in {
        rule for rule, _, _ in _reported(tasks=_block(body))
    }


def test_a_cycle_in_the_declared_dependencies_is_reported_with_its_path():
    tasks = _graph(
        [{"id": 0, "tasks": ["1.1", "2.1"]}, {"id": 1, "tasks": ["2.2"]}],
        dependencies={"2.1": ["2.2"], "2.2": ["2.1"]},
    )
    cycles = [v for v in _found(tasks=tasks) if v.rule == rules.GRAPH_CYCLE]
    assert len(cycles) == 1
    assert "2.1 -> 2.2 -> 2.1" in cycles[0].message or "2.2 -> 2.1 -> 2.2" in cycles[0].message


def test_a_task_depending_on_itself_is_a_cycle():
    tasks = _graph(
        [{"id": 0, "tasks": ["1.1", "2.1"]}, {"id": 1, "tasks": ["2.2"]}],
        dependencies={"2.2": ["2.2"]},
    )
    assert rules.GRAPH_CYCLE in {rule for rule, _, _ in _reported(tasks=tasks)}


def test_a_long_dependency_chain_is_not_mistaken_for_a_cycle():
    tasks = _graph(
        [
            {"id": 0, "tasks": ["1.1"]},
            {"id": 1, "tasks": ["2.1"]},
            {"id": 2, "tasks": ["2.2"]},
        ],
        dependencies={"2.2": ["2.1"], "2.1": ["1.1"]},
    )
    assert _reported(tasks=tasks) == []


# --- Entry points ----------------------------------------------------------


def _write_spec(directory: Path, *, requirements: str = REQUIREMENTS, tasks: str = TASKS) -> Path:
    spec = directory / ".kiro" / "specs" / "example"
    spec.mkdir(parents=True)
    (spec / "requirements.md").write_text(requirements, encoding="utf-8")
    (spec / "tasks.md").write_text(tasks, encoding="utf-8")
    return spec


def test_validating_tasks_alone_still_reports_uncovered_requirements(tmp_path):
    spec = _write_spec(
        tmp_path, tasks=_replace(TASKS, "_Requirements: 2.1_", "_Requirements: 1.1_")
    )
    report = validate_tasks(
        spec / "tasks.md",
        requirements_path=spec / "requirements.md",
        tasks_file="tasks.md",
        requirements_file="requirements.md",
    )
    assert report.rule_ids == (rules.COVERAGE_REQUIREMENT_UNCOVERED,)
    assert not report.ok


def test_validating_tasks_alone_does_not_report_defects_in_the_requirements(tmp_path):
    """The caller submitted one document; findings in the other are not theirs."""
    spec = _write_spec(tmp_path, requirements=_replace(REQUIREMENTS, "## Introduction", "Intro"))
    report = validate_tasks(
        spec / "tasks.md",
        requirements_path=spec / "requirements.md",
        tasks_file="tasks.md",
    )
    assert report.rule_ids == ()


def test_validating_a_spec_reports_format_and_cross_document_findings_together(tmp_path):
    tasks = _replace(TASKS, "  - [ ] 2.2 Report the coverage\n", "  - [ ] 2.2\n")
    tasks = _replace(tasks, "_Requirements: 2.1_", "_Requirements: 2.9_")
    spec = _write_spec(tmp_path, tasks=tasks)
    report = validate_spec(spec, relative=True)
    assert set(report.rule_ids) == {
        rules.TASK_TITLE_MISSING,
        rules.TASK_REFERENCE_CRITERION_UNKNOWN,
        rules.COVERAGE_REQUIREMENT_UNCOVERED,
    }
    assert {v.file for v in report} == {"tasks.md", "requirements.md"}


def test_validating_a_spec_orders_the_whole_report_by_file_and_line(tmp_path):
    spec = _write_spec(
        tmp_path, tasks=_replace(TASKS, "_Requirements: 1.2_", "_Requirements: 1.9_")
    )
    report = validate_spec(spec, relative=True)
    assert list(report) == sorted(report, key=lambda v: v.sort_key)


def test_a_spec_with_no_requirements_document_skips_the_link_and_coverage_checks(tmp_path):
    """Which documents a spec owes is a property of its type, decided elsewhere."""
    spec = _write_spec(
        tmp_path, tasks=_replace(TASKS, "_Requirements: 2.1_", "_Requirements: 9.9_")
    )
    (spec / "requirements.md").unlink()
    assert validate_spec(spec).rule_ids == ()


def test_a_spec_with_no_requirements_document_still_validates_its_graph(tmp_path):
    """A spec type owing no requirements document still hands over a schedule.

    The graph reads tasks.md alone, so gating it on the other document would
    leave such a spec's schedule permanently unchecked.
    """
    tasks = _graph([{"id": 0, "tasks": ["1.1", "2.1"]}, {"id": 5, "tasks": ["2.2", "2.1", "9.9"]}])
    spec = _write_spec(tmp_path, tasks=tasks)
    (spec / "requirements.md").unlink()
    assert set(validate_spec(spec).rule_ids) == {
        rules.GRAPH_WAVE_ID_NOT_SEQUENTIAL,
        rules.GRAPH_TASK_DUPLICATE,
        rules.GRAPH_TASK_UNKNOWN,
    }


def test_validating_a_spec_reports_full_paths_by_default(tmp_path):
    spec = _write_spec(
        tmp_path, tasks=_replace(TASKS, "_Requirements: 2.1_", "_Requirements: 1.1_")
    )
    report = validate_spec(spec)
    assert {v.file for v in report} == {str(spec / "requirements.md")}


def test_every_reported_rule_is_a_registered_identifier(tmp_path):
    spec = _write_spec(
        tmp_path,
        tasks=_replace(TASKS, "_Requirements: 2.1_", "_Requirements: 9.9_"),
    )
    report = validate_spec(spec)
    assert report.rule_ids
    for violation in report:
        assert violation.rule in rules.ALL_RULES
        assert rules.describe(violation.rule)
