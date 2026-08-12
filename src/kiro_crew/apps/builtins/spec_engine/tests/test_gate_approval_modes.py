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

The execution gate is the same claim at the point where it costs the most. A
policy that permits everything is refused on exactly the reasons a policy that
permits nothing is refused on, because the gate asks whether the spec is
executable before it asks who may start it, and the first question never sees the
policy. Both outcomes are audited with the initiator -- a person's identity or the
policy's declaration -- and a refusal never records the policy's reserved approver
identity, which would make the trail claim an authorization that never happened.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Iterator

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from kiro_crew.apps.builtins.spec_engine.engine import rules
from kiro_crew.apps.builtins.spec_engine.engine.audit import AuditEvent, AuditLog
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
    EXECUTION_GATE,
    EXECUTION_REFUSED_EVENT,
    EXECUTION_STARTED_EVENT,
    INITIATOR_POLICY,
    INITIATOR_USER,
    POLICY_ACTOR_SCHEME,
    REASON_APPROVAL_MISSING,
    REASON_APPROVAL_STALE,
    REASON_DOCUMENT_INVALID,
    REASON_HUMAN_REQUIRED,
    REASON_TASKS_PLAN_INVALID,
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
    policy_authorizes_execution,
    policy_declaration,
    policy_level_for_gate,
    request_execution,
    sync_staleness,
)
from kiro_crew.apps.builtins.spec_engine.engine.state import (
    LockLost,
    SpecLocked,
    SpecRef,
    StatePersistenceError,
    StateStore,
)

from .conftest import make_spec_dir, spec_dir_snapshot
from .test_phases import SPEC_NAME, approve_gates, edit, write_spec

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
def project_tree(tmp_path: Path) -> Path:
    """A project holding one spec whose three documents are real and format-clean."""
    return write_spec(tmp_path / "workspace")


@pytest.fixture()
def ref(project_tree: Path) -> SpecRef:
    """A spec whose three documents are real and format-clean."""
    return SpecRef.of(project_tree, SPEC_NAME)


def settle_plan(
    store: StateStore,
    ref: SpecRef,
    *,
    decision: AutonomyDecision | None = None,
    user: str = USER,
) -> None:
    """Approve every document gate, leaving authority as the only open question."""
    for gate in DOCUMENT_GATES:
        outcome = (
            approve_by_policy(store, ref, gate, decision=decision)
            if decision is not None
            else approve(store, ref, gate, actor=user)
        )
        assert outcome.ok, [str(reason) for reason in outcome.reasons]


def mutate_tasks(project: Path, transform, *, name: str = SPEC_NAME) -> None:
    """Rewrite tasks.md through *transform*, leaving the other documents alone."""
    path = project / ".kiro" / "specs" / name / DocumentKind.TASKS.filename
    path.write_text(transform(path.read_text(encoding="utf-8")), encoding="utf-8")


def break_task_reference(text: str) -> str:
    """Point one leaf's criteria reference at a requirement that does not exist.

    Format-neutral on purpose: tasks.md still satisfies every native rule, so the
    defect is only visible by reading requirements.md as well.
    """
    mutated, count = re.subn(r"_Requirements: [^_]*_", "_Requirements: 99.1_", text, count=1)
    assert count == 1, "tasks.md carries no criteria reference to break"
    return mutated


def cycle_the_graph(text: str) -> str:
    """Give the waves graph a two-task dependency cycle, leaving the format valid."""
    block = re.search(r"```json\n(.*?)\n```", text, re.S)
    assert block is not None, "tasks.md carries no dependency graph block"
    graph = json.loads(block.group(1))
    first, second = graph["waves"][0]["tasks"][:2]
    graph["dependencies"] = {first: [second], second: [first]}
    return text[: block.start(1)] + json.dumps(graph) + text[block.end(1) :]


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
        # The refusal must not borrow the approver scheme. It approved nothing,
        # so a reader scanning the trail for policy approvals must not find this
        # event among them.
        assert not is_policy_actor(refused[0].initiator)

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


# --- The execution gate ----------------------------------------------------


def started_events(log: AuditLog, ref: SpecRef) -> list[AuditEvent]:
    return [event for event in log.read(ref) if event.event == EXECUTION_STARTED_EVENT]


def refused_events(log: AuditLog, ref: SpecRef) -> list[AuditEvent]:
    return [event for event in log.read(ref) if event.event == EXECUTION_REFUSED_EVENT]


