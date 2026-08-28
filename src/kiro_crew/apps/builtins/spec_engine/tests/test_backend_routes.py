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
import itertools
import copy
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from kiro_crew.apps.builtins.spec_engine.backend import routes
from kiro_crew.apps.builtins.spec_engine.engine import local_analyzer
from kiro_crew.apps.builtins.spec_engine.engine.autonomy import (
    AutonomyDecision,
    AutonomyLevel,
)
from kiro_crew.apps.builtins.spec_engine.engine.capabilities.contracts import (
    DISPLAY_TRUNCATION_NOTICE,
    sanitized,
)
from kiro_crew.apps.builtins.spec_engine.engine.config import (
    AUTONOMY_LEVELS,
    CONFIG_FILENAME,
    DELEGABLE_CAPABILITIES,
    DELIVERY_STAGES,
    ENGINE_FLOOR_CAPABILITIES,
    GATE_POSITIONS,
    GATE_SEVERITIES,
    LEAST_TRUSTED_CLASS,
    PIPELINE_STAGE_ADVANCED,
    PIPELINE_STAGES,
    PROFILE_SETTING_KEYS,
    ROLES,
    SETTING_GROUP_ORDER,
    SETTINGS,
    SPEC_TYPES,
    SUBMITTER_CLASSES,
    TRANSPORTS,
    WILDCARD_KEY,
    ConfigStore,
    ConfigWriteSurface,
    Scope,
    capability_stage,
    default_root,
    pipeline,
    setting_group_stage,
)
from kiro_crew.apps.builtins.spec_engine.engine.config.profiles import (
    COST_PROFILE_PRESET_NAMES,
    cost_profile_presets,
)
from kiro_crew.apps.builtins.spec_engine.engine.delivery import (
    DELIVERY_FLOW_STAGES,
    MAX_PRESET_NAME_CHARS,
    WORKFLOW_PRESET_NAMES,
    WORKFLOW_PRESETS,
    gate_presets,
)
from kiro_crew.apps.builtins.spec_engine.engine.setup import CONFIRMED_LEVELS
from kiro_crew.apps.builtins.spec_engine.engine.watch.sources import (
    WATCH_SOURCE_PRESET_HOSTS,
    WATCH_SOURCE_PRESET_PROGRAMS,
    watch_source_presets,
)
from kiro_crew.apps.builtins.spec_engine.engine_mcp.operations import EngineOperations
from kiro_crew.apps.builtins.spec_engine.engine_mcp.setup_surface import (
    REFUSAL_APPROVER_REQUIRED,
    REFUSAL_PLAN_STALE,
    REFUSAL_SETUP_APPROVAL,
    REFUSED_KEY,
    StalePlan,
)
from kiro_crew.effort import EFFORT_LEVELS

ROUTES_SOURCE = Path(routes.__file__)

#: The approver identity the setup tests name. A human identity, because that is
#: what the apply demands and records.
APPROVER = "operator@example"

#: Fresh directory names for the property tests, which cannot take a
#: function-scoped fixture per example.
_UNIQUE = itertools.count()


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
        "_resolved_snapshot",
        "_workflow_snapshot",
        "_sources_snapshot",
        "_inspect_setup",
        "_plan_envelope",
        "_plan_setup",
        "_apply_setup",
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

#: A concrete value per dynamic path segment the table carries. A registered path
#: is a TEMPLATE, and a template is not requestable: driven literally, aiohttp's
#: dynamic pattern excludes braces, so the request 404s and the parametrized
#: 401/403 tests would assert against a routing miss instead of against the gate.
PATH_VARIABLES: dict[str, str] = {"capability": "analysis"}


