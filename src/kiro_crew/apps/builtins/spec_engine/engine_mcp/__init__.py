"""The Engine_MCP_Server: a thin stdio JSON-RPC wrapper over the Spec_Engine.

Every tool call maps onto one library call, so invoking an operation through
this server and invoking it through the engine library produce the same state.
The wrapper owns no spec rules; it owns the authored guidance it hands back as
tool results and the boundary that keeps caller-supplied data out of that
guidance. Two objects are deliberately unreachable from here: the Autonomy_Policy
and the Delivery_Workflow are configuration only, and no tool this server
exposes can mutate them (:mod:`.operations` routes any configuration write it
would make through the engine's single validated write path on a surface no
operator confirmed, so the shared fence refuses the config-only objects rather
than a second fence deciding it here).
"""

from __future__ import annotations

from .guidance import FLOWS, GuidanceUnavailable, get_guidance
from .operations import ENGINE_MCP_SURFACE, EngineOperations
from .server import TOOLS, handle, main

__all__ = [
    "ENGINE_MCP_SURFACE",
    "FLOWS",
    "TOOLS",
    "EngineOperations",
    "GuidanceUnavailable",
    "get_guidance",
    "handle",
    "main",
]