def initiator_of(event: AuditEvent) -> str:
    """The event's recorded initiator, asserted present.

    An audited outcome with no initiator is the defect the assertion is here to
    catch: the trail would say execution was refused or started and not by whom.
    """
    assert event.initiator is not None
    return event.initiator


class TestExecutionAuthority:
    """Who may start execution, once the spec itself is executable."""

    def test_a_human_reserved_run_waits_for_an_explicit_human_action(self, store, ref, log):
        """An unconfigured source reserves execution, so nobody asking means nobody starts."""
        decision = decision_for(None)
        settle_plan(store, ref)

        outcome = request_execution(store, ref, decision=decision, audit=log)

        assert not outcome.ok
        assert outcome.reason_codes == (REASON_HUMAN_REQUIRED,)
        assert outcome.reasons[0].gate == EXECUTION_GATE
        assert outcome.human_reserved
        assert outcome.started_ts is None
        assert not started_events(log, ref)

    def test_a_configured_authoring_level_also_reserves_execution(self, store, ref, log):
        """Presence means exactly what it says; nothing rounds up to the next rung."""
        settle_plan(store, ref)

        outcome = request_execution(store, ref, decision=decision_for("authoring"), audit=log)

        assert not outcome.ok
        assert outcome.reason_codes == (REASON_HUMAN_REQUIRED,)
        assert not started_events(log, ref)

    def test_an_explicit_human_action_starts_a_human_reserved_run(self, store, ref, log):
        settle_plan(store, ref)

        outcome = request_execution(store, ref, decision=decision_for(None), user=USER, audit=log)

        assert outcome.ok, [str(reason) for reason in outcome.reasons]
        assert outcome.human_reserved
        assert outcome.initiator == USER
        assert outcome.initiator_kind == INITIATOR_USER
        assert not outcome.by_policy

    def test_an_authorized_policy_starts_with_no_further_trigger(self, store, ref, log):
        """One call is the whole gate: no second request, no trigger event, no human."""
        decision = decision_for("execution")
        settle_plan(store, ref, decision=decision)

        outcome = request_execution(store, ref, decision=decision, audit=log)

        assert outcome.ok, [str(reason) for reason in outcome.reasons]
        assert outcome.by_policy
        assert not outcome.human_reserved
        assert outcome.initiator == policy_actor(decision)
        assert outcome.initiator_kind == INITIATOR_POLICY
        assert derive_phase(store, ref).phase is Phase.READY

    def test_every_level_from_execution_upward_starts_unattended(self, store, log, tmp_path):
        for level in ("execution", "delivery", "integration"):
            project = write_spec(tmp_path / f"level-{level}")
            spec = SpecRef.of(project, SPEC_NAME)
            decision = decision_for(level)
            settle_plan(store, spec, decision=decision)

            outcome = request_execution(store, spec, decision=decision, audit=log)

            assert outcome.ok, [str(reason) for reason in outcome.reasons]
            assert outcome.by_policy

    def test_a_human_may_start_a_run_the_policy_already_authorizes(self, store, ref, log):
        """Authority to proceed unasked is not a monopoly on asking."""
        decision = decision_for("integration")
        settle_plan(store, ref, decision=decision)

        outcome = request_execution(store, ref, decision=decision, user=USER, audit=log)

        assert outcome.ok
        assert outcome.initiator == USER
        assert not outcome.by_policy
        assert [event.initiator for event in started_events(log, ref)] == [USER]

    def test_a_permissive_but_undeclared_decision_cannot_start_execution(self, store, ref, log):
        """A level with no declaration behind it is the unconfigured default's shape.

        Nothing in configuration produces this today. It is what a resolver whose
        unconfigured default moved up the ladder would produce, and the gate has to
        refuse it: an install that configured nothing must never execute unattended,
        so authority asks for a declaration and not only for a rung.
        """
        undeclared = AutonomyDecision(
            level=AutonomyLevel.INTEGRATION,
            source=SOURCE,
            spec_type="feature",
            submitter_class="member",
        )
        settle_plan(store, ref)

        assert undeclared.permits(AutonomyLevel.EXECUTION)
        assert not policy_authorizes_execution(undeclared)

        outcome = request_execution(store, ref, decision=undeclared, audit=log)

        assert not outcome.ok
        assert outcome.reason_codes == (REASON_HUMAN_REQUIRED,)
        assert not started_events(log, ref)

    def test_a_forged_human_initiator_is_refused_where_the_policy_would_have_started(
        self, store, ref, log
    ):
        """The reserved scheme is engine-issued, so no person can be wearing one."""
        decision = decision_for("integration")
        settle_plan(store, ref, decision=decision)

        outcome = request_execution(
            store, ref, decision=decision, user=policy_actor(decision), audit=log
        )

        assert not outcome.ok
        assert outcome.reason_codes == (REASON_HUMAN_REQUIRED,)
        assert not started_events(log, ref)
        assert not is_policy_actor(outcome.initiator)

    def test_an_empty_initiator_is_a_driver_bug(self, store, ref, log):
        """Either a request names a human or it names none; blank is neither."""
        settle_plan(store, ref)

        for blank in ("", "   "):
            with pytest.raises(ValueError):
                request_execution(store, ref, decision=decision_for(None), user=blank, audit=log)
        assert not started_events(log, ref)


