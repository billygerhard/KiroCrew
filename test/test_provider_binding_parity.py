"""Provider construction is equivalent through the old pair and the new binding.

T3 rekeys ``AcpProvider`` construction to carry the session's
:class:`~kiro_crew.acp.harness_selection.HarnessBinding` — descriptor and
``acp_backend`` together — instead of two strings the provider re-derives
independently. The acceptance bar is ZERO behavior change: for every backend a
session could be created on before, the binding path must construct a provider
with the same backend string, the same reported harness, and the same
``is_*_backend`` glue as the legacy two-string path, and the derived adapter and
wire profile must equal the wave-1 expectations table.

The GenericAdapter case pins the design's binding fallback (``acp_backend`` =
descriptor id for a harness with no legacy spelling) at CONSTRUCTION, and — after
T6 flipped serving on — asserts ``resolve_session_harness`` now PRODUCES that
binding for a registered generic row instead of refusing it. Only the
``_UNSERVICEABLE`` map (claude) still refuses a row.
"""

from __future__ import annotations

import pytest

from kiro_crew.acp.harness_adapters import (
    ClaudeAdapter,
    GenericAdapter,
    KasAdapter,
    KiroAdapter,
    adapter_for,
    resolve_spawn_executable,
)
from kiro_crew.acp.harness_descriptor import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    HarnessDescriptor,
    render_argv,
)
from kiro_crew.acp.harness_registry import (
    HARNESS_CLAUDE,
    HARNESS_CODEX,
    HARNESS_KAS,
    HARNESS_KIRO,
    HarnessRegistry,
    registry,
)
from kiro_crew.acp.harness_selection import (
    HarnessBinding,
    resolve_session_harness,
    unserviceable_reason,
)
from kiro_crew.acp.protocol_profile import (
    KAS_PROFILE,
    KIRO_PROFILE,
    STANDARD_ACP_PROFILE,
    profile_for_backend,
)
from kiro_crew.acp.types import legacy_backend_for
from kiro_crew.providers.acp import AcpProvider

# Wave-1 expectations table: (harness id, legacy acp_backend, adapter class,
# wire profile). The one source the parity assertions read, so a drift in any
# of the three derived facts is a single-line failure rather than three.
_EXPECTATIONS = [
    (HARNESS_KIRO, ACP_BACKEND_KIRO, KiroAdapter, KIRO_PROFILE),
    (HARNESS_KAS, ACP_BACKEND_KAS, KasAdapter, KAS_PROFILE),
    (HARNESS_CLAUDE, ACP_BACKEND_CLAUDE, ClaudeAdapter, STANDARD_ACP_PROFILE),
]


def _descriptor(harness_id: str) -> HarnessDescriptor:
    return registry().get(harness_id)


@pytest.mark.parametrize(
    "harness_id,backend,adapter_cls,profile",
    _EXPECTATIONS,
    ids=[e[0] for e in _EXPECTATIONS],
)
def test_binding_and_pair_construct_the_same_provider(harness_id, backend, adapter_cls, profile):
    """The binding path equals the legacy two-string path, per backend.

    Both shapes are driven and their observable construction outputs compared:
    the client backend string, the reported ``harness_id``, and every
    ``is_*_backend`` classification. Construction is side-effect free
    (``AcpProvider.__init__`` builds an ``AcpClient`` without spawning), so this
    needs no process.
    """
    descriptor = _descriptor(harness_id)
    # Legacy shape: the two strings, resolved apart.
    old = AcpProvider(acp_backend=backend, harness_id=harness_id)
    # New shape: the pair carried as one binding.
    binding = HarnessBinding(descriptor=descriptor, acp_backend=backend)
    new = AcpProvider(binding=binding)

    # Same backend string on the client (drives spawn argv + the glue below).
    assert new.client.backend == backend
    assert old.client.backend == new.client.backend
    # Same reported harness (what a session records and every surface reports).
    assert new.harness_id == harness_id
    assert old.harness_id == new.harness_id
    # Same is_*_backend glue — the claude/kas/kiro discriminators are unchanged.
    assert (old.is_kiro_backend, old.is_kas_backend, old.is_claude_backend) == (
        new.is_kiro_backend,
        new.is_kas_backend,
        new.is_claude_backend,
    )

    # Derived facts equal the wave-1 expectations table.
    assert type(adapter_for(descriptor)) is adapter_cls
    assert adapter_for(descriptor).protocol_profile is profile
    # The string->profile mapping (for sites holding only a backend string)
    # agrees with the adapter the binding's descriptor resolves to.
    assert profile_for_backend(backend) is profile

    # Finding 1: the CLIENT the bound provider built speaks the wire profile the
    # binding's adapter declares — threaded at construction, not re-derived from
    # the backend string. For these three bundled rows the string derivation
    # happens to agree, but the assertion pins the SOURCE (adapter, via the
    # threaded override), which is what stops a generic operator harness handshaking
    # on kiro-cli's wire.
    assert new.client._protocol_profile is profile
    assert new.client._protocol_profile is adapter_for(descriptor).protocol_profile


