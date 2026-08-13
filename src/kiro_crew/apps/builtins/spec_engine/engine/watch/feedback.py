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
a value becomes exactly one argv element. Echoing that text onward is a separate
gate this module does not implement: requirement 36.7 restricts which submitter
classes may be echoed, and task 8.7 owns it. Deriving a class is not gating an
echo.
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
from ..state import SpecRef, StatePersistenceError, StateStore

logger = logging.getLogger(__name__)

__all__ = [
    "AUDIT_ITEM_FEEDBACK",
    "FEEDBACK_FIELD",
    "FeedbackOutcome",
    "FeedbackReport",
    "load_feedback",
    "post_feedback",
]

#: Source field holding the event-to-commands map.
FEEDBACK_FIELD = "feedback"

#: Audit event for one item-feedback attempt.
AUDIT_ITEM_FEEDBACK = "item.feedback"


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
    def spent_credits(self) -> float:
        """Always zero. Feedback runs configured argv, never a model."""
        return 0.0

    def describe(self) -> str:
        if self.reason:
            return f"{self.event}: {self.outcome.value} ({self.reason})"
        return f"{self.event}: {self.outcome.value}"

    def detail(self) -> dict[str, Any]:
        return {
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
        FeedbackOutcome.POSTED
        if result.outcome is StageOutcome.PASSED
        else FeedbackOutcome.FAILED
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
