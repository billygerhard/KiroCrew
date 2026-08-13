"""The semantic tier and the async analysis job shape.

Semantic analysis is a dispatched agent turn, not a prompt folded into a tool
result: the turn runs in a host session stamped to the run, so its spend counts
against the run's ceiling and the kill switch. Its output is untrusted model
text, so it is schema-validated before a finding is recorded and its depth is
recorded by the engine rather than trusted from the output. And every analysis
transport shares one asynchronous job shape — submit returns an identifier, poll
returns status, progress, and findings — under a total wall-clock deadline, so a
job never holds a call open indefinitely.

The turn provider is substituted here: what matters at this layer is what the
engine does with a dispatched turn's output, its spend, and its deadline, not how
a host actually spawns the turn (which has no production caller until the wiring
task constructs one).
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import Future
from pathlib import Path
from typing import Any, Callable

import pytest

from kiro_crew.apps.builtins.spec_engine.engine.analysis import (
    ANALYSIS_JOB_DEADLINE_SETTING,
    AUDIT_EVENT_SEMANTIC,
    DEPTH_SEMANTIC,
    SEMANTIC_PROVIDER,
    AnalysisEngine,
    AnalysisJobs,
    JobStatus,
    RecordingFindingsSink,
    SemanticAnalysisInvalid,
    SemanticAnalysisUnavailable,
    SemanticAnalyzer,
    SemanticTurnRequest,
    SemanticTurnResponse,
    UnknownJob,
)
from kiro_crew.apps.builtins.spec_engine.engine.audit import AuditLog
from kiro_crew.apps.builtins.spec_engine.engine.budget.ledger import RunAccounting
from kiro_crew.apps.builtins.spec_engine.engine.capabilities import (
    CapabilityRegistry,
    ProviderNature,
)
from kiro_crew.apps.builtins.spec_engine.engine.config import DASHBOARD_SURFACE, ConfigStore
from kiro_crew.apps.builtins.spec_engine.engine.local_analyzer import DEPTH_STRUCTURAL
from kiro_crew.apps.builtins.spec_engine.engine.local_analyzer import PROVIDER_NAME as ANALYZER_NAME
from kiro_crew.apps.builtins.spec_engine.engine.state import SpecRef, StateStore

from .test_analysis_wiring import author_spec
from .test_capability_schemas import response_payload

# --- fixtures and doubles --------------------------------------------------


@pytest.fixture()
def ref(tmp_path: Path) -> SpecRef:
    project = tmp_path / "project"
    author_spec(project / ".kiro" / "specs" / "example")
    return SpecRef.of(project, "example")


@pytest.fixture()
def config_store(tmp_path: Path) -> ConfigStore:
    return ConfigStore(tmp_path / "config")


@pytest.fixture()
def state_store(tmp_path: Path) -> StateStore:
    return StateStore(root=tmp_path / "state")


def analysis_engine(config_store: ConfigStore, **kwargs: Any) -> AnalysisEngine:
    """An engine over a registry with nothing bound, so its fallback is structural."""
    return AnalysisEngine(CapabilityRegistry(config_store), **kwargs)


class StubTurnProvider:
    """A semantic turn provider that answers with a payload, or raises, once.

    Optional *entered* and *release* events let a test hold the turn in flight
    while it advances an injected clock, which is how the wall-clock deadline is
    exercised without real sleeping.
    """

    def __init__(
        self,
        *,
        payload: Any = None,
        failure: Exception | None = None,
        session_key: str = "sess-1",
        entered: threading.Event | None = None,
        release: threading.Event | None = None,
    ) -> None:
        self._payload = payload
        self._failure = failure
        self._session_key = session_key
        self._entered = entered
        self._release = release
        self.requests: list[SemanticTurnRequest] = []

    def analyze(self, request: SemanticTurnRequest) -> SemanticTurnResponse:
        self.requests.append(request)
        if self._entered is not None:
            self._entered.set()
        if self._release is not None:
            assert self._release.wait(timeout=5), "the release event was never set"
        if self._failure is not None:
            raise self._failure
        return SemanticTurnResponse(payload=self._payload, session_key=self._session_key)


class InlineExecutor:
    """Runs submitted work synchronously, so submit-then-poll is deterministic.

    The job manager's contract is submit/poll; running the work inline removes the
    thread race from every test that does not exercise a job still in flight. The
    one test that needs an in-flight job injects a real thread pool instead.
    """

    def submit(
        self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any
    ) -> "Future[Any]":
        future: "Future[Any]" = Future()
        try:
            future.set_result(fn(*args, **kwargs))
        except BaseException as exc:  # noqa: BLE001 - mirror an executor's capture
            future.set_exception(exc)
        return future

    def shutdown(self, wait: bool = True) -> None:
        return None


class Clock:
    """A hand-advanced monotonic clock, for the wall-clock deadline test."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def semantic_analyzer(
    config_store: ConfigStore,
    state_store: StateStore,
    provider: StubTurnProvider,
    *,
    audit: AuditLog | None = None,
) -> SemanticAnalyzer:
    return SemanticAnalyzer(
        config_store,
        provider=provider,
        accounting=RunAccounting(state_store),
        audit=audit,
    )


