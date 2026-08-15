"""What the configuration surface shows per stage: preset, override, or nothing.

The commands a stage runs come from one of three layers, and the display exists
because the resulting command list does not say which. Four cases decide whether
it is right, and three of them are the ones an easy test skips:

* a stage the selected preset supplied,
* a stage the project overrode,
* a stage **nobody** defines, which skips at execution and must not read as the
  preset's,
* a stage from a **user-defined** preset, which is a document declaration rather
  than engine-authored commands.

The display is also pinned to the resolver rather than to a second precedence
rule: for any configuration, a row says "from the preset" exactly when
:meth:`DeliveryWorkflow.stage` says the preset supplied it. A display that
derived origin by diffing the project's stages against the bundled table could
pass every example above and still disagree with what runs.
"""

from __future__ import annotations

from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from kiro_crew.apps.builtins.spec_engine.engine.config import DASHBOARD_SURFACE, ConfigStore
from kiro_crew.apps.builtins.spec_engine.engine.config.schema import DELIVERY_STAGES
from kiro_crew.apps.builtins.spec_engine.engine.delivery import (
    DELIVERY_FLOW_STAGES,
    DeliveryWorkflow,
    StageOrigin,
    StageSource,
    stage_origins,
)
from kiro_crew.apps.builtins.spec_engine.engine.delivery.preset_display import (
    MAX_PRESET_NAME_CHARS,
)
from kiro_crew.apps.builtins.spec_engine.engine.delivery.workflow import (
    ISOLATE_STAGE,
    WORKFLOW_PRESET_NAMES,
)

#: A user-defined preset: an organization's own review system, which is the case
#: user definitions exist for. Deliberately not a copy of a bundled one.
ORG_PRESET: dict[str, Any] = {
    "stages": {
        ISOLATE_STAGE: [["git", "worktree", "add", "{isolated_path}", "-b", "{branch_name}"]],
        "submit": [["org-review", "create", "--title", "{review_title}"]],
    }
}


@pytest.fixture()
def store(tmp_path: Any) -> ConfigStore:
    return ConfigStore(tmp_path / "state")


def configure(store: ConfigStore, document: dict[str, Any]) -> None:
    """Write through the operator surface, so the document is one the write path
    accepted rather than one hand-built past it."""
    store.write(document, surface=DASHBOARD_SURFACE)


def rows(workflow: DeliveryWorkflow) -> dict[str, StageOrigin]:
    return {row.stage: row for row in stage_origins(workflow)}


class TestAStageFromTheSelectedPreset:
    @pytest.mark.parametrize("name", WORKFLOW_PRESET_NAMES)
    def test_it_names_the_bundled_preset_it_came_from(self, store: ConfigStore, name: str) -> None:
        configure(store, {"workflow": {"preset": name}})
        row = rows(DeliveryWorkflow.load(store))[ISOLATE_STAGE]
        assert row.source is StageSource.BUNDLED_PRESET
        assert row.source.from_preset
        assert row.preset == name
        assert not row.skipped
        assert row.commands > 0

    def test_the_line_a_human_reads_says_bundled_and_names_the_preset(
        self, store: ConfigStore
    ) -> None:
        configure(store, {"workflow": {"preset": "git-pull-request"}})
        line = rows(DeliveryWorkflow.load(store))[ISOLATE_STAGE].describe()
        assert "bundled preset 'git-pull-request'" in line
        assert "override" not in line


class TestAStageTheProjectOverrode:
    @pytest.fixture()
    def overridden(self, store: ConfigStore) -> DeliveryWorkflow:
        configure(
            store,
            {
                "projects": {
                    "acme": {
                        "path": "/somewhere",
                        "workflow": {
                            "preset": "git-pull-request",
                            "stages": {"submit": [["org-review", "create"]]},
                        },
                    }
                }
            },
        )
        return DeliveryWorkflow.load(store, project="acme")

    def test_the_overridden_stage_reads_as_the_projects_own(
        self, overridden: DeliveryWorkflow
    ) -> None:
        row = rows(overridden)["submit"]
        assert row.source is StageSource.PROJECT_OVERRIDE
        assert not row.source.from_preset
        assert row.preset == ""
        assert row.declared_at == "projects.acme.workflow.stages.submit"

    def test_the_stages_it_did_not_override_still_read_as_the_presets(
        self, overridden: DeliveryWorkflow
    ) -> None:
        assert rows(overridden)[ISOLATE_STAGE].source is StageSource.BUNDLED_PRESET

    def test_the_line_says_where_to_edit_it(self, overridden: DeliveryWorkflow) -> None:
        line = rows(overridden)["submit"].describe()
        assert "overridden by this project" in line
        assert "projects.acme.workflow.stages.submit" in line

    def test_an_app_wide_declaration_is_named_as_such_not_as_the_projects(
        self, store: ConfigStore
    ) -> None:
        configure(
            store,
            {
                "workflow": {
                    "preset": "local-only",
                    "stages": {"verify": [["make", "check"]]},
                },
                "projects": {"acme": {"path": "/somewhere"}},
            },
        )
        row = rows(DeliveryWorkflow.load(store, project="acme"))["verify"]
        assert row.source is StageSource.APP_OVERRIDE
        assert row.declared_at == "workflow.stages.verify"


