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
from typing import Any, Mapping

from ..engine.audit import AuditLog
from ..engine.config import ConfigStore, ConfigWriteSurface
from ..engine.cross_document import validate_spec as validate_spec_documents
from ..engine.phases import RunMode, advance, approve, derive_phase
from ..engine.state import SpecRef, StateStore
from ..engine.structure import parse_tasks

#: The surface this adapter writes configuration through. It is deliberately not
#: operator-confirmed: a tool call arrives from an agent, not from a human
#: looking at a configuration panel, so the engine's write path refuses any
#: config-only path (the autonomy policy, the delivery workflow, capability
#: bindings, quality gates, intake guidance, and the auto-integrate switch) from
#: it. Flipping this to confirmed would hand an agent the authority the whole
#: config-only fence exists to withhold.
ENGINE_MCP_SURFACE = ConfigWriteSurface("engine-mcp", operator_confirmed=False)


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


class EngineOperations:
    """Adapter binding tool calls to engine library calls.

    Roots are injectable so a test can point the adapter at a temporary state
    directory; in production every root resolves to the app's own data home.
    """

    def __init__(
        self,
        *,
        state_root: str | Path | None = None,
        audit_root: str | Path | None = None,
        config_root: str | Path | None = None,
    ) -> None:
        self._store = StateStore(root=state_root)
        self._audit = AuditLog(root=audit_root)
        self._config_root = Path(config_root) if config_root is not None else None

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
        """
        store = ConfigStore(root=self._config_root)
        return store.write(patch, surface=ENGINE_MCP_SURFACE)
