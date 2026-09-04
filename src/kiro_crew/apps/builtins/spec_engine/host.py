"""The single seam between this app and the gateway it currently ships inside.

**This is the ONLY file in the Spec Engine that may import ``kiro_crew.*``
gateway internals.** Every other module in the app -- backend, engine,
engine_mcp, startup, readiness, diagnostics -- reaches a gateway-internal symbol
by importing it *from here* (``from ..host import atomic_write``), never by
importing the gateway module directly.

Why the choke point exists: the Spec Engine is a builtin today, but it is meant
to be portable to an external-app SDK repository. Everything gateway-specific it
depends on -- the config-home layout, the app manager, the notification bus, the
security/SEL surfaces, the platform-compat and sandbox shims, sqlite -- is
gathered here and nowhere else. **Porting the app to the external-app SDK is
exactly the job of rewriting this one file:** each re-export below becomes a call
into the SDK's equivalent surface (or a thin local shim), and not a single other
module changes. The value is the boundary, not indirection -- most entries are
plain re-exports, and that is fine; the point is that the set of gateway
dependencies is enumerable in one place and rewired in one place.

The import-boundary is enforced by ``tests/test_host_boundary.py``, which fails
if any shipped module under the app package (this file and the tests excepted)
imports ``kiro_crew.*`` outside the app's own package.

**Eager vs lazy is deliberate and must be preserved on a port.** Symbols whose
original call sites imported them at module scope are bound eagerly below.
Symbols whose original call sites imported them *inside a function* -- to keep a
subsystem (the dashboard handlers, the notification bus, the cron control-flow
classes, the app-bridges registry, the MCP validator) off the engine library's
import-time path or to break an import cycle -- are resolved lazily through
:func:`__getattr__` (PEP 562). ``from ..host import <lazy symbol>`` written
inside a function therefore imports its backing module only when that function
runs, exactly as the original lazy site did. Binding them eagerly here would pull
those subsystems onto the load path of every engine module that imports this
seam, which changes behavior.

Modules re-exported whole (``platform_compat``, ``sandbox``) are accessed by
attribute at their call sites (``platform_compat.IS_POSIX``,
``sandbox.sandboxed_spawn_argv``); re-exporting the module object keeps those
call sites unchanged and keeps every attribute they use behind this one seam.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

# --- Platform / process shims (accessed by attribute at call sites) -----------
from kiro_crew import platform_compat, sandbox

# --- sqlite compatibility shim ------------------------------------------------
from kiro_crew._sqlite_compat import sqlite3

# --- App Kit platform surfaces ------------------------------------------------
from kiro_crew.apps.approval_grants import (
    posture_extra_env,
    session_posture,
    verify_session_posture,
)
from kiro_crew.apps.manager import app_data_dir, is_app_enabled

# --- Atomic writes ------------------------------------------------------------
from kiro_crew.atomic_write import atomic_write

# --- Config home / paths ------------------------------------------------------
from kiro_crew.config.loader import config_dir
from kiro_crew.config.paths import data_home, kiro_agents_dir, project_agents_dir

# --- Effort levels / model capability -----------------------------------------
from kiro_crew.effort import EFFORT_LEVELS, model_supports_effort

# ``chmod_safe`` is also imported by name at one site; the module above carries
# every other platform_compat attribute the app touches.
from kiro_crew.platform_compat import chmod_safe

# --- Security keystone (redaction) --------------------------------------------
from kiro_crew.security import redact

# --- Security event log -------------------------------------------------------
from kiro_crew.sel import sel

# =============================================================================
# Lazy re-exports -- these were imported INSIDE a function at their call sites,
# to keep a subsystem off the engine library's import-time path or to break an
# import cycle. :func:`__getattr__` defers each to first access so the footprint
# is unchanged. See the module docstring.
# =============================================================================

if TYPE_CHECKING:  # pragma: no cover - typing only; never on the runtime load path
    from kiro_crew.apps.bridges import app_skills_dir as app_skills_dir
    from kiro_crew.apps.bridges import registered_app_mcp_servers as registered_app_mcp_servers
    from kiro_crew.cron_script import Report as Report
    from kiro_crew.cron_script import Skip as Skip
    from kiro_crew.dashboard.handlers.usage import spend_key_for_slot as spend_key_for_slot
    from kiro_crew.notifications.bus import NotificationPayload as NotificationPayload
    from kiro_crew.platform.context import redact_via_context as redact_via_context
    from kiro_crew.validation import validate_mcp_tool_arguments as validate_mcp_tool_arguments


def _load_lazy(name: str) -> Any:
    """Import and return one lazily-exported symbol by name, or raise KeyError."""
    if name == "spend_key_for_slot":
        from kiro_crew.dashboard.handlers.usage import spend_key_for_slot

        return spend_key_for_slot
    if name == "redact_via_context":
        from kiro_crew.platform.context import redact_via_context

        return redact_via_context
    if name == "NotificationPayload":
        from kiro_crew.notifications.bus import NotificationPayload

        return NotificationPayload
    if name in ("Report", "Skip"):
        import kiro_crew.cron_script as _cron_script

        return getattr(_cron_script, name)
    if name == "validate_mcp_tool_arguments":
        from kiro_crew.validation import validate_mcp_tool_arguments

        return validate_mcp_tool_arguments
    if name in ("app_skills_dir", "registered_app_mcp_servers"):
        import kiro_crew.apps.bridges as _bridges

        return getattr(_bridges, name)
    raise KeyError(name)


def __getattr__(name: str) -> Any:
    """PEP 562 hook: resolve a lazily re-exported symbol at first access.

    ``from ..host import <lazy symbol>`` inside a function triggers this only
    when that function runs, so the backing module never lands on an importing
    module's load path -- the exact laziness the original call site relied on.
    """
    try:
        return _load_lazy(name)
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None


__all__ = [
    # --- eager ---
    # platform / process
    "platform_compat",
    "sandbox",
    "chmod_safe",
    # sqlite
    "sqlite3",
    # approval grants
    "posture_extra_env",
    "session_posture",
    "verify_session_posture",
    # app manager
    "app_data_dir",
    "is_app_enabled",
    # atomic writes
    "atomic_write",
    # config paths
    "config_dir",
    "data_home",
    "kiro_agents_dir",
    "project_agents_dir",
    # effort
    "EFFORT_LEVELS",
    "model_supports_effort",
    # security keystone
    "redact",
    # security event log
    "sel",
    # --- lazy (resolved via __getattr__) ---
    # bridges
    "app_skills_dir",
    "registered_app_mcp_servers",
    # cron control flow
    "Report",
    "Skip",
    # usage
    "spend_key_for_slot",
    # notifications
    "NotificationPayload",
    # redaction
    "redact_via_context",
    # validation
    "validate_mcp_tool_arguments",
]
