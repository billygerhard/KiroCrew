# Implementation Plan: Spec Engine Own Surface

## Overview

Restore the Prior_App to its Merge_Base state, fence the app boundary, expose
setup and configuration as MCP tools, then build the Spec_App's own page from
selected mockups. Eight waves; the restoration and the MCP tools land first so
no capability is surfaceless for longer than a wave.

## Tasks

- [x] 1. Restore the Prior App
  - [x] 1.1 Revert every Prior_App file to the Merge_Base
    - Compute the Merge_Base (`git merge-base origin/main HEAD`) and inventory every modified or added file under `src/kiro_crew/apps/builtins/spec_builder/`, `website/src/apps/spec-builder/`, and `website/src/test/SpecBuilder*` from `git diff --name-status`, never from notes
    - Restore modified files with `git checkout <merge-base> -- <path>`; delete files this branch added under those trees
    - Verify the Prior_App's own backend test suite passes at its Merge_Base count (measure the count from the Merge_Base tree, do not assume it)
    - For each reverted hunk, record the Requirement 1.4 verdict: did it fix a defect present in the Prior_App at the Merge_Base, or only adapt the Prior_App to our engine? Record the verdict and reasoning in the task record; the expected answer is that no genuine Prior_App defect was fixed
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5_
  - [x] 1.2 Post-removal gate sweep
    - With the Prior_App restored and the run surface deleted, run every gate: spec_engine pytest suite, spec_builder pytest suite (Merge_Base count), flake8/isort/black/mypy including tests, `tsc -b`, vitest, and the manifest-sync script checked by its real exit status
    - Remove any spec_engine test or import that depended on the deleted `engine_ops.py` surface; the engine library itself must lose no test
    - Confirm the app-store entry for spec-engine still satisfies the manifest-sync gate while the app has no page
    - _Requirements: 1.1, 1.5_
- [x] 2. Fence the boundary
  - [x] 2.1 App_Boundary_Fence build gate
    - New test in the spec_engine suite: compute the Merge_Base and FAIL (never pass) when it cannot be computed; list every branch-changed file; assert each lies under a declared Spec_App root or matches `BOUNDARY_ALLOWLIST`, a literal tuple of (path-prefix, one-line justification)
    - Seed the allowlist with the legitimately shared files: the app-store manifest key table, the localization catalogs, the manifest-sync script, and the spec documents
    - Plant violations assembled at runtime (a path under the Prior_App tree; an undeclared new root) and assert each is reported; prove the fail-closed branch by driving the Merge_Base computation to fail in a fixture repo
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5_
- [x] 3. Agent surface for setup and configuration
  - [x] 3.1 Setup assistant tools on the Engine_MCP_Server
    - Add `inspect_setup`, `plan_setup`, and `apply_setup` to the TOOLS table, delegating to `engine/setup.py`; `inspect_setup` returns evidence, inferences, open questions, and preset offers including each preset's declared programs
    - `plan_setup` computes and returns a SetupPlanEnvelope with a deterministic content-hash `plan_id` and applies nothing; `apply_setup` recomputes the plan, refuses on `plan_id` mismatch, requires a non-empty `approver`, and surfaces `SetupApprovalRequired` and `InferredSubjectRefused` as structured refusals
    - Property test: `plan_id` equality is total over canonical plan inputs, and a stale `plan_id` always refuses (design Property 2)
    - Trace every new catch clause against the class chain the setup module actually raises
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5_
  - [x] 3.2 Configuration tools on the Engine_MCP_Server
    - Add `get_config` and `write_config` to the TOOLS table; `get_config` elides secret-classified values by key name; `write_config` delegates to the existing fenced `EngineOperations.write_config` and rejects everything the Config_Store refuses, including vendored-provider bindings on every transport
    - Config writes run off the event loop
    - _Requirements: 4.1, 4.2, 4.3, 4.5_
  - [x] 3.3 Conformance for the five new tools
    - Drive all five through the existing stdio conformance harness: tools/list advertises them with schemas, tools/call round-trips each, error shapes are structured refusals not stack traces
    - Non-vacuity: a positive control per tool proving the harness observes a real effect (a written config, a returned plan), not merely a 200-shaped reply
    - _Requirements: 3.1, 3.2, 3.3, 4.1, 4.2_
