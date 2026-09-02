"""Every capability gate answers from the bound harness descriptor's data.

Design Property 2: for all descriptors D with arbitrary capability sets C, each
gate's answer equals C's flag — flipping a flag in D flips the gate, and no code
path reaches its answer by comparing a harness identifier.

Two things are proved separately here, because each hides the other's failure.

**The gates follow the DATA** (the Hypothesis half). A synthetic harness is
registered with a generated capability set and each gate is read against it, so a
gate wired to anything other than its flag — a backend comparison, a hardcoded
True, the wrong flag — disagrees for some generated set. Synthetic rather than
bundled on purpose: a harness with no legacy ``acp_backend`` spelling cannot be
reached by an identifier comparison at all, so a gate that still made one answers
the DEFAULT for it and the disagreement is visible.

**The gates follow the SHIPPED data** (the bundled half). The property half would
pass against descriptors that granted every harness everything, so the bundled
harnesses are read through the real gates: this is the half a mutation probe on a
descriptor flag turns red, and it is what pins that the migration did not quietly
change what kiro-cli, KAS, or the claude seam is allowed to do.

The gates are read as unbound property functions against objects built with
``__new__``. Constructing a real provider spawns a process and reads
configuration; the gate is a pure function of one attribute, and reading it this
way exercises the SHIPPED property body rather than a re-implementation of it.
"""

from __future__ import annotations

import contextlib
import inspect
from typing import Iterator
from unittest import mock

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from kiro_crew.acp import client as client_mod
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.harness_descriptor import (
    CAPABILITY_NAMES,
    CapabilitySet,
    HarnessDescriptor,
)
from kiro_crew.acp.harness_registry import (
    HARNESS_CLAUDE,
    HARNESS_KAS,
    HARNESS_KIRO,
    registry,
)
from kiro_crew.acp.runtime import AcpRuntime
from kiro_crew.acp.session_provider import AcpSessionProvider
from kiro_crew.providers.acp import AcpProvider

#: A harness id outside every bundled roster and outside the legacy
#: ``acp_backend`` vocabulary, so nothing can answer for it by identity.
_PROBE = "probe-harness"


def _capability_sets() -> st.SearchStrategy[CapabilitySet]:
    """Every combination of capability flags, as a ``CapabilitySet``."""
    return st.builds(
        CapabilitySet,
        **{name: st.booleans() for name in CAPABILITY_NAMES},
    )


def _descriptor(capabilities: CapabilitySet, harness_id: str = _PROBE) -> HarnessDescriptor:
    return HarnessDescriptor(
        id=harness_id,
        display_name="Probe Harness",
        executable="probe-harness",
        argv=("{executable}",),
        capabilities=capabilities,
    )


@contextlib.contextmanager
def _registered(descriptor: HarnessDescriptor) -> Iterator[None]:
    """Put ``descriptor`` in the live registry's cache for the block's duration.

    Reaches the private cache deliberately: the registry exposes no writer for a
    harness definition (that is a pinned security property), and the alternative —
    writing ``agent.harnesses`` into a config file — would test the config parser
    rather than the gates. The fingerprint is left alone so ``_ensure_loaded``
    short-circuits and keeps the entry; the prior value is RESTORED in ``finally``
    (not deleted), so an id already in the loaded cache survives the block — a
    bare ``pop`` evicts it and ``_ensure_loaded`` never repopulates it, the
    ordering flake finding 4 addresses. The autouse ``_restore_harness_registry``
    fixture is the framework-level backstop.
    """
    reg = registry()
    reg._ensure_loaded()
    with reg._lock:
        prior = reg._descriptors.get(descriptor.id)
        reg._descriptors[descriptor.id] = descriptor
    try:
        yield
    finally:
        with reg._lock:
            if prior is None:
                reg._descriptors.pop(descriptor.id, None)
            else:
                reg._descriptors[descriptor.id] = prior


# ── The gates, as (name, reader) over a harness id ──


def _provider_on(harness_id: str) -> AcpProvider:
    provider = AcpProvider.__new__(AcpProvider)
    provider._harness_id = harness_id
    return provider


def _client_on(harness_id: str) -> Iterator[AcpClient]:
    """An ``AcpClient`` whose roster maps a probe backend onto ``harness_id``.

    The client's harness comes from its own backend roster, so binding an
    arbitrary harness means extending that roster rather than setting an
    attribute — which is also the shape a harness added to AcpClient would take.
    Patched with ``mock.patch`` rather than the ``monkeypatch`` fixture because
    Hypothesis refuses a function-scoped fixture that is not reset between
    generated examples.
    """
    return mock.patch.object(client_mod, "_CLIENT_HARNESSES", {"probe-backend": harness_id})


def _bare_client() -> AcpClient:
    client = AcpClient.__new__(AcpClient)
    client._acp_backend = "probe-backend"
    # Unbound: no binding threaded an id, so ``harness_id`` resolves through the
    # roster (patched by ``_client_roster``) exactly as legacy construction does.
    client._harness_id_override = ""
    return client


