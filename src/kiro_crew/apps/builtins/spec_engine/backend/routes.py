"""The Operator_Surface's backing routes: ``/api/apps/spec-engine/*``.

Mounted on the GATEWAY's own aiohttp application by the builtin route loop in
``dashboard/server.py``, which walks ``BUILTIN_NAMES``, imports each app package
and calls the package's ``register_routes(app)``. So requests arrive
same-origin, already through the gateway's auth middleware, and there is no
second process, port or proxy secret anywhere in this module.

**What this surface is for.** One page's worth of operation: the Review_Queue
and its four manual overrides, the configuration document, the kill switch, and
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

Reads deliberately stop at 401. An app token may READ this surface: the
configuration comes back with credential-classified values elided, and an agent
already has the same read through the Engine_MCP_Server's ``get_config``, so
refusing it here would buy nothing and diverge the two doors.

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
"""

from __future__ import annotations

import asyncio
import logging
from functools import wraps
from typing import Any, Awaitable, Callable, Mapping

from aiohttp import web

from kiro_crew.apps.manager import is_app_enabled
from kiro_crew.sel import sel

from ..engine import audit as engine_audit
from ..engine import review_queue as engine_review_queue
from ..engine import runs as engine_runs
from ..engine import state as engine_state
from ..engine.budget import ceiling as engine_ceiling
from ..engine.budget import killswitch as engine_killswitch
from ..engine.budget import ledger as engine_ledger
from ..engine.budget import switch as engine_switch
from ..engine.config import (
    APP_NAME,
    CONFIG_ONLY_PATHS,
    DASHBOARD_SURFACE,
    ConfigLoadError,
    ConfigRecordError,
    ConfigStore,
    ConfigValidationError,
    ConfigWarning,
    ConfigWriteRefused,
    document_warnings,
    elide_secrets,
    validate_config_document,
)

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
                "this control is only writable from a signed-in dashboard session",
                status=403,
            )
        return await handler(request)

    return _wrapped


def _read(handler: Handler) -> Handler:
    """Compose the gates a READ passes: enabled, then authenticated."""
    return _require_enabled(_require_auth(handler))


def _mutate(handler: Handler, *, operation: str) -> Handler:
    """Compose the gates a MUTATION passes: enabled, then operator-only.

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
    tell an elision from a literal ``<elided>`` somebody typed, and can report
    "a token is configured here" without ever holding the token.
    """
    document = store.document()
    elided = elide_secrets(document)
    return {
        "configured": store.path.is_file(),
        "path": str(store.path),
        "document": elided.document,
        "elided": list(elided.paths),
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
