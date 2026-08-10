"""Effective-value resolution: the value in force, and where it came from.

Origin is part of the contract, not a display nicety. A configuration surface
that shows ``2`` cannot tell an operator whether someone chose 2 or whether the
app shipped 2, and those call for opposite actions — the first is a decision to
revisit, the second is a default nobody has looked at yet. Resolution therefore
returns both, and every surface renders what it was given rather than inferring
"looks like the default, must be the default" (which is wrong exactly when
someone has explicitly pinned a value that happens to equal the default).

Precedence is narrowest-first: a per-source value beats a per-project value,
which beats the app-wide value, which beats the bundled default. A layer that
holds no value for the setting is skipped rather than treated as an explicit
override, which is what makes an absent optional setting resolve rather than
fail.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping

from .schema import ConfigError, ConfigValidationError, stored_value
from .settings import SCOPE_PRECEDENCE, SETTINGS, Scope, Setting


class ValueOrigin(str, Enum):
    """Where an effective value came from."""

    BUNDLED_DEFAULT = "bundled_default"
    APP_CONFIG = "app_config"
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
