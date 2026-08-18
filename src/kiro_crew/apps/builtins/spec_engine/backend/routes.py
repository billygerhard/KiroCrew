"""The Operator_Surface's backing routes: ``/api/apps/spec-engine/*``.

Mounted on the GATEWAY's own aiohttp application by the builtin route loop in
``dashboard/server.py``, which walks ``BUILTIN_NAMES``, imports each app package
and calls the package's ``register_routes(app)``. So requests arrive
same-origin, already through the gateway's auth middleware, and there is no
second process, port or proxy secret anywhere in this module.

**What this surface is for.** One page's worth of operation: the Review_Queue
and its four manual overrides, the configuration document and the resolved read
beside it, the setup flow that produces a first document, the kill switch, and
one run's attributed spend. Everything it does it does by calling the
Spec_Engine library — no rule, no threshold and no state transition is decided
here — so a surface reading a number reads the number the engine enforces
against rather than one this module recomputed.

**Two gates, and why each exists.**

``_require_enabled`` is deny-by-default for an opt-in app. Routes are registered
once at gateway startup while ``defaultEnabled`` is ``false``, so without this
every path would answer for an app nobody enabled.

``_operator_only`` is the one that matters. ``request["user"]`` is truthy for an
APP-minted token, and the token layer's scope check grants an app its own
``/api/apps/<name>/*`` namespace unconditionally (``_app_owns_path`` in
``dashboard/token_auth.py``), so an app token minted for ``spec-engine`` reaches
every path in this module with an identity that passes an auth check. The
authority behind that is not small: the config write runs at an
``operator_confirmed`` surface, which is what unlocks the autonomy ladder and
the argv the delivery pipeline executes, and a kill-switch release restores
spending an operator stopped. An agent able to mint that token would otherwise
widen its own autonomy or lift its own stop. So every MUTATING handler refuses
an app token with 403 and a security event, and refuses an unauthenticated
caller with 401.

Reads deliberately stop at 401. An app token may READ all five read routes,
and the rationale differs by route. The configuration reads are equivalence: the
values come back with credential-classified values elided, and an agent already
has the same read through the Engine_MCP_Server's ``get_config``, so refusing
them here would buy nothing and diverge the two doors. The queue, run-spend and
kill-switch reads have NO MCP equivalent — an app token gains reads here it
could not otherwise obtain (queue rows with source and item ids, per-run spend,
kill-switch state with stoppable run ids). That is accepted, not overlooked:
the requirement guards mutations, these payloads carry no credential-classified
material, and an agent acting on a run legitimately needs to see the queue it
is part of. If that acceptance is ever revisited, the guard mechanism below is
already per-route.

**Two of the guarded routes write nothing, and are guarded anyway.** Setup
inspection and setup planning are pure reads of the library's, but they read a
project path the CALLER names: they open ``.git/config``, steering notes, docs
and CI files under an arbitrary directory and hand excerpts back. Left on the
read composer, an app-minted token would have a general-purpose filesystem reader
inside this app's own namespace. They are POSTs behind the operator guard for that
reason, not because they mutate — so the guard here is about the authority to name
a path, and the mark it stamps is what keeps that decision visible in the route
table rather than resting on a comment.

**Nothing blocking runs on the event loop.** Every handler's disk and database
work — including constructing the stores, which opens SQLite and migrates the
schema — happens inside one ``asyncio.to_thread`` call, and inside the ``try``
that maps failures to a refusal. A store built on the loop would both stall the
gateway and turn an unreadable database into a bare 500.

**Catch clauses are traced against the raising code, not against the names.**
``StateStore`` wraps every ``sqlite3.Error``/``OSError`` into
``StatePersistenceError``, which derives ``StateError`` — and ``StateError``
derives ``Exception`` directly, NOT ``OSError`` and NOT ``ValueError``. A tuple
of ``(OSError, ValueError)`` over a state mutation therefore catches nothing it
was written for. Every arm below names ``StateError`` for that reason.
``ConfigWriteRefused`` derives ``PermissionError``, ``ConfigValidationError``
derives ``ValueError``, and ``ConfigLoadError``/``ConfigRecordError`` derive
``RuntimeError`` — the last two are named explicitly rather than caught as
``RuntimeError`` so a future sibling is not swallowed silently.

The setup path adds the same trap in a second shape. ``SetupApprovalRequired``
derives ``PermissionError`` and therefore ``OSError``, and
``InferredSubjectRefused`` derives ``ValueError`` — so on those handlers the
refusal arm MUST precede the ``OSError`` and ``ValueError`` arms, or a decision
the engine made is reported as a disk failure or as a malformed request.

**The setup flow keeps no plan.** ``plan_setup`` returns a ``plan_id`` that is a
content hash of the project subject, the answers used and the patch they produce
(``engine_mcp/setup_surface.py``), and ``apply_setup`` recomputes it from the
arguments it was handed and refuses on a mismatch. That module is imported rather
than paraphrased: it is the boundary shape both doors share, so an apply driven
from this surface and an apply driven from the Engine_MCP_Server refuse and accept
the same plans, and a stale identity writes nothing on either. It is pure Python
with no import-time work of its own.
"""

from __future__ import annotations

import asyncio
import logging
from functools import wraps
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping

from aiohttp import web

from kiro_crew.apps.manager import is_app_enabled
from kiro_crew.sel import sel

from ..engine import audit as engine_audit
from ..engine import review_queue as engine_review_queue
from ..engine import runs as engine_runs
from ..engine import setup as engine_setup
from ..engine import state as engine_state
from ..engine.budget import ceiling as engine_ceiling
from ..engine.budget import killswitch as engine_killswitch
from ..engine.budget import ledger as engine_ledger
from ..engine.budget import switch as engine_switch
from ..engine.config import (
    APP_NAME,
    CONFIG_ONLY_PATHS,
    DASHBOARD_SURFACE,
    ELIDED,
    ROLES,
    SETUP_ASSISTANT_SURFACE,
    ConfigLoadError,
    ConfigRecordError,
    ConfigStore,
    ConfigValidationError,
    ConfigWarning,
    ConfigWriteRefused,
    document_warnings,
    elide_secrets,
    resolve_all,
    validate_config_document,
)
from ..engine.roles import RolePlan
from ..engine_mcp import setup_surface

logger = logging.getLogger(__name__)

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]

