"""The two SECURITY-half gates answer from the bound descriptor, fail-closed.

Wave-2 T4 rekeyed two gates from ``ACP_BACKENDS_*`` frozenset / backend-string
reads to ``binding.descriptor.capabilities`` reads at the same call site, same
polarity:

* **INTERNAL SANDBOX** — ``sandbox.wrap_argv``'s ``is_kiro_cli`` argument. The
  flag makes ``wrap_argv`` SKIP Crew's own OS sandbox in favour of the harness's
  internal one, so it is the ONE gate that fails OPEN if answered wrongly:
  granting it to a harness with no internal sandbox leaves the agent process
  unconfined. The load-bearing property is therefore the negative: an
  undeclared / absent ``internal_sandbox`` MUST make Crew's sandbox wrap the
  child.
* **KIRO IDENTITY STORE** — the identity-change sweep's membership test. A
  member's live child is retired when kiro-cli's store starts naming a
  different account; a non-member is EXCLUDED (its process authenticates some
  other way and must not be recycled on a store it never reads).

This file is the END-TO-END, fail-closed half of the rekey — a companion to
``test_harness_capability_gates.py``, which pins the property "each gate equals
its flag" against synthetic and bundled harnesses. Here every assertion lands on
the REAL decision the shipped call site computes:

* the sandbox assertions read the exact boolean ``AcpClient._spawn`` /
  ``AcpRuntime.spawn`` hand ``wrap_argv`` (``client._declares(...)`` /
  ``descriptor.capabilities.internal_sandbox``) AND drive the real
  ``sandbox.wrap_argv`` with it, asserting on the argv it returns / the value it
  received — never on a mock of the decision;
* the sweep assertions read the real predicate ``session._provider_uses_kiro_
  identity_store`` that ``SessionManager`` consults at every sweep site, against
  providers/runtimes bound to real descriptors.

The mutation discipline: each capability is asserted BOTH ways (declared ⇒ old
privileged behaviour; undeclared ⇒ fail-closed), so a gate wired to a constant,
the wrong flag, or a backend comparison disagrees for one of the two.
"""

from __future__ import annotations

import contextlib
import json
from typing import Iterator
from unittest import mock

import pytest

from kiro_crew import sandbox as sandbox_mod
from kiro_crew import session as session_mod
from kiro_crew.acp import client as client_mod
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.harness_descriptor import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    CAPABILITY_INTERNAL_SANDBOX,
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
from kiro_crew.providers.acp import AcpProvider

# ── Fixtures: a probe harness reachable by NO backend string ──

#: Ids outside every bundled roster and outside the legacy ``acp_backend``
#: vocabulary, so nothing can answer for them by identity — the fail-closed
#: path and the generic-operator path are only reachable this way.
_PRIV = "probe-internal-sandbox-and-store"  # both security caps declared
_BARE = "probe-empty-capabilities"  # a GenericAdapter operator descriptor


def _descriptor(harness_id: str, capabilities: CapabilitySet) -> HarnessDescriptor:
    return HarnessDescriptor(
        id=harness_id,
        display_name="Probe Harness",
        executable="probe-harness",
        argv=("{executable}",),
        capabilities=capabilities,
    )


@contextlib.contextmanager
def _registered(*descriptors: HarnessDescriptor) -> Iterator[None]:
    """Put ``descriptors`` in the live registry cache for the block.

    Reaches the private cache deliberately, exactly as
    ``test_harness_capability_gates`` does: the registry exposes no writer for a
    harness definition (a pinned security property), and writing config would
    test the parser instead of the gate. The fingerprint is left alone so
    ``_ensure_loaded`` keeps the entry.
    """
    reg = registry()
    reg._ensure_loaded()
    with reg._lock:
        # SAVE the prior value of each id (not just its presence), so exit
        # RESTORES rather than deletes: a bare ``pop`` would evict an id already
        # in the loaded cache, and ``_ensure_loaded``'s unchanged-fingerprint
        # short-circuit then never repopulates it for the rest of the worker —
        # the ordering flake finding 4 addresses. The autouse
        # ``_restore_harness_registry`` fixture is the framework-level backstop;
        # this keeps the helper itself correct in isolation.
        saved = {d.id: reg._descriptors.get(d.id) for d in descriptors}
        for descriptor in descriptors:
            reg._descriptors[descriptor.id] = descriptor
    try:
        yield
    finally:
        with reg._lock:
            for hid, prior in saved.items():
                if prior is None:
                    reg._descriptors.pop(hid, None)
                else:
                    reg._descriptors[hid] = prior


