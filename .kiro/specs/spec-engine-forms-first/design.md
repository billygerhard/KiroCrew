# Design Document

## Overview

The Configuration pane inverts: a Form_Surface leads, and the raw document
becomes an on-request view. Nothing changes in the write model — every form
funnels staged edits into the existing guarded `PUT /config` behind the
established exact-patch review card — so the design is mostly (a) one backend
read extension exposing vocabularies the forms are generated from, (b) a shared
staged-edit/patch/review mechanism generalized from the sources grid's existing
one, and (c) three form components that consume vocabularies rather than
hard-coding fields.

Facts that shape the design, verified against source:

- **The setting registry already carries everything a form needs**: dotted key,
  `kind` (int/float/bool/str), `minimum`/`maximum`, permitted scopes, and an
  operator-facing `summary` (`engine/config/settings.py`). The settings form is
  *generated* from this vocabulary.
- **Watch-source presets are ready-made entries**: `WATCH_SOURCE_PRESETS` in
  `engine/watch/sources.py`, with `watch_source_presets(host)` returning a
  deep-copied entry ready to write into `sources`, deliberately carrying no
  `enabled` key (a fresh copy is inert until an operator enables it) and
  `public: true`. `WATCH_SOURCE_PRESET_PROGRAMS` already derives each preset's
  program from its own argv — exactly the "which tool this needs" line the
  picker displays. The write door validates argv shape only, NOT program
  membership, so the form's never-freeform-argv constraint carries real safety
  weight.
- **The engine's merge is surgical** (`_merge` in `engine/config/store.py`:
  nested maps merge key-wise, `null` deletes), so removal patches are
  `{"sources": {"<name>": null}}` and cell-level writes leave siblings alone —
  the same property the submitter-grid editing already relies on and
  property-tests.
- **The existing pieces to generalize, not duplicate**: `configDocument.ts`
  already has a pure patch builder and prototype-safe containers;
  `ConfigPanel.tsx`'s `GridReview` already implements the exact-patch review
  card idiom; `SETTING_LABEL_KEY` already maps all 21 registry keys to
  translated labels.

## Architecture

```
ConfigPanel
├── FormSurface (default view)
│   ├── ProjectsTable (existing, unchanged)
│   ├── SettingsForm         ◄── vocabulary from GET .../config/registry (new read)
│   ├── ProfilesForm         ◄── document + resolved roles (existing reads)
│   ├── SourcesSection (existing grid) + SourceForm (new)
│   │        └── preset picker ◄── source presets in the registry read
│   └── FormReview (shared exact-patch card, generalized from GridReview)
│            └── confirm ─► writeConfig (existing PUT /config, unchanged)
└── JsonView (existing DocumentEditor + advisories, behind one explicit control)
```

## Components and Interfaces

### Backend: `GET {PREFIX}/config/registry` (new read, `backend/routes.py`)

One vocabulary read serving everything the forms are generated from, through
the `_read` guard (401 floor; app-token readable — it carries bundled
vocabulary and no stored values, strictly less than the document read already
exposes; the module posture note gains a sentence saying so):

```json
{
  "settings": [
    {"key": "concurrency.wave_max_tasks", "kind": "int", "default": 3,
     "minimum": 1, "maximum": null, "scopes": ["app", "project"],
     "summary": "Leaf tasks the orchestrator dispatches in parallel within one wave."}
  ],
  "source_presets": [
    {"host": "github.com", "program": "gh", "entry": { ...the preset entry... }}
  ],
  "profile_presets": ["quality-first", "budget"],
  "roles": ["design", "review", "implement", "analysis", "setup"],
  "levels": ["authoring", "execution", "delivery", "integration"]
}
```

All values come from the engine's own constants (`settings.py` `_REGISTRY`,
`sources.py` presets, `profiles.py` presets, schema vocabularies) — bundled
data, no document read, so the route is a pure projection and cacheable
client-side for the session.

### Frontend: `FormSurface` restructure (`ConfigPanel.tsx`)

- The pane renders ProjectsTable, SettingsForm, ProfilesForm, SourcesSection +
  SourceForm; the DocumentEditor (with its problems/advisories rendering)
  moves behind one explicit toggle control. Open state is plain component
  state; the JSON view keeps today's editor behavior in full.
- One shared `useStagedEdits` mechanism: staged edits accumulate as
  `(path segments, new value | DELETE)` pairs; `buildFormPatch(edits)` in
  `configDocument.ts` generalizes `buildGridPatch` (same prototype-safe
  containers, `null` for deletions) with the same minimality property.
- `FormReview` generalizes `GridReview`: the literal pretty-printed patch, one
  sentence per staged change (old → new, from a per-form sentence renderer),
  confirm → `writeConfig(patch)`, refusals rendered by path with stored state
  retained (no invalidation on failure), success invalidates config + resolved
  + sources queries — the proven idiom, shared rather than re-implemented.
- After any successful write (form or JSON view), both surfaces re-render from
  a fresh read: they share the same React Query keys, so invalidation is the
  mechanism.

### `SettingsForm`

- Rows generated from `registry.settings`; per-row control by `kind`
  (number input with min/max, checkbox/toggle idiom for bool, text for str).
  The registry `summary` renders as the row's help text; `SETTING_LABEL_KEY`
  labels lead, key as detail line (established idiom).
- Scope selection offers only the setting's `scopes`; writing at project scope
  targets `projects.<name>.<group>.<leaf>`, source scope
  `sources.<name>.<group>.<leaf>`, app scope the top-level path.
- The in-force value + origin render beside each input from the existing
  resolved read; a staged edit is visibly distinct (pending mark idiom).
