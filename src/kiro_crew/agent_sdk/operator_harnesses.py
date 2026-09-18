"""Operator-defined harnesses through the SDK boundary.

Application code -- the platform bootstrap that registers ``harnesses.json`` at
boot, and the dashboard handler that lists what the registry made of each
descriptor -- must not import the ACP layer directly
(``scripts/check_agent_sdk_boundary.py``). This module is the one crossing for
the operator-harness surface: three names, each a thin call into
:mod:`kiro_crew.acp.harness.operator_registry`, imported at call time so this
module stays as cheap to import as the rest of the SDK facade and adds no
module-scope edge into ``kiro_crew.acp`` (the registry module imports
``kiro_crew.acp.harness``, which in turn reaches the descriptor loader; pulling
that in at SDK import time would re-open the cycle the leaf
:mod:`kiro_crew.agent_sdk.backends` exists to avoid).
"""

from __future__ import annotations

import os
from typing import Any, Optional


def load_and_register_operator_descriptors(
    *, path: "Optional[os.PathLike[str] | str]" = None
) -> None:
    """Read ``harnesses.json`` and register every valid, routable descriptor.

    Best-effort by contract: an unreadable file leaves the builtin harnesses
    serving. Invalid and unselectable descriptors are recorded for the
    diagnostics readers below rather than raised.
    """
    from kiro_crew.acp.harness.operator_registry import (
        load_and_register_operator_descriptors as _load,
    )

    _load(path=path)


def invalid_operator_harnesses() -> dict[str, list[str]]:
    """Descriptor id -> validation errors, for every descriptor that failed to load."""
    from kiro_crew.acp.harness.operator_registry import invalid_operator_harnesses as _invalid

    return _invalid()


def unselectable_operator_harnesses() -> dict[str, str]:
    """Descriptor id -> reason, for every valid descriptor the registry refused to
    make selectable (no honest permission routing declared)."""
    from kiro_crew.acp.harness.operator_registry import (
        unselectable_operator_harnesses as _unselectable,
    )

    return _unselectable()


def operator_backend_models(backend_id: str) -> "list[str] | None":
    """The model-name list a registered operator backend should offer, or
    ``None`` for a builtin / unknown id.

    Resolves the descriptor's ``model_source`` here so the caller (the
    ``/api/models`` endpoint) stays out of the ACP layer: a ``static`` descriptor
    yields its declared ``models``; an ``acp_advertised`` one yields what its
    harness advertised on ``session/new`` (the model registry's cross-session
    cache, keyed by the backend's own namespace). See
    :func:`kiro_crew.acp.harness.operator_registry.operator_backend_models`."""
    from kiro_crew.acp.harness.descriptor import MODEL_SOURCE_STATIC
    from kiro_crew.acp.harness.operator_registry import operator_backend_models as _models

    info = _models(backend_id)
    if info is None:
        return None
    source, static_models = info
    if source == MODEL_SOURCE_STATIC:
        return list(static_models)
    from kiro_crew import model_registry
    from kiro_crew.agent_sdk.backends import model_registry_namespace

    return list(model_registry.advertised_models(model_registry_namespace(backend_id)))


def unverified_operator_harnesses() -> frozenset[str]:
    """Ids of registered operator backends whose ONLY missing piece is the
    end-to-end routing attestation. These get a Verify action in Settings."""
    from kiro_crew.acp.harness.operator_registry import unverified_operator_harnesses as _unverified

    return _unverified()


async def verify_operator_backend_routing(backend_id: str, factory: Any) -> dict[str, Any]:
    """Probe *backend_id*'s harness end to end and, on a verified verdict, record
    the attestation and make the backend selectable now.

    *factory* is the production provider factory (``build_provider_factory(cfg)``).
    Returns the verification as a plain dict (``verdict``, ``reason``, ...) plus
    ``selectable`` for the resulting registry state. Raises ``ValueError`` for an
    id that is not a registered operator descriptor -- the caller turns that into
    its own 404, since a builtin has no routing claim to verify.
    """
    from kiro_crew.acp.harness.operator_registry import (
        mark_routing_verified,
        registered_operator_descriptor,
    )
    from kiro_crew.acp.harness.routing_verification import (
        record_attestation,
        verify_routing,
    )
    from kiro_crew.agent_sdk.backends import selectable_backends

    descriptor = registered_operator_descriptor(backend_id)
    if descriptor is None:
        raise ValueError(f"{backend_id!r} is not a registered operator backend")
    result = await verify_routing(descriptor, factory)
    if result.verified:
        record_attestation(
            descriptor,
            mechanism=descriptor.routing,
            evidence={
                "permission_requests": result.permission_requests,
                "elapsed_secs": round(result.elapsed_secs, 2),
            },
        )
        mark_routing_verified(backend_id)
    payload = result.as_dict()
    payload["selectable"] = backend_id in selectable_backends()
    return payload


__all__ = [
    "invalid_operator_harnesses",
    "load_and_register_operator_descriptors",
    "operator_backend_models",
    "unselectable_operator_harnesses",
    "unverified_operator_harnesses",
    "verify_operator_backend_routing",
]
