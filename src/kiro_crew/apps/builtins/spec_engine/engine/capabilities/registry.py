"""The provider registry: one resolution path, one invocation path, one result.

Everything the app delegates comes through here, and the shape of the call does
not change with what is bound. That is the property the whole extension point
rests on: a caller asks for a capability, and whether the engine's own code, an
MCP child, or a command-line program answers is a configuration fact recorded in
the result rather than a branch in the caller.

Four rules are enforced here and nowhere else, because a rule restated per caller
is only as strong as the newest caller.

**The engine floor is not bindable.** Native-format validation, the phase gates,
autonomy resolution, budget enforcement, the claim ledger, and the audit log
always execute in the engine. A configuration naming one of them is refused with
:class:`~.contracts.EngineFloorViolation` at resolution, on top of the config
schema's own refusal at the write path — because a hand-edited document never
passed the write path, and a binding that is merely ignored reads as accepted.

**Degrade, do not block.** An unavailable provider, one that misses its deadline,
or one whose response fails its published schema falls back to that capability's
builtin, marks the result degraded with the stable finding identifier for the
condition, and returns. A broken external provider costs depth, not the run. The
only failure that propagates is a builtin raising, which is a bug in this
package rather than a provider being unreachable, and a bug earns a traceback.

**Every response is validated before it is used.** Not "when a validator is
available" and not "unless it looks fine": a response that skipped validation is
one the engine can make no claim about, and its findings would be recorded as if
it had.

**Declared cost lands on the run's budget.** A provider that reports spend is
reporting spend the run caused, so it is attributed through the cost sink rather
than displayed and forgotten.

The audit record carries provider identity, transport, declared coverage, and
degraded status. Those four are what decide whether an answer meant what its
reader assumed: the same empty findings list means one thing from a semantic pass
over every document and another from a provider that skipped two of them.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol

from ..config import (
    DELEGABLE_CAPABILITIES,
    ENGINE_FLOOR_CAPABILITIES,
    TRANSPORTS,
    ConfigError,
    ConfigStore,
    ConfigValidationError,
)
from ..config.schema import SECTION_CAPABILITIES
from ..state import SpecRef
from .contracts import (
    FINDING_BINDING_INVALID,
    FINDING_RESPONSE_INVALID,
    TRANSPORT_BUILTIN,
    TRANSPORT_COMMAND,
    TRANSPORT_MCP,
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
    Untrusted,
    require_delegable,
    untrusted_all,
)
from .providers import BuiltinProvider, default_builtins
from .schemas import (
    REQUEST,
    SchemaError,
    schema_for,
    validate_response,
)
from .transports import (
    CapabilityTransport,
    CommandProviderTransport,
    McpProviderTransport,
    TransportFailure,
)

logger = logging.getLogger(__name__)

#: Setting holding the default wall-clock ceiling for a delegated capability call.
CAPABILITY_TIMEOUT_SETTING = "timeouts.capability_s"

#: Audit event name for a completed capability call.
AUDIT_EVENT_CAPABILITY = "capability.invoked"


class CostSink(Protocol):
    """Where a provider's declared cost is attributed.

    A narrow seam on purpose: budget enforcement is engine floor and lives in its
    own module, so the registry reports spend rather than deciding what to do
    about it.
    """

    def attribute(self, *, run: str, capability: str, provider: str, credits: float) -> None: ...


@dataclass
class RecordingCostSink:
    """Accumulates declared cost per run. The default when no budget is wired in.

    Keeping a record rather than discarding it matters even without enforcement:
    a run whose cost was never attributed cannot be reconciled afterwards, and
    "we did not have a sink at the time" is not an answer to where the credits
    went.
    """

    attributed: list[dict[str, Any]] = field(default_factory=list)

    def attribute(self, *, run: str, capability: str, provider: str, credits: float) -> None:
        if credits <= 0:
            return
        self.attributed.append(
            {"run": run, "capability": capability, "provider": provider, "credits": credits}
        )

    def total_for(self, run: str) -> float:
        return sum(float(entry["credits"]) for entry in self.attributed if entry["run"] == run)


class AuditSink(Protocol):
    """Where a completed capability call is recorded."""

    def append(
        self,
        ref: SpecRef,
        event: str,
        *,
        run: str | None = None,
        initiator: str | None = None,
        detail: dict[str, Any] | None = None,
        cost: float | None = None,
    ) -> Any: ...


@dataclass(frozen=True)
class Binding:
    """How one capability is reached, and which configuration layer said so."""

    capability: str
    transport: str
    argv: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)
    #: Per-binding override of the capability timeout, zero when unset.
    timeout_s: int = 0
    #: Dotted configuration path of the declaration, empty for the default.
    declared_at: str = ""

    @property
    def is_builtin(self) -> bool:
        return self.transport == TRANSPORT_BUILTIN

    @property
    def configured(self) -> bool:
        """Whether an operator declared this binding rather than it defaulting."""
        return bool(self.declared_at)

    @property
    def program(self) -> str:
        """The program an external binding runs, empty for the builtin."""
        return self.argv[0] if self.argv else ""


def builtin_binding(capability: str) -> Binding:
    """The binding a capability resolves to when configuration names none."""
    return Binding(capability=capability, transport=TRANSPORT_BUILTIN)


def resolve_bindings(store: ConfigStore) -> dict[str, Binding]:
    """Resolve every delegable capability's binding from configuration.

    An absent entry resolves to the builtin rather than failing: a capability
    nobody configured still has to answer. An entry naming an engine-floor
    capability raises, and an entry that is structurally unusable raises with the
    configuration path, so an operator is told where the problem is instead of
    watching a call quietly take the builtin path.
    """
    document = store.document()
    section = document.get(SECTION_CAPABILITIES)
    bindings = {capability: builtin_binding(capability) for capability in DELEGABLE_CAPABILITIES}
    if section is None:
        return bindings
    if not isinstance(section, Mapping):
        raise ConfigValidationError(
            [ConfigError(SECTION_CAPABILITIES, "expected an object keyed by capability")]
        )
    for name, entry in section.items():
        path = f"{SECTION_CAPABILITIES}.{name}"
        if name in ENGINE_FLOOR_CAPABILITIES:
            raise EngineFloorViolation(str(name))
        if name not in DELEGABLE_CAPABILITIES:
            raise ConfigValidationError([ConfigError(path, "unknown capability")])
        bindings[name] = _binding_from(str(name), entry, path)
    return bindings


def _binding_from(capability: str, entry: Any, path: str) -> Binding:
    if not isinstance(entry, Mapping):
        raise ConfigValidationError([ConfigError(path, "expected an object")])
    transport = entry.get("transport")
    if transport not in TRANSPORTS:
        raise ConfigValidationError(
            [ConfigError(f"{path}.transport", "expected one of: " + ", ".join(TRANSPORTS))]
        )
    argv = _argv_from(
        entry.get("command"),
        f"{path}.command",
        required=transport != TRANSPORT_BUILTIN,
    )
    env_node = entry.get("env", {})
    if not isinstance(env_node, Mapping):
        raise ConfigValidationError([ConfigError(f"{path}.env", "expected an object of strings")])
    env = {str(key): str(value) for key, value in env_node.items()}
    timeout_raw = entry.get("timeout_s", 0)
    if isinstance(timeout_raw, bool) or not isinstance(timeout_raw, int) or timeout_raw < 0:
        raise ConfigValidationError(
            [ConfigError(f"{path}.timeout_s", "expected a positive integer")]
        )
    return Binding(
        capability=capability,
        transport=str(transport),
        argv=argv,
        env=env,
        timeout_s=timeout_raw,
        declared_at=path,
    )


def _argv_from(node: Any, path: str, *, required: bool) -> tuple[str, ...]:
    if node is None:
        if required:
            raise ConfigValidationError(
                [ConfigError(path, "this transport requires a command to run")]
            )
        return ()
    if isinstance(node, (str, bytes)) or not isinstance(node, (list, tuple)):
        raise ConfigValidationError([ConfigError(path, "expected a list of arguments")])
    if not node:
        raise ConfigValidationError([ConfigError(path, "expected at least one argument")])
    argv: list[str] = []
    for index, argument in enumerate(node):
        if not isinstance(argument, str) or not argument:
            raise ConfigValidationError(
                [ConfigError(f"{path}[{index}]", "expected a non-empty string")]
            )
        argv.append(argument)
    return tuple(argv)


class CapabilityRegistry:
    """Resolves and invokes capability providers behind one call.

    Constructing a registry asserts that every delegable capability has a
    builtin. That check is here rather than at each call site because the
    guarantee it protects — no capability is ever absent or answers "not
    configured" — has to hold before the first request, not on the request that
    happens to hit the gap.
    """

    def __init__(
        self,
        store: ConfigStore,
        *,
        builtins: Mapping[str, BuiltinProvider] | None = None,
        transports: Mapping[str, CapabilityTransport] | None = None,
        project: str | None = None,
        audit: AuditSink | None = None,
        cost_sink: CostSink | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._store = store
        self._project = project
        self._audit = audit
        self._cost_sink: CostSink = cost_sink if cost_sink is not None else RecordingCostSink()
        self._clock = clock
        table = dict(default_builtins())
        if builtins:
            for name, provider in builtins.items():
                require_delegable(name)
                table[name] = provider
        missing = [name for name in DELEGABLE_CAPABILITIES if name not in table]
        if missing:
            raise ValueError(
                "every delegable capability needs a builtin provider; missing: "
                + ", ".join(sorted(missing))
            )
        self._builtins = table
        self._transports = dict(transports or {})

    @property
    def capabilities(self) -> tuple[str, ...]:
        """Every capability this registry serves. Constant, whatever is bound."""
        return DELEGABLE_CAPABILITIES

    @property
    def cost_sink(self) -> CostSink:
        return self._cost_sink

    def register_builtin(self, capability: str, provider: BuiltinProvider) -> None:
        """Replace the builtin serving *capability*.

        How a deeper builtin — a structural analyzer, a review turn, the host
        model catalog — takes over from the shipped default. Refuses an
        engine-floor name for the same reason configuration does.
        """
        require_delegable(capability)
        self._builtins[capability] = provider

    def builtin(self, capability: str) -> BuiltinProvider:
        """The builtin serving *capability*."""
        require_delegable(capability)
        return self._builtins[capability]

    def bindings(self) -> dict[str, Binding]:
        """Every capability's resolved binding, keyed by capability."""
        return resolve_bindings(self._store)

    def binding(self, capability: str) -> Binding:
        """The binding in force for *capability*."""
        require_delegable(capability)
        return self.bindings()[capability]

    def timeout_for(self, binding: Binding) -> int:
        """The wall-clock ceiling for one call under *binding*.

        A per-binding override wins over the configured setting; a provider whose
        work is genuinely slower than the default is a reason to raise its own
        ceiling, not everyone's.
        """
        if binding.timeout_s > 0:
            return binding.timeout_s
        return int(self._store.effective(CAPABILITY_TIMEOUT_SETTING, project=self._project).value)

    def describe(self) -> tuple[dict[str, Any], ...]:
        """Per-capability provider description for a configuration surface.

        Reports which provider serves each capability, whether it is builtin or
        external, and for a builtin whether it computes its answer or asks a
        model for one — the second distinction being the difference between "the
        checks found nothing" and "a model reported nothing".
        """
        described: list[dict[str, Any]] = []
        resolved = self.bindings()
        for capability in DELEGABLE_CAPABILITIES:
            binding = resolved[capability]
            builtin = self._builtins[capability]
            identity = (
                builtin.identity
                if binding.is_builtin
                else external_identity(binding.program, binding.transport)
            )
            described.append(
                {
                    "capability": capability,
                    "transport": binding.transport,
                    "provider": identity.to_json_object(),
                    "configured": binding.configured,
                    "declared_at": binding.declared_at,
                    "timeout_s": self.timeout_for(binding),
                }
            )
        return tuple(described)

    # --- the one invocation path -------------------------------------------

    def invoke(
        self,
        request: CapabilityRequest,
        *,
        ref: SpecRef | None = None,
        initiator: str | None = None,
    ) -> CapabilityResult:
        """Serve *request* through whichever provider is bound, and record it.

        Never raises for an external provider's failure: unavailability, a missed
        deadline, and an invalid response are all degradations that fall back to
        the builtin and return. It does raise for an engine-side mistake — an
        engine-floor capability, a request that does not satisfy its own published
        schema, or configuration that cannot be read — because those are bugs or
        operator errors that a fallback would hide.
        """
        require_delegable(request.capability)
        schema_for(request.capability, REQUEST, request.schema_version).validate(request.to_wire())
        binding = self.binding(request.capability)
        if binding.is_builtin:
            started = self._clock()
            response = self._serve_builtin(request)
            result = CapabilityResult(
                request=request,
                provider=self._builtins[request.capability].identity,
                response=response,
                duration_s=self._clock() - started,
                configured_transport=binding.transport,
                configured_provider=binding.program,
            )
        else:
            result = self._try_external(request, binding)
        return self._finish(result, ref=ref, initiator=initiator)

    def _try_external(self, request: CapabilityRequest, binding: Binding) -> CapabilityResult:
        """Call an external provider, falling back to the builtin on any failure."""
        transport = self._transport_for(binding)
        timeout_s = self.timeout_for(binding)
        deadlined = CapabilityRequest(
            capability=request.capability,
            spec_type=request.spec_type,
            artifacts=request.artifacts,
            parameters=request.parameters,
            format_version=request.format_version,
            run=request.run,
            deadline_s=timeout_s,
            schema_version=request.schema_version,
        )
        started = self._clock()
        if transport is None:
            failure = Degradation(
                finding_id=FINDING_BINDING_INVALID,
                reason=(
                    f"the {binding.transport!r} transport bound to {binding.capability!r} "
                    "cannot be used as configured"
                ),
                transport=binding.transport,
            )
        else:
            try:
                payload = transport.invoke(deadlined, timeout_s=timeout_s)
            except TransportFailure as exc:
                failure = Degradation(
                    finding_id=exc.finding_id,
                    reason=_failure_reason(binding, exc),
                    transport=binding.transport,
                    detail=Untrusted(exc.detail) if exc.detail else None,
                )
            except Exception as exc:  # noqa: BLE001 - a provider must not fail the run
                # Broad on purpose. This boundary exists so that whatever an
                # external provider or its transport does — an unexpected OS
                # error, a decoder blowing up on its output — costs depth rather
                # than the run.
                logger.warning(
                    "capability %s provider raised over the %s transport: %s",
                    binding.capability,
                    binding.transport,
                    exc.__class__.__name__,
                )
                failure = Degradation(
                    finding_id=FINDING_BINDING_INVALID,
                    reason=(
                        f"the provider bound to {binding.capability!r} failed with "
                        f"{exc.__class__.__name__}"
                    ),
                    transport=binding.transport,
                )
            else:
                # Validation happens on whatever came back, including ``None``:
                # branching on the payload's truthiness first would leave one
                # shape of answer unexamined, and it would be the shape a
                # misbehaving provider is most likely to produce.
                errors = validate_response(request.capability, payload)
                if errors:
                    failure = Degradation(
                        finding_id=FINDING_RESPONSE_INVALID,
                        reason=_schema_reason(binding, errors),
                        transport=binding.transport,
                    )
                else:
                    response = response_from_payload(request.capability, payload)
                    return CapabilityResult(
                        request=deadlined,
                        provider=external_identity(
                            binding.program,
                            binding.transport,
                            declared=response.provider_name,
                            version=response.provider_version,
                        ),
                        response=response,
                        duration_s=self._clock() - started,
                        configured_transport=binding.transport,
                        configured_provider=binding.program,
                    )

        fallback = self._serve_builtin(request)
        return CapabilityResult(
            request=request,
            provider=self._builtins[request.capability].identity,
            response=fallback,
            duration_s=self._clock() - started,
            degradation=failure,
            configured_transport=binding.transport,
            configured_provider=binding.program,
        )

    def _serve_builtin(self, request: CapabilityRequest) -> CapabilityResponse:
        """Run the builtin and hold it to the same published response schema.

        The builtin is engine code, which is exactly why it is checked: an
        unvalidated fallback would be the one answer in the system nobody
        verified, and it is the answer used whenever an external provider breaks.
        """
        provider = self._builtins[request.capability]
        response = provider.serve(request)
        errors = validate_response(request.capability, _payload_from_response(response))
        if errors:
            raise ConfigValidationError(
                [
                    ConfigError(
                        f"builtin.{request.capability}",
                        "the builtin provider returned a response that fails its own "
                        "published schema: " + "; ".join(str(error) for error in errors),
                    )
                ]
            )
        return response

    def _transport_for(self, binding: Binding) -> CapabilityTransport | None:
        """Build the transport for *binding*, or ``None`` when it is unusable."""
        injected = self._transports.get(binding.transport)
        if injected is not None:
            return injected
        if not binding.argv:
            return None
        if binding.transport == TRANSPORT_COMMAND:
            return CommandProviderTransport(argv=binding.argv, env=binding.env)
        if binding.transport == TRANSPORT_MCP:
            return McpProviderTransport(argv=binding.argv, env=binding.env)
        return None

    def _finish(
        self,
        result: CapabilityResult,
        *,
        ref: SpecRef | None,
        initiator: str | None,
    ) -> CapabilityResult:
        """Attribute declared cost, record the call, and return the result."""
        if result.cost_credits > 0:
            self._cost_sink.attribute(
                run=result.request.run,
                capability=result.capability,
                provider=result.provider.name,
                credits=result.cost_credits,
            )
        if result.degraded and result.degradation is not None:
            logger.warning(
                "capability %s degraded to its builtin: %s (%s)",
                result.capability,
                result.degradation.reason,
                result.degradation.finding_id,
            )
        if self._audit is not None and ref is not None:
            self._audit.append(
                ref,
                AUDIT_EVENT_CAPABILITY,
                run=result.request.run or None,
                initiator=initiator,
                detail=result.audit_detail(),
                cost=result.cost_credits or None,
            )
        return result


