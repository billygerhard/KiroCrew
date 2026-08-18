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
- [x] 4. The app's own surface plumbing
  - [x] 4.1 Join BUILTIN_NAMES deliberately
    - Add `spec_engine` to `BUILTIN_NAMES`; rewrite the pin test's recorded reasoning to state what the entry now buys (the dashboard route-registration loop) and keep the test failing for an entry added without reading it
    - _Requirements: 5.1, 5.3_
  - [x] 4.2 Backend routes under the Spec_App
    - New `spec_engine/backend/routes.py` registering `/api/apps/spec-engine/*`: queue snapshot, queue actions (release-feedback, redispatch, clean-workspace, teardown), config get/put, kill-switch get/post, run spend
    - Every mutating handler passes an operator-only guard that refuses app-minted tokens with 403 plus a security event, tested at route level; every file and database read runs off the event loop
    - Write handlers against the engine library directly; the deleted surface in git history is a capability checklist, not a source to copy
    - Config route and `write_config` tool produce byte-identical files for the same patch (design Property 1)
    - _Requirements: 5.1, 5.3, 4.4_
  - [x] 4.3 Posture distinction: inbound serving versus outbound transmission
    - Engine trees keep the full network denylist; `backend/` alone may import aiohttp, with an AST check asserting no outbound constructor reference (`ClientSession`, `request`, `TCPConnector`, `UnixConnector`); state the distinction in the boundary docstring and fail when it cannot be drawn
    - Planted cases in both directions: an aiohttp import inside `engine/` is reported, and an outbound constructor reference inside `backend/` is reported
    - Extend the frontend provenance scan roots to `website/src/apps/spec-engine/`
    - _Requirements: 7.1, 7.2, 7.3, 7.4, 7.5_
  - [x] 4.4 Manifest page and localization
    - Add `ui.pages` (route `/spec-engine`) and `backend.routes` to the manifest; retire `test_declares_no_ui` with a reasoned replacement; keep `defaultEnabled` false
    - Add `page_label` and any new strings to all thirteen catalogs (generated pseudo-locale via the script, countless strings, Korean dual particles, context entries for short strings); restore `pageLabel` to the app-store key table for spec-engine; manifest-sync gate green by real exit status
    - _Requirements: 5.1, 5.2, 5.5_
- [x] 5. Design before pixels
  - [x] 5.1 Mockups and recorded selection
    - Produce at least two materially different self-contained HTML mockups of the Operator_Surface (layout, density, and interaction model must differ, not shades of one idea), built with the dashboard's real theme tokens
    - Cover: run review and operation as the primary activity, first-run setup entry, review queue with bounded untrusted text, config editing, kill switch and spend
    - A REVIEWER agent selects one against recorded criteria; commit mockups plus the written rationale; mark the selection VETO-PENDING for the owner in the task record and final report
    - _Requirements: 6.1, 6.3_
- [x] 6. The Operator_Surface, from the selected mockup
  - [x] 6.1 Page shell, routing, and API client
    - `website/src/apps/spec-engine/`: `SpecEnginePage.tsx` matching the selected mockup's layout, an `api.ts` for `/api/apps/spec-engine/*` written against the real route handlers, and first-run detection that leads with the setup flow when `get_config` reports no configuration
    - _Requirements: 5.4, 6.2, 6.5_
  - [x] 6.2 Review queue panel
    - Runs grouped by state, queue actions wired to the backend routes, untrusted submitter text rendered through the display contract with line-bounded layout; a teardown that keeps workspaces must surface the kept ids and must not report itself complete
    - _Requirements: 6.2, 6.3, 6.4, 6.5_
  - [x] 6.3 Config editor and setup flow
    - Config editing over the backend config route with segment-wise role matching and accurately labeled per-role reset; the setup flow drives inspect/plan/apply through the backend and requires the human approver identity before apply
    - _Requirements: 6.2, 6.5, 5.4, 3.3_
  - [x] 6.4 Kill switch and spend panel
    - Kill-switch engage/release and per-run spend display over the backend routes; engage/release confirmed by reading back persisted state, not by response status alone
    - _Requirements: 6.2, 6.5_
