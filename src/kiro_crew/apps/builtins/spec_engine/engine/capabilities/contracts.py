"""The vocabulary and value types every capability call shares.

One request shape, one response envelope, one result. That uniformity is what
lets a caller ask for analysis, a review verdict, or a task implementation
without knowing whether the answer came from the engine, an MCP child, or a
command-line program — and it is why binding a provider changes depth rather
than which calls exist.

Three types here carry more weight than their size suggests.

:class:`Untrusted` wraps the prose a provider authored. It has no ``__str__``,
so provider text cannot slip into an f-string, a log line, or a command template
by looking like a ``str``; a caller that wants the characters asks for them with
:meth:`Untrusted.for_display`, which is a display path and nothing else. The
guarantee the engine owes is that provider output is data — stored, shown, never
executed or read as instructions — and a wrapper type is how that becomes a
property the type checker holds rather than a habit each call site remembers.

The wrapper is not universal, and the exception is deliberate. A handful of
provider fields are short and identifier-shaped — a finding's kind, a criterion
identifier in a coverage list — and the engine compares and routes on them, so
wrapping them would put a display call on the matching path. Those stay plain
strings and are put through :func:`sanitized` where they enter an audit record or
a label. The property is that no provider-authored text reaches a human
unsanitized; where that happens differs by field, and a field that is not wrapped
is not therefore trusted.

:class:`Coverage` makes partial work visible. A provider that examined the
requirements and skipped the design has not analyzed the spec, and a response
carrying only findings cannot say so: no findings then reads as nothing wrong.
Coverage is therefore part of the envelope rather than an optional extra, and
what was skipped is surfaced rather than dropped.

:class:`Degradation` names why a call fell back to the builtin, using the same
stable identifiers the diagnostic reports for those conditions. A refusal and a
diagnostic that describe the same condition in two vocabularies leave the user
correlating them by hand.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..config import DELEGABLE_CAPABILITIES, ENGINE_FLOOR_CAPABILITIES, TRANSPORTS

#: Revision of the native artifact format a request describes. Sent on every
#: request so a provider can refuse a format it does not understand instead of
#: guessing at a document whose rules changed under it.
NATIVE_FORMAT_VERSION = "1"

#: Artifact kinds a request may point a provider at: the three native documents
#: plus the sidecar that records the spec type.
ARTIFACT_KINDS: tuple[str, ...] = ("requirements", "design", "tasks", "config")

#: Transport names, re-exported from the configuration vocabulary so a caller
#: needs one import rather than two to read a binding.
TRANSPORT_BUILTIN = "builtin"
TRANSPORT_MCP = "mcp"
TRANSPORT_COMMAND = "command"

#: Stable identifiers for the conditions that degrade a capability call. The
#: engine quotes these when it marks a run degraded and the diagnostic reports
#: the same strings, so both surfaces name a condition identically.
FINDING_PROVIDER_UNAVAILABLE = "capability.provider_unavailable"
FINDING_PROVIDER_TIMEOUT = "capability.provider_timeout"
FINDING_RESPONSE_INVALID = "capability.response_invalid"
FINDING_BINDING_INVALID = "capability.binding_invalid"
FINDING_ENGINE_FLOOR_BINDING = "capability.engine_floor_binding"

#: Characters kept out of displayed provider text. Control characters and the
#: bidirectional overrides are the two families that let authored text
#: misrepresent itself once rendered: one can rewrite a terminal line, the other
#: can reverse the reading order of what follows it.
_UNDISPLAYABLE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u202a-\u202e\u2066-\u2069]")

#: The same set plus the whitespace controls prose is allowed to keep. A finding
#: kind or a criterion identifier has no line breaks in it, so any that arrive
#: are either a mistake or an attempt to make one audit line look like several.
_UNPRINTABLE_IN_IDENTIFIER = re.compile(
    r"[\x00-\x1f\x7f\u202a-\u202e\u2066-\u2069]"
)

#: Cap on one displayed provider string. Provider output is unbounded input; a
#: surface rendering it needs a ceiling that is not set by the provider.
MAX_DISPLAY_CHARS = 4096

#: Appended when display text was cut, so a reader is never shown a truncated
#: message as though it were complete.
DISPLAY_TRUNCATION_NOTICE = " […]"


class CapabilityError(Exception):
    """Base class for capability-layer errors the engine raises rather than degrades."""


class EngineFloorViolation(CapabilityError):
    """Raised when something tries to bind a capability the engine always executes.

    Refusing loudly is the point. Native-format validation, the phase gates,
    autonomy resolution, budget enforcement, the claim ledger, and the audit log
    are the guarantees the engine exists to make; a binding for one of them that
    were merely ignored would read as accepted, and the operator would believe
    their provider was answering a question the engine had in fact kept.
    """

    def __init__(self, capability: str) -> None:
        self.capability = capability
        self.finding_id = FINDING_ENGINE_FLOOR_BINDING
        super().__init__(
            f"{capability!r} always executes in the engine and cannot be bound to a provider"
        )


class UnknownCapability(CapabilityError):
    """Raised for a capability name that is neither delegable nor engine floor."""

    def __init__(self, capability: str) -> None:
        self.capability = capability
        super().__init__(f"unknown capability: {capability!r}")


def require_delegable(capability: str) -> str:
    """Return *capability* when it may be bound, raising otherwise.

    Every path that resolves, binds, or invokes a capability funnels through
    here, so the engine floor is enforced in one place instead of once per
    caller — a rule restated at each call site is only as strong as the newest
    call site's memory of it.
    """
    if capability in ENGINE_FLOOR_CAPABILITIES:
        raise EngineFloorViolation(capability)
    if capability not in DELEGABLE_CAPABILITIES:
        raise UnknownCapability(capability)
    return capability


@dataclass(frozen=True)
class Untrusted:
    """A string an external provider authored.

    Deliberately not a ``str`` subclass and deliberately without ``__str__``:
    the point is that this value cannot be mistaken for engine text on the way
    into a log line, a command template, or a prompt. Ask for
    :meth:`for_display` to render it.
    """

    text: str

    def for_display(self, *, limit: int = MAX_DISPLAY_CHARS) -> str:
        """Return the text with undisplayable characters removed and a length cap."""
        cleaned = _UNDISPLAYABLE.sub("", self.text)
        if len(cleaned) <= limit:
            return cleaned
        return cleaned[:limit] + DISPLAY_TRUNCATION_NOTICE

    def __repr__(self) -> str:  # pragma: no cover - display only
        return f"Untrusted({self.for_display(limit=64)!r})"


def sanitized(text: str, *, limit: int = MAX_DISPLAY_CHARS) -> str:
    """Render provider-authored text that is not carried in :class:`Untrusted`.

    A few provider fields are short and identifier-shaped -- a finding's kind, a
    criterion identifier in a coverage list -- and are compared and routed on as
    plain strings rather than wrapped, because wrapping a value the engine
    matches against would put ``for_display`` on the matching path.

    The schema constrains them to non-empty strings and nothing more, so their
    contents are still whatever a provider sent. They pass through this on the
    way into an audit record or a label, which is the same treatment
    :meth:`Untrusted.for_display` gives, so being unwrapped changes where the
    sanitizing happens and not whether it happens.

    Stricter than the prose path in one respect: line breaks and tabs go too.
    :meth:`Untrusted.for_display` keeps them because prose legitimately contains
    them, but an identifier does not, and a carriage return is how a value
    overwrites the line printed before it in a terminal reading the log.
    """
    cleaned = _UNPRINTABLE_IN_IDENTIFIER.sub("", text)
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[:limit] + DISPLAY_TRUNCATION_NOTICE


class FindingSeverity(str, Enum):
    """How much a provider finding claims to cost.

    A provider's severity never decides a gate: gates read engine findings only.
    It orders a display and tells a human what the provider thought it had
    found.
    """

    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


#: Serialized severity vocabulary, for the published schemas.
FINDING_SEVERITIES: tuple[str, ...] = tuple(level.value for level in FindingSeverity)


class ProviderKind(str, Enum):
    """Whether a provider ships with the app or was bound by configuration."""

    BUILTIN = "builtin"
    EXTERNAL = "external"


class ProviderNature(str, Enum):
    """Whether a provider computes its answer or asks a model for one.

    Surfaced because the two carry different claims: a deterministic pass means
    the checks it runs found nothing, while a model-backed pass means a model
    reported nothing. Showing them identically invites reading the first as the
    second.
    """

    DETERMINISTIC = "deterministic"
    MODEL_BACKED = "model_backed"


@dataclass(frozen=True)
class ProviderIdentity:
    """Who served a capability call, and how it was reached."""

    name: str
    kind: ProviderKind
    nature: ProviderNature
    transport: str
    #: Version the provider declared for itself, empty when it declared none.
    version: str = ""

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("a provider identity needs a name")
        if self.transport not in TRANSPORTS:
            raise ValueError(f"unknown transport: {self.transport!r}")

    @property
    def external(self) -> bool:
        return self.kind is ProviderKind.EXTERNAL

    def to_json_object(self) -> dict[str, Any]:
        record: dict[str, Any] = {
            "name": self.name,
            "kind": self.kind.value,
            "nature": self.nature.value,
            "transport": self.transport,
        }
        if self.version:
            record["version"] = self.version
        return record


@dataclass(frozen=True)
class ArtifactRef:
    """Where one artifact lives, and which revision the request refers to.

    The revision is carried so a decision taken on a provider's answer can be
    checked against the bytes that answer was about. A document edited between
    the request and the use of its findings is a different document, and nothing
    in the findings themselves says so.
    """

    kind: str
    path: str
    revision: str = ""

    def __post_init__(self) -> None:
        if self.kind not in ARTIFACT_KINDS:
            raise ValueError(f"unknown artifact kind: {self.kind!r}")
        if not self.path.strip():
            raise ValueError(f"artifact {self.kind!r} needs a path")

    @classmethod
    def of(cls, kind: str, path: str | Path, *, revision: str = "") -> "ArtifactRef":
        """Build a ref with *path* normalised to an absolute posix path.

        Absolute because a provider runs as its own process with its own working
        directory; a relative path would resolve against whatever that happened
        to be.
        """
        return cls(kind=kind, path=Path(path).expanduser().resolve().as_posix(), revision=revision)

    def to_json_object(self) -> dict[str, Any]:
        record: dict[str, Any] = {"kind": self.kind, "path": self.path}
        if self.revision:
            record["revision"] = self.revision
        return record


@dataclass(frozen=True)
class CapabilityRequest:
    """One capability call, in the shape every transport carries it.

    Artifact locations, the spec type, and the format version travel on every
    request: a provider that has to infer any of the three is guessing about the
    document it was asked to judge.
    """

    capability: str
    spec_type: str
    artifacts: tuple[ArtifactRef, ...] = ()
    parameters: Mapping[str, Any] = field(default_factory=dict)
    format_version: str = NATIVE_FORMAT_VERSION
    #: Run this call belongs to, so a declared cost lands on the right budget.
    run: str = ""
    #: Wall-clock ceiling the engine applies to the whole call.
    deadline_s: int = 0
    schema_version: int = 1

    def __post_init__(self) -> None:
        require_delegable(self.capability)
        if self.deadline_s < 0:
            raise ValueError("a deadline cannot be negative")

    def to_wire(self) -> dict[str, Any]:
        """Return the JSON object a transport sends."""
        return {
            "schema_version": self.schema_version,
            "capability": self.capability,
            "spec_type": self.spec_type,
            "format_version": self.format_version,
            "run": self.run,
            "deadline_s": self.deadline_s,
            "artifacts": [artifact.to_json_object() for artifact in self.artifacts],
            "parameters": dict(self.parameters),
        }

    def artifact(self, kind: str) -> ArtifactRef | None:
        """Return the referenced artifact of *kind*, or ``None`` when absent."""
        for artifact in self.artifacts:
            if artifact.kind == kind:
                return artifact
        return None

    @property
    def artifact_kinds(self) -> tuple[str, ...]:
        return tuple(artifact.kind for artifact in self.artifacts)


@dataclass(frozen=True)
class SkippedItem:
    """One thing a provider declared it did not process, and why."""

    item: str
    reason: Untrusted

    def to_json_object(self) -> dict[str, Any]:
        return {"item": sanitized(self.item), "reason": self.reason.for_display()}


@dataclass(frozen=True)
class Coverage:
    """What a provider declared it processed, and what it left out."""

    processed: tuple[str, ...] = ()
    skipped: tuple[SkippedItem, ...] = ()

    @property
    def complete(self) -> bool:
        """Whether the provider declared nothing skipped."""
        return not self.skipped

    def to_json_object(self) -> dict[str, Any]:
        return {
            "processed": [sanitized(entry) for entry in self.processed],
            "skipped": [item.to_json_object() for item in self.skipped],
        }


@dataclass(frozen=True)
class ClarifyingQuestion:
    """A decision a provider wants a human to make, with the options it saw."""

    question: Untrusted
    choices: tuple[Untrusted, ...] = ()
    consequences: tuple[Untrusted, ...] = ()
    recommended: Untrusted | None = None

    def to_json_object(self) -> dict[str, Any]:
        record: dict[str, Any] = {
            "question": self.question.for_display(),
            "choices": [choice.for_display() for choice in self.choices],
            "consequences": [item.for_display() for item in self.consequences],
        }
        if self.recommended is not None:
            record["recommended"] = self.recommended.for_display()
        return record


@dataclass(frozen=True)
class ProviderFinding:
    """One thing a provider reported.

    ``refs`` names the acceptance criteria or tasks the finding concerns, which
    is what lets the engine route a finding mechanically instead of handing a
    human a wall of prose. The message and the question are provider-authored,
    so both are :class:`Untrusted`.
    """

    kind: str
    severity: FindingSeverity
    message: Untrusted
    refs: tuple[str, ...] = ()
    question: ClarifyingQuestion | None = None

    def to_json_object(self) -> dict[str, Any]:
        record: dict[str, Any] = {
            "kind": sanitized(self.kind),
            "severity": self.severity.value,
            "message": self.message.for_display(),
            "refs": list(self.refs),
        }
        if self.question is not None:
            record["question"] = self.question.to_json_object()
        return record


@dataclass(frozen=True)
class CapabilityResponse:
    """What a provider answered, after its response passed schema validation."""

    capability: str
    provider_name: str
    coverage: Coverage = field(default_factory=Coverage)
    findings: tuple[ProviderFinding, ...] = ()
    #: Credits the provider declared it spent. Attributed to the run's budget.
    cost_credits: float = 0.0
    #: Capability-specific body, validated against that capability's schema.
    result: Mapping[str, Any] = field(default_factory=dict)
    provider_version: str = ""
    schema_version: int = 1

    def findings_for(self, severity: FindingSeverity) -> tuple[ProviderFinding, ...]:
        return tuple(finding for finding in self.findings if finding.severity is severity)


@dataclass(frozen=True)
class Degradation:
    """Why a capability call fell back to the builtin."""

    finding_id: str
    #: Engine-authored explanation. Never provider text: this string is quoted in
    #: refusals and notifications, which is not a place attacker-authored text
    #: belongs.
    reason: str
    #: Transport that failed, so an operator knows which binding to look at.
    transport: str
    #: Provider-authored detail, when the failure produced any.
    detail: Untrusted | None = None

    def to_json_object(self) -> dict[str, Any]:
        record: dict[str, Any] = {
            "finding": self.finding_id,
            "reason": self.reason,
            "transport": self.transport,
        }
        if self.detail is not None:
            record["detail"] = self.detail.for_display()
        return record


@dataclass(frozen=True)
class CapabilityResult:
    """The engine's answer for one capability call, whoever served it.

    Identical in shape whether the builtin answered, an MCP child answered, or a
    command answered and then a fallback took over, so a caller reads one type
    and a surface renders one panel.
    """

    request: CapabilityRequest
    provider: ProviderIdentity
    response: CapabilityResponse
    duration_s: float = 0.0
    degradation: Degradation | None = None
    #: Binding the engine resolved before any fallback, for reporting which
    #: provider was configured versus which one answered.
    configured_transport: str = TRANSPORT_BUILTIN
    configured_provider: str = ""

    @property
    def degraded(self) -> bool:
        return self.degradation is not None

    @property
    def capability(self) -> str:
        return self.request.capability

    @property
    def coverage(self) -> Coverage:
        return self.response.coverage

    @property
    def findings(self) -> tuple[ProviderFinding, ...]:
        return self.response.findings

    @property
    def cost_credits(self) -> float:
        return self.response.cost_credits

    def audit_detail(self) -> dict[str, Any]:
        """The record written to the run's audit log for this call.

        Carries provider identity, transport, declared coverage, and degraded
        status: the four things that decide whether an answer meant what a reader
        assumed it meant.
        """
        detail: dict[str, Any] = {
            "capability": self.capability,
            "provider": self.provider.to_json_object(),
            "transport": self.provider.transport,
            "configured_transport": self.configured_transport,
            "coverage": self.coverage.to_json_object(),
            "degraded": self.degraded,
            "findings": len(self.findings),
            "duration_s": round(self.duration_s, 3),
        }
        if self.configured_provider:
            detail["configured_provider"] = self.configured_provider
        if self.degradation is not None:
            detail["degradation"] = self.degradation.to_json_object()
        return detail


def untrusted_all(values: Sequence[Any]) -> tuple[Untrusted, ...]:
    """Wrap every element of a decoded JSON string list as provider-authored."""
    return tuple(Untrusted(str(value)) for value in values)
