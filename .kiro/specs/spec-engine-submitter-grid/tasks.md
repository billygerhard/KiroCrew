# Implementation Plan: Surfacing the submitter-class autonomy grid

## Overview

Four serial waves on branch `feat/spec-engine-poc`: the backend read surface
first (the route contract everything downstream consumes), then the read-only
view, then guarded editing, then the full gate sweep. Serial because each wave
consumes the previous one's contract; the review gate runs between waves.

## Notes

- Writes reuse the existing `PUT /config` door — no new write endpoint
  anywhere in this plan.
- Both correctness properties require executed mutation probes before their
  reviews are dispatched, per the project's standing discipline.
- Every new operator-facing string ships in all 13 catalogs in the same task
  that introduces it, not in the sweep.
- **Carried from 3.1's review (for 4.1, disposition):** a pending edit whose
  (class, type) pair the refreshed payload no longer resolves — source still
  present — stays in local edits with its "Not written" mark but is excluded
  from review and patch; clearable only by discard. Near-unreachable (the
  route ships full matrices) and degrades visibly rather than silently.
  **Disposition (4.1): fixed, not excused.** The reconciliation that already
  dropped a choice whose *source* had left the document now drops any choice
  the current answer cannot resolve at all, so "shown as pending" is once
  again the same set as "in the review" and "in the patch". Named test:
  `SpecEngineSources.test.tsx` › "lets go of a choice the refreshed answer no
  longer resolves"; probed (revert to the source-only filter → that test alone
  fails → restored byte-identical).
- **Recorded deviation (3.1, reviewer-verified):** the design's "per-cell
  level selects" became a cell pick + shared in-flow level control: the repo
  bans native `<select>` (eslint error) and the mandated Radix replacements
  portal popups, which would violate the app's tested no-overlay invariant.
  Queue-then-review semantics preserved exactly.
- **Carried from 1.1's review (for 2.1):** TS types come from the actual
  payload — `declared_at` is `""` when unconfigured (never `null`), the
  wildcard key in paths is the literal `default`; branch on `origin`, not on
  `declared_at` emptiness alone. Design's payload sketch has been corrected.
- **Carried from 1.1's review (for 2.1, decide):** a hand-edited grid row
  keyed outside the class vocabulary renders as all-default with no advisory
  (faithful to gate behavior; the write door refuses such documents).
  **Disposition (2.1, reviewed and approved):** the sources view does NOT
  duplicate the config read's validation errors beside the grid — they
  already render in this same pane with the document editor, and a grid the
  resolver cannot read arrives as the route's own refusal. Instead the
  section carries one sentence stating that an unrecognized stored row
  answers no cell and shows as not configured, with document problems
  reported beside the document.

## Tasks

- [x] 1. Backend read surface
  - [x] 1.1 Sources route with resolved matrices and origins
    - Add `GET {PREFIX}/config/sources` to `backend/routes.py` through the `_read` guard, module posture note updated for the new read.
    - Compute each source's full matrix via `AutonomyPolicy.decision()` per (class, type) pair against the engine's reader; include sources with no `autonomy` field (all-default matrices).
    - Derive per-cell `origin` (`exact` / `wildcard` / `default`) from `declared_at` versus the queried pair, and `policy_covers_gates` from `decision.permits(EXECUTION)`.
    - Ship `submitter_classes`, `spec_types`, and `levels` vocabularies from `config/schema.py` constants in the payload.
    - Return the refusal-by-path shape for a malformed stored grid (`ConfigValidationError`), matching the resolved-config route's error contract.
    - Route tests: stored exact cell, wildcard row, empty grid, absent autonomy field, no sources, 401 floor, malformed-grid refusal, origin unit tests.
    - Hypothesis property: origin classification is total and faithful against the real resolver (Property 2).
    - Hypothesis property: merging a minimal cell patch through the real `_merge` leaves every other path identical (Python half of Property 1).
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 2.4, 4.1, 4.2_

