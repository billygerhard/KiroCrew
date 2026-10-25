"""``harnesses.json`` backends: load, register, select, probe and admit."""

from __future__ import annotations

import asyncio
import json
import stat

import pytest

from kiro_crew.acp.harness import _reset_operator_register
from kiro_crew.acp.harness.operator_registry import (
    _reset_operator_diagnostics,
    invalid_operator_harnesses,
    load_and_register_operator_descriptors,
    unselectable_operator_harnesses,
)
from kiro_crew.agent_sdk import backend_install
from kiro_crew.agent_sdk.backends import ACP_BACKENDS_KNOWN, selectable_backends
from kiro_crew.agent_sdk.operator_backend_registry import (
    _reset_registered_backends,
    acp_runtime_backends,
    same_session_family,
    uses_advertised_model_selection,
)


def _seat(seat_id: str, executable: str, label: str) -> dict:
    return {
        "id": seat_id,
        "display_name": label,
        "executable": executable,
        "argv": ["{executable}"],
        "model_source": "acp_advertised",
        "mcp_delivery": "session_array",
        "routing": "session_config",
        "permission_config": {"option": "mode", "value": "default"},
        "session_family": "claude-code",
    }


@pytest.fixture
def wrapper(tmp_path):
    path = tmp_path / "claude-seat-wrapper"
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


@pytest.fixture
def registered(tmp_path, wrapper):
    entries = {
        "seat-a": _seat("seat-a", wrapper, "Seat A"),
        "seat-b": _seat("seat-b", wrapper, "Seat B"),
        "seat-gone": _seat("seat-gone", str(tmp_path / "no-such-wrapper"), "Seat Gone"),
        "no-routing": {
            "id": "no-routing",
            "display_name": "No Routing",
            "executable": wrapper,
            "argv": ["{executable}"],
        },
    }
    harnesses = tmp_path / "harnesses.json"
    harnesses.write_text(json.dumps(entries), encoding="utf-8")
    backend_install.clear_probe_cache()
    ids = load_and_register_operator_descriptors(path=harnesses)
    yield ids
    _reset_registered_backends()
    _reset_operator_register()
    _reset_operator_diagnostics()
    backend_install.clear_probe_cache()


def test_valid_descriptors_become_known_runtime_backends(registered):
    assert {"seat-a", "seat-b", "seat-gone"} <= set(registered)
    for backend in ("seat-a", "seat-b"):
        assert backend in ACP_BACKENDS_KNOWN
        assert backend in acp_runtime_backends()
        assert uses_advertised_model_selection(backend)


def test_routed_descriptors_are_selectable_and_unrouted_are_not(registered):
    assert {"seat-a", "seat-b"} <= selectable_backends()
    if "no-routing" in registered:
        assert "no-routing" not in selectable_backends()
        assert "no-routing" in unselectable_operator_harnesses()
    else:
        assert "no-routing" in invalid_operator_harnesses()


def test_seats_share_a_session_family(registered):
    assert same_session_family("seat-a", "seat-b")
    assert not same_session_family("seat-a", "kiro")


def test_probe_follows_the_descriptor_executable(registered):
    assert backend_install.probe_backend("seat-a").installed == backend_install.INSTALLED
    gone = backend_install.probe_backend("seat-gone")
    assert gone.installed == backend_install.MISSING
    assert gone.missing_components and gone.missing_components[0].endswith("no-such-wrapper")


def test_spawn_backend_resolver_admits_a_seat_and_refuses_a_missing_one(registered):
    from kiro_crew.dashboard.messaging_api.spawn import _requested_backend

    backend, refused = asyncio.run(_requested_backend("seat-a"))
    assert backend == "seat-a" and refused is None
    backend, refused = asyncio.run(_requested_backend("seat-gone"))
    assert backend is None and refused is not None


def test_a_builtin_id_keeps_its_own_probe(registered):
    assert backend_install._operator_probe("") is None
    assert backend_install._operator_probe("not-a-backend") is None


def test_a_session_array_descriptor_gets_the_descriptor_projection(registered):
    from kiro_crew.providers.mirrors.descriptor_array import DescriptorArrayProjection
    from kiro_crew.providers.mirrors.registry import has_mirror, mirror_for

    assert has_mirror("seat-a")
    assert isinstance(mirror_for("seat-a"), DescriptorArrayProjection)


