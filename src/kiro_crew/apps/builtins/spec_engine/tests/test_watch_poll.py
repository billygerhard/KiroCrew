"""Polling a watch source: what it reports, and what it refuses to imply.

The load-bearing test in this module is the one about a poll program that is not
installed. A watcher whose program is absent finds nothing, and so does a
tracker with an empty backlog; if both produced the same report, a source could
be silently unwatched for as long as nobody thought to check. So the tests here
assert the shape of the answer, not just its contents: an unhealthy source names
the program it could not run and does not answer the "is there nothing to do"
question at all.

Real programs are spawned rather than mocked wherever what runs is the point,
for the same reason the delivery stage tests do it: item text is authored on a
public tracker, and only the process boundary can show that it arrives as inert
data.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Sequence

import pytest

from kiro_crew.apps.builtins.spec_engine.engine.config import (
    DASHBOARD_SURFACE,
    ConfigStore,
)
from kiro_crew.apps.builtins.spec_engine.engine.delivery import (
    MAX_CAPTURED_CHARS,
    TRUNCATION_NOTICE,
    CommandOutcome,
)
from kiro_crew.apps.builtins.spec_engine.engine.watch import (
    HealthReason,
    PollStatus,
    WatchSource,
    poll_source,
    poll_sources,
)

#: A program name no host has installed. Deliberately not "gh" or "glab": the
#: test must fail for the absence, never for a host that happens to have one.
ABSENT_PROGRAM = "kirocrew-nonexistent-tracker-cli"

#: Every shell construct that would matter if a shell were involved, in one
#: item title. Item text arrives from a tracker anyone can write to.
HOSTILE_TITLE = "boom; touch pwned && touch pwned2 | tee pwned3 `touch pwned4` $(touch pwned5)"

#: Marker files the payload above would create if anything interpreted it.
PAYLOAD_ARTEFACTS = ("pwned", "pwned2", "pwned3", "pwned4", "pwned5")

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
def store(tmp_path: Path) -> ConfigStore:
    return ConfigStore(tmp_path / "state")


def configure(store: ConfigStore, document: dict[str, Any]) -> None:
    store.write(document, surface=DASHBOARD_SURFACE)


def emitter(tmp_path: Path, payload: str, *, exit_code: int = 0, stderr: str = "") -> Path:
    """A program that prints *payload* and exits with *exit_code*."""
    script = tmp_path / f"emitter_{abs(hash((payload, exit_code, stderr))) % 10**8}.py"
    script.write_text(
        "import sys\n"
        f"sys.stdout.write({payload!r})\n"
        f"sys.stderr.write({stderr!r})\n"
        f"sys.exit({exit_code})\n",
        encoding="utf-8",
    )
    return script


def define(
    store: ConfigStore,
    *,
    poll: list[str],
    enabled: bool = True,
    field_map: dict[str, str] | None = None,
    name: str = "upstream",
) -> None:
    entry: dict[str, Any] = {"poll": poll, "enabled": enabled}
    if field_map is not None:
        entry["field_map"] = field_map
    configure(store, {"sources": {name: entry}})


def python_poll(script: Path) -> list[str]:
    return [sys.executable, str(script)]


class Recorder:
    """A command runner that records argv and returns a canned outcome."""

    def __init__(self, outcome: CommandOutcome) -> None:
        self.outcome = outcome
        self.calls: list[tuple[tuple[str, ...], Path, int]] = []

    def __call__(self, argv: Sequence[str], *, cwd: Path, timeout_s: int) -> CommandOutcome:
        self.calls.append((tuple(argv), cwd, timeout_s))
        return self.outcome


class TestAnUnavailableProgramIsNeverAnEmptyBacklog:
    def test_a_missing_poll_program_reports_unhealthy_and_names_it(
        self, store: ConfigStore
    ) -> None:
        define(store, poll=[ABSENT_PROGRAM, "list"])

        outcome = poll_source(store, "upstream")

        assert outcome.status is PollStatus.UNHEALTHY
        assert outcome.reason is HealthReason.PROGRAM_UNAVAILABLE
        assert outcome.missing_program == ABSENT_PROGRAM
        assert ABSENT_PROGRAM in outcome.detail
        assert ABSENT_PROGRAM in outcome.describe()

    def test_a_missing_poll_program_does_not_report_zero_items(self, store: ConfigStore) -> None:
        define(store, poll=[ABSENT_PROGRAM, "list"])

        outcome = poll_source(store, "upstream")

        # The distinction the requirement exists for: "nothing waiting" is a
        # claim only a poll that ran may make.
        assert outcome.found_no_items is False
        assert outcome.healthy is False
        assert outcome.items == ()

    def test_an_empty_backlog_does_report_zero_items(
        self, store: ConfigStore, tmp_path: Path
    ) -> None:
        define(store, poll=python_poll(emitter(tmp_path, "[]\n")))

        outcome = poll_source(store, "upstream")

        assert outcome.status is PollStatus.OK
        assert outcome.items == ()
        assert outcome.found_no_items is True

    def test_an_absolute_program_that_is_not_there_is_reported_the_same_way(
        self, store: ConfigStore, tmp_path: Path
    ) -> None:
        absent = tmp_path / "bin" / ABSENT_PROGRAM
        define(store, poll=[str(absent), "list"])

        outcome = poll_source(store, "upstream")

        assert outcome.reason is HealthReason.PROGRAM_UNAVAILABLE
        assert outcome.missing_program == str(absent)
        assert outcome.found_no_items is False

    def test_a_missing_program_spawns_nothing(self, store: ConfigStore) -> None:
        define(store, poll=[ABSENT_PROGRAM])
        recorder = Recorder(CommandOutcome(exit_code=0, stdout="[]"))

        poll_source(store, "upstream", runner=recorder)

        assert recorder.calls == []

    def test_an_outcome_cannot_be_built_that_hides_a_missing_program(self) -> None:
        from kiro_crew.apps.builtins.spec_engine.engine.watch import PollOutcome

        with pytest.raises(ValueError):
            PollOutcome(
                source="upstream",
                status=PollStatus.UNHEALTHY,
                reason=HealthReason.PROGRAM_UNAVAILABLE,
                detail="something went wrong",
            )

    def test_an_unhealthy_outcome_cannot_carry_items(self) -> None:
        from kiro_crew.apps.builtins.spec_engine.engine.watch import PollOutcome, WatchedItem

        with pytest.raises(ValueError):
            PollOutcome(
                source="upstream",
                status=PollStatus.UNHEALTHY,
                reason=HealthReason.COMMAND_FAILED,
                detail="exited 1",
                items=(WatchedItem(source="upstream", identifier="1"),),
            )

    def test_an_unhealthy_outcome_must_explain_itself(self) -> None:
        from kiro_crew.apps.builtins.spec_engine.engine.watch import PollOutcome

        with pytest.raises(ValueError):
            PollOutcome(
                source="upstream",
                status=PollStatus.UNHEALTHY,
                reason=HealthReason.COMMAND_FAILED,
            )


class TestDisabledSources:
    def test_a_disabled_source_is_not_polled(self, store: ConfigStore) -> None:
        define(store, poll=[ABSENT_PROGRAM], enabled=False)
        recorder = Recorder(CommandOutcome(exit_code=0, stdout="[]"))

        outcome = poll_source(store, "upstream", runner=recorder)

        assert outcome.status is PollStatus.DISABLED
        assert recorder.calls == []

    def test_a_disabled_source_does_not_claim_an_empty_backlog(self, store: ConfigStore) -> None:
        define(store, poll=[ABSENT_PROGRAM], enabled=False)
        assert poll_source(store, "upstream").found_no_items is False


class TestCommandFailures:
    def test_a_non_zero_exit_is_unhealthy_with_its_status_and_message(
        self, store: ConfigStore, tmp_path: Path
    ) -> None:
        script = emitter(tmp_path, "", exit_code=3, stderr="not authenticated\n")
        define(store, poll=python_poll(script))

        outcome = poll_source(store, "upstream")

        assert outcome.reason is HealthReason.COMMAND_FAILED
        assert outcome.exit_code == 3
        assert "not authenticated" in outcome.detail
        assert outcome.found_no_items is False

    def test_a_timeout_is_reported_as_a_timeout(self, store: ConfigStore) -> None:
        define(store, poll=[sys.executable, "-c", "pass"])
        recorder = Recorder(CommandOutcome(exit_code=None, timed_out=True))

        outcome = poll_source(store, "upstream", runner=recorder)

        assert outcome.reason is HealthReason.TIMED_OUT
        assert outcome.found_no_items is False

    def test_a_start_failure_blames_the_program(self, store: ConfigStore) -> None:
        define(store, poll=[sys.executable, "-c", "pass"])
        recorder = Recorder(CommandOutcome(exit_code=None, start_error="permission denied"))

        outcome = poll_source(store, "upstream", runner=recorder)

        assert outcome.reason is HealthReason.PROGRAM_UNAVAILABLE
        assert outcome.missing_program == sys.executable
        assert "permission denied" in outcome.detail


class TestReadingOutput:
    def test_output_that_hit_the_capture_ceiling_is_not_parsed_as_fewer_items(
        self, store: ConfigStore
    ) -> None:
        define(store, poll=[sys.executable, "-c", "pass"])
        truncated = ("[" + '{"identifier": "1"},' * 10) + TRUNCATION_NOTICE
        recorder = Recorder(CommandOutcome(exit_code=0, stdout=truncated))

        outcome = poll_source(store, "upstream", runner=recorder)

        assert outcome.reason is HealthReason.OUTPUT_TRUNCATED
        assert outcome.found_no_items is False

    def test_a_capture_ceiling_is_low_enough_to_be_reachable(self) -> None:
        # The notice is what this detection depends on, so the constants have to
        # stay the ones the executor actually applies.
        assert MAX_CAPTURED_CHARS > 0
        assert TRUNCATION_NOTICE

    def test_printing_nothing_is_unhealthy_rather_than_empty(
        self, store: ConfigStore, tmp_path: Path
    ) -> None:
        # A program that printed nothing did not report an empty backlog. This is
        # the same failure as a missing program wearing a zero exit status.
        define(store, poll=python_poll(emitter(tmp_path, "")))

        outcome = poll_source(store, "upstream")

        assert outcome.reason is HealthReason.UNREADABLE_OUTPUT
        assert outcome.found_no_items is False

    def test_output_that_is_not_json_is_unhealthy(self, store: ConfigStore, tmp_path: Path) -> None:
        define(store, poll=python_poll(emitter(tmp_path, "an error page, not JSON\n")))

        outcome = poll_source(store, "upstream")

        assert outcome.reason is HealthReason.UNREADABLE_OUTPUT

    def test_a_single_json_object_is_refused_rather_than_read_as_one_item(
        self, store: ConfigStore, tmp_path: Path
    ) -> None:
        payload = json.dumps({"items": [{"number": 1}]})
        define(store, poll=python_poll(emitter(tmp_path, payload)))

        outcome = poll_source(store, "upstream")

        assert outcome.reason is HealthReason.UNREADABLE_OUTPUT
        assert "array" in outcome.detail

    def test_one_object_per_line_is_accepted(self, store: ConfigStore, tmp_path: Path) -> None:
        payload = '{"number": 1, "title": "first"}\n{"number": 2, "title": "second"}\n'
        define(store, poll=python_poll(emitter(tmp_path, payload)), field_map=TRACKER_MAP)

        outcome = poll_source(store, "upstream")

        assert outcome.status is PollStatus.OK
        assert [item.identifier for item in outcome.items] == ["1", "2"]

    def test_a_json_array_is_mapped_field_by_field(
        self, store: ConfigStore, tmp_path: Path
    ) -> None:
        payload = json.dumps(
            [
                {
                    "number": 41,
                    "title": "crash on start",
                    "body": "steps",
                    "state": "OPEN",
                    "url": "https://example.invalid/issues/41",
                    "labels": [{"name": "bug"}],
                    "author": {"login": "someone"},
                }
            ]
        )
        define(store, poll=python_poll(emitter(tmp_path, payload)), field_map=TRACKER_MAP)

        outcome = poll_source(store, "upstream")

        assert outcome.status is PollStatus.OK
        item = outcome.items[0]
        assert item.source == "upstream"
        assert item.identifier == "41"
        assert item.classification == "bug"
        assert item.submitter == "someone"
        assert item.address == "https://example.invalid/issues/41"


class TestMappingMismatch:
    def test_a_mapping_that_reads_nothing_is_unhealthy_not_empty(
        self, store: ConfigStore, tmp_path: Path
    ) -> None:
        # The quiet version of this bug: the poll works, the mapping points at
        # keys the output does not have, and the source reports an empty backlog
        # forever.
        payload = json.dumps([{"id": 1}, {"id": 2}])
        define(
            store,
            poll=python_poll(emitter(tmp_path, payload)),
            field_map={"identifier": "number"},
        )

        outcome = poll_source(store, "upstream")

        assert outcome.reason is HealthReason.FIELD_MAP_MISMATCH
        assert "number" in outcome.detail
        assert outcome.found_no_items is False

    def test_one_unmappable_item_does_not_discard_the_others_silently(
        self, store: ConfigStore, tmp_path: Path
    ) -> None:
        payload = json.dumps([{"number": 1}, {"no_number": True}, {"number": 3}])
        define(
            store,
            poll=python_poll(emitter(tmp_path, payload)),
            field_map={"identifier": "number"},
        )

        outcome = poll_source(store, "upstream")

        assert outcome.status is PollStatus.OK
        assert [item.identifier for item in outcome.items] == ["1", "3"]
        assert [entry.index for entry in outcome.rejected] == [1]
        assert "identifier" in outcome.rejected[0].reason
        assert "unmappable" in outcome.describe()

    def test_a_field_pointing_at_a_container_is_surfaced_on_the_outcome(
        self, store: ConfigStore, tmp_path: Path
    ) -> None:
        payload = json.dumps([{"number": 1, "labels": [{"name": "bug"}]}])
        define(
            store,
            poll=python_poll(emitter(tmp_path, payload)),
            field_map={"identifier": "number", "classification": "labels"},
        )

        outcome = poll_source(store, "upstream")

        assert outcome.status is PollStatus.OK
        assert outcome.items[0].classification == ""
        assert outcome.field_problems and "classification" in outcome.field_problems[0]


class TestUntrustedItemText:
    def test_item_text_carrying_shell_syntax_executes_nothing(
        self, store: ConfigStore, tmp_path: Path
    ) -> None:
        payload = json.dumps([{"number": 1, "title": HOSTILE_TITLE}])
        define(store, poll=python_poll(emitter(tmp_path, payload)), field_map=TRACKER_MAP)

        outcome = poll_source(store, "upstream")

        assert outcome.items[0].title == HOSTILE_TITLE
        for artefact in PAYLOAD_ARTEFACTS:
            assert not (tmp_path / artefact).exists()
            assert not (store.root / artefact).exists()

    def test_a_poll_command_referencing_a_variable_is_refused_before_it_runs(
        self, store: ConfigStore
    ) -> None:
        # A poll has no run to substitute from, so an empty substitution would
        # run a different command than the one configured and say nothing.
        define(store, poll=[sys.executable, "--repo", "{item_id}"])
        recorder = Recorder(CommandOutcome(exit_code=0, stdout="[]"))

        outcome = poll_source(store, "upstream", runner=recorder)

        assert outcome.reason is HealthReason.CONFIG_INVALID
        assert "item_id" in outcome.detail
        assert recorder.calls == []


class TestReportingEverySource:
    def test_an_undeclared_source_is_reported_rather_than_raised(self, store: ConfigStore) -> None:
        outcome = poll_source(store, "never-configured")

        assert outcome.reason is HealthReason.CONFIG_INVALID
        assert outcome.found_no_items is False

    def test_every_declared_source_appears_in_the_report(
        self, store: ConfigStore, tmp_path: Path
    ) -> None:
        working = python_poll(emitter(tmp_path, "[]"))
        configure(
            store,
            {
                "sources": {
                    "healthy": {"poll": working, "enabled": True},
                    "broken": {"poll": [ABSENT_PROGRAM], "enabled": True},
                    "paused": {"poll": working},
                }
            },
        )

        outcomes = poll_sources(store)

        by_name = {outcome.source: outcome for outcome in outcomes}
        assert set(by_name) == {"healthy", "broken", "paused"}
        assert by_name["healthy"].status is PollStatus.OK
        assert by_name["broken"].reason is HealthReason.PROGRAM_UNAVAILABLE
        assert by_name["paused"].status is PollStatus.DISABLED

    def test_a_broken_source_does_not_stop_the_others_from_being_polled(
        self, store: ConfigStore, tmp_path: Path
    ) -> None:
        payload = json.dumps([{"number": 7}])
        configure(
            store,
            {
                "sources": {
                    "broken": {"poll": ["tracker-cli", "{unterminated"], "enabled": True},
                    "working": {
                        "poll": python_poll(emitter(tmp_path, payload)),
                        "enabled": True,
                        "field_map": {"identifier": "number"},
                    },
                }
            },
        )

        by_name = {outcome.source: outcome for outcome in poll_sources(store)}

        assert by_name["broken"].reason is HealthReason.CONFIG_INVALID
        assert [item.identifier for item in by_name["working"].items] == ["7"]


class TestWhereAPollRuns:
    def test_a_source_mapped_to_a_project_polls_in_that_tree(
        self, store: ConfigStore, tmp_path: Path
    ) -> None:
        project = tmp_path / "checkout"
        project.mkdir()
        configure(
            store,
            {
                "projects": {"acme": {"path": str(project)}},
                "sources": {
                    "upstream": {
                        "poll": [sys.executable, "-c", "print('[]')"],
                        "enabled": True,
                        "project": "acme",
                    }
                },
            },
        )
        recorder = Recorder(CommandOutcome(exit_code=0, stdout="[]"))

        poll_source(store, "upstream", runner=recorder)

        assert recorder.calls[0][1] == project

    def test_an_unmapped_source_polls_in_the_apps_own_directory(self, store: ConfigStore) -> None:
        define(store, poll=[sys.executable, "-c", "print('[]')"])
        recorder = Recorder(CommandOutcome(exit_code=0, stdout="[]"))

        poll_source(store, "upstream", runner=recorder)

        assert recorder.calls[0][1] == store.root

    def test_the_polls_own_timeout_is_the_one_handed_to_the_runner(
        self, store: ConfigStore
    ) -> None:
        configure(
            store,
            {
                "sources": {
                    "upstream": {
                        "poll": [sys.executable, "-c", "print('[]')"],
                        "enabled": True,
                        "timeouts": {"poll_command_s": 42},
                    }
                }
            },
        )
        recorder = Recorder(CommandOutcome(exit_code=0, stdout="[]"))

        poll_source(store, "upstream", runner=recorder)

        assert recorder.calls[0][2] == 42


class TestLoadedSourcePolling:
    def test_an_already_loaded_source_can_be_polled_directly(
        self, store: ConfigStore, tmp_path: Path
    ) -> None:
        from kiro_crew.apps.builtins.spec_engine.engine.watch import poll

        payload = json.dumps([{"number": 5}])
        define(
            store,
            poll=python_poll(emitter(tmp_path, payload)),
            field_map={"identifier": "number"},
        )
        source = WatchSource.load(store, "upstream")

        outcome = poll(store, source)

        assert [item.identifier for item in outcome.items] == ["5"]
