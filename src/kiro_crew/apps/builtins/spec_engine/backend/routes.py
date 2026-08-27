"""The Operator_Surface's backing routes: ``/api/apps/spec-engine/*``.

Mounted on the GATEWAY's own aiohttp application by the builtin route loop in
``dashboard/server.py``, which walks ``BUILTIN_NAMES``, imports each app package
and calls the package's ``register_routes(app)``. So requests arrive
same-origin, already through the gateway's auth middleware, and there is no
second process, port or proxy secret anywhere in this module.

**What this surface is for.** One page's worth of operation: the Review_Queue
and its four manual overrides, the configuration document with the resolved read
and the per-source autonomy grid beside it, the setup flow that produces a first
document, the kill switch, and one run's attributed spend. Everything it does it
does by calling the Spec_Engine library — no rule, no threshold and no state
transition is decided here — so a surface reading a number reads the number the
engine enforces against rather than one this module recomputed.

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

Reads deliberately stop at 401. An app token may READ every read route,
and the rationale differs by route. The configuration reads are equivalence: the
values come back with credential-classified values elided, and an agent already
has the same read through the Engine_MCP_Server's ``get_config``, so refusing
them here would buy nothing and diverge the two doors. The form-vocabulary read
discloses less again: it projects the setting registry, the bundled source, gate
and cost-profile presets, the transport, delivery-stage, gate-position,
gate-severity, capability, engine-floor, workflow-preset, role, effort,
profile-pinnable-key and level vocabularies, and the pipeline-stage grouping
those are presented under — data the app package itself ships, carrying no stored
value at all, so it is strictly less than the
document read the same token already reaches. The capability-binding read is a
projection of that same document: it names the provider bound to each capability,
the program an external binding runs, and the path the binding was declared at,
and it carries no ``env`` entry at all — so nothing an operator stored there as a
credential can ride out on it. The per-source autonomy
grid joins them: it carries resolved autonomy levels and the configuration paths
that declared them, which is a projection of a document the same agent can
already read, and no credential-classified value appears in it. Reading how far
a source may run unattended is also not the authority to change it — that is the
config write, which is operator-only. The conformance POLL sits with them: it
reports a run's own verdict and per-check reasons about a program the same token
can already read the binding for, and it carries no part of that binding — the
binding travels as a digest precisely so an ``env`` value cannot ride out on a
report. STARTING a run is the opposite and is operator-only, because that spawns
the program. The queue, run-spend and kill-switch reads
have NO MCP equivalent — an app token gains reads here it could not otherwise
obtain (queue rows with source and item ids, per-run spend, kill-switch state
with stoppable run ids). That is accepted, not overlooked:
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
gateway and turn an unreadable database into a bare 500. The one handler with no
``to_thread`` is the form-vocabulary read, which assembles bundled constants in
memory: it opens nothing, so there is nothing to move off the loop and no
failure to map.

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
import hashlib
import json
import logging
import shutil
import uuid
from dataclasses import dataclass, replace
from functools import wraps
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, NoReturn

from aiohttp import web

from kiro_crew.apps.manager import is_app_enabled
from kiro_crew.effort import EFFORT_LEVELS
from kiro_crew.sel import sel

from ..engine import audit as engine_audit
from ..engine import local_analyzer
from ..engine import review_queue as engine_review_queue
from ..engine import runs as engine_runs
from ..engine import setup as engine_setup
from ..engine import state as engine_state
from ..engine.autonomy import (
    AUTONOMY_FIELD,
    AutonomyDecision,
    AutonomyLevel,
    AutonomyPolicy,
)
from ..engine.budget import ceiling as engine_ceiling
from ..engine.budget import killswitch as engine_killswitch
from ..engine.budget import ledger as engine_ledger
from ..engine.budget import switch as engine_switch
from ..engine.capabilities import (
    Binding,
    CapabilityRegistry,
    EngineFloorViolation,
    TransportCandidate,
    UnknownCapability,
    register_builtins,
    require_delegable,
    resolve_bindings,
    transport_for,
    verify,
)
from ..engine.capabilities.contracts import sanitized
from ..engine.config import (
    APP_NAME,
    AUTONOMY_LEVELS,
    CONFIG_ONLY_PATHS,
    COST_PROFILE_PRESET_NAMES,
    DASHBOARD_SURFACE,
    DELEGABLE_CAPABILITIES,
    DELIVERY_STAGES,
    ELIDED,
    ENGINE_FLOOR_CAPABILITIES,
    GATE_POSITIONS,
    GATE_SEVERITIES,
    PIPELINE_STAGES,
    PROFILE_SETTING_KEYS,
    ROLES,
    SETTINGS,
    SETUP_ASSISTANT_SURFACE,
    SPEC_TYPES,
    SUBMITTER_CLASSES,
    TRANSPORTS,
    ConfigLoadError,
    ConfigRecordError,
    ConfigStore,
    ConfigValidationError,
    ConfigWarning,
    ConfigWriteRefused,
    cost_profile_presets,
    document_warnings,
    elide_secrets,
    resolve_all,
    stage_capabilities,
    stage_setting_groups,
    validate_config_document,
)
from ..engine.config.schema import SECTION_SOURCES, SECTION_WORKFLOW, WORKFLOW_PRESETS_KEY
from ..engine.config.settings import SCOPE_PRECEDENCE, Setting
from ..engine.delivery import (
    DELIVERY_FLOW_STAGES,
    ISOLATE_STAGE,
    MAX_PRESET_NAME_CHARS,
    TEARDOWN_STAGE,
    WORKFLOW_PRESET_NAMES,
    DeliveryWorkflow,
    QualityGate,
    gate_presets,
    load_quality_gates,
    stage_origins,
)

# The engine's OWN reachability answer, the one a run's prerequisite gate reports
# against. Aliased the way ``engine/setup.py`` takes ``_program_check`` from the
# same module: a second PATH lookup written here could call a provider reachable
# that the gate then refuses, which is the one disagreement this surface must not
# be able to have.
from ..engine.prerequisites import _provider_checks as provider_checks
from ..engine.roles import RolePlan
from ..engine.watch.sources import (
    WATCH_SOURCE_PRESET_HOSTS,
    WATCH_SOURCE_PRESET_PROGRAMS,
    watch_source_presets,
)
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


# --- the capability bindings --------------------------------------------------


def _no_model_catalog() -> tuple[str, ...]:
    """The model resolver a description read never calls.

    :func:`register_builtins` needs one to construct the host model catalog
    provider, but :meth:`CapabilityRegistry.describe` reads provider IDENTITIES
    and serves no request — and the catalog's identity is deterministic whatever
    it would resolve — so nothing this returns can reach a payload. A real
    resolver here would be a host call made for a value nobody reads.
    """
    return ()


class _PinnedStore(ConfigStore):
    """A read-only store that serves ONE read of the document to every consumer.

    :meth:`ConfigStore.document` re-reads and re-parses the file on every call,
    and :meth:`ConfigStore.effective` rests on it, so a snapshot that joins
    several engine answers takes each of them from a DIFFERENT read of the same
    file. A write landing partway through produces a reply whose halves describe
    different documents — and for the capability join that is not a cosmetic
    skew: a binding read from the new document has no matching check in a report
    built from the old one, so the row renders ``reachable: null``, which this
    payload's own contract means "builtin, not applicable". A configured external
    provider would be reported as a builtin.

    Everything the join needs routes through the store, so pinning one read here
    pins all of them: :meth:`CapabilityRegistry.bindings`,
    :meth:`~CapabilityRegistry.describe` (including the per-capability
    ``timeout_for``, which resolves a setting), and ``_provider_checks``.

    REFUSES to write, and that is the point of the class rather than an
    afterthought: :meth:`ConfigStore.write` merges its patch onto
    ``self.document()``, so a write through a pinned store would merge onto a
    read taken arbitrarily long ago and silently drop every change that landed
    since. A pinned store is for one reply and is then discarded.
    """

    def __init__(self, store: ConfigStore) -> None:
        super().__init__(store.root)
        # The one read. Taken before the existence check so a document written
        # between the two cannot report `configured: false` while its own
        # contents are being served.
        self._pinned = store.document()
        self._existed = store.path.is_file()

    @property
    def document_exists(self) -> bool:
        """Whether a document was on disk at the moment this store pinned it."""
        return self._existed

    def document(self) -> dict[str, Any]:
        return self._pinned

    def write(self, *args: Any, **kwargs: Any) -> NoReturn:
        raise RuntimeError(
            "a pinned store is a read snapshot and cannot write: "
            "ConfigStore.write merges onto document(), which is frozen here"
        )


def _capabilities_snapshot() -> dict[str, Any]:
    """BLOCKING — every delegable capability's bound provider and its reachability.

    Two engine answers joined, neither recomputed here.
    :meth:`CapabilityRegistry.describe` is the engine's own per-capability
    provider description, written for a configuration surface: which provider
    serves the capability, whether an operator declared the binding and where,
    and the resolved deadline one call gets. ``_provider_checks`` is the engine's
    own reachability answer, the same one a run's prerequisite gate reports
    against — so a binding this surface calls reachable is one a run would accept,
    rather than a second PATH lookup that could disagree with the gate.

    Built from ONE read of the document, like its sibling reads: the join runs
    against a :class:`_PinnedStore`, so a write landing partway through cannot
    produce a row whose description and binding come from different documents.
    See that class for why this particular join corrupts rather than merely skews.

    ``register_builtins`` runs over the registry for one reason: until it does,
    authoring, review and implementation resolve to the shipped deterministic
    no-coverage default, which reports a path that spends credits as one that
    spends nothing. ``local_analyzer.register`` runs for the same reason and is
    the same class of fact: it is what binds the analysis builtin in a real run,
    so without it this surface names the declared-skip default that reports no
    coverage while a run would report the structural analyzer. The model resolver
    ``register_builtins`` needs is never called — ``describe`` reads provider
    IDENTITIES and serves no request, and the host catalog's identity is
    deterministic whatever it would resolve — so an empty catalog here cannot
    reach a payload.

    ``reachable`` is three-valued on purpose. A builtin binding is reachable by
    construction (it is this engine), so the engine's check skips it entirely and
    this reports ``None`` rather than coercing "not applicable" into ``False`` —
    which would read as a broken provider on every unconfigured capability.

    No project is selected. The engine reads bindings from ONE app-wide section
    with no per-project layer, so a project-scoped read would imply a scope that
    does not exist. The one project-sensitive value is the resolved timeout, which
    therefore travels at app scope.
    """
    store = _PinnedStore(_config_store())
    registry = CapabilityRegistry(store)
    register_builtins(registry, model_resolver=_no_model_catalog)
    local_analyzer.register(registry)
    bindings = registry.bindings()
    # Keyed by declaring path, which is what both sides of the join hold: a
    # delegated binding always carries the dotted path it was declared at, and the
    # engine's check reports the same path so an operator is told where to fix it.
    checks = {
        check.declared_at: check
        for check in provider_checks(store, shutil.which)
        if check.declared_at
    }
    described: list[dict[str, Any]] = []
    for entry in registry.describe():
        binding = bindings[str(entry["capability"])]
        check = None if binding.is_builtin else checks.get(binding.declared_at)
        described.append(
            {
                **entry,
                "program": binding.program,
                "reachable": None if check is None else check.met,
                "action": "" if check is None else check.action,
            }
        )
    return {
        "configured": store.document_exists,
        "capabilities": described,
    }


async def handle_get_capabilities(request: web.Request) -> web.Response:
    """GET each capability's bound provider, and whether it can be reached.

    The write side of this surface has no second door: ``capabilities`` is one of
    the engine's :data:`CONFIG_ONLY_PATHS`, so the agent-facing ``write_config``
    MCP tool refuses it and an operator-confirmed surface is the only path that
    can bind one. This read is the other half of that pair.
    """
    try:
        payload = await asyncio.to_thread(_capabilities_snapshot)
    except (EngineFloorViolation, UnknownCapability) as exc:
        # A stored section the engine refuses to resolve at all. Reported as its
        # own failure rather than allowed to unwind, and NEVER as the all-builtin
        # map an unconfigured document legitimately resolves to: those two are the
        # same shape and opposite facts, and conflating them would show a refused
        # document as a clean one. Named rather than caught as CapabilityError so a
        # future sibling is not swallowed silently.
        return _refuse("capabilities_unreadable", str(exc), status=422)
    except ConfigValidationError as exc:
        # The same condition reached through the config layer: a section that is
        # not an object, or an entry naming a capability the engine does not have.
        # Must precede any ValueError arm -- ConfigValidationError derives it.
        return _refuse(
            "capabilities_unreadable",
            "; ".join(str(error) for error in exc.errors),
            status=422,
        )
    except ConfigLoadError as exc:
        return _refuse("config_unreadable", str(exc), status=409)
    except OSError as exc:
        return _refuse("config_unreadable", str(exc), status=503)
    return web.json_response(payload)


# --- the form vocabulary ----------------------------------------------------


def _setting_vocabulary(setting: Setting) -> dict[str, Any]:
    """One registry entry as the facts a generated form control is built from.

    ``kind`` travels as the type's NAME and the scopes as their value strings: the
    payload is JSON, and a client that had to know Python's spelling of a type or
    of an enum member would be reading a language detail rather than a vocabulary.

    Scopes are ordered broadest-first — the reverse of the resolver's precedence,
    which runs narrowest-first — because a scope chooser reads app before project
    before source while resolution walks the other way. Derived from
    ``SCOPE_PRECEDENCE`` rather than listed, so a scope the registry gains appears
    here without an edit, and ``scopes`` being a frozenset never leaks set
    iteration order into the payload.
    """
    return {
        "key": setting.key,
        "kind": setting.kind.__name__,
        "default": setting.default,
        # Absent bounds travel as null rather than being omitted: a numeric input
        # branches on whether a bound exists, and a key that came and went would
        # read as a shape change rather than as "this setting has no ceiling".
        "minimum": setting.minimum,
        "maximum": setting.maximum,
        "scopes": [scope.value for scope in reversed(SCOPE_PRECEDENCE) if setting.allows(scope)],
        "summary": setting.summary,
    }


def _registry_payload() -> dict[str, Any]:
    """The engine's own form vocabularies: settings, presets, roles, levels.

    A pure projection of bundled constants. Nothing here reads the configuration
    document, which is why this read carries none of the refusal-by-path contract
    its sibling reads do: there is no stored value that can be missing,
    unreadable or invalid, and nothing a concurrent write could tear. A surface
    generated from it renders the vocabulary the ENGINE enforces against — a
    hard-coded field list is how a form comes to offer a setting the write door
    rejects, or to omit one it accepts.

    Ordering is each owning module's declaration order throughout: registry order
    for settings, :data:`WATCH_SOURCE_PRESET_HOSTS` for presets, and the declared
    tuples for the profile, role, effort and level names. A client rendering the
    payload in the order it arrives therefore renders it the same way on every
    read.

    Preset entries come from :func:`watch_source_presets`, which deep-copies and
    deliberately carries no ``enabled`` key, so a copy an operator has not armed
    yet is inert. Composing the entry here rather than in a client is what keeps a
    poll argv the engine's own: the write door validates argv SHAPE and not the
    program it names, so the preset tables are the boundary on what a form can
    cause the engine to run.

    A cost-profile preset travels as its NAME and its ENTRY, the same shape a
    source preset travels in, for the same reason: a form that adds a profile
    adds a copy of one, and a client holding only the name would have to invent
    the role assignments it copies — which is the no-provenance profile the
    engine refuses to be useful with (an empty profile resolves every role to the
    session default while reporting that a profile is selected).

    ``profile_settings`` are the keys a profile may pin, and ``efforts`` the
    effort ladder an assignment may name. Both are vocabularies the WRITE DOOR
    enforces (``_check_profile_settings`` and the role-field check in
    ``schema.py``) and neither is derivable from the setting registry: pinnability
    is not a :class:`Scope`, and effort is not a setting at all. A form offering
    either from a copy kept on its own side is a form that offers what the door
    then refuses.

    ``transports``, ``delivery_stages``, ``gate_positions``, ``gate_severities``,
    ``capabilities`` and ``engine_floor`` are the extension seams' vocabularies,
    each one a closed set the write door already enforces. ``engine_floor`` is
    projected for the opposite reason to the rest: those names are what a surface
    must NOT offer a binding control for, and naming one in ``capabilities`` is a
    refusal rather than an ignored key.

    ``stages`` is the one composed entry. It carries the pipeline stages a surface
    is organised around, each with the setting groups and delegable capabilities
    it presents, so the stage a setting appears under is the engine's answer
    rather than a list kept on the far side of the wire. Group order comes from
    the setting registry's declaration order and never from ``SETTING_GROUPS``,
    which is a frozenset. Read ``engine/config/pipeline.py`` before touching it:
    a pipeline stage is a presentation grouping, and three of its five names also
    appear in ``levels`` above, where they mean autonomy rungs instead.
    """
    return {
        "settings": [_setting_vocabulary(setting) for setting in SETTINGS.values()],
        "source_presets": [
            {
                "host": host,
                # The program each preset needs on PATH, which is what a picker has
                # to state before anything is copied into configuration. Read from
                # the engine's own derivation of it rather than from the argv here,
                # so the picker and the poll cannot name two different tools.
                "program": WATCH_SOURCE_PRESET_PROGRAMS[host],
                "entry": watch_source_presets(host),
            }
            for host in WATCH_SOURCE_PRESET_HOSTS
        ],
        "profile_presets": [
            {"name": name, "entry": cost_profile_presets(name)}
            for name in COST_PROFILE_PRESET_NAMES
        ],
        "profile_settings": list(PROFILE_SETTING_KEYS),
        "roles": list(ROLES),
        "efforts": list(EFFORT_LEVELS),
        "levels": list(AUTONOMY_LEVELS),
        # The extension-seam vocabularies. Each is a closed set the WRITE DOOR
        # enforces, so a form offering any of them from its own copy would offer
        # what the door then refuses by path -- the same reason `roles` and
        # `efforts` are projected rather than duplicated.
        "transports": list(TRANSPORTS),
        "delivery_stages": list(DELIVERY_STAGES),
        "gate_positions": list(GATE_POSITIONS),
        "gate_severities": list(GATE_SEVERITIES),
        "capabilities": list(DELEGABLE_CAPABILITIES),
        # Named so a surface can state which capabilities the engine always
        # executes itself, and offer no control that would attempt to bind one:
        # naming one in `capabilities` is a REFUSAL rather than an ignored key, so
        # a surface that omitted them would leave the refusal to be discovered by
        # provoking it.
        "engine_floor": list(ENGINE_FLOOR_CAPABILITIES),
        "workflow_presets": list(WORKFLOW_PRESET_NAMES),
        # Gate presets travel as whole entries rather than names, for the reason
        # the source and cost-profile presets do: a form adds a gate as a COPY of
        # one, and a client holding only names would have to invent the argv it
        # claims to have copied.
        "gate_presets": gate_presets(),
        # The pipeline stages a surface is organised around, each carrying the
        # setting groups and capabilities it presents. Projected rather than held
        # on the far side so a setting or capability the engine adds appears
        # without an edit there, and so an UNMAPPED one lands in the advanced
        # stage rather than vanishing from every stage at once.
        #
        # These are pipeline stages, NOT autonomy rungs: `PIPELINE_STAGES` shares
        # three of its names with `AUTONOMY_LEVELS` above and answers a different
        # question -- which part of the pipeline a knob governs, not how much
        # authority a run holds. See `engine/config/pipeline.py`.
        "stages": [
            {
                "id": stage,
                "setting_groups": list(stage_setting_groups(stage)),
                "capabilities": list(stage_capabilities(stage)),
            }
            for stage in PIPELINE_STAGES
        ],
    }


async def handle_get_config_registry(request: web.Request) -> web.Response:
    """GET the vocabularies the configuration forms are generated from.

    No ``asyncio.to_thread`` and no refusal arms, both for one reason: the payload
    is bundled data assembled in memory. There is no store to construct, no file
    to open, and so no failure to map onto a status — a try block here would name
    exceptions this code cannot raise.
    """
    return web.json_response(_registry_payload())


# --- the delivery workflow and its gates ------------------------------------

#: Cap on a rendered gate name. The write door constrains a gate name to a
#: non-empty string and nothing else, so the ceiling is here for the reason
#: :data:`~..engine.delivery.MAX_PRESET_NAME_CHARS` exists on the preset display:
#: a hand-edited document must not be able to set the width of a surface's row.
MAX_GATE_NAME_CHARS = 64

#: Run point of a stage that is not part of the delivery flow, and of the flow
#: itself. Projected per stage because the fact lives in two engine modules --
#: ``DELIVERY_FLOW_STAGES`` is fixed in the flow and the teardown stage is
#: executed by archive -- and a client keeping its own copy of it would be one
#: rename away from telling an operator that teardown runs with the others.
RUN_POINT_ISOLATION = "isolation"
RUN_POINT_DELIVERY = "delivery"
RUN_POINT_ARCHIVE = "archive"

#: Which point each declared delivery stage runs at. A stage absent from this map
#: projects an empty run point, which means "this projection has no answer for a
#: stage the engine grew" and must NOT be read as "does not run": an unmapped
#: stage is a table to extend, not a stage to describe as inert.
_STAGE_RUN_POINTS: dict[str, str] = {
    ISOLATE_STAGE: RUN_POINT_ISOLATION,
    **{stage: RUN_POINT_DELIVERY for stage in DELIVERY_FLOW_STAGES},
    TEARDOWN_STAGE: RUN_POINT_ARCHIVE,
}


def _gate_payload(gate: QualityGate) -> dict[str, Any]:
    """One configured gate, as a form reads it.

    ``blocking`` travels beside ``severity`` because it is the ENGINE's own
    reading of that severity. A client deciding for itself whether a severity
    stops a run is how a surface comes to describe a flow the engine does not
    run.

    The name and the declaring path are document-authored -- the write door
    constrains the name to a non-empty string and nothing more -- so both are
    rendered through ``sanitized`` on the way to a label, for the same reason
    ``StageOrigin`` sanitizes its own preset name.
    """
    return {
        "name": sanitized(gate.name, limit=MAX_GATE_NAME_CHARS),
        "position": gate.position,
        "severity": gate.severity,
        "blocking": gate.blocking,
        # The templates as configured, which is what a configuration surface is
        # editing: a rendered argv would have run-time variables substituted and
        # could not be written back.
        "commands": [list(command.source) for command in gate.commands],
        "origin": gate.origin.value,
        "declared_at": sanitized(gate.declared_at),
    }


def _user_preset_names(document: Mapping[str, Any]) -> list[str]:
    """The user-defined workflow preset names, in declaration order.

    App level only, and read from the document rather than resolved per project:
    ``_check_workflow`` admits definitions app-wide alone, so a project has no
    preset set of its own to offer. The bundled names are NOT merged in here --
    they are constants and travel on the registry read, and a chooser that could
    not tell the two apart is the ambiguity reserving bundled names prevents.
    """
    workflow = document.get(SECTION_WORKFLOW)
    if not isinstance(workflow, Mapping):
        return []
    definitions = workflow.get(WORKFLOW_PRESETS_KEY)
    if not isinstance(definitions, Mapping):
        return []
    return [sanitized(str(name), limit=MAX_PRESET_NAME_CHARS) for name in definitions]


def _workflow_snapshot(project: str | None) -> dict[str, Any]:
    """BLOCKING — the delivery workflow in force for *project*, and the gates.

    A projection of what the engine resolved, never a second resolution.
    ``stage_origins`` answers which layer supplied each stage's commands, and it
    is consumed rather than reproduced for the reason its own module states: the
    layering lives in :class:`~..engine.delivery.DeliveryWorkflow`, and a surface
    that re-derived it would name the wrong layer with confidence on the first
    day the two disagreed. Both distinctions that module is required to keep
    therefore survive to the wire untouched -- a stage nobody defines says
    ``unconfigured`` rather than reporting the preset's name, and a user-defined
    preset says ``user_preset`` rather than being flattened onto a bundled one.

    Built from ONE read of the document, like its sibling reads: ``stage_origins``
    and ``load_quality_gates`` each take what they are given, so a write landing
    between two reads cannot produce a reply whose stages and gates come from
    different documents.

    ``argv`` is added beside the engine row's command COUNT because a form edits
    commands and a count cannot be edited. It comes from the same
    ``DeliveryWorkflow`` instance ``stage_origins`` resolved against, so it is
    that stage's resolved commands rather than a second reading of precedence.

    An unparseable gate list is reported as unreadable with ``gates`` NULL rather
    than as an empty list, because the engine refuses delivery outright in that
    case: "no gates" would tell an operator that nothing is configured when what
    is actually true is that every check is off until the document is repaired.

    A ``ValueError`` from the display path is deliberately not caught here. It
    means a resolution layer with no display answer, which is an engine invariant
    rather than a document to repair, and reporting it as a validation failure
    would send an operator to edit configuration that is correct.
    """
    store = _config_store()
    document = store.document()
    workflow = DeliveryWorkflow(document, project=project)
    selection = workflow.selected_preset()
    stages: list[dict[str, Any]] = []
    for origin in stage_origins(workflow):
        row = origin.to_json_object()
        resolved = workflow.stage(origin.stage)
        row["argv"] = [list(command.source) for command in resolved.commands] if resolved else []
        row["runs_at"] = _STAGE_RUN_POINTS.get(origin.stage, "")
        stages.append(row)
    payload: dict[str, Any] = {
        "configured": store.path.is_file(),
        "project": project,
        # The selection, separately from the stages it supplied: a preset is
        # changed by selecting another one, and an overridden stage is changed
        # where the override is declared. Null when nothing selected one.
        "preset": (
            {
                "name": sanitized(selection.name, limit=MAX_PRESET_NAME_CHARS),
                "origin": selection.origin.value,
                "declared_at": sanitized(selection.declared_at),
                "bundled": selection.bundled,
            }
            if selection is not None
            else None
        ),
        "stages": stages,
        "user_presets": _user_preset_names(document),
        # The flow's own order, so a client renders which stages a delivery runs
        # without inferring it from the schema order every stage appears in.
        "delivery_flow_stages": list(DELIVERY_FLOW_STAGES),
        # Gates are app-level: ``load_quality_gates`` takes no project, and a
        # project does not select a different set. Relayed so a form can say so
        # rather than implying the list resolves for the project above.
        "gates_scope_is_app": True,
    }
    try:
        gates = load_quality_gates(document)
    except ConfigValidationError as exc:
        payload["gates"] = None
        payload["gates_unreadable"] = True
        payload["gate_errors"] = [
            {"path": error.path, "message": error.message} for error in exc.errors
        ]
    else:
        payload["gates"] = [_gate_payload(gate) for gate in gates]
        payload["gates_unreadable"] = False
        payload["gate_errors"] = []
    return payload


async def handle_get_config_workflow(request: web.Request) -> web.Response:
    """GET the delivery workflow in force for a project, and the gate list.

    Project-scoped like the resolved read, because a project selects its own
    preset and may override a stage; the gate list beside it is not, and says so.
    """
    project = request.query.get("project") or None
    try:
        payload = await asyncio.to_thread(_workflow_snapshot, project)
    except ConfigValidationError as exc:
        # A stored workflow nobody can act on: an unknown preset name, or a stage
        # whose commands do not parse. Reported by path like the resolved read's
        # equivalent arm, and ABOVE nothing that catches ValueError, which this
        # derives. Distinct from the gate arm inside the snapshot: a workflow that
        # does not resolve has no stage rows to state a failure against, while an
        # unreadable gate list still has a workflow worth showing beside it.
        return _refuse("config_invalid", "; ".join(str(error) for error in exc.errors), status=422)
    except ConfigLoadError as exc:
        return _refuse("config_unreadable", str(exc), status=409)
    except OSError as exc:
        return _refuse("config_unreadable", str(exc), status=503)
    return web.json_response(payload)


# --- the per-source autonomy grid --------------------------------------------


#: The pair's OWN stored cell answered it.
ORIGIN_EXACT = "exact"

#: A broader stored cell — wildcard in either dimension — answered it.
ORIGIN_WILDCARD = "wildcard"

#: Nothing stored answered it, so the unconfigured default is in force. That
#: default covers no gate, which is why the distinction is worth carrying: an
#: operator reading ``authoring`` cannot otherwise tell a rung somebody chose
#: from the rung an absent declaration produces, and only one of those is a
#: decision.
ORIGIN_DEFAULT = "default"


def _cell_origin(decision: AutonomyDecision) -> str:
    """Which kind of declaration answered *decision*.

    Derived by rebuilding the pair's own cell path and comparing whole strings
    rather than by splitting ``declared_at`` into segments: a source may
    legitimately be named with a dot in it, and a split would then read the tail
    of the source name as the submitter class. Composed exactly as the resolver
    composes it, so the two spellings of one path cannot drift.

    Total by construction. An unconfigured decision carries no path at all, and
    every path the resolver does return is either the queried pair's own cell or
    a broader one — there is no fourth case and no parse to fail.
    """
    if not decision.is_configured:
        return ORIGIN_DEFAULT
    own_cell = (
        f"{SECTION_SOURCES}.{decision.source}.{AUTONOMY_FIELD}"
        f".{decision.submitter_class}.{decision.spec_type}"
    )
    return ORIGIN_EXACT if decision.declared_at == own_cell else ORIGIN_WILDCARD


def _source_grid(policy: AutonomyPolicy, source: str) -> dict[str, dict[str, Any]]:
    """*source*'s full matrix: one resolved cell per (submitter class, spec type).

    Every cell is the engine's own resolution — one ``AutonomyPolicy.resolve``
    call per pair against an already-loaded document — so a surface and the gates
    read one resolver. A matrix re-derived from the raw grid would have to
    re-implement class-first precedence and wildcard fallback, and the copy that
    drifted would be the one an operator was reading before deciding who may run
    unattended.

    ``policy_covers_gates`` is ``permits(EXECUTION)``, which is what
    ``gate_is_policy_covered`` reduces to for every document gate: it marks the
    cells whose matching items have their gates approved by the policy with no
    human in the loop.
    """
    grid: dict[str, dict[str, Any]] = {}
    for submitter_class in SUBMITTER_CLASSES:
        row: dict[str, Any] = {}
        for spec_type in SPEC_TYPES:
            decision = policy.resolve(
                source=source, spec_type=spec_type, submitter_class=submitter_class
            )
            row[spec_type] = {
                "level": decision.level.value,
                # Empty rather than absent when nothing was configured, matching
                # the resolved read's spelling of the same idea: a client branches
                # on `origin`, and a key that came and went would read as a shape
                # change rather than as "no declaration answered this".
                "declared_at": decision.declared_at,
                "origin": _cell_origin(decision),
                "policy_covers_gates": decision.permits(AutonomyLevel.EXECUTION),
            }
        grid[submitter_class] = row
    return grid


def _sources_snapshot() -> dict[str, Any]:
    """BLOCKING — every Watch_Source's fully resolved autonomy matrix.

    Built from ONE read of the document, for the reason :func:`_resolved_snapshot`
    is: a resolver that re-read the file per cell would let a write landing
    mid-reply produce a matrix describing two different documents, and the two
    halves would disagree about who may run unattended without saying so.

    A source carrying no ``autonomy`` field is listed with its all-default matrix
    rather than skipped. A configured source nobody wrote a grid for is exactly
    the fail-closed case an operator most needs to see, and omitting it would read
    as "this source is not configured".

    The vocabularies travel with the payload so a surface renders the ENGINE's
    axes: a class or spec type the schema adds appears without a client edit, and
    a client cannot render an axis the resolver has no answer for.
    """
    document = _config_store().document()
    policy = AutonomyPolicy.from_document(document)
    section = document.get(SECTION_SOURCES)
    # Sorted because a JSON object carries no order a client may rely on, and an
    # operator scanning the list wants the same order on every read.
    names = sorted(str(name) for name in section) if isinstance(section, Mapping) else []
    return {
        "sources": [{"name": name, "grid": _source_grid(policy, name)} for name in names],
        "submitter_classes": list(SUBMITTER_CLASSES),
        "spec_types": list(SPEC_TYPES),
        "levels": list(AUTONOMY_LEVELS),
    }


async def handle_get_sources(request: web.Request) -> web.Response:
    """GET every Watch_Source's autonomy grid, resolved cell by cell.

    Refusals mirror the resolved read's, because both are reads of the same
    document through the same store: a stored grid the resolver cannot read is a
    422 naming the path, an unparseable file is a 409, and a disk failure is a
    503. A malformed grid must never come back as values — a surface that
    rendered a partial matrix would show an operator authority the engine would
    refuse to act on.
    """
    try:
        payload = await asyncio.to_thread(_sources_snapshot)
    except ConfigValidationError as exc:
        # A hand-edited grid the resolver refuses to read: a level outside the
        # ladder, or a class row that is not an object. Resolution RAISES on both
        # rather than falling through to a broader cell, and this arm keeps that
        # distinction visible instead of reporting a repair as an empty matrix. It
        # must precede any ValueError arm -- ConfigValidationError derives
        # ValueError.
        return _refuse("config_invalid", "; ".join(str(error) for error in exc.errors), status=422)
    except ConfigLoadError as exc:
        return _refuse("config_unreadable", str(exc), status=409)
    except OSError as exc:
        return _refuse("config_unreadable", str(exc), status=503)
    return web.json_response(payload)


# --- provider conformance: a job, never a request ---------------------------
#
# The bundled suite decides this shape; nothing here is a matter of taste.
# ``suite_for`` gives a document capability five fixtures, four of which run the
# repeatability check and so call the candidate a SECOND time on the same
# request: nine invocations, each spawning a child process through the package's
# sandbox chokepoint. It generates a >= 256 KiB requirements document on every
# call and writes every fixture document to disk. It is fully synchronous, and it
# enforces NO aggregate deadline of its own -- it measures each call and reports,
# with no cap, no cancellation and no watchdog. Inline in a handler that blocks
# the gateway's event loop for the whole run; behind a blocking request it holds
# the request open for minutes. So the POST starts a job and returns, the GET
# polls a stored report, and the run happens in a worker thread.


#: Per-invocation deadline a conformance run puts on the candidate.
#:
#: Chosen HERE and deliberately NOT read from ``binding.timeout_s``.
#: :meth:`CapabilityRegistry.timeout_for` lets a per-binding ``timeout_s`` sit
#: ABOVE the app setting's floor with no clamp, which is right for a real
#: invocation -- a provider whose work is genuinely slower is a reason to raise
#: its own ceiling rather than everyone's -- and wrong for a probe an operator
#: started from a page. Nine invocations with no aggregate deadline means the
#: binding's number is multiplied by nine: at this bound the worst case is a
#: couple of minutes, and at a binding declaring ``timeout_s: 300`` it would be
#: roughly three quarters of an hour of held resources for a check nobody is
#: watching.
CONFORMANCE_DEADLINE_S = 10

#: A run is in flight; no outcome exists yet.
CONFORMANCE_RUNNING = "running"

#: A run finished and its report is attached.
CONFORMANCE_COMPLETE = "complete"

#: A run started and produced no report — the suite itself could not be carried
#: out. Its own status rather than ``complete`` with an empty report, because
#: "complete, no failures" is exactly how the absence of an outcome gets read as
#: a pass.
CONFORMANCE_FAILED = "failed"

#: No run has been started for this capability on this gateway.
CONFORMANCE_ABSENT = "absent"


@dataclass(frozen=True)
class _ConformanceJob:
    """One capability's most recent conformance run.

    Held in memory only, which is a decision rather than an omission: a report
    describes the binding it ran against, and nothing durable would say that the
    binding is still that one. A restarted gateway therefore reports ``absent``
    and an operator runs the check again, instead of being shown a verdict about
    configuration that may have changed while the process was down.
    """

    capability: str
    job_id: str
    #: The label the report's ``candidate`` carries: the program, or the transport
    #: name when the binding names no program.
    candidate: str
    #: Digest of the binding this run was started against.
    fingerprint: str
    status: str
    report: dict[str, Any] | None = None
    error: str = ""


#: The most recent run per capability. Keyed by CAPABILITY rather than by job id
#: because "is one already running for this capability" is the question the POST
#: has to answer, and a second index keyed the other way would be a second thing
#: to keep consistent with this one.
_CONFORMANCE_JOBS: dict[str, _ConformanceJob] = {}

#: Strong references to the in-flight background tasks. ``asyncio`` keeps only a
#: weak reference to a running task, so a task nobody else holds can be collected
#: mid-run — and the job would stay recorded as running for the life of the
#: gateway, with the poll route reporting a run that is not happening.
_CONFORMANCE_TASKS: set[asyncio.Task[None]] = set()


def _binding_fingerprint(binding: Binding) -> str:
    """A digest of everything about *binding* that decides what a run invokes.

    Digested rather than relayed because ``env`` may carry a credential: a client
    needs to know THAT the binding moved, never what it moved to. Every field the
    transport reads is in it, so editing an argument or a token invalidates an
    earlier report the same way replacing the program does.
    """
    material = json.dumps(
        {
            "transport": binding.transport,
            "command": list(binding.argv),
            "env": dict(sorted(binding.env.items())),
            "timeout_s": binding.timeout_s,
        },
        sort_keys=True,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _conformance_binding(capability: str) -> Binding:
    """BLOCKING — the binding *capability* resolves to.

    ``require_delegable`` first, so a floor name is refused as a floor name rather
    than as a missing key in the resolved map.
    """
    require_delegable(capability)
    return resolve_bindings(_config_store())[capability]


def _run_conformance(capability: str, binding: Binding) -> dict[str, Any]:
    """BLOCKING — put *binding*'s provider through *capability*'s bundled suite.

    :class:`TransportCandidate` is built straight from the binding rather than
    reached through the registry, and that is the point of the whole route: the
    registry degrades a broken provider to its builtin and continues, which is
    right for a run and would hide precisely what a conformance report exists to
    reveal.

    ``deadline_s`` is :data:`CONFORMANCE_DEADLINE_S`. ``root`` is left unset so
    the runner materialises its fixtures in a temporary directory it removes:
    passing one would leave a quarter of a megabyte of fixture documents behind
    per run, since a caller-owned root is deliberately not cleaned up.
    """
    transport = transport_for(binding)
    if transport is None:
        raise ValueError(
            f"the binding for {capability} names no program to run, so there is "
            "nothing to put through the suite"
        )
    candidate = TransportCandidate(
        transport=transport, label=binding.program or binding.transport
    )
    return verify(candidate, capability, deadline_s=CONFORMANCE_DEADLINE_S).to_json_object()


def _record_conformance(job: _ConformanceJob) -> None:
    """Store *job* unless a newer run for the same capability has replaced it.

    The concurrency refusal below means a second run cannot start while this one
    is recorded as running, so supersession should be unreachable — checked
    anyway, because a background task writing over a state it has not read is how
    a stale verdict outlives the run that replaced it.
    """
    recorded = _CONFORMANCE_JOBS.get(job.capability)
    if recorded is not None and recorded.job_id != job.job_id:
        logger.warning(
            "spec-engine: conformance job %s for %s was superseded; result discarded",
            job.job_id,
            job.capability,
        )
        return
    _CONFORMANCE_JOBS[job.capability] = job


async def _conformance_worker(job: _ConformanceJob, binding: Binding) -> None:
    """Run the suite off the event loop and record whatever it produced."""
    try:
        report = await asyncio.to_thread(_run_conformance, job.capability, binding)
    except Exception as exc:  # noqa: BLE001 - nothing may escape a background task
        # Broad in two directions. The runner already turns a candidate's own
        # crash into a failed CHECK, so anything arriving here is the run failing
        # to happen at all -- an unwritable temporary directory, a binding with no
        # program. And an exception escaping a background task leaves the job
        # recorded as running for the life of the gateway while the poll route
        # reports a run nobody is doing.
        logger.warning(
            "spec-engine: conformance run for %s did not complete",
            job.capability,
            exc_info=True,
        )
        _record_conformance(
            replace(job, status=CONFORMANCE_FAILED, error=f"{exc.__class__.__name__}: {exc}")
        )
        return
    _record_conformance(replace(job, status=CONFORMANCE_COMPLETE, report=report))


def _conformance_payload(
    capability: str, job: _ConformanceJob | None, *, current: str
) -> dict[str, Any]:
    """One capability's conformance state, with the run's verdict above the checks.

    ``report.passed`` is the engine's own verdict and is deliberately not "no
    failures": it is false whenever a declared check never ran or the suite
    produced nothing, so a surface reading the top of this payload cannot present
    a greener answer than the report supports. That ordering matters for one check
    in particular. The transport SIGKILLs a provider's child AT its deadline, so a
    provider that ignored the deadline still MEASURES as answering inside the
    grace period: ``timeout_honoring`` typically PASSES while the other four
    checks fail with "the candidate raised TransportFailure". That green check is
    reassurance about nothing, and the verdict is what a reader must take away.

    ``stale`` is derived here rather than left to a client. A client is never
    shown the binding's ``env`` values, so any fingerprint it computed would be a
    fingerprint of something else — and comparing the wrong two things is how an
    earlier outcome goes on being presented as describing the current binding.
    """
    if job is None:
        return {
            "capability": capability,
            "status": CONFORMANCE_ABSENT,
            "job_id": "",
            "candidate": "",
            "binding_fingerprint": "",
            "binding_current": current,
            "stale": False,
            "deadline_s": CONFORMANCE_DEADLINE_S,
            "error": "",
            "report": None,
        }
    return {
        "capability": capability,
        "status": job.status,
        "job_id": job.job_id,
        "candidate": job.candidate,
        "binding_fingerprint": job.fingerprint,
        "binding_current": current,
        "stale": current != job.fingerprint,
        "deadline_s": CONFORMANCE_DEADLINE_S,
        "error": job.error,
        "report": job.report,
    }


async def _binding_or_refusal(capability: str) -> Binding | web.Response:
    """*capability*'s binding, or the refusal to send instead of one.

    Shared by both conformance routes so the two cannot answer a refused document
    differently. The arms mirror the capability read's: a floor or unknown name is
    the engine refusing the question, an unresolvable section is a repair, an
    unparseable file is a 409 and a disk failure a 503.
    """
    if not capability:
        return _refuse("field_required", "a conformance run needs the capability to check")
    try:
        return await asyncio.to_thread(_conformance_binding, capability)
    except EngineFloorViolation as exc:
        # Named rather than caught as CapabilityError so a future sibling is not
        # swallowed. The floor is not bindable, so there is no candidate to check.
        return _refuse("engine_floor_capability", str(exc), status=422)
    except UnknownCapability as exc:
        return _refuse("unknown_capability", str(exc), status=404)
    except ConfigValidationError as exc:
        # Must precede any ValueError arm -- ConfigValidationError derives it.
        return _refuse(
            "capabilities_unreadable",
            "; ".join(str(error) for error in exc.errors),
            status=422,
        )
    except ConfigLoadError as exc:
        return _refuse("config_unreadable", str(exc), status=409)
    except OSError as exc:
        return _refuse("config_unreadable", str(exc), status=503)


async def handle_post_conformance(request: web.Request) -> web.Response:
    """Start a conformance run against a capability's configured provider.

    Operator-only and SEL-audited because it SPAWNS the operator-configured
    program — up to nine times — which is authority no app-minted token gets
    inside this app's namespace.

    Returns ``202`` with a job id and no outcome. The reasoning is in the section
    comment above: the suite is synchronous, spawns a child per invocation, and
    caps nothing in aggregate.
    """
    body = await _json_object(request)
    if isinstance(body, web.Response):
        return body
    capability = _text(body, "capability")
    binding = await _binding_or_refusal(capability)
    if isinstance(binding, web.Response):
        return binding
    if binding.is_builtin:
        return _refuse(
            "builtin_binding",
            f"{capability} is bound to its builtin, so there is no configured "
            "provider to check; the engine verifies its own builtins in its suite",
            status=409,
        )
    # Read the current state and claim it in ONE synchronous stretch, with no
    # await between the two statements: handlers run on the gateway's single
    # event loop, so a coroutine that does not yield cannot be interleaved. A lock
    # would guard nothing the loop does not already guarantee -- but splitting
    # these two with an await WOULD let two POSTs both pass the check and both
    # start a run against the same program.
    running = _CONFORMANCE_JOBS.get(capability)
    if running is not None and running.status == CONFORMANCE_RUNNING:
        return _refuse(
            "conformance_running",
            f"a conformance run for {capability} is already in progress as job "
            f"{running.job_id}; it invokes the configured provider up to nine "
            "times, so a second run would double that load against one program",
            status=409,
        )
    job = _ConformanceJob(
        capability=capability,
        job_id=uuid.uuid4().hex,
        candidate=binding.program or binding.transport,
        fingerprint=_binding_fingerprint(binding),
        status=CONFORMANCE_RUNNING,
    )
    _CONFORMANCE_JOBS[capability] = job
    task = asyncio.create_task(_conformance_worker(job, binding))
    _CONFORMANCE_TASKS.add(task)
    task.add_done_callback(_CONFORMANCE_TASKS.discard)
    _sel_event(
        caller=_actor(request) or SURFACE_INITIATOR,
        operation="spec_engine_conformance_run",
        outcome="success",
        resources=(
            f"capability={capability} transport={binding.transport} "
            f"program={binding.program} job={job.job_id}"
        ),
    )
    return web.json_response(
        {"ok": True, **_conformance_payload(capability, job, current=job.fingerprint)},
        status=202,
    )


async def handle_get_conformance(request: web.Request) -> web.Response:
    """GET whether a conformance run is in flight, finished, or was never started.

    The binding is resolved on every poll rather than only when a run starts,
    because ``stale`` is the answer to "does this report still describe what is
    configured" and that answer changes when the document does, not when the run
    does.
    """
    capability = request.match_info.get("capability", "")
    binding = await _binding_or_refusal(capability)
    if isinstance(binding, web.Response):
        return binding
    return web.json_response(
        _conformance_payload(
            capability,
            _CONFORMANCE_JOBS.get(capability),
            current=_binding_fingerprint(binding),
        )
    )


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
    add("GET", f"{PREFIX}/config/registry", _read(handle_get_config_registry))
    add("GET", f"{PREFIX}/config/workflow", _read(handle_get_config_workflow))
    add("GET", f"{PREFIX}/config/sources", _read(handle_get_sources))
    add("GET", f"{PREFIX}/config/capabilities", _read(handle_get_capabilities))

    # Conformance is a JOB. The POST is operator-only because it spawns the
    # operator-configured program up to nine times; the GET polls the report it
    # left behind. See the section comment beside the handlers.
    add(
        "POST",
        f"{PREFIX}/config/conformance",
        _mutate(handle_post_conformance, operation="conformance_run"),
    )
    add(
        "GET",
        f"{PREFIX}/config/conformance/{{capability}}",
        _read(handle_get_conformance),
    )

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