def _requestable(path: str) -> str:
    """*path* with each dynamic segment replaced by a value that resolves."""
    for name, value in PATH_VARIABLES.items():
        path = path.replace("{" + name + "}", value)
    return path


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
    # Conformance starts a job that spawns the operator-configured program, so it
    # is driven here like any other mutating route.
    ("POST", f"{routes.PREFIX}/config/conformance"): {"capability": "analysis"},
    # The setup flow. Its two read-only steps are guarded because the project path
    # is the CALLER's, so they are driven here like any other mutating route.
    ("POST", f"{routes.PREFIX}/setup/inspect"): {"project": "/tmp/project"},
    ("POST", f"{routes.PREFIX}/setup/plan"): {"project": "/tmp/project", "answers": {}},
    ("POST", f"{routes.PREFIX}/setup/apply"): {
        "project": "/tmp/project",
        "answers": {},
        "plan_id": "0" * 64,
        "approver": "operator@example",
    },
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
            reply = await _request(
                client, method, _requestable(path), MUTATING_BODIES[(method, path)]
            )
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
        assert len(MUTATING) == 10, (
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
            reply = await _request(client, method, _requestable(path))
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

        assert app_token_path_allowed("spec-engine", _requestable(path)) is True, (
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
            reply = await _request(
                client, method, _requestable(path), MUTATING_BODIES.get((method, path))
            )
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
            reply = await _request(
                client, method, _requestable(path), MUTATING_BODIES.get((method, path))
            )
        assert reply.status == 403
        assert reply.code == "app_disabled"


# --- the registered surface -------------------------------------------------


class TestTheRegisteredSurface:
    def test_the_method_and_path_set_is_the_declared_one(self) -> None:
        """The capability checklist, pinned. A route that vanishes 404s silently."""
        assert {(method, path) for method, path, _ in TABLE} == {
            ("GET", f"{routes.PREFIX}/config"),
            ("PUT", f"{routes.PREFIX}/config"),
            ("GET", f"{routes.PREFIX}/config/resolved"),
            ("GET", f"{routes.PREFIX}/config/registry"),
            ("GET", f"{routes.PREFIX}/config/workflow"),
            ("GET", f"{routes.PREFIX}/config/sources"),
            ("GET", f"{routes.PREFIX}/config/capabilities"),
            ("POST", f"{routes.PREFIX}/config/conformance"),
            ("GET", f"{routes.PREFIX}/config/conformance/{{capability}}"),
            ("POST", f"{routes.PREFIX}/setup/inspect"),
            ("POST", f"{routes.PREFIX}/setup/plan"),
            ("POST", f"{routes.PREFIX}/setup/apply"),
            ("GET", f"{routes.PREFIX}/kill-switch"),
            ("POST", f"{routes.PREFIX}/kill-switch"),
            ("GET", f"{routes.PREFIX}/run-spend"),
            ("GET", f"{routes.PREFIX}/queue"),
            ("POST", f"{routes.PREFIX}/queue/release-feedback"),
            ("POST", f"{routes.PREFIX}/queue/redispatch"),
            ("POST", f"{routes.PREFIX}/queue/clean-workspace"),
            ("POST", f"{routes.PREFIX}/queue/teardown"),
        }

    def test_every_dynamic_path_segment_has_a_concrete_value(self) -> None:
        """Non-vacuity for every parametrized test that drives the table.

        A registered template nobody substituted is requested literally, aiohttp's
        dynamic pattern excludes braces, and the request 404s — so the 401 and 403
        assertions above would be asserting against a routing miss rather than
        against the gate they were written for.
        """
        unsubstituted = [(method, path) for method, path, _ in TABLE if "{" in _requestable(path)]
        assert unsubstituted == [], (
            f"registered paths with no value in PATH_VARIABLES: {unsubstituted}. "
            "Add one, or the parametrized gate tests silently assert on a 404."
        )

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
        # The substituted value travels too, and it is the store's own constant: an
        # editor has to recognise it to keep it out of a patch, and a client-side
        # copy of the string is a second spelling of one constant that can drift.
        from kiro_crew.apps.builtins.spec_engine.engine.config.store import ELIDED

        assert reply.body["elided_marker"] == ELIDED
        assert reply.body["document"]["projects"]["acme"]["variables"]["api_key"] == ELIDED

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


# --- the resolved read beside the document -----------------------------------


class TestTheResolvedReadIsAReadOfTheDocumentBesideIt:
    """The value in force, and where each one came from.

    The document alone cannot answer "what is actually in force here": a setting
    resolves through five layers, and the layer an operator is editing is one of
    them. This read is what closes that gap, and it is a READ — it writes nothing
    and it is the same ``ConfigStore`` the write path uses, resolved rather than
    re-implemented.
    """

    @pytest.mark.asyncio
    async def test_a_zero_configuration_home_resolves_every_setting_to_its_default(
        self, recorded_sel: RecordedSel, enabled: None, home: Path
    ) -> None:
        async with _client() as client:
            reply = await _get(client, f"{routes.PREFIX}/config/resolved")
        assert reply.status == 200
        assert reply.body["configured"] is False
        assert reply.body["settings"], "a resolved read with no settings shows nothing"
        for value in reply.body["settings"]:
            # Origin is the field that makes the read worth having: it separates a
            # value somebody chose from one the app shipped, and those call for
            # opposite actions.
            assert value["origin"] == "bundled_default"
            assert value["is_default"] is True
            assert value["declared_at"] == ""

    @pytest.mark.asyncio
    async def test_a_profile_the_project_selected_is_reported_as_the_origin(
        self, recorded_sel: RecordedSel, enabled: None, home: Path
    ) -> None:
        """The layer that is easiest to get wrong: selecting a profile is a
        per-project act, so a profile pin beats the app-wide value and loses to a
        value pinned on the project itself."""
        async with _client() as client:
            written = await _put(
                client,
                f"{routes.PREFIX}/config",
                {
                    "patch": {
                        "cost_profiles": {
                            "thrifty": {
                                "roles": {"review": {"model": "auto", "effort": "high"}},
                                "budget": {"run_ceiling_credits": 3.0},
                            }
                        },
                        "projects": {"acme": {"path": "/tmp/acme", "cost_profile": "thrifty"}},
                    }
                },
            )
            assert written.status == 200, written.body
            resolved = await _get(client, f"{routes.PREFIX}/config/resolved?project=acme")
        assert resolved.status == 200
        values = {value["key"]: value for value in resolved.body["settings"]}
        ceiling = values["budget.run_ceiling_credits"]
        assert ceiling["value"] == 3.0
        assert ceiling["origin"] == "cost_profile"
        assert ceiling["declared_at"] == "cost_profiles.thrifty.budget.run_ceiling_credits"

    @pytest.mark.asyncio
    async def test_the_role_plan_is_the_engines_own_resolution(
        self, recorded_sel: RecordedSel, enabled: None, home: Path
    ) -> None:
        """Relayed, not re-derived. The table a surface renders must be the
        resolution a DISPATCH would use, including the four distinct fallback
        reasons — a surface that read the raw profile object instead would answer
        "which model will review run on" differently from the run."""
        async with _client() as client:
            await _put(
                client,
                f"{routes.PREFIX}/config",
                {
                    "patch": {
                        "cost_profiles": {
                            "thrifty": {"roles": {"review": {"model": "auto", "effort": "high"}}}
                        },
                        "projects": {"acme": {"path": "/tmp/acme", "cost_profile": "thrifty"}},
                    }
                },
            )
            resolved = await _get(client, f"{routes.PREFIX}/config/resolved?project=acme")
        roles = resolved.body["roles"]
        assert roles["profile"] == "thrifty"
        assert set(roles["roles"]) == set(ROLES)
        review = roles["roles"]["review"]
        assert review["source"] == "cost_profile"
        assert review["model"] == "auto"
        # The declaring node, which is what an accurately labelled per-role reset
        # names. The profile NAME travels separately, so a surface never has to
        # split this dotted string to learn it.
        assert review["declared_at"] == "cost_profiles.thrifty.roles.review"
        assert review["profile"] == "thrifty"
        # A role the profile says nothing about falls back and says why, rather
        # than reporting a model nobody assigned.
        design = roles["roles"]["design"]
        assert design["source"] == "session_default"
        assert design["fallback"] == "role_unassigned"
        assert design["report"]
        # Order travels because a JSON object has none a client may rely on.
        assert resolved.body["role_order"] == list(ROLES)

    @pytest.mark.asyncio
    async def test_a_profile_name_holding_a_dot_keeps_its_role_node_addressable(
        self, recorded_sel: RecordedSel, enabled: None, home: Path
    ) -> None:
        """Segment-wise, never by splitting a dotted path.

        A profile may legitimately be named ``thrifty.roles``, and then the role
        node's declaring path reads ``cost_profiles.thrifty.roles.roles.review``.
        Any consumer that recovered the profile and the role by splitting that
        string would address a node that does not exist — so the profile name and
        the role name travel as their own fields, which is what lets a reset build
        its patch from segments.
        """
        async with _client() as client:
            await _put(
                client,
                f"{routes.PREFIX}/config",
                {
                    "patch": {
                        "cost_profiles": {
                            "thrifty.roles": {"roles": {"review": {"model": "auto"}}}
                        },
                        "projects": {
                            "acme": {"path": "/tmp/acme", "cost_profile": "thrifty.roles"}
                        },
                    }
                },
            )
            resolved = await _get(client, f"{routes.PREFIX}/config/resolved?project=acme")
        review = resolved.body["roles"]["roles"]["review"]
        assert review["profile"] == "thrifty.roles"
        assert review["role"] == "review"
        assert review["declared_at"] == "cost_profiles.thrifty.roles.roles.review"

    @pytest.mark.asyncio
    async def test_the_reply_is_built_from_one_read_of_the_document(
        self, recorded_sel: RecordedSel, enabled: None, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``effective_settings`` and ``RolePlan.for_run`` each re-read the file, so
        composing this reply from those two accessors would resolve the settings
        from one document and the roles from the next."""
        reads: list[int] = []
        original = ConfigStore.document

        def _counting(self: ConfigStore) -> dict[str, Any]:
            reads.append(1)
            return original(self)

        monkeypatch.setattr(ConfigStore, "document", _counting)
        async with _client() as client:
            reply = await _get(client, f"{routes.PREFIX}/config/resolved")
        assert reply.status == 200
        assert sum(reads) == 1, f"the resolved read read the document {sum(reads)} times"

    @pytest.mark.asyncio
    async def test_a_stored_value_that_fails_its_own_setting_is_a_422(
        self, recorded_sel: RecordedSel, enabled: None, home: Path
    ) -> None:
        """Resolution RAISES on an out-of-range explicit value rather than falling
        through to the default, and this arm keeps that: silently substituting the
        default would run the very work the operator meant to bound. Written to
        disk directly, because the write path would have refused it."""
        config_dir = default_root()
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / CONFIG_FILENAME).write_text(
            json.dumps({"version": 1, "budget": {"warn_fraction": 9.5}}), encoding="utf-8"
        )
        async with _client() as client:
            reply = await _get(client, f"{routes.PREFIX}/config/resolved")
        assert reply.status == 422
        assert reply.code == "config_invalid"
        assert "warn_fraction" in reply.body["error"]

    @pytest.mark.asyncio
    async def test_an_unparseable_document_is_reported_not_resolved_as_empty(
        self, recorded_sel: RecordedSel, enabled: None, home: Path
    ) -> None:
        config_dir = default_root()
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / CONFIG_FILENAME).write_text("{ not json", encoding="utf-8")
        async with _client() as client:
            reply = await _get(client, f"{routes.PREFIX}/config/resolved")
        assert reply.status == 409
        assert reply.code == "config_unreadable"


# --- the capability bindings ---------------------------------------------------


def _store_document(document: dict[str, Any]) -> None:
    """Write *document* straight to the data home, bypassing the write door.

    Needed for the refusal cases: an engine-floor binding is exactly what the
    write path rejects, so the only way a document holding one exists is that
    somebody hand-edited the file — which is the case the read has to survive.
    """
    config_dir = default_root()
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / CONFIG_FILENAME).write_text(json.dumps(document), encoding="utf-8")


class TestTheCapabilityReadJoinsEachBindingToItsReachability:
    """Which provider serves each capability, and whether it can be reached.

    Both halves are the engine's own answers rather than this surface's: the
    binding description comes from the capability registry, and reachability from
    the same provider check a run's prerequisite gate reports against — so a
    binding shown here as reachable is one a run would accept.

    The claim that carries the most weight is negative. An unconfigured document
    resolves to an all-builtin map, and a document the engine REFUSES must never
    arrive as that map: the two are the same shape and opposite facts, and
    conflating them would show a refused document as a clean one.
    """

    @pytest.mark.asyncio
    async def test_an_unconfigured_home_answers_every_capability_as_its_builtin(
        self, recorded_sel: RecordedSel, enabled: None, home: Path
    ) -> None:
        """Nothing configured is not nothing bound. Every delegable capability
        ships a builtin, so none of them can answer "not configured"."""
        async with _client() as client:
            reply = await _get(client, f"{routes.PREFIX}/config/capabilities")
        assert reply.status == 200
        assert reply.body["configured"] is False
        entries = {entry["capability"]: entry for entry in reply.body["capabilities"]}
        assert list(entries) == list(DELEGABLE_CAPABILITIES), (
            "the read must answer for every capability the engine declares delegable, "
            "in the engine's own order"
        )
        for entry in entries.values():
            assert entry["transport"] == "builtin"
            assert entry["configured"] is False
            assert entry["declared_at"] == ""
            assert entry["program"] == ""
            assert entry["provider"]["kind"] == "builtin"

    @pytest.mark.asyncio
    async def test_one_entry_carries_the_engines_description_beside_its_reachability(
        self, recorded_sel: RecordedSel, enabled: None, home: Path
    ) -> None:
        """The whole shape of one row, so a field that quietly disappears fails
        here rather than in a client that stops rendering it."""
        async with _client() as client:
            reply = await _get(client, f"{routes.PREFIX}/config/capabilities")
        entries = {entry["capability"]: entry for entry in reply.body["capabilities"]}
        assert entries["model_catalog"] == {
            "capability": "model_catalog",
            "transport": "builtin",
            "provider": {
                "name": "engine-model-catalog-host",
                "kind": "builtin",
                "nature": "deterministic",
                "transport": "builtin",
            },
            "configured": False,
            "declared_at": "",
            # The resolved deadline, not the raw override: the binding declares
            # none, so the app setting's value is what one call would get.
            "timeout_s": 120,
            "program": "",
            "reachable": None,
            "action": "",
        }

    @pytest.mark.asyncio
    async def test_the_builtins_that_stand_for_a_model_backed_path_say_so(
        self, recorded_sel: RecordedSel, enabled: None, home: Path
    ) -> None:
        """The distinction an operator learns which capabilities cost money from.

        Authoring, review and implementation are served by a seeded turn, and
        until the engine's own builtins are registered over the registry all three
        resolve to the shipped deterministic no-coverage default — which reports a
        path that spends credits as one that spends nothing.
        """
        async with _client() as client:
            reply = await _get(client, f"{routes.PREFIX}/config/capabilities")
        natures = {
            entry["capability"]: entry["provider"]["nature"] for entry in reply.body["capabilities"]
        }
        assert natures["authoring"] == "model_backed"
        assert natures["review"] == "model_backed"
        assert natures["implementation"] == "model_backed"
        assert natures["model_catalog"] == "deterministic"
        assert natures["watch_sources"] == "deterministic"

    @pytest.mark.asyncio
    async def test_a_binding_on_its_builtin_reports_reachability_as_not_applicable(
        self, recorded_sel: RecordedSel, enabled: None, home: Path
    ) -> None:
        """``null``, not ``false``. A builtin is reachable by construction — it is
        this engine — so the engine's check skips it, and coercing that to ``false``
        would show a broken provider on a capability that has none."""
        _store_document({"version": 1, "capabilities": {"analysis": {"transport": "builtin"}}})
        async with _client() as client:
            reply = await _get(client, f"{routes.PREFIX}/config/capabilities")
        assert reply.status == 200
        entries = {entry["capability"]: entry for entry in reply.body["capabilities"]}
        analysis = entries["analysis"]
        # Declared by an operator AND on its builtin: the two are independent, and
        # the row has to carry both without inventing a program to check.
        assert analysis["configured"] is True
        assert analysis["declared_at"] == "capabilities.analysis"
        assert analysis["reachable"] is None
        assert analysis["action"] == ""

    @pytest.mark.asyncio
    async def test_a_delegated_program_off_the_path_is_unreachable_with_the_escape(
        self, recorded_sel: RecordedSel, enabled: None, home: Path
    ) -> None:
        """The remediation names both ways out, because unsetting the binding is
        the one that always works: the capability's builtin is still there."""
        _store_document(
            {
                "version": 1,
                "capabilities": {
                    "review": {
                        "transport": "command",
                        "command": ["definitely-not-on-this-path", "--check"],
                    }
                },
            }
        )
        async with _client() as client:
            reply = await _get(client, f"{routes.PREFIX}/config/capabilities")
        assert reply.status == 200
        entries = {entry["capability"]: entry for entry in reply.body["capabilities"]}
        review = entries["review"]
        assert review["transport"] == "command"
        assert review["program"] == "definitely-not-on-this-path"
        assert review["reachable"] is False
        assert "unset capabilities.review to use the builtin" in review["action"]
        # An external provider is reported model-backed because the engine cannot
        # know whether it reasons. The payload projects that as the engine states
        # it and adds no reading of its own.
        assert review["provider"]["kind"] == "external"
        # The capabilities that were not bound are unaffected, and still not
        # reported as unreachable.
        assert entries["analysis"]["reachable"] is None

    @pytest.mark.asyncio
    async def test_a_delegated_transport_with_an_empty_command_is_a_read_failure(
        self, recorded_sel: RecordedSel, enabled: None, home: Path
    ) -> None:
        """A transport that runs a program and names none cannot be resolved at
        all, so it is a read failure rather than a binding reported unreachable."""
        _store_document(
            {"version": 1, "capabilities": {"analysis": {"transport": "mcp", "command": []}}}
        )
        async with _client() as client:
            reply = await _get(client, f"{routes.PREFIX}/config/capabilities")
        assert reply.status == 422
        assert reply.code == "capabilities_unreadable"
        assert "capabilities" not in reply.body

    @pytest.mark.asyncio
    async def test_an_engine_floor_binding_is_a_read_failure_not_an_all_builtin_read(
        self, recorded_sel: RecordedSel, enabled: None, home: Path
    ) -> None:
        """The refusal that must not be mistaken for a clean document.

        ``resolve_bindings`` pre-seeds every capability with its builtin BEFORE it
        consults the document, so the failure path and the unconfigured path differ
        only in whether a raise escaped. A surface that swallowed the raise would
        report a document the engine refuses to run against as one with nothing
        configured.
        """
        _store_document({"version": 1, "capabilities": {"audit_log": {"transport": "command"}}})
        async with _client() as client:
            reply = await _get(client, f"{routes.PREFIX}/config/capabilities")
        assert reply.status == 422
        assert reply.code == "capabilities_unreadable"
        assert "audit_log" in reply.body["error"]
        assert "capabilities" not in reply.body, (
            "a refused document must carry no binding list at all; an all-builtin "
            "list beside the refusal is what a client would render"
        )

    @pytest.mark.asyncio
    async def test_a_section_that_is_not_an_object_is_a_read_failure(
        self, recorded_sel: RecordedSel, enabled: None, home: Path
    ) -> None:
        """The third way resolution refuses, and the same answer."""
        _store_document({"version": 1, "capabilities": ["analysis"]})
        async with _client() as client:
            reply = await _get(client, f"{routes.PREFIX}/config/capabilities")
        assert reply.status == 422
        assert reply.code == "capabilities_unreadable"
        assert "capabilities" not in reply.body

    @pytest.mark.asyncio
    async def test_an_unknown_capability_is_a_read_failure(
        self, recorded_sel: RecordedSel, enabled: None, home: Path
    ) -> None:
        _store_document({"version": 1, "capabilities": {"telepathy": {"transport": "builtin"}}})
        async with _client() as client:
            reply = await _get(client, f"{routes.PREFIX}/config/capabilities")
        assert reply.status == 422
        assert reply.code == "capabilities_unreadable"

    @pytest.mark.asyncio
    async def test_an_unparseable_document_is_reported_rather_than_read_as_builtin(
        self, recorded_sel: RecordedSel, enabled: None, home: Path
    ) -> None:
        config_dir = default_root()
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / CONFIG_FILENAME).write_text("{ not json", encoding="utf-8")
        async with _client() as client:
            reply = await _get(client, f"{routes.PREFIX}/config/capabilities")
        assert reply.status == 409
        assert reply.code == "config_unreadable"

    def test_the_binding_read_takes_no_project_because_there_is_no_project_layer(
        self,
    ) -> None:
        """The engine reads ``capabilities`` from one app-wide section with no
        per-project layer, so a project-scoped read would imply a scope that does
        not exist and let two projects appear to bind different providers."""
        assert list(inspect.signature(routes._capabilities_snapshot).parameters) == []

    @pytest.mark.asyncio
    async def test_the_whole_join_rests_on_one_read_of_the_document(
        self, recorded_sel: RecordedSel, enabled: None, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Every answer in a row must come from the SAME document.

        The join has several engine reads behind it — the registry's bindings, its
        description, the per-capability resolved timeout, and the provider checks —
        and each of them resolves the store independently. ``ConfigStore.document``
        re-reads and re-parses the file every call, so without a pinned read a
        write landing partway through yields a row whose halves describe different
        documents. That is not a cosmetic skew here: a binding read from the new
        document has no matching check in a report built from the old one, so the
        row renders ``reachable: null`` — which this payload's contract means
        "builtin, not applicable". A configured external provider would be
        reported as a builtin.

        Counted rather than raced, because the interleaving is what a test cannot
        schedule reliably: exactly one read means there is no window to interleave.
        """
        reads: list[str] = []
        original_document = ConfigStore.document
        original_store = routes._config_store

        def _counting_document(self: ConfigStore) -> dict[str, Any]:
            reads.append("document")
            return original_document(self)

        def _counting_store() -> ConfigStore:
            reads.append("store")
            return original_store()

        monkeypatch.setattr(ConfigStore, "document", _counting_document)
        monkeypatch.setattr(routes, "_config_store", _counting_store)
        async with _client() as client:
            reply = await _get(client, f"{routes.PREFIX}/config/capabilities")
        assert reply.status == 200
        assert reads.count("document") == 1, (
            "the capability join must read the document exactly once; "
            f"observed {reads.count('document')} reads: {reads}"
        )
        # Non-vacuous: the reply really is the composed seven-row join, so the
        # single read above was enough to answer the whole payload rather than
        # evidence that nothing was read because nothing was built.
        assert len(reply.body["capabilities"]) == len(DELEGABLE_CAPABILITIES)

    def test_a_pinned_store_refuses_to_write(self, home: Path) -> None:
        """The pinned read is a snapshot, and a snapshot must not be a write base.

        ``ConfigStore.write`` merges its patch onto ``self.document()``. Writing
        through a store whose document is frozen would merge onto a read taken
        arbitrarily long ago and silently drop every change that landed since, so
        the refusal is the class's reason for being a class rather than a dict.
        """
        pinned = routes._PinnedStore(routes._config_store())
        assert pinned.document() is pinned.document(), "the pinned read must be one object"
        with pytest.raises(RuntimeError, match="cannot write"):
            pinned.write({}, surface=routes.SETUP_SURFACE)

    @pytest.mark.asyncio
    async def test_the_analysis_builtin_named_is_the_one_a_run_would_bind(
        self, recorded_sel: RecordedSel, enabled: None, home: Path
    ) -> None:
        """The provider named must be the provider that would serve the call.

        ``AnalysisEngine`` binds the analysis capability through
        ``local_analyzer.register``, so a run is served by the structural
        analyzer. Until that registration runs over the registry, the capability
        resolves to the shipped declared-skip default — which reports NO COVERAGE
        under a different name. Naming that default here would tell an operator
        their analysis capability is served by something a run never uses.
        """
        async with _client() as client:
            reply = await _get(client, f"{routes.PREFIX}/config/capabilities")
        entries = {entry["capability"]: entry for entry in reply.body["capabilities"]}
        provider = entries["analysis"]["provider"]
        assert provider["name"] == local_analyzer.PROVIDER_NAME
        assert provider["nature"] == "deterministic", (
            "the structural analyzer computes its answer; reporting it as "
            "model-backed would claim a cost it does not incur"
        )

    def test_the_snapshot_does_not_mutate_the_document_it_pinned(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The pinned read is shared by every consumer, so none of them may write to it.

        ``_PinnedStore.document`` hands the SAME dict to each consumer rather than
        a fresh parse, which is what makes the join atomic — and also what would
        let one mutating consumer corrupt every later read inside the same reply.
        Every consumer on this path is read-only today; this fails the moment one
        is added that is not, which the aliasing alone cannot report.

        Observed through the store the SNAPSHOT builds, not one built here: the
        snapshot constructs its own, so a store created in this test would be a
        different object and could not witness a mutation inside the join.
        """
        (home / "config.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "capabilities": {"review": {"transport": "command", "command": ["true"]}},
                }
            ),
            encoding="utf-8",
        )
        witnessed: list[tuple[dict[str, Any], dict[str, Any]]] = []
        real_pinned = routes._PinnedStore

        class _Recording(real_pinned):  # type: ignore[valid-type,misc]
            def __init__(self, store: ConfigStore) -> None:
                super().__init__(store)
                # The live object beside a copy of it as it was at pin time.
                witnessed.append((self.document(), copy.deepcopy(self.document())))

        monkeypatch.setattr(routes, "_PinnedStore", _Recording)
        routes._capabilities_snapshot()
        assert witnessed, "the snapshot pinned no document, so nothing was observed"
        for live, at_pin_time in witnessed:
            assert live == at_pin_time, (
                "a consumer on the capability path mutated the pinned document; "
                "with one shared read that corrupts every later read in the same reply"
            )


# --- the form vocabulary ------------------------------------------------------


class TestTheFormVocabularyReadProjectsTheEnginesOwnConstants:
    """The registry read, asserted against the constants it projects.

    The claim worth defending is that a form generated from this payload is
    generated from the vocabulary the ENGINE enforces against. So these compare
    the payload with the owning modules' tables rather than with a literal copy of
    them written here: a second spelling of the registry in this file would keep
    passing after the registry moved on, which is precisely the drift the read
    exists to prevent.

    The 401 floor and the disabled-app refusal are not repeated here: both are
    parametrized over the route table, so registering this path enrolled it. The
    refusal-by-path contract its sibling config reads carry does not apply — this
    read opens no document, so there is no stored value to be unreadable.
    """

    @pytest.mark.asyncio
    async def test_every_registry_setting_is_projected_in_registry_order(
        self, recorded_sel: RecordedSel, enabled: None, home: Path
    ) -> None:
        async with _client() as client:
            reply = await _get(client, f"{routes.PREFIX}/config/registry")
        assert reply.status == 200
        keys = [entry["key"] for entry in reply.body["settings"]]
        assert keys == list(SETTINGS), "the projection dropped, added or reordered a setting"
        assert len(keys) == 21, (
            "the setting registry changed size; a form is generated from this "
            "vocabulary, so re-read the new setting's kind, bounds and scopes "
            "before accepting the count"
        )

    @pytest.mark.asyncio
    async def test_one_entry_carries_its_whole_registry_record(
        self, recorded_sel: RecordedSel, enabled: None, home: Path
    ) -> None:
        """Field by field, on a setting whose record exercises every arm: an int
        kind, a minimum, no maximum, two of the three scopes, and a summary."""
        async with _client() as client:
            reply = await _get(client, f"{routes.PREFIX}/config/registry")
        entries = {entry["key"]: entry for entry in reply.body["settings"]}
        assert entries["concurrency.wave_max_tasks"] == {
            "key": "concurrency.wave_max_tasks",
            "kind": "int",
            "default": 3,
            "minimum": 1,
            # Null rather than absent: a numeric input branches on whether a bound
            # exists, and an omitted key reads as a shape change.
            "maximum": None,
            "scopes": ["app", "project"],
            "summary": "Leaf tasks the orchestrator dispatches in parallel within one wave.",
        }

    @pytest.mark.asyncio
    async def test_every_entry_agrees_with_its_own_setting_record(
        self, recorded_sel: RecordedSel, enabled: None, home: Path
    ) -> None:
        """The whole table, not only the pinned entry: a kind name that lost its
        mapping or a scope set that leaked frozenset iteration order would send a
        form a control the write door refuses."""
        async with _client() as client:
            reply = await _get(client, f"{routes.PREFIX}/config/registry")
        for entry in reply.body["settings"]:
            setting = SETTINGS[entry["key"]]
            assert entry["kind"] == setting.kind.__name__
            assert entry["default"] == setting.default
            assert entry["minimum"] == setting.minimum
            assert entry["maximum"] == setting.maximum
            assert entry["summary"] == setting.summary
            # Broadest-first, and only the scopes the registry permits: offering a
            # scope the setting forbids would stage a write the door rejects as a
            # configuration error rather than ignoring it.
            assert entry["scopes"] == [
                scope.value
                for scope in (Scope.APP, Scope.PROJECT, Scope.SOURCE)
                if scope in setting.scopes
            ]

    @pytest.mark.asyncio
    async def test_no_projected_setting_declares_an_enforced_choice_set(
        self, recorded_sel: RecordedSel, enabled: None, home: Path
    ) -> None:
        """The precondition under which omitting ``choices`` is safe, asserted
        rather than assumed.

        A ``str`` setting renders as free text, and that is only sound because no
        setting declares ``choices``. The write door DOES enforce them
        (``Setting.coerce`` refuses a value outside the set), so a setting that
        gained one while the projection stayed silent would give the operator a
        text box whose every non-member entry the door then refuses by path. The
        vocabulary and a closed-choice control have to arrive together; this fails
        the moment the first half arrives alone.
        """
        async with _client() as client:
            reply = await _get(client, f"{routes.PREFIX}/config/registry")
        declaring = [key for key, setting in SETTINGS.items() if setting.choices]
        assert declaring == [], (
            f"{declaring} now declare choices the write door enforces; project "
            "`choices` in this read and give the settings form a closed-choice "
            "control in the same change, or its text input will offer values the "
            "door refuses"
        )
        assert all("choices" not in entry for entry in reply.body["settings"])

    @pytest.mark.asyncio
    async def test_each_source_preset_is_byte_equal_to_the_bundled_table(
        self, recorded_sel: RecordedSel, enabled: None, home: Path
    ) -> None:
        """Including the program, which the picker states before anything is
        copied, and the absence of ``enabled``, which is what makes a fresh copy
        inert until an operator arms it."""
        async with _client() as client:
            reply = await _get(client, f"{routes.PREFIX}/config/registry")
        presets = reply.body["source_presets"]
        assert [preset["host"] for preset in presets] == list(WATCH_SOURCE_PRESET_HOSTS)
        for preset in presets:
            host = preset["host"]
            assert preset["program"] == WATCH_SOURCE_PRESET_PROGRAMS[host]
            assert preset["entry"] == watch_source_presets(host)
            assert "enabled" not in preset["entry"], (
                f"the {host} preset arrived carrying enabled; a copied preset must "
                "be inert until an operator enables it"
            )

    @pytest.mark.asyncio
    async def test_the_profile_role_and_level_vocabularies_are_the_owning_modules(
        self, recorded_sel: RecordedSel, enabled: None, home: Path
    ) -> None:
        async with _client() as client:
            reply = await _get(client, f"{routes.PREFIX}/config/registry")
        assert [preset["name"] for preset in reply.body["profile_presets"]] == list(
            COST_PROFILE_PRESET_NAMES
        )
        assert reply.body["roles"] == list(ROLES)
        assert reply.body["levels"] == list(AUTONOMY_LEVELS)
        # The two vocabularies the profiles form offers that the setting registry
        # cannot supply: pinnability is not a Scope, and effort is not a setting.
        # Both are enforced by the write door, so a form offering either from its
        # own copy would offer what the door then refuses.
        assert reply.body["profile_settings"] == list(PROFILE_SETTING_KEYS)
        assert reply.body["efforts"] == list(EFFORT_LEVELS)

    @pytest.mark.asyncio
    async def test_each_profile_preset_carries_the_entry_a_copy_is_made_from(
        self, recorded_sel: RecordedSel, enabled: None, home: Path
    ) -> None:
        """The name alone would not do. A form adds a profile as a COPY of one, so
        a client holding only names would have to invent the role assignments it
        claims to copy — which is the no-provenance profile the engine refuses to
        be useful with: every role resolves to the session default while the
        project reports that a profile is selected.
        """
        async with _client() as client:
            reply = await _get(client, f"{routes.PREFIX}/config/registry")
        for preset in reply.body["profile_presets"]:
            assert preset["entry"] == cost_profile_presets(preset["name"])
            # Every projected assignment names a model, because the write door
            # refuses an assignment without one: a form staging this entry
            # verbatim must not stage a document the door then rejects.
            for role, assignment in preset["entry"]["roles"].items():
                assert assignment["model"], f"{preset['name']}.{role} arrived with no model"

    @pytest.mark.asyncio
    async def test_each_extension_seam_vocabulary_is_the_owning_modules_tuple(
        self, recorded_sel: RecordedSel, enabled: None, home: Path
    ) -> None:
        """Order and membership against the owning tuples, never a copy written
        here. Each of these is a closed set the write door enforces, so a form
        offering a value from its own list would offer what the door refuses."""
        async with _client() as client:
            reply = await _get(client, f"{routes.PREFIX}/config/registry")
        assert reply.body["transports"] == list(TRANSPORTS)
        assert reply.body["delivery_stages"] == list(DELIVERY_STAGES)
        assert reply.body["gate_positions"] == list(GATE_POSITIONS)
        assert reply.body["gate_severities"] == list(GATE_SEVERITIES)
        assert reply.body["capabilities"] == list(DELEGABLE_CAPABILITIES)
        assert reply.body["workflow_presets"] == list(WORKFLOW_PRESET_NAMES)

    @pytest.mark.asyncio
    async def test_the_preset_name_cap_is_the_display_paths_own_ceiling(
        self, recorded_sel: RecordedSel, enabled: None, home: Path
    ) -> None:
        """The cap travels as the engine's own number rather than as a copy.

        A form that defines a preset name refuses one longer than this, because
        every reader of a preset name on this route renders it through
        ``sanitized`` at exactly this limit: a longer name would be displayed as
        a string no document holds. A copy kept on the far side would drift from
        the ceiling the display actually applies.
        """
        async with _client() as client:
            reply = await _get(client, f"{routes.PREFIX}/config/registry")
        assert reply.body["workflow_preset_name_limit"] == MAX_PRESET_NAME_CHARS
        # The cap the projection ACTUALLY applies, not merely the constant it
        # reports: a name at the limit survives whole and one character more does
        # not, so the number a form refuses against is the number that is enforced.
        limit = reply.body["workflow_preset_name_limit"]
        assert sanitized("x" * limit, limit=limit) == "x" * limit
        assert sanitized("x" * (limit + 1), limit=limit) != "x" * (limit + 1)

    @pytest.mark.asyncio
    async def test_the_engine_floor_is_projected_apart_from_the_bindable_names(
        self, recorded_sel: RecordedSel, enabled: None, home: Path
    ) -> None:
        """The floor travels for the opposite reason to the rest of the
        vocabulary: those names are what a surface must NOT offer a binding
        control for. Naming one in ``capabilities`` is a refusal rather than an
        ignored key, so a surface that could not tell the two lists apart would
        leave the refusal to be discovered by provoking it."""
        async with _client() as client:
            reply = await _get(client, f"{routes.PREFIX}/config/registry")
        assert reply.body["engine_floor"] == list(ENGINE_FLOOR_CAPABILITIES)
        assert set(reply.body["engine_floor"]).isdisjoint(reply.body["capabilities"])

    @pytest.mark.asyncio
    async def test_each_gate_preset_carries_the_entry_a_copy_is_made_from(
        self, recorded_sel: RecordedSel, enabled: None, home: Path
    ) -> None:
        """Whole entries rather than names, for the reason the source and profile
        presets carry theirs: a form adds a gate as a COPY of one, and a client
        holding only names would have to invent the argv it claims to have
        copied."""
        async with _client() as client:
            reply = await _get(client, f"{routes.PREFIX}/config/registry")
        assert reply.body["gate_presets"] == gate_presets()
        for preset in reply.body["gate_presets"]:
            assert preset["position"] in reply.body["gate_positions"]
            assert preset["severity"] in reply.body["gate_severities"]

    @pytest.mark.asyncio
    async def test_the_stages_partition_every_setting_group_and_capability(
        self, recorded_sel: RecordedSel, enabled: None, home: Path
    ) -> None:
        """Property 1 as the payload states it: nothing dropped, duplicated, or
        invented. A group missing from every stage is a setting the write door
        still enforces and no panel can reach; a group in two stages is a row an
        operator can edit from two places with two staged values."""
        async with _client() as client:
            reply = await _get(client, f"{routes.PREFIX}/config/registry")
        assert [stage["id"] for stage in reply.body["stages"]] == list(PIPELINE_STAGES)
        groups = [group for stage in reply.body["stages"] for group in stage["setting_groups"]]
        capabilities = [name for stage in reply.body["stages"] for name in stage["capabilities"]]
        assert sorted(groups) == sorted(set(groups)), f"a group reached two stages: {groups}"
        assert sorted(groups) == sorted({setting.group for setting in SETTINGS.values()})
        assert sorted(capabilities) == sorted(set(capabilities))
        assert sorted(capabilities) == sorted(DELEGABLE_CAPABILITIES)
        # Each placement is the engine's own answer, not a second derivation here.
        for stage in reply.body["stages"]:
            for group in stage["setting_groups"]:
                assert setting_group_stage(group) == stage["id"]
            for name in stage["capabilities"]:
                assert capability_stage(name) == stage["id"]

    @pytest.mark.asyncio
    async def test_each_stage_orders_its_groups_by_the_setting_registry(
        self, recorded_sel: RecordedSel, enabled: None, home: Path
    ) -> None:
        """``SETTING_GROUPS`` is a frozenset. A payload ordered by it would move a
        stage's rows between two reads while nothing had changed, which is why the
        order is taken from registry declaration order instead."""
        async with _client() as client:
            reply = await _get(client, f"{routes.PREFIX}/config/registry")
        for stage in reply.body["stages"]:
            positions = [SETTING_GROUP_ORDER.index(group) for group in stage["setting_groups"]]
            assert positions == sorted(positions), f"{stage['id']} reordered its groups"

    @pytest.mark.asyncio
    async def test_the_stage_ids_are_not_the_autonomy_ladder(
        self, recorded_sel: RecordedSel, enabled: None, home: Path
    ) -> None:
        """Three names appear in both vocabularies and mean different things: a
        pipeline stage is where a knob applies, an autonomy level is how much
        authority a run holds. Both travel in one payload, so the payload is where
        the distinction is worth pinning."""
        async with _client() as client:
            reply = await _get(client, f"{routes.PREFIX}/config/registry")
        stage_ids = [stage["id"] for stage in reply.body["stages"]]
        assert stage_ids != reply.body["levels"]
        assert PIPELINE_STAGE_ADVANCED in stage_ids
        assert PIPELINE_STAGE_ADVANCED not in reply.body["levels"]

    @settings(
        max_examples=40,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    @given(
        invented_groups=st.lists(st.text(min_size=1, max_size=8), max_size=4, unique=True),
        invented_capabilities=st.lists(st.text(min_size=1, max_size=8), max_size=4, unique=True),
    )
    def test_a_vocabulary_the_engine_grows_later_still_partitions_into_stages(
        self,
        monkeypatch: pytest.MonkeyPatch,
        invented_groups: list[str],
        invented_capabilities: list[str],
    ) -> None:
        """The half of Property 1 that has to hold for a vocabulary this table has
        never seen. A setting group or capability the engine adds without placing
        it must still reach exactly one stage — the advanced one — because the
        alternatives are a projection that raises (taking the whole vocabulary
        read down over one unplaced name) and one that drops it (a control the
        write door still enforces and no panel offers).

        Composed directly rather than over HTTP: the claim is about the projection,
        and the payload is assembled in memory with no request state in it.
        """
        assume(not set(invented_groups) & set(SETTING_GROUP_ORDER))
        assume(not set(invented_capabilities) & set(DELEGABLE_CAPABILITIES))
        monkeypatch.setattr(
            pipeline,
            "SETTING_GROUP_ORDER",
            (*SETTING_GROUP_ORDER, *invented_groups),
        )
        monkeypatch.setattr(
            pipeline,
            "DELEGABLE_CAPABILITIES",
            (*DELEGABLE_CAPABILITIES, *invented_capabilities),
        )
        stages = routes._registry_payload()["stages"]

        placed_groups = [group for stage in stages for group in stage["setting_groups"]]
        placed_names = [name for stage in stages for name in stage["capabilities"]]
        assert sorted(placed_groups) == sorted(set(placed_groups))
        assert sorted(placed_names) == sorted(set(placed_names))
        assert set(placed_groups) == {*SETTING_GROUP_ORDER, *invented_groups}
        assert set(placed_names) == {*DELEGABLE_CAPABILITIES, *invented_capabilities}
        advanced = next(stage for stage in stages if stage["id"] == PIPELINE_STAGE_ADVANCED)
        assert set(invented_groups) <= set(advanced["setting_groups"])
        assert set(invented_capabilities) <= set(advanced["capabilities"])

    @pytest.mark.asyncio
    async def test_the_read_opens_no_configuration_document(
        self, recorded_sel: RecordedSel, enabled: None, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The sibling reads count ONE document read; this one must count zero.

        A projection that reached for the document would inherit refusals it has no
        answer for — an unreadable file would take the form vocabulary down with
        it, leaving an operator a pane that cannot even describe its own fields.
        Both the store construction and the document read are counted, so a read
        through a second store is caught too.
        """
        reads: list[str] = []
        original_document = ConfigStore.document
        original_store = routes._config_store

        def _counting_document(self: ConfigStore) -> dict[str, Any]:
            reads.append("document")
            return original_document(self)

        def _counting_store() -> ConfigStore:
            reads.append("store")
            return original_store()

        monkeypatch.setattr(ConfigStore, "document", _counting_document)
        monkeypatch.setattr(routes, "_config_store", _counting_store)
        async with _client() as client:
            reply = await _get(client, f"{routes.PREFIX}/config/registry")
        assert reply.status == 200
        assert reads == [], f"the vocabulary read touched the config store: {reads}"
        # Every projected key named, so a vocabulary added later has to be added
        # here too — and is then shown to have been COMPOSED under the counting
        # wrappers rather than merely to exist in some other read's payload.
        projected = (
            "settings",
            "source_presets",
            "profile_presets",
            "profile_settings",
            "roles",
            "efforts",
            "levels",
            "transports",
            "delivery_stages",
            "gate_positions",
            "gate_severities",
            "capabilities",
            "engine_floor",
            "workflow_presets",
            "workflow_preset_name_limit",
            "gate_presets",
            "stages",
        )
        assert sorted(reply.body) == sorted(projected), (
            "the vocabulary read gained or lost a key; add it above so the "
            "zero-document-read pin covers it too"
        )
        for key in projected:
            assert reply.body[key], f"{key} arrived empty from a read that opened no document"

    @pytest.mark.asyncio
    async def test_an_unconfigured_home_still_answers_the_whole_vocabulary(
        self, recorded_sel: RecordedSel, enabled: None, home: Path
    ) -> None:
        """The case the forms are for. Nothing is configured yet — no document at
        all — and the pane still has every field, preset and role it needs to offer
        the operator a first edit."""
        assert not (default_root() / CONFIG_FILENAME).exists()
        async with _client() as client:
            reply = await _get(client, f"{routes.PREFIX}/config/registry")
        assert reply.status == 200
        assert reply.body["settings"]
        assert reply.body["source_presets"]
        assert reply.body["profile_presets"]
        # Including the extension seams: an operator whose document is empty is
        # exactly the operator who has never bound a capability, defined a
        # workflow, or added a gate, and the forms for those must still offer
        # something.
        assert reply.body["capabilities"] == list(DELEGABLE_CAPABILITIES)
        assert reply.body["gate_presets"]
        assert reply.body["workflow_presets"]
        assert [stage["id"] for stage in reply.body["stages"]] == list(PIPELINE_STAGES)

    @pytest.mark.asyncio
    async def test_two_reads_return_the_identical_payload(
        self, recorded_sel: RecordedSel, enabled: None, home: Path
    ) -> None:
        """Stable ordering, asserted as bytes. A payload ordered by set iteration
        would reorder a form's rows between reads while nothing had changed."""
        async with _client() as client:
            first = await _get(client, f"{routes.PREFIX}/config/registry")
            second = await _get(client, f"{routes.PREFIX}/config/registry")
        assert json.dumps(first.body) == json.dumps(second.body)


# --- the per-source autonomy grid --------------------------------------------


#: A source entry the schema accepts. ``poll`` is required, so a grid cannot be
#: written without one.
def _source(grid: Any = None) -> dict[str, Any]:
    entry: dict[str, Any] = {"poll": ["watch", "issues"]}
    if grid is not None:
        entry["autonomy"] = grid
    return entry


def _write_document(document: dict[str, Any]) -> None:
    """Put *document* on disk verbatim, bypassing the write path's validation.

    For the shapes the write path REFUSES: a hand-edited grid is exactly what the
    malformed-grid arm exists for, and it cannot be produced through the door.
    """
    config_dir = default_root()
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / CONFIG_FILENAME).write_text(json.dumps(document), encoding="utf-8")


def _cell(body: Any, source: str, submitter_class: str, spec_type: str) -> dict[str, Any]:
    entries = {entry["name"]: entry for entry in body["sources"]}
    assert source in entries, f"{source} is missing from {sorted(entries)}"
    return dict(entries[source]["grid"][submitter_class][spec_type])


class TestTheSourcesReadResolvesEveryCellThroughTheEngine:
    """The autonomy grid, matrix by matrix, resolved by the policy the gates use.

    The claim worth defending is not that the JSON has the right shape: it is that
    a cell an operator reads is the cell a RUN would resolve. So these drive the
    real route against a real document and assert on the level, the declaring path
    and the origin together — a matrix that resolved correctly but attributed a
    wildcard cell to the pair itself would tell an operator they had written a rule
    they had not, and the next edit would be made on that belief.

    The 401 floor and the disabled-app refusal are not repeated here: both are
    parametrized over the route table, so registering this path enrolled it.
    """

    @pytest.mark.asyncio
    async def test_a_stored_cell_reports_its_own_path_as_an_exact_origin(
        self, recorded_sel: RecordedSel, enabled: None, home: Path
    ) -> None:
        async with _client() as client:
            written = await _put(
                client,
                f"{routes.PREFIX}/config",
                {"patch": {"sources": {"gh": _source({"maintainer": {"feature": "delivery"}})}}},
            )
            assert written.status == 200, written.body
            reply = await _get(client, f"{routes.PREFIX}/config/sources")
        assert reply.status == 200
        cell = _cell(reply.body, "gh", "maintainer", "feature")
        assert cell == {
            "level": "delivery",
            "declared_at": "sources.gh.autonomy.maintainer.feature",
            "origin": "exact",
            # delivery is above execution, and an enabled rung implies every rung
            # below it, so the policy covers the document gates.
            "policy_covers_gates": True,
        }
        # The pair the operator wrote nothing for is untouched by that cell.
        assert _cell(reply.body, "gh", "maintainer", "bugfix")["origin"] == "default"

    @pytest.mark.asyncio
    async def test_a_wildcard_row_answers_every_spec_type_and_says_it_was_a_wildcard(
        self, recorded_sel: RecordedSel, enabled: None, home: Path
    ) -> None:
        """The origin an edit depends on: a cell answered by a wildcard must be
        narrowed rather than overwritten, and the surface can only offer that if
        the read distinguishes the two."""
        async with _client() as client:
            written = await _put(
                client,
                f"{routes.PREFIX}/config",
                {
                    "patch": {
                        "sources": {"gh": _source({"contributor": {WILDCARD_KEY: "execution"}})}
                    }
                },
            )
            assert written.status == 200, written.body
            reply = await _get(client, f"{routes.PREFIX}/config/sources")
        for spec_type in SPEC_TYPES:
            cell = _cell(reply.body, "gh", "contributor", spec_type)
            assert cell["level"] == "execution"
            assert cell["origin"] == "wildcard"
            assert cell["declared_at"] == f"sources.gh.autonomy.contributor.{WILDCARD_KEY}"
            assert cell["policy_covers_gates"] is True
        # Class-first precedence is the engine's, and the read inherits it: a row
        # written for one class says nothing about another.
        assert _cell(reply.body, "gh", "external", "feature")["origin"] == "default"

    @pytest.mark.asyncio
    async def test_the_least_trusted_class_defaults_to_a_cell_that_covers_no_gate(
        self, recorded_sel: RecordedSel, enabled: None, home: Path
    ) -> None:
        """The fail-closed case, reported as a decision rather than as a blank."""
        async with _client() as client:
            await _put(
                client,
                f"{routes.PREFIX}/config",
                {"patch": {"sources": {"gh": _source({"maintainer": {"feature": "integration"}})}}},
            )
            reply = await _get(client, f"{routes.PREFIX}/config/sources")
        cell = _cell(reply.body, "gh", LEAST_TRUSTED_CLASS, "feature")
        assert cell == {
            "level": "authoring",
            "declared_at": "",
            "origin": "default",
            "policy_covers_gates": False,
        }

    @pytest.mark.asyncio
    @pytest.mark.parametrize("grid", [None, {}], ids=["absent", "empty"])
    async def test_a_source_with_no_grid_is_listed_with_an_all_default_matrix(
        self, grid: Any, recorded_sel: RecordedSel, enabled: None, home: Path
    ) -> None:
        """Listed, not skipped. A configured source nobody wrote a grid for is the
        fail-closed case an operator most needs to see, and omitting it would read
        as "this source is not configured"."""
        async with _client() as client:
            written = await _put(
                client,
                f"{routes.PREFIX}/config",
                {"patch": {"sources": {"gh": _source(grid)}}},
            )
            assert written.status == 200, written.body
            reply = await _get(client, f"{routes.PREFIX}/config/sources")
        assert [entry["name"] for entry in reply.body["sources"]] == ["gh"]
        matrix = reply.body["sources"][0]["grid"]
        assert set(matrix) == set(SUBMITTER_CLASSES)
        for submitter_class in SUBMITTER_CLASSES:
            assert set(matrix[submitter_class]) == set(SPEC_TYPES)
            for cell in matrix[submitter_class].values():
                assert cell["origin"] == "default"
                assert cell["level"] == "authoring"
                assert cell["policy_covers_gates"] is False

    @pytest.mark.asyncio
    async def test_a_document_with_no_sources_reports_none_rather_than_an_axis_only_matrix(
        self, recorded_sel: RecordedSel, enabled: None, home: Path
    ) -> None:
        async with _client() as client:
            reply = await _get(client, f"{routes.PREFIX}/config/sources")
        assert reply.status == 200
        assert reply.body["sources"] == []
        # The vocabularies still travel: the surface needs them to say what it is
        # not showing.
        assert reply.body["submitter_classes"] == list(SUBMITTER_CLASSES)

    @pytest.mark.asyncio
    async def test_the_axes_are_the_engines_own_vocabularies(
        self, recorded_sel: RecordedSel, enabled: None, home: Path
    ) -> None:
        """Shipped rather than hard-coded downstream, so a schema change shows up
        in the surface without a client edit — and in the schema's own ORDER,
        which is meaningful: submitter classes run least trusted last and levels
        run least autonomous first."""
        async with _client() as client:
            reply = await _get(client, f"{routes.PREFIX}/config/sources")
        assert reply.body["submitter_classes"] == list(SUBMITTER_CLASSES)
        assert reply.body["spec_types"] == list(SPEC_TYPES)
        assert reply.body["levels"] == list(AUTONOMY_LEVELS)
        assert reply.body["submitter_classes"][-1] == LEAST_TRUSTED_CLASS

    @pytest.mark.asyncio
    async def test_several_sources_are_listed_in_a_stable_order(
        self, recorded_sel: RecordedSel, enabled: None, home: Path
    ) -> None:
        async with _client() as client:
            await _put(
                client,
                f"{routes.PREFIX}/config",
                {
                    "patch": {
                        "sources": {
                            "zeta": _source({"member": {"quick": "execution"}}),
                            "alpha": _source(),
                        }
                    }
                },
            )
            reply = await _get(client, f"{routes.PREFIX}/config/sources")
        assert [entry["name"] for entry in reply.body["sources"]] == ["alpha", "zeta"]
        # One source's grid never answers another's cell.
        assert _cell(reply.body, "alpha", "member", "quick")["origin"] == "default"
        assert _cell(reply.body, "zeta", "member", "quick")["origin"] == "exact"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("grid", "expected_path"),
        [
            ("execution", "sources.gh.autonomy"),
            ({"external": "execution"}, "sources.gh.autonomy.external"),
            ({"external": {"feature": "root"}}, "sources.gh.autonomy.external.feature"),
        ],
        ids=["grid-not-an-object", "row-not-an-object", "level-off-the-ladder"],
    )
    async def test_a_malformed_stored_grid_is_refused_by_path_not_rendered(
        self,
        grid: Any,
        expected_path: str,
        recorded_sel: RecordedSel,
        enabled: None,
        home: Path,
    ) -> None:
        """Resolution RAISES on each of these rather than falling through to a
        broader cell, and the route keeps that: a partial matrix would show an
        operator authority the engine would refuse to act on. Written to disk
        directly, because the write path would have refused all three."""
        _write_document({"version": 1, "sources": {"gh": _source(grid)}})
        async with _client() as client:
            reply = await _get(client, f"{routes.PREFIX}/config/sources")
        assert reply.status == 422
        assert reply.code == "config_invalid"
        assert expected_path in reply.body["error"]
        assert "sources" not in reply.body, "a refused read must carry no values"

    @pytest.mark.asyncio
    async def test_an_unparseable_document_is_reported_not_read_as_no_sources(
        self, recorded_sel: RecordedSel, enabled: None, home: Path
    ) -> None:
        config_dir = default_root()
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / CONFIG_FILENAME).write_text("{ not json", encoding="utf-8")
        async with _client() as client:
            reply = await _get(client, f"{routes.PREFIX}/config/sources")
        assert reply.status == 409
        assert reply.code == "config_unreadable"

    @pytest.mark.asyncio
    async def test_the_whole_matrix_is_built_from_one_read_of_the_document(
        self, recorded_sel: RecordedSel, enabled: None, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Twelve resolutions per source against a live store would each re-read
        the file, so a write landing mid-reply would produce a matrix describing
        two different documents — with the two halves disagreeing about who may
        run unattended and nothing saying so."""
        reads: list[int] = []
        original = ConfigStore.document

        def _counting(self: ConfigStore) -> dict[str, Any]:
            reads.append(1)
            return original(self)

        _write_document(
            {
                "version": 1,
                "sources": {
                    "gh": _source({"maintainer": {"feature": "delivery"}}),
                    "gl": _source({WILDCARD_KEY: {WILDCARD_KEY: "execution"}}),
                },
            }
        )
        monkeypatch.setattr(ConfigStore, "document", _counting)
        async with _client() as client:
            reply = await _get(client, f"{routes.PREFIX}/config/sources")
        assert reply.status == 200
        assert sum(reads) == 1, f"the sources read read the document {sum(reads)} times"


class TestTheOriginOfACellIsClassifiedFromTheDeclaringPath:
    """Unit-level, against the real ``AutonomyDecision`` shape.

    Origin is the field an edit is built on — a wildcard-answered cell is narrowed
    rather than overwritten — so it is classified here from the decision the
    resolver returns rather than inferred from the raw grid.
    """

    @staticmethod
    def _decision(declared_at: str, *, source: str = "gh") -> AutonomyDecision:
        return AutonomyDecision(
            level=AutonomyLevel.EXECUTION,
            source=source,
            spec_type="feature",
            submitter_class="external",
            declared_at=declared_at,
        )

    def test_no_declaration_is_the_unconfigured_default(self) -> None:
        assert routes._cell_origin(self._decision("")) == routes.ORIGIN_DEFAULT

    def test_the_pairs_own_cell_is_exact(self) -> None:
        assert (
            routes._cell_origin(self._decision("sources.gh.autonomy.external.feature"))
            == routes.ORIGIN_EXACT
        )

    @pytest.mark.parametrize(
        "declared_at",
        [
            f"sources.gh.autonomy.external.{WILDCARD_KEY}",
            f"sources.gh.autonomy.{WILDCARD_KEY}.feature",
            f"sources.gh.autonomy.{WILDCARD_KEY}.{WILDCARD_KEY}",
        ],
        ids=["type-wildcard", "class-wildcard", "both-wildcard"],
    )
    def test_a_broader_cell_is_a_wildcard(self, declared_at: str) -> None:
        assert routes._cell_origin(self._decision(declared_at)) == routes.ORIGIN_WILDCARD

    def test_a_source_name_holding_a_dot_still_classifies_its_own_cell_as_exact(self) -> None:
        """Whole-path comparison rather than a segment split.

        A source may legitimately be named ``gh.issues``, and a classifier that
        split the dotted path would read ``issues`` as the submitter class — then
        report every exact cell of that source as a wildcard, and the UI would
        offer to narrow a cell that is already as narrow as it gets.
        """
        decision = self._decision("sources.gh.issues.autonomy.external.feature", source="gh.issues")
        assert routes._cell_origin(decision) == routes.ORIGIN_EXACT


# --- the setup flow ----------------------------------------------------------


def _project_tree(root: Path) -> Path:
    """A project whose own files state a GitHub remote and a build entry point."""
    root.mkdir(parents=True, exist_ok=True)
    git = root / ".git"
    git.mkdir()
    (git / "config").write_text(
        '[core]\n\trepositoryformatversion = 0\n[remote "origin"]\n'
        "\turl = git@github.com:acme/widgets.git\n",
        encoding="utf-8",
    )
    (root / "Makefile").write_text("build:\n\t@echo build\n\ntest:\n\t@echo test\n", "utf-8")
    return root


def _answers(inspection: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    """A complete, consistent answer set for an inspected project."""
    answers: dict[str, Any] = {
        "cost_profile": "budget",
        "confirmations": {level.value: False for level in CONFIRMED_LEVELS},
        "approved_subjects": [item["subject"] for item in inspection["inferences"]],
        "workflow_preset": "git-pull-request",
        "watch_source": "github",
    }
    answers.update(overrides)
    return answers


async def _inspect(client: TestClient, project: Path) -> dict[str, Any]:
    reply = await _post(client, f"{routes.PREFIX}/setup/inspect", {"project": str(project)})
    assert reply.status == 200, reply.body
    assert isinstance(reply.body, dict)
    return reply.body


class TestTheSetupFlowDrivesInspectPlanApply:
    """Three calls, and nothing is written before the third.

    The flow the first-run pane walks: inspect a project, answer what could not be
    inferred, read the plan, and apply it under a named approver. Each step is a
    separate route because each is a separate decision, and the two that precede
    the write must be verifiable as having written nothing.
    """

    @pytest.mark.asyncio
    async def test_inspection_returns_evidence_inferences_and_questions_and_writes_nothing(
        self, recorded_sel: RecordedSel, enabled: None, home: Path, tmp_path: Path
    ) -> None:
        project = _project_tree(tmp_path / "acme")
        async with _client() as client:
            found = await _inspect(client, project)
        assert found["project"] == {"name": "acme", "root": str(project.resolve())}
        for inference in found["inferences"]:
            assert inference["evidence"], f"{inference['subject']} arrived without evidence"
        asked = {question["subject"] for question in found["questions"]}
        assert "cost_profile" in asked
        # Each offered preset names the programs it would run, so what an operator
        # approves is what would land in configuration.
        for offer in found["offers"]:
            assert offer["programs"] and offer["commands"]
        # The evidence that nothing was applied: no document at all.
        assert not (default_root() / CONFIG_FILENAME).exists()

    @pytest.mark.asyncio
    async def test_an_inspection_without_a_project_is_refused(
        self, recorded_sel: RecordedSel, enabled: None, home: Path
    ) -> None:
        async with _client() as client:
            reply = await _post(client, f"{routes.PREFIX}/setup/inspect", {})
        assert reply.status == 400
        assert reply.code == "field_required"

    @pytest.mark.asyncio
    async def test_a_project_path_with_no_final_segment_is_a_client_error(
        self, recorded_sel: RecordedSel, enabled: None, home: Path
    ) -> None:
        """A filesystem root has no directory name to fall back on, so the name is
        refused rather than chosen. A malformed call, not a decision."""
        async with _client() as client:
            reply = await _post(client, f"{routes.PREFIX}/setup/inspect", {"project": "/"})
        assert reply.status == 400
        assert reply.code == "bad_project"

    @pytest.mark.asyncio
    async def test_a_plan_is_returned_and_still_nothing_is_written(
        self, recorded_sel: RecordedSel, enabled: None, home: Path, tmp_path: Path
    ) -> None:
        project = _project_tree(tmp_path / "acme")
        async with _client() as client:
            found = await _inspect(client, project)
            planned = await _post(
                client,
                f"{routes.PREFIX}/setup/plan",
                {"project": str(project), "answers": _answers(found)},
            )
        assert planned.status == 200, planned.body
        assert planned.body["plan_id"]
        # The patch itself, not a summary: an approval given against a summary is an
        # approval of something else.
        assert planned.body["config_patch"]["cost_profiles"]["budget"]["roles"]
        assert planned.body["written_paths"]
        assert not (default_root() / CONFIG_FILENAME).exists()

    @pytest.mark.asyncio
    async def test_an_unanswered_rung_is_refused_before_a_plan_exists(
        self, recorded_sel: RecordedSel, enabled: None, home: Path, tmp_path: Path
    ) -> None:
        project = _project_tree(tmp_path / "acme")
        async with _client() as client:
            found = await _inspect(client, project)
            refused = await _post(
                client,
                f"{routes.PREFIX}/setup/plan",
                {
                    "project": str(project),
                    "answers": _answers(found, confirmations={"execution": True}),
                },
            )
        assert refused.status == 409
        assert refused.code == "setup_refused"
        assert refused.body[REFUSED_KEY] == REFUSAL_SETUP_APPROVAL
        # A missing rung is unanswered, not "no", and the refusal names it.
        assert "delivery" in refused.body["message"]
        assert "plan_id" not in refused.body

    @pytest.mark.asyncio
    async def test_an_unknown_cost_profile_is_a_client_error_not_a_refusal(
        self, recorded_sel: RecordedSel, enabled: None, home: Path, tmp_path: Path
    ) -> None:
        """Not a decision the operator has to make — a call the caller got wrong —
        so it names which profiles exist rather than coming back as something a
        caller might retry unchanged."""
        project = _project_tree(tmp_path / "acme")
        async with _client() as client:
            found = await _inspect(client, project)
            reply = await _post(
                client,
                f"{routes.PREFIX}/setup/plan",
                {"project": str(project), "answers": _answers(found, cost_profile="cheap")},
            )
        assert reply.status == 400
        assert reply.code == "bad_answers"
        for name in COST_PROFILE_PRESET_NAMES:
            assert name in reply.body["error"]

    @pytest.mark.asyncio
    async def test_answers_must_be_an_object(
        self, recorded_sel: RecordedSel, enabled: None, home: Path, tmp_path: Path
    ) -> None:
        """An empty answer set is a real thing to send; an absent one is a bug that
        must not be read as "the operator answered nothing"."""
        async with _client() as client:
            reply = await _post(
                client, f"{routes.PREFIX}/setup/plan", {"project": str(tmp_path), "answers": None}
            )
        assert reply.status == 400
        assert reply.code == "bad_answers"

    @pytest.mark.asyncio
    async def test_an_apply_without_an_approver_refuses_and_writes_nothing(
        self, recorded_sel: RecordedSel, enabled: None, home: Path, tmp_path: Path
    ) -> None:
        """The requirement's own words: an explicit human approver identity, or no
        write. Refused before the project is even read."""
        project = _project_tree(tmp_path / "acme")
        async with _client() as client:
            found = await _inspect(client, project)
            answers = _answers(found)
            planned = await _post(
                client,
                f"{routes.PREFIX}/setup/plan",
                {"project": str(project), "answers": answers},
            )
            refused = await _post(
                client,
                f"{routes.PREFIX}/setup/apply",
                {
                    "project": str(project),
                    "answers": answers,
                    "plan_id": planned.body["plan_id"],
                    "approver": "   ",
                },
            )
        assert refused.status == 409
        assert refused.code == "setup_refused"
        assert refused.body[REFUSED_KEY] == REFUSAL_APPROVER_REQUIRED
        assert not (default_root() / CONFIG_FILENAME).exists()

    @pytest.mark.asyncio
    async def test_a_stale_plan_id_refuses_and_writes_nothing(
        self, recorded_sel: RecordedSel, enabled: None, home: Path, tmp_path: Path
    ) -> None:
        project = _project_tree(tmp_path / "acme")
        async with _client() as client:
            found = await _inspect(client, project)
            answers = _answers(found)
            planned = await _post(
                client,
                f"{routes.PREFIX}/setup/plan",
                {"project": str(project), "answers": answers},
            )
            # The plan the operator read, applied with DIFFERENT answers: the
            # identity covers the answers, so the quoted id no longer identifies
            # the plan these inputs produce.
            refused = await _post(
                client,
                f"{routes.PREFIX}/setup/apply",
                {
                    "project": str(project),
                    "answers": _answers(found, cost_profile="quality-first"),
                    "plan_id": planned.body["plan_id"],
                    "approver": APPROVER,
                },
            )
        assert refused.status == 409
        assert refused.body[REFUSED_KEY] == REFUSAL_PLAN_STALE
        assert not (default_root() / CONFIG_FILENAME).exists()

    @pytest.mark.asyncio
    async def test_an_approved_plan_lands_and_records_the_approver(
        self, recorded_sel: RecordedSel, enabled: None, home: Path, tmp_path: Path
    ) -> None:
        project = _project_tree(tmp_path / "acme")
        async with _client(user="alice") as client:
            found = await _inspect(client, project)
            answers = _answers(found)
            planned = await _post(
                client,
                f"{routes.PREFIX}/setup/plan",
                {"project": str(project), "answers": answers},
            )
            applied = await _post(
                client,
                f"{routes.PREFIX}/setup/apply",
                {
                    "project": str(project),
                    "answers": answers,
                    "plan_id": planned.body["plan_id"],
                    "approver": APPROVER,
                },
            )
            assert applied.status == 200, applied.body
            configured = await _get(client, f"{routes.PREFIX}/config")

        assert applied.body["applied"] is True
        assert applied.body["approver"] == APPROVER
        assert applied.body["written_paths"]
        # Read back from the document rather than trusted from the reply.
        assert configured.body["configured"] is True
        assert configured.body["document"]["projects"]["acme"]["cost_profile"] == "budget"

        # The durable record of who authorized it. The approver is the identity the
        # caller stated; the SESSION is what the guard verified, and it is recorded
        # in the security event instead — the two answer different questions and a
        # record holding one of them answers the wrong one.
        store = ConfigStore(Path(configured.body["path"]).parent)
        assert [record.get("actor") for record in store.writes()] == [APPROVER]
        assert [record.get("surface") for record in store.writes()] == [routes.SETUP_SURFACE.name]
        event = next(
            item for item in recorded_sel.events if item["operation"] == "spec_engine_setup_apply"
        )
        assert event["caller"] == "alice"
        assert APPROVER in event["resources"]

    def test_the_setup_surface_is_the_engines_own_and_is_operator_confirmed(self) -> None:
        """One surface name across both doors, and confirmed — which a setup patch
        needs, because it necessarily touches config-only paths (a project's
        workflow, a source's autonomy grid)."""
        from kiro_crew.apps.builtins.spec_engine.engine.config.store import (
            SETUP_ASSISTANT_SURFACE,
        )

        assert routes.SETUP_SURFACE is SETUP_ASSISTANT_SURFACE
        assert routes.SETUP_SURFACE.operator_confirmed is True
        # And it is NOT the config route's surface: the two writes are recorded
        # under different names because they carry different authority.
        assert routes.SETUP_SURFACE.name != routes.WRITE_SURFACE.name


class TestBothDoorsIdentifyOnePlan:
    """The plan identity is one mechanism, not one per door.

    ``plan_id`` is a content hash over the project subject, the answers used and
    the patch they produce, and both this surface and the Engine_MCP_Server compute
    it through ``engine_mcp/setup_surface.py``. If either door normalized the
    project root or defaulted the project name differently, an operator could read
    a plan through one and have the other refuse it as stale for a reason nothing
    on screen explains.
    """

    def _ops(self, root: Path) -> EngineOperations:
        return EngineOperations(
            state_root=root / "state", audit_root=root / "audit", config_root=root / "config"
        )

    @pytest.mark.parametrize(
        "spelling",
        [
            "{project}",
            # Two spellings of one path, which must not identify two plans.
            "{project}/",
            "{project}/./",
        ],
    )
    def test_the_route_and_the_tool_compute_the_same_identity(
        self, tmp_path: Path, spelling: str
    ) -> None:
        project = _project_tree(tmp_path / "acme")
        engine = self._ops(tmp_path)
        found = engine.inspect_setup(str(project))
        answers = _answers(found)
        through_tool = engine.plan_setup(str(project), answers)["plan_id"]
        through_route = routes._plan_setup({"project": spelling.format(project=project)}, answers)[
            "plan_id"
        ]
        assert through_route == through_tool

    @settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(
        profile=st.sampled_from(COST_PROFILE_PRESET_NAMES),
        granted=st.integers(min_value=0, max_value=len(CONFIRMED_LEVELS)),
        workflow=st.booleans(),
        source=st.booleans(),
    )
    def test_every_legitimate_answer_set_identifies_one_plan_at_both_doors(
        self,
        tmp_path_factory: pytest.TempPathFactory,
        profile: str,
        granted: int,
        workflow: bool,
        source: bool,
    ) -> None:
        # Over the answer sets a caller can legitimately submit: a bundled profile,
        # a ladder prefix of confirmations (a rung confirmed above a declined one is
        # refused), and either preset selected or not.
        root = tmp_path_factory.mktemp("identity")
        project = _project_tree(root / "acme")
        engine = self._ops(root)
        found = engine.inspect_setup(str(project))
        answers = _answers(
            found,
            cost_profile=profile,
            confirmations={
                level.value: index < granted for index, level in enumerate(CONFIRMED_LEVELS)
            },
            workflow_preset="git-pull-request" if workflow else None,
            watch_source="github" if source else None,
        )
        through_tool = engine.plan_setup(str(project), answers)["plan_id"]
        through_route = routes._plan_setup({"project": str(project)}, answers)["plan_id"]
        assert through_route == through_tool

    @settings(
        max_examples=40,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
    )
    @given(supplied=st.text(max_size=70))
    def test_a_stale_plan_id_always_refuses_and_never_writes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, supplied: str
    ) -> None:
        # Whatever a caller quotes, it either is the identity these inputs produce
        # or the apply refuses. A fresh data home per example, so "nothing was
        # written" is a claim about THIS call rather than about a directory an
        # earlier example happened to leave empty.
        root = tmp_path / f"stale-{next(_UNIQUE)}"
        root.mkdir(parents=True)
        monkeypatch.setenv("KIROCREW_HOME", str(root))
        project = _project_tree(root / "acme")
        found = routes._inspect_setup({"project": str(project)})
        answers = _answers(found)
        real = routes._plan_setup({"project": str(project)}, answers)["plan_id"]
        assume(supplied.strip() != real)

        with pytest.raises(StalePlan) as raised:
            routes._apply_setup({"project": str(project)}, answers, supplied, APPROVER)
        assert "plan_id" in str(raised.value)
        assert not (default_root() / CONFIG_FILENAME).exists()


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

    def test_the_setup_refusal_chain_is_what_the_setup_handlers_assume(self) -> None:
        """The same trap in a second shape, and the reason the refusal arm is FIRST
        on every setup handler.

        ``SetupApprovalRequired`` derives ``PermissionError`` and therefore
        ``OSError``; ``InferredSubjectRefused`` derives ``ValueError``. Below the
        generic arms, an absent approver would be reported as a disk failure and a
        refused inference as a malformed request — and the two boundary refusals
        are subclasses of the engine's precisely so that every catch which already
        declines to write keeps declining.
        """
        from kiro_crew.apps.builtins.spec_engine.engine.setup import (
            InferredSubjectRefused,
            SetupApprovalRequired,
        )
        from kiro_crew.apps.builtins.spec_engine.engine_mcp.setup_surface import (
            ApproverRequired,
        )

        assert issubclass(SetupApprovalRequired, PermissionError)
        assert issubclass(SetupApprovalRequired, OSError)
        assert issubclass(InferredSubjectRefused, ValueError)
        assert issubclass(ApproverRequired, SetupApprovalRequired)
        assert issubclass(StalePlan, SetupApprovalRequired)

    @pytest.mark.parametrize(
        "handler",
        [
            "handle_post_setup_inspect",
            "handle_post_setup_plan",
            "handle_post_setup_apply",
        ],
    )
    def test_every_setup_handler_catches_the_refusal_before_the_generic_arms(
        self, handler: str
    ) -> None:
        """Read off the source, because the ORDER is the property. A handler whose
        refusal arm sank below ``OSError`` would still catch — as the wrong thing."""
        node = _handler_ast(handler)
        caught = _caught_names(node)
        assert {"InferredSubjectRefused", "SetupApprovalRequired"} <= caught
        order = [
            name
            for clause in ast.walk(node)
            if isinstance(clause, ast.ExceptHandler) and clause.type is not None
            for element in (
                clause.type.elts if isinstance(clause.type, ast.Tuple) else [clause.type]
            )
            for name in [
                element.id if isinstance(element, ast.Name) else getattr(element, "attr", "")
            ]
        ]
        refusal = order.index("SetupApprovalRequired")
        for generic in ("OSError", "ValueError"):
            if generic in order:
                assert refusal < order.index(generic), (
                    f"{handler} catches {generic} before the setup refusal, so a decision "
                    "the engine made would be reported as a failure"
                )

    @pytest.mark.asyncio
    async def test_a_setup_refusal_is_a_409_carrying_the_engines_own_code(
        self, recorded_sel: RecordedSel, enabled: None, home: Path, tmp_path: Path
    ) -> None:
        """The arm, driven. One status for every setup refusal, with the actionable
        part in ``refused`` — the same vocabulary the MCP tools return."""
        async with _client() as client:
            reply = await _post(
                client,
                f"{routes.PREFIX}/setup/apply",
                {
                    "project": str(tmp_path),
                    "answers": {},
                    "plan_id": "0" * 64,
                    "approver": "",
                },
            )
        assert reply.status == 409
        assert reply.code == "setup_refused"
        assert reply.body[REFUSED_KEY] == REFUSAL_APPROVER_REQUIRED
        assert reply.body["reason"] == "ApproverRequired"

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


# --- the delivery workflow and its gates ------------------------------------


def _function_ast(name: str) -> ast.FunctionDef:
    """A plain (non-async) module-level function in :mod:`routes`, as source.

    The sibling of :func:`_handler_ast`, which finds handlers only: the claim
    below is about a blocking helper rather than about a coroutine.
    """
    for node in _module_ast().body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"no function named {name} in {ROUTES_SOURCE}")


