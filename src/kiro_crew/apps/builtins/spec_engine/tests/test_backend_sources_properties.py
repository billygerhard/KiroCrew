"""Properties behind the sources read: cell origins, and the isolation of an edit.

Two claims, both about surfaces an operator makes a trust decision from.

**Origin classification is total and faithful.** For any grid the schema's
vocabulary can express and any (submitter class, spec type) pair, the route's
resolved cell carries the level the real ``AutonomyPolicy`` returns, and its
origin agrees with the declaring path: ``default`` exactly when nothing was
declared, ``exact`` exactly when the pair's own cell declared it, ``wildcard``
otherwise — with "otherwise" shown to be a broader cell rather than an unread
path. Scripted cases cover the grid shapes somebody thought to write down; what
this guards is a shape nobody thought of, where an operator would be told they
had written a rule they had not and would make the next edit on that belief.

**A minimal cell patch touches only its own cells.** The UI writes a grid change
as the nested patch ``sources.<name>.autonomy.<class>.<type>``, through the
engine's existing config door. This drives the store's real ``_merge`` — imported
private on purpose, because the claim is about what THAT function does to a
document, and a re-implementation here would prove a merge nobody runs — and
asserts that every other path in the document survives the merge unchanged, in
both directions: nothing else altered, nothing else added, nothing else dropped.

**A projected source preset carries only the bundled argv.** The form-vocabulary
read hands a surface the entry a new Watch_Source is composed from, and its
``poll`` is argv the engine will execute. So for every bundled preset the
projected argv is byte-equal to the table's own, and no edit a surface makes to a
projected copy can change what the next read supplies — the deep copy is what
stands between one operator's edit and every later projection, and the write door
validates argv shape rather than the program it names.
"""

from __future__ import annotations

from typing import Any, Mapping

from hypothesis import given, settings
from hypothesis import strategies as st

from kiro_crew.apps.builtins.spec_engine.backend import routes
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
from kiro_crew.apps.builtins.spec_engine.engine.config.schema import SECTION_SOURCES
from kiro_crew.apps.builtins.spec_engine.engine.config.store import _merge
from kiro_crew.apps.builtins.spec_engine.engine.watch.sources import (
    POLL_KEY,
    WATCH_SOURCE_PRESET_HOSTS,
    WATCH_SOURCE_PRESETS,
)

#: Both properties are pure and in-memory, so examples are cheap. Well above the
#: number of distinct grid shapes the scripted route tests reach.
MAX_EXAMPLES = 200

SOURCE = "tracker"

_CLASS_KEYS = st.sampled_from(list(SUBMITTER_CLASSES) + [WILDCARD_KEY])
_TYPE_KEYS = st.sampled_from(list(SPEC_TYPES) + [WILDCARD_KEY])
_LEVELS = st.sampled_from(list(AUTONOMY_LEVELS))

#: An arbitrary policy grid over the schema's own vocabulary, including the empty
#: one and rows that declare nothing.
_GRIDS = st.dictionaries(
    _CLASS_KEYS,
    st.dictionaries(_TYPE_KEYS, _LEVELS, max_size=4),
    max_size=5,
)


def _document(grid: Mapping[str, Any], source: str = SOURCE) -> dict[str, Any]:
    return {"version": 1, "sources": {source: {"poll": ["watch"], AUTONOMY_FIELD: grid}}}


def _cell_path(source: str, submitter_class: str, spec_type: str) -> str:
    return f"{SECTION_SOURCES}.{source}.{AUTONOMY_FIELD}.{submitter_class}.{spec_type}"


# --- origin classification ---------------------------------------------------


