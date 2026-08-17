"""The Operator_Surface's routes, tested at route level.

Four claims, in descending order of how much damage getting them wrong does.

**The operator-only guard holds on every mutating route.** Not on the six the
author remembered: the parametrization is derived from the REGISTERED route
table, and a completeness assertion fails if the table grows a mutating route
this module does not drive. The refusal is exercised through a real request with
an app identity on it, and the security event is captured and checked against
``SecurityEventLog.log_api_access``'s own signature, so a call this suite accepts
is one the real SEL would accept too.

**The guard cannot be reached around.** ``dashboard/token_auth.py`` grants an app
token its own ``/api/apps/<name>/*`` namespace unconditionally, with no manifest
entry required — asserted here against the real predicate for every path this
module registers, so the 403s above are shown to be the ONLY thing standing
between an app-minted token and the config write.

**Nothing blocking runs on the event loop.** Asserted structurally over the
module's own AST: inside every handler, every reference to a blocking helper lies
inside an ``asyncio.to_thread`` call.

**The module cannot take gateway startup down.** Import and ``register_routes``
are both proved side-effect-free, because the builtin loop that calls them
tolerates only a ``ModuleNotFoundError`` naming the app package itself.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.apps.builtins.spec_engine.backend import routes
from kiro_crew.apps.builtins.spec_engine.engine.config import (
    CONFIG_FILENAME,
    ConfigStore,
    ConfigWriteSurface,
    default_root,
)

ROUTES_SOURCE = Path(routes.__file__)


def _blocking_helpers() -> frozenset[str]:
    """Helpers in :mod:`routes` that touch disk or the state database.

    Derived from the module's own ``BLOCKING`` docstring markers rather than
    kept as a hand list: the hand-maintained version of this set already missed
    ``_release_feedback`` once — the helper was marked BLOCKING at its
    definition, wrapped correctly at its one call site, and invisible to this
    suite, so a regression on that handler would have passed. A helper whose
    docstring opens with ``BLOCKING-safe`` is excluded by that same convention.
    """
    module = ast.parse(ROUTES_SOURCE.read_text(encoding="utf-8"))
    marked: set[str] = set()
    for node in ast.walk(module):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        doc = ast.get_docstring(node) or ""
        if doc.startswith("BLOCKING") and not doc.startswith("BLOCKING-safe"):
            marked.add(node.name)
    return frozenset(marked)


#: Every one of these must be reached only from inside ``asyncio.to_thread``.
BLOCKING_HELPERS = _blocking_helpers()


def test_the_derived_blocking_set_still_sees_the_known_helpers() -> None:
    """A marker-format drift must fail loudly, not silently empty the set.

    If ``BLOCKING`` markers were reworded, :func:`_blocking_helpers` would
    return fewer names and every downstream off-loop assertion would pass on
    nothing. Pinning known members keeps the derivation honest, and pinning
    ``_release_feedback`` specifically keeps the name that the hand list lost.
    """
    assert {
        "_config_store",
        "_state_store",
        "_review_queue",
        "_write_config",
        "_queue_snapshot",
        "_release_feedback",
    } <= BLOCKING_HELPERS
    assert "_audit_log" not in BLOCKING_HELPERS, (
        "_audit_log's docstring marks it BLOCKING-safe; the derivation must "
        "honor the -safe suffix rather than matching the BLOCKING prefix alone"
    )


# --- reading the module as source -------------------------------------------
#
# The structural claims below read routes.py's AST rather than the imported
# module: an import-time side effect has already fired by the time a test runs,
# so it cannot be observed after the fact.


def _module_ast() -> ast.Module:
    return ast.parse(ROUTES_SOURCE.read_text(encoding="utf-8"))


def _handler_ast(name: str) -> ast.AsyncFunctionDef:
    for node in _module_ast().body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            return node
    raise AssertionError(f"no handler named {name} in {ROUTES_SOURCE}")


def _to_thread_ids(handler: ast.AST) -> set[int]:
    """Identities of every node inside an ``asyncio.to_thread(...)`` call.

    Keyed by ``id`` because two structurally equal AST nodes are distinct
    objects, and "is this the same node" is exactly the question being asked.
    """
    inside: set[int] = set()
    for node in ast.walk(handler):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "to_thread"
        ):
            inside.update(id(child) for child in ast.walk(node))
    return inside


def _stray_blocking(handler: ast.AST) -> list[str]:
    """Blocking helpers *handler* reaches from OUTSIDE ``asyncio.to_thread``."""
    threaded = _to_thread_ids(handler)
    return [
        node.id
        for node in ast.walk(handler)
        if isinstance(node, ast.Name) and node.id in BLOCKING_HELPERS and id(node) not in threaded
    ]


def _caught_names(node: ast.AST) -> set[str]:
    """Every exception class name the ``except`` clauses under *node* name."""
    names: set[str] = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.ExceptHandler) or child.type is None:
            continue
        elements = child.type.elts if isinstance(child.type, ast.Tuple) else [child.type]
        for element in elements:
            if isinstance(element, ast.Name):
                names.add(element.id)
            elif isinstance(element, ast.Attribute):
                names.add(element.attr)
    return names


#: Every handler the module defines, read from source once at collection.
HANDLER_NAMES = tuple(
    node.name
    for node in _module_ast().body
    if isinstance(node, ast.AsyncFunctionDef) and node.name.startswith("handle_")
)


def _guard_mark(handler: Any) -> str:
    """The operation the operator guard stamped on *handler*, or ``""``.

    One reader for the mark, so the two claims made about the route table — every
    mutation carries it, no read does — are asking the router the same question.
    """
    return str(getattr(handler, routes.OPERATOR_GUARD_MARK, "") or "")


# --- the route table --------------------------------------------------------


def _route_table() -> tuple[tuple[str, str, Any], ...]:
    """``(method, canonical path, handler)`` for everything the module registers.

    Building it is also the first assertion in this file: calling the real
    ``register_routes`` must need no data home, no store and no gateway, because
    it runs on every boot including boots where this opt-in app is disabled.
    """
    app = web.Application()
    routes.register_routes(app)
    return tuple(
        (route.method, route.resource.canonical, route.handler)
        for route in app.router.routes()
        if route.resource is not None
    )


#: Every registered route, resolved once at collection.
TABLE = _route_table()

#: The mutating half, DERIVED rather than listed, so a new POST or PUT joins the
#: guard's parametrization without anybody remembering to add it.
MUTATING = tuple(sorted((method, path) for method, path, _ in TABLE if method != "GET"))

#: The read half.
READS = tuple(sorted((method, path) for method, path, _ in TABLE if method == "GET"))

#: What each mutating route needs in its body to get PAST parsing. The guard runs
#: before the body is read, so these matter only for tests that must reach a
#: handler — but an entry per mutating path also means a route added without one
#: fails the completeness assertion rather than going untested.
MUTATING_BODIES: dict[tuple[str, str], dict[str, Any]] = {
    ("PUT", f"{routes.PREFIX}/config"): {"patch": {"limits": {"task_retry_limit": 5}}},
    ("POST", f"{routes.PREFIX}/kill-switch"): {"action": "engage", "reason": "test"},
    ("POST", f"{routes.PREFIX}/queue/release-feedback"): {
        "project": "/tmp/project",
        "spec": "example",
        "run_id": "run-1",
        "comment_id": "c1",
    },
    ("POST", f"{routes.PREFIX}/queue/redispatch"): {
        "source": "github",
        "item_id": "1",
        "generation": 1,
    },
    ("POST", f"{routes.PREFIX}/queue/clean-workspace"): {"workspace_id": 1},
    ("POST", f"{routes.PREFIX}/queue/teardown"): {"run_id": "run-1"},
}


# --- harness ----------------------------------------------------------------


class RecordedSel:
    """Stands in for the SEL singleton and keeps what it was handed.

    The recorded kwargs are bound against the real ``log_api_access`` signature
    in the guard test, so this cannot accept a call the live audit log would
    reject. The singleton itself is not used: it binds a base directory at first
    construction, which would tie the whole suite to whichever isolated home ran
    first.
    """

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def log_api_access(self, **kwargs: Any) -> None:
        self.events.append(dict(kwargs))

    def denials(self) -> list[dict[str, Any]]:
        return [event for event in self.events if event.get("outcome") == "denied"]


@dataclass(frozen=True)
class Reply:
    """A response already drained, so it outlives the client's context manager.

    ``TestClient.__aexit__`` closes the connection and aiohttp streams the body,
    so reading it after the ``async with`` block raises ``ClientConnectionError``
    instead of returning what the handler sent. Draining at the call site keeps
    every assertion below about the handler rather than about stream lifetime.
    """

    status: int
    body: Any

    @property
    def code(self) -> str:
        """The machine-readable refusal code, or ``""`` for a non-refusal body."""
        return str(self.body.get("code", "")) if isinstance(self.body, dict) else ""


@pytest.fixture()
def recorded_sel(monkeypatch: pytest.MonkeyPatch) -> RecordedSel:
    recorder = RecordedSel()
    monkeypatch.setattr(routes, "sel", lambda: recorder)
    return recorder


@pytest.fixture()
def home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """An isolated data home, so no test reads or writes the real one.

    ``data_home()`` re-resolves a valid ``KIROCREW_HOME`` on every call, so stores
    constructed after this fixture runs land here.
    """
    root = tmp_path / "home"
    root.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(root))
    return root


@pytest.fixture()
def enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the app-enabled gate open. It is off by default on a real host."""
    monkeypatch.setattr(routes, "is_app_enabled", lambda name: True)