def _function_body_source(node: ast.FunctionDef) -> str:
    """*node*'s source with its docstring removed.

    A structural claim about what a function DOES must not read what it SAYS: a
    docstring naming the thing it promises not to compute would fail an assertion
    for explaining itself.
    """
    body = [
        statement
        for statement in node.body
        if not (isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant))
    ]
    assert body, f"{node.name} has no statements outside its docstring"
    lines = ROUTES_SOURCE.read_text(encoding="utf-8").splitlines()
    last = body[-1].end_lineno or body[-1].lineno
    return "\n".join(lines[body[0].lineno - 1 : last])


def _stage_rows(body: Any) -> dict[str, dict[str, Any]]:
    """The workflow read's stage rows, keyed by stage name.

    Also the assertion that every declared stage appears: a surface listing only
    the stages that resolved would leave an operator inferring "runs nothing" from
    a stage's absence, which is the distinction the display exists to keep.
    """
    rows = {row["stage"]: row for row in body["stages"]}
    assert list(rows) == list(DELIVERY_STAGES), (
        "the workflow read must carry one row per declared stage, in schema order: "
        f"got {list(rows)}"
    )
    return rows


class TestTheWorkflowReadRelaysWhatTheEngineResolved:
    """Which layer supplied each stage's commands, and the gates beside it.

    The claim is not that the JSON has the right shape: it is that the route
    RELAYS the engine's own per-stage resolution instead of re-deriving it. So
    every case below drives a real document through the real route and asserts on
    the source, the declaring path and the commands together — a row that resolved
    the right commands while attributing them to the wrong layer would send an
    operator to edit a preset when the answer is a project override, and the next
    edit would be made on that belief.

    The 401 floor and the disabled-app refusal are not repeated here: both are
    parametrized over the route table, so registering the path enrolled it.
    """

    @pytest.mark.asyncio
    async def test_a_bundled_preset_is_named_as_bundled_with_its_stages(
        self, recorded_sel: RecordedSel, enabled: None, home: Path
    ) -> None:
        async with _client() as client:
            written = await _put(
                client,
                f"{routes.PREFIX}/config",
                {
                    "patch": {
                        "projects": {
                            "acme": {
                                "path": "/tmp/acme",
                                "workflow": {"preset": "git-pull-request"},
                            }
                        }
                    }
                },
            )
            assert written.status == 200, written.body
            reply = await _get(client, f"{routes.PREFIX}/config/workflow?project=acme")
        assert reply.status == 200, reply.body
        assert reply.body["project"] == "acme"
        assert reply.body["preset"] == {
            "name": "git-pull-request",
            "origin": "project_config",
            "declared_at": "projects.acme.workflow.preset",
            # The one field that separates engine-authored commands from
            # document-authored ones.
            "bundled": True,
        }
        submit = _stage_rows(reply.body)["submit"]
        assert submit["source"] == "bundled_preset"
        assert submit["from_preset"] is True
        assert submit["bundled"] is True
        assert submit["preset"] == "git-pull-request"
        # Byte-equal to the engine's own table, not a paraphrase of it.
        assert submit["argv"] == [
            list(argv) for argv in WORKFLOW_PRESETS["git-pull-request"]["submit"]
        ]
        assert submit["commands"] == len(WORKFLOW_PRESETS["git-pull-request"]["submit"])

    @pytest.mark.asyncio
    async def test_a_user_defined_preset_is_never_flattened_onto_a_bundled_one(
        self, recorded_sel: RecordedSel, enabled: None, home: Path
    ) -> None:
        """The invariant a bundled name is reserved to protect.

        A definition an operator wrote and one the engine ships call for opposite
        trust, so ``preset`` alone is not an answer: the source has to say which.
        """
        async with _client() as client:
            written = await _put(
                client,
                f"{routes.PREFIX}/config",
                {
                    "patch": {
                        "workflow": {
                            "presets": {"my-host": {"stages": {"submit": [["hg", "ci"]]}}}
                        },
                        "projects": {
                            "acme": {"path": "/tmp/acme", "workflow": {"preset": "my-host"}}
                        },
                    }
                },
            )
            assert written.status == 200, written.body
            reply = await _get(client, f"{routes.PREFIX}/config/workflow?project=acme")
        assert reply.status == 200, reply.body
        assert reply.body["preset"] == {
            "name": "my-host",
            "origin": "project_config",
            "declared_at": "projects.acme.workflow.preset",
            "bundled": False,
        }
        submit = _stage_rows(reply.body)["submit"]
        assert submit["source"] == "user_preset"
        assert submit["from_preset"] is True
        assert submit["bundled"] is False
        assert submit["argv"] == [["hg", "ci"]]
        assert submit["declared_at"] == "workflow.presets.my-host.stages.submit"
        # Offered for selection from the document, because the registry read
        # carries the bundled names only.
        assert reply.body["user_presets"] == ["my-host"]

    @pytest.mark.asyncio
    async def test_an_app_wide_stage_declaration_reports_as_an_app_override(
        self, recorded_sel: RecordedSel, enabled: None, home: Path
    ) -> None:
        async with _client() as client:
            written = await _put(
                client,
                f"{routes.PREFIX}/config",
                {"patch": {"workflow": {"stages": {"publish": [["make", "deploy"]]}}}},
            )
            assert written.status == 200, written.body
            reply = await _get(client, f"{routes.PREFIX}/config/workflow?project=acme")
        assert reply.status == 200, reply.body
        publish = _stage_rows(reply.body)["publish"]
        assert publish["source"] == "app_override"
        assert publish["from_preset"] is False
        # An override names no preset: claiming one would say the commands came
        # from a definition an operator could change by selecting another.
        assert publish["preset"] == ""
        assert publish["declared_at"] == "workflow.stages.publish"
        assert publish["argv"] == [["make", "deploy"]]

    @pytest.mark.asyncio
    async def test_a_project_stage_declaration_wins_and_says_it_is_the_projects(
        self, recorded_sel: RecordedSel, enabled: None, home: Path
    ) -> None:
        """Both layers declare the same stage, so this also pins that the route
        derives no precedence of its own: the narrower layer wins because the
        engine resolved it that way, and the row names that layer."""
        async with _client() as client:
            written = await _put(
                client,
                f"{routes.PREFIX}/config",
                {
                    "patch": {
                        "workflow": {"stages": {"publish": [["make", "deploy"]]}},
                        "projects": {
                            "acme": {
                                "path": "/tmp/acme",
                                "workflow": {"stages": {"publish": [["make", "ship"]]}},
                            }
                        },
                    }
                },
            )
            assert written.status == 200, written.body
            reply = await _get(client, f"{routes.PREFIX}/config/workflow?project=acme")
        assert reply.status == 200, reply.body
        publish = _stage_rows(reply.body)["publish"]
        assert publish["source"] == "project_override"
        assert publish["declared_at"] == "projects.acme.workflow.stages.publish"
        assert publish["argv"] == [["make", "ship"]]

    @pytest.mark.asyncio
    async def test_a_stage_nothing_defines_is_unconfigured_and_not_from_the_preset(
        self, recorded_sel: RecordedSel, enabled: None, home: Path
    ) -> None:
        """The other required invariant. ``git-pull-request`` defines isolate and
        submit only, so the three stages it leaves alone must say so: rendering
        them as preset-supplied would tell an operator a stage runs when it does
        not, and omitting them would say the same thing by silence."""
        async with _client() as client:
            written = await _put(
                client,
                f"{routes.PREFIX}/config",
                {
                    "patch": {
                        "projects": {
                            "acme": {
                                "path": "/tmp/acme",
                                "workflow": {"preset": "git-pull-request"},
                            }
                        }
                    }
                },
            )
            assert written.status == 200, written.body
            reply = await _get(client, f"{routes.PREFIX}/config/workflow?project=acme")
        rows = _stage_rows(reply.body)
        for stage in ("verify", "publish", "teardown"):
            row = rows[stage]
            assert row["source"] == "unconfigured", stage
            assert row["from_preset"] is False, stage
            assert row["preset"] == "", stage
            assert row["skipped"] is True, stage
            assert row["commands"] == 0, stage
            assert row["argv"] == [], stage

    @pytest.mark.asyncio
    async def test_the_read_says_where_a_stage_outside_the_delivery_flow_runs(
        self, recorded_sel: RecordedSel, enabled: None, home: Path
    ) -> None:
        """Teardown runs at archive rather than at the end of a delivery, and
        isolate runs before the flow. Projected so a client renders when a stage
        runs without keeping its own copy of a fact that lives in the engine."""
        async with _client() as client:
            reply = await _get(client, f"{routes.PREFIX}/config/workflow")
        assert reply.status == 200, reply.body
        rows = _stage_rows(reply.body)
        assert rows["isolate"]["runs_at"] == routes.RUN_POINT_ISOLATION
        assert rows["teardown"]["runs_at"] == routes.RUN_POINT_ARCHIVE
        assert [stage for stage, row in rows.items() if row["runs_at"] == routes.RUN_POINT_DELIVERY]
        assert reply.body["delivery_flow_stages"] == list(DELIVERY_FLOW_STAGES)
        assert routes.RUN_POINT_ARCHIVE not in reply.body["delivery_flow_stages"]
        # Every declared stage must have an answer. `_STAGE_RUN_POINTS` projects ""
        # for a stage it does not name, which renders as a blank run point — so a
        # stage the engine adds to DELIVERY_STAGES alone would silently invite the
        # very inference this field exists to remove. Fail loudly instead.
        unmapped = [stage for stage, row in rows.items() if not row["runs_at"]]
        assert not unmapped, (
            f"these stages project no run point: {unmapped}. Add them to "
            "_STAGE_RUN_POINTS rather than letting the client infer when they run"
        )

    @pytest.mark.asyncio
    async def test_a_configured_gate_carries_its_position_severity_and_commands(
        self, recorded_sel: RecordedSel, enabled: None, home: Path
    ) -> None:
        async with _client() as client:
            written = await _put(
                client,
                f"{routes.PREFIX}/config",
                {
                    "patch": {
                        "quality_gates": [
                            {
                                "name": "tests",
                                "position": "pre_submit",
                                "severity": "blocking",
                                "commands": [["make", "test"]],
                            },
                            {
                                "name": "coverage",
                                "position": "post_submit",
                                "severity": "advisory",
                                "commands": [["make", "coverage"]],
                            },
                        ]
                    }
                },
            )
            assert written.status == 200, written.body
            reply = await _get(client, f"{routes.PREFIX}/config/workflow?project=acme")
        assert reply.status == 200, reply.body
        assert reply.body["gates_unreadable"] is False
        # Declaration order, because that is the order they run in at a position.
        assert [gate["name"] for gate in reply.body["gates"]] == ["tests", "coverage"]
        assert reply.body["gates"][0] == {
            "name": "tests",
            "position": "pre_submit",
            "severity": "blocking",
            # The engine's own reading of that severity, so a surface does not
            # decide for itself whether a failure stops the flow.
            "blocking": True,
            "commands": [["make", "test"]],
            "origin": "app_config",
            "declared_at": "quality_gates[0]",
        }
        assert reply.body["gates"][1]["blocking"] is False
        # App-level: the read is project-scoped for the workflow and the gate list
        # beside it is not, and it says which.
        assert reply.body["gates_scope_is_app"] is True

    @pytest.mark.asyncio
    async def test_an_unreadable_gate_list_is_not_reported_as_no_gates(
        self, recorded_sel: RecordedSel, enabled: None, home: Path
    ) -> None:
        """The distinction the engine itself makes: an unparseable list refuses
        delivery outright rather than proceeding ungated, so reporting it as an
        empty list would say every check is configured away when what is true is
        that the document needs repairing. ``gates`` is null rather than ``[]`` so
        a client cannot read the two as the same answer by accident."""
        _write_document({"version": 1, "quality_gates": "make test"})
        async with _client() as client:
            reply = await _get(client, f"{routes.PREFIX}/config/workflow")
        assert reply.status == 200, reply.body
        assert reply.body["gates_unreadable"] is True
        assert reply.body["gates"] is None
        assert [error["path"] for error in reply.body["gate_errors"]] == ["quality_gates"]
        # The workflow beside it still resolves: refusing the whole read would
        # leave the stage rows unstateable, which is the opposite failure.
        assert _stage_rows(reply.body)

    @pytest.mark.asyncio
    async def test_a_document_with_no_gates_is_reported_as_an_empty_list(
        self, recorded_sel: RecordedSel, enabled: None, home: Path
    ) -> None:
        """Non-vacuity for the test above: the two states are distinguishable, so
        the null means something."""
        async with _client() as client:
            reply = await _get(client, f"{routes.PREFIX}/config/workflow")
        assert reply.status == 200, reply.body
        assert reply.body["gates"] == []
        assert reply.body["gates_unreadable"] is False
        assert reply.body["gate_errors"] == []
        assert reply.body["configured"] is False
        assert reply.body["preset"] is None
        assert reply.body["user_presets"] == []

    @pytest.mark.asyncio
    async def test_a_preset_name_nothing_defines_is_a_refusal_naming_its_path(
        self, recorded_sel: RecordedSel, enabled: None, home: Path
    ) -> None:
        """A selection never silently degrades. The engine raises rather than
        resolving to nothing, and the route reports it by path so an operator
        repairs the selection instead of hunting a workflow that renders empty."""
        _write_document({"version": 1, "workflow": {"preset": "no-such-preset"}})
        async with _client() as client:
            reply = await _get(client, f"{routes.PREFIX}/config/workflow")
        assert reply.status == 422
        assert reply.code == "config_invalid"
        assert "workflow.preset" in reply.body["error"]

    @pytest.mark.asyncio
    async def test_an_unparseable_document_is_a_refusal_and_not_an_empty_workflow(
        self, recorded_sel: RecordedSel, enabled: None, home: Path
    ) -> None:
        config_dir = default_root()
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / CONFIG_FILENAME).write_text("{ not json", encoding="utf-8")
        async with _client() as client:
            reply = await _get(client, f"{routes.PREFIX}/config/workflow")
        assert reply.status == 409
        assert reply.code == "config_unreadable"

    @pytest.mark.asyncio
    async def test_a_hand_edited_gate_name_cannot_set_the_width_of_a_row(
        self, recorded_sel: RecordedSel, enabled: None, home: Path
    ) -> None:
        """The write door constrains a gate name to a non-empty string and nothing
        more, so the ceiling is this projection's."""
        _write_document(
            {
                "version": 1,
                "quality_gates": [
                    {
                        "name": "n" * 400,
                        "position": "pre_submit",
                        "severity": "blocking",
                        "commands": [["make", "test"]],
                    }
                ],
            }
        )
        async with _client() as client:
            reply = await _get(client, f"{routes.PREFIX}/config/workflow")
        assert reply.status == 200, reply.body
        name = reply.body["gates"][0]["name"]
        # Against the cap the projection actually declares, not merely against the
        # input width: `< 400` would have passed for any ceiling up to 399, which
        # is every value except the one this test exists to pin. `sanitized` keeps
        # `limit` characters and then appends its notice, so the notice is part of
        # the expected width rather than an overrun of it.
        assert name == "n" * routes.MAX_GATE_NAME_CHARS + DISPLAY_TRUNCATION_NOTICE, (
            f"a hand-edited gate name must be capped at {routes.MAX_GATE_NAME_CHARS} "
            f"characters and say it was truncated; got {len(name)} chars: {name!r}"
        )

    def test_the_route_derives_no_precedence_of_its_own(self) -> None:
        """Structural, over the module's own source.

        The per-stage layering lives in ``DeliveryWorkflow`` and the display of it
        in ``stage_origins``. A second implementation here would be the copy that
        drifts, and it would drift silently: both answers look plausible until the
        day a project overrides a stage its preset also defines.
        """
        snapshot = _function_ast("_workflow_snapshot")
        reached = {
            child.func.id
            for child in ast.walk(snapshot)
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Name)
        }
        assert "stage_origins" in reached, "the stage rows must come from the engine's own display"
        # The docstring is excluded deliberately: it NAMES the layers to state the
        # invariants it keeps, and reading prose as code would make this fail for
        # documenting the very property it asserts. The match is on the quoted
        # literal, so the ``user_presets`` payload key is not read as the
        # ``user_preset`` source it merely contains.
        body = _function_body_source(snapshot)
        for layer in ("bundled_preset", "user_preset", "app_override", "project_override"):
            assert f'"{layer}"' not in body, (
                f"{layer} is named inside _workflow_snapshot, which means the route "
                "is deciding a source rather than relaying the one the engine resolved"
            )


