"""Document validation: closed schema, section shapes, and vocabularies.

Validation is closed on purpose, so these tests pin refusal as much as
acceptance: a misspelled autonomy level or a stray key that survived silently
would read on the config surface as a restriction that was never applied.
"""

from __future__ import annotations

from typing import Any

from kiro_crew.apps.builtins.spec_engine.engine.config import (
    AUTONOMY_LEVELS,
    ENGINE_FLOOR_CAPABILITIES,
    PROJECT_FIELDS,
    SETTINGS,
    SOURCE_FIELDS,
    validate_config_document,
)
from kiro_crew.apps.builtins.spec_engine.engine.config.settings import SETTING_GROUPS

VALID_DOCUMENT = {
    "version": 1,
    "limits": {"task_retry_limit": 3},
    "budget": {"run_ceiling_credits": 12.5, "warn_fraction": 0.5},
    "capabilities": {
        "analysis": {
            "transport": "mcp",
            "command": ["analyzer", "--stdio"],
            "env": {"ANALYZER_MODE": "strict"},
            "timeout_s": 60,
        },
        "review": {"transport": "builtin"},
    },
    "quality_gates": [
        {
            "name": "unit-tests",
            "position": "pre_submit",
            "severity": "blocking",
            "commands": [["make", "test"]],
        }
    ],
    "cost_profiles": {
        "quality-first": {
            "roles": {
                "design": {"model": "auto", "effort": "high"},
                "implement": {"agent": "coder", "model": "auto"},
            }
        }
    },
    "workflow": {
        "preset": "git-pull-request",
        "stages": {"isolate": [["git", "worktree", "add", "{workspace}"]]},
    },
    "projects": {
        "acme": {
            "path": "/w/acme",
            "cost_profile": "quality-first",
            "base_branch": "main",
            "protected_branches": ["main", "release"],
            "variables": {"reviewer": "team-acme"},
            "limits": {"task_retry_limit": 1},
            "workflow": {"stages": {"verify": [["make", "check"]]}},
        }
    },
    "sources": {
        "acme-issues": {
            "enabled": True,
            "poll": ["gh", "issue", "list", "--json", "number,title"],
            "field_map": {"identifier": "number", "title": "title"},
            "project": "acme",
            "maintainers": ["someone"],
            "spec_types": {"bug": "bugfix", "enhancement": "feature"},
            "autonomy": {
                "maintainer": {"bugfix": "delivery"},
                "external": {"default": "authoring"},
            },
            "spend_cap": {"credits": 100, "period_days": 30},
            "feedback": {"claimed": [["gh", "issue", "comment", "{item_id}"]]},
            "watch": {"interval_s": 120},
        }
    },
}


def _paths(doc: Any) -> list[str]:
    return [e.path for e in validate_config_document(doc)]


class TestVocabularyIntegrity:
    def test_setting_groups_do_not_collide_with_project_or_source_fields(self):
        # A project or source entry holds both, so an overlapping name would
        # make one of the two unreachable.
        assert not SETTING_GROUPS & set(PROJECT_FIELDS)
        assert not SETTING_GROUPS & set(SOURCE_FIELDS)

    def test_declared_project_and_source_fields_are_understood(self):
        # Feed each declared field a value of plainly the wrong type: the error
        # must be about that field, never "unknown field".
        for field in PROJECT_FIELDS:
            errors = validate_config_document({"projects": {"p": {"path": "/w/p", field: 0}}})
            messages = [e.message for e in errors if e.path == f"projects.p.{field}"]
            assert messages and "unknown" not in messages[0], field
        for field in SOURCE_FIELDS:
            doc = {"sources": {"s": {"poll": ["gh"], field: 0}}}
            errors = validate_config_document(doc)
            messages = [e.message for e in errors if e.path == f"sources.s.{field}"]
            assert messages and "unknown" not in messages[0], field

    def test_autonomy_levels_are_ordered_least_to_most_autonomous(self):
        assert AUTONOMY_LEVELS.index("authoring") < AUTONOMY_LEVELS.index("execution")
        assert AUTONOMY_LEVELS.index("execution") < AUTONOMY_LEVELS.index("delivery")
        assert AUTONOMY_LEVELS.index("delivery") < AUTONOMY_LEVELS.index("integration")


