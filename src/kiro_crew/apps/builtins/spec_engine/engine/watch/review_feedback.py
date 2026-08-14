"""The review-feedback watcher: reviewer comments that become fix work.

After a run submits its change, a person comments on the review artifact. This
module polls for those comments and turns new ones into a revision the delivery
pipeline carries through its configured stages. That makes a **comment** a thing
that spends model credits and edits code, which is the most attacker-reachable
surface the engine has: a comment is written by whoever can reach the review, and
a tracker's review artifact is usually reachable by more people than its
maintainer list.

Five properties hold that surface, and each is enforced here rather than asked
for:

**Off unless a project armed it.** The watcher polls nothing until
``delivery.review_feedback_enabled`` is set for the project *itself* and that
project carries a ``review_feedback.poll`` command. An app-scope switch does not
arm any project, because requirement 23.2 asks for explicit per-project
enablement and a single app-level flip is the opposite of one. Both halves live
under :data:`~..config.schema.CONFIG_ONLY_PATHS`, so no tool call can arm a
project or widen who may drive it.

**Zero model credits while idle.** A poll is a configured argv list, run with no
shell, whose output is JSON-decoded (which evaluates nothing) and read by fixed
keys. Nothing on that path reaches a model, so a tick that finds no new comment
spends nothing at all -- not a cheap turn, none. This is the same shape watch
polling has, and it reuses that module's decoder and its health vocabulary rather
than growing a second one.

**The class that gates a dispatch is the commenter's own.** Each comment is one
:class:`~..trust.ContentElement` classified from *its own* author by
:func:`~..trust.reconcile`, never from the item, the run, or the artifact it sits
on. Trusting a comment because a maintainer opened the item is the container-to-element
inheritance the trust module exists to prevent. A comment edited after it was
classified is re-derived rather than remembered: the claim ledger keys on the
content revision, so a new revision is a new claim, and text is reached through
:func:`~..trust.consume`, which refuses a revision the class was not derived for.

**A class that may not dispatch is quarantined, and quarantining costs nothing.**
The permission check happens before anything that spends -- before screening,
which runs a model turn, and before the fix dispatch. A refused comment is
recorded on the run so the Review_Queue surfaces it, and it stays refused until a
person releases it, which drops its claim so the next poll re-derives it.

**Both bounds stop the loop, and needs-human is the terminal state.** A cycle is
refused when the run has spent its configured revision cycles or when its budget
ceiling is reached; either bound marks the run as needing human attention and
notifies, rather than failing silently or dispatching again. A comment thread that
could re-dispatch forever is a credit-exhaustion bug whose trigger an attacker
holds.

The revision itself runs through :class:`~..delivery.flow.DeliveryPipeline` --
the one entry point an autonomous run and a human request both use -- so a fix
dispatched from a comment runs the project's configured stages, gates, retry
ceiling and integration floor, and there is no second way to run a stage command
here.

Nothing in production constructs :class:`ReviewFeedbackWatcher` yet: the engine's
composition root owns which stores, screener, reviser and pipeline a watcher is
built from, and the tick that would call it belongs to the same wiring task. The
mechanism lands ahead of its caller for the reason the echo gate did -- a gate no
caller consults still has to exist and be correct before one does, and folding it
into the wiring would leave the decisions here untested until then.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from ..audit import AuditLog
from ..config import ConfigStore
from ..config.effective import ValueOrigin
from ..config.schema import (
    LEAST_TRUSTED_CLASS,
    REVIEW_FEEDBACK_FIELD,
    SECTION_PROJECTS,
    SUBMITTER_CLASSES,
    WILDCARD_KEY,
    ConfigError,
    ConfigValidationError,
)
from ..delivery.stages import CommandOutcome, CommandRunner, run_argv
from ..delivery.templates import CommandTemplate, TemplateError
from ..delivery.variables import RunContext, build_variables
from ..runs import (
    DETAIL_FEEDBACK_CYCLES,
    DETAIL_FEEDBACK_FAILURES,
    DETAIL_FEEDBACK_NEEDS_HUMAN,
    DETAIL_FEEDBACK_QUARANTINED,
    feedback_cycles,
    feedback_failures,
    feedback_quarantined,
)
from ..state import SpecRef, StateStore
from .poll import HealthReason, PollStatus, decode_entries
from .screening import ScreeningReport

if TYPE_CHECKING:
    # Annotations only. ``trust`` imports ``watch.dispatch``, so importing it at
    # module load would re-enter a partially initialized ``trust`` through the
    # watch package's ``__init__``; the runtime needs are imported inside the
    # functions that use them, the same way ``watch.screening`` does.
    from ..delivery.flow import DeliveryRun
    from ..trust import ContentElement, ElementTrust
    from .dispatch import SourceRoute

logger = logging.getLogger(__name__)

__all__ = [
    "AUDIT_REVIEW_FEEDBACK",
    "AUDIT_REVIEW_FEEDBACK_BOUND",
    "AUDIT_REVIEW_FEEDBACK_RELEASED",
    "CLAIM_REVIEW_COMMENT",
    "COMMENT_FIELDS",
    "CYCLE_LIMIT_SETTING",
    "DISPATCH_FIELD",
    "ENABLED_SETTING",
    "CommentPoll",
    "CommentScreener",
    "ReviewFeedbackBound",
    "CommentDisposition",
    "FeedbackNotifier",
    "ReviewFeedbackOutcome",
    "FeedbackReviser",
    "FeedbackRevision",
    "ReviewFeedbackTick",
    "RevisionDelivery",
    "ReviewComment",
    "ReviewFeedbackWatch",
    "ReviewFeedbackWatcher",
    "dispatch_permitted_for",
    "load_watch",
    "poll_comments",
    "release_quarantined_comment",
    "review_feedback_enabled",
]


#: Claim kind for one reviewer comment revision on one run. Keyed by the run and
#: the comment's identifier, with the content revision as the generation, so an
#: edited comment is a claim of its own rather than one already taken -- which is
#: what makes a re-derivation happen instead of the old class being remembered.
CLAIM_REVIEW_COMMENT = "review_comment"

#: The per-project switch that arms the watcher. Read at project scope only.
ENABLED_SETTING = "delivery.review_feedback_enabled"

#: The per-submitter-class permission to drive a fix dispatch, inside the
#: project's ``review_feedback`` container.
DISPATCH_FIELD = "dispatch"

#: The poll command inside the ``review_feedback`` container.
POLL_FIELD = "poll"

#: How long a review-artifact poll may run, in seconds, inside the container.
TIMEOUT_FIELD = "timeout_s"

#: Bundled poll timeout. A review-artifact read is one API call; a poll that
#: hangs longer than this is a broken client, not a slow one.
DEFAULT_TIMEOUT_S = 60

#: The revision-cycle limit the watcher bounds feedback cycles by. The same
#: setting the spec-review revision loop counts against, because both are
#: "authoring rounds this run may spend answering a reviewer" and two limits
#: would let one loop be widened without the other.
CYCLE_LIMIT_SETTING = "limits.revision_cycle_limit"

#: Fields one reviewer comment yields, in reporting order. Read from these exact
#: keys: the poll command shapes its own output (every tracker client can, with
#: its own query language), so the engine has no field-map layer here and no
#: second mapping mechanism.
COMMENT_FIELDS: tuple[str, ...] = ("identifier", "author", "association", "body", "revision")

#: Fields that must resolve for a comment to be usable. Without an identifier a
#: comment cannot be claimed at most once -- it could only be dispatched every
#: poll or never.
REQUIRED_COMMENT_FIELDS: tuple[str, ...] = ("identifier",)

#: Audit event for one comment's disposition: dispatched, quarantined, or bounded.
AUDIT_REVIEW_FEEDBACK = "review.feedback"

#: Audit event for a run marked needing human attention on a feedback bound.
AUDIT_REVIEW_FEEDBACK_BOUND = "review.feedback.needs-human"

#: Audit event for a quarantined comment released by a person.
AUDIT_REVIEW_FEEDBACK_RELEASED = "review.feedback.released"

#: Rejections kept per poll. Enough to recognize a mismatched command, bounded so
#: a whole review thread cannot be held in one outcome.
MAX_RECORDED_PROBLEMS = 10


class ReviewFeedbackOutcome(str, Enum):
    """What became of one reviewer comment."""

    #: A revision was dispatched and carried through the delivery stages.
    DISPATCHED = "dispatched"
    #: Already claimed at this revision: seen on an earlier tick, not new.
    ALREADY_SEEN = "already_seen"
    #: The commenter's own class is not permitted to drive a dispatch. Held for
    #: human release, at no model cost.
    QUARANTINED = "quarantined"
    #: Screening suspected an embedded instruction, or could not produce a
    #: verdict. Held for human release.
    SCREENED_OUT = "screened_out"
    #: A bound was reached, so the run was marked needing human attention.
    BOUNDED = "bounded"
    #: The dispatch was attempted and the host could not start it. The cycle is
    #: not counted, so the next tick tries again.
    FAILED = "failed"


class ReviewFeedbackBound(str, Enum):
    """Which bound stopped a feedback cycle."""

    #: The configured revision-cycle limit for this run.
    CYCLE_LIMIT = "cycle_limit"
    #: The run's budget ceiling, or the kill switch, as the guard reported it.
    BUDGET = "budget"


@dataclass(frozen=True)
class ReviewComment:
    """One reviewer comment on a review artifact, with the author of *that* text.

    *author* is this comment's own author, never the item's submitter or the
    artifact's owner. *association* is what the tracker asserts about this author,
    per comment for the same reason. *revision* is the tracker's own revision when
    it reports one; absent, the element digests its text, so an edit is still
    detectable.
    """

    identifier: str
    author: str = ""
    association: str = ""
    body: str = ""
    revision: str = ""

    def __post_init__(self) -> None:
        if not self.identifier.strip():
            raise ValueError("a reviewer comment must carry a non-blank identifier")

    def element(self) -> ContentElement:
        """This comment as a content element, for classification and screening."""
        from ..trust import ContentElement, ElementKind

        return ContentElement(
            kind=ElementKind.REVIEW_COMMENT,
            element_id=self.identifier,
            author=self.author,
            association=self.association,
            text=self.body,
            revision=self.revision,
        )


@dataclass(frozen=True)
class RejectedComment:
    """One output entry that could not become a comment, and why."""

    index: int
    reason: str


@dataclass(frozen=True)
class CommentPoll:
    """What one poll of one run's review artifact produced.

    The invariants mirror :class:`~.poll.PollOutcome`'s, for the same reason: a
    review artifact that cannot be read must never report an empty comment list,
    or a broken client would look exactly like a reviewer who said nothing.
    """

    run_id: str
    status: PollStatus
    comments: tuple[ReviewComment, ...] = ()
    reason: HealthReason | None = None
    detail: str = ""
    program: str = ""
    exit_code: int | None = None
    rejected: tuple[RejectedComment, ...] = ()
    duration_s: float = 0.0

    def __post_init__(self) -> None:
        if self.status is PollStatus.UNHEALTHY:
            if self.reason is None:
                raise ValueError("an unhealthy poll outcome must carry a reason")
            if not self.detail.strip():
                raise ValueError("an unhealthy poll outcome must explain itself")
            if self.comments:
                raise ValueError("an unhealthy poll outcome must not report comments")
        elif self.reason is not None:
            raise ValueError("only an unhealthy poll outcome carries a reason")

    @property
    def healthy(self) -> bool:
        return self.status is PollStatus.OK

    @property
    def found_no_comments(self) -> bool:
        """Whether the artifact genuinely carries no comments.

        Answerable only by a poll that ran and parsed, so no caller reaches
        "nothing to do" through a broken client or a disarmed project.
        """
        return self.status is PollStatus.OK and not self.comments

    def describe(self) -> str:
        if self.status is PollStatus.DISABLED:
            return f"{self.run_id}: review feedback is not enabled"
        if self.status is PollStatus.OK:
            counted = f"{len(self.comments)} comment(s)"
            if self.rejected:
                counted += f", {len(self.rejected)} unreadable"
            return f"{self.run_id}: polled, {counted}"
        named = self.reason.value if self.reason is not None else "unknown"
        return f"{self.run_id}: unhealthy ({named}) — {self.detail}"


@dataclass(frozen=True)
class ReviewFeedbackWatch:
    """A project's armed review-feedback configuration.

    Built only for a project that both enabled the switch at its own scope and
    configured a poll command. There is no half-armed state: a project with a
    command and no switch polls nothing, and a switch with no command is a
    configuration error the loader reports rather than an empty comment list.
    """

    project: str
    poll: CommandTemplate
    timeout_s: int = DEFAULT_TIMEOUT_S

    @property
    def program(self) -> str:
        return self.poll.program


@dataclass(frozen=True)
class FeedbackRevision:
    """Everything a host needs to author one fix round from a reviewer comment.

    The comment travels as data: :attr:`quoted_comment` is the text, already
    consumed under the class derived for that revision, and the host fences it the
    way the spec-review revision request does. The engine resolves identity, the
    cycle number, and the class the dispatch was gated on; the host owns the turn.
    """

    run_id: str
    ref: SpecRef
    project: str
    #: 1-based cycle number this round is, for the audit trail and the turn.
    cycle: int
    #: The comment's identifier and the revision the class was derived for.
    comment_id: str
    content_revision: str
    #: The commenter's own submitter class, which permitted this dispatch.
    submitter_class: str
    quoted_comment: str


class FeedbackReviser(Protocol):
    """Authors the fix for one reviewer comment, before delivery re-runs.

    A host seam like the dispatcher's ``RunStarter`` and the review queue's
    ``SpecReviser``: the engine owns which comments become work, under whose
    class, and how the round is bounded and recorded; the host owns the turn. It
    raises to say the round could not be started, which leaves the cycle
    uncounted so the next tick tries again.
    """

    def __call__(self, revision: FeedbackRevision) -> None: ...


class RevisionDelivery(Protocol):
    """The delivery pipeline, narrowed to the one call this module makes.

    Structural rather than an import of :class:`~..delivery.flow.DeliveryPipeline`
    so a test can supply its own, and required rather than optional so there is no
    configuration in which a comment-driven fix skips the stages. This module runs
    no stage command itself: the pipeline resolves the workflow, the gates, the
    verify retry ceiling and the integration floor, and a second way to run a
    stage command here would be a way around all four.
    """

    def deliver(self, context: RunContext, *, requester: str | None = ...) -> DeliveryRun: ...


class FeedbackNotifier(Protocol):
    """The slice of the host notifier a bound or a quarantine notice uses."""

    def send(
        self,
        title: str,
        body: str = ...,
        *,
        quoted: str = ...,
        detail: Mapping[str, Any] | None = ...,
    ) -> Any: ...


class SpendGuard(Protocol):
    """The budget guard, narrowed to the question this module asks it.

    Satisfied by :class:`~..budget.ceiling.BudgetGuard`. Asking it is what applies
    the ceiling: the guard parks the run and notifies when the answer is no, so a
    caller cannot read the number and forget to act on it.
    """

    def authorize_dispatch(self) -> Any: ...


@dataclass(frozen=True)
class CommentDisposition:
    """What one comment produced, and what it cost."""

    comment_id: str
    outcome: ReviewFeedbackOutcome
    submitter_class: str = ""
    content_revision: str = ""
    cycle: int = 0
    bound: ReviewFeedbackBound | None = None
    detail: str = ""
    #: True when a model turn was dispatched for this comment. Read by the tick's
    #: own accounting and by the tests that pin the zero-credit guarantee.
    spent: bool = False

    @property
    def dispatched(self) -> bool:
        return self.outcome is ReviewFeedbackOutcome.DISPATCHED


@dataclass(frozen=True)
class ReviewFeedbackTick:
    """One watcher pass over one run's review artifact."""

    run_id: str
    poll: CommentPoll
    dispositions: tuple[CommentDisposition, ...] = ()

    @property
    def dispatched(self) -> tuple[CommentDisposition, ...]:
        return tuple(d for d in self.dispositions if d.dispatched)

    @property
    def quarantined(self) -> tuple[CommentDisposition, ...]:
        return tuple(d for d in self.dispositions if d.outcome is ReviewFeedbackOutcome.QUARANTINED)

    @property
    def bounded(self) -> tuple[CommentDisposition, ...]:
        return tuple(d for d in self.dispositions if d.outcome is ReviewFeedbackOutcome.BOUNDED)

    @property
    def idle(self) -> bool:
        """Whether this tick found nothing to act on.

        True for a poll that ran and produced no new comment, which is the case
        the zero-credit guarantee is about. An unhealthy poll is not idle: it
        found out nothing, and reporting it as idle is the failure the poll
        outcome's invariants exist to prevent.
        """
        return self.poll.healthy and not any(
            d.outcome
            not in (ReviewFeedbackOutcome.ALREADY_SEEN,)
            for d in self.dispositions
        )


