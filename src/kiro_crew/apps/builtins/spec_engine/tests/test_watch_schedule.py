"""The watcher's schedule: what installs the shim and registers the crons.

Task 8.1 built the tick and proved it costs nothing, and nothing scheduled it.
An unregistered watcher polls no source at all, so these tests are about the
registration rather than about the tick: each is written to fail when the
construction is deleted from the startup hook or from
:func:`~...engine.watch.wiring.install_watch_schedule`, with the tick untouched.

The schedule is *reconciled*, not created, because configuration is the authority:
a source the operator disabled must lose its job, a changed interval must take
effect, and a job left behind by a failed removal must not keep polling a source
nobody watches.
"""

from __future__ import annotations

import asyncio
import dataclasses
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from kiro_crew.apps.builtins.spec_engine import startup
from kiro_crew.apps.builtins.spec_engine.engine.config import DASHBOARD_SURFACE, ConfigStore
from kiro_crew.apps.builtins.spec_engine.engine.watch.tick import (
    CRON_ENTRY_POINT,
    CRON_SCRIPT_FILENAME,
    cron_job_name,
)
from kiro_crew.apps.builtins.spec_engine.engine.watch.wiring import (
    REVIEW_FEEDBACK_ENTRY_POINT,
    REVIEW_FEEDBACK_JOB,
    install_watch_schedule,
    run_review_feedback_script,
    watch_definitions,
)

PROJECT = "acme"
SOURCE = "upstream-issues"
OTHER_SOURCE = "downstream-issues"


@dataclasses.dataclass
class FakeJob:
    """A scheduled job, carrying the fields the reconciliation reads."""

    id: str
    name: str
    message: str = ""
    every_secs: int | None = None
    script: str = ""
    timeout: int = 0
    silent: bool = True
    enabled: bool = True
    user_paused: bool = False


class FakeCron:
    """The app-scoped cron SDK, narrowed to what the reconciliation calls.

    Only the ``*_async`` mutators exist here on purpose: the synchronous ones
    refuse on a running event loop, so a reconciliation that reached for them
    would raise in the gateway rather than schedule anything, and a fake that
    offered both would hide it.
    """

    def __init__(self, jobs: list[FakeJob] | None = None) -> None:
        self.jobs = list(jobs or [])
        self.added: list[dict[str, Any]] = []
        self.updated: list[tuple[str, dict[str, Any]]] = []
        self.removed: list[str] = []
        self._next = len(self.jobs) + 1

    def list_jobs(self) -> list[FakeJob]:
        return list(self.jobs)

    async def add_job_async(self, name: str, message: str, **kwargs: Any) -> FakeJob:
        self.added.append({"name": name, "message": message, **kwargs})
        job = FakeJob(
            id=f"job-{self._next}",
            name=name,
            message=message,
            every_secs=kwargs.get("every_secs"),
            script=kwargs.get("script", ""),
            timeout=kwargs.get("timeout", 0),
            silent=kwargs.get("silent", True),
            enabled=kwargs.get("enabled", True),
        )
        self._next += 1
        self.jobs.append(job)
        return job

    async def update_job_async(self, job_id: str, **kwargs: Any) -> FakeJob | None:
        self.updated.append((job_id, dict(kwargs)))
        for job in self.jobs:
            if job.id == job_id:
                for key, value in kwargs.items():
                    setattr(job, key, value)
                return job
        return None

    async def remove_job_async(self, job_id: str) -> bool:
        self.removed.append(job_id)
        self.jobs = [job for job in self.jobs if job.id != job_id]
        return True


@pytest.fixture()
def config(tmp_path: Path) -> ConfigStore:
    store = ConfigStore(tmp_path / "config")
    tree = tmp_path / "tree"
    (tree / ".kiro").mkdir(parents=True)
    store.write(
        {
            "projects": {PROJECT: {"path": str(tree)}},
            "sources": {
                SOURCE: {
                    "enabled": True,
                    "poll": ["echo", "list"],
                    "project": PROJECT,
                    "spec_types": {"bug": "bugfix"},
                }
            },
        },
        surface=DASHBOARD_SURFACE,
    )
    return store


