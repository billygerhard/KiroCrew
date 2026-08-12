"""Notification routing: which host channel a notice lands on, and how it gets there.

The app delivers nothing itself. The host gateway owns one notification entry
point and this package resolves a destination for it, per project, and shapes a
payload that carries untrusted text without letting it forge structure. Import
from this package rather than its modules.

    from ...engine.notify import HostNotifier

    notifier = HostNotifier(config, project="acme", state=gateway_state)
    machine = RunMachine(store, config, project="acme", notifier=notifier)

The split follows what each part answers:

* :mod:`.channels` — where a notice goes: the channels this app declares, the
  per-project selection, and the dashboard fallback for a project that named
  none or named one that does not exist.
* :mod:`.routing` — how it gets there: sanitizing, fencing untrusted spans,
  registering the channel on the host bus, and pushing.
"""

from __future__ import annotations

from .channels import (
    CHANNEL_SETTING,
    CHANNELS,
    DASHBOARD_CHANNEL,
    REASON_MISMATCH,
    REASON_UNDECLARED,
    REASON_UNREADABLE,
    REVIEW_CHANNEL,
    ChannelRoute,
    bus_channel,
    dashboard_route,
    declared_channels,
    known_channel,
    resolve_channel,
    resolve_requested,
)
from .routing import (
    DETAIL_PREFIX,
    FALLBACK_TITLE,
    MAX_BODY_CHARS,
    MAX_DETAIL_KEYS,
    MAX_DETAIL_VALUE_CHARS,
    MAX_TITLE_CHARS,
    Bus,
    Delivery,
    HostNotifier,
    NotificationUndelivered,
    RateLimiter,
    StallLike,
    bus_from_state,
    limiter_from_state,
    quote_untrusted,
    safe_block,
    safe_detail,
    safe_line,
)

__all__ = [
    "CHANNELS",
    "CHANNEL_SETTING",
    "DASHBOARD_CHANNEL",
    "DETAIL_PREFIX",
    "FALLBACK_TITLE",
    "MAX_BODY_CHARS",
    "MAX_DETAIL_KEYS",
    "MAX_DETAIL_VALUE_CHARS",
    "MAX_TITLE_CHARS",
    "REASON_MISMATCH",
    "REASON_UNDECLARED",
    "REASON_UNREADABLE",
    "REVIEW_CHANNEL",
    "Bus",
    "ChannelRoute",
    "Delivery",
    "HostNotifier",
    "NotificationUndelivered",
    "RateLimiter",
    "StallLike",
    "bus_channel",
    "bus_from_state",
    "dashboard_route",
    "declared_channels",
    "known_channel",
    "limiter_from_state",
    "quote_untrusted",
    "resolve_channel",
    "resolve_requested",
    "safe_block",
    "safe_detail",
    "safe_line",
]
