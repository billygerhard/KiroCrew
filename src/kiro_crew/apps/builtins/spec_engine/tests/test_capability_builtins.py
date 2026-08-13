"""The engine's own paths, registered as the deeper builtin providers.

The claims under test are the ones task 17.5 adds on top of what the registry and
the conformance runner already guarantee. Three capabilities the engine serves by
seeding an agent turn — authoring, review, implementation — must identify as
model-backed, because a surface that shows them deterministic tells an operator a
turn that spends credits costs nothing. The model catalog must resolve the
identifiers the host advertises rather than inventing a list. Every one of these
builtins must pass its own conformance suite, and each must be the honest
fallback a broken external provider degrades to rather than the shipped
no-coverage default.

The registration is proven live, not merely constructed: the nature a surface
reads changes with it, so deleting a registration flips a capability back to the
deterministic default and fails the test that asserts the model-backed mark.
"""

from __future__ import annotations

from typing import Any

import pytest

from kiro_crew.apps.builtins.spec_engine.engine.capabilities import (
    AUTHORING_PROVIDER,
    FINDING_PROVIDER_TIMEOUT,
    FINDING_PROVIDER_UNAVAILABLE,
    FINDING_RESPONSE_INVALID,
    IMPLEMENTATION_PROVIDER,
    MODEL_CATALOG_PROVIDER,
    REVIEW_PROVIDER,
    TRANSPORT_COMMAND,
    CapabilityRegistry,
    CapabilityRequest,
    HostModelCatalog,
    ProviderKind,
    ProviderNature,
    TransportFailure,
    register_builtins,
    verify_builtin,
)
from kiro_crew.apps.builtins.spec_engine.engine.config import DASHBOARD_SURFACE, ConfigStore

from .test_capability_registry import StubTransport
from .test_capability_schemas import response_payload

#: The four capabilities this task registers, with the nature each must report.
_MODEL_BACKED = ("authoring", "review", "implementation")
_PROVIDER_NAMES = {
    "authoring": AUTHORING_PROVIDER,
    "review": REVIEW_PROVIDER,
    "implementation": IMPLEMENTATION_PROVIDER,
    "model_catalog": MODEL_CATALOG_PROVIDER,
}


@pytest.fixture()
def store(tmp_path: Any) -> ConfigStore:
    return ConfigStore(tmp_path / "state")


def _resolver(*models: str):
    return lambda: list(models)


def registered(store: ConfigStore, *, models: tuple[str, ...] = ("auto",), **kwargs: Any):
    """A registry with the engine's own paths registered as builtins."""
    registry = CapabilityRegistry(store, **kwargs)
    register_builtins(registry, model_resolver=_resolver(*models))
    return registry


def described(registry: CapabilityRegistry) -> dict[str, dict[str, Any]]:
    return {entry["capability"]: entry for entry in registry.describe()}


def bind(store: ConfigStore, capability: str, **binding: Any) -> None:
    store.write({"capabilities": {capability: binding}}, surface=DASHBOARD_SURFACE)


# --- registration and nature -----------------------------------------------


class TestRegistrationMarksNature:
    def test_the_seeded_turn_capabilities_register_as_model_backed(
        self, store: ConfigStore
    ) -> None:
        described_map = described(registered(store))
        for capability in _MODEL_BACKED:
            entry = described_map[capability]["provider"]
            assert entry["kind"] == ProviderKind.BUILTIN.value, capability
            # The claim that matters: a path that seeds a turn and spends credits
            # is not shown as one whose deterministic checks found nothing.
            assert entry["nature"] == ProviderNature.MODEL_BACKED.value, capability
            assert entry["name"] == _PROVIDER_NAMES[capability]

    def test_the_model_catalog_registers_as_deterministic(self, store: ConfigStore) -> None:
        entry = described(registered(store))["model_catalog"]["provider"]
        assert entry["kind"] == ProviderKind.BUILTIN.value
        # Host resolution asks a model for nothing, so it is deterministic; a
        # model-backed mark here would claim a spend the catalog never makes.
        assert entry["nature"] == ProviderNature.DETERMINISTIC.value
        assert entry["name"] == MODEL_CATALOG_PROVIDER

    def test_without_registration_the_seeded_paths_are_the_deterministic_default(
        self, store: ConfigStore
    ) -> None:
        # The anchor for the mutation probe: the shipped default marks these
        # deterministic, so deleting a registration line reverts the nature and
        # fails the model-backed assertion above rather than passing silently.
        described_map = described(CapabilityRegistry(store))
        for capability in _MODEL_BACKED:
            entry = described_map[capability]["provider"]
            assert entry["nature"] == ProviderNature.DETERMINISTIC.value, capability
            assert entry["name"] != _PROVIDER_NAMES[capability]


