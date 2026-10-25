"""The generic session-array projection for an OPERATOR-declared harness.

Every mirror in this folder is keyed by a backend id frozen into
:data:`~kiro_crew.providers.mirrors.registry.MIRRORS` at import time, and
``test_the_mirror_declares_the_backend_it_serves`` holds each key to
``ACP_BACKENDS_KNOWN``. That is correct for the builtins and impossible for a
config-authored harness: ``harnesses.json`` is read at load, so an operator can
name a backend this build has never heard of, and there is no id to hardcode.

The gap that produced is not hypothetical. A descriptor may declare
``mcp_delivery: "session_array"``, which is a PROMISE that the host learns its
MCP servers only from the ``session/new`` array
(:meth:`~kiro_crew.acp.harness.operator.DescriptorHarness.session_mcp_servers`).
Nothing honoured it: ``AcpRuntime`` chose between "mirrored" and "pooled stubs"
by ``has_mirror(backend)``, a frozen-table lookup, so such a harness fell to the
pooled branch -- and with ``mcp_gateway.enabled`` false that branch yields ZERO
servers. The session then came up with no tools, every ``@server`` ref in its
agent spec named nothing, and the only sign was one ``mcp_ref_guard`` warning.

So this module supplies the projection for that case WITHOUT a per-operator id:
one object, built from the descriptor, that serves every declared
``session_array`` harness. It is deliberately not a registered mirror -- there is
no id to register, and a subclass per operator would be a mirror per harness in
a shipped package.

**It delegates to :func:`~kiro_crew.providers.mirrors.opencode.opencode_projection`
rather than restating it, because that projection is the strict one and the
strictness is what an unknown transport needs.** Every rule it applies is
fail-CLOSED for a host whose capabilities Crew has not measured:

* one owner for BOTH halves of the array, so a broker stub cannot re-add a server
  the projection withheld (the stub is the unrestricted server);
* a server the spec narrows per tool is withheld WHOLE, because this transport is
  assumed to offer no per-call deny channel -- an availability cost, stated and
  logged, rather than a tool the operator switched off still being reachable;
* Crew's own identity-bound managed servers are withheld rather than mounted
  credential-less, and the control plane carries this session's identity on the
  ELEMENT, because the identity may ride only an element Crew itself derived;
* the ``type: "stdio"`` tag is dropped. ACP v1 makes stdio the UNTAGGED member, so
  the tag carries no information for a conforming host, and an adapter that
  discriminates on its presence would route the element down a remote branch.

The descriptor's own transport narrowing still runs afterwards, on the array this
builds, against the capabilities the host actually advertised at handshake -- so
the two compose, each answering a question the other cannot.
"""

from __future__ import annotations

from typing import Any, Collection, Mapping

from kiro_crew.providers.mirrors.base import SessionProjection
from kiro_crew.providers.mirrors.opencode import opencode_projection

__all__ = ["DescriptorArrayProjection", "descriptor_array_projection"]


def descriptor_array_projection(
    agent: str | None,
    *,
    stub_server_names: Collection[str] = (),
    stub_elements: Collection[Mapping[str, Any]] = (),
    work_dir: object = None,
    session_key: str = "",
    channel_id: str = "",
    session_token: str = "",
) -> SessionProjection:
    """The array for an operator-declared ``session_array`` harness.

    :func:`~kiro_crew.providers.mirrors.opencode.opencode_projection` verbatim, and
    the whole of the implementation, because an unmeasured transport wants that
    module's rules rather than a looser set written for the fact that it is
    unknown. What this adds is the ADDRESS: a function an operator-declared backend
    can be given without a shipped id naming it.
    """
    return opencode_projection(
        agent,
        stub_server_names=stub_server_names,
        stub_elements=stub_elements,
        work_dir=work_dir,
        session_key=session_key,
        channel_id=channel_id,
        session_token=session_token,
    )


class DescriptorArrayProjection:
    """The mirror-shaped face of :func:`descriptor_array_projection`.

    Exists so ``AcpRuntime`` can hand the descriptor path the SAME object shape a
    registered mirror presents -- ``.session_projection(...)`` and nothing else --
    rather than growing a second composition branch for config-authored backends.
    It is deliberately NOT an :class:`~kiro_crew.providers.mirrors.base.AgentConfigMirror`
    subclass: ``rulings()`` is abstract precisely so a new backend cannot inherit
    silence, and there is nothing to rule on per-operator here -- the transport
    questions are answered by the descriptor's own fields and by the projection
    above, not by a per-harness opinion.
    """

    __slots__ = ("backend",)

    def __init__(self, backend: str) -> None:
        self.backend = backend

    def session_projection(
        self,
        agent: str | None,
        *,
        stub_server_names: Collection[str] = (),
        stub_elements: Collection[Mapping[str, Any]] = (),
        work_dir: object = None,
        session_key: str = "",
        channel_id: str = "",
        session_token: str = "",
        **kwargs: object,
    ) -> SessionProjection:
        """The projection. ``permission_surface_owned`` arrives in ``kwargs`` and is
        IGNORED, which is the documented behaviour for anything outside claude's
        class: this runtime authors no native permission file, so there is no
        pre-approval channel to fail closed on."""
        del kwargs
        return descriptor_array_projection(
            agent,
            stub_server_names=stub_server_names,
            stub_elements=stub_elements,
            work_dir=work_dir,
            session_key=session_key,
            channel_id=channel_id,
            session_token=session_token,
        )
