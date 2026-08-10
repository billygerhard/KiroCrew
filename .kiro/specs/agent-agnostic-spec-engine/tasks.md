# Implementation Plan: Agent-Agnostic Spec Engine

## Overview

Ships the spec-engine app in dependency order: gateway enablers and engine foundations first, then the validator, phase machine, autonomy and run lifecycle, budget, delivery, watchers, and orchestrator, then the MCP surface, feedback loops, UI absorption, packaging, the doctor, setup assistant, and the verification suites. 19 parent tasks, 61 leaves, 8 waves; wave membership follows real code dependencies (state store and config before everything stateful; engine modules before the MCP wrapper and drivers; UI and packaging after the surfaces they expose).

## Tasks

- [ ] 1. Core gateway enablers (prerequisite PR to kiro_crew)
  - [ ] 1.1 Extend approval grants to app-seeded sessions
    - Generalize the cron `approval_mode` grant shape so an app manifest declares a wanted posture, configuration grants it, and the gateway applies it to sessions the app seeds
    - Refuse or halt a run whose applied posture does not match the grant; no runtime path can elevate a session's own posture
    - _Requirements: 7.1, 7.2, 7.4_
  - [ ] 1.2 Builtin MCP vending regression test
    - Regression test proving a builtin-declared MCP server and skill reach a Host_Agent session's tool surface; fix the stale contrary comment in the Spec Builder backend
    - Registration failure completes install but reports a not-ready state with the reason
    - _Requirements: 4.3, 4.4_

- [ ] 2. Engine foundations
  - [ ] 2.1 Config module with bundled defaults and effective-value resolution
    - Schema for app/project/source config; bundled default values for every numeric limit; absent optional settings resolve to defaults without failing
    - Effective-value API returning value plus origin (bundled default vs explicit); single validated write path for all config surfaces
    - _Requirements: 24.3, 24.5_
  - [ ] 2.2 SQLite state store, per-spec locking, and audit log
    - Tables: specs, approvals, runs, claims, workspaces, queue; per-spec lock rows rejecting conflicting concurrent writers with current state
    - Engine state lives entirely outside spec directories; persistence failure fails the operation and never writes state into a spec document; append-only per-spec audit JSONL
    - _Requirements: 2.6, 1.6, 1.7_

- [ ] 3. Validator
  - [ ] 3.1 Native-format document validator
    - Implement the native Kiro format rules (sections, EARS shape, numbering, checkbox syntax) returning every violation with file, location, and rule identifier
    - _Requirements: 1.1, 1.2_
  - [ ] 3.2 Task links, requirement coverage, and DAG validation
    - Resolve every leaf-task criterion reference against requirements.md; report uncovered requirements in tasks-only and full-spec validation
    - Waves DAG checks: acyclic, every incomplete leaf in exactly one wave, sequential wave ids
    - _Requirements: 1.3, 1.4, 1.5_

- [ ] 4. Spec lifecycle and phase machine
  - [ ] 4.1 Spec creation and spec types
    - Create spec directories with `.config.kiro` recording the spec type; derive the document plan (feature/bugfix/quick) from the recorded type
    - Unrecordable type fails creation atomically, leaving no partial directory; no validation or advancement without a recorded type
    - _Requirements: 5.1, 5.2, 5.3, 5.4_
  - [ ] 4.2 Phase derivation, advancement gates, and approval staleness
    - Read-only phase derivation from artifacts plus approval state; advancement refused on validation failure or missing approval with reasons returned
    - Approvals persist approver identity and timestamp; post-approval document edits (content hash) stale exactly the affected approvals
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5_
  - [ ] 4.3 Interactive and policy approval recording
    - Interactive runs record approvals only from explicit user action; headless runs record gate approvals from the Autonomy_Policy for covered gates and require humans for uncovered gates
    - Identical validation rules for artifacts from either mode
    - _Requirements: 6.1, 6.2, 6.4_

