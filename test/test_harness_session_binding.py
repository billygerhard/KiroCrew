"""Session harness binding, the selection composition, and the harness APIs.

What must hold once a session can name its harness:

1. **The composition of the two config keys.** ``agent.default_harness`` outranks
   the legacy ``agent.acp_backend``, and the legacy key is read RAW — a stored
   ``codex`` is clamped away from ``AgentConfig.acp_backend``, so a surface reading
   only that field would silently start sessions on kiro-cli instead of refusing to
   start one on the harness the operator named.
2. **Refusal over substitution.** An unknown, unavailable, or unserviceable
   selection raises with the harness named; nothing falls back.
3. **The binding is a snapshot.** It is recorded at creation, survives a persisted
   default change, and a resume reads the session's own recording rather than the
   current default.
4. **The empty selection is today's kiro path.** The golden assertion: no
   selection resolves to kiro-cli, spawns with the same backend spelling, and asks
   nothing of the harness's availability that the pre-binding path did not.
5. **One harness's trouble is its own.** An unavailable harness leaves sessions on
   other harnesses untouched.
"""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from dataclasses import replace

import pytest
from aiohttp.test_utils import TestClient, TestServer
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from kiro_crew.acp.harness_registry import (
    HARNESS_CODEX,
    HARNESS_KAS,
    HARNESS_KIRO,
    HarnessRegistry,
    HarnessUnavailable,
    UnknownHarness,
)
from kiro_crew.acp.harness_registry import registry as harness_registry
from kiro_crew.acp.harness_selection import (
    HarnessBindingConflict,
    HarnessNotServiceable,
    default_harness_id,
    resolve_session_harness,
    unserviceable_reason,
)
from kiro_crew.acp.types import ACP_BACKEND_KAS, ACP_BACKEND_KIRO, legacy_backend_for
from kiro_crew.config.loader import KiroCrewConfig


def _write_config(payload: dict) -> None:
    """Write ``config.json`` under this test's pinned data home."""
    from kiro_crew.config.loader import config_dir, config_path

    config_dir().mkdir(parents=True, exist_ok=True)
    config_path().write_text(json.dumps(payload), encoding="utf-8")


@pytest.fixture
def registry_from_config(monkeypatch):
    """Install a fresh registry over a config this test wrote."""

    def _install(agent: dict) -> HarnessRegistry:
        _write_config({"agent": agent})
        reg = HarnessRegistry()
        monkeypatch.setattr("kiro_crew.acp.harness_registry._REGISTRY", reg)
        return reg

    return _install


# ── 1. Composing the two config keys ──


def test_no_configuration_at_all_resolves_to_kiro(registry_from_config):
    registry_from_config({})
    assert default_harness_id(KiroCrewConfig.load()) == HARNESS_KIRO


def test_the_legacy_backend_key_still_selects_its_harness(registry_from_config):
    """An operator whose only harness statement is ``acp_backend`` keeps it.

    Without the composition the registry's ``default()`` reads
    ``agent.default_harness`` only, so this config would silently start sessions on
    kiro-cli — the harness the operator did not ask for.
    """
    registry_from_config({"acp_backend": ACP_BACKEND_KAS})
    assert default_harness_id(KiroCrewConfig.load()) == HARNESS_KAS


def test_the_default_harness_key_outranks_the_legacy_one(registry_from_config):
    registry_from_config({"acp_backend": ACP_BACKEND_KAS, "default_harness": HARNESS_KIRO})
    assert default_harness_id(KiroCrewConfig.load()) == HARNESS_KIRO


def test_one_config_read_decides_the_default(registry_from_config, monkeypatch):
    """The gate and the answer come from the SAME config object.

    Taking the precedence gate from the caller's config and the id from the
    registry's own load is how the default ``/api/harnesses`` advertises and the
    harness an unselected session binds come to disagree: a caller holding a
    config from before an edit would gate on its own ``default_harness`` and then
    be handed the id the newer file names.
    """
    # Availability is a question about the MACHINE, and this test is about the
    # config read, so every harness resolves an executable here.
    monkeypatch.setattr(
        "kiro_crew.acp.harness_registry.resolve_executable",
        lambda descriptor: (f"/usr/bin/{descriptor.executable}", ""),
    )
    registry_from_config({"default_harness": HARNESS_KAS})
    stale = KiroCrewConfig.load()
    # The operator now names a different default. The fresh read follows the file;
    # the holder of the older snapshot follows what THAT snapshot says, rather
    # than a mixture of the two.
    registry_from_config({"default_harness": HARNESS_CODEX})
    assert default_harness_id() == HARNESS_CODEX
    assert default_harness_id(stale) == HARNESS_KAS


def test_an_unusable_default_harness_key_degrades_to_kiro(registry_from_config):
    """An operator's unusable newer statement still leaves a working gateway.

    The value is ignored with a logged reason instead of raising, and it degrades
    to kiro-cli rather than to the legacy key: ``default_harness`` outranks
    ``acp_backend``, so a broken value must not silently promote the older one.
    """
    registry_from_config({"acp_backend": ACP_BACKEND_KAS, "default_harness": "not-a-harness"})
    assert default_harness_id(KiroCrewConfig.load()) == HARNESS_KIRO


def test_a_stored_codex_backend_survives_the_selectable_clamp(registry_from_config):
    """The raw spelling reaches the alias table even though the field is clamped.

    ``codex`` is not selectable, so ``AgentConfig.acp_backend`` reads back as the
    default. Alias resolution must see what was STORED — otherwise the operator's
    value resolves to kiro-cli and they are never told their harness was ignored.
    """
    registry_from_config({"acp_backend": HARNESS_CODEX})
    cfg = KiroCrewConfig.load()
    assert cfg.agent.acp_backend == ACP_BACKEND_KIRO  # the clamp still applies
    assert cfg.agent.acp_backend_alias == HARNESS_CODEX
    assert default_harness_id(cfg) == HARNESS_CODEX


def test_the_raw_backend_spelling_is_never_written_back(registry_from_config):
    """It describes the read, not the operator's settings.

    A private field that reached ``to_dict`` would appear in ``config.json`` as a
    key nobody set, and the next load would treat it as configuration.
    """
    registry_from_config({"acp_backend": HARNESS_CODEX})
    emitted = KiroCrewConfig.load().to_dict()["agent"]
    assert "_acp_backend_stored" not in emitted
    assert not [k for k in emitted if k.startswith("_")]


