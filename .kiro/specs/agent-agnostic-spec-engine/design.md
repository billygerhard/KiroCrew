# Design Document

## Overview

This feature ships one KiroCrew app, working name `spec-engine` (final name needs explicit sign-off; names are one-way doors), that packages a rules-as-code Spec_Engine library, a thin MCP wrapper exposing it to any agent, a set of zero-token drivers (watchers, delivery runner, dispatcher), and the absorbed Spec Builder UI. Two small core changes land as a prerequisite PR: per-app tool-approval grants for app-seeded sessions, and verification of the builtin registration path for app-vended MCP servers.

The design follows three verified constraints from the KiroCrew codebase:

- `ROLE_MODEL_KEYS` in `src/kiro_crew/config/loader.py` is a closed allowlist (`background`, `subagent`); `coerce_role_models` drops unknown keys. Spec role routing therefore uses per-dispatch model/effort parameters (already supported by subagent spawn and workflow `ctx.agent()` calls) driven by app-owned Cost_Profile config, not core `role_models` keys.
- `src/kiro_crew/apps/discovery.py` copies the typed `agents`, `skills`, and `mcpServers` manifest fields into the persisted app snapshot for builtin and installed apps alike; the contrary comment in the Spec Builder backend is stale. The app relies on this path to vend its skill and MCP server.
- The unattended approval mechanism exists for crons (`approval_mode` flowing through `mcp_cron.py` and `subagent.py`); the core enabler extends the same grant shape to app-seeded sessions rather than inventing a parallel mechanism.

## Architecture

Three layers. Only the middle one owns rules.

```
+---------------------------------------------------------------+
| DRIVERS (thin; no rules)                                      |
|  Spec Builder UI   Watcher crons   Setup_Assistant   Headless |
|  (React, absorbed) (zero-token)    (agent session)   seeder   |
+-------------------------+-------------------------------------+
                          | library calls (in-process)
+-------------------------v-------------------------------------+
| ENGINE (spec_engine Python library)                           |
|  validator  phases  runs+queue  autonomy  orchestrator        |
|  delivery   watchers  budget  workspaces  providers  config   |
|  audit                                                        |
|         ^ MCP wrapper (engine_mcp) exposes the same calls     |
|         | to any Host_Agent over stdio                        |
+-------------------------+-------------------------------------+
                          | reads/writes
+-------------------------v-------------------------------------+
| ARTIFACTS (the interop contract)                              |
|  <project>/.kiro/specs/<name>/{requirements,design,tasks}.md  |
|  + .config.kiro   (native format only; engine state excluded) |
|  Engine state: <app data>/state.db (SQLite) + audit JSONL     |
+---------------------------------------------------------------+
```

Public/internal split: the app ships with local default providers only. An organization's internal consumer package can wrap the open engine and register enhanced providers (enhanced requirements analysis, private model catalogs, organization-specific review policy). The MCP tool surface is identical in both builds.

## Components and Interfaces

### Spec_Engine library (`apps/spec-engine/engine/`)

