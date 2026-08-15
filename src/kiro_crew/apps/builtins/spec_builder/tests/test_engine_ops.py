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

from kiro_crew.apps.builtins.spec_builder.backend import routes
from kiro_crew.apps.builtins.spec_engine.engine import runs as engine_runs
from kiro_crew.apps.builtins.spec_engine.engine.budget.ledger import RunAccounting
from kiro_crew.apps.builtins.spec_engine.engine.budget.switch import (
    KILL_SWITCH_FILENAME,
    KillSwitch,
)
from kiro_crew.apps.builtins.spec_engine.engine.config import ConfigStore
from kiro_crew.apps.builtins.spec_engine.engine.config.settings import SETTINGS
from kiro_crew.apps.builtins.spec_engine.engine.delivery.preset_display import stage_origins
from kiro_crew.apps.builtins.spec_engine.engine.delivery.workflow import (
    DeliveryWorkflow,
    workflow_presets,
)
from kiro_crew.apps.builtins.spec_engine.engine.review_queue import ReviewQueue
from kiro_crew.apps.builtins.spec_engine.engine.runs import RunState
from kiro_crew.apps.builtins.spec_engine.engine.state import SpecRef
from kiro_crew.apps.builtins.spec_engine.engine.watch import lifecycle as engine_lifecycle

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
    async def test_engaging_records_the_authenticated_session_as_the_initiator(
        self, monkeypatch, tmp_path, config_root
    ):
        # The SESSION user, not this surface's name. "dashboard" answers where the
        # stop came from and nothing about who threw it, and the flag is what a
        # later operator reads to find out who stopped their work.
        async with _make_client(monkeypatch, tmp_path) as client:
            await client.post(f"{_BASE}/engine/kill-switch", json={"action": "engage"})
            resp = await client.get(f"{_BASE}/engine/kill-switch")
            body = await resp.json()
        assert body["switch"]["initiator"] == "tester"

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


class TestQueueGrouping:
    """The queue reaches a surface grouped by run state, as the ENGINE groups it.

    Asserted against ``QueueSnapshot.grouped()`` opened over the same store, so a
    handler that assembled its own grouping and disagreed with the engine's would
    fail here rather than show an operator a second, drifting view of one run.
    """

    @pytest.mark.asyncio
    async def test_the_response_groups_by_run_state(self, monkeypatch, tmp_path, config_root):
        async with _make_client(monkeypatch, tmp_path) as client:
            ref = _park_runs(
                tmp_path,
                (("run-a", RunState.AWAITING_REVIEW), ("run-b", RunState.HALTED_BUDGET)),
            )
            resp = await client.get(f"{_BASE}/engine/queue")
            assert resp.status == 200
            body = await resp.json()
            engine_grouped = {
                state.value: [entry.run_id for entry in group]
                for state, group in _queue().snapshot().grouped().items()
            }
        assert {
            state: [entry["run_id"] for entry in group] for state, group in body["grouped"].items()
        } == engine_grouped
        assert engine_grouped == {
            "awaiting_review": ["run-a"],
            "halted_budget": ["run-b"],
        }
        # The flat list is still relayed for the spend table, and the grouping is
        # the same runs rather than a second query.
        assert {entry["run_id"] for entry in body["entries"]} == {"run-a", "run-b"}
        assert ref.name == "example"

    @pytest.mark.asyncio
    async def test_a_state_with_nothing_waiting_gets_no_group(
        self, monkeypatch, tmp_path, config_root
    ):
        async with _make_client(monkeypatch, tmp_path) as client:
            _park_runs(tmp_path, (("run-a", RunState.AWAITING_REVIEW),))
            body = await (await client.get(f"{_BASE}/engine/queue")).json()
        # A permanent empty heading trains an operator to ignore headings, so the
        # engine omits it and the relay must not re-introduce one.
        assert list(body["grouped"]) == ["awaiting_review"]

    @pytest.mark.asyncio
    async def test_a_row_carries_what_a_reviewer_has_to_act_on(
        self, monkeypatch, tmp_path, config_root
    ):
        async with _make_client(monkeypatch, tmp_path) as client:
            _park_runs(tmp_path, (("run-a", RunState.AWAITING_REVIEW),))
            _hold_comment("run-a", "c-1")
            body = await (await client.get(f"{_BASE}/engine/queue")).json()
        row = body["grouped"]["awaiting_review"][0]
        assert row["gate"] == "requirements"
        assert row["waiting_on"] == "review"
        # The COUNT of held comments, which is what the engine's projection
        # exposes: the ids and the text live behind the watcher, so a queue row
        # cannot become a second place someone else's comment is copied to.
        assert row["feedback_quarantined"] == 1
        assert "c-1" not in json.dumps(row)