def test_an_agent_file_descriptor_is_handed_no_array(registered):
    from kiro_crew.providers.mirrors.registry import has_mirror, mirror_for

    if "no-routing" not in registered:
        pytest.skip("descriptor without routing was not registered")
    assert not has_mirror("no-routing")
    assert mirror_for("no-routing") is None


def test_a_harness_floor_raises_only_upward():
    from kiro_crew.sandbox_tiers import raise_sandbox_mode

    assert raise_sandbox_mode("off", "") == "off"
    assert raise_sandbox_mode("off", "standard") == "standard"
    assert raise_sandbox_mode("strict", "standard") == "strict"


def test_only_a_descriptor_declares_a_sandbox_floor(tmp_path, wrapper):
    from kiro_crew.acp.harness import harness_for

    entry = _seat("floor-seat", wrapper, "Floor Seat")
    entry["min_sandbox"] = "standard"
    harnesses = tmp_path / "floor.json"
    harnesses.write_text(json.dumps({"floor-seat": entry}), encoding="utf-8")
    try:
        assert load_and_register_operator_descriptors(path=harnesses) == ["floor-seat"]
        assert harness_for("floor-seat").min_sandbox_tier == "standard"
        assert harness_for("").min_sandbox_tier == ""
    finally:
        _reset_registered_backends()
        _reset_operator_register()
        _reset_operator_diagnostics()


def test_a_static_descriptor_offers_its_declared_models(tmp_path, wrapper):
    from kiro_crew.dashboard.handlers.operator_models import static_descriptor_models

    entry = {
        "id": "static-host",
        "display_name": "Static Host",
        "executable": wrapper,
        "argv": ["{executable}"],
        "agent_args": ["--agent={agent}"],
        "model_args": ["--model={model}"],
        "model_source": "static",
        "models": ["model-one", "model-two"],
        "routing": "agent_spec",
    }
    harnesses = tmp_path / "static.json"
    harnesses.write_text(json.dumps({"static-host": entry}), encoding="utf-8")
    try:
        assert load_and_register_operator_descriptors(path=harnesses) == ["static-host"]
        rows = static_descriptor_models("static-host")
        assert [r["model_name"] for r in rows] == ["auto", "model-one", "model-two"]
        assert static_descriptor_models("") is None
    finally:
        _reset_registered_backends()
        _reset_operator_register()
        _reset_operator_diagnostics()


def test_the_picker_takes_the_fullest_live_snapshot():
    from types import SimpleNamespace
    from unittest import mock

    from kiro_crew.dashboard.handlers import agents

    def provider(ids):
        return SimpleNamespace(available_models=lambda: [{"modelId": i} for i in ids])

    providers = [provider(["a", "b", "c"]), provider(["a"])]
    request = {
        "state": SimpleNamespace(sessions=SimpleNamespace(active_providers=lambda: providers))
    }
    caps = SimpleNamespace(resolves_model_from_advertised_list=True, model_id_namespace="ns")
    with mock.patch.object(agents, "capabilities_of", return_value=caps):
        rows = agents._advertised_cc_models(SimpleNamespace(app=request), "ns")
    assert [r["model_name"] for r in rows] == ["a", "b", "c"]


def test_an_antigravity_shell_call_carries_a_verifiable_command():
    from kiro_crew.acp.types import _command_from_tool_params

    assert _command_from_tool_params({"CommandLine": "ls -la", "Cwd": "/tmp"}) == "ls -la"
    assert _command_from_tool_params({"commandLine": "pwd"}) == "pwd"
    assert _command_from_tool_params({"command": "id", "CommandLine": "pwd"}) == "id"


def test_a_steerable_claude_adapter_takes_the_steering_request_under_any_id():
    from types import SimpleNamespace

    from kiro_crew.acp.steering_identity import (
        claude_adapter_reported,
        steering_request_meta,
        takes_steering_request,
    )

    init = {"agentInfo": {"name": "@agentclientprotocol/claude-agent-acp", "version": "0.94.0"}}
    assert claude_adapter_reported(init)
    seat = SimpleNamespace(
        acp_backend="claude-seat1",
        claude_adapter=True,
        steering_advertised=True,
        agent_version="0.94.0",
    )
    assert takes_steering_request(seat)
    assert steering_request_meta(seat) == {"steering": {"idleBehavior": "promptRequired"}}
    old = SimpleNamespace(**{**vars(seat), "agent_version": "0.60.0"})
    assert not takes_steering_request(old)
    quiet = SimpleNamespace(**{**vars(seat), "steering_advertised": False})
    assert not takes_steering_request(quiet)
    codex = SimpleNamespace(acp_backend="codex", claude_adapter=False, steering_advertised=True)
    assert takes_steering_request(codex) and steering_request_meta(codex) is None


