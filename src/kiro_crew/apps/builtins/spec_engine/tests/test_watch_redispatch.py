"""Manual re-dispatch: forgetting the snapshot row is what re-offers a waiting item.

Releasing the dispatch claim was never what suppressed a still-open item -- the
snapshot row is, because an item already in ``watch_items`` derives ``unchanged``
and is not a dispatch candidate. So requirement 21.4's override needs a primitive
that forgets the row, and this proves that primitive turns the item's next
observation back into a candidate while leaving the default suppression in place.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from kiro_crew.apps.builtins.spec_engine.engine.state import StateStore
from kiro_crew.apps.builtins.spec_engine.engine.watch import (
    PollOutcome,
    PollStatus,
    Transition,
    WatchedItem,
    diff_poll,
    forget_snapshot,
    record_snapshot,
)

SOURCE = "tracker"


@pytest.fixture()
def state(tmp_path: Path) -> Iterator[StateStore]:
    store = StateStore(root=tmp_path / "state")
    yield store
    store.close()


def item(identifier: str, *, state_text: str = "open") -> WatchedItem:
    return WatchedItem(source=SOURCE, identifier=identifier, state=state_text)


def polled(*items: WatchedItem) -> PollOutcome:
    return PollOutcome(
        source=SOURCE, status=PollStatus.OK, items=items, program="tracker-cli", exit_code=0
    )


def transitions(state: StateStore, *items: WatchedItem) -> dict[str, Transition]:
    diff = diff_poll(state, polled(*items))
    return {change.identifier: change.transition for change in diff.changes}


class TestSuppressionByDefault:
    def test_a_still_open_item_derives_unchanged_on_the_next_poll(self, state: StateStore) -> None:
        """This is the suppression the override has to lift: it is not the claim."""
        record_snapshot(state, diff_poll(state, polled(item("5"))))
        assert transitions(state, item("5")) == {"5": Transition.UNCHANGED}


class TestForgetSnapshot:
    def test_forgetting_re_offers_the_item_as_new(self, state: StateStore) -> None:
        record_snapshot(state, diff_poll(state, polled(item("5"))))
        assert transitions(state, item("5")) == {"5": Transition.UNCHANGED}

        forgotten = forget_snapshot(state, SOURCE, "5")

        assert forgotten is True
        # The next observation is new again, at the first generation: a candidate.
        diff = diff_poll(state, polled(item("5")))
        (change,) = diff.changes
        assert change.transition is Transition.NEW
        assert change.dispatchable is True

    def test_forgetting_an_unrecorded_item_is_false(self, state: StateStore) -> None:
        assert forget_snapshot(state, SOURCE, "nope") is False

    def test_forgetting_one_item_leaves_the_others_suppressed(self, state: StateStore) -> None:
        record_snapshot(state, diff_poll(state, polled(item("5"), item("6"))))

        forget_snapshot(state, SOURCE, "5")

        result = transitions(state, item("5"), item("6"))
        assert result == {"5": Transition.NEW, "6": Transition.UNCHANGED}

    def test_the_state_row_is_gone(self, state: StateStore) -> None:
        record_snapshot(state, diff_poll(state, polled(item("5"))))
        assert state.get_watch_item(SOURCE, "5") is not None

        assert state.forget_watch_item(SOURCE, "5") is True
        assert state.get_watch_item(SOURCE, "5") is None
