"""The authority a resumed run acts under, and where it may not come from.

A run is admitted once, under one resolved rung, and that rung is persisted on
its row. Everything here is about the second and later times the question is
asked. Three claims:

*The row is the authority.* :func:`authority_for` reconstructs the rung from the
``posture`` column and the ``autonomy`` detail key that mirrors it, and takes the
lower of the two when they disagree — so a writer that updated one and not the
other can only narrow what a run may do.

*A quarantine survives the resume.* An item intake screening suspected of prompt
injection is capped to authoring "regardless of policy", and the case that makes
that claim mean something is a run whose configured rung is ``integration``: the
policy would authorize everything, the row says the item was never cleared, and
the row wins. The counterfactual is asserted in the same shape — the same spec,
the same configuration, the quarantine mark removed — so the refusal is known to
come from the quarantine and not from the spec being unready.

*The wrong source is unreachable rather than merely unused.* The run-scoped gate
takes no decision, no level, and no policy, and :mod:`~...engine.resume` does not
import :class:`AutonomyPolicy`, so re-resolution is not something a later caller
can reach for by accident. Those two are asserted structurally, because "nobody
does that today" is not a property.

The detail keys the reconstruction reads are pinned against what the dispatcher
actually writes into a run row, not against a restatement of them: a rename on
the writing side that left the reader's constant behind would make a quarantined
run look unmarked, which is the one failure this module exists to prevent.
"""

from __future__ import annotations

import ast
import inspect
import json
from pathlib import Path
from typing import Any, Iterator

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from kiro_crew.apps.builtins.spec_engine.engine import resume as resume_module
from kiro_crew.apps.builtins.spec_engine.engine.audit import AuditLog
from kiro_crew.apps.builtins.spec_engine.engine.autonomy import (
    AUTONOMY_FIELD,
    AutonomyLevel,
    AutonomyPolicy,
)
from kiro_crew.apps.builtins.spec_engine.engine.config import (
    DASHBOARD_SURFACE,
    LEAST_TRUSTED_CLASS,
    WILDCARD_KEY,
    ConfigStore,
)
from kiro_crew.apps.builtins.spec_engine.engine.phases import (
    EXECUTION_REFUSED_EVENT,
    EXECUTION_STARTED_EVENT,
    REASON_HUMAN_REQUIRED,
    policy_declaration,
)
from kiro_crew.apps.builtins.spec_engine.engine.resume import (
    DETAIL_AUTONOMY,
    DETAIL_SCREENING_QUARANTINED,
    DETAIL_SPEC_TYPE,
    DETAIL_SUBMITTER_CLASS,
    ResumeAuthority,
    authority_for,
    request_execution_for_run,
)
from kiro_crew.apps.builtins.spec_engine.engine.runs import RunState, UnknownRun
from kiro_crew.apps.builtins.spec_engine.engine.state import RunRecord, SpecRef, StateStore
from kiro_crew.apps.builtins.spec_engine.engine.watch.dispatch import (
    ClassEvidence,
    RunSeed,
    SubmitterClass,
    dispatch_source,
)
from kiro_crew.apps.builtins.spec_engine.engine.watch.items import WatchedItem

from .test_intake_screening import (
    INJECTION_BODY,
)
from .test_intake_screening import SOURCE as SCREENED_SOURCE
from .test_intake_screening import (
    SuspectProvider,
    _AllowAll,
    _item,
    _polled,
    _screener,
    _Starter,
    _tree,
    _write_config,
)
from .test_phases import SPEC_NAME, approve_gates, write_spec

SOURCE = "tracker"
RUN = "run-resume-1"
USER = "user:ada"


@pytest.fixture()
def store(tmp_path: Path) -> Iterator[StateStore]:
    handle = StateStore(root=tmp_path / "state")
    yield handle
    handle.close()


@pytest.fixture()
def log(tmp_path: Path) -> AuditLog:
    return AuditLog(root=tmp_path / "audit")


@pytest.fixture()
def ref(tmp_path: Path) -> SpecRef:
    """A spec whose three documents are real and format-clean."""
    return SpecRef.of(write_spec(tmp_path / "workspace"), SPEC_NAME)


