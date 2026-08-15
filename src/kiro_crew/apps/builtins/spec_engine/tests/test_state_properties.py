"""Property-based tests for the state store and audit log.

Two properties matter at this layer.

**Spec directory purity.** Whatever sequence of state operations runs, the files
under ``.kiro/specs/<name>/`` are exactly the native documents and the sidecar.
This is the interop contract with the Kiro IDE and CLI, and it has to hold for
arbitrary traces rather than for the handful a unit test happens to script.

**Claim at-most-once.** Whatever order and multiplicity a set of claim requests
arrives in, each (kind, scope, subject, generation) key is granted exactly once.
The watcher's exactly-once dispatch and the tracker writeback's at-most-once
delivery both reduce to this, and the failure it prevents is a duplicate side
effect on a shared system.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from kiro_crew.apps.builtins.spec_engine.engine.audit import AuditLog
from kiro_crew.apps.builtins.spec_engine.engine.config import (
    CONFIG_ONLY_PATHS,
    ConfigStore,
    ConfigWriteRefused,
    ConfigWriteSurface,
    config_only_paths,
)
from kiro_crew.apps.builtins.spec_engine.engine.state import (
    CLAIM_DISPATCH,
    CLAIM_WRITEBACK,
    SpecLocked,
    SpecRef,
    StatePersistenceError,
    StateStore,
)

from .conftest import NATIVE_SPEC_FILES, make_spec_dir, spec_dir_snapshot

#: A surface no operator confirmed: what an MCP tool call writes through. The
#: fenced sections exist to be unreachable from here.
TOOL_SURFACE = ConfigWriteSurface("mcp-tool")

#: Hypothesis examples per property. Each example runs several SQLite
#: transactions, so this trades a little coverage for a suite that stays fast
#: enough to run on every commit.
MAX_EXAMPLES = 60

_IDENTIFIERS = st.text(
    alphabet=st.characters(min_codepoint=33, max_codepoint=122, blacklist_characters="\\/"),
    min_size=1,
    max_size=12,
)

_OPERATIONS = st.lists(
    st.one_of(
        st.tuples(st.just("register"), _IDENTIFIERS),
        st.tuples(st.just("phase"), _IDENTIFIERS),
        st.tuples(st.just("approve"), _IDENTIFIERS),
        st.tuples(st.just("stale"), _IDENTIFIERS),
        st.tuples(st.just("run"), _IDENTIFIERS),
        st.tuples(st.just("claim"), _IDENTIFIERS),
        st.tuples(st.just("workspace"), _IDENTIFIERS),
        st.tuples(st.just("enqueue"), _IDENTIFIERS),
        st.tuples(st.just("dequeue"), _IDENTIFIERS),
        st.tuples(st.just("archive"), _IDENTIFIERS),
        st.tuples(st.just("lock"), _IDENTIFIERS),
        st.tuples(st.just("audit"), _IDENTIFIERS),
    ),
    max_size=12,
)

_CLAIM_REQUESTS = st.lists(
    st.tuples(
        st.sampled_from([CLAIM_DISPATCH, CLAIM_WRITEBACK]),
        st.sampled_from(["github", "gitlab", "run-1"]),
        st.sampled_from(["1", "2", "claimed", "completed"]),
        st.sampled_from(["", "1", "2"]),
    ),
    min_size=1,
    max_size=25,
)


class TestSpecDirectoryPurity:
    @settings(
        max_examples=MAX_EXAMPLES,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    @given(operations=_OPERATIONS)
    def test_no_operation_writes_into_a_spec_directory(
        self, tmp_path: Path, operations: list[tuple[str, str]]
    ) -> None:
        # One project and one store per example: a shared store would let an
        # earlier example's rows decide a later one's outcome.
        example = tmp_path / uuid.uuid4().hex
        project = example / "project"
        project.mkdir(parents=True)
        spec_dir = make_spec_dir(project, "example")
        before = spec_dir_snapshot(spec_dir)
        assert set(before) == set(NATIVE_SPEC_FILES)

        store = StateStore(root=example / "state")
        log = AuditLog(root=example / "state")
        ref = SpecRef.of(project, "example")
        try:
            for index, (operation, value) in enumerate(operations):
                self._apply(store, log, ref, project, operation, value, index)
        finally:
            store.close()

        assert spec_dir_snapshot(spec_dir) == before

    @staticmethod
    def _apply(
        store: StateStore,
        log: AuditLog,
        ref: SpecRef,
        project: Path,
        operation: str,
        value: str,
        index: int,
    ) -> None:
        if operation == "register":
            store.register_spec(ref, spec_type=value)
        elif operation == "phase":
            store.record_phase(ref, value)
        elif operation == "approve":
            store.record_approval(ref, gate=value, actor=f"user:{value}", doc_hash=value)
        elif operation == "stale":
            store.mark_approval_stale(ref, value)
        elif operation == "run":
            run_id = f"run-{index}"
            store.create_run(run_id, ref, state="queued", source=value)
            store.update_run(run_id, state="authoring", detail={"note": value})
        elif operation == "claim":
            store.claim_dispatch(value, value, generation=str(index))
        elif operation == "workspace":
            store.record_workspace(f"run-{index}", kind="worktree", location=value)
        elif operation == "enqueue":
            store.enqueue(source=value, project=project, item_id=value)
        elif operation == "dequeue":
            store.next_queued()
        elif operation == "archive":
            store.set_archived(ref, index % 2 == 0)
        elif operation == "lock":
            # A rejection is an ordinary outcome here: what matters is that
            # neither the winner nor the loser touches the spec directory.
            try:
                with store.lock(ref, owner=f"session:{value}"):
                    pass
            except SpecLocked:
                pass
        elif operation == "audit":
            log.append(ref, f"event.{value}", run=f"run-{index}", detail={"value": value})
        else:  # pragma: no cover - the strategy generates no other operation
            raise AssertionError(f"unhandled operation {operation!r}")


class TestClaimAtMostOnce:
    @settings(
        max_examples=MAX_EXAMPLES,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    @given(requests=_CLAIM_REQUESTS)
    def test_each_key_is_granted_exactly_once(
        self, tmp_path: Path, requests: list[tuple[str, str, str, str]]
    ) -> None:
        store = StateStore(root=tmp_path / uuid.uuid4().hex)
        try:
            granted: dict[tuple[str, str, str, str], int] = {}
            for kind, scope, subject, generation in requests:
                key = (kind, scope, subject, generation)
                won = store.claim(kind, scope, subject, generation=generation)
                granted[key] = granted.get(key, 0) + (1 if won else 0)

            assert all(count == 1 for count in granted.values())
            # Every distinct key requested is recorded, and nothing else is.
            recorded = {
                (claim.kind, claim.scope, claim.subject, claim.generation)
                for claim in store.list_claims()
            }
            assert recorded == set(granted)
        finally:
            store.close()

    @settings(
        max_examples=MAX_EXAMPLES,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    @given(requests=_CLAIM_REQUESTS)
    def test_a_released_key_is_granted_exactly_once_again(
        self, tmp_path: Path, requests: list[tuple[str, str, str, str]]
    ) -> None:
        store = StateStore(root=tmp_path / uuid.uuid4().hex)
        try:
            keys = {request for request in requests}
            for kind, scope, subject, generation in keys:
                assert store.claim(kind, scope, subject, generation=generation) is True
            for kind, scope, subject, generation in keys:
                assert store.release_claim(kind, scope, subject, generation=generation) is True
            # Release is the manual re-dispatch override: after it, the key is
            # claimable once more, and still only once.
            for kind, scope, subject, generation in keys:
                assert store.claim(kind, scope, subject, generation=generation) is True
                assert store.claim(kind, scope, subject, generation=generation) is False
        finally:
            store.close()


@pytest.mark.parametrize("segment", [".kiro/specs", ".kiro/specs/example"])
def test_a_state_root_anywhere_inside_a_spec_tree_is_refused(tmp_path: Path, segment: str) -> None:
    with pytest.raises(StatePersistenceError) as raised:
        StateStore(root=tmp_path / segment / "state")
    assert "spec tree" in str(raised.value)


# --- The spec-tree fence under a second spelling ----------------------------
#
# The parametrized case above spells the spec tree the obvious way. A path is
# not its spelling: the same directory is reachable through a dot segment, a
# doubled slash, a parent traversal, a different case on a case-insensitive
# filesystem, and a symlink. The fence is what keeps engine state out of the
# interop contract, so it has to hold for every spelling of the same place, and
# this engine has already shipped a defect in exactly this area.

#: Spellings that name the spec tree without spelling it literally, and that
#: ``PurePath`` normalisation collapses back to it.
_NORMALISING_SPELLINGS = st.sampled_from(
    [
        ".kiro/specs/./state",
        ".kiro/specs//state",
        ".kiro/specs/example/../state",
        ".kiro/./specs/state",
        ".kiro/specs/example/./deeper/../state",
    ]
)


def _case_insensitive(root: Path) -> bool:
    """Whether *root*'s filesystem treats two spellings as one directory."""
    probe = root / "CaseProbe"
    probe.mkdir(exist_ok=True)
    return (root / "caseprobe").is_dir()


