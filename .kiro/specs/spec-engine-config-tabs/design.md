# Design Document

## Overview

The Configuration pane keeps its two-column shape — the `se-cfg` editing
column beside the `se-inspector` resolved column — and restructures the
editing column's stack into four tabs under a globally rendered projects
table. Nothing about what any surface *does* changes: the same forms, the
same staging machinery, the same guarded write path, the same review cards.
What changes is which surface is *visible*, and the two properties that make
that safe: hidden surfaces stay mounted (so staged state survives), and
pending work is announced on the tab that holds it (so hidden work is never
silently pending).

Within the Settings tab, the generated rows gain subsection structure derived
from the registry key's first segment — the same generated-not-hard-coded
rule every form on this pane already follows.

## Architecture

```
ConfigPane (holds: chosenProject, gridSource, activeTab, draft)
├── se-cfg column
│   ├── ProjectsTable                 ◄── global, above the tabs
│   ├── SectionTabs (role=tablist)    ◄── 4 fixed tabs + pending badges
│   ├── panel: Settings               ◄── SettingsForm (grouped subsections + jump nav)
│   ├── panel: Cost profiles          ◄── ProfilesForm
│   ├── panel: Watch sources          ◄── SourceForm + SourcesSection (grid)
│   └── panel: JSON view              ◄── DocumentEditor (+ problems/advisories counts on the tab)
└── se-inspector column               ◄── resolved configuration (unchanged)
```

All four panels render on every pass; inactive panels carry the `hidden`
attribute. `activeTab` replaces `jsonOpen` as the pane's visibility state;
the JSON `draft` stays lifted in ConfigPane exactly as today, for the same
reason (closing the view must not discard it).

## Components and Interfaces

- **SectionTabs** (new, in ConfigPanel.tsx): renders the tablist from a
  fixed four-entry table `SECTION_TABS` (id, label key, badge source). The
  tab set is deliberately NOT data-derived: the four entries are the pane's
  own components, and a fifth surface is a code change either way. Keyboard:
  ArrowLeft/ArrowRight move `aria-selected` per the WAI-ARIA tabs pattern
  (activation on focus movement, since panels are cheap to show). Visual
  idiom: the existing `se-filter` flat button styling — no overlay, no
  `position:absolute/fixed`.
- **Pending badges**: each form already owns its staging via
  `useStagedEdits`; the pane needs the counts. Each form gains an optional
  `onPendingCount?: (count: number) => void` prop, called from an effect
  whenever its *reviewable* staged count changes (the same count its own
  "unwritten changes" line shows; for the JSON tab the badge is the dirty
  flag `isDirty(draft, document)` plus the stored `problems`/`advisories`
  counts already computed for the toggle today). ConfigPane keeps a
  `pending: Record<TabId, number>` state. Rationale: a callback keeps the
  staging machinery untouched and the badge an observation, not a second
  store of truth.
- **Cross-tab routing**: `SourceForm.onOpenJson` already exists; ConfigPane
  now implements it as `setActiveTab('json')`. `onShowGrid` needs no change
  — the grid lives on the same Watch sources panel.
- **SettingsForm grouping**: a pure function `settingGroups(fields)`
  partitions the generated `SettingField[]` by `settingSegments`' group half
  (first dot-segment of the registry key), preserving first-appearance
  order. Rendering wraps each partition in a subsection with a heading
  (authored label via a `GROUP_LABEL_KEY` map, raw segment as detail line,
  raw-segment fallback for unmapped groups — the `SETTING_LABEL_KEY` idiom)
  and an `id` anchor. The jump navigation is an in-flow row of `se-filter`
  buttons above the rows that calls `scrollIntoView` on the subsection; it
  renders only when there is more than one subsection.
- **Unchanged**: ProjectsTable, ProfilesForm, SourceForm, SourcesSection,
  DocumentEditor, the resolved inspector, every API read and write, and all
  three forms' staging/review/refusal behavior.

### Design decisions

| Decision | Rationale |
|---|---|
| Panels hidden, never unmounted | Unmounting drops `useStagedEdits` state, armed removals, and typed text; `hidden` keeps React state and avoids refetch churn on every switch |
| Fixed four-tab table, generated settings subsections | The tabs name the pane's own components (code either way); the subsections name engine data (a new group must appear without a frontend edit) |
| Badge via callback, not lifted staging | Lifting three forms' staging into the pane is the drift the shared-hook design forbids; a count callback observes without owning |
| Grid on the Watch sources tab | The source form's enable consequence links into the grid; a cross-tab link would hide what it points at |
| Settings is the default tab | Most-visited surface; JSON stays demoted (last tab, badge carries problems) per forms-first R1 |
| Tab state is component state | The pane has no routing today; introducing URL state is scope this spec does not need |

## Data Models

- `TabId = 'settings' | 'profiles' | 'sources' | 'json'`.
- `SECTION_TABS: ReadonlyArray<{ id: TabId; labelKey: string }>` — the four
  fixed entries, in render order.
- `settingGroups(fields: SettingField[]): Array<{ group: string; fields: SettingField[] }>`
  — pure, exported, first-appearance order, total (every field lands in
  exactly one partition).
- `GROUP_LABEL_KEY: Record<string, string>` — authored labels for the
  shipped groups; lookups fall back to the raw segment.

## Correctness Properties

### Property 1: Switching tabs never loses staged state

**Validates: Requirements 1.2, 1.3**

FOR ALL sequences of tab switches interleaved with staging actions, every
staged edit, draft, and typed confirmation present before a switch is
present after it, and each tab's Pending_Badge equals the count its own
surface reports.

### Property 2: The settings subsections are exactly the registry's groups

**Validates: Requirements 2.1**

FOR ALL generated vocabularies, `settingGroups` yields one subsection per
distinct group segment in first-appearance order, every row appears under
exactly its own group, and no row is dropped or duplicated by regrouping.

## Error Handling

The tabs render only in the pane's success state; the refusal and reading
states render exactly as today with no tablist (requirement 1.7). A failed
registry or resolved read inside a panel keeps that form's existing refusal
rendering — hidden or visible, the tab badge reflects only staged counts,
never a claim about read health.

## Testing Strategy

Vitest + fast-check, extending the existing SpecEngine suites in
`website/src/test/`. Unit tests: tab rendering and semantics, badge counts,
cross-tab JSON routing, staged-state survival across switches (the named
test the mutation probe pins), grouped subsection rendering, jump-nav
behavior, authored/fallback group labels, catalog completeness ×13.
Property-based tests: Property 1 over generated switch/stage sequences
(component-level, @testing-library), Property 2 over generated vocabularies
(pure function, fast-check). Mutation probes per the standing discipline:
plant unmount-on-switch, badge-decoupling, and hard-coded-group-list
regressions; confirm named tests fail; restore byte-identical.