- [x] 2. Read-only sources view
  - [x] 2.1 Sources section rendering the grid with origins and semantics
    - Add the Sources section to the Configuration pane: source list, per-source matrix (classes × spec types in schema order), per-cell level + origin annotation + policy-covers-gates marker.
    - Default-answered cells render the Unconfigured_Default wording ("waits for a human"), never blank or zero.
    - Failed read forces the stated-failure state with no values rendered from retained or default data; loading state is distinct from empty.
    - Empty-sources state names the Setup Assistant's offer flow as where a source comes from.
    - Semantics copy in the section: unclassifiable authors resolve to the least-trusted class; a level authorizes every level below it; screening quarantine caps to authoring regardless of the grid and only lowers.
    - New `QK.sources` query key; api.ts types (`SourcesPayload`, `SourceGridCell`) mirroring the route.
    - All new strings in all 13 catalogs; pseudolocale regenerated.
    - Vitest: matrix rendering with all three origins, default wording, failed-read doubt state, empty state, semantics copy presence, vocabulary-driven axes.
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 2.1, 2.2, 2.3, 2.4_

- [x] 3. Guarded editing
  - [x] 3.1 Cell edits with exact-patch review through the guarded write
    - Per-cell level selects accumulating `PendingEdit`s in local state; nothing written before review.
    - `buildGridPatch` as a pure function producing the minimal nested patch; fast-check property that its only paths are the edited cells (TS half of Property 1).
    - Review card: the exact pretty-printed patch, one sentence per edit (cell, old level and origin, new level), the external-raise warning when an edit raises the least-trusted class's resolved level, the narrowing note when the edited cell was wildcard-answered (the wildcard is never modified).
    - Confirm submits through the existing `writeConfig` client; refusals render verbatim by path with the grid still showing stored state (no query invalidation on failure).
    - Success invalidates sources, config, and resolved queries; the grid re-renders from a fresh read, and the projects table's resolved settings agree on their next read.
    - Vitest: edit flow end to end, exact patch shown, external warning, wildcard narrowing note, refusal retention, fresh-read refresh; strings in all catalogs.
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 4.3_

- [x] 4. Gate sweep
  - [x] 4.1 Full verification sweep and dispositions
    - Run all gates: spec_engine pytest, spec_builder pytest, isort/flake8/mypy on touched trees, tsc, eslint, full vitest, manifest-sync, boundary fence, i18n:check, key-refs.
    - Confirm both correctness properties have executed mutation probes recorded (revert, named test fails, restore byte-identical).
    - Disposition every carried finding from tasks 1.1–3.1 in this file; verify catalogs complete for every new string.
    - _Requirements: 1.5, 3.4, 4.1, 4.2, 4.3_

## Verification record (4.1)

Swept 2026-08-21 on `feat/spec-engine-poc` at `5b179b3a3` + this task's changes.
Pre-spec base for every attribution below: `081f1ea0c` (the commit before
`4a2825ffd` authored this spec). Exit codes captured unpiped.

### Gates

| Gate | Command | Exit | Result |
| --- | --- | --- | --- |
| spec_engine pytest | `pytest .../spec_engine/tests -q` | 0 | 3407 passed |
| app-boundary fence | `pytest src/kiro_crew/apps/builtins/spec_engine/tests/test_app_boundary_fence.py -q` | 0 | 38 passed |
| spec_builder pytest | `pytest .../spec_builder/tests -q` | 0 | 269 passed |
| isort | `isort --check-only .../spec_engine` | 0 | clean |
| flake8 | `flake8 .../spec_engine` | 0 | clean |
| mypy | `mypy .../spec_engine` | 0 | 205 files, no issues |
| black | `black --check .../spec_engine` | 1 | **inherited**, see below |
| tsc | `npx tsc -b` | 0 | clean |
| eslint | `npx eslint src/apps/spec-engine + touched tests` | 0 | 0 errors, 8 warnings (all in files this spec never touched) |
| vitest (full) | `npx vitest run` | 1 | 12021 passed, 1 failed — **inherited**, see below; no unhandled errors |
| i18n:check, scoped | `I18N_BASE_REF=081f1ea0c node scripts/i18n-check.mjs` | 0 | 13/13 PASS |
| i18n:check, whole-repo | `node scripts/i18n-check.mjs` | 1 | one finding, **inherited**, see below |
| key-refs | inside i18n:check | pass | 11429 references resolve, 0 dangling |
| manifest-sync | inside i18n:check | pass | 20 manifests, 171 strings in sync |
| pseudolocale | inside i18n:check | pass | en-XA matches en, 10240 keys |

### The three non-zero exits, each proven inherited

