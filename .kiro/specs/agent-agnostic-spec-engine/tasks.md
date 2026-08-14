# Implementation Plan: Agent-Agnostic Spec Engine

## Overview

Ships the spec-engine app in dependency order: gateway enablers and engine foundations first, then the validator, phase machine, autonomy and run lifecycle, budget, delivery, watchers, and orchestrator, then the MCP surface, feedback loops, UI absorption, packaging, the doctor, setup assistant, and the verification suites. 19 parent tasks, 61 leaves, 8 waves; wave membership follows real code dependencies (state store and config before everything stateful; engine modules before the MCP wrapper and drivers; UI and packaging after the surfaces they expose).

## Tasks

- [x] 1. Core gateway enablers (prerequisite PR to kiro_crew)
  - [x] 1.1 Extend approval grants to app-seeded sessions
    - Generalize the cron `approval_mode` grant shape so an app manifest declares a wanted posture, configuration grants it, and the gateway applies it to sessions the app seeds
    - Refuse or halt a run whose applied posture does not match the grant; no runtime path can elevate a session's own posture
    - _Requirements: 7.1, 7.2, 7.4_
  - [x] 1.2 Builtin MCP vending regression test
    - Regression test proving a builtin-declared MCP server and skill reach a Host_Agent session's tool surface; fix the stale contrary comment in the Spec Builder backend
    - Registration failure completes install but reports a not-ready state with the reason
    - _Requirements: 4.3, 4.4_

- [x] 2. Engine foundations
  - [x] 2.1 Config module with bundled defaults and effective-value resolution
    - Schema for app/project/source config; bundled default values for every numeric limit; absent optional settings resolve to defaults without failing
    - Effective-value API returning value plus origin (bundled default vs explicit); single validated write path for all config surfaces
    - _Requirements: 24.3, 24.5_
  - [x] 2.2 SQLite state store, per-spec locking, and audit log
    - Tables: specs, approvals, runs, claims, workspaces, queue; per-spec lock rows rejecting conflicting concurrent writers with current state
    - Engine state lives entirely outside spec directories; persistence failure fails the operation and never writes state into a spec document; append-only per-spec audit JSONL
    - _Requirements: 2.6, 1.6, 1.7_

- [x] 3. Validator
  - [x] 3.1 Native-format document validator
    - Implement the native Kiro format rules (sections, EARS shape, numbering, checkbox syntax) returning every violation with file, location, and rule identifier
    - _Requirements: 1.1, 1.2_
  - [x] 3.2 Task links, requirement coverage, and DAG validation
    - Resolve every leaf-task criterion reference against requirements.md; report uncovered requirements in tasks-only and full-spec validation
    - Waves DAG checks: acyclic, every incomplete leaf in exactly one wave, sequential wave ids
    - _Requirements: 1.3, 1.4, 1.5_

- [x] 4. Spec lifecycle and phase machine
  - [x] 4.1 Spec creation and spec types
    - Create spec directories with `.config.kiro` recording the spec type; derive the document plan (feature/bugfix/quick) from the recorded type
    - Unrecordable type fails creation atomically, leaving no partial directory; no validation or advancement without a recorded type
    - _Requirements: 5.1, 5.2, 5.3, 5.4_
  - [x] 4.2 Phase derivation, advancement gates, and approval staleness
    - Read-only phase derivation from artifacts plus approval state; advancement refused on validation failure or missing approval with reasons returned
    - Approvals persist approver identity and timestamp; post-approval document edits (content hash) stale exactly the affected approvals
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5_
  - [x] 4.3 Interactive and policy approval recording
    - Interactive runs record approvals only from explicit user action; headless runs record gate approvals from the Autonomy_Policy for covered gates and require humans for uncovered gates
    - Identical validation rules for artifacts from either mode
    - _Requirements: 6.1, 6.2, 6.4_

- [x] 5. Autonomy policy and run lifecycle
  - [x] 5.1 Autonomy_Policy resolution
    - Resolve per (source, spec type, submitter class); unconfigured resolves to authoring-only human-reserved execution; configured resolves exactly to the configured level
    - Strictly ordered levels with lower levels implied; loaded from configuration only
    - _Requirements: 8.2, 8.6, 8.7_
  - [x] 5.2 Run state machine, timeouts, and resume
    - States: queued, authoring, awaiting review, executing, delivering, done, failed, halted for budget, cancelled, stalled; per-phase timeouts mark stalled and notify
    - Resume from persisted state at task granularity in execution and phase granularity in authoring
    - _Requirements: 18.1, 18.2, 18.3_
  - [x] 5.3 Review_Queue and archival rules
    - Engine-exposed queue of runs at human-reserved gates, renderable by any driver; no time-based archival or expiry
    - Archive only on explicit user action or triggering-item cancellation; archived stays archived until explicitly unarchived; reversible
    - _Requirements: 18.4, 18.6, 18.7_
  - [x] 5.4 Execution gate
    - Human-reserved execution starts only from explicit human action; authorized autonomy starts on gate satisfaction with no further trigger
    - Failed validation or missing approvals refuse regardless of policy; refused requests and started executions audited with initiator
    - _Requirements: 8.1, 8.3, 8.4, 8.5_

  - [x] 5.5 Per-element content trust derivation
    - Submitter class derived from each authored element's own author for item bodies, item comments, and review artifact comments; never inherited from the item, artifact, or another element; undeterminable author yields the least-trusted class
    - A changed element re-derives its class and re-applies every gated decision before the new content is used; intake screening applies per element by its own class; class, author, and content revision recorded for every gated decision; trust configuration is config-only with no tool able to modify it
    - _Requirements: 37.1, 37.2, 37.3, 37.4, 37.5, 37.6_

- [x] 6. Budget enforcement and kill switch
  - [x] 6.1 Run stamping, ledger attribution, and ceilings
    - Stamp run identifier on every session; sum per-turn metering ledger records across all run sessions (authoring, orchestrator, subagents)
    - Halt dispatch after in-flight turns at the ceiling with amount in the notification; per-run ceiling independent of source caps; bundled default ceiling so headless runs never execute unbounded; optional warning threshold notifies without halting
    - _Requirements: 16.1, 16.2, 16.3, 16.7, 16.8, 24.2_
  - [x] 6.2 Source spending caps and kill switch
    - Per-source spending caps stop new dispatches within the configured period; single kill-switch action pauses all watchers and halts autonomous runs after in-flight turns
    - Completion and halt notifications carry total credit consumption and land in the audit log
    - _Requirements: 16.4, 16.5, 16.6_