# --- configuration ---------------------------------------------------------


def review_feedback_enabled(config: ConfigStore, project: str) -> bool:
    """Whether *project* itself armed the review-feedback watcher.

    Off by default, and armed only by a value written at the project's own scope.
    An app-scope ``true`` deliberately does not arm anything: requirement 23.2
    asks for explicit per-project enablement, and one app-level switch that armed
    every project at once is what that forbids. The switch sits under
    :data:`~..config.schema.CONFIG_ONLY_PATHS`, so no tool call can flip it.
    """
    if not project.strip():
        return False
    value = config.effective(ENABLED_SETTING, project=project)
    if value.origin is not ValueOrigin.PROJECT_CONFIG:
        return False
    return value.value is True


def dispatch_permitted_for(config: ConfigStore, project: str, submitter_class: str) -> bool:
    """Whether a comment of *submitter_class* may drive a fix dispatch on *project*.

    Refused by default. Permitted only where
    ``projects.<project>.review_feedback.dispatch.<class>`` is explicitly ``true``
    -- and never for the least-trusted class, whatever configuration says, because
    the floor must not be reachable by editing one map entry. The floor comes from
    the class ordering rather than a spelled name, so a class added below the
    current bottom is still refused.

    A wildcard key is not honoured: it is never a class a comment resolves to, and
    reading it would let one entry hand every class a channel that spends the
    run's budget. Read from the raw document because the map is per class rather
    than a scalar setting; the container is config-only, so no tool can widen it.
    """
    if submitter_class == LEAST_TRUSTED_CLASS or submitter_class == WILDCARD_KEY:
        return False
    if submitter_class not in SUBMITTER_CLASSES:
        return False
    entry = _container(config, project)
    if entry is None:
        return False
    permissions = entry.get(DISPATCH_FIELD)
    if not isinstance(permissions, Mapping):
        return False
    # Only an explicit boolean true permits. Anything else -- absent, a truthy
    # string, a number -- fails toward quarantine rather than toward dispatch.
    return permissions.get(submitter_class) is True


