"""Who may approve a gate: an interactive run's user, or a headless run's policy.

Three claims, each of which is a way autonomy could quietly erode the gate.

An interactive run cannot end up with an approval nobody gave. Writing the
documents does not approve them, asking where the spec sits does not approve
them, and being refused an advancement does not approve them. The only way an
approval row appears is a caller naming the person who approved -- so these tests
run the whole interactive sequence and assert the approvals table stays empty
until someone says otherwise.

A headless run's policy approves only what it covers. An unconfigured source
resolves to authoring, which covers no document gate, so the run stops and waits
for a reviewer rather than approving itself; a source configured for execution
covers them, and the approval is recorded under the policy's own identity so the
audit trail says which declaration authorized the gate instead of naming a person
who never looked.

Autonomy buys no leniency. The same invalid document is refused with the same
reasons and the same rule identifiers whether a user or a fully-autonomous policy
asked, because both go through one approval path and its validation.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from kiro_crew.apps.builtins.spec_engine.engine import rules
from kiro_crew.apps.builtins.spec_engine.engine.audit import AuditLog
from kiro_crew.apps.builtins.spec_engine.engine.autonomy import (
    AUTONOMY_FIELD,
    AutonomyDecision,
    AutonomyLevel,
    AutonomyPolicy,
)
from kiro_crew.apps.builtins.spec_engine.engine.config import AUTONOMY_LEVELS, WILDCARD_KEY
from kiro_crew.apps.builtins.spec_engine.engine.documents import DocumentKind
from kiro_crew.apps.builtins.spec_engine.engine.phases import (
    APPROVAL_RECORDED_EVENT,
    APPROVAL_REFUSED_EVENT,
    APPROVER_POLICY,
    APPROVER_USER,
    POLICY_ACTOR_SCHEME,
    REASON_APPROVAL_MISSING,
    REASON_DOCUMENT_INVALID,
    REASON_HUMAN_REQUIRED,
    Phase,
    RunMode,
    advance,
    approve,
    approve_by_policy,
    approve_for_run,
    approve_interactive,
    derive_phase,
    gate_is_policy_covered,
    is_policy_actor,
    policy_actor,
    policy_declaration,
    policy_level_for_gate,
    sync_staleness,
)
from kiro_crew.apps.builtins.spec_engine.engine.state import SpecRef, StateStore

from .conftest import make_spec_dir, spec_dir_snapshot
from .test_phases import SPEC_NAME, write_spec

SOURCE = "tracker"
USER = "user:ada"
DOCUMENT_GATES = ("requirements", "design", "tasks")

#: Examples per property. Each one writes documents and runs SQLite
#: transactions, so this trades breadth for a suite that runs on every commit.
MAX_EXAMPLES = 30


def decision_for(level: str | None, *, spec_type: str = "feature") -> AutonomyDecision:
    """Resolve a real decision from a config document declaring *level*.

    Resolution rather than a fabricated decision, so a change to what the policy
    does with configuration shows up here instead of being mocked away. ``None``
    declares nothing, which is the unconfigured case.
    """
    entry: dict = {"poll": ["watch"]}
    if level is not None:
        entry[AUTONOMY_FIELD] = {WILDCARD_KEY: {WILDCARD_KEY: level}}
    policy = AutonomyPolicy.from_document({"sources": {SOURCE: entry}})
    return policy.resolve(source=SOURCE, spec_type=spec_type, submitter_class="member")


@pytest.fixture()
def store(tmp_path: Path) -> Iterator[StateStore]:
    handle = StateStore(root=tmp_path / "state")
    yield handle
    handle.close()


@pytest.fixture()
def log(tmp_path: Path) -> AuditLog:
    return AuditLog(root=tmp_path / "state")


@pytest.fixture()
def ref(tmp_path: Path) -> SpecRef:
    """A spec whose three documents are real and format-clean."""
    return SpecRef.of(write_spec(tmp_path / "workspace"), SPEC_NAME)


# --- Gate coverage ---------------------------------------------------------


class TestCoverage:
    """Which gates a resolved level covers."""

    def test_a_document_gate_is_covered_only_from_execution_upward(self):
        for gate in DOCUMENT_GATES:
            assert policy_level_for_gate(gate) is AutonomyLevel.EXECUTION

    def test_an_unconfigured_policy_covers_no_document_gate(self):
        decision = decision_for(None)

        assert decision.level is AutonomyLevel.AUTHORING
        assert decision.execution_is_human_reserved
        assert not any(gate_is_policy_covered(decision, gate) for gate in DOCUMENT_GATES)

    def test_authoring_covers_no_document_gate_even_when_configured(self):
        decision = decision_for("authoring")

        assert decision.is_configured
        assert not any(gate_is_policy_covered(decision, gate) for gate in DOCUMENT_GATES)

    def test_every_level_from_execution_upward_covers_the_document_gates(self):
        for level in ("execution", "delivery", "integration"):
            decision = decision_for(level)
            assert all(gate_is_policy_covered(decision, gate) for gate in DOCUMENT_GATES)

    def test_a_gate_outside_the_table_is_never_policy_covered(self):
        """A later gate must not inherit authority from configuration predating it."""
        decision = decision_for("integration")

        assert policy_level_for_gate("integration-approval") is None
        assert not gate_is_policy_covered(decision, "integration-approval")


class TestPolicyIdentity:
    """The approver identity that makes the policy's role legible."""

    def test_the_identity_names_the_declaration_that_authorized_the_gate(self):
        decision = decision_for("execution")

        actor = policy_actor(decision)

        assert actor == f"{POLICY_ACTOR_SCHEME}:{decision.declared_at}"
        assert policy_declaration(actor) == f"sources.{SOURCE}.autonomy.default.default"

    def test_a_policy_identity_is_distinguishable_from_a_person(self):
        assert is_policy_actor(policy_actor(decision_for("execution")))
        assert not is_policy_actor(USER)
        assert not is_policy_actor("autonomy-policy-admin")

    def test_an_unconfigured_decision_has_no_approver_identity(self):
        with pytest.raises(ValueError):
            policy_actor(decision_for(None))