class TestAStageNobodyDefines:
    def test_an_undefined_stage_of_a_selected_preset_reads_as_skipped(
        self, store: ConfigStore
    ) -> None:
        # The two remote presets deliberately ship no verify stage, and none of
        # them ships teardown: those stages run nothing until a project says so.
        configure(store, {"workflow": {"preset": "git-pull-request"}})
        resolved = rows(DeliveryWorkflow.load(store))
        for stage in ("verify", "publish", "teardown"):
            row = resolved[stage]
            assert row.source is StageSource.UNCONFIGURED, stage
            assert row.skipped
            assert not row.source.from_preset
            assert row.preset == ""
            assert row.declared_at == ""
            assert row.commands == 0

    def test_it_says_the_stage_is_skipped_rather_than_saying_nothing(
        self, store: ConfigStore
    ) -> None:
        configure(store, {"workflow": {"preset": "git-pull-request"}})
        assert "skipped" in rows(DeliveryWorkflow.load(store))["publish"].describe()

    def test_every_stage_appears_even_with_no_workflow_at_all(self, store: ConfigStore) -> None:
        configure(store, {"projects": {"acme": {"path": "/somewhere"}}})
        resolved = stage_origins(DeliveryWorkflow.load(store, project="acme"))
        assert tuple(row.stage for row in resolved) == DELIVERY_STAGES
        assert all(row.skipped for row in resolved)


class TestAUserDefinedPreset:
    @pytest.fixture()
    def selected(self, store: ConfigStore) -> DeliveryWorkflow:
        configure(
            store,
            {
                "workflow": {"presets": {"org-flow": ORG_PRESET}},
                "projects": {"acme": {"path": "/somewhere", "workflow": {"preset": "org-flow"}}},
            },
        )
        return DeliveryWorkflow.load(store, project="acme")

    def test_it_is_not_flattened_into_the_bundled_case(self, selected: DeliveryWorkflow) -> None:
        row = rows(selected)["submit"]
        assert row.source is StageSource.USER_PRESET
        assert row.source.from_preset
        assert not row.source.bundled
        assert row.preset == "org-flow"

    def test_the_line_says_user_defined_and_points_at_the_definition(
        self, selected: DeliveryWorkflow
    ) -> None:
        line = rows(selected)["submit"].describe()
        assert "user-defined preset 'org-flow'" in line
        assert "workflow.presets.org-flow.stages.submit" in line

    @pytest.mark.parametrize("name", WORKFLOW_PRESET_NAMES)
    def test_a_bundled_name_never_displays_as_user_defined(
        self, store: ConfigStore, name: str
    ) -> None:
        # Bundled names are reserved: the write path refuses a definition reusing
        # one, and the reader prefers the bundled table. Either way the display
        # must not tell an operator that engine-authored commands are theirs.
        configure(store, {"workflow": {"preset": name}})
        for row in stage_origins(DeliveryWorkflow.load(store)):
            assert row.source is not StageSource.USER_PRESET


