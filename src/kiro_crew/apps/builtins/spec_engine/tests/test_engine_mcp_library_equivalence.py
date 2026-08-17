"""The MCP surface and the library leave the engine in the same state.

The engine has two front doors: the Python library, and the MCP server that wraps
it. The promise is that they are the same engine, and "a guarantee that holds on
one path while an equivalent second path bypasses it" is the shape of every
security defect this spec has shipped. Two front doors onto one state machine is
that shape, so this file is a fence.

How the claim is made non-vacuous, since "identical resulting state" is easy to
assert about nothing:

* **Both paths are real.** The MCP path is the packaged server as a child process
  driven over stdio through the client init sequence (``StdioServer``, shared with
  the conformance tests). The library path calls ``phases.approve`` /
  ``phases.advance`` / ``derive_phase`` / ``parse_tasks`` / ``validate_spec``
  directly. It deliberately never touches ``EngineOperations``: comparing the
  adapter with itself would assert nothing.
* **State is read raw.** The comparison reads every row of every table with plain
  SQL and every audit record as JSON, not through ``StateStore``'s accessors. An
  accessor used on both sides can normalise a divergence away.
* **Sequences, not single calls.** Each case runs the operations that lead up to
  the one it names, and one case walks every gate of the spec, because a
  divergence can need two operations to become visible.
* **Every case states what changed.** A mutating operation must leave a witness
  row and must move the state away from the untouched baseline; a read-only
  operation must leave the state exactly at the baseline. Equal-but-empty
  therefore fails.
* **Nothing is sampled.** The covered set is derived from the server's own
  dispatch table: a tool marked as needing the engine adapter and missing from
  ``OPERATIONS`` fails ``test_every_tool_is_a_state_operation_or_guidance``.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from kiro_crew._sqlite_compat import sqlite3
from kiro_crew.apps.builtins.spec_engine.engine.audit import AuditLog, audit_root
from kiro_crew.apps.builtins.spec_engine.engine.config import ConfigStore
from kiro_crew.apps.builtins.spec_engine.engine.config.store import CONFIG_FILENAME
from kiro_crew.apps.builtins.spec_engine.engine.cross_document import (
    validate_spec as validate_spec_documents,
)
from kiro_crew.apps.builtins.spec_engine.engine.phases import (
    RunMode,
    advance,
    approve,
    derive_phase,
)
from kiro_crew.apps.builtins.spec_engine.engine.setup import (
    CONFIRMED_LEVELS,
    SetupAnswers,
    apply_setup,
    propose_setup,
)
from kiro_crew.apps.builtins.spec_engine.engine.state import (
    DB_FILENAME,
    SpecRef,
    StateStore,
    state_root,
)
from kiro_crew.apps.builtins.spec_engine.engine.structure import parse_tasks
from kiro_crew.apps.builtins.spec_engine.engine_mcp.server import TOOLS

from .conftest import spec_dir_snapshot
from .test_engine_mcp_conformance import stdio_server
from .test_phases import SPEC_NAME, write_spec

#: Tools that answer from authored text and never reach the engine adapter, so
#: they have no state to compare. Proven rather than assumed by
#: :func:`test_guidance_tools_write_no_state`.
GUIDANCE_ONLY = ("get_authoring_prompt", "get_orchestrator_prompt", "get_review_prompt")

#: Tools that only diagnose. They reach neither the engine adapter nor the state
#: store: the Doctor and the run gate are read-only aggregations over
#: configuration and the environment, so they have no library spelling to compare
#: resulting state against -- what they must prove instead is that they leave the
#: state untouched, which ``test_diagnostic_tools_write_no_state`` does.
DIAGNOSTIC_ONLY = ("run_doctor", "check_run_prerequisites")

#: The setup assistant's tools. They DO reach the engine adapter, so they are not
#: guidance and not diagnostics -- but they touch neither the state store nor the
#: audit log. They read a project's own files and, for the one that writes, write
#: the configuration document. So their equivalence claim is about ``config.json``
#: rather than about rows, and the ``Step`` machinery below cannot express them
#: anyway: every step it builds is ``(project, spec)``-shaped and these tools
#: declare no spec. ``test_setup_tools_write_the_same_configuration_as_the_library``
#: makes the claim in the form that fits: same document from both paths, and the
#: reads leaving the state at the untouched baseline.
CONFIGURATION_ONLY = ("inspect_setup", "plan_setup", "apply_setup")

#: Arguments for the diagnostic tools, so the no-state proof can call them.
DIAGNOSTIC_ARGUMENTS: dict[str, dict[str, Any]] = {
    "run_doctor": {},
    "check_run_prerequisites": {"autonomy": "authoring"},
}

#: Arguments for the guidance tools, so the no-state proof can actually call them.
GUIDANCE_ARGUMENTS: dict[str, dict[str, Any]] = {
    "get_authoring_prompt": {"spec_type": "feature"},
    "get_orchestrator_prompt": {},
    "get_review_prompt": {},
}

#: Identity used as approver and initiator on both paths. A single constant so a
#: divergence cannot hide behind two different actors.
ACTOR = "reviewer@example"

#: Columns whose value is a wall-clock or a random token: they differ between two
#: runs by design, so they are compared as present-or-absent rather than by value.
#: Deliberately narrow. Nothing else is normalised — a future operation that
#: writes an identifier of its own should fail this comparison loudly and have its
#: normalisation decided on purpose, not inherit a wildcard.
VOLATILE_COLUMNS = frozenset({"lock_token", "lock_expires_epoch"})

#: Placeholder substituted for a volatile value that is present.
VOLATILE = "<volatile>"


# --- the two spellings of each state operation -----------------------------


def _ref(args: dict[str, Any]) -> SpecRef:
    return SpecRef.of(args["project"], args["spec"])


def _lib_validate_spec(store: StateStore, audit: AuditLog, args: dict[str, Any]) -> Any:
    return validate_spec_documents(_ref(args).spec_dir)


def _lib_get_phase(store: StateStore, audit: AuditLog, args: dict[str, Any]) -> Any:
    return derive_phase(store, _ref(args))


def _lib_list_tasks(store: StateStore, audit: AuditLog, args: dict[str, Any]) -> Any:
    tasks_path = _ref(args).spec_dir / "tasks.md"
    if not tasks_path.is_file():
        return None
    return parse_tasks(tasks_path.read_text(encoding="utf-8"))


def _lib_record_approval(store: StateStore, audit: AuditLog, args: dict[str, Any]) -> Any:
    # Interactive mode with a human actor: the same authority the tool claims. A
    # tool that quietly approved as the Autonomy_Policy instead would diverge from
    # this call, which is exactly what the comparison has to catch.
    return approve(
        store,
        _ref(args),
        args["gate"],
        actor=args["actor"],
        mode=RunMode.INTERACTIVE,
        audit=audit,
    )


def _lib_advance_phase(store: StateStore, audit: AuditLog, args: dict[str, Any]) -> Any:
    return advance(store, _ref(args), actor=args["actor"], gate=args.get("gate"), audit=audit)


def _json(value: Any) -> Any:
    """Round-trip through JSON the way the server serialises a tool result."""
    return json.loads(json.dumps(value, default=str))


def _expect_serialised(result: Any) -> Any:
    """The library object's own JSON form. Its serialiser, not the adapter's."""
    return _json(result.to_json_object())


def _observe_identity(payload: Any) -> Any:
    return payload


def _expect_report(result: Any) -> Any:
    """Field-level view of a ValidationReport, built from the report's attributes."""
    return {
        "ok": bool(result.ok),
        "violations": [
            [v.file, v.location.line, v.rule, v.severity.value, v.message]
            for v in result.violations
        ],
    }


