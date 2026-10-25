"""The sandbox floor answers a builtin's clone as it answers the builtin.

A ``harnesses.json`` Claude seat declares ``capabilities_from: claude``. The
``claude`` builtin starts at ``agent.sandbox: off`` with no refusal, so the seat must
too; a descriptor with no builtin to inherit from, or one inheriting from an enforced
builtin, keeps the refusal.
"""

from __future__ import annotations

import json
import stat

import pytest

from kiro_crew.acp.harness import _reset_operator_register
from kiro_crew.acp.harness.operator_registry import (
    _reset_operator_diagnostics,
    load_and_register_operator_descriptors,
)
from kiro_crew.agent_sdk import backend_install
from kiro_crew.agent_sdk import tool_gate as gate
from kiro_crew.agent_sdk.backends import ACP_BACKEND_CLAUDE, ACP_BACKEND_CODEX
from kiro_crew.agent_sdk.operator_backend_registry import _reset_registered_backends


def _entry(backend_id: str, executable: str, **over: object) -> dict:
    entry = {
        "id": backend_id,
        "display_name": backend_id.replace("-", " ").title(),
        "executable": executable,
        "argv": ["{executable}"],
        "model_source": "acp_advertised",
        "mcp_delivery": "session_array",
        "routing": "session_config",
        "permission_config": {"option": "mode", "value": "default"},
    }
    entry.update(over)
    return entry


@pytest.fixture
def load(tmp_path):
    launcher = tmp_path / "launcher"
    launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR)

    def _load(entries: list[dict]) -> list[str]:
        harnesses = tmp_path / "harnesses.json"
        body = {
            e["id"]: _entry(e["id"], str(launcher), **{k: v for k, v in e.items() if k != "id"})
            for e in entries
        }
        harnesses.write_text(json.dumps(body), encoding="utf-8")
        backend_install.clear_probe_cache()
        return load_and_register_operator_descriptors(path=harnesses)

    yield _load
    _reset_registered_backends()
    _reset_operator_register()
    _reset_operator_diagnostics()
    backend_install.clear_probe_cache()


@pytest.fixture
def sandbox_off_unmasked(monkeypatch: pytest.MonkeyPatch) -> None:
    """A host where ``off`` really drops the mask: no governance floor, not nested."""
    from kiro_crew import sandbox

    monkeypatch.setattr(sandbox, "_governance_sandbox_floor", lambda: None)
    monkeypatch.setattr(sandbox, "_inside_kirocrew_sandbox", lambda: False)
    assert sandbox.credential_mask_applies("off") is False


def test_claude_seat_starts_at_sandbox_off_like_claude(load, sandbox_off_unmasked) -> None:
    load([{"id": "seat-one", "capabilities_from": "claude", "session_family": "claude-code"}])
    # The seat is still an enforced harness: its routing verdict keeps gating it.
    assert gate.is_enforced("seat-one") is True
    # Same answer as the builtin it clones.
    gate.enforce_sandbox_floor(ACP_BACKEND_CLAUDE, "off")
    gate.enforce_sandbox_floor("seat-one", "off")


def test_descriptor_without_a_builtin_source_is_still_refused(load, sandbox_off_unmasked) -> None:
    load([{"id": "own-caps", "capabilities": ["crew_tools"], "session_family": "none"}])
    assert gate.is_enforced("own-caps") is True
    with pytest.raises(gate.ToolGateUnroutable):
        gate.enforce_sandbox_floor("own-caps", "off")


def test_clone_of_an_enforced_builtin_is_still_refused(load, sandbox_off_unmasked) -> None:
    load([{"id": "codex-clone", "capabilities_from": ACP_BACKEND_CODEX, "session_family": "none"}])
    # codex itself is refused at ``off``, so its clone inherits the refusal.
    with pytest.raises(gate.ToolGateUnroutable):
        gate.enforce_sandbox_floor(ACP_BACKEND_CODEX, "off")
    with pytest.raises(gate.ToolGateUnroutable):
        gate.enforce_sandbox_floor("codex-clone", "off")