def test_binding_backend_matches_legacy_spelling_for_bundled_rows():
    """The binding's ``acp_backend`` is exactly the legacy spelling, per row.

    A guard that the expectations table is not lying about the pairing: the
    backend the binding carries is the one ``AcpProvider`` spawns as.
    """
    for harness_id, backend, _adapter, _profile in _EXPECTATIONS:
        binding = HarnessBinding(descriptor=_descriptor(harness_id), acp_backend=backend)
        assert binding.acp_backend == backend
        assert binding.harness_id == harness_id


def _operator_descriptor(**overrides) -> HarnessDescriptor:
    """An adapter-less descriptor shaped like an operator's own ACP server.

    No ``adapter`` (resolves to GenericAdapter) and an id that is NOT a legacy
    backend spelling (``legacy_backend_for`` returns None for it).
    """
    fields = dict(
        id="agy",
        display_name="AGY ACP",
        executable="agy-acp",
        argv=("{executable}", "acp"),
    )
    fields.update(overrides)
    return HarnessDescriptor(**fields)


def test_generic_bound_operator_descriptor_constructs_with_descriptor_id_fallback():
    """A GenericAdapter operator binding constructs, backend = descriptor id.

    The design's binding fallback: a harness with no legacy ``acp_backend``
    spelling binds with ``acp_backend = descriptor.id``. Construction must
    succeed and the client's backend string must be that id — it is admitted
    because the binding's descriptor, not a raw config string, is the authority.
    None of the three real backend discriminators may fire for it.
    """
    op = _operator_descriptor()
    binding = HarnessBinding(descriptor=op, acp_backend=op.id)
    provider = AcpProvider(binding=binding)

    assert provider.client.backend == op.id  # descriptor-id fallback
    assert provider.harness_id == op.id
    assert type(adapter_for(op)) is GenericAdapter
    # It is not one of the three real backends.
    assert not provider.is_kiro_backend
    assert not provider.is_kas_backend
    assert not provider.is_claude_backend

    # Finding 1, the case that motivates it: the client speaks the GenericAdapter's
    # PUBLIC-ACP wire, threaded from the binding — NOT the kiro-cli profile the
    # backend-STRING derivation would have handed it. ``op.id`` is an unrecognized
    # backend string, so ``profile_for_backend`` maps it to KIRO_PROFILE; the
    # bound client must ignore that and use STANDARD_ACP_PROFILE.
    assert adapter_for(op).protocol_profile is STANDARD_ACP_PROFILE
    assert provider.client._protocol_profile is STANDARD_ACP_PROFILE
    assert profile_for_backend(op.id) is KIRO_PROFILE  # the wrong answer, avoided
    assert provider.client._protocol_profile is not profile_for_backend(op.id)


