"""Item feedback: one writeback per run per event, recorded, never fatal, free."""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.apps.builtins.spec_engine.engine.audit import AuditLog
from kiro_crew.apps.builtins.spec_engine.engine.config.schema import (
    ITEM_LIFECYCLE_EVENTS,
    ConfigValidationError,
)
from kiro_crew.apps.builtins.spec_engine.engine.config.store import (
    DASHBOARD_SURFACE,
    ConfigStore,
)
from kiro_crew.apps.builtins.spec_engine.engine.delivery.stages import (
    CommandOutcome,
    StageExecutor,
)
from kiro_crew.apps.builtins.spec_engine.engine.delivery.variables import RunContext
from kiro_crew.apps.builtins.spec_engine.engine.state import (
    SpecRef,
    StatePersistenceError,
    StateStore,
)
from kiro_crew.apps.builtins.spec_engine.engine.watch.feedback import (
    AUDIT_ITEM_FEEDBACK,
    FeedbackOutcome,
    load_feedback,
    post_feedback,
)

SOURCE = "tracker"
RUN = "run-1"
EVENT = "claimed"


class Runner:
    """Records every argv it was handed and answers with a fixed outcome."""

    def __init__(self, *, exit_code: int = 0, start_error: str = "") -> None:
        self.calls: list[tuple[str, ...]] = []
        self._exit_code = exit_code
        self._start_error = start_error

    def __call__(
        self, argv: Sequence[str], *, cwd: Path, timeout_s: int
    ) -> CommandOutcome:
        self.calls.append(tuple(argv))
        return CommandOutcome(
            exit_code=self._exit_code,
            stdout="",
            stderr="",
            timed_out=False,
            start_error=self._start_error,
        )

    @property
    def programs(self) -> list[str]:
        return [call[0] for call in self.calls]


@pytest.fixture()
def config(tmp_path: Path) -> ConfigStore:
    return ConfigStore(root=tmp_path / "config")


@pytest.fixture()
def state(tmp_path: Path) -> Iterator[StateStore]:
    store = StateStore(tmp_path / "state")
    yield store
    store.close()


@pytest.fixture()
def audit(tmp_path: Path) -> AuditLog:
    return AuditLog(tmp_path / "audit")


@pytest.fixture()
def ref(tmp_path: Path) -> SpecRef:
    return SpecRef.of(tmp_path / "proj", "spec-one")


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    tree = tmp_path / "tree"
    tree.mkdir()
    return tree


@pytest.fixture()
def context(workspace: Path) -> RunContext:
    return RunContext(
        spec_name="spec-one",
        spec_type="bugfix",
        workspace_path=str(workspace),
        item_id="42",
    )


def configure(config: ConfigStore, patch: dict[str, Any]) -> None:
    config.write(patch, surface=DASHBOARD_SURFACE)


def with_feedback(config: ConfigStore, **events: Any) -> None:
    configure(
        config,
        {"sources": {SOURCE: {"poll": ["tracker-cli", "list"], "feedback": dict(events)}}},
    )


def hand_edit(config: ConfigStore, document: dict[str, Any]) -> None:
    """Write *document* straight to the config file, around the write path.

    The write path validates the feedback map, so an unparseable one cannot be
    produced through it. That is the second layer; the read-side defence in
    load_feedback is the first, and this is the state it exists for -- a document
    edited by hand. Testing it through the writer would be untestable, and
    treating it as unreachable would leave a typo resolving to silence.
    """
    config.path.parent.mkdir(parents=True, exist_ok=True)
    config.path.write_text(json.dumps(document), encoding="utf-8")


def executor(config: ConfigStore, runner: Runner) -> StageExecutor:
    return StageExecutor(config, runner=runner)


def post(
    state: StateStore,
    config: ConfigStore,
    audit: AuditLog,
    ref: SpecRef,
    context: RunContext,
    runner: Runner,
    *,
    event: str = EVENT,
    run_id: str = RUN,
):
    return post_feedback(
        state,
        config,
        audit,
        ref,
        source=SOURCE,
        event=event,
        run_id=run_id,
        context=context,
        executor=executor(config, runner),
    )


