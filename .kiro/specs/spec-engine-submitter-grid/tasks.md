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

## Tasks

- [ ] 1. Backend read surface
  - [ ] 1.1 Sources route with resolved matrices and origins
    - Add `GET {PREFIX}/config/sources` to `backend/routes.py` through the `_read` guard, module posture note updated for the new read.
    - Compute each source's full matrix via `AutonomyPolicy.decision()` per (class, type) pair against the engine's reader; include sources with no `autonomy` field (all-default matrices).
    - Derive per-cell `origin` (`exact` / `wildcard` / `default`) from `declared_at` versus the queried pair, and `policy_covers_gates` from `decision.permits(EXECUTION)`.
    - Ship `submitter_classes`, `spec_types`, and `levels` vocabularies from `config/schema.py` constants in the payload.
    - Return the refusal-by-path shape for a malformed stored grid (`ConfigValidationError`), matching the resolved-config route's error contract.
    - Route tests: stored exact cell, wildcard row, empty grid, absent autonomy field, no sources, 401 floor, malformed-grid refusal, origin unit tests.
    - Hypothesis property: origin classification is total and faithful against the real resolver (Property 2).
    - Hypothesis property: merging a minimal cell patch through the real `_merge` leaves every other path identical (Python half of Property 1).
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 2.4, 4.1, 4.2_

- [ ] 2. Read-only sources view
  - [ ] 2.1 Sources section rendering the grid with origins and semantics
    - Add the Sources section to the Configuration pane: source list, per-source matrix (classes × spec types in schema order), per-cell level + origin annotation + policy-covers-gates marker.
    - Default-answered cells render the Unconfigured_Default wording ("waits for a human"), never blank or zero.
    - Failed read forces the stated-failure state with no values rendered from retained or default data; loading state is distinct from empty.
    - Empty-sources state names the Setup Assistant's offer flow as where a source comes from.
    - Semantics copy in the section: unclassifiable authors resolve to the least-trusted class; a level authorizes every level below it; screening quarantine caps to authoring regardless of the grid and only lowers.
    - New `QK.sources` query key; api.ts types (`SourcesPayload`, `SourceGridCell`) mirroring the route.
    - All new strings in all 13 catalogs; pseudolocale regenerated.
    - Vitest: matrix rendering with all three origins, default wording, failed-read doubt state, empty state, semantics copy presence, vocabulary-driven axes.
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 2.1, 2.2, 2.3, 2.4_

- [ ] 3. Guarded editing
  - [ ] 3.1 Cell edits with exact-patch review through the guarded write
    - Per-cell level selects accumulating `PendingEdit`s in local state; nothing written before review.
    - `buildGridPatch` as a pure function producing the minimal nested patch; fast-check property that its only paths are the edited cells (TS half of Property 1).
    - Review card: the exact pretty-printed patch, one sentence per edit (cell, old level and origin, new level), the external-raise warning when an edit raises the least-trusted class's resolved level, the narrowing note when the edited cell was wildcard-answered (the wildcard is never modified).
    - Confirm submits through the existing `writeConfig` client; refusals render verbatim by path with the grid still showing stored state (no query invalidation on failure).
    - Success invalidates sources, config, and resolved queries; the grid re-renders from a fresh read, and the projects table's resolved settings agree on their next read.
    - Vitest: edit flow end to end, exact patch shown, external warning, wildcard narrowing note, refusal retention, fresh-read refresh; strings in all catalogs.
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 4.3_

- [ ] 4. Gate sweep
  - [ ] 4.1 Full verification sweep and dispositions
    - Run all gates: spec_engine pytest, spec_builder pytest, isort/flake8/mypy on touched trees, tsc, eslint, full vitest, manifest-sync, boundary fence, i18n:check, key-refs.
    - Confirm both correctness properties have executed mutation probes recorded (revert, named test fails, restore byte-identical).
    - Disposition every carried finding from tasks 1.1–3.1 in this file; verify catalogs complete for every new string.
    - _Requirements: 1.5, 3.4, 4.1, 4.2, 4.3_

## Task Dependency Graph

```json
{"waves": [{"id": 0, "tasks": ["1.1"]}, {"id": 1, "tasks": ["2.1"]}, {"id": 2, "tasks": ["3.1"]}, {"id": 3, "tasks": ["4.1"]}]}
```