def test_generic_bound_client_spawn_argv_derives_from_the_descriptor_not_kiro(tmp_path):
    """T6b: the CLIENT a generic binding builds renders its spawn argv from the
    bound DESCRIPTOR's adapter, not from kiro-cli's hardcoded argv.

    The client's ``_spawn`` takes the descriptor-driven branch for a generic
    binding (decided by adapter identity, never an ``acp_backend ==`` string),
    and the argv it produces goes through the SAME golden seam
    (``resolve_spawn_executable`` + ``render_argv``): ``argv[0]`` is the attested
    executable and the whole list equals the descriptor's rendering. A kiro
    binding does NOT take that branch — its argv stays kiro-cli's.
    """
    # An operator descriptor whose executable is a real, runnable file so
    # resolve_spawn_executable attests it without a host binary.
    tool = tmp_path / "agy-acp"
    tool.write_text("#!/bin/sh\n", encoding="utf-8")
    tool.chmod(0o755)
    op = _operator_descriptor(executable=str(tool), argv=("{executable}", "acp"))
    provider = AcpProvider(binding=HarnessBinding(descriptor=op, acp_backend=op.id))
    client = provider.client

    # Decided by adapter identity: the generic binding takes the descriptor path.
    assert client._spawns_from_descriptor() is True
    # The argv the spawn path would produce is the descriptor's own rendering,
    # anchored on the attested executable — NOT [kiro-cli, acp, --agent, ...].
    attested = resolve_spawn_executable(op)
    expected = render_argv(op, executable=attested, workdir=str(client._work_dir))
    rendered = client._render_descriptor_spawn_argv()
    assert rendered == expected
    assert rendered[0] == attested == str(tool)
    assert "kiro-cli" not in rendered[0]
    assert "--agent" not in rendered

    # A kiro binding is NOT on the generic path — its argv stays kiro-cli's.
    kiro = AcpProvider(
        binding=HarnessBinding(descriptor=_descriptor(HARNESS_KIRO), acp_backend=ACP_BACKEND_KIRO)
    )
    assert kiro.client._spawns_from_descriptor() is False


def test_generic_bound_operator_descriptor_still_admits_backend_only_via_binding():
    """The descriptor-id fallback is admitted ONLY when it rides a binding.

    The stringly-typed ``acp_backend`` path keeps its typo guard: an unknown
    backend with no binding still raises, so relaxing the guard for the binding
    fallback changed nothing for the legacy path.
    """
    op = _operator_descriptor()
    with pytest.raises(ValueError, match="Unknown acp_backend"):
        AcpProvider(acp_backend=op.id, harness_id=op.id)


def test_generic_operator_serving_is_enabled_this_wave(monkeypatch, tmp_path):
    """T6 flips serving ON: a REGISTERED generic row with no legacy spelling now
    produces a usable binding at resolution, not a refusal.

    This is the inversion of the wave-1/T3 pin. The bundled Codex row (generic
    adapter, no ``acp_backend`` spelling) is made AVAILABLE, and
    ``resolve_session_harness`` returns a :class:`HarnessBinding` whose
    ``acp_backend`` is the descriptor id — the generic fallback that construction
    already admitted. The selection surface marks the row serviceable.
    """
    # A fresh registry over an empty operator config still carries the bundled
    # rows, Codex among them; install it as THE registry for this test.
    reg = HarnessRegistry()
    monkeypatch.setattr("kiro_crew.acp.harness_registry._REGISTRY", reg)
    # Make Codex available so it is not a missing binary that refuses it. (The
    # binary is absent in CI.)
    monkeypatch.setattr(
        "kiro_crew.acp.harness_registry.resolve_executable",
        lambda descriptor: (str(tmp_path / "codex"), ""),
    )

    # Codex is a registered generic row with no legacy backend spelling.
    assert type(adapter_for(registry().get(HARNESS_CODEX))) is GenericAdapter
    binding = resolve_session_harness(HARNESS_CODEX)
    assert binding.harness_id == HARNESS_CODEX
    assert binding.acp_backend == HARNESS_CODEX  # descriptor-id fallback
    # And it constructs a provider end to end (no live spawn).
    provider = AcpProvider(binding=binding)
    assert provider.client.backend == HARNESS_CODEX
    assert provider.harness_id == HARNESS_CODEX
    # The selection surface now marks the generic row serviceable, and after
    # #7301 claude is a serviceable public backend too — nothing bundled is
    # refused by build posture any more (the _UNSERVICEABLE map is empty).
    assert unserviceable_reason(HARNESS_CODEX) == ""
    assert unserviceable_reason(HARNESS_KIRO) == ""
    assert unserviceable_reason(HARNESS_CLAUDE) == ""


