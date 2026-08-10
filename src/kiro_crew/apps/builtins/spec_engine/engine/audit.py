"""Append-only per-spec audit log.

One JSONL file per spec, under the app's state root and never inside a spec
directory. Records are appended and never rewritten: the log's value is that it
says what happened even when the run that wrote it went wrong, and an entry that
a later operation may edit is evidence of nothing.

Failures are loud. An audit append that cannot land raises
:class:`~.state.StatePersistenceError`, so the caller fails its operation rather
than proceeding unrecorded or writing the record into a spec document.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kiro_crew.platform_compat import chmod_safe

from .state import (
    SpecRef,
    StatePersistenceError,
    reject_spec_tree_path,
    state_root,
    utc_now_iso,
)

logger = logging.getLogger(__name__)

#: Directory under the state root holding one file per spec.
AUDIT_DIRNAME = "audit"

#: Log file suffix.
AUDIT_SUFFIX = ".jsonl"

#: Owner-only: audit entries carry initiators, approval postures, and command
#: output, none of which another account on the host needs to read.
AUDIT_FILE_MODE = 0o600

#: Characters kept verbatim in a path segment. Everything else is replaced, so a
#: project path or spec name can never escape the audit directory or collide with
#: a filesystem rule (a colon on Windows, a slash anywhere).
_UNSAFE_SEGMENT_CHARS = re.compile(r"[^A-Za-z0-9._-]+")

#: Cap on a slugged segment so a long project path cannot exceed the filesystem's
#: per-component limit. The fingerprint that follows it carries the identity.
_MAX_SLUG_CHARS = 48

#: Length of the project fingerprint in a directory name. Identity comes from the
#: hash; the readable slug beside it is for whoever opens the directory.
_PROJECT_FINGERPRINT_CHARS = 12


@dataclass(frozen=True)
class AuditEvent:
    """One recorded event. ``detail`` and ``cost`` are optional per event."""

    ts: str
    event: str
    run: str | None = None
    initiator: str | None = None
    detail: dict[str, Any] | None = None
    cost: float | None = None

    def to_json_object(self) -> dict[str, Any]:
        """Serialise, omitting the optional fields that carry no value."""
        record: dict[str, Any] = {"ts": self.ts, "event": self.event}
        if self.run is not None:
            record["run"] = self.run
        if self.initiator is not None:
            record["initiator"] = self.initiator
        if self.detail is not None:
            record["detail"] = self.detail
        if self.cost is not None:
            record["cost"] = self.cost
        return record

    @classmethod
    def from_json_object(cls, record: dict[str, Any]) -> "AuditEvent":
        return cls(
            ts=str(record.get("ts", "")),
            event=str(record.get("event", "")),
            run=record.get("run"),
            initiator=record.get("initiator"),
            detail=record.get("detail") if isinstance(record.get("detail"), dict) else None,
            cost=record.get("cost") if isinstance(record.get("cost"), (int, float)) else None,
        )


def _slug(value: str) -> str:
    cleaned = _UNSAFE_SEGMENT_CHARS.sub("-", value).strip("-")
    return cleaned[:_MAX_SLUG_CHARS] or "unnamed"


def audit_root(root: str | Path | None = None) -> Path:
    """The audit directory: ``<state root>/audit``."""
    base = Path(root) if root is not None else state_root()
    return base / AUDIT_DIRNAME


class AuditLog:
    """Append-only JSONL audit log, one file per spec."""

    def __init__(self, root: str | Path | None = None) -> None:
        resolved = audit_root(root)
        reject_spec_tree_path(resolved)
        self._root = resolved

    @property
    def root(self) -> Path:
        return self._root

    def path_for(self, ref: SpecRef) -> Path:
        """Where *ref*'s log lives.

        The project directory is a readable slug plus a fingerprint of the
        resolved project path: two projects whose basenames match must not share
        a log, and a project path is not usable as a path component.
        """
        digest = hashlib.sha256(ref.project.encode("utf-8")).hexdigest()
        project_dir = f"{_slug(Path(ref.project).name)}-{digest[:_PROJECT_FINGERPRINT_CHARS]}"
        return self._root / project_dir / f"{_slug(ref.name)}{AUDIT_SUFFIX}"

    def append(
        self,
        ref: SpecRef,
        event: str,
        *,
        run: str | None = None,
        initiator: str | None = None,
        detail: dict[str, Any] | None = None,
        cost: float | None = None,
        ts: str | None = None,
    ) -> AuditEvent:
        """Append one event. Raises if it cannot be written."""
        if not event:
            raise ValueError("an audit event needs a name")
        record = AuditEvent(
            ts=ts or utc_now_iso(),
            event=event,
            run=run,
            initiator=initiator,
            detail=detail,
            cost=cost,
        )
        path = self.path_for(ref)
        try:
            line = json.dumps(record.to_json_object(), default=str) + "\n"
        except (TypeError, ValueError) as exc:
            raise StatePersistenceError(
                f"audit event {event!r} for spec {ref.name!r} is not serialisable: {exc}"
            ) from exc
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # O_APPEND with an explicit mode: the log is opened for appending
            # only, so no code path here can truncate or rewrite an earlier entry.
            fd = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, AUDIT_FILE_MODE)
            with os.fdopen(fd, "a", encoding="utf-8") as handle:
                # A crash mid-append can leave a tail line without its newline.
                # Start a fresh line rather than gluing this record onto the
                # fragment, which would make both unreadable. The fragment stays:
                # an append-only log does not repair itself by deleting evidence.
                if self._ends_without_newline(path):
                    handle.write("\n")
                handle.write(line)
        except OSError as exc:
            raise StatePersistenceError(
                f"could not append to the audit log for spec {ref.name!r} at {path}: {exc}"
            ) from exc
        # Enforce the mode even when the file predates this call with a wider one.
        chmod_safe(path, AUDIT_FILE_MODE)
        return record

    @staticmethod
    def _ends_without_newline(path: Path) -> bool:
        try:
            size = path.stat().st_size
        except OSError:
            return False
        if size == 0:
            return False
        with contextlib.suppress(OSError):
            with open(path, "rb") as handle:
                handle.seek(-1, os.SEEK_END)
                return handle.read(1) != b"\n"
        return False

    def read(self, ref: SpecRef, *, limit: int | None = None) -> list[AuditEvent]:
        """Read a spec's events oldest first, or the last *limit* of them.

        An unparseable line is skipped with a warning rather than failing the
        read: a torn tail from a crash must not make the surviving history
        unreadable.
        """
        path = self.path_for(ref)
        if not path.is_file():
            return []
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise StatePersistenceError(
                f"could not read the audit log for spec {ref.name!r} at {path}: {exc}"
            ) from exc
        events: list[AuditEvent] = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                loaded = json.loads(stripped)
            except json.JSONDecodeError:
                logger.warning("skipping unparseable audit line in %s", path)
                continue
            if isinstance(loaded, dict):
                events.append(AuditEvent.from_json_object(loaded))
        if limit is not None and limit >= 0:
            return events[-limit:] if limit else []
        return events
