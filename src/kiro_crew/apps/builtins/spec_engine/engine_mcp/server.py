"""The stdio JSON-RPC dispatch for the Engine_MCP_Server.

Line-delimited JSON-RPC 2.0 on stdin/stdout, the shape a Kiro Crew builtin vends
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

from ..engine.config import ConfigWarning
from ..engine.config.store import ConfigLoadError, ConfigValidationError, ConfigWriteRefused
from ..engine.setup import InferredSubjectRefused, SetupApprovalRequired
from .config_surface import write_payload, write_refusal
from .guidance import GuidanceUnavailable, get_authoring_guidance, get_guidance
from .operations import EngineOperations
from .setup_surface import refusal_payload

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

#: The operator's answers, shared by ``plan_setup`` and ``apply_setup`` so the two
#: cannot advertise different answer shapes for the same flow. Every field is
#: optional in the schema and none is defaulted here: an unanswered rung and an
#: unchosen cost profile are refusals the engine makes with a message naming what
#: is missing, and a schema-level ``required`` would answer with a JSON-RPC error
#: that names a key instead.
_ANSWERS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": (
        "The operator's answers to the questions inspect_setup returned. "
        "cost_profile and each autonomy confirmation are asked, never inferred."
    ),
    "properties": {
        "cost_profile": {
            **_STRING,
            "description": "Bundled cost profile name the operator chose.",
        },
        "confirmations": {
            "type": "object",
            "description": (
                "One true/false per autonomy rung above authoring, keyed by rung name. "
                "Each rung is confirmed separately; a missing rung is unanswered, not no."
            ),
            "additionalProperties": {"type": "boolean"},
        },
        "approved_subjects": {
            "type": "array",
            "description": "Subjects of the inferences the operator accepted.",
            "items": _STRING,
        },
        "workflow_preset": {
            "type": ["string", "null"],
            "description": "Offered workflow preset to write, or null for none.",
        },
        "watch_source": {
            "type": ["string", "null"],
            "description": "Offered watch source preset to write, or null for none.",
        },
    },
    "additionalProperties": False,
}


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


def _refusing(call: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """Run *call*, returning a structured refusal for a setup refusal it raises.

    A refusal is a decision the engine made and an answer the caller can act on --
    which rung is unanswered, which preset was never offered, that the plan is
    stale -- so it comes back as a result with a ``refused`` code rather than as a
    protocol error carrying a class name. Anything else propagates: a broken setup
    path must surface as a tool error, never as an empty plan or a refusal the
    engine did not make.

    The catch is written against the class chain the setup module actually raises,
    not against what the names suggest: ``InferredSubjectRefused`` derives
    ``ValueError`` and ``SetupApprovalRequired`` derives ``PermissionError`` (and
    so ``OSError``). Catching them here, inside the handler, also puts them ahead
    of the dispatcher's ``(ValueError, KeyError)`` clause, which would otherwise
    turn the first into an invalid-arguments error and let the second fall through
    to the internal-error clause.
    """
    try:
        return call()
    except (InferredSubjectRefused, SetupApprovalRequired) as exc:
        refusal = refusal_payload(exc)
        if refusal is None:  # pragma: no cover - both classes are in REFUSAL_CODES
            raise
        return refusal


def _tool_inspect_setup(args: dict[str, Any], ops: EngineOperations | None) -> dict[str, Any]:
    """Inspect a project: evidence read, values inferred, questions left to ask."""
    name = args.get("name")
    return _refusing(
        lambda: _adapter(ops).inspect_setup(
            str(args["project"]), name=str(name) if name is not None else None
        )
    )


def _tool_plan_setup(args: dict[str, Any], ops: EngineOperations | None) -> dict[str, Any]:
    """Compute the configuration plan a set of answers produces. Writes nothing."""
    name = args.get("name")
    answers = args.get("answers")
    if not isinstance(answers, dict):
        raise ValueError("answers must be an object holding the operator's answers")
    return _refusing(
        lambda: _adapter(ops).plan_setup(
            str(args["project"]), answers, name=str(name) if name is not None else None
        )
    )


def _tool_apply_setup(args: dict[str, Any], ops: EngineOperations | None) -> dict[str, Any]:
    """Apply a plan by its identity, on a named human approver's authority."""
    name = args.get("name")
    answers = args.get("answers")
    if not isinstance(answers, dict):
        raise ValueError("answers must be an object holding the operator's answers")
    return _refusing(
        lambda: _adapter(ops).apply_setup(
            str(args["project"]),
            answers,
            plan_id=str(args.get("plan_id") or ""),
            approver=str(args.get("approver") or ""),
            name=str(name) if name is not None else None,
        )
    )


