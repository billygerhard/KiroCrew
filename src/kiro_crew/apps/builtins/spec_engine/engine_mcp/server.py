"""The stdio JSON-RPC dispatch for the Engine_MCP_Server.

Line-delimited JSON-RPC 2.0 on stdin/stdout, the shape a KiroCrew builtin vends
an MCP server as (a command entry, not a URL: a builtin runs in-process and has
no backend port to dial). The methods a client's init sequence sends all answer
conformantly — `initialize`, `tools/list`, and the empty `prompts/list` and
`resources/list` — and, critically, an unknown method returns a proper JSON-RPC
error rather than an empty success: an empty success for an unknown method is
how a client registers zero tools with nothing logged.

Run as:
``python -m kiro_crew.apps.builtins.spec_engine.engine_mcp.server``
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from typing import Any, Callable

from kiro_crew.security import redact

from .guidance import GuidanceUnavailable, get_authoring_guidance, get_guidance
from .operations import EngineOperations

#: JSON-RPC 2.0 error codes.
_METHOD_NOT_FOUND = -32601
_INVALID_PARAMS = -32602
_INTERNAL_ERROR = -32603

#: Protocol version echoed on initialize.
_PROTOCOL_VERSION = "2024-11-05"

#: Cap on a single serialized tool result, so a large document's violation list
#: cannot pull an unbounded string into the model's context.
_MAX_RESULT_CHARS = 200_000

#: A tool handler: given the parsed arguments and (for operational tools) the
#: engine adapter, it returns either authored text or a JSON-serialisable dict.
ToolFn = Callable[[dict[str, Any], "EngineOperations | None"], "str | dict[str, Any]"]

_STRING = {"type": "string"}


@dataclass(frozen=True)
class ToolSpec:
    """One registered tool: its handler, description, and declared arguments."""

    fn: ToolFn
    description: str
    properties: dict[str, Any] = field(default_factory=dict)
    required: tuple[str, ...] = ()
    #: Whether the handler needs the engine adapter. Guidance tools do not, so
    #: they answer without touching the filesystem or the state store.
    needs_ops: bool = False


def _redact_result(text: str) -> str:
    """Credential/exfil-scan a serialized result before it reaches the model.

    Fail-closed: an operational result carries text derived from the caller's own
    documents (a validator message quotes the line it flagged), which is
    untrusted, and it is handed to an LLM whose output may be logged or echoed.
    A result that cannot be scanned is withheld rather than served raw.
    """
    try:
        return redact(text)
    except Exception:  # noqa: BLE001 - never emit unscanned caller-derived text
        print("spec-engine mcp: result redaction failed; withholding", file=sys.stderr)
        return '{"error": "result withheld: redaction unavailable"}'


def _redact_error(text: str) -> str:
    """Scrub an error string before it reaches the model.

    Tool arguments reach exception text by design (an unknown spec name is
    quoted back), so the same fail-closed scrub the result path uses applies
    here: the caller still learns the call failed, and the JSON-RPC error code —
    the actionable part — is never derived from input.
    """
    try:
        return redact(text)
    except Exception:  # noqa: BLE001 - never emit unscanned text
        return "withheld: redaction unavailable"


# --- tool handlers ---------------------------------------------------------


def _tool_get_authoring_prompt(args: dict[str, Any], _ops: EngineOperations | None) -> str:
    """Complete authoring instructions for a spec type, as the tool result."""
    return get_authoring_guidance(str(args.get("spec_type") or ""))


def _tool_get_orchestrator_prompt(_args: dict[str, Any], _ops: EngineOperations | None) -> str:
    """Complete orchestration instructions for executing a spec."""
    return get_guidance("orchestrator")


def _tool_get_review_prompt(_args: dict[str, Any], _ops: EngineOperations | None) -> str:
    """Complete review-verdict instructions, including the test-quality criteria."""
    return get_guidance("review")


def _tool_validate_spec(args: dict[str, Any], ops: EngineOperations | None) -> dict[str, Any]:
    """Validate a spec's native documents and cross-document claims."""
    return _adapter(ops).validate_spec(str(args["project"]), str(args["spec"]))


def _tool_get_phase(args: dict[str, Any], ops: EngineOperations | None) -> dict[str, Any]:
    """Report where a spec sits in its document plan, read-only."""
    return _adapter(ops).get_phase(str(args["project"]), str(args["spec"]))


