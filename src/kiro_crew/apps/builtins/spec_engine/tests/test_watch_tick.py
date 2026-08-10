"""The poll tick: what it schedules, and the credits it cannot spend.

Two kinds of claim live here. The first is ordinary behaviour: one cron job per
enabled source, an idle tick that delivers nothing, an unhealthy source that is
reported every time it is unhealthy.

The second is the cost claim, and it is asserted structurally rather than by
measurement. "Idle watching is free" is only durable if there is no reachable
path from a tick to a model, so one test walks the watcher's imports and refuses
the dispatch surfaces outright, and another runs a real tick against a redirected
data home and asserts the per-turn metering ledger gained nothing. A benchmark
would tell us today's number; these tell us the next change cannot quietly add a
turn.
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path
from typing import Any, Iterator, Sequence

import pytest

from kiro_crew.apps.builtins.spec_engine.engine import watch
from kiro_crew.apps.builtins.spec_engine.engine.config import (
    DASHBOARD_SURFACE,
    ConfigStore,
)
from kiro_crew.apps.builtins.spec_engine.engine.delivery import CommandOutcome
from kiro_crew.apps.builtins.spec_engine.engine.watch import (
    CRON_ENTRY_POINT,
    CRON_SCRIPT_FILENAME,
    CRON_TIMEOUT_MARGIN_S,
    HOST_SCRIPT_TIMEOUT_S,
    HealthReason,
    PollStatus,
    cron_definitions,
    cron_job_name,
    crons_directory,
    install_tick_script,
    poll_tick,
    run_tick_script,
    source_of_job,
    tick_script_path,
)

ABSENT_PROGRAM = "kirocrew-nonexistent-tracker-cli"

#: Modules that reach a model, an agent session, or a turn's cost. A watcher
#: importing any of them would make the zero-credit claim a matter of care rather
#: than of structure.
FORBIDDEN_IMPORTS = (
    "kiro_crew.acp",
    "kiro_crew.agent",
    "kiro_crew.session",
    "kiro_crew.subagent",
    "kiro_crew.mcp_core",
    "kiro_crew.mcp_caller",
    "kiro_crew.model_registry",
    "kiro_crew.effort",
    "kiro_crew.llm_helpers",
    "kiro_crew.task_executor",
    "kiro_crew.taskrunner",
    "kiro_crew.dashboard",
)


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A redirected data home, so the tick reads and writes only under it."""
    root = tmp_path / "home"
    root.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(root))
    return root


@pytest.fixture()
def store(tmp_path: Path) -> ConfigStore:
    return ConfigStore(tmp_path / "state")


def configure(store: ConfigStore, document: dict[str, Any]) -> None:
    store.write(document, surface=DASHBOARD_SURFACE)


def emitter(tmp_path: Path, payload: str) -> list[str]:
    """An argv that prints *payload* and exits zero."""
    script = tmp_path / f"emit_{abs(hash(payload)) % 10**8}.py"
    script.write_text(f"import sys\nsys.stdout.write({payload!r})\n", encoding="utf-8")
    return [sys.executable, str(script)]


class Recorder:
    def __init__(self, outcome: CommandOutcome) -> None:
        self.outcome = outcome
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, argv: Sequence[str], *, cwd: Path, timeout_s: int) -> CommandOutcome:
        self.calls.append(tuple(argv))
        return self.outcome


class FakeJob:
    """Stands in for the scheduler's job object: only ``message`` is read."""

    def __init__(self, message: str = "") -> None:
        self.message = message


def watch_modules() -> Iterator[Path]:
    package = Path(watch.__file__).parent
    yield from sorted(package.glob("*.py"))


