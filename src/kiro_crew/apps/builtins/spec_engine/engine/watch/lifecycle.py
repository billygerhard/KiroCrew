"""Comparing successive polls: what changed, and what may dispatch once.

A tracker tells you what its items are, not what happened to them. Some report a
transition field and most do not, and none of them report the one an unattended
run cares about — "this item became actionable again since you last looked". So
the lifecycle is *derived here*, by comparing what this poll reported against the
snapshot the last poll left, and the comparison is the only source of a
reopen or a cancellation. Nothing reads a transition field.

The derivation is one rule per pair of observations:

* an identifier the snapshot has never held is **new**, at the first generation;
* an item the snapshot holds closed and this poll reports open is **reopened**,
  at the next generation;
* an item the snapshot holds open and this poll reports closed is **cancelled**,
  at the generation it already had;
* anything else is **unchanged**, at the generation it already had.

**The generation is what makes exactly-once compatible with re-running.** A
dispatch claim is keyed on the item identifier together with its generation, so
the second poll of an unchanged item asks for a key that is already taken and
dispatches nothing, while a reopened item asks for a key nobody holds and
dispatches again. Neither outcome depends on how many polls ran, in what order,
or whether an earlier tick died halfway: the ledger's unique constraint decides,
not the count of observations.

Three refusals are worth stating, because each is a way this module could
quietly do damage.

**A poll that did not run derives nothing.** An unhealthy source reports no
items, and so does a tracker with an empty backlog — but only one of them is
evidence. Treating a failed poll as an empty tracker would derive a cancellation
for every open item at once, cascading cancels across every in-flight run from a
missing program or an expired credential. So a diff of a non-OK poll carries the
poll's own reason, holds no changes, and writes no snapshot; the previous
snapshot stands untouched until a poll actually succeeds.

**An item's absence is not its closure.** A healthy poll that stops mentioning an
item is reported as :attr:`WatchDiff.unreported` and derives no transition,
because an absence is also what a narrowed filter, a paginated result, or a
relabelled item looks like. Cancellation is something a poll has to *say*, by
reporting the item with a state this module recognizes as closed. A source whose
command lists only open items should widen it to include closed ones if it wants
cancellation derived.

**An unrecognized state is open.** :data:`CLOSED_STATES` is a fixed vocabulary
matched against the item's state text; anything else — including a blank state
from a source that does not map the field — counts as open, because the item was
on the list the tracker just printed. Guessing that unfamiliar text means closed
would derive cancellations from a stranger's wording.

Item text stays untrusted throughout. State text is normalized only for the
vocabulary comparison; the original is stored and displayed as it arrived, and
nothing here executes, expands, or interprets any of it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Protocol, Sequence

from ..state import CLAIM_DISPATCH, StateStore, WatchItemRecord, WatchObservation
from .items import WatchedItem
from .poll import HealthReason, PollOutcome, PollStatus

logger = logging.getLogger(__name__)


class DispatchGate(Protocol):
    """Whether a watch source may have new work dispatched for it right now.

    A seam rather than an import, so the zero-token watcher keeps deciding *what
    changed* without also owning what a period costs. The engine's implementation
    is ``budget.SourceCaps``, which answers for the source's spending cap and for
    the kill switch together — a caller that had to ask two objects would
    eventually ask one.
    """

    def dispatch_allowed(self, source: str) -> bool: ...


#: The generation an item is first seen at. Generations count observed lifecycles,
#: so the first sighting is the first one rather than a zeroth.
FIRST_GENERATION = 1

#: State texts that mean an item is no longer actionable, normalized. A tracker
#: outside this vocabulary reads as open, which is the safe direction: an
#: unrecognized word derives no cancellation, and an operator can see the item's
#: stored state text beside a cancellation that never fired.
CLOSED_STATES: frozenset[str] = frozenset(
    {
        "abandoned",
        "cancelled",
        "canceled",
        "close",
        "closed",
        "complete",
        "completed",
        "declined",
        "done",
        "duplicate",
        "merged",
        "not planned",
        "rejected",
        "resolved",
        "wont do",
        "wont fix",
    }
)


class Transition(str, Enum):
    """What comparing two observations of one item derived."""

    #: No snapshot row held this identifier before.
    NEW = "new"
    #: Held closed, reported open. The one transition that raises a generation.
    REOPENED = "reopened"
    #: Held open, reported closed.
    CANCELLED = "cancelled"
    #: Reported again on the same side of open, whatever its text now says.
    UNCHANGED = "unchanged"


#: Transitions that make an item a dispatch candidate. Both are "there is work
#: here that no run has taken", which is the only thing that justifies spending.
DISPATCHING_TRANSITIONS: tuple[Transition, ...] = (Transition.NEW, Transition.REOPENED)


def generation_key(generation: int) -> str:
    """Render *generation* for the claim ledger's generation column.

    One function so a claim written by a dispatcher and a claim released by the
    re-dispatch override cannot disagree on formatting. The ledger compares the
    column as text, so ``"1"`` and ``"01"`` would be different generations.
    """
    if generation < FIRST_GENERATION:
        raise ValueError("a lifecycle generation starts at one and only rises")
    return str(generation)


@dataclass(frozen=True)
class ItemChange:
    """One item, and what comparing it against the snapshot derived.

    The invariants are the point of the type. A generation that did not move
    with its transition is the bug this whole module exists to avoid: a reopen
    recorded at the old generation cannot dispatch (its claim is held), and a
    re-poll recorded at a new one dispatches a duplicate run.
    """

    item: WatchedItem
    transition: Transition
    generation: int
    previous_generation: int | None
    is_open: bool
    #: Whether the item's content changed while its lifecycle position did not.
    #: Only an ``unchanged`` item can be edited: a content change that also moved
    #: the lifecycle is a reopen or a cancellation, and a first sighting has no
    #: baseline to have differed from. An edit is never a dispatch candidate --
    #: it keeps the item ``unchanged`` -- so this flag surfaces the edit for
    #: auditing without making it dispatchable.
    edited: bool = False

    def __post_init__(self) -> None:
        if self.generation < FIRST_GENERATION:
            raise ValueError("a lifecycle generation starts at one and only rises")
        if self.edited and self.transition is not Transition.UNCHANGED:
            raise ValueError("an edit is a content change with an unchanged lifecycle position")
        if self.transition is Transition.NEW:
            if self.previous_generation is not None:
                raise ValueError("a new item has no previous generation")
            if self.generation != FIRST_GENERATION:
                raise ValueError("a new item starts at the first generation")
            return
        if self.previous_generation is None:
            raise ValueError(f"a {self.transition.value} item must have been seen before")
        if self.transition is Transition.REOPENED:
            if self.generation != self.previous_generation + 1:
                raise ValueError("a reopened item advances exactly one generation")
            if not self.is_open:
                raise ValueError("a reopened item is open")
            return
        if self.generation != self.previous_generation:
            raise ValueError(f"a {self.transition.value} item keeps its generation")
        if self.transition is Transition.CANCELLED and self.is_open:
            raise ValueError("a cancelled item is not open")

    @property
    def source(self) -> str:
        return self.item.source

    @property
    def identifier(self) -> str:
        return self.item.identifier

    @property
    def dispatchable(self) -> bool:
        """Whether this change is work no run has been started for.

        Openness is checked as well as the transition: an item whose first
        sighting is already closed is new, and starting a run for finished work
        would spend a budget on it.
        """
        return self.is_open and self.transition in DISPATCHING_TRANSITIONS

    @property
    def observation(self) -> WatchObservation:
        """This change as the snapshot row a later poll will be compared against."""
        return WatchObservation(
            item_id=self.item.identifier,
            generation=self.generation,
            item_state=self.item.state,
            is_open=self.is_open,
            content_digest=self.item.content_digest,
        )


@dataclass(frozen=True)
class WatchDiff:
    """What one poll changed about one source, or why nothing could be derived.

    ``status`` is the poll's own status rather than a second vocabulary, so a
    caller that already handles poll health handles a diff the same way. Only an
    OK status carries changes; every other status carries the poll's reason and
    leaves the snapshot alone.
    """

    source: str
    status: PollStatus
    changes: tuple[ItemChange, ...] = ()
    unreported: tuple[WatchItemRecord, ...] = ()
    duplicates: tuple[str, ...] = ()
    reason: HealthReason | None = None
    detail: str = ""

    def __post_init__(self) -> None:
        if self.status is PollStatus.OK:
            if self.reason is not None:
                raise ValueError("only a poll that failed carries a reason")
            return
        if self.changes or self.unreported or self.duplicates:
            raise ValueError("a poll that did not run derives nothing about its items")
        if self.status is PollStatus.UNHEALTHY:
            if self.reason is None:
                raise ValueError("an unhealthy poll's diff must carry its reason")
            if not self.detail.strip():
                raise ValueError("an unhealthy poll's diff must explain itself")

    @property
    def derived(self) -> bool:
        """Whether this diff is evidence about the source's items.

        False for every poll that did not run and parse, so no caller can reach
        a cancellation, or an empty change list read as "nothing to do", through
        a source that is merely broken.
        """
        return self.status is PollStatus.OK

    @property
    def new_items(self) -> tuple[ItemChange, ...]:
        return self._of(Transition.NEW)

    @property
    def reopened(self) -> tuple[ItemChange, ...]:
        return self._of(Transition.REOPENED)

    @property
    def cancelled(self) -> tuple[ItemChange, ...]:
        return self._of(Transition.CANCELLED)

    @property
    def unchanged(self) -> tuple[ItemChange, ...]:
        return self._of(Transition.UNCHANGED)

    @property
    def edited(self) -> tuple[ItemChange, ...]:
        """Items whose content changed while their lifecycle position did not.

        A subset of :attr:`unchanged`, never disjoint from it: an edit is not a
        transition of its own, so it does not remove the item from the unchanged
        set or make it a dispatch candidate. It is surfaced so that an edit made
        while a run for the item is in flight can be recorded as ignored -- the
        item was rewritten after a run had already been given the old content,
        and requirement 21.3 wants that visible to an operator.
        """
        return tuple(change for change in self.changes if change.edited)

    @property
    def dispatchable(self) -> tuple[ItemChange, ...]:
        """Dispatch candidates in the order the source reported them.

        Poll order is arrival order, and queueing is arrival-ordered, so the
        sequence a caller iterates is the sequence capacity should free in.
        """
        return tuple(change for change in self.changes if change.dispatchable)

    @property
    def observations(self) -> tuple[WatchObservation, ...]:
        """The snapshot rows this diff would record."""
        return tuple(change.observation for change in self.changes)

    def _of(self, transition: Transition) -> tuple[ItemChange, ...]:
        return tuple(change for change in self.changes if change.transition is transition)

    def describe(self) -> str:
        """One line for a human: what moved, or why nothing could be read."""
        if not self.derived:
            named = self.reason.value if self.reason is not None else self.status.value
            return f"{self.source}: no lifecycle derived ({named})"
        parts = [
            f"{len(self.new_items)} new",
            f"{len(self.reopened)} reopened",
            f"{len(self.cancelled)} cancelled",
            f"{len(self.unchanged)} unchanged",
        ]
        if self.edited:
            parts.append(f"{len(self.edited)} edited")
        if self.unreported:
            parts.append(f"{len(self.unreported)} unreported")
        if self.duplicates:
            parts.append(f"{len(self.duplicates)} duplicate identifier(s)")
        return f"{self.source}: " + ", ".join(parts)


@dataclass(frozen=True)
class WatchAdvance:
    """The result of taking one poll all the way to claimed dispatches."""

    diff: WatchDiff
    granted: tuple[ItemChange, ...] = ()
    withheld: tuple[ItemChange, ...] = ()
    recorded: bool = False
    #: Candidates the dispatch gate refused: the source is at its spending cap, or
    #: everything is stopped. Their claims were not taken and the snapshot was not
    #: recorded, so they are still candidates on a later poll.
    gated: tuple[ItemChange, ...] = ()
    #: Why the gate refused, for a surface reporting it. Empty when it did not.
    gate_reason: str = ""

    @property
    def source(self) -> str:
        return self.diff.source

    def describe(self) -> str:
        if not self.diff.derived:
            return self.diff.describe()
        if self.gated:
            return f"{self.diff.describe()}; {len(self.gated)} not dispatched: {self.gate_reason}"
        return (
            f"{self.diff.describe()}; {len(self.granted)} claimed, "
            f"{len(self.withheld)} already claimed"
        )


def diff_poll(state: StateStore, outcome: PollOutcome) -> WatchDiff:
    """Compare *outcome* against the recorded snapshot, without writing anything.

    Read-only on purpose: a caller inspects the derived transitions, decides
    what to dispatch, and only then records the snapshot. Splitting the read
    from the write is also what lets a diff of a failed poll be a harmless
    value rather than a destructive one.
    """
    if outcome.status is not PollStatus.OK:
        return WatchDiff(
            source=outcome.source,
            status=outcome.status,
            reason=outcome.reason,
            detail=outcome.detail,
        )

    previous = {record.item_id: record for record in state.list_watch_items(outcome.source)}
    changes: list[ItemChange] = []
    duplicates: list[str] = []
    seen: set[str] = set()
    for item in outcome.items:
        if item.identifier in seen:
            # Two entries for one identifier is a filter that matched twice, not
            # two items. The first wins and the repeat is reported: recording
            # both would write one snapshot row over the other, and the losing
            # write would decide the generation.
            if item.identifier not in duplicates:
                duplicates.append(item.identifier)
            continue
        seen.add(item.identifier)
        changes.append(_derive(item, previous.get(item.identifier)))

    unreported = tuple(record for record in previous.values() if record.item_id not in seen)
    if duplicates:
        logger.warning(
            "watch source %r reported %d identifier(s) more than once in one poll",
            outcome.source,
            len(duplicates),
        )
    return WatchDiff(
        source=outcome.source,
        status=PollStatus.OK,
        changes=tuple(changes),
        unreported=unreported,
        duplicates=tuple(duplicates),
    )


def claim_dispatch(state: StateStore, change: ItemChange, *, run_id: str | None = None) -> bool:
    """Claim *change*'s generation for dispatch. True the first time only.

    The claim is taken before the run starts, because the ledger exists to
    prevent a second run rather than to record a first one. A claim held by work
    that then failed is a missed dispatch an operator can release; a run started
    before the claim is a duplicate nobody can undo.
    """
    if not change.dispatchable:
        raise ValueError(f"a {change.transition.value} item is not a dispatch candidate")
    return state.claim_dispatch(
        change.source,
        change.identifier,
        generation=generation_key(change.generation),
        run_id=run_id,
    )


def claim_dispatches(
    state: StateStore, diff: WatchDiff, *, run_id: str | None = None
) -> tuple[tuple[ItemChange, ...], tuple[ItemChange, ...]]:
    """Claim every dispatch candidate in *diff*, returning ``(granted, withheld)``.

    Withheld covers the ordinary case, not an error: the same open item is
    reported by every poll, and its generation was claimed the first time.
    """
    if not diff.derived:
        return (), ()
    granted: list[ItemChange] = []
    withheld: list[ItemChange] = []
    for change in diff.dispatchable:
        if claim_dispatch(state, change, run_id=run_id):
            granted.append(change)
        else:
            withheld.append(change)
    return tuple(granted), tuple(withheld)


def record_snapshot(state: StateStore, diff: WatchDiff) -> bool:
    """Record *diff*'s observations as the snapshot the next poll compares against.

    Refuses a diff that derived nothing, in code rather than by asking callers to
    remember: this is the write that a failed poll must never reach.
    """
    if not diff.derived:
        return False
    state.record_watch_items(diff.source, diff.observations)
    return True


def advance_watch(
    state: StateStore,
    outcome: PollOutcome,
    *,
    gate: DispatchGate,
    run_id: str | None = None,
) -> WatchAdvance:
    """Diff *outcome*, claim its dispatch candidates, then record the snapshot.

    The order is deliberate and the two writes are separate transactions, so a
    crash between them is worth being explicit about. Claiming first means an
    interrupted tick leaves a claim row for work that never started: a missed
    dispatch, recoverable by releasing the claim. Recording first would mean the
    next poll sees the item as unchanged with nothing in the ledger to say it was
    ever considered — a missed dispatch with no trace and no recovery. Neither
    order can dispatch twice, and that is the property being protected.

    *gate* decides whether this source may dispatch at all right now — its
    spending cap for the period, and the kill switch. A refusal claims nothing
    **and records nothing**: recording the snapshot would make these items
    unchanged on the next poll, so they would never be dispatch candidates again
    and the work would be lost the moment the cap lifted.

    It is required rather than defaulted. This is the only path that takes a
    dispatch claim, so a caller that could omit the gate would be an uncapped
    dispatcher — and the omission would be invisible until a bill arrived, because
    an unbounded run spends exactly like a bounded one until it passes the bound.
    Requiring it makes forgetting a ``TypeError`` at the call site instead.
    """
    diff = diff_poll(state, outcome)
    candidates = diff.dispatchable
    if candidates and not gate.dispatch_allowed(diff.source):
        reason = f"the dispatch gate refused watch source {diff.source!r}"
        logger.warning("%s; %d candidate(s) left unclaimed", reason, len(candidates))
        return WatchAdvance(diff=diff, gated=candidates, gate_reason=reason)
    granted, withheld = claim_dispatches(state, diff, run_id=run_id)
    recorded = record_snapshot(state, diff)
    if diff.derived:
        logger.info("watch lifecycle: %s", diff.describe())
    return WatchAdvance(diff=diff, granted=granted, withheld=withheld, recorded=recorded)


def release_dispatch_claim(state: StateStore, source: str, item_id: str, generation: int) -> bool:
    """Drop one generation's dispatch claim so it can be dispatched again.

    The manual override. Keyed through :func:`generation_key` so an override
    cannot miss the claim it meant to release by formatting the generation
    differently from the dispatcher that wrote it.
    """
    return state.release_claim(
        CLAIM_DISPATCH, source, item_id, generation=generation_key(generation)
    )


def forget_snapshot(state: StateStore, source: str, item_id: str) -> bool:
    """Forget an item's snapshot row so the next poll re-offers it as new.

    The other half of the manual re-dispatch override, and the half
    :func:`release_dispatch_claim` cannot do. Releasing the claim was never what
    suppressed a waiting item: a still-open item already in the snapshot derives
    :attr:`Transition.UNCHANGED`, which is not a dispatch candidate whatever the
    ledger says. Forgetting the row is what turns the item's next observation back
    into :attr:`Transition.NEW`, at :data:`FIRST_GENERATION`, so it is a candidate
    again.

    The watch-layer name for :meth:`StateStore.forget_watch_item`, the same way
    :func:`release_dispatch_claim` names :meth:`StateStore.release_claim`: one
    idiom over the store so the override does not reach past the watch API into
    raw SQL. True when a row was forgotten, False when none was held.

    Deliberately not the default for any poll path -- re-offering every unchanged
    item each tick would spend on work nobody asked to redo. The suppression is
    kept; this is how an operator lifts it for one item.
    """
    return state.forget_watch_item(source, item_id)


def dispatched_generations(state: StateStore, source: str) -> dict[str, tuple[str, ...]]:
    """Every claimed generation per item for *source*, for display and diagnosis."""
    claimed: dict[str, list[str]] = {}
    for record in state.list_claims(kind=CLAIM_DISPATCH, scope=source):
        claimed.setdefault(record.subject, []).append(record.generation)
    return {subject: tuple(generations) for subject, generations in claimed.items()}


def is_open_state(text: str) -> bool:
    """Whether *text* reads as an item that is still actionable.

    Everything outside :data:`CLOSED_STATES` is open, including a blank state
    from a source that does not map the field: the item was on the list the
    tracker printed, and inferring closure from unfamiliar wording would derive
    cancellations from text a stranger chose.
    """
    return _normalized(text) not in CLOSED_STATES


def _derive(item: WatchedItem, previous: WatchItemRecord | None) -> ItemChange:
    is_open = is_open_state(item.state)
    if previous is None:
        return ItemChange(
            item=item,
            transition=Transition.NEW,
            generation=FIRST_GENERATION,
            previous_generation=None,
            is_open=is_open,
        )
    if is_open and not previous.is_open:
        return ItemChange(
            item=item,
            transition=Transition.REOPENED,
            generation=previous.generation + 1,
            previous_generation=previous.generation,
            is_open=True,
        )
    if previous.is_open and not is_open:
        return ItemChange(
            item=item,
            transition=Transition.CANCELLED,
            generation=previous.generation,
            previous_generation=previous.generation,
            is_open=False,
        )
    return ItemChange(
        item=item,
        transition=Transition.UNCHANGED,
        generation=previous.generation,
        previous_generation=previous.generation,
        is_open=is_open,
        edited=_is_edit(item, previous),
    )


def _is_edit(item: WatchedItem, previous: WatchItemRecord) -> bool:
    """Whether *item*'s content differs from the digest the snapshot recorded.

    A blank recorded digest reads as *unknown*, never as an edit: it is what a
    row written before digests were stored, or one an upgrade migrated in, holds,
    and comparing a real digest against a blank would report every such row as
    edited on the first poll after an upgrade. Only two real digests that differ
    are an edit. The item's own digest is always present (a sha256 of its fields),
    so the blank being guarded is always the recorded side.
    """
    recorded = previous.content_digest
    return bool(recorded) and recorded != item.content_digest


def _normalized(text: str) -> str:
    """Fold state text for the vocabulary comparison only.

    Separators become spaces so ``WONT_FIX``, ``wont-fix``, and ``Won't Fix``
    all reach the same entry, and the apostrophe is dropped for the same reason.
    The original text is what gets stored and shown.
    """
    folded = text.strip().casefold().replace("'", "").replace("\u2019", "")
    for separator in ("_", "-"):
        folded = folded.replace(separator, " ")
    return " ".join(folded.split())


def observations_of(changes: Sequence[ItemChange]) -> tuple[WatchObservation, ...]:
    """The snapshot rows *changes* would record, for a caller assembling its own set."""
    return tuple(change.observation for change in changes)