# ── 2. Refusal over substitution ──


def test_an_unknown_harness_is_refused_by_name(registry_from_config):
    registry_from_config({})
    with pytest.raises(UnknownHarness) as excinfo:
        resolve_session_harness("not-a-harness", KiroCrewConfig.load())
    assert "not-a-harness" in str(excinfo.value)


def test_an_unavailable_harness_is_refused_with_its_reason(registry_from_config, monkeypatch):
    """A harness whose executable does not resolve refuses, and says why."""
    reg = registry_from_config({})
    monkeypatch.setattr(
        "kiro_crew.acp.harness_registry.resolve_executable",
        lambda descriptor: ("", f"{descriptor.executable!r} was not found on PATH"),
    )
    with pytest.raises(HarnessUnavailable) as excinfo:
        resolve_session_harness(HARNESS_KIRO, KiroCrewConfig.load())
    assert excinfo.value.harness_id == HARNESS_KIRO
    assert "not found" in excinfo.value.reason
    # The refusal is the registry's, so the listing shows the same reason.
    assert reg.availability(HARNESS_KIRO)[1] == excinfo.value.reason


def test_a_harness_with_no_legacy_spelling_binds_on_its_descriptor_id(
    registry_from_config, monkeypatch, tmp_path
):
    """Codex is available here, and now serves through the generic adapter.

    Wave 2 rekeyed every capability gate onto the bound descriptor and gave the
    provider a generic-fallback admission (``acp_backend == descriptor.id``), so a
    harness with no legacy ``acp_backend`` spelling is no longer refused: it binds
    with its own id and construction succeeds. The build no longer refuses Codex
    for lacking a spelling — only real unavailability (missing binary, probe
    failure) or the ``_UNSERVICEABLE`` map (claude) can refuse a row.
    """
    registry_from_config({})
    monkeypatch.setattr(
        "kiro_crew.acp.harness_registry.resolve_executable",
        lambda descriptor: (str(tmp_path / "codex"), ""),
    )
    binding = resolve_session_harness(HARNESS_CODEX, KiroCrewConfig.load())
    assert binding.harness_id == HARNESS_CODEX
    # Codex has no legacy spelling, so the fallback binds on the descriptor id.
    assert legacy_backend_for(HARNESS_CODEX) is None
    assert binding.acp_backend == HARNESS_CODEX
    # The selection surface now marks the row serviceable (empty reason), and it
    # is genuinely per-harness — claude still carries its refusal.
    assert unserviceable_reason(HARNESS_CODEX) == ""
    assert unserviceable_reason(HARNESS_KIRO) == ""


def test_a_recent_spawn_failure_blocks_a_fresh_pick_but_not_a_resume(
    registry_from_config, resolvable_executables
):
    """The record steers a CHOICE; it must not strand work already bound.

    Nothing clears a recorded failure except a successful spawn, so honouring it
    on the resume path would refuse every resume for the whole failure window —
    including after the operator signed in, since the refusal is what keeps a spawn
    from being attempted.
    """
    reg = registry_from_config({})
    reg.note_probe_failure(HARNESS_KIRO, "harness 'kiro' exited during ACP initialize")
    cfg = KiroCrewConfig.load()
    with pytest.raises(HarnessUnavailable):
        resolve_session_harness(HARNESS_KIRO, cfg)
    binding = resolve_session_harness(HARNESS_KIRO, cfg, recorded=True)
    assert binding.harness_id == HARNESS_KIRO


# ── 3 + 4. The binding, and the golden empty-selection path ──


def test_the_empty_selection_is_the_kiro_path(registry_from_config):
    """Golden: no selection binds kiro-cli with the backend spelling it always had.

    Both halves matter. The id is what the session records and every surface
    reports; the spelling is what the provider is constructed with, and it must be
    ``ACP_BACKEND_KIRO`` — anything else would change what every capability gate
    answers for a session nobody configured.
    """
    registry_from_config({})
    binding = resolve_session_harness("", KiroCrewConfig.load())
    assert binding.harness_id == HARNESS_KIRO
    assert binding.acp_backend == ACP_BACKEND_KIRO
    assert binding.display_name == "Kiro CLI"


def test_the_empty_selection_does_not_probe_availability(registry_from_config, monkeypatch):
    """A machine with no kiro-cli still creates the session, failing at the spawn.

    Probing here would move the failure earlier and change the error a signed-out
    or mid-install user sees on an ordinary new chat — the one path that must stay
    byte-for-byte what it was.
    """
    registry_from_config({})

    def _never(descriptor):  # pragma: no cover - the assertion is that it is unused
        raise AssertionError("the default path must not resolve an executable")

    monkeypatch.setattr("kiro_crew.acp.harness_registry.resolve_executable", _never)
    assert resolve_session_harness("", KiroCrewConfig.load()).harness_id == HARNESS_KIRO


def test_a_changed_default_does_not_retarget_a_bound_runtime(registry_from_config, tmp_path):
    """The descriptor a runtime resolved is cached for its life.

    A persisted default change between two spawn attempts on the same process
    would otherwise hand it two different harnesses.
    """
    from kiro_crew.acp.runtime import AcpRuntime

    registry_from_config({})
    runtime = AcpRuntime(work_dir=tmp_path, harness_id=HARNESS_KIRO)
    assert runtime.harness_id == HARNESS_KIRO
    first = runtime._harness()
    registry_from_config({"default_harness": HARNESS_KAS})
    assert runtime._harness() is first


def test_a_runtime_bound_to_a_harness_ignores_the_legacy_alias(registry_from_config, tmp_path):
    """The explicit binding is the authority, not the backend it was keyed on."""
    from kiro_crew.acp.runtime import AcpRuntime

    registry_from_config({})
    runtime = AcpRuntime(work_dir=tmp_path, acp_backend=ACP_BACKEND_KAS, harness_id=HARNESS_KIRO)
    assert runtime._harness().id == HARNESS_KIRO


def test_an_unbound_runtime_still_reports_the_harness_it_runs(registry_from_config, tmp_path):
    """A warm-pool process nobody bound must not report an empty harness.

    Reporting "" would leave every usage row and every auth message for the
    pre-binding paths unattributed, which is what the id exists to fix.
    """
    from kiro_crew.acp.runtime import AcpRuntime

    registry_from_config({})
    assert AcpRuntime(work_dir=tmp_path).harness_id == HARNESS_KIRO
    assert AcpRuntime(work_dir=tmp_path, acp_backend=ACP_BACKEND_KAS).harness_id == HARNESS_KAS


