"""Autonomy policy resolution: the ladder, the grid, and the safe default.

The claim these tests exist to pin is that authority is never granted by
accident. An install that configured nothing must resolve to authoring only with
execution human-reserved; a configured rung must resolve to itself and to
everything below it and to nothing above it; and the policy must be readable
from configuration and writable from nowhere reachable here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew.apps.builtins.spec_engine.engine.autonomy import (
    AUTONOMY_FIELD,
    UNCONFIGURED_LEVEL,
    AutonomyDecision,
    AutonomyLevel,
    AutonomyPolicy,
)
from kiro_crew.apps.builtins.spec_engine.engine.config import (
    AUTONOMY_LEVELS,
    CONFIG_ONLY_PATHS,
    DASHBOARD_SURFACE,
    LEAST_TRUSTED_CLASS,
    SOURCE_FIELDS,
    SPEC_TYPES,
    SUBMITTER_CLASSES,
    WILDCARD_KEY,
    ConfigStore,
    ConfigValidationError,
    ConfigWriteRefused,
    ConfigWriteSurface,
)

SOURCE = "tracker"


def policy_for(grid: dict | None, *, source: str = SOURCE) -> AutonomyPolicy:
    """A policy over one source whose autonomy grid is *grid*."""
    entry: dict = {"poll": ["watch"]}
    if grid is not None:
        entry[AUTONOMY_FIELD] = grid
    return AutonomyPolicy.from_document({"sources": {source: entry}})


def resolve(
    policy: AutonomyPolicy,
    *,
    source: str | None = SOURCE,
    spec_type: str = "feature",
    submitter_class: str | None = "member",
) -> AutonomyDecision:
    return policy.resolve(source=source, spec_type=spec_type, submitter_class=submitter_class)


class TestLadder:
    def test_rank_order_follows_the_schema_ladder(self):
        ranks = [AutonomyLevel(name).rank for name in AUTONOMY_LEVELS]
        assert ranks == sorted(ranks)
        assert len({level.value for level in AutonomyLevel}) == len(AUTONOMY_LEVELS)
        assert {level.value for level in AutonomyLevel} == set(AUTONOMY_LEVELS)

    def test_a_granted_level_implies_every_lower_level(self):
        assert AutonomyLevel.DELIVERY.permits(AutonomyLevel.EXECUTION)
        assert AutonomyLevel.DELIVERY.permits(AutonomyLevel.AUTHORING)
        assert AutonomyLevel.INTEGRATION.implies() == (
            AutonomyLevel.AUTHORING,
            AutonomyLevel.EXECUTION,
            AutonomyLevel.DELIVERY,
            AutonomyLevel.INTEGRATION,
        )

    def test_a_granted_level_never_implies_a_higher_one(self):
        assert not AutonomyLevel.EXECUTION.permits(AutonomyLevel.DELIVERY)
        assert not AutonomyLevel.AUTHORING.permits(AutonomyLevel.EXECUTION)
        assert AutonomyLevel.AUTHORING.implies() == (AutonomyLevel.AUTHORING,)

    def test_every_level_permits_itself(self):
        for level in AutonomyLevel:
            assert level.permits(level)

    def test_levels_are_not_comparable_as_strings(self):
        # Lexicographically "delivery" sorts below "execution" while the ladder
        # puts it above, so string comparison would authorize delivery for an
        # execution-only policy. The operators must be absent, not wrong.
        with pytest.raises(TypeError):
            AutonomyLevel.EXECUTION >= AutonomyLevel.DELIVERY  # type: ignore[operator]
        assert not isinstance(AutonomyLevel.EXECUTION.value, AutonomyLevel)


class TestUnconfiguredDefault:
    def test_empty_document_resolves_to_authoring_with_execution_human_reserved(self):
        decision = resolve(AutonomyPolicy.from_document({}))
        assert decision.level is AutonomyLevel.AUTHORING
        assert decision.level is UNCONFIGURED_LEVEL
        assert decision.execution_is_human_reserved
        assert not decision.is_configured
        assert decision.declared_at == ""

    def test_absent_config_file_resolves_to_the_safe_default(self, tmp_path: Path):
        store = ConfigStore(tmp_path / "state")
        assert not store.path.exists()
        decision = resolve(AutonomyPolicy.from_store(store))
        assert decision.level is AutonomyLevel.AUTHORING
        assert decision.execution_is_human_reserved

    def test_source_configured_without_a_grid_stays_human_reserved(self):
        decision = resolve(policy_for(None))
        assert decision.level is AutonomyLevel.AUTHORING
        assert decision.execution_is_human_reserved

    def test_grid_covering_other_classes_leaves_this_one_human_reserved(self):
        grid = {"maintainer": {"feature": "delivery"}}
        decision = resolve(policy_for(grid), submitter_class="contributor")
        assert decision.level is AutonomyLevel.AUTHORING
        assert decision.execution_is_human_reserved

    def test_grid_covering_other_spec_types_leaves_this_one_human_reserved(self):
        grid = {"member": {"quick": "execution"}}
        decision = resolve(policy_for(grid), spec_type="feature")
        assert decision.level is AutonomyLevel.AUTHORING

    def test_unknown_source_resolves_to_the_safe_default(self):
        decision = resolve(policy_for({"default": {"default": "integration"}}), source="other")
        assert decision.level is AutonomyLevel.AUTHORING
        assert not decision.is_configured

    def test_run_without_a_source_resolves_to_the_safe_default(self):
        decision = resolve(policy_for({"default": {"default": "integration"}}), source=None)
        assert decision.level is AutonomyLevel.AUTHORING
        assert decision.source is None
        assert not decision.is_configured

    def test_every_triple_is_human_reserved_under_zero_configuration(self):
        policy = AutonomyPolicy.from_document({})
        for spec_type in SPEC_TYPES:
            for klass in SUBMITTER_CLASSES:
                decision = resolve(policy, spec_type=spec_type, submitter_class=klass)
                assert decision.execution_is_human_reserved, (spec_type, klass)


class TestConfiguredResolution:
    @pytest.mark.parametrize("level", AUTONOMY_LEVELS)
    def test_configured_level_resolves_exactly_to_itself(self, level: str):
        grid = {"member": {"feature": level}}
        decision = resolve(policy_for(grid))
        assert decision.level.value == level
        assert decision.is_configured
        assert decision.declared_at == f"sources.{SOURCE}.{AUTONOMY_FIELD}.member.feature"

    def test_decision_carries_back_the_triple_it_resolved(self):
        decision = resolve(policy_for({"member": {"quick": "execution"}}), spec_type="quick")
        assert (decision.source, decision.spec_type, decision.submitter_class) == (
            SOURCE,
            "quick",
            "member",
        )

    def test_granting_delivery_permits_execution_without_naming_it(self):
        decision = resolve(policy_for({"member": {"feature": "delivery"}}))
        assert decision.permits(AutonomyLevel.EXECUTION)
        assert decision.permits(AutonomyLevel.AUTHORING)
        assert not decision.execution_is_human_reserved

    def test_granting_execution_does_not_permit_delivery(self):
        decision = resolve(policy_for({"member": {"feature": "execution"}}))
        assert decision.permits(AutonomyLevel.EXECUTION)
        assert not decision.permits(AutonomyLevel.DELIVERY)
        assert not decision.permits(AutonomyLevel.INTEGRATION)

    def test_configured_authoring_keeps_execution_human_reserved(self):
        decision = resolve(policy_for({"member": {"feature": "authoring"}}))
        assert decision.is_configured
        assert decision.execution_is_human_reserved

    def test_undeterminable_author_resolves_as_the_least_trusted_class(self):
        grid = {
            LEAST_TRUSTED_CLASS: {"feature": "authoring"},
            WILDCARD_KEY: {"feature": "integration"},
        }
        decision = resolve(policy_for(grid), submitter_class=None)
        assert decision.submitter_class == LEAST_TRUSTED_CLASS
        assert decision.level is AutonomyLevel.AUTHORING

    def test_two_sources_resolve_independently(self):
        document = {
            "sources": {
                "a": {
                    "poll": ["watch"],
                    AUTONOMY_FIELD: {WILDCARD_KEY: {WILDCARD_KEY: "delivery"}},
                },
                "b": {"poll": ["watch"]},
            }
        }
        policy = AutonomyPolicy.from_document(document)
        assert resolve(policy, source="a").level is AutonomyLevel.DELIVERY
        assert resolve(policy, source="b").level is AutonomyLevel.AUTHORING


class TestGridSpecificity:
    def test_exact_cell_beats_every_wildcard(self):
        grid = {
            "member": {"feature": "execution", WILDCARD_KEY: "delivery"},
            WILDCARD_KEY: {"feature": "integration", WILDCARD_KEY: "integration"},
        }
        decision = resolve(policy_for(grid))
        assert decision.level is AutonomyLevel.EXECUTION
        assert decision.declared_at.endswith("member.feature")

    def test_named_class_wildcard_type_beats_wildcard_class_named_type(self):
        # The declaration naming the author is the one an operator wrote to hold
        # something back, so it wins over one naming only the spec type.
        grid = {
            "external": {WILDCARD_KEY: "authoring"},
            WILDCARD_KEY: {"quick": "integration"},
        }
        decision = resolve(policy_for(grid), spec_type="quick", submitter_class="external")
        assert decision.level is AutonomyLevel.AUTHORING
        assert decision.declared_at.endswith(f"external.{WILDCARD_KEY}")

    def test_wildcard_class_named_type_beats_the_all_wildcard_cell(self):
        grid = {WILDCARD_KEY: {"feature": "execution", WILDCARD_KEY: "integration"}}
        decision = resolve(policy_for(grid))
        assert decision.level is AutonomyLevel.EXECUTION

    def test_all_wildcard_cell_applies_to_every_triple(self):
        policy = policy_for({WILDCARD_KEY: {WILDCARD_KEY: "execution"}})
        for spec_type in SPEC_TYPES:
            for klass in SUBMITTER_CLASSES:
                decision = resolve(policy, spec_type=spec_type, submitter_class=klass)
                assert decision.level is AutonomyLevel.EXECUTION, (spec_type, klass)


class TestMalformedAndUnknownInput:
    def test_unknown_spec_type_is_refused(self):
        with pytest.raises(ValueError, match="spec type"):
            resolve(policy_for(None), spec_type="epic")

    def test_unknown_submitter_class_is_refused(self):
        with pytest.raises(ValueError, match="submitter class"):
            resolve(policy_for(None), submitter_class="owner")

    def test_wildcard_is_not_accepted_as_a_submitter_class(self):
        with pytest.raises(ValueError, match="submitter class"):
            resolve(policy_for(None), submitter_class=WILDCARD_KEY)

    def test_unknown_stored_level_is_named_rather_than_substituted(self):
        with pytest.raises(ConfigValidationError) as raised:
            resolve(policy_for({"member": {"feature": "Delivery"}}))
        assert raised.value.errors[0].path.endswith("member.feature")

    def test_grid_that_is_not_an_object_is_named(self):
        with pytest.raises(ConfigValidationError) as raised:
            resolve(policy_for("delivery"))  # type: ignore[arg-type]
        assert raised.value.errors[0].path.endswith(AUTONOMY_FIELD)

    def test_class_entry_that_is_not_an_object_falls_through_to_wildcards(self):
        grid = {"member": "delivery", WILDCARD_KEY: {WILDCARD_KEY: "execution"}}
        assert resolve(policy_for(grid)).level is AutonomyLevel.EXECUTION

    def test_sources_section_that_is_not_an_object_resolves_to_the_default(self):
        policy = AutonomyPolicy.from_document({"sources": []})
        assert resolve(policy).level is AutonomyLevel.AUTHORING


class TestConfigurationIsTheOnlyInput:
    def test_the_policy_exposes_no_mutating_method(self):
        public = [name for name in dir(AutonomyPolicy) if not name.startswith("_")]
        assert sorted(public) == ["from_document", "from_store", "resolve"]

    def test_a_decision_cannot_be_edited_in_place(self):
        decision = resolve(policy_for({"member": {"feature": "authoring"}}))
        with pytest.raises(Exception):
            decision.level = AutonomyLevel.INTEGRATION  # type: ignore[misc]

    def test_mutating_the_caller_document_does_not_change_resolution(self):
        document: dict = {"sources": {SOURCE: {"poll": ["watch"]}}}
        policy = AutonomyPolicy.from_document(document)
        document["sources"][SOURCE][AUTONOMY_FIELD] = {WILDCARD_KEY: {WILDCARD_KEY: "integration"}}
        assert resolve(policy).level is AutonomyLevel.AUTHORING

    def test_the_policy_grid_lives_under_a_config_only_path(self):
        assert AUTONOMY_FIELD in SOURCE_FIELDS
        assert "sources" in CONFIG_ONLY_PATHS

    def test_an_operator_unconfirmed_surface_cannot_widen_the_policy(self, tmp_path: Path):
        store = ConfigStore(tmp_path / "state")
        patch = {
            "sources": {
                SOURCE: {
                    "poll": ["watch"],
                    AUTONOMY_FIELD: {WILDCARD_KEY: {WILDCARD_KEY: "integration"}},
                }
            }
        }
        with pytest.raises(ConfigWriteRefused):
            store.write(patch, surface=ConfigWriteSurface("engine"))
        assert resolve(AutonomyPolicy.from_store(store)).level is AutonomyLevel.AUTHORING

    def test_a_confirmed_operator_write_is_what_the_policy_reads(self, tmp_path: Path):
        store = ConfigStore(tmp_path / "state")
        store.write(
            {
                "sources": {
                    SOURCE: {
                        "poll": ["watch"],
                        AUTONOMY_FIELD: {"member": {"feature": "delivery"}},
                    }
                }
            },
            surface=DASHBOARD_SURFACE,
        )
        policy = AutonomyPolicy.from_store(store)
        assert resolve(policy).level is AutonomyLevel.DELIVERY
        # Live read: the next resolution sees an operator's later edit.
        store.write(
            {"sources": {SOURCE: {AUTONOMY_FIELD: {"member": {"feature": "authoring"}}}}},
            surface=DASHBOARD_SURFACE,
        )
        assert resolve(policy).execution_is_human_reserved

    def test_a_stored_grid_the_schema_accepts_is_a_grid_resolution_reads(self, tmp_path: Path):
        # The write path and the resolver must agree on the shape: a document the
        # schema calls valid is one resolution can read.
        store = ConfigStore(tmp_path / "state")
        grid = {
            klass: {spec_type: "execution" for spec_type in SPEC_TYPES}
            for klass in SUBMITTER_CLASSES
        }
        store.write(
            {"sources": {SOURCE: {"poll": ["watch"], AUTONOMY_FIELD: grid}}},
            surface=DASHBOARD_SURFACE,
        )
        assert json.loads(store.path.read_text(encoding="utf-8"))["sources"][SOURCE][AUTONOMY_FIELD]
        policy = AutonomyPolicy.from_store(store)
        for spec_type in SPEC_TYPES:
            for klass in SUBMITTER_CLASSES:
                decision = resolve(policy, spec_type=spec_type, submitter_class=klass)
                assert decision.level is AutonomyLevel.EXECUTION, (spec_type, klass)
