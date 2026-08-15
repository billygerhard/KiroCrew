"""Echo gate: which authored text a writeback may republish to a shared tracker.

Requirement 36.7 says a writeback may echo a Content_Element's text only where
that element's own submitter class is configured as permitted, and never for the
least-trusted class. These tests exercise the four decisions the gate makes and
the two mechanisms behind them: permission is per class and off by default, the
least-trusted floor holds even when configuration tries to lift it, the decision
is made on the element's *own* class (never the item's), and text is reached
through ``consume`` so an edit after the gate ran cannot be echoed under it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.apps.builtins.spec_engine.engine.config.schema import (
    LEAST_TRUSTED_CLASS,
    WILDCARD_KEY,
)
from kiro_crew.apps.builtins.spec_engine.engine.config.store import ConfigStore
from kiro_crew.apps.builtins.spec_engine.engine.delivery.variables import (
    RUN_CONTEXT_VARIABLES,
    RunContext,
)
from kiro_crew.apps.builtins.spec_engine.engine.trust import (
    ContentElement,
    ElementKind,
    StaleContent,
    derive,
)
from kiro_crew.apps.builtins.spec_engine.engine.watch.dispatch import SourceRoute
from kiro_crew.apps.builtins.spec_engine.engine.watch.echo import (
    ECHO_FIELD,
    ECHOED_CONTEXT_FIELDS,
    EchoCandidate,
    echo_permitted_for,
    echoable_text,
    echoed_context,
)

SOURCE = "tracker"
MAINTAINER = "alice-smith"


@pytest.fixture()
def route(tmp_path: Path) -> SourceRoute:
    return SourceRoute(
        source=SOURCE,
        project="proj",
        working_tree=tmp_path / "tree",
        maintainers=frozenset({MAINTAINER}),
    )


@pytest.fixture()
def config(tmp_path: Path) -> ConfigStore:
    return ConfigStore(root=tmp_path / "config")


def set_echo(config: ConfigStore, **classes: Any) -> None:
    """Hand-write an ``echo`` map onto the source.

    Written straight to the document rather than through ``config.write`` on
    purpose: the schema does not know the ``echo`` field yet, so the validated
    write path refuses it. Testing the gate against a hand-edited document is the
    same shape ``load_feedback``'s tests use, and it is the state the gate will
    read once the schema owner adds the field.
    """
    config.path.parent.mkdir(parents=True, exist_ok=True)
    document = {"sources": {SOURCE: {ECHO_FIELD: dict(classes)}}}
    config.path.write_text(json.dumps(document), encoding="utf-8")


def comment(author: str, *, association: str = "", text: str, element_id: str = "c-1"):
    return ContentElement(
        kind=ElementKind.ITEM_COMMENT,
        element_id=element_id,
        author=author,
        association=association,
        text=text,
    )


def member_element(text: str = "please look at this", element_id: str = "c-1") -> ContentElement:
    return comment("org-teammate", association="member", text=text, element_id=element_id)


class TestPermittedClass:
    def test_a_configured_class_is_echoed(self, route: SourceRoute, config: ConfigStore) -> None:
        set_echo(config, member=True)
        element = member_element(text="the words to echo")
        trust = derive(route, element)

        assert trust.class_name == "member"
        assert echo_permitted_for(config, SOURCE, "member") is True
        assert echoable_text(config, SOURCE, element, trust) == "the words to echo"

    def test_a_permitted_maintainer_is_echoed(
        self, route: SourceRoute, config: ConfigStore
    ) -> None:
        set_echo(config, maintainer=True)
        element = comment(MAINTAINER, text="maintainer note")
        trust = derive(route, element)

        assert trust.class_name == "maintainer"
        assert echoable_text(config, SOURCE, element, trust) == "maintainer note"


class TestUnpermittedClass:
    def test_a_class_not_in_the_echo_map_is_refused(
        self, route: SourceRoute, config: ConfigStore
    ) -> None:
        set_echo(config, maintainer=True)  # member is not listed
        element = member_element()
        trust = derive(route, element)

        assert echo_permitted_for(config, SOURCE, "member") is False
        assert echoable_text(config, SOURCE, element, trust) is None

    def test_echo_is_off_by_default(self, route: SourceRoute, config: ConfigStore) -> None:
        # No echo map at all: writeback is disabled by default, so echo is too.
        element = member_element()
        trust = derive(route, element)

        assert echo_permitted_for(config, SOURCE, "member") is False
        assert echoable_text(config, SOURCE, element, trust) is None

    def test_only_boolean_true_permits(self, config: ConfigStore) -> None:
        # A truthy non-boolean must not be read as permission.
        set_echo(config, member="true")
        assert echo_permitted_for(config, SOURCE, "member") is False
        set_echo(config, member=1)
        assert echo_permitted_for(config, SOURCE, "member") is False
        set_echo(config, member=False)
        assert echo_permitted_for(config, SOURCE, "member") is False

    def test_an_undeclared_source_is_refused(self, config: ConfigStore) -> None:
        set_echo(config, member=True)
        assert echo_permitted_for(config, "some-other-source", "member") is False

    def test_a_wildcard_key_never_permits(self, config: ConfigStore) -> None:
        # A single wildcard entry must not permit echo for every class at once.
        set_echo(config, **{WILDCARD_KEY: True})
        assert echo_permitted_for(config, SOURCE, WILDCARD_KEY) is False


class TestLeastTrustedFloor:
    def test_least_trusted_is_refused_even_when_explicitly_permitted(
        self, route: SourceRoute, config: ConfigStore
    ) -> None:
        # The floor is a guarantee configuration cannot lift: echo.<least> = true
        # must still refuse.
        set_echo(config, **{LEAST_TRUSTED_CLASS: True})
        element = comment("drive-by-account", text="attacker text")
        trust = derive(route, element)

        assert trust.class_name == LEAST_TRUSTED_CLASS
        assert echo_permitted_for(config, SOURCE, LEAST_TRUSTED_CLASS) is False
        assert echoable_text(config, SOURCE, element, trust) is None

    def test_an_undetermined_author_lands_on_the_refused_floor(
        self, route: SourceRoute, config: ConfigStore
    ) -> None:
        set_echo(config, **{LEAST_TRUSTED_CLASS: True, "member": True})
        element = comment("nobody-knows-them", text="unattributed")
        trust = derive(route, element)

        assert trust.class_name == LEAST_TRUSTED_CLASS
        assert echoable_text(config, SOURCE, element, trust) is None


class TestElementOwnClass:
    def test_the_gate_decides_on_the_elements_own_class_not_the_items(
        self, route: SourceRoute, config: ConfigStore
    ) -> None:
        """Two comments on one item, different authors, opposite decisions.

        The gate is handed each element's own derived trust, so a maintainer's
        comment is echoed while a stranger's on the same item is refused. A gate
        that read one item-level class would decide both the same way.
        """
        set_echo(config, maintainer=True)
        maintainer_comment = comment(MAINTAINER, text="mine", element_id="c-1")
        stranger_comment = comment("drive-by", text="theirs", element_id="c-2")

        maintainer_trust = derive(route, maintainer_comment)
        stranger_trust = derive(route, stranger_comment)

        assert echoable_text(config, SOURCE, maintainer_comment, maintainer_trust) == "mine"
        assert echoable_text(config, SOURCE, stranger_comment, stranger_trust) is None

    def test_a_maintainers_comment_on_a_strangers_item_is_still_a_maintainers(
        self, route: SourceRoute, config: ConfigStore
    ) -> None:
        # The gate never sees the item; it can only reflect the element's author.
        set_echo(config, maintainer=True)
        element = comment(MAINTAINER, text="on someone else's issue")
        trust = derive(route, element)
        assert echoable_text(config, SOURCE, element, trust) == "on someone else's issue"


class TestEditedAfterGate:
    def test_text_edited_after_the_gate_ran_cannot_be_echoed(
        self, route: SourceRoute, config: ConfigStore
    ) -> None:
        """A permitted decision about one revision does not license a later one."""
        set_echo(config, member=True)
        original = member_element(text="the reviewed words")
        trust = derive(route, original)
        # The gate ran and permitted the original revision.
        assert echoable_text(config, SOURCE, original, trust) == "the reviewed words"

        edited = member_element(text="something else entirely")
        # Same element, new text, old decision: consume refuses rather than echo.
        with pytest.raises(StaleContent):
            echoable_text(config, SOURCE, edited, trust)


def run_context(**fields: str) -> RunContext:
    return RunContext(
        spec_name="example",
        spec_type="feature",
        workspace_path="/tmp/example",
        **fields,
    )


class TestGateSitsAtContextPopulation:
    """The gate's placement, not just its answer.

    ``review_title`` and ``review_summary`` are engine-owned run context
    variables, so the shared executor substitutes them for a delivery stage
    command and a feedback command alike. These pin that the decision is made
    where the element's text becomes one of those fields -- the one point
    upstream of every consumer -- rather than in front of any single writeback.
    """

    def test_every_gated_field_is_a_run_context_variable(self) -> None:
        """A field name the run context does not own would be gated and inert.

        The gate's whole claim is that it covers the shared executor, and it
        covers it by owning the variables that executor substitutes. A field here
        that ``RUN_CONTEXT_VARIABLES`` does not carry would be a name no command
        can reference, so the gate would guard nothing.
        """
        assert set(ECHOED_CONTEXT_FIELDS) <= set(RUN_CONTEXT_VARIABLES)

    def test_a_permitted_class_reaches_the_variable_set(
        self, route: SourceRoute, config: ConfigStore
    ) -> None:
        set_echo(config, member=True)
        element = member_element(text="please rename the flag")
        gated = echoed_context(
            config,
            SOURCE,
            run_context(),
            summary=EchoCandidate(element, derive(route, element)),
        )

        assert gated.context.review_summary == "please rename the flag"
        assert gated.echoed == ("review_summary",)
        assert gated.omitted == ()
        # The variable set is what a command actually renders against.
        assert gated.context.to_variables()["review_summary"] == "please rename the flag"

    def test_a_refused_class_omits_the_variable_rather_than_emptying_it(
        self, route: SourceRoute, config: ConfigStore
    ) -> None:
        """Omission and emptying are not the same outcome, and only one is safe.

        An empty variable still substitutes: the command runs with an argument
        that means whatever the program decides an empty string means. An omitted
        variable has no value, so the template layer refuses the command before
        anything spawns. This pins the absence, not merely the falsiness.
        """
        set_echo(config, maintainer=True)  # member is not permitted
        element = member_element(text="untrusted words")
        gated = echoed_context(
            config,
            SOURCE,
            run_context(),
            summary=EchoCandidate(element, derive(route, element)),
        )

        assert "review_summary" not in gated.context.to_variables()
        assert gated.echoed == ()
        assert [omission.field for omission in gated.omitted] == ["review_summary"]
        assert gated.omitted[0].class_name == "member"

    def test_a_refused_field_is_cleared_even_when_the_base_context_carried_text(
        self, route: SourceRoute, config: ConfigStore
    ) -> None:
        """A pre-populated base context must not be a way past the gate.

        Were a refusal to leave the incoming value alone, a caller could put the
        element's text on the context first and then ask the gate about it, and
        the gate would refuse while the text stayed exactly where a command reads
        it.
        """
        set_echo(config, maintainer=True)
        element = member_element(text="untrusted words")
        gated = echoed_context(
            config,
            SOURCE,
            run_context(review_summary="untrusted words"),
            summary=EchoCandidate(element, derive(route, element)),
        )

        assert gated.context.review_summary == ""
        assert "review_summary" not in gated.context.to_variables()

    def test_a_field_the_caller_did_not_name_is_left_alone(
        self, route: SourceRoute, config: ConfigStore
    ) -> None:
        """An engine-authored title is not element text and not this gate's business."""
        set_echo(config, member=True)
        element = member_element(text="the comment")
        gated = echoed_context(
            config,
            SOURCE,
            run_context(review_title="Spec: example"),
            summary=EchoCandidate(element, derive(route, element)),
        )

        assert gated.context.review_title == "Spec: example"

    def test_the_least_trusted_floor_holds_at_population(
        self, route: SourceRoute, config: ConfigStore
    ) -> None:
        set_echo(config, **{LEAST_TRUSTED_CLASS: True})
        element = comment("drive-by-account", text="attacker text")
        gated = echoed_context(
            config,
            SOURCE,
            run_context(),
            title=EchoCandidate(element, derive(route, element)),
        )

        assert "review_title" not in gated.context.to_variables()
        assert gated.omitted[0].class_name == LEAST_TRUSTED_CLASS

    def test_blank_text_is_not_carried_as_an_empty_argument(
        self, route: SourceRoute, config: ConfigStore
    ) -> None:
        set_echo(config, member=True)
        element = member_element(text="   ")
        gated = echoed_context(
            config,
            SOURCE,
            run_context(),
            summary=EchoCandidate(element, derive(route, element)),
        )

        assert "review_summary" not in gated.context.to_variables()
        assert gated.omitted[0].reason.endswith("nothing to echo")

    def test_naming_no_candidate_returns_the_context_untouched(self, config: ConfigStore) -> None:
        base = run_context(review_title="Spec: example")
        gated = echoed_context(config, SOURCE, base)
        assert gated.context is base
        assert gated.omitted == ()

    def test_two_elements_are_decided_separately(
        self, route: SourceRoute, config: ConfigStore
    ) -> None:
        """One permitted field and one refused, in a single population call."""
        set_echo(config, maintainer=True)
        title = comment(MAINTAINER, text="Fix the flag", element_id="c-1")
        summary = comment("drive-by", text="attacker text", element_id="c-2")
        gated = echoed_context(
            config,
            SOURCE,
            run_context(),
            title=EchoCandidate(title, derive(route, title)),
            summary=EchoCandidate(summary, derive(route, summary)),
        )

        variables = gated.context.to_variables()
        assert variables["review_title"] == "Fix the flag"
        assert "review_summary" not in variables


