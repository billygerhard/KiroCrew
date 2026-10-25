"""Operator-defined ACP backends: the additive gateway step that registers them.

Loading ``harnesses.json`` is adapter work (a file read, descriptor validation,
registry writes), so it does not live in ``bootstrap_context``: the Kiro
construction path gains nothing (harness-parity H13). The gateway calls
:func:`register_operator_backends` once, right after ``boot_platform``. Every
other process (a CLI command, a cron runner, an MCP server) registers on first
need instead: :func:`ensure_operator_backends_registered` runs when a config load
meets an ``agent.acp_backend`` no builtin answers, so a seat default resolves the
same way outside the gateway. ``bootstrap_context`` narrows by the
``agent_backend`` scope after that load, so a lazily registered seat is still
governed.

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
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.config.loader import KiroCrewConfig

logger = logging.getLogger(__name__)

#: Data homes whose ``harnesses.json`` this process has already read.
_LOADED_HOMES: set[str] = set()
_LOAD_LOCK = threading.Lock()


def ensure_operator_backends_registered() -> list[str]:
    """Register this data home's ``harnesses.json`` once per process; returns the new ids.

    Cheap after the first call (a set lookup). Safe inside ``KiroCrewConfig.load()``:
    it reads only the file and the registry, never the platform context. A call that
    finds the load already running on another frame or thread returns ``[]`` rather
    than waiting on it, so a re-entrant config load cannot deadlock here.
    """
    from kiro_crew.config.paths import config_dir

    try:
        home = str(config_dir())
    except Exception:  # noqa: BLE001 - no resolvable home means nothing to load
        return []
    if home in _LOADED_HOMES or not _LOAD_LOCK.acquire(blocking=False):
        return []
    try:
        if home in _LOADED_HOMES:
            return []
        _LOADED_HOMES.add(home)
        from kiro_crew.acp.harness.operator_registry import (
            load_and_register_operator_descriptors,
        )

        return load_and_register_operator_descriptors()
    except Exception:
        logger.warning("operator backend registration failed; continuing", exc_info=True)
        return []
    finally:
        _LOAD_LOCK.release()


def _reset_operator_backends_loaded() -> None:
    """TEST-ONLY: forget which homes were read, so the next need re-reads."""
    _LOADED_HOMES.clear()


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
    ensure_operator_backends_registered()
    # Every operator id now registered, including one a config load already
    # registered on first need: the narrowing and re-resolve below still apply.
    try:
        from kiro_crew.agent_sdk.operator_backend_registry import registered_backend_ids

        registered = sorted(registered_backend_ids())
    except Exception:
        logger.warning("reading operator backends failed; continuing", exc_info=True)
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
