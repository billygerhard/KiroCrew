"""The headless run driver seeds a session under exactly the granted authority.

The claims here are the ones whose regression grants a session more than the
operator did, or leaves an unattended run silent when a person needs to act:

1. **The applied posture is the engine's own resolution, and only that.** The
   seeder resolves the tool-approval posture from ``session_posture`` and passes
   it to the opener; it does not read the run row's autonomy level, and it takes
   no posture from a caller. A run whose policy authorizes integration still runs
   its session under the app's *granted* approval posture, not one derived from
   the ladder.
2. **The session is stamped with the run id.** Its turns are attributable, so the
   budget ceiling can see them.
3. **The applied posture is recorded and verified.** Whatever posture the session
   came up under is written to the audit log, and a posture the operator did not
   grant refuses the run rather than proceeding elevated.
4. **A run parked for review is announced.** A human-reserved gate reaches the
   configured channel, defaulting to the dashboard, and a channel outage is
   recorded rather than allowed to fail the run.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.apps.approval_grants import (
    POSTURE_AUTO,
    POSTURE_DEFAULT,
    RESERVED_APPROVAL_ENV,
)
from kiro_crew.apps.builtins.spec_engine.engine import seeder as seeder_module
from kiro_crew.apps.builtins.spec_engine.engine.audit import AuditLog
from kiro_crew.apps.builtins.spec_engine.engine.autonomy import (
    AutonomyDecision,
    AutonomyLevel,
)
from kiro_crew.apps.builtins.spec_engine.engine.budget import RunAccounting
from kiro_crew.apps.builtins.spec_engine.engine.config import DASHBOARD_SURFACE, ConfigStore
from kiro_crew.apps.builtins.spec_engine.engine.config.store import APP_NAME
from kiro_crew.apps.builtins.spec_engine.engine.notify import DASHBOARD_CHANNEL, REVIEW_CHANNEL
from kiro_crew.apps.builtins.spec_engine.engine.seeder import (
    AWAITING_REVIEW_EVENT,
    AWAITING_REVIEW_NOTIFY_FAILED_EVENT,
    POSTURE_MISMATCH_EVENT,
    SESSION_SEEDED_EVENT,
    OpenedSession,
    PostureMismatch,
    SeededRun,
    SessionRequest,
    SessionSeeder,
    reserves_human_review,
    session_name,
)
from kiro_crew.apps.builtins.spec_engine.engine.state import SpecRef, StateStore
from kiro_crew.notifications.bus import NotificationPayload

PROJECT = "acme"


# --- test doubles ----------------------------------------------------------


@dataclass(frozen=True)
class FakeOpenedSession:
    """A session the opener brought up, and the posture it came up under."""

    session_key: str
    applied_posture: str


class FakeOpener:
    """Records every request and returns a session under a chosen posture.

    ``applied`` defaults to echoing the requested posture — the honest host, which
    applies what it was asked to. A test forces a different value to model a
    session that came up more or less permissive than the grant.
    """

    def __init__(self, *, applied: str | None = None) -> None:
        self.requests: list[SessionRequest] = []
        self._applied = applied
        self._n = 0

    def __call__(self, request: SessionRequest) -> OpenedSession:
        self.requests.append(request)
        self._n += 1
        applied = request.posture if self._applied is None else self._applied
        return FakeOpenedSession(session_key=f"chat-{self._n}", applied_posture=applied)


class FakeBus:
    """Records what the host notification bus would receive."""

    def __init__(self, *, fail: bool = False) -> None:
        self.pushed: list[NotificationPayload] = []
        self.registered: dict[str, str] = {}
        self._fail = fail

    def is_registered(self, channel: str) -> bool:
        return channel in self.registered

    def register_channel(self, channel: str, default_priority: str = "default") -> None:
        self.registered[channel] = default_priority

    def push(self, payload: NotificationPayload) -> dict[str, Any]:
        if self._fail:  # pragma: no cover - not exercised; absence of a bus is the tested path
            raise RuntimeError("bus said no")
        self.pushed.append(payload)
        return {"channel": payload.channel}


class FakeState:
    """Gateway state, reduced to the notification bus the notifier reaches for."""

    def __init__(self, bus: FakeBus | None) -> None:
        self.notification_bus = bus


@dataclass(frozen=True)
class FakeRun:
    """A resolved run, carrying only what the seeder reads (satisfies SeededRun)."""

    run_id: str
    ref: SpecRef
    project: str
    working_tree: Path
    autonomy: AutonomyDecision
    prompt: str = "author this spec"

    def seed_text(self) -> str:
        return self.prompt


# --- fixtures --------------------------------------------------------------


@pytest.fixture()
def config(tmp_path: Path) -> ConfigStore:
    return ConfigStore(root=tmp_path / "config")


@pytest.fixture()
def store(tmp_path: Path) -> StateStore:
    return StateStore(root=tmp_path / "state")


@pytest.fixture()
def audit(tmp_path: Path) -> AuditLog:
    return AuditLog(root=tmp_path / "state")


@pytest.fixture()
def accounting(store: StateStore) -> RunAccounting:
    return RunAccounting(store)


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    return root


@pytest.fixture()
def ref(project: Path) -> SpecRef:
    return SpecRef.of(project, "example")


def grant(monkeypatch: pytest.MonkeyPatch, granted: str) -> None:
    """Pin the engine's posture resolution to *granted*.

    Models the real contract: :func:`session_posture` returns the granted
    posture, and :func:`verify_session_posture` passes only when the applied
    posture equals it. Patched in the seeder's namespace so the real grant file
    and manifest — neither of which a test controls — do not decide the outcome.
    """

    def _verify(app: str, applied: str) -> str | None:
        if applied == granted:
            return None
        return f"applied {applied or 'default'!r} does not match granted {granted or 'default'!r}"

    monkeypatch.setattr(seeder_module, "session_posture", lambda app: granted)
    monkeypatch.setattr(seeder_module, "verify_session_posture", _verify)


def decision(level: AutonomyLevel) -> AutonomyDecision:
    return AutonomyDecision(
        level=level,
        source="gh",
        spec_type="feature",
        submitter_class="external",
    )


def make_run(ref: SpecRef, project: Path, *, level: AutonomyLevel) -> FakeRun:
    return FakeRun(
        run_id="run-abc",
        ref=ref,
        project=str(project),
        working_tree=project,
        autonomy=decision(level),
    )


def make_seeder(
    config: ConfigStore,
    accounting: RunAccounting,
    audit: AuditLog,
    opener: FakeOpener,
    *,
    state: FakeState | None = None,
) -> SessionSeeder:
    return SessionSeeder(
        config,
        opener=opener,
        accounting=accounting,
        audit=audit,
        state=state,
    )


def events(audit: AuditLog, ref: SpecRef, name: str) -> list[dict[str, Any]]:
    return [event.to_json_object() for event in audit.read(ref) if event.event == name]


# --- the granted posture is applied ----------------------------------------


class TestPostureApplication:
    def test_the_granted_posture_reaches_the_opener(
        self,
        monkeypatch: pytest.MonkeyPatch,
        config: ConfigStore,
        accounting: RunAccounting,
        audit: AuditLog,
        ref: SpecRef,
        project: Path,
    ) -> None:
        grant(monkeypatch, POSTURE_AUTO)
        opener = FakeOpener()
        result = make_seeder(config, accounting, audit, opener).seed(
            make_run(ref, project, level=AutonomyLevel.AUTHORING)
        )
        (request,) = opener.requests
        assert request.posture == POSTURE_AUTO
        assert request.extra_env == {RESERVED_APPROVAL_ENV: POSTURE_AUTO}
        assert result.applied_posture == POSTURE_AUTO

    def test_the_default_posture_injects_no_approval_env(
        self,
        monkeypatch: pytest.MonkeyPatch,
        config: ConfigStore,
        accounting: RunAccounting,
        audit: AuditLog,
        ref: SpecRef,
        project: Path,
    ) -> None:
        grant(monkeypatch, POSTURE_DEFAULT)
        opener = FakeOpener()
        make_seeder(config, accounting, audit, opener).seed(
            make_run(ref, project, level=AutonomyLevel.AUTHORING)
        )
        (request,) = opener.requests
        assert request.posture == POSTURE_DEFAULT
        # posture_extra_env returns None for the default posture, so nothing that
        # could auto-approve a tool call rides along.
        assert request.extra_env is None

    def test_the_autonomy_ladder_does_not_decide_the_tool_posture(
        self,
        monkeypatch: pytest.MonkeyPatch,
        config: ConfigStore,
        accounting: RunAccounting,
        audit: AuditLog,
        ref: SpecRef,
        project: Path,
    ) -> None:
        """A run the policy authorizes to integrate still runs under the grant.

        The run row's ``posture`` column and the ``autonomy`` decision carry the
        ladder rung; the tool-approval posture is a different authority with a
        different owner. Deriving the tool posture from the rung would be the
        second derivation that grants a session more than the operator did, so an
        integration-level run whose app was granted nothing runs on the default
        posture, not an elevated one.
        """
        grant(monkeypatch, POSTURE_DEFAULT)
        opener = FakeOpener()
        make_seeder(config, accounting, audit, opener).seed(
            make_run(ref, project, level=AutonomyLevel.INTEGRATION)
        )
        (request,) = opener.requests
        assert request.posture == POSTURE_DEFAULT
        assert request.extra_env is None

    def test_seed_takes_no_posture_from_a_caller(self) -> None:
        """There is one input to the posture, and it is not a parameter.

        A posture argument on the seeding entry points would be a second place the
        posture can be set — exactly the shape the module exists to deny — so its
        absence is asserted rather than assumed.
        """
        for entry in (SessionSeeder.seed, SessionSeeder.__call__):
            assert "posture" not in inspect.signature(entry).parameters


# --- the run id is stamped -------------------------------------------------


class TestRunStamping:
    def test_the_session_is_attributed_to_the_run(
        self,
        monkeypatch: pytest.MonkeyPatch,
        config: ConfigStore,
        accounting: RunAccounting,
        audit: AuditLog,
        ref: SpecRef,
        project: Path,
    ) -> None:
        grant(monkeypatch, POSTURE_AUTO)
        opener = FakeOpener()
        result = make_seeder(config, accounting, audit, opener).seed(
            make_run(ref, project, level=AutonomyLevel.AUTHORING)
        )
        assert result.stamped is True
        assert accounting.sessions_for("run-abc") == (result.session_key,)

    def test_a_run_with_no_stamped_session_reports_no_spend(
        self,
        accounting: RunAccounting,
    ) -> None:
        # Sanity anchor for the stamp: an unseeded run owns no session, so the
        # attribution the seeder installs is what makes its turns countable.
        assert accounting.sessions_for("run-abc") == ()


# --- the applied posture is recorded and verified --------------------------


class TestAuditAndVerification:
    def test_the_applied_posture_is_written_to_the_audit_log(
        self,
        monkeypatch: pytest.MonkeyPatch,
        config: ConfigStore,
        accounting: RunAccounting,
        audit: AuditLog,
        ref: SpecRef,
        project: Path,
    ) -> None:
        grant(monkeypatch, POSTURE_AUTO)
        opener = FakeOpener()
        make_seeder(config, accounting, audit, opener).seed(
            make_run(ref, project, level=AutonomyLevel.AUTHORING)
        )
        (seeded,) = events(audit, ref, SESSION_SEEDED_EVENT)
        assert seeded["run"] == "run-abc"
        assert seeded["detail"]["posture"] == POSTURE_AUTO
        assert seeded["detail"]["resolved_posture"] == POSTURE_AUTO

    def test_a_posture_the_operator_did_not_grant_refuses_the_run(
        self,
        monkeypatch: pytest.MonkeyPatch,
        config: ConfigStore,
        accounting: RunAccounting,
        audit: AuditLog,
        ref: SpecRef,
        project: Path,
    ) -> None:
        """A session that came up elevated is refused, recorded, and still stamped.

        The grant is the default posture, but the opened session reports auto —
        an elevation. The run is refused so it cannot proceed on authority the
        operator never conferred, the mismatch is on the record, and the session
        is attributed anyway so any turn it managed to run is still counted.
        """
        grant(monkeypatch, POSTURE_DEFAULT)
        opener = FakeOpener(applied=POSTURE_AUTO)
        seeder = make_seeder(config, accounting, audit, opener)

        with pytest.raises(PostureMismatch) as raised:
            seeder.seed(make_run(ref, project, level=AutonomyLevel.AUTHORING))

        assert raised.value.applied == POSTURE_AUTO
        (mismatch,) = events(audit, ref, POSTURE_MISMATCH_EVENT)
        assert mismatch["detail"]["applied"] == POSTURE_AUTO
        # The applied posture is recorded before the refusal, and the session is
        # attributed so its spend is not invisible.
        (seeded,) = events(audit, ref, SESSION_SEEDED_EVENT)
        assert seeded["detail"]["posture"] == POSTURE_AUTO
        assert accounting.sessions_for("run-abc") == ("chat-1",)

    def test_a_session_more_restrictive_than_the_grant_also_refuses(
        self,
        monkeypatch: pytest.MonkeyPatch,
        config: ConfigStore,
        accounting: RunAccounting,
        audit: AuditLog,
        ref: SpecRef,
        project: Path,
    ) -> None:
        # Auto was granted but the session came up on the default posture: an
        # unattended run there does not fail loudly, it stalls forever on a prompt
        # nobody answers, so it is refused rather than started.
        grant(monkeypatch, POSTURE_AUTO)
        opener = FakeOpener(applied=POSTURE_DEFAULT)
        seeder = make_seeder(config, accounting, audit, opener)
        with pytest.raises(PostureMismatch):
            seeder.seed(make_run(ref, project, level=AutonomyLevel.AUTHORING))


# --- a run parked for review is announced ----------------------------------


class TestAwaitingReviewNotice:
    def test_a_human_reserved_gate_reaches_the_configured_channel(
        self,
        config: ConfigStore,
        accounting: RunAccounting,
        audit: AuditLog,
        ref: SpecRef,
    ) -> None:
        config.write(
            {"projects": {PROJECT: {"path": "/w/acme", "notify": {"channel": REVIEW_CHANNEL}}}},
            surface=DASHBOARD_SURFACE,
        )
        bus = FakeBus()
        seeder = make_seeder(config, accounting, audit, FakeOpener(), state=FakeState(bus))

        notice = seeder.notify_awaiting_review(ref, "run-abc", project=PROJECT, gate="design")

        assert notice.delivered is True
        assert notice.channel == f"{APP_NAME}.{REVIEW_CHANNEL}"
        (payload,) = bus.pushed
        assert payload.channel == f"{APP_NAME}.{REVIEW_CHANNEL}"
        assert "waiting" in payload.body.lower()
        (recorded,) = events(audit, ref, AWAITING_REVIEW_EVENT)
        assert recorded["detail"]["gate"] == "design"

    def test_a_run_with_no_configured_channel_lands_on_the_dashboard(
        self,
        config: ConfigStore,
        accounting: RunAccounting,
        audit: AuditLog,
        ref: SpecRef,
    ) -> None:
        # No channel configured for the project: the notice still reaches a
        # person, on the one channel every install has.
        bus = FakeBus()
        seeder = make_seeder(config, accounting, audit, FakeOpener(), state=FakeState(bus))

        notice = seeder.notify_awaiting_review(ref, "run-abc", project=PROJECT)

        assert notice.delivered is True
        assert notice.channel == f"{APP_NAME}.{DASHBOARD_CHANNEL}"
        (payload,) = bus.pushed
        assert payload.channel == f"{APP_NAME}.{DASHBOARD_CHANNEL}"

    def test_an_undelivered_notice_is_recorded_not_raised(
        self,
        config: ConfigStore,
        accounting: RunAccounting,
        audit: AuditLog,
        ref: SpecRef,
    ) -> None:
        # No bus in this process (no gateway state): the notice cannot be
        # delivered, but the run is already parked and a lost courtesy must not
        # unwind it, so the failure is recorded and returned.
        seeder = make_seeder(config, accounting, audit, FakeOpener(), state=None)

        notice = seeder.notify_awaiting_review(ref, "run-abc", project=PROJECT)

        assert notice.delivered is False
        assert notice.error
        assert events(audit, ref, AWAITING_REVIEW_NOTIFY_FAILED_EVENT)
        assert not events(audit, ref, AWAITING_REVIEW_EVENT)

    def test_only_a_human_reserved_run_is_a_review_run(
        self,
        ref: SpecRef,
        project: Path,
    ) -> None:
        # The predicate that decides when the announcement is due reads the run's
        # own resolved decision: an authoring-only run parks for a person, an
        # execution-authorized run proceeds without one.
        assert reserves_human_review(make_run(ref, project, level=AutonomyLevel.AUTHORING))
        assert not reserves_human_review(make_run(ref, project, level=AutonomyLevel.EXECUTION))


# --- the seeder is the dispatcher's RunStarter -----------------------------


class TestRunStarterSeam:
    def test_a_real_run_seed_drives_the_seeder(
        self,
        monkeypatch: pytest.MonkeyPatch,
        config: ConfigStore,
        accounting: RunAccounting,
        audit: AuditLog,
        ref: SpecRef,
        project: Path,
    ) -> None:
        """The watcher's own ``RunSeed`` seeds through the ``RunStarter`` seam.

        Proves the seeder accepts what the dispatcher hands a ``RunStarter`` — the
        real ``RunSeed`` — rather than only the test's stand-in, so the structural
        ``SeededRun`` protocol is not narrower than the production type.
        """
        from kiro_crew.apps.builtins.spec_engine.engine.config import LEAST_TRUSTED_CLASS
        from kiro_crew.apps.builtins.spec_engine.engine.watch.dispatch import (
            ClassEvidence,
            RunSeed,
            SubmitterClass,
        )
        from kiro_crew.apps.builtins.spec_engine.engine.watch.items import WatchedItem

        grant(monkeypatch, POSTURE_AUTO)
        opener = FakeOpener()
        seeder = make_seeder(config, accounting, audit, opener)

        seed = RunSeed(
            run_id="run-seed",
            ref=ref,
            working_tree=project,
            project=str(project),
            base_branch="main",
            spec_type="feature",
            source="gh",
            item=WatchedItem(source="gh", identifier="7", title="a bug"),
            generation=1,
            submitter_class=SubmitterClass(LEAST_TRUSTED_CLASS, ClassEvidence.UNDETERMINED),
            autonomy=decision(AutonomyLevel.AUTHORING),
        )

        # The real RunSeed satisfies the structural SeededRun the seeder reads, so
        # the protocol is not narrower than the production type the dispatcher
        # hands a RunStarter.
        assert isinstance(seed, SeededRun)
        # Called the way the dispatcher calls a RunStarter: start(seed).
        seeder(seed)
        (request,) = opener.requests
        assert request.run_id == "run-seed"
        assert request.name == session_name(seed)
        assert accounting.sessions_for("run-seed") == ("chat-1",)


def test_the_stand_ins_satisfy_their_protocols() -> None:
    """The fakes are the protocols the seams name, checked structurally.

    A stand-in that drifted from the protocol would make every test above a test
    of a shape the production seam does not have, so conformance is asserted
    directly rather than trusted.
    """
    assert isinstance(FakeOpenedSession(session_key="s", applied_posture=""), OpenedSession)
    run = FakeRun(
        run_id="r",
        ref=SpecRef(project="/w/acme", name="example"),
        project="/w/acme",
        working_tree=Path("/w/acme"),
        autonomy=AutonomyDecision(
            level=AutonomyLevel.AUTHORING,
            source=None,
            spec_type="feature",
            submitter_class="external",
        ),
    )
    assert isinstance(run, SeededRun)
