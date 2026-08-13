"""Test-quality is a review-verdict criterion, gated through one spelling.

A green suite that cannot fail reports the opposite of the truth, so the review
verdict judges the tests as well as the implementation. The claims here are:

* the verdict folds the test-quality assessment into ``approved`` fail-closed, so
  a sound implementation whose tests are inadequate is a changes-required verdict
  and not an approval — through the *one* value the completion gate reads, with no
  second route to completion that skips it;
* the loop records those test-quality findings in the run's audit log, and records
  nothing when the tests met the criteria;
* the criteria are a defined, referenceable thing rather than prose.

The integration claims are made through the same factory a real caller uses and
the real wave loop, so a task that reaches an approving-but-inadequate verdict
still fails, and the audit entry is the one the loop actually wrote.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from kiro_crew.apps.builtins.spec_engine.engine.audit import AuditLog
from kiro_crew.apps.builtins.spec_engine.engine.budget import (
    KillSwitch,
    MeteringLedger,
    RecordingNotifier,
    RunAccounting,
)
from kiro_crew.apps.builtins.spec_engine.engine.config import DASHBOARD_SURFACE, ConfigStore
from kiro_crew.apps.builtins.spec_engine.engine.delivery import ISOLATE_STAGE, RunContext
from kiro_crew.apps.builtins.spec_engine.engine.orchestrator import (
    TASK_REVIEWED_EVENT,
    TASK_SETTLED_EVENT,
    ExecutionOutcome,
    ReviewVerdict,
)
from kiro_crew.apps.builtins.spec_engine.engine.review_criteria import (
    DERIVED_ASSERTIONS,
    ERROR_AND_BOUNDARY_CASES,
    FAILS_ON_WRONG_BEHAVIOR,
    TEST_QUALITY_CRITERIA,
    TestQualityAssessment,
    TestQualityFinding,
    is_known_criterion,
)
from kiro_crew.apps.builtins.spec_engine.engine.roles import Dispatch
from kiro_crew.apps.builtins.spec_engine.engine.runs import TaskStatus
from kiro_crew.apps.builtins.spec_engine.engine.state import SpecRef

from .conftest import make_spec_dir
from .test_orchestrator_waves import (
    BASE,
    IMPLEMENT_MODEL,
    PROJECT,
    REVIEW_MODEL,
    RUN,
    CountingStore,
    Harness,
    RecordingReviewer,
    Worker,
    context_for,
    runner_for,
    set_retry_limit,
    write_tasks,
)


@pytest.fixture()
def harness(tmp_path: Path) -> Harness:
    """A run's engine state, built the way the wave-loop suite builds it.

    A local fixture rather than an imported one: pytest can only reuse a fixture
    across modules through a conftest, and importing the fixture symbol here would
    read to the linters as an unused import redefined by every test's parameter.
    """
    project = tmp_path / "project"
    project.mkdir()
    spec_dir = make_spec_dir(project, "example")
    write_tasks(spec_dir, [["1.1", "1.2"], ["2.1"]])
    ledger_path = tmp_path / "usage" / "tokens"
    state = CountingStore(tmp_path / "engine-state")
    config = ConfigStore(tmp_path / "config")
    config.write(
        {
            "cost_profiles": {
                "thrifty": {
                    "roles": {
                        "implement": {"model": IMPLEMENT_MODEL},
                        "review": {"model": REVIEW_MODEL},
                    }
                }
            },
            "projects": {
                PROJECT: {
                    "path": str(project),
                    "base_branch": BASE,
                    "cost_profile": "thrifty",
                    "workflow": {"stages": {ISOLATE_STAGE: [["git", "worktree", "add"]]}},
                }
            },
        },
        surface=DASHBOARD_SURFACE,
    )
    return Harness(
        project=project,
        ref=SpecRef.of(project, "example"),
        config=config,
        state=state,
        audit=AuditLog(tmp_path / "audit"),
        notifier=RecordingNotifier(),
        accounting=RunAccounting(state, ledger=MeteringLedger(ledger_path)),
        ledger_path=ledger_path,
        switch=KillSwitch(tmp_path / "switch-root"),
    )


# A finding phrased against a real criterion, reused across cases.
_UNMET = (
    TestQualityFinding(
        criterion=FAILS_ON_WRONG_BEHAVIOR.key,
        detail="the assertion holds whether or not the behavior is correct",
    ),
)


class AssessingReviewer(RecordingReviewer):
    """A reviewer whose judgement of the implementation and of the tests are set
    independently, so a test can produce the case the gate exists for: an
    implementation the reviewer would approve whose tests it would not.

    Subclasses the recording reviewer so it records ``seen`` the same way and
    satisfies the factory's parameter type, but returns a verdict carrying a
    test-quality assessment. ``findings_for`` names tasks whose tests fail the
    criteria; ``approve_impl`` is the reviewer's judgement of the implementation,
    which a satisfied assessment lets through and an unsatisfied one must not.
    """

    def __init__(
        self,
        *,
        findings_for: dict[str, tuple[TestQualityFinding, ...]] | None = None,
        approve_impl: bool = True,
    ) -> None:
        super().__init__()
        self._findings_for = dict(findings_for or {})
        self._approve = approve_impl

    def __call__(self, *, task: str, dispatch: Dispatch, context: RunContext) -> ReviewVerdict:
        # Let the parent record that this task was reviewed and on which dispatch.
        super().__call__(task=task, dispatch=dispatch, context=context)
        findings = self._findings_for.get(task, ())
        return ReviewVerdict(
            approved=self._approve,
            reason=f"{task} implementation judged",
            test_quality=TestQualityAssessment(findings=findings),
        )


class TestTheAssessmentIsSatisfiedOnlyWithoutFindings:
    def test_an_assessment_with_no_findings_is_satisfied(self) -> None:
        assert TestQualityAssessment().satisfied is True

    def test_any_finding_makes_the_assessment_unsatisfied(self) -> None:
        assert TestQualityAssessment(findings=_UNMET).satisfied is False

    def test_the_findings_serialise_for_the_audit_log(self) -> None:
        detail = TestQualityAssessment(findings=_UNMET).detail()
        assert detail == {
            "findings": [
                {
                    "criterion": FAILS_ON_WRONG_BEHAVIOR.key,
                    "detail": "the assertion holds whether or not the behavior is correct",
                }
            ]
        }


class TestTheCriteriaAreADefinedThing:
    def test_the_three_criteria_are_the_defined_set(self) -> None:
        assert TEST_QUALITY_CRITERIA == (
            DERIVED_ASSERTIONS,
            FAILS_ON_WRONG_BEHAVIOR,
            ERROR_AND_BOUNDARY_CASES,
        )

    def test_each_criterion_key_is_recognised(self) -> None:
        for criterion in TEST_QUALITY_CRITERIA:
            assert is_known_criterion(criterion.key)

    def test_an_unknown_key_is_not_a_criterion(self) -> None:
        assert is_known_criterion("not-a-criterion") is False

    def test_every_criterion_states_the_property_it_judges(self) -> None:
        # A criterion with an empty statement could not seed a review turn.
        for criterion in TEST_QUALITY_CRITERIA:
            assert criterion.statement.strip()


class TestTheVerdictFoldsTestQualityFailClosed:
    def test_an_approval_with_tests_that_meet_the_criteria_stays_approved(self) -> None:
        verdict = ReviewVerdict(approved=True, test_quality=TestQualityAssessment())
        assert verdict.approved is True

    def test_a_sound_implementation_with_inadequate_tests_is_not_approved(self) -> None:
        # The case the gate exists for: the reviewer would approve the code, but
        # the tests failed a criterion, so the verdict is changes-required.
        verdict = ReviewVerdict(approved=True, test_quality=TestQualityAssessment(findings=_UNMET))
        assert verdict.approved is False

    def test_the_coercion_supplies_a_reason_when_the_verdict_had_none(self) -> None:
        verdict = ReviewVerdict(approved=True, test_quality=TestQualityAssessment(findings=_UNMET))
        assert verdict.reason
        assert "test quality" in verdict.reason

    def test_the_reviewers_own_reason_survives_the_coercion(self) -> None:
        verdict = ReviewVerdict(
            approved=True,
            reason="tests assert on values the test constructed",
            test_quality=TestQualityAssessment(findings=_UNMET),
        )
        assert verdict.approved is False
        assert verdict.reason == "tests assert on values the test constructed"

    def test_a_non_approving_verdict_with_findings_stays_non_approving(self) -> None:
        verdict = ReviewVerdict(
            approved=False,
            reason="the implementation is wrong",
            test_quality=TestQualityAssessment(findings=_UNMET),
        )
        assert verdict.approved is False
        assert verdict.reason == "the implementation is wrong"

    @given(
        approved=st.booleans(),
        findings=st.lists(
            st.builds(
                TestQualityFinding,
                criterion=st.sampled_from([c.key for c in TEST_QUALITY_CRITERIA]),
                detail=st.text(max_size=40),
            ),
            max_size=4,
        ),
    )
    def test_approved_is_true_exactly_when_it_was_approved_and_the_tests_passed(
        self, approved: bool, findings: list[TestQualityFinding]
    ) -> None:
        # The fence property: whatever the reviewer supplied, the one value the
        # gate reads is true only when the implementation was approved AND no
        # criterion was left unmet. Nothing gets past it by any combination.
        verdict = ReviewVerdict(
            approved=approved,
            test_quality=TestQualityAssessment(findings=tuple(findings)),
        )
        assert verdict.approved is (approved and not findings)


def _test_quality_events(harness: Harness, run_id: str = RUN) -> list[dict[str, object]]:
    """The test-quality findings the loop recorded for *run_id*."""
    return [
        event.detail or {}
        for event in harness.audit.read(harness.ref)
        if event.event == TASK_REVIEWED_EVENT and event.run == run_id
    ]


class TestTheGateAndTheAuditRecordThroughTheLoop:
    def test_a_task_whose_tests_fail_the_criteria_never_completes(self, harness: Harness) -> None:
        # The worker succeeds every round and the reviewer would approve the
        # implementation; only the test-quality finding keeps the task from
        # completing, which is the whole of this gate.
        set_retry_limit(harness, 1)
        harness.start_run()
        worker = Worker()
        reviewer = AssessingReviewer(findings_for={"1.1": _UNMET})

        report = runner_for(harness, worker, reviewer=reviewer).execute(context_for(harness))

        assert report.outcome is ExecutionOutcome.FAILED
        assert report.failed_tasks == ("1.1",)
        assert harness.detail_statuses()["1.1"] is TaskStatus.FAILED
        # Implemented on every attempt, so the failure is the verdict's on the
        # tests, not the worker's: 1 retry means two rounds and two reviews.
        assert worker.dispatched.count("1.1") == 2
        assert reviewer.seen["1.1"] == 2

    def test_the_failing_criteria_are_recorded_in_the_audit_log(self, harness: Harness) -> None:
        set_retry_limit(harness, 0)
        harness.start_run()
        worker = Worker()
        reviewer = AssessingReviewer(findings_for={"1.1": _UNMET})

        runner_for(harness, worker, reviewer=reviewer).execute(context_for(harness))

        recorded = _test_quality_events(harness)
        assert any(
            event.get("task") == "1.1"
            and event.get("findings")
            == [
                {
                    "criterion": FAILS_ON_WRONG_BEHAVIOR.key,
                    "detail": "the assertion holds whether or not the behavior is correct",
                }
            ]
            for event in recorded
        )

    def test_tests_that_meet_the_criteria_complete_and_record_no_finding(
        self, harness: Harness
    ) -> None:
        set_retry_limit(harness, 0)
        harness.start_run()
        worker = Worker()
        reviewer = AssessingReviewer()  # approves, no findings

        report = runner_for(harness, worker, reviewer=reviewer).execute(context_for(harness))

        assert report.outcome is ExecutionOutcome.COMPLETED, report.reason
        # No test-quality event at all when the tests met the criteria: the audit
        # records a finding, not the absence of one.
        assert _test_quality_events(harness) == []
        # The settled events are still written, so the run is not silent.
        assert any(event.event == TASK_SETTLED_EVENT for event in harness.audit.read(harness.ref))

    def test_only_the_task_with_failing_tests_is_held_back(self, harness: Harness) -> None:
        # A finding on one leaf must not fail its independent siblings: the gate
        # is per verdict, not per wave.
        set_retry_limit(harness, 0)
        harness.start_run()
        worker = Worker()
        reviewer = AssessingReviewer(findings_for={"1.1": _UNMET})

        runner_for(harness, worker, reviewer=reviewer).execute(context_for(harness))

        statuses = harness.detail_statuses()
        assert statuses["1.1"] is TaskStatus.FAILED
        assert statuses["1.2"] is TaskStatus.COMPLETE
        recorded = _test_quality_events(harness)
        assert {event.get("task") for event in recorded} == {"1.1"}
