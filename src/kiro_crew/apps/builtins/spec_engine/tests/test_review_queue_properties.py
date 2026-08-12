"""Property-based tests for the Review_Queue and the archival rules.

**Membership is a function of state and archival, nothing else.** Whatever
sequence of transitions, sweeps, clock advances, archivals, and refused
archivals a caller performs, a run is in the queue exactly when it sits in a
human-reserved state and its spec is not archived. Scripted cases cover the
sequences someone thought of; the failures this guards against are the ones an
unimagined order produces — a run waiting on a person that appears in nobody's
list, or a run in a reviewer's list that nothing is waiting for.

**Only an explicit archival changes the archived flag.** The same generated
sequences assert the flag equals what the explicit archive and unarchive calls
in them said, so a clock advance, a sweep, a transition, or a refused archival
that moved it is a failure however deep in the sequence it happened.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from kiro_crew.apps.builtins.spec_engine.engine.config import ConfigStore
from kiro_crew.apps.builtins.spec_engine.engine.review_queue import (
    HUMAN_RESERVED_STATES,
    ArchivalRefused,
    ReviewQueue,
)
from kiro_crew.apps.builtins.spec_engine.engine.runs import (
    IllegalTransition,
    RunMachine,
    RunState,
    StallNotice,
)
from kiro_crew.apps.builtins.spec_engine.engine.state import SpecRef, StateStore

from .conftest import make_spec_dir

#: Each example drives several SQLite transactions against a real store, so keep
#: the count modest rather than trading suite time for breadth.
MAX_EXAMPLES = 50

#: The item the generated runs are attributed to, so a cancellation cascade in a
#: sequence has something to match on.
ITEM_ID = "42"

_OPERATIONS = st.lists(
    st.one_of(
        st.tuples(st.just("archive"), st.none()),
        st.tuples(st.just("unarchive"), st.none()),
        st.tuples(st.just("cancel_item"), st.none()),
        st.tuples(st.just("refuse"), st.sampled_from(["expired", "retention", "elapsed", ""])),
        st.tuples(st.just("to"), st.sampled_from(list(RunState))),
        st.tuples(st.just("sweep"), st.none()),
        st.tuples(st.just("advance"), st.sampled_from([1, 600, 86_400, 31_536_000])),
    ),
    min_size=1,
    max_size=14,
)


class _Clock:
    def __init__(self) -> None:
        self._now = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now = self._now + timedelta(seconds=seconds)


def _swallow(notice: StallNotice) -> None:
    """Stall delivery is not what these properties are about."""


def _engine(root: Path, name: str) -> tuple[RunMachine, ReviewQueue, SpecRef, _Clock]:
    project = root / name
    project.mkdir(parents=True, exist_ok=True)
    make_spec_dir(project, "example")
    clock = _Clock()
    machine = RunMachine(
        StateStore(root=root / f"{name}-state"),
        ConfigStore(root=root / f"{name}-config"),
        notifier=_swallow,
        clock=clock,
    )
    return machine, ReviewQueue(machine), SpecRef.of(project, "example"), clock


@settings(max_examples=MAX_EXAMPLES, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(_OPERATIONS, st.integers(min_value=0, max_value=10_000))
def test_queue_membership_and_archival_hold_over_any_operation_sequence(
    tmp_path_factory, operations: list[tuple[str, object]], seed: int
) -> None:
    machine, queue, ref, clock = _engine(tmp_path_factory.mktemp("queueprop"), f"p{seed}")
    machine.create(ref, run_id="run-1", item_id=ITEM_ID, source="github")
    archived = False

    for operation, argument in operations:
        if operation == "archive":
            queue.archive(ref, actor="user:someone")
            archived = True
        elif operation == "unarchive":
            queue.unarchive(ref, actor="user:someone")
            archived = False
        elif operation == "cancel_item":
            queue.archive_cancelled_item(ref, item_id=ITEM_ID, actor="watcher:github")
            archived = True
        elif operation == "refuse":
            try:
                queue.archive(ref, cause=str(argument))
            except ArchivalRefused:
                pass
            else:  # pragma: no cover - a refused cause that was accepted
                raise AssertionError(f"{argument!r} was accepted as an archival cause")
        elif operation == "to":
            assert isinstance(argument, RunState)
            try:
                machine.transition(ref, "run-1", argument)
            except IllegalTransition:
                pass
        elif operation == "sweep":
            machine.sweep_stalled()
        elif operation == "advance":
            assert isinstance(argument, int)
            clock.advance(argument)

        # The flag tracks the explicit archivals in the sequence and nothing
        # else: no transition, sweep, clock advance, or refused cause may move it.
        assert queue.is_archived(ref) is archived
        state = machine.state_of("run-1")
        expected = state in HUMAN_RESERVED_STATES and not archived
        assert queue.holds("run-1") is expected
        # The projection agrees with itself: an entry exists exactly when the
        # membership rule says one should, and carries that state.
        entries = queue.snapshot().for_spec(ref)
        assert tuple(entry.state for entry in entries) == ((state,) if expected else ())
