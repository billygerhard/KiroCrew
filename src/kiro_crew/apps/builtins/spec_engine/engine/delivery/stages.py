"""The stage executor: runs one delivery stage's configured commands.

Ordering here is the safety property, not an implementation detail. A stage is
resolved, its variables assembled, and **every** command in it fully validated
and rendered before the first process is spawned. A stage whose third command
references a variable with no value therefore runs none of its commands, rather
than performing two side effects on a shared system and then discovering it
cannot finish. Refusing before the first spawn is also the only refusal that
means anything for stages that push, comment, or deploy.

Commands run through ``subprocess`` as argv lists. There is no shell anywhere in
this module and no code path that builds a command string: see
:mod:`.templates` for why that is what makes attacker-authored variable values
inert.

Three smaller decisions worth stating:

* **stdin is closed.** An unattended run has nobody to answer a prompt, so a
  command that reads stdin gets end-of-file immediately instead of hanging until
  the stage timeout.
* **The child gets its own process group**, so a timeout kills the whole tree.
  Killing only the direct child leaves a build's grandchildren holding the
  workspace and the timeout accomplishes nothing.
* **Captured output is capped.** Output is data from a program the engine does
  not control; an unbounded capture is a memory ceiling set by whatever that
  program prints.
"""

from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Mapping, Protocol, Sequence

from kiro_crew import platform_compat, sandbox

from ..config import DELIVERY_STAGES, ConfigStore, ConfigValidationError, ValueOrigin
from .templates import CommandTemplate, MissingVariableError, TemplateError
from .variables import RunContext, VariableError, build_variables
from .workflow import DeliveryWorkflow

logger = logging.getLogger(__name__)

#: Setting holding the per-command wall clock ceiling.
STAGE_TIMEOUT_SETTING = "timeouts.stage_command_s"

#: Characters kept per stream per command. Enough to hold a test failure or a
#: deployment address, bounded so one chatty command cannot exhaust memory.
MAX_CAPTURED_CHARS = 64 * 1024

#: Appended when a stream was cut at the cap, so a reader is never shown a
#: truncated tail as if it were the end of the output.
TRUNCATION_NOTICE = "\n[output truncated]"

#: Grace period for draining a killed command's pipes. A tree that still holds
#: them after this loses its output rather than blocking the pipeline.
_DRAIN_TIMEOUT_S = 5


class StageOutcome(str, Enum):
    """How a stage ended."""

    #: No commands configured for this stage. Not a failure.
    SKIPPED = "skipped"
    #: Every configured command exited zero.
    PASSED = "passed"
    #: A command exited non-zero, or could not be started.
    FAILED = "failed"
    #: A command exceeded the stage command timeout and was killed.
    TIMED_OUT = "timed_out"
    #: Nothing ran: a variable had no value, the configuration was unusable, or
    #: the workspace was not there. Distinct from FAILED because no side effect
    #: reached the outside world.
    REFUSED = "refused"


@dataclass(frozen=True)
class CommandOutcome:
    """What running one argv produced. Returned by a :class:`CommandRunner`."""

    exit_code: int | None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    #: Set when the program could not be started at all (absent, not executable).
    start_error: str = ""


@dataclass(frozen=True)
class CommandResult:
    """One executed command: what ran, what it produced, and how it ended."""

    argv: tuple[str, ...]
    outcome: StageOutcome
    exit_code: int | None
    stdout: str
    stderr: str
    duration_s: float

    @property
    def ok(self) -> bool:
        return self.outcome is StageOutcome.PASSED


@dataclass(frozen=True)
class StageResult:
    """The result of one stage: its outcome plus every command that ran."""

    stage: str
    outcome: StageOutcome
    commands: tuple[CommandResult, ...] = ()
    #: Human-readable cause for a skip or a refusal.
    reason: str = ""
    #: Variables a command referenced with no value, for a refusal.
    missing_variables: tuple[str, ...] = ()
    #: Which configuration layer declared the commands, when any resolved.
    origin: ValueOrigin | None = None
    declared_at: str = ""
    #: Variable names substituted into this stage's commands. Recorded rather
    #: than the values: a review title carries text from a public tracker.
    variables_used: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        """Whether the pipeline may continue past this stage."""
        return self.outcome in (StageOutcome.PASSED, StageOutcome.SKIPPED)

    @property
    def executed(self) -> bool:
        """Whether any command was spawned."""
        return bool(self.commands)


class CommandRunner(Protocol):
    """Runs one already-rendered argv list. The seam tests substitute."""

    def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        timeout_s: int,
    ) -> CommandOutcome: ...


