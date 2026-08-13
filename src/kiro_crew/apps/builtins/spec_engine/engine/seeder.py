"""The headless run driver: turn a resolved run into an ordinary agent session.

A dispatched run exists as a spec on disk and a run row in the state store, but
nothing is working on it until a session is opened. This module is that step,
and it owns exactly the guarantees that make an unattended session safe to leave
running:

**The tool-approval posture is the engine's own resolution, applied once.** The
posture a seeded session runs under is ``apps.approval_grants.session_posture``:
the intersection of what this app's manifest declares and what the operator
granted in the keystone grant file. It is resolved HERE from that one source and
never taken from a caller, from configuration this module reads, or from the run
row — the run row's ``posture`` column holds the *autonomy level* (how far the
run may go), which is a different question with a different owner, and reading it
for the tool-approval decision would be a second derivation of an authority
question. A second derivation is how a session comes up with more approval than
the operator conferred, so there is exactly one: :func:`session_posture`, passed
straight to the opener and then re-checked against the session that came back.

**The session is stamped with the run identifier.** Every session the engine
opens for a run is attributed to it in the claim ledger, so the per-turn metering
records the session produces sum into the run's spend and the budget ceiling can
see them. The stamp is part of seeding, not a step a caller may skip: a session
that ran unattributed is spend the ceiling never counts.

**The applied posture is verified and recorded.** The posture the opened session
actually came up under is checked against the grant with
:func:`verify_session_posture`; a mismatch — in either direction — refuses the
run rather than letting it proceed elevated or stall forever on a prompt nobody
will answer. Whatever posture was applied is written to the run's audit log, so
what an unattended session was allowed to do is reconstructable after the fact.

**A run parked on a person is announced.** When a headless run reaches a gate the
Autonomy_Policy reserves for human action, the configured notification channel is
told it is waiting for review, defaulting to the dashboard when a project named
none. Delivery is best-effort in the same sense the run lifecycle's stall notice
is: the run's state is primary, and a channel outage is recorded rather than
allowed to unwind it.

This module opens the session through an injected :class:`SessionOpener` seam
rather than importing the host's session manager: the host owns session creation
(which is what makes a seeded run appear in the dashboard session list like any
chat), and the seam is what lets every guarantee above be tested without a
gateway. See :class:`SessionSeeder` for who is expected to construct it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Mapping, Protocol, runtime_checkable

from kiro_crew.apps.approval_grants import (
    posture_extra_env,
    session_posture,
    verify_session_posture,
)

from .audit import AuditLog
from .autonomy import AutonomyDecision
from .budget import RunAccounting
from .config import ConfigStore
from .config.store import APP_NAME
from .notify import HostNotifier, NotificationUndelivered
from .state import SpecRef

if TYPE_CHECKING:
    # For the RunStarter seam only: typing __call__ against the watcher's own
    # RunSeed is what makes a SessionSeeder statically assignable to the
    # dispatcher's `start: RunStarter`. RunSeed satisfies the structural SeededRun
    # that seed() reads, so no behaviour depends on this import; it is under
    # TYPE_CHECKING so the runtime module stays decoupled from the watcher.
    from .watch.dispatch import RunSeed

logger = logging.getLogger(__name__)

# --- Audit event names -----------------------------------------------------

#: Recorded when a session is opened for a run, carrying the posture that was
#: applied to it. This is the record requirement 7.3 asks for: the applied
#: approval posture in the run's audit log, written whatever the posture is.
SESSION_SEEDED_EVENT = "spec.run.session-seeded"

#: Recorded when the posture the opened session came up under does not match the
#: posture granted in configuration. Both an elevation and an under-grant record
#: here and refuse the run.
POSTURE_MISMATCH_EVENT = "spec.run.posture-mismatch"

#: Recorded when a run parked at a human-reserved gate was announced to a channel.
AWAITING_REVIEW_EVENT = "spec.run.awaiting-review"

#: Recorded when that announcement could not be delivered. The run stays where it
#: is: the notice is a courtesy, not the state.
AWAITING_REVIEW_NOTIFY_FAILED_EVENT = "spec.run.awaiting-review-notification-failed"

#: Title of the "waiting for review" notice. The body carries the specifics.
REVIEW_NOTICE_TITLE = "Spec run waiting for review"

#: Prefix on the seeded session's name, so a headless run is recognisable in the
#: dashboard session list beside interactive chats.
SESSION_NAME_PREFIX = "spec"


@runtime_checkable
class SeededRun(Protocol):
    """What the seeder reads off a resolved run.

    Structural rather than an import of the watcher's ``RunSeed`` so the seeder
    stays usable by any producer of a run and satisfies the dispatcher's
    ``RunStarter`` seam without depending on it: ``RunSeed`` carries every member
    named here, so a :class:`SessionSeeder` is a valid ``RunStarter``.
    """

    @property
    def run_id(self) -> str: ...

    @property
    def ref(self) -> SpecRef: ...

    @property
    def project(self) -> str: ...

    @property
    def working_tree(self) -> Path: ...

    @property
    def autonomy(self) -> AutonomyDecision: ...

    def seed_text(self) -> str: ...


@dataclass(frozen=True)
class SessionRequest:
    """What the opener needs to bring up one ordinary agent session.

    ``extra_env`` already carries the reserved approval control variable when the
    posture is auto (built by :func:`posture_extra_env`, the one re-injection
    point), and ``posture`` names what the host is expected to apply, so a mismatch
    between the two is detectable when the opened session reports back.
    """

    run_id: str
    project: str
    working_tree: Path
    name: str
    prompt: str
    posture: str
    extra_env: Mapping[str, str] | None = None


@runtime_checkable
class OpenedSession(Protocol):
    """The slice of a newly opened session the seeder reads.

    ``applied_posture`` is what the session actually came up under, which the
    seeder checks against the grant rather than assuming the opener honoured the
    request. Declared as read-only properties because a session handle is not
    something this module mutates.
    """

    @property
    def session_key(self) -> str: ...

    @property
    def applied_posture(self) -> str: ...


class SessionOpener(Protocol):
    """Opens an ordinary agent session for a run and reports what it applied.

    A seam rather than an import of the host session manager: the host owns
    session creation, and only a real host session appears in the dashboard
    session list, but every guarantee this module makes about that session
    (stamping, posture verification, audit) is testable without a gateway when
    the opener is injected. The opener must NOT run the session's first turn — the
    posture is verified before the run is allowed to proceed, so a turn started
    inside ``open`` would run before the check that could refuse it.
    """

    def __call__(self, request: SessionRequest) -> OpenedSession: ...


class PostureMismatch(RuntimeError):
    """The opened session's posture does not match what the operator granted.

    Raised so the caller refuses the run rather than letting a session that came
    up more permissive than the grant proceed, or one that came up more
    restrictive stall forever on an approval prompt no one will answer.
    """

    def __init__(self, run_id: str, applied: str, reason: str) -> None:
        super().__init__(reason)
        self.run_id = run_id
        self.applied = applied
        self.reason = reason


@dataclass(frozen=True)
class SeedResult:
    """The outcome of seeding one run's session."""

    run_id: str
    session_key: str
    applied_posture: str
    #: Whether the run's resolved policy reserves execution for a human, which is
    #: what makes it a run that will park for review rather than proceed.
    execution_human_reserved: bool
    #: Whether this call created the session's run stamp (False when it was
    #: already stamped, which a resume re-seeding the same session produces).
    stamped: bool


