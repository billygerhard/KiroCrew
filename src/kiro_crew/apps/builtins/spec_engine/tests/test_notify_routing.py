"""Notification routing: where a notice goes, and what it is allowed to carry.

Four claims, in the order a regression would hurt:

1. **Both directions of the channel decision.** A project that names a channel
   gets that channel, and a project that names none lands on the dashboard.
   Testing only the second would pass against routing that ignored
   configuration entirely and hardwired the dashboard, which is the shape of
   defect this file exists to catch.
2. **Delivery is the host's.** The notice reaches the host notification bus as a
   payload the bus accepts, on a channel namespaced to this app and registered
   once. No transport of this app's own.
3. **Untrusted text cannot forge structure.** A watched-item title carrying a
   newline, a markdown heading, or a credential must not produce a second feed
   row, a heading the engine never wrote, or a leaked token.
4. **An undelivered notice raises.** Both engine callers catch, log, and audit;
   one records ``notified=False`` from the exception. Swallowing failure here
   would turn a channel outage into a silent one.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.apps.builtins.spec_engine.engine.budget import Notifier as BudgetNotifier
from kiro_crew.apps.builtins.spec_engine.engine.config import DASHBOARD_SURFACE, ConfigStore
from kiro_crew.apps.builtins.spec_engine.engine.config.effective import ValueOrigin
from kiro_crew.apps.builtins.spec_engine.engine.config.store import APP_NAME
from kiro_crew.apps.builtins.spec_engine.engine.notify import (
    CHANNEL_SETTING,
    CHANNELS,
    DASHBOARD_CHANNEL,
    DETAIL_PREFIX,
    FALLBACK_TITLE,
    MAX_BODY_CHARS,
    MAX_DETAIL_KEYS,
    MAX_TITLE_CHARS,
    REASON_MISMATCH,
    REASON_UNDECLARED,
    REVIEW_CHANNEL,
    HostNotifier,
    NotificationUndelivered,
    declared_channels,
    known_channel,
    quote_untrusted,
    resolve_channel,
    safe_detail,
    safe_line,
)
from kiro_crew.apps.builtins.spec_engine.engine.runs import Notifier as RunNotifier
from kiro_crew.apps.builtins.spec_engine.engine.runs import RunMachine, RunState
from kiro_crew.apps.builtins.spec_engine.engine.state import SpecRef, StateStore
from kiro_crew.notifications.bus import (
    PRIORITIES,
    NotificationBus,
    NotificationPayload,
    NotificationValidationError,
)

from .conftest import NATIVE_SPEC_FILES, spec_dir_snapshot

PROJECT = "acme"


class FakeClock:
    """A clock the test advances, so a phase timeout fires without a sleep."""

    def __init__(self) -> None:
        self._now = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now = self._now + timedelta(seconds=seconds)


class FakeBus:
    """Records what the host bus would receive, and nothing more."""

    def __init__(self, *, fail: bool = False) -> None:
        self.pushed: list[NotificationPayload] = []
        self.registered: dict[str, str] = {}
        self.registrations: list[str] = []
        self._fail = fail

    def is_registered(self, channel: str) -> bool:
        return channel in self.registered

    def register_channel(self, channel: str, default_priority: str = "default") -> None:
        self.registered[channel] = default_priority
        self.registrations.append(channel)

    def push(self, payload: NotificationPayload) -> dict[str, Any]:
        if self._fail:
            raise NotificationValidationError("bus said no")
        self.pushed.append(payload)
        return {"channel": payload.channel}


class FakeLimiter:
    """The host's token bucket, reduced to what routing consumes from it."""

    def __init__(self, *, allow: bool = True) -> None:
        self.allowed = allow
        self.consumed: list[str] = []
        self.refunded: list[str] = []

    def allow(self, app_name: str) -> bool:
        self.consumed.append(app_name)
        return self.allowed

    def refund(self, app_name: str) -> None:
        self.refunded.append(app_name)


class FakeState:
    """The two attributes routing reads off gateway state."""

    def __init__(self, bus: Any = None, limiter: Any = None) -> None:
        self.notification_bus = bus
        self.notification_rate_limiter = limiter