def test_the_session_map_records_the_harness_beside_the_sid():
    """The sid names a conversation in one harness's store; the two travel together."""
    from kiro_crew.session_map import SessionMap

    # The map writes under the test's pinned data home (conftest), so no path
    # argument exists to redirect — and none is needed.
    smap = SessionMap()
    smap.set("dashboard:1", "sid-1", provider="acp", harness=HARNESS_KAS)
    assert smap.get_harness("dashboard:1") == HARNESS_KAS
    # A later write that records no harness must not erase the binding: every
    # other field on this method behaves that way, and a caller with nothing to
    # record is not making a statement about the harness.
    smap.set("dashboard:1", "sid-2", provider="acp")
    assert smap.get_harness("dashboard:1") == HARNESS_KAS
    # A write that DOES name one updates the existing entry — this is how a
    # harness switch on a live conversation is persisted.
    smap.set("dashboard:1", "sid-3", provider="acp", harness=HARNESS_KIRO)
    assert smap.get_harness("dashboard:1") == HARNESS_KIRO


def test_a_legacy_entry_reports_no_harness():
    """Every entry written before binding existed says '' — read as "the default"."""
    from kiro_crew.session_map import SessionMap

    smap = SessionMap()
    smap.set("dashboard:9", "sid-9", provider="acp")
    assert smap.get_harness("dashboard:9") == ""


# ── 5. One harness's trouble is its own ──


def test_an_unavailable_harness_leaves_other_harnesses_selectable(
    registry_from_config, resolvable_executables
):
    """A recorded failure is stored per harness, so it cannot spread."""
    reg = registry_from_config({})
    reg.note_probe_failure(HARNESS_KAS, "kas exited during ACP initialize")
    rows = {row.id: row for row in reg.list()}
    assert rows[HARNESS_KAS].available is False
    assert rows[HARNESS_KIRO].reason == "" or rows[HARNESS_KIRO].available is True
    # And the default path is unaffected: a session created with no selection
    # still binds kiro-cli.
    assert resolve_session_harness("", KiroCrewConfig.load()).harness_id == HARNESS_KIRO


# ── Session creation: what the binding does to the session ──


@pytest.fixture
def resolvable_executables(monkeypatch):
    """Make every harness's executable resolve, so availability is not the subject."""
    monkeypatch.setattr(
        "kiro_crew.acp.harness_registry.resolve_executable",
        lambda descriptor: (f"/usr/bin/{descriptor.executable}", ""),
    )


def _manager(monkeypatch, factory):
    """A SessionManager over *factory*, with the pool and cleanup loop inert."""
    from kiro_crew.session import SessionManager

    cfg = KiroCrewConfig.load()
    cfg.session.timeout_secs = 60
    mgr = SessionManager(cfg, provider_factory=factory)
    mgr._pool_size = 0
    return mgr


def _recording_factory(calls: list[dict]):
    """A provider factory that records its kwargs and returns a live-looking double."""
    from unittest.mock import AsyncMock

    def factory(session_key=None, agent=None, channel_id=None, **kwargs):
        calls.append({"session_key": session_key, **kwargs})
        provider = AsyncMock()
        provider.start = AsyncMock()
        provider.shutdown = AsyncMock()
        provider.is_process_alive = lambda: True
        provider.is_alive = lambda: True
        provider.context_usage_pct = lambda: 0.0
        provider.has_active_turn = lambda: False
        return provider

    return factory


@pytest.mark.asyncio
async def test_an_explicit_selection_reaches_the_factory_as_both_halves(
    registry_from_config, resolvable_executables, monkeypatch
):
    """The provider is constructed with the harness id AND its backend spelling.

    Passing only the id would leave the provider keyed on the configured backend —
    a session labelled KAS running kiro-cli.
    """
    registry_from_config({})
    calls: list[dict] = []
    mgr = _manager(monkeypatch, _recording_factory(calls))

    provider, _is_new, _resumed = await mgr.get_or_create("dashboard:kas", harness=HARNESS_KAS)
    mgr.release("dashboard:kas")

    # Finding 3: the factory now receives ONE binding object, not the redundant
    # two-string pair. Both halves are read off it — the id a session records and
    # the backend its process spawns as, derived from the same unit.
    _binding = calls[-1]["binding"]
    assert _binding.harness_id == HARNESS_KAS
    assert _binding.acp_backend == ACP_BACKEND_KAS
    assert mgr._sessions["dashboard:kas"].harness_id == HARNESS_KAS
    assert mgr.get_harness("dashboard:kas") == HARNESS_KAS
    assert provider is not None


@pytest.mark.asyncio
async def test_an_unavailable_selection_refuses_before_any_process_exists(
    registry_from_config, monkeypatch
):
    """The refusal lands before the factory is called at all.

    A refusal after the spawn would leave a harness the caller was told it could
    not have already running, and the caller with an error.
    """
    registry_from_config({})
    monkeypatch.setattr(
        "kiro_crew.acp.harness_registry.resolve_executable",
        lambda descriptor: ("", f"{descriptor.executable!r} was not found on PATH"),
    )
    calls: list[dict] = []
    mgr = _manager(monkeypatch, _recording_factory(calls))

    with pytest.raises(HarnessUnavailable) as excinfo:
        await mgr.get_or_create("dashboard:gone", harness=HARNESS_KAS)

    assert excinfo.value.harness_id == HARNESS_KAS
    assert calls == []
    assert "dashboard:gone" not in mgr._sessions


