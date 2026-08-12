"""Autonomy_Policy resolution: how far a triggered run proceeds unattended.

The policy answers one question for one run: given the source that produced the
item, the spec type being built, and the trust class of whoever authored the
text, how far may this run go without a human doing anything? The answer is a
rung on a strictly ordered ladder — authoring, execution, delivery,
integration — and an enabled rung implies every rung below it, so granting
delivery grants execution without the operator having to say both.

Three properties hold, and each exists because its opposite is a silent
authority increase:

* **Absence means human-reserved.** A triple with nothing configured resolves to
  authoring only, with execution reserved for an explicit human action. This is
  the behaviour of an absent setting, not a recommendation in a document: an
  install that configures nothing never executes unattended, and a source
  someone forgot to configure fails toward asking rather than toward acting.
* **Presence means exactly what it says.** A configured level resolves to that
  level and no other. Nothing rounds up to the next rung, and nothing infers a
  rung from adjacent configuration such as a delivery posture switch.
* **Configuration is the only input.** This module reads; it has no writer, and
  the config store's single validated write path refuses the ``sources``
  section from a surface no operator confirmed. So no engine path, tool call, or
  agent turn can widen the authority it is about to be judged by — a policy an
  unattended run could edit is not a policy, it is a suggestion.

Resolution walks the (submitter class, spec type) grid most specific first, and
takes the class dimension as the more specific one:

    (class, type) -> (class, *) -> (*, type) -> (*, *) -> unconfigured

Class-first is not arbitrary. Given ``default: {quick: integration}`` alongside
``external: {default: authoring}``, class-first answers authoring for an
external contributor's quick spec and type-first answers integration. When two
partial declarations disagree, the one naming the *author* is the one an
operator wrote to hold something back.

The ladder is also not the whole story for a stage that carries its own posture
switch. Integration is gated by the ladder *and* by the delivery posture, both
of which must allow it: the ladder says how far a run may go, the posture switch
says whether this project's protected destination accepts unattended writes, and
integration is the one stage a mistake cannot undo.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Mapping

from .config import (
    AUTONOMY_LEVELS,
    LEAST_TRUSTED_CLASS,
    SPEC_TYPES,
    SUBMITTER_CLASSES,
    WILDCARD_KEY,
    ConfigError,
    ConfigStore,
    ConfigValidationError,
)
from .config.schema import SECTION_SOURCES

#: Field holding a source's policy grid. The schema's ``SOURCE_FIELDS`` is the
#: owner of the source vocabulary; this names the one entry resolution reads.
AUTONOMY_FIELD = "autonomy"

#: Rung positions, taken from the schema's ladder so ordering has one owner. A
#: level the schema accepts but this table does not know is a drift bug, and
#: raising on it beats resolving it to an arbitrary rank.
_RANKS: dict[str, int] = {name: index for index, name in enumerate(AUTONOMY_LEVELS)}


class AutonomyLevel(Enum):
    """One rung of the autonomy ladder.

    Deliberately not a ``str`` enum. String members would inherit lexicographic
    comparison, and lexicographically ``delivery`` sorts below ``execution``
    while the ladder puts it above — so ``level >= DELIVERY`` would quietly
    return true for an execution-only policy. Comparison is therefore expressed
    through :meth:`permits` against explicit ranks, and the operators are absent
    rather than wrong.
    """

    AUTHORING = "authoring"
    EXECUTION = "execution"
    DELIVERY = "delivery"
    INTEGRATION = "integration"

    @property
    def rank(self) -> int:
        """Position on the ladder, ``0`` being the least autonomous rung."""
        return _RANKS[self.value]

    def permits(self, needed: AutonomyLevel) -> bool:
        """Whether this level authorizes work that requires *needed*.

        This is where "an enabled level implies every lower level" lives, so a
        caller asks whether execution is permitted rather than comparing names.
        """
        return needed.rank <= self.rank

    def implies(self) -> tuple[AutonomyLevel, ...]:
        """Every level this one authorizes, least autonomous first."""
        return tuple(level for level in AutonomyLevel if level.rank <= self.rank)


#: The level in force when a triple has nothing configured: authoring only, with
#: execution reserved for an explicit human action.
UNCONFIGURED_LEVEL = AutonomyLevel.AUTHORING


@dataclass(frozen=True)
class AutonomyDecision:
    """The resolved level for one (source, spec type, submitter class) triple.

    ``declared_at`` carries the dotted config path the level was read from, so a
    surface can show an operator which declaration is in force instead of making
    them guess which of four grid cells matched. It is empty exactly when
    nothing was configured, which is why :attr:`is_configured` derives from it
    rather than being a second field the two could disagree on.
    """

    level: AutonomyLevel
    source: str | None
    spec_type: str
    submitter_class: str
    declared_at: str = ""

    @property
    def is_configured(self) -> bool:
        """Whether a configured declaration produced this level."""
        return bool(self.declared_at)

    @property
    def execution_is_human_reserved(self) -> bool:
        """Whether starting execution requires an explicit human action."""
        return not self.permits(AutonomyLevel.EXECUTION)

    def permits(self, level: AutonomyLevel) -> bool:
        """Whether the resolved level authorizes work that requires *level*."""
        return self.level.permits(level)


class AutonomyPolicy:
    """Resolves the Autonomy_Policy for a run, from configuration only.

    Construct from the config store (live, so an operator's edit takes effect on
    the next resolution) or from an already-loaded document. Either way the
    instance holds a way to *read* configuration and nothing else: there is no
    mutating method here, and none may be added — a later MCP surface will expose
    whatever this class can do, so anything reachable from it is reachable from
    an agent turn.
    """

    def __init__(self, reader: Callable[[], Mapping[str, Any]]) -> None:
        self._read_document = reader

    @classmethod
    def from_store(cls, store: ConfigStore) -> AutonomyPolicy:
        """Resolve against *store*'s current document on every call."""
        return cls(store.document)

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> AutonomyPolicy:
        """Resolve against a snapshot of *document*.

        The document is copied, so a caller that keeps mutating its own dict
        cannot change what an already-constructed policy resolves to.
        """
        snapshot: Mapping[str, Any] = copy.deepcopy(dict(document))
        return cls(lambda: snapshot)

    def resolve(
        self,
        *,
        source: str | None,
        spec_type: str,
        submitter_class: str | None,
    ) -> AutonomyDecision:
        """Resolve the level in force for one triple.

        *submitter_class* is derived per authored element from that element's own
        author and is passed in rather than read from the item; ``None`` means the
        author could not be determined, which resolves to the least-trusted class.

        A run with no watch source behind it has no policy to read and resolves
        to the unconfigured default. That is the correct answer rather than a gap:
        an interactive run's initiator is the human driving it, so it does not
        need the policy's permission to proceed.

        Raises ``ValueError`` for a spec type or submitter class outside the
        schema's vocabulary, and ``ConfigValidationError`` when the stored grid
        is malformed — a hand-edited level the schema would reject is named
        rather than substituted, because substituting silently would run under a
        policy nobody wrote.
        """
        klass = LEAST_TRUSTED_CLASS if submitter_class is None else submitter_class
        if spec_type not in SPEC_TYPES:
            raise ValueError(f"unknown spec type: {spec_type!r}")
        if klass not in SUBMITTER_CLASSES:
            raise ValueError(f"unknown submitter class: {klass!r}")

        grid, base_path = self._grid(source)
        for class_key, type_key in _candidates(klass, spec_type):
            if class_key not in grid:
                continue
            by_type = grid[class_key]
            if not isinstance(by_type, Mapping):
                # An absent row and a malformed one are not the same question.
                # Absent means the operator did not write a rule here, so a
                # broader cell should answer. Malformed means they DID write one
                # and it cannot be read -- and because the specific row is the
                # restrictive one under class-first precedence, skipping it hands
                # the decision to a wildcard that may permit more. A misplaced
                # indent under a class name would then raise this run's authority
                # instead of lowering it, which is the one direction a
                # configuration mistake must never move.
                raise ConfigValidationError(
                    [
                        ConfigError(
                            f"{base_path}.{class_key}",
                            "expected an object keyed by spec type",
                        )
                    ]
                )
            if type_key not in by_type:
                continue
            path = f"{base_path}.{class_key}.{type_key}"
            return AutonomyDecision(
                level=_level_at(by_type[type_key], path),
                source=source,
                spec_type=spec_type,
                submitter_class=klass,
                declared_at=path,
            )
        return AutonomyDecision(
            level=UNCONFIGURED_LEVEL,
            source=source,
            spec_type=spec_type,
            submitter_class=klass,
        )

    def _grid(self, source: str | None) -> tuple[Mapping[str, Any], str]:
        """Return the source's policy grid and the dotted path it lives at."""
        if source is None:
            return {}, ""
        base_path = f"{SECTION_SOURCES}.{source}.{AUTONOMY_FIELD}"
        sources = self._read_document().get(SECTION_SOURCES)
        if not isinstance(sources, Mapping):
            return {}, base_path
        entry = sources.get(source)
        if not isinstance(entry, Mapping):
            return {}, base_path
        grid = entry.get(AUTONOMY_FIELD)
        if grid is None:
            return {}, base_path
        if not isinstance(grid, Mapping):
            raise ConfigValidationError(
                [ConfigError(base_path, "expected an object keyed by submitter class")]
            )
        return grid, base_path


def _candidates(submitter_class: str, spec_type: str) -> tuple[tuple[str, str], ...]:
    """Grid keys to try, most specific first, class dimension taking precedence."""
    return tuple(
        (class_key, type_key)
        for class_key in (submitter_class, WILDCARD_KEY)
        for type_key in (spec_type, WILDCARD_KEY)
    )


def _level_at(raw: Any, path: str) -> AutonomyLevel:
    """Convert a stored level to a rung, naming *path* when it is not one."""
    if isinstance(raw, str) and raw in _RANKS:
        return AutonomyLevel(raw)
    raise ConfigValidationError(
        [ConfigError(path, "expected one of: " + ", ".join(AUTONOMY_LEVELS))]
    )