def valid_semantic_payload(**overrides: Any) -> dict[str, Any]:
    """A schema-valid analysis response a turn might return, at semantic depth."""
    return response_payload("analysis", result={"depth": DEPTH_SEMANTIC}, **overrides)


# --- the model-backed builtin dispatches a turn ----------------------------


class TestSemanticTurnIsModelBacked:
    def test_a_dispatched_turn_produces_a_semantic_keyed_report(
        self, ref: SpecRef, config_store: ConfigStore, state_store: StateStore
    ) -> None:
        provider = StubTurnProvider(
            payload=valid_semantic_payload(
                findings=[
                    {"kind": "meaning", "severity": "warning", "message": "1.1 is ambiguous",
                     "refs": ["1.1"]},
                ]
            )
        )
        report = semantic_analyzer(config_store, state_store, provider).run(ref, run="run-1")
        # The report is keyed to the criterion the finding names, exactly as the
        # registry path's report is: one findings shape across depths.
        assert set(report.by_criterion) == {"1.1"}
        # The provider identity says model-backed, which is how a surface tells an
        # operator this capability spends credits.
        assert report.provider.name == SEMANTIC_PROVIDER
        assert report.provider.nature is ProviderNature.MODEL_BACKED

    def test_the_turn_carries_the_authored_prompt_and_the_documents_as_data(
        self, ref: SpecRef, config_store: ConfigStore, state_store: StateStore
    ) -> None:
        provider = StubTurnProvider(payload=valid_semantic_payload())
        semantic_analyzer(config_store, state_store, provider).run(ref, run="run-1")
        request = provider.requests[0]
        assert request.guidance  # the engine-authored analysis prompt
        kinds = {kind for kind, _ in request.documents}
        assert {"requirements", "tasks"}.issubset(kinds)

    def test_the_engine_records_semantic_depth_even_if_the_turn_claims_more(
        self, ref: SpecRef, config_store: ConfigStore, state_store: StateStore
    ) -> None:
        # A turn that declares a deeper depth than it performed must not have that
        # claim recorded: the engine dispatched a semantic turn, so semantic is
        # what is recorded. This is requirement 35.5 — absence of findings at one
        # depth is never correctness at a greater one.
        provider = StubTurnProvider(
            payload=response_payload("analysis", result={"depth": "extended"})
        )
        report = semantic_analyzer(config_store, state_store, provider).run(ref, run="run-1")
        assert report.result.response.result["depth"] == DEPTH_SEMANTIC


# --- spend is attributed to the run ----------------------------------------


class TestSpendIsAttributed:
    def test_the_turn_session_is_stamped_to_the_run(
        self, ref: SpecRef, config_store: ConfigStore, state_store: StateStore
    ) -> None:
        provider = StubTurnProvider(payload=valid_semantic_payload(), session_key="sess-9")
        semantic_analyzer(config_store, state_store, provider).run(ref, run="run-1")
        # Stamping is what makes the turn's spend count against the run's ceiling
        # and the kill switch. Without it the session's metering escapes the run.
        assert RunAccounting(state_store).sessions_for("run-1") == ("sess-9",)

    def test_a_ran_but_invalid_turn_still_has_its_spend_stamped(
        self, ref: SpecRef, config_store: ConfigStore, state_store: StateStore
    ) -> None:
        # The turn ran and spent before answering unusably. The job will fail, but
        # the spend is real, so the session is stamped before the failure is
        # raised — a total that omitted it would authorise turns already paid for.
        provider = StubTurnProvider(
            payload={"schema_version": 1, "capability": "analysis"}, session_key="sess-2"
        )
        with pytest.raises(SemanticAnalysisInvalid):
            semantic_analyzer(config_store, state_store, provider).run(ref, run="run-1")
        assert RunAccounting(state_store).sessions_for("run-1") == ("sess-2",)


