"""The one concrete harness that serves an OPERATOR-DEFINED (config) backend.

Every bundled harness is a hand-written class (``KiroHarness``, ``KasHarness``,
``CodexHarness``) because each answers its seams with judgement a table cannot
hold. :class:`DescriptorHarness` is the opposite shape: it is built from a
:class:`~kiro_crew.acp.harness.descriptor.HarnessDescriptor` and answers every seam
from that data, so an ACP host an operator already runs can be served with no
per-host code branch.

Posture: a GENERIC, plain-ACP host, modelled on ``codex.py`` rather than on the
kiro family. A descriptor names no ``_kiro.dev`` vocabulary and registers no
agents over the wire, so the defaults here are the plain-ACP ones: standard
``session/update`` notifications, an ordinary ``session/cancel`` teardown, empty
session extras, no host-answered methods. What the descriptor DOES carry moves
onto the seam that reads it:

* ``resolve_spawn`` renders the descriptor's argv template after resolving its
  executable, and -- for a ``session_config``-routed descriptor -- resolves the
  same routing-keyed credential mask ``codex.py`` resolves, refusing a tier that
  would drop it.
* ``apply_spawn_env`` always strips kiro-cli's API key: a foreign binary must
  never receive it.
* ``session_mcp_servers`` is a transform whose SHAPE the descriptor's
  ``mcp_delivery`` chooses: ``agent_file`` passes the caller's array through
  unchanged, ``session_array`` narrows it against the transports the handshake
  advertised (``drop_unadvertised_transports``). It never ADDS.
* ``verifies_agent_activation`` is True only for an ``agent_spec``-routed
  descriptor that puts an agent in its argv.

The remaining seams inherit :class:`MembershipHarness`'s membership lookups. A
registered descriptor id is in none of the frozen ``ACP_BACKENDS_*`` vocabulary
sets, so it takes the default branch at every gate that reads one. Admitting a
harness to a capability is a code-reviewed edit to the named frozen set
(harness-parity H6/H7).

A descriptor never names code (the schema has no ``adapter`` key), so this class
is the ONLY code a config-authored backend runs. It carries the descriptor as its
one piece of instance state, because the runtime resolves a harness by backend id
and the id alone cannot reconstruct the argv template or the routing.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from kiro_crew.acp.harness._common import MembershipHarness
from kiro_crew.acp.harness.base import (
    NotificationAliases,
    SessionExtras,
    SpawnContext,
    SpawnPlan,
    TeardownPolicy,
)
from kiro_crew.acp.harness.descriptor import (
    MCP_DELIVERY_SESSION_ARRAY,
    HarnessDescriptor,
    render_argv,
)
from kiro_crew.acp.types import ACP_CLIENT_CAPABILITIES, METHOD_CANCEL, METHOD_SESSION_UPDATE

__all__ = ["DescriptorHarness"]


class DescriptorHarness(MembershipHarness):
    """A generic ACP host, built from a :class:`HarnessDescriptor`."""

    def __init__(self, descriptor: HarnessDescriptor) -> None:
        # Frozen, so a session bound to it cannot have its argv or routing edited
        # underneath it.
        self._descriptor = descriptor
        self.backend = descriptor.id

    @property
    def descriptor(self) -> HarnessDescriptor:
        """The descriptor this harness was built from."""
        return self._descriptor

    # ── Seam 1: spawn ──

    async def resolve_spawn(self, ctx: SpawnContext) -> SpawnPlan:
        """Render the descriptor's argv, plus the mask an enforced descriptor needs.

        The executable is resolved once, to an absolute path, and that path is what
        ``{executable}`` renders to -- so the file found is the file that execs, not
        a PATH name re-resolved at exec. A bare name that does not resolve aborts
        the spawn with a message naming the directories searched.

        For a ``session_config``-routed descriptor the credential mask is resolved
        here and a refusal aborts the spawn: such a host's privileged tools ask over
        the session config option, so ACP cannot make it ask about a passive read
        and the OS-boundary mask is the compensating control. The mask helper
        re-checks ``tool_gate.is_enforced`` itself, so an ``agent_spec`` descriptor
        gets an empty mask with no identity test here.
        """
        from kiro_crew.acp import client as client_mod
        from kiro_crew.acp.harness.codex import resolve_spawn_masks
        from kiro_crew.acp.session_handle import AcpRuntimeError

        exe, search_path = await asyncio.to_thread(
            client_mod.resolve_descriptor_executable, self._descriptor.executable
        )
        if not exe:
            raise AcpRuntimeError(
                client_mod.descriptor_executable_not_found_message(
                    self._descriptor.id, self._descriptor.executable, search_path
                )
            )
        argv = render_argv(
            self._descriptor,
            executable=exe,
            agent=ctx.agent,
            model=ctx.model or "",
            workdir=str(ctx.work_dir) if ctx.work_dir is not None else "",
        )
        hidden, expose = await resolve_spawn_masks(ctx.sandbox_mode, backend=self.backend)
        return SpawnPlan(argv=argv, extra_hidden_dirs=hidden, extra_expose_files=expose)

    def apply_spawn_env(
        self,
        env: dict[str, str],
        *,
        spawned_binary: str | None = None,
        cli_owned_auth: bool = False,
    ) -> None:
        """Take kiro-cli's API key OUT of the child's environment.

        A descriptor host is a foreign binary; it must never receive kiro-cli's
        model credential. The same positive action ``codex.py`` takes.
        """
        from kiro_crew.config import loader as loader_mod

        loader_mod.strip_kiro_cli_api_key(env)

    @property
    def min_sandbox_tier(self) -> str:
        """This descriptor's own ``min_sandbox``, or empty when it declared none.

        A pass-through: the value is validated to a tightening tier at
        registration, and the one clamp lives in the runtime so a harness cannot
        implement its own comparison and get the direction wrong.
        """
        return self._descriptor.min_sandbox

    @property
    def verifies_agent_activation(self) -> bool:
        """True iff an agent was actually selected at spawn for a later check to confirm.

        The descriptor must be ``agent_spec``-routed and carry an ``agent_args``
        block -- the argv fragment that puts the agent name on the command line.
        Read through the routing TABLE rather than a stored copy, so this harness
        is not a second declaration of routing free to disagree with the drivers.
        """
        from kiro_crew.agent_sdk.backends import Routing, routing_for

        return routing_for(self.backend) is Routing.AGENT_SPEC and bool(self._descriptor.agent_args)

    # ── Seam 2: initialize ──

    @property
    def protocol_version(self) -> Any:
        """Plain ACP v1, as an integer -- the codex/claude spelling."""
        return 1

    @property
    def client_capabilities(self) -> dict[str, Any]:
        """The shared plain-ACP client capabilities, as codex sends them."""
        return ACP_CLIENT_CAPABILITIES

    # ── Seam 3: session/new and session/load extras ──

    async def session_extras(
        self,
        agent: str,
        *,
        work_dir: str | Path | None,
        mcp_gateway_overlay: Any = None,
        member_dispatch: bool = False,
        crew_panel: bool = False,
        session_key: str = "",
    ) -> SessionExtras:
        """Empty. A descriptor host registers no agents over the wire."""
        return SessionExtras()

    def session_mcp_servers(
        self,
        requested: list[dict[str, Any]],
        *,
        agent_capabilities: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """A TRANSFORM whose shape the descriptor's ``mcp_delivery`` chooses.

        ``agent_file`` (the default): passthrough -- the host reads its MCP servers
        from its own config channel, so the caller's list goes through unchanged.

        ``session_array``: narrowing against the transports THIS session's
        handshake advertised, exactly as ``codex.py`` narrows. An unknown handshake
        passes the array through untouched: empty means "nothing is known", never
        "nothing is supported".
        """
        if self._descriptor.mcp_delivery != MCP_DELIVERY_SESSION_ARRAY:
            return requested
        advertised = agent_capabilities.get("mcpCapabilities")
        if not isinstance(advertised, dict) or not advertised:
            return requested
        from kiro_crew.providers.mirrors.codex import drop_unadvertised_transports

        return drop_unadvertised_transports(list(requested), dict(advertised))

    # ── Seam 4: inbound requests the host answers ──

    @property
    def host_answered_methods(self) -> tuple[str, ...]:
        """None. A descriptor host asks Crew for nothing at the connection level."""
        return ()

    async def answer_request(self, method: str) -> dict[str, Any]:
        """Never called: :attr:`host_answered_methods` is empty."""
        raise NotImplementedError(
            f"descriptor harness {self.backend!r} answers no inbound request, "
            f"including {method!r}"
        )

    # ── Seam 5: notification aliases ──

    @property
    def notification_aliases(self) -> NotificationAliases:
        """Plain ACP, with no forked spellings: ``session/update`` only."""
        return NotificationAliases(session_update=(METHOD_SESSION_UPDATE,))

    # ── Seam 6: teardown ──

    @property
    def teardown(self) -> TeardownPolicy:
        """An ordinary ``session/cancel`` notification; the caller then drops the session.

        A generic ACP host has no delete verb of its own, and the kiro-family
        verbs would draw a method-not-found. ``session/cancel`` carries no id and
        the host answers nothing, so it is a NOTIFICATION.
        """
        return TeardownPolicy(method=METHOD_CANCEL, notification=True)
