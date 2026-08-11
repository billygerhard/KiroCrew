"""Role routing: which agent, model, and effort each unit of a run's work uses.

Every dispatch a run makes belongs to a role — the design role authors documents,
the review role judges them, the implement role writes code, the analysis role
reads for meaning, the setup role interviews the operator — and the project's
selected Cost_Profile says what each role runs on. This module turns a work unit
into that decision and hands it back as a :class:`Dispatch` the caller passes
per-call.

Per-call is not a style choice. The host's own ``role_models`` map is a closed
allowlist of two keys and drops anything else, so a spec role could not be
expressed there even by writing to it: an added ``review`` key would be discarded
on read and the run would quietly use the default model. Agent, model, and effort
therefore travel with each dispatch, which is how the seams that spawn a subagent
and start a turn already accept them.

**A fallback is reported, never silent.** When a role has no assignment the
dispatch resolves to the session's own agent and model, and the resolution says
so, naming the role and why. That report is the whole point of the mechanism: a
run configured to be reviewed by a careful model, whose review role was never
assigned, is reviewed by whatever the session defaults to — and without a report
the only evidence is a verdict that looks exactly like a real one.

**A run's assignments are a snapshot.** :meth:`RolePlan.for_run` resolves every
role once, at the start, and every later dispatch reads that plan. A subagent
therefore inherits the run's assignment rather than re-resolving, so a mid-run
configuration edit cannot leave half a run's tasks on one model and half on
another, and a subagent is never handed a default because it looked up a role
nobody assigned since.

**An effort a model cannot take is dropped, not sent.** Effort is rejected at the
wire by models that do not support it, which fails the turn rather than degrading
it. A concrete model that declares no support has its pinned effort dropped with a
report; an unpinned model is left alone, because "inherit" is not a model whose
capability can be judged yet.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping

from kiro_crew.effort import model_supports_effort

from .config import ROLES, ConfigStore
from .config.profiles import CostProfile, RoleAssignment, selected_profile

#: Roles, one per kind of work a run dispatches. Named here as the module's own
#: vocabulary so a reader of the routing path does not have to open the schema,
#: while the schema stays the single definition of the set.
ROLE_DESIGN = "design"
ROLE_REVIEW = "review"
ROLE_IMPLEMENT = "implement"
ROLE_ANALYSIS = "analysis"
ROLE_SETUP = "setup"


class WorkKind(str, Enum):
    """One unit of work a run dispatches, at the granularity roles differ on."""

    REQUIREMENTS_AUTHORING = "requirements_authoring"
    DESIGN_AUTHORING = "design_authoring"
    TASKS_AUTHORING = "tasks_authoring"
    DOCUMENT_REVISION = "document_revision"
    SPEC_REVIEW = "spec_review"
    TASK_REVIEW = "task_review"
    DELIVERY_REVIEW = "delivery_review"
    INTAKE_SCREENING = "intake_screening"
    TASK_IMPLEMENTATION = "task_implementation"
    FIX_TASK = "fix_task"
    ANALYSIS = "analysis"
    SETUP_INTERVIEW = "setup_interview"


#: Which role each kind of work belongs to. Total over :class:`WorkKind`, because
#: a work kind with no role would resolve to nothing at dispatch time — the one
#: moment there is no good answer to give.
ROLE_FOR_KIND: dict[WorkKind, str] = {
    WorkKind.REQUIREMENTS_AUTHORING: ROLE_DESIGN,
    WorkKind.DESIGN_AUTHORING: ROLE_DESIGN,
    WorkKind.TASKS_AUTHORING: ROLE_DESIGN,
    WorkKind.DOCUMENT_REVISION: ROLE_DESIGN,
    WorkKind.SPEC_REVIEW: ROLE_REVIEW,
    WorkKind.TASK_REVIEW: ROLE_REVIEW,
    WorkKind.DELIVERY_REVIEW: ROLE_REVIEW,
    # Screening judges whether text is trying to steer the run, which is a review
    # judgment made on adversarial input; it runs at review quality for the same
    # reason a verdict does.
    WorkKind.INTAKE_SCREENING: ROLE_REVIEW,
    WorkKind.TASK_IMPLEMENTATION: ROLE_IMPLEMENT,
    WorkKind.FIX_TASK: ROLE_IMPLEMENT,
    WorkKind.ANALYSIS: ROLE_ANALYSIS,
    WorkKind.SETUP_INTERVIEW: ROLE_SETUP,
}


def role_for(kind: WorkKind) -> str:
    """The role *kind* of work belongs to."""
    return ROLE_FOR_KIND[kind]


class RoleSource(str, Enum):
    """Where a role's agent and model came from."""

    COST_PROFILE = "cost_profile"
    SESSION_DEFAULT = "session_default"


