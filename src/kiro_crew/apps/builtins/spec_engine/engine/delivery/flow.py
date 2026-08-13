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

**An explicit human request starts this pipeline, not a second one.** Interactive
delivery is the same :meth:`DeliveryPipeline.deliver` call with a *requester*
named on it. There is deliberately no separate interactive path: a second
entry point would be a second place for the stage order, the variable set, the
gate list and the retry ceiling to be decided, and the two would drift in the
direction that is never noticed — the interactive one skipping a check because a
human was watching, which is precisely when nobody re-reads the audit trail. So
the requester decides *who may start* the flow and changes nothing about what the
flow does.

What the requester does change is the authorization question. The autonomy ladder
says how far the engine may go *unasked*; it was never a monopoly on asking, and
the execution gate already answers it this way. A named person may therefore
start delivery at any resolved level — otherwise a default install, whose
unconfigured policy resolves to authoring, could never deliver interactively at
all. Two floors still hold, because neither is about who asked: a project with no
configured delivery workflow has described no way to isolate, submit, or verify,
so there is nothing for a request to start; and integration remains gated on its
own posture switch, so starting a delivery is not consent to an unattended merge.

**A requester the engine could have minted is not a human.** The reserved
autonomy-policy identity namespace is engine-issued, in more than one spelling —
a refusal's initiator is rewritten to a parenthesised form specifically so it
cannot be mistaken for an approval. Handing either spelling back as a requester
would launder policy authority into "an explicit human action", so the whole
namespace is refused rather than the one approver spelling.

**Completion and failure both notify, with every executed stage's outcome.** One
notice at the end of the flow, for the run that passed and for the run that
failed, listing each stage the pipeline ran and how it ended, each gate with its
severity, the stages never reached, and any deployment address the publish
printed. A notifier that only fired on success would leave the case an operator
actually needs told — the unattended run that stopped — as the silent one.

Routing is the notify package's, not this module's: configuration owns the
destination, so a channel named here is a request that may be declined, and the
route's own substitution reason is carried out untouched because it is the only
thing an operator can act on. Delivery of the notice is best-effort and the run
state is primary: a notice that cannot be handed over is recorded as undelivered
and never changes what the delivery did.
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
from ..notify import Delivery
from ..phases import INITIATOR_POLICY, INITIATOR_USER, is_reserved_actor
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

#: Notified once, after the submit stage has actually raised the review artifact.
#: The pipeline owns *when* the artifact exists; it does not own the item-feedback
#: writeback, its ledger claim, or the tracker conversation, all of which live in
#: the watch layer. So the seam is a callback the driver backs with the one shared
#: feedback poster rather than an import of it: keeping the poster out of the
#: delivery layer is what stops a second writeback route from appearing here, and
#: routing the callback through that one poster is what keeps the ``delivery_submitted``
#: comment on the same at-most-once ledger as every other lifecycle event.
OnSubmitted = Callable[[RunContext], None]

#: Audit event names.
EVENT_STAGE = "delivery.stage"
EVENT_FIX_DISPATCH = "delivery.fix_dispatch"
EVENT_PUBLISHED = "delivery.published"
EVENT_INTEGRATION = "delivery.integration"

#: One per delivery attempt, naming who asked for it and whether the request
#: stood. Recorded for a refusal too: a caller presenting an identity only the
#: engine may mint is the event most worth having written down.
EVENT_REQUESTED = "delivery.requested"

#: One per delivery that entered the flow: its outcome, every stage's outcome,
#: and what became of the notification.
EVENT_OUTCOME = "delivery.outcome"

#: One per gate execution: which gate, at which position, in which round, how it
#: ended, and what it printed.
EVENT_GATE = "delivery.gate"

#: One per delivery, naming the gates configured for it. Recorded even when none
#: are, because "no gate ran" and "no gate was configured" are the same sentence
#: only if somebody wrote it down.
EVENT_GATES = "delivery.gates"

#: Reason recorded with :data:`EVENT_GATES` when a project configured no gates.
NO_GATES_REASON = "no quality gates are configured, so the flow ran without them"

#: Recorded when a delivery ended with no notifier wired to the pipeline. The
#: absence is written down rather than passed over: the completion notice is the
#: only thing that tells an operator an unattended delivery stopped, so a
#: pipeline nobody gave a notifier to must be visible in the trail as such.
NO_NOTIFIER_REASON = "no notifier is wired to this pipeline"

