"""Spec types, the plan each one implies, and the sidecar that records it.

The claim under test is that the plan comes from the *recorded* type. Reading it
back matters: a spec created in the IDE has a sidecar and no engine state, and a
sidecar someone hand-edited is the normal way a spec changes weight. So every
derivation here goes through the file, and a spec whose type cannot be read
refuses rather than falling back to the fullest plan.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew.apps.builtins.spec_engine.engine.config import SPEC_TYPES
from kiro_crew.apps.builtins.spec_engine.engine.documents import DocumentKind
from kiro_crew.apps.builtins.spec_engine.engine.spec_types import (
    PLANS,
    REASON_SIDECAR_MISSING,
    REASON_SIDECAR_UNREADABLE,
    REASON_TYPE_ABSENT,
    REASON_TYPE_UNKNOWN,
    REQUIREMENTS_FIRST_WORKFLOW,
    SIDECAR_FILENAME,
    SPEC_ID_KEY,
    SPEC_TYPE_KEY,
    WORKFLOW_TYPE_KEY,
    SpecType,
    SpecTypeUnrecorded,
    UnknownSpecType,
    build_sidecar,
    documents_on_disk,
    missing_documents,
    off_plan_documents,
    plan_for,
    plan_of,
    read_sidecar,
    recorded_spec_type,
    validate_spec_documents,
    write_sidecar,
)

from .conftest import NATIVE_SPEC_FILES

REQUIREMENTS_BODY = """# Requirements Document

## Introduction

A short introduction.

## Requirements

### Requirement 1: A capability

**User Story:** As a user, I want a capability, so that I get a benefit.

#### Acceptance Criteria

1. WHEN something happens, THE system SHALL respond.
"""

TASKS_BODY = """# Implementation Plan

## Tasks

- [ ] 1. Do the thing
  - _Requirements: 1.1_