class FakeNotice:
    """A stall notice's shape, without importing the run lifecycle's dataclass."""

    def __init__(self, *, channel: str = "", message: str = "run stalled", run_id: str = "run-1"):
        self.channel = channel
        self.run_id = run_id
        self._message = message

    def message(self) -> str:
        return self._message

    def to_json_object(self) -> dict[str, Any]:
        return {"run_id": self.run_id, "channel": self.channel}


@pytest.fixture()
def config(tmp_path: Path) -> ConfigStore:
    return ConfigStore(root=tmp_path / "config")


@pytest.fixture()
def bus() -> FakeBus:
    return FakeBus()


def with_project(config: ConfigStore, name: str = PROJECT, **notify: Any) -> None:
    """Declare *name* as a project, optionally pinning its notify settings."""
    entry: dict[str, Any] = {"path": f"/w/{name}"}
    if notify:
        entry["notify"] = dict(notify)
    config.write({"projects": {name: entry}}, surface=DASHBOARD_SURFACE)


class TestChannelSelection:
    """Both directions: a named channel is used, an unnamed one becomes the dashboard."""

    def test_a_project_that_names_no_channel_lands_on_the_dashboard(
        self, config: ConfigStore
    ) -> None:
        with_project(config)
        route = resolve_channel(config, project=PROJECT)
        assert route.channel_id == DASHBOARD_CHANNEL
        assert route.origin is ValueOrigin.BUNDLED_DEFAULT
        assert route.configured is False

    def test_an_install_with_no_configuration_at_all_lands_on_the_dashboard(
        self, config: ConfigStore
    ) -> None:
        assert not config.path.exists()
        assert resolve_channel(config).channel_id == DASHBOARD_CHANNEL

    def test_a_channel_named_by_a_project_is_honoured(self, config: ConfigStore) -> None:
        with_project(config, channel=REVIEW_CHANNEL)
        route = resolve_channel(config, project=PROJECT)
        assert route.channel_id == REVIEW_CHANNEL
        assert route.origin is ValueOrigin.PROJECT_CONFIG
        assert route.declared_at == f"projects.{PROJECT}.{CHANNEL_SETTING}"
        assert route.configured is True

    def test_a_channel_named_app_wide_is_honoured_without_a_project(
        self, config: ConfigStore
    ) -> None:
        config.write({"notify": {"channel": REVIEW_CHANNEL}}, surface=DASHBOARD_SURFACE)
        route = resolve_channel(config)
        assert route.channel_id == REVIEW_CHANNEL
        assert route.origin is ValueOrigin.APP_CONFIG

    def test_a_project_channel_beats_the_app_wide_one(self, config: ConfigStore) -> None:
        config.write({"notify": {"channel": REVIEW_CHANNEL}}, surface=DASHBOARD_SURFACE)
        with_project(config, channel=DASHBOARD_CHANNEL)
        # The project pinned the dashboard explicitly, so the route is configured
        # even though the channel equals the bundled default.
        route = resolve_channel(config, project=PROJECT)
        assert route.channel_id == DASHBOARD_CHANNEL
        assert route.configured is True
        # ...and a project that pinned nothing still reads the app-wide choice.
        assert resolve_channel(config, project="other").channel_id == REVIEW_CHANNEL

    def test_two_projects_route_independently(self, config: ConfigStore) -> None:
        with_project(config, "left", channel=REVIEW_CHANNEL)
        with_project(config, "right")
        assert resolve_channel(config, project="left").channel_id == REVIEW_CHANNEL
        assert resolve_channel(config, project="right").channel_id == DASHBOARD_CHANNEL