def _observe_report(payload: Any) -> Any:
    return {
        "ok": payload["ok"],
        "violations": [
            [v["file"], v["line"], v["rule"], v["severity"], v["message"]]
            for v in payload["violations"]
        ],
    }


def _expect_plan(result: Any) -> Any:
    """Field-level view of a TaskPlan, built from the plan's own objects."""
    if result is None:
        return {"present": False, "tasks": [], "leaves": []}
    return {
        "present": True,
        "tasks": [[t.number, t.title, t.complete] for t in result.tasks],
        "leaves": [t.number for t in result.leaves],
    }


def _observe_plan(payload: Any) -> Any:
    return {
        "present": payload["present"],
        "tasks": [[t["number"], t["title"], t["complete"]] for t in payload["tasks"]],
        "leaves": list(payload["leaves"]),
    }


@dataclass(frozen=True)
class StateOperation:
    """One state operation in both spellings, plus how to compare the results."""

    #: The library call: the second path, written from the engine's own API.
    library: Callable[[StateStore, AuditLog, dict[str, Any]], Any]
    #: Library result -> comparable form.
    expect: Callable[[Any], Any] = _expect_serialised
    #: MCP payload -> comparable form.
    observe: Callable[[Any], Any] = _observe_identity


#: Every state operation the server dispatches, keyed by tool name. The keys are
#: fenced against the dispatch table by
#: :func:`test_every_tool_is_a_state_operation_or_guidance`.
OPERATIONS: dict[str, StateOperation] = {
    "validate_spec": StateOperation(_lib_validate_spec, _expect_report, _observe_report),
    "get_phase": StateOperation(_lib_get_phase),
    "list_tasks": StateOperation(_lib_list_tasks, _expect_plan, _observe_plan),
    "record_approval": StateOperation(_lib_record_approval),
    "advance_phase": StateOperation(_lib_advance_phase),
}


