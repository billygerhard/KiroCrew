"""Resolving the Delivery_Workflow: which commands a stage runs, and from where.

Stage commands are read through the config store, which is the only reader of
the document, so the workflow the pipeline runs is the workflow the write path
validated and the configuration surface displays. Nothing here can be set by a
tool call: the workflow lives under a config-only path precisely because it is
the list of commands the app may run unattended.

A workflow is read **once** per pipeline and reused across the stages of that
run. A configuration saved while a run is between stages would otherwise submit
with one workflow and publish with another, and the run's audit trail would name
two workflows without any record of which stage saw which.

Absence resolves rather than fails, in two layered ways:

* A stage with no configured commands **skips**. Not every workflow raises a
  review artifact or publishes anything, so "not configured" is an answer.
* A project with no configured stages at all is the zero-configuration case:
  authoring and execution happen in the working tree, matching what the IDE
  does, and autonomy is capped at execution. A project that configured nothing
  has told the engine nothing about how to isolate a workspace, how to raise a
  review, or what verifies a change — so it must not reach delivery or
  integration, whatever else resolves.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from ..config import (
    AUTONOMY_LEVELS,
    DELIVERY_STAGES,
    ConfigError,
    ConfigStore,
    ConfigValidationError,
    ValueOrigin,
)
from ..config.schema import SECTION_PROJECTS, SECTION_WORKFLOW
from .templates import CommandTemplate, TemplateError

#: Key holding the stage-to-commands map inside a workflow object.
STAGES_KEY = "stages"

#: Key holding a project's custom stage command variables.
VARIABLES_KEY = "variables"

#: The stage that materializes a workspace for a run. Named here because the
#: absence of this one stage is what decides where a run's work happens.
ISOLATE_STAGE = "isolate"

#: The highest autonomy level a project with no configured workflow reaches.
#: Delivery and integration both mean running commands that were never
#: configured, so the ladder stops one rung below them.
ZERO_CONFIG_AUTONOMY_CEILING = "execution"


@dataclass(frozen=True)
class StageCommands:
    """The commands one stage runs, and which configuration layer declared them."""

    stage: str
    commands: tuple[CommandTemplate, ...]
    origin: ValueOrigin
    #: Dotted path of the declaration, for reporting and for the config surface.
    declared_at: str

    @property
    def variables(self) -> tuple[str, ...]:
        """Every variable referenced across this stage's commands."""
        seen: dict[str, None] = {}
        for command in self.commands:
            for name in command.variables:
                seen.setdefault(name, None)
        return tuple(seen)