# --- provider conformance as a job ------------------------------------------


def _conformance_source(name: str) -> str:
    """A conformance function's source with its docstring dropped.

    Tolerant of both function kinds because this section owns one blocking helper
    and one coroutine, and the claim below is the same about each. A structural
    claim must not read a docstring: prose NAMING the value a function promises not
    to read would fail the assertion for explaining itself.
    """
    for node in _module_ast().body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            body = [
                statement
                for statement in node.body
                if not (
                    isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant)
                )
            ]
            assert body, f"{name} has no statements outside its docstring"
            lines = ROUTES_SOURCE.read_text(encoding="utf-8").splitlines()
            last = body[-1].end_lineno or body[-1].lineno
            return "\n".join(lines[body[0].lineno - 1 : last])
    raise AssertionError(f"no function named {name} in {ROUTES_SOURCE}")


#: A configured command binding for the analysis capability, with a deliberately
#: enormous per-binding timeout. Nothing ever runs it: every test below either
#: substitutes the runner or substitutes the transport, so the argv exists to be
#: fingerprinted and turned into a candidate rather than to be executed.
_BOUND_ANALYSIS: dict[str, Any] = {
    "capabilities": {
        "analysis": {
            "transport": "command",
            "command": ["my-analyzer", "--json"],
            "env": {"TOKEN": "not-a-real-secret"},
            # Far above the app setting's floor, which the engine deliberately
            # permits: raising ONE provider's ceiling is a legitimate thing for an
            # operator to do. It must not raise this surface's probe bound.
            "timeout_s": 3000,
        }
    }
}


