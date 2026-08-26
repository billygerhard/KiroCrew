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
- **Carried from 1.1 (contract facts for 2.1–5.1):** the registry route is
  `GET {PREFIX}/config/registry`; source preset host keys are the bundled
  table's own (`github`, `gitlab`), NOT domain names — type from the payload.
- **Carried from 2.1's review (binding on 3.1, 4.1, 5.1):** (a) `FormReview`
  is presentational — each form's mutation OWNS its `onSuccess` invalidation
  (config + resolved + sources) and must pin it with a named fresh-read test,
  since the shared piece cannot enforce it; (b) 3.1 introduces the shared
  `useStagedEdits` hook the design names, and 4.1/5.1 consume it — three
  ad-hoc staging states is the drift the design forbids; (c) the hook must
  prevent or reconcile ancestor/descendant path overlaps in staged edits:
  `buildFormPatch` is last-edit-wins, so an unreconciled overlap lets the
  review card describe an edit the patch no longer carries. Property-check
  the reconciliation.
- **Fixed post-review (orchestrator):** `FormReview`'s refusal gate widened to
  `error != null` so a caller passing `undefined` cannot render an empty
  refusal for a write never attempted.
- **Carried from 1.1's review (for 3.1 and 6.1):** `Setting.choices` is not
  projected (every shipped setting has it empty). 3.1's unknown-kind fallback
  must also treat a str setting as free-text-safe only because no shipped
  setting declares choices; if one ever does, the vocabulary and the form gain
  the choices arm together. Disposition in 6.1.
- **Carried from 3.1's review (design gap, disposition in 6.1):** the settings
  form's resolved read is taken without the `source` query parameter the route
  accepts, so a source-scoped row shows the app/project resolution while its
  write targets `sources.<name>.…`. The review sentence's wording stays honest
  (it never claims the shown value is stored at the target path), but
  `storedAt()` cannot withdraw a no-op re-entry of a source's own stored
  value. Design-conformant ("the existing resolved read"), so the fix belongs
  to the design, not this task.
- **Fixed post-review (orchestrator, 3.1):** the watch-source picker is now
  gated on some rendered setting permitting source scope, not on sources
  merely existing; pinned by "offers the source picker only while a setting
  can be written at source scope" (mutation-probed).
- **Dispositioned (3.1, no code change):** an int row stages `Number(raw)`
  verbatim, so a browser-reported fractional entry reaches the write door and
  is refused as "expected an integer" by path. Left as-is deliberately: the
  row's own comment already argues bounds live in the registry/door rather
  than restated frontend copies that can drift, and the refusal is honest and
  path-addressed. Same disposition class as the door's shape-only validation.
- **Accepted deviation (4.1, review-verified against requirements):** the
  registry route now projects each cost-profile preset's entry beside its
  name, plus `profile_settings` and the effort ladder — 3.5's add-as-copy was
  unbuildable from names alone, and neither vocabulary is derivable from a
  Setting record while both are door-enforced. Route stays a pure zero-
  document-read projection; design.md updated in the same commit; two pinned
  backend tests. Include in 6.1's disposition ledger.
- **Carried from 4.1's review (for 6.1's ledger):** the effort-on-auto copy
  ("takes effect only once this role names a concrete model") over-promises —
  the engine's `model_supports_effort()` also drops effort for some concrete
  models (haiku, nova, deepseek). Wording mirrors requirement 3.2 verbatim and
  effort capability is not projected in the registry, so the fix belongs to
  the requirement/registry, not the catalog. Disposition in 6.1.
- **Fixed post-review (orchestrator, 4.1):** (a) stale ProfilesForm annotation
  in design.md's diagram corrected to "document + registry vocabularies";
  (b) a staged copy whose source changed under it is now withdrawn WITH a
  stated announcement (was: silently absent from card and patch) — named test,
  mutation-probed; (c) a refused removal click is now acknowledged with a
  leading "The removal was refused." sentence that clears on profile switch —
  named test, mutation-probed; (d) the naming note's copy rewritten across all
  13 catalogs to fix subject-verb disagreement with multiple projects;
  (e) render coverage added for `edit_replaces_the_pinned_limit`,
  `the_registry_kind_is_not_editable_here` on a pinned row, and
  `a_profile_may_pin_only_these_limits`.
