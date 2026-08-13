"""The dispatcher writes back ``claimed`` beside the run it starts -- on both paths.

The poll path (``dispatch_source``) and the queue-drain path (``drain_queue``)
both start runs, and this is the second consumer of that split. A ``claimed``
comment posted by only one of them would be silent for every item that waited
behind the concurrency cap, so both are proved here, through the one shared
poster and its one at-most-once writeback claim.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest

from kiro_crew.apps.builtins.spec_engine.engine.audit import AuditLog
from kiro_crew.apps.builtins.spec_engine.engine.config import DASHBOARD_SURFACE, ConfigStore
from kiro_crew.apps.builtins.spec_engine.engine.delivery.stages import (
    CommandOutcome,
    StageExecutor,
)
from kiro_crew.apps.builtins.spec_engine.engine.runs import RunState
from kiro_crew.apps.builtins.spec_engine.engine.state import StateStore
from kiro_crew.apps.builtins.spec_engine.engine.watch import (
    PollOutcome,
    PollStatus,
    RunSeed,
    WatchedItem,
    dispatch_source,
    drain_queue,
)
from kiro_crew.apps.builtins.spec_engine.engine.watch.feedback import (
    AUDIT_ITEM_FEEDBACK,
    FeedbackOutcome,
    FeedbackPoster,
)

SOURCE = "upstream-issues"
PROJECT = "acme"


class Runner:
    """Records every argv it was handed, answering success."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, argv: Sequence[str], *, cwd: Path, timeout_s: int) -> CommandOutcome:
        self.calls.append(tuple(argv))
        return CommandOutcome(exit_code=0, stdout="", stderr="", timed_out=False)


class Starter:
    """Records seeds handed to it."""

    def __init__(self) -> None:
        self.seeds: list[RunSeed] = []

    def __call__(self, seed: RunSeed) -> None:
        self.seeds.append(seed)


class AllowAll:
    def dispatch_allowed(self, source: str) -> bool:
        return True


@pytest.fixture()
def state(tmp_path: Path) -> Iterator[StateStore]:
    store = StateStore(root=tmp_path / "state")
    yield store
    store.close()


@pytest.fixture()
def audit(tmp_path: Path) -> AuditLog:
    return AuditLog(root=tmp_path / "state")


@pytest.fixture()
def tree(tmp_path: Path) -> Path:
    root = tmp_path / "acme-tree"
    (root / ".kiro" / "steering").mkdir(parents=True)
    return root


@pytest.fixture()
def config(tmp_path: Path, tree: Path) -> ConfigStore:
    store = ConfigStore(root=tmp_path / "config")
    store.write(
        {
            "projects": {PROJECT: {"path": str(tree), "base_branch": "trunk"}},
            "sources": {
                SOURCE: {
                    "enabled": True,
                    "poll": ["tracker-cli", "list"],
                    "project": PROJECT,
                    "spec_types": {"bug": "bugfix"},
                    "autonomy": {"default": {"default": "authoring"}},
                    "feedback": {"claimed": [["gh", "comment", "{item_id}"]]},
                }
            },
        },
        surface=DASHBOARD_SURFACE,
    )
    return store


def poster_over(state: StateStore, config: ConfigStore, audit: AuditLog, runner: Runner):
    return FeedbackPoster(state, config, audit, executor=StageExecutor(config, runner=runner))


def item(identifier: str) -> WatchedItem:
    return WatchedItem(
        source=SOURCE,
        identifier=identifier,
        title="t",
        body="b",
        state="open",
        address="https://example.invalid/items/" + identifier,
        classification="bug",
        submitter="someone",
    )


def polled(*items: WatchedItem) -> PollOutcome:
    return PollOutcome(
        source=SOURCE,
        status=PollStatus.OK,
        items=items,
        program="tracker-cli",
        exit_code=0,
    )


def set_project_cap(config: ConfigStore, limit: int) -> None:
    config.write(
        {"projects": {PROJECT: {"concurrency": {"project_max_runs": limit}}}},
        surface=DASHBOARD_SURFACE,
    )


class TestPollPath:
    def test_dispatching_an_item_posts_its_claimed_feedback(
        self, state: StateStore, config: ConfigStore, audit: AuditLog, tree: Path
    ) -> None:
        runner = Runner()
        report = dispatch_source(
            state,
            config,
            polled(item("7")),
            gate=AllowAll(),
            start=Starter(),
            feedback=poster_over(state, config, audit, runner),
        )

        assert [d.identifier for d in report.dispatched] == ["7"]
        assert runner.calls == [("gh", "comment", "7")]

    def test_without_a_poster_the_dispatch_still_runs_and_nothing_posts(
        self, state: StateStore, config: ConfigStore
    ) -> None:
        starter = Starter()
        report = dispatch_source(state, config, polled(item("7")), gate=AllowAll(), start=starter)

        assert [d.identifier for d in report.dispatched] == ["7"]
        assert len(starter.seeds) == 1

    def test_a_source_with_no_feedback_map_posts_nothing(
        self, state: StateStore, config: ConfigStore, audit: AuditLog
    ) -> None:
        # Remove the feedback map by rewriting the source without it.
        config.write(
            {
                "sources": {
                    SOURCE: {
                        "enabled": True,
                        "poll": ["tracker-cli", "list"],
                        "project": PROJECT,
                        "spec_types": {"bug": "bugfix"},
                        "autonomy": {"default": {"default": "authoring"}},
                        "feedback": None,
                    }
                }
            },
            surface=DASHBOARD_SURFACE,
        )
        runner = Runner()
        dispatch_source(
            state,
            config,
            polled(item("7")),
            gate=AllowAll(),
            start=Starter(),
            feedback=poster_over(state, config, audit, runner),
        )
        assert runner.calls == []

    def test_claimed_is_recorded_in_the_audit_log(
        self, state: StateStore, config: ConfigStore, audit: AuditLog
    ) -> None:
        runner = Runner()
        report = dispatch_source(
            state,
            config,
            polled(item("7")),
            gate=AllowAll(),
            start=Starter(),
            feedback=poster_over(state, config, audit, runner),
        )
        ref = report.dispatched[0].seed.ref
        feedback = [e for e in audit.read(ref) if e.event == AUDIT_ITEM_FEEDBACK]
        assert [e.detail["outcome"] for e in feedback] == [FeedbackOutcome.POSTED.value]
        assert feedback[0].detail["event"] == "claimed"


class TestQueuePath:
    def test_a_drained_item_posts_its_claimed_feedback(
        self, state: StateStore, config: ConfigStore, audit: AuditLog
    ) -> None:
        """The item that waited behind the cap must also announce it was claimed."""
        set_project_cap(config, 1)
        runner = Runner()
        poster = poster_over(state, config, audit, runner)

        report = dispatch_source(
            state,
            config,
            polled(item("1"), item("2")),
            gate=AllowAll(),
            start=Starter(),
            feedback=poster,
        )
        dispatched = [d.identifier for d in report.dispatched]
        queued = [d.identifier for d in report.queued]
        assert dispatched == ["1"]
        assert queued == ["2"]
        # Item 1's claimed posted on the poll path; item 2 is only queued so far.
        assert runner.calls == [("gh", "comment", "1")]

        # Free the slot, then drain: item 2 starts, and its claimed must post too.
        state.update_run(report.dispatched[0].seed.run_id, state=RunState.DONE.value)
        drained = drain_queue(state, config, gate=AllowAll(), start=Starter(), feedback=poster)
        assert [d.record.item_id for d in drained] == ["2"]
        assert runner.calls == [("gh", "comment", "1"), ("gh", "comment", "2")]
