"""JSON-RPC conformance for the Engine_MCP_Server.

The cases that matter here are the ones a happy-path test skips: the unknown
method that must be a proper error rather than an empty success (the defect that
once made a client register zero tools with nothing logged), the malformed
`arguments`, and the unknown tool. The init-sequence methods are asserted too,
including the empty `prompts/list` and `resources/list` a client sends before it
lists tools.
"""

from __future__ import annotations

from typing import Any

from kiro_crew.apps.builtins.spec_engine.engine_mcp import server
from kiro_crew.apps.builtins.spec_engine.engine_mcp.server import TOOLS, handle

_METHOD_NOT_FOUND = -32601
_INVALID_PARAMS = -32602


def _req(method: str, params: dict[str, Any] | None = None, req_id: Any = 1) -> dict[str, Any]:
    request: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        request["params"] = params
    return request


def test_initialize_reports_protocol_and_server_name() -> None:
    reply = handle(_req("initialize"))
    assert reply is not None
    result = reply["result"]
    assert result["protocolVersion"]
    assert result["serverInfo"]["name"] == "spec-engine"
    assert "tools" in result["capabilities"]


def test_notifications_initialized_is_a_notification() -> None:
    # A notification carries no id and expects no reply.
    assert handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert handle({"jsonrpc": "2.0", "method": "initialized"}) is None


def test_tools_list_advertises_every_registered_tool() -> None:
    reply = handle(_req("tools/list"))
    assert reply is not None
    listed = {t["name"] for t in reply["result"]["tools"]}
    assert listed == set(TOOLS)
    for tool in reply["result"]["tools"]:
        schema = tool["inputSchema"]
        assert schema["type"] == "object"
        # Closed schema: an unknown argument is refused, not ignored.
        assert schema["additionalProperties"] is False


def test_prompts_list_answers_an_empty_set() -> None:
    reply = handle(_req("prompts/list"))
    assert reply is not None
    assert reply["result"] == {"prompts": []}


def test_resources_list_answers_an_empty_set() -> None:
    reply = handle(_req("resources/list"))
    assert reply is not None
    assert reply["result"] == {"resources": []}


def test_unknown_method_is_a_jsonrpc_error_not_empty_success() -> None:
    reply = handle(_req("does/not/exist"))
    assert reply is not None
    # The whole point: an unknown method returns an error object with the right
    # code, never an empty `result`, which is how a client silently registers
    # zero tools.
    assert "result" not in reply
    assert reply["error"]["code"] == _METHOD_NOT_FOUND


def test_unknown_tool_is_method_not_found() -> None:
    reply = handle(_req("tools/call", {"name": "no_such_tool", "arguments": {}}))
    assert reply is not None
    assert "result" not in reply
    assert reply["error"]["code"] == _METHOD_NOT_FOUND


def test_non_dict_arguments_are_rejected() -> None:
    reply = handle(_req("tools/call", {"name": "get_orchestrator_prompt", "arguments": [1, 2]}))
    assert reply is not None
    assert "result" not in reply
    assert reply["error"]["code"] == _INVALID_PARAMS


def test_missing_required_argument_is_invalid_params() -> None:
    reply = handle(_req("tools/call", {"name": "get_authoring_prompt", "arguments": {}}))
    assert reply is not None
    assert "result" not in reply
    assert reply["error"]["code"] == _INVALID_PARAMS


def test_unknown_argument_is_refused_by_the_closed_schema() -> None:
    reply = handle(
        _req(
            "tools/call",
            {"name": "get_authoring_prompt", "arguments": {"spec_type": "feature", "x": 1}},
        )
    )
    assert reply is not None
    assert "result" not in reply
    assert reply["error"]["code"] == _INVALID_PARAMS


def test_deleting_a_registration_hides_the_tool(monkeypatch: Any) -> None:
    # Registration is what makes a tool real: with the entry removed, the tool
    # is neither advertised nor callable, and the surface test that expects it
    # fails. This pins that the registry, not the handler, is the surface.
    patched = dict(TOOLS)
    del patched["validate_spec"]
    monkeypatch.setattr(server, "TOOLS", patched)

    tools = handle(_req("tools/list"))
    assert tools is not None, "tools/list is a request and always answers"
    listed = {t["name"] for t in tools["result"]["tools"]}
    assert "validate_spec" not in listed

    reply = handle(_req("tools/call", {"name": "validate_spec", "arguments": {}}))
    assert reply is not None, "a call carrying an id always answers"
    assert reply["error"]["code"] == _METHOD_NOT_FOUND
