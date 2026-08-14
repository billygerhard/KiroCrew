"""The engine's one construction point.

Every collaborator the engine enforces something through is inert until
something builds it, and a library that nothing constructs passes every test it
has. This module is where the object graph is assembled, once, so that a surface
cannot assemble a partial one: the builtin providers are registered, the cost of
a delegated capability is attributed to a durable row rather than to memory, the
capability audit reaches the same log the rest of the run writes to, and exactly
one :class:`~.runs.RunMachine` exists over the state store.

Two properties are load-bearing and both are enforced by construction rather
than by convention:

*One machine per store.* :meth:`RunMachine.transition` is the only production
writer of a run's ``state`` column, and several guarantees rest on that being
true. :func:`~.orchestrator.orchestrator_for` builds its own machine when none is
passed, so a surface reaching it directly would create a second writer over the
same store. :meth:`EngineGraph.orchestrator` passes the graph's machine every
time, and takes no ``machine`` parameter for a caller to override it with.

*No seam whose default means "skip".* :func:`build_engine` takes the seams the
engine library cannot build for itself as required keywords. A default for any
of them would be the same defect this module exists to remove, moved up one
level: a graph that looks complete, resolves no builtin, and records what it
found where nobody can read it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .analysis import AnalysisEngine, FindingsSink
from .audit import AuditLog
from .budget.ledger import RunCostSink
from .capabilities.builtins import ModelResolver, register_builtins
from .capabilities.registry import CapabilityRegistry
from .config import ConfigStore
from .delivery import DeliveryAuthority, WorkspaceJanitor
from .notify.routing import HostNotifier
from .orchestrator import (
    Reviewer,
    TaskWorker,
    WaveRunner,
    orchestrator_for,
    workspace_root,
)
from .review_queue import ReviewQueue
from .roles import SessionDefault
from .runs import RunMachine
from .state import SpecRef, StateStore


@dataclass(frozen=True)
class EngineGraph:
    """The constructed engine: every collaborator a surface needs, already wired.

    Frozen because the graph is the wiring, not state: a surface that could swap
    a field would be able to put back the partial graph :func:`build_engine`
    exists to make unconstructable. Obtained only from :func:`build_engine` —
    constructing this dataclass directly is possible in the language but skips
    every registration the factory performs, so surfaces call the factory.
    """

    #: Configured project key, or ``None`` for the unscoped defaults. Not a
    #: filesystem path: the settings layer keys projects by configured name.
    project: str | None
    state: StateStore
    config: ConfigStore
    audit: AuditLog
    registry: CapabilityRegistry
    analysis: AnalysisEngine
    #: Satisfies both notifier seams the engine has. Passed as the budget
    #: ceiling's ``notifier`` and the delivery pipeline's ``delivery_notifier``,
    #: which are separate parameters over one object rather than two objects.
    notifier: HostNotifier
    #: The only run-state writer in the process. Exposed because the surfaces
    #: that dispatch, drain, and resume runs need it; exposed *from here* so
    #: they share this one instead of each building another over the same store.
    machine: RunMachine
    review_queue: ReviewQueue

    def orchestrator(
        self,
        ref: SpecRef,
        run_id: str,
        *,
        authority: DeliveryAuthority,
        worker: TaskWorker,
        reviewer: Reviewer,
        session_default: SessionDefault = SessionDefault(),
        headless: bool = False,
    ) -> WaveRunner:
        """Build the wave runner for one run over this graph's collaborators.

        Reached through :func:`~.orchestrator.orchestrator_for` rather than by
        assembling a :class:`~.orchestrator.WaveRunner` here: that factory is the
        single construction point for the workspace broker, the role plan, the
        budget guard, and the janitor, so a caller building its own runner would
        silently drop all four.

        The per-run arguments are required and the shared ones are not offered:
        there is no ``machine`` parameter, because a second machine over this
        store would be a second writer of the run's state column, and no
        notifier parameter, because both notifier seams are already satisfied by
        the graph's one host notifier.
        """
        return orchestrator_for(
            ref,
            run_id,
            state=self.state,
            config=self.config,
            authority=authority,
            worker=worker,
            reviewer=reviewer,
            project=self.project,
            session_default=session_default,
            audit=self.audit,
            headless=headless,
            notifier=self.notifier,
            delivery_notifier=self.notifier,
            machine=self.machine,
        )


def build_engine(
    *,
    model_resolver: ModelResolver,
    findings_sink: FindingsSink,
    host_state: Any,
    project: str | None = None,
    state_root: str | Path | None = None,
    audit_root: str | Path | None = None,
    config_root: str | Path | None = None,
) -> EngineGraph:
    """Assemble the engine. The one place a surface gets a working object graph.

    The three required keywords are the seams the engine library cannot build
    for itself, and each is required rather than defaulted because a default
    could only mean *skip*:

    * *model_resolver* is the host's model catalog. It is what makes the
      model-backed builtins registerable, and the registration is what stops
      authoring, review, and implementation resolving to the shipped
      deterministic no-coverage default — which mislabels a path that spends
      credits as one that spends nothing.
    * *findings_sink* is where a routed analysis report is recorded. Defaulting
      it lands every report in :class:`~.analysis.RecordingFindingsSink`, whose
      rows die with the process; requiring it means the durable sink has exactly
      one place to be passed and no silent fallback to memory.
    * *host_state* is the gateway state the notifier resolves its bus from.
      ``None`` is a legitimate value — a process with no bus cannot deliver, and
      the notifier reports that through :attr:`HostNotifier.available` — but it
      has to be *said*, not arrived at by forgetting a keyword.

    The root arguments are different in kind: their default is the app's real
    data home, so omitting one selects production rather than skipping a wiring.
    Tests point them at a temporary directory.
    """
    if model_resolver is None:  # pragma: no cover - typed away, guarded anyway
        raise ValueError("build_engine needs a model resolver to register the builtins with")
    if findings_sink is None:  # pragma: no cover - typed away, guarded anyway
        raise ValueError("build_engine needs a findings sink; a default would only be memory")

    state = StateStore(root=state_root)
    config = ConfigStore(root=Path(config_root) if config_root is not None else None)
    audit = AuditLog(root=audit_root)

    registry = CapabilityRegistry(
        config,
        project=project,
        audit=audit,
        # Durable per-run attribution rather than the in-memory recorder the
        # registry falls back to: a delegated provider's spend happened in
        # another process, so nothing else in the engine would ever record it.
        cost_sink=RunCostSink(state),
    )
    # The registration that makes a running engine's builtins real. Without this
    # call the graph is complete in shape and empty in effect.
    register_builtins(registry, model_resolver=model_resolver)

    analysis = AnalysisEngine(registry, findings_sink=findings_sink)
    notifier = HostNotifier(config, project=project, state=host_state)
    machine = RunMachine(state, config, project=project, audit=audit, notifier=notifier)
    review_queue = ReviewQueue(
        machine,
        # Rooted where the orchestrator's own janitor is rooted, so archival
        # cleanup and run teardown do not hold two different ideas of where a
        # run's workspace lives.
        janitor=WorkspaceJanitor(state, root=workspace_root(state)),
    )

    return EngineGraph(
        project=project,
        state=state,
        config=config,
        audit=audit,
        registry=registry,
        analysis=analysis,
        notifier=notifier,
        machine=machine,
        review_queue=review_queue,
    )
