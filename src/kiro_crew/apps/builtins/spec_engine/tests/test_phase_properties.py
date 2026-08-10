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

    def expected_stale(self, gate: str) -> bool:
        """An approval is stale exactly when its own document has moved."""
        if gate not in self.approved_over:
            return False
        kind = DocumentKind(gate)
        return self.approved_over[gate] != content_hash(self.texts[kind])

    def settled(self, gate: str) -> bool:
        return gate in self.approved_over and not self.expected_stale(gate)

    def expected_phase(self, plan: tuple[DocumentKind, ...]) -> Phase:
        for kind in plan:
            if not self.settled(kind.value):
                return Phase.of(kind)
        return Phase.READY


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
