"""State store behaviour: schema, records, the claim ledger, locking, and failure.

Two of these classes carry the invariants the rest of the app leans on.
``TestConcurrentWriters`` pins that exactly one of several simultaneous writers
wins a spec and that every loser is handed the spec's current state, because a
loser that only learns "rejected" has to guess what the winner did.
``TestPersistenceFailure`` pins that an unusable state store fails the operation
and leaves the spec directory byte-identical — the failure mode being guarded
against is an engine that "helpfully" records its state in a spec document, which
breaks the IDE and CLI contract silently and permanently.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.apps.builtins.spec_engine.engine.state import (
    CLAIM_DISPATCH,
    CLAIM_WRITEBACK,
    SCHEMA_VERSION,
    LockLost,
    SpecLocked,
    SpecRef,
    StatePersistenceError,
    StateStore,
    reject_spec_tree_path,
    state_root,
)

from .conftest import NATIVE_SPEC_FILES, spec_dir_snapshot


class TestSpecRef:
    def test_project_path_is_resolved_so_one_project_has_one_identity(self, project: Path) -> None:
        direct = SpecRef.of(project, "example")
        indirect = SpecRef.of(project / "sub" / "..", "example")
        assert direct == indirect
        assert direct.key == indirect.key

    def test_key_separates_same_named_specs_in_different_projects(self, tmp_path: Path) -> None:
        first = SpecRef.of(tmp_path / "a", "login")
        second = SpecRef.of(tmp_path / "b", "login")
        assert first.key != second.key

    def test_spec_dir_is_the_native_interop_path(self, project: Path) -> None:
        assert SpecRef.of(project, "example").spec_dir == project / ".kiro" / "specs" / "example"

    @pytest.mark.parametrize("name", ["", "   ", "a/b", "a\\b", ".", ".."])
    def test_a_name_that_is_not_a_single_segment_is_refused(self, project: Path, name: str) -> None:
        with pytest.raises(ValueError):
            SpecRef.of(project, name)


class TestSchema:
    def test_every_table_the_engine_needs_exists(self, store: StateStore) -> None:
        rows = store._query("SELECT name FROM sqlite_master WHERE type = 'table'")
        tables = {row["name"] for row in rows}
        assert {
            "specs",
            "approvals",
            "runs",
            "claims",
            "workspaces",
            "queue",
            "schema_meta",
        } <= tables

    def test_schema_version_is_recorded(self, store: StateStore) -> None:
        row = store._query_one("SELECT value FROM schema_meta WHERE key = 'version'")
        assert row is not None
        assert row["value"] == str(SCHEMA_VERSION)

    def test_opening_an_existing_store_again_is_idempotent(self, state_dir: Path) -> None:
        first = StateStore(root=state_dir)
        ref = SpecRef.of(state_dir.parent / "project", "example")
        first.register_spec(ref, spec_type="feature")
        second = StateStore(root=state_dir)
        record = second.get_spec(ref)
        assert record is not None
        assert record.spec_type == "feature"

    def test_state_root_defaults_outside_any_project(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
        resolved = state_root()
        assert ".kiro/specs" not in resolved.as_posix()
        # No exception: the default root is always a legal place for engine state.
        reject_spec_tree_path(resolved)


class TestSpecRegistry:
    def test_register_then_read_back(self, store: StateStore, ref: SpecRef) -> None:
        store.register_spec(ref, spec_type="feature", phase="requirements")
        record = store.get_spec(ref)
        assert record is not None
        assert (record.project, record.name) == (ref.project, ref.name)
        assert (record.spec_type, record.phase) == ("feature", "requirements")
        assert record.archived is False
        assert record.ref == ref

    def test_re_registering_does_not_erase_a_recorded_type(
        self, store: StateStore, ref: SpecRef
    ) -> None:
        store.register_spec(ref, spec_type="bugfix")
        store.register_spec(ref, phase="tasks")
        record = store.get_spec(ref)
        assert record is not None
        assert record.spec_type == "bugfix"
        assert record.phase == "tasks"

    def test_unknown_spec_reads_as_none(self, store: StateStore, project: Path) -> None:
        assert store.get_spec(SpecRef.of(project, "absent")) is None

    def test_archival_is_reversible_and_hidden_from_the_default_listing(
        self, store: StateStore, ref: SpecRef
    ) -> None:
        store.register_spec(ref, spec_type="feature")
        store.set_archived(ref, True)
        assert store.list_specs(project=ref.project) == []
        assert [r.name for r in store.list_specs(project=ref.project, include_archived=True)] == [
            "example"
        ]
        store.set_archived(ref, False)
        assert [r.name for r in store.list_specs(project=ref.project)] == ["example"]

    def test_listing_is_scoped_to_a_project(self, store: StateStore, tmp_path: Path) -> None:
        store.register_spec(SpecRef.of(tmp_path / "a", "one"), spec_type="quick")
        store.register_spec(SpecRef.of(tmp_path / "b", "two"), spec_type="quick")
        assert [r.name for r in store.list_specs(project=tmp_path / "a")] == ["one"]
        assert len(store.list_specs()) == 2

    def test_set_spec_type_rejects_an_empty_value(self, store: StateStore, ref: SpecRef) -> None:
        with pytest.raises(ValueError):
            store.set_spec_type(ref, "")


class TestApprovals:
    def test_an_approval_records_who_approved_and_when(
        self, store: StateStore, ref: SpecRef
    ) -> None:
        record = store.record_approval(
            ref, gate="requirements", actor="user:ada", doc_hash="hash-1"
        )
        assert record.actor == "user:ada"
        assert record.approved_ts
        stored = store.get_approval(ref, "requirements")
        assert stored == record

    def test_re_approving_replaces_the_record_and_clears_staleness(
        self, store: StateStore, ref: SpecRef
    ) -> None:
        store.record_approval(ref, gate="design", actor="user:ada", doc_hash="hash-1")
        store.mark_approval_stale(ref, "design")
        assert store.get_approval(ref, "design").stale is True  # type: ignore[union-attr]
        store.record_approval(ref, gate="design", actor="policy:autonomy", doc_hash="hash-2")
        refreshed = store.get_approval(ref, "design")
        assert refreshed is not None
        assert (refreshed.actor, refreshed.doc_hash, refreshed.stale) == (
            "policy:autonomy",
            "hash-2",
            False,
        )

    def test_staling_a_gate_that_was_never_approved_changes_nothing(
        self, store: StateStore, ref: SpecRef
    ) -> None:
        assert store.mark_approval_stale(ref, "design") is False
        assert store.get_approval(ref, "design") is None

    def test_approvals_list_in_gate_order(self, store: StateStore, ref: SpecRef) -> None:
        store.record_approval(ref, gate="tasks", actor="user:ada", doc_hash="h")
        store.record_approval(ref, gate="design", actor="user:ada", doc_hash="h")
        assert [a.gate for a in store.list_approvals(ref)] == ["design", "tasks"]

    def test_an_approval_needs_a_gate_and_an_actor(self, store: StateStore, ref: SpecRef) -> None:
        with pytest.raises(ValueError):
            store.record_approval(ref, gate="", actor="user:ada", doc_hash="h")
        with pytest.raises(ValueError):
            store.record_approval(ref, gate="design", actor="", doc_hash="h")


class TestRuns:
    def test_create_then_update_fields(self, store: StateStore, ref: SpecRef) -> None:
        store.create_run("run-1", ref, state="queued", source="github", item_id="42")
        updated = store.update_run("run-1", state="authoring", cost_credits=1.5)
        assert (updated.state, updated.cost_credits) == ("authoring", 1.5)
        assert (updated.source, updated.item_id) == ("github", "42")

    def test_detail_merges_so_one_writer_does_not_drop_anothers_key(
        self, store: StateStore, ref: SpecRef
    ) -> None:
        store.create_run("run-1", ref, state="queued", detail={"posture_source": "config"})
        store.update_run("run-1", detail={"stage": "verify"})
        run = store.get_run("run-1")
        assert run is not None
        assert run.detail == {"posture_source": "config", "stage": "verify"}

    def test_updating_an_unknown_run_raises(self, store: StateStore) -> None:
        with pytest.raises(KeyError):
            store.update_run("nope", state="done")

    def test_runs_filter_by_spec_and_state(self, store: StateStore, tmp_path: Path) -> None:
        first = SpecRef.of(tmp_path / "a", "one")
        second = SpecRef.of(tmp_path / "b", "two")
        store.create_run("run-a", first, state="executing")
        store.create_run("run-b", first, state="done")
        store.create_run("run-c", second, state="executing")
        assert {r.run_id for r in store.list_runs(ref=first)} == {"run-a", "run-b"}
        assert {r.run_id for r in store.list_runs(states=["executing"])} == {"run-a", "run-c"}

    def test_a_run_needs_an_id_and_a_state(self, store: StateStore, ref: SpecRef) -> None:
        with pytest.raises(ValueError):
            store.create_run("", ref, state="queued")
        with pytest.raises(ValueError):
            store.create_run("run-1", ref, state="")


class TestClaimLedger:
    def test_the_first_claim_wins_and_every_repeat_loses(self, store: StateStore) -> None:
        assert store.claim_dispatch("github", "42", generation="1", run_id="run-1") is True
        for _ in range(3):
            assert store.claim_dispatch("github", "42", generation="1", run_id="run-2") is False

    def test_a_new_lifecycle_generation_is_claimable_again(self, store: StateStore) -> None:
        assert store.claim_dispatch("github", "42", generation="1") is True
        assert store.claim_dispatch("github", "42", generation="2") is True

    def test_the_same_item_id_on_another_source_is_a_separate_claim(
        self, store: StateStore
    ) -> None:
        assert store.claim_dispatch("github", "42") is True
        assert store.claim_dispatch("gitlab", "42") is True

    def test_writeback_is_claimed_once_per_run_per_event(self, store: StateStore) -> None:
        assert store.claim_writeback("run-1", "claimed") is True
        # A re-poll or a resumed run asks again and is refused, which is what
        # keeps one run from commenting on one item five times.
        assert store.claim_writeback("run-1", "claimed") is False
        assert store.claim_writeback("run-1", "completed") is True
        assert store.claim_writeback("run-2", "claimed") is True

    def test_a_claim_records_its_run_and_time(self, store: StateStore) -> None:
        store.claim_dispatch("github", "42", generation="1", run_id="run-1")
        claim = store.get_claim(CLAIM_DISPATCH, "github", "42", generation="1")
        assert claim is not None
        assert (claim.run_id, claim.generation) == ("run-1", "1")
        assert claim.claimed_ts

    def test_releasing_a_claim_allows_a_manual_re_dispatch(self, store: StateStore) -> None:
        store.claim_dispatch("github", "42", generation="1")
        assert store.release_claim(CLAIM_DISPATCH, "github", "42", generation="1") is True
        assert store.claim_dispatch("github", "42", generation="1") is True
        assert store.release_claim(CLAIM_DISPATCH, "github", "99") is False

    def test_claims_list_by_kind_and_scope(self, store: StateStore) -> None:
        store.claim_dispatch("github", "1")
        store.claim_dispatch("gitlab", "2")
        store.claim_writeback("run-1", "claimed")
        assert {c.subject for c in store.list_claims(kind=CLAIM_DISPATCH)} == {"1", "2"}
        assert {c.subject for c in store.list_claims(scope="run-1")} == {"claimed"}
        assert {c.kind for c in store.list_claims(kind=CLAIM_WRITEBACK)} == {CLAIM_WRITEBACK}

    def test_a_claim_needs_all_three_key_parts(self, store: StateStore) -> None:
        with pytest.raises(ValueError):
            store.claim("", "github", "42")
        with pytest.raises(ValueError):
            store.claim(CLAIM_DISPATCH, "", "42")
        with pytest.raises(ValueError):
            store.claim(CLAIM_DISPATCH, "github", "")

    def test_only_one_of_many_threads_claims_the_same_item(self, store: StateStore) -> None:
        threads_count = 8
        ready = threading.Barrier(threads_count)
        results: list[bool] = []
        results_lock = threading.Lock()

        def attempt() -> None:
            ready.wait()
            won = store.claim_dispatch("github", "42", generation="1")
            with results_lock:
                results.append(won)

        threads = [threading.Thread(target=attempt) for _ in range(threads_count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert results.count(True) == 1
        assert len(results) == threads_count


class TestWorkspaceLedger:
    def test_only_one_of_many_dispatchers_takes_the_same_queue_entry(
        self, store: StateStore, ref: SpecRef
    ) -> None:
        """The queue's own stated contract, which every other queue test is too
        sequential to observe.

        Selecting and marking share one transaction precisely so two dispatchers
        cannot both take one entry. Run in sequence the select always sees the
        previous mark, so splitting the two apart changes nothing a sequential
        test can see -- while under real contention it hands the same work to two
        dispatchers. The claim ledger and the spec lock both got a contention test
        of this shape; the queue did not.
        """
        store.enqueue(source="github", project=ref.project, item_id="42")
        dispatchers = 8
        ready = threading.Barrier(dispatchers)
        taken: list[int] = []
        taken_lock = threading.Lock()

        def dequeue() -> None:
            ready.wait()
            entry = store.next_queued()
            if entry is not None:
                with taken_lock:
                    taken.append(entry.seq)

        threads = [threading.Thread(target=dequeue) for _ in range(dispatchers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert len(taken) == 1, f"the same entry went to several dispatchers: {taken}"


class TestWorkspaceLedgerRecords:
    def test_recorded_workspaces_are_findable_by_run(self, store: StateStore) -> None:
        worktree = store.record_workspace("run-1", kind="worktree", location="/tmp/wt")
        store.record_workspace(
            "run-1", kind="deployment", location="stack-1", address="https://example.invalid"
        )
        store.record_workspace("run-2", kind="worktree", location="/tmp/other")
        assert worktree.workspace_id > 0
        assert {w.kind for w in store.list_workspaces(run_id="run-1")} == {
            "worktree",
            "deployment",
        }

    def test_cleanup_is_recorded_once(self, store: StateStore) -> None:
        record = store.record_workspace("run-1", kind="worktree", location="/tmp/wt")
        assert store.mark_workspace_cleaned(record.workspace_id) is True
        assert store.mark_workspace_cleaned(record.workspace_id) is False
        assert store.list_workspaces(run_id="run-1") == []
        cleaned = store.list_workspaces(run_id="run-1", include_cleaned=True)
        assert len(cleaned) == 1
        assert cleaned[0].cleaned is True
        assert cleaned[0].cleaned_ts

    def test_a_workspace_record_needs_a_run_and_a_kind(self, store: StateStore) -> None:
        with pytest.raises(ValueError):
            store.record_workspace("", kind="worktree", location="/tmp/wt")
        with pytest.raises(ValueError):
            store.record_workspace("run-1", kind="", location="/tmp/wt")


class TestQueue:
    def test_entries_come_back_in_arrival_order(self, store: StateStore, tmp_path: Path) -> None:
        for item in ("1", "2", "3"):
            store.enqueue(source="github", project=tmp_path, item_id=item)
        taken = [store.next_queued(), store.next_queued(), store.next_queued()]
        assert [entry.item_id for entry in taken if entry] == ["1", "2", "3"]
        assert store.next_queued() is None

    def test_a_repeated_poll_does_not_grow_the_backlog(
        self, store: StateStore, tmp_path: Path
    ) -> None:
        assert store.enqueue(source="github", project=tmp_path, item_id="1") is not None
        assert store.enqueue(source="github", project=tmp_path, item_id="1") is None
        assert store.queue_depth() == 1

    def test_a_new_generation_of_the_same_item_queues_separately(
        self, store: StateStore, tmp_path: Path
    ) -> None:
        store.enqueue(source="github", project=tmp_path, item_id="1", generation="1")
        assert (
            store.enqueue(source="github", project=tmp_path, item_id="1", generation="2")
            is not None
        )
        assert store.queue_depth() == 2

    def test_dequeueing_is_scoped_to_a_project(self, store: StateStore, tmp_path: Path) -> None:
        store.enqueue(source="github", project=tmp_path / "a", item_id="1")
        store.enqueue(source="github", project=tmp_path / "b", item_id="2")
        taken = store.next_queued(project=tmp_path / "b")
        assert taken is not None
        assert taken.item_id == "2"
        assert store.queue_depth(project=tmp_path / "b") == 0
        assert store.queue_depth(project=tmp_path / "a") == 1

    def test_the_payload_round_trips(self, store: StateStore, tmp_path: Path) -> None:
        store.enqueue(
            source="github",
            project=tmp_path,
            item_id="1",
            payload={"title": "Crash on save", "classification": "bug"},
        )
        entry = store.next_queued()
        assert entry is not None
        assert entry.payload == {"title": "Crash on save", "classification": "bug"}
        assert entry.dequeued_ts

    def test_a_dequeued_entry_stays_visible_only_on_request(
        self, store: StateStore, tmp_path: Path
    ) -> None:
        store.enqueue(source="github", project=tmp_path, item_id="1")
        store.next_queued()
        assert store.list_queue() == []
        assert len(store.list_queue(include_dequeued=True)) == 1

    def test_a_queue_entry_needs_a_source_and_an_item(
        self, store: StateStore, tmp_path: Path
    ) -> None:
        with pytest.raises(ValueError):
            store.enqueue(source="", project=tmp_path, item_id="1")
        with pytest.raises(ValueError):
            store.enqueue(source="github", project=tmp_path, item_id="")


class TestSpecLocking:
    def test_a_lock_is_taken_and_released(self, store: StateStore, ref: SpecRef) -> None:
        handle = store.acquire_lock(ref, owner="session:a")
        record = store.get_spec(ref)
        assert record is not None
        assert record.lock_owner == "session:a"
        assert store.release_lock(handle) is True
        record = store.get_spec(ref)
        assert record is not None
        assert record.lock_owner is None

    def test_the_context_manager_releases_on_the_way_out(
        self, store: StateStore, ref: SpecRef
    ) -> None:
        with store.lock(ref, owner="session:a"):
            assert store.current_state(ref)["lock"]["owner"] == "session:a"
        assert store.current_state(ref)["lock"] is None

    def test_the_context_manager_releases_after_a_failure(
        self, store: StateStore, ref: SpecRef
    ) -> None:
        with pytest.raises(RuntimeError):
            with store.lock(ref, owner="session:a"):
                raise RuntimeError("operation failed")
        assert store.current_state(ref)["lock"] is None

    def test_an_expired_lock_is_taken_over_rather_than_wedging_the_spec(
        self, store: StateStore, ref: SpecRef
    ) -> None:
        store.acquire_lock(ref, owner="session:crashed", ttl_s=0.01)
        time.sleep(0.05)
        handle = store.acquire_lock(ref, owner="session:b")
        assert handle.owner == "session:b"

    def test_releasing_with_a_stale_token_reports_the_loss(
        self, store: StateStore, ref: SpecRef
    ) -> None:
        stale = store.acquire_lock(ref, owner="session:crashed", ttl_s=0.01)
        time.sleep(0.05)
        store.acquire_lock(ref, owner="session:b")
        assert store.release_lock(stale) is False

    def test_verify_lock_reports_a_lock_taken_over_underneath_the_holder(
        self, store: StateStore, ref: SpecRef
    ) -> None:
        handle = store.acquire_lock(ref, owner="session:a", ttl_s=0.01)
        store.verify_lock(handle)
        time.sleep(0.05)
        with pytest.raises(LockLost):
            store.verify_lock(handle)
        store.acquire_lock(ref, owner="session:b")
        with pytest.raises(LockLost):
            store.verify_lock(handle)

    def test_verify_lock_after_release_reports_the_loss(
        self, store: StateStore, ref: SpecRef
    ) -> None:
        handle = store.acquire_lock(ref, owner="session:a")
        store.verify_lock(handle)
        store.release_lock(handle)
        with pytest.raises(LockLost):
            store.verify_lock(handle)

    def test_locking_rejects_a_bad_owner_or_ttl(self, store: StateStore, ref: SpecRef) -> None:
        with pytest.raises(ValueError):
            store.acquire_lock(ref, owner="")
        with pytest.raises(ValueError):
            store.acquire_lock(ref, owner="session:a", ttl_s=0)


class TestConcurrentWriters:
    def test_the_second_writer_is_rejected_with_the_specs_current_state(
        self, state_dir: Path, ref: SpecRef
    ) -> None:
        # Two stores over one database file: the real case is two sessions in
        # different processes, and a single connection would prove nothing.
        winner = StateStore(root=state_dir)
        loser = StateStore(root=state_dir)
        winner.register_spec(ref, spec_type="feature", phase="design")
        winner.record_approval(ref, gate="requirements", actor="user:ada", doc_hash="hash-1")
        winner.create_run("run-1", ref, state="authoring")
        winner.acquire_lock(ref, owner="session:winner")

        with pytest.raises(SpecLocked) as raised:
            loser.acquire_lock(ref, owner="session:loser")

        state = raised.value.state
        assert raised.value.holder == "session:winner"
        assert state["lock"]["owner"] == "session:winner"
        assert state["lock"]["expired"] is False
        assert (state["spec_type"], state["phase"]) == ("feature", "design")
        assert state["approvals"] == [
            {
                "gate": "requirements",
                "actor": "user:ada",
                "approved_ts": state["approvals"][0]["approved_ts"],
                "stale": False,
            }
        ]
        assert state["runs"] == [
            {
                "run_id": "run-1",
                "state": "authoring",
                "updated_ts": state["runs"][0]["updated_ts"],
            }
        ]

    def test_a_second_operation_in_the_same_session_is_still_a_conflict(
        self, store: StateStore, ref: SpecRef
    ) -> None:
        store.acquire_lock(ref, owner="session:a")
        with pytest.raises(SpecLocked):
            store.acquire_lock(ref, owner="session:a")

    def test_a_conflict_on_one_spec_does_not_block_another(
        self, store: StateStore, project: Path
    ) -> None:
        first = SpecRef.of(project, "example")
        second = SpecRef.of(project, "other")
        store.acquire_lock(first, owner="session:a")
        assert store.acquire_lock(second, owner="session:b").owner == "session:b"

    def test_exactly_one_of_many_simultaneous_writers_wins(
        self, store: StateStore, ref: SpecRef
    ) -> None:
        writers = 8
        ready = threading.Barrier(writers)
        outcomes: list[tuple[str, dict[str, Any] | None]] = []
        outcomes_lock = threading.Lock()

        def attempt(index: int) -> None:
            owner = f"session:{index}"
            ready.wait()
            try:
                store.acquire_lock(ref, owner=owner)
            except SpecLocked as rejected:
                with outcomes_lock:
                    outcomes.append(("rejected", rejected.state))
                return
            with outcomes_lock:
                outcomes.append(("acquired", None))

        threads = [threading.Thread(target=attempt, args=(index,)) for index in range(writers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        verdicts = [verdict for verdict, _ in outcomes]
        assert verdicts.count("acquired") == 1
        assert len(verdicts) == writers
        holder = store.current_state(ref)["lock"]["owner"]
        # Every loser learns who holds the spec, not merely that it lost.
        for verdict, state in outcomes:
            if verdict == "rejected":
                assert state is not None
                assert state["lock"]["owner"] == holder
                assert state["name"] == ref.name


class TestPersistenceFailure:
    def test_a_state_root_inside_a_spec_tree_is_refused(self, project: Path) -> None:
        with pytest.raises(StatePersistenceError):
            StateStore(root=project / ".kiro" / "specs" / "example" / "state")

    def test_an_unopenable_database_fails_construction_and_writes_nothing(
        self, state_dir: Path, project: Path, ref: SpecRef
    ) -> None:
        before = spec_dir_snapshot(ref.spec_dir)
        # A directory where the database file belongs: SQLite cannot open it.
        (state_dir / "state.db").mkdir(parents=True)
        with pytest.raises(StatePersistenceError):
            StateStore(root=state_dir)
        assert spec_dir_snapshot(ref.spec_dir) == before
        assert set(before) == set(NATIVE_SPEC_FILES)

    def test_a_failing_write_fails_the_operation_without_touching_the_spec_dir(
        self, store: StateStore, ref: SpecRef, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        before = spec_dir_snapshot(ref.spec_dir)

        class _UnwritableConnection:
            """Stands in for a database that has become unwritable mid-operation."""

            def execute(self, *_args: Any, **_kwargs: Any) -> Any:
                raise sqlite_error("disk I/O error")

        monkeypatch.setattr(store, "_conn", lambda: _UnwritableConnection())

        for operation in (
            lambda: store.register_spec(ref, spec_type="feature"),
            lambda: store.record_approval(ref, gate="design", actor="user:ada", doc_hash="hash-1"),
            lambda: store.create_run("run-1", ref, state="queued"),
            lambda: store.claim_dispatch("github", "42"),
            lambda: store.acquire_lock(ref, owner="session:a"),
        ):
            with pytest.raises(StatePersistenceError):
                operation()

        assert spec_dir_snapshot(ref.spec_dir) == before
        assert set(before) == set(NATIVE_SPEC_FILES)

    def test_a_commit_that_fails_is_not_reported_as_a_successful_claim(
        self, store: StateStore, ref: SpecRef, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A claim's answer is computed before the commit that makes it true.

        ``claim`` returns ``cursor.rowcount == 1`` from inside the write block, so
        the value is decided while the insert is still uncommitted and only the
        commit failure propagating out of the transaction stops it being returned.
        That makes this the narrowest path in the store: a swallowed commit error
        hands back "you hold this claim" for a row that never landed, and the next
        poll claims the same key again -- so the exactly-once guarantee fails
        through the persistence one rather than through anything in the ledger.
        """
        real = store._conn()

        class _UncommittableConnection:
            """Accepts the work and refuses only to make it durable."""

            def execute(self, statement: str, *args: Any, **kwargs: Any) -> Any:
                if statement.strip().upper().startswith("COMMIT"):
                    raise sqlite_error("disk I/O error")
                return real.execute(statement, *args, **kwargs)

        monkeypatch.setattr(store, "_conn", lambda: _UncommittableConnection())

        with pytest.raises(StatePersistenceError):
            store.claim_dispatch("github", "42")

        # The claim must still be available: if the failed attempt were reported
        # as held, this second call would return False and the item would never
        # dispatch at all.
        monkeypatch.undo()
        assert store.claim_dispatch("github", "42") is True

    def test_a_statement_that_fails_mid_transaction_leaves_nothing_behind(
        self, store: StateStore, ref: SpecRef, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The other half of the write path: the transaction opened, so the failure
        # is a partial write to undo rather than a store that was never usable.
        real = store._conn()
        store.register_spec(ref, spec_type="feature")

        class _FailsAfterBegin:
            def execute(self, statement: str, *args: Any, **kwargs: Any) -> Any:
                head = statement.strip().upper()
                if head.startswith(("INSERT", "UPDATE", "DELETE")):
                    raise sqlite_error("database or disk is full")
                return real.execute(statement, *args, **kwargs)

        monkeypatch.setattr(store, "_conn", lambda: _FailsAfterBegin())

        with pytest.raises(StatePersistenceError):
            store.create_run("run-1", ref, state="queued")

        monkeypatch.undo()
        assert store.get_run("run-1") is None

    def test_a_failing_read_is_reported_rather_than_answered_with_a_guess(
        self, store: StateStore, ref: SpecRef, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _UnreadableConnection:
            def execute(self, *_args: Any, **_kwargs: Any) -> Any:
                raise sqlite_error("database disk image is malformed")

        monkeypatch.setattr(store, "_conn", lambda: _UnreadableConnection())
        with pytest.raises(StatePersistenceError):
            store.get_spec(ref)
        with pytest.raises(StatePersistenceError):
            store.current_state(ref)


def sqlite_error(message: str) -> Exception:
    """Build the error a real SQLite failure raises, without a live database."""
    from kiro_crew._sqlite_compat import sqlite3

    return sqlite3.OperationalError(message)
