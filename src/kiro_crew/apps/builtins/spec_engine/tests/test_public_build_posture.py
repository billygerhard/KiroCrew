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

*Nothing non-public is in the tree.* This app is authored clean-room for a public
repository, so the rule is not "no secrets" but "no non-public reference at all":
no internal endpoint or hostname, no internal service name, no internal header, no
credential. The provenance scan at the end of this module fails the build on any of
them, asserts the bundled preset tables name only public systems, proves a
delegated provider is reachable by configuration alone on every transport, and
pins the inventory of shipped prompt text. What that scan can and cannot see is
stated on :class:`TestNoNonPublicReferenceIsInTheTree`, because a scanner is worth
only the spellings it was driven against.

One boundary worth naming, so this module is not mistaken for it:

* "Spawns no child" is a claim about the **default spec-processing path** only —
  validation, phase reads, task listing, and a capability call served by its
  builtin. The delivery pipeline, the watch poll, and the external capability
  transports all execute operator-configured commands by design, and sealing them
  would be asserting the opposite of what they are for.
"""

from __future__ import annotations

import ast
import builtins
import configparser
import http.client
import importlib
import ipaddress
import json
import os
import re
import socket
import subprocess
import sys
import urllib.request
from contextlib import contextmanager
from glob import glob
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

import pytest

import kiro_crew
from kiro_crew.apps.builtins.spec_engine.engine.capabilities import (
    TRANSPORT_BUILTIN,
    TRANSPORT_COMMAND,
    TRANSPORT_MCP,
    ArtifactRef,
    Binding,
    CapabilityRegistry,
    CapabilityRequest,
    ProviderKind,
    resolve_bindings,
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
from kiro_crew.apps.builtins.spec_engine.engine.delivery import (
    GIT_ISOLATE_COMMANDS,
    QUALITY_GATE_PRESETS,
    WORKFLOW_PRESET_NAMES,
    WORKFLOW_PRESETS,
)
from kiro_crew.apps.builtins.spec_engine.engine.watch import (
    FEEDBACK_PRESET_HOSTS,
    FEEDBACK_PRESETS,
)
from kiro_crew.apps.builtins.spec_engine.engine_mcp import server
from kiro_crew.apps.builtins.spec_engine.engine_mcp.guidance import FLOWS, get_guidance
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


# --- provenance: nothing non-public is in the tree --------------------------

#: The app's own package, derived rather than spelled: this module lives in the
#: app's ``tests`` package, so the parent is the app. Used to recognize an
#: argument that names an in-tree module.
APP_PACKAGE = (__package__ or "").rsplit(".", 1)[0]

#: File kinds the provenance scan reads. The app ships nothing else, which
#: :meth:`TestNoNonPublicReferenceIsInTheTree.test_the_scan_reads_the_whole_tree`
#: asserts rather than assumes.
PROVENANCE_SUFFIXES = frozenset({".py", ".json", ".md"})

#: Name suffixes reserved by RFC 2606 and RFC 6761 for documentation and testing.
#: A host under one of these can never resolve, so it cannot be an address of
#: anything, public or not — which is what makes a fixture that uses one provably
#: fake rather than merely unfamiliar.
RESERVED_HOST_SUFFIXES = (".invalid", ".test", ".example", ".localhost")

#: Reserved names with no dot to match on.
RESERVED_HOSTS = frozenset({"localhost", "example.com", "example.net", "example.org"})

#: The public hosts this tree is allowed to name, reviewed one at a time.
#:
#: This list is the inversion that makes the endpoint rule mean something. A
#: denylist of internal-looking names would only ever be a claim about what its
#: author thought of, and the names themselves could not be written down here
#: anyway — spelling an internal hostname in the check would be the very thing
#: the check forbids. So the rule is closed the other way: a host is either
#: provably unresolvable (reserved above), loopback, or on this list, and
#: anything else fails. An internal address fails without this file having to
#: know what internal addresses look like, and the cost of adding a public host
#: is one reviewed line here.
PUBLIC_HOSTS = frozenset({"api.github.com", "github.com", "gitlab.com", "json-schema.org"})

#: Programs a bundled command table may name. All five are public tools any
#: reader can obtain: an organization's own CLI in a bundled preset would be a
#: non-public system name shipped in the package, so the set is closed.
PUBLIC_PROGRAMS = frozenset({"gh", "git", "glab", "make", "python"})

#: Scheme-and-host of a URL. The host class stops at every delimiter a URL can
#: end on, and deliberately does **not** exclude ``{``: a templated host is not
#: recognizable as public, so it fails rather than being skipped.
_URL_RE = re.compile(r"(?i)\b[a-z][a-z0-9+.\-]*://(?P<host>[^\s/?#'\"`)\]}>,;|\\]*)")

#: A dotted quad, bounded so ``10.0`` in a float and ``10.10`` in a requirement
#: number cannot match. Every hit is parsed as an address before it is judged;
#: an earlier draft that matched two- and three-octet prefixes fired on
#: ``BUSY_TIMEOUT_S = 10.0``, which is how a check gets disabled.
_IPV4_RE = re.compile(r"(?<![\w.])\d{1,3}(?:\.\d{1,3}){3}(?![\w.])")

#: HTTP headers that carry authentication or identity. The trailing colon is
#: required so prose naming a header in a sentence does not fire.
_AUTH_HEADER_RE = re.compile(
    r"(?i)\b(?:authorization|proxy-authorization|www-authenticate|x-api-key"
    r"|x-auth-token|set-cookie|cookie)[\"']?\s*:"
)

#: The shape of a custom header, which is how an internal one would arrive: this
#: catches a header name nobody here could have listed.
_CUSTOM_HEADER_RE = re.compile(r"\bX-[A-Za-z][A-Za-z0-9]*(?:-[A-Za-z][A-Za-z0-9]*)+[\"']?\s*:")

#: Credential shapes, matched on their issued form rather than on any word.
#: Nothing here looks at an identifier called ``token``: the notification token
#: bucket, the coverage tokens, and the parsed tokens elsewhere in this app are
#: all legitimate uses of the word, and a check that fired on them would be
#: turned off within a day.
_CREDENTIAL_RE = re.compile(
    r"AKIA[0-9A-Z]{12,}"
    r"|ASIA[0-9A-Z]{12,}"
    r"|gh[opsur]_[A-Za-z0-9]{20,}"
    r"|github_pat_[A-Za-z0-9_]{20,}"
    r"|glpat-[A-Za-z0-9_\-]{15,}"
    r"|xox[abprs]-[A-Za-z0-9\-]{10,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\."
    r"|(?i:bearer)\s+[A-Za-z0-9._~+/=\-]{16,}"
)

#: Marker of a documentation placeholder, by the same convention the reserved
#: TLDs above use. The AWS documentation key ``AKIAIOSFODNN7EXAMPLE`` appears in
#: this app's redaction test, where its whole point is being scrubbed from
#: output; a credential shape that does not say EXAMPLE is not exempt, which
#: ``credential-key`` among the planted cases proves.
_DOC_PLACEHOLDER = "EXAMPLE"

#: An identifier component that means "this holds a credential". Whole components
#: only, so ``authoring`` is not an ``auth`` and ``TOKEN_BUCKET`` is only a
#: candidate — it still has to hold a credential-shaped literal to be reported.
_CREDENTIAL_NAME_RE = re.compile(
    r"(?i)(?:^|_)(?:tokens?|secrets?|passwords?|passwd|api_?keys?|credentials?"
    r"|private_key|access_key)(?:$|_)"
)

#: What an issued credential looks like as a literal: long, unbroken, and mixing
#: letters with digits. ``PLANTED-INSTRUCTION-do-as-i-say`` and
#: ``SENTINEL-DO-NOT-READ`` are sentinels in this app's tests and carry no digit,
#: so they are not reported; a real key does.
_SECRET_SHAPE_RE = re.compile(r"^[A-Za-z0-9_\-+/=.]{16,}$")

#: Shipped prompt text held outside :mod:`engine_mcp.guidance`, as a review
#: ledger. Each entry is text a model reads, so a new one has to be looked at by
#: a human; the completeness check below fails until it is recorded here.
PROMPT_SOURCES: tuple[tuple[str, str], ...] = (
    ("engine.analysis", "AUTHORED_ANALYSIS_PROMPT"),
    ("engine.review_queue", "_REVISION_INSTRUCTION"),
    ("engine.setup", "LEVEL_PROMPTS"),
    ("engine.watch.dispatch", "_SEED_INSTRUCTION"),
    ("engine.watch.screening", "BUNDLED_SCREENING_GUIDANCE"),
    ("engine.watch.screening_provider", "VERDICT_INSTRUCTION"),
)

#: Identifier shapes that name prompt text, and the length above which a string
#: constant is prose rather than a message fragment. Together they are the
#: forcing function behind :data:`PROMPT_SOURCES`.
_PROMPT_NAME_RE = re.compile(r"(?i)(prompt|guidance|instruction|criteri)")
_PROMPT_MIN_CHARS = 200

#: Top-level names of the standard library, read from the interpreter rather than
#: listed: a list would go stale against the next Python and start reporting a
#: stdlib module as vendored third-party code.
_STDLIB_ROOTS = frozenset(sys.stdlib_module_names)


def _provenance_files() -> list[Path]:
    """Every file in the app tree the provenance scan reads, tests included.

    Tests are in scope here — unlike the import scan above, which asks what
    *ships* — because this repository is public: a non-public hostname in a test
    fixture is published exactly as widely as one in a shipped module.
    """
    return sorted(
        path
        for path in APP_ROOT.rglob("*")
        if path.is_file() and path.suffix in PROVENANCE_SUFFIXES and "__pycache__" not in path.parts
    )


def _host_of(raw: str) -> str:
    """The bare host of a URL authority: no userinfo, no port, lowercased."""
    host = raw.rsplit("@", 1)[-1].strip().lower().rstrip(".")
    if host.startswith("["):  # bracketed IPv6 literal
        return host.partition("]")[0].lstrip("[")
    if host.count(":") == 1:
        host = host.rpartition(":")[0]
    return host


def _host_is_public(host: str) -> bool:
    """Whether *host* is provably fake, loopback, or a reviewed public name."""
    if not host:
        return True  # no authority to judge: not an address
    if host in RESERVED_HOSTS or host.endswith(RESERVED_HOST_SUFFIXES):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        pass
    return host in PUBLIC_HOSTS


def _non_public_references(text: str) -> list[tuple[int, str]]:
    """Every non-public endpoint, address, header, or credential shape in *text*.

    Comments and docstrings are **in scope**. A docstring ships in the package and
    is read by anyone who installs the app, so a hostname in one is as published
    as a hostname in code. That is affordable only because every rule here matches
    an address or a secret rather than vocabulary: a docstring explaining that an
    organization's internal tracker is configuration rather than a bundled preset
    contains no host and no credential, so it passes untouched.
    """
    found: list[tuple[int, str]] = []
    for index, line in enumerate(text.splitlines(), start=1):
        for match in _URL_RE.finditer(line):
            host = _host_of(match.group("host"))
            if not _host_is_public(host):
                found.append((index, f"non-public endpoint host {host!r}"))
        for match in _IPV4_RE.finditer(line):
            try:
                address = ipaddress.IPv4Address(match.group(0))
            except ValueError:
                continue
            if not address.is_loopback:
                found.append((index, f"network address literal {match.group(0)!r}"))
        for regex, label in (
            (_AUTH_HEADER_RE, "auth header"),
            (_CUSTOM_HEADER_RE, "custom header"),
        ):
            header = regex.search(line)
            if header:
                found.append((index, f"{label} literal {header.group(0)!r}"))
        for match in _CREDENTIAL_RE.finditer(line):
            if _DOC_PLACEHOLDER in match.group(0).upper():
                continue
            found.append((index, f"credential shape {match.group(0)[:12]!r}..."))
    return found


def _secret_shaped(value: Any) -> bool:
    return (
        isinstance(value, str)
        and bool(_SECRET_SHAPE_RE.match(value))
        and any(character.isdigit() for character in value)
        and any(character.isalpha() for character in value)
        and _DOC_PLACEHOLDER not in value.upper()
    )


def _credential_assignments(source: str) -> list[int]:
    """Lines binding a credential-named target to a credential-shaped literal.

    Parsed rather than matched as text, and that is the whole reason this rule is
    usable: ``write_text("aws_secret_access_key = SENTINEL-DO-NOT-READ")`` in this
    app's symlink test is a call argument, not an assignment, so an AST rule walks
    past it while a regex over source text would have reported it.

    Three spellings are followed, because a credential arrives by whichever one
    is at hand: a name bound to it, a keyword argument, and a mapping entry.
    """
    hits: list[int] = []
    for node in ast.walk(ast.parse(source)):
        candidates: list[tuple[str, ast.expr]] = []
        lineno = 0
        if isinstance(node, ast.Assign):
            lineno = node.lineno
            for target in node.targets:
                name = _target_name(target)
                if name:
                    candidates.append((name, node.value))
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            lineno = node.lineno
            name = _target_name(node.target)
            if name:
                candidates.append((name, node.value))
        elif isinstance(node, ast.Call):
            lineno = node.lineno
            candidates.extend((kw.arg, kw.value) for kw in node.keywords if kw.arg)
        elif isinstance(node, ast.Dict):
            lineno = node.lineno
            for key, value in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    candidates.append((key.value, value))
        for name, value in candidates:
            if not _CREDENTIAL_NAME_RE.search(name):
                continue
            if isinstance(value, ast.Constant) and _secret_shaped(value.value):
                hits.append(lineno)
    return sorted(set(hits))


def _target_name(target: ast.expr) -> str:
    if isinstance(target, ast.Name):
        return target.id
    if isinstance(target, ast.Attribute):
        return target.attr
    return ""


def _planted_url(host: str, path: str = "/v1") -> str:
    """A URL assembled at runtime, so no offending literal sits in this file.

    The scan reads this module like any other — asserted, so nobody exempts it —
    which means a planted violation written as a literal would fail the very check
    it exists to prove. Concatenation keeps the scheme-and-authority pair out of
    the source text while the value handed to the scanner is exactly what a real
    one is.
    """
    return "https:" + "//" + host + path


def _bundled_command_tables() -> dict[str, tuple[tuple[str, ...], ...]]:
    """Every argv the app bundles, read from the real tables it ships.

    Read rather than restated: a fourth workflow preset or a third feedback host
    lands in this mapping without anyone updating a fixture, which is the
    difference between a check and a copy of the thing it checks.
    """
    tables: dict[str, tuple[tuple[str, ...], ...]] = {
        "isolation.GIT_ISOLATE_COMMANDS": tuple(tuple(argv) for argv in GIT_ISOLATE_COMMANDS)
    }
    for name, stages in WORKFLOW_PRESETS.items():
        for stage, commands in stages.items():
            tables[f"WORKFLOW_PRESETS[{name}].{stage}"] = tuple(tuple(a) for a in commands)
    for name, preset in QUALITY_GATE_PRESETS.items():
        commands = preset["commands"]
        tables[f"QUALITY_GATE_PRESETS[{name}]"] = tuple(tuple(a) for a in commands)
    for host, events in FEEDBACK_PRESETS.items():
        for event, commands in events.items():
            tables[f"FEEDBACK_PRESETS[{host}].{event}"] = tuple(tuple(a) for a in commands)
    return tables


def _non_public_programs(tables: Mapping[str, Sequence[Sequence[str]]]) -> list[str]:
    """Programs in *tables* that are not public tools, as ``table: program``."""
    offenders: list[str] = []
    for label, commands in sorted(tables.items()):
        for argv in commands:
            if argv and argv[0] not in PUBLIC_PROGRAMS:
                offenders.append(f"{label}: {argv[0]}")
    return offenders


def _in_tree_argument(argument: str) -> bool:
    """Whether *argument* names an implementation inside this app's tree.

    Two spellings, because a command reaches in-tree code by either: a dotted
    module under the app's package (what ``python -m`` takes), and a filesystem
    path that lands under the app root. A bare program name is resolved through
    PATH by the OS and only counts if a file of that name actually exists here,
    so ``make`` is not mistaken for an in-tree program.
    """
    if argument == APP_PACKAGE or argument.startswith(APP_PACKAGE + "."):
        return True
    candidate = Path(argument)
    try:
        resolved = (candidate if candidate.is_absolute() else APP_ROOT / candidate).resolve()
    except (OSError, ValueError):
        return False
    if resolved != APP_ROOT and APP_ROOT not in resolved.parents:
        return False
    return candidate.is_absolute() or resolved.exists()


def _vendored_provider_offenders(bindings: Mapping[str, Binding]) -> list[str]:
    """Bindings whose provider is vendored into the tree rather than configured.

    Unconditional across transports by construction: there is no per-transport
    branch that could exempt one. ``command`` was where an earlier version of this
    rule leaked — a program is a program whether it is spawned or imported — so it
    is judged by the same two lines as ``mcp``. The ``builtin`` transport is not
    an exemption either: it is the engine answering in-process and carries no argv
    at all, so a builtin binding holding a program is a delegated provider wearing
    the engine's name.
    """
    offenders: list[str] = []
    for capability, binding in sorted(bindings.items()):
        if binding.transport == TRANSPORT_BUILTIN:
            if binding.argv:
                offenders.append(
                    f"{capability}: builtin binding carrying program {binding.argv[0]}"
                )
            continue
        in_tree = [argument for argument in binding.argv if _in_tree_argument(argument)]
        if in_tree:
            offenders.append(f"{capability}: {binding.transport} provider in the tree {in_tree}")
    return offenders


def _prompt_named_constants(tree: ast.Module) -> list[str]:
    """Module-level constants in *tree* that hold prompt text, by name and length.

    Deliberately narrow: a name that says prompt, guidance, instruction, or
    criteria, holding at least :data:`_PROMPT_MIN_CHARS` of literal text. A
    broader "any long string" rule swept up SQL statements and analysis fixtures,
    and a ledger that fails when someone adds a schema statement is a ledger
    nobody keeps.
    """
    found: list[str] = []
    for node in tree.body:
        pairs: list[tuple[ast.expr, ast.expr]] = []
        if isinstance(node, ast.Assign):
            pairs = [(target, node.value) for target in node.targets]
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            pairs = [(node.target, node.value)]
        for target, value in pairs:
            name = _target_name(target)
            if not name or not _PROMPT_NAME_RE.search(name):
                continue
            text = sum(
                len(child.value)
                for child in ast.walk(value)
                if isinstance(child, ast.Constant) and isinstance(child.value, str)
            )
            if text >= _PROMPT_MIN_CHARS:
                found.append(name)
    return found


def _prompt_constants() -> list[tuple[str, str]]:
    """Every prompt-holding constant in the shipped modules, as (module, name).

    The completeness half of :data:`PROMPT_SOURCES`: the ledger is a claim, and
    this is the reading of the tree the claim is compared against.
    """
    found: list[tuple[str, str]] = []
    for path in _app_modules():
        relative = path.relative_to(APP_ROOT)
        module = ".".join(relative.with_suffix("").parts)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        found.extend((module, name) for name in _prompt_named_constants(tree))
    return sorted(found)


def _prompt_texts() -> dict[str, str]:
    """All shipped prompt text, keyed by where it comes from.

    Resolved through the runtime for the guidance flows, because their table maps
    a flow to a constant by name and reading the literals would miss the
    composition; read off the module for the ledger entries.
    """
    texts = {f"guidance:{flow}": get_guidance(flow) for flow in FLOWS}
    for module_name, symbol in PROMPT_SOURCES:
        module = importlib.import_module(f"{APP_PACKAGE}.{module_name}")
        value = getattr(module, symbol)
        if isinstance(value, Mapping):
            value = "\n".join(str(item) for item in value.values())
        texts[f"{module_name}:{symbol}"] = str(value)
    skill = APP_ROOT / "skills" / "spec-engine-discovery" / "SKILL.md"
    texts["skills/spec-engine-discovery/SKILL.md"] = skill.read_text(encoding="utf-8")
    return texts


#: Planted hosts and addresses. Invented, and assembled into URLs at runtime by
#: :func:`_planted_url` so the offending form never appears as a literal here.
_PLANTED_FQDN = "review-service.somecorp.net"
_PLANTED_SUBDOMAIN = "specs.eng.somecorp.co"
_PLANTED_SHORT_HOST = "buildhost"
_PLANTED_PRIVATE_IP = "10.20" + ".30.40"
_PLANTED_LAN_IP = "192.168" + ".7.7"

#: Planted credential tails, split so no whole credential is a literal.
_PLANTED_KEY_TAIL = "3XMPLQR7ZZTOPKID"
_PLANTED_PAT_TAIL = "aB1cD2eF3gH4iJ5kL6mN7oP8"


class TestNoNonPublicReferenceIsInTheTree:
    """No non-public endpoint, service name, header, or credential is checked in.

    **What this can see.** Any URL whose host is not provably unresolvable,
    loopback, or a reviewed public name; any non-loopback IPv4 literal; any
    authentication or custom HTTP header literal; any credential in an issued
    shape; and any credential-named binding holding a credential-shaped literal.
    Each of those is driven against a planted violation below, one case per
    spelling, because a rule with no planted case is decoration.

    **What this cannot see**, stated plainly because it is the boundary of the
    guarantee:

    * A non-public **service or system name written as an ordinary word** — no
      host, no scheme, no argv position, no credential shape. That is not
      decidable from text, and the names could not be listed here anyway without
      putting them in the tree. Where such a name would actually do damage it is
      reachable: inside a hostname (the endpoint rule), as the program of a
      bundled command (:class:`TestOnlyPublicPresetsAreBundled`), or as an in-tree
      provider implementation (:class:`TestDelegatedProvidersAreConfigurationOnly`).
      A bare word in prose is left to review.
    * A credential of a shape not listed — a bare high-entropy string bound to an
      innocuous name reads exactly like a test fixture.
    * Anything outside this app's tree. The scan is rooted at :data:`APP_ROOT`.
    """

    def test_the_scan_reads_the_whole_tree(self) -> None:
        files = _provenance_files()
        assert len(files) > 100, "too few files read; the scan would prove little"
        relative = {path.relative_to(APP_ROOT).as_posix() for path in files}
        # Named so a later exemption is visible as a failure here, and so the
        # scan is known to include this module: the planted violations below are
        # assembled at runtime precisely because this file is scanned too.
        for expected in (
            "app.json",
            "skills/spec-engine-discovery/SKILL.md",
            "engine_mcp/guidance.py",
            "engine/delivery/workflow.py",
            "tests/test_public_build_posture.py",
        ):
            assert expected in relative, f"{expected} is outside the provenance scan"
        # Nothing escapes by extension: every file in the tree is a kind the scan
        # reads, so a checked-in .env, .cfg, or .yaml would fail here rather than
        # sit unread.
        unread = sorted(
            path.relative_to(APP_ROOT).as_posix()
            for path in APP_ROOT.rglob("*")
            if path.is_file() and "__pycache__" not in path.parts and path not in set(files)
        )
        assert unread == [], f"files of a kind the provenance scan does not read: {unread}"

    def test_the_tree_names_no_non_public_endpoint_header_or_credential(self) -> None:
        offenders: list[str] = []
        for path in _provenance_files():
            relative = path.relative_to(APP_ROOT).as_posix()
            for line, detail in _non_public_references(path.read_text(encoding="utf-8")):
                offenders.append(f"{relative}:{line}: {detail}")
        assert offenders == [], (
            "non-public references in the tree: "
            f"{offenders}. This app is authored clean-room for a public "
            "repository: an internal endpoint, address, header, or credential "
            "cannot be checked in, in code or in a docstring."
        )

    def test_no_credential_is_bound_to_a_credential_named_symbol(self) -> None:
        offenders: list[str] = []
        for path in _provenance_files():
            if path.suffix != ".py":
                continue
            relative = path.relative_to(APP_ROOT).as_posix()
            source = path.read_text(encoding="utf-8")
            offenders.extend(f"{relative}:{line}" for line in _credential_assignments(source))
        assert offenders == [], f"a credential is assigned in the tree: {offenders}"

    @pytest.mark.parametrize(
        "planted",
        [
            pytest.param(_planted_url(_PLANTED_FQDN, "/api/specs"), id="endpoint-fqdn"),
            pytest.param(_planted_url(_PLANTED_SUBDOMAIN), id="endpoint-subdomain"),
            pytest.param(_planted_url(_PLANTED_SHORT_HOST), id="endpoint-single-label"),
            pytest.param(_planted_url(_PLANTED_PRIVATE_IP), id="endpoint-private-ip"),
            pytest.param(f"HOST = {_PLANTED_LAN_IP!r}", id="address-literal-no-url"),
            pytest.param(
                "# reachable at " + _planted_url(_PLANTED_FQDN), id="endpoint-in-a-comment"
            ),
            pytest.param(
                '"""Docs live at ' + _planted_url(_PLANTED_SUBDOMAIN) + '."""',
                id="endpoint-in-a-docstring",
            ),
            pytest.param('headers = {"' + "Authorization" + '": "x"}', id="header-authorization"),
            pytest.param("send(" + '"X-Api-Key' + '": key)', id="header-api-key"),
            pytest.param("raw = " + '"X-Build-Service' + ': widgets"', id="header-custom-shape"),
            pytest.param("key = " + repr("AKIA" + _PLANTED_KEY_TAIL), id="credential-key"),
            pytest.param("pat = " + repr("ghp_" + _PLANTED_PAT_TAIL), id="credential-pat"),
            pytest.param("pat = " + repr("glpat-" + _PLANTED_PAT_TAIL), id="credential-glpat"),
            pytest.param("hook = " + repr("xoxb-" + _PLANTED_PAT_TAIL), id="credential-chat"),
            pytest.param("header = " + repr("Bearer " + _PLANTED_PAT_TAIL), id="credential-bearer"),
            pytest.param(
                "pem = " + repr("-----BEGIN RSA " + "PRIVATE KEY-----"), id="credential-private-key"
            ),
            pytest.param(
                "jwt = " + repr("eyJ" + "hbGciOiJIUzI1" + "." + "eyJzdWIiOiIx" + "."),
                id="credential-jwt",
            ),
        ],
    )
    def test_each_planted_reference_is_reported(self, planted: str) -> None:
        assert _non_public_references(planted), f"the scan missed this spelling: {planted!r}"

    @pytest.mark.parametrize(
        "planted",
        [
            pytest.param("api_token = " + repr(_PLANTED_PAT_TAIL), id="assigned"),
            pytest.param("PASSWORD: str = " + repr(_PLANTED_PAT_TAIL), id="annotated"),
            pytest.param("client(api_key=" + repr(_PLANTED_PAT_TAIL) + ")", id="keyword-argument"),
            pytest.param('cfg = {"secret": ' + repr(_PLANTED_PAT_TAIL) + "}", id="mapping-entry"),
            pytest.param("self.access_key = " + repr(_PLANTED_PAT_TAIL), id="attribute"),
        ],
    )
    def test_each_planted_credential_binding_is_reported(self, planted: str) -> None:
        assert _credential_assignments(planted), f"the scan missed this spelling: {planted!r}"

    @pytest.mark.parametrize(
        "legitimate",
        [
            pytest.param('"""The host\'s per-app notification token bucket."""', id="token-bucket"),
            pytest.param(
                "processed_names = {_coverage_name(token) for token in processed}",
                id="coverage-tokens",
            ),
            pytest.param(
                'ids = [token.strip() for token in match.group("ids")]', id="parsed-token"
            ),
            pytest.param("secrets.token_hex(_RUN_ID_BYTES)", id="stdlib-token-hex"),
            pytest.param('http.client.HTTPConnection("127.0.0.1")', id="loopback"),
            pytest.param('ARTIFACT = "https://tracker.invalid/acme/pull/7"', id="reserved-invalid"),
            pytest.param('address = "https://example.test/items/1"', id="reserved-test"),
            pytest.param(
                'DEPLOYED = "https://deployed.example/spec-engine"', id="reserved-example"
            ),
            pytest.param('"html_url": "https://github.com/owner/repo/issues/412"', id="github"),
            pytest.param('"$schema": "https://json-schema.org/draft/2020-12/schema"', id="schema"),
            pytest.param("BUSY_TIMEOUT_S = 10.0", id="float-not-an-address"),
            pytest.param("requirement 10.10 writes back where feedback goes", id="requirement-id"),
            pytest.param('quoted="key AKIAIOSFODNN7EXAMPLE rejected"', id="documentation-key"),
            pytest.param(
                "an organization's own internal tracker is served by writing its "
                "own feedback commands into the source, which is configuration "
                "rather than a bundled preset",
                id="docstring-explaining-the-exclusion",
            ),
        ],
    )
    def test_the_scan_is_silent_on_the_legitimate_uses_in_this_tree(self, legitimate: str) -> None:
        # Every case here is a line this tree really contains. A check that fired
        # on any of them would be switched off, and a switched-off check is worse
        # than none — so the false-positive half is pinned as hard as the teeth.
        assert _non_public_references(legitimate) == [], f"false positive on {legitimate!r}"

    @pytest.mark.parametrize(
        "legitimate",
        [
            pytest.param('secret = "PLANTED-INSTRUCTION-do-as-i-say"', id="sentinel-no-digits"),
            pytest.param('secret = tmp_path / "credentials"', id="path-not-a-literal"),
            pytest.param(
                'f.write_text("aws_secret_access_key = SENTINEL-DO-NOT-READ")',
                id="call-argument-not-an-assignment",
            ),
            pytest.param('TOKEN_BUCKET = "notify"', id="short-value"),
            pytest.param("_TEMP_TOKEN_BYTES = 8", id="integer-value"),
            pytest.param("token = tokens[0]", id="not-a-literal"),
            pytest.param('key = "AKIAIOSFODNN7EXAMPLE"', id="documentation-key"),
        ],
    )
    def test_the_credential_binding_scan_is_silent_on_these(self, legitimate: str) -> None:
        assert _credential_assignments(legitimate) == [], f"false positive on {legitimate!r}"