- [ ] 4. The app's own surface plumbing
  - [x] 4.1 Join BUILTIN_NAMES deliberately
    - Add `spec_engine` to `BUILTIN_NAMES`; rewrite the pin test's recorded reasoning to state what the entry now buys (the dashboard route-registration loop) and keep the test failing for an entry added without reading it
    - _Requirements: 5.1, 5.3_
  - [ ] 4.2 Backend routes under the Spec_App
    - New `spec_engine/backend/routes.py` registering `/api/apps/spec-engine/*`: queue snapshot, queue actions (release-feedback, redispatch, clean-workspace, teardown), config get/put, kill-switch get/post, run spend
    - Every mutating handler passes an operator-only guard that refuses app-minted tokens with 403 plus a security event, tested at route level; every file and database read runs off the event loop
    - Write handlers against the engine library directly; the deleted surface in git history is a capability checklist, not a source to copy
    - Config route and `write_config` tool produce byte-identical files for the same patch (design Property 1)
    - _Requirements: 5.1, 5.3, 4.4_
  - [ ] 4.3 Posture distinction: inbound serving versus outbound transmission
    - Engine trees keep the full network denylist; `backend/` alone may import aiohttp, with an AST check asserting no outbound constructor reference (`ClientSession`, `request`, `TCPConnector`, `UnixConnector`); state the distinction in the boundary docstring and fail when it cannot be drawn
    - Planted cases in both directions: an aiohttp import inside `engine/` is reported, and an outbound constructor reference inside `backend/` is reported
    - Extend the frontend provenance scan roots to `website/src/apps/spec-engine/`
    - _Requirements: 7.1, 7.2, 7.3, 7.4, 7.5_
  - [x] 4.4 Manifest page and localization
    - Add `ui.pages` (route `/spec-engine`) and `backend.routes` to the manifest; retire `test_declares_no_ui` with a reasoned replacement; keep `defaultEnabled` false
    - Add `page_label` and any new strings to all thirteen catalogs (generated pseudo-locale via the script, countless strings, Korean dual particles, context entries for short strings); restore `pageLabel` to the app-store key table for spec-engine; manifest-sync gate green by real exit status
    - _Requirements: 5.1, 5.2, 5.5_
- [ ] 5. Design before pixels
  - [ ] 5.1 Mockups and recorded selection
    - Produce at least two materially different self-contained HTML mockups of the Operator_Surface (layout, density, and interaction model must differ, not shades of one idea), built with the dashboard's real theme tokens
    - Cover: run review and operation as the primary activity, first-run setup entry, review queue with bounded untrusted text, config editing, kill switch and spend
    - A REVIEWER agent selects one against recorded criteria; commit mockups plus the written rationale; mark the selection VETO-PENDING for the owner in the task record and final report
    - _Requirements: 6.1, 6.3_
- [ ] 6. The Operator_Surface, from the selected mockup
  - [ ] 6.1 Page shell, routing, and API client
    - `website/src/apps/spec-engine/`: `SpecEnginePage.tsx` matching the selected mockup's layout, an `api.ts` for `/api/apps/spec-engine/*` written against the real route handlers, and first-run detection that leads with the setup flow when `get_config` reports no configuration
    - _Requirements: 5.4, 6.2, 6.5_
  - [ ] 6.2 Review queue panel
    - Runs grouped by state, queue actions wired to the backend routes, untrusted submitter text rendered through the display contract with line-bounded layout; a teardown that keeps workspaces must surface the kept ids and must not report itself complete
    - _Requirements: 6.2, 6.3, 6.4, 6.5_
  - [ ] 6.3 Config editor and setup flow
    - Config editing over the backend config route with segment-wise role matching and accurately labeled per-role reset; the setup flow drives inspect/plan/apply through the backend and requires the human approver identity before apply
    - _Requirements: 6.2, 6.5, 5.4, 3.3_
  - [ ] 6.4 Kill switch and spend panel
    - Kill-switch engage/release and per-run spend display over the backend routes; engage/release confirmed by reading back persisted state, not by response status alone
    - _Requirements: 6.2, 6.5_
