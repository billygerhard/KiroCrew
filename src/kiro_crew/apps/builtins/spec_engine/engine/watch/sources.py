"""Watch sources: a poll command plus a field mapping, both read from configuration.

Defining a source takes no plugin code. A source is a program to run and a map
from the engine's seven item fields to wherever that program's output puts them,
so watching a new tracker is a configuration entry rather than a release.

Three properties are worth stating, because each closes a failure this module
would otherwise have.

**Enabled is opt-in per source.** A source with no ``enabled`` key does not
poll. Polling is the step that decides an unattended run may start at all, so a
half-finished source definition must be inert rather than live.

**The poll command reuses the delivery template machinery.** It is parsed once
into argv elements by :mod:`..delivery.templates` and run with no shell, for the
same reason a delivery command is: a poll command carries a repository name or a
label filter an operator typed, and the program it names decides what runs. A
second substitution engine here would be a second place for that to go wrong.

**The field mapping is a path, not an expression.** ``author.login`` reads a key
then a key; ``labels.0.name`` reads a key, an index, a key. Nothing is
evaluated, nothing is matched, and no source output can direct the walk
somewhere else. Reading tracker output is where a clever reader would become an
interpreter for text a stranger wrote.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence, Union

from ..config import ConfigError, ConfigStore, ConfigValidationError
from ..config.schema import SECTION_SOURCES
from ..delivery.templates import CommandTemplate, TemplateError
from .items import ITEM_FIELDS

#: Keys a source entry carries that this module reads. The schema owns the full
#: field vocabulary; these are the ones a poll needs.
ENABLED_KEY = "enabled"
POLL_KEY = "poll"
FIELD_MAP_KEY = "field_map"
PROJECT_KEY = "project"
BASE_BRANCH_KEY = "base_branch"
PRESET_KEY = "preset"

#: Setting holding the seconds between poll ticks for a source.
INTERVAL_SETTING = "watch.interval_s"

#: Setting holding the wall clock one poll command may take.
POLL_TIMEOUT_SETTING = "timeouts.poll_command_s"

#: Separator between path segments in a field mapping value.
PATH_SEPARATOR = "."

#: One step of a field path: a mapping key, or a list index.
PathSegment = Union[str, int]


def source_names(store: ConfigStore) -> tuple[str, ...]:
    """Return every declared source name, in the order the document holds them.

    Declared, not enabled, and not valid either: a caller reporting source
    health needs the names of the sources whose definitions are broken just as
    much as the names of the ones that work.
    """
    node = store.document().get(SECTION_SOURCES)
    if not isinstance(node, Mapping):
        return ()
    return tuple(name for name in node if isinstance(name, str) and name.strip())


@dataclass(frozen=True)
class FieldMapping:
    """Where each engine item field lives in one source's output."""

    paths: tuple[tuple[str, tuple[PathSegment, ...]], ...]

    @classmethod
    def identity(cls) -> "FieldMapping":
        """The mapping for output that already uses the engine's field names.

        This is what an unmapped source resolves to. It is a real mapping rather
        than a "no mapping" state so every source is read through one code path;
        a source whose command emits ``{"identifier": ...}`` needs no
        configuration to say so.
        """
        return cls(paths=tuple((field, (field,)) for field in ITEM_FIELDS))

    @classmethod
    def parse(cls, node: Any, path: str) -> "FieldMapping":
        """Parse a configured ``field_map`` object.

        An unknown field name is an error rather than an ignored key: a mapping
        for ``author`` when the engine field is ``submitter`` looks applied in
        the file and resolves to nothing at all.
        """
        if node is None:
            return cls.identity()
        if not isinstance(node, Mapping):
            raise ConfigValidationError(
                [ConfigError(path, "expected an object mapping item fields to output paths")]
            )
        errors: list[ConfigError] = []
        parsed: list[tuple[str, tuple[PathSegment, ...]]] = []
        for field, raw in node.items():
            field_path = f"{path}.{field}"
            if field not in ITEM_FIELDS:
                errors.append(ConfigError(field_path, "expected one of: " + ", ".join(ITEM_FIELDS)))
                continue
            if not isinstance(raw, str) or not raw.strip():
                errors.append(ConfigError(field_path, "expected a non-empty output path"))
                continue
            try:
                parsed.append((field, _parse_path(raw)))
            except ValueError as exc:
                errors.append(ConfigError(field_path, str(exc)))
        if errors:
            raise ConfigValidationError(errors)
        mapped = dict(parsed)
        # Fields the operator did not map still resolve, from the same-named key.
        # Spelling out only the fields whose names differ is the common case, and
        # an unmapped field that silently never resolved would read as a tracker
        # that does not report it.
        for field in ITEM_FIELDS:
            mapped.setdefault(field, (field,))
        return cls(paths=tuple((field, mapped[field]) for field in ITEM_FIELDS))

    @property
    def fields(self) -> tuple[str, ...]:
        """Mapped field names, in the engine's field order."""
        return tuple(field for field, _ in self.paths)

    def path_of(self, field: str) -> str:
        """The configured path for *field*, rendered for reporting."""
        for name, segments in self.paths:
            if name == field:
                return PATH_SEPARATOR.join(str(segment) for segment in segments)
        raise KeyError(field)

    def extract(self, raw: Any) -> tuple[dict[str, str], tuple[str, ...]]:
        """Map one output item, returning ``(values, problems)``.

        Values are strings; an absent path yields ``""``. Problems name the
        fields whose path led somewhere a scalar could not come from, so a
        mapping aimed at the wrong shape is reported rather than read as a
        tracker that left the field blank.
        """
        values: dict[str, str] = {field: "" for field in ITEM_FIELDS}
        problems: list[str] = []
        if not isinstance(raw, Mapping):
            return values, (f"expected an object, got {_shape(raw)}",)
        for field, segments in self.paths:
            found, node = _walk(raw, segments)
            if not found:
                continue
            text, ok = _scalar(node)
            if not ok:
                rendered = PATH_SEPARATOR.join(str(segment) for segment in segments)
                problems.append(f"{field} at {rendered!r} is {_shape(node)}, not a value")
                continue
            values[field] = text
        return values, tuple(problems)