def admit(
    store: StateStore,
    ref: SpecRef,
    *,
    level: str | None,
    quarantined: bool = False,
    mirrored: str | None = "",
    run_id: str = RUN,
) -> RunRecord:
    """Create a run row the way an admitted dispatch leaves one.

    *level* is what the dispatcher persisted; ``mirrored`` defaults to the same
    value, because both writers write both fields. Passing a different one is how
    the disagreement cases are set up.
    """
    detail: dict[str, Any] = {
        DETAIL_SPEC_TYPE: "feature",
        DETAIL_SUBMITTER_CLASS: "member",
    }
    if level is not None:
        detail[DETAIL_AUTONOMY] = level if mirrored == "" else mirrored
    if quarantined:
        detail[DETAIL_SCREENING_QUARANTINED] = True
    return store.create_run(
        run_id,
        ref,
        state=RunState.QUEUED.value,
        source=SOURCE,
        item_id="item-7",
        posture=level,
        detail=detail,
    )


def configure(store: ConfigStore, level: str) -> None:
    """Declare *level* for every triple of the watch source."""
    grid = {WILDCARD_KEY: {WILDCARD_KEY: level}}
    store.write(
        {"sources": {SOURCE: {"poll": ["watch"], AUTONOMY_FIELD: grid}}},
        surface=DASHBOARD_SURFACE,
    )


def settle(store: StateStore, ref: SpecRef) -> None:
    """Approve every document gate, leaving authority the only open question."""
    approve_gates(store, ref, "requirements", "design", "tasks", actor=USER)


class TestTheRowIsTheAuthority:
    def test_the_persisted_rung_is_what_the_authority_carries(
        self, store: StateStore, ref: SpecRef
    ) -> None:
        record = admit(store, ref, level="delivery")

        authority = authority_for(record)

        assert authority.level is AutonomyLevel.DELIVERY
        assert authority.decision.is_configured
        assert authority.recorded_posture == "delivery"
        assert authority.quarantined is False
        assert authority.narrowed is False

    def test_the_declaration_names_the_row_rather_than_a_configuration_path(
        self, store: StateStore, ref: SpecRef
    ) -> None:
        # An audit reader must not be told configuration was consulted, because it
        # was not. The declaration is where the rung was actually read.
        authority = authority_for(admit(store, ref, level="integration"))

        assert authority.decision.declared_at == f"runs.{RUN}.posture"
        assert "sources" not in authority.decision.declared_at

    def test_a_row_with_no_posture_reserves_execution_for_a_person(
        self, store: StateStore, ref: SpecRef
    ) -> None:
        authority = authority_for(admit(store, ref, level=None))

        assert authority.level is AutonomyLevel.AUTHORING
        # Unconfigured, not "authoring as declared": nothing on the row said how
        # far this run may go, and an empty declaration is what makes
        # policy_authorizes_execution answer no even if the rung were raised.
        assert not authority.decision.is_configured
        assert authority.decision.execution_is_human_reserved

    @pytest.mark.parametrize("stored", ["", "root", "INTEGRATION", "delivery "])
    def test_a_rung_the_ladder_does_not_know_is_not_substituted(
        self, store: StateStore, ref: SpecRef, stored: str
    ) -> None:
        # A hand-edited row and a column from an older schema both arrive as a
        # string the ladder cannot place. Guessing one would invent authority.
        authority = authority_for(admit(store, ref, level=stored))

        assert authority.level is AutonomyLevel.AUTHORING
        assert not authority.decision.is_configured

    def test_the_lower_of_two_disagreeing_fields_wins(
        self, store: StateStore, ref: SpecRef
    ) -> None:
        record = admit(store, ref, level="integration", mirrored="authoring")

        authority = authority_for(record)

        assert authority.level is AutonomyLevel.AUTHORING
        assert authority.narrowed is True
        # The column is still reported verbatim, so a surface can show the operator
        # the disagreement instead of only its consequence.
        assert authority.recorded_posture == "integration"

    def test_the_lower_field_wins_whichever_side_it_is_on(
        self, store: StateStore, ref: SpecRef
    ) -> None:
        # The mirror image of the case above. A min() over one field, or a
        # preference for whichever field the writer happened to update last, passes
        # one of these two and fails the other.
        authority = authority_for(admit(store, ref, level="authoring", mirrored="integration"))

        assert authority.level is AutonomyLevel.AUTHORING
        assert authority.narrowed is True

    def test_the_descriptive_fields_come_from_the_row(
        self, store: StateStore, ref: SpecRef
    ) -> None:
        authority = authority_for(admit(store, ref, level="execution"))

        assert authority.decision.source == SOURCE
        assert authority.decision.spec_type == "feature"
        assert authority.decision.submitter_class == "member"

    def test_a_class_outside_the_vocabulary_falls_back_to_the_least_trusted(
        self, store: StateStore, ref: SpecRef
    ) -> None:
        record = admit(store, ref, level="execution")
        rewritten = store.update_run(
            RUN, detail={DETAIL_SUBMITTER_CLASS: "owner", DETAIL_SPEC_TYPE: "epic"}
        )
        assert record.detail[DETAIL_SUBMITTER_CLASS] == "member"

        authority = authority_for(rewritten)

        assert authority.decision.submitter_class == LEAST_TRUSTED_CLASS
        assert authority.decision.spec_type == ""