- **black, 38 files.** The identical 38-file set reformats at `081f1ea0c`
  (`git archive` of the base tree, same `pyproject.toml`, byte-compared file
  lists). None of the three files this spec touched is in it. The backlog is a
  black-version gap, not a regression here.
- **vitest `hiStyle › does not use formal आप`.** Asserts ≤ 119; the catalog is
  at 125. Swapping `origin/main`'s own `hi.json` in and running that one test
  reproduces the failure at **121 > 119**, so the gate is red on mainline
  before this branch exists (restored byte-identical afterwards). This spec
  added zero आप values: applying the test's exact predicate to the base and
  head catalogs gives 125 both times, added set empty. The 8 spec-engine keys
  the branch did add belong to earlier tasks and need a Hindi-register pass
  from someone who can conjugate for `तुम`; a mechanical substitution would
  ship ungrammatical Hindi, so it is not done here.
- **i18n:check `[source-strings] 1 badly shaped`**:
  `pages.artifactDeployPage.domain` = `"domain —"`, a trailing connector. Absent
  at `origin/main`, already present at `081f1ea0c` — introduced by an earlier
  branch task. Scoped to this spec's base the check passes outright, which is
  the same fact stated positively: this spec's 34 new keys are all well shaped.

### zh-CN style regressions found and fixed

The whole-suite run caught two `的`-stacking violations this spec introduced
(`sourcesSection.an_edit_writes_the_pairs_own_cell`,
`sourcesSection.this_raises_the_least_trusted_class`) — both rewritten. Two
single-string violations from earlier branch tasks in the same app's copy were
fixed alongside them, because both are mechanical and leaving them would keep
`zhStyle` red for want of one clause each:
`configPanel.overrides_counts_declared_values` (`的` stacking) and
`setupFlowPanel.approver_placeholder` (honorific 您 → 你). `zhStyle` is now
fully green, and `[changed-values]` reports 0 QA findings across everything
changed.

### Mutation probes re-executed in this sweep

Each planted, the named test confirmed failing, then restored and
`shasum`-compared byte-identical; the worktree was verified clean of probe
residue afterwards.

| Property | Probe planted | Named tests that failed | Restored |
| --- | --- | --- | --- |
| 2 — origin classification is total and faithful | `_cell_origin`: `==` → `!=` | `test_every_cell_carries_the_resolvers_own_level_and_an_agreeing_origin`, `test_an_exact_origin_means_the_operator_wrote_that_cell_and_nothing_broader` | byte-identical |
| 1, TS half — a patch touches only its own cells | `buildGridPatch` also writes the `default` sibling | fast-check `has exactly one leaf per edited cell…` + `serialises every edited cell…`, and 3 unit tests incl. `never writes the wildcard key of a pair it was not asked for` | byte-identical |
| 1, Python half — merge leaves every other path identical | `store._merge`: replace the subtree instead of merging into it | `test_merging_a_cell_patch_leaves_every_other_path_identical`, `test_a_patched_document_resolves_the_edited_cells_and_no_others_differently` (both hypothesis, both falsified) | byte-identical |
| the 3.1 fix (above) | reconciliation back to source-membership only | `lets go of a choice the refreshed answer no longer resolves` | byte-identical |

### Catalog completeness

34 `apps.specEngine.sourcesSection.*` keys × 13 catalogs (`bn de en en-XA es fr
hi it ja ko pt ru zh-CN`): every key present in every catalog, none empty, no
extras, and zero values left byte-identical to English. `en.manual.json` is an
override layer merged into `en`, not a catalog, so it carries none by design.

### Carried findings, all closed

- 1.1 → 2.1, contract corrections (`declared_at: ""`, literal `default`, branch
  on `origin`): applied in 2.1, design sketch corrected — closed.
- 1.1 → 2.1, out-of-vocabulary stored row: dispositioned in 2.1 (one sentence in
  the section; document problems reported beside the document) — closed.
- 3.1, deviation from per-cell selects to a cell pick plus a shared in-flow level
  control: recorded and reviewer-verified, queue-then-review semantics unchanged
  — closed as a recorded deviation.
- 3.1, stale pending edit: fixed in this task with a named test and a probe —
  closed.

## Task Dependency Graph

```json
{"waves": [{"id": 0, "tasks": ["1.1"]}, {"id": 1, "tasks": ["2.1"]}, {"id": 2, "tasks": ["3.1"]}, {"id": 3, "tasks": ["4.1"]}]}
```