@settings(
    max_examples=MAX_EXAMPLES,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(spelling=_NORMALISING_SPELLINGS)
def test_a_normalised_spelling_of_a_spec_tree_is_refused(tmp_path: Path, spelling: str) -> None:
    """A dot segment, a doubled slash or a traversal does not evade the fence."""
    example = tmp_path / uuid.uuid4().hex
    example.mkdir()

    with pytest.raises(StatePersistenceError) as raised:
        StateStore(root=Path(str(example) + "/" + spelling))
    assert "spec tree" in str(raised.value)


def test_a_case_spelled_spec_tree_is_refused_where_case_does_not_distinguish(
    tmp_path: Path,
) -> None:
    """On a case-insensitive filesystem, two spellings are one directory.

    Skipped where case distinguishes directories, because there ``.KIRO/SPECS``
    is genuinely somewhere else and storing state in it is allowed. Asserting a
    refusal there would be asserting the wrong thing rather than finding a defect.
    """
    if not _case_insensitive(tmp_path):
        pytest.skip("filesystem is case-sensitive, so .KIRO/SPECS is a different directory")
    project = tmp_path / "project"
    (project / ".kiro" / "specs" / "example").mkdir(parents=True)

    with pytest.raises(StatePersistenceError):
        StateStore(root=project / ".KIRO" / "SPECS" / "state")


def test_a_state_root_reached_through_a_symlink_into_a_spec_tree_is_refused(
    tmp_path: Path,
) -> None:
    """A link is a second spelling of its target."""
    project = tmp_path / "project"
    specs = project / ".kiro" / "specs"
    specs.mkdir(parents=True)
    link = tmp_path / "linked-state"
    try:
        link.symlink_to(specs, target_is_directory=True)
    except (OSError, NotImplementedError):  # pragma: no cover - unprivileged Windows
        pytest.skip("this platform does not allow creating a directory symlink")

    with pytest.raises(StatePersistenceError):
        StateStore(root=link / "state")


# --- The config-only fence, for any patch shape -----------------------------

#: A project or source name a generated patch can be keyed by, including one that
#: differs from another only by case: the fence's wildcard matches a key by
#: identity, so a second spelling of a NAME must still be reported.
_ENTRY_NAMES = st.sampled_from(["acme", "Acme", "acme ", "a.b", "*"])


def _fenced_patch(path: str, name: str) -> dict[str, Any]:
    """A patch that writes exactly *path*, expanding a ``*`` segment to *name*."""
    node: Any = {"written": True}
    for segment in reversed(path.split(".")):
        node = {name if segment == "*" else segment: node}
    return dict(node)


@settings(max_examples=MAX_EXAMPLES, deadline=None)
@given(path=st.sampled_from(CONFIG_ONLY_PATHS), name=_ENTRY_NAMES)
def test_every_fenced_path_is_reported_whatever_the_patch_is_keyed_by(path: str, name: str) -> None:
    """A patch writing a fenced path is reported as writing it.

    Generated over every fenced path rather than the handful a scripted case
    lists, and over entry names including a case variant and one carrying
    whitespace, because the wildcard segment matches whatever key is there and a
    fence that only recognised tidy names would be a fence with a spelling.
    """
    reported = config_only_paths(_fenced_patch(path, name))

    assert reported, f"{path} keyed by {name!r} was not reported as config-only"
    expected = path.replace("*", name)
    assert expected in reported


@settings(max_examples=MAX_EXAMPLES, deadline=None)
@given(path=st.sampled_from(CONFIG_ONLY_PATHS), name=_ENTRY_NAMES)
def test_a_fenced_patch_is_refused_from_a_surface_no_operator_confirmed(
    tmp_path_factory: pytest.TempPathFactory, path: str, name: str
) -> None:
    """The report is enforced, not merely produced.

    A path reported as config-only and then written anyway would be a fence that
    describes itself accurately and stops nothing.
    """
    store = ConfigStore(tmp_path_factory.mktemp("fence") / "config")
    patch = _fenced_patch(path, name)

    with pytest.raises(ConfigWriteRefused) as raised:
        store.write(patch, surface=TOOL_SURFACE)
    assert set(raised.value.paths) == set(config_only_paths(patch))
    # Nothing was written, so a refused write cannot have left a partial document.
    assert not store.path.exists() or store.document().get(path.split(".")[0]) is None


#: The key pool a claim trace runs over: two subjects of one scope, each with two
#: generations. Deliberately tiny -- four keys against a thirty-step trace, so a
#: claim/release/re-claim cycle on one key and a bulk release of a subject holding
#: two generations both happen constantly instead of by luck. The kind, scope and
#: subject dimensions are already swept by the at-most-once property above; what
#: this trace needs is repetition, not variety.
_TRACE_SUBJECTS = ("17", "18")
_TRACE_GENERATIONS = ("", "1")

#: One step of a claim trace. ``release`` names a generation; ``release_all``
#: drops every generation of a subject without knowing them, which is the
#: override a held reviewer comment needs.
_CLAIM_STEPS = st.lists(
    st.tuples(
        st.sampled_from(["claim", "claim", "release", "release_all"]),
        st.just(CLAIM_DISPATCH),
        st.just("github"),
        st.sampled_from(_TRACE_SUBJECTS),
        st.sampled_from(_TRACE_GENERATIONS),
    ),
    min_size=1,
    max_size=30,
)


class TestClaimExactlyOnceUnderInterleaving:
    """Exactly-once across arbitrary claim/release/re-claim interleavings.

    The properties above cover claims arriving repeatedly, and one bulk
    release-then-reclaim pass. Neither reaches the ordering this ledger is
    actually asked to survive: a release landing between two polls, a release of
    a generation nobody claimed, and a subject released by every generation at
    once while another generation of the same subject is still held. That last
    one is the engine's own named failure -- one generation left behind makes a
    release look applied while the next poll still reads the subject as already
    seen -- and it is reachable only from a trace, never from a single call.

    The expected answer comes from a shadow model of which keys are held,
    maintained alongside the trace. Asking the store what it holds and comparing
    it against itself would confirm the store against its own echo.
    """

    @settings(
        max_examples=MAX_EXAMPLES,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    @given(steps=_CLAIM_STEPS)
    def test_work_starts_exactly_once_per_generation_between_releases(
        self, tmp_path: Path, steps: list[tuple[str, str, str, str, str]]
    ) -> None:
        store = StateStore(root=tmp_path / uuid.uuid4().hex)
        #: Keys the ledger should be holding, per the trace so far.
        held: set[tuple[str, str, str, str]] = set()
        #: How many times each key was granted, and how many times released.
        grants: dict[tuple[str, str, str, str], int] = {}
        releases: dict[tuple[str, str, str, str], int] = {}
        try:
            for action, kind, scope, subject, generation in steps:
                key = (kind, scope, subject, generation)
                if action == "claim":
                    won = store.claim(kind, scope, subject, generation=generation)
                    # A claim is granted exactly when the ledger is not already
                    # holding that generation. This is the whole guarantee: a
                    # duplicate dispatch is a second grant.
                    assert won is (key not in held)
                    if won:
                        held.add(key)
                        grants[key] = grants.get(key, 0) + 1
                elif action == "release":
                    dropped = store.release_claim(kind, scope, subject, generation=generation)
                    # Releasing a generation nobody claimed reports that it
                    # dropped nothing, rather than reporting success for a
                    # release that changed no state.
                    assert dropped is (key in held)
                    if dropped:
                        held.discard(key)
                        releases[key] = releases.get(key, 0) + 1
                else:
                    expected = {item for item in held if item[:3] == (kind, scope, subject)}
                    dropped_count = store.release_claims(kind, scope, subject)
                    # Every generation goes, counted. One left behind is the
                    # defect this method exists to prevent.
                    assert dropped_count == len(expected)
                    for item in expected:
                        held.discard(item)
                        releases[item] = releases.get(item, 0) + 1

                # After every step the ledger holds exactly what the trace says.
                recorded = {
                    (claim.kind, claim.scope, claim.subject, claim.generation)
                    for claim in store.list_claims()
                }
                assert recorded == held

            # Work started once per claim/release cycle and never twice within
            # one: a key granted more often than it was released plus once has
            # started work it was not entitled to start.
            for key, count in grants.items():
                assert count == releases.get(key, 0) + (1 if key in held else 0)
        finally:
            store.close()

    @settings(
        max_examples=MAX_EXAMPLES,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    @given(
        generations=st.lists(st.sampled_from(["", "1", "2", "3"]), min_size=1, unique=True),
        target=st.sampled_from(["", "1", "2", "3"]),
    )
    def test_a_bulk_release_leaves_no_generation_of_the_subject_behind(
        self, tmp_path: Path, generations: list[str], target: str
    ) -> None:
        """The named bug class, stated directly.

        A subject claimed at several generations and then released wholesale must
        be claimable again at *every* generation, including one this run never
        claimed. A single row surviving would make the next poll read the subject
        as already seen while the operator was told the release applied.
        """
        store = StateStore(root=tmp_path / uuid.uuid4().hex)
        try:
            for generation in generations:
                assert store.claim(CLAIM_DISPATCH, "github", "17", generation=generation) is True

            assert store.release_claims(CLAIM_DISPATCH, "github", "17") == len(generations)
            assert store.list_claims(kind=CLAIM_DISPATCH, scope="github") == []
            # Re-claimable once, at any generation, whether or not that
            # generation was among the released ones.
            assert store.claim(CLAIM_DISPATCH, "github", "17", generation=target) is True
            assert store.claim(CLAIM_DISPATCH, "github", "17", generation=target) is False
        finally:
            store.close()

    @settings(
        max_examples=MAX_EXAMPLES,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    @given(
        held=st.sampled_from(["", "1", "2"]),
        released=st.sampled_from(["", "1", "2"]),
    )
    def test_releasing_one_generation_never_frees_another(
        self, tmp_path: Path, held: str, released: str
    ) -> None:
        """Generations are independent subjects of the same claim.

        Releasing one must not make a sibling claimable, and must not report
        success for a generation that was never claimed.
        """
        store = StateStore(root=tmp_path / uuid.uuid4().hex)
        try:
            assert store.claim(CLAIM_DISPATCH, "github", "17", generation=held) is True

            dropped = store.release_claim(CLAIM_DISPATCH, "github", "17", generation=released)
            assert dropped is (released == held)

            still_claimed = store.claim(CLAIM_DISPATCH, "github", "17", generation=held)
            # The held generation is re-grantable only if it was the one released.
            assert still_claimed is (released == held)
        finally:
            store.close()