class TestTheDetailKeysMatchTheWriter:
    def test_the_keys_the_reader_uses_are_the_keys_the_dispatcher_writes(
        self, tmp_path: Path
    ) -> None:
        """Pinned against a real ``RunSeed``, not against a list of strings.

        A rename on the writing side that left these constants behind would make a
        quarantined run read as unmarked and a delivery run read as unconfigured.
        Building the seed is what makes this an observation.
        """
        seed = RunSeed(
            run_id=RUN,
            ref=SpecRef.of(tmp_path, "example"),
            working_tree=tmp_path,
            project=str(tmp_path),
            base_branch="main",
            spec_type="feature",
            source=SOURCE,
            item=WatchedItem(source=SOURCE, identifier="item-7", title="t", body="b"),
            generation=1,
            submitter_class=SubmitterClass(name="member", evidence=ClassEvidence.ASSOCIATION),
            autonomy=AutonomyPolicy.from_document({}).resolve(
                source=SOURCE, spec_type="feature", submitter_class="member"
            ),
        )

        written = seed.detail()

        assert DETAIL_AUTONOMY in written
        assert DETAIL_SPEC_TYPE in written
        assert DETAIL_SUBMITTER_CLASS in written

    def test_the_quarantine_key_is_the_one_the_screener_writes(self) -> None:
        # The screener imports this constant rather than spelling the key again,
        # so the assertion is that it did not go back to a literal.
        source = (
            Path(resume_module.__file__)
            .parent.joinpath("watch", "screening.py")
            .read_text(encoding="utf-8")
        )
        assert f'"{DETAIL_SCREENING_QUARANTINED}"' not in source
        assert "DETAIL_SCREENING_QUARANTINED" in source


