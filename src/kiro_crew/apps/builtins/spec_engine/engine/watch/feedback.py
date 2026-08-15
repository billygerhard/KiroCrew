"""Item feedback: telling the tracker what happened to the item it reported.

A watched item that produced a run is a conversation an operator's colleagues are
watching. Configured feedback commands post the run's progress back to it, so the
issue that triggered work says the work is happening rather than sitting silent
until a pull request appears.

**One writeback per run per event.** The claim ledger holds the record, so a
resumed run, a retried tick, and a re-polled source cannot comment twice. This is
the same at-most-once rule tracker housekeeping needs, and it lives here so both
read one ledger rather than each keeping its own idea of what has been said.

**A failure is recorded, not fatal.** The run's work is already done or already
under way; a tracker that rejected a comment is not a reason to fail it. The
failure surfaces and the run continues, because the alternative is a completed
delivery reported as failed because a comment did not post.

**Zero model credits, and tracker text stays data.** These are configured argv
lists run through the delivery executor; nothing here composes text with a model.
Tracker text can reach an *argument* position -- ``review_title`` carries it, and
a feedback command may legitimately quote it -- but never the program position,
which ``CommandTemplate.parse`` requires to be literal, and never a shell, because
a value becomes exactly one argv element.

**The echo gate is not here, and must not be added here.** Requirement 36.7
restricts which submitter classes may be echoed, and the decision is made where
an element's text becomes a run context field
(:func:`~.echo.echoed_context`). A gate in front of :func:`post_feedback` would
look like it covered echoing and would not: element text reaches an argument
through :meth:`~..delivery.stages.StageExecutor.run_labelled`, which this module
shares with every delivery stage command and every quality gate, and
``review_title`` / ``review_summary`` are engine-owned variables all of them can
reference. By the time a context arrives here the decision has already been made
and the refused text is not on it.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any

from ..audit import AuditLog
from ..config import ConfigStore
from ..config.schema import (
    ITEM_LIFECYCLE_EVENTS,
    SECTION_SOURCES,
    ConfigError,
    ConfigValidationError,
)
from ..delivery.stages import StageExecutor, StageOutcome, StageResult
from ..delivery.templates import CommandTemplate, TemplateError
from ..delivery.variables import RunContext
from ..state import (
    CLAIM_WRITEBACK,
    SpecRef,
    StatePersistenceError,
    StateStore,
)

logger = logging.getLogger(__name__)

__all__ = [
    "AUDIT_ITEM_FEEDBACK",
    "FEEDBACK_FIELD",
    "FEEDBACK_PRESETS",
    "FEEDBACK_PRESET_HOSTS",
    "FeedbackOutcome",
    "FeedbackPoster",
    "FeedbackReport",
    "feedback_presets",
    "load_feedback",
    "post_feedback",
    "release_writeback_claim",
]

#: Source field holding the event-to-commands map.
FEEDBACK_FIELD = "feedback"

#: Audit event for one item-feedback attempt.
AUDIT_ITEM_FEEDBACK = "item.feedback"

#: Bundled tracker-housekeeping presets, keyed by the public host they drive.
#:
#: Each maps a subset of :data:`~..config.schema.ITEM_LIFECYCLE_EVENTS` to argv
#: lists, demonstrating the operations requirement 36.1 names -- comment, set
#: label, set state, assign, and referencing the review artifact -- through the
#: host's own CLI (``gh`` / ``glab``). Like the quality-gate presets, these are
#: starting points copied into ``sources.<source>.feedback`` and edited there,
#: not a live binding: a per-event override is simply the copied event's command
#: list, replaced.
#:
#: **Public hosts only, structurally.** This table is a closed literal of the
#: public hosts the engine already bundles watch sources for, and
#: :func:`feedback_presets` raises for any other name. There is no registration
#: path, so a preset for a private or internal tracker cannot exist -- shipping
#: one would put an organization's argv in the engine. An organization points an
#: event at its own tracker by writing that event's command list into its
#: source's ``feedback`` map directly, which is configuration, not a bundled
#: preset.
#:
#: **Only run-context variables that exist when the event fires.** A command that
#: referenced an unset variable would fail its event before execution (36.2), so
#: every ``{name}`` here is a real :data:`~..delivery.variables.RUN_CONTEXT_VARIABLES`
#: name present at that lifecycle point: ``item_url`` / ``item_id`` for the
#: triggering item at every event, ``branch_name`` only from delivery onward,
#: ``review_title`` only once a review artifact exists, and ``review_url`` only at
#: ``delivery_submitted`` and after, because the delivery pipeline learns the
#: artifact's address from the submit command that raised it.
#:
#: **The link-artifact operation names the artifact, not the branch.** Requirement
#: 36.1 names linking the review artifact among the operations these presets
#: demonstrate, and a comment naming the branch a change was pushed from is not a
#: link to the artifact -- the branch was a stand-in from before a run-context
#: variable carried the address.
FEEDBACK_PRESETS: Mapping[str, Mapping[str, tuple[tuple[str, ...], ...]]] = {
    "github": {
        "claimed": (
            ("gh", "issue", "comment", "{item_url}", "--body",
             "Automated spec authoring has started for this item."),
            ("gh", "issue", "edit", "{item_url}", "--add-label", "in-progress"),
            ("gh", "issue", "edit", "{item_url}", "--add-assignee", "@me"),
        ),
        "awaiting_review": (
            ("gh", "issue", "comment", "{item_url}", "--body",
             "Spec is awaiting review: {review_title}"),
        ),
        "delivery_submitted": (
            ("gh", "issue", "comment", "{item_url}", "--body",
             "Delivery submitted for review: {review_url}"),
        ),
        "completed": (
            ("gh", "issue", "comment", "{item_url}", "--body", "Spec run completed."),
            ("gh", "issue", "close", "{item_url}"),
        ),
        "failed": (
            ("gh", "issue", "edit", "{item_url}", "--add-label", "needs-human"),
            ("gh", "issue", "comment", "{item_url}", "--body",
             "Spec run needs a human: it failed or requires attention."),
        ),
        "refused": (
            ("gh", "issue", "edit", "{item_url}", "--add-label", "needs-human"),
            ("gh", "issue", "comment", "{item_url}", "--body",
             "Spec run was refused by the autonomy policy and needs a human."),
        ),
    },
    "gitlab": {
        "claimed": (
            ("glab", "issue", "note", "{item_id}", "--message",
             "Automated spec authoring has started for this item."),
            ("glab", "issue", "update", "{item_id}", "--label", "in-progress"),
        ),
        "awaiting_review": (
            ("glab", "issue", "note", "{item_id}", "--message",
             "Spec is awaiting review: {review_title}"),
        ),
        "delivery_submitted": (
            ("glab", "issue", "note", "{item_id}", "--message",
             "Delivery submitted for review: {review_url}"),
        ),
        "completed": (
            ("glab", "issue", "note", "{item_id}", "--message", "Spec run completed."),
            ("glab", "issue", "close", "{item_id}"),
        ),
        "failed": (
            ("glab", "issue", "update", "{item_id}", "--label", "needs-human"),
            ("glab", "issue", "note", "{item_id}", "--message",
             "Spec run needs a human: it failed or requires attention."),
        ),
        "refused": (
            ("glab", "issue", "update", "{item_id}", "--label", "needs-human"),
            ("glab", "issue", "note", "{item_id}", "--message",
             "Spec run was refused by the autonomy policy and needs a human."),
        ),
    },
}

#: The public hosts a bundled feedback preset exists for, in declaration order.
FEEDBACK_PRESET_HOSTS: tuple[str, ...] = tuple(FEEDBACK_PRESETS)


def feedback_presets(host: str) -> dict[str, list[list[str]]]:
    """Return *host*'s bundled feedback map, ready to write into a source.

    The result is the shape ``sources.<source>.feedback`` takes -- an event-keyed
    map of command lists -- deep-copied so a configuration surface can offer it
    for editing without an edit reaching back into the bundled table and changing
    what every later source is offered.

    Raises ``KeyError`` for any host that is not a bundled public one. This is the
    structural half of "no non-public preset": there is no name a caller can pass
    that yields a preset for a private tracker, because the table holds only the
    public hosts and every miss raises rather than inventing an empty map.
    """
    preset = FEEDBACK_PRESETS.get(host)
    if preset is None:
        raise KeyError(
            f"no bundled feedback preset for {host!r}; bundled presets exist only for the "
            f"public hosts {', '.join(FEEDBACK_PRESET_HOSTS)}, and an organization's tracker "
            "is served by writing its own feedback commands into the source"
        )
    return {event: [list(argv) for argv in commands] for event, commands in preset.items()}


class FeedbackOutcome(str, Enum):
    """What became of one feedback attempt."""

    #: Commands ran and every one succeeded.
    POSTED = "posted"
    #: No commands configured for this event on this source. Recorded, not an error.
    UNCONFIGURED = "unconfigured"
    #: This run already posted this event. The ledger held.
    ALREADY_POSTED = "already_posted"
    #: Commands ran and one failed, or the configuration could not be read.
    FAILED = "failed"


@dataclass(frozen=True)
class FeedbackReport:
    """One event's feedback attempt for one run."""

    source: str
    event: str
    run_id: str
    outcome: FeedbackOutcome
    #: The executor's result when commands ran, absent otherwise.
    result: StageResult | None = None
    reason: str = ""
    declared_at: str = ""

    @property
    def posted(self) -> bool:
        return self.outcome is FeedbackOutcome.POSTED

    @property
    def suppressed(self) -> bool:
        """Whether this event is now held back from posting until a release.

        A failed post keeps its claim on purpose -- retrying a command that may
        already have commented is how one event becomes two comments -- so the
        event stays suppressed for the run until someone who knows what landed
        clears the ledger row. The only outcome that suppresses is ``FAILED``:
        ``ALREADY_POSTED`` means an earlier attempt held, not that this one
        needs releasing.
        """
        return self.outcome is FeedbackOutcome.FAILED

    def clears(self) -> dict[str, str]:
        """The ledger row an operator releases to let this event post again.

        Named rather than described so a report and the audit detail point at the
        exact claim :func:`release_writeback_claim` deletes, not a paraphrase of
        it. Empty when nothing is suppressed.
        """
        if not self.suppressed:
            return {}
        return {"kind": CLAIM_WRITEBACK, "scope": self.run_id, "subject": self.event}

    def suppression_note(self) -> str:
        """One line for a FAILED report saying the event is suppressed and how to clear it."""
        if not self.suppressed:
            return ""
        return (
            f"the {self.event!r} feedback for run {self.run_id} is now suppressed; its "
            f"writeback claim (kind={CLAIM_WRITEBACK}, scope={self.run_id}, "
            f"subject={self.event}) is kept so a retry cannot comment twice, and "
            "release_writeback_claim clears it once an operator knows what landed"
        )

    @property
    def spent_credits(self) -> float:
        """Always zero. Feedback runs configured argv, never a model."""
        return 0.0

    def describe(self) -> str:
        if self.reason:
            return f"{self.event}: {self.outcome.value} ({self.reason})"
        return f"{self.event}: {self.outcome.value}"

    def detail(self) -> dict[str, Any]:
        record: dict[str, Any] = {
            "source": self.source,
            "event": self.event,
            "outcome": self.outcome.value,
            "reason": self.reason,
            "declared_at": self.declared_at,
            # Names only. A rendered argv can carry a tracker title, and an audit
            # record is read by people who did not choose that text.
            "variables_used": list(self.result.variables_used) if self.result else [],
            "commands_run": len(self.result.commands) if self.result else 0,
        }
        if self.suppressed:
            # A failed post keeps its claim, so the audit trail has to say the
            # event is now held back and name the ledger row that releases it --
            # otherwise the only surface for a permanently suppressed event is
            # reading the claims table by hand.
            record["suppressed"] = True
            record["clears"] = self.clears()
        return record