class TestDocumentValidation:
    def test_a_fully_populated_document_validates(self):
        assert validate_config_document(VALID_DOCUMENT) == ()

    def test_an_empty_document_validates(self):
        assert validate_config_document({}) == ()

    def test_a_non_object_document_is_rejected(self):
        assert _paths([]) == [""]

    def test_unknown_top_level_key_is_rejected(self):
        assert _paths({"autonomy": {}}) == ["autonomy"]

    def test_unknown_setting_inside_a_known_group_is_rejected(self):
        assert _paths({"limits": {"retries": 2}}) == ["limits.retries"]

    def test_out_of_range_and_wrong_typed_settings_are_rejected(self):
        errors = validate_config_document({"limits": {"task_retry_limit": -1}})
        assert [e.path for e in errors] == ["limits.task_retry_limit"]
        assert "at least 0" in errors[0].message
        assert _paths({"delivery": {"auto_integrate": "yes"}}) == ["delivery.auto_integrate"]
        assert _paths({"concurrency": {"global_max_runs": True}}) == ["concurrency.global_max_runs"]

    def test_app_only_setting_cannot_be_overridden_per_project(self):
        doc = {"projects": {"acme": {"path": "/w", "concurrency": {"global_max_runs": 2}}}}
        errors = validate_config_document(doc)
        assert [e.path for e in errors] == ["projects.acme.concurrency.global_max_runs"]
        assert "not overridable at project scope" in errors[0].message

    def test_project_only_setting_cannot_be_overridden_per_source(self):
        doc = {"sources": {"s": {"poll": ["gh"], "limits": {"task_retry_limit": 1}}}}
        assert _paths(doc) == ["sources.s.limits.task_retry_limit"]

    def test_project_requires_a_path(self):
        assert _paths({"projects": {"acme": {"base_branch": "main"}}}) == ["projects.acme.path"]

    def test_source_requires_a_poll_command(self):
        assert _paths({"sources": {"s": {"project": "acme"}}}) == ["sources.s.poll"]

    def test_a_per_class_screening_opt_out_validates(self):
        doc = {"sources": {"s": {"poll": ["gh"], "screening": {"maintainer": False}}}}
        assert _paths(doc) == []

    def test_screening_refuses_the_default_key_that_would_disable_it_for_everyone(self):
        doc = {"sources": {"s": {"poll": ["gh"], "screening": {"default": False}}}}
        errors = validate_config_document(doc)
        assert [e.path for e in errors] == ["sources.s.screening.default"]
        # The reader refuses it too. Enforcing it here as well is what lets an
        # operator find out at the moment they save, instead of saving a setting
        # that is silently ignored and believing screening is off.
        assert "no single setting" in errors[0].message

    def test_screening_rejects_an_unknown_class_and_a_non_boolean(self):
        doc = {"sources": {"s": {"poll": ["gh"], "screening": {"maintainerz": False}}}}
        assert _paths(doc) == ["sources.s.screening.maintainerz"]
        doc = {"sources": {"s": {"poll": ["gh"], "screening": {"external": "no"}}}}
        assert _paths(doc) == ["sources.s.screening.external"]

    def test_engine_floor_capability_cannot_be_bound(self):
        for capability in ENGINE_FLOOR_CAPABILITIES:
            doc = {"capabilities": {capability: {"transport": "mcp", "command": ["x"]}}}
            errors = validate_config_document(doc)
            assert [e.path for e in errors] == [f"capabilities.{capability}"]
            assert "engine-floor" in errors[0].message

    def test_delegated_transport_requires_a_command(self):
        assert _paths({"capabilities": {"analysis": {"transport": "command"}}}) == [
            "capabilities.analysis.command"
        ]

    def test_builtin_transport_rejects_a_command(self):
        doc = {"capabilities": {"analysis": {"transport": "builtin", "command": ["x"]}}}
        assert _paths(doc) == ["capabilities.analysis.command"]

    def test_unknown_transport_is_rejected(self):
        doc = {"capabilities": {"analysis": {"transport": "grpc"}}}
        assert _paths(doc) == ["capabilities.analysis.transport"]

    def test_commands_must_be_argv_lists_not_shell_strings(self):
        # Substitution builds argv and never hands a string to a shell, so the
        # schema has nowhere to put one.
        doc = {"workflow": {"stages": {"submit": ["gh pr create --fill"]}}}
        assert _paths(doc) == ["workflow.stages.submit[0]"]

    def test_empty_command_lists_are_rejected(self):
        assert _paths({"workflow": {"stages": {"submit": []}}}) == ["workflow.stages.submit"]
        assert _paths({"workflow": {"stages": {"submit": [[]]}}}) == ["workflow.stages.submit[0]"]

    def test_unknown_delivery_stage_is_rejected(self):
        doc = {"workflow": {"stages": {"deploy": [["make", "deploy"]]}}}
        assert _paths(doc) == ["workflow.stages.deploy"]

    def test_quality_gate_fields_are_checked(self):
        doc = {
            "quality_gates": [
                {"name": "", "position": "later", "severity": "fatal", "commands": [["x"]]}
            ]
        }
        assert _paths(doc) == [
            "quality_gates[0].name",
            "quality_gates[0].position",
            "quality_gates[0].severity",
        ]

    def test_duplicate_quality_gate_names_are_rejected(self):
        gate = {
            "name": "lint",
            "position": "pre_submit",
            "severity": "advisory",
            "commands": [["lint"]],
        }
        assert _paths({"quality_gates": [gate, dict(gate)]}) == ["quality_gates[1].name"]

    def test_unknown_role_and_effort_are_rejected(self):
        doc = {"cost_profiles": {"p": {"roles": {"typing": {"model": "auto"}}}}}
        assert _paths(doc) == ["cost_profiles.p.roles.typing"]
        doc = {"cost_profiles": {"p": {"roles": {"review": {"model": "auto", "effort": "epic"}}}}}
        assert _paths(doc) == ["cost_profiles.p.roles.review.effort"]

    def test_autonomy_keys_and_levels_are_checked(self):
        doc = {"sources": {"s": {"poll": ["gh"], "autonomy": {"maintainer": {"bugfix": "yolo"}}}}}
        assert _paths(doc) == ["sources.s.autonomy.maintainer.bugfix"]
        doc = {"sources": {"s": {"poll": ["gh"], "autonomy": {"friend": {"bugfix": "delivery"}}}}}
        assert _paths(doc) == ["sources.s.autonomy.friend"]

    def test_wildcard_keys_are_accepted_in_autonomy(self):
        doc = {
            "sources": {"s": {"poll": ["gh"], "autonomy": {"default": {"default": "authoring"}}}}
        }
        assert validate_config_document(doc) == ()

    def test_spend_cap_requires_positive_amounts(self):
        doc = {"sources": {"s": {"poll": ["gh"], "spend_cap": {"credits": 0, "period_days": 0}}}}
        assert _paths(doc) == ["sources.s.spend_cap.credits", "sources.s.spend_cap.period_days"]

    def test_unknown_lifecycle_event_in_feedback_is_rejected(self):
        doc = {"sources": {"s": {"poll": ["gh"], "feedback": {"merged": [["gh", "x"]]}}}}
        assert _paths(doc) == ["sources.s.feedback.merged"]

    def test_version_must_be_a_positive_integer(self):
        assert _paths({"version": 0}) == ["version"]
        assert _paths({"version": True}) == ["version"]

    def test_every_setting_default_survives_a_round_trip_through_validation(self):
        doc: dict = {}
        for setting in SETTINGS.values():
            doc.setdefault(setting.group, {})[setting.leaf] = setting.default
        assert validate_config_document(doc) == ()
