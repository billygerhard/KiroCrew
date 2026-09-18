"""End-to-end routing verification for descriptor backends.

The threat the gate answers: a descriptor DECLARES a routing, and a host can
honour the declared mechanism (load the agent, accept the config option) while
never sending ``session/request_permission`` -- every tool call it made would
bypass PreToolUse, the deny rules and the audit. So a declared routing makes a
descriptor known and runnable-for-verification, never selectable; only an
attestation this gateway recorded after observing the host ask (and honour a
refusal) does. These tests pin the store, the gate, and the probe's verdicts.
"""

from __future__ import annotations

import json
import os

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
    is_attested,
    load_attestations,
    record_attestation,
    revoke_attestation,
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


def _descriptor(**overrides) -> HarnessDescriptor:
    base = dict(
        id="acme",
        display_name="Acme",
        executable="/opt/acme",
        argv=("{executable}", "acp"),
        agent_args=("--agent", "{agent}"),
        routing="agent_spec",
    )
    base.update(overrides)
    return HarnessDescriptor(**base)


# ── Fingerprint ──


def test_fingerprint_covers_every_spawn_shaping_field_and_nothing_else():
    d = _descriptor()
    fp = descriptor_fingerprint(d)
    assert fp == descriptor_fingerprint(_descriptor())
    # Cosmetic fields do not move it: a rename or a static-catalog edit must not
    # revoke an attestation, because neither changes what runs or how it routes.
    assert descriptor_fingerprint(_descriptor(display_name="Renamed")) == fp
    assert descriptor_fingerprint(_descriptor(models=("m1",))) == fp
    # Every spawn-shaping field does.
    assert descriptor_fingerprint(_descriptor(executable="/opt/other")) != fp
    assert descriptor_fingerprint(_descriptor(argv=("{executable}", "serve"))) != fp
    assert descriptor_fingerprint(_descriptor(agent_args=("--profile", "{agent}"))) != fp
    assert descriptor_fingerprint(_descriptor(model_args=("--model", "{model}"))) != fp
    assert descriptor_fingerprint(_descriptor(mcp_delivery="session_array")) != fp
    assert (
        descriptor_fingerprint(
            _descriptor(
                agent_args=(),
                routing="session_config",
                permission_config=PermissionConfig("mode", "ask"),
            )
        )
        != fp
    )


# ── Store ──


def test_store_round_trip_and_fingerprint_binding(home):
    d = _descriptor()
    assert load_attestations() == {}
    assert is_attested(d) is False
    rec = record_attestation(d, mechanism="agent_spec", evidence={"permission_requests": 1})
    assert rec["fingerprint"] == descriptor_fingerprint(d)
    assert is_attested(d) is True
    # The same id with a different spawn shape is NOT attested: the edit revoked it.
    assert is_attested(_descriptor(executable="/opt/evil")) is False
    # The file is gateway-owned JSON beside config.json.
    on_disk = json.loads((home / "backend-routing-attestations.json").read_text("utf-8"))
    assert on_disk["acme"]["mechanism"] == "agent_spec"
    assert revoke_attestation("acme") is True
    assert is_attested(d) is False
    assert revoke_attestation("acme") is False


def test_unreadable_store_fails_closed(home):
    (home / "backend-routing-attestations.json").write_text("not json", encoding="utf-8")
    assert load_attestations() == {}
    assert is_attested(_descriptor()) is False


# ── Boot gate ──


def _write(home, mapping):
    p = home / "harnesses.json"
    p.write_text(json.dumps(mapping), encoding="utf-8")
    return str(p)


_ROUTED = {
    "acme": {
        "executable": "/opt/acme",
        "argv": ["{executable}", "acp"],
        "agent_args": ["--agent", "{agent}"],
        "routing": "agent_spec",
    }
}


def test_a_routed_descriptor_is_known_but_unselectable_until_attested(home, clean_registry):
    reg.load_and_register_operator_descriptors(path=_write(home, _ROUTED))
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
    no_route = {"acme": {"executable": "/opt/acme", "argv": ["{executable}"]}}
    reg.load_and_register_operator_descriptors(path=_write(home, no_route))
    assert "acme" in reg.unselectable_operator_harnesses()
    assert "acme" not in reg.unverified_operator_harnesses()


def test_an_attested_descriptor_is_selectable_at_boot(home, clean_registry):
    from kiro_crew.acp.harness.descriptor import descriptor_from_mapping

    d, _ = descriptor_from_mapping(_ROUTED["acme"], harness_id="acme")
    assert d is not None
    record_attestation(d, mechanism="agent_spec", evidence={})
    reg.load_and_register_operator_descriptors(path=_write(home, _ROUTED))
    assert "acme" in b.selectable_backends()
    assert "acme" not in reg.unselectable_operator_harnesses()
    assert "acme" not in reg.unverified_operator_harnesses()


def test_editing_the_spawn_shape_revokes_selectability_at_the_next_boot(home, clean_registry):
    from kiro_crew.acp.harness.descriptor import descriptor_from_mapping

    d, _ = descriptor_from_mapping(_ROUTED["acme"], harness_id="acme")
    record_attestation(d, mechanism="agent_spec", evidence={})
    edited = {"acme": dict(_ROUTED["acme"], argv=["{executable}", "acp", "--yolo"])}
    reg.load_and_register_operator_descriptors(path=_write(home, edited))
    assert "acme" not in b.selectable_backends()
    assert reg.unselectable_operator_harnesses()["acme"] == UNVERIFIED_REASON