def external_identity(
    program: str,
    transport: str,
    *,
    declared: str = "",
    version: str = "",
) -> ProviderIdentity:
    """Identity of a provider reached over an external transport.

    The name a provider declares for itself is provider-authored, so it is used
    for display only and the program the operator configured is what identifies
    the binding. An external provider is reported as model-backed: the engine
    cannot know whether it reasons, and claiming determinism it did not promise
    would be the more damaging of the two mistakes.
    """
    label = program or transport
    if declared.strip():
        label = f"{label} ({Untrusted(declared).for_display(limit=64)})"
    return ProviderIdentity(
        name=label,
        kind=ProviderKind.EXTERNAL,
        nature=ProviderNature.MODEL_BACKED,
        transport=transport,
        version=Untrusted(version).for_display(limit=32) if version else "",
    )


def _failure_reason(binding: Binding, failure: TransportFailure) -> str:
    """A non-empty explanation of a transport failure.

    A degradation reason is quoted in refusals, notifications, and the audit log,
    so an empty one leaves a reader with a finding identifier and nothing else. A
    transport that reported no reason gets an engine-authored one naming the
    condition and the binding it came from.
    """
    reported = failure.reason.strip()
    if reported:
        return reported
    program = binding.program or binding.transport
    return f"{program!r} bound to {binding.capability!r} failed: {failure.finding_id}"