- [ ] 7. End-to-end verification
  - [ ] 7.1 Write-path equivalence and fence verification
    - Property test driving the same patches through the MCP tool and the backend route against identical starting stores, asserting byte-identical `config.json` (design Property 1); App_Boundary_Fence green on the finished branch; mutation probes for every property claimed in tasks 2.1, 3.1, and 4.3
    - _Requirements: 4.4, 2.1_
  - [ ] 7.2 Final sweep and dispositions
    - All gates green (both pytest suites, lint/type gates including tests, tsc, vitest, manifest-sync, boundary fence); record in the prior spec's tasks.md that its one-app design intent is superseded by this spec; final report lists the VETO-PENDING mockup selection and any Requirement 1.4 findings
    - _Requirements: 1.1, 5.2, 6.1_

## Task Dependency Graph

```json
{"waves": [
  {"id": 0, "tasks": ["1.1", "3.1"]},
  {"id": 1, "tasks": ["1.2", "2.1", "3.2"]},
  {"id": 2, "tasks": ["3.3", "4.1", "4.4"]},
  {"id": 3, "tasks": ["4.2", "5.1"]},
  {"id": 4, "tasks": ["4.3", "6.1"]},
  {"id": 5, "tasks": ["6.2", "7.1"]},
  {"id": 6, "tasks": ["6.3"]},
  {"id": 7, "tasks": ["6.4", "7.2"]}
]}
```

## Notes

- The Merge_Base is the authority for the Prior_App's content; every restoration decision derives from `git diff` against it, never from notes or memory.
- Task 5.1's mockup selection is made by a reviewer agent and is VETO-PENDING for the owner; overturning it re-runs only waves 4-7's frontend tasks.
- All tests run offline; the fence and posture checks are ordinary pytest tests in the Spec_App's suite, so CI needs no new steps.
- The config file remains JSON by the owner's explicit decision.

### Findings carried forward by the review gate

- **For 7.2 (owns "all gates green"):** 38 black-dirty files inside spec_engine's own tree were ADDED by this branch (13 engine modules + 25 test modules, cosmetic wrapping under the pinned black==26.3.1) — branch-contributed debt, not inherited repo dirt; CI has black commented out, so nothing blocks mechanically. 7.2 decides: format them or record why not.
- **For 4.2 (owns the config surface's consumers):** `config_payload()` assembles its reply from three unlocked reads (document, validate, advisories) — a write landing between them yields a torn REPORT (never a torn document). Pass the already-read document into validate/advisories when building the routes.
- **Small documentation debts, fix opportunistically in whichever task next touches the file:** (a) state the secret classifier's residual in `engine/config/store.py` (a credential under an innocent last segment with a low-entropy value is not withheld; the repo's "What this cannot see" convention applies); (b) `_unschedule`'s docstring says "from the graph block" but replaces document-wide — scope it to `plan.graph_block.body`; (c) the no-scheduled-leaves rot path raises a bare StopIteration — give it a message.