#: URL namespace this app owns. Composed from the manifest name so the prefix and
#: the token layer's ``/api/apps/<name>`` ownership rule cannot drift apart.
PREFIX = f"/api/apps/{APP_NAME}"

#: Attribute the operator guard stamps on the handler it wraps, carrying the
#: operation name it audits under. Read by the suite to assert that EVERY
#: mutating route in the registered table carries the guard — a claim about the
#: route table rather than about the handlers somebody remembered to test.
OPERATOR_GUARD_MARK = "spec_engine_operator_only"

#: Initiator recorded when a request carries no authenticated identity. Only
#: reachable on a deployment whose middleware sets no user; the entry still says
#: which surface acted rather than attributing the action to nobody.
SURFACE_INITIATOR = "spec-engine-dashboard"

#: The surface this route writes configuration on. ``operator_confirmed``, which
#: is what unlocks the config-only sections — the autonomy ladder, the delivery
#: workflow, the argv the pipeline executes. That claim is sound ONLY because
#: :func:`_operator_only` has already refused every caller that is not a
#: signed-in human; bound to a name here so the claim has one place to be read,
#: asserted, and (if it ever changes) found.
WRITE_SURFACE = DASHBOARD_SURFACE

#: The surface a setup apply writes on: the engine's own setup-assistant surface,
#: the SAME constant the Engine_MCP_Server's ``apply_setup`` uses. Shared rather
#: than declared per door, because the surface name is recorded beside the approver
#: in the store's durable write record and lands in the merged document's fenced
#: paths — two doors writing one approved plan under two surface names would make
#: that record answer "which surface applied this" differently depending on where
#: the operator happened to be standing.
#:
#: It is operator-confirmed for a reason narrower than it looks: a setup patch
#: necessarily touches config-only paths (a project's workflow, a source's autonomy
#: grid), so an unconfirmed surface could not complete setup at all. The authority
#: is bounded by construction — the patch is built by the engine from an offered,
#: approved plan and no caller-supplied patch reaches this path.
SETUP_SURFACE = SETUP_ASSISTANT_SURFACE


# --- refusals and audit -----------------------------------------------------


def _refuse(code: str, error: str, *, status: int = 400) -> web.Response:
    """A refusal carrying a machine-readable ``code`` beside its human text.

    Backend-owned strings have no localization catalog, so the code is the part a
    client branches on and the text is for a log or a fallback.
    """
    return web.json_response({"code": code, "error": error}, status=status)


def _sel_event(
    *,
    caller: str,
    operation: str,
    outcome: str,
    resources: str = "",
    error: str = "",
) -> None:
    """Record one security-relevant event, best effort.

    Best effort by the same rule the rest of the dashboard applies: this is the
    RECORD of an operator action, not a precondition for it, and an audit trail
    that cannot be written must not turn a completed stop into a reported
    failure. The denial paths below still return their 403 whether or not this
    lands, which is what keeps the guard fail-closed while the record is not.
    """
    try:
        sel().log_api_access(
            caller=caller,
            operation=operation,
            outcome=outcome,
            source="app_isolation" if outcome == "denied" else "dashboard",
            resources=resources,
            error=error,
        )
    except Exception:  # noqa: BLE001 - an unwritable trail must not void the refusal
        logger.warning("spec-engine: SEL audit failed for %s", operation, exc_info=True)


# --- gates ------------------------------------------------------------------


def _require_enabled(handler: Handler) -> Handler:
    """Deny when the app is disabled.

    ``is_app_enabled`` reads ``installed.json`` synchronously, so it goes to a
    thread like every other read on this surface.
    """

    @wraps(handler)
    async def _wrapped(request: web.Request) -> web.StreamResponse:
        if not await asyncio.to_thread(is_app_enabled, APP_NAME):
            return _refuse("app_disabled", f"{APP_NAME} is disabled", status=403)
        return await handler(request)

    return _wrapped


def _require_auth(handler: Handler) -> Handler:
    """Require an identity the middleware set. 401 otherwise.

    Trusts only ``request["user"]``: a body-supplied or header-supplied name is
    whatever the caller typed.
    """

    @wraps(handler)
    async def _wrapped(request: web.Request) -> web.StreamResponse:
        if request.get("user") is None:
            return _refuse("unauthorized", "this surface needs a signed-in session", status=401)
        return await handler(request)

    return _wrapped


def _operator_only(handler: Handler, *, operation: str) -> Handler:
    """Require a signed-in dashboard session and refuse app-minted tokens.

    Both refusals, because there are two callers to keep out of a write that can
    widen authority:

    * **anonymous** — 401, the same as every read here.
    * **an app token** — 403 plus a security event. This is the refusal the
      module docstring's escalation is about: the token layer grants an app its
      own ``/api/apps/<name>/*`` namespace with no manifest entry needed, so
      scope enforcement passes and ``request["user"]`` is truthy. The only place
      a human and an app can still be told apart is ``request["app"]``, which the
      middleware sets from the verified token and no caller can forge.

    Applied at REGISTRATION rather than inside each handler, so the guard is a
    property of the route table: a mutating route added without it is visible in
    the router, which is what :data:`OPERATOR_GUARD_MARK` lets the suite assert.
    """

    @wraps(handler)
    async def _wrapped(request: web.Request) -> web.StreamResponse:
        if request.get("user") is None:
            return _refuse("unauthorized", "this control needs a signed-in session", status=401)
        app_caller = request.get("app")
        if app_caller:
            _sel_event(
                caller=str(app_caller),
                operation=operation,
                outcome="denied",
                resources=request.path,
                error="app tokens may not operate the spec engine",
            )
            return _refuse(
                "dashboard_user_required",
                "this control is only reachable from a signed-in dashboard session",
                status=403,
            )
        return await handler(request)

    return _wrapped


def _read(handler: Handler) -> Handler:
    """Compose the gates a READ passes: enabled, then authenticated."""
    return _require_enabled(_require_auth(handler))


def _mutate(handler: Handler, *, operation: str) -> Handler:
    """Compose the gates an operator-only route passes: enabled, then the guard.

    Mutations all pass through here, and so do the two setup READS
    (``/setup/inspect``, ``/setup/plan``): they write nothing, but they read a
    project path the caller names, so an app-minted token would gain a
    filesystem reader inside this app's namespace — the module docstring
    carries the full reasoning.

    The composed handler carries :data:`OPERATOR_GUARD_MARK` so the guard can be
    read back off the registered route rather than taken on trust. Stamped on the
    OUTER wrapper explicitly instead of relying on ``functools.wraps`` copying
    ``__dict__`` outward, which is true but too subtle to rest a security
    assertion on.
    """
    wrapped = _require_enabled(_operator_only(handler, operation=operation))
    setattr(wrapped, OPERATOR_GUARD_MARK, operation)
    return wrapped