#: Refusal for a delivery request that names nobody. An interactive start is an
#: explicit human action, and an action attributable to no one is not one.
UNNAMED_REQUESTER_REASON = (
    "an interactive delivery names the person who asked for it, and this request names nobody"
)


class Notifier(Protocol):
    """Where a delivery's completion notice goes. ``HostNotifier`` satisfies it.

    Narrow on purpose, and deliberately not a channel: this module decides *that*
    an operator must be told and what the notice says. Which channel it lands on
    is configuration's answer, resolved inside the notifier.
    """

    def send(
        self,
        title: str,
        body: str = "",
        *,
        quoted: str = "",
        channel: str = "",
        priority: str | None = None,
        group_key: str = "",
        detail: Mapping[str, Any] | None = None,
    ) -> Delivery: ...


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
class DeliveryNotice:
    """The completion-or-failure notice for one delivery, and what became of it.

    Carried on the run rather than only sent, because "did anybody get told" is
    part of what happened: delivery of a notice is best-effort while the run state
    is primary, so an undelivered notice must leave a record instead of leaving
    silence.

    The stage list is every stage the pipeline produced a result for, in the order
    they ran, including the ones that skipped — a submit that skipped and a submit
    that passed are different things to read, and an omitted entry says neither.
    """

    outcome: DeliveryOutcome
    title: str
    body: str
    #: Text the engine did not author. A stage's failure reason can be the first
    #: line of a command's stderr, so it travels fenced rather than interpolated
    #: into prose a surface renders as markdown.
    quoted: str = ""
    #: ``(stage, outcome)`` per stage result, in the order the stages ran.
    stage_outcomes: tuple[tuple[str, str], ...] = ()
    #: ``(gate, severity, outcome)`` per gate execution, in the order they ran.
    gate_outcomes: tuple[tuple[str, str, str], ...] = ()
    not_reached: tuple[str, ...] = ()
    addresses: tuple[str, ...] = ()
    notified: bool = False
    #: Host bus channel the notice reached, empty when it reached none.
    channel: str = ""
    #: Why the route replaced the configured channel, taken from the route. Read
    #: through rather than restated: the router's reason names the document an
    #: operator has to edit, and a reason written here would send them elsewhere.
    route_reason: str = ""
    #: Why the notice was not delivered, when it was not.
    error: str = ""

    @property
    def delivered(self) -> bool:
        return self.notified and not self.error

    def detail(self) -> dict[str, Any]:
        """The notice as bounded detail for the note's meta map and the audit log."""
        detail: dict[str, Any] = {"outcome": self.outcome.value}
        if self.stage_outcomes:
            detail["stages"] = ", ".join(f"{stage}={how}" for stage, how in self.stage_outcomes)
        if self.gate_outcomes:
            detail["gates"] = ", ".join(
                f"{gate}({severity})={how}" for gate, severity, how in self.gate_outcomes
            )
        if self.not_reached:
            detail["not_reached"] = ", ".join(self.not_reached)
        if self.addresses:
            detail["addresses"] = ", ".join(self.addresses)
        return detail


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
    #: The person whose explicit action started this delivery, empty when the
    #: Autonomy_Policy's own authority did. Never carries a refused identity: a
    #: trail scan for what started a delivery must not turn up a request that
    #: started nothing.
    initiator: str = ""
    #: Whether a person or the policy is credited with starting this delivery.
    initiator_kind: str = ""
    #: The completion notice, absent only for a request refused before the flow.
    notice: DeliveryNotice | None = None

    @property
    def ok(self) -> bool:
        return self.outcome is DeliveryOutcome.PASSED

    @property
    def interactive(self) -> bool:
        """Whether an explicit human action started this delivery."""
        return self.initiator_kind == INITIATOR_USER

    def stage_outcomes(self) -> tuple[tuple[str, str], ...]:
        """Every stage result this delivery produced, with how it ended."""
        return tuple((result.stage, result.outcome.value) for result in self.stages)

    def gate_outcomes(self) -> tuple[tuple[str, str, str], ...]:
        """Every gate execution: its name, its severity, and how it ended."""
        return tuple((run.gate, run.severity, run.result.outcome.value) for run in self.gate_runs())

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
        notifier: Notifier | None = None,
        channel: str = "",
        on_submitted: OnSubmitted | None = None,
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
        self._notifier = notifier
        self._channel = channel
        self._on_submitted = on_submitted
        self._isolated: StageResult | None = None
        #: The run this pipeline isolated for, learned at isolate time. A
        #: published deployment is recorded against it so archive can find the
        #: environment from the run identifier alone; without it a deployment row
        #: would be keyed to nobody and could never be torn down.
        self._run_id: str = ""

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

        The result is held on the pipeline so the delivery notice can report it.
        Isolation runs hours before delivery, and a notice that listed only the
        stages of the final pass would omit the stage that made the workspace the
        others ran in.
        """
        result = self._isolate(context, run_id=run_id)
        self._isolated = result
        # Learned here rather than at deliver time: isolation is where the run's
        # own working tree is claimed, so it is the one point that knows which run
        # a later publish deployment belongs to.
        self._run_id = run_id
        return result

    def _isolate(self, context: RunContext, *, run_id: str) -> StageResult:
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

    def deliver(self, context: RunContext, *, requester: str | None = None) -> DeliveryRun:
        """Run the gates, submit, verification, and publish for one run.

        Returns rather than raises for every stage-level problem, so a caller
        reports what ran alongside what did not instead of losing the earlier
        stages to an exception from a later one.

        *requester* names the person whose explicit action started this delivery,
        and naming one is the whole of what an interactive delivery is: the same
        stages, the same variables, the same gates, the same retry ceiling. It
        changes only who may start the flow — a named person may, at any resolved
        autonomy level, because the ladder governs what the engine does unasked
        rather than who may ask. It does not lift the two floors that are not
        about who asked: a project with no configured delivery workflow has
        nothing to start, and integration keeps its own posture gate.

        An identity from the engine's reserved namespace is not a person, in any
        of its spellings, and is refused: accepting one would let policy authority
        re-enter as an explicit human action.

        With no *requester* the Autonomy_Policy has to carry the authority itself,
        which is the autonomous path and unchanged.
        """
        refusal = self._authorization_refusal(requester)
        kind = INITIATOR_USER if requester is not None else INITIATOR_POLICY
        self._record(EVENT_REQUESTED, self._request_detail(requester, refusal))
        if refusal:
            # Nothing ran, so there is nothing to notify about and no side effect
            # left the host. A human who asked has this answer in their hand; an
            # unattended run that the policy does not authorize is waiting at a
            # gate the run lifecycle already reports.
            return DeliveryRun(
                outcome=DeliveryOutcome.REFUSED,
                reason=refusal,
                not_reached=DELIVERY_FLOW_STAGES,
                initiator_kind=kind,
            )
        run = replace(
            self._flow(context),
            initiator=requester or "",
            initiator_kind=kind,
        )
        return self._notified(run, context)

    def _authorization_refusal(self, requester: str | None) -> str:
        """Why this delivery may not start, or the empty string when it may."""
        if requester is not None:
            claim = _requester_refusal(requester)
            if claim:
                return claim
            if not self._authority.workflow_configured:
                return (
                    "the project has no configured delivery workflow, so an explicit "
                    "request has no stages to start"
                )
            return ""
        if self._authority.permits(AutonomyLevel.DELIVERY):
            return ""
        return "the autonomy policy does not authorize delivery for this run" + (
            "; the project has no configured delivery workflow"
            if not self._authority.workflow_configured
            else ""
        )

    def _request_detail(self, requester: str | None, refusal: str) -> dict[str, Any]:
        """Audit fields naming who asked for this delivery and how it was judged."""
        decision = self._authority.decision
        detail: dict[str, Any] = {
            "initiator_kind": INITIATOR_USER if requester is not None else INITIATOR_POLICY,
            "accepted": not refusal,
            "autonomy_level": self._authority.level.value,
            "policy_declared_at": decision.declared_at or None,
        }
        if requester is not None:
            # A refused identity is recorded under a key of its own rather than as
            # the initiator, so the forged claim stays visible while a search for
            # what started a delivery cannot match it.
            detail["requester" if not refusal else "claimed_requester"] = requester
        if refusal:
            detail["reason"] = refusal
        return detail

    def _flow(self, context: RunContext) -> DeliveryRun:
        """The stage flow itself, once the request is authorized."""
        # The isolate result leads the stage list when this pipeline ran one. It
        # is the stage the rest of the flow depended on, so a record of what the
        # delivery did that started at submit would be missing its foundation.
        isolated: tuple[StageResult, ...] = (self._isolated,) if self._isolated is not None else ()
        try:
            gates = self.gates()
        except ConfigValidationError as exc:
            # Fail closed. A gate list that cannot be read is not a project
            # without gates: resolving it that way would make deleting one
            # character the way to turn every check off.
            self._record(EVENT_GATES, {"configured": [], "error": str(exc)})
            return DeliveryRun(
                outcome=DeliveryOutcome.REFUSED,
                stages=isolated,
                reason=f"the configured quality gates cannot be read: {exc}",
                not_reached=DELIVERY_FLOW_STAGES,
            )
        declared = tuple(gate.name for gate in gates)
        self._record_gate_configuration(gates)

        stages: list[StageResult] = list(isolated)
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
        self._post_submitted(context, submit)

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
            # Recorded whether or not publish exited zero: a command that
            # deployed and then failed still left a live environment, and
            # requirement 20.1 records every deployment the run created so
            # archive's teardown commands can find it from the run id alone.
            self._record_deployments(addresses)
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
            refused = [
                failure
                for failure in current.blocking_failures
                if failure.outcome is StageOutcome.REFUSED
            ]
            if len(refused) == len(current.blocking_failures):
                # A refusal happened before anything ran, so it says nothing about
                # the code and no fix task can change it: the same configuration
                # would refuse identically on every remaining round. Asking for
                # fixes anyway spends real credits on an unattended path to
                # rediscover a config error, which is the same reasoning that
                # already stops this loop when a dispatcher creates nothing.
                rounds.append(
                    replace(
                        current,
                        fix=FixDispatch(
                            dispatched=False,
                            reason=(
                                "the blocking check refused before executing, so the "
                                f"configuration it names must change: {refused[0].reason}"
                            ),
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

    def _record_deployments(self, addresses: Sequence[str]) -> None:
        """Record each published address in the workspace ledger against the run.

        The broker holds the ledger and knows how to record a deployment as a
        non-disposable row, so recording routes through it rather than opening a
        second writer — the isolation module explains why a deployment is not a
        path the terminal sweep may delete.

        Skipped without a broker or without a run identity: both are the ledger
        coordinates a deployment row is keyed on, and a row keyed to neither could
        never be found for teardown. Recording is best-effort like the rest of the
        delivery's side effects — a ledger write that fails is logged and does not
        fail a delivery whose change already published.
        """
        if self._isolation is None or not self._run_id.strip():
            return
        for address in addresses:
            try:
                self._isolation.record_deployment(self._run_id, address=address)
            except Exception as exc:  # a ledger write must not unwind a publish
                logger.warning(
                    "could not record deployment %s for run %s: %s",
                    address,
                    self._run_id,
                    exc,
                )

    # --- the completion notice ---------------------------------------------

    def _notified(self, run: DeliveryRun, context: RunContext) -> DeliveryRun:
        """Notify the outcome of every executed stage, and record what happened.

        One exit for both outcomes. A notifier reached only on the way out of a
        successful delivery would leave the unattended failure — the case an
        operator is actually waiting to hear about — as the silent one, and no
        test of the passing path would say so.

        Delivery of the notice is best-effort and the run is primary: a channel
        that cannot be reached loses the message, never the delivery's outcome.
        The exception is therefore swallowed here and recorded, which is also why
        the notice is carried on the run — an undelivered notice leaves a record
        rather than leaving silence.
        """
        notice = self._compose_notice(run, context)
        if self._notifier is None:
            notice = replace(notice, error=NO_NOTIFIER_REASON)
            logger.warning(
                "delivery for spec %r finished as %s with no notifier wired",
                context.spec_name,
                run.outcome.value,
            )
        else:
            try:
                delivered = self._notifier.send(
                    notice.title,
                    notice.body,
                    quoted=notice.quoted,
                    # A request, not the answer: the notifier resolves the
                    # project's configured channel and may decline this one.
                    channel=self._channel,
                    group_key=context.spec_name,
                    detail=notice.detail(),
                )
            except Exception as exc:
                notice = replace(notice, error=str(exc))
                logger.warning(
                    "delivery notice for spec %r was not delivered: %s", context.spec_name, exc
                )
            else:
                notice = replace(
                    notice,
                    notified=True,
                    channel=delivered.channel,
                    # Read through from the route rather than restated. The
                    # router's reason names the configuration an operator must
                    # edit; a reason written here would send them hunting a
                    # caller instead.
                    route_reason=delivered.route.reason,
                )
        self._record(EVENT_OUTCOME, _outcome_detail(run, notice))
        return replace(run, notice=notice)

    def _compose_notice(self, run: DeliveryRun, context: RunContext) -> DeliveryNotice:
        """Build the notice for *run*: what ran, how it ended, and where it went."""
        stage_outcomes = run.stage_outcomes()
        gate_outcomes = run.gate_outcomes()
        return DeliveryNotice(
            outcome=run.outcome,
            title=f"Spec delivery {run.outcome.value}: {context.spec_name}",
            body=_notice_body(run, context, stage_outcomes, gate_outcomes),
            # The cause can be the first line of a command's stderr, so it is
            # fenced by the notifier rather than interpolated into the body.
            quoted=run.reason,
            stage_outcomes=stage_outcomes,
            gate_outcomes=gate_outcomes,
            not_reached=run.not_reached,
            addresses=run.deployment_addresses,
        )

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

    def _post_submitted(self, context: RunContext, submit: StageResult) -> None:
        """Announce that the review artifact was raised, once, after submit.

        Only when the submit stage actually spawned a command: a project with no
        submit stage skips it, and there is no artifact to write back about, so a
        ``delivery_submitted`` comment then would report a submission that never
        happened. Best-effort like the rest of the notice path -- the observer is
        backed by the feedback poster, which records a failure without raising, so
        a tracker refusal cannot unwind a delivery that already submitted.
        """
        if self._on_submitted is None or not submit.executed:
            return
        self._on_submitted(context)

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


def _requester_refusal(requester: str) -> str:
    """Why *requester* is not an explicit human action, or "" when it is.

    Two refusals, and the second is the one that matters. A blank name is an
    action attributable to nobody, which an interactive start cannot be.

    A name inside the engine's reserved identity namespace is refused whatever its
    punctuation, because the engine mints more than one spelling of it: the
    approver form when the policy approves a gate, and a parenthesised form on a
    refusal, written that way precisely so it cannot be read as an approval. A
    guard keyed to the approver spelling alone would hand that refusal's own
    initiator straight back as a human requester — turning authority the policy
    was denied into an explicit human action attributed to a person.
    """
    if not requester.strip():
        return UNNAMED_REQUESTER_REASON
    if is_reserved_actor(requester):
        return (
            f"{requester!r} claims the Autonomy_Policy's reserved identity as a human "
            "requester; delivery starts from a named person or from the policy's own "
            "authority, never from one wearing the other's identity"
        )
    return ""


def _outcome_detail(run: DeliveryRun, notice: DeliveryNotice) -> dict[str, Any]:
    """Audit fields for one finished delivery, including the notice's fate."""
    detail = notice.detail()
    detail["initiator_kind"] = run.initiator_kind
    detail["notified"] = notice.notified
    detail["channel"] = notice.channel
    if notice.route_reason:
        detail["route_reason"] = notice.route_reason
    if notice.error:
        detail["error"] = notice.error
    if run.reason:
        detail["reason"] = run.reason
    return detail


