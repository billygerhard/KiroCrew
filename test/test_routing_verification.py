"""End-to-end routing verification for descriptor backends.

The threat the gate answers: a descriptor DECLARES a routing, and a host can
honour the declared mechanism (load the agent, accept the config option) while
never sending ``session/request_permission`` -- every tool call it made would
bypass PreToolUse, the deny rules and the audit. So a declared routing makes a
descriptor known and runnable-for-verification, never selectable; only an
attestation this gateway recorded after observing the host ask (and honour a
refusal) does -- bound to the descriptor's spawn shape AND to the bytes of the
executable that was verified, and re-checked against the bytes about to run on
every spawn. These tests pin the store, the gate, the probe's verdicts, and the
two ways the probe must not be fooled (a provider for another backend; a binary
swapped under an unchanged path).
"""

from __future__ import annotations

import json
import os
import stat
import sys

import pytest

from kiro_crew.acp import harness as harness_pkg
from kiro_crew.acp.harness import operator_registry as reg
from kiro_crew.acp.harness.descriptor import HarnessDescriptor, PermissionConfig
from kiro_crew.acp.harness.routing_verification import (
    PROBE_FILE_NAME,
    UNVERIFIED_REASON,
    VERDICT_INCONCLUSIVE,
    VERDICT_VERIFIED,
    VERDICT_VIOLATION,
    descriptor_fingerprint,
    executable_digest,
    is_attested,
    load_attestations,
    record_attestation,
    revoke_attestation,
    spawn_attestation_problem,
    verify_routing,
)
from kiro_crew.agent_sdk import backends as b
from kiro_crew.providers.base import (
    EVENT_COMPLETE,
    EVENT_PERMISSION_REQUEST,
    EVENT_TEXT_CHUNK,
    LLMEvent,
)


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    return tmp_path / "home"


@pytest.fixture
def clean_registry():
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


def _stub(tmp_path, name="acme", body="#!/bin/sh\nexec cat\n") -> str:
    """A real, readable, executable file: the attestation binds to its bytes.

    Resolves on every platform without being run: POSIX wants the execute bit;
    Windows has no execute bit and accepts a known runnable suffix (``.cmd``).
    """
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    exe = bindir / (f"{name}.cmd" if sys.platform == "win32" else name)
    exe.write_text(body, encoding="utf-8")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return str(exe)


def _descriptor(exe: str, **overrides) -> HarnessDescriptor:
    base = dict(
        id="acme",
        display_name="Acme",
        executable=exe,
        argv=("{executable}", "acp"),
        agent_args=("--agent", "{agent}"),
        routing="agent_spec",
    )
    base.update(overrides)
    return HarnessDescriptor(**base)


# ── Fingerprint ──


def test_fingerprint_covers_every_spawn_shaping_field_and_nothing_else(tmp_path):
    exe = _stub(tmp_path)
    d = _descriptor(exe)
    fp = descriptor_fingerprint(d)
    assert fp == descriptor_fingerprint(_descriptor(exe))
    # Cosmetic fields do not move it: a rename or a static-catalog edit must not
    # revoke an attestation, because neither changes what runs or how it routes.
    assert descriptor_fingerprint(_descriptor(exe, display_name="Renamed")) == fp
    assert descriptor_fingerprint(_descriptor(exe, models=("m1",))) == fp
    # Every spawn-shaping field does.
    assert descriptor_fingerprint(_descriptor("/opt/other")) != fp
    assert descriptor_fingerprint(_descriptor(exe, argv=("{executable}", "serve"))) != fp
    assert descriptor_fingerprint(_descriptor(exe, agent_args=("--profile", "{agent}"))) != fp
    assert descriptor_fingerprint(_descriptor(exe, model_args=("--model", "{model}"))) != fp
    assert descriptor_fingerprint(_descriptor(exe, mcp_delivery="session_array")) != fp
    assert (
        descriptor_fingerprint(
            _descriptor(
                exe,
                agent_args=(),
                routing="session_config",
                permission_config=PermissionConfig("mode", "ask"),
            )
        )
        != fp
    )