# --- Interactive runs ------------------------------------------------------


class TestInteractiveRuns:
    """An approval nobody gave must not appear."""

    def test_no_approval_appears_without_an_explicit_user_action(self, store, ref, log):
        """The interactive sequence up to the gate records nothing on its own."""
        assert derive_phase(store, ref).phase is Phase.REQUIREMENTS
        assert sync_staleness(store, ref) == ()
        refused = advance(store, ref, actor=USER, gate="requirements")

        assert not refused.ok
        assert REASON_APPROVAL_MISSING in refused.reason_codes
        assert store.list_approvals(ref) == []

        outcome = approve_interactive(store, ref, "requirements", user=USER, audit=log)

        assert outcome.ok
        assert not outcome.by_policy
        recorded = store.list_approvals(ref)
        assert [record.gate for record in recorded] == ["requirements"]
        assert recorded[0].actor == USER

    def test_the_recorded_approval_names_the_person_who_approved(self, store, ref, log):
        approve_interactive(store, ref, "requirements", user=USER, audit=log)

        gate = derive_phase(store, ref).gate_named("requirements")
        assert gate is not None
        assert gate.to_json_object()["approver_kind"] == APPROVER_USER
        events = [event for event in log.read(ref) if event.event == APPROVAL_RECORDED_EVENT]
        assert [event.initiator for event in events] == [USER]
        assert events[0].detail is not None
        assert events[0].detail["mode"] == RunMode.INTERACTIVE.value
        assert events[0].detail["approver"] == APPROVER_USER
        assert "policy_declaration" not in events[0].detail

    def test_the_policy_cannot_approve_in_an_interactive_run(self, store, ref, log):
        """A present user is the approver even when the policy would cover the gate."""
        decision = decision_for("integration")

        outcome = approve(
            store,
            ref,
            "requirements",
            actor=policy_actor(decision),
            mode=RunMode.INTERACTIVE,
            decision=decision,
            audit=log,
        )

        assert not outcome.ok
        assert outcome.reason_codes == (REASON_HUMAN_REQUIRED,)
        assert store.list_approvals(ref) == []
        refused = [event for event in log.read(ref) if event.event == APPROVAL_REFUSED_EVENT]
        assert len(refused) == 1

    def test_the_policy_cannot_approve_without_a_declared_mode(self, store, ref, log):
        """An absent mode is not evidence of an unattended run.

        The policy would cover this gate and carries the decision that authorizes
        it, so the only thing missing is the run kind. Accepting it recorded a
        policy approval whose audit trail could not say which kind of run made it,
        which is the one thing that trail exists to distinguish.
        """
        decision = decision_for("integration")

        outcome = approve(
            store,
            ref,
            "requirements",
            actor=policy_actor(decision),
            decision=decision,
            audit=log,
        )

        assert not outcome.ok
        assert outcome.reason_codes == (REASON_HUMAN_REQUIRED,)
        assert store.list_approvals(ref) == []

    def test_a_user_cannot_borrow_the_policy_identity(self, store, ref):
        """The reserved scheme is refused from the interactive path in both roles."""
        outcome = approve_interactive(
            store, ref, "requirements", user=f"{POLICY_ACTOR_SCHEME}:sources.tracker"
        )

        assert not outcome.ok
        assert outcome.reason_codes == (REASON_HUMAN_REQUIRED,)
        assert store.list_approvals(ref) == []

    def test_an_interactive_dispatch_carrying_a_decision_is_a_driver_bug(self, store, ref):
        with pytest.raises(ValueError):
            approve_for_run(
                store,
                ref,
                "requirements",
                mode=RunMode.INTERACTIVE,
                user=USER,
                decision=decision_for("execution"),
            )
        with pytest.raises(ValueError):
            approve_for_run(store, ref, "requirements", mode=RunMode.INTERACTIVE)
        assert store.list_approvals(ref) == []


