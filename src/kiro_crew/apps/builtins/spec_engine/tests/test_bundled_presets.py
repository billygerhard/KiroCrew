"""Bundled presets: watch sources, delivery workflows, and cost profiles.

Three tables, one set of guarantees, because a preset is the same kind of object
in each case: a starting point a project copies into its configuration and edits
there. That makes two properties worth pinning per table and one across all of
them.

* **A copy is a copy, all the way down.** An accessor that returned anything
  sharing structure with the bundled table would let one project's edit change
  what every later project is offered in the same process. The tests reach into
  the nesting deliberately: a shallow-copy accessor passes a top-level mutation
  test while a nested list is still shared.
* **What comes out is configuration the schema accepts.** A preset whose keys the
  validator refuses is not a starting point, it is a broken paste. Each table's
  output is validated as the document section it is written into.
* **Public hosts only, structurally.** The watch source table is a closed literal
  and every miss raises, so no name -- including one supplied by configuration --
  yields a preset for a private tracker.
"""

from __future__ import annotations

from typing import Any

import pytest

from kiro_crew.apps.builtins.spec_engine.engine.config import DASHBOARD_SURFACE, ConfigStore
from kiro_crew.apps.builtins.spec_engine.engine.config.profiles import (
    COST_PROFILE_PRESET_NAMES,
    cost_profile_presets,
    profiles,
)
from kiro_crew.apps.builtins.spec_engine.engine.config.schema import (
    DELIVERY_STAGES,
    PROFILE_SETTING_KEYS,
    ROLES,
    SOURCE_FIELDS,
    validate_config_document,
)
from kiro_crew.apps.builtins.spec_engine.engine.delivery.isolation import git_isolate_commands
from kiro_crew.apps.builtins.spec_engine.engine.delivery.templates import CommandTemplate
from kiro_crew.apps.builtins.spec_engine.engine.delivery.variables import RUN_CONTEXT_VARIABLES
from kiro_crew.apps.builtins.spec_engine.engine.delivery.workflow import (
    ISOLATE_STAGE,
    WORKFLOW_PRESET_NAMES,
    workflow_presets,
)
from kiro_crew.apps.builtins.spec_engine.engine.roles import (
    Dispatch,
    RolePlan,
    SessionDefault,
    WorkKind,
)
from kiro_crew.apps.builtins.spec_engine.engine.watch import (
    BUNDLED_SCREENING_GUIDANCE,
    ITEM_FIELDS,
    WATCH_SOURCE_PRESET_HOSTS,
    WATCH_SOURCE_PRESET_PROGRAMS,
    WATCH_SOURCE_PRESETS,
    WatchSource,
    watch_source_presets,
)
from kiro_crew.effort import EFFORT_LEVELS

#: A model that accepts a reasoning effort, for the tests that ask what a bundled
#: preset's effort pin actually does. Concrete ids appear only in test data.
EFFORT_CAPABLE_MODEL = "claude-sonnet-4.6"


def resolve_from_preset(name: str, kind: WorkKind, *, model: str) -> Dispatch:
    """Resolve one dispatch from a bundled preset through the real resolver.

    The preset is written into a document as a project's selected profile and
    read back, so what is measured is what a run would actually be dispatched
    with -- not what the table says. *model* stands in for the role's model,
    which is how these tests separate "the pin is declared" from "the pin
    reaches the wire".
    """
    preset = cost_profile_presets(name)
    for assignment in preset["roles"].values():
        assignment["model"] = model
    document = {
        "cost_profiles": {name: preset},
        "projects": {"acme": {"path": "/acme", "cost_profile": name}},
    }
    plan = RolePlan.from_document(
        document,
        project="acme",
        session_default=SessionDefault(agent="session-agent", model="auto"),
    )
    return plan.dispatch(kind)


