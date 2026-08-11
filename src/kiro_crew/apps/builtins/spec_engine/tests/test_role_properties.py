"""Property-based test for role routing.

**Resolution is total, and never silently substitutes.** Whatever a profile says
or omits, every role resolves to something dispatchable, an assignment the profile
did make is used verbatim, and every resolution that did *not* come from the
profile carries a report. The scripted cases cover the profile shapes someone
thought to write down; the failure this guards against is a shape nobody thought
of resolving to the session default with nothing said about it, which is
indistinguishable afterwards from a run that used the model it was configured for.
"""

from __future__ import annotations

from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

from kiro_crew.apps.builtins.spec_engine.engine.config import ROLES
from kiro_crew.apps.builtins.spec_engine.engine.roles import (
    RolePlan,
    RoleSource,
    SessionDefault,
    WorkKind,
    role_for,
)

#: Resolution is pure and in-memory, so examples are cheap; well above the number
#: of distinct profile shapes the scripted cases cover.
MAX_EXAMPLES = 200

PROJECT = "acme"
PROFILE = "generated"

SESSION = SessionDefault(agent="session-agent", model="session-model")

#: Model ids that differ in whether they accept a reasoning effort, so generated
#: examples exercise both sides of the capability check.
_MODELS = st.sampled_from(["", "claude-sonnet-4.6", "claude-haiku-4.5", "auto"])
_EFFORTS = st.sampled_from(["", "low", "high", "max"])
_AGENTS = st.sampled_from(["", "reviewer", "coder"])

_ASSIGNMENTS = st.fixed_dictionaries({"agent": _AGENTS, "model": _MODELS, "effort": _EFFORTS}).map(
    lambda entry: {key: value for key, value in entry.items() if value}
)

#: An arbitrary role table, including the empty one and roles that name nothing.
_ROLE_TABLES = st.dictionaries(st.sampled_from(ROLES), _ASSIGNMENTS, max_size=len(ROLES))

_SELECTIONS = st.sampled_from(["", PROFILE, "not-defined"])


def _document(roles: dict[str, Any], selection: str) -> dict[str, Any]:
    project: dict[str, Any] = {"path": "/w/acme"}
    if selection:
        project["cost_profile"] = selection
    return {"cost_profiles": {PROFILE: {"roles": roles}}, "projects": {PROJECT: project}}


@settings(max_examples=MAX_EXAMPLES)
@given(roles=_ROLE_TABLES, selection=_SELECTIONS)
def test_every_role_resolves_and_every_substitution_is_reported(
    roles: dict[str, Any], selection: str
):
    plan = RolePlan.from_document(
        _document(roles, selection), project=PROJECT, session_default=SESSION
    )
    for role in ROLES:
        resolved = plan.role(role)
        assignment = roles.get(role) if selection == PROFILE else None
        assigned_model = (assignment or {}).get("model", "")
        if assigned_model:
            # An assignment the profile made is used, not adjusted.
            assert resolved.model == assigned_model
            assert resolved.source is RoleSource.COST_PROFILE
            assert resolved.agent == (assignment or {}).get("agent", "") or SESSION.agent
        else:
            # Anything else fell back, and saying so is the requirement.
            assert resolved.model == SESSION.model
            assert resolved.fallback is not None
            assert resolved.report
            assert role in resolved.report


@settings(max_examples=MAX_EXAMPLES)
@given(roles=_ROLE_TABLES, selection=_SELECTIONS)
def test_a_subagent_dispatch_never_diverges_from_its_roles_resolution(
    roles: dict[str, Any], selection: str
):
    plan = RolePlan.from_document(
        _document(roles, selection), project=PROJECT, session_default=SESSION
    )
    for kind in WorkKind:
        inline = plan.dispatch(kind)
        spawned = plan.dispatch(kind, subagent=True)
        assert inline.resolved == spawned.resolved == plan.role(role_for(kind))
        assert spawned.spawn_options().get("model", "") == inline.model