# --- the model catalog resolves from the host ------------------------------


class TestModelCatalogHostResolution:
    def test_it_returns_the_identifiers_the_host_advertises(self, store: ConfigStore) -> None:
        registry = registered(store, models=("auto", "fast", "careful"))
        response = registry.builtin("model_catalog").serve(_request("model_catalog"))
        assert response.result["models"] == ["auto", "fast", "careful"]
        assert response.cost_credits == 0.0

    def test_blank_and_duplicate_identifiers_are_dropped_preserving_order(self) -> None:
        catalog = HostModelCatalog(resolver=lambda: ["auto", "  ", "fast", "auto", "", "fast"])
        response = catalog.serve(_request("model_catalog"))
        assert response.result["models"] == ["auto", "fast"]

    def test_an_empty_host_catalog_still_declares_what_it_processed(self) -> None:
        # An empty models list is a legitimate answer, but a response with no
        # coverage cannot say "asked the host, it advertised nothing" apart from
        # "never asked". The processed declaration is what carries that, and it
        # is also what a repeatability check needs to compare.
        catalog = HostModelCatalog(resolver=lambda: [])
        response = catalog.serve(_request("model_catalog"))
        assert response.result["models"] == []
        assert response.coverage.processed
        assert response.coverage.complete  # nothing skipped, so no false blind spot


# --- every registered builtin passes its own suite -------------------------


class TestEachBuiltinConforms:
    @pytest.mark.parametrize("capability", ["authoring", "review", "implementation", "model_catalog"])
    def test_the_registered_builtin_passes_the_capabilitys_suite(
        self, store: ConfigStore, capability: str
    ) -> None:
        report = verify_builtin(registered(store).builtin(capability), capability)
        assert report.passed, report.report_text()


# --- the builtin is the honest fallback ------------------------------------


class TestBrokenExternalProviderDegradesToTheEnginePath:
    def test_an_unavailable_authoring_provider_degrades_to_the_seeded_turn(
        self, store: ConfigStore
    ) -> None:
        bind(store, "authoring", transport=TRANSPORT_COMMAND, command=["author"])
        transport = StubTransport(
            failure=TransportFailure(FINDING_PROVIDER_UNAVAILABLE, "child would not start")
        )
        result = registered(store, transports={transport.transport: transport}).invoke(
            _request("authoring")
        )
        assert result.degraded
        assert result.degradation is not None
        assert result.degradation.finding_id == FINDING_PROVIDER_UNAVAILABLE
        # The fallback is the model-backed engine path, not the shipped default.
        assert result.provider.name == AUTHORING_PROVIDER
        assert result.provider.nature is ProviderNature.MODEL_BACKED

    def test_a_schema_invalid_review_response_degrades_to_the_verdict_turn(
        self, store: ConfigStore
    ) -> None:
        bind(store, "review", transport=TRANSPORT_COMMAND, command=["reviewer"])
        # Missing the required coverage and result: not a valid review response.
        transport = StubTransport(payload={"schema_version": 1, "capability": "review"})
        result = registered(store, transports={transport.transport: transport}).invoke(
            _request("review")
        )
        assert result.degraded
        assert result.degradation is not None
        assert result.degradation.finding_id == FINDING_RESPONSE_INVALID
        assert result.provider.name == REVIEW_PROVIDER

    def test_a_timed_out_implementation_provider_degrades_to_the_dispatch(
        self, store: ConfigStore
    ) -> None:
        bind(store, "implementation", transport=TRANSPORT_COMMAND, command=["agent"])
        transport = StubTransport(
            failure=TransportFailure(FINDING_PROVIDER_TIMEOUT, "exceeded its deadline")
        )
        result = registered(store, transports={transport.transport: transport}).invoke(
            _request("implementation")
        )
        assert result.degraded
        assert result.degradation is not None
        assert result.degradation.finding_id == FINDING_PROVIDER_TIMEOUT
        assert result.provider.name == IMPLEMENTATION_PROVIDER

    def test_a_working_external_provider_answers_and_is_not_a_fallback(
        self, store: ConfigStore
    ) -> None:
        # The negative control: a valid external response must be used as-is, so
        # the degrade tests above are about failure and not about the engine
        # always taking its own path.
        bind(store, "review", transport=TRANSPORT_COMMAND, command=["reviewer"])
        transport = StubTransport(payload=response_payload("review"))
        result = registered(store, transports={transport.transport: transport}).invoke(
            _request("review")
        )
        assert not result.degraded
        assert result.provider.kind is ProviderKind.EXTERNAL


def _request(capability: str) -> CapabilityRequest:
    return CapabilityRequest(capability=capability, spec_type="feature", run="run-1")
