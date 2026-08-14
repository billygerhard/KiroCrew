"""The run lifecycle: legal states, per-phase timeouts, and resume.

A run is the unit of work the engine reports on: one pass at one spec, from the
moment it is queued to the moment it is done, failed, or cancelled. This module
owns three things about it.

**The state set is a table, not a convention.** Every legal move lives in
:data:`TRANSITIONS`, and a move that is not in it is refused with the state it
was refused from. The alternative — each caller writing whichever state it
believes comes next — produces two failures that are invisible afterwards: a
state nothing can reach (so a surface renders a case that never happens) and a
state two callers enter for different reasons (so the run's history no longer
says what happened). Neither shows up as an exception, which is why the table is
the thing under test rather than the callers.

**A phase that overruns is marked stalled and notified.** A run that dies
mid-phase looks exactly like a run still working: same state, same row, no
output either way. The sweep is what tells them apart, and it does so on wall
clock against the phase's own configured ceiling. Stalled is a notification and
never an expiry — nothing here archives, cancels, or deletes anything on
elapsed time, and a stalled run resumes like any other parked run.

**Resume continues from what was persisted, at the granularity the phase has.**
Authoring is resumed at phase granularity: the run re-enters the document gate
it was working on, because a document is written or it is not. Execution is
resumed at task granularity: the run picks up at the next incomplete leaf, not
at the start of its wave. The granularity is the whole point — coarser redoes
model turns someone already paid for, finer would need per-turn state the engine
does not have. Getting it wrong in the other direction is worse: a resume that
skips an incomplete leaf reports success for work nobody did.

Budget accounting lives elsewhere. ``halted_budget`` is modelled here as an
ordinary transition that another component triggers, so the ceiling logic and
the state machine can be reasoned about — and changed — separately.
"""

from __future__ import annotations

import contextlib
import logging
import secrets
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Iterator, Mapping, Protocol

from . import phases, structure
from .audit import AuditLog
from .config import ConfigStore
from .documents import DocumentKind
from .state import (
    RunRecord,
    SpecLock,
    SpecLocked,
    SpecRef,
    StatePersistenceError,
    StateStore,
)

logger = logging.getLogger(__name__)


class RunState(str, Enum):
    """Every state a run can be in.

    Grouped by what a reader of a run list actually needs to know: whether work
    is happening (the four phases), whether it is parked and resumable, or
    whether the run is over.
    """

    #: Created, waiting for capacity. Has no timeout: a run waiting its turn
    #: behind a concurrency cap is working as designed, and stalling it would
    #: report the cap as a fault every time the queue is busy.
    QUEUED = "queued"
    AUTHORING = "authoring"
    #: Waiting at a human-reserved gate. This is the state the Review_Queue
    #: projects, so a run parked on a person is never indistinguishable from one
    #: parked on a machine.
    AWAITING_REVIEW = "awaiting_review"
    EXECUTING = "executing"
    DELIVERING = "delivering"
    DONE = "done"
    FAILED = "failed"
    #: Stopped at a budget ceiling, after in-flight turns. Resumable once an
    #: operator raises the ceiling; the accounting that triggers it is not here.
    HALTED_BUDGET = "halted_budget"
    CANCELLED = "cancelled"
    #: Exceeded its phase's configured wall clock. A watchdog observation, not a
    #: verdict: the work may still be alive, so this state resumes.
    STALLED = "stalled"

    @classmethod
    def parse(cls, raw: str | None) -> "RunState | None":
        """Return the state *raw* names, or ``None`` when it names none."""
        if not raw:
            return None
        try:
            return cls(raw)
        except ValueError:
            return None


#: The phases during which work is happening. Each has a configured wall clock,
#: which is what makes this tuple the set the timeout sweep looks at.
ACTIVE_PHASES: tuple[RunState, ...] = (
    RunState.AUTHORING,
    RunState.AWAITING_REVIEW,
    RunState.EXECUTING,
    RunState.DELIVERING,
)

#: Non-terminal states in which nothing is progressing. A parked run is resumed
#: back into the state it parked from, and is never advanced from where it sits.
PARKED_STATES: tuple[RunState, ...] = (RunState.STALLED, RunState.HALTED_BUDGET)

#: States a run never leaves. Nothing may transition out of one, so a finished
#: run's history cannot be rewritten by a late writer.
TERMINAL_STATES: tuple[RunState, ...] = (
    RunState.DONE,
    RunState.FAILED,
    RunState.CANCELLED,
)

#: The state every run starts in. One entry point keeps the graph rooted, so
#: "every state is reachable" is a property that can be checked rather than
#: assumed.
INITIAL_STATE = RunState.QUEUED

#: Setting holding each phase's wall clock ceiling. A phase absent from this map
#: has no timeout and is never stalled; a phase present in it is stalled on
#: nothing but its own configured value, read from the registry so that changing
#: a ceiling is a configuration edit.
PHASE_TIMEOUT_SETTINGS: Mapping[RunState, str] = {
    RunState.AUTHORING: "timeouts.authoring_s",
    RunState.AWAITING_REVIEW: "timeouts.awaiting_review_s",
    RunState.EXECUTING: "timeouts.executing_s",
    RunState.DELIVERING: "timeouts.delivering_s",
}

#: Setting naming the channel a stall notification routes to.
NOTIFY_CHANNEL_SETTING = "notify.channel"