# --- request parsing --------------------------------------------------------


async def _json_object(request: web.Request) -> dict[str, Any] | web.Response:
    """The request body as a JSON object, or a refusal.

    Valid JSON is not necessarily an object: ``[]``, ``null`` and bare scalars all
    parse and then fail on ``.get()`` as a 500 instead of a 400.
    """
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - aiohttp raises several unrelated classes here
        return _refuse("bad_json", "request body must be a JSON object")
    if not isinstance(body, dict):
        return _refuse("bad_json", "request body must be a JSON object")
    return body


def _text(payload: Mapping[str, Any], field: str) -> str:
    return str(payload.get(field, "")).strip()


def _whole(payload: Mapping[str, Any], field: str) -> int | None:
    """*field* as an int, or ``None`` when it is absent or not one.

    A bool is refused: ``True`` is an ``int`` in Python, so a workspace id of
    ``True`` would resolve to row 1.
    """
    value = payload.get(field)
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


def _actor(request: web.Request) -> str:
    """The authenticated identity, for the engine's own records."""
    return str(request.get("user") or "")


# --- engine handles ---------------------------------------------------------
#
# All BLOCKING. Constructing a StateStore opens SQLite and migrates the schema,
# and constructing a ConfigStore resolves the data home, so every one of these is
# called from inside a worker thread and inside the caller's try — a store built
# on the loop stalls the gateway, and one built outside the try turns an
# unreadable database into a bare 500.


def _config_store() -> ConfigStore:
    """BLOCKING — the engine's config store for the live data home.

    Constructed per call rather than cached: the document is read from disk on
    every access anyway, and a cached store pins a root that ``KIROCREW_HOME``
    may have moved since.
    """
    return ConfigStore()


def _state_store() -> engine_state.StateStore:
    """BLOCKING — the engine's state database for the live data home."""
    return engine_state.StateStore()


def _audit_log(store: engine_state.StateStore) -> engine_audit.AuditLog:
    """BLOCKING-safe — the per-spec audit log rooted beside *store*'s state."""
    return engine_audit.AuditLog(store.root)


def _review_queue() -> engine_review_queue.ReviewQueue:
    """BLOCKING — the engine's Review_Queue, WITH its audit log.

    The audit log is not decoration. Every action this surface drives is a
    privileged manual override, and the engine REFUSES a feedback release
    outright when its run machine records to nowhere — so a queue built without
    the log would not skip the trail quietly, it would fail. Passing it is how
    the trail gets written, and no branch here proceeds without it.
    """
    store = _state_store()
    machine = engine_runs.RunMachine(store, _config_store(), audit=_audit_log(store))
    return engine_review_queue.ReviewQueue(machine)


# --- configuration ----------------------------------------------------------


def _advisory(warning: ConfigWarning) -> dict[str, Any]:
    """One advisory as its identifier, its location, and its text.

    ``requires_acknowledgment`` travels because an advisory a human must say
    "yes, I know" to is a different obligation from one they only have to read,
    and a surface cannot tell them apart otherwise.
    """
    return {
        "code": warning.code,
        "path": warning.path,
        "message": warning.message,
        "project": warning.project,
        "requires_acknowledgment": warning.requires_acknowledgment,
    }


def _config_snapshot(store: ConfigStore) -> dict[str, Any]:
    """BLOCKING — the persisted configuration as an operator may see it.

    Built from ONE read of the document. ``ConfigStore.validate`` and
    ``ConfigStore.advisories`` each re-read the file, so composing the reply from
    those three accessors would let a write landing between them produce a report
    describing two different documents — errors from one, advisories from the
    next. The document itself is never torn (the write is atomic under an
    exclusive lock); the REPORT about it was, so the document is read once and
    the two derivations are handed that value.

    ``configured`` answers the question a first-run caller actually has — is
    there configuration yet — separately from ``document``, because an absent
    file and an empty one both serialize to ``{}`` and only one of them means
    "offer the setup assistant".

    ``elided`` lists the dotted paths whose value was withheld, so a page can
    tell an elision from a literal marker somebody typed, and can report
    "a token is configured here" without ever holding the token.

    ``elided_marker`` is the substituted value itself, relayed rather than left for
    a client to hardcode. An editor has to recognise it to keep it out of a patch --
    saving the marker back would replace a live credential with it, and the
    document would stay valid, so nothing downstream would report the loss. A
    client-side copy of the string is a second spelling of one constant, and the
    two drifting apart breaks exactly that protection.
    """
    document = store.document()
    elided = elide_secrets(document)
    return {
        "configured": store.path.is_file(),
        "path": str(store.path),
        "document": elided.document,
        "elided": list(elided.paths),
        "elided_marker": ELIDED,
        "errors": [
            {"path": error.path, "message": error.message}
            for error in validate_config_document(document)
        ],
        "advisories": [_advisory(warning) for warning in document_warnings(document)],
        # The paths the engine fences to an operator-confirmed surface. Relayed so
        # a panel can mark them instead of keeping a second copy of the list.
        "config_only_paths": list(CONFIG_ONLY_PATHS),
    }


async def handle_get_config(request: web.Request) -> web.Response:
    """GET the persisted configuration, credential values elided."""
    try:
        payload = await asyncio.to_thread(lambda: _config_snapshot(_config_store()))
    except ConfigLoadError as exc:
        # A document a human has to repair. Reported as its own refusal rather
        # than as an empty configuration: "nothing is configured" would send the
        # operator to the setup assistant, which would then refuse to write over
        # a file it cannot parse.
        return _refuse("config_unreadable", str(exc), status=409)
    except OSError as exc:
        return _refuse("config_unreadable", str(exc), status=503)
    return web.json_response(payload)


