# Design Document

## Overview

This design closes three live-test gaps in the Spec_Engine operator surface:
first-run landing and orientation, directory picking, and a projects surface
over the already-multi-project configuration model. Everything is frontend
presentation and flow inside `website/src/apps/spec-engine/`; the backend
routes, the engine's resolution semantics, and the approver-gated setup flow
are used as they shipped. No new backend route is added and no existing route
changes shape.

Grounding facts this design is built on (verified in source):

1. `SpecEnginePage.tsx` already computes a first-run flag and lands on
   `'setup'` during first-run (`chosenPane ?? (config.isPending ? null :
   firstRun ? 'setup' : 'queue')`). The backend's `configured` field is set
   from `store.path.is_file()` alone, so it cannot express the requirement's
   definition (a file created by one app-scoped save still configures no
   project) — first-run derives from `document.projects` being empty, which
   covers the absent-file arm trivially. The nav rail, however, always lists
   Queue, Configuration, Setup in that order, and the setup pane opens on the
   bare path field with guard-rail copy only.
2. `ConfigStore._merge` deletes a key when a patch value is `null`
   ("A ``None`` value removes its key"), so removing a `projects.<name>` entry
   is an ordinary guarded `PUT /config` with `{"projects": {"<name>": null}}`
   — recorded in the config write log like every accepted write.
3. `GET /config/resolved?project=<name>&source=<s>` already returns every
   setting's value-in-force with its origin scope, per project.
4. Setup apply writes `patch[SECTION_PROJECTS] = {plan.project: project_entry}`
   — one merge patch per project; other entries are untouched by construction.
5. The dashboard owns a shared `ProjectPicker` component
   (`website/src/components/ProjectPicker.tsx`: `open`, `onOpenChange`,
   `anchorRef`, `onSelect`) backed by `api.browseDirs()` and
   `api.recentProjects()`; `FolderConfigModal` documents that pickers are
   "reused rather than reimplemented".
6. The `POST /setup/inspect` reply carries the derived project name, which is
   the key the projects section is stored under — the UI never derives keys
   from paths itself.

## Architecture

Pane and landing flow (the only shell-level change is nav ordering plus a
retained-data guard; the landing rule already exists):

```
config read (GET /config)
  ├─ pending  → work area held, no pane claimed (existing behavior, kept)
  ├─ error    → firstRun forced FALSE (guard), landing 'queue',
  │             read failure stated — doubt never reads as "not configured"
  └─ ok
      ├─ document.projects holds NO entry → firstRun
      │     (an absent file trivially holds none — `configured` is only "the
      │      file exists" and is read by nothing here)
      │     landing pane: setup   nav order: Setup, Queue, Configuration
      │     setup pane leads with the orientation block
      └─ document.projects holds at least one entry
            landing pane: queue   nav order: Queue, Configuration, Setup
```

Projects surface data flow (Configuration pane):

```
GET /config          → document.projects → projects table (one row per entry)
row selected         → GET /config/resolved?project=<name>
                        → docked resolved view (value + origin per setting)
row "remove" armed   → PUT /config {"projects": {"<name>": null}}  (operator-
                        guarded; write log records actor + touched path)
setup apply succeeds → invalidate the config query → table shows the new entry
```

Both flows follow the surface's established mockup-b language: one ordered
table on the left, a docked detail pane on the right, no overlays, and the
kill-switch strip remains a grid row of the shell in every state. The
ProjectPicker is an anchored popover portal; it does not scrim the page and
must not cover the strip.

## Components and Interfaces

### SpecEnginePage.tsx (shell)

- **Nav ordering**: the rail renders its three buttons from an ordered list
  derived from `firstRun` — `['setup', 'queue', 'config']` while first-run,
  `['queue', 'config', 'setup']` otherwise — instead of a hardcoded order.
  The existing `data-alarm` marker on the setup button is kept.
- **Retained-data guard and the widened derivation**: `firstRun` is a
  successful read (`!config.isError`, data present) whose `document.projects`
  holds zero entries. React Query retains the last data across a failed
  refetch, so without the guard a projectless snapshot plus a later failed
  read would keep claiming first-run. This is the same defect class the
  kill-switch dot fix closed; the guard is applied at the single derivation
  both the landing rule and the nav order consume, so the two cannot disagree.
