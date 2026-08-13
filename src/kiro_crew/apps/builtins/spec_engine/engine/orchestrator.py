"""The wave loop: which leaf tasks run, when, and what is written down after.

The tasks document's dependency graph says which leaves may run together. This
module walks it wave by wave, dispatches the leaves of one wave concurrently up
to the configured cap, and persists every task's status as it moves. Four things
about it are load-bearing.

**A wave order that cannot be read is a refusal, not a guess.** Dispatch order
is the only thing standing between "these tasks are independent" and two agents
editing the same code, so an unreadable, incomplete, or non-sequential graph
stops the loop with the reason. Falling back to document order would look like
working: the tasks all run, and the ones whose inputs were not built yet fail for
reasons that read as implementation defects.

**Task status is persisted by the loop, in batches, under one held lock.** The
store refuses a conflicting writer rather than queueing behind it, so two
finished tasks writing their own status at the same instant means one refusal.
A refused status that is then dropped is the expensive kind of lost write: the
work happened and was paid for, and a resumed run pays for it again. So the
worker threads never write status. The loop drains whatever finished together,
takes the spec lock once, and writes that batch through the handle it already
holds — the lock is not re-entrant, so the handle is passed down rather than
re-acquired.

**Every dispatch's role is resolved through the role resolver, per call.** The
host's own role-model map is a closed allowlist that silently drops a key it does
not know, so a spec role cannot be expressed there at all: agent, model, and
effort have to travel with each dispatch. The run resolves its plan once and each
dispatch reads it, which is what makes a subagent inherit the run's assignment
instead of resolving a fresh one mid-run.

**Isolation happens before the first task, through a broker.** A
delivery-authorized run claims a working tree of its own before anything is
implemented, and the claim is what turns "two runs in one tree" into a refusal
instead of two agents committing over each other. A pipeline built without the
broker still isolates and never refuses, which is why the construction here
passes one rather than accepting one.

**A successful implementation is not a completed task until it is approved.**
Every leaf that its worker reports done is reviewed on the review role's model
before the loop records it complete, and only an approving verdict makes it
complete. Any unsuccessful outcome — the implementation failing, the review
requiring changes, or an infrastructure failure raising out of either — retries
the leaf up to the configured limit and then fails it. The retry is per leaf and
runs on that leaf's own worker thread, so a leaf that exhausts its retries fails
alone: its independent siblings in the same wave finish, and the later waves
still run. There is one path to a complete leaf, and it goes through the verdict;
a run built without a reviewer cannot complete a task at all, which is why the
reviewer is required rather than defaulted.

**A run that becomes terminal takes its disposable workspace back.**
:meth:`WaveRunner.finish` is where a run reaches done, failed, or cancelled, so
it is where the workspace janitor retires the run's checkout — a worktree or a
temporary copy that is worth nothing once the run is over and is the reason disk
fills up if nobody removes it. Branches, commits, and published deployments are
left untouched; only the disposable materialization is swept, and only at a
terminal state, never for a parked run that will resume.
"""

from __future__ import annotations

import json
import logging
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from . import phases, structure
from .audit import AuditLog
from .budget import (
    BudgetGuard,
    CompletionReport,
    DispatchDecision,
    MeteringLedger,
    Notifier,
    RunAccounting,
    guard_for,
)
from .budget.switch import KillSwitch
from .config import ConfigStore
from .cross_document import (
    FIRST_WAVE_ID,
    WAVE_ID_KEY,
    WAVE_TASKS_KEY,
    WAVES_KEY,
)
from .delivery import (
    AuditRecorder,
    CommandRunner,
    DeliveryAuthority,
    DeliveryPipeline,
)
from .delivery import Notifier as DeliveryNotifier
from .delivery import (
    RunContext,
    StageResult,
    WorkspaceBroker,
    WorkspaceJanitor,
)
from .documents import DocumentKind
from .review_criteria import TestQualityAssessment
from .roles import Dispatch, RolePlan, SessionDefault, WorkKind
from .runs import (
    PARKED_STATES,
    TERMINAL_STATES,
    RunMachine,
    RunState,
    TaskStatus,
    is_legal,
)
from .state import SpecRef, StateStore, reject_spec_tree_path
from .structure import TASK_ID_RE, TaskPlan

logger = logging.getLogger(__name__)

#: Setting bounding how many leaves of one wave are dispatched at once.
WAVE_CONCURRENCY_SETTING = "concurrency.wave_max_tasks"

#: Setting bounding how many times one leaf is retried before it is failed. A
#: leaf that exhausts it fails without abandoning its independent siblings. The
#: registry owns the number; named here so the retry policy holds no literal.
TASK_RETRY_LIMIT_SETTING = "limits.task_retry_limit"

#: Directory under the state root that run workspaces are created in. Under the
#: state root rather than beside the spec: engine state lives outside the
#: interop contract, and a worktree inside a project's own tree would read as
#: untracked files in whatever that project is committing.
WORKSPACES_DIRNAME = "workspaces"