@settings(max_examples=MAX_EXAMPLES)
@given(_GRIDS)
def test_every_cell_carries_the_resolvers_own_level_and_an_agreeing_origin(
    grid: dict[str, Any],
) -> None:
    """Totality and faithfulness together, over the whole matrix per example."""
    policy = AutonomyPolicy.from_document(_document(grid))
    matrix = routes._source_grid(policy, SOURCE)

    assert set(matrix) == set(SUBMITTER_CLASSES)
    for submitter_class in SUBMITTER_CLASSES:
        assert set(matrix[submitter_class]) == set(SPEC_TYPES)
        for spec_type in SPEC_TYPES:
            cell = matrix[submitter_class][spec_type]
            decision = policy.resolve(
                source=SOURCE, spec_type=spec_type, submitter_class=submitter_class
            )
            # Faithful to the resolver the GATES read, not to a second walk of the
            # grid: a view that resolved differently would be read as the policy.
            assert cell["level"] == decision.level.value
            assert cell["declared_at"] == decision.declared_at
            assert cell["policy_covers_gates"] == decision.permits(AutonomyLevel.EXECUTION)

            # Total: one of exactly three answers, always.
            origin = cell["origin"]
            assert origin in {
                routes.ORIGIN_EXACT,
                routes.ORIGIN_WILDCARD,
                routes.ORIGIN_DEFAULT,
            }

            own_cell = _cell_path(SOURCE, submitter_class, spec_type)
            if origin == routes.ORIGIN_DEFAULT:
                assert cell["declared_at"] == ""
            elif origin == routes.ORIGIN_EXACT:
                assert cell["declared_at"] == own_cell
            else:
                # "Otherwise" is shown to be a BROADER declaration rather than an
                # unread path: the declaring cell is one of the wildcard
                # candidates, so an edit that narrows it is offering the operator a
                # real choice.
                assert cell["declared_at"] != own_cell
                assert cell["declared_at"] in {
                    _cell_path(SOURCE, submitter_class, WILDCARD_KEY),
                    _cell_path(SOURCE, WILDCARD_KEY, spec_type),
                    _cell_path(SOURCE, WILDCARD_KEY, WILDCARD_KEY),
                }


@settings(max_examples=MAX_EXAMPLES)
@given(_GRIDS, st.sampled_from(SUBMITTER_CLASSES), st.sampled_from(SPEC_TYPES))
def test_an_exact_origin_means_the_operator_wrote_that_cell_and_nothing_broader(
    grid: dict[str, Any], submitter_class: str, spec_type: str
) -> None:
    """The other direction of the same agreement, read off the stored grid.

    Faithfulness above is asserted against ``declared_at``; this asserts against
    the DOCUMENT, so a resolver and a classifier that agreed with each other while
    both misreading the grid would still be caught.
    """
    policy = AutonomyPolicy.from_document(_document(grid))
    cell = routes._source_grid(policy, SOURCE)[submitter_class][spec_type]
    stored_exactly = spec_type in grid.get(submitter_class, {})
    assert (cell["origin"] == routes.ORIGIN_EXACT) is stored_exactly
    if cell["origin"] == routes.ORIGIN_EXACT:
        assert cell["level"] == grid[submitter_class][spec_type]
    if cell["origin"] == routes.ORIGIN_DEFAULT:
        # Nothing broader answered either, which is what makes the cell's
        # "waits for a human" wording true rather than merely unconfigured-looking.
        assert cell["level"] == AutonomyLevel.AUTHORING.value
        assert cell["policy_covers_gates"] is False


# --- a grid patch touches only its own cells ---------------------------------


#: Key names for the arbitrary content a document carries beside the grid. No
#: dots: a dotted key would make a document node and a dotted config path
#: disagree about where a setting lives, which is a separate hazard the config
#: suite already pins and would only blur this property's claim.
_KEYS = st.text(
    alphabet=st.characters(blacklist_characters=".", blacklist_categories=("Cs", "Cc")),
    min_size=1,
    max_size=6,
)

_SCALARS = st.one_of(
    st.booleans(),
    st.integers(min_value=-1000, max_value=1000),
    st.text(max_size=8),
    st.lists(st.text(max_size=4), max_size=3),
)

#: Arbitrary nested settings content, two levels deep, which is enough to carry a
#: sibling of the edited leaf at every depth the patch descends through.
_CONTENT = st.dictionaries(
    _KEYS,
    st.one_of(_SCALARS, st.dictionaries(_KEYS, _SCALARS, max_size=3)),
    max_size=3,
)