class TestStaleContentIsSkippedNotRaised:
    """An edit between derivation and population omits the field, silently.

    Letting ``StaleContent`` propagate is safe in itself -- nothing is echoed --
    but a writeback that receives it records a FAILURE, and a failed writeback
    keeps its ledger claim, which suppresses that lifecycle event for the rest of
    the run. A refusal to echo one field would then permanently silence an event.
    """

    def test_an_edited_element_omits_the_field_instead_of_raising(
        self, route: SourceRoute, config: ConfigStore
    ) -> None:
        set_echo(config, member=True)
        original = member_element(text="the reviewed words")
        trust = derive(route, original)
        edited = member_element(text="something else entirely")

        gated = echoed_context(
            config,
            SOURCE,
            run_context(),
            summary=EchoCandidate(edited, trust),
        )

        assert "review_summary" not in gated.context.to_variables()
        assert "edited" in gated.omitted[0].reason

    def test_a_rederive_seam_answers_the_edit_rather_than_skipping_it(
        self, route: SourceRoute, config: ConfigStore
    ) -> None:
        set_echo(config, member=True)
        original = member_element(text="the reviewed words")
        stale = derive(route, original)
        edited = member_element(text="the edited words")

        gated = echoed_context(
            config,
            SOURCE,
            run_context(),
            summary=EchoCandidate(edited, stale),
            rederive=lambda element: derive(route, element),
        )

        assert gated.context.review_summary == "the edited words"
        assert gated.omitted == ()

    def test_a_rederive_that_lands_on_a_refused_class_still_omits(
        self, route: SourceRoute, config: ConfigStore
    ) -> None:
        """Re-deriving is not a way around the permission check.

        The re-derived class is checked exactly like the first one, so an element
        whose author is not permitted is refused however many times its trust is
        derived.
        """
        set_echo(config, maintainer=True)
        original = member_element(text="the reviewed words")
        stale = derive(route, original)
        edited = member_element(text="the edited words")

        gated = echoed_context(
            config,
            SOURCE,
            run_context(),
            summary=EchoCandidate(edited, stale),
            rederive=lambda element: derive(route, element),
        )

        assert "review_summary" not in gated.context.to_variables()

    def test_a_rederive_that_itself_fails_omits_rather_than_raising(
        self, route: SourceRoute, config: ConfigStore
    ) -> None:
        set_echo(config, member=True)
        stale = derive(route, member_element(text="the reviewed words"))
        edited = member_element(text="the edited words")

        def broken(element: ContentElement) -> Any:
            raise RuntimeError("the tracker could not be reached")

        gated = echoed_context(
            config,
            SOURCE,
            run_context(),
            summary=EchoCandidate(edited, stale),
            rederive=broken,
        )

        assert "review_summary" not in gated.context.to_variables()
        assert "could not be re-derived" in gated.omitted[0].reason

    def test_an_element_edited_again_during_rederive_is_not_retried_forever(
        self, route: SourceRoute, config: ConfigStore
    ) -> None:
        set_echo(config, member=True)
        stale = derive(route, member_element(text="one"))
        edited = member_element(text="two")
        # A re-derive that answers about yet another revision: one retry, then a skip.
        derived: list[str] = []

        def moving(element: ContentElement) -> Any:
            derived.append(element.element_id)
            return derive(route, member_element(text="three"))

        gated = echoed_context(
            config,
            SOURCE,
            run_context(),
            summary=EchoCandidate(edited, stale),
            rederive=moving,
        )

        assert derived == ["c-1"]
        assert "review_summary" not in gated.context.to_variables()
        assert "edited again" in gated.omitted[0].reason
