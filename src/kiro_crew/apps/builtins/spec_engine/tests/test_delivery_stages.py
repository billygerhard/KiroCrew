"""The stage executor: skipping, refusing, and running configured commands.

These tests spawn real processes rather than asserting against a mock, because
the claim under test is precisely what happens at the process boundary: that a
value carrying shell syntax arrives at the program as one inert argument and
executes nothing. A mock would confirm the argv the engine intended and say
nothing about the thing that could go wrong.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Sequence

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from kiro_crew import platform_compat
from kiro_crew.apps.builtins.spec_engine.engine.config import (
    DASHBOARD_SURFACE,
    DELIVERY_STAGES,
    ConfigStore,
    ValueOrigin,
)
from kiro_crew.apps.builtins.spec_engine.engine.delivery import (
    ISOLATE_STAGE,
    MAX_CAPTURED_CHARS,
    TRUNCATION_NOTICE,
    ZERO_CONFIG_AUTONOMY_CEILING,
    CommandOutcome,
    CommandTemplate,
    DeliveryWorkflow,
    RunContext,
    StageExecutor,
    StageOutcome,
    cap_autonomy,
)

#: Every shell construct that would matter if a shell were involved, plus a
#: newline, in one value. Watched-item text is attacker-controlled on a public
#: tracker, so this is the shape of a real payload rather than a synthetic one.
HOSTILE_TITLE = "boom; touch pwned && touch pwned2 | tee pwned3 `touch pwned4` $(touch pwned5)\nx"

#: Marker file names the payload above would create if anything interpreted it.
PAYLOAD_ARTEFACTS = ("pwned", "pwned2", "pwned3", "pwned4", "pwned5")


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    """The run's working tree: where stage commands are executed."""
    path = tmp_path / "workspace"
    path.mkdir()
    return path


@pytest.fixture()
def store(tmp_path: Path) -> ConfigStore:
    return ConfigStore(tmp_path / "state")


@pytest.fixture()
def recorder(tmp_path: Path) -> Path:
    """A program that records the argv it was handed, and nothing else.

    It writes its arguments to the file named by its first argument, so a test
    can compare what the program received against what the engine rendered.
    """
    script = tmp_path / "recorder.py"
    script.write_text(
        "import json, sys\n"
        "with open(sys.argv[1], 'w', encoding='utf-8') as handle:\n"
        "    json.dump(sys.argv[2:], handle)\n",
        encoding="utf-8",
    )
    return script


def context(workspace: Path, **overrides: str) -> RunContext:
    values: dict[str, str] = {
        "spec_name": "example",
        "spec_type": "feature",
        "workspace_path": str(workspace),
    }
    values.update(overrides)
    return RunContext(**values)


def configure(store: ConfigStore, document: dict[str, Any]) -> None:
    store.write(document, surface=DASHBOARD_SURFACE)


def recorded_argv(target: Path) -> list[str]:
    return list(json.loads(target.read_text(encoding="utf-8")))