_SOURCE_NAMES = st.text(
    alphabet=st.characters(blacklist_characters=".", blacklist_categories=("Cs", "Cc")),
    min_size=1,
    max_size=5,
)

#: Content for a source entry BESIDE its grid. The two fields the property is
#: about are stripped rather than generated over: an arbitrary value at
#: ``autonomy`` would be a malformed grid, and this property is about what the
#: merge does to a document the resolver can read — malformed grids are the
#: route's refusal arm, tested there.
_SOURCE_CONTENT = _CONTENT.map(
    lambda content: {
        key: value for key, value in content.items() if key not in {"poll", AUTONOMY_FIELD}
    }
)


@st.composite
def _documents_and_edits(
    draw: st.DrawFn,
) -> tuple[dict[str, Any], tuple[tuple[str, str, str], ...]]:
    """A document holding several sources, and cells to edit on some of them."""
    names = draw(st.lists(_SOURCE_NAMES, min_size=1, max_size=3, unique=True))
    sources: dict[str, Any] = {}
    for name in names:
        entry: dict[str, Any] = {**draw(_SOURCE_CONTENT), "poll": ["watch"]}
        if draw(st.booleans()):
            entry[AUTONOMY_FIELD] = draw(_GRIDS)
        sources[name] = entry
    document: dict[str, Any] = {"version": 1, **draw(_CONTENT), SECTION_SOURCES: sources}
    edits = draw(
        st.lists(
            st.tuples(st.sampled_from(names), _CLASS_KEYS, _TYPE_KEYS),
            min_size=1,
            max_size=4,
            unique=True,
        )
    )
    return document, tuple(edits)


def _grid_patch(edits: tuple[tuple[str, str, str], ...], level: str) -> dict[str, Any]:
    """The minimal nested patch a cell edit is written as.

    The shape the UI's patch builder produces, spelled here from the same three
    coordinates: the point of the property is that this shape — and nothing about
    the client that assembled it — is what bounds the write.
    """
    patch: dict[str, Any] = {SECTION_SOURCES: {}}
    for source, submitter_class, spec_type in edits:
        row = patch[SECTION_SOURCES].setdefault(source, {}).setdefault(AUTONOMY_FIELD, {})
        row.setdefault(submitter_class, {})[spec_type] = level
    return patch


def _leaves(value: Any, prefix: tuple[str, ...] = ()) -> dict[tuple[str, ...], Any]:
    """Every scalar leaf in *value*, keyed by its path.

    Segment tuples rather than dotted strings, so a key holding a separator cannot
    make two different nodes compare equal.
    """
    if isinstance(value, Mapping):
        leaves: dict[tuple[str, ...], Any] = {}
        for key, child in value.items():
            leaves.update(_leaves(child, prefix + (str(key),)))
        return leaves
    return {prefix: value}


@settings(max_examples=MAX_EXAMPLES)
@given(_documents_and_edits(), _LEVELS)
def test_merging_a_cell_patch_leaves_every_other_path_identical(
    case: tuple[dict[str, Any], tuple[tuple[str, str, str], ...]], level: str
) -> None:
    document, edits = case
    merged = _merge(document, _grid_patch(edits, level))

    edited = {
        (SECTION_SOURCES, source, AUTONOMY_FIELD, submitter_class, spec_type)
        for source, submitter_class, spec_type in edits
    }
    before = _leaves(document)
    after = _leaves(merged)

    # The cells asked for, and only at the level asked for.
    for path in edited:
        assert after[path] == level

    # Everything else, in both directions: nothing altered, nothing added, nothing
    # dropped. Tightening one source's policy must not be able to loosen another's,
    # and the merge is what makes that true by construction rather than by client
    # care.
    assert {path: value for path, value in after.items() if path not in edited} == {
        path: value for path, value in before.items() if path not in edited
    }


