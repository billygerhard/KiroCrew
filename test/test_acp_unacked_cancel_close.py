"""A turn that ignores its cancel is closed, and its session reloaded before the next turn.

The synthesized terminal event for an unacked cancel only unblocks the caller:
the engine's turn kept running and its tool calls still landed (a Stop that
"worked" while the turn went on to spawn a worker, 2026-11-15). On an engine
that advertises ``sessionCapabilities.close`` the session is now closed, which
ends that one turn without touching co-tenants, and the provider reloads the
same id before its next turn. No engine is spawned; the runtime is a fake.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp.client import AcpError
from kiro_crew.acp.session_handle_dispatch import _close_unacked_session
from kiro_crew.acp.session_provider import AcpSessionProvider
from kiro_crew.acp.types import ACP_BACKEND_CLAUDE

_SID = "sess-unacked-1"


def _runtime(caps: dict | None) -> SimpleNamespace:
    new_handle = SimpleNamespace(
        session_id=_SID,
        model="",
        set_model=AsyncMock(),
        closed_after_unacked_cancel=None,
    )
    kept: list[str] = []
    runtime = SimpleNamespace(
        acp_backend=ACP_BACKEND_CLAUDE,
        is_cloud_sandbox=False,
        _agent="kirocrew",
        _agent_capabilities=caps if caps is not None else {},
        is_alive=lambda: True,
        evict_session=AsyncMock(),
        load_session=AsyncMock(return_value=new_handle),
        new_handle=new_handle,
        kept_during=kept,
    )

    @contextmanager
    def keep_record_on_teardown(session_id: str) -> Iterator[None]:
        kept.append(f"open:{session_id}")
        try:
            yield
        finally:
            kept.append(f"close:{session_id}")

    runtime.keep_record_on_teardown = keep_record_on_teardown
    return runtime


def _handle(runtime: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(
        _runtime=runtime,
        _session_id=_SID,
        session_id=_SID,
        active_agent="kirocrew",
        _bound_cwd="/work",
        _crew_agent="kirocrew",
        model="",
        keep_transcript=False,
        memory_mode="persistent",
        child_fidelity_aware=False,
        delete_delay_secs=0.0,
        set_model=AsyncMock(),
        closed_after_unacked_cancel=None,
    )


def _provider(runtime: SimpleNamespace, handle: SimpleNamespace) -> AcpSessionProvider:
    provider = AcpSessionProvider(handle, runtime, session_key="dashboard:abc")  # type: ignore[arg-type]
    provider.reclaim = MagicMock()  # type: ignore[method-assign]
    return provider


_CLOSE = {"sessionCapabilities": {"close": {}}}


class TestTheClose:
    @pytest.mark.asyncio
    async def test_a_close_capable_engine_closes_the_session(self) -> None:
        runtime = _runtime(_CLOSE)
        handle = _handle(runtime)
        _close_unacked_session(handle)  # type: ignore[arg-type]
        assert isinstance(handle.closed_after_unacked_cancel, asyncio.Task)
        await handle.closed_after_unacked_cancel
        runtime.evict_session.assert_awaited_once_with(_SID)

    @pytest.mark.asyncio
    async def test_an_engine_without_close_keeps_the_old_behaviour(self) -> None:
        runtime = _runtime({"sessionCapabilities": {"resume": {}}})
        handle = _handle(runtime)
        _close_unacked_session(handle)  # type: ignore[arg-type]
        assert handle.closed_after_unacked_cancel is None
        runtime.evict_session.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_failed_close_is_logged_not_raised(self) -> None:
        runtime = _runtime(_CLOSE)
        runtime.evict_session = AsyncMock(side_effect=RuntimeError("gone"))
        handle = _handle(runtime)
        _close_unacked_session(handle)  # type: ignore[arg-type]
        await handle.closed_after_unacked_cancel


class TestTheReload:
    @pytest.mark.asyncio
    async def test_the_next_turn_reloads_the_same_id_without_a_second_close(self) -> None:
        runtime = _runtime(_CLOSE)
        handle = _handle(runtime)
        _close_unacked_session(handle)  # type: ignore[arg-type]
        provider = _provider(runtime, handle)
        await provider.apply_pending_mcp_reinjection()
        runtime.evict_session.assert_awaited_once_with(_SID)
        runtime.load_session.assert_awaited_once()
        assert runtime.load_session.await_args.args[1] == _SID
        assert provider._handle is runtime.new_handle
        assert provider._mcp_reinject_pending is False
        assert runtime.kept_during == [f"open:{_SID}", f"close:{_SID}"]

    @pytest.mark.asyncio
    async def test_a_session_never_closed_is_left_alone(self) -> None:
        runtime = _runtime(_CLOSE)
        provider = _provider(runtime, _handle(runtime))
        await provider.apply_pending_mcp_reinjection()
        runtime.load_session.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_failed_reload_keeps_the_handle_and_names_stop(self) -> None:
        runtime = _runtime(_CLOSE)
        runtime.load_session = AsyncMock(side_effect=RuntimeError("no load"))
        handle = _handle(runtime)
        _close_unacked_session(handle)  # type: ignore[arg-type]
        provider = _provider(runtime, handle)
        with pytest.raises(AcpError, match="closed by Stop"):
            await provider.apply_pending_mcp_reinjection()
        # Not turned into an MCP mark; the closed handle retries at the next turn.
        assert provider._mcp_reinject_pending is False
        assert provider._handle is handle