def test_mark_routing_verified_promotes_live_only_with_a_matching_attestation(home, clean_registry):
    reg.load_and_register_operator_descriptors(path=_write(home, _ROUTED))
    with pytest.raises(ValueError):
        reg.mark_routing_verified("acme")  # no attestation on disk
    with pytest.raises(ValueError):
        reg.mark_routing_verified("not-registered")
    d = reg.registered_operator_descriptor("acme")
    assert d is not None
    record_attestation(d, mechanism="agent_spec", evidence={})
    reg.mark_routing_verified("acme")
    assert "acme" in b.selectable_backends()
    assert "acme" not in reg.unverified_operator_harnesses()
    assert "acme" not in reg.unselectable_operator_harnesses()


# ── The probe ──


class _FakeProvider:
    """A provider whose one turn replays a scripted event list.

    ``writes_probe`` makes the fake behave like a host that executes the write
    regardless of the answer (the violation the probe exists to catch).
    """

    def __init__(self, events, *, cwd, writes_probe=False, raise_on_stream=None):
        self._events = events
        self._cwd = cwd
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


def _factory(events, **kw):
    made = {}

    def factory(session_key, **kwargs):
        p = _FakeProvider(events, cwd=kwargs["cwd"], **kw)
        made["provider"] = p
        made["kwargs"] = kwargs
        made["session_key"] = session_key
        return p

    return factory, made


def _perm(rid):
    return LLMEvent(kind=EVENT_PERMISSION_REQUEST, request_id=rid)


@pytest.mark.asyncio
async def test_probe_verifies_a_host_that_asks_and_honours_the_refusal():
    factory, made = _factory(
        [_perm("r1"), LLMEvent(kind=EVENT_TEXT_CHUNK, text="denied"), LLMEvent(kind=EVENT_COMPLETE)]
    )
    result = await verify_routing(_descriptor(), factory, timeout=5)
    assert result.verdict == VERDICT_VERIFIED
    assert result.permission_requests == 1
    # The probe crossed the production factory with the descriptor's id as the
    # per-chat override, in a scratch cwd, and never granted anything.
    assert made["kwargs"]["backend_override"] == "acme"
    assert made["session_key"] == "backend-routing-probe:acme"
    assert made["provider"].rejected == ["r1"]
    assert made["provider"].shut_down is True
    assert not os.path.isdir(made["kwargs"]["cwd"])  # scratch removed


@pytest.mark.asyncio
async def test_probe_reports_a_violation_when_the_write_lands_despite_denial():
    factory, made = _factory([_perm("r1"), LLMEvent(kind=EVENT_COMPLETE)], writes_probe=True)
    result = await verify_routing(_descriptor(), factory, timeout=5)
    assert result.verdict == VERDICT_VIOLATION
    assert result.probe_file_written is True
    assert "without going through the permission gate" in result.reason
    # A host that never asked AND wrote is the same verdict.
    factory, _ = _factory([LLMEvent(kind=EVENT_COMPLETE)], writes_probe=True)
    result = await verify_routing(_descriptor(), factory, timeout=5)
    assert result.verdict == VERDICT_VIOLATION


@pytest.mark.asyncio
async def test_probe_is_inconclusive_when_nothing_was_attempted_or_the_turn_failed():
    factory, _ = _factory(
        [LLMEvent(kind=EVENT_TEXT_CHUNK, text="I cannot"), LLMEvent(kind=EVENT_COMPLETE)]
    )
    result = await verify_routing(_descriptor(), factory, timeout=5)
    assert result.verdict == VERDICT_INCONCLUSIVE
    assert result.permission_requests == 0
    factory, made = _factory([], raise_on_stream=RuntimeError("spawn failed"))
    result = await verify_routing(_descriptor(), factory, timeout=5)
    assert result.verdict == VERDICT_INCONCLUSIVE
    assert "spawn failed" in result.reason
    assert made["provider"].shut_down is True

    def broken_factory(session_key, **kwargs):
        raise RuntimeError("no such backend")

    result = await verify_routing(_descriptor(), broken_factory, timeout=5)
    assert result.verdict == VERDICT_INCONCLUSIVE
    assert "no such backend" in result.reason


@pytest.mark.asyncio
async def test_facade_records_and_promotes_only_on_verified(home, clean_registry):
    from kiro_crew.agent_sdk.operator_harnesses import verify_operator_backend_routing

    reg.load_and_register_operator_descriptors(path=_write(home, _ROUTED))
    assert "acme" not in b.selectable_backends()

    factory, _ = _factory([LLMEvent(kind=EVENT_COMPLETE)])
    out = await verify_operator_backend_routing("acme", factory)
    assert out["verdict"] == VERDICT_INCONCLUSIVE
    assert out["selectable"] is False
    assert load_attestations() == {}

    factory, _ = _factory([_perm("r1"), LLMEvent(kind=EVENT_COMPLETE)])
    out = await verify_operator_backend_routing("acme", factory)
    assert out["verdict"] == VERDICT_VERIFIED
    assert out["selectable"] is True
    assert "acme" in b.selectable_backends()
    assert load_attestations()["acme"]["fingerprint"] == descriptor_fingerprint(
        reg.registered_operator_descriptor("acme")
    )

    with pytest.raises(ValueError):
        await verify_operator_backend_routing("claude", factory)  # a builtin has no claim