#: Every legal move, keyed by the state being left.
#:
#: The shape carries the decisions worth arguing about:
#:
#: * A run enters at ``queued`` and starts either by authoring documents or, for
#:   a spec whose gates are already settled, by executing them. Delivery is
#:   never an entry: it consumes an execution's workspace.
#: * ``delivering`` returns to ``executing`` because a failing verify stage
#:   dispatches fix tasks, and those are tasks.
#: * ``awaiting_review`` returns to ``authoring`` because a changes-required
#:   verdict is a revision cycle rather than a failure.
#: * A parked run resumes into the state it parked from — including ``queued``,
#:   which is where a run halted before it started belongs — and can otherwise
#:   only be given up on. It cannot reach ``done`` without resuming first, so a
#:   completed run always has an active phase behind it.
#: * ``cancelled`` is reachable from every non-terminal state, because a
#:   triggering item can be cancelled at any moment.
TRANSITIONS: Mapping[RunState, frozenset[RunState]] = {
    RunState.QUEUED: frozenset(
        {
            RunState.AUTHORING,
            RunState.EXECUTING,
            RunState.HALTED_BUDGET,
            RunState.CANCELLED,
            RunState.FAILED,
        }
    ),
    RunState.AUTHORING: frozenset(
        {
            RunState.AWAITING_REVIEW,
            RunState.EXECUTING,
            RunState.DONE,
            RunState.STALLED,
            RunState.HALTED_BUDGET,
            RunState.CANCELLED,
            RunState.FAILED,
        }
    ),
    RunState.AWAITING_REVIEW: frozenset(
        {
            RunState.AUTHORING,
            RunState.EXECUTING,
            RunState.DELIVERING,
            RunState.DONE,
            RunState.STALLED,
            RunState.HALTED_BUDGET,
            RunState.CANCELLED,
            RunState.FAILED,
        }
    ),
    RunState.EXECUTING: frozenset(
        {
            RunState.AWAITING_REVIEW,
            RunState.DELIVERING,
            RunState.DONE,
            RunState.STALLED,
            RunState.HALTED_BUDGET,
            RunState.CANCELLED,
            RunState.FAILED,
        }
    ),
    RunState.DELIVERING: frozenset(
        {
            RunState.EXECUTING,
            RunState.AWAITING_REVIEW,
            RunState.DONE,
            RunState.STALLED,
            RunState.HALTED_BUDGET,
            RunState.CANCELLED,
            RunState.FAILED,
        }
    ),
    RunState.STALLED: frozenset(
        {
            # QUEUED is unreachable in practice and kept deliberately: a resume
            # returns a parked run to the state it parked from, and QUEUED has no
            # timeout, so nothing can stall out of it. Listing every resumable
            # state rather than the subset that can currently stall means adding
            # a timeout to a phase does not also require remembering to widen
            # this set, which is the kind of omission that turns a resumable park
            # into a stranded run.
            RunState.QUEUED,
            RunState.AUTHORING,
            RunState.AWAITING_REVIEW,
            RunState.EXECUTING,
            RunState.DELIVERING,
            RunState.CANCELLED,
            RunState.FAILED,
        }
    ),
    RunState.HALTED_BUDGET: frozenset(
        {
            RunState.QUEUED,
            RunState.AUTHORING,
            RunState.AWAITING_REVIEW,
            RunState.EXECUTING,
            RunState.DELIVERING,
            RunState.CANCELLED,
            RunState.FAILED,
        }
    ),
    RunState.DONE: frozenset(),
    RunState.FAILED: frozenset(),
    RunState.CANCELLED: frozenset(),
}


class TaskStatus(str, Enum):
    """A leaf task's status inside one run.

    ``FAILED`` is not terminal for the task: the retry limit decides that, and a
    resumed run treats a failed leaf as incomplete because unfinished work is
    unfinished however it stopped.
    """

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETE = "complete"
    FAILED = "failed"

    @classmethod
    def parse(cls, raw: Any) -> "TaskStatus | None":
        if not isinstance(raw, str):
            return None
        try:
            return cls(raw)
        except ValueError:
            return None


class ResumeGranularity(str, Enum):
    """How finely a phase's progress was persisted."""

    #: Authoring and the phases around it: the unit is a document gate.
    PHASE = "phase"
    #: Execution: the unit is a leaf task.
    TASK = "task"


#: Keys this module owns inside a run's ``detail`` object. The column is shared,
#: and ``update_run`` merges rather than replaces, so every writer namespaces
#: what it owns instead of assuming it is alone.
DETAIL_PHASE_ENTERED = "phase_entered_ts"
DETAIL_PARKED_FROM = "parked_from"
DETAIL_TASKS = "tasks"
#: How many authoring revision cycles a run has spent at each review gate, keyed
#: by gate name. The count is per gate rather than per run because the revision
#: limit is per gate: a spec re-reviewed at design after its requirements were
#: settled starts design's count at zero. Written by the review feedback loop
#: only, so it lives beside :data:`DETAIL_TASKS` under this module's namespace.
DETAIL_REVISION_CYCLES = "revision_cycles"
#: Gate names at which a run has exhausted its revision cycles and been marked
#: needing human attention, so the loop dispatches no further revision turn for
#: that gate. A list rather than a flag because a run reviewed across several
#: gates can exhaust one without the others.
DETAIL_REVISION_EXHAUSTED = "revision_exhausted"
#: How many fix rounds the review-feedback watcher has dispatched for this run
#: from comments on its review artifact. Per run rather than per gate, because a
#: delivery review is one artifact rather than a sequence of document gates, and
#: the count is what the retry bound is measured against.
DETAIL_FEEDBACK_CYCLES = "feedback_cycles"
#: True once a review-feedback bound was reached -- the cycle limit or the budget
#: ceiling -- so no further fix is dispatched from a comment until a person acts.
#: A flag rather than a list because the bound is per run like the count.
DETAIL_FEEDBACK_NEEDS_HUMAN = "feedback_needs_human"
#: Reviewer comments held for human release: refused because the commenter's own
#: submitter class may not drive a dispatch, or because screening held them. Ids
#: only -- the comment text is never copied into the run row.
DETAIL_FEEDBACK_QUARANTINED = "feedback_quarantined"
#: Consecutive failed dispatch attempts per reviewer comment id, for the comments
#: a host seam could not start a fix round for. A count here is why a comment is
#: retried on a later tick rather than treated as already handled; it is cleared
#: when the comment is finally held for a person, so a held comment carries no
#: stale count to resume from. Ids and integers only -- never comment text.
DETAIL_FEEDBACK_FAILURES = "feedback_failures"

