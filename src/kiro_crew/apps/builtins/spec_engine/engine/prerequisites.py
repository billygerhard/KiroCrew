"""Phase-scoped prerequisite checks: what a project still needs before anything runs.

A misconfiguration that surfaces halfway through an unattended run has already
cost credits and left a half-finished spec behind. These checks answer the same
questions up front, and they answer them **per phase**, because a project that
can author perfectly well may have no delivery workflow at all -- reporting one
unmet delivery check as a project-wide failure would stop authoring that works.

**Scoped by autonomy level, not by a vocabulary of its own.** A phase here *is* a
rung of the ladder, so the run gate can ask "every phase this level will reach"
through :meth:`~.autonomy.AutonomyLevel.permits` instead of a second mapping
that could disagree with the first. A second vocabulary for the same partition is
the shape every trust and fencing defect in this codebase has had.

**The gate refuses before the first credit.** :func:`gate_run` evaluates every
phase the run will reach -- including delivery and integration, which execute
long after the run starts -- and refuses up front. Checking delivery only when
delivery begins is what turns a missing program into a run that authored a whole
spec and then could not ship it.

**Read-only and zero-token.** Every check is a configuration read, a ``PATH``
lookup, or a git ref query. Nothing here takes a model, a provider transport, or
a ledger, so there is no path by which a preflight can spend: the absence is
structural rather than promised, and the program lookup and branch lookup are
injectable so the tests never touch the real environment either.
"""

from __future__ import annotations

import shutil
import subprocess  # nosec B404 - read-only git ref queries, argv lists, never a shell
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from .audit import AuditLog
from .autonomy import AutonomyLevel
from .budget.ceiling import CEILING_SETTING, Budget, resolve_budget
from .capabilities.registry import Binding, resolve_bindings
from .config import ConfigStore
from .config.schema import DELIVERY_STAGES, SECTION_QUALITY_GATES, SECTION_SOURCES
from .delivery.flow import load_quality_gates
from .delivery.integration import resolve_protected_branches
from .delivery.workflow import DeliveryWorkflow
from .notify.channels import resolve_channel
from .state import SpecRef

__all__ = [
    "AUDIT_PREREQUISITE_UNMET",
    "CheckName",
    "Prerequisite",
    "PrerequisiteReport",
    "RunRefusal",
    "check_project",
    "check_source",
    "gate_run",
    "stage_phase",
]

#: Audit event recorded when an unmet prerequisite prevents a run or dispatch.
AUDIT_PREREQUISITE_UNMET = "prerequisite.unmet"

#: Which phase each delivery stage belongs to. ``isolate`` is execution's because
#: it materializes the workspace the run's work happens in, and a project without
#: it still executes -- in its own tree, which is why zero configuration caps at
#: execution. The rest are delivery's: they raise, verify, and publish an
#: artifact, which is the work delivery names.
STAGE_PHASES: Mapping[str, AutonomyLevel] = {
    "isolate": AutonomyLevel.EXECUTION,
    "submit": AutonomyLevel.DELIVERY,
    "verify": AutonomyLevel.DELIVERY,
    "publish": AutonomyLevel.DELIVERY,
    "teardown": AutonomyLevel.DELIVERY,
}

#: Which phase binds each delegable capability. Authoring and analysis are used
#: while documents are written; review and implementation while tasks execute;
#: the rest are read by the surfaces that dispatch at all, so they sit at the
#: lowest rung that can reach them.
CAPABILITY_PHASES: Mapping[str, AutonomyLevel] = {
    "analysis": AutonomyLevel.AUTHORING,
    "authoring": AutonomyLevel.AUTHORING,
    "validation_rules": AutonomyLevel.AUTHORING,
    "model_catalog": AutonomyLevel.AUTHORING,
    "watch_sources": AutonomyLevel.AUTHORING,
    "review": AutonomyLevel.EXECUTION,
    "implementation": AutonomyLevel.EXECUTION,
}

#: Field naming a watch source's poll command.
POLL_FIELD = "poll"

ProgramResolver = Callable[[str], str | None]
BranchResolver = Callable[[str], bool]


