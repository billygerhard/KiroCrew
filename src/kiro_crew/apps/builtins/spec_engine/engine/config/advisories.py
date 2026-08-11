"""Configuration advisories: valid documents that describe a dangerous setup.

An advisory is not a validation error. Every document reaching this module has
already passed the schema, so nothing here refuses a write; each advisory names a
combination that is legal, saved, and worth telling the operator about before
they walk away from it.

Two advisories live here, and they share one reason to exist: the moment the
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

The warning is raised **where the setting is written**, not where it would fire,
and it carries the dotted path of the declaration rather than a prose description
of it, so an operator lands on the exact key to change.

Recording is left to the caller through :data:`WarningRecorder`. The audit log is
per-spec and a configuration edit is per-project, so binding the two here would
force this module to invent a spec identity it does not have; a surface that has
one passes a recorder, and a surface that does not still gets the warning.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from .agent_surface import ENGINE_MCP_SERVER, AgentSurfaceLookup, disk_lookup
from .effective import resolve
from .profiles import FIELD_AGENT, profiles
from .schema import SECTION_PROJECTS, SECTION_WORKFLOW
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

#: Audit event name for a recorded configuration warning.
CONFIG_WARNING_EVENT = "config.warning"

#: The setting that arms unattended integration.
AUTO_INTEGRATE_SETTING = "delivery.auto_integrate"

#: Key holding the stage-to-commands map inside a workflow object, and the stage
#: whose absence this module asks about.
_STAGES_KEY = "stages"
_VERIFY_STAGE = "verify"


@dataclass(frozen=True)
class ConfigWarning:
    """One valid-but-dangerous configuration, addressed by its dotted path."""

    code: str
    path: str
    message: str
    #: The project the warning applies to, ``None`` for an app-wide setting.
    project: str | None = None

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