def load_watch(config: ConfigStore, project: str) -> ReviewFeedbackWatch | None:
    """The armed watch for *project*, or ``None`` when it is not armed.

    Raises :class:`~..config.schema.ConfigValidationError`, naming the path, for a
    project that armed the watcher and configured no usable poll command. An
    unreadable declaration must not resolve to "no comments": that would turn a
    typo into silence on every review, which looks exactly like a reviewer who
    said nothing -- the one thing this module's poll outcome exists to keep
    distinguishable.
    """
    if not review_feedback_enabled(config, project):
        return None
    base = f"{SECTION_PROJECTS}.{project}.{REVIEW_FEEDBACK_FIELD}"
    entry = _container(config, project)
    if entry is None:
        raise ConfigValidationError(
            [
                ConfigError(
                    base,
                    "review feedback is enabled for this project but no poll command is "
                    "configured, so nothing can read its review artifact",
                )
            ]
        )
    raw_poll = entry.get(POLL_FIELD)
    if isinstance(raw_poll, (str, bytes)) or not isinstance(raw_poll, Sequence):
        raise ConfigValidationError(
            [ConfigError(f"{base}.{POLL_FIELD}", "expected a list of arguments")]
        )
    try:
        poll = CommandTemplate.parse(raw_poll)
    except (TemplateError, TypeError, ValueError) as exc:
        raise ConfigValidationError(
            [ConfigError(f"{base}.{POLL_FIELD}", str(exc))]
        ) from exc
    raw_timeout = entry.get(TIMEOUT_FIELD)
    timeout_s = (
        int(raw_timeout)
        if isinstance(raw_timeout, int) and not isinstance(raw_timeout, bool) and raw_timeout > 0
        else DEFAULT_TIMEOUT_S
    )
    return ReviewFeedbackWatch(project=project, poll=poll, timeout_s=timeout_s)


