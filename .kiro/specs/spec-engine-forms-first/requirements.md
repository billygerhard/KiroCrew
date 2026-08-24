# Requirements Document

## Introduction

The Configuration pane grew around its JSON editor: the raw document is the
first thing an operator sees, and for most stored things it is the only write
surface. Reading is already solved — the resolved pane renders every value
with its origin — but changing anything beyond a grid cell, a role reset, or a
project removal means hand-editing JSON. Watch_Sources are the sharpest case:
they are creatable only through the Setup_Assistant's offers, which exist only
when project inspection produced evidence for them, so a project without a
recognizable remote has no UI path to a source at all.

This feature inverts the pane: forms lead, the JSON becomes an on-request
view. Settings edit through typed inputs generated from the engine's own
Setting_Registry (which already carries each setting's type, bounds, and
summary). Cost profiles and their role assignments edit through forms that
finally state the effort-on-`auto` rule where the operator meets it.
Watch_Sources become creatable and editable through a preset-constrained form
— never freeform commands, because a source's poll is argv the engine
executes, the write door validates only its shape, and the bundled preset
tables are the established boundary on what the engine runs.

Nothing about the write model changes: every form funnels into the existing
guarded write path, and the exact JSON patch remains the confirmation step —
JSON moves from being the editor to being the receipt. The Setup_Assistant
remains the path that creates a project; forms edit what exists.

The established surface invariants bind: no overlay may cover the kill-switch
strip and nothing renders in a modal or drawer; a failed read states the
failure and never renders retained or default values as current; every new
operator-facing string ships in all localization catalogs; out-of-territory
files require boundary-fence allowlisting.

## Glossary

- **Spec_App_UI**: the spec-engine app's operator page in the dashboard, whose
  Configuration pane this feature restructures.
- **Config_Document**: the engine's single JSON configuration document.
- **Guarded_Write_Path**: the engine's single fenced configuration write door —
  operator-authenticated, schema-validated, recorded in the durable write log.
- **Setting_Registry**: the engine's registry of settings, each carrying a
  dotted key, value type, bounds, permitted scopes, and an operator-facing
  summary.
- **Cost_Profile**: a named `cost_profiles` entry holding Role_Assignments and
  profile-pinned settings.
- **Role_Assignment**: one role's model and effort within a Cost_Profile.
- **Watch_Source**: a named entry under the Config_Document's `sources`
  section from which the engine ingests work items, carrying among other
  fields a poll command the engine executes.
- **Source_Preset**: a bundled Watch_Source template whose commands come from
  the engine's own preset tables.
- **Exact_Patch_Review**: the confirmation step showing the literal JSON patch
  a write will submit, shown before anything is written.
- **JSON_View**: the raw Config_Document editor, reachable on request.
- **Form_Surface**: the collection of typed forms this feature adds.
- **Operator**: the human using the dashboard page.
- **Setup_Assistant**: the existing guided flow that inspects a project and
  writes its first configuration under a named approver.

## Requirements

### Requirement 1: Forms lead and the JSON is on request

**User Story:** As an Operator, I want the Configuration pane to lead with
forms I can read and fill as a human, so that editing configuration does not
require thinking in JSON.

#### Acceptance Criteria

1. WHEN the Operator opens the Configuration pane, THE Spec_App_UI SHALL
   present the Form_Surface without rendering the JSON_View's content.
2. WHILE the Configuration pane is open, THE Spec_App_UI SHALL offer one
   explicit control that opens the JSON_View.
3. WHEN the JSON_View is opened, THE Spec_App_UI SHALL provide the same
   document editing and validation behavior the pane provides today, so
   anything the Form_Surface cannot express remains reachable.
4. WHEN a write submitted through the JSON_View or the Form_Surface succeeds,
   THE Spec_App_UI SHALL refresh both from a fresh configuration read, so the
   two never disagree.
5. IF the configuration read behind the Form_Surface fails, THEN THE
   Spec_App_UI SHALL state the failure and SHALL NOT render form values from
   retained or default data.

### Requirement 2: Settings edit through registry-generated forms

**User Story:** As an Operator, I want each setting to be a typed input with
its meaning beside it, so that changing a timeout or a ceiling is a form fill
rather than a JSON edit.

#### Acceptance Criteria

1. WHEN the settings form renders, THE Spec_App_UI SHALL derive its fields
   from the Setting_Registry vocabulary the read supplies — key, value type,
   bounds, permitted scopes, and summary — rather than from a hard-coded field
   list.
2. WHERE a setting's registry type is numeric, THE Spec_App_UI SHALL render a
   numeric input carrying the registry's bounds; WHERE boolean, a two-state
   control; WHERE string, a text input.
3. WHILE a setting field is displayed, THE Spec_App_UI SHALL display the
   registry's operator-facing summary with it.
