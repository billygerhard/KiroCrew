"""A watched item: seven fields of external text the engine carries, never runs.

Every string on a :class:`WatchedItem` was authored outside this machine, most
often by whoever opened an issue on a tracker anyone may write to. The engine
stores it, shows it, and hands it to a model as quoted data. It is never a
command, never a template, and never an instruction, and nothing here parses it
looking for meaning.

The field set is fixed because those are the questions a dispatch decision asks:
which item is this (``identifier``), what is it about (``title``, ``body``), is
it still open (``state``), where does a human go to see it (``address``), what
kind of work is it (``classification``), who asked (``submitter``), and what
standing does the tracker say that person has (``association``). A source that
reports more is welcome to; the mapping picks these out and drops the rest, so a
tracker adding a field cannot change what a dispatch decision reads.

``association`` is the tracker's own author-association text, and it is the only
field read as a *statement about the author* rather than about the work. It is
still untrusted text: it is matched against a fixed vocabulary, and text outside
that vocabulary yields the least-trusted class rather than a guess.

``identifier`` is the one field with no empty answer. It is the key the claim
ledger dedupes on, so an item without one cannot be dispatched at most once —
it can only be dispatched every poll or never. An item that fails to yield one
is reported as rejected rather than carried with a blank.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

#: The fields a watch source's mapping yields, in reporting order.
ITEM_FIELDS: tuple[str, ...] = (
    "identifier",
    "title",
    "body",
    "state",
    "address",
    "classification",
    "submitter",
    "association",
)

#: Fields that must resolve to a non-blank value for an item to be usable.
REQUIRED_ITEM_FIELDS: tuple[str, ...] = ("identifier",)

#: Fields the content digest is taken over: the ones that would change what a
#: run was given. ``title`` and ``body`` are the quoted data a run authors from;
#: ``classification`` selects the spec type, so an edit to it would author a
#: different kind of spec. The other fields do not: ``identifier`` is the claim
#: key (an item that changed it is a different item, not an edit of this one),
#: ``state`` drives the lifecycle transition rather than the content, ``address``
#: is where a human looks, and ``submitter``/``association`` drive the trust
#: class, which is re-derived on its own path. A field added to a source's
#: mapping does not widen this set.
CONTENT_DIGEST_FIELDS: tuple[str, ...] = ("title", "body", "classification")


@dataclass(frozen=True)
class WatchedItem:
    """One item a watch source reported, with its fields already mapped.

    Frozen because an item is a snapshot of what a poll saw. A later poll that
    finds different text is a new observation to compare against this one, not
    an edit to apply to it: the comparison is what tells the engine an item was
    reopened, closed, or rewritten after a trust decision was made about it.
    """

    source: str
    identifier: str
    title: str = ""
    body: str = ""
    state: str = ""
    address: str = ""
    classification: str = ""
    submitter: str = ""
    association: str = ""

    def __post_init__(self) -> None:
        if not self.source.strip():
            raise ValueError("a watched item must name the source that reported it")
        if not self.identifier.strip():
            raise ValueError("a watched item must carry a non-blank identifier")

    @property
    def fields(self) -> dict[str, str]:
        """The mapped fields, keyed by engine field name."""
        return {name: getattr(self, name) for name in ITEM_FIELDS}

    @property
    def content_digest(self) -> str:
        """A stable hash over the fields that would change what a run was given.

        The one function that computes an item's digest, so the value written to
        a snapshot row and the value a later poll compares against it are the
        same by construction: a second spelling of this would let one poll record
        a digest a later one could never match, reporting an edit on every
        subsequent poll. The fields are length-prefixed before hashing so no
        pair of values can be rearranged across the field boundary into the same
        digest -- ``title='ab', body='c'`` must not collide with
        ``title='a', body='bc'``.

        Never reversible and never the text: the digest is what lets the engine
        notice an edit without keeping a second copy of an attacker-controlled
        body in its state store.
        """
        hasher = hashlib.sha256()
        for name in CONTENT_DIGEST_FIELDS:
            encoded = getattr(self, name).encode("utf-8")
            hasher.update(f"{name}:{len(encoded)}:".encode("ascii"))
            hasher.update(encoded)
        return hasher.hexdigest()
