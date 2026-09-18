"""Integration: harnesses.json -> known + selectable + harness_for-resolvable.

The unit suites cover each seam in isolation. This one drives the whole boot-load
path end to end against a temp ``harnesses.json`` with a real (stub) executable on
PATH, so the three registration sides -- vocabulary, runtime, selection -- and the
invalid/unselectable diagnostics are exercised together, exactly as
``bootstrap_context`` runs them.

Every test restores the process-global registry state in a fixture (registered
ids, the operator register, the selectable pair, and the diagnostic maps), so one
test's boot-load cannot leak into another.
"""

from __future__ import annotations

import json
import os
import stat
import sys

import pytest

from kiro_crew.acp import harness as harness_pkg
from kiro_crew.acp.harness import DescriptorHarness, harness_for
from kiro_crew.acp.harness import operator_registry as reg
from kiro_crew.agent_sdk import backends as b


@pytest.fixture
def clean_boot(tmp_path, monkeypatch):
    """Snapshot/restore every registry surface the boot-load writes.

    Also pins ``KIROCREW_HOME`` to a per-test home, so the routing-attestation
    store the selectability gate reads (``backend-routing-attestations.json``
    under the crew home) is this test's own file and never the developer's.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir(exist_ok=True)
    baseline = set(b._baseline)
    selectable = set(b._selectable)
    yield
    b._reset_registered_backends()
    harness_pkg._reset_operator_register()
    reg._reset_operator_diagnostics()
    b._baseline.clear()
    b._baseline.update(baseline)
    b._selectable.clear()
    b._selectable.update(selectable)


def _stub_executable(tmp_path, name="my-acp"):
    """Create an executable stub on a directory placed on PATH.

    Portable: the stub is only ever RESOLVED, never run. POSIX resolution wants
    the execute bit; Windows has no execute bit and resolves PATH candidates
    through PATHEXT, so the file takes a ``.cmd`` suffix there and a bare
    ``name`` lookup finds it -- exactly the shape an operator's descriptor uses.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    exe = bindir / (f"{name}.cmd" if sys.platform == "win32" else name)
    exe.write_text("#!/bin/sh\nexec cat\n", encoding="utf-8")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return exe


def _write_harnesses(tmp_path, mapping, *, attest: bool = True) -> str:
    path = tmp_path / "harnesses.json"
    path.write_text(json.dumps(mapping), encoding="utf-8")
    if attest:
        _attest(mapping)
    return str(path)


def _attest(mapping) -> None:
    """Record a routing attestation for every valid, routed descriptor in *mapping*.

    The selectability gate needs the gateway's own end-to-end evidence
    (``routing_verification``); these suites test what happens AFTER that
    evidence exists, so the fixture supplies it the way a successful Verify would
    -- keyed to the descriptor's spawn fingerprint, under the test's crew home.
    Tests of the unverified state call ``_write_harnesses(..., attest=False)``.
    """
    from kiro_crew.acp.harness.descriptor import descriptor_from_mapping
    from kiro_crew.acp.harness.routing_verification import record_attestation

    for harness_id, raw in mapping.items():
        d, _reasons = descriptor_from_mapping(raw, harness_id=harness_id)
        if d is not None and d.selectable:
            record_attestation(d, mechanism=d.routing, evidence={"fixture": True})


def test_valid_agent_spec_becomes_known_selectable_and_resolvable(clean_boot, tmp_path):
    """The acceptance case: a routed descriptor is fully served after the load.

    Known (spellable), selectable (offered in the switch), on the runtime path,
    and resolvable through ``harness_for`` as a ``DescriptorHarness`` whose argv
    renders the stub executable.
    """
    exe = _stub_executable(tmp_path)
    path = _write_harnesses(
        tmp_path,
        {
            "my-acp": {
                "id": "my-acp",
                "display_name": "My ACP",
                "executable": str(exe),
                "argv": ["{executable}", "serve"],
                "agent_args": ["--agent", "{agent}"],
                "routing": "agent_spec",
            }
        },
    )

    reg.load_and_register_operator_descriptors(path=path)

    # Known + routed + labelled + own-namespaced.
    assert "my-acp" in b.ACP_BACKENDS_KNOWN
    assert b.routing_for("my-acp") is b.Routing.AGENT_SPEC
    assert b.provider_label_for("my-acp") == "My ACP"
    assert b.model_registry_namespace("my-acp") == "my-acp"
    # Selectable, and on the shared-runtime serving path (path A).
    assert "my-acp" in b.selectable_backends()
    assert "my-acp" in b.acp_runtime_backends()
    # Resolvable through the one function every caller uses.
    harness = harness_for("my-acp")
    assert isinstance(harness, DescriptorHarness)
    assert harness.backend == "my-acp"
    # No diagnostic rows for a clean, routed descriptor.
    assert "my-acp" not in reg.invalid_operator_harnesses()
    assert "my-acp" not in reg.unselectable_operator_harnesses()


