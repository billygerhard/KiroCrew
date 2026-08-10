"""Stable identifiers for the native spec-document format rules.

Every violation the validator reports names one of these identifiers. They are
part of the engine's public contract: tool results quote them, the dashboard
groups by them, and the diagnostic aggregator addresses conditions by identifier
rather than by message text. Renaming one is a breaking change; retire an
identifier instead of repurposing it.

The ``native.`` prefix marks a rule the engine enforces itself. A supplementary
validation provider contributes findings under its own prefix, which keeps the
engine's own findings distinguishable from anything added around them.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Mapping

# --- Rules that apply to any native document -------------------------------

#: The document contains no content at all.
DOCUMENT_EMPTY = "native.document.empty"
#: The document does not open with a level-1 heading.
DOCUMENT_TITLE_MISSING = "native.document.title-missing"
#: The level-1 heading is not the title the document kind requires.
DOCUMENT_TITLE_MISMATCH = "native.document.title-mismatch"
#: More than one level-1 heading; the document title must be unique.
DOCUMENT_TITLE_DUPLICATE = "native.document.title-duplicate"
#: A heading line is not spelled as hashes, one space, then text.
HEADING_MALFORMED = "native.heading.malformed"
#: A section the document kind requires is absent.
SECTION_MISSING = "native.section.missing"
#: A required section heading appears more than once.
SECTION_DUPLICATE = "native.section.duplicate"
#: A required section heading carries no content.
SECTION_EMPTY = "native.section.empty"

# --- requirements.md -------------------------------------------------------

#: The requirements section declares no requirements.
REQUIREMENTS_NONE = "native.requirements.none"
#: A level-3 heading in the requirements section is not a requirement heading.
REQUIREMENT_HEADING_MALFORMED = "native.requirements.heading-malformed"
#: A requirement heading carries a number but no title.
REQUIREMENT_TITLE_MISSING = "native.requirements.title-missing"
#: Requirement numbers do not run sequentially from one.
REQUIREMENT_NUMBER_NOT_SEQUENTIAL = "native.requirements.number-not-sequential"
#: A requirement carries no user story.
USER_STORY_MISSING = "native.requirements.user-story-missing"
#: A user story is not spelled as role, want, and benefit.
USER_STORY_MALFORMED = "native.requirements.user-story-malformed"
#: A requirement carries no acceptance criteria heading.
CRITERIA_SECTION_MISSING = "native.requirements.criteria-section-missing"
#: The acceptance criteria heading is present but lists no criteria.
CRITERIA_EMPTY = "native.requirements.criteria-empty"
#: Criterion numbers within a requirement do not run sequentially from one.
CRITERION_NUMBER_NOT_SEQUENTIAL = "native.requirements.criterion-number-not-sequential"
#: A criterion does not open with an EARS keyword.
CRITERION_KEYWORD_MISSING = "native.criterion.keyword-missing"
#: A criterion states no obligation, so nothing about it is verifiable.
CRITERION_SHALL_MISSING = "native.criterion.shall-missing"
#: A conditional criterion states its precondition but never its consequence.
CRITERION_IF_WITHOUT_THEN = "native.criterion.if-without-then"

# --- tasks.md --------------------------------------------------------------

#: The tasks section declares no tasks.
TASKS_NONE = "native.tasks.none"
#: A task list item's checkbox is not spelled as a native checkbox.
TASK_CHECKBOX_MALFORMED = "native.tasks.checkbox-malformed"
#: A task list item carries no leading number.
TASK_NUMBER_MISSING = "native.tasks.number-missing"
#: A task number nests deeper than a parent and a leaf.
TASK_NUMBER_DEPTH = "native.tasks.number-depth"
#: A task's number depth disagrees with its indentation.
TASK_NUMBER_DEPTH_MISMATCH = "native.tasks.number-depth-mismatch"
#: Two task list items claim the same number.
TASK_NUMBER_DUPLICATE = "native.tasks.number-duplicate"
#: A task list item carries a number but no title.
TASK_TITLE_MISSING = "native.tasks.title-missing"
#: A task list item's indentation is not a native nesting level.
TASK_INDENT_INVALID = "native.tasks.indent-invalid"
#: A subtask's number names a parent task that is not declared.
TASK_PARENT_UNKNOWN = "native.tasks.parent-unknown"
#: A leaf task carries no acceptance-criteria reference.
TASK_REFERENCE_MISSING = "native.tasks.requirements-ref-missing"
#: An acceptance-criteria reference is not spelled as a native reference.
TASK_REFERENCE_MALFORMED = "native.tasks.requirements-ref-malformed"
#: A reference names a requirement that requirements.md does not declare.
TASK_REFERENCE_REQUIREMENT_UNKNOWN = "native.tasks.requirements-ref-requirement-unknown"
#: A reference names a criterion the referenced requirement does not declare.
TASK_REFERENCE_CRITERION_UNKNOWN = "native.tasks.requirements-ref-criterion-unknown"

# --- Coverage of requirements by tasks -------------------------------------

#: No task claims any part of a requirement.
COVERAGE_REQUIREMENT_UNCOVERED = "native.coverage.requirement-uncovered"
#: A requirement is worked on, but one of its criteria is claimed by no task.
COVERAGE_CRITERION_UNCOVERED = "native.coverage.criterion-uncovered"

# --- Dependency graph ------------------------------------------------------

#: The dependency-graph section carries no graph block.
GRAPH_BLOCK_MISSING = "native.graph.block-missing"
#: The graph block does not hold readable JSON.
GRAPH_JSON_MALFORMED = "native.graph.json-malformed"
#: The graph's top level is not an object declaring a list of waves.
GRAPH_ROOT_INVALID = "native.graph.root-invalid"
#: The graph declares no waves.
GRAPH_WAVES_EMPTY = "native.graph.waves-empty"
#: A wave entry is not an object carrying an identifier and a list of tasks.
GRAPH_WAVE_INVALID = "native.graph.wave-invalid"
#: Wave identifiers are not consecutive integers counting from zero.
GRAPH_WAVE_ID_NOT_SEQUENTIAL = "native.graph.wave-id-not-sequential"
#: A wave's task entry is not spelled as a task number.
GRAPH_TASK_ID_MALFORMED = "native.graph.task-id-malformed"
#: A wave schedules a task the plan does not declare.
GRAPH_TASK_UNKNOWN = "native.graph.task-unknown"
#: A wave schedules a task that only groups other tasks.
GRAPH_TASK_NOT_LEAF = "native.graph.task-not-leaf"
#: A task is scheduled in more than one wave.
GRAPH_TASK_DUPLICATE = "native.graph.task-duplicate"
#: An unfinished leaf task is scheduled in no wave.
GRAPH_TASK_UNASSIGNED = "native.graph.task-unassigned"
#: The declared dependencies are not an object mapping a task to its
#: prerequisites.
GRAPH_DEPENDENCIES_INVALID = "native.graph.dependencies-invalid"
#: A declared dependency names a task the graph does not schedule.
GRAPH_DEPENDENCY_UNKNOWN = "native.graph.dependency-unknown"
#: A declared dependency does not sit in an earlier wave than the task needing
#: it.
GRAPH_DEPENDENCY_ORDER = "native.graph.dependency-order"
#: The declared dependencies close a cycle, so no wave order can satisfy them.
GRAPH_CYCLE = "native.graph.cycle"


_DESCRIPTIONS: dict[str, str] = {
    DOCUMENT_EMPTY: "The document has no content.",
    DOCUMENT_TITLE_MISSING: "The document must open with a level-1 heading.",
    DOCUMENT_TITLE_MISMATCH: "The level-1 heading must name the document kind.",
    DOCUMENT_TITLE_DUPLICATE: "A document carries exactly one level-1 heading.",
    HEADING_MALFORMED: "A heading is hash marks, one space, then heading text.",
    SECTION_MISSING: "The document kind requires this section.",
    SECTION_DUPLICATE: "A required section appears exactly once.",
    SECTION_EMPTY: "A required section must carry content.",
    REQUIREMENTS_NONE: "The requirements section must declare at least one requirement.",
    REQUIREMENT_HEADING_MALFORMED: ("A requirement heading reads 'Requirement <number>: <title>'."),
    REQUIREMENT_TITLE_MISSING: "A requirement heading must carry a title.",
    REQUIREMENT_NUMBER_NOT_SEQUENTIAL: "Requirement numbers run sequentially from one.",
    USER_STORY_MISSING: "A requirement must carry a user story.",
    USER_STORY_MALFORMED: (
        "A user story reads 'As a <role>, I want <capability>, so that <benefit>'."
    ),
    CRITERIA_SECTION_MISSING: "A requirement must carry an acceptance criteria heading.",
    CRITERIA_EMPTY: "The acceptance criteria heading must list at least one criterion.",
    CRITERION_NUMBER_NOT_SEQUENTIAL: (
        "Acceptance criterion numbers run sequentially from one within a requirement."
    ),
    CRITERION_KEYWORD_MISSING: "A criterion opens with an EARS keyword.",
    CRITERION_SHALL_MISSING: "A criterion states an obligation with SHALL.",
    CRITERION_IF_WITHOUT_THEN: "A criterion opening with IF states its consequence with THEN.",
    TASKS_NONE: "The tasks section must declare at least one task.",
    TASK_CHECKBOX_MALFORMED: "A task item reads '- [ ] ' with a single status character.",
    TASK_NUMBER_MISSING: "A task item must carry a leading number.",
    TASK_NUMBER_DEPTH: "A task number is a parent number or a parent and leaf number.",
    TASK_NUMBER_DEPTH_MISMATCH: "A task's number depth must match its indentation.",
    TASK_NUMBER_DUPLICATE: "Each task number is declared once.",
    TASK_TITLE_MISSING: "A task item must carry a title.",
    TASK_INDENT_INVALID: "A task item is indented zero or two spaces.",
    TASK_PARENT_UNKNOWN: "A subtask's parent task must be declared.",
    TASK_REFERENCE_MISSING: "A leaf task must reference the acceptance criteria it satisfies.",
    TASK_REFERENCE_MALFORMED: (
        "An acceptance-criteria reference reads '_Requirements: <id>, <id>_'."
    ),
    TASK_REFERENCE_REQUIREMENT_UNKNOWN: (
        "A referenced requirement must be declared in requirements.md."
    ),
    TASK_REFERENCE_CRITERION_UNKNOWN: (
        "A referenced criterion must be declared under its requirement."
    ),
    COVERAGE_REQUIREMENT_UNCOVERED: "Every requirement is claimed by at least one task.",
    COVERAGE_CRITERION_UNCOVERED: "Every acceptance criterion is claimed by at least one task.",
    GRAPH_BLOCK_MISSING: "The dependency-graph section holds a fenced JSON block.",
    GRAPH_JSON_MALFORMED: "The dependency graph is readable JSON.",
    GRAPH_ROOT_INVALID: "The dependency graph is an object with a 'waves' list.",
    GRAPH_WAVES_EMPTY: "A dependency graph declares at least one wave.",
    GRAPH_WAVE_INVALID: (
        "A wave is an object with an integer 'id' and a non-empty 'tasks' list of strings."
    ),
    GRAPH_WAVE_ID_NOT_SEQUENTIAL: "Wave identifiers are consecutive integers counting from zero.",
    GRAPH_TASK_ID_MALFORMED: "A scheduled task is named by its number, as in '1.2'.",
    GRAPH_TASK_UNKNOWN: "A scheduled task must be declared in the tasks section.",
    GRAPH_TASK_NOT_LEAF: "Waves schedule leaf tasks; a parent task is a grouping.",
    GRAPH_TASK_DUPLICATE: "A task is scheduled in exactly one wave.",
    GRAPH_TASK_UNASSIGNED: "Every unfinished leaf task is scheduled in exactly one wave.",
    GRAPH_DEPENDENCIES_INVALID: (
        "Declared dependencies map a task number to a list of task numbers."
    ),
    GRAPH_DEPENDENCY_UNKNOWN: "A dependency must name a task the graph schedules.",
    GRAPH_DEPENDENCY_ORDER: "A dependency runs in an earlier wave than the task that needs it.",
    GRAPH_CYCLE: "The dependency graph is acyclic.",
}

#: Rule identifier to a one-line statement of what the rule requires. Exposed so
#: a caller can render a violation without hard-coding message text of its own.
DESCRIPTIONS: Mapping[str, str] = MappingProxyType(_DESCRIPTIONS)

#: Every identifier the native-format validator can emit. Held as data so a
#: change to the published set is visible rather than incidental.
ALL_RULES: frozenset[str] = frozenset(_DESCRIPTIONS)


def describe(rule: str) -> str:
    """Return the requirement statement for ``rule``, or an empty string."""
    return _DESCRIPTIONS.get(rule, "")
