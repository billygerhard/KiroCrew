"""Warning and acknowledgment for execution+ autonomy on a public watch source.

A publicly submittable source means a stranger can create the item that starts a
run. At execution autonomy or above that run spends credits and executes
configured commands with no human gate, so the operator is warned and the
acknowledgment is kept. The tests here ask four things of that: that a private
source and an authoring-only public source stay silent, that a public source at
execution and above warns, that an undetermined source is treated as public, and
that "execution or higher" is read off the ladder's ordering rather than a frozen
list of names — the last proven by driving every rung the ladder declares.
"""

from __future__ import annotations

from typing import Any

import pytest

from kiro_crew.apps.builtins.spec_engine.engine.config import (
    AUTONOMY_LEVELS,
    DASHBOARD_SURFACE,
    PUBLIC_SOURCE_AUTONOMY,
    Acknowledgment,
    ConfigStore,
    ConfigWarning,
    acknowledge,
    document_warnings,
)

SOURCE = "tracker"


def _source_document(
    *,
    autonomy: dict[str, Any] | None = None,
    public: bool | None = None,
) -> dict[str, Any]:
    """A one-source document, carrying a poll so it also survives validation."""
    entry: dict[str, Any] = {"poll": ["gh", "issue", "list"]}
    if public is not None:
        entry["public"] = public
    if autonomy is not None:
        entry["autonomy"] = autonomy
    return {"sources": {SOURCE: entry}}


def _public_warnings(doc: dict[str, Any]) -> tuple[ConfigWarning, ...]:
    return tuple(w for w in document_warnings(doc) if w.code == PUBLIC_SOURCE_AUTONOMY)


class TestWhenTheWarningFires:
    def test_a_private_source_at_execution_stays_silent(self) -> None:
        doc = _source_document(public=False, autonomy={"default": {"default": "execution"}})

        assert _public_warnings(doc) == ()

    def test_a_public_source_at_authoring_stays_silent(self) -> None:
        doc = _source_document(public=True, autonomy={"default": {"default": "authoring"}})

        assert _public_warnings(doc) == ()

    def test_a_public_source_at_execution_warns(self) -> None:
        doc = _source_document(public=True, autonomy={"default": {"default": "execution"}})

        warnings = _public_warnings(doc)

        assert [w.code for w in warnings] == [PUBLIC_SOURCE_AUTONOMY]
        assert warnings[0].path == f"sources.{SOURCE}.autonomy"
        assert warnings[0].requires_acknowledgment is True

    def test_an_undetermined_source_is_treated_as_public(self) -> None:
        # No 'public' key at all: the operator never said, so the warning fires.
        doc = _source_document(autonomy={"default": {"default": "execution"}})

        assert [w.code for w in _public_warnings(doc)] == [PUBLIC_SOURCE_AUTONOMY]

    def test_a_source_with_no_autonomy_grid_stays_silent(self) -> None:
        doc = _source_document(public=True)

        assert _public_warnings(doc) == ()


class TestTheLadderIsTheAuthority:
    """Coverage is derived from AUTONOMY_LEVELS, not a hardcoded name list.

    Driving every rung the ladder declares is what proves it: a level added above
    execution later joins this test's coverage the moment it joins the tuple,
    without the assertion or the module under test being edited. The threshold is
    execution's own index, so everything at or above it warns and everything
    below stays silent.
    """

    _EXECUTION_RANK = AUTONOMY_LEVELS.index("execution")

    @pytest.mark.parametrize("rank", range(len(AUTONOMY_LEVELS)))
    def test_every_rung_warns_exactly_when_it_is_execution_or_higher(self, rank: int) -> None:
        level = AUTONOMY_LEVELS[rank]
        doc = _source_document(public=True, autonomy={"default": {"default": level}})

        fired = bool(_public_warnings(doc))

        assert fired is (rank >= self._EXECUTION_RANK), level

    def test_a_rung_appended_above_execution_would_be_covered(self) -> None:
        # The ladder puts integration last; a hypothetical rung above it would sit
        # at a higher index, so the same index comparison already covers it. This
        # asserts the property directly against the tuple's own top rung.
        top = AUTONOMY_LEVELS[-1]
        doc = _source_document(public=True, autonomy={"default": {"default": top}})

        assert _public_warnings(doc) != ()


class TestWhichGrantsAreNamed:
    def test_one_warning_per_source_names_every_armed_cell(self) -> None:
        doc = _source_document(
            public=True,
            autonomy={
                "external": {"quick": "execution"},
                "maintainer": {"feature": "delivery"},
            },
        )

        warnings = _public_warnings(doc)

        assert len(warnings) == 1
        message = warnings[0].message
        assert "sources.tracker.autonomy.external.quick = execution" in message
        assert "sources.tracker.autonomy.maintainer.feature = delivery" in message

    def test_an_authoring_cell_beside_an_execution_cell_does_not_suppress(self) -> None:
        doc = _source_document(
            public=True,
            autonomy={
                "maintainer": {"default": "authoring"},
                "external": {"default": "integration"},
            },
        )

        warnings = _public_warnings(doc)

        assert len(warnings) == 1
        assert "external.default = integration" in warnings[0].message
        # The authoring cell is not an armed grant and must not be listed.
        assert "authoring" not in warnings[0].message


