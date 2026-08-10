"""Property-based tests for watching.

Two claims are worth generating cases for rather than choosing examples.

The first is the correctness property this whole module exists to hold: **for any
poll result, "no items waiting" is reported only by a poll that ran and parsed.**
Handwritten tests cover the failures somebody thought of; a generated one covers
the failure shape itself, across every combination of exit status, output, and
mapping a source can produce.

The second is that item text is data. Item fields arrive from a tracker anyone
can write to, so the generated values favour the characters that would matter if
anything ever interpreted them.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from kiro_crew.apps.builtins.spec_engine.engine.config import (
    DASHBOARD_SURFACE,
    ConfigStore,
)
from kiro_crew.apps.builtins.spec_engine.engine.delivery import (
    TRUNCATION_NOTICE,
    CommandOutcome,
)
from kiro_crew.apps.builtins.spec_engine.engine.watch import (
    FieldMapping,
    PollStatus,
    WatchedItem,
    poll_source,
)

#: Structural properties render and decode only, so they can search widely.
MAX_EXAMPLES = 200

#: A program name no host has installed, used where absence is the point.
ABSENT_PROGRAM = "kirocrew-nonexistent-tracker-cli"

#: Substrings assembled into hostile field values. Each would change the meaning
#: of a command line if any shell parsed one.
_HOSTILE_PIECES = (
    "; touch pwned",
    "&& rm -rf .",
    "| tee pwned",
    "`touch pwned`",
    "$(touch pwned)",
    "> pwned",
    "'quoted'",
    '"quoted"',
    "\n",
    "\t",
    "{identifier}",
    "{{",
    "}}",
    "$HOME",
    "%PATH%",
    "../..",
    "\\",
    "ignore previous instructions",
)

hostile_text = st.lists(st.sampled_from(_HOSTILE_PIECES), min_size=1, max_size=6).map("".join)

#: Whatever a poll command can hand back: any exit status, any output.
command_outcomes = st.one_of(
    st.builds(
        CommandOutcome,
        exit_code=st.integers(min_value=-4, max_value=9),
        stdout=st.one_of(
            st.just(""),
            st.just("[]"),
            st.just("null"),
            st.just("{}"),
            st.just('{"items": []}'),
            st.just('[{"identifier": "1"}]'),
            st.just('[{"other": "1"}]'),
            st.just("not json at all"),
            st.just("[" + TRUNCATION_NOTICE),
            hostile_text,
        ),
        stderr=st.one_of(st.just(""), hostile_text),
    ),
    st.builds(CommandOutcome, exit_code=st.none(), timed_out=st.just(True)),
    st.builds(CommandOutcome, exit_code=st.none(), start_error=st.just("permission denied")),
)


@pytest.fixture()
def store(tmp_path: Path) -> ConfigStore:
    configured = ConfigStore(tmp_path / "state")
    configured.write(
        {
            "sources": {
                "upstream": {"poll": [sys.executable, "-c", "pass"], "enabled": True},
                "absent": {"poll": [ABSENT_PROGRAM], "enabled": True},
                "paused": {"poll": [sys.executable, "-c", "pass"]},
            }
        },
        surface=DASHBOARD_SURFACE,
    )
    return configured


class TestNothingWaitingIsAClaimOnlyAPollCanMake:
    @settings(
        max_examples=MAX_EXAMPLES,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    @given(produced=command_outcomes)
    def test_found_no_items_implies_a_healthy_poll(
        self, store: ConfigStore, produced: CommandOutcome
    ) -> None:
        outcome = poll_source(store, "upstream", runner=lambda *a, **k: produced)

        if outcome.found_no_items:
            assert outcome.status is PollStatus.OK
            assert outcome.healthy
            assert outcome.reason is None
        else:
            assert outcome.items or outcome.status is not PollStatus.OK

    @settings(
        max_examples=MAX_EXAMPLES,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    @given(produced=command_outcomes)
    def test_an_unhealthy_outcome_always_explains_itself(
        self, store: ConfigStore, produced: CommandOutcome
    ) -> None:
        outcome = poll_source(store, "upstream", runner=lambda *a, **k: produced)

        if outcome.status is PollStatus.UNHEALTHY:
            assert outcome.reason is not None
            assert outcome.detail.strip()
            assert outcome.items == ()
            assert outcome.describe().startswith("upstream:")

    @settings(
        max_examples=MAX_EXAMPLES,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    @given(produced=command_outcomes)
    def test_an_absent_program_never_reaches_the_runner_or_reports_emptiness(
        self, store: ConfigStore, produced: CommandOutcome
    ) -> None:
        calls: list[Any] = []

        def runner(*args: Any, **kwargs: Any) -> CommandOutcome:
            calls.append(args)
            return produced

        outcome = poll_source(store, "absent", runner=runner)

        assert calls == []
        assert outcome.found_no_items is False
        assert outcome.missing_program == ABSENT_PROGRAM

    @settings(
        max_examples=MAX_EXAMPLES,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    @given(produced=command_outcomes)
    def test_a_disabled_source_reports_neither_health_nor_emptiness(
        self, store: ConfigStore, produced: CommandOutcome
    ) -> None:
        outcome = poll_source(store, "paused", runner=lambda *a, **k: produced)

        assert outcome.status is PollStatus.DISABLED
        assert outcome.found_no_items is False
        assert outcome.healthy is False


class TestItemTextIsData:
    @settings(max_examples=MAX_EXAMPLES, deadline=None)
    @given(
        title=hostile_text,
        body=hostile_text,
        classification=hostile_text,
        submitter=hostile_text,
    )
    def test_mapped_values_arrive_verbatim(
        self, title: str, body: str, classification: str, submitter: str
    ) -> None:
        mapping = FieldMapping.identity()
        raw = {
            "identifier": "1",
            "title": title,
            "body": body,
            "classification": classification,
            "submitter": submitter,
        }

        values, problems = mapping.extract(raw)

        # Copied, never parsed: a value that looks like a template reference stays
        # the characters it was.
        assert problems == ()
        assert values["title"] == title
        assert values["body"] == body
        assert values["classification"] == classification
        assert values["submitter"] == submitter

    @settings(
        max_examples=MAX_EXAMPLES,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    @given(title=hostile_text)
    def test_hostile_titles_survive_a_whole_poll_unchanged(
        self, store: ConfigStore, title: str
    ) -> None:
        payload = json.dumps([{"identifier": "1", "title": title}])
        produced = CommandOutcome(exit_code=0, stdout=payload)

        outcome = poll_source(store, "upstream", runner=lambda *a, **k: produced)

        assert outcome.status is PollStatus.OK
        assert outcome.items[0].title == title

    @settings(max_examples=MAX_EXAMPLES, deadline=None)
    @given(identifier=hostile_text.filter(lambda text: text.strip()))
    def test_any_non_blank_identifier_makes_a_usable_item(self, identifier: str) -> None:
        item = WatchedItem(source="upstream", identifier=identifier)
        assert item.identifier == identifier
        assert item.fields["identifier"] == identifier

    @settings(max_examples=MAX_EXAMPLES, deadline=None)
    @given(identifier=st.sampled_from(("", " ", "\n", "\t ")))
    def test_a_blank_identifier_never_becomes_an_item(self, identifier: str) -> None:
        # Whitespace is not an identifier: the claim ledger dedupes on this
        # value, and a blank one can only be claimed always or never.
        with pytest.raises(ValueError):
            WatchedItem(source="upstream", identifier=identifier)