class FallbackReason(str, Enum):
    """Why a role fell back to the session default, if it did.

    Four distinct conditions rather than one "unconfigured": they are fixed in
    different places. Nothing selected a profile, the selected profile does not
    exist, the profile exists but says nothing about this role, and the profile
    names the role without naming a model.
    """

    NO_PROFILE_SELECTED = "no_cost_profile_selected"
    PROFILE_NOT_DEFINED = "selected_cost_profile_not_defined"
    ROLE_UNASSIGNED = "role_unassigned"
    MODEL_UNASSIGNED = "role_model_unassigned"


@dataclass(frozen=True)
class SessionDefault:
    """The agent and model a session runs on when a role assigns neither.

    Supplied by the caller rather than read from the host here: the value that
    matters is the one the *session doing the dispatching* is running under, which
    only that caller knows. Empty means "inherit whatever the provider serves",
    which is the entitlement-safe answer on every subscription tier.
    """

    agent: str = ""
    model: str = ""


@dataclass(frozen=True)
class ResolvedRole:
    """One role's resolved agent, model, and effort, and how it got there."""

    role: str
    agent: str = ""
    model: str = ""
    effort: str = ""
    source: RoleSource = RoleSource.SESSION_DEFAULT
    #: Name of the profile that decided this, empty when none did.
    profile: str = ""
    #: Dotted configuration path of the assignment, empty for a fallback.
    declared_at: str = ""
    fallback: FallbackReason | None = None
    #: Operator-facing sentence for the fallback, empty when there was none.
    report: str = ""
    #: Effort the profile pinned that the resolved model cannot accept.
    dropped_effort: str = ""

    @property
    def reported(self) -> bool:
        """Whether this resolution has something an operator should be told."""
        return bool(self.report)

    @property
    def from_profile(self) -> bool:
        return self.source is RoleSource.COST_PROFILE

    def detail(self) -> dict[str, Any]:
        """Audit/notification detail for this resolution."""
        record: dict[str, Any] = {
            "role": self.role,
            "source": self.source.value,
            "agent": self.agent,
            "model": self.model,
            "effort": self.effort,
        }
        if self.profile:
            record["profile"] = self.profile
        if self.declared_at:
            record["declared_at"] = self.declared_at
        if self.fallback is not None:
            record["fallback"] = self.fallback.value
            record["report"] = self.report
        if self.dropped_effort:
            record["dropped_effort"] = self.dropped_effort
        return record


