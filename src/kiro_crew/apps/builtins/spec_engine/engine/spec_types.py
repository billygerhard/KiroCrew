"""Spec types, the document plan each one implies, and the sidecar that records it.

A spec's type is not a label on the side: it decides which documents the spec
owes, which gates it passes through, and therefore what validation and
advancement mean for it. So the type has to be *recorded* rather than inferred,
and it is recorded in the one file that both the engine and the Kiro IDE read:
the ``.config.kiro`` sidecar inside the spec directory.

Two consequences shape this module.

**The sidecar is the authority, not the engine's own registry.** A spec created
in the IDE has a sidecar and no row in the engine's state store, and it must
still validate and advance correctly. Every derivation here therefore reads the
sidecar; the ``specs`` table mirrors the type for listing and reporting, and a
mirror that disagrees with the sidecar loses.

**A spec with no readable type is unusable, not merely undocumented.** Nothing
can say which documents it needs, so validation and advancement have no plan to
apply. Rather than guessing a default -- which would silently hold a bugfix to a
feature's document set -- every entry point here raises
:class:`SpecTypeUnrecorded`, and callers turn that into a refusal.

The three plans deliberately map onto the same three native filenames. The IDE
and CLI know ``requirements.md``, ``design.md``, and ``tasks.md``; a bugfix spec
whose analysis lived in ``bug-analysis.md`` would be a file those tools do not
read. What the type changes is which of those documents the spec owes and what
each one is *for*, which is carried as the document's label.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from .documents import DocumentKind
from .findings import ValidationReport, Violation, build_report
from .native_format import validate_document

logger = logging.getLogger(__name__)

#: The native sidecar inside every spec directory. Shared with the Kiro IDE and
#: CLI, which is why the engine writes only the keys below into it and preserves
#: every other key it finds.
SIDECAR_FILENAME = ".config.kiro"

#: Sidecar keys the engine owns.
SPEC_ID_KEY = "specId"
WORKFLOW_TYPE_KEY = "workflowType"
SPEC_TYPE_KEY = "specType"

#: The workflow every plan follows: requirements are authored first, and each
#: later document depends on the approved one before it. Recorded in the sidecar
#: because the IDE reads it; it is a property of the plan, not a separate choice,
#: so it is derived rather than accepted as an argument.
REQUIREMENTS_FIRST_WORKFLOW = "requirements-first"

#: Why a spec has no usable type. Carried on :class:`SpecTypeUnrecorded` so a
#: caller can distinguish "never recorded" from "recorded as something this
#: engine has no plan for" without matching on message text.
REASON_SIDECAR_MISSING = "sidecar-missing"
REASON_SIDECAR_UNREADABLE = "sidecar-unreadable"
REASON_TYPE_ABSENT = "type-absent"
REASON_TYPE_UNKNOWN = "type-unknown"

#: Suffix for the temporary file a sidecar update is written through. The update
#: lands with :func:`os.replace`, so a crash mid-write cannot truncate a sidecar
#: that already carries a spec's identity.
_TEMP_SUFFIX = ".tmp"

#: Bytes of randomness in that temporary file's name, so two writers cannot
#: collide on it.
_TEMP_TOKEN_BYTES = 6


class SpecTypeError(Exception):
    """Base class for spec-type failures."""


class UnknownSpecType(SpecTypeError):
    """A caller named a spec type the engine has no document plan for.

    Distinct from :class:`SpecTypeUnrecorded`: this is a bad argument, caught
    before anything is created, whereas that one describes a spec already on
    disk.
    """

    def __init__(self, value: object) -> None:
        known = ", ".join(spec_type.value for spec_type in SpecType)
        super().__init__(f"unknown spec type {value!r}; the engine plans for {known}")
        self.value = value


class SpecTypeUnrecorded(SpecTypeError):
    """The spec on disk carries no spec type this engine can act on.

    Raised instead of falling back to a default plan. A spec whose type is
    unreadable cannot be validated or advanced, because neither operation knows
    which documents it is judging.
    """

    def __init__(self, spec_dir: Path | str, reason: str, detail: str = "") -> None:
        suffix = f": {detail}" if detail else ""
        super().__init__(
            f"spec at {spec_dir} has no recorded spec type ({reason}){suffix}; "
            f"validation and advancement are refused until one is recorded"
        )
        self.spec_dir = Path(spec_dir)
        self.reason = reason
        self.detail = detail


class SpecType(Enum):
    """The three process weights a spec can carry."""

    FEATURE = "feature"
    BUGFIX = "bugfix"
    QUICK = "quick"

    @classmethod
    def parse(cls, value: object) -> "SpecType | None":
        """Return the type ``value`` names, or ``None`` if it names none.

        Tolerant about case and surrounding whitespace because the sidecar is
        hand-editable, strict about the vocabulary because an unrecognized value
        has no plan and must not be rounded to one that does.
        """
        if not isinstance(value, str):
            return None
        try:
            return cls(value.strip().casefold())
        except ValueError:
            return None

    @classmethod
    def require(cls, value: object) -> "SpecType":
        """Return the type ``value`` names, raising :class:`UnknownSpecType`."""
        if isinstance(value, cls):
            return value
        parsed = cls.parse(value)
        if parsed is None:
            raise UnknownSpecType(value)
        return parsed

    @property
    def plan(self) -> "DocumentPlan":
        """The document plan this type implies."""
        return PLANS[self]


@dataclass(frozen=True)
class PlannedDocument:
    """One document a plan calls for.

    ``label`` is what the document is for under this plan. It is the only thing
    that differs between a feature's ``requirements.md`` and a bugfix's, and it
    is what authoring guidance and the dashboard show, so it lives with the plan
    rather than in prose somewhere downstream.
    """

    kind: DocumentKind
    label: str

    @property
    def filename(self) -> str:
        return self.kind.filename

    @property
    def gate(self) -> str:
        """The approval gate this document passes through.

        One document, one gate, named after the document, so the phase machine
        and the plan cannot drift into two vocabularies for the same thing.
        """
        return self.kind.value


@dataclass(frozen=True)
class DocumentPlan:
    """The ordered document set one spec type owes."""

    spec_type: SpecType
    documents: tuple[PlannedDocument, ...]

    @property
    def kinds(self) -> tuple[DocumentKind, ...]:
        return tuple(document.kind for document in self.documents)

    @property
    def filenames(self) -> tuple[str, ...]:
        return tuple(document.filename for document in self.documents)

    @property
    def gates(self) -> tuple[str, ...]:
        """The gates, in the order the plan passes through them."""
        return tuple(document.gate for document in self.documents)

    @property
    def workflow_type(self) -> str:
        return REQUIREMENTS_FIRST_WORKFLOW

    def includes(self, kind: DocumentKind) -> bool:
        return kind in self.kinds

    def document_for(self, kind: DocumentKind) -> PlannedDocument | None:
        for document in self.documents:
            if document.kind is kind:
                return document
        return None

    def label_for(self, kind: DocumentKind) -> str:
        """The label this plan gives ``kind``, or an empty string when off-plan."""
        document = self.document_for(kind)
        return document.label if document is not None else ""


_PLANS: dict[SpecType, DocumentPlan] = {
    SpecType.FEATURE: DocumentPlan(
        spec_type=SpecType.FEATURE,
        documents=(
            PlannedDocument(DocumentKind.REQUIREMENTS, "Requirements"),
            PlannedDocument(DocumentKind.DESIGN, "Design"),
            PlannedDocument(DocumentKind.TASKS, "Implementation plan"),
        ),
    ),
    # A bugfix owes the same three files with different jobs: the requirements
    # document is the investigation (symptoms, reproduction, root cause, expected
    # behaviour) and the design document is the fix approach.
    SpecType.BUGFIX: DocumentPlan(
        spec_type=SpecType.BUGFIX,
        documents=(
            PlannedDocument(DocumentKind.REQUIREMENTS, "Bug analysis"),
            PlannedDocument(DocumentKind.DESIGN, "Fix design"),
            PlannedDocument(DocumentKind.TASKS, "Implementation plan"),
        ),
    ),
    # Quick deliberately has no design gate. Demanding one is what turned every
    # small change into a three-document exercise and is the reason the type
    # exists at all.
    SpecType.QUICK: DocumentPlan(
        spec_type=SpecType.QUICK,
        documents=(
            PlannedDocument(DocumentKind.REQUIREMENTS, "Requirements"),
            PlannedDocument(DocumentKind.TASKS, "Implementation plan"),
        ),
    ),
}

#: Spec type to the documents it owes. Read-only so a caller cannot reshape one
#: spec type's plan for every other caller in the process.
PLANS: Mapping[SpecType, DocumentPlan] = MappingProxyType(_PLANS)


def plan_of(spec_type: SpecType | str) -> DocumentPlan:
    """The plan of a named spec type, raising :class:`UnknownSpecType`."""
    return SpecType.require(spec_type).plan


@dataclass(frozen=True)
class Sidecar:
    """The sidecar's contents, with the recorded type already resolved.

    ``extra`` holds every key the engine does not own. It is carried so an update
    can write the engine's keys back without dropping whatever the IDE keeps
    beside them.
    """

    spec_id: str
    workflow_type: str
    spec_type: SpecType
    extra: Mapping[str, Any] = MappingProxyType({})

    @property
    def plan(self) -> DocumentPlan:
        return self.spec_type.plan

    def to_json_object(self) -> dict[str, Any]:
        """Serialise with the engine's keys first, in their conventional order."""
        record: dict[str, Any] = {
            SPEC_ID_KEY: self.spec_id,
            WORKFLOW_TYPE_KEY: self.workflow_type,
            SPEC_TYPE_KEY: self.spec_type.value,
        }
        record.update(self.extra)
        return record


