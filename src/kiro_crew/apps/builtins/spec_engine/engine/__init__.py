"""Spec_Engine — spec rules as code.

Native-format validation is the engine's baseline: it always runs in the engine
itself, and any additional validation a provider contributes is added to its
findings rather than allowed to replace them. Validation has two layers, and
both are part of that baseline: the format of each document on its own, and the
claims that only hold between documents -- task links, requirement coverage, and
the schedulability of the dependency graph.
"""

from __future__ import annotations

from .cross_document import (
    check_cross_document,
    check_dependency_graph,
    check_requirement_coverage,
    check_task_links,
    validate_spec,
    validate_tasks,
)
from .documents import DocumentKind, kind_for_filename
from .findings import Location, Severity, ValidationReport, Violation
from .native_format import validate_document, validate_document_text, validate_documents
from .structure import RequirementsIndex, TaskPlan, parse_requirements, parse_tasks

__all__ = [
    "DocumentKind",
    "Location",
    "RequirementsIndex",
    "Severity",
    "TaskPlan",
    "ValidationReport",
    "Violation",
    "check_cross_document",
    "check_dependency_graph",
    "check_requirement_coverage",
    "check_task_links",
    "kind_for_filename",
    "parse_requirements",
    "parse_tasks",
    "validate_document",
    "validate_document_text",
    "validate_documents",
    "validate_spec",
    "validate_tasks",
]
