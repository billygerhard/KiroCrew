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
