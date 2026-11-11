"""The batch guard's token scans share one event-loop time budget per second.

A turn streaming a lot of text on a busy host held the loop past the
loop-stall exit (2026-11-11). Past the budget a scan reads nothing and marks
the seen set incomplete, which only ever makes the guard refuse less.
"""

from __future__ import annotations

import pytest

from kiro_crew.acp import batch_guard as guard

_SHA = "0123456789abcdef0123456789abcdef01234567"


@pytest.fixture(autouse=True)
def _fresh_budget(monkeypatch):
    monkeypatch.setattr(guard, "_scan_spent", [0.0, 0.0])


def test_a_scan_within_the_budget_reads_the_text():
    window = guard.BatchWindow()
    assert _SHA in window._scanned(f"commit {_SHA}")
    assert window.seen.complete


def test_a_scan_past_the_budget_reads_nothing_and_marks_the_set_incomplete(monkeypatch):
    monkeypatch.setattr(guard.time, "monotonic", lambda: 100.0)
    window = guard.BatchWindow()
    window._scanned("warm up")  # opens the window at t=100
    guard._charge_scan(guard._SCAN_BUDGET_SECS)
    assert window._scanned(f"commit {_SHA}") == frozenset()
    assert not window.seen.complete


def test_the_budget_refills_in_the_next_window(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(guard.time, "monotonic", lambda: clock[0])
    window = guard.BatchWindow()
    window._scanned("warm up")
    guard._charge_scan(guard._SCAN_BUDGET_SECS)
    clock[0] += guard._SCAN_WINDOW_SECS
    assert _SHA in window._scanned(f"commit {_SHA}")