- [x] 7. Delivery pipeline
  - [x] 7.1 Stage executor with argv substitution
    - Read Delivery_Workflow stage-to-commands config; unconfigured stages skip; tokenize templates once and substitute variables as single literal argv elements via subprocess (no shell interpretation)
    - Run context plus custom project variables; valueless referenced variable fails the stage before execution; zero-config projects run authoring/execution in the working tree with autonomy capped at execution
    - _Requirements: 13.1, 13.2, 13.3, 13.4, 13.5, 13.6, 24.1_
  - [x] 7.2 Stage flow, verify loop, and integration floor
    - Isolate before execution for delivery-authorized runs; verify failure dispatches fix tasks up to the retry limit; publish only after all verify stages pass
    - Publish output captured with deployment addresses surfaced; protected branch set from config defaulting to the base branch; integration requires human action unless autonomous integration explicitly enabled; no-verify auto-integration warns at config time
    - _Requirements: 13.7, 13.8, 13.9, 13.11, 13.17, 13.18, 13.20, 13.21_
  - [x] 7.3 Worktree isolation for concurrent runs
    - Git preset isolate creates a dedicated worktree on a new branch from the refreshed base; no two active runs share a working tree
    - _Requirements: 13.15, 13.16_
  - [x] 7.4 Teardown and workspace stewardship
    - Ledger records every workspace and per-run deployment; terminal state removes disposable materializations while preserving all branches and commits
    - Archive triggers teardown commands and ledger cleanup
    - _Requirements: 13.19, 20.1, 20.2, 20.3, 20.4_
  - [x] 7.5 Interactive delivery and completion notifications
    - Explicit user action starts the same pipeline with identical stages, variables, and rules; completion or failure notifies with every executed stage's outcome
    - _Requirements: 13.22, 13.13_

  - [x] 7.6 Quality gates and stage ordering
    - Workflow-declared stage order with verify-class gates runnable before submit, after it, or both; each gate carries severity (blocking stops the flow and dispatches fix tasks, advisory records and surfaces without stopping)
    - Run context substitution including base branch so gates can compare against base; gate name, severity, exit status and captured output audited and displayed on the run; bundled presets for tests, coverage thresholds, lint and type checks; no gates configured is recorded, not an error
    - _Requirements: 13.23, 29.1, 29.2, 29.3, 29.4, 29.5, 29.6, 29.7, 29.8_

  - [x] 7.7 Phase-scoped prerequisite checks and safe failure
    - Read-only, zero-token preflight per project, each check scoped to the phase requiring it: that phase's command programs resolve, the providers it binds reach, base branch exists, protected set valid, notification channel resolves, budget ceiling present per enabled level above authoring
    - Run gate evaluates every phase the run's autonomy level will reach, including phases executing later in the run, and refuses before the first credit is spent
    - Watch-source checks cover the programs needed to poll at all; an unavailable program reports unmet and unhealthy, never "no items"
    - Property test: for a config whose delivery-phase program is absent, no run starts and no model credits are consumed
    - _Requirements: 32.1, 32.3, 32.4, 32.5, 32.6_

- [x] 8. Watchers and dispatch
  - [x] 8.1 Command-based watch sources and zero-token polling
    - Sources defined as poll command plus field mapping (identifier, title, body, state, address, classification, submitter); disabled by default with per-source enablement
    - Poll tick runs from a script cron with zero model invocations while idle
    - _Requirements: 10.2, 10.6, 17.3_
  - [x] 8.2 Poll diffing, lifecycle generations, and atomic claims
    - Diff successive poll snapshots to derive new items and transitions (reopened, cancelled); claims keyed on (item identifier, lifecycle generation) with a SQLite unique constraint for exactly-once dispatch
    - _Requirements: 10.3, 10.9, 21.1_
  - [x] 8.3 Dispatcher routing, caps, and run seeding
    - Route via source config: target project, base branch, classification-to-spec-type and autonomy per submitter class; submitter class from maintainer list or author-association, least-trusted when undetermined
    - Global and per-project caps with arrival-order queueing; unmapped classification without a default is recorded, not dispatched; no target project refuses dispatch; item content passed as quoted data; intake guidance injected separately; runs seeded in the project working tree so native steering applies
    - Consume what the tick and the lifecycle diff already produce. Tasks 8.1 and 8.2 built polling and the claim-keyed diff but no production caller, so a tick's items currently go nowhere; this is the consumer, and the "no target project refuses dispatch" property named above belongs here rather than to the poll
    - Construct the per-source spend gate 6.2 added and pass it in: a dispatch path without it is uncapped, because the parameter currently defaults to no enforcement. Budget enforcement is Engine_Floor and never delegable, so make the gate a required argument as part of wiring it — a seam that defaults to off delegates the ceiling to whoever writes the caller, and the test certifying that the ungated path "behaves as it did" retires with it
    - _Requirements: 10.1, 10.4, 10.5, 10.7, 10.12, 19.1, 19.2, 19.3, 19.4, 19.5, 19.6, 19.7_
  - [x] 8.4 Item feedback and lifecycle cascade
    - Configured feedback commands post dispatch and completion updates to the item; mid-flight cancellation cancels the run, archives the spec, and audits the cascade; mid-run item edits ignored and audited
    - Give the manual re-dispatch override a way to re-offer a refused item. Releasing the dispatch claim is done (both refusals that kept one now release it and record a ledger row under their own kind, on the poll path and the queue path), but investigating it disproved half the original finding: the claim was never the only thing suppressing the item. The snapshot row is, because a still-open item already in `watch_items` derives `unchanged`, which is not a dispatch candidate. So fixing the configuration does NOT re-offer the waiting items, and `release_dispatch_claim` alone cannot satisfy requirement 21.4 — the override also has to make the item a candidate, which needs a primitive that forgets the snapshot row. Nothing in the state store does that today. Keep the suppression by default: re-offering every unchanged item each poll would spend on work nobody asked to redo
    - Wire `watch/feedback.py:post_feedback` at the lifecycle points, which is what makes requirement 10.10 true of a running engine rather than of a library: `claimed` from the dispatcher beside the claim, `awaiting_review` / `completed` / `failed` / `refused` from the run lifecycle transitions, `delivery_submitted` from the delivery flow after submit. The mechanism, its at-most-once ledger claim and its recorded-not-fatal failure are built and tested; nothing constructs it. Do not check this task off while that holds
    - Give the `writeback` claim kind an operator surface. A failed post deliberately keeps its claim, because retrying a command that may already have commented is how one event becomes two — but that permanently suppresses the event for the run, and the only release path today is hand-written Python against the store. Add a `release_writeback_claim` twin of `release_dispatch_claim`, and say in the FAILED report and audit detail that the event is now suppressed and which ledger row clears it
    - _Requirements: 10.10, 21.2, 21.3, 21.4_
  - [x] 8.5 Public-source autonomy warning
    - Enabling execution-or-higher autonomy on a publicly submittable source warns and records the acknowledgment
    - _Requirements: 10.11_
  - [x] 8.6 Intake injection screening
    - Screen dispatched items with bundled guidance plus configured intake guidance on the review role's model; enabled by default per submitter class with explicit opt-out
    - Suspected injection quarantines at authoring level regardless of policy, flags findings in the Review_Queue, and notifies; reviewer release is the human gate; verdicts audited and cost attributed to the run
    - Screen each `ContentElement` under its own class from `engine/trust.py`, never the item's. Task 5.5 built the per-element derivation and the consumption gate but does not own screening, so this is where requirement 37.4 is satisfied: screening only the item at intake would classify a stranger's comment by the class of whoever opened the issue, which is the escalation 5.5 exists to prevent. Reach element text through `trust.consume` so an element edited after it was screened cannot be used under the old verdict
    - _Requirements: 25.1, 25.2, 25.3, 25.4, 25.5, 25.6, 25.7, 37.4_

  - [x] 8.7 Tracker housekeeping writeback
    - Named lifecycle events (claimed, awaiting review, delivery submitted, completed, failed or needs-human, refused) mapped to configured commands under the delivery stage-command rules; comment, label, state, assign, and link-artifact operations; bundled presets for the public hosts, per-event override for an organization's tracker, no non-public preset
    - Disabled by default, enabled per event per source through configuration only with no tool able to enable it; at-most-once per run per event recorded in the ledger so a repeated poll, retry, or resumed run does not repeat a delivered writeback
    - Failure recorded and surfaced without failing the run; content composed only from declared templates and engine values, never model-composed text or verbatim item body; zero model credits
    - Extend `engine/watch/feedback.py` rather than adding a second writeback path. Task 8.4 built the one that posts a configured event's argv through the delivery executor, claims `writeback` per run per event before spawning, records every outcome including unconfigured, and treats a failure as recorded-not-fatal — all four of which this task also requires. A second path would be a second answer to what has already been said to an item, and the two ledgers would disagree the first time a run resumed
    - Implement requirement 36.7's echo gate, which nothing does today. Text from a Content_Element may be echoed only where that element's submitter class is configured as permitted, and never for the least-trusted class. Task 5.5's `engine/trust.py` derives a class; it does not gate an echo, so this must not be assumed handled by it. The gap is latent only because nothing in production populates the run-context fields that carry tracker text
    - Cover `sources.*.feedback` in the prerequisite check. It is a third place in the document holding argv the engine executes, alongside workflow stages and quality gates, and task 7.7 checks the other two plus each source's `poll` program. Without it a missing `gh` is discovered as a failed writeback that permanently holds its claim, rather than as an unmet prerequisite before the run starts
    - _Requirements: 36.1, 36.2, 36.3, 36.4, 36.5, 36.6, 36.7, 36.8, 10.10, 37.1_

