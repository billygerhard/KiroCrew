"""Whether a shared-runtime session takes ``_session/steering``, from its handshake.

``ACP_BACKENDS_STEERING_REQUEST`` answers by backend id, which cannot name a
``harnesses.json`` backend: the Claude seats run claude-agent-acp under their own
ids (``claude-seat1``...), so every mid-turn message to a seat was queued. The
adapter names itself and advertises the extension at ``initialize``, so a runtime
whose ``initialize_result`` identifies a steerable claude-agent-acp takes the
request too, with claude's ``idleBehavior: "promptRequired"`` as ``AcpClient``
sends it.
"""

from __future__ import annotations

from typing import Any

from kiro_crew.acp.client_claude_session import (
    CLAUDE_ACP_STEERING_MIN_VERSION,
    _adapter_version_at_least,
)
from kiro_crew.acp.types import ACP_BACKENDS_STEERING_REQUEST, steering_advertised
from kiro_crew.agent_sdk.backends import ACP_BACKEND_CLAUDE
from kiro_crew.agent_sdk.capability_inheritance import capability_source, inherits_capabilities

#: ``agentInfo.name`` claude-agent-acp reports at ``initialize`` (its package name).
CLAUDE_ACP_AGENT_NAME = "@agentclientprotocol/claude-agent-acp"


def _initialize_result(runtime: Any) -> dict:
    init = getattr(runtime, "initialize_result", None)
    return init if isinstance(init, dict) else {}


def claude_adapter_reported(init_resp: object) -> bool:
    """Whether an ``initialize`` result names claude-agent-acp, whatever the backend id."""
    info = init_resp.get("agentInfo") if isinstance(init_resp, dict) else None
    return isinstance(info, dict) and info.get("name") == CLAUDE_ACP_AGENT_NAME


def claude_steering_ready(runtime: Any) -> bool:
    """*runtime* is claude-agent-acp at or above the steering floor and advertised it."""
    init = _initialize_result(runtime)
    return (
        claude_adapter_reported(init)
        and steering_advertised(init)
        and _adapter_version_at_least(
            getattr(runtime, "agent_version", "") or "", CLAUDE_ACP_STEERING_MIN_VERSION
        )
    )


def _inherits_claude(backend: object) -> bool:
    return (
        isinstance(backend, str)
        and inherits_capabilities(backend)
        and capability_source(backend) == ACP_BACKEND_CLAUDE
    )


def takes_steering_request(runtime: Any) -> bool:
    """Whether a steer on *runtime* would travel on ``_session/steering``.

    Membership alone for a listed backend, as upstream's loss-recovery and denial
    rules read it; a Claude seat only once its handshake proved the extension. A
    seat that inherits claude's memberships (``capabilities_from``) is in the table
    by inheritance, but claude's own steer is gated on the adapter's version and
    its advertisement, so the seat is held to the same check here.
    """
    if _inherits_claude(runtime.acp_backend):
        return claude_steering_ready(runtime)
    return runtime.acp_backend in ACP_BACKENDS_STEERING_REQUEST or claude_steering_ready(runtime)


def steering_request_ready(runtime: Any) -> bool:
    """Whether *runtime*'s handshake advertised ``_session/steering`` for a steer now."""
    if not _inherits_claude(runtime.acp_backend) and (
        runtime.acp_backend in ACP_BACKENDS_STEERING_REQUEST
    ):
        return steering_advertised(_initialize_result(runtime))
    return claude_steering_ready(runtime)


def steering_request_meta(runtime: Any) -> dict[str, Any] | None:
    """The ``_meta`` a steering request carries on *runtime*, or ``None``.

    claude-agent-acp refuses a steer that arrives after the turn ended instead of
    starting a turn of its own, the same request ``AcpClient`` sends.
    """
    if claude_steering_ready(runtime):
        return {"steering": {"idleBehavior": "promptRequired"}}
    return None


__all__ = [
    "CLAUDE_ACP_AGENT_NAME",
    "claude_adapter_reported",
    "claude_steering_ready",
    "steering_request_meta",
    "steering_request_ready",
    "takes_steering_request",
]
