"""Model choices for a ``harnesses.json`` backend that declares a static list.

An ``acp_advertised`` descriptor (the Claude seats) is served by the advertised
path in :mod:`kiro_crew.dashboard.handlers.agents` through its own namespace. A
``static`` descriptor (strands-bedrock) advertises nothing, so without this its
picker fell to kiro-cli's catalog, whose ids that host refuses. The ids it
declared in ``harnesses.json`` are the only ones it accepts, so those are the rows.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from kiro_crew import model_registry
from kiro_crew.agent_sdk.backends_registered import registered_model_source

if TYPE_CHECKING:
    from aiohttp import web

#: The model source a descriptor declares when it lists its ids itself.
MODEL_SOURCE_STATIC = "static"


def static_descriptor_models(backend: str) -> Optional[list[dict]]:
    """``auto`` plus *backend*'s declared ids, or ``None`` when it declares no static list."""
    source = registered_model_source(backend)
    if source is None or source[0] != MODEL_SOURCE_STATIC:
        return None
    rows: list[dict] = [
        {"model_name": "auto", "display_name": "Auto", "description": "Backend default"}
    ]
    rows += [
        {"model_name": model, "display_name": model, "description": ""}
        for model in source[1]
        if model and model != "auto"
    ]
    for entry in rows:
        entry["context_window"] = (
            model_registry.model_window(entry["model_name"])
            or model_registry.REFERENCE_WINDOW_TOKENS
        )
    return rows


__all__ = ["MODEL_SOURCE_STATIC", "static_descriptor_models"]


def requested_models_backend(request: web.Request) -> Optional[str]:
    """The runtime backend ``GET /api/models?backend=<id>`` names, when selectable.

    The id is a stored spelling (``"kiro"``, ``"claude-seat1"``), as a chat's pick
    and the ``/api/acp-backends`` rows carry it. Anything this build would not
    start returns ``None``, so the caller keeps the configured backend.
    """
    from kiro_crew.acp_backends import backend_from_stored, selectable_backends

    raw = request.query.get("backend")
    if not raw:
        return None
    backend = backend_from_stored(raw)
    if isinstance(backend, str) and backend in selectable_backends():
        return backend
    return None
