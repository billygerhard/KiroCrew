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

**Quality gates are verify commands with a declared severity and position.**
A gate is not a second mechanism: it is a verify-stage command list that the
workflow's stage map did not declare, executed through the same executor under
the same rules — whole list validated before the first spawn, same variable set
substituted, a valueless reference refusing without running anything. Two
declarations shape the flow:

* **Position** decides which side of submit the gate runs on. An analyzer worth
  running before a human sees the change belongs before submit; CI on the review
  artifact belongs after it; a gate may name ``both``, because a position is a
  property of the moment rather than of the check, and splitting one check into
  two named gates would put it in the audit record twice with two severities to
  keep in step.
* **Severity** decides what a failure costs. A blocking failure stops the flow
  and dispatches fix tasks; an advisory failure is recorded and surfaced and the
  run continues. The distinction is the point: a gate that stopped everything
  would make a coverage ratio able to abandon finished work, and a gate that
  stopped nothing would let an analyzer failure reach a human as a review.

Within one round every gate runs, including the ones after a blocking failure,
so a single fix dispatch answers every finding rather than each failure buying
its own round of a bounded limit. The two positions carry their own fix loops
because they gate different things — the review artifact and the publish — and a
run that spent its rounds on pre-submit analyzers still needs rounds for the CI
that runs on the artifact afterwards.

**No gates configured is an answer, and it is recorded.** A project that
configured none proceeds, and the record says none ran. Silence would be
indistinguishable from gates that ran and passed, which is the one reading that
matters when someone asks afterwards what checked a change.

**Gate output is recorded, and it is untrusted.** The name, severity, exit
status, and captured output of every gate reach the audit record and the run, so
a failure is diagnosable without re-running anything. That output is written by
a program the engine does not control, so what is recorded is bounded before it
is stored anywhere a human reads it.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Callable, Mapping, Protocol, Sequence, TypeVar

from ..autonomy import AutonomyLevel
from ..config import ConfigError, ConfigStore, ConfigValidationError, ValueOrigin
from ..config.schema import (
    GATE_POSITION_BOTH,
    GATE_POSITION_POST_SUBMIT,
    GATE_POSITION_PRE_SUBMIT,
    GATE_POSITIONS,
    GATE_SEVERITIES,
    GATE_SEVERITY_ADVISORY,
    GATE_SEVERITY_BLOCKING,
    SECTION_QUALITY_GATES,
)
from .integration import DeliveryAuthority, IntegrationDecision
from .isolation import WorkspaceBroker, isolated_context
from .stages import (
    TRUNCATION_NOTICE,
    CommandRunner,
    StageExecutor,
    StageOutcome,
    StageResult,
)
from .templates import CommandTemplate, TemplateError
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

#: Characters of a gate's captured output kept on the run and in the audit
#: record. Smaller than the executor's capture cap on purpose: this text is read
#: by a human and stored per gate per round, and it is written by a program the
#: engine does not control.
MAX_GATE_OUTPUT_CHARS = 4 * 1024

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

#: One per gate execution: which gate, at which position, in which round, how it
#: ended, and what it printed.
EVENT_GATE = "delivery.gate"

#: One per delivery, naming the gates configured for it. Recorded even when none
#: are, because "no gate ran" and "no gate was configured" are the same sentence
#: only if somebody wrote it down.
EVENT_GATES = "delivery.gates"

#: Reason recorded with :data:`EVENT_GATES` when a project configured no gates.
NO_GATES_REASON = "no quality gates are configured, so the flow ran without them"


