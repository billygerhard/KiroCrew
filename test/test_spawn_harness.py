"""Per-spawn harness selection: schema, inheritance, refusals, and reporting.

``spawn_run`` gains a batch-wide ``harness`` beside the existing ``model``, and
the value travels the same hops that one does: schema -> tool body ->
``POST /api/spawn`` -> ``SubagentManager.spawn`` -> the record every surface
reports from. Each hop is asserted here, because each is a place the value can be
dropped silently.

What must NOT happen carries most of the weight: an unknown or unavailable
harness must REFUSE the spawn rather than dispatch it onto a working one, and a
run whose harness was inherited or defaulted must still say which harness served
it — an unattributed run in a mixed-harness fleet is indistinguishable from one
that ran on the default.

Availability is forced through ``harness_registry.resolve_executable`` rather
than by installing binaries: whether kiro-cli (or Node, for KAS) exists is a
property of the machine running the suite, and a refusal test that passes only
where a harness is missing tests the host, not the code.
"""

from __future__ import annotations

import json
import stat
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.acp import harness_registry
from kiro_crew.acp.harness_descriptor import (
    MODEL_SOURCE_STATIC,
    CapabilitySet,
    HarnessDescriptor,
    validate_descriptor,
)
from kiro_crew.acp.harness_registry import HARNESS_CLAUDE, HARNESS_KAS, HARNESS_KIRO
from kiro_crew.acp.harness_selection import HarnessBinding
from kiro_crew.acp.types import ACP_BACKEND_KAS
from kiro_crew.validation import (
    SPAWN_RUN_SCHEMA,
    ValidationError,
    is_harness_id,
    validate_tool_args,
)

# ``SubagentManager.spawn`` refuses -- registering no task -- while the host
# looks short of memory, which is the runner's state, not this test's input.
pytestmark = pytest.mark.usefixtures("healthy_host_memory")


# ── Helpers ──


@pytest.fixture
def every_harness_installed(tmp_path, monkeypatch):
    """Make every registered harness resolve its executable.

    The registry's availability answer is "does this descriptor's executable
    resolve to a runnable file"; substituting that one seam is what lets an
    explicit-selection test assert the code's decision instead of the host's
    installed software.

    A FRESH registry is installed rather than the shared one reloaded, because
    availability also consults recorded spawn failures — and those survive
    ``reload()`` deliberately (a config edit does not repair a machine). A real
    spawn against a signed-out kiro-cli anywhere else in the worker would
    otherwise mark ``kiro`` unavailable here for the record's whole TTL, and every
    explicit-selection test would refuse for a reason the test never set up.
    """
    binary = tmp_path / "harness-stub"
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    def _resolve(descriptor: HarnessDescriptor) -> tuple[str, str]:
        return str(binary), ""

    monkeypatch.setattr(harness_registry, "resolve_executable", _resolve)
    monkeypatch.setattr(harness_registry, "_REGISTRY", harness_registry.HarnessRegistry())
    yield


def _mock_sessions(parent_harness: str | None = None) -> MagicMock:
    """A SessionManager double whose parent session reports *parent_harness*.

    ``None`` means "nothing recorded", which is what every session created before
    harness binding — and every test double that does not opt in — answers.
    """
    sessions = MagicMock()
    sessions.get_pid = MagicMock(return_value=None)
    sessions.get_approval_policy = MagicMock(return_value="auto")
    sessions.get_agent = MagicMock(return_value="")
    sessions.has_session = MagicMock(return_value=True)
    sessions.release = MagicMock()
    sessions.reset = AsyncMock()
    # A MagicMock's default answer is a Mock, not a str: the spawn path must read
    # that as "unrecorded", so the default double is the no-binding case.
    sessions.get_harness = MagicMock(return_value=parent_harness or MagicMock())
    return sessions


def _mgr(parent_harness: str | None = None):
    from kiro_crew.subagent import SubagentManager

    ctx = MagicMock()
    ctx.hooks.auto_approve_subagent_spawn = True
    # A real two-tuple: ``_run_inner`` unpacks what ``build_message`` returns, and
    # the routing tests execute that path.
    ctx.build_message = MagicMock(return_value=("msg", None))
    mgr = SubagentManager(sessions=_mock_sessions(parent_harness), ctx_builder=ctx)
    mgr._run = AsyncMock()  # type: ignore[method-assign]
    return mgr


def _mgr_per_key(bindings: dict[str, str]):
    """A manager whose sessions report a DIFFERENT harness per session key.

    The continuation cases need exactly this: the session DISPATCHING a follow-up
    and the conversation being resumed are two different keys bound to two
    different harnesses, and which one the spawn reads is the whole question.
    """
    from kiro_crew.subagent import SubagentManager

    ctx = MagicMock()
    ctx.hooks.auto_approve_subagent_spawn = True
    ctx.build_message = MagicMock(return_value=("msg", None))
    sessions = _mock_sessions()
    # An unlisted key answers with a Mock, which the spawn path reads as
    # "nothing recorded" — the same default the plain double gives.
    sessions.get_harness = MagicMock(side_effect=lambda key: bindings.get(key, MagicMock()))
    mgr = SubagentManager(sessions=sessions, ctx_builder=ctx)
    mgr._run = AsyncMock()  # type: ignore[method-assign]
    return mgr