- [x] 9. Orchestrator
  - [x] 9.1 Wave loop and task persistence
    - Dispatch leaf tasks wave by wave with in-wave parallelism up to the concurrency cap; persist task status after every state change for resume
    - Wire the pieces earlier waves built as libraries but left unconstructed outside their tests: pass a workspace broker into the delivery pipeline so the shared-working-tree refusal actually runs, and resolve each dispatch's role through the role resolver. Reviews of tasks 7.3 and 9.2 found both inert in production; a library nothing constructs is an enforcement that never fires
    - Also call the completion reporter 6.2 added. It attributes a run's total consumption to the notification and the audit record, and run completion lives here rather than in the budget module, so nothing invokes it yet
    - Hold one spec lock across a batch of task-status writes rather than one per write. The store refuses a conflicting writer instead of waiting, so two tasks reporting at once get one refusal, and a status that is refused and then dropped makes a resumed run pay again for finished work
    - _Requirements: 9.1, 9.5_
  - [x] 9.2 Role resolution and cost profiles
    - Determine each work unit's role and resolve agent, model, and effort from the selected Cost_Profile; session default agent/model fallback with a report when unset
    - Config-time verification that an assigned agent's tool surface includes the engine tools; subagents inherit the run's role assignments
    - _Requirements: 9.2, 15.1, 15.2, 15.3, 15.5, 15.6_
  - [x] 9.3 Review verdicts and retry policy
    - Successful implementations get a review verdict on the review role's model; no completion without approval; any unsuccessful completion (implementation, review rejection, infrastructure) retries to the limit then fails without abandoning independent tasks
    - Call the two seams task 7.4 built and could not reach from its own files. `WaveRunner.finish` is where a run becomes terminal, so it is where the workspace janitor's `retire_run` belongs — without it every completed run leaves its checkout on disk for good. And `DeliveryRun.deployment_addresses` is produced but `record_deployment` is never called, so the ledger has no row for a live environment the engine created. Both are one call each; both are inert today
    - Give the delivery pipeline a notifier, which is NOT the one-argument wiring it looks like: the engine has two `Notifier` protocols and they are not interchangeable. The budget one takes `notify(channel=..., message=..., detail=...)`, while the delivery one takes `send(...)` and deliberately holds no channel because configuration resolves the destination inside the notifier. Passing the orchestrator's budget notifier to the pipeline is a type error. Converge the two protocols on the delivery shape, or resolve a delivery notifier separately in the factory — until then an autonomous delivery records that it had no notifier and tells nobody its outcome
    - _Requirements: 9.3, 9.4_

  - [x] 9.4 Test quality criteria in the review gate
    - Review verdicts judge tests explicitly: assertions derive from the code under test rather than test-constructed values, the test fails when the covered behavior is wrong, error and boundary cases covered; failing the criteria yields changes-required
    - Mandatory mutation probe, executed not judged: for each behaviour the task claims to cover, the gate neuters the mechanism in the engine tree, runs the covering tests, and requires a failure. A behaviour whose tests still pass under mutation is a gate failure, not a comment. Reading a test to decide whether it is adequate is not sufficient evidence and does not satisfy this gate
    - The probe reports which test failed, so a mutation caught only by an unrelated test is distinguished from one caught by the test that claims the behaviour
    - A behaviour covered by a repo-wide static guard rather than a unit test satisfies the gate when the guard fails under mutation; the gate records which artefact caught it
    - Screen for the assertion shapes that pass regardless of the mechanism: a proxy the failure path also sets, a short-circuit reached before the property, only the direction a constant satisfies, a branch no test executes, a fake too broken to reach the branch under test, an assertion made vacuous by operator precedence or by a representation that escapes its own input, one sanitized field beside an unsanitized sibling, and a condition phrased in terms of the outputs a bug moves together
    - Ask of every guarantee what ELSE reaches the same effect, because a guarantee enforced at one spelling or one path is the shape that has produced the security defects here rather than the coverage gaps: a second spelling the engine itself emits, a second config path holding the same executable content, a second comparison of the same identity, a second delivery path that skips the one under test. The fence is not the property; the property is that nothing gets past it
    - Serialize the probe against the tree it mutates. A mutation is observed by running only the tests covering the neutered mechanism, never the whole suite, because a second prober's mutation makes a suite-wide result evidence about neither. Restore before doing anything else and confirm the file is byte-identical; never hold a mutation across a turn boundary, so an interrupted prober cannot leave a neutered mechanism behind. One prober per tree, or a tree per prober
    - Restore a mutation by reverting the edit, not with `git checkout --`. Three probers destroyed or nearly destroyed their own work this way: `git checkout` discards EVERY uncommitted change in that file rather than just the mutation, and on an untracked file it restores nothing at all. So commit the piece before probing it, and undo the neutering the same way it was applied. A prober that has to recover from a backup has already lost the evidence it was gathering
    - The probe is necessary and not sufficient, so it does not replace a reader with the whole tree in view. It proves a test CAN fail; it cannot show the test asked the right question. What it structurally cannot see: an engine output accepted back as engine input, a caller in a different spec or module, an obligation the requirements state that the mechanism does not mention, a second delivery path that bypasses the one under test, and a stub that differs from the existing stub in the way that matters. A verdict rests on both
    - Test quality findings recorded in the audit log
    - _Requirements: 30.1, 30.2, 30.3, 30.4_