- [ ] 5. Autonomy policy and run lifecycle
  - [ ] 5.1 Autonomy_Policy resolution
    - Resolve per (source, spec type, submitter class); unconfigured resolves to authoring-only human-reserved execution; configured resolves exactly to the configured level
    - Strictly ordered levels with lower levels implied; loaded from configuration only
    - _Requirements: 8.2, 8.6, 8.7_
  - [ ] 5.2 Run state machine, timeouts, and resume
    - States: queued, authoring, awaiting review, executing, delivering, done, failed, halted for budget, cancelled, stalled; per-phase timeouts mark stalled and notify
    - Resume from persisted state at task granularity in execution and phase granularity in authoring
    - _Requirements: 18.1, 18.2, 18.3_
  - [ ] 5.3 Review_Queue and archival rules
    - Engine-exposed queue of runs at human-reserved gates, renderable by any driver; no time-based archival or expiry
    - Archive only on explicit user action or triggering-item cancellation; archived stays archived until explicitly unarchived; reversible
    - _Requirements: 18.4, 18.6, 18.7_
  - [ ] 5.4 Execution gate
    - Human-reserved execution starts only from explicit human action; authorized autonomy starts on gate satisfaction with no further trigger
    - Failed validation or missing approvals refuse regardless of policy; refused requests and started executions audited with initiator
    - _Requirements: 8.1, 8.3, 8.4, 8.5_

  - [ ] 5.5 Per-element content trust derivation
    - Submitter class derived from each authored element's own author for item bodies, item comments, and review artifact comments; never inherited from the item, artifact, or another element; undeterminable author yields the least-trusted class
    - A changed element re-derives its class and re-applies every gated decision before the new content is used; intake screening applies per element by its own class; class, author, and content revision recorded for every gated decision; trust configuration is config-only with no tool able to modify it
    - _Requirements: 37.1, 37.2, 37.3, 37.4, 37.5, 37.6_

- [ ] 6. Budget enforcement and kill switch
  - [ ] 6.1 Run stamping, ledger attribution, and ceilings
    - Stamp run identifier on every session; sum per-turn metering ledger records across all run sessions (authoring, orchestrator, subagents)
    - Halt dispatch after in-flight turns at the ceiling with amount in the notification; per-run ceiling independent of source caps; bundled default ceiling so headless runs never execute unbounded; optional warning threshold notifies without halting
    - _Requirements: 16.1, 16.2, 16.3, 16.7, 16.8, 24.2_
  - [ ] 6.2 Source spending caps and kill switch
    - Per-source spending caps stop new dispatches within the configured period; single kill-switch action pauses all watchers and halts autonomous runs after in-flight turns
    - Completion and halt notifications carry total credit consumption and land in the audit log
    - _Requirements: 16.4, 16.5, 16.6_