4. WHEN the Operator edits a setting at a scope, THE Spec_App_UI SHALL offer
   only the scopes the registry permits for that setting.
5. WHILE a setting field is displayed, THE Spec_App_UI SHALL display the
   value currently in force and its origin, and a pending edit SHALL be
   visibly distinct from the value in force.
6. IF the Guarded_Write_Path refuses a settings write, THEN THE Spec_App_UI
   SHALL display the refusal's stated reason against the configuration path it
   names, and the form SHALL continue to display stored state rather than the
   submitted values.

### Requirement 3: Cost profiles and role assignments edit through forms

**User Story:** As an Operator, I want to change a role's model and effort on
a form that explains the rules, so that tuning a profile does not require
knowing the config schema or why an effort pin is inert.

#### Acceptance Criteria

1. WHEN a Cost_Profile is selected, THE Spec_App_UI SHALL render its
   Role_Assignments as a form — one row per role with the role vocabulary the
   read supplies — beside the profile's pinned settings.
2. WHILE a Role_Assignment's model is the unpinned `auto`, THE Spec_App_UI
   SHALL state beside its effort control that a pinned effort takes effect
   only once a concrete model is named.
3. WHEN the Operator edits a Role_Assignment or a profile-pinned setting, THE
   Spec_App_UI SHALL stage the change without writing, and the staged change
   SHALL be visibly distinct from the stored value.
4. WHILE a Cost_Profile form is displayed, THE Spec_App_UI SHALL state that
   the profile's values apply to every project that selected the profile.
5. WHEN the Operator asks to add a Cost_Profile, THE Spec_App_UI SHALL create
   it as a copy of a bundled preset or an existing profile under a new name,
   and SHALL NOT offer an empty profile with no provenance.
6. WHEN the Operator asks to remove a Cost_Profile that any project has
   selected, THE Spec_App_UI SHALL refuse and name the projects that select
   it, so a removal can never strand a project's profile reference.

### Requirement 4: Watch sources are creatable and editable by form

**User Story:** As an Operator whose project inspection offered no source, I
want to add and edit a Watch_Source through a form, so that connecting an
intake feed does not require hand-writing JSON that includes commands.

#### Acceptance Criteria

1. WHEN the Operator asks to add a Watch_Source, THE Spec_App_UI SHALL offer
   the Source_Presets the engine bundles, each described by what it ingests
   and the programs its commands run.
2. WHEN a Watch_Source is created from a Source_Preset, THE Spec_App_UI SHALL
   compose the entry's commands from the preset's own tables, and THE
   Form_Surface SHALL NOT offer freeform command or argument entry anywhere.
3. WHEN a Watch_Source is selected, THE Spec_App_UI SHALL render its stored
   fields as a form — name, enabled, project binding, and its per-source
   settings — with its poll command displayed read-only alongside the preset
   it came from.
4. WHILE a Watch_Source form is displayed, THE Spec_App_UI SHALL display the
   source's submitter-class autonomy grid or link directly to it, and state
   that an absent grid fails closed.
5. WHEN a Watch_Source entry whose stored shape the form cannot express is
   selected, THE Spec_App_UI SHALL say so and route the Operator to the
   JSON_View rather than rendering a partial form that would rewrite fields it
   did not show.
6. WHERE a Watch_Source is newly created and enabled, THE Spec_App_UI SHALL
   state before confirmation that the engine will begin polling it and
   ingesting its items under the autonomy the grid resolves.
7. WHEN the Operator asks to remove a Watch_Source, THE Spec_App_UI SHALL
   require a distinct confirmation naming the source, submit the removal
   through the Guarded_Write_Path, and state that the engine stops ingesting
   from it.

### Requirement 5: Every form write shows the exact patch first

**User Story:** As an Operator, I want to see precisely what a form will
write before it writes, so that a form summary can never differ from the
stored result without my knowledge.

#### Acceptance Criteria

1. WHEN the Operator asks to apply staged form changes, THE Spec_App_UI SHALL
   display the Exact_Patch_Review — the literal JSON patch that will be
   submitted — together with one plain-language sentence per changed value
   naming its old and new state.
2. WHEN the Operator confirms an Exact_Patch_Review, THE Spec_App_UI SHALL
   submit exactly the displayed patch through the Guarded_Write_Path, and the
   write SHALL appear in the durable write log.
3. FOR ALL form writes, the submitted patch's paths SHALL be limited to the
   values the Operator staged, leaving every other Config_Document value
   byte-identical.
4. IF the Guarded_Write_Path refuses a confirmed patch, THEN THE Spec_App_UI
   SHALL display the refusal's stated reason against the configuration path it
   names, and SHALL NOT present the form as saved.
5. WHEN a confirmed write succeeds, THE Spec_App_UI SHALL re-render the
   Form_Surface from a fresh configuration read rather than from the
   submitted values.