class TestAQuarantineSurvivesTheResume:
    def test_a_quarantined_run_is_capped_to_authoring_at_an_integration_policy(
        self, store: StateStore, ref: SpecRef
    ) -> None:
        # "Regardless of policy", stated at the rung where the policy permits
        # everything: the row says the item's text was never cleared.
        authority = authority_for(admit(store, ref, level="integration", quarantined=True))

        assert authority.level is AutonomyLevel.AUTHORING
        assert authority.quarantined is True
        assert authority.decision.execution_is_human_reserved

    def test_the_cap_holds_even_when_only_the_mark_survived(
        self, store: StateStore, ref: SpecRef
    ) -> None:
        # The screener writes the rung and the mark together. If only the mark
        # landed -- an interrupted write, a later writer that reset the column --
        # the run must still be held. The cap must not rest on one field.
        record = admit(store, ref, level="integration", mirrored="integration")
        held = store.update_run(RUN, detail={DETAIL_SCREENING_QUARANTINED: True})
        assert record.posture == "integration"

        authority = authority_for(held)

        assert authority.level is AutonomyLevel.AUTHORING
        assert authority.decision.declared_at == f"runs.{RUN}.screening-quarantine"

    def test_the_gate_refuses_a_quarantined_run_the_policy_would_authorize(
        self, store: StateStore, ref: SpecRef, log: AuditLog
    ) -> None:
        settle(store, ref)
        admit(store, ref, level="integration", quarantined=True)

        outcome = request_execution_for_run(store, ref, run=RUN, audit=log)

        assert not outcome.ok
        assert [reason.code for reason in outcome.reasons] == [REASON_HUMAN_REQUIRED]
        assert outcome.human_reserved
        events = [entry.event for entry in log.read(ref)]
        assert EXECUTION_REFUSED_EVENT in events
        assert EXECUTION_STARTED_EVENT not in events

    def test_the_same_run_without_the_mark_is_allowed_to_execute(
        self, store: StateStore, ref: SpecRef, log: AuditLog
    ) -> None:
        # The counterfactual, in the same shape: this is what proves the refusal
        # above came from the quarantine rather than from an unready spec.
        settle(store, ref)
        admit(store, ref, level="integration")

        outcome = request_execution_for_run(store, ref, run=RUN, audit=log)

        assert outcome.ok, [str(reason) for reason in outcome.reasons]
        assert not outcome.human_reserved
        assert policy_declaration(outcome.initiator) == f"runs.{RUN}.posture"

    def test_a_person_may_still_release_a_quarantined_run(
        self, store: StateStore, ref: SpecRef, log: AuditLog
    ) -> None:
        # The release requirement 25.5 treats as a human review action. The cap
        # holds against the policy, never against the person looking at it.
        settle(store, ref)
        admit(store, ref, level="integration", quarantined=True)

        outcome = request_execution_for_run(store, ref, run=RUN, audit=log, user=USER)

        assert outcome.ok, [str(reason) for reason in outcome.reasons]
        assert outcome.initiator == USER

    def test_an_unknown_run_is_refused_rather_than_gated_as_unconfigured(
        self, store: StateStore, ref: SpecRef, log: AuditLog
    ) -> None:
        # An absent row must not resolve to "authoring, nothing configured": that
        # reads as a held run rather than as a caller naming a run that does not
        # exist.
        with pytest.raises(UnknownRun):
            request_execution_for_run(store, ref, run="run-missing", audit=log)


class TestTheGateCannotReResolveThePolicy:
    def test_the_run_scoped_gate_accepts_no_decision_level_or_policy(self) -> None:
        """The authority has no parameter to arrive through.

        A gate that still accepted one would be a guarantee at one spelling with
        an equivalent second beside it: the next caller passes a freshly resolved
        decision and the persisted posture stops mattering.
        """
        for callable_under_test in (
            request_execution_for_run,
            resume_module.authority_for,
        ):
            names = set(inspect.signature(callable_under_test).parameters)
            assert not names & {"decision", "level", "autonomy", "policy", "authority"}

    def test_the_resume_module_does_not_import_the_policy_resolver(self) -> None:
        """Structural, because "nothing calls it" is not a property of the code.

        The module cannot re-resolve configuration if it never imports the class
        that reads it. Asserting on the import graph is what makes that true of the
        file rather than of today's call sites.
        """
        tree = ast.parse(Path(resume_module.__file__).read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)

        assert "AutonomyPolicy" not in imported
        assert not hasattr(resume_module, "AutonomyPolicy")

    def test_a_widened_configuration_does_not_reach_a_run_already_admitted(
        self, store: StateStore, ref: SpecRef, log: AuditLog, tmp_path: Path
    ) -> None:
        """Configuration is live; the run's authority is not.

        The run is admitted at authoring and the source's grid is then declared
        ``integration`` -- which is what an operator widening a source, or an
        upgrade shipping a wider default, looks like from the run's side. A gate
        that re-resolved would now execute a run admitted as human-reserved.
        """
        settle(store, ref)
        admit(store, ref, level="authoring")
        config = ConfigStore(root=tmp_path / "config")
        configure(config, "integration")
        assert (
            AutonomyPolicy.from_store(config)
            .resolve(source=SOURCE, spec_type="feature", submitter_class="member")
            .permits(AutonomyLevel.EXECUTION)
        ), "the widened configuration must genuinely authorize execution"

        outcome = request_execution_for_run(store, ref, run=RUN, audit=log)

        assert not outcome.ok
        assert [reason.code for reason in outcome.reasons] == [REASON_HUMAN_REQUIRED]