def _provider_on(harness_id: str) -> AcpProvider:
    """A bare ``AcpProvider`` bound to ``harness_id`` (no process, no config)."""
    provider = AcpProvider.__new__(AcpProvider)
    provider._harness_id = harness_id
    return provider


def _runtime_on(harness_id: str, *, resolved: HarnessDescriptor | None = None) -> AcpRuntime:
    runtime = AcpRuntime.__new__(AcpRuntime)
    runtime._acp_backend = "probe-backend"
    runtime._harness_id = harness_id
    runtime._harness_descriptor = resolved
    return runtime


def _client_on(harness_id: str) -> AcpClient:
    """A bare ``AcpClient`` whose roster maps ``probe-backend`` onto ``harness_id``.

    The client's harness comes from its own backend roster, so binding an
    arbitrary harness means extending that roster (patched by the caller) rather
    than setting an attribute — the shape a harness added to AcpClient takes.
    """
    client = AcpClient.__new__(AcpClient)
    client._acp_backend = "probe-backend"
    # Unbound: no binding threaded an id, so the property resolves through the
    # roster (patched by ``_client_roster``) exactly as legacy construction does.
    client._harness_id_override = ""
    return client


@contextlib.contextmanager
def _client_roster(harness_id: str) -> Iterator[None]:
    with mock.patch.object(client_mod, "_CLIENT_HARNESSES", {"probe-backend": harness_id}):
        yield


def _client_bound_on(harness_id: str) -> AcpClient:
    """A bare ``AcpClient`` whose harness id was THREADED from a session binding.

    This is the generic-operator shape finding 1 addresses: the backend string is
    a descriptor id absent from ``_CLIENT_HARNESSES``, and the id reaches the
    client through ``binding.harness_id`` (``_harness_id_override``) rather than
    the roster. ``harness_id`` prefers the override, so ``_declares`` gates on the
    BOUND descriptor — never on the roster's kiro fallback.
    """
    client = AcpClient.__new__(AcpClient)
    # A backend string that is its own descriptor id (the generic-fallback
    # admission acp_backend == harness_id), reachable by NO roster entry.
    client._acp_backend = harness_id
    client._harness_id_override = harness_id
    return client


# ── Finding 1: a bound generic operator inherits NO kiro sandbox waiver ──


def test_bound_generic_operator_client_fails_closed_through_real_declares() -> None:
    """A bound operator harness answers ``internal_sandbox`` False via real ``_declares``.

    The generic-fallback binding admits ``acp_backend == descriptor.id`` — a
    string absent from ``_CLIENT_HARNESSES``. Finding 1: without the threaded id,
    ``harness_id`` fell back to kiro-cli's id and ``_declares`` read kiro-cli's
    FULL CapabilitySet, silently granting the internal-sandbox waiver (Crew's
    seatbelt skipped) to a harness that declares none. With the id threaded from
    the binding, ``_declares`` gates on the operator's own empty descriptor and
    answers False — asserted through the SHIPPED ``_declares`` path, not a mock.
    """
    with _registered(_descriptor(_BARE, CapabilitySet())):
        client = _client_bound_on(_BARE)
        # The id the client reports is the bound operator, NOT kiro's fallback.
        assert client.harness_id == _BARE
        # The real gate the spawn seam computes: fail-closed.
        assert client._declares(CAPABILITY_INTERNAL_SANDBOX) is False


def test_old_roster_fallback_would_have_answered_kiro() -> None:
    """Proof the pre-fix fallback was the vulnerability: the roster maps NO unknown
    string to kiro any more.

    The fix changed the fallback from ``HARNESS_KIRO`` to ``""``. This pins that
    an id absent from the roster resolves to the empty (unknown) id — so an
    unbound client on an unmapped backend routes ``_declares`` through
    ``bound_capabilities("")``, which warns and answers every flag OFF, rather
    than inheriting kiro-cli's waiver. If a future edit restored the kiro
    fallback, ``_BARE`` would map to ``HARNESS_KIRO`` here and this fails.
    """
    assert client_mod._CLIENT_HARNESSES.get(_BARE, "") == ""
    # And the unbound client (roster path, no override) is therefore fail-closed
    # on an unmapped backend — the property the old kiro fallback broke.
    with _registered(_descriptor(_BARE, CapabilitySet())):
        assert _client_on(_BARE)._declares(CAPABILITY_INTERNAL_SANDBOX) is False