- [x] 10. Engine MCP server
  - [x] 10.1 Tool surface and JSON-RPC conformance
    - Expose engine operations as tools with prompt-as-tool-result authoring/orchestration guidance; a stock agent with only this server completes the workflow, without excluding agents holding other spec tools
    - Conformant prompts/resources/unknown-method handling; unavailable guidance returns an error, never partial text; no tool mutates the Autonomy_Policy or Delivery_Workflow
    - _Requirements: 3.1, 3.2, 3.3, 3.5, 13.12_
  - [x] 10.2 MCP-library state equivalence tests
    - Drive the server over stdio with the kiro-cli init sequence; assert identical resulting state for every state operation invoked via MCP and via the library
    - _Requirements: 3.4_

- [x] 11. Feedback loops
  - [x] 11.1 Spec review revision cycle
    - Request-changes records comments, returns the run to authoring, and dispatches a revision turn with comments as quoted data; revisions validate under original rules and re-enter the queue; per-gate cycle limit marks needs-human
    - _Requirements: 22.2, 22.3, 22.4_
  - [x] 11.2 Delivery review feedback watcher
    - Per-project opt-in (default off) polling of the review artifact via configured commands, zero credits while idle; new comments dispatch fix tasks through the same delivery stages, bounded by retry limit and budget ceiling with needs-human on the bound
    - Comment-driven dispatch gated on the commenter's own submitter class; a class not permitted to drive dispatch is quarantined in the Review_Queue for human release, consuming no credits; dispatching comments screened for embedded instructions on watched-item terms
    - _Requirements: 23.1, 23.2, 23.3, 23.4, 23.5, 23.6, 23.7, 23.8_

- [ ] 12. UI driver (absorb Spec Builder)
  - [x] 12.1 Backend collapse onto the engine
    - Replace `_seed_prompt`/`_TYPE_PLAN`/`_derive_phase` backend logic with engine library calls; UI state, validation, and transitions come only from the engine; failed refresh retains last known state with a staleness indicator
    - _Requirements: 12.1, 12.2_
  - [ ] 12.2 Review queue surface and gate actions
    - Review_Queue grouped by run state with approve and request-changes actions; quarantine release, manual re-dispatch override, and manual workspace cleanup actions
    - Give the analysis report a durable sink, which is the last step of a chain three tasks built and none could finish. Task 17.4 produced criterion-keyed findings, 17.5 gave them a `FindingsSink` seam whose DEFAULT records in memory only, and neither owned the files that persist anything — so today a report is "recorded" and then dropped when the process ends. Needed: an `analysis_findings` table keyed on the run and a nullable criterion, whose columns are exactly `AnalysisReport.review_rows(run)`, with a writer that REPLACES a run's rows so a re-analysis supersedes rather than appends; then project those rows onto the run's queue entry grouped by criterion. Do NOT add a second queue — this is the same run projection the Review_Queue already is. The sink itself is then one line beside `budget/ledger.py`'s `RunCostSink`. Render finding prose through the display contract: it keeps the line breaks prose is entitled to, so a surface that trusts them can still be reflowed by a crafted message
    - Close the execution-gate capability gap task 12.1's review demonstrated, and the client-side transition map beside it. Today a spec holding only a `tasks.md` -- never validated, never approved -- executes through the app, while the engine's `phases.execution_blocking_reasons` would refuse it with an approval-missing reason at every gate, because the app records no approvals with the engine at all. Task 12.1 correctly left its own gate standing rather than routing through an authority that would have refused every execution the app currently allows, so this is the task that has to record approvals through `phases.approve` FIRST and then route the gate. The sibling half is `website/src/apps/spec-builder/components/SpecDetail.tsx`'s `ADVANCE` map, which computes the next transition client-side and sends an "approved -- proceed" prompt without consulting the engine: requirement 12.1's "transitions come only from the engine" is not satisfiable while it stands. Both are the same missing piece seen from two ends -- the app has no approval-recording path -- and until it exists the app offers advances and executions the engine would refuse
    - _Requirements: 12.3, 18.5, 22.1, 21.4, 20.5_
  - [ ] 12.3 Configuration surface, cost display, and kill switch
    - Config UI for autonomy, workflow commands, watch sources, role assignments, notification channels; every setting shows effective value and origin; per-run credit consumption on queue entries and detail views; kill-switch control
    - Make the screening opt-out persistable, and enforce its one rule at validation. Task 8.6 reads a per-class opt-out from `sources.<name>.screening.<class>`, but `SOURCE_FIELDS` does not list `screening`, so the validated write path REFUSES it and an operator cannot save one through any surface — the reader is currently the only enforcement of requirement 25.2's rule that no single setting disables screening for every class. Add the field with a validator that accepts only the four submitter-class keys mapping to booleans and rejects a default or wildcard key, so the no-disable-all rule is enforced where a value is written rather than only where it is read
    - _Requirements: 12.4, 12.5, 24.6_
  - [ ] 12.5 Preset origin display
    - Per-stage display of whether commands come from the selected preset or a project override
    - _Requirements: 33.6_

  - [ ] 12.4 Replace the Spec Builder builtin
    - New app replaces spec-builder as the single spec surface; specs created by the prior app remain valid artifacts
    - _Requirements: 12.6_

