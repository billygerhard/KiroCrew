"""Cost profiles as configuration: what a profile may pin, and what it earns a warning for.

What these tests hold: a profile may pin exactly the two settings the registry
allows it to and nothing else, a pinned setting sits between the project layer and
the app layer with its own origin, and an agent assigned to a role is checked
against its tool surface at configuration time — the moment a human is present —
rather than when the run needs it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.apps.builtins.spec_engine.engine.config import (
    AGENT_MISSING_ENGINE_TOOLS,
    AGENT_NOT_INSTALLED,
    DASHBOARD_SURFACE,
    ENGINE_MCP_SERVER,
    PROFILE_SETTING_KEYS,
    AgentToolSurface,
    ConfigStore,
    ConfigValidationError,
    ConfigWarning,
    DiskAgentSurfaces,
    ValueOrigin,
    disk_lookup,
    document_warnings,
    profiles,
    selected_profile,
    validate_config_document,
)

PROJECT = "acme"
PROFILE = "budget"
ENGINE_REF = f"@{ENGINE_MCP_SERVER}"


def profile_document(
    *,
    roles: dict[str, Any] | None = None,
    pins: dict[str, Any] | None = None,
    project_path: str = "/w/acme",
    project_settings: dict[str, Any] | None = None,
    app_settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    profile: dict[str, Any] = {"roles": roles if roles is not None else {}}
    profile.update(pins or {})
    project: dict[str, Any] = {"path": project_path, "cost_profile": PROFILE}
    project.update(project_settings or {})
    document: dict[str, Any] = {
        "cost_profiles": {PROFILE: profile},
        "projects": {PROJECT: project},
    }
    document.update(app_settings or {})
    return document


def write_agent(directory: Path, name: str, tools: list[str] | None) -> Path:
    """Write a kiro-cli agent configuration; ``None`` declares no allowlist."""
    directory.mkdir(parents=True, exist_ok=True)
    body: dict[str, Any] = {"name": name}
    if tools is not None:
        body["tools"] = tools
    path = directory / f"{name}.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


class TestProfileSchema:
    def test_a_profile_may_pin_the_settings_the_registry_allows(self):
        document = profile_document(
            roles={"implement": {"model": "model-one"}},
            pins={
                "concurrency": {"wave_max_tasks": 1},
                "budget": {"run_ceiling_credits": 0.5},
            },
        )
        assert validate_config_document(document) == ()

    def test_a_profile_may_not_pin_any_other_setting(self):
        errors = validate_config_document(
            profile_document(
                roles={"implement": {"model": "model-one"}},
                pins={"limits": {"task_retry_limit": 9}},
            )
        )
        assert [error.path for error in errors] == [
            f"cost_profiles.{PROFILE}.limits.task_retry_limit"
        ]

    def test_a_pinned_value_is_held_to_the_settings_own_bounds(self):
        errors = validate_config_document(
            profile_document(
                roles={"implement": {"model": "model-one"}},
                pins={"concurrency": {"wave_max_tasks": 0}},
            )
        )
        assert errors and "at least 1" in errors[0].message

    def test_every_pinnable_key_is_reachable_through_the_schema(self):
        for key in PROFILE_SETTING_KEYS:
            group, leaf = key.split(".", 1)
            document = profile_document(
                roles={"implement": {"model": "model-one"}},
                pins={group: {leaf: 1}},
            )
            assert validate_config_document(document) == (), key

    def test_an_unknown_profile_field_is_still_refused(self):
        errors = validate_config_document(profile_document(roles={}, pins={"nonsense": {"x": 1}}))
        assert [error.message for error in errors] == ["unknown profile field"]


class TestProfileParsing:
    def test_a_project_selects_its_profile_by_name(self):
        document = profile_document(roles={"review": {"model": "model-one"}})
        selected, requested = selected_profile(document, PROJECT)
        assert requested == PROFILE
        assert selected is not None
        assert selected.assignment("review") is not None

    def test_a_role_the_schema_does_not_know_is_not_parsed_as_one(self):
        document = profile_document(roles={"design": {"model": "model-one"}})
        document["cost_profiles"][PROFILE]["roles"]["invented"] = {"model": "model-two"}
        parsed = profiles(document)[PROFILE]
        assert set(parsed.assignments) == {"design"}

    def test_an_unusable_profile_entry_is_skipped_rather_than_raised_on(self):
        # Read on the dispatch path: one malformed profile must not take down the
        # resolution of every other one.
        document = {"cost_profiles": {PROFILE: ["not", "an", "object"], "other": {"roles": {}}}}
        assert set(profiles(document)) == {"other"}


class TestProfilePinPrecedence:
    def test_a_pinned_setting_resolves_with_its_own_origin(self, tmp_path: Path):
        store = ConfigStore(tmp_path / "state")
        store.write(
            profile_document(
                roles={"implement": {"model": "model-one"}},
                pins={"budget": {"run_ceiling_credits": 0.5}},
            ),
            surface=DASHBOARD_SURFACE,
        )
        effective = store.effective("budget.run_ceiling_credits", project=PROJECT)
        assert effective.value == 0.5
        assert effective.origin is ValueOrigin.COST_PROFILE
        assert effective.declared_at == f"cost_profiles.{PROFILE}.budget.run_ceiling_credits"

    def test_a_pin_beats_the_app_wide_value(self, tmp_path: Path):
        store = ConfigStore(tmp_path / "state")
        store.write(
            profile_document(
                roles={},
                pins={"concurrency": {"wave_max_tasks": 1}},
                app_settings={"concurrency": {"wave_max_tasks": 8}},
            ),
            surface=DASHBOARD_SURFACE,
        )
        assert store.effective("concurrency.wave_max_tasks", project=PROJECT).value == 1
        # No project, no selected profile: the app-wide value stands.
        assert store.effective("concurrency.wave_max_tasks").value == 8

    def test_the_projects_own_value_beats_the_profile_it_selected(self, tmp_path: Path):
        store = ConfigStore(tmp_path / "state")
        store.write(
            profile_document(
                roles={},
                pins={"concurrency": {"wave_max_tasks": 1}},
                project_settings={"concurrency": {"wave_max_tasks": 5}},
            ),
            surface=DASHBOARD_SURFACE,
        )
        effective = store.effective("concurrency.wave_max_tasks", project=PROJECT)
        assert effective.value == 5
        assert effective.origin is ValueOrigin.PROJECT_CONFIG

    def test_an_unpinned_setting_still_resolves_to_its_default(self, tmp_path: Path):
        store = ConfigStore(tmp_path / "state")
        store.write(profile_document(roles={}), surface=DASHBOARD_SURFACE)
        effective = store.effective("budget.run_ceiling_credits", project=PROJECT)
        assert effective.origin is ValueOrigin.BUNDLED_DEFAULT

    def test_an_out_of_range_pin_is_named_rather_than_replaced(self, tmp_path: Path):
        store = ConfigStore(tmp_path / "state")
        # Written past the validated door, as a hand edit would be.
        store.root.mkdir(parents=True, exist_ok=True)
        store.path.write_text(
            json.dumps(profile_document(roles={}, pins={"concurrency": {"wave_max_tasks": -3}})),
            encoding="utf-8",
        )
        with pytest.raises(ConfigValidationError):
            store.effective("concurrency.wave_max_tasks", project=PROJECT)


class TestAgentToolSurface:
    def test_an_agent_with_no_allowlist_reaches_every_tool(self):
        assert AgentToolSurface(name="a", found=True, tools=None).grants(ENGINE_MCP_SERVER)

    def test_a_whole_server_grant_counts(self):
        assert AgentToolSurface(name="a", found=True, tools=(ENGINE_REF,)).grants()

    def test_a_wildcard_grant_counts(self):
        assert AgentToolSurface(name="a", found=True, tools=("*",)).grants()

    def test_a_per_tool_grant_counts(self):
        surface = AgentToolSurface(name="a", found=True, tools=(f"{ENGINE_REF}/spec_status",))
        assert surface.grants()

    def test_an_allowlist_without_the_server_does_not_grant(self):
        assert not AgentToolSurface(name="a", found=True, tools=("read", "@git")).grants()

    def test_an_empty_allowlist_grants_nothing(self):
        assert not AgentToolSurface(name="a", found=True, tools=()).grants()

    def test_an_agent_that_was_not_found_grants_nothing(self):
        assert not AgentToolSurface(name="a", found=False).grants()

    def test_a_server_whose_name_only_prefixes_the_engine_is_not_the_engine(self):
        surface = AgentToolSurface(name="a", found=True, tools=(f"{ENGINE_REF}-other",))
        assert not surface.grants()


class TestDiskAgentSurfaces:
    def test_an_agent_is_read_from_its_file(self, tmp_path: Path):
        write_agent(tmp_path / "agents", "reviewer", [ENGINE_REF])
        surface = DiskAgentSurfaces((tmp_path / "agents",))("reviewer")
        assert surface.found
        assert surface.grants()

    def test_an_agent_saved_under_another_filename_is_still_found(self, tmp_path: Path):
        directory = tmp_path / "agents"
        directory.mkdir(parents=True)
        (directory / "custom.json").write_text(
            json.dumps({"name": "reviewer", "tools": ["read"]}), encoding="utf-8"
        )
        surface = DiskAgentSurfaces((directory,))("reviewer")
        assert surface.found
        assert not surface.grants()

    def test_a_missing_directory_is_not_an_error(self, tmp_path: Path):
        assert not DiskAgentSurfaces((tmp_path / "absent",))("reviewer").found

    def test_a_malformed_agent_file_is_not_read_as_an_agent(self, tmp_path: Path):
        directory = tmp_path / "agents"
        directory.mkdir(parents=True)
        (directory / "reviewer.json").write_text("{not json", encoding="utf-8")
        assert not DiskAgentSurfaces((directory,))("reviewer").found

    def test_the_first_directory_in_search_order_wins(self, tmp_path: Path):
        write_agent(tmp_path / "project", "reviewer", [ENGINE_REF])
        write_agent(tmp_path / "user", "reviewer", ["read"])
        lookup = DiskAgentSurfaces((tmp_path / "project", tmp_path / "user"))
        assert lookup("reviewer").grants()

    def test_a_projects_agents_directory_is_searched(self, tmp_path: Path):
        project = tmp_path / "checkout"
        write_agent(project / ".kiro" / "agents", "reviewer", [ENGINE_REF])
        document = profile_document(
            roles={"review": {"agent": "reviewer", "model": "model-one"}},
            project_path=str(project),
        )
        assert disk_lookup(document)("reviewer").grants()


class TestAssignedAgentAdvisories:
    def test_an_agent_lacking_the_engine_tools_is_warned_about(self):
        document = profile_document(roles={"review": {"agent": "reviewer", "model": "model-one"}})
        warnings = document_warnings(
            document,
            agents=lambda name: AgentToolSurface(name=name, found=True, tools=("read",)),
        )
        assert [warning.code for warning in warnings] == [AGENT_MISSING_ENGINE_TOOLS]
        assert warnings[0].path == f"cost_profiles.{PROFILE}.roles.review.agent"
        assert ENGINE_REF in warnings[0].message

    def test_an_agent_with_the_engine_tools_earns_no_warning(self):
        document = profile_document(roles={"review": {"agent": "reviewer", "model": "model-one"}})
        warnings = document_warnings(
            document,
            agents=lambda name: AgentToolSurface(name=name, found=True, tools=(ENGINE_REF,)),
        )
        assert warnings == ()

    def test_an_agent_with_no_configuration_is_warned_about_separately(self):
        document = profile_document(roles={"review": {"agent": "ghost", "model": "model-one"}})
        warnings = document_warnings(
            document, agents=lambda name: AgentToolSurface(name=name, found=False)
        )
        assert [warning.code for warning in warnings] == [AGENT_NOT_INSTALLED]

    def test_a_role_with_no_assigned_agent_is_not_checked(self):
        document = profile_document(roles={"review": {"model": "model-one"}})

        def refuse(name: str) -> AgentToolSurface:
            raise AssertionError(f"no agent is assigned, so {name!r} must not be looked up")

        assert document_warnings(document, agents=refuse) == ()

    def test_one_agent_assigned_to_several_roles_is_reported_once(self):
        document = profile_document(
            roles={
                "review": {"agent": "reviewer", "model": "model-one"},
                "design": {"agent": "reviewer", "model": "model-one"},
            }
        )
        warnings = document_warnings(
            document,
            agents=lambda name: AgentToolSurface(name=name, found=True, tools=("read",)),
        )
        assert len(warnings) == 1

    def test_the_warning_arrives_at_configuration_time_from_the_write_path(self, tmp_path: Path):
        # The whole point of the check: the operator hears about it while saving,
        # not when a run needs the agent hours later.
        checkout = tmp_path / "checkout"
        write_agent(checkout / ".kiro" / "agents", "narrow-reviewer", ["read", "@git"])
        store = ConfigStore(tmp_path / "state")
        recorded: list[ConfigWarning] = []
        store.write(
            profile_document(
                roles={"review": {"agent": "narrow-reviewer", "model": "model-one"}},
                project_path=str(checkout),
            ),
            surface=DASHBOARD_SURFACE,
            warn=recorded.append,
        )
        assert [warning.code for warning in recorded] == [AGENT_MISSING_ENGINE_TOOLS]
        assert store.path.exists()

    def test_a_granted_agent_writes_cleanly(self, tmp_path: Path):
        checkout = tmp_path / "checkout"
        write_agent(checkout / ".kiro" / "agents", "wide-reviewer", ["read", ENGINE_REF])
        store = ConfigStore(tmp_path / "state")
        recorded: list[ConfigWarning] = []
        store.write(
            profile_document(
                roles={"review": {"agent": "wide-reviewer", "model": "model-one"}},
                project_path=str(checkout),
            ),
            surface=DASHBOARD_SURFACE,
            warn=recorded.append,
        )
        assert recorded == []
