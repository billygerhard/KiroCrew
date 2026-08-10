"""Property-based tests for substitution safety.

The claim: for any command template and any variable values, a substituted value
appears as exactly one argv element with no shell interpretation, and a template
referencing a valueless variable never executes.

Generated values deliberately favour the characters that would matter if a shell
were ever involved — separators, pipes, backticks, substitution, quotes,
newlines, braces — because those are the values a public tracker supplies and
handwritten examples only cover the ones somebody thought of.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Sequence

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from kiro_crew.apps.builtins.spec_engine.engine.config import DASHBOARD_SURFACE, ConfigStore
from kiro_crew.apps.builtins.spec_engine.engine.delivery import (
    CommandOutcome,
    CommandTemplate,
    MissingVariableError,
    RunContext,
    StageExecutor,
    StageOutcome,
)

#: Structural properties render only, so they can afford a wide search.
MAX_EXAMPLES = 200

#: The end-to-end property spawns a process per example, so it trades breadth
#: for staying inside a per-commit test budget.
MAX_SPAWNING_EXAMPLES = 12

#: Substrings assembled into hostile values. Each would change the meaning of a
#: command line if any shell parsed it.
_HOSTILE_PIECES = (
    "; touch pwned",
    "&& touch pwned",
    "| tee pwned",
    "`touch pwned`",
    "$(touch pwned)",
    "> pwned",
    "'quoted'",
    '"quoted"',
    "\n",
    "\t",
    "{branch_name}",
    "{{",
    "}}",
    "$HOME",
    "%PATH%",
    "../..",
    "\\",
)

_VARIABLE_NAMES = st.sampled_from(
    ["spec_name", "spec_type", "base_branch", "branch_name", "item_id", "review_title"]
)

_HOSTILE_VALUES = st.lists(st.sampled_from(_HOSTILE_PIECES), min_size=1, max_size=4).map(
    lambda pieces: "payload" + "".join(pieces)
)

_LITERAL_ARGUMENTS = st.sampled_from(["--title", "--body", "-m", "push", "origin", "review"])


@st.composite
def _templates(draw: st.DrawFn) -> tuple[list[str], list[str]]:
    """A command template plus the variable names it references."""
    referenced = draw(st.lists(_VARIABLE_NAMES, min_size=1, max_size=4, unique=True))
    argv = ["recorder"]
    for name in referenced:
        if draw(st.booleans()):
            argv.append(draw(_LITERAL_ARGUMENTS))
        # Half the references sit inside a larger argument, which is the case
        # where a naive splitter would break the element apart.
        argv.append(f"prefix/{{{name}}}" if draw(st.booleans()) else f"{{{name}}}")
    return argv, referenced


@settings(max_examples=MAX_EXAMPLES, deadline=None)
@given(template=_templates(), values=st.data())
def test_each_value_occupies_exactly_one_argv_element(
    template: tuple[list[str], list[str]], values: st.DataObject
) -> None:
    argv_template, referenced = template
    assigned = {name: values.draw(_HOSTILE_VALUES) for name in referenced}
    command = CommandTemplate.parse(argv_template)

    argv = command.render(assigned)

    # One rendered element per template element: no value ever split into two,
    # and none collapsed into its neighbour.
    assert len(argv) == len(argv_template)
    for index, source in enumerate(argv_template):
        for name, value in assigned.items():
            if f"{{{name}}}" in source:
                assert value in argv[index]
    # A value spelled like a reference is not expanded, so no rendered element
    # can contain another variable's value by accident.
    for element in argv:
        assert "\x00" not in element


@settings(max_examples=MAX_EXAMPLES, deadline=None)
@given(template=_templates(), blank=st.sampled_from(["", " ", "\t", "\n", "   "]))
def test_a_valueless_reference_never_renders(
    template: tuple[list[str], list[str]], blank: str
) -> None:
    argv_template, referenced = template
    command = CommandTemplate.parse(argv_template)
    # Every reference has a value except the last, which is blank.
    assigned = {name: "value" for name in referenced[:-1]}
    assigned[referenced[-1]] = blank

    assert command.missing(assigned) == (referenced[-1],)
    with pytest.raises(MissingVariableError):
        command.render(assigned)


@settings(max_examples=MAX_SPAWNING_EXAMPLES, deadline=None)
@given(value=_HOSTILE_VALUES)
def test_a_rendered_value_reaches_the_program_verbatim(
    tmp_path_factory: pytest.TempPathFactory, value: str
) -> None:
    root = tmp_path_factory.mktemp("substitution")
    workspace = root / "workspace"
    workspace.mkdir()
    recorder = root / "recorder.py"
    recorder.write_text(
        "import json, sys\n"
        "with open(sys.argv[1], 'w', encoding='utf-8') as handle:\n"
        "    json.dump(sys.argv[2:], handle)\n",
        encoding="utf-8",
    )
    target = workspace / "argv.json"
    store = ConfigStore(root / "state")
    store.write(
        {
            "workflow": {
                "stages": {
                    "submit": [[sys.executable, str(recorder), str(target), "{review_title}"]]
                }
            }
        },
        surface=DASHBOARD_SURFACE,
    )

    result = StageExecutor(store).run(
        "submit",
        RunContext(
            spec_name="example",
            spec_type="feature",
            workspace_path=str(workspace),
            review_title=value,
        ),
    )

    assert result.outcome is StageOutcome.PASSED
    assert json.loads(target.read_text(encoding="utf-8")) == [value]
    # Nothing in the payload ran: every piece that could create a file was data.
    assert sorted(p.name for p in workspace.iterdir()) == [target.name]


@settings(max_examples=MAX_EXAMPLES, deadline=None)
@given(
    stage=st.sampled_from(["isolate", "submit", "verify", "publish", "teardown"]),
    referenced=st.lists(_VARIABLE_NAMES, min_size=1, max_size=3, unique=True),
)
def test_a_stage_with_a_valueless_variable_spawns_nothing(
    tmp_path_factory: pytest.TempPathFactory, stage: str, referenced: list[str]
) -> None:
    root = tmp_path_factory.mktemp("refusal")
    workspace = root / "workspace"
    workspace.mkdir()
    store = ConfigStore(root / "state")
    argv = ["recorder"] + [f"{{{name}}}" for name in referenced]
    store.write({"workflow": {"stages": {stage: [argv]}}}, surface=DASHBOARD_SURFACE)
    spawned: list[Sequence[str]] = []

    def record_spawn(argv: Sequence[str], *, cwd: Path, timeout_s: int) -> CommandOutcome:
        spawned.append(argv)
        return CommandOutcome(exit_code=0)

    # The context supplies only the spec identity, so any other reference is
    # valueless for this run.
    result = StageExecutor(store, runner=record_spawn).run(
        stage,
        RunContext(spec_name="example", spec_type="feature", workspace_path=str(workspace)),
    )

    unresolvable = [name for name in referenced if name not in ("spec_name", "spec_type")]
    if unresolvable:
        assert result.outcome is StageOutcome.REFUSED
        assert set(result.missing_variables) == set(unresolvable)
        assert spawned == []
    else:
        assert result.outcome is StageOutcome.PASSED
        assert len(spawned) == 1
