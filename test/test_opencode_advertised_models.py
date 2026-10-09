"""The opencode backend is offered, and is held to, ITS OWN model list.

The reported symptom was a dashboard whose model picker listed kiro's catalog
(``claude-opus-…``) while ``agent.acp_backend`` was set to ``opencode``.

The cause was never in opencode's wiring. Everything it needs was already in
place -- it IS an ``ACP_BACKENDS_ADVERTISED_MODEL_SELECTION`` member, it has its own
``opencode`` registry namespace, its ``session/new`` ``model`` select is captured
into that namespace's bucket of ``provider_models.json``. What was missing was the
one line that connects them: :func:`api_models` dispatched on backend NAMES and
knew only ``claude`` and ``codex``. ``opencode`` fell past both branches onto the
kiro-family ``--list-models`` spawn, so the endpoint answered with kiro ids that
opencode refuses at ``session/set_config_option("model", …)`` -- while the cache
holding the real list sat unread, in the very file the endpoint should have asked.

These tests pin the DISPATCH rather than one backend's rows, so a harness added to
the set later cannot be forgotten the same way: one of them walks the whole set and
requires that none of its members reaches the kiro spawn.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from kiro_crew import model_registry
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.types import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    ACP_BACKEND_OPENCODE,
)
from kiro_crew.agent_sdk import backends as sdk_backends
from kiro_crew.agent_sdk.capabilities import capabilities_for
from kiro_crew.dashboard.handlers import agents

#: The ids opencode's ``model`` select advertises -- ``provider/model`` pairs out of
#: the operator's own provider list, which no static registry names. The three
#: hosted ones are the real shapes from this install's ``provider_models.json``.
OPENCODE_SESSION_NEW = {
    "sessionId": "opencode-sess-1",
    "modes": {"currentModeId": "build", "availableModes": []},
    "configOptions": [
        {
            "id": "model",
            "type": "select",
            "currentValue": "opencode/big-pickle",
            "options": [
                {
                    "value": "opencode/big-pickle",
                    "name": "big-pickle",
                    "description": "Default",
                },
                {"value": "opencode/space-bunny-free", "name": "space-bunny-free"},
                {"value": "ollama/qwen3:8b", "name": "qwen3:8b"},
            ],
        },
    ],
}

OPENCODE_IDS = ["opencode/big-pickle", "opencode/space-bunny-free", "ollama/qwen3:8b"]


@pytest.fixture(autouse=True)
def _cold_advertised_cache(monkeypatch):
    """Every test here starts from an empty cross-session cache.

    ``model_registry._ADVERTISED_MODELS`` is a module global other tests on the
    same xdist worker feed; these tests assert on which BUCKET gets read, so a warm
    one would pass or fail on a neighbour's leftovers.
    """
    monkeypatch.setattr(model_registry, "_ADVERTISED_MODELS", {})
    monkeypatch.setattr(model_registry, "persist_advertised_models", lambda: None)


def _opencode_client(tmp_path, model: str = "") -> AcpClient:
    client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_OPENCODE)
    client._session_id = "opencode-sess-1"
    client._model = model
    return client


def _request(*providers) -> MagicMock:
    state = SimpleNamespace(sessions=SimpleNamespace(active_providers=lambda: list(providers)))
    request = MagicMock()
    request.app = {"state": state}
    return request


def _opencode_provider(tmp_path) -> MagicMock:
    """A provider double wrapping a client that captured a real opencode session/new."""
    client = _opencode_client(tmp_path)
    client._capture_available_models(OPENCODE_SESSION_NEW)
    provider = MagicMock()
    # ``_advertised_cc_models`` selects on the capability record, and a MagicMock's
    # attributes are all truthy -- so hand it the real record.
    provider.capabilities = capabilities_for(ACP_BACKEND_OPENCODE)
    provider.available_models = MagicMock(return_value=client.available_models())
    return provider


def _config(backend: str, model: str = ""):
    return SimpleNamespace(agent=SimpleNamespace(acp_backend=backend, model=model))


def _names(rows: list[dict]) -> list[str]:
    return [r["model_name"] for r in rows]


# ── The capability set: already correct, pinned so it cannot be narrowed ──


def test_opencode_is_an_advertised_model_selection_member() -> None:
    assert ACP_BACKEND_OPENCODE in sdk_backends.ACP_BACKENDS_ADVERTISED_MODEL_SELECTION
    assert capabilities_for(ACP_BACKEND_OPENCODE).resolves_model_from_advertised_list is True


def test_opencode_has_its_own_registry_namespace() -> None:
    """The namespace is also the advertised-cache bucket: ``provider/model`` ids must
    not land in the ``acp`` bucket a kiro-family harness would read back."""
    assert sdk_backends.model_registry_namespace(ACP_BACKEND_OPENCODE) == "opencode"
    assert capabilities_for(ACP_BACKEND_OPENCODE).model_id_namespace == "opencode"
    # The kiro-family answer is untouched (harness-parity H13).
    assert sdk_backends.model_registry_namespace(ACP_BACKEND_KIRO) == "acp"
    assert sdk_backends.model_registry_namespace(ACP_BACKEND_KAS) == "acp"


# ── The capture: the ids are learnable, and land in the right bucket ──


def test_opencode_capture_harvests_the_config_options_model_select(tmp_path) -> None:
    client = _opencode_client(tmp_path)

    client._capture_available_models(OPENCODE_SESSION_NEW)

    assert [m["modelId"] for m in client.available_models()] == OPENCODE_IDS
    assert client._resolved_model_id == "opencode/big-pickle"


def test_opencode_capture_feeds_the_opencode_cache_bucket_only(tmp_path) -> None:
    client = _opencode_client(tmp_path)

    client._capture_available_models(OPENCODE_SESSION_NEW)

    assert model_registry.advertised_models("opencode") == OPENCODE_IDS
    # The whole point of the separate namespace: kiro's bucket is untouched.
    assert model_registry.advertised_models("acp") == []


# ── The picker ──


def test_opencode_picker_lists_the_live_session_advertised_ids(tmp_path) -> None:
    rows = agents._advertised_backend_models(
        _request(_opencode_provider(tmp_path)), ACP_BACKEND_OPENCODE
    )

    assert _names(rows) == ["auto", *OPENCODE_IDS]
    assert rows[1]["display_name"] == "big-pickle"
    assert rows[1]["description"] == "Default"
    assert all(isinstance(r["context_window"], int) and r["context_window"] > 0 for r in rows)


def test_opencode_picker_reads_the_opencode_cache_bucket_when_no_session_is_live() -> None:
    """The reported install: a dashboard restarted after an opencode session, with
    ``provider_models.json`` holding the real list and nothing live."""
    model_registry.refresh_advertised_models("opencode", OPENCODE_IDS)

    rows = agents._advertised_backend_models(_request(), ACP_BACKEND_OPENCODE)

    assert _names(rows) == ["auto", *OPENCODE_IDS]


def test_opencode_picker_never_offers_the_kiro_ids(tmp_path) -> None:
    """The reported symptom, stated as an assertion.

    The kiro bucket holds kiro's catalog. Reading it is what made the picker list
    ``claude-opus-…`` for an opencode session -- ids opencode refuses outright.
    """
    model_registry.refresh_advertised_models("acp", ["claude-opus-5", "claude-sonnet-4.5"])

    rows = agents._advertised_backend_models(_request(), ACP_BACKEND_OPENCODE)

    assert _names(rows) == ["auto"]
    assert not any(r["model_name"].startswith("claude-") for r in rows)


def test_opencode_picker_ignores_a_live_kiro_session(tmp_path) -> None:
    """A kiro session advertises kiro's catalog; a different vocabulary entirely."""
    kiro = MagicMock()
    kiro.capabilities = capabilities_for(ACP_BACKEND_KIRO)
    kiro.available_models = MagicMock(return_value=[{"modelId": "claude-opus-5"}])

    assert _names(agents._advertised_backend_models(_request(kiro), ACP_BACKEND_OPENCODE)) == [
        "auto"
    ]


