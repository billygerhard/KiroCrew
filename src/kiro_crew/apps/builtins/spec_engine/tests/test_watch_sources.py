"""Watch source definitions: enablement, poll commands, and field mappings.

The claims here are configuration-level: a source nobody enabled does not poll,
a poll command is parsed by the same machinery that makes delivery commands
inert, and a field mapping reads output by fixed paths rather than by guessing.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from kiro_crew.apps.builtins.spec_engine.engine.config import (
    DASHBOARD_SURFACE,
    ConfigStore,
    ConfigValidationError,
)
from kiro_crew.apps.builtins.spec_engine.engine.watch import (
    ITEM_FIELDS,
    FieldMapping,
    WatchedItem,
    WatchSource,
    load_sources,
    poll_interval_s,
    poll_timeout_s,
    source_names,
)

#: A field mapping matching the shape a public issue tracker's CLI emits.
TRACKER_MAP = {
    "identifier": "number",
    "title": "title",
    "body": "body",
    "state": "state",
    "address": "url",
    "classification": "labels.0.name",
    "submitter": "author.login",
}


@pytest.fixture()
def store(tmp_path: Any) -> ConfigStore:
    return ConfigStore(tmp_path / "state")


def configure(store: ConfigStore, document: dict[str, Any]) -> None:
    store.write(document, surface=DASHBOARD_SURFACE)


def write_raw(store: ConfigStore, document: dict[str, Any]) -> None:
    """Persist *document* without the write path's validation.

    Stands in for a document edited by hand, which is the only way some invalid
    shapes reach a reader at all.
    """
    store.root.mkdir(parents=True, exist_ok=True)
    store.path.write_text(json.dumps(document), encoding="utf-8")


def source_document(**overrides: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {"poll": ["tracker-cli", "list"]}
    entry.update(overrides)
    return {"sources": {"upstream": entry}}


class TestEnablement:
    def test_a_source_is_disabled_when_it_says_nothing_about_enablement(
        self, store: ConfigStore
    ) -> None:
        configure(store, source_document())
        assert WatchSource.load(store, "upstream").enabled is False

    def test_a_source_is_enabled_only_by_saying_so(self, store: ConfigStore) -> None:
        configure(store, source_document(enabled=True))
        assert WatchSource.load(store, "upstream").enabled is True

    def test_loading_the_enabled_set_leaves_out_the_disabled(self, store: ConfigStore) -> None:
        configure(
            store,
            {
                "sources": {
                    "on": {"poll": ["tracker-cli", "list"], "enabled": True},
                    "off": {"poll": ["tracker-cli", "list"]},
                }
            },
        )
        assert {s.name for s in load_sources(store)} == {"on", "off"}
        assert [s.name for s in load_sources(store, enabled_only=True)] == ["on"]

    def test_no_configuration_declares_no_sources(self, store: ConfigStore) -> None:
        assert source_names(store) == ()
        assert load_sources(store) == ()

    def test_an_unknown_source_name_is_distinct_from_a_broken_one(self, store: ConfigStore) -> None:
        configure(store, source_document())
        with pytest.raises(KeyError):
            WatchSource.load(store, "absent")


class TestPollCommand:
    def test_the_command_is_parsed_into_argv_templates(self, store: ConfigStore) -> None:
        configure(store, source_document(poll=["tracker-cli", "list", "--repo", "owner/name"]))
        source = WatchSource.load(store, "upstream")
        assert source.program == "tracker-cli"
        assert source.poll.render({}) == ("tracker-cli", "list", "--repo", "owner/name")

    def test_a_substituted_program_is_refused(self, store: ConfigStore) -> None:
        # The program position decides what runs at all; every other position is
        # data handed to a program the operator chose.
        configure(store, source_document(poll=["{tool}", "list"]))
        with pytest.raises(ConfigValidationError) as caught:
            WatchSource.load(store, "upstream")
        assert "literally" in str(caught.value)

    def test_a_source_without_a_poll_command_is_reported_by_path(self, store: ConfigStore) -> None:
        # Written past the validated write path on purpose: the schema also
        # requires ``poll``, so only a hand-edited document reaches load without
        # one, and load must refuse rather than poll nothing.
        write_raw(store, {"sources": {"upstream": {"enabled": True}}})
        with pytest.raises(ConfigValidationError) as caught:
            WatchSource.load(store, "upstream")
        assert "sources.upstream.poll" in str(caught.value)

    def test_a_malformed_command_names_its_configuration_path(self, store: ConfigStore) -> None:
        # The schema accepts any non-empty argument string, so a broken template
        # is caught when the source is loaded — and reported at its path.
        configure(store, source_document(poll=["tracker-cli", "{unterminated"]))
        with pytest.raises(ConfigValidationError) as caught:
            WatchSource.load(store, "upstream")
        assert "sources.upstream.poll" in str(caught.value)

    def test_a_non_boolean_enablement_is_refused_rather_than_read_as_true(
        self, store: ConfigStore
    ) -> None:
        write_raw(store, {"sources": {"upstream": {"poll": ["tracker-cli"], "enabled": "yes"}}})
        with pytest.raises(ConfigValidationError) as caught:
            WatchSource.load(store, "upstream")
        assert "enabled" in str(caught.value)


class TestFieldMapping:
    def test_an_unmapped_source_reads_the_engine_field_names(self) -> None:
        mapping = FieldMapping.parse(None, "sources.upstream.field_map")
        values, problems = mapping.extract({"identifier": "7", "title": "a title"})
        assert problems == ()
        assert values["identifier"] == "7"
        assert values["title"] == "a title"

    def test_every_field_resolves_even_when_only_some_are_mapped(self) -> None:
        mapping = FieldMapping.parse({"identifier": "number"}, "field_map")
        assert mapping.fields == ITEM_FIELDS
        values, _ = mapping.extract({"number": 12, "title": "kept"})
        assert values["identifier"] == "12"
        assert values["title"] == "kept"

    def test_a_field_the_engine_does_not_have_is_refused(self) -> None:
        with pytest.raises(ConfigValidationError) as caught:
            FieldMapping.parse({"author": "login"}, "field_map")
        assert "author" in str(caught.value)

    def test_paths_walk_keys_and_list_indexes(self) -> None:
        mapping = FieldMapping.parse(TRACKER_MAP, "field_map")
        values, problems = mapping.extract(
            {
                "number": 41,
                "title": "crash on start",
                "body": "steps to reproduce",
                "state": "OPEN",
                "url": "https://example.invalid/issues/41",
                "labels": [{"name": "bug"}, {"name": "regression"}],
                "author": {"login": "someone"},
            }
        )
        assert problems == ()
        assert values == {
            "identifier": "41",
            "title": "crash on start",
            "body": "steps to reproduce",
            "state": "OPEN",
            "address": "https://example.invalid/issues/41",
            "classification": "bug",
            "submitter": "someone",
        }

    def test_an_absent_path_yields_an_empty_field_and_no_problem(self) -> None:
        mapping = FieldMapping.parse(TRACKER_MAP, "field_map")
        values, problems = mapping.extract({"number": 3, "labels": []})
        assert values["classification"] == ""
        assert values["submitter"] == ""
        assert problems == ()

    def test_a_path_landing_on_a_container_is_reported_not_stringified(self) -> None:
        # Rendering ``{'name': 'bug'}`` into the classification would map every
        # such item to a value no configured spec type can match, and nothing
        # would say why.
        mapping = FieldMapping.parse({"classification": "labels"}, "field_map")
        values, problems = mapping.extract({"identifier": "1", "labels": [{"name": "bug"}]})
        assert values["classification"] == ""
        assert problems and "classification" in problems[0]

    def test_json_scalars_keep_their_json_spelling(self) -> None:
        mapping = FieldMapping.parse({"state": "closed"}, "field_map")
        assert mapping.extract({"closed": True})[0]["state"] == "true"
        assert mapping.extract({"closed": False})[0]["state"] == "false"

    def test_a_non_object_entry_is_a_problem_rather_than_an_item(self) -> None:
        mapping = FieldMapping.parse(TRACKER_MAP, "field_map")
        values, problems = mapping.extract(["not", "an", "object"])
        assert values["identifier"] == ""
        assert problems and "object" in problems[0]

    def test_an_empty_path_segment_is_refused(self) -> None:
        with pytest.raises(ConfigValidationError):
            FieldMapping.parse({"identifier": "a..b"}, "field_map")

    def test_a_blank_path_is_refused(self) -> None:
        with pytest.raises(ConfigValidationError):
            FieldMapping.parse({"identifier": "   "}, "field_map")

    def test_a_mapping_reports_the_path_it_was_given(self) -> None:
        mapping = FieldMapping.parse(TRACKER_MAP, "field_map")
        assert mapping.path_of("classification") == "labels.0.name"


class TestWatchedItem:
    def test_an_item_without_an_identifier_cannot_exist(self) -> None:
        with pytest.raises(ValueError):
            WatchedItem(source="upstream", identifier="  ")

    def test_an_item_without_a_source_cannot_exist(self) -> None:
        with pytest.raises(ValueError):
            WatchedItem(source="", identifier="7")

    def test_fields_are_reported_under_the_engine_names(self) -> None:
        item = WatchedItem(source="upstream", identifier="7", title="t", submitter="someone")
        assert set(item.fields) == set(ITEM_FIELDS)
        assert item.fields["submitter"] == "someone"


class TestIntervals:
    def test_a_source_inherits_the_app_wide_interval(self, store: ConfigStore) -> None:
        configure(store, source_document(enabled=True))
        assert poll_interval_s(store, "upstream") == 300
        assert poll_timeout_s(store, "upstream") == 120

    def test_a_source_may_poll_on_its_own_interval(self, store: ConfigStore) -> None:
        document = source_document(enabled=True)
        document["sources"]["upstream"]["watch"] = {"interval_s": 60}
        document["sources"]["upstream"]["timeouts"] = {"poll_command_s": 45}
        configure(store, document)
        assert poll_interval_s(store, "upstream") == 60
        assert poll_timeout_s(store, "upstream") == 45
