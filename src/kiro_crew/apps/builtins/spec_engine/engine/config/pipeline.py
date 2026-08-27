"""Pipeline stages: which part of the pipeline a setting or capability governs.

The configuration surface is organised around the pipeline an operator runs --
where work comes from, how documents are authored, how tasks execute, where
results go -- rather than around the shape of the configuration document. The
mapping lives here so a surface DERIVES it from the engine instead of keeping a
list of its own: a setting or capability the engine gains later is placed by this
table rather than by an edit on the far side of the wire.

**These tables are a presentation projection. They are not a fourth phase
vocabulary and they are not the autonomy ladder.** The engine already carries
three phase-shaped mappings, and not one of them answers the question this
module answers:

* ``prerequisites.CAPABILITY_PHASES`` maps a capability onto ``AutonomyLevel``:
  how much AUTHORITY a run must hold before that capability is reached.
* ``prerequisites.STAGE_PHASES`` maps a DELIVERY stage -- isolate, submit,
  verify, publish, teardown -- onto the same ladder.
* ``runs.PHASE_TIMEOUT_SETTINGS`` maps four run states onto the four setting
  keys that bound them.

Conflating any of those with a pipeline stage would put an information
architecture in charge of authority. The ladder has no intake rung at all, and
:data:`PIPELINE_STAGE_EXECUTION` here means "the settings that govern executing
tasks", never "the execution rung is granted". Nothing in this module is read
when a gate, prerequisite, autonomy or budget decision is made, and nothing here
may become an input to one.

The grouping rides on data that already exists -- ``Setting.group``, the leading
segment of a dotted key -- so a new setting is declared in one place and
re-declared in none.
"""

from __future__ import annotations

from typing import Mapping

from .schema import DELEGABLE_CAPABILITIES
from .settings import SETTING_GROUP_ORDER

#: Where work comes from: the watch sources polled for items to work on.
PIPELINE_STAGE_INTAKE = "intake"

#: How specification documents are written, analyzed, and validated.
PIPELINE_STAGE_AUTHORING = "authoring"

#: How tasks execute: concurrency, retry and timeout limits, spend ceilings.
PIPELINE_STAGE_EXECUTION = "execution"

#: Where results go: the delivery workflow and the notifications about it.
PIPELINE_STAGE_DELIVERY = "delivery"

#: Everything that is not a step of the pipeline, plus anything unmapped. A
#: separately reachable area rather than a hidden one, so a setting the engine
#: adds is visible and editable before anyone places it.
PIPELINE_STAGE_ADVANCED = "advanced"

#: The stages, in the order a surface presents them: the pipeline's own order,
#: with the advanced area last because it is reached rather than passed through.
PIPELINE_STAGES: tuple[str, ...] = (
    PIPELINE_STAGE_INTAKE,
    PIPELINE_STAGE_AUTHORING,
    PIPELINE_STAGE_EXECUTION,
    PIPELINE_STAGE_DELIVERY,
    PIPELINE_STAGE_ADVANCED,
)

#: Which pipeline stage each setting group governs. Keyed by ``Setting.group``,
#: the leading segment of a dotted key, which is the segment the write door
#: already accepts as a container -- so this table needs no key of its own and
#: cannot name a group the registry does not have.
#:
#: ``timeouts`` and ``budget`` sit with execution because every bound they carry
#: is a bound on running a task; a timeout is not an "advanced" knob just because
#: it is numeric. ``telemetry`` is the one group that governs no step of the
#: pipeline, so it is the one group in the advanced area.
SETTING_GROUP_STAGES: Mapping[str, str] = {
    "watch": PIPELINE_STAGE_INTAKE,
    "concurrency": PIPELINE_STAGE_EXECUTION,
    "limits": PIPELINE_STAGE_EXECUTION,
    "timeouts": PIPELINE_STAGE_EXECUTION,
    "budget": PIPELINE_STAGE_EXECUTION,
    "delivery": PIPELINE_STAGE_DELIVERY,
    "notify": PIPELINE_STAGE_DELIVERY,
    "telemetry": PIPELINE_STAGE_ADVANCED,
}

#: Which pipeline stage each delegable capability belongs to. A separate table
#: from :data:`SETTING_GROUP_STAGES` rather than a derivation from it, because a
#: capability is not named by a setting group and the authoring stage holds no
#: setting group at all -- authoring is configured entirely by which providers
#: write, analyze and validate documents.
#:
#: ``model_catalog`` is advanced rather than authoring: it is a lookup every
#: stage reads rather than a step any one of them performs, so placing it in a
#: step would claim it affects only that step.
CAPABILITY_STAGES: Mapping[str, str] = {
    "watch_sources": PIPELINE_STAGE_INTAKE,
    "analysis": PIPELINE_STAGE_AUTHORING,
    "authoring": PIPELINE_STAGE_AUTHORING,
    "validation_rules": PIPELINE_STAGE_AUTHORING,
    "review": PIPELINE_STAGE_EXECUTION,
    "implementation": PIPELINE_STAGE_EXECUTION,
    "model_catalog": PIPELINE_STAGE_ADVANCED,
}


def setting_group_stage(group: str) -> str:
    """The pipeline stage *group* is presented under.

    An unmapped group resolves to :data:`PIPELINE_STAGE_ADVANCED` rather than
    raising or resolving to nothing. Both alternatives are worse than a default
    here, and for reasons specific to a projection: raising would take the whole
    vocabulary read down over one unplaced group, leaving an operator a surface
    that cannot describe even the fields it does place, and resolving to nothing
    would hide a setting the write door still enforces -- a setting present in
    the document, in force on every run, and unreachable from the surface meant
    to edit it.

    The engine's phase resolvers make the same call in both directions and say
    why each way round: ``prerequisites.stage_phase`` RAISES, because a delivery
    stage silently checked at the wrong rung is a check that never blocks the run
    needing it, while ``prerequisites._capability_phase`` defaults DOWN to the
    lowest rung, so an unplaced capability is checked by every run rather than by
    the most autonomous ones. Both choose the answer that cannot quietly grant
    something. A pipeline stage grants nothing, so the safe default is the one
    that keeps the unplaced thing visible.
    """
    return SETTING_GROUP_STAGES.get(group, PIPELINE_STAGE_ADVANCED)


def capability_stage(capability: str) -> str:
    """The pipeline stage *capability* is presented under.

    Unmapped resolves to :data:`PIPELINE_STAGE_ADVANCED`, for the reasons given
    on :func:`setting_group_stage`. A capability the schema declares delegable is
    bindable whether or not this table places it, so vanishing from the surface
    would leave it configurable only by hand.
    """
    return CAPABILITY_STAGES.get(capability, PIPELINE_STAGE_ADVANCED)


def stage_setting_groups(stage: str) -> tuple[str, ...]:
    """The setting groups *stage* presents, in setting-registry order.

    Order comes from :data:`~.settings.SETTING_GROUP_ORDER` -- the registry's own
    declaration order -- and never from ``SETTING_GROUPS``, which is a frozenset
    whose iteration order would reorder a surface's rows between two reads while
    nothing had changed.

    An unrecognised *stage* returns an empty tuple rather than raising: the
    stages are a closed vocabulary this module owns, so the only caller that can
    ask for an unknown one is asking about a stage that holds nothing.
    """
    return tuple(group for group in SETTING_GROUP_ORDER if setting_group_stage(group) == stage)


def stage_capabilities(stage: str) -> tuple[str, ...]:
    """The delegable capabilities *stage* presents, in schema declaration order."""
    return tuple(
        capability for capability in DELEGABLE_CAPABILITIES if capability_stage(capability) == stage
    )