@dataclass(frozen=True)
class QualityGate:
    """One configured quality gate: a verify-class command list with a severity.

    ``position`` may name both sides of submit, so :meth:`runs_at` rather than an
    equality test is how a caller decides whether this gate belongs at a point in
    the flow.
    """

    name: str
    severity: str
    position: str
    commands: tuple[CommandTemplate, ...]
    origin: ValueOrigin = ValueOrigin.APP_CONFIG
    #: Dotted configuration path of the declaration, for reporting.
    declared_at: str = ""

    @property
    def blocking(self) -> bool:
        """Whether a failure of this gate stops the flow."""
        return self.severity == GATE_SEVERITY_BLOCKING

    @property
    def variables(self) -> tuple[str, ...]:
        """Every variable referenced across this gate's commands."""
        seen: dict[str, None] = {}
        for command in self.commands:
            for name in command.variables:
                seen.setdefault(name, None)
        return tuple(seen)

    def runs_at(self, position: str) -> bool:
        """Whether this gate runs at *position*."""
        if position not in (GATE_POSITION_PRE_SUBMIT, GATE_POSITION_POST_SUBMIT):
            raise ValueError(f"not a point in the flow: {position!r}")
        return self.position in (position, GATE_POSITION_BOTH)


#: Bundled gate presets, keyed by the name they are configured under.
#:
#: A preset is a starting point copied into configuration and edited there, not
#: a live binding: the project owns the commands after that, which is what makes
#: the bundled set safe to keep small. ``make`` is the entry point almost every
#: project already has, and a project whose checks are spelled differently edits
#: one list rather than inventing a workflow.
#:
#: The severities are the defaults, and they differ for a reason. A failing test,
#: a lint error, and a type error are each a defect with a fix, so they block and
#: earn fix rounds before a human is asked to look. A coverage threshold is a
#: ratio that legitimately dips on a refactor, so it is advisory: blocking it
#: would let a percentage abandon finished work.
QUALITY_GATE_PRESETS: Mapping[str, Mapping[str, Any]] = {
    "tests": {
        "name": "tests",
        "position": GATE_POSITION_PRE_SUBMIT,
        "severity": GATE_SEVERITY_BLOCKING,
        "commands": [["make", "test"]],
    },
    "coverage": {
        "name": "coverage",
        "position": GATE_POSITION_PRE_SUBMIT,
        "severity": GATE_SEVERITY_ADVISORY,
        # The base branch is passed through so the threshold can be a delta
        # against what the change started from rather than a project-wide floor.
        "commands": [["make", "coverage", "BASE={base_branch}"]],
    },
    "lint": {
        "name": "lint",
        "position": GATE_POSITION_PRE_SUBMIT,
        "severity": GATE_SEVERITY_BLOCKING,
        "commands": [["make", "lint"]],
    },
    "types": {
        "name": "types",
        "position": GATE_POSITION_PRE_SUBMIT,
        "severity": GATE_SEVERITY_BLOCKING,
        "commands": [["make", "typecheck"]],
    },
}


def gate_presets(*names: str) -> list[dict[str, Any]]:
    """Return preset gate declarations, ready to write into configuration.

    Deep copies: a configuration surface offers a preset for editing, and an
    edit that reached back into the bundled table would change what every later
    project is offered. Naming no preset returns all of them, in declaration
    order.
    """
    wanted = names or tuple(QUALITY_GATE_PRESETS)
    presets: list[dict[str, Any]] = []
    for name in wanted:
        preset = QUALITY_GATE_PRESETS.get(name)
        if preset is None:
            raise KeyError(f"unknown quality gate preset: {name!r}")
        presets.append(
            {
                "name": preset["name"],
                "position": preset["position"],
                "severity": preset["severity"],
                "commands": [list(argv) for argv in preset["commands"]],
            }
        )
    return presets