def load_feedback(config: ConfigStore, source: str) -> dict[str, tuple[CommandTemplate, ...]]:
    """Read *source*'s feedback commands, keyed by lifecycle event.

    Raises ``ConfigValidationError`` naming the path for a declaration that
    cannot be parsed, for an event outside the vocabulary, and for a source that
    is not declared at all. An unreadable feedback map must not resolve to "no
    feedback": that would turn a typo into silence on every item, which looks
    exactly like a source nobody configured feedback for. The same reasoning
    covers a misspelled *source* -- strictness about the event name and silence
    about the source name would make ``githb`` indistinguishable from a correct
    name with nothing configured.
    """
    entry = _source_entry(config, source)
    base = f"{SECTION_SOURCES}.{source}.{FEEDBACK_FIELD}"
    if entry is None:
        raise ConfigValidationError(
            [ConfigError(f"{SECTION_SOURCES}.{source}", "watch source is not declared")]
        )
    node = entry.get(FEEDBACK_FIELD)
    if node is None:
        return {}
    if not isinstance(node, Mapping):
        raise ConfigValidationError(
            [ConfigError(base, "expected an object keyed by lifecycle event")]
        )
    errors: list[ConfigError] = []
    loaded: dict[str, tuple[CommandTemplate, ...]] = {}
    for event, commands in node.items():
        path = f"{base}.{event}"
        if event not in ITEM_LIFECYCLE_EVENTS:
            errors.append(ConfigError(path, "unknown item lifecycle event"))
            continue
        if isinstance(commands, (str, bytes)) or not isinstance(commands, Sequence):
            errors.append(ConfigError(path, "expected a list of commands"))
            continue
        if not commands:
            # The schema calls this "expected at least one command", and skipping
            # it here would report the event as UNCONFIGURED -- turning a typo into
            # silence on every item, which is the exact outcome this loader exists
            # to prevent.
            errors.append(ConfigError(path, "expected at least one command"))
            continue
        parsed: list[CommandTemplate] = []
        for index, argv in enumerate(commands):
            try:
                parsed.append(CommandTemplate.parse(argv))
            except (TemplateError, TypeError, ValueError) as exc:
                errors.append(ConfigError(f"{path}[{index}]", str(exc)))
        loaded[str(event)] = tuple(parsed)
    if errors:
        raise ConfigValidationError(errors)
    return loaded


