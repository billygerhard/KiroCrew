"""The operator surface for a suppressed writeback, and the poster that gates it.

A failed feedback post keeps its writeback claim on purpose -- retrying a command
that may already have commented is how one event becomes two -- so the event is
suppressed for the run until an operator who knows what landed clears it. This
covers the release twin of the dispatch release, the report's suppression
surface, and the poster that decides which sources write back at all.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.apps.builtins.spec_engine.engine.audit import AuditLog
from kiro_crew.apps.builtins.spec_engine.engine.config import DASHBOARD_SURFACE, ConfigStore
from kiro_crew.apps.builtins.spec_engine.engine.delivery.stages import (
    CommandOutcome,
    StageExecutor,
)
from kiro_crew.apps.builtins.spec_engine.engine.delivery.variables import RunContext
from kiro_crew.apps.builtins.spec_engine.engine.state import (
    CLAIM_WRITEBACK,
    SpecRef,
    StateStore,
)
from kiro_crew.apps.builtins.spec_engine.engine.watch.feedback import (
    FeedbackOutcome,
    FeedbackPoster,
    FeedbackReport,
    post_feedback,
    release_writeback_claim,
)

SOURCE = "tracker"
RUN = "run-1"
EVENT = "claimed"


class Runner:
    def __init__(self, *, exit_code: int = 0) -> None:
        self.calls: list[tuple[str, ...]] = []
        self._exit_code = exit_code

    def __call__(self, argv: Sequence[str], *, cwd: Path, timeout_s: int) -> CommandOutcome:
        self.calls.append(tuple(argv))
        return CommandOutcome(exit_code=self._exit_code, stdout="", stderr="", timed_out=False)


@pytest.fixture()
def config(tmp_path: Path) -> ConfigStore:
    return ConfigStore(root=tmp_path / "config")


@pytest.fixture()
def state(tmp_path: Path) -> Iterator[StateStore]:
    store = StateStore(root=tmp_path / "state")
    yield store
    store.close()


@pytest.fixture()
def audit(tmp_path: Path) -> AuditLog:
    return AuditLog(root=tmp_path / "state")


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    tree = tmp_path / "tree"
    tree.mkdir()
    return tree


@pytest.fixture()
def ref(tmp_path: Path) -> SpecRef:
    return SpecRef.of(tmp_path / "proj", "spec-one")


@pytest.fixture()
def context(workspace: Path) -> RunContext:
    return RunContext(
        spec_name="spec-one", spec_type="bugfix", workspace_path=str(workspace), item_id="42"
    )


def with_feedback(config: ConfigStore, **events: Any) -> None:
    config.write(
        {"sources": {SOURCE: {"poll": ["tracker-cli"], "feedback": dict(events)}}},
        surface=DASHBOARD_SURFACE,
    )


def post(state, config, audit, ref, context, runner, *, event=EVENT, run_id=RUN):
    return post_feedback(
        state,
        config,
        audit,
        ref,
        source=SOURCE,
        event=event,
        run_id=run_id,
        context=context,
        executor=StageExecutor(config, runner=runner),
    )


class TestReleaseWritebackClaim:
    def test_a_failed_post_is_suppressed_until_released_then_posts(
        self, state, config, audit, ref, context
    ) -> None:
        with_feedback(config, claimed=[["gh", "comment", "{item_id}"]])

        first = post(state, config, audit, ref, context, Runner(exit_code=1))
        held = post(state, config, audit, ref, context, Runner())
        released = release_writeback_claim(state, RUN, EVENT)
        after = post(state, config, audit, ref, context, Runner())

        assert first.outcome is FeedbackOutcome.FAILED
        assert held.outcome is FeedbackOutcome.ALREADY_POSTED  # the failed claim held
        assert released is True
        assert after.outcome is FeedbackOutcome.POSTED

    def test_releasing_an_unheld_claim_is_false(self, state) -> None:
        assert release_writeback_claim(state, RUN, EVENT) is False

    def test_it_clears_the_row_named_by_the_report(
        self, state, config, audit, ref, context
    ) -> None:
        """The report names the exact ledger row; releasing that row clears it."""
        with_feedback(config, claimed=[["gh", "comment", "{item_id}"]])
        report = post(state, config, audit, ref, context, Runner(exit_code=1))
        row = report.clears()

        assert state.get_claim(row["kind"], row["scope"], row["subject"]) is not None
        assert release_writeback_claim(state, row["scope"], row["subject"]) is True
        assert state.get_claim(row["kind"], row["scope"], row["subject"]) is None

    def test_an_unknown_event_is_rejected(self, state) -> None:
        with pytest.raises(ValueError):
            release_writeback_claim(state, RUN, "shipped-it")

    def test_a_release_must_name_its_run(self, state) -> None:
        with pytest.raises(ValueError):
            release_writeback_claim(state, "  ", EVENT)


class TestSuppressionSurface:
    def test_a_failed_report_is_suppressed_and_names_its_row(self) -> None:
        report = FeedbackReport(
            source=SOURCE, event=EVENT, run_id=RUN, outcome=FeedbackOutcome.FAILED
        )
        assert report.suppressed is True
        assert report.clears() == {"kind": CLAIM_WRITEBACK, "scope": RUN, "subject": EVENT}
        note = report.suppression_note()
        assert "release_writeback_claim" in note
        assert EVENT in note and RUN in note
        detail = report.detail()
        assert detail["suppressed"] is True
        assert detail["clears"]["kind"] == CLAIM_WRITEBACK

    @pytest.mark.parametrize(
        "outcome",
        [FeedbackOutcome.POSTED, FeedbackOutcome.ALREADY_POSTED, FeedbackOutcome.UNCONFIGURED],
    )
    def test_only_a_failure_suppresses(self, outcome: FeedbackOutcome) -> None:
        """ALREADY_POSTED means an earlier attempt held, not that this one needs a release."""
        report = FeedbackReport(source=SOURCE, event=EVENT, run_id=RUN, outcome=outcome)
        assert report.suppressed is False
        assert report.clears() == {}
        assert report.suppression_note() == ""
        assert "suppressed" not in report.detail()


class TestFeedbackPoster:
    def test_a_run_with_no_source_posts_nothing(self, state, config, audit, ref, context) -> None:
        with_feedback(config, claimed=[["gh", "comment", "{item_id}"]])
        poster = FeedbackPoster(state, config, audit)
        assert poster.post(ref, source=None, run_id=RUN, event=EVENT, context=context) is None

    def test_a_source_with_no_feedback_map_posts_nothing(
        self, state, config, audit, ref, context
    ) -> None:
        config.write({"sources": {SOURCE: {"poll": ["tracker-cli"]}}}, surface=DASHBOARD_SURFACE)
        poster = FeedbackPoster(state, config, audit)
        assert poster.post(ref, source=SOURCE, run_id=RUN, event=EVENT, context=context) is None

    def test_a_declared_map_missing_this_event_is_a_stated_unconfigured(
        self, state, config, audit, ref, context
    ) -> None:
        """A source that asked for some feedback gets a report, not silence, for an
        event it left out -- the difference between 'ran, nothing here' and 'no run'."""
        with_feedback(config, completed=[["gh", "close", "{item_id}"]])
        runner = Runner()
        poster = FeedbackPoster(state, config, audit, executor=StageExecutor(config, runner=runner))
        report = poster.post(ref, source=SOURCE, run_id=RUN, event=EVENT, context=context)
        assert report is not None
        assert report.outcome is FeedbackOutcome.UNCONFIGURED
        assert runner.calls == []

    def test_a_configured_event_posts_through_the_default_executor(
        self, state, config, audit, ref, context
    ) -> None:
        with_feedback(config, claimed=[["gh", "comment", "{item_id}"]])
        runner = Runner()
        poster = FeedbackPoster(state, config, audit, executor=StageExecutor(config, runner=runner))
        report = poster.post(ref, source=SOURCE, run_id=RUN, event=EVENT, context=context)
        assert report is not None and report.outcome is FeedbackOutcome.POSTED
        assert runner.calls == [("gh", "comment", "42")]