#: Prefix on the thread names the loop dispatches under, so a stack trace from a
#: worker says which run and which task it belongs to.
WORKER_THREAD_PREFIX = "spec-task"

# --- Audit event names -----------------------------------------------------

#: One wave's leaves were dispatched.
WAVE_DISPATCHED_EVENT = "spec.orchestrator.wave-dispatched"
#: One leaf finished, with the status that was persisted for it.
TASK_SETTLED_EVENT = "spec.orchestrator.task-settled"
#: One leaf's implementation was reviewed, with the verdict it received.
TASK_REVIEWED_EVENT = "spec.orchestrator.task-reviewed"
#: The wave loop stopped, for whatever reason.
EXECUTION_FINISHED_EVENT = "spec.orchestrator.execution-finished"
#: The run ended and its consumption was reported.
RUN_COMPLETED_EVENT = "spec.orchestrator.run-completed"

#: Initiator recorded for the state changes the loop makes on its own.
ORCHESTRATOR_INITIATOR = "orchestrator"


class ScheduleProblem(str, Enum):
    """Why a tasks document yields no wave schedule.

    Named cases rather than one "unreadable": each is repaired somewhere
    different, and the loop's refusal is the only place an operator hears about
    it, because the wave loop runs unattended.
    """

    NO_TASKS_DOCUMENT = "no_tasks_document"
    NO_GRAPH = "no_dependency_graph"
    MALFORMED_GRAPH = "malformed_dependency_graph"
    MALFORMED_WAVE = "malformed_wave"
    NON_SEQUENTIAL_WAVES = "non_sequential_wave_ids"
    UNSCHEDULABLE_TASK = "unschedulable_task"


@dataclass(frozen=True)
class Wave:
    """One wave: its declared identifier and the leaves it schedules, in order."""

    identifier: int
    tasks: tuple[str, ...]


@dataclass(frozen=True)
class WaveSchedule:
    """The wave order a tasks document declares, or why it declares none."""

    waves: tuple[Wave, ...] = ()
    problem: ScheduleProblem | None = None
    reason: str = ""

    @property
    def usable(self) -> bool:
        """Whether the loop may dispatch from this schedule."""
        return self.problem is None

    @property
    def scheduled_tasks(self) -> tuple[str, ...]:
        """Every leaf the schedule holds, in wave order."""
        return tuple(task for wave in self.waves for task in wave.tasks)


def _unusable(problem: ScheduleProblem, reason: str) -> WaveSchedule:
    return WaveSchedule(problem=problem, reason=reason)


def read_schedule(spec_dir: Path) -> WaveSchedule:
    """Read the wave schedule from the spec's tasks document."""
    text = phases.read_document(spec_dir, DocumentKind.TASKS)
    if text is None:
        return _unusable(
            ScheduleProblem.NO_TASKS_DOCUMENT,
            "the spec has no readable tasks document, so there are no leaves to dispatch",
        )
    return schedule_of(structure.parse_tasks(text))


def schedule_of(plan: TaskPlan) -> WaveSchedule:
    """Derive the wave schedule from an already-parsed tasks document.

    Deliberately strict where the validator is strict. Anything this refuses the
    validator already reports as a violation, so a spec that passes validation
    schedules, and a spec that does not is refused here rather than executed in
    an order nobody wrote. Dependencies declared alongside the waves are not
    consulted: the validator requires every edge to point at an earlier wave, so
    the wave order already carries them, and reading them again here would be a
    second answer to one question.
    """
    if plan.graph_block is None:
        return _unusable(
            ScheduleProblem.NO_GRAPH,
            "the tasks document declares no dependency graph, so no wave order is known",
        )
    try:
        graph = json.loads(plan.graph_block.body)
    except json.JSONDecodeError as error:
        return _unusable(
            ScheduleProblem.MALFORMED_GRAPH,
            f"the dependency graph is not readable JSON: {error.msg}",
        )
    if not isinstance(graph, dict) or not isinstance(graph.get(WAVES_KEY), list):
        return _unusable(
            ScheduleProblem.MALFORMED_GRAPH,
            f"the dependency graph is not an object carrying a {WAVES_KEY!r} list",
        )
    return _read_waves(graph[WAVES_KEY], plan)


