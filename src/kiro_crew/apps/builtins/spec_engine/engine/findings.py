"""The shape of a validation result.

A violation is addressable rather than merely readable: it names the file, the
location inside it, and the rule identifier that was broken, so a driver can
route it, a diagnostic can quote it, and a fix can be checked against it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, Iterator


class Severity(str, Enum):
    """How much a violation costs.

    ``ERROR`` means the document does not satisfy the native format and any gate
    that depends on validation must refuse. ``WARNING`` means the document is
    structurally readable but carries a defect worth surfacing. Inheriting from
    ``str`` keeps the value directly serializable for tool results and the audit
    log without a custom encoder.
    """

    ERROR = "error"
    WARNING = "warning"


@dataclass(frozen=True, order=True)
class Location:
    """Where in a document a violation sits.

    ``line`` and ``column`` are 1-based to match how editors and compilers
    address text. ``column`` is optional because some violations are about a
    whole line, or about something a line fails to contain, where pointing at a
    column would invent precision the finding does not have.
    """

    line: int
    column: int | None = None

    def __post_init__(self) -> None:
        if self.line < 1:
            raise ValueError(f"line must be 1-based, got {self.line}")
        if self.column is not None and self.column < 1:
            raise ValueError(f"column must be 1-based, got {self.column}")

    def __str__(self) -> str:
        return f"{self.line}" if self.column is None else f"{self.line}:{self.column}"


@dataclass(frozen=True)
class Violation:
    """One broken rule at one place."""

    file: str
    location: Location
    rule: str
    severity: Severity
    message: str

    @property
    def sort_key(self) -> tuple[str, int, int, str]:
        """Ordering that groups a report by file and reads down the document."""
        return (self.file, self.location.line, self.location.column or 0, self.rule)

    def __str__(self) -> str:
        return f"{self.file}:{self.location}: {self.severity.value}: {self.rule}: {self.message}"


@dataclass(frozen=True)
class ValidationReport:
    """Every violation found, in document order.

    The report is complete by construction: validation collects violations
    rather than raising on the first one, because a caller fixing a document
    needs the whole list and an agent given one error at a time burns a turn per
    defect.
    """

    violations: tuple[Violation, ...] = field(default=())

    @property
    def ok(self) -> bool:
        """True when nothing blocking was found. Warnings do not fail a report."""
        return not any(v.severity is Severity.ERROR for v in self.violations)

    @property
    def errors(self) -> tuple[Violation, ...]:
        return tuple(v for v in self.violations if v.severity is Severity.ERROR)

    @property
    def warnings(self) -> tuple[Violation, ...]:
        return tuple(v for v in self.violations if v.severity is Severity.WARNING)

    @property
    def rule_ids(self) -> tuple[str, ...]:
        """The rule identifiers present, deduplicated, in first-seen order."""
        seen: dict[str, None] = {}
        for violation in self.violations:
            seen.setdefault(violation.rule, None)
        return tuple(seen)

    def for_rule(self, rule: str) -> tuple[Violation, ...]:
        return tuple(v for v in self.violations if v.rule == rule)

    def for_file(self, file: str) -> tuple[Violation, ...]:
        return tuple(v for v in self.violations if v.file == file)

    def __iter__(self) -> Iterator[Violation]:
        return iter(self.violations)

    def __len__(self) -> int:
        return len(self.violations)

    def __bool__(self) -> bool:
        """A report is truthy when it holds violations, so ``if report:`` reads
        as "something was found" rather than inverting ``ok``."""
        return bool(self.violations)


def build_report(violations: Iterable[Violation]) -> ValidationReport:
    """Order ``violations`` by file and position and freeze them into a report."""
    return ValidationReport(tuple(sorted(violations, key=lambda v: v.sort_key)))