# ── Store ──


def test_store_binds_fingerprint_and_executable_bytes(home, tmp_path):
    exe = _stub(tmp_path)
    d = _descriptor(exe)
    assert load_attestations() == {}
    assert is_attested(d) is False
    rec = record_attestation(d, mechanism="agent_spec", evidence={"permission_requests": 1})
    assert rec["fingerprint"] == descriptor_fingerprint(d)
    assert rec["executable_path"] == exe
    assert rec["executable_digest"] == executable_digest(exe)
    assert is_attested(d) is True
    # A different spawn shape under the same id is NOT attested: the edit revoked it.
    assert is_attested(_descriptor(exe, argv=("{executable}", "serve"))) is False
    # The binary's BYTES replaced under the same path: not attested either.
    with open(exe, "w", encoding="utf-8") as fh:
        fh.write("#!/bin/sh\nexec evil\n")
    assert is_attested(d) is False
    # The file is gateway-owned JSON beside config.json.
    on_disk = json.loads((home / "backend-routing-attestations.json").read_text("utf-8"))
    assert on_disk["acme"]["mechanism"] == "agent_spec"
    assert revoke_attestation("acme") is True
    assert revoke_attestation("acme") is False


def test_recording_refuses_an_executable_it_cannot_bind_to(home):
    with pytest.raises(ValueError):
        record_attestation(_descriptor("/opt/does-not-exist"), mechanism="agent_spec", evidence={})
    assert load_attestations() == {}


def test_unreadable_store_fails_closed(home, tmp_path):
    (home / "backend-routing-attestations.json").write_text("not json", encoding="utf-8")
    assert load_attestations() == {}
    assert is_attested(_descriptor(_stub(tmp_path))) is False


# ── Spawn-path re-validation ──


