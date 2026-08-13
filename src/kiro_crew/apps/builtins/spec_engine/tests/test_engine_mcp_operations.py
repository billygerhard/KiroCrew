"""The operational tools drive the real engine, and a stock agent completes the flow.

These tests use no fakes: they materialise a real spec directory (this
repository's own format-clean documents), point the adapter at a temporary
state store, and drive the workflow through the same JSON-RPC surface a Host_
Agent would. The end-to-end case is the answer to "can an agent holding only
this server author, validate, approve, advance, and list tasks" — it does
exactly that and nothing else.

The malformed and refusing cases are here too: a spec that fails validation
comes back with violations rather than a pass, and an approval the engine would
refuse comes back refused rather than silently recorded.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.apps.builtins.spec_engine.engine.documents import DocumentKind
from kiro_crew.apps.builtins.spec_engine.engine_mcp.operations import EngineOperations
from kiro_crew.apps.builtins.spec_engine.engine_mcp.server import handle

#: This repository's own spec: its documents are format-clean, so they stand in
#: for a realistically authored spec rather than a fixture minimum.
_REPO_SPEC_DIR = (
    Path(__file__).resolve().parents[6] / ".kiro" / "specs" / "agent-agnostic-spec-engine"
)
_SPEC_NAME = "agent-agnostic-spec-engine"


def _live_text(kind: DocumentKind) -> str:
    path = _REPO_SPEC_DIR / kind.filename
    if not path.is_file():
        pytest.skip(f"{path} is not present in this checkout")
    return path.read_text(encoding="utf-8")


def _materialise_repo_spec(project: Path) -> None:
    spec_dir = project / ".kiro" / "specs" / _SPEC_NAME
    spec_dir.mkdir(parents=True, exist_ok=True)
    for kind in DocumentKind:
        (spec_dir / kind.filename).write_text(_live_text(kind), encoding="utf-8")
    (spec_dir / ".config.kiro").write_text(
        json.dumps({"specId": _SPEC_NAME, "specType": "feature"}), encoding="utf-8"
    )


def _ops(tmp_path: Path) -> EngineOperations:
    return EngineOperations(state_root=tmp_path / "state", audit_root=tmp_path / "audit")


def _call(name: str, arguments: dict[str, Any], ops: EngineOperations) -> dict[str, Any]:
    reply = handle(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": name, "arguments": arguments}},
        ops=ops,
    )
    assert reply is not None
    return reply


def _payload(reply: dict[str, Any]) -> Any:
    return json.loads(reply["result"]["content"][0]["text"])


def test_validate_spec_passes_a_clean_spec(tmp_path: Path) -> None:
    project = tmp_path / "project"
    _materialise_repo_spec(project)
    ops = _ops(tmp_path)
    report = _payload(_call("validate_spec", {"project": str(project), "spec": _SPEC_NAME}, ops))
    assert report["ok"] is True


def test_validate_spec_reports_violations_on_a_malformed_spec(tmp_path: Path) -> None:
    # A document missing its required sections must come back with violations,
    # not a pass — the failing case differs from the happy path in the way that
    # matters.
    project = tmp_path / "project"
    spec_dir = project / ".kiro" / "specs" / "broken"
    spec_dir.mkdir(parents=True, exist_ok=True)
    (spec_dir / "requirements.md").write_text("# Requirements Document\n", encoding="utf-8")
    (spec_dir / ".config.kiro").write_text(
        json.dumps({"specId": "broken", "specType": "feature"}), encoding="utf-8"
    )
    ops = _ops(tmp_path)
    report = _payload(_call("validate_spec", {"project": str(project), "spec": "broken"}, ops))
    assert report["ok"] is False
    assert report["violations"]
    for violation in report["violations"]:
        assert violation["file"] and violation["rule"] and violation["message"]


def test_stock_agent_completes_the_authoring_flow(tmp_path: Path) -> None:
    # The whole claim of the server: an agent holding only these tools authors,
    # validates, approves, advances, and lists tasks. The documents are the real
    # ones; the state is a real store; nothing is faked.
    project = tmp_path / "project"
    _materialise_repo_spec(project)
    ops = _ops(tmp_path)
    key = {"project": str(project), "spec": _SPEC_NAME}

    # 1. Learn the workflow.
    guidance = handle(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "get_authoring_prompt", "arguments": {"spec_type": "feature"}}}
    )
    assert guidance is not None and "record_approval" in guidance["result"]["content"][0]["text"]

    # 2. Validate the authored documents.
    assert _payload(_call("validate_spec", key, ops))["ok"] is True

    # 3. The spec starts at its first gate.
    phase = _payload(_call("get_phase", key, ops))
    assert phase["phase"] == "requirements"

    # 4. Approve the requirements gate.
    approval = _payload(
        _call("record_approval", {**key, "gate": "requirements", "actor": "reviewer@example"}, ops)
    )
    assert approval["ok"] is True
    assert approval["approver"] == "reviewer@example"

    # 5. Advance past it. All three documents are present, so the gate being
    # left is named explicitly rather than defaulting to the last written.
    advanced = _payload(
        _call("advance_phase", {**key, "actor": "reviewer@example", "gate": "requirements"}, ops)
    )
    assert advanced["ok"] is True
    assert advanced["to_phase"] == "design"

    # 6. The phase now reflects the move.
    assert _payload(_call("get_phase", key, ops))["phase"] == "design"

    # 7. Tasks are listable, with leaf tasks resolved.
    tasks = _payload(_call("list_tasks", key, ops))
    assert tasks["present"] is True
    assert tasks["leaves"]


def test_advance_refuses_an_unapproved_gate(tmp_path: Path) -> None:
    project = tmp_path / "project"
    _materialise_repo_spec(project)
    ops = _ops(tmp_path)
    # Advancing with no approval recorded is refused, with reasons — not silently
    # allowed.
    advanced = _payload(
        _call("advance_phase", {"project": str(project), "spec": _SPEC_NAME, "actor": "x"}, ops)
    )
    assert advanced["ok"] is False
    assert advanced["reasons"]


def test_mcp_and_library_agree_on_validation(tmp_path: Path) -> None:
    # A spot check of the state-equivalence property: the report the tool returns
    # matches the library call it wraps.
    from kiro_crew.apps.builtins.spec_engine.engine.cross_document import validate_spec

    project = tmp_path / "project"
    _materialise_repo_spec(project)
    ops = _ops(tmp_path)
    via_tool = _payload(_call("validate_spec", {"project": str(project), "spec": _SPEC_NAME}, ops))
    via_lib = validate_spec(project / ".kiro" / "specs" / _SPEC_NAME)
    assert via_tool["ok"] == via_lib.ok
    assert len(via_tool["violations"]) == len(via_lib.violations)