@settings(max_examples=MAX_EXAMPLES)
@given(_documents_and_edits(), _LEVELS)
def test_a_patched_document_resolves_the_edited_cells_and_no_others_differently(
    case: tuple[dict[str, Any], tuple[tuple[str, str, str], ...]], level: str
) -> None:
    """Isolation as an operator experiences it: resolved cells, not stored paths.

    Byte-identity elsewhere in the document does not by itself mean every other
    RESOLVED cell is unchanged — a wildcard write answers pairs nobody named. So
    this reads the matrices of every source through the real resolver before and
    after, and requires that a pair whose own cell was not edited changes only
    when the cell that answers it was.
    """
    document, edits = case
    patch = _grid_patch(edits, level)
    merged = _merge(document, patch)
    before_policy = AutonomyPolicy.from_document(document)
    after_policy = AutonomyPolicy.from_document(merged)

    edited_by_source: dict[str, set[tuple[str, str]]] = {}
    for source, submitter_class, spec_type in edits:
        edited_by_source.setdefault(source, set()).add((submitter_class, spec_type))

    for source in document[SECTION_SOURCES]:
        before = routes._source_grid(before_policy, source)
        after = routes._source_grid(after_policy, source)
        touched = edited_by_source.get(source, set())
        for submitter_class in SUBMITTER_CLASSES:
            for spec_type in SPEC_TYPES:
                if after[submitter_class][spec_type] == before[submitter_class][spec_type]:
                    continue
                declaring = after[submitter_class][spec_type]["declared_at"]
                assert declaring, "a changed cell must name the declaration that changed it"
                # The cell that answers this pair after the merge is one the patch
                # named -- either the pair's own cell or a wildcard the operator
                # was shown they were writing.
                assert any(
                    declaring == _cell_path(source, edited_class, edited_type)
                    for edited_class, edited_type in touched
                ), f"{source}.{submitter_class}.{spec_type} changed on no edit of its own"


# --- a projected preset carries only the bundled argv -------------------------


#: Argv a surface might write over a projected copy — including the empty list,
#: which is the edit that would leave a source with no program at all. Generated
#: rather than fixed because the claim is about ANY edit to the copy, and the one
#: mutation somebody thought to write down is the one that would be avoided.
_ARGV_EDITS = st.lists(st.text(max_size=8), max_size=4)


@settings(max_examples=MAX_EXAMPLES)
@given(st.sampled_from(list(WATCH_SOURCE_PRESET_HOSTS)), _ARGV_EDITS)
def test_a_projected_preset_carries_the_bundled_argv_and_survives_an_edit_to_it(
    host: str, edit: list[str]
) -> None:
    """Byte-equality, then independence from an arbitrary edit to the copy.

    Two failures are closed here, and only the second needs generation. A
    projection that composed argv of its own would run a program the preset tables
    never sanctioned; a projection that handed out the table's own containers would
    let one operator's edit — the ``OWNER/REPO`` placeholder is edited on every
    real source — rewrite what every later read of this process supplies, silently,
    for sources nobody was editing.
    """
    bundled = [str(argument) for argument in WATCH_SOURCE_PRESETS[host][POLL_KEY]]

    projected = {preset["host"]: preset for preset in routes._registry_payload()["source_presets"]}
    assert set(projected) == set(WATCH_SOURCE_PRESET_HOSTS)
    assert projected[host]["entry"][POLL_KEY] == bundled

    # Every OTHER preset in the same payload is its own table entry too, so a
    # projection that leaked one host's argv into another's is caught by the same
    # example rather than needing its own.
    for other, preset in projected.items():
        assert preset["entry"][POLL_KEY] == [
            str(argument) for argument in WATCH_SOURCE_PRESETS[other][POLL_KEY]
        ]

    # The edit a surface makes to its own copy, applied in place — which is what a
    # shallow projection would let reach the bundled table.
    projected[host]["entry"][POLL_KEY][:] = edit

    assert [str(argument) for argument in WATCH_SOURCE_PRESETS[host][POLL_KEY]] == bundled
    refreshed = {
        preset["host"]: preset["entry"][POLL_KEY]
        for preset in routes._registry_payload()["source_presets"]
    }
    for other, argv in refreshed.items():
        assert argv == [
            str(argument) for argument in WATCH_SOURCE_PRESETS[other][POLL_KEY]
        ], "an edit to a projected copy changed what a later read supplies"
