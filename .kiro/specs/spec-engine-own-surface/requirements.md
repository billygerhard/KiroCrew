# Requirements Document

## Introduction

The `agent-agnostic-spec-engine` spec shipped a working engine and then put its
operator surface in the wrong place. Its design opened with "this feature ships
**one** KiroCrew app... and the absorbed Spec Builder UI", but what shipped is two
apps: a new `spec-engine` app with no user interface, and roughly 7,700 lines of
engine-driving code added to `spec-builder` — a **pre-existing app owned by
another team**, present on `origin/main` since PR #518, whose interface this
project was started specifically to replace.

Seven of the Prior_App's own files were modified, including a deleted line in its
manifest that retired one of its declared skills, and eleven new files were added
inside its trees. That is the defect this spec exists to correct.

Correcting it exposes a second gap. The Engine_MCP_Server vends ten tools that
cover authoring, validation and diagnosis, but nothing else in the engine has a
surface at all: the Setup_Assistant, the Config_Store write path, and the whole
run half (run state, spend metering, the kill switch, delivery) were reachable
only through the code placed inside the Prior_App. Removing that code without
replacing the surface would leave a library nothing can call.

This spec therefore does three things in order: restore the Prior_App to its
original state and fence it against recurrence; give the Spec_App its own agent
surface for setup and configuration so a human is not required to hand-write
configuration; and give the Spec_App its own Operator_Surface, designed from
mockups rather than inherited from an interface the owner rejected.

Out of scope, and deliberately: the delivery-isolation defect recorded as an open
obligation in the prior spec's `tasks.md` (delivery stages do not execute in the
run's isolated checkout). It is orthogonal to surface ownership and remains
recorded there.

## Glossary

- **Spec_App**: The KiroCrew builtin app this project owns, directory name `spec_engine`, which packages the Spec_Engine library, the Engine_MCP_Server, the discovery skill, and — after this spec — the Operator_Surface.
- **Prior_App**: The pre-existing `spec-builder` builtin app, owned by another team and present on the repository's default branch before this project began. Not ours to modify.
- **Spec_Engine**: The rules-as-code library under the Spec_App that derives phases, validates documents, records approvals, and owns run state.
- **Engine_MCP_Server**: The stdio MCP server the Spec_App declares, which exposes Spec_Engine capabilities as tools to any agent.
- **Operator_Surface**: The Spec_App's own dashboard page and its backing HTTP routes, through which a human reviews and operates runs.
- **Setup_Assistant**: The Spec_Engine component that inspects a project, infers configuration, asks a human the questions it cannot infer, and applies an approved plan.
- **Config_Store**: The Spec_Engine component owning the single write path to the Spec_App's `config.json`.
- **App_Boundary_Fence**: A build gate asserting that the Spec_App's source tree modifies and contains no file belonging to any other app.
- **Merge_Base**: The commit at which this project's branch diverged from the repository's default branch, and therefore the authoritative record of the Prior_App's unmodified content.

## Requirements

### Requirement 1: Restore the Prior App

**User Story:** As the owner of a neighbouring app, I want my app returned to exactly the state it was in before this project touched it, so that I can trust that another team's feature work leaves no residue in my code.

#### Acceptance Criteria

1. WHEN the Prior_App's tree is compared against the Merge_Base, THE Spec_App SHALL introduce no difference in any file the Prior_App owns.
2. WHEN the Prior_App's manifest is compared against the Merge_Base, THE Spec_App SHALL leave its declared skills, routes, permissions and metadata byte-identical, including the skill declaration this project removed.
3. WHERE this project added a file under a directory the Prior_App owns, THE Spec_App SHALL contain no such file after remediation.
4. IF a change this project made to the Prior_App fixed a genuine defect in the Prior_App, THEN THE Spec_App SHALL record that defect for separate reporting to its owner rather than retaining the change.
5. WHEN the Prior_App's own test suite is run after remediation, THE Prior_App SHALL pass at the same count it passed at the Merge_Base.

### Requirement 2: Enforce an app boundary fence

**User Story:** As a developer on this project, I want a build gate that fails when our app reaches into another app, so that this class of trespass cannot recur silently.

#### Acceptance Criteria

1. WHEN the App_Boundary_Fence runs, THE App_Boundary_Fence SHALL report every source file outside the Spec_App's own trees that this project's branch has modified or added.
2. WHEN the App_Boundary_Fence finds such a file, THE App_Boundary_Fence SHALL fail the build and name the file and its owning app.
3. WHERE a change outside the Spec_App's trees is legitimate, THE App_Boundary_Fence SHALL require that path to appear in an explicit, reviewed allowlist rather than being inferred.
4. FOR ALL files the App_Boundary_Fence reports as in-bounds, the file resides under a directory the Spec_App declares as its own.
5. IF the App_Boundary_Fence cannot determine the Merge_Base, THEN THE App_Boundary_Fence SHALL fail rather than report a clean result.

