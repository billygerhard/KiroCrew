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
from typing import Optional


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


__all__ = [
    "invalid_operator_harnesses",
    "load_and_register_operator_descriptors",
    "operator_backend_models",
    "unselectable_operator_harnesses",
]