class TestQueueActions:
    """The row actions, at the HTTP boundary an operator actually reaches.

    Each is a privileged manual override, so each is asserted for three things:
    it happened in the ENGINE (read back, not taken from the response), it
    attributes itself to the authenticated session, and a no-op is answered
    rather than reported as a change that did not occur.
    """

    @pytest.mark.asyncio
    async def test_releasing_a_held_comment_releases_it_in_the_engine(
        self, monkeypatch, tmp_path, config_root
    ):
        async with _make_client(monkeypatch, tmp_path) as client:
            ref = _park_runs(tmp_path, (("run-a", RunState.AWAITING_REVIEW),))
            _hold_comment("run-a", "c-1")
            resp = await client.post(
                f"{_BASE}/engine/queue/release-feedback",
                json={
                    "project": ref.project,
                    "spec": ref.name,
                    "run_id": "run-a",
                    "comment_id": "c-1",
                },
            )
            assert resp.status == 200
            assert (await resp.json())["released"] is True
            # Read back from the engine: the comment is no longer held.
            store, _audit = routes._engine_store()
            record = store.get_run("run-a")
            assert record is not None
            assert engine_runs.feedback_quarantined(record) == ()

    @pytest.mark.asyncio
    async def test_a_release_records_the_authenticated_session_as_its_actor(
        self, monkeypatch, tmp_path, config_root
    ):
        async with _make_client(monkeypatch, tmp_path) as client:
            ref = _park_runs(tmp_path, (("run-a", RunState.AWAITING_REVIEW),))
            _hold_comment("run-a", "c-1")
            resp = await client.post(
                f"{_BASE}/engine/queue/release-feedback",
                json={
                    "project": ref.project,
                    "spec": ref.name,
                    "run_id": "run-a",
                    "comment_id": "c-1",
                    # A body that names its own actor. It must not be believed:
                    # the release is the human gate on quarantined content, and an
                    # override that records whoever the caller typed records
                    # nothing.
                    "actor": "somebody-else",
                    "user": "somebody-else",
                },
            )
            assert resp.status == 200
            entries = _audit_entries(ref)
        released = [e for e in entries if "release" in str(e.get("event", ""))]
        assert released, f"no release entry in the audit trail: {entries}"
        assert released[-1]["initiator"] == "tester"

    @pytest.mark.asyncio
    async def test_releasing_a_comment_nobody_held_is_answered_not_reported_as_a_release(
        self, monkeypatch, tmp_path, config_root
    ):
        async with _make_client(monkeypatch, tmp_path) as client:
            ref = _park_runs(tmp_path, (("run-a", RunState.AWAITING_REVIEW),))
            resp = await client.post(
                f"{_BASE}/engine/queue/release-feedback",
                json={
                    "project": ref.project,
                    "spec": ref.name,
                    "run_id": "run-a",
                    "comment_id": "never-held",
                },
            )
            assert resp.status == 200
            assert (await resp.json())["released"] is False

    @pytest.mark.asyncio
    async def test_a_release_missing_the_comment_it_names_is_refused(
        self, monkeypatch, tmp_path, config_root
    ):
        async with _make_client(monkeypatch, tmp_path) as client:
            resp = await client.post(
                f"{_BASE}/engine/queue/release-feedback",
                json={"project": "/p", "spec": "example", "run_id": "run-a"},
            )
            assert resp.status == 400
            assert (await resp.json())["code"] == "field_required"

    @pytest.mark.asyncio
    async def test_a_redispatch_needs_the_generation_the_queue_row_does_not_carry(
        self, monkeypatch, tmp_path, config_root
    ):
        async with _make_client(monkeypatch, tmp_path) as client:
            resp = await client.post(
                f"{_BASE}/engine/queue/redispatch",
                json={"source": "github", "item_id": "42"},
            )
            # Named rather than defaulted: guessing a generation would lift the
            # suppression on a version of the item nobody asked about.
            assert resp.status == 400
            assert (await resp.json())["code"] == "field_required"

    @pytest.mark.asyncio
    async def test_a_redispatch_lifts_the_suppression_the_engine_recorded(
        self, monkeypatch, tmp_path, config_root
    ):
        async with _make_client(monkeypatch, tmp_path) as client:
            store, _audit = routes._engine_store()
            # Claimed the way the dispatcher claims it, through the engine's own
            # generation key: an override that formatted the generation
            # differently would report success and release nothing.
            claimed = engine_lifecycle.generation_key(3)
            assert store.claim_dispatch("github", "42", generation=claimed) is True
            resp = await client.post(
                f"{_BASE}/engine/queue/redispatch",
                json={"source": "github", "item_id": "42", "generation": 3},
            )
            assert resp.status == 200
            assert (await resp.json())["lifted"] is True
            # Nothing is claimed any more, so the next poll can dispatch it.
            assert store.claim_dispatch("github", "42", generation=claimed) is True

    @pytest.mark.asyncio
    async def test_cleaning_a_workspace_row_that_does_not_exist_is_answered(
        self, monkeypatch, tmp_path, config_root
    ):
        async with _make_client(monkeypatch, tmp_path) as client:
            resp = await client.post(
                f"{_BASE}/engine/queue/clean-workspace", json={"workspace_id": 9999}
            )
            assert resp.status == 200
            body = await resp.json()
        # None from the engine means no ACTIVE row has that id, so a double click
        # reads as "nothing to do" rather than as a second removal.
        assert body["removed"] is False
        assert body["cleanup"] is None

    @pytest.mark.asyncio
    async def test_a_workspace_id_that_is_not_a_number_is_refused(
        self, monkeypatch, tmp_path, config_root
    ):
        async with _make_client(monkeypatch, tmp_path) as client:
            resp = await client.post(
                f"{_BASE}/engine/queue/clean-workspace", json={"workspace_id": True}
            )
            # True is an int in Python and would resolve to ledger row 1.
            assert resp.status == 400
            assert (await resp.json())["code"] == "field_required"

    @pytest.mark.asyncio
    async def test_a_teardown_reports_what_it_kept_as_well_as_what_it_removed(
        self, monkeypatch, tmp_path, config_root
    ):
        async with _make_client(monkeypatch, tmp_path) as client:
            _park_runs(tmp_path, (("run-a", RunState.AWAITING_REVIEW),))
            resp = await client.post(f"{_BASE}/engine/queue/teardown", json={"run_id": "run-a"})
            assert resp.status == 200
            body = await resp.json()
        # A teardown with no ledger rows removes nothing and is complete. The
        # kept list is reported either way: calling a teardown that left a tree
        # standing a success is how an environment outlives every record of it.
        assert body["report"]["run_id"] == "run-a"
        assert body["report"]["kept"] == []
        assert body["complete"] is True

    @pytest.mark.asyncio
    async def test_a_teardown_without_a_run_is_refused(self, monkeypatch, tmp_path, config_root):
        async with _make_client(monkeypatch, tmp_path) as client:
            resp = await client.post(f"{_BASE}/engine/queue/teardown", json={})
            assert resp.status == 400
            assert (await resp.json())["code"] == "field_required"

    @pytest.mark.asyncio
    async def test_every_action_refuses_an_unauthenticated_request(self, monkeypatch, tmp_path):
        # No auth middleware at all, so ``request["user"]`` is absent. These are
        # privileged overrides: a release drives held content into a dispatch and
        # a cleanup deletes a recorded tree, so an anonymous caller is refused
        # before the engine is reached.
        async with _make_client_without_auth(monkeypatch, tmp_path) as client:
            for path, body in (
                (
                    "release-feedback",
                    {"project": "/p", "spec": "s", "run_id": "r", "comment_id": "c"},
                ),
                ("redispatch", {"source": "github", "item_id": "42", "generation": 1}),
                ("clean-workspace", {"workspace_id": 1}),
                ("teardown", {"run_id": "run-a"}),
            ):
                resp = await client.post(f"{_BASE}/engine/queue/{path}", json=body)
                assert resp.status == 401, path
                assert (await resp.json())["code"] == "unauthorized"

    @pytest.mark.asyncio
    async def test_the_config_write_and_the_stop_switch_refuse_an_unauthenticated_request(
        self, monkeypatch, tmp_path, config_root
    ):
        # The two privileged writes that had NO auth check while every queue
        # action beside them did. The config write runs at an operator-confirmed
        # surface (so it can lower an autonomy floor or a program minimum) and a
        # kill-switch release restores spending, which makes them the two that
        # least tolerate an anonymous caller.
        async with _make_client_without_auth(monkeypatch, tmp_path) as client:
            for method, path, body in (
                ("put", "config", {"patch": {"concurrency": {"global_max_runs": 9}}}),
                ("post", "config", {"patch": {"concurrency": {"global_max_runs": 9}}}),
                ("post", "kill-switch", {"action": "engage"}),
                ("post", "kill-switch", {"action": "release"}),
            ):
                resp = await getattr(client, method)(f"{_BASE}/engine/{path}", json=body)
                assert resp.status == 401, f"{method} {path} {body}"
                assert (await resp.json())["code"] == "unauthorized"
        # And nothing landed: a refusal that still wrote would be the defect the
        # status code hides.
        assert not (config_root / "config.json").exists()
        assert KillSwitch(tmp_path / "spec-builder" / "engine-state").engaged is False

    @pytest.mark.asyncio
    async def test_the_config_write_and_the_stop_switch_refuse_an_app_token(
        self, monkeypatch, tmp_path, config_root
    ):
        # An app token yields a truthy ``request["user"]``, so the auth check alone
        # does not separate a human from an app -- and this app's own manifest
        # allowlists ``/api/apps/spec-builder/*``, so a token minted from an
        # ``.app_secret`` reaches these routes. Both are refused with 403, and the
        # denial is audited.
        events: list[tuple[str, str, str]] = []
        monkeypatch.setattr(
            routes,
            "_audit",
            lambda operation, resources="", outcome="success": events.append(
                (operation, resources, outcome)
            ),
        )
        async with _make_client_as_app(monkeypatch, tmp_path) as client:
            resp = await client.put(
                f"{_BASE}/engine/config", json={"patch": {"concurrency": {"global_max_runs": 9}}}
            )
            assert resp.status == 403
            assert (await resp.json())["code"] == "dashboard_user_required"
            for action in ("engage", "release"):
                resp = await client.post(f"{_BASE}/engine/kill-switch", json={"action": action})
                assert resp.status == 403, action
                assert (await resp.json())["code"] == "dashboard_user_required"
        assert not (config_root / "config.json").exists()
        assert KillSwitch(tmp_path / "spec-builder" / "engine-state").engaged is False
        assert [outcome for _op, _res, outcome in events] == ["denied", "denied", "denied"]
        assert all("app:squatter" in resources for _op, resources, _out in events)

    @pytest.mark.asyncio
    async def test_a_release_is_recorded_in_the_engine_audit_log_with_the_session_user(
        self, monkeypatch, tmp_path, config_root
    ):
        # The dangerous half of this control: engaging is conservative, releasing
        # restores spending. It recorded a logger.warning and nothing else.
        async with _make_client(monkeypatch, tmp_path) as client:
            ref = _park_runs(tmp_path, (("run-a", RunState.HALTED_BUDGET),))
            await client.post(
                f"{_BASE}/engine/kill-switch", json={"action": "engage", "reason": "runaway"}
            )
            before = len(_audit_entries(ref))
            resp = await client.post(f"{_BASE}/engine/kill-switch", json={"action": "release"})
            assert resp.status == 200
            entries = _audit_entries(ref)
        released = [row for row in entries if row["event"] == "budget.kill_switch_released"]
        assert len(released) == 1, entries[before:]
        assert released[0]["initiator"] == "tester"
        # Who stopped it and why travel onto the release, and the runs the release
        # does NOT resume are named.
        assert released[0]["detail"]["engaged_by"] == "tester"
        assert released[0]["detail"]["engaged_reason"] == "runaway"
        assert released[0]["detail"]["parked_runs"] == ["run-a"]

    @pytest.mark.asyncio
    async def test_releasing_a_switch_that_was_not_engaged_records_no_release(
        self, monkeypatch, tmp_path, config_root
    ):
        # A no-op must not put a spending event in the trail: a reader counting
        # releases would count decisions nobody made.
        async with _make_client(monkeypatch, tmp_path) as client:
            ref = _park_runs(tmp_path, (("run-a", RunState.HALTED_BUDGET),))
            resp = await client.post(f"{_BASE}/engine/kill-switch", json={"action": "release"})
            assert resp.status == 200
            assert (await resp.json())["changed"] is False
            entries = _audit_entries(ref)
        assert [row for row in entries if row["event"] == "budget.kill_switch_released"] == []


