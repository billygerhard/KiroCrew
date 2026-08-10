# Requirements Document

## Introduction

KiroCrew replicates the full Kiro spec-driven development process (requirements -> design -> tasks -> execution) as an open, agent-agnostic system. Today the process is fragmented: the Kiro IDE owns one implementation, the Spec Builder app embeds its rules as prompt text in its own backend (reachable only through its embedded chat), and a separately hosted MCP server carries a third copy. This feature extracts the rules into a single Spec_Engine (rules as code), exposes it to any agent through an MCP tool surface that carries its own instructions, and packages the whole thing as a KiroCrew app in the public repository. The result supports both interactive authoring ("Spec this out" in any chat) and headless authoring (a watcher fires a spec run when an issue arrives), with a policy-governed review gate before execution (human review by default, configurable up to full autonomy per source), an optional autonomous delivery pipeline that carries results through the project's own delivery workflow, whether that is a pull request with CI, an internal code review, or plain local builds, and orchestrated execution that routes design/review work to a smart model and implementation work to a cheaper one.

## Glossary

- **Spec_Artifacts**: The on-disk contract for a spec: `requirements.md`, `design.md`, `tasks.md`, and the `.config.kiro` sidecar under `<project>/.kiro/specs/<name>/`.
- **Spec_Engine**: The library that implements spec rules as code: document validation, phase state machine, dependency-graph analysis, approval recording, and task status persistence. The single source of truth for spec discipline.
- **Engine_MCP_Server**: The MCP wrapper over the Spec_Engine that exposes its operations as tools to any agent.
- **Spec_App**: The KiroCrew app that packages the Spec_Engine, the Engine_MCP_Server, the discovery skill, and the Spec_Builder_UI for installation.
- **Host_Agent**: Any agent session (default agent, subagent, cron, third-party agent) that consumes the Engine_MCP_Server tools. Requires no spec-specific configuration.
- **Gateway**: The KiroCrew gateway process that hosts agent sessions and owns tool-approval decisions.
- **Review_Gate**: The structural boundary between spec authoring and spec execution. Execution starts only through this gate, governed by the Autonomy_Policy.
- **Autonomy_Policy**: Per-source, per-spec-type, and optionally per-submitter-class configuration that sets how far a triggered run proceeds without human action: authoring only, through execution, through delivery, or through integration. Loaded from configuration only.
- **Delivery_Pipeline**: The post-execution flow of a run, executing the stages of the configured Delivery_Workflow: isolate, submit, verify, publish, and teardown. Available to autonomous and interactive runs alike.
- **Delivery_Workflow**: A pure-configuration mapping of each delivery stage to the project's own terminal commands, for example git branching with a pull-request command and CI, an internal code-review command, or local-only build and test commands. Defined entirely in configuration with no plugin code.
- **Integration_Target**: The protected destination where changes become part of the project, such as a protected branch like mainline or the primary working copy of a non-version-controlled project.
- **Orchestrator**: The execution component that dispatches leaf tasks in dependency-graph waves and routes each role (design, review, implement) to its configured model.
- **Watcher_Dispatcher**: The headless trigger component that observes external sources (issue trackers) and starts headless spec runs for newly detected items.
- **Watched_Item**: A newly detected external item (for example a GitHub issue) that the Watcher_Dispatcher may turn into a headless spec run.
- **Spec_Builder_UI**: The dashboard application surface for browsing specs, reviewing documents, approving phases, and starting execution. A driver of the Spec_Engine, not an engine.
- **Setup_Assistant**: An agent-driven interactive setup flow that inspects existing project context, such as KiroCrew memory, Kiro steering files, and project documentation, to infer and propose the Spec_App configuration.
- **Review_Queue**: The engine-exposed set of runs waiting at human-reserved gates, renderable by any driver.
- **Cost_Profile**: A named, per-project-selectable bundle of role assignments, an optional Host_Agent plus a model and a reasoning effort per role, with a subagent concurrency cap and a default per-run budget ceiling.
- **Quality_Gate**: A verify-stage command declared with a severity: blocking, where failure stops the run and dispatches fix tasks, or advisory, where failure is recorded and surfaced without stopping the run.
- **Analysis_Depth**: The declared thoroughness of an analysis result: structural (deterministic checks), semantic (model-reasoned over the documents), or extended (a provider declaring coverage beyond semantic).
- **Doctor**: The single read-only diagnostic operation that aggregates prerequisite, health, provider, configuration, and budget state into a list of Findings, and is reachable from every surface.
- **Finding**: One diagnosed problem carrying a stable identifier, a severity, the phase or surface it affects, what is wrong, and the action that resolves it.
- **Prerequisite_Check**: A read-only check that a configured project can actually run: required programs resolve, provider transports reach, the base branch exists, the protected set is valid, notifications resolve, and an autonomous level has a budget ceiling.
- **Workflow_Preset**: A named, bundled or user-defined Delivery_Workflow or watch-source definition that a project selects and may override per stage or per field.
- **Capability_Provider**: The implementation bound to a delegable capability, resolved from configuration to one of three transports: builtin (the app's own implementation), mcp (an MCP server invoked as a child process), or command (a program invoked with structured input and output).
- **Delegable_Capability**: A capability whose implementation may be provided externally: analysis, document authoring, review verdicts, task implementation, supplementary validation rules, watch sources, and model catalogs.
- **Engine_Floor**: The capabilities that are never delegable and always execute in the engine: native-format validation, phase gate enforcement, Autonomy_Policy resolution, budget enforcement, the dispatch claim ledger, and the audit log.
- **Analysis_Provider**: The Capability_Provider bound to analysis. Returns Analysis_Findings.
- **Local_Analyzer**: The bundled Analysis_Provider that runs structural and lexical checks locally with no network access.
- **Analysis_Findings**: The structured analysis result: per finding a kind, severity, referenced acceptance criteria, message, and an optional clarifying question with choices, consequences, and a recommended answer; plus declared coverage and optional cost.
- **Provider_Interface**: The extension point through which pluggable capabilities, including analysis, model catalogs, review policy, and additional watch sources, plug into the Spec_Engine without changing its tool surface.

## Requirements

### Requirement 1: Rules-as-code validation engine

**User Story:** As a developer, I want spec rules implemented as code in a shared engine, so that every agent and UI enforces the same discipline instead of each reimplementing rules as prompt text.

#### Acceptance Criteria

1. WHEN a spec document is submitted for validation, THE Spec_Engine SHALL validate it against the native Kiro spec format, including required sections, EARS acceptance-criterion shape, sequential requirement numbering, task checkbox syntax, and dependency-graph JSON structure.
2. IF a document fails validation, THEN THE Spec_Engine SHALL return every violation with its file, location, and rule identifier.
3. WHEN tasks.md is validated, THE Spec_Engine SHALL verify that every leaf task references at least one acceptance criterion that exists in requirements.md.
4. WHEN tasks.md is validated, alone or as part of a full spec validation, THE Spec_Engine SHALL report every requirement that is not covered by at least one task.
5. WHEN a dependency graph is validated, THE Spec_Engine SHALL verify that the graph is acyclic, that every incomplete leaf task appears in exactly one wave, and that wave identifiers are sequential integers starting from zero.
6. THE Spec_Engine SHALL store engine-managed run state outside the native spec documents, and every spec directory SHALL remain consumable by the Kiro IDE and CLI.
7. IF engine-managed run state cannot be persisted outside the native spec documents, THEN THE Spec_Engine SHALL fail the operation and report the reason, and SHALL NOT write that state into a spec document.

### Requirement 2: Enforced phase advancement

**User Story:** As a spec owner, I want phase advancement enforced by the engine, so that an agent cannot self-promote a spec to ready-to-build by writing all documents at once.

#### Acceptance Criteria

1. WHEN the phase of a spec is requested, THE Spec_Engine SHALL derive it from the Spec_Artifacts present on disk combined with recorded approval state, as a read-only computation that never advances the phase as a side effect.
2. IF advancement to the next phase is requested while the current phase's document fails validation, THEN THE Spec_Engine SHALL refuse the advancement and return the validation errors.
3. IF advancement to the next phase is requested while the current phase lacks a recorded approval, THEN THE Spec_Engine SHALL refuse the advancement and identify the missing approval.
4. WHEN a phase approval is recorded, THE Spec_Engine SHALL persist the approver identity and timestamp with the approval.
5. IF a spec document is modified while a recorded approval exists for its phase, THEN THE Spec_Engine SHALL mark that recorded approval stale and require re-approval before any subsequent advancement, and WHERE no approval was recorded for that phase THE Spec_Engine SHALL leave approval state unchanged.
6. THE Spec_Engine SHALL serialize state-changing operations per spec, and IF a second session attempts a conflicting state change on the same spec, THEN THE Spec_Engine SHALL reject it and return the spec's current state.

### Requirement 3: Agent-agnostic MCP tool surface

**User Story:** As a user of any coding agent, I want the spec workflow exposed as MCP tools that carry their own instructions, so that a stock agent with no spec knowledge can author and run specs.

#### Acceptance Criteria

1. THE Engine_MCP_Server SHALL expose Spec_Engine operations as MCP tools, including authoring guidance, document validation, phase advancement, approval recording, task listing and status updates, review verdicts, and orchestration guidance.
2. WHEN a Host_Agent calls an authoring-guidance tool, THE Engine_MCP_Server SHALL return the complete authoring instructions for the requested flow as the tool result, including document formats, phase flow, and approval gates.
3. WHEN a Host_Agent with only the Engine_MCP_Server registered performs the spec workflow, THE Engine_MCP_Server SHALL supply all spec-specific knowledge required, without agent-side prompt or configuration changes, and THE Engine_MCP_Server SHALL NOT require that a Host_Agent lack other spec-related tools.
4. FOR ALL spec state operations, invoking an operation through the MCP tool surface and invoking the same operation through the Spec_Engine library interface SHALL produce identical resulting state.
5. IF the Engine_MCP_Server cannot supply the authoring guidance for a requested flow, THEN THE Engine_MCP_Server SHALL return an error and SHALL NOT return partial guidance.

### Requirement 4: Plain-prompt discovery in any chat

**User Story:** As a user, I want to say "Spec this out" or "Create a spec" in any chat, so that spec work starts without selecting a special agent or opening a UI.

#### Acceptance Criteria

1. THE Spec_App SHALL vend a discovery skill whose trigger phrases include natural spec requests such as creating a spec, planning a feature, fixing a bug as a spec, and making a quick plan.
2. WHEN the discovery skill is triggered, THE discovery skill SHALL direct the Host_Agent to obtain workflow instructions from the Engine_MCP_Server tools before performing any spec operation, rather than embedding format rules in the skill body.
3. WHEN the Spec_App is installed or enabled, THE Spec_App SHALL register its discovery skill and its Engine_MCP_Server so that both reach Host_Agent sessions, for builtin and installed app paths alike.
4. IF registration fails, THEN THE Spec_App SHALL complete installation, report a not-ready state with the failure reason, and SHALL NOT present itself as operational.

### Requirement 5: Feature, bugfix, and quick spec types

**User Story:** As a user, I want feature, bugfix, and quick-plan flows, so that the process weight matches the work.

#### Acceptance Criteria

1. THE Spec_Engine SHALL support three spec types: feature (requirements, design, tasks), bugfix (bug analysis, fix design, tasks), and quick (requirements and tasks only).
2. WHEN a spec is created, THE Spec_Engine SHALL record the spec type in the spec's `.config.kiro` sidecar and derive the required document set from that recorded type.
3. WHEN a spec is validated or advanced, THE Spec_Engine SHALL apply the document plan of the recorded spec type.
4. IF the spec type cannot be recorded when a spec is created, THEN THE Spec_Engine SHALL fail the creation, leave no partial spec directory behind, and refuse validation and advancement for any spec with no recorded spec type.

### Requirement 6: One workflow, interactive and headless

**User Story:** As a user, I want the same spec process to run interactively in chat or headless from a trigger, so that autonomy does not fork the workflow.

#### Acceptance Criteria

1. WHERE a spec run is interactive, THE Spec_Engine SHALL record phase approvals only from explicit user approval actions.
2. WHERE a spec run is headless, THE Spec_Engine SHALL record gate approvals from the Autonomy_Policy for every gate the policy covers, and THE Spec_Engine SHALL require human action for every gate the policy does not cover.
3. WHEN a headless run reaches a gate that the Autonomy_Policy reserves for human action, THE Spec_App SHALL notify the configured notification channel that the run is waiting for review.
4. FOR ALL completed spec runs, artifacts produced by interactive runs and artifacts produced by headless runs SHALL satisfy the same Spec_Engine validation rules.
5. THE Spec_App SHALL deliver notifications through the host gateway's notification channels, and THE Spec_App SHALL read the channel selection from project configuration.

### Requirement 7: Unattended tool approval for headless runs

**User Story:** As an operator, I want unattended spec runs to call tools without stalling on approval prompts, so that headless authoring completes without a human watching.

#### Acceptance Criteria

1. WHERE a session is seeded by the Spec_App for a headless run, THE Gateway SHALL apply the tool-approval posture granted to the Spec_App in configuration.
2. THE Gateway SHALL NOT allow a running agent session to modify or elevate its own tool-approval posture through any tool call.
3. WHEN a headless run starts, THE Spec_App SHALL record the applied approval posture in the run's audit log.
4. IF a session's applied approval posture does not match the posture granted in configuration, THEN THE Spec_App SHALL refuse to start the run, or halt it if already started, and record the mismatch in the audit log.

### Requirement 8: Policy-governed execution gate

**User Story:** As a spec owner, I want the execution gate driven by a policy I configure, so that I choose per source and spec type between human review and full autonomy.

#### Acceptance Criteria

1. WHERE the Autonomy_Policy reserves execution for human action, THE Review_Gate SHALL start spec execution only from an explicit human action.
2. WHERE no Autonomy_Policy level is configured for a source and spec type, THE Autonomy_Policy SHALL resolve to human-reserved execution, and WHERE a level is explicitly configured for that source and spec type THE Autonomy_Policy SHALL resolve to the configured level.
3. WHERE the Autonomy_Policy authorizes autonomous execution for a run's source, spec type, and submitter class, THE Review_Gate SHALL start execution when validation and required gate approvals pass, and SHALL NOT require any further trigger event or human action.
4. IF execution is requested for a spec whose tasks.md fails validation or whose gates lack required approvals, THEN THE Review_Gate SHALL refuse the request, return the blocking reasons regardless of the Autonomy_Policy, and record the refused request with its initiator in the audit log.
5. WHEN execution starts, THE Spec_Engine SHALL record the initiator, an explicit human identity or the Autonomy_Policy identifier, with a timestamp in the spec's audit log.
6. THE Spec_Engine SHALL load the Autonomy_Policy from configuration only, and THE Engine_MCP_Server SHALL NOT expose any tool that modifies the Autonomy_Policy.
7. THE Spec_Engine SHALL treat the Autonomy_Policy levels as strictly ordered, authoring, then execution, then delivery, then integration, and an enabled level SHALL imply every lower level.

### Requirement 9: Wave-based orchestration with role-based model routing

**User Story:** As a cost-conscious user, I want a smart model to design and review while a cheaper model implements, so that spec execution is affordable at scale.

#### Acceptance Criteria

1. WHEN executing a spec, THE Orchestrator SHALL dispatch leaf tasks wave by wave in dependency-graph order, running tasks within a wave in parallel up to a configured concurrency cap.
2. WHEN resolving the model for a unit of work, THE Orchestrator SHALL determine that work's role, one of design, review, or implement, and resolve the agent, model, and effort from the configuration entry for that role, falling back to the session default agent and model when that role is unconfigured.
3. WHEN a task implementation completes successfully, THE Orchestrator SHALL obtain a review verdict using the review role's model, and THE Orchestrator SHALL NOT mark a task complete unless that task's review verdict is approval.
4. IF a task does not complete successfully, including implementation failure, a review verdict requiring changes, or an infrastructure failure, THEN THE Orchestrator SHALL retry the task up to the configured retry limit and SHALL mark the task failed after the limit without abandoning independent tasks in the remaining waves.
5. WHILE execution is in progress, THE Spec_Engine SHALL persist task status after every task state change so that an interrupted execution resumes from the recorded state.

### Requirement 10: Watcher-triggered headless dispatch

**User Story:** As a team, I want new bug issues to trigger headless bugfix specs and feature requests to trigger headless plans, so that spec authoring starts before an engineer looks at the item.

#### Acceptance Criteria

1. WHERE watcher dispatch is enabled for a source, THE Watcher_Dispatcher SHALL start a headless spec run for each newly detected Watched_Item, mapping the item's classification to a spec type according to configuration.
2. THE Watcher_Dispatcher SHALL be disabled by default and SHALL require explicit per-source enablement.
3. WHEN a Watched_Item is selected for dispatch, THE Watcher_Dispatcher SHALL atomically claim the item in a persistent ledger before starting the run, and THE Watcher_Dispatcher SHALL NOT dispatch an already-claimed item.
4. WHILE the number of active headless runs is at the configured concurrency cap, THE Watcher_Dispatcher SHALL queue further Watched_Items instead of starting new runs.
5. WHEN a Watched_Item is passed to a headless run, THE Watcher_Dispatcher SHALL provide the item content as quoted data input to the run, and THE Watcher_Dispatcher SHALL delegate all execution and delivery decisions to the Autonomy_Policy.
6. THE Watcher_Dispatcher SHALL support watch sources defined in configuration as a poll command with a field mapping that yields each item's identifier, title, body, state, address, classification, and submitter, and THE Spec_App SHALL NOT require plugin code to define a command-based watch source.
7. THE Watcher_Dispatcher SHALL enforce the concurrency cap per project in addition to the global cap, and SHALL start queued Watched_Items in arrival order as capacity frees.
8. THE Spec_App SHALL bundle command-based watch source presets for GitHub and GitLab issues, and THE Spec_Engine SHALL accept additional watch sources registered through the Provider_Interface.
9. THE Watcher_Dispatcher SHALL derive Watched_Item lifecycle transitions, including reopened and cancelled, by comparing successive poll results.
10. WHERE item feedback commands are configured for a source, THE Watcher_Dispatcher SHALL post dispatch and completion updates back to the Watched_Item using those commands with the run context variables.
11. WHERE a watch source's items are publicly submittable, THE Spec_App SHALL warn the user when the execution, delivery, or integration autonomy levels are enabled for that source, and SHALL record the user's acknowledgment in the audit log.
12. WHERE an item's classification has no configured spec-type mapping and the source configures no default spec type, THE Watcher_Dispatcher SHALL NOT dispatch the item and SHALL record it as unmapped.

### Requirement 11: Public app with pluggable providers

**User Story:** As a maintainer, I want the app published in the public KiroCrew repository with proprietary capabilities pluggable, so that public consumers get a working engine while internal builds add enhanced analysis.

#### Acceptance Criteria

1. THE Spec_App SHALL build and operate with no internal-only dependencies in its default configuration.
2. THE Spec_Engine SHALL define the Capability_Provider interface for every Delegable_Capability, with a bundled builtin provider for each.
3. WHERE an enhanced provider is registered, THE Spec_Engine SHALL route the corresponding operations to that provider without changing the Engine_MCP_Server tool surface.
4. THE Spec_Engine SHALL perform all spec content processing on the local machine and SHALL NOT transmit spec content to remote services.
5. THE Spec_App SHALL NOT transmit telemetry by default, and WHERE telemetry is explicitly enabled it SHALL carry only anonymous operational counts and never spec content.

### Requirement 12: Spec Builder UI as a driver of the engine

**User Story:** As a dashboard user, I want the Spec Builder UI to drive the shared engine, so that the UI and agents cannot diverge on spec rules.

#### Acceptance Criteria

1. THE Spec_Builder_UI SHALL obtain spec state, validation results, and phase transitions from the Spec_Engine, and THE Spec_Builder_UI SHALL NOT implement independent spec rules.
2. WHEN a spec is modified through any driver, THE Spec_Builder_UI SHALL display the updated spec state on its next refresh, and WHERE a refresh fails THE Spec_Builder_UI SHALL retain the last known state with a visible staleness indicator.
3. THE Spec_Builder_UI SHALL provide the human review surface for the Review_Gate, including document review, phase approval, and starting execution.
4. THE Spec_Builder_UI SHALL provide a configuration surface for the Autonomy_Policy, the Delivery_Workflow stage commands, watch sources, model role assignments, and notification channels.
5. THE Spec_Builder_UI SHALL display each run's credit consumption, resolved from the per-turn metering ledger, on the run's Review_Queue entry and run detail view.
6. THE Spec_App SHALL replace the existing Spec Builder builtin as the single spec surface, and specs created by the prior app SHALL remain valid Spec_Artifacts.
### Requirement 13: Autonomous delivery through configured commands

**User Story:** As a user, I want to define my delivery workflow as configuration that maps each stage to my own terminal commands, so that any workflow works, pull requests, internal code reviews, or plain local builds, without writing plugin code.

#### Acceptance Criteria

1. THE Spec_App SHALL read the Delivery_Workflow from configuration that maps each of the stages isolate, submit, verify, publish, and teardown to terminal commands, and THE Spec_App SHALL NOT require plugin code to define a delivery workflow.
2. WHERE a stage has no configured commands, THE Delivery_Pipeline SHALL skip that stage.
3. WHEN a stage command runs, THE Delivery_Pipeline SHALL substitute run context variables into the configured command, including the spec name, the spec type, the workspace path, the base branch, the generated branch name, the triggering Watched_Item identifier and URL, and a review title and review summary derived from the spec documents.
4. WHERE the configuration defines custom project variables, THE Delivery_Pipeline SHALL substitute those variables into stage commands alongside the run context variables.
5. IF a stage command references a variable that has no value for the current run, THEN THE Delivery_Pipeline SHALL fail the stage before executing the command and report the missing variable.
6. THE Delivery_Pipeline SHALL pass substituted variable values to commands as literal argument values without shell interpretation of the values.
7. WHERE the Autonomy_Policy authorizes delivery for a run, THE Delivery_Pipeline SHALL run the isolate stage before task execution begins, producing an isolated workspace such as a feature branch, a git worktree, or a separate working copy.
8. IF a verify stage command exits with a failure status, THEN THE Delivery_Pipeline SHALL dispatch fix tasks up to the configured retry limit before marking the delivery failed.
9. WHERE a publish stage is configured, THE Delivery_Pipeline SHALL run it only after every configured verify stage has passed.
10. THE Spec_App SHALL bundle editable Delivery_Workflow presets, including a git-with-pull-request workflow and a local-only build-and-test workflow.
11. WHERE autonomous integration is not enabled for the project, THE Delivery_Pipeline SHALL NOT integrate changes into the project's Integration_Target, and integration SHALL require explicit human action. WHERE the project configuration explicitly enables autonomous integration, THE Delivery_Pipeline SHALL integrate changes into the Integration_Target after every configured verify stage has passed.
12. THE Spec_App SHALL accept Delivery_Workflow configuration changes only through its configuration surfaces, and THE Engine_MCP_Server SHALL NOT expose any tool that modifies the Delivery_Workflow.
13. WHEN the Delivery_Pipeline completes or fails, THE Spec_App SHALL notify the configured notification channel with the outcome of every executed stage.
14. WHEN a delivery stage executes, THE Spec_Engine SHALL record the stage, the commands run, the initiator, and the outcome in the spec's audit log.
15. FOR ALL concurrently active runs, each run operates in its own isolated workspace and no two runs share a working tree.
16. WHERE a project uses the bundled git workflow preset, THE isolate stage SHALL create a dedicated git worktree on a new branch cut from the refreshed base branch, so that concurrent runs share one repository without interfering.
17. THE Spec_App SHALL read the set of protected integration branches from configuration, and THE Delivery_Pipeline SHALL permit publish stage commands to push to non-protected environment branches, such as a development branch that feeds a test pipeline.
18. WHEN a publish stage command completes, THE Delivery_Pipeline SHALL capture the command output and include deployment addresses from that output in the run's notification, the Review_Queue entry, and the audit log.
19. WHEN a spec is archived, THE Delivery_Pipeline SHALL run the configured teardown stage commands to remove the run's dedicated deployments.
20. WHERE no protected branch set is configured for a project, THE Spec_App SHALL treat the project's base branch as protected.
21. IF autonomous integration is enabled for a project with no verify stage configured, THEN THE Spec_App SHALL warn the user at configuration time and record the warning in the audit log.
22. WHERE a run is interactive, THE Delivery_Pipeline SHALL be startable by explicit user action and SHALL apply the same stages, variables, and rules as autonomous delivery.
23. THE Delivery_Workflow SHALL define the order in which its configured stages run, and THE Delivery_Pipeline SHALL support verify stages configured to run before the submit stage, after it, or both.
### Requirement 14: Agent-assisted interactive setup

**User Story:** As a new user, I want an agent-guided setup that learns my project and delivery workflow from context that already exists, so that my configuration is generated and explained instead of hand-written.

#### Acceptance Criteria

1. WHEN a user starts the interactive setup, THE Setup_Assistant SHALL inspect the available project context, including KiroCrew memory, Kiro steering files, project documentation, and CI or build configuration files, to infer the project's delivery workflow, watch sources, and tooling.
2. WHEN the Setup_Assistant infers a proposed configuration, THE Setup_Assistant SHALL present each inferred setting together with the evidence it was inferred from before applying anything.
3. IF the Setup_Assistant cannot infer a required setting from the available context, THEN THE Setup_Assistant SHALL ask the user for that setting conversationally.
4. WHERE prior KiroCrew memory and steering files are absent, THE Setup_Assistant SHALL operate from project files alone.
5. WHEN the user approves a proposed configuration, THE Setup_Assistant SHALL write it through the same validated configuration path used by the configuration surface of the Spec_Builder_UI.
6. THE Setup_Assistant SHALL propose the authoring level as the Autonomy_Policy floor, and SHALL NOT enable the execution, delivery, or integration levels without explicit user confirmation of each level being enabled.
7. WHEN the interactive setup runs, THE Setup_Assistant SHALL offer the applicable Workflow_Presets, SHALL run the Doctor against the proposed configuration, and SHALL report each Finding with the action that resolves it.
### Requirement 15: Per-project cost profiles for models and effort

**User Story:** As a user who pays differently in different contexts, I want model, effort, and concurrency defaults bundled into selectable per-project profiles, so that a work project can maximize quality while a personal project strictly minimizes spend.

#### Acceptance Criteria

1. THE Spec_App SHALL define Cost_Profiles that assign an optional Host_Agent, a model, and a reasoning effort to each of the roles design, review, implement, and setup, together with a subagent concurrency cap and a default per-run budget ceiling.
2. WHERE a role has no assigned Host_Agent, THE Orchestrator SHALL seed that role's sessions with the session default agent.
3. WHEN a Cost_Profile assigns a Host_Agent to a role, THE Spec_App SHALL verify at configuration time that the assigned agent's tool surface includes the Engine_MCP_Server tools, and SHALL warn the user when it does not.
4. THE Spec_App SHALL bundle editable Cost_Profile presets, including a quality-first profile and a budget profile, and SHALL accept user-defined profiles.
5. WHERE a project has a selected Cost_Profile, THE Orchestrator SHALL resolve every agent, model, and effort decision for that project's runs from the selected profile, and WHERE a project has no selected Cost_Profile THE Orchestrator SHALL resolve from the session default agent and model and report that no profile is selected.
6. WHEN the Orchestrator dispatches a subagent, THE Orchestrator SHALL apply the run's Cost_Profile role assignment, including any assigned Host_Agent, to the subagent session.
7. WHEN the interactive setup runs, THE Setup_Assistant SHALL ask the user to choose a Cost_Profile rather than inferring one from project context.

### Requirement 16: Budget enforcement and kill switch

**User Story:** As a paying user, I want hard spend limits and a single stop control, so that autonomous activity can never run away with my money.

#### Acceptance Criteria

1. WHEN a session belonging to a spec run starts, THE Spec_Engine SHALL stamp the run identifier onto the session so that per-turn metering records are attributable to the run.
2. THE Spec_App SHALL compute a run's credit consumption from the per-turn metering ledger across all sessions belonging to the run, including authoring, orchestrator, and subagent sessions.
3. IF a run's attributed credit consumption reaches its budget ceiling, THEN THE Orchestrator SHALL halt further dispatch after in-flight turns complete, mark the run as halted for budget, and notify the configured notification channel with the consumed amount.
4. WHERE a spending cap is configured for a watch source, THE Watcher_Dispatcher SHALL stop dispatching new runs for that source once the cap is reached within the configured period.
5. THE Spec_App SHALL provide a single kill-switch action that pauses all watchers and halts all autonomous runs after in-flight turns complete.
6. WHEN a run completes or halts, THE Spec_App SHALL notify the configured notification channel with the run's total credit consumption and record it in the spec's audit log.
7. THE per-run budget ceiling SHALL be enforced independently of watch-source spending caps, so that a run halts on its own ceiling even when no source cap stopped its dispatch.
8. WHERE a budget warning threshold is configured, THE Spec_App SHALL notify the configured notification channel when a run's consumption crosses the threshold, without halting the run.

### Requirement 17: Deterministic execution outside reasoning steps

**User Story:** As a cost-conscious user, I want every deterministic part of the pipeline to run without model calls, so that idle watching and mechanical stages cost zero.

#### Acceptance Criteria

1. THE Spec_App SHALL execute watch polling, document validation, phase derivation, gate enforcement, delivery stage commands, and notifications without model invocation.
2. THE Spec_App SHALL restrict model invocations to document authoring, task implementation, review verdicts, fix-task generation, intake screening, delegated analysis, and interactive setup inference.
3. WHILE no new Watched_Item is detected, THE Watcher_Dispatcher SHALL consume zero model credits.
4. FOR ALL spec runs, the credit consumption attributed to validation, phase derivation, and delivery command execution SHALL be zero.
### Requirement 18: Run lifecycle and review queue

**User Story:** As a reviewer, I want every run to carry a defined lifecycle state and to find everything waiting on me in one queue, so that autonomous authoring stays manageable at any volume.

#### Acceptance Criteria

1. THE Spec_Engine SHALL track every run through defined states, including queued, authoring, awaiting review, executing, delivering, done, failed, halted for budget, cancelled, and stalled.
2. WHEN a run exceeds the configured timeout for its current phase, THE Spec_Engine SHALL mark the run stalled and notify the configured notification channel.
3. WHEN a stalled or interrupted run is resumed, THE Spec_Engine SHALL continue from the last persisted state, at task granularity during execution and at phase granularity during authoring, rather than restarting the run.
4. THE Spec_Engine SHALL expose the Review_Queue so that any driver can render it.
5. THE Spec_Builder_UI SHALL render the Review_Queue grouped by run state, and headless runs SHALL occupy ordinary agent sessions that appear in the dashboard session list.
6. THE Spec_Engine SHALL NOT archive or expire a spec based on elapsed time.
7. THE Spec_App SHALL archive a spec only on explicit user action or on cancellation of its triggering Watched_Item, an archived spec SHALL remain archived until explicitly unarchived, and archival SHALL be reversible.

### Requirement 19: Watch source to project mapping

**User Story:** As a user watching multiple sources, I want each source explicitly mapped to a project, so that a dispatched spec always lands in the right place with the right workflow and cost profile.

#### Acceptance Criteria

1. THE Watcher_Dispatcher SHALL read, for each watch source, a configured target project, base branch, and classification-to-spec-type mapping.
2. WHEN dispatching a Watched_Item, THE Watcher_Dispatcher SHALL create the spec in the source's target project and resolve the Delivery_Workflow and Cost_Profile from that project's configuration.
3. IF a watch source has no target project configured, THEN THE Watcher_Dispatcher SHALL NOT dispatch for that source and SHALL report the missing configuration.
4. THE Watcher_Dispatcher SHALL determine each Watched_Item's submitter class from configuration, using a configured maintainer list or the source's author-association field, and WHERE the submitter class cannot be determined THE Watcher_Dispatcher SHALL assign the least-trusted class.
5. WHERE the classification-to-spec-type mapping or the Autonomy_Policy is configured per submitter class, THE Watcher_Dispatcher SHALL resolve the spec type and the autonomy level using both the item's classification and its submitter class.
6. WHERE a project or watch source configures intake guidance for a spec type, such as a project-specific debugging playbook for bugfix runs, THE Watcher_Dispatcher SHALL include that guidance in the headless run's input, separated from the Watched_Item's quoted data, and intake guidance SHALL be writable only through the configuration surfaces.
7. WHEN a headless run is seeded, THE Spec_App SHALL run it in the target project's working tree so that the project's native Kiro steering files apply to the run.

### Requirement 20: Workspace stewardship

**User Story:** As a user whose work lives on branches, I want disposable workspace materializations cleaned up while branches and commits persist indefinitely, so that disk does not leak and work is never lost.

#### Acceptance Criteria

1. THE Spec_App SHALL record every isolated workspace and every per-run deployment it creates in a workspace ledger with the run identifier and location.
2. WHEN a run reaches a terminal state, THE Delivery_Pipeline SHALL remove disposable workspace materializations, such as git worktrees and temporary working copies, and SHALL preserve all branches and commits.
3. THE Spec_App SHALL NOT delete branches or commits created by a run.
4. WHEN a spec is archived, THE Spec_App SHALL clean up the spec's ledger-recorded workspace materializations.
5. THE Spec_Builder_UI SHALL provide a manual cleanup action for ledger-recorded workspaces.

### Requirement 21: Watched item lifecycle and re-dispatch

**User Story:** As a user, I want the triggering item's lifecycle to drive the spec's lifecycle, so that reopened issues re-run, cancelled issues stop work, and duplicates never double-dispatch.

#### Acceptance Criteria

1. THE Watcher_Dispatcher SHALL key dispatch claims on the Watched_Item identifier together with its lifecycle generation, so that a reopened item is dispatchable as a new run while an in-flight item is not.
2. IF a Watched_Item is cancelled while its run is in flight, THEN THE Spec_App SHALL cancel the run after in-flight turns complete, archive the spec, and record the cascade in the audit log.
3. WHILE a run is in flight, THE Spec_App SHALL ignore edits to the triggering Watched_Item and SHALL record that the edits occurred in the audit log.
4. THE Spec_Builder_UI SHALL provide a manual re-dispatch action that overrides the claim ledger for a selected Watched_Item.
### Requirement 22: Spec review feedback loop

**User Story:** As a reviewer, I want to approve or request changes on an authored spec from the review surface, so that headless authoring incorporates my feedback without me editing documents by hand.

#### Acceptance Criteria

1. THE Spec_Builder_UI SHALL provide approve and request-changes actions on each Review_Queue entry, and WHERE a run waits at a human-reserved gate, the approve action SHALL record that gate's approval.
2. WHEN a reviewer requests changes with comments, THE Spec_Engine SHALL record the comments, return the run to its authoring state, and dispatch a revision turn that receives the reviewer comments as quoted data input.
3. WHEN a revision turn completes, THE Spec_Engine SHALL validate the revised documents under the same rules as original documents and return the run to the Review_Queue.
4. IF revision cycles at a single gate exceed the configured limit, THEN THE Spec_App SHALL mark the run as needing human attention and SHALL NOT dispatch further revision turns for that gate.

### Requirement 23: Delivery review feedback loop

**User Story:** As a reviewer, I want my comments on the submitted review artifact to drive fix tasks automatically, so that the loop from issue to integrated fix closes without me writing code.

#### Acceptance Criteria

1. WHERE review-feedback watching is enabled for a project, THE Spec_App SHALL poll the run's review artifact for new reviewer comments using configured commands, following the same command-based pattern as watch sources.
2. THE review-feedback watching SHALL be disabled by default and SHALL require explicit per-project enablement.
3. WHEN new reviewer comments are detected on a run's review artifact, THE Spec_App SHALL dispatch fix tasks that receive the comments as quoted data input, and THE Delivery_Pipeline SHALL carry the resulting revision through the same stages as the original delivery.
4. THE Spec_App SHALL bound feedback cycles by the configured retry limit and the run's budget ceiling, and IF either bound is reached, THEN THE Spec_App SHALL mark the run as needing human attention and notify the configured notification channel.
5. WHILE no new reviewer comments are detected, THE review-feedback polling SHALL consume zero model credits.
### Requirement 24: Safe zero-configuration defaults

**User Story:** As a user who installs the app and configures nothing, I want every absent setting to resolve to a safe, useful default, so that the app works out of the box and never surprises me with spend or autonomy.

#### Acceptance Criteria

1. WHERE no Delivery_Workflow is configured for a project, THE Spec_Engine SHALL support authoring and execution in the project's working tree, matching Kiro IDE behavior, and THE Autonomy_Policy SHALL NOT resolve above the execution level for that project.
2. WHERE no Cost_Profile is selected and no per-run budget ceiling is configured, THE Spec_App SHALL apply a bundled default budget ceiling to every headless run, and a headless run SHALL NOT execute without a budget ceiling.
3. THE Spec_App SHALL ship bundled default values for every numeric limit, including the task retry limit, the revision cycle limit, phase timeouts, watch poll intervals, and the global and per-project concurrency caps, and each SHALL be overridable in configuration.
4. WHERE no notification channel is configured, THE Spec_App SHALL deliver notifications to the host gateway's default dashboard channel.
5. FOR ALL optional configuration settings, an absent setting resolves to a defined default and never causes a failure or a blocked operation by absence alone.
6. THE Spec_Builder_UI configuration surface SHALL display the effective value of every setting together with its origin, bundled default or explicit configuration.
7. WHERE no Capability_Provider is configured for a Delegable_Capability, THE Spec_Engine SHALL resolve that capability to its builtin provider.
### Requirement 25: Intake injection screening

**User Story:** As an operator wiring untrusted issue intake to autonomous runs, I want each watched item screened for prompt-injection attempts before autonomy applies, so that a crafted issue is quarantined for my review instead of steering an unattended agent.

#### Acceptance Criteria

1. WHERE intake screening is enabled for a Watched_Item's submitter class, THE Watcher_Dispatcher SHALL screen the item's content for embedded instructions and injection attempts before the run proceeds past intake, using bundled screening guidance.
2. THE intake screening SHALL default to enabled for every submitter class, and disabling it for a submitter class SHALL require explicit configuration.
3. WHERE intake guidance is configured for a project or source, THE screening SHALL apply that guidance in addition to the bundled screening guidance.
4. IF screening suspects injection, THEN THE Spec_App SHALL NOT proceed past the authoring level regardless of the Autonomy_Policy, SHALL flag the run and its screening findings in the Review_Queue, and SHALL notify the configured notification channel.
5. WHEN a reviewer releases a quarantined run, THE Spec_App SHALL treat the release as the human review action and proceed according to the Autonomy_Policy.
6. THE screening invocation SHALL resolve its model through the review role of the selected Cost_Profile, and its credit consumption SHALL attribute to the run's budget.
7. WHEN screening completes, THE Spec_Engine SHALL record the screening verdict and findings in the run's audit log.
### Requirement 26: Pluggable capability providers

**User Story:** As a user with my own tooling, I want every delegable capability bound to a provider I choose, so that the app hosts my analyzer, reviewer, or coding agent while still guaranteeing the rules the engine enforces.

#### Acceptance Criteria

1. THE Spec_Engine SHALL resolve each Delegable_Capability from configuration to a Capability_Provider using one of the transports builtin, mcp, or command, and THE Engine_MCP_Server tool surface SHALL be identical regardless of which providers are bound.
2. THE Spec_App SHALL ship a builtin Capability_Provider for every Delegable_Capability, and THE Spec_App SHALL NOT require any external provider to function.
3. THE Spec_Engine SHALL execute every Engine_Floor capability itself, and THE Spec_Engine SHALL NOT accept a Capability_Provider binding for any Engine_Floor capability.
4. WHERE the mcp transport is configured, THE Spec_Engine SHALL invoke the provider as an MCP server child process using the configured command and environment, and THE Spec_App SHALL NOT bundle, vendor, or embed that provider's implementation.
5. WHERE the command transport is configured, THE Spec_Engine SHALL invoke the configured program with the capability's structured input and SHALL parse its structured output, so that an external agent or command-line tool can serve a capability.
6. WHEN THE Spec_Engine invokes a Capability_Provider, THE request SHALL carry the artifact locations, the spec type, and the artifact format version.
7. WHEN a Capability_Provider returns a response, THE Spec_Engine SHALL validate it against that capability's published schema, and findings SHALL reference the acceptance criteria or tasks they concern.
8. THE Capability_Provider SHALL declare what it processed and what it skipped, and THE Spec_App SHALL surface skipped items to the user.
9. IF a Capability_Provider is unavailable, exceeds its configured timeout, or returns a response that fails schema validation, THEN THE Spec_Engine SHALL fall back to the builtin provider for that capability, mark the run degraded with the reason, and SHALL NOT block the run.
10. WHERE a response declares a cost, THE Spec_App SHALL attribute that cost to the run's budget.
11. THE Spec_Engine SHALL treat all Capability_Provider output as untrusted data, stored and displayed but never executed or interpreted as instructions.
12. WHERE a supplementary validation provider is bound, THE Spec_Engine SHALL add its findings to the engine's own findings, and THE Spec_Engine SHALL NOT allow a provider to suppress, downgrade, or override an engine finding or gate.
13. WHEN a capability completes, THE Spec_Engine SHALL record the provider identity, transport, declared coverage, and degraded status in the run's audit log, and THE Spec_Builder_UI SHALL display which provider served each capability.
14. THE Spec_App SHALL publish a versioned request and response schema for every Delegable_Capability.
15. THE Spec_App SHALL provide a conformance runner that verifies a candidate Capability_Provider for a named capability against bundled fixtures, checking schema validity, detection of planted defects where applicable, declared coverage, timeout honoring, and repeatability.

### Requirement 27: Bundled local analyzer

**User Story:** As a user of a default install, I want mechanical analysis without any external provider, so that spec quality comes from executable checks rather than from prompt wording alone.

#### Acceptance Criteria

1. THE Local_Analyzer SHALL run deterministic checks over the Spec_Artifacts, including glossary terms used but not defined, unquantified qualifiers, acceptance criteria that are not independently testable, requirements not covered by any task, and criteria within a requirement whose conditions overlap or contradict.
2. THE Local_Analyzer SHALL emit results in the Analysis_Findings schema used by external providers.
3. WHERE a finding admits a human decision, THE Local_Analyzer SHALL generate a clarifying question with choices, consequences, and a recommended answer.
4. THE Local_Analyzer SHALL declare its analysis depth as structural, and SHALL NOT report absence of findings as proof of correctness.
5. THE Local_Analyzer SHALL operate with no network access and SHALL consume zero model credits.
6. THE Local_Analyzer SHALL pass the conformance runner.

### Requirement 28: Clean-room implementation provenance

**User Story:** As a maintainer preparing this for an open repository, I want the implementation derived only from public surfaces, so that the app carries no provenance question.

#### Acceptance Criteria

1. THE Spec_App implementation SHALL be derived only from the publicly documented artifact format, publicly available spec artifacts, and the public host codebase.
2. THE Spec_App SHALL NOT contain code, schemas, or prompt text copied or adapted from non-public implementations.
3. THE Spec_App SHALL author all shipped prompt text for this app.
4. THE Spec_App SHALL NOT contain endpoints, service names, request headers, or credentials for non-public services.
5. WHERE enhanced capability is delegated, THE Spec_App SHALL reference the provider by configuration only.
### Requirement 29: Pre-submit quality gates

**User Story:** As a reviewer, I want quality gates to run before the change is submitted for review, so that analyzer and coverage findings are fixed by the run instead of landing on me.

#### Acceptance Criteria

1. THE Spec_App SHALL read Quality_Gates as verify-stage commands, each declared with a severity of blocking or advisory.
2. WHERE Quality_Gates are configured to run before the submit stage, THE Delivery_Pipeline SHALL run them before producing the review artifact.
3. IF a blocking Quality_Gate fails, THEN THE Delivery_Pipeline SHALL dispatch fix tasks up to the configured retry limit and SHALL NOT run the submit stage until it passes or the limit is reached.
4. IF an advisory Quality_Gate fails, THEN THE Delivery_Pipeline SHALL record the outcome and surface it on the run without stopping the run.
5. WHEN a Quality_Gate runs, THE Delivery_Pipeline SHALL substitute the run context variables, including the base branch, so that a gate can compare the change against its base.
6. WHEN a Quality_Gate produces output, THE Spec_Engine SHALL record the gate name, severity, exit status, and captured output in the run's audit log, and THE Spec_Builder_UI SHALL display each gate's outcome on the run.
7. THE Spec_App SHALL bundle editable Quality_Gate presets for test execution, coverage thresholds, linting, and type checking.
8. WHERE no Quality_Gates are configured, THE Delivery_Pipeline SHALL proceed without them and SHALL record that no gates ran.

### Requirement 30: Test quality verification

**User Story:** As an engineer relying on an autonomous run's tests, I want test quality judged explicitly, so that a green suite means the tests would actually catch a regression.

#### Acceptance Criteria

1. WHEN a task's implementation includes tests, THE review verdict SHALL judge those tests against defined criteria, including that assertions derive from the code under test rather than from values the test itself constructed, that the test fails when the covered behavior is wrong, and that error and boundary cases are covered.
2. IF a task's tests fail the test quality criteria, THEN THE Orchestrator SHALL treat the verdict as requiring changes.
3. WHERE a mutation probe command is configured as a Quality_Gate, THE Delivery_Pipeline SHALL run it and treat a suite that still passes under mutation as a gate failure.
4. WHEN a review verdict reports test quality findings, THE Spec_Engine SHALL record them in the run's audit log.
### Requirement 31: Shipped builtin providers

**User Story:** As a user of a default install, I want to know exactly what each builtin provider does, so that no capability is a stub and I can tell when I would want to bind an external provider.

#### Acceptance Criteria

1. THE builtin analysis providers SHALL be the Local_Analyzer at structural depth and the model-backed analysis builtin at semantic depth, selectable by configuration.
2. THE builtin authoring provider SHALL seed a turn with the Spec_App's authoring guidance and SHALL rely on native-format validation and the phase gate to accept or reject the produced documents.
3. THE builtin review provider SHALL seed a review turn using the review role's assignment, SHALL apply the review and test quality criteria, and SHALL return a verdict.
4. THE builtin implementation provider SHALL dispatch a subagent per leaf task with the spec context, wave ordering, and retry policy.
5. THE builtin watch source providers SHALL be the bundled GitHub and GitLab command presets, and IF a preset's required command-line program is unavailable, THEN THE Watcher_Dispatcher SHALL mark that source unhealthy and report the missing program rather than reporting no items.
6. THE builtin model catalog provider SHALL resolve the available models from the host.
7. THE Spec_App SHALL ship no supplementary validation rules, and native-format validation in the Engine_Floor SHALL be the validation baseline.
8. THE Spec_Builder_UI SHALL identify each capability's bound provider as builtin or external, and SHALL identify each builtin as deterministic or model-backed.
### Requirement 32: Prerequisite checks and safe failure

**User Story:** As a user configuring autonomy, I want to see what my project still needs before anything runs, so that a misconfiguration is reported up front instead of failing halfway through an unattended run.

#### Acceptance Criteria

1. THE Spec_App SHALL define Prerequisite_Checks per project, each scoped to the phase that requires it, covering the programs required by that phase's configured commands, reachability of the Capability_Providers that phase binds, existence of the configured base branch, validity of the protected branch set, resolvability of the notification channel, and presence of a budget ceiling for each enabled autonomy level above authoring.
2. THE Prerequisite_Check results SHALL be reported as Doctor Findings grouped by phase, and every unmet check SHALL state what is missing and the action that resolves it.
3. WHEN a run is about to start, THE Spec_App SHALL evaluate the Prerequisite_Checks for every phase that run's autonomy level will reach, including phases that execute later in the run, and IF any is unmet THEN it SHALL refuse the run before consuming model credits and SHALL report the unmet prerequisite.
4. THE Prerequisite_Checks for a watch source SHALL cover the programs that source needs in order to poll at all, and IF such a program is unavailable THEN the source SHALL be reported as an unmet prerequisite and as unhealthy, and SHALL NOT be reported as having found no items.
5. THE Prerequisite_Checks SHALL be read-only and SHALL consume zero model credits.
6. WHEN an unmet Prerequisite_Check prevents a run or a dispatch, THE Spec_Engine SHALL record it in the audit log.

### Requirement 33: Workflow presets and organization overrides

**User Story:** As a user whose organization uses its own review system, I want to start from a bundled preset and override only the stages that differ, so that I do not have to define a whole workflow to fit my tooling.

#### Acceptance Criteria

1. THE Spec_App SHALL bundle named Workflow_Presets for a git repository with pull requests, a git repository with merge requests, and a local-only build, and SHALL bundle watch-source presets for the same public hosts.
2. THE Spec_App SHALL NOT bundle presets for non-public review or tracking systems.
3. THE configuration SHALL allow a project to select a Workflow_Preset and to override individual stage commands, so that an organization's own review system can replace a stage without redefining the workflow.
4. THE configuration SHALL allow user-defined named Workflow_Presets that are selectable in the same way as bundled presets.
5. THE Spec_App SHALL treat bundled presets as read-only, and selection and overrides SHALL live in project configuration.
6. THE Spec_Builder_UI SHALL display, for each stage, whether its commands come from the selected preset or from a project override.
### Requirement 34: Doctor diagnostic

**User Story:** As a user whose spec runs are not working, I want one diagnostic I can open in the UI or ask my agent to run, so that I get the same actionable answer either way instead of guessing why the app refused.

#### Acceptance Criteria

1. THE Spec_Engine SHALL provide a Doctor operation that aggregates, into a single list of Findings, the phase-scoped Prerequisite_Check results, watch source health, Capability_Provider reachability and degraded status, configuration validation errors, budget ceiling and kill switch state, runs blocked or awaiting a human, and whether the app's skill and Engine_MCP_Server reached Host_Agent sessions.
2. FOR ALL Findings, THE Doctor SHALL report a stable identifier, a severity distinguishing blocking from advisory, the affected phase or surface, what is wrong, and the action that resolves it.
3. THE Doctor SHALL be reachable as an Engine_MCP_Server tool and from the Spec_Builder_UI, and FOR ALL surfaces invoking the Doctor against the same state SHALL return identical Findings.
4. THE Doctor SHALL be read-only and SHALL consume zero model credits.
5. IF an individual check cannot complete, THEN THE Doctor SHALL report that as a Finding and SHALL return its remaining Findings, and SHALL NOT fail the whole diagnostic.
6. WHEN the Spec_Engine refuses a run, blocks a dispatch, or marks a run degraded, THE reported reason SHALL carry the same Finding identifier the Doctor reports for that condition.
7. THE Doctor SHALL treat provider output, command output, and watched-item text included in a Finding as untrusted data to be stored and displayed, and SHALL NOT execute it.
8. THE Doctor SHALL NOT expose any operation that modifies configuration, the Autonomy_Policy, or the Delivery_Workflow.
9. THE Spec_Engine SHALL record the last known result per Finding identifier, and WHEN a check that previously passed is evaluated as failing, THE Doctor SHALL report it as a regression distinguished from a check that has never passed, including when it last passed.
10. WHEN a Finding is reported as a regression, THE Spec_App SHALL notify through the configured channel, and SHALL NOT notify for an unchanged Finding.
11. WHERE a Workflow_Preset or configuration declares a minimum version for a required program, THE Prerequisite_Check SHALL verify the resolved program's version against it rather than verifying presence alone.
### Requirement 35: Capability tool parity and the analysis depth ladder

**User Story:** As a user without any external provider, I want every capability tool present and working at the best depth available to me, so that binding a provider changes how deep the analysis goes rather than which tools exist.

#### Acceptance Criteria

1. FOR ALL Delegable_Capabilities, THE Spec_App SHALL ship a working builtin, and no tool in the Engine_MCP_Server surface SHALL be absent, stubbed, or answer with a not-configured error when no external provider is bound.
2. THE Spec_App SHALL ship a model-backed analysis builtin that performs semantic analysis by dispatching an agent turn with an authored analysis prompt, using the agent, model, and effort configured for the analysis role, and SHALL NOT require a network service.
3. FOR ALL analysis transports, THE tool shape SHALL be an asynchronous job comprising a submit operation that returns a job identifier and a poll operation that returns status, progress, and the findings on completion.
4. THE Spec_Engine SHALL apply a configured total wall-clock deadline to every analysis job, and IF the deadline elapses THEN it SHALL fail the job reporting elapsed time and partial progress, and SHALL NOT hold a call open indefinitely.
5. FOR ALL analysis results, THE Spec_Engine SHALL record the Analysis_Depth and the provider identity that produced them, and SHALL NOT report absence of findings at one depth as correctness at a greater depth.
6. WHEN a dispatched analysis turn returns, THE Spec_Engine SHALL validate its output against the Analysis_Findings schema before recording, and IF validation fails THEN it SHALL fail the job and SHALL NOT record partial findings.
7. FOR ALL depths and transports, findings SHALL be recorded in the single Analysis_Findings schema so a spec's analysis history is comparable across providers.
8. THE Spec_Engine SHALL attribute a dispatched analysis turn's spend to the run's budget, and the dispatch SHALL be subject to the budget ceiling and the kill switch.
