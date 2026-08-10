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
- **orchestrator** — wave loop over the tasks.md DAG. Review verdicts judge test quality explicitly (assertions derived from the code under test rather than test-constructed values, the test fails when the covered behavior is wrong, error and boundary cases present); failing those criteria is a changes-required verdict rather than a comment. For each dispatch it determines the work's role (design, review, implement), resolves the optional Host_Agent plus model and effort from the selected Cost_Profile (falling back to the session default agent/model with a report), and passes them per-call to the subagent spawn (`agent=`, `model=`, `effort=` are existing per-call parameters). Config-time validation checks an assigned agent's tool surface for the Engine_MCP_Server grant, catching explicit `tools[]` allowlists that would silently filter the spec tools. Review verdict required before completion; any unsuccessful completion (implementation failure, changes-required verdict, infrastructure failure) retries up to the limit; task status persisted after every transition for resume.
- **delivery** — executes Delivery_Workflow stages (isolate, submit, verify, publish, teardown) as configured commands, in the order the workflow declares, so verify-class gates can run before submit (analyzers pass before a review is raised), after it (CI on the review artifact), or both. Verify commands are Quality_Gates carrying a severity: blocking failures stop the flow and dispatch fix tasks; advisory failures are recorded and surfaced without stopping. Base branch substitution lets a gate compare the change against its base (coverage delta, changed-files linting). Substitution builds an argv array: the command template is tokenized once, each `{variable}` token is replaced by the literal value as a single argument, and the command runs with `subprocess.run(argv)` — never through a shell string. A referenced variable with no value fails the stage before execution. Publish stdout is captured and scanned for URLs into the notification, Review_Queue entry, and audit log. Teardown runs at archive.
- **watchers** — runs each enabled source's poll command on its interval (invoked from a zero-token script cron), applies the field mapping, diffs against the previous snapshot to derive new items and lifecycle transitions (reopened, cancelled), determines each item's submitter class (configured maintainer list or the source's author-association field, defaulting to least-trusted when undetermined), claims `(item id, lifecycle generation)` rows atomically in SQLite (unique constraint = exactly-once dispatch), enforces global and per-project concurrency caps with arrival-order queueing, screens dispatched items for prompt injection (bundled screening guidance + any configured intake guidance, run on the review role's model; a suspected-injection verdict quarantines the run at authoring level with findings in the Review_Queue — defense in depth on top of the hard rails: quoted data, argv substitution, agent-immutable config, budget ceilings), and starts headless runs in the source's target project, injecting any configured per-spec-type intake guidance (for example a project debugging playbook for bugfix runs) into the seed separately from the item's quoted data; runs execute in the project's working tree so native `.kiro/steering/` files apply. Item feedback commands post dispatch/completion updates. GitHub and GitLab presets ship as config files using `gh`/`glab`; Taskei arrives via the Provider_Interface in the internal build.
- **budget** — stamps the run identifier onto every session it creates; computes run cost by summing per-turn records from the gateway metering ledger (`<data home>/usage/tokens/YYYY-MM-DD.jsonl`) across the run's sessions; halts dispatch after in-flight turns when the ceiling is reached; enforces per-source spending caps independently; implements the kill switch (pause all watchers, halt all autonomous runs after in-flight turns).
- **workspaces** — ledger of every isolated workspace and per-run deployment; removes disposable materializations (worktrees, temp copies) at terminal state while never deleting branches or commits; archive triggers teardown plus ledger cleanup; manual cleanup action.
- **capabilities** — the provider registry. Every Delegable_Capability (analysis, authoring, review verdicts, task implementation, supplementary validation rules, watch sources, model catalogs) resolves from config to one of three transports: `builtin` (in-process, ships with the app), `mcp` (spawned MCP stdio child), or `command` (a program handed structured input, structured output parsed back — how an external coding agent or CLI serves a capability). One invocation path normalizes all three: schema-validated response, declared coverage surfaced, cost attributed to the run budget, output treated as untrusted data, degraded fallback to builtin on unavailability/timeout/invalid response, and the provider identity plus transport recorded in the audit log and shown in the UI. The **Engine_Floor** is not bindable at all — native-format validation, phase gates, autonomy resolution, budget enforcement, the claim ledger and the audit log always execute in the engine, and a supplementary validation provider may only *add* findings, never suppress or downgrade an engine finding or gate.
- **analysis** — the analysis capability's builtin provider path; resolves the Analysis_Provider from config and normalizes both paths behind one call. `provider: local` runs the bundled analyzer in-process; `provider: mcp` spawns `command`/`env` as an MCP stdio child and calls its analysis tool. Request carries document location, spec type, and format version; the response is schema-validated before use, findings are keyed to acceptance criteria so the engine can route them into the Review_Queue mechanically, declared `coverage.skipped` is surfaced rather than silently dropped, and any declared cost is attributed to the run's budget. Unavailable, timed-out, or schema-invalid responses fall back to local with a degraded marker and reason. Finding text is untrusted data throughout.
- **local analyzer** — deterministic structural checks (glossary terms used but undefined, unquantified qualifiers, criteria that are not independently testable, requirements with no covering task, overlapping or contradictory criteria within a requirement), emitting the same Analysis_Findings schema plus generated clarifying questions. No network, zero model credits, declares its depth as structural.
- **providers** — the Provider_Interface for the remaining pluggable capabilities: model catalog, review policy, additional watch sources. Bundled local defaults for each; enhanced providers surface degraded status without changing the tool surface.
- **analysis depth ladder** — three rungs behind one tool shape. **Structural** is the deterministic Local_Analyzer: no network, no credits, catches undefined glossary terms, unquantified qualifiers, untestable criteria, uncovered requirements, contradictory conditions. **Semantic** is a model-backed builtin that needs no service at all — it **dispatches an agent turn** with an authored analysis prompt at the agent, model, and effort configured for the analysis role, exactly as the builtin review and implementation providers already dispatch their own turns. Analysis stops being a special case: it is one more role whose depth the Cost_Profile dials. **Extended** is an external provider bound by config, declaring coverage beyond semantic. Binding a provider changes the *depth*, never which tools exist: every capability ships a working builtin, so no tool is absent or answers "not configured".

  The three have genuinely different execution models — deterministic returns at once, a dispatched turn runs for minutes, external runs for tens of minutes — so the one shape they share is an **asynchronous job**: submit returns an identifier, poll returns status, progress, and findings. Every job carries a total wall-clock deadline; a blocking call with no deadline is how a trickling provider stream hangs a caller indefinitely, which is a failure observed in practice, not a theoretical one. A dispatched turn's output is untrusted model output and is schema-validated before recording, and every result records its depth and provider so a clean structural pass is never mistaken for semantic correctness.