@pytest.mark.asyncio
async def test_a_resumed_session_goes_back_to_its_own_harness(
    registry_from_config, resolvable_executables, monkeypatch
):
    """A changed default does not retarget a session that already has a transcript.

    The recording is read instead of the default, because the stored native session
    id names a conversation in that harness's store — resuming it anywhere else
    replays one harness's conversation into another.
    """
    registry_from_config({})
    calls: list[dict] = []
    mgr = _manager(monkeypatch, _recording_factory(calls))
    # Written through the manager's OWN map: a second SessionMap instance defers
    # its disk flush to the loop, so the manager would read the file before the
    # write landed and the test would pass or fail on flush timing.
    mgr._session_map.set("dashboard:resume", "sid-r", provider="acp", harness=HARNESS_KAS)
    # The transcript has to exist: the map self-prunes an entry whose kiro session
    # files are gone, which drops the harness recording with it — correctly, since
    # there is then nothing to resume and the session is a fresh one. Both files
    # matter (``SessionMap.get`` treats a near-empty ``.jsonl`` as stale), and the
    # directory comes from the map's own resolver so the test writes where it reads.
    from kiro_crew.session_map import _kiro_sessions_dir

    sessions_dir = _kiro_sessions_dir()
    sessions_dir.mkdir(parents=True, exist_ok=True)
    (sessions_dir / "sid-r.json").write_text("{}", encoding="utf-8")
    (sessions_dir / "sid-r.jsonl").write_text('{"role":"user"}\n', encoding="utf-8")
    # The configured default is kiro-cli; the session's recording says KAS.
    assert default_harness_id(mgr._cfg) == HARNESS_KIRO

    await mgr.get_or_create("dashboard:resume")
    mgr.release("dashboard:resume")

    assert calls[-1]["binding"].harness_id == HARNESS_KAS


@pytest.mark.asyncio
async def test_an_explicit_harness_that_disagrees_with_the_recording_is_refused(
    registry_from_config, resolvable_executables, monkeypatch
):
    """A selection does not outrank where the conversation actually lives.

    Honouring it would issue ``session/load`` for a KAS-minted id on kiro-cli:
    nothing loads, a fresh conversation starts, and the map still reports the
    session as resumed. The refusal names both harnesses, and it fires before any
    process exists — no session is registered and no provider is built.
    """
    registry_from_config({})
    calls: list[dict] = []
    mgr = _manager(monkeypatch, _recording_factory(calls))
    mgr._session_map.set("dashboard:conflict", "sid-c", provider="acp", harness=HARNESS_KAS)

    from kiro_crew.session_map import _kiro_sessions_dir

    sessions_dir = _kiro_sessions_dir()
    sessions_dir.mkdir(parents=True, exist_ok=True)
    (sessions_dir / "sid-c.json").write_text("{}", encoding="utf-8")
    (sessions_dir / "sid-c.jsonl").write_text('{"role":"user"}\n', encoding="utf-8")

    with pytest.raises(HarnessBindingConflict) as excinfo:
        await mgr.get_or_create("dashboard:conflict", harness=HARNESS_KIRO)
    assert excinfo.value.recorded == HARNESS_KAS
    assert excinfo.value.requested == HARNESS_KIRO
    assert HARNESS_KAS in str(excinfo.value)
    assert HARNESS_KIRO in str(excinfo.value)
    assert calls == []
    assert "dashboard:conflict" not in mgr._sessions
    # The recording is untouched, so the conversation stays resumable on its own
    # harness — a refusal must not cost the session its binding.
    assert mgr._session_map.get_harness("dashboard:conflict") == HARNESS_KAS


@pytest.mark.asyncio
async def test_an_explicit_harness_agreeing_with_the_recording_resumes(
    registry_from_config, resolvable_executables, monkeypatch
):
    """Naming the harness a session already runs on is not a disagreement.

    The surfaces that pass a stored selection on every run (a cron job, a task
    runner) send the same id each time, so refusing an AGREEING selection would
    break every repeat run of a job that names its harness.
    """
    registry_from_config({})
    calls: list[dict] = []
    mgr = _manager(monkeypatch, _recording_factory(calls))
    mgr._session_map.set("dashboard:agree", "sid-a", provider="acp", harness=HARNESS_KAS)

    from kiro_crew.session_map import _kiro_sessions_dir

    sessions_dir = _kiro_sessions_dir()
    sessions_dir.mkdir(parents=True, exist_ok=True)
    (sessions_dir / "sid-a.json").write_text("{}", encoding="utf-8")
    (sessions_dir / "sid-a.jsonl").write_text('{"role":"user"}\n', encoding="utf-8")

    await mgr.get_or_create("dashboard:agree", harness=HARNESS_KAS)
    mgr.release("dashboard:agree")
    assert calls[-1]["binding"].harness_id == HARNESS_KAS


@pytest.mark.asyncio
async def test_an_explicit_harness_disagreeing_with_a_LIVE_session_is_refused(
    registry_from_config, resolvable_executables, monkeypatch
):
    """The live-session fast path returns before the recorded check ever runs.

    Reachable when a stored selection is edited while its session is still inside
    the idle timeout — a cron job whose harness changed between runs. Without the
    refusal the fire-time gate blesses the new id, the old process serves the work,
    and the run is reported as a success on a harness that never saw it.
    """
    registry_from_config({})
    calls: list[dict] = []
    mgr = _manager(monkeypatch, _recording_factory(calls))

    await mgr.get_or_create("cron:j1", harness=HARNESS_KAS)
    mgr.release("cron:j1")
    assert mgr.get_harness("cron:j1") == HARNESS_KAS

    with pytest.raises(HarnessBindingConflict) as excinfo:
        await mgr.get_or_create("cron:j1", harness=HARNESS_KIRO)
    assert excinfo.value.recorded == HARNESS_KAS
    assert excinfo.value.requested == HARNESS_KIRO
    # Refused, not reconciled: no second provider was built and the live session
    # keeps its own binding, so the next run on the harness it has still works.
    assert len(calls) == 1
    assert mgr.get_harness("cron:j1") == HARNESS_KAS