def _read_waves(declared: Sequence[Any], plan: TaskPlan) -> WaveSchedule:
    leaves = {task.number for task in plan.leaves}
    waves: list[Wave] = []
    seen: dict[str, int] = {}
    for position, entry in enumerate(declared):
        expected = FIRST_WAVE_ID + position
        if not isinstance(entry, dict):
            return _unusable(
                ScheduleProblem.MALFORMED_WAVE,
                f"wave {position} is not an object carrying {WAVE_ID_KEY!r} and "
                f"{WAVE_TASKS_KEY!r}",
            )
        identifier = entry.get(WAVE_ID_KEY)
        tasks = entry.get(WAVE_TASKS_KEY)
        if isinstance(identifier, bool) or not isinstance(identifier, int):
            return _unusable(
                ScheduleProblem.MALFORMED_WAVE,
                f"wave {position} carries no integer {WAVE_ID_KEY!r}",
            )
        if not isinstance(tasks, list) or not tasks:
            return _unusable(
                ScheduleProblem.MALFORMED_WAVE,
                f"wave {identifier} schedules no tasks",
            )
        if identifier != expected:
            # Order is the whole product of this function, and a graph whose
            # identifiers disagree with its own order has two of them.
            return _unusable(
                ScheduleProblem.NON_SEQUENTIAL_WAVES,
                f"expected wave {expected} at position {position}, found {identifier}; "
                f"wave identifiers count from {FIRST_WAVE_ID} without gaps or repeats",
            )
        numbers: list[str] = []
        for task in tasks:
            if not isinstance(task, str) or not TASK_ID_RE.match(task):
                return _unusable(
                    ScheduleProblem.UNSCHEDULABLE_TASK,
                    f"wave {identifier} schedules {task!r}, which is not a task number",
                )
            if task in seen:
                return _unusable(
                    ScheduleProblem.UNSCHEDULABLE_TASK,
                    f"task {task} is scheduled in wave {seen[task]} and again in "
                    f"wave {identifier}",
                )
            if task not in leaves:
                return _unusable(
                    ScheduleProblem.UNSCHEDULABLE_TASK,
                    f"wave {identifier} schedules task {task}, which the tasks document "
                    "does not declare as a leaf",
                )
            seen[task] = identifier
            numbers.append(task)
        waves.append(Wave(identifier=identifier, tasks=tuple(numbers)))
    if not waves:
        return _unusable(
            ScheduleProblem.MALFORMED_GRAPH,
            "the dependency graph declares no waves",
        )
    return WaveSchedule(waves=tuple(waves))


# --- the dispatch seam -----------------------------------------------------


@dataclass(frozen=True)
class TaskResult:
    """What a worker made of one leaf task.

    A value rather than an exception for the ordinary failure, because the loop
    records it and continues: an exception would make one leaf's failure end the
    wave it shares with independent work.
    """

    ok: bool
    reason: str = ""


class TaskWorker(Protocol):
    """Implements one leaf task.

    Handed the routing decision rather than resolving one: agent, model, and
    effort are per-call parameters on the seams that spawn a subagent and start a
    turn, and a worker that looked them up itself would be a second answer to
    the question the run's role plan already answered.
    """

    def __call__(self, *, task: str, dispatch: Dispatch, context: RunContext) -> TaskResult: ...


@dataclass(frozen=True)
class ReviewVerdict:
    """A review role's judgement of one implemented leaf.

    ``approved`` is the whole gate: a task is not complete until it carries an
    approving verdict, so a reviewer that cannot decide returns ``approved=False``
    with a reason rather than a value the loop could read as approval.

    Test quality is judged by the same verdict, not a second gate: ``test_quality``
    carries the assessment against the review criteria, and a verdict cannot read
    ``approved`` while that assessment is unsatisfied. The fold is fail-closed and
    lives in one place, so a reviewer that judged the implementation sound but its
    tests inadequate produces a changes-required verdict through the one spelling
    the loop already reads — there is no route to completion that skips it.
    """

    approved: bool
    reason: str = ""
    test_quality: TestQualityAssessment = field(default_factory=TestQualityAssessment)

    def __post_init__(self) -> None:
        # A verdict that approves the implementation but carries unmet test-quality
        # findings is contradictory; the safe reading is changes-required. Coerce
        # here rather than trusting every caller to, so ``approved`` — the one
        # value the completion gate reads — can never be true past a failed
        # criterion, and a second path to approval cannot open by omission.
        if self.approved and not self.test_quality.satisfied:
            object.__setattr__(self, "approved", False)
            if not self.reason:
                object.__setattr__(self, "reason", "the tests did not meet the test quality criteria")


class Reviewer(Protocol):
    """Judges an implemented leaf on the review role's model.

    Handed the review dispatch the run's plan resolved, so the verdict runs on
    the review role's agent, model, and effort rather than the implementer's —
    the whole point of routing review to a smarter model. A worker that resolved
    its own role would be a second answer to the question the plan already
    answered, so the seam takes the dispatch rather than looking one up.
    """

    def __call__(self, *, task: str, dispatch: Dispatch, context: RunContext) -> ReviewVerdict: ...


@dataclass(frozen=True)
class LeafOutcome:
    """What the retry-and-review policy made of one leaf, across its attempts.

    The loop persists a status from this and records what happened. ``reviewed``
    says whether the final attempt reached a review verdict, so a failure at
    implementation is distinguishable from a failure at review; ``attempts`` is
    how many implementation rounds were spent, one when the first was approved.
    """

    ok: bool
    reason: str = ""
    attempts: int = 1
    reviewed: bool = False
    #: The test-quality assessment of the verdict that decided this leaf. Carried
    #: out of the worker thread so the loop — the one writer of the audit log —
    #: records the findings; empty when no review ran or the tests met the criteria.
    test_quality: TestQualityAssessment = field(default_factory=TestQualityAssessment)


