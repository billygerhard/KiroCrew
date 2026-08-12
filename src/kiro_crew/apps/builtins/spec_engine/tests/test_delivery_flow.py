"""The stage flow: isolation before execution, the verify loop, and the publish gate.

The claims under test are orderings rather than outcomes. Publish must not be
reached while verification is failing; isolation must happen for a run that will
deliver and not for one that will not; a failing verify stage must buy a bounded
number of fix rounds rather than either an immediate failure or an endless loop.
A quality gate adds a second axis to the same question: which side of submit it
runs on, and what its failure costs.

Commands are answered by a scripted runner instead of real processes. What
happens at the process boundary — a hostile value arriving as one inert
argument — is the stage executor's claim and is tested against real spawns
there; here the question is which commands the pipeline decides to run at all,
and a scripted runner records exactly that.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Sequence

import pytest

from kiro_crew.apps.builtins.spec_engine.engine.autonomy import (
    AUTONOMY_FIELD,
    AutonomyDecision,
    AutonomyLevel,
)
from kiro_crew.apps.builtins.spec_engine.engine.config import DASHBOARD_SURFACE, ConfigStore
from kiro_crew.apps.builtins.spec_engine.engine.config.schema import (
    GATE_POSITION_BOTH,
    GATE_POSITION_POST_SUBMIT,
    GATE_POSITION_PRE_SUBMIT,
    GATE_SEVERITY_ADVISORY,
    GATE_SEVERITY_BLOCKING,
    SECTION_QUALITY_GATES,
)
from kiro_crew.apps.builtins.spec_engine.engine.delivery import (
    DELIVERY_FLOW_STAGES,
    EVENT_FIX_DISPATCH,
    EVENT_GATE,
    EVENT_GATES,
    EVENT_INTEGRATION,
    EVENT_PUBLISHED,
    EVENT_STAGE,
    ISOLATE_STAGE,
    MAX_ADDRESS_CHARS,
    MAX_DEPLOYMENT_ADDRESSES,
    MAX_GATE_OUTPUT_CHARS,
    NO_GATES_REASON,
    PUBLISH_STAGE,
    QUALITY_GATE_PRESETS,
    REASON_DELIVERY_FAILED,
    REASON_LADDER,
    REASON_POSTURE,
    REASON_VERIFY,
    SUBMIT_STAGE,
    TRUNCATION_NOTICE,
    VERIFY_STAGE,
    CommandOutcome,
    DeliveryOutcome,
    DeliveryPipeline,
    DeliveryRun,
    FixDispatch,
    RunContext,
    StageOutcome,
    StageResult,
    gate_presets,
    load_quality_gates,
    resolve_authority,
)

PROJECT = "acme"
SOURCE = "tracker"
BASE = "main"

ISOLATE_PROGRAM = "make-worktree"
SUBMIT_PROGRAM = "raise-review"
VERIFY_PROGRAM = "run-checks"
PUBLISH_PROGRAM = "deploy"

LINT_PROGRAM = "run-lint"
COVERAGE_PROGRAM = "run-coverage"
CI_PROGRAM = "run-ci"


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


def gate(
    name: str,
    program: str,
    *,
    position: str = GATE_POSITION_PRE_SUBMIT,
    severity: str = GATE_SEVERITY_BLOCKING,
    arguments: Sequence[str] = (),
) -> dict[str, Any]:
    """One quality gate declaration, as a configuration surface would write it."""
    return {
        "name": name,
        "position": position,
        "severity": severity,
        "commands": [[program, *arguments]],
    }


def configure(
    store: ConfigStore,
    *gates: Mapping[str, Any],
    stages: dict[str, Any] | None = None,
    auto_integrate: bool | None = None,
    limits: dict[str, Any] | None = None,
) -> None:
    """Persist a workflow plus *gates* through the validated write path."""
    document: dict[str, Any] = dict(workflow_document(stages=stages, auto_integrate=auto_integrate))
    if gates:
        document[SECTION_QUALITY_GATES] = [dict(entry) for entry in gates]
    if limits is not None:
        document["limits"] = limits
    store.write(document, surface=DASHBOARD_SURFACE)


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

    def test_each_verification_point_gets_its_own_retry_budget(
        self, store: ConfigStore, workspace: Path
    ) -> None:
        """The limit is per verification point, which doubles the worst case.

        A pre-submit gate and a post-submit check gate different things -- the
        review artifact and the publish -- so a run that spent its rounds fixing
        analyzers still needs rounds for the CI that runs on the artifact. The
        consequence is that the unattended worst case is two budgets, not one,
        and nothing distinguished the two semantics: making the budget shared
        across the delivery left every test passing.
        """
        configure(
            store,
            gate("lint", LINT_PROGRAM),
            limits={"verify_retry_limit": 1},
        )
        # The gate fails then recovers, so the pre-submit point spends one round.
        # The post-submit check then fails, and must still get a full round of its
        # own rather than finding the budget already spent.
        runner = ScriptedRunner(exits={LINT_PROGRAM: [1, 0], VERIFY_PROGRAM: [1, 0]})
        dispatch = dispatcher()
        pipeline = build_pipeline(store, runner=runner, fix_dispatcher=dispatch)

        run = pipeline.deliver(context(workspace))

        assert run.outcome is DeliveryOutcome.PASSED, run.reason
        # One round at each point, not one shared between them.
        assert rounds_of(dispatch) == [0, 0]

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


class TestGatePositions:
    """Which side of submit a gate runs on is declared, not fixed.

    The pipeline has one verify stage in its stage list, so a gate list that
    always ran at the same point would satisfy every "the gate ran" assertion
    while making the declaration decorative. These tests pin the position against
    the submit command, which is the boundary the position is named after.
    """

    def test_a_pre_submit_gate_runs_before_the_review_artifact_is_raised(
        self, store: ConfigStore, workspace: Path
    ) -> None:
        configure(store, gate("lint", LINT_PROGRAM))
        runner = ScriptedRunner()
        pipeline = build_pipeline(store, runner=runner)

        run = pipeline.deliver(context(workspace))

        assert run.outcome is DeliveryOutcome.PASSED
        assert runner.programs == [LINT_PROGRAM, SUBMIT_PROGRAM, VERIFY_PROGRAM, PUBLISH_PROGRAM]
        assert [entry.position for entry in run.gate("lint")] == [GATE_POSITION_PRE_SUBMIT]

    def test_a_post_submit_gate_runs_on_the_raised_artifact(
        self, store: ConfigStore, workspace: Path
    ) -> None:
        configure(store, gate("ci", CI_PROGRAM, position=GATE_POSITION_POST_SUBMIT))
        runner = ScriptedRunner()
        pipeline = build_pipeline(store, runner=runner)

        run = pipeline.deliver(context(workspace))

        assert run.outcome is DeliveryOutcome.PASSED
        assert runner.programs == [SUBMIT_PROGRAM, VERIFY_PROGRAM, CI_PROGRAM, PUBLISH_PROGRAM]
        assert [entry.position for entry in run.gate("ci")] == [GATE_POSITION_POST_SUBMIT]

    def test_one_gate_declared_at_both_positions_runs_on_both_sides_of_submit(
        self, store: ConfigStore, workspace: Path
    ) -> None:
        """The case a two-valued position cannot express.

        An analyzer worth running before a human sees the change is usually worth
        re-running on the artifact CI built. Declaring that as two gates would put
        one check in the record under two names with two severities to keep in
        step, so the position itself carries it — and a pipeline that resolved
        ``both`` to either single side would still pass every one-sided test.
        """
        configure(store, gate("checks", LINT_PROGRAM, position=GATE_POSITION_BOTH))
        runner = ScriptedRunner()
        pipeline = build_pipeline(store, runner=runner)

        run = pipeline.deliver(context(workspace))

        assert runner.programs == [
            LINT_PROGRAM,
            SUBMIT_PROGRAM,
            VERIFY_PROGRAM,
            LINT_PROGRAM,
            PUBLISH_PROGRAM,
        ]
        assert [entry.position for entry in run.gate("checks")] == [
            GATE_POSITION_PRE_SUBMIT,
            GATE_POSITION_POST_SUBMIT,
        ]

    def test_gates_at_one_position_run_in_declared_order(
        self, store: ConfigStore, workspace: Path
    ) -> None:
        configure(store, gate("lint", LINT_PROGRAM), gate("coverage", COVERAGE_PROGRAM))
        runner = ScriptedRunner()

        build_pipeline(store, runner=runner).deliver(context(workspace))

        assert runner.programs.index(LINT_PROGRAM) < runner.programs.index(COVERAGE_PROGRAM)

    def test_a_workflow_declaring_its_verify_work_only_as_gates_still_verified(
        self, store: ConfigStore, workspace: Path
    ) -> None:
        # No verify stage at all: the gates are the verification, and a run that
        # published after them has not published something unchecked.
        configure(
            store,
            gate("lint", LINT_PROGRAM),
            stages={
                SUBMIT_STAGE: [[SUBMIT_PROGRAM, "--title", "{review_title}"]],
                PUBLISH_STAGE: [[PUBLISH_PROGRAM]],
            },
        )
        run = build_pipeline(store, runner=ScriptedRunner()).deliver(context(workspace))

        assert run.outcome is DeliveryOutcome.PASSED
        assert run.verified
        assert run.verification_executed


class TestGateSeverity:
    """Blocking and advisory failures must cost different things.

    A gate that stopped the flow on every failure passes any blocking-only test,
    and one that stopped on nothing passes any advisory-only test, so each of
    these asserts what the *other* severity does in the same delivery.
    """

    def test_a_blocking_failure_stops_the_flow_and_dispatches_fix_tasks(
        self, store: ConfigStore, workspace: Path
    ) -> None:
        configure(store, gate("lint", LINT_PROGRAM), limits={"verify_retry_limit": 1})
        runner = ScriptedRunner(exits={LINT_PROGRAM: [1]})
        dispatch = dispatcher()
        pipeline = build_pipeline(store, runner=runner, fix_dispatcher=dispatch)

        run = pipeline.deliver(context(workspace))

        assert run.outcome is DeliveryOutcome.FAILED
        assert runner.ran(SUBMIT_PROGRAM) == 0
        assert run.not_reached == DELIVERY_FLOW_STAGES
        assert rounds_of(dispatch) == [0]
        assert [entry.gate for entry in run.blocking_failures()] == ["lint", "lint"]

    def test_an_advisory_failure_is_recorded_and_surfaced_without_stopping(
        self, store: ConfigStore, workspace: Path
    ) -> None:
        configure(store, gate("coverage", COVERAGE_PROGRAM, severity=GATE_SEVERITY_ADVISORY))
        runner = ScriptedRunner(exits={COVERAGE_PROGRAM: [1]})
        dispatch = dispatcher()
        pipeline = build_pipeline(store, runner=runner, fix_dispatcher=dispatch)

        run = pipeline.deliver(context(workspace))

        assert run.outcome is DeliveryOutcome.PASSED
        assert runner.programs == [
            COVERAGE_PROGRAM,
            SUBMIT_PROGRAM,
            VERIFY_PROGRAM,
            PUBLISH_PROGRAM,
        ]
        assert rounds_of(dispatch) == []
        surfaced = run.advisory_failures()
        assert [entry.gate for entry in surfaced] == ["coverage"]
        assert surfaced[0].exit_status == 1
        assert run.blocking_failures() == ()

    def test_an_advisory_and_a_blocking_failure_in_one_round_do_different_things(
        self, store: ConfigStore, workspace: Path
    ) -> None:
        """Both directions of the severity axis, in one delivery.

        The advisory gate fails first and the blocking gate after it still runs,
        so the advisory failure stopped nothing; the flow never reaches submit, so
        the blocking failure stopped everything. Collapsing the two severities in
        either direction changes exactly one of those two assertions.
        """
        configure(
            store,
            gate("coverage", COVERAGE_PROGRAM, severity=GATE_SEVERITY_ADVISORY),
            gate("lint", LINT_PROGRAM),
        )
        runner = ScriptedRunner(exits={COVERAGE_PROGRAM: [1], LINT_PROGRAM: [2]})
        pipeline = build_pipeline(store, runner=runner)

        run = pipeline.deliver(context(workspace))

        assert runner.programs == [COVERAGE_PROGRAM, LINT_PROGRAM]
        assert run.outcome is DeliveryOutcome.FAILED
        assert [entry.gate for entry in run.advisory_failures()] == ["coverage"]
        assert [entry.gate for entry in run.blocking_failures()] == ["lint"]

    def test_the_whole_round_runs_so_one_fix_dispatch_answers_every_finding(
        self, store: ConfigStore, workspace: Path
    ) -> None:
        # A blocking failure stops the flow, not its siblings: stopping the round
        # would make each finding spend one round of a bounded limit to reveal
        # the next.
        configure(store, gate("lint", LINT_PROGRAM), gate("coverage", COVERAGE_PROGRAM))
        runner = ScriptedRunner(exits={LINT_PROGRAM: [1], COVERAGE_PROGRAM: [1]})
        pipeline = build_pipeline(store, runner=runner, fix_dispatcher=dispatcher(False))

        run = pipeline.deliver(context(workspace))

        assert runner.programs == [LINT_PROGRAM, COVERAGE_PROGRAM]
        assert len(run.blocking_failures()) == 2

    def test_blocking_gate_rounds_stop_at_the_configured_retry_limit(
        self, store: ConfigStore, workspace: Path
    ) -> None:
        configure(store, gate("lint", LINT_PROGRAM), limits={"verify_retry_limit": 2})
        runner = ScriptedRunner(exits={LINT_PROGRAM: [1]})
        dispatch = dispatcher()
        pipeline = build_pipeline(store, runner=runner, fix_dispatcher=dispatch)

        run = pipeline.deliver(context(workspace))

        assert run.outcome is DeliveryOutcome.FAILED
        assert runner.ran(LINT_PROGRAM) == 3
        assert rounds_of(dispatch) == [0, 1]
        assert [round_.attempt for round_ in run.gate_rounds] == [0, 1, 2]
        assert "retry limit" in run.gate_rounds[-1].fix.reason  # type: ignore[union-attr]

    def test_a_gate_that_passes_after_a_fix_round_lets_the_flow_continue(
        self, store: ConfigStore, workspace: Path
    ) -> None:
        configure(store, gate("lint", LINT_PROGRAM))
        runner = ScriptedRunner(exits={LINT_PROGRAM: [1, 0]})
        pipeline = build_pipeline(store, runner=runner, fix_dispatcher=dispatcher())

        run = pipeline.deliver(context(workspace))

        assert run.outcome is DeliveryOutcome.PASSED
        assert runner.ran(LINT_PROGRAM) == 2
        assert runner.programs.index(SUBMIT_PROGRAM) > max(
            index for index, name in enumerate(runner.programs) if name == LINT_PROGRAM
        )

    def test_a_failing_post_submit_blocking_gate_keeps_publish_out_of_reach(
        self, store: ConfigStore, workspace: Path
    ) -> None:
        configure(
            store,
            gate("ci", CI_PROGRAM, position=GATE_POSITION_POST_SUBMIT),
            limits={"verify_retry_limit": 0},
        )
        runner = ScriptedRunner(exits={CI_PROGRAM: [1]})
        pipeline = build_pipeline(store, runner=runner)

        run = pipeline.deliver(context(workspace))

        assert run.outcome is DeliveryOutcome.FAILED
        assert runner.ran(PUBLISH_PROGRAM) == 0
        assert PUBLISH_STAGE in run.not_reached
        assert not run.verified

    def test_a_failing_advisory_gate_does_not_withhold_verification(
        self, store: ConfigStore, workspace: Path
    ) -> None:
        # The verify stage passed and only an advisory gate failed, so the change
        # is verified and integration is not held back by a ratio.
        configure(
            store,
            gate(
                "coverage",
                COVERAGE_PROGRAM,
                position=GATE_POSITION_POST_SUBMIT,
                severity=GATE_SEVERITY_ADVISORY,
            ),
        )
        runner = ScriptedRunner(exits={COVERAGE_PROGRAM: [1]})

        run = build_pipeline(store, runner=runner).deliver(context(workspace))

        assert run.outcome is DeliveryOutcome.PASSED
        assert run.verified
        assert run.integration is not None
        assert REASON_VERIFY not in run.integration.reasons


class TestGateVariables:
    def test_a_gate_is_substituted_the_run_context_including_its_base_branch(
        self, store: ConfigStore, workspace: Path
    ) -> None:
        # The whole point of the base branch reaching a gate: a coverage delta or
        # a changed-files lint compares the change against what it started from.
        configure(
            store,
            gate("coverage", COVERAGE_PROGRAM, arguments=["--against", "{base_branch}"]),
        )
        runner = ScriptedRunner()

        build_pipeline(store, runner=runner).deliver(context(workspace))

        assert (COVERAGE_PROGRAM, "--against", BASE) in runner.calls

    def test_a_gate_referencing_a_valueless_variable_refuses_before_executing(
        self, store: ConfigStore, workspace: Path
    ) -> None:
        # Fail closed: an empty substitution would leave a gate command that runs,
        # exits zero, and checked something other than what was configured.
        configure(store, gate("lint", LINT_PROGRAM, arguments=["--item", "{item_url}"]))
        runner = ScriptedRunner()
        pipeline = build_pipeline(store, runner=runner)

        run = pipeline.deliver(context(workspace, item_url=""))

        assert runner.calls == []
        assert run.outcome is DeliveryOutcome.FAILED
        refused = run.gate("lint")[0]
        assert refused.result.outcome is StageOutcome.REFUSED
        assert refused.result.missing_variables == ("item_url",)
        assert refused.exit_status is None

    def test_a_refused_gate_asks_for_no_fixes_at_all(
        self, store: ConfigStore, workspace: Path
    ) -> None:
        """A refusal is about the configuration, so no fix task can change it.

        Nothing ran, so the refusal says nothing about the code and the same
        configuration refuses identically on every remaining round. Spending the
        retry budget on model-backed fix dispatches would burn real credits on an
        unattended path to rediscover a config error the first round already
        named.
        """
        configure(store, gate("lint", LINT_PROGRAM, arguments=["--item", "{item_url}"]))
        runner = ScriptedRunner()
        dispatch = dispatcher()
        pipeline = build_pipeline(store, runner=runner, fix_dispatcher=dispatch)

        run = pipeline.deliver(context(workspace, item_url=""))

        assert run.outcome is DeliveryOutcome.FAILED
        assert runner.calls == []
        rounds = run.gate("lint")
        assert len(rounds) == 1, "a refusal must not be retried"
        assert dispatch.rounds == []


class TestGateRecording:
    def test_each_gate_execution_records_name_severity_exit_status_and_output(
        self, store: ConfigStore, workspace: Path
    ) -> None:
        configure(store, gate("coverage", COVERAGE_PROGRAM, severity=GATE_SEVERITY_ADVISORY))
        recorded: list[tuple[str, dict[str, Any]]] = []
        pipeline = build_pipeline(
            store,
            runner=ScriptedRunner(
                exits={COVERAGE_PROGRAM: [3]},
                stdout={COVERAGE_PROGRAM: "coverage 71% (was 74%)\n"},
                stderr={COVERAGE_PROGRAM: "below the threshold\n"},
            ),
            audit=lambda event, detail: recorded.append((event, detail)),
        )

        run = pipeline.deliver(context(workspace))

        gates = [detail for event, detail in recorded if event == EVENT_GATE]
        assert len(gates) == 1
        assert gates[0]["gate"] == "coverage"
        assert gates[0]["severity"] == GATE_SEVERITY_ADVISORY
        assert gates[0]["position"] == GATE_POSITION_PRE_SUBMIT
        assert gates[0]["exit_status"] == 3
        assert gates[0]["blocked"] is False
        assert "coverage 71%" in gates[0]["output"]
        assert "below the threshold" in gates[0]["output"]
        # The same text the audit record holds is on the run, for a driver to
        # display without reading the log back.
        assert run.gate("coverage")[0].output == gates[0]["output"]

    def test_gate_output_is_bounded_before_it_is_recorded(
        self, store: ConfigStore, workspace: Path
    ) -> None:
        """Gate output is written by a program the engine does not control.

        It flows into the notification, the queue entry, and the audit record, so
        the bound is what keeps a chatty analyzer from deciding how large those
        get.
        """
        configure(store, gate("lint", LINT_PROGRAM, severity=GATE_SEVERITY_ADVISORY))
        flood = "x" * (MAX_GATE_OUTPUT_CHARS * 3)
        pipeline = build_pipeline(
            store, runner=ScriptedRunner(exits={LINT_PROGRAM: [1]}, stdout={LINT_PROGRAM: flood})
        )

        run = pipeline.deliver(context(workspace))
        output = run.gate("lint")[0].output

        assert len(output) < len(flood)
        assert output.startswith("x" * MAX_GATE_OUTPUT_CHARS)
        assert output.endswith(TRUNCATION_NOTICE)

    def test_the_configured_gates_are_recorded_with_the_side_they_run_on(
        self, store: ConfigStore, workspace: Path
    ) -> None:
        configure(
            store,
            gate("lint", LINT_PROGRAM),
            gate("ci", CI_PROGRAM, position=GATE_POSITION_POST_SUBMIT),
            gate("checks", COVERAGE_PROGRAM, position=GATE_POSITION_BOTH),
        )
        recorded: list[tuple[str, dict[str, Any]]] = []
        pipeline = build_pipeline(
            store, audit=lambda event, detail: recorded.append((event, detail))
        )

        run = pipeline.deliver(context(workspace))

        configuration = next(detail for event, detail in recorded if event == EVENT_GATES)
        assert configuration["configured"] == ["lint", "ci", "checks"]
        assert configuration["pre_submit"] == ["lint", "checks"]
        assert configuration["post_submit"] == ["ci", "checks"]
        assert run.declared_gates == ("lint", "ci", "checks")

    def test_no_gates_configured_is_recorded_rather_than_silent(
        self, store: ConfigStore, workspace: Path
    ) -> None:
        """ "No gate ran" and "no gate was configured" must not read alike.

        Recording nothing would leave the two indistinguishable afterwards, which
        is exactly the question asked when someone wants to know what checked a
        change.
        """
        configure(store)
        recorded: list[tuple[str, dict[str, Any]]] = []
        pipeline = build_pipeline(
            store, audit=lambda event, detail: recorded.append((event, detail))
        )

        run = pipeline.deliver(context(workspace))

        configuration = next(detail for event, detail in recorded if event == EVENT_GATES)
        assert configuration["configured"] == []
        assert configuration["reason"] == NO_GATES_REASON
        assert run.outcome is DeliveryOutcome.PASSED
        assert not run.gates_configured
        assert run.gate_runs() == ()
        assert EVENT_GATE not in [event for event, _ in recorded]

    def test_a_gate_that_cannot_be_read_refuses_instead_of_resolving_to_none(
        self, store: ConfigStore, workspace: Path
    ) -> None:
        """A document edited around the write path must not disable the gates.

        Resolving an unparseable gate list to "no gates configured" would make
        one bad character the way to turn every check off while the delivery still
        reports success.
        """
        configure(store, gate("lint", LINT_PROGRAM))
        document = json.loads(store.path.read_text(encoding="utf-8"))
        document[SECTION_QUALITY_GATES][0]["severity"] = "whenever"
        store.path.write_text(json.dumps(document), encoding="utf-8")
        recorded: list[tuple[str, dict[str, Any]]] = []
        runner = ScriptedRunner()
        pipeline = build_pipeline(
            store, runner=runner, audit=lambda event, detail: recorded.append((event, detail))
        )

        run = pipeline.deliver(context(workspace))

        assert run.outcome is DeliveryOutcome.REFUSED
        assert runner.calls == []
        assert f"{SECTION_QUALITY_GATES}[0].severity" in run.reason
        configuration = next(detail for event, detail in recorded if event == EVENT_GATES)
        assert configuration["error"]


class TestGateConfiguration:
    def test_the_bundled_presets_cover_tests_coverage_lint_and_type_checks(self) -> None:
        assert set(QUALITY_GATE_PRESETS) == {"tests", "coverage", "lint", "types"}

    def test_a_preset_is_valid_configuration_and_loads_back_as_a_gate(
        self, store: ConfigStore
    ) -> None:
        # Editable means it goes through the ordinary write path and comes back
        # as an ordinary gate, not that it is a special kind of binding.
        store.write(
            {SECTION_QUALITY_GATES: gate_presets()},
            surface=DASHBOARD_SURFACE,
        )

        loaded = load_quality_gates(store.document())

        assert [entry.name for entry in loaded] == ["tests", "coverage", "lint", "types"]
        severities = {entry.name: entry.severity for entry in loaded}
        assert severities["tests"] == GATE_SEVERITY_BLOCKING
        # A coverage threshold dips on an honest refactor, so it reports rather
        # than abandoning finished work.
        assert severities["coverage"] == GATE_SEVERITY_ADVISORY

    def test_the_coverage_preset_compares_against_the_run_base_branch(self) -> None:
        coverage = gate_presets("coverage")[0]
        arguments = [argument for argv in coverage["commands"] for argument in argv]

        assert any("{base_branch}" in argument for argument in arguments)

    def test_editing_a_preset_does_not_change_what_the_next_project_is_offered(self) -> None:
        first = gate_presets("tests")[0]
        first["severity"] = GATE_SEVERITY_ADVISORY
        first["commands"][0].append("--only-fast")

        second = gate_presets("tests")[0]

        assert second["severity"] == GATE_SEVERITY_BLOCKING
        assert second["commands"] == [["make", "test"]]

    def test_an_unknown_preset_name_is_refused(self) -> None:
        with pytest.raises(KeyError):
            gate_presets("no-such-preset")

    def test_gates_are_read_in_declaration_order(self, store: ConfigStore) -> None:
        store.write(
            {
                SECTION_QUALITY_GATES: [
                    gate("second", COVERAGE_PROGRAM),
                    gate("first", LINT_PROGRAM),
                ]
            },
            surface=DASHBOARD_SURFACE,
        )

        assert [entry.name for entry in load_quality_gates(store.document())] == [
            "second",
            "first",
        ]

    def test_a_document_with_no_gate_section_reads_as_none(self) -> None:
        assert load_quality_gates({}) == ()