def post_feedback(
    state: StateStore,
    config: ConfigStore,
    audit: AuditLog,
    ref: SpecRef,
    *,
    source: str,
    event: str,
    run_id: str,
    context: RunContext,
    executor: StageExecutor,
) -> FeedbackReport:
    """Post *event*'s configured feedback for *run_id*, at most once.

    *audit* and *ref* are required. Requirement 36.5 wants a writeback failure
    surfaced rather than swallowed, and a caller able to omit the log would drop
    the only record that the tracker refused -- the same shape as a kill-switch
    stop that notifies nobody.

    Never raises. Every outcome, including an unreadable configuration, comes back
    as a report so the caller records it beside the run instead of unwinding work
    that already happened.
    """
    if event not in ITEM_LIFECYCLE_EVENTS:
        raise ValueError(f"unknown item lifecycle event: {event!r}")
    if not run_id.strip():
        raise ValueError("item feedback must name the run it is reporting")

    declared_at = f"{SECTION_SOURCES}.{source}.{FEEDBACK_FIELD}.{event}"
    try:
        commands = load_feedback(config, source).get(event, ())
    except ConfigValidationError as exc:
        return _record(
            audit,
            ref,
            FeedbackReport(
                source=source,
                event=event,
                run_id=run_id,
                outcome=FeedbackOutcome.FAILED,
                reason=str(exc),
                declared_at=declared_at,
            ),
        )
    if not commands:
        # Recorded, not an error. Most sources configure feedback for some events
        # and not others, and requirement 36.8 wants the unconfigured case stated
        # rather than treated as a fault.
        return _record(
            audit,
            ref,
            FeedbackReport(
                source=source,
                event=event,
                run_id=run_id,
                outcome=FeedbackOutcome.UNCONFIGURED,
                reason="no feedback commands configured for this event",
                declared_at=declared_at,
            ),
        )

    # The claim is taken before the commands run, not after. A claim taken
    # afterwards would let a crash between the comment and the claim post the
    # same comment twice, and a duplicate comment on a public issue cannot be
    # withdrawn.
    if not state.claim_writeback(run_id, event):
        return FeedbackReport(
            source=source,
            event=event,
            run_id=run_id,
            outcome=FeedbackOutcome.ALREADY_POSTED,
            reason="this run already posted this event",
            declared_at=declared_at,
        )

    result = executor.run_labelled(event, context, commands, declared_at=declared_at)
    outcome = (
        FeedbackOutcome.POSTED if result.outcome is StageOutcome.PASSED else FeedbackOutcome.FAILED
    )
    if outcome is FeedbackOutcome.FAILED:
        # The claim stays taken. Retrying a writeback whose command may already
        # have commented is how one event becomes two comments; an operator
        # releases the claim deliberately once they know what landed.
        logger.warning(
            "item feedback for event %r on source %r failed for run %s: %s",
            event,
            source,
            run_id,
            result.reason,
        )
    return _record(
        audit,
        ref,
        FeedbackReport(
            source=source,
            event=event,
            run_id=run_id,
            outcome=outcome,
            result=result,
            reason=result.reason,
            declared_at=declared_at,
        ),
    )