def _notice_body(
    run: DeliveryRun,
    context: RunContext,
    stage_outcomes: Sequence[tuple[str, str]],
    gate_outcomes: Sequence[tuple[str, str, str]],
) -> str:
    """The notice's prose: every stage's outcome, every gate's, and what was not run.

    Engine-authored throughout. The stage and gate names come from the schema's
    stage list and an operator's own gate declarations, and the outcomes are enum
    values — nothing a command printed reaches this text, which is why the failure
    cause travels separately as fenced content.

    Addresses come last so that a publish which printed many of them is what gets
    clipped by the notifier's body cap, rather than the stage list a reader needs.
    """
    lines = [f"Delivery for the {context.spec_name!r} spec finished as {run.outcome.value}."]
    if stage_outcomes:
        lines += ["", "Stages:"]
        lines += [f"- {stage}: {how}" for stage, how in stage_outcomes]
    if run.not_reached:
        lines += ["", f"Stages not reached: {', '.join(run.not_reached)}"]
    lines.append("")
    if gate_outcomes:
        lines.append("Quality gates:")
        lines += [f"- {gate} ({severity}): {how}" for gate, severity, how in gate_outcomes]
    else:
        lines.append(f"Quality gates: {NO_GATES_REASON}")
    if run.deployment_addresses:
        lines += ["", "Deployment addresses:"]
        lines += [f"- {address}" for address in run.deployment_addresses]
    return "\n".join(lines)


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
