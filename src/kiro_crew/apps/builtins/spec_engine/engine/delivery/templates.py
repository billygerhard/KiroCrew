"""Command templates: tokenized once, substituted into argv elements.

This module is the reason the delivery pipeline can run operator-configured
commands on text an anonymous stranger wrote. Two properties carry that weight.

**A value becomes exactly one argv element.** A template is a list of argument
templates, each parsed here into literal text and ``{variable}`` references. A
rendered argument is one string handed to ``subprocess`` as one element of an
argv list, so a value containing ``;``, ``|``, backticks, ``$(...)``, a quote,
or a newline is inert data rather than syntax. There is no shell in the path and
no command string to be parsed, which is what makes the inertness structural
rather than a matter of escaping the characters someone thought of.

**Parsing happens once, before any value exists.** Rendering walks the parsed
segments and copies values in; it never re-scans its own output. A value that
itself looks like ``{branch_name}`` therefore stays six literal characters
instead of expanding, which is the difference between substitution and an
evaluator.

Two smaller rules fall out of the same reasoning:

* A referenced variable with no value raises rather than rendering empty.
  ``git push origin {branch_name}`` with an empty branch is not the same command
  with a piece missing, it is a different command, and the caller cannot see the
  difference in an exit code.
* The program (``argv[0]``) must be literal. Every other position is data being
  handed to a program the operator named; the program position decides which
  program runs at all, and some variable values arrive from a public tracker.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence, Union

#: A variable name: an identifier, so a reference is unambiguous to read and a
#: typo is a parse error rather than a variable that silently never resolves.
VARIABLE_NAME_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

#: Doubled braces stand for a literal brace, so a command that needs one (a
#: format string handed to another program) does not have to be unwriteable.
_OPEN_ESCAPE = "{{"
_CLOSE_ESCAPE = "}}"


class TemplateError(ValueError):
    """Raised when a command template cannot be parsed.

    A malformed template is a configuration error, and it is caught here at
    parse time rather than at render time: the whole point of parsing once is
    that no template surprises the pipeline after values are in hand.
    """


class MissingVariableError(ValueError):
    """Raised when a template references variables that have no value."""

    def __init__(self, variables: Iterable[str]) -> None:
        self.variables: tuple[str, ...] = tuple(variables)
        listed = ", ".join(self.variables)
        super().__init__(f"no value for referenced variable(s): {listed}")


@dataclass(frozen=True)
class VariableRef:
    """A ``{name}`` reference inside one argument template."""

    name: str


#: One piece of a parsed argument: literal text, or a variable reference.
Segment = Union[str, VariableRef]


@dataclass(frozen=True)
class ArgumentTemplate:
    """One argv element, already parsed into literal text and references."""

    source: str
    segments: tuple[Segment, ...]

    @classmethod
    def parse(cls, source: str) -> "ArgumentTemplate":
        """Parse *source* into segments, raising ``TemplateError`` when malformed."""
        if not isinstance(source, str) or not source:
            raise TemplateError("an argument template must be a non-empty string")
        return cls(source=source, segments=_parse_segments(source))

    @property
    def variables(self) -> tuple[str, ...]:
        """Referenced variable names, in first-appearance order without repeats."""
        return _ordered_unique(
            segment.name for segment in self.segments if isinstance(segment, VariableRef)
        )

    @property
    def is_literal(self) -> bool:
        """Whether this argument references no variables at all."""
        return not any(isinstance(segment, VariableRef) for segment in self.segments)

    def render(self, values: Mapping[str, str]) -> str:
        """Return this argument as one string, with values copied in verbatim.

        Values are never re-parsed, so a value containing braces, shell
        metacharacters, or newlines contributes those characters and nothing
        else. Raises ``MissingVariableError`` when a referenced variable has no
        value, so no caller can render a command with a hole in it.
        """
        missing = self.missing(values)
        if missing:
            raise MissingVariableError(missing)
        parts: list[str] = []
        for segment in self.segments:
            if isinstance(segment, VariableRef):
                parts.append(values[segment.name])
            else:
                parts.append(segment)
        return "".join(parts)

    def missing(self, values: Mapping[str, str]) -> tuple[str, ...]:
        """Referenced variables that have no value in *values*."""
        return _ordered_unique(name for name in self.variables if not has_value(values, name))


@dataclass(frozen=True)
class CommandTemplate:
    """One command: an argv list of argument templates, parsed once."""

    arguments: tuple[ArgumentTemplate, ...]

    @classmethod
    def parse(cls, argv: Sequence[str]) -> "CommandTemplate":
        """Parse an argv template list.

        Refuses a variable reference in the program position. Everything after
        it is data passed to a program the operator chose; the program itself
        decides what runs, and some values reaching this module were authored by
        whoever opened an issue.
        """
        if isinstance(argv, (str, bytes)) or not isinstance(argv, Sequence):
            raise TemplateError("a command must be a list of arguments")
        if not argv:
            raise TemplateError("a command must have at least one argument")
        arguments = tuple(ArgumentTemplate.parse(item) for item in argv)
        if not arguments[0].is_literal:
            raise TemplateError(
                "the program to run must be written literally, "
                f"not substituted from a variable: {arguments[0].source!r}"
            )
        return cls(arguments=arguments)

    @property
    def source(self) -> tuple[str, ...]:
        """The template as configured, for reporting."""
        return tuple(argument.source for argument in self.arguments)

    @property
    def program(self) -> str:
        """The literal program name or path this command runs."""
        return self.arguments[0].source

    @property
    def variables(self) -> tuple[str, ...]:
        """Every referenced variable, in first-appearance order without repeats."""
        return _ordered_unique(name for argument in self.arguments for name in argument.variables)

    def missing(self, values: Mapping[str, str]) -> tuple[str, ...]:
        """Referenced variables that have no value in *values*."""
        return _ordered_unique(name for name in self.variables if not has_value(values, name))

    def render(self, values: Mapping[str, str]) -> tuple[str, ...]:
        """Return the argv list to execute.

        Raises ``MissingVariableError`` naming every valueless variable at once,
        so an operator fixes one report instead of rediscovering the next
        missing variable on the next run.
        """
        missing = self.missing(values)
        if missing:
            raise MissingVariableError(missing)
        return tuple(argument.render(values) for argument in self.arguments)


def has_value(values: Mapping[str, str], name: str) -> bool:
    """Whether *name* holds a usable value.

    Blank counts as absent. An empty branch name or review title is not a
    shorter version of the command, it is a different command: the argument
    either disappears or lands as an empty string the program reads as
    something else. Refusing both cases keeps the failure at configuration
    level, where it names the variable, instead of inside a program's argument
    parser.
    """
    raw = values.get(name)
    return isinstance(raw, str) and bool(raw.strip())


def _parse_segments(source: str) -> tuple[Segment, ...]:
    segments: list[Segment] = []
    literal: list[str] = []
    index = 0
    length = len(source)
    while index < length:
        char = source[index]
        if char == "{":
            if source.startswith(_OPEN_ESCAPE, index):
                literal.append("{")
                index += 2
                continue
            close = source.find("}", index + 1)
            if close == -1:
                raise TemplateError(f"unterminated variable reference in {source!r}")
            name = source[index + 1 : close]
            if not VARIABLE_NAME_PATTERN.fullmatch(name):
                raise TemplateError(f"{name!r} is not a valid variable name in {source!r}")
            if literal:
                segments.append("".join(literal))
                literal.clear()
            segments.append(VariableRef(name))
            index = close + 1
            continue
        if char == "}":
            if source.startswith(_CLOSE_ESCAPE, index):
                literal.append("}")
                index += 2
                continue
            raise TemplateError(f"unmatched '}}' in {source!r}; write '}}}}' for a literal brace")
        literal.append(char)
        index += 1
    if literal:
        segments.append("".join(literal))
    return tuple(segments)


def _ordered_unique(names: Iterable[str]) -> tuple[str, ...]:
    seen: dict[str, None] = {}
    for name in names:
        seen.setdefault(name, None)
    return tuple(seen)
