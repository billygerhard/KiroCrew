"""The one engine operation every Doctor surface renders.

An MCP tool and a UI panel are two renderings of one result, and this is the
function that produces it. Both surfaces call :func:`diagnose`; neither assembles
a :class:`~.doctor.Doctor` itself. That is the whole point: a surface that built
its own aggregation would choose its own collaborators, and two panels
disagreeing about whether a host is ready is worse than one panel nobody trusts.

Three things this module is responsible for, each because leaving it to the
surfaces produced a defect:

**It populates what the doctor cannot read for itself.** The declared program
minimums come from configuration through
:func:`~.prerequisites.declared_minimum_versions`. An unpopulated minimum mapping
does not fail -- it iterates nothing, reports nothing, and reads exactly like a
host that satisfies every minimum, which is the worst available outcome for a
check whose job is to catch a downgrade.

**It requires the registration state.** ``registration`` is a required keyword,
not a default. The app assesses whether its skill and MCP server reached
Host_Agent sessions; the engine must not import the app root to find that out, so
it is passed in. Making it required is what stops a new surface from silently
dropping the one check that distinguishes a half-registered app from a whole one.

**It gives the refusal translators their callers.** A run refused before the
first credit and a dispatch blocked by the ceiling are reported here through
:func:`~.doctor.refusal_finding_ids` and :func:`~.doctor.dispatch_finding_id`, so
"run refused" and "doctor says" quote one identifier rather than two spellings of
one condition.

Read-only throughout, and free: nothing here dispatches a model turn, and the one
subprocess in the whole path is the doctor's own ``--version`` probe.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from .autonomy import AutonomyLevel
from .budget.ceiling import DispatchDecision
from .budget.switch import KillSwitch
from .capabilities.contracts import Degradation
from .config import ConfigStore
from .config.agent_surface import AgentSurfaceLookup
from .doctor import (
    BranchResolver,
    Doctor,
    DoctorHistory,
    DoctorReport,
    ProgramResolver,
    QueueProjection,
    RegistrationState,
    VersionReader,
    dispatch_finding_id,
    refusal_finding_ids,
)
from .prerequisites import RunRefusal, check_project, declared_minimum_versions
from .watch.poll import PollOutcome

__all__ = [
    "diagnose",
    "dispatch_block_report",
    "refusal_report",
    "run_gate_report",
]


def diagnose(
    config: ConfigStore,
    *,
    registration: RegistrationState,
    project: str | None = None,
    base_branch: str = "",
    which: ProgramResolver | None = None,
    branch_exists: BranchResolver | None = None,
    kill_switch: KillSwitch | None = None,
    queue: QueueProjection | None = None,
    degradations: Sequence[Degradation] = (),
    poll_outcomes: Sequence[PollOutcome] = (),
    agents: AgentSurfaceLookup | None = None,
    version_of: VersionReader | None = None,
    history: DoctorHistory | None = None,
) -> DoctorReport:
    """Run every Doctor check against *config* and return the one report.

    *registration* is required rather than defaulted, because the alternative is a
    surface that omits it and shows an app as healthy without having asked whether
    its tools ever arrived.

    Collaborators stay optional and are read from the real environment when a
    caller supplies none, so the diagnostic is callable on the broken host it
    exists to diagnose.
    """
    return Doctor(
        config=config,
        project=project,
        base_branch=base_branch,
        which=which,
        branch_exists=branch_exists,
        kill_switch=kill_switch,
        queue=queue,
        degradations=tuple(degradations),
        poll_outcomes=tuple(poll_outcomes),
        agents=agents,
        registration=registration,
        minimum_versions=declared_minimum_versions(config),
        version_of=version_of,
        history=history,
    ).run()


def refusal_report(refusal: RunRefusal) -> dict[str, Any]:
    """A refused run's reported reason, carrying the Doctor's own identifiers.

    The refusal already knows its unmet prerequisites; what a surface needs beside
    them is the identifier a Doctor panel shows for the same condition, so an
    operator reading "refused" and an operator reading the panel are reading one
    sentence. Derived from the refusal rather than restated, so there is no second
    list to fall out of step with the gate.
    """
    detail = refusal.detail()
    detail["finding_ids"] = list(refusal_finding_ids(refusal))
    return detail


def run_gate_report(
    config: ConfigStore,
    level: AutonomyLevel,
    *,
    project: str | None = None,
    base_branch: str = "",
    which: ProgramResolver | None = None,
    branch_exists: BranchResolver | None = None,
) -> dict[str, Any]:
    """Answer "may a run at *level* start", read-only and before any credit.

    The same evaluation :func:`~.prerequisites.gate_run` refuses on, without the
    audit write, so an agent or a panel can ask the question without starting a
    run. Every phase the level reaches is included, later-executing phases too: a
    delivery prerequisite discovered halfway through a run has already cost the
    authoring credits.
    """
    report = check_project(
        config,
        project=project,
        base_branch=base_branch,
        which=which,
        branch_exists=branch_exists,
    )
    unmet = report.unmet_through(level)
    if not unmet:
        return {"may_start": True, "autonomy": level.value, "finding_ids": [], "unmet": []}
    payload = refusal_report(RunRefusal(level=level, unmet=unmet))
    payload["may_start"] = False
    return payload


def dispatch_block_report(decision: DispatchDecision) -> dict[str, Any]:
    """A blocked dispatch's reported reason, carrying the Doctor's identifier.

    Empty identifier when the dispatch was allowed: nothing is wrong, and a caller
    must not be able to quote "nothing" as a reason.
    """
    return {
        "allowed": decision.allowed,
        "outcome": decision.outcome.value,
        "reason": decision.message,
        "finding_id": dispatch_finding_id(decision.outcome),
    }