- [ ] 7. Delivery pipeline
  - [ ] 7.1 Stage executor with argv substitution
    - Read Delivery_Workflow stage-to-commands config; unconfigured stages skip; tokenize templates once and substitute variables as single literal argv elements via subprocess (no shell interpretation)
    - Run context plus custom project variables; valueless referenced variable fails the stage before execution; zero-config projects run authoring/execution in the working tree with autonomy capped at execution
    - _Requirements: 13.1, 13.2, 13.3, 13.4, 13.5, 13.6, 24.1_
  - [ ] 7.2 Stage flow, verify loop, and integration floor
    - Isolate before execution for delivery-authorized runs; verify failure dispatches fix tasks up to the retry limit; publish only after all verify stages pass
    - Publish output captured with deployment addresses surfaced; protected branch set from config defaulting to the base branch; integration requires human action unless autonomous integration explicitly enabled; no-verify auto-integration warns at config time
    - _Requirements: 13.7, 13.8, 13.9, 13.11, 13.17, 13.18, 13.20, 13.21_
  - [ ] 7.3 Worktree isolation for concurrent runs
    - Git preset isolate creates a dedicated worktree on a new branch from the refreshed base; no two active runs share a working tree
    - _Requirements: 13.15, 13.16_
  - [ ] 7.4 Teardown and workspace stewardship
    - Ledger records every workspace and per-run deployment; terminal state removes disposable materializations while preserving all branches and commits
    - Archive triggers teardown commands and ledger cleanup
    - _Requirements: 13.19, 20.1, 20.2, 20.3, 20.4_
  - [ ] 7.5 Interactive delivery and completion notifications
    - Explicit user action starts the same pipeline with identical stages, variables, and rules; completion or failure notifies with every executed stage's outcome
    - _Requirements: 13.22, 13.13_

  - [ ] 7.6 Quality gates and stage ordering
    - Workflow-declared stage order with verify-class gates runnable before submit, after it, or both; each gate carries severity (blocking stops the flow and dispatches fix tasks, advisory records and surfaces without stopping)
    - Run context substitution including base branch so gates can compare against base; gate name, severity, exit status and captured output audited and displayed on the run; bundled presets for tests, coverage thresholds, lint and type checks; no gates configured is recorded, not an error
    - _Requirements: 13.23, 29.1, 29.2, 29.3, 29.4, 29.5, 29.6, 29.7, 29.8_

  - [ ] 7.7 Phase-scoped prerequisite checks and safe failure
    - Read-only, zero-token preflight per project, each check scoped to the phase requiring it: that phase's command programs resolve, the providers it binds reach, base branch exists, protected set valid, notification channel resolves, budget ceiling present per enabled level above authoring
    - Run gate evaluates every phase the run's autonomy level will reach, including phases executing later in the run, and refuses before the first credit is spent
    - Watch-source checks cover the programs needed to poll at all; an unavailable program reports unmet and unhealthy, never "no items"
    - Property test: for a config whose delivery-phase program is absent, no run starts and no model credits are consumed
    - _Requirements: 32.1, 32.3, 32.4, 32.5, 32.6_

- [ ] 8. Watchers and dispatch
  - [ ] 8.1 Command-based watch sources and zero-token polling
    - Sources defined as poll command plus field mapping (identifier, title, body, state, address, classification, submitter); disabled by default with per-source enablement
    - Poll tick runs from a script cron with zero model invocations while idle
    - _Requirements: 10.2, 10.6, 17.3_
  - [ ] 8.2 Poll diffing, lifecycle generations, and atomic claims
    - Diff successive poll snapshots to derive new items and transitions (reopened, cancelled); claims keyed on (item identifier, lifecycle generation) with a SQLite unique constraint for exactly-once dispatch
    - _Requirements: 10.3, 10.9, 21.1_
  - [ ] 8.3 Dispatcher routing, caps, and run seeding
    - Route via source config: target project, base branch, classification-to-spec-type and autonomy per submitter class; submitter class from maintainer list or author-association, least-trusted when undetermined
    - Global and per-project caps with arrival-order queueing; unmapped classification without a default is recorded, not dispatched; no target project refuses dispatch; item content passed as quoted data; intake guidance injected separately; runs seeded in the project working tree so native steering applies
    - _Requirements: 10.1, 10.4, 10.5, 10.7, 10.12, 19.1, 19.2, 19.3, 19.4, 19.5, 19.6, 19.7_
  - [ ] 8.4 Item feedback and lifecycle cascade
    - Configured feedback commands post dispatch and completion updates to the item; mid-flight cancellation cancels the run, archives the spec, and audits the cascade; mid-run item edits ignored and audited
    - _Requirements: 10.10, 21.2, 21.3_
  - [ ] 8.5 Public-source autonomy warning
    - Enabling execution-or-higher autonomy on a publicly submittable source warns and records the acknowledgment
    - _Requirements: 10.11_
  - [ ] 8.6 Intake injection screening
    - Screen dispatched items with bundled guidance plus configured intake guidance on the review role's model; enabled by default per submitter class with explicit opt-out
    - Suspected injection quarantines at authoring level regardless of policy, flags findings in the Review_Queue, and notifies; reviewer release is the human gate; verdicts audited and cost attributed to the run
    - _Requirements: 25.1, 25.2, 25.3, 25.4, 25.5, 25.6, 25.7_

  - [ ] 8.7 Tracker housekeeping writeback
    - Named lifecycle events (claimed, awaiting review, delivery submitted, completed, failed or needs-human, refused) mapped to configured commands under the delivery stage-command rules; comment, label, state, assign, and link-artifact operations; bundled presets for the public hosts, per-event override for an organization's tracker, no non-public preset
    - Disabled by default, enabled per event per source through configuration only with no tool able to enable it; at-most-once per run per event recorded in the ledger so a repeated poll, retry, or resumed run does not repeat a delivered writeback
    - Failure recorded and surfaced without failing the run; content composed only from declared templates and engine values, never model-composed text or verbatim item body; zero model credits
    - _Requirements: 36.1, 36.2, 36.3, 36.4, 36.5, 36.6, 36.7, 36.8, 10.10, 37.1_