def _runtime_on(harness_id: str, *, resolved: HarnessDescriptor | None = None) -> AcpRuntime:
    runtime = AcpRuntime.__new__(AcpRuntime)
    runtime._acp_backend = "probe-backend"
    runtime._harness_id = harness_id
    runtime._harness_descriptor = resolved
    return runtime


#: ``gate name -> (capability flag it must mirror, reader taking a harness id)``.
#: Readers take the id rather than a built object so the Hypothesis half and the
#: bundled half drive the same table.
_PROVIDER_GATES = {
    "AcpProvider.is_acp_runtime_backend": (
        "acp_runtime_pool",
        lambda hid: AcpProvider.is_acp_runtime_backend.fget(_provider_on(hid)),
    ),
    "AcpProvider.is_session_sharing_eligible": (
        "session_sharing",
        lambda hid: AcpProvider.is_session_sharing_eligible.fget(_provider_on(hid)),
    ),
    "AcpProvider.uses_kiro_identity_store": (
        "kiro_identity_store",
        lambda hid: AcpProvider.uses_kiro_identity_store.fget(_provider_on(hid)),
    ),
    "AcpProvider.supports_reasoning_effort": (
        "reasoning_effort",
        lambda hid: AcpProvider.supports_reasoning_effort.fget(_provider_on(hid)),
    ),
    "AcpProvider.supports_mcp_tool_search": (
        "mcp_tool_search",
        lambda hid: AcpProvider.supports_mcp_tool_search.fget(_provider_on(hid)),
    ),
    "AcpRuntime.uses_kiro_identity_store": (
        "kiro_identity_store",
        lambda hid: AcpRuntime.uses_kiro_identity_store.fget(_runtime_on(hid)),
    ),
}


# ── Property 2: the gate equals the flag ──


@pytest.mark.parametrize("gate", sorted(_PROVIDER_GATES))
@settings(max_examples=40, deadline=None)
@given(capabilities=_capability_sets())
def test_gate_answer_equals_the_descriptor_flag(gate: str, capabilities: CapabilitySet) -> None:
    """Property 2 for every gate reachable from a harness id alone."""
    flag, read = _PROVIDER_GATES[gate]
    with _registered(_descriptor(capabilities)):
        assert read(_PROBE) is capabilities.has(flag), (
            f"{gate} disagreed with the {flag!r} flag on its bound descriptor; "
            "the gate is not reading descriptor data"
        )


@settings(max_examples=40, deadline=None)
@given(capabilities=_capability_sets())
def test_client_steer_equals_the_descriptor_flag(capabilities: CapabilitySet) -> None:
    """``AcpClient.supports_steer`` mirrors ``steer``.

    Separate from the table because binding a harness onto an ``AcpClient``
    means extending its backend roster rather than setting one attribute.
    """
    with _registered(_descriptor(capabilities)), _client_on(_PROBE):
        assert AcpClient.supports_steer.fget(_bare_client()) is capabilities.has("steer")


@settings(max_examples=40, deadline=None)
@given(capabilities=_capability_sets())
def test_client_sandbox_waiver_equals_the_descriptor_flag(capabilities: CapabilitySet) -> None:
    """The spawn path's sandbox waiver mirrors ``internal_sandbox``.

    Read through the client's capability seam rather than by spawning: the flag
    is what ``_spawn`` hands ``wrap_argv`` as ``is_kiro_cli``, and
    ``test_harness_parity.test_is_kiro_cli_is_positive`` is what pins that the
    call site still reads it. This is the one gate that fails OPEN — granting it
    to a harness with no internal sandbox leaves the agent process unconfined —
    so it is asserted for every generated set rather than only for kiro-cli.
    """
    with _registered(_descriptor(capabilities)), _client_on(_PROBE):
        assert _bare_client()._declares("internal_sandbox") is capabilities.has("internal_sandbox")


def test_session_provider_delegates_the_identity_store_answer() -> None:
    """``AcpSessionProvider`` reports the runtime's answer, not its own derivation.

    Two derivations of one process's capability can disagree, and the sweep that
    reads them would then retire a child on one reading while another says it is
    fine. Delegation is what makes that impossible.
    """
    provider = AcpSessionProvider.__new__(AcpSessionProvider)
    for expected in (True, False):
        with _registered(_descriptor(CapabilitySet(kiro_identity_store=expected))):
            provider._runtime = _runtime_on(_PROBE)
            assert AcpSessionProvider.uses_kiro_identity_store.fget(provider) is expected


def test_runtime_prefers_the_descriptor_it_already_resolved() -> None:
    """A runtime that has spawned answers from the descriptor it BOUND.

    The binding has to outlive the registry's copy: an operator who deletes a
    harness definition mid-session must not silently change what the running
    process is allowed to do. The resolved descriptor is that binding, so it
    outranks any later registry read — asserted by leaving the probe harness
    unregistered while the runtime holds it.
    """
    bound = _descriptor(CapabilitySet(kiro_identity_store=True))
    runtime = _runtime_on(_PROBE, resolved=bound)
    assert AcpRuntime.uses_kiro_identity_store.fget(runtime) is True


