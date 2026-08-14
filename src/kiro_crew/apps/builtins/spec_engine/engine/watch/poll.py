"""Polling a watch source: run its command, read its output, or say why not.

This module exists to make one failure impossible to express: **a source that
cannot be polled never reports an empty backlog.** A watcher whose program is
absent and a tracker with nothing open both produce no items, and if the engine
reported them the same way the difference would be invisible for as long as
nobody happened to look. Silence is indistinguishable from health, so the only
safe design is one where "nothing to do" is a claim only a successful poll can
make.

Three things enforce that here rather than leaving it to callers:

* :class:`PollOutcome` refuses to be constructed unhealthy without a reason, and
  refuses to carry items alongside one.
* :attr:`PollOutcome.found_no_items` — the question a dispatcher actually asks —
  is true only after a poll that ran and parsed. An unhealthy source answers no.
* A command that exits zero and prints nothing is unhealthy, not empty. A
  program that printed nothing did not report an empty list; it reported
  nothing, and the two differ in exactly the way this module cares about.

The output is untrusted. It is JSON-decoded (which evaluates nothing), walked by
fixed paths, and stored as text. No part of it is executed, expanded, or
interpreted, and the poll command itself runs as an argv list with no shell.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Sequence

from ..config import ConfigStore, ConfigValidationError
from ..config.schema import SECTION_PROJECTS
from ..delivery.stages import (
    TRUNCATION_NOTICE,
    CommandOutcome,
    CommandRunner,
    run_argv,
)
from ..delivery.templates import MissingVariableError
from .items import REQUIRED_ITEM_FIELDS, WatchedItem
from .sources import WatchSource, poll_timeout_s, source_names

logger = logging.getLogger(__name__)

#: Rejections and mapping problems kept per poll. Enough for an operator to
#: recognize the pattern, bounded so a whole tracker's worth of mismatched items
#: cannot be held in one outcome.
MAX_RECORDED_PROBLEMS = 10


class PollStatus(str, Enum):
    """How a poll ended."""

    #: The command ran, its output parsed, and its items were mapped. Only this
    #: status permits any statement about how many items the source has.
    OK = "ok"
    #: The source is defined but not enabled, so nothing ran.
    DISABLED = "disabled"
    #: The source could not be polled. Carries a reason and never carries items.
    UNHEALTHY = "unhealthy"


class HealthReason(str, Enum):
    """Why a source could not be polled."""

    #: The source's definition is missing, malformed, or references a variable
    #: with no value. Nothing was spawned.
    CONFIG_INVALID = "config_invalid"
    #: The program the poll command names could not be found or could not be
    #: started. This is the reason that must never look like an empty backlog.
    PROGRAM_UNAVAILABLE = "program_unavailable"
    #: The command ran and exited non-zero.
    COMMAND_FAILED = "command_failed"
    #: The command exceeded its timeout and was killed.
    TIMED_OUT = "timed_out"
    #: Output hit the capture ceiling, so what arrived is a prefix of the real
    #: answer. Reported rather than parsed: a truncated list parses as fewer
    #: items, which is the silent-undercount version of the same bug.
    OUTPUT_TRUNCATED = "output_truncated"
    #: Output was absent, not JSON, or not a list of objects.
    UNREADABLE_OUTPUT = "unreadable_output"
    #: Items arrived and not one of them yielded the required fields, so the
    #: mapping does not fit this source's output.
    FIELD_MAP_MISMATCH = "field_map_mismatch"


@dataclass(frozen=True)
class RejectedItem:
    """One output entry that could not become a watched item, and why."""

    index: int
    reason: str


@dataclass(frozen=True)
class PollOutcome:
    """What one poll of one source produced.

    The invariants in ``__post_init__`` are the point of the type: an unhealthy
    outcome cannot be built without a reason and a human-readable detail, cannot
    carry items, and cannot blame an unavailable program without naming it.
    """

    source: str
    status: PollStatus
    items: tuple[WatchedItem, ...] = ()
    reason: HealthReason | None = None
    detail: str = ""
    program: str = ""
    exit_code: int | None = None
    rejected: tuple[RejectedItem, ...] = ()
    field_problems: tuple[str, ...] = ()
    duration_s: float = 0.0

    def __post_init__(self) -> None:
        if self.status is PollStatus.UNHEALTHY:
            if self.reason is None:
                raise ValueError("an unhealthy poll outcome must carry a reason")
            if not self.detail.strip():
                raise ValueError("an unhealthy poll outcome must explain itself")
            if self.items:
                raise ValueError("an unhealthy poll outcome must not report items")
            if self.reason is HealthReason.PROGRAM_UNAVAILABLE and not self.program.strip():
                raise ValueError("an unavailable program must be named")
        elif self.reason is not None:
            raise ValueError("only an unhealthy poll outcome carries a reason")

    @property
    def healthy(self) -> bool:
        """Whether this poll produced a usable answer about the source."""
        return self.status is PollStatus.OK

    @property
    def found_no_items(self) -> bool:
        """Whether the source genuinely has nothing waiting.

        The question a dispatcher asks, answerable only by a poll that ran. An
        unhealthy or disabled source answers false, so no caller can reach "no
        work to do" through a broken watcher.
        """
        return self.status is PollStatus.OK and not self.items

    @property
    def missing_program(self) -> str:
        """The program that could not be run, or empty when that is not the problem."""
        return self.program if self.reason is HealthReason.PROGRAM_UNAVAILABLE else ""

    def describe(self) -> str:
        """One line for a human: the source, what happened, and what to fix."""
        if self.status is PollStatus.DISABLED:
            return f"{self.source}: not enabled"
        if self.status is PollStatus.OK:
            counted = f"{len(self.items)} item(s)"
            if self.rejected:
                counted += f", {len(self.rejected)} unmappable"
            return f"{self.source}: polled, {counted}"
        named = self.reason.value if self.reason is not None else "unknown"
        return f"{self.source}: unhealthy ({named}) — {self.detail}"


def poll_source(
    store: ConfigStore,
    name: str,
    *,
    runner: CommandRunner | None = None,
) -> PollOutcome:
    """Poll one source by name, returning an outcome rather than raising.

    Every failure is an outcome because a poll tick reports on several sources
    at once: one broken definition must not stop the others from being polled or
    hide their results behind a traceback.
    """
    try:
        source = WatchSource.load(store, name)
    except KeyError:
        return _unhealthy(
            name,
            HealthReason.CONFIG_INVALID,
            "no watch source of that name is configured",
        )
    except ConfigValidationError as exc:
        return _unhealthy(name, HealthReason.CONFIG_INVALID, str(exc))
    return poll(store, source, runner=runner)


def poll(
    store: ConfigStore,
    source: WatchSource,
    *,
    runner: CommandRunner | None = None,
) -> PollOutcome:
    """Poll an already-loaded *source*."""
    if not source.enabled:
        return PollOutcome(source=source.name, status=PollStatus.DISABLED, program=source.program)

    # A poll command has no run context to substitute from, so a template that
    # references a variable can never be completed. Refused before the spawn and
    # by name: rendering it empty would run a different command than the one
    # configured, and the exit code would not say so.
    try:
        argv = source.poll.render({})
    except MissingVariableError as exc:
        return _unhealthy(
            source.name,
            HealthReason.CONFIG_INVALID,
            f"the poll command references variables that a poll cannot supply: "
            f"{', '.join(exc.variables)}",
            program=source.program,
        )

    resolved = _resolve_program(argv[0])
    if resolved is None:
        return _unhealthy(
            source.name,
            HealthReason.PROGRAM_UNAVAILABLE,
            f"the poll program {argv[0]!r} was not found on PATH, so this source "
            f"reports nothing about its items",
            program=argv[0],
        )

    cwd = _working_directory(store, source)
    timeout_s = poll_timeout_s(store, source.name)
    execute = runner if runner is not None else run_argv
    started = time.monotonic()
    produced = execute(argv, cwd=cwd, timeout_s=timeout_s)
    duration = time.monotonic() - started
    # argv[0] only: later elements carry operator-configured filters, and the
    # output they select is not log material.
    logger.info("watch source %r polled with %r in %.2fs", source.name, argv[0], duration)
    return _read(source, produced, duration=duration)


def poll_sources(
    store: ConfigStore,
    names: Sequence[str] | None = None,
    *,
    runner: CommandRunner | None = None,
) -> tuple[PollOutcome, ...]:
    """Poll *names*, or every declared source when *names* is ``None``.

    Every declared source gets an outcome, including the disabled and the
    broken. A source missing from the report would be a source nobody is
    watching and nobody can tell is unwatched.
    """
    selected = tuple(names) if names is not None else source_names(store)
    return tuple(poll_source(store, name, runner=runner) for name in selected)


# --- reading one command's output ------------------------------------------


def _read(source: WatchSource, produced: CommandOutcome, *, duration: float) -> PollOutcome:
    program = source.program
    if produced.timed_out:
        return _unhealthy(
            source.name,
            HealthReason.TIMED_OUT,
            f"the poll command {program!r} exceeded its timeout and was killed",
            program=program,
            duration_s=duration,
        )
    if produced.start_error:
        return _unhealthy(
            source.name,
            HealthReason.PROGRAM_UNAVAILABLE,
            f"the poll program {program!r} could not be started: {produced.start_error}",
            program=program,
            duration_s=duration,
        )
    if produced.exit_code != 0:
        detail = _first_line(produced.stderr) or _first_line(produced.stdout)
        suffix = f": {detail}" if detail else ""
        return _unhealthy(
            source.name,
            HealthReason.COMMAND_FAILED,
            f"the poll command {program!r} exited {produced.exit_code}{suffix}",
            program=program,
            exit_code=produced.exit_code,
            duration_s=duration,
        )
    if produced.stdout.endswith(TRUNCATION_NOTICE):
        return _unhealthy(
            source.name,
            HealthReason.OUTPUT_TRUNCATED,
            f"the poll command {program!r} produced more output than can be captured; "
            f"narrow it so one poll returns fewer or smaller items",
            program=program,
            exit_code=produced.exit_code,
            duration_s=duration,
        )

    try:
        entries = decode_entries(produced.stdout)
    except ValueError as exc:
        return _unhealthy(
            source.name,
            HealthReason.UNREADABLE_OUTPUT,
            f"the poll command {program!r} exited 0 but its output could not be read: {exc}",
            program=program,
            exit_code=produced.exit_code,
            duration_s=duration,
        )

    items, rejected, problems = _map_items(source, entries)
    if entries and not items:
        listed = "; ".join(entry.reason for entry in rejected[:MAX_RECORDED_PROBLEMS])
        return _unhealthy(
            source.name,
            HealthReason.FIELD_MAP_MISMATCH,
            f"the poll command {program!r} returned {len(entries)} item(s) and the field "
            f"mapping read none of them: {listed}",
            program=program,
            exit_code=produced.exit_code,
            duration_s=duration,
        )
    return PollOutcome(
        source=source.name,
        status=PollStatus.OK,
        items=items,
        program=program,
        exit_code=produced.exit_code,
        rejected=rejected,
        field_problems=problems,
        duration_s=duration,
    )


def decode_entries(stdout: str) -> list[Any]:
    """Decode poll output into a list of entries, raising ``ValueError`` when it is not one.

    Two shapes are accepted because real tracker clients emit both: one JSON
    array, or one JSON object per line. Nothing at all is not a third shape — a
    command that printed nothing has not told us its backlog is empty.

    Public because the review-feedback watcher reads its own configured poll
    command's output through it. Two decoders would be two answers to "did this
    command report an empty list or fail to report", and the distinction is the
    whole reason this one is careful.
    """
    text = stdout.strip()
    if not text:
        raise ValueError("it printed nothing; expected a JSON array of items")
    try:
        decoded = json.loads(text)
    except json.JSONDecodeError:
        return _decode_lines(text)
    if isinstance(decoded, list):
        return list(decoded)
    if isinstance(decoded, dict):
        raise ValueError(
            "it printed a single JSON object; expected an array of items, so a source "
            "with one item is not confused with a source that wraps its results"
        )
    raise ValueError(f"expected a JSON array of items, got {type(decoded).__name__}")


def _decode_lines(text: str) -> list[Any]:
    entries: list[Any] = []
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            entries.append(json.loads(stripped))
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"line {number} is neither JSON nor part of a JSON array: {exc}"
            ) from exc
    if not entries:
        raise ValueError("it printed nothing readable; expected a JSON array of items")
    return entries


def _map_items(
    source: WatchSource, entries: Sequence[Any]
) -> tuple[tuple[WatchedItem, ...], tuple[RejectedItem, ...], tuple[str, ...]]:
    items: list[WatchedItem] = []
    rejected: list[RejectedItem] = []
    problems: list[str] = []
    for index, entry in enumerate(entries):
        values, entry_problems = source.field_map.extract(entry)
        missing = tuple(field for field in REQUIRED_ITEM_FIELDS if not values[field].strip())
        if missing:
            reason = _rejection_reason(source, missing, entry_problems)
            if len(rejected) < MAX_RECORDED_PROBLEMS:
                rejected.append(RejectedItem(index=index, reason=reason))
            continue
        items.append(WatchedItem(source=source.name, **values))
        for problem in entry_problems:
            if problem not in problems and len(problems) < MAX_RECORDED_PROBLEMS:
                problems.append(problem)
    return tuple(items), tuple(rejected), tuple(problems)


def _rejection_reason(source: WatchSource, missing: Sequence[str], problems: Sequence[str]) -> str:
    described = ", ".join(f"{field} at {source.field_map.path_of(field)!r}" for field in missing)
    reason = f"no value for {described}"
    if problems:
        reason += f" ({'; '.join(problems)})"
    return reason


# --- resolution helpers ----------------------------------------------------


def _resolve_program(program: str) -> str | None:
    """Return the executable *program* resolves to, or ``None``.

    Resolved with the same ``PATH`` the child will inherit, so this answer and
    the spawn's answer are the same one. A path with a separator is checked
    directly: ``shutil.which`` would not search for it, and reporting "not on
    PATH" for an absolute path an operator typed names the wrong problem.
    """
    if os.sep in program or (os.altsep and os.altsep in program):
        candidate = Path(program).expanduser()
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
        return None
    return shutil.which(program)


def _working_directory(store: ConfigStore, source: WatchSource) -> Path:
    """Where a poll command runs.

    A source mapped to a project polls inside that project's tree, because a
    tracker client asked about "this repository" answers from the checkout it is
    standing in. Everything else polls in the app's own data directory: a
    directory the app owns, rather than whatever the gateway's working directory
    happens to be, which is neither stable nor the operator's choice.
    """
    configured = _project_path(store, source.project)
    if configured is not None and configured.is_dir():
        return configured
    root = store.root
    root.mkdir(parents=True, exist_ok=True)
    return root


def _project_path(store: ConfigStore, project: str) -> Path | None:
    if not project:
        return None
    projects = store.document().get(SECTION_PROJECTS)
    entry = projects.get(project) if isinstance(projects, dict) else None
    path = entry.get("path") if isinstance(entry, dict) else None
    if not isinstance(path, str) or not path.strip():
        return None
    return Path(path).expanduser()


def _unhealthy(
    source: str,
    reason: HealthReason,
    detail: str,
    *,
    program: str = "",
    exit_code: int | None = None,
    duration_s: float = 0.0,
) -> PollOutcome:
    logger.warning("watch source %r is unhealthy (%s): %s", source, reason.value, detail)
    return PollOutcome(
        source=source,
        status=PollStatus.UNHEALTHY,
        reason=reason,
        detail=detail,
        program=program,
        exit_code=exit_code,
        duration_s=duration_s,
    )


def _first_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return ""
