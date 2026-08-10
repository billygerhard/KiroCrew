"""The native documents read as structures rather than as text.

Native-format validation checks one line at a time and answers with findings.
The checks that span documents need something else: which requirements exist and
what criteria each declares, which tasks are leaves and what each leaf claims to
satisfy, and which waves the dependency graph declares. That is a model, not a
finding stream -- several checks query the same model, and a driver can render it
-- so parsing happens once here and produces models only.

Parsing is deliberately forgiving. Anything it cannot read is left out of the
model instead of reported, because :mod:`.native_format` already reports
malformed shapes; reporting them here as well would bill an author twice for one
defect. It follows that a model is not evidence that a document is clean, only a
description of the parts of it that were legible.

The lexical shapes come from :mod:`.native_format` rather than being restated,
so a line the validator accepts as a task is the same line this module counts as
a task.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterator, Sequence

from .documents import ACCEPTANCE_CRITERIA_HEADING, normalize_heading
from .native_format import (
    FENCE_RE,
    HEADING_LEVEL_CRITERIA,
    HEADING_LEVEL_REQUIREMENT,
    HEADING_LEVEL_SECTION,
    HEADING_RE,
    NUMBERED_ITEM_RE,
    REFERENCE_ID_RE,
    REQUIREMENT_HEADING_RE,
    TASK_ITEM_LOOSE_RE,
    TASK_ITEM_RE,
    TASK_NUMBER_RE,
    TASK_REFERENCE_RE,
)

#: A task number as the dependency graph spells it: the plain number, with no
#: trailing period and no surrounding prose.
TASK_ID_RE = re.compile(r"^\d+(?:\.\d+)*$")

#: The section that carries the dependency graph, matched as a substring of the
#: normalized heading so that a document may name it "Task Dependency Graph" or
#: "Dependency Graph" without the checks caring which.
GRAPH_SECTION_MARKER = "dependency graph"

#: The fence info string the dependency graph is written under.
GRAPH_BLOCK_INFO = "json"

#: Checkbox marks that mean the task is finished. A dash means started but not
#: finished, so it counts as incomplete: the graph must still schedule it.
_COMPLETE_MARKS = frozenset({"x", "X"})


@dataclass(frozen=True)
class Line:
    """A line of document content, outside any fenced block."""

    number: int
    text: str


@dataclass(frozen=True)
class FencedBlock:
    """A fenced block's info string and body, with the body's first line."""

    info: str
    body: str
    #: Line the opening fence sits on.
    fence_line: int
    #: Line the body starts on. A block with no body addresses its own fence
    #: rather than the line after it, which for a block at the end of a document
    #: would be past the end.
    body_line: int

    @property
    def body_lines(self) -> tuple[str, ...]:
        return tuple(self.body.splitlines())


@dataclass(frozen=True)
class Criterion:
    """One acceptance criterion, addressed the way a task references it."""

    requirement: int
    number: int
    line: int

    @property
    def identifier(self) -> str:
        return f"{self.requirement}.{self.number}"


@dataclass(frozen=True)
class Requirement:
    """One numbered requirement and the criteria it declares."""

    number: int
    title: str
    line: int
    criteria: tuple[Criterion, ...] = ()


@dataclass(frozen=True)
class RequirementsIndex:
    """Every requirement a requirements document declares, in document order."""

    requirements: tuple[Requirement, ...] = ()

    def get(self, number: int) -> Requirement | None:
        for requirement in self.requirements:
            if requirement.number == number:
                return requirement
        return None

    def criterion(self, requirement: int, number: int) -> Criterion | None:
        found = self.get(requirement)
        if found is None:
            return None
        for criterion in found.criteria:
            if criterion.number == number:
                return criterion
        return None

    def __iter__(self) -> Iterator[Requirement]:
        return iter(self.requirements)

    def __len__(self) -> int:
        return len(self.requirements)


@dataclass(frozen=True)
class CriterionRef:
    """A leaf task's claim to satisfy an acceptance criterion.

    ``criterion`` is ``None`` for a reference that names a requirement without
    naming a criterion inside it, which reads as a claim on the whole
    requirement rather than as an incomplete reference.
    """

    text: str
    requirement: int
    criterion: int | None
    line: int


@dataclass(frozen=True)
class Task:
    """One checklist item."""

    number: str
    title: str
    line: int
    complete: bool
    references: tuple[CriterionRef, ...] = ()

    @property
    def depth(self) -> int:
        return self.number.count(".") + 1

    @property
    def parent(self) -> str | None:
        return self.number.rsplit(".", 1)[0] if self.depth > 1 else None


@dataclass(frozen=True)
class TaskPlan:
    """A tasks document: its checklist and its dependency graph block."""

    tasks: tuple[Task, ...] = ()
    #: Line of the dependency-graph section heading, when the document has one.
    graph_heading_line: int | None = None
    #: The graph's fenced block, when the section carries one.
    graph_block: FencedBlock | None = None

    @property
    def parent_numbers(self) -> frozenset[str]:
        """Numbers that some other task names as its parent.

        Parentage is read from the numbers rather than from document order,
        because a plan legitimately declares a subtask after a later parent.
        """
        return frozenset(task.parent for task in self.tasks if task.parent is not None)

    @property
    def leaves(self) -> tuple[Task, ...]:
        """Tasks that group nothing else, which are the units of work."""
        parents = self.parent_numbers
        return tuple(task for task in self.tasks if task.number not in parents)

    def get(self, number: str) -> Task | None:
        for task in self.tasks:
            if task.number == number:
                return task
        return None


def scan(text: str) -> tuple[list[Line], list[FencedBlock]]:
    """Split ``text`` into its content lines and its fenced blocks.

    Content lines exclude fenced bodies, matching how the format rules read a
    document. The bodies are kept separately rather than dropped, because the
    dependency graph lives inside one.
    """
    lines: list[Line] = []
    blocks: list[FencedBlock] = []
    fence: str | None = None
    info = ""
    fence_line = 0
    body: list[str] = []

    def close() -> None:
        blocks.append(
            FencedBlock(
                info=info,
                body="\n".join(body),
                fence_line=fence_line,
                body_line=fence_line + 1 if body else fence_line,
            )
        )

    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.rstrip()
        match = FENCE_RE.match(line)
        if fence is not None:
            if match is not None:
                marker = match.group("marker")
                # Only a run of the same character, at least as long as the
                # opener, closes the fence.
                if marker[0] == fence[0] and len(marker) >= len(fence):
                    close()
                    fence = None
                    body = []
                    continue
            body.append(line)
            continue
        if match is not None:
            fence = match.group("marker")
            fence_line = number
            info = line[match.end("marker") :].strip().casefold()
            body = []
            continue
        lines.append(Line(number=number, text=line))

    if fence is not None:
        # An unclosed fence still holds a body; treat the rest of the document
        # as that body rather than losing it.
        close()
    return lines, blocks


@dataclass(frozen=True)
class _Heading:
    level: int
    text: str
    line: int
    index: int

    @property
    def normalized(self) -> str:
        return normalize_heading(self.text)


def _headings(lines: Sequence[Line]) -> list[_Heading]:
    headings: list[_Heading] = []
    for index, line in enumerate(lines):
        match = HEADING_RE.match(line.text)
        if match is not None:
            headings.append(
                _Heading(
                    level=len(match.group("hashes")),
                    text=match.group("text"),
                    line=line.number,
                    index=index,
                )
            )
    return headings


def parse_requirements(text: str) -> RequirementsIndex:
    """Index the requirements and acceptance criteria ``text`` declares.

    A criterion is a numbered item under a requirement's acceptance-criteria
    heading. Numbered items elsewhere in the document -- an ordered list in the
    introduction, for instance -- are not criteria and are not indexed.
    """
    lines, _ = scan(text)
    headings = _headings(lines)
    heading_lines = {heading.index for heading in headings}

    requirements: list[Requirement] = []
    number: int | None = None
    title = ""
    heading_line = 0
    criteria: list[Criterion] = []
    in_criteria = False

    def flush() -> None:
        if number is not None:
            requirements.append(
                Requirement(
                    number=number,
                    title=title,
                    line=heading_line,
                    criteria=tuple(criteria),
                )
            )

    for index, line in enumerate(lines):
        if index in heading_lines:
            heading = next(h for h in headings if h.index == index)
            if heading.level <= HEADING_LEVEL_REQUIREMENT:
                match = REQUIREMENT_HEADING_RE.match(heading.text)
                flush()
                criteria = []
                in_criteria = False
                if match is not None and heading.level == HEADING_LEVEL_REQUIREMENT:
                    number = int(match.group("number"))
                    title = match.group("title").strip()
                    heading_line = heading.line
                else:
                    number = None
                continue
            if heading.level == HEADING_LEVEL_CRITERIA:
                in_criteria = heading.normalized == normalize_heading(ACCEPTANCE_CRITERIA_HEADING)
                continue
            if heading.level > HEADING_LEVEL_CRITERIA:
                continue
        if not in_criteria or number is None:
            continue
        item = NUMBERED_ITEM_RE.match(line.text)
        if item is not None:
            criteria.append(
                Criterion(
                    requirement=number,
                    number=int(item.group("number")),
                    line=line.number,
                )
            )

    flush()
    return RequirementsIndex(tuple(requirements))


def _parse_reference_line(line: Line) -> list[CriterionRef]:
    match = TASK_REFERENCE_RE.match(line.text)
    if match is None:
        return []
    references: list[CriterionRef] = []
    for token in match.group("ids").split(","):
        text = token.strip()
        if not REFERENCE_ID_RE.match(text):
            # Reported as a malformed reference by the format rules.
            continue
        requirement, _, criterion = text.partition(".")
        references.append(
            CriterionRef(
                text=text,
                requirement=int(requirement),
                criterion=int(criterion) if criterion else None,
                line=line.number,
            )
        )
    return references


def _task_item_indices(lines: Sequence[Line]) -> list[int]:
    return [
        index
        for index, line in enumerate(lines)
        if TASK_ITEM_RE.match(line.text) or TASK_ITEM_LOOSE_RE.match(line.text)
    ]


def _graph_block(
    blocks: Sequence[FencedBlock], headings: Sequence[_Heading]
) -> tuple[int | None, FencedBlock | None]:
    """Find the dependency-graph section heading and the block it carries."""
    sections = [
        heading
        for heading in headings
        if heading.level <= HEADING_LEVEL_SECTION and GRAPH_SECTION_MARKER in heading.normalized
    ]
    if not sections:
        return None, None
    section = sections[0]
    following = [
        heading
        for heading in headings
        if heading.index > section.index and heading.level <= section.level
    ]
    end = following[0].line if following else None
    for block in blocks:
        if block.fence_line < section.line:
            continue
        if end is not None and block.fence_line > end:
            continue
        if block.info == GRAPH_BLOCK_INFO:
            return section.line, block
    return section.line, None


def parse_tasks(text: str) -> TaskPlan:
    """Read the checklist and the dependency graph ``text`` declares."""
    lines, blocks = scan(text)
    headings = _headings(lines)
    indices = _task_item_indices(lines)

    tasks: list[Task] = []
    for position, index in enumerate(indices):
        line = lines[index]
        match = TASK_ITEM_RE.match(line.text) or TASK_ITEM_LOOSE_RE.match(line.text)
        assert match is not None  # only matching lines are indexed
        number_match = TASK_NUMBER_RE.match(match.group("rest"))
        if number_match is None:
            continue
        # Detail lines belong to the item until the next checklist item starts.
        end = indices[position + 1] if position + 1 < len(indices) else len(lines)
        references: list[CriterionRef] = []
        for detail in lines[index + 1 : end]:
            references.extend(_parse_reference_line(detail))
        tasks.append(
            Task(
                number=number_match.group("number"),
                title=(number_match.group("title") or "").strip(),
                line=line.number,
                complete=match.group("mark").strip() in _COMPLETE_MARKS,
                references=tuple(references),
            )
        )

    heading_line, block = _graph_block(blocks, headings)
    return TaskPlan(tasks=tuple(tasks), graph_heading_line=heading_line, graph_block=block)