def load_quality_gates(document: Mapping[str, Any]) -> tuple[QualityGate, ...]:
    """Read the configured quality gates, in declaration order.

    Gates are app-level rather than per-project: they are the checks this
    installation insists on, while a project's own workflow stages say how its
    change is submitted and published. Declaration order is preserved because it
    is the order the gates run in at their position.

    Raises ``ConfigValidationError`` naming the configuration path for a
    declaration that cannot be read. The write path validates the same shape, so
    reaching this means the document was edited around it, and a gate list that
    cannot be parsed must not resolve to "no gates" — deleting one character
    would then disable every check.
    """
    node = document.get(SECTION_QUALITY_GATES)
    if node is None:
        return ()
    if isinstance(node, (str, bytes)) or not isinstance(node, Sequence):
        raise ConfigValidationError(
            [ConfigError(SECTION_QUALITY_GATES, "expected a list of gates")]
        )
    gates: list[QualityGate] = []
    seen: set[str] = set()
    errors: list[ConfigError] = []
    for index, entry in enumerate(node):
        path = f"{SECTION_QUALITY_GATES}[{index}]"
        if not isinstance(entry, Mapping):
            errors.append(ConfigError(path, "expected an object"))
            continue
        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            errors.append(ConfigError(f"{path}.name", "expected a non-empty string"))
            continue
        if name in seen:
            errors.append(ConfigError(f"{path}.name", "duplicate gate name"))
            continue
        seen.add(name)
        position = entry.get("position")
        if position not in GATE_POSITIONS:
            errors.append(
                ConfigError(f"{path}.position", "expected one of: " + _listed(GATE_POSITIONS))
            )
            continue
        severity = entry.get("severity")
        if severity not in GATE_SEVERITIES:
            errors.append(
                ConfigError(f"{path}.severity", "expected one of: " + _listed(GATE_SEVERITIES))
            )
            continue
        commands = entry.get("commands")
        if isinstance(commands, (str, bytes)) or not isinstance(commands, Sequence) or not commands:
            errors.append(ConfigError(f"{path}.commands", "expected at least one command"))
            continue
        parsed: list[CommandTemplate] = []
        for argv_index, argv in enumerate(commands):
            try:
                parsed.append(CommandTemplate.parse(argv))
            except TemplateError as exc:
                errors.append(ConfigError(f"{path}.commands[{argv_index}]", str(exc)))
        if len(parsed) != len(commands):
            continue
        gates.append(
            QualityGate(
                name=name,
                severity=str(severity),
                position=str(position),
                commands=tuple(parsed),
                origin=ValueOrigin.APP_CONFIG,
                declared_at=path,
            )
        )
    if errors:
        raise ConfigValidationError(errors)
    return tuple(gates)


def _listed(values: Sequence[str]) -> str:
    return ", ".join(values)


@dataclass(frozen=True)
class GateRun:
    """One execution of one quality gate.

    Carries the gate's identity alongside its result because a gate declared at
    both positions produces two of these in one delivery, and "which gate failed"
    is unanswerable from a stage result: every gate runs as verify-stage commands,
    so they all report the same stage name.
    """

    gate: str
    severity: str
    #: The point in the flow this execution ran at. Never ``both``: that is a
    #: declaration, and this is one of the two runs it produced.
    position: str
    attempt: int
    result: StageResult
    #: The gate's captured output, bounded. Recorded rather than left in the
    #: command results so the audit record and the run display share one text.
    output: str = ""

    @property
    def ok(self) -> bool:
        return self.result.ok

    @property
    def blocking(self) -> bool:
        return self.severity == GATE_SEVERITY_BLOCKING

    @property
    def blocked(self) -> bool:
        """Whether this gate's failure stops the flow."""
        return self.blocking and not self.result.ok

    @property
    def advisory_failure(self) -> bool:
        """Whether this gate failed without stopping anything."""
        return not self.blocking and not self.result.ok

    @property
    def exit_status(self) -> int | None:
        """The exit status that decided this gate.

        The first non-zero exit when a command failed, since that is the one the
        gate ended on; the last exit when every command passed; ``None`` when
        nothing exited at all, which is a gate that timed out or was refused
        before it spawned.
        """
        for command in self.result.commands:
            if not command.ok:
                return command.exit_code
        if self.result.commands:
            return self.result.commands[-1].exit_code
        return None

    @property
    def reason(self) -> str:
        """Why this gate ended as it did, for a record a human reads."""
        if self.result.reason:
            return self.result.reason
        return f"the {self.gate!r} gate {self.result.outcome.value}"