def sidecar_path(spec_dir: Path | str) -> Path:
    return Path(spec_dir) / SIDECAR_FILENAME


def new_spec_id() -> str:
    """A fresh spec identifier: a random UUID, as the native sidecar uses."""
    return str(uuid.uuid4())


def build_sidecar(
    spec_type: SpecType | str,
    *,
    spec_id: str | None = None,
    extra: Mapping[str, Any] | None = None,
) -> Sidecar:
    """Build the sidecar for a spec of ``spec_type``.

    Raises :class:`UnknownSpecType` before anything is written, which is what
    keeps an unrecordable type from reaching the filesystem at all.
    """
    resolved = SpecType.require(spec_type)
    return Sidecar(
        spec_id=spec_id or new_spec_id(),
        workflow_type=resolved.plan.workflow_type,
        spec_type=resolved,
        extra=MappingProxyType(dict(extra or {})),
    )


def read_sidecar_document(spec_dir: Path | str) -> dict[str, Any]:
    """Return the sidecar's raw JSON object.

    Raises :class:`SpecTypeUnrecorded` when there is nothing usable to read: an
    absent, unreadable, or non-object sidecar all leave the spec with no
    recorded type, which is the same refusal from a caller's point of view.
    """
    path = sidecar_path(spec_dir)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise SpecTypeUnrecorded(
            spec_dir, REASON_SIDECAR_MISSING, f"no {SIDECAR_FILENAME}"
        ) from exc
    except OSError as exc:
        raise SpecTypeUnrecorded(spec_dir, REASON_SIDECAR_UNREADABLE, str(exc)) from exc
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SpecTypeUnrecorded(spec_dir, REASON_SIDECAR_UNREADABLE, str(exc)) from exc
    if not isinstance(loaded, dict):
        raise SpecTypeUnrecorded(
            spec_dir,
            REASON_SIDECAR_UNREADABLE,
            f"{SIDECAR_FILENAME} does not hold a JSON object",
        )
    return loaded


