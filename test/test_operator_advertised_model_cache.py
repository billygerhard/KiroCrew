"""A config-registered operator backend feeds the cross-session model cache.

The reported symptom: an operator backend declared ``model_source:
"acp_advertised"`` in ``harnesses.json`` (Antigravity/AGY) answered a real
``session/new``, named its models, served a turn on one -- and the dashboard's
model picker offered nothing but ``auto``.

The cause was TWO gates on the same builtin-only frozenset, not a parse. They had
to be opened together, which is why this file pins both:

1. :func:`models_from_config_options` refused to fold the harness's ``model`` select
   into a ``models`` envelope, so :meth:`AcpClient._capture_available_models`
   returned before recording anything -- neither on the session nor anywhere else.
2. Even once captured, the cross-session cache write ``/api/models`` reads is gated
   on the same question, asked a second time.

``ACP_BACKENDS_ADVERTISED_MODEL_SELECTION`` is built from module-level constants, so
it is complete at import -- before any config-authored descriptor is registered -- and
a backend named only in ``harnesses.json`` is structurally absent from it. One shared
predicate, :func:`uses_advertised_model_selection`, now answers for both gates, so
they cannot drift apart again.

The neighbouring answers are pinned too, so the widening cannot become a blanket
"every operator backend folds its select".
"""

from __future__ import annotations

import pytest

from kiro_crew import model_registry
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.harness import operator_registry as reg
from kiro_crew.acp.session_handle import models_from_config_options
from kiro_crew.acp.types import ACP_BACKEND_CLAUDE
from kiro_crew.agent_sdk import backends as sdk_backends

#: A minimal ``session/new`` carrying a ``model`` select, the shape an
#: ``acp_advertised`` harness uses. The ids are deliberately absent from every static
#: registry -- they could only have come from the harness itself.
AGY_IDS = ["gemini-3.7-flash", "gemini-3.7-pro"]
AGY_SESSION_NEW = {
    "sessionId": "agy-sess-1",
    "configOptions": [
        {
            "id": "model",
            "type": "select",
            "currentValue": AGY_IDS[0],
            "options": [{"value": v, "name": v, "description": ""} for v in AGY_IDS],
        }
    ],
}

#: A config-registered backend id, not a member of the builtin set -- the whole point.
OPERATOR_ID = "agy"


@pytest.fixture(autouse=True)
def _registered_operator_backend(monkeypatch):
    """Stand in for a ``harnesses.json`` descriptor at the point of registration.

    Two globals are what registration actually moves, so both are moved here:
    ``_REGISTERED_MODEL_NAMESPACE`` (the descriptor's own cache bucket) and the
    registry lookup the predicate asks.
    """
    monkeypatch.setitem(sdk_backends._REGISTERED_MODEL_NAMESPACE, OPERATOR_ID, OPERATOR_ID)
    monkeypatch.setattr(reg, "operator_backend_models", lambda _id: ("acp_advertised", ()))


@pytest.fixture(autouse=True)
def _cold_advertised_cache(monkeypatch):
    """Start from an empty cross-session cache.

    ``_ADVERTISED_MODELS`` is a module global other tests on the same xdist worker
    feed, and these assert on which BUCKET was written.
    """
    monkeypatch.setattr(model_registry, "_ADVERTISED_MODELS", {})
    monkeypatch.setattr(model_registry, "persist_advertised_models", lambda: None)


def _client(backend: str) -> AcpClient:
    client = AcpClient(work_dir=None, acp_backend=backend)
    client._session_id = "agy-sess-1"
    client._model = ""
    return client


def _static_descriptor(monkeypatch) -> None:
    monkeypatch.setattr(
        reg, "operator_backend_models", lambda _id: ("static", ("claude-sonnet-4-6",))
    )


# ── Why the set alone could never have been the gate ──


def test_a_config_registered_backend_is_structurally_absent_from_the_builtin_set():
    """Asserted so the set is never quietly widened to paper over the real fix: it is
    built from module constants, so a backend that exists only in ``harnesses.json``
    cannot be in it, at any import order."""
    assert OPERATOR_ID not in sdk_backends.ACP_BACKENDS_ADVERTISED_MODEL_SELECTION


def test_the_predicate_answers_for_a_registered_advertised_backend():
    assert sdk_backends.uses_advertised_model_selection(OPERATOR_ID) is True
    assert sdk_backends.uses_advertised_model_selection(ACP_BACKEND_CLAUDE) is True


# ── Gate 1: the configOptions fold, which fired first and returned None ──


def test_the_model_select_folds_into_a_models_envelope_for_an_advertised_backend():
    """Gate 1. Before the fix this returned None for a config-registered id, so
    ``_capture_available_models`` bailed before recording anything."""
    envelope = models_from_config_options(AGY_SESSION_NEW, OPERATOR_ID)

    assert envelope is not None
    assert [m["modelId"] for m in envelope["availableModels"]] == AGY_IDS
    assert envelope["currentModelId"] == AGY_IDS[0]


