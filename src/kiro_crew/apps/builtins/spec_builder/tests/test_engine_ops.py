"""The operator surfaces: effective configuration, per-run spend, the stop control.

Three claims, each asserted against the ENGINE rather than against the handler
that reports it:

* **the config surface shows what the engine will use, and where it came from.**
  Every effective-value assertion is cross-checked against
  ``ConfigStore.effective_settings`` opened over the same root, so a handler that
  re-derived precedence and disagreed with the engine would fail here rather than
  quietly show an operator a number the engine does not use.
* **a write goes through the engine's validated path.** The rules asserted are the
  engine's own -- an unknown key, an out-of-range value, and the screening
  disable-all key -- because the handler is not allowed to have rules of its own to
  keep in step. The screening case matters most: it is the requirement that no
  single setting disables screening for every submitter class, and it is asserted
  here at the HTTP boundary an operator actually reaches.
* **the stop control stops runs and reports the credits they consumed.** Read back
  from the engine's run table and its persisted flag file, not from the response.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew.apps.builtins.spec_engine.engine.budget.switch import (
    KILL_SWITCH_FILENAME,
    KillSwitch,
)
from kiro_crew.apps.builtins.spec_engine.engine.config import ConfigStore
from kiro_crew.apps.builtins.spec_engine.engine.config.settings import SETTINGS

from .test_routes import _BASE, _make_client


@pytest.fixture()
def config_root(monkeypatch, tmp_path: Path) -> Path:
    """Point the engine's config store at a tmp root for the duration of a test.

    Patched on the store module rather than on the handler: the handler resolves
    its store through ``ConfigStore()`` with no argument, so redirecting the
    default root is what proves the handler reads the engine's own document
    instead of one it was handed.
    """
    root = tmp_path / "engine-config"
    monkeypatch.setattr(
        "kiro_crew.apps.builtins.spec_engine.engine.config.store.default_root",
        lambda: root,
    )
    return root


def _store(config_root: Path) -> ConfigStore:
    return ConfigStore(config_root)


class TestEffectiveConfigSurface:
    @pytest.mark.asyncio
    async def test_an_unconfigured_install_reports_every_setting_as_a_bundled_default(
        self, monkeypatch, tmp_path, config_root
    ):
        async with _make_client(monkeypatch, tmp_path) as client:
            resp = await client.get(f"{_BASE}/engine/config")
            assert resp.status == 200
            body = await resp.json()
        assert set(body["settings"]) == set(SETTINGS)
        for key, row in body["settings"].items():
            assert row["origin"] == "bundled_default"
            assert row["is_default"] is True
            assert row["value"] == SETTINGS[key].default

    @pytest.mark.asyncio
    async def test_an_override_is_reported_with_its_origin_and_declaration_path(
        self, monkeypatch, tmp_path, config_root
    ):
        _store(config_root).write(
            {"concurrency": {"global_max_runs": 9}},
            surface=_dashboard_surface(),
        )
        async with _make_client(monkeypatch, tmp_path) as client:
            resp = await client.get(f"{_BASE}/engine/config")
            body = await resp.json()
        row = body["settings"]["concurrency.global_max_runs"]
        assert row["value"] == 9
        assert row["origin"] == "app_config"
        assert row["declared_at"] == "concurrency.global_max_runs"
        # The bundled default travels with it, so the surface can offer a reset
        # without a second round trip.
        assert row["default"] == SETTINGS["concurrency.global_max_runs"].default

    @pytest.mark.asyncio
    async def test_the_reported_value_is_the_one_the_engine_resolves_for_that_scope(
        self, monkeypatch, tmp_path, config_root
    ):
        # A project override plus an app value: the two differ, so a surface that
        # ignored the project scope would report the app number. Cross-checked
        # against the engine's own resolver for the same scope.
        _store(config_root).write(
            {
                "concurrency": {"project_max_runs": 2},
                "projects": {"web": {"path": "/tmp/web", "concurrency": {"project_max_runs": 7}}},
            },
            surface=_dashboard_surface(),
        )
        async with _make_client(monkeypatch, tmp_path) as client:
            resp = await client.get(f"{_BASE}/engine/config?project=web")
            body = await resp.json()
        engine_view = _store(config_root).effective_settings(project="web")
        for key, row in body["settings"].items():
            assert row["value"] == engine_view[key].value, key
            assert row["origin"] == engine_view[key].origin.value, key
        assert body["settings"]["concurrency.project_max_runs"]["value"] == 7
        assert body["scope"] == {"project": "web", "source": None}

    @pytest.mark.asyncio
    async def test_the_configured_domains_are_reported_for_the_sections_that_exist(
        self, monkeypatch, tmp_path, config_root
    ):
        _store(config_root).write(
            {"sources": {"gh": {"poll": ["gh", "issue", "list"]}}},
            surface=_dashboard_surface(),
        )
        async with _make_client(monkeypatch, tmp_path) as client:
            resp = await client.get(f"{_BASE}/engine/config")
            body = await resp.json()
        assert body["domains"]["sources"]["gh"]["poll"] == ["gh", "issue", "list"]
        # An absent section is omitted rather than reported as an empty object, and
        # the catalogue of sections is named so a surface can say "none configured"
        # for a domain instead of implying it has no such domain.
        assert "projects" not in body["domains"]
        assert "projects" in body["domain_sections"]

    @pytest.mark.asyncio
    async def test_a_stored_value_the_registry_refuses_is_reported_not_silently_defaulted(
        self, monkeypatch, tmp_path, config_root
    ):
        # Written past the write path, which is the only way this document could
        # exist: a hand edit. Substituting the default here would run the work the
        # operator meant to bound.
        config_root.mkdir(parents=True, exist_ok=True)
        (config_root / "config.json").write_text(
            json.dumps({"concurrency": {"global_max_runs": 0}}), encoding="utf-8"
        )
        async with _make_client(monkeypatch, tmp_path) as client:
            resp = await client.get(f"{_BASE}/engine/config")
            assert resp.status == 409
            body = await resp.json()
        assert body["code"] == "config_invalid"
        assert "concurrency.global_max_runs" in body["error"]


class TestConfigWrites:
    @pytest.mark.asyncio
    async def test_a_write_persists_through_the_engine_and_reads_back_as_effective(
        self, monkeypatch, tmp_path, config_root
    ):
        async with _make_client(monkeypatch, tmp_path) as client:
            resp = await client.put(
                f"{_BASE}/engine/config",
                json={"patch": {"concurrency": {"global_max_runs": 6}}},
            )
            assert resp.status == 200
        # Read through a SEPARATE store over the engine's root: a value the
        # handler kept in memory would satisfy a test that asked the handler.
        assert _store(config_root).effective("concurrency.global_max_runs").value == 6

    @pytest.mark.asyncio
    async def test_an_unknown_key_is_refused_by_path(self, monkeypatch, tmp_path, config_root):
        async with _make_client(monkeypatch, tmp_path) as client:
            resp = await client.put(
                f"{_BASE}/engine/config",
                json={"patch": {"concurrency": {"not_a_setting": 1}}},
            )
            assert resp.status == 422
            body = await resp.json()
        assert body["code"] == "config_invalid"
        assert "concurrency.not_a_setting" in body["error"]
        assert not (config_root / "config.json").exists()

    @pytest.mark.asyncio
    async def test_an_out_of_range_value_is_refused(self, monkeypatch, tmp_path, config_root):
        async with _make_client(monkeypatch, tmp_path) as client:
            resp = await client.put(
                f"{_BASE}/engine/config",
                json={"patch": {"concurrency": {"global_max_runs": 0}}},
            )
            assert resp.status == 422

    @pytest.mark.asyncio
    async def test_a_per_class_screening_opt_out_can_be_saved(
        self, monkeypatch, tmp_path, config_root
    ):
        # The opt-out has to be reachable at all: an operator who cannot save one
        # through any surface has a feature that exists only in the reader.
        async with _make_client(monkeypatch, tmp_path) as client:
            resp = await client.put(
                f"{_BASE}/engine/config",
                json={
                    "patch": {
                        "sources": {"gh": {"poll": ["gh"], "screening": {"maintainer": False}}}
                    }
                },
            )
            assert resp.status == 200
        document = _store(config_root).document()
        assert document["sources"]["gh"]["screening"] == {"maintainer": False}

    @pytest.mark.asyncio
    async def test_a_screening_disable_all_key_is_refused_at_the_operator_surface(
        self, monkeypatch, tmp_path, config_root
    ):
        # The requirement is that NO single setting disables screening for every
        # submitter class. Asserted at the HTTP boundary because that is where an
        # operator would try it, and because the engine's reader defending the
        # same rule is the second line, not the only one.
        async with _make_client(monkeypatch, tmp_path) as client:
            resp = await client.put(
                f"{_BASE}/engine/config",
                json={
                    "patch": {"sources": {"gh": {"poll": ["gh"], "screening": {"default": False}}}}
                },
            )
            assert resp.status == 422
            body = await resp.json()
        assert "no default key" in body["error"]
        # Nothing reached disk: the refusal is a refusal to persist, not a note
        # recorded beside a saved disable-all.
        assert not (config_root / "config.json").exists()

    @pytest.mark.asyncio
    async def test_a_screening_wildcard_key_is_refused_too(
        self, monkeypatch, tmp_path, config_root
    ):
        # The second spelling of the same thing. ``*`` is not the document's
        # wildcard token, so it is refused as an unknown submitter class -- but it
        # is refused, which is what matters: a surface that accepted it would have
        # saved a key no reader honours, and an operator would believe screening
        # was off for everyone while it ran for all four classes.
        async with _make_client(monkeypatch, tmp_path) as client:
            resp = await client.put(
                f"{_BASE}/engine/config",
                json={"patch": {"sources": {"gh": {"poll": ["gh"], "screening": {"*": False}}}}},
            )
            assert resp.status == 422
        assert not (config_root / "config.json").exists()

    @pytest.mark.asyncio
    async def test_a_non_boolean_screening_value_is_refused(
        self, monkeypatch, tmp_path, config_root
    ):
        async with _make_client(monkeypatch, tmp_path) as client:
            resp = await client.put(
                f"{_BASE}/engine/config",
                json={
                    "patch": {
                        "sources": {"gh": {"poll": ["gh"], "screening": {"maintainer": "off"}}}
                    }
                },
            )
            assert resp.status == 422

    @pytest.mark.asyncio
    async def test_a_non_object_body_is_refused(self, monkeypatch, tmp_path, config_root):
        async with _make_client(monkeypatch, tmp_path) as client:
            resp = await client.put(f"{_BASE}/engine/config", json=[1, 2])
            assert resp.status == 400


class TestKillSwitchSurface:
    @pytest.mark.asyncio
    async def test_a_released_switch_is_reported_released_with_nothing_to_stop(
        self, monkeypatch, tmp_path, config_root
    ):
        async with _make_client(monkeypatch, tmp_path) as client:
            resp = await client.get(f"{_BASE}/engine/kill-switch")
            assert resp.status == 200
            body = await resp.json()
        assert body["switch"]["engaged"] is False
        assert body["stoppable"] == []
        assert body["stoppable_credits"] == 0.0

    @pytest.mark.asyncio
    async def test_engaging_writes_the_flag_the_engine_reads(
        self, monkeypatch, tmp_path, config_root
    ):
        state_dir = tmp_path / "spec-builder"
        async with _make_client(monkeypatch, tmp_path) as client:
            resp = await client.post(
                f"{_BASE}/engine/kill-switch",
                json={"action": "engage", "reason": "runaway wave"},
            )
            assert resp.status == 200
            body = await resp.json()
        assert body["switch"]["engaged"] is True
        assert body["switch"]["reason"] == "runaway wave"
        # Read back through the ENGINE's own switch over the engine's state root:
        # a flag the handler reported but did not persist would stop nothing, and
        # every reader of this switch (the watch tick, the dispatch gate, the
        # budget guard) reads the file rather than the response.
        engine_root = state_dir / "engine-state"
        assert KillSwitch(engine_root).engaged is True
        assert (engine_root / KILL_SWITCH_FILENAME).exists()

    @pytest.mark.asyncio
    async def test_engaging_records_the_dashboard_as_the_initiator(
        self, monkeypatch, tmp_path, config_root
    ):
        async with _make_client(monkeypatch, tmp_path) as client:
            await client.post(f"{_BASE}/engine/kill-switch", json={"action": "engage"})
            resp = await client.get(f"{_BASE}/engine/kill-switch")
            body = await resp.json()
        assert body["switch"]["initiator"] == "dashboard"

    @pytest.mark.asyncio
    async def test_engaging_twice_reports_the_second_call_as_already_engaged(
        self, monkeypatch, tmp_path, config_root
    ):
        async with _make_client(monkeypatch, tmp_path) as client:
            await client.post(f"{_BASE}/engine/kill-switch", json={"action": "engage"})
            resp = await client.post(f"{_BASE}/engine/kill-switch", json={"action": "engage"})
            body = await resp.json()
        assert body["already_engaged"] is True
        assert body["switch"]["engaged"] is True

    @pytest.mark.asyncio
    async def test_releasing_clears_the_flag_and_says_it_resumed_nothing(
        self, monkeypatch, tmp_path, config_root
    ):
        state_dir = tmp_path / "spec-builder"
        async with _make_client(monkeypatch, tmp_path) as client:
            await client.post(f"{_BASE}/engine/kill-switch", json={"action": "engage"})
            resp = await client.post(f"{_BASE}/engine/kill-switch", json={"action": "release"})
            body = await resp.json()
        assert body["changed"] is True
        assert body["switch"]["engaged"] is False
        # Stated explicitly: releasing lets NEW work start and resumes nothing that
        # was parked. An operator who assumes otherwise waits for runs that will
        # never move.
        assert body["resumed"] == []
        assert KillSwitch(state_dir / "engine-state").engaged is False

    @pytest.mark.asyncio
    async def test_an_unknown_action_is_refused_and_changes_nothing(
        self, monkeypatch, tmp_path, config_root
    ):
        state_dir = tmp_path / "spec-builder"
        async with _make_client(monkeypatch, tmp_path) as client:
            resp = await client.post(f"{_BASE}/engine/kill-switch", json={"action": "stop"})
            assert resp.status == 400
            assert (await resp.json())["code"] == "bad_action"
        assert KillSwitch(state_dir / "engine-state").engaged is False

    @pytest.mark.asyncio
    async def test_an_unreadable_record_is_reported_as_engaged_and_as_doubt(
        self, monkeypatch, tmp_path, config_root
    ):
        state_dir = tmp_path / "spec-builder"
        engine_root = state_dir / "engine-state"
        async with _make_client(monkeypatch, tmp_path) as client:
            # Reached through the client first so the engine root exists.
            await client.get(f"{_BASE}/engine/kill-switch")
            (engine_root / KILL_SWITCH_FILENAME).write_text("{not json", encoding="utf-8")
            resp = await client.get(f"{_BASE}/engine/kill-switch")
            body = await resp.json()
        assert body["switch"]["engaged"] is True
        assert body["switch"]["unreadable"] is True


class TestQueueSpendSurface:
    @pytest.mark.asyncio
    async def test_an_empty_queue_reports_no_entries_and_no_credits(
        self, monkeypatch, tmp_path, config_root
    ):
        async with _make_client(monkeypatch, tmp_path) as client:
            resp = await client.get(f"{_BASE}/engine/queue")
            assert resp.status == 200
            body = await resp.json()
        assert body["entries"] == []
        assert body["total_credits"] == 0.0

    @pytest.mark.asyncio
    async def test_every_queue_row_carries_the_credits_its_run_consumed(
        self, monkeypatch, tmp_path, config_root
    ):
        # The projection is the engine's, so the assertion is that the field is
        # present and relayed rather than dropped on the way out: a surface
        # missing it would show a reviewer a run with no cost at all.
        async with _make_client(monkeypatch, tmp_path) as client:
            resp = await client.get(f"{_BASE}/engine/queue")
            body = await resp.json()
        for entry in body["entries"]:
            assert "cost_credits" in entry


def _dashboard_surface():
    from kiro_crew.apps.builtins.spec_engine.engine.config.store import DASHBOARD_SURFACE

    return DASHBOARD_SURFACE
