"""Per-stage command origin, rendered for the configuration surface.

An operator reading a delivery workflow sees the commands a stage runs but not
which layer produced them, and the two facts call for different edits: a stage
still holding preset commands changes by selecting a different preset (or by
overriding that stage), while an overridden one changes where the override is
declared. A reviewer reading a run's audit trail has the mirror-image question --
whether the recorded preset name describes what actually executed, or only the
stages nobody overrode.

Everything here reads what :mod:`.workflow` already resolved:
:class:`~.workflow.StageCommands` carries the declaring layer, the dotted path,
and the preset the commands came from, and :class:`~.workflow.PresetSelection`
carries whether that preset's definition is the engine's own. Nothing is
re-derived by comparing a project's stages against the bundled table -- that
would be a second precedence implementation, and the display would confidently
name the wrong layer on the first day the two disagreed.

Two distinctions the display is required to keep:

* **A stage nobody defines is not a stage from the preset.** It skips at
  execution, so rendering it as preset-supplied (or omitting it) would tell an
  operator a stage runs when it does not. Every delivery stage gets a row, and an
  undefined one says so.
* **A user-defined preset is not a bundled one.** A bundled name means
  engine-authored commands, which is why bundled names are reserved -- a
  definition reusing one is refused at the write path and loses to the bundled
  table at read. Flattening both to "preset" here would give back the ambiguity
  that reservation exists to prevent.

A preset name is document-authored, identifier-shaped input, so it is rendered
through :func:`~..capabilities.contracts.sanitized`. Preset definitions carry no
prose field, so there is nothing here for the prose display path.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from ..capabilities.contracts import sanitized
from ..config import DELIVERY_STAGES, ValueOrigin
from .workflow import DeliveryWorkflow, PresetSelection, StageCommands

#: Cap on a rendered preset name. Names are short by construction; the ceiling is
#: here so a hand-edited document cannot set the width of a surface's row.
MAX_PRESET_NAME_CHARS = 64


class StageSource(str, Enum):
    """Where one stage's commands came from, as a surface shows it.

    Deliberately not :class:`~..config.effective.ValueOrigin`: that names the
    configuration layer, and two of these answers are not layers. A preset's
    definition being the engine's own is a trust distinction rather than a
    location, and an unconfigured stage has no declaration to locate.
    """

    #: The selected preset, defined by the engine and read-only.
    BUNDLED_PRESET = "bundled_preset"
    #: The selected preset, defined in the configuration document.
    USER_PRESET = "user_preset"
    #: Declared app-wide, which replaces the selected preset's stage.
    APP_OVERRIDE = "app_override"
    #: Declared on this project, the narrowest layer.
    PROJECT_OVERRIDE = "project_override"
    #: Neither the preset nor any layer defines it, so the stage skips.
    UNCONFIGURED = "unconfigured"

    @property
    def from_preset(self) -> bool:
        """Whether the selected preset supplied the commands."""
        return self in (StageSource.BUNDLED_PRESET, StageSource.USER_PRESET)

    @property
    def bundled(self) -> bool:
        """Whether the definition is the engine's own rather than the document's."""
        return self is StageSource.BUNDLED_PRESET


#: The layer a non-preset declaration reports as. A layer absent from this map is
#: refused rather than displayed as the nearest neighbour: a resolution order that
#: grew a layer needs a display answer of its own, and guessing one would label
#: someone else's declaration as this project's own override.
_SOURCE_FOR_ORIGIN: dict[ValueOrigin, StageSource] = {
    ValueOrigin.PROJECT_CONFIG: StageSource.PROJECT_OVERRIDE,
    ValueOrigin.APP_CONFIG: StageSource.APP_OVERRIDE,
}

_SUMMARIES: dict[StageSource, str] = {
    StageSource.APP_OVERRIDE: "overridden app-wide",
    StageSource.PROJECT_OVERRIDE: "overridden by this project",
    StageSource.UNCONFIGURED: "not configured, so this stage is skipped",
}


@dataclass(frozen=True)
class StageOrigin:
    """One delivery stage, and which layer's commands it runs."""

    stage: str
    source: StageSource
    #: Name of the preset the commands came from, empty when no preset supplied
    #: them. Sanitized: a user-defined name is document-authored.
    preset: str = ""
    #: Dotted path of the declaration, empty for an unconfigured stage.
    declared_at: str = ""
    #: How many commands the stage runs, zero when unconfigured.
    commands: int = 0

    def __post_init__(self) -> None:
        # Sanitized once, at construction, rather than at each display site: the
        # name reaches a label, a payload, and a describe() line, and a rendering
        # done per site is only as good as the newest site's memory of it. The
        # path is sanitized for the same reason -- it embeds a user-defined name.
        object.__setattr__(self, "preset", sanitized(self.preset, limit=MAX_PRESET_NAME_CHARS))
        object.__setattr__(self, "declared_at", sanitized(self.declared_at))
        if self.source.from_preset and not self.preset.strip():
            raise ValueError(f"stage {self.stage} claims a preset without naming it")
        if not self.source.from_preset and self.preset:
            raise ValueError(f"stage {self.stage} names a preset it did not come from")

    @property
    def skipped(self) -> bool:
        """Whether this stage runs nothing, because nothing defines it."""
        return self.source is StageSource.UNCONFIGURED

    def describe(self) -> str:
        """One line for a human, naming the layer and where to edit it."""
        if self.source.from_preset:
            kind = "bundled" if self.source.bundled else "user-defined"
            summary = f"from {kind} preset {self.preset!r}"
        else:
            summary = _SUMMARIES[self.source]
        if self.skipped:
            return f"{self.stage}: {summary}"
        return f"{self.stage}: {summary} ({self.commands} command(s), at {self.declared_at})"

    def to_json_object(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "source": self.source.value,
            "from_preset": self.source.from_preset,
            "bundled": self.source.bundled,
            "preset": self.preset,
            "declared_at": self.declared_at,
            "commands": self.commands,
            "skipped": self.skipped,
            "summary": self.describe(),
        }


