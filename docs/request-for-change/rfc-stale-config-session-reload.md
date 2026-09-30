---
title: Stale-config session reload — a chat picks up changed config at its next turn
status: in-progress
author: billygerhard
created: 2026-09-30
last-audited: 2026-09-30
audited-at: aec81410e2
doc-pr: null
implementation-prs: [14810]
tracking-issues: [10211]
supersedes: []
superseded-by: []
---

# RFC: Stale-Config Session Reload

> **Status:** `in-progress`. The implementation is live in an open PR and
> nothing is on main. Verified at `aec81410e2`: no `stale_config` module exists
> under `src/kiro_crew/dashboard/`. A live chat moves onto changed config in two
> ways today. The manual Reload action, `api_chat_slot_reload` in
> `src/kiro_crew/dashboard/chat_handlers.py` (`POST /api/chat/slots/{slot}/reload`),
> reloads one chat and writes the `session_reload` notice (`SESSION_RELOAD_KIND`
> in `src/kiro_crew/dashboard/system_notices.py`). And four dashboard routes
> call `_reset_all_sessions` in
> `src/kiro_crew/dashboard/handlers/sessions.py`, which drains every live
> session and the warm pool with no in-flight-turn or sub-agent check: the
> agent-config `PUT` (`handlers/agents.py`), MCP sync (`handlers/mcp.py`, the
> only one that skips the reset when kiro-cli hot-reload is active), the
> Computer Use switch (`handlers/computer_use.py`) and the sessions restart
> route (`api_sessions_restart`, `POST /api/sessions/restart`, in
> `handlers/sessions.py`). A backend change reaches no live session:
> `SessionManager.refresh_defaults` applies it to new sessions only. On KAS one more path
> exists: `invalidate_stale_kas_session` and `reproject_claimed_session` in
> `src/kiro_crew/agent_sdk/spec_hooks.py` reset an idle session at the turn
> boundary when a `hooks` edit now covers a capability the live session
> auto-approves.
> The implementation is [#14810](https://github.com/kirodotdev/KiroCrew/pull/14810),
> for [#10211](https://github.com/kirodotdev/KiroCrew/issues/10211).

## Summary

When the config a chat's agent process was started with has changed, the chat
reloads that process at the start of its next turn, before the message is sent,
and keeps the conversation. It does this only when the chat is idle and has no
sub-agents attached; otherwise the reload waits and the Reload menu row says so.
This changes a default for every chat, with no setting.

## Motivation

A chat's agent process reads some inputs exactly once, when it starts: the MCP
servers it mounts, the agent spec that names them (and each server's env), the
MCP gateway stub servers, and the ACP backend itself. Four dashboard routes
reset every live session (Status), but an edit made any
other way (a spec or `mcp.json` edited on disk or by an app, a workspace
`.kiro/` file, a backend change in `config.json`) never reaches the running
process. The chat keeps working with the old tools, nothing tells the user, and
the first sign is usually a tool that is missing or behaves wrongly.

The remedy already exists: the Reload action tears the process down and resumes
the conversation on a fresh one. But the user has to know that a reload is
needed, find the action, and press it. kiro-cli 2.21 and later
(`MCP_HOT_RELOAD_MIN_KIRO_CLI_VERSION` in `src/kiro_crew/mcp_hot_reload.py`)
reconciles MCP server edits in its agent file and `mcp.json`, but not the
spec's other fields (`prompt`, `tools`, `hooks` and the rest) or the backend,
and other backends reconcile nothing.

## Goals

- A chat whose spawn-time config changed runs its next turn on the new config,
  without the user pressing anything.
- The conversation is preserved across the reload.
- A reload never kills a streaming turn or an attached sub-agent's work.
- The user and the agent can both see when a reload is pending.

## Non-goals

- Reloading at the moment config is written, beyond the four dashboard
  writers that already do it. Extending that would kill streaming turns and
  shared sub-agent processes (Alternatives).
- Changing those four routes' `_reset_all_sessions` calls. They stay as they
  are; the turn-boundary check covers the edits they never see. Narrowing them
  onto the turn-boundary path is a possible follow-up (Open questions).
- Pushing new config into a running process. ACP has no message for it.
- Reloading for model or reasoning-effort changes. A chat's own pick is
  applied in place (`session/set_model` in `api_chat_slot_model`) and only
  resets the session when that is impossible, and neither case re-reads
  spawn-time config. A change to the configured default is not a reason to move
  a running conversation.
- An opt-out setting (Open questions).
- Keeping a pending reload across a gateway restart. A restart already starts
  fresh processes.

## Design

### 1. Fingerprint at spawn

When a chat's process starts (at the eager spawn for a prewarmed provider,
otherwise at its first turn, and for a process taken from the warm pool, at the
pool's own spawn of it, carried with the process when a chat claims it) the
gateway records a fingerprint of its inputs,
reading every file through the credential gate. The fingerprint is defined by
the FIELDS a backend reads at spawn, not by whole-file digests, because a
backend's own hot-reload covers only some fields of a file, and some fields in
the same file are read by no spawn at all. It has two halves:

- **reconcilable:** the MCP fields kiro-cli 2.21 and later applies live
  (`src/kiro_crew/mcp_hot_reload.py`): the `mcpServers` entries of the agent spec
  and of the `mcp.json` files, with their `disabled` and `disabledTools`, and the
  `@server` refs ADDED to the spec's `tools` (the hot-reload contract in that
  module names an added `tools` ref and nothing in `allowedTools`). Counted as
  stale only on a provider that does not reconcile them itself. A `@server`
  ref REMOVED from `tools` is spawn-only: the same contract says a removed ref
  stays mounted while its server keeps running, and only the dashboard's own
  MCP sync compensates by also writing `disabled: true`, so a removal by any
  other writer needs a relaunch. The project's own `.kiro/settings/mcp.json`
  is the exception: it is compared on its own, owes nothing on a provider that
  hot-reloads MCP, and on any other provider is manual-only (it waits for a
  Reload press and never relaunches a chat by itself), because an agent
  working in the project can write it (Security considerations).
- **spawn-only:** every other agent-spec field the backend reads at spawn (for
  example `prompt`, `hooks`, `resources`, all of `allowedTools`, and the
  non-`@server` entries of `tools`) and the ACP backend. `allowedTools` is here
  because it is the auto-approve list: a grant revoked by a writer other than
  the dashboard (for example the allowed-tools ceiling applied when an agent
  config is rebuilt) must not leave a live process auto-approving it, and no
  hot-reload contract covers the field. The cost is that an MCP enable which
  also writes an `allowedTools` grant reloads idle chats even on kiro-cli 2.21
  and later. KAS re-reads `hooks` from disk every turn for its own hook firing,
  but its per-session hook projection is still fixed at spawn, so `hooks`
  stays here too. Otherwise no backend re-reads these, so a change is stale
  everywhere. A spawn-only change that comes from a
  WORKSPACE-level spec is recorded as "manual only": it never reloads on its
  own (Security considerations).

The spec fingerprinted is the file the chat's backend actually loads, never a
file it ignores:

- **kiro-cli:** `*.json` specs only, the project's `.kiro/agents` first and
  then `~/.kiro/agents`; within a folder a spec that declares the agent's name
  wins over a filename match. A Markdown spec is never its spec, so an edit to
  one changes nothing, and a revocation in the JSON it does load is still seen.
- **KAS:** `~/.kiro/agents` only, JSON or Markdown, `<name>.json` before
  `<name>.md`. It never reads a project spec, so a KAS chat is never a
  workspace-spec chat.
- **Every other backend:** the spec the gateway hands it, the project's first
  and then the user-level one, either format.

Whether a chat is a workspace-spec chat follows the same resolved file.

Two inputs are deliberately left out, because a relaunch cannot change the
outcome and reloading for them would cost a cold start and show a false notice:

- **`model` in the spec.** The model is applied through its own path (Non-goals),
  and a backend that does not project it (KAS, `src/kiro_crew/acp/kas_agents.py`)
  would otherwise reload running chats whenever a default-model rewrite touches
  the spec file.
- **The MCP gateway stub-server set.** A stub change takes effect only at the
  next gateway start, which already starts every chat on a fresh process; a
  relaunched chat before then gets the same stub topology it had.

Nothing is marked when config is written, so no config writer has to know which
chats it affects, and a chat nobody talks to again costs nothing.

### 2. Check at the next turn boundary

At the start of the next turn, before the session is acquired, the gateway
recomputes the fingerprint and compares. If it is stale:

- **Idle, no sub-agents attached:** reload through the Reload action's own path,
  then send the message. The chat shows the existing `session_reload` notice
  with a "picked up config changes" reason.
- **The chat's agent spec is workspace-level:** every stale reading is held
  for a press, never relaunched automatically or at the agent's request
  (Security considerations, "No automatic relaunch onto a workspace spec").
- **A manual-only change is stale** (a workspace spec's spawn-only fields),
  alone or together with other changes: send the message on the current
  process and mark the reload pending. Only a person's Reload press applies it.
  A reload is all-or-nothing, since the new process reads every file as it is on
  disk, so while a manual-only change is unapplied NOTHING else may reload the
  chat either: not a reconcilable or spawn-only change that would otherwise
  reload automatically, and not a `session_reload` request (§3). Otherwise
  those triggers would relaunch onto the manual-only change as a rider. The
  one exception is the existing KAS hook re-projection reset (Status), which
  this design leaves as it is: it fires only to put a newly added PreToolUse
  hook in front of a capability the session auto-approves, so holding it would
  keep an auto-approval live against the person's own hook. It is not a
  trigger this design adds.
- **Busy, or sub-agents attached:** send the message on the current process and
  mark the reload pending. The Reload menu row shows it, and the next boundary
  tries again.

Pressing Reload by hand settles a pending reload.

### 2a. Failure posture

The check never fails a turn on its own account, and it never reloads the same
chat twice for one edit:

- **Fingerprint recompute fails** (an unexpected error): no comparison is made
  this turn, nothing is recorded against the provider, nothing is marked
  pending, and the message is sent on the current process. The next turn tries
  again.
- **One input cannot be read** (a spec or `mcp.json` that exists but is
  unreadable or refused by the credential gate): that input alone is left out
  of the comparison; every other input is still compared and acted on, so an
  agent that makes its own workspace file unreadable cannot hide a user-level
  change (an `allowedTools` revocation, say) behind it. The unreadable input
  carries its previous recorded value forward rather than counting as a
  change, since it has no fields to assign to a half and treating it as a
  change would latch a manual-only hold with no config change behind it.
  An input that was already unreadable or refused when the spawn fingerprint
  was recorded has no previous value to carry: it stays out of the comparison,
  and its first successful read RECORDS its value against the provider rather
  than comparing it, so it can never register as a change on its own.
  `session_reload` reports that input's staleness as unknown, never as
  current. A file that is ABSENT is a real change (deleted config) and is
  compared like any other edit. If the same input is still unreadable at the
  NEXT turn boundary, the chat is also marked pending on the Reload row and
  writes one transcript notice that part of its config could not be read; the
  mark clears on its own once the input reads again.
- **The teardown is refused or raises** (another actor holds the session, or the
  reset errors): the message is sent on the current process, the reload stays
  pending, and the Reload row shows it.
- **The reload succeeds but the new config is broken** (for example a malformed
  `mcp.json` on disk): the new process starts, or fails to start, exactly as a
  brand-new chat on that config would, and that turn surfaces whatever that
  start reports. This is the same outcome the next new chat, any gateway
  restart, and the four `_reset_all_sessions` routes already produce for that
  edit; the design does not add a way to keep an old process alive against a
  known-bad file. There is no reload loop: the new process records the
  fingerprint it started from, so the same edit is not stale again, and if no
  process came up the next turn simply cold-starts on whatever config is then
  current, so fixing the file recovers the chat without pressing anything.
- **Config keeps changing between turns** (a tool that rewrites a spec or
  `mcp.json` with a volatile value, such as a rotated token in an MCP
  server's env, on every run): each turn would find a new fingerprint and
  reload again. A churn damper stops that. After a chat has reloaded
  automatically at three consecutive turn boundaries, the next stale reading
  does not reload: the chat is marked pending on the Reload row and writes one
  transcript notice that its config keeps changing and automatic reload is
  paused. A Reload press, or a turn boundary that finds the config unchanged,
  resets the count.

Validating a config edit before relaunching onto it, and keeping the old
process when validation fails, is listed under Open questions.

### 3. Agent surface

A `session_reload` MCP tool reports whether the calling chat is stale or has a
reload pending, and can request a reload. The calling chat always has its own
turn in flight while the tool runs, and the Reload path refuses a session with a
turn in flight, so for the calling chat a request marks the reload pending and
the next turn boundary performs it, unless a manual-only change is unapplied, in
which case the request stays pending until a person presses Reload (§2). The
tool reports which case it is in. The tool goes through the same
session-control API and principal gates as the dashboard action, so a caller the
API refuses is refused identically.

## Migration plan

### Phase 1: automatic reload, pending state, agent tool

[#14810](https://github.com/kirodotdev/KiroCrew/pull/14810).

**Exit criteria:**

- After an MCP server is added to an idle chat's agent on a provider that does
  not hot-reload, the chat's next message runs on a new process that mounts it,
  the conversation is intact, and one reload notice appears.
- The same edit on kiro-cli 2.21 or later, including the `@server` ref the
  dashboard's MCP sync adds to `tools`, does not make the turn-boundary check
  reload the process. A change to `allowedTools` does, on every provider.
- A warm-pool process spawned before a config edit and claimed by a chat
  after it is detected as stale at that chat's first turn boundary.
- A `prompt` or `hooks` edit to a user-level agent spec, or a backend change,
  reloads on every provider, kiro-cli 2.21 and later included.
- The same edit to a workspace-level spec does not reload; it marks the reload
  pending on the Reload row, and pressing Reload applies it.
- A chat whose agent spec is workspace-level is never relaunched automatically
  or by its own `session_reload` request, for any stale input; only a press
  relaunches it.
- A turn not run because a post-spawn re-read found an unapproved edit writes a
  notice that names the message as not processed.
- A `@server` ref removed from a USER-level spec's `tools` by a writer other
  than the dashboard's MCP sync reloads on every provider, kiro-cli 2.21 and
  later included. The same removal from a workspace-level spec is manual-only:
  it marks the reload pending, and a press applies it.
- A spec or `mcp.json` that is unreadable or refused for one turn is left out
  of that turn's comparison while every other input is still compared: a
  user-level `allowedTools` revocation made at the same time still reloads.
  Once the file is readable again with unchanged content, nothing is pending.
- An input unreadable when the spawn fingerprint is recorded, and readable at a
  later turn, is recorded at that turn and causes no reload and no pending
  mark.
- An input still unreadable at the following turn boundary marks the chat
  pending and writes one transcript notice; the mark clears once it reads
  again.
- With that workspace-spec edit unapplied, a further edit that would reload on
  its own (a user-level `mcp.json` change on a provider that does not
  hot-reload, or a user-level spec edit) also does not reload, and neither does
  a `session_reload` request from the chat; each leaves the reload pending
  until Reload is pressed, and the chat shows one transcript notice that a
  config change is waiting for a press.
- A change to the project's `.kiro/settings/mcp.json` causes no reload and no
  pending mark on a provider that hot-reloads MCP. On any other provider it
  marks the reload pending and a press applies it; it never relaunches the
  chat on its own, whether the chat's spec is user-level or workspace-level.
- A `model`-only rewrite of the spec, or a stub-server change, does not reload
  any chat.
- On kiro-cli, with a project Markdown spec beside a user-level JSON spec,
  revoking an `allowedTools` grant in the JSON reloads the chat, and an edit to
  the Markdown file alone does nothing.
- With a sub-agent attached, the message is sent without a reload and the
  Reload row shows the pending state; once the sub-agent detaches, the next
  message reloads.
- Pressing Reload clears the pending state.
- A fingerprint recompute that raises, or a refused teardown, still sends the
  message on the current process; a refused teardown leaves the reload pending.
- After a reload onto an edit, the next turn with no further edit does not
  reload again.
- Config that changes before every turn reloads the chat at most three turns
  in a row; the fourth marks it pending and writes one notice, and a press or
  an unchanged turn resets the count.
- The Reload row's pending note says the reload happens at the next message
  only when it will; for a hold that only a press applies (manual-only,
  unreadable input, paused churn), it says a press is needed.
- `session_reload` reports the same pending state as the Reload row; a
  request from the calling chat marks the reload pending and the chat's next
  turn performs it; the tool is refused for every principal the
  session-control API refuses.

## Backward compatibility

Compatible. Chats whose config never changes behave exactly as today. A chat
started before the upgrade has no recorded fingerprint and records one at its
next spawn.

## Security considerations

Config files are read through the existing credential gate, so the fingerprint
can hash only what the gateway is already allowed to read, and only digests are
kept. The reload reuses the Reload action's path and guards: it refuses while a
turn is in flight or sub-agents are attached. The agent tool reuses the
session-control principal gates.

**Who can author a change that triggers a reload.** An automatic reload turns a
config edit into a live change at the next turn with no human press, so the
question is whether an agent can arm its own reload by editing config. By input:

- **User-level agent specs (`~/.kiro/agents`) and the global
  `~/.kiro/settings/mcp.json`:** write-protected against the agent's own
  file-edit tool (`_KIRO_AGENTS_DIR` and `_KIRO_SETTINGS_MCP_JSON` on
  `_WRITE_PROTECTED_HOME_PATHS` in `src/kiro_crew/security/paths.py`). That
  protection is enforced at the file-edit tool gate; the shell gate does not
  match paths in command text, and the OS sandbox does not deny these paths.
  So "trusted" here means the same thing it means for every other decision
  this tier feeds (the `autoApprove` entries in the same `mcp.json`, the
  resource ceilings in `config.json`): not writable through the agent's
  file-edit tool. An agent that can run an auto-approved shell command can
  write these files, and can equally write them to reach a new chat or the
  next gateway restart today; this design does not add a stronger boundary
  and does not rely on one existing.
- **ACP backend:** read from the crew `config.json` / `config.local.json`
  (`acp_backend` in `src/kiro_crew/config/`), both on the same
  write-protected tier, with the same file-edit-gate-only enforcement as the
  bullet above. It is written through
  the owner's config PATCH, and no path resets a live session on a backend
  change (`refresh_defaults` applies it to new sessions only), so the spawn-only
  half is what moves a live chat onto it.
- **Workspace-level files under the project's `.kiro/`:** not fenced; an agent
  working in the project can edit them. They split by half:
  - **Their reconcilable MCP fields** (workspace `mcp.json`, a workspace spec's
    `mcpServers` and `@server` refs ADDED to its `tools`) are applied live by a
    provider that hot-reloads MCP, as on base. On any other provider they are
    manual-only: they mark the reload pending and only a press relaunches.
    A workspace `mcp.json` is held this way even on a chat whose spec is
    user-level, because it is the one agent-writable file such a chat reads.
    A `@server` ref REMOVED from a workspace spec's `tools` is spawn-only, so
    manual-only like the fields below. No automatic relaunch therefore runs on
    any agent-writable file; the existing no-human path is the provider's own
    live hot-reload, which this RFC does not change.
  - **A workspace spec's spawn-only fields** (`prompt`, `hooks`, `resources`,
    all of `allowedTools`, the non-`@server` entries of `tools`, and the rest) do
    NOT reload automatically. On
    base no running process applies them: they reach only new chats and
    restarts. Reloading for them would let an agent change the prompt, or add a
    hook that runs a command, in its own live conversation at its next turn,
    which is a new no-human path. So a change here only marks the reload
    pending and shows it on the Reload row; the person applies it by pressing
    Reload, exactly as today. Because a reload applies every file at once, an
    unapplied change here also blocks every other reload of that chat,
    automatic or agent-requested, until the press (§2), so no trigger this
    design adds can carry it in. The pre-existing KAS hook re-projection reset
    is left as it is, for the reason §2 gives. The same fields in a user-level
    spec, which the agent cannot write, reload automatically.

**Residual: the hold can be armed by the agent.** Because a workspace spec is
agent-writable, an agent can create a manual-only change (one byte of its
workspace `prompt`) and so hold back every automatic reload of its own chat,
including one a trusted writer needs, such as an `allowedTools` grant revoked in
a user-level spec. The design does not pretend otherwise. What it adds is that
the hold is never silent when something trusted is waiting behind it: when a
held reload also carries a change that would have reloaded on its own, the chat
writes one transcript notice (once per held set of changes, not every turn)
saying a config change is waiting for a Reload press, in addition to the Reload
row. Resolving the hold without the rider (relaunching onto the user-level
change while keeping the workspace spec's old fields) would need the gateway to
feed kiro-cli a spec other than the one on disk, which it does not do today; it
is listed under Open questions.

**No automatic relaunch onto a workspace spec.** A relaunch reads the agent
spec from disk, and a workspace spec is agent-writable, so any relaunch the
agent can cause (automatic, or by its own `session_reload` request) could load a
spec the agent changed and then changed back within the moment the new process
reads it; no re-read before or after the spawn can see such an edit. So for a
chat whose agent spec resolves to a workspace-level file, the gateway never
relaunches on its own or at the agent's request: any stale reading for that
chat, whatever half it is in, is held for a press. Only a person's Reload press
relaunches it, which is exactly the path that exists today. The pre-teardown
and post-spawn re-reads stay as defence in depth for that press. A chat whose
spec is user-level (not agent-writable) keeps automatic reload. Lifting this
for workspace specs needs the gateway to hand kiro-cli a spec snapshot rather
than the file on disk (Open questions).

A tool that a reloaded process newly mounts is subject to the same tool-approval
policy as the same tool in a freshly started chat; this design adds no approval
path and bypasses none. Telling an agent's workspace edit apart from the
person's would need provenance for each write, which the gateway does not
record, so the rule above is by field and location, not by author: a person's
own edit to a workspace spec's `prompt` also waits for a Reload press.

## Alternatives considered

- **Reload when config is written, for every writer.** Four dashboard routes
  already do this through `_reset_all_sessions`. Extending it applies changes
  sooner, but kills a streaming reply and throws away attached sub-agents' work,
  and it cannot cover edits no gateway writer makes (a file edited on disk, a
  workspace `.kiro/` file).
- **Opt-in setting.** Avoids the default change, but leaves every user on stale
  config unless they find the setting, and the failure it prevents is silent.
- **Banner only.** Tells the user a reload is needed without doing it. Cheaper,
  but still leaves the user to find and press Reload, which is the problem in
  #10211.

## Open questions

1. Should the four `_reset_all_sessions` callers move onto the turn-boundary
   path, so a dashboard config save also stops killing in-flight turns and
   attached sub-agents? Out of scope here; it would be its own change.
2. Should there be an owner setting to turn automatic reload off? It can be
   added later without changing the design.
3. Should workspace-level reconcilable MCP edits also wait for a Reload press?
   They do not today because kiro-cli 2.21+ already applies them live with no
   human step; holding them only on other backends would make the two behave
   differently for no security gain (Security considerations).
4. Should the reload validate the changed config (for example, parse
   `mcp.json`) before relaunching, and keep the current process when it does
   not parse? Today no path does this: a new chat or a restart starts onto the
   broken file as well (Failure posture).
5. Should a held reload be resolvable without the manual-only rider, by
   relaunching onto the trusted change while keeping the workspace spec's
   previous fields? That needs the gateway to pass kiro-cli a spec snapshot
   rather than the file on disk (Security considerations, Residual).
