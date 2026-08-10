"""Published request and response schemas, one pair per delegable capability.

Two jobs, one definition. Externally these are documents an author of a provider
reads: :func:`published_schemas` emits standard JSON Schema for every capability
and every version, which is what makes the extension point an interface rather
than a promise. Internally the same definition validates each response before a
single finding is recorded, so the published contract and the enforced contract
cannot drift apart.

The validator is written here rather than taken from a schema library on purpose.
Response validation is not optional: a response that skipped validation is a
response the engine cannot say anything about, and a validator that silently
becomes a no-op when an optional dependency is missing is worse than none —
every call then reports success at a depth nobody checked. Keeping it in the
package means it always runs.

Validation is closed: an unknown key is an error. A provider that returns
``coverage_`` instead of ``coverage`` has not declared its coverage, and quietly
keeping the extra key would let it look like it had.

Versioning is per capability. ``schema_version`` on the wire selects the contract
the payload claims to satisfy; a version this build does not publish is a
validation failure, not a best-effort parse, because the alternative is reading
fields whose meaning changed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from ..config import DELEGABLE_CAPABILITIES
from .contracts import (
    ARTIFACT_KINDS,
    FINDING_SEVERITIES,
    NATIVE_FORMAT_VERSION,
    UnknownCapability,
    require_delegable,
)

#: The version every capability's schemas currently publish at. Per-capability
#: because capabilities evolve independently; equal today because they all ship
#: together for the first time.
CURRENT_SCHEMA_VERSION = 1

REQUEST = "request"
RESPONSE = "response"

#: Directions a schema may describe.
DIRECTIONS: tuple[str, ...] = (REQUEST, RESPONSE)

#: Cap on one string inside a payload. Provider output is unbounded input, and a
#: bound applied at parse time is the only one that keeps an oversized field from
#: being held in memory in the first place.
MAX_STRING_CHARS = 64 * 1024

#: Cap on one array inside a payload, for the same reason.
MAX_ARRAY_ITEMS = 4096


@dataclass(frozen=True)
class SchemaError:
    """One schema violation, addressed by its path inside the payload."""

    path: str
    message: str

    def __str__(self) -> str:
        where = self.path or "(root)"
        return f"{where}: {self.message}"


class SchemaViolation(ValueError):
    """Raised when a payload the engine itself built fails its own schema.

    A provider's invalid response is a degradation, not an exception: the engine
    falls back and the run continues. This is the other direction — an engine-built
    request that does not satisfy the published contract is a bug in the engine,
    and a bug is worth a traceback.
    """

    def __init__(self, errors: Iterable[SchemaError]) -> None:
        self.errors: tuple[SchemaError, ...] = tuple(errors)
        super().__init__("; ".join(str(error) for error in self.errors) or "invalid payload")


# --- the type algebra ------------------------------------------------------
#
# Each node validates a decoded JSON value and emits its JSON Schema form. Two
# methods, no inheritance tree worth speaking of: the whole point is that the
# published document and the executed check come from the same object.


class TypeSpec:
    """A validatable, publishable payload type."""

    def check(self, value: Any, path: str) -> list[SchemaError]:  # pragma: no cover - abstract
        raise NotImplementedError

    def json_schema(self) -> dict[str, Any]:  # pragma: no cover - abstract
        raise NotImplementedError


@dataclass(frozen=True)
class Str(TypeSpec):
    """A string, optionally restricted to a fixed vocabulary."""

    choices: tuple[str, ...] = ()
    allow_empty: bool = True
    max_chars: int = MAX_STRING_CHARS

    def check(self, value: Any, path: str) -> list[SchemaError]:
        if not isinstance(value, str):
            return [SchemaError(path, "expected a string")]
        if not self.allow_empty and not value.strip():
            return [SchemaError(path, "expected a non-empty string")]
        if len(value) > self.max_chars:
            return [SchemaError(path, f"longer than the {self.max_chars} character limit")]
        if self.choices and value not in self.choices:
            return [SchemaError(path, "expected one of: " + ", ".join(self.choices))]
        return []

    def json_schema(self) -> dict[str, Any]:
        schema: dict[str, Any] = {"type": "string", "maxLength": self.max_chars}
        if not self.allow_empty:
            schema["minLength"] = 1
        if self.choices:
            schema["enum"] = list(self.choices)
        return schema


@dataclass(frozen=True)
class Int(TypeSpec):
    """An integer. ``bool`` is refused: Python counts it as an int, and a
    ``true`` where a count belongs turns a mistake into the value 1."""

    minimum: int | None = None
    maximum: int | None = None

    def check(self, value: Any, path: str) -> list[SchemaError]:
        if isinstance(value, bool) or not isinstance(value, int):
            return [SchemaError(path, "expected an integer")]
        return _bounds(value, self.minimum, self.maximum, path)

    def json_schema(self) -> dict[str, Any]:
        return _with_bounds({"type": "integer"}, self.minimum, self.maximum)


@dataclass(frozen=True)
class Num(TypeSpec):
    """A number. An int is accepted and widened, since JSON has one numeric type."""

    minimum: float | None = None
    maximum: float | None = None

    def check(self, value: Any, path: str) -> list[SchemaError]:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return [SchemaError(path, "expected a number")]
        return _bounds(float(value), self.minimum, self.maximum, path)

    def json_schema(self) -> dict[str, Any]:
        return _with_bounds({"type": "number"}, self.minimum, self.maximum)


@dataclass(frozen=True)
class Bool(TypeSpec):
    """A boolean."""

    def check(self, value: Any, path: str) -> list[SchemaError]:
        if not isinstance(value, bool):
            return [SchemaError(path, "expected true or false")]
        return []

    def json_schema(self) -> dict[str, Any]:
        return {"type": "boolean"}


@dataclass(frozen=True)
class Arr(TypeSpec):
    """A homogeneous array."""

    item: TypeSpec
    max_items: int = MAX_ARRAY_ITEMS

    def check(self, value: Any, path: str) -> list[SchemaError]:
        if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
            return [SchemaError(path, "expected an array")]
        if len(value) > self.max_items:
            return [SchemaError(path, f"more than the {self.max_items} item limit")]
        errors: list[SchemaError] = []
        for index, element in enumerate(value):
            errors.extend(self.item.check(element, f"{path}[{index}]"))
        return errors

    def json_schema(self) -> dict[str, Any]:
        return {
            "type": "array",
            "maxItems": self.max_items,
            "items": self.item.json_schema(),
        }


@dataclass(frozen=True)
class StrMap(TypeSpec):
    """An object with free-form string keys and uniformly typed values."""

    value: TypeSpec

    def check(self, value: Any, path: str) -> list[SchemaError]:
        if not isinstance(value, Mapping):
            return [SchemaError(path, "expected an object")]
        errors: list[SchemaError] = []
        for key, item in value.items():
            if not isinstance(key, str):
                errors.append(SchemaError(path, "object keys must be strings"))
                continue
            errors.extend(self.value.check(item, f"{path}.{key}" if path else key))
        return errors

    def json_schema(self) -> dict[str, Any]:
        return {"type": "object", "additionalProperties": self.value.json_schema()}


@dataclass(frozen=True)
class Obj(TypeSpec):
    """A closed object: named fields, some optional, no unknown keys."""

    fields: Mapping[str, TypeSpec]
    optional: frozenset[str] = frozenset()

    def check(self, value: Any, path: str) -> list[SchemaError]:
        if not isinstance(value, Mapping):
            return [SchemaError(path, "expected an object")]
        errors: list[SchemaError] = []
        for name, spec in self.fields.items():
            child = f"{path}.{name}" if path else name
            if name not in value:
                if name not in self.optional:
                    errors.append(SchemaError(child, "required field is missing"))
                continue
            errors.extend(spec.check(value[name], child))
        for name in value:
            if name not in self.fields:
                child = f"{path}.{name}" if path else str(name)
                errors.append(SchemaError(child, "unknown field"))
        return errors

    def json_schema(self) -> dict[str, Any]:
        required = [name for name in self.fields if name not in self.optional]
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {name: spec.json_schema() for name, spec in self.fields.items()},
            "required": required,
        }


def _bounds(
    value: float, minimum: float | None, maximum: float | None, path: str
) -> list[SchemaError]:
    if minimum is not None and value < minimum:
        return [SchemaError(path, f"must be at least {minimum:g}")]
    if maximum is not None and value > maximum:
        return [SchemaError(path, f"must be at most {maximum:g}")]
    return []


def _with_bounds(
    schema: dict[str, Any], minimum: float | None, maximum: float | None
) -> dict[str, Any]:
    if minimum is not None:
        schema["minimum"] = minimum
    if maximum is not None:
        schema["maximum"] = maximum
    return schema


# --- the published payload schemas -----------------------------------------


@dataclass(frozen=True)
class PayloadSchema:
    """One capability's contract for one direction at one version."""

    capability: str
    direction: str
    version: int
    root: Obj
    title: str

    @property
    def schema_id(self) -> str:
        """Identifier a published document carries.

        A relative reference rather than an absolute URL: this contract is
        published with the package, and pointing it at a host would make reading
        it depend on that host still serving it.
        """
        return f"spec-engine/capability/{self.capability}/{self.direction}/v{self.version}.json"

    def errors(self, payload: Any) -> tuple[SchemaError, ...]:
        """Return every violation in *payload*, empty when it satisfies the schema."""
        return tuple(self.root.check(payload, ""))

    def validate(self, payload: Any) -> None:
        """Raise :class:`SchemaViolation` unless *payload* satisfies the schema."""
        errors = self.errors(payload)
        if errors:
            raise SchemaViolation(errors)

    def json_schema(self) -> dict[str, Any]:
        """Return the publishable JSON Schema document."""
        document: dict[str, Any] = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": self.schema_id,
            "title": self.title,
        }
        document.update(self.root.json_schema())
        return document