#: One item as each host's CLI actually emits it, so the field map is exercised
#: against the shape it was written for rather than against engine field names.
#: One item as each host's poll command actually emits it.
#:
#: The GitHub entry is the REST shape returned by ``gh api repos/O/R/issues``,
#: captured from a live call, NOT hand-authored to match the field map. That
#: distinction is the whole value of this fixture: the previous version was
#: written to agree with the map, so it confirmed the map against its own echo
#: and passed while every real poll failed -- ``gh issue list`` has no author
#: association in its ``--json`` vocabulary at all. A payload that merely
#: restates the thing under test cannot catch that.
#:
#: ``pull_request`` is present on the second GitHub entry because this endpoint
#: returns pull requests as issues; the preset's ``--jq`` filter drops them.
HOST_PAYLOADS: dict[str, dict[str, Any]] = {
    "github": {
        "number": 412,
        "title": "Crash on empty input",
        "body": "Steps to reproduce...",
        "state": "open",
        "html_url": "https://github.com/owner/repo/issues/412",
        "url": "https://api.github.com/repos/owner/repo/issues/412",
        "labels": [{"name": "bug"}, {"name": "triage"}],
        "user": {"login": "octocat"},
        "author_association": "CONTRIBUTOR",
    },
    "gitlab": {
        "iid": 77,
        "title": "Crash on empty input",
        "description": "Steps to reproduce...",
        "state": "opened",
        "web_url": "https://gitlab.com/owner/repo/-/issues/77",
        "labels": ["bug", "triage"],
        "author": {"username": "octocat"},
    },
}


@pytest.fixture()
def store(tmp_path: Any) -> ConfigStore:
    return ConfigStore(tmp_path / "state")


def source_from_preset(store: ConfigStore, host: str, *, name: str = "upstream") -> WatchSource:
    """Write *host*'s preset into configuration and load the source it defines.

    Going through the write path rather than constructing a ``WatchSource``
    directly is the point: it proves the preset is a document the validator
    accepts and the loader reads, not just a dict with plausible keys.
    """
    store.write({"sources": {name: watch_source_presets(host)}}, surface=DASHBOARD_SURFACE)
    return WatchSource.load(store, name)


class TestWatchPresetsArePublicHostsOnly:
    def test_only_the_public_hosts_are_bundled(self) -> None:
        assert set(WATCH_SOURCE_PRESETS) == {"github", "gitlab"}
        assert WATCH_SOURCE_PRESET_HOSTS == ("github", "gitlab")

    def test_an_unbundled_host_raises_rather_than_inventing_a_preset(self) -> None:
        with pytest.raises(KeyError):
            watch_source_presets("internal-tracker")

    def test_a_mistyped_host_cannot_yield_a_source_definition_at_all(self) -> None:
        """Asserts the harm, not the raise.

        There is no registration path and no fallback, so the only thing a
        non-public name can produce is nothing. If a miss ever returned an empty
        or fabricated entry, an operator would end up with a declared source
        whose poll command came from somewhere unbundled.
        """
        built: dict[str, Any] = {}
        try:
            built = watch_source_presets("github-enterprise")
        except KeyError:
            pass
        assert built == {}, "a miss produced a usable source entry instead of refusing"
        # The positive half, so this cannot pass by the accessor always failing.
        assert watch_source_presets("github")

    def test_the_preset_programs_are_the_public_host_clis_only(self) -> None:
        assert set(WATCH_SOURCE_PRESET_PROGRAMS.values()) == {"gh", "glab"}


class TestWatchPresetsAreDeepCopies:
    @pytest.mark.parametrize("host", ["github", "gitlab"])
    def test_mutating_a_copy_deeply_leaves_the_bundled_table_pristine(self, host: str) -> None:
        """The whole safety property, exercised past the top level.

        A shallow copy passes a test that only reassigns a top-level key, so this
        mutates the nested poll list in place and the nested field map in place --
        the two edits a configuration surface actually makes.
        """
        first = watch_source_presets(host)
        pristine_poll = list(first["poll"])
        pristine_map = dict(first["field_map"])

        first["poll"].append("--label=injected")
        first["poll"][0] = "not-the-program"
        first["field_map"]["identifier"] = "injected"
        first["field_map"]["extra"] = "injected"

        second = watch_source_presets(host)
        assert second["poll"] == pristine_poll
        assert second["field_map"] == pristine_map

    @pytest.mark.parametrize("host", ["github", "gitlab"])
    def test_two_copies_share_no_container(self, host: str) -> None:
        first = watch_source_presets(host)
        second = watch_source_presets(host)
        assert first["poll"] is not second["poll"]
        assert first["field_map"] is not second["field_map"]


