"""The dispatcher refuses a starter that skipped the prerequisite gate.

``EngineGraph.begin_run`` is where the run gate runs before the first credit, and
the object it hands back — a ``SessionSeeder`` — is publicly constructible. So a
surface could build its own and pass it to a dispatch entry point as ``start=``,
opening host sessions for runs whose prerequisites were never checked. A gate a
caller can route around is documentation, so these tests are about the routes:

* every dispatch entry point (poll, source, queue drain), because a guarantee on
  one of several equivalent entry points is the shape that produced this
  project's shipped security defects;
* every obvious spelling of "hand over the seeder" — bare, bound method,
  ``functools.partial``, and a lambda closing over one — for the same reason;
* the accepted route, so the refusal is not simply "refuse everything": a
  ``GatedStarter`` gates each seed through the graph and then starts it.
"""

from __future__ import annotations

import functools
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.apps.builtins.spec_engine.engine.audit import AuditLog
from kiro_crew.apps.builtins.spec_engine.engine.autonomy import (
    AutonomyDecision,
    AutonomyLevel,
)
from kiro_crew.apps.builtins.spec_engine.engine.budget import RunAccounting
from kiro_crew.apps.builtins.spec_engine.engine.config import (
    DASHBOARD_SURFACE,
    ConfigStore,
)
from kiro_crew.apps.builtins.spec_engine.engine.prerequisites import (
    CheckName,
    Prerequisite,
    RunRefusal,
)
from kiro_crew.apps.builtins.spec_engine.engine.seeder import SessionSeeder
from kiro_crew.apps.builtins.spec_engine.engine.state import SpecRef, StateStore
from kiro_crew.apps.builtins.spec_engine.engine.watch import (
    ClassEvidence,
    GatedStarter,
    PollOutcome,
    PollStatus,
    RunSeed,
    SubmitterClass,
    TickReport,
    UngatedStarter,
    WatchedItem,
    dispatch_source,
    dispatch_tick,
    drain_queue,
)

SOURCE = "upstream-issues"
PROJECT = "acme"


class AllowAll:
    def dispatch_allowed(self, source: str) -> bool:
        return True


class CleanScreener:
    def screen_seed(self, route: Any, seed: RunSeed) -> RunSeed:
        return seed


class RecordingCascade:
    def archive_cancelled_item(
        self, ref: SpecRef, *, item_id: str, actor: str | None = None
    ) -> None:  # pragma: no cover - nothing is withdrawn in these tests
        return None


class NeverOpens:
    """A session opener that fails loudly if a run ever reaches it.

    These tests are about the refusal arriving *before* a session, so the opener
    is the assertion rather than a recorder: an ungated dispatch that got past the
    check would open one here.
    """

    def __call__(self, request: Any) -> Any:  # pragma: no cover - must never run
        raise AssertionError("an ungated dispatch opened a host session")


def _seeder(tmp_path: Path) -> SessionSeeder:
    """The engine's real starter, built the way a surface could build it."""
    state = StateStore(root=tmp_path / "seeder-state")
    return SessionSeeder(
        ConfigStore(tmp_path / "seeder-config"),
        opener=NeverOpens(),
        accounting=RunAccounting(state),
        audit=AuditLog(root=tmp_path / "seeder-audit"),
    )


def _stores(tmp_path: Path) -> tuple[StateStore, ConfigStore]:
    tree = tmp_path / "tree"
    (tree / ".kiro").mkdir(parents=True)
    config = ConfigStore(tmp_path / "config")
    config.write(
        {
            "projects": {PROJECT: {"path": str(tree)}},
            "sources": {
                SOURCE: {
                    "enabled": True,
                    "poll": ["tracker-cli", "list"],
                    "project": PROJECT,
                    "spec_types": {"bug": "bugfix"},
                }
            },
        },
        surface=DASHBOARD_SURFACE,
    )
    return StateStore(root=tmp_path / "state"), config


def _polled() -> PollOutcome:
    return PollOutcome(
        source=SOURCE,
        status=PollStatus.OK,
        items=(
            WatchedItem(
                source=SOURCE,
                identifier="1",
                title="a crash",
                body="it crashes",
                state="open",
                address="https://example.invalid/items/1",
                classification="bug",
                submitter="someone",
            ),
        ),
        program="tracker-cli",
        exit_code=0,
    )


def _seed(tmp_path: Path) -> RunSeed:
    return RunSeed(
        run_id="run-1",
        ref=SpecRef.of(tmp_path / "tree", "bugfix-1"),
        working_tree=tmp_path / "tree",
        project=PROJECT,
        base_branch="trunk",
        spec_type="bugfix",
        source=SOURCE,
        item=_polled().items[0],
        generation=1,
        submitter_class=SubmitterClass(name="external", evidence=ClassEvidence.UNDETERMINED),
        autonomy=AutonomyDecision(
            level=AutonomyLevel.INTEGRATION,
            source=SOURCE,
            spec_type="bugfix",
            submitter_class="external",
            declared_at="sources.upstream-issues.autonomy",
        ),
    )


def _ungated_spellings(seeder: SessionSeeder) -> dict[str, Any]:
    """Every way the same session-opening starter can be handed over."""
    return {
        "bare": seeder,
        "bound method": seeder.__call__,
        "partial": functools.partial(seeder),
        "closure": lambda seed: seeder(seed),
    }


