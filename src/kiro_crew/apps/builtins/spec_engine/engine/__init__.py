"""Spec_Engine — spec rules as code.

Native-format validation is the engine's baseline: it always runs in the engine
itself, and any additional validation a provider contributes is added to its
findings rather than allowed to replace them.
"""

from __future__ import annotations

from .documents import DocumentKind, kind_for_filename
from .findings import Location, Severity, ValidationReport, Violation
from .native_format import validate_document, validate_document_text, validate_documents

__all__ = [
    "DocumentKind",
    "Location",
    "Severity",
    "ValidationReport",
    "Violation",
    "kind_for_filename",
    "validate_document",
    "validate_document_text",
    "validate_documents",
]
