---
title: Gateway freezes for hours when its thread pool stalls, but the loop-stall watchdog never fires
labels: bug
---

## What

The gateway stopped running chat turns for about 85 minutes (14:14 to 15:40 local, when
it was restarted by hand), but the event loop stayed responsive the whole time:
`loop_lag_ms_max` stayed under 50 ms, apart from one 134 ms sample. The loop-stall
watchdog (`dashboard.loop_stall_exit_after_secs = 25`) therefore never fired, and no
faulthandler dump was written.

What failed was everything that goes through a worker thread:

- `GET /api/session-tool-policy` timed out (5 s) for every new MCP stub between
  14:13 and 14:31 (`mcp_shared: Tool policy resolution failed (TimeoutError) ...
  tool calls are REFUSED`). The handler does its work in `asyncio.to_thread`
  (`dashboard/handlers/sessions_parts/tool_policy.py`).
- New sessions logged `mcp_mount_check: ... never reached the MCP broker within 30s`
  on claude-seat2, seat3 and seat4.
- Dashboard messages sent at 14:14, 14:16, 14:30 and 14:30 were persisted, but no turn
  ever started: there was no assistant text and no tool call until after the restart.
- The gateway log is silent from 14:31 to 15:14.

Most likely cause: the default executor's workers were all blocked, probably on file
I/O during a disk stall at around 14:10–14:16 caused by a large recursive grep, and
never recovered. The event loop kept working while every `to_thread` call queued
forever. This hasn't been confirmed: on macOS the diagnostics sampler reports
`thread_states: {'?': 92}` and `interpretation: not measured`, so no stack was
captured.

## Why

Users see a frozen product with no error, while every health signal stays green. The
only fix was a manual restart, and 85 subagents were orphaned by it.

## Suggested fix

- Make the watchdog also probe the default executor: periodically submit a no-op with
  `to_thread` and treat it as a stall when it hasn't completed within N seconds. On a
  stall, dump all thread stacks (`faulthandler.dump_traceback(all_threads=True)`) and
  restart, the same way a loop stall is handled.
- Capture thread stacks on macOS too (for example via `sys._current_frames()`), so
  `thread_states` isn't `?` there.

## Environment

macOS (Darwin 27), Mac mini, gateway running from a source checkout, Python 3.12.15.
Diag snapshots: `~/.kiro/crew/diag/snapshots-20261028.jsonl`.