class TestLoading:
    def test_a_source_with_no_feedback_loads_empty(self, config: ConfigStore) -> None:
        configure(config, {"sources": {SOURCE: {"poll": ["tracker-cli"]}}})
        assert load_feedback(config, SOURCE) == {}

    def test_commands_load_per_event(self, config: ConfigStore) -> None:
        with_feedback(config, claimed=[["gh", "issue", "comment", "{item_id}"]])
        loaded = load_feedback(config, SOURCE)
        assert list(loaded) == ["claimed"]
        assert loaded["claimed"][0].program == "gh"

    def test_an_empty_command_list_is_an_error_not_silence(
        self, config: ConfigStore
    ) -> None:
        """The schema calls this "at least one command"; the loader must agree.

        Skipping it would report the event as UNCONFIGURED, which is the typo
        becoming silence that this loader exists to prevent.
        """
        hand_edit(
            config,
            {"sources": {SOURCE: {"poll": ["tracker-cli"], "feedback": {"claimed": []}}}},
        )
        with pytest.raises(ConfigValidationError):
            load_feedback(config, SOURCE)

    def test_an_undeclared_source_is_an_error_not_silence(
        self, config: ConfigStore
    ) -> None:
        """Strict about the event name and silent about the source name would make
        a misspelled source indistinguishable from one with nothing configured."""
        configure(config, {"sources": {SOURCE: {"poll": ["tracker-cli"]}}})
        with pytest.raises(ConfigValidationError):
            load_feedback(config, "githb")

    def test_an_unparseable_map_raises_rather_than_reading_as_no_feedback(
        self, config: ConfigStore
    ) -> None:
        """A typo must not turn into silence that looks like "none configured"."""
        hand_edit(
            config,
            {"sources": {SOURCE: {"poll": ["tracker-cli"], "feedback": {"claimed": [[]]}}}},
        )
        with pytest.raises(ConfigValidationError):
            load_feedback(config, SOURCE)

    def test_an_unknown_event_in_the_document_raises(self, config: ConfigStore) -> None:
        hand_edit(
            config,
            {
                "sources": {
                    SOURCE: {"poll": ["tracker-cli"], "feedback": {"shipped": [["gh"]]}}
                }
            },
        )
        with pytest.raises(ConfigValidationError):
            load_feedback(config, SOURCE)

    def test_a_feedback_map_that_is_not_an_object_raises(self, config: ConfigStore) -> None:
        hand_edit(
            config, {"sources": {SOURCE: {"poll": ["tracker-cli"], "feedback": ["gh"]}}}
        )
        with pytest.raises(ConfigValidationError):
            load_feedback(config, SOURCE)


class TestPosting:
    def test_configured_commands_run_for_the_event(
        self,
        state: StateStore,
        config: ConfigStore,
        audit: AuditLog,
        ref: SpecRef,
        context: RunContext,
    ) -> None:
        with_feedback(config, claimed=[["gh", "issue", "comment", "{item_id}"]])
        runner = Runner()

        report = post(state, config, audit, ref, context, runner)

        assert report.outcome is FeedbackOutcome.POSTED
        assert runner.calls == [("gh", "issue", "comment", "42")]

    def test_an_unconfigured_event_is_recorded_not_failed(
        self,
        state: StateStore,
        config: ConfigStore,
        audit: AuditLog,
        ref: SpecRef,
        context: RunContext,
    ) -> None:
        with_feedback(config, completed=[["gh", "issue", "close", "{item_id}"]])
        runner = Runner()

        report = post(state, config, audit, ref, context, runner, event="claimed")

        assert report.outcome is FeedbackOutcome.UNCONFIGURED
        assert runner.calls == []

    def test_only_the_events_configured_post(
        self,
        state: StateStore,
        config: ConfigStore,
        audit: AuditLog,
        ref: SpecRef,
        context: RunContext,
    ) -> None:
        with_feedback(
            config,
            claimed=[["gh", "comment", "{item_id}"]],
            completed=[["gh", "close", "{item_id}"]],
        )
        runner = Runner()

        post(state, config, audit, ref, context, runner, event="claimed")
        post(state, config, audit, ref, context, runner, event="failed")

        assert runner.programs == ["gh"]

    def test_an_unknown_event_is_rejected_at_the_call(
        self,
        state: StateStore,
        config: ConfigStore,
        audit: AuditLog,
        ref: SpecRef,
        context: RunContext,
    ) -> None:
        with pytest.raises(ValueError):
            post(state, config, audit, ref, context, Runner(), event="shipped-it")

    def test_feedback_must_name_its_run(
        self,
        state: StateStore,
        config: ConfigStore,
        audit: AuditLog,
        ref: SpecRef,
        context: RunContext,
    ) -> None:
        with pytest.raises(ValueError):
            post(state, config, audit, ref, context, Runner(), run_id="  ")


