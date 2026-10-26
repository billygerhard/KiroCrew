"""Record each Claude seat's plan usage from the rate-limit info its replies carry.

claude-agent-acp forwards Claude Code's ``rate_limit_event`` as a ``usage_update``
whose ``_meta["_claude/rateLimit"]`` holds the plan windows the API just reported,
e.g. ``unifiedWindows: {five_hour: {utilization: 0.04, resetsAt: 1793056800}}``
(utilization is a 0-1 fraction, resetsAt epoch seconds). Folding those into
``<config_dir>/seat-rate-limits.json``, keyed by backend id (``claude-seat1``...),
gives the seat picker and failover cron live usage for every seat in use, so they
no longer have to run ``claude -p /usage`` (which scans every local transcript)
on seats that are busy anyway.

File shape: ``{"claude-seat1": {"at": <epoch>, "windows": {"five_hour":
{"utilization": 0.04, "resetsAt": 1793056800, "at": <epoch>}}}}``. A window is
replaced only by a newer report of the same window, so a reply that reports only
``five_hour`` keeps the last weekly reading. Never raises: this runs inside the
prompt-turn dispatch path.
"""

from __future__ import annotations

import json
import logging
import math
import threading
import time
from typing import Any

from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import config_dir

logger = logging.getLogger(__name__)

FILE_NAME = "seat-rate-limits.json"
META_KEY = "_claude/rateLimit"

_lock = threading.Lock()
_state: dict[str, Any] | None = None


def _windows(info: dict[str, Any]) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    raw = info.get("unifiedWindows")
    if not isinstance(raw, dict):
        return out
    for name, w in raw.items():
        if not isinstance(name, str) or not isinstance(w, dict):
            continue
        util = w.get("utilization")
        resets = w.get("resetsAt")
        if isinstance(util, bool) or not isinstance(util, (int, float)) or not math.isfinite(util):
            continue
        entry: dict[str, float] = {"utilization": max(0.0, float(util))}
        if isinstance(resets, (int, float)) and not isinstance(resets, bool) and math.isfinite(resets):
            entry["resetsAt"] = float(resets)
        out[name] = entry
    return out


def record(backend: str | None, update: Any) -> None:
    """Fold one ``usage_update``'s rate-limit info into the per-seat file."""
    global _state
    try:
        if not backend or not isinstance(update, dict):
            return
        meta = update.get("_meta")
        info = meta.get(META_KEY) if isinstance(meta, dict) else None
        if not isinstance(info, dict):
            return
        windows = _windows(info)
        if not windows:
            return
        now = time.time()
        path = config_dir() / FILE_NAME
        with _lock:
            if _state is None:
                try:
                    loaded = json.loads(path.read_text())
                    _state = loaded if isinstance(loaded, dict) else {}
                except (OSError, ValueError):
                    _state = {}
            seat = _state.setdefault(backend, {"windows": {}})
            known = seat.setdefault("windows", {})
            changed = False
            for name, w in windows.items():
                old = known.get(name) or {}
                if old.get("utilization") != w["utilization"] or old.get("resetsAt") != w.get("resetsAt"):
                    changed = True
                known[name] = {**w, "at": now}
            seat["at"] = now
            # Rewrite on a change, or once a minute so readers can tell the seat is live.
            if changed or now - seat.get("written", 0) >= 60:
                seat["written"] = now
                atomic_write(path, json.dumps(_state, indent=1))
    except Exception:  # noqa: BLE001 - never break a turn over bookkeeping
        logger.debug("seat rate-limit record failed", exc_info=True)
