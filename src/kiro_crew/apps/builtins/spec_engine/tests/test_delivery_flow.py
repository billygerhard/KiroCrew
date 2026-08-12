"""The stage flow: isolation before execution, the verify loop, and the publish gate.

The claims under test are orderings rather than outcomes. Publish must not be
reached while verification is failing; isolation must happen for a run that will
deliver and not for one that will not; a failing verify stage must buy a bounded
number of fix rounds rather than either an immediate failure or an endless loop.

Commands are answered by a scripted runner instead of real processes. What
happens at the process boundary — a hostile value arriving as one inert
argument — is the stage executor's claim and is tested against real spawns
there; here the question is which commands the pipeline decides to run at all,
and a scripted runner records exactly that.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence

import pytest

from kiro_crew.apps.builtins.spec_engine.engine.autonomy import (
    AUTONOMY_FIELD,
    AutonomyDecision,
    AutonomyLevel,
)
from kiro_crew.apps.builtins.spec_engine.engine.config import DASHBOARD_SURFACE, ConfigStore
from kiro_crew.apps.builtins.spec_engine.engine.delivery import (
    DELIVERY_FLOW_STAGES,
    EVENT_FIX_DISPATCH,
    EVENT_INTEGRATION,
    EVENT_PUBLISHED,
    EVENT_STAGE,
    ISOLATE_STAGE,
    MAX_ADDRESS_CHARS,
    MAX_DEPLOYMENT_ADDRESSES,
    PUBLISH_STAGE,
    REASON_DELIVERY_FAILED,
    REASON_LADDER,
    REASON_POSTURE,
    REASON_VERIFY,
    SUBMIT_STAGE,
    VERIFY_STAGE,
    CommandOutcome,
    DeliveryOutcome,
    DeliveryPipeline,
    DeliveryRun,
    FixDispatch,
    RunContext,
    StageOutcome,
    StageResult,
    resolve_authority,
)

PROJECT = "acme"
SOURCE = "tracker"
BASE = "main"

ISOLATE_PROGRAM = "make-worktree"
SUBMIT_PROGRAM = "raise-review"
VERIFY_PROGRAM = "run-checks"
PUBLISH_PROGRAM = "deploy"


class ScriptedRunner:
    """A command runner that answers from a per-program script and records calls.

    An absent program answers success, so a test scripts only the outcome it
    cares about. Each program's script is consumed in order and its final entry
    repeats, which is what lets a verify command fail twice and then pass.
    """

    def __init__(
        self,
        *,
        exits: Mapping[str, Sequence[int]] | None = None,
        stdout: Mapping[str, str] | None = None,
        stderr: Mapping[str, str] | None = None,
    ) -> None:
        self._exits = {program: list(codes) for program, codes in (exits or {}).items()}
        self._stdout = dict(stdout or {})
        self._stderr = dict(stderr or {})
        self.calls: list[tuple[str, ...]] = []

    @property
    def programs(self) -> list[str]:
        """The program of every command run, in order."""
        return [argv[0] for argv in self.calls]

    def ran(self, program: str) -> int:
        return self.programs.count(program)

    def __call__(self, argv: Sequence[str], *, cwd: Path, timeout_s: int) -> CommandOutcome:
        self.calls.append(tuple(argv))
        program = argv[0]
        script = self._exits.get(program)
        if not script:
            code = 0
        else:
            code = script.pop(0) if len(script) > 1 else script[0]
        return CommandOutcome(
            exit_code=code,
            stdout=self._stdout.get(program, ""),
            stderr=self._stderr.get(program, ""),
        )


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    path = tmp_path / "workspace"
    path.mkdir()
    return path


@pytest.fixture()
def store(tmp_path: Path) -> ConfigStore:
    return ConfigStore(tmp_path / "state")


def context(workspace: Path, **overrides: str) -> RunContext:
    values: dict[str, str] = {
        "spec_name": "example",
        "spec_type": "feature",
        "workspace_path": str(workspace),
        "base_branch": BASE,
        "branch_name": "spec/example",
        "review_title": "Example",
    }
    values.update(overrides)
    return RunContext(**values)


def workflow_document(
    *,
    stages: dict[str, Any] | None = None,
    auto_integrate: bool | None = None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {"path": "/tmp/acme", "base_branch": BASE}
    entry["workflow"] = {
        "stages": (
            stages
            if stages is not None
            else {
                ISOLATE_STAGE: [[ISOLATE_PROGRAM, "{branch_name}"]],
                SUBMIT_STAGE: [[SUBMIT_PROGRAM, "--title", "{review_title}"]],
                VERIFY_STAGE: [[VERIFY_PROGRAM]],
                PUBLISH_STAGE: [[PUBLISH_PROGRAM, "--branch", "development"]],
            }
        )
    }
    if auto_integrate is not None:
        entry["delivery"] = {"auto_integrate": auto_integrate}
    return {"projects": {PROJECT: entry}}


def decision_at(level: AutonomyLevel) -> AutonomyDecision:
    return AutonomyDecision(
        level=level,
        source=SOURCE,
        spec_type="feature",
        submitter_class="maintainer",
        declared_at=f"sources.{SOURCE}.{AUTONOMY_FIELD}.maintainer.feature",
    )


def build_pipeline(
    store: ConfigStore,
    *,
    level: AutonomyLevel = AutonomyLevel.DELIVERY,
    runner: ScriptedRunner | None = None,
    fix_dispatcher: Any = None,
    audit: Any = None,
) -> DeliveryPipeline:
    authority = resolve_authority(
        store, decision=decision_at(level), project=PROJECT, base_branch=BASE
    )
    return DeliveryPipeline(
        store,
        authority=authority,
        project=PROJECT,
        runner=runner or ScriptedRunner(),
        fix_dispatcher=fix_dispatcher,
        audit=audit,
    )


def dispatcher(dispatched: bool = True) -> Any:
    """A fix-task dispatcher that records the rounds it was asked for."""
    rounds: list[int] = []

    def dispatch(*, attempt: int, stage: StageResult) -> FixDispatch:
        rounds.append(attempt)
        return FixDispatch(dispatched=dispatched, tasks=(f"fix-{attempt}",))

    dispatch.rounds = rounds  # type: ignore[attr-defined]
    return dispatch


def rounds_of(dispatch: Any) -> list[int]:
    return list(dispatch.rounds)


class TestIsolationBeforeExecution:
    def test_a_delivery_authorized_run_isolates(self, store: ConfigStore, workspace: Path) -> None:
        store.write(workflow_document(), surface=DASHBOARD_SURFACE)
        runner = ScriptedRunner()
        pipeline = build_pipeline(store, runner=runner)

        result = pipeline.isolate(context(workspace))

        assert result.outcome is StageOutcome.PASSED
        assert runner.programs == [ISOLATE_PROGRAM]

    def test_a_run_without_delivery_authority_works_in_the_project_tree(
        self, store: ConfigStore, workspace: Path
    ) -> None:
        store.write(workflow_document(), surface=DASHBOARD_SURFACE)
        runner = ScriptedRunner()
        pipeline = build_pipeline(store, level=AutonomyLevel.EXECUTION, runner=runner)

        result = pipeline.isolate(context(workspace))

        assert result.outcome is StageOutcome.SKIPPED
        assert "not authorized for delivery" in result.reason
        assert runner.calls == []

    def test_a_failing_isolate_stage_is_a_failure_not_a_skip(
        self, store: ConfigStore, workspace: Path
    ) -> None:
        store.write(workflow_document(), surface=DASHBOARD_SURFACE)
        runner = ScriptedRunner(exits={ISOLATE_PROGRAM: [1]})
        pipeline = build_pipeline(store, runner=runner)

        result = pipeline.isolate(context(workspace))

        assert result.outcome is StageOutcome.FAILED
        assert not result.ok

    def test_an_unconfigured_isolate_stage_skips_without_failing(
        self, store: ConfigStore, workspace: Path
    ) -> None:
        store.write(
            workflow_document(stages={VERIFY_STAGE: [[VERIFY_PROGRAM]]}),
            surface=DASHBOARD_SURFACE,
        )
        pipeline = build_pipeline(store)

        result = pipeline.isolate(context(workspace))

        assert result.outcome is StageOutcome.SKIPPED
        assert result.ok


class TestPublishWaitsOnVerification:
    def test_publish_does_not_run_while_a_verify_stage_has_failed(
        self, store: ConfigStore, workspace: Path
    ) -> None:
        # The ordering this module exists for. A publish command's exit code says
        # nothing about whether the change it published was ever checked.
        store.write(workflow_document(), surface=DASHBOARD_SURFACE)
        runner = ScriptedRunner(exits={VERIFY_PROGRAM: [1]})
        pipeline = build_pipeline(store, runner=runner)

        run = pipeline.deliver(context(workspace))

        assert run.outcome is DeliveryOutcome.FAILED
        assert runner.ran(PUBLISH_PROGRAM) == 0
        assert PUBLISH_STAGE in run.not_reached
        assert run.stage(PUBLISH_STAGE) is None
        assert not run.verified

    def test_publish_runs_once_verification_passes(
        self, store: ConfigStore, workspace: Path
    ) -> None:
        store.write(workflow_document(), surface=DASHBOARD_SURFACE)
        runner = ScriptedRunner()
        pipeline = build_pipeline(store, runner=runner)

        run = pipeline.deliver(context(workspace))

        assert run.outcome is DeliveryOutcome.PASSED
        assert runner.programs == [SUBMIT_PROGRAM, VERIFY_PROGRAM, PUBLISH_PROGRAM]
        assert run.verified

    def test_publish_waits_for_the_verification_that_follows_fix_rounds(
        self, store: ConfigStore, workspace: Path
    ) -> None:
        store.write(workflow_document(), surface=DASHBOARD_SURFACE)
        runner = ScriptedRunner(exits={VERIFY_PROGRAM: [1, 0]})
        pipeline = build_pipeline(store, runner=runner, fix_dispatcher=dispatcher())

        run = pipeline.deliver(context(workspace))

        assert run.outcome is DeliveryOutcome.PASSED
        assert runner.programs.index(PUBLISH_PROGRAM) > max(
            index for index, name in enumerate(runner.programs) if name == VERIFY_PROGRAM
        )
        assert run.verified

    def test_a_failing_submit_stage_stops_before_verification(
        self, store: ConfigStore, workspace: Path
    ) -> None:
        store.write(workflow_document(), surface=DASHBOARD_SURFACE)
        runner = ScriptedRunner(exits={SUBMIT_PROGRAM: [3]})
        pipeline = build_pipeline(store, runner=runner)

        run = pipeline.deliver(context(workspace))

        assert run.outcome is DeliveryOutcome.FAILED
        assert runner.programs == [SUBMIT_PROGRAM]
        assert run.not_reached == (VERIFY_STAGE, PUBLISH_STAGE)

    def test_a_workflow_with_no_verify_stage_publishes_without_a_check_having_run(
        self, store: ConfigStore, workspace: Path
    ) -> None:
        # Nothing was configured to check the change, so nothing blocks publish.
        # The combination that makes this dangerous — an armed auto-integration
        # with no verify stage — is warned about at configuration time instead.
        store.write(
            workflow_document(stages={PUBLISH_STAGE: [[PUBLISH_PROGRAM]]}),
            surface=DASHBOARD_SURFACE,
        )
        runner = ScriptedRunner()
        pipeline = build_pipeline(store, runner=runner)

        run = pipeline.deliver(context(workspace))

        assert run.outcome is DeliveryOutcome.PASSED
        assert runner.ran(PUBLISH_PROGRAM) == 1
        assert run.verified
        assert not run.verification_executed


class TestVerifyRetryLoop:
    def test_a_verify_failure_dispatches_fix_tasks(
        self, store: ConfigStore, workspace: Path
    ) -> None:
        store.write(workflow_document(), surface=DASHBOARD_SURFACE)
        dispatch = dispatcher()
        pipeline = build_pipeline(
            store,
            runner=ScriptedRunner(exits={VERIFY_PROGRAM: [1, 0]}),
            fix_dispatcher=dispatch,
        )

        run = pipeline.deliver(context(workspace))

        assert rounds_of(dispatch) == [0]
        assert run.verify_attempts[0].fix == FixDispatch(dispatched=True, tasks=("fix-0",))
        assert run.verify_attempts[-1].ok

    def test_fix_rounds_stop_at_the_configured_retry_limit(
        self, store: ConfigStore, workspace: Path
    ) -> None:
        store.write(
            {**workflow_document(), "limits": {"verify_retry_limit": 2}},
            surface=DASHBOARD_SURFACE,
        )
        runner = ScriptedRunner(exits={VERIFY_PROGRAM: [1]})
        dispatch = dispatcher()
        pipeline = build_pipeline(store, runner=runner, fix_dispatcher=dispatch)

        run = pipeline.deliver(context(workspace))

        assert run.outcome is DeliveryOutcome.FAILED
        assert rounds_of(dispatch) == [0, 1]
        assert runner.ran(VERIFY_PROGRAM) == 3
        assert runner.ran(PUBLISH_PROGRAM) == 0
        assert not run.verify_attempts[-1].fix.dispatched  # type: ignore[union-attr]
        assert "retry limit" in run.verify_attempts[-1].fix.reason  # type: ignore[union-attr]

    def test_a_zero_retry_limit_dispatches_no_fix_tasks(
        self, store: ConfigStore, workspace: Path
    ) -> None:
        store.write(
            {**workflow_document(), "limits": {"verify_retry_limit": 0}},
            surface=DASHBOARD_SURFACE,
        )
        runner = ScriptedRunner(exits={VERIFY_PROGRAM: [1]})
        dispatch = dispatcher()
        pipeline = build_pipeline(store, runner=runner, fix_dispatcher=dispatch)

        run = pipeline.deliver(context(workspace))

        assert run.outcome is DeliveryOutcome.FAILED
        assert rounds_of(dispatch) == []
        assert runner.ran(VERIFY_PROGRAM) == 1

    def test_a_dispatcher_that_creates_nothing_ends_the_loop(
        self, store: ConfigStore, workspace: Path
    ) -> None:
        # Verifying again after nothing was fixed spends a round of the limit to
        # reproduce the same failure.
        store.write(
            {**workflow_document(), "limits": {"verify_retry_limit": 3}},
            surface=DASHBOARD_SURFACE,
        )
        runner = ScriptedRunner(exits={VERIFY_PROGRAM: [1]})
        pipeline = build_pipeline(store, runner=runner, fix_dispatcher=dispatcher(dispatched=False))

        run = pipeline.deliver(context(workspace))

        assert run.outcome is DeliveryOutcome.FAILED
        assert runner.ran(VERIFY_PROGRAM) == 1

    def test_no_dispatcher_wired_fails_without_pretending_to_retry(
        self, store: ConfigStore, workspace: Path
    ) -> None:
        store.write(workflow_document(), surface=DASHBOARD_SURFACE)
        runner = ScriptedRunner(exits={VERIFY_PROGRAM: [1]})
        pipeline = build_pipeline(store, runner=runner)

        run = pipeline.deliver(context(workspace))

        assert run.outcome is DeliveryOutcome.FAILED
        assert runner.ran(VERIFY_PROGRAM) == 1
        assert "dispatcher" in run.verify_attempts[-1].fix.reason  # type: ignore[union-attr]


class TestPublishOutput:
    def test_deployment_addresses_are_surfaced_from_publish_output(
        self, store: ConfigStore, workspace: Path
    ) -> None:
        store.write(workflow_document(), surface=DASHBOARD_SURFACE)
        runner = ScriptedRunner(
            stdout={
                PUBLISH_PROGRAM: (
                    "uploading...\n" "deployed to https://example.test/preview/example.\n" "done\n"
                )
            },
            stderr={PUBLISH_PROGRAM: "logs at http://logs.example.test/run/7\n"},
        )
        pipeline = build_pipeline(store, runner=runner)

        run = pipeline.deliver(context(workspace))

        assert run.deployment_addresses == (
            "https://example.test/preview/example",
            "http://logs.example.test/run/7",
        )

    def test_publish_output_is_captured_on_the_stage_result(
        self, store: ConfigStore, workspace: Path
    ) -> None:
        store.write(workflow_document(), surface=DASHBOARD_SURFACE)
        runner = ScriptedRunner(stdout={PUBLISH_PROGRAM: "https://example.test/a\n"})
        pipeline = build_pipeline(store, runner=runner)

        run = pipeline.deliver(context(workspace))
        publish = run.stage(PUBLISH_STAGE)

        assert publish is not None
        assert publish.commands[0].stdout == "https://example.test/a\n"

    def test_addresses_from_a_failed_publish_are_still_surfaced(
        self, store: ConfigStore, workspace: Path
    ) -> None:
        # A publish that created a deployment and then failed still leaves an
        # address someone has to go look at.
        store.write(workflow_document(), surface=DASHBOARD_SURFACE)
        runner = ScriptedRunner(
            exits={PUBLISH_PROGRAM: [1]},
            stdout={PUBLISH_PROGRAM: "partially deployed to https://example.test/half\n"},
        )
        pipeline = build_pipeline(store, runner=runner)

        run = pipeline.deliver(context(workspace))

        assert run.outcome is DeliveryOutcome.FAILED
        assert run.deployment_addresses == ("https://example.test/half",)

    def test_repeated_addresses_are_reported_once(
        self, store: ConfigStore, workspace: Path
    ) -> None:
        store.write(workflow_document(), surface=DASHBOARD_SURFACE)
        runner = ScriptedRunner(
            stdout={PUBLISH_PROGRAM: "https://example.test/a\nhttps://example.test/a\n"}
        )
        pipeline = build_pipeline(store, runner=runner)

        assert pipeline.deliver(context(workspace)).deployment_addresses == (
            "https://example.test/a",
        )

    def test_the_address_list_is_bounded(self, store: ConfigStore, workspace: Path) -> None:
        store.write(workflow_document(), surface=DASHBOARD_SURFACE)
        flood = "".join(f"https://example.test/{index}\n" for index in range(200))
        pipeline = build_pipeline(store, runner=ScriptedRunner(stdout={PUBLISH_PROGRAM: flood}))

        addresses = pipeline.deliver(context(workspace)).deployment_addresses

        assert len(addresses) == MAX_DEPLOYMENT_ADDRESSES

    def test_an_overlong_address_is_dropped_rather_than_carried(
        self, store: ConfigStore, workspace: Path
    ) -> None:
        """The bound is on length as well as count.

        Publish output is a provider's stdout, so its shape is the provider's
        choice. One absurd address would otherwise ride into the notification,
        the queue entry, and the audit record, all of which a human reads.
        """
        store.write(workflow_document(), surface=DASHBOARD_SURFACE)
        absurd = "https://example.test/" + "p" * (MAX_ADDRESS_CHARS + 1)
        keep = "https://example.test/real"
        pipeline = build_pipeline(
            store, runner=ScriptedRunner(stdout={PUBLISH_PROGRAM: f"{absurd}\n{keep}\n"})
        )

        addresses = pipeline.deliver(context(workspace)).deployment_addresses

        assert addresses == (keep,)

    def test_output_without_an_address_reports_none(
        self, store: ConfigStore, workspace: Path
    ) -> None:
        store.write(workflow_document(), surface=DASHBOARD_SURFACE)
        pipeline = build_pipeline(
            store,
            runner=ScriptedRunner(stdout={PUBLISH_PROGRAM: "deployed. see the console.\n"}),
        )

        assert pipeline.deliver(context(workspace)).deployment_addresses == ()


class TestIntegrationInTheFlow:
    def test_integration_requires_human_action_by_default(
        self, store: ConfigStore, workspace: Path
    ) -> None:
        store.write(workflow_document(), surface=DASHBOARD_SURFACE)
        pipeline = build_pipeline(store, level=AutonomyLevel.INTEGRATION)

        run = pipeline.deliver(context(workspace))

        assert run.integration is not None
        assert run.integration.requires_human_action
        assert run.integration.reasons == (REASON_POSTURE,)

    def test_integration_needs_both_the_ladder_rung_and_the_switch(
        self, store: ConfigStore, workspace: Path
    ) -> None:
        store.write(workflow_document(auto_integrate=True), surface=DASHBOARD_SURFACE)

        without_rung = build_pipeline(store, level=AutonomyLevel.DELIVERY).deliver(
            context(workspace)
        )
        with_both = build_pipeline(store, level=AutonomyLevel.INTEGRATION).deliver(
            context(workspace)
        )

        assert without_rung.integration is not None
        assert without_rung.integration.reasons == (REASON_LADDER,)
        assert with_both.integration is not None
        assert with_both.integration.permitted

    def test_a_failed_verification_blocks_integration_even_with_both_gates_open(
        self, store: ConfigStore, workspace: Path
    ) -> None:
        store.write(
            {
                **workflow_document(auto_integrate=True),
                "limits": {"verify_retry_limit": 0},
            },
            surface=DASHBOARD_SURFACE,
        )
        pipeline = build_pipeline(
            store,
            level=AutonomyLevel.INTEGRATION,
            runner=ScriptedRunner(exits={VERIFY_PROGRAM: [1]}),
        )

        run = pipeline.deliver(context(workspace))

        assert run.integration is not None
        assert not run.integration.permitted
        assert REASON_VERIFY in run.integration.reasons

    def test_a_publish_that_deployed_and_then_failed_does_not_permit_integration(
        self, store: ConfigStore, workspace: Path
    ) -> None:
        # The dangerous shape: verification passed, both configuration gates are
        # open, and a protected target is named, so every configured gate holds
        # while the run itself broke partway through publishing. A caller reading
        # only `permitted` would integrate a half-deployed change.
        store.write(workflow_document(auto_integrate=True), surface=DASHBOARD_SURFACE)
        pipeline = build_pipeline(
            store,
            level=AutonomyLevel.INTEGRATION,
            runner=ScriptedRunner(exits={PUBLISH_PROGRAM: [1]}),
        )

        run = pipeline.deliver(context(workspace))

        assert run.outcome is DeliveryOutcome.FAILED
        assert run.verified
        assert run.integration is not None
        assert run.integration.ladder_permits
        assert run.integration.auto_integrate
        assert not run.integration.delivered
        assert not run.integration.permitted
        assert run.integration.reasons == (REASON_DELIVERY_FAILED,)

    def test_the_publish_target_outside_the_protected_set_is_not_an_integration(
        self, store: ConfigStore, workspace: Path
    ) -> None:
        store.write(workflow_document(), surface=DASHBOARD_SURFACE)
        pipeline = build_pipeline(store)

        run = pipeline.deliver(context(workspace))
        decision = pipeline.authority.integration(verified=run.verified, target="development")

        assert run.outcome is DeliveryOutcome.PASSED
        assert not decision.target_protected

    def test_a_protected_target_is_recorded_as_protected(
        self, store: ConfigStore, workspace: Path
    ) -> None:
        """The direction nothing asserted, on the stage the module calls irreversible.

        Protection gates nothing on its own -- a blank target is refused by its own
        check -- so the classification exists to be *recorded*: it is what says
        afterwards whether the branch a change landed on was one other people build
        on. Only the negative direction was ever asserted, and a constant False
        satisfies that, so every protected merge could be recorded as unprotected
        with the whole set resolution reduced to a boolean nobody read.
        """
        store.write(workflow_document(auto_integrate=True), surface=DASHBOARD_SURFACE)
        recorded: list[tuple[str, dict[str, Any]]] = []
        pipeline = build_pipeline(
            store,
            level=AutonomyLevel.INTEGRATION,
            audit=lambda event, detail: recorded.append((event, detail)),
        )

        run = pipeline.deliver(context(workspace))

        assert run.integration is not None
        # The run's base branch is the protected set's fallback, so this target is
        # exactly the case that fallback exists for.
        assert run.integration.target_protected is True
        details = [detail for event, detail in recorded if event == EVENT_INTEGRATION]
        assert details, "the integration decision was never audited"
        assert details[-1]["target_protected"] is True


class TestRefusedDelivery:
    def test_a_run_without_delivery_authority_runs_no_stage(
        self, store: ConfigStore, workspace: Path
    ) -> None:
        store.write(workflow_document(), surface=DASHBOARD_SURFACE)
        runner = ScriptedRunner()
        pipeline = build_pipeline(store, level=AutonomyLevel.EXECUTION, runner=runner)

        run = pipeline.deliver(context(workspace))

        assert run.outcome is DeliveryOutcome.REFUSED
        assert runner.calls == []
        assert run.not_reached == DELIVERY_FLOW_STAGES
        assert run.integration is None

    def test_a_zero_config_project_can_never_reach_integration(
        self, store: ConfigStore, workspace: Path
    ) -> None:
        # Nothing configured, a policy grid naming integration, and the posture
        # switch on: the workflow ceiling still holds.
        store.write({"delivery": {"auto_integrate": True}}, surface=DASHBOARD_SURFACE)
        runner = ScriptedRunner()
        pipeline = build_pipeline(store, level=AutonomyLevel.INTEGRATION, runner=runner)

        run = pipeline.deliver(context(workspace))

        assert pipeline.authority.level is AutonomyLevel.EXECUTION
        assert run.outcome is DeliveryOutcome.REFUSED
        assert runner.calls == []
        assert not pipeline.authority.integration(verified=True, target=BASE).permitted


class TestAuditRecording:
    def test_every_stage_and_the_integration_decision_are_recorded(
        self, store: ConfigStore, workspace: Path
    ) -> None:
        store.write(workflow_document(), surface=DASHBOARD_SURFACE)
        recorded: list[tuple[str, dict[str, Any]]] = []
        pipeline = build_pipeline(
            store,
            runner=ScriptedRunner(stdout={PUBLISH_PROGRAM: "https://example.test/a\n"}),
            audit=lambda event, detail: recorded.append((event, detail)),
        )

        pipeline.deliver(context(workspace))

        events = [event for event, _ in recorded]
        assert events.count(EVENT_STAGE) == 3
        assert EVENT_PUBLISHED in events
        assert events[-1] == EVENT_INTEGRATION
        published = next(detail for event, detail in recorded if event == EVENT_PUBLISHED)
        assert published["addresses"] == ["https://example.test/a"]

    def test_a_fix_dispatch_is_recorded_with_its_round(
        self, store: ConfigStore, workspace: Path
    ) -> None:
        store.write(workflow_document(), surface=DASHBOARD_SURFACE)
        recorded: list[tuple[str, dict[str, Any]]] = []
        pipeline = build_pipeline(
            store,
            runner=ScriptedRunner(exits={VERIFY_PROGRAM: [1, 0]}),
            fix_dispatcher=dispatcher(),
            audit=lambda event, detail: recorded.append((event, detail)),
        )

        pipeline.deliver(context(workspace))

        dispatches = [detail for event, detail in recorded if event == EVENT_FIX_DISPATCH]
        assert dispatches == [
            {
                "stage": VERIFY_STAGE,
                "attempt": 0,
                "dispatched": True,
                "tasks": ["fix-0"],
                "reason": "",
            }
        ]

    def test_a_stage_record_names_variables_rather_than_their_values(
        self, store: ConfigStore, workspace: Path
    ) -> None:
        # A review title carries text from a public tracker; the audit record
        # says which variables a stage used, not what was in them.
        store.write(workflow_document(), surface=DASHBOARD_SURFACE)
        recorded: list[tuple[str, dict[str, Any]]] = []
        pipeline = build_pipeline(
            store, audit=lambda event, detail: recorded.append((event, detail))
        )

        pipeline.deliver(context(workspace, review_title="rm -rf /"))

        submit = next(
            detail
            for event, detail in recorded
            if event == EVENT_STAGE and detail["stage"] == SUBMIT_STAGE
        )
        assert submit["variables_used"] == ["review_title"]
        assert "rm -rf /" not in repr(submit)


class TestRunReporting:
    def test_executed_stages_exclude_the_ones_that_skipped(
        self, store: ConfigStore, workspace: Path
    ) -> None:
        store.write(
            workflow_document(stages={VERIFY_STAGE: [[VERIFY_PROGRAM]]}),
            surface=DASHBOARD_SURFACE,
        )
        run = build_pipeline(store).deliver(context(workspace))

        assert [result.stage for result in run.executed_stages()] == [VERIFY_STAGE]
        assert run.ok

    def test_an_empty_run_reports_no_verification(self) -> None:
        run = DeliveryRun(outcome=DeliveryOutcome.REFUSED)

        assert not run.verified
        assert not run.verification_executed