def _write_config(patch: Mapping[str, Any], actor: str) -> tuple[dict[str, Any], list[dict]]:
    """BLOCKING — persist *patch* through the engine's ONE write path.

    ``ConfigStore.write`` is that path: it merges, validates, serializes with
    ``json.dumps(..., indent=2, sort_keys=True)`` and writes atomically under an
    exclusive inter-process lock. The Engine_MCP_Server's ``write_config`` tool
    calls the same method, which is what makes the two surfaces produce a
    byte-identical ``config.json`` for a patch both accept — the property holds
    because there is one serializer, not because two were kept in step.

    The surface is :data:`WRITE_SURFACE`, which claims ``operator_confirmed``
    and so unlocks the config-only sections. That claim is only true because
    :func:`_operator_only` has already refused every caller that is not a
    signed-in human.

    Advisories are collected rather than dropped: the write path raises them at
    the moment a human is present and looking at the setting, and a reply that
    swallowed them would arm an unattended behaviour with nobody told.
    """
    collected: list[ConfigWarning] = []
    merged = _config_store().write(
        patch,
        surface=WRITE_SURFACE,
        actor=actor or None,
        warn=collected.append,
    )
    return elide_secrets(merged).document, [_advisory(warning) for warning in collected]


async def handle_put_config(request: web.Request) -> web.Response:
    """Persist a configuration patch through the engine's single write path.

    Every rule an operator can trip — an unknown key, an out-of-range value, a
    setting written at a scope it is not overridable at — is enforced by the
    engine and reported here by path, so this handler keeps no validation of its
    own to drift out of step.
    """
    body = await _json_object(request)
    if isinstance(body, web.Response):
        return body
    # `patch` is optional: a caller may post the patch as the whole body, which
    # is what the MCP tool's argument shape looks like.
    patch = body.get("patch", body)
    if not isinstance(patch, dict):
        return _refuse("bad_patch", "patch must be a JSON object")
    actor = _actor(request)
    try:
        document, advisories = await asyncio.to_thread(_write_config, patch, actor)
    except ConfigWriteRefused as exc:
        # The engine's surface fence. Unreachable while :data:`WRITE_SURFACE` is
        # operator-confirmed -- kept because without it a future non-confirmed
        # surface would render a legitimate engine refusal as a 503 "write failed"
        # and send the operator to fix a disk problem that does not exist. It must
        # stay ABOVE the OSError arm: ConfigWriteRefused derives PermissionError,
        # which derives OSError, so reordering these two turns the refusal into a
        # 503. Reported as a refusal rather than a validation failure -- nothing
        # about the values is wrong, and an operator told "invalid" would keep
        # editing them.
        return _refuse("config_write_refused", str(exc), status=403)
    except ConfigValidationError as exc:
        return _refuse("config_invalid", "; ".join(str(error) for error in exc.errors), status=422)
    except ConfigRecordError as exc:
        # The document WAS persisted and nothing recorded who changed it. Loud,
        # and not reported as a plain success: those two facts together are the
        # only pair that leads to the right reaction.
        return _refuse("config_write_unrecorded", str(exc), status=500)
    except (ConfigLoadError, OSError) as exc:
        return _refuse("config_write_failed", str(exc), status=503)
    _sel_event(
        caller=actor or SURFACE_INITIATOR,
        operation="spec_engine_config_write",
        outcome="success",
        resources=",".join(sorted(str(key) for key in patch)),
    )
    return web.json_response({"ok": True, "document": document, "advisories": advisories})


# --- the resolved read beside the document ----------------------------------


def _resolved_snapshot(project: str | None, source: str | None) -> dict[str, Any]:
    """BLOCKING — every setting's value in force, and the role plan it produces.

    A READ of the document the config route writes, never a second write path.
    That is the whole point of it: an operator editing ``config.json`` is editing
    one layer of a five-layer precedence (bundled default, app, the profile a
    project selected, project, source), and a surface showing only the document
    cannot answer "what is actually in force here" — which is the question every
    edit is really about. ``EffectiveValue`` carries the origin and the declaring
    path beside each value, so a ``2`` that somebody chose is distinguishable from
    a ``2`` the app shipped; those call for opposite actions.

    Built from ONE read of the document, for the same reason
    :func:`_config_snapshot` is: ``ConfigStore.effective_settings`` and
    ``RolePlan.for_run`` each re-read the file, so composing this reply from those
    two accessors would let a write landing between them resolve the settings from
    one document and the roles from the next.

    ``roles`` is the engine's own ``RolePlan.detail()``, relayed. The role table a
    surface renders must be the resolution a DISPATCH would use — including the
    fallback reasons, which are four distinct conditions fixed in four different
    places — and a surface that re-derived "which model will the review role run
    on" from the raw profile object would answer differently from the run.

    ``role_order`` travels because a JSON object has no order a client may rely on,
    and the engine's role order is meaningful (it is the order the profiles declare
    and the audit records).
    """
    store = _config_store()
    document = store.document()
    plan = RolePlan.from_document(document, project=project)
    resolved = resolve_all(document, project=project, source=source)
    return {
        "configured": store.path.is_file(),
        "project": project,
        "source": source,
        "settings": [resolved[key].to_json_object() for key in sorted(resolved)],
        "roles": plan.detail(),
        "role_order": list(ROLES),
    }


async def handle_get_resolved_config(request: web.Request) -> web.Response:
    """GET the value in force for every setting, with the origin of each."""
    project = request.query.get("project") or None
    source = request.query.get("source") or None
    try:
        payload = await asyncio.to_thread(_resolved_snapshot, project, source)
    except ConfigValidationError as exc:
        # An explicitly stored value that fails the setting's own validation.
        # Resolution RAISES rather than falling through to the default, and this
        # arm keeps that distinction: the document has a value nobody can act on,
        # which is a repair, not an unreadable file and not an empty resolution.
        # It must precede any ValueError arm -- ConfigValidationError derives
        # ValueError.
        return _refuse("config_invalid", "; ".join(str(error) for error in exc.errors), status=422)
    except ConfigLoadError as exc:
        return _refuse("config_unreadable", str(exc), status=409)
    except OSError as exc:
        return _refuse("config_unreadable", str(exc), status=503)
    return web.json_response(payload)


# --- the setup assistant ----------------------------------------------------


def _setup_subject(payload: Mapping[str, Any]) -> tuple[Path, str]:
    """The project root and configuration name a setup call names.

    Both come from :mod:`..engine_mcp.setup_surface` rather than from a spelling
    here, because they are hashed into the ``plan_id``: a root resolved one way at
    this door and another way at the MCP door would compute two identities for one
    project, and the apply would then refuse a plan the operator had just read.
    """
    root = setup_surface.setup_root(_text(payload, "project"))
    name = payload.get("name")
    return root, setup_surface.project_name(root, str(name) if name is not None else None)


