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
from typing import Mapping, Sequence

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from kiro_crew.apps.builtins.spec_engine.engine.config import DASHBOARD_SURFACE, ConfigStore
from kiro_crew.apps.builtins.spec_engine.engine.delivery import (
    ArgumentTemplate,
    CommandOutcome,
    CommandTemplate,
    MissingVariableError,
    RunContext,
    StageExecutor,
    StageOutcome,
    TemplateError,
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
    # A value that looks like a second argument. Splitting on whitespace turns
    # this into one, which is the difference between a title and a flag.
    " --force",
    "-o pwned",
    # Characters that only become dangerous after some later layer normalises
    # them. NFKC maps the fullwidth forms onto the ASCII metacharacters above,
    # and the ligature onto two letters, so a length or membership check run
    # before normalisation and an execve run after it disagree.
    "\uff04(touch pwned)",
    "\uff1b touch pwned",
    "\ufb01",
    # Bidirectional and zero-width marks: invisible in a review, present in argv.
    "\u202e",
    "\u200b",
    # NUL. execve cannot carry it, so the only safe outcomes are a refusal or a
    # verbatim string that never reaches a spawn -- never a truncated argument.
    "\x00",
)

_VARIABLE_NAMES = st.sampled_from(
    ["spec_name", "spec_type", "base_branch", "branch_name", "item_id", "review_title"]
)

_HOSTILE_VALUES = st.lists(st.sampled_from(_HOSTILE_PIECES), min_size=1, max_size=4).map(
    lambda pieces: "payload" + "".join(pieces)
)

#: The same alphabet minus NUL, for the property that actually spawns. execve
#: cannot carry a NUL in any argument, so a value holding one is not a case about
#: shell interpretation -- it is covered on its own below.
_SPAWNABLE_VALUES = st.lists(
    st.sampled_from(tuple(piece for piece in _HOSTILE_PIECES if "\x00" not in piece)),
    min_size=1,
    max_size=4,
).map(lambda pieces: "payload" + "".join(pieces))

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
    # A value spelled like a reference is not expanded, so no element can hold
    # another variable's value by accident. Stated as the whole element rather
    # than as a character ban: the renderer's contract is that a value is copied
    # verbatim, so no character is forbidden here -- what is forbidden is a
    # second element, and that is the length assertion above.
    for index, source in enumerate(argv_template):
        if ArgumentTemplate.parse(source).is_literal:
            assert argv[index] == source


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
@given(value=_SPAWNABLE_VALUES)
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


# --- Exact argv, and the template grammar's own edges ----------------------
#
# The properties above assert that a value lands inside the right element. That
# is necessary and not sufficient: an implementation that appended the value
# twice, or that expanded a value which itself spells a reference, satisfies
# containment. What the security claim actually needs is the resulting ARGV --
# a value is allowed to contain a semicolon, it is just not allowed to become a
# second command -- so the expectation here is computed by an independent
# substitution written from the documented grammar rather than by calling the
# parser under test.


def _substitute(source: str, values: Mapping[str, str]) -> str:
    """One non-recursive substitution pass over *source*, per the documented grammar.

    ``{{`` and ``}}`` are literal braces, ``{name}`` is a reference, and a value
    is copied in without being rescanned. Written out longhand rather than
    delegating to :class:`ArgumentTemplate`, because a shadow model that called
    the code under test would agree with it by construction.
    """
    out: list[str] = []
    index = 0
    while index < len(source):
        char = source[index]
        if source.startswith("{{", index) or source.startswith("}}", index):
            out.append(char)
            index += 2
            continue
        if char == "{":
            close = source.index("}", index + 1)
            out.append(values[source[index + 1 : close]])
            index = close + 1
            continue
        out.append(char)
        index += 1
    return "".join(out)


@settings(max_examples=MAX_EXAMPLES, deadline=None)
@given(template=_templates(), values=st.data())
def test_the_rendered_argv_is_exactly_the_expected_substitution(
    template: tuple[list[str], list[str]], values: st.DataObject
) -> None:
    argv_template, referenced = template
    assigned = {name: values.draw(_HOSTILE_VALUES) for name in referenced}

    argv = CommandTemplate.parse(argv_template).render(assigned)

    # Element for element, the whole command. No argument added, none removed,
    # none split, and no value expanded twice or re-expanded.
    assert list(argv) == [_substitute(source, assigned) for source in argv_template]
    # The program is whatever the operator wrote, never anything a value chose.
    assert argv[0] == argv_template[0]