@pytest.mark.asyncio
async def test_an_agreeing_harness_reuses_the_live_session(
    registry_from_config, resolvable_executables, monkeypatch
):
    """The surfaces that thread a stored selection pass it on EVERY run.

    Cron threads ``job.harness`` each time it fires, so refusing an agreeing id
    would break every repeat run of a job that names its harness — the check has
    to fire on disagreement only.
    """
    registry_from_config({})
    calls: list[dict] = []
    mgr = _manager(monkeypatch, _recording_factory(calls))

    first, _is_new, _resumed = await mgr.get_or_create("cron:j2", harness=HARNESS_KAS)
    mgr.release("cron:j2")
    again, _is_new, _resumed = await mgr.get_or_create("cron:j2", harness=HARNESS_KAS)
    mgr.release("cron:j2")

    assert again is first  # reused, not rebuilt
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_a_session_with_no_binding_is_not_a_disagreement(
    registry_from_config, resolvable_executables, monkeypatch
):
    """A live session created before bindings existed carries ``""``.

    Reading that as a disagreement would refuse the first selection every
    pre-binding session is ever handed.
    """
    registry_from_config({})
    calls: list[dict] = []
    mgr = _manager(monkeypatch, _recording_factory(calls))

    await mgr.get_or_create("dashboard:legacy")
    mgr.release("dashboard:legacy")
    mgr._sessions["dashboard:legacy"].harness_id = ""  # a pre-binding session

    provider, _is_new, _resumed = await mgr.get_or_create("dashboard:legacy", harness=HARNESS_KAS)
    mgr.release("dashboard:legacy")
    assert provider is not None
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_a_prebound_selection_is_not_gated_by_a_recorded_spawn_failure(
    registry_from_config, resolvable_executables, monkeypatch
):
    """The spawn path resolves and validates BEFORE it dispatches.

    A recorded failure describes one attempt and only a successful spawn clears
    it, so re-asking that question at session creation would refuse the work the
    pre-dispatch gate just admitted — for the whole failure window, with the
    refusal preventing the spawn that would clear it.
    """
    reg = registry_from_config({})
    calls: list[dict] = []
    mgr = _manager(monkeypatch, _recording_factory(calls))
    reg.note_probe_failure(HARNESS_KAS, "kas exited during ACP initialize")

    # A FRESH pick still honours the record.
    with pytest.raises(HarnessUnavailable):
        await mgr.get_or_create("dashboard:fresh-pick", harness=HARNESS_KAS)

    provider, _is_new, _resumed = await mgr.get_or_create(
        "subagent:s1", harness=HARNESS_KAS, harness_prebound=True
    )
    mgr.release("subagent:s1")
    assert provider is not None
    assert calls[-1]["binding"].harness_id == HARNESS_KAS


@pytest.mark.asyncio
async def test_a_prebound_selection_is_still_refused_when_the_machine_cannot_run_it(
    registry_from_config, monkeypatch
):
    """Pre-bound waives one question, not availability itself.

    The two answers are seconds apart but not simultaneous, and the flag is set by
    the caller — so treating it as a blanket bypass would let a spawn start a
    harness whose binary is gone.
    """
    registry_from_config({})
    monkeypatch.setattr(
        "kiro_crew.acp.harness_registry.resolve_executable",
        lambda descriptor: ("", f"{descriptor.executable!r} was not found on PATH"),
    )
    calls: list[dict] = []
    mgr = _manager(monkeypatch, _recording_factory(calls))

    with pytest.raises(HarnessUnavailable) as excinfo:
        await mgr.get_or_create("subagent:s2", harness=HARNESS_KAS, harness_prebound=True)
    assert excinfo.value.harness_id == HARNESS_KAS
    assert calls == []


@pytest.mark.asyncio
async def test_a_session_bound_elsewhere_never_claims_a_pooled_process(
    registry_from_config, resolvable_executables, monkeypatch
):
    """Pooled processes are spawned unbound; a bound session must not take one.

    The pool is a performance path, and claiming a process spawned for another
    harness would arrive at the same silent substitution refusal-over-fallback
    exists to prevent.
    """
    registry_from_config({})
    calls: list[dict] = []
    mgr = _manager(monkeypatch, _recording_factory(calls))
    mgr._pool_size = 2
    claimed: list[str] = []

    async def _drain_and_claim(agent=None):
        claimed.append(agent or "")
        return None

    monkeypatch.setattr(mgr, "_drain_and_claim", _drain_and_claim)

    await mgr.get_or_create("dashboard:bound", harness=HARNESS_KAS)
    mgr.release("dashboard:bound")

    assert claimed == [], "a KAS-bound session consulted the kiro warm pool"

    # Control: the same call with no selection DOES consult the pool, so the
    # bypass above is the harness decision rather than a disabled pool.
    await mgr.get_or_create("dashboard:unbound")
    mgr.release("dashboard:unbound")
    assert claimed, "the default path stopped using the warm pool"


# ── The harness APIs ──


def test_the_harness_listing_payload_carries_availability_and_the_default(
    registry_from_config, monkeypatch
):
    """``GET /api/harnesses`` — id, display name, available, reason, and the default.

    Exercised through the handler's own resolution rather than an HTTP round trip,
    which is what the route test covers; what matters here is that every registered
    harness appears with its reason, including the unavailable ones a surface must
    render as visible-but-unselectable.
    """
    reg = registry_from_config({})
    reg.note_probe_failure(HARNESS_KAS, "kas exited during ACP initialize")
    rows = {row.id: row for row in reg.list()}
    assert set(rows) >= {HARNESS_KIRO, HARNESS_KAS, HARNESS_CODEX}
    assert rows[HARNESS_KAS].reason == "kas exited during ACP initialize"
    assert all(row.display_name for row in rows.values())
    assert default_harness_id(KiroCrewConfig.load()) == HARNESS_KIRO


def test_a_static_model_source_is_served_from_the_descriptor(registry_from_config):
    """``GET /api/models?harness=`` for a harness that cannot enumerate over ACP.

    Rows carry the kiro path's keys so one picker renders both, and a window the
    registry does not know is OMITTED rather than emitted as 0 — the picker reads
    that number as occupancy, where a 0 means "no context at all".
    """
    from kiro_crew.dashboard.handlers.agents import _static_harness_models

    rows = _static_harness_models(("model-a", "", "model-b"))
    assert [r["model_name"] for r in rows] == ["model-a", "model-b"]
    assert [r["display_name"] for r in rows] == ["model-a", "model-b"]
    assert all("context_window_tokens" not in r for r in rows)


def test_advertised_models_come_only_from_a_session_on_that_harness(monkeypatch):
    """One harness's catalog must never be served for another.

    The filter reads the provider's DECLARED harness, so a live kiro session cannot
    supply the list for a harness the operator asked about — serving it would put
    models the other harness has never heard of in the picker.
    """
    from kiro_crew.dashboard.handlers.agents import _advertised_harness_models

    class _Provider:
        def __init__(self, harness_id: str, models: list[dict]) -> None:
            self.harness_id = harness_id
            self._models = models

        def available_models(self) -> list[dict]:
            return self._models

    class _Sessions:
        def active_providers(self):
            return [
                _Provider(HARNESS_KIRO, [{"modelId": "kiro-model", "name": "Kiro Model"}]),
                _Provider(HARNESS_KAS, [{"modelId": "kas-model", "name": "KAS Model"}]),
            ]

    class _State:
        sessions = _Sessions()

    state = _State()
    assert [r["model_name"] for r in _advertised_harness_models(state, HARNESS_KAS)] == [
        "kas-model"
    ]
    assert _advertised_harness_models(state, HARNESS_CODEX) == []