def _record(audit: AuditLog, ref: SpecRef, report: FeedbackReport) -> FeedbackReport:
    """Audit *report*, and never let the audit write fail the run.

    An audit append can raise on any filesystem trouble, and this is called on
    every return path -- including after the comment has already landed on the
    item. Propagating would turn a writeback whose failure requirement 36.6 says
    must not fail the run into the thing that fails it. The report still comes
    back, and the lost record is logged so the gap is visible rather than silent.
    """
    try:
        audit.append(
            ref,
            AUDIT_ITEM_FEEDBACK,
            run=report.run_id,
            initiator=None,
            detail=report.detail(),
            cost=report.spent_credits,
        )
    except StatePersistenceError as exc:
        logger.warning(
            "item feedback for event %r on run %s could not be audited: %s",
            report.event,
            report.run_id,
            exc,
        )
    return report


def _source_entry(config: ConfigStore, source: str) -> Mapping[str, Any] | None:
    """The source's config entry, or ``None`` when it is not declared."""
    node = config.document().get(SECTION_SOURCES)
    if not isinstance(node, Mapping):
        return None
    entry = node.get(source)
    return entry if isinstance(entry, Mapping) else None


def release_writeback_claim(state: StateStore, run_id: str, event: str) -> bool:
    """Drop one lifecycle-event writeback claim so the event can post again.

    The operator surface for a suppressed event, and the twin of
    :func:`~.lifecycle.release_dispatch_claim`: one release idiom over the shared
    claim ledger rather than hand-written SQL against the store. A failed post
    keeps its claim on purpose -- retrying a command that may already have
    commented is how one event becomes two -- so the event stays suppressed for
    the run until someone who knows what landed clears it here.

    True when a claim was cleared, False when none was held. Unlike its dispatch
    twin it takes no lifecycle generation, because a writeback claim is keyed on
    the run and the event alone: a run does not span generations, so there is
    nothing for a generation to disambiguate.
    """
    if event not in ITEM_LIFECYCLE_EVENTS:
        raise ValueError(f"unknown item lifecycle event: {event!r}")
    if not run_id.strip():
        raise ValueError("a writeback release must name the run whose claim it clears")
    return state.release_claim(CLAIM_WRITEBACK, run_id, event)