- **doctor** — one read-only aggregation over everything that can be wrong: phase-scoped prerequisites, source health, provider reachability and degradation, config validation, budget and kill-switch state, blocked runs, and whether the skill and MCP server actually reached sessions. It emits **Findings** with stable identifiers, and the engine's own refusals quote those identifiers — so "run refused" and "doctor says" are the same sentence rather than two vocabularies the user has to correlate. Reachable as an MCP tool and from the UI off the *same* operation, because a diagnostic that disagrees with the gate is worse than none. Individual checks fail into Findings rather than aborting the run of checks: the doctor's whole value is being callable when the app is broken.

  Findings also carry history, which is what makes the doctor an answer to **environment drift** rather than only to misconfiguration. A managed-fleet policy push or an unattended package update that removes, downgrades, or replaces `gh` produces a check that *used to pass* — materially different from one never configured, both in what it means and in what fixes it. Comparing against the last recorded result per identifier turns that into a **regression** Finding with the time it last passed, which is the one case worth a notification; an unchanged Finding stays quiet. This needs no new scheduler: the comparison runs on whatever already triggers an evaluation — a run gate, a watch poll, or a doctor call — so drift surfaces at the next gate rather than waiting for a human to wonder. Where a preset declares a minimum version, the check asserts the version, since a policy-pushed downgrade leaves the program present and the presence check green.