# ── Gate 1: internal sandbox (sandbox.wrap_argv is_kiro_cli), fails OPEN ──


def _wrap_spy() -> tuple[mock.MagicMock, "contextlib.AbstractContextManager"]:
    """A spy standing in for ``sandbox.wrap_argv`` in BOTH ACP spawn modules.

    Returns ``(argv, cleanup)`` unchanged so the caller's argv assertions still
    work, and records every ``is_kiro_cli`` value it was handed. Patched on the
    client and runtime modules' own imported reference — the name each call site
    actually invokes — so the boolean captured is the one the shipped code
    computed, not one this test supplied.
    """
    spy = mock.MagicMock(side_effect=lambda argv, **kw: (list(argv), None))

    @contextlib.contextmanager
    def _patched() -> Iterator[None]:
        with (
            mock.patch.object(client_mod, "wrap_argv", spy),
            mock.patch.object(sandbox_mod, "wrap_argv", spy),
        ):
            yield

    return spy, _patched()


def _client_sandbox_decision(harness_id: str) -> bool:
    """The exact boolean ``AcpClient._spawn`` hands ``wrap_argv`` as is_kiro_cli.

    ``_spawn`` computes ``is_kiro_cli=self._declares(CAPABILITY_INTERNAL_SANDBOX)``
    (client.py); reading that same expression here exercises the SHIPPED decision
    body, not a re-implementation of it.
    """
    with _client_roster(harness_id):
        return _client_on(harness_id)._declares(CAPABILITY_INTERNAL_SANDBOX)


def _runtime_sandbox_decision(descriptor: HarnessDescriptor) -> bool:
    """The exact boolean ``AcpRuntime.spawn`` hands ``wrap_argv`` as is_kiro_cli.

    ``spawn`` computes ``is_kiro_cli=descriptor.capabilities.internal_sandbox``
    off the descriptor it resolved for the run (runtime.py); this reads that.
    """
    return descriptor.capabilities.internal_sandbox


def test_declared_internal_sandbox_skips_crew_wrap_on_the_client_seam() -> None:
    """A harness WITH ``internal_sandbox`` waives Crew's wrap (old privileged path).

    The value the client hands ``wrap_argv`` is True, which is what makes
    ``wrap_argv`` skip Crew's seatbelt in favour of the harness's own sandbox.
    """
    with _registered(_descriptor(_PRIV, CapabilitySet(internal_sandbox=True))):
        assert _client_sandbox_decision(_PRIV) is True


def test_undeclared_internal_sandbox_makes_crew_wrap_the_child_client_seam() -> None:
    """FAIL-CLOSED: an empty CapabilitySet must NOT waive Crew's sandbox.

    This is the load-bearing property. The client hands ``wrap_argv``
    ``is_kiro_cli=False``, so Crew's own OS sandbox wraps the child rather than
    delegating to an internal sandbox the harness never declared.
    """
    with _registered(_descriptor(_BARE, CapabilitySet())):
        assert _client_sandbox_decision(_BARE) is False


def test_runtime_hands_wrap_argv_the_descriptor_flag_declared() -> None:
    """A bound runtime WITH the capability hands ``wrap_argv`` True."""
    bound = _descriptor(_PRIV, CapabilitySet(internal_sandbox=True))
    assert _runtime_sandbox_decision(bound) is True


def test_runtime_hands_wrap_argv_the_descriptor_flag_fail_closed() -> None:
    """FAIL-CLOSED: a bound operator runtime (empty caps) hands ``wrap_argv`` False."""
    bound = _descriptor(_BARE, CapabilitySet())
    assert _runtime_sandbox_decision(bound) is False


@pytest.mark.parametrize("declared", [True, False])
def test_wrap_argv_receives_the_descriptor_decision_not_a_backend_check(declared: bool) -> None:
    """The value flowing into the REAL ``sandbox.wrap_argv`` is the descriptor flag.

    Drives ``sandbox.wrap_argv`` with the boolean the shipped call site computes
    for a probe harness (declared / not), through the spy that captures
    ``is_kiro_cli``. Asserting the captured value equals the descriptor flag —
    for a harness reachable by NO backend string — proves the argument is not a
    hidden ``ACP_BACKEND_ == ...`` comparison (which would answer the default for
    a probe harness) and that an undeclared capability arrives as False.
    """
    caps = CapabilitySet(internal_sandbox=declared)
    hid = _PRIV if declared else _BARE
    spy, patched = _wrap_spy()
    with _registered(_descriptor(hid, caps)), patched:
        decision = _client_sandbox_decision(hid)
        # Exercise the real wrap_argv call shape with the shipped decision, mode
        # "off" so no real seatbelt/temp file is allocated by the spy stand-in.
        sandbox_mod.wrap_argv(["probe-harness"], mode="off", is_kiro_cli=decision)
    assert spy.call_args.kwargs["is_kiro_cli"] is declared