class CheckName(str, Enum):
    """The prerequisite questions, one per thing that can be missing."""

    #: The programs a phase's configured commands invoke resolve on PATH.
    PROGRAMS = "programs"
    #: The capability providers a phase binds can be reached.
    PROVIDERS = "providers"
    #: The configured base branch exists in the project's repository.
    BASE_BRANCH = "base_branch"
    #: The protected branch set is non-empty and names real branches.
    PROTECTED_BRANCHES = "protected_branches"
    #: The configured notification channel resolves to a declared channel.
    NOTIFY_CHANNEL = "notify_channel"
    #: A budget ceiling is present for an enabled level above authoring.
    BUDGET_CEILING = "budget_ceiling"
    #: A watch source's poll program resolves, so the source can poll at all.
    WATCH_PROGRAMS = "watch_programs"


@dataclass(frozen=True)
class Prerequisite:
    """One check's result, and -- when unmet -- how to resolve it.

    *missing* and *action* are separate because they answer different questions:
    what the engine looked for and did not find, and what the operator does about
    it. An unmet check that states only the first leaves a reader to guess, so
    both are required rather than optional.
    """

    check: CheckName
    phase: AutonomyLevel
    met: bool
    #: What was looked for and not found. Empty when met.
    missing: str = ""
    #: The action that resolves it. Empty when met.
    action: str = ""
    #: Dotted configuration path involved, for the config surface to link to.
    declared_at: str = ""
    #: The watch source a source-scoped check is about, empty for project checks.
    source: str = ""

    def __post_init__(self) -> None:
        if self.met:
            return
        if not self.missing.strip():
            raise ValueError(f"unmet prerequisite {self.check.value} must say what is missing")
        if not self.action.strip():
            raise ValueError(
                f"unmet prerequisite {self.check.value} must state the action that resolves it"
            )

    def describe(self) -> str:
        if self.met:
            return f"{self.phase.value}/{self.check.value}: met"
        return f"{self.phase.value}/{self.check.value}: {self.missing} -- {self.action}"

    def detail(self) -> dict[str, Any]:
        return {
            "check": self.check.value,
            "phase": self.phase.value,
            "missing": self.missing,
            "action": self.action,
            "declared_at": self.declared_at,
            "source": self.source,
        }


@dataclass(frozen=True)
class PrerequisiteReport:
    """Every check that ran, in the order they were evaluated."""

    checks: tuple[Prerequisite, ...] = ()

    @property
    def met(self) -> bool:
        return not self.unmet

    @property
    def unmet(self) -> tuple[Prerequisite, ...]:
        return tuple(check for check in self.checks if not check.met)

    def for_phase(self, phase: AutonomyLevel) -> tuple[Prerequisite, ...]:
        return tuple(check for check in self.checks if check.phase is phase)

    def unmet_through(self, level: AutonomyLevel) -> tuple[Prerequisite, ...]:
        """Unmet checks for every phase *level* reaches, lowest phase first.

        Ordered by rung rather than by discovery so an operator reads the
        earliest blocking phase first: a missing authoring prerequisite is what
        they have to fix before a delivery one matters.
        """
        reachable = [check for check in self.unmet if level.permits(check.phase)]
        return tuple(sorted(reachable, key=lambda check: check.phase.rank))

    def by_phase(self) -> dict[AutonomyLevel, tuple[Prerequisite, ...]]:
        """Checks grouped by phase, in ladder order, for Doctor's rendering."""
        grouped: dict[AutonomyLevel, tuple[Prerequisite, ...]] = {}
        for phase in sorted(AutonomyLevel, key=lambda level: level.rank):
            found = self.for_phase(phase)
            if found:
                grouped[phase] = found
        return grouped

    def describe(self) -> str:
        if self.met:
            return f"{len(self.checks)} prerequisite(s) met"
        return f"{len(self.unmet)} of {len(self.checks)} prerequisite(s) unmet"


