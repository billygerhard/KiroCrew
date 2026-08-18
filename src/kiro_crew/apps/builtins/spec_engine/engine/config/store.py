"""The config store: the one validated door in and out of the config document.

Every configuration surface — the dashboard config panel, the setup assistant —
writes through :meth:`ConfigStore.write` and nothing else. A second writer is
not a convenience, it is a second validator: the moment one surface can persist
a document the other's rules would have rejected, "validated configuration"
stops being a property of the file and becomes a property of whichever code
happened to touch it last.

The write path holds three properties:

* **Validate the merged result, not the patch.** A patch that is fine in
  isolation can still produce an invalid document, so validation runs on what
  would land on disk.
* **Serialize read-modify-write.** Two surfaces saving at once would otherwise
  last-write-wins away each other's sections. The lock is a dedicated lock file
  taken through ``platform_compat`` so the app works on Windows too.
* **Refuse config-only writes from a surface no human confirmed.** The autonomy
  policy, the delivery workflow, and capability bindings are the objects that
  decide how far a run proceeds unattended, which commands it runs, and who
  answers for its judgment. They are writable only from an operator-confirmed
  surface, so no engine or tool path can widen its own authority.
* **Record who wrote, durably.** Every accepted write appends one line to
  :data:`WRITE_LOG_FILENAME` naming the surface, the actor the surface says
  authorized it, and which paths were touched. A log line in the gateway's
  output is not that record: it rotates, it is not there on a host that ran the
  MCP server as its own process, and the approver a setup apply demands would
  survive nowhere.

The document lives in the app's data directory, never inside a spec directory:
the Kiro IDE and CLI read the same ``.kiro/specs/<name>/`` trees, so anything
the engine adds there would be a foreign file in someone else's contract.

Reading the document back out to somewhere it can be displayed is the other half
of this module's job, and it is why the secret classification lives here rather
than at a surface: a value this document can hold — an access token in a
capability's environment, a credential in a project's variables — must be elided
by whoever hands the document to an agent or a page, and a classification each
surface spelled for itself is a classification one of them gets wrong.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from kiro_crew import platform_compat
from kiro_crew.apps.manager import app_data_dir
from kiro_crew.atomic_write import atomic_write

from ..state import utc_now_iso
from .advisories import ConfigWarning, WarningRecorder, document_warnings, record_config_warnings
from .effective import EffectiveValue, resolve, resolve_all
from .schema import (
    CURRENT_VERSION,
    VERSION_KEY,
    ConfigError,
    ConfigValidationError,
    config_only_paths,
    validate_config_document,
)
from .settings import SETTINGS

logger = logging.getLogger(__name__)

#: App identity used for the data directory. The published app name is gated on
#: sign-off; this is the working name and the directory it owns.
APP_NAME = "spec-engine"

CONFIG_FILENAME = "config.json"
_LOCK_FILENAME = ".config.lock"

#: Append-only record of every accepted write: one JSON object per line, holding
#: who wrote and what they touched, never a written value.
WRITE_LOG_FILENAME = "config-writes.jsonl"

#: Owner-only: the document names the projects a user works on, their branch
#: layout, and the commands the app may run on their behalf.
_DIR_MODE = 0o700
_FILE_MODE = 0o600

#: Path segments that mark a spec directory. Engine state must never resolve
#: inside one.
_SPEC_DIR_SEGMENTS = (".kiro", "specs")

#: Substituted for a value classified as secret. A marker rather than an omitted
#: key, so a caller can tell "this document holds a token" from "this document
#: holds no such setting" -- the second would send an operator looking for a
#: value that is already there.
ELIDED = "<elided>"

#: Key segments that name a credential. Matched against the LAST segment of a
#: key, because the last segment is what the value IS: ``api_key`` and
#: ``GITHUB_TOKEN`` hold a credential, while ``token_bucket_size`` holds a size
#: and ``key_order`` holds an order. A substring test would elide both of those,
#: and once a caller sees ordinary settings elided it stops reading the marker as
#: meaning anything.
SECRET_KEY_SEGMENTS: frozenset[str] = frozenset(
    {
        "credential",
        "credentials",
        "key",
        "keys",
        "passphrase",
        "passwd",
        "password",
        "passwords",
        "secret",
        "secrets",
        "token",
        "tokens",
    }
)

#: Splits a key into segments: on any non-alphanumeric run (``api_key``,
#: ``api-key``, ``api.key``) and at a lower-to-upper transition (``apiKey``), so
#: one classification covers the snake_case this schema uses, the SCREAMING_SNAKE
#: of an environment map, and a camelCase key a caller pasted in.
_SEGMENT_BOUNDARY = re.compile(r"[^0-9A-Za-z]+|(?<=[a-z0-9])(?=[A-Z])")


@dataclass(frozen=True)
class ConfigWriteSurface:
    """A named surface allowed to write configuration.

    ``operator_confirmed`` records that a human is looking at the change on that
    surface. It gates the config-only sections; a surface that cannot claim it
    may still write ordinary settings.
    """

    name: str
    operator_confirmed: bool = False


#: The dashboard configuration panel: an authenticated human editing settings.
DASHBOARD_SURFACE = ConfigWriteSurface("dashboard", operator_confirmed=True)

#: The setup assistant, which proposes configuration and writes it only after
#: the user approves each inference.
SETUP_ASSISTANT_SURFACE = ConfigWriteSurface("setup-assistant", operator_confirmed=True)


class ConfigWriteRefused(PermissionError):
    """Raised when a surface writes a path it is not allowed to write."""

    def __init__(self, surface: ConfigWriteSurface, paths: tuple[str, ...]) -> None:
        self.surface = surface
        self.paths = paths
        super().__init__(
            f"surface {surface.name!r} may not write config-only paths: " + ", ".join(paths)
        )


class ConfigLoadError(RuntimeError):
    """Raised when the persisted document cannot be read or parsed."""


class ConfigRecordError(RuntimeError):
    """Raised when an accepted write could not be recorded.

    Loud on purpose, and the message says the document WAS persisted: the record
    is what names the human a setup apply demanded an approver for, so a write
    that lands unrecorded must not read as an ordinary success. A caller that
    sees this knows both facts -- the configuration changed, and nothing says who
    changed it -- which is the only pair of facts that leads to the right
    reaction.
    """


def key_segments(key: str) -> tuple[str, ...]:
    """Split *key* into its lowercased segments.

    Segment-wise rather than substring, because a substring rule cannot tell
    ``api_key`` from ``token_bucket_size``.
    """
    return tuple(part.lower() for part in _SEGMENT_BOUNDARY.split(key) if part)


def is_secret_key(key: str) -> bool:
    """Whether a value stored under *key* is classified as a credential.

    True when the key's LAST segment names a credential. The last segment is the
    noun the key is about: ``env.GITHUB_TOKEN`` is a token, ``variables.api_key``
    is a key, and ``limits.token_bucket_size`` is a size that merely mentions one.

    **What this cannot see**, stated because it bounds what elision protects: the
    classification reads the NAME only, never the value. A credential stored under
    an innocent last segment — ``variables.deploy_target``, ``env.MY_SETTING`` —
    is not withheld, and no entropy or format test is applied that might catch it,
    because a value test that guessed would either withhold ordinary settings or
    give a caller reason to believe an unwithheld value was checked. Elision is
    therefore a display convenience over a naming convention, not a containment
    boundary: the FILE holds every value verbatim, so the file's own permissions
    remain what actually protects a credential.
    """
    segments = key_segments(key)
    return bool(segments) and segments[-1] in SECRET_KEY_SEGMENTS


@dataclass(frozen=True)
class ElidedDocument:
    """A document safe to display, plus the dotted paths whose value was removed.

    The paths are reported rather than left implicit so a surface can tell an
    operator *what* was withheld. Without them, a page showing ``<elided>`` in
    three places and a caller checking whether a token is configured cannot tell
    an elision from a literal value someone typed.
    """

    document: dict[str, Any]
    paths: tuple[str, ...]


def elide_secrets(document: Mapping[str, Any]) -> ElidedDocument:
    """Return *document* with every secret-classified value replaced by :data:`ELIDED`.

    A secret-classified key elides its whole value, container included: a
    ``credentials`` object holding three fields is three secrets, and descending
    into it to elide the leaves would leak the field names of the thing being
    withheld.
    """
    found: list[str] = []
    elided = _elide(document, "", found)
    return ElidedDocument(document=elided if isinstance(elided, dict) else {}, paths=tuple(found))


def _elide(node: Any, prefix: str, found: list[str]) -> Any:
    """Rebuild *node* with secret-classified values replaced, recording paths."""
    if isinstance(node, Mapping):
        rebuilt: dict[str, Any] = {}
        for raw_key, value in node.items():
            name = str(raw_key)
            path = f"{prefix}.{name}" if prefix else name
            if is_secret_key(name):
                found.append(path)
                rebuilt[name] = ELIDED
            else:
                rebuilt[name] = _elide(value, path, found)
        return rebuilt
    if isinstance(node, Sequence) and not isinstance(node, (str, bytes)):
        # Lists are walked too: quality gates are a list of objects, so a secret
        # under one of them is at `quality_gates[1].token`, not under a key.
        return [_elide(item, f"{prefix}[{index}]", found) for index, item in enumerate(node)]
    return node


def default_root() -> Path:
    """Return the app data directory holding the config document."""
    return app_data_dir(APP_NAME)


class ConfigStore:
    """Reads and writes the spec engine configuration document."""

    def __init__(self, root: Path | None = None) -> None:
        resolved = Path(root) if root is not None else default_root()
        _reject_spec_directory(resolved)
        self._root = resolved

    @property
    def root(self) -> Path:
        """Directory holding the document."""
        return self._root

    @property
    def path(self) -> Path:
        """Path of the config document, whether or not it exists yet."""
        return self._root / CONFIG_FILENAME

    # --- reads -------------------------------------------------------------

    def document(self) -> dict[str, Any]:
        """Return the persisted document, or an empty one when nothing is saved.

        An absent file is the zero-configuration case, not an error: every
        setting resolves to its bundled default from an empty document.
        """
        path = self.path
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except OSError as exc:
            raise ConfigLoadError(f"cannot read {path}: {exc}") from exc
        if not raw.strip():
            return {}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ConfigLoadError(f"{path} is not valid JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ConfigLoadError(f"{path} must contain a JSON object")
        return parsed

    def validate(self) -> tuple[ConfigError, ...]:
        """Return every problem in the persisted document, empty when valid.

        Returns rather than raises so a diagnostic can report configuration
        problems alongside everything else that is wrong instead of aborting on
        the first one.
        """
        return validate_config_document(self.document())

    def advisories(
        self,
        *,
        recorder: WarningRecorder | None = None,
    ) -> tuple[ConfigWarning, ...]:
        """Return advisories for the persisted document: valid but dangerous setups.

        Separate from :meth:`validate` because the two call for different
        handling: a validation error means the document is not usable, while an
        advisory means it is usable and somebody should know what they armed.
        """
        return record_config_warnings(document_warnings(self.document()), recorder)

    def effective(
        self,
        key: str,
        *,
        project: str | None = None,
        source: str | None = None,
    ) -> EffectiveValue:
        """Return the value in force for *key* together with its origin."""
        setting = SETTINGS.get(key)
        if setting is None:
            raise KeyError(f"unknown setting: {key}")
        return resolve(self.document(), setting, project=project, source=source)

    def effective_settings(
        self,
        *,
        project: str | None = None,
        source: str | None = None,
    ) -> dict[str, EffectiveValue]:
        """Return every setting's effective value and origin, keyed by dotted key."""
        return resolve_all(self.document(), project=project, source=source)

    # --- the single write path ---------------------------------------------

    def write(
        self,
        patch: Mapping[str, Any],
        *,
        surface: ConfigWriteSurface,
        actor: str | None = None,
        warn: WarningRecorder | None = None,
    ) -> dict[str, Any]:
        """Merge *patch* into the document, validate, and persist it.

        Nested objects merge key by key so a surface editing one project does not
        have to resend every other. A ``None`` value removes its key, which is
        how a setting is returned to its bundled default — distinct from writing
        the default's current value, which would pin it.

        *actor* is the identity the surface says authorized the write: the
        approver a setup apply required, the signed-in operator behind a panel.
        It is recorded, not trusted — the surface name beside it in the record is
        the part no caller chooses — and it is optional because a surface that
        genuinely has no identity to give must record that fact rather than
        invent one.

        *warn* receives each advisory the persisted document earns. Advisories
        are raised here because this is the moment a human is present and looking
        at the setting: a document that arms unattended integration with nothing
        verifying it is valid, saved, and worth saying out loud before the run
        that acts on it starts hours later with nobody watching.

        Returns the persisted document. Raises ``ConfigWriteRefused`` when the
        surface may not write a path in the patch, and ``ConfigValidationError``
        when the merged document would be invalid; on either, nothing is written.
        """
        if not isinstance(patch, Mapping):
            raise ConfigValidationError([ConfigError("", "patch must be an object")])
        restricted = config_only_paths(patch)
        if restricted and not surface.operator_confirmed:
            raise ConfigWriteRefused(surface, restricted)

        self._root.mkdir(parents=True, exist_ok=True)
        platform_compat.chmod_safe(self._root, _DIR_MODE)
        lock_path = self._root / _LOCK_FILENAME
        fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, _FILE_MODE)
        try:
            with platform_compat.file_lock(fd, exclusive=True):
                merged = _merge(self.document(), patch)
                merged.setdefault(VERSION_KEY, CURRENT_VERSION)
                errors = validate_config_document(merged)
                if errors:
                    raise ConfigValidationError(errors)
                serialized = json.dumps(merged, indent=2, sort_keys=True) + "\n"
                atomic_write(self.path, serialized, mode=_FILE_MODE)
                # Recorded under the same lock and after the write, so the record
                # cannot claim a change the document does not carry, and two
                # surfaces saving at once cannot interleave a half-written line.
                self._record(
                    surface=surface, actor=actor, patch=patch, restricted=restricted, saved=merged
                )
        finally:
            os.close(fd)
        logger.info(
            "spec engine configuration updated by %s as %s (%d top-level keys)",
            surface.name,
            actor or "an unnamed actor",
            len(patch),
        )
        for warning in record_config_warnings(document_warnings(merged), warn):
            logger.warning(
                "spec engine configuration advisory %s at %s", warning.code, warning.path
            )
        return merged

    # --- the durable record of who wrote -----------------------------------

    @property
    def write_log_path(self) -> Path:
        """Path of the append-only write record, whether or not it exists yet."""
        return self._root / WRITE_LOG_FILENAME

    def writes(self) -> tuple[dict[str, Any], ...]:
        """Every recorded write, oldest first; empty when nothing was written.

        Unparseable lines are skipped rather than raising: the record's purpose is
        to say what it can about what happened, and one truncated line (a host
        that lost power mid-append) must not make the rest unreadable.
        """
        path = self.write_log_path
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ()
        except OSError as exc:
            raise ConfigLoadError(f"cannot read {path}: {exc}") from exc
        records: list[dict[str, Any]] = []
        for line in raw.splitlines():
            if not line.strip():
                continue
            try:
                parsed = json.loads(line)
            except ValueError:
                continue
            if isinstance(parsed, dict):
                records.append(parsed)
        return tuple(records)

    def _record(
        self,
        *,
        surface: ConfigWriteSurface,
        actor: str | None,
        patch: Mapping[str, Any],
        restricted: tuple[str, ...],
        saved: Mapping[str, Any],
    ) -> None:
        """Append one line naming who wrote and what they touched.

        Deliberately no values: a patch can carry a token in a capability's
        environment, and a record that copied it would turn the audit trail into a
        second place credentials live — one that no read path elides. What is kept
        is the shape of the change (top-level keys, and any config-only path the
        confirmed surface exercised) plus who claimed it.
        """
        identity = (actor or "").strip()
        record = {
            "ts": utc_now_iso(),
            "surface": surface.name,
            "operator_confirmed": surface.operator_confirmed,
            "actor": identity or None,
            "keys": sorted(str(key) for key in patch),
            "config_only_paths": list(restricted),
            "version": saved.get(VERSION_KEY),
        }
        line = json.dumps(record, sort_keys=True) + "\n"
        path = self.write_log_path
        try:
            descriptor = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, _FILE_MODE)
            try:
                os.write(descriptor, line.encode("utf-8"))
            finally:
                os.close(descriptor)
            platform_compat.chmod_safe(path, _FILE_MODE)
        except OSError as exc:
            raise ConfigRecordError(
                f"the configuration document at {self.path} was persisted but the write could "
                f"not be recorded in {path}: {exc}"
            ) from exc


def _merge(base: dict[str, Any], patch: Mapping[str, Any]) -> dict[str, Any]:
    """Recursively merge *patch* into a copy of *base*; ``None`` deletes a key."""
    result = dict(base)
    for key, value in patch.items():
        if value is None:
            result.pop(key, None)
        elif isinstance(value, Mapping):
            current = result.get(key)
            base_child = dict(current) if isinstance(current, Mapping) else {}
            result[key] = _merge(base_child, value)
        else:
            result[key] = value
    return result


def _reject_spec_directory(root: Path) -> None:
    """Refuse a state root inside a spec directory.

    Spec directories are the interoperability contract with the Kiro IDE and
    CLI. Engine state written there would travel with the spec, be diffed as
    part of it, and be edited by tools that know nothing about it.
    """
    parts = tuple(root.parts)
    for index in range(len(parts) - 1):
        if parts[index : index + 2] == _SPEC_DIR_SEGMENTS:
            raise ValueError(f"engine state must not live inside a spec directory: {root}")
