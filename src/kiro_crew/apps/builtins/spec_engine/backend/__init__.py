"""The Spec_App's inbound HTTP surface: the Operator_Surface's backing routes.

The only tree in this app that may import a web framework, and it imports one to
DECLARE handlers rather than to construct a client. Everything under
``engine/`` and ``engine_mcp/`` keeps the full network denylist, so a
transmission path cannot appear here by accident: this package holds routes and
no engine rules, and the engine holds rules and no sockets.

Deliberately empty of re-exports. The gateway reads ``register_routes`` off the
APP package (``spec_engine/__init__.py``), not off this one, so a second
re-export here would be a spelling nobody dispatches.
"""

from __future__ import annotations
