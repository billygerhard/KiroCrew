"""Tests for kiro_crew.apps.approval_grants — per-app tool-approval grants.

The security property under test: an app may DECLARE that it wants an unattended
tool-approval posture, but only the operator's grant file confers it, and no
runtime path — an SDK argument, an update call, a manifest env block, a stored
job row — can raise a posture the operator did not grant.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from kiro_crew.apps.approval_grants import (
    POSTURE_AUTO,
    POSTURE_DEFAULT,
    RESERVED_APPROVAL_ENV,
    AppApprovalGrants,
    app_from_owner,
    clamp_posture,
    declared_posture,
    effective_posture,
    granted_posture,
    load_app_approval_grants,
    posture_exceeds_grant,
    posture_extra_env,
    session_posture,
    verify_applied_posture,
    verify_session_posture,
    wanted_posture,
)
from kiro_crew.apps.cron_sdk import CronSDK
from kiro_crew.apps.manifest import Permissions

APP = "spec-engine"


@pytest.fixture()
def grant_home(tmp_path, monkeypatch):
    """Isolated data home so the grant file under test is the only one read."""
    home = tmp_path / "crew-home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    return home


def _write_grants(home, grants: Any, *, raw: str | None = None) -> None:
    path = home / "app_approval_grants.json"
    if raw is not None:
        path.write_text(raw, encoding="utf-8")
        return
    path.write_text(json.dumps({"version": 1, "grants": grants}), encoding="utf-8")


def _install_app(home, name: str = APP, **permissions: Any) -> None:
    """Write a minimal installed-app manifest declaring *permissions*."""
    app_root = home / "apps" / name
    app_root.mkdir(parents=True, exist_ok=True)
    (app_root / "app.json").write_text(
        json.dumps(
            {
                "name": name,
                "version": "1.0.0",
                "displayName": name,
                "description": "test app",
                "permissions": permissions,
            }
        ),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Grant table loading
# ---------------------------------------------------------------------------


class TestGrantLoading:
    def test_absent_file_grants_nothing(self, grant_home):
        assert load_app_approval_grants().postures == {}
        assert granted_posture(APP) == POSTURE_DEFAULT

    def test_granted_posture_is_read(self, grant_home):
        _write_grants(grant_home, {APP: "auto"})
        assert granted_posture(APP) == POSTURE_AUTO

    def test_unreadable_file_grants_nothing(self, grant_home):
        _write_grants(grant_home, None, raw="{not valid json")
        assert granted_posture(APP) == POSTURE_DEFAULT

    def test_non_object_file_grants_nothing(self, grant_home):
        _write_grants(grant_home, None, raw='["auto"]')
        assert granted_posture(APP) == POSTURE_DEFAULT

    def test_missing_grants_key_grants_nothing(self, grant_home):
        (grant_home / "app_approval_grants.json").write_text(
            json.dumps({"version": 1}), encoding="utf-8"
        )
        assert granted_posture(APP) == POSTURE_DEFAULT

    @pytest.mark.parametrize("value", ["AUTO", "Auto", "yolo", "true", True, 1, None, ["auto"]])
    def test_malformed_posture_grants_nothing(self, grant_home, value):
        # A grant coercion must fail toward withholding: only the literal "auto"
        # confers the permissive posture.
        _write_grants(grant_home, {APP: value})
        assert granted_posture(APP) == POSTURE_DEFAULT

    def test_app_name_is_normalized(self, grant_home):
        _write_grants(grant_home, {" Spec-Engine ": "auto"})
        assert granted_posture(APP) == POSTURE_AUTO

    def test_other_app_is_not_granted(self, grant_home):
        _write_grants(grant_home, {APP: "auto"})
        assert granted_posture("some-other-app") == POSTURE_DEFAULT

    def test_empty_app_name_is_never_granted(self, grant_home):
        _write_grants(grant_home, {APP: "auto"})
        assert granted_posture("") == POSTURE_DEFAULT

    def test_none_granted_helper(self):
        assert AppApprovalGrants.none_granted().posture_for(APP) == POSTURE_DEFAULT


# ---------------------------------------------------------------------------
# Manifest declaration
# ---------------------------------------------------------------------------


class TestManifestDeclaration:
    def test_declared_auto_parses(self):
        assert Permissions.from_dict({"approvalMode": "auto"}).approvalMode == POSTURE_AUTO

    @pytest.mark.parametrize("value", ["AUTO", "trust", True, 1, None])
    def test_malformed_declaration_requests_nothing(self, value):
        assert Permissions.from_dict({"approvalMode": value}).approvalMode == POSTURE_DEFAULT

    def test_absent_declaration_requests_nothing(self):
        assert Permissions.from_dict({}).approvalMode == POSTURE_DEFAULT

    def test_round_trips_through_to_dict(self):
        perms = Permissions.from_dict({"approvalMode": "auto", "cron": True})
        assert perms.to_dict()["approvalMode"] == POSTURE_AUTO
        assert Permissions.from_dict(perms.to_dict()).approvalMode == POSTURE_AUTO

    def test_default_posture_is_omitted_from_to_dict(self):
        assert "approvalMode" not in Permissions.from_dict({"cron": True}).to_dict()

    def test_wanted_posture_reads_the_permissions_block(self):
        assert wanted_posture({"approvalMode": "auto"}) == POSTURE_AUTO
        assert wanted_posture({"approvalMode": "nope"}) == POSTURE_DEFAULT
        assert wanted_posture(None) == POSTURE_DEFAULT

    def test_declared_posture_reads_the_installed_manifest(self, grant_home):
        _install_app(grant_home, approvalMode="auto")
        assert declared_posture(APP) == POSTURE_AUTO

    def test_declared_posture_of_unknown_app_is_default(self, grant_home):
        assert declared_posture("not-installed") == POSTURE_DEFAULT
        assert declared_posture("") == POSTURE_DEFAULT


# ---------------------------------------------------------------------------
# Effective posture = declaration ∩ grant
# ---------------------------------------------------------------------------


class TestEffectivePosture:
    def test_declared_and_granted_applies(self, grant_home):
        _write_grants(grant_home, {APP: "auto"})
        assert effective_posture(APP, POSTURE_AUTO) == POSTURE_AUTO

    def test_declared_without_grant_is_refused(self, grant_home):
        assert effective_posture(APP, POSTURE_AUTO) == POSTURE_DEFAULT

    def test_granted_without_declaration_stays_default(self, grant_home):
        # The grant is a ceiling, not an assignment: an app that never asked for
        # an unattended posture does not silently receive one.
        _write_grants(grant_home, {APP: "auto"})
        assert effective_posture(APP, POSTURE_DEFAULT) == POSTURE_DEFAULT

    def test_session_posture_resolves_both_halves(self, grant_home):
        _install_app(grant_home, approvalMode="auto")
        _write_grants(grant_home, {APP: "auto"})
        assert session_posture(APP) == POSTURE_AUTO

    def test_session_posture_without_grant_is_default(self, grant_home):
        _install_app(grant_home, approvalMode="auto")
        assert session_posture(APP) == POSTURE_DEFAULT


# ---------------------------------------------------------------------------
# No runtime path may elevate a session's own posture
# ---------------------------------------------------------------------------


class TestNoSelfElevation:
    def test_clamp_refuses_an_ungranted_request(self, grant_home):
        assert clamp_posture(APP, POSTURE_AUTO) == POSTURE_DEFAULT

    def test_clamp_honors_a_granted_request(self, grant_home):
        _write_grants(grant_home, {APP: "auto"})
        assert clamp_posture(APP, POSTURE_AUTO) == POSTURE_AUTO

    def test_clamp_rejects_an_unmodeled_posture_even_when_granted(self, grant_home):
        _write_grants(grant_home, {APP: "auto"})
        assert clamp_posture(APP, "trust") == POSTURE_DEFAULT

    def test_verify_accepts_the_granted_posture(self, grant_home):
        _write_grants(grant_home, {APP: "auto"})
        assert verify_applied_posture(APP, POSTURE_AUTO, POSTURE_AUTO) is None

    def test_verify_refuses_an_elevated_posture(self, grant_home):
        reason = verify_applied_posture(APP, POSTURE_AUTO, POSTURE_AUTO)
        assert reason is not None
        assert "does not match" in reason
        assert APP in reason

    def test_verify_refuses_a_downgraded_posture(self, grant_home):
        # An unattended run on the hook-based posture does not fail loudly, it
        # blocks forever on a prompt nobody answers — so a downgrade refuses too.
        _write_grants(grant_home, {APP: "auto"})
        assert verify_applied_posture(APP, POSTURE_AUTO, POSTURE_DEFAULT) is not None

    def test_verify_session_posture_resolves_the_declaration(self, grant_home):
        _install_app(grant_home, approvalMode="auto")
        _write_grants(grant_home, {APP: "auto"})
        assert verify_session_posture(APP, POSTURE_AUTO) is None
        assert verify_session_posture(APP, POSTURE_DEFAULT) is not None

    def test_verify_session_posture_refuses_auto_without_grant(self, grant_home):
        _install_app(grant_home, approvalMode="auto")
        assert verify_session_posture(APP, POSTURE_AUTO) is not None

    def test_ceiling_allows_the_default_posture_always(self, grant_home):
        assert posture_exceeds_grant(APP, POSTURE_DEFAULT) is None

    def test_ceiling_refuses_auto_without_grant(self, grant_home):
        reason = posture_exceeds_grant(APP, POSTURE_AUTO)
        assert reason is not None
        assert "exceeds" in reason

    def test_ceiling_allows_auto_with_grant(self, grant_home):
        _write_grants(grant_home, {APP: "auto"})
        assert posture_exceeds_grant(APP, POSTURE_AUTO) is None

    def test_ceiling_refuses_after_the_grant_is_revoked(self, grant_home):
        _write_grants(grant_home, {APP: "auto"})
        assert posture_exceeds_grant(APP, POSTURE_AUTO) is None
        _write_grants(grant_home, {})
        assert posture_exceeds_grant(APP, POSTURE_AUTO) is not None


# ---------------------------------------------------------------------------
# Reserved env control var
# ---------------------------------------------------------------------------


class TestReservedEnv:
    def test_auto_posture_injects_the_control_var(self, grant_home):
        assert posture_extra_env(POSTURE_AUTO, None) == {RESERVED_APPROVAL_ENV: POSTURE_AUTO}

    def test_default_posture_injects_nothing(self, grant_home):
        assert posture_extra_env(POSTURE_DEFAULT, None) is None

    def test_other_env_entries_are_preserved(self, grant_home):
        assert posture_extra_env(POSTURE_AUTO, {"FOO": "bar"}) == {
            "FOO": "bar",
            RESERVED_APPROVAL_ENV: POSTURE_AUTO,
        }

    def test_default_posture_strips_a_smuggled_control_var(self, grant_home):
        # An app-authored env block must not be able to set the control var and
        # have its unattended subagents auto-approved with no grant.
        assert posture_extra_env(POSTURE_DEFAULT, {RESERVED_APPROVAL_ENV: "auto"}) is None

    def test_auto_posture_does_not_duplicate_the_control_var(self, grant_home):
        assert posture_extra_env(POSTURE_AUTO, {RESERVED_APPROVAL_ENV: "auto", "A": "b"}) == {
            "A": "b",
            RESERVED_APPROVAL_ENV: POSTURE_AUTO,
        }


class TestOwnerTag:
    def test_app_owner_is_recognized(self):
        assert app_from_owner("app:spec-engine") == APP

    def test_human_creator_is_not_an_app(self):
        assert app_from_owner("U012ABCDE") == ""
        assert app_from_owner("") == ""


# ---------------------------------------------------------------------------
# Cron SDK: an app cannot self-select its own posture
# ---------------------------------------------------------------------------


@dataclass
class _FakeJob:
    id: str = "job-1"
    name: str = ""
    message: str = ""
    created_by: str = ""
    approval_mode: str = ""
    env: dict[str, str] = field(default_factory=dict)


class _FakeCronService:
    """Records what the SDK actually asked the service to persist."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.jobs: list[_FakeJob] = []

    def add_job(self, **kwargs: Any) -> _FakeJob:
        self.calls.append(kwargs)
        job = _FakeJob(
            id=f"job-{len(self.jobs) + 1}",
            name=kwargs.get("name", ""),
            created_by=kwargs.get("created_by", ""),
            approval_mode=kwargs.get("approval_mode", ""),
            env=dict(kwargs.get("env") or {}),
        )
        self.jobs.append(job)
        return job

    def list_jobs(self, include_disabled: bool = False) -> list[_FakeJob]:
        return list(self.jobs)

    def update_job(self, job_id: str, **kwargs: Any) -> _FakeJob | None:
        self.calls.append({"job_id": job_id, **kwargs})
        for job in self.jobs:
            if job.id == job_id:
                if "approval_mode" in kwargs:
                    job.approval_mode = kwargs["approval_mode"]
                if kwargs.get("env") is not None:
                    job.env = dict(kwargs["env"])
                return job
        return None


