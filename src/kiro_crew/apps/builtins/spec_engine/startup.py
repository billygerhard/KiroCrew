"""The app's startup hook: assess readiness, then schedule the watcher.

Two things have to happen when this app starts or is enabled, and both are
observations-or-wiring rather than work:

* **Readiness** is assessed and recorded, so an app whose skill or MCP server
  never registered reports the specific failure instead of looking operational.
  That logic lives in :mod:`.readiness`; this module calls it.
* **The watcher's schedule** is reconciled against configuration: the tick shim is
  installed and one script cron per enabled source is created, updated, or
  removed. Without this the whole watch path is inert — a tick nothing schedules
  polls no source, whatever its own tests prove.

They are in one hook because the manifest declares one ``on_startup`` and both
are properties of "this app just came up". Ordering matters only in that
readiness is recorded first: it is the thing a surface reads to explain a broken
install, and a scheduling failure must not cost the operator that answer.

Never raises. A hook that raised would leave the app with no recorded readiness
and no schedule, and the operator with a traceback in the gateway log instead of
a state they can read.
"""

from __future__ import annotations

import logging
from typing import Any

from . import readiness
from .engine.watch.wiring import install_watch_schedule

logger = logging.getLogger(__name__)


async def on_startup(ctx: Any) -> None:
    """Record readiness, then reconcile the watcher's cron schedule.

    Asynchronous because the cron SDK's synchronous mutators refuse to run on a
    running event loop — they would park the gateway for the store-lock window —
    and this hook is invoked on that loop. The host awaits a coroutine hook.
    """
    readiness.on_startup(ctx)

    cron = getattr(ctx, "cron", None)
    if cron is None:
        # A context with no cron SDK is a process that cannot schedule anything
        # (a test, a CLI). Nothing to reconcile, and nothing wrong.
        logger.info("spec-engine: no cron service in this context; watch schedule not reconciled")
        return
    try:
        report = await install_watch_schedule(cron)
    except Exception as exc:  # noqa: BLE001 - a failed schedule is not a failed startup
        logger.warning("spec-engine: the watch schedule could not be reconciled: %s", exc)
        _degrade(ctx, f"the watch schedule could not be reconciled: {exc}")
        return
    if report.problems:
        _degrade(ctx, "the watch schedule is incomplete: " + "; ".join(report.problems))


def _degrade(ctx: Any, reason: str) -> None:
    """Report a scheduling problem on the app's health, when the host offers one.

    Degraded rather than error: the app's tools and skill still work, and a
    surface that reported the whole app broken because one cron job failed would
    hide the readiness state that is the more useful answer.
    """
    health = getattr(ctx, "health", None)
    if health is not None and hasattr(health, "mark_degraded"):
        health.mark_degraded(f"spec-engine: {reason}")
    logger.warning("spec-engine: %s", reason)
