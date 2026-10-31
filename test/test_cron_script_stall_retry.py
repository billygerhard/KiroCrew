"""A script cron retries a sensitive-path resolver STALL, never a policy match."""
import pytest

from kiro_crew import cron_script
from kiro_crew.security import UNVERIFIABLE_PATH_PREFIX


def _fake_resolve(errors):
    calls = []

    def resolve(spec, allow_bundle_roots=False):
        calls.append(spec)
        if errors:
            raise errors.pop(0)
        return ("/x/job.py", "run")

    return resolve, calls


def test_a_stall_is_retried_until_the_check_passes(monkeypatch):
    stall = PermissionError(f"{UNVERIFIABLE_PATH_PREFIX} (symlink resolution did not complete)")
    resolve, calls = _fake_resolve([stall, stall])
    monkeypatch.setattr(cron_script, "resolve_script_path", resolve)
    monkeypatch.setattr(cron_script.time, "sleep", lambda s: None)
    assert cron_script._resolve_script_path_retrying("job.py:run") == ("/x/job.py", "run")
    assert len(calls) == 3


def test_a_stall_that_never_clears_still_fails(monkeypatch):
    stall = PermissionError(f"{UNVERIFIABLE_PATH_PREFIX} (symlink resolution did not complete)")
    resolve, calls = _fake_resolve([stall] * 10)
    monkeypatch.setattr(cron_script, "resolve_script_path", resolve)
    monkeypatch.setattr(cron_script.time, "sleep", lambda s: None)
    with pytest.raises(PermissionError):
        cron_script._resolve_script_path_retrying("job.py:run")
    assert len(calls) == len(cron_script._STALL_RETRY_DELAYS_SECS) + 1


def test_a_policy_block_is_not_retried(monkeypatch):
    resolve, calls = _fake_resolve([PermissionError("Script path blocked by security policy: /x")])
    monkeypatch.setattr(cron_script, "resolve_script_path", resolve)
    monkeypatch.setattr(cron_script.time, "sleep", lambda s: pytest.fail("slept on a match"))
    with pytest.raises(PermissionError):
        cron_script._resolve_script_path_retrying("job.py:run")
    assert len(calls) == 1
