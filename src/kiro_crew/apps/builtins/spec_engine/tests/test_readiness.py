"""The not-ready state: what it says, who can read it, and what it does not do.

The requirement has three halves and each is testable separately: installation
still completes, the state names the reason, and the app does not present itself
as operational. A test that only covered the first would pass on an app that
swallows the failure entirely, which is the behavior this state replaces.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew.apps.builtins.spec_engine import readiness


class _Health:
    """The subset of AppHealthStatus a hook touches, recording what it was told."""

    def __init__(self) -> None:
        self.status = "healthy"
        self.issues: list[str] = []

    def mark_error(self, issue: str) -> None:
        self.status = "error"
        self.issues.append(issue)

    def mark_degraded(self, issue: str) -> None:
        self.status = "degraded"
        self.issues.append(issue)


def _context(data_dir: Path) -> SimpleNamespace:
    return SimpleNamespace(name="spec-engine", data_dir=data_dir, health=_Health())


class TestRequiredResourcesComeFromTheManifest:
    def test_reads_the_shipped_declarations(self):
        req = readiness.required_resources()
        assert req.app_name == "spec-engine"
        assert req.skills == ("spec-engine-discovery",)
        assert req.servers == ("spec-engine",)
        assert req.problems == ()

    def test_a_manifest_declaring_no_skill_is_a_problem(self):
        # Otherwise dropping the skill from the manifest would turn a broken
        # install into a passing one: nothing declared, nothing missing, ready.
        req = readiness.required_resources({"name": "spec-engine", "mcpServers": {"s": {}}})
        assert any("discovery skill" in p for p in req.problems)
        assert readiness.assess(
            present_skills=(), present_servers=("s",), required=req
        ).ready is False

    def test_a_manifest_declaring_no_server_is_a_problem(self):
        req = readiness.required_resources({"name": "spec-engine", "skills": ["skills/x"]})
        assert any("MCP server" in p for p in req.problems)
        assert readiness.assess(
            present_skills=("x",), present_servers=(), required=req
        ).ready is False

    def test_an_unreadable_manifest_is_a_problem(self, tmp_path, monkeypatch):
        monkeypatch.setattr(readiness, "APP_MANIFEST_PATH", tmp_path / "absent.json")
        req = readiness.required_resources()
        assert req.problems
        assert readiness.assess(present_skills=(), present_servers=(), required=req).ready is False


class TestAssess:
    def test_ready_only_when_both_arrived(self):
        verdict = readiness.assess(
            present_skills=("spec-engine-discovery",), present_servers=("spec-engine",)
        )
        assert verdict.ready is True
        assert verdict.operational is True
        assert verdict.reasons == ()
        assert verdict.checked_at

    def test_a_missing_skill_names_the_skill(self):
        verdict = readiness.assess(present_skills=(), present_servers=("spec-engine",))
        assert verdict.ready is False
        assert verdict.operational is False
        assert any("spec-engine/spec-engine-discovery" in r for r in verdict.reasons)

    def test_a_missing_server_names_the_server(self):
        verdict = readiness.assess(
            present_skills=("spec-engine-discovery",), present_servers=()
        )
        assert verdict.ready is False
        assert any("spec-engine:spec-engine" in r for r in verdict.reasons)

    def test_a_registered_server_does_not_compensate_for_a_missing_skill(self):
        # A partial miss is not a partial pass: an agent holding the tools but
        # never shown the skill has no reason to reach for them.
        assert (
            readiness.assess(present_skills=(), present_servers=("spec-engine",)).ready is False
        )

    def test_registrar_errors_are_carried_into_the_reasons(self):
        verdict = readiness.assess(
            present_skills=("spec-engine-discovery",),
            present_servers=("spec-engine",),
            errors=["MCP server registration failed: disk full"],
        )
        assert verdict.ready is False
        assert "MCP server registration failed: disk full" in verdict.reasons


class TestRecordedState:
    def test_round_trips(self, tmp_path):
        verdict = readiness.assess(
            present_skills=("spec-engine-discovery",), present_servers=("spec-engine",)
        )
        readiness.record(verdict, tmp_path)
        assert readiness.current(tmp_path).ready is True

    def test_reasons_survive_the_round_trip(self, tmp_path):
        verdict = readiness.assess(present_skills=(), present_servers=())
        readiness.record(verdict, tmp_path)
        restored = readiness.current(tmp_path)
        assert restored.ready is False
        assert restored.reasons == verdict.reasons

    def test_an_unassessed_app_is_not_operational(self, tmp_path):
        # The default matters more than any other case here: an app nothing has
        # checked must not look healthy by omission.
        state = readiness.current(tmp_path)
        assert state.ready is False
        assert state.operational is False
        assert readiness.NOT_ASSESSED in state.reasons

    def test_a_corrupt_state_file_is_not_operational(self, tmp_path):
        readiness.status_path(tmp_path).write_text("{not json", encoding="utf-8")
        assert readiness.current(tmp_path).ready is False

    def test_ready_with_reasons_is_refused(self, tmp_path):
        # A hand-edited or half-written file claiming both must not read as ready.
        readiness.status_path(tmp_path).write_text(
            json.dumps({"ready": True, "reasons": ["the skill never registered"]}),
            encoding="utf-8",
        )
        state = readiness.current(tmp_path)
        assert state.ready is False
        assert state.reasons == ("the skill never registered",)

    def test_a_non_object_state_file_is_not_operational(self, tmp_path):
        readiness.status_path(tmp_path).write_text("[]", encoding="utf-8")
        assert readiness.current(tmp_path).ready is False


class TestStartupHook:
    def test_a_failed_registration_does_not_abort_the_lifecycle(self, tmp_path, monkeypatch):
        # Installation and enablement still complete: the hook returns instead of
        # raising, and a raising hook is what would make the host mark the enable
        # as failed and degrade the app for a reason it could not name.
        monkeypatch.setattr(readiness, "observe", lambda req=None: (set(), set()))
        ctx = _context(tmp_path)
        readiness.on_startup(ctx)
        # It got far enough to record the state rather than dying part way.
        assert readiness.status_path(tmp_path).is_file()

    def test_a_failed_registration_records_a_readable_not_ready_state(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(readiness, "observe", lambda req=None: (set(), {"spec-engine"}))
        readiness.on_startup(_context(tmp_path))

        state = readiness.current(tmp_path)
        assert state.ready is False
        assert state.operational is False
        assert any("spec-engine/spec-engine-discovery" in r for r in state.reasons)

    def test_a_failed_registration_marks_the_app_not_operational(self, tmp_path, monkeypatch):
        monkeypatch.setattr(readiness, "observe", lambda req=None: (set(), set()))
        ctx = _context(tmp_path)
        readiness.on_startup(ctx)
        assert ctx.health.status == "error"
        assert ctx.health.issues and "not ready" in ctx.health.issues[0]

    def test_a_complete_registration_reports_operational(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            readiness,
            "observe",
            lambda req=None: ({"spec-engine-discovery"}, {"spec-engine"}),
        )
        ctx = _context(tmp_path)
        readiness.on_startup(ctx)
        assert readiness.current(tmp_path).operational is True
        assert ctx.health.status == "healthy"

    def test_a_check_that_itself_fails_is_a_not_ready_state(self, tmp_path, monkeypatch):
        def boom(req=None):
            raise OSError("skills directory is unreadable")

        monkeypatch.setattr(readiness, "observe", boom)
        ctx = _context(tmp_path)
        readiness.on_startup(ctx)

        state = readiness.current(tmp_path)
        assert state.ready is False
        assert any("skills directory is unreadable" in r for r in state.reasons)
        assert ctx.health.status == "error"

    def test_a_state_that_cannot_be_written_still_reads_as_not_operational(
        self, tmp_path, monkeypatch
    ):
        # The recording itself can fail. The app must still not look ready.
        monkeypatch.setattr(
            readiness,
            "observe",
            lambda req=None: ({"spec-engine-discovery"}, {"spec-engine"}),
        )
        unwritable = tmp_path / "data"
        unwritable.write_text("this is a file, not a directory", encoding="utf-8")
        readiness.on_startup(SimpleNamespace(name="spec-engine", data_dir=unwritable, health=None))
        assert readiness.current(unwritable).ready is False


@pytest.mark.parametrize("missing", ["skill", "server", "both"])
def test_every_partial_registration_is_not_ready(missing):
    """Neither resource may be missing quietly, in any combination."""
    skills = () if missing in ("skill", "both") else ("spec-engine-discovery",)
    servers = () if missing in ("server", "both") else ("spec-engine",)
    assert readiness.assess(present_skills=skills, present_servers=servers).ready is False