@dataclass(frozen=True)
class ReviewNotice:
    """The outcome of announcing that a run is waiting for review."""

    run_id: str
    delivered: bool
    channel: str = ""
    error: str = ""


def session_name(run: SeededRun) -> str:
    """A stable, run-identifying name for the seeded session.

    Carries the run id so a headless session is traceable to its run in the
    dashboard session list, and the spec name so a person reading the list sees
    what it is working on.
    """
    return f"{SESSION_NAME_PREFIX}:{run.ref.name}:{run.run_id}"


class SessionSeeder:
    """Seeds ordinary agent sessions for headless runs.

    Constructed once and used as the dispatcher's ``RunStarter`` (its
    :meth:`__call__` accepts a ``RunSeed``). It holds the seams every guarantee
    needs: the :class:`SessionOpener` that creates the host session, the
    :class:`~.budget.RunAccounting` that stamps the run onto it, the
    :class:`~.audit.AuditLog` that records the applied posture, and the config and
    optional gateway state a :class:`~.notify.HostNotifier` is built from per
    project when a run parks for review.

    The gateway ``state`` is threaded rather than reached for globally, so the
    notification path is testable with a fake bus and absent outside the gateway
    process (a CLI or a test), which the notifier treats as a delivery condition
    rather than a misconfiguration.
    """

    def __init__(
        self,
        config: ConfigStore,
        *,
        opener: SessionOpener,
        accounting: RunAccounting,
        audit: AuditLog,
        state: object | None = None,
        app_name: str = APP_NAME,
    ) -> None:
        self._config = config
        self._opener = opener
        self._accounting = accounting
        self._audit = audit
        self._state = state
        self._app_name = app_name

    # ------------------------------------------------------------------ seed

    def __call__(self, seed: "RunSeed") -> None:
        """Satisfy the dispatcher's ``RunStarter`` seam.

        The parameter is named and typed to match ``RunStarter.__call__`` so a
        ``SessionSeeder`` is statically assignable to a ``start: RunStarter``; the
        body needs only what :class:`SeededRun` names, which ``RunSeed`` carries.
        """
        self.seed(seed)

    def seed(self, run: SeededRun) -> SeedResult:
        """Open a session for *run*, stamp it, verify the posture, and record it.

        The posture is resolved once from :func:`session_posture` and never taken
        from *run* or a caller. The session is opened with that posture applied,
        the run is stamped onto the session so its turns are attributable, the
        applied posture is written to the audit log, and a mismatch against the
        grant raises :class:`PostureMismatch` after recording it.
        """
        posture = session_posture(self._app_name)
        request = SessionRequest(
            run_id=run.run_id,
            project=run.project,
            working_tree=run.working_tree,
            name=session_name(run),
            prompt=run.seed_text(),
            posture=posture,
            extra_env=posture_extra_env(posture),
        )
        opened = self._opener(request)
        applied = opened.applied_posture
        session_key = opened.session_key

        # Stamp before proceeding: a session that ran even one turn unattributed
        # is spend the ceiling never sees, so it is attributed the moment it
        # exists, before the posture check that may refuse the run.
        stamped = self._accounting.stamp(run.run_id, session_key)

        self._audit.append(
            run.ref,
            SESSION_SEEDED_EVENT,
            run=run.run_id,
            initiator=self._app_name,
            detail={
                "session": session_key,
                "posture": applied,
                "resolved_posture": posture,
                "stamped": stamped,
                "autonomy": run.autonomy.level.value,
                "execution_human_reserved": run.autonomy.execution_is_human_reserved,
            },
        )

        reason = verify_session_posture(self._app_name, applied)
        if reason is not None:
            self._audit.append(
                run.ref,
                POSTURE_MISMATCH_EVENT,
                run=run.run_id,
                initiator=self._app_name,
                detail={"session": session_key, "applied": applied, "reason": reason},
            )
            raise PostureMismatch(run.run_id, applied, reason)

        return SeedResult(
            run_id=run.run_id,
            session_key=session_key,
            applied_posture=applied,
            execution_human_reserved=run.autonomy.execution_is_human_reserved,
            stamped=stamped,
        )

    # ---------------------------------------------------------- review notice

    def notify_awaiting_review(
        self,
        ref: SpecRef,
        run_id: str,
        *,
        project: str | None = None,
        gate: str = "",
    ) -> ReviewNotice:
        """Announce that *run_id* is parked at a human-reserved gate.

        Called when a headless run reaches a gate the Autonomy_Policy reserves for
        human action. The channel is resolved per project from configuration and
        defaults to the dashboard when a project named none; delivery is
        best-effort, so a channel outage is recorded and returned rather than
        raised — the run is already parked and a lost notice does not change that.
        """
        notifier = HostNotifier(self._config, project=project, state=self._state)
        body = _review_body(run_id, ref.name, gate)
        try:
            delivery = notifier.send(REVIEW_NOTICE_TITLE, body, group_key=run_id)
        except NotificationUndelivered as exc:
            logger.warning("awaiting-review notice for run %s was not delivered: %s", run_id, exc)
            self._audit.append(
                ref,
                AWAITING_REVIEW_NOTIFY_FAILED_EVENT,
                run=run_id,
                initiator=self._app_name,
                detail={"gate": gate, "error": str(exc)},
            )
            return ReviewNotice(run_id=run_id, delivered=False, error=str(exc))
        self._audit.append(
            ref,
            AWAITING_REVIEW_EVENT,
            run=run_id,
            initiator=self._app_name,
            detail={"channel": delivery.channel, "gate": gate},
        )
        return ReviewNotice(run_id=run_id, delivered=True, channel=delivery.channel)


def reserves_human_review(run: SeededRun) -> bool:
    """Whether *run*'s resolved policy reserves execution for a human.

    A run whose policy reserves execution will author its documents and then park
    for a person, so it is the run whose arrival at the gate is announced; a run
    the policy authorizes to execute proceeds without one. Reads the run's own
    resolved decision rather than re-resolving the policy, so the "when to
    announce" answer cannot disagree with the "how far may it go" answer the
    engine already gave.
    """
    return run.autonomy.execution_is_human_reserved


def _review_body(run_id: str, spec: str, gate: str) -> str:
    """The human-readable body of a waiting-for-review notice.

    Composed only of engine-held values — the run id, the spec name, the gate —
    never of item text, which is attacker-controlled on a public tracker and has
    no place in a notice this module authors.
    """
    at_gate = f" at the {gate} gate" if gate.strip() else ""
    return (
        f"Spec run {run_id} ({spec}) has reached a gate reserved for human review"
        f"{at_gate}. It is waiting for an approve or request-changes decision; "
        "nothing will proceed until a person acts."
    )
