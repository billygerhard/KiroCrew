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

from .analysis import AnalysisEngine, FindingsSink, RecordingFindingsSink, StateFindingsSink
from .audit import AuditLog
from .autonomy import AutonomyLevel
from .budget.ledger import RunAccounting, RunCostSink
from .capabilities.builtins import (
    AUTHORING_PROVIDER,
    IMPLEMENTATION_PROVIDER,
    MODEL_CATALOG_PROVIDER,
    REVIEW_PROVIDER,
    ModelResolver,
    register_builtins,
)
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
from .phases import ExecutionOutcome
from .prerequisites import BranchResolver, Budget, ProgramResolver, RunRefusal, gate_run
from .resume import ResumeAuthority, authority_for, request_execution_for_run
from .review_queue import ReviewQueue
from .roles import SessionDefault
from .runs import RunMachine
from .seeder import SessionOpener, SessionSeeder
from .state import SpecLock, SpecRef, StateStore

#: The provider each deeper builtin must resolve to once
#: :func:`~.capabilities.builtins.register_builtins` has run. Checked on the
#: constructed graph rather than trusted, because the shipped default answers
#: the same call with a deterministic no-coverage provider — a graph missing the
#: registration is complete in shape and mislabels three paths that spend
#: credits as paths that spend nothing.
REQUIRED_BUILTINS: dict[str, str] = {
    "authoring": AUTHORING_PROVIDER,
    "review": REVIEW_PROVIDER,
    "implementation": IMPLEMENTATION_PROVIDER,
    "model_catalog": MODEL_CATALOG_PROVIDER,
}


class IncompleteEngineGraph(ValueError):
    """A graph that is missing wiring :func:`build_engine` would have performed.

    Raised from :meth:`EngineGraph.__post_init__`, which is the only reason the
    module's "by construction rather than by convention" claim is true of the
    code and not only of the factory: a frozen dataclass is constructable
    directly and, more likely, reachable through :func:`dataclasses.replace`,
    which an ordinary refactor reaches for and which re-runs ``__post_init__``.
    """


#: Sentinel for a collaborator that names no project scope at all, so an absent
#: attribute is not mistaken for the unscoped ``None`` a coherent graph may hold.
_UNKNOWN_SCOPE = object()


class DurableFindings:
    """Marker asking :func:`build_engine` for the durable sink over its OWN store.

    Not a sink and never called: it carries no ``record``, so a graph cannot end
    up holding one of these by accident. It exists because
    :class:`~.analysis.StateFindingsSink` needs the :class:`~.state.StateStore`
    that :func:`build_engine` is about to build, and a caller constructing the
    sink first would have to open a *second* store over the same database — two
    connections, and two ideas of where a run's rows live. Passing this instead
    means the sink is built from the one store the graph uses.

    It is also the shape of the answer to a subtler risk. The only non-test
    ``build_engine`` caller today is the engine-MCP surface, which passes a sink
    that *refuses* to record — correct there, because none of its operations
    touches a run. Whoever built the first run-driving graph by copying that
    spelling would get a graph that satisfies every wiring check and records
    nothing at all. :func:`build_run_engine` takes no sink argument, so the run
    path has nothing to copy.
    """


#: The one value that means "durable, over this graph's store". A module-level
#: singleton rather than a fresh instance per call so the marker compares
#: identically wherever it is passed.
DURABLE_FINDINGS = DurableFindings()