- [ ] 9. Orchestrator
  - [ ] 9.1 Wave loop and task persistence
    - Dispatch leaf tasks wave by wave with in-wave parallelism up to the concurrency cap; persist task status after every state change for resume
    - _Requirements: 9.1, 9.5_
  - [ ] 9.2 Role resolution and cost profiles
    - Determine each work unit's role and resolve agent, model, and effort from the selected Cost_Profile; session default agent/model fallback with a report when unset
    - Config-time verification that an assigned agent's tool surface includes the engine tools; subagents inherit the run's role assignments
    - _Requirements: 9.2, 15.1, 15.2, 15.3, 15.5, 15.6_
  - [ ] 9.3 Review verdicts and retry policy
    - Successful implementations get a review verdict on the review role's model; no completion without approval; any unsuccessful completion (implementation, review rejection, infrastructure) retries to the limit then fails without abandoning independent tasks
    - _Requirements: 9.3, 9.4_

  - [ ] 9.4 Test quality criteria in the review gate
    - Review verdicts judge tests explicitly: assertions derive from the code under test rather than test-constructed values, the test fails when the covered behavior is wrong, error and boundary cases covered; failing the criteria yields changes-required
    - Optional mutation-probe gate: a suite that still passes under mutation is a gate failure; test quality findings recorded in the audit log
    - _Requirements: 30.1, 30.2, 30.3, 30.4_

- [ ] 10. Engine MCP server
  - [ ] 10.1 Tool surface and JSON-RPC conformance
    - Expose engine operations as tools with prompt-as-tool-result authoring/orchestration guidance; a stock agent with only this server completes the workflow, without excluding agents holding other spec tools
    - Conformant prompts/resources/unknown-method handling; unavailable guidance returns an error, never partial text; no tool mutates the Autonomy_Policy or Delivery_Workflow
    - _Requirements: 3.1, 3.2, 3.3, 3.5, 13.12_
  - [ ] 10.2 MCP-library state equivalence tests
    - Drive the server over stdio with the kiro-cli init sequence; assert identical resulting state for every state operation invoked via MCP and via the library
    - _Requirements: 3.4_

- [ ] 11. Feedback loops
  - [ ] 11.1 Spec review revision cycle
    - Request-changes records comments, returns the run to authoring, and dispatches a revision turn with comments as quoted data; revisions validate under original rules and re-enter the queue; per-gate cycle limit marks needs-human
    - _Requirements: 22.2, 22.3, 22.4_
  - [ ] 11.2 Delivery review feedback watcher
    - Per-project opt-in (default off) polling of the review artifact via configured commands, zero credits while idle; new comments dispatch fix tasks through the same delivery stages, bounded by retry limit and budget ceiling with needs-human on the bound
    - Comment-driven dispatch gated on the commenter's own submitter class; a class not permitted to drive dispatch is quarantined in the Review_Queue for human release, consuming no credits; dispatching comments screened for embedded instructions on watched-item terms
    - _Requirements: 23.1, 23.2, 23.3, 23.4, 23.5, 23.6, 23.7, 23.8_