def test_an_unresolvable_harness_answers_every_capability_off() -> None:
    """The floor is fail-CLOSED: no descriptor means no capability.

    A gate cannot raise — it is read mid-turn and from the identity sweep — so an
    id nothing can resolve has to answer something. Answering "unsupported"
    costs a feature; answering "supported" would hand a harness a sandbox waiver
    or session sharing it never declared.
    """
    caps = registry().bound_capabilities("no-such-harness")
    assert caps == CapabilitySet()
    assert not any(caps.has(name) for name in CAPABILITY_NAMES)


# ── The bundled half: the shipped grants, read through the real gates ──


@pytest.mark.parametrize(
    "harness_id,expected",
    [
        (
            HARNESS_KIRO,
            {
                "AcpProvider.is_acp_runtime_backend": True,
                "AcpProvider.is_session_sharing_eligible": True,
                "AcpProvider.uses_kiro_identity_store": True,
                "AcpProvider.supports_reasoning_effort": True,
                "AcpProvider.supports_mcp_tool_search": True,
                "AcpRuntime.uses_kiro_identity_store": True,
            },
        ),
        (
            HARNESS_KAS,
            {
                "AcpProvider.is_acp_runtime_backend": True,
                # KAS's teardown deletes the persisted session, so a shared
                # subagent session would strand spawn_continue.
                "AcpProvider.is_session_sharing_eligible": False,
                # Granted: the relay is `kiro-cli acp --agent-engine v3
                # --auth-method cli`, and that auth owner resolves every access
                # token from kiro-cli's own store — so a logout invalidates a
                # running KAS relay exactly as it does the kiro backend.
                "AcpProvider.uses_kiro_identity_store": True,
                # Both withdrawn deliberately: see the KAS descriptor's comments.
                "AcpProvider.supports_reasoning_effort": False,
                "AcpProvider.supports_mcp_tool_search": False,
                "AcpRuntime.uses_kiro_identity_store": True,
            },
        ),
        (
            HARNESS_CLAUDE,
            {
                # One AcpClient per session: no shared runtime, no sharing.
                "AcpProvider.is_acp_runtime_backend": False,
                "AcpProvider.is_session_sharing_eligible": False,
                "AcpProvider.uses_kiro_identity_store": False,
                # Honoured, but through a live set_config_option rather than
                # kiro's cli.json overlay.
                "AcpProvider.supports_reasoning_effort": True,
                "AcpProvider.supports_mcp_tool_search": False,
                "AcpRuntime.uses_kiro_identity_store": False,
            },
        ),
    ],
)
def test_bundled_harness_answers_through_the_real_gates(
    harness_id: str, expected: dict[str, bool]
) -> None:
    """The shipped grants, asserted where they take effect.

    This is the half a flipped descriptor flag turns red: the property half
    generates its own capability sets and would accept any bundled grant at all.
    """
    assert set(expected) == set(_PROVIDER_GATES), "every gate is accounted for per harness"
    for gate, want in expected.items():
        _flag, read = _PROVIDER_GATES[gate]
        assert read(harness_id) is want, f"{gate} on {harness_id!r}"


# ── No gate decides by harness identity ──

#: Every migrated gate body, as the source a decision must not appear in.
_GATE_SOURCES = {
    "AcpProvider._declares": AcpProvider._declares,
    "AcpProvider.is_acp_runtime_backend": AcpProvider.is_acp_runtime_backend.fget,
    "AcpProvider.is_session_sharing_eligible": AcpProvider.is_session_sharing_eligible.fget,
    "AcpProvider.uses_kiro_identity_store": AcpProvider.uses_kiro_identity_store.fget,
    "AcpProvider.supports_reasoning_effort": AcpProvider.supports_reasoning_effort.fget,
    "AcpProvider.supports_mcp_tool_search": AcpProvider.supports_mcp_tool_search.fget,
    "AcpClient._declares": AcpClient._declares,
    "AcpClient.supports_steer": AcpClient.supports_steer.fget,
    "AcpRuntime._declares": AcpRuntime._declares,
    "AcpRuntime.uses_kiro_identity_store": AcpRuntime.uses_kiro_identity_store.fget,
    "AcpSessionProvider.uses_kiro_identity_store": (
        AcpSessionProvider.uses_kiro_identity_store.fget
    ),
}


@pytest.mark.parametrize("name", sorted(_GATE_SOURCES))
def test_no_gate_compares_a_harness_identifier(name: str) -> None:
    """A capability decision is never a comparison against an identifier.

    Source-level because that is where the shape is visible: a gate can agree
    with the descriptor for every harness that HAS a legacy spelling and still be
    keyed on one, and the harness it then answers wrongly for is the next one
    added — which no behavioural test in this file can generate.
    """
    body = inspect.getsource(_GATE_SOURCES[name])
    code = body.split('"""')[-1] if '"""' in body else body
    for forbidden in ("ACP_BACKEND_", "HARNESS_KIRO", "HARNESS_KAS", "HARNESS_CLAUDE"):
        assert forbidden not in code, f"{name} decides a capability by harness identity"
    for operator in ("==", "!="):
        assert operator not in code, f"{name} decides a capability by comparison"
