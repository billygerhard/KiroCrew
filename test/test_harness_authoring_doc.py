"""The harness-authoring doc's copy-paste example must validate.

Hands-on testing (2026-09-02) caught the shipped example carrying
``"mcp_delivery": "wire"`` while the validator's vocabulary is
``file_fed`` / ``wire_fed`` — an operator pasting the doc got an invalid
descriptor. The doc example is the operator's copy-paste source, so it is
pinned against the REAL validator here: if either side changes, this fails.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from kiro_crew.acp.harness_descriptor import descriptor_from_mapping

_DOC = (
    Path(__file__).resolve().parent.parent
    / "docs"
    / "system-specs"
    / "modules"
    / "harness-authoring.md"
)


def _doc_example_entries() -> dict[str, dict]:
    """Every ``agent.harnesses`` map in the doc's fenced json/jsonc blocks."""
    text = _DOC.read_text(encoding="utf-8")
    entries: dict[str, dict] = {}
    for block in re.findall(r"```jsonc?\n(.*?)```", text, flags=re.S):
        # jsonc: strip // comments (none carry URLs in this doc).
        cleaned = re.sub(r"^\s*//.*$", "", block, flags=re.M)
        try:
            payload = json.loads(cleaned)
        except ValueError:
            continue  # prose-fragment blocks are fine; only full objects are pinned
        harnesses = (payload.get("agent") or {}).get("harnesses")
        if isinstance(harnesses, dict):
            entries.update(harnesses)
    return entries


def test_the_docs_worked_examples_actually_validate() -> None:
    entries = _doc_example_entries()
    assert (
        entries
    ), f"no agent.harnesses example found in {_DOC.name} — doc restructure? repin this test"
    for harness_id, body in entries.items():
        descriptor, reasons = descriptor_from_mapping(body, harness_id=harness_id)
        assert (
            descriptor is not None and not reasons
        ), f"doc example {harness_id!r} fails validation: {reasons}"


class TestManagedMcpEnvPortPin:
    """The gateway pins its own port into managed MCP children's env.

    Regression (hands-on, 2026-09-02): a pod gateway binds a non-default port
    with no port in config; its stdio MCP children resolved their callback
    port through the client chain, whose run-marker step fails CLOSED on a
    sandboxed host (cannot prove the port's owner), falling through to the
    default port — every spawn_run died with Connection refused, on every
    harness. The gateway knows the port it bound; the spec env is the channel.
    """

    def test_bound_port_is_pinned(self, monkeypatch):
        from kiro_crew import agent

        monkeypatch.setenv("KIROCREW_BOUND_PORT", "7929")
        monkeypatch.setenv("KIROCREW_PORT", "1111")  # bound truth outranks launch instruction
        assert agent._managed_mcp_env().get("KIROCREW_PORT") == "7929"

    def test_launch_port_pins_before_bind(self, monkeypatch):
        from kiro_crew import agent

        monkeypatch.delenv("KIROCREW_BOUND_PORT", raising=False)
        monkeypatch.setenv("KIROCREW_PORT", "7929")
        assert agent._managed_mcp_env().get("KIROCREW_PORT") == "7929"

    def test_default_install_pins_nothing(self, monkeypatch):
        from kiro_crew import agent

        monkeypatch.delenv("KIROCREW_BOUND_PORT", raising=False)
        monkeypatch.delenv("KIROCREW_PORT", raising=False)
        assert "KIROCREW_PORT" not in agent._managed_mcp_env()

    def test_garbage_port_is_not_pinned(self, monkeypatch):
        from kiro_crew import agent

        monkeypatch.setenv("KIROCREW_BOUND_PORT", "not-a-port")
        monkeypatch.delenv("KIROCREW_PORT", raising=False)
        assert "KIROCREW_PORT" not in agent._managed_mcp_env()
