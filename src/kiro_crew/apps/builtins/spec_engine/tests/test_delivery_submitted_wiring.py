"""``delivery_submitted`` is wired at the engine's own construction point.

A seam nothing constructs passes every test it has. ``test_delivery_submitted_feedback``
proves the pipeline calls ``on_submitted`` and that the poster does the right
thing when a *test* points the one at the other; these prove the production
factory does that pointing, so deleting the wiring turns one red.

The event is also the one item lifecycle event no run-state move emits --
``lifecycle_event_for`` never names it, because raising a review artifact is a
stage outcome inside a delivery rather than a transition -- so this is its only
route, and there is no second path for it to arrive by.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.apps.builtins.spec_engine.engine.audit import AuditLog
from kiro_crew.apps.builtins.spec_engine.engine.autonomy import (
    AUTONOMY_FIELD,
    AutonomyDecision,
    AutonomyLevel,
)
from kiro_crew.apps.builtins.spec_engine.engine.config import DASHBOARD_SURFACE, ConfigStore
from kiro_crew.apps.builtins.spec_engine.engine.delivery import (
    ISOLATE_STAGE,
    PUBLISH_STAGE,
    SUBMIT_STAGE,
    VERIFY_STAGE,
    CommandOutcome,
    RunContext,
    resolve_authority,
)
from kiro_crew.apps.builtins.spec_engine.engine.orchestrator import orchestrator_for
from kiro_crew.apps.builtins.spec_engine.engine.runs import RunMachine, lifecycle_event_for
from kiro_crew.apps.builtins.spec_engine.engine.state import SpecRef, StateStore
from kiro_crew.apps.builtins.spec_engine.engine.watch.feedback import (
    AUDIT_ITEM_FEEDBACK,
    EVENT_DELIVERY_SUBMITTED,
    FeedbackOutcome,
)

PROJECT = "acme"
SOURCE = "tracker"
BASE = "main"
RUN = "run-1"
ARTIFACT = "https://tracker.invalid/acme/pull/7"


class Runner:
    """Records argv and answers success, printing the artifact URL for submit."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, argv: Sequence[str], *, cwd: Path, timeout_s: int) -> CommandOutcome:
        self.calls.append(tuple(argv))
        stdout = f"created {ARTIFACT}\n" if argv[0] == "raise-review" else ""
        return CommandOutcome(exit_code=0, stdout=stdout, stderr="")


def unused_worker(**_: Any) -> Any:  # pragma: no cover - the delivery never dispatches a task
    raise AssertionError("this test drives delivery only; no task should be dispatched")


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    path = tmp_path / "project"
    path.mkdir()
    return path


@pytest.fixture()
def config(tmp_path: Path, project: Path) -> ConfigStore:
    store = ConfigStore(root=tmp_path / "config")
    store.write(
        {
            "projects": {
                PROJECT: {
                    "path": str(project),
                    "base_branch": BASE,
                    "workflow": {
                        "stages": {
                            ISOLATE_STAGE: [["make-worktree"]],
                            SUBMIT_STAGE: [["raise-review"]],
                            VERIFY_STAGE: [["run-checks"]],
                            PUBLISH_STAGE: [["deploy"]],
                        }
                    },
                }
            },
            "sources": {
                SOURCE: {
                    "poll": ["tracker-cli"],
                    "feedback": {
                        EVENT_DELIVERY_SUBMITTED: [
                            ["gh", "comment", "{item_id}", "--body", "{review_url}"]
                        ]
                    },
                }
            },
        },
        surface=DASHBOARD_SURFACE,
    )
    return store


@pytest.fixture()
def state(tmp_path: Path) -> Iterator[StateStore]:
    store = StateStore(root=tmp_path / "engine-state")
    yield store
    store.close()


@pytest.fixture()
def audit(tmp_path: Path) -> AuditLog:
    return AuditLog(root=tmp_path / "audit")


@pytest.fixture()
def ref(project: Path) -> SpecRef:
    return SpecRef.of(project, "example")


def seed_run(
    state: StateStore, config: ConfigStore, ref: SpecRef, *, source: str | None = SOURCE
) -> None:
    """Record the run row the observer reads the tracker source off."""
    RunMachine(state, config, project=PROJECT).create(ref, run_id=RUN, source=source, item_id="42")