- [x] 13. App packaging and providers
  - [x] 13.1 Manifest, discovery skill, and registration
    - app.json declaring the MCP server and skill; trigger phrases for natural spec requests; skill directs agents to obtain instructions from the tools before any spec operation
    - Report a not-ready state when registration of either the discovery skill or the Engine_MCP_Server fails, naming the failure reason, and do not present the app as operational. Installation still completes. A half-registered app that claims to work is worse than one that admits it did not, because the first symptom a user meets is then a spec operation whose tools are missing, with nothing connecting that to the failed registration. Requirement 4.4 was claimed by NO task until this split found it unowned
    - The engine WIRING obligations that accumulated on this task are now section 20. Eleven of them landed here as wave-4 reviews found inert seams, and NONE was covered by this task's requirements -- Requirement 4 is discovery only -- so a reviewer validating 13.1 against its own criteria could have approved it having built just the manifest, with every wiring obligation passing unexamined. That is the same inert-library shape the obligations themselves were recorded to prevent
    - _Requirements: 4.1, 4.2, 4.3, 4.4_
  - [x] 13.2 Bundled presets
    - GitHub/GitLab watch source presets, git-with-PR and local-only workflow presets, quality-first and budget cost profiles, bundled screening guidance
    - The watch presets are a shape, not a new mechanism, and task 17.5 specified them rather than writing them because it did not own `engine/watch/sources.py`: a `WATCH_SOURCE_PRESETS` map keyed `github` and `gitlab`, each carrying a poll argv naming `gh` respectively `glab`, a field map from the engine's item fields to that tool's output paths, and the program name, plus an accessor returning deep copies ready to write into `sources.*`. Follow `delivery/flow.py`'s `QUALITY_GATE_PRESETS` / `gate_presets` — `WatchSource` already has a `preset` field and a `program` property, so no new field is needed. Do NOT add a second health path for the missing-program case: `poll.HealthReason.PROGRAM_UNAVAILABLE` and `prerequisites.check_source` already answer it, and a preset that reports its own unhealthiness would be a second answer to the same question
    - _Requirements: 10.8, 13.10, 15.4_
  - [x] 13.4 Preset library and organization overrides
    - Named bundled Workflow_Presets (git + pull request, git + merge request, local-only) and matching public watch sources, treated as read-only; project selects a preset and overrides individual stage commands; user-defined named presets selectable identically; no preset for a non-public review or tracking system ships
    - _Requirements: 33.1, 33.2, 33.3, 33.4, 33.5_

  - [x] 13.3 Provider_Interface and public build posture
    - Pluggable requirements analysis, model catalog, review policy, and watch sources with bundled local defaults; enhanced providers surface degraded status without changing the tool surface
    - No internal dependencies in the default build; all spec processing local; telemetry off by default and content-free when enabled
    - _Requirements: 11.1, 11.2, 11.3, 11.4, 11.5_

- [ ] 14. Setup assistant
  - [ ] 14.1 Agent-assisted setup flow
    - Inspect KiroCrew memory, steering files, docs, and CI/build configs to infer workflow, watch sources, and tooling; present each inference with evidence; ask conversationally for what cannot be inferred; operate from project files alone when memory is absent
    - Write through the validated config path on approval; ask (never infer) the Cost_Profile; per-level confirmation for execution, delivery, and integration; offer applicable Workflow_Presets and run the prerequisite checks, reporting each unmet check with its resolving action
    - _Requirements: 14.1, 14.2, 14.3, 14.4, 14.5, 14.6, 14.7, 15.7_

- [x] 15. Headless run driver and notifications
  - [x] 15.1 Session seeder
    - Seed ordinary agent sessions with the granted approval posture applied and the run identifier stamped; posture recorded in the audit log; runs appear in the dashboard session list; human-reserved gates notify that the run waits for review
    - _Requirements: 7.3, 6.3_
  - [x] 15.2 Notification routing
    - Deliver through the host gateway's channels with selection from project config; default to the gateway dashboard channel when unconfigured
    - _Requirements: 6.5, 24.4_

- [ ] 16. Verification suites
  - [ ] 16.1 Property-based test suite
    - Hypothesis tests for the seven design properties: wave ordering, ladder monotonicity, claim exactly-once, substitution safety under adversarial values, budget attribution completeness, phase gate soundness, spec directory purity
    - _Requirements: 1.5, 8.7, 10.3, 13.6, 16.2, 2.5, 1.7, 17.4_
  - [ ] 16.2 End-to-end integration suite
    - Fixture git repository with a local bare remote exercising isolate through teardown offline; seeded fake metering ledger exercising budget halt; deterministic stages verified to make zero model invocations
    - _Requirements: 13.16, 16.3, 6.4, 17.1, 17.2_

- [x] 17. Analysis
  - [x] 17.1 Capability provider registry, schemas, and transports
    - Resolve every Delegable_Capability from config to builtin, mcp, or command transport behind one invocation path; identical tool surface regardless; builtin provider shipped for each; Engine_Floor capabilities refuse any binding
    - Per-capability versioned request/response schemas; schema-validated responses; declared coverage surfaced; cost attributed to the run budget; provider output treated as untrusted data; provider identity, transport, coverage and degraded status audited and displayed
    - Unavailable, timed-out or schema-invalid provider falls back to builtin with a degraded marker and reason, never blocking the run; supplementary validation providers may only add findings, never suppress or downgrade engine findings or gates
    - _Requirements: 26.1, 26.2, 26.3, 26.4, 26.5, 26.6, 26.7, 26.8, 26.9, 26.10, 26.11, 26.12, 26.13, 26.14, 24.7, 11.2_
  - [x] 17.4 Analysis capability wiring
    - Publish versioned request and Analysis_Findings JSON Schemas; resolve the Analysis_Provider from config (local in-process, or an MCP stdio child from configured command/env/timeout) behind one engine call with an identical tool surface either way
    - Validate every response against the schema; key findings to acceptance criteria; surface declared skipped coverage; attribute declared cost to the run's budget; treat finding text as untrusted data
    - Unavailable, timed-out, or schema-invalid provider falls back to the local analyzer with a degraded marker and reason, never blocking authoring; analyzer identity, coverage, and degraded status audited
    - Bind the analysis capability through the registry: request carries document location, spec type and format version; findings keyed to acceptance criteria route into the Review_Queue
    - _Requirements: 26.6, 26.7, 26.8_
  - [x] 17.2 Bundled local analyzer
    - Deterministic structural checks: glossary terms used but undefined, unquantified qualifiers, criteria that are not independently testable, requirements with no covering task, overlapping or contradictory criteria within a requirement
    - Emit the shared Analysis_Findings schema with generated clarifying questions (choices, consequences, recommended answer); declare depth as structural; no network, zero model credits
    - _Requirements: 27.1, 27.2, 27.3, 27.4, 27.5_
  - [x] 17.5 Builtin provider bindings
    - Register the engine's own paths as the builtin providers for authoring (seeded turn behind validation and the phase gate), review (seeded verdict turn with review and test-quality criteria), implementation (per-task subagent dispatch), and model catalog (host resolution)
    - Bind the bundled GitHub/GitLab watch presets, marking a source unhealthy with the missing program name when its command-line dependency is absent; ship no supplementary validation rules; UI identifies each capability's provider as builtin or external and each builtin as deterministic or model-backed
    - Consume the analysis report rather than producing a second one. Task 17.4 built `AnalysisEngine`, `route_findings` and `AnalysisReport.to_review_items` in `engine/analysis.py`, and they have no non-test caller: the review binding is where criterion-keyed findings reach a human. Two things 17.4 could not do belong here. There is no findings sink in the data model -- `engine/review_queue.py` is a projection of runs in human-reserved states and the tables hold no analysis rows -- so persisting a report against a queued run is this task's, and inventing a second queue is not. And the surface that renders a finding's prose must escape control characters itself: the display contract keeps the line breaks prose is entitled to, so a terminal render that trusts them can still be reflowed by a crafted message
    - _Requirements: 31.1, 31.2, 31.3, 31.4, 31.5, 31.6, 31.7, 31.8_

  - [x] 17.3 Conformance runner
    - Per-capability conformance runner over bundled fixtures (planted ambiguity, contradictory criteria, coverage hole, oversized document, malformed response) asserting schema validity, planted-defect detection, declared coverage, timeout honoring and repeatability; every builtin provider passes its own suite
    - _Requirements: 26.15, 27.6_