- **R1.4 final-report item (from 1.1's review):** no reverted hunk fixed a genuine Merge_Base defect of the Prior_App. One observation for its owner: the Prior_App's `_seed_prompt` docstring claims a builtin's declared skills "are NOT on the agent's skill path", but `bridges.reconcile_app_skills` links manifest-declared skills for enabled non-self-managed apps, so the claim appears factually wrong at the Merge_Base. Not fixed (byte-identity forbids it); report to the owner.
- **For 1.2's gate sweep:** two Prior_App files (`backend/routes.py`, `tests/test_routes.py`) are black-dirty AT the Merge_Base; the sweep must treat them as pre-existing, not reformat them.
- **For 3.2 (owns the config door):** (a) the approver identity demanded by `apply_setup` is echoed but recorded nowhere durable — `ConfigStore.write` logs only surface name and key count; give the write path a durable approver record. (b) `apply_setup` drops `ConfigStore.write`'s merge advisories from the tool reply (pre-existing engine shape, newly exposed) — plumb the warn recorder into the reply. (c) `CONFIGURATION_ONLY` in the library-equivalence fence lacks the completeness pin its sibling categories have — add it so a future tool cannot join the partition undriven.

- **From 5.1 — the mockup selection is VETO-PENDING for Billy.** A reviewer agent
  selected **`mockup-b.html` ("Operator Console")** over `mockup-a.html`
  ("Triage Board") against criteria recorded before judging. Everything —
  criteria, the reviewer's per-criterion comparison, the post-selection
  corrections applied to the winner, and the open holes — is in
  `website/src/apps/spec-engine/design/selection.md`. Overturning it re-runs
  tasks 6.1–6.4 only.
  - **For 6.1:** B passes the "safety controls never behind navigation"
    criterion *only because it contains no overlay*. The kill switch and spend
    strip is a grid row of the page shell. A drawer, modal or scrim added later
    silently reintroduces the failure the losing mockup had.
  - **For 6.2:** the documents-and-findings view on B's verdict pane was added
    *after* the review, to close the reviewer's finding that neither mockup
    showed enough to render a verdict on. It is the piece of the selected design
    with the least review behind it.
  - **For 6.2:** `revision_exhausted` still has no legitimate distinct action.
    B's corrected mockup offers "raise the revision limit and retry" —
    confirm the engine supports raising it for one gate before shipping that
    control.
  - **For 6.3:** the mockups' config pane is not a second write path.
    `config.json` is shown as the write path and the resolved view beside it is
    a read, per Property 1.
  - **Do NOT port** `mockup-a.html`'s `Rewrite the gate myself` button. It is
    left in the losing artifact only so the file still matches the recorded
    comparison; it is the hand-authoring affordance Requirement 6.3 excludes.
  - Task 5.1 also widened `UI_SUFFIXES` in `test_public_build_posture.py` to
    `.html`/`.md` so the design artifacts are provenance-scanned rather than
    exempted. **4.3 owns the scan ROOTS and must not narrow the suffixes.**

- **For 6.1 (from 4.4's review, two unrecorded shared-file obligations):** `website/src/apps/builtinIcons.tsx` must register `Cog` (unregistered today — the app store falls back to the generic Package glyph) and `website/src/apps/builtinRegistry.ts` must map `/spec-engine` (unmapped today — the route redirects to /chat). Both files sit outside the declared roots, so each needs its own reviewed BOUNDARY_ALLOWLIST entry with a one-line justification.
- **For 7.2's sweep (from 4.1's and 4.4's reviews):** (a) assert three-way terminal coherence — manifest `backend.routes` field ⟷ `backend/routes.py` on disk ⟷ package attribute — once 4.2 lands (4.2 may close it itself); (b) `node scripts/i18n-check.mjs` exits 1 on `pages.artifactDeployPage.domain` (trailing-connector), upstream drift inherited from the merge-base, not branch work — disposition it (fix or record as inherited); (c) the fence's allowlist justification for `check-app-manifest-sync.mjs` reads "made bidirectional for a card with no page" but 4.4 removed the last pageless card — refresh the justification; the gate's pageless else-branch is now driven by no shipped manifest (vitest pairing covers that direction).
- **Bookkeeping (no action):** commit `aa201071d` swept 3.3's two conformance modules in under 4.4's message — content verified intact by 4.4's reviewer (reflog traced, 3166 green); attribution note only.