class TestCronSdkCannotSelfElevate:
    def test_ungranted_app_request_is_clamped(self, grant_home):
        svc = _FakeCronService()
        job = CronSDK(APP, svc).add_job("poll", "go", every_secs=60, approval_mode="auto")
        assert job.approval_mode == POSTURE_DEFAULT
        assert svc.calls[0]["approval_mode"] == POSTURE_DEFAULT

    def test_granted_app_request_is_honored(self, grant_home):
        _write_grants(grant_home, {APP: "auto"})
        svc = _FakeCronService()
        job = CronSDK(APP, svc).add_job("poll", "go", every_secs=60, approval_mode="auto")
        assert job.approval_mode == POSTURE_AUTO

    def test_grant_for_another_app_does_not_carry_over(self, grant_home):
        _write_grants(grant_home, {"other-app": "auto"})
        svc = _FakeCronService()
        job = CronSDK(APP, svc).add_job("poll", "go", every_secs=60, approval_mode="auto")
        assert job.approval_mode == POSTURE_DEFAULT

    def test_update_cannot_raise_a_created_job_posture(self, grant_home):
        svc = _FakeCronService()
        sdk = CronSDK(APP, svc)
        job = sdk.add_job("poll", "go", every_secs=60)
        assert job.approval_mode == POSTURE_DEFAULT
        sdk.update_job(job.id, approval_mode="auto")
        assert job.approval_mode == POSTURE_DEFAULT

    def test_update_honors_a_granted_posture(self, grant_home):
        _write_grants(grant_home, {APP: "auto"})
        svc = _FakeCronService()
        sdk = CronSDK(APP, svc)
        job = sdk.add_job("poll", "go", every_secs=60)
        sdk.update_job(job.id, approval_mode="auto")
        assert job.approval_mode == POSTURE_AUTO

    def test_reserved_env_is_stripped_on_create(self, grant_home):
        svc = _FakeCronService()
        job = CronSDK(APP, svc).add_job(
            "poll", "go", every_secs=60, env={RESERVED_APPROVAL_ENV: "auto", "KEEP": "1"}
        )
        assert job.env == {"KEEP": "1"}

    def test_reserved_env_is_stripped_on_update(self, grant_home):
        svc = _FakeCronService()
        sdk = CronSDK(APP, svc)
        job = sdk.add_job("poll", "go", every_secs=60)
        sdk.update_job(job.id, env={RESERVED_APPROVAL_ENV: "auto", "KEEP": "1"})
        assert job.env == {"KEEP": "1"}


