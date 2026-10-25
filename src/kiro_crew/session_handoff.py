"""A parent moves to another backend while its sub-agents keep running.

A chat moving seats (``session_set_backend`` between Claude seats, a failover
move) resets its session. The plain reset releases the runtime the parent's
session-sharing children are multiplexed onto, and its companion sub-agent
runtime, so a parent with children could not move until every child finished,
and on a seat that ran out of quota that meant it could not run at all.

``reset(keep_children=True)`` hands those runtimes off instead: the parent's own
session ends (its transcript is kept as the resume material), and each runtime is
parked here until its last session drains, then killed. The watcher is per
runtime and bounded by the runtime's own life, so nothing is left behind: a
runtime that dies on its own ends its watcher, and one another reaper took off
the draining list is left to that reaper.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

#: How often a parked runtime is probed for remaining sessions.
DRAIN_POLL_SECONDS = 10.0

#: A runtime is killed only after it read idle on this many consecutive probes, so
#: a child claimed a moment before the hand-off and not yet in its init scope is
#: never mistaken for a drained runtime.
IDLE_PROBES_BEFORE_KILL = 2


async def hand_off_runtime(provider: Any) -> Any:
    """Detach the runtime *provider* owns, or ``None`` when it owns none.

    Looks through the provider wrapper the way the sub-agent manager finds a
    parent's runtime (``client`` / ``_client``). ``None`` for a provider with no
    shared runtime of its own, and the reset then proceeds as usual.
    """
    for candidate in (
        provider,
        getattr(provider, "client", None),
        getattr(provider, "_client", None),
    ):
        hand_off = getattr(candidate, "hand_off_runtime", None)
        if callable(hand_off):
            try:
                return await hand_off()
            except Exception:
                logger.warning("runtime hand-off failed; resetting normally", exc_info=True)
                return None
    return None


def _busy(runtime: Any) -> bool:
    try:
        return bool(runtime.is_alive() and runtime.has_active_or_initializing_sessions())
    except Exception:
        return True  # fail toward preserving work


async def _reap_when_drained(owner: Any, runtime: Any, reason: str) -> None:
    idle_probes = 0
    while True:
        await asyncio.sleep(DRAIN_POLL_SECONDS)
        if runtime not in owner._draining_subagent_runtimes:
            return
        try:
            alive = runtime.is_alive()
        except Exception:
            alive = True
        if alive and _busy(runtime):
            idle_probes = 0
            continue
        idle_probes += 1
        if alive and idle_probes < IDLE_PROBES_BEFORE_KILL:
            continue
        try:
            await runtime.kill(expected=True, reason=reason)
        except Exception:
            logger.warning("Failed to reap a handed-off runtime; will retry", exc_info=True)
            continue
        try:
            owner._draining_subagent_runtimes.remove(runtime)
        except ValueError:
            pass
        logger.info("Reaped a handed-off runtime once its sub-agents drained")
        return


def park_until_drained(owner: Any, runtime: Any, *, reason: str) -> None:
    """Park *runtime* on *owner*'s draining list and kill it once its sessions end.

    Parked, never dropped: an unreferenced runtime escapes ``close_all`` and every
    reaper alike, and the draining list is what the shutdown path and the PID
    shield both read.
    """
    owner._draining_subagent_runtimes.append(runtime)
    task = asyncio.create_task(_reap_when_drained(owner, runtime, reason))
    owner._background_tasks.add(task)
    task.add_done_callback(owner._background_tasks.discard)


__all__ = [
    "DRAIN_POLL_SECONDS",
    "IDLE_PROBES_BEFORE_KILL",
    "hand_off_runtime",
    "park_until_drained",
]
