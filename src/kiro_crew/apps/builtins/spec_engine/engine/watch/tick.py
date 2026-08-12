"""The poll tick: watching that costs nothing until something is found.

Idle watching has to be free, and "free" here is structural rather than
careful. A tick is a Python function that runs a configured program and decodes
its output. It reaches no model, opens no agent session, and writes no turn to
the metering ledger, so an idle watcher's cost is not a small number — there is
no path from this module to a place where credits are spent.

That is why the tick is a **script cron** rather than a scheduled prompt. A
prompt-driven watcher would spend a turn every interval asking a model whether
anything changed, which is the cost of a poll multiplied by the number of
sources and divided by nothing. The host's script cron support runs the function
directly in a sandboxed subprocess and never involves a model.

**One cron job per enabled source.** The interval is a per-source setting, and
the scheduler is already a durable timekeeper, so one job per source is the
whole implementation of per-source intervals: no shared tick has to decide whose
turn it is, and disabling a source removes its job. The tick re-reads
``enabled`` when it fires anyway — configuration is the authority, and a job left
behind by a failed removal must not poll a source the operator turned off.

**Only trouble is delivered.** A tick that found nothing and saw nothing wrong
raises ``Skip``: silent, no message, no credits. A tick that found items
also stays quiet, because an open issue is still open on the next tick and a
message per tick per item is noise a human learns to ignore. What happens to
those items is not this module's decision: the tick reports what it saw, and the
dispatcher that consumes a tick's items is separate work, so nothing here
records or claims anything.
An unhealthy source, though, is reported every time it is unhealthy — a watcher
that cannot see is the one condition where silence is the bug.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import config_dir

from ..budget.switch import KillSwitch
from ..config import ConfigStore
from ..delivery.stages import CommandRunner
from .items import WatchedItem
from .poll import PollOutcome, PollStatus, poll_sources
from .sources import load_sources, poll_interval_s, poll_timeout_s

logger = logging.getLogger(__name__)

#: Prefix of the cron job name for a source's poll tick. The source name follows
#: it, so a job is traceable to the configuration entry that produced it.
CRON_JOB_PREFIX = "watch-poll"

#: Separator between the prefix and the source name in a job name.
CRON_JOB_SEPARATOR = ":"

#: File the tick shim is installed as. Script crons may only run files under the
#: host's crons directory, so the engine's entry point reaches the scheduler
#: through a small generated file that calls into this module.
CRON_SCRIPT_FILENAME = "spec_engine_watch.py"

#: Function the shim exposes to the scheduler.
CRON_ENTRY_POINT = "run"

#: Owner-only: the file is executed as code by the scheduler.
_SCRIPT_MODE = 0o600

#: Seconds a script cron gets when its job sets no timeout. Mirrored here
#: because a poll command's own ceiling can exceed it, and a job whose timeout
#: is left at the host default would be killed mid-poll and recorded as a
#: failure of the source rather than of the schedule.
HOST_SCRIPT_TIMEOUT_S = 30

#: Headroom added to a poll's own timeout when sizing the cron job's. Covers
#: interpreter start, configuration read, and output decoding, so the poll
#: command's timeout is the only ceiling that can actually fire.
CRON_TIMEOUT_MARGIN_S = 15

_SHIM_SOURCE = '''"""Watch poll tick for the spec engine app.

Generated file. The scheduler may only run scripts from this directory, so this
shim is how the engine's tick is reachable; the logic lives in the app.
"""

from kiro_crew.apps.builtins.spec_engine.engine.watch import run_tick_script


def run(ctx):
    return run_tick_script(ctx)
'''


@dataclass(frozen=True)
class TickReport:
    """Every source's outcome for one tick."""

    outcomes: tuple[PollOutcome, ...]
    #: Set when the kill switch was engaged, in which case nothing was polled.
    #: The reason travels with the report because a tick that returns no outcomes
    #: is otherwise indistinguishable from one with no sources configured.
    paused: str = ""

    @property
    def polled(self) -> tuple[PollOutcome, ...]:
        """Outcomes from sources that were actually polled."""
        return tuple(o for o in self.outcomes if o.status is PollStatus.OK)

    @property
    def unhealthy(self) -> tuple[PollOutcome, ...]:
        """Sources that could not be polled."""
        return tuple(o for o in self.outcomes if o.status is PollStatus.UNHEALTHY)

    @property
    def items(self) -> tuple[WatchedItem, ...]:
        """Every item found this tick, across sources, in source order."""
        return tuple(item for outcome in self.polled for item in outcome.items)

    @property
    def idle(self) -> bool:
        """Whether this tick found nothing and saw nothing wrong.

        Deliberately not "found no items": a tick with an unhealthy source is
        never idle, whatever the item count, because the reason there is nothing
        to do may be that nothing could be looked at.
        """
        return not self.items and not self.unhealthy

    def summary(self) -> str:
        """A human-readable line per source."""
        if self.paused:
            return self.paused
        return "\n".join(outcome.describe() for outcome in self.outcomes)


