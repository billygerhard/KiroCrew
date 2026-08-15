"""The operator-facing engine surfaces: configuration, spend, and the stop control.

Three read models and two writes, each a thin relay onto an engine object that
already owns the answer:

* **configuration.** Every setting's effective value with the origin that
  produced it, from :meth:`ConfigStore.effective_settings`. This module does NOT
  re-derive precedence. A second precedence implementation that disagreed with
  the engine's would show an operator a value the engine does not use, which is
  worse than showing nothing, so the projection is the engine's own
  ``EffectiveValue.to_json_object`` and the only thing added here is the shape of
  the HTTP envelope around it.
* **the Review_Queue with per-run spend.** Relayed from
  :meth:`ReviewQueue.snapshot`, whose ``QueueEntry`` already carries
  ``cost_credits``. Nothing here builds a second projection of a run: two
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
"""

from __future__ import annotations

import logging
from typing import Any

from aiohttp import web

from ...spec_engine.engine import review_queue as engine_review_queue
from ...spec_engine.engine import runs as engine_runs
from ...spec_engine.engine.budget import switch as engine_switch
from ...spec_engine.engine.budget.killswitch import (
    engage_kill_switch,
    release_kill_switch,
    stoppable_runs,
)
from ...spec_engine.engine.config import ConfigLoadError, ConfigStore, ConfigValidationError
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
    """
    try:
        payload = await request.json()
    except Exception:
        return _bad_request("bad_json", "request body must be a JSON object")
    if not isinstance(payload, dict):
        return _bad_request("bad_json", "request body must be a JSON object")
    patch = payload.get("patch", payload)
    if not isinstance(patch, dict):
        return _bad_request("bad_patch", "patch must be a JSON object")
    try:
        document = _config_store().write(patch, surface=DASHBOARD_SURFACE)
    except ConfigValidationError as exc:
        return _bad_request(
            "config_invalid",
            "; ".join(str(e) for e in exc.errors),
            status=422,
        )
    except (ConfigLoadError, OSError) as exc:
        return _bad_request("config_write_failed", str(exc), status=503)
    logger.info("spec-builder: engine configuration written from the dashboard")
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
    """
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
    if action == "release":
        released = release_kill_switch(state=store, initiator=DASHBOARD_INITIATOR)
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
        initiator=DASHBOARD_INITIATOR,
        reason=reason,
        machine=machine,
        audit=audit_log,
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


async def handle_get_queue(request: web.Request) -> web.Response:
    """GET the Review_Queue, each row carrying the credits its run consumed.

    Relayed from the engine's own snapshot, so the credits an operator reads here
    are the ones the ceiling and the kill switch account against.
    """
    from . import routes

    store, audit_log = routes._engine_store()
    project = request.query.get("project") or None
    machine = engine_runs.RunMachine(store, _config_store(), audit=audit_log)
    snapshot = engine_review_queue.ReviewQueue(machine).snapshot(project=project)
    entries = [entry.to_json_object() for entry in snapshot]
    return web.json_response(
        {
            "entries": entries,
            "total_credits": round(sum(entry.cost_credits for entry in snapshot), 4),
        }
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
