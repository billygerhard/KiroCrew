"""Whether this app belongs in ``apps/builtins/__init__.py``'s ``BUILTIN_NAMES``.

**The decision: it goes in — and this module is the record of the reversal being
as deliberate as the refusal it replaces.** The entry was once deliberately
withheld, pinned here, on the reasoning that the list dispatches three things
this app did not have. One of the three is now the app's own Operator_Surface, so
the reasoning is rewritten rather than deleted, and the assertions still fail for
a change to the entry made without reading them.

The list is still not app registration. Four things a reader might expect it to
do are done elsewhere, all from the app manifest: the ``on_startup`` hook is
dispatched by ``apps/lifecycle.py`` for whatever ``apps/discovery.py``'s
directory walk found, the MCP server is launched by the manifest's own
``python3 -m ...engine_mcp.server`` command, the App Store entry comes from
``register_builtin_apps()`` over the discovered manifests, and the initial
enabled state comes from that manifest's ``defaultEnabled``. None of those reads
``BUILTIN_NAMES``.

**What the entry buys: route registration, and nothing else buys it.** The
dashboard's ``start_dashboard`` walks this list, imports each app package, and
calls the package's ``register_routes(app)`` on the gateway's own aiohttp
``Application`` — full ``/api/apps/<name>/*`` paths, same origin, behind the
dashboard's auth. That loop is the ONLY caller of that contract. The manifest's
``backend.routes`` field names the same entry point, but for a builtin nothing
dispatches it: the generic App Kit loader in ``apps/hooks_integration.py`` reads
``backend.hooks.routes`` — a different key, which no builtin sets — and hands it
to ``RouteRegistry.register_app_routes``, whose contract is a different function
altogether (``register_fn(ctx) -> list[AppRoute]``, soft-routed behind a
catch-all) that a ``register_routes(app)`` does not satisfy. So without this
entry the Operator_Surface's routes would exist in the tree and 404 on the wire.

**Why the entry is safe before those routes exist.** The loop guards the call
with ``hasattr(_mod, "register_routes")``, so while the app package exposes none
the entry is a no-op and the gateway boots exactly as before. That is asserted
against the loop as it is written, not assumed. The manifest may already declare
``backend.routes`` while the module it names is still to come: for a builtin that
field is documentation, dispatched by nothing, so the declaration alone changes no
runtime behaviour. What the loop reads is the attribute on the app PACKAGE, which
is why the assertion below keys on the route module existing rather than on the
manifest field.

**The hazard the loop hands to whoever adds the routes.** Its only tolerance is
``except ModuleNotFoundError`` re-raised unless ``exc.name`` is precisely the app
package — which covers an absent app, not a broken import inside a present one. A
``ModuleNotFoundError`` raised while importing ``backend/routes.py`` (a missing
third-party dependency, a mistyped submodule) therefore propagates out of
``start_dashboard`` and takes gateway startup down for every app. Route code
reached through this list has to import cleanly on every supported platform.

**What the entry still does not buy, so that no reader infers it:**

* the legacy ``on_disable`` module hook in ``apps/routes.py``. Two independent
  reasons it cannot fire here: this app defines no such module attribute (the
  manifest's ``backend.hooks.on_shutdown`` is the supported way, which
  ``lifecycle.py`` calls when an app is disabled), and that branch tests the URL
  app NAME from ``request.match_info`` — ``spec-engine`` — against a list of
  importable MODULE names — ``spec_engine``. For every builtin whose name is more
  than one word the two spellings never meet, so the entry cannot switch that
  dispatch on even for an app that grew the hook.
* a working ``kirocrew mcp-<name>`` CLI alias. The entry DOES add the suppressed
  ``mcp-spec_engine`` subcommand, and it still resolves
  ``kiro_crew.apps.builtins.spec_engine.mcp_server``, which this app does not
  have — its server is ``engine_mcp/server.py``. So the alias raises
  ``ModuleNotFoundError`` for anyone who runs it. That cost is now ACCEPTED
  rather than avoided: it is the same cost the refusal weighed, only the other
  side of the scale grew. Nothing in the tree composes that command — ``cli.py``
  is its only builder, from this list — and the manifest starts the real server
  by module path, so no shipped code path reaches it.

**The residual, settled without a live gateway.** ``register_builtin_apps()``
takes a newly registered app's initial enabled state from ``defaultEnabled``
(default ``True``) on the entries ``discover_builtin_apps()`` builds from each
``app.json``. This app's manifest declares ``false``, so on a real host it
appears in the App Store and stays disabled until an operator enables it. That
was independent of ``BUILTIN_NAMES`` before the entry and must still be after
it — which is asserted below against the registration function's own source
rather than argued.
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import inspect
import json
from pathlib import Path

import pytest

import kiro_crew
from kiro_crew.apps.builtins import BUILTIN_NAMES

#: The Python package name the list holds, which is not the manifest name
#: (``spec-engine``): the list is keyed by importable module.
MODULE_NAME = "spec_engine"
PACKAGE = f"kiro_crew.apps.builtins.{MODULE_NAME}"
APP_ROOT = Path(__file__).resolve().parent.parent

PKG_ROOT = Path(kiro_crew.__file__).resolve().parent
#: Read as text, never imported: importing the dashboard drags in the whole
#: gateway, and the claims here are about the code as written.
DASHBOARD_SERVER = PKG_ROOT / "dashboard" / "server.py"
HOOKS_INTEGRATION = PKG_ROOT / "apps" / "hooks_integration.py"
CLI_MODULE = PKG_ROOT / "cli.py"


def manifest_data() -> dict:
    return json.loads((APP_ROOT / "app.json").read_text(encoding="utf-8"))


def _dashboard_source() -> str:
    return DASHBOARD_SERVER.read_text(encoding="utf-8")


def _builtin_route_loop(source: str) -> ast.For:
    """The dashboard's builtin route loop, found by what it iterates.

    Located structurally rather than by line number so that the assertions below
    describe the loop that exists rather than the line it once sat on. A loop
    that cannot be found is a failure: the reasoning in this module's docstring
    rests on the loop's shape, so losing sight of it must not read as agreement.
    """
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.For)
            and isinstance(node.iter, ast.Name)
            and node.iter.id == "BUILTIN_NAMES"
        ):
            return node
    raise AssertionError(
        f"no `for ... in BUILTIN_NAMES:` loop in {DASHBOARD_SERVER}. This module's "
        "recorded reason for the entry is that loop; re-verify it before trusting "
        "either the entry or this test."
    )


def _hasattr_guards(loop: ast.For) -> list[ast.If]:
    """Every ``if hasattr(x, "register_routes"):`` inside *loop*."""
    guards: list[ast.If] = []
    for node in ast.walk(loop):
        if not isinstance(node, ast.If) or not isinstance(node.test, ast.Call):
            continue
        call = node.test
        if not (isinstance(call.func, ast.Name) and call.func.id == "hasattr"):
            continue
        if any(
            isinstance(arg, ast.Constant) and arg.value == "register_routes" for arg in call.args
        ):
            guards.append(node)
    return guards


def _register_routes_calls(node: ast.AST) -> list[ast.Call]:
    """Every ``<something>.register_routes(...)`` call under *node*."""
    return [
        child
        for child in ast.walk(node)
        if isinstance(child, ast.Call)
        and isinstance(child.func, ast.Attribute)
        and child.func.attr == "register_routes"
    ]


class TestTheAppIsDeliberatelyABuiltinName:
    def test_the_module_is_present_in_the_list(self) -> None:
        """The decision, pinned. Removing the entry has to be a deliberate act too.

        Whoever removes it will fail this test and read the module docstring,
        which is where the reasoning lives: the entry is what makes the
        Operator_Surface's routes reachable, and no other dispatcher calls a
        builtin's ``register_routes``.
        """
        assert MODULE_NAME in BUILTIN_NAMES, (
            f"{MODULE_NAME} was removed from BUILTIN_NAMES. That list is the only "
            "dispatcher of a builtin's register_routes(app), so removing the entry "
            "leaves this app's /api/apps/spec-engine/* routes in the tree and 404 "
            "on the wire. Read this module's docstring before changing it back."
        )
        # Not a coincidence-shaped presence: the list holds the sibling app too,
        # so "present" here means present rather than "some list got truthy".
        assert "spec_builder" in BUILTIN_NAMES

    def test_the_entry_appears_exactly_once(self) -> None:
        """A duplicate entry would run the whole registration twice for one app.

        What aiohttp does with the second copy of a route is its business; the
        claim here is only that the loop would call ``register_routes(app)`` twice,
        which no app's registration function is written to expect.
        """
        assert BUILTIN_NAMES.count(MODULE_NAME) == 1

    def test_the_list_holds_the_module_name_not_the_manifest_name(self) -> None:
        """The two spellings differ, and several claims here depend on that.

        The manifest name reaches the App Store, the URL space and the
        ``on_disable`` branch; the module name reaches the import machinery and
        this list. Anything matching one against the other silently never fires.
        """
        manifest_name = manifest_data()["name"]
        assert manifest_name == "spec-engine"
        assert manifest_name != MODULE_NAME
        assert manifest_name not in BUILTIN_NAMES


class TestWhatTheEntryBuys:
    """Route registration — verified against the loop, not assumed of it."""

    def test_the_dashboard_walks_the_list_and_calls_register_routes(self) -> None:
        source = _dashboard_source()
        loop = _builtin_route_loop(source)
        assert _register_routes_calls(loop), (
            "the builtin loop no longer calls register_routes; the entry's only "
            "benefit has moved and this module's reasoning must be re-derived"
        )
        # The import target is composed from the list entry, which is why the
        # list is keyed by module name rather than by manifest name.
        segment = ast.get_source_segment(source, loop) or ""
        assert "kiro_crew.apps.builtins." in segment

    def test_the_call_is_guarded_so_an_app_without_routes_is_a_no_op(self) -> None:
        """Why the entry is safe in the wave before the routes land.

        Written to hold in both states — before the app has ``register_routes``
        and after — because a test that pins today's absence would fail the
        commit that adds the routes, which is the opposite of a guard.
        """
        source = _dashboard_source()
        loop = _builtin_route_loop(source)
        guards = _hasattr_guards(loop)
        assert guards, "the loop no longer guards the call with hasattr"
        guarded = [call for guard in guards for call in _register_routes_calls(guard)]
        assert len(guarded) == len(_register_routes_calls(loop)), (
            "an unguarded register_routes call in the builtin loop: an app in the "
            "list that exposes none would raise AttributeError at gateway startup"
        )

    def test_the_app_package_imports_cleanly_and_agrees_with_the_guard(self) -> None:
        """The other half of "the gateway still boots": the import cannot raise.

        The loop imports every listed package unconditionally. Whatever the
        package exposes, it must import, and anything it calls ``register_routes``
        must be callable — the two states the guard distinguishes are both
        asserted so this stays true across the commit that adds the routes.
        """
        package = importlib.import_module(PACKAGE)
        if hasattr(package, "register_routes"):
            assert callable(package.register_routes)

    def test_no_other_dispatcher_would_register_a_builtins_routes(self) -> None:
        """The generic App Kit loader reads a different manifest key.

        Asserted two ways: the loader's own expression chain, and this app's real
        manifest evaluated through it. If the loader is refactored the first
        assertion speaks up, which is the point — the recorded reason for the
        entry is that this path finds nothing.
        """
        loader = HOOKS_INTEGRATION.read_text(encoding="utf-8")
        assert 'get("backend", {}).get("hooks", {})' in loader
        assert 'hooks.get("routes", "")' in loader

        manifest = manifest_data()
        hooks = manifest.get("backend", {}).get("hooks", {})
        assert hooks.get("routes", "") == "", (
            "this app now declares backend.hooks.routes, which the App Kit route "
            "registry dispatches under a different contract "
            "(register_fn(ctx) -> list[AppRoute]) than the builtin loop's "
            "register_routes(app). Two dispatchers, one route module: pick one."
        )

    def test_the_loops_tolerance_covers_a_missing_app_not_a_broken_import(self) -> None:
        """The catch clause, traced rather than trusted.

        Whoever adds route code reached through this list inherits this: the only
        swallowed failure is the app package itself being absent. Any other
        ``ModuleNotFoundError`` — an optional dependency missing on one platform,
        a mistyped submodule — is re-raised and fails gateway startup for every
        app, not just this one.
        """
        source = _dashboard_source()
        loop = _builtin_route_loop(source)
        tries = [node for node in ast.walk(loop) if isinstance(node, ast.Try)]
        assert tries, "the builtin loop no longer wraps the import in a try"

        handlers = [handler for node in tries for handler in node.handlers]
        caught = {handler.type.id for handler in handlers if isinstance(handler.type, ast.Name)}
        assert caught == {"ModuleNotFoundError"}, (
            f"the builtin loop now catches {sorted(caught)}; a wider catch would "
            "swallow a broken route import and serve 404s silently instead"
        )
        # Re-raised unless the missing module IS the app package: that comparison
        # is what makes the tolerance narrow, so it is asserted, not described.
        segment = " ".join((ast.get_source_segment(source, handler) or "") for handler in handlers)
        assert "exc.name" in segment
        assert any(
            isinstance(node, ast.Raise) for handler in handlers for node in ast.walk(handler)
        ), "the handler no longer re-raises: every ModuleNotFoundError is swallowed"


class TestWhatTheEntryStillDoesNotBuy:
    """The two costs the refusal weighed. One is unchanged; one is now accepted."""

    def test_the_legacy_on_disable_hook_still_cannot_fire(self) -> None:
        package = importlib.import_module(PACKAGE)
        assert not hasattr(package, "on_disable")
        # And it could not fire even if it did: that branch keys on the URL app
        # name, which is the manifest's hyphenated spelling.
        assert manifest_data()["name"] not in BUILTIN_NAMES

    def test_the_supported_shutdown_hook_is_the_manifests(self) -> None:
        """What the app uses instead, so the absence above reads as a choice."""
        hooks = manifest_data().get("backend", {}).get("hooks", {})
        assert "on_disable" not in hooks
        assert set(hooks) <= {"on_startup", "on_shutdown"}

    def test_the_cli_alias_the_entry_adds_is_still_broken(self) -> None:
        """The accepted cost, stated as the truth rather than as a hope.

        The subcommand now exists because this list builds it. Its import target
        does not, so running it raises ``ModuleNotFoundError``.
        """
        assert importlib.util.find_spec(f"{PACKAGE}.mcp_server") is None

        cli_source = CLI_MODULE.read_text(encoding="utf-8")
        # The alias's name and its import target are both composed from the list
        # entry. If either shape changes, the recorded cost needs re-verifying.
        assert 'sub.add_parser(f"mcp-{_bname}"' in cli_source
        assert 'f"kiro_crew.apps.builtins.{args.command[4:]}.mcp_server"' in cli_source

    def test_the_mcp_server_the_app_does_ship_is_launched_from_the_manifest(self) -> None:
        """Why the broken alias costs nothing: nothing shipped reaches for it."""
        command = manifest_data()["mcpServers"]["spec-engine"]
        assert command["args"][-1].endswith("engine_mcp.server")
        assert command["args"][-1] != f"{PACKAGE}.mcp_server"


class TestTheResidualIsUnchangedByTheEntry:
    """Enablement was manifest-driven before the entry and must stay so after."""

    def test_the_app_is_not_enabled_by_default_on_a_real_host(self) -> None:
        """``register_builtin_apps()`` defaults ``defaultEnabled`` to ``True``, so
        the key has to be present and false, not merely not-true.
        """
        data = manifest_data()

        assert "defaultEnabled" in data
        assert data["defaultEnabled"] is False

    def test_the_registration_path_never_consults_the_list(self) -> None:
        """Read from the functions themselves: the entry changes no enabled state.

        The refusal proved this by reading the code; the reversal has to prove it
        again, because "adding the entry enables the app" is exactly the wrong
        inference an operator could draw from a card appearing to change.
        """
        from kiro_crew.apps.discovery import discover_builtin_apps
        from kiro_crew.apps.manager import register_builtin_apps

        for function in (register_builtin_apps, discover_builtin_apps):
            assert "BUILTIN_NAMES" not in inspect.getsource(function), (
                f"{function.__name__} now reads BUILTIN_NAMES; the entry would "
                "then affect registration or enabled state, which this module "
                "records as impossible"
            )


class TestTheManifestAndThePackageAgreeOnRoutes:
    """One dangling half of this contract serves 404s without failing anything."""

    def test_a_route_module_on_disk_is_re_exported_from_the_package(self) -> None:
        """Keyed on the route module existing, which is when the risk begins.

        The loop reads ``register_routes`` off the app PACKAGE, not off
        ``backend.routes``, so a route module that the package never re-exports
        registers nothing and every handler in it 404s — with no error anywhere,
        which is the failure mode this asserts against. The manifest field is not
        the trigger: it can legitimately be declared a wave before the module
        exists, and a manifest-keyed assertion would fail that intermediate state
        instead of the mistake.
        """
        module_on_disk = (APP_ROOT / "backend" / "routes.py").is_file()
        exposes = hasattr(importlib.import_module(PACKAGE), "register_routes")
        assert module_on_disk == exposes, (
            f"backend/routes.py exists={module_on_disk} but the package exposes "
            f"register_routes={exposes}. The builtin loop reads the PACKAGE "
            "attribute, so a route module the package does not re-export never "
            "registers — add `from .backend.routes import register_routes` to the "
            "app package's __init__.py, as every sibling builtin does."
        )

    def test_declared_routes_name_the_entry_point_the_loop_would_find(self) -> None:
        """The manifest field is documentation here, so it must not misdocument.

        Nothing dispatches ``backend.routes`` for a builtin, but a reader (and the
        App Kit's own reference) takes it as the entry point. Whatever it names has
        to be the callable the loop actually calls.
        """
        declared = manifest_data().get("backend", {}).get("routes", "")
        if declared:
            assert declared.endswith(":register_routes"), (
                f"manifest declares backend.routes={declared!r}; the builtin loop "
                "calls register_routes and nothing dispatches any other name"
            )

    @pytest.mark.parametrize("sibling", ["spec_builder", "issue_radar"])
    def test_the_sibling_builtins_hold_that_contract_too(self, sibling: str) -> None:
        """Non-vacuity for the check above, which passes on two absences as well
        as on two presences. A sibling that HAS routes proves the assertion can
        tell the states apart rather than only agreeing with an empty app.
        """
        sibling_root = APP_ROOT.parent / sibling
        manifest = json.loads((sibling_root / "app.json").read_text(encoding="utf-8"))
        assert manifest.get("backend", {}).get("routes", "").endswith(":register_routes")
        assert (sibling_root / "backend" / "routes.py").is_file()
        package = importlib.import_module(f"kiro_crew.apps.builtins.{sibling}")
        assert hasattr(package, "register_routes")
