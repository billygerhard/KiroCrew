"""Property-based test for the autonomy ladder.

**Ladder monotonicity.** Whatever an operator has configured, an enabled level
implies every level below it and resolution never yields a level above the one
that was configured. Scripted cases cover the grid cells someone thought to
write down; the failure this guards against is a grid shape nobody thought of
resolving upward, which is an authority increase no test would attribute to the
resolver afterwards.
"""

from __future__ import annotations

from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

from kiro_crew.apps.builtins.spec_engine.engine.autonomy import (
    AUTONOMY_FIELD,
    AutonomyLevel,
    AutonomyPolicy,
)
from kiro_crew.apps.builtins.spec_engine.engine.config import (
    AUTONOMY_LEVELS,
    SPEC_TYPES,
    SUBMITTER_CLASSES,
    WILDCARD_KEY,
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