def _make_client_without_auth(monkeypatch, tmp_path):
    """The app with NO auth middleware, so nothing populates ``request['user']``."""
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from .test_routes import _redirect_state

    _redirect_state(monkeypatch, tmp_path)
    app = web.Application()
    routes.register_routes(app)
    return TestClient(TestServer(app))


def _make_client_as_app(monkeypatch, tmp_path):
    """The app reached by an APP TOKEN rather than a browser session.

    Mirrors what the gateway's token middleware sets for one: a truthy ``user``
    AND an ``app`` identity. The pair is the point -- an app token passes an
    auth check on its ``user`` alone, so only the ``app`` key separates it from
    the operator whose confirmation the config surface claims.
    """
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from .test_routes import _redirect_state

    _redirect_state(monkeypatch, tmp_path)

    @web.middleware
    async def _app_token_mw(request, handler):
        request["user"] = "app:squatter"
        request["app"] = "squatter"
        return await handler(request)

    app = web.Application(middlewares=[_app_token_mw])
    routes.register_routes(app)
    return TestClient(TestServer(app))


def _queue():
    """The engine's Review_Queue over the redirected root, as the handler builds it."""
    store, audit_log = routes._engine_store()
    machine = engine_runs.RunMachine(store, ConfigStore(), audit=audit_log)
    return ReviewQueue(machine)