# --- the schema gate on untrusted turn output ------------------------------


class TestTurnOutputIsValidated:
    def test_a_schema_invalid_output_raises_and_records_nothing(
        self, ref: SpecRef, config_store: ConfigStore, state_store: StateStore
    ) -> None:
        provider = StubTurnProvider(payload={"schema_version": 1, "capability": "analysis"})
        with pytest.raises(SemanticAnalysisInvalid):
            semantic_analyzer(config_store, state_store, provider).run(ref, run="run-1")

    def test_a_finding_naming_no_real_criterion_is_unkeyed_not_forged(
        self, ref: SpecRef, config_store: ConfigStore, state_store: StateStore
    ) -> None:
        # The document declares 1.1 and 1.2. A finding naming 9.9 must not create a
        # 9.9 key: turn output is attacker-influenceable and cannot conjure a
        # criterion the document does not declare.
        provider = StubTurnProvider(
            payload=valid_semantic_payload(
                findings=[
                    {"kind": "invention", "severity": "error", "message": "9.9 broken",
                     "refs": ["9.9"]},
                ]
            )
        )
        report = semantic_analyzer(config_store, state_store, provider).run(ref, run="run-1")
        assert report.by_criterion == {}
        assert len(report.unkeyed) == 1

    def test_declared_coverage_the_turn_claims_is_surfaced_at_semantic_depth(
        self, ref: SpecRef, config_store: ConfigStore, state_store: StateStore
    ) -> None:
        # The engine cannot verify a semantic pass's coverage claims, so it
        # surfaces what the turn declared rather than inventing its own — but it
        # records the depth authoritatively, so a reader sees "semantic pass,
        # these documents skipped" rather than mistaking the depth.
        provider = StubTurnProvider(
            payload=valid_semantic_payload(
                coverage={"processed": ["requirements"],
                          "skipped": [{"item": "design", "reason": "not read this pass"}]}
            )
        )
        report = semantic_analyzer(config_store, state_store, provider).run(ref, run="run-1")
        assert report.result.response.result["depth"] == DEPTH_SEMANTIC
        assert {item.item for item in report.skipped} == {"design"}

    def test_the_turn_is_audited_with_its_depth_and_provider(
        self, ref: SpecRef, config_store: ConfigStore, state_store: StateStore, tmp_path: Path
    ) -> None:
        audit = AuditLog(tmp_path / "audit")
        provider = StubTurnProvider(payload=valid_semantic_payload())
        semantic_analyzer(config_store, state_store, provider, audit=audit).run(ref, run="run-1")
        events = [e for e in audit.read(ref) if e.event == AUDIT_EVENT_SEMANTIC]
        assert len(events) == 1
        detail = events[0].detail or {}
        assert detail["depth"] == DEPTH_SEMANTIC
        assert detail["provider"]["nature"] == ProviderNature.MODEL_BACKED.value


# --- the async job shape ---------------------------------------------------