def _still_running(pid: int) -> bool:
    """Whether *pid* still exists, without signalling it."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class TestUnconfiguredStages:
    def test_every_stage_skips_when_nothing_is_configured(
        self, store: ConfigStore, workspace: Path
    ) -> None:
        executor = StageExecutor(store, project="acme")
        for stage in DELIVERY_STAGES:
            result = executor.run(stage, context(workspace))
            assert result.outcome is StageOutcome.SKIPPED
            assert result.ok
            assert result.commands == ()
            assert result.reason

    def test_a_configured_stage_does_not_configure_its_neighbours(
        self, store: ConfigStore, workspace: Path, recorder: Path
    ) -> None:
        target = workspace / "argv.json"
        configure(
            store,
            {"workflow": {"stages": {"verify": [[sys.executable, str(recorder), str(target)]]}}},
        )
        executor = StageExecutor(store)
        assert executor.run("verify", context(workspace)).outcome is StageOutcome.PASSED
        assert executor.run("publish", context(workspace)).outcome is StageOutcome.SKIPPED

    def test_unknown_stage_name_is_a_programming_error(
        self, store: ConfigStore, workspace: Path
    ) -> None:
        with pytest.raises(ValueError):
            StageExecutor(store).run("deploy-everything", context(workspace))


class TestSubstitutionAtTheProcessBoundary:
    def test_hostile_value_arrives_as_one_inert_argument(
        self, store: ConfigStore, workspace: Path, recorder: Path
    ) -> None:
        target = workspace / "argv.json"
        configure(
            store,
            {
                "workflow": {
                    "stages": {
                        "submit": [
                            [
                                sys.executable,
                                str(recorder),
                                str(target),
                                "--title",
                                "{review_title}",
                            ]
                        ]
                    }
                }
            },
        )
        result = StageExecutor(store).run("submit", context(workspace, review_title=HOSTILE_TITLE))
        assert result.outcome is StageOutcome.PASSED
        assert recorded_argv(target) == ["--title", HOSTILE_TITLE]
        for artefact in PAYLOAD_ARTEFACTS:
            assert not (workspace / artefact).exists()

    def test_value_embedded_in_a_larger_argument_stays_one_argument(
        self, store: ConfigStore, workspace: Path, recorder: Path
    ) -> None:
        target = workspace / "argv.json"
        configure(
            store,
            {
                "workflow": {
                    "stages": {
                        "isolate": [
                            [sys.executable, str(recorder), str(target), "refs/heads/{branch_name}"]
                        ]
                    }
                }
            },
        )
        result = StageExecutor(store).run(
            "isolate", context(workspace, branch_name="topic; rm -rf /")
        )
        assert result.outcome is StageOutcome.PASSED
        assert recorded_argv(target) == ["refs/heads/topic; rm -rf /"]

    def test_custom_project_variables_are_substituted(
        self, store: ConfigStore, workspace: Path, recorder: Path
    ) -> None:
        target = workspace / "argv.json"
        configure(
            store,
            {
                "projects": {
                    "acme": {
                        "path": str(workspace),
                        "variables": {"deploy_env": "staging"},
                        "workflow": {
                            "stages": {
                                "publish": [
                                    [sys.executable, str(recorder), str(target), "{deploy_env}"]
                                ]
                            }
                        },
                    }
                }
            },
        )
        result = StageExecutor(store, project="acme").run("publish", context(workspace))
        assert result.outcome is StageOutcome.PASSED
        assert recorded_argv(target) == ["staging"]


class TestValuelessVariables:
    def test_stage_refuses_before_any_process_starts(
        self, store: ConfigStore, workspace: Path, recorder: Path
    ) -> None:
        first = workspace / "first.json"
        second = workspace / "second.json"
        configure(
            store,
            {
                "workflow": {
                    "stages": {
                        "submit": [
                            [sys.executable, str(recorder), str(first)],
                            [sys.executable, str(recorder), str(second), "{item_url}"],
                        ]
                    }
                }
            },
        )
        spawned: list[Sequence[str]] = []

        def refuse_to_run(argv: Sequence[str], *, cwd: Path, timeout_s: int) -> CommandOutcome:
            spawned.append(argv)
            return CommandOutcome(exit_code=0)

        result = StageExecutor(store, runner=refuse_to_run).run("submit", context(workspace))
        assert result.outcome is StageOutcome.REFUSED
        assert result.missing_variables == ("item_url",)
        assert not result.executed
        # The first command of the stage was valid on its own and still did not
        # run: a stage that cannot finish must not perform half its side effects.
        assert spawned == []
        assert not first.exists()
        assert not second.exists()

    def test_blank_value_refuses_rather_than_substituting_nothing(
        self, store: ConfigStore, workspace: Path, recorder: Path
    ) -> None:
        target = workspace / "argv.json"
        configure(
            store,
            {
                "workflow": {
                    "stages": {
                        "submit": [[sys.executable, str(recorder), str(target), "{branch_name}"]]
                    }
                }
            },
        )
        result = StageExecutor(store).run("submit", context(workspace, branch_name="   "))
        assert result.outcome is StageOutcome.REFUSED
        assert result.missing_variables == ("branch_name",)
        assert not target.exists()

    def test_project_variable_shadowing_a_run_context_name_refuses_the_stage(
        self, store: ConfigStore, workspace: Path, recorder: Path
    ) -> None:
        configure(
            store,
            {
                "projects": {
                    "acme": {
                        "path": str(workspace),
                        "variables": {"branch_name": "attacker"},
                        "workflow": {"stages": {"submit": [[sys.executable, "-c", "pass"]]}},
                    }
                }
            },
        )
        result = StageExecutor(store, project="acme").run(
            "submit", context(workspace, branch_name="real")
        )
        assert result.outcome is StageOutcome.REFUSED
        assert not result.executed


class TestExecutionOutcomes:
    def test_non_zero_exit_fails_the_stage_and_stops_it(
        self, store: ConfigStore, workspace: Path, recorder: Path
    ) -> None:
        target = workspace / "argv.json"
        configure(
            store,
            {
                "workflow": {
                    "stages": {
                        "verify": [
                            [sys.executable, "-c", "import sys; sys.exit(3)"],
                            [sys.executable, str(recorder), str(target)],
                        ]
                    }
                }
            },
        )
        result = StageExecutor(store).run("verify", context(workspace))
        assert result.outcome is StageOutcome.FAILED
        assert not result.ok
        assert len(result.commands) == 1
        assert result.commands[0].exit_code == 3
        assert not target.exists()

    def test_missing_program_fails_without_raising(
        self, store: ConfigStore, workspace: Path
    ) -> None:
        configure(
            store,
            {"workflow": {"stages": {"verify": [["kirocrew-no-such-program-here", "--run"]]}}},
        )
        result = StageExecutor(store).run("verify", context(workspace))
        assert result.outcome is StageOutcome.FAILED
        assert "kirocrew-no-such-program-here" in result.commands[0].stderr

    def test_commands_run_in_the_run_workspace(self, store: ConfigStore, workspace: Path) -> None:
        configure(
            store,
            {
                "workflow": {
                    "stages": {"verify": [[sys.executable, "-c", "import os; print(os.getcwd())"]]}
                }
            },
        )
        result = StageExecutor(store).run("verify", context(workspace))
        assert result.outcome is StageOutcome.PASSED
        assert Path(result.commands[0].stdout.strip()).resolve() == workspace.resolve()

    def test_absent_workspace_refuses_before_spawning(
        self, store: ConfigStore, tmp_path: Path
    ) -> None:
        configure(store, {"workflow": {"stages": {"verify": [[sys.executable, "-c", "pass"]]}}})
        result = StageExecutor(store).run(
            "verify",
            RunContext(
                spec_name="example",
                spec_type="feature",
                workspace_path=str(tmp_path / "nowhere"),
            ),
        )
        assert result.outcome is StageOutcome.REFUSED
        assert not result.executed

    def test_timeout_kills_the_command_and_fails_the_stage(
        self, store: ConfigStore, workspace: Path
    ) -> None:
        configure(
            store,
            {
                "timeouts": {"stage_command_s": 1},
                "workflow": {
                    "stages": {"verify": [[sys.executable, "-c", "import time; time.sleep(30)"]]}
                },
            },
        )
        result = StageExecutor(store).run("verify", context(workspace))
        assert result.outcome is StageOutcome.TIMED_OUT
        assert not result.ok
        assert "timeout" in result.reason

    @pytest.mark.skipif(
        not platform_compat.IS_POSIX, reason="process groups are the POSIX mechanism"
    )
    def test_a_timed_out_stage_leaves_no_process_behind(
        self, store: ConfigStore, workspace: Path, tmp_path: Path
    ) -> None:
        """The outcome enum says the stage timed out; it cannot say the work stopped.

        Stage commands are whatever an operator configured -- a build, a push, a
        deploy -- so one that outlives its timeout is not an idle process. It can
        still be holding a worktree lock or writing to the tree the next stage is
        about to read, and the enum above would look identical.
        """
        marker = tmp_path / "pids"
        # No braces anywhere in this script: the executor substitutes {name}
        # patterns and would refuse the stage for an unresolvable variable before
        # running anything, which is the substitution guard doing its job.
        spawner = (
            "import os, subprocess, sys, time\n"
            "kid = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
            "open(" + repr(str(marker)) + ", 'w').write(str(os.getpid()) + ' ' + str(kid.pid))\n"
            "time.sleep(30)\n"
        )
        configure(
            store,
            {
                "timeouts": {"stage_command_s": 3},
                "workflow": {"stages": {"verify": [[sys.executable, "-c", spawner]]}},
            },
        )

        result = StageExecutor(store).run("verify", context(workspace))

        assert result.outcome is StageOutcome.TIMED_OUT
        assert marker.is_file(), "the stage never got far enough to start a child"
        pids = [int(entry) for entry in marker.read_text(encoding="utf-8").split()]
        deadline = time.monotonic() + 10.0
        alive = [pid for pid in pids if _still_running(pid)]
        while alive and time.monotonic() < deadline:
            time.sleep(0.05)
            alive = [pid for pid in pids if _still_running(pid)]
        assert not alive, f"stage processes survived the timeout: {alive}"

    def test_captured_output_is_capped(self, store: ConfigStore, workspace: Path) -> None:
        oversized = MAX_CAPTURED_CHARS * 2
        configure(
            store,
            {
                "workflow": {
                    "stages": {"verify": [[sys.executable, "-c", f"print('x' * {oversized})"]]}
                }
            },
        )
        result = StageExecutor(store).run("verify", context(workspace))
        assert result.outcome is StageOutcome.PASSED
        assert result.commands[0].stdout.endswith(TRUNCATION_NOTICE)
        assert len(result.commands[0].stdout) == MAX_CAPTURED_CHARS + len(TRUNCATION_NOTICE)


class TestWorkflowResolution:
    def test_project_stage_overrides_the_app_wide_one(
        self, store: ConfigStore, workspace: Path, recorder: Path
    ) -> None:
        app_target = workspace / "app.json"
        project_target = workspace / "project.json"
        configure(
            store,
            {
                "workflow": {
                    "stages": {"submit": [[sys.executable, str(recorder), str(app_target)]]}
                },
                "projects": {
                    "acme": {
                        "path": str(workspace),
                        "workflow": {
                            "stages": {
                                "submit": [[sys.executable, str(recorder), str(project_target)]]
                            }
                        },
                    }
                },
            },
        )
        result = StageExecutor(store, project="acme").run("submit", context(workspace))
        assert result.outcome is StageOutcome.PASSED
        assert result.origin is ValueOrigin.PROJECT_CONFIG
        assert project_target.exists()
        assert not app_target.exists()

    def test_app_wide_stage_applies_when_the_project_declares_none(
        self, store: ConfigStore, workspace: Path, recorder: Path
    ) -> None:
        configure(
            store,
            {
                "workflow": {
                    "stages": {
                        "submit": [[sys.executable, str(recorder), str(workspace / "argv.json")]]
                    }
                },
                "projects": {"acme": {"path": str(workspace)}},
            },
        )
        result = StageExecutor(store, project="acme").run("submit", context(workspace))
        assert result.origin is ValueOrigin.APP_CONFIG
        assert result.declared_at == "workflow.stages.submit"

    def test_unusable_stage_configuration_refuses_rather_than_raising(
        self, store: ConfigStore, workspace: Path
    ) -> None:
        # Written past the validated write path, which is the only way this
        # shape reaches disk: a hand-edited document must refuse the stage, not
        # crash the pipeline.
        store.root.mkdir(parents=True, exist_ok=True)
        store.path.write_text(
            json.dumps({"version": 1, "workflow": {"stages": {"verify": [["{tool}"]]}}}),
            encoding="utf-8",
        )
        result = StageExecutor(store).run("verify", context(workspace))
        assert result.outcome is StageOutcome.REFUSED
        assert not result.executed

    def test_variables_used_are_recorded_without_their_values(
        self, store: ConfigStore, workspace: Path, recorder: Path
    ) -> None:
        configure(
            store,
            {
                "workflow": {
                    "stages": {
                        "submit": [
                            [
                                sys.executable,
                                str(recorder),
                                str(workspace / "argv.json"),
                                "{review_title}",
                            ]
                        ]
                    }
                }
            },
        )
        result = StageExecutor(store).run("submit", context(workspace, review_title=HOSTILE_TITLE))
        assert result.variables_used == ("review_title",)
        assert HOSTILE_TITLE not in str(result.variables_used)


class TestZeroConfiguration:
    def test_the_isolate_stage_name_is_a_real_stage(self) -> None:
        assert ISOLATE_STAGE in DELIVERY_STAGES

    def test_a_project_with_no_stages_runs_in_its_own_working_tree(
        self, store: ConfigStore
    ) -> None:
        configure(store, {"projects": {"acme": {"path": "/somewhere"}}})
        workflow = DeliveryWorkflow.load(store, project="acme")
        assert not workflow.isolates

    def test_a_configured_isolate_stage_means_a_workspace_of_its_own(
        self, store: ConfigStore
    ) -> None:
        configure(store, {"workflow": {"stages": {"isolate": [["git", "worktree", "add", "w"]]}}})
        assert DeliveryWorkflow.load(store).isolates

    def test_a_project_with_no_stages_is_not_configured(self, store: ConfigStore) -> None:
        configure(store, {"projects": {"acme": {"path": "/somewhere"}}})
        workflow = DeliveryWorkflow.load(store, project="acme")
        assert not workflow.configured
        assert workflow.configured_stages() == ()

    def test_selecting_a_preset_without_stages_is_still_unconfigured(
        self, store: ConfigStore
    ) -> None:
        configure(store, {"workflow": {"preset": "local-build"}})
        assert not DeliveryWorkflow.load(store).configured

    def test_one_configured_stage_makes_the_workflow_configured(self, store: ConfigStore) -> None:
        configure(store, {"workflow": {"stages": {"verify": [["make", "test"]]}}})
        workflow = DeliveryWorkflow.load(store)
        assert workflow.configured
        assert workflow.configured_stages() == ("verify",)

    @pytest.mark.parametrize("resolved", ["authoring", "execution", "delivery", "integration"])
    def test_autonomy_is_capped_at_execution_without_a_workflow(self, resolved: str) -> None:
        capped = cap_autonomy(resolved, workflow_configured=False)
        assert capped in ("authoring", ZERO_CONFIG_AUTONOMY_CEILING)
        if resolved in ("authoring", "execution"):
            assert capped == resolved
        else:
            assert capped == ZERO_CONFIG_AUTONOMY_CEILING

    @pytest.mark.parametrize("resolved", ["authoring", "execution", "delivery", "integration"])
    def test_a_configured_workflow_leaves_the_resolved_level_alone(self, resolved: str) -> None:
        assert cap_autonomy(resolved, workflow_configured=True) == resolved

    def test_capping_never_raises_a_level(self) -> None:
        assert cap_autonomy("authoring", workflow_configured=False) == "authoring"

    def test_unknown_level_is_refused(self) -> None:
        with pytest.raises(ValueError):
            cap_autonomy("root", workflow_configured=True)


class TestCommandsSuppliedByTheCaller:
    """Quality gates are command lists no workflow stage declared.

    They run through the executor's command path rather than a second executor
    precisely so the guarantees are the same ones, and these tests assert that
    against real processes: the value is inert, the variable set is the run's,
    and a valueless reference refuses before anything spawns.
    """

    def test_caller_supplied_commands_substitute_the_same_run_context(
        self, store: ConfigStore, workspace: Path, recorder: Path
    ) -> None:
        target = workspace / "argv.json"
        commands = [
            CommandTemplate.parse(
                [sys.executable, str(recorder), str(target), "--against", "{base_branch}"]
            )
        ]

        result = StageExecutor(store).run_commands(
            "verify", context(workspace, base_branch="main"), commands
        )

        assert result.outcome is StageOutcome.PASSED
        assert recorded_argv(target) == ["--against", "main"]

    def test_a_hostile_value_is_as_inert_in_a_gate_as_in_a_stage(
        self, store: ConfigStore, workspace: Path, recorder: Path
    ) -> None:
        target = workspace / "argv.json"
        commands = [
            CommandTemplate.parse(
                [sys.executable, str(recorder), str(target), "--title", "{review_title}"]
            )
        ]

        result = StageExecutor(store).run_commands(
            "verify", context(workspace, review_title=HOSTILE_TITLE), commands
        )

        assert result.outcome is StageOutcome.PASSED
        assert recorded_argv(target) == ["--title", HOSTILE_TITLE]
        for artefact in PAYLOAD_ARTEFACTS:
            assert not (workspace / artefact).exists()

    def test_a_valueless_reference_refuses_before_any_process_starts(
        self, store: ConfigStore, workspace: Path, recorder: Path
    ) -> None:
        target = workspace / "argv.json"
        commands = [
            CommandTemplate.parse([sys.executable, str(recorder), str(target)]),
            CommandTemplate.parse([sys.executable, str(recorder), str(target), "{item_url}"]),
        ]

        result = StageExecutor(store).run_commands("verify", context(workspace), commands)

        assert result.outcome is StageOutcome.REFUSED
        assert result.missing_variables == ("item_url",)
        # The first command was runnable and still did not run: a gate that
        # performed half its checks and then refused would report a partial
        # result as a refusal.
        assert not target.exists()

    def test_the_variables_used_are_recorded_for_caller_supplied_commands(
        self, store: ConfigStore, workspace: Path, recorder: Path
    ) -> None:
        target = workspace / "argv.json"
        commands = [
            CommandTemplate.parse(
                [sys.executable, str(recorder), str(target), "{base_branch}", "{spec_name}"]
            )
        ]

        result = StageExecutor(store).run_commands(
            "verify", context(workspace, base_branch="main"), commands
        )

        assert result.variables_used == ("base_branch", "spec_name")

    def test_an_unknown_stage_name_is_a_programming_error(
        self, store: ConfigStore, workspace: Path
    ) -> None:
        commands = [CommandTemplate.parse(["true"])]

        with pytest.raises(ValueError):
            StageExecutor(store).run_commands("deploy-everything", context(workspace), commands)

    def test_no_commands_at_all_is_a_programming_error(
        self, store: ConfigStore, workspace: Path
    ) -> None:
        # An empty gate is refused by the configuration schema, so reaching the
        # executor with one is a caller bug rather than a stage outcome.
        with pytest.raises(ValueError):
            StageExecutor(store).run_commands("verify", context(workspace), [])


#: Values a public tracker can supply, biased toward the characters that would
#: matter if a shell were ever involved. Handwritten examples only cover the ones
#: somebody thought of.
_GATE_VALUES = st.text(
    alphabet=st.sampled_from(list("abc 12;|&`$()'\"{}[]<>\\\n\t*?~#!")),
    min_size=1,
    max_size=24,
).filter(lambda value: value.strip() != "")


class CapturingRunner:
    """A runner that records the argv it was handed and reports success."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, argv: Sequence[str], *, cwd: Path, timeout_s: int) -> CommandOutcome:
        self.calls.append(tuple(argv))
        return CommandOutcome(exit_code=0)


