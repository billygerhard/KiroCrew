---
title: macOS: completed sub-agent marked failed with "kill failed: PermissionError" (EPERM from a zombie-only process group)
labels: bug
---

## What

On macOS, a sub-agent that finished its turn is recorded as **failed** with `kill failed: PermissionError` during teardown.

## Cause

On macOS, `killpg()` on a process group whose only members are zombies (an exited leader its parent has not reaped yet) fails with **EPERM**, not ESRCH. That happens for signal 0 and for SIGKILL alike.

- `pgroup_exists` reads that EPERM as "alive".
- `process_survived` counts the zombie leader as running, because `kill(pid, 0)` reaches a zombie.
- So a reset that already ended the process looks like it survived, the fallback group SIGKILL raises `PermissionError`, and the completed run is marked failed.

Upstream main (checked 2026-10-27, `e062153c45`) handles a zombie *provider root* (#11491), but not a group where every member is a zombie.

## Reproduce

macOS. Start any sub-agent and let it finish normally. Its teardown races the parent's reap of the exited leader, and some runs end as failed with `kill failed: PermissionError` in the session's `subagents.json` and `log.jsonl`. We saw several a day on a Mac mini running macOS 27. None have appeared since we started running the fix below.

## Fix we are running (for reference)

Commit: https://github.com/billygerhard/KiroCrew/commit/66c566389d

- `platform_compat.pgroup_has_live_member` lists the group's members (sysctl on macOS, /proc on Linux) and returns False only when every listed member is a zombie.
- `kill_process_group` / `kill_process_tree` raise that EPERM as `ProcessLookupError` (the group is empty), and `pgroup_exists` reads it as empty. An EPERM from a group with a running member, or a listing that can't be read, still propagates as a real refusal.
- `process_survived` no longer counts a zombie leader, or a group of only zombies, as still running.
- A run that already recorded a completed ending keeps that result if the teardown kill fails afterwards. The failure is logged and audited, and the process still goes to reconciliation through the leaked-process tombstone.
- Test: `test/test_reaper_eperm_exited_group.py`.