def test_a_registered_backend_is_a_member_of_no_session_path_capability_set(clean_boot, tmp_path):
    """Harness-parity H6/H7: capability membership is a code-reviewed literal.

    A descriptor cannot claim its way into a session-path capability: after a
    successful boot-load the registered id is KNOWN, RUNTIME and SELECTABLE, and a
    member of none of the frozen ``ACP_BACKENDS_*`` capability sets -- so every
    gate that reads one takes the default branch for it. The second half pins
    the reading side: the client's gates spell the frozen set, not a derived
    accessor a registration could grow.
    """
    import inspect

    from kiro_crew.acp import client as client_mod

    exe = _stub_executable(tmp_path)
    path = _write_harnesses(
        tmp_path,
        {
            "my-acp": {
                "executable": str(exe),
                "argv": ["{executable}"],
                "agent_args": ["--agent", "{agent}"],
                "routing": "agent_spec",
            }
        },
    )
    reg.load_and_register_operator_descriptors(path=path)
    assert "my-acp" in b.ACP_BACKENDS_KNOWN
    assert "my-acp" in b.acp_runtime_backends()

    for frozen in (
        b.ACP_BACKENDS_SESSION_MCP_ARRAY,
        b.ACP_BACKENDS_HARNESS_OWNED_SESSIONS,
        b.ACP_BACKENDS_LOAD_WITHOUT_MODES,
        b.ACP_BACKENDS_MODEL_VIA_CONFIG_OPTION,
        b.ACP_BACKENDS_ADVERTISED_MODEL_SELECTION,
    ):
        assert "my-acp" not in frozen
        assert isinstance(frozen, frozenset)

    body = inspect.getsource(client_mod)
    assert "self.backend in ACP_BACKENDS_ADVERTISED_MODEL_SELECTION" in body
    assert "self.backend not in ACP_BACKENDS_SESSION_MCP_ARRAY" in body
    assert "self.backend in ACP_BACKENDS_HARNESS_OWNED_SESSIONS" in body
    assert "self.backend in ACP_BACKENDS_LOAD_WITHOUT_MODES" in body
    # No derived capability accessor exists for a registration to grow.
    for name in (
        "session_mcp_array_backends",
        "harness_owned_sessions_backends",
        "load_without_modes_backends",
        "model_via_config_option_backends",
        "advertised_model_selection_backends",
    ):
        assert not hasattr(b, name), name


def test_a_registered_operator_backend_takes_the_acp_runtime_start_path(clean_boot, tmp_path):
    """The start-path gate must see a config-authored runtime id.

    ``AcpProvider.start`` branches on ``is_acp_runtime_backend``: True spawns an
    ``AcpRuntime`` (the only path that resolves ``harness_for`` and so the only
    path a ``DescriptorHarness`` exists on); False takes the direct ``AcpClient``
    path, whose spawn ladder has no descriptor arm and ends in the kiro-cli
    branch. Read off the frozen vocabulary alone, an operator backend answers
    False and a chat pinned to it silently runs kiro-cli under the operator's
    label. So the gate must read the derived set that includes registrations.
    """
    from unittest.mock import MagicMock, patch

    from kiro_crew.providers.acp import AcpProvider

    exe = _stub_executable(tmp_path)
    path = _write_harnesses(
        tmp_path,
        {
            "my-acp": {
                "id": "my-acp",
                "display_name": "My ACP",
                "executable": str(exe),
                "argv": ["{executable}", "serve"],
                "agent_args": ["--agent", "{agent}"],
                "routing": "agent_spec",
            }
        },
    )
    reg.load_and_register_operator_descriptors(path=path)
    assert "my-acp" in b.acp_runtime_backends()

    with patch("kiro_crew.providers.acp.AcpClient"):
        provider = AcpProvider(acp_backend="my-acp")
    provider._client = MagicMock()
    provider._client.backend = "my-acp"
    assert (
        provider.is_acp_runtime_backend is True
    ), "operator backend fell off the AcpRuntime path: its chats would spawn kiro-cli"
    # And the builtin answers are unchanged by the registration.
    for builtin, expected in ((b.ACP_BACKEND_KIRO, True), (b.ACP_BACKEND_CLAUDE, False)):
        provider._client.backend = builtin
        assert provider.is_acp_runtime_backend is expected