def _inspect_setup(payload: Mapping[str, Any]) -> dict[str, Any]:
    """BLOCKING — read the project and report evidence, inferences, questions.

    Reads the caller's project tree (steering notes, docs, CI and build files, the
    git remote) and the PATH probes each offered preset's prerequisites make.
    Writes nothing — not the config document and not the state store.
    """
    root, name = _setup_subject(payload)
    plan = engine_setup.propose_setup(root, project=name)
    return setup_surface.inspection_payload(plan, root=root)


def _plan_envelope(
    payload: Mapping[str, Any], answers: Mapping[str, Any]
) -> tuple[engine_setup.SetupPlan, engine_setup.SetupAnswers, setup_surface.SetupPlanEnvelope]:
    """BLOCKING — recompute the plan and its envelope from the arguments alone.

    The one place a plan is built for both the plan and the apply handlers, so the
    two cannot compute different plans from one set of arguments — which is the
    only way the identity check could pass while the write differed from the plan
    the operator approved.
    """
    root, name = _setup_subject(payload)
    plan = engine_setup.propose_setup(root, project=name)
    parsed = setup_surface.answers_from_arguments(answers)
    patch = engine_setup.setup_patch(plan, parsed)
    return plan, parsed, setup_surface.plan_envelope(plan, parsed, patch)


def _plan_setup(payload: Mapping[str, Any], answers: Mapping[str, Any]) -> dict[str, Any]:
    """BLOCKING — compute the patch these answers would write. Writes nothing.

    Every gate the apply would fail is evaluated here, so an operator learns that a
    rung is unanswered or a preset was never offered BEFORE they are asked to put
    their name to it.
    """
    _, _, envelope = _plan_envelope(payload, answers)
    return envelope.to_json_object()


def _apply_setup(
    payload: Mapping[str, Any], answers: Mapping[str, Any], plan_id: str, approver: str
) -> dict[str, Any]:
    """BLOCKING — write the plan identified by *plan_id*, on *approver*'s authority.

    Two refusals precede the write, in this order, and neither writes anything:

    * an absent *approver* — the plan writes the commands a project runs
      unattended and the rung it runs them at, so the human who accepted it is
      named rather than implied by the session. Checked FIRST, before the project
      is even read, so a call with no identity cannot cost a filesystem walk;
    * a ``plan_id`` that is not the identity these inputs produce now — the
      project's evidence or the answers have changed since the plan was computed,
      so applying it would write something the operator never read.

    The approver is the identity the caller states, and it is NOT the session: an
    operator may apply a plan a colleague approved, and the engine records the
    approver beside the surface name in the store's durable write record. The
    session is what the operator guard verified in order to reach here at all, and
    it is recorded separately in the security event.
    """
    identity = setup_surface.require_approver(approver)
    plan, parsed, envelope = _plan_envelope(payload, answers)
    setup_surface.require_plan_identity(plan_id, envelope.plan_id)
    result = engine_setup.apply_setup(
        _config_store(), plan, parsed, surface=SETUP_SURFACE, actor=identity
    )
    return setup_surface.apply_payload(envelope, result, identity)


def _setup_refusal(exc: Exception) -> web.Response:
    """A setup refusal as one status with the engine's own refusal code inside.

    One status for all four refusals (an absent approver, a stale plan, an
    unanswered gate, an inference of a subject that is only ever asked) because
    they differ in what the caller must do next and not in who may act: every one
    of them means the flow did not proceed and NOTHING was written. The actionable
    part is ``refused``, which carries the same vocabulary the Engine_MCP_Server's
    setup tools return, so a client that learned one door's refusals understands
    the other's.
    """
    payload = setup_surface.refusal_payload(exc)
    if payload is None:  # pragma: no cover - callers only pass classified refusals
        raise exc
    return web.json_response({"code": "setup_refused", "error": str(exc), **payload}, status=409)


async def _setup_arguments(
    request: web.Request,
) -> tuple[dict[str, Any], dict[str, Any]] | web.Response:
    """The body and its ``answers`` object, or a refusal.

    ``answers`` is required as an OBJECT rather than defaulted to empty: an empty
    answer set is a real thing to send (it refuses, naming the unchosen cost
    profile), but a caller that sent ``answers: null`` or omitted the key has a bug
    this must not translate into "the operator answered nothing".
    """
    body = await _json_object(request)
    if isinstance(body, web.Response):
        return body
    if not _text(body, "project"):
        return _refuse("field_required", "a setup call needs the project path to inspect")
    answers = body.get("answers")
    if not isinstance(answers, dict):
        return _refuse("bad_answers", "answers must be an object holding the operator's answers")
    return body, answers


async def handle_post_setup_inspect(request: web.Request) -> web.Response:
    """Inspect a project: evidence read, values inferred, questions left to ask."""
    body = await _json_object(request)
    if isinstance(body, web.Response):
        return body
    if not _text(body, "project"):
        return _refuse("field_required", "a setup inspection needs the project path to read")
    try:
        payload = await asyncio.to_thread(_inspect_setup, body)
    except (engine_setup.InferredSubjectRefused, engine_setup.SetupApprovalRequired) as exc:
        # Ahead of the two arms below, and the ordering is load-bearing:
        # InferredSubjectRefused derives ValueError and SetupApprovalRequired
        # derives PermissionError (so OSError). Below them, a decision the engine
        # made would be reported as a malformed request or as a disk failure.
        return _setup_refusal(exc)
    except ValueError as exc:
        # An unnameable project (a filesystem root has no final segment to fall
        # back on). A malformed call, not a decision.
        return _refuse("bad_project", str(exc))
    except OSError as exc:
        return _refuse("setup_read_failed", str(exc), status=503)
    return web.json_response(payload)


async def handle_post_setup_plan(request: web.Request) -> web.Response:
    """Compute the configuration plan a set of answers produces. Writes nothing."""
    parsed = await _setup_arguments(request)
    if isinstance(parsed, web.Response):
        return parsed
    body, answers = parsed
    try:
        payload = await asyncio.to_thread(_plan_setup, body, answers)
    except (engine_setup.InferredSubjectRefused, engine_setup.SetupApprovalRequired) as exc:
        return _setup_refusal(exc)
    except ValueError as exc:
        # An unknown cost profile name, an unknown autonomy rung, an unnameable
        # project: the caller learns which names exist rather than having a bad key
        # silently dropped into a rung the engine then refuses as "missing".
        return _refuse("bad_answers", str(exc))
    except OSError as exc:
        return _refuse("setup_read_failed", str(exc), status=503)
    return web.json_response(payload)