def test_a_host_outside_the_predicate_still_gets_no_synthesized_envelope(monkeypatch):
    """Harness-parity H6 survives: a non-member keeps whatever the static registry
    gave it, rather than having a select reinterpreted as its vocabulary."""
    monkeypatch.setattr(reg, "operator_backend_models", lambda _id: None)

    assert models_from_config_options(AGY_SESSION_NEW, "some-unregistered-backend") is None


def test_a_static_operator_backend_does_not_fold_its_select(monkeypatch):
    """A ``static`` descriptor's catalog is declared, not advertised. Folding it in
    would let the live select contradict the declared list."""
    _static_descriptor(monkeypatch)

    assert models_from_config_options(AGY_SESSION_NEW, OPERATOR_ID) is None


# ── Gate 2: the cross-session cache /api/models reads ──


def test_an_advertised_operator_backend_feeds_its_own_cache_bucket():
    """The reported bug end to end: session/new names two models, and the picker now
    has a real list to read."""
    client = _client(OPERATOR_ID)

    client._capture_available_models(AGY_SESSION_NEW)

    # Written to the backend's own bucket, never the shared ``acp`` one a kiro-family
    # harness reads -- the reason a registered id gets its own namespace.
    assert model_registry.advertised_models(OPERATOR_ID) == AGY_IDS
    assert model_registry.advertised_models("acp") == []
    # The session half, which never broke, still holds them.
    assert [m["modelId"] for m in client.available_models()] == AGY_IDS


def test_a_static_operator_backend_does_not_feed_the_advertised_cache(monkeypatch):
    """The cache is the ADVERTISED bucket; a declared catalog has its own path."""
    _static_descriptor(monkeypatch)
    client = _client(OPERATOR_ID)

    client._capture_available_models(AGY_SESSION_NEW)

    assert model_registry.advertised_models(OPERATOR_ID) == []


def test_a_builtin_member_is_unchanged():
    """Regression: the widening must not disturb a frozenset member. ``claude`` is
    the backend id; its cache bucket is ``claude_code``."""
    client = _client(ACP_BACKEND_CLAUDE)

    client._capture_available_models(AGY_SESSION_NEW)

    assert model_registry.advertised_models("claude_code") == AGY_IDS


def test_an_unregistered_non_member_still_excluded_end_to_end(monkeypatch):
    """The negative case through the same call, not just the predicate: no descriptor
    means the frozenset alone decides, and it says no."""
    monkeypatch.setattr(reg, "operator_backend_models", lambda _id: None)
    client = _client("goose-not-a-member")

    client._capture_available_models(AGY_SESSION_NEW)

    assert model_registry.advertised_models("goose-not-a-member") == []


# ── The cold-cache case the widening must not break ──


def test_an_empty_cache_folds_nothing_and_leaves_the_model_id_alone():
    """``resolve_wire_model_id`` is what the other two readers of the predicate do.
    On a cold cache it must be a no-op, or enabling the gate would corrupt a model
    id the harness never advertised."""
    assert model_registry.resolve_wire_model_id("gemini-3.7-flash", OPERATOR_ID) == (
        "gemini-3.7-flash"
    )


# ── Gate 3: Live active session provider resolution & capabilities ──


def test_capabilities_for_advertised_operator_backend_resolves_advertised_list():
    from kiro_crew.agent_sdk.capabilities import capabilities_for

    caps = capabilities_for(OPERATOR_ID)
    assert caps.resolves_model_from_advertised_list is True
    assert caps.model_id_namespace == OPERATOR_ID


def test_capabilities_for_static_operator_backend_does_not_resolve_advertised_list(monkeypatch):
    from kiro_crew.agent_sdk.capabilities import capabilities_for

    _static_descriptor(monkeypatch)
    caps = capabilities_for(OPERATOR_ID)
    assert caps.resolves_model_from_advertised_list is False


def test_advertised_backend_models_reads_live_active_session():
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from kiro_crew.agent_sdk.capabilities import capabilities_for
    from kiro_crew.dashboard.handlers.agents import _advertised_backend_models
    from kiro_crew.providers.acp import AcpProvider

    prov = MagicMock(spec=AcpProvider)
    prov.capabilities = capabilities_for(OPERATOR_ID)
    prov.available_models.return_value = [
        {
            "modelId": "gemini-3.7-flash-high",
            "name": "Gemini 3.7 Flash High",
            "description": "Fast",
        },
        {"modelId": "gemini-pro-agent", "name": "Gemini Pro Agent", "description": "Reasoning"},
    ]

    sessions = SimpleNamespace(active_providers=lambda: [prov])
    req = MagicMock()
    req.app.__getitem__.return_value = SimpleNamespace(sessions=sessions)

    models = _advertised_backend_models(req, OPERATOR_ID)
    assert models[0]["model_name"] == "auto"
    assert [m["model_name"] for m in models[1:]] == ["gemini-3.7-flash-high", "gemini-pro-agent"]
    assert [m["display_name"] for m in models[1:]] == ["Gemini 3.7 Flash High", "Gemini Pro Agent"]
