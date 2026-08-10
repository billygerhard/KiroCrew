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
* :mod:`.tick` — the script cron that runs a tick with no model invocation.
"""

from __future__ import annotations

from .items import ITEM_FIELDS, REQUIRED_ITEM_FIELDS, WatchedItem
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
    "CRON_ENTRY_POINT",
    "CRON_JOB_PREFIX",
    "CRON_SCRIPT_FILENAME",
    "CRON_TIMEOUT_MARGIN_S",
    "ENABLED_KEY",
    "FIELD_MAP_KEY",
    "HOST_SCRIPT_TIMEOUT_S",
    "INTERVAL_SETTING",
    "ITEM_FIELDS",
    "MAX_RECORDED_PROBLEMS",
    "POLL_KEY",
    "POLL_TIMEOUT_SETTING",
    "REQUIRED_ITEM_FIELDS",
    "FieldMapping",
    "HealthReason",
    "PollOutcome",
    "PollStatus",
    "RejectedItem",
    "TickReport",
    "WatchSource",
    "WatchedItem",
    "cron_definitions",
    "cron_job_name",
    "crons_directory",
    "install_tick_script",
    "load_sources",
    "poll",
    "poll_interval_s",
    "poll_source",
    "poll_sources",
    "poll_tick",
    "poll_timeout_s",
    "run_tick_script",
    "source_names",
    "source_of_job",
    "tick_script_path",
]