def test_operator_descriptor_naming_claude_agent_acp_is_serviceable(monkeypatch, tmp_path):
    """R4.2 nuance: only the BUNDLED claude ROW is refused — by its harness id, not
    by executable name. An operator MAY author their own descriptor whose
    executable happens to be ``claude-agent-acp``; that goes through the generic
    path and must NOT be specially blocked.

    The refusal lives in the registry's ``_UNSERVICEABLE`` map keyed on
    ``HARNESS_CLAUDE`` (the bundled id), so an operator row with a different id is
    serviceable even when it points at the same binary.
    """
    op = _operator_descriptor(
        id="my-claude",
        display_name="My Claude (operator)",
        executable="claude-agent-acp",
        argv=("{executable}", "acp"),
    )
    reg = HarnessRegistry()
    reg._ensure_loaded()
    with reg._lock:
        reg._descriptors[op.id] = op
    monkeypatch.setattr("kiro_crew.acp.harness_registry._REGISTRY", reg)
    monkeypatch.setattr(
        "kiro_crew.acp.harness_registry.resolve_executable",
        lambda descriptor: (str(tmp_path / "claude-agent-acp"), ""),
    )

    # Not blocked: it has no legacy spelling and is not the bundled claude id.
    assert legacy_backend_for(op.id) is None
    assert unserviceable_reason(op.id) == ""
    binding = resolve_session_harness(op.id)
    assert binding.harness_id == op.id
    assert binding.acp_backend == op.id  # generic fallback, serves via GenericAdapter
    assert type(adapter_for(op)) is GenericAdapter
    provider = AcpProvider(binding=binding)
    assert provider.client.backend == op.id
    assert not provider.is_claude_backend


# ── Finding 2: a binding that disagrees with a non-empty explicit pair is refused ──


def test_binding_disagreeing_with_explicit_acp_backend_raises():
    """A binding plus a non-empty explicit ``acp_backend`` naming a different
    backend is two contradictory statements — refuse, naming both values."""
    binding = HarnessBinding(descriptor=_descriptor(HARNESS_KIRO), acp_backend=ACP_BACKEND_KIRO)
    with pytest.raises(ValueError, match="disagrees with binding"):
        AcpProvider(binding=binding, acp_backend=ACP_BACKEND_CLAUDE)


def test_binding_disagreeing_with_explicit_harness_id_raises():
    """A binding plus a non-empty explicit ``harness_id`` naming a different
    harness is refused, naming both values."""
    binding = HarnessBinding(descriptor=_descriptor(HARNESS_KIRO), acp_backend=ACP_BACKEND_KIRO)
    with pytest.raises(ValueError, match="disagrees with binding"):
        AcpProvider(binding=binding, harness_id=HARNESS_CLAUDE)


def test_binding_agreeing_empty_defaults_do_not_disagree():
    """The empty defaults are NOT a disagreement. ``acp_backend=""`` is kiro-cli's
    real spelling and ``harness_id=""`` is "unspecified" — a binding alongside the
    untouched defaults must construct, taking both halves from the binding."""
    binding = HarnessBinding(descriptor=_descriptor(HARNESS_CLAUDE), acp_backend=ACP_BACKEND_CLAUDE)
    # acp_backend defaults to "" (kiro's spelling), harness_id to "" — neither is
    # a non-empty explicit statement, so the binding wins with no error.
    provider = AcpProvider(binding=binding)
    assert provider.client.backend == ACP_BACKEND_CLAUDE
    assert provider.harness_id == HARNESS_CLAUDE