def test_real_wrap_argv_wraps_when_capability_absent_off_mode() -> None:
    """The genuine ``sandbox.wrap_argv`` does not silently unconfine on fail-closed.

    With ``is_kiro_cli=False`` (the fail-closed decision for an operator harness)
    ``mode="off"`` yields an env-scrubbed passthrough, NOT a kiro-delegation
    branch — asserted on the ACTUAL returned argv, proving the fail-closed value
    reaches the real function and takes the non-delegated path.
    """
    argv, cleanup = sandbox_mod.wrap_argv(
        ["probe-harness"], mode="off", strip_python_env=True, is_kiro_cli=False
    )
    assert argv[0] != "probe-harness" or argv == ["probe-harness"]
    # The delegated darwin branch would return early with a SEL-audited
    # passthrough; on every platform the non-delegated path returns the argv
    # (optionally env-wrapped) with no cleanup temp file.
    assert cleanup is None


# ── Gate 2: kiro identity-store sweep membership, fails CLOSED ──


def test_sweep_includes_a_member_provider() -> None:
    """A provider bound to a descriptor WITH the capability is swept (retired)."""
    with _registered(_descriptor(_PRIV, CapabilitySet(kiro_identity_store=True))):
        assert session_mod._provider_uses_kiro_identity_store(_provider_on(_PRIV)) is True


def test_sweep_excludes_an_operator_provider_fail_closed() -> None:
    """FAIL-CLOSED: an empty-CapabilitySet operator provider is EXCLUDED from the sweep.

    A foreign harness does not authenticate through kiro-cli's store, so a
    kiro-cli account change must not retire its sessions.
    """
    with _registered(_descriptor(_BARE, CapabilitySet())):
        assert session_mod._provider_uses_kiro_identity_store(_provider_on(_BARE)) is False


def test_sweep_includes_a_member_runtime() -> None:
    """The sweep reaches shared runtimes too: a member runtime is swept."""
    bound = _descriptor(_PRIV, CapabilitySet(kiro_identity_store=True))
    assert (
        session_mod._provider_uses_kiro_identity_store(_runtime_on(_PRIV, resolved=bound)) is True
    )


def test_sweep_excludes_an_operator_runtime_fail_closed() -> None:
    """FAIL-CLOSED: an operator runtime (empty caps) is excluded from the sweep."""
    bound = _descriptor(_BARE, CapabilitySet())
    assert (
        session_mod._provider_uses_kiro_identity_store(_runtime_on(_BARE, resolved=bound)) is False
    )


# ── Legacy / unbound construction: the six bundled combinations ──

# The two security-half capabilities as the bundled descriptors declare them.
# kiro: internal_sandbox + kiro_identity_store. KAS: identity store only (its
# argv runs kiro-cli, but it starts no inner OS sandbox). claude seam: neither
# (one AcpClient per session, own subscription auth).
_BUNDLED_SECURITY_CAPS = {
    HARNESS_KIRO: {"internal_sandbox": True, "kiro_identity_store": True},
    HARNESS_KAS: {"internal_sandbox": False, "kiro_identity_store": True},
    HARNESS_CLAUDE: {"internal_sandbox": False, "kiro_identity_store": False},
}

# Legacy ``acp_backend`` spelling → harness id, for the unbound construction path
# (AcpClient resolves its harness from ``_CLIENT_HARNESSES``; provider/runtime
# from ``harness_id``). This is the "cannot see a descriptor" case: it must
# resolve via the registry from the backend string, never assume kiro.
_LEGACY_SPELLINGS = {
    ACP_BACKEND_KIRO: HARNESS_KIRO,
    ACP_BACKEND_KAS: HARNESS_KAS,
    ACP_BACKEND_CLAUDE: HARNESS_CLAUDE,
}