@pytest.mark.asyncio
async def test_api_models_serves_an_operator_backends_own_static_catalog(
    clean_boot, tmp_path, monkeypatch
):
    """GET /api/models?backend=<operator id> answers with the descriptor's OWN
    declared models, never kiro-cli's --list-models catalog.

    A static operator backend that fell through to the kiro branch would offer
    kiro model ids its harness rejects; this pins the operator branch instead.
    """
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from chat_test_helpers import _make_state

    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.dashboard.handlers import agents as agents_mod

    exe = _stub_executable(tmp_path)
    path = _write_harnesses(
        tmp_path,
        {
            "my-acp": {
                "id": "my-acp",
                "display_name": "My ACP",
                "executable": str(exe),
                "argv": ["{executable}", "serve"],
                "agent_args": ["--agent", "{agent}"],
                "routing": "agent_spec",
                "model_source": "static",
                "models": ["my-large", "my-mini"],
            }
        },
    )
    reg.load_and_register_operator_descriptors(path=path)
    assert "my-acp" in b.selectable_backends()

    cfg = KiroCrewConfig()
    cfg.agent.acp_backend = ""  # global = kiro; the query alone re-keys
    monkeypatch.setattr(
        agents_mod, "KiroCrewConfig", type("C", (), {"load": staticmethod(lambda: cfg)})
    )

    # A tripwire: if the branch fell through to the kiro catalog this would fire.
    async def _boom(request):
        raise AssertionError("fell through to the kiro --list-models branch")

    monkeypatch.setattr(agents_mod, "reject_if_kiro_unverified", _boom)

    app = web.Application()
    app["state"] = _make_state(tmp_path)
    app.router.add_get("/api/models", agents_mod.api_models)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/api/models?backend=my-acp")
        assert resp.status == 200, await resp.text()
        names = [row["model_name"] for row in await resp.json()]
    assert names == ["auto", "my-large", "my-mini"]


@pytest.mark.asyncio
async def test_the_resolved_harness_spawns_the_stub_executable(clean_boot, tmp_path, monkeypatch):
    """End to end: the registered harness renders an argv naming the stub binary.

    The stub is on PATH, so the generic resolver finds it and render_argv puts its
    resolved path first -- proving the descriptor's executable actually drives a
    spawn plan, not just a listing row. Runs on Windows too: the fixture carries
    a ``.cmd`` suffix there, so the bare ``my-acp`` lookup resolves through
    PATHEXT to the same file ``exe`` names.
    """
    from pathlib import Path

    from kiro_crew.acp.harness.base import SpawnContext

    exe = _stub_executable(tmp_path)
    monkeypatch.setenv("PATH", str(exe.parent) + os.pathsep + os.environ.get("PATH", ""))
    path = _write_harnesses(
        tmp_path,
        {
            "my-acp": {
                "executable": "my-acp",
                "argv": ["{executable}", "serve"],
                "agent_args": ["--agent", "{agent}"],
                "routing": "agent_spec",
            }
        },
    )
    reg.load_and_register_operator_descriptors(path=path)

    ctx = SpawnContext(
        agent="a", work_dir=str(tmp_path), model=None, environ={}, home=Path(tmp_path)
    )
    plan = await harness_for("my-acp").resolve_spawn(ctx)
    # realpath + normcase: Windows may restore on-disk casing and PATHEXT may
    # append the suffix; both name the same file ``exe`` is.
    assert os.path.normcase(os.path.realpath(plan.argv[0])) == os.path.normcase(
        os.path.realpath(str(exe))
    )
    # The agent_args block renders after argv, carrying the selected agent: this
    # is the selection the later activation check confirms.
    assert plan.argv[1:] == ["serve", "--agent", "a"]
    # The spawn mask is keyed on ROUTING: an agent_spec descriptor gets an empty
    # mask (byte-identical argv), the same answer codex.py gives.
    assert plan.extra_hidden_dirs == ()