- [ ] 19. Doctor
  - [x] 19.1 Finding vocabulary and aggregation
    - Stable Finding identifiers with severity, affected phase or surface, cause, and resolving action; aggregate phase prerequisites, source health, provider reachability and degradation, config validation, budget and kill-switch state, blocked runs, and skill/MCP registration reach
    - A check that cannot complete becomes a Finding and the remaining Findings still return; provider, command, and watched-item text carried as untrusted data, never executed; no operation modifies config, Autonomy_Policy, or Delivery_Workflow
    - Engine refusals, blocked dispatches, and degraded marks quote the same Finding identifier
    - Last known result recorded per identifier; a previously-passing check that now fails reports as a regression with when it last passed, notifies once, and stays quiet while unchanged; declared minimum program versions verified rather than presence alone
    - Aggregate source health from ONE resolution, not two. `prerequisites.check_source` and `watch/poll.py`'s `HealthReason.PROGRAM_UNAVAILABLE` currently answer the same question — is this source's poll program on PATH — in two representations. Both fail closed today, so this is a convergence rather than a defect, but a Doctor panel built on one and a watcher tick built on the other can disagree after either side is refactored. Either derive one from the other, or pin their agreement with a test that an absent poll program yields both, naming the same program
    - _Requirements: 34.1, 34.2, 34.4, 34.5, 34.6, 34.7, 34.8, 34.9, 34.10, 34.11_
  - [ ] 19.2 Doctor surfaces and surface equivalence
    - Doctor exposed as an Engine_MCP_Server tool and as the UI panel from the one engine operation; prerequisite Findings grouped by phase
    - Take the grouping from `PrerequisiteReport.by_phase` in `engine/prerequisites.py` rather than regrouping the checks here. Task 7.7 built the phase scoping and the run gate reads it; a second grouping could disagree with the one the gate refuses on, so Doctor would show a phase as ready while a run is refused for it
    - Equivalence test: the tool and the UI path return identical Findings for the same state
    - Give `refusal_finding_ids` and `dispatch_finding_id` their call sites, and populate the doctor's `minimum_versions`. Task 19.1 built all three and its review found the handoff had been REPORTED as recorded here while nothing was written -- so this entry exists because a claimed recording is worth no more than an unclaimed one. Requirement 34.6 wants an engine refusal, a blocked dispatch and a degraded mark to quote the same Finding identifier a Doctor panel shows; 19.1 built the two translators and nothing calls them, so today a refusal and a panel can name the same condition differently. And `Doctor.minimum_versions` is a caller-supplied mapping with no populator, which makes requirement 34.11 pass VACUOUSLY -- the version check verifies nothing while reporting no findings, which is the worst of both. 19.1 could not close either: no config key for a program minimum exists and `workflow.stages[].preset` expansion did not exist when it ran. Whoever populates it decides where a minimum is declared
    - Give the app's readiness state a READER, because requirement 4.4's third clause is satisfied only on paper without one. Task 13.1 built `readiness.py` -- a persistent, fail-closed not-ready state naming why registration failed -- and its review found NOTHING in the tree reads it: `on_gateway_startup` discards the AppContext so `ctx.health.mark_error` evaporates on the boot path, `GET /api/apps` carries no health field, and the Doctor check that WOULD read it does not exist. So after the one-shot enable response scrolls away, a half-registered app is indistinguishable from a whole one on every surface a user looks at, which is verbatim the failure 4.4 exists to end. Two readiness sources also coexist unreconciled: the host's `RegistrationResult`, fresh only at enable time, and the app's, fresh at every boot and read by no one. NOTE A DISAGREEMENT TO SETTLE RATHER THAN INHERIT: task 19.1 reported that skill/MCP-server reach was already covered through the config advisories' `AGENT_NOT_INSTALLED` / `AGENT_MISSING_ENGINE_TOOLS` codes, while 13.1's reviewer read the check catalog and found no registration check at all. Those answer different questions -- whether an agent can see the engine's tools, versus whether THIS app's registration landed -- so decide which requirement 34.1's "skill/MCP registration reach" means before building. The doctor must not import the app root to get this: pass the state in, the way `minimum_versions` is passed, and populate it here rather than leaving a second unpopulated seam
    - Warn at configuration time when a watch source polls open items only, because such a source can never derive a cancellation and the run for a closed item keeps going. `engine/watch/lifecycle.py` derives a cancellation only from a closure a poll REPORTS, reading an item's absence as a narrowed filter — so the filter in the poll command decides whether requirement 10.9 is reachable at all. Task 13.2's review found both bundled presets had this shape. The GitHub one is fixed (it now asks `state=all`); the GitLab one is NOT, deliberately: `glab` is not installed on this machine, so the flag that widens `glab issue list` past open items could not be verified, and guessing one is precisely the defect 13.2 shipped — a plausible flag the real CLI rejects, which failed every poll rather than degrading. `test_only_github_can_derive_a_cancellation_and_that_asymmetry_is_deliberate` pins the asymmetry and fails the day the GitLab argv gains a state filter. Whoever has a real `glab` verifies the flag and fixes the preset; the advisory is worth building regardless, because a user-written source has the same trap and no reviewer looking at it
    - _Requirements: 34.3, 32.2_

  - [x] 17.6 Semantic builtin and the async analysis job shape
    - Model-backed analysis builtin dispatching an agent turn with an authored analysis prompt at the agent, model, and effort configured for the analysis role, no network service; submit/poll job shape shared by every transport; configured total wall-clock deadline failing the job with elapsed time and partial progress
    - Dispatched turn output schema-validated before recording, invalid output fails the job with nothing partial recorded; spend attributed to the run's budget and subject to the ceiling and kill switch; every result records depth and provider identity; one findings schema across depths and transports
    - Every capability answers from a working builtin: no absent, stubbed, or not-configured tool in the surface
    - _Requirements: 35.1, 35.2, 35.3, 35.4, 35.5, 35.6, 35.7, 35.8_

