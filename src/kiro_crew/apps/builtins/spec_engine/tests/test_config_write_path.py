"""The single validated write path.

What these tests hold: every surface writes through one door, that door
validates the merged result before anything lands on disk, a failed write leaves
the previous document intact, and the config-only objects — the autonomy policy,
the delivery workflow, capability bindings — cannot be written by a surface no
human confirmed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew import platform_compat
from kiro_crew.apps.builtins.spec_engine.engine.config import (
    APP_NAME,
    CONFIG_FILENAME,
    CURRENT_VERSION,
    DASHBOARD_SURFACE,
    SETUP_ASSISTANT_SURFACE,
    ConfigLoadError,
    ConfigStore,
    ConfigValidationError,
    ConfigWriteRefused,
    ConfigWriteSurface,
    ValueOrigin,
    config_only_paths,
    default_root,
)

#: A surface with no human watching. Stands in for any future non-interactive
#: writer; the config-only guard must refuse it.
UNCONFIRMED_SURFACE = ConfigWriteSurface("automation")


@pytest.fixture()
def store(tmp_path: Path) -> ConfigStore:
    return ConfigStore(tmp_path / "state")


class TestWritePath:
    def test_write_persists_and_is_readable_as_an_effective_value(self, store: ConfigStore):
        store.write({"limits": {"task_retry_limit": 6}}, surface=DASHBOARD_SURFACE)
        effective = store.effective("limits.task_retry_limit")
        assert effective.value == 6
        assert effective.origin is ValueOrigin.APP_CONFIG
        assert store.path.name == CONFIG_FILENAME
        assert json.loads(store.path.read_text(encoding="utf-8"))["limits"]["task_retry_limit"] == 6

    def test_write_stamps_the_schema_version(self, store: ConfigStore):
        saved = store.write({"limits": {"task_retry_limit": 1}}, surface=DASHBOARD_SURFACE)
        assert saved["version"] == CURRENT_VERSION

    def test_writes_merge_rather_than_replace(self, store: ConfigStore):
        store.write(
            {"projects": {"acme": {"path": "/w/acme", "base_branch": "main"}}},
            surface=DASHBOARD_SURFACE,
        )
        store.write(
            {"projects": {"widgets": {"path": "/w/widgets"}}},
            surface=SETUP_ASSISTANT_SURFACE,
        )
        saved = store.document()
        assert set(saved["projects"]) == {"acme", "widgets"}
        assert saved["projects"]["acme"]["base_branch"] == "main"

    def test_a_null_value_removes_a_setting_and_restores_its_default(self, store: ConfigStore):
        store.write({"limits": {"task_retry_limit": 6}}, surface=DASHBOARD_SURFACE)
        store.write({"limits": {"task_retry_limit": None}}, surface=DASHBOARD_SURFACE)
        effective = store.effective("limits.task_retry_limit")
        assert effective.is_default
        assert "task_retry_limit" not in store.document().get("limits", {})

    def test_an_invalid_patch_is_refused_and_leaves_the_document_untouched(
        self, store: ConfigStore
    ):
        store.write({"limits": {"task_retry_limit": 6}}, surface=DASHBOARD_SURFACE)
        before = store.path.read_text(encoding="utf-8")
        with pytest.raises(ConfigValidationError) as caught:
            store.write({"limits": {"task_retry_limit": -3}}, surface=DASHBOARD_SURFACE)
        assert [e.path for e in caught.value.errors] == ["limits.task_retry_limit"]
        assert store.path.read_text(encoding="utf-8") == before

    def test_a_patch_valid_alone_but_invalid_when_merged_is_refused(self, store: ConfigStore):
        # The project entry is only complete once merged, so validation has to
        # run on the merged result rather than on the patch.
        with pytest.raises(ConfigValidationError):
            store.write({"projects": {"acme": {"base_branch": "main"}}}, surface=DASHBOARD_SURFACE)
        store.write({"projects": {"acme": {"path": "/w/acme"}}}, surface=DASHBOARD_SURFACE)
        store.write({"projects": {"acme": {"base_branch": "main"}}}, surface=DASHBOARD_SURFACE)
        assert store.document()["projects"]["acme"]["base_branch"] == "main"

    def test_a_non_object_patch_is_refused(self, store: ConfigStore):
        with pytest.raises(ConfigValidationError):
            store.write(["limits"], surface=DASHBOARD_SURFACE)  # type: ignore[arg-type]

    def test_the_document_is_owner_only(self, store: ConfigStore):
        if platform_compat.IS_WINDOWS:
            pytest.skip("no POSIX mode bits on Windows")
        store.write({"limits": {"task_retry_limit": 2}}, surface=DASHBOARD_SURFACE)
        assert store.path.stat().st_mode & 0o777 == 0o600

    def test_a_corrupt_document_fails_loudly(self, store: ConfigStore):
        store.root.mkdir(parents=True, exist_ok=True)
        store.path.write_text("{not json", encoding="utf-8")
        with pytest.raises(ConfigLoadError):
            store.document()

    def test_a_json_scalar_document_is_refused(self, store: ConfigStore):
        store.root.mkdir(parents=True, exist_ok=True)
        store.path.write_text("42", encoding="utf-8")
        with pytest.raises(ConfigLoadError):
            store.document()

    def test_validate_reports_problems_instead_of_raising(self, store: ConfigStore):
        store.root.mkdir(parents=True, exist_ok=True)
        store.path.write_text(json.dumps({"limits": {"nope": 1}}), encoding="utf-8")
        assert [e.path for e in store.validate()] == ["limits.nope"]


class TestConfigOnlyObjects:
    def test_config_only_paths_names_the_restricted_objects(self):
        patch = {
            "sources": {"s": {"enabled": True}},
            "workflow": {"stages": {}},
            "capabilities": {"analysis": {"transport": "builtin"}},
            "projects": {"acme": {"workflow": {"stages": {}}}, "widgets": {"path": "/w"}},
            "limits": {"task_retry_limit": 1},
        }
        assert config_only_paths(patch) == (
            "capabilities",
            "projects.acme.workflow",
            "sources",
            "workflow",
        )

    def test_ordinary_settings_are_not_restricted(self):
        assert config_only_paths({"limits": {"task_retry_limit": 1}}) == ()

    def test_the_integration_switch_is_restricted_at_both_scopes(self):
        # It is one setting rather than a whole section, but it co-gates
        # unattended integration alongside the autonomy ladder, and integration
        # is the stage a mistake cannot undo. A ladder no tool can widen buys
        # little if the other gate on the same action stays freely writable.
        assert config_only_paths({"delivery": {"auto_integrate": True}}) == (
            "delivery.auto_integrate",
        )
        assert config_only_paths(
            {"projects": {"acme": {"delivery": {"auto_integrate": True}}}}
        ) == ("projects.acme.delivery.auto_integrate",)

    def test_other_delivery_settings_stay_ordinary(self):
        # Only the integration switch is fenced; fencing the whole section would
        # make routine delivery settings need an operator-confirmed surface.
        assert config_only_paths({"delivery": {"base_branch": "main"}}) == ()

    @pytest.mark.parametrize(
        "patch",
        [
            {"delivery": {"auto_integrate": True}},
            {"projects": {"acme": {"path": "/w", "delivery": {"auto_integrate": True}}}},
        ],
    )
    def test_an_unconfirmed_surface_cannot_arm_unattended_integration(
        self, store: ConfigStore, patch: dict
    ):
        with pytest.raises(ConfigWriteRefused):
            store.write(patch, surface=UNCONFIRMED_SURFACE)
        assert not store.path.exists()

    @pytest.mark.parametrize(
        "patch",
        [
            {"sources": {"s": {"poll": ["gh"], "autonomy": {"external": {"default": "delivery"}}}}},
            {"workflow": {"stages": {"publish": [["gh", "pr", "merge"]]}}},
            {"capabilities": {"review": {"transport": "command", "command": ["x"]}}},
            {"projects": {"acme": {"path": "/w", "workflow": {"stages": {"verify": [["x"]]}}}}},
        ],
    )
    def test_an_unconfirmed_surface_cannot_write_a_config_only_object(
        self, store: ConfigStore, patch: dict
    ):
        with pytest.raises(ConfigWriteRefused) as caught:
            store.write(patch, surface=UNCONFIRMED_SURFACE)
        assert caught.value.surface is UNCONFIRMED_SURFACE
        assert caught.value.paths
        assert not store.path.exists()

    def test_an_unconfirmed_surface_may_still_write_ordinary_settings(self, store: ConfigStore):
        store.write({"limits": {"task_retry_limit": 1}}, surface=UNCONFIRMED_SURFACE)
        assert store.effective("limits.task_retry_limit").value == 1

    def test_operator_confirmed_surfaces_may_write_config_only_objects(self, store: ConfigStore):
        for surface in (DASHBOARD_SURFACE, SETUP_ASSISTANT_SURFACE):
            assert surface.operator_confirmed
        store.write(
            {"workflow": {"stages": {"verify": [["make", "test"]]}}},
            surface=SETUP_ASSISTANT_SURFACE,
        )
        assert store.document()["workflow"]["stages"]["verify"] == [["make", "test"]]


class TestStateLocation:
    def test_state_root_may_not_be_inside_a_spec_directory(self, tmp_path: Path):
        with pytest.raises(ValueError):
            ConfigStore(tmp_path / ".kiro" / "specs" / "my-feature")

    def test_state_lives_in_the_app_data_directory_not_a_spec_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # Engine state must be shareable across the IDE and CLI without
        # appearing inside the spec directories they both read.
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "crew"))
        root = default_root()
        assert root.parts[-3:] == ("apps", APP_NAME, "data")
        pairs = {root.parts[i : i + 2] for i in range(len(root.parts) - 1)}
        assert (".kiro", "specs") not in pairs
        assert ConfigStore().path == root / CONFIG_FILENAME
