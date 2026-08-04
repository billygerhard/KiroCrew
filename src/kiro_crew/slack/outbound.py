"""The lifecycle of a posted Slack OPTIONS control.

Rendering text for Slack is NOT this module's job. ``slack.format`` owns that —
``render_for_slack`` for bodies and ``build_options_blocks`` for the control,
which redacts every choice through ``redact_for_display`` so a key split by ANSI,
emphasis, backticks or link markup is caught in the form Slack actually shows.
This module deliberately holds no second copy of that pipeline: an earlier
version did, and the two drifted apart until the same credential-exposure bug
had to be fixed twice, three review rounds apart.

What is left here is the part ``slack.format`` has no opinion about: a posted
control stays answerable until something spends it. ``PostedOptions`` carries
enough to find the control again, and ``expire_options`` renders it spent once
the conversation has moved past the question it asked.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from kiro_crew.slack.format import build_options_selected_blocks, replace_options_blocks
from kiro_crew.slack.retry import is_retryable_slack_error

if TYPE_CHECKING:
    from kiro_crew.slack.client import SlackClientOps

logger = logging.getLogger(__name__)

#: Notification/fallback text for the message carrying an OPTIONS control.
OPTIONS_FALLBACK_TEXT = "Options"


@dataclass(frozen=True)
class PostedOptions:
    """A posted OPTIONS control, addressed well enough to expire it later.

    ``blocks`` is the block list exactly as posted. Keeping it means expiry can
    run the same block surgery the Send button uses, editing only the OPTIONS
    block and leaving any surrounding blocks (a timing footer, a
    Link-to-Dashboard button) intact — without re-fetching the message.
    """

    channel: str
    ts: str
    choices: tuple[str, ...]
    blocks: tuple[dict, ...]
    text: str = OPTIONS_FALLBACK_TEXT


_MAX_EDIT_LOCKS = 512
_EDIT_LOCKS: dict[tuple[str, str], asyncio.Lock] = {}


def options_edit_lock(channel: str, ts: str) -> asyncio.Lock:
    """The one lock guarding edits to a single OPTIONS message.

    Two writers race for the same Slack message: this module's expiry, and the
    Send handler rewriting it with the user's selection. Without a shared lock the
    expiry's edit can land AFTER the selection's and erase the answer the user
    just gave. Slack offers no compare-and-set on an edit, so ordering has to be
    imposed here — and it can be, because both writers run in this one gateway
    process. (Two gateways driving the same workspace would defeat it; that is not
    a supported topology.)

    Holding this lock is not sufficient on its own: the expiry must ALSO re-read,
    inside the lock, whether its record is still tracked — a Send that won the
    race forgets the record, and that is what tells the expiry to skip its edit.

    Created lazily and never awaited between lookup and insert, so no registry
    lock is needed on a single-threaded event loop. Bounded: once the registry
    exceeds ``_MAX_EDIT_LOCKS``, uncontended entries are dropped, since a lock
    nobody holds carries no state worth keeping.
    """
    key = (channel, ts)
    lock = _EDIT_LOCKS.get(key)
    if lock is None:
        lock = _EDIT_LOCKS[key] = asyncio.Lock()
    if len(_EDIT_LOCKS) > _MAX_EDIT_LOCKS:
        for stale_key, stale in list(_EDIT_LOCKS.items()):
            if stale_key != key and not stale.locked():
                del _EDIT_LOCKS[stale_key]
    return lock


async def expire_options(slack: SlackClientOps, posted: PostedOptions) -> bool:
    """Render a previously-posted OPTIONS control as spent.

    Strikes every choice through, so a control the conversation has moved past
    reads as unanswerable rather than inviting a click that would answer a
    superseded question. Only the OPTIONS block is replaced; surrounding blocks
    survive.

    Returns True when the record is SETTLED and the caller should stop tracking
    it — either the edit landed, or it failed in a way that will fail identically
    forever (a deleted message, a channel we are not in, a malformed payload).
    Returns False only for a transient failure, so the caller can keep the record
    and try again on a later turn instead of leaving a live control on screen
    with nothing tracking it.

    Still never raises: a thread that keeps a stale control is bad, but it is not
    worth disrupting the turn that triggered the cleanup.
    """
    try:
        spent = build_options_selected_blocks(list(posted.choices), [])
        blocks = replace_options_blocks(list(posted.blocks), spent)
        await slack.update_message(posted.channel, posted.ts, text=posted.text, blocks=blocks)
        return True
    except Exception as exc:
        if is_retryable_slack_error(exc):
            logger.debug(
                "Slack OPTIONS control expiry failed transiently; keeping the "
                "record so a later turn retries it",
                exc_info=True,
            )
            return False
        logger.debug("Failed to expire Slack OPTIONS control", exc_info=True)
        return True