async def _run_and_capture(mgr, info, *, resumed: bool = False, shared_fails: bool = False) -> dict:
    """Execute *info* with session creation stubbed; return its kwargs.

    Asserting on the RECORD cannot see this hop, and the hop is where a
    selection stops being a label: ``get_or_create`` resolves an empty harness to
    the configured default on a fresh session, so a run whose selection never
    arrives is served by the default and reported as whatever it recorded.
    """
    from kiro_crew.providers.base import EVENT_COMPLETE, LLMEvent

    captured: dict[str, Any] = {}
    client = MagicMock()

    async def _fake_get_or_create(key, **kwargs):
        captured["_key"] = key
        captured.update(kwargs)
        return client, True, resumed

    async def _stream(_msg):
        yield LLMEvent(kind=EVENT_COMPLETE)

    client.stream = _stream
    mgr._sessions.get_or_create = _fake_get_or_create
    shared = AsyncMock(side_effect=RuntimeError("shared runtime is dead"))
    with (
        patch.object(mgr, "_should_use_session_sharing", return_value=shared_fails),
        patch.object(mgr, "_create_shared_session", shared),
    ):
        await mgr._run_inner(info, info.conversation_key or f"subagent:{info.id}")
    return captured


def _static_binding(models: tuple[str, ...], harness_id: str = "static-tool") -> HarnessBinding:
    """A binding for a harness that enumerates a FIXED model list."""
    descriptor = HarnessDescriptor(
        id=harness_id,
        display_name="Static Tool",
        executable="static-tool",
        argv=("{executable}", "acp"),
        model_source=MODEL_SOURCE_STATIC,
        models=models,
        capabilities=CapabilitySet(reasoning_effort=True),
    )
    return HarnessBinding(descriptor=descriptor, acp_backend=ACP_BACKEND_KAS)


def _run_tool(args: dict[str, Any], responses: dict | None = None) -> tuple[list[dict], str]:
    """Run spawn_run and return (POSTed bodies, returned text)."""
    from kiro_crew import mcp_core

    bodies: list[dict] = []

    def _fake_post(path: str, body: dict) -> dict:
        if path == "/api/spawn":
            bodies.append(body)
        return {"id": "a1", **(responses or {})}

    with (
        patch.object(mcp_core, "_post", side_effect=_fake_post),
        patch.object(mcp_core, "_resolve_session_key", return_value="dashboard:chat-1"),
        patch.object(mcp_core, "sel", MagicMock()),
    ):
        result = mcp_core._call_tool_inner("spawn_run", args)
    return bodies, result


# ── Schema ──


class TestSchema:
    @pytest.mark.parametrize("value", ["kiro", "kas", "codex", "my-acp-tool", "a", "x9"])
    def test_valid_ids_are_accepted(self, value):
        cleaned = validate_tool_args({"task": "x", "harness": value}, SPAWN_RUN_SCHEMA)
        assert cleaned["harness"] == value

    def test_empty_string_means_inherit_and_is_accepted(self):
        cleaned = validate_tool_args({"task": "x", "harness": ""}, SPAWN_RUN_SCHEMA)
        assert cleaned["harness"] == ""

    def test_absent_field_cleans_to_none(self):
        cleaned = validate_tool_args({"task": "x"}, SPAWN_RUN_SCHEMA)
        assert cleaned.get("harness") is None

    @pytest.mark.parametrize(
        "bad", ["Kiro", "my_tool", "my tool", "kiro/../etc", "a" * 33, "kiro.cli", "kiro:1"]
    )
    def test_malformed_ids_are_rejected(self, bad):
        with pytest.raises(ValidationError):
            validate_tool_args({"task": "x", "harness": bad}, SPAWN_RUN_SCHEMA)

    def test_surrounding_whitespace_is_sanitized_rather_than_refused(self):
        """``sanitize_string`` runs before the pattern, so a stray newline from a
        hand-written config or a model's formatting is trimmed, not rejected."""
        cleaned = validate_tool_args({"task": "x", "harness": "kiro\n"}, SPAWN_RUN_SCHEMA)
        assert cleaned["harness"] == "kiro"

    @pytest.mark.parametrize("bad", [1, 2.5, True, [], {}])
    def test_non_string_is_rejected(self, bad):
        with pytest.raises(ValidationError):
            validate_tool_args({"task": "x", "harness": bad}, SPAWN_RUN_SCHEMA)

    @pytest.mark.parametrize(
        "candidate",
        ["kiro", "my-acp-tool", "a", "x9", "9", "-", "Kiro", "my_tool", "my tool", "a" * 33, ""],
    )
    def test_the_pattern_agrees_with_descriptor_validation(self, candidate):
        """The grammar is spelled twice (here and in ``harness_descriptor``) to
        keep the ACP package off every tool-validation import. This is what stops
        the two spellings drifting: an id the schema accepts must be an id the
        descriptor layer would register, and vice versa.

        Asked through ``is_harness_id`` — the public reader every surface outside a
        ``FieldSpec`` shares — so this also pins that the helper answers for the
        same grammar the schema field enforces."""
        descriptor = HarnessDescriptor(id=candidate, executable="x", argv=("{executable}",))
        id_reasons = [r for r in validate_descriptor(descriptor) if "identifier" in r]
        assert is_harness_id(candidate) is (not id_reasons), candidate


