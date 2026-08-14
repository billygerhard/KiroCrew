"""Public build posture: the tool surface, the default bindings, and egress.

The pluggability these tests guard is built elsewhere — the registry resolves
bindings, :func:`register_builtins` supplies the bundled defaults, and the
transports reach an external provider. What is *not* built anywhere is the proof
of the two claims that make the app publishable, so this module is those proofs:

*The tool surface does not depend on what is bound.* An enhanced provider may
serve analysis, review, the model catalog, or a watch source, and it may degrade
while doing so; none of that may add, remove, or rename a tool. A Host_Agent's
instructions are written against the advertised surface, so a surface that moved
with an operator's bindings would make every agent's instructions conditional on
someone's configuration. The comparison here is over a fingerprint that includes
each tool's description and full input schema, and
:meth:`TestToolSurfaceIsInvariantAcrossBindings.test_the_comparison_would_catch_an_injected_tool`
drives an injected and a removed tool through the same fingerprint so a passing
equality is known to be an observation rather than two empty sets meeting.

*Nothing leaves the machine unless an operator asked for it.* Proven three ways,
because each is observable in a different place: every zero-configuration binding
resolves to a builtin over the in-process transport, the default spec-processing
path completes with sockets and child processes sealed off, and the app's own
modules import no network client and no telemetry emitter.

Two boundaries worth naming, so this module is not mistaken for either:

* The repo-wide provenance gate that fails a build on a non-public endpoint or
  service name is not here. This module's subject is narrower and different in
  kind: which providers the *default bindings* resolve to, and what the default
  path does at runtime.
* "Spawns no child" is a claim about the **default spec-processing path** only —
  validation, phase reads, task listing, and a capability call served by its
  builtin. The delivery pipeline, the watch poll, and the external capability
  transports all execute operator-configured commands by design, and sealing them
  would be asserting the opposite of what they are for.
"""

from __future__ import annotations

import ast
import configparser
import http.client
import json
import os
import socket
import subprocess
import urllib.request
from contextlib import contextmanager
from glob import glob
from pathlib import Path
from typing import Any, Iterator

import pytest

import kiro_crew
from kiro_crew.apps.builtins.spec_engine.engine.capabilities import (
    TRANSPORT_BUILTIN,
    TRANSPORT_COMMAND,
    TRANSPORT_MCP,
    ArtifactRef,
    CapabilityRegistry,
    CapabilityRequest,
    ProviderKind,
)
from kiro_crew.apps.builtins.spec_engine.engine.capabilities.builtins import register_builtins
from kiro_crew.apps.builtins.spec_engine.engine.composition import build_engine
from kiro_crew.apps.builtins.spec_engine.engine.config import (
    DASHBOARD_SURFACE,
    DELEGABLE_CAPABILITIES,
    TRANSPORTS,
    ConfigStore,
    default_of,
)
from kiro_crew.apps.builtins.spec_engine.engine_mcp import server
from kiro_crew.apps.builtins.spec_engine.engine_mcp.operations import EngineOperations

from .conftest import make_spec_dir

APP_ROOT = Path(__file__).resolve().parent.parent
PKG_ROOT = Path(kiro_crew.__file__).resolve().parent
REPO_ROOT = PKG_ROOT.parents[1]

#: The telemetry knob the app declares. Kept as a constant so the two tests that
#: read it cannot drift onto different keys.
TELEMETRY_SETTING = "telemetry.enabled"

#: Transports that carry a call out of the engine's process. Derived from
#: :data:`TRANSPORTS` rather than listed, so a transport added later is treated as
#: leaving the process until someone states otherwise here.
EXTERNAL_TRANSPORTS = tuple(t for t in TRANSPORTS if t != TRANSPORT_BUILTIN)


# --- helpers ---------------------------------------------------------------


def _tools_list(ops: EngineOperations | None = None) -> list[dict[str, Any]]:
    """The advertised tool surface, read the way a client reads it."""
    reply = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, ops=ops)
    assert reply is not None and "result" in reply, reply
    tools = reply["result"]["tools"]
    assert isinstance(tools, list)
    return tools


