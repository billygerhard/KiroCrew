"""A sandbox that hides the SEL trust dir leaves the log unsigned, not crashed.

An enforced harness's sandbox (agy's, for one) denies the data home's ``trust``
dir to the processes it runs: ``mkdir`` answers EEXIST while ``stat`` is
refused. The SEL singleton used to raise ``FileExistsError`` there, and every
caller that audits on its way to an answer raised with it -- an in-sandbox MCP
server's ``tools/list`` among them, which the harness treats as fatal.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from sel_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _make_event,
    reset_singleton,
)

from kiro_crew import sel as sel_mod
from kiro_crew.sel import SecurityEventLog


def _hidden_key_load(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(_self: SecurityEventLog) -> bytes:
        raise FileExistsError(17, "File exists")

    monkeypatch.setattr(SecurityEventLog, "_load_or_create_hmac_key", _raise)


def test_a_hidden_trust_root_drops_best_effort_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _hidden_key_load(monkeypatch)
    log = SecurityEventLog(base_dir=tmp_path, sync=True)
    log.log(_make_event())
    log.log_api_access(
        caller="s", operation="tool_policy.forbidden", outcome="unresolved", source="mcp_shared"
    )
    assert log.dropped_events == 2
    assert not (tmp_path / sel_mod._SEL_FILE).exists()


def test_a_hidden_trust_root_still_refuses_a_critical_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _hidden_key_load(monkeypatch)
    log = SecurityEventLog(base_dir=tmp_path, sync=True)
    with pytest.raises(PermissionError, match="critical"):
        log.log(_make_event(), critical=True)


def test_a_visible_non_directory_at_the_trust_name_still_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file planted at ``trust`` is something this process can see: not a sandbox."""
    (tmp_path / sel_mod._TRUST_SUBDIR).write_text("planted")
    _hidden_key_load(monkeypatch)
    with pytest.raises(FileExistsError):
        SecurityEventLog(base_dir=tmp_path, sync=True)


@pytest.mark.parametrize(
    ("exc", "lstat_error", "hidden"),
    [
        (FileExistsError(17, "exists"), PermissionError(1, "denied"), True),
        (PermissionError(1, "denied"), PermissionError(1, "denied"), True),
        (FileExistsError(17, "exists"), FileNotFoundError(2, "missing"), True),
        (PermissionError(1, "denied"), FileNotFoundError(2, "missing"), False),
        (OSError(28, "no space"), PermissionError(1, "denied"), False),
        (FileExistsError(17, "exists"), None, False),
    ],
)
def test_the_hidden_trust_root_rule(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exc: OSError,
    lstat_error: OSError | None,
    hidden: bool,
) -> None:
    real_lstat = os.lstat

    def _lstat(path, *args, **kwargs):  # type: ignore[no-untyped-def]
        if Path(path) == tmp_path / sel_mod._TRUST_SUBDIR:
            if lstat_error is None:
                return real_lstat(tmp_path)
            raise lstat_error
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(sel_mod.os, "lstat", _lstat)
    assert sel_mod._trust_root_hidden(tmp_path, exc) is hidden
