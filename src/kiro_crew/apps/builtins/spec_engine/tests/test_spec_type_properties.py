"""Property-based tests for spec types and spec creation.

Three properties matter here, and each one has to hold for arbitrary inputs
rather than for the handful a scripted test happens to pick.

**Creation is all-or-nothing.** For any spec type, any name, and any failure
point, either the spec directory exists with its type recorded, or the specs tree
is exactly as it was. The state in between — a directory with no recorded type —
is what these tests exist to rule out.

**The plan comes from the recorded type.** Whatever a caller asked for, the plan
every later gate applies is derived by reading the sidecar back, so a
round-trip through the file is the only thing that decides it.

**Spec directory purity.** Creation adds the sidecar and nothing else. Staging
directories, temporary files, and placeholder documents all break the interop
contract with the Kiro IDE and CLI in different ways.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from kiro_crew.apps.builtins.spec_engine.engine import creation
from kiro_crew.apps.builtins.spec_engine.engine.creation import (
    SpecTypeNotRecorded,
    create_spec,
)
from kiro_crew.apps.builtins.spec_engine.engine.spec_types import (
    SIDECAR_FILENAME,
    SpecType,
    plan_for,
    read_sidecar,
    recorded_spec_type,
    write_sidecar,
)
from kiro_crew.apps.builtins.spec_engine.engine.state import (
    StatePersistenceError,
    StateStore,
)

#: Hypothesis examples per property. Each example creates a project tree and runs
#: SQLite transactions, so this buys breadth without making the suite slow.
MAX_EXAMPLES = 50

#: Names the engine accepts. Generated rather than sampled so the property covers
#: the whole accepted shape instead of three hand-picked spellings. Truncated
#: because a maximal name plus a temporary directory can approach a path limit.
_NAMES = st.from_regex(creation.NAME_PATTERN, fullmatch=True).map(lambda name: name[:40])

_SPEC_TYPES = st.sampled_from(list(SpecType))

#: Where creation can fail. Each point is reached after a different amount of
#: work, and the guarantee is the same at every one of them.
_FAILURE_POINTS = st.sampled_from(["sidecar", "move", "register"])


def _tree(project: Path) -> list[str]:
    root = project / ".kiro"
    if not root.exists():
        return []
    return sorted(str(path.relative_to(root)) for path in root.rglob("*"))


def _fresh(tmp_path: Path) -> tuple[Path, StateStore]:
    """A project and a state store that no other example has touched.

    Hypothesis reuses one ``tmp_path`` across every example of a test, so each
    example needs its own tree; sharing one would let an earlier example's spec
    decide a later example's outcome.
    """
    label = uuid.uuid4().hex[:12]
    project = tmp_path / f"project-{label}"
    project.mkdir(parents=True)
    return project, StateStore(root=tmp_path / f"state-{label}")


class TestCreationIsAllOrNothing:
    @settings(
        max_examples=MAX_EXAMPLES,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    @given(name=_NAMES, spec_type=_SPEC_TYPES)
    def test_a_created_spec_always_has_its_type_recorded(
        self, tmp_path: Path, name: str, spec_type: SpecType
    ) -> None:
        project, store = _fresh(tmp_path)
        try:
            created = create_spec(project, name, spec_type, store=store)
            assert recorded_spec_type(created.spec_dir) is spec_type
            assert plan_for(created.spec_dir) == spec_type.plan
            assert [path.name for path in created.spec_dir.iterdir()] == [SIDECAR_FILENAME]
        finally:
            store.close()

    @settings(
        max_examples=MAX_EXAMPLES,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    @given(name=_NAMES, spec_type=_SPEC_TYPES, failure=_FAILURE_POINTS)
    def test_a_failed_creation_leaves_no_directory_at_all(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        name: str,
        spec_type: SpecType,
        failure: str,
    ) -> None:
        project, store = _fresh(tmp_path)
        try:
            with monkeypatch.context() as patch:
                _inject(patch, store, failure)
                with pytest.raises(SpecTypeNotRecorded):
                    create_spec(project, name, spec_type, store=store)

            spec_dir = project / ".kiro" / "specs" / name
            assert not spec_dir.exists()
            # ".kiro/specs" is the container the engine may create; anything
            # deeper is a partial spec or staging litter.
            assert _tree(project) == ["specs"]
        finally:
            store.close()

    @settings(
        max_examples=MAX_EXAMPLES,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    @given(name=_NAMES, spec_type=_SPEC_TYPES, failure=_FAILURE_POINTS)
    def test_the_name_stays_usable_after_a_failure(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        name: str,
        spec_type: SpecType,
        failure: str,
    ) -> None:
        # A failed creation that reserved the name would be a half-created spec by
        # another route: the user could never retry without a manual repair.
        project, store = _fresh(tmp_path)
        try:
            with monkeypatch.context() as patch:
                _inject(patch, store, failure)
                with pytest.raises(SpecTypeNotRecorded):
                    create_spec(project, name, spec_type, store=store)

            created = create_spec(project, name, spec_type, store=store)
            assert recorded_spec_type(created.spec_dir) is spec_type
        finally:
            store.close()


def _inject(patch: pytest.MonkeyPatch, store: StateStore, failure: str) -> None:
    """Make one step of creation fail, leaving the others intact."""
    if failure == "sidecar":

        def refuse_write(*args: object, **kwargs: object) -> Path:
            raise OSError("injected write failure")

        patch.setattr(creation, "write_sidecar_document", refuse_write)
    elif failure == "move":

        def refuse_move(source: Path, target: Path) -> None:
            raise OSError("injected move failure")

        patch.setattr(creation, "_move_into_place", refuse_move)
    else:

        def refuse_register(*args: object, **kwargs: object) -> None:
            raise StatePersistenceError("injected persistence failure")

        patch.setattr(store, "register_spec", refuse_register)


class TestThePlanFollowsTheRecordedType:
    @settings(
        max_examples=MAX_EXAMPLES,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    @given(created_as=_SPEC_TYPES, changed_to=_SPEC_TYPES)
    def test_rewriting_the_sidecar_changes_the_plan(
        self, tmp_path: Path, created_as: SpecType, changed_to: SpecType
    ) -> None:
        project, store = _fresh(tmp_path)
        try:
            created = create_spec(project, "subject", created_as, store=store)
            write_sidecar(created.spec_dir, changed_to)

            # The registry still holds the type the spec was created with; the
            # sidecar is what the plan follows.
            assert plan_for(created.spec_dir) == changed_to.plan
            assert created.record.spec_type == created_as.value
            # Identity survives a change of weight.
            assert recorded_spec_type(created.spec_dir) is changed_to
        finally:
            store.close()

    @settings(
        max_examples=MAX_EXAMPLES,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    @given(spec_type=_SPEC_TYPES, rewrites=st.integers(min_value=1, max_value=4))
    def test_recording_a_type_repeatedly_is_stable(
        self, tmp_path: Path, spec_type: SpecType, rewrites: int
    ) -> None:
        project, store = _fresh(tmp_path)
        try:
            created = create_spec(project, "subject", spec_type, store=store)
            first = created.sidecar.spec_id
            for _ in range(rewrites):
                write_sidecar(created.spec_dir, spec_type)
            assert recorded_spec_type(created.spec_dir) is spec_type
            # A rewrite must not mint a new identity: anything already pointing
            # at this spec would then be pointing at nothing.
            assert read_sidecar(created.spec_dir).spec_id == first
            # And it must not leave a temporary file inside the spec directory.
            assert [path.name for path in created.spec_dir.iterdir()] == [SIDECAR_FILENAME]
        finally:
            store.close()
