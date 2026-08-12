"""Delivering a notice through the host gateway's notification bus.

This app builds no transport of its own. The host already owns one entry point
for notification delivery — the notification bus, which fans a note out to the
dashboard feed, the OS notification centre, and every connected surface — so
routing here means resolving a channel, shaping a payload the bus accepts, and
pushing. What this module adds on top of the bus is the part the bus cannot know:

**A channel is not a free string.** Only a channel this app declares is ever
registered on the bus, so a configuration document cannot invent a namespace by
naming one. :mod:`.channels` owns that catalog and the fallback.

**Everything outbound is treated as untrusted.** A notice quotes a spec name, a
watched item's title, a provider's error, or a reviewer's comment. That text
lands in a persisted note, in a desktop toast, and on every dashboard the
gateway serves — an egress boundary — so it is redacted, stripped of control
characters, clipped, and confined:

* a title is a single line, because a newline in a title forges a second row in
  a feed that renders one line per note;
* untrusted spans are fenced rather than interpolated, so ``## Done`` in an
  issue title cannot become a heading in the body;
* detail travels under an engine-owned key prefix, so no detail key can occupy a
  note field the bus schema owns now or adds later.

**Delivery failure is the caller's to record, so it is raised, not swallowed.**
Run state is primary and delivery is best-effort — the run lifecycle and the
budget ceiling each already catch, log, and audit a failed notification, and one
of them records ``notified=False`` on the notice it returns. Returning False
here instead of raising would leave those recorders with nothing to catch and
turn an undelivered notice into a silent one.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol

from ..config.store import APP_NAME, ConfigStore
from .channels import ChannelRoute, resolve_channel, resolve_requested

logger = logging.getLogger(__name__)

#: Bus caps are 500 and 20000. Staying well under them means long text is
#: clipped here, where the clip can end on an ellipsis, rather than refused
#: there, where the whole notice would be lost.
MAX_TITLE_CHARS = 200
MAX_BODY_CHARS = 4000

#: Cap on detail entries carried into note meta. Detail is diagnostic context,
#: not a payload channel, and an unbounded map would let one notice bloat every
#: persisted row and every dashboard response that reads it.
MAX_DETAIL_KEYS = 24
MAX_DETAIL_VALUE_CHARS = 200

#: Prefix every detail key carries into note meta. The bus already refuses meta
#: keys that collide with its schema, are underscore-prefixed, or are reserved;
#: namespacing on top means an engine detail key cannot collide with a note
#: field the schema gains later either, without this module tracking that schema.
DETAIL_PREFIX = "spec_"

#: Title used when a caller supplies a message with no usable first line. The
#: bus requires a non-empty title, and refusing the notice over a formatting
#: detail would lose the thing somebody needs to hear.
FALLBACK_TITLE = "Spec engine"

#: Anything that is not printable text: C0 and C1 controls, plus the delete
#: character. Newlines are handled separately — kept in a body, flattened in a
#: title — so they are excluded from this class.
_CONTROL_CHARS = re.compile(r"[\x00-\x09\x0b-\x1f\x7f-\x9f]")

#: Runs of backticks, to pick a code fence longer than any inside the content.
_BACKTICK_RUN = re.compile(r"`+")

#: Detail keys are identifiers. A key with a space, a dot, or a control
#: character in it would be an operator-visible field name assembled from
#: untrusted text, which is a place attacker-authored strings do not belong.
_DETAIL_KEY = re.compile(r"^[a-z][a-z0-9_]*$")


class NotificationUndelivered(RuntimeError):
    """Raised when a notice could not be handed to the host bus."""


class Bus(Protocol):
    """The slice of the host notification bus this module uses."""

    def is_registered(self, channel: str) -> bool: ...

    def register_channel(self, channel: str, default_priority: str = ...) -> None: ...

    def push(self, payload: Any) -> dict[str, Any]: ...


class RateLimiter(Protocol):
    """The host's per-app notification token bucket."""

    def allow(self, app_name: str) -> bool: ...

    def refund(self, app_name: str) -> None: ...


def bus_from_state(state: Any | None) -> Any | None:
    """Pull the live notification bus off gateway state, tolerating its absence.

    Threaded in rather than reached for globally because the gateway keeps state
    per application object, and an explicit dependency is what lets every
    delivery path be tested without a gateway. Absent outside the gateway
    process — a CLI run or a test holds no state — which is a runtime condition,
    not a misconfiguration.
    """
    return getattr(state, "notification_bus", None) if state is not None else None


def limiter_from_state(state: Any | None) -> Any | None:
    """Pull the host's notification rate limiter off gateway state.

    The same instance the HTTP producer endpoint consumes from, so the
    in-process path shares one budget with it instead of opening a second.
    """
    return getattr(state, "notification_rate_limiter", None) if state is not None else None