@pytest.fixture()
def no_conformance_jobs() -> Any:
    """Empty the module's job table around each test.

    The table is process-global by design — a job outlives the request that
    started it — so without this a completed run leaks into the next test and the
    absent-state assertions read a report somebody else's test produced.
    """
    routes._CONFORMANCE_JOBS.clear()
    routes._CONFORMANCE_TASKS.clear()
    yield
    routes._CONFORMANCE_JOBS.clear()
    routes._CONFORMANCE_TASKS.clear()


async def _drain_conformance() -> None:
    """Wait for every in-flight conformance task to record its outcome.

    Completion is not the same event as removal from the module's task set: the
    callback that discards a finished task is scheduled on the loop, so awaiting
    the task alone leaves it in the set and a loop over "is the set empty" never
    terminates. Awaited on ``done()`` instead, with one trip through the loop
    afterwards so the set a caller inspects is settled.
    """
    for _ in range(100):
        pending = tuple(task for task in routes._CONFORMANCE_TASKS if not task.done())
        if not pending:
            await asyncio.sleep(0)
            return
        await asyncio.gather(*pending, return_exceptions=True)
    raise AssertionError("conformance tasks never drained")


def _report_stub(
    *, capability: str = "analysis", candidate: str = "my-analyzer", passed: bool = True
) -> Any:
    """A real :class:`ConformanceReport`, so the route serializes what it would.

    A hand-written dict here would let the route pass while ``to_json_object``
    changed shape underneath it, which is the coupling the payload exists to keep.
    """
    from kiro_crew.apps.builtins.spec_engine.engine.capabilities import (
        CHECK_PLANTED_DEFECT,
        CheckResult,
        ConformanceReport,
    )

    return ConformanceReport(
        capability=capability,
        candidate=candidate,
        declared_fixtures=("planted-ambiguity",),
        declared_checks=(CHECK_PLANTED_DEFECT,),
        results=(
            CheckResult(
                check=CHECK_PLANTED_DEFECT,
                fixture="planted-ambiguity",
                passed=passed,
                detail="detected 1 planted defect(s)" if passed else "reported no finding",
                excused=0 if passed else 1,
            ),
        ),
    )


