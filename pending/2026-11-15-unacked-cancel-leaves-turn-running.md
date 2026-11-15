---
title: Stop on a shared-runtime session whose cancel goes unacked marks the turn stopped, but the engine's turn keeps running and its tool calls still land
labels: bug
---

## What

On a Claude chat (`claude-agent-acp` 0.94.0 behind a `harnesses.json` backend, shared-runtime session handle), the user pressed Stop several times before the agent called `spawn_run`. The dashboard showed `[Stopped]`, but the turn went on and the spawn landed: a background worker started on a Claude seat 19 s after the parent's turn "ended". Getting a later turn on the same chat to stop took about 20 presses and then a force stop. The gateway log for the force stop:

```
11:31:04 abort applied: pids=[81647] reason=hard-stop session=dashboard:chat-542-...
11:31:28 WARNING kiro_crew.mcp_gateway.abort: abort-push failed: pids=[81647] reason=hard-stop ...
11:31:32 WARNING kiro_crew.acp.session_handle: Cancel unacked after 10.0s on session 538bf21a-... — unblocking caller (runtime kept alive for co-tenants)
```

None of the plain Stop presses reached the MCP gateway's abort path (it has recorded only `reason=hard-stop` aborts, ever).

## Where

`_dispatch_events` in `src/kiro_crew/acp/session_handle_dispatch.py`, the unresponsive-cancel branch. When `session/cancel` is not acked within `_cancel_grace_secs`, it sets `_turn_done` and yields a synthesized `EVENT_COMPLETE` with `STOP_REASON_CANCEL_UNACKED`. By design it does not kill the process, because co-tenants share it. Nothing else then ends the session's turn on the engine. The engine keeps generating and calling tools: MCP tools through the gateway, and built-in tools through the permission path. The dashboard already shows the turn as stopped.

`OperatorHarness.teardown` for generic ACP hosts is also `session/cancel`, the same verb that went unacked. So no teardown path ends such a turn either.

## Expected

An unacked cancel on a shared runtime ends that one session's turn on the engine. `claude-agent-acp` advertises `sessionCapabilities.close`, and its `session/close` tears the session's query down (`cancelTurns(..., {awaitInterrupt: false})`, then `query.close()`) while keeping the stored record. `AcpRuntime.evict_session` already sends exactly that. A fix we run locally:

1. In the unacked branch, when the runtime's `initialize` answer advertises `sessionCapabilities.close`, schedule `runtime.evict_session(sid)` and keep the task on the handle.
2. Before the next turn, `AcpSessionProvider` reloads the same id on the existing re-injection path (`apply_pending_mcp_reinjection`: `keep_record_on_teardown` + `load_session`), skipping the second close. The conversation carries on, and a failed reload refuses the turn with a message naming the Stop.

Engines without `close` keep today's behaviour.

## Workaround

None from the dashboard; a force stop eventually ends it. Stopping a spawned worker from its own card works.