- [ ] 7. End-to-end verification
  - [x] 7.1 Write-path equivalence and fence verification
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
- **For 6.3 (from 4.2's review):** the backend serves only the PERSISTED config document — the deleted predecessor also served an effective/resolved view (`ConfigStore.effective_settings`, value-in-force plus origin, narrowed by project/source) and no route exposes it now. The selected mockup shows config.json as the write path and the resolved view beside it as a read, so 6.3 must either add that read route or record the narrowed scope as a deliberate disposition.
- **For 6.2 and 6.4 (from 5.1's review):** mockup-b's docked inspector is STATIC below its header — selecting the budget-parked or closed run still shows the first run's documents/findings/spend/teardown. Treat the inspector's per-row binding as unresolved design, not fidelity: bind those panes to the selected row for real.
- **For 6.2 (from 6.1's review):** the corrected mockup's row-level state words (`revisions spent`, `N held`, `N workspaces kept` — post-selection correction 3 in design/selection.md) are absent from the shell's rows; `revision_exhausted` surfaces only in the inspector's why line, and `feedback_quarantined`/`feedback_needs_human` are transcribed into QueueEntry but rendered nowhere. 6.2 owns rendering them on the row per the corrected mockup.
- **For 6.2/6.4 (from 6.1's round-2 review, strip and shell leftovers):** (a) the strip renders confident zeros while the queue read is PENDING — branch on isPending too (the error state is fixed; pending is the same argument one state earlier); (b) on a failed kill-switch read the dot stays green (`data-engaged="false"` → var(--ok)) beside text saying the switch could not be read — doubt must not read released on the visual channel; the test pins only the text; (c) the `pane === null` pending shell has no covering test; (d) api.ts's REFUSAL docstring still overstates — the page branches on no code today, and the `__testing` re-export of REFUSAL is dead; restate or drop when the first real branch lands.
- **Disposition (mockup narrowing, from 6.1's round-2 review):** mockup-b carries working/closed filters, an "In flight" count and a ceiling meter, but `/queue` vends only runs waiting on a person and no route vends a global ceiling — the shell's narrowing is correct given the routes. Recorded so 6.2/6.4 treat those affordances as requiring either a new read route or a recorded omission, not as shell bugs. Also covers the STATE-grouping dimension (from 6.2's round-3 review): the task text says "runs grouped by state" and the queue API relays the engine's `grouped` map, but the SELECTED mockup's presentation is one ordered table with waiting-on filters, and the selection supersedes the earlier wording — the grouped map stays relayed (not re-derived) for any surface that wants it; run state renders in the inspector subheading.
- **For 7.2 (from 6.2's review):** post-selection correction 5 put a bounded per-document view on the verdict pane; only the findings half shipped, with an in-UI note that no route serves document bodies. Disposition needed: add a guarded document-body read route, or record the omission beside the resolved-config-view note. Also from 7.1's review: the repo-root `tmp/` fence entry now admits 363 scratch files including `.py` copies of engine modules — sweep-worthy hygiene, not a defect; and the equivalence suite's credential-key reach is pinned only by a hand-written case (the hypothesis.find block covers the other four shapes). From 6.3's review: the `.se-pending` rule in styles.ts is dead CSS (its Pending component was deleted when the panels landed) — remove it once 6.4's concurrent edit of that file settles. From 6.1's round-2 review: the `pane === null` pending shell still has no covering test if 6.4 did not add one.
- **Disposition (absent verdict controls, from 6.2's round-2 review):** five of the selected mockup's six verdict ACTIONS (approve gate, request changes, cancel run, raise ceiling, resume) shipped as prose notes naming their real transport (or, for cancel, the absence of any) because no HTTP route exists — the design's backend route list scoped them out. Recorded here so requirement 6.2 fidelity is decided in the open: the departure is a route-surface decision inherited from the design, not a panel bug; a future task adding those routes re-opens the controls.
- **Disposition (criteria checkability, from 5.1's review):** criteria.md's "checkable rather than asserted" ordering claim is overstated — all five design files land in one commit, so criteria-before-judging is not verifiable from history. Process-conformant (disclosed, owner-vetoable) and the selection stands; recorded so the claim is not repeated as fact.
- **For 7.2's sweep (from 4.1's and 4.4's reviews):** (a) assert three-way terminal coherence — manifest `backend.routes` field ⟷ `backend/routes.py` on disk ⟷ package attribute — once 4.2 lands (4.2 may close it itself); (b) `node scripts/i18n-check.mjs` exits 1 on `pages.artifactDeployPage.domain` (trailing-connector), upstream drift inherited from the merge-base, not branch work — disposition it (fix or record as inherited); (c) the fence's allowlist justification for `check-app-manifest-sync.mjs` reads "made bidirectional for a card with no page" but 4.4 removed the last pageless card — refresh the justification; the gate's pageless else-branch is now driven by no shipped manifest (vitest pairing covers that direction).
- **For 7.2 (from 6.4's round-3 review, approved):** two minor retained-data-during-isError instances on ADVISORY copy in `SafetyPanel.tsx` — (a) the arm-engage blast radius is gated on `read.data === undefined`, not `read.isError`, so `stoppable`/`stoppableCredits` render retained figures as a current claim after a failed refetch (`the_blast_radius_could_not_be_read` fires only on the never-read case); (b) `armed` is not cleared when the reading degrades to `'unknown'`, so an already-open release pane keeps rendering the engaged record from retained data and keeps offering the confirm (the release itself stays read-back-confirmed). Sweep or record. **Closed by 6.4 (verify, do not re-chase):** the failed-read dot/tint now force the doubt state (`read.isError ? 'unknown' : …` in both SafetyPanel and SpecEnginePage, pinned by "shows doubt on the dot…" and "stops asserting the stop on the strip…"), and the `pane === null` pending shell is covered by "holds the work area until the configuration read decides the pane" in SpecEngineSafety.test.tsx.
- **Bookkeeping (no action):** commit `aa201071d` swept 3.3's two conformance modules in under 4.4's message — content verified intact by 4.4's reviewer (reflog traced, 3166 green); attribution note only.

### 7.2's dispositions and the final gate sweep (recorded 2026-08-17)

Appended, not substituted: every finding above stays as written, and this
section records what was DONE with each. Exit codes below are real — read with
`PIPESTATUS` or with no pipe, never through a `-q` that hides the failure line.

#### Gate sweep, on the finished branch

| Gate | Command | Exit |
|---|---|---|
| Spec_App pytest | `python -m pytest .../spec_engine/tests/` | **0** — 3376 passed |
| Prior_App pytest | `python -m pytest .../spec_builder/tests/` | **0** — 269 passed |
| flake8 (incl. tests) | `flake8 src/kiro_crew test` | **0** |
| isort (incl. tests) | `isort --check-only src/kiro_crew test` | **0** |
| mypy, app trees incl. tests | `mypy .../spec_engine .../spec_builder` | **0** — 210 files |
| mypy, repo-wide | `mypy src/kiro_crew` | **1** — 3 inherited errors, below |
| black | `black --check --fast src/kiro_crew test` | **1** — repo-wide condition, below |
| tsc | `cd website && npx tsc -b` | **0** |
| vitest, the six SpecEngine suites | `npx vitest run src/test/SpecEngine` | **0** — 143 tests (140 + 3 added here) |
| manifest-sync | `node scripts/check-app-manifest-sync.mjs` | **0** — 20 manifests, 171 strings |
| i18n aggregate | `npm run i18n:check` | **1** — one inherited finding, below |
| App_Boundary_Fence | `pytest .../test_app_boundary_fence.py` | **0** — 38 passed |
| docs-lint | `bash scripts/docs-lint.sh` | **0** — 190 files |
| brand name | `BRAND_BASE_REF=origin/main python3 scripts/check_brand_name.py` | **0** after the fix below (was 1) |
| scrub-lint | `bash scripts/scrub-lint.sh` | **141** — local scratch + repo history, below |

This task's own merge-base worktree, used for the pre-existence proofs, was
removed afterwards (`git worktree remove`, with the `node_modules` symlink
unlinked first so the real tree was never at risk).

Requirement 1 re-verified rather than assumed, at Merge_Base
`adbec83be0787fbbbd14af40eaaf44e627553d27`:
`git diff <mb>..HEAD -- spec_builder/ website/src/apps/spec-builder/ website/src/test/SpecBuilder*`
is EMPTY, and no path matching `spec.builder` appears anywhere in the branch
diff (1.1, 1.2, 1.3). The Prior_App suite was run in a detached worktree at the
Merge_Base and passed **269**, the count it also passes on the branch, so 1.5 is
a measured equality rather than an assumption.

#### The three inherited failures, each PROVEN at the Merge_Base

Every one was reproduced in a detached worktree checked out at the Merge_Base,
with an identical signature. None is branch work; none is fixed here.

- **`npm run i18n:check` exits 1** on `[source-strings]`, one badly-shaped key:
  `pages.artifactDeployPage.domain` = `"domain —"` (trailing-connector). The key
  is present AT the Merge_Base, ABSENT from `origin/main`'s tip, and byte-identical
  at HEAD — upstream deleted it after we diverged, so the check reports it as
  "new vs origin/main" purely from that drift. The Merge_Base worktree fails the
  same check on the same key. Additionally proven: this branch's `en.json` change
  is purely additive — 249 keys added, all `specEngine`-namespaced, **zero**
  removed and **zero** changed. Not fixed: the key belongs to another page,
  upstream has already removed it, and an edit here would be a boundary crossing
  needing its own allowlist entry and would conflict on the eventual rebase.
- **`src/test/App.test.tsx` "Kiro credits pill"** — a VARYING 2–3 failures per
  run at a single commit (`opens a details modal…`, `closes the modal on
  Escape`, sometimes `defaults covered/overage to 0 and renders sub-1000 values
  without K suffix`). The Merge_Base worktree shows the same nondeterministic
  signature (the review gate measured 2/74 and 3/73 across consecutive runs at
  the SAME HEAD, and 3/73 at a clean `git archive` extraction of the
  Merge_Base; all failures trace to one pre-existing defect — the resolved-data
  modal never opens — and the file passes in isolation with `-t credits`,
  so it is intra-file pollution). Do not diff a new failure against a pinned
  count here; diff against the named tests. The branch touches no
  `App.tsx`, no credits component and no `App.test.tsx`, and its catalog change is
  additive-only, so it cannot reach them.
- **Repo-wide mypy, 3 errors in `src/kiro_crew/hooks.py`** (lines 1752, 1753,
  1874: `os.listxattr`/`getxattr`/`setxattr` do not exist on macOS). `hooks.py`
  is untouched by the branch (empty `git diff`), and the Merge_Base worktree
  reports the same 3 errors at the same lines. mypy over the two app trees, tests
  included, is clean.

#### black: the 38 branch-added dirty files are NOT reformatted

Decided against, on measurement rather than preference:

- The condition is repo-wide, not ours: **1178 of 2189** files fail
  `black --check --fast` under the pinned `black==26.3.1`, and an inherited file
  untouched for months wants plain slice spacing (`[len(FLAG) :]`). The repo's
  committed code was never formatted with this version, CI has black commented
  out, and the two Prior_App files are dirty AT the Merge_Base and deliberately
  left that way — the project's own precedent that byte-stability outranks
  format. Conforming 38 files would leave them the only conforming files in the
  tree.
- The reformat was nevertheless RUN and measured before being reverted, so this
  is a known quantity rather than a guess: `black --fast` on the 38 → 38
  reformatted, `flake8` 0, `isort` 0, spec_engine pytest **3376 passed**. Then
  reverted with `git checkout --`; byte-identity confirmed by a clean
  `git status` and by AST dumps captured beforehand comparing equal.
- **The one finding worth carrying:** an AST comparison over the 38 showed
  `tests/test_review_feedback_watcher.py` changing SEMANTICALLY — black inserts a
  space after `"""` in a docstring that begins with a quote, altering the
  docstring's value. That is precisely what `--fast` skips, and `--fast` is
  mandatory here (the safety check cannot parse py314-targeted output on py312).
  So whoever clears this debt should do it repo-wide, in one commit, on a py314
  interpreter where the safety check actually runs.

#### Items swept, with what was found

- **Document-body read route (6.2's correction 5): omission RECORDED, no route
  added.** The design's backend route list scopes document bodies out, and the
  spec already recorded the same disposition for five of the six verdict ACTIONS.
  Adding it is not a sweep-sized change either: reading a spec document means a
  path-confined file read on a path derived from run state, a size bound, an
  operator guard, route-level tests and thirteen catalogs — security-sensitive
  work that wants a reviewer, which the final task does not have. The in-UI note
  (`document_bodies_have_no_route_note`) states the omission truthfully and was
  re-verified against the route table: `config`, `config/resolved`, `setup/*`,
  `kill-switch`, `run-spend`, `queue` and the four queue actions, and nothing
  serving a document body. NOTE the asymmetry deliberately: 6.3 CLOSED its
  resolved-config item by adding `GET /config/resolved`, because that read is a
  pure engine call with no path handling. A future task adding the document route
  re-opens the per-document view.
- **`.se-pending` is NOT dead — kept.** The finding predates 6.4's edit of the
  same file. The spend pane's pending state in `SafetyPanel.tsx` renders with
  that class today (grep `se-pending` — line numbers shift under this same
  task's own edits), so removing the rule would have unstyled a live state. No
  test pins the class (it is styling), which is why the grep the finding asked
  for was the right check.
- **Three-way manifest terminal coherence: already closed by 4.2, nothing
  added.** `test_all_three_legs_of_the_route_contract_agree` resolves the
  manifest's dotted `backend.routes` through the import system and asserts the
  object it yields IS the attribute the gateway loop calls off the package;
  `test_the_three_way_gate_would_notice_each_leg_going_missing` drives each leg's
  predicate against a violating value so the conjunction is falsifiable. Both run
  green here by name.
- **The manifest-sync allowlist justification is refreshed.** It read "made
  bidirectional for a card with no page"; all 20 shipped builtin manifests now
  declare a page (measured), so the pageless else-branch is driven by no shipped
  manifest — only by `appManifest.test.ts`'s `pageless-probe-app`. It now reads
  "taught to demand a page_label exactly when a page exists".
- **`tmp/` hygiene: sharper than recorded, and NOT swept.** The entry admits
  scratch that cannot reach a build artifact, and that half is now checked rather
  than assumed: **0** files under `tmp/` are tracked by git; `MANIFEST.in` grafts
  no repo-root directory (the sdist is assembled from named root files plus
  `recursive-include src/kiro_crew/...`); and `setuptools_scm` is not in use, so
  no tracked-file set is auto-included. But the scratch is not inert either, which
  the earlier finding did not know: `scripts/scrub-lint.sh` scans the WORKING TREE,
  and an earlier task's copy at `tmp/mb-black/test_test_security.py` trips its
  credential-pattern check 25 times. The check allowlists `./test/` and
  `./website/src/test/` by ANCHORED prefix, so a copy of an allowlisted fixture
  placed anywhere else is a failure by construction. CI is unaffected — `tmp/` is
  untracked and absent from a fresh checkout — so this is a local-tree cost, not a
  build one. Not deleted: the directory is shared between concurrent sessions and
  removing another session's scratch is the one thing a sweep must not do. For the
  owner: sweep `tmp/` between runs, or add it to `.gitignore` (a shared-file change
  needing its own fence allowlist entry, hence not done here).
- **`scripts/scrub-lint.sh`'s other failure is the repo's history, and its exit
  code is not 1.** Step 5 reports **1310** commits carrying internal author
  references — including `origin/main`'s own tip — so it is inherited and
  unfixable from this branch. Note for whoever wires it into a gate: the script
  exits **141** (SIGPIPE from an internal `| head`), not 1, so a caller reading
  the status as signal-death rather than failure would misread it.
- **Equivalence suite's credential-key reach: extended.** The `hypothesis.find`
  non-vacuity block gained a fifth shape, `"a credential-classified key"`, whose
  predicate asks the engine's own `is_secret_key` rather than carrying a copy of
  the rule. It was pinned only by a hand-written case that names its own key, so
  narrowing `_VARIABLE_KEYS` would have left byte-identity proven only where the
  reply and the file agree.
- **6.4's two approved-round minors: FIXED, with pinned tests.** (a) The
  arm-engage blast radius now claims its figures from a read that SUCCEEDED
  (`radiusKnown`), not merely from a payload being present, so a retained count
  is no longer quoted as the reach of a stop about to be thrown. (b) An armed
  RELEASE is withdrawn when the reading stops being `engaged` — in both
  degradations, a failed read and a switch another operator already released —
  which applies the offer site's own rule for as long as the pane is up. The
  read-back confirmation property is untouched: the verdict block renders on
  `outcome`, so a completed operation still reports against the flag it read back.
- **`api.ts`'s REFUSAL docstring undercounted.** It said three codes have real
  branches; 6.4 added a fourth (`runUnknown`, the spend pane). Corrected. The same
  finding's dead `__testing` re-export of REFUSAL is already gone — that export
  now carries `waitedParts`/`WHY_KEY`/`WHY_EXHAUSTED_KEY`/`WAIT_LABEL_KEY`, all
  consumed by `SpecEngineShell.test.tsx`.
- **The three documentation debts are closed.** `is_secret_key` now carries a
  "What this cannot see" note (the classification reads the NAME, so a credential
  under an innocent last segment is not withheld, and the file holds every value
  verbatim either way — elision is a display convenience over a naming
  convention, not a containment boundary). `_unschedule` no longer claims a scope
  it did not have: the search and the splice are confined to the parsed graph
  block's body. The no-scheduled-leaves rot path says which condition the corpus
  stopped meeting instead of raising a bare `StopIteration`.
- **"Closed by 6.4 (verify, do not re-chase)": verified by name, not re-chased.**
  `shows doubt on the dot…`, `stops asserting the stop on the strip…` and `holds
  the work area until the configuration read decides the pane` each run green
  individually.

#### Mutation probes run by this task

Four, each committed first, then neutered with a grepped `MUTATION_PROBE`
marker, then restored byte-identical with zero markers left in the tree:

| Mechanism neutered | Named test that failed |
|---|---|
| `radiusKnown` reverted to the payload-absent gate | `engaging > stops quoting a blast radius the last read left behind` |
| the armed-release withdrawal made unreachable | `releasing > withdraws an open release pane when the reading stops being engaged` **and** `… when the switch is already released` |
| `_unschedule` made a no-op | `test_unscheduling_a_real_task_is_caught` |
| `_VARIABLE_KEYS` narrowed to non-credential keys | `test_the_generators_really_can_draw_the_shapes_the_docstring_claims` — "the generators can no longer draw a credential-classified key" |

#### Carried to the final report

- **The mockup selection is VETO-PENDING for Billy.** A reviewer agent selected
  `mockup-b.html` ("Operator Console") over `mockup-a.html` ("Triage Board");
  criteria, per-criterion comparison, post-selection corrections and open holes
  are in `website/src/apps/spec-engine/design/selection.md`. Overturning it
  re-runs tasks 6.1–6.4 only.
- **Requirement 1.4: no reverted hunk fixed a genuine Merge_Base defect of the
  Prior_App.** One observation for its owner, deliberately NOT fixed because
  byte-identity forbids it: `_seed_prompt`'s docstring claims a builtin's declared
  skills "are NOT on the agent's skill path", but `bridges.reconcile_app_skills`
  links manifest-declared skills for enabled non-self-managed apps. Report, do
  not patch.
- **Recorded in the prior spec.** `.kiro/specs/agent-agnostic-spec-engine/tasks.md`
  gains a dated section stating that its one-app design intent — and the two-app
  shape it ratified in that intent's place — are superseded by this spec, that the
  second app was in fact another team's, and that its delivery-isolation
  obligation remains open there. No entry of its own is rewritten.
- **The brand gate was failing on the branch and is now green.** Not on this
  task's list, found while sweeping: `BRAND_BASE_REF=origin/main python3
  scripts/check_brand_name.py` exited 1 on **16 lines** spelling the product
  `KiroCrew`. Scoped to 7.2's own commits (`BRAND_BASE_REF=3dbbe0139`) it was
  already green, so every one was earlier-wave content — 12 in the prior spec's
  three documents, which this branch ADDS (they are absent from both the
  Merge_Base and `origin/main`, so the gate is right to count them), plus this
  spec's glossary, `engine_mcp/server.py`'s module docstring and one line of
  `test/test_app_bridges.py`. All 15 distinct lines are corrected. The one that
  needed care is this spec's requirements.md:7, which quotes the prior design
  verbatim: both sides were corrected together, so the quotation still matches
  its source word for word.
