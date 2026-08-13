"""The run lifecycle writes back to its triggering item at mapped transitions.

The feedback library is proved elsewhere; this proves it is *reached* from a real
transition, at the one seam every move is observed from, for the events a run
lifecycle owns: awaiting_review, completed, failed, refused. The dispatcher owns
``claimed`` and the delivery flow owns ``delivery_submitted``; each has its own
test beside this one.
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
from kiro_crew.apps.builtins.spec_engine.engine.runs import (
    RunMachine,
    RunState,
    lifecycle_event_for,
)
from kiro_crew.apps.builtins.spec_engine.engine.state import SpecRef, StateStore
from kiro_crew.apps.builtins.spec_engine.engine.watch.feedback import (
    AUDIT_ITEM_FEEDBACK,
    FeedbackOutcome,
    FeedbackPoster,
)

SOURCE = "tracker"


class Runner:
    """Records every argv it was handed and answers with a fixed outcome."""

    def __init__(self, *, exit_code: int = 0) -> None:
        self.calls: list[tuple[str, ...]] = []
        self._exit_code = exit_code

    def __call__(self, argv: Sequence[str], *, cwd: Path, timeout_s: int) -> CommandOutcome:
        self.calls.append(tuple(argv))
        return CommandOutcome(exit_code=self._exit_code, stdout="", stderr="", timed_out=False)

    @property
    def programs(self) -> list[str]:
        return [call[0] for call in self.calls]


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
def project(tmp_path: Path) -> Path:
    tree = tmp_path / "proj"
    tree.mkdir()
    return tree


@pytest.fixture()
def ref(project: Path) -> SpecRef:
    return SpecRef.of(project, "spec-one")


@pytest.fixture()
def runner() -> Runner:
    return Runner()


def with_feedback(config: ConfigStore, **events: Any) -> None:
    config.write(
        {"sources": {SOURCE: {"poll": ["tracker-cli"], "feedback": dict(events)}}},
        surface=DASHBOARD_SURFACE,
    )


def machine_with_feedback(
    state: StateStore,
    config: ConfigStore,
    audit: AuditLog,
    runner: Runner,
) -> RunMachine:
    poster = FeedbackPoster(state, config, audit, executor=StageExecutor(config, runner=runner))
    return RunMachine(state, config, audit=audit, feedback=poster)


def make_run(
    machine: RunMachine,
    ref: SpecRef,
    project: Path,
    *,
    source: str | None = SOURCE,
    item_id: str | None = "42",
) -> str:
    record = machine.create(
        ref,
        source=source,
        item_id=item_id,
        detail={
            "spec_type": "bugfix",
            "working_tree": str(project),
            "item_url": "https://tracker/42",
        },
    )
    return record.run_id


class TestLifecycleEventMapping:
    def test_awaiting_review_maps_to_its_event(self) -> None:
        assert (
            lifecycle_event_for(RunState.AUTHORING, RunState.AWAITING_REVIEW) == "awaiting_review"
        )

    def test_done_maps_to_completed(self) -> None:
        assert lifecycle_event_for(RunState.EXECUTING, RunState.DONE) == "completed"

    def test_a_failure_from_a_working_phase_is_failed(self) -> None:
        assert lifecycle_event_for(RunState.EXECUTING, RunState.FAILED) == "failed"

    def test_a_failure_straight_from_queued_is_a_refusal(self) -> None:
        """A run that never left queued did no work, so its watchers read it as
        the request declined, not as work that broke."""
        assert lifecycle_event_for(RunState.QUEUED, RunState.FAILED) == "refused"

    def test_transitions_with_no_watched_event_map_to_none(self) -> None:
        assert lifecycle_event_for(RunState.QUEUED, RunState.AUTHORING) is None
        assert lifecycle_event_for(RunState.AUTHORING, RunState.EXECUTING) is None
        assert lifecycle_event_for(RunState.EXECUTING, RunState.CANCELLED) is None


class TestWiredToTransitions:
    def test_reaching_awaiting_review_posts_that_events_feedback(
        self,
        state: StateStore,
        config: ConfigStore,
        audit: AuditLog,
        ref: SpecRef,
        project: Path,
        runner: Runner,
    ) -> None:
        with_feedback(config, awaiting_review=[["gh", "comment", "{item_id}"]])
        machine = machine_with_feedback(state, config, audit, runner)
        run_id = make_run(machine, ref, project)

        machine.transition(ref, run_id, RunState.AUTHORING)
        machine.transition(ref, run_id, RunState.AWAITING_REVIEW)

        assert runner.calls == [("gh", "comment", "42")]

    def test_completion_posts_the_completed_event(
        self,
        state: StateStore,
        config: ConfigStore,
        audit: AuditLog,
        ref: SpecRef,
        project: Path,
        runner: Runner,
    ) -> None:
        with_feedback(config, completed=[["gh", "close", "{item_id}"]])
        machine = machine_with_feedback(state, config, audit, runner)
        run_id = make_run(machine, ref, project)

        machine.transition(ref, run_id, RunState.AUTHORING)
        machine.transition(ref, run_id, RunState.DONE)

        assert runner.calls == [("gh", "close", "42")]

    def test_a_queued_run_that_fails_posts_refused_not_failed(
        self,
        state: StateStore,
        config: ConfigStore,
        audit: AuditLog,
        ref: SpecRef,
        project: Path,
        runner: Runner,
    ) -> None:
        """The two failure spellings are distinct commands, and a refusal must not
        run the failed command or the reverse."""
        with_feedback(
            config,
            refused=[["gh", "comment", "declined", "{item_id}"]],
            failed=[["gh", "comment", "broke", "{item_id}"]],
        )
        machine = machine_with_feedback(state, config, audit, runner)
        run_id = make_run(machine, ref, project)

        machine.transition(ref, run_id, RunState.FAILED)

        assert runner.calls == [("gh", "comment", "declined", "42")]

    def test_a_working_run_that_fails_posts_failed_not_refused(
        self,
        state: StateStore,
        config: ConfigStore,
        audit: AuditLog,
        ref: SpecRef,
        project: Path,
        runner: Runner,
    ) -> None:
        with_feedback(
            config,
            refused=[["gh", "comment", "declined", "{item_id}"]],
            failed=[["gh", "comment", "broke", "{item_id}"]],
        )
        machine = machine_with_feedback(state, config, audit, runner)
        run_id = make_run(machine, ref, project)

        machine.transition(ref, run_id, RunState.AUTHORING)
        machine.transition(ref, run_id, RunState.FAILED)

        assert runner.calls == [("gh", "comment", "broke", "42")]

    def test_a_transition_with_no_watched_event_posts_nothing(
        self,
        state: StateStore,
        config: ConfigStore,
        audit: AuditLog,
        ref: SpecRef,
        project: Path,
        runner: Runner,
    ) -> None:
        with_feedback(config, awaiting_review=[["gh", "comment", "{item_id}"]])
        machine = machine_with_feedback(state, config, audit, runner)
        run_id = make_run(machine, ref, project)

        machine.transition(ref, run_id, RunState.AUTHORING)  # queued -> authoring: no event

        assert runner.calls == []

    def test_a_refused_transition_posts_nothing(
        self,
        state: StateStore,
        config: ConfigStore,
        audit: AuditLog,
        ref: SpecRef,
        project: Path,
        runner: Runner,
    ) -> None:
        """An illegal move leaves the row untouched, so it must not write back a
        transition that did not happen."""
        from kiro_crew.apps.builtins.spec_engine.engine.runs import IllegalTransition

        with_feedback(config, completed=[["gh", "close", "{item_id}"]])
        machine = machine_with_feedback(state, config, audit, runner)
        run_id = make_run(machine, ref, project)
        machine.transition(ref, run_id, RunState.AUTHORING)
        machine.transition(ref, run_id, RunState.DONE)
        runner.calls.clear()

        with pytest.raises(IllegalTransition):
            machine.transition(ref, run_id, RunState.EXECUTING)

        assert runner.calls == []


class TestInteractiveAndUnconfigured:
    def test_a_run_with_no_source_posts_nothing(
        self,
        state: StateStore,
        config: ConfigStore,
        audit: AuditLog,
        ref: SpecRef,
        project: Path,
        runner: Runner,
    ) -> None:
        """A hand-authored spec reaches these transitions with no item to report."""
        with_feedback(config, awaiting_review=[["gh", "comment", "{item_id}"]])
        machine = machine_with_feedback(state, config, audit, runner)
        run_id = make_run(machine, ref, project, source=None, item_id=None)

        machine.transition(ref, run_id, RunState.AUTHORING)
        machine.transition(ref, run_id, RunState.AWAITING_REVIEW)

        assert runner.calls == []

    def test_an_event_the_source_did_not_configure_is_recorded_unconfigured(
        self,
        state: StateStore,
        config: ConfigStore,
        audit: AuditLog,
        ref: SpecRef,
        project: Path,
        runner: Runner,
    ) -> None:
        """A source that configured some feedback gets a stated answer, not silence,
        for an event it left out -- and still spawns nothing."""
        with_feedback(config, awaiting_review=[["gh", "comment", "{item_id}"]])
        machine = machine_with_feedback(state, config, audit, runner)
        run_id = make_run(machine, ref, project)

        machine.transition(ref, run_id, RunState.AUTHORING)
        machine.transition(ref, run_id, RunState.DONE)  # completed, not configured

        assert runner.calls == []
        feedback = [e for e in audit.read(ref) if e.event == AUDIT_ITEM_FEEDBACK]
        assert [(e.detail or {})["outcome"] for e in feedback] == [FeedbackOutcome.UNCONFIGURED.value]

    def test_the_writeback_is_recorded_in_the_audit_log(
        self,
        state: StateStore,
        config: ConfigStore,
        audit: AuditLog,
        ref: SpecRef,
        project: Path,
        runner: Runner,
    ) -> None:
        with_feedback(config, awaiting_review=[["gh", "comment", "{item_id}"]])
        machine = machine_with_feedback(state, config, audit, runner)
        run_id = make_run(machine, ref, project)

        machine.transition(ref, run_id, RunState.AUTHORING)
        machine.transition(ref, run_id, RunState.AWAITING_REVIEW)

        feedback = [e for e in audit.read(ref) if e.event == AUDIT_ITEM_FEEDBACK]
        assert len(feedback) == 1
        assert (feedback[0].detail or {})["event"] == "awaiting_review"
        assert (feedback[0].detail or {})["outcome"] == FeedbackOutcome.POSTED.value