def _container(config: ConfigStore, project: str) -> Mapping[str, Any] | None:
    projects = config.document().get(SECTION_PROJECTS)
    if not isinstance(projects, Mapping):
        return None
    entry = projects.get(project)
    if not isinstance(entry, Mapping):
        return None
    container = entry.get(REVIEW_FEEDBACK_FIELD)
    return container if isinstance(container, Mapping) else None


# --- polling ---------------------------------------------------------------


def poll_comments(
    watch: ReviewFeedbackWatch,
    context: RunContext,
    *,
    run_id: str,
    cwd: Path,
    custom: Mapping[str, str] | None = None,
    runner: CommandRunner | None = None,
) -> CommentPoll:
    """Read one run's review artifact, spending no model credits.

    The command is the operator's argv list with the run's variables substituted
    as single elements -- the same substitution every stage command gets, so a
    branch name a tracker chose cannot become syntax. Its output is JSON-decoded
    by :func:`~.poll.decode_entries`, the one decoder watch polling already uses,
    and read by fixed keys. No part of it is executed, expanded, or interpreted,
    and nothing on this path reaches a model.
    """
    values = build_variables(context, custom)
    try:
        argv = watch.poll.render(values)
    except Exception as exc:  # MissingVariableError and any template fault
        return _unhealthy(
            run_id,
            HealthReason.CONFIG_INVALID,
            f"the review-feedback poll command could not be rendered for this run: {exc}",
            program=watch.program,
        )
    started = time.monotonic()
    execute = runner if runner is not None else run_argv
    produced = execute(argv, cwd=cwd, timeout_s=watch.timeout_s)
    duration = time.monotonic() - started
    # argv[0] only: later elements carry the run's substituted values, and the
    # review text they select is not log material.
    logger.info(
        "review feedback for run %s polled with %r in %.2fs", run_id, argv[0], duration
    )
    return _read(run_id, watch, produced, duration=duration)


def _read(
    run_id: str, watch: ReviewFeedbackWatch, produced: CommandOutcome, *, duration: float
) -> CommentPoll:
    program = watch.program
    if produced.timed_out:
        return _unhealthy(
            run_id,
            HealthReason.TIMED_OUT,
            f"the review-feedback poll command {program!r} exceeded its timeout and was killed",
            program=program,
            duration_s=duration,
        )
    if produced.start_error:
        return _unhealthy(
            run_id,
            HealthReason.PROGRAM_UNAVAILABLE,
            f"the review-feedback poll program {program!r} could not be started: "
            f"{produced.start_error}",
            program=program,
            duration_s=duration,
        )
    if produced.exit_code != 0:
        detail = _first_line(produced.stderr) or _first_line(produced.stdout)
        suffix = f": {detail}" if detail else ""
        return _unhealthy(
            run_id,
            HealthReason.COMMAND_FAILED,
            f"the review-feedback poll command {program!r} exited "
            f"{produced.exit_code}{suffix}",
            program=program,
            exit_code=produced.exit_code,
            duration_s=duration,
        )
    try:
        entries = decode_entries(produced.stdout)
    except ValueError as exc:
        return _unhealthy(
            run_id,
            HealthReason.UNREADABLE_OUTPUT,
            f"the review-feedback poll command {program!r} exited 0 but its output could "
            f"not be read: {exc}",
            program=program,
            exit_code=produced.exit_code,
            duration_s=duration,
        )
    comments, rejected = _map_comments(entries)
    if entries and not comments:
        listed = "; ".join(entry.reason for entry in rejected[:MAX_RECORDED_PROBLEMS])
        return _unhealthy(
            run_id,
            HealthReason.FIELD_MAP_MISMATCH,
            f"the review-feedback poll command {program!r} returned {len(entries)} "
            f"entr(ies) and none of them yielded a comment: {listed}",
            program=program,
            exit_code=produced.exit_code,
            duration_s=duration,
        )
    return CommentPoll(
        run_id=run_id,
        status=PollStatus.OK,
        comments=comments,
        program=program,
        exit_code=produced.exit_code,
        rejected=rejected,
        duration_s=duration,
    )


