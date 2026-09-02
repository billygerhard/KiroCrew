"""Per-cron ACP harness selection: persistence, writers, and the fire-time gate.

A cron job carries a ``harness`` override beside its ``model`` override. The
axis is deliberately validated when the job FIRES rather than when it is
written, because the registry answers for the machine as it is right now — so
these tests pin both halves of that decision:

* a write accepts a harness the registry does not serve (it may be installed
  later), through every writer: ``add_job``, ``update_job``, the REST create and
  patch handlers, and the MCP tools;
* a RUN whose harness is unknown, unavailable, or unserviceable fails with the
  harness named in ``last_error``, dispatches nothing, and leaves the job
  registered and enabled so a repaired harness heals the schedule.

Resolution is driven through the ``resolve_session_harness`` seam with the real
refusal classes rather than through the registry, so nothing here depends on
which harness binaries the test host happens to have installed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import kiro_crew.agent_sdk.harness as sel_mod
from kiro_crew.acp.harness_registry import (
    HARNESS_KAS,
    HARNESS_KIRO,
    HarnessUnavailable,
    UnknownHarness,
)
from kiro_crew.acp.harness_registry import registry as harness_registry
from kiro_crew.acp.harness_selection import HarnessBinding, HarnessNotServiceable
from kiro_crew.cron import CronJob, CronSchedule, CronService, resolve_job_harness

# ── Fixtures ──


@pytest.fixture(autouse=True)
def _isolate_cron_store(monkeypatch, tmp_path):
    monkeypatch.setattr("kiro_crew.cron._DEFAULT_DIR", tmp_path)
    yield


@pytest.fixture(autouse=True)
def _restore_kiro_crew_log_level():
    """Give the ``kiro_crew`` logger back the level it had.

    ``cli.main()`` runs ``_setup_cli_logging``, which sets an EXPLICIT level on
    that logger and never restores it. The level outlives the test and the whole
    worker, so a later ``caplog.at_level`` test silently loses records it filtered
    out — a failure that appears in an unrelated file and only under the ordering
    that happens to put a CLI test first.
    """
    logger = logging.getLogger("kiro_crew")
    previous = logger.level
    try:
        yield
    finally:
        logger.setLevel(previous)


def _job(**kw) -> CronJob:
    base = dict(
        id="j1",
        name="nightly",
        message="go",
        schedule=CronSchedule(kind="every", every_secs=300),
    )
    base.update(kw)
    return CronJob(**base)  # type: ignore[arg-type]


# ── Persistence ──


class TestPersistence:
    def test_default_is_inherit(self, tmp_path):
        svc = CronService(base_dir=tmp_path)
        job = svc.add_job(name="j", message="m", every_secs=3600)
        assert job.harness == ""

    def test_add_job_persists_harness_across_a_reload(self, tmp_path):
        svc = CronService(base_dir=tmp_path)
        job = svc.add_job(name="j", message="m", every_secs=3600, harness="kas")
        assert job.harness == "kas"
        # A separate service instance reads the file, so this pins the store
        # round-trip (_save + _job_from_record), not the in-memory object.
        reloaded = CronService(base_dir=tmp_path).list_jobs()
        assert [j.harness for j in reloaded] == ["kas"]
        assert reloaded[0].id == job.id

    def test_add_job_strips_surrounding_whitespace(self, tmp_path):
        svc = CronService(base_dir=tmp_path)
        job = svc.add_job(name="j", message="m", every_secs=3600, harness="  kas  ")
        assert job.harness == "kas"

    def test_add_job_accepts_an_unregistered_harness(self, tmp_path):
        """A write must not require the harness to exist yet.

        The operator may be about to install it; the run is what judges the
        machine's current state.
        """
        svc = CronService(base_dir=tmp_path)
        job = svc.add_job(name="j", message="m", every_secs=3600, harness="not-installed-yet")
        assert job.harness == "not-installed-yet"

    def test_add_job_rejects_a_non_string_harness(self, tmp_path):
        svc = CronService(base_dir=tmp_path)
        with pytest.raises(ValueError, match="harness must be a string"):
            svc.add_job(name="j", message="m", every_secs=3600, harness=["kas"])  # type: ignore[arg-type]

    def test_add_job_rejects_an_oversize_harness(self, tmp_path):
        from kiro_crew.validation import MAX_SHORT_STRING

        svc = CronService(base_dir=tmp_path)
        with pytest.raises(ValueError, match="harness exceeds max length"):
            svc.add_job(
                name="j", message="m", every_secs=3600, harness="x" * (MAX_SHORT_STRING + 1)
            )

    def test_update_job_sets_and_clears_the_override(self, tmp_path):
        svc = CronService(base_dir=tmp_path)
        job = svc.add_job(name="j", message="m", every_secs=3600, harness="kas")
        assert svc.update_job(job.id, harness="kiro").harness == HARNESS_KIRO
        # An empty value is a real assignment, not a skipped no-op: clearing back
        # to inherit has to be expressible.
        assert svc.update_job(job.id, harness="").harness == ""
        assert CronService(base_dir=tmp_path).list_jobs()[0].harness == ""

    def test_update_job_rejects_a_non_string_harness(self, tmp_path):
        svc = CronService(base_dir=tmp_path)
        job = svc.add_job(name="j", message="m", every_secs=3600, harness="kas")
        with pytest.raises(ValueError, match="harness must be a string"):
            svc.update_job(job.id, harness=7)
        assert svc.list_jobs()[0].harness == "kas"


# ── Fire-time resolution ──
#
# The resolver delegates to ``harness_selection.resolve_session_harness``, which
# is the one place the two configuration keys compose into a harness. These tests
# drive that seam directly with the REAL refusal classes rather than through the
# registry, for two reasons: what belongs to cron is the MAPPING (which verdicts
# refuse a run, which degrade, and what the operator reads in ``last_error``),
# and a registry-driven test would additionally depend on which harness binaries
# the test host happens to have installed.


def _binding(harness_id: str) -> HarnessBinding:
    """A real binding for *harness_id*, as the resolver would return."""
    return HarnessBinding(descriptor=harness_registry().get(harness_id), acp_backend="")


@pytest.fixture
def resolver(monkeypatch):
    """Replace the harness resolver with a recorded, scriptable stand-in."""

    class _Resolver:
        def __init__(self) -> None:
            self.calls: list[str] = []
            self.outcome: object = _binding(HARNESS_KIRO)

        def __call__(self, harness_id: str = "", *_args, **_kwargs):
            self.calls.append(harness_id)
            if isinstance(self.outcome, BaseException):
                raise self.outcome
            return self.outcome

    stub = _Resolver()
    monkeypatch.setattr(sel_mod, "resolve_session_harness", stub)
    return stub


class TestResolveJobHarness:
    def test_empty_selection_resolves_to_the_default_without_refusing(self, resolver):
        harness_id, reason = resolve_job_harness(_job())
        assert (harness_id, reason) == (HARNESS_KIRO, "")
        # Delegated rather than re-derived: the precedence between
        # agent.default_harness and the legacy agent.acp_backend has one owner.
        assert resolver.calls == [""]

    def test_an_explicit_selection_is_passed_through_stripped(self, resolver):
        resolver.outcome = _binding(HARNESS_KAS)
        assert resolve_job_harness(_job(harness="  kas  ")) == (HARNESS_KAS, "")
        assert resolver.calls == ["kas"]

    def test_unknown_harness_refuses_and_names_it(self, resolver):
        resolver.outcome = UnknownHarness("unknown harness 'nope' (registered: kiro)")
        harness_id, reason = resolve_job_harness(_job(harness="nope"))
        assert harness_id == ""
        assert "nope" in reason
        assert "not registered" in reason

    def test_unavailable_harness_refuses_with_the_registry_reason(self, resolver):
        resolver.outcome = HarnessUnavailable("kiro", "kiro-cli was not found on PATH")
        harness_id, reason = resolve_job_harness(_job(harness="kiro"))
        assert harness_id == ""
        assert "kiro" in reason
        assert "kiro-cli was not found on PATH" in reason

    def test_unserviceable_harness_refuses_and_names_it(self, resolver):
        resolver.outcome = HarnessNotServiceable("codex", "no legacy backend identifier")
        harness_id, reason = resolve_job_harness(_job(harness="codex"))
        assert harness_id == ""
        assert "codex" in reason
        assert "no legacy backend identifier" in reason

    def test_inherit_survives_an_unresolvable_default(self, resolver):
        """A job that opted into nothing must not gain a new failure mode."""
        resolver.outcome = RuntimeError("registry exploded")
        assert resolve_job_harness(_job()) == ("", "")

    def test_inherit_survives_a_refused_default(self, resolver):
        resolver.outcome = HarnessUnavailable("kiro", "kiro-cli was not found on PATH")
        assert resolve_job_harness(_job()) == ("", "")

    def test_a_non_string_selection_reads_as_no_selection(self, resolver, caplog):
        """Only a corrupted store can produce one, and it names no harness.

        Read as "unset" rather than as a refusal, so a job whose file was
        hand-edited still runs — and logged, so the ignored override is findable.
        """
        job = _job()
        job.harness = 42  # type: ignore[assignment]
        with caplog.at_level("WARNING"):
            harness_id, reason = resolve_job_harness(job)
        assert (harness_id, reason) == (HARNESS_KIRO, "")
        assert "non-string harness" in caplog.text

    def test_explicit_selection_fails_closed_on_an_unexpected_error(self, resolver):
        """The opposite direction: never dispatch elsewhere behind the operator."""
        resolver.outcome = RuntimeError("registry exploded")
        with pytest.raises(RuntimeError, match="registry exploded"):
            resolve_job_harness(_job(harness="kas"))


# ── Gateway fire-time gate ──


def _run_cron_callback(job: CronJob, *, model_unavailable: bool = False) -> dict:
    """Invoke the real ``_cron_callback`` closure for *job*.

    Returns the recorded session-creation calls so a test can assert that a
    refused run dispatched nothing at all, and what each dispatch asked for.

    ``model_unavailable`` makes the FIRST session creation fail the way a pinned
    model that the account cannot serve does, which is the one path that calls
    ``get_or_create`` twice.
    """
    from kiro_crew.slack.gateway import GatewayOrchestrator

    gw = GatewayOrchestrator.__new__(GatewayOrchestrator)
    gw.ctx_builder = MagicMock()
    gw.ctx_builder.build_message = MagicMock(return_value=("msg", None))
    gw.ctx_builder.hooks = MagicMock()
    gw.slack = MagicMock()
    gw.conv_log = None
    gw.dashboard_state = None
    gw._owner_id = "U000"
    gw.subagent_mgr = None
    gw._cron_injecting = {}
    gw._no_crons = False
    gw.cron_svc = None
    calls: dict = {"get_or_create": 0, "kwargs": []}

    async def fake_get_or_create(_key, **_kwargs):
        calls["get_or_create"] += 1
        calls["kwargs"].append(_kwargs)
        if model_unavailable and calls["get_or_create"] == 1:
            raise RuntimeError(f"model {_kwargs.get('model')!r} is not available")
        return MagicMock(), True, False

    gw.sessions = MagicMock()
    gw.sessions.get_pid = MagicMock(return_value=None)
    gw.sessions.get_or_create = fake_get_or_create
    gw.sessions.release = MagicMock()
    gw.sessions.reset = AsyncMock()
    gw.sessions.cancel_current = AsyncMock()
    gw._interactive_approval = MagicMock(return_value="cb")

    captured_cb = None

    with (
        patch("kiro_crew.slack.gateway.stream_and_collect", AsyncMock(return_value="done")),
        patch("kiro_crew.slack.gateway.CronService") as mock_cron_cls,
    ):

        def capture_cron(on_job=None, **_kw):
            nonlocal captured_cb
            captured_cb = on_job
            svc = MagicMock()
            svc.start = AsyncMock()
            return svc

        mock_cron_cls.create = AsyncMock(side_effect=capture_cron)

        async def _init_and_run():
            await gw._init_cron()
            assert captured_cb is not None
            await captured_cb(job)

        asyncio.run(_init_and_run())

    return calls


class TestGatewayHarnessGate:
    @pytest.fixture
    def refusing(self, resolver):
        resolver.outcome = UnknownHarness("unknown harness 'nope' (registered: kiro)")
        return resolver

    def test_unknown_harness_fails_the_run_and_dispatches_nothing(self, refusing):
        job = _job(harness="nope")
        calls = _run_cron_callback(job)
        assert calls["get_or_create"] == 0
        assert job.last_status == "error"
        assert "nope" in (job.last_error or "")

    def test_a_refused_run_leaves_the_job_registered_and_unpaused(self, refusing):
        job = _job(harness="nope")
        _run_cron_callback(job)
        # Refusal-over-fallback must not cost the schedule: no auto-pause
        # bookkeeping, so a repaired harness heals it with no operator action.
        assert job.enabled is True
        assert job.auto_paused is False
        assert job.consecutive_failures == 0
        assert job.failure_recorded is False

    def test_a_refused_run_retains_a_one_shot_job(self, refusing):
        """The run dispatched nothing, so a delete-after-run job must survive it."""
        job = _job(harness="nope", delete_after_run=True)
        _run_cron_callback(job)
        assert job.run_never_started is True
        # Not the policy-denial marker: no policy decision was made here, and
        # that flag would also park an at-job disabled.
        assert job.fire_time_denied is False

    def test_a_refused_run_does_not_show_a_previous_run_s_output(self, refusing):
        """An error status beside a stale result reads as "this run produced that"."""
        job = _job(harness="nope")
        job.last_result = "yesterday's answer"
        _run_cron_callback(job)
        assert not job.last_result

    def test_an_inherited_harness_dispatches_as_before(self, resolver):
        job = _job()
        calls = _run_cron_callback(job)
        assert calls["get_or_create"] == 1
        assert job.last_status != "error"

    def test_a_resolved_explicit_harness_dispatches(self, resolver):
        resolver.outcome = _binding(HARNESS_KAS)
        job = _job(harness="kas")
        calls = _run_cron_callback(job)
        assert calls["get_or_create"] == 1
        assert job.last_status != "error"

    def test_an_explicit_harness_reaches_session_creation(self, resolver):
        """Passing the gate is not the same as being served by that harness.

        A job stored on KAS whose session is created without the selection is run
        by the gateway's default harness and reported as a SUCCESS — the fire-time
        gate having blessed a harness that never ran the work. The stored value is
        threaded (stripped, as every other reader of the field does), so the
        session binds what the job asked for or refuses.
        """
        resolver.outcome = _binding(HARNESS_KAS)
        job = _job(harness="  kas  ")
        calls = _run_cron_callback(job)
        assert calls["kwargs"][0]["harness"] == HARNESS_KAS

    def test_an_inherited_harness_is_not_pushed_onto_the_explicit_path(self, resolver):
        """A job that opted into nothing keeps the un-probed default path.

        Threading the RESOLVED id instead of the job's own value would turn every
        inherited job into an explicit selection, and an explicit selection is
        availability-checked: an ordinary run on a machine mid-kiro-install would
        start refusing before the spawn, which is where it has always failed.
        """
        job = _job()
        calls = _run_cron_callback(job)
        assert calls["kwargs"][0]["harness"] == ""

    def test_the_model_retry_stays_on_the_job_s_harness(self, resolver):
        """The model is what is being retried; the harness is not.

        The second creation is the one a downgraded run is actually served by, so
        dropping the selection there moves the work to another harness and reports
        it as a model downgrade only.
        """
        resolver.outcome = _binding(HARNESS_KAS)
        job = _job(harness="kas", model="some-model")
        calls = _run_cron_callback(job, model_unavailable=True)
        assert calls["get_or_create"] == 2
        assert [c["harness"] for c in calls["kwargs"]] == [HARNESS_KAS, HARNESS_KAS]
        # The retry is the one that drops the model.
        assert calls["kwargs"][1].get("model") is None


# ── REST surface ──


class TestRestHarnessField:
    def _state(self) -> MagicMock:
        state = MagicMock()
        state.crons.add_job_async = AsyncMock(return_value=_job())
        state.crons.update_job_async = AsyncMock(return_value=_job())
        state.push_refresh = MagicMock()
        return state

    def _request(self, state: MagicMock, body: dict, job_id: str = "") -> MagicMock:
        request = MagicMock()
        request.app = {"state": state}
        request.json = AsyncMock(return_value=body)
        request.match_info = {"job_id": job_id}
        return request

    def test_create_forwards_the_harness_to_the_store(self):
        from kiro_crew.dashboard.handlers.cron import api_crons_create

        state = self._state()
        body = {"name": "j", "message": "m", "every": 3600, "harness": "kas"}
        resp = asyncio.run(api_crons_create(self._request(state, body)))
        assert resp.status == 200
        assert state.crons.add_job_async.await_args.kwargs["harness"] == "kas"

    def test_create_defaults_the_harness_to_inherit(self):
        from kiro_crew.dashboard.handlers.cron import api_crons_create

        state = self._state()
        body = {"name": "j", "message": "m", "every": 3600}
        asyncio.run(api_crons_create(self._request(state, body)))
        assert state.crons.add_job_async.await_args.kwargs["harness"] == ""

    def test_create_rejects_a_non_string_harness(self):
        from kiro_crew.dashboard.handlers.cron import api_crons_create

        state = self._state()
        body = {"name": "j", "message": "m", "every": 3600, "harness": 7}
        resp = asyncio.run(api_crons_create(self._request(state, body)))
        assert resp.status == 400
        assert state.crons.add_job_async.await_count == 0

    def test_patch_clears_the_harness_with_an_empty_value(self):
        from kiro_crew.dashboard.handlers.cron import api_cron_update

        state = self._state()
        resp = asyncio.run(api_cron_update(self._request(state, {"harness": ""}, job_id="j1")))
        assert resp.status == 200
        assert state.crons.update_job_async.await_args.kwargs["harness"] == ""

    def test_patch_rejects_a_non_string_harness(self):
        from kiro_crew.dashboard.handlers.cron import api_cron_update

        state = self._state()
        resp = asyncio.run(api_cron_update(self._request(state, {"harness": []}, job_id="j1")))
        assert resp.status == 400
        assert state.crons.update_job_async.await_count == 0

    def _list_payload(self, job: CronJob) -> dict:
        from kiro_crew.dashboard.handlers.cron import api_crons

        state = MagicMock()
        state.has_slot.return_value = False
        state.crons.list_jobs_async = AsyncMock(return_value=[job])
        state.crons.running_since.return_value = None
        state.crons.is_running.return_value = False
        request = MagicMock()
        request.app = {"state": state}
        resp = asyncio.run(api_crons(request))
        return json.loads(resp.text)["jobs"][0]

    def test_list_serializes_the_harness(self):
        """The editor round-trips the stored value, so the list has to carry it."""
        assert self._list_payload(_job(harness="kas"))["harness"] == "kas"

    def test_list_reports_an_absent_harness_as_null(self):
        assert self._list_payload(_job())["harness"] is None


# ── Boundary schemas ──


class TestBoundarySchemas:
    def test_cron_add_schema_accepts_a_harness_id(self):
        from kiro_crew.validation import CRON_ADD_SCHEMA, validate_tool_args

        cleaned = validate_tool_args(
            {"name": "j", "message": "m", "harness": "kas"}, CRON_ADD_SCHEMA
        )
        assert cleaned["harness"] == "kas"

    def test_cron_add_schema_rejects_a_malformed_harness_id(self):
        from kiro_crew.validation import CRON_ADD_SCHEMA, ValidationError, validate_tool_args

        with pytest.raises(ValidationError):
            validate_tool_args(
                {"name": "j", "message": "m", "harness": "Not A Harness"}, CRON_ADD_SCHEMA
            )

    def test_cron_update_schema_accepts_an_empty_harness_for_clearing(self):
        from kiro_crew.validation import MCP_CRON_SCHEMAS, validate_tool_args

        cleaned = validate_tool_args(
            {"job_id": "abcd1234", "harness": ""}, MCP_CRON_SCHEMAS["cron_update"]
        )
        assert cleaned.get("harness", "") == ""


# ── MCP surface ──


class TestMcpCronHarness:
    """The MCP tools carry the axis too, so an agent can schedule per harness."""

    @pytest.fixture(autouse=True)
    def _named_caller(self, named_cron_caller):
        """``mcp_cron`` refuses a write from a caller it cannot name."""

    def test_cron_add_stores_the_harness(self, monkeypatch, tmp_path):
        from kiro_crew.mcp_cron import _call_tool_inner

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        monkeypatch.delenv("KIROCREW_CHANNEL_ID", raising=False)
        name = f"harness-add-{uuid.uuid4().hex[:8]}"
        result = _call_tool_inner(
            "cron_add",
            {"name": name, "message": "go", "every": 120, "harness": "kas"},
        )
        assert "Added job" in result
        stored = [j for j in CronService(base_dir=tmp_path).list_jobs() if j.name == name]
        assert [j.harness for j in stored] == ["kas"]

    def test_cron_update_clears_the_harness(self, monkeypatch, tmp_path):
        from kiro_crew.mcp_cron import _call_tool_inner

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        monkeypatch.delenv("KIROCREW_CHANNEL_ID", raising=False)
        name = f"harness-update-{uuid.uuid4().hex[:8]}"
        _call_tool_inner(
            "cron_add", {"name": name, "message": "go", "every": 120, "harness": "kas"}
        )
        job = [j for j in CronService(base_dir=tmp_path).list_jobs() if j.name == name][0]
        _call_tool_inner("cron_update", {"job_id": job.id, "harness": ""})
        reread = [j for j in CronService(base_dir=tmp_path).list_jobs() if j.name == name][0]
        assert reread.harness == ""


# ── CLI surface ──


class TestCliHarnessFlag:
    def _parse(self, argv: list[str]):
        import sys as _sys

        with (
            patch.object(_sys, "argv", ["kirocrew", *argv]),
            patch("kiro_crew.cli_commands._cron") as mock_cron,
        ):
            from kiro_crew.cli import main

            main()
            return mock_cron.call_args[0][0]

    def test_add_parses_the_harness_flag(self):
        ns = self._parse(["cron", "add", "j", "m", "--every", "300", "--harness", "kas"])
        assert ns.harness == "kas"

    def test_add_without_the_flag_inherits(self):
        """Empty, not None: create has no "leave unchanged" state to express."""
        ns = self._parse(["cron", "add", "j", "m", "--every", "300"])
        assert ns.harness == ""

    def test_update_without_the_flag_is_none(self):
        """None means "do not touch"; an empty string is a real reset."""
        ns = self._parse(["cron", "update", "abc123", "--name", "renamed"])
        assert ns.harness is None

    def test_add_persists_the_harness(self, monkeypatch, tmp_path):
        import argparse

        from kiro_crew import cli_commands

        monkeypatch.setattr(cli_commands, "config_dir", lambda: tmp_path)
        args = argparse.Namespace(
            cron_action="add",
            name="cli-job",
            message="go",
            every=300,
            cron_expr=None,
            channel=None,
            approval_mode="",
            agent="",
            harness="kas",
            silent=False,
        )
        cli_commands._cron(args)
        assert [j.harness for j in CronService(base_dir=tmp_path).list_jobs()] == ["kas"]

    def test_add_persists_the_harness_on_a_cron_expression_job(self, monkeypatch, tmp_path):
        """The two schedule branches build the job separately, so both are pinned."""
        import argparse

        from kiro_crew import cli_commands

        monkeypatch.setattr(cli_commands, "config_dir", lambda: tmp_path)
        cli_commands._cron(
            argparse.Namespace(
                cron_action="add",
                name="cli-cron-job",
                message="go",
                every=None,
                cron_expr="0 9 * * 1-5",
                channel=None,
                approval_mode="",
                agent="",
                harness="kas",
                silent=False,
            )
        )
        assert [j.harness for j in CronService(base_dir=tmp_path).list_jobs()] == ["kas"]

    def test_update_resets_the_harness_with_an_empty_flag(self, monkeypatch, tmp_path):
        import argparse

        from kiro_crew import cli_commands

        monkeypatch.setattr(cli_commands, "config_dir", lambda: tmp_path)
        job = CronService(base_dir=tmp_path).add_job(
            name="cli-job", message="go", every_secs=300, harness="kas"
        )
        cli_commands._cron(
            argparse.Namespace(
                cron_action="update",
                job_id=job.id,
                name=None,
                message=None,
                every_secs=None,
                cron_expr=None,
                channel=None,
                timeout_secs=None,
                agent=None,
                approval_mode=None,
                harness="",
            )
        )
        assert CronService(base_dir=tmp_path).list_jobs()[0].harness == ""

    def test_writing_an_unregistered_harness_warns_but_saves(self, monkeypatch, tmp_path, capsys):
        """The registry may gain the harness later, so a write is never refused."""
        import argparse

        from kiro_crew import cli_commands

        monkeypatch.setattr(cli_commands, "config_dir", lambda: tmp_path)
        cli_commands._cron(
            argparse.Namespace(
                cron_action="add",
                name="cli-job",
                message="go",
                every=300,
                cron_expr=None,
                channel=None,
                approval_mode="",
                agent="",
                harness="not-installed-yet",
                silent=False,
            )
        )
        assert capsys.readouterr().err.count("not-installed-yet") == 1
        assert [j.harness for j in CronService(base_dir=tmp_path).list_jobs()] == [
            "not-installed-yet"
        ]
