---
title: Batch write guard's token scan runs on the event loop and can stall the gateway into the loop-stall exit
labels: bug
---

## What

The gateway was ended twice in one afternoon (2026-11-11, macOS, launchd service, ~5 load per core) by its own loop-stall alarm (`SIGALRM`, launchd `last terminating signal = Alarm clock: 14`). The loop-stall crash dump of one of them shows the event-loop thread inside the batch write guard's token scan of streamed model text:

```
Current thread (most recent call first):
  File "src/kiro_crew/acp/batch_guard.py", line 707 in _scanned
  File "src/kiro_crew/acp/batch_guard.py", line 853 in note_text
  File "src/kiro_crew/acp/batch_guard.py", line 1013 in note_text
  File "src/kiro_crew/acp/session_handle_updates.py", line 506 in _handle_update
  File "src/kiro_crew/acp/session_handle_dispatch.py", line 922 in _dispatch_events
  File "src/kiro_crew/acp/session_handle_turn.py", line 848 in _run_turn
  ...
```

In the minutes before the exit the watchdog logged `event loop silent 16.7s` and `event loop silent 20.0s`, with heartbeat lags of 3-10 s in between.

## Where

`BatchWindow.note_text` (and `printed`/`_tokens` for results) calls `_scanned` -> `_all_tokens` -> `tokens_of` / `times_of` synchronously for every `agent_message_chunk` the dispatcher handles. `tokens_of` is pure-Python character work over up to `MAX_SCANNED_TEXT_CHARS` (256 KiB) per call. `_dispatch_events` handles a backlog of updates back to back, so a turn streaming a lot of text (or a large tool result) on a CPU-contended host keeps the loop busy for many seconds with no await in between. Every other chat, the dashboard HTTP server and the heartbeat wait behind it.

## Expected

Collecting tokens for the guard never holds the event loop long enough to approach the loop-stall budget. For example: scan off the loop (`asyncio.to_thread`) or in a bounded per-tick slice, yield between dispatched updates, or cap the work per window per second and mark `seen.complete = False` past the cap (the guard already treats an incomplete set as "refuse only on proof").

## Workaround

`hooks.batch_write_guard: false` stops the collection. On a launchd/systemd service, a wider `dashboard.loop_stall_exit_after_secs` keeps such a stall from ending the process (an install whose config still pins the old explicit 25 s does not get the 90 s managed-service default).
