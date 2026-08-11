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

The document lives in the app's data directory, never inside a spec directory:
the Kiro IDE and CLI read the same ``.kiro/specs/<name>/`` trees, so anything
the engine adds there would be a foreign file in someone else's contract.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from kiro_crew import platform_compat
from kiro_crew.apps.manager import app_data_dir
from kiro_crew.atomic_write import atomic_write

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

#: Owner-only: the document names the projects a user works on, their branch
#: layout, and the commands the app may run on their behalf.
_DIR_MODE = 0o700
_FILE_MODE = 0o600

#: Path segments that mark a spec directory. Engine state must never resolve
#: inside one.
_SPEC_DIR_SEGMENTS = (".kiro", "specs")


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
        warn: WarningRecorder | None = None,
    ) -> dict[str, Any]:
        """Merge *patch* into the document, validate, and persist it.

        Nested objects merge key by key so a surface editing one project does not
        have to resend every other. A ``None`` value removes its key, which is
        how a setting is returned to its bundled default — distinct from writing
        the default's current value, which would pin it.

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
                atomic_write(
                    self.path,
                    json.dumps(merged, indent=2, sort_keys=True) + "\n",
                    mode=_FILE_MODE,
                )
        finally:
            os.close(fd)
        logger.info(
            "spec engine configuration updated by %s (%d top-level keys)",
            surface.name,
            len(patch),
        )
        for warning in record_config_warnings(document_warnings(merged), warn):
            logger.warning(
                "spec engine configuration advisory %s at %s", warning.code, warning.path
            )
        return merged


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
