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
from kiro_crew.apps.builtins.spec_engine.engine.config.schema import (
    SOURCE_FIELDS,
    validate_config_document,
)
from kiro_crew.apps.builtins.spec_engine.engine.delivery.templates import CommandTemplate
from kiro_crew.apps.builtins.spec_engine.engine.watch import (
    ITEM_FIELDS,
    WATCH_SOURCE_PRESET_HOSTS,
    WATCH_SOURCE_PRESET_PROGRAMS,
    WATCH_SOURCE_PRESETS,
    WatchSource,
    watch_source_presets,
)

#: One item as each host's CLI actually emits it, so the field map is exercised
#: against the shape it was written for rather than against engine field names.
HOST_PAYLOADS: dict[str, dict[str, Any]] = {
    "github": {
        "number": 412,
        "title": "Crash on empty input",
        "body": "Steps to reproduce...",
        "state": "OPEN",
        "url": "https://github.com/owner/repo/issues/412",
        "labels": [{"name": "bug"}, {"name": "triage"}],
        "author": {"login": "octocat"},
        "authorAssociation": "CONTRIBUTOR",
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