class TestOnlyPublicPresetsAreBundled:
    """The bundled presets name public systems only; an org's own is user config.

    Read off the real tables rather than compared with a hand-written copy of
    them. A fixture that lists what it expects agrees with itself and keeps
    passing after a fourth preset lands, which is the defect this suite exists to
    not repeat.
    """

    def test_exactly_three_public_workflow_presets_ship(self) -> None:
        assert WORKFLOW_PRESET_NAMES == ("git-pull-request", "git-merge-request", "local-only")
        assert tuple(WORKFLOW_PRESETS) == WORKFLOW_PRESET_NAMES

    def test_exactly_two_public_feedback_hosts_ship(self) -> None:
        assert FEEDBACK_PRESET_HOSTS == ("github", "gitlab")
        assert tuple(FEEDBACK_PRESETS) == FEEDBACK_PRESET_HOSTS
        # Each host's own public CLI, so a third host cannot arrive by reusing one.
        for host, program in (("github", "gh"), ("gitlab", "glab")):
            programs = {
                argv[0] for commands in FEEDBACK_PRESETS[host].values() for argv in commands
            }
            assert programs == {program}, host

    def test_every_bundled_command_names_a_public_program(self) -> None:
        tables = _bundled_command_tables()
        assert len(tables) > 10, "too few tables read; this would pass on nothing"
        assert _non_public_programs(tables) == []

    def test_no_bundled_command_names_a_non_public_endpoint(self) -> None:
        offenders: list[str] = []
        for label, commands in sorted(_bundled_command_tables().items()):
            for argv in commands:
                for line, detail in _non_public_references(" ".join(argv)):
                    offenders.append(f"{label}: {detail} (line {line})")
        assert offenders == []

    @pytest.mark.parametrize(
        ("label", "argv"),
        [
            pytest.param(
                "WORKFLOW_PRESETS[acme-review].submit", ("acme-cr", "create"), id="review"
            ),
            pytest.param("FEEDBACK_PRESETS[acme].claimed", ("acme-tickets", "note"), id="tracker"),
            pytest.param("QUALITY_GATE_PRESETS[acme-lint]", ("acme-analyzer",), id="gate"),
        ],
    )
    def test_a_preset_for_an_organisation_system_is_reported(
        self, label: str, argv: tuple[str, ...]
    ) -> None:
        # The teeth of the three assertions above: a fourth workflow preset, a
        # third feedback host, or a fifth gate that shelled out to an internal
        # tool would be a non-public system name shipped in the package.
        assert _non_public_programs({label: (argv,)}) == [f"{label}: {argv[0]}"]

    def test_the_program_check_admits_the_bundled_public_tools(self) -> None:
        # The other half: a check that rejected everything would pass the case
        # above while making the real tables unmaintainable.
        admitted = {name: ((name, "--version"),) for name in sorted(PUBLIC_PROGRAMS)}
        assert _non_public_programs(admitted) == []