def read_sidecar(spec_dir: Path | str) -> Sidecar:
    """Read the sidecar and resolve its recorded type.

    Raises :class:`SpecTypeUnrecorded` when the type is absent or names a plan
    this engine does not have.
    """
    document = read_sidecar_document(spec_dir)
    raw_type = document.get(SPEC_TYPE_KEY)
    if raw_type is None or (isinstance(raw_type, str) and not raw_type.strip()):
        raise SpecTypeUnrecorded(spec_dir, REASON_TYPE_ABSENT, f"no {SPEC_TYPE_KEY}")
    spec_type = SpecType.parse(raw_type)
    if spec_type is None:
        raise SpecTypeUnrecorded(spec_dir, REASON_TYPE_UNKNOWN, f"{SPEC_TYPE_KEY}={raw_type!r}")
    spec_id = document.get(SPEC_ID_KEY)
    workflow_type = document.get(WORKFLOW_TYPE_KEY)
    if not isinstance(workflow_type, str) or not workflow_type.strip():
        # An absent or non-string workflow reads as the plan's own: the plan is
        # what the engine acts on, and a missing hint changes nothing about which
        # documents the spec owes.
        workflow_type = spec_type.plan.workflow_type
    return Sidecar(
        spec_id=spec_id if isinstance(spec_id, str) else "",
        workflow_type=workflow_type,
        spec_type=spec_type,
        extra=MappingProxyType(_foreign_keys(document)),
    )


def _foreign_keys(document: Mapping[str, Any]) -> dict[str, Any]:
    """Every sidecar key the engine does not own, so an update can keep them."""
    owned = {SPEC_ID_KEY, WORKFLOW_TYPE_KEY, SPEC_TYPE_KEY}
    return {key: value for key, value in document.items() if key not in owned}


def recorded_spec_type(spec_dir: Path | str) -> SpecType:
    """The spec's recorded type. This is the guard every gate calls first."""
    return read_sidecar(spec_dir).spec_type


def plan_for(spec_dir: Path | str) -> DocumentPlan:
    """The document plan of the type recorded for the spec at ``spec_dir``."""
    return recorded_spec_type(spec_dir).plan


