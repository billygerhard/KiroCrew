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

Nothing in production calls this gate yet, because the run-context fields that
carry tracker text into a writeback command are not populated on the live path;
wiring that is a later task. The gate is the mechanism; a permitted-class check
that no writeback consults would republish untrusted text regardless, so the gate
exists ahead of its caller rather than being folded into a path that cannot yet
exercise it.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING

from ..config import ConfigStore
from ..config.schema import LEAST_TRUSTED_CLASS, SECTION_SOURCES, WILDCARD_KEY

if TYPE_CHECKING:
    # Annotations only. ``trust`` imports ``watch.dispatch``, so importing it at
    # module load would re-enter a partially-initialized ``trust`` through the
    # watch package's __init__; the runtime need (``consume``) is imported inside
    # the one function that uses it, the same way ``watch.screening`` does.
    from ..trust import ContentElement, ElementTrust

logger = logging.getLogger(__name__)

__all__ = [
    "ECHO_FIELD",
    "echo_permitted_for",
    "echoable_text",
]

#: The per-submitter-class echo-permission map on a watch source. Echo is off for
#: every class unless this map turns a class on explicitly, and the least-trusted
#: class is refused even when it does.
ECHO_FIELD = "echo"


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