@dataclass(frozen=True)
class GateRound:
    """One round of the gates at one position, and the fix dispatch it triggered."""

    position: str
    attempt: int
    gates: tuple[GateRun, ...] = ()
    fix: FixDispatch | None = None

    @property
    def blocking_failures(self) -> tuple[StageResult, ...]:
        return tuple(run.result for run in self.gates if run.blocked)

    @property
    def advisory_failures(self) -> tuple[GateRun, ...]:
        return tuple(run for run in self.gates if run.advisory_failure)

    @property
    def ok(self) -> bool:
        """Whether the flow may continue past this round.

        Advisory failures are deliberately absent from this answer: they are
        recorded and surfaced, and a run they stopped would be a run a coverage
        ratio could abandon.
        """
        return not self.blocking_failures

    @property
    def executed(self) -> bool:
        return any(run.result.executed for run in self.gates)


@dataclass(frozen=True)
class VerifyAttempt:
    """One verification round: its results, and the fix dispatch it triggered.

    A round is the workflow's own verify stage plus every gate declared at this
    position, because they answer one question — is this change checked — and a
    fix dispatch that saw only half of the failures would ask for half a fix.
    """

    attempt: int
    stage: StageResult
    fix: FixDispatch | None = None
    position: str = GATE_POSITION_POST_SUBMIT
    gates: tuple[GateRun, ...] = ()

    @property
    def blocking_failures(self) -> tuple[StageResult, ...]:
        """Every result in this round that stops the flow, the stage first."""
        stage_failure = () if self.stage.ok else (self.stage,)
        return stage_failure + tuple(run.result for run in self.gates if run.blocked)

    @property
    def advisory_failures(self) -> tuple[GateRun, ...]:
        return tuple(run for run in self.gates if run.advisory_failure)

    @property
    def ok(self) -> bool:
        return not self.blocking_failures

    @property
    def executed(self) -> bool:
        """Whether anything in this round actually spawned a command."""
        return self.stage.executed or any(run.result.executed for run in self.gates)


