"""Property-based test for the autonomy ladder.

**Ladder monotonicity.** Whatever an operator has configured, an enabled level
implies every level below it and resolution never yields a level above the one
that was configured. Scripted cases cover the grid cells someone thought to
write down; the failure this guards against is a grid shape nobody thought of
resolving upward, which is an authority increase no test would attribute to the
resolver afterwards.

The second half of the file carries the same claim across the composition a run
actually passes through -- the workflow ceiling, the delivery authority, the
integration gates, and the prerequisite report -- because a resolver that is
monotone on its own says nothing about whether some combination of the settings
sitting beside it lets a lower rung do something a higher rung cannot.
"""

from __future__ import annotations

from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

from kiro_crew.apps.builtins.spec_engine.engine.autonomy import (
    AUTONOMY_FIELD,
    AutonomyDecision,
    AutonomyLevel,
    AutonomyPolicy,
)
from kiro_crew.apps.builtins.spec_engine.engine.config import (
    AUTONOMY_LEVELS,
    LEAST_TRUSTED_CLASS,
    SPEC_TYPES,
    SUBMITTER_CLASSES,
    WILDCARD_KEY,
    ValueOrigin,
)
from kiro_crew.apps.builtins.spec_engine.engine.delivery.integration import (
    DeliveryAuthority,
    ProtectedBranches,
)
from kiro_crew.apps.builtins.spec_engine.engine.delivery.workflow import cap_autonomy
from kiro_crew.apps.builtins.spec_engine.engine.prerequisites import (
    CAPABILITY_PHASES,
    STAGE_PHASES,
    CheckName,
    Prerequisite,
    PrerequisiteReport,
)

#: Resolution is pure and in-memory, so examples are cheap; this is well above
#: the number of distinct grid shapes the scripted cases cover.
MAX_EXAMPLES = 200

SOURCE = "tracker"

_LEVELS = st.sampled_from([AutonomyLevel(name) for name in AUTONOMY_LEVELS])
_CLASS_KEYS = st.sampled_from(list(SUBMITTER_CLASSES) + [WILDCARD_KEY])
_TYPE_KEYS = st.sampled_from(list(SPEC_TYPES) + [WILDCARD_KEY])

#: An arbitrary policy grid, including the empty one.
_GRIDS = st.dictionaries(
    _CLASS_KEYS,
    st.dictionaries(_TYPE_KEYS, _LEVELS.map(lambda level: level.value), max_size=4),
    max_size=5,
)

_TRIPLES = st.tuples(st.sampled_from(SPEC_TYPES), st.sampled_from(SUBMITTER_CLASSES))


def _policy(grid: dict[str, Any]) -> AutonomyPolicy:
    return AutonomyPolicy.from_document(
        {"sources": {SOURCE: {"poll": ["watch"], AUTONOMY_FIELD: grid}}}
    )


def _declared(grid: dict[str, dict[str, str]]) -> set[AutonomyLevel]:
    return {AutonomyLevel(level) for by_type in grid.values() for level in by_type.values()}


@settings(max_examples=MAX_EXAMPLES)
@given(_GRIDS, _TRIPLES)
def test_resolution_never_exceeds_what_was_configured(
    grid: dict[str, Any], triple: tuple[str, str]
):
    spec_type, submitter_class = triple
    decision = _policy(grid).resolve(
        source=SOURCE, spec_type=spec_type, submitter_class=submitter_class
    )
    declared = _declared(grid)
    if decision.is_configured:
        # The resolved level is one an operator wrote, not one derived from it.
        assert decision.level in declared
        assert decision.level.rank <= max(level.rank for level in declared)
    else:
        # Nothing matched, so the safe default stands and execution stays human.
        assert decision.level is AutonomyLevel.AUTHORING
        assert decision.execution_is_human_reserved


@settings(max_examples=MAX_EXAMPLES)
@given(_GRIDS, _TRIPLES)
def test_the_resolved_level_permits_exactly_itself_and_below(
    grid: dict[str, Any], triple: tuple[str, str]
):
    spec_type, submitter_class = triple
    decision = _policy(grid).resolve(
        source=SOURCE, spec_type=spec_type, submitter_class=submitter_class
    )
    for level in AutonomyLevel:
        assert decision.permits(level) == (level.rank <= decision.level.rank)
    assert decision.level.implies() == tuple(
        level for level in AutonomyLevel if level.rank <= decision.level.rank
    )
    assert decision.execution_is_human_reserved == (decision.level is AutonomyLevel.AUTHORING)


