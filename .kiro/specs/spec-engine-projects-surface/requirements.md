# Requirements Document

## Introduction

The Spec_Engine's operator surface shipped (spec-engine-own-surface, 17/17) with a
setup flow and configuration editor that work but present the engine as a
single-project tool and assume the operator already knows what the engine is. Live
testing produced three findings from the owner: the first-run screen explains its
guard rails but not the product; the project path must be typed blind into a text
field although the dashboard already owns a directory picker; and although the
Config_Document and its resolution engine are already multi-project (a `projects`
section with narrowest-first precedence: per-source over per-project over the
project's selected cost profile over the app default), no surface lists projects,
shows a project's resolved settings, or offers setup for a second project.

This spec closes those three gaps in the existing operator UI. It changes no
resolution semantics: the per-project config model, the guarded backend routes
(`GET /config/resolved` already accepts `project` and `source` query parameters),
the approver-gated setup apply, and the JSON config format all stay exactly as
shipped. The work is presentation and flow: landing-pane behavior, orientation
copy, picker adoption, a projects view, and re-entry into the setup flow. The
established surface
invariants continue to bind: safety controls stay on the page shell (no overlay
may hide or trap focus from the kill-switch strip), all new strings ship in every
supported catalog, and files outside the app's declared territory are touched only
with a reviewed App_Boundary_Fence allowlist entry.

## Glossary

- **Spec_Engine**: the builtin app that drives spec-driven development runs; its
  engine, MCP tools, and backend routes shipped in prior specs.
- **Spec_App_UI**: the Spec_Engine's operator page in the dashboard (shell,
  queue, configuration, setup, and safety panels).
- **Setup_Assistant**: the four-step guided flow (inspect, answer, review plan,
  approve and apply) over the backend's `/setup/inspect`, `/setup/plan`, and
  `/setup/apply` routes.
- **Config_Document**: the single JSON document the engine's ConfigStore holds,
  containing app-scoped settings, a `cost_profiles` section, and a `projects`
  section of per-project entries.
- **Effective_Settings**: the resolved value in force for every setting for a
  given project and source, each carrying its origin scope, as served by
  `GET /config/resolved`.
- **Project_Entry**: one project's block in the Config_Document's `projects`
  section.
- **Project_Picker**: the dashboard's existing shared directory-picker component
  (browse directories plus recent projects), already used by the sessions bar
  and folder configuration.
- **Operator**: a signed-in dashboard user acting through the operator-guarded
  routes; app-minted tokens are refused by those routes.
- **First_Run**: the state in which the Config_Document is absent or contains no
  Project_Entry.

## Requirements

### Requirement 1: First-run landing and orientation

**User Story:** As an SDE opening the Spec_Engine page for the first time, I want
to land directly on a setup page that explains what the engine does and what to
do first, so that I can act without hunting through navigation or reading the
source code.

#### Acceptance Criteria

1. WHILE First_Run, THE Spec_App_UI SHALL present the Setup_Assistant as the
   landing pane when the page opens.
2. WHILE First_Run, THE Spec_App_UI SHALL list the Setup_Assistant first in the
   pane navigation.
3. WHILE First_Run, THE Setup_Assistant pane SHALL display an orientation that
   states what the Spec_Engine does, what completing the Setup_Assistant
   produces, and names the inspect step as the first action.
4. THE orientation SHALL describe each of the four Setup_Assistant steps in
   terms of what the Operator does and receives at that step, in addition to
   any guard-rail statements.
5. WHEN at least one Project_Entry exists, THE Spec_App_UI SHALL present the
   Queue as the landing pane, SHALL list the Queue first in the pane
   navigation, and SHALL NOT display the First_Run orientation as the page's
   primary content.
6. THE Spec_App_UI SHALL ship every orientation string through the app's
   localization catalogs, with no hardcoded user-facing English in components.
7. WHERE a setup step is not yet reachable because a prior step is incomplete,
   THE Setup_Assistant SHALL state which prior step must complete first.
8. IF the configuration read that determines First_Run fails, THEN THE
   Spec_App_UI SHALL state the read failure and SHALL NOT apply the First_Run
   landing or orientation as if the absence of configuration were known.

### Requirement 2: Project path picker

**User Story:** As an Operator configuring a project, I want to browse and pick
the project path in the UI, so that I do not have to type a host path from
memory.

#### Acceptance Criteria

1. WHEN the Setup_Assistant offers project-path entry, THE Spec_App_UI SHALL
   offer the shared Project_Picker alongside manual text entry.
2. WHEN the Operator selects a directory in the Project_Picker, THE Spec_App_UI
   SHALL fill the path entry with the selected absolute path.
3. THE Spec_App_UI SHALL use the dashboard's existing Project_Picker component
   rather than a reimplemented directory browser.
4. WHILE the Project_Picker is open, THE Spec_App_UI SHALL keep the kill-switch
   strip visible and operable.
5. IF the directory-browse read fails, THEN THE Spec_App_UI SHALL state the
   failure and keep manual path entry available.

### Requirement 3: Projects surface

**User Story:** As an Operator with several projects on one machine, I want to
see every configured project and the settings in force for each, so that I can
tell which configuration governs which project.

#### Acceptance Criteria

1. THE Spec_App_UI SHALL list every Project_Entry in the Config_Document,
   identifying each project.
2. WHEN the Operator selects a listed project, THE Spec_App_UI SHALL display
   that project's Effective_Settings with each value's origin scope.
3. WHEN the Effective_Settings read fails, THE Spec_App_UI SHALL state the
   failure rather than rendering defaults or retained values as current.
4. THE Spec_App_UI SHALL obtain per-project Effective_Settings through the
   existing guarded resolved-config read, passing the project identifier.
5. FOR ALL pairs of distinct Project_Entry values in the Config_Document,
   writing one project's entry SHALL leave every other project's stored entry
   and resolved Effective_Settings unchanged.
6. WHEN the Operator removes a Project_Entry, THE Spec_App_UI SHALL submit the
   removal through the guarded config write path, and the write SHALL be
   recorded in the config write log.

### Requirement 4: Repeatable setup flow

**User Story:** As an Operator, I want to run the Setup_Assistant again for
another project, so that adding my next project is the same guided flow as the
first.

#### Acceptance Criteria

1. WHILE at least one Project_Entry exists, THE Spec_App_UI SHALL continue to
   offer the Setup_Assistant for configuring an additional project.
2. WHEN a Setup_Assistant apply completes for a new project, THE Spec_App_UI
   SHALL show the new project in the projects list without a manual page
   reload.
3. IF the inspect step targets a path whose Project_Entry already exists, THEN
   THE Setup_Assistant SHALL state that the project is already configured and
   SHALL offer re-inspection of the existing entry instead of creating a
   duplicate Project_Entry.
4. WHEN a Setup_Assistant apply completes for an additional project, THE
   Config_Document SHALL contain the prior projects' entries unchanged.
5. THE Setup_Assistant SHALL require the same named-approver apply gate for an
   additional project as for the first.