def imported_modules(path: Path) -> set[str]:
    """Every module name *path* imports, absolute names only."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


class TestNoPathToAModel:
    def test_the_watcher_imports_no_model_or_session_surface(self) -> None:
        offences: list[str] = []
        for path in watch_modules():
            for name in sorted(imported_modules(path)):
                for forbidden in FORBIDDEN_IMPORTS:
                    if name == forbidden or name.startswith(forbidden + "."):
                        offences.append(f"{path.name} imports {name}")
        assert offences == []

    def test_a_tick_writes_nothing_to_the_metering_ledger(self, home: Path, tmp_path: Path) -> None:
        store = ConfigStore(tmp_path / "state")
        configure(
            store,
            {
                "sources": {
                    "quiet": {"poll": emitter(tmp_path, "[]"), "enabled": True},
                    "busy": {
                        "poll": emitter(tmp_path, json.dumps([{"identifier": "9"}])),
                        "enabled": True,
                    },
                    "broken": {"poll": [ABSENT_PROGRAM], "enabled": True},
                }
            },
        )
        ledger = home / "usage" / "tokens"

        report = poll_tick(store)

        # Every branch a tick has — a quiet source, a source with items, and a
        # broken one — and not one per-turn record between them.
        assert len(report.outcomes) == 3
        assert not ledger.exists() or list(ledger.iterdir()) == []


class TestTickReport:
    def test_only_enabled_sources_are_polled(self, store: ConfigStore, tmp_path: Path) -> None:
        configure(
            store,
            {
                "sources": {
                    "on": {"poll": emitter(tmp_path, "[]"), "enabled": True},
                    "off": {"poll": [ABSENT_PROGRAM], "enabled": False},
                }
            },
        )

        report = poll_tick(store)

        assert [outcome.source for outcome in report.outcomes] == ["on"]

    def test_a_named_source_is_polled_even_when_others_are_enabled(
        self, store: ConfigStore, tmp_path: Path
    ) -> None:
        configure(
            store,
            {
                "sources": {
                    "first": {"poll": emitter(tmp_path, "[]"), "enabled": True},
                    "second": {"poll": emitter(tmp_path, "[]"), "enabled": True},
                }
            },
        )

        report = poll_tick(store, sources=["second"])

        assert [outcome.source for outcome in report.outcomes] == ["second"]

    def test_a_job_left_behind_for_a_disabled_source_polls_nothing(
        self, store: ConfigStore
    ) -> None:
        # Configuration is the authority on enablement, so a stale job naming a
        # disabled source must not be the thing that decides to poll it.
        configure(store, {"sources": {"upstream": {"poll": [ABSENT_PROGRAM]}}})
        recorder = Recorder(CommandOutcome(exit_code=0, stdout="[]"))

        report = poll_tick(store, sources=["upstream"], runner=recorder)

        assert report.outcomes[0].status is PollStatus.DISABLED
        assert recorder.calls == []

    def test_an_idle_tick_is_idle(self, store: ConfigStore, tmp_path: Path) -> None:
        configure(store, {"sources": {"on": {"poll": emitter(tmp_path, "[]"), "enabled": True}}})

        report = poll_tick(store)

        assert report.idle is True
        assert report.items == ()

    def test_a_tick_with_an_unhealthy_source_is_not_idle(self, store: ConfigStore) -> None:
        # Nothing found might mean nothing to find, or might mean nothing could
        # be looked at; a tick that cannot tell them apart is not idle.
        configure(store, {"sources": {"on": {"poll": [ABSENT_PROGRAM], "enabled": True}}})

        report = poll_tick(store)

        assert report.idle is False
        assert report.unhealthy[0].reason is HealthReason.PROGRAM_UNAVAILABLE

    def test_a_tick_with_items_is_not_idle(self, store: ConfigStore, tmp_path: Path) -> None:
        payload = json.dumps([{"identifier": "4", "title": "a bug"}])
        configure(store, {"sources": {"on": {"poll": emitter(tmp_path, payload), "enabled": True}}})

        report = poll_tick(store)

        assert report.idle is False
        assert [item.identifier for item in report.items] == ["4"]

    def test_the_summary_names_every_source_and_the_missing_program(
        self, store: ConfigStore, tmp_path: Path
    ) -> None:
        configure(
            store,
            {
                "sources": {
                    "on": {"poll": emitter(tmp_path, "[]"), "enabled": True},
                    "broken": {"poll": [ABSENT_PROGRAM], "enabled": True},
                }
            },
        )

        summary = poll_tick(store).summary()

        assert "on" in summary
        assert "broken" in summary
        assert ABSENT_PROGRAM in summary


class TestCronDefinitions:
    def test_one_job_per_enabled_source(self, store: ConfigStore, tmp_path: Path) -> None:
        configure(
            store,
            {
                "sources": {
                    "on": {"poll": emitter(tmp_path, "[]"), "enabled": True},
                    "also-on": {"poll": emitter(tmp_path, "[]"), "enabled": True},
                    "off": {"poll": emitter(tmp_path, "[]")},
                }
            },
        )

        definitions = cron_definitions(store, script_path="/crons/x.py:run")

        # The persisted document sorts its keys, so the enabled set arrives in
        # that order rather than in the order somebody typed it.
        assert [d["name"] for d in definitions] == [
            cron_job_name("also-on"),
            cron_job_name("on"),
        ]

    def test_no_enabled_source_schedules_nothing(self, store: ConfigStore) -> None:
        configure(store, {"sources": {"off": {"poll": [ABSENT_PROGRAM]}}})
        assert cron_definitions(store, script_path="/crons/x.py:run") == ()

    def test_each_job_carries_its_sources_own_interval(
        self, store: ConfigStore, tmp_path: Path
    ) -> None:
        configure(
            store,
            {
                "watch": {"interval_s": 600},
                "sources": {
                    "slow": {"poll": emitter(tmp_path, "[]"), "enabled": True},
                    "fast": {
                        "poll": emitter(tmp_path, "[]"),
                        "enabled": True,
                        "watch": {"interval_s": 60},
                    },
                },
            },
        )

        by_name = {d["name"]: d for d in cron_definitions(store, script_path="/x.py:run")}

        assert by_name[cron_job_name("slow")]["every"] == 600
        assert by_name[cron_job_name("fast")]["every"] == 60

    def test_the_job_timeout_leaves_room_for_the_poll_it_runs(
        self, store: ConfigStore, tmp_path: Path
    ) -> None:
        # The host kills a script job at its own default, which is shorter than a
        # poll command may legitimately take: a job that did not set this would
        # record a killed schedule as a broken source.
        configure(
            store,
            {
                "sources": {
                    "upstream": {
                        "poll": emitter(tmp_path, "[]"),
                        "enabled": True,
                        "timeouts": {"poll_command_s": 200},
                    }
                }
            },
        )

        definition = cron_definitions(store, script_path="/x.py:run")[0]

        assert definition["timeout"] == 200 + CRON_TIMEOUT_MARGIN_S
        assert definition["timeout"] > HOST_SCRIPT_TIMEOUT_S

    def test_the_source_travels_in_the_jobs_message(
        self, store: ConfigStore, tmp_path: Path
    ) -> None:
        configure(
            store, {"sources": {"upstream": {"poll": emitter(tmp_path, "[]"), "enabled": True}}}
        )

        definition = cron_definitions(store, script_path="/x.py:run")[0]

        assert definition["message"] == "upstream"
        assert source_of_job(definition["name"]) == "upstream"
        assert definition["silent"] is True

    def test_a_job_name_from_elsewhere_names_no_source(self) -> None:
        assert source_of_job("nightly-digest") == ""


class TestTickScriptInstallation:
    def test_the_script_lands_in_the_hosts_crons_directory(self, home: Path) -> None:
        installed = install_tick_script()

        assert installed == tick_script_path()
        assert installed.parent == crons_directory()
        assert installed.name == CRON_SCRIPT_FILENAME
        assert installed.is_relative_to(home)

    def test_the_installed_script_is_what_the_scheduler_will_accept(self, home: Path) -> None:
        from kiro_crew.cron_script import resolve_script_path
        from kiro_crew.mcp_cron import _vet_script_file

        installed = install_tick_script()
        spec = f"{installed}:{CRON_ENTRY_POINT}"

        resolved, function = resolve_script_path(spec)

        assert Path(resolved) == installed
        assert function == CRON_ENTRY_POINT
        assert _vet_script_file(resolved) is None

    def test_the_installed_script_exposes_the_entry_point(self, home: Path) -> None:
        namespace: dict[str, Any] = {}
        exec(install_tick_script().read_text(encoding="utf-8"), namespace)
        assert callable(namespace[CRON_ENTRY_POINT])

    def test_installing_twice_leaves_the_file_alone(self, home: Path) -> None:
        first = install_tick_script()
        stamp = first.stat().st_mtime_ns

        again = install_tick_script()

        assert again == first
        assert again.stat().st_mtime_ns == stamp

    def test_a_replaced_script_is_restored(self, home: Path) -> None:
        installed = install_tick_script()
        installed.write_text("# something else\n", encoding="utf-8")

        install_tick_script()

        assert CRON_ENTRY_POINT in installed.read_text(encoding="utf-8")

    def test_the_definitions_point_at_the_installed_script(self, home: Path) -> None:
        store = ConfigStore()
        configure(
            store,
            {"sources": {"upstream": {"poll": [ABSENT_PROGRAM], "enabled": True}}},
        )

        definition = cron_definitions(store)[0]

        assert definition["script"] == f"{install_tick_script()}:{CRON_ENTRY_POINT}"


class TestSchedulerEntryPoint:
    def test_an_idle_tick_delivers_nothing(self, home: Path, tmp_path: Path) -> None:
        from kiro_crew.cron_script import Skip

        store = ConfigStore()
        configure(
            store,
            {"sources": {"upstream": {"poll": emitter(tmp_path, "[]"), "enabled": True}}},
        )

        with pytest.raises(Skip):
            run_tick_script(FakeJob("upstream"))

    def test_an_unhealthy_source_is_reported_naming_the_program(self, home: Path) -> None:
        from kiro_crew.cron_script import Report

        store = ConfigStore()
        configure(store, {"sources": {"upstream": {"poll": [ABSENT_PROGRAM], "enabled": True}}})

        with pytest.raises(Report) as caught:
            run_tick_script(FakeJob("upstream"))

        assert ABSENT_PROGRAM in caught.value.message
        assert "upstream" in caught.value.message

    def test_a_tick_that_found_items_reports_nothing_by_itself(
        self, home: Path, tmp_path: Path
    ) -> None:
        # An open item is still open next tick; a message per tick per item is
        # noise. Dispatch, not delivery, is what acts on a found item, so the
        # tick returns without raising either control exception.
        store = ConfigStore()
        payload = json.dumps([{"identifier": "12", "title": "still open"}])
        configure(
            store,
            {"sources": {"upstream": {"poll": emitter(tmp_path, payload), "enabled": True}}},
        )

        run_tick_script(FakeJob("upstream"))

    def test_a_job_with_no_message_polls_every_enabled_source(
        self, home: Path, tmp_path: Path
    ) -> None:
        from kiro_crew.cron_script import Report

        store = ConfigStore()
        configure(
            store,
            {
                "sources": {
                    "quiet": {"poll": emitter(tmp_path, "[]"), "enabled": True},
                    "broken": {"poll": [ABSENT_PROGRAM], "enabled": True},
                }
            },
        )

        with pytest.raises(Report) as caught:
            run_tick_script(FakeJob())

        assert "broken" in caught.value.message