class TestExecutionRefusesRegardlessOfPolicy:
    """The load-bearing direction: the most permissive policy is refused the same.

    Each test here configures integration -- every rung the ladder has -- and then
    breaks something about the spec. The permissive path is exactly where a
    wildcard fallthrough would hide, so it is the path under test rather than the
    restrictive one.
    """

    def test_a_missing_approval_refuses_under_a_policy_that_permits_everything(
        self, store, ref, log
    ):
        decision = decision_for("integration")
        approve_gates(store, ref, "requirements", "design")  # tasks left unapproved

        outcome = request_execution(store, ref, decision=decision, audit=log)

        assert not outcome.ok
        assert REASON_APPROVAL_MISSING in outcome.reason_codes
        assert not outcome.human_reserved  # the policy is permissive; the gate still refused
        assert not started_events(log, ref)

    def test_a_stale_approval_refuses_under_a_policy_that_permits_everything(
        self, store, ref, project_tree, log
    ):
        decision = decision_for("integration")
        settle_plan(store, ref, decision=decision)
        edit(project_tree, DocumentKind.REQUIREMENTS)

        outcome = request_execution(store, ref, decision=decision, audit=log)

        assert not outcome.ok
        assert REASON_APPROVAL_STALE in outcome.reason_codes
        assert not started_events(log, ref)

    def test_an_invalid_document_refuses_under_a_policy_that_permits_everything(
        self, store, project, log
    ):
        """conftest's project holds title-only documents: real files, invalid format."""
        spec = SpecRef.of(project, "example")

        outcome = request_execution(store, spec, decision=decision_for("integration"), audit=log)

        assert not outcome.ok
        assert REASON_DOCUMENT_INVALID in outcome.reason_codes
        assert rules.SECTION_MISSING in outcome.rule_ids
        assert outcome.report is not None and not outcome.report.ok
        assert not started_events(log, spec)

    def test_an_unresolvable_criteria_reference_refuses_execution(
        self, store, ref, project_tree, log
    ):
        """tasks.md is format-clean, so only reading requirements.md finds this."""
        decision = decision_for("integration")
        mutate_tasks(project_tree, break_task_reference)
        settle_plan(store, ref, decision=decision)

        outcome = request_execution(store, ref, decision=decision, audit=log)

        assert not outcome.ok
        assert REASON_TASKS_PLAN_INVALID in outcome.reason_codes
        assert rules.TASK_REFERENCE_REQUIREMENT_UNKNOWN in outcome.rule_ids
        assert not started_events(log, ref)

    def test_a_cyclic_dependency_graph_refuses_execution(self, store, ref, project_tree, log):
        """The orchestrator dispatches this graph wave by wave; a cycle has no order."""
        decision = decision_for("integration")
        mutate_tasks(project_tree, cycle_the_graph)
        settle_plan(store, ref, decision=decision)

        outcome = request_execution(store, ref, decision=decision, audit=log)

        assert not outcome.ok
        assert REASON_TASKS_PLAN_INVALID in outcome.reason_codes
        assert rules.GRAPH_CYCLE in outcome.rule_ids
        assert not started_events(log, ref)

    def test_an_explicit_human_action_cannot_override_a_blocked_spec(self, store, ref, log):
        """A person asking is authority to start, never permission to skip the plan."""
        approve_gates(store, ref, "requirements")

        outcome = request_execution(
            store, ref, decision=decision_for("integration"), user=USER, audit=log
        )

        assert not outcome.ok
        assert REASON_APPROVAL_MISSING in outcome.reason_codes
        assert not started_events(log, ref)

    def test_the_reasons_a_spec_is_unexecutable_do_not_depend_on_the_policy(
        self, store, ref, log, tmp_path
    ):
        """Same broken spec, every rung: one identical list of blocking reasons."""
        approve_gates(store, ref, "requirements", "design")
        seen = set()

        for level in (None, *AUTONOMY_LEVELS):
            outcome = request_execution(store, ref, decision=decision_for(level), audit=log)
            seen.add(tuple(code for code in outcome.reason_codes if code != REASON_HUMAN_REQUIRED))

        assert seen == {(REASON_APPROVAL_MISSING,)}