# --- Headless runs ---------------------------------------------------------


class TestHeadlessRuns:
    """The policy approves what it covers, and only that."""

    def test_a_covered_gate_is_approved_under_the_policy_identity(self, store, ref, log):
        decision = decision_for("execution")

        outcome = approve_by_policy(store, ref, "requirements", decision=decision, audit=log)

        assert outcome.ok
        assert outcome.by_policy
        assert outcome.approval is not None
        assert outcome.approval.actor == policy_actor(decision)
        assert outcome.to_json_object()["approver_kind"] == APPROVER_POLICY
        events = [event for event in log.read(ref) if event.event == APPROVAL_RECORDED_EVENT]
        assert events[0].detail is not None
        assert events[0].detail["mode"] == RunMode.HEADLESS.value
        assert events[0].detail["approver"] == APPROVER_POLICY
        assert events[0].detail["policy_declaration"] == decision.declared_at

    def test_an_uncovered_gate_records_nothing_and_asks_for_a_human(self, store, ref, log):
        decision = decision_for(None)

        outcome = approve_by_policy(store, ref, "requirements", decision=decision, audit=log)

        assert not outcome.ok
        assert outcome.reason_codes == (REASON_HUMAN_REQUIRED,)
        assert store.list_approvals(ref) == []
        refused = [event for event in log.read(ref) if event.event == APPROVAL_REFUSED_EVENT]
        assert len(refused) == 1
        assert refused[0].detail is not None
        assert refused[0].detail["mode"] == RunMode.HEADLESS.value

    def test_an_uncovered_gate_leaves_advancement_refused(self, store, ref):
        """Nothing recorded is what keeps the run stopped, not a second check."""
        approve_by_policy(store, ref, "requirements", decision=decision_for(None))

        result = advance(store, ref, actor="run:headless", gate="requirements")

        assert not result.ok
        assert REASON_APPROVAL_MISSING in result.reason_codes
        assert derive_phase(store, ref).phase is Phase.REQUIREMENTS

    def test_a_covered_run_can_clear_its_whole_document_plan(self, store, ref):
        decision = decision_for("execution")

        for gate in DOCUMENT_GATES:
            outcome = approve_by_policy(store, ref, gate, decision=decision)
            assert outcome.ok, [str(reason) for reason in outcome.reasons]

        assert derive_phase(store, ref).phase is Phase.READY
        state = derive_phase(store, ref)
        assert all(gate.approval is not None for gate in state.gates)
        assert {gate.to_json_object()["approver_kind"] for gate in state.gates} == {APPROVER_POLICY}

    def test_a_reviewer_can_approve_a_gate_the_policy_left_alone(self, store, ref, log):
        """The human path in a headless run records the person, not the policy."""
        outcome = approve_for_run(
            store, ref, "requirements", mode=RunMode.HEADLESS, user=USER, audit=log
        )

        assert outcome.ok
        assert not outcome.by_policy
        assert outcome.approval is not None and outcome.approval.actor == USER

    def test_a_claimed_policy_identity_without_its_decision_is_refused(self, store, ref):
        """Nothing may credit the policy with an approval it did not authorize."""
        decision = decision_for("execution")

        bare = approve(
            store, ref, "requirements", actor=policy_actor(decision), mode=RunMode.HEADLESS
        )
        forged = approve(
            store,
            ref,
            "requirements",
            actor=f"{POLICY_ACTOR_SCHEME}:sources.other.autonomy.default.default",
            mode=RunMode.HEADLESS,
            decision=decision,
        )

        assert bare.reason_codes == (REASON_HUMAN_REQUIRED,)
        assert forged.reason_codes == (REASON_HUMAN_REQUIRED,)
        assert store.list_approvals(ref) == []

    def test_an_authoring_only_policy_cannot_approve_through_the_generic_path(self, store, ref):
        decision = decision_for("authoring")

        outcome = approve(
            store,
            ref,
            "requirements",
            actor=policy_actor(decision),
            mode=RunMode.HEADLESS,
            decision=decision,
        )

        assert outcome.reason_codes == (REASON_HUMAN_REQUIRED,)
        assert store.list_approvals(ref) == []

    def test_a_headless_dispatch_with_neither_approver_is_a_driver_bug(self, store, ref):
        with pytest.raises(ValueError):
            approve_for_run(store, ref, "requirements", mode=RunMode.HEADLESS)