# --- driving one sequence down both paths ----------------------------------


@dataclass(frozen=True)
class Step:
    """One operation in a sequence, named by its tool."""

    tool: str
    extra: dict[str, Any] = field(default_factory=dict)

    def arguments(self, project: Path) -> dict[str, Any]:
        return {"project": str(project), "spec": SPEC_NAME, **self.extra}


def _approve(gate: str) -> Step:
    return Step("record_approval", {"gate": gate, "actor": ACTOR})


def _advance(gate: str) -> Step:
    return Step("advance_phase", {"gate": gate, "actor": ACTOR})


#: The lead-up each operation needs before it is the one under test, and the full
#: walk. A single-call comparison would miss a divergence that only appears once
#: state exists, so every mutating operation is measured after the operations that
#: create the state it reads.
_REQUIREMENTS_APPROVED = (Step("validate_spec"), Step("get_phase"), _approve("requirements"))

SEQUENCES: dict[str, tuple[Step, ...]] = {
    "validate_spec": (Step("validate_spec"),),
    "get_phase": (Step("get_phase"),),
    "list_tasks": (Step("list_tasks"),),
    "record_approval": _REQUIREMENTS_APPROVED,
    "advance_phase": _REQUIREMENTS_APPROVED + (_advance("requirements"),),
}

#: Every gate, walked end to end, with reads interleaved. This is the case that
#: can see a divergence needing more than one operation.
FULL_WALK: tuple[Step, ...] = (
    Step("validate_spec"),
    Step("get_phase"),
    _approve("requirements"),
    _advance("requirements"),
    Step("get_phase"),
    _approve("design"),
    _advance("design"),
    Step("get_phase"),
    _approve("tasks"),
    _advance("tasks"),
    Step("get_phase"),
    Step("list_tasks"),
)

#: Operations that must leave a mark on the state store. The rest must leave none:
#: a read that started writing on one path only is a divergence too.
MUTATING = frozenset({"record_approval", "advance_phase"})