def test_opencode_picker_ignores_a_claude_session_that_holds_the_same_capability() -> None:
    """claude also resolves from its advertised list, so the capability gate alone
    cannot separate them -- only ``model_id_namespace`` says whose ids these are."""
    claude = MagicMock()
    claude.capabilities = capabilities_for(ACP_BACKEND_CLAUDE)
    claude.available_models = MagicMock(
        return_value=[{"modelId": "claude-opus-5", "name": "Opus 5", "description": ""}]
    )

    assert _names(agents._advertised_backend_models(_request(claude), ACP_BACKEND_OPENCODE)) == [
        "auto"
    ]


def test_opencode_picker_cold_offers_auto_alone() -> None:
    """No session yet and nothing cached: ``auto`` (inherit opencode's default) only.
    The frontend refetches on the first session spawn."""
    assert _names(agents._advertised_backend_models(_request(), ACP_BACKEND_OPENCODE)) == ["auto"]


def test_opencode_picker_keeps_provider_model_ids_verbatim() -> None:
    """The slash is load-bearing: it is how opencode names a model, and folding it
    away (``provider``/``model`` on some other namespace) yields an id it refuses."""
    model_registry.refresh_advertised_models("opencode", OPENCODE_IDS)

    rows = agents._advertised_backend_models(_request(), ACP_BACKEND_OPENCODE)

    assert rows[1]["model_name"] == "opencode/big-pickle"