class TestUndeclaredChannels:
    """A channel id is untrusted text, and it ends up as a bus namespace."""

    def test_a_channel_this_app_does_not_declare_falls_back_and_says_why(
        self, config: ConfigStore
    ) -> None:
        with_project(config, channel="../system.approval")
        route = resolve_channel(config, project=PROJECT)
        assert route.channel_id == DASHBOARD_CHANNEL
        assert route.requested == "../system.approval"
        assert route.reason == REASON_UNDECLARED
        assert route.substituted is True

    def test_an_undeclared_channel_never_reaches_the_bus(
        self, config: ConfigStore, bus: FakeBus
    ) -> None:
        with_project(config, channel="system.approval")
        HostNotifier(config, project=PROJECT, bus=bus).send("stalled")
        assert bus.registrations == [f"{APP_NAME}.{DASHBOARD_CHANNEL}"]
        assert [payload.channel for payload in bus.pushed] == [f"{APP_NAME}.{DASHBOARD_CHANNEL}"]

    def test_an_unreadable_configuration_still_yields_a_route(self, config: ConfigStore) -> None:
        config.path.parent.mkdir(parents=True, exist_ok=True)
        config.path.write_text("{not json", encoding="utf-8")
        route = resolve_channel(config, project=PROJECT)
        assert route.channel_id == DASHBOARD_CHANNEL
        assert route.substituted is True

    def test_a_caller_named_channel_does_not_override_the_project_route(
        self, config: ConfigStore, bus: FakeBus
    ) -> None:
        # The run lifecycle and the budget ceiling each pass the channel string
        # they read; configuration, not that argument, decides the destination.
        with_project(config, channel=DASHBOARD_CHANNEL)
        delivery = HostNotifier(config, project=PROJECT, bus=bus).send(
            "stalled", channel=REVIEW_CHANNEL
        )
        assert delivery.route.channel_id == DASHBOARD_CHANNEL
        assert delivery.route.requested == REVIEW_CHANNEL
        assert delivery.route.reason == REASON_MISMATCH

    def test_a_caller_naming_the_project_route_is_not_a_mismatch(
        self, config: ConfigStore, bus: FakeBus
    ) -> None:
        with_project(config, channel=REVIEW_CHANNEL)
        delivery = HostNotifier(config, project=PROJECT, bus=bus).send(
            "stalled", channel=REVIEW_CHANNEL
        )
        assert delivery.route.channel_id == REVIEW_CHANNEL
        assert delivery.route.reason == ""

    def test_every_declared_channel_is_one_the_bus_would_accept(self) -> None:
        for entry in declared_channels():
            assert entry["default_priority"] in PRIORITIES
            assert entry["bus_channel"] == f"{APP_NAME}.{entry['id']}"
            assert known_channel(entry["id"])


