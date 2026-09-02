#!/usr/bin/env python3
"""A minimal, deterministic ACP-speaking harness for integration tests.

Stdlib only. Speaks public ACP JSON-RPC 2.0 over stdio using NEWLINE-DELIMITED
framing (one compact JSON object per line), because that is exactly what
``kiro_crew.acp.client.AcpClient`` sends and reads:

* it writes ``json.dumps(...) + "\\n"`` and reads with ``stdout.readline()``
  (client.py ``_send_request`` / ``_send_response`` / ``_read_message``), so
  there is no ``Content-Length`` header anywhere in the transport. This stub
  matches that byte-for-byte: read a line, parse it, write compact JSON + "\\n".

What it implements (the public-ACP subset the real client drives):

* ``initialize`` — expects an INTEGER ``protocolVersion`` (the STANDARD_ACP
  profile sends ``1``); replies with ``protocolVersion`` echoed and an
  ``agentCapabilities`` object (``loadSession: false`` so the client never tries
  ``session/load``).
* ``session/new`` — replies with a ``sessionId`` and a ``models`` object
  ``{availableModels: [{modelId, name, description}], currentModelId}`` in the
  SAME shape ``session_handle.parse_advertised_models`` /
  ``AcpClient._capture_available_models`` read, advertising two fake models
  ``stub-fast`` / ``stub-smart``.
* ``session/prompt`` — streams, as ``session/update`` NOTIFICATIONS then a final
  response:
    1. one ``agent_message_chunk`` update echoing the prompt text,
    2. one server→client ``session/request_permission`` REQUEST carrying STANDARD
       ``options`` (``{optionId, name, kind}`` with kind ``allow_once`` /
       ``allow_always`` / ``reject_once``), then it BLOCKS for the client's
       ``{outcome:{outcome:"selected", optionId:...}}`` response,
    3. the final response to the prompt request with ``{stopReason:"end_turn"}``.
* ``session/cancel`` — acknowledges (empty result) and reports the in-flight
  turn's stopReason as ``cancelled`` if a prompt is pending.
* clean EOF — a closed stdin ends the loop and the process exits 0.

Determinism knobs, via env vars (so a test picks a mode without argv wrangling):

* ``STUB_ACP_BAD_INITIALIZE=1`` — reply to ``initialize`` with a NON-JSON line
  (garbage), so the client's handshake fails. Used to prove the failure surface
  names the harness + step and records a probe failure.
* ``STUB_ACP_DENY=1`` — the permission round trip is DENIED, not allowed: the
  stub still sends the ``session/request_permission`` request advertising the
  standard options, but it now ASSERTS the client's response selected the
  ``reject_once`` option (``{outcome:{outcome:"selected", optionId:"reject_once"}}``)
  and then completes the prompt with ``{stopReason:"refusal"}`` — the deny stop
  reason. If the client answers with anything other than the reject option the
  stub exits non-zero, so the test fails loudly rather than silently passing on
  an allow. Used to prove the permission-DENY transport end to end: a client
  that rejects a tool is received as ``reject_once`` and the turn ends on the
  refusal stop reason.
* ``STUB_ACP_MODELS`` — comma-separated modelIds overriding the default two.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

_MODELS_ENV = os.environ.get("STUB_ACP_MODELS", "").strip()
_MODEL_IDS = [m for m in _MODELS_ENV.split(",") if m] or ["stub-fast", "stub-smart"]
_BAD_INITIALIZE = os.environ.get("STUB_ACP_BAD_INITIALIZE", "") == "1"
_DENY = os.environ.get("STUB_ACP_DENY", "") == "1"

_SESSION_ID = "stub-session-1"


def _write(obj: dict[str, Any]) -> None:
    """Emit one compact JSON object as a newline-delimited frame."""
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _write_raw(line: str) -> None:
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def _result(req_id: Any, result: dict[str, Any]) -> None:
    _write({"jsonrpc": "2.0", "id": req_id, "result": result})


def _notify(method: str, params: dict[str, Any]) -> None:
    _write({"jsonrpc": "2.0", "method": method, "params": params})


def _server_request(req_id: Any, method: str, params: dict[str, Any]) -> None:
    _write({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})


def _read_message() -> dict[str, Any] | None:
    """Read one newline-framed JSON-RPC message, or None at clean EOF."""
    while True:
        line = sys.stdin.readline()
        if line == "":  # EOF: stdin closed
            return None
        text = line.strip()
        if not text:
            continue
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            # A malformed inbound frame is skipped, same tolerance the client has.
            continue


def _handle_initialize(req_id: Any) -> None:
    if _BAD_INITIALIZE:
        # Garbage instead of a valid response frame, then exit: the client reads
        # a non-JSON line (skipped), then hits EOF with the process gone and
        # raises AcpError("ACP process exited ...") — the real "harness died
        # during initialize" surface, and it fails fast rather than blocking on
        # the init deadline.
        _write_raw("this is not json — stub is misbehaving on purpose")
        sys.stdout.close()
        sys.exit(3)
    _result(
        req_id,
        {
            # Echo the integer version the client sent (STANDARD_ACP == 1).
            "protocolVersion": 1,
            "agentCapabilities": {
                # Keep loadSession off so the client never issues session/load.
                "loadSession": False,
            },
        },
    )


def _handle_session_new(req_id: Any) -> None:
    available = [
        {
            "modelId": model_id,
            "name": model_id.replace("-", " ").title(),
            "description": f"Stub model {model_id}",
        }
        for model_id in _MODEL_IDS
    ]
    _result(
        req_id,
        {
            "sessionId": _SESSION_ID,
            "modes": {  # a modes object so the client treats the session as live
                "currentModeId": "stub",
                "availableModes": [{"id": "stub", "name": "Stub"}],
            },
            "models": {
                "availableModels": available,
                "currentModelId": _MODEL_IDS[0],
            },
        },
    )


def _permission_options() -> list[dict[str, str]]:
    """STANDARD-ACP option shape: optionId + name + kind."""
    return [
        {"optionId": "allow_once", "name": "Allow once", "kind": "allow_once"},
        {"optionId": "allow_always", "name": "Allow always", "kind": "allow_always"},
        {"optionId": "reject_once", "name": "Reject", "kind": "reject_once"},
    ]


def _prompt_text(params: dict[str, Any]) -> str:
    blocks = params.get("prompt", [])
    parts: list[str] = []
    if isinstance(blocks, list):
        for block in blocks:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
    return "".join(parts)


def _handle_session_prompt(req_id: Any, params: dict[str, Any]) -> None:
    """Echo chunk → one permission round trip → end_turn.

    Emits updates as notifications, sends a server→client permission request and
    BLOCKS on the client's response, then answers the prompt request itself. The
    permission-request id is distinct from the prompt request id so the client's
    ``_wait_for_response`` correctly defers it and answers it via its permission
    path rather than mistaking it for the prompt reply.
    """
    text = _prompt_text(params)
    # 1. Echo the prompt back as one agent_message_chunk.
    _notify(
        "session/update",
        {
            "sessionId": _SESSION_ID,
            "update": {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": f"echo: {text}"},
            },
        },
    )

    # 2. One tool-permission round trip.
    perm_id = "perm-1"
    _server_request(
        perm_id,
        "session/request_permission",
        {
            "sessionId": _SESSION_ID,
            "toolCall": {
                "toolCallId": "tc-1",
                "title": "stub_tool",
                "kind": "other",
            },
            "options": _permission_options(),
        },
    )
    # Block for the client's response to the permission request. Ignore any
    # unrelated inbound frames (there should be none in the test flow).
    while True:
        msg = _read_message()
        if msg is None:  # client closed mid-turn
            return
        if msg.get("id") == perm_id and "result" in msg:
            break

    if _DENY:
        # DENY mode: the client is expected to have REJECTED the tool. Assert the
        # response is a `selected` outcome carrying the advertised reject option
        # (reject_once), then complete the prompt with the refusal stop reason.
        # A non-reject response is a test-visible failure (an allow leaking
        # through), so exit non-zero rather than emitting a spurious refusal.
        outcome = (msg.get("result") or {}).get("outcome") or {}
        if not (outcome.get("outcome") == "selected" and outcome.get("optionId") == "reject_once"):
            sys.stderr.write(f"stub: expected reject_once selection, got {outcome!r}\n")
            sys.stderr.flush()
            sys.exit(4)
        _result(req_id, {"stopReason": "refusal"})
        return

    # 3. Final response to the prompt request.
    _result(req_id, {"stopReason": "end_turn"})


def _handle_session_cancel(req_id: Any) -> None:
    # Acknowledge the cancel; the real ack the client keys on is the prompt's
    # stopReason:"cancelled", which a pending turn would emit. With no turn in
    # flight the empty ack is sufficient and harmless.
    _result(req_id, {})


def main() -> int:
    while True:
        msg = _read_message()
        if msg is None:  # clean EOF → shutdown
            return 0
        method = msg.get("method")
        req_id = msg.get("id")
        params = msg.get("params") or {}

        if method == "initialize":
            _handle_initialize(req_id)
        elif method == "session/new":
            _handle_session_new(req_id)
        elif method == "session/prompt":
            _handle_session_prompt(req_id, params if isinstance(params, dict) else {})
        elif method == "session/cancel":
            _handle_session_cancel(req_id)
        elif req_id is not None:
            # Any other server-directed request must be answered or the peer
            # blocks; reply method-not-found (JSON-RPC -32601).
            _write(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32601, "message": f"Method not found: {method}"},
                }
            )
        # A notification we do not recognize is simply ignored.


if __name__ == "__main__":
    sys.exit(main())