def deliver_through_the_factory(
    state: StateStore,
    config: ConfigStore,
    audit: AuditLog,
    ref: SpecRef,
    project: Path,
    runner: Runner,
) -> Any:
    """Build the runner the way a real caller does, then deliver through it."""
    authority = resolve_authority(
        config,
        decision=AutonomyDecision(
            level=AutonomyLevel.DELIVERY,
            source=SOURCE,
            spec_type="feature",
            submitter_class="maintainer",
            declared_at=f"sources.{SOURCE}.{AUTONOMY_FIELD}.maintainer.feature",
        ),
        project=PROJECT,
        base_branch=BASE,
    )
    wave_runner = orchestrator_for(
        ref,
        RUN,
        state=state,
        config=config,
        authority=authority,
        worker=unused_worker,
        reviewer=unused_worker,
        project=PROJECT,
        audit=audit,
        runner=runner,
    )
    context = RunContext(
        spec_name="example",
        spec_type="feature",
        workspace_path=str(project),
        base_branch=BASE,
        item_id="42",
    )
    # The pipeline the factory built, reached through the property that exists so
    # a caller continuing into delivery uses the run's own pipeline.
    return wave_runner.pipeline.deliver(context)


class TestTheFactoryWiresTheWriteback:
    def test_a_delivery_built_by_the_factory_posts_delivery_submitted(
        self,
        state: StateStore,
        config: ConfigStore,
        audit: AuditLog,
        ref: SpecRef,
        project: Path,
    ) -> None:
        seed_run(state, config, ref)
        runner = Runner()

        run = deliver_through_the_factory(state, config, audit, ref, project, runner)

        assert run.ok, run.reason
        # The writeback ran through the same runner the stages did, so its argv is
        # in the record: the artifact address the submit stage printed reached it.
        assert ("gh", "comment", "42", "--body", ARTIFACT) in runner.calls
        feedback = [e for e in audit.read(ref) if e.event == AUDIT_ITEM_FEEDBACK]
        assert [(e.detail or {})["event"] for e in feedback] == [EVENT_DELIVERY_SUBMITTED]
        assert (feedback[0].detail or {})["outcome"] == FeedbackOutcome.POSTED.value

    def test_it_posts_once_even_across_two_deliveries_of_the_run(
        self,
        state: StateStore,
        config: ConfigStore,
        audit: AuditLog,
        ref: SpecRef,
        project: Path,
    ) -> None:
        """The writeback ledger is the shared one, so a second delivery is silent.

        This is what wiring through the poster buys: the claim is keyed on the run
        and the event, so a re-delivered run -- a fix round after review feedback
        -- cannot comment on the item twice.
        """
        seed_run(state, config, ref)
        runner = Runner()

        deliver_through_the_factory(state, config, audit, ref, project, runner)
        deliver_through_the_factory(state, config, audit, ref, project, runner)

        posts = [call for call in runner.calls if call[0] == "gh"]
        assert len(posts) == 1
        outcomes = [
            (e.detail or {})["outcome"] for e in audit.read(ref) if e.event == AUDIT_ITEM_FEEDBACK
        ]
        assert outcomes == [FeedbackOutcome.POSTED.value]

    def test_a_run_with_no_tracker_source_posts_nothing(
        self,
        state: StateStore,
        config: ConfigStore,
        audit: AuditLog,
        ref: SpecRef,
        project: Path,
    ) -> None:
        """A spec a person authored by hand has no item to write back to."""
        seed_run(state, config, ref, source=None)
        runner = Runner()

        run = deliver_through_the_factory(state, config, audit, ref, project, runner)

        assert run.ok, run.reason
        assert [call for call in runner.calls if call[0] == "gh"] == []
        assert [e for e in audit.read(ref) if e.event == AUDIT_ITEM_FEEDBACK] == []


class TestNoSecondRouteForTheEvent:
    def test_no_run_state_transition_emits_delivery_submitted(self) -> None:
        """The event has one producer, so the ledger claim cannot race itself.

        Every other lifecycle event is reached from a run-state move. If a
        transition ever mapped to this one as well, the same run would have two
        producers for one at-most-once claim, and which of them posted would
        depend on ordering.
        """
        from kiro_crew.apps.builtins.spec_engine.engine.runs import RunState

        emitted = {
            lifecycle_event_for(from_state, to_state)
            for from_state in RunState
            for to_state in RunState
        }
        assert EVENT_DELIVERY_SUBMITTED not in emitted