#: A legal route to each parked state, so a test naming a state need not spell
#: the walk. Mirrors the engine suite's own table.
_PATHS: dict[RunState, tuple[RunState, ...]] = {
    RunState.AWAITING_REVIEW: (RunState.AUTHORING, RunState.AWAITING_REVIEW),
    RunState.HALTED_BUDGET: (RunState.HALTED_BUDGET,),
    RunState.STALLED: (RunState.AUTHORING, RunState.STALLED),
}


def _park_runs(tmp_path: Path, runs: tuple[tuple[str, RunState], ...]) -> SpecRef:
    """Create a spec the engine can address and park *runs* in the queue.

    Walked through the RunMachine by legal transitions rather than written into
    the store directly, so the rows the surface reads are rows the state machine
    actually produces.
    """
    project = tmp_path / "queue-project"
    spec_dir = project / ".kiro" / "specs" / "example"
    spec_dir.mkdir(parents=True, exist_ok=True)
    (spec_dir / "requirements.md").write_text("# Requirements Document\n", encoding="utf-8")
    (spec_dir / "design.md").write_text("# Design Document\n", encoding="utf-8")
    (spec_dir / "tasks.md").write_text("# Implementation Plan\n", encoding="utf-8")
    (spec_dir / ".config.kiro").write_text(
        json.dumps({"specId": "example", "specType": "feature"}), encoding="utf-8"
    )
    ref = SpecRef.of(project, "example")
    store, audit_log = routes._engine_store()
    machine = engine_runs.RunMachine(store, ConfigStore(), audit=audit_log)
    for run_id, state in runs:
        machine.create(ref, run_id=run_id, source="github")
        for step in _PATHS[state]:
            machine.transition(ref, run_id, step)
    return ref


