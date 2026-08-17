"""The cross-document checks against the real spec, and their exact behaviour.

Two guarantees live here that per-rule fixtures cannot reach.

The repository's own spec is the positive case. Its documents are format-clean
and its dependency graph is healthy, so any finding beyond an advisory coverage
gap is a false positive that would block authoring. That spec also legitimately
orders task 19 before 18, which is why nothing here reads anything into the order
tasks are declared in.

The checks are exact, not merely present. Coverage, wave membership, wave
numbering, and acyclicity are each restated from the generated inputs and
compared against what the checks report, so a check that fires too often is
caught alongside one that never fires.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Sequence

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from kiro_crew.apps.builtins.spec_engine.engine import (
    Severity,
    check_cross_document,
    check_dependency_graph,
    check_requirement_coverage,
    check_task_links,
    parse_requirements,
    parse_tasks,
    rules,
    validate_spec,
    validate_tasks,
)

#: The repository's own spec, which doubles as the corpus the checks must not
#: fire on.
_SPEC_DIR = (
    # tests -> spec_engine -> builtins -> apps -> kiro_crew -> src -> repository
    Path(__file__).resolve().parents[6]
    / ".kiro"
    / "specs"
    / "agent-agnostic-spec-engine"
)

_GRAPH_RULES = frozenset(rule for rule in rules.ALL_RULES if rule.startswith("native.graph."))


def _spec_dir() -> Path:
    if not (_SPEC_DIR / "tasks.md").is_file():
        pytest.skip(f"{_SPEC_DIR} is not present in this checkout")
    return _SPEC_DIR


# --- The real spec ---------------------------------------------------------


def test_the_repositorys_own_spec_carries_no_blocking_finding():
    report = validate_spec(_spec_dir(), relative=True)
    assert not report.errors, "\n".join(str(v) for v in report.errors)


def test_the_repositorys_own_dependency_graph_is_healthy():
    """The corpus schedules every unfinished leaf exactly once, in order."""
    report = validate_spec(_spec_dir(), relative=True)
    assert [v for v in report if v.rule in _GRAPH_RULES] == []


def test_the_repositorys_own_spec_has_a_graph_to_check():
    """Guards the case above from passing because nothing was parsed.

    The anti-vacuity claim is that a real schedule was read and that it is this
    plan's schedule: a non-empty set of task numbers the checklist declares,
    covering every leaf the graph rules examine. It deliberately says nothing
    about how much of the plan remains open, because a finished spec is the
    healthiest state the corpus can be in and must not read as an empty check.
    """
    plan = parse_tasks((_spec_dir() / "tasks.md").read_text(encoding="utf-8"))
    assert plan.graph_block is not None
    scheduled = {
        task for wave in json.loads(plan.graph_block.body)["waves"] for task in wave["tasks"]
    }
    assert scheduled
    assert scheduled <= {task.number for task in plan.leaves}
    unfinished = {task.number for task in plan.leaves if not task.complete}
    assert unfinished <= scheduled


def test_every_advisory_finding_on_the_real_spec_is_an_uncovered_criterion():
    """Any warning must point at the criterion it names, in requirements.md."""
    report = validate_spec(_spec_dir(), relative=True)
    lines = (_spec_dir() / "requirements.md").read_text(encoding="utf-8").splitlines()
    for violation in report.warnings:
        assert violation.rule == rules.COVERAGE_CRITERION_UNCOVERED
        assert violation.file == "requirements.md"
        identifier = violation.message.rsplit(" ", 1)[-1].rstrip(".")
        criterion = identifier.split(".")[-1]
        assert lines[violation.location.line - 1].startswith(f"{criterion}. ")


def test_validating_the_real_tasks_alone_reports_the_same_coverage():
    """Coverage does not depend on whether the whole spec was submitted."""
    spec = _spec_dir()
    whole = validate_spec(spec, relative=True)
    alone = validate_tasks(
        spec / "tasks.md",
        requirements_path=spec / "requirements.md",
        tasks_file="tasks.md",
        requirements_file="requirements.md",
    )
    coverage = {
        rules.COVERAGE_REQUIREMENT_UNCOVERED,
        rules.COVERAGE_CRITERION_UNCOVERED,
    }
    assert [str(v) for v in whole if v.rule in coverage] == [
        str(v) for v in alone if v.rule in coverage
    ]


def test_dropping_a_reference_from_the_real_plan_opens_a_coverage_gap():
    """Mutating the real documents, not a fixture, still finds the hole."""
    spec = _spec_dir()
    requirements = (spec / "requirements.md").read_text(encoding="utf-8")
    tasks = (spec / "tasks.md").read_text(encoding="utf-8")
    index = parse_requirements(requirements)
    plan = parse_tasks(tasks)
    lines = tasks.splitlines()

    # A criterion referenced exactly once, on a line that names others too:
    # dropping it uncovers that criterion and leaves the line well-formed.
    counts: dict[str, int] = {}
    for task in plan.tasks:
        for reference in task.references:
            if reference.criterion is not None:
                counts[reference.text] = counts.get(reference.text, 0) + 1
    target, line_number, listed = next(
        (text, reference.line, ids)
        for text, count in sorted(counts.items())
        if count == 1
        for task in plan.tasks
        for reference in task.references
        if reference.text == text
        for ids in [[token.strip() for token in _reference_ids(lines[reference.line - 1])]]
        if len(ids) > 1
    )

    kept = ", ".join(identifier for identifier in listed if identifier != target)
    mutated = list(lines)
    mutated[line_number - 1] = f"    - _Requirements: {kept}_"
    before = check_requirement_coverage(index, plan, requirements_file="requirements.md")
    after = check_requirement_coverage(
        index, parse_tasks("\n".join(mutated)), requirements_file="requirements.md"
    )
    opened = {v.message for v in after} - {v.message for v in before}
    assert opened == {f"No task references criterion {target}."}


def _reference_ids(line: str) -> list[str]:
    return line.split("_Requirements:")[1].strip().rstrip("_").split(",")


def test_renumbering_a_real_wave_is_caught():
    spec = _spec_dir()
    tasks = (spec / "tasks.md").read_text(encoding="utf-8")
    mutated = tasks.replace('{"id": 1, "tasks"', '{"id": 4, "tasks"', 1)
    assert mutated != tasks
    found = check_dependency_graph(parse_tasks(mutated), tasks_file="tasks.md")
    assert [v.rule for v in found] == [rules.GRAPH_WAVE_ID_NOT_SEQUENTIAL]


def test_unscheduling_a_real_task_is_caught():
    spec = _spec_dir()
    tasks, number, line = _with_an_unfinished_leaf((spec / "tasks.md").read_text(encoding="utf-8"))
    mutated = _unschedule(tasks, number)
    found = check_dependency_graph(parse_tasks(mutated), tasks_file="tasks.md")
    assert [(v.rule, v.location.line) for v in found] == [(rules.GRAPH_TASK_UNASSIGNED, line)]


def _with_an_unfinished_leaf(tasks: str) -> tuple[str, str, int]:
    """Return ``tasks`` holding an unfinished scheduled leaf, and its identity.

    ``GRAPH_TASK_UNASSIGNED`` fires only for a leaf that is not complete, so a
    corpus spec whose work has finished carries nothing for the unscheduling
    check to bite on — and every spec finishes eventually. The leaf is therefore
    derived rather than found: one real, scheduled leaf is marked incomplete.
    That keeps the check running against the real documents (a fixture spec
    would not) and keeps it running when the corpus is at its healthiest (a skip
    would go vacuous exactly then). The checkbox flip preserves the line's
    length and the document's line count, so the line the check reports is the
    line the real file carries.
    """
    plan = parse_tasks(tasks)
    assert plan.graph_block is not None, "the corpus spec declares a dependency graph"
    scheduled = {
        task for wave in json.loads(plan.graph_block.body)["waves"] for task in wave["tasks"]
    }
    target = next(task for task in plan.leaves if task.number in scheduled)
    lines = tasks.splitlines(keepends=True)
    lines[target.line - 1] = lines[target.line - 1].replace("- [x] ", "- [ ] ", 1)
    return "".join(lines), target.number, target.line


def _unschedule(tasks: str, number: str) -> str:
    """Drop ``number`` from the graph block, leaving the JSON body well-formed.

    The trailing-separator form is tried first so that removing a task from the
    middle of a wave does not leave a doubled comma; the bare form covers a wave
    that scheduled the task alone.
    """
    for occurrence in (f'"{number}", ', f', "{number}"', f'"{number}"'):
        if occurrence in tasks:
            return tasks.replace(occurrence, "", 1)
    raise AssertionError(f"task {number} is not scheduled in the graph block")


# --- Generated documents --------------------------------------------------


def _requirements_document(criteria: Sequence[int]) -> str:
    """A clean requirements document declaring ``criteria[i]`` criteria each."""
    parts = [
        "# Requirements Document",
        "",
        "## Introduction",
        "",
        "Generated.",
        "",
        "## Requirements",
        "",
    ]
    for position, count in enumerate(criteria, start=1):
        parts += [
            f"### Requirement {position}: Generated {position}",
            "",
            "**User Story:** As a user, I want a thing, so that it helps.",
            "",
            "#### Acceptance Criteria",
            "",
        ]
        for number in range(1, count + 1):
            parts.append(f"{number}. WHEN a thing happens, THE Spec_Engine SHALL act.")
        parts.append("")
    return "\n".join(parts) + "\n"


def _tasks_document(
    leaves: Sequence[tuple[bool, Sequence[str]]],
    *,
    waves: Sequence[Sequence[str]] | None = None,
    dependencies: dict[str, list[str]] | None = None,
) -> str:
    """A clean plan of one parent task and ``leaves`` subtasks under it."""
    parts = ["# Implementation Plan", "", "## Tasks", "", "- [ ] 1. Generated", ""]
    parts.pop()
    for position, (complete, references) in enumerate(leaves, start=1):
        mark = "x" if complete else " "
        parts.append(f"  - [{mark}] 1.{position} Leaf {position}")
        if references:
            parts.append(f"    - _Requirements: {', '.join(references)}_")
    if waves is not None:
        parts += ["", "## Task Dependency Graph", "", "```json", '{"waves": [']
        entries = [
            json.dumps({"id": identifier, "tasks": list(tasks)})
            for identifier, tasks in enumerate(waves)
        ]
        parts.append("  " + ",\n  ".join(entries) if entries else "  ")
        if dependencies is None:
            parts += ["]}", "```"]
        else:
            parts += ["],", f' "dependencies": {json.dumps(dependencies)}', "}", "```"]
    return "\n".join(parts) + "\n"


def _leaf_numbers(count: int) -> list[str]:
    return [f"1.{position}" for position in range(1, count + 1)]


def _chunks(items: Sequence[str], count: int) -> list[list[str]]:
    """Split ``items`` into at most ``count`` non-empty consecutive groups.

    Empty groups are dropped rather than emitted, because a wave listing no
    tasks is its own defect and would mask the property under test.
    """
    if not items:
        return []
    size = max(1, -(-len(items) // count))
    groups = [list(items[start : start + size]) for start in range(0, len(items), size)]
    return [group for group in groups if group]


_CRITERIA_COUNTS = st.lists(st.integers(min_value=1, max_value=4), min_size=1, max_size=5)


@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(counts=_CRITERIA_COUNTS, picks=st.data())
def test_coverage_reports_exactly_what_no_task_references(counts, picks):
    """A criterion is reported when, and only when, no task references it."""
    available = [f"{r}.{c}" for r, count in enumerate(counts, 1) for c in range(1, count + 1)]
    chosen = picks.draw(st.lists(st.sampled_from(available), max_size=len(available)))
    requirements = _requirements_document(counts)
    tasks = _tasks_document([(False, chosen)] if chosen else [(False, [])])

    covered = set(chosen)
    expected_requirements = {
        requirement
        for requirement, count in enumerate(counts, 1)
        if not any(f"{requirement}.{c}" in covered for c in range(1, count + 1))
    }
    expected_criteria = {
        identifier
        for identifier in available
        if identifier not in covered and int(identifier.split(".")[0]) not in expected_requirements
    }

    found = check_requirement_coverage(
        parse_requirements(requirements),
        parse_tasks(tasks),
        requirements_file="requirements.md",
    )
    reported_requirements = {
        int(v.message.split("requirement ")[1].split(";")[0])
        for v in found
        if v.rule == rules.COVERAGE_REQUIREMENT_UNCOVERED
    }
    reported_criteria = {
        v.message.rsplit(" ", 1)[-1].rstrip(".")
        for v in found
        if v.rule == rules.COVERAGE_CRITERION_UNCOVERED
    }
    assert reported_requirements == expected_requirements
    assert reported_criteria == expected_criteria
    assert all(
        v.severity is Severity.ERROR
        for v in found
        if v.rule == rules.COVERAGE_REQUIREMENT_UNCOVERED
    )
    assert all(
        v.severity is Severity.WARNING
        for v in found
        if v.rule == rules.COVERAGE_CRITERION_UNCOVERED
    )


@settings(max_examples=100, deadline=None)
@given(
    counts=_CRITERIA_COUNTS,
    references=st.lists(
        st.tuples(st.integers(min_value=1, max_value=8), st.integers(min_value=1, max_value=8)),
        min_size=1,
        max_size=6,
    ),
)
def test_a_reference_resolves_exactly_when_requirements_declares_it(counts, references):
    """Every unresolvable reference is reported, and no resolvable one is."""
    identifiers = [f"{requirement}.{criterion}" for requirement, criterion in references]
    requirements = _requirements_document(counts)
    tasks = _tasks_document([(False, identifiers)])

    declared = {(r, c) for r, count in enumerate(counts, 1) for c in range(1, count + 1)}
    expected = {
        f"{requirement}.{criterion}"
        for requirement, criterion in references
        if (requirement, criterion) not in declared
    }
    found = check_task_links(parse_requirements(requirements), parse_tasks(tasks), tasks_file="t")
    reported = {v.message.split("references ")[1].split(",")[0] for v in found}
    assert reported == expected


@settings(max_examples=150, deadline=None)
@given(
    statuses=st.lists(st.booleans(), min_size=1, max_size=8),
    wave_count=st.integers(min_value=1, max_value=4),
    dropped=st.sets(st.integers(min_value=0, max_value=7), max_size=3),
    repeated=st.sets(st.integers(min_value=0, max_value=7), max_size=2),
)
def test_every_unfinished_leaf_is_required_in_exactly_one_wave(
    statuses, wave_count, dropped, repeated
):
    """Unassigned and duplicate findings match the schedule exactly."""
    numbers = _leaf_numbers(len(statuses))
    dropped = {index for index in dropped if index < len(numbers)}
    repeated = {index for index in repeated if index < len(numbers)} - dropped
    scheduled = [number for index, number in enumerate(numbers) if index not in dropped]
    scheduled += [numbers[index] for index in sorted(repeated)]
    # An empty wave list is its own defect and would mask the property.
    assume(scheduled)
    waves = _chunks(scheduled, wave_count)

    tasks = _tasks_document([(status, ["1.1"]) for status in statuses], waves=waves)
    found = check_dependency_graph(parse_tasks(tasks), tasks_file="tasks.md")

    expected_unassigned = {numbers[index] for index in dropped if not statuses[index]}
    reported_unassigned = {
        v.message.split("Task ")[1].split(" is")[0]
        for v in found
        if v.rule == rules.GRAPH_TASK_UNASSIGNED
    }
    reported_duplicate = {
        v.message.split("Task ")[1].split(" is")[0]
        for v in found
        if v.rule == rules.GRAPH_TASK_DUPLICATE
    }
    assert reported_unassigned == expected_unassigned
    assert reported_duplicate == {numbers[index] for index in repeated}


@settings(max_examples=100, deadline=None)
@given(identifiers=st.lists(st.integers(min_value=-2, max_value=6), min_size=1, max_size=5))
def test_wave_identifiers_are_accepted_exactly_when_they_count_from_zero(identifiers):
    """Only the consecutive-from-zero sequence passes."""
    tasks = _tasks_document([(False, ["1.1"])], waves=[["1.1"]])
    body = json.dumps(
        {"waves": [{"id": identifier, "tasks": ["1.1"]} for identifier in identifiers]}
    )
    tasks = tasks.replace('{"waves": [\n  {"id": 0, "tasks": ["1.1"]}\n]}', body)
    found = check_dependency_graph(parse_tasks(tasks), tasks_file="tasks.md")
    out_of_sequence = [v for v in found if v.rule == rules.GRAPH_WAVE_ID_NOT_SEQUENTIAL]
    assert bool(out_of_sequence) == (identifiers != list(range(len(identifiers))))


@settings(max_examples=100, deadline=None)
@given(
    leaf_count=st.integers(min_value=2, max_value=7),
    wave_count=st.integers(min_value=2, max_value=4),
    edges=st.data(),
)
def test_dependencies_pointing_backwards_are_acyclic_and_in_order(leaf_count, wave_count, edges):
    """A graph whose edges all point at earlier waves reports nothing."""
    numbers = _leaf_numbers(leaf_count)
    waves = _chunks(numbers, wave_count)
    assume(len(waves) > 1)
    wave_of = {number: index for index, wave in enumerate(waves) for number in wave}

    dependencies: dict[str, list[str]] = {}
    for number in numbers:
        earlier = [other for other in numbers if wave_of[other] < wave_of[number]]
        if earlier:
            picked = edges.draw(st.lists(st.sampled_from(earlier), max_size=2, unique=True))
            if picked:
                dependencies[number] = picked

    tasks = _tasks_document([(False, ["1.1"])] * leaf_count, waves=waves, dependencies=dependencies)
    found = check_dependency_graph(parse_tasks(tasks), tasks_file="tasks.md")
    assert [str(v) for v in found] == []


@settings(max_examples=100, deadline=None)
@given(
    leaf_count=st.integers(min_value=2, max_value=6),
    pair=st.data(),
)
def test_a_cycle_in_the_declared_dependencies_is_always_found(leaf_count, pair):
    """Any closed loop is reported once, whatever else the graph declares."""
    numbers = _leaf_numbers(leaf_count)
    waves = _chunks(numbers, 2)
    assume(len(waves) > 1)
    # A loop of any length the graph can hold, not only a mutual pair: a
    # detector that scans for self-loops and reciprocal edges satisfies a
    # generator that only ever builds two-node loops, and "any closed loop" is
    # the claim being made.
    loop = pair.draw(
        st.lists(
            st.sampled_from(numbers),
            min_size=2,
            max_size=len(numbers),
            unique=True,
        )
    )
    dependencies = {node: [loop[(index + 1) % len(loop)]] for index, node in enumerate(loop)}

    tasks = _tasks_document(
        [(False, ["1.1"])] * leaf_count,
        waves=waves,
        dependencies=dependencies,
    )
    found = check_dependency_graph(parse_tasks(tasks), tasks_file="tasks.md")
    cycles = [v for v in found if v.rule == rules.GRAPH_CYCLE]
    assert len(cycles) == 1
    for node in loop:
        assert node in cycles[0].message


# --- Totality --------------------------------------------------------------

_FRAGMENTS = st.sampled_from(
    [
        "# Implementation Plan",
        "## Tasks",
        "- [ ] 1. Parent",
        "  - [ ] 1.1 Leaf",
        "    - _Requirements: 1.1_",
        "    - _Requirements: 99.99_",
        "    - _Requirements: _",
        "## Task Dependency Graph",
        "```json",
        '{"waves": [{"id": 0, "tasks": ["1.1"]}]}',
        '{"waves": [{"id": 0, "tasks": ["1.1"]}',
        '{"waves": []}',
        '{"waves": [[]]}',
        "[]",
        "```",
        "~~~",
        "",
        "   ",
    ]
)


@settings(max_examples=250, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(fragments=st.lists(_FRAGMENTS, max_size=20), requirements=st.text(max_size=200))
def test_the_checks_are_total_and_self_describing(fragments, requirements):
    """Any input yields a report whose findings are registered and locatable."""
    tasks = "\n".join(fragments)
    found = check_cross_document(
        parse_requirements(requirements),
        parse_tasks(tasks),
        requirements_file="requirements.md",
        tasks_file="tasks.md",
    )
    limits = {
        "requirements.md": max(len(requirements.splitlines()), 1),
        "tasks.md": max(len(tasks.splitlines()), 1),
    }
    for violation in found:
        assert violation.rule in rules.ALL_RULES, violation.rule
        assert rules.describe(violation.rule)
        assert violation.message.strip()
        assert 1 <= violation.location.line <= limits[violation.file]


@settings(max_examples=50, deadline=None)
@given(tasks=st.text(max_size=300), requirements=st.text(max_size=300))
def test_arbitrary_documents_never_raise(tmp_path_factory, tasks, requirements):
    spec = tmp_path_factory.mktemp("spec")
    (spec / "tasks.md").write_text(tasks, encoding="utf-8")
    (spec / "requirements.md").write_text(requirements, encoding="utf-8")
    validate_spec(spec)
    validate_tasks(spec / "tasks.md", requirements_path=spec / "requirements.md")