# ── The spawn_run tool: forwarding and the drop report ──


class TestToolForwarding:
    def test_set_value_is_sent_in_the_body(self):
        bodies, _ = _run_tool({"task": "x", "harness": "kas"})
        assert bodies[0]["harness"] == "kas"

    def test_unset_value_is_omitted_from_the_body(self):
        bodies, _ = _run_tool({"task": "x"})
        assert "harness" not in bodies[0]

    def test_value_is_batch_wide(self):
        bodies, _ = _run_tool({"tasks": ["t1", "t2", "t3"], "harness": "kas"})
        assert len(bodies) == 3
        assert all(b["harness"] == "kas" for b in bodies)


class TestEffortDropReport:
    """A dropped effort level is REPORTED, never a rejection — on the one
    ``effort_dropped`` channel it shares with the model-capability verdict.

    The stubbed responses carry the REASON the gateway actually sends (see
    ``api_spawn``), not a bare level, so these exercise the tool against a
    realistic payload rather than against a shape nothing produces.
    """

    _KAS_DROP = "harness 'kas' does not support effort configuration"

    def test_report_names_the_harness_and_the_spawn_still_happens(self):
        bodies, result = _run_tool(
            {"task": "x", "harness": "kas", "reasoning_effort": "high"},
            responses={"harness": "kas", "effort_dropped": self._KAS_DROP},
        )
        assert len(bodies) == 1  # dispatched regardless
        assert "does not support effort" in result
        assert "kas" in result
        assert "high" in result

    def test_no_report_when_the_gateway_dropped_nothing(self):
        bodies, result = _run_tool(
            {"task": "x", "harness": "kas", "reasoning_effort": "high"},
            responses={"harness": "kas"},
        )
        assert len(bodies) == 1
        assert "does not support effort" not in result

    def test_report_is_emitted_for_an_inherited_harness_too(self):
        """The caller named no harness, so only the gateway knows which one
        served the run — which is why the report is read from the response."""
        _, result = _run_tool(
            {"task": "x", "reasoning_effort": "high"},
            responses={"harness": "kas", "effort_dropped": self._KAS_DROP},
        )
        assert "harness 'kas' does not support effort" in result

    def test_report_never_shadows_the_error_prefix_on_total_failure(self):
        """SEL and callers test the FIRST line for the 'Error:' prefix, so a
        spawn where nothing started must not lead with the report line."""
        from kiro_crew import mcp_core

        with (
            patch.object(mcp_core, "_post", side_effect=lambda p, b: {"error": "capacity"}),
            patch.object(mcp_core, "_resolve_session_key", return_value="dash:1"),
            patch.object(mcp_core, "sel", MagicMock()),
        ):
            result = mcp_core._call_tool_inner(
                "spawn_run", {"task": "x", "harness": "kas", "reasoning_effort": "high"}
            )
        assert result.startswith("Error:")
        assert "does not support effort" not in result


class TestSpawnListRendering:
    def test_the_roster_names_the_harness_and_resolved_model(self):
        from kiro_crew import mcp_core

        agents = {
            "agents": [
                {
                    "id": "a1",
                    "task": "review the diff",
                    "done": False,
                    "harness": "kiro",
                    "resolved_model": "claude-sonnet-4.5",
                }
            ]
        }
        with (
            patch.object(mcp_core, "_get", return_value=agents),
            patch.object(mcp_core, "sel", MagicMock()),
            patch.object(mcp_core, "list_agents", return_value=[]),
        ):
            out = mcp_core._call_tool_inner("spawn_list", {})
        assert "harness: kiro (claude-sonnet-4.5)" in out

    def test_a_record_without_a_harness_renders_no_clause(self):
        from kiro_crew import mcp_core

        agents = {"agents": [{"id": "a1", "task": "t", "done": False}]}
        with (
            patch.object(mcp_core, "_get", return_value=agents),
            patch.object(mcp_core, "sel", MagicMock()),
            patch.object(mcp_core, "list_agents", return_value=[]),
        ):
            out = mcp_core._call_tool_inner("spawn_list", {})
        assert "harness:" not in out


# ── POST /api/spawn ──


