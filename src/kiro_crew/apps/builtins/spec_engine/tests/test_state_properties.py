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

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from kiro_crew.apps.builtins.spec_engine.engine.audit import AuditLog
from kiro_crew.apps.builtins.spec_engine.engine.state import (
    CLAIM_DISPATCH,
    CLAIM_WRITEBACK,
    SpecLocked,
    SpecRef,
    StatePersistenceError,
    StateStore,
)

from .conftest import NATIVE_SPEC_FILES, make_spec_dir, spec_dir_snapshot

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
            store.set_approval_stale(ref, value)
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