@contextmanager
def _pinned_home(home: Path) -> Iterator[None]:
    """Resolve the engine's default roots under *home* for the duration.

    The library path uses the same default-root resolution the server child uses,
    so neither side is reading a root the other could not have.
    """
    previous = os.environ.get("KIROCREW_HOME")
    os.environ["KIROCREW_HOME"] = str(home)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("KIROCREW_HOME", None)
        else:
            os.environ["KIROCREW_HOME"] = previous


def _run_via_mcp(steps: tuple[Step, ...], project: Path, home: Path) -> list[Any]:
    """Drive *steps* over stdio, through the client init sequence first."""
    with stdio_server(home) as running:
        advertised = running.initialize()
        for step in steps:
            assert step.tool in advertised, f"{step.tool} is not advertised by the server"
        return [running.tool_payload(step.tool, step.arguments(project)) for step in steps]


def _run_via_library(steps: tuple[Step, ...], project: Path, home: Path) -> list[Any]:
    """Run the same steps as direct library calls against *home*."""
    with _pinned_home(home):
        store = StateStore(root=None)
        audit = AuditLog(root=None)
        try:
            return [
                OPERATIONS[step.tool].library(store, audit, step.arguments(project))
                for step in steps
            ]
        finally:
            store.close()


# --- reading the resulting state, rawly ------------------------------------


def _normalise(value: Any, *, column: str = "") -> Any:
    """Blank out only wall-clock and random values, recursively."""
    if isinstance(value, dict):
        return {key: _normalise(item, column=str(key)) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalise(item, column=column) for item in value]
    if value is None or value == "":
        return value
    if column == "ts" or column.endswith("_ts"):
        return VOLATILE
    if column in VOLATILE_COLUMNS:
        return VOLATILE
    return value


def _dump_tables(database: Path) -> dict[str, list[Any]]:
    """Every row of every table, by raw SQL.

    Deliberately not ``StateStore``: an accessor is a normaliser, and reading both
    sides through the same one can hide the divergence being looked for. This
    reads what was actually written.
    """
    if not database.is_file():
        return {}
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        connection.row_factory = sqlite3.Row
        names = [
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            )
        ]
        tables: dict[str, list[Any]] = {}
        for name in names:
            rows = [
                _normalise(dict(row))
                for row in connection.execute(f"SELECT * FROM {name}")  # nosec B608 - name is
                # read back from sqlite_master, never from a caller
            ]
            tables[name] = sorted(rows, key=lambda row: json.dumps(row, sort_keys=True))
        return tables
    finally:
        connection.close()