class TestApiSpawnHandler:
    def _request(self, body: dict, info: Any = None) -> tuple[Any, MagicMock]:
        mgr = MagicMock()
        mgr.spawn.return_value = info or SimpleNamespace(
            id="a1", done=False, error="", harness="kiro", effort_dropped=""
        )
        mgr.max_concurrent = 4
        state = SimpleNamespace(subagents=mgr)
        request = MagicMock()
        request.app = {"state": state}

        async def _json() -> dict:
            return body

        request.json = _json
        return request, mgr

    @pytest.mark.asyncio
    async def test_value_reaches_spawn(self):
        from kiro_crew.dashboard.handlers.messaging import api_spawn

        request, mgr = self._request({"task": "x", "harness": "kas"})
        await api_spawn(request)
        assert mgr.spawn.call_args.kwargs["harness"] == "kas"

    @pytest.mark.asyncio
    async def test_absent_value_reaches_spawn_as_empty(self):
        from kiro_crew.dashboard.handlers.messaging import api_spawn

        request, mgr = self._request({"task": "x"})
        await api_spawn(request)
        assert mgr.spawn.call_args.kwargs["harness"] == ""

    @pytest.mark.asyncio
    async def test_malformed_value_is_rejected_with_400(self):
        from kiro_crew.dashboard.handlers.messaging import api_spawn

        request, mgr = self._request({"task": "x", "harness": "Not A Harness"})
        resp = await api_spawn(request)
        assert resp.status == 400
        mgr.spawn.assert_not_called()

    @pytest.mark.asyncio
    async def test_the_response_reports_the_resolved_harness_and_any_drop(self):
        from kiro_crew.dashboard.handlers.messaging import api_spawn

        request, _ = self._request(
            {"task": "x"},
            info=SimpleNamespace(
                id="a1", done=False, error="", harness="kas", effort_dropped="high"
            ),
        )
        resp = await api_spawn(request)
        payload = json.loads(resp.body)
        assert payload["harness"] == "kas"
        # The response carries a REASON, not the bare level: `effort_dropped` is
        # one channel shared with the model-capability verdict, so a consumer
        # renders whichever reason applied without having to know which axis
        # produced it.
        assert "kas" in payload["effort_dropped"]
        assert "effort" in payload["effort_dropped"]


class TestRetryKeepsTheHarness:
    @pytest.mark.asyncio
    async def test_retry_re_spawns_on_the_same_harness(self):
        from kiro_crew.dashboard.handlers.messaging import api_spawn_retry

        old = SimpleNamespace(
            id="a1",
            task="t",
            _raw_task="t",
            parent_session_key="dash:1",
            agent="",
            max_turns=0,
            cwd="",
            model="",
            reasoning_effort="",
            harness="kas",
            approval_mode="",
            silent=False,
            include_memory=True,
            include_lessons=True,
            include_project=True,
            done=True,
            outcome="failed",
        )
        mgr = MagicMock()
        mgr.get.return_value = old
        mgr.spawn.return_value = SimpleNamespace(id="a2", done=False, error="")
        request = MagicMock()
        request.app = {"state": SimpleNamespace(subagents=mgr)}
        request.match_info = {"agent_id": "a1"}
        await api_spawn_retry(request)
        assert mgr.spawn.call_args.kwargs["harness"] == "kas"


# ── SubagentManager.spawn: resolution, refusal, drop ──


class TestResolution:
    @pytest.mark.asyncio
    async def test_an_absent_harness_inherits_the_spawning_session(self, every_harness_installed):
        mgr = _mgr(parent_harness=HARNESS_KAS)
        info = mgr.spawn("do the thing", parent_session_key="dashboard:chat-1")
        assert info is not None and info.error == ""
        assert info.harness == HARNESS_KAS

    @pytest.mark.asyncio
    async def test_an_explicit_harness_beats_the_inherited_one(self, every_harness_installed):
        mgr = _mgr(parent_harness=HARNESS_KIRO)
        info = mgr.spawn("do the thing", parent_session_key="c1", harness=HARNESS_KAS)
        assert info is not None and info.error == ""
        assert info.harness == HARNESS_KAS

    @pytest.mark.asyncio
    async def test_an_unrecorded_parent_binding_falls_back_to_the_default_harness(self):
        mgr = _mgr(parent_harness=None)
        info = mgr.spawn("do the thing", parent_session_key="c1")
        assert info is not None and info.error == ""
        assert info.harness == HARNESS_KIRO

    @pytest.mark.asyncio
    async def test_the_default_path_never_probes_an_executable(self, monkeypatch):
        """A spawn that selects nothing must behave exactly as it does today: no
        availability question is asked, so kiro-cli's absence still surfaces from
        the spawn itself rather than as a new pre-dispatch refusal."""
        calls: list[str] = []

        def _counting(descriptor):
            calls.append(descriptor.id)
            return "", "not installed"

        monkeypatch.setattr(harness_registry, "resolve_executable", _counting)
        harness_registry.registry().reload()
        mgr = _mgr()
        info = mgr.spawn("do the thing")
        assert info is not None and info.error == ""
        assert calls == []

    @pytest.mark.asyncio
    async def test_the_record_names_the_harness_even_with_no_selection_at_all(self):
        """The inherited/defaulted case: an empty ``harness`` on the record would
        be indistinguishable from "nobody knows", so the resolved id is stored
        rather than the caller's blank argument."""
        mgr = _mgr()
        info = mgr.spawn("do the thing")
        assert info is not None
        assert info.harness == HARNESS_KIRO