"""


def write_sidecar_json(spec_dir: Path, document: object) -> None:
    """Write a sidecar verbatim, including shapes the engine would not produce."""
    spec_dir.mkdir(parents=True, exist_ok=True)
    (spec_dir / SIDECAR_FILENAME).write_text(json.dumps(document), encoding="utf-8")


@pytest.fixture()
def bare_spec_dir(tmp_path: Path) -> Path:
    """A spec directory with nothing in it yet."""
    spec_dir = tmp_path / "project" / ".kiro" / "specs" / "subject"
    spec_dir.mkdir(parents=True)
    return spec_dir


class TestDocumentPlans:
    def test_every_spec_type_has_a_plan(self) -> None:
        assert set(PLANS) == set(SpecType)

    def test_the_engine_and_the_config_vocabulary_agree(self) -> None:
        # Autonomy policy and intake guidance are keyed by spec type in config,
        # so a type the engine plans for and config rejects (or the reverse) is a
        # spec that can be created and never dispatched.
        assert tuple(spec_type.value for spec_type in SpecType) == SPEC_TYPES

    def test_a_feature_owes_all_three_documents(self) -> None:
        assert plan_of("feature").kinds == (
            DocumentKind.REQUIREMENTS,
            DocumentKind.DESIGN,
            DocumentKind.TASKS,
        )

    def test_a_bugfix_owes_the_same_files_with_different_jobs(self) -> None:
        feature = plan_of("feature")
        bugfix = plan_of("bugfix")
        # The filenames are the interop contract with the IDE and must not vary.
        assert bugfix.filenames == feature.filenames
        assert bugfix.label_for(DocumentKind.REQUIREMENTS) == "Bug analysis"
        assert bugfix.label_for(DocumentKind.DESIGN) == "Fix design"
        assert feature.label_for(DocumentKind.REQUIREMENTS) == "Requirements"

    def test_a_quick_plan_has_no_design_gate(self) -> None:
        plan = plan_of("quick")
        assert plan.kinds == (DocumentKind.REQUIREMENTS, DocumentKind.TASKS)
        assert not plan.includes(DocumentKind.DESIGN)
        assert plan.label_for(DocumentKind.DESIGN) == ""
        assert "design" not in plan.gates

    def test_every_plan_starts_at_requirements_and_ends_at_tasks(self) -> None:
        for plan in PLANS.values():
            assert plan.kinds[0] is DocumentKind.REQUIREMENTS
            assert plan.kinds[-1] is DocumentKind.TASKS

    def test_a_gate_is_named_after_its_document(self) -> None:
        assert plan_of("feature").gates == ("requirements", "design", "tasks")

    def test_an_unknown_type_has_no_plan(self) -> None:
        with pytest.raises(UnknownSpecType):
            plan_of("epic")

    def test_a_type_is_read_tolerantly_but_matched_strictly(self) -> None:
        assert SpecType.parse("  Feature ") is SpecType.FEATURE
        assert SpecType.parse("bug") is None
        assert SpecType.parse(None) is None
        assert SpecType.parse(3) is None


class TestReadingTheRecordedType:
    def test_the_plan_comes_from_the_sidecar(self, bare_spec_dir: Path) -> None:
        write_sidecar_json(bare_spec_dir, {SPEC_TYPE_KEY: "quick"})
        assert recorded_spec_type(bare_spec_dir) is SpecType.QUICK
        assert plan_for(bare_spec_dir).kinds == (DocumentKind.REQUIREMENTS, DocumentKind.TASKS)

    def test_editing_the_sidecar_changes_the_plan(self, bare_spec_dir: Path) -> None:
        write_sidecar_json(bare_spec_dir, {SPEC_TYPE_KEY: "quick"})
        assert plan_for(bare_spec_dir).spec_type is SpecType.QUICK
        write_sidecar_json(bare_spec_dir, {SPEC_TYPE_KEY: "feature"})
        assert plan_for(bare_spec_dir).spec_type is SpecType.FEATURE

    def test_foreign_keys_are_carried_not_dropped(self, bare_spec_dir: Path) -> None:
        write_sidecar_json(
            bare_spec_dir,
            {SPEC_ID_KEY: "abc", SPEC_TYPE_KEY: "feature", "somethingElse": {"a": 1}},
        )
        sidecar = read_sidecar(bare_spec_dir)
        assert sidecar.spec_id == "abc"
        assert sidecar.extra == {"somethingElse": {"a": 1}}

    def test_an_absent_workflow_reads_as_the_plans_own(self, bare_spec_dir: Path) -> None:
        write_sidecar_json(bare_spec_dir, {SPEC_TYPE_KEY: "feature"})
        assert read_sidecar(bare_spec_dir).workflow_type == REQUIREMENTS_FIRST_WORKFLOW

    @pytest.mark.parametrize(
        ("document", "reason"),
        [
            ({}, REASON_TYPE_ABSENT),
            ({SPEC_TYPE_KEY: "  "}, REASON_TYPE_ABSENT),
            ({SPEC_TYPE_KEY: None}, REASON_TYPE_ABSENT),
            ({SPEC_TYPE_KEY: "epic"}, REASON_TYPE_UNKNOWN),
            ({SPEC_TYPE_KEY: 7}, REASON_TYPE_UNKNOWN),
            (["feature"], REASON_SIDECAR_UNREADABLE),
        ],
    )
    def test_an_unusable_sidecar_names_why(
        self, bare_spec_dir: Path, document: object, reason: str
    ) -> None:
        write_sidecar_json(bare_spec_dir, document)
        with pytest.raises(SpecTypeUnrecorded) as raised:
            recorded_spec_type(bare_spec_dir)
        assert raised.value.reason == reason

    def test_a_missing_sidecar_is_reported_as_missing(self, bare_spec_dir: Path) -> None:
        with pytest.raises(SpecTypeUnrecorded) as raised:
            recorded_spec_type(bare_spec_dir)
        assert raised.value.reason == REASON_SIDECAR_MISSING

    def test_a_truncated_sidecar_is_reported_as_unreadable(self, bare_spec_dir: Path) -> None:
        (bare_spec_dir / SIDECAR_FILENAME).write_text('{"specType": "fea', encoding="utf-8")
        with pytest.raises(SpecTypeUnrecorded) as raised:
            recorded_spec_type(bare_spec_dir)
        assert raised.value.reason == REASON_SIDECAR_UNREADABLE


class TestWritingTheSidecar:
    def test_a_built_sidecar_carries_the_plans_workflow(self) -> None:
        sidecar = build_sidecar("bugfix")
        assert sidecar.spec_type is SpecType.BUGFIX
        assert sidecar.workflow_type == REQUIREMENTS_FIRST_WORKFLOW
        assert sidecar.spec_id

    def test_an_unknown_type_is_refused_before_anything_is_written(self) -> None:
        with pytest.raises(UnknownSpecType):
            build_sidecar("epic")

    def test_recording_a_type_round_trips(self, bare_spec_dir: Path) -> None:
        write_sidecar(bare_spec_dir, SpecType.QUICK)
        assert recorded_spec_type(bare_spec_dir) is SpecType.QUICK

    def test_the_written_sidecar_holds_the_native_keys(self, bare_spec_dir: Path) -> None:
        written = write_sidecar(bare_spec_dir, "feature", spec_id="fixed-id")
        stored = json.loads((bare_spec_dir / SIDECAR_FILENAME).read_text(encoding="utf-8"))
        assert stored == {
            SPEC_ID_KEY: "fixed-id",
            WORKFLOW_TYPE_KEY: REQUIREMENTS_FIRST_WORKFLOW,
            SPEC_TYPE_KEY: "feature",
        }
        assert written.spec_id == "fixed-id"

    def test_a_type_change_keeps_the_identity_and_foreign_keys(self, bare_spec_dir: Path) -> None:
        write_sidecar_json(
            bare_spec_dir,
            {SPEC_ID_KEY: "keep-me", SPEC_TYPE_KEY: "feature", "ideaOnly": True},
        )
        write_sidecar(bare_spec_dir, "quick")
        sidecar = read_sidecar(bare_spec_dir)
        assert sidecar.spec_id == "keep-me"
        assert sidecar.spec_type is SpecType.QUICK
        assert sidecar.extra == {"ideaOnly": True}

    def test_writing_leaves_no_temporary_file_behind(self, bare_spec_dir: Path) -> None:
        write_sidecar(bare_spec_dir, "feature")
        assert [path.name for path in bare_spec_dir.iterdir()] == [SIDECAR_FILENAME]

    def test_an_unreadable_sidecar_is_repaired_by_recording_a_type(
        self, bare_spec_dir: Path
    ) -> None:
        (bare_spec_dir / SIDECAR_FILENAME).write_text("not json at all", encoding="utf-8")
        write_sidecar(bare_spec_dir, "bugfix")
        assert recorded_spec_type(bare_spec_dir) is SpecType.BUGFIX


class TestApplyingThePlan:
    def test_documents_on_disk_are_reported_by_kind(self, bare_spec_dir: Path) -> None:
        (bare_spec_dir / "requirements.md").write_text(REQUIREMENTS_BODY, encoding="utf-8")
        assert set(documents_on_disk(bare_spec_dir)) == {DocumentKind.REQUIREMENTS}

    def test_missing_documents_follow_the_recorded_plan(self, bare_spec_dir: Path) -> None:
        write_sidecar_json(bare_spec_dir, {SPEC_TYPE_KEY: "quick"})
        (bare_spec_dir / "requirements.md").write_text(REQUIREMENTS_BODY, encoding="utf-8")
        # A quick spec never owes a design document, so the only gap is tasks.
        assert missing_documents(bare_spec_dir) == (DocumentKind.TASKS,)

    def test_a_feature_still_owes_its_design_document(self, bare_spec_dir: Path) -> None:
        write_sidecar_json(bare_spec_dir, {SPEC_TYPE_KEY: "feature"})
        (bare_spec_dir / "requirements.md").write_text(REQUIREMENTS_BODY, encoding="utf-8")
        assert missing_documents(bare_spec_dir) == (DocumentKind.DESIGN, DocumentKind.TASKS)

    def test_an_off_plan_document_is_surfaced_not_validated(self, bare_spec_dir: Path) -> None:
        write_sidecar_json(bare_spec_dir, {SPEC_TYPE_KEY: "quick"})
        (bare_spec_dir / "requirements.md").write_text(REQUIREMENTS_BODY, encoding="utf-8")
        (bare_spec_dir / "tasks.md").write_text(TASKS_BODY, encoding="utf-8")
        (bare_spec_dir / "design.md").write_text("junk with no headings", encoding="utf-8")
        assert off_plan_documents(bare_spec_dir) == (DocumentKind.DESIGN,)
        report = validate_spec_documents(bare_spec_dir)
        assert report.ok
        assert not report.for_file(str(bare_spec_dir / "design.md"))

    def test_a_planned_document_is_validated(self, bare_spec_dir: Path) -> None:
        write_sidecar_json(bare_spec_dir, {SPEC_TYPE_KEY: "quick"})
        (bare_spec_dir / "requirements.md").write_text("# Wrong Title\n", encoding="utf-8")
        report = validate_spec_documents(bare_spec_dir)
        assert not report.ok
        assert report.for_file(str(bare_spec_dir / "requirements.md"))

    def test_an_absent_planned_document_is_not_a_violation(self, bare_spec_dir: Path) -> None:
        # Mid-authoring is the normal state of a spec; only the phase machine
        # decides whether a gap should refuse a gate.
        write_sidecar_json(bare_spec_dir, {SPEC_TYPE_KEY: "feature"})
        (bare_spec_dir / "requirements.md").write_text(REQUIREMENTS_BODY, encoding="utf-8")
        assert validate_spec_documents(bare_spec_dir).ok


class TestNoTypeNoGate:
    """Without a recorded type, nothing that depends on the plan may proceed."""

    def test_validation_is_refused(self, bare_spec_dir: Path) -> None:
        (bare_spec_dir / "requirements.md").write_text(REQUIREMENTS_BODY, encoding="utf-8")
        with pytest.raises(SpecTypeUnrecorded):
            validate_spec_documents(bare_spec_dir)

    def test_the_document_plan_is_refused(self, bare_spec_dir: Path) -> None:
        with pytest.raises(SpecTypeUnrecorded):
            plan_for(bare_spec_dir)

    def test_gap_and_off_plan_reporting_are_refused(self, bare_spec_dir: Path) -> None:
        with pytest.raises(SpecTypeUnrecorded):
            missing_documents(bare_spec_dir)
        with pytest.raises(SpecTypeUnrecorded):
            off_plan_documents(bare_spec_dir)

    def test_an_unknown_recorded_type_is_refused_rather_than_rounded(
        self, bare_spec_dir: Path
    ) -> None:
        # Rounding "epic" to the feature plan would hold the spec to a document
        # set nobody chose, and it would pass, which is the worse outcome.
        write_sidecar_json(bare_spec_dir, {SPEC_TYPE_KEY: "epic"})
        with pytest.raises(SpecTypeUnrecorded) as raised:
            validate_spec_documents(bare_spec_dir)
        assert raised.value.reason == REASON_TYPE_UNKNOWN


class TestArtifactShape:
    def test_this_repositorys_own_spec_records_its_type(self) -> None:
        """The engine reads the sidecar of the spec that specified it.

        A real artifact is the only check that the shape written here is the
        shape the IDE actually keeps; a fixture only proves this module agrees
        with itself.
        """
        # tests -> spec_engine -> builtins -> apps -> kiro_crew -> src -> repository
        specs_root = Path(__file__).resolve().parents[6] / ".kiro" / "specs"
        candidates = sorted(
            path.parent for path in specs_root.glob(f"*/{SIDECAR_FILENAME}") if path.is_file()
        )
        if not candidates:
            pytest.skip("no spec artifacts in this checkout")
        for candidate in candidates:
            sidecar = read_sidecar(candidate)
            assert sidecar.spec_type in set(SpecType)
            assert sidecar.spec_id
            assert set(sidecar.plan.filenames) <= set(NATIVE_SPEC_FILES)