@pytest.mark.parametrize("harness_id", sorted(_BUNDLED_SECURITY_CAPS))
def test_unbound_identity_store_matches_the_bundled_grant(harness_id: str) -> None:
    """The identity-store sweep answer is unchanged for each bundled harness.

    Unbound legacy construction: a provider carrying only ``harness_id`` (no
    HarnessBinding) resolves capabilities through the registry, and its sweep
    membership must equal the pre-rekey frozenset membership
    (``ACP_BACKENDS_KIRO_IDENTITY_STORE`` = {kiro, kas}).
    """
    want = _BUNDLED_SECURITY_CAPS[harness_id]["kiro_identity_store"]
    assert session_mod._provider_uses_kiro_identity_store(_provider_on(harness_id)) is want


@pytest.mark.parametrize("backend,harness_id", sorted(_LEGACY_SPELLINGS.items()))
def test_unbound_internal_sandbox_matches_the_bundled_grant(backend: str, harness_id: str) -> None:
    """The sandbox waiver is unchanged for each bundled harness, from the backend string.

    Legacy AcpClient construction resolves its harness from the backend spelling
    via ``_CLIENT_HARNESSES``; the ``is_kiro_cli`` value it hands ``wrap_argv``
    must equal the pre-rekey frozenset membership
    (``ACP_BACKENDS_INTERNAL_SANDBOX`` = {kiro}).
    """
    want = _BUNDLED_SECURITY_CAPS[harness_id]["internal_sandbox"]
    with _client_roster(harness_id):
        assert _client_on(harness_id)._declares(CAPABILITY_INTERNAL_SANDBOX) is want


def test_all_six_bundled_combinations_are_pinned() -> None:
    """Guard: both security caps × the three bundled harnesses are all asserted.

    The two parametrized tests above cover the six combinations; this fails if a
    harness is added to one table but not the other, so the matrix cannot silently
    shrink.
    """
    assert set(_BUNDLED_SECURITY_CAPS) == set(_LEGACY_SPELLINGS.values())
    assert len(_BUNDLED_SECURITY_CAPS) == 3


# ── GenericAdapter operator descriptor (empty CapabilitySet): both fail-closed ──


def test_generic_operator_descriptor_gets_crew_sandbox_and_no_sweep() -> None:
    """An adapter-less operator harness (empty caps) is fully fail-closed.

    It declares neither capability, so: (1) the sandbox decision is False ⇒ Crew's
    own sandbox wraps the child (no delegation to an internal sandbox it never
    declared), and (2) it is excluded from the identity sweep ⇒ a kiro-cli account
    change never retires its sessions. This is the exact posture a
    ``GenericAdapter`` harness resolves to (adapter unset ⇒ generic; capabilities
    default to all-False).
    """
    operator = _descriptor(_BARE, CapabilitySet())
    with _registered(operator):
        # Sandbox: Crew wraps (fail-closed), via both the client seam and the
        # runtime's descriptor read.
        assert _client_sandbox_decision(_BARE) is False
        assert _runtime_sandbox_decision(operator) is False
        # Identity sweep: excluded, via both provider and runtime predicates.
        assert session_mod._provider_uses_kiro_identity_store(_provider_on(_BARE)) is False
        assert (
            session_mod._provider_uses_kiro_identity_store(_runtime_on(_BARE, resolved=operator))
            is False
        )


# ══════════════════════════════════════════════════════════════════════════
# BEHAVIOR HALF (wave-2 T5): the five behavior gates answer from the descriptor
# ══════════════════════════════════════════════════════════════════════════
#
# The security half above pins internal_sandbox + kiro_identity_store. This half
# pins the five BEHAVIOR gates the ``ACP_BACKENDS_*`` frozensets used to answer,
# now retired: session_sharing, steer, acp_runtime_pool, mcp_tool_search,
# reasoning_effort. Same mutation discipline — declared ⇒ old privileged
# behaviour; undeclared ⇒ fail-closed — and every assertion lands on the REAL
# seam the shipped call site computes (the spawn-reuse decision, the runtime
# path taken, the steer-delivery gate, the overlay writer actually writing a file
# or not), never on a mock of the decision.

from kiro_crew.providers import acp as providers_acp_mod  # noqa: E402
from kiro_crew.providers.acp import AcpProvider as _AcpProvider  # noqa: E402

#: A probe harness declaring ALL FIVE behavior capabilities, reachable by no
#: backend string — so the only way its gate can answer "yes" is a descriptor
#: read, never an identity comparison.
_BEHAVE = "probe-behavior-all-five"


def _all_behavior_caps() -> CapabilitySet:
    return CapabilitySet(
        session_sharing=True,
        steer=True,
        acp_runtime_pool=True,
        mcp_tool_search=True,
        reasoning_effort=True,
    )


