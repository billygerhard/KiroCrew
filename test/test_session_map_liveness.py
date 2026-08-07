"""Tests for SessionMap liveness probing against the kiro-cli session files.

kiro-cli writes ``<sid>.jsonl`` (transcript) when a session starts and creates
``<sid>.json`` (metadata) at an unspecified later time — observed delays range
from seconds to hours. Liveness must therefore follow the transcript, not the
metadata file. These tests pin that contract.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from kiro_crew.session_map import SessionMap

LIVE_TRANSCRIPT = '{"role": "user", "content": "hello"}\n'  # > 10 bytes


@pytest.fixture()
def kiro_dir(tmp_path, monkeypatch):
    """Point the module's sessions dir at a temp directory."""
    kiro = tmp_path / "kiro-sessions"
    kiro.mkdir()
    monkeypatch.setattr("kiro_crew.session_map._KIRO_SESSIONS_DIR", kiro)
    return kiro


@pytest.fixture()
def session_map(tmp_path):
    """Create a SessionMap backed by a temp directory."""
    with patch("kiro_crew.session_map.config_dir", return_value=tmp_path / "cfg"):
        yield SessionMap()


class TestBugCondition:
    """Exploration tests: a live session that has written only its transcript.

    A session in its first hours has ``<sid>.jsonl`` on disk but no
    ``<sid>.json`` yet. It is live and must be treated as live.
    """

    def test_get_returns_sid_when_only_transcript_exists(self, session_map, kiro_dir):
        (kiro_dir / "sid-live.jsonl").write_text(LIVE_TRANSCRIPT)
        session_map.set("dash:1", "sid-live")

        assert session_map.get("dash:1") == "sid-live"

    def test_get_does_not_destroy_the_entry(self, session_map, kiro_dir):
        (kiro_dir / "sid-live.jsonl").write_text(LIVE_TRANSCRIPT)
        session_map.set("dash:1", "sid-live")

        session_map.get("dash:1")

        # The mapping must survive the lookup — a second lookup still resolves.
        assert session_map.get("dash:1") == "sid-live"

    def test_prune_retains_transcript_only_entries(self, session_map, kiro_dir):
        (kiro_dir / "sid-live.jsonl").write_text(LIVE_TRANSCRIPT)
        session_map.set("dash:1", "sid-live")

        assert session_map.prune() == 0
        assert session_map.get("dash:1") == "sid-live"


class TestPreservation:
    """Behavior outside the bug condition must not change."""

    def test_get_returns_sid_when_both_files_exist(self, session_map, kiro_dir):
        (kiro_dir / "sid-a.jsonl").write_text(LIVE_TRANSCRIPT)
        (kiro_dir / "sid-a.json").write_text("{}")
        session_map.set("dash:1", "sid-a")

        assert session_map.get("dash:1") == "sid-a"

    def test_get_removes_entry_with_near_empty_transcript(self, session_map, kiro_dir):
        # Under 10 bytes = the established empty-session rule.
        (kiro_dir / "sid-b.jsonl").write_text("{}\n")
        (kiro_dir / "sid-b.json").write_text("{}")
        session_map.set("dash:1", "sid-b")

        assert session_map.get("dash:1") is None
        assert session_map.get("dash:1") is None  # entry removed, stays gone

    def test_get_removes_entry_when_no_session_files_exist(self, session_map, kiro_dir):
        session_map.set("dash:1", "sid-gone")

        assert session_map.get("dash:1") is None

    def test_prune_removes_entry_when_no_transcript_exists(self, session_map, kiro_dir):
        session_map.set("dash:1", "sid-gone")

        assert session_map.prune() == 1
        assert session_map.get("dash:1") is None

    def test_claude_code_entries_skip_filesystem_probe(self, session_map, kiro_dir):
        session_map.set("dash:1", "cc-sid", provider="claude_code")

        assert session_map.get("dash:1") == "cc-sid"
        assert session_map.prune() == 0
        assert session_map.get("dash:1") == "cc-sid"

    def test_prune_retains_sidless_entry_with_slack_thread(self, session_map, kiro_dir):
        session_map.set("slack:C1:111.222", "")
        session_map.set_slack_link("slack:C1:111.222", "111.222", "C1")

        assert session_map.prune() == 0

    def test_prune_removes_bare_sidless_entry(self, session_map, kiro_dir):
        session_map.set("dash:1", "")

        assert session_map.prune() == 1

    def test_removal_persists_to_disk(self, tmp_path, kiro_dir):
        cfg = tmp_path / "cfg"
        with patch("kiro_crew.session_map.config_dir", return_value=cfg):
            sm = SessionMap()
            sm.set("dash:1", "sid-gone")
            assert sm.get("dash:1") is None  # removes: no session files exist

        with patch("kiro_crew.session_map.config_dir", return_value=cfg):
            sm2 = SessionMap()
            assert sm2.get("dash:1") is None

    def test_dashboard_history_roundtrip_fallback(self, session_map, kiro_dir):
        (kiro_dir / "sid-live.jsonl").write_text(LIVE_TRANSCRIPT)
        (kiro_dir / "sid-live.json").write_text("{}")
        session_map.set("dashboard:65", "sid-live")

        assert session_map.get("dashboard:dashboard_65") == "sid-live"