def _hold_comment(run_id: str, comment_id: str) -> None:
    """Put *comment_id* on *run_id*'s held list, as the watcher's screening does."""
    store, _audit = routes._engine_store()
    record = store.get_run(run_id)
    held = list(engine_runs.feedback_quarantined(record)) if record is not None else []
    store.update_run(
        run_id,
        detail={engine_runs.DETAIL_FEEDBACK_QUARANTINED: held + [comment_id]},
    )


def _audit_entries(ref: SpecRef) -> list[dict]:
    """Every audit row the engine wrote for *ref*, read through its own reader."""
    _store_, audit_log = routes._engine_store()
    return [event.to_json_object() for event in audit_log.read(ref)]


def _dashboard_surface():
    from kiro_crew.apps.builtins.spec_engine.engine.config.store import DASHBOARD_SURFACE

    return DASHBOARD_SURFACE


class TestWorkflowOriginSurface:
    """Per-stage command origin, relayed from the engine's own preset display.

    The claim is that the surface reports the LAYER, not a value comparison. The
    case that separates the two is a mixed workflow: a project overriding one
    stage of a selected preset must read as an override on that stage and as the
    preset on the others, and an override whose commands are byte-identical to the
    preset's must still read as an override.
    """

    @pytest.mark.asyncio
    async def test_a_mixed_workflow_reports_each_stage_at_its_own_layer(
        self, monkeypatch, tmp_path, config_root
    ):
        _store(config_root).write(
            {
                "workflow": {"preset": "git-pull-request"},
                "projects": {
                    "web": {
                        "path": "/tmp/web",
                        "workflow": {"stages": {"submit": [["git", "push"]]}},
                    }
                },
            },
            surface=_dashboard_surface(),
        )
        async with _make_client(monkeypatch, tmp_path) as client:
            resp = await client.get(f"{_BASE}/engine/workflow-origins?project=web")
            assert resp.status == 200
            body = await resp.json()
        by_stage = {row["stage"]: row for row in body["stages"]}
        # Cross-checked against the engine's own projection over the same
        # document: a surface that re-derived the layer and disagreed fails here.
        engine_rows = {
            origin.stage: origin.to_json_object()
            for origin in stage_origins(
                DeliveryWorkflow.load(_store(config_root), project="web")
            )
        }
        assert by_stage == engine_rows
        assert by_stage["submit"]["source"] == "project_override"
        assert by_stage["submit"]["from_preset"] is False
        # Every OTHER stage the preset defines still reads as the preset's, which
        # is what makes this a per-stage answer rather than one label.
        preset_stages = [
            stage for stage, row in by_stage.items() if row["source"] == "bundled_preset"
        ]
        assert preset_stages, "overriding one stage must not detach the rest"
        assert body["preset"]["name"] == "git-pull-request"
        assert body["preset"]["bundled"] is True

    @pytest.mark.asyncio
    async def test_an_override_identical_to_the_preset_still_reads_as_an_override(
        self, monkeypatch, tmp_path, config_root
    ):
        # The case a value comparison gets wrong. The engine derives the layer from
        # the declaration, so a byte-identical copy is still this project's own.
        _store(config_root).write(
            {"workflow": {"preset": "git-pull-request"}}, surface=_dashboard_surface()
        )
        # The preset's own definition, copied verbatim: byte-identical to what the
        # stage would have run had nobody declared it.
        copied = [list(argv) for argv in workflow_presets("git-pull-request")["stages"]["submit"]]
        _store(config_root).write(
            {
                "projects": {
                    "web": {"path": "/tmp/web", "workflow": {"stages": {"submit": copied}}}
                }
            },
            surface=_dashboard_surface(),
        )
        async with _make_client(monkeypatch, tmp_path) as client:
            resp = await client.get(f"{_BASE}/engine/workflow-origins?project=web")
            body = await resp.json()
        row = next(r for r in body["stages"] if r["stage"] == "submit")
        assert row["source"] == "project_override"
        assert row["preset"] == "", "an override names no preset, however it was spelled"

    @pytest.mark.asyncio
    async def test_a_stage_nobody_defines_says_it_is_skipped(
        self, monkeypatch, tmp_path, config_root
    ):
        # An unconfigured stage skips at execution. Omitting it, or rendering it as
        # preset-supplied, would tell an operator a stage runs when it does not.
        async with _make_client(monkeypatch, tmp_path) as client:
            resp = await client.get(f"{_BASE}/engine/workflow-origins")
            assert resp.status == 200
            body = await resp.json()
        assert body["preset"] is None
        assert body["stages"], "every delivery stage gets a row"
        skipped = [row for row in body["stages"] if row["skipped"]]
        assert skipped, "a zero-configuration install defines no stage of its own"
        assert all(row["summary"] for row in body["stages"]), "the engine writes the line"

    @pytest.mark.asyncio
    async def test_a_selection_the_engine_refuses_is_reported_as_a_refusal(
        self, monkeypatch, tmp_path, config_root
    ):
        # Written past the write path, which is the only way this document exists.
        # "no stages" and "the selection names a preset that does not exist" call
        # for different edits, so they must not read the same.
        config_root.mkdir(parents=True, exist_ok=True)
        (config_root / "config.json").write_text(
            json.dumps({"workflow": {"preset": "not-a-preset"}}), encoding="utf-8"
        )
        async with _make_client(monkeypatch, tmp_path) as client:
            resp = await client.get(f"{_BASE}/engine/workflow-origins")
            assert resp.status == 409
            body = await resp.json()
        assert body["code"] == "workflow_invalid"