- [ ] 12. UI driver (absorb Spec Builder)
  - [ ] 12.1 Backend collapse onto the engine
    - Replace `_seed_prompt`/`_TYPE_PLAN`/`_derive_phase` backend logic with engine library calls; UI state, validation, and transitions come only from the engine; failed refresh retains last known state with a staleness indicator
    - _Requirements: 12.1, 12.2_
  - [ ] 12.2 Review queue surface and gate actions
    - Review_Queue grouped by run state with approve and request-changes actions; quarantine release, manual re-dispatch override, and manual workspace cleanup actions
    - _Requirements: 12.3, 18.5, 22.1, 21.4, 20.5_
  - [ ] 12.3 Configuration surface, cost display, and kill switch
    - Config UI for autonomy, workflow commands, watch sources, role assignments, notification channels; every setting shows effective value and origin; per-run credit consumption on queue entries and detail views; kill-switch control
    - _Requirements: 12.4, 12.5, 24.6_
  - [ ] 12.5 Preset origin display
    - Per-stage display of whether commands come from the selected preset or a project override
    - _Requirements: 33.6_

  - [ ] 12.4 Replace the Spec Builder builtin
    - New app replaces spec-builder as the single spec surface; specs created by the prior app remain valid artifacts
    - _Requirements: 12.6_

- [ ] 13. App packaging and providers
  - [ ] 13.1 Manifest, discovery skill, and registration
    - app.json declaring the MCP server and skill; trigger phrases for natural spec requests; skill directs agents to obtain instructions from the tools before any spec operation
    - _Requirements: 4.1, 4.2, 4.3_
  - [ ] 13.2 Bundled presets
    - GitHub/GitLab watch source presets, git-with-PR and local-only workflow presets, quality-first and budget cost profiles, bundled screening guidance
    - _Requirements: 10.8, 13.10, 15.4_
  - [ ] 13.4 Preset library and organization overrides
    - Named bundled Workflow_Presets (git + pull request, git + merge request, local-only) and matching public watch sources, treated as read-only; project selects a preset and overrides individual stage commands; user-defined named presets selectable identically; no preset for a non-public review or tracking system ships
    - _Requirements: 33.1, 33.2, 33.3, 33.4, 33.5_

  - [ ] 13.3 Provider_Interface and public build posture
    - Pluggable requirements analysis, model catalog, review policy, and watch sources with bundled local defaults; enhanced providers surface degraded status without changing the tool surface
    - No internal dependencies in the default build; all spec processing local; telemetry off by default and content-free when enabled
    - _Requirements: 11.1, 11.2, 11.3, 11.4, 11.5_

- [ ] 14. Setup assistant
  - [ ] 14.1 Agent-assisted setup flow
    - Inspect KiroCrew memory, steering files, docs, and CI/build configs to infer workflow, watch sources, and tooling; present each inference with evidence; ask conversationally for what cannot be inferred; operate from project files alone when memory is absent
    - Write through the validated config path on approval; ask (never infer) the Cost_Profile; per-level confirmation for execution, delivery, and integration; offer applicable Workflow_Presets and run the prerequisite checks, reporting each unmet check with its resolving action
    - _Requirements: 14.1, 14.2, 14.3, 14.4, 14.5, 14.6, 14.7, 15.7_

- [ ] 15. Headless run driver and notifications
  - [ ] 15.1 Session seeder
    - Seed ordinary agent sessions with the granted approval posture applied and the run identifier stamped; posture recorded in the audit log; runs appear in the dashboard session list; human-reserved gates notify that the run waits for review
    - _Requirements: 7.3, 6.3_
  - [ ] 15.2 Notification routing
    - Deliver through the host gateway's channels with selection from project config; default to the gateway dashboard channel when unconfigured
    - _Requirements: 6.5, 24.4_