- A registry entry with an unknown `kind` renders read-only with its raw value
  and a "edit in the JSON view" note — vocabulary-driven fail-open, never a
  crash.

### `ProfilesForm`

- Profile list from the document's `cost_profiles`; per-profile role rows from
  the `roles` vocabulary; model as free text defaulting to `auto` (the engine
  deliberately does not validate entitlement); effort as the level buttons
  idiom. While model is `auto`, the effort control carries the inline sentence
  that a pinned effort takes effect once a concrete model is named.
- "Every project that selected this profile" consequence sentence rendered on
  the form (the resolved read's project list supplies the count).
- Add = copy of a bundled preset (`profile_presets`) or an existing profile
  under a new name; Remove = refused with the selecting projects named while
  any project's `cost_profile` references it (computed from the document).

### `SourceForm`

- Add: preset picker listing `source_presets` with host, program, and what it
  ingests; selecting one stages the deep-copied entry under an operator-chosen
  name with `enabled` absent (inert by construction — the preset's own
  contract). Editable fields: name, enabled, project binding, per-source
  settings (registry-scoped), maintainers list. The poll command and field map
  render read-only beside the preset host. No control anywhere accepts
  command text.
- Edit: same form over a stored entry WHEN its shape is preset-expressible
  (poll argv equal to a preset's, or fields limited to what the form shows);
  otherwise the honest not-expressible state routing to the JSON view.
- Remove: named confirmation, patch `{"sources": {"<name>": null}}`, with the
  stops-ingesting sentence. Enabling a new source carries the begins-polling
  sentence with a link to the source's autonomy grid (the existing
  SourcesSection).

## Data Models

- `RegistryPayload` (TS): mirrors the new route; arrays typed `readonly`,
  rendered not assumed.
- `StagedEdit` (TS): `{ segments: readonly string[], value: unknown | DELETE }`
  — `DELETE` is a sentinel mapping to JSON `null` in the patch (the merge's
  deletion form).
- Backend adds no stored data: the registry route projects existing constants.

## Correctness Properties

### Property 1: A form patch touches only its staged paths

FOR ALL sets of staged edits and FOR ALL starting documents, merging
`buildFormPatch(edits)` through the engine's real `_merge` yields a document
identical to the original at every path other than the staged ones — including
deletions, which remove exactly their own key. Verified with fast-check on the
patch builder and hypothesis through the real `_merge`.

**Validates: Requirements 5.3**

### Property 2: The settings form is total over the registry

FOR ALL settings in the registry vocabulary, the generated form renders
exactly one control whose input kind matches the registry `kind`, carrying the
registry bounds, labeled and help-texted — and an entry with an unrecognized
`kind` renders the read-only fallback rather than nothing. Verified with a
vitest property over generated vocabularies (fast-check), not just the shipped
21.

**Validates: Requirements 2.1, 2.2, 2.3**

### Property 3: A composed source carries only preset commands

FOR ALL bundled presets, the entry the form stages carries `poll` argv
byte-equal to the preset table's own, and no staged source entry path ever
carries argv the preset did not supply. Verified against the real preset
tables (backend hypothesis test on the route payload + frontend fast-check on
the staging function).

**Validates: Requirements 4.2**

## Error Handling

- Registry read failures: the Form_Surface's failed-read state (stated, no
  values rendered); forms never render from retained data (`isError` before
  `data`, the SafetyPanel idiom).
- Write refusals: the engine's errors verbatim by path beside the review card;
  stored state retained; no query invalidation on failure.
- Unknown vocabulary (setting kind, preset host, stored source shape): render
  the honest fallback (read-only row / JSON-view routing), never a crash and
  never a partial form that writes fields it did not show.

## Testing Strategy

- **Backend (pytest)**: registry route tests (payload matches the constants,
  401 floor via the route table, one-read guarantee), the Property 3 backend
  half, plus hypothesis for Property 1's merge half (extending the existing
  sources-properties module).
- **Frontend (vitest + Testing Library + fast-check)**: form-lead/JSON-demoted
  rendering, per-form staging/review/confirm flows, refusal retention,
  fresh-read refresh, effort-on-auto sentence, profile add/remove guards,
  source preset picker/compose/enable/remove flows, not-expressible routing,
  Property 1 (patch builder) and Property 2 (generated vocabulary) properties.
- **Mutation probes**: every claimed guard (JSON never rendered unbidden,
  minimality, no-freeform-argv, profile-removal refusal, staged-vs-stored
  distinction) gets a revert-mutation probe before its review dispatch.
- **Catalogs**: every new string in all 13 catalogs in the task that introduces
  it; pseudolocale regenerated; i18n:check clean except the proven-inherited
  key.

## Design Decisions

| Decision | Rationale |
| --- | --- |
| One registry read for all vocabularies | Forms are projections of engine constants; one route keeps the projection in one place and cacheable |
| Generalize buildGridPatch/GridReview rather than new mechanisms | The grid editing's staged→review→confirm flow is reviewed, property-tested, and proven; three parallel implementations would drift |
| JSON view keeps the full editor | It is the escape hatch for shapes forms cannot express; making it read-only would re-create the dead ends this spec removes |
| Model stays free text with `auto` default | The engine deliberately refuses to validate entitlement; a picker would promise what the engine cannot check |
| Source form is preset-plus-parameters only | The write door validates argv shape, not program membership — the form's constraint is a real boundary, mirroring setup's offer discipline |
| Preset copies stage without `enabled` | The preset contract itself: polling is what arms unattended runs, so a fresh copy must be inert until the operator enables it |