def reconcile(cron: FakeCron, config: ConfigStore, directory: Path) -> Any:
    return asyncio.run(install_watch_schedule(cron, store=config, directory=directory))


class TestTheShimAndTheJobsAreInstalled:
    def test_an_enabled_source_gets_a_job_and_the_shim_is_written(
        self, config: ConfigStore, tmp_path: Path
    ) -> None:
        cron = FakeCron()

        report = reconcile(cron, config, tmp_path / "crons")

        assert (tmp_path / "crons" / CRON_SCRIPT_FILENAME).is_file()
        assert report.added == (cron_job_name(SOURCE),)
        added = cron.added[0]
        # The source travels in the job's message, and the job runs the shim's
        # entry point rather than a command.
        assert added["message"] == SOURCE
        assert added["script"].endswith(f"{CRON_SCRIPT_FILENAME}:{CRON_ENTRY_POINT}")
        # Explicit timeout: the host's default script ceiling is shorter than a
        # poll command may legitimately take, and a job left at it would be killed
        # mid-poll and recorded as a failure of the source.
        assert added["timeout"] > 0
        assert added["silent"] is True

    def test_a_second_reconciliation_changes_nothing(
        self, config: ConfigStore, tmp_path: Path
    ) -> None:
        """Idempotent: startup happens often, and each one must not churn the store."""
        cron = FakeCron()
        reconcile(cron, config, tmp_path / "crons")

        report = reconcile(cron, config, tmp_path / "crons")

        assert (report.added, report.updated, report.removed) == ((), (), ())

    def test_a_disabled_source_loses_its_job(self, config: ConfigStore, tmp_path: Path) -> None:
        cron = FakeCron()
        reconcile(cron, config, tmp_path / "crons")
        config.write({"sources": {SOURCE: {"enabled": False}}}, surface=DASHBOARD_SURFACE)

        report = reconcile(cron, config, tmp_path / "crons")

        assert report.removed == (cron_job_name(SOURCE),)
        assert cron.jobs == []

    def test_a_changed_interval_is_updated_rather_than_duplicated(
        self, config: ConfigStore, tmp_path: Path
    ) -> None:
        cron = FakeCron()
        reconcile(cron, config, tmp_path / "crons")
        config.write(
            {"sources": {SOURCE: {"watch": {"interval_s": 900}}}},
            surface=DASHBOARD_SURFACE,
        )

        report = reconcile(cron, config, tmp_path / "crons")

        assert report.updated == (cron_job_name(SOURCE),)
        assert cron.updated[0][1]["every_secs"] == 900
        assert len(cron.jobs) == 1

    def test_a_job_whose_script_moved_is_replaced(
        self, config: ConfigStore, tmp_path: Path
    ) -> None:
        """The scheduler cannot update a job's script, so a stale one is replaced.

        An update that silently applied nothing would leave the old file running
        for good.
        """
        stale = FakeJob(id="job-1", name=cron_job_name(SOURCE), script="/gone/old.py:run")
        cron = FakeCron([stale])

        report = reconcile(cron, config, tmp_path / "crons")

        assert report.removed == (cron_job_name(SOURCE),)
        assert report.added == (cron_job_name(SOURCE),)
        assert cron.jobs[0].script.endswith(f"{CRON_SCRIPT_FILENAME}:{CRON_ENTRY_POINT}")

    def test_a_job_the_watcher_does_not_own_is_left_alone(
        self, config: ConfigStore, tmp_path: Path
    ) -> None:
        """Another feature's job is not this reconciliation's to remove."""
        theirs = FakeJob(id="job-9", name="daily-briefing")
        cron = FakeCron([theirs])

        report = reconcile(cron, config, tmp_path / "crons")

        assert report.removed == ()
        assert theirs in cron.jobs