class TestExecutionAudit:
    """Both outcomes are recorded, and the initiator is what makes them readable."""

    def test_a_started_execution_records_its_initiator_and_timestamp(self, store, ref, log):
        settle_plan(store, ref)

        outcome = request_execution(store, ref, decision=decision_for(None), user=USER, audit=log)

        events = started_events(log, ref)
        assert len(events) == 1
        assert events[0].initiator == USER
        assert events[0].ts == outcome.started_ts
        assert events[0].ts.endswith("+00:00")
        assert events[0].detail is not None
        assert events[0].detail["initiator_kind"] == INITIATOR_USER
        assert events[0].detail["human_reserved"] is True
        assert events[0].detail["autonomy_level"] == AutonomyLevel.AUTHORING.value

    def test_an_autonomous_start_records_the_declaration_that_authorized_it(self, store, ref, log):
        decision = decision_for("execution")
        settle_plan(store, ref, decision=decision)

        request_execution(store, ref, decision=decision, audit=log)

        event = started_events(log, ref)[0]
        assert event.initiator == policy_actor(decision)
        assert is_policy_actor(event.initiator)
        assert policy_declaration(event.initiator) == decision.declared_at
        assert event.detail is not None
        assert event.detail["initiator_kind"] == INITIATOR_POLICY
        assert event.detail["policy_declared_at"] == decision.declared_at
        assert event.detail["human_reserved"] is False

    def test_a_refused_request_records_its_initiator_and_reasons(self, store, ref, log):
        approve_gates(store, ref, "requirements")

        request_execution(store, ref, decision=decision_for(None), user=USER, audit=log)

        events = refused_events(log, ref)
        assert len(events) == 1
        assert events[0].initiator == USER
        assert events[0].detail is not None
        assert events[0].detail["gate"] == EXECUTION_GATE
        codes = [reason["code"] for reason in events[0].detail["reasons"]]
        assert REASON_APPROVAL_MISSING in codes

    @pytest.mark.parametrize(
        "level, blocked",
        [
            (None, False),
            ("authoring", False),
            ("integration", True),
        ],
    )
    def test_a_refusal_never_records_the_policys_approver_identity(
        self, store, log, tmp_path, level, blocked
    ):
        """Every way a request is refused, in the one field a reader scans.

        ``scheme:path`` means the policy authorized something. A refusal wearing it
        would make an operator auditing what autonomy let through find a gate the
        policy never opened -- including the case that matters most, a policy that
        *did* authorize execution whose spec was refused on validation.
        """
        project = write_spec(tmp_path / f"refusal-{level}-{blocked}")
        spec = SpecRef.of(project, SPEC_NAME)
        decision = decision_for(level)
        if not blocked:
            settle_plan(store, spec, user="user:grace")

        outcome = request_execution(store, spec, decision=decision, audit=log)

        assert not outcome.ok
        event = refused_events(log, spec)[0]
        assert not is_policy_actor(initiator_of(event))
        assert not is_policy_actor(outcome.initiator)
        assert event.initiator == outcome.initiator
        assert event.detail is not None
        # The declaration stays legible even though the scheme is not borrowed.
        assert event.detail["policy_declared_at"] == (decision.declared_at or None)

    def test_a_forged_initiator_is_not_recorded_as_an_approver(self, store, ref, log):
        decision = decision_for("integration")
        settle_plan(store, ref, decision=decision)

        request_execution(store, ref, decision=decision, user=policy_actor(decision), audit=log)

        event = refused_events(log, ref)[0]
        assert not is_policy_actor(initiator_of(event))

    def test_a_refused_policy_approval_is_not_recorded_as_an_approver(self, store, ref, log):
        """The same rule on the approval path: a configured level too low to approve."""
        outcome = approve_by_policy(store, ref, "requirements", decision=decision_for("authoring"))
        assert not outcome.ok

        approve_by_policy(store, ref, "requirements", decision=decision_for("authoring"), audit=log)

        refused = [event for event in log.read(ref) if event.event == APPROVAL_REFUSED_EVENT]
        assert refused and not any(is_policy_actor(initiator_of(e)) for e in refused)

    def test_an_audit_failure_fails_the_request(self, store, ref, log, monkeypatch):
        """An execution nobody could record must not begin: the append is the record."""
        decision = decision_for("execution")
        settle_plan(store, ref, decision=decision)

        def unwritable(*args, **kwargs):
            raise StatePersistenceError("audit log is unwritable")

        monkeypatch.setattr(log, "append", unwritable)

        with pytest.raises(StatePersistenceError):
            request_execution(store, ref, decision=decision, audit=log)

    def test_the_gate_writes_nothing_into_the_spec_directory(self, store, ref, log):
        decision = decision_for("execution")
        settle_plan(store, ref, decision=decision)
        before = spec_dir_snapshot(ref.spec_dir)

        request_execution(store, ref, decision=decision, audit=log)
        request_execution(store, ref, decision=decision_for(None), audit=log)

        assert spec_dir_snapshot(ref.spec_dir) == before