- [ ] 16. Verification suites
  - [ ] 16.1 Property-based test suite
    - Hypothesis tests for the seven design properties: wave ordering, ladder monotonicity, claim exactly-once, substitution safety under adversarial values, budget attribution completeness, phase gate soundness, spec directory purity
    - _Requirements: 1.5, 8.7, 10.3, 13.6, 16.2, 2.5, 1.7, 17.4_
  - [ ] 16.2 End-to-end integration suite
    - Fixture git repository with a local bare remote exercising isolate through teardown offline; seeded fake metering ledger exercising budget halt; deterministic stages verified to make zero model invocations
    - _Requirements: 13.16, 16.3, 6.4, 17.1, 17.2_

- [ ] 17. Analysis
  - [ ] 17.1 Capability provider registry, schemas, and transports
    - Resolve every Delegable_Capability from config to builtin, mcp, or command transport behind one invocation path; identical tool surface regardless; builtin provider shipped for each; Engine_Floor capabilities refuse any binding
    - Per-capability versioned request/response schemas; schema-validated responses; declared coverage surfaced; cost attributed to the run budget; provider output treated as untrusted data; provider identity, transport, coverage and degraded status audited and displayed
    - Unavailable, timed-out or schema-invalid provider falls back to builtin with a degraded marker and reason, never blocking the run; supplementary validation providers may only add findings, never suppress or downgrade engine findings or gates
    - _Requirements: 26.1, 26.2, 26.3, 26.4, 26.5, 26.6, 26.7, 26.8, 26.9, 26.10, 26.11, 26.12, 26.13, 26.14, 24.7, 11.2_
  - [ ] 17.4 Analysis capability wiring
    - Publish versioned request and Analysis_Findings JSON Schemas; resolve the Analysis_Provider from config (local in-process, or an MCP stdio child from configured command/env/timeout) behind one engine call with an identical tool surface either way
    - Validate every response against the schema; key findings to acceptance criteria; surface declared skipped coverage; attribute declared cost to the run's budget; treat finding text as untrusted data
    - Unavailable, timed-out, or schema-invalid provider falls back to the local analyzer with a degraded marker and reason, never blocking authoring; analyzer identity, coverage, and degraded status audited
    - Bind the analysis capability through the registry: request carries document location, spec type and format version; findings keyed to acceptance criteria route into the Review_Queue
    - _Requirements: 26.6, 26.7, 26.8_
  - [ ] 17.2 Bundled local analyzer
    - Deterministic structural checks: glossary terms used but undefined, unquantified qualifiers, criteria that are not independently testable, requirements with no covering task, overlapping or contradictory criteria within a requirement
    - Emit the shared Analysis_Findings schema with generated clarifying questions (choices, consequences, recommended answer); declare depth as structural; no network, zero model credits
    - _Requirements: 27.1, 27.2, 27.3, 27.4, 27.5_
  - [ ] 17.5 Builtin provider bindings
    - Register the engine's own paths as the builtin providers for authoring (seeded turn behind validation and the phase gate), review (seeded verdict turn with review and test-quality criteria), implementation (per-task subagent dispatch), and model catalog (host resolution)
    - Bind the bundled GitHub/GitLab watch presets, marking a source unhealthy with the missing program name when its command-line dependency is absent; ship no supplementary validation rules; UI identifies each capability's provider as builtin or external and each builtin as deterministic or model-backed
    - _Requirements: 31.1, 31.2, 31.3, 31.4, 31.5, 31.6, 31.7, 31.8_

  - [ ] 17.3 Conformance runner
    - Per-capability conformance runner over bundled fixtures (planted ambiguity, contradictory criteria, coverage hole, oversized document, malformed response) asserting schema validity, planted-defect detection, declared coverage, timeout honoring and repeatability; every builtin provider passes its own suite
    - _Requirements: 26.15, 27.6_

