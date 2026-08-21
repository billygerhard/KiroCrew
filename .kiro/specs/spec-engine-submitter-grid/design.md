# Design Document

## Overview

The submitter-class autonomy grid already exists end to end in the engine:
stored at `sources.<name>.autonomy` as a mapping of submitter class (or `*`)
to spec type (or `*`) to level, resolved class-first by
`AutonomyPolicy.decision(...)` with the fail-closed defaults the requirements
describe. This feature adds no resolution semantics. It adds one read surface
(a backend route that returns every source's fully resolved matrix, computed
by the engine's own resolver) and one operator UI section (a Sources view in
the Configuration pane that renders the matrix, states the semantics, and
edits cells through the existing guarded write door).

Two design facts shape everything below:

- **Resolution stays server-side, in one place.** The UI never re-implements
  class-first precedence or wildcard matching. A new route computes each
  Resolved_Cell by calling `AutonomyPolicy.decision(source=..., spec_type=...,
  submitter_class=...)` per pair — 12 calls per source (4 classes × 3 spec
  types) against the same reader the gates use, so the view and the gates
  cannot disagree.
- **Writes reuse the single fenced door.** `PUT /config` already merges a
  patch recursively into the document (`_merge`: nested maps merge key-wise,
  `None` deletes), schema-validates through the engine, and records the write
  durably. A cell edit is the minimal nested patch
  `{"sources": {"<name>": {"autonomy": {"<class>": {"<type>": "<level>"}}}}}` —
  the merge touches nothing outside the named leaves, which is what makes
  Requirement 4's byte-identical-elsewhere property hold by construction
  rather than by client care. No new write endpoint exists.

## Architecture

```
ConfigPanel (existing)
└── SourcesSection (new)
    ├── source list  ──────────────► GET  .../config/sources   (new, read)
    │                                  └─ AutonomyPolicy.decision() × cells
    ├── grid matrix (per source)
    │     cell = level + origin (exact | wildcard | default)
    ├── semantics copy (R2)
    └── edit flow
          pending edits ─► patch builder (pure TS) ─► review card
          review card    = exact patch + per-edit sentences
          confirm ───────► PUT .../config  { patch }   (existing, guarded)
          on success ────► invalidate sources + config + resolved queries
```

## Components and Interfaces

### Backend: `handle_get_sources` (new, in `backend/routes.py`)

- `GET {PREFIX}/config/sources`, registered through `_read(...)` like the
  other read routes (401 floor; an app token may read — the payload carries
  autonomy levels and config paths, no credential-classified material, and the
  posture note in the module docstring gains a sentence saying so).
- Response shape:

```json
{
  "sources": [
    {
      "name": "gh-issues",
      "grid": {
        "external": {
          "feature": {
            "level": "authoring",
            "declared_at": "sources.gh-issues.autonomy.default.feature",
            "origin": "wildcard",
            "policy_covers_gates": false
          }
        }
      }
    }
  ],
  "submitter_classes": ["maintainer", "member", "contributor", "external"],
  "spec_types": ["feature", "bugfix", "quick"],
  "levels": ["authoring", "execution", "delivery", "integration"]
}
```

