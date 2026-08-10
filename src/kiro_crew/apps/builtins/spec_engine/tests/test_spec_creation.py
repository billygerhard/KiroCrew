"""Spec creation: the type is recorded, or nothing is created.

The failure mode under test is the half-created spec — a directory with no
recorded type. It is unusable (the engine refuses to validate or advance it) but
it looks created, so it surfaces later as a confusing refusal instead of a clear
failure at the moment of creation. Every failure path here is therefore asserted
against the filesystem: after the call, the specs tree must look untouched.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.apps.builtins.spec_engine.engine import creation
from kiro_crew.apps.builtins.spec_engine.engine.creation import (
    InvalidSpecName,
    SpecAlreadyExists,
    SpecTypeNotRecorded,
    create_spec,
    validate_spec_name,
)
from kiro_crew.apps.builtins.spec_engine.engine.documents import DocumentKind
from kiro_crew.apps.builtins.spec_engine.engine.spec_types import (
    SIDECAR_FILENAME,
    SPEC_ID_KEY,
    SPEC_TYPE_KEY,
    WORKFLOW_TYPE_KEY,
    SpecType,
    SpecTypeUnrecorded,
    UnknownSpecType,
    plan_for,
    read_sidecar,
    recorded_spec_type,
    validate_spec_documents,
)
from kiro_crew.apps.builtins.spec_engine.engine.state import (
    SpecLocked,
    SpecRef,
    StatePersistenceError,
    StateStore,
)


@pytest.fixture()
def empty_project(tmp_path: Path) -> Path:
    """A project tree with no specs in it."""
    root = tmp_path / "workspace"
    root.mkdir()
    return root


def specs_root(project: Path) -> Path:
    return project / ".kiro" / "specs"


def tree_entries(project: Path) -> list[str]:
    """Every path under ``.kiro``, so staging litter is visible too."""
    root = project / ".kiro"
    if not root.exists():
        return []
    return sorted(str(path.relative_to(root)) for path in root.rglob("*"))


class TestCreatingASpec:
    def test_creation_records_the_type_in_the_sidecar(
        self, empty_project: Path, store: StateStore
    ) -> None:
        created = create_spec(empty_project, "new-thing", "bugfix", store=store)
        assert created.spec_dir == specs_root(empty_project) / "new-thing"
        assert recorded_spec_type(created.spec_dir) is SpecType.BUGFIX

    def test_the_sidecar_carries_the_native_keys(
        self, empty_project: Path, store: StateStore
    ) -> None:
        created = create_spec(empty_project, "new-thing", "feature", store=store)
        stored: dict[str, Any] = json.loads(
            (created.spec_dir / SIDECAR_FILENAME).read_text(encoding="utf-8")
        )
        assert set(stored) == {SPEC_ID_KEY, WORKFLOW_TYPE_KEY, SPEC_TYPE_KEY}
        assert stored[SPEC_TYPE_KEY] == "feature"
        assert stored[SPEC_ID_KEY] == created.sidecar.spec_id

    def test_creation_writes_the_sidecar_and_nothing_else(
        self, empty_project: Path, store: StateStore
    ) -> None:
        # No placeholder documents: an empty requirements.md would make a new
        # spec look like one whose requirements had already been authored.
        created = create_spec(empty_project, "new-thing", "feature", store=store)
        assert [path.name for path in created.spec_dir.iterdir()] == [SIDECAR_FILENAME]

    def test_creation_leaves_no_staging_directory_behind(
        self, empty_project: Path, store: StateStore
    ) -> None:
        create_spec(empty_project, "new-thing", "quick", store=store)
        assert tree_entries(empty_project) == [
            "specs",
            "specs/new-thing",
            f"specs/new-thing/{SIDECAR_FILENAME}",
        ]

    def test_the_plan_is_derived_from_what_was_recorded(
        self, empty_project: Path, store: StateStore
    ) -> None:
        created = create_spec(empty_project, "small-change", "quick", store=store)
        # Read back through the sidecar rather than trusting the argument: the
        # recorded type is what every later gate will apply.
        assert plan_for(created.spec_dir).kinds == (DocumentKind.REQUIREMENTS, DocumentKind.TASKS)
        assert created.plan == plan_for(created.spec_dir)

    @pytest.mark.parametrize("spec_type", [t.value for t in SpecType])
    def test_each_type_round_trips_into_its_plan(
        self, empty_project: Path, store: StateStore, spec_type: str
    ) -> None:
        created = create_spec(empty_project, f"spec-{spec_type}", spec_type, store=store)
        assert created.spec_type is SpecType(spec_type)
        assert plan_for(created.spec_dir) == SpecType(spec_type).plan

    def test_the_spec_is_registered_with_its_type(
        self, empty_project: Path, store: StateStore
    ) -> None:
        created = create_spec(empty_project, "new-thing", "bugfix", store=store)
        record = store.get_spec(SpecRef.of(empty_project, "new-thing"))
        assert record is not None
        assert record.spec_type == "bugfix"
        assert created.record.spec_type == "bugfix"

    def test_a_spec_type_object_is_accepted_as_well_as_its_name(
        self, empty_project: Path, store: StateStore
    ) -> None:
        created = create_spec(empty_project, "new-thing", SpecType.QUICK, store=store)
        assert created.spec_type is SpecType.QUICK

    def test_a_created_spec_validates_under_its_plan(
        self, empty_project: Path, store: StateStore
    ) -> None:
        # A spec with a type but no documents yet is valid and empty, not broken:
        # the plan resolves, and there is nothing to validate.
        created = create_spec(empty_project, "new-thing", "feature", store=store)
        assert validate_spec_documents(created.spec_dir).ok

    def test_the_lock_is_released_afterwards(self, empty_project: Path, store: StateStore) -> None:
        create_spec(empty_project, "new-thing", "feature", store=store)
        state = store.current_state(SpecRef.of(empty_project, "new-thing"))
        assert state["lock"] is None


class TestRefusals:
    def test_an_unknown_type_creates_nothing(self, empty_project: Path, store: StateStore) -> None:
        with pytest.raises(UnknownSpecType):
            create_spec(empty_project, "new-thing", "epic", store=store)
        assert tree_entries(empty_project) == []

    def test_an_unknown_type_does_not_even_register_the_spec(
        self, empty_project: Path, store: StateStore
    ) -> None:
        with pytest.raises(UnknownSpecType):
            create_spec(empty_project, "new-thing", "epic", store=store)
        assert store.get_spec(SpecRef.of(empty_project, "new-thing")) is None

    @pytest.mark.parametrize("name", ["", "   ", " padded", "with/slash", ".hidden", "a" * 65])
    def test_an_unusable_name_creates_nothing(
        self, empty_project: Path, store: StateStore, name: str
    ) -> None:
        with pytest.raises(InvalidSpecName):
            create_spec(empty_project, name, "feature", store=store)
        assert tree_entries(empty_project) == []

    def test_a_valid_name_is_returned_unchanged(self) -> None:
        assert validate_spec_name("Agent_agnostic-spec-2") == "Agent_agnostic-spec-2"

    def test_an_existing_spec_is_never_adopted_or_overwritten(
        self, empty_project: Path, store: StateStore
    ) -> None:
        created = create_spec(empty_project, "new-thing", "feature", store=store)
        (created.spec_dir / "requirements.md").write_text("# Requirements Document\n", "utf-8")
        before = sorted(path.name for path in created.spec_dir.iterdir())

        with pytest.raises(SpecAlreadyExists):
            create_spec(empty_project, "new-thing", "quick", store=store)

        assert sorted(path.name for path in created.spec_dir.iterdir()) == before
        assert recorded_spec_type(created.spec_dir) is SpecType.FEATURE

    def test_an_existing_directory_with_no_sidecar_is_still_not_adopted(
        self, empty_project: Path, store: StateStore
    ) -> None:
        # The repair path for a directory with no recorded type is a deliberate
        # one, not a creation call that silently writes into it.
        spec_dir = specs_root(empty_project) / "orphan"
        spec_dir.mkdir(parents=True)
        with pytest.raises(SpecAlreadyExists):
            create_spec(empty_project, "orphan", "feature", store=store)
        assert list(spec_dir.iterdir()) == []

    def test_a_second_concurrent_writer_is_rejected_with_the_current_state(
        self, empty_project: Path, store: StateStore
    ) -> None:
        ref = SpecRef.of(empty_project, "contended")
        with store.lock(ref, owner="the-other-writer"):
            with pytest.raises(SpecLocked) as raised:
                create_spec(empty_project, "contended", "feature", store=store)
        assert raised.value.state["name"] == "contended"
        assert not (specs_root(empty_project) / "contended").exists()


class TestAtomicity:
    """An unrecordable type leaves no partial directory behind."""

    def test_a_sidecar_that_cannot_be_written_creates_nothing(
        self, empty_project: Path, store: StateStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def refuse(*args: object, **kwargs: object) -> Path:
            raise OSError("no space left on device")

        monkeypatch.setattr(creation, "write_sidecar_document", refuse)

        with pytest.raises(SpecTypeNotRecorded):
            create_spec(empty_project, "doomed", "feature", store=store)

        assert not (specs_root(empty_project) / "doomed").exists()
        # Including the staging directory: leftover staging is litter in the
        # user's project, and a second attempt would accumulate more of it.
        assert tree_entries(empty_project) == ["specs"]

    def test_a_move_that_cannot_complete_creates_nothing(
        self, empty_project: Path, store: StateStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def refuse(source: Path, target: Path) -> None:
            raise OSError("cross-device link")

        monkeypatch.setattr(creation, "_move_into_place", refuse)

        with pytest.raises(SpecTypeNotRecorded):
            create_spec(empty_project, "doomed", "feature", store=store)

        assert not (specs_root(empty_project) / "doomed").exists()
        assert tree_entries(empty_project) == ["specs"]

    def test_unpersistable_engine_state_undoes_the_directory(
        self, empty_project: Path, store: StateStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def refuse(*args: object, **kwargs: object) -> None:
            raise StatePersistenceError("the state database is unwritable")

        monkeypatch.setattr(store, "register_spec", refuse)

        with pytest.raises(SpecTypeNotRecorded):
            create_spec(empty_project, "doomed", "feature", store=store)

        assert not (specs_root(empty_project) / "doomed").exists()
        assert tree_entries(empty_project) == ["specs"]

    def test_a_failed_creation_leaves_the_name_free(
        self, empty_project: Path, store: StateStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = {"n": 0}
        real = creation.write_sidecar_document

        def fail_once(spec_dir: Path, document: Any) -> Path:
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("transient")
            return real(spec_dir, document)

        monkeypatch.setattr(creation, "write_sidecar_document", fail_once)

        with pytest.raises(SpecTypeNotRecorded):
            create_spec(empty_project, "retried", "feature", store=store)
        created = create_spec(empty_project, "retried", "quick", store=store)
        assert recorded_spec_type(created.spec_dir) is SpecType.QUICK

    def test_rollback_never_removes_content_it_did_not_write(
        self, empty_project: Path, store: StateStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A rollback deletes inside the user's project, so it is conservative.

        If anything unexpected is in the directory, it stays. A refusal that also
        deletes someone's file is a worse outcome than a directory left behind,
        and the "no recorded type" refusal already makes that directory harmless.
        """
        spec_dir = specs_root(empty_project) / "raced"

        def refuse(*args: object, **kwargs: object) -> None:
            (spec_dir / "authored.md").write_text("a racing writer's work", encoding="utf-8")
            raise StatePersistenceError("the state database is unwritable")

        monkeypatch.setattr(store, "register_spec", refuse)

        with pytest.raises(SpecTypeNotRecorded):
            create_spec(empty_project, "raced", "feature", store=store)

        assert (spec_dir / "authored.md").is_file()

    def test_a_directory_with_no_recorded_type_gates_nothing(
        self, empty_project: Path, store: StateStore
    ) -> None:
        """The backstop for a crash the rollback could not reach.

        A process killed between the move and the sidecar write cannot be undone
        by any handler, so the surviving directory must be inert: no plan, no
        validation, no advancement.
        """
        spec_dir = specs_root(empty_project) / "half-created"
        spec_dir.mkdir(parents=True)
        (spec_dir / "requirements.md").write_text("# Requirements Document\n", encoding="utf-8")

        with pytest.raises(SpecTypeUnrecorded):
            plan_for(spec_dir)
        with pytest.raises(SpecTypeUnrecorded):
            validate_spec_documents(spec_dir)

    def test_a_registry_row_alone_grants_no_type(
        self, empty_project: Path, store: StateStore
    ) -> None:
        """The sidecar is the authority; a state row cannot stand in for it.

        Taking the spec's lock registers a row, so a failed creation can leave one
        behind. If that row could answer "what type is this spec?", a spec with no
        sidecar would validate against a plan the IDE knows nothing about.
        """
        ref = SpecRef.of(empty_project, "row-only")
        store.register_spec(ref, spec_type="feature")
        ref.spec_dir.mkdir(parents=True)

        with pytest.raises(SpecTypeUnrecorded):
            recorded_spec_type(ref.spec_dir)


class TestSidecarAuthority:
    def test_a_spec_created_outside_the_engine_is_readable(
        self, empty_project: Path, store: StateStore
    ) -> None:
        """A spec the IDE created has a sidecar and no engine state.

        Deriving the plan from the sidecar rather than from the registry is what
        makes that spec work, and it is the common case for a project that used
        the IDE before the engine.
        """
        spec_dir = specs_root(empty_project) / "from-the-ide"
        spec_dir.mkdir(parents=True)
        (spec_dir / SIDECAR_FILENAME).write_text(
            json.dumps(
                {
                    SPEC_ID_KEY: "19cfc576-473e-4ff5-9b0d-0121de66eb0e",
                    WORKFLOW_TYPE_KEY: "requirements-first",
                    SPEC_TYPE_KEY: "feature",
                }
            ),
            encoding="utf-8",
        )

        assert store.get_spec(SpecRef.of(empty_project, "from-the-ide")) is None
        assert read_sidecar(spec_dir).spec_type is SpecType.FEATURE
        assert plan_for(spec_dir).kinds == (
            DocumentKind.REQUIREMENTS,
            DocumentKind.DESIGN,
            DocumentKind.TASKS,
        )
