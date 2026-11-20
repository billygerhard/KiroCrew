---
title: Let one enforced harness run sandboxed while agent.sandbox stays off
labels: enhancement
issue: https://github.com/kirodotdev/KiroCrew/issues/39721
---

## What happens

With `agent.sandbox: "off"`, every enforced third-party harness (Antigravity, Codex, Goose, OpenCode, …) is refused at launch:

> AcpToolGateUnroutable: Google Antigravity routes tool calls through an enforced permission route whose compensating control is an OS-level credential mask, but this session would spawn it unsandboxed -- either agent.sandbox is 'off', or no sandbox backend is available and agent.sandbox_allow_unsandboxed_exec is set -- so the mask is never applied and the adapter's credential reads are unfenced. Set agent.sandbox to 'auto' or 'strict' ON A HOST WITH A WORKING BACKEND to select this harness, or select a harness whose tool calls reach the gate directly.

The refusal is correct. But the only remedy offered is to turn the sandbox on for the whole gateway: every harness, every script and command cron, every MCP server. A user who keeps the sandbox off on purpose (for example because their crons write outside the sandbox's allowed paths) and wants one enforced harness has no way to get it. The host has a working sandbox backend; the setting just can't be scoped.

Reproduced on a gateway based on upstream `main` at e06be9f4dd (macOS, Antigravity ACP 1.4.4, `agent.sandbox: "off"`): `spawn_run(backend="antigravity", task="Reply PONG")` fails with the message above. The code path below was checked on `main` itself.

## Where

- `src/kiro_crew/acp/launch.py`, `_sandbox_preflight(backend, mode)`: `mode` is the gateway-wide tier, so an enforced backend can never resolve to a sandboxed tier while the setting is off.
- `src/kiro_crew/agent_sdk/tool_gate.py`, `enforce_sandbox_floor`: already keyed on the *effective* tier ("a governed host whose `sandbox.min_level` floor raises `off` still gets the mask"). A per-harness floor would fit the same check.
- PR #34147 makes Settings > Agent Harness show these rows as refused when the sandbox is off, and lists making them run as a non-goal. This request is that missing remedy.

## Expected

A per-harness tier floor, e.g.

```json
"agent": {
  "sandbox": "off",
  "harness_sandbox": { "antigravity": "standard" }
}
```

so a session on that harness is spawned at `max(agent.sandbox, harness floor)`, with its credential mask applied, while everything else keeps the gateway-wide tier. The floor can only raise the tier, never lower it, so it cannot weaken an existing setting. Enforced harnesses could default to `standard` here when the host has a working backend, which would make them usable out of the box on a sandbox-off gateway.

## Suggested tests

- `agent.sandbox: "off"` + floor `standard` for an enforced harness: the launch is not refused and the spawned argv carries the credential mask.
- `agent.sandbox: "off"` with no floor: still refused with today's sentence.
- `agent.sandbox: "strict"` + floor `standard`: the session stays at `strict` (a floor never lowers).
- Native Windows with a floor: still refused with the host sentence, since there is no backend to apply it.
