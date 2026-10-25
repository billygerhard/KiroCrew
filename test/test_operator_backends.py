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
from kiro_crew.agent_sdk.backends_registered import (
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