class TestJobShape:
    def test_submit_returns_an_id_and_poll_returns_findings_on_completion(
        self, ref: SpecRef, config_store: ConfigStore, state_store: StateStore
    ) -> None:
        provider = StubTurnProvider(
            payload=valid_semantic_payload(
                findings=[{"kind": "m", "severity": "info", "message": "on 1.1", "refs": ["1.1"]}]
            )
        )
        engine = analysis_engine(config_store)
        jobs = AnalysisJobs(
            engine,
            config_store,
            semantic=semantic_analyzer(config_store, state_store, provider),
            executor=InlineExecutor(),
        )
        job_id = jobs.submit(ref, run="run-1", semantic=True)
        view = jobs.poll(job_id)
        assert view.status is JobStatus.DONE
        assert view.depth == DEPTH_SEMANTIC
        assert view.provider == SEMANTIC_PROVIDER
        assert view.report is not None
        assert set(view.report.by_criterion) == {"1.1"}

    def test_a_semantic_job_records_its_report_to_the_engine_sink(
        self, ref: SpecRef, config_store: ConfigStore, state_store: StateStore
    ) -> None:
        sink = RecordingFindingsSink()
        engine = analysis_engine(config_store, findings_sink=sink)
        provider = StubTurnProvider(
            payload=valid_semantic_payload(
                findings=[{"kind": "m", "severity": "info", "message": "on 1.1", "refs": ["1.1"]}]
            )
        )
        jobs = AnalysisJobs(
            engine,
            config_store,
            semantic=semantic_analyzer(config_store, state_store, provider),
            executor=InlineExecutor(),
        )
        jobs.poll(jobs.submit(ref, run="run-7", semantic=True))
        # Recorded through the one findings sink, the same one the structural path
        # records through — not a second persistence path.
        assert sink.rows_for("run-7")[0]["criterion"] == "1.1"

    def test_a_structural_job_runs_through_the_engine(
        self, ref: SpecRef, config_store: ConfigStore
    ) -> None:
        engine = analysis_engine(config_store)
        jobs = AnalysisJobs(engine, config_store, executor=InlineExecutor())
        view = jobs.poll(jobs.submit(ref, run="run-1"))
        assert view.status is JobStatus.DONE
        assert view.depth == DEPTH_STRUCTURAL
        assert view.provider == ANALYZER_NAME

    def test_polling_an_unknown_job_is_refused(self, config_store: ConfigStore) -> None:
        jobs = AnalysisJobs(analysis_engine(config_store), config_store, executor=InlineExecutor())
        with pytest.raises(UnknownJob):
            jobs.poll("nope")

    def test_an_unrecorded_spec_type_refuses_to_start_a_job(self, tmp_path: Path) -> None:
        project = tmp_path / "project"
        author_spec(project / ".kiro" / "specs" / "typeless", spec_type=None)
        ref = SpecRef.of(project, "typeless")
        config_store = ConfigStore(tmp_path / "config")
        jobs = AnalysisJobs(analysis_engine(config_store), config_store, executor=InlineExecutor())
        from kiro_crew.apps.builtins.spec_engine.engine.analysis import SpecTypeUnrecorded

        with pytest.raises(SpecTypeUnrecorded):
            jobs.submit(ref, run="run-1", semantic=True)


# --- schema-invalid turn fails the job -------------------------------------


class TestInvalidTurnFailsTheJob:
    def test_a_schema_invalid_turn_fails_the_job_recording_nothing(
        self, ref: SpecRef, config_store: ConfigStore, state_store: StateStore
    ) -> None:
        sink = RecordingFindingsSink()
        engine = analysis_engine(config_store, findings_sink=sink)
        provider = StubTurnProvider(payload={"schema_version": 1, "capability": "analysis"})
        jobs = AnalysisJobs(
            engine,
            config_store,
            semantic=semantic_analyzer(config_store, state_store, provider),
            executor=InlineExecutor(),
        )
        view = jobs.poll(jobs.submit(ref, run="run-1", semantic=True))
        assert view.status is JobStatus.FAILED
        assert view.failure_reason
        # Nothing partial recorded: a half-parsed set of findings is worse than
        # none, so the sink holds no row for this run.
        assert sink.rows_for("run-1") == ()


# --- an unavailable turn degrades to the structural analyzer ----------------