@dataclass(frozen=True)
class TaskAttempt:
    """One dispatched leaf: what it was routed to, and how it ended."""

    task: str
    status: TaskStatus
    role: str = ""
    model: str = ""
    reason: str = ""
    #: Implementation rounds spent on this leaf, one when the first was approved.
    attempts: int = 1
    #: Whether the final attempt reached a review verdict.
    reviewed: bool = False

    @property
    def ok(self) -> bool:
        return self.status is TaskStatus.COMPLETE

    def detail(self) -> dict[str, Any]:
        return {
            "task": self.task,
            "status": self.status.value,
            "role": self.role,
            "model": self.model,
            "reason": self.reason,
            "attempts": self.attempts,
            "reviewed": self.reviewed,
        }


@dataclass(frozen=True)
class WaveReport:
    """One wave's outcome: what ran, what was already done, what never started."""

    wave: int
    attempts: tuple[TaskAttempt, ...] = ()
    #: Leaves the run had already finished, which a resumed run must not re-run.
    already_complete: tuple[str, ...] = ()
    #: Leaves this wave scheduled that were never dispatched, because dispatch
    #: stopped before reaching them.
    not_dispatched: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return all(attempt.ok for attempt in self.attempts) and not self.not_dispatched

    @property
    def failed(self) -> tuple[str, ...]:
        return tuple(attempt.task for attempt in self.attempts if not attempt.ok)


class ExecutionOutcome(str, Enum):
    """How the wave loop ended."""

    #: Every scheduled leaf is complete.
    COMPLETED = "completed"
    #: Every leaf was dispatched and at least one did not complete.
    FAILED = "failed"
    #: Dispatch stopped on the budget or the kill switch, with leaves left.
    HALTED = "halted"
    #: Nothing was dispatched: no usable schedule, or no workspace of its own.
    REFUSED = "refused"


@dataclass(frozen=True)
class RunCompletion:
    """What ending the run did: the state it ended in, and what it consumed."""

    final_state: RunState | None
    #: The consumption report, absent when the run has not ended.
    report: CompletionReport | None = None
    #: Whether this call moved the run into its final state.
    transitioned: bool = False
    #: Why no consumption was reported, when none was.
    reason: str = ""


@dataclass(frozen=True)
class ExecutionReport:
    """Everything one pass of the wave loop did."""

    outcome: ExecutionOutcome
    waves: tuple[WaveReport, ...] = ()
    isolation: StageResult | None = None
    #: Waves the loop never reached, so "not run" is distinguishable from "passed".
    not_reached: tuple[int, ...] = ()
    reason: str = ""
    #: Set once the run has been ended and its consumption reported.
    completion: RunCompletion | None = None

    @property
    def ok(self) -> bool:
        return self.outcome is ExecutionOutcome.COMPLETED

    @property
    def attempts(self) -> tuple[TaskAttempt, ...]:
        return tuple(attempt for wave in self.waves for attempt in wave.attempts)

    @property
    def failed_tasks(self) -> tuple[str, ...]:
        return tuple(attempt.task for attempt in self.attempts if not attempt.ok)

    def detail(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "waves": [
                {
                    "wave": wave.wave,
                    "attempts": [attempt.detail() for attempt in wave.attempts],
                    "already_complete": list(wave.already_complete),
                    "not_dispatched": list(wave.not_dispatched),
                }
                for wave in self.waves
            ],
            "not_reached": list(self.not_reached),
            "reason": self.reason,
        }


