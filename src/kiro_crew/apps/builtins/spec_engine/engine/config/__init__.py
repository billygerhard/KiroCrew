"""Spec engine configuration: schema, bundled defaults, and effective values.

Import from this package rather than its modules; the split between the setting
registry, the schema, the resolver, and the store is an implementation detail.

    from ...engine.config import ConfigStore, DASHBOARD_SURFACE

    store = ConfigStore()
    ceiling = store.effective("budget.run_ceiling_credits", project="acme")
    ceiling.value    # 5.0
    ceiling.origin   # ValueOrigin.BUNDLED_DEFAULT
    store.write({"limits": {"task_retry_limit": 4}}, surface=DASHBOARD_SURFACE)
"""

from __future__ import annotations

from .effective import EffectiveValue, ValueOrigin, resolve, resolve_all
from .schema import (
    AUTONOMY_LEVELS,
    CONFIG_ONLY_PATHS,
    CURRENT_VERSION,
    DELEGABLE_CAPABILITIES,
    DELIVERY_STAGES,
    ENGINE_FLOOR_CAPABILITIES,
    GATE_POSITIONS,
    GATE_SEVERITIES,
    ITEM_LIFECYCLE_EVENTS,
    LEAST_TRUSTED_CLASS,
    PROJECT_FIELDS,
    ROLES,
    SECTIONS,
    SOURCE_FIELDS,
    SPEC_TYPES,
    SUBMITTER_CLASSES,
    TRANSPORTS,
    VERSION_KEY,
    WILDCARD_KEY,
    ConfigError,
    ConfigValidationError,
    config_only_paths,
    validate_config_document,
)
from .settings import SETTINGS, Scope, Setting, default_of, lookup, settings_in_scope
from .store import (
    APP_NAME,
    CONFIG_FILENAME,
    DASHBOARD_SURFACE,
    SETUP_ASSISTANT_SURFACE,
    ConfigLoadError,
    ConfigStore,
    ConfigWriteRefused,
    ConfigWriteSurface,
    default_root,
)

__all__ = [
    "APP_NAME",
    "AUTONOMY_LEVELS",
    "CONFIG_FILENAME",
    "CONFIG_ONLY_PATHS",
    "CURRENT_VERSION",
    "DASHBOARD_SURFACE",
    "DELEGABLE_CAPABILITIES",
    "DELIVERY_STAGES",
    "ENGINE_FLOOR_CAPABILITIES",
    "GATE_POSITIONS",
    "GATE_SEVERITIES",
    "ITEM_LIFECYCLE_EVENTS",
    "LEAST_TRUSTED_CLASS",
    "PROJECT_FIELDS",
    "ROLES",
    "SECTIONS",
    "SETTINGS",
    "SETUP_ASSISTANT_SURFACE",
    "SOURCE_FIELDS",
    "SPEC_TYPES",
    "SUBMITTER_CLASSES",
    "TRANSPORTS",
    "VERSION_KEY",
    "WILDCARD_KEY",
    "ConfigError",
    "ConfigLoadError",
    "ConfigStore",
    "ConfigValidationError",
    "ConfigWriteRefused",
    "ConfigWriteSurface",
    "EffectiveValue",
    "Scope",
    "Setting",
    "ValueOrigin",
    "config_only_paths",
    "default_of",
    "default_root",
    "lookup",
    "resolve",
    "resolve_all",
    "settings_in_scope",
    "validate_config_document",
]