class TestUnavailableTurnDegrades:
    def test_an_unavailable_turn_falls_back_to_the_structural_analyzer(
        self, ref: SpecRef, config_store: ConfigStore, state_store: StateStore
    ) -> None:
        engine = analysis_engine(config_store)
        provider = StubTurnProvider(
            failure=SemanticAnalysisUnavailable("the analysis model is unavailable")
        )
        jobs = AnalysisJobs(
            engine,
            config_store,
            semantic=semantic_analyzer(config_store, state_store, provider),
            executor=InlineExecutor(),
        )
        view = jobs.poll(jobs.submit(ref, run="run-1", semantic=True))
        # Never blocks authoring: it answers from the structural analyzer, which
        # reports structural depth, rather than failing the job.
        assert view.status is JobStatus.DONE
        assert view.depth == DEPTH_STRUCTURAL
        assert view.provider == ANALYZER_NAME

    def test_an_undeclared_provider_fault_also_degrades(
        self, ref: SpecRef, config_store: ConfigStore, state_store: StateStore
    ) -> None:
        # A provider is a host seam and can fail in ways it never declared. Any
        # such fault degrades rather than escaping as a run-failing traceback.
        engine = analysis_engine(config_store)
        provider = StubTurnProvider(failure=RuntimeError("the host session crashed"))
        jobs = AnalysisJobs(
            engine,
            config_store,
            semantic=semantic_analyzer(config_store, state_store, provider),
            executor=InlineExecutor(),
        )
        view = jobs.poll(jobs.submit(ref, run="run-1", semantic=True))
        assert view.status is JobStatus.DONE
        assert view.depth == DEPTH_STRUCTURAL


# --- the wall-clock deadline -----------------------------------------------


class TestWallClockDeadline:
    def test_a_job_that_exceeds_its_deadline_times_out_with_progress(
        self, ref: SpecRef, config_store: ConfigStore, state_store: StateStore
    ) -> None:
        # A short deadline, an in-flight turn, and a hand-advanced clock: the job
        # is asked after its deadline has passed and answers timed out with the
        # time spent and the stage it had reached, rather than holding the call
        # open until the turn finishes.
        config_store.write(
            {"timeouts": {"analysis_job_s": 5}}, surface=DASHBOARD_SURFACE
        )
        assert (
            int(config_store.effective(ANALYSIS_JOB_DEADLINE_SETTING).value) == 5
        )
        entered = threading.Event()
        release = threading.Event()
        provider = StubTurnProvider(
            payload=valid_semantic_payload(), entered=entered, release=release
        )
        clock = Clock()
        jobs = AnalysisJobs(
            analysis_engine(config_store),
            config_store,
            semantic=semantic_analyzer(config_store, state_store, provider),
            clock=clock,
        )
        try:
            job_id = jobs.submit(ref, run="run-1", semantic=True)
            assert entered.wait(timeout=5), "the turn never started"
            # Before the deadline: still running.
            clock.now += 2
            assert jobs.poll(job_id).status is JobStatus.RUNNING
            # Past the deadline: terminally timed out, with elapsed and the stage.
            clock.now += 4
            timed_out = jobs.poll(job_id)
            assert timed_out.status is JobStatus.TIMED_OUT
            assert timed_out.elapsed_s == pytest.approx(6.0)
            assert timed_out.stage == "semantic_dispatch"
            assert timed_out.failure_reason
        finally:
            release.set()
            jobs.close()

    def test_a_timed_out_job_stays_timed_out_after_the_worker_finishes(
        self, ref: SpecRef, config_store: ConfigStore, state_store: StateStore
    ) -> None:
        # The deadline is authoritative: once it has passed, a worker that
        # completes afterwards cannot reopen the job as done.
        config_store.write({"timeouts": {"analysis_job_s": 1}}, surface=DASHBOARD_SURFACE)
        entered = threading.Event()
        release = threading.Event()
        provider = StubTurnProvider(
            payload=valid_semantic_payload(), entered=entered, release=release
        )
        clock = Clock()
        jobs = AnalysisJobs(
            analysis_engine(config_store),
            config_store,
            semantic=semantic_analyzer(config_store, state_store, provider),
            clock=clock,
        )
        try:
            job_id = jobs.submit(ref, run="run-1", semantic=True)
            assert entered.wait(timeout=5), "the turn never started"
            clock.now += 10
            assert jobs.poll(job_id).status is JobStatus.TIMED_OUT
            # Let the worker complete, then poll again: still timed out.
            release.set()
            deadline = time.time() + 5
            while provider.requests and not jobs._jobs[job_id].future.done():  # noqa: SLF001
                if time.time() > deadline:
                    break
                time.sleep(0.005)
            assert jobs.poll(job_id).status is JobStatus.TIMED_OUT
        finally:
            release.set()
            jobs.close()