class TestEveryEntryPointRefusesAnUngatedStarter:
    @pytest.mark.parametrize("spelling", sorted(_ungated_spellings(object.__new__(SessionSeeder))))
    def test_dispatch_source_refuses(self, tmp_path: Path, spelling: str) -> None:
        state, config = _stores(tmp_path)
        start = _ungated_spellings(_seeder(tmp_path))[spelling]

        with pytest.raises(UngatedStarter) as raised:
            dispatch_source(
                state,
                config,
                _polled(),
                gate=AllowAll(),
                start=start,
                screener=CleanScreener(),
            )

        assert "prerequisite gate" in str(raised.value)

    @pytest.mark.parametrize("spelling", sorted(_ungated_spellings(object.__new__(SessionSeeder))))
    def test_dispatch_tick_refuses(self, tmp_path: Path, spelling: str) -> None:
        state, config = _stores(tmp_path)
        start = _ungated_spellings(_seeder(tmp_path))[spelling]

        with pytest.raises(UngatedStarter):
            dispatch_tick(
                TickReport(outcomes=(_polled(),)),
                state=state,
                config=config,
                start=start,
                screener=CleanScreener(),
                cascade=RecordingCascade(),
                audit=AuditLog(root=tmp_path / "audit"),
            )

    @pytest.mark.parametrize("spelling", sorted(_ungated_spellings(object.__new__(SessionSeeder))))
    def test_drain_queue_refuses(self, tmp_path: Path, spelling: str) -> None:
        """The queue is the second way an item starts, so it checks too."""
        state, config = _stores(tmp_path)
        start = _ungated_spellings(_seeder(tmp_path))[spelling]

        with pytest.raises(UngatedStarter):
            drain_queue(
                state,
                config,
                gate=AllowAll(),
                start=start,
                screener=CleanScreener(),
            )

    def test_nothing_was_claimed_by_the_refused_dispatch(self, tmp_path: Path) -> None:
        """The refusal lands before the claim, so the backlog survives it.

        Otherwise an ungated wiring would burn every item's generation on the way
        to being refused, and the items would never be offered again.
        """
        state, config = _stores(tmp_path)

        with pytest.raises(UngatedStarter):
            dispatch_source(
                state,
                config,
                _polled(),
                gate=AllowAll(),
                start=_seeder(tmp_path),
                screener=CleanScreener(),
            )

        assert state.list_claims() == []
        assert state.list_runs() == []


class TestAnOrdinaryCallableIsLeftAlone:
    def test_a_starter_that_opens_no_session_is_accepted(self, tmp_path: Path) -> None:
        """The check is on the dangerous object, not a blessed type.

        A callable that cannot open a host session cannot spend credits on an
        ungated run, so requiring it to prove provenance would only make every
        test double satisfy a type check while closing nothing.
        """
        state, config = _stores(tmp_path)
        seeds: list[RunSeed] = []

        def start(seed: RunSeed) -> None:
            seeds.append(seed)

        report = dispatch_source(
            state,
            config,
            _polled(),
            gate=AllowAll(),
            start=start,
            screener=CleanScreener(),
        )

        assert [d.identifier for d in report.dispatched] == ["1"]
        assert len(seeds) == 1


class TestTheGatedStarterIsTheAcceptedRoute:
    def test_it_gates_each_seed_and_then_starts_it(self, tmp_path: Path) -> None:
        started: list[RunSeed] = []
        gated: list[tuple[str, AutonomyLevel, str]] = []

        class Gate:
            def begin_run(
                self,
                ref: SpecRef,
                level: AutonomyLevel,
                *,
                base_branch: str = "",
                run: str | None = None,
            ) -> Any:
                gated.append((ref.name, level, run or ""))
                return started.append

        GatedStarter(Gate())(_seed(tmp_path))

        assert gated == [("bugfix-1", AutonomyLevel.INTEGRATION, "run-1")]
        assert [seed.run_id for seed in started] == ["run-1"]

    def test_a_refused_prerequisite_starts_nothing(self, tmp_path: Path) -> None:
        """The gate's refusal reaches the caller instead of a silent no-op."""
        from kiro_crew.apps.builtins.spec_engine.engine.composition import RunPrevented

        refusal = RunRefusal(
            level=AutonomyLevel.DELIVERY,
            unmet=(
                Prerequisite(
                    check=CheckName.PROGRAMS,
                    phase=AutonomyLevel.DELIVERY,
                    met=False,
                    missing="the delivery program is not on PATH",
                    action="install it",
                ),
            ),
        )

        class RefusingGate:
            def begin_run(
                self,
                ref: SpecRef,
                level: AutonomyLevel,
                *,
                base_branch: str = "",
                run: str | None = None,
            ) -> Any:
                raise RunPrevented(refusal)

        with pytest.raises(RunPrevented):
            GatedStarter(RefusingGate())(_seed(tmp_path))

    def test_a_gated_starter_passes_every_entry_point(self, tmp_path: Path) -> None:
        """The accepted route reaches a real dispatch, not only the wrapper."""
        state, config = _stores(tmp_path)
        started: list[RunSeed] = []

        class Gate:
            def begin_run(
                self,
                ref: SpecRef,
                level: AutonomyLevel,
                *,
                base_branch: str = "",
                run: str | None = None,
            ) -> Any:
                return started.append

        reports = dispatch_tick(
            TickReport(outcomes=(_polled(),)),
            state=state,
            config=config,
            start=GatedStarter(Gate()),
            screener=CleanScreener(),
            cascade=RecordingCascade(),
            audit=AuditLog(root=tmp_path / "audit"),
        )

        assert [d.identifier for report in reports for d in report.dispatched] == ["1"]
        assert len(started) == 1