@dataclass(frozen=True)
class RunRefusal:
    """A run that will not start, and every prerequisite that stops it.

    Carries all of them rather than the first. An operator fixing one missing
    program only to be refused for the next has been told the truth one item at
    a time, which is slower than being told it once.
    """

    level: AutonomyLevel
    unmet: tuple[Prerequisite, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.unmet:
            raise ValueError("a run refusal must carry the prerequisites that caused it")

    @property
    def phases(self) -> tuple[AutonomyLevel, ...]:
        seen: dict[AutonomyLevel, None] = {}
        for check in self.unmet:
            seen.setdefault(check.phase, None)
        return tuple(seen)

    def describe(self) -> str:
        items = "; ".join(check.describe() for check in self.unmet)
        return f"refused at {self.level.value}: {items}"

    def detail(self) -> dict[str, Any]:
        return {
            "autonomy": self.level.value,
            "unmet": [check.detail() for check in self.unmet],
            "phases": [phase.value for phase in self.phases],
        }


def stage_phase(stage: str) -> AutonomyLevel:
    """The phase *stage* belongs to.

    Raises for an unknown stage rather than defaulting. A stage this function does
    not know would otherwise land in whichever phase the default names, and a new
    delivery stage silently checked at authoring is a check that never blocks the
    run that needs it.
    """
    try:
        return STAGE_PHASES[stage]
    except KeyError:
        raise ValueError(f"unknown delivery stage: {stage!r}") from None


def check_project(
    config: ConfigStore,
    *,
    project: str | None = None,
    which: ProgramResolver | None = None,
    branch_exists: BranchResolver | None = None,
    budget: Budget | None = None,
) -> PrerequisiteReport:
    """Evaluate every project prerequisite, each recorded against its phase.

    *which*, *branch_exists* and *budget* are injectable so this stays a pure read
    of configuration plus two lookups, and so a test can describe an environment
    rather than arrange one.
    """
    resolve_program = which or shutil.which
    checks: list[Prerequisite] = []
    checks.extend(_command_program_checks(config, project, resolve_program))
    checks.extend(_provider_checks(config, resolve_program))
    checks.extend(_branch_checks(config, project, branch_exists))
    checks.append(_channel_check(config, project))
    checks.extend(_ceiling_checks(config, project, budget))
    return PrerequisiteReport(checks=tuple(checks))


def check_source(
    config: ConfigStore,
    source: str,
    *,
    which: ProgramResolver | None = None,
) -> PrerequisiteReport:
    """Evaluate what *source* needs in order to poll at all.

    Kept separate from :func:`check_project` because the consequence differs. A
    source whose poll program is absent is **unhealthy**, and the one thing it
    must never be reported as is a source that found no items: "nothing to do"
    and "I cannot look" are opposite facts that would read identically on a
    dashboard.
    """
    resolve_program = which or shutil.which
    entry = _source_entry(config, source)
    path = f"{SECTION_SOURCES}.{source}.{POLL_FIELD}"
    argv = _argv(entry.get(POLL_FIELD))
    if not argv:
        return PrerequisiteReport(
            checks=(
                Prerequisite(
                    check=CheckName.WATCH_PROGRAMS,
                    phase=AutonomyLevel.AUTHORING,
                    met=False,
                    missing=f"watch source {source!r} declares no poll command",
                    action=f"declare {path} as the argv list that lists this source's items",
                    declared_at=path,
                    source=source,
                ),
            )
        )
    program = argv[0]
    resolved = resolve_program(program)
    return PrerequisiteReport(
        checks=(
            Prerequisite(
                check=CheckName.WATCH_PROGRAMS,
                phase=AutonomyLevel.AUTHORING,
                met=resolved is not None,
                missing=(
                    ""
                    if resolved is not None
                    else f"poll program {program!r} for source {source!r} is not on PATH"
                ),
                action=(
                    ""
                    if resolved is not None
                    else f"install {program!r} or change {path} to a program this host has"
                ),
                declared_at=path,
                source=source,
            ),
        )
    )


def gate_run(
    config: ConfigStore,
    level: AutonomyLevel,
    audit: AuditLog,
    ref: SpecRef,
    *,
    project: str | None = None,
    run: str | None = None,
    which: ProgramResolver | None = None,
    branch_exists: BranchResolver | None = None,
    budget: Budget | None = None,
) -> RunRefusal | None:
    """Refuse before the first credit if any phase *level* reaches is unmet.

    *audit* and *ref* are required rather than optional. Recording an unmet
    prerequisite that stopped a run is a requirement, and a caller that could
    omit the log would satisfy the refusal while losing the reason -- the same
    defect as a kill-switch stop that notifies nobody.

    Returns ``None`` when the run may start, so the caller's check reads as
    "refused or not" rather than as a truthiness test on a report.
    """
    report = check_project(
        config, project=project, which=which, branch_exists=branch_exists, budget=budget
    )
    unmet = report.unmet_through(level)
    if not unmet:
        return None
    refusal = RunRefusal(level=level, unmet=unmet)
    audit.append(
        ref,
        AUDIT_PREREQUISITE_UNMET,
        run=run,
        initiator=None,
        detail=refusal.detail(),
        cost=0.0,
    )
    return refusal


def _command_program_checks(
    config: ConfigStore, project: str | None, which: ProgramResolver
) -> list[Prerequisite]:
    """One check per program a configured stage or quality gate invokes."""
    checks: list[Prerequisite] = []
    workflow = DeliveryWorkflow.load(config, project=project)
    for stage in DELIVERY_STAGES:
        commands = workflow.stage(stage)
        if commands is None:
            # An unconfigured stage is not an unmet prerequisite. Most projects
            # configure no delivery at all, and reporting that as missing would
            # make the report unreadable for every one of them; the ladder
            # already caps such a project below delivery.
            continue
        for command in commands.commands:
            checks.append(
                _program_check(
                    command.program,
                    phase=stage_phase(stage),
                    declared_at=commands.declared_at,
                    used_for=f"delivery stage {stage!r}",
                    which=which,
                )
            )
    checks.extend(_gate_program_checks(config, project, which))
    return checks


def _gate_program_checks(
    config: ConfigStore, project: str | None, which: ProgramResolver
) -> list[Prerequisite]:
    """Quality gates hold argv the delivery flow executes, so their programs count.

    Read through :func:`~.delivery.flow.load_quality_gates` rather than parsed
    here. A second parser for the same section would be a second answer to "what
    gates are configured", and the two could disagree about which commands exist
    -- reporting a project as ready while the flow runs a program this never
    looked for. Gates are app-level, so *project* does not select a different set.
    """
    checks: list[Prerequisite] = []
    for gate in load_quality_gates(config.document()):
        for command in gate.commands:
            checks.append(
                _program_check(
                    command.program,
                    phase=AutonomyLevel.DELIVERY,
                    declared_at=gate.declared_at or SECTION_QUALITY_GATES,
                    used_for=f"quality gate {gate.name!r}",
                    which=which,
                )
            )
    return checks


def _provider_checks(config: ConfigStore, which: ProgramResolver) -> list[Prerequisite]:
    """Each phase's bound providers must be reachable.

    A builtin binding is reachable by construction -- it is this engine. Only a
    binding that runs a program can be unreachable, so that is the only case with
    something to look for.
    """
    checks: list[Prerequisite] = []
    for capability, binding in sorted(resolve_bindings(config).items()):
        if binding.is_builtin:
            continue
        program = binding.program
        if not program:
            checks.append(
                Prerequisite(
                    check=CheckName.PROVIDERS,
                    phase=_capability_phase(capability),
                    met=False,
                    missing=f"capability {capability!r} is delegated but names no program",
                    action=f"give {binding.declared_at or capability} an argv list to run",
                    declared_at=binding.declared_at,
                )
            )
            continue
        checks.append(
            _provider_check(capability, binding, which)
        )
    return checks


def _provider_check(capability: str, binding: Binding, which: ProgramResolver) -> Prerequisite:
    program = binding.program
    resolved = which(program)
    return Prerequisite(
        check=CheckName.PROVIDERS,
        phase=_capability_phase(capability),
        met=resolved is not None,
        missing=(
            ""
            if resolved is not None
            else f"provider program {program!r} for capability {capability!r} is not on PATH"
        ),
        action=(
            ""
            if resolved is not None
            else f"install {program!r} or unset {binding.declared_at} to use the builtin"
        ),
        declared_at=binding.declared_at,
    )


def _capability_phase(capability: str) -> AutonomyLevel:
    """The phase that binds *capability*.

    Unknown capabilities sit at authoring, the lowest rung, so a capability added
    to the schema without a phase here is checked by **every** run rather than
    only by the most autonomous ones. Defaulting upward would exempt the runs
    that need the check most.
    """
    return CAPABILITY_PHASES.get(capability, AutonomyLevel.AUTHORING)


def _branch_checks(
    config: ConfigStore, project: str | None, branch_exists: BranchResolver | None
) -> list[Prerequisite]:
    checks: list[Prerequisite] = []
    base = _base_branch(config, project)
    # The run's base is passed in rather than re-read inside, so the protected
    # set falls back to the same branch this project would integrate into.
    protected = resolve_protected_branches(
        config.document(), project=project, base_branch=base
    )
    branches = tuple(protected.branches)
    checks.append(
        Prerequisite(
            check=CheckName.PROTECTED_BRANCHES,
            phase=AutonomyLevel.INTEGRATION,
            met=bool(branches),
            missing="" if branches else "the protected branch set is empty",
            action=(
                ""
                if branches
                else "set a base branch or list protected_branches so integration has a floor"
            ),
            declared_at=protected.declared_at,
        )
    )
    base = _base_branch(config, project)
    if not base:
        # No configured base branch is the zero-configuration state rather than a
        # misconfiguration: the delivery workflow that would need one is absent
        # too, and the ladder already caps such a project below delivery. The
        # protected-set check above still ran, because an empty protected set is
        # a real gap at integration whatever the base branch says.
        return checks
    resolver = branch_exists or _git_branch_exists(_project_path(config, project))
    present = resolver(base)
    checks.append(
        Prerequisite(
            check=CheckName.BASE_BRANCH,
            phase=AutonomyLevel.DELIVERY,
            met=present,
            missing="" if present else f"base branch {base!r} does not exist in the project",
            action=(
                ""
                if present
                else f"create {base!r}, or point projects.{project}.base_branch at one that exists"
            ),
            declared_at=f"projects.{project}.base_branch",
        )
    )
    return checks


def _channel_check(config: ConfigStore, project: str | None) -> Prerequisite:
    """The notification channel must be one this app declares.

    ``resolve_channel`` never raises -- it substitutes the dashboard so a notice
    is always deliverable -- so the unmet condition is that it *had to*
    substitute. A check that only asked whether a route came back would pass for
    every configuration, which is a check that cannot fail.
    """
    route = resolve_channel(config, project=project)
    return Prerequisite(
        check=CheckName.NOTIFY_CHANNEL,
        phase=AutonomyLevel.AUTHORING,
        met=not route.substituted,
        missing=(
            ""
            if not route.substituted
            else f"configured notification channel does not resolve ({route.reason})"
        ),
        action=(
            ""
            if not route.substituted
            else "name a channel this app declares in notify.channel, or unset it for the dashboard"
        ),
        declared_at=route.declared_at,
    )


def _ceiling_checks(
    config: ConfigStore, project: str | None, budget: Budget | None
) -> list[Prerequisite]:
    """Every enabled level above authoring needs a ceiling.

    Authoring is exempt: it is the rung a zero-configuration project already sits
    on, and it cannot start an unattended run. Above it, an unbounded ceiling
    means an unattended run with no spending limit, which is the one failure a
    budget exists to prevent.

    ``bounded`` is asked of the budget rather than recomputed here, so "is a
    ceiling in force" has one answer shared with the guard that enforces it. The
    schema is a second layer that refuses to write a non-positive ceiling, and it
    makes this state hard to reach through the dashboard -- but the two layers are
    independent and each has to hold on its own. A preflight that trusted the
    schema would be a check that cannot fail.
    """
    resolved = budget if budget is not None else resolve_budget(config, project=project)
    bounded = resolved.bounded
    checks: list[Prerequisite] = []
    for level in sorted(AutonomyLevel, key=lambda item: item.rank):
        if level is AutonomyLevel.AUTHORING:
            continue
        checks.append(
            Prerequisite(
                check=CheckName.BUDGET_CEILING,
                phase=level,
                met=bounded,
                missing=(
                    ""
                    if bounded
                    else f"no finite budget ceiling is in force for unattended {level.value}"
                ),
                action=(
                    ""
                    if bounded
                    else f"set {CEILING_SETTING} to the credits one {level.value} run may spend"
                ),
                declared_at=resolved.declared_at or CEILING_SETTING,
            )
        )
    return checks


def _program_check(
    program: str,
    *,
    phase: AutonomyLevel,
    declared_at: str,
    used_for: str,
    which: ProgramResolver,
) -> Prerequisite:
    resolved = which(program) if program else None
    return Prerequisite(
        check=CheckName.PROGRAMS,
        phase=phase,
        met=resolved is not None,
        missing=(
            ""
            if resolved is not None
            else f"program {program!r} needed by {used_for} is not on PATH"
        ),
        action=(
            ""
            if resolved is not None
            else f"install {program!r} or change {declared_at} to a program this host has"
        ),
        declared_at=declared_at,
    )


def _git_branch_exists(root: Path | None) -> BranchResolver:
    """A read-only branch lookup rooted at *root*.

    ``rev-parse --verify`` with an explicit argv list and no shell. When there is
    no project path to ask in, every branch reads as absent rather than as
    present: an unanswerable question must not resolve to "fine".
    """

    def exists(branch: str) -> bool:
        if root is None or not branch:
            return False
        try:
            completed = subprocess.run(  # nosec B603 - fixed argv, no shell, read-only
                ["git", "-C", str(root), "rev-parse", "--verify", "--quiet", branch],
                capture_output=True,
                check=False,
                timeout=_GIT_TIMEOUT_S,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return completed.returncode == 0

    return exists


#: Bound on the branch lookup. A hung git call must not hold a preflight open.
_GIT_TIMEOUT_S = 10


def _project_path(config: ConfigStore, project: str | None) -> Path | None:
    """The project's own working tree, from its required ``path`` field.

    The project *name* is a key in the document, not a location, so the branch
    lookup has to ask in the configured path. Returning ``None`` when there is
    none makes every branch read as absent rather than as present.
    """
    declared = _project_field(config, project, "path")
    return Path(declared).expanduser() if declared else None


def _base_branch(config: ConfigStore, project: str | None) -> str:
    """The project's configured base branch.

    A project field rather than a setting, so it is read from the project entry.
    Reading it as a setting would resolve to nothing for every project and make
    the base-branch check silently unreachable.
    """
    return _project_field(config, project, "base_branch")


def _project_field(config: ConfigStore, project: str | None, field_name: str) -> str:
    entry = _project_entry(config, project)
    value = entry.get(field_name)
    return value.strip() if isinstance(value, str) else ""


def _project_entry(config: ConfigStore, project: str | None) -> Mapping[str, Any]:
    if not project:
        return {}
    projects = config.document().get("projects")
    if not isinstance(projects, Mapping):
        return {}
    entry = projects.get(project)
    return entry if isinstance(entry, Mapping) else {}


def _source_entry(config: ConfigStore, source: str) -> Mapping[str, Any]:
    node = config.document().get(SECTION_SOURCES)
    if not isinstance(node, Mapping):
        return {}
    entry = node.get(source)
    return entry if isinstance(entry, Mapping) else {}


def _argv(value: Any) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, Sequence):
        return ()
    return tuple(str(item) for item in value if isinstance(item, (str, int, float)))


def _command_list(value: Any) -> tuple[tuple[str, ...], ...]:
    if isinstance(value, str) or not isinstance(value, Sequence):
        return ()
    return tuple(_argv(item) for item in value)
