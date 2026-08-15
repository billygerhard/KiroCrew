"""Property-based tests for the phase gate.

The design states the property this file checks: across arbitrary
edit/approve/advance sequences, advancement succeeds only when the current
document validates and carries a non-stale approval, and any post-approval edit
stales exactly the approvals whose own documents changed.

A scripted test can only show that on the handful of orderings someone thought
of, and the orderings that break a gate are the ones nobody thought of -- approve,
edit, approve again, advance from a later document. So the expected answer here
comes from a shadow model kept alongside the trace (which documents exist, what
each currently hashes to, and what hash each approval was given over), never from
the engine's own derivation. If the two agree for every generated trace, the gate
is sound for reasons that do not depend on the implementation being right.

Edits append a formatting-neutral trailing comment, which changes content without
changing validity. That isolates the approval logic: validity is held constant so
a failure here can only be about hashes, staleness, or gate ordering.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from kiro_crew.apps.builtins.spec_engine.engine.documents import DocumentKind
from kiro_crew.apps.builtins.spec_engine.engine.phases import (
    REASON_APPROVAL_MISSING,
    REASON_APPROVAL_STALE,
    REASON_DOCUMENT_INVALID,
    REASON_DOCUMENT_MISSING,
    Phase,
    advance,
    approve,
    content_hash,
    derive_phase,
    document_plan,
    execution_blocking_reasons,
    validate_gate,
)
from kiro_crew.apps.builtins.spec_engine.engine.state import SpecRef, StateStore

from .conftest import spec_dir_snapshot
from .test_phases import EDIT_MARKER, SPEC_NAME, live_document_text, write_spec

#: Examples per property. Each one runs several SQLite transactions and rewrites
#: documents on disk, so this trades a little coverage for a suite that still
#: runs on every commit.
MAX_EXAMPLES = 40

_SPEC_TYPE = "feature"

_ACTIONS = st.lists(
    st.tuples(
        st.sampled_from(["edit", "approve", "advance"]),
        st.sampled_from(list(document_plan(_SPEC_TYPE))),
    ),
    max_size=10,
)

#: Refusal codes this trace can produce. An edit keeps documents valid and never
#: removes one, so anything outside this set means the gate refused for a reason
#: the trace cannot explain.
_EXPECTED_CODES = frozenset({REASON_APPROVAL_MISSING, REASON_APPROVAL_STALE})
_UNEXPECTED_CODES = frozenset({REASON_DOCUMENT_MISSING, REASON_DOCUMENT_INVALID})


@dataclass
class _Model:
    """What the trace has done, tracked independently of the engine."""

    #: Current text of each document, so the expected hash is computable.
    texts: dict[DocumentKind, str] = field(default_factory=dict)
    #: Hash each gate's approval was recorded over, for gates that have one.
    approved_over: dict[str, str] = field(default_factory=dict)
    #: Documents the trace has deleted. A missing document is not an empty one:
    #: the gate reports a different refusal for each.
    deleted: set[DocumentKind] = field(default_factory=set)
    #: Documents the trace has made unparseable under the native-format rules.
    corrupted: set[DocumentKind] = field(default_factory=set)
    #: Gates whose staleness has been PERSISTED. Staleness is sticky: once
    #: ``advance`` has recorded that a document moved, reverting it to the exact
    #: bytes that were approved makes the hashes agree again without reviving the
    #: approval, which was given without sight of the intervening edit. Only the
    #: calls that persist put a gate in here, because stickiness reaches exactly
    #: as far as what was observed -- an edit made and undone with no persisting
    #: derivation in between is indistinguishable from an untouched document.
    staled: set[str] = field(default_factory=set)

    def present(self, kind: DocumentKind) -> bool:
        return kind not in self.deleted

    def valid(self, kind: DocumentKind) -> bool:
        return self.present(kind) and kind not in self.corrupted

    def observe_staleness(self) -> None:
        """Persist staleness for every gate whose document has moved.

        Called for the engine operations that sync it, so the model becomes
        sticky at the same moments the engine does.
        """
        for gate in list(self.approved_over):
            if self._hash_moved(gate):
                self.staled.add(gate)

    def _hash_moved(self, gate: str) -> bool:
        kind = DocumentKind(gate)
        if not self.present(kind):
            return True
        return self.approved_over[gate] != content_hash(self.texts[kind])

    def expected_stale(self, gate: str) -> bool:
        """Whether the gate's approval no longer covers its document.

        Either the hash has moved, or a persisted flag says it once did. A
        deleted document counts as moved: an approval recorded over text that is
        no longer there plainly does not cover what is there now, so the gate
        reports it stale rather than treating absence as a separate axis that
        leaves the approval looking live.
        """
        if gate not in self.approved_over:
            return False
        return gate in self.staled or self._hash_moved(gate)

    def settled(self, gate: str) -> bool:
        kind = DocumentKind(gate)
        if not self.valid(kind):
            return False
        return gate in self.approved_over and not self.expected_stale(gate)

    def expected_phase(self, plan: tuple[DocumentKind, ...]) -> Phase:
        for kind in plan:
            if not self.settled(kind.value):
                return Phase.of(kind)
        return Phase.READY

    def expected_codes(self, gate: str) -> set[str]:
        """Every refusal code the gate should report for *gate*.

        A set rather than one code, because the gate reports every reason at once
        so an author fixes one report instead of rediscovering the next refusal
        after each attempt. Absence is the exception: a document that is not
        there cannot also be invalid or unapproved-in-a-useful-sense, so it
        short-circuits to the one reason.
        """
        kind = DocumentKind(gate)
        if not self.present(kind):
            return {REASON_DOCUMENT_MISSING}
        codes: set[str] = set()
        if kind in self.corrupted:
            codes.add(REASON_DOCUMENT_INVALID)
        if gate not in self.approved_over:
            codes.add(REASON_APPROVAL_MISSING)
        elif self.expected_stale(gate):
            codes.add(REASON_APPROVAL_STALE)
        return codes


class TestPhaseGateSoundness:
    @settings(
        max_examples=MAX_EXAMPLES,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
    )
    @given(actions=_ACTIONS)
    def test_the_gate_agrees_with_the_shadow_model_for_any_trace(
        self, tmp_path: Path, actions: list[tuple[str, DocumentKind]]
    ) -> None:
        example = tmp_path / uuid.uuid4().hex
        project = write_spec(example / "project", spec_type=_SPEC_TYPE)
        spec_dir = project / ".kiro" / "specs" / SPEC_NAME
        model = _Model()
        for kind in DocumentKind:
            model.texts[kind] = live_document_text(kind)

        plan = document_plan(_SPEC_TYPE)
        store = StateStore(root=example / "state")
        ref = SpecRef.of(project, SPEC_NAME)
        store.register_spec(ref, spec_type=_SPEC_TYPE)
        native_files = set(spec_dir_snapshot(spec_dir))

        try:
            for step, (action, kind) in enumerate(actions):
                self._apply(store, ref, spec_dir, model, action, kind, step)
                self._assert_derivation_matches(store, ref, plan, model)
                # Purity: the trace writes documents and nothing else appears.
                assert set(spec_dir_snapshot(spec_dir)) == native_files
        finally:
            store.close()

    @staticmethod
    def _apply(
        store: StateStore,
        ref: SpecRef,
        spec_dir: Path,
        model: _Model,
        action: str,
        kind: DocumentKind,
        step: int,
    ) -> None:
        gate = kind.value
        if action == "edit":
            updated = model.texts[kind] + EDIT_MARKER
            (spec_dir / kind.filename).write_text(updated, encoding="utf-8")
            model.texts[kind] = updated
            return

        if action == "approve":
            outcome = approve(store, ref, gate, actor=f"user:{step}")
            # The documents stay valid and present, so an approval always lands.
            assert outcome.ok, [str(reason) for reason in outcome.reasons]
            assert outcome.approval is not None
            assert outcome.approval.actor == f"user:{step}"
            model.approved_over[gate] = content_hash(model.texts[kind])
            return

        plan = document_plan(_SPEC_TYPE)
        upto = plan[: plan.index(kind) + 1]
        expected_ok = all(model.settled(item.value) for item in upto)
        result = advance(store, ref, actor=f"agent:{step}", gate=gate)
        assert result.ok is expected_ok, [str(reason) for reason in result.reasons]

        if expected_ok:
            following = plan[plan.index(kind) + 1 :]
            expected_next = Phase.of(following[0]) if following else Phase.READY
            assert result.to_phase is expected_next
            return

        # A refusal names every unsettled gate up to the target, and only those.
        assert result.to_phase is None
        blocked = {item.value for item in upto if not model.settled(item.value)}
        assert {reason.gate for reason in result.reasons} == blocked
        codes = {reason.code for reason in result.reasons}
        assert codes <= _EXPECTED_CODES
        assert not codes & _UNEXPECTED_CODES

    @staticmethod
    def _assert_derivation_matches(
        store: StateStore,
        ref: SpecRef,
        plan: tuple[DocumentKind, ...],
        model: _Model,
    ) -> None:
        before = store.current_state(ref)
        state = derive_phase(store, ref)

        assert state.phase is model.expected_phase(plan)
        assert [gate.gate for gate in state.gates] == [kind.value for kind in plan]
        for gate in state.gates:
            assert gate.present
            assert gate.stale is model.expected_stale(gate.gate)
            assert gate.approved is (gate.gate in model.approved_over and not gate.stale)
            assert gate.content_hash == content_hash(model.texts[gate.kind])
        assert set(state.stale_gates) == {
            gate for gate in model.approved_over if model.expected_stale(gate)
        }
        # Derivation is a read: it cannot have changed what it read.
        assert store.current_state(ref) == before


#: A document body that no native-format rule accepts: no title heading, no
#: sections. Corruption has to be recognisable to the validator rather than
#: merely different, otherwise the "invalid" branch of the gate is never taken.
CORRUPT_TEXT = "not a spec document\n"

#: Actions for the trace below. ``delete`` removes a document and ``corrupt``
#: makes one unparseable, so the gate's DOCUMENT_MISSING and DOCUMENT_INVALID
#: refusals become reachable; ``restore`` puts a live document back, so a spec
#: can recover and the trace is not a one-way ratchet into refusal.
_DEFECT_ACTIONS = st.lists(
    st.tuples(
        st.sampled_from(["edit", "approve", "advance", "delete", "corrupt", "restore"]),
        st.sampled_from(list(document_plan(_SPEC_TYPE))),
    ),
    max_size=12,
)


class TestPhaseGateSoundnessWithDefectiveDocuments:
    """The same shadow model, over traces where documents break and come back.

    The property above holds validity constant on purpose: edits are
    formatting-neutral, so a failure there can only be about hashes, staleness or
    ordering. The cost is that two of the gate's four refusals -- a document that
    is missing and one that does not validate -- are asserted never to occur and
    therefore never checked, and neither ``validate_gate`` nor
    ``execution_blocking_reasons`` is exercised at all.

    This trace deletes, corrupts and restores documents alongside approving them,
    so every refusal the gate can produce is reachable and the model has to
    predict which one. The interesting orderings are the ones a scripted case
    does not reach: approve, then delete; approve, then corrupt, then restore to
    the exact text that was approved -- which is a live approval again, not a
    stale one, because the hash is what staleness is measured against.
    """

    @settings(
        max_examples=MAX_EXAMPLES,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
    )
    @given(actions=_DEFECT_ACTIONS)
    def test_every_refusal_the_gate_reports_is_the_one_the_trace_earned(
        self, tmp_path: Path, actions: list[tuple[str, DocumentKind]]
    ) -> None:
        example = tmp_path / uuid.uuid4().hex
        project = write_spec(example / "project", spec_type=_SPEC_TYPE)
        spec_dir = project / ".kiro" / "specs" / SPEC_NAME
        model = _Model()
        for kind in DocumentKind:
            model.texts[kind] = live_document_text(kind)

        plan = document_plan(_SPEC_TYPE)
        store = StateStore(root=example / "state")
        ref = SpecRef.of(project, SPEC_NAME)
        store.register_spec(ref, spec_type=_SPEC_TYPE)

        try:
            for step, (action, kind) in enumerate(actions):
                self._apply_defect(store, ref, spec_dir, model, action, kind, step)
                self._assert_gates_match(store, ref, plan, model)
                self._assert_execution_agrees(store, ref, plan, model)
        finally:
            store.close()

    @staticmethod
    def _apply_defect(
        store: StateStore,
        ref: SpecRef,
        spec_dir: Path,
        model: _Model,
        action: str,
        kind: DocumentKind,
        step: int,
    ) -> None:
        path = spec_dir / kind.filename
        gate = kind.value
        if action == "delete":
            path.unlink(missing_ok=True)
            model.deleted.add(kind)
            return
        if action == "corrupt":
            path.write_text(CORRUPT_TEXT, encoding="utf-8")
            model.deleted.discard(kind)
            model.corrupted.add(kind)
            model.texts[kind] = CORRUPT_TEXT
            return
        if action == "restore":
            text = live_document_text(kind)
            path.write_text(text, encoding="utf-8")
            model.deleted.discard(kind)
            model.corrupted.discard(kind)
            model.texts[kind] = text
            return
        if action == "edit":
            if not model.present(kind):
                # Nothing to edit. Left as a no-op rather than filtered out, so
                # the trace can hold an edit that lands after a delete.
                return
            updated = model.texts[kind] + EDIT_MARKER
            path.write_text(updated, encoding="utf-8")
            model.texts[kind] = updated
            return
        if action == "approve":
            outcome = approve(store, ref, gate, actor=f"user:{step}")
            # An approval lands exactly when the document is there and valid.
            assert outcome.ok is model.valid(kind), [str(r) for r in outcome.reasons]
            if outcome.ok:
                # A fresh approval records the current hash, which clears any
                # flag a previous edit had persisted.
                model.approved_over[gate] = content_hash(model.texts[kind])
                model.staled.discard(gate)
            else:
                # Approval refuses on the document's own defects only: whether an
                # approval already exists is what this call is deciding.
                assert {r.code for r in outcome.reasons} == model.expected_codes(gate) & {
                    REASON_DOCUMENT_MISSING,
                    REASON_DOCUMENT_INVALID,
                }
            return

        plan = document_plan(_SPEC_TYPE)
        upto = plan[: plan.index(kind) + 1]
        # advance persists staleness for every gate it derives, so the model
        # becomes sticky at the same moment the engine does.
        model.observe_staleness()
        expected_ok = all(model.settled(item.value) for item in upto)
        result = advance(store, ref, actor=f"agent:{step}", gate=gate)
        assert result.ok is expected_ok, [str(reason) for reason in result.reasons]
        if expected_ok:
            return
        # A refusal names every unsettled gate up to the target, and for each one
        # the single reason the trace earned -- absent before invalid, invalid
        # before unapproved, unapproved before stale.
        blocked = {item.value for item in upto if not model.settled(item.value)}
        assert {reason.gate for reason in result.reasons} == blocked
        # Exactly the reasons the trace earned for each blocked gate: an extra
        # code is a refusal nobody can act on, a missing one is a repair the
        # author is never told about.
        for blocked_gate in blocked:
            reported = {r.code for r in result.reasons if r.gate == blocked_gate}
            assert reported == model.expected_codes(blocked_gate)

    @staticmethod
    def _assert_gates_match(
        store: StateStore,
        ref: SpecRef,
        plan: tuple[DocumentKind, ...],
        model: _Model,
    ) -> None:
        state = derive_phase(store, ref)

        assert state.phase is model.expected_phase(plan)
        for gate in state.gates:
            assert gate.present is model.present(gate.kind)
            assert gate.stale is model.expected_stale(gate.gate)
            assert gate.approved is model.settled(gate.gate)
            if model.present(gate.kind):
                assert gate.content_hash == content_hash(model.texts[gate.kind])

            # validate_gate reports on the document as it is now: nothing for an
            # absent one, errors for a corrupt one, and none for a live one.
            report = validate_gate(state, gate.gate)
            if not model.present(gate.kind):
                assert report is None
            elif gate.kind in model.corrupted:
                assert report is not None and report.errors
            else:
                assert report is not None and not report.errors

    @staticmethod
    def _assert_execution_agrees(
        store: StateStore,
        ref: SpecRef,
        plan: tuple[DocumentKind, ...],
        model: _Model,
    ) -> None:
        state = derive_phase(store, ref)
        reasons, _report = execution_blocking_reasons(state)

        if all(model.settled(kind.value) for kind in plan):
            # Every document written, valid and live-approved. The only reason
            # left is a tasks plan that does not resolve, which this trace cannot
            # create: it either restores the real document or corrupts it, and a
            # corrupt document is unsettled above.
            assert [reason.code for reason in reasons] == []
            return
        assert reasons
        # Execution is blocked by gates, and every gate it names is one the model
        # agrees is unsettled -- never a settled gate reported as blocking.
        for reason in reasons:
            if reason.gate:
                assert not model.settled(reason.gate)

    def test_a_document_corrupted_after_approval_reports_both_defects(self, tmp_path: Path) -> None:
        """The compound refusal, pinned rather than left to the trace.

        Measured over 400 draws the generated trace produces an approved document
        that is later corrupted -- which is invalid AND carries a stale approval
        -- in about 2% of examples, so at this file's example count the case shows
        up in roughly one run in five. A property that covers a case one run in
        five does not cover it, so the compound is also asserted directly.
        """
        example = tmp_path / uuid.uuid4().hex
        project = write_spec(example / "project", spec_type=_SPEC_TYPE)
        spec_dir = project / ".kiro" / "specs" / SPEC_NAME
        store = StateStore(root=example / "state")
        ref = SpecRef.of(project, SPEC_NAME)
        store.register_spec(ref, spec_type=_SPEC_TYPE)
        gate = DocumentKind.REQUIREMENTS.value

        try:
            assert approve(store, ref, gate, actor="user:1").ok
            (spec_dir / DocumentKind.REQUIREMENTS.filename).write_text(
                CORRUPT_TEXT, encoding="utf-8"
            )

            result = advance(store, ref, actor="agent:1", gate=gate)

            assert not result.ok
            codes = {reason.code for reason in result.reasons if reason.gate == gate}
            # Both, not whichever is checked first: the author has to rewrite the
            # document and re-approve it, and being told only one of those sends
            # them back for the other.
            assert codes == {REASON_DOCUMENT_INVALID, REASON_APPROVAL_STALE}
        finally:
            store.close()