#: Prefix on a generated run identifier, so a run id is recognisable in a
#: session name, a log line, or a metering record.
RUN_ID_PREFIX = "run-"

#: Bytes of randomness in a generated run identifier.
_RUN_ID_BYTES = 8

# --- Audit event names -----------------------------------------------------

RUN_CREATED_EVENT = "spec.run.created"
RUN_TRANSITIONED_EVENT = "spec.run.transitioned"
RUN_TRANSITION_REFUSED_EVENT = "spec.run.transition-refused"
RUN_STALLED_EVENT = "spec.run.stalled"
RUN_RESUMED_EVENT = "spec.run.resumed"
#: Recorded when a stall notification could not be delivered. The run stays
#: stalled: state is primary, delivery is best-effort.
RUN_NOTIFY_FAILED_EVENT = "spec.run.notification-failed"


def lifecycle_event_for(from_state: RunState, to_state: RunState) -> str | None:
    """The item-feedback event a transition emits, or ``None`` when it emits none.

    Only the states an item's watchers care about map to an event, and each maps
    to exactly one so the writeback ledger's at-most-once key is stable: a run
    that re-enters ``awaiting_review`` on a revision cycle posts that event once,
    not once per cycle.

    ``failed`` and ``refused`` are both the terminal ``failed`` state, told apart
    by where the run came from. A run that never left ``queued`` failed before it
    authored anything -- an execution gate or a prerequisite refused it -- which
    reads to the item's watchers as *refused*, the request declined. A run that
    failed from any working phase did work that then broke, which reads as
    *failed*. The distinction is drawn from the transition rather than the reason
    string because the reason is prose a human wrote and the from-state is the
    machine's own record of whether the run ever ran.
    """
    if to_state is RunState.AWAITING_REVIEW:
        return "awaiting_review"
    if to_state is RunState.DONE:
        return "completed"
    if to_state is RunState.FAILED:
        return "refused" if from_state is RunState.QUEUED else "failed"
    return None


class RunError(Exception):
    """Base class for run lifecycle failures."""


class UnknownRun(RunError):
    """No run with that identifier is recorded."""


class IllegalTransition(RunError):
    """A move that is not in the transition table was refused.

    Carries both ends and the legal set, so a caller can report what it tried
    and what it could have done instead of retrying blind.
    """

    def __init__(self, run_id: str, from_state: RunState, to_state: RunState) -> None:
        self.run_id = run_id
        self.from_state = from_state
        self.to_state = to_state
        self.allowed = allowed_transitions(from_state)
        legal = ", ".join(sorted(state.value for state in self.allowed)) or "nothing"
        super().__init__(
            f"run {run_id} cannot move from {from_state.value} to {to_state.value}; "
            f"legal moves are: {legal}"
        )


def allowed_transitions(state: RunState) -> frozenset[RunState]:
    """The states *state* may legally move to. Empty for a terminal state."""
    return TRANSITIONS[state]


def is_legal(from_state: RunState, to_state: RunState) -> bool:
    """Whether moving from *from_state* to *to_state* is in the table."""
    return to_state in TRANSITIONS[from_state]


def timeout_setting(state: RunState) -> str | None:
    """The setting holding *state*'s wall clock, or ``None`` when it has none."""
    return PHASE_TIMEOUT_SETTINGS.get(state)


def run_state_of(record: RunRecord) -> RunState:
    """The state *record* holds, or raise when the column holds a foreign value.

    A row whose state this module does not know is not coerced to something
    plausible: every writer goes through the table, so an unrecognised value
    means a schema or version mismatch, and guessing would resume a run under a
    lifecycle nobody wrote.
    """
    state = RunState.parse(record.state)
    if state is None:
        raise RunError(f"run {record.run_id} holds an unrecognised state: {record.state!r}")
    return state


def phase_entered_ts(record: RunRecord) -> str:
    """When the run entered its current state.

    Falls back to the row's creation timestamp, which is when a run that has
    never transitioned entered ``queued``. Deliberately not ``updated_ts``: that
    moves on every write, including a cost update, so a run whose cost is
    refreshed every minute would never appear to overrun its phase.
    """
    recorded = record.detail.get(DETAIL_PHASE_ENTERED)
    return recorded if isinstance(recorded, str) and recorded else record.created_ts


def parked_from(record: RunRecord) -> RunState | None:
    """The state a parked run will resume into, when one was recorded."""
    return RunState.parse(record.detail.get(DETAIL_PARKED_FROM))


def task_statuses(record: RunRecord) -> dict[str, TaskStatus]:
    """The run's persisted per-leaf statuses, keyed by task number."""
    stored = record.detail.get(DETAIL_TASKS)
    if not isinstance(stored, Mapping):
        return {}
    parsed: dict[str, TaskStatus] = {}
    for number, raw in stored.items():
        status = TaskStatus.parse(raw)
        if status is not None:
            parsed[str(number)] = status
    return parsed