- **validator** — implements the native Kiro format rules (sections, EARS shape, requirement numbering, checkbox syntax, waves DAG JSON), task-to-criterion link resolution, requirement coverage reporting, and DAG checks (acyclic, every incomplete leaf in exactly one wave, sequential wave ids). Returns a list of `{file, location, rule, severity, message}` violations. Interface: `validate(spec_dir) -> ValidationReport`.
- **phases** — derives phase from artifacts on disk plus recorded approvals; refuses advancement on validation failure or missing/stale approval; persists approver identity and timestamp; marks approvals stale when a document's content hash changes after approval. Interface: `phase(spec) -> PhaseState`, `advance(spec, actor) -> Result`, `approve(spec, gate, actor)`.
- **runs** — the run lifecycle state machine (queued, authoring, awaiting review, executing, delivering, done, failed, halted for budget, cancelled, stalled), per-phase timeouts, resume from persisted state, and the Review_Queue projection (all runs waiting at human-reserved gates). All state-changing operations acquire a per-spec lock row; a conflicting concurrent write is rejected with the current state.
- **autonomy** — resolves the Autonomy_Policy for a (source, spec type, submitter class): unconfigured resolves to authoring-only with human-reserved execution; configured resolves to the configured level; levels strictly ordered with lower levels implied. Loaded from configuration only; the MCP surface has no mutating tool for it.
- **orchestrator** — wave loop over the tasks.md DAG. For each dispatch it determines the work's role (design, review, implement), resolves the optional Host_Agent plus model and effort from the selected Cost_Profile (falling back to the session default agent/model with a report), and passes them per-call to the subagent spawn (`agent=`, `model=`, `effort=` are existing per-call parameters). Config-time validation checks an assigned agent's tool surface for the Engine_MCP_Server grant, catching explicit `tools[]` allowlists that would silently filter the spec tools. Review verdict required before completion; any unsuccessful completion (implementation failure, changes-required verdict, infrastructure failure) retries up to the limit; task status persisted after every transition for resume.
- **delivery** — executes Delivery_Workflow stages (isolate, submit, verify, publish, teardown) as configured commands. Substitution builds an argv array: the command template is tokenized once, each `{variable}` token is replaced by the literal value as a single argument, and the command runs with `subprocess.run(argv)` — never through a shell string. A referenced variable with no value fails the stage before execution. Publish stdout is captured and scanned for URLs into the notification, Review_Queue entry, and audit log. Teardown runs at archive.
- **watchers** — runs each enabled source's poll command on its interval (invoked from a zero-token script cron), applies the field mapping, diffs against the previous snapshot to derive new items and lifecycle transitions (reopened, cancelled), determines each item's submitter class (configured maintainer list or the source's author-association field, defaulting to least-trusted when undetermined), claims `(item id, lifecycle generation)` rows atomically in SQLite (unique constraint = exactly-once dispatch), enforces global and per-project concurrency caps with arrival-order queueing, screens dispatched items for prompt injection (bundled screening guidance + any configured intake guidance, run on the review role's model; a suspected-injection verdict quarantines the run at authoring level with findings in the Review_Queue — defense in depth on top of the hard rails: quoted data, argv substitution, agent-immutable config, budget ceilings), and starts headless runs in the source's target project, injecting any configured per-spec-type intake guidance (for example a project debugging playbook for bugfix runs) into the seed separately from the item's quoted data; runs execute in the project's working tree so native `.kiro/steering/` files apply. Item feedback commands post dispatch/completion updates. GitHub and GitLab presets ship as config files using `gh`/`glab`; Taskei arrives via the Provider_Interface in the internal build.
- **budget** — stamps the run identifier onto every session it creates; computes run cost by summing per-turn records from the gateway metering ledger (`<data home>/usage/tokens/YYYY-MM-DD.jsonl`) across the run's sessions; halts dispatch after in-flight turns when the ceiling is reached; enforces per-source spending caps independently; implements the kill switch (pause all watchers, halt all autonomous runs after in-flight turns).
- **workspaces** — ledger of every isolated workspace and per-run deployment; removes disposable materializations (worktrees, temp copies) at terminal state while never deleting branches or commits; archive triggers teardown plus ledger cleanup; manual cleanup action.
- **providers** — the Provider_Interface: requirements analysis, model catalog, review policy, watch sources. Bundled local defaults for each; enhanced providers surface degraded status in tool results without changing the tool surface.
- **config** — schema, per-project overrides, bundled numeric defaults, and effective-value resolution with origin (default vs configured). Zero-config resolves to IDE-parity behavior: work in the project tree, autonomy capped at execution, bundled budget ceiling on every headless run.
- **audit** — append-only per-spec event log (JSONL in app data): gate decisions, initiators, stage commands, outcomes, costs, warnings and acknowledgments.

### Engine_MCP_Server (`apps/spec-engine/engine_mcp/`)

Thin stdio JSON-RPC wrapper over the library; every tool call maps 1:1 onto a library call so MCP and library paths produce identical state. Tool surface: `get_authoring_prompt`, `get_bugfix_prompt`, `get_orchestrator_prompt` (prompt-as-tool-result carries all workflow knowledge), `validate_spec`, `analyze_requirements` (provider-backed; local default), `task_list`/`task_get`/`task_update`, review verdict tools, subagent orchestration tools, `get_user_input`. Conformant JSON-RPC: `prompts/list` and `resources/list` answer empty sets; unknown methods return -32601. No tool mutates the Autonomy_Policy or the Delivery_Workflow. Guidance that cannot be supplied returns an error, never partial text.

