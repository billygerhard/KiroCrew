"""The Settings write surface for harnesses, and what it is allowed to write.

Settings can name the harness new sessions start on. Three things have to hold, and
each one's opposite is silent:

1. **The two harness keys are writable at all.** A key absent from
   ``_EDITABLE_CONFIG`` renders a control that then fails to save, so the panel
   would look like a setting and behave like a decoration.
2. **A typo is refused with the vocabulary, and an uninstalled harness is not.**
   Registration is the bar, not availability: the load path degrades an unknown id
   to kiro-cli with only a log line, so persisting one hides the setting forever —
   while an operator naming the default before installing the tool is legitimate
   and heals on the next listing.
3. **The write reaches the next session without a restart.** The session manager
   snapshots the config and the warm pool holds processes spawned on the previously
   aliased harness, so a write that skipped ``refresh_defaults`` would look like it
   did not take.

Plus the payload the panel's alias input is seeded from: ``/api/harnesses`` carries
the ``agent.acp_backend`` spelling AS STORED, because the config GET reports it
clamped and a surface seeded from that would render kiro-cli as the operator's
legacy choice.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.acp.harness_registry import HARNESS_CLAUDE, HARNESS_CODEX, HARNESS_KAS, HARNESS_KIRO
from kiro_crew.acp.harness_registry import registry as harness_registry
from kiro_crew.acp.types import ACP_BACKEND_KAS

# Wave-2 dropped the frozen ``ACP_BACKENDS_SELECTABLE`` snapshot for the
# ``selectable_backends()`` registry an edition extends; ``selectable_backend_values()``
# is its sorted-list form and is the single owner the source surfaces read
# (``_EDITABLE_CONFIG[...]["values"]`` and ``/api/harnesses``'s ``legacy_backends``),
# so the seeded-from assertions below compare against it rather than a copy.
from kiro_crew.acp_backends import selectable_backend_values


def _seed_config(agent: dict | None = None) -> dict:
    return {
        "agents": {"kirocrew": {"kiro_agent": "kirocrew"}},
        "default_agent": "kirocrew",
        "agent": {"approval_mode": "auto", **(agent or {})},
    }


@pytest.fixture
def tmp_config(tmp_path):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(_seed_config()), encoding="utf-8")
    with patch("kiro_crew.config.loader.config_path", return_value=cfg_path):
        yield cfg_path


def _app() -> tuple[web.Application, MagicMock]:
    """The PATCH handler with a stubbed session manager.

    ``refresh_defaults`` is the observable this file cares about, so the mock is
    returned rather than discarded.
    """
    from kiro_crew.dashboard.handlers import api_kirocrew_config_patch

    app = web.Application()
    app.router.add_patch("/api/config/kirocrew", api_kirocrew_config_patch)
    sessions = MagicMock(spec=["refresh_defaults"])
    sessions.refresh_defaults = AsyncMock()
    app["state"] = SimpleNamespace(sessions=sessions, subagents=None)
    return app, sessions


async def _patch(client, path, value):
    return await client.patch("/api/config/kirocrew", json={"path": path, "value": value})


# ── What Settings may write ──


def test_both_harness_keys_are_editable_and_definitions_are_not() -> None:
    """The panel writes the two selection keys and never a harness definition.

    ``agent.harnesses`` names a binary Kiro Crew spawns, so it stays config-file
    only — the panel reads the registry's verdict on those entries instead.
    """
    from kiro_crew.dashboard.handlers.core import _EDITABLE_CONFIG

    assert "agent.default_harness" in _EDITABLE_CONFIG
    assert "agent.acp_backend" in _EDITABLE_CONFIG
    assert "agent.harnesses" not in _EDITABLE_CONFIG


def test_the_legacy_backend_enum_is_the_selectable_set() -> None:
    """Derived, not restated: a backend the build cannot serve is not offerable.

    A hardcoded list here would let Settings write a spelling session creation then
    refuses — the panel offering a value the gateway will not honour. Post-merge the
    entry carries a ``values_fn`` callable rather than a frozen ``values`` list,
    because the selectable set WIDENS after this module is imported (an edition
    registers a backend at boot); the enum is still exactly the registry's answer.
    """
    from kiro_crew.dashboard.handlers.core import _EDITABLE_CONFIG

    entry = _EDITABLE_CONFIG["agent.acp_backend"]
    resolved = entry["values_fn"]() if "values_fn" in entry else entry["values"]
    assert resolved == selectable_backend_values()


@pytest.mark.asyncio
async def test_a_registered_harness_is_written_and_reaches_the_next_session(tmp_config) -> None:
    app, sessions = _app()
    async with TestClient(TestServer(app)) as client:
        resp = await _patch(client, "agent.default_harness", HARNESS_KAS)
        assert resp.status == 200
    assert json.loads(tmp_config.read_text(encoding="utf-8"))["agent"]["default_harness"] == (
        HARNESS_KAS
    )
    # Without this the change waits for a gateway restart: the manager resolves the
    # default from its own config snapshot, and the warm pool still holds processes
    # spawned on the harness the legacy key named.
    sessions.refresh_defaults.assert_awaited_once()


@pytest.mark.asyncio
async def test_the_legacy_backend_write_also_refreshes_defaults(tmp_config) -> None:
    app, sessions = _app()
    async with TestClient(TestServer(app)) as client:
        resp = await _patch(client, "agent.acp_backend", ACP_BACKEND_KAS)
        assert resp.status == 200
    assert json.loads(tmp_config.read_text(encoding="utf-8"))["agent"]["acp_backend"] == (
        ACP_BACKEND_KAS
    )
    sessions.refresh_defaults.assert_awaited_once()


@pytest.mark.asyncio
async def test_an_unknown_harness_is_refused_with_the_registered_ids(tmp_config) -> None:
    app, _sessions = _app()
    async with TestClient(TestServer(app)) as client:
        resp = await _patch(client, "agent.default_harness", "not-a-harness")
        assert resp.status == 400
        body = await resp.json()
    # The vocabulary is in the refusal: the operator cannot look up the registry's
    # ids from the UI, and the load path would have swallowed this value.
    assert "not-a-harness" in body["error"]
    assert HARNESS_KIRO in body["error"] or "kiro" in body["error"]
    # And a machine-readable code beside it: the prose is a diagnostic the panel
    # renders behind a TRANSLATED prefix, so without a code a twelve-language
    # dashboard would explain this failure in English and nothing else.
    assert body["code"] == "unknown_harness"
    assert "default_harness" not in json.loads(tmp_config.read_text(encoding="utf-8"))["agent"]


@pytest.mark.asyncio
async def test_an_unavailable_but_registered_harness_is_accepted(tmp_config, monkeypatch) -> None:
    """Registration is the bar, because availability describes the machine now.

    Refusing here would make "set the default, then install the tool" unexpressible
    and would refuse a harness whose binary is merely absent this second.
    """
    monkeypatch.setattr(
        "kiro_crew.acp.harness_registry.resolve_executable",
        lambda descriptor: ("", "not found"),
    )
    app, _sessions = _app()
    async with TestClient(TestServer(app)) as client:
        resp = await _patch(client, "agent.default_harness", HARNESS_CODEX)
        assert resp.status == 200
    assert json.loads(tmp_config.read_text(encoding="utf-8"))["agent"]["default_harness"] == (
        HARNESS_CODEX
    )


@pytest.mark.asyncio
async def test_an_operator_harness_is_writable_once_it_validates(tmp_config) -> None:
    """The vocabulary is the registry's, so an operator's own harness is offerable."""
    tmp_config.write_text(
        json.dumps(
            _seed_config(
                {"harnesses": {"mine": {"executable": "my-acp", "argv": ["{executable}", "acp"]}}}
            )
        ),
        encoding="utf-8",
    )
    harness_registry().reload()
    app, _sessions = _app()
    async with TestClient(TestServer(app)) as client:
        resp = await _patch(client, "agent.default_harness", "mine")
        assert resp.status == 200
    assert json.loads(tmp_config.read_text(encoding="utf-8"))["agent"]["default_harness"] == "mine"


@pytest.mark.asyncio
async def test_an_invalid_operator_entry_is_not_writable(tmp_config) -> None:
    """An entry the registry rejected is not selectable anywhere, Settings included.

    It is served by ``registry.invalid()`` for display only; accepting its id here
    would persist a default that resolves to nothing.
    """
    tmp_config.write_text(
        json.dumps(_seed_config({"harnesses": {"broken": {"executable": ""}}})),
        encoding="utf-8",
    )
    harness_registry().reload()
    app, _sessions = _app()
    async with TestClient(TestServer(app)) as client:
        resp = await _patch(client, "agent.default_harness", "broken")
        assert resp.status == 400
        body = await resp.json()
    assert "broken" in body["error"]


@pytest.mark.asyncio
async def test_an_unserviceable_legacy_spelling_is_refused(tmp_config) -> None:
    """``codex`` is outside the field's enum, so Settings cannot write it.

    The panel still SHOWS a stored one (see the listing test below) — displaying
    what the operator wrote is not the same as offering to write it.
    """
    app, _sessions = _app()
    async with TestClient(TestServer(app)) as client:
        resp = await _patch(client, "agent.acp_backend", "codex")
        assert resp.status == 400


# ── What the panel's alias input is seeded from ──


@pytest.mark.asyncio
async def test_the_listing_serves_the_stored_legacy_spelling(tmp_config, monkeypatch) -> None:
    """``legacy_backend`` is the raw value; ``default`` is what it resolves to.

    A hand-edited ``codex`` reads back from ``GET /api/config/kirocrew`` as ``""``
    because the field is clamped at load. A Settings surface seeded from that would
    render Kiro CLI as the operator's legacy choice and, on the next change event,
    write that over the value they wrote.
    """
    from kiro_crew.dashboard.handlers.agents import api_harnesses

    tmp_config.write_text(json.dumps(_seed_config({"acp_backend": "codex"})), encoding="utf-8")
    harness_registry().reload()
    monkeypatch.setattr(
        "kiro_crew.acp.harness_registry.resolve_executable",
        lambda descriptor: (
            ("", "codex was not found") if descriptor.id == HARNESS_CODEX else ("/usr/bin/x", "")
        ),
    )
    app = web.Application()
    app.router.add_get("/api/harnesses", api_harnesses)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/api/harnesses")
        assert resp.status == 200
        body = await resp.json()

    assert body["legacy_backend"] == "codex"
    # And the resolution the panel reports beside it is the server's own, so the
    # displayed default cannot disagree with what an unselected creation does.
    assert body["default"] == HARNESS_CODEX
    rows = {row["id"]: row for row in body["harnesses"]}
    assert rows[HARNESS_CODEX]["available"] is False
    assert rows[HARNESS_CODEX]["reason"]


@pytest.mark.asyncio
async def test_the_listing_serves_the_writable_legacy_vocabulary(tmp_config) -> None:
    """``legacy_backends`` is the selectable set, so the client restates nothing.

    The alias input's options and the PATCH allowlist's enum are one vocabulary. A
    copy in TypeScript would be an edit away from offering a spelling this build
    refuses, with nothing failing in between — the panel would look like it saved
    and the write would be denied.
    """
    from kiro_crew.dashboard.handlers.agents import api_harnesses

    harness_registry().reload()
    app = web.Application()
    app.router.add_get("/api/harnesses", api_harnesses)
    async with TestClient(TestServer(app)) as client:
        body = await (await client.get("/api/harnesses")).json()

    assert body["legacy_backends"] == selectable_backend_values()
    from kiro_crew.dashboard.handlers.core import _EDITABLE_CONFIG

    entry = _EDITABLE_CONFIG["agent.acp_backend"]
    resolved = entry["values_fn"]() if "values_fn" in entry else entry["values"]
    assert body["legacy_backends"] == resolved


@pytest.mark.asyncio
async def test_the_listing_carries_the_serviceable_gate_per_row(tmp_config, monkeypatch) -> None:
    """``serviceable`` is a SECOND gate, and a surface needs both to be honest.

    After wave 2 flipped serving on, Codex is served through the generic adapter,
    and the merge with origin/main (upstream #7301) made Claude Code a selectable
    public backend — the registry's ``_UNSERVICEABLE`` map is now EMPTY, so no
    bundled row is unserviceable and Claude's row is BOTH available and serviceable.
    The gate is still a real per-row field driven by that map (the mechanism is
    retained for an edition whose build genuinely cannot serve a bundled harness),
    not a constant the endpoint stamps on: every bundled row still carries its own
    ``serviceable`` verdict, which today reads true for all of them.

    No reason travels with the flag: when a row IS unserviceable the verdict is
    identical for every such row, so the explanation is a catalog string on the
    surface rather than English prose on the wire.
    """
    from kiro_crew.dashboard.handlers.agents import api_harnesses

    harness_registry().reload()
    monkeypatch.setattr(
        "kiro_crew.acp.harness_registry.resolve_executable",
        lambda descriptor: ("/usr/bin/anything", ""),
    )
    app = web.Application()
    app.router.add_get("/api/harnesses", api_harnesses)
    async with TestClient(TestServer(app)) as client:
        body = await (await client.get("/api/harnesses")).json()

    rows = {row["id"]: row for row in body["harnesses"]}
    # Codex: available AND serviceable — the wave-2 inversion.
    assert rows[HARNESS_CODEX]["available"] is True
    assert rows[HARNESS_CODEX]["serviceable"] is True
    # Claude Code now serves in the public build (merge / upstream #7301): its
    # posture moved out of the empty ``_UNSERVICEABLE`` map.
    assert rows[HARNESS_CLAUDE]["serviceable"] is True
    # The harnesses this build keys a provider on serve too.
    assert rows[HARNESS_KIRO]["serviceable"] is True
    assert rows[HARNESS_KAS]["serviceable"] is True
    # The gate is still real per-row data: every bundled row carries its own
    # ``serviceable`` verdict rather than the field being absent or a constant.
    for hid in (HARNESS_KIRO, HARNESS_KAS, HARNESS_CODEX, HARNESS_CLAUDE):
        assert "serviceable" in rows[hid]
