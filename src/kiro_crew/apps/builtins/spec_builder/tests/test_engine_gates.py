"""The app's approval path, and the execution gate that reads it.

This app used to hold three answers the engine already owned, and each of its own
was more permissive:

* **no approvals at all.** Nothing in the app ever called ``phases.approve``, so
  the engine's approval table was empty for every spec the app drove.
* **a client-side transition map.** The SPA decided which phase followed which and
  sent an "approved -- proceed" prompt the engine never authorised.
* **a tasks.md existence check as the execution gate.** A spec holding one
  never-validated, never-approved ``tasks.md`` executed, while
  ``phases.execution_blocking_reasons`` would have refused it at every gate.

The order those were fixed in matters and is asserted here: an approval can be
recorded through the app BEFORE the gate reads one, and the same spec can have one
gate approved while execution is still refused for the gates that are not. Routing
the gate first would have refused every execution the app permitted.

Every assertion about a recorded approval reads it back through a SEPARATE store
opened over the engine's own database, not through the app: an approval the app
recorded somewhere only the app can see would satisfy a test that asked the app.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.apps.builtins.spec_builder.backend import routes
from kiro_crew.apps.builtins.spec_engine.engine.state import SpecRef, StateStore

from .test_routes import (
    _BASE,
    _READY_DESIGN,
    _READY_REQUIREMENTS,
    _READY_TASKS,
    _make_client,
    _write_ready_documents,
)


def _index(tmp_path: Path, name: str, **extra) -> Path:
    """Index one spec whose documents are format-clean, and return its directory."""
    working_dir = tmp_path / "wd"
    spec_dir = working_dir / ".kiro" / "specs" / name
    _write_ready_documents(spec_dir)
    routes._save_index(
        {
            name: {
                "spec_dir": str(spec_dir),
                "working_dir": str(working_dir),
                "spec_type": "feature",
                **extra,
            }
        }
    )
    return spec_dir


def _engine_approvals(tmp_path: Path, name: str) -> dict[str, str]:
    """Gate -> approver, read from the ENGINE's database by a separate store.

    Opened fresh rather than through ``routes._engine_store()``: the claim is that
    the approval is in the engine's own table, and reusing the app's cached handle
    would not distinguish that from a value the app kept in memory.
    """
    store = StateStore(root=routes._ENGINE_STATE_ROOT)
    try:
        ref = SpecRef.of(tmp_path / "wd", name)
        return {record.gate: record.actor for record in store.list_approvals(ref)}
    finally:
        store.close()


class TestTheApprovalPathExists:
    @pytest.mark.asyncio
    async def test_approving_a_gate_records_it_with_the_engine(self, tmp_path, monkeypatch):
        client = _make_client(monkeypatch, tmp_path)
        _index(tmp_path, "s")

        await client.start_server()
        try:
            resp = await client.post(f"{_BASE}/specs/s/approve", json={"gate": "requirements"})
            body = await resp.json()
        finally:
            await client.close()

        assert resp.status == 200, body
        assert body["gate"] == "requirements"
        # The row is in the engine's table, under the authenticated person's name.
        assert _engine_approvals(tmp_path, "s") == {"requirements": "tester"}

    @pytest.mark.asyncio
    async def test_the_approval_is_attributed_to_the_authenticated_user_only(
        self, tmp_path, monkeypatch
    ):
        client = _make_client(monkeypatch, tmp_path)
        _index(tmp_path, "s")

        await client.start_server()
        try:
            # A body that tries to name its own approver, including the engine's
            # reserved policy identity. The actor comes from the authenticated
            # request and nothing in the body can reach it.
            resp = await client.post(
                f"{_BASE}/specs/s/approve",
                json={"gate": "requirements", "actor": "autonomy-policy:forged", "user": "someone"},
            )
        finally:
            await client.close()

        assert resp.status == 200
        assert _engine_approvals(tmp_path, "s") == {"requirements": "tester"}

    @pytest.mark.asyncio
    async def test_an_invalid_document_cannot_be_approved_and_records_nothing(
        self, tmp_path, monkeypatch
    ):
        client = _make_client(monkeypatch, tmp_path)
        spec_dir = _index(tmp_path, "s")
        (spec_dir / "requirements.md").write_text("# Requirements Document\n", encoding="utf-8")

        await client.start_server()
        try:
            resp = await client.post(f"{_BASE}/specs/s/approve", json={"gate": "requirements"})
            body = await resp.json()
        finally:
            await client.close()

        assert resp.status == 409
        assert body["code"] == "approval_refused"
        # The engine's own reason code, not a message this app composed.
        assert [reason["code"] for reason in body["reasons"]] == ["phase.document-invalid"]
        assert _engine_approvals(tmp_path, "s") == {}

    @pytest.mark.asyncio
    async def test_a_gate_the_plan_does_not_have_is_refused(self, tmp_path, monkeypatch):
        client = _make_client(monkeypatch, tmp_path)
        _index(tmp_path, "s")

        await client.start_server()
        try:
            resp = await client.post(f"{_BASE}/specs/s/approve", json={"gate": "invented"})
            body = await resp.json()
        finally:
            await client.close()

        assert resp.status == 409
        assert [reason["code"] for reason in body["reasons"]] == ["phase.gate-not-in-plan"]
        assert _engine_approvals(tmp_path, "s") == {}

    @pytest.mark.asyncio
    async def test_an_approval_must_name_its_gate(self, tmp_path, monkeypatch):
        client = _make_client(monkeypatch, tmp_path)
        _index(tmp_path, "s")

        await client.start_server()
        try:
            resp = await client.post(f"{_BASE}/specs/s/approve", json={})
            body = await resp.json()
        finally:
            await client.close()

        assert resp.status == 400
        assert body["code"] == "gate_required"

    @pytest.mark.asyncio
    async def test_an_unauthenticated_request_records_no_approval(self, tmp_path, monkeypatch):
        # No auth middleware: the app must not record an approval for nobody.
        from .test_routes import _redirect_state

        _redirect_state(monkeypatch, tmp_path)
        _index(tmp_path, "s")
        app = web.Application()
        routes.register_routes(app)
        client = TestClient(TestServer(app))

        await client.start_server()
        try:
            resp = await client.post(f"{_BASE}/specs/s/approve", json={"gate": "requirements"})
        finally:
            await client.close()

        assert resp.status == 401
        assert _engine_approvals(tmp_path, "s") == {}


class TestTheApprovalPathCameFirst:
    """The intermediate state: approvals recordable, execution still refused.

    This is the ordering the whole change depends on. If the gate had been routed
    through the engine before this path existed, every execution the app allowed
    would have been refused with an approval-missing reason and nothing could have
    approved its way out.
    """

    @pytest.mark.asyncio
    async def test_one_gate_can_be_approved_while_execution_is_still_refused(
        self, tmp_path, monkeypatch
    ):
        client = _make_client(monkeypatch, tmp_path)
        _index(tmp_path, "s")

        await client.start_server()
        try:
            approved = await client.post(
                f"{_BASE}/specs/s/approve", json={"gate": "requirements"}
            )
            detail = await (await client.get(f"{_BASE}/specs/s")).json()
        finally:
            await client.close()

        assert approved.status == 200
        engine = detail["engine"]
        assert engine["addressable"] is True
        gates = {gate["gate"]: gate for gate in engine["gates"]}
        assert gates["requirements"]["approved"] is True
        assert gates["design"]["approved"] is False
        # The approval landed and the gate still refuses, which is exactly the
        # intermediate state: recording works before the gate reads it.
        assert engine["can_execute"] is False
        assert "phase.approval-missing" in [
            reason["code"] for reason in engine["execution_blocked_by"]
        ]

    @pytest.mark.asyncio
    async def test_approving_every_gate_opens_the_engines_execution_gate(
        self, tmp_path, monkeypatch
    ):
        client = _make_client(monkeypatch, tmp_path)
        _index(tmp_path, "s")

        await client.start_server()
        try:
            for gate in ("requirements", "design", "tasks"):
                resp = await client.post(f"{_BASE}/specs/s/approve", json={"gate": gate})
                assert resp.status == 200, await resp.json()
            detail = await (await client.get(f"{_BASE}/specs/s")).json()
        finally:
            await client.close()

        assert detail["engine"]["can_execute"] is True
        assert detail["engine"]["execution_blocked_by"] == []


class TestTheExecutionGateIsTheEngines:
    @pytest.mark.asyncio
    async def test_a_tasks_only_spec_is_refused_and_nothing_is_dispatched(
        self, tmp_path, monkeypatch
    ):
        """The defect, stated as a test.

        A spec holding one never-validated, never-approved tasks.md is precisely
        what this app used to execute: its gate asked only whether the file
        existed.
        """
        client = _make_client(monkeypatch, tmp_path)
        spec_dir = tmp_path / "wd" / ".kiro" / "specs" / "raw"
        spec_dir.mkdir(parents=True)
        (spec_dir / "tasks.md").write_text("- [ ] do everything", encoding="utf-8")
        routes._save_index(
            {"raw": {"spec_dir": str(spec_dir), "working_dir": str(tmp_path / "wd")}}
        )
        dispatched: list[str] = []
        monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: dispatched.append("turn"))

        await client.start_server()
        try:
            resp = await client.post(f"{_BASE}/specs/raw/execute", json={})
            body = await resp.json()
        finally:
            await client.close()

        assert resp.status == 409
        assert body["code"] == "execution_blocked"
        codes = [reason["code"] for reason in body["reasons"]]
        assert "phase.approval-missing" in codes
        assert dispatched == []
        # Refused before any side effect: no claim was recorded.
        assert routes._load_index()["raw"].get("status") != "executing"

    @pytest.mark.asyncio
    async def test_a_spec_whose_documents_are_written_but_unapproved_is_refused(
        self, tmp_path, monkeypatch
    ):
        # Every document present and valid, no approval anywhere. Nothing about
        # the files says "a person agreed to this", which is the whole point of the
        # approval half of the gate.
        client = _make_client(monkeypatch, tmp_path)
        _index(tmp_path, "s")

        await client.start_server()
        try:
            resp = await client.post(f"{_BASE}/specs/s/execute", json={})
            body = await resp.json()
        finally:
            await client.close()

        assert resp.status == 409
        assert {reason["code"] for reason in body["reasons"]} == {"phase.approval-missing"}

    @pytest.mark.asyncio
    async def test_an_edit_after_approval_closes_the_gate_again(self, tmp_path, monkeypatch):
        client = _make_client(monkeypatch, tmp_path)
        spec_dir = _index(tmp_path, "s")

        await client.start_server()
        try:
            for gate in ("requirements", "design", "tasks"):
                assert (
                    await client.post(f"{_BASE}/specs/s/approve", json={"gate": gate})
                ).status == 200
            # A formatting-neutral edit to an already approved document: still
            # valid, no longer the bytes anybody approved.
            (spec_dir / "requirements.md").write_text(
                _READY_REQUIREMENTS + "\n<!-- reviewed -->\n", encoding="utf-8"
            )
            resp = await client.post(f"{_BASE}/specs/s/execute", json={})
            body = await resp.json()
        finally:
            await client.close()

        assert resp.status == 409
        assert "phase.approval-stale" in [reason["code"] for reason in body["reasons"]]

    @pytest.mark.asyncio
    async def test_the_gate_fails_closed_when_the_engine_cannot_be_asked(
        self, tmp_path, monkeypatch
    ):
        client = _make_client(monkeypatch, tmp_path)
        _index(tmp_path, "s")

        def _boom():
            raise RuntimeError("engine state unavailable")

        monkeypatch.setattr(routes, "_engine_store", _boom)
        dispatched: list[str] = []
        monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: dispatched.append("turn"))

        await client.start_server()
        try:
            resp = await client.post(f"{_BASE}/specs/s/execute", json={})
            body = await resp.json()
        finally:
            await client.close()

        # A gate with a permissive error path is the gate an attacker aims at.
        assert resp.status == 409
        assert body["code"] == "engine_unavailable"
        assert dispatched == []

    @pytest.mark.asyncio
    async def test_a_spec_outside_the_engine_layout_is_refused_not_waved_through(
        self, tmp_path, monkeypatch
    ):
        # A spec directory that is not <project>/.kiro/specs/<name>, which the app
        # allows through its base_path setting. The engine addresses documents by
        # that layout, so an approval recorded for this spec would hash a document
        # at a path nobody edited -- and executing it ungoverned is the other way
        # to be wrong.
        client = _make_client(monkeypatch, tmp_path)
        spec_dir = tmp_path / "elsewhere" / "s"
        spec_dir.mkdir(parents=True)
        (spec_dir / "requirements.md").write_text(_READY_REQUIREMENTS, encoding="utf-8")
        (spec_dir / "design.md").write_text(_READY_DESIGN, encoding="utf-8")
        (spec_dir / "tasks.md").write_text(_READY_TASKS, encoding="utf-8")
        routes._save_index(
            {"s": {"spec_dir": str(spec_dir), "working_dir": str(tmp_path / "wd")}}
        )
        (tmp_path / "wd").mkdir(exist_ok=True)

        await client.start_server()
        try:
            execute = await client.post(f"{_BASE}/specs/s/execute", json={})
            execute_body = await execute.json()
            approve = await client.post(f"{_BASE}/specs/s/approve", json={"gate": "requirements"})
            approve_body = await approve.json()
        finally:
            await client.close()

        assert execute.status == 409
        assert execute_body["code"] == "spec_outside_engine_layout"
        assert approve.status == 409
        assert approve_body["code"] == "spec_outside_engine_layout"

    @pytest.mark.asyncio
    async def test_the_gate_runs_before_the_execution_claim(self, tmp_path, monkeypatch):
        # Ordering, read off the source: the refusal has to happen before the
        # compare-and-set that records the run as executing, or a refused
        # execution would leave the spec marked as building.
        import inspect

        src = inspect.getsource(routes._handle_handoff)
        assert src.index("_execution_refusal") < src.index("_claim_execution")


class TestTransitionsComeFromTheEngine:
    @pytest.mark.asyncio
    async def test_the_advance_response_carries_the_engines_transition(
        self, tmp_path, monkeypatch
    ):
        client = _make_client(monkeypatch, tmp_path)
        _index(tmp_path, "s")

        await client.start_server()
        try:
            resp = await client.post(f"{_BASE}/specs/s/advance", json={"gate": "requirements"})
            body = await resp.json()
        finally:
            await client.close()

        assert resp.status == 200, body
        # The engine decided both the gate that was left and where the spec goes.
        # A client that computed "requirements -> design" for itself would be a
        # second authority on the transition. ``from_phase`` is the phase as the
        # engine derived it for this advance -- already past requirements, because
        # the approval that precedes the transition settled that gate.
        assert body["gate"] == "requirements"
        assert body["to_phase"] == "design"
        # An advance records the approval too, so the transition it reports rests
        # on a fact rather than on a prompt that says one exists.
        assert _engine_approvals(tmp_path, "s") == {"requirements": "tester"}

    @pytest.mark.asyncio
    async def test_an_advance_on_an_invalid_document_records_and_moves_nothing(
        self, tmp_path, monkeypatch
    ):
        client = _make_client(monkeypatch, tmp_path)
        spec_dir = _index(tmp_path, "s")
        (spec_dir / "requirements.md").write_text("# Requirements Document\n", encoding="utf-8")

        await client.start_server()
        try:
            resp = await client.post(f"{_BASE}/specs/s/advance", json={"gate": "requirements"})
            body = await resp.json()
        finally:
            await client.close()

        assert resp.status == 409
        assert body["code"] == "approval_refused"
        assert _engine_approvals(tmp_path, "s") == {}

    @pytest.mark.asyncio
    async def test_advancing_past_the_last_gate_is_the_engines_refusal(
        self, tmp_path, monkeypatch
    ):
        client = _make_client(monkeypatch, tmp_path)
        _index(tmp_path, "s")

        await client.start_server()
        try:
            for gate in ("requirements", "design"):
                assert (
                    await client.post(f"{_BASE}/specs/s/advance", json={"gate": gate})
                ).status == 200
            last = await client.post(f"{_BASE}/specs/s/advance", json={"gate": "tasks"})
            body = await last.json()
        finally:
            await client.close()

        # Past the last document there is no further authoring phase, and the
        # engine says so by naming the ready phase rather than inventing one.
        assert last.status == 200, body
        assert body["to_phase"] == "ready"

    @pytest.mark.asyncio
    async def test_an_advance_must_name_the_gate_it_is_leaving(self, tmp_path, monkeypatch):
        client = _make_client(monkeypatch, tmp_path)
        _index(tmp_path, "s")

        await client.start_server()
        try:
            resp = await client.post(f"{_BASE}/specs/s/advance", json={})
            body = await resp.json()
        finally:
            await client.close()

        assert resp.status == 400
        assert body["code"] == "gate_required"

    @pytest.mark.asyncio
    async def test_the_detail_read_reports_the_gate_a_person_is_asked_about(
        self, tmp_path, monkeypatch
    ):
        client = _make_client(monkeypatch, tmp_path)
        _index(tmp_path, "s")

        await client.start_server()
        try:
            first = await (await client.get(f"{_BASE}/specs/s")).json()
            assert (
                await client.post(f"{_BASE}/specs/s/approve", json={"gate": "requirements"})
            ).status == 200
            second = await (await client.get(f"{_BASE}/specs/s")).json()
        finally:
            await client.close()

        # What the SPA labels its approve control from, instead of deriving it from
        # a phase string that knows nothing about approvals.
        assert first["engine"]["current_gate"] == "requirements"
        assert second["engine"]["current_gate"] == "design"


class TestTheEngineStateIsNotTheAppsOwn:
    def test_the_engine_root_is_the_engines_own_and_resolved_per_call(
        self, tmp_path, monkeypatch
    ):
        # Resolved per call for the same reason every other path in this app is:
        # a value bound at import freezes whichever KIROCREW_HOME was active then.
        monkeypatch.setattr(routes, "_ENGINE_STATE_ROOT", None)
        monkeypatch.setattr(routes, "config_dir", lambda: tmp_path / "home")
        first = routes._engine_state_root()
        monkeypatch.setattr(routes, "_ENGINE_STATE_ROOT", tmp_path / "override")
        assert routes._engine_state_root() == tmp_path / "override"
        assert first.parts[-3:] == ("spec-engine", "data", "state")

    def test_the_app_writes_its_approvals_where_the_engine_reads_them(
        self, tmp_path, monkeypatch
    ):
        from .test_routes import _redirect_state

        _redirect_state(monkeypatch, tmp_path)
        spec_dir = _index(tmp_path, "s")
        meta = routes._load_index()["s"]

        outcome = routes._record_gate_approval("s", meta, "requirements", actor="tester")

        assert outcome is not None and outcome.ok
        # Same database, opened independently, containing the row -- and the hash
        # is of the document actually on disk, which is what makes staleness work.
        approvals = _engine_approvals(tmp_path, "s")
        assert approvals == {"requirements": "tester"}
        assert json.loads(json.dumps(sorted(approvals))) == ["requirements"]
        assert (spec_dir / "requirements.md").is_file()
