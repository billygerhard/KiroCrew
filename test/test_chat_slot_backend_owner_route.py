"""``POST /api/chat/slots/{slot}/backend``: the owner's path to a chat's backend pick.

An internal caller with no session (a seat-failover cron) and the dashboard owner
reach the switch; an agent session is sent to ``session_set_backend``.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp.test_utils import make_mocked_request

from kiro_crew.dashboard.chat_api import slot_backend


def _request(slot: object, body: dict, *, internal: bool, session_key: str = ""):
    headers = {"X-Session-Key": session_key} if session_key else {}
    app = {"state": MagicMock(_slots={"chat-1": slot})}
    req = make_mocked_request(
        "POST",
        "/api/chat/slots/chat-1/backend",
        headers=headers,
        match_info={"slot": "chat-1"},
        app=app,
    )
    if internal:
        req["internal_auth"] = True
    return req


@pytest.mark.asyncio
async def test_an_internal_caller_with_no_session_switches_the_chat():
    slot = MagicMock()
    switch = AsyncMock(return_value={"ok": True, "backend": "claude-seat2"})
    with (
        patch("kiro_crew.dashboard.chat_handlers.switch_slot_backend", switch),
        patch(
            "kiro_crew.dashboard.chat_handlers.read_bounded_json",
            AsyncMock(return_value=({"backend": " claude-seat2 "}, None)),
        ),
    ):
        resp = await slot_backend.api_chat_slot_backend(
            _request(slot, {"backend": "claude-seat2"}, internal=True)
        )
    assert resp.status == 200
    assert json.loads(resp.body)["backend"] == "claude-seat2"
    assert switch.await_args.args[3] == "claude-seat2"


@pytest.mark.asyncio
async def test_an_agent_session_is_sent_to_session_control():
    switch = AsyncMock()
    with patch("kiro_crew.dashboard.chat_handlers.switch_slot_backend", switch):
        resp = await slot_backend.api_chat_slot_backend(
            _request(MagicMock(), {"backend": "claude-seat2"}, internal=True, session_key="chat-9")
        )
    assert resp.status == 403
    assert json.loads(resp.body)["code"] == "use_session_control"
    switch.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_non_owner_without_the_secret_is_refused():
    switch = AsyncMock()
    with (
        patch("kiro_crew.dashboard.chat_handlers.switch_slot_backend", switch),
        patch(
            "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
            return_value=False,
        ),
    ):
        resp = await slot_backend.api_chat_slot_backend(
            _request(MagicMock(), {"backend": "claude-seat2"}, internal=False)
        )
    assert resp.status == 403
    switch.assert_not_awaited()