async def handle_post_setup_apply(request: web.Request) -> web.Response:
    """Apply a plan by its identity, on a named human approver's authority."""
    parsed = await _setup_arguments(request)
    if isinstance(parsed, web.Response):
        return parsed
    body, answers = parsed
    plan_id = _text(body, "plan_id")
    approver = _text(body, "approver")
    actor = _actor(request)
    try:
        payload = await asyncio.to_thread(_apply_setup, body, answers, plan_id, approver)
    except (engine_setup.InferredSubjectRefused, engine_setup.SetupApprovalRequired) as exc:
        # Covers the absent approver and the stale plan too: both refusals are
        # SetupApprovalRequired subclasses precisely so that every catch which
        # already refuses to write keeps refusing.
        return _setup_refusal(exc)
    except ConfigWriteRefused as exc:
        # Unreachable while SETUP_SURFACE is operator-confirmed, and kept for the
        # same reason as its sibling on the config write: without it, a future
        # unconfirmed surface would render a legitimate engine refusal as a 503
        # disk problem. Above the OSError arm because ConfigWriteRefused derives
        # PermissionError, which derives OSError.
        return _refuse("config_write_refused", str(exc), status=403)
    except ConfigValidationError as exc:
        return _refuse("config_invalid", "; ".join(str(error) for error in exc.errors), status=422)
    except ConfigRecordError as exc:
        # The plan WAS applied and nothing recorded who approved it. That pair of
        # facts is the whole reason the approver is demanded, so it is reported as
        # a failure rather than as an ordinary apply.
        return _refuse("config_write_unrecorded", str(exc), status=500)
    except ValueError as exc:
        return _refuse("bad_answers", str(exc))
    except (ConfigLoadError, OSError) as exc:
        return _refuse("setup_apply_failed", str(exc), status=503)
    _sel_event(
        caller=actor or SURFACE_INITIATOR,
        operation="spec_engine_setup_apply",
        outcome="success",
        # The approver travels beside the session, never instead of it: the session
        # is who acted and the approver is who authorized it, and a record holding
        # one of them answers the wrong question.
        resources=f"approver={approver} plan={plan_id}",
    )
    return web.json_response(payload)


# --- the kill switch --------------------------------------------------------


def _kill_switch_snapshot() -> dict[str, Any]:
    """BLOCKING — whether unattended work is stopped, and what a stop would stop.

    ``stoppable`` is the runs the switch has something to do about, each with the
    credits it has already consumed, so the control names its own blast radius
    BEFORE it is thrown rather than after.
    """
    store = _state_store()
    state = engine_switch.KillSwitch(store.root).read()
    records = engine_killswitch.stoppable_runs(store)
    return {
        "switch": state.to_json_object(),
        "stoppable": [
            {
                "run_id": record.run_id,
                "spec_key": record.spec_key,
                "source": record.source,
                "state": record.state,
                "cost_credits": record.cost_credits,
            }
            for record in records
        ],
        "stoppable_credits": round(sum(record.cost_credits for record in records), 4),
    }


async def handle_get_kill_switch(request: web.Request) -> web.Response:
    """GET the kill switch's state and the runs a stop would park."""
    try:
        payload = await asyncio.to_thread(_kill_switch_snapshot)
    except (OSError, ValueError, engine_state.StateError) as exc:
        return _refuse("kill_switch_unreadable", str(exc), status=503)
    return web.json_response(payload)


def _engage(initiator: str, reason: str) -> engine_killswitch.KillSwitchReport:
    """BLOCKING — throw the switch and park every stoppable run."""
    store = _state_store()
    config = _config_store()
    audit = _audit_log(store)
    machine = engine_runs.RunMachine(store, config, audit=audit)
    return engine_killswitch.engage_kill_switch(
        state=store,
        config=config,
        initiator=initiator,
        reason=reason,
        machine=machine,
        audit=audit,
    )


def _release(initiator: str) -> tuple[bool, dict[str, Any]]:
    """BLOCKING — release the switch, and read back the state that resulted.

    The state is READ BACK from the flag rather than assumed from the return
    value: "released" is a claim about a file, and an operator who is told the
    stop is lifted while it is still in force will keep waiting for work that
    never starts.
    """
    store = _state_store()
    changed = engine_killswitch.release_kill_switch(
        state=store,
        initiator=initiator,
        audit=_audit_log(store),
    )
    return changed, engine_switch.KillSwitch(store.root).read().to_json_object()


async def handle_post_kill_switch(request: web.Request) -> web.Response:
    """Engage or release the kill switch.

    Engaging parks every stoppable run and reports what it stopped. Releasing
    only lets NEW work start and resumes nothing — that is the engine's
    behaviour, and it is reported as such rather than smoothed over, because
    "nothing restarts on its own" is the thing an operator most often assumes
    otherwise.

    Both directions are attributed to the authenticated session, never to a name
    in the body: a stop recorded against whatever the caller typed records
    nothing.
    """
    body = await _json_object(request)
    if isinstance(body, web.Response):
        return body
    action = body.get("action")
    if action not in ("engage", "release"):
        return _refuse("bad_action", "action must be 'engage' or 'release'")
    reason = body.get("reason", "")
    if not isinstance(reason, str):
        return _refuse("bad_reason", "reason must be a string")
    initiator = _actor(request) or SURFACE_INITIATOR

    if action == "release":
        try:
            changed, switch = await asyncio.to_thread(_release, initiator)
        except (OSError, ValueError, engine_state.StateError) as exc:
            # StateError is the arm that matters: both failure modes this branch
            # exists for -- the audit append and the flag unlink -- raise
            # StatePersistenceError, which derives StateError and neither OSError
            # nor ValueError. A release whose trail could not be written leaves
            # the switch engaged, and says so.
            return _refuse("release_failed", str(exc), status=503)
        _sel_event(
            caller=initiator,
            operation="spec_engine_kill_switch_release",
            outcome="success",
        )
        # `resumed` is stated rather than left out: an empty list is the answer,
        # not a missing field.
        return web.json_response(
            {"ok": True, "action": "release", "changed": changed, "switch": switch, "resumed": []}
        )

    try:
        report = await asyncio.to_thread(_engage, initiator, reason)
    except (OSError, ValueError, engine_state.StateError, ConfigLoadError) as exc:
        # ConfigLoadError is the arm easiest to miss and the worst to: engage
        # reads the budget through the config document, so an unparseable
        # document raises AFTER the flag has persisted. Uncaught, the operator
        # reads a 500 while the stop is silently in force.
        return _refuse("engage_failed", str(exc), status=503)
    _sel_event(
        caller=initiator,
        operation="spec_engine_kill_switch_engage",
        outcome="success",
        resources=f"halted={len(report.halted)}",
    )
    return web.json_response(
        {
            "ok": True,
            "action": "engage",
            "already_engaged": report.already_engaged,
            "switch": report.state.to_json_object(),
            "halted": [
                {
                    "run_id": run.run_id,
                    "parked": run.parked,
                    "cost_credits": run.consumed_credits,
                }
                for run in report.halted
            ],
            "total_credits": round(report.total_credits, 4),
            "description": report.describe(),
        }
    )