def test_unroutable_descriptor_is_known_but_not_selectable(clean_boot, tmp_path):
    """A descriptor with no routing registers as known-but-unselectable, with a reason."""
    exe = _stub_executable(tmp_path, name="no-route")
    path = _write_harnesses(
        tmp_path,
        {
            "no-route": {
                "executable": str(exe),
                "argv": ["{executable}"],
                # routing omitted -> valid but unselectable
            }
        },
    )
    reg.load_and_register_operator_descriptors(path=path)

    assert "no-route" in b.ACP_BACKENDS_KNOWN  # spellable + nameable
    assert "no-route" not in b.selectable_backends()  # not offered
    assert "no-route" in reg.unselectable_operator_harnesses()
    assert "no-route" not in reg.invalid_operator_harnesses()
    # It IS resolvable as a harness (known + on the runtime set), just not selectable.
    assert isinstance(harness_for("no-route"), DescriptorHarness)


def test_malformed_descriptor_lands_in_invalid_and_costs_only_its_row(clean_boot, tmp_path):
    """A malformed entry is recorded in invalid() and never registered; siblings survive."""
    exe = _stub_executable(tmp_path)
    path = _write_harnesses(
        tmp_path,
        {
            "my-acp": {
                "executable": str(exe),
                "argv": ["{executable}", "serve"],
                "agent_args": ["--agent", "{agent}"],
                "routing": "agent_spec",
            },
            "bad-one": {
                # argv[0] is not {executable} -> validation failure
                "executable": "x",
                "argv": ["x", "run"],
                "agent_args": ["--agent", "{agent}"],
                "routing": "agent_spec",
            },
        },
    )
    reg.load_and_register_operator_descriptors(path=path)

    # The good one is fully served.
    assert "my-acp" in b.selectable_backends()
    assert isinstance(harness_for("my-acp"), DescriptorHarness)
    # The bad one costs only its own row: recorded invalid, registered nowhere.
    assert "bad-one" in reg.invalid_operator_harnesses()
    assert "bad-one" not in b.ACP_BACKENDS_KNOWN
    with pytest.raises(ValueError, match="no ACP harness"):
        harness_for("bad-one")


def test_session_config_descriptor_records_permission_config(clean_boot, tmp_path):
    """A session_config descriptor is enforced: its (option, value) is recorded, selectable."""
    exe = _stub_executable(tmp_path, name="wire-host")
    path = _write_harnesses(
        tmp_path,
        {
            "wire-host": {
                "executable": str(exe),
                "argv": ["{executable}"],
                "routing": "session_config",
                "permission_config": {"option": "mode", "value": "read-only"},
            }
        },
    )
    reg.load_and_register_operator_descriptors(path=path)

    assert b.routing_for("wire-host") is b.Routing.SESSION_CONFIG
    assert b.permission_config_for("wire-host") == ("mode", "read-only")
    assert "wire-host" in b.selectable_backends()


def test_a_missing_file_registers_nothing(clean_boot, tmp_path):
    """No harnesses.json is the normal case: nothing registered, no diagnostics."""
    known_before = set(b.ACP_BACKENDS_KNOWN)
    reg.load_and_register_operator_descriptors(path=str(tmp_path / "absent.json"))
    assert set(b.ACP_BACKENDS_KNOWN) == known_before
    assert reg.invalid_operator_harnesses() == {}


def test_the_load_is_idempotent(clean_boot, tmp_path):
    """A second boot-load pass is a no-op, not a re-registration crash.

    bootstrap_context can run twice in one process; re-registering a known id
    raises, so the second pass must skip an already-registered id.
    """
    exe = _stub_executable(tmp_path)
    path = _write_harnesses(
        tmp_path,
        {
            "my-acp": {
                "executable": str(exe),
                "argv": ["{executable}"],
                "agent_args": ["--agent", "{agent}"],
                "routing": "agent_spec",
            }
        },
    )
    reg.load_and_register_operator_descriptors(path=path)
    # Must not raise on the second pass.
    reg.load_and_register_operator_descriptors(path=path)
    assert "my-acp" in b.selectable_backends()


