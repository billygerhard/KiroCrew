"""Watcher wiring: what constructs the watcher, and what schedules it.

Every piece of the watch path was built before this module and none of it ran. A
tick that nothing installs polls no source; a screener that nothing constructs
screens no item; a ``claimed`` writeback whose poster is never passed reaches no
tracker. A library that nothing constructs passes every test it has, so this
module is where the watcher becomes a running thing, and each construction here
is covered by a test that fails when the construction is deleted.

What is wired, and why each is not optional:

* **The poll tick's schedule.** One script cron per enabled source plus the shim
  the host's scheduler runs, reconciled on every startup so a disabled source
  loses its job and a changed interval takes effect without an operator editing
  the scheduler.
* **The screener, on both entry paths.** An item starts two ways — a poll tick
  and the queue drain — and the same screener instance is passed to both. A
  guarantee enforced on one of two entry paths is the shape that produced this
  project's security defects, and the queue path is the one a reader forgets.
* **The cancel cascade and the audit log.** The dispatcher takes both as required
  keywords because a default could only mean *skip*: an item withdrawn mid-run
  would keep spending, and an edit ignored for dispatch would go unrecorded.
* **The ``claimed`` writeback poster.** The other three item-feedback events fire
  from ``RunMachine.transition``, which builds its own poster; ``claimed`` is
  emitted by the dispatcher instead, so it is the one event that is silent unless
  a poster is passed here.
* **The prerequisite gate.** The starter is a :class:`~.dispatch.GatedStarter`
  over the engine graph, never the graph's seeder: the seeder is publicly
  constructible, and the dispatch entry points refuse one that did not come
  through :meth:`~..composition.EngineGraph.begin_run`.

What is *not* wired, named rather than implied: the review-feedback watcher is
constructed here but its fix-round reviser is a caller-supplied seam, because a
fix round edits code in the run's own tool-enabled host session and no bridge to
one exists yet (the host's session manager offers no per-run session key or
applied posture). :func:`build_review_feedback_watcher` therefore requires the
reviser and the delivery pipeline rather than defaulting them — a default could
only claim comments and author nothing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from ..budget.caps import caps_for
from ..budget.ledger import RunAccounting
from ..config import ConfigStore
from ..roles import SessionDefault
from ..turns import TurnHost
from .dispatch import (
    DispatchReport,
    GatedStarter,
    QueueDispatch,
    SeedScreener,
    dispatch_tick,
    drain_queue,
)
from .feedback import FeedbackPoster
from .review_feedback import (
    CommentScreener,
    FeedbackReviser,
    ReviewFeedbackWatcher,
    RevisionDelivery,
    review_feedback_enabled,
)
from .screening import IntakeScreener, ScreeningProvider
from .screening_provider import DispatchedScreeningProvider
from .sources import load_sources
from .tick import (
    CRON_ENTRY_POINT,
    CRON_TIMEOUT_MARGIN_S,
    TickReport,
    cron_definitions,
    cron_job_name,
    install_tick_script,
    poll_tick,
    source_of_job,
    tick_script_path,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..composition import EngineGraph

logger = logging.getLogger(__name__)

#: Cron job name for the review-feedback poll. One job rather than one per
#: project: the watcher reads which projects armed it on every tick, so a job per
#: project would need removing when a project disarms and would poll a stale set
#: until it was.
REVIEW_FEEDBACK_JOB = "watch-review-feedback"

#: Function the shim exposes for the review-feedback poll. A second entry point
#: in the one installed shim rather than a second file, so the two schedules
#: cannot drift onto different copies of the app's code.
REVIEW_FEEDBACK_ENTRY_POINT = "review_feedback"

#: How often the review-feedback poll runs, in seconds. A reviewer's comment is
#: answered within a few minutes rather than instantly; a poll costs no credits,
#: but it does run a configured tracker command per delivered run.
REVIEW_FEEDBACK_INTERVAL_S = 300

#: Timeout for the review-feedback cron job. The watcher's own poll ceiling is
#: per project, so the job is sized from the bundled ceiling plus the same
#: headroom the poll ticks use for interpreter start and configuration reads.
REVIEW_FEEDBACK_TIMEOUT_S = 60 + CRON_TIMEOUT_MARGIN_S


@dataclass(frozen=True)
class ScheduleReport:
    """What one reconciliation of the watcher's schedule changed."""

    script: Path | None = None
    added: tuple[str, ...] = ()
    updated: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    #: Jobs that could not be reconciled, as ``name: reason``. Carried rather
    #: than raised: one source's bad interval must not stop the others being
    #: scheduled, and an app's startup hook that raised would leave no schedule
    #: at all.
    problems: tuple[str, ...] = ()

    @property
    def scheduled(self) -> tuple[str, ...]:
        return tuple(sorted(set(self.added) | set(self.updated)))

    def describe(self) -> str:
        parts = [
            f"added {len(self.added)}",
            f"updated {len(self.updated)}",
            f"removed {len(self.removed)}",
        ]
        if self.problems:
            parts.append(f"problems: {'; '.join(self.problems)}")
        return ", ".join(parts)


