"""The integration floor: who may write to a protected destination, and when.

Integration is the one delivery action a mistake cannot undo. A branch can be
deleted, a deployment can be rolled back, a review artifact can be closed — a
change merged into the destination other people build on is already in their
history. Everything in this module exists because of that asymmetry.

**Two independent gates, both required.** The autonomy ladder says how far this
run may go unattended; ``delivery.auto_integrate`` says whether this project's
destination accepts unattended writes at all. They answer different questions and
are set by different people at different times — a ladder rung is per source, per
spec type, and per submitter class, while the posture switch is a property of the
destination — so neither substitutes for the other. Requiring both means a policy
grid that grew an ``integration`` cell by accident still cannot merge, and a
project that armed auto-integration still cannot merge work the policy only
authorized as far as delivery.

**Absence reserves the action for a human.** Neither gate defaults open:
unconfigured autonomy resolves to authoring, and the posture switch ships off. So
an install that configures nothing never integrates, and integration "requires
explicit human action" as the behaviour of an absent setting rather than as a
sentence in a document.

**A protected set that is never empty.** With no protected branches configured,
the project's base branch is protected. An empty set would read as "nothing is
protected", which is precisely backwards: a project that has not thought about
which branches are protected is a project whose main line must be treated as one.
Publish stages remain free to push to branches outside the set — a development
branch feeding a test pipeline is a normal publish target, and treating every
push as an integration would make the safe case impossible to configure.

The engine cannot inspect what a configured command does, so this module does not
pretend to intercept a merge. It answers the authorization question with a record
of which gates held, and the caller that would integrate asks first. That keeps
the decision auditable and reconstructable: a refusal names the gate that was
shut rather than reporting a generic denial.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from ..autonomy import AutonomyDecision, AutonomyLevel
from ..config import AUTO_INTEGRATE_SETTING, ConfigStore, ValueOrigin
from ..config.schema import SECTION_PROJECTS
from .workflow import DeliveryWorkflow, cap_autonomy

#: Project field holding the protected integration branches.
PROTECTED_BRANCHES_FIELD = "protected_branches"

#: Reason codes for a refused integration. Stable strings rather than prose: a
#: refusal, a queue entry, and an audit record all quote the same one.
REASON_LADDER = "autonomy_ladder"
REASON_POSTURE = "auto_integrate_off"
REASON_VERIFY = "verify_incomplete"
REASON_NO_TARGET = "no_integration_target"
REASON_DELIVERY_FAILED = "delivery_failed"


@dataclass(frozen=True)
class ProtectedBranches:
    """The branches that count as integration targets for one project."""

    branches: frozenset[str]
    origin: ValueOrigin
    #: Dotted path of the declaration, empty when derived from the base branch.
    declared_at: str = ""

    @property
    def from_base_branch(self) -> bool:
        """Whether this set was derived from the base branch rather than configured."""
        return not self.declared_at

    def protects(self, branch: str) -> bool:
        """Whether writing *branch* is an integration rather than a publish.

        A blank branch is treated as protected. The caller could not name where
        it is writing, and "unknown destination" must not be the one case that
        walks past the gate.
        """
        if not branch or not branch.strip():
            return True
        return branch.strip() in self.branches


def resolve_protected_branches(
    document: Mapping[str, Any],
    *,
    project: str | None,
    base_branch: str = "",
) -> ProtectedBranches:
    """Resolve the protected set for *project*, falling back to the base branch.

    *base_branch* is the run's resolved base rather than a second read of
    configuration: a run dispatched from a watch source takes its base from that
    source, so the branch this run would integrate into is a property of the run
    and is passed in.
    """
    entry = _project_entry(document, project)
    configured = entry.get(PROTECTED_BRANCHES_FIELD)
    named = _clean_names(configured)
    if named:
        return ProtectedBranches(
            branches=frozenset(named),
            origin=ValueOrigin.PROJECT_CONFIG,
            declared_at=f"{SECTION_PROJECTS}.{project}.{PROTECTED_BRANCHES_FIELD}",
        )
    fallback = base_branch.strip() or _project_base_branch(entry)
    return ProtectedBranches(
        branches=frozenset({fallback}) if fallback else frozenset(),
        origin=ValueOrigin.BUNDLED_DEFAULT,
    )


@dataclass(frozen=True)
class IntegrationDecision:
    """Whether this run may integrate unattended, and which gates held.

    Both gate results are carried rather than only the verdict. "Refused" on its
    own sends an operator looking through two unrelated configuration objects;
    naming the gate that was shut is the difference between a report and a hint.
    """

    permitted: bool
    ladder_permits: bool
    auto_integrate: bool
    verified: bool
    target: str
    target_protected: bool
    #: Whether the delivery this decision belongs to actually succeeded.
    delivered: bool = True
    reasons: tuple[str, ...] = ()
    #: Where the posture switch was read from, for a configuration surface.
    auto_integrate_declared_at: str = ""

    @property
    def requires_human_action(self) -> bool:
        """Whether integration is reserved for an explicit human action."""
        return not self.permitted


def evaluate_integration(
    *,
    decision: AutonomyDecision,
    auto_integrate: bool,
    verified: bool,
    protected: ProtectedBranches,
    target: str,
    delivered: bool = True,
    auto_integrate_declared_at: str = "",
) -> IntegrationDecision:
    """Decide whether the pipeline may integrate into *target* without a human.

    Every gate is evaluated rather than short-circuited, so a run blocked by two
    things reports two things: an operator who flips the posture switch on a run
    the ladder also refused would otherwise be told the same "refused" twice.

    *delivered* is the outcome of the delivery this decision belongs to, and it
    is a gate rather than context. The configuration gates answer "may this run
    integrate"; they cannot answer "did this run produce something worth
    integrating". A publish that deployed half a change and exited non-zero
    satisfies every configured gate, so without this a failed delivery would
    carry a permitted decision and a caller reading only ``permitted`` would
    integrate it.
    """
    ladder = decision.permits(AutonomyLevel.INTEGRATION)
    reasons: list[str] = []
    if not ladder:
        reasons.append(REASON_LADDER)
    if not auto_integrate:
        reasons.append(REASON_POSTURE)
    if not verified:
        reasons.append(REASON_VERIFY)
    if not target.strip():
        reasons.append(REASON_NO_TARGET)
    if not delivered:
        reasons.append(REASON_DELIVERY_FAILED)
    return IntegrationDecision(
        permitted=not reasons,
        ladder_permits=ladder,
        auto_integrate=auto_integrate,
        verified=verified,
        target=target,
        target_protected=protected.protects(target),
        delivered=delivered,
        reasons=tuple(reasons),
        auto_integrate_declared_at=auto_integrate_declared_at,
    )


@dataclass(frozen=True)
class DeliveryAuthority:
    """How far one run may proceed, after the workflow's own ceiling applies.

    The autonomy policy resolves a level from what an operator configured; this
    lowers it to what the project's configuration can actually carry out. A
    project with no delivery workflow has described no way to isolate a
    workspace, raise a review, or verify a change, so a policy naming delivery or
    integration for it is authority over machinery that does not exist.
    """

    decision: AutonomyDecision
    level: AutonomyLevel
    workflow_configured: bool
    auto_integrate: bool
    auto_integrate_declared_at: str
    protected: ProtectedBranches

    @property
    def capped(self) -> bool:
        """Whether the workflow ceiling lowered the resolved level."""
        return self.level is not self.decision.level

    def permits(self, level: AutonomyLevel) -> bool:
        """Whether the capped level authorizes work requiring *level*."""
        return self.level.permits(level)

    @property
    def isolates_before_execution(self) -> bool:
        """Whether this run gets its own workspace before task execution begins.

        Delivery authority is what makes isolation required: a run that will
        raise a review and push branches must not be doing that from the
        project's own working tree, where a concurrent run is also working.
        """
        return self.permits(AutonomyLevel.DELIVERY)

    def integration(
        self, *, verified: bool, target: str, delivered: bool = True
    ) -> IntegrationDecision:
        """Evaluate the integration gates for this run against *target*."""
        return evaluate_integration(
            decision=_capped_decision(self.decision, self.level),
            auto_integrate=self.auto_integrate,
            verified=verified,
            protected=self.protected,
            target=target,
            delivered=delivered,
            auto_integrate_declared_at=self.auto_integrate_declared_at,
        )


def resolve_authority(
    store: ConfigStore,
    *,
    decision: AutonomyDecision,
    project: str | None = None,
    workflow: DeliveryWorkflow | None = None,
    base_branch: str = "",
) -> DeliveryAuthority:
    """Resolve the delivery authority for one run from configuration.

    Reads the workflow once and holds it: a configuration saved mid-run would
    otherwise let a run isolate under one answer and integrate under another.

    **Raises ``ConfigValidationError``** when the workflow cannot be resolved --
    an unresolvable ``workflow.preset`` name is the case, refused by name rather
    than ignored, because a silent ignore turned a typo into the
    zero-configuration workflow and capped a project's autonomy without saying
    so. Nothing here catches it, and the callers today are tests and the
    prerequisite gate's own reader, which converts it.

    The first *production* caller must convert it the way
    :func:`~..prerequisites.gate_run` does -- into an audited
    :class:`~..prerequisites.RunRefusal` naming the configuration path -- rather
    than letting a configuration error escape from a delivery decision. A run
    stopped by an unreadable document is the strongest case for a recorded
    refusal, and an exception unwinding past the audit log loses the one thing
    the operator can act on.
    """
    resolved = workflow if workflow is not None else DeliveryWorkflow.load(store, project=project)
    document = store.document()
    posture = store.effective(AUTO_INTEGRATE_SETTING, project=project)
    return DeliveryAuthority(
        decision=decision,
        level=AutonomyLevel(
            cap_autonomy(decision.level.value, workflow_configured=resolved.configured)
        ),
        workflow_configured=resolved.configured,
        auto_integrate=bool(posture.value),
        auto_integrate_declared_at=posture.declared_at,
        protected=resolve_protected_branches(document, project=project, base_branch=base_branch),
    )


def _capped_decision(decision: AutonomyDecision, level: AutonomyLevel) -> AutonomyDecision:
    """The resolved decision restated at the capped level.

    The ladder gate is evaluated against the *capped* level, so a zero-config
    project cannot reach integration by way of a policy grid that names it.
    """
    if level is decision.level:
        return decision
    return AutonomyDecision(
        level=level,
        source=decision.source,
        spec_type=decision.spec_type,
        submitter_class=decision.submitter_class,
        declared_at=decision.declared_at,
    )


def _project_entry(document: Mapping[str, Any], project: str | None) -> Mapping[str, Any]:
    if project is None:
        return {}
    projects = document.get(SECTION_PROJECTS)
    if not isinstance(projects, Mapping):
        return {}
    entry = projects.get(project)
    return entry if isinstance(entry, Mapping) else {}


def _project_base_branch(entry: Mapping[str, Any]) -> str:
    raw = entry.get("base_branch")
    return raw.strip() if isinstance(raw, str) else ""


def _clean_names(raw: Any) -> tuple[str, ...]:
    if not isinstance(raw, (list, tuple)):
        return ()
    seen: dict[str, None] = {}
    for item in raw:
        if isinstance(item, str) and item.strip():
            seen.setdefault(item.strip(), None)
    return tuple(seen)