- **prerequisites** — a read-only, zero-token preflight per project, **scoped to the phase that needs each check**, because the programs required to watch, to build, and to deliver are different sets with different gates. A missing watch program means no run is ever created, so its failure mode is silence and its gate is the source's own health (never "no items"). A missing later-phase program means a run *does* start, spends credits authoring and building, and dies at delivery — so the run gate evaluates every phase the run's autonomy level will reach, **including phases that execute hours later**, and refuses before the first credit is spent. Blocked runs and unhealthy sources are audited.
- **presets** — named bundled Workflow_Presets (git + pull request, git + merge request, local-only) and the matching public watch sources, treated as read-only; a project selects one and overrides individual stage commands, so an organization's own review system replaces a stage rather than requiring a whole hand-written workflow. Users may define their own named presets, selectable identically. No preset for a non-public review or tracking system ships.
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
- **Config** — app-level file plus per-project overrides in the app data dir, written only by the UI config surface and the Setup_Assistant endpoint. Sections: `capabilities` (per capability: `transport: builtin | mcp | command`, plus `command`/`env`/`timeout_s` when delegated), `quality_gates` (per gate: name, commands, `severity: blocking | advisory`, and stage position), `cost_profiles` (role entries are `{agent?, model, effort}`), `projects` (profile, concurrency, protected, auto_integrate, review_feedback, notify, limits, variables, workflow stages), `sources` (enabled, poll, map, every, project, base_branch, maintainers, types, autonomy — both keyable per submitter class, spend_cap, feedback). Bundled defaults for every numeric limit; effective-value API returns value plus origin.
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
- **Analysis conformance suite**: the published request/response JSON Schemas plus a `verify-analyzer` runner that feeds fixture documents (a planted ambiguity, contradictory criteria, a coverage hole, an oversized document, a deliberately malformed response) at a candidate provider and asserts schema validity, planted-defect detection, declared skips, timeout honoring, and repeatability. The local analyzer runs the same suite, so the reference implementation is held to the published contract.
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
| Every delegable capability is provider-bound; the Engine_Floor is not | The app becomes a host rather than a monolith — bring your own analyzer, reviewer, or coding agent — while validation, gates, autonomy, budget and audit stay engine-enforced, so a permissive provider can never weaken the guarantees |
| Three transports (builtin / mcp / command) | Covers in-process defaults, MCP servers, and plain CLIs (an external coding agent) without a plugin API or a language binding |
| Providers may add findings, never suppress them | Keeps "rules as code" true under delegation: extension is additive by construction |
| Analysis delegated by configuration to an MCP command, never vendored | Keeps the public build free of any non-public implementation while allowing an enhanced analyzer where one exists; the seam is config, so the public build is complete on its own |
| Findings keyed to acceptance criteria, with declared coverage | Lets the engine route findings and questions mechanically instead of handing prose to a human; declared skips make partial analysis honest rather than invisible |
| A conformance runner ships with the contract | An extension point without an executable conformance check is a promise, not an interface |
| Clean-room implementation from public surfaces | The app carries no provenance question into an open repository; prompts are authored fresh and no non-public schema, code, or endpoint appears in the tree |
| Working name `spec-engine`, final name gated on sign-off | App/registry names are one-way doors |
| Depth is the variable; the tool list is fixed | A surface that gains and loses tools with configuration makes every agent's instructions conditional; holding the surface constant and varying declared depth means a stock agent's call path is identical whether or not anything is bound |
| The semantic tier dispatches an agent turn, not the caller's session | Dispatch keeps the submit/poll shape genuinely identical across all three tiers (no extra "awaiting the caller" state), makes analysis consistent with the review and implementation builtins instead of a special case, and keeps the spend inside the run's budget ceiling and kill switch — work executed in the caller's own session would escape the budget accounting the spec otherwise insists on |
| One async job shape, always deadlined | The three tiers cannot share a synchronous shape, and an undeadlined blocking analysis is a hang we have already hit; submit-and-poll with a wall-clock bound is the only shape all three satisfy honestly |
| Drift is a first-class Finding, not a re-run of setup | Managed fleets change tooling underneath a working config; distinguishing "never passed" from "passed until Tuesday" changes both the diagnosis and the remedy, and only the regression justifies interrupting the user |
| The doctor reports; it never fixes | An auto-fixing doctor would need write authority over exactly the two objects the spec keeps config-only (Autonomy_Policy, Delivery_Workflow), reintroducing the escalation path R6 and R13.12 exist to close |
| Refusals quote doctor Finding identifiers | A refusal that says only "prerequisites unmet" sends the user hunting; sharing one identifier vocabulary makes the gate self-explaining and keeps the panel, the tool result, and the audit entry from drifting |
| Prerequisites are phase-scoped and checked forward | A missing watch program yields silence, not a failed run, so it belongs to source health; a missing delivery program only bites hours in, after credits are spent, so the run gate must check phases it has not reached yet rather than discovering the gap on arrival |
| Bundled presets cover public hosts only; org systems arrive as overrides | Keeps the clean-room boundary (no non-public tooling names in the tree) while making the common case one selection and the org case a stage override rather than a fork |
| Quality gates are verify commands with a declared severity and position | Reuses the config-command model rather than a second mechanism, while letting analyzers run pre-submit (fix before a human sees it) and letting advisory findings surface without blocking |
| Test quality is a review-verdict criterion, not a comment | A green suite that cannot fail is worse than no suite; making it a verdict makes it gate task completion, and an optional mutation-probe gate turns it into a mechanical check |
| A CLI driver is a future consumer, not in this scope | The engine is a library with a published contract, so a command-line entry point over the same calls is additive; adding it later costs a thin driver, not an engine change |