class TestRefusals:
    def test_an_unknown_harness_refuses_the_spawn_and_names_it(self):
        mgr = _mgr()
        info = mgr.spawn("do the thing", harness="no-such-tool")
        assert info is not None
        assert info.done is True
        assert "no-such-tool" in info.error
        assert info.harness == "no-such-tool"
        mgr._run.assert_not_called()

    def test_an_unavailable_harness_refuses_with_the_recorded_reason(self, monkeypatch):
        # Claude is serviceable now (#7301), so the refusal barrier is exercised
        # on a synthetic build-declared-unserviceable row instead: a persisted
        # selection of a harness this build cannot serve must REFUSE — not
        # silently spawn something else under that harness's label.
        from kiro_crew.acp import harness_registry

        monkeypatch.setitem(
            harness_registry._UNSERVICEABLE, HARNESS_KAS, "build cannot serve this harness"
        )
        mgr = _mgr()
        info = mgr.spawn("do the thing", harness=HARNESS_KAS)
        assert info is not None
        assert info.done is True
        assert HARNESS_KAS in info.error
        mgr._run.assert_not_called()

    def test_a_missing_executable_refuses_with_a_reason_naming_it(self, monkeypatch):
        def _missing(descriptor):
            return "", f"{descriptor.executable!r} was not found on PATH"

        monkeypatch.setattr(harness_registry, "resolve_executable", _missing)
        harness_registry.registry().reload()
        mgr = _mgr()
        info = mgr.spawn("do the thing", harness=HARNESS_KAS)
        assert info is not None and info.done is True
        assert HARNESS_KAS in info.error and "not found on PATH" in info.error
        mgr._run.assert_not_called()

    def test_a_refusal_does_not_fall_back_to_the_parent_harness(self):
        mgr = _mgr(parent_harness=HARNESS_KIRO)
        info = mgr.spawn("do the thing", parent_session_key="c1", harness="no-such-tool")
        assert info is not None and info.done is True
        assert info.harness != HARNESS_KIRO
        mgr._run.assert_not_called()

    def test_a_model_the_harness_does_not_serve_refuses_with_the_valid_list(self):
        mgr = _mgr()
        binding = _static_binding(("tool-small", "tool-large"))
        with patch("kiro_crew.subagent.resolve_session_harness", return_value=binding):
            info = mgr.spawn("do the thing", harness="static-tool", model="claude-opus-4.8")
        assert info is not None and info.done is True
        assert "claude-opus-4.8" in info.error
        assert "tool-small" in info.error and "tool-large" in info.error
        mgr._run.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_model_the_harness_serves_is_dispatched(self):
        mgr = _mgr()
        binding = _static_binding(("tool-small", "tool-large"))
        with patch("kiro_crew.subagent.resolve_session_harness", return_value=binding):
            info = mgr.spawn("do the thing", harness="static-tool", model="tool-large")
        assert info is not None and info.error == ""
        assert info.model == "tool-large"

    @pytest.mark.asyncio
    async def test_a_harness_that_cannot_enumerate_its_models_never_refuses_one(self):
        """An ``acp_advertised`` harness has no catalog before a session exists,
        and refusing on an unknowable list would reject every model pin on the
        default harness."""
        mgr = _mgr()
        info = mgr.spawn("do the thing", model="some-future-model")
        assert info is not None and info.error == ""
        assert info.harness == HARNESS_KIRO

    @pytest.mark.asyncio
    async def test_the_auto_sentinel_is_never_refused(self):
        mgr = _mgr()
        binding = _static_binding(("tool-small",))
        with patch("kiro_crew.subagent.resolve_session_harness", return_value=binding):
            info = mgr.spawn("do the thing", harness="static-tool", model="auto")
        assert info is not None and info.error == ""


class TestEffortDrop:
    @pytest.mark.asyncio
    async def test_effort_is_dropped_and_recorded_when_the_harness_lacks_it(
        self, every_harness_installed
    ):
        mgr = _mgr()
        info = mgr.spawn("do the thing", harness=HARNESS_KAS, reasoning_effort="high")
        assert info is not None and info.error == ""
        assert info.reasoning_effort == ""  # dropped, so the dedicated path is not forced
        assert info.effort_dropped == "high"

    @pytest.mark.asyncio
    async def test_effort_survives_on_a_harness_that_declares_the_capability(
        self, every_harness_installed
    ):
        mgr = _mgr()
        info = mgr.spawn("do the thing", harness=HARNESS_KIRO, reasoning_effort="high")
        assert info is not None and info.error == ""
        assert info.reasoning_effort == "high"
        assert info.effort_dropped == ""

    @pytest.mark.asyncio
    async def test_an_inherited_harness_drops_effort_too(self, every_harness_installed):
        """Nothing was selected here, so only the resolved harness can answer —
        and a level silently kept would cost a dedicated process for nothing."""
        mgr = _mgr(parent_harness=HARNESS_KAS)
        info = mgr.spawn("do the thing", parent_session_key="c1", reasoning_effort="max")
        assert info is not None and info.error == ""
        assert info.reasoning_effort == ""
        assert info.effort_dropped == "max"