class TestReachedThroughTheWritePath:
    """The warning covers every way autonomy is armed, not one write shape.

    document_warnings runs on the fully merged document at write time, so a whole
    document write and a later partial merge that adds the autonomy both reach it.
    """

    @pytest.fixture()
    def store(self, tmp_path: Any) -> ConfigStore:
        return ConfigStore(tmp_path / "state")

    def test_a_whole_document_write_earns_the_warning(self, store: ConfigStore) -> None:
        seen: list[ConfigWarning] = []
        store.write(
            _source_document(public=True, autonomy={"default": {"default": "execution"}}),
            surface=DASHBOARD_SURFACE,
            warn=seen.append,
        )

        assert [w.code for w in seen if w.code == PUBLIC_SOURCE_AUTONOMY] == [
            PUBLIC_SOURCE_AUTONOMY
        ]

    def test_a_partial_merge_that_arms_autonomy_later_earns_the_warning(
        self, store: ConfigStore
    ) -> None:
        # First write a public source with no autonomy: nothing to acknowledge.
        store.write(_source_document(public=True), surface=DASHBOARD_SURFACE)
        seen: list[ConfigWarning] = []

        # A second write merges the autonomy grid in; the advisory sees the merge.
        store.write(
            {"sources": {SOURCE: {"autonomy": {"default": {"default": "delivery"}}}}},
            surface=DASHBOARD_SURFACE,
            warn=seen.append,
        )

        assert [w.code for w in seen if w.code == PUBLIC_SOURCE_AUTONOMY] == [
            PUBLIC_SOURCE_AUTONOMY
        ]

    def test_the_persisted_document_reports_the_advisory(self, store: ConfigStore) -> None:
        store.write(
            _source_document(public=True, autonomy={"default": {"default": "execution"}}),
            surface=DASHBOARD_SURFACE,
        )

        codes = [w.code for w in store.advisories()]

        assert PUBLIC_SOURCE_AUTONOMY in codes


class TestTheAcknowledgment:
    """The record that a human was told cannot be produced implicitly."""

    def _warning(self) -> ConfigWarning:
        (warning,) = _public_warnings(
            _source_document(public=True, autonomy={"default": {"default": "execution"}})
        )
        return warning

    def test_a_named_operator_acknowledgment_is_built(self) -> None:
        warning = self._warning()

        ack = acknowledge(warning, "alice")

        assert isinstance(ack, Acknowledgment)
        assert ack.actor == "alice"
        assert ack.code == PUBLIC_SOURCE_AUTONOMY
        assert ack.path == warning.path

    def test_the_acknowledgment_detail_records_who_and_what(self) -> None:
        ack = acknowledge(self._warning(), "  bob  ")

        # Surrounding whitespace is stripped: the identity is what is recorded.
        assert ack.detail == {
            "code": PUBLIC_SOURCE_AUTONOMY,
            "path": f"sources.{SOURCE}.autonomy",
            "actor": "bob",
        }

    def test_an_empty_actor_is_refused(self) -> None:
        with pytest.raises(ValueError, match="identity"):
            acknowledge(self._warning(), "")

    def test_a_whitespace_only_actor_is_refused(self) -> None:
        # Whitespace is not a signature: it would let an absent value stand in.
        with pytest.raises(ValueError, match="identity"):
            acknowledge(self._warning(), "   ")

    def test_a_warning_that_needs_no_acknowledgment_cannot_be_acknowledged(self) -> None:
        display_only = ConfigWarning(code="x", path="y", message="z")

        with pytest.raises(ValueError, match="does not require"):
            acknowledge(display_only, "alice")


class TestSchemaAcceptsThePublicFlag:
    @pytest.fixture()
    def store(self, tmp_path: Any) -> ConfigStore:
        return ConfigStore(tmp_path / "state")

    def test_a_boolean_public_flag_validates(self, store: ConfigStore) -> None:
        store.write(_source_document(public=False), surface=DASHBOARD_SURFACE)

        assert store.validate() == ()

    def test_a_non_boolean_public_flag_is_rejected(self, store: ConfigStore) -> None:
        from kiro_crew.apps.builtins.spec_engine.engine.config import ConfigValidationError

        with pytest.raises(ConfigValidationError):
            store.write(
                {"sources": {SOURCE: {"poll": ["gh"], "public": "yes"}}},
                surface=DASHBOARD_SURFACE,
            )