def test_a_provider_that_declares_no_harness_is_not_attributed(monkeypatch):
    """Usage attribution reports the harness that served the turn, or nothing.

    ``harness_id`` is a declared property with an empty default, so a non-ACP
    provider or a test double answers "" and the row is recorded unattributed
    rather than credited to a harness it never ran on.
    """
    from kiro_crew.dashboard.handlers.usage import _resolve_harness

    class _Bare:
        pass

    class _Bound:
        harness_id = HARNESS_KAS

    assert _resolve_harness("", _Bare()) == ""
    assert _resolve_harness("", _Bound()) == HARNESS_KAS
    assert _resolve_harness(HARNESS_KIRO, _Bound()) == HARNESS_KIRO
    assert _resolve_harness("", None) == ""


def test_the_auth_message_names_the_harness_that_expired():
    """R6.3 end to end: a non-kiro harness must not send the user to kiro-cli login.

    kiro-cli keeps its exact wording — it is the default harness and the one whose
    remedy Kiro Crew actually knows — while any other harness is named, because a
    descriptor declares no login command and inventing one prints a guess as an
    instruction.
    """
    from kiro_crew.acp.client import _NOT_LOGGED_IN_MESSAGE, not_logged_in_message

    assert not_logged_in_message(HARNESS_KIRO) == _NOT_LOGGED_IN_MESSAGE
    assert not_logged_in_message("") == _NOT_LOGGED_IN_MESSAGE
    kas_message = not_logged_in_message(HARNESS_KAS)
    assert "kiro-cli login" not in kas_message
    assert HARNESS_KAS in kas_message


def test_the_running_harness_identifies_itself_even_when_unselectable(
    registry_from_config, tmp_path, caplog
):
    """The claude seam is ITSELF when it is what is running, never kiro-cli.

    ``resolve_alias`` degrades ``claude`` to the default because SELECTING the
    dormant seam must not start a session on it. Read as an identity that same
    answer is a misattribution: this session's usage rows would be credited to
    kiro-cli, its signed-out user would be told to run ``kiro-cli login`` for a
    harness that never refused, and a config warning would fire on a read that
    touched no configuration.
    """
    from kiro_crew.acp.client import not_logged_in_message
    from kiro_crew.acp.harness_registry import HARNESS_CLAUDE
    from kiro_crew.acp.runtime import AcpRuntime
    from kiro_crew.acp.types import ACP_BACKEND_CLAUDE
    from kiro_crew.providers.acp import AcpProvider

    registry_from_config({})
    provider = AcpProvider(work_dir=str(tmp_path), acp_backend=ACP_BACKEND_CLAUDE)
    runtime = AcpRuntime(work_dir=tmp_path, acp_backend=ACP_BACKEND_CLAUDE)
    with caplog.at_level(logging.WARNING):
        assert provider.harness_id == HARNESS_CLAUDE
        assert runtime.harness_id == HARNESS_CLAUDE
    # An identity read is not a config read: nothing may report the operator's
    # ``agent.acp_backend`` as unusable because a session asked what it is.
    assert "acp_backend" not in caplog.text

    message = not_logged_in_message(provider.harness_id)
    assert "kiro-cli login" not in message
    assert HARNESS_CLAUDE in message


def test_a_backend_spelling_with_no_bundled_row_still_resolves(registry_from_config, tmp_path):
    """The alias table stays the fallback, so identity remains total.

    Provider construction rejects an unknown backend, so this is reachable only by
    a runtime built with one — and it must answer a registered harness rather than
    an empty id.
    """
    from kiro_crew.acp.runtime import AcpRuntime

    registry_from_config({})
    assert AcpRuntime(work_dir=tmp_path, acp_backend="not-a-backend").harness_id == HARNESS_KIRO


def test_a_descriptor_without_a_display_name_still_names_itself(registry_from_config):
    """The auth message falls back to the id rather than an empty sentence."""
    from kiro_crew.acp.client import not_logged_in_message

    registry_from_config({})
    assert "ghost" in not_logged_in_message("ghost")


def test_the_binding_dataclass_reports_the_descriptor_it_wraps(
    registry_from_config, resolvable_executables
):
    """Both halves describe ONE harness, resolved together."""
    registry_from_config({})
    binding = resolve_session_harness(HARNESS_KIRO, KiroCrewConfig.load(), recorded=True)
    assert binding.harness_id == binding.descriptor.id
    assert binding.acp_backend == legacy_backend_for(binding.harness_id)
    # Frozen: a binding handed to a provider cannot be edited behind it.
    with pytest.raises(Exception):
        binding.acp_backend = "kas"  # type: ignore[misc]
    assert replace(binding.descriptor, id="other").id == "other"


# ── Totality of the selection, over every registered harness ──


