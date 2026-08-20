# Implementation Plan: Spec Engine Projects Surface

## Overview

Five serial tasks closing the three live-test findings on the Spec_Engine
operator surface: first-run landing/orientation, ProjectPicker adoption, and a
projects surface over the existing multi-project config model. Frontend-only —
no backend route is added or changed; removal rides the existing guarded PUT's
`null`-deletes-a-key merge semantics.

## Notes

- Branch `feat/spec-engine-poc` in this worktree; stage files BY NAME, never
  `-A`; `.tasks-meta.json` stays unstaged.
- Every claimed guard gets a revert-mutation probe (commit first, plant the
  regression, name the failing test, restore byte-identical) before review.
- `ProjectPicker` and every other file outside `website/src/apps/spec-engine/`
  and the shared catalogs is import-only; an out-of-territory modification
  needs a reviewed App_Boundary_Fence allowlist entry.
- All new strings: interpolated variables, no trailing-connector fragments, no
  digit formatting on identifiers, every supported catalog plus pseudo.

## Tasks

- [x] 1. Shell landing and navigation
  - [x] 1.1 First-run nav ordering and the retained-data guard
    - Derive `firstRun` in `SpecEnginePage.tsx` from the requirement's own definition — a successful config read whose `document.projects` holds ZERO entries (`configured` alone says only that the file exists; a document created by an app-scoped save still configures no project) — guarded by `!config.isError`; one derivation consumed by BOTH the landing rule and the nav order, so the two cannot disagree
    - Render the nav rail from an ordered pane list: Setup, Queue, Configuration while first-run; Queue, Configuration, Setup otherwise; keep the existing `data-alarm` marker on the setup button, off whenever the read is in error
    - Keep the existing landing rule (`chosenPane ?? (pending ? null : firstRun ? 'setup' : 'queue')`) and the pending hold; a failed config read lands on queue with no first-run claim and the failure stated where the config query renders
    - Shell tests: nav order in both states, landing in both states, retained `configured===false` data under a failed refetch claims neither first-run landing nor alarm; mutation probes on the guard and the order derivation
    - _Requirements: 1.1, 1.2, 1.5, 1.8_

- [ ] 2. Setup pane
  - [x] 2.1 Orientation block and project picker
    - Orientation block at the top of `SetupFlowPanel.tsx` while first-run: what the Spec_Engine does, what completing setup produces, inspect named as the first action; one operator-verb description per setup step alongside the existing guard-rail copy; collapses once a project exists; unreachable steps state which prior step must complete, interpolated (no trailing-connector strings)
    - Browse button anchoring the shared `ProjectPicker` (`website/src/components/ProjectPicker.tsx`, same props as `FolderConfigModal`'s usage — import it, never modify it); selection fills the path field with the absolute path; manual entry stays live; a failed directory browse states itself and leaves manual entry usable; the picker popover must not cover the kill-switch strip
    - All new strings through `en.json` and every supported catalog plus pseudo; setup tests for orientation presence/absence, step descriptions, picker fill, and browse failure
    - _Requirements: 1.3, 1.4, 1.6, 1.7, 2.1, 2.2, 2.3, 2.4, 2.5_
  - [ ] 2.2 Repeatable setup and duplicate detection
    - Setup pane fully usable when projects already exist (not first-run-only); apply success invalidates the config query so the projects table and first-run derivation update without a reload
    - The pane's `<h1>` currently renders "Nothing is configured yet" unconditionally (carried from 2.1's round-3 review): give the configured state its own honest heading — the panel already receives `firstRun` — with the new string in every catalog
    - Compare the inspect reply's derived project name against `document.projects` keys from the already-loaded config query; on a match, state the project is already configured and frame continuing as re-inspection of the existing entry — the UI derives no names from paths itself
    - Assert the apply patch shape touches only the new project's entry (prior entries unchanged through the merge) and that the named-approver gate is exercised identically for an additional project
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5_

- [x] 3. Projects surface
  - [x] 3.1 Projects table, per-project resolved view, and removal
    - Projects section in `ConfigPanel.tsx`: one row per `document.projects` entry (name, pinned cost profile, override count) plus an App defaults row; queue-table keyboard conventions; no overlays
    - Selecting a row fetches `GET /config/resolved?project=<name>` (App defaults row omits the parameter), rendering each setting's value with its origin scope; queries keyed per project name; a failed resolved read states the failure and renders neither app defaults nor retained values as current (branch on `isError` before data)
    - Arm-then-confirm removal per row (SafetyPanel's two-step pattern); confirm submits `PUT /config` with `{"projects": {"<name>": null}}` through the existing guarded client; table re-renders from the reply document; a refused write renders the refusal and keeps the entry
    - Verify BY NAME the engine tests covering `_merge` null-deletion, sibling-entry independence, and write-log recording; pin the UI's patch shape with a test that fails if the null is dropped or the patch widens; mutation probes on the isError branch and the patch shape
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6_

- [ ] 4. Verification
  - [ ] 4.1 Gate sweep and record
    - All six SpecEngine vitest suites, `npx tsc -b`, eslint on touched files, `npm run i18n:check`, `node scripts/check-app-manifest-sync.mjs`, app-boundary fence pytest (no out-of-territory file modified), and both backend pytest suites as regression — real exit codes, never `$?` through a pipe, no quiet flags
    - Confirm every new string exists in all supported catalogs; record any dispositions in this tasks.md; leave `.tasks-meta.json` unstaged
    - _Requirements: 1.6, 2.3, 3.4_

## Task Dependency Graph

```json
{"waves": [
  {"id": 0, "tasks": ["1.1"]},
  {"id": 1, "tasks": ["2.1"]},
  {"id": 2, "tasks": ["3.1"]},
  {"id": 3, "tasks": ["2.2"]},
  {"id": 4, "tasks": ["4.1"]}
]}
```

Waves are serial: every task edits the shared localization catalogs, and 2.2's
list-refresh behavior is only testable once 3.1's projects table exists.

### Findings carried forward by the review gate

- **Disposition (from 3.1's round-2 review, approved):** the render-time half of
  the selection normalization in `ConfigPanel.tsx` is deliberately unpinned —
  the collapse effect alone satisfies the named test, and the render guard only
  closes the one-frame window between commit and effect. Recorded rather than
  tested: a test distinguishing the two halves would have to observe a single
  React frame, which jsdom render cycles do not expose meaningfully.

- **For 4.1 (inherited i18n:check failure, from 2.1's rounds 2 and 4):** `npm run
  i18n:check` exits 1 on exactly one finding — `pages.artifactDeployPage.domain`
  ("domain —", trailing-connector), introduced by the i18n rollout commit
  `c1a691238` long before this branch, in a page outside spec-engine territory.
  The prior spec dispositioned the same key as inherited (proven at a merge-base
  worktree). 4.1 must record it as inherited, not treat the exit 1 as its own
  gate failure; `changed-values` scoped to this branch's additions passes 0.

- **For 4.1 (from 1.1's round-2 review):** `check-i18n-keys.mjs --report` lists
  `SpecEnginePage.tsx` at the pre-existing FILTERS destructure
  (`i18nT(labelKey)` in the filter row render) — present before this spec's
  work, report-only, and out of 1.1's scope. Disposition it: convert to a flat
  call-site-indexed map like `PANE_LABEL_KEY`, or record as accepted. Also
  note: 1.1's round-2 report wrongly claimed the file left the report entirely;
  the pane-label site is resolved, the FILTERS site remains.
