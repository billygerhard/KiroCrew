"""Role routing: what each unit of a run's work runs on, and what gets reported.

What these tests hold: every work unit resolves to a role, a role with an
assignment runs on that assignment, a role without one falls back to the session
default *and says so*, a subagent dispatch carries the run's assignment rather
than a default, and the plan a run executes under does not change under it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from kiro_crew.apps.builtins.spec_engine.engine.config import (
    DASHBOARD_SURFACE,
    ROLES,
    ConfigStore,
)
from kiro_crew.apps.builtins.spec_engine.engine.roles import (
    ROLE_FOR_KIND,
    Dispatch,
    FallbackReason,
    RolePlan,
    RoleSource,
    SessionDefault,
    WorkKind,
    role_for,
)

#: Models used throughout. Concrete ids appear only in test data — never as a
#: default in the code under test — and are chosen for their effort capability:
#: the first accepts a reasoning effort, the second does not.
EFFORT_CAPABLE_MODEL = "claude-sonnet-4.6"
EFFORT_INCAPABLE_MODEL = "claude-haiku-4.5"

SESSION = SessionDefault(agent="session-agent", model="auto")

PROJECT = "acme"
PROFILE = "quality-first"


def document(roles: dict[str, Any], *, selected: str = PROFILE) -> dict[str, Any]:
    """A configuration document whose project selects a profile with *roles*."""
    return {
        "cost_profiles": {PROFILE: {"roles": roles}},
        "projects": {PROJECT: {"path": "/w/acme", "cost_profile": selected}},
    }


def plan(roles: dict[str, Any], *, selected: str = PROFILE, project: str | None = PROJECT):
    return RolePlan.from_document(
        document(roles, selected=selected), project=project, session_default=SESSION
    )


class TestRoleDetermination:
    def test_every_work_kind_has_a_role(self):
        assert set(ROLE_FOR_KIND) == set(WorkKind)
        assert set(ROLE_FOR_KIND.values()) <= set(ROLES)

    @pytest.mark.parametrize(
        ("kind", "expected"),
        [
            (WorkKind.REQUIREMENTS_AUTHORING, "design"),
            (WorkKind.DESIGN_AUTHORING, "design"),
            (WorkKind.TASKS_AUTHORING, "design"),
            (WorkKind.DOCUMENT_REVISION, "design"),
            (WorkKind.SPEC_REVIEW, "review"),
            (WorkKind.TASK_REVIEW, "review"),
            (WorkKind.DELIVERY_REVIEW, "review"),
            (WorkKind.INTAKE_SCREENING, "review"),
            (WorkKind.TASK_IMPLEMENTATION, "implement"),
            (WorkKind.FIX_TASK, "implement"),
            (WorkKind.ANALYSIS, "analysis"),
            (WorkKind.SETUP_INTERVIEW, "setup"),
        ],
    )
    def test_work_units_route_to_their_role(self, kind: WorkKind, expected: str):
        assert role_for(kind) == expected


class TestAssignedRoles:
    def test_an_assigned_role_runs_on_its_assignment(self):
        resolved = plan(
            {
                "implement": {
                    "agent": "coder",
                    "model": EFFORT_CAPABLE_MODEL,
                    "effort": "low",
                }
            }
        ).dispatch(WorkKind.TASK_IMPLEMENTATION)
        assert resolved.agent == "coder"
        assert resolved.model == EFFORT_CAPABLE_MODEL
        assert resolved.effort == "low"
        assert resolved.resolved.source is RoleSource.COST_PROFILE
        assert resolved.resolved.fallback is None
        assert resolved.report == ""

    def test_an_assignment_without_an_agent_keeps_the_session_agent(self):
        resolved = plan({"review": {"model": EFFORT_CAPABLE_MODEL}}).dispatch(WorkKind.SPEC_REVIEW)
        assert resolved.agent == SESSION.agent
        assert resolved.model == EFFORT_CAPABLE_MODEL
        assert resolved.resolved.source is RoleSource.COST_PROFILE

    def test_turn_options_carry_the_decision_and_omit_what_is_unpinned(self):
        resolved = plan({"review": {"model": EFFORT_CAPABLE_MODEL}}).dispatch(WorkKind.TASK_REVIEW)
        assert resolved.turn_options() == {
            "agent": SESSION.agent,
            "model": EFFORT_CAPABLE_MODEL,
        }

    def test_the_declaration_path_is_reported_for_a_configuration_surface(self):
        resolved = plan({"design": {"model": EFFORT_CAPABLE_MODEL}}).role("design")
        assert resolved.declared_at == f"cost_profiles.{PROFILE}.roles.design"


class TestFallbackIsReported:
    def test_an_unassigned_role_falls_back_to_the_session_default(self):
        resolved = plan({"implement": {"model": EFFORT_CAPABLE_MODEL}}).dispatch(
            WorkKind.SPEC_REVIEW
        )
        assert resolved.agent == SESSION.agent
        assert resolved.model == SESSION.model
        assert resolved.resolved.source is RoleSource.SESSION_DEFAULT

    def test_the_fallback_is_reported_rather_than_silent(self):
        resolved = plan({"implement": {"model": EFFORT_CAPABLE_MODEL}}).role("review")
        assert resolved.fallback is FallbackReason.ROLE_UNASSIGNED
        assert resolved.reported
        assert "review" in resolved.report
        assert PROFILE in resolved.report
        assert resolved.detail()["fallback"] == FallbackReason.ROLE_UNASSIGNED.value

    def test_a_project_with_no_selected_profile_reports_that(self):
        resolved = RolePlan.from_document(
            {"projects": {PROJECT: {"path": "/w/acme"}}},
            project=PROJECT,
            session_default=SESSION,
        )
        assert resolved.profile == ""
        for role in ROLES:
            assert resolved.role(role).fallback is FallbackReason.NO_PROFILE_SELECTED
        assert len(resolved.reports) == len(ROLES)
        assert "no cost profile is selected" in resolved.reports[0]

    def test_a_selected_profile_that_does_not_exist_names_the_missing_name(self):
        resolved = plan({"review": {"model": EFFORT_CAPABLE_MODEL}}, selected="tier-one")
        assert resolved.profile == ""
        assert resolved.requested_profile == "tier-one"
        report = resolved.role("review").report
        assert "tier-one" in report
        assert resolved.role("review").fallback is FallbackReason.PROFILE_NOT_DEFINED
        assert resolved.detail()["requested_profile"] == "tier-one"

    def test_an_assignment_missing_its_model_keeps_the_agent_and_reports(self):
        resolved = plan({"review": {"agent": "reviewer"}}).role("review")
        assert resolved.agent == "reviewer"
        assert resolved.model == SESSION.model
        assert resolved.fallback is FallbackReason.MODEL_UNASSIGNED
        assert "no model" in resolved.report

    def test_every_fallback_appears_in_the_plan_reports(self):
        resolved = plan({"implement": {"model": EFFORT_CAPABLE_MODEL}})
        assert {role.role for role in resolved.fallbacks} == set(ROLES) - {"implement"}
        assert len(resolved.reports) == len(ROLES) - 1


class TestEffortCapability:
    def test_an_effort_the_model_cannot_take_is_dropped_and_reported(self):
        resolved = plan({"implement": {"model": EFFORT_INCAPABLE_MODEL, "effort": "high"}}).role(
            "implement"
        )
        assert resolved.effort == ""
        assert resolved.dropped_effort == "high"
        assert "high" in resolved.report
        assert resolved.source is RoleSource.COST_PROFILE

    def test_an_effort_pinned_against_an_unresolved_session_model_is_dropped(self):
        # The session default here is "auto", which is not a model that accepts an
        # effort level; sending one fails the turn outright.
        resolved = plan({"implement": {"agent": "coder", "effort": "high"}}).role("implement")
        assert resolved.model == SESSION.model
        assert resolved.effort == ""
        assert resolved.dropped_effort == "high"

    def test_an_unpinned_model_leaves_a_pinned_effort_alone(self):
        # Nothing anywhere named a model, so it resolves at the provider and its
        # capability is not knowable here; dropping the effort would discard a
        # decision on a guess.
        resolved = RolePlan.from_document(
            document({"implement": {"agent": "coder", "effort": "high"}}),
            project=PROJECT,
            session_default=SessionDefault(agent="session-agent"),
        ).role("implement")
        assert resolved.model == ""
        assert resolved.effort == "high"
        assert resolved.dropped_effort == ""


class TestSubagentInheritance:
    def test_a_subagent_dispatch_carries_the_runs_assignment(self):
        resolved = plan(
            {"implement": {"agent": "coder", "model": EFFORT_CAPABLE_MODEL, "effort": "low"}}
        ).dispatch(WorkKind.TASK_IMPLEMENTATION, subagent=True)
        assert resolved.subagent is True
        assert resolved.spawn_options() == {"agent": "coder", "model": EFFORT_CAPABLE_MODEL}
        assert resolved.detail()["subagent"] is True

    def test_a_subagent_and_an_inline_turn_resolve_identically(self):
        run_plan = plan({"review": {"agent": "reviewer", "model": EFFORT_CAPABLE_MODEL}})
        inline = run_plan.dispatch(WorkKind.TASK_REVIEW)
        spawned = run_plan.dispatch(WorkKind.TASK_REVIEW, subagent=True)
        assert inline.resolved == spawned.resolved

    def test_the_plan_is_a_snapshot_a_later_config_edit_cannot_change(self, tmp_path: Path):
        store = ConfigStore(tmp_path / "state")
        store.write(
            {
                "cost_profiles": {PROFILE: {"roles": {"implement": {"model": "model-one"}}}},
                "projects": {PROJECT: {"path": "/w/acme", "cost_profile": PROFILE}},
            },
            surface=DASHBOARD_SURFACE,
        )
        run_plan = RolePlan.for_run(store, project=PROJECT, session_default=SESSION)
        store.write(
            {"cost_profiles": {PROFILE: {"roles": {"implement": {"model": "model-two"}}}}},
            surface=DASHBOARD_SURFACE,
        )
        assert run_plan.dispatch(WorkKind.TASK_IMPLEMENTATION, subagent=True).model == "model-one"
        fresh = RolePlan.for_run(store, project=PROJECT, session_default=SESSION)
        assert fresh.dispatch(WorkKind.TASK_IMPLEMENTATION).model == "model-two"

    def test_spawn_options_leave_out_effort_the_spawn_seam_cannot_take(self):
        resolved: Dispatch = plan(
            {"implement": {"model": EFFORT_CAPABLE_MODEL, "effort": "max"}}
        ).dispatch(WorkKind.FIX_TASK, subagent=True)
        assert "effort" not in resolved.spawn_options()
        assert resolved.effort == "max"


class TestProjectlessRuns:
    def test_a_run_with_no_project_resolves_from_the_session_default(self):
        resolved = RolePlan.from_document({}, project=None, session_default=SESSION)
        assert resolved.profile == ""
        for role in ROLES:
            assert resolved.role(role).model == SESSION.model
            assert resolved.role(role).fallback is FallbackReason.NO_PROFILE_SELECTED
