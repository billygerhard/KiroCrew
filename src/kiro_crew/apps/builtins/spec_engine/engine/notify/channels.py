"""Which host channel a project's notifications land on, and why that one.

The host gateway owns notification delivery; this app only decides *where* a
notice goes. A channel is named by configuration, per project, and the app
declares the set of channels it can name. Two rules make that safe and useful:

**A configured channel is honoured.** A project that names a channel gets that
channel. Routing that quietly sent everything to one place would make the
setting a lie, and a lie that only shows up when somebody is waiting on a
notification that went elsewhere.

**An unnamed channel resolves to the dashboard.** The dashboard notification
feed is the one destination every install has, with no credential and no
inbound URL, so a project nobody configured still reaches a person.

**A channel this app does not declare resolves to the dashboard too, and says
so.** The channel id is untrusted text: it comes from a configuration document
the setting registry types as a free string, and it ends up as the channel
namespace the host bus registers. Refusing an undeclared id keeps that string
out of a control position, and falling back rather than raising keeps a typo
in one project's config from swallowing the notice that would have told
somebody about it. The substitution is recorded on the route so a surface can
show what happened instead of leaving an operator to wonder why their channel
is empty.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace

from ..config.effective import ValueOrigin
from ..config.store import APP_NAME, ConfigStore

logger = logging.getLogger(__name__)

#: Setting naming the channel notifications route to. Owned by the setting
#: registry; named here so the routing path holds no literal of its own.
CHANNEL_SETTING = "notify.channel"

#: The dashboard notification feed. Present on every install, which is what
#: makes it the answer for a project that configured nothing.
DASHBOARD_CHANNEL = "dashboard"

#: Runs parked until a person acts. Separate from the dashboard channel because
#: the bus assigns priority per channel: a project whose notices always mean
#: "somebody is blocked" can route here and have them arrive as critical,
#: without every stall report ringing the same way.
REVIEW_CHANNEL = "review"

#: Channels this app declares, mapped to the priority the bus gives a notice
#: that does not ask for one. Keys are ids as configuration names them; the bus
#: sees them namespaced by app.
CHANNELS: dict[str, str] = {
    DASHBOARD_CHANNEL: "default",
    REVIEW_CHANNEL: "critical",
}

#: Substituted-channel reasons, recorded on the route rather than phrased at
#: each call site so two surfaces cannot describe the same fallback differently.
REASON_UNDECLARED = "undeclared_channel"
REASON_UNREADABLE = "config_unreadable"
REASON_MISMATCH = "caller_channel_ignored"


def known_channel(channel_id: str) -> bool:
    """Whether *channel_id* is a channel this app declares."""
    return channel_id in CHANNELS


def bus_channel(channel_id: str) -> str:
    """The host bus name for *channel_id*, namespaced by this app.

    The bus namespaces every app channel as ``<app>.<id>`` so one app cannot
    push into another's channels, nor into the reserved system ones.
    """
    return f"{APP_NAME}.{channel_id}"


@dataclass(frozen=True)
class ChannelRoute:
    """The channel in force for a project, and where the choice came from."""

    channel_id: str
    origin: ValueOrigin
    #: Dotted configuration path the channel was declared at, empty when the
    #: route is the bundled default.
    declared_at: str = ""
    #: What configuration asked for. Differs from ``channel_id`` only when the
    #: request could not be honoured.
    requested: str = ""
    #: Why the requested channel was not used, empty when it was.
    reason: str = ""

    @property
    def bus_channel(self) -> str:
        """Host bus channel this route delivers to."""
        return bus_channel(self.channel_id)

    @property
    def default_priority(self) -> str:
        """Priority the bus gives a notice on this channel that names none."""
        return CHANNELS[self.channel_id]

    @property
    def configured(self) -> bool:
        """Whether a human named this channel, as opposed to it being the default."""
        return self.origin is not ValueOrigin.BUNDLED_DEFAULT

    @property
    def substituted(self) -> bool:
        """Whether the requested channel was replaced by the fallback."""
        return bool(self.reason)


def dashboard_route(*, requested: str = "", reason: str = "") -> ChannelRoute:
    """The fallback route, optionally recording what it replaced."""
    return ChannelRoute(
        channel_id=DASHBOARD_CHANNEL,
        origin=ValueOrigin.BUNDLED_DEFAULT,
        requested=requested or DASHBOARD_CHANNEL,
        reason=reason,
    )


def resolve_channel(store: ConfigStore, *, project: str | None = None) -> ChannelRoute:
    """Resolve the channel *project*'s notifications route to.

    Never raises. A notification is what tells somebody the engine needs them,
    so a configuration document that cannot be read or that names a channel
    this app does not declare still yields a deliverable route.
    """
    try:
        effective = store.effective(CHANNEL_SETTING, project=project)
    except Exception as exc:  # a config problem must not silence the notice
        logger.warning("notification channel could not be read, using dashboard: %s", exc)
        return dashboard_route(reason=REASON_UNREADABLE)
    requested = str(effective.value)
    if not known_channel(requested):
        logger.warning(
            "notification channel %r is not declared by this app, using dashboard", requested
        )
        return dashboard_route(requested=requested, reason=REASON_UNDECLARED)
    return ChannelRoute(
        channel_id=requested,
        origin=effective.origin,
        declared_at=effective.declared_at,
        requested=requested,
    )


def resolve_requested(
    store: ConfigStore,
    requested: str,
    *,
    project: str | None = None,
) -> ChannelRoute:
    """The project's route, recording when a caller named a different channel.

    Callers inside the engine carry their own channel string: the run lifecycle
    and the budget ceiling each read the setting themselves and pass what they
    read. That string is a request, not the answer. Configuration decides the
    destination, so a caller holding a stale or hand-edited value cannot steer a
    notice somewhere the project did not choose — while urgency, which is a
    per-notice judgment rather than a per-project one, travels as priority.
    """
    route = resolve_channel(store, project=project)
    if not requested or requested == route.channel_id:
        return route
    if route.substituted:
        # Configuration already failed and the route says why. The caller read
        # that same configured value, so it differs from the substituted channel
        # for the router's own reason, not the caller's -- and replacing the
        # reason here would tell an operator to hunt a caller when the fix is one
        # line in their own document.
        return route
    logger.warning(
        "notification channel %r named by the caller is not the route for this project (%r)",
        requested,
        route.channel_id,
    )
    return replace(route, requested=requested, reason=REASON_MISMATCH)


def declared_channels() -> tuple[dict[str, str], ...]:
    """Every channel this app declares, for a configuration surface to render.

    Read from the catalog rather than from the bus: the bus registers a channel
    lazily on its first push, so an install that has never notified would show
    an empty list and an operator could not tell which channels exist.
    """
    return tuple(
        {"id": channel_id, "bus_channel": bus_channel(channel_id), "default_priority": priority}
        for channel_id, priority in CHANNELS.items()
    )
