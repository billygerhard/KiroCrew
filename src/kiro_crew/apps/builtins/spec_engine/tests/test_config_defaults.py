"""Bundled defaults and effective-value resolution.

The zero-configuration contract lives here: an install that configures nothing
must resolve every setting, and every surface must be able to tell an operator
whether the number on screen was chosen or shipped.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import settings as hyp_settings
from hypothesis import strategies as st

from kiro_crew.apps.builtins.spec_engine.engine.config import (
    SETTINGS,
    ConfigStore,
    ConfigValidationError,
    Scope,
    ValueOrigin,
    settings_in_scope,
)


@pytest.fixture()
def store(tmp_path: Path) -> ConfigStore:
    return ConfigStore(tmp_path / "state")


def _save(store: ConfigStore, doc: dict) -> None:
    """Persist a document directly, bypassing the write path.

    Used only to set up read-side cases (including deliberately invalid stored
    values, which the write path would never produce).
    """
    store.root.mkdir(parents=True, exist_ok=True)
    store.path.write_text(json.dumps(doc), encoding="utf-8")


class TestBundledDefaults:
    def test_every_setting_has_a_finite_default_of_its_declared_type(self):
        for key, setting in SETTINGS.items():
            assert setting.default is not None, key
            # coerce accepts its own default: the table cannot ship a default
            # that the validated write path would reject.
            assert setting.coerce(setting.default) == setting.default, key

    def test_absent_settings_resolve_to_defaults_without_a_config_file(self, store: ConfigStore):
        assert not store.path.exists()
        resolved = store.effective_settings()
        assert set(resolved) == set(SETTINGS)
        for key, effective in resolved.items():
            assert effective.value == SETTINGS[key].default
            assert effective.origin is ValueOrigin.BUNDLED_DEFAULT
            assert effective.is_default
            assert effective.declared_at == ""

    def test_absent_setting_alongside_configured_ones_still_resolves(self, store: ConfigStore):
        _save(store, {"limits": {"task_retry_limit": 7}})
        configured = store.effective("limits.task_retry_limit")
        absent = store.effective("limits.revision_cycle_limit")
        assert configured.value == 7
        assert not configured.is_default
        assert absent.value == SETTINGS["limits.revision_cycle_limit"].default
        assert absent.is_default

    def test_empty_and_whitespace_documents_are_the_zero_config_case(self, store: ConfigStore):
        store.root.mkdir(parents=True, exist_ok=True)
        store.path.write_text("   \n", encoding="utf-8")
        assert store.document() == {}
        assert store.effective("watch.interval_s").is_default

    def test_budget_ceiling_default_is_finite_and_positive(self):
        ceiling = SETTINGS["budget.run_ceiling_credits"]
        assert isinstance(ceiling.default, float)
        assert ceiling.default > 0

    def test_unknown_setting_lookup_is_a_key_error(self, store: ConfigStore):
        with pytest.raises(KeyError):
            store.effective("limits.no_such_limit")


class TestOrigin:
    def test_app_scope_value_reports_app_origin_and_its_path(self, store: ConfigStore):
        _save(store, {"concurrency": {"global_max_runs": 9}})
        effective = store.effective("concurrency.global_max_runs")
        assert effective.value == 9
        assert effective.origin is ValueOrigin.APP_CONFIG
        assert effective.declared_at == "concurrency.global_max_runs"
        assert not effective.is_default

    def test_project_override_beats_app_value(self, store: ConfigStore):
        _save(
            store,
            {
                "limits": {"task_retry_limit": 5},
                "projects": {"acme": {"path": "/w/acme", "limits": {"task_retry_limit": 1}}},
            },
        )
        assert store.effective("limits.task_retry_limit").value == 5
        scoped = store.effective("limits.task_retry_limit", project="acme")
        assert scoped.value == 1
        assert scoped.origin is ValueOrigin.PROJECT_CONFIG
        assert scoped.declared_at == "projects.acme.limits.task_retry_limit"

    def test_source_override_beats_app_value(self, store: ConfigStore):
        _save(
            store,
            {
                "watch": {"interval_s": 600},
                "sources": {"issues": {"poll": ["gh", "issue", "list"]}},
            },
        )
        # The source declares no interval of its own, so the app value stands.
        app_level = store.effective("watch.interval_s", source="issues")
        assert app_level.value == 600
        assert app_level.origin is ValueOrigin.APP_CONFIG

        _save(
            store,
            {
                "watch": {"interval_s": 600},
                "sources": {
                    "issues": {"poll": ["gh", "issue", "list"], "watch": {"interval_s": 60}}
                },
            },
        )
        scoped = store.effective("watch.interval_s", source="issues")
        assert scoped.value == 60
        assert scoped.origin is ValueOrigin.SOURCE_CONFIG
        assert scoped.declared_at == "sources.issues.watch.interval_s"

    def test_project_override_is_ignored_for_a_different_project(self, store: ConfigStore):
        _save(store, {"projects": {"acme": {"path": "/w/acme", "limits": {"task_retry_limit": 1}}}})
        other = store.effective("limits.task_retry_limit", project="widgets")
        assert other.is_default

    def test_explicit_value_equal_to_the_default_still_reports_explicit(self, store: ConfigStore):
        default = SETTINGS["limits.task_retry_limit"].default
        _save(store, {"limits": {"task_retry_limit": default}})
        effective = store.effective("limits.task_retry_limit")
        assert effective.value == default
        assert effective.origin is ValueOrigin.APP_CONFIG
        assert not effective.is_default

    def test_app_only_setting_ignores_a_project_argument(self, store: ConfigStore):
        _save(store, {"projects": {"acme": {"path": "/w/acme"}}})
        effective = store.effective("concurrency.global_max_runs", project="acme")
        assert effective.is_default

    def test_out_of_range_stored_value_is_named_rather_than_silently_defaulted(
        self, store: ConfigStore
    ):
        _save(store, {"watch": {"interval_s": 1}})
        with pytest.raises(ConfigValidationError) as caught:
            store.effective("watch.interval_s")
        assert [e.path for e in caught.value.errors] == ["watch.interval_s"]

    @hyp_settings(max_examples=50, deadline=None)
    @given(
        explicit=st.sets(
            st.sampled_from([s.key for s in settings_in_scope(Scope.APP)]),
            max_size=6,
        )
    )
    def test_origin_marks_exactly_the_explicitly_written_settings(self, explicit: set):
        # Every explicit value here equals its own bundled default, so origin is
        # the only thing that can tell the two apart — which is exactly what a
        # config surface needs and what value comparison cannot give it.
        with tempfile.TemporaryDirectory() as tmp:
            store = ConfigStore(Path(tmp) / "state")
            doc: dict = {}
            for key in explicit:
                setting = SETTINGS[key]
                doc.setdefault(setting.group, {})[setting.leaf] = setting.default
            _save(store, doc)

            resolved = store.effective_settings()
            assert {k for k, v in resolved.items() if not v.is_default} == explicit
            for key, value in resolved.items():
                assert value.value == SETTINGS[key].default
                assert value.declared_at == (key if key in explicit else "")
