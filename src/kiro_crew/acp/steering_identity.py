"""Whether a shared-runtime session takes ``_session/steering``, from its handshake.

``ACP_BACKENDS_STEERING_REQUEST`` answers by backend id, which cannot name a
``harnesses.json`` backend: the Claude seats run claude-agent-acp under their own
ids (``claude-seat1``...), so every mid-turn message to a seat was queued. The
adapter names itself and advertises the extension at ``initialize``, so a runtime
that identified itself as a steerable claude-agent-acp takes the request too, with
claude's ``idleBehavior: "promptRequired"`` as ``AcpClient`` sends it.
"""

from __future__ import annotations

from typing import Any

from kiro_crew.acp.client_claude_session import (
    CLAUDE_ACP_STEERING_MIN_VERSION,
    _adapter_version_at_least,
)
from kiro_crew.acp.types import ACP_BACKENDS_STEERING_REQUEST
from kiro_crew.agent_sdk.backends import ACP_BACKEND_CLAUDE
from kiro_crew.agent_sdk.capability_inheritance import capability_source, inherits_capabilities

#: ``agentInfo.name`` claude-agent-acp reports at ``initialize`` (its package name).
CLAUDE_ACP_AGENT_NAME = "@agentclientprotocol/claude-agent-acp"


def claude_adapter_reported(init_resp: object) -> bool:
    """Whether an ``initialize`` result names claude-agent-acp, whatever the backend id."""
    info = init_resp.get("agentInfo") if isinstance(init_resp, dict) else None
    return isinstance(info, dict) and info.get("name") == CLAUDE_ACP_AGENT_NAME


def claude_steering_ready(runtime: Any) -> bool:
    """*runtime* is claude-agent-acp at or above the steering floor and advertised it."""
    return (
        getattr(runtime, "claude_adapter", False) is True
        and getattr(runtime, "steering_advertised", False) is True
        and _adapter_version_at_least(
            getattr(runtime, "agent_version", "") or "", CLAUDE_ACP_STEERING_MIN_VERSION
        )
    )


def takes_steering_request(runtime: Any) -> bool:
    """Whether a user steer on *runtime* travels on ``_session/steering``.

    A seat that inherits claude's memberships (``capabilities_from``) is in the
    table by inheritance, but claude's own steer is gated on the adapter's version
    and its advertisement, so the seat is held to the same check here.
    """
    backend = runtime.acp_backend
    if isinstance(backend, str) and inherits_capabilities(backend):
        if capability_source(backend) == ACP_BACKEND_CLAUDE:
            return claude_steering_ready(runtime)
    return backend in ACP_BACKENDS_STEERING_REQUEST or claude_steering_ready(runtime)


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
    "takes_steering_request",
]
