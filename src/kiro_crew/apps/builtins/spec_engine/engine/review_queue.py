"""The Review_Queue projection, and the rules that govern archival.

Two things live together here because they are the same question asked twice:
what is waiting on a person right now, and what has a person said they are
finished with.

**The queue is a projection, not a table.** It is derived from the run rows on
every call, so there is no second copy to fall out of step with the state
machine. A stored queue needs an enqueue at every entry into a human-reserved
state and a dequeue at every exit — including the exits nobody thinks about, a
cancelled run and a budget halt — and the failure when one is missed is a run
sitting in a reviewer's list forever, or worse, a run waiting on a person that
appears in nobody's list. Deriving costs a query and cannot drift.

**Nothing here removes anything on elapsed time.** There is deliberately no
expiry, no retention window, and no cause an operation could pass to mean "this
got old": :class:`ArchiveCause` names the only two events that archive a spec,
and a cause outside it is refused rather than coerced to something plausible.
Time-based cleanup is attractive right up to the moment it deletes the spec
somebody was going to come back to on Monday, and an autonomous system that
quietly discards its own work leaves the user unable to tell a run that never
happened from one that was swept.

**Archival is a person's statement and only a person's statement.** It happens on
explicit action, or when the item that triggered the run is cancelled at the
source — the one case where the request itself has been withdrawn. It survives
every other operation: no transition, sweep, resume, or projection flips the
flag, and only :meth:`ReviewQueue.unarchive` clears it. It is reversible in both
directions, so an archived spec that turns out to matter comes back with its runs
and its place in the queue intact; nothing is deleted, only hidden.
"""

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterator, Mapping

from . import phases
from .runs import (
    TERMINAL_STATES,
    RunMachine,
    RunState,
    phase_entered_ts,
    run_state_of,
)
from .state import RunRecord, SpecLock, SpecRecord, SpecRef, StatePersistenceError

logger = logging.getLogger(__name__)


class WaitingOn(str, Enum):
    """What the person a queued run waits for actually has to do.

    The run's state alone does not say this: three different states all mean
    "parked on a human", and a reviewer looking at one list needs to know which
    of them is a verdict, which is a spending decision, and which is a judgement
    call about a run that stopped reporting.
    """

    #: A review verdict at a human-reserved gate.
    REVIEW = "review"
    #: A budget ceiling. Progress needs an operator to raise it or let the run go.
    BUDGET = "budget"
    #: A phase that overran its wall clock. Someone decides to resume or abandon.
    STALL = "stall"


#: The states a run occupies while it waits on a person, and what it waits for.
#:
#: ``awaiting_review`` is the obvious member. The two parked states belong for
#: the same reason: nothing in the engine will ever move them on its own, so a
#: queue that showed only ``awaiting_review`` would leave a budget-halted run and
#: a stalled run in the one place no surface lists and no automation revisits.
#: Iteration order is the order a grouped rendering shows the groups in.
HUMAN_RESERVED_STATES: Mapping[RunState, WaitingOn] = {
    RunState.AWAITING_REVIEW: WaitingOn.REVIEW,
    RunState.HALTED_BUDGET: WaitingOn.BUDGET,
    RunState.STALLED: WaitingOn.STALL,
}


class ArchiveCause(str, Enum):
    """The complete set of reasons a spec may be archived.

    Closed on purpose, and asserted closed by the tests. Every member is an event
    somebody caused: there is no member meaning "enough time passed", because the
    engine must not be able to name one.
    """

    #: A person asked for it, from any driver.
    USER = "user"
    #: The Watched_Item that triggered the run was cancelled at its source, so
    #: the request the spec exists to answer has been withdrawn.
    ITEM_CANCELLED = "item_cancelled"


#: The causes :meth:`ReviewQueue.archive` accepts, as data a surface can render.
ARCHIVE_CAUSES: frozenset[ArchiveCause] = frozenset(ArchiveCause)

# --- Audit event names -----------------------------------------------------

SPEC_ARCHIVED_EVENT = "spec.archived"
SPEC_UNARCHIVED_EVENT = "spec.unarchived"
#: Recorded when an archival was refused, so an attempt to archive for a reason
#: the engine does not accept leaves a trace rather than looking like no attempt.
SPEC_ARCHIVE_REFUSED_EVENT = "spec.archive-refused"


class ArchivalRefused(Exception):
    """An archival was refused because its cause is not one the engine accepts."""


