# Requirements Document

## Introduction

The spec-engine operator Configuration pane became forms-first in the
spec-engine-forms-first spec: registry-generated settings rows, a cost-profile
form, a preset-constrained watch-source form, the submitter-class autonomy
grid, and the demoted JSON view now all render on one page. Live testing found
the result overloading: five dense surfaces stack in a single scroll, and an
operator looking for one control reads past everything else to find it.

This spec restructures the pane's editing surfaces into tabs — Settings, Cost
profiles, Watch sources, and JSON view — with the projects table and the
shared project selection staying global above them, and the resolved
inspector column unchanged beside them. Within the Settings tab, the
generated rows are grouped into subsections by the registry's own group
segment, with a jump navigation. Two safety properties of the existing pane
must survive the restructure unchanged: no staged-but-unwritten state may be
lost by switching tabs, and pending work on a hidden tab must stay visible so
an operator never confirms a patch carrying edits no surface on screen shows.

Judgment calls written in with defaults (veto inline): the four tab names are
the pane's own fixed surfaces and are NOT derived from data (unlike the
settings subsections, which are); the autonomy grid lives on the Watch
sources tab beside the form that links into it; the Settings tab is the
default active tab; tab state is component state, not routed in the URL.

## Glossary

- **Config_Pane**: the Configuration pane of the spec-engine operator UI —
  the left `se-cfg` column (projects table plus editing surfaces) beside the
  resolved-configuration inspector column.
- **Section_Tab**: one of the four fixed tabs the Config_Pane presents:
  Settings, Cost profiles, Watch sources, and JSON view.
- **Settings_Form**: the registry-generated typed settings editing surface.
- **Profiles_Form**: the cost-profile and role-assignment editing surface.
- **Source_Form**: the preset-constrained watch-source editing surface.
- **Autonomy_Grid**: the submitter-class autonomy matrix (SourcesSection),
  linked into by the Source_Form's enable consequence.
- **JSON_View**: the raw config.json editor, the escape hatch the
  forms-first spec demoted behind an explicit control.
- **Setting_Group**: the first dot-segment of a registry setting key (for
  example `limits`, `budget`, `watch`), the engine's own grouping.
- **Registry_Read**: the `GET /config/registry` projection of the engine's
  setting vocabulary the forms are generated from.
- **Staged_State**: any user-entered state not yet written through the
  guarded write path: staged edits in any form, the JSON draft text, a typed
  removal confirmation, and a pending add's name and repository text.
- **Pending_Badge**: the per-tab indicator carrying the count of staged
  edits held by that tab's surfaces.

## Requirements

### Requirement 1: The editing surfaces are tabs, and switching costs nothing

**User Story:** As an Operator, I want the configuration surfaces separated
into tabs with the shared context above them, so that I can work one concern
at a time without reading past every other surface — and without losing work
I staged on another tab.

#### Acceptance Criteria

1. WHEN the Config_Pane renders with a readable configuration, THE
   Config_Pane SHALL present exactly four Section_Tabs — Settings, Cost
   profiles, Watch sources, and JSON view — with the projects table and the
   shared project selection rendered above the tabs and governing every tab,
   and the resolved inspector column unchanged beside the pane.
2. WHEN the Operator switches Section_Tabs, THE Config_Pane SHALL preserve
   all Staged_State unchanged, and FOR ALL Section_Tabs the tab's surfaces
   SHALL remain mounted while hidden so no form, query, or draft re-derives
   from scratch on return.
3. WHILE any surface holds staged edits, THE Config_Pane SHALL show a
   Pending_Badge with the staged-edit count on that surface's Section_Tab,
   visible whichever tab is active.
4. WHEN the stored document carries validation problems or advisories, THE
   Config_Pane SHALL carry their counts on the JSON view tab label, as the
   demoted toggle carries them today.
5. WHEN the Source_Form routes the Operator to the JSON_View (a
   not-expressible source), THE Config_Pane SHALL activate the JSON view tab
   rather than rendering the editor beneath the source form, and the Watch
   sources tab SHALL contain the Autonomy_Grid beside the Source_Form so the
   form's grid link never crosses tabs.
6. WHEN the tabs render, THE Config_Pane SHALL expose them with tab-list
   semantics — `role="tablist"`, `role="tab"` with `aria-selected`, panels
   labelled by their tabs, and arrow-key movement between tabs — using the
   pane's existing flat button styling with no overlay, popup, or floating
   positioning.
7. WHERE the configuration is unreadable or not yet read, THE Config_Pane
   SHALL render the existing refusal or reading state without tabs, and the
   first-run Setup Assistant landing behavior SHALL be unchanged.

### Requirement 2: The Settings tab is grouped by the registry's own groups

**User Story:** As an Operator, I want the settings rows organized under the
engine's own group headings with a way to jump between them, so that a long
generated list reads as a small number of named concerns.

#### Acceptance Criteria

1. WHEN the Settings tab renders a non-empty vocabulary, THE Settings_Form
   SHALL group its rows into subsections keyed by Setting_Group, deriving
   the subsection set and order from the Registry_Read alone — first
   appearance order, no hard-coded group list — and FOR ALL vocabularies
   the set of subsections SHALL equal the set of distinct Setting_Groups
   and every generated row SHALL appear under exactly its own group.
2. WHEN a Setting_Group has an authored human label in the catalogs, THE
   Settings_Form SHALL head its subsection with that label and show the raw
   group segment as the detail line; a group with no authored label SHALL
   render its raw segment rather than being dropped.
3. WHILE the Settings tab shows more than one subsection, THE Settings_Form
   SHALL offer an in-flow jump navigation naming each subsection, scrolling
   to the subsection on activation, with no floating or sticky positioning.
4. WHEN rows regroup, THE Settings_Form SHALL leave the write machinery
   unchanged: scope offering, staging, reconciliation, the review card, and
   refusal retention behave exactly as before regrouping, and all existing
   named tests for those behaviors continue to pass unmodified except for
   structural selectors.
5. FOR ALL new operator-facing strings introduced by this spec (tab labels,
   group labels, jump-navigation copy), the strings SHALL ship translated in
   all 13 catalogs with the pseudolocale regenerated.