def _identity_middleware(user: str | None, app: str | None) -> Any:
    """Populate the two request keys the gateway's own auth middleware sets.

    ``user`` is the authenticated identity; ``app`` is the calling app's name for
    an app-minted token. ``None`` leaves the key unset, which is how an anonymous
    request arrives.
    """

    @web.middleware
    async def _mw(request: web.Request, handler: Any) -> web.StreamResponse:
        if user is not None:
            request["user"] = user
        if app is not None:
            request["app"] = app
        return await handler(request)

    return _mw


def _client(*, user: str | None = "operator", app: str | None = None) -> TestClient:
    application = web.Application(middlewares=[_identity_middleware(user, app)])
    routes.register_routes(application)
    return TestClient(TestServer(application))


async def _request(client: TestClient, method: str, path: str, body: Any = None) -> Reply:
    if method == "GET":
        response = await client.get(path)
    else:
        response = await client.request(method, path, json=body if body is not None else {})
    try:
        payload: Any = await response.json()
    except Exception:  # noqa: BLE001 - a non-JSON body is a fact to assert on
        payload = await response.text()
    return Reply(status=response.status, body=payload)


async def _get(client: TestClient, path: str) -> Reply:
    return await _request(client, "GET", path)


async def _put(client: TestClient, path: str, body: Any) -> Reply:
    return await _request(client, "PUT", path, body)


async def _post(client: TestClient, path: str, body: Any) -> Reply:
    return await _request(client, "POST", path, body)


