"""Configuration advisories: valid documents that describe a dangerous setup.

An advisory is not a validation error. Every document reaching this module has
already passed the schema, so nothing here refuses a write; each advisory names a
combination that is legal, saved, and worth telling the operator about before
they walk away from it.

Two advisories share one reason to exist: the moment the
document is written is the last moment a human is present, and both conditions
otherwise surface hours later in an unattended run with nobody reading its output.

**Unattended integration armed with nothing verifying the change.** Autonomous
integration writes to a destination a mistake cannot be taken back from, and a
project with no verify stage has told the engine nothing that could stop a bad
change from getting there. Both halves are individually reasonable — a local-only
workflow legitimately configures no verify stage, and auto-integration is a
deliberate opt-in — so neither is an error, and the combination is not something to
discover from a merged commit at three in the morning.

**A role's assigned agent that cannot reach the engine's tools.** A profile that
routes review to a specific agent is making a quality decision, and an agent whose
tool allowlist filters the engine's MCP server cannot record a verdict at all. The
run would fail mid-flight, at the point the assignment was supposed to improve.

A third advisory is stronger than a warning, because a stranger is on the other
end of it. **Execution-or-higher autonomy armed on a publicly submittable
source** means an item created by someone the operator has never met can start a
run that spends credits and runs configured commands with no human gate. So this
one both warns *and* requires an acknowledgment: the warning is the last point
the operator is told what they are turning on, and the acknowledgment is the
record that they were told. The warning is emitted whenever the source is public
— including when the operator never said whether it is, because undetermined
resolves to public here for the same reason the trust model treats an unknown
author as least-trusted: the safe direction when unsure is to warn, not to stay
silent. "Execution or higher" is read off the ladder's own ordering rather than a
list of level names, so a rung added above execution later is covered without an
edit here.

The warning is raised **where the setting is written**, not where it would fire,
and it carries the dotted path of the declaration rather than a prose description
of it, so an operator lands on the exact key to change.

Recording is left to the caller through :data:`WarningRecorder` (for warnings)
and :func:`acknowledge` (for the acknowledgment record). The audit log is
per-spec and a configuration edit is per-project, so binding the two here would
force this module to invent a spec identity it does not have; a surface that has
one passes a recorder and records the acknowledgment, and a surface that does not
still gets the warning.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from .agent_surface import ENGINE_MCP_SERVER, AgentSurfaceLookup, disk_lookup
from .effective import resolve
from .profiles import FIELD_AGENT, profiles
from .schema import AUTONOMY_LEVELS, SECTION_PROJECTS, SECTION_SOURCES, SECTION_WORKFLOW
from .settings import SETTINGS

#: Stable identifier for the auto-integration-without-verification advisory.
#: Identifiers are part of the contract: a refusal, a doctor finding, and an
#: audit entry all quote the same one, so an operator correlates them by name
#: rather than by matching prose.
AUTO_INTEGRATE_WITHOUT_VERIFY = "delivery.auto_integrate_without_verify"

#: An assigned agent exists but its tool allowlist filters the engine's tools.
AGENT_MISSING_ENGINE_TOOLS = "cost_profiles.agent_missing_engine_tools"

#: An assigned agent has no configuration this host can find.
AGENT_NOT_INSTALLED = "cost_profiles.agent_not_installed"

#: Execution-or-higher autonomy is armed on a publicly submittable source. Unlike
#: the other advisories this one requires an acknowledgment, because a stranger
#: can create the item that starts the run it authorizes.
PUBLIC_SOURCE_AUTONOMY = "sources.public_source_autonomy"

#: Audit event name for a recorded configuration warning.
CONFIG_WARNING_EVENT = "config.warning"

#: Audit event name for a recorded acknowledgment of a warning that requires one.
CONFIG_ACKNOWLEDGMENT_EVENT = "config.acknowledgment"

#: The setting that arms unattended integration.
AUTO_INTEGRATE_SETTING = "delivery.auto_integrate"

#: Key holding the stage-to-commands map inside a workflow object, and the stage
#: whose absence this module asks about.
_STAGES_KEY = "stages"
_VERIFY_STAGE = "verify"

#: Source-entry key declaring whether items are publicly submittable. The schema
#: owns the source field vocabulary; this names the one this module reads.
_PUBLIC_KEY = "public"

#: Source-entry key holding the per-(class, type) autonomy grid. Named locally
#: for the same reason the poller names its own keys: this module reads config,
#: it does not import the resolver that also names this field, so a circular
#: import between the config package and the autonomy resolver stays impossible.
_AUTONOMY_KEY = "autonomy"

#: The ladder rung at which unattended execution begins. Named once; the covered
#: set is everything at or above it in :data:`AUTONOMY_LEVELS`, so a rung added
#: above execution later is covered without editing this module. Enumerating the
#: covered names instead ("execution", "delivery", "integration") is the exact
#: shape that stops covering a level the ladder gains after it was written.
_EXECUTION_LEVEL = "execution"


@dataclass(frozen=True)
class ConfigWarning:
    """One valid-but-dangerous configuration, addressed by its dotted path."""

    code: str
    path: str
    message: str
    #: The project the warning applies to, ``None`` for an app-wide setting.
    project: str | None = None
    #: Whether an operator must acknowledge this warning, not merely be shown it.
    #: Most advisories are for display; this is set only where a warning names an
    #: authority a stranger could exercise, so the record that the operator was
    #: told is worth keeping. An acknowledgment is built through :func:`acknowledge`.
    requires_acknowledgment: bool = False

    def __str__(self) -> str:  # pragma: no cover - display only
        return f"{self.path}: {self.message}"

    @property
    def detail(self) -> dict[str, Any]:
        """Audit detail for this warning: identifier, location, and text."""
        record: dict[str, Any] = {"code": self.code, "path": self.path, "message": self.message}
        if self.project is not None:
            record["project"] = self.project
        return record


#: Handed each warning by a surface that wants to show or record it.
WarningRecorder = Callable[[ConfigWarning], None]


@dataclass(frozen=True)
class Acknowledgment:
    """A human's explicit acknowledgment of one warning that requires one.

    The point of the type is that it cannot be produced implicitly. It is built
    only by :func:`acknowledge`, which refuses a warning that does not ask for an
    acknowledgment and refuses an empty actor — so an absent key, a default
    value, or anything the engine wrote for itself can never stand in for a human
    saying "yes, I know what this arms". ``actor`` is who said it; ``code`` and
    ``path`` tie it to the exact declaration, so an acknowledgment of one source
    is not silently reused for another.
    """

    code: str
    path: str
    actor: str
    project: str | None = None

    def __str__(self) -> str:  # pragma: no cover - display only
        return f"{self.actor} acknowledged {self.code} at {self.path}"

    @property
    def detail(self) -> dict[str, Any]:
        """Audit detail: what was acknowledged, where, and by whom."""
        record: dict[str, Any] = {"code": self.code, "path": self.path, "actor": self.actor}
        if self.project is not None:
            record["project"] = self.project
        return record


def acknowledge(warning: ConfigWarning, actor: str) -> Acknowledgment:
    """Build the record that *actor* was warned about *warning* and proceeded.

    The two guards are the whole point: an acknowledgment that a default value,
    an absent field, or the engine itself could satisfy would record nothing an
    operator actually did.

    Raises ``ValueError`` when *warning* does not require an acknowledgment (only
    ack-requiring warnings have one to give), and when *actor* is empty or blank
    (an acknowledgment with nobody behind it is the implicit acceptance the
    warning exists to prevent).
    """
    if not warning.requires_acknowledgment:
        raise ValueError(f"warning {warning.code!r} does not require an acknowledgment")
    identity = actor.strip()
    if not identity:
        raise ValueError("an acknowledgment needs the identity of who gave it")
    return Acknowledgment(
        code=warning.code,
        path=warning.path,
        actor=identity,
        project=warning.project,
    )


def document_warnings(
    doc: Mapping[str, Any],
    *,
    agents: AgentSurfaceLookup | None = None,
) -> tuple[ConfigWarning, ...]:
    """Return every advisory the merged document earns.

    The auto-integration advisory is evaluated per project, plus once app-wide
    when no project is configured yet. A project is the unit that has a workflow,
    so an app-wide switch with three projects under it is three separate
    situations: two may verify and one may not, and reporting that as one app-wide
    warning would name a location the operator cannot act on.

    *agents* resolves an assigned agent's tool surface; it defaults to reading the
    agent directories the document implies. A caller that already knows the
    surfaces passes its own and touches no disk.
    """
    warnings: list[ConfigWarning] = []
    warnings.extend(_assigned_agent_warnings(doc, agents))
    warnings.extend(_public_source_autonomy_warnings(doc))
    projects = doc.get(SECTION_PROJECTS)
    names = tuple(projects) if isinstance(projects, Mapping) else ()
    if not names:
        warnings.extend(_auto_integrate_warnings(doc, project=None))
    for name in names:
        if isinstance(name, str):
            warnings.extend(_auto_integrate_warnings(doc, project=name))
    return tuple(warnings)


def record_config_warnings(
    warnings: Sequence[ConfigWarning],
    recorder: WarningRecorder | None,
) -> tuple[ConfigWarning, ...]:
    """Hand each warning to *recorder*, returning them unchanged.

    A ``None`` recorder is the ordinary case for a caller that only wants to
    display them, so the absence of a recorder is not an error.
    """
    if recorder is not None:
        for warning in warnings:
            recorder(warning)
    return tuple(warnings)


def _assigned_agent_warnings(
    doc: Mapping[str, Any],
    agents: AgentSurfaceLookup | None,
) -> tuple[ConfigWarning, ...]:
    """Advisories for every Host_Agent a cost profile assigns to a role.

    One lookup per distinct agent name, not per assignment: a profile that routes
    three roles to the same agent has one thing wrong with it, and three warnings
    naming one agent read as three problems.
    """
    parsed = profiles(doc)
    assignments = [
        assignment
        for profile in parsed.values()
        for assignment in profile.assignments.values()
        if assignment.assigns_agent
    ]
    if not assignments:
        return ()
    lookup = agents if agents is not None else disk_lookup(doc)
    surfaces: dict[str, bool] = {}
    warnings: list[ConfigWarning] = []
    for assignment in assignments:
        agent = assignment.agent
        path = f"{assignment.declared_at}.{FIELD_AGENT}"
        if agent in surfaces:
            continue
        surface = lookup(agent)
        surfaces[agent] = surface.grants(ENGINE_MCP_SERVER)
        if surfaces[agent]:
            continue
        if not surface.found:
            warnings.append(
                ConfigWarning(
                    code=AGENT_NOT_INSTALLED,
                    path=path,
                    message=(
                        f"role {assignment.role!r} is assigned to agent {agent!r}, which has "
                        "no configuration on this host, so a session for that role cannot "
                        "start; install the agent or remove the assignment"
                    ),
                )
            )
            continue
        warnings.append(
            ConfigWarning(
                code=AGENT_MISSING_ENGINE_TOOLS,
                path=path,
                message=(
                    f"role {assignment.role!r} is assigned to agent {agent!r}, whose tool "
                    f"allowlist does not include the spec engine tools (@{ENGINE_MCP_SERVER}), "
                    "so that role's session cannot drive the run; grant the server in the "
                    "agent's tools or assign a different agent"
                ),
            )
        )
    return tuple(warnings)


def _public_source_autonomy_warnings(doc: Mapping[str, Any]) -> tuple[ConfigWarning, ...]:
    """One warning per publicly submittable source that arms execution or higher.

    One per source, not one per armed grid cell: a source is the unit an operator
    turned autonomy on for, and a source that grants execution to three submitter
    classes is one decision to acknowledge, not three. The armed cells are named
    in the message so the operator can see exactly which grants earned it.
    """
    sources = doc.get(SECTION_SOURCES)
    if not isinstance(sources, Mapping):
        return ()
    warnings: list[ConfigWarning] = []
    for name, entry in sources.items():
        if not isinstance(name, str) or not isinstance(entry, Mapping):
            continue
        if not _source_is_public(entry):
            continue
        armed = _armed_autonomy_cells(entry, name)
        if not armed:
            continue
        base_path = f"{SECTION_SOURCES}.{name}.{_AUTONOMY_KEY}"
        grants = ", ".join(f"{cell} = {level}" for cell, level in armed)
        warnings.append(
            ConfigWarning(
                code=PUBLIC_SOURCE_AUTONOMY,
                path=base_path,
                message=(
                    f"source {name!r} accepts publicly submitted items and grants "
                    f"execution-or-higher autonomy ({grants}), so an item created by "
                    "anyone can start a run that spends credits and runs configured "
                    "commands with no human gate; acknowledge this or lower the autonomy "
                    "level, and set this source's 'public' flag to false if its items "
                    "are not publicly submittable"
                ),
                requires_acknowledgment=True,
            )
        )
    return tuple(warnings)


def _source_is_public(entry: Mapping[str, Any]) -> bool:
    """Whether a source's items are publicly submittable.

    Undetermined is public. A source that never declared the flag resolves to
    public so the warning fires when the operator has not said, matching the
    trust model's least-trusted-when-unknown stance: a spurious warning costs a
    sentence read, a missed one costs an unattended run a stranger started.
    """
    declared = entry.get(_PUBLIC_KEY)
    if isinstance(declared, bool):
        return declared
    return True


def _armed_autonomy_cells(entry: Mapping[str, Any], name: str) -> tuple[tuple[str, str], ...]:
    """The (dotted path, level) of every grid cell at execution or higher."""
    grid = entry.get(_AUTONOMY_KEY)
    if not isinstance(grid, Mapping):
        return ()
    armed: list[tuple[str, str]] = []
    for class_key, by_type in grid.items():
        if not isinstance(class_key, str) or not isinstance(by_type, Mapping):
            continue
        for type_key, level in by_type.items():
            if not isinstance(type_key, str) or not isinstance(level, str):
                continue
            if _is_execution_or_higher(level):
                cell = f"{SECTION_SOURCES}.{name}.{_AUTONOMY_KEY}.{class_key}.{type_key}"
                armed.append((cell, level))
    return tuple(armed)


def _is_execution_or_higher(level: str) -> bool:
    """Whether *level* is execution or a more autonomous rung of the ladder.

    Derived from :data:`AUTONOMY_LEVELS`, the ladder's single owner, so "or
    higher" tracks the ordering rather than a frozen list of names. An
    unrecognised level (which a validated document cannot hold, but a hand-edited
    file reaching this read can) is treated as covered: warn when unsure.
    """
    try:
        rank = AUTONOMY_LEVELS.index(level)
    except ValueError:
        return True
    return rank >= AUTONOMY_LEVELS.index(_EXECUTION_LEVEL)


def _auto_integrate_warnings(
    doc: Mapping[str, Any], *, project: str | None
) -> tuple[ConfigWarning, ...]:
    """The advisory for one scope, empty when the combination is not present."""
    effective = resolve(doc, SETTINGS[AUTO_INTEGRATE_SETTING], project=project)
    if not effective.value:
        return ()
    if _verifies(doc, project=project):
        return ()
    where = effective.declared_at or AUTO_INTEGRATE_SETTING
    scope = "this project" if project is not None else "the app"
    return (
        ConfigWarning(
            code=AUTO_INTEGRATE_WITHOUT_VERIFY,
            path=where,
            message=(
                f"autonomous integration is enabled for {scope} but no verify stage is "
                "configured, so nothing checks a change before it reaches the protected "
                "destination; configure a verify stage or turn autonomous integration off"
            ),
            project=project,
        ),
    )


def _verifies(doc: Mapping[str, Any], *, project: str | None) -> bool:
    """Whether a verify stage resolves for *project*.

    Narrow on purpose: this asks only whether the stage exists, never what it
    runs. Stage semantics — parsing, precedence, and what an unconfigured stage
    means — belong to the delivery workflow resolver, which is the module the
    pipeline reads. The precedence mirrored here is project-replaces-app, and a
    test pins this answer against that resolver so the two cannot drift apart.
    """
    if project is not None and _has_verify(_project_entry(doc, project)):
        return True
    return _has_verify(doc)


def _project_entry(doc: Mapping[str, Any], project: str) -> Mapping[str, Any]:
    projects = doc.get(SECTION_PROJECTS)
    if not isinstance(projects, Mapping):
        return {}
    entry = projects.get(project)
    return entry if isinstance(entry, Mapping) else {}


def _has_verify(container: Mapping[str, Any]) -> bool:
    workflow = container.get(SECTION_WORKFLOW)
    if not isinstance(workflow, Mapping):
        return False
    stages = workflow.get(_STAGES_KEY)
    if not isinstance(stages, Mapping):
        return False
    return stages.get(_VERIFY_STAGE) is not None
