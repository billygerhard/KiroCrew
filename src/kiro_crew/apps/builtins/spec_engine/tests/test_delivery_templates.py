"""Command template parsing and argv substitution.

Every test here is about one claim: a variable's value is data. It becomes one
argv element, it is never re-read as template syntax, and a variable with no
value stops the command instead of shrinking it.
"""

from __future__ import annotations

import pytest

from kiro_crew.apps.builtins.spec_engine.engine.delivery import (
    RUN_CONTEXT_VARIABLES,
    ArgumentTemplate,
    CommandTemplate,
    MissingVariableError,
    RunContext,
    TemplateError,
    VariableError,
    VariableRef,
    build_variables,
    has_value,
)

#: A value carrying every shell construct that would matter if any shell were
#: involved: command separators, a pipe, a background operator, backticks,
#: substitution, a redirect, a quote, and a newline.
HOSTILE_VALUE = "x; touch pwned | cat & `id` $(whoami) > out 'q' \"q\"\nsecond"


class TestParsing:
    def test_literal_argument_has_no_variables(self) -> None:
        template = ArgumentTemplate.parse("--all")
        assert template.variables == ()
        assert template.is_literal

    def test_variable_and_surrounding_literals_are_separate_segments(self) -> None:
        template = ArgumentTemplate.parse("origin/{base_branch}")
        assert template.segments == ("origin/", VariableRef("base_branch"))
        assert template.variables == ("base_branch",)

    def test_repeated_reference_is_listed_once_in_order(self) -> None:
        command = CommandTemplate.parse(["git", "{branch_name}", "{base_branch}", "{branch_name}"])
        assert command.variables == ("branch_name", "base_branch")

    def test_doubled_braces_render_as_literal_braces(self) -> None:
        command = CommandTemplate.parse(["fmt", "{{literal}}"])
        assert command.render({}) == ("fmt", "{literal}")

    @pytest.mark.parametrize(
        "source",
        [
            "{unterminated",
            "}stray",
            "{}",
            "{1bad}",
            "{has space}",
            "{dot.ted}",
        ],
    )
    def test_malformed_reference_is_refused_at_parse_time(self, source: str) -> None:
        with pytest.raises(TemplateError):
            ArgumentTemplate.parse(source)

    def test_empty_command_is_refused(self) -> None:
        with pytest.raises(TemplateError):
            CommandTemplate.parse([])

    def test_command_must_be_a_list_not_a_string(self) -> None:
        with pytest.raises(TemplateError):
            CommandTemplate.parse("git push origin main")  # type: ignore[arg-type]

    def test_program_position_may_not_be_substituted(self) -> None:
        with pytest.raises(TemplateError) as caught:
            CommandTemplate.parse(["{tool}", "--version"])
        assert "literally" in str(caught.value)

    def test_program_position_accepts_a_literal_path(self) -> None:
        command = CommandTemplate.parse(["/usr/bin/env", "true"])
        assert command.program == "/usr/bin/env"


class TestSubstitution:
    def test_value_with_shell_metacharacters_stays_one_element(self) -> None:
        command = CommandTemplate.parse(["printer", "--title", "{review_title}"])
        argv = command.render({"review_title": HOSTILE_VALUE})
        assert argv == ("printer", "--title", HOSTILE_VALUE)
        assert len(argv) == 3

    def test_value_with_spaces_is_not_split(self) -> None:
        command = CommandTemplate.parse(["printer", "{review_title}"])
        assert command.render({"review_title": "two words here"}) == (
            "printer",
            "two words here",
        )

    def test_value_that_looks_like_a_reference_is_not_expanded(self) -> None:
        # The template is parsed once, so rendering never re-reads its own
        # output: a value spelled like a reference stays text.
        command = CommandTemplate.parse(["printer", "{review_title}"])
        argv = command.render({"review_title": "{branch_name}", "branch_name": "secret"})
        assert argv == ("printer", "{branch_name}")

    def test_several_variables_in_one_element_still_produce_one_element(self) -> None:
        command = CommandTemplate.parse(["git", "{base_branch}..{branch_name}"])
        argv = command.render({"base_branch": "main", "branch_name": "topic"})
        assert argv == ("git", "main..topic")


class TestValuelessVariables:
    def test_absent_variable_refuses_to_render(self) -> None:
        command = CommandTemplate.parse(["git", "push", "origin", "{branch_name}"])
        with pytest.raises(MissingVariableError) as caught:
            command.render({})
        assert caught.value.variables == ("branch_name",)

    @pytest.mark.parametrize("blank", ["", "   ", "\n", "\t"])
    def test_blank_value_counts_as_no_value(self, blank: str) -> None:
        command = CommandTemplate.parse(["git", "push", "origin", "{branch_name}"])
        with pytest.raises(MissingVariableError):
            command.render({"branch_name": blank})

    def test_every_missing_variable_is_reported_at_once(self) -> None:
        command = CommandTemplate.parse(["deploy", "{item_id}", "{branch_name}"])
        assert command.missing({}) == ("item_id", "branch_name")

    def test_has_value_rejects_a_non_string(self) -> None:
        assert not has_value({"n": 3}, "n")  # type: ignore[dict-item]
        assert has_value({"n": "3"}, "n")


class TestVariableAssembly:
    def test_run_context_omits_the_fields_it_has_no_value_for(self) -> None:
        context = RunContext(spec_name="s", spec_type="feature", workspace_path="/tmp/w")
        values = context.to_variables()
        assert values == {"spec_name": "s", "spec_type": "feature", "workspace_path": "/tmp/w"}
        assert "item_id" not in values

    def test_custom_variables_join_the_run_context(self) -> None:
        context = RunContext(spec_name="s", spec_type="feature", workspace_path="/w")
        values = build_variables(context, {"deploy_env": "staging"})
        assert values["deploy_env"] == "staging"
        assert values["spec_name"] == "s"

    def test_custom_variable_may_not_shadow_a_run_context_name(self) -> None:
        context = RunContext(spec_name="s", spec_type="feature", workspace_path="/w")
        with pytest.raises(VariableError):
            build_variables(context, {"branch_name": "attacker-branch"})

    def test_custom_variable_name_must_be_referenceable(self) -> None:
        context = RunContext(spec_name="s", spec_type="feature", workspace_path="/w")
        with pytest.raises(VariableError):
            build_variables(context, {"not a name": "value"})

    def test_blank_custom_value_is_dropped_so_it_reads_as_valueless(self) -> None:
        context = RunContext(spec_name="s", spec_type="feature", workspace_path="/w")
        values = build_variables(context, {"deploy_env": "  "})
        assert "deploy_env" not in values

    def test_every_run_context_name_is_a_declared_field(self) -> None:
        context = RunContext(
            spec_name="s",
            spec_type="feature",
            workspace_path="/w",
            base_branch="main",
            branch_name="topic",
            item_id="7",
            item_url="https://example.invalid/7",
            review_title="t",
            review_summary="y",
        )
        assert tuple(context.to_variables()) == RUN_CONTEXT_VARIABLES