@dataclass
class _RecordedVerify:
    """Stands in for the conformance runner, recording what the route asked of it.

    Substituted at ``routes.verify`` rather than lower down because the deadline is
    the thing under test and that is the argument the route chooses: a fake any
    deeper would be asserting about the runner's defaults instead of the route's
    decision.
    """

    calls: list[dict[str, Any]]
    gate: Any = None
    raises: BaseException | None = None
    report: Any = None

    def __call__(self, candidate: Any, capability: str, **kwargs: Any) -> Any:
        self.calls.append(
            {"candidate": candidate, "capability": capability, "kwargs": dict(kwargs)}
        )
        if self.gate is not None:
            assert self.gate.wait(timeout=5), "the gated run was never released"
        if self.raises is not None:
            raise self.raises
        return self.report if self.report is not None else _report_stub(capability=capability)


@pytest.fixture()
def recorded_verify(monkeypatch: pytest.MonkeyPatch) -> _RecordedVerify:
    fake = _RecordedVerify(calls=[])
    monkeypatch.setattr(routes, "verify", fake)
    return fake


class TestConformanceIsAJobAndNotARequest:
    """A started run answers immediately; the outcome is polled.

    Not a matter of taste. The bundled suite gives a document capability five
    fixtures, four of which invoke the candidate a second time for the
    repeatability check — nine child processes, each spawned through the package's
    sandbox chokepoint — and it enforces no aggregate deadline of its own. Held
    inline it would block the gateway's event loop for the whole run; held open as
    a request it would hold a connection for minutes.
    """

    @pytest.mark.asyncio
    async def test_starting_a_run_answers_before_the_suite_finishes(
        self,
        recorded_sel: RecordedSel,
        recorded_verify: _RecordedVerify,
        no_conformance_jobs: None,
        enabled: None,
        home: Path,
    ) -> None:
        import threading

        recorded_verify.gate = threading.Event()
        _store_document(_BOUND_ANALYSIS)
        async with _client() as client:
            started = await _post(
                client, f"{routes.PREFIX}/config/conformance", {"capability": "analysis"}
            )
            # 202: accepted, and deliberately carrying no outcome. The suite has
            # not finished -- the fake is still blocked on its gate.
            assert started.status == 202
            assert started.body["status"] == "running"
            assert started.body["job_id"]
            assert started.body["report"] is None

            polled = await _get(client, f"{routes.PREFIX}/config/conformance/analysis")
            assert polled.status == 200
            assert polled.body["status"] == "running"
            assert polled.body["report"] is None
            assert polled.body["job_id"] == started.body["job_id"]

            recorded_verify.gate.set()
            await _drain_conformance()

            done = await _get(client, f"{routes.PREFIX}/config/conformance/analysis")
        assert done.body["status"] == "complete"
        assert done.body["report"]["capability"] == "analysis"

    @pytest.mark.asyncio
    async def test_a_builtin_binding_is_not_applicable_rather_than_never_checked(
        self,
        recorded_sel: RecordedSel,
        no_conformance_jobs: None,
        enabled: None,
        home: Path,
    ) -> None:
        """The poll and the start must not disagree about whether there is work.

        The POST refuses a builtin-bound capability with ``builtin_binding``. If the
        GET called the same capability ``absent`` — "nobody has checked this yet" —
        the two routes would describe one document two ways, and a panel would
        offer a run the server then refuses.
        """
        async with _client() as client:
            reply = await _get(client, f"{routes.PREFIX}/config/conformance/analysis")
            refused = await _post(
                client, f"{routes.PREFIX}/config/conformance", {"capability": "analysis"}
            )
        assert reply.status == 200, reply.body
        assert reply.body["status"] == routes.CONFORMANCE_NOT_APPLICABLE
        assert reply.body["status"] != routes.CONFORMANCE_ABSENT
        assert reply.body["is_builtin"] is True
        # Non-vacuous: the POST really does refuse this same capability, so the two
        # answers are being compared against each other rather than asserted apart.
        assert refused.status == 409, refused.body
        assert refused.body["code"] == "builtin_binding"

    @pytest.mark.asyncio
    async def test_a_report_kept_after_rebinding_to_the_builtin_says_it_cannot_rerun(
        self,
        recorded_sel: RecordedSel,
        recorded_verify: _RecordedVerify,
        no_conformance_jobs: None,
        enabled: None,
        home: Path,
    ) -> None:
        """The one state where `status` alone cannot answer "can I run this".

        A capability rebound to its builtin AFTER a run keeps its report, so the
        poll reports ``complete`` — correctly, the outcome is real — while the POST
        refuses it. Without ``is_builtin`` a client would offer a re-run the server
        declines, which is the same disagreement ``not_applicable`` was added to
        close, surviving in the state where a report exists.
        """
        _write_document(_BOUND_ANALYSIS)
        async with _client() as client:
            started = await _post(
                client, f"{routes.PREFIX}/config/conformance", {"capability": "analysis"}
            )
            assert started.status == 202, started.body
            assert started.body["is_builtin"] is False, "the run was against a real program"
            await _drain_conformance()
            # Rebind to the builtin with the report already stored.
            _write_document({"version": 1})
            polled = await _get(client, f"{routes.PREFIX}/config/conformance/analysis")
            refused = await _post(
                client, f"{routes.PREFIX}/config/conformance", {"capability": "analysis"}
            )
        assert polled.status == 200, polled.body
        # The report survives — rebinding does not delete an outcome that happened.
        assert polled.body["report"] is not None
        assert polled.body["stale"] is True, "the report no longer describes the binding"
        # ...and the payload says a re-run is impossible, which `status` cannot.
        assert polled.body["is_builtin"] is True
        assert refused.status == 409, refused.body
        assert refused.body["code"] == "builtin_binding"

    @pytest.mark.asyncio
    async def test_the_in_flight_task_is_held_by_a_strong_reference(
        self,
        recorded_sel: RecordedSel,
        recorded_verify: _RecordedVerify,
        no_conformance_jobs: None,
        enabled: None,
        home: Path,
    ) -> None:
        """A run nobody holds can be collected mid-flight.

        ``asyncio`` keeps only a weak reference to a running task, so without the
        module's own set a task can be garbage-collected while the job stays
        recorded as ``running`` — leaving the poll route reporting a run that is
        not happening, for the life of the gateway. The set is also what
        ``_drain_conformance`` reads, so a refactor dropping the ``add()`` would
        quietly turn that helper into a no-op and every other test in this class
        would keep passing. Asserted directly for that reason.

        Takes ``recorded_verify`` and ``no_conformance_jobs`` like every sibling:
        the substituted runner is what keeps this from spawning the real suite's
        nine child processes, and the cleared table is what keeps the started-run
        assertion from depending on whatever an earlier test left behind.

        GATED, because substituting the runner removed the very slowness a naive
        version of this assertion depended on. The fake returns in microseconds,
        so the task completes and its done-callback discards it from the set
        before an ungated assertion can read it — a wall-clock race that passes
        under the repo's parallel addopts only because that run is an order of
        magnitude slower. Blocking the fake makes both assertions hold by
        construction rather than by timing: non-empty WHILE the worker is held,
        empty only after it is released and drained.
        """
        _write_document(_BOUND_ANALYSIS)
        import threading

        recorded_verify.gate = threading.Event()
        async with _client() as client:
            started = await _post(
                client, f"{routes.PREFIX}/config/conformance", {"capability": "analysis"}
            )
            # 202: the run was ACCEPTED, not completed. A job start that answered
            # 200 would read as an outcome.
            assert started.status == 202, started.body
            # Read while the fake is still blocked on its gate, so the task cannot
            # have settled and been discarded yet. Asserting it is NOT DONE is the
            # real property — a strong reference to a RUNNING task — and it is what
            # makes this independent of host timing: merely being present is
            # satisfiable by a settled task whose discard callback has not yet run.
            assert any(not task.done() for task in routes._CONFORMANCE_TASKS), (
                "no in-flight run is held in the module's task set, so nothing holds "
                "a strong reference to it and it can be collected mid-run"
            )
            recorded_verify.gate.set()
            await _drain_conformance()
        assert not routes._CONFORMANCE_TASKS, (
            "a settled task was never discarded from the set, which leaks one entry "
            "per run for the life of the gateway"
        )

    @pytest.mark.asyncio
    async def test_the_run_happens_off_the_event_loop(
        self,
        recorded_sel: RecordedSel,
        recorded_verify: _RecordedVerify,
        no_conformance_jobs: None,
        enabled: None,
        home: Path,
    ) -> None:
        """The suite is fully synchronous, so it must not run on the loop.

        Asserted by thread identity rather than by reading the source: a
        ``to_thread`` that was refactored into a direct call would still look like
        a job from the outside, and the symptom would be a gateway that stops
        answering everything else for the length of a run.
        """
        import threading

        loop_thread = threading.get_ident()
        seen: list[int] = []

        def _record(candidate: Any, capability: str, **kwargs: Any) -> Any:
            seen.append(threading.get_ident())
            return _report_stub(capability=capability)

        _store_document(_BOUND_ANALYSIS)
        async with _client() as client:
            with pytest.MonkeyPatch.context() as patch:
                patch.setattr(routes, "verify", _record)
                await _post(
                    client, f"{routes.PREFIX}/config/conformance", {"capability": "analysis"}
                )
                await _drain_conformance()
        assert seen, "the runner was never called"
        assert seen[0] != loop_thread

    @pytest.mark.asyncio
    async def test_a_second_run_for_the_same_capability_is_refused_with_the_reason(
        self,
        recorded_sel: RecordedSel,
        recorded_verify: _RecordedVerify,
        no_conformance_jobs: None,
        enabled: None,
        home: Path,
    ) -> None:
        """Nothing in the runner prevents overlapping runs against one program."""
        import threading

        recorded_verify.gate = threading.Event()
        _store_document(_BOUND_ANALYSIS)
        async with _client() as client:
            first = await _post(
                client, f"{routes.PREFIX}/config/conformance", {"capability": "analysis"}
            )
            second = await _post(
                client, f"{routes.PREFIX}/config/conformance", {"capability": "analysis"}
            )
            assert first.status == 202
            assert second.status == 409
            assert second.code == "conformance_running"
            # Saying why, and naming the job already in flight, so the operator can
            # poll the one that is running rather than retrying into the refusal.
            assert first.body["job_id"] in second.body["error"]
            recorded_verify.gate.set()
            await _drain_conformance()
        assert len(recorded_verify.calls) == 1, "the refused run must not have invoked anything"

    @pytest.mark.asyncio
    async def test_a_run_can_start_again_once_the_first_one_finished(
        self,
        recorded_sel: RecordedSel,
        recorded_verify: _RecordedVerify,
        no_conformance_jobs: None,
        enabled: None,
        home: Path,
    ) -> None:
        """Non-vacuity: the refusal is about CONCURRENCY, not a one-shot latch."""
        _store_document(_BOUND_ANALYSIS)
        async with _client() as client:
            first = await _post(
                client, f"{routes.PREFIX}/config/conformance", {"capability": "analysis"}
            )
            await _drain_conformance()
            second = await _post(
                client, f"{routes.PREFIX}/config/conformance", {"capability": "analysis"}
            )
            await _drain_conformance()
        assert first.status == 202
        assert second.status == 202
        assert second.body["job_id"] != first.body["job_id"]


