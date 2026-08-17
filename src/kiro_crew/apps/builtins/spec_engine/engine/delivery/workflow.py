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

from copy import deepcopy
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
from ..config.schema import (
    SECTION_PROJECTS,
    SECTION_WORKFLOW,
    WORKFLOW_PRESET_KEY,
    WORKFLOW_PRESETS_KEY,
)
from ..config.schema import WORKFLOW_STAGES_KEY as STAGES_KEY
from .isolation import GIT_ISOLATE_COMMANDS, ISOLATED_PATH_VARIABLE
from .templates import CommandTemplate, TemplateError

#: Key holding a project's custom stage command variables.
VARIABLES_KEY = "variables"

#: The stage that materializes a workspace for a run. Named here because the
#: absence of this one stage is what decides where a run's work happens.
ISOLATE_STAGE = "isolate"

#: The highest autonomy level a project with no configured workflow reaches.
#: Delivery and integration both mean running commands that were never
#: configured, so the ladder stops one rung below them.
ZERO_CONFIG_AUTONOMY_CEILING = "execution"

#: Bundled workflow presets, keyed by the name a project selects them under.
#:
#: Each entry is the ``workflow`` object configuration already holds -- a stage
#: map of command lists -- so a preset is a starting point copied in and edited,
#: never a live binding. :func:`workflow_presets` deep-copies one.
#:
#: Three decisions here are worth stating, because each avoids a second mechanism
#: rather than adding one.
#:
#: **The git isolate commands come from :func:`..isolation.git_isolate_commands`.**
#: That fetch-then-worktree pair is what makes the new branch a cut of the base
#: branch as it is *now*, and it is the isolate step the workspace broker's own
#: conflict reporting is written against. A second spelling of it here would be a
#: second answer to "how does the git preset isolate".
#:
#: **No teardown stage in the git presets.** A worktree is a disposable kind the
#: engine removes itself, so teardown commands that also removed it would be a
#: second remover racing the first. Teardown is where a project removes what its
#: *publish* stage created, which is why it is empty until a project publishes.
#:
#: **No verify stage in the two remote presets.** What verifies a change is
#: project knowledge -- its CI, its checks -- and the app-level quality gates
#: already carry the bundled ``make`` checks at pre-submit. A verify stage
#: guessing at a CI-watching invocation would run a command nobody configured.
#: The local-only preset is the exception because verifying locally *is* what it
#: is for.
WORKFLOW_PRESETS: Mapping[str, Mapping[str, tuple[tuple[str, ...], ...]]] = {
    "git-pull-request": {
        ISOLATE_STAGE: GIT_ISOLATE_COMMANDS,
        "submit": (
            # WARNING, and the reason this preset is not yet safe for a project
            # with unrelated uncommitted work: these commands do NOT run in the
            # run's worktree. ``isolated_context`` sets ``isolated_path`` for the
            # isolate stage alone and deliberately leaves ``workspace_path``
            # alone, and the flow passes the ORIGINAL context to submit, so every
            # command here runs in the project's own tree and ``{isolated_path}``
            # is not even in scope. ``git add --all`` therefore stages whatever is
            # in the project tree, and the commit does not land on the branch the
            # push then names. An earlier version of this comment asserted the
            # opposite ("commands run in the run's own worktree, so staging
            # everything stages this run's work and nothing else"), which is how a
            # preset comes to license the one command that makes the gap harmful.
            # Recorded as a follow-up obligation on requirement 13 in tasks.md.
            ("git", "add", "--all"),
            ("git", "commit", "--message", "{review_title}", "--message", "{review_summary}"),
            ("git", "push", "--set-upstream", "origin", "{branch_name}"),
            # Supplying both title and body is what keeps this non-interactive.
            (
                "gh",
                "pr",
                "create",
                "--base",
                "{base_branch}",
                "--head",
                "{branch_name}",
                "--title",
                "{review_title}",
                "--body",
                "{review_summary}",
            ),
        ),
    },
    "git-merge-request": {
        ISOLATE_STAGE: GIT_ISOLATE_COMMANDS,
        "submit": (
            ("git", "add", "--all"),
            ("git", "commit", "--message", "{review_title}", "--message", "{review_summary}"),
            ("git", "push", "--set-upstream", "origin", "{branch_name}"),
            # ``--yes`` because glab otherwise asks for confirmation, and a
            # delivery pipeline has nobody to answer it.
            (
                "glab",
                "mr",
                "create",
                "--target-branch",
                "{base_branch}",
                "--source-branch",
                "{branch_name}",
                "--title",
                "{review_title}",
                "--description",
                "{review_summary}",
                "--yes",
            ),
        ),
    },
    "local-only": {
        # Deliberately NOT the git preset's isolate: there is no remote to
        # refresh from, so the branch is cut from the local base ref. A fetch
        # here would fail the stage on a repository that has no origin, which is
        # the case this preset exists for.
        ISOLATE_STAGE: (
            (
                "git",
                "worktree",
                "add",
                "{" + ISOLATED_PATH_VARIABLE + "}",
                "-b",
                "{branch_name}",
                "{base_branch}",
            ),
        ),
        "submit": (
            ("git", "add", "--all"),
            ("git", "commit", "--message", "{review_title}", "--message", "{review_summary}"),
        ),
        # The build-and-test half. A failing verify earns fix rounds before a
        # human is asked to look, which is what makes it a delivery stage rather
        # than one more app-level gate. ``make`` for the same reason the gate
        # presets use it: it is the entry point a project already has.
        "verify": (
            ("make", "build"),
            ("make", "test"),
        ),
    },
}