@dataclass(frozen=True)
class WatchSource:
    """One source's definition as configured: what to run, and how to read it."""

    name: str
    enabled: bool
    poll: CommandTemplate
    field_map: FieldMapping
    project: str = ""
    base_branch: str = ""
    preset: str = ""
    declared_at: str = ""

    @property
    def program(self) -> str:
        """The literal program the poll command runs."""
        return self.poll.program

    @classmethod
    def load(cls, store: ConfigStore, name: str) -> "WatchSource":
        """Read *name*'s definition, raising ``ConfigValidationError`` when unusable.

        Raises ``KeyError`` when no source of that name is declared, which is a
        different problem from a source declared badly and is reported
        differently.
        """
        node = store.document().get(SECTION_SOURCES)
        entry = node.get(name) if isinstance(node, Mapping) else None
        if entry is None:
            raise KeyError(name)
        declared_at = f"{SECTION_SOURCES}.{name}"
        if not isinstance(entry, Mapping):
            raise ConfigValidationError([ConfigError(declared_at, "expected an object")])
        enabled = entry.get(ENABLED_KEY, False)
        if not isinstance(enabled, bool):
            raise ConfigValidationError(
                [ConfigError(f"{declared_at}.{ENABLED_KEY}", "expected true or false")]
            )
        return cls(
            name=name,
            enabled=enabled,
            poll=_parse_poll(entry.get(POLL_KEY), f"{declared_at}.{POLL_KEY}"),
            field_map=FieldMapping.parse(
                entry.get(FIELD_MAP_KEY), f"{declared_at}.{FIELD_MAP_KEY}"
            ),
            project=_text(entry.get(PROJECT_KEY)),
            base_branch=_text(entry.get(BASE_BRANCH_KEY)),
            preset=_text(entry.get(PRESET_KEY)),
            declared_at=declared_at,
        )


def load_sources(store: ConfigStore, *, enabled_only: bool = False) -> tuple[WatchSource, ...]:
    """Load every declared source, skipping the ones that will not load.

    Skipping is safe **only** because polling reports an unloadable source as
    unhealthy by name: this function serves callers that want the working set,
    and the health of the rest is answered by :mod:`.poll`.
    """
    loaded: list[WatchSource] = []
    for name in source_names(store):
        try:
            source = WatchSource.load(store, name)
        except (ConfigValidationError, KeyError):
            continue
        if enabled_only and not source.enabled:
            continue
        loaded.append(source)
    return tuple(loaded)


def poll_interval_s(store: ConfigStore, source: str) -> int:
    """Seconds between poll ticks for *source*."""
    return int(store.effective(INTERVAL_SETTING, source=source).value)


def poll_timeout_s(store: ConfigStore, source: str) -> int:
    """Wall clock *source*'s poll command may take before it is killed."""
    return int(store.effective(POLL_TIMEOUT_SETTING, source=source).value)


# --- parsing helpers -------------------------------------------------------


def _parse_poll(node: Any, path: str) -> CommandTemplate:
    if node is None:
        raise ConfigValidationError([ConfigError(path, "required source field is missing")])
    try:
        return CommandTemplate.parse(node)
    except TemplateError as exc:
        raise ConfigValidationError([ConfigError(path, str(exc))]) from exc


def _parse_path(raw: str) -> tuple[PathSegment, ...]:
    segments: list[PathSegment] = []
    for piece in raw.split(PATH_SEPARATOR):
        if not piece.strip():
            raise ValueError(f"{raw!r} has an empty path segment")
        if piece.isdigit():
            segments.append(int(piece))
        else:
            segments.append(piece)
    return tuple(segments)


def _walk(raw: Mapping[str, Any], segments: Sequence[PathSegment]) -> tuple[bool, Any]:
    """Follow *segments* into *raw*, returning ``(found, value)``."""
    node: Any = raw
    for segment in segments:
        if isinstance(segment, int):
            if isinstance(node, Sequence) and not isinstance(node, (str, bytes)):
                if segment >= len(node):
                    return False, None
                node = node[segment]
                continue
            return False, None
        if isinstance(node, Mapping) and segment in node:
            node = node[segment]
            continue
        return False, None
    if node is None:
        return False, None
    return True, node


def _scalar(node: Any) -> tuple[str, bool]:
    """Render *node* as text, reporting whether it was a value at all.

    ``true``/``false`` rather than Python's ``True``/``False``: the text came
    from JSON and is shown to a human beside the tracker that produced it.
    """
    if isinstance(node, str):
        return node, True
    if isinstance(node, bool):
        return ("true" if node else "false"), True
    if isinstance(node, (int, float)):
        return str(node), True
    return "", False


def _shape(node: Any) -> str:
    """Name *node*'s JSON shape, for a mapping problem a human has to fix."""
    if node is None:
        return "empty"
    if isinstance(node, Mapping):
        return "an object"
    if isinstance(node, (str, bytes)):
        return "a string"
    if isinstance(node, Sequence):
        return "a list"
    return type(node).__name__


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""