class TestTheRouteChoosesTheDeadlineAndABindingCannotRaiseIt:
    """The one number this surface must not take from configuration.

    ``CapabilityRegistry.timeout_for`` lets a per-binding ``timeout_s`` sit above
    the app setting's floor with no clamp, and that is correct for a real
    invocation. For a probe an operator started from a page it is not: the suite
    invokes the provider nine times and caps nothing in aggregate, so the
    binding's number is multiplied by nine — a declared 3000 seconds would be
    most of a day of held resources for a check nobody is watching.
    """

    @pytest.mark.asyncio
    async def test_a_binding_declaring_a_large_timeout_does_not_raise_the_servers_cap(
        self,
        recorded_sel: RecordedSel,
        recorded_verify: _RecordedVerify,
        no_conformance_jobs: None,
        enabled: None,
        home: Path,
    ) -> None:
        _store_document(_BOUND_ANALYSIS)
        declared = _BOUND_ANALYSIS["capabilities"]["analysis"]["timeout_s"]
        assert declared > routes.CONFORMANCE_DEADLINE_S, "the fixture must exceed the cap"
        async with _client() as client:
            started = await _post(
                client, f"{routes.PREFIX}/config/conformance", {"capability": "analysis"}
            )
            await _drain_conformance()
        assert started.status == 202
        assert recorded_verify.calls, "the runner was never reached"
        assert recorded_verify.calls[0]["kwargs"]["deadline_s"] == routes.CONFORMANCE_DEADLINE_S
        # And the cap travels in the payload, so the surface offering the action
        # can state the bound rather than guessing it.
        assert started.body["deadline_s"] == routes.CONFORMANCE_DEADLINE_S

    def test_the_route_never_reads_a_bindings_timeout_for_its_deadline(self) -> None:
        """Structural, over the module's own source.

        The live assertion above proves the value for one document. This proves
        the route has no path to the binding's number at all, so a later branch
        cannot reintroduce it for the case somebody thought was special.
        """
        for name in ("_run_conformance", "handle_post_conformance"):
            body = _conformance_source(name)
            assert "timeout_s" not in body, (
                f"{name} reads a timeout out of the binding; the per-invocation "
                "deadline is this route's own choice"
            )
            assert "timeout_for" not in body

    @pytest.mark.asyncio
    async def test_the_candidate_is_built_from_the_binding_and_bypasses_the_registry(
        self,
        recorded_sel: RecordedSel,
        recorded_verify: _RecordedVerify,
        no_conformance_jobs: None,
        enabled: None,
        home: Path,
    ) -> None:
        """The registry degrades a broken provider to its builtin and continues.

        Right for a run, and it would hide exactly what a conformance report
        exists to reveal — so the candidate is built from the binding directly and
        carries the program's own label.
        """
        from kiro_crew.apps.builtins.spec_engine.engine.capabilities import TransportCandidate
        from kiro_crew.apps.builtins.spec_engine.engine.capabilities.transports import (
            CommandProviderTransport,
        )

        _store_document(_BOUND_ANALYSIS)
        async with _client() as client:
            await _post(
                client, f"{routes.PREFIX}/config/conformance", {"capability": "analysis"}
            )
            await _drain_conformance()
        candidate = recorded_verify.calls[0]["candidate"]
        assert isinstance(candidate, TransportCandidate)
        assert isinstance(candidate.transport, CommandProviderTransport)
        assert candidate.transport.argv == ("my-analyzer", "--json")
        assert candidate.name == "my-analyzer"