@settings(max_examples=MAX_EXAMPLES)
@given(_LEVELS, _TRIPLES)
def test_a_blanket_grant_resolves_to_exactly_that_level_for_every_triple(
    level: AutonomyLevel, triple: tuple[str, str]
):
    spec_type, submitter_class = triple
    decision = _policy({WILDCARD_KEY: {WILDCARD_KEY: level.value}}).resolve(
        source=SOURCE, spec_type=spec_type, submitter_class=submitter_class
    )
    assert decision.level is level


@settings(max_examples=MAX_EXAMPLES)
@given(_GRIDS, _TRIPLES, _LEVELS, _LEVELS)
def test_raising_a_cell_never_lowers_what_is_permitted(
    grid: dict[str, Any],
    triple: tuple[str, str],
    lower: AutonomyLevel,
    higher: AutonomyLevel,
):
    spec_type, submitter_class = triple
    if lower.rank > higher.rank:
        lower, higher = higher, lower
    quiet = dict(grid)
    quiet[submitter_class] = {spec_type: lower.value}
    loud = dict(grid)
    loud[submitter_class] = {spec_type: higher.value}
    resolved_quiet = _policy(quiet).resolve(
        source=SOURCE, spec_type=spec_type, submitter_class=submitter_class
    )
    resolved_loud = _policy(loud).resolve(
        source=SOURCE, spec_type=spec_type, submitter_class=submitter_class
    )
    # The exact cell wins in both documents, so the only difference is the level
    # the operator wrote: a raised cell resolves up, never down.
    assert resolved_quiet.level is lower
    assert resolved_loud.level is higher
    assert resolved_loud.level.rank >= resolved_quiet.level.rank


# --- The composed permission surface ---------------------------------------
#
# The properties above constrain the resolver. They are not the whole ladder: by
# the time a run acts, the resolved level has passed through the workflow
# ceiling, and the answer to "may this run do X" comes from DeliveryAuthority,
# the integration gates, and the prerequisite report rather than from
# AutonomyDecision.permits. Monotonicity has to hold across that composition,
# for every combination of the settings sitting beside the ladder -- otherwise
# some combination lets a lower rung do something a higher rung cannot, which is
# a permission that appears when authority is reduced and would be attributed to
# the reduction by nobody.

#: The delegable work each level reaches, as the engine's own tables declare it.
#: Read from the tables rather than restated, because the claim is about the
#: shape of the permitted SET at each rung, not about which rung a stage sits on.
_GATED_ACTIONS: tuple[tuple[str, AutonomyLevel], ...] = tuple(STAGE_PHASES.items()) + tuple(
    CAPABILITY_PHASES.items()
)

#: Settings that sit beside the ladder on the same decisions. Every combination
#: is generated, so "no combination" is a claim the generator can actually refute.
_POSTURES = st.tuples(st.booleans(), st.booleans(), st.booleans(), st.booleans())

_TARGETS = st.sampled_from(["", "  ", "main", "release/1.0"])


def _authority(
    level: AutonomyLevel,
    *,
    workflow_configured: bool,
    auto_integrate: bool,
    protected: ProtectedBranches,
) -> DeliveryAuthority:
    """A delivery authority at *level*, built without touching configuration.

    Constructed directly rather than through ``resolve_authority`` so the only
    thing varying between the two sides of a comparison is the rung: a store
    would reintroduce configuration as a hidden second input.
    """
    decision = AutonomyDecision(
        level=level,
        source=SOURCE,
        spec_type=SPEC_TYPES[0],
        submitter_class=LEAST_TRUSTED_CLASS,
        declared_at=f"sources.{SOURCE}.{AUTONOMY_FIELD}.{LEAST_TRUSTED_CLASS}",
    )
    return DeliveryAuthority(
        decision=decision,
        level=AutonomyLevel(cap_autonomy(level.value, workflow_configured=workflow_configured)),
        workflow_configured=workflow_configured,
        auto_integrate=auto_integrate,
        auto_integrate_declared_at="delivery.auto_integrate",
        protected=protected,
    )