@dataclass(frozen=True)
class Dispatch:
    """One work unit's routing decision, ready to pass to the dispatching seam."""

    kind: WorkKind
    resolved: ResolvedRole
    #: Whether this dispatch starts a subagent, which inherits the run's plan.
    subagent: bool = False

    @property
    def role(self) -> str:
        return self.resolved.role

    @property
    def agent(self) -> str:
        return self.resolved.agent

    @property
    def model(self) -> str:
        return self.resolved.model

    @property
    def effort(self) -> str:
        return self.resolved.effort

    @property
    def report(self) -> str:
        return self.resolved.report

    def turn_options(self) -> dict[str, str]:
        """Per-call agent, model, and effort for starting a turn.

        Empty values are omitted rather than passed as empty strings: the seams
        treat an absent argument as "inherit", and an explicit empty would have to
        be interpreted by every one of them the same way to mean the same thing.
        """
        options = {"agent": self.agent, "model": self.model, "effort": self.effort}
        return {key: value for key, value in options.items() if value}

    def spawn_options(self) -> dict[str, str]:
        """Per-call options for spawning a subagent.

        Effort is left out because the spawn seam takes no effort argument; a
        subagent's effort follows the model it is given. Keeping it out here rather
        than passing it and hoping is the difference between a known limitation and
        a silently ignored setting.
        """
        options = {"agent": self.agent, "model": self.model}
        return {key: value for key, value in options.items() if value}

    def detail(self) -> dict[str, Any]:
        """Audit detail for this dispatch."""
        record = self.resolved.detail()
        record["kind"] = self.kind.value
        record["subagent"] = self.subagent
        return record


@dataclass(frozen=True)
class RolePlan:
    """A run's role assignments, resolved once and read by every dispatch."""

    roles: Mapping[str, ResolvedRole]
    project: str | None = None
    #: The selected profile's name, empty when no profile is in force.
    profile: str = ""
    #: The name a project selected when that profile is not defined, so a caller
    #: can say which name was wrong rather than only that something was.
    requested_profile: str = ""
    session_default: SessionDefault = SessionDefault()

    @classmethod
    def for_run(
        cls,
        store: ConfigStore,
        *,
        project: str | None = None,
        session_default: SessionDefault = SessionDefault(),
    ) -> RolePlan:
        """Resolve every role for a run from the persisted configuration."""
        return cls.from_document(store.document(), project=project, session_default=session_default)

    @classmethod
    def from_document(
        cls,
        doc: Mapping[str, Any],
        *,
        project: str | None = None,
        session_default: SessionDefault = SessionDefault(),
    ) -> RolePlan:
        """Resolve every role against an in-memory configuration document."""
        selected, requested = selected_profile(doc, project)
        resolved = {
            role: _resolve_role(role, selected, requested, session_default) for role in ROLES
        }
        return cls(
            roles=resolved,
            project=project,
            profile=selected.name if selected is not None else "",
            requested_profile=requested,
            session_default=session_default,
        )

    def role(self, role: str) -> ResolvedRole:
        """The resolution for *role*; raises ``KeyError`` for an unknown role."""
        return self.roles[role]

    def dispatch(self, kind: WorkKind, *, subagent: bool = False) -> Dispatch:
        """The routing decision for one unit of work.

        A subagent dispatch reads the same plan as everything else, which is what
        makes it inherit the run's assignment instead of resolving one of its own.
        """
        return Dispatch(kind=kind, resolved=self.roles[role_for(kind)], subagent=subagent)

    @property
    def fallbacks(self) -> tuple[ResolvedRole, ...]:
        """Every role that fell back, in role order."""
        return tuple(self.roles[role] for role in ROLES if self.roles[role].fallback is not None)

    @property
    def reports(self) -> tuple[str, ...]:
        """Operator-facing sentences for everything worth reporting about the plan."""
        return tuple(self.roles[role].report for role in ROLES if self.roles[role].reported)

    def detail(self) -> dict[str, Any]:
        """Audit detail for the plan a run executes under."""
        record: dict[str, Any] = {
            "profile": self.profile,
            "roles": {role: self.roles[role].detail() for role in ROLES},
        }
        if self.project is not None:
            record["project"] = self.project
        if self.requested_profile and not self.profile:
            record["requested_profile"] = self.requested_profile
        return record