class TestTheRenderedName:
    def test_a_name_carrying_control_characters_is_sanitized(self) -> None:
        row = StageOrigin(
            stage="submit",
            source=StageSource.USER_PRESET,
            preset="org\r\nflow\x00",
            declared_at="workflow.presets.org\nflow.stages.submit",
            commands=1,
        )
        assert row.preset == "orgflow"
        assert "\n" not in row.declared_at and "\r" not in row.declared_at

    def test_a_long_name_is_capped_so_the_document_does_not_set_the_width(self) -> None:
        row = StageOrigin(
            stage="submit",
            source=StageSource.USER_PRESET,
            preset="x" * (MAX_PRESET_NAME_CHARS * 3),
            commands=1,
        )
        assert len(row.preset) <= MAX_PRESET_NAME_CHARS + len(" […]")

    def test_a_preset_row_without_a_name_is_refused_rather_than_shown_unnamed(self) -> None:
        with pytest.raises(ValueError, match="without naming it"):
            StageOrigin(stage="submit", source=StageSource.USER_PRESET, commands=1)

    def test_an_override_row_carrying_a_preset_name_is_refused(self) -> None:
        with pytest.raises(ValueError, match="did not come from"):
            StageOrigin(
                stage="submit",
                source=StageSource.PROJECT_OVERRIDE,
                preset="org-flow",
                commands=1,
            )


class TestThePayloadASurfaceRenders:
    def test_it_carries_the_source_the_trust_flag_and_the_path(self, store: ConfigStore) -> None:
        configure(store, {"workflow": {"preset": "local-only"}})
        payload = rows(DeliveryWorkflow.load(store))["verify"].to_json_object()
        assert payload["source"] == "bundled_preset"
        assert payload["from_preset"] is True
        assert payload["bundled"] is True
        assert payload["preset"] == "local-only"
        assert payload["skipped"] is False
        assert payload["commands"] == 2
        assert payload["declared_at"]

    def test_a_skipped_stage_says_so_in_the_payload(self, store: ConfigStore) -> None:
        configure(store, {"workflow": {"preset": "local-only"}})
        payload = rows(DeliveryWorkflow.load(store))["publish"].to_json_object()
        assert payload["skipped"] is True
        assert payload["source"] == "unconfigured"
        assert payload["from_preset"] is False
        assert payload["bundled"] is False


#: Stages a generated document may declare, and the layer it declares them at.
_STAGE_KEYS = st.sampled_from(tuple(DELIVERY_STAGES))


class TestTheDisplayAgreesWithTheResolver:
    """The property that rules out a second precedence implementation."""

    @settings(max_examples=60, deadline=None)
    @given(
        preset=st.sampled_from((None,) + WORKFLOW_PRESET_NAMES + ("org-flow",)),
        project_stages=st.sets(_STAGE_KEYS, max_size=3),
        app_stages=st.sets(_STAGE_KEYS, max_size=2),
    )
    def test_every_row_reports_what_the_workflow_resolved(
        self,
        tmp_path_factory: Any,
        preset: str | None,
        project_stages: set[str],
        app_stages: set[str],
    ) -> None:
        store = ConfigStore(tmp_path_factory.mktemp("state"))
        project: dict[str, Any] = {"path": "/somewhere", "workflow": {}}
        if preset is not None:
            project["workflow"]["preset"] = preset
        if project_stages:
            project["workflow"]["stages"] = {
                stage: [["project", stage]] for stage in sorted(project_stages)
            }
        document: dict[str, Any] = {
            "workflow": {"presets": {"org-flow": ORG_PRESET}},
            "projects": {"acme": project},
        }
        if app_stages:
            document["workflow"]["stages"] = {
                stage: [["app", stage]] for stage in sorted(app_stages)
            }
        configure(store, document)
        workflow = DeliveryWorkflow.load(store, project="acme")
        selection = workflow.selected_preset()

        for row in stage_origins(workflow):
            resolved = workflow.stage(row.stage)
            assert row.skipped is (resolved is None), row.stage
            if resolved is None:
                continue
            # The resolver decides whether the preset supplied the stage; the row
            # reports it. Any disagreement here is a second precedence rule.
            assert row.source.from_preset is resolved.from_preset, row.stage
            assert row.declared_at == resolved.declared_at, row.stage
            assert row.commands == len(resolved.commands), row.stage
            if resolved.from_preset:
                assert selection is not None
                assert row.source.bundled is selection.bundled, row.stage
                assert row.preset == resolved.preset

    def test_the_rows_are_the_schemas_stages_in_order(self, store: ConfigStore) -> None:
        configure(store, {"workflow": {"preset": "local-only"}})
        resolved = stage_origins(DeliveryWorkflow.load(store))
        assert tuple(row.stage for row in resolved) == DELIVERY_STAGES
        # The flow's own stage order is a subset of the schema's, so a surface
        # listing these rows never shows a stage the pipeline cannot run.
        assert set(DELIVERY_FLOW_STAGES) <= set(DELIVERY_STAGES)