class TestDelegatedProvidersAreConfigurationOnly:
    """A delegated provider is referenced by configuration, never vendored here.

    The distinction this rests on, because it is the one that decides what the
    rule covers: a **delegated capability provider** answers one of
    :data:`DELEGABLE_CAPABILITIES` through the registry, under a contract, and is
    named by an operator's configuration. The ``mutation-probe`` quality gate also
    shells out — to :mod:`engine.mutation_probe`, which is in this tree — and it is
    **not** a vendored provider: it serves no capability, is registered nowhere in
    the registry, implements no provider contract, and its name is not a capability
    name. It is an engine builtin that happens to be invoked by command rather than
    imported, and the engine's own code being in the engine's own tree is the
    normal case. What the rule forbids is an *external provider's implementation*
    living here, so that a reader of the repository cannot tell configuration from
    code. Point a **capability binding** at that same in-tree module and it is
    reported, which is the case planted below.
    """

    def test_no_third_party_code_is_vendored_into_the_tree(self) -> None:
        # A vendored provider arrives either as a third-party import or as a
        # directory of foreign code. Shipped modules import stdlib and this host
        # package only, so either would show up here.
        offenders: list[str] = []
        for path in _app_modules():
            for name in _imported_modules(path):
                root = name.split(".")[0]
                if root in ("kiro_crew", "__future__") or root in _STDLIB_ROOTS:
                    continue
                offenders.append(f"{path.relative_to(APP_ROOT)}: {name}")
        assert offenders == [], f"third-party code reached from the app tree: {offenders}"
        assert not (APP_ROOT / "_vendor").exists(), "a vendor directory appeared in the app"

    def test_a_zero_configuration_tree_vendors_no_provider(self, tmp_path: Path) -> None:
        bindings = resolve_bindings(ConfigStore(tmp_path / "config"))
        assert set(bindings) == set(DELEGABLE_CAPABILITIES)
        assert _vendored_provider_offenders(bindings) == []

    @pytest.mark.parametrize("transport", TRANSPORTS)
    @pytest.mark.parametrize(
        "argv",
        [
            pytest.param((f"{APP_PACKAGE}.engine.local_analyzer",), id="dotted-module"),
            pytest.param(("python", "-m", f"{APP_PACKAGE}.engine.mutation_probe"), id="python-m"),
            pytest.param(("engine/local_analyzer.py",), id="relative-path"),
            pytest.param((str(APP_ROOT / "engine" / "local_analyzer.py"),), id="absolute-path"),
        ],
    )
    def test_an_in_tree_provider_is_reported_on_every_transport(
        self, transport: str, argv: tuple[str, ...]
    ) -> None:
        # Unconditional is the point. ``command`` is where an earlier version of
        # this rule leaked, and ``builtin`` is not an exemption either: it carries
        # no argv at all, so one holding a program is reported too.
        binding = Binding(capability="analysis", transport=transport, argv=argv)
        assert _vendored_provider_offenders(
            {"analysis": binding}
        ), f"a {transport} provider implemented in the tree went unreported: {argv}"

    @pytest.mark.parametrize("transport", [TRANSPORT_COMMAND, TRANSPORT_MCP])
    def test_an_in_tree_provider_is_reported_through_the_real_config_path(
        self, tmp_path: Path, transport: str
    ) -> None:
        # Not only a hand-built dataclass: the same binding written through the
        # config write path and resolved the way the engine resolves it.
        store = ConfigStore(tmp_path / "config")
        store.write(
            {
                "capabilities": {
                    "analysis": {
                        "transport": transport,
                        "command": ["python", "-m", f"{APP_PACKAGE}.engine.local_analyzer"],
                    }
                }
            },
            surface=DASHBOARD_SURFACE,
        )
        bindings = resolve_bindings(store)
        assert bindings["analysis"].transport == transport, "the binding did not land"
        assert _vendored_provider_offenders(bindings) == [
            f"analysis: {transport} provider in the tree "
            f"['{APP_PACKAGE}.engine.local_analyzer']"
        ]

    def test_an_operator_configured_external_program_is_not_reported(self, tmp_path: Path) -> None:
        # The other half: the whole point is that an operator MAY bind a provider
        # by configuration. A check that reported every external binding would
        # pass the cases above while forbidding the extension point.
        store = ConfigStore(tmp_path / "config")
        _bind_all(store, TRANSPORT_COMMAND, "enhanced-provider")
        bindings = resolve_bindings(store)
        assert {b.transport for b in bindings.values()} == {TRANSPORT_COMMAND}
        assert _vendored_provider_offenders(bindings) == []

    def test_a_gate_shelling_out_to_the_engine_is_not_a_delegated_provider(self) -> None:
        gate_argv = tuple(QUALITY_GATE_PRESETS["mutation-probe"]["commands"][0])
        # It really is in-tree code invoked by command — that is the case worth
        # drawing a line through rather than skipping.
        assert any(_in_tree_argument(argument) for argument in gate_argv)
        # And it is not a provider: no gate name is a capability name, so no gate
        # can be read as a binding for one.
        assert set(QUALITY_GATE_PRESETS).isdisjoint(DELEGABLE_CAPABILITIES)
        # Nothing binds it as a provider either, in the bundled defaults...
        assert (
            _vendored_provider_offenders(
                {
                    c: Binding(capability=c, transport=TRANSPORT_BUILTIN)
                    for c in DELEGABLE_CAPABILITIES
                }
            )
            == []
        )
        # ...and the same argv declared as a capability binding IS reported, which
        # is what keeps the distinction from being an escape hatch.
        as_binding = Binding(capability="analysis", transport=TRANSPORT_COMMAND, argv=gate_argv)
        assert _vendored_provider_offenders({"analysis": as_binding})

    def test_a_public_program_is_not_mistaken_for_in_tree_code(self) -> None:
        # ``make`` and ``gh`` are resolved through PATH by the OS. Treating a bare
        # program name as in-tree would report every bundled preset.
        for program in sorted(PUBLIC_PROGRAMS):
            assert not _in_tree_argument(program), program
        assert not _in_tree_argument("enhanced-provider")
        assert not _in_tree_argument("/usr/bin/env")


