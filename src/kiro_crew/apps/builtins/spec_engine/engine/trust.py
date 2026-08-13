"""Per-element authorship and trust derivation.

An item is not one piece of text by one author. A public tracker item carries a
body written by whoever opened it and comments written by anyone at all, and a
review artifact carries comments the same way. Trusting a comment because the
item it sits on was opened by a maintainer is the whole defect this module
exists to prevent: commenting on a maintainer's issue would borrow the
maintainer's trust, and the autonomy ladder is selected by that class.

So every consumed element is classified by **its own** author, through the same
:func:`~.watch.dispatch.class_of_author` the item body goes through. There is
deliberately no second derivation here. A second one would be a second spelling
of one guarantee, which is the shape every security finding in this codebase has
had so far.

**A class is about a revision, not about an element.** An author can edit a
comment after it was classified, and the class on file then describes text that
is no longer there. :func:`reconcile` compares the element's current revision
against the recorded one and reports the change; :func:`consume` refuses text
whose trust was derived from a different revision, so a caller cannot use edited
content under the old decision by forgetting to re-derive. The refusal is the
mechanism -- an unchecked "please re-derive" comment would not be one.

**Trust configuration is configuration only.** The maintainer list lives under
``sources``, which is fenced in :data:`~.config.schema.CONFIG_ONLY_PATHS`, and
the association vocabulary is a module constant. Neither is reachable from a
tool, so no Engine_MCP_Server surface can widen who counts as a maintainer.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import Enum
from typing import Any

from .audit import AuditLog
from .state import ElementTrustRecord, SpecRef, StateStore
from .watch.dispatch import SourceRoute, SubmitterClass, class_of_author

__all__ = [
    "AUDIT_ELEMENT_TRUST",
    "ContentElement",
    "ElementKind",
    "ElementTrust",
    "Reconciliation",
    "StaleContent",
    "consume",
    "derive",
    "reconcile",
    "record_gated_decision",
]


#: Audit event name for a decision gated on an element's class.
AUDIT_ELEMENT_TRUST = "element.trust"

#: Length of the derived content revision. Long enough that two revisions of one
#: comment do not collide in practice, short enough to read in an audit line.
_REVISION_CHARS = 16


class ElementKind(str, Enum):
    """Which authored surface an element came from.

    Kept as a closed set because each kind names a different place an author can
    put text the engine will read, and a new one is a new intake path that has to
    be classified rather than assumed.
    """

    #: The item's own body, authored by the submitter who opened it.
    ITEM_BODY = "item_body"
    #: A comment on the item, authored by whoever wrote that comment.
    ITEM_COMMENT = "item_comment"
    #: A comment on a review artifact, such as a pull request review thread.
    REVIEW_COMMENT = "review_comment"


class StaleContent(Exception):
    """Content was offered under a class derived from a different revision.

    Raised rather than returned. A caller that holds an :class:`ElementTrust`
    from before an edit is holding a decision about text it no longer has, and
    the safe outcome is that its operation fails and re-derives -- not that it
    receives a flag it may not read.
    """

    def __init__(self, element_id: str, *, held: str, current: str) -> None:
        super().__init__(
            f"element {element_id!r} was classified at revision {held!r} "
            f"but its current revision is {current!r}: re-derive before using it"
        )
        self.element_id = element_id
        self.held = held
        self.current = current


@dataclass(frozen=True)
class ContentElement:
    """One authored piece of text, with the author of that text.

    *author* is the author of this element, never of the item or artifact it sits
    on. *association* is what the tracker asserts about this author, and it is
    per element for the same reason -- a maintainer's issue can carry a
    first-time contributor's comment.

    *revision* is the tracker's own revision for this element when it reports
    one. When it does not, :attr:`content_revision` digests the text instead, so
    an edit is still detectable. A blank revision is never used: it would make
    every edit invisible, which is the one failure this whole mechanism is for.
    """

    kind: ElementKind
    element_id: str
    author: str = ""
    association: str = ""
    text: str = ""
    revision: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ElementKind):
            raise ValueError(f"unknown content element kind: {self.kind!r}")
        if not self.element_id.strip():
            raise ValueError("a content element must carry a non-blank identifier")

    @property
    def content_revision(self) -> str:
        """The revision this element's text is at.

        The tracker's revision when it gave one, because it distinguishes an edit
        that restored earlier text; otherwise a digest of the text itself.
        """
        declared = self.revision.strip()
        if declared:
            return declared
        digest = hashlib.sha256(self.text.encode("utf-8", "surrogatepass")).hexdigest()
        return f"sha256:{digest[:_REVISION_CHARS]}"

    @property
    def has_author(self) -> bool:
        return bool(self.author.strip())


@dataclass(frozen=True)
class ElementTrust:
    """The class derived for one element, at one revision, from its own author.

    Carries the revision so that holding this object is not the same as being
    entitled to use the element's current text -- see :func:`consume`.
    """

    kind: ElementKind
    element_id: str
    author: str
    revision: str
    submitter_class: SubmitterClass

    @property
    def class_name(self) -> str:
        return self.submitter_class.name

    @property
    def is_determined(self) -> bool:
        return self.submitter_class.is_determined

    def detail(self) -> dict[str, Any]:
        """What a gated decision records: the class, the author, the revision.

        All three, because the class alone does not say who it was about and the
        author alone does not say which text they wrote. An operator reading back
        a decision needs to be able to find the exact content it relied upon.
        """
        return {
            "element_kind": self.kind.value,
            "element_id": self.element_id,
            "element_author": self.author,
            "content_revision": self.revision,
            "submitter_class": self.class_name,
            "class_evidence": self.submitter_class.evidence.value,
            "class_declared_at": self.submitter_class.declared_at,
        }

    def describe(self) -> str:
        author = self.author.strip() or "(unattributed)"
        return f"{self.kind.value} {self.element_id} by {author}: {self.submitter_class.describe()}"


@dataclass(frozen=True)
class Reconciliation:
    """The current trust for an element, and whether it moved since last time.

    *changed* is true when the element is at a revision the engine has not
    classified. That is the signal to re-apply every decision gated on the class,
    and it is true for a first sighting as well as an edit: neither has a
    standing decision behind it.
    """

    trust: ElementTrust
    changed: bool
    previous: ElementTrustRecord | None = None

    @property
    def previous_revision(self) -> str:
        return self.previous.revision if self.previous is not None else ""

    @property
    def previous_class(self) -> str:
        return self.previous.class_name if self.previous is not None else ""

    @property
    def class_moved(self) -> bool:
        """Whether the class itself differs, not merely the revision.

        A re-derivation is required either way. This distinguishes an edit that
        also changed who is trusted -- an element reassigned to another author --
        from one that only changed the words.
        """
        return self.previous is not None and self.previous.class_name != self.trust.class_name


def derive(route: SourceRoute, element: ContentElement) -> ElementTrust:
    """Classify *element* by its own author.

    An element with no determinable author gets the least-trusted class, which is
    what :func:`~.watch.dispatch.class_of_author` returns for a blank author with
    no recognized association. That is stated here rather than special-cased,
    because a second unattributed-author branch is a second place the rule could
    drift from the one the item body goes through.
    """
    return ElementTrust(
        kind=element.kind,
        element_id=element.element_id,
        author=element.author,
        revision=element.content_revision,
        submitter_class=class_of_author(route, element.author, element.association),
    )


def reconcile(
    state: StateStore,
    route: SourceRoute,
    element: ContentElement,
    *,
    scope: str = "",
) -> Reconciliation:
    """Derive *element*'s class now and compare it with what was recorded.

    *scope* is what consumed the element -- a watch source, or a run for review
    comments. It defaults to the route's source. Scoping the record means two
    runs reading the same item do not silently share one revision cursor, so
    neither can consume edited text under the other's re-derivation.

    Recording happens here rather than in :func:`derive` so that classification
    stays a pure function callers can use for a question, while the durable "this
    revision has been classified" claim is made only by the path that also
    reports whether it moved.
    """
    consumer = scope.strip() or route.source
    trust = derive(route, element)
    previous = state.get_element_trust(consumer, element.element_id)
    changed = previous is None or previous.revision != trust.revision
    state.record_element_trust(
        consumer,
        element_id=trust.element_id,
        kind=trust.kind.value,
        author=trust.author,
        revision=trust.revision,
        class_name=trust.class_name,
        evidence=trust.submitter_class.evidence.value,
    )
    return Reconciliation(trust=trust, changed=changed, previous=previous)


def consume(element: ContentElement, trust: ElementTrust) -> str:
    """Return *element*'s text, refusing if *trust* is about other text.

    The gate is the revision rather than the identifier: an element keeps its id
    and its author across an edit, so those matching proves nothing about the
    words. Callers reach content through this function so that "re-derive before
    using changed content" is enforced at the point of use instead of asked for
    in a docstring.
    """
    if trust.element_id != element.element_id:
        raise StaleContent(element.element_id, held=trust.revision, current=element.content_revision)
    current = element.content_revision
    if trust.revision != current:
        raise StaleContent(element.element_id, held=trust.revision, current=current)
    return element.text


def record_gated_decision(
    audit: AuditLog,
    ref: SpecRef,
    decision: str,
    trust: ElementTrust,
    *,
    run: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    """Audit one decision gated on an element's class.

    *decision* names what was gated, and the element's class, author and revision
    travel with it. Recording the class without the revision would leave an
    operator unable to tell which version of a comment a decision was made
    about, which is the question an edited comment raises.
    """
    if not decision.strip():
        raise ValueError("a gated decision must name what was decided")
    payload: dict[str, Any] = {"decision": decision, **trust.detail()}
    if detail:
        # The caller's own fields go under a sub-key so that no supplied name can
        # overwrite the trust fields this record exists to carry.
        payload["context"] = dict(detail)
    audit.append(ref, AUDIT_ELEMENT_TRUST, run=run, initiator=None, detail=payload)
