"""The published capability schemas, and the validator that enforces them.

Two claims are worth testing precisely here. The first is that the published
document and the executed check come from one definition, so a provider author
who satisfies the document is satisfied by the engine. The second is that
validation is closed and unconditional: a response with a stray key has not
declared what the schema asked for, and a response at an unpublished version is
refused rather than read with today's field meanings.
"""

from __future__ import annotations

from typing import Any

import pytest

from kiro_crew.apps.builtins.spec_engine.engine.capabilities import (
    CURRENT_SCHEMA_VERSION,
    REQUEST,
    RESPONSE,
    ArtifactRef,
    CapabilityRequest,
    EngineFloorViolation,
    SchemaViolation,
    UnknownCapability,
    published_schemas,
    published_versions,
    schema_for,
    validate_response,
)
from kiro_crew.apps.builtins.spec_engine.engine.capabilities.schemas import (
    Arr,
    Bool,
    Int,
    Num,
    Obj,
    Str,
    StrMap,
    declared_version,
)
from kiro_crew.apps.builtins.spec_engine.engine.config import (
    DELEGABLE_CAPABILITIES,
    ENGINE_FLOOR_CAPABILITIES,
)


def response_payload(capability: str, **overrides: Any) -> dict[str, Any]:
    """A minimal valid response for *capability*, before any override."""
    bodies: dict[str, Any] = {
        "analysis": {"depth": "structural"},
        "authoring": {"documents": ["requirements.md"]},
        "review": {"verdict": "approved"},
        "implementation": {"tasks": [{"id": "1.1", "status": "done"}]},
        "validation_rules": {},
        "watch_sources": {"items": [{"id": "7"}]},
        "model_catalog": {"models": ["auto"]},
    }
    payload: dict[str, Any] = {
        "schema_version": CURRENT_SCHEMA_VERSION,
        "capability": capability,
        "provider": {"name": "candidate"},
        "coverage": {"processed": ["requirements"], "skipped": []},
        "findings": [],
        "result": bodies[capability],
    }
    payload.update(overrides)
    return payload


class TestPublication:
    def test_every_delegable_capability_publishes_both_directions(self) -> None:
        for capability in DELEGABLE_CAPABILITIES:
            for direction in (REQUEST, RESPONSE):
                schema = schema_for(capability, direction)
                assert schema.capability == capability
                assert schema.direction == direction
                assert schema.version == CURRENT_SCHEMA_VERSION

    def test_published_documents_are_json_schema_and_uniquely_identified(self) -> None:
        documents = published_schemas()
        assert len(documents) == len(DELEGABLE_CAPABILITIES) * 2
        for schema_id, document in documents.items():
            assert document["$id"] == schema_id
            assert document["$schema"].startswith("https://json-schema.org/")
            assert document["type"] == "object"
            # Closed by construction, so an author reading the document learns
            # that an extra key is refused rather than tolerated.
            assert document["additionalProperties"] is False
            assert document["required"]

    def test_a_schema_identifier_names_capability_direction_and_version(self) -> None:
        schema = schema_for("analysis", RESPONSE)
        assert schema.schema_id == "spec-engine/capability/analysis/response/v1.json"

    def test_published_identifiers_carry_no_host(self) -> None:
        # A contract shipped with the package must stay readable without anyone
        # serving it, so its identifier is a relative reference.
        for schema_id in published_schemas():
            assert "://" not in schema_id

    def test_published_versions_are_reported_ascending(self) -> None:
        assert published_versions("analysis", RESPONSE) == (CURRENT_SCHEMA_VERSION,)

    def test_an_engine_floor_capability_publishes_nothing(self) -> None:
        for capability in ENGINE_FLOOR_CAPABILITIES:
            with pytest.raises(EngineFloorViolation):
                schema_for(capability, RESPONSE)

    def test_an_unknown_capability_is_refused(self) -> None:
        with pytest.raises(UnknownCapability):
            schema_for("telepathy", RESPONSE)

    def test_an_unpublished_version_is_refused_rather_than_approximated(self) -> None:
        with pytest.raises(KeyError):
            schema_for("analysis", RESPONSE, CURRENT_SCHEMA_VERSION + 1)

    def test_an_unknown_direction_is_a_programming_error(self) -> None:
        with pytest.raises(ValueError):
            schema_for("analysis", "sideways")