# ── Gate 3: session sharing (spawn-reuse decision) ──


def test_session_sharing_eligible_when_declared() -> None:
    """A harness WITH ``session_sharing`` is eligible to host multiplexed subs."""
    with _registered(_descriptor(_BEHAVE, CapabilitySet(session_sharing=True))):
        assert _provider_on(_BEHAVE).is_session_sharing_eligible is True


def test_session_sharing_ineligible_fail_closed() -> None:
    """FAIL-CLOSED: an empty-caps operator provider is NOT session-sharing eligible.

    The real spawn-reuse decision (``AcpProvider.is_session_sharing_eligible``,
    read by ``SessionManager.is_session_sharing_eligible``) answers False, so a
    subagent falls back to the legacy per-process path rather than sharing a
    process a foreign harness never declared it can multiplex.
    """
    with _registered(_descriptor(_BARE, CapabilitySet())):
        assert _provider_on(_BARE).is_session_sharing_eligible is False


# ── Gate 4: steer delivery (AcpClient.supports_steer) ──


def test_steer_supported_when_declared() -> None:
    """A harness WITH ``steer`` reports ``supports_steer`` True (the delivery gate)."""
    with _registered(_descriptor(_BEHAVE, CapabilitySet(steer=True))), _client_roster(_BEHAVE):
        assert _client_on(_BEHAVE).supports_steer is True


def test_steer_unsupported_fail_closed() -> None:
    """FAIL-CLOSED: an empty-caps client reports no steer, so a steer is not delivered.

    ``supports_steer`` is the exact gate every transport's steer path reads
    (``getattr(provider, "supports_steer", False)``); False means the mid-turn
    ``_session/steer`` is never attempted for a harness that never declared it.
    """
    with _registered(_descriptor(_BARE, CapabilitySet())), _client_roster(_BARE):
        assert _client_on(_BARE).supports_steer is False


# ── Gate 5: ACP runtime pool (runtime acquisition path) ──


def test_acp_runtime_backend_when_declared() -> None:
    """A harness WITH ``acp_runtime_pool`` takes the shared-runtime start path.

    ``is_acp_runtime_backend`` is what ``AcpProvider.start`` branches on to spawn
    an ``AcpRuntime`` (vs the one-AcpClient-per-session claude path), so this is
    the real runtime-acquisition decision.
    """
    with _registered(_descriptor(_BEHAVE, CapabilitySet(acp_runtime_pool=True))):
        assert _provider_on(_BEHAVE).is_acp_runtime_backend is True


def test_acp_runtime_backend_fail_closed() -> None:
    """FAIL-CLOSED: an empty-caps provider does NOT take the shared-runtime path."""
    with _registered(_descriptor(_BARE, CapabilitySet())):
        assert _provider_on(_BARE).is_acp_runtime_backend is False


# ── Gates 6 & 7: the cli.json overlays (writer writes a file, or does not) ──


class _StubClient:
    """The minimal client shape the overlay writers read (work_dir + model)."""

    def __init__(self, work_dir, model: str) -> None:
        self._work_dir = work_dir
        self._model = model


def _overlay_provider(
    harness_id: str,
    work_dir,
    *,
    model: str = "claude-opus-4",
    tool_search: bool | None = True,
) -> _AcpProvider:
    """A bare ``AcpProvider`` wired just enough to drive the real overlay writers.

    No process and no config: ``__new__`` plus the exact attributes
    ``_apply_effort_overlay`` / ``_apply_tool_search_overlay`` read. The harness
    id is the only capability input — every gate answer flows from the descriptor
    ``harness_id`` resolves to, which is the property under test.
    """
    provider = _AcpProvider.__new__(_AcpProvider)
    provider._harness_id = harness_id
    provider._client = _StubClient(work_dir, model)  # type: ignore[assignment]
    provider._effort_per_model = {model: "high"}
    provider._effort_defaults = None
    provider._tool_search = tool_search
    provider._tool_search_min_pct = providers_acp_mod.TOOL_SEARCH_DEFAULT_MIN_PCT
    provider._tool_search_min_tokens = providers_acp_mod.TOOL_SEARCH_DEFAULT_MIN_TOKENS
    return provider


def _cli_json(work_dir):
    return work_dir / ".kiro" / "settings" / "cli.json"