class TestTheConformanceStateIsHonestAboutWhatItHas:
    """Every way this read could flatter a provider, closed one at a time."""

    @pytest.mark.asyncio
    async def test_a_capability_never_checked_reports_absent_rather_than_passing(
        self, recorded_sel: RecordedSel, no_conformance_jobs: None, enabled: None, home: Path
    ) -> None:
        _store_document(_BOUND_ANALYSIS)
        async with _client() as client:
            reply = await _get(client, f"{routes.PREFIX}/config/conformance/analysis")
        assert reply.status == 200
        assert reply.body["status"] == "absent"
        assert reply.body["report"] is None
        assert reply.body["binding_fingerprint"] == ""
        # The live binding still has a fingerprint, so a client can key a cache on
        # it before any run exists.
        assert reply.body["binding_current"]

    @pytest.mark.asyncio
    async def test_a_run_that_could_not_be_carried_out_is_not_reported_as_complete(
        self,
        recorded_sel: RecordedSel,
        recorded_verify: _RecordedVerify,
        no_conformance_jobs: None,
        enabled: None,
        home: Path,
    ) -> None:
        """The absence of failures is never a pass.

        ``complete`` with an empty report is exactly how "no outcome was obtained"
        gets read as "nothing was wrong", so a run that did not happen gets its own
        status.
        """
        recorded_verify.raises = OSError("no room for a temporary directory")
        _store_document(_BOUND_ANALYSIS)
        async with _client() as client:
            await _post(
                client, f"{routes.PREFIX}/config/conformance", {"capability": "analysis"}
            )
            await _drain_conformance()
            reply = await _get(client, f"{routes.PREFIX}/config/conformance/analysis")
        assert reply.body["status"] == "failed"
        assert reply.body["status"] != routes.CONFORMANCE_COMPLETE
        assert reply.body["report"] is None
        assert "OSError" in reply.body["error"]

    @pytest.mark.asyncio
    async def test_a_binding_edited_after_a_run_makes_the_report_stale(
        self,
        recorded_sel: RecordedSel,
        recorded_verify: _RecordedVerify,
        no_conformance_jobs: None,
        enabled: None,
        home: Path,
    ) -> None:
        """A report describes the binding it ran against and no other."""
        _store_document(_BOUND_ANALYSIS)
        async with _client() as client:
            await _post(
                client, f"{routes.PREFIX}/config/conformance", {"capability": "analysis"}
            )
            await _drain_conformance()
            fresh = await _get(client, f"{routes.PREFIX}/config/conformance/analysis")
            assert fresh.body["stale"] is False
            assert fresh.body["binding_current"] == fresh.body["binding_fingerprint"]

            moved = json.loads(json.dumps(_BOUND_ANALYSIS))
            moved["capabilities"]["analysis"]["command"] = ["a-different-analyzer"]
            _store_document(moved)
            after = await _get(client, f"{routes.PREFIX}/config/conformance/analysis")
        assert after.body["stale"] is True
        assert after.body["binding_current"] != after.body["binding_fingerprint"]
        # The report is still relayed rather than dropped: it is evidence about a
        # binding that existed, and hiding it would be as dishonest as presenting
        # it as current.
        assert after.body["report"] is not None

    @pytest.mark.asyncio
    async def test_the_invocation_bound_is_the_suites_own_and_differs_by_capability(
        self, recorded_sel: RecordedSel, no_conformance_jobs: None, enabled: None, home: Path
    ) -> None:
        """A surface must state what a run costs BEFORE offering to start one.

        Projected rather than left to a client because the figure is not one
        figure: ``analysis`` has five fixtures and four of them make a second call
        for the repeatability check, while ``watch_sources`` has one fixture making
        two calls. A client holding a single number would state the wrong one for
        every non-document capability.

        Both are pinned as LITERALS rather than recomputed from ``suite_for`` here,
        which would only assert that one expression equals itself. A suite that
        grows a fixture has to move these numbers, which is the point: the copy an
        operator reads changes with it.
        """
        _store_document(_BOUND_ANALYSIS)
        async with _client() as client:
            document = await _get(client, f"{routes.PREFIX}/config/conformance/analysis")
            other = await _get(client, f"{routes.PREFIX}/config/conformance/watch_sources")
        # Present with no run recorded, which is the state the number exists for.
        assert document.body["status"] == "absent"
        assert document.body["max_invocations"] == 9
        # `watch_sources` is on its builtin here, so this is also the case where the
        # bound has to be stated beside a capability that cannot be run at all.
        assert other.body["max_invocations"] == 2
        assert other.body["max_invocations"] != document.body["max_invocations"]

    @pytest.mark.asyncio
    async def test_an_edited_environment_value_also_makes_the_report_stale(
        self,
        recorded_sel: RecordedSel,
        recorded_verify: _RecordedVerify,
        no_conformance_jobs: None,
        enabled: None,
        home: Path,
    ) -> None:
        """Every field the transport reads is in the fingerprint, not just argv.

        A provider handed a different token is a different provider, and a report
        that survived the swap would be a report about a call nobody makes.
        """
        _store_document(_BOUND_ANALYSIS)
        async with _client() as client:
            await _post(
                client, f"{routes.PREFIX}/config/conformance", {"capability": "analysis"}
            )
            await _drain_conformance()
            moved = json.loads(json.dumps(_BOUND_ANALYSIS))
            moved["capabilities"]["analysis"]["env"] = {"TOKEN": "a-different-value"}
            _store_document(moved)
            after = await _get(client, f"{routes.PREFIX}/config/conformance/analysis")
        assert after.body["stale"] is True

    @pytest.mark.asyncio
    async def test_the_fingerprint_never_carries_the_binding_it_digests(
        self,
        recorded_sel: RecordedSel,
        no_conformance_jobs: None,
        enabled: None,
        home: Path,
    ) -> None:
        """``env`` may hold a credential, so the binding travels as a digest.

        A relayed binding would put a token on a read an app-minted session may
        make, which is the one thing this payload must not do.
        """
        _store_document(_BOUND_ANALYSIS)
        async with _client() as client:
            reply = await _get(client, f"{routes.PREFIX}/config/conformance/analysis")
        body = json.dumps(reply.body)
        assert "not-a-real-secret" not in body
        assert "TOKEN" not in body

    @pytest.mark.asyncio
    async def test_a_declined_detection_and_its_excused_count_reach_the_payload(
        self,
        recorded_sel: RecordedSel,
        monkeypatch: pytest.MonkeyPatch,
        no_conformance_jobs: None,
        enabled: None,
        home: Path,
    ) -> None:
        """The qualifier the verdict line carries, rendered from the payload.

        Driven through the REAL runner with only the transport substituted, so the
        report is serialized by ``to_json_object`` rather than by a fixture that
        could agree with the route while disagreeing with the engine. Without these
        two fields a client shows an unqualified pass about a provider that
        declared every document skipped.
        """
        from kiro_crew.apps.builtins.spec_engine.engine.capabilities import (
            CURRENT_SCHEMA_VERSION,
            TRANSPORT_COMMAND,
        )

        @dataclass
        class _DecliningTransport:
            """Answers every fixture by declaring the documents skipped."""

            @property
            def transport(self) -> str:
                return TRANSPORT_COMMAND

            def invoke(self, request: Any, *, timeout_s: int) -> Any:
                return {
                    "schema_version": CURRENT_SCHEMA_VERSION,
                    "capability": request.capability,
                    "provider": {"name": "declining"},
                    "coverage": {
                        "processed": [],
                        "skipped": [
                            {"item": f"document:{artifact.kind}", "reason": "declined to examine"}
                            for artifact in request.artifacts
                        ]
                        or [{"item": "nothing", "reason": "declined to examine"}],
                    },
                    "findings": [],
                    "cost": {"credits": 0.0},
                    "result": {"depth": "structural"},
                }

        monkeypatch.setattr(routes, "transport_for", lambda binding: _DecliningTransport())
        _store_document(_BOUND_ANALYSIS)
        async with _client() as client:
            await _post(
                client, f"{routes.PREFIX}/config/conformance", {"capability": "analysis"}
            )
            await _drain_conformance()
            reply = await _get(client, f"{routes.PREFIX}/config/conformance/analysis")
        report = reply.body["report"]
        assert reply.body["status"] == "complete", reply.body
        assert report["declined_detections"] > 0
        excused = [entry for entry in report["results"] if entry["excused"]]
        assert excused, report["results"]
        assert all(entry["passed"] is True for entry in excused)

    @pytest.mark.asyncio
    async def test_the_verdict_is_the_engines_and_a_gap_makes_it_false(
        self,
        recorded_sel: RecordedSel,
        recorded_verify: _RecordedVerify,
        no_conformance_jobs: None,
        enabled: None,
        home: Path,
    ) -> None:
        """A declared check that never ran is a failure of the RUN, not an absence.

        ``passed`` is not "no failures": it also requires that every declared
        fixture and check was evaluated. The route relays that answer rather than
        recomputing one from the results it can see.
        """
        from kiro_crew.apps.builtins.spec_engine.engine.capabilities import (
            CHECK_PLANTED_DEFECT,
            CHECK_SCHEMA_VALIDITY,
            ConformanceReport,
        )

        recorded_verify.report = ConformanceReport(
            capability="analysis",
            candidate="my-analyzer",
            declared_fixtures=("planted-ambiguity",),
            # Two declared, one evaluated: the report's own gap machinery.
            declared_checks=(CHECK_PLANTED_DEFECT, CHECK_SCHEMA_VALIDITY),
            results=_report_stub().results,
        )
        _store_document(_BOUND_ANALYSIS)
        async with _client() as client:
            await _post(
                client, f"{routes.PREFIX}/config/conformance", {"capability": "analysis"}
            )
            await _drain_conformance()
            reply = await _get(client, f"{routes.PREFIX}/config/conformance/analysis")
        report = reply.body["report"]
        assert report["gaps"], "the gap must travel so a client can name it"
        assert report["passed"] is False
        assert all(entry["passed"] is True for entry in report["results"]), (
            "every individual check passed, which is exactly why the verdict has "
            "to come from the engine rather than from the results"
        )


class TestTheConformanceRoutesRefuseWhatCannotBeChecked:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("capability", ["format_validation", "claim_ledger"])
    async def test_an_engine_floor_capability_has_no_provider_to_check(
        self,
        capability: str,
        recorded_sel: RecordedSel,
        no_conformance_jobs: None,
        enabled: None,
        home: Path,
    ) -> None:
        """The floor is not bindable, so there is no candidate and no suite."""
        async with _client() as client:
            started = await _post(
                client, f"{routes.PREFIX}/config/conformance", {"capability": capability}
            )
            polled = await _get(client, f"{routes.PREFIX}/config/conformance/{capability}")
        for reply in (started, polled):
            assert reply.status == 422
            assert reply.code == "engine_floor_capability"

    @pytest.mark.asyncio
    async def test_an_unknown_capability_is_refused_rather_than_answered_absent(
        self, recorded_sel: RecordedSel, no_conformance_jobs: None, enabled: None, home: Path
    ) -> None:
        """``absent`` means "not checked yet", which would be a lie about a
        capability the engine does not have."""
        async with _client() as client:
            started = await _post(
                client, f"{routes.PREFIX}/config/conformance", {"capability": "no-such-thing"}
            )
            polled = await _get(client, f"{routes.PREFIX}/config/conformance/no-such-thing")
        for reply in (started, polled):
            assert reply.status == 404
            assert reply.code == "unknown_capability"

    @pytest.mark.asyncio
    async def test_a_capability_bound_to_its_builtin_has_nothing_external_to_check(
        self, recorded_sel: RecordedSel, no_conformance_jobs: None, enabled: None, home: Path
    ) -> None:
        async with _client() as client:
            reply = await _post(
                client, f"{routes.PREFIX}/config/conformance", {"capability": "analysis"}
            )
        assert reply.status == 409
        assert reply.code == "builtin_binding"

    @pytest.mark.asyncio
    async def test_a_run_needs_a_capability_named(
        self, recorded_sel: RecordedSel, no_conformance_jobs: None, enabled: None, home: Path
    ) -> None:
        async with _client() as client:
            reply = await _post(client, f"{routes.PREFIX}/config/conformance", {})
        assert reply.status == 400
        assert reply.code == "field_required"

    @pytest.mark.asyncio
    async def test_a_document_the_engine_refuses_is_not_reported_as_all_builtin(
        self, recorded_sel: RecordedSel, no_conformance_jobs: None, enabled: None, home: Path
    ) -> None:
        """A refused section and an unconfigured one are the same shape.

        Resolving a document that binds an engine-floor name RAISES, and answering
        that as "analysis is on its builtin, nothing to check" would report a
        document a human has to repair as a clean one.
        """
        _store_document({"capabilities": {"audit_log": {"transport": "builtin"}}})
        async with _client() as client:
            reply = await _get(client, f"{routes.PREFIX}/config/conformance/analysis")
        assert reply.status == 422
        assert reply.code == "engine_floor_capability"

    @pytest.mark.asyncio
    async def test_an_unparseable_document_is_reported_as_unreadable(
        self, recorded_sel: RecordedSel, no_conformance_jobs: None, enabled: None, home: Path
    ) -> None:
        config_dir = default_root()
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / CONFIG_FILENAME).write_text("{ not json", encoding="utf-8")
        async with _client() as client:
            reply = await _get(client, f"{routes.PREFIX}/config/conformance/analysis")
        assert reply.status == 409
        assert reply.code == "config_unreadable"


class TestStartingARunIsRecordedAsAnOperatorAction:
    """It spawns the operator-configured program, so the trail is not optional."""

    @pytest.mark.asyncio
    async def test_a_started_run_records_the_program_it_spawned(
        self,
        recorded_sel: RecordedSel,
        recorded_verify: _RecordedVerify,
        no_conformance_jobs: None,
        enabled: None,
        home: Path,
    ) -> None:
        _store_document(_BOUND_ANALYSIS)
        async with _client() as client:
            reply = await _post(
                client, f"{routes.PREFIX}/config/conformance", {"capability": "analysis"}
            )
            await _drain_conformance()
        assert reply.status == 202
        events = [
            event
            for event in recorded_sel.events
            if event.get("operation") == "spec_engine_conformance_run"
        ]
        assert len(events) == 1
        recorded = events[0]
        assert recorded["outcome"] == "success"
        assert recorded["caller"] == "operator"
        # The program is named, because "a conformance run happened" does not say
        # what was executed on this host.
        assert "my-analyzer" in recorded["resources"]
        assert "capability=analysis" in recorded["resources"]

    @pytest.mark.asyncio
    async def test_a_refused_run_records_no_success(
        self, recorded_sel: RecordedSel, no_conformance_jobs: None, enabled: None, home: Path
    ) -> None:
        """Non-vacuity: the record is of a run that started, not of a request."""
        async with _client() as client:
            await _post(
                client, f"{routes.PREFIX}/config/conformance", {"capability": "analysis"}
            )
        assert [
            event
            for event in recorded_sel.events
            if event.get("operation") == "spec_engine_conformance_run"
        ] == []