def _surface(ops: EngineOperations | None = None) -> frozenset[str]:
    """A fingerprint per advertised tool: name, description, and input schema.

    Names alone would miss a renamed argument or a widened schema, both of which
    change what an agent may send. The schema is serialized with sorted keys so
    two equal schemas cannot differ by dict ordering.
    """
    return frozenset(
        json.dumps(
            {
                "name": tool["name"],
                "description": tool["description"],
                "inputSchema": tool["inputSchema"],
            },
            sort_keys=True,
        )
        for tool in _tools_list(ops)
    )


def _names(ops: EngineOperations | None = None) -> frozenset[str]:
    return frozenset(str(tool["name"]) for tool in _tools_list(ops))


def _call(name: str, arguments: dict[str, Any], ops: EngineOperations | None) -> dict[str, Any]:
    reply = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
        ops=ops,
    )
    assert reply is not None
    return reply


def _bind_all(store: ConfigStore, transport: str, program: str) -> None:
    """Bind every delegable capability to *transport*, through the write path."""
    store.write(
        {
            "capabilities": {
                capability: {"transport": transport, "command": [program]}
                for capability in DELEGABLE_CAPABILITIES
            }
        },
        surface=DASHBOARD_SURFACE,
    )


def _request(capability: str) -> CapabilityRequest:
    return CapabilityRequest(
        capability=capability,
        spec_type="feature",
        artifacts=(ArtifactRef(kind="requirements", path="/p/requirements.md"),),
        run="run-posture",
    )


def _ops(tmp_path: Path) -> EngineOperations:
    return EngineOperations(
        state_root=tmp_path / "state",
        audit_root=tmp_path / "audit",
        config_root=tmp_path / "config",
    )


# --- the tool surface under every binding ----------------------------------