- [ ] 18. Clean-room provenance gate
  - [ ] 18.1 Provenance checks and audit
    - Repository check asserting no non-public endpoints, service names, headers, or credentials appear in the tree; shipped prompt text authored for this app; delegated providers referenced by configuration only
    - _Requirements: 28.1, 28.2, 28.3, 28.4, 28.5, 28.6_

- [ ] 20. Engine composition and wiring
  - [x] 20.1 Composition root
    - One construction point building the engine's object graph, so a surface cannot assemble a partial one. Reach the orchestrator through `orchestrator_for` rather than assembling a wave runner by hand: task 9.1 made that factory the single construction point for the workspace broker, the role resolver and the completion reporter, so a surface that builds its own runner silently drops all three -- the inertness moves up one level rather than away
    - Register the engine's builtin providers and pass a findings sink. Task 17.5 built `register_builtins(registry, model_resolver=...)` and the `FindingsSink` seam but has no production caller, so today no capability resolves to a builtin and every analysis report lands in a memory-only default. Call it at the registry construction point and hand `AnalysisEngine` the durable sink once it exists
    - Build the shared collaborators the other slices need, rather than each slice building its own: an `AuditLog` rooted at the state root, a `RunMachine(state, config, audit=audit)`, and the `ReviewQueue` over it. `RunMachine.transition` is the only production writer of a run's state column, so two machines over one store would be two writers of a column whose single-writer property several tasks depend on
    - Everything in 20.2 through 20.6 depends on this slice; land it first
    - _Requirements: 31.1, 31.5, 9.1, 9.5_
  - [ ] 20.2 Entry-point gating and session seeding
    - Call `prerequisites.gate_run` before an entry point starts a run, and refuse on a returned `RunRefusal`. Task 7.7 built the gate and proved it refuses, but nothing constructs it outside tests, so today an entry point can author a whole spec and only then discover its delivery program is absent. The gate must run before the first credit, which means before the first dispatch and not at the phase that needs the missing thing
    - Construct the session seeder and hand it to the dispatcher. Task 15.1 built `engine/seeder.py` with the approval posture resolved from the app's own grant, the run-id stamping and the posture-mismatch refusal, and made `SessionSeeder` statically assignable to the dispatcher's `RunStarter` seam -- but nothing builds a `SessionOpener` over the host session manager and nothing passes the seeder as `start=`, so no headless run is seeded today
    - Give `notify_awaiting_review` a caller. Requirement 6.3 fires when a run reaches a gate reserved for human action, which is the transition into `awaiting_review` in `engine/runs.py`, and task 15.1 could not add that hook because another task owned the file. Whichever hook lands there, this is the task that has to prove a parked run actually announces itself
    - Migrate `engine_mcp/operations.py` onto `build_engine`, and harden the graph before five slices consume it. Task 20.1's review approved the composition root and left three things for its first caller, which is this task. (a) `EngineOperations` still builds its own `StateStore` and `AuditLog`; that is acceptable only while its six operations touch phases, validation, approvals and config and no run row or capability -- the moment the MCP surface gains a run-touching or capability-touching tool it becomes a second partial graph, so migrate it here rather than waiting for that. (b) A partial graph IS constructable at language level: the reviewer demonstrated direct `EngineGraph(...)` construction resolving authoring to the deterministic provider with an in-memory cost sink, and `dataclasses.replace(graph, registry=...)` returning a frozen graph with no builtins -- the module claims "by construction rather than by convention", so a `__post_init__` invariant check closes the gap between the claim and the code. (c) The machine-forwarding half of the single-writer invariant is held by exactly one test, and `guard_for` receiving the same machine is asserted nowhere
    - _Requirements: 32.1, 32.3, 32.4, 32.5, 32.6, 7.3, 6.3_
  - [ ] 20.3 Watcher path wiring
    - Register the watcher's script cron and install its shim. Task 8.1 built the tick and proved it costs nothing, but nothing installs or schedules it, so an unregistered watcher polls no source at all -- a review found the same inert shape in three separate tasks, which is why the wiring is named rather than assumed
    - Pass a cancel cascade, an audit log, and a screener to `dispatch_tick`. Tasks 8.4 and 8.6 made all three REQUIRED keywords rather than ones defaulting to `None`, deliberately: the tick can build its own spend gate from the stores it holds, but cancelling a run, auditing an ignored edit, and screening intake each need something it does not have, so a default could only have meant skip -- and each skip is a real loss (an item withdrawn mid-run keeps spending; an ignored edit goes unrecorded; attacker-authored text reaches a run unscreened). The cascade and audit log come from 20.1
    - Build the concrete screening provider that dispatches the review-role turn. It is a host seam like `RunStarter`, so constructing it belongs here rather than in the task that defined the seam
    - Pass the screener to `drain_queue` as well: the queue path is the second way an item starts, and a guarantee enforced on one of two entry paths is the shape that produced every security defect this spec has shipped
    - Wire the `claimed` feedback event by constructing `dispatch_tick`/`dispatch_source` with `feedback=`. It is inert for the same reason and is NOT covered by the run lifecycle's self-wiring -- the other three events (`awaiting_review`, `completed`, `failed`/`refused`) already fire from `RunMachine.transition`
    - Construct and schedule the review-feedback watcher, because task 11.2 built the whole of it and nothing constructs it -- verified by grep by both the implementer and the reviewer, with no half-wiring. `ReviewFeedbackWatcher.tick` and `release_quarantined_comment` have no production caller, so requirements 23.1, 23.3 and 23.5 hold at module level and are NOT true of a running engine: today a reviewer's comment on a delivered change reaches nothing. It needs the reviser seam (a fix-round dispatch), the screener 20.3 already builds for intake, the delivery pipeline for the stages, and its own poll schedule. This is the fourth module in this spec built complete and inert; the pattern is why every wiring obligation is now named on the task that owns the construction rather than assumed to follow
    - _Requirements: 10.2, 10.6, 10.10, 17.3, 25.1, 25.2, 25.3, 25.5, 25.6, 25.7, 23.1, 23.3, 23.5_
  - [ ] 20.4 Delivery path wiring
    - Call the pipeline's `deliver()` after `execute()`. Task 9.3 wired the review gate, the retry policy, the workspace janitor's `retire_run` and `record_deployment`, and resolved a SEPARATE delivery notifier -- `orchestrator_for` now takes both `notifier` (budget) and `delivery_notifier` (delivery), and a production caller passes the same host notifier to both because it satisfies each protocol. Neither has a non-test caller yet
    - Wire `delivery_submitted` by pointing the delivery pipeline's `on_submitted` at the poster. With `claimed` in 20.3 this completes requirement 10.10, which is only partly true of a running engine until both are wired
    - Apply the echo gate where element text enters a run's CONTEXT, not in front of the writeback poster. Task 8.7's first slice built `echoable_text`/`echo_permitted_for` in `engine/watch/echo.py`, and its review contradicted the implementer's own "one echo path" claim: element text reaches argv through the SHARED `StageExecutor.run_labelled`, which the delivery stages call as well as the feedback poster, and `review_title`/`review_summary` are engine-owned run-context variables any stage command can reference. A gate in front of `post_feedback` would leave every delivery-stage command uncovered. Gate at element-to-run-context population, with a refusal omitting the field rather than emptying it
    - Catch `StaleContent` at that call site as re-derive-or-skip. `echoable_text` lets it propagate, which is safe -- nothing is echoed -- but reaching `post_feedback` uncaught would surface a refusal-to-echo as a writeback FAILURE that keeps its ledger claim and suppresses the event
    - Add the artifact-URL run context variable the link-artifact operation needs. Task 8.7's bundled presets reference the delivery BRANCH where requirement 36.1 names a link-artifact operation, because no run-context variable carries a PR or MR URL. A comment naming a branch is not a link to the artifact, so 36.1 is satisfied only once this variable exists and a preset references it -- and the delivery pipeline is what learns the submitted artifact's URL
    - Handle the preset refusal when `resolve_authority` first gets a production caller. Task 13.4 made an unresolvable `workflow.preset` name raise `ConfigValidationError` from `workflow.configured` -- deliberately, replacing a silent ignore -- and `resolve_authority` in `engine/delivery/integration.py` reads `configured` with no `try`. Its reviewer found it has no production caller today (tests only), so this is latent rather than live; whoever wires it must convert the refusal the way `prerequisites.py:470` does, into an audited `RunRefusal`, rather than letting a bad preset name escape as an unhandled error from a delivery decision. While there: `_preset_stages` returns the live document node for a USER-DEFINED preset while bundled ones get deep copies -- harmless today because `document()` re-parses per load and nothing mutates `PresetSelection.stages`, but the asymmetry is undocumented and a caller that did mutate it would edit that project's configuration in place
    - _Requirements: 9.3, 9.4, 36.1, 36.7, 10.10, 33.3_
  - [ ] 20.5 Resume authority
    - Make the execution gate read the run's PERSISTED posture rather than re-resolving autonomy from config. Two reviews raised this shape independently. Task 8.6 caps a quarantined run to authoring and persists `posture` plus `screening_quarantined` on the run row, but nothing outside tests reads `posture` back to reconstruct a decision -- so a driver that re-resolves from `AutonomyPolicy` would run a quarantined item at its configured level with never-screened text, and requirement 25.4's "regardless of policy" would be false exactly when it matters
    - Stop treating a `tasks.md` checkbox as authority. Task 9.3's review found the sibling defect: `completed_tasks` counts a leaf complete when its checkbox is set, with no attribution, so a checkbox reaching the canonical spec directory would let a resumed run skip the review gate 9.3 built. Treat the approving verdict and the persisted posture as the authorities
    - Add the resume tests neither owning task could write without a driver. Both defects are resume paths trusting a stored fact whose authority nothing checks, and both are only reachable once 20.2 exists
    - _Requirements: 25.4, 9.3, 9.4_
  - [ ] 20.6 Semantic turn provider
    - Build the concrete `SemanticTurnProvider`. Task 17.6 built `SemanticAnalyzer`, `AnalysisJobs` and the Protocol with its total wall-clock deadline, but the Protocol has no implementation, so model-backed analysis depth cannot run at all -- the structural tier answers instead, which is why nothing appears broken today
    - Decide the stamp timing when you build it. One property is the provider's to honour and 17.6's review could not exercise it: the turn's session is stamped to the run only AFTER the provider returns, so an in-flight turn's spend is attributed post-hoc and the kill switch cannot preempt it. Stamp on dispatch if the host session key is knowable before the turn completes
    - _Requirements: 35.1, 35.2, 35.3, 35.4, 35.5, 35.6, 35.7, 35.8_

