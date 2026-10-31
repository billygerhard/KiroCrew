---
title: Background embedding repair keeps running local inference while the host is under memory pressure
labels: bug
---

## What

The member-memory repair loop (`slack/gateway_runtime/memory_lifecycle.py`,
`_repair_member_memory`) wakes every 30 seconds. When any store has rows without an
embedding, it runs the local embedding model on up to 16 rows per kind
(`backfill_missing_embeddings(max_rows_per_kind=16, ...)`) on `embed_executor()`,
inside the gateway process. Nothing in the loop looks at the host: the only exit is
`_memory_repair_stop`. When many sessions write memories and lessons, there is always
a backlog, so the loop runs model inference through a memory or CPU crunch.

Seen on 2026-10-31 on a 12-core, 32 GB Mac mini. Five iOS Simulators were booted, many
Angular builds and installs were running, and swap had grown to 11 GB. The 1-minute
load average passed 400, and macOS reported memory pressure WARN. The gateway's event
loop stalled twice (loop-stall dumps at 16:20Z and 16:41Z), and the watchdog restarted
the gateway. Each restart killed every chat and subagent.

The main-thread stack in the dump was an ordinary cron timer tick
(`cron_service/service/wake_timer.py` handing off through `to_thread`), which points
to the event loop being starved of CPU rather than blocked in one call. Among the
dump's 40 threads, one was inside `backfill_missing_embeddings`
(`vector_memory_runtime/embedding_repair.py:445`). The repair is not proven to be the
cause. It is extra CPU work, in the same process as the event loop, at the worst
possible moment, and it gains nothing by running then: the backlog keeps.

## Why

Users lose every running chat and subagent when the gateway restarts, and the
gateway itself adds avoidable load to a host that is already struggling.
`resource_status` already measures exactly this (`resource_status.probe()` posture
`tight`/`critical`, `pressure_level_held()` for macOS kernel memory pressure,
`load_per_cpu`), and the subagent gate already holds new subagents on it. The repair
loop doesn't use any of it.

## Suggested fix

- Before each repair round, check the host. Skip the round when kernel memory pressure
  is held (WARN or worse), when `resource_status.probe()` posture is `tight` or
  `critical`, or when `load_per_cpu` is above a threshold.
- While skipping, back off (for example 30 s doubling to 5 min), and log one INFO line
  per pressure streak rather than one per round. Go back to the normal 30 s pace once
  the host is clear.
- Run the check in the executor thread, beside the repair, so a slow probe never
  blocks the event loop.