def stage_origins(workflow: DeliveryWorkflow) -> tuple[StageOrigin, ...]:
    """One row per delivery stage, in the schema's stage order.

    Every stage appears, including the ones no layer defines: those skip at
    execution, and a surface that listed only what resolved would leave an
    operator to infer the difference between "runs the preset's commands" and
    "runs nothing" from a stage's absence.
    """
    selection = workflow.selected_preset()
    return tuple(_origin(stage, workflow.stage(stage), selection) for stage in DELIVERY_STAGES)


def _origin(
    stage: str, commands: StageCommands | None, selection: PresetSelection | None
) -> StageOrigin:
    if commands is None:
        return StageOrigin(stage=stage, source=StageSource.UNCONFIGURED)
    if commands.from_preset:
        if selection is None:  # pragma: no cover - a preset stage implies a selection
            raise ValueError(f"stage {stage} came from a preset with no selection in force")
        # ``selection.bundled`` is the one field that separates engine-authored
        # commands from document-authored ones. Deriving it here from the name
        # would be a second reading of the reserved-name rule.
        source = StageSource.BUNDLED_PRESET if selection.bundled else StageSource.USER_PRESET
        preset = commands.preset or selection.name
    else:
        source = _source_for(stage, commands.origin)
        preset = ""
    return StageOrigin(
        stage=stage,
        source=source,
        preset=preset,
        declared_at=commands.declared_at,
        commands=len(commands.commands),
    )


def _source_for(stage: str, origin: ValueOrigin) -> StageSource:
    try:
        return _SOURCE_FOR_ORIGIN[origin]
    except KeyError as exc:
        raise ValueError(
            f"stage {stage} was declared by {origin.value}, which has no display source"
        ) from exc