#: The bundled preset names, in declaration order.
WORKFLOW_PRESET_NAMES: tuple[str, ...] = tuple(WORKFLOW_PRESETS)


def workflow_presets(name: str) -> dict[str, Any]:
    """Return *name*'s bundled workflow, ready to write into ``workflow``.

    Deep copies: a configuration surface offers a preset for editing, and an edit
    that reached back into the bundled table would change what every later
    project is offered in this process.

    The result records which preset it came from in its ``preset`` field, so a
    surface can say whether a stage still holds preset commands or a project's
    own without keeping a second record of the choice.

    Raises ``KeyError`` for an unknown name rather than returning an empty
    workflow: an empty workflow is the zero-configuration case, which caps
    autonomy at execution and would read as a project that configured nothing
    instead of a selection that did not resolve.
    """
    preset = WORKFLOW_PRESETS.get(name)
    if preset is None:
        raise KeyError(
            f"unknown workflow preset: {name!r}; bundled presets are "
            f"{', '.join(WORKFLOW_PRESET_NAMES)}"
        )
    return {
        WORKFLOW_PRESET_KEY: name,
        STAGES_KEY: {
            stage: [list(argv) for argv in commands] for stage, commands in preset.items()
        },
    }


def workflow_preset_definition(bundled: str) -> dict[str, Any]:
    """Return *bundled* as a user-defined preset definition, ready to name and edit.

    The copy path for a bundled preset an organization wants to change wholesale
    rather than one stage at a time: the caller writes the result under a name of
    its own in ``workflow.presets``, and that definition is then editable like any
    other configuration. Delegates to :func:`workflow_presets` so there is one
    copier and one place an unknown name is refused; drops the ``preset`` field,
    which records a *selection* and would name the bundled preset the definition
    stopped being a copy of at the first edit.
    """
    return {STAGES_KEY: workflow_presets(bundled)[STAGES_KEY]}


@dataclass(frozen=True)
class PresetSelection:
    """The workflow preset in force for a project, and where it was selected."""

    name: str
    #: Configuration layer that selected it, project narrowing over app.
    origin: ValueOrigin
    #: Dotted path of the selection.
    declared_at: str
    #: Whether the definition is the engine's own rather than the document's.
    bundled: bool
    #: The preset's stages, as command lists. Always a copy of the definition,
    #: bundled or user-defined, so mutating it edits neither the engine's table
    #: nor the project's configuration document.
    stages: Mapping[str, Any]


@dataclass(frozen=True)
class StageCommands:
    """The commands one stage runs, and which configuration layer declared them."""

    stage: str
    commands: tuple[CommandTemplate, ...]
    origin: ValueOrigin
    #: Dotted path of the declaration, for reporting and for the config surface.
    declared_at: str
    #: Name of the preset these commands came from, ``None`` when the layer named
    #: in ``origin`` declared them itself. This is what lets a surface say
    #: "preset" or "project override" per stage without a second record of the
    #: selection: the selection stays the one ``preset`` field in configuration,
    #: and this is derived from it at read time.
    preset: str | None = None

    @property
    def from_preset(self) -> bool:
        """Whether the selected preset supplied these commands."""
        return self.preset is not None

    @property
    def variables(self) -> tuple[str, ...]:
        """Every variable referenced across this stage's commands."""
        seen: dict[str, None] = {}
        for command in self.commands:
            for name in command.variables:
                seen.setdefault(name, None)
        return tuple(seen)