class TestWatchPresetsAreUsableConfiguration:
    @pytest.mark.parametrize("host", ["github", "gitlab"])
    def test_the_preset_is_a_source_entry_the_validator_accepts(self, host: str) -> None:
        assert validate_config_document({"sources": {"upstream": watch_source_presets(host)}}) == ()

    @pytest.mark.parametrize("host", ["github", "gitlab"])
    def test_the_preset_carries_only_source_fields(self, host: str) -> None:
        """No key the source schema does not own.

        This is the structural version of the validation test above: a preset that
        carried, say, its own program or its own health opinion would be refused as
        an unknown source field the moment it was written.
        """
        assert set(watch_source_presets(host)) <= set(SOURCE_FIELDS)

    @pytest.mark.parametrize("host", ["github", "gitlab"])
    def test_a_copied_preset_is_inert_until_enablement_is_declared(
        self, store: ConfigStore, host: str
    ) -> None:
        """A freshly copied preset still holds a repository placeholder.

        Polling is what decides an unattended run may start, so the preset must
        not arrive enabled. Absence of the key, not ``enabled: false``, because
        the loader's default is what the rest of the engine reads.
        """
        assert "enabled" not in watch_source_presets(host)
        assert source_from_preset(store, host).enabled is False

    @pytest.mark.parametrize("host", ["github", "gitlab"])
    def test_the_loaded_source_runs_the_program_the_preset_advertises(
        self, store: ConfigStore, host: str
    ) -> None:
        """Pins the advertised program to the one the poll argv actually runs.

        Two spellings of a program name is how a preset comes to name a tool its
        own command does not run -- and the program name is already the identifier
        that source health and the doctor's prerequisite check agree on.
        """
        source = source_from_preset(store, host)
        assert source.program == WATCH_SOURCE_PRESET_PROGRAMS[host]
        assert source.program == watch_source_presets(host)["poll"][0]

    @pytest.mark.parametrize("host", ["github", "gitlab"])
    def test_the_preset_records_where_it_came_from(self, store: ConfigStore, host: str) -> None:
        assert source_from_preset(store, host).preset == host

    @pytest.mark.parametrize("host", ["github", "gitlab"])
    def test_the_poll_command_references_no_variables(self, host: str) -> None:
        """A poll has no run context, so a poll command that referenced a variable
        would be refused by the poller the moment the preset was enabled. The
        repository is therefore a literal placeholder an operator edits."""
        argv = watch_source_presets(host)["poll"]
        assert CommandTemplate.parse(argv).variables == ()

    @pytest.mark.parametrize("host", ["github", "gitlab"])
    def test_the_preset_declares_its_items_publicly_submittable(self, host: str) -> None:
        """Which is what earns the public-source advisory when autonomy is armed."""
        assert watch_source_presets(host)["public"] is True


