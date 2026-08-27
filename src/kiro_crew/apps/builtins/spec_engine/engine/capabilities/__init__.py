"""Capability providers: what the app delegates, and what it never will.

The app is a host. Analysis, document authoring, review verdicts, task
implementation, supplementary validation rules, watch sources, and the model
catalog are all **delegable**: each resolves from configuration to one of three
transports — ``builtin`` (in-process, ships with the app), ``mcp`` (an MCP server
invoked as a child process), or ``command`` (a program handed structured input
whose structured output is parsed back) — and every one of them ships a working
builtin, so nothing is ever absent or answers "not configured".

The **engine floor** is the other half of the same design, and the half that
makes the first half safe. Native-format validation, the phase gates, autonomy
resolution, budget enforcement, the claim ledger, and the audit log always
execute in the engine and cannot be bound at all. Without that line, delegation
would quietly undo rules-as-code: whoever configured the most permissive provider
would decide what the engine guarantees. So a configuration naming an
engine-floor capability is an error rather than an ignored key, and a
supplementary validation provider may only *add* findings — never suppress,
downgrade, or override an engine finding or a gate.

Everything else in this package follows from those two sentences:

* :mod:`.contracts` — the request, response, and result types every transport
  shares, plus :class:`~.contracts.Untrusted`, which is how "provider output is
  data, never instructions" becomes something the type checker holds.
* :mod:`.schemas` — the published, versioned request and response schema per
  capability, and the validator that runs on every response before a finding is
  recorded.
* :mod:`.transports` — the two external transports, each spawning its child
  through the package's sandbox chokepoint under one wall-clock deadline.
* :mod:`.providers` — the builtin every capability answers from.
* :mod:`.registry` — the one resolution and invocation path, where the engine
  floor is enforced, an unusable provider degrades to the builtin instead of
  blocking the run, declared cost reaches the budget, and provider identity,
  transport, coverage, and degraded status reach the audit log.
* :mod:`.supplementary` — the additive-only seam for supplementary validation.
* :mod:`.fixtures` and :mod:`.conformance` — the bundled fixtures and the runner
  that judges a candidate provider against them, so the extension point comes
  with an executable check rather than a description of one.

No provider implementation is bundled here beyond the app's own builtins. An
external provider is named by configuration — a command and an environment — and
its code lives wherever its author put it, which is what keeps the app complete
on its own and free of anything it did not write.
"""

from __future__ import annotations

from .builtins import (
    AUTHORING_PROVIDER,
    IMPLEMENTATION_PROVIDER,
    MODEL_CATALOG_PROVIDER,
    REVIEW_PROVIDER,
    HostModelCatalog,
    ModelResolver,
    register_builtins,
)
from .conformance import (
    CHECK_CLASSES,
    CHECK_DECLARED_COVERAGE,
    CHECK_PLANTED_DEFECT,
    CHECK_REPEATABILITY,
    CHECK_SCHEMA_VALIDITY,
    CHECK_TIMEOUT_HONORING,
    DEFAULT_DEADLINE_S,
    DEFAULT_GRACE_S,
    SCHEMA_VIOLATION_CHARS,
    BuiltinCandidate,
    Candidate,
    CheckResult,
    ConformanceReport,
    ConformanceRunner,
    TransportCandidate,
    suite_for,
    verify,
    verify_builtin,
)
from .contracts import (
    ARTIFACT_KINDS,
    FINDING_BINDING_INVALID,
    FINDING_ENGINE_FLOOR_BINDING,
    FINDING_PROVIDER_TIMEOUT,
    FINDING_PROVIDER_UNAVAILABLE,
    FINDING_RESPONSE_INVALID,
    FINDING_SEVERITIES,
    MAX_DISPLAY_CHARS,
    NATIVE_FORMAT_VERSION,
    TRANSPORT_BUILTIN,
    TRANSPORT_COMMAND,
    TRANSPORT_MCP,
    ArtifactRef,
    CapabilityError,
    CapabilityRequest,
    CapabilityResponse,
    CapabilityResult,
    ClarifyingQuestion,
    Coverage,
    Degradation,
    EngineFloorViolation,
    FindingSeverity,
    ProviderFinding,
    ProviderIdentity,
    ProviderKind,
    ProviderNature,
    SkippedItem,
    UnknownCapability,
    Untrusted,
    require_delegable,
    sanitized,
)
from .fixtures import (
    DEFECT_REPORTING_CAPABILITIES,
    DOCUMENT_CAPABILITIES,
    FIXTURE_CONTRADICTORY_CRITERIA,
    FIXTURE_COVERAGE_HOLE,
    FIXTURE_MALFORMED_RESPONSE,
    FIXTURE_MINIMAL_REQUEST,
    FIXTURE_OVERSIZED_DOCUMENT,
    FIXTURE_PLANTED_AMBIGUITY,
    OVERSIZED_MIN_CHARS,
    ConformanceFixture,
    PlantedDefect,
    oversized_requirements,
)
from .providers import (
    BuiltinProvider,
    DeclaredSkipProvider,
    builtin_identity,
    default_builtins,
)
from .registry import (
    AUDIT_EVENT_CAPABILITY,
    CAPABILITY_TIMEOUT_SETTING,
    AuditSink,
    Binding,
    CapabilityRegistry,
    CostSink,
    RecordingCostSink,
    builtin_binding,
    external_identity,
    resolve_bindings,
    response_from_payload,
    transport_for,
)
from .schemas import (
    CURRENT_SCHEMA_VERSION,
    REQUEST,
    RESPONSE,
    PayloadSchema,
    SchemaError,
    SchemaViolation,
    published_schemas,
    published_versions,
    schema_for,
    validate_response,
)
from .supplementary import (
    DisplayEntry,
    EntryOrigin,
    SupplementaryFinding,
    SupplementedReport,
    blocking_rules,
    engine_severities,
    supplement,
)
from .transports import (
    MAX_OUTPUT_CHARS,
    MCP_PROTOCOL_VERSION,
    MCP_TOOL_PREFIX,
    CapabilityTransport,
    ChildOutcome,
    ChildRunner,
    CommandProviderTransport,
    McpProviderTransport,
    TransportFailure,
    run_provider_child,
)

