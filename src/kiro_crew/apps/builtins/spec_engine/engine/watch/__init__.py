"""Watchers: turning a configured command into watched items, for free.

A watch source is configuration — a poll command and a field mapping — so
watching a new tracker needs no plugin code. Import from this package rather
than its modules.

    from ...engine.watch import poll_source, poll_tick

    outcome = poll_source(store, "upstream-issues")
    if outcome.found_no_items:
        ...   # true only after a poll that actually ran

The module split follows what each part is responsible for:

* :mod:`.items` — the seven fields a source yields, all of them untrusted text.
* :mod:`.sources` — a source's definition, its enablement, and its field mapping.
* :mod:`.poll` — running the command and reporting health, where an unavailable
  program is never mistaken for an empty backlog.
* :mod:`.lifecycle` — diffing successive polls into new items, reopens, and
  cancellations, and claiming each (item, generation) once.
* :mod:`.tick` — the script cron that runs a tick with no model invocation.
"""

from __future__ import annotations

from .items import ITEM_FIELDS, REQUIRED_ITEM_FIELDS, WatchedItem
from .lifecycle import (
    CLOSED_STATES,
    DISPATCHING_TRANSITIONS,
    FIRST_GENERATION,
    ItemChange,
    Transition,
    WatchAdvance,
    WatchDiff,
    advance_watch,
    claim_dispatch,
    claim_dispatches,
    diff_poll,
    dispatched_generations,
    generation_key,
    is_open_state,
    observations_of,
    record_snapshot,
    release_dispatch_claim,
)
from .poll import (
    MAX_RECORDED_PROBLEMS,
    HealthReason,
    PollOutcome,
    PollStatus,
    RejectedItem,
    poll,
    poll_source,
    poll_sources,
)
from .sources import (
    ENABLED_KEY,
    FIELD_MAP_KEY,
    INTERVAL_SETTING,
    POLL_KEY,
    POLL_TIMEOUT_SETTING,
    FieldMapping,
    WatchSource,
    load_sources,
    poll_interval_s,
    poll_timeout_s,
    source_names,
)
from .tick import (
    CRON_ENTRY_POINT,
    CRON_JOB_PREFIX,
    CRON_SCRIPT_FILENAME,
    CRON_TIMEOUT_MARGIN_S,
    HOST_SCRIPT_TIMEOUT_S,
    TickReport,
    cron_definitions,
    cron_job_name,
    crons_directory,
    install_tick_script,
    poll_tick,
    run_tick_script,
    source_of_job,
    tick_script_path,
)

__all__ = [
    "CLOSED_STATES",
    "CRON_ENTRY_POINT",
    "CRON_JOB_PREFIX",
    "CRON_SCRIPT_FILENAME",
    "CRON_TIMEOUT_MARGIN_S",
    "DISPATCHING_TRANSITIONS",
    "ENABLED_KEY",
    "FIELD_MAP_KEY",
    "FIRST_GENERATION",
    "HOST_SCRIPT_TIMEOUT_S",
    "INTERVAL_SETTING",
    "ITEM_FIELDS",
    "MAX_RECORDED_PROBLEMS",
    "POLL_KEY",
    "POLL_TIMEOUT_SETTING",
    "REQUIRED_ITEM_FIELDS",
    "FieldMapping",
    "HealthReason",
    "ItemChange",
    "PollOutcome",
    "PollStatus",
    "RejectedItem",
    "TickReport",
    "Transition",
    "WatchAdvance",
    "WatchDiff",
    "WatchSource",
    "WatchedItem",
    "advance_watch",
    "claim_dispatch",
    "claim_dispatches",
    "cron_definitions",
    "cron_job_name",
    "crons_directory",
    "diff_poll",
    "dispatched_generations",
    "generation_key",
    "install_tick_script",
    "is_open_state",
    "load_sources",
    "observations_of",
    "poll",
    "poll_interval_s",
    "poll_source",
    "poll_sources",
    "poll_tick",
    "poll_timeout_s",
    "record_snapshot",
    "release_dispatch_claim",
    "run_tick_script",
    "source_names",
    "source_of_job",
    "tick_script_path",
]