def test_a_descriptor_colliding_with_a_builtin_id_is_reported_not_dropped(clean_boot, tmp_path):
    """A descriptor named ``claude`` must land in ``invalid``, never vanish.

    The idempotency skip keys on the OPERATOR register (what this loader wrote),
    not on ``ACP_BACKENDS_KNOWN`` -- every builtin is in the known set from module
    import, so a known-set skip would read a first-pass collision as "already
    registered" and drop it with no diagnosable row. The registrar refuses the
    collision and the refusal is recorded with its reason.
    """
    exe = _stub_executable(tmp_path)
    path = _write_harnesses(
        tmp_path,
        {
            b.ACP_BACKEND_CLAUDE: {
                "executable": str(exe),
                "argv": ["{executable}"],
                "agent_args": ["--agent", "{agent}"],
                "routing": "agent_spec",
            },
            "my-acp": {
                "executable": str(exe),
                "argv": ["{executable}"],
                "agent_args": ["--agent", "{agent}"],
                "routing": "agent_spec",
            },
        },
    )
    reg.load_and_register_operator_descriptors(path=path)

    invalid = reg.invalid_operator_harnesses()
    assert b.ACP_BACKEND_CLAUDE in invalid, invalid
    assert any("already a known backend" in r for r in invalid[b.ACP_BACKEND_CLAUDE]), invalid
    # The builtin's own registration is untouched, and the sibling still served.
    assert b.ACP_BACKEND_CLAUDE not in reg.unselectable_operator_harnesses()
    assert "my-acp" in b.selectable_backends()
    # Second pass stays a no-op for the sibling and stays reported for the collision.
    reg.load_and_register_operator_descriptors(path=path)
    assert b.ACP_BACKEND_CLAUDE in reg.invalid_operator_harnesses()


def test_a_registered_backends_sessions_persist_under_its_own_label(clean_boot, tmp_path):
    """H11 on the data path: ``provider_label`` must answer a registered id's label.

    The label is the key three things share -- the session-map persistence value,
    ``detect_provider_switch``, and cleanup routing. Resolved off the builtin
    mapping alone, an operator backend falls to the DEFAULT (kiro's label): its
    session is persisted as kiro, resume-checked as kiro, and pruned by the map
    for want of a kiro transcript. The registered label recorded at
    ``register_known_backend`` must be what the persistence path reads.
    """
    from unittest.mock import MagicMock

    from kiro_crew.acp.session_provider import AcpSessionProvider
    from kiro_crew.acp.types import PROVIDER_LABEL_BY_BACKEND, PROVIDER_LABEL_DEFAULT
    from kiro_crew.providers import acp as providers_acp

    exe = _stub_executable(tmp_path)
    path = _write_harnesses(
        tmp_path,
        {
            "my-acp": {
                "display_name": "Acme Agent",
                "executable": str(exe),
                "argv": ["{executable}"],
                "agent_args": ["--agent", "{agent}"],
                "routing": "agent_spec",
            }
        },
    )
    reg.load_and_register_operator_descriptors(path=path)

    runtime = MagicMock()
    runtime.acp_backend = "my-acp"
    provider = AcpSessionProvider(MagicMock(), runtime)
    label = providers_acp.provider_label(provider)
    assert label == "Acme Agent"
    assert label != PROVIDER_LABEL_DEFAULT
    assert label not in PROVIDER_LABEL_BY_BACKEND.values()
    # The session map leaves a non-default label's sid alone (no kiro-transcript
    # stat), which is the behaviour the label exists to select.
    from kiro_crew.session_map import PROVIDER_LABEL_DEFAULT as map_default

    assert label != map_default