def _refusing_config(call: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """Run *call*, returning a structured refusal for a configuration refusal.

    Traced against what :meth:`ConfigStore.write` and
    :meth:`ConfigStore.document` raise, not against the class names:
    ``ConfigWriteRefused`` derives ``PermissionError``, ``ConfigValidationError``
    derives ``ValueError``, and ``ConfigLoadError`` derives ``RuntimeError``.
    Catching them inside the handler is what puts them ahead of the dispatcher's
    ``(ValueError, KeyError)`` clause, which would report a fenced path as
    malformed arguments and a corrupt file as an internal error.

    ``ConfigRecordError`` is deliberately absent: it means the document was
    persisted and nothing recorded who wrote it, so it must reach the caller as a
    tool error rather than as a decision the engine made.
    """
    try:
        return call()
    except (ConfigWriteRefused, ConfigValidationError, ConfigLoadError) as exc:
        refusal = write_refusal(exc)
        if refusal is None:  # pragma: no cover - all three are in REFUSAL_CODES
            raise
        return refusal


def _tool_get_config(_args: dict[str, Any], ops: EngineOperations | None) -> dict[str, Any]:
    """Read the persisted configuration, with secret-classified values elided."""
    return _refusing_config(lambda: _adapter(ops).get_config())


def _tool_write_config(args: dict[str, Any], ops: EngineOperations | None) -> dict[str, Any]:
    """Merge a configuration patch through the engine's one fenced write path."""
    patch = args.get("patch")
    if not isinstance(patch, dict):
        raise ValueError("patch must be an object holding the configuration to merge")
    actor = args.get("actor")
    advisories: list[ConfigWarning] = []

    def write() -> dict[str, Any]:
        merged = _adapter(ops).write_config(
            patch,
            actor=str(actor) if actor is not None else None,
            warn=advisories.append,
        )
        return write_payload(patch, merged, advisories)

    return _refusing_config(write)


def _tool_run_doctor(args: dict[str, Any], _ops: EngineOperations | None) -> dict[str, Any]:
    """Every Finding the Doctor found, as the UI panel's own path returns them.

    Delegates to the app-side surface rather than assembling a diagnostic here.
    That is the requirement, not tidiness: a tool that built its own aggregation
    would pick its own collaborators, and a panel and a tool disagreeing about
    whether a host is ready is worse than either alone. Read-only and free -- no
    model turn, and the one subprocess in the path is a ``--version`` probe.
    """
    from ..diagnostics import doctor_payload

    project = args.get("project")
    return doctor_payload(project=str(project) if project else None)


def _tool_check_run_prerequisites(
    args: dict[str, Any], _ops: EngineOperations | None
) -> dict[str, Any]:
    """Whether a run at an autonomy level may start, before any credit is spent.

    Quotes the same Finding identifiers the Doctor panel shows for the conditions
    that would refuse it, so an agent told "may not start" and an operator reading
    the panel are reading one sentence about one host.
    """
    from ..engine.autonomy import AutonomyLevel
    from ..engine.config import ConfigStore
    from ..engine.diagnosis import run_gate_report

    raw = str(args.get("autonomy") or "").strip().lower()
    try:
        level = AutonomyLevel(raw)
    except ValueError:
        raise ValueError(
            "autonomy must be one of: " + ", ".join(member.value for member in AutonomyLevel)
        ) from None
    project = args.get("project")
    return run_gate_report(ConfigStore(), level, project=str(project) if project else None)


#: The registered tool surface. A stock Host_Agent holding only this server
#: reads the guidance tools to learn the workflow, then drives it through the
#: operational tools. The Autonomy_Policy and the Delivery_Workflow are still
#: unreachable from here: ``write_config`` is the adapter's one configuration
#: door, and it writes on a surface no operator confirmed, so the engine's shared
#: fence refuses every config-only object through it on every transport. The one
#: path onto the confirmed surface is ``apply_setup``, which writes a patch the
#: engine built from an offered plan on a named human's authority.
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
            "gate": {
                **_STRING,
                "description": "The gate to approve (requirements, design, tasks).",
            },
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
            "gate": {
                **_STRING,
                "description": "The gate being left (optional; defaults to the last written).",
            },
        },
        ("project", "spec", "actor"),
        needs_ops=True,
    ),
    "inspect_setup": ToolSpec(
        _tool_inspect_setup,
        "Inspect a project and report what its own files state about how it works: the "
        "evidence read, the values inferred from it with that evidence attached, the "
        "questions that cannot be inferred and must be asked, and every bundled preset "
        "the evidence makes applicable together with the commands that preset would run. "
        "Read-only: it writes no configuration and spends nothing.",
        {
            "project": {**_STRING, "description": "Path to the project to set up."},
            "name": {
                **_STRING,
                "description": (
                    "Name to configure the project under (optional; defaults to the "
                    "project directory's own name)."
                ),
            },
        },
        ("project",),
        needs_ops=True,
    ),
    "plan_setup": ToolSpec(
        _tool_plan_setup,
        "Compute the configuration plan a set of answers produces, and apply nothing. "
        "Returns the patch that would be written, the paths it would touch, and a "
        "plan_id identifying it. Refuses when the cost profile was not chosen, when an "
        "autonomy rung is unanswered, or when a selected preset was never offered.",
        {
            "project": {**_STRING, "description": "Path to the project to set up."},
            "name": {
                **_STRING,
                "description": "Name to configure the project under (optional).",
            },
            "answers": _ANSWERS_SCHEMA,
        },
        ("project", "answers"),
        needs_ops=True,
    ),
    "apply_setup": ToolSpec(
        _tool_apply_setup,
        "Write a computed setup plan through the engine's validated configuration path. "
        "Requires the plan_id returned by plan_setup for the same project and answers, "
        "and a non-empty approver naming the human who accepted the plan. Refuses "
        "without an approver, and refuses a plan_id that no longer identifies the plan "
        "these inputs produce.",
        {
            "project": {**_STRING, "description": "Path to the project to set up."},
            "name": {
                **_STRING,
                "description": "Name to configure the project under (optional).",
            },
            "answers": _ANSWERS_SCHEMA,
            "plan_id": {
                **_STRING,
                "description": "The plan_id plan_setup returned for these same inputs.",
            },
            "approver": {
                **_STRING,
                "description": "Identity of the human who approved the plan.",
            },
        },
        ("project", "answers", "plan_id", "approver"),
        needs_ops=True,
    ),
    "get_config": ToolSpec(
        _tool_get_config,
        "Read the engine's persisted configuration: whether any exists yet, the document "
        "itself with every secret-classified value elided, the dotted paths that were "
        "elided, any validation error the saved document holds, and any advisory it earns. "
        "Read-only and spends nothing. Call this before setup to find out whether the "
        "project is configured at all.",
        needs_ops=True,
    ),
    "write_config": ToolSpec(
        _tool_write_config,
        "Merge a configuration patch into the engine's document through its single "
        "validated write path. Nested objects merge key by key and a null value returns a "
        "setting to its bundled default. Refuses, naming the paths, any config-only "
        "object — the autonomy grid, the delivery workflow, capability bindings on every "
        "transport, quality gates, intake guidance, and the unattended-integration "
        "switch — because those are writable only from a surface a human confirmed. "
        "Returns the merged document with secret values elided, plus any advisory the "
        "result earns.",
        {
            "patch": {
                "type": "object",
                "description": (
                    "The configuration to merge, shaped like the document itself. A null "
                    "value at a key removes it, restoring that setting's default."
                ),
                # Free-form on purpose: the patch IS configuration, and enumerating
                # the document's schema here would be a second schema to drift from
                # the validator that actually decides what is accepted.
                "additionalProperties": True,
            },
            "actor": {
                **_STRING,
                "description": (
                    "Who authorized this write (optional). Recorded with the write; the "
                    "caller asserts it, so it names an accountable human rather than "
                    "proving one."
                ),
            },
        },
        ("patch",),
        needs_ops=True,
    ),
    "run_doctor": ToolSpec(
        _tool_run_doctor,
        "Diagnose why spec runs are not working: prerequisite checks grouped by phase, "
        "watch source health, provider degradation, configuration errors, budget and "
        "kill switch state, runs waiting on a person, and whether this app's skill and "
        "MCP server reached agent sessions. Read-only and spends nothing.",
        {
            "project": {
                **_STRING,
                "description": "Limit project-scoped checks to this project (optional).",
            }
        },
    ),
    "check_run_prerequisites": ToolSpec(
        _tool_check_run_prerequisites,
        "Ask whether a run at an autonomy level may start. Returns every unmet "
        "prerequisite with the action that resolves it, and the Doctor Finding "
        "identifiers a refusal would quote. Read-only and spends nothing.",
        {
            "autonomy": {
                **_STRING,
                "description": "The autonomy level the run would reach.",
            },
            "project": {**_STRING, "description": "The project the run is for (optional)."},
        },
        ("autonomy",),
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