class WaveRunner:
    """Dispatches a run's leaf tasks wave by wave and records what happened.

    Every collaborator is required rather than defaulted. The budget guard is
    Engine_Floor and a loop that could be built without one would dispatch
    unbounded work whenever a caller forgot it; the role plan is what keeps a
    dispatch off the session default model by accident; the pipeline is what
    isolates the run's working tree before the first edit; the reviewer is what
    keeps a task from completing on an implementation nobody judged; the janitor
    is what takes the run's disposable checkout back when it ends. A seam that
    defaults to off delegates the decision to whoever writes the next caller.
    """

    def __init__(
        self,
        ref: SpecRef,
        run_id: str,
        *,
        machine: RunMachine,
        config: ConfigStore,
        plan: RolePlan,
        guard: BudgetGuard,
        pipeline: DeliveryPipeline,
        worker: TaskWorker,
        reviewer: Reviewer,
        janitor: WorkspaceJanitor,
        project: str | None = None,
        audit: AuditLog | None = None,
    ) -> None:
        if not run_id.strip():
            raise ValueError("a wave runner needs a run identifier")
        self._ref = ref
        self._run_id = run_id.strip()
        self._machine = machine
        self._config = config
        self._plan = plan
        self._guard = guard
        self._pipeline = pipeline
        self._worker = worker
        self._reviewer = reviewer
        self._janitor = janitor
        self._project = project
        self._audit = audit

    @property
    def run_id(self) -> str:
        return self._run_id

    @property
    def role_plan(self) -> RolePlan:
        """The assignments every dispatch of this run reads."""
        return self._plan

    @property
    def guard(self) -> BudgetGuard:
        """The run's budget guard.

        Exposed because a worker that creates a session has to stamp it onto the
        run: consumption from a session nobody stamped is spend the ceiling cannot
        see, and the worker is the only party that knows the session key it was
        given.
        """
        return self._guard

    @property
    def pipeline(self) -> DeliveryPipeline:
        """The delivery pipeline this run isolates through.

        Exposed so a caller that continues into delivery uses the pipeline the
        run was isolated by rather than building a second one — a second pipeline
        would carry a second broker, and the claim the first one made would be a
        conflict against the same run.
        """
        return self._pipeline

    # --- the loop ----------------------------------------------------------

    def run(self, context: RunContext) -> ExecutionReport:
        """Execute the run's tasks and then end the run, reporting what it cost."""
        return self.finish(self.execute(context))

    def execute(self, context: RunContext) -> ExecutionReport:
        """Isolate, then dispatch every wave in order.

        Does not end the run: :meth:`finish` is what moves it into a final state
        and reports what it consumed, and the two are separate so a delivery pass
        can sit between them. :meth:`run` is the whole sequence.

        Returns rather than raises for a refusal or a failure, so the report says
        what ran alongside what did not. A persistence failure is the exception:
        the recorded status is what a resumed run reads, so a status that could
        not be written fails the operation instead of leaving the loop to report
        progress that nothing recorded.
        """
        isolation = self._pipeline.isolate(context, run_id=self._run_id)
        if not isolation.ok:
            return self._finished(
                ExecutionReport(
                    outcome=ExecutionOutcome.REFUSED,
                    isolation=isolation,
                    reason=isolation.reason or f"the isolate stage {isolation.outcome.value}",
                )
            )
        schedule = self._schedule()
        if not schedule.usable:
            return self._finished(
                ExecutionReport(
                    outcome=ExecutionOutcome.REFUSED,
                    isolation=isolation,
                    reason=schedule.reason,
                )
            )

        reports: list[WaveReport] = []
        halt: DispatchDecision | None = None
        for index, wave in enumerate(schedule.waves):
            pending = self._pending(wave)
            already = tuple(task for task in wave.tasks if task not in pending)
            if not pending:
                reports.append(WaveReport(wave=wave.identifier, already_complete=already))
                continue
            report, halt = self._dispatch_wave(wave, pending, context, already)
            reports.append(report)
            if halt is not None:
                return self._finished(
                    ExecutionReport(
                        outcome=ExecutionOutcome.HALTED,
                        waves=tuple(reports),
                        isolation=isolation,
                        not_reached=tuple(
                            later.identifier for later in schedule.waves[index + 1 :]
                        ),
                        reason=halt.message,
                    )
                )
        failed = tuple(task for report in reports for task in report.failed)
        return self._finished(
            ExecutionReport(
                outcome=ExecutionOutcome.FAILED if failed else ExecutionOutcome.COMPLETED,
                waves=tuple(reports),
                isolation=isolation,
                reason=("tasks did not complete: " + ", ".join(failed) if failed else ""),
            )
        )

    def _dispatch_wave(
        self,
        wave: Wave,
        pending: tuple[str, ...],
        context: RunContext,
        already: tuple[str, ...],
    ) -> tuple[WaveReport, DispatchDecision | None]:
        """Run one wave's pending leaves, at most *cap* of them at a time.

        The window is refilled as leaves settle rather than being submitted in
        one go, so the budget is consulted before each dispatch instead of once
        for the whole wave: a ceiling reached halfway through a wave has to stop
        the leaves that have not started yet.
        """
        cap = self._cap()
        queue = list(pending)
        running: dict[Future[LeafOutcome], str] = {}
        attempts: list[TaskAttempt] = []
        routed: dict[str, Dispatch] = {}
        halt: DispatchDecision | None = None
        with ThreadPoolExecutor(
            max_workers=cap, thread_name_prefix=f"{WORKER_THREAD_PREFIX}-{self._run_id}"
        ) as pool:
            while queue or running:
                batch: list[str] = []
                while queue and len(running) + len(batch) < cap and halt is None:
                    decision = self._guard.authorize_dispatch()
                    if not decision.allowed:
                        halt = decision
                        break
                    batch.append(queue.pop(0))
                if batch:
                    # One lock for the whole batch. Marking each task in turn
                    # would take and drop the lock per task, which is the
                    # contention this batching exists to remove.
                    self.record_statuses({task: TaskStatus.IN_PROGRESS for task in batch})
                    for task in batch:
                        dispatch = self._plan.dispatch(WorkKind.TASK_IMPLEMENTATION, subagent=True)
                        routed[task] = dispatch
                        # Opened on the loop's own thread, so the in-flight count
                        # is never incremented from two workers at once.
                        self._guard.open_turn()
                        running[
                            pool.submit(
                                self._attempt, task=task, dispatch=dispatch, context=context
                            )
                        ] = task
                    self._record(
                        WAVE_DISPATCHED_EVENT,
                        {
                            "wave": wave.identifier,
                            "tasks": list(batch),
                            "concurrency_cap": cap,
                            "in_flight": len(running),
                        },
                    )
                if not running:
                    break
                done, _ = wait(set(running), return_when=FIRST_COMPLETED)
                settled: dict[str, TaskStatus] = {}
                results: dict[str, LeafOutcome] = {}
                for future in done:
                    task = running.pop(future)
                    self._guard.settle_turn()
                    result = self._result_of(future, task)
                    results[task] = result
                    settled[task] = TaskStatus.COMPLETE if result.ok else TaskStatus.FAILED
                # Everything that finished together is written under one lock, so
                # simultaneous completions coalesce into one writer instead of
                # one of them being refused and dropped.
                self.record_statuses(settled)
                for task, status in settled.items():
                    dispatch = routed[task]
                    outcome = results[task]
                    attempt = TaskAttempt(
                        task=task,
                        status=status,
                        role=dispatch.role,
                        model=dispatch.model,
                        reason=outcome.reason,
                        attempts=outcome.attempts,
                        reviewed=outcome.reviewed,
                    )
                    attempts.append(attempt)
                    # Record test-quality findings on their own event so a run's
                    # audit log says which criteria a task's tests failed, separate
                    # from the settled status. Only when findings exist: an approved
                    # leaf and a leaf that never reached review carry none.
                    if not outcome.test_quality.satisfied:
                        self._record(
                            TASK_REVIEWED_EVENT,
                            {
                                "wave": wave.identifier,
                                "task": task,
                                **outcome.test_quality.detail(),
                            },
                        )
                    self._record(TASK_SETTLED_EVENT, {"wave": wave.identifier, **attempt.detail()})
        return (
            WaveReport(
                wave=wave.identifier,
                attempts=tuple(attempts),
                already_complete=already,
                not_dispatched=tuple(queue),
            ),
            halt,
        )

    def _attempt(self, *, task: str, dispatch: Dispatch, context: RunContext) -> LeafOutcome:
        """Implement a leaf, review it, and retry the pair up to the limit.

        The whole retry-and-review policy for one leaf, run on that leaf's own
        worker thread. Each round implements, and on a successful implementation
        obtains a review verdict on the review role's model; only an approving
        verdict returns success. Any unsuccessful round — the implementation not
        completing, the review requiring changes, or either raising — is retried
        until the configured limit is spent, and then the leaf fails.

        Because this runs per leaf on its own thread, a leaf that exhausts its
        retries fails alone: its independent siblings in the same wave keep
        running, and the loop's later waves are untouched. Nothing here writes a
        status or an audit record — the loop persists and records what this
        returns, from its own thread, so the store and the audit log are written
        by one writer rather than raced by the pool.
        """
        limit = self._retry_limit()
        reason = ""
        reviewed = False
        attempt = 0
        # The assessment of the last review that ran, carried to the failure
        # return so the loop can record its findings; an implementation that never
        # reached review leaves it empty, which records nothing.
        assessment = TestQualityAssessment()
        while True:
            impl = self._implement(task, dispatch, context)
            if impl.ok:
                verdict = self._review(task, context)
                reviewed = True
                assessment = verdict.test_quality
                if verdict.approved:
                    return LeafOutcome(
                        ok=True,
                        reason=verdict.reason or "the implementation was approved",
                        attempts=attempt + 1,
                        reviewed=True,
                        test_quality=verdict.test_quality,
                    )
                reason = verdict.reason or "the review verdict required changes"
            else:
                reviewed = False
                assessment = TestQualityAssessment()
                reason = impl.reason or "the implementation did not complete"
            if attempt >= limit:
                return LeafOutcome(
                    ok=False,
                    reason=f"{reason} (after {attempt + 1} attempts)",
                    attempts=attempt + 1,
                    reviewed=reviewed,
                    test_quality=assessment,
                )
            attempt += 1

    def _implement(self, task: str, dispatch: Dispatch, context: RunContext) -> TaskResult:
        """Dispatch one implementation round, turning a raise into a failed round.

        A worker that raises is an infrastructure failure of this leaf, and it is
        turned into an unsuccessful result here so the retry policy treats it like
        any other unsuccessful round rather than tearing down the wave the leaf
        shares with independent work.
        """
        try:
            return self._worker(task=task, dispatch=dispatch, context=context)
        except Exception as exc:  # a worker's failure is this leaf's failure
            logger.warning(
                "task %s of run %s raised during implementation: %s", task, self._run_id, exc
            )
            return TaskResult(ok=False, reason=f"the task dispatch raised: {exc}")

    def _review(self, task: str, context: RunContext) -> ReviewVerdict:
        """Obtain a review verdict on the review role's model.

        The review dispatch is resolved from the run's own plan, so the verdict
        runs on the review role's agent, model, and effort. A reviewer that raises
        is a failed review, not an approval: it returns a non-approving verdict so
        an infrastructure failure in the reviewer can never be read as a pass.
        """
        review_dispatch = self._plan.dispatch(WorkKind.TASK_REVIEW, subagent=True)
        try:
            return self._reviewer(task=task, dispatch=review_dispatch, context=context)
        except Exception as exc:  # a reviewer's failure is not an approval
            logger.warning("review of task %s of run %s raised: %s", task, self._run_id, exc)
            return ReviewVerdict(approved=False, reason=f"the review dispatch raised: {exc}")

    def _retry_limit(self) -> int:
        """Retries one leaf earns before it fails, never below zero."""
        setting = self._config.effective(TASK_RETRY_LIMIT_SETTING, project=self._project)
        return max(0, int(setting.value))

    def _result_of(self, future: Future[LeafOutcome], task: str) -> LeafOutcome:
        """The leaf's outcome, or the failure it raised instead of returning one.

        The attempt loop already turns a raising worker or reviewer into an
        unsuccessful round, so a raise reaching here is the loop itself failing
        rather than a task's own dispatch. It is still one leaf's failure and must
        not take its independent siblings down, nor leave a dispatched leaf with
        no recorded outcome, so it becomes a failed status the same way.
        """
        try:
            return future.result()
        except Exception as exc:  # the attempt loop itself failed for this leaf
            logger.warning("task %s of run %s raised: %s", task, self._run_id, exc)
            return LeafOutcome(ok=False, reason=f"the task dispatch raised: {exc}")

    # --- completion --------------------------------------------------------

    def finish(self, report: ExecutionReport) -> ExecutionReport:
        """End the run, then report what it consumed.

        The consumption report is made exactly when the run has ended, and the
        state change comes first so the report names the state it ended in. A
        parked run is left alone: halted for budget is resumable, and reporting
        it as finished would spend the once-per-run notification on a run that
        has not finished.
        """
        state = self._machine.state_of(self._run_id)
        if state in PARKED_STATES:
            return replace(
                report,
                completion=RunCompletion(
                    final_state=state,
                    reason=(
                        f"run {self._run_id} is {state.value} and resumable, so its "
                        "consumption is not yet reported as final"
                    ),
                ),
            )
        transitioned = False
        final = state
        if state not in TERMINAL_STATES:
            final = RunState.DONE if report.ok else RunState.FAILED
            if not is_legal(state, final):
                return replace(
                    report,
                    completion=RunCompletion(
                        final_state=state,
                        reason=(
                            f"run {self._run_id} cannot move from {state.value} to "
                            f"{final.value}, so it has not ended"
                        ),
                    ),
                )
            self._machine.transition(
                self._ref,
                self._run_id,
                final,
                initiator=ORCHESTRATOR_INITIATOR,
                reason=report.reason or f"execution {report.outcome.value}",
            )
            transitioned = True
        # The run is terminal here — it was already, or it just moved. Its
        # disposable checkout is worth nothing now and is the reason disk fills
        # up if nobody sweeps it, so it is retired before the run is reported
        # done. Branches, commits, and deployments are left untouched; a parked
        # run returned above never reaches this.
        self._retire()
        completion = self._guard.report_completion()
        self._record(
            RUN_COMPLETED_EVENT,
            {
                "final_state": final.value,
                "outcome": report.outcome.value,
                "consumed_credits": completion.consumed_credits,
                "notified": completion.notified,
            },
        )
        return replace(
            report,
            completion=RunCompletion(
                final_state=final,
                report=completion,
                transitioned=transitioned,
            ),
        )

    # --- plumbing ----------------------------------------------------------

    def _schedule(self) -> WaveSchedule:
        return read_schedule(self._ref.spec_dir)

    def _pending(self, wave: Wave) -> tuple[str, ...]:
        """The wave's leaves that are not already finished, in schedule order.

        Read per wave rather than once, because a leaf checked off in the
        document while the previous wave ran is finished work either way, and the
        completed set already merges the run's own record with the checkbox.
        """
        complete = set(self._machine.completed_tasks(self._ref, self._run_id))
        return tuple(task for task in wave.tasks if task not in complete)

    def _cap(self) -> int:
        """In-wave parallelism, never below one."""
        setting = self._config.effective(WAVE_CONCURRENCY_SETTING, project=self._project)
        return max(1, int(setting.value))

    def record_statuses(self, statuses: Mapping[str, TaskStatus]) -> None:
        """Write a batch of task statuses under one held spec lock.

        Public because the batching is the contract rather than an
        implementation detail: anything layered on this loop that reports several
        tasks at once has to write them the same way. One acquisition per batch
        rather than one per status, and the handle is passed to each write rather
        than re-acquired -- the store's lock is not re-entrant, so a nested
        acquisition is refused by its own caller, and a status that is refused and
        then dropped is finished work a resumed run pays for twice.

        No lock is held while a task is running. The lock serialises writers of
        one spec, and holding it across a model turn would block every other
        writer for as long as the turn takes.
        """
        if not statuses:
            return
        with self._machine.store.lock(self._ref, owner=self._run_id) as handle:
            for task, status in statuses.items():
                self._machine.record_task_status(self._ref, self._run_id, task, status, lock=handle)

    def _finished(self, report: ExecutionReport) -> ExecutionReport:
        self._record(EXECUTION_FINISHED_EVENT, report.detail())
        return report

    def _retire(self) -> None:
        """Take the run's disposable materializations back at a terminal state.

        Best-effort like every other side effect the run leaves behind: the run
        state is primary, so a teardown that cannot complete is logged and does
        not unwind the completion the run already reached. The janitor itself
        keeps branches, commits, and deployments — it removes only the checkout.
        """
        try:
            self._janitor.retire_run(self._run_id)
        except Exception:  # cleanup failing must not lose the run's completion
            logger.warning("workspace teardown for run %s failed", self._run_id, exc_info=True)

    def _record(self, event: str, detail: dict[str, Any]) -> None:
        if self._audit is None:
            return
        self._audit.append(
            self._ref,
            event,
            run=self._run_id,
            initiator=ORCHESTRATOR_INITIATOR,
            detail=detail,
        )