@dataclass(frozen=True)
class _Any(TypeSpec):
    """A JSON value of any type.

    Used only for capability-specific parameter values, which the *provider*
    validates. The engine cannot enumerate them without knowing every provider,
    and pretending to would just reject valid extensions.
    """

    def check(self, value: Any, path: str) -> list[SchemaError]:
        return []

    def json_schema(self) -> dict[str, Any]:
        return {}


_ANY = _Any()

_ARTIFACT = Obj(
    fields={
        "kind": Str(choices=ARTIFACT_KINDS),
        "path": Str(allow_empty=False),
        "revision": Str(),
    },
    optional=frozenset({"revision"}),
)

_SKIPPED = Obj(fields={"item": Str(allow_empty=False), "reason": Str()})

_COVERAGE = Obj(
    fields={
        "processed": Arr(Str(allow_empty=False)),
        "skipped": Arr(_SKIPPED),
    },
    # Both default to empty: a provider that processed nothing and skipped
    # nothing is reporting an empty run, which is a legitimate answer.
    optional=frozenset({"processed", "skipped"}),
)

_QUESTION = Obj(
    fields={
        "question": Str(allow_empty=False),
        "choices": Arr(Str()),
        "consequences": Arr(Str()),
        "recommended": Str(),
    },
    optional=frozenset({"choices", "consequences", "recommended"}),
)