class RunPrevented(RuntimeError):
    """A run was refused before it started, and will not be started.

    Raised rather than returned by :meth:`EngineGraph.begin_run`, because that
    method's return value is the object that *starts* runs: a caller free to
    ignore a returned refusal would hold a starter it was just told not to use.
    The refusal it carries is the audited one, so a surface can render every
    unmet prerequisite without re-deriving them.
    """

    def __init__(self, refusal: RunRefusal) -> None:
        super().__init__(refusal.describe())
        self.refusal = refusal


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
    #: The headless run driver, deliberately private. It is a ``RunStarter``, so a
    #: surface holding it could pass it straight to a dispatch entry point as
    #: ``start=`` and open sessions for a run whose prerequisites are unmet.
    #: :meth:`begin_run` is the only way to obtain it, and it gates first.
    _seeder: SessionSeeder

    def __post_init__(self) -> None:
        """Refuse a partial graph, whatever spelling produced it.

        :func:`build_engine` performs registrations and passes shared
        collaborators that nothing in the field types requires, so a graph built
        by any other route — a direct call, or
        ``dataclasses.replace(graph, registry=...)`` — type-checks while
        resolving three credit-spending capabilities to the deterministic
        no-coverage default, attributing spend to memory, and running a second
        state writer over the same store. Each check below is one of those.
        """
        problems: list[str] = []

        for capability, provider_name in REQUIRED_BUILTINS.items():
            try:
                resolved = self.registry.builtin(capability).identity.name
            except Exception as exc:  # noqa: BLE001 - a registry that cannot answer is partial
                problems.append(f"{capability!r} has no resolvable builtin: {exc}")
                continue
            if resolved != provider_name:
                problems.append(
                    f"{capability!r} resolves to {resolved!r}, not the engine's "
                    f"{provider_name!r}: register_builtins did not run over this registry"
                )

        if not isinstance(self.registry.cost_sink, RunCostSink):
            problems.append(
                "the capability registry attributes cost to "
                f"{type(self.registry.cost_sink).__name__}, not to a durable run row"
            )
        if getattr(self.registry, "_audit", None) is not self.audit:
            problems.append(
                "the capability registry does not record through this graph's audit log"
            )
        if isinstance(self.analysis.findings_sink, RecordingFindingsSink):
            problems.append("the analysis engine records findings in memory, not to a durable sink")
        if getattr(self.analysis, "_registry", None) is not self.registry:
            problems.append("the analysis engine runs over a different capability registry")
        if self.machine.store is not self.state:
            problems.append("the run machine writes to a different state store than the graph's")
        if getattr(self.review_queue, "_machine", None) is not self.machine:
            problems.append(
                "the review queue transitions runs through a second machine over this store, "
                "which would be a second writer of the run's state column"
            )
        if getattr(self._seeder, "_audit", None) is not self.audit:
            problems.append(
                "the session seeder records the posture it applied to a seeded session in "
                "a different audit log than the run writes to"
            )
        if getattr(self._seeder, "_config", None) is not self.config:
            problems.append(
                "the session seeder resolves its notification channel from a different "
                "configuration document than the graph's"
            )
        announcer = getattr(self.machine, "_review_announcer", None)
        if getattr(announcer, "__self__", None) is not self._seeder:
            problems.append(
                "the run machine announces a run parked for review through something "
                "other than this graph's seeder, so a parked run may tell nobody"
            )

        # Project coherence. The graph's own ``project`` is what
        # prerequisite_refusal and notify_awaiting_review pass down, while the
        # machine, registry and notifier each baked one in when they were built.
        # ``replace(graph, project=...)`` rebuilds none of them, so a graph can
        # gate under one project and audit, meter and notify under another —
        # settings are resolved per project, so that is two different effective
        # configurations for one run rather than a cosmetic mismatch.
        for label, collaborator in (
            ("run machine", self.machine),
            ("capability registry", self.registry),
            ("host notifier", self.notifier),
        ):
            scope = getattr(collaborator, "project", _UNKNOWN_SCOPE)
            if scope is _UNKNOWN_SCOPE:
                scope = getattr(collaborator, "_project", _UNKNOWN_SCOPE)
            if scope is not _UNKNOWN_SCOPE and scope != self.project:
                problems.append(
                    f"the {label} resolves settings under project {scope!r}, not this "
                    f"graph's {self.project!r}"
                )

        if problems:
            raise IncompleteEngineGraph(
                "this engine graph is missing wiring build_engine performs: " + "; ".join(problems)
            )

    def prerequisite_refusal(
        self,
        ref: SpecRef,
        level: AutonomyLevel,
        *,
        base_branch: str = "",
        run: str | None = None,
        which: ProgramResolver | None = None,
        branch_exists: BranchResolver | None = None,
        budget: Budget | None = None,
    ) -> RunRefusal | None:
        """Evaluate the run gate for *ref* at *level*, recording any refusal.

        Returns the refusal rather than raising, for a surface that *reports*
        readiness (Doctor, a settings panel) instead of starting work. Nothing
        here hands back a starter, so this is not a second route past
        :meth:`begin_run`'s gate — it is the same gate, asked without acting.
        """
        return gate_run(
            self.config,
            level,
            self.audit,
            ref,
            project=self.project,
            base_branch=base_branch,
            run=run,
            which=which,
            branch_exists=branch_exists,
            budget=budget,
        )

    def begin_run(
        self,
        ref: SpecRef,
        level: AutonomyLevel,
        *,
        base_branch: str = "",
        run: str | None = None,
        which: ProgramResolver | None = None,
        branch_exists: BranchResolver | None = None,
        budget: Budget | None = None,
    ) -> SessionSeeder:
        """Gate, then hand back the starter that opens sessions for runs.

        The one door onto starting work, and the reason the prerequisite gate runs
        *before the first credit*. A dispatch entry point cannot dispatch without
        a ``RunStarter`` — ``start=`` is required at every one of them — and the
        only way to obtain the engine's is through this method, so the gate
        precedes the claim, the spec, the intake screening, and the session.
        Discovering a missing delivery program at the delivery phase instead would
        mean an authored spec, a screened item, and real spend already gone.

        *level* is the highest rung a run started with this starter may reach: the
        gate refuses on any unmet prerequisite of any phase that rung permits, so
        an entry point that will only author passes ``AUTHORING`` and is not
        refused for a delivery program it will never invoke.

        Raises :class:`RunPrevented` when the gate refuses, which is already
        audited by then.
        """
        refusal = self.prerequisite_refusal(
            ref,
            level,
            base_branch=base_branch,
            run=run,
            which=which,
            branch_exists=branch_exists,
            budget=budget,
        )
        if refusal is not None:
            raise RunPrevented(refusal)
        return self._seeder

    def resume_authority(self, run_id: str) -> ResumeAuthority:
        """The authority *run_id* may act under, read back from its own row.

        Exposed for a surface that has to *explain* a held run — why a queue entry
        is waiting, which rung it is at, whether intake screening is what is
        holding it — without being able to change the answer. The reconstruction
        is :func:`~.resume.authority_for`, which is also what the gate below uses,
        so an explanation and a decision cannot come apart.
        """
        return authority_for(self.machine.get(run_id))

    def request_execution(
        self,
        ref: SpecRef,
        run_id: str,
        *,
        user: str | None = None,
        spec_type: str | None = None,
        lock: SpecLock | None = None,
    ) -> ExecutionOutcome:
        """The execution gate for a run this graph already has a row for.

        Takes no autonomy level and no decision. A run's authority was settled
        when it was admitted and persisted on its row, so this reads it back
        through :func:`~.resume.request_execution_for_run` rather than resolving
        the policy again — a fresh resolution would hand a quarantined run its
        configured rung back, and would let a widened configuration retroactively
        raise a run already in flight.

        Contrast :meth:`begin_run`, which does take a level: there the level is
        the *ceiling a caller is asking for* before any row exists, and the
        prerequisite gate is what refuses it. Once the row exists there is nothing
        left to ask.
        """
        return request_execution_for_run(
            self.state,
            ref,
            run=run_id,
            audit=self.audit,
            user=user,
            spec_type=spec_type,
            lock=lock,
        )

    def notify_awaiting_review(self, ref: SpecRef, run_id: str, *, gate: str = "") -> object:
        """Announce a run parked at a human-reserved gate, through the seeder.

        Exposed so the run lifecycle can announce a parked run without being
        handed the starter: this reaches the notification half of the seeder only,
        and cannot open a session.
        """
        return self._seeder.notify_awaiting_review(ref, run_id, project=self.project, gate=gate)

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
    findings_sink: FindingsSink | DurableFindings,
    host_state: Any,
    session_opener: SessionOpener,
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
      one place to be passed and no silent fallback to memory. A graph that
      drives runs passes :data:`DURABLE_FINDINGS` — or, better, is built by
      :func:`build_run_engine`, which passes it for the caller.
    * *host_state* is the gateway state the notifier resolves its bus from.
      ``None`` is a legitimate value — a process with no bus cannot deliver, and
      the notifier reports that through :attr:`HostNotifier.available` — but it
      has to be *said*, not arrived at by forgetting a keyword.
    * *session_opener* creates the host session a headless run works in. Required
      for the same reason: a default could only be a no-op opener, and a run whose
      session never opened is one that reports as started and does nothing. Only a
      real host session appears in the dashboard session list, so this seam is the
      host's to satisfy.

    The root arguments are different in kind: their default is the app's real
    data home, so omitting one selects production rather than skipping a wiring.
    Tests point them at a temporary directory.
    """
    if model_resolver is None:  # pragma: no cover - typed away, guarded anyway
        raise ValueError("build_engine needs a model resolver to register the builtins with")
    if findings_sink is None:  # pragma: no cover - typed away, guarded anyway
        raise ValueError("build_engine needs a findings sink; a default would only be memory")
    if session_opener is None:  # pragma: no cover - typed away, guarded anyway
        raise ValueError("build_engine needs a session opener; a default would open nothing")

    state = StateStore(root=state_root)
    config = ConfigStore(root=Path(config_root) if config_root is not None else None)
    audit = AuditLog(root=audit_root)

    cost_sink = RunCostSink(state)
    registry = CapabilityRegistry(
        config,
        project=project,
        audit=audit,
        # Durable per-run attribution rather than the in-memory recorder the
        # registry falls back to: a delegated provider's spend happened in
        # another process, so nothing else in the engine would ever record it.
        cost_sink=cost_sink,
    )
    # The registration that makes a running engine's builtins real. Without this
    # call the graph is complete in shape and empty in effect.
    register_builtins(registry, model_resolver=model_resolver)

    analysis = AnalysisEngine(
        registry,
        # The marker is resolved here and only here, because this is the first
        # moment the store exists: a caller resolving it earlier would have had to
        # open a second store over the same database.
        findings_sink=(
            StateFindingsSink(state)
            if isinstance(findings_sink, DurableFindings)
            else findings_sink
        ),
    )
    notifier = HostNotifier(config, project=project, state=host_state)
    seeder = SessionSeeder(
        config,
        opener=session_opener,
        # The same durable cost sink the registry attributes through, so a seeded
        # session's metering and a delegated provider's spend land on one run row
        # rather than in two ideas of what a run cost.
        accounting=RunAccounting(state, cost_sink=cost_sink),
        audit=audit,
        state=host_state,
    )
    machine = RunMachine(
        state,
        config,
        project=project,
        audit=audit,
        notifier=notifier,
        # Built before the machine so this can be passed rather than set
        # afterwards: the announcement is a property of the transition, and a
        # machine that could be handed one later would have a window in which a
        # run parks on a person and tells nobody.
        review_announcer=seeder.notify_awaiting_review,
    )
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
        _seeder=seeder,
    )


def build_run_engine(
    *,
    model_resolver: ModelResolver,
    host_state: Any,
    session_opener: SessionOpener,
    project: str | None = None,
    state_root: str | Path | None = None,
    audit_root: str | Path | None = None,
    config_root: str | Path | None = None,
) -> EngineGraph:
    """Assemble the engine for a surface that DRIVES RUNS. No sink to choose.

    The same graph :func:`build_engine` builds, with one seam settled rather than
    offered: analysis findings land in the durable store, keyed to the run they
    were found for. That is not a convenience. A run's findings are read back by a
    reviewer — the Review_Queue projects them onto the run's entry — so a run-path
    graph holding an in-memory sink loses the analyzer's verdict at process exit,
    and one holding the MCP surface's refusing sink loses it louder and no less
    completely.

    There is deliberately no ``findings_sink`` parameter here. The alternatives a
    caller could otherwise pass are all wrong on this path and two of them look
    right: :class:`~.analysis.RecordingFindingsSink` is what ``AnalysisEngine``
    itself defaults to, and the engine-MCP surface's refusing sink is the spelling
    a reader is most likely to copy, because it is the only non-test
    ``build_engine`` call in the tree. Removing the parameter removes the choice.

    Everything else — the required host seams, the roots defaulting to the app's
    real data home — is :func:`build_engine`'s, unchanged.
    """
    return build_engine(
        model_resolver=model_resolver,
        findings_sink=DURABLE_FINDINGS,
        host_state=host_state,
        session_opener=session_opener,
        project=project,
        state_root=state_root,
        audit_root=audit_root,
        config_root=config_root,
    )
