"""The stage flow: what runs, in what order, and what stops the next thing.

The executor in :mod:`.stages` runs one stage. This module decides which stages
run at all, and it is where the ordering guarantees live.

**Isolate happens before execution, not before delivery.** A delivery-authorized
run will push branches, raise a review artifact, and possibly deploy. Doing that
from the project's own working tree means a second run's edits are in the first
run's commit, so the workspace has to exist before the first task is implemented —
hours before the first delivery command runs. A run that is not
delivery-authorized skips isolation and works in the project tree, which is what
the IDE does and the only thing a zero-configuration project can do.

**Publish runs only after every configured verify stage has passed.** This is the
ordering the module exists for. Publishing a half-verified change is not a
slightly worse outcome than publishing a verified one: it puts an unchecked change
somewhere other people or systems consume, and the exit code of the publish
command says nothing about that. So verification is a precondition evaluated
before publish is reached, not a check the publish command is trusted to make.

**A failing verify stage buys fix rounds, not an immediate failure.** A blocking
verify failure dispatches fix tasks and verification runs again, up to the
configured retry limit, and only then is the delivery failed. The retry limit is
finite by configuration, because a fix loop with no ceiling is an unattended run
spending credits on a change nothing can repair.

**Publish output is captured and its deployment addresses surfaced.** A publish
command that deploys somewhere prints where. That address is the one piece of a
delivery a human actually needs, and leaving it inside captured output means
reading a log to find out where the work went. Addresses are extracted from the
output as data, deduplicated, and carried on the result for the notification, the
queue entry, and the audit record to share one list.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Protocol, Sequence

from ..autonomy import AutonomyLevel
from ..config import ConfigStore, ConfigValidationError
from .integration import DeliveryAuthority, IntegrationDecision
from .isolation import WorkspaceBroker, isolated_context
from .stages import CommandRunner, StageExecutor, StageOutcome, StageResult
from .variables import RunContext
from .workflow import ISOLATE_STAGE, DeliveryWorkflow

logger = logging.getLogger(__name__)

#: Stage that raises the review artifact.
SUBMIT_STAGE = "submit"

#: Stage whose commands check the change. Publish waits on every one of them.
VERIFY_STAGE = "verify"

#: Stage that puts the change somewhere it is consumed.
PUBLISH_STAGE = "publish"

#: The order the pipeline runs stages in after isolation. Teardown is not here:
#: it runs at archive, not at the end of a delivery.
DELIVERY_FLOW_STAGES: tuple[str, ...] = (SUBMIT_STAGE, VERIFY_STAGE, PUBLISH_STAGE)

#: Setting bounding how many fix rounds a failing verify stage earns.
VERIFY_RETRY_LIMIT_SETTING = "limits.verify_retry_limit"

#: Addresses are extracted from output written by a program the engine does not
#: control, so the count and length are both bounded.
MAX_DEPLOYMENT_ADDRESSES = 20
MAX_ADDRESS_CHARS = 2048

#: An http(s) address. Deliberately narrow: a deployment address is shown to a
#: human and stored, and widening the pattern to every scheme would turn any
#: colon in a build log into something the app presents as a place to visit.
_ADDRESS_PATTERN = re.compile(r"https?://[^\s<>\"'`)\]}]+")

#: Trailing punctuation that belongs to the sentence, not to the address.
_ADDRESS_TRAILING = ".,;:!?'\")]}>"


class DeliveryOutcome(str, Enum):
    """How a delivery ended."""

    #: Every stage that ran passed, or skipped because it was unconfigured.
    PASSED = "passed"
    #: A stage failed, or verification never passed within its retry limit.
    FAILED = "failed"
    #: Nothing ran. The run is not authorized for delivery, or the workflow
    #: could not be used. Distinct from FAILED: no side effect left the host.
    REFUSED = "refused"


@dataclass(frozen=True)
class FixDispatch:
    """The result of asking for fix tasks after a verify failure."""

    dispatched: bool
    #: Task identifiers the dispatcher created, when it names them.
    tasks: tuple[str, ...] = ()
    #: Why nothing was dispatched, when nothing was.
    reason: str = ""


class FixTaskDispatcher(Protocol):
    """Dispatches fix tasks for a failing verify stage.

    The orchestrator owns fix-task creation; the pipeline owns when to ask and
    how many times. Keeping the two apart is what lets the retry ceiling be
    tested without a model in the loop.
    """

    def __call__(self, *, attempt: int, stage: StageResult) -> FixDispatch: ...


#: Receives one audit-shaped event: a name and a detail object. The pipeline does
#: not own the audit log's spec identity, so recording is a callable the driver
#: supplies rather than a log this module opens.
AuditRecorder = Callable[[str, dict[str, Any]], None]

#: Audit event names.
EVENT_STAGE = "delivery.stage"
EVENT_FIX_DISPATCH = "delivery.fix_dispatch"
EVENT_PUBLISHED = "delivery.published"
EVENT_INTEGRATION = "delivery.integration"


@dataclass(frozen=True)
class VerifyAttempt:
    """One verification round: its result, and the fix dispatch it triggered."""

    attempt: int
    stage: StageResult
    fix: FixDispatch | None = None

    @property
    def ok(self) -> bool:
        return self.stage.ok


@dataclass(frozen=True)
class DeliveryRun:
    """Everything one pass through the pipeline did."""

    outcome: DeliveryOutcome
    stages: tuple[StageResult, ...] = ()
    verify_attempts: tuple[VerifyAttempt, ...] = ()
    deployment_addresses: tuple[str, ...] = ()
    integration: IntegrationDecision | None = None
    reason: str = ""
    #: Stages the flow never reached, so a reader can tell "passed" from
    #: "not run" without inferring it from an absent entry.
    not_reached: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return self.outcome is DeliveryOutcome.PASSED

    @property
    def verified(self) -> bool:
        """Whether every configured verify stage passed.

        A workflow with no verify stage satisfies this vacuously, and that is
        deliberate rather than an oversight: publishing and integrating are not
        blocked on a check nobody configured. The danger in that combination is
        real, and it is answered where it is created — a configuration advisory
        when unattended integration is armed with nothing verifying it — rather
        than by a refusal hours later that an unattended run cannot act on.
        Use :attr:`verification_executed` to tell a passing check from an absent
        one.
        """
        attempts = self.verify_attempts
        # The last round is the answer: an earlier failure that fix tasks
        # repaired is history, and a run that ends unverified ends with a
        # failing round because the loop stops there.
        return bool(attempts) and attempts[-1].ok

    @property
    def verification_executed(self) -> bool:
        """Whether a verify command actually ran, rather than the stage skipping."""
        return any(attempt.stage.executed for attempt in self.verify_attempts)

    def stage(self, name: str) -> StageResult | None:
        """The last result recorded for *name*, or ``None`` when it never ran."""
        for result in reversed(self.stages):
            if result.stage == name:
                return result
        return None

    def executed_stages(self) -> tuple[StageResult, ...]:
        """Stages that actually spawned a command."""
        return tuple(result for result in self.stages if result.executed)


class DeliveryPipeline:
    """Runs a run's delivery stages in order, enforcing the flow's preconditions."""

    def __init__(
        self,
        store: ConfigStore,
        *,
        authority: DeliveryAuthority,
        project: str | None = None,
        workflow: DeliveryWorkflow | None = None,
        runner: CommandRunner | None = None,
        executor: StageExecutor | None = None,
        fix_dispatcher: FixTaskDispatcher | None = None,
        audit: AuditRecorder | None = None,
        isolation: WorkspaceBroker | None = None,
    ) -> None:
        self._store = store
        self._project = project
        self._authority = authority
        self._executor = executor or StageExecutor(
            store, project=project, workflow=workflow, runner=runner
        )
        self._fix_dispatcher = fix_dispatcher
        self._audit = audit
        self._isolation = isolation

    @property
    def workflow(self) -> DeliveryWorkflow:
        return self._executor.workflow

    @property
    def authority(self) -> DeliveryAuthority:
        return self._authority

    # --- isolation ---------------------------------------------------------

    def isolate(self, context: RunContext, *, run_id: str = "") -> StageResult:
        """Run the isolate stage, before any task executes.

        Called by the run driver ahead of execution rather than at delivery time.
        A run that is not delivery-authorized gets a skip with the reason on it,
        because "no isolated workspace" is the correct outcome there and a caller
        must be able to tell it apart from a failed isolation.

        With a workspace broker wired, the run's own working tree is claimed
        before the stage spawns anything and released again if nothing was
        created. The claim is what makes a second run asking for the same tree a
        refusal instead of two runs editing one checkout; the broker's module
        explains why that has to happen before the command rather than after it.
        """
        if not self._authority.isolates_before_execution:
            result = StageResult(
                stage=ISOLATE_STAGE,
                outcome=StageOutcome.SKIPPED,
                reason=(
                    "this run is not authorized for delivery, so it works in the project's "
                    "own tree rather than an isolated workspace"
                ),
            )
            self._record_stage(result)
            return result
        if self._isolation is None or not self._isolates():
            # No broker, or a workflow with nothing to materialize a workspace:
            # claiming a path here would hold it against later runs for a tree
            # that never appears.
            result = self._executor.run(ISOLATE_STAGE, context)
            self._record_stage(result)
            return result
        claim = self._isolation.claim(run_id=run_id, context=context)
        if not claim.granted:
            result = StageResult(
                stage=ISOLATE_STAGE,
                outcome=StageOutcome.REFUSED,
                reason=claim.reason,
            )
            self._record_stage(result)
            return result
        result = self._executor.run(ISOLATE_STAGE, isolated_context(context, claim))
        if not result.executed or not result.ok:
            self._isolation.release(claim)
        self._record_stage(result)
        return result

    def _isolates(self) -> bool:
        """Whether the workflow has isolate commands, tolerating a bad one."""
        try:
            return self.workflow.isolates
        except ConfigValidationError:
            # An unusable isolate declaration is the executor's refusal to
            # report, with the configuration path on it. Claiming first would
            # add a released row to the ledger and change nothing else.
            return False

    # --- the flow ----------------------------------------------------------

    def deliver(self, context: RunContext) -> DeliveryRun:
        """Run submit, verification, and publish for one run.

        Returns rather than raises for every stage-level problem, so a caller
        reports what ran alongside what did not instead of losing the earlier
        stages to an exception from a later one.
        """
        if not self._authority.permits(AutonomyLevel.DELIVERY):
            return DeliveryRun(
                outcome=DeliveryOutcome.REFUSED,
                reason=(
                    "the autonomy policy does not authorize delivery for this run"
                    + (
                        "; the project has no configured delivery workflow"
                        if not self._authority.workflow_configured
                        else ""
                    )
                ),
                not_reached=DELIVERY_FLOW_STAGES,
            )

        stages: list[StageResult] = []
        attempts: list[VerifyAttempt] = []
        remaining = list(DELIVERY_FLOW_STAGES)

        submit = self._run_stage(SUBMIT_STAGE, context)
        stages.append(submit)
        remaining.remove(SUBMIT_STAGE)
        if not submit.ok:
            return self._failed(context, stages, attempts, remaining, submit)

        attempts.extend(self._verify(context))
        stages.extend(attempt.stage for attempt in attempts)
        remaining.remove(VERIFY_STAGE)
        if attempts and not attempts[-1].ok:
            # Publish is not reached. A change that did not verify must not land
            # anywhere it is consumed, whatever the publish command would report.
            return self._failed(context, stages, attempts, remaining, attempts[-1].stage)

        publish = self._run_stage(PUBLISH_STAGE, context)
        stages.append(publish)
        remaining.remove(PUBLISH_STAGE)
        addresses = _deployment_addresses(publish)
        if addresses:
            self._record(EVENT_PUBLISHED, {"addresses": list(addresses)})
        if not publish.ok:
            return self._failed(context, stages, attempts, remaining, publish, addresses=addresses)

        run = DeliveryRun(
            outcome=DeliveryOutcome.PASSED,
            stages=tuple(stages),
            verify_attempts=tuple(attempts),
            deployment_addresses=addresses,
        )
        return self._with_integration(run, context)

    # --- verification ------------------------------------------------------

    def _verify(self, context: RunContext) -> tuple[VerifyAttempt, ...]:
        """Verify, dispatching fix tasks and verifying again up to the limit."""
        limit = int(self._store.effective(VERIFY_RETRY_LIMIT_SETTING, project=self._project).value)
        attempts: list[VerifyAttempt] = []
        attempt = 0
        while True:
            result = self._run_stage(VERIFY_STAGE, context)
            if result.ok:
                attempts.append(VerifyAttempt(attempt=attempt, stage=result))
                return tuple(attempts)
            if attempt >= limit:
                attempts.append(
                    VerifyAttempt(
                        attempt=attempt,
                        stage=result,
                        fix=FixDispatch(
                            dispatched=False,
                            reason=f"the verify retry limit of {limit} is exhausted",
                        ),
                    )
                )
                return tuple(attempts)
            dispatch = self._dispatch_fixes(attempt=attempt, stage=result)
            attempts.append(VerifyAttempt(attempt=attempt, stage=result, fix=dispatch))
            if not dispatch.dispatched:
                # Nothing was fixed, so verifying again would produce the same
                # failure while spending another round of the limit.
                return tuple(attempts)
            attempt += 1

    def _dispatch_fixes(self, *, attempt: int, stage: StageResult) -> FixDispatch:
        if self._fix_dispatcher is None:
            return FixDispatch(
                dispatched=False,
                reason="no fix-task dispatcher is wired to this pipeline",
            )
        dispatch = self._fix_dispatcher(attempt=attempt, stage=stage)
        self._record(
            EVENT_FIX_DISPATCH,
            {
                "stage": stage.stage,
                "attempt": attempt,
                "dispatched": dispatch.dispatched,
                "tasks": list(dispatch.tasks),
                "reason": dispatch.reason,
            },
        )
        return dispatch

    # --- results -----------------------------------------------------------

    def _with_integration(self, run: DeliveryRun, context: RunContext) -> DeliveryRun:
        """Attach the integration decision for this run's target."""
        decision = self._authority.integration(verified=run.verified, target=context.base_branch)
        self._record(
            EVENT_INTEGRATION,
            {
                "permitted": decision.permitted,
                "target": decision.target,
                "target_protected": decision.target_protected,
                "ladder_permits": decision.ladder_permits,
                "auto_integrate": decision.auto_integrate,
                "verified": decision.verified,
                "reasons": list(decision.reasons),
            },
        )
        if not decision.permitted:
            logger.info(
                "integration for spec %r is reserved for human action (%s)",
                context.spec_name,
                ", ".join(decision.reasons),
            )
        return DeliveryRun(
            outcome=run.outcome,
            stages=run.stages,
            verify_attempts=run.verify_attempts,
            deployment_addresses=run.deployment_addresses,
            integration=decision,
            reason=run.reason,
            not_reached=run.not_reached,
        )

    def _failed(
        self,
        context: RunContext,
        stages: Sequence[StageResult],
        attempts: Sequence[VerifyAttempt],
        remaining: Sequence[str],
        cause: StageResult,
        *,
        addresses: tuple[str, ...] = (),
    ) -> DeliveryRun:
        run = DeliveryRun(
            outcome=DeliveryOutcome.FAILED,
            stages=tuple(stages),
            verify_attempts=tuple(attempts),
            deployment_addresses=addresses,
            reason=cause.reason or f"the {cause.stage} stage {cause.outcome.value}",
            not_reached=tuple(remaining),
        )
        # The integration decision is attached to a failed delivery too. A run
        # that failed verification is exactly the run whose record must say that
        # integration was not authorized, rather than leaving the question open.
        return self._with_integration(run, context)

    def _run_stage(self, stage: str, context: RunContext) -> StageResult:
        result = self._executor.run(stage, context)
        self._record_stage(result)
        return result

    def _record_stage(self, result: StageResult) -> None:
        self._record(
            EVENT_STAGE,
            {
                "stage": result.stage,
                "outcome": result.outcome.value,
                "reason": result.reason,
                "declared_at": result.declared_at,
                "variables_used": list(result.variables_used),
                "missing_variables": list(result.missing_variables),
                "commands": [
                    {
                        "program": command.argv[0],
                        "outcome": command.outcome.value,
                        "exit_code": command.exit_code,
                        "duration_s": round(command.duration_s, 3),
                    }
                    for command in result.commands
                ],
            },
        )

    def _record(self, event: str, detail: dict[str, Any]) -> None:
        if self._audit is None:
            return
        self._audit(event, detail)


def _deployment_addresses(publish: StageResult) -> tuple[str, ...]:
    """Extract deployment addresses from a publish stage's captured output.

    Both streams are scanned: plenty of deployment tools print the address they
    created to stderr alongside their progress, and an address a human needs is
    not less useful for having arrived on the other stream.
    """
    found: dict[str, None] = {}
    for command in publish.commands:
        for stream in (command.stdout, command.stderr):
            for match in _ADDRESS_PATTERN.finditer(stream or ""):
                address = match.group(0).rstrip(_ADDRESS_TRAILING)
                if not address or len(address) > MAX_ADDRESS_CHARS:
                    continue
                found.setdefault(address, None)
                if len(found) >= MAX_DEPLOYMENT_ADDRESSES:
                    return tuple(found)
    return tuple(found)