# --- run spend --------------------------------------------------------------


def _run_spend(run_id: str) -> dict[str, Any] | None:
    """BLOCKING — one run's attributed spend and the ceiling in force for it.

    ``credits`` is the engine's own total: every stamped session's metered
    credits plus every credit an external capability provider declared. It is
    deliberately not a sum a surface assembles from the rows it happened to
    fetch, because a browser-side total silently disagrees with the number the
    ceiling compares — and the split below is what shows a caller that
    out-of-session spend is INSIDE the total rather than beside it.
    """
    store = _state_store()
    record = store.get_run(run_id)
    if record is None:
        return None
    specs = {spec.spec_key: spec for spec in store.list_specs(include_archived=True)}
    spec = specs.get(record.spec_key)
    project = spec.project if spec is not None else None
    spend = engine_ledger.RunAccounting(store).spend(run_id)
    ceiling = _config_store().effective(engine_ceiling.CEILING_SETTING, project=project)
    return {
        "run_id": run_id,
        "project": project,
        "spec": spec.name if spec is not None else "",
        "state": record.state,
        "source": record.source,
        "credits": round(spend.total_credits, 4),
        "metered_credits": round(spend.metered_credits, 4),
        # Spend that happened OUTSIDE any host session -- the half a total over
        # turn rows would miss entirely.
        "declared_credits": round(spend.declared_credits, 4),
        "turns": spend.turns,
        "sessions": len(spend.sessions),
        # The run row's own stored figure, reported beside the total so a surface
        # can show the two agreeing rather than silently choosing one. The
        # ceiling compares `credits`.
        "recorded_credits": round(record.cost_credits, 4),
        "ceiling": {
            "value": ceiling.value,
            "origin": ceiling.origin.value,
            "declared_at": ceiling.declared_at,
        },
    }


async def handle_get_run_spend(request: web.Request) -> web.Response:
    """GET one run's attributed spend, with the ceiling it is judged against."""
    run_id = (request.query.get("run_id") or "").strip()
    if not run_id:
        return _refuse("field_required", "a spend view needs the run_id to report on")
    try:
        payload = await asyncio.to_thread(_run_spend, run_id)
    except (OSError, ValueError, engine_state.StateError, ConfigLoadError) as exc:
        return _refuse("spend_unreadable", str(exc), status=503)
    if payload is None:
        return _refuse("run_unknown", "no run has that id", status=404)
    return web.json_response(payload)


# --- the review queue -------------------------------------------------------


def _queue_snapshot(project: str | None) -> dict[str, Any]:
    """BLOCKING — the Review_Queue, flat and grouped by run state.

    Relayed from the engine's own snapshot, so the credits an operator reads are
    the ones the ceiling and the kill switch account against, and the grouping is
    ``QueueSnapshot.grouped``'s rather than a second grouping assembled per
    surface — two groupings of one run drift, and an operator cannot tell which
    is current.
    """
    snapshot = _review_queue().snapshot(project=project)
    payload = snapshot.to_json_object()
    payload["total_credits"] = round(sum(entry.cost_credits for entry in snapshot), 4)
    return payload


async def handle_get_queue(request: web.Request) -> web.Response:
    """GET every run waiting on a person, optionally narrowed to one project."""
    project = request.query.get("project") or None
    try:
        payload = await asyncio.to_thread(_queue_snapshot, project)
    except (OSError, ValueError, engine_state.StateError, ConfigLoadError) as exc:
        return _refuse("queue_unreadable", str(exc), status=503)
    return web.json_response(payload)


def _release_feedback(ref: engine_state.SpecRef, run_id: str, comment_id: str, actor: str) -> bool:
    """BLOCKING — release one held reviewer comment."""
    return _review_queue().release_quarantined_feedback(ref, run_id, comment_id, actor=actor)


async def handle_post_release_feedback(request: web.Request) -> web.Response:
    """Release one held reviewer comment for a queue row.

    The human gate on quarantined content, so the comment IDENTIFIER is all that
    crosses this boundary. The comment TEXT never does: it is an outside
    submitter's data, and this route must not become a second place it is copied
    to.
    """
    body = await _json_object(request)
    if isinstance(body, web.Response):
        return body
    project = _text(body, "project")
    spec = _text(body, "spec")
    run_id = _text(body, "run_id")
    comment_id = _text(body, "comment_id")
    missing = [
        name
        for name, value in (
            ("project", project),
            ("spec", spec),
            ("run_id", run_id),
            ("comment_id", comment_id),
        )
        if not value
    ]
    if missing:
        return _refuse("field_required", "releasing a held comment needs " + ", ".join(missing))
    # Constructed directly rather than through `SpecRef.of`: the project on a
    # queue row is already the resolved posix path the engine stored, and
    # re-resolving it against THIS process's filesystem would rewrite the
    # identity of a project whose path is a symlink or is not mounted here.
    ref = engine_state.SpecRef(project=project, name=spec)
    actor = _actor(request)
    try:
        released = await asyncio.to_thread(_release_feedback, ref, run_id, comment_id, actor)
    except engine_review_queue.ReviewFeedbackRefused as exc:
        # The engine's own refusal -- a queue whose run machine records nowhere --
        # reported rather than worked around. Derives Exception directly, so it is
        # named before the tuple below and would not be caught by it.
        return _refuse("release_refused", str(exc), status=409)
    except (OSError, ValueError, engine_state.StateError) as exc:
        return _refuse("release_failed", str(exc), status=503)
    _sel_event(
        caller=actor or SURFACE_INITIATOR,
        operation="spec_engine_release_feedback",
        outcome="success",
        resources=f"run={run_id}",
    )
    # False means nobody held that comment, so a click on a stale row is answered
    # rather than reported as a release that did not happen.
    return web.json_response({"ok": True, "released": released})