_FINDING = Obj(
    fields={
        "kind": Str(allow_empty=False),
        "severity": Str(choices=FINDING_SEVERITIES),
        "message": Str(allow_empty=False),
        # Acceptance criteria or task identifiers the finding concerns. Present
        # so the engine can route a finding rather than hand over prose.
        "refs": Arr(Str(allow_empty=False)),
        "question": _QUESTION,
    },
    optional=frozenset({"refs", "question"}),
)

_PROVIDER = Obj(
    fields={"name": Str(allow_empty=False), "version": Str()},
    optional=frozenset({"version"}),
)

_COST = Obj(fields={"credits": Num(minimum=0.0)})


def _request_root(capability: str) -> Obj:
    return Obj(
        fields={
            "schema_version": Int(minimum=1),
            "capability": Str(choices=(capability,)),
            "spec_type": Str(allow_empty=False),
            "format_version": Str(choices=(NATIVE_FORMAT_VERSION,)),
            "run": Str(),
            "deadline_s": Int(minimum=0),
            "artifacts": Arr(_ARTIFACT),
            "parameters": StrMap(_ANY),
        },
        optional=frozenset({"run", "parameters", "artifacts"}),
    )


def _response_root(capability: str, result: TypeSpec) -> Obj:
    return Obj(
        fields={
            "schema_version": Int(minimum=1),
            "capability": Str(choices=(capability,)),
            "provider": _PROVIDER,
            "coverage": _COVERAGE,
            "findings": Arr(_FINDING),
            "cost": _COST,
            "result": result,
        },
        # Coverage and findings are required: they are the two fields that say
        # what the provider actually did. Cost and result are optional because a
        # provider may spend nothing and return no capability-specific body.
        optional=frozenset({"cost", "result"}),
    )