class TestQueueRoundTrip:
    def test_the_queue_entry_carries_the_RESOLVED_harness(self, every_harness_installed):
        """A queued member's parent may be gone by the time it drains, so the
        inherited id has to be written down rather than re-derived."""
        mgr = _mgr(parent_harness=HARNESS_KAS)
        mgr._should_stagger_queue = MagicMock(return_value=(True, False))  # type: ignore[method-assign]
        info = mgr.spawn("read these files", parent_session_key="c1")
        assert info is not None and info.queued is True
        assert mgr._queue[0]["harness"] == HARNESS_KAS
        assert info.harness == HARNESS_KAS

    def test_a_drained_spawn_receives_the_value(self, every_harness_installed):
        mgr = _mgr(parent_harness=HARNESS_KAS)
        mgr._should_stagger_queue = MagicMock(return_value=(True, False))  # type: ignore[method-assign]
        mgr.spawn("validate this finding", parent_session_key="c1")
        captured: dict[str, object] = {}

        def _capture(**kwargs: object) -> None:
            captured.update(kwargs)

        mgr.spawn = _capture  # type: ignore[method-assign]
        mgr._max_concurrent = 4
        mgr._running_count = 0
        mgr._spawn_stagger_secs = 0.0
        mgr._drain_queue()
        assert captured["harness"] == HARNESS_KAS
        assert captured["_from_queue"] is True

    @pytest.mark.asyncio
    async def test_a_drained_spawn_is_not_re_validated(self, every_harness_installed):
        """The harness was validated at submission. Re-validating on drain would
        refuse work for an availability blip during a wait the caller never saw,
        so the drained call resolves nothing and keeps the recorded id."""
        mgr = _mgr()
        with patch("kiro_crew.subagent.resolve_session_harness") as resolver:
            info = mgr.spawn(
                "drained work",
                harness="no-such-tool",
                reasoning_effort="high",
                _from_queue=True,
                _effort_dropped="max",
            )
        resolver.assert_not_called()
        assert info is not None and info.error == ""
        assert info.harness == "no-such-tool"
        assert info.reasoning_effort == "high"
        assert info.effort_dropped == "max"


class TestSessionCreationRouting:
    """The selection has to reach SESSION CREATION, not just the record.

    Everything else in this file asserts what the run is reported as. This class
    asserts what it actually runs on, because the two come apart in exactly one
    place: ``get_or_create`` resolves an empty harness to the configured DEFAULT on
    a fresh session, so a spawn whose selection is dropped here is validated,
    recorded and reported as one harness while another serves it — the
    substitution per-spawn selection exists to make impossible.
    """

    @pytest.mark.asyncio
    async def test_an_explicit_selection_reaches_session_creation(self, every_harness_installed):
        mgr = _mgr()
        info = mgr.spawn("do the thing", harness=HARNESS_KAS)
        assert info is not None and info.error == ""
        captured = await _run_and_capture(mgr, info)
        assert captured["harness"] == HARNESS_KAS

    @pytest.mark.asyncio
    async def test_an_inherited_binding_reaches_session_creation(self, every_harness_installed):
        """The parent's harness is not the default, so an empty value is not it.

        A subagent session key is always fresh, so ``get_or_create`` has no
        recording to read: passing nothing would bind the configured default while
        the record, the completion meta and ``spawn_list`` all said KAS.
        """
        mgr = _mgr(parent_harness=HARNESS_KAS)
        info = mgr.spawn("do the thing", parent_session_key="dashboard:chat-1")
        assert info is not None and info.error == ""
        captured = await _run_and_capture(mgr, info)
        assert captured["harness"] == HARNESS_KAS

    @pytest.mark.asyncio
    async def test_a_run_that_selected_nothing_threads_nothing(self):
        """A defaulted run keeps the un-probed default path.

        Threading the RESOLVED id would turn every such spawn into an explicit
        selection, and an explicit selection is availability-checked — an ordinary
        fan-out on a machine mid-kiro-install would start refusing before the
        spawn, which is where it has always failed.
        """
        mgr = _mgr(parent_harness=None)
        info = mgr.spawn("do the thing")
        assert info is not None and info.harness == HARNESS_KIRO
        captured = await _run_and_capture(mgr, info)
        assert captured["harness"] == ""

    @pytest.mark.asyncio
    async def test_the_threaded_selection_is_marked_prebound(self, every_harness_installed):
        """The gate above already made this choice a moment ago.

        Without the flag the second resolution re-asks the recorded-spawn-failure
        question and can refuse work the pre-dispatch gate just admitted — for the
        whole failure window, with the refusal preventing the spawn that clears it.
        """
        mgr = _mgr(parent_harness=HARNESS_KAS)
        info = mgr.spawn("do the thing", parent_session_key="c1")
        assert info is not None and info.error == ""
        captured = await _run_and_capture(mgr, info)
        assert captured["harness_prebound"] is True

    @pytest.mark.asyncio
    async def test_the_shared_runtime_fallback_threads_it_too(self, every_harness_installed):
        """A dead shared runtime falls back to a dedicated process HERE.

        That fallback is then the run's real session creation, so a selection
        dropped on this arm is dropped for every subagent whose parent runtime
        died — silently, since the fallback is logged as a warning and the run
        succeeds.
        """
        mgr = _mgr(parent_harness=HARNESS_KAS)
        info = mgr.spawn("do the thing", parent_session_key="c1", harness=HARNESS_KAS)
        assert info is not None and info.error == ""
        captured = await _run_and_capture(mgr, info, shared_fails=True)
        assert captured["harness"] == HARNESS_KAS

    @pytest.mark.asyncio
    async def test_a_drained_member_still_threads_its_selection(self, every_harness_installed):
        """The drain resolves nothing, so the selection has to be in the entry.

        A member can wait long enough for its parent to be gone, and the drained
        call deliberately re-derives nothing — including the distinction between an
        inherited binding and no selection at all.
        """
        mgr = _mgr(parent_harness=HARNESS_KAS)
        mgr._should_stagger_queue = MagicMock(return_value=(True, False))  # type: ignore[method-assign]
        queued = mgr.spawn("read these files", parent_session_key="c1")
        assert queued is not None and queued.queued is True

        mgr._should_stagger_queue = MagicMock(return_value=(False, True))  # type: ignore[method-assign]
        mgr._spawn_stagger_secs = 0.0
        mgr._running_count = 0
        mgr._drain_queue()
        drained = mgr.get(queued.id)
        assert drained is not None and drained.queued is False
        captured = await _run_and_capture(mgr, drained)
        assert captured["harness"] == HARNESS_KAS