class TestDeliveryThroughTheHost:
    """The notice leaves through the host bus, or not at all."""

    def test_the_notice_is_pushed_to_the_apps_namespaced_channel(
        self, config: ConfigStore, bus: FakeBus
    ) -> None:
        with_project(config, channel=REVIEW_CHANNEL)
        HostNotifier(config, project=PROJECT, bus=bus).send("run parked", "waiting for review")
        (payload,) = bus.pushed
        assert payload.channel == f"{APP_NAME}.{REVIEW_CHANNEL}"
        assert payload.source == APP_NAME
        assert payload.title == "run parked"
        assert payload.body == "waiting for review"

    def test_the_channel_priority_comes_from_the_catalog(
        self, config: ConfigStore, bus: FakeBus
    ) -> None:
        with_project(config, channel=REVIEW_CHANNEL)
        HostNotifier(config, project=PROJECT, bus=bus).send("run parked")
        assert bus.registered[f"{APP_NAME}.{REVIEW_CHANNEL}"] == CHANNELS[REVIEW_CHANNEL]
        assert bus.pushed[0].priority == CHANNELS[REVIEW_CHANNEL]

    def test_a_caller_may_raise_the_priority_of_one_notice(
        self, config: ConfigStore, bus: FakeBus
    ) -> None:
        with_project(config)
        HostNotifier(config, project=PROJECT, bus=bus).send("halted", priority="critical")
        assert bus.pushed[0].priority == "critical"
        assert CHANNELS[DASHBOARD_CHANNEL] != "critical"

    def test_the_channel_is_registered_once_however_many_notices(
        self, config: ConfigStore, bus: FakeBus
    ) -> None:
        # Re-registering would stomp a priority an operator overrode at runtime.
        notifier = HostNotifier(config, project=PROJECT, bus=bus)
        notifier.send("first")
        notifier.send("second")
        assert bus.registrations == [f"{APP_NAME}.{DASHBOARD_CHANNEL}"]
        assert len(bus.pushed) == 2

    def test_the_bus_is_read_off_gateway_state(self, config: ConfigStore, bus: FakeBus) -> None:
        notifier = HostNotifier(config, state=FakeState(bus=bus))
        assert notifier.available is True
        notifier.send("stalled")
        assert len(bus.pushed) == 1

    def test_the_real_bus_accepts_what_routing_builds(self, config: ConfigStore) -> None:
        # The fake bus records rather than validates, so the payload shape is
        # pinned against the host's own validation too.
        delivered: list[dict[str, Any]] = []
        real = NotificationBus(delivered.append)
        with_project(config, channel=REVIEW_CHANNEL)
        HostNotifier(config, project=PROJECT, bus=real).send(
            "run parked",
            "waiting for review",
            quoted="issue #4: ship it",
            detail={"run": "run-1", "phase": "awaiting_review"},
        )
        (note,) = delivered
        assert note["channel"] == f"{APP_NAME}.{REVIEW_CHANNEL}"
        assert note["priority"] == CHANNELS[REVIEW_CHANNEL]
        assert note[f"{DETAIL_PREFIX}run"] == "run-1"

    def test_no_bus_in_this_process_is_an_undelivered_notice(self, config: ConfigStore) -> None:
        notifier = HostNotifier(config, project=PROJECT)
        assert notifier.available is False
        with pytest.raises(NotificationUndelivered):
            notifier.send("stalled")

    def test_a_refusing_bus_is_an_undelivered_notice(self, config: ConfigStore) -> None:
        with pytest.raises(NotificationUndelivered) as caught:
            HostNotifier(config, project=PROJECT, bus=FakeBus(fail=True)).send("stalled")
        assert "bus said no" in str(caught.value)

    def test_an_exhausted_rate_limit_is_an_undelivered_notice(
        self, config: ConfigStore, bus: FakeBus
    ) -> None:
        limiter = FakeLimiter(allow=False)
        with pytest.raises(NotificationUndelivered):
            HostNotifier(config, project=PROJECT, bus=bus, limiter=limiter).send("stalled")
        assert bus.pushed == []

    def test_the_hosts_own_limiter_is_the_one_consumed(
        self, config: ConfigStore, bus: FakeBus
    ) -> None:
        # One budget shared with the host's producer endpoint, not a second one.
        limiter = FakeLimiter()
        HostNotifier(config, state=FakeState(bus=bus, limiter=limiter)).send("stalled")
        assert limiter.consumed == [APP_NAME]
        assert limiter.refunded == []

    def test_a_notice_that_reached_nobody_refunds_its_token(self, config: ConfigStore) -> None:
        limiter = FakeLimiter()
        with pytest.raises(NotificationUndelivered):
            HostNotifier(config, bus=FakeBus(fail=True), limiter=limiter).send("stalled")
        assert limiter.refunded == [APP_NAME]


class TestNotifierSeams:
    """The two shapes the engine already calls a notifier with."""

    def test_the_notifier_fits_both_engine_notifier_seams(self, config: ConfigStore) -> None:
        # Checked by the type checker rather than at runtime: a notifier whose
        # signature drifts from a seam it claims to fill still passes every
        # behavioural test in this file, because those call it directly.
        notifier = HostNotifier(config)
        run_seam: RunNotifier = notifier
        budget_seam: BudgetNotifier = notifier
        assert run_seam is budget_seam is notifier

    def test_the_budget_shape_delivers_with_the_message_as_title_and_body(
        self, config: ConfigStore, bus: FakeBus
    ) -> None:
        with_project(config, channel=REVIEW_CHANNEL)
        notifier = HostNotifier(config, project=PROJECT, bus=bus)
        notifier.notify(
            channel=REVIEW_CHANNEL,
            message="run run-1 halted for budget after consuming 5.00 of 5.00 credits",
            detail={"run": "run-1", "consumed_credits": 5.0},
        )
        (payload,) = bus.pushed
        assert payload.channel == f"{APP_NAME}.{REVIEW_CHANNEL}"
        assert payload.title.startswith("run run-1 halted for budget")
        assert payload.group_key == "run-1"
        assert payload.meta[f"{DETAIL_PREFIX}consumed_credits"] == 5.0

    def test_the_stall_shape_delivers_and_groups_by_run(
        self, config: ConfigStore, bus: FakeBus
    ) -> None:
        notifier = HostNotifier(config, project=PROJECT, bus=bus)
        notifier(FakeNotice(channel=DASHBOARD_CHANNEL, message="Spec run run-7 is stalled"))
        (payload,) = bus.pushed
        assert payload.title == "Spec run run-7 is stalled"
        assert payload.group_key == "run-1"
        assert payload.meta[f"{DETAIL_PREFIX}run_id"] == "run-1"

    def test_a_failed_delivery_raises_so_the_caller_can_record_it(
        self, config: ConfigStore
    ) -> None:
        # The run lifecycle catches this and records notified=False; returning
        # False instead would leave it nothing to catch.
        notifier = HostNotifier(config, project=PROJECT, bus=FakeBus(fail=True))
        with pytest.raises(NotificationUndelivered):
            notifier(FakeNotice(message="Spec run run-7 is stalled"))
        with pytest.raises(NotificationUndelivered):
            notifier.notify(channel="", message="halted", detail={})