class TestAtMostOnce:
    def test_the_same_event_does_not_post_twice_for_one_run(
        self,
        state: StateStore,
        config: ConfigStore,
        audit: AuditLog,
        ref: SpecRef,
        context: RunContext,
    ) -> None:
        """A resumed run or a retried tick must not comment twice."""
        with_feedback(config, claimed=[["gh", "comment", "{item_id}"]])
        runner = Runner()

        first = post(state, config, audit, ref, context, runner)
        second = post(state, config, audit, ref, context, runner)

        assert first.outcome is FeedbackOutcome.POSTED
        assert second.outcome is FeedbackOutcome.ALREADY_POSTED
        assert len(runner.calls) == 1

    def test_a_different_run_posts_the_same_event(
        self,
        state: StateStore,
        config: ConfigStore,
        audit: AuditLog,
        ref: SpecRef,
        context: RunContext,
    ) -> None:
        """The claim is per run: a second item's run reports its own progress."""
        with_feedback(config, claimed=[["gh", "comment", "{item_id}"]])
        runner = Runner()

        post(state, config, audit, ref, context, runner, run_id="run-1")
        post(state, config, audit, ref, context, runner, run_id="run-2")

        assert len(runner.calls) == 2

    def test_a_failed_post_keeps_its_claim(
        self,
        state: StateStore,
        config: ConfigStore,
        audit: AuditLog,
        ref: SpecRef,
        context: RunContext,
    ) -> None:
        """Retrying a command that may already have commented is how one becomes two."""
        with_feedback(config, claimed=[["gh", "comment", "{item_id}"]])
        runner = Runner(exit_code=1)

        first = post(state, config, audit, ref, context, runner)
        second = post(state, config, audit, ref, context, runner)

        assert first.outcome is FeedbackOutcome.FAILED
        assert second.outcome is FeedbackOutcome.ALREADY_POSTED
        assert len(runner.calls) == 1

    def test_the_claim_is_taken_before_the_command_runs(
        self,
        state: StateStore,
        config: ConfigStore,
        audit: AuditLog,
        ref: SpecRef,
        context: RunContext,
    ) -> None:
        """A claim taken afterwards would let a crash post the same comment twice."""
        with_feedback(config, claimed=[["gh", "comment", "{item_id}"]])
        held: list[bool] = []

        class Crashing(Runner):
            def __call__(self, argv, *, cwd, timeout_s):  # type: ignore[no-untyped-def]
                # Whether the ledger already holds the claim at the moment the
                # command runs is the ordering under test.
                held.append(not state.claim_writeback(RUN, EVENT))
                raise AssertionError("the tracker went down mid-comment")

        with pytest.raises(AssertionError):
            post(state, config, audit, ref, context, Crashing())

        assert held == [True]


class TestFailureIsRecordedNotFatal:
    def test_a_failing_command_reports_failed_without_raising(
        self,
        state: StateStore,
        config: ConfigStore,
        audit: AuditLog,
        ref: SpecRef,
        context: RunContext,
    ) -> None:
        with_feedback(config, claimed=[["gh", "comment", "{item_id}"]])
        report = post(state, config, audit, ref, context, Runner(exit_code=1))

        assert report.outcome is FeedbackOutcome.FAILED
        assert report.reason

    def test_an_unreadable_configuration_reports_failed_without_raising(
        self,
        state: StateStore,
        config: ConfigStore,
        audit: AuditLog,
        ref: SpecRef,
        context: RunContext,
    ) -> None:
        configure(
            config,
            {"sources": {SOURCE: {"poll": ["tracker-cli"]}}},
        )
        hand_edit(
            config,
            {"sources": {SOURCE: {"poll": ["tracker-cli"], "feedback": {"claimed": [[]]}}}},
        )
        report = post(state, config, audit, ref, context, Runner())

        assert report.outcome is FeedbackOutcome.FAILED

    def test_a_valueless_variable_refuses_before_spawning_anything(
        self,
        state: StateStore,
        config: ConfigStore,
        audit: AuditLog,
        ref: SpecRef,
        workspace: Path,
    ) -> None:
        """Feedback inherits the executor's rule rather than restating it.

        An empty substitution would turn a comment on issue 42 into a comment on
        nothing, with the same exit code.
        """
        with_feedback(config, claimed=[["gh", "comment", "{item_id}"]])
        runner = Runner()
        no_item = RunContext(
            spec_name="spec-one", spec_type="bugfix", workspace_path=str(workspace)
        )

        report = post(state, config, audit, ref, no_item, runner)

        assert report.outcome is FeedbackOutcome.FAILED
        assert runner.calls == []


