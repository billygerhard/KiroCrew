"""Checks that no single document can answer on its own.

Three claims live between the documents rather than inside one of them.

A task's criteria references must resolve. The format rules confirm a leaf task
carries a reference and that the reference is spelled correctly; only
requirements.md can say whether what it points at exists.

Requirements must be covered. A requirement no task claims is planned work that
was dropped, and it is invisible while each document is read alone. Coverage is
reported whether tasks.md is validated by itself or as part of a whole spec,
because the omission is the same omission either way.

The dependency graph must be schedulable. It is data the orchestrator executes,
so it is validated as data -- readable JSON with the expected shape -- and as a
plan: consecutive waves counting from zero, every unfinished leaf scheduled
exactly once, and no cycle among declared dependencies.

Severities separate work that is missing from work that is merely unaccounted
for. A requirement with no task at all blocks a gate; one covered requirement
with a single criterion nobody claimed is a warning, because that is a judgement
about granularity and a plan that groups two criteria under one task is not
broken. Findings about coverage are reported against requirements.md at the line
of the thing not covered, which is what the reader needs to look at even though
the repair is usually made in tasks.md.

The graph's canonical form is the wave list. A graph may additionally declare
``dependencies``, mapping a task to the tasks it needs, and then the edges must
agree with the layering: a wave list on its own cannot express a cycle, so a
graph that carries no edges is acyclic by construction and reports nothing.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from . import rules
from .documents import DocumentKind
from .findings import Location, Severity, ValidationReport, Violation, build_report
from .native_format import validate_document
from .structure import (
    GRAPH_BLOCK_INFO,
    TASK_ID_RE,
    CriterionRef,
    FencedBlock,
    RequirementsIndex,
    Task,
    TaskPlan,
    parse_requirements,
    parse_tasks,
)

#: Key holding the wave list.
WAVES_KEY = "waves"
#: Key holding a wave's identifier.
WAVE_ID_KEY = "id"
#: Key holding a wave's scheduled tasks.
WAVE_TASKS_KEY = "tasks"
#: Optional key mapping a task to the tasks it depends on.
DEPENDENCIES_KEY = "dependencies"

#: Wave identifiers count from here.
FIRST_WAVE_ID = 0


class _Collector:
    """Accumulates violations that may concern either document."""

    def __init__(self) -> None:
        self._violations: list[Violation] = []

    def add(
        self,
        rule: str,
        file: str,
        line: int,
        message: str,
        *,
        severity: Severity = Severity.ERROR,
    ) -> None:
        self._violations.append(
            Violation(
                file=file,
                location=Location(line=line),
                rule=rule,
                severity=severity,
                message=message,
            )
        )

    @property
    def violations(self) -> tuple[Violation, ...]:
        return tuple(self._violations)


# --- Task links ------------------------------------------------------------


def _iter_references(plan: TaskPlan) -> Iterable[tuple[Task, CriterionRef]]:
    for task in plan.tasks:
        for reference in task.references:
            yield task, reference


def check_task_links(
    index: RequirementsIndex,
    plan: TaskPlan,
    *,
    tasks_file: str,
) -> tuple[Violation, ...]:
    """Resolve every criteria reference in ``plan`` against ``index``.

    References are resolved wherever they appear. The format rules require one
    on every leaf and permit one on a parent, and a reference that points
    nowhere is wrong in either position.
    """
    collector = _Collector()
    for task, reference in _iter_references(plan):
        requirement = index.get(reference.requirement)
        if requirement is None:
            collector.add(
                rules.TASK_REFERENCE_REQUIREMENT_UNKNOWN,
                tasks_file,
                reference.line,
                f"Task {task.number} references {reference.text}, but requirements.md "
                f"declares no requirement {reference.requirement}.",
            )
            continue
        if reference.criterion is None:
            continue
        if index.criterion(reference.requirement, reference.criterion) is None:
            declared = ", ".join(str(c.number) for c in requirement.criteria) or "none"
            collector.add(
                rules.TASK_REFERENCE_CRITERION_UNKNOWN,
                tasks_file,
                reference.line,
                f"Task {task.number} references {reference.text}, but requirement "
                f"{reference.requirement} declares criteria: {declared}.",
            )
    return collector.violations


# --- Coverage --------------------------------------------------------------


def _covered(index: RequirementsIndex, plan: TaskPlan) -> tuple[set[int], set[tuple[int, int]]]:
    """Split resolvable references into whole requirements and single criteria.

    A reference naming a requirement without a criterion claims all of it, so it
    covers every criterion that requirement declares.
    """
    requirements: set[int] = set()
    criteria: set[tuple[int, int]] = set()
    for _, reference in _iter_references(plan):
        requirement = index.get(reference.requirement)
        if requirement is None:
            continue
        if reference.criterion is None:
            requirements.add(reference.requirement)
            criteria.update((requirement.number, c.number) for c in requirement.criteria)
        elif index.criterion(reference.requirement, reference.criterion) is not None:
            # A reference that resolves to nothing covers nothing, so naming an
            # undeclared criterion does not make its requirement accounted for.
            requirements.add(reference.requirement)
            criteria.add((reference.requirement, reference.criterion))
    return requirements, criteria


def check_requirement_coverage(
    index: RequirementsIndex,
    plan: TaskPlan,
    *,
    requirements_file: str,
) -> tuple[Violation, ...]:
    """Report the requirements and criteria no task claims.

    A requirement no task touches at all is reported once, at its heading; its
    criteria are not then reported individually, because the requirement is the
    finding and repeating it per criterion would bury it.
    """
    collector = _Collector()
    covered_requirements, covered_criteria = _covered(index, plan)
    for requirement in index:
        if requirement.number not in covered_requirements:
            collector.add(
                rules.COVERAGE_REQUIREMENT_UNCOVERED,
                requirements_file,
                requirement.line,
                f"No task claims requirement {requirement.number}; add a task "
                f"referencing its acceptance criteria.",
            )
            continue
        for criterion in requirement.criteria:
            if (requirement.number, criterion.number) not in covered_criteria:
                collector.add(
                    rules.COVERAGE_CRITERION_UNCOVERED,
                    requirements_file,
                    criterion.line,
                    f"No task references criterion {criterion.identifier}.",
                    severity=Severity.WARNING,
                )
    return collector.violations


# --- Dependency graph ------------------------------------------------------


class _BlockLines:
    """Maps a token in the graph block back to the line it sits on.

    The graph is one JSON value, so a finding about a wave or a task would
    otherwise have to address the whole block. Occurrences are addressable by
    index, so the second mention of a task is located at its second line rather
    than at the first one again. A token that cannot be found addresses the
    block, which is the honest answer rather than an invented line.
    """

    def __init__(self, block: FencedBlock) -> None:
        self._lines = block.body_lines
        self._base = block.body_line
        self._found: dict[str, tuple[int, ...]] = {}

    def occurrences(self, token: str) -> tuple[int, ...]:
        """Every line the token sits on, repeated per occurrence on that line."""
        needle = json.dumps(token)
        cached = self._found.get(needle)
        if cached is None:
            lines: list[int] = []
            for offset, text in enumerate(self._lines):
                lines.extend([self._base + offset] * text.count(needle))
            cached = tuple(lines)
            self._found[needle] = cached
        return cached

    def line(self, token: str, occurrence: int = 0) -> int:
        found = self.occurrences(token)
        if not found:
            return self._base
        try:
            return found[occurrence]
        except IndexError:
            return found[-1]


@dataclass(frozen=True)
class _Slot:
    """Where a scheduled task sits: its wave, and the line that schedules it."""

    wave: int
    line: int


def _is_integer(value: object) -> bool:
    """True for a JSON integer. ``bool`` is an ``int`` in Python but not an id."""
    return isinstance(value, int) and not isinstance(value, bool)


def _read_graph(
    block: FencedBlock, tasks_file: str, collector: _Collector
) -> Mapping[str, object] | None:
    try:
        graph = json.loads(block.body)
    except json.JSONDecodeError as error:
        collector.add(
            rules.GRAPH_JSON_MALFORMED,
            tasks_file,
            # A decode error's line is 1-based within the block body.
            block.body_line + max(error.lineno - 1, 0),
            f"The dependency graph is not readable JSON: {error.msg}.",
        )
        return None
    if not isinstance(graph, dict) or not isinstance(graph.get(WAVES_KEY), list):
        collector.add(
            rules.GRAPH_ROOT_INVALID,
            tasks_file,
            block.body_line,
            f"Write the graph as an object with a {WAVES_KEY!r} list of waves.",
        )
        return None
    return graph


def _read_waves(
    waves: Sequence[object],
    block: _BlockLines,
    tasks_file: str,
    collector: _Collector,
) -> dict[str, _Slot]:
    """Check every wave entry and return the wave each task is scheduled in."""
    assigned: dict[str, _Slot] = {}
    mentions: Counter[str] = Counter()
    for position, wave in enumerate(waves):
        line = block.line(WAVE_ID_KEY, position)
        if not isinstance(wave, dict):
            collector.add(
                rules.GRAPH_WAVE_INVALID,
                tasks_file,
                line,
                f"Wave {position} is not an object carrying {WAVE_ID_KEY!r} "
                f"and {WAVE_TASKS_KEY!r}.",
            )
            continue
        identifier = wave.get(WAVE_ID_KEY)
        tasks = wave.get(WAVE_TASKS_KEY)
        if not _is_integer(identifier) or not isinstance(tasks, list) or not tasks:
            collector.add(
                rules.GRAPH_WAVE_INVALID,
                tasks_file,
                line,
                f"A wave carries an integer {WAVE_ID_KEY!r} and a non-empty "
                f"{WAVE_TASKS_KEY!r} list.",
            )
            continue
        expected = FIRST_WAVE_ID + position
        if identifier != expected:
            collector.add(
                rules.GRAPH_WAVE_ID_NOT_SEQUENTIAL,
                tasks_file,
                line,
                f"Expected wave {expected}, found {identifier}; wave identifiers "
                f"count from {FIRST_WAVE_ID} without gaps or repeats.",
            )
        for entry in tasks:
            if not isinstance(entry, str) or not TASK_ID_RE.match(entry):
                collector.add(
                    rules.GRAPH_TASK_ID_MALFORMED,
                    tasks_file,
                    line,
                    f"Wave {identifier} schedules {entry!r}, which is not a task number.",
                )
                continue
            entry_line = block.line(entry, mentions[entry])
            mentions[entry] += 1
            if entry in assigned:
                collector.add(
                    rules.GRAPH_TASK_DUPLICATE,
                    tasks_file,
                    entry_line,
                    f"Task {entry} is already scheduled in wave {assigned[entry].wave}.",
                )
                continue
            assigned[entry] = _Slot(wave=expected, line=entry_line)
    return assigned


def _check_membership(
    plan: TaskPlan,
    assigned: Mapping[str, _Slot],
    tasks_file: str,
    collector: _Collector,
) -> None:
    parents = plan.parent_numbers
    for number, slot in assigned.items():
        task = plan.get(number)
        if task is None:
            collector.add(
                rules.GRAPH_TASK_UNKNOWN,
                tasks_file,
                slot.line,
                f"The graph schedules task {number}, which the tasks section " f"does not declare.",
            )
        elif number in parents:
            collector.add(
                rules.GRAPH_TASK_NOT_LEAF,
                tasks_file,
                slot.line,
                f"Task {number} groups other tasks; schedule its subtasks instead.",
            )
    for task in plan.leaves:
        # A finished task needs no wave: the graph schedules remaining work.
        if not task.complete and task.number not in assigned:
            collector.add(
                rules.GRAPH_TASK_UNASSIGNED,
                tasks_file,
                task.line,
                f"Task {task.number} is unfinished and is scheduled in no wave.",
            )


def _read_dependencies(
    declared: object,
    assigned: Mapping[str, _Slot],
    block: _BlockLines,
    tasks_file: str,
    collector: _Collector,
) -> dict[str, tuple[str, ...]]:
    """Resolve the declared edges, checking that each runs before its dependent.

    A task's own line here is its last mention in the block, which for a graph
    that declares dependencies is the entry inside them rather than the wave
    that schedules it.
    """
    if not isinstance(declared, dict):
        collector.add(
            rules.GRAPH_DEPENDENCIES_INVALID,
            tasks_file,
            block.line(DEPENDENCIES_KEY),
            f"Write {DEPENDENCIES_KEY!r} as an object mapping a task number to "
            f"a list of task numbers.",
        )
        return {}

    last = -1
    edges: dict[str, tuple[str, ...]] = {}
    for task, needs in declared.items():
        line = block.line(task, last) if isinstance(task, str) else block.line(DEPENDENCIES_KEY)
        if not isinstance(task, str) or not isinstance(needs, list):
            collector.add(
                rules.GRAPH_DEPENDENCIES_INVALID,
                tasks_file,
                line,
                f"The dependencies of {task!r} must be a list of task numbers.",
            )
            continue
        if task not in assigned:
            collector.add(
                rules.GRAPH_DEPENDENCY_UNKNOWN,
                tasks_file,
                line,
                f"Task {task} declares dependencies but is scheduled in no wave.",
            )
            continue
        resolved: list[str] = []
        for need in needs:
            if not isinstance(need, str) or need not in assigned:
                collector.add(
                    rules.GRAPH_DEPENDENCY_UNKNOWN,
                    tasks_file,
                    line,
                    f"Task {task} depends on {need!r}, which no wave schedules.",
                )
                continue
            resolved.append(need)
            if assigned[need].wave >= assigned[task].wave:
                collector.add(
                    rules.GRAPH_DEPENDENCY_ORDER,
                    tasks_file,
                    line,
                    f"Task {task} runs in wave {assigned[task].wave} but depends on "
                    f"{need} in wave {assigned[need].wave}; a dependency runs earlier.",
                )
        edges[task] = tuple(resolved)
    return edges


def _find_cycle(edges: Mapping[str, tuple[str, ...]]) -> tuple[str, ...] | None:
    """Return one cycle in ``edges`` as a path, or None when acyclic.

    A depth-first walk that remembers the nodes on the current path: reaching a
    node already on that path closes a cycle, and the slice from that node is
    the cycle itself, which is what makes the finding actionable rather than a
    bare assertion that one exists.
    """
    finished: set[str] = set()
    for start in edges:
        if start in finished:
            continue
        path: list[str] = []
        on_path: set[str] = set()
        # Each frame is a node plus the dependencies of it left to walk.
        stack: list[tuple[str, list[str]]] = [(start, list(edges.get(start, ())))]
        path.append(start)
        on_path.add(start)
        while stack:
            node, pending = stack[-1]
            if not pending:
                stack.pop()
                finished.add(node)
                on_path.discard(node)
                if path:
                    path.pop()
                continue
            following = pending.pop()
            if following in on_path:
                return tuple(path[path.index(following) :]) + (following,)
            if following in finished:
                continue
            path.append(following)
            on_path.add(following)
            stack.append((following, list(edges.get(following, ()))))
    return None


def check_dependency_graph(plan: TaskPlan, *, tasks_file: str) -> tuple[Violation, ...]:
    """Validate the graph as JSON and as a schedule.

    A plan that declares no dependency-graph section has nothing to validate:
    the section is an addition to the native tasks document, and a document
    without one is an ordinary tasks document.
    """
    collector = _Collector()
    if plan.graph_heading_line is None:
        return ()
    if plan.graph_block is None:
        collector.add(
            rules.GRAPH_BLOCK_MISSING,
            tasks_file,
            plan.graph_heading_line,
            f"The dependency-graph section carries no fenced {GRAPH_BLOCK_INFO} "
            f"block declaring its waves.",
        )
        return collector.violations

    block = plan.graph_block
    graph = _read_graph(block, tasks_file, collector)
    if graph is None:
        return collector.violations

    waves = graph[WAVES_KEY]
    assert isinstance(waves, list)  # checked while reading the graph
    if not waves:
        collector.add(
            rules.GRAPH_WAVES_EMPTY,
            tasks_file,
            block.body_line,
            "The dependency graph declares no waves.",
        )
        return collector.violations

    lines = _BlockLines(block)
    assigned = _read_waves(waves, lines, tasks_file, collector)
    _check_membership(plan, assigned, tasks_file, collector)

    if DEPENDENCIES_KEY in graph:
        edges = _read_dependencies(graph[DEPENDENCIES_KEY], assigned, lines, tasks_file, collector)
        cycle = _find_cycle(edges)
        if cycle is not None:
            collector.add(
                rules.GRAPH_CYCLE,
                tasks_file,
                lines.line(cycle[0]),
                "The dependencies close a cycle, so no wave order satisfies "
                f"them: {' -> '.join(cycle)}.",
            )
    return collector.violations


# --- Entry points ----------------------------------------------------------


def check_cross_document(
    index: RequirementsIndex,
    plan: TaskPlan,
    *,
    requirements_file: str,
    tasks_file: str,
) -> tuple[Violation, ...]:
    """Run every check that spans the two documents."""
    return (
        check_task_links(index, plan, tasks_file=tasks_file)
        + check_requirement_coverage(index, plan, requirements_file=requirements_file)
        + check_dependency_graph(plan, tasks_file=tasks_file)
    )


def validate_tasks(
    tasks_path: Path | str,
    *,
    requirements_path: Path | str,
    tasks_file: str | None = None,
    requirements_file: str | None = None,
) -> ValidationReport:
    """Validate tasks.md alone, resolved against a requirements document.

    Requirements are read but not validated: this is the tasks-only mode, and
    reporting defects in a document the caller did not submit would be noise.
    Coverage is still reported, because a requirement with no task is a defect
    of the plan rather than of the requirements.
    """
    tasks_path = Path(tasks_path)
    requirements_path = Path(requirements_path)
    tasks_name = tasks_file if tasks_file is not None else str(tasks_path)
    requirements_name = (
        requirements_file if requirements_file is not None else str(requirements_path)
    )

    report = validate_document(tasks_path, kind=DocumentKind.TASKS, file=tasks_name)
    plan = parse_tasks(tasks_path.read_text(encoding="utf-8"))
    index = parse_requirements(requirements_path.read_text(encoding="utf-8"))
    return build_report(
        list(report)
        + list(
            check_cross_document(
                index,
                plan,
                requirements_file=requirements_name,
                tasks_file=tasks_name,
            )
        )
    )


def validate_spec(spec_dir: Path | str, *, relative: bool = False) -> ValidationReport:
    """Validate every native document a spec directory holds, plus their links.

    Documents that are absent are skipped rather than reported: which documents
    a spec owes depends on its recorded spec type, which is not a property of
    the documents on disk. Cross-document checks need both requirements.md and
    tasks.md, so they run only when both are present.

    ``relative`` reports each violation against the bare filename instead of the
    full path, for a caller rendering a report next to the spec itself.
    """
    spec_dir = Path(spec_dir)
    present: dict[DocumentKind, Path] = {}
    violations: list[Violation] = []

    def name(kind: DocumentKind) -> str:
        return kind.filename if relative else str(spec_dir / kind.filename)

    for kind in DocumentKind:
        path = spec_dir / kind.filename
        if not path.is_file():
            continue
        present[kind] = path
        violations.extend(validate_document(path, kind=kind, file=name(kind)))

    requirements_path = present.get(DocumentKind.REQUIREMENTS)
    tasks_path = present.get(DocumentKind.TASKS)
    if requirements_path is not None and tasks_path is not None:
        violations.extend(
            check_cross_document(
                parse_requirements(requirements_path.read_text(encoding="utf-8")),
                parse_tasks(tasks_path.read_text(encoding="utf-8")),
                requirements_file=name(DocumentKind.REQUIREMENTS),
                tasks_file=name(DocumentKind.TASKS),
            )
        )
    return build_report(violations)