class TestInheritedAvailability:
    """What the inherited path asks, spelled out because the docs promise it.

    The inherited binding IS availability-checked (a harness that cannot run is
    refused pre-dispatch, with a better message than the spawn would give), but a
    recorded spawn failure does not gate it — the session it was inherited from is
    alive on that harness, so one other attempt's failure says nothing about it.
    """

    @pytest.mark.asyncio
    async def test_an_inherited_harness_that_cannot_run_refuses_the_spawn(self, monkeypatch):
        def _missing(descriptor):
            return "", f"{descriptor.executable!r} was not found on PATH"

        monkeypatch.setattr(harness_registry, "resolve_executable", _missing)
        harness_registry.registry().reload()
        mgr = _mgr(parent_harness=HARNESS_KAS)
        info = mgr.spawn("do the thing", parent_session_key="c1")
        assert info is not None and info.done is True
        assert HARNESS_KAS in info.error and "not found on PATH" in info.error
        mgr._run.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_recorded_spawn_failure_does_not_gate_an_inherited_harness(
        self, every_harness_installed
    ):
        harness_registry.registry().note_probe_failure(
            HARNESS_KAS, "kas exited during ACP initialize"
        )
        mgr = _mgr(parent_harness=HARNESS_KAS)
        info = mgr.spawn("do the thing", parent_session_key="c1")
        assert info is not None and info.error == ""
        assert info.harness == HARNESS_KAS

    @pytest.mark.asyncio
    async def test_an_explicit_selection_is_gated_by_a_recorded_spawn_failure(
        self, every_harness_installed
    ):
        """The other half of the same rule: a fresh PICK does honour the record."""
        harness_registry.registry().note_probe_failure(
            HARNESS_KAS, "kas exited during ACP initialize"
        )
        mgr = _mgr()
        info = mgr.spawn("do the thing", harness=HARNESS_KAS)
        assert info is not None and info.done is True
        assert HARNESS_KAS in info.error and "ACP initialize" in info.error