class TestWatchPresetFieldMaps:
    def test_the_github_map_reads_a_real_gh_issue(self, store: ConfigStore) -> None:
        source = source_from_preset(store, "github")
        values, problems = source.field_map.extract(HOST_PAYLOADS["github"])
        assert problems == ()
        assert values["identifier"] == "412"
        assert values["address"] == "https://github.com/owner/repo/issues/412"
        assert values["classification"] == "bug"
        assert values["submitter"] == "octocat"
        assert values["association"] == "CONTRIBUTOR"

    def test_the_address_is_the_browsable_one_not_the_api_endpoint(
        self, store: ConfigStore
    ) -> None:
        """The REST payload carries both, and ``url`` is the API endpoint. An
        address is somewhere a person is sent, so aiming it at ``url`` would
        produce a working-looking value that is useless to the human who
        receives it -- a wrong answer rather than a missing one, which is why
        the fixture deliberately carries both keys."""
        source = source_from_preset(store, "github")
        values, _ = source.field_map.extract(HOST_PAYLOADS["github"])
        assert values["address"] == HOST_PAYLOADS["github"]["html_url"]
        assert values["address"] != HOST_PAYLOADS["github"]["url"]

    def test_the_github_poll_asks_a_command_that_can_answer_about_association(
        self, store: ConfigStore
    ) -> None:
        """``gh issue list`` cannot report an author association -- the field is
        absent from its ``--json`` vocabulary, so requesting it fails the entire
        poll rather than omitting one value. The association is what decides how
        much autonomy a stranger's issue commands, so the preset has to reach a
        command that actually carries it.

        This pins the pairing between the argv and the map, which is the part a
        payload fixture cannot check: a fixture proves the map reads a shape, not
        that the command emits that shape.
        """
        argv = list(WATCH_SOURCE_PRESETS["github"]["poll"])
        assert argv[:2] == ["gh", "api"], argv
        assert "issue" not in argv and "list" not in argv, argv
        association = WATCH_SOURCE_PRESETS["github"]["field_map"]["association"]
        assert association == "author_association", association

    def test_the_github_poll_excludes_pull_requests(self, store: ConfigStore) -> None:
        """GitHub returns pull requests from the issues endpoint -- they are the
        same object to it. Without the filter every open pull request becomes a
        watched work item, so the engine would start runs for its own review
        submissions. ``pull_request`` is the only key that distinguishes them.
        """
        argv = list(WATCH_SOURCE_PRESETS["github"]["poll"])
        assert "--jq" in argv, argv
        assert "pull_request" in argv[argv.index("--jq") + 1]

    def test_the_github_poll_reports_closed_items_so_cancellation_can_fire(
        self, store: ConfigStore
    ) -> None:
        """Lifecycle derives a cancellation only from a closure a poll REPORTS,
        reading absence as a narrowed filter instead. A preset listing only open
        items therefore cannot ever cancel a run whose issue was closed."""
        argv = list(WATCH_SOURCE_PRESETS["github"]["poll"])
        assert any("state=all" in part for part in argv), argv

    def test_the_bundled_table_itself_refuses_an_in_place_edit(self) -> None:
        """The accessor's deep copy protects callers who go through it. A direct
        importer does not, and one project mutating this table in place would
        change what every later project in the process is offered -- so the inner
        mapping is a read-only view rather than a dict trusted not to be written.
        """
        for host in WATCH_SOURCE_PRESET_HOSTS:
            with pytest.raises(TypeError):
                WATCH_SOURCE_PRESETS[host]["field_map"]["submitter"] = "attacker"  # type: ignore[index]  # noqa: E501

    def test_only_github_can_derive_a_cancellation_and_that_asymmetry_is_deliberate(
        self,
    ) -> None:
        """An honest record of a gap rather than a guessed fix.

        Lifecycle derives a cancellation only from a closure a poll REPORTS. The
        GitHub preset asks for ``state=all`` and so can report one. The GitLab
        preset cannot: ``glab issue list`` defaults to open items, and the flag
        that widens it could not be verified here because ``glab`` is not
        installed on this machine. Guessing one is precisely the defect this
        preset table already shipped once -- a plausible flag that the real CLI
        rejects, which failed every poll rather than degrading.

        So the asymmetry is pinned instead: a GitLab source cannot cancel a run
        whose issue was closed, and this test fails the day the GitLab argv gains
        a state filter, at which point the claim above is what to correct.
        """
        github = list(WATCH_SOURCE_PRESETS["github"]["poll"])
        gitlab = list(WATCH_SOURCE_PRESETS["gitlab"]["poll"])
        assert any("state=all" in part for part in github)
        assert not any("state" in part for part in gitlab), (
            "the GitLab preset gained a state filter -- verify it against a real "
            "glab before claiming cancellation works for GitLab"
        )

    def test_the_gitlab_map_reads_a_real_glab_issue(self, store: ConfigStore) -> None:

        """GitLab's shapes differ from GitHub's in three places at once: the
        identifier is ``iid``, the body is ``description``, and labels are bare
        strings rather than objects. Reading them through the same extractor is
        what makes the second preset more than a copy of the first."""
        source = source_from_preset(store, "gitlab")
        values, problems = source.field_map.extract(HOST_PAYLOADS["gitlab"])
        assert problems == ()
        assert values["identifier"] == "77"
        assert values["body"] == "Steps to reproduce..."
        assert values["classification"] == "bug"
        assert values["submitter"] == "octocat"

    def test_gitlab_reports_no_author_association_and_that_resolves_to_undetermined(
        self, store: ConfigStore
    ) -> None:
        """The case that does NOT work, and must fail in the safe direction.

        GitLab has no equivalent of GitHub's author association. The preset leaves
        the field unmapped rather than aiming it at something that is not one, so
        it resolves to empty -- which submitter classification reads as
        undetermined and therefore least-trusted.
        """
        source = source_from_preset(store, "gitlab")
        values, problems = source.field_map.extract(HOST_PAYLOADS["gitlab"])
        assert values["association"] == ""
        assert problems == (), "an absent field is not a mapping problem"

    @pytest.mark.parametrize("host", ["github", "gitlab"])
    def test_every_mapped_name_is_an_engine_item_field(self, host: str) -> None:
        assert set(watch_source_presets(host)["field_map"]) <= set(ITEM_FIELDS)

    @pytest.mark.parametrize("host", ["github", "gitlab"])
    def test_the_identifier_resolves_because_nothing_dispatches_without_it(
        self, store: ConfigStore, host: str
    ) -> None:
        source = source_from_preset(store, host)
        values, _ = source.field_map.extract(HOST_PAYLOADS[host])
        assert values["identifier"].strip()