class TestShippedPromptTextIsAuthoredHere:
    """Prompt text lives in this tree, names nothing non-public, and is inventoried.

    **The part that is not mechanically checkable, said plainly:** that the words
    were *authored for this app* rather than adapted from non-public text is a
    judgement about provenance, and no test can make it. Pretending otherwise —
    a check that looks like it verifies authorship and actually verifies that a
    string is non-empty — would be worse than the gap, because it would retire the
    review that is the real control.

    So the mechanical part is scoped to what is observable, and the ledger is
    built to force the judgement rather than fake it:

    * every flow's text resolves from constants in this tree, with no file read
      and no environment lookup on the path;
    * the text carries no non-public reference, by the same rules as the tree scan;
    * :data:`PROMPT_SOURCES` is a closed inventory, and a newly added
      prompt-shaped constant fails until someone records it — which is exactly the
      moment a reviewer has to read the new text.

    **What a reviewer must look at** to discharge the judgement: the text behind
    every entry of :data:`PROMPT_SOURCES`, the flow constants in
    :mod:`engine_mcp.guidance`, and ``skills/spec-engine-discovery/SKILL.md``.
    :meth:`test_the_prompt_inventory_is_complete` keeps that list honest.
    """

    def test_every_flow_returns_text_the_app_holds_itself(self) -> None:
        texts = _prompt_texts()
        assert len(texts) == len(FLOWS) + len(PROMPT_SOURCES) + 1
        for where, text in sorted(texts.items()):
            assert text.strip(), f"{where} is empty"

    def test_no_prompt_text_is_read_from_outside_the_tree(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def refuse(*_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("prompt text was loaded at runtime")

        monkeypatch.setattr(builtins, "open", refuse)
        monkeypatch.setattr(Path, "read_text", refuse)
        monkeypatch.setattr(os, "getenv", refuse)
        texts = [get_guidance(flow) for flow in FLOWS]
        assert all(text.strip() for text in texts), "a flow returned nothing"

    def test_the_seal_over_prompt_loading_actually_trips(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Without this, a sealed test that stopped exercising anything would pass
        # forever.
        def refuse(*_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("read")

        monkeypatch.setattr(builtins, "open", refuse)
        monkeypatch.setattr(Path, "read_text", refuse)
        with pytest.raises(AssertionError):
            (APP_ROOT / "app.json").read_text(encoding="utf-8")

    def test_the_prompt_text_names_nothing_non_public(self) -> None:
        offenders: list[str] = []
        for where, text in sorted(_prompt_texts().items()):
            offenders.extend(f"{where}: {detail}" for _line, detail in _non_public_references(text))
        assert offenders == [], f"shipped prompt text names something non-public: {offenders}"

    def test_the_prompt_inventory_is_complete(self) -> None:
        found = _prompt_constants()
        assert found, "the heuristic found nothing; the inventory would be vacuous"
        assert found == sorted(PROMPT_SOURCES), (
            "the inventory of shipped prompt text no longer matches the tree: "
            f"found {found}, recorded {sorted(PROMPT_SOURCES)}. Read the new text, "
            "confirm it was authored for this app and names nothing non-public, "
            "then record it in PROMPT_SOURCES."
        )

    def test_the_inventory_heuristic_would_notice_new_prompt_text(self) -> None:
        # The forcing function, exercised: the heuristic is what makes the pin
        # above a gate rather than a restatement, so it is driven against an
        # unrecorded constant and against the two shapes it must not sweep up.
        prose = "x" * _PROMPT_MIN_CHARS
        module = ast.parse(f"REVIEW_PROMPT = {prose!r}\n")
        assert _prompt_named_constants(module) == ["REVIEW_PROMPT"]
        assert _prompt_named_constants(ast.parse(f"_SCHEMA_STATEMENTS = {prose!r}\n")) == []
        assert _prompt_named_constants(ast.parse('SEED_INSTRUCTION = "short"\n')) == []