def write_sidecar_document(spec_dir: Path | str, document: Mapping[str, Any]) -> Path:
    """Write ``document`` to the sidecar, replacing it in one step.

    The content is written to a temporary file beside the sidecar and then moved
    onto it, so an interrupted write leaves the previous sidecar intact rather
    than a truncated one. The spec's identity lives in that file; losing it to a
    half-written update would orphan the spec for the IDE as well as the engine.
    """
    target = sidecar_path(spec_dir)
    body = json.dumps(dict(document), indent=2) + "\n"
    temp = target.with_name(f"{target.name}.{secrets.token_hex(_TEMP_TOKEN_BYTES)}{_TEMP_SUFFIX}")
    try:
        temp.write_text(body, encoding="utf-8")
        os.replace(temp, target)
    finally:
        # A failed replace leaves the temporary file behind, and a spec directory
        # holds only the native documents and the sidecar.
        if temp.exists():
            try:
                temp.unlink()
            except OSError:  # pragma: no cover - best effort on a failed write
                logger.warning("could not remove the temporary sidecar file %s", temp)
    return target


def write_sidecar(
    spec_dir: Path | str,
    spec_type: SpecType | str,
    *,
    spec_id: str | None = None,
) -> Sidecar:
    """Record ``spec_type`` in the spec's sidecar, keeping every foreign key.

    An existing sidecar's identifier is reused unless ``spec_id`` overrides it: a
    spec's identity outlives a change of type, and minting a new one would break
    anything already referring to the spec.
    """
    resolved = SpecType.require(spec_type)
    existing: dict[str, Any] = {}
    if sidecar_path(spec_dir).exists():
        try:
            existing = read_sidecar_document(spec_dir)
        except SpecTypeUnrecorded:
            # An unreadable sidecar carries nothing worth preserving; recording
            # the type is exactly what repairs it.
            logger.warning("replacing an unreadable %s at %s", SIDECAR_FILENAME, spec_dir)
    extra = _foreign_keys(existing)
    inherited = existing.get(SPEC_ID_KEY)
    sidecar = build_sidecar(
        resolved,
        spec_id=spec_id or (inherited if isinstance(inherited, str) and inherited else None),
        extra=extra,
    )
    write_sidecar_document(spec_dir, sidecar.to_json_object())
    return sidecar


# --- Applying the plan -----------------------------------------------------


def documents_on_disk(spec_dir: Path | str) -> dict[DocumentKind, Path]:
    """Every native document present under ``spec_dir``, by kind."""
    root = Path(spec_dir)
    present: dict[DocumentKind, Path] = {}
    for kind in DocumentKind:
        path = root / kind.filename
        if path.is_file():
            present[kind] = path
    return present


def missing_documents(
    spec_dir: Path | str, *, plan: DocumentPlan | None = None
) -> tuple[DocumentKind, ...]:
    """Planned documents that are not on disk yet, in plan order.

    Absence is reported rather than judged. Mid-authoring it is the normal state
    of a spec, and only the phase machine knows whether a particular gate should
    refuse over it.
    """
    resolved = plan if plan is not None else plan_for(spec_dir)
    present = documents_on_disk(spec_dir)
    return tuple(kind for kind in resolved.kinds if kind not in present)


def off_plan_documents(
    spec_dir: Path | str, *, plan: DocumentPlan | None = None
) -> tuple[DocumentKind, ...]:
    """Native documents present that the recorded type does not call for.

    A design document under a quick spec is the case this exists for: it is not
    an error -- someone may have deliberately written one -- but the plan does
    not govern it, so it is surfaced instead of being validated silently.
    """
    resolved = plan if plan is not None else plan_for(spec_dir)
    present = documents_on_disk(spec_dir)
    return tuple(kind for kind in DocumentKind if kind in present and not resolved.includes(kind))


def validate_spec_documents(
    spec_dir: Path | str, *, plan: DocumentPlan | None = None
) -> ValidationReport:
    """Validate the documents the recorded type calls for.

    Refuses with :class:`SpecTypeUnrecorded` when no type is recorded: applying
    a plan the spec never declared would hold it to the wrong document set, and
    a plausible-looking pass is worse than a refusal.

    Only planned documents that exist are validated. A planned document that is
    absent is reported by :func:`missing_documents`, and an off-plan document is
    left alone -- the plan decides what is judged.
    """
    resolved = plan if plan is not None else plan_for(spec_dir)
    root = Path(spec_dir)
    violations: list[Violation] = []
    for kind in resolved.kinds:
        path = root / kind.filename
        if path.is_file():
            violations.extend(validate_document(path, kind=kind))
    return build_report(violations)