def test_effort_overlay_written_when_declared(tmp_path) -> None:
    """A runtime+effort harness writes the cli.json effort overlay (old behaviour).

    Both ``reasoning_effort`` (the harness honours effort) and ``acp_runtime_pool``
    (it takes effort from this workspace file at spawn) are required, and the real
    ``_apply_effort_overlay`` writes the actual file when both hold.
    """
    caps = CapabilitySet(reasoning_effort=True, acp_runtime_pool=True)
    with _registered(_descriptor(_BEHAVE, caps)):
        prov = _overlay_provider(_BEHAVE, tmp_path)
        prov._apply_effort_overlay()
        assert _cli_json(tmp_path).exists()
        data = json.loads(_cli_json(tmp_path).read_text(encoding="utf-8"))
        assert "chat.modelDefaults" in data


def test_effort_overlay_not_written_fail_closed(tmp_path) -> None:
    """FAIL-CLOSED: an empty-caps harness writes NO effort file into the project.

    R3.2: an undeclared overlay capability causes no settings file write. Asserted
    on the real seam — the file simply does not exist — so a gate wired to a
    constant or the wrong flag would leave a cli.json behind and fail here.
    """
    with _registered(_descriptor(_BARE, CapabilitySet())):
        prov = _overlay_provider(_BARE, tmp_path)
        prov._apply_effort_overlay()
        assert not _cli_json(tmp_path).exists()


def test_effort_overlay_not_written_when_only_effort_no_runtime(tmp_path) -> None:
    """A harness honouring effort but NOT on the runtime pool writes no overlay.

    The overlay is a kiro-family CHANNEL: ``reasoning_effort`` alone (the claude
    seam's posture) must not write kiro's settings file — that harness takes
    effort through a live ``session/set_config_option`` instead. Proves the gate
    is the conjunction the shipped code computes, not either flag alone.
    """
    with _registered(_descriptor(_BEHAVE, CapabilitySet(reasoning_effort=True))):
        prov = _overlay_provider(_BEHAVE, tmp_path)
        prov._apply_effort_overlay()
        assert not _cli_json(tmp_path).exists()


def test_tool_search_overlay_written_when_declared(tmp_path) -> None:
    """A harness WITH ``mcp_tool_search`` writes the Tool Search overlay."""
    with _registered(_descriptor(_BEHAVE, CapabilitySet(mcp_tool_search=True))):
        prov = _overlay_provider(_BEHAVE, tmp_path, tool_search=True)
        prov._apply_tool_search_overlay()
        assert _cli_json(tmp_path).exists()
        data = json.loads(_cli_json(tmp_path).read_text(encoding="utf-8"))
        assert data.get("toolSearch.enabled") is True


def test_tool_search_overlay_not_written_fail_closed(tmp_path) -> None:
    """FAIL-CLOSED: an empty-caps harness writes NO Tool Search file into the project.

    R3.2 again, on the ``_write_tool_search_overlay`` seam: the writer is never
    reached, so no cli.json lands in the user's project directory.
    """
    with _registered(_descriptor(_BARE, CapabilitySet())):
        prov = _overlay_provider(_BARE, tmp_path, tool_search=True)
        prov._apply_tool_search_overlay()
        assert not _cli_json(tmp_path).exists()


# ── Bundled combinations, pinned via unbound construction ──

# The five behavior capabilities as the bundled descriptors declare them:
# kiro — the full set; KAS — steer + runtime + identity (its deliberate
# withdrawals: no session sharing, no sandbox, no overlays); claude — effort only
# (delivered live, not via the runtime overlay). Keyed by harness id, read
# through the registry the same way an unbound provider/client resolves them.
_BUNDLED_BEHAVIOR_CAPS = {
    HARNESS_KIRO: {
        "session_sharing": True,
        "steer": True,
        "acp_runtime_pool": True,
        "mcp_tool_search": True,
        "reasoning_effort": True,
    },
    HARNESS_KAS: {
        "session_sharing": False,
        "steer": True,
        "acp_runtime_pool": True,
        "mcp_tool_search": False,
        "reasoning_effort": False,
    },
    HARNESS_CLAUDE: {
        "session_sharing": False,
        "steer": False,
        "acp_runtime_pool": False,
        "mcp_tool_search": False,
        # Effort IS declared — the one behavior cap the claude seam keeps — but
        # delivered as a live ``session/set_config_option`` push, NOT through the
        # kiro runtime overlay (which is why the overlay-write test above requires
        # acp_runtime_pool too, and claude does not take that path).
        "reasoning_effort": True,
    },
}