class TestWorkflowPresets:
    def test_the_bundled_set_is_the_one_the_requirements_name(self) -> None:
        assert WORKFLOW_PRESET_NAMES == ("git-pull-request", "git-merge-request", "local-only")

    def test_an_unknown_name_raises_rather_than_yielding_an_empty_workflow(self) -> None:
        """An empty workflow is not a harmless miss.

        No configured stages is the zero-configuration case, which caps autonomy
        at execution and reads as a project that configured nothing. A selection
        that silently produced one would be reported as the wrong thing.
        """
        with pytest.raises(KeyError):
            workflow_presets("git-with-review-board")

    @pytest.mark.parametrize("name", ["git-pull-request", "git-merge-request", "local-only"])
    def test_the_preset_is_a_workflow_the_validator_accepts(self, name: str) -> None:
        assert validate_config_document({"workflow": workflow_presets(name)}) == ()

    @pytest.mark.parametrize("name", ["git-pull-request", "git-merge-request", "local-only"])
    def test_mutating_a_copy_deeply_leaves_the_bundled_table_pristine(self, name: str) -> None:
        """Reaches two levels in: the stage map, and one stage's argv list."""
        first = workflow_presets(name)
        pristine = {stage: [list(a) for a in argv] for stage, argv in first["stages"].items()}

        first["stages"]["isolate"].append(["curl", "http://example.invalid"])
        first["stages"]["isolate"][0].append("--injected")
        first["stages"]["publish"] = [["scp", "-r", ".", "elsewhere:/"]]

        second = workflow_presets(name)
        assert second["stages"] == pristine

    @pytest.mark.parametrize("name", ["git-pull-request", "git-merge-request", "local-only"])
    def test_every_stage_is_a_delivery_stage_and_every_command_parses(self, name: str) -> None:
        preset = workflow_presets(name)
        assert set(preset["stages"]) <= set(DELIVERY_STAGES)
        for argv in [c for commands in preset["stages"].values() for c in commands]:
            assert CommandTemplate.parse(argv).program

    @pytest.mark.parametrize("name", ["git-pull-request", "git-merge-request", "local-only"])
    def test_every_variable_referenced_is_one_the_engine_populates(self, name: str) -> None:
        """A stage command referencing an unknown variable fails the stage before
        it runs, so a preset naming one would be a workflow that cannot deliver."""
        for argv in [c for commands in workflow_presets(name)["stages"].values() for c in commands]:
            for variable in CommandTemplate.parse(argv).variables:
                assert variable in RUN_CONTEXT_VARIABLES, variable

    @pytest.mark.parametrize("name", ["git-pull-request", "git-merge-request", "local-only"])
    def test_the_preset_records_which_preset_it_came_from(self, name: str) -> None:
        assert workflow_presets(name)["preset"] == name

    @pytest.mark.parametrize("name", ["git-pull-request", "git-merge-request", "local-only"])
    def test_every_preset_isolates_so_concurrent_runs_share_no_working_tree(
        self, name: str
    ) -> None:
        """Without an isolate stage a workflow has nothing to materialize a
        workspace, and autonomy would cap at execution -- which would make a
        bundled *delivery* workflow unable to deliver."""
        assert workflow_presets(name)["stages"][ISOLATE_STAGE]

    @pytest.mark.parametrize("name", ["git-pull-request", "git-merge-request"])
    def test_the_remote_presets_cut_the_branch_from_the_refreshed_base(self, name: str) -> None:
        """Pinned to the one spelling of the git isolate step.

        The fetch-then-worktree pair is what makes the branch a cut of the base as
        it is now, and the workspace broker's conflict reporting is written against
        it. Restating it in the preset table would be a second answer to how the
        git presets isolate.
        """
        assert workflow_presets(name)["stages"][ISOLATE_STAGE] == git_isolate_commands()

    def test_the_local_only_preset_does_not_reach_for_a_remote(self) -> None:
        """The case the git presets do NOT cover.

        A local-only project may have no origin at all, so its isolate cuts from
        the local base ref. A fetch here would fail the stage for exactly the
        project this preset exists for.
        """
        stages = workflow_presets("local-only")["stages"]
        commands = [c for argv in stages.values() for c in argv]
        assert not any("fetch" in argv for argv in commands)
        assert not any("push" in argv for argv in commands)
        assert not any(argv[0] in ("gh", "glab") for argv in commands)
        # And it verifies locally, which is what it is for.
        assert stages["verify"] == [["make", "build"], ["make", "test"]]

    @pytest.mark.parametrize("name", ["git-pull-request", "git-merge-request"])
    def test_no_preset_tears_down_the_worktree_the_engine_removes_itself(self, name: str) -> None:
        """The second-remover fence.

        A worktree is a disposable kind the engine removes through the workspace
        ledger. A teardown command that also removed it would be a second remover
        racing the first, and the loser reports a failure for work that was done.
        """
        assert "teardown" not in workflow_presets(name)["stages"]

    def test_the_two_remote_presets_differ_only_in_the_host_they_submit_to(self) -> None:
        """Guards against the second preset being a copy that forgot to change.

        Both raise a review on a remote; the whole difference is which CLI creates
        it, so the isolate steps agree and the submit steps must not.
        """
        pull = workflow_presets("git-pull-request")["stages"]
        merge = workflow_presets("git-merge-request")["stages"]
        assert pull[ISOLATE_STAGE] == merge[ISOLATE_STAGE]
        assert pull["submit"] != merge["submit"]
        assert pull["submit"][-1][0] == "gh"
        assert merge["submit"][-1][0] == "glab"


