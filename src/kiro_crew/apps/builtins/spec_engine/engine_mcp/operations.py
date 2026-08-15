"""Thin adapters from tool calls onto the Spec_Engine library.

Each method here is one library call with its arguments unpacked from a tool's
`arguments` object and its result shaped into a JSON-serialisable dict. The
engine is the product; this is the adapter, so nothing here re-implements a rule
the library already holds.

The one thing this module owns outright is the configuration boundary. The
Autonomy_Policy and the Delivery_Workflow are configuration only — they hold the
argv the engine executes and the levels a run may reach unattended — so no tool
may write them. This module does not invent a second fence for that: the engine
already refuses config-only writes from any surface no operator confirmed, and
:data:`ENGINE_MCP_SURFACE` is exactly such a surface. Every configuration write
the adapter could make goes through the engine's single validated write path on
that surface, so the shared fence is the one that refuses, and adding a
config-only object to the fence protects this path for free.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence

from ..engine.analysis import AnalysisReport
from ..engine.composition import EngineGraph, build_engine
from ..engine.config import ConfigWriteSurface
from ..engine.cross_document import validate_spec as validate_spec_documents
from ..engine.phases import RunMode, advance, approve, derive_phase
from ..engine.seeder import OpenedSession, SessionRequest
from ..engine.state import SpecRef
from ..engine.structure import parse_tasks

#: The surface this adapter writes configuration through. It is deliberately not
#: operator-confirmed: a tool call arrives from an agent, not from a human
#: looking at a configuration panel, so the engine's write path refuses any
#: config-only path (the autonomy policy, the delivery workflow, capability
#: bindings, quality gates, intake guidance, and the auto-integrate switch) from
#: it. Flipping this to confirmed would hand an agent the authority the whole
#: config-only fence exists to withhold.
ENGINE_MCP_SURFACE = ConfigWriteSurface("engine-mcp", operator_confirmed=False)


def no_host_model_catalog() -> Sequence[str]:
    """The model identifiers this surface can see, which are none.

    The MCP server runs as its own process with no gateway handle, so it cannot
    ask the host what it advertises. Reporting an empty catalog is the honest
    answer and the one the model-catalog builtin is built for ("entitlement
    unknown"); inventing a model id here would be exactly the static list that
    capability exists to avoid. It is a *catalog* that is empty, not a skipped
    registration: the builtins are registered either way, so the three
    credit-spending capabilities are still labelled model-backed.
    """
    return ()


class RefusingFindingsSink:
    """Refuses to record: no analysis path is wired on the MCP surface.

    None of this adapter's operations invokes analysis, so nothing routes a
    report here. It raises rather than quietly holding rows in memory so that a
    future tool which does run analysis has to pass the durable sink instead of
    silently discovering that its findings went nowhere. Not a second spelling of
    :class:`~..engine.analysis.RecordingFindingsSink` — that one keeps rows for a
    caller to read, which would make a lost report look handled.
    """

    def record(self, ref: SpecRef, *, run: str, report: AnalysisReport) -> None:
        raise NotImplementedError(
            "the engine-MCP surface has no findings sink wired; a tool that runs "
            "analysis must build its graph with the durable sink"
        )


def _report_json(report: Any) -> dict[str, Any]:
    """Shape a ValidationReport into a JSON-serialisable dict."""
    return {
        "ok": bool(report.ok),
        "violations": [
            {
                "file": v.file,
                "line": v.location.line,
                "column": v.location.column,
                "rule": v.rule,
                "severity": v.severity.value,
                "message": v.message,
            }
            for v in report.violations
        ],
    }


class RefusingSessionOpener:
    """Refuses to open a session: this process cannot create host sessions.

    The MCP server runs outside the gateway, and only the host's session manager
    can create a session that appears in the dashboard session list. None of this
    adapter's tools starts a headless run, so nothing calls this; it raises rather
    than returning a stub handle so that a tool which one day does start a run
    cannot get a session that exists nowhere, is attributed to nothing, and spends
    under no posture the operator granted.
    """

    def __call__(self, request: SessionRequest) -> OpenedSession:
        raise NotImplementedError(
            "the engine-MCP surface cannot open host sessions; a run-starting tool "
            "must build its graph with the gateway's session opener"
        )


class EngineOperations:
    """Adapter binding tool calls to engine library calls.

    Built over the engine's one composition root rather than over collaborators
    of its own. Today's six operations touch documents, phases, approvals and
    configuration — no run row and no capability — so a private store and audit
    log would still *work*; they would also be a second, partial object graph,
    and the first run-touching or capability-touching tool added here would
    inherit an unregistered registry, a cost sink in memory, and a second writer
    of the run's state column. Taking the graph now means that tool has nothing
    left to get wrong.

    Roots are injectable so a test can point the adapter at a temporary state
    directory; in production every root resolves to the app's own data home. A
    caller that already holds a graph passes it instead, so a surface inside the
    gateway (with a real model catalog and a durable findings sink) does not get
    this process's deliberately empty ones.
    """

    def __init__(
        self,
        *,
        graph: EngineGraph | None = None,
        state_root: str | Path | None = None,
        audit_root: str | Path | None = None,
        config_root: str | Path | None = None,
    ) -> None:
        self._graph = (
            graph if graph is not None else self._build(state_root, audit_root, config_root)
        )
        self._store = self._graph.state
        self._audit = self._graph.audit
        self._config = self._graph.config

    @staticmethod
    def _build(
        state_root: str | Path | None,
        audit_root: str | Path | None,
        config_root: str | Path | None,
    ) -> EngineGraph:
        """The graph this process can honestly assemble.

        Both host-facing seams are supplied with what a standalone MCP process
        actually has: no catalog to advertise and no analysis path to record
        through. Each is a stated value rather than an omission — ``build_engine``
        has no defaults for them precisely so that a surface which cannot supply
        one has to say what it is passing instead.
        """
        return build_engine(
            model_resolver=no_host_model_catalog,
            findings_sink=RefusingFindingsSink(),
            host_state=None,
            session_opener=RefusingSessionOpener(),
            state_root=state_root,
            audit_root=audit_root,
            config_root=config_root,
        )

    @property
    def graph(self) -> EngineGraph:
        """The engine graph this adapter runs over.

        Exposed so a future run-touching tool reaches ``graph.machine`` — the
        process's one run-state writer — instead of building a second machine.
        """
        return self._graph

    # --- reads -------------------------------------------------------------

    def validate_spec(self, project: str, spec: str) -> dict[str, Any]:
        """Validate every native document a spec owes, plus their cross-links."""
        ref = SpecRef.of(project, spec)
        report = validate_spec_documents(ref.spec_dir)
        return _report_json(report)

    def get_phase(self, project: str, spec: str) -> dict[str, Any]:
        """Report where a spec sits, derived read-only from disk and approvals."""
        ref = SpecRef.of(project, spec)
        return derive_phase(self._store, ref).to_json_object()

    def list_tasks(self, project: str, spec: str) -> dict[str, Any]:
        """List the checklist and the leaf tasks a spec's tasks.md declares."""
        ref = SpecRef.of(project, spec)
        tasks_path = ref.spec_dir / "tasks.md"
        if not tasks_path.is_file():
            return {"present": False, "tasks": [], "leaves": []}
        plan = parse_tasks(tasks_path.read_text(encoding="utf-8"))
        tasks = [
            {
                "number": t.number,
                "title": t.title,
                "complete": t.complete,
                "criteria": [str(ref_) for ref_ in t.references],
            }
            for t in plan.tasks
        ]
        return {
            "present": True,
            "tasks": tasks,
            "leaves": [t.number for t in plan.leaves],
        }

    # --- state changes -----------------------------------------------------

    def record_approval(self, project: str, spec: str, gate: str, actor: str) -> dict[str, Any]:
        """Record an interactive approval of one gate, through the engine's gate.

        Runs in interactive mode: the recorded approver is *actor*, a human
        identity the caller supplies. The engine validates the document and
        refuses an approval it would not otherwise accept, so this cannot record
        an approval the library would have rejected.
        """
        ref = SpecRef.of(project, spec)
        outcome = approve(
            self._store,
            ref,
            gate,
            actor=actor,
            mode=RunMode.INTERACTIVE,
            audit=self._audit,
        )
        return outcome.to_json_object()

    def advance_phase(
        self, project: str, spec: str, actor: str, gate: str | None = None
    ) -> dict[str, Any]:
        """Ask the engine whether a spec may move past a gate, and record it."""
        ref = SpecRef.of(project, spec)
        result = advance(self._store, ref, actor=actor, gate=gate, audit=self._audit)
        return result.to_json_object()

    # --- the guarded configuration door ------------------------------------

    def write_config(self, patch: Mapping[str, Any]) -> dict[str, Any]:
        """Persist a configuration *patch* through the engine's single write path.

        This is the ONLY door this adapter has onto configuration, and it is
        fenced by construction: the write goes through :meth:`ConfigStore.write`
        on :data:`ENGINE_MCP_SURFACE`, which is not operator-confirmed, so the
        engine refuses every config-only path — the autonomy policy, the delivery
        workflow, and everything else the shared fence guards. No tool is wired
        to this method; it exists so that any configuration this adapter ever
        needs to write cannot escape the fence the rest of the app relies on.

        Writes through the graph's own store, so this adapter reads and writes
        configuration through one object rather than through a second store whose
        root could drift from the one everything else resolved.
        """
        return self._config.write(patch, surface=ENGINE_MCP_SURFACE)
