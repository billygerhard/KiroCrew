"""Register the operator's ``harnesses.json`` descriptors as backends.

Called once per gateway start by :mod:`kiro_crew.operator_backends`, after the
platform booted. Never raises: a descriptor that fails to parse or register costs
its own row (kept in :func:`invalid_operator_harnesses`) and the gateway still
boots on the builtin harnesses.

For each valid descriptor:

1. ``register_known_backend`` -- the id becomes spellable, routed, labelled,
   policy-nameable, own-namespaced and served on ``AcpRuntime``.
2. ``register_operator_harness`` -- ``harness_for`` resolves it to a
   ``DescriptorHarness``.
3. ``register_selectable_backend`` -- only for a descriptor with a recognized
   routing. ``harnesses.json`` is a file the agent cannot write, so the
   operator's routing declaration is the trust decision; the
   ``agent_backend`` governance scope is re-applied afterwards by the caller.
"""

from __future__ import annotations

import logging
from typing import Optional

from kiro_crew.acp.harness import operator_descriptor, register_operator_harness
from kiro_crew.acp.harness.descriptor import (
    MCP_DELIVERY_SESSION_ARRAY,
    ROUTING_AGENT_SPEC,
    ROUTING_SESSION_CONFIG,
    HarnessDescriptor,
    load_operator_descriptors,
)
from kiro_crew.agent_sdk.backends import Routing, register_selectable_backend
from kiro_crew.agent_sdk.capability_inheritance import (
    record_capabilities_from,
    record_declared_capabilities,
)
from kiro_crew.agent_sdk.operator_backend_registry import (
    is_registered_backend,
    provider_label_for,
    register_known_backend,
)

logger = logging.getLogger(__name__)

#: Entries that failed to parse or register, id -> reasons.
_INVALID: dict[str, list[str]] = {}

#: Valid descriptors registered known but not selectable, id -> reason.
_UNSELECTABLE: dict[str, str] = {}


def _routing_enum_for(descriptor: HarnessDescriptor) -> Optional[Routing]:
    if descriptor.routing == ROUTING_AGENT_SPEC:
        return Routing.AGENT_SPEC
    if descriptor.routing == ROUTING_SESSION_CONFIG:
        return Routing.SESSION_CONFIG
    return None


def _label_taken(label: str) -> bool:
    """Whether a builtin or an earlier registration already persists under *label*.

    The label is what a session is persisted, compared and cleaned up under, so
    two backends sharing one would file each other's sessions.
    """
    from kiro_crew.acp.types import PROVIDER_LABEL_BY_BACKEND
    from kiro_crew.agent_sdk.operator_backend_registry import registered_backend_ids

    if label in PROVIDER_LABEL_BY_BACKEND.values():
        return True
    return any(provider_label_for(b) == label for b in registered_backend_ids())


#: ``capabilities_from`` value that opts a descriptor out of any inheritance.
CAPABILITIES_FROM_NONE = "none"

#: ``session_family`` -> the builtin whose adapter owns that native session store.
#: A descriptor sharing claude's store is claude-agent-acp, so it needs no
#: ``capabilities_from`` written by hand.
_FAMILY_CAPABILITY_SOURCE = {"claude-code": "claude"}


def capability_source_for(descriptor: HarnessDescriptor) -> tuple[str, str]:
    """``(builtin id, "")`` the descriptor inherits from, ``("", "")`` for none, or ``("", reason)``.

    The explicit ``capabilities_from`` wins over the ``session_family`` inference.
    """
    from kiro_crew.agent_sdk.backends import ACP_BACKENDS_BUILTIN, backend_from_stored

    declared = descriptor.capabilities_from
    if declared == CAPABILITIES_FROM_NONE:
        return "", ""
    value = declared or _FAMILY_CAPABILITY_SOURCE.get(descriptor.session_family, "")
    if not value:
        return "", ""
    source = backend_from_stored(value)
    if not isinstance(source, str) or source not in ACP_BACKENDS_BUILTIN:
        return "", (
            f"capabilities_from {value!r} is not a builtin backend; name one this "
            f"build ships (e.g. claude) or {CAPABILITIES_FROM_NONE!r}"
        )
    return source, ""