## Task Dependency Graph

```json
{"waves": [
  {"id": 0, "tasks": ["1.1", "1.2", "2.1", "2.2", "3.1"]},
  {"id": 1, "tasks": ["3.2", "4.1", "4.2", "5.1", "7.1", "8.1", "17.1"]},
  {"id": 2, "tasks": ["4.3", "5.2", "6.1", "7.2", "7.3", "8.2", "9.2", "17.2", "17.4"]},
  {"id": 3, "tasks": ["5.3", "5.4", "5.5", "6.2", "7.4", "7.5", "7.6", "7.7", "8.3", "9.1", "15.2", "17.3"]},
  {"id": 4, "tasks": ["8.4", "8.5", "8.6", "8.7", "9.3", "9.4", "10.1", "11.1", "15.1", "17.5", "17.6"]},
  {"id": 5, "tasks": ["10.2", "11.2", "12.1", "13.1", "13.2", "13.3", "13.4", "19.1", "20.1"]},
  {"id": 6, "tasks": ["12.2", "12.3", "12.5", "14.1", "16.1", "19.2", "20.2", "20.3", "20.4", "20.6"]},
  {"id": 7, "tasks": ["12.4", "16.2", "18.1", "20.5"]}
]}
```

## Notes

- Task 1 (core enablers) ships as a separate prerequisite PR to the gateway; everything else lands in the app.
- Every leaf includes its own unit tests; build gates are the repo's standard pytest + isort + flake8 + mypy + tsc + vitest run in a worktree.
- Task 16.1 implements the seven property-based correctness properties from design.md with hypothesis.
- All tests run offline: stage commands under test are stubbed binaries; the e2e suite uses a fixture git repo with a local bare remote and a seeded fake metering ledger.
- The app's working name is spec-engine; the final published name requires explicit sign-off before packaging (names are one-way doors).