- **Failure statement**: when the config read is in error and no pane was
  chosen, the shell lands on queue; the config-read failure is stated on the
  Configuration pane (through `ConfigPane`'s error prop — the surface where
  the config query renders) and the setup nav button keeps its alarm off (an
  alarm asserts "unconfigured", which is not known). No new banner is
  introduced on the queue pane — the strip and panes already own their own
  failure statements, and a second global banner would duplicate them.

### SetupFlowPanel.tsx (orientation, picker, re-entry)

- **Orientation block**: rendered at the top of the setup pane while
  `firstRun`. Content: one short paragraph naming what the Spec_Engine does
  (drives spec-driven development runs against a project, with the operator
  reviewing rather than hand-authoring), one naming what setup produces (a
  reviewed, approver-gated configuration entry for the project), and a lead-in
  that names "Inspect the project" as the first action. The four step labels
  each gain a one-line operator-verb description (what you do, what you get)
  alongside the existing guard-rail sentence. When a step is unreachable, the
  step list already gates progression; the copy states which prior step must
  complete (reusing the step names, interpolated — no trailing-connector
  strings).
- **Orientation visibility**: once at least one project exists the orientation
  collapses to nothing (the pane's normal flow starts at the path field); the
  orientation is not repeated on other panes, preserving the existing "said on
  the setup pane rather than repeated" rule.
- **ProjectPicker adoption**: a Browse button next to the path field anchors
  the shared `ProjectPicker` (same props as `FolderConfigModal`'s usage);
  `onSelect` fills the path field with the absolute path and closes the
  picker. Manual typing stays live. If `browseDirs` fails, the picker's own
  error surface states it and the path field is untouched — typing remains the
  fallback (fail-open to manual entry, fail-closed on nothing).
- **Duplicate detection (R4.3)**: the inspect reply's project name is compared
  against the keys of `document.projects` from the already-loaded config
  query. On a match, the panel states the project is already configured and
  frames continuing as re-inspection of the existing entry; it never creates a
  second entry because the entry key IS the derived name — the statement makes
  the merge-overwrite behavior visible instead of silent.
- **List refresh (R4.2)**: apply success invalidates the config query (the
  panel already invalidates the queue on kill-switch actions — same pattern),
  so the projects table and the first-run derivation update without a reload.

### ConfigPanel.tsx (projects surface)

- **Projects table**: a section listing one row per `document.projects` entry
  (name, selected cost profile if pinned, override count), plus a fixed "App
  defaults" row representing resolution with no project. Rows follow the
  queue table's keyboard-first conventions.
- **Docked resolved view**: selecting a row fetches
  `GET /config/resolved?project=<name>` (the App defaults row omits the
  parameter) and renders each setting's value with its origin scope
  (per-source, per-project, profile, app default) — the same payload the
  route already serves. A failed read states the failure; it never renders
  app defaults in place of an unreadable per-project resolution and never
  renders retained values as current (query keyed per project name; the view
  branches on `isError` before data).
- **Removal**: each project row offers an arm-then-confirm removal (the
  SafetyPanel's two-step pattern, not a browser confirm). Confirm submits
  `PUT /config` with `{"projects": {"<name>": null}}` through the existing
  guarded client. The reply is the merged document; the table re-renders from
  it, and the write log carries the write (engine behavior, verified by an
  existing-route test).

### api.ts (spec-engine client)

- Adds typed wrappers only where a call shape is new to this app: resolved
  config with a `project` parameter (the route exists; the current client may
  call it bare) and nothing else. Browse/recent calls belong to the shared
  dashboard client and are used by ProjectPicker internally — the spec-engine
  client does not wrap them.

### Localization

All new strings ship under `apps.specEngine.*` namespaces in `en.json` and
every supported catalog (the app's established 13-catalog set plus pseudo via
`npm run i18n:pseudo`), with interpolated variables rather than concatenated
fragments, and no digit formatting applied to identifiers.

## Data Models

- **Project_Entry** (existing, engine-owned): `document.projects.<name>` — an
  object that may carry a selected cost profile field and per-project setting
  groups. The UI treats it as opaque except for: its key (the display name and
  removal target) and the presence of known summary fields.
- **Resolved settings payload** (existing): `{ project, source, settings: [
  { key, value, origin } ... ] }` as served by `GET /config/resolved` — the UI
  renders it verbatim and adds no client-side resolution.
- **Removal patch** (existing semantics): `{"projects": {"<name>": null}}` —
  `null` deletes the key at merge; sibling entries are structurally untouched.

Design decisions and rationale:

| Decision | Rationale |
|---|---|
| Projects surface lives inside the Configuration pane, not a fourth nav pane | It is a view of the Config_Document; mockup-b's table + docked-detail split fits it; avoids nav growth and a second place config truth appears |
| Removal via `null` merge patch on the existing PUT | `_merge` already defines deletion; reusing the single write path keeps the write log, validation, and guard identical to every other write — no new route to review |
| Nav order derived from the same `firstRun` value as the landing rule | One derivation consumed by both prevents the landing and the rail from disagreeing (the class of two-rules-one-flag bugs the review gate caught in 6.4) |
| Duplicate detection compares the inspect reply's project name to document keys | The engine owns name derivation; the UI comparing derived-name-to-stored-key can neither false-positive on path spelling nor invent its own normalization |
| No new mockup round | The projects table + docked resolved view and the orientation block extend the recorded mockup-b design language (single ordered table, docked inspector, no overlays); the visual spec remains mockup-b as corrected in design/selection.md, and this decision is recorded here for the owner's veto alongside the still-pending mockup-b selection |
| ProjectPicker reused as-is | The dashboard convention (documented at its other call sites) is one shared picker; a reimplementation would fork keyboard and recents behavior |

## Error Handling

The surface's doubt discipline applies to every new read and is the review
gate's standing concern:

- **Config read fails** → `firstRun` is false by the guard; no first-run
  landing, no orientation, no setup alarm; the failure is stated where the
  query renders. Retained `configured===false` data under `isError` must not
  re-assert first-run.
- **Resolved read fails** → the docked view states the failure for that
  project; it renders neither app defaults nor retained values as current.
- **Directory browse fails** → stated in the picker; manual entry unaffected.
- **Removal refused** (engine validation, guard, or lock) → the refusal's code
  and message render in the arm panel; the table keeps the entry (the reply
  document is the truth, and a refused write returns none).
- **Inspect of an already-configured project** → not an error: a stated
  condition with a re-inspection framing.

## Testing Strategy

Unit tests only (no Correctness Properties section: this is UI presentation
and existing-route CRUD — resolution and merge algorithms are engine code with
their own suites). Framework: vitest + Testing Library in
`website/src/test/`, following the six existing SpecEngine suites.

- **Shell** (`SpecEngineShell.test.tsx`): nav order in both states; landing
  pane in both states; retained-data guard (configured===false data + failed
  refetch → no first-run claim, no alarm); pending holds the work area
  (existing test, kept).
- **Setup** (`SpecEngineSetup.test.tsx`): orientation present on first-run and
  absent once configured; step descriptions render; picker opens, selection
  fills the field, browse failure states itself with manual entry alive;
  duplicate inspect states re-inspection; apply invalidates the config query.
- **Config** (`SpecEngineConfig.test.tsx`): projects table rows from document;
  App defaults row; per-project resolved fetch keyed by name with origin
  rendering; resolved failure statement; removal arm/confirm sends the null
  patch and re-renders from the reply; removal refusal renders.
- **Mutation discipline**: every claimed guard (firstRun isError guard, nav
  order derivation, resolved-view isError branch, null-patch shape) gets a
  revert-mutation probe — commit first, plant the regression, name the failing
  test, restore byte-identical — before review dispatch.
- **Gates**: the six SpecEngine vitest suites, `npx tsc -b`, eslint on touched
  files, `npm run i18n:check` (catalog completeness, DNT, manifest-sync),
  `node scripts/check-app-manifest-sync.mjs`, and the app-boundary fence
  pytest (no out-of-territory file is modified; ProjectPicker is imported, not
  edited). Backend pytest suites run as regression confirmation; no backend
  source change is expected.