def _tool_list_tasks(args: dict[str, Any], ops: EngineOperations | None) -> dict[str, Any]:
    """List a spec's checklist tasks and its leaf tasks."""
    return _adapter(ops).list_tasks(str(args["project"]), str(args["spec"]))


def _tool_record_approval(args: dict[str, Any], ops: EngineOperations | None) -> dict[str, Any]:
    """Record an interactive approval of one gate."""
    return _adapter(ops).record_approval(
        str(args["project"]), str(args["spec"]), str(args["gate"]), str(args["actor"])
    )


def _tool_advance_phase(args: dict[str, Any], ops: EngineOperations | None) -> dict[str, Any]:
    """Ask whether a spec may move past a gate, and record the answer."""
    gate = args.get("gate")
    return _adapter(ops).advance_phase(
        str(args["project"]),
        str(args["spec"]),
        str(args["actor"]),
        gate=str(gate) if gate is not None else None,
    )


#: The registered tool surface. A stock Host_Agent holding only this server
#: reads the guidance tools to learn the workflow, then drives it through the
#: operational tools. There is deliberately no tool that writes the Autonomy_
#: Policy or the Delivery_Workflow: those are configuration only, and the
#: adapter's single configuration door is fenced by the engine's write path.
TOOLS: dict[str, ToolSpec] = {
    "get_authoring_prompt": ToolSpec(
        _tool_get_authoring_prompt,
        "Authoring instructions for a spec type (feature, bugfix, or quick): "
        "document formats, phase flow, and approval gates.",
        {"spec_type": {**_STRING, "description": "feature, bugfix, or quick."}},
        ("spec_type",),
    ),
    "get_orchestrator_prompt": ToolSpec(
        _tool_get_orchestrator_prompt,
        "Instructions for executing a spec: wave order, per-task review, and retry.",
    ),
    "get_review_prompt": ToolSpec(
        _tool_get_review_prompt,
        "Instructions for returning a task review verdict, with the test-quality criteria.",
    ),
    "validate_spec": ToolSpec(
        _tool_validate_spec,
        "Validate a spec's native documents and cross-document claims; returns every "
        "violation with its file, location, and rule.",
        {
            "project": {**_STRING, "description": "Path to the project holding the spec."},
            "spec": {**_STRING, "description": "The spec directory name."},
        },
        ("project", "spec"),
        needs_ops=True,
    ),
    "get_phase": ToolSpec(
        _tool_get_phase,
        "Report the spec's current phase and gate state, read-only.",
        {
            "project": {**_STRING, "description": "Path to the project holding the spec."},
            "spec": {**_STRING, "description": "The spec directory name."},
        },
        ("project", "spec"),
        needs_ops=True,
    ),
    "list_tasks": ToolSpec(
        _tool_list_tasks,
        "List the spec's tasks.md checklist and its leaf tasks.",
        {
            "project": {**_STRING, "description": "Path to the project holding the spec."},
            "spec": {**_STRING, "description": "The spec directory name."},
        },
        ("project", "spec"),
        needs_ops=True,
    ),
    "record_approval": ToolSpec(
        _tool_record_approval,
        "Record an interactive approval of one phase gate. The engine validates the "
        "document first and refuses an approval it would not otherwise accept.",
        {
            "project": {**_STRING, "description": "Path to the project holding the spec."},
            "spec": {**_STRING, "description": "The spec directory name."},
            "gate": {**_STRING, "description": "The gate to approve (requirements, design, tasks)."},
            "actor": {**_STRING, "description": "The approver's identity."},
        },
        ("project", "spec", "gate", "actor"),
        needs_ops=True,
    ),
    "advance_phase": ToolSpec(
        _tool_advance_phase,
        "Advance a spec past a gate. The engine refuses while the document fails "
        "validation or lacks a live approval, and returns the blocking reasons.",
        {
            "project": {**_STRING, "description": "Path to the project holding the spec."},
            "spec": {**_STRING, "description": "The spec directory name."},
            "actor": {**_STRING, "description": "The initiator's identity."},
            "gate": {**_STRING, "description": "The gate being left (optional; defaults to the last written)."},
        },
        ("project", "spec", "actor"),
        needs_ops=True,
    ),
}


def _adapter(ops: EngineOperations | None) -> EngineOperations:
    """Return the injected adapter, or a default rooted at the app data home."""
    return ops if ops is not None else EngineOperations()


