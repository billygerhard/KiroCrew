"""The phase machine: derivation, the advancement gate, and approval staleness.

Three claims carry most of the weight here.

Derivation is a read. Asking where a spec sits opens documents and reads approval
rows and changes nothing -- not the approval table, not the cached phase, not even
the spec's registry row. The test proves it by deriving against a spec the state
store has never seen and showing the store still has never seen it.

A refusal is actionable. Every refused advancement names its reason, and a
refusal caused by an invalid document carries the validator's own rule
identifiers rather than prose a caller would have to parse.

Staleness is exact. Approving two documents and editing one invalidates one
approval. That precision is the whole point: blanket invalidation would train
users to re-approve reflexively, and no invalidation would let an edit slip past
a gate that had already been passed.

The realistic case throughout is this repository's own spec, whose three
documents are real, format-clean, and hand-authored.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

import pytest

from kiro_crew.apps.builtins.spec_engine.engine import rules
from kiro_crew.apps.builtins.spec_engine.engine.audit import AuditLog
from kiro_crew.apps.builtins.spec_engine.engine.documents import DocumentKind
from kiro_crew.apps.builtins.spec_engine.engine.phases import (
    APPROVAL_RECORDED_EVENT,
    APPROVAL_STALED_EVENT,
    CONTENT_HASH_PREFIX,
    PHASE_ADVANCE_REFUSED_EVENT,
    REASON_ALREADY_FINAL,
    REASON_APPROVAL_MISSING,
    REASON_APPROVAL_STALE,
    REASON_DOCUMENT_INVALID,
    REASON_DOCUMENT_MISSING,
    REASON_GATE_NOT_IN_PLAN,
    REASON_SPEC_TYPE_UNRECORDED,
    Phase,
    advance,
    approve,
    content_hash,
    derive_phase,
    document_plan,
    read_document,
    recorded_spec_type,
    sync_staleness,
)
from kiro_crew.apps.builtins.spec_engine.engine.spec_types import SpecType
from kiro_crew.apps.builtins.spec_engine.engine.state import (
    LockLost,
    SpecLocked,
    SpecRef,
    StateStore,
)

from .conftest import spec_dir_snapshot

#: This repository's own spec. Its documents are format-clean, so they stand in
#: for what a real authored spec looks like rather than what a fixture allows.
_REPO_SPEC_DIR = (
    # tests -> spec_engine -> builtins -> apps -> kiro_crew -> src -> repository
    Path(__file__).resolve().parents[6]
    / ".kiro"
    / "specs"
    / "agent-agnostic-spec-engine"
)

SPEC_NAME = "agent-agnostic-spec-engine"

#: An appended trailing comment: it changes the document's content without
#: breaking its format, so an edit-driven test isolates staleness from validity.
EDIT_MARKER = "\n<!-- reviewed -->\n"


def live_document_text(kind: DocumentKind) -> str:
    path = _REPO_SPEC_DIR / kind.filename
    if not path.is_file():
        pytest.skip(f"{path} is not present in this checkout")
    return path.read_text(encoding="utf-8")


def write_spec(
    root: Path,
    *,
    name: str = SPEC_NAME,
    kinds: tuple[DocumentKind, ...] = tuple(DocumentKind),
    spec_type: str | None = "feature",
) -> Path:
    """Materialise a spec directory holding real documents, and return its project."""
    spec_dir = root / ".kiro" / "specs" / name
    spec_dir.mkdir(parents=True, exist_ok=True)
    for kind in kinds:
        (spec_dir / kind.filename).write_text(live_document_text(kind), encoding="utf-8")
    if spec_type is not None:
        (spec_dir / ".config.kiro").write_text(
            json.dumps({"specId": name, "specType": spec_type}), encoding="utf-8"
        )
    return root


def edit(project: Path, kind: DocumentKind, *, name: str = SPEC_NAME) -> None:
    """Append a formatting-neutral line to one document."""
    path = project / ".kiro" / "specs" / name / kind.filename
    path.write_text(path.read_text(encoding="utf-8") + EDIT_MARKER, encoding="utf-8")


@pytest.fixture()
def live_project(tmp_path: Path) -> Path:
    return write_spec(tmp_path / "workspace")


@pytest.fixture()
def live_ref(live_project: Path) -> SpecRef:
    return SpecRef.of(live_project, SPEC_NAME)


@pytest.fixture()
def live_store(tmp_path: Path) -> Iterator[StateStore]:
    store = StateStore(root=tmp_path / "state")
    yield store
    store.close()


@pytest.fixture()
def log(tmp_path: Path) -> AuditLog:
    return AuditLog(root=tmp_path / "state")


def approve_gates(store: StateStore, ref: SpecRef, *gates: str, actor: str = "user:ada") -> None:
    for gate in gates:
        outcome = approve(store, ref, gate, actor=actor)
        assert outcome.ok, [str(reason) for reason in outcome.reasons]


# --- The document plan -----------------------------------------------------


def test_the_document_plan_comes_from_the_spec_type_table():
    """One table of plans in the engine, and this is the phase machine's view.

    A second table here would be a place for the gates to disagree with what
    validation and authoring apply to the same spec.
    """
    for spec_type in SpecType:
        assert document_plan(spec_type.value) == spec_type.plan.kinds

    assert document_plan("quick") == (DocumentKind.REQUIREMENTS, DocumentKind.TASKS)
    assert DocumentKind.DESIGN not in document_plan("quick")
    assert document_plan(None) == ()
    assert document_plan("unheard-of") == ()


def test_a_bugfix_plan_uses_the_same_documents_as_a_feature_plan():
    """Bug analysis and fix design are content, not extra files."""
    assert document_plan("bugfix") == document_plan("feature")


# --- Derivation ------------------------------------------------------------


def test_derivation_walks_the_plan_as_gates_are_approved(live_store, live_ref):
    """Every document exists, so only approvals move the phase forward."""
    assert derive_phase(live_store, live_ref).phase is Phase.REQUIREMENTS

    approve_gates(live_store, live_ref, "requirements")
    assert derive_phase(live_store, live_ref).phase is Phase.DESIGN

    approve_gates(live_store, live_ref, "design")
    assert derive_phase(live_store, live_ref).phase is Phase.TASKS

    approve_gates(live_store, live_ref, "tasks")
    state = derive_phase(live_store, live_ref)
    assert state.phase is Phase.READY
    assert state.is_final
    assert state.current_gate is None


def test_an_unwritten_document_holds_the_phase_at_its_own_gate(tmp_path, live_store):
    project = write_spec(tmp_path / "partial", kinds=(DocumentKind.REQUIREMENTS,))
    ref = SpecRef.of(project, SPEC_NAME)

    assert derive_phase(live_store, ref).phase is Phase.REQUIREMENTS
    approve_gates(live_store, ref, "requirements")

    state = derive_phase(live_store, ref)
    assert state.phase is Phase.DESIGN
    design = state.gate_named("design")
    assert design is not None and not design.present
    assert design.content_hash is None


def test_a_whitespace_only_document_counts_as_unwritten(live_project, live_store, live_ref):
    """A touched placeholder is not a drafted document."""
    path = live_project / ".kiro" / "specs" / SPEC_NAME / DocumentKind.DESIGN.filename
    path.write_text("   \n\n", encoding="utf-8")

    assert read_document(path.parent, DocumentKind.DESIGN) is None
    design = derive_phase(live_store, live_ref).gate_named("design")
    assert design is not None and not design.present


def test_derivation_writes_nothing_at_all(live_project, live_store, live_ref):
    """A caller asking where a spec sits must not be able to move it."""
    spec_dir = live_ref.spec_dir
    documents_before = spec_dir_snapshot(spec_dir)
    approve_gates(live_store, live_ref, "requirements")
    state_before = live_store.current_state(live_ref)
    cached_phase_before = live_store.get_spec(live_ref).phase

    for _ in range(3):
        derive_phase(live_store, live_ref)

    assert spec_dir_snapshot(spec_dir) == documents_before
    assert live_store.current_state(live_ref) == state_before
    assert live_store.get_spec(live_ref).phase == cached_phase_before


def test_derivation_does_not_register_a_spec_it_has_never_seen(live_store, live_ref):
    """The strongest form of read-only: no row appears where there was none."""
    assert live_store.get_spec(live_ref) is None

    state = derive_phase(live_store, live_ref)

    assert state.spec_type == "feature"  # read from the native sidecar
    assert live_store.get_spec(live_ref) is None
    assert live_store.list_specs(include_archived=True) == []


def test_the_sidecar_wins_over_a_disagreeing_store_row(live_store, live_ref):
    """The sidecar records the type; the store's column only mirrors it.

    The IDE or a user can retype a spec by editing the sidecar, leaving the row
    behind. Reading the row would derive the other type's plan -- the wrong
    gates, and with them the wrong advancement decision.
    """
    assert recorded_spec_type(live_ref) == "feature"
    live_store.register_spec(live_ref, spec_type="quick")

    assert recorded_spec_type(live_ref) == "feature"
    gates = [gate.gate for gate in derive_phase(live_store, live_ref).gates]
    assert gates == ["requirements", "design", "tasks"]


def test_a_store_row_cannot_supply_a_type_the_sidecar_does_not_record(tmp_path, live_store):
    """A mirror of a type nobody recorded is not a document plan."""
    project = write_spec(tmp_path / "mirror-only", spec_type=None)
    ref = SpecRef.of(project, SPEC_NAME)
    live_store.register_spec(ref, spec_type="feature")

    assert recorded_spec_type(ref) is None
    assert derive_phase(live_store, ref).is_untyped

    result = advance(live_store, ref, actor="user:ada")
    assert not result.ok
    assert result.reason_codes == (REASON_SPEC_TYPE_UNRECORDED,)


def test_a_spec_with_no_recorded_type_derives_untyped(tmp_path, live_store):
    project = write_spec(tmp_path / "untyped", spec_type=None)
    ref = SpecRef.of(project, SPEC_NAME)

    state = derive_phase(live_store, ref)

    assert state.phase is Phase.UNTYPED
    assert state.is_untyped
    assert state.gates == ()
    assert state.spec_type is None


# --- Approval recording ----------------------------------------------------


def test_an_approval_persists_its_approver_and_timestamp(live_store, live_ref):
    outcome = approve(live_store, live_ref, "requirements", actor="user:grace")

    assert outcome.ok
    approval = outcome.approval
    assert approval is not None
    assert approval.actor == "user:grace"
    assert approval.approved_ts.endswith("+00:00")
    assert approval.doc_hash.startswith(CONTENT_HASH_PREFIX)
    assert not approval.stale

    stored = live_store.get_approval(live_ref, "requirements")
    assert stored is not None
    assert (stored.actor, stored.approved_ts, stored.doc_hash) == (
        approval.actor,
        approval.approved_ts,
        approval.doc_hash,
    )


def test_the_recorded_hash_is_the_hash_of_the_approved_document(live_store, live_ref):
    outcome = approve(live_store, live_ref, "requirements", actor="user:ada")

    expected = content_hash(live_document_text(DocumentKind.REQUIREMENTS))
    assert outcome.approval is not None
    assert outcome.approval.doc_hash == expected


def test_approving_an_unwritten_document_is_refused(tmp_path, live_store):
    project = write_spec(tmp_path / "partial", kinds=(DocumentKind.REQUIREMENTS,))
    ref = SpecRef.of(project, SPEC_NAME)

    outcome = approve(live_store, ref, "design", actor="user:ada")

    assert not outcome.ok
    assert [reason.code for reason in outcome.reasons] == [REASON_DOCUMENT_MISSING]
    assert live_store.get_approval(ref, "design") is None


def test_approving_a_document_that_fails_validation_is_refused(tmp_path, live_store, project):
    """An approval of an invalid document would advance a spec the gate would stop."""
    ref = SpecRef.of(project, "example")  # conftest writes titles with no sections

    outcome = approve(live_store, ref, "requirements", actor="user:ada")

    assert not outcome.ok
    assert [reason.code for reason in outcome.reasons] == [REASON_DOCUMENT_INVALID]
    assert rules.SECTION_MISSING in outcome.reasons[0].rule_ids
    assert live_store.list_approvals(ref) == []


def test_approving_a_gate_outside_the_plan_is_refused(tmp_path, live_store):
    project = write_spec(tmp_path / "quick", spec_type="quick")
    ref = SpecRef.of(project, SPEC_NAME)

    outcome = approve(live_store, ref, "design", actor="user:ada")

    assert not outcome.ok
    assert [reason.code for reason in outcome.reasons] == [REASON_GATE_NOT_IN_PLAN]
    assert "requirements, tasks" in outcome.reasons[0].message


def test_approving_a_spec_with_no_recorded_type_is_refused(tmp_path, live_store):
    project = write_spec(tmp_path / "untyped", spec_type=None)
    ref = SpecRef.of(project, SPEC_NAME)

    outcome = approve(live_store, ref, "requirements", actor="user:ada")

    assert not outcome.ok
    assert [reason.code for reason in outcome.reasons] == [REASON_SPEC_TYPE_UNRECORDED]


def test_re_approval_replaces_the_stale_record(live_project, live_store, live_ref):
    approve_gates(live_store, live_ref, "requirements", actor="user:ada")
    edit(live_project, DocumentKind.REQUIREMENTS)
    sync_staleness(live_store, live_ref)
    assert live_store.get_approval(live_ref, "requirements").stale

    outcome = approve(live_store, live_ref, "requirements", actor="user:grace")

    assert outcome.ok
    stored = live_store.get_approval(live_ref, "requirements")
    assert not stored.stale
    assert stored.actor == "user:grace"
    assert derive_phase(live_store, live_ref).stale_gates == ()


def test_an_approval_is_recorded_in_the_audit_log(live_store, live_ref, log):
    approve(live_store, live_ref, "requirements", actor="user:ada", audit=log)

    events = log.read(live_ref)
    assert [event.event for event in events] == [APPROVAL_RECORDED_EVENT]
    assert events[0].initiator == "user:ada"
    assert events[0].detail["gate"] == "requirements"


# --- Approval staleness ----------------------------------------------------


def test_editing_one_approved_document_stales_exactly_that_approval(
    live_project, live_store, live_ref
):
    """The precision claim: two approvals, one edit, one casualty."""
    approve_gates(live_store, live_ref, "requirements", "design")

    edit(live_project, DocumentKind.DESIGN)

    assert sync_staleness(live_store, live_ref) == ("design",)
    assert live_store.get_approval(live_ref, "design").stale
    assert not live_store.get_approval(live_ref, "requirements").stale

    state = derive_phase(live_store, live_ref)
    assert state.stale_gates == ("design",)
    assert state.phase is Phase.DESIGN  # the edit walked the phase back


def test_editing_the_other_approved_document_stales_only_it(live_project, live_store, live_ref):
    """The mirror case, so the precision is not an accident of gate order."""
    approve_gates(live_store, live_ref, "requirements", "design")

    edit(live_project, DocumentKind.REQUIREMENTS)

    assert sync_staleness(live_store, live_ref) == ("requirements",)
    assert live_store.get_approval(live_ref, "requirements").stale
    assert not live_store.get_approval(live_ref, "design").stale
    assert derive_phase(live_store, live_ref).stale_gates == ("requirements",)


def test_editing_an_unapproved_document_leaves_approval_state_untouched(
    live_project, live_store, live_ref
):
    """An edit must not invent approval state for a gate nobody approved."""
    approve_gates(live_store, live_ref, "requirements")

    edit(live_project, DocumentKind.DESIGN)

    assert sync_staleness(live_store, live_ref) == ()
    approvals = live_store.list_approvals(live_ref)
    assert [record.gate for record in approvals] == ["requirements"]
    assert not approvals[0].stale
    assert derive_phase(live_store, live_ref).stale_gates == ()


def test_deleting_an_approved_document_stales_its_approval(live_project, live_store, live_ref):
    approve_gates(live_store, live_ref, "requirements", "design")

    (live_ref.spec_dir / DocumentKind.DESIGN.filename).unlink()

    assert sync_staleness(live_store, live_ref) == ("design",)
    assert not live_store.get_approval(live_ref, "requirements").stale


def test_derivation_reports_drift_before_anything_persists_it(live_project, live_store, live_ref):
    """Live hash comparison, so an edit is visible without a sync first."""
    approve_gates(live_store, live_ref, "requirements")
    edit(live_project, DocumentKind.REQUIREMENTS)

    state = derive_phase(live_store, live_ref)

    assert state.stale_gates == ("requirements",)
    # Nothing has persisted the flag yet: the derivation is still a pure read.
    assert not live_store.get_approval(live_ref, "requirements").stale


def test_syncing_staleness_is_idempotent(live_project, live_store, live_ref):
    approve_gates(live_store, live_ref, "requirements")
    edit(live_project, DocumentKind.REQUIREMENTS)

    assert sync_staleness(live_store, live_ref) == ("requirements",)
    assert sync_staleness(live_store, live_ref) == ()


def test_reverting_to_the_approved_bytes_does_not_revive_the_approval(
    live_project, live_store, live_ref
):
    """A recorded staleness outlives an undo of the edit that caused it.

    The approval was given without sight of the intervening edit, so a gate that
    a well-chosen revert reopens is a gate worth nothing. Note what this pins: the
    reverted document hashes to exactly what was approved, so comparing hashes
    alone would call the approval fresh again -- only the persisted flag keeps it
    stale.
    """
    approve_gates(live_store, live_ref, "requirements")
    path = live_ref.spec_dir / DocumentKind.REQUIREMENTS.filename
    approved_bytes = path.read_text(encoding="utf-8")

    edit(live_project, DocumentKind.REQUIREMENTS)
    assert sync_staleness(live_store, live_ref) == ("requirements",)

    path.write_text(approved_bytes, encoding="utf-8")

    approval = live_store.get_approval(live_ref, "requirements")
    assert approval.doc_hash == content_hash(approved_bytes)
    assert derive_phase(live_store, live_ref).stale_gates == ("requirements",)

    result = advance(live_store, live_ref, actor="user:ada", gate="requirements")
    assert not result.ok
    assert REASON_APPROVAL_STALE in result.reason_codes


def test_a_staled_approval_is_recorded_in_the_audit_log(live_project, live_store, live_ref, log):
    approve_gates(live_store, live_ref, "design")
    edit(live_project, DocumentKind.DESIGN)

    sync_staleness(live_store, live_ref, audit=log)

    staled = [event for event in log.read(live_ref) if event.event == APPROVAL_STALED_EVENT]
    assert [event.detail["gate"] for event in staled] == ["design"]


def test_content_hash_ignores_line_ending_style():
    """A checkout that rewrites newlines changes no words."""
    assert content_hash("alpha\r\nbeta\r\n") == content_hash("alpha\nbeta\n")
    assert content_hash("alpha\rbeta") == content_hash("alpha\nbeta")
    assert content_hash("alpha beta") != content_hash("alpha  beta")


# --- The advancement gate --------------------------------------------------


def test_advancement_is_refused_while_the_gate_lacks_an_approval(live_store, live_ref):
    result = advance(live_store, live_ref, actor="agent", gate="requirements")

    assert not result.ok
    assert result.reason_codes == (REASON_APPROVAL_MISSING,)
    assert result.reasons[0].gate == "requirements"
    assert result.to_phase is None


def test_advancement_is_refused_on_validation_failure_and_quotes_the_rules(live_store, project):
    ref = SpecRef.of(project, "example")

    result = advance(live_store, ref, actor="agent", gate="requirements")

    assert not result.ok
    # Complete rather than first-blocking: the document is both invalid and
    # unapproved, and a caller fixing it wants to know both.
    assert set(result.reason_codes) == {REASON_DOCUMENT_INVALID, REASON_APPROVAL_MISSING}
    assert rules.SECTION_MISSING in result.rule_ids
    assert result.report is not None and not result.report.ok


def test_advancement_is_refused_while_the_document_is_unwritten(tmp_path, live_store):
    project = write_spec(tmp_path / "partial", kinds=(DocumentKind.REQUIREMENTS,))
    ref = SpecRef.of(project, SPEC_NAME)
    approve_gates(live_store, ref, "requirements")

    result = advance(live_store, ref, actor="agent", gate="design")

    assert not result.ok
    assert result.reason_codes == (REASON_DOCUMENT_MISSING,)


def test_advancement_reports_the_next_phase_and_caches_it(live_store, live_ref):
    approve_gates(live_store, live_ref, "requirements")

    result = advance(live_store, live_ref, actor="agent", gate="requirements")

    assert result.ok
    assert result.from_phase is Phase.DESIGN
    assert result.to_phase is Phase.DESIGN
    assert result.gate == "requirements"
    assert live_store.get_spec(live_ref).phase == "design"


def test_advancement_defaults_to_the_last_written_document(tmp_path, live_store):
    """The document whoever called this has just finished."""
    project = write_spec(
        tmp_path / "partial", kinds=(DocumentKind.REQUIREMENTS, DocumentKind.DESIGN)
    )
    ref = SpecRef.of(project, SPEC_NAME)
    approve_gates(live_store, ref, "requirements", "design")

    result = advance(live_store, ref, actor="agent")

    assert result.ok
    assert result.gate == "design"
    assert result.to_phase is Phase.TASKS


def test_advancing_past_the_last_gate_reaches_ready(live_store, live_ref):
    approve_gates(live_store, live_ref, "requirements", "design", "tasks")

    result = advance(live_store, live_ref, actor="agent", gate="tasks")

    assert result.ok
    assert result.to_phase is Phase.READY


def test_advancement_with_no_target_on_a_settled_spec_reports_already_final(live_store, live_ref):
    approve_gates(live_store, live_ref, "requirements", "design", "tasks")

    result = advance(live_store, live_ref, actor="agent")

    assert not result.ok
    assert result.reason_codes == (REASON_ALREADY_FINAL,)
    assert result.from_phase is Phase.READY


def test_a_stale_earlier_approval_blocks_advancement_later_in_the_plan(
    live_project, live_store, live_ref
):
    """Otherwise re-approval could be side-stepped by advancing from a later document."""
    approve_gates(live_store, live_ref, "requirements", "design", "tasks")
    assert advance(live_store, live_ref, actor="agent", gate="tasks").ok

    edit(live_project, DocumentKind.REQUIREMENTS)
    result = advance(live_store, live_ref, actor="agent", gate="tasks")

    assert not result.ok
    assert result.reason_codes == (REASON_APPROVAL_STALE,)
    assert result.reasons[0].gate == "requirements"
    assert "re-approval" in result.reasons[0].message


def test_re_approving_the_edited_document_unblocks_advancement(live_project, live_store, live_ref):
    approve_gates(live_store, live_ref, "requirements", "design", "tasks")
    edit(live_project, DocumentKind.REQUIREMENTS)
    assert not advance(live_store, live_ref, actor="agent", gate="tasks").ok

    approve_gates(live_store, live_ref, "requirements", actor="user:grace")

    assert advance(live_store, live_ref, actor="agent", gate="tasks").ok


def test_advancing_a_spec_with_no_recorded_type_is_refused(tmp_path, live_store):
    project = write_spec(tmp_path / "untyped", spec_type=None)
    ref = SpecRef.of(project, SPEC_NAME)

    result = advance(live_store, ref, actor="agent")

    assert not result.ok
    assert result.reason_codes == (REASON_SPEC_TYPE_UNRECORDED,)


def test_advancing_a_gate_outside_the_plan_is_refused(tmp_path, live_store):
    project = write_spec(tmp_path / "quick", spec_type="quick")
    ref = SpecRef.of(project, SPEC_NAME)

    result = advance(live_store, ref, actor="agent", gate="design")

    assert not result.ok
    assert result.reason_codes == (REASON_GATE_NOT_IN_PLAN,)


def test_a_refused_advancement_is_recorded_with_its_reason_codes(live_store, live_ref, log):
    advance(live_store, live_ref, actor="agent", gate="requirements", audit=log)

    events = [event for event in log.read(live_ref) if event.event == PHASE_ADVANCE_REFUSED_EVENT]
    assert len(events) == 1
    codes = [reason["code"] for reason in events[0].detail["reasons"]]
    assert codes == [REASON_APPROVAL_MISSING]


def test_advancement_persists_drift_it_discovers(live_project, live_store, live_ref):
    """The rows a queue reads must agree with the decision the gate just took."""
    approve_gates(live_store, live_ref, "requirements", "design")
    edit(live_project, DocumentKind.DESIGN)

    advance(live_store, live_ref, actor="agent", gate="design")

    assert live_store.get_approval(live_ref, "design").stale
    assert not live_store.get_approval(live_ref, "requirements").stale


# --- Serialisation of state changes ----------------------------------------


def test_a_second_writer_is_rejected_while_the_spec_is_locked(live_store, live_ref):
    with live_store.lock(live_ref, owner="other-session"):
        with pytest.raises(SpecLocked) as caught:
            approve(live_store, live_ref, "requirements", actor="user:ada")

    assert caught.value.state["name"] == SPEC_NAME
    assert live_store.list_approvals(live_ref) == []


def test_an_operation_may_reuse_a_lock_it_already_holds(live_store, live_ref):
    """The store's lock is not re-entrant, so a longer operation passes its handle."""
    with live_store.lock(live_ref, owner="driver") as handle:
        outcome = approve(live_store, live_ref, "requirements", actor="user:ada", lock=handle)
        result = advance(live_store, live_ref, actor="agent", gate="requirements", lock=handle)

    assert outcome.ok
    assert result.ok


