"""Whether this app belongs in ``apps/builtins/__init__.py``'s ``BUILTIN_NAMES``.

**The decision: it stays out, and this module is the record of that being a
choice rather than an omission.**

The list is not app registration. Three things a reader might expect it to do are
done elsewhere, all from the app manifest: the ``on_startup`` hook is dispatched
by ``apps/lifecycle.py`` for whatever ``apps/discovery.py``'s directory walk
found, the MCP server is launched by the manifest's own
``python3 -m ...engine_mcp.server`` command, and the App Store entry comes from
``register_builtin_apps()``, which registers the discovered manifests. None of
those reads ``BUILTIN_NAMES``.

What the list actually dispatches is three things this app does not have:

* the dashboard's builtin ``register_routes`` loop — and this app is forbidden
  from shipping aiohttp routes by its own build posture, so the loop would find
  nothing to call;
* the ``on_disable`` module hook in ``apps/routes.py`` — this app defines none,
  and the manifest's own ``backend.hooks.on_shutdown`` is the supported way to
  get one, which ``lifecycle.py`` calls when an app is disabled;
* a ``kirocrew mcp-<name>`` CLI alias, which imports
  ``kiro_crew.apps.builtins.<name>.mcp_server``. This app's server is at
  ``engine_mcp/server.py`` and is started from the manifest, so the alias would
  be the one *new* behaviour an entry produced — a suppressed subcommand that
  raises ``ModuleNotFoundError`` if anyone ran it.

So the entry buys nothing today and adds one broken alias, while implying two
capabilities the app deliberately does not have. It stays out. The costs accepted
are exactly that alias and the legacy ``on_disable`` dispatch, and the reversal is
one line the day this app grows a ``register_routes``, an ``on_disable``, or an
``mcp_server`` shim — which is what the assertions below are watching for.

**The residual, settled without a live gateway.** ``register_builtin_apps()``
documents ``defaultEnabled`` (default ``True``) as what sets a newly registered
app's initial enabled state, and it registers the entries
``discover_builtin_apps()`` builds from each ``app.json``. This app's manifest
declares ``false``, so on a real host it appears in the App Store and stays
disabled until an operator enables it. That is a manifest field rather than a
gateway behaviour, so reading it here is the whole answer — and it is independent
of ``BUILTIN_NAMES``, which neither the walk nor the enabled state consults.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
from pathlib import Path

from kiro_crew.apps.builtins import BUILTIN_NAMES

#: The Python package name the list would hold, which is not the manifest name
#: (``spec-engine``): the list is keyed by importable module.
MODULE_NAME = "spec_engine"
APP_ROOT = Path(__file__).resolve().parent.parent


def manifest_data() -> dict:
    return json.loads((APP_ROOT / "app.json").read_text(encoding="utf-8"))


class TestTheAppIsDeliberatelyNotABuiltinName:
    def test_the_module_is_absent_from_the_list(self) -> None:
        """The decision, pinned. Adding the entry has to be a deliberate act too.

        Whoever adds it will fail this test and read the module docstring, which is
        where the reasoning and the three conditions that would justify the entry
        are written down.
        """
        assert MODULE_NAME not in BUILTIN_NAMES
        # Not a typo-shaped absence: the list is populated and holds the sibling
        # app, so "absent" here means absent rather than "the import broke".
        assert "spec_builder" in BUILTIN_NAMES

    def test_the_app_ships_none_of_the_three_things_the_list_dispatches(self) -> None:
        """Each of these becoming true is a reason to revisit the decision."""
        package = importlib.import_module(f"kiro_crew.apps.builtins.{MODULE_NAME}")

        assert not hasattr(package, "register_routes")
        assert not hasattr(package, "on_disable")
        assert importlib.util.find_spec(f"kiro_crew.apps.builtins.{MODULE_NAME}.mcp_server") is None

    def test_the_mcp_server_the_app_does_ship_is_launched_from_the_manifest(self) -> None:
        # Which is why the missing CLI alias costs nothing: the command that starts
        # the server names the module the app actually has.
        command = manifest_data()["mcpServers"]["spec-engine"]
        assert command["args"][-1].endswith("engine_mcp.server")

    def test_the_app_is_not_enabled_by_default_on_a_real_host(self) -> None:
        """The residual answered from the field the registration path reads.

        ``register_builtin_apps()`` takes the initial enabled state from
        ``defaultEnabled``, defaulting to ``True`` when the key is absent — so the
        key has to be present and false, not merely not-true.
        """
        data = manifest_data()

        assert "defaultEnabled" in data
        assert data["defaultEnabled"] is False