def inferred_capabilities(descriptor: HarnessDescriptor) -> tuple[str, ...]:
    """The capability names a descriptor's ``mcp_delivery`` alone establishes.

    Both deliveries hand the host Crew's MCP array on ``session/new``: ``agent_file``
    passes it through, ``session_array`` narrows it to advertised transports. Only
    ``session_array`` also declares the host READS that array, which is where the
    member session-control server is appended.
    """
    if descriptor.mcp_delivery == MCP_DELIVERY_SESSION_ARRAY:
        return ("crew_tools", "member_tools")
    return ("crew_tools",)


def _record_capabilities(descriptor: HarnessDescriptor, capability_source: str) -> None:
    """Apply the first of: the explicit list, ``capabilities_from``, the delivery inference."""
    if descriptor.capabilities is not None:
        record_declared_capabilities(descriptor.id, descriptor.capabilities)
    elif capability_source:
        record_capabilities_from(descriptor.id, capability_source)
    else:
        record_declared_capabilities(descriptor.id, inferred_capabilities(descriptor))


def load_and_register_operator_descriptors(*, path=None) -> list[str]:
    """Read ``harnesses.json`` and register every valid descriptor. Returns the ids registered.

    Idempotent: an id this loader already registered is skipped.
    """
    valid, invalid = load_operator_descriptors(path=path)
    for harness_id, reasons in invalid:
        _INVALID[harness_id] = list(reasons)
        logger.warning("operator harness %r ignored: %s", harness_id, "; ".join(reasons))

    registered: list[str] = []
    for descriptor in valid:
        backend_id = descriptor.id
        if operator_descriptor(backend_id) is not None:
            continue
        if _label_taken(descriptor.label):
            _INVALID[backend_id] = [
                "display_name is already the provider label of another backend; "
                "sessions are persisted by label, so two backends cannot share one"
            ]
            continue
        capability_source, source_reason = capability_source_for(descriptor)
        if source_reason:
            _INVALID[backend_id] = [source_reason]
            continue
        routing = _routing_enum_for(descriptor)
        permission_config = None
        if routing is Routing.SESSION_CONFIG and descriptor.permission_config is not None:
            permission_config = (
                descriptor.permission_config.option,
                descriptor.permission_config.value,
            )
        try:
            register_known_backend(
                backend_id,
                label=descriptor.label,
                routing=routing if routing is not None else Routing.UNVERIFIED,
                permission_config=permission_config,
                model_namespace=backend_id,
                model_source=descriptor.model_source,
                models=descriptor.models,
                session_family=descriptor.session_family,
                runtime=True,
                session_array=descriptor.mcp_delivery == MCP_DELIVERY_SESSION_ARRAY,
            )
            register_operator_harness(descriptor)
            _record_capabilities(descriptor, capability_source)
        except ValueError as exc:
            logger.warning("operator harness %r could not be registered: %s", backend_id, exc)
            _INVALID[backend_id] = [f"could not be registered: {exc}"]
            continue
        registered.append(backend_id)
        if routing is None:
            _UNSELECTABLE[backend_id] = (
                "no recognized routing declared, so nothing establishes that its "
                "tool calls reach the host permission gate"
            )
            continue
        try:
            register_selectable_backend(backend_id)
        except ValueError as exc:
            _UNSELECTABLE[backend_id] = str(exc)
            logger.warning("operator harness %r is known but not selectable: %s", backend_id, exc)
    if registered:
        logger.info("registered operator harnesses: %s", ", ".join(registered))
    return registered


def invalid_operator_harnesses() -> dict[str, list[str]]:
    """Entries that failed to parse or register, id -> reasons (a copy)."""
    return {k: list(v) for k, v in _INVALID.items()}


def unselectable_operator_harnesses() -> dict[str, str]:
    """Valid descriptors known but not selectable, id -> reason (a copy)."""
    return dict(_UNSELECTABLE)


def registered_operator_descriptor(backend_id: str) -> Optional[HarnessDescriptor]:
    """The registered descriptor for *backend_id*, or ``None`` for a builtin/unknown id."""
    if not is_registered_backend(backend_id):
        return None
    return operator_descriptor(backend_id)


def _reset_operator_diagnostics() -> None:
    """TEST-ONLY: clear the diagnostic rows."""
    _INVALID.clear()
    _UNSELECTABLE.clear()


__all__ = [
    "CAPABILITIES_FROM_NONE",
    "capability_source_for",
    "inferred_capabilities",
    "invalid_operator_harnesses",
    "load_and_register_operator_descriptors",
    "registered_operator_descriptor",
    "unselectable_operator_harnesses",
]