- [ ] 19. Doctor
  - [ ] 19.1 Finding vocabulary and aggregation
    - Stable Finding identifiers with severity, affected phase or surface, cause, and resolving action; aggregate phase prerequisites, source health, provider reachability and degradation, config validation, budget and kill-switch state, blocked runs, and skill/MCP registration reach
    - A check that cannot complete becomes a Finding and the remaining Findings still return; provider, command, and watched-item text carried as untrusted data, never executed; no operation modifies config, Autonomy_Policy, or Delivery_Workflow
    - Engine refusals, blocked dispatches, and degraded marks quote the same Finding identifier
    - Last known result recorded per identifier; a previously-passing check that now fails reports as a regression with when it last passed, notifies once, and stays quiet while unchanged; declared minimum program versions verified rather than presence alone
    - _Requirements: 34.1, 34.2, 34.4, 34.5, 34.6, 34.7, 34.8, 34.9, 34.10, 34.11_
  - [ ] 19.2 Doctor surfaces and surface equivalence
    - Doctor exposed as an Engine_MCP_Server tool and as the UI panel from the one engine operation; prerequisite Findings grouped by phase
    - Equivalence test: the tool and the UI path return identical Findings for the same state
    - _Requirements: 34.3, 32.2_

  - [ ] 17.6 Semantic builtin and the async analysis job shape
    - Model-backed analysis builtin dispatching an agent turn with an authored analysis prompt at the agent, model, and effort configured for the analysis role, no network service; submit/poll job shape shared by every transport; configured total wall-clock deadline failing the job with elapsed time and partial progress
    - Dispatched turn output schema-validated before recording, invalid output fails the job with nothing partial recorded; spend attributed to the run's budget and subject to the ceiling and kill switch; every result records depth and provider identity; one findings schema across depths and transports
    - Every capability answers from a working builtin: no absent, stubbed, or not-configured tool in the surface
    - _Requirements: 35.1, 35.2, 35.3, 35.4, 35.5, 35.6, 35.7, 35.8_

- [ ] 18. Clean-room provenance gate
  - [ ] 18.1 Provenance checks and audit
    - Repository check asserting no non-public endpoints, service names, headers, or credentials appear in the tree; shipped prompt text authored for this app; delegated providers referenced by configuration only
    - _Requirements: 28.1, 28.2, 28.3, 28.4, 28.5, 28.6_

## Task Dependency Graph

```json
{"waves": [
  {"id": 0, "tasks": ["1.1", "1.2", "2.1", "2.2", "3.1"]},
  {"id": 1, "tasks": ["3.2", "4.1", "4.2", "5.1", "7.1", "8.1", "17.1"]},
  {"id": 2, "tasks": ["4.3", "5.2", "6.1", "7.2", "7.3", "8.2", "9.2", "17.2", "17.4"]},
  {"id": 3, "tasks": ["5.3", "5.4", "5.5", "6.2", "7.4", "7.5", "7.6", "7.7", "8.3", "9.1", "15.2", "17.3"]},
  {"id": 4, "tasks": ["8.4", "8.5", "8.6", "8.7", "9.3", "9.4", "10.1", "11.1", "15.1", "17.5", "17.6"]},
  {"id": 5, "tasks": ["10.2", "11.2", "12.1", "13.1", "13.2", "13.3", "13.4", "19.1"]},
  {"id": 6, "tasks": ["12.2", "12.3", "12.5", "14.1", "16.1", "19.2"]},
  {"id": 7, "tasks": ["12.4", "16.2", "18.1"]}
]}
```

## Notes

- Task 1 (core enablers) ships as a separate prerequisite PR to the gateway; everything else lands in the app.
- Every leaf includes its own unit tests; build gates are the repo's standard pytest + isort + flake8 + mypy + tsc + vitest run in a worktree.
- Task 16.1 implements the seven property-based correctness properties from design.md with hypothesis.
- All tests run offline: stage commands under test are stubbed binaries; the e2e suite uses a fixture git repo with a local bare remote and a seeded fake metering ledger.
- The app's working name is spec-engine; the final published name requires explicit sign-off before packaging (names are one-way doors).