def _schema_reason(binding: Binding, errors: tuple[SchemaError, ...]) -> str:
    """An engine-authored explanation of a schema failure.

    Paths and engine messages only. The provider's own strings are not quoted
    here: this reason is displayed, notified, and audited, and provider text
    belongs in a field marked as provider text.
    """
    shown = "; ".join(str(error) for error in errors[:3])
    more = "" if len(errors) <= 3 else f" (and {len(errors) - 3} more)"
    program = binding.program or binding.transport
    return f"{program!r} returned a response that fails the published schema: {shown}{more}"


def _payload_from_response(response: CapabilityResponse) -> dict[str, Any]:
    """Render a builtin's response as the wire payload its schema describes."""
    return response.to_wire()


def response_from_payload(capability: str, payload: Mapping[str, Any]) -> CapabilityResponse:
    """Build a response from a payload that already passed its schema.

    Every provider-authored string is wrapped as untrusted on the way in, at the
    one place a payload becomes engine data. Wrapping later would leave a window
    in which the text is an ordinary ``str``, and a window is all it takes.

    Public because the semantic analyzer's dispatched-turn output is the same
    untrusted, schema-valid analysis payload an external provider returns, and it
    becomes engine data through this one wrapping rather than a second spelling of
    it.
    """
    provider = payload.get("provider", {})
    provider_name = str(provider.get("name", "")) if isinstance(provider, Mapping) else ""
    provider_version = str(provider.get("version", "")) if isinstance(provider, Mapping) else ""
    cost = payload.get("cost", {})
    credits = float(cost.get("credits", 0.0)) if isinstance(cost, Mapping) else 0.0
    result = payload.get("result", {})
    return CapabilityResponse(
        capability=capability,
        provider_name=provider_name,
        coverage=_coverage_from(payload.get("coverage", {})),
        findings=_findings_from(payload.get("findings", ())),
        cost_credits=credits,
        result=dict(result) if isinstance(result, Mapping) else {},
        provider_version=provider_version,
        schema_version=int(payload.get("schema_version", 1)),
    )