class _Unresolved:
    """Sentinel type: the selection has not been read yet, distinct from none."""


_UNRESOLVED = _Unresolved()


class DeliveryWorkflow:
    """The stage-to-commands configuration in force for one project."""

    def __init__(self, document: Mapping[str, Any], *, project: str | None = None) -> None:
        self._document = document
        self._project = project
        # Resolved once per instance, like the document itself: a preset copy per
        # stage query would hand each stage its own copy of the same definition.
        self._selection: PresetSelection | None | _Unresolved = _UNRESOLVED

    @classmethod
    def load(cls, store: ConfigStore, *, project: str | None = None) -> "DeliveryWorkflow":
        """Read the workflow in force for *project* through the config store."""
        return cls(store.document(), project=project)

    @property
    def project(self) -> str | None:
        return self._project

    def stage(self, stage: str) -> StageCommands | None:
        """Return *stage*'s commands, or ``None`` when the stage is unconfigured.

        A project declaration replaces the app-wide one for that stage, and both
        replace the selected preset's, so an organization selects a preset and
        overrides the one stage that differs rather than restating a whole
        workflow. Layering is per stage: overriding ``submit`` leaves every other
        stage the preset's.
        """
        if stage not in DELIVERY_STAGES:
            raise ValueError(f"unknown delivery stage: {stage!r}")
        for origin, path, node, preset in self._stage_declarations(stage):
            if node is None:
                continue
            return StageCommands(
                stage=stage,
                commands=_parse_commands(node, path),
                origin=origin,
                declared_at=path,
                preset=preset,
            )
        return None

    def configured_stages(self) -> tuple[str, ...]:
        """Stages that resolve to commands, in the schema's stage order."""
        return tuple(stage for stage in DELIVERY_STAGES if self.stage(stage) is not None)

    @property
    def configured(self) -> bool:
        """Whether any stage resolves to commands for this project.

        A selected preset counts, because its stages resolve here like any other
        declaration: the project has said how a run isolates and how a change is
        submitted, and it said so by naming a definition rather than restating
        one. A name that does not resolve is refused before reaching this
        question, so no selection produces the zero-configuration answer.
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

    def _stage_declarations(
        self, stage: str
    ) -> tuple[tuple[ValueOrigin, str, Any, str | None], ...]:
        """Candidate declarations for *stage*, narrowest configuration layer first.

        Each candidate carries the preset it came from, or ``None`` when the layer
        declared the stage itself. The selected preset is the widest candidate, so
        it supplies the stages nobody overrode and never the ones somebody did.
        """
        project_stages = self._stages_node(self._project_entry())
        app_stages = self._stages_node(self._document)
        project_prefix = f"{SECTION_PROJECTS}.{self._project}.{SECTION_WORKFLOW}.{STAGES_KEY}"
        candidates: list[tuple[ValueOrigin, str, Any, str | None]] = [
            (
                ValueOrigin.PROJECT_CONFIG,
                f"{project_prefix}.{stage}",
                project_stages.get(stage) if project_stages is not None else None,
                None,
            ),
            (
                ValueOrigin.APP_CONFIG,
                f"{SECTION_WORKFLOW}.{STAGES_KEY}.{stage}",
                app_stages.get(stage) if app_stages is not None else None,
                None,
            ),
        ]
        selection = self.selected_preset()
        if selection is not None:
            candidates.append(
                (
                    # A bundled definition is the engine's, not any layer's, so it
                    # reports as a bundled default however it was selected. A
                    # user-defined one is a declaration in the app-level document,
                    # so it reports as one, at the path an operator edits.
                    ValueOrigin.BUNDLED_DEFAULT if selection.bundled else ValueOrigin.APP_CONFIG,
                    (
                        selection.declared_at
                        if selection.bundled
                        else f"{SECTION_WORKFLOW}.{WORKFLOW_PRESETS_KEY}"
                        f".{selection.name}.{STAGES_KEY}.{stage}"
                    ),
                    selection.stages.get(stage),
                    selection.name,
                )
            )
        return tuple(candidates)

    def selected_preset(self) -> PresetSelection | None:
        """The preset in force for this project, or ``None`` when none is selected.

        The selection resolves like a stage does: a project's ``preset`` replaces
        the app-wide one rather than merging with it, because two presets in force
        at once would need a rule for which stage came from which.

        Raises ``ConfigValidationError`` naming the selection's path when the name
        is neither a bundled preset nor a user-defined one. A selection that
        resolved to nothing would leave the project running whatever it had
        configured otherwise -- for a project that configured nothing, the
        zero-configuration workflow -- while its configuration says it selected
        one.
        """
        if self._selection is _UNRESOLVED:
            self._selection = self._resolve_selection()
        assert not isinstance(self._selection, _Unresolved)  # narrowed by the line above
        return self._selection

    def _resolve_selection(self) -> PresetSelection | None:
        for origin, path, name in self._selection_declarations():
            if name is None:
                continue
            if not isinstance(name, str) or not name.strip():
                raise ConfigValidationError([ConfigError(path, "expected a preset name")])
            return PresetSelection(
                name=name,
                origin=origin,
                declared_at=path,
                bundled=name in WORKFLOW_PRESETS,
                stages=self._preset_stages(name, path),
            )
        return None

    def _selection_declarations(self) -> tuple[tuple[ValueOrigin, str, Any], ...]:
        project_workflow = self._workflow_node(self._project_entry())
        app_workflow = self._workflow_node(self._document)
        return (
            (
                ValueOrigin.PROJECT_CONFIG,
                f"{SECTION_PROJECTS}.{self._project}.{SECTION_WORKFLOW}.{WORKFLOW_PRESET_KEY}",
                project_workflow.get(WORKFLOW_PRESET_KEY) if project_workflow else None,
            ),
            (
                ValueOrigin.APP_CONFIG,
                f"{SECTION_WORKFLOW}.{WORKFLOW_PRESET_KEY}",
                app_workflow.get(WORKFLOW_PRESET_KEY) if app_workflow else None,
            ),
        )

    def _preset_stages(self, name: str, path: str) -> Mapping[str, Any]:
        """The stages *name* defines, bundled definitions taking precedence.

        Bundled first is what makes a bundled name unshadowable: the write path
        refuses a definition that reuses one, and a document that reached the
        reader without passing it still cannot redirect ``git-pull-request`` at
        commands of its own.

        Resolution goes through :func:`workflow_presets`, so an unknown name is
        refused by the one accessor that knows the bundled set, and no name
        outside it can be reached by adding a definition the engine did not ship.

        A user-defined definition is copied out of the document rather than
        returned live, so both kinds of preset answer the same way. The live node
        was harmless while nothing mutated the result -- ``document()`` re-parses
        per load -- but it made the returned mapping a writable handle on that
        project's configuration, and the difference between the two kinds was
        nowhere stated. The copy is a deep one because the node is unvalidated
        JSON of arbitrary shape, unlike the bundled table's known argv lists.
        """
        if name in WORKFLOW_PRESETS:
            return workflow_presets(name)[STAGES_KEY]
        definition = self._preset_definitions().get(name)
        if isinstance(definition, Mapping):
            stages = definition.get(STAGES_KEY)
            if isinstance(stages, Mapping) and stages:
                return deepcopy(dict(stages))
            raise ConfigValidationError(
                [
                    ConfigError(
                        f"{SECTION_WORKFLOW}.{WORKFLOW_PRESETS_KEY}.{name}.{STAGES_KEY}",
                        "expected an object with at least one stage",
                    )
                ]
            )
        try:
            workflow_presets(name)
        except KeyError as exc:
            defined = tuple(self._preset_definitions())
            detail = str(exc.args[0] if exc.args else exc)
            if defined:
                detail += f"; user-defined presets are {', '.join(sorted(defined))}"
            raise ConfigValidationError([ConfigError(path, detail)]) from exc
        raise AssertionError("unreachable: a bundled name resolves above")  # pragma: no cover

    def _preset_definitions(self) -> Mapping[str, Any]:
        """The user-defined preset definitions, app level only."""
        workflow = self._workflow_node(self._document)
        definitions = workflow.get(WORKFLOW_PRESETS_KEY) if workflow else None
        return definitions if isinstance(definitions, Mapping) else {}

    def _project_entry(self) -> Mapping[str, Any]:
        if self._project is None:
            return {}
        projects = self._document.get(SECTION_PROJECTS)
        if not isinstance(projects, Mapping):
            return {}
        entry = projects.get(self._project)
        return entry if isinstance(entry, Mapping) else {}

    @staticmethod
    def _workflow_node(container: Mapping[str, Any]) -> Mapping[str, Any] | None:
        workflow = container.get(SECTION_WORKFLOW)
        return workflow if isinstance(workflow, Mapping) else None

    @classmethod
    def _stages_node(cls, container: Mapping[str, Any]) -> Mapping[str, Any] | None:
        workflow = cls._workflow_node(container)
        if workflow is None:
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