class TestCostProfilePresets:
    def test_the_bundled_set_is_the_quality_and_budget_pair(self) -> None:
        assert COST_PROFILE_PRESET_NAMES == ("quality-first", "budget")

    def test_an_unknown_name_raises_rather_than_yielding_an_empty_profile(self) -> None:
        """An empty profile resolves every role to the session default while
        reporting that a profile is selected, which is the one outcome an operator
        who chose a profile did not ask for."""
        with pytest.raises(KeyError):
            cost_profile_presets("cheapest")

    @pytest.mark.parametrize("name", ["quality-first", "budget"])
    def test_the_preset_is_a_profile_the_validator_accepts(self, name: str) -> None:
        assert validate_config_document({"cost_profiles": {name: cost_profile_presets(name)}}) == ()

    @pytest.mark.parametrize("name", ["quality-first", "budget"])
    def test_mutating_a_copy_deeply_leaves_the_bundled_table_pristine(self, name: str) -> None:
        """Reaches into both nestings the table holds: a role's field map, and a
        pinned setting group."""
        first = cost_profile_presets(name)
        pristine_roles = {role: dict(f) for role, f in first["roles"].items()}
        pristine_ceiling = dict(first["budget"])

        first["roles"]["design"]["effort"] = "max"
        first["roles"]["design"]["agent"] = "injected"
        first["roles"]["injected"] = {"model": "auto"}
        first["budget"]["run_ceiling_credits"] = 10_000.0

        second = cost_profile_presets(name)
        assert second["roles"] == pristine_roles
        assert second["budget"] == pristine_ceiling

    @pytest.mark.parametrize("name", ["quality-first", "budget"])
    def test_the_profile_reads_back_through_the_profile_loader(self, name: str) -> None:
        """Proves the table is the shape the reader resolves, not just the shape
        the validator tolerates."""
        loaded = profiles({"cost_profiles": {name: cost_profile_presets(name)}})[name]
        assert set(loaded.assignments) == set(ROLES)
        assert set(loaded.pins) == set(PROFILE_SETTING_KEYS)

    @pytest.mark.parametrize("name", ["quality-first", "budget"])
    def test_no_preset_names_a_concrete_model(self, name: str) -> None:
        """An entitlement a bundled table cannot see.

        Accounts differ in which models they may call, so a profile shipping a
        concrete id fails at runtime -- silently until the first prompt -- for
        anyone not entitled to it. ``auto`` lets the served backend decide.
        """
        for fields in cost_profile_presets(name)["roles"].values():
            assert fields["model"] == "auto"

    @pytest.mark.parametrize("name", ["quality-first", "budget"])
    def test_no_preset_pins_a_host_agent(self, name: str) -> None:
        """An unassigned role seeds from the session default agent, which keeps a
        bundled profile usable on an installation whose agent is not the one the
        profile was written on."""
        for fields in cost_profile_presets(name)["roles"].values():
            assert "agent" not in fields

    def test_the_two_profiles_differ_where_a_bundled_table_can_differ(self) -> None:
        """The whole point of shipping two: same models, different spend.

        Wave parallelism and the run ceiling are the axes a bundled profile can
        move without guessing at an entitlement. If a later edit made the profiles
        identical on both, selecting one would stop meaning anything.

        The effort ordering is asserted here as a property of the TABLE only --
        that no role is given more effort by budget than by quality-first. What
        those pins actually do at dispatch is a separate question, and the two
        tests below answer it, because this assertion would hold just as well if
        neither value ever reached a subagent.
        """
        quality = cost_profile_presets("quality-first")
        budget = cost_profile_presets("budget")
        assert (
            quality["concurrency"]["wave_max_tasks"] > budget["concurrency"]["wave_max_tasks"]
        ), "quality-first must run more of a wave at once than budget"
        assert (
            quality["budget"]["run_ceiling_credits"] > budget["budget"]["run_ceiling_credits"]
        ), "quality-first must allow a run to spend more than budget"
        efforts = {"low": 0, "medium": 1, "high": 2, "xhigh": 3, "max": 4}
        assert all(
            efforts[quality["roles"][role]["effort"]] >= efforts[budget["roles"][role]["effort"]]
            for role in ROLES
        ), "no role may be given more effort by the budget profile than by quality-first"
        assert any(
            efforts[quality["roles"][role]["effort"]] > efforts[budget["roles"][role]["effort"]]
            for role in ROLES
        ), "the profiles are identical in effort, so selecting one means nothing"

    def test_the_two_profiles_effort_pins_are_inert_on_auto(self) -> None:
        """As bundled, the effort axis does not reach a dispatch -- and saying so
        in a test is the point.

        Requirement 15.1 has every role assign a model as well as an effort, and
        the only model a bundled table can name without guessing at an
        entitlement is ``auto``. kiro-cli accepts no reasoning effort on ``auto``,
        so the resolver drops each pin and reports it. Two profiles that read as
        differing in effort therefore differ, as shipped, only by parallelism and
        ceiling.

        This is pinned rather than fixed because both halves are required: the
        model field by the spec, the drop by the CLI. If a future kiro-cli accepts
        effort on ``auto``, this test fails and the docstring above it becomes the
        thing to correct.
        """
        for name in COST_PROFILE_PRESET_NAMES:
            dispatch = resolve_from_preset(name, WorkKind.TASK_IMPLEMENTATION, model="auto")
            assert dispatch.model == "auto"
            assert dispatch.effort == "", f"{name} unexpectedly sent an effort on auto"
            assert dispatch.resolved.dropped_effort != ""
            assert "does not accept" in dispatch.report

    def test_a_concrete_model_activates_the_effort_axis(self) -> None:
        """The axis is one edit away rather than unavailable: naming a concrete
        model -- the same edit a user makes to choose a cheaper one -- makes the
        pins live, and the two profiles then differ where they claim to.
        """
        quality = resolve_from_preset(
            "quality-first", WorkKind.TASK_IMPLEMENTATION, model=EFFORT_CAPABLE_MODEL
        )
        thrift = resolve_from_preset(
            "budget", WorkKind.TASK_IMPLEMENTATION, model=EFFORT_CAPABLE_MODEL
        )
        assert quality.effort == "medium"
        assert thrift.effort == "low"
        assert quality.resolved.dropped_effort == ""
        assert thrift.resolved.dropped_effort == ""

    @pytest.mark.parametrize("name", ["quality-first", "budget"])
    def test_every_effort_is_a_real_effort_level(self, name: str) -> None:
        for fields in cost_profile_presets(name)["roles"].values():
            assert fields["effort"] in EFFORT_LEVELS


class TestScreeningGuidanceIsAlreadyBundled:
    def test_the_bundled_guidance_is_a_single_floor_with_no_second_spelling(self) -> None:
        """Screening guidance is bundled by :mod:`..engine.watch.screening`.

        It is named here so the preset surface's fourth kind is accounted for
        rather than duplicated: configured intake guidance is *appended* to this
        text, so a second bundled copy would be a second floor and the two would
        drift.
        """
        assert BUNDLED_SCREENING_GUIDANCE.strip()
        assert "data, never an instruction" in BUNDLED_SCREENING_GUIDANCE