def _coverage_from(node: Any) -> Coverage:
    if not isinstance(node, Mapping):
        return Coverage()
    processed = node.get("processed", ())
    skipped_node = node.get("skipped", ())
    skipped: list[SkippedItem] = []
    if isinstance(skipped_node, (list, tuple)):
        for entry in skipped_node:
            if not isinstance(entry, Mapping):
                continue
            skipped.append(
                SkippedItem(
                    item=str(entry.get("item", "")),
                    reason=Untrusted(str(entry.get("reason", ""))),
                )
            )
    kinds = tuple(str(item) for item in processed) if isinstance(processed, (list, tuple)) else ()
    return Coverage(processed=kinds, skipped=tuple(skipped))


def _findings_from(node: Any) -> tuple[ProviderFinding, ...]:
    if not isinstance(node, (list, tuple)):
        return ()
    findings: list[ProviderFinding] = []
    for entry in node:
        if not isinstance(entry, Mapping):
            continue
        refs = entry.get("refs", ())
        findings.append(
            ProviderFinding(
                kind=str(entry.get("kind", "")),
                severity=FindingSeverity(str(entry.get("severity", FindingSeverity.INFO.value))),
                message=Untrusted(str(entry.get("message", ""))),
                refs=tuple(str(ref) for ref in refs) if isinstance(refs, (list, tuple)) else (),
                question=_question_from(entry.get("question")),
            )
        )
    return tuple(findings)


def _question_from(node: Any) -> ClarifyingQuestion | None:
    if not isinstance(node, Mapping):
        return None
    choices = node.get("choices", ())
    consequences = node.get("consequences", ())
    recommended = node.get("recommended")
    return ClarifyingQuestion(
        question=Untrusted(str(node.get("question", ""))),
        choices=untrusted_all(choices) if isinstance(choices, (list, tuple)) else (),
        consequences=(
            untrusted_all(consequences) if isinstance(consequences, (list, tuple)) else ()
        ),
        recommended=Untrusted(str(recommended)) if isinstance(recommended, str) else None,
    )