#: Capability-specific response bodies. Anything a capability answers beyond the
#: shared envelope lives here, so the envelope stays identical across all seven.
_RESULT_SPECS: dict[str, TypeSpec] = {
    # Declared analysis depth, so a structural pass is never read as a semantic
    # one. Deliberately not a closed vocabulary here: the depth ladder's
    # published rungs are owned by the analysis capability.
    "analysis": Obj(fields={"depth": Str(allow_empty=False)}, optional=frozenset()),
    # Which documents an authoring pass wrote.
    "authoring": Obj(fields={"documents": Arr(Str(allow_empty=False))}, optional=frozenset()),
    # A review verdict plus its rationale.
    "review": Obj(
        fields={"verdict": Str(allow_empty=False), "rationale": Str()},
        optional=frozenset({"rationale"}),
    ),
    # Which tasks an implementation pass moved, and to what status.
    "implementation": Obj(
        fields={"tasks": Arr(Obj(fields={"id": Str(allow_empty=False), "status": Str()}))},
        optional=frozenset(),
    ),
    # Supplementary validation adds findings only; it has no body of its own.
    "validation_rules": Obj(fields={}, optional=frozenset()),
    # Items a watch source observed. Bodies stay out of the envelope: the engine
    # asks for identity and classification, not a copy of the tracker.
    "watch_sources": Obj(
        fields={
            "items": Arr(
                Obj(
                    fields={
                        "id": Str(allow_empty=False),
                        "title": Str(),
                        "state": Str(),
                        "url": Str(),
                        "classification": Str(),
                        "submitter": Str(),
                    },
                    optional=frozenset({"title", "state", "url", "classification", "submitter"}),
                )
            )
        },
        optional=frozenset(),
    ),
    # Model identifiers the host advertises. Ids only: a picker lists what the
    # host advertises, and inventing metadata here would invite a static list.
    "model_catalog": Obj(fields={"models": Arr(Str(allow_empty=False))}, optional=frozenset()),
}

_TITLES: dict[str, str] = {
    "analysis": "spec analysis",
    "authoring": "spec document authoring",
    "review": "review verdict",
    "implementation": "task implementation",
    "validation_rules": "supplementary validation rules",
    "watch_sources": "watch source poll",
    "model_catalog": "model catalog",
}


def _build() -> dict[tuple[str, str, int], PayloadSchema]:
    built: dict[tuple[str, str, int], PayloadSchema] = {}
    for capability in DELEGABLE_CAPABILITIES:
        label = _TITLES[capability]
        version = CURRENT_SCHEMA_VERSION
        built[(capability, REQUEST, version)] = PayloadSchema(
            capability=capability,
            direction=REQUEST,
            version=version,
            root=_request_root(capability),
            title=f"{label} request",
        )
        built[(capability, RESPONSE, version)] = PayloadSchema(
            capability=capability,
            direction=RESPONSE,
            version=version,
            root=_response_root(capability, _RESULT_SPECS[capability]),
            title=f"{label} response",
        )
    return built


_SCHEMAS: dict[tuple[str, str, int], PayloadSchema] = _build()


def schema_for(capability: str, direction: str, version: int | None = None) -> PayloadSchema:
    """Return a published schema, defaulting to the current version.

    Raises :class:`UnknownCapability` for a name that is not delegable, and
    ``KeyError`` for a version this build does not publish — a caller cannot get
    a best-effort match, because reading fields whose meaning changed is worse
    than refusing.
    """
    require_delegable(capability)
    if direction not in DIRECTIONS:
        raise ValueError(f"unknown schema direction: {direction!r}")
    resolved = CURRENT_SCHEMA_VERSION if version is None else version
    key = (capability, direction, resolved)
    if key not in _SCHEMAS:
        raise KeyError(
            f"no published {direction} schema version {resolved} for capability {capability!r}"
        )
    return _SCHEMAS[key]


def published_versions(capability: str, direction: str) -> tuple[int, ...]:
    """Every version published for *capability* and *direction*, ascending."""
    require_delegable(capability)
    return tuple(
        sorted(
            version for (name, way, version) in _SCHEMAS if name == capability and way == direction
        )
    )


def published_schemas() -> dict[str, dict[str, Any]]:
    """Every published schema keyed by its identifier, for publication."""
    return {schema.schema_id: schema.json_schema() for schema in _SCHEMAS.values()}


def declared_version(payload: Any) -> int | None:
    """Return the ``schema_version`` a payload claims, or ``None`` when unusable."""
    if not isinstance(payload, Mapping):
        return None
    version = payload.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        return None
    return version


def validate_response(capability: str, payload: Any) -> tuple[SchemaError, ...]:
    """Return every reason *payload* is not a valid response for *capability*.

    Returns rather than raises: an invalid provider response is a condition the
    engine handles by degrading to the builtin, and the reasons are what the
    degradation reports.
    """
    try:
        require_delegable(capability)
    except UnknownCapability:
        return (SchemaError("capability", f"unknown capability: {capability!r}"),)
    version = declared_version(payload)
    if version is None:
        return (SchemaError("schema_version", "expected a positive integer"),)
    try:
        schema = schema_for(capability, RESPONSE, version)
    except KeyError:
        published = ", ".join(str(v) for v in published_versions(capability, RESPONSE))
        return (
            SchemaError(
                "schema_version",
                f"version {version} is not published for this capability (published: {published})",
            ),
        )
    return schema.errors(payload)