class TestUntrustedText:
    """Item titles, provider errors, and reviewer comments are not the engine's words."""

    def test_a_title_is_one_line_whatever_it_was_handed(
        self, config: ConfigStore, bus: FakeBus
    ) -> None:
        HostNotifier(config, bus=bus).send("Approved\nAll gates passed", "body")
        assert bus.pushed[0].title == "Approved All gates passed"

    def test_control_characters_do_not_survive(self, config: ConfigStore, bus: FakeBus) -> None:
        HostNotifier(config, bus=bus).send("issue\x00\x07 title", "line\x1bone\nline two")
        payload = bus.pushed[0]
        assert payload.title == "issue title"
        assert "\x1b" not in payload.body
        assert payload.body.count("\n") == 1

    def test_untrusted_text_is_fenced_rather_than_interpolated(
        self, config: ConfigStore, bus: FakeBus
    ) -> None:
        HostNotifier(config, bus=bus).send("run parked", "review needed", quoted="## Approved")
        body = bus.pushed[0].body
        assert "review needed" in body
        assert "```\n## Approved\n```" in body

    def test_a_fence_is_longer_than_any_backtick_run_inside_it(self) -> None:
        # A quoted span carrying its own fence would otherwise close ours and
        # continue as rendered structure.
        quoted = quote_untrusted("```\n## Approved\n```")
        assert quoted.startswith("````\n")
        assert quoted.endswith("\n````")

    def test_empty_untrusted_text_adds_nothing(self, config: ConfigStore, bus: FakeBus) -> None:
        HostNotifier(config, bus=bus).send("run parked", "review needed", quoted="   ")
        assert bus.pushed[0].body == "review needed"

    def test_credentials_are_redacted_on_the_way_out(
        self, config: ConfigStore, bus: FakeBus
    ) -> None:
        HostNotifier(config, bus=bus).send(
            "poll failed", "401 from the tracker", quoted="key AKIAIOSFODNN7EXAMPLE rejected"
        )
        body = bus.pushed[0].body
        assert "AKIAIOSFODNN7EXAMPLE" not in body

    def test_long_text_is_clipped_rather_than_refused(
        self, config: ConfigStore, bus: FakeBus
    ) -> None:
        HostNotifier(config, bus=bus).send("t" * 900, "b" * (MAX_BODY_CHARS * 2))
        payload = bus.pushed[0]
        assert len(payload.title) == MAX_TITLE_CHARS
        assert len(payload.body) == MAX_BODY_CHARS

    def test_a_message_with_no_usable_first_line_still_has_a_title(
        self, config: ConfigStore, bus: FakeBus
    ) -> None:
        HostNotifier(config, bus=bus).notify(channel="", message="\x00\x00", detail={})
        assert bus.pushed[0].title == FALLBACK_TITLE

    def test_detail_travels_under_an_engine_owned_prefix(self) -> None:
        safe = safe_detail({"run": "run-1", "headless": True, "sessions": ["a", "b"]})
        assert set(safe) == {
            f"{DETAIL_PREFIX}run",
            f"{DETAIL_PREFIX}headless",
            f"{DETAIL_PREFIX}sessions",
        }
        assert safe[f"{DETAIL_PREFIX}headless"] is True

    def test_detail_keys_that_are_not_identifiers_are_dropped(self) -> None:
        # A field name assembled from untrusted text is a name an operator reads.
        safe = safe_detail({"_type": "chat", "kind": "x", "a b": 1, "Title": 2, "ok": 3})
        assert set(safe) == {f"{DETAIL_PREFIX}kind", f"{DETAIL_PREFIX}ok"}

    def test_detail_is_bounded_in_keys_and_in_value_length(self) -> None:
        safe = safe_detail({f"k{i}": "v" for i in range(MAX_DETAIL_KEYS * 2)})
        assert len(safe) == MAX_DETAIL_KEYS
        (value,) = set(safe_detail({"note": "x" * 5000}).values())
        assert len(str(value)) < 5000

    def test_a_detail_value_cannot_carry_a_newline_into_the_feed(self) -> None:
        safe = safe_detail({"note": "first\nsecond"})
        assert safe[f"{DETAIL_PREFIX}note"] == "first second"

    def test_safe_line_collapses_runs_of_whitespace(self) -> None:
        assert safe_line("  a\t\t b \n c ") == "a b c"


