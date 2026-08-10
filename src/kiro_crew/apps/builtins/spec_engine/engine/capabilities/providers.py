"""Builtin providers: the answer every capability has without any configuration.

A capability with no builtin is a tool that answers "not configured", and a tool
that can answer that makes every agent's instructions conditional on someone's
configuration. So the registry refuses to exist unless all seven delegable
capabilities have a builtin, and this module is where they are registered.

:class:`DeclaredSkipProvider` is the shape a builtin takes when the honest answer
is that it did not examine something. It returns a schema-valid response with no
findings and a coverage block naming every requested artifact as skipped, with
the reason. That is deliberately not the same as returning nothing: a response
with an empty findings list and no coverage says "nothing wrong", while this one
says "nothing looked at, here is why". The distinction is the whole reason
coverage is in the envelope, and it is what keeps a clean pass at one depth from
reading as correctness at a greater one.

The supplementary-validation builtin is final rather than provisional. The app
ships no supplementary spec-document validation rules on purpose: native-format
validation in the engine is the validation baseline, and a second bundled rule
set would compete with it. Its builtin therefore contributes nothing and says so.

The deeper builtins — the structural and model-backed analyzers, the authoring,
review, and implementation turns, the bundled watch presets, the host model
catalog — register over these through :meth:`register_builtin`. Binding depth is
their business; guaranteeing an answer exists is this module's.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Protocol

from ..config import DELEGABLE_CAPABILITIES
from .contracts import (
    TRANSPORT_BUILTIN,
    CapabilityRequest,
    CapabilityResponse,
    Coverage,
    ProviderIdentity,
    ProviderKind,
    ProviderNature,
    SkippedItem,
    Untrusted,
)


class BuiltinProvider(Protocol):
    """A capability implementation that runs in the engine's own process.

    Returns a :class:`CapabilityResponse` directly rather than a wire payload:
    the builtin is engine code, so there is no untrusted boundary to serialize
    across and no reason to make the fallback path pay for one.
    """

    @property
    def identity(self) -> ProviderIdentity: ...

    def serve(self, request: CapabilityRequest) -> CapabilityResponse: ...


def builtin_identity(
    name: str,
    *,
    nature: ProviderNature = ProviderNature.DETERMINISTIC,
    version: str = "",
) -> ProviderIdentity:
    """Build the identity of a provider that ships with the app."""
    return ProviderIdentity(
        name=name,
        kind=ProviderKind.BUILTIN,
        nature=nature,
        transport=TRANSPORT_BUILTIN,
        version=version,
    )


@dataclass(frozen=True)
class DeclaredSkipProvider:
    """A builtin that answers with an honest, complete declaration of no coverage.

    Deterministic by construction: it invokes no model, reaches no network, and
    spends nothing, so it is always available as the fallback that keeps a broken
    external provider from blocking a run.
    """

    capability: str
    reason: str
    provider_name: str
    #: Body the capability's response schema requires. Empty for capabilities
    #: whose schema needs no fields.
    result: Mapping[str, object] = field(default_factory=dict)

    @property
    def identity(self) -> ProviderIdentity:
        return builtin_identity(self.provider_name)

    def serve(self, request: CapabilityRequest) -> CapabilityResponse:
        # Every requested artifact is named as skipped. Naming the artifacts
        # rather than emitting one blanket entry is what lets a surface show the
        # user exactly which documents went unexamined.
        skipped = tuple(
            SkippedItem(item=artifact.kind, reason=Untrusted(self.reason))
            for artifact in request.artifacts
        ) or (
            SkippedItem(item=self.capability, reason=Untrusted(self.reason)),
        )
        return CapabilityResponse(
            capability=self.capability,
            provider_name=self.provider_name,
            coverage=Coverage(processed=(), skipped=skipped),
            findings=(),
            cost_credits=0.0,
            result=dict(self.result),
        )


#: Reasons the shipped defaults declare. Each names the engine path that serves
#: the capability, so a reader learns where the work happens rather than only
#: that this provider did not do it.
_DEFAULT_REASONS: dict[str, str] = {
    "analysis": (
        "the bundled analyzers report at their own declared depth; this default "
        "declares no coverage so an unexamined document is never read as a clean one"
    ),
    "authoring": (
        "documents are authored by a seeded turn and accepted only by native-format "
        "validation and the phase gate"
    ),
    "review": "review verdicts come from a seeded review turn against the review criteria",
    "implementation": "leaf tasks are implemented by per-task dispatch in wave order",
    "validation_rules": (
        "the app ships no supplementary spec-document validation rules; native-format "
        "validation in the engine is the validation baseline"
    ),
    "watch_sources": "watch sources poll through their configured command presets",
    "model_catalog": "the available models are resolved from the host",
}

#: Bodies the per-capability response schemas require of any response.
_DEFAULT_RESULTS: dict[str, Mapping[str, object]] = {
    "analysis": {"depth": "none"},
    "authoring": {"documents": []},
    "review": {"verdict": "none"},
    "implementation": {"tasks": []},
    "validation_rules": {},
    "watch_sources": {"items": []},
    "model_catalog": {"models": []},
}


def default_builtins() -> dict[str, BuiltinProvider]:
    """Return one builtin provider per delegable capability.

    Built fresh on each call so a registry cannot mutate another registry's
    table — two registries with different configuration are ordinary in tests and
    in a multi-project gateway.
    """
    return {
        capability: DeclaredSkipProvider(
            capability=capability,
            reason=_DEFAULT_REASONS[capability],
            provider_name=f"engine-{capability.replace('_', '-')}",
            result=_DEFAULT_RESULTS[capability],
        )
        for capability in DELEGABLE_CAPABILITIES
    }
