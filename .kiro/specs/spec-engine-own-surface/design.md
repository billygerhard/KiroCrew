# Design Document

## Overview

Three moves, strictly ordered by risk. First, restore the Prior_App
(`spec_builder`) to its Merge_Base state and fence the boundary so the trespass
cannot recur. Second, give the Engine_MCP_Server the five tools that make the
engine operable headlessly: setup inspection, setup planning, setup application,
configuration read, and configuration write. Third, give the Spec_App its own
page — manifest, routes, localization, and components designed from mockups —
in its own directories.

The restoration deletes the only callers of the engine's run surface
(`engine_ops.py` and the panels inside the Prior_App's trees), so the MCP tools
land in the same early waves: the engine must not pass through a state where a
capability that had a surface has none for longer than a wave.

One approved requirement is in tension with this run's mandate. Requirement 6.1
requires mockups to be "presented for selection", and the owner has directed the
run to complete without him. Resolution: the mockup task produces the mockups,
a REVIEWER agent selects one against recorded criteria, the rationale is
committed alongside the mockups, and the selection is flagged in the final
report as VETO-PENDING. The owner can overturn it at the cost of re-running only
the frontend wave; nothing upstream depends on which mockup wins.

## Architecture

```
                       agents (any MCP client)
                              │
                    Engine_MCP_Server (stdio)
       authoring: get_*_prompt, validate_spec, get_phase, list_tasks,
                  record_approval, advance_phase, run_doctor,
                  check_run_prerequisites
       NEW setup: inspect_setup, plan_setup, apply_setup
       NEW config: get_config, write_config
                              │
                        Spec_Engine library
        (phases, native format, setup.py, config store, run state,
         budget, kill switch, review queue, delivery)
                              │
              ┌───────────────┴───────────────┐
     spec_engine/backend/routes.py    website/src/apps/spec-engine/
      (NEW: aiohttp.web, inbound       (NEW: page shell, review queue,
       only; queue/config/kill-         config editor + setup flow,
       switch/spend/teardown;           kill switch + spend; from the
       operator-only guard)             selected mockup)

     spec_builder/** : byte-identical to Merge_Base. Never imported,
     never modified. App_Boundary_Fence fails the build otherwise.
```

Route registration for builtins dispatches through `BUILTIN_NAMES` in
`apps/builtins/__init__.py`. The prior spec deliberately kept `spec_engine` out
of that list because the app had nothing the list dispatches; this design gives
it routes, so the app joins the list and the pin test's recorded reasoning is
rewritten rather than deleted — the test must still fail for an entry added
without reading it.

## Components and Interfaces

### Restoration (Requirement 1)

Pure git surgery driven by the Merge_Base: every Prior_App-owned path that
differs is restored with `git checkout <merge-base> -- <path>`; every file this
branch added under their trees is deleted. Their trees are:
`src/kiro_crew/apps/builtins/spec_builder/`, `website/src/apps/spec-builder/`,
and `website/src/test/SpecBuilder*`. The inventory comes from
`git diff --name-status <merge-base>..HEAD` over those roots — never from
memory or notes. Requirement 1.4's check: re-read every reverted hunk and ask
whether it fixed a defect present in the Prior_App AT the Merge_Base (as
opposed to adapting the Prior_App to our engine, which is not their defect);
the expected verdict is "none", and the verdict with its reasoning is recorded
in the task record.

### App_Boundary_Fence (Requirement 2)

A test in the Spec_App's own suite. It computes the Merge_Base with
`git merge-base origin/main HEAD`; if that fails (no `origin/main`, detached
tree, shallow clone) the test FAILS — a fence that cannot compute its baseline
must not report clean. It then lists every file changed on the branch and
asserts each resides under a declared Spec_App root or appears in
`BOUNDARY_ALLOWLIST`, a literal tuple in the test file whose every entry
carries a one-line justification. Initial allowlist: the shared app-store
manifest table, the localization catalogs, the manifest-sync script, the spec
documents, and the repo-root scratch patterns. Planted violations are assembled
at runtime (a path under the Prior_App's tree), following the provenance
suite's convention.

### Engine_MCP_Server additions (Requirements 3, 4)

Five new entries in the `TOOLS` table, all delegating to `EngineOperations`:

| Tool | Delegates to | Notes |
|---|---|---|
| `inspect_setup(project)` | `setup.inspect_project` | returns evidence, inferences, open questions, preset offers with their declared programs |
| `plan_setup(project, answers)` | `SetupAnswers` -> `SetupPlan` | pure: computes and returns the plan, applies nothing |
| `apply_setup(project, plan_id, approver)` | plan application | refuses without a non-empty `approver`; surfaces `SetupApprovalRequired` and `InferredSubjectRefused` as structured refusals, not stack traces |
| `get_config()` | `ConfigStore` read | secret-classified values elided by key name |
| `write_config(patch)` | `EngineOperations.write_config` | the existing single fenced door; vendored-provider refusal applies unconditionally across transports |

`plan_setup`/`apply_setup` bridge a stateless protocol to a two-step flow: the
plan returned by `plan_setup` carries a deterministic `plan_id` (content hash),
and `apply_setup` recomputes the plan from the same inputs and refuses if the
hash differs — no server-side session state, no stale-plan application.

### Spec_App surface (Requirements 5, 6)

- **Manifest**: `ui.pages` gains route `/spec-engine`, plus
  `backend.routes: backend.routes:register_routes`. `defaultEnabled` stays
  `false`.
- **Backend**: new `spec_engine/backend/routes.py`, the app's only aiohttp
  importer. Handlers: queue snapshot, queue actions (release-feedback,
  redispatch, clean-workspace, teardown), config get/put, kill-switch get/post,
  run spend. All mutating handlers pass an operator-only guard that refuses
  app-minted tokens with 403 plus a security-event record; all file and
  database reads run off the event loop. The handlers are written against the
  engine library directly; the deleted `engine_ops.py` may be consulted via git
  history as a capability checklist, never copied wholesale, because it carries
  the Prior_App's route prefix and idioms.
- **Frontend**: `website/src/apps/spec-engine/` — `SpecEnginePage.tsx`, an
  `api.ts` client for `/api/apps/spec-engine/*`, and one component per panel.
  Untrusted text rendered through the display contract with the line-bounding
  the review-queue threat model requires.
- **First-run**: when `get_config` reports no configuration, the page's primary
  action is the setup flow (Requirement 5.4), which drives the same three MCP
  operations through the backend rather than reimplementing them.

### Provenance posture under an inbound surface (Requirement 7)

The import fence currently denylists `aiohttp` everywhere in the app. The new
rule: the engine trees (`engine/`, `engine_mcp/`) keep the full denylist;
`backend/` alone may import `aiohttp`, and a companion AST check asserts
`backend/` never references an outbound constructor (`ClientSession`,
`request`, `TCPConnector`, `UnixConnector`) — the distinction drawn is
"declares handlers versus constructs a client", it is stated in the boundary
docstring, and where it cannot be drawn (a new network module appearing in
`backend/`) the check fails. Planted cases cover both directions: an aiohttp
import inside `engine/` and a `ClientSession` reference inside `backend/`.

### Localization

The page makes `spec-engine` an app WITH a page, so the manifest-sync gate now
requires `page_label` in every catalog — the reverse direction of the gate
built during the prior spec, which was made bidirectional for exactly this
transition. Countless strings, no `{{count}}`, Korean particles in dual form,
and the three-character-or-shorter context entries, per the catalog rules.

## Data Models

- **SetupPlanEnvelope** (new, MCP boundary): `{plan_id: str, inferences: [...],
  answers_used: {...}, config_patch: {...}, warnings: [...]}`. `plan_id` is a
  SHA-256 over the canonical JSON of `(project_subject, answers_used,
  config_patch)`.
- **BOUNDARY_ALLOWLIST** (new, fence): `tuple[tuple[str, str], ...]` of
  (path-prefix, justification).
- Config file stays `config.json` (owner's decision: JSON, not YAML), written
  only by `ConfigStore.write`.
- No new run-state tables; the surface reads existing ones.

## Correctness Properties

### Property 1: Config write-path equivalence

**Validates: Requirements 4.2, 4.4**

FOR ALL configuration patches accepted by both surfaces, applying the patch
through the Engine_MCP_Server's `write_config` and applying the same patch to
an identical starting store through the Operator_Surface route yields
byte-identical `config.json` files. (Covers 4.2, 4.4)

### Property 2: Plan identity is total over its inputs

**Validates: Requirements 3.2, 3.3**

FOR ALL pairs of setup inputs, `plan_id` is equal exactly when the canonical
plan inputs are equal — differing answers, subject, or patch always produce a
differing `plan_id`, and `apply_setup` with a stale `plan_id` always refuses.
(Covers 3.2, 3.3)

### Property 3: The fence admits exactly the declared territory

**Validates: Requirements 2.1, 2.4**

FOR ALL file paths, the App_Boundary_Fence classifies a path as in-bounds
exactly when it lies under a declared Spec_App root or matches an allowlist
entry — no path is both reported and admitted, and no path is neither. (Covers
2.1, 2.4)

## Error Handling

- `apply_setup` without an approver: structured refusal `{refused:
  "approver-required"}`, never a write. `InferredSubjectRefused` and
  `SetupApprovalRequired` surface with their names and messages; every other
  exception from the setup path is a tool error, not a silent empty plan.
- Fence with no computable Merge_Base: test failure with the reason, never a
  pass. This is the fail-closed rule the prior spec learned three times.
- Operator-only guard: app-token callers get 403 and a security event; the
  refusal is tested at route level, not only at the guard function.
- Catch clauses in every new module are written against the RAISED class chain,
  verified by tracing the raising code — the dominant defect class of the prior
  spec was a catch tuple that could not catch what is raised.

## Testing Strategy

Unit tests per component in the Spec_App's suites (pytest for engine and
backend, vitest for the page). Property-based tests with `hypothesis` for the
three correctness properties. The five new MCP tools are additionally driven
through the existing stdio conformance harness end to end (list, call, error
shapes). Mutation probes accompany every property-shaped claim: commit first,
apply one mutation with a grepped marker, require the specific covering test to
fail with a test failure (never a usage error read as failure), restore
byte-identical. Gates for every task: engine and backend pytest suites, flake8,
isort, black under the repo config, mypy including tests, `tsc -b`, vitest for
touched frontend, the manifest-sync script checked by real exit status, and —
once it lands — the App_Boundary_Fence.