def _input_schema(name: str) -> dict[str, Any]:
    """The declared inputSchema for one tool.

    Factored out so ``tools/list`` and the argument validator cannot advertise
    one shape and check another. ``additionalProperties`` is false so an unknown
    key is refused rather than ignored.
    """
    spec = TOOLS[name]
    return {
        "type": "object",
        "properties": spec.properties,
        "required": list(spec.required),
        "additionalProperties": False,
    }


def _schema() -> list[dict[str, Any]]:
    return [
        {"name": name, "description": spec.description, "inputSchema": _input_schema(name)}
        for name, spec in TOOLS.items()
    ]


def _result(req_id: Any, payload: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": payload}


def _error(req_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def _content(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def handle(
    request: dict[str, Any], *, ops: EngineOperations | None = None
) -> dict[str, Any] | None:
    """Handle one JSON-RPC request. Returns None for a notification."""
    method = str(request.get("method") or "")
    req_id = request.get("id")
    raw_params = request.get("params")
    params: dict[str, Any] = raw_params if isinstance(raw_params, dict) else {}

    if method == "initialize":
        return _result(
            req_id,
            {
                "protocolVersion": _PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "spec-engine", "version": "1.0.0"},
            },
        )
    if method in {"notifications/initialized", "initialized"}:
        return None  # a notification carries no id and expects no reply
    if method == "tools/list":
        return _result(req_id, {"tools": _schema()})
    if method == "prompts/list":
        # Guidance is vended as tools, not as MCP prompts, so this set is empty —
        # but it is answered, because a client's init sequence sends it and a
        # missing answer would look like a broken server.
        return _result(req_id, {"prompts": []})
    if method == "resources/list":
        return _result(req_id, {"resources": []})
    if method == "tools/call":
        return _handle_call(req_id, params, ops)
    # Unknown method: a JSON-RPC error, never an empty success. An empty success
    # here is how a client registers zero tools with nothing in any log.
    return _error(req_id, _METHOD_NOT_FOUND, f"unknown method: {_redact_error(method)}")


def _handle_call(
    req_id: Any, params: dict[str, Any], ops: EngineOperations | None
) -> dict[str, Any]:
    name = str(params.get("name") or "")
    safe_name = _redact_error(name)
    spec = TOOLS.get(name)
    if spec is None:
        return _error(req_id, _METHOD_NOT_FOUND, f"unknown tool: {safe_name}")

    raw_args = params.get("arguments")
    if raw_args is not None and not isinstance(raw_args, dict):
        # Reject a present-but-non-dict arguments rather than coercing it to {},
        # which would let a malformed call reach a no-required-arg tool.
        return _error(req_id, _INVALID_PARAMS, "invalid arguments: expected an object")
    args: dict[str, Any] = raw_args if isinstance(raw_args, dict) else {}

    try:
        from kiro_crew.validation import validate_mcp_tool_arguments

        validate_mcp_tool_arguments(args, _input_schema(name))
    except ImportError:  # pragma: no cover - the validator ships with the package
        pass
    except Exception as exc:  # noqa: BLE001 - a schema violation is a client error
        return _error(req_id, _INVALID_PARAMS, f"invalid arguments: {_redact_error(str(exc))}")

    try:
        payload = spec.fn(args, ops if spec.needs_ops else None)
    except GuidanceUnavailable as exc:
        # Unavailable guidance is an error, never partial text.
        return _error(req_id, _INVALID_PARAMS, _redact_error(str(exc)))
    except (ValueError, KeyError) as exc:
        return _error(req_id, _INVALID_PARAMS, _redact_error(str(exc)))
    except Exception as exc:  # noqa: BLE001 - a tool error is a result, not a crash
        detail = f"{type(exc).__name__}: {_redact_error(str(exc))}"
        return _error(req_id, _INTERNAL_ERROR, detail)

    text = payload if isinstance(payload, str) else json.dumps(payload, default=str)
    # Redact before truncating so a credential cannot be split across the cut and
    # leave a fragment the scanner no longer recognizes.
    return _result(req_id, _content(_redact_result(text)[:_MAX_RESULT_CHARS]))


def main() -> None:
    """Read line-delimited JSON-RPC on stdin, write replies on stdout.

    One malformed line must not end the session: the client may still send valid
    requests afterwards, and dying here would surface as the server vanishing
    mid-session.
    """
    ops = EngineOperations()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except ValueError:
            continue
        if not isinstance(request, dict):
            continue
        reply = handle(request, ops=ops)
        if reply is None:
            continue
        sys.stdout.write(json.dumps(reply) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