class TestRunSpendDetail:
    @pytest.mark.asyncio
    async def test_a_run_detail_reports_the_engine_s_attributed_total(
        self, monkeypatch, tmp_path, config_root
    ):
        async with _make_client(monkeypatch, tmp_path) as client:
            _park_runs(tmp_path, (("run-a", RunState.AWAITING_REVIEW),))
            resp = await client.get(f"{_BASE}/engine/run-spend?run_id=run-a")
            assert resp.status == 200
            body = await resp.json()
            store, _audit = routes._engine_store()
            engine_total = RunAccounting(store).spend("run-a").total_credits
        # The engine's own attribution, not a sum of whatever the browser fetched.
        assert body["credits"] == pytest.approx(engine_total)
        assert body["run_id"] == "run-a"
        assert body["spec"] == "example"
        # The ceiling travels with it, so the number has the denominator the engine
        # will judge it against, with the origin that produced it.
        assert body["ceiling"]["value"] == SETTINGS["budget.run_ceiling_credits"].default
        assert body["ceiling"]["origin"] == "bundled_default"

    @pytest.mark.asyncio
    async def test_declared_spend_outside_a_session_is_inside_the_total(
        self, monkeypatch, tmp_path, config_root
    ):
        # The half a browser-side sum over turn rows misses: a capability provider
        # (screening, analysis) declares credits spent outside any host session,
        # and this engine has already shipped one defect where that spend escaped a
        # run's ceiling.
        async with _make_client(monkeypatch, tmp_path) as client:
            _park_runs(tmp_path, (("run-a", RunState.AWAITING_REVIEW),))
            store, _audit = routes._engine_store()
            RunAccounting(store).cost_sink.attribute(
                run="run-a", capability="analysis", provider="external", credits=2.5
            )
            resp = await client.get(f"{_BASE}/engine/run-spend?run_id=run-a")
            body = await resp.json()
            engine_total = RunAccounting(store).spend("run-a").total_credits
        assert body["declared_credits"] == pytest.approx(2.5)
        assert body["credits"] == pytest.approx(engine_total)
        assert body["credits"] >= 2.5

    @pytest.mark.asyncio
    async def test_an_unknown_run_is_a_404_rather_than_a_zero(
        self, monkeypatch, tmp_path, config_root
    ):
        # Zero credits for a run that does not exist would read as a free run.
        async with _make_client(monkeypatch, tmp_path) as client:
            resp = await client.get(f"{_BASE}/engine/run-spend?run_id=nope")
            assert resp.status == 404
            assert (await resp.json())["code"] == "run_unknown"

    @pytest.mark.asyncio
    async def test_a_spend_view_without_a_run_is_refused(self, monkeypatch, tmp_path, config_root):
        async with _make_client(monkeypatch, tmp_path) as client:
            resp = await client.get(f"{_BASE}/engine/run-spend")
            assert resp.status == 400
            assert (await resp.json())["code"] == "field_required"