@dataclass(frozen=True)
class QueueEntry:
    """One run waiting on a person, flattened for any driver to render.

    Deliberately plain: every field is a string, a number, or ``None``, so a
    dashboard, a chat surface, and a CLI render the same queue without one of
    them reaching back into the engine for the field the projection left out.
    """

    run_id: str
    project: str
    spec: str
    spec_type: str | None
    state: RunState
    waiting_on: WaitingOn
    #: When the run entered the state it is waiting in.
    entered_ts: str
    #: How long it has been waiting, by the engine's clock.
    waiting_s: float
    source: str | None
    item_id: str | None
    cost_credits: float
    #: The document gate the run is parked on, when it is parked on one.
    gate: str | None = None

    @property
    def ref(self) -> SpecRef:
        return SpecRef(project=self.project, name=self.spec)

    def to_json_object(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "project": self.project,
            "spec": self.spec,
            "spec_type": self.spec_type,
            "state": self.state.value,
            "waiting_on": self.waiting_on.value,
            "entered_ts": self.entered_ts,
            "waiting_s": round(self.waiting_s, 3),
            "source": self.source,
            "item_id": self.item_id,
            "cost_credits": self.cost_credits,
            "gate": self.gate,
        }


@dataclass(frozen=True)
class QueueSnapshot:
    """The Review_Queue as it stood when it was taken.

    A value rather than a live view, so a driver that renders a list and then
    acts on a row is acting on what it showed. Ordered longest-waiting first:
    that is the order in which ignoring the queue costs the most, and it does not
    change under a re-render the way an arrival-ordered list does when a run
    leaves.
    """

    entries: tuple[QueueEntry, ...] = ()

    def __len__(self) -> int:
        return len(self.entries)

    def __iter__(self) -> Iterator[QueueEntry]:
        return iter(self.entries)

    def __contains__(self, run_id: object) -> bool:
        return any(entry.run_id == run_id for entry in self.entries)

    @property
    def run_ids(self) -> tuple[str, ...]:
        return tuple(entry.run_id for entry in self.entries)

    def grouped(self) -> dict[RunState, tuple[QueueEntry, ...]]:
        """The entries grouped by run state, in :data:`HUMAN_RESERVED_STATES` order.

        A state with nothing waiting in it is omitted rather than given an empty
        group: a surface renders the groups it is handed, and a permanent empty
        "stalled" heading trains people to ignore headings.
        """
        grouped: dict[RunState, tuple[QueueEntry, ...]] = {}
        for state in HUMAN_RESERVED_STATES:
            matching = tuple(entry for entry in self.entries if entry.state is state)
            if matching:
                grouped[state] = matching
        return grouped

    def for_spec(self, ref: SpecRef) -> tuple[QueueEntry, ...]:
        """Just this spec's entries, for a run or spec detail surface."""
        return tuple(entry for entry in self.entries if entry.ref == ref)

    def to_json_object(self) -> dict[str, Any]:
        return {
            "entries": [entry.to_json_object() for entry in self.entries],
            "grouped": {
                state.value: [entry.to_json_object() for entry in group]
                for state, group in self.grouped().items()
            },
            "total": len(self.entries),
        }


@dataclass(frozen=True)
class ArchiveResult:
    """The outcome of one archival decision.

    ``changed`` is separate from ``archived`` because re-archiving an archived
    spec and archiving a live one are both fine and are not the same event: a
    caller that reports "archived" on every call cannot tell a driver's duplicate
    click from a second, real decision.
    """

    project: str
    spec: str
    archived: bool
    changed: bool
    cause: ArchiveCause | None = None
    actor: str | None = None
    item_id: str | None = None
    #: Runs cancelled as part of the archival, when the triggering item was.
    cancelled_runs: tuple[str, ...] = ()

    @property
    def ref(self) -> SpecRef:
        return SpecRef(project=self.project, name=self.spec)

    def to_json_object(self) -> dict[str, Any]:
        return {
            "project": self.project,
            "spec": self.spec,
            "archived": self.archived,
            "changed": self.changed,
            "cause": self.cause.value if self.cause is not None else None,
            "actor": self.actor,
            "item_id": self.item_id,
            "cancelled_runs": list(self.cancelled_runs),
        }


def resolve_cause(cause: ArchiveCause | str) -> ArchiveCause:
    """Return the :class:`ArchiveCause` *cause* names, or refuse it.

    An unrecognised cause is refused rather than defaulted. Defaulting to
    :attr:`ArchiveCause.USER` would let any caller — a sweep, a retention job
    somebody adds later — archive a spec and have the audit log attribute it to a
    person who never touched it.
    """
    if isinstance(cause, ArchiveCause):
        return cause
    try:
        return ArchiveCause(cause)
    except ValueError:
        accepted = ", ".join(sorted(member.value for member in ARCHIVE_CAUSES))
        raise ArchivalRefused(
            f"{cause!r} is not a reason a spec may be archived; accepted causes are: {accepted}"
        ) from None


