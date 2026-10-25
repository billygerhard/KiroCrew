"""A parent moving backend hands its runtime to running sub-agents to drain."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from kiro_crew import session_handoff


class _Runtime:
    def __init__(self) -> None:
        self.sessions = 1
        self.killed = asyncio.Event()

    def is_alive(self) -> bool:
        return not self.killed.is_set()

    def has_active_or_initializing_sessions(self) -> bool:
        return self.sessions > 0

    async def kill(self, *, expected: bool, reason: str) -> None:
        self.killed.set()


def _owner() -> SimpleNamespace:
    return SimpleNamespace(_draining_subagent_runtimes=[], _background_tasks=set())


@pytest.fixture
def fast_poll(monkeypatch):
    monkeypatch.setattr(session_handoff, "DRAIN_POLL_SECONDS", 0.0)


def test_a_parked_runtime_is_kept_while_busy_and_killed_once_drained(fast_poll):
    async def scenario() -> None:
        owner, runtime = _owner(), _Runtime()
        session_handoff.park_until_drained(owner, runtime, reason="test")
        assert runtime in owner._draining_subagent_runtimes
        for _ in range(20):
            await asyncio.sleep(0)
        assert not runtime.killed.is_set()
        runtime.sessions = 0
        await asyncio.wait_for(runtime.killed.wait(), timeout=5)
        for _ in range(5):
            await asyncio.sleep(0)
        assert runtime not in owner._draining_subagent_runtimes

    asyncio.run(scenario())


def test_a_runtime_another_reaper_took_ends_the_watcher(fast_poll):
    async def scenario() -> None:
        owner, runtime = _owner(), _Runtime()
        session_handoff.park_until_drained(owner, runtime, reason="test")
        owner._draining_subagent_runtimes.remove(runtime)
        runtime.sessions = 0
        (task,) = owner._background_tasks
        await asyncio.wait_for(task, timeout=5)
        assert not runtime.killed.is_set()

    asyncio.run(scenario())


def test_hand_off_finds_the_provider_through_its_wrapper():
    async def scenario() -> None:
        runtime = object()

        class _Inner:
            async def hand_off_runtime(self):
                return runtime

        assert await session_handoff.hand_off_runtime(SimpleNamespace(_client=_Inner())) is runtime
        assert await session_handoff.hand_off_runtime(SimpleNamespace()) is None

    asyncio.run(scenario())


def test_a_provider_hands_off_only_a_runtime_it_owns():
    from kiro_crew.acp.session_provider import AcpSessionProvider

    async def scenario() -> None:
        released = []

        async def release() -> None:
            released.append(True)

        handle = SimpleNamespace(keep_transcript=False)
        owner = SimpleNamespace(
            _owns_runtime=True,
            _runtime="rt",
            _handle=handle,
            release_runtime_lease=release,
        )
        assert await AcpSessionProvider.hand_off_runtime(owner) == "rt"
        assert owner._owns_runtime is False and handle.keep_transcript is True
        assert released == [True]
        assert await AcpSessionProvider.hand_off_runtime(owner) is None

    asyncio.run(scenario())
