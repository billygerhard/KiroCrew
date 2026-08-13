"""Bundled tracker-housekeeping presets, and the guarantees around enabling them.

The presets are public-host only by construction, and turning feedback on is a
per-event-per-source configuration act no tool can perform. These tests pin both
so a later change cannot ship a private-tracker preset or open an enable-all
switch without turning one red.
"""

from __future__ import annotations

import pytest

from kiro_crew.apps.builtins.spec_engine.engine.config.schema import (
    CONFIG_ONLY_PATHS,
    ITEM_LIFECYCLE_EVENTS,
    SECTION_SOURCES,
    WILDCARD_KEY,
    config_only_paths,
    validate_config_document,
)
from kiro_crew.apps.builtins.spec_engine.engine.delivery.templates import CommandTemplate
from kiro_crew.apps.builtins.spec_engine.engine.delivery.variables import (
    RUN_CONTEXT_VARIABLES,
)
from kiro_crew.apps.builtins.spec_engine.engine.watch.feedback import (
    FEEDBACK_PRESET_HOSTS,
    FEEDBACK_PRESETS,
    feedback_presets,
)

# Which run-context variables the engine has populated by each lifecycle point.
# A preset command may only reference a variable present when its event fires,
# because a referenced-but-unset variable fails the event before it runs.
_ITEM_VARS = {"item_url", "item_id"}
_VARS_AVAILABLE_AT = {
    "claimed": _ITEM_VARS,
    "awaiting_review": _ITEM_VARS | {"review_title", "review_summary"},
    "delivery_submitted": _ITEM_VARS | {"branch_name", "review_title", "review_summary"},
    "completed": _ITEM_VARS | {"branch_name", "review_title", "review_summary"},
    "failed": _ITEM_VARS | {"branch_name", "review_title", "review_summary"},
    "refused": _ITEM_VARS,
}


class TestPublicHostOnly:
    def test_only_public_hosts_are_bundled(self) -> None:
        assert set(FEEDBACK_PRESETS) == {"github", "gitlab"}
        assert FEEDBACK_PRESET_HOSTS == ("github", "gitlab")

    def test_an_unbundled_host_raises_rather_than_inventing_a_preset(self) -> None:
        """The structural half of "no non-public preset": every miss raises.

        There is no name a caller can pass that yields a preset for a private
        tracker, because a host not in the table raises instead of returning an
        empty or fabricated map an organization's argv could be read into.
        """
        with pytest.raises(KeyError):
            feedback_presets("internal-tracker")

    def test_the_preset_programs_are_the_public_host_clis_only(self) -> None:
        programs = {
            CommandTemplate.parse(argv).program
            for host in FEEDBACK_PRESETS
            for commands in FEEDBACK_PRESETS[host].values()
            for argv in commands
        }
        assert programs == {"gh", "glab"}


class TestPresetShape:
    @pytest.mark.parametrize("host", ["github", "gitlab"])
    def test_a_preset_validates_as_a_source_feedback_map(self, host: str) -> None:
        """Copied into a source, a preset passes the same schema its reader uses.

        The reader (load_feedback) and the writer (schema validation) must accept
        the identical shape, so a preset that validated but would not load, or
        loaded but would not validate, is caught here rather than at first use.
        """
        doc = {
            "sources": {
                "tracker": {
                    "poll": ["list-issues"],
                    "feedback": feedback_presets(host),
                }
            }
        }
        assert validate_config_document(doc) == ()

    @pytest.mark.parametrize("host", ["github", "gitlab"])
    def test_returned_map_is_a_deep_copy(self, host: str) -> None:
        first = feedback_presets(host)
        first["claimed"].append(["gh", "tampered"])
        first["claimed"][0].append("--mutated")
        second = feedback_presets(host)
        assert ["gh", "tampered"] not in second["claimed"]
        assert "--mutated" not in second["claimed"][0]

    @pytest.mark.parametrize("host", ["github", "gitlab"])
    def test_every_event_is_a_known_lifecycle_event(self, host: str) -> None:
        for event in FEEDBACK_PRESETS[host]:
            assert event in ITEM_LIFECYCLE_EVENTS

    @pytest.mark.parametrize("host", ["github", "gitlab"])
    def test_commands_reference_only_variables_present_when_the_event_fires(
        self, host: str
    ) -> None:
        """A preset that named an unset variable would fail its event every run.

        Every referenced name must be a real run-context variable, and one the
        engine has populated by the time that event fires -- otherwise the
        starting point the operator copies is broken out of the box.
        """
        for event, commands in FEEDBACK_PRESETS[host].items():
            available = _VARS_AVAILABLE_AT[event]
            for argv in commands:
                referenced = set(CommandTemplate.parse(argv).variables)
                assert referenced <= set(RUN_CONTEXT_VARIABLES), (event, argv, referenced)
                assert referenced <= available, (event, argv, referenced - available)

    def test_the_bundled_operations_span_comment_label_state_and_assign(self) -> None:
        """The presets demonstrate the operations requirement 36.1 names.

        Read off the GitHub preset's argv rather than asserted by a second list,
        so the check tracks the commands themselves.
        """
        flat = [tuple(argv) for cmds in FEEDBACK_PRESETS["github"].values() for argv in cmds]
        joined = [" ".join(a) for a in flat]
        assert any("issue comment" in j for j in joined)  # comment
        assert any("--add-label" in j for j in joined)  # set label
        assert any("issue close" in j for j in joined)  # set state
        assert any("--add-assignee" in j for j in joined)  # assign


class TestEnableIsConfigurationOnly:
    def test_feedback_lives_under_the_config_only_sources_subtree(self) -> None:
        """No tool can enable feedback: sources is fenced as a whole subtree.

        The guarantee is not spelled at sources.*.feedback specifically; it is
        that the entire sources subtree is config-only, so every field under it,
        including feedback, is unreachable by a tool write. Asserting the whole
        subtree rather than the one spelling is the point -- a feedback-specific
        fence would leave a sibling field reachable.
        """
        assert SECTION_SOURCES in CONFIG_ONLY_PATHS
        patch = {"sources": {"tracker": {"feedback": {"claimed": [["gh", "x"]]}}}}
        assert config_only_paths(patch) == ("sources",)

    def test_a_wildcard_event_key_is_rejected_as_an_enable_all_switch(self) -> None:
        """A default/wildcard event would turn feedback on for every event at once.

        It is refused because it is not a lifecycle event, which is the same
        structural refusal screening and echo give their wildcard: there is no
        single key that enables the mechanism across the board.
        """
        assert WILDCARD_KEY not in ITEM_LIFECYCLE_EVENTS
        doc = {
            "sources": {
                "tracker": {
                    "poll": ["list-issues"],
                    "feedback": {WILDCARD_KEY: [["gh", "issue", "comment"]]},
                }
            }
        }
        errors = validate_config_document(doc)
        assert any("feedback" in e.path for e in errors)

    def test_a_source_with_no_feedback_map_enables_nothing(self) -> None:
        """Disabled by default: a source that declares no feedback is valid and silent."""
        doc = {"sources": {"tracker": {"poll": ["list-issues"]}}}
        assert validate_config_document(doc) == ()
