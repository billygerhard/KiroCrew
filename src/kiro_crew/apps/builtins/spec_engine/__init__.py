"""Spec engine app package.

The rules-as-code engine lives in :mod:`.engine`; the drivers (MCP wrapper, UI
backend, watcher cron) are thin layers over it and own no spec rules.

``register_routes`` is re-exported because the gateway's builtin route loop reads
that attribute off THIS package — not off ``backend.routes`` — and calls it with
the gateway's own aiohttp application. A route module the package does not
re-export registers nothing and every handler in it 404s with no error anywhere.
Keep the re-export a plain import: it runs on every gateway boot, including boots
where this opt-in app is disabled.
"""

from .backend.routes import register_routes

__all__ = ["register_routes"]