def _permitted(
    level: AutonomyLevel,
    *,
    workflow_configured: bool,
    auto_integrate: bool,
    verified: bool,
    target: str,
    delivered: bool,
) -> set[str]:
    """Everything a run at *level* is allowed to do, as a set of opaque tokens.

    A set rather than a tuple of booleans: the property is that the permitted set
    never shrinks as the rung rises, and a set states that directly.
    """
    authority = _authority(
        level,
        workflow_configured=workflow_configured,
        auto_integrate=auto_integrate,
        protected=ProtectedBranches(
            branches=frozenset({"main"}), origin=ValueOrigin.PROJECT_CONFIG
        ),
    )
    allowed = {f"level:{rung.value}" for rung in AutonomyLevel if authority.permits(rung)}
    allowed |= {name for name, needed in _GATED_ACTIONS if authority.permits(needed)}
    if authority.isolates_before_execution:
        allowed.add("isolate-before-execution")
    if authority.integration(verified=verified, target=target, delivered=delivered).permitted:
        allowed.add("integrate")
    return allowed


@settings(max_examples=MAX_EXAMPLES)
@given(_LEVELS, _LEVELS, _POSTURES, _TARGETS)
def test_no_combination_of_settings_lets_a_lower_rung_outdo_a_higher_one(
    first: AutonomyLevel,
    second: AutonomyLevel,
    posture: tuple[bool, bool, bool, bool],
    target: str,
) -> None:
    lower, higher = sorted((first, second), key=lambda level: level.rank)
    workflow_configured, auto_integrate, verified, delivered = posture

    below = _permitted(
        lower,
        workflow_configured=workflow_configured,
        auto_integrate=auto_integrate,
        verified=verified,
        target=target,
        delivered=delivered,
    )
    above = _permitted(
        higher,
        workflow_configured=workflow_configured,
        auto_integrate=auto_integrate,
        verified=verified,
        target=target,
        delivered=delivered,
    )

    # Everything the lower rung allows, the higher one allows too. Raising
    # autonomy adds permissions; it never trades one away.
    assert (
        below <= above
    ), f"{lower.value} allows {sorted(below - above)} that {higher.value} does not"
    if lower is higher:
        assert below == above


@settings(max_examples=MAX_EXAMPLES)
@given(_LEVELS, _POSTURES, _TARGETS)
def test_each_adjacent_step_up_the_ladder_only_adds(
    level: AutonomyLevel,
    posture: tuple[bool, bool, bool, bool],
    target: str,
) -> None:
    """The pairwise claim, on the pairs a whole-ladder sample can step over.

    Adjacent rungs are where a ceiling or an off-by-one in a rank table shows
    up: two rungs that both cap to the same level are indistinguishable, and a
    comparison of distant rungs can hide that.
    """
    ladder = sorted(AutonomyLevel, key=lambda item: item.rank)
    workflow_configured, auto_integrate, verified, delivered = posture
    sets = [
        _permitted(
            rung,
            workflow_configured=workflow_configured,
            auto_integrate=auto_integrate,
            verified=verified,
            target=target,
            delivered=delivered,
        )
        for rung in ladder
    ]

    for lower, upper in zip(sets, sets[1:]):
        assert lower <= upper
    # The floor allows the least and the ceiling the most, so the ladder is not
    # merely locally consistent while being globally flat in the wrong place.
    assert sets[0] <= sets[-1]
    assert level.permits(AutonomyLevel.AUTHORING)


@settings(max_examples=MAX_EXAMPLES)
@given(_LEVELS, st.booleans())
def test_an_unmet_prerequisite_report_only_grows_up_the_ladder(level: AutonomyLevel, met: bool):
    """A higher rung reaches at least the blocking checks a lower rung reached.

    ``unmet_through`` is what a refusal is built from, so a rung that reached
    fewer checks than the one below it would refuse for fewer reasons while
    holding more authority.
    """
    checks = tuple(
        Prerequisite(
            check=CheckName.PROGRAMS,
            phase=phase,
            met=met,
            missing="" if met else f"a program for {phase.value}",
            action="" if met else "configure it",
        )
        for phase in AutonomyLevel
    )
    report = PrerequisiteReport(checks=checks)
    ladder = sorted(AutonomyLevel, key=lambda item: item.rank)

    reached = [{check.phase for check in report.unmet_through(rung)} for rung in ladder]
    for lower, upper in zip(reached, reached[1:]):
        assert lower <= upper
    if met:
        assert reached[-1] == set()
    else:
        # Every phase the rung permits is reported, and no phase above it.
        for rung, found in zip(ladder, reached):
            assert found == {phase for phase in AutonomyLevel if rung.permits(phase)}
    assert report.unmet_through(level) == tuple(
        sorted(
            (check for check in report.unmet if level.permits(check.phase)),
            key=lambda check: check.phase.rank,
        )
    )