class TestRequestSchema:
    def test_an_engine_built_request_satisfies_its_own_published_schema(self) -> None:
        for capability in DELEGABLE_CAPABILITIES:
            request = CapabilityRequest(
                capability=capability,
                spec_type="feature",
                artifacts=(ArtifactRef(kind="requirements", path="/p/requirements.md"),),
                run="run-1",
                deadline_s=30,
            )
            schema_for(capability, REQUEST).validate(request.to_wire())

    def test_the_request_carries_locations_spec_type_and_format_version(self) -> None:
        request = CapabilityRequest(
            capability="analysis",
            spec_type="bugfix",
            artifacts=(ArtifactRef(kind="design", path="/p/design.md", revision="sha256:abc"),),
        )
        wire = request.to_wire()
        assert wire["spec_type"] == "bugfix"
        assert wire["format_version"]
        assert wire["artifacts"] == [
            {"kind": "design", "path": "/p/design.md", "revision": "sha256:abc"}
        ]

    def test_a_request_for_another_capability_fails_that_capabilitys_schema(self) -> None:
        request = CapabilityRequest(capability="review", spec_type="feature")
        with pytest.raises(SchemaViolation):
            schema_for("analysis", REQUEST).validate(request.to_wire())

    def test_an_unknown_artifact_kind_is_refused_at_construction(self) -> None:
        with pytest.raises(ValueError):
            ArtifactRef(kind="glossary", path="/p/glossary.md")

    def test_an_artifact_needs_a_path(self) -> None:
        with pytest.raises(ValueError):
            ArtifactRef(kind="tasks", path="   ")

    def test_a_relative_artifact_path_is_resolved_to_an_absolute_one(self) -> None:
        # A provider runs as its own process with its own working directory, so a
        # relative path would resolve against something the engine does not know.
        ref = ArtifactRef.of("tasks", "tasks.md")
        assert ref.path.startswith("/")


class TestResponseValidation:
    def test_a_minimal_valid_response_passes_for_every_capability(self) -> None:
        for capability in DELEGABLE_CAPABILITIES:
            assert validate_response(capability, response_payload(capability)) == ()

    def test_an_unknown_key_is_refused_rather_than_kept(self) -> None:
        payload = response_payload("analysis", coverage_={"processed": []})
        errors = validate_response("analysis", payload)
        assert any(error.path == "coverage_" for error in errors)

    def test_a_missing_coverage_block_is_a_violation(self) -> None:
        payload = response_payload("analysis")
        payload.pop("coverage")
        errors = validate_response("analysis", payload)
        assert any(error.path == "coverage" for error in errors)

    def test_a_response_declaring_an_unpublished_version_is_refused(self) -> None:
        payload = response_payload("analysis", schema_version=99)
        errors = validate_response("analysis", payload)
        assert len(errors) == 1
        assert errors[0].path == "schema_version"
        assert "not published" in errors[0].message

    def test_a_response_with_no_version_is_refused(self) -> None:
        payload = response_payload("analysis")
        payload.pop("schema_version")
        errors = validate_response("analysis", payload)
        assert errors and errors[0].path == "schema_version"

    def test_a_response_that_is_not_an_object_is_refused(self) -> None:
        assert validate_response("analysis", ["findings"])
        assert validate_response("analysis", None)

    def test_a_finding_must_name_its_severity_from_the_vocabulary(self) -> None:
        payload = response_payload(
            "analysis",
            findings=[{"kind": "ambiguity", "severity": "catastrophic", "message": "x"}],
        )
        errors = validate_response("analysis", payload)
        assert any(error.path == "findings[0].severity" for error in errors)

    def test_a_finding_may_reference_the_criteria_it_concerns(self) -> None:
        payload = response_payload(
            "analysis",
            findings=[
                {
                    "kind": "ambiguity",
                    "severity": "warning",
                    "message": "unquantified qualifier",
                    "refs": ["3.2"],
                    "question": {
                        "question": "which threshold?",
                        "choices": ["100ms", "1s"],
                        "consequences": ["tighter", "looser"],
                        "recommended": "100ms",
                    },
                }
            ],
        )
        assert validate_response("analysis", payload) == ()

    def test_a_declared_cost_must_be_a_non_negative_number(self) -> None:
        assert (
            validate_response("analysis", response_payload("analysis", cost={"credits": 0.5})) == ()
        )
        errors = validate_response("analysis", response_payload("analysis", cost={"credits": -1}))
        assert any(error.path == "cost.credits" for error in errors)

    def test_a_skipped_entry_carries_its_item_and_reason(self) -> None:
        payload = response_payload(
            "analysis",
            coverage={"processed": ["requirements"], "skipped": [{"item": "design"}]},
        )
        errors = validate_response("analysis", payload)
        assert any(error.path == "coverage.skipped[0].reason" for error in errors)

    def test_a_response_for_the_wrong_capability_is_refused(self) -> None:
        payload = response_payload("analysis")
        payload["capability"] = "review"
        errors = validate_response("analysis", payload)
        assert any(error.path == "capability" for error in errors)

    def test_validation_reports_every_violation_rather_than_the_first(self) -> None:
        payload = response_payload("analysis")
        payload.pop("coverage")
        payload.pop("findings")
        payload["surprise"] = 1
        errors = validate_response("analysis", payload)
        assert {error.path for error in errors} >= {"coverage", "findings", "surprise"}

    def test_an_unknown_capability_name_is_reported_not_raised(self) -> None:
        errors = validate_response("telepathy", response_payload("analysis"))
        assert errors and "unknown capability" in errors[0].message