@dataclass(frozen=True)
class FeedbackPoster:
    """Posts a run's item-feedback events by one route, at every wiring site.

    The feedback library posts one event for one run; this is what the lifecycle
    points call so all of them post the same way. Bundling the stores here rather
    than threading four arguments through each site is what keeps the ledger
    agreeing with itself: a second site that built its own executor or reached
    the claim by a different key would be the equivalent-second-path defect that
    lets a resumed run comment twice.

    **The gate is "did the source ask for feedback".** Requirement 10.10 writes
    back *where item feedback commands are configured*, so a source that declares
    no ``feedback`` map at all posts nothing and records nothing -- the ordinary
    case, and the one that must stay silent so an unattended engine does not
    narrate every transition of every run to a tracker nobody configured. A
    source that declares the map but this event is not in it is handed to
    :func:`post_feedback`, which records it ``UNCONFIGURED``: the operator asked
    for some feedback, so "nothing for this event" is a stated answer rather than
    silence. A source a run names but configuration does not declare is treated
    as no feedback, because a run must never point at an undeclared source and
    posting would only manufacture a FAILED row for a misconfiguration this class
    is not the place to surface.
    """

    state: StateStore
    config: ConfigStore
    audit: AuditLog
    project: str | None = None
    #: Executor every post routes through when a call names none. Left unset in
    #: production, where each post builds one for the run's project; a caller that
    #: needs a specific runner (a test's recording runner, a shared executor)
    #: sets it once here rather than passing it to every ``post``.
    executor: StageExecutor | None = None

    def post(
        self,
        ref: SpecRef,
        *,
        source: str | None,
        run_id: str,
        event: str,
        context: RunContext,
        executor: StageExecutor | None = None,
    ) -> FeedbackReport | None:
        """Post *event* for *run_id*, or ``None`` when this source posts nothing.

        Returns the :class:`FeedbackReport` when a post was attempted so a caller
        can surface a suppressed failure, and ``None`` when the source configured
        no feedback -- the two are different: a report says the mechanism ran, a
        ``None`` says there was nothing to run.
        """
        if not (source or "").strip():
            # An interactive run has no triggering item, so there is no tracker
            # conversation to write back to. Not an error: most runs of a spec a
            # person authored by hand reach these same transitions.
            return None
        assert source is not None  # narrowed by the guard above, for the checker
        if not self._declares_feedback(source):
            return None
        resolved = executor or self.executor or StageExecutor(self.config, project=self.project)
        return post_feedback(
            self.state,
            self.config,
            self.audit,
            ref,
            source=source,
            event=event,
            run_id=run_id,
            context=context,
            executor=resolved,
        )

    def _declares_feedback(self, source: str) -> bool:
        """Whether *source* declares a feedback map at all.

        A missing source and a source without the map both answer False: neither
        asked for feedback, and the difference between them is a configuration
        fault this poster does not exist to report.
        """
        entry = _source_entry(self.config, source)
        return entry is not None and entry.get(FEEDBACK_FIELD) is not None