def test_a_descriptor_whose_label_is_taken_is_reported_invalid(clean_boot, tmp_path):
    """A label is a persistence key, so two backends may not share one.

    ``display_name`` equal to a builtin's label (here kiro's DEFAULT, ``acp``)
    would file the operator backend's sessions as kiro sessions and get them
    pruned; equal to a sibling operator's label, the two would be indistinguishable
    to resume and cleanup. Both are refused with a reason naming the owner, and
    the sibling with a free label is still served.
    """
    from kiro_crew.acp.types import PROVIDER_LABEL_DEFAULT

    exe = _stub_executable(tmp_path)
    path = _write_harnesses(
        tmp_path,
        {
            "a-acp": {
                "display_name": PROVIDER_LABEL_DEFAULT,
                "executable": str(exe),
                "argv": ["{executable}"],
                "agent_args": ["--agent", "{agent}"],
                "routing": "agent_spec",
            },
            "b-acp": {
                "display_name": "Shared Name",
                "executable": str(exe),
                "argv": ["{executable}"],
                "agent_args": ["--agent", "{agent}"],
                "routing": "agent_spec",
            },
            "c-acp": {
                "display_name": "Shared Name",
                "executable": str(exe),
                "argv": ["{executable}"],
                "agent_args": ["--agent", "{agent}"],
                "routing": "agent_spec",
            },
        },
    )
    reg.load_and_register_operator_descriptors(path=path)

    invalid = reg.invalid_operator_harnesses()
    assert "a-acp" in invalid, invalid
    assert any("already the provider label of backend ''" in r for r in invalid["a-acp"]), invalid
    assert "a-acp" not in b.ACP_BACKENDS_KNOWN
    # Sorted load order: b-acp claims the label, c-acp collides with it.
    assert "b-acp" in b.selectable_backends()
    assert "c-acp" in invalid, invalid
    assert any("backend 'b-acp'" in r for r in invalid["c-acp"]), invalid
    assert "c-acp" not in b.ACP_BACKENDS_KNOWN


def test_full_teardown_restores_the_builtin_state(tmp_path):
    """After reset, the registry is back to builtins with no operator harness.

    Does its own before/after bookkeeping rather than using the fixture, to prove
    the resets -- not the fixture -- are what clean up.
    """
    baseline_before = set(b._baseline)
    selectable_before = set(b._selectable)
    exe = _stub_executable(tmp_path)
    path = _write_harnesses(
        tmp_path,
        {
            "my-acp": {
                "executable": str(exe),
                "argv": ["{executable}"],
                "agent_args": ["--agent", "{agent}"],
                "routing": "agent_spec",
            }
        },
    )
    try:
        reg.load_and_register_operator_descriptors(path=path)
        assert "my-acp" in b.ACP_BACKENDS_KNOWN

        b._reset_registered_backends()
        harness_pkg._reset_operator_register()
        reg._reset_operator_diagnostics()

        assert "my-acp" not in b.ACP_BACKENDS_KNOWN
        assert "my-acp" not in b.acp_runtime_backends()
        with pytest.raises(ValueError, match="no ACP harness"):
            harness_for("my-acp")
        assert reg.invalid_operator_harnesses() == {}
        assert reg.unselectable_operator_harnesses() == {}
    finally:
        b._selectable.clear()
        b._selectable.update(selectable_before)
        b._baseline.clear()
        b._baseline.update(baseline_before)


def test_the_provider_registry_seam_is_the_one_place_operator_descriptors_load(
    clean_boot, tmp_path, monkeypatch
):
    """Harness-parity H13: operator descriptors enter through
    ``ProviderRegistry.register_acp_backends``, not a second bootstrap call.

    Two halves. The Default seam, invoked with a temp crew home holding a
    ``harnesses.json``, registers the descriptor (known + runtime + selectable);
    and ``bootstrap_context``'s source carries exactly ONE registration call --
    the seam -- and no direct call to the descriptor loader, so an edition that
    overrides the seam is the only thing deciding what gets registered.
    """
    import inspect

    from kiro_crew.platform import bootstrap as boot_mod
    from kiro_crew.platform.defaults import DefaultProviderRegistry

    exe = _stub_executable(tmp_path)
    # ``clean_boot`` pinned KIROCREW_HOME to tmp_path/home; the seam reads the
    # descriptor file from there with no explicit path.
    mapping = {
        "my-acp": {
            "executable": str(exe),
            "argv": ["{executable}"],
            "agent_args": ["--agent", "{agent}"],
            "routing": "agent_spec",
        }
    }
    (tmp_path / "home" / "harnesses.json").write_text(json.dumps(mapping), encoding="utf-8")
    _attest(mapping)

    DefaultProviderRegistry().register_acp_backends()

    assert "my-acp" in b.ACP_BACKENDS_KNOWN
    assert isinstance(harness_for("my-acp"), DescriptorHarness)
    assert "my-acp" in b.selectable_backends()

    src = inspect.getsource(boot_mod.bootstrap_context)
    assert src.count("register_acp_backends()") == 1
    assert "load_and_register_operator_descriptors" not in src