def run_argv(argv: Sequence[str], *, cwd: Path, timeout_s: int) -> CommandOutcome:
    """Run *argv* in *cwd* with no shell, capturing bounded output.

    The spawn goes through the package's sandbox chokepoint. The program and its
    literal arguments come from configuration an operator wrote, but the values
    substituted into them do not: a branch name, an item title, a review body
    all originate in a tracker anyone may write to. Argv isolation keeps that
    text from becoming syntax; the sandbox is the second layer, hiding
    credential trees the workflow has no business reading and handing the child
    a scrubbed environment. Standard mode is the mode that leaves git-over-SSH
    and the AWS CLI working, which a delivery stage needs.

    The resource-limit preexec is the third layer: a stage command is a build or
    a push an operator configured but nobody watches at three in the morning, so
    a kernel-enforced ceiling is what keeps a runaway child from taking the host
    down with it.
    """
    wrapped, child_env, cleanup = sandbox.sandboxed_spawn_argv(list(argv))
    try:
        started: subprocess.Popen[str]
        try:
            # Both isolation flags are passed explicitly rather than unpacked
            # from a dict, which keeps the Popen overload resolvable for the
            # type checker. start_new_session is a no-op on Windows and the
            # creation flag is a no-op on POSIX; together they make the child's
            # tree killable.
            started = subprocess.Popen(
                wrapped,
                cwd=str(cwd),
                env=child_env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                errors="replace",
                preexec_fn=sandbox.resource_limit_preexec(),
                start_new_session=platform_compat.IS_POSIX,
                creationflags=platform_compat.CREATE_NEW_PROCESS_GROUP,
            )
        except (OSError, ValueError) as exc:
            return CommandOutcome(exit_code=None, start_error=f"cannot run {argv[0]!r}: {exc}")
        try:
            stdout, stderr = started.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            platform_compat.kill_process_tree(started.pid, platform_compat.SIGKILL)
            try:
                stdout, stderr = started.communicate(timeout=_DRAIN_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                stdout, stderr = "", ""
            return CommandOutcome(
                exit_code=None,
                stdout=_cap(stdout),
                stderr=_cap(stderr),
                timed_out=True,
            )
        return CommandOutcome(
            exit_code=started.returncode,
            stdout=_cap(stdout),
            stderr=_cap(stderr),
        )
    finally:
        # The chokepoint hands back a temp launcher / sandbox profile and makes
        # unlinking it the caller's job. A stage runs one command per pipeline
        # step, so dropping it leaks a file per command until the stale sweep.
        if cleanup:
            Path(cleanup).unlink(missing_ok=True)


class StageExecutor:
    """Executes one delivery stage's configured commands for a run."""

    def __init__(
        self,
        store: ConfigStore,
        *,
        project: str | None = None,
        workflow: DeliveryWorkflow | None = None,
        runner: CommandRunner | None = None,
    ) -> None:
        self._store = store
        self._project = project
        self._workflow = (
            workflow if workflow is not None else DeliveryWorkflow.load(store, project=project)
        )
        self._runner: CommandRunner = runner if runner is not None else run_argv

    @property
    def workflow(self) -> DeliveryWorkflow:
        """The workflow this executor resolved at construction."""
        return self._workflow

    def run(self, stage: str, context: RunContext) -> StageResult:
        """Run *stage* for the run described by *context*.

        Never raises for a configuration or variable problem: those are the
        stage's outcome, so a caller reports them alongside stages that did run
        instead of unwinding the pipeline through an exception.
        """
        if stage not in DELIVERY_STAGES:
            raise ValueError(f"unknown delivery stage: {stage!r}")
        try:
            configured = self._workflow.stage(stage)
        except ConfigValidationError as exc:
            return StageResult(stage=stage, outcome=StageOutcome.REFUSED, reason=str(exc))
        if configured is None:
            return StageResult(
                stage=stage,
                outcome=StageOutcome.SKIPPED,
                reason="no commands configured for this stage",
            )
        return self.run_commands(
            stage,
            context,
            configured.commands,
            origin=configured.origin,
            declared_at=configured.declared_at,
        )

    def run_commands(
        self,
        stage: str,
        context: RunContext,
        commands: Sequence[CommandTemplate],
        *,
        origin: ValueOrigin | None = None,
        declared_at: str = "",
    ) -> StageResult:
        """Run *commands* as *stage* for the run described by *context*.

        The stage's commands are one argument rather than resolved here, because
        a quality gate is a verify-stage command list that a workflow stage did
        not declare. Running it through this method rather than a second executor
        is what makes a gate obey the same rules as a stage: the whole list is
        validated before the first spawn, the same variable set is substituted,
        and a valueless reference refuses without executing anything.
        """
        if stage not in DELIVERY_STAGES:
            raise ValueError(f"unknown delivery stage: {stage!r}")
        if not commands:
            raise ValueError(f"no commands to run for the {stage!r} stage")
        try:
            values = build_variables(context, self._workflow.project_variables())
        except (ConfigValidationError, VariableError) as exc:
            return StageResult(
                stage=stage,
                outcome=StageOutcome.REFUSED,
                reason=str(exc),
                origin=origin,
                declared_at=declared_at,
            )

        missing = _missing_across(commands, values)
        if missing:
            # Reported before anything spawns: an empty substitution would turn a
            # push, a comment, or a deploy into a different command with the same
            # exit code.
            return StageResult(
                stage=stage,
                outcome=StageOutcome.REFUSED,
                reason="a command references variables that have no value for this run",
                missing_variables=missing,
                origin=origin,
                declared_at=declared_at,
            )

        workspace = Path(context.workspace_path) if context.workspace_path.strip() else None
        if workspace is None or not workspace.is_dir():
            return StageResult(
                stage=stage,
                outcome=StageOutcome.REFUSED,
                reason=f"the run's workspace is not a directory: {context.workspace_path!r}",
                origin=origin,
                declared_at=declared_at,
            )

        try:
            rendered = [command.render(values) for command in commands]
        except (MissingVariableError, TemplateError) as exc:  # pragma: no cover - guarded above
            return StageResult(
                stage=stage,
                outcome=StageOutcome.REFUSED,
                reason=str(exc),
                origin=origin,
                declared_at=declared_at,
            )

        timeout_s = int(self._store.effective(STAGE_TIMEOUT_SETTING, project=self._project).value)
        results: list[CommandResult] = []
        outcome = StageOutcome.PASSED
        reason = ""
        for argv in rendered:
            result = self._execute(argv, cwd=workspace, timeout_s=timeout_s)
            results.append(result)
            if not result.ok:
                # Stop at the first failure: later commands in a stage assume the
                # earlier ones happened.
                outcome = result.outcome
                reason = _failure_reason(result)
                break
        return StageResult(
            stage=stage,
            outcome=outcome,
            commands=tuple(results),
            reason=reason,
            origin=origin,
            declared_at=declared_at,
            variables_used=_variables_across(commands),
        )

    def _execute(self, argv: tuple[str, ...], *, cwd: Path, timeout_s: int) -> CommandResult:
        started = time.monotonic()
        produced = self._runner(argv, cwd=cwd, timeout_s=timeout_s)
        duration = time.monotonic() - started
        if produced.timed_out:
            outcome = StageOutcome.TIMED_OUT
        elif produced.start_error or produced.exit_code is None or produced.exit_code != 0:
            outcome = StageOutcome.FAILED
        else:
            outcome = StageOutcome.PASSED
        stderr = produced.stderr
        if produced.start_error:
            stderr = f"{produced.start_error}\n{stderr}" if stderr else produced.start_error
        # argv[0] only: the remaining elements can carry text from a public
        # tracker, and a log line is a place that text does not belong.
        logger.info(
            "delivery stage command %r finished as %s in %.2fs",
            argv[0],
            outcome.value,
            duration,
        )
        return CommandResult(
            argv=tuple(argv),
            outcome=outcome,
            exit_code=produced.exit_code,
            stdout=produced.stdout,
            stderr=stderr,
            duration_s=duration,
        )


def _missing_across(
    commands: Sequence[CommandTemplate], values: Mapping[str, str]
) -> tuple[str, ...]:
    """Every valueless variable across *commands*, in first-appearance order."""
    seen: dict[str, None] = {}
    for command in commands:
        for name in command.missing(values):
            seen.setdefault(name, None)
    return tuple(seen)


def _variables_across(commands: Sequence[CommandTemplate]) -> tuple[str, ...]:
    """Every variable referenced across *commands*, in first-appearance order."""
    seen: dict[str, None] = {}
    for command in commands:
        for name in command.variables:
            seen.setdefault(name, None)
    return tuple(seen)


def _failure_reason(result: CommandResult) -> str:
    if result.outcome is StageOutcome.TIMED_OUT:
        return f"{result.argv[0]!r} exceeded the stage command timeout and was killed"
    if result.exit_code is None:
        first_line = result.stderr.splitlines()[0] if result.stderr else ""
        return first_line or f"{result.argv[0]!r} could not run"
    return f"{result.argv[0]!r} exited {result.exit_code}"


def _cap(text: str | None) -> str:
    if not text:
        return ""
    if len(text) <= MAX_CAPTURED_CHARS:
        return text
    return text[:MAX_CAPTURED_CHARS] + TRUNCATION_NOTICE
