"""Orphan backend recycling tests (issue #1574).

Covers the new step 2b in _heartbeat_once: a backend with refcount > 0 but
empty _pending_requests and no forwarded frames for HARD_WEDGE_CEILING_SECS
is recycled as "wedged" (orphan).

This closes the gap where Windows backends become zombies because a dead named
pipe delivers no SIGPIPE — the gateway never calls detach_stub, so refcount
stays > 0 with _pending_requests empty, and the existing step 3 (which is
gated on _pending_requests being non-empty) never fires.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Optional
from unittest.mock import MagicMock

from kiro_crew.mcp_gateway import backend as backend_mod
from kiro_crew.mcp_gateway.backend import (
    HARD_WEDGE_CEILING_SECS,
    PING_STALE_SECS,
    _PendingRequest,
)


@dataclass
class _FakePoolKey:
    server_name: str = "kirocrew-core"
    agent_name: str = "kirocrew"

    def human_readable(self) -> str:
        return f"{self.agent_name}:{self.server_name}"


def _make_backend(
    *,
    refcount: int = 1,
    pending: Optional[dict[str, Any]] = None,
    returncode: Optional[int] = None,
    last_frame_mono: Optional[float] = None,
    last_ping_response_mono: Optional[float] = None,
    created_at: Optional[float] = None,
) -> Any:
    """Create a real Backend with mocked process/stdin for heartbeat testing."""
    process = MagicMock()
    process.returncode = returncode
    process.pid = 9999
    stdin = MagicMock()

    async def _drain() -> None:
        return None

    stdin.drain = _drain
    stdin.write = MagicMock()

    now = time.monotonic()
    backend = backend_mod.Backend(
        pool_key=_FakePoolKey(),  # type: ignore[arg-type]
        process=process,
        stdin=stdin,
        stdout=MagicMock(),
        created_at=created_at if created_at is not None else now,
        last_used_at=now,
    )
    backend.refcount = refcount
    if pending:
        backend._pending_requests.update(pending)
    # Default: recent ping (not stale)
    if last_ping_response_mono is not None:
        backend._last_ping_response_mono = last_ping_response_mono
    else:
        backend._last_ping_response_mono = now
    if last_frame_mono is not None:
        backend._last_frame_mono = last_frame_mono
    else:
        backend._last_frame_mono = now

    # Attach a stub inbox so broadcast_backend_gone has a target
    inbox: asyncio.Queue[bytes] = asyncio.Queue()
    backend._stub_inboxes["stub-A"] = inbox
    return backend


def _run(coro):
    """Run an async test without pytest-asyncio."""
    return asyncio.run(coro)


# --- Orphan detection (step 2b) tests ---------------------------------------


def test_orphan_refcount_positive_no_frames_recycled() -> None:
    """A backend with refcount > 0, empty _pending_requests, no frames
    for HARD_WEDGE_CEILING_SECS, AND stale pings should be recycled."""

    async def _inner():
        now = time.monotonic()
        backend = _make_backend(
            refcount=1,
            last_frame_mono=now - HARD_WEDGE_CEILING_SECS - 1,
            last_ping_response_mono=now - PING_STALE_SECS - 1,  # stale ping
        )
        state = await backend._heartbeat_once(now=now)
        assert state == "wedged"
        assert "orphan" in (backend._dead_reason or "")
        assert "refcount=1" in (backend._dead_reason or "")

    _run(_inner())


def test_orphan_not_triggered_with_recent_frames() -> None:
    """A backend with refcount > 0 but recent frame activity should NOT be
    recycled — it is legitimately idle between tool calls."""

    async def _inner():
        now = time.monotonic()
        backend = _make_backend(
            refcount=1,
            last_frame_mono=now - 60,
        )
        state = await backend._heartbeat_once(now=now)
        assert state == "alive"
        assert backend._dead_reason is None

    _run(_inner())


def test_orphan_not_triggered_when_pings_fresh() -> None:
    """A backend with frame silence >= ceiling but FRESH pings should NOT be
    recycled — it is a healthy idle session (user walked away). The ping
    response proves the backend's MCP read-loop is still servicing requests."""

    async def _inner():
        now = time.monotonic()
        backend = _make_backend(
            refcount=1,
            last_frame_mono=now - HARD_WEDGE_CEILING_SECS - 100,  # very silent
            last_ping_response_mono=now - 30,  # but pings are fresh!
        )
        state = await backend._heartbeat_once(now=now)
        assert state == "alive"
        assert backend._dead_reason is None

    _run(_inner())


def test_orphan_not_triggered_with_pending_requests() -> None:
    """When _pending_requests is non-empty, the orphan detection (step 2b) is
    skipped — the existing step 3 wedge detection handles that case."""

    async def _inner():
        now = time.monotonic()
        pending = {
            "gw-9999-1": _PendingRequest(
                stub_uuid="stub-A",
                original_id=1,
                method="tools/call",
                t_start_ms=(now - 100) * 1000.0,
            ),
        }
        backend = _make_backend(
            refcount=1,
            pending=pending,
            last_frame_mono=now - HARD_WEDGE_CEILING_SECS - 1,
        )
        state = await backend._heartbeat_once(now=now)
        # Should NOT trigger orphan — step 3 handles pending requests
        assert state != "wedged" or "orphan" not in (backend._dead_reason or "")

    _run(_inner())


def test_orphan_not_triggered_when_refcount_zero() -> None:
    """A backend with refcount == 0 returns 'idle' at step 2 regardless of
    frame silence — the idle-sweep owns those."""

    async def _inner():
        now = time.monotonic()
        backend = _make_backend(
            refcount=0,
            last_frame_mono=now - HARD_WEDGE_CEILING_SECS - 1,
        )
        state = await backend._heartbeat_once(now=now)
        assert state == "idle"

    _run(_inner())


def test_orphan_uses_created_at_when_no_frames_ever() -> None:
    """If _last_frame_mono is 0 (never forwarded), fall back to created_at
    for the silence calculation."""

    async def _inner():
        now = time.monotonic()
        old_created = now - HARD_WEDGE_CEILING_SECS - 100
        backend = _make_backend(
            refcount=1,
            last_frame_mono=0.0,
            created_at=old_created,
            last_ping_response_mono=now - PING_STALE_SECS - 1,  # stale
        )
        state = await backend._heartbeat_once(now=now)
        assert state == "wedged"
        assert "orphan" in (backend._dead_reason or "")

    _run(_inner())


def test_orphan_just_under_ceiling_not_recycled() -> None:
    """A backend just under the ceiling should NOT be recycled."""

    async def _inner():
        now = time.monotonic()
        backend = _make_backend(
            refcount=1,
            last_frame_mono=now - HARD_WEDGE_CEILING_SECS + 60,
        )
        state = await backend._heartbeat_once(now=now)
        assert state == "alive"
        assert backend._dead_reason is None

    _run(_inner())


# --- touch() and _route_backend_line frame tracking -------------------------


def test_touch_updates_last_frame_mono() -> None:
    """Backend.touch() should update both last_used_at and _last_frame_mono."""
    now = time.monotonic()
    backend = _make_backend(last_frame_mono=now - 1000)
    backend.touch(now=now)
    assert backend._last_frame_mono == now
    assert backend.last_used_at == now