def test_opencode_picker_scopes_a_foreign_configured_pin_away() -> None:
    """``agent.model`` still says ``claude-sonnet-5`` on a provider that cannot run
    it. It must not become a row -- and not the selected slot either."""
    model_registry.refresh_advertised_models("opencode", OPENCODE_IDS)

    rows = agents._advertised_backend_models(
        _request(), ACP_BACKEND_OPENCODE, configured_default="claude-sonnet-5"
    )

    assert "claude-sonnet-5" not in _names(rows)


# ── The dispatch: the actual regression ──


@pytest.mark.asyncio
async def test_api_models_routes_the_opencode_backend_off_the_kiro_spawn(monkeypatch, tmp_path):
    """The reported bug as a test: opencode never reaches kiro-cli's spawn.

    ``reject_if_kiro_unverified`` is the first thing the kiro branch does, so
    asserting it is never reached is asserting the branch was never entered --
    without spawning anything or resolving a kiro binary.
    """
    monkeypatch.setattr(
        agents.KiroCrewConfig, "load", staticmethod(lambda: _config(ACP_BACKEND_OPENCODE))
    )

    async def _never_reached(*_a, **_k):  # pragma: no cover - the assertion is that it is unreached
        raise AssertionError("opencode must not enter the kiro --list-models branch")

    monkeypatch.setattr(agents, "reject_if_kiro_unverified", _never_reached)

    resp = await agents.api_models(_request(_opencode_provider(tmp_path)))

    assert resp.status == 200
    assert _names(json.loads(resp.body)) == ["auto", *OPENCODE_IDS]


@pytest.mark.asyncio
async def test_api_models_routes_opencode_from_the_cache_when_nothing_is_live(monkeypatch):
    """The reported install's exact state: no live session, cache populated."""
    monkeypatch.setattr(
        agents.KiroCrewConfig, "load", staticmethod(lambda: _config(ACP_BACKEND_OPENCODE))
    )
    model_registry.refresh_advertised_models("opencode", OPENCODE_IDS)

    async def _never_reached(*_a, **_k):  # pragma: no cover - the assertion is that it is unreached
        raise AssertionError("opencode must not enter the kiro --list-models branch")

    monkeypatch.setattr(agents, "reject_if_kiro_unverified", _never_reached)

    resp = await agents.api_models(_request())

    assert resp.status == 200
    assert _names(json.loads(resp.body)) == ["auto", *OPENCODE_IDS]