### Drivers

- **Spec Builder UI (absorbed)** — the existing app's backend collapses to thin endpoints over the engine library: list specs, render docs, relay chat turns, Review_Queue with approve/request-changes actions, run detail with per-run cost from the ledger, configuration surface with effective-value display, kill switch. `_seed_prompt`, `_TYPE_PLAN`/`_TYPE_GUIDANCE`, and `_derive_phase` are deleted in favor of engine calls. The new app replaces the `spec-builder` builtin; prior specs remain valid artifacts because the format is unchanged.
- **Watcher cron** — one gateway script cron per instance tick invoking `engine.watchers.tick()`; zero model calls while idle.
- **Headless run driver** — seeds ordinary agent sessions through the existing chat-runner path with the run id stamped and the app's granted approval posture applied; refuses the run on posture mismatch. Sessions appear in the dashboard session list like any chat.
- **Setup_Assistant** — an agent session seeded with a setup skill; inspects KiroCrew memory, `.kiro/steering/`, project docs, and CI configs; proposes config with per-setting evidence through a validated config-write endpoint; asks conversationally for what it cannot infer; requires per-level confirmation for execution/delivery/integration and asks (never infers) the Cost_Profile.
- **Review-feedback watcher** — per-project opt-in poll of the run's review artifact for new comments (configured commands, zero-token idle); new comments dispatch fix tasks bounded by retry limit and budget ceiling.

### Core enablers (separate prerequisite PR to kiro_crew)

1. **Per-app approval grants**: extend the cron `approval_mode` grant shape so an app manifest may declare a wanted posture and configuration may grant it; the gateway applies it to sessions the app seeds. Agents cannot modify it at runtime.
2. **Builtin MCP vending verification**: `discovery.py` already persists `mcpServers` for builtins; add a regression test that a builtin-declared MCP server reaches a Host_Agent session's tool surface, and fix the stale Spec Builder comment.

## Data Models

- **`state.db` (SQLite, app data dir)** — tables: `specs` (project, name, type, phase cache, lock), `approvals` (spec, gate, actor, ts, doc hash, stale), `runs` (id, spec, source item, state, timestamps, posture, cost cache), `claims` (source, item id, generation, run id; UNIQUE(source, item id, generation)), `workspaces` (run id, kind, location, deployment address, cleaned), `queue` (arrival-ordered pending dispatches).
- **Config** — app-level file plus per-project overrides in the app data dir, written only by the UI config surface and the Setup_Assistant endpoint. Sections: `cost_profiles` (role entries are `{agent?, model, effort}`), `projects` (profile, concurrency, protected, auto_integrate, review_feedback, notify, limits, variables, workflow stages), `sources` (enabled, poll, map, every, project, base_branch, maintainers, types, autonomy — both keyable per submitter class, spend_cap, feedback). Bundled defaults for every numeric limit; effective-value API returns value plus origin.
- **`.config.kiro`** — unchanged native sidecar: `specId`, `workflowType`, `specType`. The engine writes nothing else into spec directories; if state persistence fails, the operation fails rather than polluting a spec document.
- **Audit log** — per-spec JSONL: `{ts, run, event, initiator, detail, cost?}`.
- **Run context variables** — spec name/type, workspace path, base branch, branch name, item id/URL, review title/summary (derived from the spec documents), plus configured custom variables.

## Correctness Properties

### Property 1: Wave ordering safety

FOR ALL task DAGs and execution traces, no task is dispatched before every task in every prior wave has reached a terminal state. **Validates: Requirements 1.5, 9.1**

### Property 2: Autonomy ladder monotonicity

FOR ALL policy configurations, an enabled level implies every lower level, and resolution never yields a level above the configured one. **Validates: Requirements 8.2, 8.7**

### Property 3: Claim exactly-once

FOR ALL sequences of poll snapshots, each (item, lifecycle generation) pair dispatches at most one run, and a reopened item forms a new generation. **Validates: Requirements 10.3, 21.1**

### Property 4: Substitution safety

FOR ALL command templates and variable values, substituted values appear as exactly one argv element with no shell interpretation, and a template referencing a valueless variable never executes. **Validates: Requirements 13.5, 13.6**

### Property 5: Budget attribution completeness