- **Dispositioned (4.1, leftovers):** the implementer's probe backups sit in
  the shared untracked `./tmp` (ConfigPanel.bak.tsx, useStagedEdits.bak.ts,
  add_profiles_form.py, sha-before.txt, commit-4.1.txt) per the shared-tmp
  rule. Every commit in this spec stages explicit paths, never `git add .`,
  so the stale-duplicate risk the review named does not arise in this flow.
- **Accepted deviation (5.1, for 6.1's ledger):** the source form is
  preset-plus-parameters, per the design's own decision row. Requirement 4.2's
  clause reads "no freeform command or argument entry anywhere"; the repository
  parameter is a shape-checked DATA field (one owner and one repo,
  `[A-Za-z0-9._-]` halves joined by a single `/`, enforced on compose AND
  acceptance) that fills only a preset's designated placeholder slot, never
  `argv[0]`, so it is not freeform and cannot rewrite the argument around the
  slot. 6.1 should consider amending R4.2's wording to name the parameter
  explicitly; requirements outrank design and the current text predates the
  round-1 review finding that byte-equality made R4.3/4.7 unreachable for
  every functional source.
- **Accepted deviation (5.1, for 6.1's ledger):** no rename control on the
  source edit flow, though task 5.1's bullet lists `name` among the editable
  fields. Renaming is a delete plus a whole-entry add, including fields the
  form never shows; the JSON view owns it. Recorded in design.md's SourceForm
  section; the name renders in the selector and every path line.
- **Fixed post-review (5.1 round 3, orchestrator):** the round-2 reviewer's
  major — the repository parameter accepted arbitrary text into executed argv
  (gitlab's slot is a whole argument; github's endpoint/query rewritable via
  `#?&/` while `slotValue` still called the result the preset's own) — closed
  with `wellFormedRepository` enforced on both `pollForRepository` (compose)
  and `slotValue` (acceptance), stated refusals on both repository controls
  (typed text kept, stale staged poll withdrawn), the slotless-preset add
  refusal (a typed repository is never silently dropped), the sentence-table
  call-site indexing fixed to match its comment, and +5 named tests / +1
  property (all three guards mutation-probed, restores SHA-verified).
- **Recorded (5.1, for 6.1's sweep):** ConfigPanel.tsx's eslint a11y warning
  count grew 7 → 19 across 5.1 (same accepted htmlFor/id label idiom the other
  forms carry; label association verified by getByLabelText in tests). The
  round-2 implementer's report had misstated these as all pre-existing.
- **Residuals (5.1, accepted):** a poll hand-edited beyond its repository slot
  (changed flag, extra argument, another program) is owned by the JSON view;
  the not-expressible state offers removal but no other control. The source
  suite's default `gh` fixture polls a NAMED repository (`GH_POLL_NAMED`) and
  `fresh` holds the placeholder — later tasks assert against those.
- **Fixed post-review (5.1 round 3 approval minors, orchestrator):** (a)
  `wellFormedRepository` now refuses halves made entirely of dots (`../..`
  re-targeted the endpoint by path normalization while the frame reassembled);
  property list extended, guard mutation-probed. (b) the repository buffer and
  both refusal states now reset on a successful write and on discard, matching
  the form's re-derive-from-a-fresh-read posture; pinned by "drops the refusal
  with the rest of the pending posture on discard".

## Tasks

- [x] 1. Vocabulary read
  - [x] 1.1 Registry route projecting the engine's form vocabularies
    - Add `GET {PREFIX}/config/registry` to `backend/routes.py` through the `_read` guard; module posture note updated (bundled vocabulary only, no stored values).
    - Project the setting registry (key, kind name, default, minimum, maximum, permitted scope names, summary) from `engine/config/settings.py`.
    - Project the bundled source presets (host, program from `WATCH_SOURCE_PRESET_PROGRAMS`, the deep-copied entry from `watch_source_presets`) and the cost-profile preset names, role names, and level names from their owning modules.
    - Pure projection of constants: no document read; stable ordering.
    - Route tests: payload equals the constants (settings count and one full entry pinned, preset entries byte-equal to the tables), 401 floor and disabled-app refusal via the pinned route table, refusal-by-path error contract N/A (no document read) — assert no config read occurs.
    - Hypothesis property: every projected source preset's `poll` argv is byte-equal to the bundled table's (backend half of Property 3).
    - _Requirements: 2.1, 4.1, 4.2_

- [x] 2. Pane restructure and shared machinery
  - [x] 2.1 Forms lead, JSON on request, staged edits and the shared review card
    - Restructure `ConfigPanel.tsx`: forms surface renders by default with the document editor (including its problems/advisories rendering) behind one explicit toggle control; opening it provides today's full editor behavior.
    - `buildFormPatch` in `configDocument.ts` generalizing `buildGridPatch`: staged `(segments, value | DELETE)` edits to a minimal nested patch, `DELETE` mapping to JSON `null`, prototype-safe containers.
    - `FormReview` generalizing `GridReview`: literal pretty-printed patch, one sentence per staged change from a caller-supplied sentence renderer, confirm through the existing `writeConfig`, refusals verbatim by path with stored state retained (no invalidation on failure), success invalidates config + resolved + sources queries.
    - Both surfaces re-render from a fresh read after any successful write (shared query keys); failed reads state the failure with no retained or default values.
    - fast-check property: `buildFormPatch`'s only leaf paths are the staged edits, including deletions (TS half of Property 1); hypothesis property through the real `_merge` for the deletion form (extend the existing sources-properties module).
    - Vitest: JSON never rendered unbidden, the toggle opens the full editor, fresh-read refresh both directions, failed-read doubt state; strings in all 13 catalogs.
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 5.1, 5.2, 5.3, 5.4, 5.5_

- [x] 3. Settings form
  - [x] 3.1 Registry-generated typed settings editing
    - Rows generated from the registry payload: control kind by registry `kind` (numeric with bounds, boolean toggle idiom, text), registry `summary` as help text, `SETTING_LABEL_KEY` labels leading with the key as detail line.
    - Scope offering limited to the setting's permitted scopes; project/source scope writes target the correct nested path.
    - In-force value + origin beside each control from the existing resolved read; staged edits visibly distinct from stored.
    - Unknown registry `kind` renders the read-only fallback with a route-to-JSON-view note, never a crash.
    - Writes staged into the shared machinery and confirmed through `FormReview`; refusal retention per the shared contract.
    - fast-check property over generated vocabularies: exactly one control per setting, matching kind and bounds, plus the fallback arm (Property 2).
    - Vitest: generated rows for the shipped vocabulary, scope gating, staged distinction, refusal retention; strings in all 13 catalogs.
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 5.1, 5.4_

- [x] 4. Profiles form
  - [x] 4.1 Cost profile and role assignment editing with honest effort copy
    - Per-profile role rows from the roles vocabulary: model free text defaulting to `auto`, effort via the level-buttons idiom, the inline effort-on-auto sentence while model is `auto`.
    - Profile-pinned settings (wave ceiling, run ceiling) editable on the same form via the registry vocabulary.
    - The every-project-that-selected-this-profile sentence with the count from the document.
    - Add profile = named copy of a bundled preset or existing profile; remove refused while any project's `cost_profile` selects it, naming the projects.
    - Staged edits through the shared machinery and `FormReview`.
    - Vitest: effort-on-auto sentence appears and disappears with the model value, add-as-copy provenance, removal refusal naming projects, staged distinction; strings in all 13 catalogs.
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 5.1, 5.4_

- [x] 5. Source form
  - [x] 5.1 Preset-constrained watch source creation, editing, and removal
    - Add flow: preset picker from the registry payload (host, program, what it ingests), staging the deep-copied entry under an operator-chosen name with `enabled` absent; no control anywhere accepts command or argument text.
    - Edit flow: name, enabled, project binding, per-source settings, maintainers; poll command and field map read-only beside the preset host; a stored entry the form cannot express renders the honest not-expressible state routing to the JSON view, never a partial form.
    - Enable states the begins-polling consequence with a link to the source's autonomy grid section; removal takes a named confirmation, patches `{"sources": {"<name>": null}}`, and states ingestion stops.
    - fast-check property on the staging function: a composed entry's `poll` is byte-equal to its preset's and no staged path carries argv the preset did not supply (frontend half of Property 3).
    - Staged edits through the shared machinery and `FormReview`.
    - Vitest: picker rendering, compose-inert-by-default, no-freeform-argv (absence of any command input), not-expressible routing, enable consequence, removal confirmation and patch shape; strings in all 13 catalogs.
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7, 5.1, 5.4_

- [x] 6. Gate sweep
  - [x] 6.1 Full verification sweep and dispositions
    - Run all gates with real exit codes: spec_engine pytest, spec_builder pytest, isort/flake8/mypy, tsc, eslint, full vitest (watch for unhandled errors exiting 1 with tests green), i18n:check, key-refs, manifest-sync, boundary fence.
    - Confirm all three correctness properties have executed mutation probes recorded (plant, named test fails, restore byte-identical).
    - Disposition every carried finding from tasks 1.1–5.1 in this file; verify catalog completeness for every new string; append a dated verification record section.
    - _Requirements: 1.5, 2.6, 5.3, 5.4_

## Task Dependency Graph

```json
{"waves": [{"id": 0, "tasks": ["1.1"]}, {"id": 1, "tasks": ["2.1"]}, {"id": 2, "tasks": ["3.1"]}, {"id": 3, "tasks": ["4.1"]}, {"id": 4, "tasks": ["5.1"]}, {"id": 5, "tasks": ["6.1"]}]}
```

## Verification record — 2026-08-26 (task 6.1, closing sweep)

Run on `feat/spec-engine-poc` at `086e5a825` plus this task's three edits (the
zh-CN string, the `choices` guard test, and R4.2's clause).
Exit codes are the real unpiped codes.

### Gates

| Gate | Command | Exit | Result |
|---|---|---|---|
| spec_engine pytest | `pytest src/kiro_crew/apps/builtins/spec_engine/tests/ -q` | **0** | `3424 passed` (3423 before this task's added guard) |
| spec_builder pytest | `pytest src/kiro_crew/apps/builtins/spec_builder/ -q` | **0** | `269 passed` |
| isort | `isort --check-only src/kiro_crew test` | **0** | `Skipped 2 files` |
| flake8 | `flake8 src/kiro_crew test` | **0** | no output |
| mypy (repo) | `mypy src/kiro_crew` | **1** | `Found 3 errors in 1 file (checked 1068 source files)` — all `hooks.py` `os.{list,get,set}xattr`, Linux-only names on darwin; `hooks.py` is untouched by the whole branch. **Inherited.** |
| mypy (territory) | `mypy src/kiro_crew/apps/builtins/spec_engine` | **0** | `Success: no issues found in 205 source files` |
| black (repo) | `black --check src/kiro_crew test` | **1** | `1178 files would be reformatted` under the pinned `black==26.3.1`; repo-wide and pre-existing. Wave-touched `.py` files: exit **0**. Not in this task's gate list; `AGENTS.md` runs black as a writer, not a checker. **Inherited.** |
| tsc | `npx tsc -b` | **0** | no output |
| eslint | `npx eslint src --ext .ts,.tsx` | **0** | `601 problems (0 errors, 601 warnings)`; `ConfigPanel.tsx` = 19 warnings (10 `jsx-a11y/label-has-for`, 9 `jsx-a11y/control-has-associated-label`) |
| vitest (full) | `npx vitest run` | **1** | `1 failed \| 12168 passed \| 2 expected fail \| 3 skipped (12174)`, `890 passed (891)` files. The one failure is the inherited `hiStyle` baseline, proven below. No unhandled errors; the `App.test.tsx` credits-pill flake did not reproduce in either full run. |
| i18n:check | `npm run i18n:check` | **1** | pseudolocale ok · key-refs ok · extractable ok · plurals ok · **source-strings exit 1** · added-lines/vs-base/untranslated/allcaps ok · dnt ok · manifest-sync ok. Every hard-zero gate passes; the one failure is `pages.artifactDeployPage.domain` (`"domain —"`), present at `b84c6d1df^` and not a spec-engine key. **Inherited.** `changed-values` reports 0 findings, which covers this task's zh-CN edit. |
| key-refs | `node scripts/check-i18n-keys.mjs` | **0** | `11611 static key references all resolve, 30 dynamic site(s) at baseline` |
| manifest-sync | `node scripts/check-app-manifest-sync.mjs` | **0** | `20 built-in manifests, 171 strings match locales/en.json exactly` |
| boundary fence | `pytest …/tests/test_app_boundary_fence.py -q` | **0** | `38 passed` |
| build | `npm run build` | **0** | `✓ built in 8.55s` |

**One environment note, not a finding.** With the ambient `TMPDIR`
(`…/KiroCrew/.kirocrew-dev/scratch/runtime-*`, which sits inside a *different*
git repository), `test_a_directory_that_is_not_a_repository_fails_closed` fails
its own stated precondition — `git -C <tmp_path> rev-parse --git-dir` returns 0
— and the spec_engine suite exits 1 on that one test. Re-run with a `TMPDIR`
outside any repository and the fence suite is `38 passed`, the whole suite
`3424 passed`. The fence code is not implicated; the test asserts the
precondition rather than assuming it, which is why it says so out loud.

### Inherited failures, proven rather than asserted

- **`hiStyle` formal-आप, 125 > 119.** Re-proved by parent-catalog swap because
  the count sits above the baseline: flattening `hi.json` at `b84c6d1df^` (the
  parent of the wave's first commit) and applying the test's own predicate
  (`'आप' in value` after stripping `अपने[-\s]?आप`) yields **125** violations
  pre-wave and **125** now — **0 added, 0 removed**, and no
  `apps.specEngine.*Form.*` key among them. The stale figure is the test's
  hard-coded `119`, which the catalog had already outgrown before this spec
  began. Not this wave's to move.
- **`source-strings` on `pages.artifactDeployPage.domain`.** The value
  `"domain —"` is byte-identical at `b84c6d1df^`. Not a spec-engine key; the
  gate reports it because it is new *vs `origin/main`*, i.e. it belongs to an
  earlier branch commit.

### Correctness properties: executed mutation probes

Every probe below was executed **in this task**, not cited from a commit
message: plant → named test fails → restore → SHA compared. Each source file's
`shasum -a 256` after restore equals its value before the plant, and
`git diff --stat` over `src/apps/spec-engine/` is empty.

| Property | Live named test | Planted regression | Named test failed with |
|---|---|---|---|
| **1** — a form patch touches only its staged paths | `SpecEngineFormPatch.property.test.ts` › *Property 1 (TS half): a form patch touches only its staged paths* (+ backend hypothesis `test_merging_a_cell_patch_leaves_every_other_path_identical`, `test_merging_a_deletion_patch_removes_only_the_named_key` through the real `_merge`) | `buildFormPatch`: `edit.value === DELETE ? null` → `? false` | 5 tests red, incl. *spells a removal as null and never emits one otherwise* — `expected false to be null`. Restore SHA `d9755967…` = before. |
| **2** — the settings form is total over the registry | `SpecEngineSettingsForm.property.test.tsx` › *Property 2: the settings form is total over the registry* | `CONTROL_BY_KIND`: `bool: 'checkbox'` → `bool: 'text'` | *renders exactly one control per setting, of the kind the registry names* — counterexample `{"kind":"bool",…}`. Restore SHA `a7cfe295…` = before. |
| **3** — a composed source carries only preset commands | `SpecEngineSourceCompose.property.test.ts` (frontend half, incl. the round-3 shape guard) + backend hypothesis `test_a_projected_preset_carries_the_bundled_argv_and_survives_an_edit_to_it` | `wellFormedRepository`: dot-only-halves guard → `owner !== '' && repo !== ''` | *refuses a repository that could rewrite the argument around the slot* — counterexample `"../.."`, `expected true to be false`. Restore SHA `a7cfe295…` = before. |

The round-3 shape guard is therefore confirmed live, not merely recorded: the
`../..` probe is exactly the approval-minor (a) fix, and removing it turns a
named test red.

A fourth probe covers the guard **added by this sweep** (see the ledger's
`Setting.choices` row): planting `choices=("dashboard","slack")` on
`notify.channel` turned
`test_no_projected_setting_declares_an_enforced_choice_set` red with
`['notify.channel'] now declare choices the write door enforces`; restore SHA
`27014c25…` = before.

### Catalog completeness

Computed against the catalogs, not against the diff: every en key absent at
`b84c6d1df^` and present now.

- **141 new keys** — `sourceForm` 75, `profilesForm` 44, `settingsForm` 22.
- All **13** catalogs (`bn de en es fr hi it ja ko pt ru zh-CN` + `en-XA`)
  carry **141/141**: **0 missing, 0 empty**.
- `en.manual.json` carries none, which is correct — it is an override overlay,
  and the key-refs gate separately proves no en/en.manual shadowing.
- **4 values are identical to English, all legitimately**: `de`/`it`
  `Repository (owner/repo)` (the loanword *is* the German and Italian term) and
  `it`/`pt` `Preset` (likewise). Every other 137 × 12 value differs.
- `node scripts/gen-pseudolocale.mjs` → `wrote … (10407 keys)` and
  `git diff -- src/i18n/locales/en-XA.json` is **empty**: en-XA is byte-identical
  to a fresh regeneration.

### Disposition ledger, tasks 1.1 → 5.1

Verdicts: **fixed** · **accepted-with-reason** · **amendment-recommended**.

| # | Carried finding | Verdict | Reason / evidence |
|---|---|---|---|
| 1 | 1.1 contract facts for 2.1–5.1 (registry route path; preset host keys are the table's own) | accepted-with-reason | Informational and consumed: the route is `GET {PREFIX}/config/registry`, and `test_each_source_preset_is_byte_equal_to_the_bundled_table` asserts hosts against `WATCH_SOURCE_PRESET_HOSTS` rather than domain names. |
| 2 | 1.1 review — `Setting.choices` not projected | **fixed** | The assumption was documented in two prose comments (`api.ts`, `CONTROL_BY_KIND`) and enforced nowhere, while `Setting.coerce` *does* refuse a non-member value — so a setting gaining `choices` would silently give the operator a text box the door then refuses by path. Added `test_no_projected_setting_declares_an_enforced_choice_set`, which pins the precondition (21 settings, none declaring choices) and fails the moment the first half of that change lands alone. Mutation-probed. |
| 3 | 2.1 review (a) — `FormReview` is presentational, each mutation owns its invalidation | accepted-with-reason | Honored: all three forms carry a named *re-renders … from a fresh read after a successful write* test (`SettingsForm:693`, `ProfilesForm:864`, `SourceForm:1232`). |
| 4 | 2.1 review (b) — one shared `useStagedEdits`, not three ad-hoc states | accepted-with-reason | Honored: exactly three `useStagedEdits()` call sites, one per form (`ConfigPanel.tsx:1491, 2226, 3460`); no other staging state. |
| 5 | 2.1 review (c) — ancestor/descendant overlap must be reconciled, property-checked | accepted-with-reason | Honored and property-checked: *leaves no two staged paths overlapping, however they were staged* and *drops an ancestor when a descendant is staged, and the other way round*. |
| 6 | 2.1 orchestrator fix — `FormReview` refusal gate widened to `error != null` | accepted-with-reason | Verified in place (`ConfigPanel.tsx:579`), so a caller passing `undefined` cannot render an empty refusal for a write never attempted. |
| 7 | 3.1 review — the settings form's resolved read omits the `source` query parameter | accepted-with-reason + **design-amendment recommended** | Real and unfixed here. `specEngineApi.resolvedConfig` accepts `source`; the form deliberately does not pass it, and says why: it shares `QK.resolved(project)` with the resolved pane so *"the two read one answer"*. Passing `source` needs a second cache entry keyed `(project, source)` or a re-keying that makes the pane refetch on every source pick — a design change, not a call-site edit. The operator-visible residual is bounded: the review sentence stays honest (`{{oldValue}} is in force from {{origin}}, and {{newValue}} would be stored at {{path}}` never claims the old value is stored at the target), and the only concrete loss is that `storedAt()` cannot withdraw a no-op re-entry of a source's own stored value — a redundant write, logged, never a wrong one. **Recommended amendment:** design.md's `SettingsForm` section should give a source-scoped row its own resolved read keyed `(project, source)`, so R2.5's "value currently in force" is the value in force at the row's own target. |
| 8 | 3.1 orchestrator fix — source picker gated on a setting permitting source scope | accepted-with-reason | Verified: *offers the source picker only while a setting can be written at source scope* (`SettingsForm:519`). |
| 9 | 3.1 — an int row stages `Number(raw)` verbatim, so a fractional entry is refused by the door | accepted-with-reason | Re-affirmed unchanged. Bounds live in the registry and the door; a restated frontend copy is a second thing to drift. The refusal is stated and path-addressed — the same disposition class as the door's shape-only argv validation. |
| 10 | 4.1 — the registry route grew profile-preset entries, `profile_settings`, the effort ladder | accepted-with-reason | Reviewed against the requirements and sound: R3.5's add-as-copy is unbuildable from names alone, and neither pinnability (not a `Scope`) nor effort (not a setting) is derivable from a `Setting` record while both are door-enforced. The route stays a zero-document-read projection; design.md carries it; `test_each_profile_preset_carries_the_entry_a_copy_is_made_from` and `test_the_profile_role_and_level_vocabularies_are_the_owning_modules` pin it. |
| 11 | 4.1 review — effort-on-auto copy over-promises vs `model_supports_effort()` | accepted-with-reason + **requirement-amendment recommended** | The engine's `_usable_effort()` drops a pinned effort for any concrete model failing `model_supports_effort()` (haiku, nova, deepseek, minimax, glm, qwen), so naming a concrete model is necessary and *not* sufficient — the copy mirrors R3.2 verbatim, so the requirement is what over-promises. Not silent, though, and that bounds it: the engine reports `dropped_effort` per role and the pane already flags it beside the resolved effort (`ConfigPanel.tsx:5118`, `configPanel.effort_dropped`). Left as-is because the honest sentence needs `supports_effort` projected in the registry — a backend + design + requirement change, outside a sweep. **Recommended amendment:** R3.2 should state that a pinned effort takes effect once the role names a concrete model *that accepts one*, and the registry read should project the capability the sentence would then depend on. |
| 12 | 4.1 orchestrator fixes (a)–(e) — design diagram annotation, withdrawn-copy announcement, refused-removal acknowledgement, naming-note subject-verb, render coverage | accepted-with-reason | All five landed in `1cbd82b42` with named tests; the profiles suite is green in the full run above. |
| 13 | 4.1 — probe backups left in the shared untracked `./tmp` | accepted-with-reason | Left untouched per the shared-tmp rule (other sessions' files sit beside them). Every commit in this spec stages explicit paths, so the stale-duplicate risk does not arise. This task's own probe backups went to `./tmp/sweep-probe/` and are likewise left in place. |
| 14 | 5.1 — preset-plus-parameters vs R4.2's absolute wording | **fixed, by amending R4.2** | The clause was a single clause, so it is amended rather than merely recommended: R4.2 now says the UI composes commands from the preset's tables *"filling only the placeholder positions those commands designate — never the position naming the program — from a value constrained to the shape of a repository name"*, and keeps *"SHALL NOT offer freeform command or argument entry anywhere"* absolute. EARS form (`WHEN … THE … SHALL …`) preserved; requirements now outrank design on the point they previously contradicted, and the round-1 finding that byte-equality made R4.3/4.7 unreachable for every functional source is no longer in tension with the text. **The orchestrator must re-run the spec validator** — this task had no access to it. |
| 15 | 5.1 — no rename control, though the task bullet lists `name` | accepted-with-reason | A rename is a delete plus an add of the *whole* entry, including fields this form never shows, which is precisely the partial-form write R4.5 forbids. The JSON view owns it; the name still renders in the selector and in every path line. Read the task bullet's `name` as the displayed key. |
| 16 | 5.1 round-3 fix — `wellFormedRepository` on both compose and acceptance, stated refusals, slotless-preset refusal | accepted-with-reason | Confirmed **live**, not merely recorded: the Property 3 probe above removes the guard and turns a named test red on `"../.."`. |
| 17 | 5.1 — `ConfigPanel.tsx` eslint a11y warnings grew 7 → 19 | accepted-with-reason | Count verified at exactly 19, 0 errors. All 19 are the file's existing label idiom: `jsx-a11y/label-has-for` demands *both* nesting and `id`, which is stricter than WCAG and deprecated upstream, and every flagged site does carry `htmlFor` + a matching `id` (spot-verified at `ConfigPanel.tsx:4380`). Association is proven behaviorally by `getByLabelText` in the profiles and source suites. |
| 18 | 5.1 residuals — a poll hand-edited beyond its slot belongs to the JSON view; `GH_POLL_NAMED` fixture note | accepted-with-reason | Unchanged and correct: the not-expressible state offers removal and no other control, because a deletion writes no field it did not show. Fixture verified — `GH_POLL_NAMED` is `GH_POLL` with `OWNER/REPO` replaced, and `fresh` keeps the placeholder. |
| 19 | 5.1 approval minor (b) — repository buffer and both refusal states reset on write and discard | accepted-with-reason | Landed in `086e5a825` and pinned by *drops the refusal with the rest of the pending posture on discard*; source suite green. |

### Fixed in this sweep

- **zh-CN half-width punctuation** (this sweep's own finding, not carried).
  `apps.specEngine.sourceForm.that_is_not_a_repository_name` used `,` and `:`
  between CJK characters, failing `zhStyle`'s hard-zero *uses full-width
  punctuation between CJK characters*. Replaced with `，` and `：`. A broader
  scan than the gate's own — any half-width `, . ; : ? ! ( )` adjacent to a CJK
  character across all 141 wave-added zh-CN values — finds no second instance.
  `zhStyle` is now `21 passed`.
- **The `Setting.choices` guard** described in ledger row 2.
- **R4.2's wording** described in ledger row 14.

### Open items handed back to the orchestrator

1. Re-run the spec validator over the amended `requirements.md` (R4.2).
2. Two recommended amendments, neither implemented here: the source-scoped
   resolved read (ledger 7, design) and the effort-capability sentence
   (ledger 11, requirement + registry).
3. Four inherited gate failures stay red and are not this spec's:
   `hooks.py` mypy on darwin, repo-wide `black --check`, `hiStyle`'s stale 119
   baseline, and `source-strings` on `pages.artifactDeployPage.domain`.
