# Requirements Document

## Introduction

The Spec Engine's config model already encodes the distinction between work
originated by the repository's own maintainer and work arriving from the wild:
each Watch_Source carries an Autonomy_Grid mapping a Submitter_Class and a spec
type to an autonomy level, resolved class-first with wildcards, failing closed
to the least-trusted class when a submitter cannot be classified. The gates read
that grid — a granted `execution` rung lets the Autonomy_Policy approve document
gates unattended (the maintainer's ungated flow), while an absent or
authoring-only cell parks the run for a human (the external submitter's gated
flow). Intake screening additionally caps a quarantined item to authoring
regardless of the grid.

None of that is visible or editable in the operator UI today. The grid is
reachable only by hand-editing the raw configuration JSON, which means the
central trust decision of open-source intake — who may run how unattended — is
configured blind. This feature surfaces the Autonomy_Grid in the Spec_App_UI's
Configuration pane: a read surface showing every resolved cell with its origin,
comprehension copy stating the fail-closed semantics, and a guarded edit path
that shows the exact change before anything is written.

Out of scope, deliberately: creating or removing Watch_Sources (that remains
the Setup_Assistant's offer flow); the per-class screening opt-out and echo
permission maps (adjacent per-class settings with their own validation rules —
a separate surface); any change to resolution semantics, gate coverage, or
screening behavior. The established surface invariants bind: no overlay may
cover the kill-switch strip, all new strings ship in all localization catalogs,
and out-of-territory files require boundary-fence allowlisting.

## Glossary

- **Spec_Engine**: the engine backing the spec-engine builtin app — config
  store, autonomy policy, gates, watch intake, screening.
- **Spec_App_UI**: the spec-engine app's operator page in the dashboard, whose
  Configuration pane this feature extends.
- **Config_Document**: the engine's single JSON configuration document, written
  only through the Guarded_Write_Path.
- **Watch_Source**: a named entry under the Config_Document's `sources` section
  from which the engine ingests work items.
- **Submitter_Class**: the trust classification of an item's author —
  `maintainer`, `member`, `contributor`, or `external` — where `external` is
  the least-trusted class and the fail-closed default for an author that
  cannot be classified.
- **Autonomy_Grid**: the per-source mapping at `sources.<name>.autonomy` from
  Submitter_Class and spec type (either may be a wildcard) to an autonomy
  level, resolved class-first, most specific cell winning.
- **Resolved_Cell**: one (Submitter_Class, spec type) pair's effective autonomy
  level together with the origin that answered it — an exact stored cell, a
  wildcard cell, or the unconfigured default.
- **Unconfigured_Default**: the level a Resolved_Cell takes when no stored cell
  answers it — authoring-only, which covers no gate, so the run waits for a
  human.
- **Guarded_Write_Path**: the engine's single fenced configuration write door —
  operator-authenticated, schema-validated, recorded in the durable write log.
- **Screening_Quarantine**: intake screening's cap forcing a flagged item to
  the authoring rung regardless of the Autonomy_Grid.
- **Operator**: the human using the dashboard page.

## Requirements

### Requirement 1: The grid is visible, cell by cell, with origins

**User Story:** As an Operator preparing a project for open-source intake, I
want to see every Watch_Source's Autonomy_Grid fully resolved, so that I can
verify who may run how unattended without reading raw JSON.

#### Acceptance Criteria

1. WHEN the Operator opens the Configuration pane's sources view, THE
   Spec_App_UI SHALL list every Watch_Source present in the Config_Document.
2. WHEN a Watch_Source is selected, THE Spec_App_UI SHALL display its
   Autonomy_Grid as the full matrix of every Submitter_Class against every
   spec type, with a Resolved_Cell for each pair.
3. WHERE a Resolved_Cell was answered by a stored cell, THE Spec_App_UI SHALL
   display the resolved level together with the configuration path of the
   stored cell that answered it, distinguishing an exact cell from a wildcard
   cell.
4. WHERE no stored cell answers a pair, THE Spec_App_UI SHALL display the
   Unconfigured_Default and state that this cell waits for a human rather than
   displaying an empty or zero value.
5. IF the configuration read behind the sources view fails, THEN THE
   Spec_App_UI SHALL state the failure and SHALL NOT render grid values from
   retained or default data.
6. WHERE the Config_Document contains no Watch_Source, THE Spec_App_UI SHALL
   state that no sources are configured and name the Setup_Assistant's offer
   flow as where a source comes from, rather than rendering an empty matrix.

### Requirement 2: The semantics are stated where the values are read

**User Story:** As an Operator reading the grid, I want the fail-closed rules
stated beside the values, so that I do not have to infer what an absent cell or
an unclassifiable submitter means.

#### Acceptance Criteria

1. WHILE the sources view is displayed, THE Spec_App_UI SHALL state that an
   author who cannot be classified resolves to the least-trusted
   Submitter_Class.
2. WHILE the sources view is displayed, THE Spec_App_UI SHALL state that an
   autonomy level authorizes every level below it.
3. WHILE the sources view is displayed, THE Spec_App_UI SHALL state that
   Screening_Quarantine caps a flagged item to authoring regardless of the
   grid, and that the cap only ever lowers authority.
4. WHERE a Resolved_Cell grants `execution` or above, THE Spec_App_UI SHALL
   indicate that document gates for matching items are approved by the
   Autonomy_Policy without a human.

### Requirement 3: Editing is explicit, guarded, and shown before it happens

**User Story:** As an Operator, I want to change a grid cell from the UI with
the exact consequence shown before anything is written, so that raising or
lowering a class's authority is a deliberate act rather than a JSON edit.

#### Acceptance Criteria

1. WHEN the Operator changes one or more Resolved_Cells and asks to apply, THE
   Spec_App_UI SHALL display the exact configuration change that would be
   written — the stored cells created or replaced, at their configuration
   paths — before any write occurs.
2. WHEN the Operator confirms a displayed change, THE Spec_App_UI SHALL submit
   it through the Guarded_Write_Path, and the write SHALL appear in the
   durable write log.
3. WHERE a pending change raises the resolved level of the least-trusted
   Submitter_Class for any spec type, THE Spec_App_UI SHALL state that
   consequence plainly in the displayed change before confirmation.
4. IF the Guarded_Write_Path refuses a change, THEN THE Spec_App_UI SHALL
   display the refusal's stated reason against the configuration path the
   refusal names, and THE Spec_App_UI SHALL continue to display the grid's
   stored state rather than the submitted values.
5. WHEN an edit targets a pair whose Resolved_Cell was answered by a wildcard
   cell, THE Spec_App_UI SHALL write a more specific stored cell for that pair
   and SHALL NOT modify the wildcard cell, and the displayed change SHALL say
   so.
6. WHEN a confirmed change is applied, THE Spec_App_UI SHALL refresh the
   displayed grid from a fresh configuration read rather than from the
   submitted values.

### Requirement 4: An edit touches nothing but its own cells

**User Story:** As an Operator running several projects and sources, I want a
grid edit on one source to be provably isolated, so that tightening one
source's policy cannot loosen another's.

#### Acceptance Criteria

1. FOR ALL pairs of distinct Watch_Sources, applying a grid change to one
   SHALL leave the other's stored Autonomy_Grid and every one of its
   Resolved_Cells unchanged.
2. FOR ALL grid changes, every Config_Document setting outside the edited
   source's Autonomy_Grid SHALL be byte-identical before and after the write.
3. WHEN a grid change is applied while another view of the same configuration
   is open, THE Spec_App_UI SHALL reflect the change in the projects table's
   resolved settings on their next read rather than showing the two views in
   disagreement.