# --- the guard, on every mutating route -------------------------------------


class TestAnAppTokenIsRefusedOnEveryMutatingRoute:
    """The confirmed escalation, closed and pinned.

    ``request["user"]`` is truthy for an app-minted token and the token layer
    grants the app its own namespace, so this 403 is the whole defence. Driven per
    ROUTE rather than per handler because the guard is applied at registration: a
    handler tested in isolation says nothing about whether the route that reaches
    it is wrapped.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("method", "path"), MUTATING, ids=lambda value: str(value))
    async def test_an_app_token_gets_403_and_a_security_event(
        self,
        method: str,
        path: str,
        recorded_sel: RecordedSel,
        enabled: None,
        home: Path,
    ) -> None:
        async with _client(user="spec-engine", app="spec-engine") as client:
            reply = await _request(client, method, path, MUTATING_BODIES[(method, path)])
        assert reply.status == 403
        assert reply.code == "dashboard_user_required"

        denials = recorded_sel.denials()
        assert len(denials) == 1, f"{method} {path} recorded {len(denials)} denial events"
        event = denials[0]
        assert event["caller"] == "spec-engine"
        assert event["resources"] == path
        assert event["source"] == "app_isolation"
        assert event["operation"], "the denial must name the operation it refused"
        # The recorded call must be one the real audit log would accept: a stub
        # tolerant of a misspelled keyword would let a denial that RAISES in
        # production pass here, and the raise happens inside the guard.
        from kiro_crew.sel import SecurityEventLog

        inspect.signature(SecurityEventLog.log_api_access).bind(object(), **event)

    def test_the_parametrization_covers_the_whole_mutating_table(self) -> None:
        """Completeness. A mutating route added without a case fails HERE.

        Without this, the suite above is only as complete as a list somebody
        maintained — which is the failure mode the guard exists to prevent.
        """
        assert set(MUTATING) == set(MUTATING_BODIES), (
            "the registered mutating routes and the driven ones disagree: "
            f"undriven={sorted(set(MUTATING) - set(MUTATING_BODIES))} "
            f"stale={sorted(set(MUTATING_BODIES) - set(MUTATING))}"
        )
        assert len(MUTATING) == 6, (
            "the mutating surface changed size; re-read the guard's reasoning in "
            "routes.py before accepting it"
        )

    def test_every_mutating_route_carries_the_guard_in_the_route_table(self) -> None:
        """A structural claim about the table, not about the handlers.

        The live 403s are per path; this says the same of the router itself, so a
        mutating route registered through the READ composer fails before anyone
        writes a request for it.
        """
        unguarded = [
            (method, path)
            for method, path, handler in TABLE
            if method != "GET" and not _guard_mark(handler)
        ]
        assert unguarded == [], (
            f"mutating routes registered without the operator guard: {unguarded}. "
            "Register them through _mutate(), which stamps the mark this reads."
        )

    def test_no_read_route_carries_the_guard(self) -> None:
        """Non-vacuity for the assertion above.

        A mark set on everything would make that check pass while examining
        nothing, and the reads are deliberately app-token-readable.
        """
        marked_reads = [
            (method, path)
            for method, path, handler in TABLE
            if method == "GET" and _guard_mark(handler)
        ]
        assert marked_reads == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("method", "path"), READS, ids=lambda value: str(value))
    async def test_an_app_token_may_still_read(
        self, method: str, path: str, recorded_sel: RecordedSel, enabled: None, home: Path
    ) -> None:
        """The line is drawn at mutation, and the reads prove it is a line.

        If app tokens were refused everywhere, the 403s above would say nothing
        about the guard being aimed at AUTHORITY rather than at app tokens as
        such. A read reaches its handler, so the status is whatever the empty
        isolated home yields — never the guard's 403, and never a denial event.
        """
        async with _client(user="spec-engine", app="spec-engine") as client:
            reply = await _request(client, method, path)
        assert reply.status != 403
        assert recorded_sel.denials() == []


class TestTheGuardCannotBeReachedAround:
    """The platform layer in front of these routes does NOT stop an app token."""

    @pytest.mark.parametrize(("method", "path"), MUTATING + READS, ids=lambda value: str(value))
    def test_the_token_layer_grants_this_apps_own_namespace(self, method: str, path: str) -> None:
        """Asserted against the real predicate, for every path registered.

        ``_app_owns_path`` grants ``/api/apps/<name>/*`` with no
        ``permissions.api`` entry needed, so ``_enforce_app_scope`` returns None
        and the request arrives at the handler. That is what makes the route-level
        403 load-bearing rather than a second belt.
        """
        from kiro_crew.dashboard.token_auth import app_token_path_allowed

        assert app_token_path_allowed("spec-engine", path) is True, (
            "the token layer no longer grants this app its own namespace; "
            "re-derive the guard's reasoning in routes.py before relaxing it"
        )

    def test_a_neighbouring_app_is_not_granted_this_namespace(self) -> None:
        """Non-vacuity: the predicate can say no, so the True above means something."""
        from kiro_crew.dashboard.token_auth import app_token_path_allowed

        assert app_token_path_allowed("spec-builder", f"{routes.PREFIX}/config") is False


class TestTheOtherTwoRefusals:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(("method", "path"), MUTATING + READS, ids=lambda value: str(value))
    async def test_an_anonymous_caller_gets_401(
        self, method: str, path: str, recorded_sel: RecordedSel, enabled: None, home: Path
    ) -> None:
        async with _client(user=None) as client:
            reply = await _request(client, method, path, MUTATING_BODIES.get((method, path)))
        assert reply.status == 401
        assert reply.code == "unauthorized"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("method", "path"), MUTATING + READS, ids=lambda value: str(value))
    async def test_a_disabled_app_answers_nothing(
        self,
        method: str,
        path: str,
        recorded_sel: RecordedSel,
        monkeypatch: pytest.MonkeyPatch,
        home: Path,
    ) -> None:
        """Deny-by-default for an opt-in app, ahead of both other gates."""
        monkeypatch.setattr(routes, "is_app_enabled", lambda name: False)
        async with _client() as client:
            reply = await _request(client, method, path, MUTATING_BODIES.get((method, path)))
        assert reply.status == 403
        assert reply.code == "app_disabled"


# --- the registered surface -------------------------------------------------


class TestTheRegisteredSurface:
    def test_the_method_and_path_set_is_the_declared_one(self) -> None:
        """The capability checklist, pinned. A route that vanishes 404s silently."""
        assert {(method, path) for method, path, _ in TABLE} == {
            ("GET", f"{routes.PREFIX}/config"),
            ("PUT", f"{routes.PREFIX}/config"),
            ("GET", f"{routes.PREFIX}/kill-switch"),
            ("POST", f"{routes.PREFIX}/kill-switch"),
            ("GET", f"{routes.PREFIX}/run-spend"),
            ("GET", f"{routes.PREFIX}/queue"),
            ("POST", f"{routes.PREFIX}/queue/release-feedback"),
            ("POST", f"{routes.PREFIX}/queue/redispatch"),
            ("POST", f"{routes.PREFIX}/queue/clean-workspace"),
            ("POST", f"{routes.PREFIX}/queue/teardown"),
        }

    def test_the_prefix_is_this_apps_own_namespace(self) -> None:
        """Served from a route this app declares, never one the Prior_App does.

        The prefix is composed from the manifest name, so this pins the
        composition as well as the value.
        """
        assert routes.PREFIX == "/api/apps/spec-engine"
        assert all(path.startswith(routes.PREFIX) for _, path, _ in TABLE)
        assert not any("spec-builder" in path for _, path, _ in TABLE)

    def test_registration_returns_none_and_takes_the_gateway_application(self) -> None:
        """The builtin contract, which differs from the App Kit's route registry:
        ``register_routes(app) -> None`` over full paths, not
        ``register_fn(ctx) -> list[AppRoute]``."""
        signature = inspect.signature(routes.register_routes)
        assert list(signature.parameters) == ["app"]
        assert signature.return_annotation in (None, "None")


# --- the startup hazard -----------------------------------------------------


def _is_safe_module_scope_call(node: ast.Call) -> bool:
    """Whether *node* is a constant-table or logger call, not work.

    Narrow on purpose: anything that is not one of these is reported, so a new
    kind of import-time call has to be read and admitted rather than inferred.
    """
    if isinstance(node.func, ast.Attribute) and node.func.attr == "getLogger":
        return True
    return isinstance(node.func, ast.Name) and node.func.id in {"frozenset", "tuple", "dict"}


class TestThisModuleCannotTakeGatewayStartupDown:
    """The builtin loop re-raises everything but a missing app package.

    So an error at import time here, or in ``register_routes``, fails startup for
    EVERY app rather than only this one. Both are asserted rather than promised.
    """

    def test_import_time_code_performs_no_io_and_builds_no_store(self) -> None:
        offenders: list[str] = []
        for node in _module_ast().body:
            if isinstance(
                node,
                (
                    ast.Import,
                    ast.ImportFrom,
                    ast.FunctionDef,
                    ast.AsyncFunctionDef,
                    ast.ClassDef,
                    ast.AnnAssign,
                ),
            ):
                continue
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
                continue
            if isinstance(node, ast.Assign):
                # A constant assignment is fine; a CALL on the right-hand side is
                # how a store or a resolved data home reaches import time.
                calls = [
                    child
                    for child in ast.walk(node.value)
                    if isinstance(child, ast.Call) and not _is_safe_module_scope_call(child)
                ]
                if calls:
                    offenders.append(f"line {node.lineno}: call at module scope")
                continue
            offenders.append(f"line {node.lineno}: {type(node).__name__} at module scope")
        assert offenders == [], (
            f"routes.py now does work at import time: {offenders}. The builtin "
            "route loop imports this module on every gateway boot, including boots "
            "where the app is disabled, and re-raises anything it raises."
        )

    def test_the_import_time_detector_reports_a_planted_side_effect(self) -> None:
        """Non-vacuity: the check above passes on the module as written."""
        planted = ast.parse("STORE = ConfigStore()\nPATH = frozenset({'a'})\n")
        calls = [
            child
            for node in planted.body
            if isinstance(node, ast.Assign)
            for child in ast.walk(node.value)
            if isinstance(child, ast.Call) and not _is_safe_module_scope_call(child)
        ]
        assert len(calls) == 1

    def test_the_module_imports_nothing_outside_the_stdlib_and_this_repo(self) -> None:
        """Why no ``ModuleNotFoundError`` can arise here on any platform.

        ``aiohttp`` is the one third-party name, and it is the framework the
        gateway that imports this module is itself built on — a process that
        reached the builtin loop has it. Anything else would be a dependency this
        module cannot assume, which is exactly the shape that takes every app's
        routes down with it.
        """
        import sys

        roots: set[str] = set()
        for node in ast.walk(_module_ast()):
            if isinstance(node, ast.Import):
                roots.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                roots.add(node.module.split(".")[0])
        foreign = {
            root
            for root in roots
            if root not in sys.stdlib_module_names
            and root not in {"kiro_crew", "__future__", "aiohttp"}
        }
        assert foreign == set(), f"routes.py imports {sorted(foreign)}"

    def test_register_routes_touches_no_store_and_no_disk(self) -> None:
        """Registration is pure wiring, asserted twice.

        Structurally, so a future edit that constructs a store is caught by
        reading; and by running it twice on fresh applications with no data home
        prepared, so a real dependency on process state surfaces here rather than
        at a customer's gateway boot.
        """
        register = next(
            node
            for node in _module_ast().body
            if isinstance(node, ast.FunctionDef) and node.name == "register_routes"
        )
        reached = {
            child.func.id
            for child in ast.walk(register)
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Name)
        }
        assert not (
            reached & BLOCKING_HELPERS
        ), f"register_routes calls blocking helpers: {sorted(reached & BLOCKING_HELPERS)}"
        for _ in range(2):
            routes.register_routes(web.Application())


# --- off the event loop -----------------------------------------------------


class TestNothingBlockingRunsOnTheEventLoop:
    """Every disk and database read is inside ``asyncio.to_thread``.

    Structural, over this module's own AST, because the alternative — observing a
    stalled loop — is a timing test. Constructing a ``StateStore`` opens SQLite
    and migrates the schema, so "the store construction is on the loop" is the
    easy version of this mistake, and it is the one the deleted predecessor made.
    """

    @pytest.mark.parametrize("name", sorted(HANDLER_NAMES))
    def test_a_handler_reaches_blocking_work_only_through_to_thread(self, name: str) -> None:
        stray = _stray_blocking(_handler_ast(name))
        assert stray == [], (
            f"{name} reaches {sorted(set(stray))} outside asyncio.to_thread; that "
            "is disk or SQLite work on the gateway's event loop"
        )

    def test_every_handler_was_examined(self) -> None:
        """Non-vacuity: the parametrization must be neither empty nor partial."""
        assert len(HANDLER_NAMES) == len(TABLE), (
            "the handler set and the route table disagree in size: "
            f"{sorted(HANDLER_NAMES)} against {len(TABLE)} routes"
        )

    def test_the_detector_sees_a_planted_stray_call(self) -> None:
        """One reference outside a thread and one inside, so the detector is shown
        to tell the two apart rather than to reject whatever it is handed."""
        planted = ast.parse(
            "async def handle_bad(request):\n"
            "    store = _config_store()\n"
            "    return await asyncio.to_thread(lambda: _state_store())\n"
        )
        assert _stray_blocking(planted.body[0]) == ["_config_store"]


# --- the configuration surface ----------------------------------------------


class TestTheConfigurationRoutes:
    @pytest.mark.asyncio
    async def test_an_unconfigured_home_reports_itself_unconfigured(
        self, recorded_sel: RecordedSel, enabled: None, home: Path
    ) -> None:
        """The page leads with setup rather than an empty form, which it can only
        do if this field distinguishes "absent" from "empty" — both of which
        serialize to ``{}``."""
        async with _client() as client:
            reply = await _get(client, f"{routes.PREFIX}/config")
        assert reply.status == 200
        assert reply.body["configured"] is False
        assert reply.body["document"] == {}
        assert reply.body["errors"] == []
        assert reply.body[
            "config_only_paths"
        ], "the fenced paths must travel for a panel to mark them"

    @pytest.mark.asyncio
    async def test_a_patch_round_trips_through_the_engines_write_path(
        self, recorded_sel: RecordedSel, enabled: None, home: Path
    ) -> None:
        async with _client() as client:
            written = await _put(
                client,
                f"{routes.PREFIX}/config",
                {"patch": {"limits": {"task_retry_limit": 7}}},
            )
            assert written.status == 200, written.body
            assert written.body["ok"] is True
            read = await _get(client, f"{routes.PREFIX}/config")
        assert read.body["configured"] is True
        assert read.body["document"]["limits"]["task_retry_limit"] == 7
        # The engine's durable record of WHO wrote, taken by the route from the
        # authenticated session rather than from the body.
        store = ConfigStore(Path(read.body["path"]).parent)
        assert [record.get("actor") for record in store.writes()] == ["operator"]

    @pytest.mark.asyncio
    async def test_a_body_without_a_patch_key_is_taken_as_the_patch(
        self, recorded_sel: RecordedSel, enabled: None, home: Path
    ) -> None:
        """The MCP tool's argument shape posts the patch as the body itself."""
        async with _client() as client:
            written = await _put(
                client, f"{routes.PREFIX}/config", {"limits": {"task_retry_limit": 4}}
            )
        assert written.status == 200, written.body

    @pytest.mark.asyncio
    async def test_a_value_the_engine_refuses_comes_back_as_a_validation_refusal(
        self, recorded_sel: RecordedSel, enabled: None, home: Path
    ) -> None:
        """The engine validates; this route reports. 422, not 500 — and nothing
        lands, because a refused write must leave the previous document alone."""
        async with _client() as client:
            reply = await _put(
                client,
                f"{routes.PREFIX}/config",
                {"patch": {"limits": {"task_retry_limit": -1}}},
            )
        assert reply.status == 422
        assert reply.code == "config_invalid"
        assert "task_retry_limit" in reply.body["error"]
        assert not (default_root() / CONFIG_FILENAME).exists()

    @pytest.mark.asyncio
    async def test_a_non_object_patch_is_a_400(
        self, recorded_sel: RecordedSel, enabled: None, home: Path
    ) -> None:
        async with _client() as client:
            reply = await _put(client, f"{routes.PREFIX}/config", {"patch": [1, 2]})
        assert reply.status == 400
        assert reply.code == "bad_patch"

    @pytest.mark.asyncio
    async def test_a_body_that_is_not_an_object_is_a_400(
        self, recorded_sel: RecordedSel, enabled: None, home: Path
    ) -> None:
        """Valid JSON is not necessarily an object, and a bare scalar would
        otherwise fail on ``.get()`` as a 500."""
        async with _client() as client:
            reply = await _put(client, f"{routes.PREFIX}/config", [1, 2])
        assert reply.status == 400
        assert reply.code == "bad_json"

    @pytest.mark.asyncio
    async def test_the_surface_fence_arm_reports_a_refusal_rather_than_a_failure(
        self,
        recorded_sel: RecordedSel,
        enabled: None,
        home: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``ConfigWriteRefused`` is unreachable at this surface TODAY, and the arm
        is kept anyway — this test is how that stays honest.

        The route writes on an operator-confirmed surface, so the engine never
        refuses it a config-only path; the arm guards the surface CONSTANT, and
        without it a future non-confirmed surface would render a legitimate engine
        refusal as a 503 "write failed" and send the operator to fix a disk problem
        that does not exist.

        The ordering matters and is exercised here too: ``ConfigWriteRefused``
        derives ``PermissionError``, which derives ``OSError``, so the generic
        ``OSError`` arm below it WOULD swallow this refusal if the two were
        reordered.
        """
        monkeypatch.setattr(routes, "WRITE_SURFACE", ConfigWriteSurface("automation"))
        async with _client() as client:
            reply = await _put(client, f"{routes.PREFIX}/config", {"patch": {"quality_gates": []}})
        assert reply.status == 403
        assert reply.code == "config_write_refused"
        assert "quality_gates" in reply.body["error"]

    def test_the_write_surface_is_operator_confirmed(self) -> None:
        """Which is only sound because the guard refused every non-human first."""
        assert routes.WRITE_SURFACE.operator_confirmed is True

    @pytest.mark.asyncio
    async def test_an_unparseable_document_is_reported_not_read_as_empty(
        self, recorded_sel: RecordedSel, enabled: None, home: Path
    ) -> None:
        """ "Nothing is configured" would send the operator to the setup assistant,
        which would then refuse to write over a file it cannot parse."""
        config_dir = default_root()
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / CONFIG_FILENAME).write_text("{ not json", encoding="utf-8")
        async with _client() as client:
            reply = await _get(client, f"{routes.PREFIX}/config")
        assert reply.status == 409
        assert reply.code == "config_unreadable"

    @pytest.mark.asyncio
    async def test_a_credential_value_never_leaves_the_surface(
        self, recorded_sel: RecordedSel, enabled: None, home: Path
    ) -> None:
        """This read is a path onto a browser, so the store's own classification
        elides before the document travels."""
        config_dir = default_root()
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / CONFIG_FILENAME).write_text(
            json.dumps({"version": 1, "projects": {"acme": {"variables": {"api_key": "s3cret"}}}}),
            encoding="utf-8",
        )
        async with _client() as client:
            reply = await _get(client, f"{routes.PREFIX}/config")
        assert "s3cret" not in json.dumps(reply.body)
        assert "projects.acme.variables.api_key" in reply.body["elided"]

    @pytest.mark.asyncio
    async def test_the_report_is_built_from_one_read_of_the_document(
        self, recorded_sel: RecordedSel, enabled: None, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A torn REPORT, not a torn document: ``validate`` and ``advisories`` each
        re-read the file, so composing the reply from those accessors would let a
        write landing between them describe two different documents — errors from
        one, advisories from the next.

        Driven by counting reads: one snapshot must read the document once.
        """
        reads: list[int] = []
        original = ConfigStore.document

        def _counting(self: ConfigStore) -> dict[str, Any]:
            reads.append(1)
            return original(self)

        monkeypatch.setattr(ConfigStore, "document", _counting)
        async with _client() as client:
            reply = await _get(client, f"{routes.PREFIX}/config")
        assert reply.status == 200
        assert sum(reads) == 1, (
            f"the config snapshot read the document {sum(reads)} times; a write "
            "landing between reads yields a report describing two documents"
        )


# --- the operational reads --------------------------------------------------


class TestTheOperationalReads:
    @pytest.mark.asyncio
    async def test_an_empty_queue_is_an_empty_queue_and_not_an_error(
        self, recorded_sel: RecordedSel, enabled: None, home: Path
    ) -> None:
        async with _client() as client:
            reply = await _get(client, f"{routes.PREFIX}/queue")
        assert reply.status == 200
        assert reply.body["entries"] == []
        assert reply.body["grouped"] == {}
        assert reply.body["total"] == 0
        assert reply.body["total_credits"] == 0

    @pytest.mark.asyncio
    async def test_the_kill_switch_reads_released_with_nothing_stoppable(
        self, recorded_sel: RecordedSel, enabled: None, home: Path
    ) -> None:
        async with _client() as client:
            reply = await _get(client, f"{routes.PREFIX}/kill-switch")
        assert reply.status == 200
        assert reply.body["switch"]["engaged"] is False
        assert reply.body["stoppable"] == []
        assert reply.body["stoppable_credits"] == 0

    @pytest.mark.asyncio
    async def test_a_spend_view_needs_a_run_id(
        self, recorded_sel: RecordedSel, enabled: None, home: Path
    ) -> None:
        async with _client() as client:
            reply = await _get(client, f"{routes.PREFIX}/run-spend")
        assert reply.status == 400
        assert reply.code == "field_required"

    @pytest.mark.asyncio
    async def test_an_unknown_run_is_a_404_rather_than_an_empty_view(
        self, recorded_sel: RecordedSel, enabled: None, home: Path
    ) -> None:
        async with _client() as client:
            reply = await _get(client, f"{routes.PREFIX}/run-spend?run_id=nope")
        assert reply.status == 404
        assert reply.code == "run_unknown"


# --- the mutating handlers reached by an operator ---------------------------


class TestTheKillSwitchIsOperableAndReadsBackItsOwnState:
    @pytest.mark.asyncio
    async def test_engage_then_release_is_confirmed_by_the_persisted_flag(
        self, recorded_sel: RecordedSel, enabled: None, home: Path
    ) -> None:
        """Confirmed by reading state back, never by the response status alone."""
        async with _client() as client:
            engaged = await _post(
                client, f"{routes.PREFIX}/kill-switch", {"action": "engage", "reason": "audit"}
            )
            assert engaged.status == 200, engaged.body
            assert engaged.body["switch"]["engaged"] is True

            after_engage = await _get(client, f"{routes.PREFIX}/kill-switch")
            assert after_engage.body["switch"]["engaged"] is True

            released = await _post(client, f"{routes.PREFIX}/kill-switch", {"action": "release"})
            assert released.status == 200, released.body
            assert released.body["changed"] is True
            assert released.body["switch"]["engaged"] is False
            # Stated, because it is what an operator most often assumes otherwise:
            # a release lets NEW work start and resumes nothing.
            assert released.body["resumed"] == []

            after_release = await _get(client, f"{routes.PREFIX}/kill-switch")
            assert after_release.body["switch"]["engaged"] is False

    @pytest.mark.asyncio
    async def test_the_engage_is_attributed_to_the_session_not_the_body(
        self, recorded_sel: RecordedSel, enabled: None, home: Path
    ) -> None:
        """A stop recorded against a name its caller supplied records nothing."""
        async with _client(user="alice") as client:
            reply = await _post(
                client,
                f"{routes.PREFIX}/kill-switch",
                {"action": "engage", "initiator": "somebody-else"},
            )
        assert reply.body["switch"]["initiator"] == "alice"

    @pytest.mark.asyncio
    async def test_an_unknown_action_is_refused_before_anything_is_written(
        self, recorded_sel: RecordedSel, enabled: None, home: Path
    ) -> None:
        async with _client() as client:
            reply = await _post(client, f"{routes.PREFIX}/kill-switch", {"action": "stop"})
            assert reply.status == 400
            assert reply.code == "bad_action"
            state = await _get(client, f"{routes.PREFIX}/kill-switch")
        assert state.body["switch"]["engaged"] is False

    @pytest.mark.asyncio
    async def test_a_non_string_reason_is_refused(
        self, recorded_sel: RecordedSel, enabled: None, home: Path
    ) -> None:
        async with _client() as client:
            reply = await _post(
                client, f"{routes.PREFIX}/kill-switch", {"action": "engage", "reason": 7}
            )
        assert reply.status == 400
        assert reply.code == "bad_reason"


class TestTheQueueActionsValidateBeforeTheyReachTheEngine:
    @pytest.mark.asyncio
    async def test_a_release_names_every_field_it_is_missing(
        self, recorded_sel: RecordedSel, enabled: None, home: Path
    ) -> None:
        async with _client() as client:
            reply = await _post(
                client, f"{routes.PREFIX}/queue/release-feedback", {"project": "/p"}
            )
        assert reply.status == 400
        assert reply.code == "field_required"
        for field in ("spec", "run_id", "comment_id"):
            assert field in reply.body["error"]

    @pytest.mark.asyncio
    async def test_a_redispatch_without_a_generation_is_refused(
        self, recorded_sel: RecordedSel, enabled: None, home: Path
    ) -> None:
        """The generation identifies the claim being lifted; guessing one would
        release a claim the operator did not name."""
        async with _client() as client:
            reply = await _post(
                client,
                f"{routes.PREFIX}/queue/redispatch",
                {"source": "github", "item_id": "7"},
            )
        assert reply.status == 400
        assert "generation" in reply.body["error"]

    @pytest.mark.asyncio
    async def test_a_boolean_workspace_id_is_not_read_as_row_one(
        self, recorded_sel: RecordedSel, enabled: None, home: Path
    ) -> None:
        """``True`` is an ``int`` in Python, so a bool would resolve to row 1."""
        async with _client() as client:
            reply = await _post(
                client, f"{routes.PREFIX}/queue/clean-workspace", {"workspace_id": True}
            )
        assert reply.status == 400
        assert reply.code == "field_required"

    @pytest.mark.asyncio
    async def test_a_cleanup_for_an_unknown_row_answers_rather_than_failing(
        self, recorded_sel: RecordedSel, enabled: None, home: Path
    ) -> None:
        """``None`` from the engine means no active row has that id, so a double
        click reads as "nothing to do" and not as a second removal."""
        async with _client() as client:
            reply = await _post(
                client, f"{routes.PREFIX}/queue/clean-workspace", {"workspace_id": 99}
            )
        assert reply.status == 200, reply.body
        assert reply.body["removed"] is False
        assert reply.body["cleanup"] is None

    @pytest.mark.asyncio
    async def test_a_teardown_needs_a_run_id(
        self, recorded_sel: RecordedSel, enabled: None, home: Path
    ) -> None:
        async with _client() as client:
            reply = await _post(client, f"{routes.PREFIX}/queue/teardown", {})
        assert reply.status == 400
        assert reply.code == "field_required"

    @pytest.mark.asyncio
    async def test_a_teardown_reports_the_ids_it_kept(
        self, recorded_sel: RecordedSel, enabled: None, home: Path
    ) -> None:
        """A run with no recorded workspaces keeps nothing and is complete; the
        field is present in both cases so a surface never has to dig for it and
        never renders an incomplete teardown as done."""
        async with _client() as client:
            reply = await _post(client, f"{routes.PREFIX}/queue/teardown", {"run_id": "run-1"})
        assert reply.status == 200, reply.body
        assert reply.body["kept"] == []
        assert reply.body["complete"] is True
        assert reply.body["report"]["kept"] == []


# --- catch clauses traced against the raising code --------------------------


class TestRefusalsAreTracedAgainstTheClassChainTheEngineRaises:
    """The dominant defect class of the predecessor: a tuple that cannot catch.

    ``StateStore`` wraps every ``sqlite3.Error``/``OSError`` into
    ``StatePersistenceError``, and that class derives ``StateError``, which
    derives ``Exception`` DIRECTLY. So the ``(OSError, ValueError)`` tuple the
    deleted surface used over its queue actions could not catch the one failure
    those arms existed for.
    """

    def test_the_state_error_chain_is_what_the_module_assumes(self) -> None:
        from kiro_crew.apps.builtins.spec_engine.engine.state import (
            StateError,
            StatePersistenceError,
        )

        assert issubclass(StatePersistenceError, StateError)
        assert StateError.__mro__[1] is Exception
        assert not issubclass(StateError, OSError)
        assert not issubclass(StateError, ValueError)

    def test_the_config_error_chain_is_what_the_module_assumes(self) -> None:
        from kiro_crew.apps.builtins.spec_engine.engine.config import (
            ConfigLoadError,
            ConfigRecordError,
            ConfigValidationError,
            ConfigWriteRefused,
        )

        assert issubclass(ConfigValidationError, ValueError)
        assert issubclass(ConfigWriteRefused, PermissionError)
        # The ordering constraint in handle_put_config, as a fact rather than a
        # comment: a PermissionError IS an OSError.
        assert issubclass(ConfigWriteRefused, OSError)
        assert issubclass(ConfigLoadError, RuntimeError)
        assert issubclass(ConfigRecordError, RuntimeError)
        assert not issubclass(ConfigLoadError, OSError)

    def test_the_review_refusal_is_not_covered_by_the_tuple_beside_it(self) -> None:
        from kiro_crew.apps.builtins.spec_engine.engine.review_queue import (
            ReviewFeedbackRefused,
        )

        assert ReviewFeedbackRefused.__mro__[1] is Exception
        assert not issubclass(ReviewFeedbackRefused, (OSError, ValueError))

    @pytest.mark.parametrize(
        "handler",
        [
            "handle_post_release_feedback",
            "handle_post_redispatch",
            "handle_post_clean_workspace",
            "handle_post_teardown",
        ],
    )
    def test_every_state_mutating_handler_names_state_error(self, handler: str) -> None:
        """Read off the source, so a future edit that drops the arm fails here."""
        caught = _caught_names(_handler_ast(handler))
        assert "StateError" in caught, (
            f"{handler} does not catch StateError; a sqlite or filesystem failure "
            "in the engine's state store would surface as a bare 500"
        )

    def test_the_engage_path_names_the_config_load_error_too(self) -> None:
        """Engage reads the budget through the config document AFTER the flag has
        persisted, so an unparseable document raises with the stop in force."""
        assert {"StateError", "ConfigLoadError"} <= _caught_names(
            _handler_ast("handle_post_kill_switch")
        )

    @pytest.mark.asyncio
    async def test_a_state_failure_becomes_a_503_and_not_a_500(
        self,
        recorded_sel: RecordedSel,
        enabled: None,
        home: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The arm, driven. Proves the catch reaches what the engine RAISES rather
        than merely naming a class that appears in the module."""
        from kiro_crew.apps.builtins.spec_engine.engine.state import StatePersistenceError

        def _boom() -> Any:
            raise StatePersistenceError("the database is gone")

        monkeypatch.setattr(routes, "_review_queue", _boom)
        async with _client() as client:
            reply = await _post(client, f"{routes.PREFIX}/queue/teardown", {"run_id": "run-1"})
        assert reply.status == 503
        assert reply.code == "teardown_failed"


# --- the app package re-export the gateway reads ----------------------------


def test_the_app_package_re_exports_register_routes() -> None:
    """The builtin loop reads this attribute off the APP package.

    A route module the package does not re-export registers nothing and every
    handler in it 404s with no error anywhere — a failure mode with no symptom,
    so it gets an assertion here as well as in the registration pin test.
    """
    import kiro_crew.apps.builtins.spec_engine as package

    assert package.register_routes is routes.register_routes


def test_the_module_does_not_reach_into_the_neighbouring_app() -> None:
    """This surface depends on no route and no module the Prior_App declares."""
    source = ROUTES_SOURCE.read_text(encoding="utf-8")
    assert "spec_builder" not in source
    assert "spec-builder" not in source


def test_the_module_starts_no_threads_of_its_own() -> None:
    """A thread the module started would outlive the request that made it and
    would not be bounded by the loop's executor."""
    source = ROUTES_SOURCE.read_text(encoding="utf-8")
    assert "threading" not in source
    assert "asyncio.to_thread" in source
    assert asyncio.to_thread is not None