def _map_comments(
    entries: Sequence[Any],
) -> tuple[tuple[ReviewComment, ...], tuple[RejectedComment, ...]]:
    comments: list[ReviewComment] = []
    rejected: list[RejectedComment] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, Mapping):
            _reject(rejected, index, f"expected an object, got {type(entry).__name__}")
            continue
        values = {field: _text(entry.get(field)) for field in COMMENT_FIELDS}
        missing = tuple(field for field in REQUIRED_COMMENT_FIELDS if not values[field].strip())
        if missing:
            _reject(rejected, index, f"no value for {', '.join(missing)}")
            continue
        comments.append(ReviewComment(**values))
    return tuple(comments), tuple(rejected)


def _reject(rejected: list[RejectedComment], index: int, reason: str) -> None:
    if len(rejected) < MAX_RECORDED_PROBLEMS:
        rejected.append(RejectedComment(index=index, reason=reason))


def _text(value: Any) -> str:
    """Render one output value as text without interpreting it.

    A number or a boolean identifier is spelled rather than refused -- trackers
    number their comments -- but a nested object is not flattened into a value: a
    field whose path leads to a container has not been reported.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return ""


def _unhealthy(
    run_id: str,
    reason: HealthReason,
    detail: str,
    *,
    program: str = "",
    exit_code: int | None = None,
    duration_s: float = 0.0,
) -> CommentPoll:
    logger.warning(
        "review feedback for run %s is unhealthy (%s): %s", run_id, reason.value, detail
    )
    return CommentPoll(
        run_id=run_id,
        status=PollStatus.UNHEALTHY,
        reason=reason,
        detail=detail,
        program=program,
        exit_code=exit_code,
        duration_s=duration_s,
    )


def _first_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return ""


# --- the watcher -----------------------------------------------------------


class CommentScreener(Protocol):
    """The screener, narrowed to the per-element call this module makes.

    Structural rather than an import of :class:`~.screening.IntakeScreener` so a
    test can supply its own, and required rather than optional for the reason the
    dispatcher's screener seam is required: a default could only mean *do not
    screen*, and a comment that dispatches work is exactly the injection surface
    screening was built for. This module screens through this one seam and grows
    no screening of its own.
    """

    def screen_elements(
        self,
        route: SourceRoute,
        elements: Sequence[ContentElement],
        *,
        run_id: str,
        ref: SpecRef,
        source: str,
        project: str | None = ...,
        intake_guidance: str = ...,
    ) -> ScreeningReport: ...


class ReviewFeedbackWatcher:
    """Polls a run's review artifact and turns permitted new comments into fixes.

    Constructed with the reviser (the fix-authoring turn), the delivery pipeline
    (which carries the fix through the project's configured stages), the screener,
    the audit log, and optionally the host notifier. None of the three seams has a
    default: a watcher without a reviser would claim comments and author nothing,
    one without a pipeline would need a second way to run a stage command, and one
    without a screener would read attacker-authored text into a run unscreened.
    """

    def __init__(
        self,
        config: ConfigStore,
        state: StateStore,
        *,
        reviser: FeedbackReviser,
        delivery: RevisionDelivery,
        screener: CommentScreener,
        audit: AuditLog,
        notifier: FeedbackNotifier | None = None,
        guard: SpendGuard | None = None,
    ) -> None:
        self._config = config
        self._state = state
        self._reviser = reviser
        self._delivery = delivery
        self._screener = screener
        self._audit = audit
        self._notifier = notifier
        #: A guard supplied by a caller that already holds one, or a test with its
        #: own clock. ``None`` means *build the engine's own guard for the run*,
        #: not "no ceiling": the ceiling is constructed per tick from the stores
        #: this watcher already holds, so an ordinary caller cannot end up on an
        #: unbounded path.
        self._guard = guard

    def tick(
        self,
        route: SourceRoute,
        *,
        run_id: str,
        ref: SpecRef,
        context: RunContext,
        spec_type: str = "",
        custom: Mapping[str, str] | None = None,
        runner: CommandRunner | None = None,
    ) -> ReviewFeedbackTick:
        """Poll one run's review artifact and act on every new comment.

        Returns rather than raises for a project that is not armed, a poll that
        could not run, and a comment that was refused: one broken review must not
        stop the tick that also covers the others, and a caller reports what
        happened rather than losing it to an exception.

        A tick that finds no new comment dispatches nothing and spends nothing.
        """
        watch, invalid = self._watch(route.project)
        if watch is None:
            if invalid:
                return ReviewFeedbackTick(
                    run_id=run_id,
                    poll=CommentPoll(
                        run_id=run_id,
                        status=PollStatus.UNHEALTHY,
                        reason=HealthReason.CONFIG_INVALID,
                        detail=invalid,
                    ),
                )
            return ReviewFeedbackTick(
                run_id=run_id, poll=CommentPoll(run_id=run_id, status=PollStatus.DISABLED)
            )
        cwd = route.working_tree if route.working_tree is not None else self._config.root
        polled = poll_comments(
            watch, context, run_id=run_id, cwd=cwd, custom=custom, runner=runner
        )
        if not polled.healthy:
            return ReviewFeedbackTick(run_id=run_id, poll=polled)
        dispositions = tuple(
            self._handle(
                route,
                comment,
                run_id=run_id,
                ref=ref,
                context=context,
                spec_type=spec_type,
            )
            for comment in polled.comments
        )
        return ReviewFeedbackTick(run_id=run_id, poll=polled, dispositions=dispositions)

    def _watch(self, project: str) -> tuple[ReviewFeedbackWatch | None, str]:
        """The project's armed watch, plus the reason it is unusable if it is."""
        try:
            return load_watch(self._config, project), ""
        except ConfigValidationError as exc:
            # Two different answers otherwise share one None: a project that is
            # not armed, and a project that IS armed but whose definition cannot
            # be used. The reason separates them, because reporting the second as
            # "not enabled" sends the operator to look at a switch they already
            # turned on. Raising is not an option -- one project's bad
            # configuration must not stop the tick covering the other runs -- so
            # it travels back as a value.
            logger.warning("review feedback for project %r cannot be polled: %s", project, exc)
            return None, str(exc)

    # ------------------------------------------------------------ one comment

    def _handle(
        self,
        route: SourceRoute,
        comment: ReviewComment,
        *,
        run_id: str,
        ref: SpecRef,
        context: RunContext,
        spec_type: str,
    ) -> CommentDisposition:
        """Decide and act on one comment, in the order that keeps a refusal free.

        The order is the requirement: newness, then the commenter's own class,
        then the two bounds, then screening, then the dispatch. Everything that
        can refuse comes before everything that can spend, so a comment from a
        class that may not drive work and a comment on a run that has run out of
        cycles both cost nothing on their way to being refused.
        """
        from ..trust import acknowledge, consume, reconcile

        element = comment.element()
        revision = element.content_revision
        if not self._claim(run_id, comment.identifier, revision):
            return CommentDisposition(
                comment_id=comment.identifier,
                outcome=ReviewFeedbackOutcome.ALREADY_SEEN,
                content_revision=revision,
                detail="this comment revision was already acted on",
            )
        # The class comes from THIS comment's own author, through the one
        # derivation every trust question in the engine goes through. The item the
        # review belongs to has a class of its own and it is not consulted here:
        # serving a comment under its container's class is the escalation the
        # trust module exists to foreclose.
        reconciliation = reconcile(self._state, route, element, scope=run_id)
        trust = reconciliation.trust
        klass = trust.class_name
        if not dispatch_permitted_for(self._config, route.project, klass):
            outcome = self._quarantine(ref, run_id, comment, trust)
            acknowledge(self._state, route, element, scope=run_id)
            return outcome
        bound = self._bound(ref, run_id)
        if bound is not None:
            outcome = self._needs_human(ref, run_id, comment, trust, bound)
            acknowledge(self._state, route, element, scope=run_id)
            return outcome
        # Screening spends, so it happens only for a comment that has already
        # passed the class gate and both bounds. It is the intake screener, on the
        # item's own source and the item's own intake guidance, so a comment is
        # screened on the same terms the item that introduced it was.
        report = self._screener.screen_elements(
            route,
            (element,),
            run_id=run_id,
            ref=ref,
            source=route.source,
            project=route.project,
            intake_guidance=route.intake_for(spec_type),
        )
        if report.quarantined:
            outcome = self._screened_out(ref, run_id, comment, trust, report)
            acknowledge(self._state, route, element, scope=run_id)
            return outcome
        # Reached through consume, so a comment edited between the class
        # derivation and here raises rather than being dispatched under a class
        # derived for text that is gone.
        text = consume(element, trust)
        outcome = self._dispatch(
            ref, run_id, comment, trust, context=context, project=route.project, text=text
        )
        acknowledge(self._state, route, element, scope=run_id)
        return outcome

    def _claim(self, run_id: str, comment_id: str, revision: str) -> bool:
        """Claim one comment revision for this run. True the first time only.

        The revision is the claim's generation, so an edited comment is a claim of
        its own: the class is re-derived for the new text rather than the old
        decision being remembered. Claimed before anything acts on the comment,
        because a claim held by refused work is a comment nobody re-reads while an
        action taken before the claim is one taken twice.
        """
        return self._state.claim(
            CLAIM_REVIEW_COMMENT, run_id, comment_id, generation=revision, run_id=run_id
        )

    # ----------------------------------------------------------------- bounds

    def _bound(self, ref: SpecRef, run_id: str) -> ReviewFeedbackBound | None:
        """Which bound stops another cycle, or ``None`` when neither does.

        Two independent limits, either of which is enough: the configured
        revision-cycle count for this run, and the run's own budget ceiling. The
        cycle count is asked first because it is free; asking the guard applies
        the ceiling, which parks the run.
        """
        record = self._state.get_run(run_id)
        spent = feedback_cycles(record) if record is not None else 0
        if spent >= self._cycle_limit():
            return ReviewFeedbackBound.CYCLE_LIMIT
        decision = self._spend_guard(ref, run_id).authorize_dispatch()
        if not getattr(decision, "allowed", False):
            return ReviewFeedbackBound.BUDGET
        return None

    def _spend_guard(self, ref: SpecRef, run_id: str) -> SpendGuard:
        if self._guard is not None:
            return self._guard
        from ..budget import guard_for

        return guard_for(
            run_id, ref, state=self._state, config=self._config, headless=True
        )

    def _cycle_limit(self) -> int:
        return int(self._config.effective(CYCLE_LIMIT_SETTING).value)

    # ----------------------------------------------------------- dispositions

    def _quarantine(
        self, ref: SpecRef, run_id: str, comment: ReviewComment, trust: ElementTrust
    ) -> CommentDisposition:
        """Hold a comment whose own class may not drive a dispatch.

        Nothing has spent by the time this runs, and nothing spends inside it: the
        comment's text is never read, no turn is dispatched, and the record is a
        row on the run plus an audit line. A person releases it from the
        Review_Queue, which drops the claim so the next poll re-derives it.
        """
        self._mark(run_id, {DETAIL_FEEDBACK_QUARANTINED: self._held(run_id, comment.identifier)})
        detail = (
            f"submitter class {trust.class_name!r} is not permitted to drive review-feedback "
            f"dispatch for project; held for human release"
        )
        self._record(ref, run_id, ReviewFeedbackOutcome.QUARANTINED, trust, detail=detail)
        logger.warning(
            "review comment %r on run %s is from submitter class %r, which may not drive a "
            "fix dispatch; quarantined for human release at no model cost",
            comment.identifier,
            run_id,
            trust.class_name,
        )
        self._notify(
            "A reviewer comment is held for release",
            f"Comment {comment.identifier!r} on run {run_id} was written by a "
            f"{trust.class_name} and that class is not permitted to drive review-feedback "
            "dispatch for this project. Nothing was dispatched and nothing was spent; "
            "release it from the Review Queue to act on it.",
            detail={
                "run": run_id,
                "spec": ref.name,
                "comment": comment.identifier,
                "submitter_class": trust.class_name,
            },
        )
        return CommentDisposition(
            comment_id=comment.identifier,
            outcome=ReviewFeedbackOutcome.QUARANTINED,
            submitter_class=trust.class_name,
            content_revision=trust.revision,
            detail=detail,
        )

    def _screened_out(
        self,
        ref: SpecRef,
        run_id: str,
        comment: ReviewComment,
        trust: ElementTrust,
        report: ScreeningReport,
    ) -> CommentDisposition:
        """Hold a comment screening suspected, or could not produce a verdict for."""
        self._mark(run_id, {DETAIL_FEEDBACK_QUARANTINED: self._held(run_id, comment.identifier)})
        detail = "; ".join(report.findings) or "screening produced no verdict for this comment"
        self._record(ref, run_id, ReviewFeedbackOutcome.SCREENED_OUT, trust, detail=detail)
        self._notify(
            "A reviewer comment was screened out",
            f"Screening held comment {comment.identifier!r} on run {run_id}. No fix was "
            "dispatched from it; release it from the Review Queue to act on it.",
            quoted="\n\n".join(f for f in report.findings if f.strip()),
            detail={"run": run_id, "spec": ref.name, "comment": comment.identifier},
        )
        return CommentDisposition(
            comment_id=comment.identifier,
            outcome=ReviewFeedbackOutcome.SCREENED_OUT,
            submitter_class=trust.class_name,
            content_revision=trust.revision,
            detail=detail,
            # A screening verdict is a real model turn on the review role, and the
            # screener stamps its session to the run. Reporting it as free would
            # make the zero-credit claim untestable where it actually matters.
            spent=True,
        )

    def _needs_human(
        self,
        ref: SpecRef,
        run_id: str,
        comment: ReviewComment,
        trust: ElementTrust,
        bound: ReviewFeedbackBound,
    ) -> CommentDisposition:
        """Mark the run as needing human attention and dispatch nothing.

        The mark is what the Review_Queue surfaces, so the run waits on a person
        in the one place a person already looks rather than in a state of its own.
        It is NOT what stops further cycles: nothing reads it back in
        :meth:`_bound`. Suppression comes from the two conditions themselves
        persisting -- the cycle count never decreases, and the budget guard leaves
        the run parked, so a later tick re-derives the same bound. Said plainly
        because a future change that relied on the mark to hold the loop closed
        would find nothing enforcing it. The notice is required by the same
        requirement as the bound: a loop that stopped silently would look exactly
        like a reviewer who stopped commenting.
        """
        record = self._state.get_run(run_id)
        spent = feedback_cycles(record) if record is not None else 0
        self._mark(run_id, {DETAIL_FEEDBACK_NEEDS_HUMAN: True})
        # The comment that tripped the bound joins the held list rather than
        # staying merely claimed. Otherwise raising the limit or the ceiling --
        # the very thing a person does in response to this notice -- would not
        # bring the comment back, because it is claimed and absent from the list
        # the release reads: the one comment the human was told about would be the
        # one they could not act on, unless its author happened to edit it.
        self._mark(
            run_id,
            {DETAIL_FEEDBACK_QUARANTINED: self._held(run_id, comment.identifier)},
        )
        detail = (
            f"the {bound.value} bound was reached after {spent} feedback cycle(s); no further "
            "fix is dispatched for this run until a person acts"
        )
        self._audit.append(
            ref,
            AUDIT_REVIEW_FEEDBACK_BOUND,
            run=run_id,
            initiator=None,
            detail={
                "bound": bound.value,
                "cycles": spent,
                "limit": self._cycle_limit(),
                "comment": comment.identifier,
            },
        )
        logger.info(
            "run %s reached the %s bound on review feedback; marked needing human attention",
            run_id,
            bound.value,
        )
        self._notify(
            "A run needs human attention on review feedback",
            f"Run {run_id} for spec {ref.name!r} reached its {bound.value} bound after "
            f"{spent} review-feedback cycle(s). Comment {comment.identifier!r} was not "
            "dispatched; the run waits in the Review Queue.",
            detail={
                "run": run_id,
                "spec": ref.name,
                "bound": bound.value,
                "cycles": spent,
                "comment": comment.identifier,
            },
        )
        return CommentDisposition(
            comment_id=comment.identifier,
            outcome=ReviewFeedbackOutcome.BOUNDED,
            submitter_class=trust.class_name,
            content_revision=trust.revision,
            cycle=spent,
            bound=bound,
            detail=detail,
        )

    def _dispatch(
        self,
        ref: SpecRef,
        run_id: str,
        comment: ReviewComment,
        trust: ElementTrust,
        *,
        context: RunContext,
        project: str,
        text: str,
    ) -> CommentDisposition:
        """Author the fix, then carry it through the configured delivery stages.

        The cycle is counted only after the reviser has started, so a host that
        could not start one has not burned a round. The delivery goes through the
        pipeline's own entry point, which is the one an autonomous run and an
        explicit human request both use, so the stages, the gates, the verify
        retry ceiling and the integration floor are the project's configured ones
        rather than anything assembled here.
        """
        record = self._state.get_run(run_id)
        cycle = (feedback_cycles(record) if record is not None else 0) + 1
        revision = FeedbackRevision(
            run_id=run_id,
            ref=ref,
            project=project,
            cycle=cycle,
            comment_id=comment.identifier,
            content_revision=trust.revision,
            submitter_class=trust.class_name,
            quoted_comment=text,
        )
        try:
            self._reviser(revision)
        except Exception as exc:  # a host seam can fail for its own reasons
            return self._failed(ref, run_id, comment, trust, exc)
        self._mark(run_id, {DETAIL_FEEDBACK_CYCLES: cycle})
        delivered = self._delivery.deliver(context)
        detail = f"cycle {cycle} delivered: {getattr(delivered, 'outcome', '')}"
        self._record(ref, run_id, ReviewFeedbackOutcome.DISPATCHED, trust, detail=detail, cycle=cycle)
        return CommentDisposition(
            comment_id=comment.identifier,
            outcome=ReviewFeedbackOutcome.DISPATCHED,
            submitter_class=trust.class_name,
            content_revision=trust.revision,
            cycle=cycle,
            detail=detail,
            spent=True,
        )

    # --------------------------------------------------------------- recording

    def _record(
        self,
        ref: SpecRef,
        run_id: str,
        outcome: ReviewFeedbackOutcome,
        trust: ElementTrust,
        *,
        detail: str,
        cycle: int = 0,
    ) -> None:
        """Audit one comment's disposition as a decision gated on its class.

        Through the gated-decision recorder, so the class, the author and the
        content revision the decision relied upon travel with it. A record that
        named only the outcome would leave an operator unable to tell which
        version of a comment was acted on, which is the question an edited
        comment raises.
        """
        from ..trust import record_gated_decision

        record_gated_decision(
            self._audit,
            ref,
            AUDIT_REVIEW_FEEDBACK,
            trust,
            run=run_id,
            detail={"outcome": outcome.value, "cycle": cycle, "reason": detail},
        )

    def _failed(
        self,
        ref: SpecRef,
        run_id: str,
        comment: ReviewComment,
        trust: ElementTrust,
        exc: Exception,
    ) -> CommentDisposition:
        """A fix round the host could not start: retry it, but not forever.

        The claim is taken before the attempt, so a failure that simply returned
        would leave the comment claimed and permanently unreadable -- the next
        poll would call it already seen, the human release path would not find it
        in the held list, and a reviewer's comment would be lost to one transient
        host failure. So the claim is dropped here and the next tick re-enters the
        same gate, which is what this module documents.

        The retry is counted, because the failure could equally be permanent and
        an unbounded retry is not free: a retry re-runs screening, which is a
        model turn, so a host that always fails would spend one per tick on an
        external trigger. Once the failures reach the same revision-cycle limit
        that bounds successful rounds, the comment is held instead -- visible in
        the Review_Queue and recoverable by the release a person already has --
        and the claim is kept so nothing retries behind their back.
        """
        failures = self._failures(run_id, comment.identifier) + 1
        limit = self._cycle_limit()
        exhausted = limit > 0 and failures >= limit
        detail = f"the fix round could not be started: {exc}"
        if exhausted:
            detail = (
                f"{detail} (attempt {failures} of {limit}; held for release rather "
                "than retried again)"
            )
            self._mark(
                run_id,
                {
                    DETAIL_FEEDBACK_QUARANTINED: self._held(run_id, comment.identifier),
                    DETAIL_FEEDBACK_NEEDS_HUMAN: True,
                    DETAIL_FEEDBACK_FAILURES: self._failure_map(run_id, comment.identifier, 0),
                },
            )
        else:
            self._mark(
                run_id,
                {DETAIL_FEEDBACK_FAILURES: self._failure_map(run_id, comment.identifier, failures)},
            )
            self._state.release_claims(CLAIM_REVIEW_COMMENT, run_id, comment.identifier)
        self._record(ref, run_id, ReviewFeedbackOutcome.FAILED, trust, detail=detail)
        logger.warning(
            "a fix round for comment %r on run %s could not be dispatched: %s",
            comment.identifier,
            run_id,
            exc,
        )
        if exhausted:
            self._notify(
                "A reviewer comment could not be acted on",
                f"Comment {comment.identifier!r} on run {run_id} failed to dispatch "
                f"{failures} times and is now held. Release it from the Review Queue "
                "to try again.",
                detail={"run": run_id, "spec": ref.name, "comment": comment.identifier},
            )
        return CommentDisposition(
            comment_id=comment.identifier,
            outcome=ReviewFeedbackOutcome.FAILED,
            submitter_class=trust.class_name,
            content_revision=trust.revision,
            detail=detail,
        )

    def _failures(self, run_id: str, comment_id: str) -> int:
        """How many times dispatching *comment_id* has failed for this run."""
        record = self._state.get_run(run_id)
        return int(feedback_failures(record).get(comment_id, 0)) if record is not None else 0

    def _failure_map(self, run_id: str, comment_id: str, count: int) -> dict[str, int]:
        """The run's failure counts with *comment_id* set to *count*, zero removing."""
        record = self._state.get_run(run_id)
        counts = dict(feedback_failures(record)) if record is not None else {}
        if count <= 0:
            counts.pop(comment_id, None)
        else:
            counts[comment_id] = count
        return counts

    def _held(self, run_id: str, comment_id: str) -> list[str]:
        """The run's quarantined comment ids with *comment_id* added, deduplicated."""
        record = self._state.get_run(run_id)
        held = list(feedback_quarantined(record)) if record is not None else []
        if comment_id not in held:
            held.append(comment_id)
        return held

    def _mark(self, run_id: str, detail: Mapping[str, Any]) -> None:
        """Merge *detail* onto the run, tolerating a run row that is not there.

        ``update_run`` merges rather than replaces, so this writes only the keys
        the feedback loop owns and leaves every other writer's alone.
        """
        try:
            self._state.update_run(run_id, detail=dict(detail))
        except KeyError:
            logger.warning("no run row %r to record review feedback against", run_id)

    def _notify(
        self,
        title: str,
        body: str,
        *,
        quoted: str = "",
        detail: Mapping[str, Any] | None = None,
    ) -> None:
        """Send one notice, best-effort. The record is primary."""
        if self._notifier is None:
            return
        try:
            self._notifier.send(title, body, quoted=quoted, detail=dict(detail or {}))
        except Exception:  # a notice is best-effort; the record is already written
            logger.warning("could not deliver a review-feedback notice", exc_info=True)


def release_quarantined_comment(
    state: StateStore,
    audit: AuditLog,
    ref: SpecRef,
    run_id: str,
    comment_id: str,
    *,
    actor: str | None = None,
) -> bool:
    """Release one held comment so the next poll re-derives it. Human action only.

    Two things happen and both are needed: the comment leaves the run's held list,
    which is what the Review_Queue surfaces, and its claim is dropped, which is
    what lets the next poll see it as new again. Dropping the claim rather than
    dispatching from here is deliberate -- the release re-runs the whole decision,
    including a class re-derivation, so a comment edited while it was held is
    judged on the text it now has.

    Returns whether the comment was actually held. A release for a comment nobody
    held changes nothing and says so, rather than reporting a success that did not
    happen.
    """
    record = state.get_run(run_id)
    held = list(feedback_quarantined(record)) if record is not None else []
    if comment_id not in held:
        return False
    held.remove(comment_id)
    state.update_run(run_id, detail={DETAIL_FEEDBACK_QUARANTINED: held})
    # Every generation of this comment: a held comment may have been claimed at a
    # revision that has since been edited, and leaving an older generation claimed
    # would let a release appear to work while the poll still read it as seen.
    released = state.release_claims(CLAIM_REVIEW_COMMENT, run_id, comment_id)
    audit.append(
        ref,
        AUDIT_REVIEW_FEEDBACK_RELEASED,
        run=run_id,
        initiator=actor,
        detail={"comment": comment_id, "claims_released": released},
    )
    logger.info(
        "review comment %r on run %s was released by %s; the next poll re-derives it",
        comment_id,
        run_id,
        actor or "an operator",
    )
    return True