class TestTheReviewFeedbackPollIsScheduledOnlyWhereArmed:
    def test_an_unarmed_project_gets_no_review_feedback_job(
        self, config: ConfigStore, tmp_path: Path
    ) -> None:
        cron = FakeCron()

        reconcile(cron, config, tmp_path / "crons")

        assert REVIEW_FEEDBACK_JOB not in {job.name for job in cron.jobs}

    def test_an_armed_project_gets_one(self, config: ConfigStore, tmp_path: Path) -> None:
        _arm(config)
        cron = FakeCron()

        report = reconcile(cron, config, tmp_path / "crons")

        assert REVIEW_FEEDBACK_JOB in report.added
        job = next(j for j in cron.jobs if j.name == REVIEW_FEEDBACK_JOB)
        assert job.script.endswith(f"{CRON_SCRIPT_FILENAME}:{REVIEW_FEEDBACK_ENTRY_POINT}")

    def test_disarming_removes_it(self, config: ConfigStore, tmp_path: Path) -> None:
        _arm(config)
        cron = FakeCron()
        reconcile(cron, config, tmp_path / "crons")
        config.write(
            {"projects": {PROJECT: {"delivery": {"review_feedback_enabled": False}}}},
            surface=DASHBOARD_SURFACE,
        )

        report = reconcile(cron, config, tmp_path / "crons")

        assert REVIEW_FEEDBACK_JOB in report.removed

    def test_the_definitions_carry_both_schedules_from_one_call(
        self, config: ConfigStore
    ) -> None:
        """One function, so a caller cannot schedule the polls and forget this one."""
        _arm(config)

        names = {d["name"] for d in watch_definitions(config)}

        assert names == {cron_job_name(SOURCE), REVIEW_FEEDBACK_JOB}

    def test_the_poll_entry_point_spends_nothing_while_its_seams_are_missing(
        self, config: ConfigStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """It skips before polling, so an armed project costs nothing today.

        The fix-round reviser and the delivery pipeline are not constructible in a
        cron subprocess yet. The honest behaviour is a skip, not a poll whose
        comments nothing can act on and not a message every five minutes.
        """
        from kiro_crew.cron_script import Skip

        monkeypatch.setattr(
            "kiro_crew.apps.builtins.spec_engine.engine.watch.wiring.review_feedback_armed",
            lambda store=None: (PROJECT,),
        )

        with pytest.raises(Skip):
            run_review_feedback_script(SimpleNamespace(message=""))


class TestTheStartupHookIsWhatSchedulesIt:
    def test_the_hook_reconciles_the_schedule(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Deleting the call from the hook leaves the whole watcher unscheduled."""
        calls: list[Any] = []

        async def fake_install(cron: Any, **kwargs: Any) -> Any:
            calls.append(cron)
            return SimpleNamespace(problems=())

        monkeypatch.setattr(startup, "install_watch_schedule", fake_install)
        monkeypatch.setattr(startup.readiness, "on_startup", lambda ctx: None)
        cron = FakeCron()

        asyncio.run(startup.on_startup(SimpleNamespace(data_dir=tmp_path, cron=cron)))

        assert calls == [cron]

    def test_a_scheduling_failure_degrades_rather_than_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A hook that raised would leave no readiness state and no explanation."""

        async def explode(cron: Any, **kwargs: Any) -> Any:
            raise RuntimeError("the cron store is busy")

        monkeypatch.setattr(startup, "install_watch_schedule", explode)
        monkeypatch.setattr(startup.readiness, "on_startup", lambda ctx: None)
        degraded: list[str] = []
        health = SimpleNamespace(mark_degraded=lambda reason: degraded.append(reason))

        asyncio.run(
            startup.on_startup(
                SimpleNamespace(data_dir=tmp_path, cron=FakeCron(), health=health)
            )
        )

        assert degraded and "cron store is busy" in degraded[0]


def _arm(config: ConfigStore) -> None:
    """Arm review feedback for the project, at the project's own scope."""
    config.write(
        {
            "projects": {
                PROJECT: {
                    "delivery": {"review_feedback_enabled": True},
                    "review_feedback": {"poll": ["echo", "comments"]},
                }
            }
        },
        surface=DASHBOARD_SURFACE,
    )