### Requirement 3: Setup assistant reachable by an agent

**User Story:** As an agent with no user interface available, I want to walk a human through building the engine's configuration conversationally, so that a human never has to hand-author a configuration file.

#### Acceptance Criteria

1. WHEN an agent calls the Setup_Assistant's inspection tool with a project path, THE Engine_MCP_Server SHALL return the evidence gathered, the values inferred from it, and the questions that could not be inferred.
2. WHEN an agent supplies answers to those questions, THE Engine_MCP_Server SHALL return the resulting configuration plan without applying it.
3. WHEN an agent requests that a plan be applied, THE Engine_MCP_Server SHALL require an explicit human approver identity and SHALL refuse to apply a plan without one.
4. IF the Setup_Assistant cannot infer a project's subject with confidence, THEN THE Engine_MCP_Server SHALL return a refusal naming the ambiguity rather than selecting a plausible default.
5. WHERE the Setup_Assistant offers a bundled workflow preset, THE Engine_MCP_Server SHALL present the preset's declared programs so a caller can see what the preset will execute.

### Requirement 4: Configuration reachable by an agent

**User Story:** As an agent operating headlessly, I want to read and write the engine's configuration through the same fenced path the user interface uses, so that there is one write path rather than two.

#### Acceptance Criteria

1. WHEN an agent requests the current configuration, THE Engine_MCP_Server SHALL return it without exposing any value the Config_Store classifies as secret.
2. WHEN an agent submits a configuration patch, THE Engine_MCP_Server SHALL apply it through the Config_Store's single write path and SHALL reject a patch the Config_Store would refuse.
3. IF a configuration patch would bind a delegated provider to a vendored implementation inside the Spec_App, THEN THE Engine_MCP_Server SHALL refuse the patch regardless of the transport named.
4. FOR ALL configuration writes accepted through the Engine_MCP_Server, the resulting file is the same the Operator_Surface would have produced for the same patch.
5. WHILE a configuration write is in progress, THE Config_Store SHALL not block the gateway's event loop.

### Requirement 5: One app card and one page of its own

**User Story:** As an operator, I want the engine to have exactly one app card and one page of its own, so that enabling the engine gives me a working system rather than half of one.

#### Acceptance Criteria

1. WHEN the Spec_App's manifest is loaded, THE Spec_App SHALL declare its own dashboard page and its own backing routes.
2. WHEN the app store is listed, THE Spec_App SHALL appear as a single entry, and no second entry SHALL be required to obtain the Operator_Surface.
3. WHEN the Operator_Surface issues a request, THE Spec_App SHALL serve it from a route the Spec_App declares, and SHALL NOT depend on any route the Prior_App declares.
4. IF the Spec_App is enabled and its page is opened before configuration exists, THEN THE Operator_Surface SHALL offer the Setup_Assistant rather than presenting an empty form.
5. WHERE the Spec_App declares a page, THE Spec_App SHALL supply the localized label and description keys the app-manifest gate requires for every shipped locale.

### Requirement 6: An operator surface that is designed, not inherited

**User Story:** As the owner of this project, I want our interface designed rather than inherited, because the interface we replaced is the one I rejected and porting its shape forward would repeat the mistake.

#### Acceptance Criteria

1. BEFORE any Operator_Surface component is implemented, THE Operator_Surface SHALL be presented as at least two materially different mockups for selection.
2. WHEN a mockup is selected, THE Operator_Surface SHALL be implemented to match the selected mockup's layout, density and interaction model.
3. WHERE the Operator_Surface presents a run, THE Operator_Surface SHALL present it for review and operation rather than requiring a human to drive spec authoring by hand.
4. WHEN the Operator_Surface renders untrusted text originating from an outside submitter, THE Operator_Surface SHALL bound its layout so that line count cannot displace surrounding controls.
5. FOR ALL Operator_Surface components implemented under this spec, the component resides under the Spec_App's own frontend directory.

### Requirement 7: Provenance preserved under an inbound surface

**User Story:** As a reviewer of the public build, I want the provenance fence to keep forbidding outbound network access while permitting the app to serve its own page, so that gaining a user interface does not weaken the clean-room guarantee.

#### Acceptance Criteria

1. WHEN the provenance posture check runs, THE Spec_App SHALL continue to fail the build if it imports a module capable of initiating an outbound network request.
2. WHERE the Spec_App imports a server framework solely to declare inbound routes, THE provenance posture check SHALL distinguish that import from an outbound client and SHALL state the distinction it draws.
3. IF the distinction between inbound serving and outbound transmission cannot be drawn from the import alone, THEN THE provenance posture check SHALL fail rather than permit the import.
4. WHEN the Operator_Surface's frontend files are added, THE provenance posture check SHALL scan them under the same rules it applies to the Spec_App's other trees.
5. FOR ALL rules the provenance posture check enforces, a planted violation exists that the check reports.