def _dump_audit(root: Path) -> dict[str, list[Any]]:
    """Every audit record, keyed by its path relative to the audit root."""
    base = audit_root(root)
    if not base.is_dir():
        return {}
    logs: dict[str, list[Any]] = {}
    for path in sorted(base.rglob("*.jsonl")):
        records = [
            _normalise(json.loads(line))
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        logs[path.relative_to(base).as_posix()] = records
    return logs


def _dump_files(home: Path) -> dict[str, str]:
    """Every OTHER file under *home*, as a path-to-content-hash inventory.

    The row and audit dumps read the two places state is written today, which is
    why a review demonstrated a hole in them: an operation writing a sidecar file
    on ONE path only left every comparison green. That is precisely the invisible
    second path this whole module exists to fence, so the dump has to cover the
    home rather than the two stores anyone thought of.

    The database and the audit logs are skipped because they are compared
    structurally above -- hashing them here would make every row difference show
    up twice, once uselessly. Content is hashed rather than kept so a large file
    costs nothing and a diff still shows up.
    """
    root = home
    inventory: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if relative.endswith(DB_FILENAME) or "/audit/" in f"/{relative}":
            continue
        inventory[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return inventory


def _dump_state(home: Path) -> dict[str, Any]:
    """The whole resulting state under *home*: rows, audit records, and files."""
    with _pinned_home(home):
        root = state_root()
    return {
        "tables": _dump_tables(root / DB_FILENAME),
        "audit": _dump_audit(root),
        "files": _dump_files(home),
    }


def _rows(dump: dict[str, Any], table: str) -> list[Any]:
    return list(dump["tables"].get(table, []))


@pytest.fixture(scope="module")
def baseline(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    """The state of a home where the server started and nothing else happened.

    Every case is compared against this so "the two paths agree" cannot pass by
    both of them doing nothing.
    """
    home = tmp_path_factory.mktemp("baseline") / "home"
    with stdio_server(home) as running:
        running.initialize()
    return _dump_state(home)


def _project(tmp_path: Path, name: str) -> Path:
    """A project holding this repository's own spec, at one fixed path.

    One path for both runs, so the project string and the spec key that hashes it
    are identical in both stores and the dumps compare as written.
    """
    return write_spec(tmp_path / name)


def _compare_paths(
    steps: tuple[Step, ...], tmp_path: Path, label: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run *steps* over MCP and over the library; return both state dumps."""
    project = _project(tmp_path, f"{label}-project")
    spec_dir = project / ".kiro" / "specs" / SPEC_NAME
    before = spec_dir_snapshot(spec_dir)

    mcp_home = tmp_path / f"{label}-mcp-home"
    library_home = tmp_path / f"{label}-library-home"

    mcp_payloads = _run_via_mcp(steps, project, mcp_home)
    # The spec directory is the interop contract: if the MCP run had rewritten a
    # document, the library run would be reading different bytes and the
    # comparison below would be between two different inputs.
    assert spec_dir_snapshot(spec_dir) == before, "the MCP run rewrote the spec directory"

    library_results = _run_via_library(steps, project, library_home)
    assert spec_dir_snapshot(spec_dir) == before, "the library run rewrote the spec directory"

    for step, payload, result in zip(steps, mcp_payloads, library_results):
        operation = OPERATIONS[step.tool]
        # Normalised the same way the state dump is: a result carries the wall
        # clock (`approved_ts`), which differs between two sequential runs by
        # design. Presence is still compared, so a path that stopped recording a
        # timestamp at all still fails here.
        observed = _normalise(operation.observe(payload))
        expected = _normalise(operation.expect(result))
        assert (
            observed == expected
        ), f"{step.tool} returned different results over MCP and over the library"

    return _dump_state(mcp_home), _dump_state(library_home)


# --- the fence: nothing is sampled ----------------------------------------


def test_every_tool_is_a_state_operation_or_guidance() -> None:
    # The covered set comes from the server's dispatch table, not from memory: a
    # new tool that reaches the engine adapter fails here until it is covered, and
    # a new guidance tool has to be declared as one.
    state_tools = {name for name, spec in TOOLS.items() if spec.needs_ops}
    assert state_tools == set(OPERATIONS) | set(
        CONFIGURATION_ONLY
    ), "a tool reaching the engine adapter has no library spelling here"
    assert set(TOOLS) == (
        set(OPERATIONS) | set(GUIDANCE_ONLY) | set(DIAGNOSTIC_ONLY) | set(CONFIGURATION_ONLY)
    )
    assert set(GUIDANCE_ARGUMENTS) == set(GUIDANCE_ONLY)
    assert set(DIAGNOSTIC_ARGUMENTS) == set(DIAGNOSTIC_ONLY)
    assert set(SEQUENCES) == set(OPERATIONS), "a state operation has no sequence"


def test_guidance_tools_write_no_state(tmp_path: Path, baseline: dict[str, Any]) -> None:
    # The exclusion above is proven, not assumed: the guidance tools are actually
    # called, and the state they leave is the untouched baseline.
    home = tmp_path / "guidance-home"
    with stdio_server(home) as running:
        running.initialize()
        for name in GUIDANCE_ONLY:
            text = running.tool_text(name, GUIDANCE_ARGUMENTS[name])
            assert text.strip(), f"{name} returned no guidance"
    assert _dump_state(home) == baseline


def test_diagnostic_tools_write_no_state(tmp_path: Path, baseline: dict[str, Any]) -> None:
    # A diagnostic exists to be safe to run on a broken host, so "read-only" is
    # proven by bytes rather than by inspection: the tools are called through the
    # real child server and the state tree afterwards is the untouched baseline.
    home = tmp_path / "diagnostic-home"
    with stdio_server(home) as running:
        running.initialize()
        for name in DIAGNOSTIC_ONLY:
            payload = running.tool_payload(name, DIAGNOSTIC_ARGUMENTS[name])
            assert isinstance(payload, dict) and payload, f"{name} returned nothing"
    assert _dump_state(home) == baseline


# --- the setup tools: one engine, one configuration document ---------------

#: A project whose own files state a remote and a review practice, so the setup
#: assistant has something to infer from on both paths.
_SETUP_PROJECT = "setup-project"

#: The answers used on both paths. Every rung declined, so the case does not
#: depend on an autonomy grant to produce a document.
_SETUP_ANSWERS: dict[str, Any] = {
    "cost_profile": "budget",
    "confirmations": {"execution": False, "delivery": False, "integration": False},
    "approved_subjects": ["watch.source", "workflow.preset", "tooling"],
    "workflow_preset": "git-pull-request",
    "watch_source": "github",
}


def _setup_project(tmp_path: Path) -> Path:
    root = tmp_path / _SETUP_PROJECT
    (root / ".git").mkdir(parents=True)
    (root / ".git" / "config").write_text(
        '[remote "origin"]\n\turl = git@github.com:acme/widgets.git\n', encoding="utf-8"
    )
    steering = root / ".kiro" / "steering"
    steering.mkdir(parents=True)
    (steering / "review.md").write_text("We land changes through a pull request.\n", "utf-8")
    (root / "Makefile").write_text("build:\n\t@echo build\n\ntest:\n\t@echo test\n", "utf-8")
    return root


def test_setup_reads_write_no_state(tmp_path: Path, baseline: dict[str, Any]) -> None:
    # The two read-only setup tools, through the real child server: the state tree
    # afterwards is the untouched baseline, which for these also means no
    # configuration document was created.
    project = _setup_project(tmp_path)
    home = tmp_path / "setup-read-home"
    with stdio_server(home) as running:
        running.initialize()
        inspected = running.tool_payload("inspect_setup", {"project": str(project)})
        assert inspected["inferences"], "inspection found nothing to compare"
        planned = running.tool_payload(
            "plan_setup", {"project": str(project), "answers": _SETUP_ANSWERS}
        )
        assert planned["plan_id"], "planning returned no identity"
    assert _dump_state(home) == baseline


def test_setup_tools_write_the_same_configuration_as_the_library(
    tmp_path: Path, baseline: dict[str, Any]
) -> None:
    """The apply tool is the library's flow, not a second implementation of it.

    Both paths run against their own pinned home and the same project, and the
    document each produces is compared byte for byte. The state tables and audit
    log are compared against a home where the server merely started, so "the setup
    tools wrote only configuration" is a measurement rather than a claim about
    intent.
    """
    project = _setup_project(tmp_path)

    mcp_home = tmp_path / "setup-mcp-home"
    with stdio_server(mcp_home) as running:
        running.initialize()
        planned = running.tool_payload(
            "plan_setup", {"project": str(project), "answers": _SETUP_ANSWERS}
        )
        applied = running.tool_payload(
            "apply_setup",
            {
                "project": str(project),
                "answers": _SETUP_ANSWERS,
                "plan_id": planned["plan_id"],
                "approver": ACTOR,
            },
        )
    assert applied["applied"] is True, f"the apply did not report a write: {applied}"

    library_home = tmp_path / "setup-library-home"
    with _pinned_home(library_home):
        plan = propose_setup(project.resolve(), project=_SETUP_PROJECT)
        library_result = apply_setup(
            ConfigStore(),
            plan,
            SetupAnswers(
                cost_profile=str(_SETUP_ANSWERS["cost_profile"]),
                confirmations={level: False for level in CONFIRMED_LEVELS},
                approved_subjects=frozenset(item.subject for item in plan.inferences),
                workflow_preset=str(_SETUP_ANSWERS["workflow_preset"]),
                watch_source=str(_SETUP_ANSWERS["watch_source"]),
            ),
        )
    assert library_result.written_paths == tuple(applied["written_paths"])

    with _pinned_home(mcp_home):
        mcp_document = ConfigStore().path
    with _pinned_home(library_home):
        library_document = ConfigStore().path
        library_valid = ConfigStore().validate()

    # The claim, at the file: the tool's document and the library's document are
    # the same bytes -- same merged content, same version stamp, same formatting.
    # A tool that assembled its own patch, wrote through another surface, or
    # skipped the stamp diverges here.
    assert mcp_document.is_file(), "the apply tool wrote no configuration document"
    assert library_document.is_file(), "the library path wrote no configuration document"
    assert mcp_document.read_bytes() == library_document.read_bytes()
    assert library_valid == ()

    # And the tool wrote ONLY configuration: no row, no audit record. Compared
    # against a home where the server merely started, because starting it creates
    # the state database either way -- that is the difference this comparison must
    # not read as a write.
    mcp_state = _dump_state(mcp_home)
    assert mcp_state["tables"] == baseline["tables"], "the setup tools wrote state rows"
    assert mcp_state["audit"] == baseline["audit"], "the setup tools wrote audit records"
    assert CONFIG_FILENAME not in " ".join(
        baseline["files"]
    ), "the baseline already holds a configuration document, so its absence proves nothing"
    assert any(name.endswith(CONFIG_FILENAME) for name in mcp_state["files"])


# --- the claim: identical resulting state ---------------------------------


@pytest.mark.parametrize("tool", sorted(SEQUENCES))
def test_state_operation_leaves_identical_state(
    tool: str, tmp_path: Path, baseline: dict[str, Any]
) -> None:
    mcp_state, library_state = _compare_paths(SEQUENCES[tool], tmp_path, tool)

    assert mcp_state == library_state, f"{tool} left different state over MCP than over the library"

    if tool in MUTATING:
        # Non-vacuity: the equality above would also hold if both paths stopped
        # writing anything, so the state has to have actually moved.
        assert mcp_state != baseline, f"{tool} wrote nothing on either path"
    else:
        # A read is a claim too: it must leave the store exactly as it found it, on
        # both paths, or one of them is writing state the other does not.
        assert mcp_state == baseline, f"{tool} is a read but changed the state store"


def test_recorded_approval_is_the_same_row_on_both_paths(
    tmp_path: Path, baseline: dict[str, Any]
) -> None:
    # The witness the equality assertion needs: the approvals row an approval
    # writes, named field by field. If either path stopped writing it, the row is
    # missing here even though the two empty tables would still be equal.
    mcp_state, library_state = _compare_paths(SEQUENCES["record_approval"], tmp_path, "approval")

    for label, state in (("mcp", mcp_state), ("library", library_state)):
        rows = _rows(state, "approvals")
        assert len(rows) == 1, f"{label} recorded {len(rows)} approvals, expected one"
        row = rows[0]
        assert row["gate"] == "requirements"
        assert row["actor"] == ACTOR
        assert row["stale"] == 0
        # The hash of what was approved is the staleness mechanism: an approval
        # recorded without it would still be a row, and would still compare equal
        # to another empty-hash row.
        assert row["doc_hash"], f"{label} recorded an approval with no document hash"

    mcp_hash = _rows(mcp_state, "approvals")[0]["doc_hash"]
    assert mcp_hash == _rows(library_state, "approvals")[0]["doc_hash"]
    assert _rows(baseline, "approvals") == []


def test_advance_moves_the_phase_the_same_way_on_both_paths(tmp_path: Path) -> None:
    # The witness for advance: the specs row's phase, plus the audit records both
    # paths owe. Named explicitly so a path that stopped recording either one
    # fails even though the two dumps would still match each other.
    mcp_state, library_state = _compare_paths(SEQUENCES["advance_phase"], tmp_path, "advance")

    for label, state in (("mcp", mcp_state), ("library", library_state)):
        specs = _rows(state, "specs")
        assert len(specs) == 1, f"{label} has {len(specs)} spec rows, expected one"
        assert specs[0]["phase"] == "design", f"{label} did not move the phase past requirements"
        records = [record for log in state["audit"].values() for record in log]
        assert records, f"{label} recorded no audit entries for an approval and an advance"
        assert {record["initiator"] for record in records} == {ACTOR}


def test_the_full_gate_walk_leaves_identical_state(
    tmp_path: Path, baseline: dict[str, Any]
) -> None:
    # Every gate, in order, with reads interleaved: the case that can see a
    # divergence which needs more than one operation to appear.
    mcp_state, library_state = _compare_paths(FULL_WALK, tmp_path, "walk")

    assert mcp_state == library_state, "the full gate walk diverged between MCP and the library"
    assert mcp_state != baseline
    # The walk really walked: three gates approved, and the phase left the first.
    assert {row["gate"] for row in _rows(mcp_state, "approvals")} == {
        "requirements",
        "design",
        "tasks",
    }
    assert _rows(mcp_state, "specs")[0]["phase"] not in (None, "requirements")


# --- the property: any order of operations, not just the ones chosen here ---

#: The operations a generated sequence draws from, including the out-of-order and
#: repeated calls a hand-written case would not think to try (advancing before
#: approving, approving the same gate twice, approving a later gate first). A
#: refusal is a legitimate outcome; what the property claims is that both paths
#: reach the SAME outcome and the same state.
STEP_POOL: tuple[Step, ...] = (
    Step("validate_spec"),
    Step("get_phase"),
    Step("list_tasks"),
    _approve("requirements"),
    _advance("requirements"),
    _approve("design"),
    _advance("design"),
)

#: Small on purpose: every example starts a server child and walks a real spec, so
#: the budget buys ordering coverage rather than volume.
PROPERTY_EXAMPLES = 8

#: Indexes into the pool, split by whether the operation writes. A generated
#: sequence always gets one write spliced into it: a probe that dropped the audit
#: write on the MCP path only was NOT caught while the generator was free to emit
#: reads alone, because two untouched stores compare equal.
READ_PICKS = tuple(index for index, step in enumerate(STEP_POOL) if step.tool not in MUTATING)
WRITE_PICKS = tuple(index for index, step in enumerate(STEP_POOL) if step.tool in MUTATING)


@settings(
    max_examples=PROPERTY_EXAMPLES,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(
    picks=st.lists(st.sampled_from(READ_PICKS + WRITE_PICKS), max_size=4),
    required=st.sampled_from(WRITE_PICKS),
    position=st.integers(min_value=0, max_value=4),
)
def test_any_order_of_state_operations_leaves_identical_state(
    tmp_path: Path, picks: list[int], required: int, position: int
) -> None:
    indexes = list(picks)
    indexes.insert(min(position, len(indexes)), required)
    steps = tuple(STEP_POOL[index] for index in indexes)
    # A fresh example directory, so an earlier example's rows cannot decide this
    # one's outcome.
    example = tmp_path / uuid.uuid4().hex
    example.mkdir(parents=True)
    mcp_state, library_state = _compare_paths(steps, example, "property")

    named = [step.tool for step in steps]
    assert mcp_state == library_state, f"{named} diverged"
    # Non-vacuity: a generated write can legitimately be refused, but reaching one
    # at all makes the store aware of the spec, so this comparison is never
    # between two stores that were left untouched.
    assert _rows(mcp_state, "specs"), f"{named} left no state to compare"