#: A verification round: the workflow's verify stage with its post-submit gates,
#: or a round of the pre-submit gates. Both shapes answer whether the flow may
#: continue and which results stopped it, so one bounded fix loop drives them.
_RoundT = TypeVar("_RoundT", VerifyAttempt, GateRound)


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
    #: Rounds of the pre-submit gates. The post-submit gates ran inside
    #: :attr:`verify_attempts`, alongside the workflow's own verify stage.
    gate_rounds: tuple[GateRound, ...] = ()
    #: Names of every gate configured for this delivery, in declaration order.
    #: Empty is a recorded answer rather than missing information.
    declared_gates: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.outcome is DeliveryOutcome.PASSED

    @property
    def gates_configured(self) -> bool:
        """Whether any quality gate was configured for this delivery."""
        return bool(self.declared_gates)

    def gate_runs(self) -> tuple[GateRun, ...]:
        """Every gate execution in this delivery, in the order they ran."""
        runs: list[GateRun] = []
        for round_ in self.gate_rounds:
            runs.extend(round_.gates)
        for attempt in self.verify_attempts:
            runs.extend(attempt.gates)
        return tuple(runs)

    def gate(self, name: str) -> tuple[GateRun, ...]:
        """Every execution of the gate called *name*."""
        return tuple(run for run in self.gate_runs() if run.gate == name)

    def advisory_failures(self) -> tuple[GateRun, ...]:
        """Gate failures that were recorded and surfaced without stopping the run."""
        return tuple(run for run in self.gate_runs() if run.advisory_failure)

    def blocking_failures(self) -> tuple[GateRun, ...]:
        """Gate failures that stopped the flow."""
        return tuple(run for run in self.gate_runs() if run.blocked)

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
        """Whether a verify-class command actually ran, rather than skipping.

        Gates count: a pre-submit gate is verification that ran, and a workflow
        that declares its checks as gates rather than as a verify stage has not
        published something unchecked.
        """
        if any(attempt.executed for attempt in self.verify_attempts):
            return True
        return any(round_.executed for round_ in self.gate_rounds)

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
        """Run the gates, submit, verification, and publish for one run.

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

        try:
            gates = self.gates()
        except ConfigValidationError as exc:
            # Fail closed. A gate list that cannot be read is not a project
            # without gates: resolving it that way would make deleting one
            # character the way to turn every check off.
            self._record(EVENT_GATES, {"configured": [], "error": str(exc)})
            return DeliveryRun(
                outcome=DeliveryOutcome.REFUSED,
                reason=f"the configured quality gates cannot be read: {exc}",
                not_reached=DELIVERY_FLOW_STAGES,
            )
        declared = tuple(gate.name for gate in gates)
        self._record_gate_configuration(gates)

        stages: list[StageResult] = []
        attempts: list[VerifyAttempt] = []
        remaining = list(DELIVERY_FLOW_STAGES)

        pre_submit = tuple(gate for gate in gates if gate.runs_at(GATE_POSITION_PRE_SUBMIT))
        rounds = self._gate_rounds(pre_submit, GATE_POSITION_PRE_SUBMIT, context)
        if rounds and not rounds[-1].ok:
            # The review artifact is not raised. A blocking gate exists so a
            # finding is fixed by the run rather than delivered to a human.
            return self._failed(
                context,
                stages,
                attempts,
                remaining,
                rounds[-1].blocking_failures[0],
                gate_rounds=rounds,
                declared_gates=declared,
            )

        submit = self._run_stage(SUBMIT_STAGE, context)
        stages.append(submit)
        remaining.remove(SUBMIT_STAGE)
        if not submit.ok:
            return self._failed(
                context,
                stages,
                attempts,
                remaining,
                submit,
                gate_rounds=rounds,
                declared_gates=declared,
            )

        post_submit = tuple(gate for gate in gates if gate.runs_at(GATE_POSITION_POST_SUBMIT))
        attempts.extend(self._verify(context, gates=post_submit))
        stages.extend(attempt.stage for attempt in attempts)
        remaining.remove(VERIFY_STAGE)
        if attempts and not attempts[-1].ok:
            # Publish is not reached. A change that did not verify must not land
            # anywhere it is consumed, whatever the publish command would report.
            return self._failed(
                context,
                stages,
                attempts,
                remaining,
                attempts[-1].blocking_failures[0],
                gate_rounds=rounds,
                declared_gates=declared,
            )

        publish = self._run_stage(PUBLISH_STAGE, context)
        stages.append(publish)
        remaining.remove(PUBLISH_STAGE)
        addresses = _deployment_addresses(publish)
        if addresses:
            self._record(EVENT_PUBLISHED, {"addresses": list(addresses)})
        if not publish.ok:
            return self._failed(
                context,
                stages,
                attempts,
                remaining,
                publish,
                addresses=addresses,
                gate_rounds=rounds,
                declared_gates=declared,
            )

        run = DeliveryRun(
            outcome=DeliveryOutcome.PASSED,
            stages=tuple(stages),
            verify_attempts=tuple(attempts),
            deployment_addresses=addresses,
            gate_rounds=rounds,
            declared_gates=declared,
        )
        return self._with_integration(run, context)

    # --- quality gates -----------------------------------------------------

    def gates(self) -> tuple[QualityGate, ...]:
        """The quality gates configured for this pipeline, in declaration order.

        Read through the store on each call rather than held, matching how the
        workflow is resolved once per pipeline: a pipeline is constructed for one
        delivery, so the two reads see one configuration.
        """
        return load_quality_gates(self._store.document())

    def _record_gate_configuration(self, gates: Sequence[QualityGate]) -> None:
        """Record which gates this delivery will run, including none of them."""
        detail: dict[str, Any] = {
            "configured": [gate.name for gate in gates],
            "pre_submit": [gate.name for gate in gates if gate.runs_at(GATE_POSITION_PRE_SUBMIT)],
            "post_submit": [gate.name for gate in gates if gate.runs_at(GATE_POSITION_POST_SUBMIT)],
        }
        if not gates:
            detail["reason"] = NO_GATES_REASON
        self._record(EVENT_GATES, detail)

    def _gate_rounds(
        self,
        gates: Sequence[QualityGate],
        position: str,
        context: RunContext,
    ) -> tuple[GateRound, ...]:
        """Run *gates* at *position*, buying fix rounds for blocking failures."""
        if not gates:
            return ()
        return self._fix_loop(
            lambda attempt: GateRound(
                position=position,
                attempt=attempt,
                gates=self._run_gates(gates, context, position=position, attempt=attempt),
            )
        )

    def _run_gates(
        self,
        gates: Sequence[QualityGate],
        context: RunContext,
        *,
        position: str,
        attempt: int,
    ) -> tuple[GateRun, ...]:
        """Run every gate in *gates*, including the ones after a failure.

        A blocking failure stops the flow, not the rest of the round: running the
        remaining gates is what lets one fix dispatch answer every finding
        instead of each failure spending a round of the retry limit to reveal the
        next one.
        """
        return tuple(
            self._run_gate(gate, context, position=position, attempt=attempt) for gate in gates
        )

    def _run_gate(
        self,
        gate: QualityGate,
        context: RunContext,
        *,
        position: str,
        attempt: int,
    ) -> GateRun:
        result = self._executor.run_commands(
            VERIFY_STAGE,
            context,
            gate.commands,
            origin=gate.origin,
            declared_at=gate.declared_at,
        )
        run = GateRun(
            gate=gate.name,
            severity=gate.severity,
            position=position,
            attempt=attempt,
            result=result,
            output=_gate_output(result),
        )
        self._record_gate(run)
        return run

    # --- verification ------------------------------------------------------

    def _verify(
        self,
        context: RunContext,
        *,
        gates: Sequence[QualityGate] = (),
    ) -> tuple[VerifyAttempt, ...]:
        """Verify, dispatching fix tasks and verifying again up to the limit.

        One round is the workflow's verify stage followed by every post-submit
        gate. The stage runs first because it is the project's own definition of
        a checked change; the gates are what this installation adds to it.
        """
        return self._fix_loop(
            lambda attempt: VerifyAttempt(
                attempt=attempt,
                stage=self._run_stage(VERIFY_STAGE, context),
                position=GATE_POSITION_POST_SUBMIT,
                gates=self._run_gates(
                    gates, context, position=GATE_POSITION_POST_SUBMIT, attempt=attempt
                ),
            )
        )

    def _fix_loop(self, round_for: Callable[[int], _RoundT]) -> tuple[_RoundT, ...]:
        """Run verification rounds until one passes or the retry limit is spent.

        The limit is applied per verification point rather than per delivery: the
        pre-submit gates and the post-submit checks gate different things — the
        review artifact and the publish — and a run that spent its rounds fixing
        analyzers still needs rounds for the CI that runs on the artifact.
        """
        limit = int(self._store.effective(VERIFY_RETRY_LIMIT_SETTING, project=self._project).value)
        rounds: list[_RoundT] = []
        attempt = 0
        while True:
            current = round_for(attempt)
            if current.ok:
                rounds.append(current)
                return tuple(rounds)
            if attempt >= limit:
                rounds.append(
                    replace(
                        current,
                        fix=FixDispatch(
                            dispatched=False,
                            reason=f"the verify retry limit of {limit} is exhausted",
                        ),
                    )
                )
                return tuple(rounds)
            dispatch = self._dispatch_fixes(attempt=attempt, stage=current.blocking_failures[0])
            rounds.append(replace(current, fix=dispatch))
            if not dispatch.dispatched:
                # Nothing was fixed, so verifying again would produce the same
                # failure while spending another round of the limit.
                return tuple(rounds)
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
        decision = self._authority.integration(
            verified=run.verified,
            target=context.base_branch,
            delivered=run.outcome is not DeliveryOutcome.FAILED,
        )
        self._record(
            EVENT_INTEGRATION,
            {
                "permitted": decision.permitted,
                "target": decision.target,
                "target_protected": decision.target_protected,
                "ladder_permits": decision.ladder_permits,
                "auto_integrate": decision.auto_integrate,
                "verified": decision.verified,
                "delivered": decision.delivered,
                "reasons": list(decision.reasons),
            },
        )
        if not decision.permitted:
            logger.info(
                "integration for spec %r is reserved for human action (%s)",
                context.spec_name,
                ", ".join(decision.reasons),
            )
        return replace(run, integration=decision)

    def _failed(
        self,
        context: RunContext,
        stages: Sequence[StageResult],
        attempts: Sequence[VerifyAttempt],
        remaining: Sequence[str],
        cause: StageResult,
        *,
        addresses: tuple[str, ...] = (),
        gate_rounds: Sequence[GateRound] = (),
        declared_gates: Sequence[str] = (),
    ) -> DeliveryRun:
        run = DeliveryRun(
            outcome=DeliveryOutcome.FAILED,
            stages=tuple(stages),
            verify_attempts=tuple(attempts),
            deployment_addresses=addresses,
            reason=cause.reason or f"the {cause.stage} stage {cause.outcome.value}",
            not_reached=tuple(remaining),
            gate_rounds=tuple(gate_rounds),
            declared_gates=tuple(declared_gates),
        )
        # The integration decision is attached to a failed delivery too, so the
        # record answers the question rather than leaving it open. The failure
        # itself is one of the gates: a publish that deployed part of a change
        # and then exited non-zero passes every configured gate, so were the
        # outcome not evaluated this record would say integration was permitted
        # on a run that broke halfway through.
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

    def _record_gate(self, run: GateRun) -> None:
        """Record one gate execution, its exit status, and its output.

        Output is recorded here and not for an ordinary stage because a gate's
        finding *is* its output: a coverage delta or an analyzer diagnostic is
        unusable as an exit code alone, and re-running the gate to read it is not
        available after the fact. It is bounded before it is stored, since it was
        written by a program the engine does not control.
        """
        self._record(
            EVENT_GATE,
            {
                "gate": run.gate,
                "severity": run.severity,
                "position": run.position,
                "attempt": run.attempt,
                "outcome": run.result.outcome.value,
                "exit_status": run.exit_status,
                "blocked": run.blocked,
                "reason": run.reason,
                "declared_at": run.result.declared_at,
                "missing_variables": list(run.result.missing_variables),
                "output": run.output,
            },
        )

    def _record(self, event: str, detail: dict[str, Any]) -> None:
        if self._audit is None:
            return
        self._audit(event, detail)


def _gate_output(result: StageResult) -> str:
    """The gate's captured output as one bounded text.

    Both streams of every command that ran are kept, in order: a test runner
    prints its summary to stdout and a type checker prints its diagnostics to
    stderr, and a record that dropped one of them would be silent about half the
    gates. The result is bounded because the text was written by a program the
    engine does not control and it lands in a notification, a queue entry, and an
    audit record.
    """
    parts: list[str] = []
    for command in result.commands:
        for stream in (command.stdout, command.stderr):
            if stream and stream.strip():
                parts.append(stream.rstrip("\n"))
    text = "\n".join(parts)
    if len(text) <= MAX_GATE_OUTPUT_CHARS:
        return text
    return text[:MAX_GATE_OUTPUT_CHARS] + TRUNCATION_NOTICE


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