def test_binding_agreeing_explicit_pair_is_allowed():
    """A binding whose non-empty explicit pair AGREES is fine (session.py's own
    prior call passed exactly this before finding 3 dropped the redundant pair)."""
    binding = HarnessBinding(descriptor=_descriptor(HARNESS_KAS), acp_backend=ACP_BACKEND_KAS)
    provider = AcpProvider(binding=binding, acp_backend=ACP_BACKEND_KAS, harness_id=HARNESS_KAS)
    assert provider.client.backend == ACP_BACKEND_KAS
    assert provider.harness_id == HARNESS_KAS


# ── Finding 4: a non-HarnessBinding ``binding`` is refused, naming the type ──


def test_non_harnessbinding_binding_is_refused_naming_the_type():
    """A wrong type forwarded as ``binding`` (the loader's param is only
    TYPE_CHECKING-annotated, so runtime cannot rely on it) is refused with a
    ValueError naming the received type — not an opaque ``AttributeError`` on
    ``binding.acp_backend``."""
    with pytest.raises(ValueError, match="binding must be a HarnessBinding or None, got dict"):
        AcpProvider(binding={"acp_backend": "kiro"})  # type: ignore[arg-type]


# ── Finding 5: drive the REAL loader ``_acp`` factory path ──


def test_real_loader_factory_honours_a_resolved_binding(tmp_path, monkeypatch):
    """The loader's ``_acp`` factory, called with a resolved ``HarnessBinding``,
    builds a provider whose client.backend / harness_id match the binding.

    This is the seam finding 3 changed (session.py passes ONLY ``binding=`` now)
    and finding 4 typed — hand-constructing ``AcpProvider`` would miss a factory
    that dropped the binding, so drive the real factory the loader produces.
    """
    from kiro_crew.acp.harness_selection import resolve_session_harness
    from kiro_crew.config.loader import KiroCrewConfig

    # kiro-cli's availability probe resolves the real binary, which is on a dev
    # box's PATH but NOT on CI — resolving the kiro harness would raise
    # ``HarnessUnavailable`` there. Stub the resolver to a real non-empty
    # executable, the same seam every availability test in
    # test_harness_registry.py uses, so the factory (the thing under test) still
    # runs against a serviceable kiro row without depending on the host.
    kiro_bin = tmp_path / "kiro-cli"
    kiro_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    kiro_bin.chmod(0o755)
    monkeypatch.setattr(
        "kiro_crew.kiro_cli.resolve_kiro_cli", lambda *a, **k: str(kiro_bin), raising=True
    )
    # The GLOBAL registry keeps a TTL'd probe-failure ledger, and earlier tests
    # in a shared process (the spawn-shield/offload suites deliberately fail
    # kiro spawns) can leave a recorded failure that ``require_available``
    # honours — refusing this resolution with the STALE reason for the rest of
    # the TTL window. That is the shipped behaviour for real sessions; for this
    # test it is cross-test state, so start from a clean ledger the way a
    # successful spawn would.
    from kiro_crew.acp.harness_registry import registry

    registry().clear_probe_failure(HARNESS_KIRO)

    cfg = KiroCrewConfig()
    # Resolve a real binding for the kiro harness — the one bundled row that is
    # always available in CI (no external binary) AND carries a legacy spelling,
    # so it is serviceable this wave and resolution does not raise.
    binding = resolve_session_harness(HARNESS_KIRO, cfg)
    assert isinstance(binding, HarnessBinding)
    provider = cfg.create_provider_factory()(session_key="test:binding", binding=binding)
    assert provider.client.backend == binding.acp_backend == ACP_BACKEND_KIRO
    assert provider.harness_id == binding.harness_id == HARNESS_KIRO
    # And the wire profile came from the binding's adapter, through the factory.
    assert provider.client._protocol_profile is adapter_for(binding.descriptor).protocol_profile


def test_real_loader_factory_refuses_a_non_binding_type():
    """The loader factory forwards a non-``HarnessBinding`` ``binding`` and the
    provider refuses it with the typed error — the negative half of finding 5."""
    from kiro_crew.config.loader import KiroCrewConfig

    cfg = KiroCrewConfig()
    with pytest.raises(ValueError, match="binding must be a HarnessBinding or None, got dict"):
        cfg.create_provider_factory()(
            session_key="test:badbinding", binding={"acp_backend": "kiro"}
        )