async def handle_post_redispatch(request: web.Request) -> web.Response:
    """Lift the suppression on one watched item, so the next poll dispatches it."""
    body = await _json_object(request)
    if isinstance(body, web.Response):
        return body
    source = _text(body, "source")
    item_id = _text(body, "item_id")
    generation = _whole(body, "generation")
    if not source or not item_id:
        return _refuse("field_required", "a re-dispatch needs source and item_id")
    if generation is None:
        return _refuse(
            "field_required",
            "a re-dispatch needs the generation it is lifting",
        )
    actor = _actor(request)
    try:
        lifted = await asyncio.to_thread(
            lambda: _review_queue().redispatch_item(source, item_id, generation=generation)
        )
    except (OSError, ValueError, engine_state.StateError) as exc:
        return _refuse("redispatch_failed", str(exc), status=503)
    _sel_event(
        caller=actor or SURFACE_INITIATOR,
        operation="spec_engine_redispatch",
        outcome="success",
        resources=f"{source}:{item_id}",
    )
    return web.json_response({"ok": True, "lifted": lifted})


async def handle_post_clean_workspace(request: web.Request) -> web.Response:
    """Remove one ledger-recorded workspace: the retry for a kept teardown."""
    body = await _json_object(request)
    if isinstance(body, web.Response):
        return body
    workspace_id = _whole(body, "workspace_id")
    if workspace_id is None:
        return _refuse("field_required", "a cleanup needs the workspace_id to remove")
    force = body.get("force") is True
    actor = _actor(request)
    try:
        cleanup = await asyncio.to_thread(
            lambda: _review_queue().clean_workspace(workspace_id, force=force)
        )
    except (OSError, ValueError, engine_state.StateError) as exc:
        return _refuse("cleanup_failed", str(exc), status=503)
    _sel_event(
        caller=actor or SURFACE_INITIATOR,
        operation="spec_engine_clean_workspace",
        outcome="success",
        resources=f"workspace={workspace_id} force={force}",
    )
    # None from the engine means no ACTIVE row has that id, so a double click
    # reads as "nothing to do" rather than as a second removal.
    return web.json_response(
        {
            "ok": True,
            "removed": cleanup is not None,
            "cleanup": cleanup.to_json_object() if cleanup is not None else None,
        }
    )


async def handle_post_teardown(request: web.Request) -> web.Response:
    """Tear down every workspace a run recorded.

    Reports what it KEPT as well as what it removed, and does not call itself
    complete when anything was kept: a teardown that could not finish leaves a
    tree or a deployment standing, and reporting that as a success is how an
    environment outlives every record of itself.
    """
    body = await _json_object(request)
    if isinstance(body, web.Response):
        return body
    run_id = _text(body, "run_id")
    if not run_id:
        return _refuse("field_required", "a teardown needs the run_id to tear down")
    actor = _actor(request)
    try:
        report = await asyncio.to_thread(lambda: _review_queue().teardown_run_workspaces(run_id))
    except (OSError, ValueError, engine_state.StateError) as exc:
        return _refuse("teardown_failed", str(exc), status=503)
    _sel_event(
        caller=actor or SURFACE_INITIATOR,
        operation="spec_engine_teardown",
        outcome="success",
        resources=f"run={run_id} complete={report.complete}",
    )
    return web.json_response(
        {
            "ok": True,
            "complete": report.complete,
            # The kept ids, named at the top level rather than only inside the
            # report: a surface that has to dig for them will render the teardown
            # as done.
            "kept": [cleanup.workspace_id for cleanup in report.kept],
            "report": report.to_json_object(),
        }
    )


# --- registration -----------------------------------------------------------


def register_routes(app: web.Application) -> None:
    """Mount this app's routes on the gateway's aiohttp application.

    The builtin contract: full paths on the gateway's own router, returning
    ``None`` rather than a list of route descriptors.

    Nothing here touches disk or constructs a store. This function runs on every
    gateway boot, including boots where the app is disabled, and it runs inside a
    loop whose only tolerance is a ``ModuleNotFoundError`` naming the app package
    itself — anything else raised from here fails gateway startup for EVERY app,
    not just this one.
    """
    add = app.router.add_route

    add("GET", f"{PREFIX}/config", _read(handle_get_config))
    add("PUT", f"{PREFIX}/config", _mutate(handle_put_config, operation="config_write"))
    add("GET", f"{PREFIX}/config/resolved", _read(handle_get_resolved_config))

    # The setup flow. All three are POSTs behind the operator guard, and the first
    # two write nothing: they read a project path the CALLER names, so leaving them
    # on the read composer would hand an app-minted token a filesystem reader inside
    # this app's own namespace. See the module docstring.
    add(
        "POST",
        f"{PREFIX}/setup/inspect",
        _mutate(handle_post_setup_inspect, operation="setup_inspect"),
    )
    add("POST", f"{PREFIX}/setup/plan", _mutate(handle_post_setup_plan, operation="setup_plan"))
    add("POST", f"{PREFIX}/setup/apply", _mutate(handle_post_setup_apply, operation="setup_apply"))

    add("GET", f"{PREFIX}/kill-switch", _read(handle_get_kill_switch))
    add(
        "POST",
        f"{PREFIX}/kill-switch",
        _mutate(handle_post_kill_switch, operation="kill_switch"),
    )

    add("GET", f"{PREFIX}/run-spend", _read(handle_get_run_spend))
    add("GET", f"{PREFIX}/queue", _read(handle_get_queue))

    # Each queue action is a privileged manual override, so each is operator-only
    # and takes its actor from the authenticated session.
    add(
        "POST",
        f"{PREFIX}/queue/release-feedback",
        _mutate(handle_post_release_feedback, operation="release_feedback"),
    )
    add(
        "POST",
        f"{PREFIX}/queue/redispatch",
        _mutate(handle_post_redispatch, operation="redispatch"),
    )
    add(
        "POST",
        f"{PREFIX}/queue/clean-workspace",
        _mutate(handle_post_clean_workspace, operation="clean_workspace"),
    )
    add(
        "POST",
        f"{PREFIX}/queue/teardown",
        _mutate(handle_post_teardown, operation="teardown"),
    )
