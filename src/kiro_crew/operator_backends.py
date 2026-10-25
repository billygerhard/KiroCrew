"""Operator-defined ACP backends: the additive gateway step that registers them.

Loading ``harnesses.json`` is adapter work (a file read, descriptor validation,
registry writes), so it does not live in ``bootstrap_context``: the Kiro
construction path gains nothing (harness-parity H13). The gateway calls
:func:`register_operator_backends` once, right after ``boot_platform``; a CLI
command or app server that boots the platform never does.

The step runs synchronously before any service starts. It reads one small file,
and running it before the dashboard binds means no chat can dispatch while the
configured default still reads the boot-time coercion.

Ordering:

* Descriptors register AFTER ``bootstrap_context`` narrowed the selectable set
  by the ``agent_backend`` scope, so the narrowing is re-applied here.
* The config instance was loaded before registration, so an ``agent.acp_backend``
  naming an operator id was coerced to the default. The persisted value is
  re-read from ``config.json`` and re-resolved through the one selection gate.

Best-effort: a failure leaves the builtin harnesses serving and is logged.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.config.loader import KiroCrewConfig

logger = logging.getLogger(__name__)


def _persisted_acp_backend() -> str | None:
    """``agent.acp_backend`` as written in ``config.json``, or ``None``."""
    from kiro_crew.config.paths import config_dir

    try:
        with open(config_dir() / "config.json", "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    agent = data.get("agent") if isinstance(data, dict) else None
    value = agent.get("acp_backend") if isinstance(agent, dict) else None
    return value if isinstance(value, str) else None


def register_operator_backends(cfg: "KiroCrewConfig") -> None:
    """Load ``harnesses.json``, register its descriptors, and settle *cfg*'s default."""
    try:
        from kiro_crew.acp.harness.operator_registry import (
            load_and_register_operator_descriptors,
        )

        registered = load_and_register_operator_descriptors()
    except Exception:
        logger.warning("operator backend registration failed; continuing", exc_info=True)
        return
    if not registered:
        return
    try:
        from kiro_crew.agent_backend_governance import narrow_selectable_backends

        narrow_selectable_backends()
    except Exception:
        logger.warning("agent_backend governance re-apply failed; continuing", exc_info=True)
    try:
        from kiro_crew.acp_backends import resolve_selected_backend

        persisted = _persisted_acp_backend()
        if persisted in registered:
            resolved = resolve_selected_backend(persisted)
            if resolved != cfg.agent.acp_backend:
                cfg.agent.acp_backend = resolved
    except Exception:
        logger.warning("re-resolving agent.acp_backend failed; continuing", exc_info=True)
