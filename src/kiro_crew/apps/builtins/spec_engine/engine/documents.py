"""The native spec-document contract, expressed as data.

The three native documents differ only in their title, their required sections,
and which body rules apply, so those differences live here as tables rather than
as branches inside the validator. A format change is then a data edit, which is
also what keeps the contract readable to someone who has not read the validator.
"""

from __future__ import annotations

from enum import Enum
from types import MappingProxyType
from typing import Mapping


class DocumentKind(Enum):
    """One of the native spec documents."""

    REQUIREMENTS = "requirements"
    DESIGN = "design"
    TASKS = "tasks"

    @property
    def filename(self) -> str:
        return f"{self.value}.md"


#: Filename to kind, so a caller holding a path need not restate the mapping.
_FILENAMES: dict[str, DocumentKind] = {kind.filename: kind for kind in DocumentKind}

#: The level-1 heading each kind opens with. Matched as a prefix rather than an
#: equality: real documents append the feature name to the plan title, and a
#: title that names its subject is better than one that does not.
_TITLE_PREFIXES: dict[DocumentKind, str] = {
    DocumentKind.REQUIREMENTS: "Requirements",
    DocumentKind.DESIGN: "Design",
    DocumentKind.TASKS: "Implementation Plan",
}

#: The level-2 sections each kind must carry, in the order a reader expects
#: them. Order is not enforced -- documents legitimately interleave additional
#: sections -- but presence is.
_REQUIRED_SECTIONS: dict[DocumentKind, tuple[str, ...]] = {
    DocumentKind.REQUIREMENTS: ("Introduction", "Requirements"),
    DocumentKind.DESIGN: (
        "Overview",
        "Architecture",
        "Components and Interfaces",
        "Data Models",
        "Error Handling",
        "Testing Strategy",
    ),
    DocumentKind.TASKS: ("Tasks",),
}

TITLE_PREFIXES: Mapping[DocumentKind, str] = MappingProxyType(_TITLE_PREFIXES)
REQUIRED_SECTIONS: Mapping[DocumentKind, tuple[str, ...]] = MappingProxyType(_REQUIRED_SECTIONS)

#: The section whose body carries the numbered requirements.
REQUIREMENTS_SECTION = "Requirements"
#: The section whose body carries the task checklist.
TASKS_SECTION = "Tasks"
#: The level-4 heading that introduces a requirement's acceptance criteria.
ACCEPTANCE_CRITERIA_HEADING = "Acceptance Criteria"


def kind_for_filename(filename: str) -> DocumentKind | None:
    """Return the kind a native filename denotes, or None if it is not one."""
    return _FILENAMES.get(filename.casefold())


def normalize_heading(text: str) -> str:
    """Fold a heading to its comparable form.

    Case and internal whitespace vary between hand-edited and generated
    documents without changing which section a heading names, so comparison
    ignores both. Trailing colons are dropped for the same reason.
    """
    return " ".join(text.strip().rstrip(":").split()).casefold()