def revision_cycles(record: RunRecord) -> dict[str, int]:
    """The run's spent revision-cycle count per review gate, keyed by gate name.

    A gate absent from the map has spent none. Values are coerced defensively:
    a bool is not a count (``isinstance(True, int)`` is true, so it is excluded),
    and a negative or non-integer value is dropped rather than trusted, because
    the count gates whether another revision turn is dispatched.
    """
    stored = record.detail.get(DETAIL_REVISION_CYCLES)
    if not isinstance(stored, Mapping):
        return {}
    cycles: dict[str, int] = {}
    for gate, raw in stored.items():
        if isinstance(gate, str) and isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0:
            cycles[gate] = raw
    return cycles


def revision_exhausted_gates(record: RunRecord) -> frozenset[str]:
    """Gates at which the run was marked needing human attention.

    Once a gate is here the feedback loop dispatches no further revision turn for
    it: the run waits for a person. Read defensively, since a foreign value in
    the shared detail column must not read as an exhausted gate.
    """
    stored = record.detail.get(DETAIL_REVISION_EXHAUSTED)
    if not isinstance(stored, list):
        return frozenset()
    return frozenset(gate for gate in stored if isinstance(gate, str) and gate)


def feedback_cycles(record: RunRecord) -> int:
    """How many review-feedback fix rounds the run has dispatched.

    Read defensively, because the count is what the retry bound is measured
    against: a bool is not a count (``isinstance(True, int)`` is true, so it is
    excluded), and a negative or non-integer value reads as none spent rather than
    being trusted. A foreign value in the shared detail column must not be able to
    raise the bound.
    """
    stored = record.detail.get(DETAIL_FEEDBACK_CYCLES)
    if isinstance(stored, bool) or not isinstance(stored, int) or stored < 0:
        return 0
    return stored


def feedback_needs_human(record: RunRecord) -> bool:
    """Whether a review-feedback bound parked this run for a person.

    Only an explicit boolean true counts. Anything else reads as not parked, so a
    stray value cannot make a run look attended-to when nothing bounded it.
    """
    return record.detail.get(DETAIL_FEEDBACK_NEEDS_HUMAN) is True


def feedback_quarantined(record: RunRecord) -> tuple[str, ...]:
    """Reviewer comment ids held for human release, in the order they were held."""
    stored = record.detail.get(DETAIL_FEEDBACK_QUARANTINED)
    if not isinstance(stored, list):
        return ()
    return tuple(item for item in stored if isinstance(item, str) and item)


def feedback_failures(record: RunRecord) -> dict[str, int]:
    """Consecutive failed dispatch attempts per reviewer comment id.

    Filtered on the way out rather than trusted: a run row is merged into by
    several writers, so a malformed or hand-edited value reads as no failures --
    which retries once more, the safe direction, instead of raising inside a poll.
    """
    stored = record.detail.get(DETAIL_FEEDBACK_FAILURES)
    if not isinstance(stored, dict):
        return {}
    return {
        key: int(value)
        for key, value in stored.items()
        if isinstance(key, str) and key and isinstance(value, int) and value > 0
    }


def new_run_id() -> str:
    """Generate a run identifier."""
    return RUN_ID_PREFIX + secrets.token_hex(_RUN_ID_BYTES)