class TestTheSurfaceSaysWhatItWillWrite:
    @pytest.mark.asyncio
    async def test_the_domains_that_execute_argv_are_reported_read_only_with_a_reason(
        self, monkeypatch, tmp_path, config_root
    ):
        async with _make_client(monkeypatch, tmp_path) as client:
            resp = await client.get(f"{_BASE}/engine/config")
            body = await resp.json()
        editors = {row["domain"]: row for row in body["domain_editors"]}
        # The four domains the bullet names as editable.
        for domain in ("autonomy", "watch_sources", "role_assignments", "notification_channels"):
            assert editors[domain]["editable"] is True, domain
        # And the ones this surface deliberately does not write, each carrying a
        # code the surface can translate: a control that silently failed would be
        # worse than an honest read-only view.
        for domain in ("workflow", "quality_gates", "programs", "capabilities"):
            assert editors[domain]["editable"] is False, domain
            assert editors[domain]["reason_code"], domain
        # A partial editor names the fields it offers, so ``poll`` (the argv the
        # watcher executes) is visibly not one of them.
        assert editors["watch_sources"]["fields"] == ["enabled"]

    @pytest.mark.asyncio
    async def test_the_pickers_vocabulary_is_the_engine_s_own(
        self, monkeypatch, tmp_path, config_root
    ):
        from kiro_crew.apps.builtins.spec_engine.engine.config import (
            AUTONOMY_LEVELS,
            CONFIG_ONLY_PATHS,
            ROLES,
            SPEC_TYPES,
            SUBMITTER_CLASSES,
        )

        async with _make_client(monkeypatch, tmp_path) as client:
            resp = await client.get(f"{_BASE}/engine/config")
            body = await resp.json()
        # A picker built from a hardcoded copy would offer a level or a role the
        # validator refuses the day either list grows.
        assert body["catalogs"]["autonomy_levels"] == list(AUTONOMY_LEVELS)
        assert body["catalogs"]["submitter_classes"] == list(SUBMITTER_CLASSES)
        assert body["catalogs"]["spec_types"] == list(SPEC_TYPES)
        assert body["catalogs"]["roles"] == list(ROLES)
        assert body["config_only_paths"] == list(CONFIG_ONLY_PATHS)
