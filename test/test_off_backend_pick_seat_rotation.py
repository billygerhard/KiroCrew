"""An unpicked chat follows a rotated default across one session family.

The seat failover moves ``agent.acp_backend`` off a full Claude seat. An
unpicked chat whose live process still ran on the old seat kept hitting that
seat's limit while the dashboard showed it on the new default (2026-10-30).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kiro_crew.dashboard import chat_runner as cr
from kiro_crew.agent_sdk import operator_backend_registry as registry


def _judge(monkeypatch, *, running, default, pick="", allocation=None, family=True):
    monkeypatch.setattr(cr, "effective_session_key", lambda slot: "dashboard:k")
    monkeypatch.setattr(cr, "slot_backend_override", lambda slot: pick)
    monkeypatch.setattr(
        cr, "slot_backend", lambda slot, member, dflt, key=None: pick or dflt
    )
    monkeypatch.setattr(
        cr, "capabilities_of", lambda live: SimpleNamespace(backend=running)
    )
    monkeypatch.setattr(
        registry,
        "same_session_family",
        lambda a, b: family and a.startswith("claude-seat") and b.startswith("claude-seat"),
    )
    state = SimpleNamespace(
        sessions=SimpleNamespace(allocation_backend_override=lambda key: allocation)
    )
    cfg = SimpleNamespace(agent=SimpleNamespace(member_acp_backend="", acp_backend=default))
    return cr._off_backend_pick(state, object(), cfg, live=object())


def test_an_unpicked_chat_left_on_the_old_seat_moves_to_the_new_default(monkeypatch):
    assert _judge(monkeypatch, running="claude-seat3", default="claude-seat1")


def test_an_unpicked_chat_already_on_the_default_seat_stays(monkeypatch):
    assert not _judge(monkeypatch, running="claude-seat1", default="claude-seat1")


@pytest.mark.parametrize("running,default", [("kiro", "claude-seat1"), ("claude-seat1", "kas")])
def test_an_unpicked_chat_across_families_follows_the_default_as_before(
    monkeypatch, running, default
):
    assert not _judge(monkeypatch, running=running, default=default, family=False)


def test_a_picked_chat_is_still_judged_on_its_pick(monkeypatch):
    assert _judge(monkeypatch, running="claude-seat3", default="claude-seat1", pick="claude-seat2")
    assert not _judge(
        monkeypatch, running="claude-seat2", default="claude-seat1", pick="claude-seat2"
    )