# ---------------------------------------------------------------------------
# Fire-time refusal (the "halt a run whose posture does not match" half)
# ---------------------------------------------------------------------------


class TestFireTimePostureGate:
    def _job(self, **kwargs: Any):
        from kiro_crew.cron import CronJob, CronSchedule

        return CronJob(
            id="fire-1",
            name="poll",
            message="go",
            schedule=CronSchedule(kind="every", every_secs=600),
            **kwargs,
        )

    def test_ungranted_auto_job_is_refused(self, grant_home):
        from kiro_crew.mcp_cron import vet_job_at_fire_time

        job = self._job(created_by=f"app:{APP}", approval_mode="auto")
        reason = vet_job_at_fire_time(job)
        assert reason is not None
        assert "exceeds" in reason

    def test_granted_auto_job_runs(self, grant_home):
        from kiro_crew.mcp_cron import vet_job_at_fire_time

        _write_grants(grant_home, {APP: "auto"})
        job = self._job(created_by=f"app:{APP}", approval_mode="auto")
        assert vet_job_at_fire_time(job) is None

    def test_revoking_the_grant_refuses_an_already_scheduled_job(self, grant_home):
        from kiro_crew.mcp_cron import vet_job_at_fire_time

        _write_grants(grant_home, {APP: "auto"})
        job = self._job(created_by=f"app:{APP}", approval_mode="auto")
        assert vet_job_at_fire_time(job) is None
        _write_grants(grant_home, {})
        assert vet_job_at_fire_time(job) is not None

    def test_app_job_on_the_default_posture_runs(self, grant_home):
        from kiro_crew.mcp_cron import vet_job_at_fire_time

        assert vet_job_at_fire_time(self._job(created_by=f"app:{APP}")) is None

    def test_user_authored_job_is_untouched(self, grant_home):
        from kiro_crew.mcp_cron import vet_job_at_fire_time

        # A human-authored auto cron is the operator's own choice and predates
        # the app grant; the ceiling applies to app-owned jobs only.
        job = self._job(created_by="U012ABCDE", approval_mode="auto")
        assert vet_job_at_fire_time(job) is None
