"""The chat composer's harness selection, from POST /api/chat/slots to the slot.

The composer picks a harness before a session exists, so the pick has to travel
on the CREATE body: the harness binds when the session starts and owns it for the
session's whole life, which is why there is no switch endpoint to patch it with.
Four properties carry that, and each has a quiet failure mode:

- an unknown, unavailable, or unserviceable selection REFUSES creation, with the
  harness named and a machine-readable code. Creating the slot anyway would hand
  the user a tab labelled with a harness whose first turn fails — or one served by
  whatever the default happens to be, reported as the harness they asked for.
- an ABSENT selection creates exactly the slot it always did, with no availability
  probe. That is every install's behaviour before harnesses were selectable, and
  probing would move a signed-out kiro-cli failure out of the spawn.
- the CANONICAL id is stored, so the slot cannot report an id no listing contains.
- a stored selection round-trips through the history metadata line, because a
  restarted gateway that forgot it would render the default in the picker for a
  session bound elsewhere.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

# Bare import: `test/` is not a package and `test` is a stdlib package name.
from chat_test_helpers import _make_ready_kiro_prerequisite

from kiro_crew.acp.harness_descriptor import HarnessDescriptor
from kiro_crew.acp.harness_registry import HarnessUnavailable, UnknownHarness
from kiro_crew.acp.harness_selection import HarnessBinding, HarnessNotServiceable
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.history import ConversationLog


def _make_state(tmp_path):
    sessions = MagicMock(count=0)
    sessions.remove = AsyncMock()
    sessions.recycle_background = AsyncMock()
    sessions.get_pid = MagicMock(return_value=None)
    state = DashboardState(
        sessions=sessions,
        crons=MagicMock(list_jobs=MagicMock(return_value=[]), status=MagicMock(return_value={})),
        lessons=MagicMock(load_all=MagicMock(return_value=[])),
        start_time=0.0,
        conversation_log=ConversationLog(base_dir=tmp_path),
    )
    state.kiro_prerequisite_service = _make_ready_kiro_prerequisite()
    return state


def _make_app(state) -> web.Application:
    from kiro_crew.dashboard.chat import api_chat_slot_create

    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/chat/slots", api_chat_slot_create)
    return app


def _binding(harness_id: str) -> HarnessBinding:
    return HarnessBinding(
        descriptor=HarnessDescriptor(id=harness_id, display_name=harness_id, executable=harness_id),
        acp_backend="",
    )


@pytest.fixture(autouse=True)
def _isolate_config_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)


def _resolver(monkeypatch, outcome):
    """Replace the shared session resolver the create handler consults.

    Patched at the handler's own module reference, which is the call it actually
    makes: the point of the test is that the composer's verdict comes from the
    SAME resolver session creation will use a moment later, not from a second
    availability rule written into the HTTP layer.
    """
    calls: list[str] = []

    def fake(harness_id, *args, **kwargs):
        calls.append(harness_id)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.resolve_session_harness", fake)
    return calls


class TestCreateWithHarness:
    @pytest.mark.asyncio
    async def test_selection_is_stored_canonically(self, tmp_path, monkeypatch):
        # The resolver answers with the descriptor an ALIAS names, and that id —
        # not the spelling the client sent — is what the slot must report.
        calls = _resolver(monkeypatch, _binding("kas"))
        state = _make_state(tmp_path)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots", json={"name": "s1", "harness": "KAS-alias"})
            assert resp.status == 200
            assert (await resp.json())["harness"] == "kas"
        assert calls == ["KAS-alias"]
        assert state._slots["s1"].harness == "kas"

    @pytest.mark.asyncio
    async def test_unknown_harness_refuses_creation(self, tmp_path, monkeypatch):
        _resolver(monkeypatch, UnknownHarness("unknown harness 'ghost'"))
        state = _make_state(tmp_path)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots", json={"name": "s1", "harness": "ghost"})
            assert resp.status == 400
            body = await resp.json()
        assert body["code"] == "unknown_harness"
        # The harness is NAMED: a user who typed it needs to know which id was
        # rejected, and no other harness was substituted for it.
        assert "ghost" in body["error"]
        assert "s1" not in state._slots

    @pytest.mark.asyncio
    async def test_unavailable_harness_refuses_creation(self, tmp_path, monkeypatch):
        _resolver(monkeypatch, HarnessUnavailable("kas", "kas-acp was not found on PATH"))
        state = _make_state(tmp_path)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots", json={"name": "s1", "harness": "kas"})
            # 409, not 400: the body is well-formed and the MACHINE refused it, so
            # the same request succeeds once the operator installs the harness.
            assert resp.status == 409
            body = await resp.json()
        assert body["code"] == "harness_unavailable"
        assert "kas" in body["error"]
        assert "s1" not in state._slots

    @pytest.mark.asyncio
    async def test_unserviceable_harness_refuses_creation(self, tmp_path, monkeypatch):
        _resolver(monkeypatch, HarnessNotServiceable("codex", "no legacy backend identifier"))
        state = _make_state(tmp_path)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots", json={"name": "s1", "harness": "codex"})
            assert resp.status == 409
            body = await resp.json()
        assert body["code"] == "harness_not_serviceable"
        assert "codex" in body["error"]
        assert "s1" not in state._slots

    @pytest.mark.asyncio
    async def test_absent_selection_creates_without_probing(self, tmp_path, monkeypatch):
        calls = _resolver(monkeypatch, _binding("kiro"))
        state = _make_state(tmp_path)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots", json={"name": "s1"})
            assert resp.status == 200
            assert (await resp.json())["harness"] == ""
        # No resolution at all for an unselected creation: the default path stays
        # exactly the request it was before harnesses existed, and a kiro-cli
        # failure keeps surfacing from the spawn where the user already knows it.
        assert calls == []
        assert state._slots["s1"].harness == ""

    @pytest.mark.asyncio
    async def test_blank_selection_is_not_a_selection(self, tmp_path, monkeypatch):
        calls = _resolver(monkeypatch, _binding("kiro"))
        state = _make_state(tmp_path)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots", json={"name": "s1", "harness": "   "})
            assert resp.status == 200
        assert calls == []
        assert state._slots["s1"].harness == ""

    @pytest.mark.asyncio
    async def test_recreating_a_named_slot_on_another_harness_is_refused(
        self, tmp_path, monkeypatch
    ):
        _resolver(monkeypatch, _binding("kas"))
        state = _make_state(tmp_path)
        state.get_or_create_slot("s1")
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots", json={"name": "s1", "harness": "kas"})
            # ``get_or_create_slot`` returns an existing slot untouched, so
            # answering 200 here would report a harness binding this session does
            # not have — the substitution refusal-over-fallback exists to prevent,
            # in its quietest form.
            assert resp.status == 409
            body = await resp.json()
        assert body["code"] == "harness_binding_conflict"
        assert "kas" in body["error"]
        assert state._slots["s1"].harness == ""


class TestHarnessSurvivesRestart:
    def test_metadata_round_trip(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard import chat_persistence

        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1", harness="kas")
        meta: dict = {}
        chat_persistence._restore_slot_harness(slot, {"harness": "kas"})
        assert slot.harness == "kas"
        # A tampered or malformed value reads as "inherit the default" rather
        # than landing on the slot and reaching every connected dashboard.
        for bad in ["../../etc/passwd", "KAS", "a" * 40, 7, None, ""]:
            meta["harness"] = bad
            probe = state.get_or_create_slot(f"probe-{abs(hash(str(bad)))}")
            chat_persistence._restore_slot_harness(probe, meta)
            assert probe.harness == "", bad

    def test_serialized_slot_carries_the_binding(self, tmp_path, monkeypatch):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1", harness="kas")
        payload = json.loads(json.dumps(state.serialize_slot(slot)))
        # The picker reads this field to know which row is chosen; without it a
        # session bound to KAS renders as running the default.
        assert payload["harness"] == "kas"