@dataclass(frozen=True)
class WatchTickResult:
    """One composed tick: what was polled, dispatched, and drained from the queue."""

    report: TickReport
    dispatched: tuple[DispatchReport, ...] = ()
    drained: tuple[QueueDispatch, ...] = ()

    @property
    def idle(self) -> bool:
        """Whether this tick found nothing, started nothing, and drained nothing.

        Derived from the dispositions rather than from the report list: a
        successful poll yields one report per source whether or not it had a
        candidate item, so a truthiness test on the reports would call every tick
        busy and quietly stop asserting anything.
        """
        return (
            self.report.idle
            and not any(report.dispositions for report in self.dispatched)
            and not self.drained
        )


def build_screening_provider(host: TurnHost) -> ScreeningProvider:
    """The concrete provider that dispatches a screening turn on the host.

    Its own function so a caller with a different turn host substitutes one object
    rather than reimplementing the screener's construction, and so both screening
    call sites — intake and review feedback — reach the same one.
    """
    return DispatchedScreeningProvider(host)


def build_screener(
    graph: "EngineGraph",
    *,
    host: TurnHost,
    provider: ScreeningProvider | None = None,
    session_default: SessionDefault = SessionDefault(),
) -> IntakeScreener:
    """Construct the one screener the whole engine screens through.

    *host* is the turn host the screening turn is dispatched through. Required:
    a screener with nothing to dispatch through quarantines every item, which is
    the correct failure but not a configuration to arrive at by omission.

    One screener, not one per path: the intake dispatcher and the review-feedback
    watcher both take it, and a second instance would be a second screening path —
    the defect class every security defect in this engine has belonged to.
    """
    return IntakeScreener(
        graph.config,
        graph.state,
        provider=provider if provider is not None else build_screening_provider(host),
        audit=graph.audit,
        # The same durable per-run attribution the registry and the seeder use:
        # RunAccounting's own default is a RunCostSink over this state store, so
        # the screening turn's spend lands on the run row the ceiling and the kill
        # switch read rather than in a second idea of what a run cost.
        accounting=RunAccounting(graph.state),
        notifier=graph.notifier,
        session_default=session_default,
    )


def build_feedback_poster(graph: "EngineGraph") -> FeedbackPoster:
    """The item-feedback poster the dispatcher's ``claimed`` writeback needs.

    Built the same way ``RunMachine`` builds its own — same stores, same project —
    so the two sites take the one writeback claim and cannot say ``claimed``
    twice by two routes.
    """
    return FeedbackPoster(graph.state, graph.config, graph.audit, project=graph.project)