@dataclass(frozen=True)
class ResumePoint:
    """Where a run continues from, and at what granularity.

    ``gate`` is set for the document phases and ``task`` for execution; the
    other is ``None``, because a resume point that carried both would leave the
    caller choosing which one to honour.
    """

    run_id: str
    state: RunState
    granularity: ResumeGranularity
    gate: str | None = None
    task: str | None = None
    #: Leaf tasks a resumed execution must not run again.
    completed_tasks: tuple[str, ...] = ()
    #: Why there is nothing finer to point at, when there is not.
    reason: str = ""

    @property
    def target(self) -> str:
        """What to continue with, for a surface that renders one line per run."""
        return self.task or self.gate or self.state.value

    def to_json_object(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "state": self.state.value,
            "granularity": self.granularity.value,
            "gate": self.gate,
            "task": self.task,
            "completed_tasks": list(self.completed_tasks),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class StallNotice:
    """One run found to have overrun its phase, and what was done about it."""

    run_id: str
    project: str
    spec: str
    #: The phase that overran. The run's state is ``stalled`` once this exists.
    phase: RunState
    channel: str
    elapsed_s: float
    timeout_s: int
    entered_ts: str
    #: Whether the notification reached its channel. False leaves the run
    #: stalled: a failed notification never unwinds run state.
    notified: bool = False
    #: Why delivery failed, when it did.
    error: str = ""

    def message(self) -> str:
        """The human-readable line a channel renders."""
        return (
            f"Spec run {self.run_id} ({self.spec}) has been {self.phase.value} for "
            f"{int(self.elapsed_s)}s, past its {self.timeout_s}s limit, and is "
            "marked stalled. It can be resumed; nothing was cancelled."
        )

    def to_json_object(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "project": self.project,
            "spec": self.spec,
            "phase": self.phase.value,
            "channel": self.channel,
            "elapsed_s": round(self.elapsed_s, 3),
            "timeout_s": self.timeout_s,
            "entered_ts": self.entered_ts,
            "notified": self.notified,
            "error": self.error,
        }


class Notifier(Protocol):
    """Delivers a stall notice to its channel. The seam tests substitute."""

    def __call__(self, notice: StallNotice) -> None: ...


def log_notification(notice: StallNotice) -> None:
    """Default notifier: record the notice where the operator's logs are.

    Every install has logs, and a default that needs configuration to work at
    all would make the stall silent on exactly the installs nobody has set up.
    """
    logger.warning("[%s] %s", notice.channel, notice.message())


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class RunMachine:
    """Creates runs, moves them between states, stalls them, and resumes them.

    Both stores are required rather than defaulted: the state store decides
    where run state lands, the config store decides which timeouts apply, and a
    silently constructed default for either would write to the real data home
    from a caller that thought it was passing everything.
    """

    def __init__(
        self,
        store: StateStore,
        config: ConfigStore,
        *,
        project: str | None = None,
        audit: AuditLog | None = None,
        notifier: Notifier | None = None,
        clock: Callable[[], datetime] | None = None,
        feedback: Any | None = None,
    ) -> None:
        self._store = store
        self._config = config
        #: Configured project key used to resolve per-project settings. Not a
        #: filesystem path: the settings layer keys projects by configured name.
        self._project = project
        self._audit = audit
        self._notifier: Notifier = notifier if notifier is not None else log_notification
        self._clock: Callable[[], datetime] = clock if clock is not None else _utc_now
        #: Item-feedback poster, built lazily on first use unless one is injected.
        #: Constructed from this machine's own stores rather than injected in
        #: production, so a run's lifecycle transitions write back to its
        #: triggering item wherever the machine is constructed -- the
        #: orchestrator, the review queue, a driver -- without each of those
        #: having to remember to wire it. An injected poster (a test's, or a
        #: caller that already holds one) is used as-is. Requires the audit log,
        #: because a writeback records its outcome there and a poster that could
        #: post without recording would drop the one trace that a tracker refused.
        self._feedback: Any = feedback
        self._feedback_ready = feedback is not None

    # ------------------------------------------------------------------ clock

    @property
    def store(self) -> StateStore:
        """The state store this machine writes through.

        Exposed so a component layered on the machine — the review queue, its
        archival rules — reads and locks through the same store rather than being
        handed a second one. Two stores rooted differently is a class of bug that
        looks like state vanishing: the write lands, and the reader is looking at
        another database.
        """
        return self._store

    @property
    def config(self) -> ConfigStore:
        """The config store this machine resolves limits and timeouts through.

        Exposed for the same reason as :attr:`store`: a component layered on the
        machine — the review feedback loop reading the revision-cycle limit —
        resolves settings through the one config the machine already holds, with
        the machine's own project scope, rather than being handed a second store
        that could resolve a different effective value for the same key.
        """
        return self._config

    @property
    def project(self) -> str | None:
        """The configured project key this machine scopes per-project settings to."""
        return self._project

    def now(self) -> datetime:
        """The current instant, from the injected clock."""
        return self._clock()

    def _now_iso(self) -> str:
        """Timestamp in the form persisted records use."""
        return self.now().replace(microsecond=0).isoformat()

    # ----------------------------------------------------------------- create

    def create(
        self,
        ref: SpecRef,
        *,
        run_id: str | None = None,
        source: str | None = None,
        item_id: str | None = None,
        posture: str | None = None,
        detail: Mapping[str, Any] | None = None,
        initiator: str | None = None,
        lock: SpecLock | None = None,
    ) -> RunRecord:
        """Record a new run in :data:`INITIAL_STATE`.

        A run cannot be created directly in a later state. Starting work is a
        transition, so the row carries when the phase began and the audit log
        carries who started it — neither of which exists for a run that
        materialised mid-lifecycle.
        """
        identifier = run_id or new_run_id()
        payload: dict[str, Any] = dict(detail or {})
        payload[DETAIL_PHASE_ENTERED] = self._now_iso()
        with self._held(ref, lock, owner=initiator or identifier):
            record = self._store.create_run(
                identifier,
                ref,
                state=INITIAL_STATE.value,
                source=source,
                item_id=item_id,
                posture=posture,
                detail=payload,
            )
        self.append_audit(
            ref,
            RUN_CREATED_EVENT,
            run=identifier,
            initiator=initiator,
            detail={"state": INITIAL_STATE.value, "source": source, "item_id": item_id},
        )
        return record

    # ------------------------------------------------------------- transition

    def transition(
        self,
        ref: SpecRef,
        run_id: str,
        to_state: RunState,
        *,
        initiator: str | None = None,
        reason: str = "",
        detail: Mapping[str, Any] | None = None,
        lock: SpecLock | None = None,
    ) -> RunRecord:
        """Move a run to *to_state*, or refuse the move.

        The table is consulted before anything is written, so a refused move
        leaves the row exactly as it was: the failure this guards against is a
        state that was applied and then reported as illegal, which is
        unrecoverable because nothing recorded what the previous state had been.

        The read and the check happen INSIDE the lock, with the write, because
        the store is shared across threads and processes. Checking first and
        locking second lets two writers validate against the same state and both
        pass: the loser then takes the lock the winner has released and commits a
        move whose from-state no longer exists. A cancel followed by a ``done``
        writer that had already read ``executing`` would move a terminal run,
        which is the one thing this module promises cannot happen.
        """
        refusal: tuple[RunState, RunState] | None = None
        with self._held(ref, lock, owner=initiator or run_id):
            record = self.get(run_id)
            from_state = run_state_of(record)
            if not is_legal(from_state, to_state):
                refusal = (from_state, to_state)
            else:
                payload: dict[str, Any] = dict(detail or {})
                payload[DETAIL_PHASE_ENTERED] = self._now_iso()
                # Where a park resumes to is recorded when the park happens,
                # because that is the only moment the previous state is still
                # known. Leaving a park clears it, so a stale value cannot send a
                # later resume somewhere the run never was.
                payload[DETAIL_PARKED_FROM] = from_state.value if to_state in PARKED_STATES else ""
                updated = self._store.update_run(run_id, state=to_state.value, detail=payload)

        # Audit outside the lock: the decision is already durable either way, and
        # holding the lock across a second file write would make every writer
        # wait on the audit log rather than on the state it actually contends for.
        if refusal is not None:
            from_state, to_state = refusal
            self.append_audit(
                ref,
                RUN_TRANSITION_REFUSED_EVENT,
                run=run_id,
                initiator=initiator,
                detail={
                    "from": from_state.value,
                    "to": to_state.value,
                    "allowed": sorted(state.value for state in allowed_transitions(from_state)),
                    "reason": reason,
                },
            )
            raise IllegalTransition(run_id, from_state, to_state)
        self.append_audit(
            ref,
            RUN_TRANSITIONED_EVENT,
            run=run_id,
            initiator=initiator,
            detail={"from": from_state.value, "to": to_state.value, "reason": reason},
        )
        self._observe_transition(ref, updated, from_state, to_state, initiator=initiator)
        return updated

    # ------------------------------------------------------------------ reads

    def get(self, run_id: str) -> RunRecord:
        """The run's record, or raise :class:`UnknownRun`."""
        record = self._store.get_run(run_id)
        if record is None:
            raise UnknownRun(f"no such run: {run_id!r}")
        return record

    def state_of(self, run_id: str) -> RunState:
        """The run's current state."""
        return run_state_of(self.get(run_id))

    def active_runs(self) -> tuple[RunRecord, ...]:
        """Every run in a phase where work should be happening."""
        states = [phase.value for phase in ACTIVE_PHASES]
        return tuple(self._store.list_runs(states=states))

    def phase_timeout_s(self, state: RunState) -> int | None:
        """The wall clock in force for *state*, or ``None`` when it has none."""
        key = timeout_setting(state)
        if key is None:
            return None
        return int(self._config.effective(key, project=self._project).value)

    def elapsed_in_phase_s(self, record: RunRecord) -> float:
        """Seconds since the run entered its current state.

        A timestamp in the future — a clock that moved backwards, a row written
        by a host whose clock is ahead — yields zero rather than a negative
        elapsed time, so skew delays a stall instead of triggering one.
        """
        entered = _parse_ts(phase_entered_ts(record))
        if entered is None:
            return 0.0
        return max(0.0, (self.now() - entered).total_seconds())

    def overruns_phase(self, record: RunRecord) -> bool:
        """Whether the run has been in its current state past its ceiling."""
        state = run_state_of(record)
        timeout = self.phase_timeout_s(state)
        if timeout is None:
            return False
        return self.elapsed_in_phase_s(record) >= timeout

    # ------------------------------------------------------------------ stall

    def sweep_stalled(self) -> tuple[StallNotice, ...]:
        """Mark every overrunning run stalled and notify, oldest phase first.

        Runs whose spec is archived are left alone: archival is a person saying
        they are finished looking at that spec, and a notification about it is
        noise they cannot act on.

        A run whose spec is being written by someone else right now is skipped
        rather than waited for. The lock holder is mid-change, its write may well
        be the one that ends the phase, and the run is still over its ceiling on
        the next sweep if it is not.
        """
        refs = {record.spec_key: record.ref for record in self._store.list_specs()}
        notices: list[StallNotice] = []
        for record in self.active_runs():
            ref = refs.get(record.spec_key)
            if ref is None:
                continue
            if not self.overruns_phase(record):
                continue
            try:
                notices.append(self._stall(ref, record))
            except SpecLocked as exc:
                logger.debug(
                    "skipping stall of run %s: spec %r is held by %s",
                    record.run_id,
                    ref.name,
                    exc.holder or "another writer",
                )
        return tuple(notices)

    def _stall(self, ref: SpecRef, record: RunRecord) -> StallNotice:
        """Mark one run stalled, then notify. State first, delivery second."""
        phase = run_state_of(record)
        timeout = self.phase_timeout_s(phase)
        if timeout is None:  # pragma: no cover - only overrunning phases reach here
            raise RunError(f"{phase.value} has no configured timeout")
        elapsed = self.elapsed_in_phase_s(record)
        entered = phase_entered_ts(record)
        self.transition(
            ref,
            record.run_id,
            RunState.STALLED,
            reason=f"{phase.value} exceeded {timeout}s",
        )
        self.append_audit(
            ref,
            RUN_STALLED_EVENT,
            run=record.run_id,
            detail={
                "phase": phase.value,
                "elapsed_s": round(elapsed, 3),
                "timeout_s": timeout,
                "entered_ts": entered,
            },
        )
        return self._notify(
            StallNotice(
                run_id=record.run_id,
                project=ref.project,
                spec=ref.name,
                phase=phase,
                channel=self._notify_channel(),
                elapsed_s=elapsed,
                timeout_s=timeout,
                entered_ts=entered,
            ),
            ref,
        )

    def _notify_channel(self) -> str:
        return str(self._config.effective(NOTIFY_CHANNEL_SETTING, project=self._project).value)

    def _notify(self, notice: StallNotice, ref: SpecRef) -> StallNotice:
        """Deliver *notice*, recording a failure instead of raising it.

        The run is already stalled by the time this runs. A notifier reaches a
        chat surface the engine does not control, so its failure is a delivery
        problem: unwinding the state change would leave the run looking healthy
        because the messaging host was down.
        """
        try:
            self._notifier(notice)
        except Exception as exc:  # a channel's failure is not the run's failure
            logger.warning(
                "stall notification for run %s was not delivered: %s", notice.run_id, exc
            )
            self.append_audit(
                ref,
                RUN_NOTIFY_FAILED_EVENT,
                run=notice.run_id,
                detail={"channel": notice.channel, "error": str(exc)},
            )
            return replace(notice, notified=False, error=str(exc))
        return replace(notice, notified=True)

    # --------------------------------------------------------- task progress

    def record_task_status(
        self,
        ref: SpecRef,
        run_id: str,
        task: str,
        status: TaskStatus,
        *,
        lock: SpecLock | None = None,
    ) -> RunRecord:
        """Persist one leaf task's status inside the run.

        Written on every task state change, because the record is what a resumed
        execution reads: a status held only in the orchestrator's memory is lost
        by exactly the interruption resume exists for.

        Refused for a finished run. Recording progress against a run that is
        done, failed, or cancelled would rewrite a history that has already been
        reported.

        The check and the read-modify-write both happen inside the lock. The
        status map is rewritten whole, so doing the read outside would lose an
        update: two tasks in the same wave reporting at once would each write a
        map built before the other's, and whichever committed second would erase
        the first. The terminal check has to be under the same lock for the same
        reason a transition's does — otherwise a run that finished between the
        check and the write still accepts progress against itself.
        """
        if not task.strip():
            raise ValueError("a task status needs a task number")
        with self._held(ref, lock, owner=run_id):
            record = self.get(run_id)
            state = run_state_of(record)
            if state in TERMINAL_STATES:
                raise RunError(
                    f"run {run_id} is {state.value}; task status cannot be recorded against it"
                )
            statuses = {number: value.value for number, value in task_statuses(record).items()}
            statuses[task] = status.value
            return self._store.update_run(run_id, detail={DETAIL_TASKS: statuses})

    def completed_tasks(self, ref: SpecRef, run_id: str) -> tuple[str, ...]:
        """Leaf tasks a resumed run must not run again, in document order.

        A leaf counts as complete when the run recorded it complete **or** the
        tasks document has it checked off. Both are records of the same fact
        written by different hands — the engine's own bookkeeping, and the
        checkbox the IDE and a human also write — and honouring only one of them
        re-runs paid work whenever the other is the one that was updated.
        """
        record = self.get(run_id)
        statuses = task_statuses(record)
        return tuple(
            leaf.number
            for leaf in self._leaves(ref)
            if leaf.complete or statuses.get(leaf.number) is TaskStatus.COMPLETE
        )

    def next_incomplete_task(self, ref: SpecRef, run_id: str) -> str | None:
        """The leaf a resumed execution picks up at, or ``None`` when none is left.

        Document order, not wave order: the first leaf that is neither recorded
        complete nor checked off. A failed leaf is incomplete, so it is offered
        again and the retry limit — not this method — decides whether it is tried.
        """
        record = self.get(run_id)
        statuses = task_statuses(record)
        for leaf in self._leaves(ref):
            if leaf.complete or statuses.get(leaf.number) is TaskStatus.COMPLETE:
                continue
            return leaf.number
        return None

    def _leaves(self, ref: SpecRef) -> tuple[structure.Task, ...]:
        """The tasks document's leaf tasks, empty when it cannot be read."""
        text = phases.read_document(ref.spec_dir, DocumentKind.TASKS)
        if text is None:
            return ()
        return structure.parse_tasks(text).leaves

    # ----------------------------------------------------------------- resume

    def resume_point(self, ref: SpecRef, run_id: str) -> ResumePoint:
        """Where the run continues from, without changing anything.

        For a parked run this is the state it parked from; for a run still in a
        phase it is that phase, which is what makes this readable from a status
        surface as well as from :meth:`resume`.
        """
        record = self.get(run_id)
        state = run_state_of(record)
        if state in TERMINAL_STATES:
            raise RunError(f"run {run_id} is {state.value} and has nothing to resume")
        target = state
        if state in PARKED_STATES:
            recorded = parked_from(record)
            if recorded is None:
                raise RunError(
                    f"run {run_id} is {state.value} but no state was recorded to resume into"
                )
            target = recorded
        return self._point_for(ref, record, target)

    def _point_for(self, ref: SpecRef, record: RunRecord, target: RunState) -> ResumePoint:
        run_id = record.run_id
        if target is RunState.EXECUTING:
            completed = self.completed_tasks(ref, run_id)
            task = self.next_incomplete_task(ref, run_id)
            return ResumePoint(
                run_id=run_id,
                state=target,
                granularity=ResumeGranularity.TASK,
                task=task,
                completed_tasks=completed,
                reason="" if task else "every leaf task is complete",
            )
        gate: str | None = None
        reason = ""
        if target in (RunState.AUTHORING, RunState.AWAITING_REVIEW):
            phase_state = phases.derive_phase(self._store, ref)
            current = phase_state.current_gate
            gate = current.gate if current is not None else None
            if gate is None:
                reason = f"no gate is outstanding; the spec is {phase_state.phase.value}"
        elif target is RunState.QUEUED:
            reason = "the run had not started"
        else:
            reason = f"{target.value} keeps no finer progress than the phase itself"
        return ResumePoint(
            run_id=run_id,
            state=target,
            granularity=ResumeGranularity.PHASE,
            gate=gate,
            reason=reason,
        )

    def resume(
        self,
        ref: SpecRef,
        run_id: str,
        *,
        initiator: str | None = None,
        lock: SpecLock | None = None,
    ) -> ResumePoint:
        """Return a parked run to the state it parked from, and say where to pick up.

        Only a parked run resumes. A run still in a phase has nothing to return
        to, and re-entering its own phase would reset the timeout that is the
        only evidence it is not progressing.
        """
        record = self.get(run_id)
        state = run_state_of(record)
        if state not in PARKED_STATES:
            raise RunError(f"run {run_id} is {state.value}, not parked; only a parked run resumes")
        point = self.resume_point(ref, run_id)
        self.transition(
            ref,
            run_id,
            point.state,
            initiator=initiator,
            reason=f"resumed from {state.value}",
            lock=lock,
        )
        self.append_audit(
            ref,
            RUN_RESUMED_EVENT,
            run=run_id,
            initiator=initiator,
            detail={
                "from": state.value,
                "to": point.state.value,
                "granularity": point.granularity.value,
                "target": point.target,
            },
        )
        return point

    # --------------------------------------------------------------- plumbing

    @contextlib.contextmanager
    def _held(self, ref: SpecRef, lock: SpecLock | None, owner: str) -> Iterator[SpecLock]:
        """Hold *ref*'s lock, reusing one the caller already holds.

        The store's lock is not re-entrant, so an operation running inside a
        longer one passes the handle it already has rather than being rejected
        by itself.
        """
        if lock is not None:
            # verify_lock checks the row for the handle's OWN ref, so a valid
            # handle for a different spec would pass it and leave the writes in
            # this block unlocked.
            if lock.ref != ref:
                raise StatePersistenceError(f"lock is held for {lock.ref.key}, not {ref.key}")
            self._store.verify_lock(lock)
            yield lock
            return
        with self._store.lock(ref, owner=owner) as handle:
            yield handle

    def append_audit(
        self,
        ref: SpecRef,
        event: str,
        *,
        run: str | None = None,
        initiator: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        """Append one spec event, letting a failure to record it surface.

        Public because the sibling modules built on this machine — the review
        queue and its archival rules — record through the same log rather than
        opening a second handle to it. Two handles can be rooted differently, and
        an operator reading one log while the other holds half the history is the
        failure the audit log exists to prevent.

        Deliberately unlike :meth:`_notify`, which swallows. A notification is a
        courtesy and its loss costs someone a message; the audit log is the
        record of what the engine did to a repository unattended, and a run whose
        state moved with no trace of the move is the thing an operator later
        cannot reconstruct.

        The cost is that a transition which persisted and then failed to audit is
        reported to its caller as an error. That is the safe direction: the state
        is already durable and correct, and a caller that retries is refused as an
        illegal self-transition rather than doubling anything.
        """
        if self._audit is None:
            return
        self._audit.append(ref, event, run=run, initiator=initiator, detail=detail)

    def _observe_transition(
        self,
        ref: SpecRef,
        record: RunRecord,
        from_state: RunState,
        to_state: RunState,
        *,
        initiator: str | None = None,
    ) -> None:
        """The single point every committed transition is observed from.

        Called once per successful move, outside the lock and after the audit,
        with the post-transition record and both ends of the move already in
        hand. It exists so that a second thing that has to happen *at a
        transition* -- item feedback today, the awaiting-review human-gate
        notification requirement 6.3 wants next -- is one more line here rather
        than a second call to :meth:`transition` or a second walk of the state
        machine. Both observers then see the same ``from_state``/``to_state`` the
        machine just decided, so they cannot disagree about what moved.

        A second observer added here inherits the two properties this call site
        already guarantees and a caller would have to re-earn: it runs only after
        the state change is durable, and it runs outside the spec lock, so a slow
        observer cannot stall a contended write or unwind a move that happened.
        """
        self._post_item_feedback(ref, record, from_state, to_state)

    def _item_feedback(self) -> Any:
        """The item-feedback poster, or ``None`` when this machine cannot post.

        Built once, on first transition. Absent without an audit log because a
        writeback records its outcome there, and a poster that could act without
        recording would drop the one trace that a tracker refused a comment. The
        import is deferred to break the cycle: the watch package imports this
        module, so importing its feedback poster at module load would import a
        half-built ``runs``.
        """
        if not self._feedback_ready:
            self._feedback_ready = True
            if self._audit is not None:
                from .watch.feedback import FeedbackPoster

                self._feedback = FeedbackPoster(
                    self._store, self._config, self._audit, project=self._project
                )
        return self._feedback

    def _post_item_feedback(
        self,
        ref: SpecRef,
        record: RunRecord,
        from_state: RunState,
        to_state: RunState,
    ) -> None:
        """Write back to the run's triggering item at a mapped transition.

        Outside the lock and after the audit, for the same reason the transition
        audit is: the state change is already durable, and a feedback command
        spawns a subprocess the spec lock must not be held across. Best-effort by
        construction -- :func:`~.watch.feedback.post_feedback` never raises -- so a
        tracker that refuses a comment cannot unwind a transition that happened.

        Nothing is posted for a run with no source: those are specs a person
        authored by hand, which reach these same transitions with no item to
        report to. The poster itself declines a source that configured no
        feedback, so the common case spawns nothing.
        """
        event = lifecycle_event_for(from_state, to_state)
        if event is None:
            return
        poster = self._item_feedback()
        if poster is None:
            return
        from .delivery.variables import RunContext

        detail = record.detail
        context = RunContext(
            spec_name=ref.name,
            spec_type=str(detail.get("spec_type", "")),
            workspace_path=str(detail.get("working_tree", "")),
            base_branch=str(detail.get("base_branch", "")),
            item_id=record.item_id or str(detail.get("item_id", "")),
            item_url=str(detail.get("item_url", "")),
        )
        poster.post(
            ref,
            source=record.source,
            run_id=record.run_id,
            event=event,
            context=context,
        )


def _parse_ts(value: str) -> datetime | None:
    """Parse a persisted timestamp, treating a naive one as UTC."""
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        logger.warning("unparseable run timestamp: %r", value)
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def reachable_states(start: RunState = INITIAL_STATE) -> frozenset[RunState]:
    """Every state reachable from *start* by legal moves.

    Exposed because "no state is stranded" is a property of the table rather
    than of any caller: a state nothing can reach is a case surfaces render and
    operators are told about that a run can never actually be in.
    """
    seen = {start}
    frontier = [start]
    while frontier:
        for nxt in TRANSITIONS[frontier.pop()]:
            if nxt not in seen:
                seen.add(nxt)
                frontier.append(nxt)
    return frozenset(seen)