@settings(max_examples=MAX_EXAMPLES, deadline=None)
@given(
    name=_VARIABLE_NAMES,
    other=_VARIABLE_NAMES,
    tail=st.sampled_from(["", "; touch pwned", "}", "{"]),
)
def test_a_value_spelling_a_reference_is_never_expanded(name: str, other: str, tail: str) -> None:
    """Substitution happens once. A value naming a variable stays text.

    The values reaching this module come from item text a stranger wrote, so a
    second pass over the result would let that text name the run's own branch or
    workspace path and have it filled in.
    """
    assume(name != other)
    values = {name: "{" + other + "}" + tail, other: "SECRET"}

    argv = CommandTemplate.parse(["recorder", f"{{{name}}}"]).render(values)

    assert len(argv) == 2
    assert argv[1] == "{" + other + "}" + tail
    assert "SECRET" not in argv[1]


@settings(max_examples=MAX_EXAMPLES, deadline=None)
@given(
    broken=st.sampled_from(
        [
            "{unterminated",
            "prefix/{unterminated",
            "}",
            "trailing}",
            "{spec name}",
            "{spec-name}",
            "{1nvalid}",
            "{}",
            "{ }",
        ]
    )
)
def test_a_malformed_template_is_refused_rather_than_taken_literally(broken: str) -> None:
    """An unparseable argument raises, and never becomes a literal.

    Treating ``{unterminated`` as text would put an operator's typo on a command
    line as data; treating ``{spec-name}`` as text would silently drop a
    reference the operator meant, and the command would run one argument short.
    """
    with pytest.raises(TemplateError):
        CommandTemplate.parse(["recorder", broken])


@settings(max_examples=MAX_EXAMPLES, deadline=None)
@given(value=_HOSTILE_VALUES, name=_VARIABLE_NAMES)
def test_no_hostile_value_can_add_an_argument_or_name_the_program(value: str, name: str) -> None:
    """The two things a value must never do, whatever it contains."""
    argv = CommandTemplate.parse(["git", "commit", "-m", f"{{{name}}}"]).render({name: value})

    # Exactly four arguments: the value is one of them, entire.
    assert len(argv) == 4
    assert argv[:3] == ("git", "commit", "-m")
    assert argv[3] == value
    # A reference in the program position is refused outright, so no value is
    # ever consulted to decide what runs.
    with pytest.raises(TemplateError):
        CommandTemplate.parse([f"{{{name}}}", "commit"])


@settings(max_examples=MAX_SPAWNING_EXAMPLES, deadline=None)
@given(
    prefix=st.sampled_from(["payload", "", "; touch pwned"]),
    suffix=st.sampled_from(["", "tail", "$(touch pwned)"]),
)
def test_a_value_carrying_a_nul_never_runs_and_never_reports_success(
    tmp_path_factory: pytest.TempPathFactory, prefix: str, suffix: str
) -> None:
    """A NUL in item text ends the stage instead of truncating an argument.

    ``execve`` cannot carry a NUL, so this is the one hostile character that
    cannot reach the program at all. The outcomes that are safe are a refusal or
    a failure; the outcome that would be a defect is a silently truncated
    argument, because the surviving prefix is a different command than the
    operator configured and the stage would report success for running it.
    """
    root = tmp_path_factory.mktemp("nul")
    workspace = root / "workspace"
    workspace.mkdir()
    marker = workspace / "ran.txt"
    recorder = root / "recorder.py"
    recorder.write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "Path(sys.argv[1]).write_text(repr(sys.argv[2:]), encoding='utf-8')\n",
        encoding="utf-8",
    )
    store = ConfigStore(root / "state")
    store.write(
        {
            "workflow": {
                "stages": {
                    "submit": [[sys.executable, str(recorder), str(marker), "{review_title}"]]
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
            review_title=f"{prefix}\x00{suffix}",
        ),
    )

    assert result.outcome in (StageOutcome.FAILED, StageOutcome.REFUSED)
    # Nothing ran, so nothing in the payload could have run either.
    assert not marker.exists()
    assert sorted(path.name for path in workspace.iterdir()) == []