def _resolve_role(
    role: str,
    selected: CostProfile | None,
    requested: str,
    session_default: SessionDefault,
) -> ResolvedRole:
    """Resolve one role from the selected profile, falling back with a report."""
    assignment = selected.assignment(role) if selected is not None else None
    if selected is None:
        reason = (
            FallbackReason.PROFILE_NOT_DEFINED if requested else FallbackReason.NO_PROFILE_SELECTED
        )
        return _session_fallback(role, session_default, reason, requested)
    if assignment is None:
        return _session_fallback(
            role, session_default, FallbackReason.ROLE_UNASSIGNED, selected.name
        )
    if not assignment.assigns_model:
        return _partial_assignment(role, selected, assignment, session_default)
    return _from_assignment(
        role,
        selected,
        assignment,
        agent=assignment.agent or session_default.agent,
        model=assignment.model,
        fallback=None,
        report="",
    )


def _from_assignment(
    role: str,
    selected: CostProfile,
    assignment: RoleAssignment,
    *,
    agent: str,
    model: str,
    fallback: FallbackReason | None,
    report: str,
) -> ResolvedRole:
    effort, dropped = _usable_effort(assignment.effort, model)
    if dropped:
        note = (
            f"the {role} role pins {dropped!r} reasoning effort, which model {model!r} does "
            "not accept; the dispatch runs at the model's own default effort"
        )
        report = f"{report} {note}" if report else note
    return ResolvedRole(
        role=role,
        agent=agent,
        model=model,
        effort=effort,
        source=RoleSource.COST_PROFILE,
        profile=selected.name,
        declared_at=assignment.declared_at,
        fallback=fallback,
        report=report,
        dropped_effort=dropped,
    )


def _partial_assignment(
    role: str,
    selected: CostProfile,
    assignment: RoleAssignment,
    session_default: SessionDefault,
) -> ResolvedRole:
    """An assignment that named the role but no model.

    Whatever else it declared is kept — an assigned agent is still the operator's
    decision — and the missing model is reported rather than inferred, because a
    profile that names a role and forgets its model looks configured from every
    surface that lists profiles.
    """
    return _from_assignment(
        role,
        selected,
        assignment,
        agent=assignment.agent or session_default.agent,
        model=session_default.model,
        fallback=FallbackReason.MODEL_UNASSIGNED,
        report=(
            f"cost profile {selected.name!r} assigns the {role} role no model, so it runs on "
            f"the session default model {_shown(session_default.model)}"
        ),
    )


def _session_fallback(
    role: str,
    session_default: SessionDefault,
    reason: FallbackReason,
    profile_name: str,
) -> ResolvedRole:
    """Fall back to the session's own agent and model, and say why."""
    tail = (
        f"runs on the session default agent {_shown(session_default.agent)} and model "
        f"{_shown(session_default.model)}"
    )
    if reason is FallbackReason.NO_PROFILE_SELECTED:
        report = f"no cost profile is selected, so the {role} role {tail}"
    elif reason is FallbackReason.PROFILE_NOT_DEFINED:
        report = (
            f"the selected cost profile {profile_name!r} is not defined, so the {role} role "
            f"{tail}"
        )
    else:
        report = f"cost profile {profile_name!r} assigns no {role} role, so it {tail}"
    return ResolvedRole(
        role=role,
        agent=session_default.agent,
        model=session_default.model,
        effort="",
        source=RoleSource.SESSION_DEFAULT,
        profile=profile_name if reason is FallbackReason.ROLE_UNASSIGNED else "",
        fallback=reason,
        report=report,
    )


def _usable_effort(effort: str, model: str) -> tuple[str, str]:
    """Return ``(effort to send, effort dropped)`` for *model*.

    An unpinned model resolves at the provider, so its capability is unknown here
    and a pinned effort passes through untouched. A concrete model that declares no
    effort support would reject the turn outright, so its effort is dropped.
    """
    if not effort or not model:
        return effort, ""
    if model_supports_effort(model):
        return effort, ""
    return "", effort


def _shown(value: str) -> str:
    """Render an agent or model for a report, naming the inherit case plainly."""
    return repr(value) if value else "(inherited from the provider)"
