"""Owner backend pick: ``POST /api/chat/slots/{slot}/backend``.

The same switch ``session_set_backend`` makes (``chat_handlers.switch_slot_backend``),
for the two callers the session-control verb cannot serve: the dashboard owner,
and owner automation such as a seat-failover cron, which presents the internal
secret and no session of its own. Session control refuses a caller with no
session (``caller_unidentified``) and fences a cron to slots it created, so a cron
moving the owner's chats off a full seat needs this owner path. An agent's call,
which always carries ``X-Session-Key``, is refused here and keeps going through
``session_set_backend`` and its fence.
"""

from __future__ import annotations

from aiohttp import web

from kiro_crew.dashboard.handlers._shared_session_memory import _read_session_key


def _owner_caller_refusal(request: web.Request) -> web.Response | None:
    if request.get("internal_auth") is True:
        if _read_session_key(request):
            return web.json_response(
                {
                    "error": "a session picks another session's backend with session_set_backend",
                    "code": "use_session_control",
                },
                status=403,
            )
        return None
    from kiro_crew.dashboard.handlers._shared import _owner_denial_response
    from kiro_crew.dashboard.handlers.source_providers import is_owner_dashboard_request

    if is_owner_dashboard_request(request):
        return None
    return _owner_denial_response(request, "picking a chat's backend is owner-only")


async def api_chat_slot_backend(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/backend -- ``{"backend": "<id>"}``; ``""`` clears it."""
    from kiro_crew.dashboard.chat_handlers import (
        read_bounded_json,
        slot_not_found,
        switch_slot_backend,
    )
    from kiro_crew.dashboard.session_control import SessionControlError

    refusal = _owner_caller_refusal(request)
    if refusal is not None:
        return refusal
    state = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return slot_not_found()
    body, body_err = await read_bounded_json(request)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    pick = body.get("backend", "")
    if not isinstance(pick, str):
        return web.json_response({"error": "backend must be a string"}, status=400)
    try:
        out = await switch_slot_backend(
            state,
            slot,
            name,
            pick.strip(),
            still_ours=lambda: state._slots.get(name) is slot,
            requested_by=str(body.get("requested_by") or ""),
        )
    except SessionControlError as exc:
        return web.json_response(
            {"error": str(exc), "code": getattr(exc, "code", "")},
            status=getattr(exc, "status", 400),
        )
    return web.json_response({"ok": True, **out})