class TestCallerSuppliedCommandsUnderAnyValue:
    """Substitution safety for the command path a quality gate takes.

    The stage path has this property already, and gates reach the executor by a
    different door. Pinning it here is what keeps the two from diverging into one
    door that renders values and one that interpolates them.
    """

    @settings(max_examples=150, deadline=None)
    @given(value=_GATE_VALUES)
    def test_any_value_occupies_exactly_one_argument_of_a_gate_command(
        self, tmp_path_factory: pytest.TempPathFactory, value: str
    ) -> None:
        root = tmp_path_factory.mktemp("gate-values")
        workspace = root / "workspace"
        workspace.mkdir()
        store = ConfigStore(root / "state")
        runner = CapturingRunner()
        commands = [CommandTemplate.parse(["check", "--title", "{review_title}", "--tail"])]

        result = StageExecutor(store, runner=runner).run_commands(
            "verify", context(workspace, review_title=value), commands
        )

        assert result.outcome is StageOutcome.PASSED
        assert runner.calls == [("check", "--title", value, "--tail")]

    @settings(max_examples=25, deadline=None)
    @given(blank=st.sampled_from(["", " ", "\t", "\n", "   "]))
    def test_a_blank_value_spawns_nothing_from_a_gate_command(
        self, tmp_path_factory: pytest.TempPathFactory, blank: str
    ) -> None:
        root = tmp_path_factory.mktemp("gate-blank")
        workspace = root / "workspace"
        workspace.mkdir()
        store = ConfigStore(root / "state")
        runner = CapturingRunner()
        commands = [CommandTemplate.parse(["check", "--against", "{base_branch}"])]

        result = StageExecutor(store, runner=runner).run_commands(
            "verify", context(workspace, base_branch=blank), commands
        )

        assert result.outcome is StageOutcome.REFUSED
        assert result.missing_variables == ("base_branch",)
        assert runner.calls == []
