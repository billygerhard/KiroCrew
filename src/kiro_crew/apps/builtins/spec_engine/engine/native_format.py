"""Native-format validation for the three spec documents.

This is the engine's own rule set: it is never delegated to a provider, and a
supplementary validation provider may only add findings alongside it. Validation
therefore has two properties that shape the code below.

It is **complete**. Every rule broken anywhere in the document is reported, not
just the first. A caller repairing a document -- human or agent -- needs the
whole list, because surfacing one defect per attempt costs a turn per defect.

It is **addressable**. Each violation names a file, a 1-based location, and a
stable rule identifier from :mod:`.rules`, so a driver can route it and a
diagnostic can quote it instead of matching on message text.

The rules themselves are derived from the published document format and from
real, format-clean spec artifacts. Where the artifacts and a tighter reading of
the format disagree, the artifacts win and the reason is stated at the rule:
tolerating a shape that occurs in practice matters more than a rule that reads
neatly and fires on good documents.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Sequence

from . import rules
from .documents import (
    ACCEPTANCE_CRITERIA_HEADING,
    REQUIRED_SECTIONS,
    REQUIREMENTS_SECTION,
    TASKS_SECTION,
    TITLE_PREFIXES,
    DocumentKind,
    kind_for_filename,
    normalize_heading,
)
from .findings import Location, Severity, ValidationReport, Violation, build_report

# --- Lexical shapes --------------------------------------------------------
#
# These are the format itself rather than this module's private business, so
# they are public: :mod:`.structure` parses the same documents for the
# cross-document checks, and a second definition of what a task item or a
# criteria reference looks like would let a shape validate here and stay
# invisible there.

#: A fenced block opener or closer. Up to three leading spaces still fences, per
#: the markdown the documents are written in.
FENCE_RE = re.compile(r"^ {0,3}(?P<marker>`{3,}|~{3,})")

#: A well-formed ATX heading: one to six hashes, exactly one space, then text.
HEADING_RE = re.compile(r"^(?P<hashes>#{1,6}) (?P<text>\S.*?)\s*$")

#: A heading that begins with a level-1..2 requirement heading number.
REQUIREMENT_HEADING_RE = re.compile(r"^Requirement +(?P<number>\d+) *:(?P<title>.*)$")

#: A numbered list item at the left margin, which is how criteria are written.
NUMBERED_ITEM_RE = re.compile(r"^(?P<number>\d+)\. +(?P<body>\S.*?)\s*$")

#: The user story line inside a requirement.
_USER_STORY_RE = re.compile(r"^\*\*User Story:\*\* *(?P<body>.*?)\s*$")

#: Role, capability, benefit. ``an`` is accepted alongside ``a`` because roles
#: beginning with a vowel are ordinary ("As an operator").
_USER_STORY_BODY_RE = re.compile(
    r"^As an? +\S.*?, *I want +\S.*?, *so that +\S.*$",
    re.IGNORECASE,
)

#: EARS openings. ``FOR ALL`` precedes the single words so the longer opening
#: wins the alternation. ``THE`` covers the unconditional (ubiquitous) form.
_EARS_OPENERS: tuple[str, ...] = ("FOR ALL", "WHEN", "IF", "WHILE", "WHERE", "THE")
_EARS_OPENER_RE = re.compile(r"^(?P<opener>FOR ALL|WHEN|IF|WHILE|WHERE|THE)\s")

_SHALL_RE = re.compile(r"\bSHALL\b")
_THEN_RE = re.compile(r"\bTHEN\b")

#: ``FOR ALL`` states a universal invariant about the system rather than an
#: obligation on it, so it is the one opening that does not need SHALL. Every
#: other opening promises behaviour, and behaviour with no SHALL is not testable.
_INVARIANT_OPENER = "FOR ALL"

#: A native task item: zero-or-two-space indent, a dash, a single-character
#: status box, one space, then the item.
TASK_ITEM_RE = re.compile(r"^(?P<indent> *)- \[(?P<mark>[ xX\-])\] (?P<rest>.*?)\s*$")

#: Anything close enough to a checkbox to have been meant as one. Matching this
#: but not :data:`TASK_ITEM_RE` is what makes a malformed checkbox reportable
#: instead of silently invisible.
TASK_ITEM_LOOSE_RE = re.compile(
    r"^(?P<indent>\s*)[-*+][ \t]*\[(?P<mark>[^\]\n]*)\][ \t]*(?P<rest>.*?)\s*$"
)

#: A task's leading number, with the trailing period optional. Real plans write
#: the period on parent numbers and omit it on subtask numbers; both are the
#: same number, so neither spelling is an error.
TASK_NUMBER_RE = re.compile(r"^(?P<number>\d+(?:\.\d+)*)\.?(?: +(?P<title>.*))?$")

#: The acceptance-criteria reference that closes a leaf task.
TASK_REFERENCE_RE = re.compile(r"^ *- *_Requirements: *(?P<ids>[^_]*)_$")

#: How a reference to a single acceptance criterion is spelled.
REFERENCE_ID_RE = re.compile(r"^\d+(?:\.\d+)?$")

#: Case-folded marker used to notice a reference line that is present but not
#: spelled correctly, so a typo reports as malformed rather than as missing.
REFERENCE_MARKER = "_requirements:"

#: Indent widths that denote a native nesting level: a parent task and its leaf.
_TASK_INDENTS: tuple[int, ...] = (0, 2)
_INDENT_PER_LEVEL = 2

#: Deepest task number the format nests to: a parent and its leaf.
_MAX_TASK_DEPTH = 2

HEADING_LEVEL_SECTION = 2
HEADING_LEVEL_REQUIREMENT = 3
HEADING_LEVEL_CRITERIA = 4

#: Location used for a violation about the document as a whole, such as a
#: section that is absent and therefore has no line of its own.
_FILE_LEVEL_LINE = 1


@dataclass(frozen=True)
class _Line:
    """A line of document content, outside any fenced block."""

    number: int
    text: str


@dataclass(frozen=True)
class _Heading:
    """A heading, with its position in the content-line sequence."""

    level: int
    text: str
    line: int
    index: int

    @property
    def normalized(self) -> str:
        return normalize_heading(self.text)


@dataclass(frozen=True)
class _TaskItem:
    """A parsed task checklist item."""

    number: str
    line: int
    depth: int
    #: Index into the content-line sequence, used to find the item's own detail
    #: lines without rescanning the document.
    index: int
    has_reference: bool


class _Collector:
    """Accumulates violations for one file."""

    def __init__(self, file: str) -> None:
        self._file = file
        self._violations: list[Violation] = []

    def add(
        self,
        rule: str,
        line: int,
        message: str,
        *,
        column: int | None = None,
        severity: Severity = Severity.ERROR,
    ) -> None:
        self._violations.append(
            Violation(
                file=self._file,
                location=Location(line=line, column=column),
                rule=rule,
                severity=severity,
                message=message,
            )
        )

    @property
    def violations(self) -> tuple[Violation, ...]:
        return tuple(self._violations)


def _scan(text: str, collector: _Collector) -> tuple[list[_Line], list[_Heading]]:
    """Split ``text`` into content lines and headings.

    Fenced blocks are dropped entirely: the architecture diagram and the
    dependency-graph JSON both contain lines that would otherwise read as
    markup. Anything outside a fence that starts with a hash is treated as an
    attempted heading, so ``##Overview`` reports as malformed rather than
    vanishing into prose.
    """
    lines: list[_Line] = []
    headings: list[_Heading] = []
    fence: str | None = None

    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.rstrip()
        fence_match = FENCE_RE.match(line)
        if fence is not None:
            # Only a run of the same character, at least as long as the opener,
            # closes the fence; a shorter or different run is block content.
            if fence_match is not None:
                marker = fence_match.group("marker")
                if marker[0] == fence[0] and len(marker) >= len(fence):
                    fence = None
            continue
        if fence_match is not None:
            fence = fence_match.group("marker")
            continue

        if line.lstrip().startswith("#") and line.startswith("#"):
            heading_match = HEADING_RE.match(line)
            if heading_match is None:
                collector.add(
                    rules.HEADING_MALFORMED,
                    number,
                    "Write a heading as one to six hash marks, a single space, "
                    "then the heading text.",
                )
            else:
                headings.append(
                    _Heading(
                        level=len(heading_match.group("hashes")),
                        text=heading_match.group("text"),
                        line=number,
                        index=len(lines),
                    )
                )

        lines.append(_Line(number=number, text=line))

    return lines, headings


def _span_end(headings: Sequence[_Heading], heading: _Heading, limit: int) -> int:
    """Index one past the last content line belonging to ``heading``."""
    for candidate in headings:
        if candidate.index > heading.index and candidate.level <= heading.level:
            return min(candidate.index, limit)
    return limit


def _body(lines: Sequence[_Line], heading: _Heading, end: int) -> list[_Line]:
    return list(lines[heading.index + 1 : end])


def _headings_within(
    headings: Sequence[_Heading], start: int, end: int, level: int
) -> list[_Heading]:
    return [h for h in headings if start <= h.index < end and h.level == level]


def _check_title(kind: DocumentKind, headings: Sequence[_Heading], collector: _Collector) -> None:
    prefix = TITLE_PREFIXES[kind]
    if not headings:
        collector.add(
            rules.DOCUMENT_TITLE_MISSING,
            _FILE_LEVEL_LINE,
            f"Open the document with a level-1 heading beginning '{prefix}'.",
        )
        return

    first = headings[0]
    if first.level != 1:
        collector.add(
            rules.DOCUMENT_TITLE_MISSING,
            first.line,
            f"The first heading must be the level-1 document title beginning '{prefix}'.",
        )
    elif not first.text.strip().casefold().startswith(prefix.casefold()):
        collector.add(
            rules.DOCUMENT_TITLE_MISMATCH,
            first.line,
            f"The document title must begin '{prefix}'; found {first.text!r}.",
        )

    for extra in [h for h in headings if h.level == 1][1:]:
        collector.add(
            rules.DOCUMENT_TITLE_DUPLICATE,
            extra.line,
            "A document carries one level-1 heading; demote this one to a section.",
        )


def _check_sections(
    kind: DocumentKind,
    lines: Sequence[_Line],
    headings: Sequence[_Heading],
    collector: _Collector,
) -> dict[str, list[_Heading]]:
    """Report on the required level-2 sections and index every section found."""
    found: dict[str, list[_Heading]] = {}
    for heading in headings:
        if heading.level == HEADING_LEVEL_SECTION:
            found.setdefault(heading.normalized, []).append(heading)

    for required in REQUIRED_SECTIONS[kind]:
        key = normalize_heading(required)
        occurrences = found.get(key, [])
        if not occurrences:
            collector.add(
                rules.SECTION_MISSING,
                _FILE_LEVEL_LINE,
                f"Add the required '{required}' section as a level-2 heading.",
            )
            continue
        for duplicate in occurrences[1:]:
            collector.add(
                rules.SECTION_DUPLICATE,
                duplicate.line,
                f"The '{required}' section is already declared; merge the two.",
            )
        first = occurrences[0]
        end = _span_end(headings, first, len(lines))
        if not any(line.text.strip() for line in _body(lines, first, end)):
            collector.add(
                rules.SECTION_EMPTY,
                first.line,
                f"The '{required}' section carries no content.",
                severity=Severity.WARNING,
            )

    return found


def _section(found: dict[str, list[_Heading]], name: str) -> _Heading | None:
    occurrences = found.get(normalize_heading(name), [])
    return occurrences[0] if occurrences else None


# --- requirements.md -------------------------------------------------------


def _check_criterion(body: str, line: int, column: int, collector: _Collector) -> None:
    opener_match = _EARS_OPENER_RE.match(body)
    if opener_match is None:
        collector.add(
            rules.CRITERION_KEYWORD_MISSING,
            line,
            "Open the criterion with one of "
            f"{', '.join(_EARS_OPENERS)} so its trigger is explicit.",
            column=column,
        )
        return

    opener = opener_match.group("opener")
    if opener != _INVARIANT_OPENER and not _SHALL_RE.search(body):
        collector.add(
            rules.CRITERION_SHALL_MISSING,
            line,
            "State the obligation with SHALL so the criterion is testable.",
            column=column,
        )
    if opener == "IF" and not _THEN_RE.search(body):
        collector.add(
            rules.CRITERION_IF_WITHOUT_THEN,
            line,
            "A criterion opening with IF must state its consequence with THEN.",
            column=column,
        )


def _check_criteria(
    requirement: _Heading,
    number: str,
    lines: Sequence[_Line],
    headings: Sequence[_Heading],
    end: int,
    collector: _Collector,
) -> None:
    criteria_headings = [
        h
        for h in _headings_within(headings, requirement.index, end, HEADING_LEVEL_CRITERIA)
        if h.normalized == normalize_heading(ACCEPTANCE_CRITERIA_HEADING)
    ]
    if not criteria_headings:
        collector.add(
            rules.CRITERIA_SECTION_MISSING,
            requirement.line,
            f"Requirement {number} needs an "
            f"'{ACCEPTANCE_CRITERIA_HEADING}' heading listing its criteria.",
        )
        return

    heading = criteria_headings[0]
    expected = 1
    for line in _body(lines, heading, _span_end(headings, heading, end)):
        item = NUMBERED_ITEM_RE.match(line.text)
        if item is None:
            continue
        if int(item.group("number")) != expected:
            collector.add(
                rules.CRITERION_NUMBER_NOT_SEQUENTIAL,
                line.number,
                f"Requirement {number}: expected criterion {expected}, "
                f"found {item.group('number')}.",
            )
        expected += 1
        _check_criterion(
            item.group("body"),
            line.number,
            item.start("body") + 1,
            collector,
        )

    if expected == 1:
        collector.add(
            rules.CRITERIA_EMPTY,
            heading.line,
            f"Requirement {number} lists no acceptance criteria.",
        )


def _check_user_story(
    requirement: _Heading,
    number: str,
    body: Sequence[_Line],
    collector: _Collector,
) -> None:
    for line in body:
        match = _USER_STORY_RE.match(line.text)
        if match is None:
            continue
        if not _USER_STORY_BODY_RE.match(match.group("body")):
            collector.add(
                rules.USER_STORY_MALFORMED,
                line.number,
                "Write the user story as 'As a <role>, I want <capability>, " "so that <benefit>'.",
                column=match.start("body") + 1,
            )
        return

    collector.add(
        rules.USER_STORY_MISSING,
        requirement.line,
        f"Requirement {number} needs a '**User Story:**' line naming the role, "
        "the capability, and the benefit.",
    )


def _check_requirements_body(
    lines: Sequence[_Line],
    headings: Sequence[_Heading],
    found: dict[str, list[_Heading]],
    collector: _Collector,
) -> None:
    section = _section(found, REQUIREMENTS_SECTION)
    if section is None:
        # Already reported as a missing section; there is no body to check.
        return

    section_end = _span_end(headings, section, len(lines))
    candidates = _headings_within(headings, section.index, section_end, HEADING_LEVEL_REQUIREMENT)

    position = 0
    for heading in candidates:
        match = REQUIREMENT_HEADING_RE.match(heading.text)
        if match is None:
            collector.add(
                rules.REQUIREMENT_HEADING_MALFORMED,
                heading.line,
                "Write a requirement heading as 'Requirement <number>: <title>'.",
            )
            continue

        position += 1
        number = match.group("number")
        if int(number) != position:
            collector.add(
                rules.REQUIREMENT_NUMBER_NOT_SEQUENTIAL,
                heading.line,
                f"Expected requirement {position}, found {number}; "
                "requirement numbers run sequentially from 1.",
            )
        if not match.group("title").strip():
            collector.add(
                rules.REQUIREMENT_TITLE_MISSING,
                heading.line,
                f"Requirement {number} needs a title after the colon.",
            )

        end = _span_end(headings, heading, section_end)
        _check_user_story(heading, number, _body(lines, heading, end), collector)
        _check_criteria(heading, number, lines, headings, end, collector)

    if position == 0:
        collector.add(
            rules.REQUIREMENTS_NONE,
            section.line,
            "The requirements section must declare at least one "
            "'### Requirement <number>: <title>'.",
        )


# --- tasks.md --------------------------------------------------------------


def _parse_task_item(line: _Line, index: int, collector: _Collector) -> tuple[str, int] | None:
    """Validate one checklist line and return its number and depth.

    Returns ``None`` when the line carries no usable number, which is reported
    on its own and leaves nothing for the numbering checks to work with.
    """
    match = TASK_ITEM_RE.match(line.text)
    if match is None:
        match = TASK_ITEM_LOOSE_RE.match(line.text)
        if match is None:
            return None
        collector.add(
            rules.TASK_CHECKBOX_MALFORMED,
            line.number,
            "Write a task as '- [ ] ' with a single status character "
            "(space, x, or -) and one following space.",
        )

    indent = match.group("indent")
    indent_ok = indent.isascii() and set(indent) <= {" "} and len(indent) in _TASK_INDENTS
    if not indent_ok:
        collector.add(
            rules.TASK_INDENT_INVALID,
            line.number,
            "Indent a parent task by zero spaces and a subtask by " f"{_INDENT_PER_LEVEL} spaces.",
        )

    rest = match.group("rest")
    number_match = TASK_NUMBER_RE.match(rest)
    if number_match is None:
        collector.add(
            rules.TASK_NUMBER_MISSING,
            line.number,
            "Give the task a leading number, as in '1.' or '1.1'.",
            column=match.start("rest") + 1,
        )
        return None

    number = number_match.group("number")
    depth = number.count(".") + 1
    if depth > _MAX_TASK_DEPTH:
        collector.add(
            rules.TASK_NUMBER_DEPTH,
            line.number,
            f"Task numbers nest at most {_MAX_TASK_DEPTH} levels; " f"'{number}' nests {depth}.",
        )
    elif indent_ok:
        expected_depth = len(indent) // _INDENT_PER_LEVEL + 1
        if depth != expected_depth:
            collector.add(
                rules.TASK_NUMBER_DEPTH_MISMATCH,
                line.number,
                f"'{number}' is a level-{depth} number but sits at "
                f"level-{expected_depth} indentation.",
            )

    title = number_match.group("title")
    if not (title or "").strip():
        collector.add(
            rules.TASK_TITLE_MISSING,
            line.number,
            f"Task {number} needs a title after its number.",
        )

    return number, depth


def _has_reference(lines: Sequence[_Line], start: int, end: int, collector: _Collector) -> bool:
    """Check the detail lines owned by a task for its criteria reference.

    A line that mentions the reference marker but is not spelled correctly
    reports as malformed and still counts as present, so a typo yields one
    finding rather than a malformed-and-missing pair.
    """
    present = False
    for line in lines[start:end]:
        if REFERENCE_MARKER not in line.text.casefold():
            continue
        present = True
        match = TASK_REFERENCE_RE.match(line.text)
        if match is None:
            collector.add(
                rules.TASK_REFERENCE_MALFORMED,
                line.number,
                "Write the reference as '- _Requirements: 1.1, 1.2_'.",
            )
            continue
        ids = [token.strip() for token in match.group("ids").split(",")]
        bad = [token for token in ids if not REFERENCE_ID_RE.match(token)]
        if bad or not ids:
            collector.add(
                rules.TASK_REFERENCE_MALFORMED,
                line.number,
                "Reference acceptance criteria as comma-separated "
                "'<requirement>.<criterion>' identifiers; "
                f"cannot read {', '.join(repr(token) for token in bad) or 'an empty list'}.",
            )
    return present


def _task_item_indices(lines: Sequence[_Line], start: int, end: int) -> list[int]:
    return [
        index
        for index in range(start, end)
        if TASK_ITEM_RE.match(lines[index].text) or TASK_ITEM_LOOSE_RE.match(lines[index].text)
    ]


def _check_tasks_body(
    lines: Sequence[_Line],
    headings: Sequence[_Heading],
    found: dict[str, list[_Heading]],
    collector: _Collector,
) -> None:
    section = _section(found, TASKS_SECTION)
    if section is None:
        return

    end = _span_end(headings, section, len(lines))
    item_indices = _task_item_indices(lines, section.index + 1, end)
    if not item_indices:
        collector.add(
            rules.TASKS_NONE,
            section.line,
            "The tasks section must declare at least one '- [ ] <number>. <title>'.",
        )
        return

    items: list[_TaskItem] = []
    first_seen: dict[str, int] = {}
    for position, index in enumerate(item_indices):
        line = lines[index]
        parsed = _parse_task_item(line, index, collector)
        # Detail lines belong to the item until the next checklist item begins.
        detail_end = item_indices[position + 1] if position + 1 < len(item_indices) else end
        has_reference = _has_reference(lines, index + 1, detail_end, collector)
        if parsed is None:
            continue
        number, depth = parsed
        if number in first_seen:
            collector.add(
                rules.TASK_NUMBER_DUPLICATE,
                line.number,
                f"Task {number} is already declared on line {first_seen[number]}.",
            )
        else:
            first_seen[number] = line.number
        items.append(
            _TaskItem(
                number=number,
                line=line.number,
                depth=depth,
                index=index,
                has_reference=has_reference,
            )
        )

    _check_task_tree(items, collector)


def _check_task_tree(items: Sequence[_TaskItem], collector: _Collector) -> None:
    """Check parentage and leaf annotations across the whole checklist.

    Parentage is read from the numbers, not from document order. Real plans
    reorder blocks and insert a late subtask under an earlier parent, so a
    positional reading would report parents that are plainly declared.
    """
    declared = {item.number for item in items}
    parents = {item.number.rsplit(".", 1)[0] for item in items if item.depth > 1}

    for item in items:
        if item.depth > 1:
            parent = item.number.rsplit(".", 1)[0]
            if parent not in declared:
                collector.add(
                    rules.TASK_PARENT_UNKNOWN,
                    item.line,
                    f"Task {item.number} names parent task {parent}, which is not declared.",
                )
        if item.number in parents:
            # A parent task is a grouping; its children carry the references.
            continue
        if not item.has_reference:
            collector.add(
                rules.TASK_REFERENCE_MISSING,
                item.line,
                f"Task {item.number} is a leaf task and must reference the "
                "acceptance criteria it satisfies, as "
                "'- _Requirements: 1.1, 1.2_'.",
            )


_BODY_CHECKS = {
    DocumentKind.REQUIREMENTS: _check_requirements_body,
    DocumentKind.TASKS: _check_tasks_body,
}


# --- Entry points ----------------------------------------------------------


def validate_document_text(text: str, *, kind: DocumentKind, file: str) -> ValidationReport:
    """Validate one native document held in memory.

    ``file`` is carried through onto every violation unchanged, so a caller may
    pass whatever path spelling its users will recognize.
    """
    collector = _Collector(file)
    if not text.strip():
        collector.add(
            rules.DOCUMENT_EMPTY,
            _FILE_LEVEL_LINE,
            "The document is empty.",
        )
        return build_report(collector.violations)

    lines, headings = _scan(text, collector)
    _check_title(kind, headings, collector)
    found = _check_sections(kind, lines, headings, collector)
    body_check = _BODY_CHECKS.get(kind)
    if body_check is not None:
        body_check(lines, headings, found, collector)
    return build_report(collector.violations)


def validate_document(
    path: Path | str, *, kind: DocumentKind | None = None, file: str | None = None
) -> ValidationReport:
    """Validate one native document on disk.

    ``kind`` defaults to the kind the filename denotes. A path that is not a
    native document name and carries no explicit kind is a programming error,
    not a document defect, so it raises rather than reporting a violation.
    """
    path = Path(path)
    resolved = kind or kind_for_filename(path.name)
    if resolved is None:
        raise ValueError(f"{path.name!r} is not a native spec document; pass kind= explicitly.")
    return validate_document_text(
        path.read_text(encoding="utf-8"),
        kind=resolved,
        file=file if file is not None else str(path),
    )


def validate_documents(paths: Iterable[Path | str]) -> ValidationReport:
    """Validate several native documents into one ordered report."""
    violations: list[Violation] = []
    for path in paths:
        violations.extend(validate_document(path))
    return build_report(violations)


def iter_rule_ids(report: ValidationReport) -> Iterator[str]:
    """Yield each violation's rule identifier in report order."""
    for violation in report:
        yield violation.rule