@given(st.integers(min_value=0, max_value=8))
@settings(
    max_examples=9, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
def test_resolving_any_registered_harness_never_substitutes(index):
    """For EVERY registered harness: one agreeing binding, or a refusal naming it.

    The property that makes the whole surface trustworthy — there is no third
    outcome. A binding whose two halves disagreed would be a session labelled with
    one harness running another's process, and a silent fallback would be the same
    failure without even a label to notice it by.

    Availability is left as the machine really has it (nothing is monkeypatched),
    so on a CI host with no harness binary this exercises the refusal branch and on
    a developer machine with kiro-cli installed it exercises both.
    """
    reg = harness_registry()
    rows = reg.list()
    if not rows:  # pragma: no cover - the bundled set is never empty
        return
    harness_id = rows[index % len(rows)].id
    try:
        binding = resolve_session_harness(harness_id)
    except (UnknownHarness, HarnessUnavailable, HarnessNotServiceable) as exc:
        # Every refusal names the harness the caller asked for; a reason that did
        # not would leave the operator with nothing to fix.
        assert harness_id in str(exc)
        return
    assert binding.harness_id == harness_id
    assert binding.acp_backend == legacy_backend_for(harness_id)


# ── MCP delivery on the shared runtime (wire-fed harnesses) ──


def _kas_runtime(tmp_path):
    """An ``AcpRuntime`` bound to KAS, with no process behind it."""
    from kiro_crew.acp.runtime import AcpRuntime

    return AcpRuntime(work_dir=tmp_path, acp_backend=ACP_BACKEND_KAS, harness_id=HARNESS_KAS)


@pytest.mark.asyncio
async def test_a_wire_fed_harness_on_the_runtime_receives_its_real_server_map(
    registry_from_config, tmp_path, monkeypatch
):
    """KAS is wire-fed, so the runtime must deliver the converted authorized map.

    Before this wiring the runtime composed ``session/new`` from the pooled broker
    stubs alone — empty on any install with the shared gateway off — so a wire-fed
    harness reached its first turn with no tool at all and nothing said so.
    """
    registry_from_config({})
    runtime = _kas_runtime(tmp_path)
    monkeypatch.setattr(
        "kiro_crew.mcp_gateway.session_servers.authorized_servers_scoped",
        lambda overlay, agent, project=None: (
            {"fs": {"command": "fs-server", "args": ["--root", "/"]}},
            "user",
        ),
    )

    servers = await runtime._session_mcp_servers("kirocrew")

    assert [e["name"] for e in servers] == ["fs"]
    assert servers[0]["command"] == "fs-server"
    assert runtime.mcp_delivery is not None
    assert runtime.mcp_delivery.harness == HARNESS_KAS
    assert runtime.mcp_delivery.no_mcp_tools is False


@pytest.mark.asyncio
async def test_an_empty_wire_fed_delivery_is_reported_not_silent(
    registry_from_config, tmp_path, monkeypatch, caplog
):
    """A tool-less wire-fed session is retained on the runtime AND logged.

    Deliberately not an ``AcpEvent``: delivery resolves during session creation,
    before any consumer is attached to the session's event stream.
    """
    registry_from_config({})
    runtime = _kas_runtime(tmp_path)
    monkeypatch.setattr(
        "kiro_crew.mcp_gateway.session_servers.authorized_servers_scoped",
        lambda overlay, agent, project=None: ({}, ""),
    )

    with caplog.at_level("WARNING"):
        servers = await runtime._session_mcp_servers("kirocrew")

    assert servers == []
    assert runtime.mcp_delivery is not None
    assert runtime.mcp_delivery.no_mcp_tools is True
    assert "NO MCP tools" in caplog.text


@pytest.mark.asyncio
async def test_the_file_fed_default_still_sends_only_the_pooled_stubs(
    registry_from_config, tmp_path, monkeypatch
):
    """Golden: kiro-cli's array is unchanged — the pooled stubs, nothing added.

    A wire conversion leaking onto the default path would send kiro-cli its whole
    agent spec back as session-injected servers, which SHADOW the spec's own
    entries and would double every non-pooled server.
    """
    from kiro_crew.acp.runtime import AcpRuntime

    registry_from_config({})
    runtime = AcpRuntime(work_dir=tmp_path, harness_id=HARNESS_KIRO)
    monkeypatch.setattr(
        "kiro_crew.mcp_gateway.session_servers.authorized_servers_scoped",
        lambda overlay, agent, project=None: (
            {"fs": {"command": "fs-server"}},
            "user",
        ),
    )

    servers = await runtime._session_mcp_servers("kirocrew")

    assert servers == []
    assert runtime.mcp_delivery is not None
    assert runtime.mcp_delivery.mode == "file_fed"
    # file_fed never reports "no MCP tools": the harness loads them from its own
    # spec, so an empty array there says nothing about the session's tools.
    assert runtime.mcp_delivery.no_mcp_tools is False


# ── Scope precedence: a project-declared agent is not served the user overlay ──


def _write_project_agent(project_dir, name, servers):
    """Write ``<project>/.kiro/agents/<name>.json`` declaring *servers*."""
    agents = project_dir / ".kiro" / "agents"
    agents.mkdir(parents=True, exist_ok=True)
    (agents / f"{name}.json").write_text(
        json.dumps({"name": name, "mcpServers": servers}), encoding="utf-8"
    )


def _write_overlay(overlay_dir, name, servers):
    """Write a rewriter overlay for *name* declaring *servers*."""
    overlay_dir.mkdir(parents=True, exist_ok=True)
    (overlay_dir / f"{name}.json").write_text(
        json.dumps({"name": name, "mcpServers": servers}), encoding="utf-8"
    )


def test_a_project_declared_agent_is_not_delivered_the_user_level_overlay(tmp_path):
    """The project map decides which servers exist; the overlay only lends stubs.

    The rewriter writes overlays from ``~/.kiro/agents`` only, so an overlay that
    won outright would deliver a wire-fed harness a rewrite of a spec its harness
    never activated — converted cleanly and reported as a successful delivery,
    which is what makes the substitution silent.
    """
    from kiro_crew.mcp_gateway.session_servers import authorized_servers_scoped

    project = tmp_path / "proj"
    overlay = tmp_path / "overlay"
    _write_project_agent(project, "dev", {"project-only": {"command": "p"}})
    _write_overlay(overlay, "dev", {"user-only": {"command": "u"}})

    servers, scope = authorized_servers_scoped(overlay, "dev", project)

    assert set(servers) == {"project-only"}
    assert scope == "project"


def test_a_shared_name_still_gets_its_broker_stub(tmp_path):
    """Pooling survives correct scoping: a shared name takes the overlay's stub.

    Scoping the overlay must not cost pooling for a project-scoped session — the
    stub is the same server, brokered, so substituting it where BOTH scopes declare
    the name keeps one backend shared instead of spawning a second copy.
    """
    from kiro_crew.mcp_gateway.session_servers import authorized_servers_scoped

    project = tmp_path / "proj"
    overlay = tmp_path / "overlay"
    _write_project_agent(project, "dev", {"shared": {"command": "real-server"}})
    _write_overlay(
        overlay,
        "dev",
        {"shared": {"command": "broker", "_kirocrew_mcp_gateway_wrapped": True}},
    )

    servers, scope = authorized_servers_scoped(overlay, "dev", project)

    assert scope == "project"
    assert servers["shared"]["command"] == "broker"


def test_a_non_stub_overlay_entry_never_overrides_the_project_scope(tmp_path):
    """Only a real broker stub substitutes — a plain copy is the wrong scope's.

    Without the stub test this would be the same silent substitution, one entry at
    a time instead of a whole map.
    """
    from kiro_crew.mcp_gateway.session_servers import authorized_servers_scoped

    project = tmp_path / "proj"
    overlay = tmp_path / "overlay"
    _write_project_agent(project, "dev", {"shared": {"command": "project-cmd"}})
    _write_overlay(overlay, "dev", {"shared": {"command": "user-cmd"}})

    servers, _scope = authorized_servers_scoped(overlay, "dev", project)

    assert servers["shared"]["command"] == "project-cmd"


def test_the_overlay_still_wins_when_no_project_declares_the_agent(tmp_path):
    """Unchanged for every ordinary install: no project spec, overlay decides."""
    from kiro_crew.mcp_gateway.session_servers import authorized_servers_scoped

    overlay = tmp_path / "overlay"
    _write_overlay(overlay, "dev", {"user-only": {"command": "u"}})

    servers, scope = authorized_servers_scoped(overlay, "dev", tmp_path / "empty-proj")

    assert set(servers) == {"user-only"}
    assert scope == "overlay"


# ── The harness APIs, over HTTP ──


@asynccontextmanager
async def _client(app):
    """A live aiohttp test client over *app*, torn down on exit.

    ``aiohttp.test_utils`` directly rather than the ``aiohttp_client`` fixture:
    ``pytest-aiohttp`` is not installed, and adding it for four requests would put
    a second asyncio plugin beside ``pytest-asyncio``'s strict mode.
    """
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        yield client
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_api_harnesses_lists_every_harness_with_its_reason(registry_from_config, monkeypatch):
    """``GET /api/harnesses`` renders availability, reasons, and the default."""
    from aiohttp import web

    from kiro_crew.dashboard.handlers.agents import api_harnesses

    registry_from_config(
        {
            "harnesses": {
                "broken": {"executable": ""},
                "mine": {"executable": "my-acp", "argv": ["{executable}", "acp"]},
            }
        }
    )
    monkeypatch.setattr(
        "kiro_crew.acp.harness_registry.resolve_executable",
        lambda descriptor: (
            ("", "not found") if descriptor.executable == "codex-acp" else ("/usr/bin/x", "")
        ),
    )
    app = web.Application()
    app.router.add_get("/api/harnesses", api_harnesses)
    async with _client(app) as client:
        resp = await client.get("/api/harnesses")
        assert resp.status == 200
        body = await resp.json()

    rows = {row["id"]: row for row in body["harnesses"]}
    assert {HARNESS_KIRO, HARNESS_KAS, HARNESS_CODEX, "mine"} <= set(rows)
    assert body["default"] == HARNESS_KIRO
    # Unavailable entries are PRESENT with their reason: a surface renders them
    # visible and unselectable rather than hiding a harness the build supports.
    assert rows[HARNESS_CODEX]["available"] is False
    assert rows[HARNESS_CODEX]["reason"]
    assert rows["mine"]["bundled"] is False
    # An invalid operator descriptor is served separately — never as a row a
    # session could be created on.
    assert [row["id"] for row in body["invalid"]] == ["broken"]
    assert body["invalid"][0]["reason"]
    assert "broken" not in rows


@pytest.mark.asyncio
async def test_api_models_refuses_an_unknown_harness_by_name(registry_from_config):
    """404, not a degraded 503: retrying cannot make the harness exist."""
    from aiohttp import web

    from kiro_crew.dashboard.handlers.agents import api_models

    registry_from_config({})
    app = web.Application()
    app.router.add_get("/api/models", api_models)
    async with _client(app) as client:
        resp = await client.get("/api/models", params={"harness": "not-a-harness"})
        assert resp.status == 404
        body = await resp.json()
    assert body["code"] == "unknown_harness"
    assert "not-a-harness" in body["error"]


@pytest.mark.asyncio
async def test_api_models_serves_a_static_descriptor_list(registry_from_config):
    """An operator harness that cannot enumerate is served from its descriptor."""
    from aiohttp import web

    from kiro_crew.dashboard.handlers.agents import api_models

    registry_from_config(
        {
            "harnesses": {
                "mine": {
                    "executable": "my-acp",
                    "argv": ["{executable}", "acp"],
                    "model_source": "static",
                    "models": ["m1", "m2"],
                }
            }
        }
    )
    app = web.Application()
    app.router.add_get("/api/models", api_models)
    async with _client(app) as client:
        resp = await client.get("/api/models", params={"harness": "mine"})
        assert resp.status == 200
        assert [row["model_name"] for row in await resp.json()] == ["m1", "m2"]


@pytest.mark.asyncio
async def test_api_models_never_shells_out_for_a_non_kiro_harness(
    registry_from_config, monkeypatch
):
    """An ``acp_advertised`` non-kiro harness answers from live sessions only.

    Falling through to ``kiro chat --list-models`` would serve kiro-cli's catalog
    under another harness's name — the substitution the per-harness parameter
    exists to prevent — and would spawn kiro-cli to answer a question about a
    different tool.
    """
    from aiohttp import web

    from kiro_crew.dashboard.handlers.agents import api_models

    registry_from_config({})

    def _never(*_args, **_kwargs):  # pragma: no cover - the assertion is non-use
        raise AssertionError("the kiro --list-models path must not be reached")

    monkeypatch.setattr("kiro_crew.acp.client._resolve_kiro_bin_for_spawn", _never)

    class _Sessions:
        def active_providers(self):
            return []

    class _State:
        sessions = _Sessions()

    app = web.Application()
    app["state"] = _State()
    app.router.add_get("/api/models", api_models)
    async with _client(app) as client:
        resp = await client.get("/api/models", params={"harness": HARNESS_KAS})
        # 200 with an empty list: "nothing advertised yet" is an answer, not a
        # failure, so the client must not poll a state only the user can leave.
        assert resp.status == 200
        assert await resp.json() == []


# ── Usage attribution ──


def test_a_usage_row_records_the_harness_that_served_the_turn():
    """The row carries the bound harness, so a mixed fleet reports per harness."""
    from datetime import datetime

    from kiro_crew.dashboard.handlers.usage import _build_token_record

    class _Event:
        usage = None
        stop_reason = ""

    record = _build_token_record(
        "dashboard:1",
        "auto",
        _Event(),
        "acp",
        datetime.now().astimezone(),
        harness=HARNESS_KAS,
    )
    assert record["harness"] == HARNESS_KAS
    # Unattributed stays empty rather than defaulting to the kiro row.
    blank = _build_token_record("dashboard:1", "auto", _Event(), "acp", datetime.now().astimezone())
    assert blank["harness"] == ""