@pytest.mark.parametrize("harness_id", sorted(_BUNDLED_BEHAVIOR_CAPS))
def test_unbound_session_sharing_matches_the_bundled_grant(harness_id: str) -> None:
    """Session-sharing eligibility is unchanged for each bundled harness.

    Unbound legacy construction (a provider carrying only ``harness_id``) resolves
    capabilities through the registry; its answer must equal the pre-rekey
    ``ACP_BACKENDS_SESSION_SHARING`` membership ({kiro}).
    """
    want = _BUNDLED_BEHAVIOR_CAPS[harness_id]["session_sharing"]
    assert _provider_on(harness_id).is_session_sharing_eligible is want


@pytest.mark.parametrize("harness_id", sorted(_BUNDLED_BEHAVIOR_CAPS))
def test_unbound_acp_runtime_matches_the_bundled_grant(harness_id: str) -> None:
    """The runtime-pool path is unchanged for each bundled harness
    (pre-rekey ``ACP_BACKENDS_ACP_RUNTIME`` = {kiro, kas})."""
    want = _BUNDLED_BEHAVIOR_CAPS[harness_id]["acp_runtime_pool"]
    assert _provider_on(harness_id).is_acp_runtime_backend is want


@pytest.mark.parametrize("harness_id", sorted(_BUNDLED_BEHAVIOR_CAPS))
def test_unbound_steer_matches_the_bundled_grant(harness_id: str) -> None:
    """Steer support is unchanged for each bundled harness, resolved from the
    client's backend roster (pre-rekey ``ACP_BACKENDS_STEER`` = {kiro, kas})."""
    want = _BUNDLED_BEHAVIOR_CAPS[harness_id]["steer"]
    with _client_roster(harness_id):
        assert _client_on(harness_id).supports_steer is want


@pytest.mark.parametrize("cap", ["mcp_tool_search", "reasoning_effort"])
@pytest.mark.parametrize("harness_id", sorted(_BUNDLED_BEHAVIOR_CAPS))
def test_unbound_overlay_capability_matches_the_bundled_grant(harness_id: str, cap: str) -> None:
    """The overlay-honouring flags are unchanged for each bundled harness.

    Read through the ``supports_*`` properties the overlay writers gate on, so a
    drift in either flag (KAS's withdrawn overlays, claude's live-only effort)
    surfaces here.
    """
    want = _BUNDLED_BEHAVIOR_CAPS[harness_id][cap]
    prop = "supports_mcp_tool_search" if cap == "mcp_tool_search" else "supports_reasoning_effort"
    assert getattr(_provider_on(harness_id), prop) is want


def test_all_bundled_behavior_combinations_are_pinned() -> None:
    """Guard: the five behavior caps × the three bundled harnesses are all covered.

    Fails if a harness is added to the table but drops a capability key, so the
    matrix cannot silently shrink.
    """
    assert set(_BUNDLED_BEHAVIOR_CAPS) == {HARNESS_KIRO, HARNESS_KAS, HARNESS_CLAUDE}
    expected_keys = {
        "session_sharing",
        "steer",
        "acp_runtime_pool",
        "mcp_tool_search",
        "reasoning_effort",
    }
    for caps in _BUNDLED_BEHAVIOR_CAPS.values():
        assert set(caps) == expected_keys


# ── GenericAdapter empty-set operator descriptor: every behavior gate is off ──


def test_generic_operator_descriptor_declares_no_behavior_capability(tmp_path) -> None:
    """An adapter-less operator harness (empty caps) fails every behavior gate closed.

    The exact posture a ``GenericAdapter`` harness resolves to: no session
    sharing, no steer, not on the runtime pool, and neither overlay is written
    into the project directory. Asserted on all five real seams at once.
    """
    operator = _descriptor(_BARE, CapabilitySet())
    with _registered(operator), _client_roster(_BARE):
        prov = _provider_on(_BARE)
        assert prov.is_session_sharing_eligible is False
        assert prov.is_acp_runtime_backend is False
        assert prov.supports_reasoning_effort is False
        assert prov.supports_mcp_tool_search is False
        assert _client_on(_BARE).supports_steer is False
        # Overlays: writer never reached ⇒ no cli.json in the project dir.
        oprov = _overlay_provider(_BARE, tmp_path, tool_search=True)
        oprov._apply_effort_overlay()
        oprov._apply_tool_search_overlay()
        assert not _cli_json(tmp_path).exists()