def watch_tick(
    graph: "EngineGraph",
    *,
    host: TurnHost,
    sources: Sequence[str] | None = None,
    screener: SeedScreener | None = None,
    runner: Any = None,
    drain: bool = True,
) -> WatchTickResult:
    """Poll, dispatch, and drain the queue — the watcher, wired end to end.

    The composition the whole watch path was waiting for. Every seam the dispatch
    entry points require is passed here and nothing is left to a default that
    would mean *skip*:

    * ``start`` is a :class:`~.dispatch.GatedStarter` over *graph*, so the
      prerequisite gate runs for each run before its session is opened, and the
      graph's own seeder never reaches a dispatch entry point unwrapped.
    * ``screener`` is one instance passed to *both* the poll path and the queue
      drain, because an item starts two ways.
    * ``cascade`` is the graph's review queue, which cancels a withdrawn item's
      in-flight run and archives its spec under one lock.
    * ``audit`` is the graph's log, so an edit ignored for dispatch is recorded.
    * ``feedback`` is the poster, so the ``claimed`` writeback — the one item
      event the run machine does not emit — actually reaches the tracker.

    An idle tick costs nothing: the poll runs a configured program, and with no
    candidate item the screener is never called, no session is opened, and no
    spend is recorded.
    """
    report = poll_tick(graph.config, sources=sources, runner=runner)
    if report.paused:
        # The kill switch is engaged. Nothing was polled, so there is nothing to
        # dispatch, and draining the queue would start exactly the work the switch
        # exists to stop.
        return WatchTickResult(report=report)
    resolved_screener = (
        screener if screener is not None else build_screener(graph, host=host)
    )
    starter = GatedStarter(graph)
    poster = build_feedback_poster(graph)
    dispatched = dispatch_tick(
        report,
        state=graph.state,
        config=graph.config,
        start=starter,
        screener=resolved_screener,
        cascade=graph.review_queue,
        audit=graph.audit,
        feedback=poster,
    )
    drained: tuple[QueueDispatch, ...] = ()
    if drain:
        drained = drain_queue(
            graph.state,
            graph.config,
            gate=caps_for(graph.state, graph.config),
            start=starter,
            # The same screener object as the poll path above. Passing a second
            # one here, or none, would leave every queued item screened by
            # something else or not at all.
            screener=resolved_screener,
            feedback=poster,
        )
    return WatchTickResult(report=report, dispatched=dispatched, drained=drained)


def build_review_feedback_watcher(
    graph: "EngineGraph",
    *,
    screener: CommentScreener,
    reviser: FeedbackReviser,
    delivery: RevisionDelivery,
) -> ReviewFeedbackWatcher:
    """Construct the review-feedback watcher over this graph.

    *screener* is the same object :func:`build_screener` produced for intake, not
    a second one: a reviewer's comment and an issue body are both untrusted text
    reaching a run, and two screening paths would be two places for a verdict to
    differ. *reviser* and *delivery* are required for the reason the library
    requires them — a watcher without a reviser claims comments and authors
    nothing, and one without a pipeline needs a second way to run a stage command.
    """
    return ReviewFeedbackWatcher(
        graph.config,
        graph.state,
        reviser=reviser,
        delivery=delivery,
        screener=screener,
        audit=graph.audit,
        notifier=graph.notifier,
    )


def review_feedback_definition(*, script_path: str = "") -> dict[str, Any]:
    """The script-cron definition for the review-feedback poll.

    Data for whoever registers crons, like the poll ticks' definitions. The
    entry point is a separate function in the same shim, so one installed file
    carries both schedules.
    """
    script = script_path or f"{tick_script_path()}:{REVIEW_FEEDBACK_ENTRY_POINT}"
    return {
        "name": REVIEW_FEEDBACK_JOB,
        "every": REVIEW_FEEDBACK_INTERVAL_S,
        "script": script,
        "message": "",
        "timeout": REVIEW_FEEDBACK_TIMEOUT_S,
        "silent": True,
        "enabled": True,
    }


def review_feedback_armed(store: ConfigStore | None = None) -> tuple[str, ...]:
    """Projects that armed the review-feedback watcher, in configuration order.

    Read here so the schedule carries the review-feedback job only where a
    project asked for it: the switch is per project by requirement, and a job
    that polls for a feature nobody armed is a scheduled no-op an operator has to
    explain.
    """
    resolved = store if store is not None else ConfigStore()
    projects: list[str] = []
    for source in load_sources(resolved, enabled_only=True):
        project = source.project
        if project and project not in projects and review_feedback_enabled(resolved, project):
            projects.append(project)
    return tuple(projects)


