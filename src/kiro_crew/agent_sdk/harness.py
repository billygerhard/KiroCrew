"""Harness selection and vocabulary, as the SDK-side import surface.

Application code that binds a session to a harness — the dashboard chat
handlers, cron, the subagent manager, the CLI — needs the selection entry
points (:func:`resolve_session_harness`, :func:`default_harness_id`), the
refusal types those raise, and the small descriptor vocabulary the callers
branch on (MCP delivery modes, the reasoning-effort capability key). Before
this module existed each of those files imported ``kiro_crew.acp.*`` directly,
which is exactly the edge ``scripts/check_agent_sdk_boundary.py`` refuses to
let grow.

This is the Phase-1-era shape the boundary RFC
(``docs/request-for-change/rfc-crew-agent-sdk-boundary.md``) prescribes for a
consumer need the SDK does not carry yet: the surface moves INSIDE the
boundary package (this tree is the gate's exempt set, because it IS the
boundary), so the day a later phase replaces these re-exports with SDK-owned
types, every consumer already imports from the one module that changes.

Re-export, not translation, deliberately for now: the selection functions
return :class:`HarnessBinding` — a frozen dataclass of plain strings plus the
descriptor — and the constants are plain strings, so no JSON-RPC or wire shape
crosses here. The one genuinely ACP-flavoured object is
:class:`HarnessDescriptor`, which the MCP gateway needs to type a parameter;
translating it would mean duplicating a validated frozen dataclass field for
field with no seam gained.

Unlike :mod:`kiro_crew.agent_sdk.drivers.acp` this module imports at module
scope: every consumer of these names already held module-scope imports of the
same targets (they are what this module replaces), so laziness here would not
keep anything off the boot path that is not already on it.
"""

from __future__ import annotations

from kiro_crew.acp.harness_descriptor import (
    CAPABILITY_REASONING_EFFORT,
    MCP_DELIVERY_FILE_FED,
    MCP_DELIVERY_WIRE_FED,
    MODEL_SOURCE_STATIC,
    HarnessDescriptor,
)
from kiro_crew.acp.harness_registry import (
    HARNESS_KIRO,
    HarnessUnavailable,
    UnknownHarness,
    registry,
)
from kiro_crew.acp.harness_selection import (
    HarnessBinding,
    HarnessBindingConflict,
    HarnessNotServiceable,
    default_harness_id,
    pooled_harness_id,
    resolve_session_harness,
    unserviceable_reason,
)

__all__ = [
    "CAPABILITY_REASONING_EFFORT",
    "HARNESS_KIRO",
    "HarnessBinding",
    "HarnessBindingConflict",
    "HarnessDescriptor",
    "HarnessNotServiceable",
    "HarnessUnavailable",
    "MCP_DELIVERY_FILE_FED",
    "MCP_DELIVERY_WIRE_FED",
    "MODEL_SOURCE_STATIC",
    "UnknownHarness",
    "default_harness_id",
    "pooled_harness_id",
    "registry",
    "resolve_session_harness",
    "unserviceable_reason",
]