class TestExecutionSerialisation:
    """Two drivers must not both start one spec."""

    def test_a_second_writer_is_rejected_while_the_spec_is_locked(self, store, ref, log):
        decision = decision_for("execution")
        settle_plan(store, ref, decision=decision)

        with store.lock(ref, owner="other-session"):
            with pytest.raises(SpecLocked):
                request_execution(store, ref, decision=decision, audit=log)

        assert not started_events(log, ref)

    def test_a_request_may_reuse_a_lock_its_caller_already_holds(self, store, ref, log):
        decision = decision_for("execution")
        settle_plan(store, ref, decision=decision)

        with store.lock(ref, owner="driver") as handle:
            outcome = request_execution(store, ref, decision=decision, audit=log, lock=handle)

        assert outcome.ok, [str(reason) for reason in outcome.reasons]

    def test_a_lock_taken_over_mid_request_records_no_start(self, store, ref, log, monkeypatch):
        """Acquisition is not the whole guarantee.

        The lock can expire underneath the validation the gate just ran and be
        taken over by a second writer. Only the re-verification before the audit
        append stands between that and two recorded starts for one spec.
        """
        decision = decision_for("execution")
        settle_plan(store, ref, decision=decision)

        def taken_over(handle):
            raise LockLost(SPEC_NAME)

        monkeypatch.setattr(store, "verify_lock", taken_over)

        with pytest.raises(LockLost):
            request_execution(store, ref, decision=decision, audit=log)

        assert not started_events(log, ref)


# --- Property: authority and executability are independent -----------------


@settings(
    max_examples=MAX_EXAMPLES,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    level=st.one_of(st.none(), st.sampled_from(AUTONOMY_LEVELS)),
    approved=st.integers(min_value=0, max_value=len(DOCUMENT_GATES)),
    by_human=st.booleans(),
)
def test_execution_starts_exactly_when_the_plan_is_settled_and_someone_may_start(
    tmp_path: Path, level: str | None, approved: int, by_human: bool
) -> None:
    """The gate is the conjunction of two independent facts, over the whole grid.

    Executable comes from the spec: every document gate approved. Authorized comes
    from the request: a human asked, or a configured level reaches the execution
    rung. Neither substitutes for the other, and the expectation here is computed
    from the two facts rather than from the gate's own code -- so a permissive
    policy that started an unapproved spec, or a settled spec that refused a
    person, both fail this.
    """
    slug = f"{level}-{approved}-{by_human}"
    project = write_spec(tmp_path / f"grid-{slug}")
    ref = SpecRef.of(project, SPEC_NAME)
    store = StateStore(root=tmp_path / f"state-{slug}")
    log = AuditLog(root=tmp_path / f"state-{slug}")
    try:
        decision = decision_for(level)
        for gate in DOCUMENT_GATES[:approved]:
            assert approve(store, ref, gate, actor=USER).ok

        outcome = request_execution(
            store, ref, decision=decision, user=USER if by_human else None, audit=log
        )

        settled = approved == len(DOCUMENT_GATES)
        authorized = by_human or (
            level is not None and AutonomyLevel(level).permits(AutonomyLevel.EXECUTION)
        )
        assert outcome.ok is (settled and authorized)
        assert bool(started_events(log, ref)) is outcome.ok
        if not settled:
            # Refused for the spec's own state, whatever the policy permitted.
            assert REASON_APPROVAL_MISSING in outcome.reason_codes
        if not authorized:
            assert REASON_HUMAN_REQUIRED in outcome.reason_codes
        if not outcome.ok:
            assert not is_policy_actor(initiator_of(refused_events(log, ref)[0]))
    finally:
        store.close()


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