FOR ALL runs, the run's reported cost equals the sum of metering ledger records for sessions stamped with the run id, and a headless run always executes under a finite ceiling. **Validates: Requirements 16.1, 16.2, 24.2**

### Property 6: Phase gate soundness

FOR ALL edit/approve/advance sequences, advancement succeeds only when the current document validates and carries a non-stale approval, and any post-approval edit stales exactly the approvals whose documents changed. **Validates: Requirements 2.2, 2.3, 2.5**

### Property 7: Spec directory purity

FOR ALL engine operations, the set of files under `.kiro/specs/<name>/` after the operation contains only the native documents and `.config.kiro`. **Validates: Requirements 1.6, 1.7**

## Error Handling

- **Fail-fast rules**: unrecordable spec type fails creation; unavailable authoring guidance returns an error with no partial text; failed registration completes install but reports not-ready; posture mismatch refuses or halts the run; state-persistence failure fails the operation without touching spec documents.
- **Stage commands**: non-zero exit on verify dispatches fix tasks up to the retry limit then marks delivery failed; stderr and exit codes land in the audit log; missing variables fail before execution.
- **Runs**: phase timeout marks the run stalled and notifies; resume continues from persisted state; budget ceiling halts after in-flight turns with cost in the notification; item cancellation cascades cancel + archive + audit. State changes are primary and notification delivery is best-effort: a failed notification never unwinds run state and is recorded in the audit log.
- **Watchers**: a failing poll command marks the source unhealthy in the UI and skips the tick (no dispatch on partial data); a source with no target project never dispatches and reports the missing configuration.
- **Concurrency**: per-spec lock conflicts reject the second writer with current state; caps queue rather than drop, arrival-ordered.

## Testing Strategy

Frameworks: **pytest** for the engine and MCP wrapper (with **hypothesis** for the property-based tests), **vitest** for the UI driver, matching the repo's existing gates (pytest + isort + flake8 + mypy + tsc + vitest, run in a worktree).

- **Unit tests** per engine module: validator rule fixtures (valid and violating documents), phase machine transitions, autonomy resolution table, delivery substitution and output capture against a stub executor, watcher snapshot diffs and claim behavior, budget summation against fixture ledgers, config default resolution with origin.
- **Property-based tests** for Properties 1–7: generated DAGs, policy configs, poll snapshot sequences, command templates with adversarial variable values (shell metacharacters, newlines, quotes), edit/approve/advance sequences, and operation traces asserting spec-directory purity.
- **Integration tests**: drive the Engine_MCP_Server over stdio with the kiro-cli init sequence (initialize, prompts/list, resources/list, tools/list) and assert library-vs-MCP state equivalence on a temp project; a fixture git repository with a local bare remote exercises isolate/submit/verify/publish/teardown end to end without network; a seeded fake metering ledger exercises budget halt.
- **Core enabler tests**: regression test that a builtin-declared MCP server reaches a session tool surface; approval-grant tests that an app-seeded session receives exactly the granted posture and that no runtime path can elevate it.
- **No-network default**: all tests run offline; commands under test are stubbed binaries on PATH inside the test sandbox.

## Design Decisions

| Decision | Rationale |
|---|---|
| Engine as in-process library with a thin MCP wrapper | One rules implementation, two transports; guarantees the R3.4 state-equivalence property by construction |
| Cost_Profiles applied per-dispatch, not via core `role_models` | `ROLE_MODEL_KEYS` is a closed allowlist (verified); per-call model/effort params already exist; avoids a core config schema change |
| SQLite for state, claims, and ledgers | Atomic claim uniqueness, crash-safe resume, concurrent driver access; JSONL reserved for append-only audit |
| Engine state outside spec dirs, keyed by project+spec | Preserves IDE/CLI interop (R1.6); spec dirs stay byte-compatible with native tooling |
| Watchers and delivery as commands, argv substitution | Zero-token determinism (R17), no plugin code (R13.1), injection rail (R13.6) |
| Core enablers as a separate prerequisite PR | Approval grants and builtin vending are general-purpose gateway capabilities, reviewable independently of the app |
| Internal consumers wrap the open engine | An organization's internal package can pin the engine and register enhanced providers, keeping one tool surface |
| Working name `spec-engine`, final name gated on sign-off | App/registry names are one-way doors |