class ReviewQueue:
    """Projects the Review_Queue and applies the archival rules.

    Built on a :class:`~.runs.RunMachine` rather than beside one: the queue's
    membership is a function of run state, and cancelling a run during an
    archival is a transition. Sharing the machine means both go through the same
    table, the same lock, and the same audit log instead of a second path with
    its own idea of what a legal move is.
    """

    def __init__(self, machine: RunMachine) -> None:
        self._machine = machine
        self._store = machine.store

    # ------------------------------------------------------------------ queue

    def snapshot(self, *, project: str | None = None) -> QueueSnapshot:
        """Every run waiting on a person, optionally narrowed to one project.

        Archived specs contribute nothing: the listing that supplies the specs
        excludes them, so archiving a spec removes its runs from the queue
        without touching a single run row — which is what makes unarchiving put
        them back exactly as they were.
        """
        specs = {record.spec_key: record for record in self._store.list_specs(project=project)}
        states = [state.value for state in HUMAN_RESERVED_STATES]
        entries = [
            self._entry(record, specs[record.spec_key])
            for record in self._store.list_runs(states=states)
            if record.spec_key in specs
        ]
        entries.sort(key=lambda entry: (-entry.waiting_s, entry.run_id))
        return QueueSnapshot(entries=tuple(entries))

    def entries(self, *, project: str | None = None) -> tuple[QueueEntry, ...]:
        """The queue's entries, for a caller that wants no wrapper."""
        return self.snapshot(project=project).entries

    def holds(self, run_id: str) -> bool:
        """Whether the queue currently holds *run_id*."""
        return run_id in self.snapshot()

    def _entry(self, record: RunRecord, spec: SpecRecord) -> QueueEntry:
        state = run_state_of(record)
        return QueueEntry(
            run_id=record.run_id,
            project=spec.project,
            spec=spec.name,
            spec_type=spec.spec_type,
            state=state,
            waiting_on=HUMAN_RESERVED_STATES[state],
            entered_ts=phase_entered_ts(record),
            waiting_s=self._machine.elapsed_in_phase_s(record),
            source=record.source,
            item_id=record.item_id,
            cost_credits=record.cost_credits,
            gate=self._outstanding_gate(spec.ref),
        )

    def _outstanding_gate(self, ref: SpecRef) -> str | None:
        """The document gate the spec is working on, or ``None``.

        Derived from disk, not from the cached phase column: the reviewer is
        being told which document to look at, and a cached value that predates
        the last edit sends them to the wrong one.
        """
        current = phases.derive_phase(self._store, ref).current_gate
        return current.gate if current is not None else None

    # -------------------------------------------------------------- archival

    def is_archived(self, ref: SpecRef) -> bool:
        """Whether *ref* is archived. False for a spec nothing has registered."""
        record = self._store.get_spec(ref)
        return bool(record is not None and record.archived)

    def archive(
        self,
        ref: SpecRef,
        *,
        cause: ArchiveCause | str = ArchiveCause.USER,
        actor: str | None = None,
        item_id: str | None = None,
        lock: SpecLock | None = None,
    ) -> ArchiveResult:
        """Archive *ref*, or refuse when the cause is not one the engine accepts.

        The cause is resolved and checked BEFORE anything is written, and the
        refusal is recorded. Writing first would mean a rejected archival still
        archived the spec — the caller sees an error, the user sees their spec
        gone from every listing, and nothing in the log explains it.

        Archiving an already-archived spec is not an error. Two drivers can
        reasonably both act on the same decision, and a spec is already in the
        state the caller wanted.
        """
        resolved = self._resolved_cause(ref, cause, actor=actor, item_id=item_id)
        with self._held(ref, lock, owner=actor or resolved.value):
            was_archived = self.is_archived(ref)
            self._store.set_archived(ref, True)
            result = ArchiveResult(
                project=ref.project,
                spec=ref.name,
                archived=True,
                changed=not was_archived,
                cause=resolved,
                actor=actor,
                item_id=item_id,
            )
        self._machine.append_audit(
            ref,
            SPEC_ARCHIVED_EVENT,
            initiator=actor,
            detail={
                "cause": resolved.value,
                "item_id": item_id,
                "changed": result.changed,
            },
        )
        return result

    def _resolved_cause(
        self,
        ref: SpecRef,
        cause: ArchiveCause | str,
        *,
        actor: str | None,
        item_id: str | None,
    ) -> ArchiveCause:
        """Resolve *cause*, recording and raising when it cannot be accepted."""
        try:
            resolved = resolve_cause(cause)
            if resolved is ArchiveCause.ITEM_CANCELLED and not (item_id or "").strip():
                raise ArchivalRefused(
                    "archiving because a triggering item was cancelled needs the item's "
                    "identifier, so the claim can be checked against the source"
                )
        except ArchivalRefused as exc:
            self._machine.append_audit(
                ref,
                SPEC_ARCHIVE_REFUSED_EVENT,
                initiator=actor,
                detail={
                    "cause": cause.value if isinstance(cause, ArchiveCause) else str(cause),
                    "item_id": item_id,
                    "reason": str(exc),
                },
            )
            raise
        return resolved

    def unarchive(
        self,
        ref: SpecRef,
        *,
        actor: str | None = None,
        lock: SpecLock | None = None,
    ) -> ArchiveResult:
        """Bring *ref* back. The only path that clears the flag.

        Nothing else in the engine unarchives: a spec stays archived through
        transitions, sweeps, resumes, and every projection, so "archived" means a
        person's decision is still standing rather than the last write happening
        to have left it set.
        """
        with self._held(ref, lock, owner=actor or "unarchive"):
            was_archived = self.is_archived(ref)
            self._store.set_archived(ref, False)
            result = ArchiveResult(
                project=ref.project,
                spec=ref.name,
                archived=False,
                changed=was_archived,
                actor=actor,
            )
        self._machine.append_audit(
            ref,
            SPEC_UNARCHIVED_EVENT,
            initiator=actor,
            detail={"changed": result.changed},
        )
        return result

    def archive_cancelled_item(
        self,
        ref: SpecRef,
        *,
        item_id: str,
        actor: str | None = None,
        lock: SpecLock | None = None,
    ) -> ArchiveResult:
        """Cancel the runs the cancelled item triggered, then archive the spec.

        One lock covers the cancellations and the archival, so a spec is never
        left half-cascaded: runs cancelled but still listed, or archived with
        runs that look live. Terminal runs are left alone — a run that already
        finished is history, and rewriting it to ``cancelled`` would misreport
        work that actually shipped.

        Runs from a different item are left alone too. Several items can drive
        the same spec, and cancelling one of them is not a statement about the
        others.
        """
        subject = (item_id or "").strip()
        if not subject:
            raise ArchivalRefused("a cancelled-item archival must name the cancelled item")
        with self._held(ref, lock, owner=actor or f"item:{subject}") as handle:
            cancelled = self._cancel_runs_for_item(ref, subject, actor=actor, lock=handle)
            result = self.archive(
                ref,
                cause=ArchiveCause.ITEM_CANCELLED,
                actor=actor,
                item_id=subject,
                lock=handle,
            )
        return ArchiveResult(
            project=result.project,
            spec=result.spec,
            archived=result.archived,
            changed=result.changed,
            cause=result.cause,
            actor=result.actor,
            item_id=result.item_id,
            cancelled_runs=cancelled,
        )

    def _cancel_runs_for_item(
        self,
        ref: SpecRef,
        item_id: str,
        *,
        actor: str | None,
        lock: SpecLock,
    ) -> tuple[str, ...]:
        cancelled: list[str] = []
        for record in self._store.list_runs(ref=ref):
            if (record.item_id or "") != item_id:
                continue
            if run_state_of(record) in TERMINAL_STATES:
                continue
            self._machine.transition(
                ref,
                record.run_id,
                RunState.CANCELLED,
                initiator=actor,
                reason=f"triggering item {item_id} was cancelled",
                lock=lock,
            )
            cancelled.append(record.run_id)
        return tuple(cancelled)

    # --------------------------------------------------------------- plumbing

    @contextlib.contextmanager
    def _held(self, ref: SpecRef, lock: SpecLock | None, owner: str) -> Iterator[SpecLock]:
        """Hold *ref*'s lock, reusing one the caller already holds.

        The store's lock is not re-entrant and refuses a second writer instead of
        waiting, so an operation nested inside a longer one passes the handle it
        already has rather than deadlocking against itself.
        """
        if lock is not None:
            # verify_lock checks the row for the handle's OWN ref, so a valid
            # handle for a different spec would pass it and leave every write in
            # this block unlocked. The archival cascade's atomicity is exactly
            # what that would void.
            if lock.ref != ref:
                raise StatePersistenceError(
                    f"lock is held for {lock.ref.key}, not {ref.key}"
                )
            self._store.verify_lock(lock)
            yield lock
            return
        with self._store.lock(ref, owner=owner) as handle:
            yield handle