# --- One validation rule for both modes ------------------------------------


class TestValidationParity:
    """A headless run is judged by the interactive run's rules."""

    def test_the_same_invalid_document_is_refused_identically_in_both_modes(
        self, tmp_path, store, project
    ):
        """conftest's project holds title-only documents: real files, invalid format."""
        ref = SpecRef.of(project, "example")
        decision = decision_for("integration")

        interactive = approve_interactive(store, ref, "requirements", user=USER)
        headless = approve_by_policy(store, ref, "requirements", decision=decision)

        assert not interactive.ok and not headless.ok
        assert interactive.reason_codes == headless.reason_codes == (REASON_DOCUMENT_INVALID,)
        assert interactive.reasons[0].rule_ids == headless.reasons[0].rule_ids
        assert rules.SECTION_MISSING in headless.reasons[0].rule_ids
        assert store.list_approvals(ref) == []

    def test_an_unwritten_document_is_refused_identically_in_both_modes(self, tmp_path, store):
        project = write_spec(tmp_path / "partial", kinds=(DocumentKind.REQUIREMENTS,))
        ref = SpecRef.of(project, SPEC_NAME)
        decision = decision_for("integration")

        interactive = approve_interactive(store, ref, "design", user=USER)
        headless = approve_by_policy(store, ref, "design", decision=decision)

        assert interactive.reason_codes == headless.reason_codes
        assert store.get_approval(ref, "design") is None

    def test_neither_mode_writes_into_the_spec_directory(self, store, ref):
        spec_dir = ref.spec_dir
        before = spec_dir_snapshot(spec_dir)

        approve_interactive(store, ref, "requirements", user=USER)
        approve_by_policy(store, ref, "design", decision=decision_for("execution"))
        approve_by_policy(store, ref, "tasks", decision=decision_for(None))

        assert spec_dir_snapshot(spec_dir) == before


# --- Property: attribution follows the ladder ------------------------------


@settings(
    max_examples=MAX_EXAMPLES,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    level=st.one_of(st.none(), st.sampled_from(AUTONOMY_LEVELS)),
    gate=st.sampled_from(DOCUMENT_GATES),
)
def test_a_policy_approval_appears_exactly_when_the_ladder_permits_execution(
    tmp_path: Path, level: str | None, gate: str
) -> None:
    """Attribution to the policy tracks the ladder, for every rung and gate.

    The expected answer comes from the ladder itself rather than from the phase
    machine's own coverage table: a rung at or above execution covers a document
    gate, and every other rung leaves it for a human. An approval that appears
    anywhere else is authority granted by accident.
    """
    project = write_spec(tmp_path / f"ladder-{level}-{gate}")
    ref = SpecRef.of(project, SPEC_NAME)
    store = StateStore(root=tmp_path / f"state-{level}-{gate}")
    try:
        decision = decision_for(level)
        expected = level is not None and AutonomyLevel(level).permits(AutonomyLevel.EXECUTION)

        outcome = approve_by_policy(store, ref, gate, decision=decision)

        assert outcome.ok is expected
        recorded = store.get_approval(ref, gate)
        if expected:
            assert outcome.by_policy
            assert recorded is not None and recorded.actor == policy_actor(decision)
        else:
            assert recorded is None
            assert outcome.reason_codes == (REASON_HUMAN_REQUIRED,)
    finally:
        store.close()


@settings(
    max_examples=MAX_EXAMPLES,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(mode=st.sampled_from(list(RunMode)), gate=st.sampled_from(DOCUMENT_GATES))
def test_no_mode_approves_a_document_that_fails_validation(
    tmp_path: Path, mode: RunMode, gate: str
) -> None:
    """The gate is the same height whoever is standing at it."""
    project = tmp_path / f"invalid-{mode.value}-{gate}"
    project.mkdir(exist_ok=True)
    make_spec_dir(project, "example")  # title-only documents
    ref = SpecRef.of(project, "example")
    store = StateStore(root=tmp_path / f"state-{mode.value}-{gate}")
    try:
        if mode is RunMode.INTERACTIVE:
            outcome = approve_for_run(store, ref, gate, mode=mode, user=USER)
        else:
            outcome = approve_for_run(
                store, ref, gate, mode=mode, decision=decision_for("integration")
            )

        assert not outcome.ok
        assert REASON_DOCUMENT_INVALID in outcome.reason_codes
        assert store.list_approvals(ref) == []
    finally:
        store.close()