def test_spawn_check_refuses_a_replaced_binary_and_allows_the_probe(home, tmp_path):
    exe = _stub(tmp_path)
    d = _descriptor(exe)
    assert (
        spawn_attestation_problem(d, exe) == "no routing attestation is recorded for this backend"
    )
    record_attestation(d, mechanism="agent_spec", evidence={})
    assert spawn_attestation_problem(d, exe) is None
    with open(exe, "w", encoding="utf-8") as fh:
        fh.write("#!/bin/sh\nexec evil\n")
    problem = spawn_attestation_problem(d, exe)
    assert problem is not None and "changed since its routing was verified" in problem
    # A descriptor edit is refused too, and named as such.
    edited = _descriptor(exe, argv=("{executable}", "serve"))
    assert "descriptor changed" in (spawn_attestation_problem(edited, exe) or "")
    # The probe's own spawn -- the one that produces the first attestation -- needs
    # no record while, and only while, the probe holds the marker; but it is held
    # to the bytes the probe resolved, so a swap before exec is refused too.
    from kiro_crew.acp.harness import routing_verification as rv

    rv._PROBING["acme"] = rv.executable_digest(exe)
    try:
        assert spawn_attestation_problem(_descriptor(exe), exe) is None
        with open(exe, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\nexec swapped-mid-probe\n")
        problem = spawn_attestation_problem(_descriptor(exe), exe)
        assert problem is not None and "between the routing probe's resolution" in problem
    finally:
        rv._PROBING.pop("acme", None)


# ── Boot gate ──


def _write(home, mapping):
    p = home / "harnesses.json"
    p.write_text(json.dumps(mapping), encoding="utf-8")
    return str(p)


def _routed(exe):
    return {
        "acme": {
            "executable": exe,
            "argv": ["{executable}", "acp"],
            "agent_args": ["--agent", "{agent}"],
            "routing": "agent_spec",
        }
    }


def _attest_mapping(mapping):
    from kiro_crew.acp.harness.descriptor import descriptor_from_mapping

    for hid, raw in mapping.items():
        d, _ = descriptor_from_mapping(raw, harness_id=hid)
        assert d is not None
        record_attestation(d, mechanism=d.routing, evidence={})


def test_a_routed_descriptor_is_known_but_unselectable_until_attested(
    home, clean_registry, tmp_path
):
    exe = _stub(tmp_path)
    reg.load_and_register_operator_descriptors(path=_write(home, _routed(exe)))
    assert "acme" in b.ACP_BACKENDS_KNOWN
    assert "acme" in b.acp_runtime_backends()
    assert "acme" not in b.selectable_backends()
    assert reg.unselectable_operator_harnesses()["acme"] == UNVERIFIED_REASON
    assert "acme" in reg.unverified_operator_harnesses()
    # An unroutable descriptor is unselectable too, but NOT verifiable: there is
    # nothing to verify.
    reg._reset_operator_diagnostics()
    b._reset_registered_backends()
    harness_pkg._reset_operator_register()
    no_route = {"acme": {"executable": exe, "argv": ["{executable}"]}}
    reg.load_and_register_operator_descriptors(path=_write(home, no_route))
    assert "acme" in reg.unselectable_operator_harnesses()
    assert "acme" not in reg.unverified_operator_harnesses()


def test_an_attested_descriptor_is_selectable_at_boot(home, clean_registry, tmp_path):
    mapping = _routed(_stub(tmp_path))
    _attest_mapping(mapping)
    reg.load_and_register_operator_descriptors(path=_write(home, mapping))
    assert "acme" in b.selectable_backends()
    assert "acme" not in reg.unselectable_operator_harnesses()
    assert "acme" not in reg.unverified_operator_harnesses()


def test_editing_the_spawn_shape_or_the_binary_revokes_selectability_at_boot(
    home, clean_registry, tmp_path
):
    exe = _stub(tmp_path)
    mapping = _routed(exe)
    _attest_mapping(mapping)
    edited = {"acme": dict(mapping["acme"], argv=["{executable}", "acp", "--yolo"])}
    reg.load_and_register_operator_descriptors(path=_write(home, edited))
    assert "acme" not in b.selectable_backends()
    assert reg.unselectable_operator_harnesses()["acme"] == UNVERIFIED_REASON
    # Same descriptor, replaced binary.
    reg._reset_operator_diagnostics()
    b._reset_registered_backends()
    harness_pkg._reset_operator_register()
    with open(exe, "w", encoding="utf-8") as fh:
        fh.write("#!/bin/sh\nexec evil\n")
    reg.load_and_register_operator_descriptors(path=_write(home, mapping))
    assert "acme" not in b.selectable_backends()
    assert "acme" in reg.unverified_operator_harnesses()


def test_mark_and_revoke_routing_verification_live(home, clean_registry, tmp_path):
    exe = _stub(tmp_path)
    reg.load_and_register_operator_descriptors(path=_write(home, _routed(exe)))
    d = reg.registered_operator_descriptor("acme")
    assert d is not None
    with pytest.raises(ValueError):
        reg.mark_routing_verified("acme", {"fingerprint": "not-this-descriptor"})
    with pytest.raises(ValueError):
        reg.mark_routing_verified("not-registered", {})
    rec = record_attestation(d, mechanism="agent_spec", evidence={})
    assert reg.mark_routing_verified("acme", rec) is True
    assert "acme" in b.selectable_backends()
    assert "acme" not in reg.unverified_operator_harnesses()
    # Revocation (what the spawn path does on a digest mismatch) withdraws the
    # grant everywhere: registry, attestation file, diagnostics.
    reg.revoke_routing_verification("acme", "the executable changed")
    assert "acme" not in b.selectable_backends()
    assert "acme" not in b._baseline
    assert load_attestations() == {}
    assert "acme" in reg.unverified_operator_harnesses()
    assert reg.unselectable_operator_harnesses()["acme"].startswith("the executable changed; ")
    with pytest.raises(ValueError):
        reg.revoke_routing_verification("claude", "x")  # a builtin has no grant to revoke
    with pytest.raises(ValueError):
        b.unregister_selectable_backend("claude")


def test_live_verification_reapplies_the_agent_backend_policy(
    home, clean_registry, tmp_path, monkeypatch
):
    """An administrator's agent_backend denial holds for a backend the owner
    verifies AFTER boot: the narrowing that ran at boot never saw the id, so the
    live path re-runs it, and a denied id is verified-but-not-selectable, with
    the policy named on its row and no Verify offered (verification is not what
    it lacks)."""
    import kiro_crew.agent_backend_governance as gov

    exe = _stub(tmp_path)
    reg.load_and_register_operator_descriptors(path=_write(home, _routed(exe)))
    d = reg.registered_operator_descriptor("acme")
    assert d is not None
    monkeypatch.setattr(gov, "_scope_permits", lambda backend: backend != "acme")
    rec = record_attestation(d, mechanism="agent_spec", evidence={})
    assert reg.mark_routing_verified("acme", rec) is False
    assert "acme" not in b.selectable_backends()
    assert "acme" not in reg.unverified_operator_harnesses()
    assert reg.unselectable_operator_harnesses()["acme"] == reg.POLICY_DENIED_REASON


# ── The probe ──


class _FakeProvider:
    """A provider whose one turn replays a scripted event list.

    ``writes_probe`` makes the fake behave like a host that executes the write
    regardless of the answer (the violation the probe exists to catch).
    ``backend`` is the identity the provider reports; the probe must refuse to
    attest when it is not the descriptor's own.
    """

    def __init__(self, events, *, cwd, backend="acme", writes_probe=False, raise_on_stream=None):
        self._events = events
        self._cwd = cwd
        self.acp_backend = backend
        self._writes_probe = writes_probe
        self._raise = raise_on_stream
        self.rejected: list = []
        self.started = False
        self.shut_down = False

    async def start(self):
        self.started = True

    async def stream(self, message):
        if self._raise is not None:
            raise self._raise
        for ev in self._events:
            if self._writes_probe and ev.kind == EVENT_COMPLETE:
                with open(os.path.join(self._cwd, PROBE_FILE_NAME), "w", encoding="utf-8") as fh:
                    fh.write("probe")
            yield ev

    async def reject_tool(self, request_id):
        self.rejected.append(request_id)

    async def shutdown(self):
        self.shut_down = True


def _builder(events, **kw):
    made = {}

    def build(session_key, cwd):
        p = _FakeProvider(events, cwd=cwd, **kw)
        made["provider"] = p
        made["cwd"] = cwd
        made["session_key"] = session_key
        return p

    return build, made


def _perm(rid):
    return LLMEvent(kind=EVENT_PERMISSION_REQUEST, request_id=rid)


@pytest.mark.asyncio
async def test_probe_verifies_a_host_that_asks_and_honours_the_refusal(tmp_path):
    build, made = _builder(
        [_perm("r1"), LLMEvent(kind=EVENT_TEXT_CHUNK, text="denied"), LLMEvent(kind=EVENT_COMPLETE)]
    )
    result = await verify_routing(_descriptor(_stub(tmp_path)), build, timeout=5)
    assert result.verdict == VERDICT_VERIFIED
    assert result.permission_requests == 1
    assert made["session_key"] == "backend-routing-probe:acme"
    assert made["provider"].rejected == ["r1"]  # never granted
    assert made["provider"].shut_down is True
    assert not os.path.isdir(made["cwd"])  # scratch removed


@pytest.mark.asyncio
async def test_probe_refuses_to_attest_from_a_provider_for_another_backend(tmp_path):
    # The failure GPT named: an unselectable descriptor put through the per-chat
    # selection gate degrades to the configured default, which then asks for
    # permission -- and the wrong backend would be attested. The probe asserts
    # the provider's identity before any verdict.
    build, made = _builder([_perm("r1"), LLMEvent(kind=EVENT_COMPLETE)], backend="")
    result = await verify_routing(_descriptor(_stub(tmp_path)), build, timeout=5)
    assert result.verdict == VERDICT_INCONCLUSIVE
    assert "provider for backend ''" in result.reason
    assert result.details == {"provider_backend": ""}
    assert made["provider"].shut_down is True


@pytest.mark.asyncio
async def test_probe_reports_a_violation_when_the_write_lands_despite_denial(tmp_path):
    exe = _stub(tmp_path)
    build, _ = _builder([_perm("r1"), LLMEvent(kind=EVENT_COMPLETE)], writes_probe=True)
    result = await verify_routing(_descriptor(exe), build, timeout=5)
    assert result.verdict == VERDICT_VIOLATION
    assert result.probe_file_written is True
    assert "without going through the permission gate" in result.reason
    # A host that never asked AND wrote is the same verdict.
    build, _ = _builder([LLMEvent(kind=EVENT_COMPLETE)], writes_probe=True)
    result = await verify_routing(_descriptor(exe), build, timeout=5)
    assert result.verdict == VERDICT_VIOLATION


@pytest.mark.asyncio
async def test_probe_is_inconclusive_when_nothing_was_attempted_or_the_turn_failed(tmp_path):
    exe = _stub(tmp_path)
    build, _ = _builder(
        [LLMEvent(kind=EVENT_TEXT_CHUNK, text="I cannot"), LLMEvent(kind=EVENT_COMPLETE)]
    )
    result = await verify_routing(_descriptor(exe), build, timeout=5)
    assert result.verdict == VERDICT_INCONCLUSIVE
    assert result.permission_requests == 0
    build, made = _builder([], raise_on_stream=RuntimeError("spawn failed"))
    result = await verify_routing(_descriptor(exe), build, timeout=5)
    assert result.verdict == VERDICT_INCONCLUSIVE
    assert "spawn failed" in result.reason
    assert made["provider"].shut_down is True

    def broken(session_key, cwd):
        raise RuntimeError("no such backend")

    result = await verify_routing(_descriptor(exe), broken, timeout=5)
    assert result.verdict == VERDICT_INCONCLUSIVE
    assert "no such backend" in result.reason


@pytest.mark.asyncio
async def test_probe_holds_the_spawn_allowance_only_while_running(tmp_path):
    from kiro_crew.acp.harness import routing_verification as rv

    seen = {}

    def build(session_key, cwd):
        seen["probing"] = "acme" in rv._PROBING
        return _FakeProvider([LLMEvent(kind=EVENT_COMPLETE)], cwd=cwd)

    await verify_routing(_descriptor(_stub(tmp_path)), build, timeout=5)
    assert seen["probing"] is True
    assert "acme" not in rv._PROBING


@pytest.mark.asyncio
async def test_facade_builds_the_descriptors_own_provider_and_promotes_only_on_verified(
    home, clean_registry, tmp_path, monkeypatch
):
    from types import SimpleNamespace

    import kiro_crew.providers.acp as acp_mod
    from kiro_crew.acp.harness import routing_verification as rv
    from kiro_crew.agent_sdk import operator_harnesses as facade

    exe = _stub(tmp_path)
    reg.load_and_register_operator_descriptors(path=_write(home, _routed(exe)))
    assert "acme" not in b.selectable_backends()

    constructed = {}

    class _StubAcpProvider(_FakeProvider):
        def __init__(self, **kwargs):
            constructed.update(kwargs)
            super().__init__(
                constructed.pop("_events"), cwd=kwargs["work_dir"], backend=kwargs["acp_backend"]
            )

    def make(events):
        def _ctor(**kwargs):
            kwargs["_events"] = events
            return _StubAcpProvider(**kwargs)

        return _ctor

    cfg = SimpleNamespace(agent=SimpleNamespace(default_agent="kirocrew", sandbox="auto"))

    monkeypatch.setattr(acp_mod, "AcpProvider", make([LLMEvent(kind=EVENT_COMPLETE)]))
    out = await facade.verify_operator_backend_routing("acme", cfg)
    assert out["verdict"] == VERDICT_INCONCLUSIVE and out["selectable"] is False
    assert load_attestations() == {}
    # The facade built the DESCRIPTOR's provider directly, with the configured
    # default agent and sandbox -- no per-chat selection gate in the path.
    assert constructed["acp_backend"] == "acme"
    assert constructed["agent"] == "kirocrew"
    assert constructed["sandbox_mode"] == "auto"
    assert constructed["session_key"] == "backend-routing-probe:acme"

    monkeypatch.setattr(acp_mod, "AcpProvider", make([_perm("r1"), LLMEvent(kind=EVENT_COMPLETE)]))
    out = await facade.verify_operator_backend_routing("acme", cfg)
    assert out["verdict"] == VERDICT_VERIFIED and out["selectable"] is True
    assert "acme" in b.selectable_backends()
    rec = load_attestations()["acme"]
    assert rec["fingerprint"] == descriptor_fingerprint(reg.registered_operator_descriptor("acme"))
    assert rec["executable_digest"] == rv.executable_digest(exe)
    assert rec["executable_path"] == exe

    with pytest.raises(ValueError):
        await facade.verify_operator_backend_routing("claude", cfg)  # a builtin has no claim


@pytest.mark.asyncio
async def test_a_binary_swapped_during_the_probe_is_inconclusive_and_records_nothing(
    home, clean_registry, tmp_path, monkeypatch
):
    """The verdict is bound to the bytes resolved BEFORE the run: a file replaced
    while the probe runs -- even by a well-behaved turn -- attests nothing, and the
    backend stays unverified."""
    from types import SimpleNamespace

    import kiro_crew.providers.acp as acp_mod
    from kiro_crew.agent_sdk import operator_harnesses as facade

    exe = _stub(tmp_path)
    reg.load_and_register_operator_descriptors(path=_write(home, _routed(exe)))

    class _Swapping(_FakeProvider):
        def __init__(self, **kwargs):
            super().__init__(
                [_perm("r1"), LLMEvent(kind=EVENT_COMPLETE)],
                cwd=kwargs["work_dir"],
                backend=kwargs["acp_backend"],
            )

        async def start(self):
            with open(exe, "w", encoding="utf-8") as fh:
                fh.write("#!/bin/sh\nexec replaced-while-probing\n")
            await super().start()

    monkeypatch.setattr(acp_mod, "AcpProvider", lambda **kw: _Swapping(**kw))
    cfg = SimpleNamespace(agent=SimpleNamespace(default_agent="kirocrew", sandbox="auto"))
    out = await facade.verify_operator_backend_routing("acme", cfg)
    assert out["verdict"] == VERDICT_INCONCLUSIVE
    assert "changed while the probe ran" in out["reason"]
    assert out["selectable"] is False
    assert load_attestations() == {}
    assert "acme" in reg.unverified_operator_harnesses()


@pytest.mark.asyncio
async def test_verified_but_policy_denied_reports_the_policy_and_stays_unselectable(
    home, clean_registry, tmp_path, monkeypatch
):
    from types import SimpleNamespace

    import kiro_crew.agent_backend_governance as gov
    import kiro_crew.providers.acp as acp_mod
    from kiro_crew.agent_sdk import operator_harnesses as facade

    exe = _stub(tmp_path)
    reg.load_and_register_operator_descriptors(path=_write(home, _routed(exe)))
    monkeypatch.setattr(gov, "_scope_permits", lambda backend: backend != "acme")
    monkeypatch.setattr(
        acp_mod,
        "AcpProvider",
        lambda **kw: _FakeProvider(
            [_perm("r1"), LLMEvent(kind=EVENT_COMPLETE)], cwd=kw["work_dir"], backend="acme"
        ),
    )
    cfg = SimpleNamespace(agent=SimpleNamespace(default_agent="kirocrew", sandbox="auto"))
    out = await facade.verify_operator_backend_routing("acme", cfg)
    assert out["verdict"] == VERDICT_VERIFIED
    assert out["selectable"] is False and out["policy_denied"] is True
    assert "agent_backend policy" in out["reason"]
    assert "acme" not in b.selectable_backends()
    assert load_attestations()["acme"]["executable_digest"]  # the evidence is kept