class TestTheAuthorityRendersForASurface:
    def test_the_json_object_carries_the_rung_the_mark_and_the_disagreement(
        self, store: StateStore, ref: SpecRef
    ) -> None:
        authority = authority_for(admit(store, ref, level="integration", quarantined=True))

        rendered = authority.to_json_object()

        assert json.loads(json.dumps(rendered)) == rendered
        assert rendered["level"] == "authoring"
        assert rendered["recorded_posture"] == "integration"
        assert rendered["quarantined"] is True
        assert rendered["run"] == RUN

    def test_the_authority_is_a_value_a_surface_cannot_edit(
        self, store: StateStore, ref: SpecRef
    ) -> None:
        authority = authority_for(admit(store, ref, level="execution"))

        with pytest.raises(Exception):
            authority.decision = None  # type: ignore[assignment,misc]
        assert isinstance(authority, ResumeAuthority)


class TestTheRowTheScreenerWroteIsTheRowTheGateReads:
    def test_a_real_quarantine_write_reconstructs_as_an_authoring_authority(
        self, tmp_path: Path
    ) -> None:
        """Writer and reader, end to end, with nothing standing in for either.

        The dispatch runs with an integration grid and a provider that suspects
        the item, so the row is written by the screener itself rather than by this
        test's idea of what the screener writes. A rename on either side of the
        detail keys, or a reader that consulted configuration, lands here.
        """
        tree = _tree(tmp_path)
        state = StateStore(root=tmp_path / "screened")
        config = _write_config(tmp_path, tree)

        starter = _Starter()
        dispatch_source(
            state,
            config,
            _polled(_item(body=INJECTION_BODY)),
            gate=_AllowAll(),
            start=starter,
            screener=_screener(config, state, SuspectProvider()),
        )

        seed = starter.seeds[0]
        record = state.get_run(seed.run_id)
        assert record is not None
        authority = authority_for(record)

        assert authority.quarantined is True
        assert authority.level is AutonomyLevel.AUTHORING
        assert authority.decision.execution_is_human_reserved
        # And the grid really did say integration, so the cap is the row's doing.
        assert (
            AutonomyPolicy.from_store(config)
            .resolve(
                source=SCREENED_SOURCE, spec_type="bugfix", submitter_class=LEAST_TRUSTED_CLASS
            )
            .level
            is AutonomyLevel.INTEGRATION
        )


#: Rungs the ladder knows, plus the shapes a hand-edited or older row arrives as.
_STORED = st.sampled_from([level.value for level in AutonomyLevel] + ["", "root", "INTEGRATION"])


@settings(max_examples=80, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(_STORED, _STORED, st.booleans())
def test_a_reconstructed_authority_never_exceeds_what_the_row_recorded(
    column: str, mirrored: str, quarantined: bool
) -> None:
    """The property the whole module exists for, over every row shape.

    Whatever the two persisted fields say and whichever of them says it, the
    reconstruction lands on the LOWER recognised rung, resolves to human-reserved
    when neither field named a rung the ladder knows, and authorizes nothing past
    authoring while the quarantine mark is on the row. A reconstruction that took
    the maximum, that read one field only, or that let a quarantined row keep its
    configured rung fails this rather than merely failing a scripted case.
    """
    detail: dict[str, Any] = {DETAIL_AUTONOMY: mirrored}
    if quarantined:
        detail[DETAIL_SCREENING_QUARANTINED] = True
    record = RunRecord(
        run_id=RUN,
        spec_key="p/example",
        source=SOURCE,
        item_id="item-7",
        state=RunState.QUEUED.value,
        posture=column,
        cost_credits=0.0,
        created_ts="2026-03-01T12:00:00+00:00",
        updated_ts="2026-03-01T12:00:00+00:00",
        detail=detail,
    )

    authority = authority_for(record)

    recognised = [
        AutonomyLevel(raw)
        for raw in (column, mirrored)
        if raw in {lv.value for lv in AutonomyLevel}
    ]
    if quarantined:
        assert authority.level is AutonomyLevel.AUTHORING
        assert not authority.decision.permits(AutonomyLevel.EXECUTION)
        assert authority.quarantined
        return
    if not recognised:
        assert authority.level is AutonomyLevel.AUTHORING
        assert not authority.decision.is_configured
        assert authority.decision.execution_is_human_reserved
        return
    assert authority.level.rank == min(rung.rank for rung in recognised)
    assert authority.decision.is_configured