def watch_definitions(
    store: ConfigStore | None = None,
    *,
    script_path: str = "",
) -> tuple[dict[str, Any], ...]:
    """Every cron definition the watcher needs: a poll per source, plus feedback.

    One function so a caller cannot schedule the poll ticks and forget the
    review-feedback poll — the two schedules are one schedule.
    """
    resolved = store if store is not None else ConfigStore()
    script = script_path or f"{tick_script_path()}:{CRON_ENTRY_POINT}"
    definitions = list(cron_definitions(resolved, script_path=script))
    if review_feedback_armed(resolved):
        feedback_script = (
            f"{tick_script_path()}:{REVIEW_FEEDBACK_ENTRY_POINT}"
            if not script_path
            else script_path
        )
        definitions.append(review_feedback_definition(script_path=feedback_script))
    return tuple(definitions)


@dataclass
class _Reconciliation:
    """The three lists a reconciliation produces, accumulated as it walks."""

    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    def report(self, script: Path | None) -> ScheduleReport:
        return ScheduleReport(
            script=script,
            added=tuple(self.added),
            updated=tuple(self.updated),
            removed=tuple(self.removed),
            problems=tuple(self.problems),
        )


def _owned(job: Any) -> str:
    """The watcher job this cron job is, or empty for a job the watcher does not own."""
    name = str(getattr(job, "name", "") or "")
    if name == REVIEW_FEEDBACK_JOB or source_of_job(name):
        return name
    return ""


def _changed(job: Any, definition: Mapping[str, Any]) -> dict[str, Any]:
    """The updatable fields of *definition* that differ from the scheduled *job*.

    Only what ``CronService.update_job`` actually applies. The rest —
    ``script``, ``timeout``, and the paused flag — is handled by
    :func:`_needs_replacing`, because an update carrying them would report
    success while changing nothing.
    """
    fields = {
        "every_secs": definition["every"],
        "message": definition["message"],
        "silent": definition["silent"],
    }
    changed: dict[str, Any] = {}
    for name, value in fields.items():
        current = getattr(job, name, None)
        if current != value:
            changed[name] = value
    # A blank message cannot be updated onto a job (the writer ignores a falsy
    # one), and the poll ticks always carry their source name, so dropping it
    # here keeps a no-op update from being reported as a change forever.
    if not changed.get("message"):
        changed.pop("message", None)
    return changed


def _needs_replacing(job: Any, definition: Mapping[str, Any]) -> str:
    """Why this job has to be removed and re-added, or empty when it does not.

    The scheduler's update path cannot change a job's script or its timeout, so a
    shim that moved or a poll ceiling that grew is only reachable by replacing the
    job. Reported as a reason rather than a boolean so the schedule can say what
    it did.
    """
    if str(getattr(job, "script", "") or "") != definition["script"]:
        return "its script path changed"
    if int(getattr(job, "timeout", 0) or 0) != int(definition["timeout"]):
        return "its poll timeout changed"
    if bool(getattr(job, "user_paused", False)):
        return "it was left paused"
    return ""


