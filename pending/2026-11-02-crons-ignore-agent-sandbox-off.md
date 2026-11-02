---
title: Script and command crons ignore agent.sandbox='off' and always run in the cc sandbox
labels: bug
---

## What

With `agent.sandbox: "off"` (agents and sub agents run unconfined), script crons and
command crons still run inside the OS sandbox on the `cc` profile. A script cron that
writes `~/.kiro/crew/config.json` (here: rotating `agent.acp_backend` to a Claude seat
with room) fails with:

```
[Errno 1] Operation not permitted: '/Users/<user>/.kiro/crew/config.json.failover-tmp' -> '/Users/<user>/.kiro/crew/config.json'
```

The generated Seatbelt profile under `~/.kiro/crew/run/` has
`(deny file-write* (literal ".../.kiro/crew/config.json"))`. The same write from an
agent's shell succeeds, because the agent runs unsandboxed.

The failure is easy to miss: a job that only reports failures looked like it was
working for two weeks, until the rotation was actually needed.

## Why

`run_script_sandboxed` in `src/kiro_crew/cron_script.py` (upstream main at 1dd2be3342)
hard-codes `sandbox_mode = "strict" if stdin_payload is not None else "cc"` (line 402),
and `run_command_sandboxed` passes `mode="cc"` (line 968). Neither reads `agent.sandbox`,
unlike the one-shot kiro-cli spawns, which use `configured_sandbox_mode()`.

The comments explain the intent: cron bodies are agent-written and run unattended. But
when the operator has set `agent.sandbox: "off"`, the agents that write those cron
bodies are unconfined themselves, so confining the cron adds no protection; it only
breaks jobs that do what an agent shell may already do. The two settings disagree with
no warning: the startup `SECURITY: agent.sandbox='off'` warnings imply everything runs
unconfined.

## Expected

One of:

- Ungranted script crons and command crons follow `configured_sandbox_mode()`, so
  `agent.sandbox: "off"` runs them unconfined. Secret-granted script crons stay on
  `strict` (they already refuse to run without isolation), or
- a separate documented setting (e.g. `cron.sandbox`) controls the cron tier, and the
  startup warnings say crons stay confined when `agent.sandbox` is `off`.

## Repro

1. Set `agent.sandbox: "off"` and restart the gateway.
2. Register a script cron whose function opens `~/.kiro/crew/config.json` for append
   (`open(path, "a")`, writing nothing).
3. Run it (`kirocrew cron trigger <id>`): `PermissionError: [Errno 1] Operation not
   permitted`. The same `open` from an agent shell works.

Fork evidence: the fork takes the first option (billygerhard/KiroCrew `c2eb1fab6b`).
After a gateway restart the same probe prints "writable"; the cron test suite passes.