class TestTheRunLifecycleSeam:
    """The notifier is a drop-in for the seam the run lifecycle already calls.

    A structural protocol that does not actually match the notice the engine
    produces is worth nothing, so this drives a real stall through a real run
    machine rather than asserting against a hand-built notice.
    """

    def stalled(
        self,
        store: StateStore,
        config: ConfigStore,
        ref: SpecRef,
        bus: FakeBus,
    ) -> Any:
        clock = FakeClock()
        machine = RunMachine(
            store,
            config,
            project=PROJECT,
            notifier=HostNotifier(config, project=PROJECT, bus=bus),
            clock=clock,
        )
        machine.create(ref, run_id="run-9")
        machine.transition(ref, "run-9", RunState.AUTHORING)
        timeout = machine.phase_timeout_s(RunState.AUTHORING)
        assert timeout is not None
        clock.advance(timeout + 1)
        (notice,) = machine.sweep_stalled()
        return notice

    def test_a_real_stall_reaches_the_configured_channel(
        self, store: StateStore, config: ConfigStore, ref: SpecRef, bus: FakeBus
    ) -> None:
        with_project(config, channel=REVIEW_CHANNEL)
        notice = self.stalled(store, config, ref, bus)
        assert notice.notified is True
        (payload,) = bus.pushed
        assert payload.channel == f"{APP_NAME}.{REVIEW_CHANNEL}"
        assert payload.group_key == "run-9"
        assert "run-9" in payload.body

    def test_a_project_with_no_channel_configured_reaches_the_dashboard(
        self, store: StateStore, config: ConfigStore, ref: SpecRef, bus: FakeBus
    ) -> None:
        with_project(config)
        notice = self.stalled(store, config, ref, bus)
        assert notice.notified is True
        assert bus.pushed[0].channel == f"{APP_NAME}.{DASHBOARD_CHANNEL}"

    def test_an_undelivered_stall_leaves_the_run_stalled_and_recorded(
        self, store: StateStore, config: ConfigStore, ref: SpecRef
    ) -> None:
        with_project(config)
        notice = self.stalled(store, config, ref, FakeBus(fail=True))
        assert notice.notified is False
        assert "bus said no" in notice.error

    def test_notifying_writes_nothing_into_the_spec_directory(
        self, store: StateStore, config: ConfigStore, ref: SpecRef, project: Path, bus: FakeBus
    ) -> None:
        # The spec directory holds the native documents and the sidecar, and
        # nothing else: the IDE and the CLI read the same trees, so a note,
        # a channel record, or a delivery receipt left there is a foreign file
        # in someone else's contract.
        spec_dir = project / ".kiro" / "specs" / "example"
        before = spec_dir_snapshot(spec_dir)
        with_project(config, channel=REVIEW_CHANNEL)
        self.stalled(store, config, ref, bus)
        assert bus.pushed, "the notice went out, so this is not a vacuous snapshot"
        assert spec_dir_snapshot(spec_dir) == before
        assert set(before) == set(NATIVE_SPEC_FILES)