class TestToolSurfaceIsInvariantAcrossBindings:
    """The agent-facing surface is identical builtin, external, or degraded."""

    def test_the_surface_is_the_pinned_set_with_no_configuration(self, tmp_path: Path) -> None:
        # Pinned by name so the equalities below cannot pass by comparing two
        # empty — or two equally shrunken — surfaces.
        assert _names(_ops(tmp_path)) == frozenset(
            {
                "get_authoring_prompt",
                "get_orchestrator_prompt",
                "get_review_prompt",
                "validate_spec",
                "get_phase",
                "list_tasks",
                "record_approval",
                "advance_phase",
                "run_doctor",
                "check_run_prerequisites",
            }
        )

    @pytest.mark.parametrize("transport", EXTERNAL_TRANSPORTS)
    def test_binding_every_capability_externally_changes_no_tool(
        self, tmp_path: Path, transport: str
    ) -> None:
        ops = _ops(tmp_path)
        baseline = _surface(ops)
        store = ConfigStore(tmp_path / "config")
        _bind_all(store, transport, "enhanced-provider")
        # The bindings really did land: otherwise this compares the default
        # configuration with itself and proves nothing about an external one.
        bound = CapabilityRegistry(store).bindings()
        assert {b.transport for b in bound.values()} == {transport}
        assert _surface(ops) == baseline

    def test_a_degraded_external_provider_changes_no_tool(self, tmp_path: Path) -> None:
        ops = _ops(tmp_path)
        baseline = _surface(ops)
        store = ConfigStore(tmp_path / "config")
        # A program that does not exist is the shape an unavailable enhanced
        # provider takes. The call degrades and falls back to the builtin.
        _bind_all(store, TRANSPORT_COMMAND, str(tmp_path / "no-such-provider"))
        registry = CapabilityRegistry(store)
        register_builtins(registry, model_resolver=lambda: ("auto",))
        result = registry.invoke(_request("analysis"))
        assert result.degraded, "the unavailable provider did not degrade"
        assert result.degradation is not None
        # The degradation is reported through the result, which is where a
        # surface reads it — not through the tool list.
        assert _surface(ops) == baseline

    def test_no_bound_program_name_reaches_the_tool_names(self, tmp_path: Path) -> None:
        # A future design that exposed one tool per bound provider would satisfy
        # a set-equality check between two identically-configured runs while
        # still making the surface configuration-dependent. The bound program's
        # name is the thing such a design would have to spell, so it may not
        # appear — checked on the program rather than on the capability, because
        # "authoring" and "review" are legitimate words in an authored tool name.
        ops = _ops(tmp_path)
        before = _names(ops)
        store = ConfigStore(tmp_path / "config")
        _bind_all(store, TRANSPORT_MCP, "enhanced-provider")
        names = _names(ops)
        assert names == before
        assert not any("enhanced" in name for name in names)

    def test_an_operation_returns_the_same_answer_under_an_external_binding(
        self, tmp_path: Path, project: Path
    ) -> None:
        # Declaring the same tools while answering differently would be the same
        # divergence at one remove, so the surface claim is also driven through a
        # call: a spec's validation report cannot move because a capability is
        # bound to a provider that is not there.
        ops = _ops(tmp_path)
        before = _call("validate_spec", {"project": str(project), "spec": "example"}, ops)
        store = ConfigStore(tmp_path / "config")
        _bind_all(store, TRANSPORT_COMMAND, str(tmp_path / "no-such-provider"))
        assert _call("validate_spec", {"project": str(project), "spec": "example"}, ops) == before

    def test_the_comparison_would_catch_an_injected_tool(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The teeth of every equality above, exercised on purpose."""
        ops = _ops(tmp_path)
        baseline = _surface(ops)
        assert baseline, "an empty surface would make every equality vacuous"

        injected = dict(server.TOOLS)
        injected["enhanced_analysis"] = server.ToolSpec(
            lambda _args, _ops: "", "an injected tool", {}, ()
        )
        monkeypatch.setattr(server, "TOOLS", injected)
        assert _surface(ops) != baseline, "an added tool went unnoticed"
        assert "enhanced_analysis" in _names(ops)

        shrunk = dict(server.TOOLS)
        shrunk.pop("enhanced_analysis")
        shrunk.pop("validate_spec")
        monkeypatch.setattr(server, "TOOLS", shrunk)
        assert _surface(ops) != baseline, "a removed tool went unnoticed"

    def test_the_comparison_would_catch_a_reworded_schema(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ops = _ops(tmp_path)
        baseline = _surface(ops)
        original = server.TOOLS["get_authoring_prompt"]
        widened = dict(server.TOOLS)
        widened["get_authoring_prompt"] = server.ToolSpec(
            original.fn,
            original.description,
            {**original.properties, "depth": {"type": "string"}},
            original.required,
        )
        monkeypatch.setattr(server, "TOOLS", widened)
        assert _surface(ops) != baseline, "a widened input schema went unnoticed"


# --- the default bindings stay on this machine ------------------------------


class TestDefaultBindingsResolveToBundledProviders:
    def test_a_zero_configuration_engine_binds_every_capability_to_a_builtin(
        self, tmp_path: Path
    ) -> None:
        # Through the composition root rather than a hand-built registry: the
        # posture that matters is the one a surface actually gets.
        graph = build_engine(
            model_resolver=lambda: ("auto",),
            findings_sink=_NullFindingsSink(),
            host_state=None,
            session_opener=_NoSessionOpener(),
            state_root=tmp_path / "state",
            audit_root=tmp_path / "audit",
            config_root=tmp_path / "config",
        )
        bindings = graph.registry.bindings()
        assert set(bindings) == set(DELEGABLE_CAPABILITIES)
        assert bindings, "no bindings resolved; the assertions below would be vacuous"
        for capability, binding in bindings.items():
            assert binding.transport == TRANSPORT_BUILTIN, capability
            assert binding.transport not in EXTERNAL_TRANSPORTS, capability
            assert not binding.configured, capability
            assert binding.argv == (), capability
        for entry in graph.registry.describe():
            assert entry["provider"]["kind"] == ProviderKind.BUILTIN.value, entry["capability"]

    def test_the_only_in_process_transport_is_the_builtin_one(self) -> None:
        # EXTERNAL_TRANSPORTS is derived, so this pins the classification itself:
        # a transport added to the schema lands in the external set and forces
        # whoever added it past the assertions above.
        assert TRANSPORT_BUILTIN in TRANSPORTS
        assert set(EXTERNAL_TRANSPORTS) == {TRANSPORT_MCP, TRANSPORT_COMMAND}


class _NoSessionOpener:
    """An opener no test reaches: nothing here seeds a run, so it refuses."""

    def __call__(self, request: Any) -> Any:
        raise AssertionError("no test in this module opens a session")


class _NullFindingsSink:
    """A sink that records nothing; the graph needs one, no test reads it."""

    def record(self, ref: Any, *, run: str, report: Any) -> None:
        return None


# --- the default path opens no socket and spawns no child -------------------


@contextmanager
def _sealed(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Seal every egress and child-spawn door, recording any attempt.

    Patched on the owning modules, because that is where the app reaches them
    (``import subprocess`` then ``subprocess.Popen``), so a call through any of
    these names raises instead of connecting.
    """
    attempts: list[str] = []

    def refuse(label: str):
        def guard(*_args: Any, **_kwargs: Any):
            attempts.append(label)
            raise AssertionError(f"the default path reached {label}")

        return guard

    monkeypatch.setattr(socket, "socket", refuse("socket.socket"))
    monkeypatch.setattr(socket, "create_connection", refuse("socket.create_connection"))
    monkeypatch.setattr(subprocess, "Popen", refuse("subprocess.Popen"))
    monkeypatch.setattr(subprocess, "run", refuse("subprocess.run"))
    monkeypatch.setattr(subprocess, "call", refuse("subprocess.call"))
    monkeypatch.setattr(subprocess, "check_output", refuse("subprocess.check_output"))
    monkeypatch.setattr(urllib.request, "urlopen", refuse("urllib.request.urlopen"))
    monkeypatch.setattr(http.client, "HTTPConnection", refuse("http.client.HTTPConnection"))
    monkeypatch.setattr(http.client, "HTTPSConnection", refuse("http.client.HTTPSConnection"))
    monkeypatch.setattr(os, "system", refuse("os.system"))
    yield attempts


class TestDefaultSpecProcessingIsLocal:
    """What the default path does *not* do, observed rather than reasoned about."""

    def test_the_seal_actually_trips(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Without this, a sealed test that quietly stopped exercising anything
        # would pass forever. Each door is proven closed before it is trusted.
        with _sealed(monkeypatch):
            for attempt in (
                lambda: socket.socket(),
                lambda: socket.create_connection(("127.0.0.1", 9)),
                lambda: subprocess.run(["true"]),
                lambda: subprocess.Popen(["true"]),
                lambda: subprocess.call(["true"]),
                lambda: subprocess.check_output(["true"]),
                lambda: urllib.request.urlopen("http://127.0.0.1:9/"),
                lambda: http.client.HTTPConnection("127.0.0.1"),
                lambda: http.client.HTTPSConnection("127.0.0.1"),
                lambda: os.system("true"),
            ):
                with pytest.raises(AssertionError):
                    attempt()

    def test_building_the_engine_and_serving_every_capability_stays_local(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with _sealed(monkeypatch) as attempts:
            graph = build_engine(
                model_resolver=lambda: ("auto",),
                findings_sink=_NullFindingsSink(),
                host_state=None,
                session_opener=_NoSessionOpener(),
                state_root=tmp_path / "state",
                audit_root=tmp_path / "audit",
                config_root=tmp_path / "config",
            )
            served = [
                graph.registry.invoke(_request(capability)) for capability in DELEGABLE_CAPABILITIES
            ]
        assert len(served) == len(DELEGABLE_CAPABILITIES)
        # Each capability really answered — a raised guard would have failed the
        # call, and an empty list would have passed while examining nothing.
        assert all(not result.degraded for result in served)
        assert attempts == []

    def test_reading_and_validating_a_spec_stays_local(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = tmp_path / "project"
        project.mkdir()
        make_spec_dir(project, "example")
        ops = _ops(tmp_path)
        with _sealed(monkeypatch) as attempts:
            report = ops.validate_spec(str(project), "example")
            phase = ops.get_phase(str(project), "example")
            tasks = ops.list_tasks(str(project), "example")
            listed = _tools_list(ops)
            called = _call("get_orchestrator_prompt", {}, ops)
        assert "ok" in report and "violations" in report
        assert phase and tasks is not None
        assert listed and "result" in called
        assert attempts == []


# --- telemetry --------------------------------------------------------------


class TestTelemetryIsOffAndUnimplemented:
    """The app declares one telemetry knob and ships no emitter behind it.

    Absence is the stronger guarantee, so it is the one asserted: there is no
    payload to keep content-free because nothing is ever sent. If an emitter is
    ever added, :meth:`test_the_app_imports_no_telemetry_emitter` fails, and
    whoever adds it owes a test over the payload's fields — no spec text, no item
    body, no user identifier — which cannot be written against nothing.
    """

    def test_the_setting_ships_off(self) -> None:
        assert default_of(TELEMETRY_SETTING) is False

    def test_a_zero_configuration_store_reports_it_off(self, tmp_path: Path) -> None:
        store = ConfigStore(tmp_path / "config")
        assert store.effective(TELEMETRY_SETTING).value is False

    def test_the_app_imports_no_telemetry_emitter(self) -> None:
        offenders = _scan_app_imports(_TELEMETRY_MODULES)
        assert offenders == [], (
            "the app now reaches a telemetry emitter: "
            f"{offenders}. Content-free was guaranteed by absence; an emitter "
            "needs a test over the fields of what it sends."
        )

    def test_the_app_imports_no_network_client(self) -> None:
        # The import-level half of "all spec processing local": a module that
        # cannot construct a client cannot transmit through one.
        offenders = _scan_app_imports(_NETWORK_MODULES)
        assert offenders == [], f"the app now imports a network client: {offenders}"

    def test_the_import_scanner_actually_detects(self, tmp_path: Path) -> None:
        # The scan above passes on an app that imports nothing at all, so the
        # detector is driven against source that does import these.
        planted = tmp_path / "planted.py"
        planted.write_text(
            "import socket\n"
            "from urllib.request import urlopen\n"
            "from kiro_crew.beacon import send\n"
            "from .relative import thing\n",
            encoding="utf-8",
        )
        imported = _imported_modules(planted)
        assert _offending(imported, _NETWORK_MODULES) == ["socket", "urllib.request"]
        assert _offending(imported, _TELEMETRY_MODULES) == ["kiro_crew.beacon"]
        # A relative import cannot name a host module, and treating it as one
        # would flag every in-package import the app makes.
        assert "relative" not in imported


#: Modules that can put bytes on a wire. Import of any of them from app code
#: would be the first half of a transmission path.
_NETWORK_MODULES = frozenset(
    {
        "socket",
        "ssl",
        "http",
        "urllib.request",
        "urllib.error",
        "requests",
        "httpx",
        "aiohttp",
        "websockets",
        "ftplib",
        "smtplib",
        "xmlrpc",
        "telnetlib",
    }
)

#: The host's telemetry emitters. The app reaches neither.
_TELEMETRY_MODULES = frozenset({"kiro_crew.beacon", "kiro_crew.telemetry"})


def _imported_modules(path: Path) -> set[str]:
    """Every absolute module name *path* imports, plus its ``from`` targets.

    Relative imports are skipped: they name modules inside this app, which by
    construction are not the host modules being looked for.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def _offending(imported: set[str], deny: frozenset[str]) -> list[str]:
    """Denied module names among *imported*, matching on dotted prefixes.

    Prefix matching so ``import urllib.request.foo`` and
    ``from kiro_crew.beacon import send`` are both caught by one entry.
    """
    hits: set[str] = set()
    for name in imported:
        parts = name.split(".")
        for index in range(1, len(parts) + 1):
            prefix = ".".join(parts[:index])
            if prefix in deny:
                hits.add(prefix)
    return sorted(hits)


def _app_modules() -> list[Path]:
    """Every shipped module of this app. Tests are excluded: they do not ship."""
    return sorted(
        path for path in APP_ROOT.rglob("*.py") if "tests" not in path.relative_to(APP_ROOT).parts
    )


def _scan_app_imports(deny: frozenset[str]) -> list[str]:
    modules = _app_modules()
    assert modules, "no app modules found; the scan would pass while reading nothing"
    offenders: list[str] = []
    for path in modules:
        for name in _offending(_imported_modules(path), deny):
            offenders.append(f"{path.relative_to(APP_ROOT)}: {name}")
    return sorted(offenders)


# --- the bundled resources actually ship -----------------------------------


class TestBundledResourcesArePackaged:
    """A manifest or skill the build does not package ships as an absent file.

    Resolved with the same ``glob(..., recursive=True)`` call setuptools makes
    over ``[options.package_data]``, so this reads the real patterns rather than
    a restatement of them. It is not a wheel build: what it proves is that the
    declared globs match these files on disk, and that a bundled file landing in
    an undeclared directory is caught.
    """

    @pytest.fixture()
    def packaged(self) -> frozenset[Path]:
        setup_cfg = REPO_ROOT / "setup.cfg"
        if not setup_cfg.is_file():
            pytest.skip("needs the repo checkout: setup.cfg is not in the package")
        parser = configparser.ConfigParser()
        parser.read(setup_cfg, encoding="utf-8")
        patterns = [
            line.strip()
            for line in parser["options.package_data"]["kiro_crew"].splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        assert patterns, "no package_data patterns read; every assertion would be vacuous"
        matched: set[Path] = set()
        for pattern in patterns:
            for hit in glob(str(PKG_ROOT / pattern), recursive=True):
                path = Path(hit)
                if path.is_file():
                    matched.add(path.resolve())
        return frozenset(matched)

    def test_the_manifest_and_the_skill_are_packaged(self, packaged: frozenset[Path]) -> None:
        # Pinned by name: a reworded glob that stopped covering these would
        # otherwise pass the completeness check below by covering nothing.
        for relative in ("app.json", "skills/spec-engine-discovery/SKILL.md"):
            path = (APP_ROOT / relative).resolve()
            assert path.is_file(), f"{relative} is missing from the app"
            assert path in packaged, f"{relative} is not covered by any package_data glob"

    def test_every_bundled_non_python_file_is_packaged(self, packaged: frozenset[Path]) -> None:
        # The completeness half: a preset table, a template, or a JSON schema
        # dropped into a directory no glob names would ship as an absent file.
        bundled = [
            path.resolve()
            for path in APP_ROOT.rglob("*")
            if path.is_file()
            and path.suffix != ".py"
            and "__pycache__" not in path.parts
            and "tests" not in path.relative_to(APP_ROOT).parts
        ]
        assert bundled, "no bundled files found; this test would prove nothing"
        missing = sorted(str(p.relative_to(APP_ROOT)) for p in bundled if p not in packaged)
        assert missing == [], (
            f"bundled files no package_data glob covers: {missing}. Add a glob to "
            "setup.cfg or the installed app ships without them."
        )

    def test_an_undeclared_path_is_not_reported_as_packaged(
        self, packaged: frozenset[Path]
    ) -> None:
        # The resolver returns matches, not everything: a .py file under the app
        # is shipped as a module, never as package_data, so it must be absent.
        assert (APP_ROOT / "readiness.py").resolve() not in packaged