class TestContinuation:
    """A follow-up is served by the harness holding the transcript.

    ``spawn_continue`` resumes the conversation's OWN session, so the harness
    question is answered by that conversation's binding — not by whichever session
    dispatched the follow-up. Reading the dispatcher's refuses continuations for a
    harness the resumed run is not served by, and stamps the run with a harness
    that did not run it.
    """

    _CONV_KEY = "subagent:conv1"

    @pytest.mark.asyncio
    async def test_the_conversation_s_binding_wins_over_the_dispatcher_s(
        self, every_harness_installed
    ):
        mgr = _mgr_per_key({"dashboard:chat-1": HARNESS_KIRO, self._CONV_KEY: HARNESS_KAS})
        info = mgr.spawn(
            "and now the follow-up",
            parent_session_key="dashboard:chat-1",
            keep=True,
            conversation_key=self._CONV_KEY,
        )
        assert info is not None and info.error == ""
        assert info.harness == HARNESS_KAS

    @pytest.mark.asyncio
    async def test_a_dispatcher_on_an_unrunnable_harness_does_not_refuse_the_follow_up(
        self, every_harness_installed
    ):
        """The dormant Claude seam cannot serve a session in this build.

        A parent bound there refuses its own spawns, correctly — but a
        continuation it dispatches is served by the conversation's harness, so
        refusing it costs a resumable conversation for a harness it never touches.
        """
        mgr = _mgr_per_key({"dashboard:chat-1": HARNESS_CLAUDE, self._CONV_KEY: HARNESS_KAS})
        info = mgr.spawn(
            "and now the follow-up",
            parent_session_key="dashboard:chat-1",
            keep=True,
            conversation_key=self._CONV_KEY,
        )
        assert info is not None and info.error == ""
        assert info.harness == HARNESS_KAS

    @pytest.mark.asyncio
    async def test_a_continuation_lets_its_session_resume_its_own_binding(
        self, every_harness_installed
    ):
        """Threaded as ``""``, so ``get_or_create`` reads the recording itself.

        Threading the id instead would hand a resume an explicit selection: it
        agrees today, and the moment it does not, the conversation is refused
        rather than resumed on the harness that holds it.
        """
        mgr = _mgr_per_key({"dashboard:chat-1": HARNESS_KIRO, self._CONV_KEY: HARNESS_KAS})
        info = mgr.spawn(
            "and now the follow-up",
            parent_session_key="dashboard:chat-1",
            keep=True,
            conversation_key=self._CONV_KEY,
        )
        assert info is not None and info.error == ""
        captured = await _run_and_capture(mgr, info, resumed=True)
        assert captured["_key"] == self._CONV_KEY
        assert captured["harness"] == ""

    @pytest.mark.asyncio
    async def test_the_seeded_map_entry_carries_the_run_s_own_harness(self):
        """A restart loses the map entry while ``state.json`` survives.

        Seeding the sid alone records "no binding", which reads as the CURRENT
        default — so a KAS conversation would be resumed on kiro-cli, loading
        nothing and starting a fresh conversation under an id the map still trusts.
        """
        mgr = _mgr()
        mgr._sessions.resumable_sid = MagicMock(side_effect=["", "sid-1"])
        mgr._promote_conversation = MagicMock()  # type: ignore[method-assign]
        state = {"session_id": "sid-1", "provider": "acp", "cwd": "", "harness": HARNESS_KAS}
        with patch("kiro_crew.subagent.read_state", return_value=state):
            mgr.continue_conversation("conv1", "follow up")
        assert mgr._sessions.seed_conversation.call_args.kwargs["harness"] == HARNESS_KAS


class TestSessionSharing:
    """The parent's runtime process IS one harness, so a subagent bound to
    another cannot borrow it."""

    def _info(self, harness: str):
        from kiro_crew.subagent import SubagentInfo

        return SubagentInfo(id="s1", task="t", parent_session_key="c1", harness=harness)

    def test_a_cross_harness_spawn_takes_the_dedicated_path(self):
        mgr = _mgr(parent_harness=HARNESS_KIRO)
        mgr._sessions.is_session_sharing_eligible = MagicMock(return_value=True)
        assert mgr._should_use_session_sharing(self._info(HARNESS_KAS)) is False

    def test_the_same_harness_still_shares(self):
        mgr = _mgr(parent_harness=HARNESS_KIRO)
        mgr._sessions.is_session_sharing_eligible = MagicMock(return_value=True)
        assert mgr._should_use_session_sharing(self._info(HARNESS_KIRO)) is True

    def test_an_unrecorded_parent_harness_is_not_treated_as_a_mismatch(self):
        """Fail-open on purpose: reading "" as a mismatch would push every spawn
        onto a ~3-5s, ~400MB dedicated process the moment the binding stopped
        being readable."""
        mgr = _mgr(parent_harness=None)
        mgr._sessions.is_session_sharing_eligible = MagicMock(return_value=True)
        assert mgr._should_use_session_sharing(self._info(HARNESS_KIRO)) is True


# ── Reporting surfaces ──


class TestCompletionMeta:
    def test_the_meta_carries_the_harness(self):
        from kiro_crew.subagent_completion_meta import OUTCOME_OK, single_completion_meta

        meta = single_completion_meta(agent_id="a1", outcome=OUTCOME_OK, harness="kas")
        assert meta["harness"] == "kas"

    def test_an_unreported_harness_is_empty_not_the_default(self):
        from kiro_crew.subagent_completion_meta import OUTCOME_OK, single_completion_meta

        meta = single_completion_meta(agent_id="a1", outcome=OUTCOME_OK)
        assert meta["harness"] == ""


class TestSpawnListRecords:
    @pytest.mark.asyncio
    async def test_the_listing_records_harness_and_resolved_model(self):
        from kiro_crew.dashboard.handlers.messaging import api_spawn_list

        info = SimpleNamespace(
            id="a1",
            task="t",
            done=False,
            parent_session_key="dash:1",
            agent="",
            started=1.0,
            harness="kas",
            resolved_model="kas-model-1",
            turns=0,
            last_tool="",
            include_memory=True,
            include_lessons=True,
            include_project=True,
        )
        mgr = MagicMock()
        mgr.all_agents = [info]
        request = MagicMock()
        request.app = {"state": SimpleNamespace(subagents=mgr)}
        resp = await api_spawn_list(request)
        row = json.loads(resp.body)["agents"][0]
        assert row["harness"] == "kas"
        assert row["resolved_model"] == "kas-model-1"
