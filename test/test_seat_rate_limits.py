"""seat_rate_limits folds claude-agent-acp's forwarded rate_limit_event into a per-seat file."""

from __future__ import annotations

import json

import pytest

from kiro_crew.acp import seat_rate_limits


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(seat_rate_limits, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(seat_rate_limits, "_state", None)
    return tmp_path


def _update(windows):
    # The real frame shape, captured from a seat on 2026-10-26.
    return {
        "sessionUpdate": "usage_update",
        "used": 1000,
        "size": 200000,
        "_meta": {
            "_claude/rateLimit": {
                "status": "allowed",
                "rateLimitType": "five_hour",
                "unifiedWindows": windows,
            }
        },
    }


def _read(home):
    return json.loads((home / seat_rate_limits.FILE_NAME).read_text())


def test_records_windows_per_seat_and_keeps_unreported_ones(home):
    seat_rate_limits.record(
        "claude-seat1",
        _update({
            "five_hour": {"utilization": 0.04, "resetsAt": 1793056800},
            "seven_day_fable": {"utilization": 0.3, "resetsAt": 1793400000},
        }),
    )
    seat_rate_limits.record("claude-seat1", _update({"five_hour": {"utilization": 0.06, "resetsAt": 1793056800}}))
    seat_rate_limits.record("claude-seat2", _update({"five_hour": {"utilization": 1.0, "resetsAt": 1793050000}}))

    data = _read(home)
    w1 = data["claude-seat1"]["windows"]
    assert w1["five_hour"]["utilization"] == 0.06
    assert w1["seven_day_fable"]["utilization"] == 0.3  # a five_hour-only reply keeps the weekly reading
    assert data["claude-seat2"]["windows"]["five_hour"] == {
        "utilization": 1.0, "resetsAt": 1793050000, "at": data["claude-seat2"]["windows"]["five_hour"]["at"],
    }


@pytest.mark.parametrize(
    "backend,update",
    [
        (None, _update({"five_hour": {"utilization": 0.1}})),
        ("claude-seat1", {"sessionUpdate": "usage_update", "used": 1, "size": 2}),  # kiro: no _meta
        ("claude-seat1", _update("garbage")),
        ("claude-seat1", _update({"five_hour": {"utilization": "high"}})),
        ("claude-seat1", _update({"five_hour": {"utilization": float("nan")}})),
        ("claude-seat1", "not a dict"),
    ],
)
def test_ignores_frames_without_usable_windows(home, backend, update):
    seat_rate_limits.record(backend, update)
    assert not (home / seat_rate_limits.FILE_NAME).exists()


def test_never_raises_when_the_write_fails(home, monkeypatch):
    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(seat_rate_limits, "atomic_write", boom)
    seat_rate_limits.record("claude-seat1", _update({"five_hour": {"utilization": 0.5}}))