class TestAuditAndCost:
    def test_every_outcome_is_audited(
        self,
        state: StateStore,
        config: ConfigStore,
        audit: AuditLog,
        ref: SpecRef,
        context: RunContext,
    ) -> None:
        with_feedback(config, claimed=[["gh", "comment", "{item_id}"]])
        post(state, config, audit, ref, context, Runner())

        events = audit.read(ref)
        assert [event.event for event in events] == [AUDIT_ITEM_FEEDBACK]
        detail = events[0].detail or {}
        assert detail["event"] == EVENT
        assert detail["outcome"] == FeedbackOutcome.POSTED.value

    def test_a_failure_is_audited_too(
        self,
        state: StateStore,
        config: ConfigStore,
        audit: AuditLog,
        ref: SpecRef,
        context: RunContext,
    ) -> None:
        """The failure surfaces even though the run continues."""
        with_feedback(config, claimed=[["gh", "comment", "{item_id}"]])
        post(state, config, audit, ref, context, Runner(exit_code=1))

        detail = audit.read(ref)[0].detail or {}
        assert detail["outcome"] == FeedbackOutcome.FAILED.value
        assert detail["reason"]

    def test_an_unwritable_audit_log_does_not_fail_the_run(
        self,
        state: StateStore,
        config: ConfigStore,
        ref: SpecRef,
        context: RunContext,
        tmp_path: Path,
    ) -> None:
        """R36.6: a writeback failure must not fail the run.

        The audit append happens after the comment has already landed, so an
        unwritable log turning into an exception would make the one thing that
        must not fail the run into the thing that fails it.
        """

        class Unwritable(AuditLog):
            def append(self, *args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
                raise StatePersistenceError("the disk is full")

        with_feedback(config, claimed=[["gh", "comment", "{item_id}"]])
        runner = Runner()

        report = post_feedback(
            state,
            config,
            Unwritable(tmp_path / "audit"),
            ref,
            source=SOURCE,
            event=EVENT,
            run_id=RUN,
            context=context,
            executor=executor(config, runner),
        )

        assert report.outcome is FeedbackOutcome.POSTED
        assert runner.calls == [("gh", "comment", "42")]

    def test_feedback_costs_no_model_credits(
        self,
        state: StateStore,
        config: ConfigStore,
        audit: AuditLog,
        ref: SpecRef,
        context: RunContext,
    ) -> None:
        with_feedback(config, claimed=[["gh", "comment", "{item_id}"]])
        post(state, config, audit, ref, context, Runner())

        assert audit.read(ref)[0].cost == 0.0

    def test_the_record_names_variables_rather_than_their_values(
        self,
        state: StateStore,
        config: ConfigStore,
        audit: AuditLog,
        ref: SpecRef,
        workspace: Path,
    ) -> None:
        """An item title is attacker-chosen text; an audit reader did not pick it."""
        with_feedback(config, claimed=[["gh", "comment", "{review_title}"]])
        titled = RunContext(
            spec_name="spec-one",
            spec_type="bugfix",
            workspace_path=str(workspace),
            item_id="42",
            review_title="pwn <script>",
        )

        post(state, config, audit, ref, titled, Runner())

        detail = audit.read(ref)[0].detail or {}
        assert "review_title" in detail["variables_used"]
        assert "pwn" not in str(detail)


class TestVocabulary:
    def test_the_events_are_the_configured_vocabulary(self) -> None:
        """One list, so a schema-valid event cannot be unknown to this module."""
        assert EVENT in ITEM_LIFECYCLE_EVENTS
        assert "completed" in ITEM_LIFECYCLE_EVENTS