def poll_tick(
    store: ConfigStore | None = None,
    *,
    sources: Sequence[str] | None = None,
    runner: CommandRunner | None = None,
    kill_switch: KillSwitch | None = None,
) -> TickReport:
    """Run one poll tick over *sources*, or every enabled source.

    Reads the enabled set from configuration on every tick rather than trusting
    the caller: the scheduler holds a job per source, and a job that outlived its
    source's enablement must not be the thing that decides whether to poll.

    The kill switch is read here, before anything is selected, which is what makes
    one operator action pause *every* watcher. Checking it per source, or handing a
    list of jobs to pause, would pause the watchers that were known when the switch
    was thrown and poll the one added to configuration afterwards.
    """
    resolved = store if store is not None else ConfigStore()
    switch = kill_switch if kill_switch is not None else KillSwitch()
    state = switch.read()
    if state.engaged:
        logger.warning("watch tick skipped: %s", state.describe())
        return TickReport(outcomes=(), paused=state.describe())
    if sources is None:
        selected = tuple(source.name for source in load_sources(resolved, enabled_only=True))
    else:
        selected = tuple(sources)
    return TickReport(outcomes=poll_sources(resolved, selected, runner=runner))


def cron_job_name(source: str) -> str:
    """The cron job name for *source*'s poll tick."""
    return f"{CRON_JOB_PREFIX}{CRON_JOB_SEPARATOR}{source}"


def source_of_job(name: str) -> str:
    """The source a poll-tick job name refers to, or empty for another job."""
    prefix = f"{CRON_JOB_PREFIX}{CRON_JOB_SEPARATOR}"
    return name[len(prefix) :] if name.startswith(prefix) else ""


def cron_definitions(
    store: ConfigStore | None = None,
    *,
    script_path: str = "",
) -> tuple[dict[str, Any], ...]:
    """Return one script-cron definition per enabled source.

    The definitions are data for whoever registers crons; nothing is scheduled
    here. ``timeout`` is set explicitly from the source's own poll ceiling plus
    headroom, because the host's default for a script job is shorter than a poll
    command may legitimately take.
    """
    resolved = store if store is not None else ConfigStore()
    script = script_path or f"{tick_script_path()}:{CRON_ENTRY_POINT}"
    definitions: list[dict[str, Any]] = []
    for source in load_sources(resolved, enabled_only=True):
        definitions.append(
            {
                "name": cron_job_name(source.name),
                "every": poll_interval_s(resolved, source.name),
                "script": script,
                # The source name travels in the job's message, the documented
                # channel for handing a script its arguments.
                "message": source.name,
                "timeout": poll_timeout_s(resolved, source.name) + CRON_TIMEOUT_MARGIN_S,
                # Nothing is delivered on an ordinary tick, so the job has no
                # reason to appear in a chat surface.
                "silent": True,
                "enabled": True,
            }
        )
    return tuple(definitions)


def crons_directory() -> Path:
    """The host directory script crons may be run from."""
    return config_dir() / "crons"


def tick_script_path() -> Path:
    """Where the tick shim is installed."""
    return crons_directory() / CRON_SCRIPT_FILENAME


def install_tick_script(directory: Path | None = None) -> Path:
    """Write the tick shim, and return its path.

    Idempotent by content: an unchanged shim is left alone so its modification
    time keeps meaning "this changed", and a rewritten one replaces the old
    version atomically rather than being briefly half-written while the
    scheduler could read it.
    """
    target = (directory or crons_directory()) / CRON_SCRIPT_FILENAME
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        if target.read_text(encoding="utf-8") == _SHIM_SOURCE:
            return target
    except (OSError, UnicodeDecodeError):
        pass
    atomic_write(target, _SHIM_SOURCE, mode=_SCRIPT_MODE)
    logger.info("installed the watch poll tick script at %s", target)
    return target


def run_tick_script(ctx: Any) -> None:
    """Scheduler entry point: poll, then report only what a human must act on.

    Raises the scheduler's own control exceptions, imported here rather than at
    module scope so the engine stays importable without the host's cron
    subsystem: a test, the MCP server, or the UI backend all load the watcher's
    logic without loading a scheduler.
    """
    from kiro_crew.cron_script import Report, Skip

    source = str(getattr(ctx, "message", "") or "").strip()
    report = poll_tick(sources=(source,) if source else None)
    if report.unhealthy:
        raise Report(report.summary())
    if report.idle:
        raise Skip()
