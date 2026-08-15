"""The actions a Review_Queue row offers, and where their rules live.

A reviewer acts on a queue row, so the queue is where the actions hang. What is
under test here is that each one is a DELEGATION to the module that owns the rule,
not a second implementation of it -- because a second spelling would be a second
answer to a question the engine has already settled: which comment was actually
held, what re-offers a suppressed item, when a workspace row may be closed out.

Each test therefore asserts the effect in the OWNING module's own terms (the run's
held list and the claim ledger, the watch snapshot, the workspace ledger) rather
than only that the queue method returned something.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kiro_crew.apps.builtins.spec_engine.engine.audit import AuditLog
from kiro_crew.apps.builtins.spec_engine.engine.config import ConfigStore
from kiro_crew.apps.builtins.spec_engine.engine.delivery.isolation import TEMP_COPY_KIND
from kiro_crew.apps.builtins.spec_engine.engine.delivery.teardown import WorkspaceJanitor
from kiro_crew.apps.builtins.spec_engine.engine.review_queue import (
    ReviewFeedbackRefused,
    ReviewQueue,
)
from kiro_crew.apps.builtins.spec_engine.engine.runs import RunMachine, RunState
from kiro_crew.apps.builtins.spec_engine.engine.state import (
    CLAIM_DISPATCH,
    SpecRef,
    StateStore,
    WatchObservation,
)
from kiro_crew.apps.builtins.spec_engine.engine.watch.lifecycle import generation_key
from kiro_crew.apps.builtins.spec_engine.engine.watch.review_feedback import (
    CLAIM_REVIEW_COMMENT,
    DETAIL_FEEDBACK_QUARANTINED,
)


@pytest.fixture()
def audit(tmp_path: Path) -> AuditLog:
    return AuditLog(root=tmp_path / "audit")


@pytest.fixture()
def machine(store: StateStore, tmp_path: Path, audit: AuditLog) -> RunMachine:
    return RunMachine(store, ConfigStore(root=tmp_path / "config"), audit=audit)


@pytest.fixture()
def queue(machine: RunMachine) -> ReviewQueue:
    return ReviewQueue(machine)


def observe(store: StateStore, item_id: str) -> None:
    """Record one open watched item, the way a poll does."""
    store.record_watch_items(
        "github", [WatchObservation(item_id=item_id, generation=1, item_state="open")]
    )


def park_for_review(machine: RunMachine, ref: SpecRef, run_id: str) -> None:
    machine.create(ref, run_id=run_id, source="github")
    machine.transition(ref, run_id, RunState.AUTHORING)
    machine.transition(ref, run_id, RunState.AWAITING_REVIEW)


class TestQuarantineRelease:
    def test_releasing_a_held_comment_clears_the_hold_and_its_claim(
        self, store: StateStore, machine: RunMachine, queue: ReviewQueue, ref: SpecRef
    ) -> None:
        park_for_review(machine, ref, "run-1")
        store.update_run("run-1", detail={DETAIL_FEEDBACK_QUARANTINED: ["c-1", "c-2"]})
        store.claim(CLAIM_REVIEW_COMMENT, "run-1", "c-1", run_id="run-1")

        released = queue.release_quarantined_feedback(ref, "run-1", "c-1", actor="reviewer")

        assert released is True
        record = store.get_run("run-1")
        assert record is not None
        # The hold is lifted for that comment only, and the claim is gone -- which
        # is what lets the next poll re-derive the decision rather than skip it as
        # already seen. Releasing the hold alone would leave the comment invisible.
        assert record.detail[DETAIL_FEEDBACK_QUARANTINED] == ["c-2"]
        assert store.get_claim(CLAIM_REVIEW_COMMENT, "run-1", "c-1") is None

    def test_releasing_a_comment_nobody_held_reports_that_it_changed_nothing(
        self, store: StateStore, machine: RunMachine, queue: ReviewQueue, ref: SpecRef
    ) -> None:
        park_for_review(machine, ref, "run-1")
        store.update_run("run-1", detail={DETAIL_FEEDBACK_QUARANTINED: ["c-1"]})

        assert queue.release_quarantined_feedback(ref, "run-1", "c-9") is False
        record = store.get_run("run-1")
        assert record is not None
        assert record.detail[DETAIL_FEEDBACK_QUARANTINED] == ["c-1"]

    def test_the_release_shows_on_the_queue_entry(
        self, store: StateStore, machine: RunMachine, queue: ReviewQueue, ref: SpecRef
    ) -> None:
        park_for_review(machine, ref, "run-1")
        store.update_run("run-1", detail={DETAIL_FEEDBACK_QUARANTINED: ["c-1", "c-2"]})
        before = queue.snapshot().entries[0].feedback_quarantined

        queue.release_quarantined_feedback(ref, "run-1", "c-1")

        # The count on the row is what a surface renders, so a release that did not
        # move it would leave a reviewer looking at work they have already done.
        assert (before, queue.snapshot().entries[0].feedback_quarantined) == (2, 1)

    def test_a_release_is_refused_when_nothing_would_record_it(
        self, store: StateStore, tmp_path: Path, ref: SpecRef
    ) -> None:
        # A privileged manual action that leaves no trace is worse than one that
        # did not happen: the release is what lets a held comment drive a fix
        # dispatch, and an operator would later find the dispatch with nothing
        # saying who allowed it.
        machine = RunMachine(store, ConfigStore(root=tmp_path / "config2"))
        queue = ReviewQueue(machine)
        park_for_review(machine, ref, "run-1")
        store.update_run("run-1", detail={DETAIL_FEEDBACK_QUARANTINED: ["c-1"]})

        with pytest.raises(ReviewFeedbackRefused):
            queue.release_quarantined_feedback(ref, "run-1", "c-1")

        record = store.get_run("run-1")
        assert record is not None
        assert record.detail[DETAIL_FEEDBACK_QUARANTINED] == ["c-1"]


class TestManualRedispatch:
    def test_the_override_lifts_both_halves_of_the_suppression(
        self, store: StateStore, queue: ReviewQueue
    ) -> None:
        observe(store, "42")
        store.claim(CLAIM_DISPATCH, "github", "42", generation=generation_key(1))

        assert queue.redispatch_item("github", "42", generation=1) is True

        # Releasing the claim alone was never what suppressed a waiting item: a
        # still-open item already in the snapshot derives UNCHANGED, which is not a
        # dispatch candidate whatever the ledger says. Both have to go.
        assert store.get_claim(CLAIM_DISPATCH, "github", "42", generation=generation_key(1)) is None
        assert store.get_watch_item("github", "42") is None

    def test_an_override_for_an_item_nothing_suppressed_changes_nothing(
        self, queue: ReviewQueue
    ) -> None:
        assert queue.redispatch_item("github", "never-seen", generation=1) is False

    def test_the_override_leaves_another_items_suppression_standing(
        self, store: StateStore, queue: ReviewQueue
    ) -> None:
        observe(store, "42")
        observe(store, "43")

        queue.redispatch_item("github", "42", generation=1)

        # A forget that missed its item predicate would re-offer the whole backlog,
        # and a test that looked at only the target item would still pass.
        assert store.get_watch_item("github", "43") is not None


class TestManualWorkspaceCleanup:
    def test_cleaning_one_workspace_closes_its_ledger_row(
        self, store: StateStore, machine: RunMachine, ref: SpecRef, tmp_path
    ) -> None:
        park_for_review(machine, ref, "run-1")
        disposable = tmp_path / "disposable"
        tree = disposable / "tree"
        tree.mkdir(parents=True)
        record = store.record_workspace("run-1", kind=TEMP_COPY_KIND, location=tree)
        # Rooted where the run's own janitor is rooted, which is what makes a copy
        # removable at all: a janitor with no disposable root deletes nothing by
        # path, on purpose.
        queue = ReviewQueue(machine, janitor=WorkspaceJanitor(store, root=disposable))

        cleanup = queue.clean_workspace(record.workspace_id)

        assert cleanup is not None and cleanup.removed is True
        assert not tree.exists()
        # Closed out in the ledger the janitor owns, which is the only record that
        # the tree existed. A cleanup that removed the directory and left the row
        # would have the sweep try again forever.
        assert store.list_workspaces(run_id="run-1") == []

    def test_the_default_janitor_keeps_a_tree_it_cannot_place_and_says_why(
        self, store: StateStore, machine: RunMachine, queue: ReviewQueue, ref: SpecRef, tmp_path
    ) -> None:
        # The default carries no disposable root, so it reports rather than
        # deleting a path on a guess. Kept-with-a-reason is the answer a surface
        # renders; silently reporting success would lose the tree's only record.
        park_for_review(machine, ref, "run-1")
        tree = tmp_path / "unplaceable"
        tree.mkdir()
        record = store.record_workspace("run-1", kind=TEMP_COPY_KIND, location=tree)

        cleanup = queue.clean_workspace(record.workspace_id)

        assert cleanup is not None and cleanup.removed is False
        assert cleanup.reason
        assert tree.exists()
        assert [r.workspace_id for r in store.list_workspaces(run_id="run-1")] == [
            record.workspace_id
        ]

    def test_cleaning_a_workspace_nobody_has_answers_rather_than_pretending(
        self, queue: ReviewQueue
    ) -> None:
        # A double click is answered, not mistaken for a second removal.
        assert queue.clean_workspace(9999) is None

    def test_tearing_down_a_run_reports_what_it_did(
        self, store: StateStore, machine: RunMachine, queue: ReviewQueue, ref: SpecRef, tmp_path
    ) -> None:
        park_for_review(machine, ref, "run-1")
        tree = tmp_path / "run-tree"
        tree.mkdir()
        store.record_workspace("run-1", kind=TEMP_COPY_KIND, location=tree)

        report = queue.teardown_run_workspaces("run-1")

        assert report.run_id == "run-1"
        assert len(report.cleanups) == 1


class TestTheActionsAreDelegations:
    def test_the_queue_holds_no_second_copy_of_the_rules(self) -> None:
        """The absence this file exists to pin, read off the source.

        Each action is one call into the module that owns it. A queue that grew its
        own idea of which comment was held, what re-offers an item, or when a
        workspace row may close would be a second authority on each -- and the two
        would disagree the first time only one of them was changed.
        """
        import inspect

        from kiro_crew.apps.builtins.spec_engine.engine import review_queue

        source = inspect.getsource(review_queue)
        # No raw SQL and no direct claim or ledger manipulation for these actions:
        # the queue reaches the store through the owning module's own idiom.
        assert "release_claims(" not in source
        assert "forget_watch_item(" not in source
        assert "DELETE FROM" not in source
        for owner in (
            "release_quarantined_comment(",
            "release_dispatch_claim(",
            "forget_snapshot(",
        ):
            assert owner in source, f"{owner} is the owning primitive and must be the one called"