async def install_watch_schedule(
    cron: Any,
    *,
    store: ConfigStore | None = None,
    directory: Path | None = None,
) -> ScheduleReport:
    """Install the tick shim and reconcile the watcher's cron jobs.

    Reconciled rather than created: the schedule is derived from configuration on
    every startup, so a source the operator disabled loses its job, a changed
    poll interval takes effect, and a job left behind by a failed removal does
    not keep polling. The tick re-reads ``enabled`` when it fires as well, which
    is the second half of the same guarantee.

    Uses the cron SDK's ``*_async`` mutators throughout: this runs from the app's
    startup hook, which is on the gateway event loop, where the synchronous
    mutators refuse rather than park it.

    Never raises. A schedule that could not be written is reported so the caller
    records it; a hook that raised would leave the app with no schedule and no
    explanation.
    """
    resolved = store if store is not None else ConfigStore()
    walk = _Reconciliation()
    script: Path | None = None
    try:
        script = install_tick_script(directory)
    except OSError as exc:
        # Without the shim there is nothing for a job to run, so the jobs are not
        # created either: a scheduled job pointing at a missing file fails every
        # interval and reports it as the source's problem.
        walk.problems.append(f"the tick script could not be installed: {exc}")
        return walk.report(None)

    try:
        definitions = {d["name"]: d for d in watch_definitions(resolved)}
    except Exception as exc:  # noqa: BLE001 - unreadable configuration schedules nothing
        walk.problems.append(f"the watch configuration could not be read: {exc}")
        return walk.report(script)

    existing: dict[str, Any] = {}
    for job in cron.list_jobs():
        owned = _owned(job)
        if owned:
            existing[owned] = job

    for name, definition in definitions.items():
        job = existing.get(name)
        try:
            if job is not None and _needs_replacing(job, definition):
                # Removed first, then re-added below: the scheduler cannot update
                # a job's script or timeout, so leaving the old one would keep
                # running the old file with the old ceiling.
                await cron.remove_job_async(job.id)
                walk.removed.append(name)
                job = None
            if job is None:
                await cron.add_job_async(
                    name,
                    definition["message"],
                    every_secs=definition["every"],
                    script=definition["script"],
                    timeout=definition["timeout"],
                    silent=definition["silent"],
                    enabled=definition["enabled"],
                )
                walk.added.append(name)
                continue
            changed = _changed(job, definition)
            if changed:
                await cron.update_job_async(job.id, **changed)
                walk.updated.append(name)
        except Exception as exc:  # noqa: BLE001 - one job's failure is not the schedule's
            walk.problems.append(f"{name}: {exc}")

    for name, job in existing.items():
        if name in definitions:
            continue
        try:
            await cron.remove_job_async(job.id)
            walk.removed.append(name)
        except Exception as exc:  # noqa: BLE001 - a job left behind is reported, not raised
            walk.problems.append(f"{name}: {exc}")

    report = walk.report(script)
    logger.info("spec-engine watch schedule reconciled: %s", report.describe())
    return report


def watch_job_names(store: ConfigStore | None = None) -> tuple[str, ...]:
    """The job names the watcher's schedule should hold, for a surface to show."""
    resolved = store if store is not None else ConfigStore()
    names = [cron_job_name(source.name) for source in load_sources(resolved, enabled_only=True)]
    if review_feedback_armed(resolved):
        names.append(REVIEW_FEEDBACK_JOB)
    return tuple(names)


def run_review_feedback_script(ctx: Any) -> None:
    """Scheduler entry point for the review-feedback poll.

    Costs nothing on every path it can take today. A project that armed nothing
    is skipped without reading a tracker, and an armed project is skipped *before*
    the poll because the two seams a fix round needs are not constructible in this
    process:

    * the **fix-round reviser**, which authors the change a comment asks for. It
      is a tool-enabled turn in the run's own host session, and the host bridge
      that opens one — a ``SessionOpener``/``TurnHost`` over the gateway's session
      manager — does not exist yet. ``SessionManager.get_or_create`` is async and
      returns no session key and no applied posture, so there is nothing here to
      build one from without inventing a posture read.
    * the **delivery pipeline**, which carries the fix through the project's
      configured stages.

    :func:`build_review_feedback_watcher` takes both, so the day either arrives
    this function constructs the watcher and ticks it — that is the only change
    needed, and the schedule is already in place. Until then the refusal is a log
    line rather than a delivered message: an operator does not need the same
    notice every five minutes, and the state is visible in the app's own surfaces.

    Raises the scheduler's control exceptions, imported here rather than at module
    scope so the engine stays importable without the host's cron subsystem.
    """
    from kiro_crew.cron_script import Skip

    armed = review_feedback_armed()
    if not armed:
        raise Skip()
    logger.info(
        "review feedback is armed for %s but no fix-round reviser or delivery pipeline is "
        "constructible in this process, so nothing was polled and nothing was spent",
        ", ".join(armed),
    )
    raise Skip()
