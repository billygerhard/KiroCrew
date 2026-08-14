"""The durable home for an analysis report, and its Review_Queue projection.

Before this, a routed report was "recorded" into the default in-memory sink and
dropped when the process ended. Three claims are under test here:

* the stored columns are exactly the ones the engine emitted, so a surface reads
  the report the engine wrote rather than a store-shaped translation of it;
* a re-analysis SUPERSEDES its predecessor rather than appending beside it, which
  is the one behaviour a single-analysis test cannot distinguish; and
* the rows reach a reviewer on the run's existing queue entry, grouped by
  criterion, with an unkeyed finding kept as its own group rather than dropped.

The display contract is checked against text a provider could actually send: the
finding body was rendered before it was stored, and it must still be safe after a
round trip through SQLite -- prose keeping the line breaks prose is entitled to,
identifier-shaped fields keeping none.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from kiro_crew.apps.builtins.spec_engine.engine.analysis import (
    AnalysisEngine,
    RecordingFindingsSink,
    StateFindingsSink,
)
from kiro_crew.apps.builtins.spec_engine.engine.audit import AuditLog
from kiro_crew.apps.builtins.spec_engine.engine.capabilities import (
    TRANSPORT_COMMAND,
    CapabilityRegistry,
)
from kiro_crew.apps.builtins.spec_engine.engine.config import DASHBOARD_SURFACE, ConfigStore
from kiro_crew.apps.builtins.spec_engine.engine.review_queue import ReviewQueue
from kiro_crew.apps.builtins.spec_engine.engine.runs import RunMachine, RunState
from kiro_crew.apps.builtins.spec_engine.engine.state import SpecRef, StateStore

from .test_analysis_wiring import StubTransport, author_spec
from .test_capability_schemas import response_payload


@pytest.fixture()
def analysis_ref(tmp_path: Path) -> SpecRef:
    project = tmp_path / "project"
    author_spec(project / ".kiro" / "specs" / "example")
    return SpecRef.of(project, "example")


@pytest.fixture()
def config(tmp_path: Path) -> ConfigStore:
    store = ConfigStore(root=tmp_path / "config")
    store.write(
        {"capabilities": {"analysis": {"transport": TRANSPORT_COMMAND, "command": ["analyzer"]}}},
        surface=DASHBOARD_SURFACE,
    )
    return store


def analysis_engine(
    config: ConfigStore, transport: StubTransport, sink: Any = None
) -> AnalysisEngine:
    registry = CapabilityRegistry(config, transports={transport.transport: transport})
    return AnalysisEngine(registry, findings_sink=sink) if sink else AnalysisEngine(registry)


def transport_with(*findings: dict[str, Any]) -> StubTransport:
    return StubTransport(payload=response_payload("analysis", findings=list(findings)))


def finding(
    message: str = "1.1 is ambiguous",
    *,
    kind: str = "ambiguity",
    severity: str = "warning",
    refs: tuple[str, ...] = ("1.1",),
) -> dict[str, Any]:
    return {"kind": kind, "severity": severity, "message": message, "refs": list(refs)}


class TestTheStoredRowIsTheEmittedRow:
    def test_the_columns_are_exactly_the_review_row_keys(
        self, store: StateStore, config: ConfigStore, analysis_ref: SpecRef
    ) -> None:
        engine = analysis_engine(config, transport_with(finding()), StateFindingsSink(store))
        report = engine.analyze(analysis_ref, run="run-1")

        stored = store.list_analysis_findings(run="run-1")

        # Compared against the report's own rows rather than against a hand-written
        # expectation: a fixture authored to match the writer would agree with a
        # writer that dropped a column, and the point of this row shape is that a
        # surface can read back what the engine emitted.
        assert [record.to_review_row() for record in stored] == list(report.review_rows("run-1"))

    def test_an_unkeyed_finding_is_stored_with_a_null_criterion(
        self, store: StateStore, config: ConfigStore, analysis_ref: SpecRef
    ) -> None:
        # 9.9 is not a criterion the authored requirements declare, so the engine
        # routes it unkeyed. The column has to accept NULL for it to be stored at
        # all -- a NOT NULL criterion would force either a dropped finding or a
        # forged identifier.
        engine = analysis_engine(
            config, transport_with(finding("9.9 broken", refs=("9.9",))), StateFindingsSink(store)
        )
        engine.analyze(analysis_ref, run="run-1")

        stored = store.list_analysis_findings(run="run-1")

        assert len(stored) == 1
        assert stored[0].criterion is None
        assert stored[0].keyed is False

    def test_the_stored_prose_keeps_its_breaks_and_loses_its_overwrites(
        self, store: StateStore, config: ConfigStore, analysis_ref: SpecRef
    ) -> None:
        # The display contract has to survive the round trip through SQLite. A
        # carriage return in the message would return the cursor and overwrite the
        # engine-authored line above it; the identifier-shaped kind keeps no break
        # at all, while prose keeps the newline it is entitled to.
        engine = analysis_engine(
            config,
            transport_with(
                finding("line one\rline two\nline three\x07", kind="ambiguity\rforged")
            ),
            StateFindingsSink(store),
        )
        engine.analyze(analysis_ref, run="run-1")

        body = store.list_analysis_findings(run="run-1")[0].finding

        assert "\r" not in body["kind"]
        assert "\r" not in body["message"]
        assert "\x07" not in body["message"]
        assert "\n" in body["message"]


class TestAReanalysisSupersedes:
    def test_re_analysing_replaces_the_runs_rows_rather_than_appending(
        self, store: StateStore, config: ConfigStore, analysis_ref: SpecRef
    ) -> None:
        sink = StateFindingsSink(store)
        first = analysis_engine(config, transport_with(finding("first pass")), sink)
        first.analyze(analysis_ref, run="run-1")
        second = analysis_engine(config, transport_with(finding("second pass")), sink)
        second.analyze(analysis_ref, run="run-1")

        stored = store.list_analysis_findings(run="run-1")

        # An appending writer leaves two rows and passes every single-analysis
        # assertion there is; the count and the surviving message together are what
        # separate replace from append.
        assert len(stored) == 1
        assert stored[0].finding["message"] == "second pass"

    def test_an_analysis_that_finds_nothing_clears_the_previous_findings(
        self, store: StateStore, config: ConfigStore, analysis_ref: SpecRef
    ) -> None:
        sink = StateFindingsSink(store)
        analysis_engine(config, transport_with(finding()), sink).analyze(analysis_ref, run="run-1")
        analysis_engine(config, transport_with(), sink).analyze(analysis_ref, run="run-1")

        # Leaving the old rows standing would report a superseded verdict as the
        # current one, which is worse than reporting nothing.
        assert store.list_analysis_findings(run="run-1") == []

    def test_replacing_one_runs_rows_leaves_another_runs_alone(
        self, store: StateStore, config: ConfigStore, analysis_ref: SpecRef
    ) -> None:
        sink = StateFindingsSink(store)
        analysis_engine(config, transport_with(finding("for run one")), sink).analyze(
            analysis_ref, run="run-1"
        )
        analysis_engine(config, transport_with(finding("for run two")), sink).analyze(
            analysis_ref, run="run-2"
        )
        analysis_engine(config, transport_with(finding("run one again")), sink).analyze(
            analysis_ref, run="run-1"
        )

        # A DELETE that forgot its run predicate would wipe run-2 here, and every
        # test that looks at only one run would still pass.
        assert [r.finding["message"] for r in store.list_analysis_findings(run="run-2")] == [
            "for run two"
        ]
        assert [r.finding["message"] for r in store.list_analysis_findings(run="run-1")] == [
            "run one again"
        ]


class TestTheQueueEntryCarriesTheFindings:
    @pytest.fixture()
    def queue(self, store: StateStore, tmp_path: Path) -> ReviewQueue:
        machine = RunMachine(
            store,
            ConfigStore(root=tmp_path / "runconfig"),
            audit=AuditLog(root=tmp_path / "audit"),
        )
        return ReviewQueue(machine)

    def park_for_review(self, queue: ReviewQueue, ref: SpecRef, run_id: str) -> None:
        machine = queue._machine
        machine.create(ref, run_id=run_id, source="dashboard")
        machine.transition(ref, run_id, RunState.AUTHORING)
        machine.transition(ref, run_id, RunState.AWAITING_REVIEW)

    def test_the_findings_reach_the_runs_existing_queue_entry_grouped_by_criterion(
        self,
        store: StateStore,
        config: ConfigStore,
        analysis_ref: SpecRef,
        queue: ReviewQueue,
    ) -> None:
        self.park_for_review(queue, analysis_ref, "run-1")
        analysis_engine(
            config,
            transport_with(
                finding("first about 1.1"),
                finding("second about 1.1"),
                finding("about 9.9", refs=("9.9",)),
            ),
            StateFindingsSink(store),
        ).analyze(analysis_ref, run="run-1")

        entries = queue.snapshot().entries

        assert len(entries) == 1
        groups = {group.criterion: group for group in entries[0].analysis}
        # Two findings about one criterion arrive as one group, not two: a grouping
        # keyed per row would give a reviewer the criterion heading twice.
        assert [f["message"] for f in groups["1.1"].findings] == [
            "first about 1.1",
            "second about 1.1",
        ]
        # The unkeyed finding is a group of its own rather than a second list a
        # surface has to remember to render.
        assert [f["message"] for f in groups[None].findings] == ["about 9.9"]

    def test_the_unkeyed_group_comes_last(
        self,
        store: StateStore,
        config: ConfigStore,
        analysis_ref: SpecRef,
        queue: ReviewQueue,
    ) -> None:
        self.park_for_review(queue, analysis_ref, "run-1")
        analysis_engine(
            config,
            transport_with(finding("about 9.9", refs=("9.9",)), finding("about 1.1")),
            StateFindingsSink(store),
        ).analyze(analysis_ref, run="run-1")

        criteria = [group.criterion for group in queue.snapshot().entries[0].analysis]

        # A reviewer works down the document first and then reads what could not be
        # placed in it, whatever order the provider reported them in.
        assert criteria == ["1.1", None]

    def test_a_run_with_no_recorded_analysis_carries_no_groups(
        self, analysis_ref: SpecRef, queue: ReviewQueue
    ) -> None:
        self.park_for_review(queue, analysis_ref, "run-1")

        assert queue.snapshot().entries[0].analysis == ()

    def test_the_entry_json_carries_the_groups_for_a_rendering_driver(
        self,
        store: StateStore,
        config: ConfigStore,
        analysis_ref: SpecRef,
        queue: ReviewQueue,
    ) -> None:
        self.park_for_review(queue, analysis_ref, "run-1")
        analysis_engine(config, transport_with(finding()), StateFindingsSink(store)).analyze(
            analysis_ref, run="run-1"
        )

        rendered = queue.snapshot().to_json_object()

        groups = rendered["entries"][0]["analysis"]
        assert groups[0]["criterion"] == "1.1"
        assert groups[0]["keyed"] is True
        assert groups[0]["findings"][0]["message"] == "1.1 is ambiguous"

    def test_the_projection_is_re_read_so_a_re_analysis_shows_through(
        self,
        store: StateStore,
        config: ConfigStore,
        analysis_ref: SpecRef,
        queue: ReviewQueue,
    ) -> None:
        self.park_for_review(queue, analysis_ref, "run-1")
        sink = StateFindingsSink(store)
        analysis_engine(config, transport_with(finding("first verdict")), sink).analyze(
            analysis_ref, run="run-1"
        )
        before = queue.snapshot().entries[0].analysis
        analysis_engine(config, transport_with(finding("second verdict")), sink).analyze(
            analysis_ref, run="run-1"
        )

        after = queue.snapshot().entries[0].analysis

        # The queue is derived on every call; a cached copy would be the one part
        # of an entry that could keep reporting a superseded analysis.
        assert [f["message"] for f in before[0].findings] == ["first verdict"]
        assert [f["message"] for f in after[0].findings] == ["second verdict"]


class TestTheSinkIsTheOneWiredIn:
    def test_the_durable_sink_is_not_the_in_memory_default(
        self, store: StateStore, config: ConfigStore, analysis_ref: SpecRef
    ) -> None:
        # The graph's wiring check refuses a RecordingFindingsSink by type, so the
        # durable sink must not be one -- and the report must land in the store
        # rather than on a list that dies with the process.
        sink = StateFindingsSink(store)
        assert not isinstance(sink, RecordingFindingsSink)

        analysis_engine(config, transport_with(finding()), sink).analyze(
            analysis_ref, run="run-1"
        )

        assert len(store.list_analysis_findings(run="run-1")) == 1

    def test_findings_survive_a_fresh_store_over_the_same_database(
        self, store: StateStore, config: ConfigStore, analysis_ref: SpecRef
    ) -> None:
        analysis_engine(config, transport_with(finding()), StateFindingsSink(store)).analyze(
            analysis_ref, run="run-1"
        )

        # The durability claim, stated as the thing the in-memory sink cannot do: a
        # second store opened over the same database reads the rows back.
        reopened = StateStore(root=store.root)
        assert len(reopened.list_analysis_findings(run="run-1")) == 1