class DeliveryWorkflow:
    """The stage-to-commands configuration in force for one project."""

    def __init__(self, document: Mapping[str, Any], *, project: str | None = None) -> None:
        self._document = document
        self._project = project

    @classmethod
    def load(cls, store: ConfigStore, *, project: str | None = None) -> "DeliveryWorkflow":
        """Read the workflow in force for *project* through the config store."""
        return cls(store.document(), project=project)

    @property
    def project(self) -> str | None:
        return self._project

    def stage(self, stage: str) -> StageCommands | None:
        """Return *stage*'s commands, or ``None`` when the stage is unconfigured.

        A project declaration replaces the app-wide one for that stage, so an
        organization overrides the one stage that differs rather than restating a
        whole workflow.
        """
        if stage not in DELIVERY_STAGES:
            raise ValueError(f"unknown delivery stage: {stage!r}")
        for origin, path, node in self._stage_declarations(stage):
            if node is None:
                continue
            return StageCommands(
                stage=stage,
                commands=_parse_commands(node, path),
                origin=origin,
                declared_at=path,
            )
        return None

    def configured_stages(self) -> tuple[str, ...]:
        """Stages that resolve to commands, in the schema's stage order."""
        return tuple(stage for stage in DELIVERY_STAGES if self.stage(stage) is not None)

    @property
    def configured(self) -> bool:
        """Whether any stage resolves to commands for this project.

        Selecting a named preset is not counted here. A preset that has not been
        expanded into stage commands would authorize delivery for a pipeline
        whose every stage then skips, which reports success for work nobody did.
        When preset expansion lands it feeds this same resolution, so this stays
        the one place the question is answered.
        """
        return bool(self.configured_stages())

    @property
    def isolates(self) -> bool:
        """Whether a run gets a workspace of its own from this workflow.

        False is the zero-configuration answer and the IDE's behavior: with no
        isolate stage there is nothing to materialize a branch, worktree, or
        copy, so authoring and execution happen in the project's own working
        tree. It is also why autonomy stops at execution there — concurrent runs
        would otherwise share one tree.
        """
        return self.stage(ISOLATE_STAGE) is not None

    def project_variables(self) -> dict[str, str]:
        """The project's custom stage command variables, empty when none."""
        entry = self._project_entry()
        node = entry.get(VARIABLES_KEY) if isinstance(entry, Mapping) else None
        path = f"{SECTION_PROJECTS}.{self._project}.{VARIABLES_KEY}"
        if node is None:
            return {}
        if not isinstance(node, Mapping):
            raise ConfigValidationError([ConfigError(path, "expected an object of strings")])
        variables: dict[str, str] = {}
        for name, value in node.items():
            if not isinstance(name, str) or not isinstance(value, str):
                raise ConfigValidationError([ConfigError(f"{path}.{name}", "expected a string")])
            variables[name] = value
        return variables

    # --- resolution order --------------------------------------------------

    def _stage_declarations(self, stage: str) -> tuple[tuple[ValueOrigin, str, Any], ...]:
        """Candidate declarations for *stage*, narrowest configuration layer first."""
        project_stages = self._stages_node(self._project_entry())
        app_stages = self._stages_node(self._document)
        project_prefix = f"{SECTION_PROJECTS}.{self._project}.{SECTION_WORKFLOW}.{STAGES_KEY}"
        return (
            (
                ValueOrigin.PROJECT_CONFIG,
                f"{project_prefix}.{stage}",
                project_stages.get(stage) if project_stages is not None else None,
            ),
            (
                ValueOrigin.APP_CONFIG,
                f"{SECTION_WORKFLOW}.{STAGES_KEY}.{stage}",
                app_stages.get(stage) if app_stages is not None else None,
            ),
        )

    def _project_entry(self) -> Mapping[str, Any]:
        if self._project is None:
            return {}
        projects = self._document.get(SECTION_PROJECTS)
        if not isinstance(projects, Mapping):
            return {}
        entry = projects.get(self._project)
        return entry if isinstance(entry, Mapping) else {}

    @staticmethod
    def _stages_node(container: Mapping[str, Any]) -> Mapping[str, Any] | None:
        workflow = container.get(SECTION_WORKFLOW)
        if not isinstance(workflow, Mapping):
            return None
        stages = workflow.get(STAGES_KEY)
        return stages if isinstance(stages, Mapping) else None


def cap_autonomy(level: str, *, workflow_configured: bool) -> str:
    """Return *level* lowered to what the project's configuration supports.

    The resolved level is an input rather than something read here: autonomy
    resolution owns the policy, and this applies the one ceiling that follows
    from the delivery workflow being absent. Capping never raises a level.
    """
    if level not in AUTONOMY_LEVELS:
        raise ValueError(f"unknown autonomy level: {level!r}")
    if workflow_configured:
        return level
    ceiling = AUTONOMY_LEVELS.index(ZERO_CONFIG_AUTONOMY_CEILING)
    if AUTONOMY_LEVELS.index(level) <= ceiling:
        return level
    return ZERO_CONFIG_AUTONOMY_CEILING


def _parse_commands(node: Any, path: str) -> tuple[CommandTemplate, ...]:
    """Parse a stage's configured command list into templates.

    Raised errors carry the dotted configuration path, so a bad template is
    reported where an operator can fix it rather than as a stage that failed for
    reasons found in a traceback.
    """
    if isinstance(node, (str, bytes)) or not isinstance(node, (list, tuple)):
        raise ConfigValidationError(
            [ConfigError(path, "expected a list of commands, each a list of arguments")]
        )
    if not node:
        raise ConfigValidationError([ConfigError(path, "expected at least one command")])
    commands: list[CommandTemplate] = []
    for index, argv in enumerate(node):
        try:
            commands.append(CommandTemplate.parse(argv))
        except TemplateError as exc:
            raise ConfigValidationError([ConfigError(f"{path}[{index}]", str(exc))]) from exc
    return tuple(commands)