class TestTypeAlgebra:
    def test_a_string_may_be_restricted_to_a_vocabulary(self) -> None:
        spec = Str(choices=("a", "b"))
        assert spec.check("a", "f") == []
        assert spec.check("c", "f")
        assert spec.json_schema()["enum"] == ["a", "b"]

    def test_an_oversized_string_is_refused_at_parse_time(self) -> None:
        spec = Str(max_chars=8)
        assert spec.check("x" * 9, "f")

    def test_a_boolean_is_not_an_integer(self) -> None:
        # Python counts True as 1, so accepting it where a count belongs turns a
        # mistake into the value 1.
        assert Int().check(True, "f")
        assert Num().check(False, "f")
        assert Bool().check(1, "f")

    def test_numeric_bounds_are_enforced_and_published(self) -> None:
        spec = Int(minimum=1, maximum=3)
        assert spec.check(0, "f")
        assert spec.check(4, "f")
        assert spec.check(2, "f") == []
        assert spec.json_schema() == {"type": "integer", "minimum": 1, "maximum": 3}

    def test_an_array_reports_the_index_of_each_bad_element(self) -> None:
        errors = Arr(Str(allow_empty=False)).check(["ok", "", 3], "items")
        assert {error.path for error in errors} == {"items[1]", "items[2]"}

    def test_an_oversized_array_is_refused(self) -> None:
        assert Arr(Int(), max_items=2).check([1, 2, 3], "items")

    def test_a_string_is_not_an_array(self) -> None:
        assert Arr(Str()).check("abc", "items")

    def test_an_object_requires_its_fields_unless_declared_optional(self) -> None:
        spec = Obj(fields={"a": Str(), "b": Str()}, optional=frozenset({"b"}))
        assert spec.check({"a": "x"}, "") == []
        assert spec.check({"b": "x"}, "")

    def test_a_map_checks_every_value_and_names_its_key(self) -> None:
        errors = StrMap(Int()).check({"good": 1, "bad": "x"}, "p")
        assert [error.path for error in errors] == ["p.bad"]

    def test_declared_version_rejects_anything_that_is_not_a_positive_int(self) -> None:
        assert declared_version({"schema_version": 2}) == 2
        assert declared_version({"schema_version": True}) is None
        assert declared_version({"schema_version": 0}) is None
        assert declared_version({"schema_version": "1"}) is None
        assert declared_version(["1"]) is None