def test_both_resume_gates_load_a_harnesses_json_session_natively():
    """A seat keeps no kiro transcript; gating its load on one starts it empty."""
    import inspect

    from kiro_crew.acp import client_start
    from kiro_crew.providers import acp_runtime_start

    for module in (client_start, acp_runtime_start):
        assert "is_registered_backend(" in inspect.getsource(module), module.__name__


def test_a_client_on_a_seat_switches_model_through_its_advertised_select(registered):
    import inspect

    from kiro_crew.acp import client_model_selection
    from kiro_crew.agent_sdk.operator_backend_registry import switches_model_via_config_option

    select = [{"id": "model", "type": "select", "options": []}]
    assert switches_model_via_config_option("seat-a", select)
    # A claude-code seat inherits claude's membership, so it switches by config
    # option before its session has advertised the select; one with no source
    # still waits for the advertisement.
    assert switches_model_via_config_option("seat-a", [])
    assert switches_model_via_config_option("no-routing", select)
    assert not switches_model_via_config_option("no-routing", [])
    assert not switches_model_via_config_option("kiro", select)
    for fn in (client_model_selection.set_model, client_model_selection._apply_startup_model):
        assert "switches_model_via_config_option(" in inspect.getsource(fn), fn.__name__


def test_a_process_that_never_booted_resolves_a_seat_default(tmp_path, wrapper, caplog):
    """A CLI or cron process reads config without the gateway's boot step."""
    import logging
    from unittest import mock

    from kiro_crew.acp.harness import descriptor
    from kiro_crew.agent_sdk.backends import resolve_selected_backend

    harnesses = tmp_path / "harnesses.json"
    harnesses.write_text(
        json.dumps({"seat-a": _seat("seat-a", wrapper, "Seat A")}), encoding="utf-8"
    )
    _reset_registered_backends()
    _reset_operator_register()
    backend_install.clear_probe_cache()
    try:
        with (
            mock.patch.object(descriptor, "operator_harnesses_path", return_value=harnesses),
            caplog.at_level(logging.WARNING, logger="kiro_crew.agent_sdk.backends"),
        ):
            assert resolve_selected_backend("seat-a") == "seat-a"
            assert resolve_selected_backend("seat-a") == "seat-a"
        assert "not selectable" not in caplog.text
    finally:
        _reset_registered_backends()
        _reset_operator_register()
        _reset_operator_diagnostics()
        backend_install.clear_probe_cache()


def test_first_need_reads_harnesses_json_once_per_home(tmp_path):
    from unittest import mock

    from kiro_crew import operator_backends

    operator_backends._reset_operator_backends_loaded()
    with mock.patch(
        "kiro_crew.acp.harness.operator_registry.load_and_register_operator_descriptors",
        return_value=["seat-x"],
    ) as load:
        assert operator_backends.ensure_operator_backends_registered() == ["seat-x"]
        assert operator_backends.ensure_operator_backends_registered() == []
    assert load.call_count == 1
    operator_backends._reset_operator_backends_loaded()


def test_a_models_request_may_name_a_selectable_seat(registered):
    from aiohttp.test_utils import make_mocked_request

    from kiro_crew.dashboard.handlers.operator_models import requested_models_backend

    def ask(query: str):
        return requested_models_backend(make_mocked_request("GET", "/api/models" + query))

    assert ask("?backend=seat-a") == "seat-a"
    assert ask("?backend=not-a-backend") is None
    assert ask("") is None


def test_an_acp_backends_row_carries_a_seats_display_name(registered):
    from kiro_crew.dashboard.handlers.acp_backend_status import _operator_label

    assert _operator_label("seat-a") == "Seat A"
    assert _operator_label("claude") == ""
