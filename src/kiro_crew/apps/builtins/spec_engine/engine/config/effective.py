"""Effective-value resolution: the value in force, and where it came from.

Origin is part of the contract, not a display nicety. A configuration surface
that shows ``2`` cannot tell an operator whether someone chose 2 or whether the
app shipped 2, and those call for opposite actions — the first is a decision to
revisit, the second is a default nobody has looked at yet. Resolution therefore
returns both, and every surface renders what it was given rather than inferring
"looks like the default, must be the default" (which is wrong exactly when
someone has explicitly pinned a value that happens to equal the default).

Precedence is narrowest-first: a per-source value beats a per-project value,
which beats a value pinned by the profile that project selected, which beats the
app-wide value, which beats the bundled default. A layer that holds no value for
the setting is skipped rather than treated as an explicit override, which is what
makes an absent optional setting resolve rather than fail.

The profile layer sits where it does because selecting a profile is a per-project
act: it is a narrower declaration than the app-wide value it overrides, and a
wider one than a value pinned on the project itself. So a project that picks the
budget profile runs under that profile's ceiling, and a project that also pins its
own ceiling keeps the one it pinned.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping

from .profiles import profile_pin
from .schema import ConfigError, ConfigValidationError, stored_value
from .settings import SCOPE_PRECEDENCE, SETTINGS, Scope, Setting


class ValueOrigin(str, Enum):
    """Where an effective value came from."""

    BUNDLED_DEFAULT = "bundled_default"
    APP_CONFIG = "app_config"
    COST_PROFILE = "cost_profile"
    PROJECT_CONFIG = "project_config"
    SOURCE_CONFIG = "source_config"


_ORIGIN_FOR_SCOPE: dict[Scope, ValueOrigin] = {
    Scope.APP: ValueOrigin.APP_CONFIG,
    Scope.PROJECT: ValueOrigin.PROJECT_CONFIG,
    Scope.SOURCE: ValueOrigin.SOURCE_CONFIG,
}


@dataclass(frozen=True)
class EffectiveValue:
    """A setting's value in force, its origin, and the path it was read from."""

    key: str
    value: Any
    origin: ValueOrigin
    #: Dotted path of the explicit declaration, empty for a bundled default.
    declared_at: str = ""

    @property
    def is_default(self) -> bool:
        """Whether this value is the bundled default rather than a configured one."""
        return self.origin is ValueOrigin.BUNDLED_DEFAULT

    def to_json_object(self) -> dict[str, Any]:
        """Shape this value for a surface that has to show it and say where it came from.

        Carries the registry's own description of the setting alongside the
        resolved value: a surface rendering "4 (bundled default)" beside a
        writable field needs the default, the bounds, and the scopes a write
        would be accepted at, and every one of those is registry data. Joined
        here rather than looked up again by each caller, so no surface grows a
        second opinion about what this setting is -- and deliberately NOT a
        second resolver: ``value``, ``origin`` and ``declared_at`` are passed
        through from whatever :func:`resolve` decided, never recomputed.
        """
        setting = SETTINGS.get(self.key)
        payload: dict[str, Any] = {
            "key": self.key,
            "value": self.value,
            "origin": self.origin.value,
            "declared_at": self.declared_at,
            "is_default": self.is_default,
        }
        if setting is None:  # pragma: no cover - a value only exists for a registered key
            return payload
        payload.update(
            {
                "default": setting.default,
                "summary": setting.summary,
                "kind": setting.kind.__name__,
                "scopes": sorted(scope.value for scope in setting.scopes),
                "minimum": setting.minimum,
                "maximum": setting.maximum,
                "choices": list(setting.choices),
            }
        )
        return payload


def resolve(
    doc: Mapping[str, Any],
    setting: Setting,
    *,
    project: str | None = None,
    source: str | None = None,
) -> EffectiveValue:
    """Resolve *setting* against *doc* for an optional project and source.

    An explicit value that fails the setting's own validation raises rather than
    falling through to the next layer: a hand-edited out-of-range ceiling is an
    operator error worth naming, and silently substituting the default would run
    the very work the operator meant to bound.
    """
    for scope in SCOPE_PRECEDENCE:
        # The selected profile is consulted immediately above the app layer, so a
        # project's own declaration still wins over the profile it picked.
        if scope is Scope.APP:
            pinned = _profile_value(doc, setting, project)
            if pinned is not None:
                return pinned
        if not setting.allows(scope):
            continue
        if scope is Scope.PROJECT and project is None:
            continue
        if scope is Scope.SOURCE and source is None:
            continue
        present, raw, path = stored_value(doc, setting, scope, project=project, source=source)
        if not present:
            continue
        try:
            value = setting.coerce(raw)
        except ValueError as exc:
            raise ConfigValidationError([ConfigError(path, str(exc))]) from exc
        return EffectiveValue(setting.key, value, _ORIGIN_FOR_SCOPE[scope], path)
    return EffectiveValue(setting.key, setting.default, ValueOrigin.BUNDLED_DEFAULT)


def _profile_value(
    doc: Mapping[str, Any], setting: Setting, project: str | None
) -> EffectiveValue | None:
    """Resolve *setting* from the profile *project* selected, if it pins it."""
    present, raw, path = profile_pin(doc, setting.key, project)
    if not present:
        return None
    try:
        value = setting.coerce(raw)
    except ValueError as exc:
        raise ConfigValidationError([ConfigError(path, str(exc))]) from exc
    return EffectiveValue(setting.key, value, ValueOrigin.COST_PROFILE, path)


def resolve_all(
    doc: Mapping[str, Any],
    *,
    project: str | None = None,
    source: str | None = None,
) -> dict[str, EffectiveValue]:
    """Resolve every registered setting, keyed by dotted key."""
    return {
        key: resolve(doc, setting, project=project, source=source)
        for key, setting in SETTINGS.items()
    }
