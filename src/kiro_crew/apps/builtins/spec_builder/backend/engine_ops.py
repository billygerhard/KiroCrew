"""The operator-facing engine surfaces: configuration, spend, the queue, and the stop control.

Three read models and the writes that act on them, each a thin relay onto an
engine object that already owns the answer:

* **configuration.** Every setting's effective value with the origin that
  produced it, from :meth:`ConfigStore.effective_settings`. This module does NOT
  re-derive precedence. A second precedence implementation that disagreed with
  the engine's would show an operator a value the engine does not use, which is
  worse than showing nothing, so the projection is the engine's own
  ``EffectiveValue.to_json_object`` and the only thing added here is the shape of
  the HTTP envelope around it.
* **the Review_Queue with per-run spend.** Relayed from
  :meth:`ReviewQueue.snapshot`, whose ``QueueEntry`` already carries
  ``cost_credits`` and whose ``grouped()`` already groups by run state. Nothing
  here builds a second projection of a run, or a second grouping of one: two
  projections of one run drift, and an operator cannot tell which is current.
* **the kill switch.** Read and written through
  :func:`engage_kill_switch` / :func:`release_kill_switch`, which park the runs
  and write the audit entry. This module holds no stopping logic of its own.

The configuration write goes through the engine's single validated write path on
:data:`~...spec_engine.engine.config.store.DASHBOARD_SURFACE`. That surface is
operator-confirmed, which is correct for a panel a human is looking at and is
also why nothing here needs its own fence: the engine refuses an invalid
document, and the config-only paths that an unconfirmed surface may not write are
the engine's rule, applied identically to this one.

**The queue actions are privileged.** A feedback release is the human gate on
quarantined content; a re-dispatch overrides the suppression that stopped an item
from being worked twice; a cleanup deletes a recorded tree. So each one
authenticates, takes its actor from that authenticated session rather than from
its request body, and passes the engine's real audit log -- which is what makes
the engine's own refusal (a release into a queue that records to no log) apply
here instead of being routed around.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiohttp import web

from ...spec_engine.engine import review_queue as engine_review_queue
from ...spec_engine.engine import runs as engine_runs
from ...spec_engine.engine import state as engine_state
from ...spec_engine.engine.budget import switch as engine_switch
from ...spec_engine.engine.budget.killswitch import (
    engage_kill_switch,
    release_kill_switch,
    stoppable_runs,
)
from ...spec_engine.engine.config import (
    ConfigLoadError,
    ConfigStore,
    ConfigValidationError,
    ConfigWriteRefused,
)
from ...spec_engine.engine.config.store import DASHBOARD_SURFACE

logger = logging.getLogger("kirocrew.app.spec-builder")

#: Initiator recorded when the stop comes from this panel. A name rather than the
#: empty string so the audit entry and the parked rows say where the stop came
#: from: "an operator" and "the dashboard" answer different questions when a
#: second surface reaches the same switch.
DASHBOARD_INITIATOR = "dashboard"

#: Sections of the configuration document this surface renders as domains. Each
#: is relayed as stored, because these are containers (watch sources, projects,
#: the workflow, notification channels) rather than registry settings, so there
#: is no effective value to resolve for them -- what is stored IS what applies.
CONFIG_DOMAIN_SECTIONS: tuple[str, ...] = (
    "sources",
    "projects",
    "workflow",
    "notifications",
    "capabilities",
    "cost_profiles",
)


def _config_store() -> ConfigStore:
    """The engine's config store for the live data home.

    Constructed per call rather than cached: the document is small, it is read
    from disk on every access anyway, and a cached store binds a root that
    ``KIROCREW_HOME`` may have moved since.
    """
    return ConfigStore()


def _bad_request(code: str, error: str, *, status: int = 400) -> web.Response:
    """A refusal carrying a machine-readable code beside its human text."""
    return web.json_response({"code": code, "error": error}, status=status)


def _sel_audit(operation: str, resources: str = "", outcome: str = "success") -> None:
    """Record one security-relevant event on this app's SEL trail.

    Best-effort by the same rule the app's own ``_audit`` uses: this is the
    record of an operator action, not a precondition for it, and a SEL log that
    cannot be written must not turn a completed stop into a reported failure.
    """
    from . import routes

    routes._audit(operation, resources, outcome)


def _operator_only(request: web.Request, *, operation: str) -> web.Response | None:
    """Refuse anything that is not an authenticated dashboard browser session.

    Two refusals, because there are two distinct callers to keep out of a write
    that can lower a floor:

    * **anonymous** -- the 401 every other privileged route in this module
      applies. These two writes did not have it.
    * **an app token** -- 403. ``request["user"]`` is truthy for an app token, so
      the auth check alone does not separate a human from an app, and an app whose
      manifest allowlists this app's API path (``spec-builder``'s own does) passes
      the token layer's scope gate. That matters here specifically because the
      config write runs at an ``operator_confirmed`` surface and the kill-switch
      release restores spending: an agent that can mint an app token would
      otherwise be able to widen its own autonomy ladder or lift the stop an
      operator put on it. Same gate, and same reason, as the computer-use and
      Browser Mode keystone saves.
    """
    from . import routes

    if denied := routes._require_auth(request):
        return denied
    app_caller = request.get("app")
    if app_caller:
        _sel_audit(operation, f"app:{app_caller}", "denied")
        return _bad_request(
            "dashboard_user_required",
            "this control is only writable from a signed-in dashboard session",
            status=403,
        )
    return None


async def handle_get_config(request: web.Request) -> web.Response:
    """GET the effective configuration: every setting's value in force, and its origin.

    ``project`` and ``source`` narrow the resolution, because a setting's
    effective value is a question about a scope: the same key resolves to
    different values for two projects, and a panel editing one project has to
    show that project's answer rather than the app-wide one.
    """
    project = request.query.get("project") or None
    source = request.query.get("source") or None
    store = _config_store()
    try:
        resolved = store.effective_settings(project=project, source=source)
        document = store.document()
    except ConfigValidationError as exc:
        # A stored value the registry refuses. Reported rather than silently
        # replaced by the default: the operator who hand-edited an out-of-range
        # ceiling is the one who needs to hear about it, and substituting the
        # default would run the work they meant to bound.
        return _bad_request(
            "config_invalid",
            "the saved configuration has a value the engine refuses: "
            + "; ".join(str(e) for e in exc.errors),
            status=409,
        )
    except ConfigLoadError as exc:
        return _bad_request("config_unreadable", str(exc), status=409)
    return web.json_response(
        {
            "scope": {"project": project, "source": source},
            "settings": {key: value.to_json_object() for key, value in resolved.items()},
            "domains": {
                section: document.get(section, {})
                for section in CONFIG_DOMAIN_SECTIONS
                if section in document
            },
            # Named so a surface can label the domains it has no editor for
            # instead of implying that an absent section is an empty one.
            "domain_sections": list(CONFIG_DOMAIN_SECTIONS),
        }
    )


async def handle_put_config(request: web.Request) -> web.Response:
    """Persist a configuration patch through the engine's single write path.

    The patch is merged, validated and persisted by the engine. Every rule an
    operator can trip -- an unknown key, an out-of-range value, a setting written
    at a scope it is not overridable at, a screening opt-out carrying a
    disable-all key -- is enforced there and reported here by path, so this
    handler has no validation of its own to keep in step.

    **A dashboard browser session only.** This write is the one place a floor can
    be lowered: :data:`DASHBOARD_SURFACE` claims ``operator_confirmed``, which is
    what unlocks the config-only sections (the workflow and quality-gate argv the
    delivery pipeline executes, the declared program minimums the Doctor checks,
    a source's autonomy ladder, and ``delivery.auto_integrate``). That claim is
    only true while a human is the one writing, and ``request["user"]`` is truthy
    for an app token too -- ``spec_builder/app.json`` allowlists
    ``/api/apps/spec-builder/*`` in ``permissions.api``, so an app token minted
    from an ``.app_secret`` reaches this route with an identity that passes an
    auth check. So an app token is refused before the body is read, the same gate
    the computer-use and Browser Mode keystone saves apply for the same reason.
    """
    denied = _operator_only(request, operation="engine_config_write")
    if denied is not None:
        return denied
    try:
        payload = await request.json()
    except Exception:
        return _bad_request("bad_json", "request body must be a JSON object")
    if not isinstance(payload, dict):
        return _bad_request("bad_json", "request body must be a JSON object")
    patch = payload.get("patch", payload)
    if not isinstance(patch, dict):
        return _bad_request("bad_patch", "patch must be a JSON object")
    actor = str(request.get("user") or "")
    try:
        document = _config_store().write(patch, surface=DASHBOARD_SURFACE)
    except ConfigValidationError as exc:
        return _bad_request(
            "config_invalid",
            "; ".join(str(e) for e in exc.errors),
            status=422,
        )
    except ConfigWriteRefused as exc:
        # The engine's own surface fence. Reported as a refusal with its paths
        # rather than as a validation failure: nothing about the values is wrong,
        # and an operator told "invalid" would keep editing them.
        return _bad_request("config_write_refused", str(exc), status=403)
    except (ConfigLoadError, OSError) as exc:
        return _bad_request("config_write_failed", str(exc), status=503)
    logger.info("spec-builder: %s wrote the engine configuration", actor or "an operator")
    _sel_audit("engine_config_write", f"actor={actor}" if actor else "")
    return web.json_response({"ok": True, "document": document})


async def handle_get_kill_switch(request: web.Request) -> web.Response:
    """GET whether unattended work is stopped, and what a stop would stop.

    ``stoppable`` is the runs the switch has something to do about, each with the
    credits it has already consumed, so the control names its own blast radius
    before it is thrown rather than after.
    """
    from . import routes

    store, _audit_log = routes._engine_store()
    switch = engine_switch.KillSwitch(store.root)
    records = stoppable_runs(store)
    rows = [
        {
            "run_id": record.run_id,
            "spec_key": record.spec_key,
            "source": record.source,
            "state": record.state,
            "cost_credits": record.cost_credits,
        }
        for record in records
    ]
    return web.json_response(
        {
            "switch": switch.read().to_json_object(),
            "stoppable": rows,
            "stoppable_credits": round(sum(record.cost_credits for record in records), 4),
        }
    )


async def handle_post_kill_switch(request: web.Request) -> web.Response:
    """Engage or release the kill switch.

    Engaging parks every stoppable run and reports what it stopped; releasing
    only lets new work start again and resumes nothing, which is the engine's
    behaviour and is reported as such rather than smoothed over here.

    Both directions are attributed to the AUTHENTICATED session rather than to
    this surface's name, and both are gated to a dashboard session: the release
    is the direction that restores spending, so an app token lifting an
    operator's stop is exactly what :func:`_operator_only` is here to prevent.
    The engine writes the release into its own audit log; the SEL event below
    covers the case that log cannot address -- a release while nothing is parked
    concerns no spec, and the engine's log is per spec.
    """
    denied = _operator_only(request, operation="engine_kill_switch")
    if denied is not None:
        return denied
    try:
        payload = await request.json()
    except Exception:
        return _bad_request("bad_json", "request body must be a JSON object")
    if not isinstance(payload, dict):
        return _bad_request("bad_json", "request body must be a JSON object")
    action = payload.get("action")
    if action not in ("engage", "release"):
        return _bad_request("bad_action", "action must be 'engage' or 'release'")
    reason = payload.get("reason") or ""
    if not isinstance(reason, str):
        return _bad_request("bad_reason", "reason must be a string")

    from . import routes

    store, audit_log = routes._engine_store()
    config = _config_store()
    # The actor is the session, never the body: a stop or a release recorded
    # against a name its caller supplied records nothing. The surface name is the
    # fallback for a deployment with no user on the request, so the entry still
    # says where the action came from.
    initiator = str(request.get("user") or "") or DASHBOARD_INITIATOR
    if action == "release":
        try:
            released = await asyncio.to_thread(
                lambda: release_kill_switch(
                    state=store,
                    initiator=initiator,
                    audit=audit_log,
                )
            )
        except (OSError, ValueError) as exc:
            # Includes the audit-first refusal: a release whose trail could not be
            # written leaves the switch engaged, and says so rather than reporting
            # a stop that is no longer in force.
            return _bad_request("release_failed", str(exc), status=503)
        _sel_audit("engine_kill_switch_release", f"actor={initiator}")
        switch = engine_switch.KillSwitch(store.root)
        return web.json_response(
            {
                "ok": True,
                "action": "release",
                "changed": released,
                "switch": switch.read().to_json_object(),
                # Stated because it is the thing an operator most often assumes
                # otherwise: nothing that was parked starts again on its own.
                "resumed": [],
            }
        )
    machine = engine_runs.RunMachine(store, config, audit=audit_log)
    report = engage_kill_switch(
        state=store,
        config=config,
        initiator=initiator,
        reason=reason,
        machine=machine,
        audit=audit_log,
    )
    _sel_audit("engine_kill_switch_engage", f"actor={initiator}")
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


async def handle_get_queue(request: web.Request) -> web.Response:
    """GET the Review_Queue: every run waiting on a person, flat and grouped.

    Relayed from the engine's own snapshot, so the credits an operator reads here
    are the ones the ceiling and the kill switch account against, and the run
    state grouping is :meth:`QueueSnapshot.grouped`'s rather than a second
    grouping assembled per surface -- two groupings of one run drift, and an
    operator cannot tell which is current.
    """
    from . import routes

    store, audit_log = routes._engine_store()
    project = request.query.get("project") or None
    machine = engine_runs.RunMachine(store, _config_store(), audit=audit_log)
    snapshot = engine_review_queue.ReviewQueue(machine).snapshot(project=project)
    payload = snapshot.to_json_object()
    payload["total_credits"] = round(sum(entry.cost_credits for entry in snapshot), 4)
    return web.json_response(payload)


def _review_queue() -> engine_review_queue.ReviewQueue:
    """The engine's Review_Queue over the live data home, WITH the audit log.

    The audit log is not optional decoration here. Every action below is a
    privileged manual override, and the engine refuses a feedback release
    outright when its run machine records to nowhere -- so a queue built without
    the log would not silently skip the trail, it would fail. Passing it is how
    the trail gets written, and there is no branch here that proceeds without it.
    """
    from . import routes

    store, audit_log = routes._engine_store()
    machine = engine_runs.RunMachine(store, _config_store(), audit=audit_log)
    return engine_review_queue.ReviewQueue(machine)


async def _action_request(request: web.Request) -> tuple[dict, str] | web.Response:
    """The body and the ACTOR for one queue action, or a refusal.

    The actor is the authenticated session, read from the request the middleware
    populated. It is never taken from the body: a privileged override that let
    its caller name the actor would record whoever the caller typed, which is the
    same as recording nothing. This mirrors the approval writer's rule, for the
    same reason.
    """
    from . import routes

    if denied := routes._require_auth(request):
        return denied
    try:
        payload = await request.json()
    except Exception:
        return _bad_request("bad_json", "request body must be a JSON object")
    if not isinstance(payload, dict):
        return _bad_request("bad_json", "request body must be a JSON object")
    return payload, str(request.get("user") or "")


def _text_field(payload: dict, field: str) -> str:
    return str(payload.get(field, "")).strip()


def _int_field(payload: dict, field: str) -> int | None:
    """*field* as an int, or ``None`` when it is absent or not one.

    A bool is refused: ``True`` is an ``int`` in Python and a workspace id of
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


async def handle_post_release_feedback(request: web.Request) -> web.Response:
    """Release one held reviewer comment for a queue row.

    The human gate on quarantined content, so the comment IDENTIFIER is all that
    crosses this boundary -- the comment text is someone else's data and this
    route must not become a second place it is copied to. Refused when the
    engine's queue records to no audit log, because a release with no trail is
    what lets held content drive a dispatch with nothing saying who allowed it.
    """
    prepared = await _action_request(request)
    if isinstance(prepared, web.Response):
        return prepared
    payload, actor = prepared
    project = _text_field(payload, "project")
    spec = _text_field(payload, "spec")
    run_id = _text_field(payload, "run_id")
    comment_id = _text_field(payload, "comment_id")
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
        return _bad_request(
            "field_required",
            "releasing a held comment needs " + ", ".join(missing),
        )
    ref = engine_state.SpecRef(project=project, name=spec)
    try:
        released = await asyncio.to_thread(_run_release, ref, run_id, comment_id, actor=actor)
    except engine_review_queue.ReviewFeedbackRefused as exc:
        # The engine's own refusal, reported rather than worked around.
        return _bad_request("release_refused", str(exc), status=409)
    except (OSError, ValueError) as exc:
        return _bad_request("release_failed", str(exc), status=503)
    logger.info("spec-builder: a held review comment was released from the dashboard")
    return web.json_response(
        {
            "ok": True,
            # False means nobody held that comment, so a click on a stale row is
            # answered rather than reported as a release that did not happen.
            "released": released,
        }
    )


def _run_release(ref: engine_state.SpecRef, run_id: str, comment_id: str, *, actor: str) -> bool:
    """BLOCKING -- the release itself, off the event loop."""
    return _review_queue().release_quarantined_feedback(ref, run_id, comment_id, actor=actor)


async def handle_post_redispatch(request: web.Request) -> web.Response:
    """Lift the suppression on one watched item, so the next poll dispatches it."""
    prepared = await _action_request(request)
    if isinstance(prepared, web.Response):
        return prepared
    payload, actor = prepared
    source = _text_field(payload, "source")
    item_id = _text_field(payload, "item_id")
    generation = _int_field(payload, "generation")
    if not source or not item_id:
        return _bad_request("field_required", "a re-dispatch needs source and item_id")
    if generation is None:
        return _bad_request(
            "field_required",
            "a re-dispatch needs the generation it is lifting, which the queue row "
            "does not carry",
        )
    try:
        lifted = await asyncio.to_thread(
            lambda: _review_queue().redispatch_item(source, item_id, generation=generation)
        )
    except (OSError, ValueError) as exc:
        return _bad_request("redispatch_failed", str(exc), status=503)
    logger.info(
        "spec-builder: %s requested a manual re-dispatch of a %s item",
        actor or "an operator",
        source,
    )
    return web.json_response({"ok": True, "lifted": lifted})


async def handle_post_clean_workspace(request: web.Request) -> web.Response:
    """Remove one ledger-recorded workspace: the retry for a kept teardown."""
    prepared = await _action_request(request)
    if isinstance(prepared, web.Response):
        return prepared
    payload, actor = prepared
    workspace_id = _int_field(payload, "workspace_id")
    if workspace_id is None:
        return _bad_request("field_required", "a cleanup needs the workspace_id to remove")
    force = payload.get("force") is True
    try:
        cleanup = await asyncio.to_thread(
            lambda: _review_queue().clean_workspace(workspace_id, force=force)
        )
    except (OSError, ValueError) as exc:
        return _bad_request("cleanup_failed", str(exc), status=503)
    logger.info(
        "spec-builder: %s asked to clean workspace row %s (force=%s)",
        actor or "an operator",
        workspace_id,
        force,
    )
    return web.json_response(
        {
            "ok": True,
            # None from the engine means no ACTIVE row has that id, so a double
            # click reads as "nothing to do" rather than as a second removal.
            "removed": cleanup is not None,
            "cleanup": cleanup.to_json_object() if cleanup is not None else None,
        }
    )


async def handle_post_teardown_workspaces(request: web.Request) -> web.Response:
    """Tear down every workspace a run recorded.

    Reports what it KEPT as well as what it removed: a teardown that could not
    finish leaves a tree or a deployment standing, and calling that a success is
    how an environment outlives every record of itself.
    """
    prepared = await _action_request(request)
    if isinstance(prepared, web.Response):
        return prepared
    payload, actor = prepared
    run_id = _text_field(payload, "run_id")
    if not run_id:
        return _bad_request("field_required", "a teardown needs the run_id to tear down")
    try:
        report = await asyncio.to_thread(lambda: _review_queue().teardown_run_workspaces(run_id))
    except (OSError, ValueError) as exc:
        return _bad_request("teardown_failed", str(exc), status=503)
    logger.info(
        "spec-builder: %s asked to tear down the workspaces of run %s",
        actor or "an operator",
        run_id,
    )
    return web.json_response(
        {"ok": True, "complete": report.complete, "report": report.to_json_object()}
    )


def register_engine_routes(app: web.Application, base: str, wrap: Any) -> None:
    """Register the operator surfaces under *base*.

    *wrap* is the app's ``_require_enabled`` decorator, passed in rather than
    imported: these handlers must be gated exactly as every other route in this
    app is, and taking the caller's decorator means there is one gate rather than
    a second spelling of it that could drift open.
    """
    app.router.add_get(f"{base}/engine/config", wrap(handle_get_config))
    app.router.add_put(f"{base}/engine/config", wrap(handle_put_config))
    app.router.add_post(f"{base}/engine/config", wrap(handle_put_config))
    app.router.add_get(f"{base}/engine/kill-switch", wrap(handle_get_kill_switch))
    app.router.add_post(f"{base}/engine/kill-switch", wrap(handle_post_kill_switch))
    app.router.add_get(f"{base}/engine/queue", wrap(handle_get_queue))
    # The queue row's actions. Each one is a privileged manual override, so each
    # is authenticated inside its handler and takes its actor from that session.
    app.router.add_post(f"{base}/engine/queue/release-feedback", wrap(handle_post_release_feedback))
    app.router.add_post(f"{base}/engine/queue/redispatch", wrap(handle_post_redispatch))
    app.router.add_post(f"{base}/engine/queue/clean-workspace", wrap(handle_post_clean_workspace))
    app.router.add_post(f"{base}/engine/queue/teardown", wrap(handle_post_teardown_workspaces))