def _audit_recorder(log: AuditLog, ref: SpecRef, run_id: str) -> AuditRecorder:
    """Adapt the spec's audit log to the pipeline's event-and-detail recorder.

    The pipeline does not own the spec's audit identity, so its stage events are
    recorded through the same log the rest of the run writes to rather than a
    second handle opened elsewhere.
    """

    def record(event: str, detail: dict[str, Any]) -> None:
        log.append(ref, event, run=run_id, detail=detail)

    return record


def workspace_root(state: StateStore, root: str | Path | None = None) -> Path:
    """Where run workspaces are created: ``<state root>/workspaces`` by default."""
    resolved = Path(root) if root is not None else state.root / WORKSPACES_DIRNAME
    reject_spec_tree_path(resolved)
    return resolved


def orchestrator_for(
    ref: SpecRef,
    run_id: str,
    *,
    state: StateStore,
    config: ConfigStore,
    authority: DeliveryAuthority,
    worker: TaskWorker,
    reviewer: Reviewer,
    project: str | None = None,
    session_default: SessionDefault = SessionDefault(),
    audit: AuditLog | None = None,
    headless: bool = False,
    notifier: Notifier | None = None,
    delivery_notifier: DeliveryNotifier | None = None,
    accounting: RunAccounting | None = None,
    ledger: MeteringLedger | None = None,
    kill_switch: KillSwitch | None = None,
    runner: CommandRunner | None = None,
    workspaces_root: str | Path | None = None,
    machine: RunMachine | None = None,
) -> WaveRunner:
    """Build the wave runner with everything it enforces actually constructed.

    This is the wiring point, and each piece it builds is inert unless something
    builds it: a pipeline with no broker isolates without ever refusing a shared
    tree, a dispatch with no role plan runs on the session default, a consumption
    report nobody asks for is never made, a delivery with no notifier tells nobody
    its outcome, and a run with no janitor leaves its checkout on disk for good.
    Constructing them here — rather than accepting them as optional arguments — is
    what makes those enforcements fire for every caller instead of for the ones
    that remembered.

    The reviewer is required rather than defaulted for the same reason, but a
    sharper one: it is the review gate, and a default would be a route to a
    completed task that no verdict ever judged. The caller supplies both the
    implementation worker and the reviewer — in the default build, the builtin
    implementation and review providers.

    Two ``Notifier`` seams meet here and they are not the same shape: the budget
    ceiling's ``notify(channel=, message=, detail=)`` and the delivery pipeline's
    ``send(...)``. ``HostNotifier`` satisfies both, so a production caller passes
    one object as both ``notifier`` and ``delivery_notifier``; they are separate
    parameters because converging the two protocols onto one shape would mean
    rewriting the budget module's call sites, which this construction point does
    not own. The delivery notifier is passed with no channel: configuration
    resolves the destination inside the notifier, and naming one here would take
    that decision away from the project.
    """
    if machine is None:
        machine = RunMachine(state, config, project=project, audit=audit)
    ws_root = workspace_root(state, workspaces_root)
    recorder = _audit_recorder(audit, ref, run_id) if audit is not None else None
    broker = WorkspaceBroker(state, root=ws_root)
    pipeline = DeliveryPipeline(
        config,
        authority=authority,
        project=project,
        runner=runner,
        audit=recorder,
        isolation=broker,
        notifier=delivery_notifier,
    )
    janitor = WorkspaceJanitor(state, root=ws_root, runner=runner, audit=recorder)
    return WaveRunner(
        ref,
        run_id,
        machine=machine,
        config=config,
        plan=RolePlan.for_run(config, project=project, session_default=session_default),
        guard=guard_for(
            run_id,
            ref,
            state=state,
            config=config,
            project=project,
            accounting=accounting,
            notifier=notifier,
            audit=audit,
            headless=headless,
            ledger=ledger,
            machine=machine,
            kill_switch=kill_switch,
        ),
        pipeline=pipeline,
        worker=worker,
        reviewer=reviewer,
        janitor=janitor,
        project=project,
        audit=audit,
    )