__all__ = [
    "ARTIFACT_KINDS",
    "AUDIT_EVENT_CAPABILITY",
    "AUTHORING_PROVIDER",
    "CAPABILITY_TIMEOUT_SETTING",
    "CHECK_CLASSES",
    "CHECK_DECLARED_COVERAGE",
    "CHECK_PLANTED_DEFECT",
    "CHECK_REPEATABILITY",
    "CHECK_SCHEMA_VALIDITY",
    "CHECK_TIMEOUT_HONORING",
    "CURRENT_SCHEMA_VERSION",
    "DEFAULT_DEADLINE_S",
    "DEFAULT_GRACE_S",
    "DEFECT_REPORTING_CAPABILITIES",
    "DOCUMENT_CAPABILITIES",
    "FINDING_BINDING_INVALID",
    "FINDING_ENGINE_FLOOR_BINDING",
    "FINDING_PROVIDER_TIMEOUT",
    "FINDING_PROVIDER_UNAVAILABLE",
    "FINDING_RESPONSE_INVALID",
    "FINDING_SEVERITIES",
    "FIXTURE_CONTRADICTORY_CRITERIA",
    "FIXTURE_COVERAGE_HOLE",
    "FIXTURE_MALFORMED_RESPONSE",
    "FIXTURE_MINIMAL_REQUEST",
    "FIXTURE_OVERSIZED_DOCUMENT",
    "FIXTURE_PLANTED_AMBIGUITY",
    "IMPLEMENTATION_PROVIDER",
    "MAX_DISPLAY_CHARS",
    "MAX_OUTPUT_CHARS",
    "MCP_PROTOCOL_VERSION",
    "MCP_TOOL_PREFIX",
    "MODEL_CATALOG_PROVIDER",
    "NATIVE_FORMAT_VERSION",
    "OVERSIZED_MIN_CHARS",
    "REQUEST",
    "RESPONSE",
    "REVIEW_PROVIDER",
    "SCHEMA_VIOLATION_CHARS",
    "TRANSPORT_BUILTIN",
    "TRANSPORT_COMMAND",
    "TRANSPORT_MCP",
    "ArtifactRef",
    "AuditSink",
    "Binding",
    "BuiltinCandidate",
    "BuiltinProvider",
    "Candidate",
    "CapabilityError",
    "CapabilityRegistry",
    "CapabilityRequest",
    "CapabilityResponse",
    "CapabilityResult",
    "CapabilityTransport",
    "CheckResult",
    "ChildOutcome",
    "ChildRunner",
    "ClarifyingQuestion",
    "CommandProviderTransport",
    "ConformanceFixture",
    "ConformanceReport",
    "ConformanceRunner",
    "CostSink",
    "Coverage",
    "DeclaredSkipProvider",
    "Degradation",
    "DisplayEntry",
    "EngineFloorViolation",
    "EntryOrigin",
    "FindingSeverity",
    "HostModelCatalog",
    "McpProviderTransport",
    "ModelResolver",
    "PayloadSchema",
    "PlantedDefect",
    "ProviderFinding",
    "ProviderIdentity",
    "ProviderKind",
    "ProviderNature",
    "RecordingCostSink",
    "SchemaError",
    "SchemaViolation",
    "SkippedItem",
    "SupplementaryFinding",
    "SupplementedReport",
    "TransportCandidate",
    "TransportFailure",
    "UnknownCapability",
    "Untrusted",
    "blocking_rules",
    "builtin_binding",
    "builtin_identity",
    "default_builtins",
    "engine_severities",
    "external_identity",
    "oversized_requirements",
    "published_schemas",
    "published_versions",
    "register_builtins",
    "require_delegable",
    "resolve_bindings",
    "response_from_payload",
    "run_provider_child",
    "sanitized",
    "schema_for",
    "suite_for",
    "supplement",
    "transport_for",
    "validate_response",
    "verify",
    "verify_builtin",
]
