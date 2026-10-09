"""A model pick on a config-authored harness must reach the adapter, or fail loudly.

A ``harnesses.json`` descriptor wrapping claude-agent-acp is never in the frozen
``ACP_BACKENDS_MODEL_VIA_CONFIG_OPTION`` set, so its switch fell through to
``session/set_model`` -- a method claude-agent-acp 0.66.0 does not implement. The
request was also never awaited, so the adapter's refusal was dropped and the handle
recorded the pick as applied while the previous model kept serving every turn.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp.client import AcpError
from kiro_crew.acp.harness import operator_registry
from kiro_crew.acp.session_handle import AcpSessionHandle
from kiro_crew.acp.types import (
    ACP_BACKEND_KIRO,
    METHOD_SET_CONFIG_OPTION,
    METHOD_SET_MODEL,
    MODEL_CONFIG_ID,
)

_OPERATOR = "claude-seat9"
_MODEL = "sonnet"
_MODEL_SELECT = {
    "id": MODEL_CONFIG_ID,
    "type": "select",
    "currentValue": "default",
    "options": [{"value": "default"}, {"value": "opus"}, {"value": _MODEL}],
}


@pytest.fixture
def operator_backend(monkeypatch):
    def _models(backend_id):
        return ("acp_advertised", ()) if backend_id == _OPERATOR else None

    monkeypatch.setattr(operator_registry, "operator_backend_models", _models)


def _handle(backend: str, config_options: list) -> tuple[AcpSessionHandle, MagicMock]:
    runtime = MagicMock()
    runtime.acp_backend = backend
    runtime.send_request = AsyncMock(return_value=1)
    handle = AcpSessionHandle("sess-1", asyncio.Queue(), runtime)
    handle._wait_for_response = AsyncMock(return_value={})  # type: ignore[method-assign]
    handle._available_models = [{"modelId": "opus"}, {"modelId": _MODEL}]
    handle._config_options = config_options
    return handle, runtime


def _sent_methods(runtime: MagicMock) -> list[str]:
    return [call.args[0] for call in runtime.send_request.await_args_list]


@pytest.mark.asyncio
async def test_operator_harness_advertising_a_model_select_switches_through_it(
    operator_backend,
):
    handle, runtime = _handle(_OPERATOR, [_MODEL_SELECT])
    await handle.set_model(_MODEL)
    methods = _sent_methods(runtime)
    assert METHOD_SET_CONFIG_OPTION in methods
    assert METHOD_SET_MODEL not in methods
    assert handle._model == _MODEL


@pytest.mark.asyncio
async def test_operator_harness_without_a_model_select_keeps_set_model(operator_backend):
    handle, runtime = _handle(_OPERATOR, [{"id": "mode", "type": "select"}])
    await handle.set_model(_MODEL)
    assert _sent_methods(runtime) == [METHOD_SET_MODEL]


@pytest.mark.asyncio
async def test_a_builtin_is_not_moved_by_its_advertisement(operator_backend):
    """Builtins stay governed by the reviewed set alone."""
    handle, runtime = _handle(ACP_BACKEND_KIRO, [_MODEL_SELECT])
    await handle.set_model(_MODEL)
    assert _sent_methods(runtime) == [METHOD_SET_MODEL]


@pytest.mark.asyncio
async def test_a_refused_set_model_raises_and_records_nothing(operator_backend):
    handle, runtime = _handle(_OPERATOR, [])
    handle._model = "opus"
    handle._wait_for_response = AsyncMock(  # type: ignore[method-assign]
        side_effect=AcpError("Method not found")
    )
    with pytest.raises(AcpError):
        await handle.set_model(_MODEL)
    assert _sent_methods(runtime) == [METHOD_SET_MODEL]
    handle._wait_for_response.assert_awaited_once()
    assert handle._model == "opus"
