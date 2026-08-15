"""The delivery flow announces ``delivery_submitted`` after the artifact is raised.

The pipeline owns *when* the review artifact exists; the writeback itself is the
watch layer's one shared poster, reached through the ``on_submitted`` seam. This
proves the seam fires once, after a submit that actually ran, and not when there
is no submit stage to raise anything.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
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
    DeliveryPipeline,
    RunContext,
    resolve_authority,
)
from kiro_crew.apps.builtins.spec_engine.engine.delivery.stages import StageExecutor
from kiro_crew.apps.builtins.spec_engine.engine.state import SpecRef, StateStore
from kiro_crew.apps.builtins.spec_engine.engine.watch.feedback import (
    AUDIT_ITEM_FEEDBACK,
    FeedbackOutcome,
    FeedbackPoster,
)

PROJECT = "acme"
SOURCE = "tracker"
BASE = "main"
RUN = "run-1"


class Runner:
    """Records argv and answers success (the delivery stages and the feedback both)."""

    def __init__(self, stdout_for: Mapping[str, str] | None = None) -> None:
        self.calls: list[tuple[str, ...]] = []
        self._stdout_for = dict(stdout_for or {})

    def __call__(self, argv: Sequence[str], *, cwd: Path, timeout_s: int) -> CommandOutcome:
        self.calls.append(tuple(argv))
        return CommandOutcome(exit_code=0, stdout=self._stdout_for.get(argv[0], ""), stderr="")


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    path = tmp_path / "workspace"
    path.mkdir()
    return path


@pytest.fixture()
def config(tmp_path: Path, workspace: Path) -> ConfigStore:
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
def ref(workspace: Path) -> SpecRef:
    return SpecRef.of(workspace, "example")


def configure(
    config: ConfigStore,
    workspace: Path,
    *,
    with_submit: bool = True,
    feedback_argv: list[list[str]] | None = None,
    verify_argv: list[list[str]] | None = None,
) -> None:
    stages: dict[str, Any] = {
        ISOLATE_STAGE: [["make-worktree"]],
        VERIFY_STAGE: verify_argv or [["run-checks"]],
        PUBLISH_STAGE: [["deploy"]],
    }
    if with_submit:
        stages[SUBMIT_STAGE] = [["raise-review", "--title", "{review_title}"]]
    config.write(
        {
            "projects": {
                PROJECT: {
                    "path": str(workspace),
                    "base_branch": BASE,
                    "workflow": {"stages": stages},
                }
            },
            "sources": {
                SOURCE: {
                    "poll": ["tracker-cli"],
                    "feedback": {
                        "delivery_submitted": feedback_argv or [["gh", "comment", "{item_id}"]]
                    },
                }
            },
        },
        surface=DASHBOARD_SURFACE,
    )


def context(workspace: Path) -> RunContext:
    return RunContext(
        spec_name="example",
        spec_type="feature",
        workspace_path=str(workspace),
        base_branch=BASE,
        review_title="Example",
        item_id="42",
    )


def build_pipeline(
    config: ConfigStore,
    state: StateStore,
    audit: AuditLog,
    ref: SpecRef,
    delivery_runner: Runner,
    feedback_runner: Runner,
    *,
    on_submitted_wired: bool = True,
) -> DeliveryPipeline:
    decision = AutonomyDecision(
        level=AutonomyLevel.DELIVERY,
        source=SOURCE,
        spec_type="feature",
        submitter_class="maintainer",
        declared_at=f"sources.{SOURCE}.{AUTONOMY_FIELD}.maintainer.feature",
    )
    authority = resolve_authority(config, decision=decision, project=PROJECT, base_branch=BASE)
    poster = FeedbackPoster(
        state, config, audit, executor=StageExecutor(config, runner=feedback_runner)
    )

    def on_submitted(ctx: RunContext) -> None:
        poster.post(ref, source=SOURCE, run_id=RUN, event="delivery_submitted", context=ctx)

    return DeliveryPipeline(
        config,
        authority=authority,
        project=PROJECT,
        runner=delivery_runner,
        on_submitted=on_submitted if on_submitted_wired else None,
    )


class TestDeliverySubmittedFeedback:
    def test_a_passing_submit_posts_delivery_submitted(
        self, config: ConfigStore, state: StateStore, audit: AuditLog, ref: SpecRef, workspace: Path
    ) -> None:
        configure(config, workspace)
        feedback_runner = Runner()
        pipeline = build_pipeline(config, state, audit, ref, Runner(), feedback_runner)

        pipeline.deliver(context(workspace))

        assert feedback_runner.calls == [("gh", "comment", "42")]

    def test_it_is_recorded_in_the_audit_log(
        self, config: ConfigStore, state: StateStore, audit: AuditLog, ref: SpecRef, workspace: Path
    ) -> None:
        configure(config, workspace)
        pipeline = build_pipeline(config, state, audit, ref, Runner(), Runner())

        pipeline.deliver(context(workspace))

        feedback = [e for e in audit.read(ref) if e.event == AUDIT_ITEM_FEEDBACK]
        assert [(e.detail or {})["outcome"] for e in feedback] == [FeedbackOutcome.POSTED.value]
        assert (feedback[0].detail or {})["event"] == "delivery_submitted"

    def test_no_submit_stage_posts_nothing(
        self, config: ConfigStore, state: StateStore, audit: AuditLog, ref: SpecRef, workspace: Path
    ) -> None:
        """With no submit stage nothing is raised, so there is nothing to announce."""
        configure(config, workspace, with_submit=False)
        feedback_runner = Runner()
        pipeline = build_pipeline(config, state, audit, ref, Runner(), feedback_runner)

        pipeline.deliver(context(workspace))

        assert feedback_runner.calls == []

    def test_without_the_seam_wired_the_delivery_still_runs(
        self, config: ConfigStore, state: StateStore, audit: AuditLog, ref: SpecRef, workspace: Path
    ) -> None:
        configure(config, workspace)
        delivery_runner = Runner()
        pipeline = build_pipeline(
            config, state, audit, ref, delivery_runner, Runner(), on_submitted_wired=False
        )

        run = pipeline.deliver(context(workspace))

        assert run.ok
        assert "raise-review" in [call[0] for call in delivery_runner.calls]


ARTIFACT = "https://tracker.invalid/acme/pull/7"


class TestTheArtifactUrlTheSubmitStageRaised:
    """The link-artifact operation needs an address, and submit is what learns it.

    Requirement 36.1 names linking the review artifact, and before this the
    bundled presets referenced the delivery *branch* because no run-context
    variable carried a PR or MR URL. These pin that the pipeline reads the address
    out of what the submit command printed and carries it to both consumers -- the
    writeback that announces the submission, and the post-submit stages that check
    the artifact.
    """

    def test_the_writeback_receives_the_url_submit_printed(
        self, config: ConfigStore, state: StateStore, audit: AuditLog, ref: SpecRef, workspace: Path
    ) -> None:
        configure(
            config,
            workspace,
            feedback_argv=[["gh", "comment", "{item_id}", "--body", "{review_url}"]],
        )
        feedback_runner = Runner()
        pipeline = build_pipeline(
            config,
            state,
            audit,
            ref,
            Runner(stdout_for={"raise-review": f"created {ARTIFACT}\n"}),
            feedback_runner,
        )

        pipeline.deliver(context(workspace))

        assert feedback_runner.calls == [("gh", "comment", "42", "--body", ARTIFACT)]

    def test_a_post_submit_stage_can_check_the_artifact(
        self, config: ConfigStore, state: StateStore, audit: AuditLog, ref: SpecRef, workspace: Path
    ) -> None:
        """The URL reaches the stages after submit, not only the writeback.

        A post-submit gate is CI on the review artifact, so the address has to be
        on the context the rest of the flow runs with. A pipeline that handed it
        only to the ``on_submitted`` observer would refuse this verify stage for a
        variable with no value.
        """
        configure(
            config,
            workspace,
            verify_argv=[["run-checks", "--artifact", "{review_url}"]],
        )
        delivery_runner = Runner(stdout_for={"raise-review": f"created {ARTIFACT}\n"})
        pipeline = build_pipeline(config, state, audit, ref, delivery_runner, Runner())

        run = pipeline.deliver(context(workspace))

        assert run.ok, run.reason
        assert ("run-checks", "--artifact", ARTIFACT) in delivery_runner.calls

    def test_a_submit_that_printed_no_address_leaves_the_variable_absent(
        self, config: ConfigStore, state: StateStore, audit: AuditLog, ref: SpecRef, workspace: Path
    ) -> None:
        """No artifact means no link, so the variable has no value at all.

        Absent rather than blank: the writeback command referencing it refuses
        before spawning, which is a stated refusal naming the variable, instead of
        a comment posted with an empty string where a link belongs.
        """
        configure(
            config,
            workspace,
            feedback_argv=[["gh", "comment", "{item_id}", "--body", "{review_url}"]],
        )
        feedback_runner = Runner()
        pipeline = build_pipeline(config, state, audit, ref, Runner(), feedback_runner)

        pipeline.deliver(context(workspace))

        assert feedback_runner.calls == []
        feedback = [e for e in audit.read(ref) if e.event == AUDIT_ITEM_FEEDBACK]
        assert (feedback[0].detail or {})["outcome"] == FeedbackOutcome.FAILED.value

    def test_the_first_printed_address_is_the_artifact(
        self, config: ConfigStore, state: StateStore, audit: AuditLog, ref: SpecRef, workspace: Path
    ) -> None:
        """A submit command that prints more than one address names the artifact first."""
        configure(
            config,
            workspace,
            feedback_argv=[["gh", "comment", "{item_id}", "--body", "{review_url}"]],
        )
        feedback_runner = Runner()
        pipeline = build_pipeline(
            config,
            state,
            audit,
            ref,
            Runner(
                stdout_for={
                    "raise-review": f"{ARTIFACT}\nsee also https://docs.invalid/reviewing\n"
                }
            ),
            feedback_runner,
        )

        pipeline.deliver(context(workspace))

        assert feedback_runner.calls == [("gh", "comment", "42", "--body", ARTIFACT)]
