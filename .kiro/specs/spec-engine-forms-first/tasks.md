# Implementation Plan: Forms-first configuration

## Overview

Six serial waves on branch `feat/spec-engine-poc`: the vocabulary read first,
then the pane restructure with the shared staged-edit/review machinery, then
the three forms one wave each (they share the pane, the shared machinery, and
the same 13 catalog files, so parallel dispatch would collide), then the gate
sweep. The review gate runs between waves.

## Notes

- Every form write goes through the existing `PUT /config` door and the shared
  exact-patch review card — no new write endpoint anywhere in this plan.
- All three correctness properties require executed mutation probes before
  their reviews are dispatched, per the standing discipline.
- Every new operator-facing string ships in all 13 catalogs in the task that
  introduces it, not in the sweep.
- Contract facts for implementers: the merge deletes on JSON `null`; presets
  come from `watch_source_presets()` deep copies carrying no `enabled` key;
  `WATCH_SOURCE_PRESET_PROGRAMS` derives each preset's program from its own
  argv; the write door validates argv shape only.

## Tasks

- [ ] 1. Vocabulary read
  - [ ] 1.1 Registry route projecting the engine's form vocabularies
    - Add `GET {PREFIX}/config/registry` to `backend/routes.py` through the `_read` guard; module posture note updated (bundled vocabulary only, no stored values).
    - Project the setting registry (key, kind name, default, minimum, maximum, permitted scope names, summary) from `engine/config/settings.py`.
    - Project the bundled source presets (host, program from `WATCH_SOURCE_PRESET_PROGRAMS`, the deep-copied entry from `watch_source_presets`) and the cost-profile preset names, role names, and level names from their owning modules.
    - Pure projection of constants: no document read; stable ordering.
    - Route tests: payload equals the constants (settings count and one full entry pinned, preset entries byte-equal to the tables), 401 floor and disabled-app refusal via the pinned route table, refusal-by-path error contract N/A (no document read) — assert no config read occurs.
    - Hypothesis property: every projected source preset's `poll` argv is byte-equal to the bundled table's (backend half of Property 3).
    - _Requirements: 2.1, 4.1, 4.2_

- [ ] 2. Pane restructure and shared machinery
  - [ ] 2.1 Forms lead, JSON on request, staged edits and the shared review card
    - Restructure `ConfigPanel.tsx`: forms surface renders by default with the document editor (including its problems/advisories rendering) behind one explicit toggle control; opening it provides today's full editor behavior.
    - `buildFormPatch` in `configDocument.ts` generalizing `buildGridPatch`: staged `(segments, value | DELETE)` edits to a minimal nested patch, `DELETE` mapping to JSON `null`, prototype-safe containers.
    - `FormReview` generalizing `GridReview`: literal pretty-printed patch, one sentence per staged change from a caller-supplied sentence renderer, confirm through the existing `writeConfig`, refusals verbatim by path with stored state retained (no invalidation on failure), success invalidates config + resolved + sources queries.
    - Both surfaces re-render from a fresh read after any successful write (shared query keys); failed reads state the failure with no retained or default values.
    - fast-check property: `buildFormPatch`'s only leaf paths are the staged edits, including deletions (TS half of Property 1); hypothesis property through the real `_merge` for the deletion form (extend the existing sources-properties module).
    - Vitest: JSON never rendered unbidden, the toggle opens the full editor, fresh-read refresh both directions, failed-read doubt state; strings in all 13 catalogs.
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 5.1, 5.2, 5.3, 5.4, 5.5_

- [ ] 3. Settings form
  - [ ] 3.1 Registry-generated typed settings editing
    - Rows generated from the registry payload: control kind by registry `kind` (numeric with bounds, boolean toggle idiom, text), registry `summary` as help text, `SETTING_LABEL_KEY` labels leading with the key as detail line.
    - Scope offering limited to the setting's permitted scopes; project/source scope writes target the correct nested path.
    - In-force value + origin beside each control from the existing resolved read; staged edits visibly distinct from stored.
    - Unknown registry `kind` renders the read-only fallback with a route-to-JSON-view note, never a crash.
    - Writes staged into the shared machinery and confirmed through `FormReview`; refusal retention per the shared contract.
    - fast-check property over generated vocabularies: exactly one control per setting, matching kind and bounds, plus the fallback arm (Property 2).
    - Vitest: generated rows for the shipped vocabulary, scope gating, staged distinction, refusal retention; strings in all 13 catalogs.
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 5.1, 5.4_

- [ ] 4. Profiles form
  - [ ] 4.1 Cost profile and role assignment editing with honest effort copy
    - Per-profile role rows from the roles vocabulary: model free text defaulting to `auto`, effort via the level-buttons idiom, the inline effort-on-auto sentence while model is `auto`.
    - Profile-pinned settings (wave ceiling, run ceiling) editable on the same form via the registry vocabulary.
    - The every-project-that-selected-this-profile sentence with the count from the document.
    - Add profile = named copy of a bundled preset or existing profile; remove refused while any project's `cost_profile` selects it, naming the projects.
    - Staged edits through the shared machinery and `FormReview`.
    - Vitest: effort-on-auto sentence appears and disappears with the model value, add-as-copy provenance, removal refusal naming projects, staged distinction; strings in all 13 catalogs.
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 5.1, 5.4_

- [ ] 5. Source form
  - [ ] 5.1 Preset-constrained watch source creation, editing, and removal
    - Add flow: preset picker from the registry payload (host, program, what it ingests), staging the deep-copied entry under an operator-chosen name with `enabled` absent; no control anywhere accepts command or argument text.
    - Edit flow: name, enabled, project binding, per-source settings, maintainers; poll command and field map read-only beside the preset host; a stored entry the form cannot express renders the honest not-expressible state routing to the JSON view, never a partial form.
    - Enable states the begins-polling consequence with a link to the source's autonomy grid section; removal takes a named confirmation, patches `{"sources": {"<name>": null}}`, and states ingestion stops.
    - fast-check property on the staging function: a composed entry's `poll` is byte-equal to its preset's and no staged path carries argv the preset did not supply (frontend half of Property 3).
    - Staged edits through the shared machinery and `FormReview`.
    - Vitest: picker rendering, compose-inert-by-default, no-freeform-argv (absence of any command input), not-expressible routing, enable consequence, removal confirmation and patch shape; strings in all 13 catalogs.
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7, 5.1, 5.4_

- [ ] 6. Gate sweep
  - [ ] 6.1 Full verification sweep and dispositions
    - Run all gates with real exit codes: spec_engine pytest, spec_builder pytest, isort/flake8/mypy, tsc, eslint, full vitest (watch for unhandled errors exiting 1 with tests green), i18n:check, key-refs, manifest-sync, boundary fence.
    - Confirm all three correctness properties have executed mutation probes recorded (plant, named test fails, restore byte-identical).
    - Disposition every carried finding from tasks 1.1–5.1 in this file; verify catalog completeness for every new string; append a dated verification record section.
    - _Requirements: 1.5, 2.6, 5.3, 5.4_

## Task Dependency Graph

```json
{"waves": [{"id": 0, "tasks": ["1.1"]}, {"id": 1, "tasks": ["2.1"]}, {"id": 2, "tasks": ["3.1"]}, {"id": 3, "tasks": ["4.1"]}, {"id": 4, "tasks": ["5.1"]}, {"id": 5, "tasks": ["6.1"]}]}
```
