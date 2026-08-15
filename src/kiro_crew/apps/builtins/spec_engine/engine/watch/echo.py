"""Echo gate: which authored text a writeback may republish to a shared tracker.

A tracker-housekeeping writeback echoes text back onto a public item, and that
text is attacker-authored in the general case -- a stranger's comment on a
maintainer's issue is still that stranger's words. So before any Content_Element's
text reaches a writeback command's argument, this gate decides whether that
element's submitter class is one an operator has said may be amplified back into
the shared system.

**The class is the element's own, and it is never re-derived here.** The gate
takes the :class:`~..trust.ElementTrust` that :func:`~..trust.derive` already
produced from the element's *own* author. It does not look at the item the
element sits on: echoing a stranger's comment under the item-opener's class is
exactly the container-to-element inheritance the trust module exists to prevent,
and it was a real defect once. Passing the element's own trust rather than an
item class is what structurally forecloses it -- there is no item class to reach.

**The least-trusted class is never permitted, whatever configuration says.** An
operator can misconfigure ``echo`` for the bottom class, so the floor is enforced
here rather than left to configuration. The bottom class is read from the trust
ordering itself (:data:`~..config.schema.LEAST_TRUSTED_CLASS`, the last of
:data:`~..config.schema.SUBMITTER_CLASSES`) rather than spelled, so a class added
below the current floor later is still refused instead of silently uncovered.

**Permission is off by default and per class.** Writeback is disabled by default,
so echo is too: a class is permitted only where ``sources.<source>.echo.<class>``
is explicitly ``true``. This mirrors the shape ``sources.<source>.screening.<class>``
already uses, with the polarity flipped -- screening fails toward running, echo
fails toward silence, because republishing untrusted text is the risk here.

**Text is reached through :func:`~..trust.consume`.** A caller cannot hand the
gate one revision and then echo another: an element edited after the gate ran
raises :class:`~..trust.StaleContent` at the point of use, forcing a re-derive
rather than republishing text under a decision made about words that are gone.
This is the same discipline intake screening uses for the same reason.

Nothing in production calls :func:`echoable_text` directly. It is reached through
:func:`echoed_context`, which is where an element's text becomes a *run context*
field, and that placement is the point rather than a convenience.

**The gate belongs at population, not in front of a writeback.** Element text
reaches an argument through :meth:`~..delivery.stages.StageExecutor.run_labelled`,
and the item-feedback poster is only one of its callers -- every delivery stage
command and every quality gate goes through the same executor over the same
variable set, and ``review_title`` / ``review_summary`` are engine-owned run
context variables any of those commands may reference. A gate sitting in front of
:func:`~.feedback.post_feedback` would therefore leave every delivery-stage
command uncovered while reading as though echo were gated. Populating the run
context is the one place upstream of both consumers, so that is where the
decision is made.

**A refusal omits the field; it does not empty it.** An emptied variable still
substitutes, and a command may read the empty argument as a valid value that
means something else. So a refused field is left with no value at all, which
:meth:`~..delivery.variables.RunContext.to_variables` drops, which makes a
command referencing it refuse before spawning rather than run short an argument.
A field the caller names is answered here whatever the incoming context carried:
otherwise a base context could carry a stranger's words past the gate that was
asked about them.

**Stale content is a skip, not a failure.** :func:`echoable_text` lets
:class:`~..trust.StaleContent` propagate, which is safe in itself -- nothing is
echoed -- but reaching a writeback uncaught would surface a refusal-to-echo as a
writeback *failure*, and a failed writeback keeps its ledger claim, which
suppresses that event for the rest of the run. A refusal to echo one field would
then permanently silence an entire lifecycle event. So an edit between derivation
and use re-derives when the caller supplied a way to, and otherwise omits the
field and records why.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Callable

from ..config import ConfigStore
from ..config.schema import LEAST_TRUSTED_CLASS, SECTION_SOURCES, WILDCARD_KEY
from ..delivery.variables import RunContext

if TYPE_CHECKING:
    # Annotations only. ``trust`` imports ``watch.dispatch``, so importing it at
    # module load would re-enter a partially-initialized ``trust`` through the
    # watch package's __init__; the runtime need (``consume``) is imported inside
    # the one function that uses it, the same way ``watch.screening`` does.
    from ..trust import ContentElement, ElementTrust

logger = logging.getLogger(__name__)

__all__ = [
    "ECHOED_CONTEXT_FIELDS",
    "ECHO_FIELD",
    "EchoCandidate",
    "EchoOmission",
    "EchoedContext",
    "echo_permitted_for",
    "echoable_text",
    "echoed_context",
]

#: The per-submitter-class echo-permission map on a watch source. Echo is off for
#: every class unless this map turns a class on explicitly, and the least-trusted
#: class is refused even when it does.
ECHO_FIELD = "echo"

#: Run context variables that carry an element's authored text into a command's
#: argument. These are the fields :func:`echoed_context` decides, and the reason
#: the gate sits at population: they are engine-owned names, so a delivery stage
#: command, a quality gate, and an item-feedback command can each reference them
#: through the one shared executor.
ECHOED_CONTEXT_FIELDS: tuple[str, ...] = ("review_title", "review_summary")


@dataclass(frozen=True)
class EchoCandidate:
    """An element whose text a caller wants to put into a run context field.

    The trust travels with the element rather than being derived here, so this
    carries :func:`~..trust.derive`'s answer about *this element's own* author.
    There is no item or artifact on it to fall back to, which is what forecloses
    the container-to-element inheritance the trust module exists to prevent.
    """

    element: ContentElement
    trust: ElementTrust


@dataclass(frozen=True)
class EchoOmission:
    """One field the gate refused to populate, and why.

    Recorded rather than only acted on: a run whose submit command refuses for a
    variable with no value is otherwise a puzzle, and "the class that wrote this
    text may not be echoed" is the answer an operator needs to see.
    """

    field: str
    class_name: str
    reason: str

    def detail(self) -> dict[str, Any]:
        """Names and a reason only. The refused text itself is never recorded."""
        return {"field": self.field, "class": self.class_name, "reason": self.reason}


@dataclass(frozen=True)
class EchoedContext:
    """A run context with the echo decision already applied to it.

    The context is the whole answer: a caller passes :attr:`context` on to the
    executor and cannot reach the text that was refused, because it was never put
    anywhere. :attr:`omitted` is for the record, not for a second decision.
    """

    context: RunContext
    echoed: tuple[str, ...] = ()
    omitted: tuple[EchoOmission, ...] = ()

    @property
    def refused(self) -> bool:
        return bool(self.omitted)

    def detail(self) -> dict[str, Any]:
        """Audit fields: which names were populated, and why the others were not."""
        return {
            "echoed": list(self.echoed),
            "omitted": [omission.detail() for omission in self.omitted],
        }


#: Re-derives an element's trust at its current revision. Supplied by a caller
#: that can afford to answer an edit rather than skip it -- deriving reads the
#: element's own author through the same one derivation every trust question in
#: the engine uses, so this seam never becomes a second classifier.
Rederive = Callable[["ContentElement"], "ElementTrust"]


def echo_permitted_for(config: ConfigStore, source: str, submitter_class: str) -> bool:
    """Whether an element of *submitter_class* may be echoed on *source*.

    Refused by default. Permitted only where ``sources.<source>.echo.<class>`` is
    explicitly ``true`` -- and never for the least-trusted class, whatever the
    configuration says, because the floor is a guarantee an operator must not be
    able to switch off by editing one map entry.

    The floor comes from the class ordering (:data:`LEAST_TRUSTED_CLASS`), not a
    spelled name, so a class added below the current bottom is still covered. A
    wildcard key is not honoured: it is never a class an element resolves to, and
    reading it would let one entry permit echo for every class at once.

    Read from the raw document rather than the effective-value resolver because
    the map is per class rather than a scalar. ``sources`` is config-only, so no
    tool can widen who may be echoed.
    """
    if submitter_class == LEAST_TRUSTED_CLASS:
        # The floor. Enforced here, not in configuration, so a misconfigured
        # ``echo.external: true`` cannot amplify the least-trusted class.
        return False
    if submitter_class == WILDCARD_KEY:
        return False
    sources = config.document().get(SECTION_SOURCES)
    if not isinstance(sources, Mapping):
        return False
    entry = sources.get(source)
    if not isinstance(entry, Mapping):
        return False
    echo = entry.get(ECHO_FIELD)
    if not isinstance(echo, Mapping):
        return False
    # Only an explicit boolean true permits. Anything else -- absent, a truthy
    # string, a number -- fails toward silence rather than toward republishing.
    return echo.get(submitter_class) is True


def echoable_text(
    config: ConfigStore,
    source: str,
    element: ContentElement,
    trust: ElementTrust,
) -> str | None:
    """*element*'s current text if it may be echoed on *source*, else ``None``.

    The class is taken from *trust* -- the class :func:`~..trust.derive` produced
    from this element's *own* author -- and never re-derived here, so a caller
    cannot slip in the item's class in its place. When the class is not permitted
    the text is never read at all: the gate answers ``None`` without touching the
    words.

    When the class is permitted, the text is reached through
    :func:`~..trust.consume`, which raises :class:`~..trust.StaleContent` if the
    element has been edited since *trust* was derived. That is deliberate: a gate
    decision is about the revision it saw, and republishing a later revision under
    it would echo text no one screened.
    """
    if not echo_permitted_for(config, source, trust.class_name):
        return None
    from ..trust import consume

    return consume(element, trust)


def echoed_context(
    config: ConfigStore,
    source: str,
    context: RunContext,
    *,
    title: EchoCandidate | None = None,
    summary: EchoCandidate | None = None,
    rederive: Rederive | None = None,
) -> EchoedContext:
    """Populate *context*'s echo fields from gated elements, omitting refusals.

    The one route by which an element's text becomes ``review_title`` or
    ``review_summary``. Both are engine-owned run context variables, so they are
    substituted by the shared executor for every delivery stage command, every
    quality gate, and every item-feedback command alike -- which is why the
    decision is made here, upstream of all of them, rather than in front of any
    one consumer.

    A named field is answered here whatever *context* already carried: a refused
    candidate leaves the field with no value even if the incoming context had one,
    because a base context that could pre-populate the field would be a way past
    the gate. A field not named is left untouched -- an engine-authored review
    title is not element text and is not this gate's business.

    An omitted field is *absent*, not blank. It has no value, so
    :meth:`~..delivery.variables.RunContext.to_variables` drops it and a command
    referencing it refuses before spawning. A blank string handed to a command as
    an argument is a different and worse outcome: the command runs, and what it
    does with an empty argument is its own business.

    :class:`~..trust.StaleContent` never escapes. An element edited since its
    trust was derived is re-derived when *rederive* is supplied and skipped
    otherwise, because letting the refusal propagate to a writeback would record
    a *failure* -- and a failed writeback keeps its ledger claim, so one refused
    field would suppress a whole lifecycle event for the rest of the run.
    """
    populated: dict[str, str] = {}
    omitted: list[EchoOmission] = []
    for field, candidate in (("review_title", title), ("review_summary", summary)):
        if candidate is None:
            continue
        text, reason = _gated_text(config, source, candidate, rederive)
        if text is None:
            omitted.append(
                EchoOmission(
                    field=field,
                    class_name=candidate.trust.class_name,
                    reason=reason,
                )
            )
            # Cleared rather than left as it arrived. The caller asked this gate
            # about the field, so the gate's answer is the field's value, and
            # "no value" is what a refusal means.
            populated[field] = ""
            continue
        populated[field] = text
    if not populated:
        return EchoedContext(context=context)
    echoed = tuple(name for name, value in populated.items() if value)
    return EchoedContext(
        context=replace(context, **populated),
        echoed=echoed,
        omitted=tuple(omitted),
    )


def _gated_text(
    config: ConfigStore,
    source: str,
    candidate: EchoCandidate,
    rederive: Rederive | None,
) -> tuple[str | None, str]:
    """*candidate*'s echoable text, or ``None`` with the reason it was refused."""
    from ..trust import StaleContent

    element = candidate.element
    trust = candidate.trust
    try:
        text = echoable_text(config, source, element, trust)
    except StaleContent as exc:
        if rederive is None:
            logger.info(
                "not echoing element %r: it was edited since it was classified", exc.element_id
            )
            return None, (
                f"element {exc.element_id!r} was edited since its class was derived and "
                "there is nothing here to re-derive it with, so its text is not echoed"
            )
        try:
            text = echoable_text(config, source, element, rederive(element))
        except StaleContent as second:
            # Edited again while being re-derived. One retry, not a loop: an
            # element being edited faster than it can be classified is a reason to
            # echo nothing, not a reason to keep asking.
            return None, (
                f"element {second.element_id!r} was edited again while its class was "
                "being re-derived, so its text is not echoed"
            )
        except Exception as exc2:  # noqa: BLE001 - a re-derive seam has its own faults
            logger.warning("could not re-derive trust for element %r: %s", element.element_id, exc2)
            return None, (
                f"the class for element {element.element_id!r} could not be re-derived "
                f"after an edit, so its text is not echoed: {exc2}"
            )
    if text is None:
        return None, (
            f"echo is not permitted for the {candidate.trust.class_name!r} class on source "
            f"{source!r}, so this element's text is not put where a command can read it"
        )
    if not text.strip():
        # Nothing to echo, and a blank value is not a value: carrying it would
        # substitute an empty argument into a command that asked for text.
        return None, "the element's text is blank, so there is nothing to echo"
    return text, ""