@pytest.mark.parametrize("backend", sorted(sdk_backends.ACP_BACKENDS_ADVERTISED_MODEL_SELECTION))
@pytest.mark.asyncio
async def test_no_advertised_selection_member_reaches_the_kiro_spawn(monkeypatch, backend):
    """The ratchet that makes the name-by-name bug impossible to reintroduce.

    Every member of the set is served from its adapter's advertised list. Before
    this, dispatch named ``claude`` and ``codex`` and let everything else fall
    through to kiro -- so a member added to the set was silently mis-served the day
    it was added. The test walks the SET, not a list of names, so the next member
    is covered by construction rather than by remembering to edit a branch.
    """
    monkeypatch.setattr(agents.KiroCrewConfig, "load", staticmethod(lambda: _config(backend)))

    async def _never_reached(*_a, **_k):  # pragma: no cover - the assertion is that it is unreached
        raise AssertionError(f"{backend} must not enter the kiro --list-models branch")

    monkeypatch.setattr(agents, "reject_if_kiro_unverified", _never_reached)

    resp = await agents.api_models(_request())

    assert resp.status == 200
    assert "auto" in _names(json.loads(resp.body))
    # Cold cache, no live session: for every member that has NO static registry of
    # its own, ``auto`` alone is the honest answer. claude is the exception and is
    # deliberately not asserted here -- it has a ``claude_code`` registry to merge,
    # so a cold cache is not "nothing is known" for it, and ``_cc_models`` returns
    # those rows. The point of this test is the BRANCH, which the patch above
    # already proves: the kiro gate was never reached.
    if backend != ACP_BACKEND_CLAUDE:
        assert _names(json.loads(resp.body)) == ["auto"]


@pytest.mark.asyncio
async def test_kiro_backend_still_reaches_the_list_models_branch(monkeypatch):
    """Harness-parity H13: the kiro path did not move.

    ``reject_if_kiro_unverified`` IS the first step of the kiro branch, so being
    called again is the assertion. Its response short-circuits before the spawn, so
    this pins the routing without resolving a kiro binary.
    """
    monkeypatch.setattr(
        agents.KiroCrewConfig, "load", staticmethod(lambda: _config(ACP_BACKEND_KIRO))
    )
    seen: list[object] = []

    async def _gate(request):
        seen.append(request)
        return "short-circuit-response"

    monkeypatch.setattr(agents, "reject_if_kiro_unverified", _gate)

    resp = await agents.api_models(_request())

    assert seen, "the kiro backend must still enter the kiro branch"
    assert resp == "short-circuit-response"


@pytest.mark.asyncio
async def test_kas_backend_still_reaches_the_list_models_branch(monkeypatch):
    """KAS is a kiro-family harness on the shared ``acp`` namespace, and it is not a
    member of the advertised-selection set -- so it must keep reading the catalog."""
    monkeypatch.setattr(
        agents.KiroCrewConfig, "load", staticmethod(lambda: _config(ACP_BACKEND_KAS))
    )
    seen: list[object] = []

    async def _gate(request):
        seen.append(request)
        return "short-circuit-response"

    monkeypatch.setattr(agents, "reject_if_kiro_unverified", _gate)

    resp = await agents.api_models(_request())

    assert seen, "kas must still enter the kiro branch"
    assert resp == "short-circuit-response"


@pytest.mark.asyncio
async def test_codex_still_has_its_own_named_spelling(monkeypatch, tmp_path):
    """``_codex_models`` remains the named entry point codex's own tests drive; it
    now delegates rather than carrying a second copy of the row assembly."""
    client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_CODEX)
    client._session_id = "codex-sess-1"
    client._capture_available_models(
        {
            "sessionId": "codex-sess-1",
            "configOptions": [
                {
                    "id": "model",
                    "type": "select",
                    "currentValue": "gpt-5.4",
                    "options": [{"value": "gpt-5.4", "name": "gpt-5.4"}],
                },
            ],
        }
    )
    provider = MagicMock()
    provider.capabilities = capabilities_for(ACP_BACKEND_CODEX)
    provider.available_models = MagicMock(return_value=client.available_models())

    assert _names(agents._codex_models(_request(provider))) == ["auto", "gpt-5.4"]