def test_a_lock_taken_over_mid_approval_does_not_persist_the_approval(
    live_store, live_ref, monkeypatch
):
    """Acquisition is not the whole guarantee.

    The lock can expire underneath a slow check and be taken over by a second
    writer. Acquisition already succeeded by then, so only the re-verification
    before the write stands between that and an approval that silently
    supersedes the writer who now owns the lock.
    """

    def taken_over(handle):
        raise LockLost(SPEC_NAME)

    monkeypatch.setattr(live_store, "verify_lock", taken_over)

    with pytest.raises(LockLost):
        approve(live_store, live_ref, "requirements", actor="user:ada")

    assert live_store.list_approvals(live_ref) == []


def test_a_lock_taken_over_mid_advance_does_not_record_the_phase(
    live_store, live_ref, monkeypatch
):
    """The same window exists in advance, before the phase is recorded."""
    approve(live_store, live_ref, "requirements", actor="user:ada")
    before = live_store.get_spec(live_ref)

    def taken_over(handle):
        raise LockLost(SPEC_NAME)

    monkeypatch.setattr(live_store, "verify_lock", taken_over)

    with pytest.raises(LockLost):
        advance(live_store, live_ref, actor="agent", gate="requirements")

    assert live_store.get_spec(live_ref) == before


def test_an_operation_needs_an_actor(live_store, live_ref):
    with pytest.raises(ValueError):
        approve(live_store, live_ref, "requirements", actor="")
    with pytest.raises(ValueError):
        advance(live_store, live_ref, actor="")


# --- Serialisable results --------------------------------------------------


def test_results_serialise_for_a_tool_result(live_store, live_ref):
    approve_gates(live_store, live_ref, "requirements")
    state = derive_phase(live_store, live_ref).to_json_object()
    refused = advance(live_store, live_ref, actor="agent", gate="design").to_json_object()

    assert state["phase"] == "design"
    assert {gate["gate"] for gate in state["gates"]} == {"requirements", "design", "tasks"}
    assert json.loads(json.dumps(state))["spec_type"] == "feature"
    assert refused["ok"] is False
    assert refused["reasons"][0]["code"] == REASON_APPROVAL_MISSING