Contract facts confirmed at implementation (the sketch above reflects them):
the engine's wildcard key is the literal `"default"` (schema `WILDCARD_KEY`),
not `*`; an unconfigured cell ships `declared_at: ""` (matching the resolved
read's spelling), never `null`; and the resolver entry point is
`AutonomyPolicy.resolve(...)` — `AutonomyDecision` is its return type, not a
method. Frontend types are written from this payload, not from any sketch.

- Vocabularies come from `config/schema.py` constants so the UI renders the
  engine's axes rather than a hard-coded copy.
- `origin` is derived by comparing `declared_at`'s class and type segments to
  the queried pair: `exact` (both match), `wildcard` (either segment is `*`),
  `default` (no `declared_at`; the decision is unconfigured). The
  classification lives beside the route, unit-tested against
  `AutonomyDecision.declared_at`'s real format.
- `policy_covers_gates` is `decision.permits(EXECUTION)` — computed by the
  same predicate the gates use (`gate_is_policy_covered` reduces to it for
  document gates), so R2.4's marker cannot drift from gate behavior.
- A `ConfigValidationError` from a malformed stored grid returns the same
  refusal-by-path shape the resolved-config route uses; the UI renders it as
  a failed read (R1.5), never as values.
- Sources with no `autonomy` field still appear (their matrix is all
  defaults): a configured source with no grid is exactly the fail-closed case
  the operator most needs to see.

### Frontend: `SourcesSection` (new, in `ConfigPanel.tsx` or a sibling file)

- React Query `useQuery` on the new route under a new `QK.sources` key.
  `isError` forces the failed-read state (retained data never rendered —
  the SafetyPanel guard idiom). Loading state is "reading", not an empty
  matrix.
- No sources → the R1.6 empty state naming the Setup Assistant.
- Matrix per selected source: rows = submitter classes (least trusted last,
  schema order), columns = spec types. Each cell shows the resolved level, an
  origin annotation, and — where `policy_covers_gates` — the "policy approves
  document gates" marker. Cells answered by the default render the
  Unconfigured_Default wording (R1.4).
- Semantics copy (R2.1–2.3) renders once in the section, not per cell.
- Edit flow: each cell is a level select; changes accumulate in local state as
  `PendingEdit { source, klass, specType, level }`; nothing is written until
  the review step. The review card shows:
  - the exact JSON patch (the payload itself, pretty-printed — approving a
    plan means approving what will be written, the setup flow's rule),
  - one sentence per edit naming cell, old resolved level and origin, new
    level,
  - the R3.3 warning sentence when an edit raises `external`'s resolved level
    for any spec type,
  - the R3.5 narrowing note when the edited cell was wildcard-answered: the
    write creates the specific cell and leaves the wildcard for other cells.
- Confirm submits through the existing `writeConfig` client function. On
  refusal (`config_write_refused` / `config_invalid`), the errors render
  verbatim by path beside the review card; the grid keeps showing stored
  state (the query is not invalidated on failure). On success, the sources,
  config, and resolved queries are invalidated and the grid re-renders from
  the fresh read (R3.6, R4.3).

### Patch builder (pure function, new)

`buildGridPatch(edits: PendingEdit[]): ConfigPatch` — builds the minimal
nested patch from pending edits. Pure and side-effect free so its isolation
property is directly testable: the patch's only paths are
`sources.<name>.autonomy.<class>.<type>` for the edited cells.

## Data Models

- `SourceGridCell` (TS): `{ level, declared_at: string, origin: 'exact' |
  'wildcard' | 'default', policy_covers_gates: boolean }` — `declared_at` is
  `""` when unconfigured (matching the resolved read's spelling), never
  `null`; consumers branch on `origin`.
- `SourcesPayload` (TS): mirrors the route response; vocabulary arrays typed
  as `readonly string[]` and rendered, not assumed.
- `PendingEdit` (TS): `{ source, klass, specType, level }`.
- Backend adds no stored data model: the route derives everything from the
  document through existing types (`AutonomyPolicy`, `AutonomyDecision`).

## Correctness Properties

### Property 1: A grid patch touches only its own cells

FOR ALL sets of pending edits and FOR ALL starting documents, merging
`buildGridPatch(edits)` into the document (the engine's `_merge` semantics)
yields a document identical to the original at every path other than
`sources.<name>.autonomy.<class>.<type>` for the edited cells. Verified with
fast-check on the TS side (patch shape) and hypothesis on the Python side
(merge outcome through the real `_merge`).

**Validates: Requirements 4.1, 4.2**

### Property 2: Origin classification is total and faithful

FOR ALL stored grids expressible in the schema's vocabulary and FOR ALL
(class, type) pairs, the route's resolved cell carries the level
`AutonomyPolicy.decision` returns, and its `origin` agrees with
`declared_at`: `default` iff `declared_at` is absent, `exact` iff both path
segments equal the queried pair, `wildcard` otherwise. Verified with
hypothesis against the real resolver.

**Validates: Requirements 1.3, 1.4**

## Error Handling

- Route read failures and malformed-grid `ConfigValidationError`s produce the
  refusal-by-path shape; the UI's failed-read state names the failure and
  renders no values (R1.5).
- Write refusals surface the engine's own errors verbatim by path (R3.4); the
  handler already reports them and keeps no validation of its own.
- An unknown vocabulary value in a stored document (hand-edited) is the
  engine's problem to refuse; the UI renders vocabularies from the payload so
  it cannot crash on axes it did not expect.

## Testing Strategy

- **Backend (pytest, in the spec_engine test tree):** route tests for the
  matrix (stored exact cell, wildcard row, empty grid, absent autonomy field,
  no sources), origin classification unit tests, the 401 floor, malformed-grid
  refusal, and the two hypothesis properties above.
- **Frontend (vitest + Testing Library, existing SpecEngine harness):**
  rendering tests (matrix, origins, default wording, empty state, failed
  read shows no values), semantics copy presence, edit flow (pending edit →
  review card shows exact patch + sentences → confirm writes → fresh-read
  refresh), the R3.3 external-raise warning, the R3.5 narrowing note, refusal
  rendering with stored state retained, and fast-check on `buildGridPatch`.
- **Mutation probes:** each claimed guard (failed-read doubt state, external
  warning, wildcard narrowing, refusal retention) gets a revert-mutation probe
  before its review dispatch — plant the exact regression, confirm the named
  test fails, restore byte-identical.
- **Catalogs:** every new string in all 13 catalogs; pseudolocale regenerated;
  `i18n:check` clean except the one proven-inherited key.

## Design Decisions

| Decision | Rationale |
| --- | --- |
| New read route rather than client-side resolution | One resolver: the gates and the view read the same code path; a TS re-implementation would drift |
| Per-cell `decision()` calls rather than a bulk resolver API | 12 calls per source against an in-memory document is trivial; no new engine API surface to maintain |
| Writes through existing `PUT /config` | The engine's one-door principle; the deep-merge patch gives Requirement 4 by construction |
| Patch builder as a pure TS function | Makes the isolation property directly property-testable with fast-check |
| Vocabularies shipped in the payload | The UI renders the engine's axes; a schema change shows up without a frontend edit |
| Edit writes the specific cell, never the wildcard | Editing a wildcard silently changes unqueried cells; narrowing is the least-surprise write (R3.5) |
