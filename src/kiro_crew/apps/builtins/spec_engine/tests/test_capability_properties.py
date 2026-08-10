"""Property-based tests for the three guarantees delegation must not break.

Scripted cases cover the shapes someone thought to write down. Each of these
three properties guards against the shape nobody thought of, and all three are
failures that would be attributed to something other than the registry after the
fact: a binding that took effect, a gate that opened, a run that died because a
provider did.

**The engine floor is never bindable.** Whatever a configuration document says
and however it is shaped, a capability the engine always executes is refused —
never accepted, and never silently ignored.

**Supplementation is additive.** Whatever a provider returns, every engine
violation survives with the same rule and severity and the gate verdict is the
engine's own.

**Fallback is total.** However an external provider fails — unreachable, past its
deadline, or answering with any payload at all — the call returns an answer from
the builtin and marks itself degraded, rather than raising into the run.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from kiro_crew.apps.builtins.spec_engine.engine.capabilities import (
    TRANSPORT_COMMAND,
    TRANSPORT_MCP,
    ArtifactRef,
    CapabilityRegistry,
    CapabilityRequest,
    EngineFloorViolation,
    FindingSeverity,
    ProviderFinding,
    ProviderKind,
    TransportFailure,
    Untrusted,
    resolve_bindings,
    supplement,
)
from kiro_crew.apps.builtins.spec_engine.engine.capabilities.contracts import (
    FINDING_PROVIDER_TIMEOUT,
    FINDING_PROVIDER_UNAVAILABLE,
    FINDING_RESPONSE_INVALID,
)
from kiro_crew.apps.builtins.spec_engine.engine.config import (
    DASHBOARD_SURFACE,
    DELEGABLE_CAPABILITIES,
    ENGINE_FLOOR_CAPABILITIES,
    ConfigStore,
    ConfigValidationError,
)
from kiro_crew.apps.builtins.spec_engine.engine.findings import (
    Location,
    Severity,
    Violation,
    build_report,
)

#: Resolution, supplementation, and the fallback path are all in-memory, so
#: examples are cheap and the count sits well above the distinct shapes the
#: scripted cases cover.
MAX_EXAMPLES = 200

#: The fallback property builds a config store per example, which touches the
#: filesystem, so it runs a smaller number of examples.
MAX_IO_EXAMPLES = 50

_ENGINE_FLOOR = st.sampled_from(ENGINE_FLOOR_CAPABILITIES)
_DELEGABLE = st.sampled_from(DELEGABLE_CAPABILITIES)
_EXTERNAL_TRANSPORTS = st.sampled_from([TRANSPORT_COMMAND, TRANSPORT_MCP])

#: Binding shapes an operator might write, valid and not.
_BINDINGS = st.one_of(
    st.just({"transport": "builtin"}),
    st.builds(
        lambda transport, program: {"transport": transport, "command": [program]},
        _EXTERNAL_TRANSPORTS,
        st.sampled_from(["analyzer", "reviewer", "agent"]),
    ),
    st.just({}),
    st.just({"transport": "carrier-pigeon"}),
    st.just(None),
    st.just([]),
    st.just("builtin"),
    st.builds(lambda timeout: {"transport": "builtin", "timeout_s": timeout}, st.integers()),
)

_SEVERITIES = st.sampled_from(list(FindingSeverity))

#: Violation shapes the engine's own validator produces: a file, a 1-based
#: position, a dotted rule identifier, and a severity.
_VIOLATIONS = st.builds(
    lambda file, line, rule, severity: Violation(
        file=file,
        location=Location(line),
        rule=rule,
        severity=severity,
        message="engine finding",
    ),
    st.sampled_from(["requirements.md", "design.md", "tasks.md"]),
    st.integers(min_value=1, max_value=500),
    st.sampled_from(
        [
            "requirements.section.missing",
            "requirements.criterion.shape",
            "tasks.checkbox.syntax",
            "tasks.wave.cycle",
        ]
    ),
    st.sampled_from(list(Severity)),
)

_PROVIDER_FINDINGS = st.builds(
    lambda kind, severity, message, refs: ProviderFinding(
        kind=kind, severity=severity, message=Untrusted(message), refs=tuple(refs)
    ),
    st.sampled_from(["ambiguity", "coverage", "dispute", "resolved", "severity"]),
    _SEVERITIES,
    st.text(max_size=40),
    st.lists(st.sampled_from(["1.1", "2.3", "9.9"]), max_size=3),
)

#: Payloads an external provider might answer with. Deliberately includes shapes
#: that are close to valid: a response missing one required field is the case a
#: lenient parser would accept and record.
_PAYLOADS = st.one_of(
    st.just({}),
    st.just({"schema_version": 1}),
    st.just({"schema_version": 1, "capability": "analysis"}),
    st.just(
        {
            "schema_version": 1,
            "capability": "analysis",
            "provider": {"name": "p"},
            "findings": [],
            "result": {"depth": "extended"},
        }
    ),
    st.just({"schema_version": 99, "capability": "analysis"}),
    st.just([1, 2, 3]),
    st.just("not an object"),
    st.none(),
    st.just({"schema_version": 1, "capability": "analysis", "unexpected": True}),
)

_FAILURES = st.one_of(
    st.builds(
        TransportFailure,
        st.sampled_from(
            [FINDING_PROVIDER_TIMEOUT, FINDING_PROVIDER_UNAVAILABLE, FINDING_RESPONSE_INVALID]
        ),
        st.text(max_size=40),
    ),
    st.builds(RuntimeError, st.text(max_size=20)),
    st.builds(OSError, st.text(max_size=20)),
    st.builds(ValueError, st.text(max_size=20)),
    st.builds(KeyError, st.text(max_size=20)),
)


def _store(root: Path) -> ConfigStore:
    return ConfigStore(root)


def _request(capability: str = "analysis") -> CapabilityRequest:
    return CapabilityRequest(
        capability=capability,
        spec_type="feature",
        artifacts=(ArtifactRef(kind="requirements", path="/p/requirements.md"),),
        run="run-1",
    )


class _AnsweringTransport:
    def __init__(self, transport: str, payload: Any) -> None:
        self._transport = transport
        self._payload = payload

    @property
    def transport(self) -> str:
        return self._transport

    def invoke(self, request: CapabilityRequest, *, timeout_s: int) -> Any:
        return self._payload


class _FailingTransport:
    def __init__(self, transport: str, failure: Exception) -> None:
        self._transport = transport
        self._failure = failure

    @property
    def transport(self) -> str:
        return self._transport

    def invoke(self, request: CapabilityRequest, *, timeout_s: int) -> Any:
        raise self._failure


@settings(max_examples=MAX_EXAMPLES, deadline=None)
@given(capability=_ENGINE_FLOOR, binding=_BINDINGS)
def test_no_binding_shape_ever_binds_an_engine_floor_capability(
    tmp_path_factory: pytest.TempPathFactory, capability: str, binding: Any
) -> None:
    store = _store(tmp_path_factory.mktemp("floor") / "state")
    try:
        store.write({"capabilities": {capability: binding}}, surface=DASHBOARD_SURFACE)
    except (ConfigValidationError, EngineFloorViolation):
        refused = True
    else:
        refused = False
    # ``None`` is the write path's remove-this-key verb rather than a binding, so
    # it is a no-op; every shape that actually declares a provider is refused.
    assert refused or binding is None
    # The invariant that matters holds either way: nothing was bound, so no
    # engine-floor capability can be reached through a provider.
    assert capability not in resolve_bindings(store)
    assert capability not in store.document().get("capabilities", {})


@settings(max_examples=MAX_EXAMPLES, deadline=None)
@given(capability=_ENGINE_FLOOR, binding=_BINDINGS)
def test_a_hand_written_engine_floor_binding_is_refused_at_resolution(
    tmp_path_factory: pytest.TempPathFactory, capability: str, binding: Any
) -> None:
    import json

    store = _store(tmp_path_factory.mktemp("handwritten") / "state")
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text(
        json.dumps({"version": 1, "capabilities": {capability: binding}}), encoding="utf-8"
    )
    with pytest.raises(EngineFloorViolation):
        resolve_bindings(store)


@settings(max_examples=MAX_EXAMPLES, deadline=None)
@given(
    violations=st.lists(_VIOLATIONS, max_size=6),
    findings=st.lists(_PROVIDER_FINDINGS, max_size=6),
)
def test_supplementation_never_removes_or_downgrades_an_engine_finding(
    violations: list[Violation], findings: list[ProviderFinding]
) -> None:
    from .test_capability_supplementary import provider_result

    report = build_report(violations)
    before = tuple((v.file, str(v.location), v.rule, v.severity.value) for v in report.violations)
    merged = supplement(report, provider_result(*findings))
    after = tuple((v.file, str(v.location), v.rule, v.severity.value) for v in merged.violations)
    assert after == before
    assert merged.engine is report
    assert merged.errors == report.errors
    # The gate verdict has no term involving provider input.
    assert merged.gate_ok is report.ok
    assert len(merged.supplementary) == len(findings)
    # Every provider finding is present and none replaced an engine entry.
    entries = merged.all_entries()
    assert sum(1 for entry in entries if entry.from_engine) == len(report.violations)
    assert sum(1 for entry in entries if not entry.from_engine) == len(findings)


@settings(
    max_examples=MAX_IO_EXAMPLES,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    capability=_DELEGABLE,
    transport=_EXTERNAL_TRANSPORTS,
    failure=_FAILURES,
)
def test_any_provider_failure_falls_back_to_the_builtin_without_raising(
    tmp_path_factory: pytest.TempPathFactory,
    capability: str,
    transport: str,
    failure: Exception,
) -> None:
    store = _store(tmp_path_factory.mktemp("fallback") / "state")
    store.write(
        {"capabilities": {capability: {"transport": transport, "command": ["provider"]}}},
        surface=DASHBOARD_SURFACE,
    )
    registry = CapabilityRegistry(
        store, transports={transport: _FailingTransport(transport, failure)}
    )
    result = registry.invoke(_request(capability))
    assert result.degraded
    assert result.degradation is not None
    assert result.degradation.reason
    assert result.degradation.transport == transport
    assert result.provider.kind is ProviderKind.BUILTIN
    assert result.response.capability == capability


@settings(
    max_examples=MAX_IO_EXAMPLES,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(transport=_EXTERNAL_TRANSPORTS, payload=_PAYLOADS)
def test_no_unvalidated_payload_is_ever_recorded_as_findings(
    tmp_path_factory: pytest.TempPathFactory, transport: str, payload: Any
) -> None:
    store = _store(tmp_path_factory.mktemp("payload") / "state")
    store.write(
        {"capabilities": {"analysis": {"transport": transport, "command": ["provider"]}}},
        surface=DASHBOARD_SURFACE,
    )
    registry = CapabilityRegistry(
        store, transports={transport: _AnsweringTransport(transport, payload)}
    )
    result = registry.invoke(_request())
    # Every payload in the strategy is invalid, so every call must degrade and
    # none may have carried a finding through.
    assert result.degraded
    assert result.findings == ()
    assert result.provider.kind is ProviderKind.BUILTIN