def _redacted(text: str) -> str:
    """Redact credentials and exfiltration URLs through the platform shim.

    Deferred import: the redaction stack is large and only an outbound notice
    needs it, so importing it here keeps it off this module's load path.
    """
    if not text:
        return text
    from kiro_crew.platform.context import redact_via_context

    return redact_via_context(text)


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def safe_line(text: str, *, limit: int = MAX_TITLE_CHARS) -> str:
    """One line of display text: redacted, flattened, and clipped.

    Flattening is the structural part. A feed and a desktop toast both render a
    title as one line, so a newline inside it would let quoted text end the
    real title and start a line of its own choosing.
    """
    flattened = _CONTROL_CHARS.sub(" ", _redacted(text).replace("\n", " ").replace("\r", " "))
    return _clip(" ".join(flattened.split()), limit)


def safe_block(text: str, *, limit: int = MAX_BODY_CHARS) -> str:
    """Multi-line display text: redacted, control-stripped, and clipped.

    Line structure survives because a body is read as prose; everything that is
    not text does not.
    """
    normalized = _redacted(text).replace("\r\n", "\n").replace("\r", "\n")
    return _clip(_CONTROL_CHARS.sub(" ", normalized), limit)


def quote_untrusted(text: str, *, limit: int = MAX_BODY_CHARS) -> str:
    """Fence *text* so it cannot forge structure in the note that carries it.

    Watched-item titles, provider errors, and reviewer comments are authored by
    someone other than the operator reading the notice. Interpolated into a body
    that a surface renders as markdown, ``## Approved`` or a crafted link would
    become structure the engine never wrote. A code fence has no such escape as
    long as the fence is longer than any backtick run inside the content, which
    is what the fence length is computed from — so the worst a crafted title can
    do is look like a crafted title.

    Returns the empty string for empty content: an empty fence is a visual
    artifact that says nothing.
    """
    body = safe_block(text, limit=limit)
    if not body:
        return ""
    longest = max((len(run.group()) for run in _BACKTICK_RUN.finditer(body)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}\n{body}\n{fence}"


def safe_detail(detail: Mapping[str, Any] | None) -> dict[str, Any]:
    """Namespace, filter, and bound *detail* for the note's meta map."""
    if not detail:
        return {}
    safe: dict[str, Any] = {}
    for key, value in detail.items():
        if len(safe) >= MAX_DETAIL_KEYS:
            logger.debug("notification detail truncated at %d keys", MAX_DETAIL_KEYS)
            break
        if not isinstance(key, str) or not _DETAIL_KEY.match(key):
            continue
        safe[f"{DETAIL_PREFIX}{key}"] = _safe_detail_value(value)
    return safe


def _safe_detail_value(value: Any) -> Any:
    """Reduce one detail value to something bounded and free of credentials."""
    if isinstance(value, bool) or isinstance(value, (int, float)) or value is None:
        return value
    if isinstance(value, str):
        return safe_line(value, limit=MAX_DETAIL_VALUE_CHARS)
    try:
        rendered = json.dumps(value, default=str, sort_keys=True)
    except (TypeError, ValueError):  # pragma: no cover - default=str covers the field
        rendered = str(value)
    return safe_line(rendered, limit=MAX_DETAIL_VALUE_CHARS)


@dataclass(frozen=True)
class Delivery:
    """What happened to one notice: where it went, and what the bus was told."""

    route: ChannelRoute
    title: str
    body: str
    priority: str
    group_key: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def channel(self) -> str:
        """Host bus channel the notice was pushed to."""
        return self.route.bus_channel


class StallLike(Protocol):
    """The slice of a stall notice this module reads.

    Structural rather than an import of the run lifecycle's dataclass: routing
    needs a channel, a line of text, and an identifier to group by, and naming
    only those keeps a notifier usable by anything that produces a notice.

    ``channel`` is a read-only property because the notice it describes is a
    frozen dataclass. Declared as a plain attribute the protocol would demand a
    settable one, and the notifier would then not satisfy the seam it exists to
    fill — a mismatch the type checker catches and no test would.
    """

    @property
    def channel(self) -> str: ...

    def message(self) -> str: ...


class HostNotifier:
    """Routes engine notices to the host bus. Satisfies the engine's notifier seams.

    One object rather than one adapter per caller: the run lifecycle calls a
    notifier, the budget ceiling calls ``notify(channel=, message=, detail=)``,
    and both mean "tell somebody". Splitting them would duplicate the channel
    resolution and the sanitizing, and two copies of a sanitizer drift.
    """

    def __init__(
        self,
        config: ConfigStore,
        *,
        project: str | None = None,
        state: Any | None = None,
        bus: Any | None = None,
        limiter: Any | None = None,
    ) -> None:
        self._config = config
        self._project = project
        resolved_bus = bus if bus is not None else bus_from_state(state)
        resolved_limiter = limiter if limiter is not None else limiter_from_state(state)
        self._bus: Bus | None = resolved_bus
        self._limiter: RateLimiter | None = resolved_limiter

    @property
    def available(self) -> bool:
        """Whether a bus exists to deliver through in this process."""
        return self._bus is not None

    def route(self) -> ChannelRoute:
        """The channel this notifier's project delivers to."""
        return resolve_channel(self._config, project=self._project)

    def send(
        self,
        title: str,
        body: str = "",
        *,
        quoted: str = "",
        channel: str = "",
        priority: str | None = None,
        group_key: str = "",
        detail: Mapping[str, Any] | None = None,
        url: str | None = None,
        ttl: int | None = None,
    ) -> Delivery:
        """Push one notice, raising :class:`NotificationUndelivered` on failure.

        *quoted* carries text this app did not author; it is fenced into the
        body rather than interpolated into it.
        """
        route = resolve_requested(self._config, channel, project=self._project)
        safe_title = safe_line(title) or FALLBACK_TITLE
        safe_body = _compose_body(body, quoted)
        resolved_priority = priority or route.default_priority
        delivery = Delivery(
            route=route,
            title=safe_title,
            body=safe_body,
            priority=resolved_priority,
            group_key=safe_line(group_key, limit=MAX_DETAIL_VALUE_CHARS),
            detail=safe_detail(detail),
        )
        self._push(delivery, url=url, ttl=ttl)
        return delivery

    # ------------------------------------------------------- notifier seams

    def notify(self, *, channel: str, message: str, detail: dict[str, Any]) -> None:
        """The budget ceiling's notifier shape: a channel, a message, and detail.

        The message is one operator-facing sentence, so its opening clause makes
        the title and the whole of it makes the body — a title synthesized from
        elsewhere would say less than the sentence already does.
        """
        self.send(
            _title_from(message),
            message,
            channel=channel,
            group_key=str(detail.get("run", "")) if detail else "",
            detail=detail,
        )

    def __call__(self, notice: StallLike) -> None:
        """The run lifecycle's notifier shape: one notice object.

        Grouped by run, so a run that stalls, resumes, and stalls again collapses
        into one stack in the feed rather than a column of near-identical rows.

        The optional fields are read with ``getattr`` on purpose: the protocol
        names only what routing needs, so a caller with a leaner notice still
        gets delivery instead of an attribute error.
        """
        message = notice.message()
        detail = getattr(notice, "to_json_object", None)
        rendered = detail() if callable(detail) else None
        self.send(
            _title_from(message),
            message,
            channel=str(getattr(notice, "channel", "")),
            group_key=str(getattr(notice, "run_id", "")),
            detail=rendered if isinstance(rendered, Mapping) else None,
        )

    # ------------------------------------------------------------- internals

    def _push(self, delivery: Delivery, *, url: str | None, ttl: int | None) -> None:
        """Hand *delivery* to the bus, registering its channel on first use."""
        bus = self._bus
        if bus is None:
            # Logged rather than dropped: outside the gateway there is no bus to
            # reach, and the operator's log is the only surface left.
            logger.warning("[%s] %s", delivery.channel, delivery.title)
            raise NotificationUndelivered("no notification bus in this process")
        limiter = self._limiter
        if limiter is not None and not limiter.allow(APP_NAME):
            raise NotificationUndelivered("notification rate limit exhausted")
        try:
            self._register(bus, delivery)
            bus.push(self._payload(delivery, url=url, ttl=ttl))
        except NotificationUndelivered:
            self._refund(limiter)
            raise
        except Exception as exc:
            self._refund(limiter)
            raise NotificationUndelivered(str(exc)) from exc

    def _refund(self, limiter: RateLimiter | None) -> None:
        """Return the token a non-delivering attempt consumed.

        The budget caps delivered notifications, so a refusal that reached
        nobody must not spend from it.
        """
        if limiter is None:
            return
        try:
            limiter.refund(APP_NAME)
        except Exception:  # pragma: no cover - a refund failure is not the notice's problem
            logger.debug("notification token refund failed", exc_info=True)

    @staticmethod
    def _register(bus: Bus, delivery: Delivery) -> None:
        """Register the channel once, on its first push.

        Once, not on every push: re-registering would stomp a priority an
        operator overrode at runtime.
        """
        if not bus.is_registered(delivery.channel):
            bus.register_channel(delivery.channel, delivery.route.default_priority)

    def _payload(self, delivery: Delivery, *, url: str | None, ttl: int | None) -> Any:
        """Build the bus payload. Imported here so the bus is a runtime dependency."""
        from kiro_crew.notifications.bus import NotificationPayload

        return NotificationPayload(
            source=APP_NAME,
            channel=delivery.channel,
            title=delivery.title,
            body=delivery.body,
            priority=delivery.priority,
            group_key=delivery.group_key or None,
            url=url,
            ttl=ttl,
            meta=dict(delivery.detail),
        )


def _compose_body(body: str, quoted: str) -> str:
    """Join engine-authored prose with a fenced block of text it did not author."""
    parts = [part for part in (safe_block(body), quote_untrusted(quoted)) if part]
    return "\n\n".join(parts)


def _title_from(message: str) -> str:
    """The first sentence-ish clause of *message*, for a notice with no title.

    Sanitizing happens downstream in :func:`safe_line`; this only chooses how
    much of the message to take.
    """
    first_line = message.strip().splitlines()[0] if message.strip() else ""
    return first_line or FALLBACK_TITLE
