---
title: Script crons fail when the sensitive-path resolver stalls, though the script path is already resolved
labels: bug
---

## What

On a busy host a healthy script cron fails before it starts, with:

```
PermissionError: Blocked: the path could not be verified against the sensitive-path
list within the resolver budget (symlink resolution did not complete in time), so it
is refused fail-closed. This is NOT a match: the path is not known to be sensitive.
Retry the same call after a short wait; do not re-spell it.
Path: '/Users/<user>/.kiro/crew/crons/claude_seat_failover.py'
```

The next run (2 minutes later) succeeds. A 2-minute script cron hit this about once an
hour over a working day (10:00-12:15: 4 failures in ~70 runs), and a second script cron
hit it too. Each failure also lands in the inbox as a cron failure ("Still failing
(suppressed — same reason)" on the repeat).

## Why

`resolve_script_path` in `src/kiro_crew/cron_script_paths.py` (upstream main at
1dd2be3342) already canonicalises the script with `Path(...).resolve()`, then calls
`sensitive_path_refusal(str(file_path))`, which resolves the path again on the
`mc-pathres` pool with the 2 s `_PATH_RESOLVE_TIMEOUT_SECS` budget. When the pool misses
the budget the call refuses fail-closed, and the stalled prefix stays refused for the
next 30 s, so even a short retry inside the job would not help.

This is the case `canonical_path_refusal` / `is_sensitive_canonical_path` exist for: a
caller that already resolved the path. The script run happens off the event loop (on
the scheduler's worker thread), where `canonical_path_refusal` answers inline through
`is_sensitive_resolved_path` without submitting to the pool, so a saturated pool can't
refuse a healthy script. On-loop callers of `resolve_script_path` (if any) keep the
bounded check.

Related: the pending draft
`2026-11-02-folder-project-resolver-stall-reported-as-sensitive.md` (same stall,
chat-folder project validation).

## Expected

- `resolve_script_path` checks its already-resolved path with `canonical_path_refusal`,
  so a resolver stall doesn't fail a script cron run off the loop.
- A real sensitive-path match is still refused, with the same wording as today.

## Repro

1. Register a script cron (e.g. every 2 minutes) under `~/.kiro/crew/crons/`.
2. Load the host so a `realpath` under `$HOME` exceeds 2 s now and then (heavy disk
   I/O, many sub agents).
3. Watch the job's history: occasional `failure` runs with the "could not be verified
   ... within the resolver budget" error, each followed by a success.

Fork evidence: the fork carries a fix that swaps in `canonical_path_refusal`
(billygerhard/KiroCrew `00fb90045f`), with a test that a stalled pool doesn't refuse a
healthy script; the cron and mcp_cron test suites pass.
