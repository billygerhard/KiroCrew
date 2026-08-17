# Operator_Surface mockup selection

> ## VETO-PENDING — Billy (owner)
>
> **This selection was made without the owner.** Requirement 6.1 asks that the
> mockups be *presented for selection*; the run was directed to complete without
> him, so a separate REVIEWER agent selected against
> [`criteria.md`](criteria.md) and the result is recorded here as **provisional**.
>
> **Billy can overturn it outright.** Cost of overturning: re-running the
> frontend wave only (tasks 6.1–6.4). Nothing upstream — the restoration, the
> boundary fence, the MCP tools, the backend routes, the manifest and
> localization — depends on which mockup won.
>
> **Selected: `mockup-b.html` — "Operator Console".** Rejected: `mockup-a.html`
> — "Triage Board".

## How the selection was made

1. [`criteria.md`](criteria.md) was written and saved **before** either mockup
   was judged and before any reviewer saw them. It is a separate file so the
   ordering is checkable rather than asserted.
2. Both mockups were built.
3. A REVIEWER agent was dispatched as a separate session
   (`kirocrew spawn --port 6777 run`, subagent `29c46d11`) and handed exactly
   three inputs: `criteria.md` and the two HTML files. It was told the owner's
   direction (reviewers, not drivers) and instructed to verify the CSS and JS
   rather than score from each file's own self-description, to be decisive, and
   to name any criterion where neither option is adequate.
4. Its verdict is reproduced below, unedited in substance.

The implementer did not cast the deciding vote. The implementer's own view
before dispatch also favoured B, which is a fact worth disclosing rather than
hiding: it means the reviewer was not an independent tiebreak between two
positions, only an independent check on one.

## The two options

| | `mockup-a.html` — Triage Board | `mockup-b.html` — Operator Console |
|---|---|---|
| **Layout** | 5-column board, cards | Left rail / dense table / docked inspector / status strip |
| **Density** | Low: ~110–170px per run, cards | High: ~34px tabular row per run |
| **Detail** | Overlay drawer, summoned and dismissed per run | Permanently docked pane, follows selection |
| **Grouping** | `WaitingOn` is the **container** (a column each) | `WaitingOn` is a **cell value** plus filters over one list |
| **Navigation** | Horizontal tabs; config replaces the board | Vertical rail; config reuses the same split geometry |
| **Safety controls** | Sticky header pill + meter | Bottom status strip that is a layout row, not an overlay |
| **Config** | Two panes: JSON, roles table + resolution trace | JSON pane + resolved read in the docked pane |
| **First run** | Full-screen takeover over the board | Setup pane with a 4-step progress rail |
| **Input** | Pointer | Keyboard-first (`j`/`k`, state-dependent action keys) + pointer |

## Reviewer's verdict, as returned

**Winner: `mockup-b.html`.** It takes the blocking criterion C1 and then C2, C4,
C5, C6, C8, C9 and C10. A wins exactly one criterion, C3, and **loses C1** on
the single control C1 was written to exclude.

Per-criterion, condensed (the reviewer's findings, not a restatement of the
mockups' own claims):

- **C1 — reviewer not driver (blocking).** A's `run_1c93de` card carries
  `Rewrite the gate myself` on the default path as the offered response to
  exhausted revision cycles. That is a hand-authoring affordance on the very
  surface the owner rejected the prior interface over. B's action set never
  composes content and says so in place.
- **C2 — interactions per verdict.** A costs `Open` + read + `Close` per run —
  the open/dismiss-per-run pattern C2 penalises — and splits the five waiting
  runs across three columns, so the backlog is not one traversal. B's docked
  inspector makes detail free. *The reviewer noted B's advertised `a`/`c`/`x`
  keys were not actually bound and scored C2 on the geometry alone.*
- **C3 — fidelity to the waiting model.** **A wins.** Its columns give each
  `WaitingOn` its own action set, which is why A had a `Raise ceiling` control
  and **B had none at all** — B offered `Approve gate` to a budget-parked run.
  B's exceptional states were unlegended codes (`RX`, `FB2`, `WS2`), with one
  CSS class carrying two different meanings.
- **C4 — untrusted text cannot move the controls.** Both bound the expanded
  form and both render the escaped markup payload as text. B wins on ordering:
  its untrusted block is the *last* child of the pane, so expansion cannot
  displace the verdict controls at all, and its expanded height is fixed
  (`height:104px`) rather than capped (`max-height:132px`). *The reviewer
  flagged A's comment calling a `max-height` "fixed-height" as inaccurate.*
- **C5 — safety never behind navigation.** Decisive for B. A's `.scrim`
  (`z-index:50`) covers the header while the drawer is open, so the kill switch
  is both dimmed and click-blocked, and its first-run overlay hides spend
  entirely. B's strip is a grid row of `body`; B contains no `position:fixed`
  overlay at all.
- **C6 — first run.** B renders the unanswered autonomy rung as a state
  (`<span class="unanswered">`, danger-coloured, plus a refusal naming it);
  A only describes the rule in prose and has no rung control.
- **C7 — config honesty.** Tie. A was more legible (separate Model and Effort
  columns); B was more explicit about read-versus-write.
- **C8 — both ends of the population.** B. A's board has a ~1330px minimum and
  scrolls horizontally to see a run's state.
- **C9 — tokens.** B, narrowly. A's `.scrim` is the one non-token colour.
- **C10 — cost.** B, slightly. A needs a drawer with focus trapping and an
  `aria-modal` that is not built; B is a CSS-grid shell with no overlays.

### Strongest case for the loser, as recorded

A is the only option that models the three waiting reasons as **structurally
different jobs rather than differently-coloured rows**. B's static inspector
offered `Approve gate` to a run parked at a budget ceiling — the exact confusion
C3 forbids — and A's layout makes that mistake impossible.

## Post-selection corrections applied to the winner

The reviewer's carry-over list was applied to `mockup-b.html` **after** the
verdict. This is disclosed rather than silently folded in, because it means the
committed winner is not byte-identical to the file that was scored.

`mockup-a.html` is left **exactly as compared** — including the C1-failing
`Rewrite the gate myself` button — so the losing artifact still matches the
comparison above. **That button is the anti-pattern this task exists to
exclude; task 6.x must not implement it.**

Applied to B:

1. **Action set is now a function of the waiting reason** (`ACTIONS` /
   `setActions()`), so a budget-parked run gets `Raise ceiling` and a stalled run
   gets `Resume`. Exhausted revision cycles get their own set — approve as
   written, raise the limit, or cancel — and notably **not** an editor. This was
   the reviewer's "B is not merely less pretty but incorrect" finding.
2. **The waiting reason is stated in words** in the inspector header
   (`.insp-why`), carried over from A's per-column prose.
3. **Unlegended codes replaced with words** — `revisions spent`, `2 held`,
   `2 workspaces kept` — and the one CSS class carrying two meanings split into
   `.flag.exhausted` / `.flag.held` / `.flag.kept`.
4. **Advertised keystrokes are now bound**, resolved against the selected run's
   own action set so a key the current state does not offer is inert rather than
   mapped to whatever sits in that position. The rail no longer advertises fixed
   action keys.
5. **Documents and findings under review are now on the verdict pane** — a
   bounded document view with a tab per document and a findings list keyed to
   acceptance criteria. This addresses the reviewer's largest shared hole (see
   below): four summary counts are not enough to render a verdict on.
6. **Roles are a table with separate Model, Effort and From columns**, per A's
   shape, and the `budget.warn_fraction` / `limits.task_retry_limit` keys B had
   dropped are back in the config sample.
7. **Two false self-descriptions fixed**: the header comment claiming the queue
   stays visible while config is read (it does not — config replaces the work
   area, reusing its split geometry), and the `Needs a person` rail entry that
   was a dead pane link; it is now a real filter over the one list, sharing one
   implementation with the filter chips.
8. **`td.spec` no longer opts out of the ellipsis clamp**, closing the one path
   by which B could still scroll horizontally.

## Open holes the reviewer named, carried to the implementing tasks

- **Neither mockup surfaced enough to render a verdict.** Fixed in B by
  correction 5, but the *shape* of that surface is now design decided in a
  mockup rather than reviewed — task 6.2 owns making the documents and findings
  view real, and it is the piece with the least review behind it.
- **`revision_exhausted` and `feedback_needs_human` still have no legitimate
  distinct action** beyond what correction 1 added. Task 6.2 should confirm the
  engine actually supports raising a revision limit for one gate before shipping
  a control that says so.
- **The no-overlay constraint is now load-bearing.** B passes C5 only because it
  has no overlay. It is written into B's header comment; a later drawer or modal
  would silently reintroduce A's C5 failure. Task 6.1 should carry that
  constraint into the page shell.
- **`--info` at 11px bold on light `--bg`** is marginal contrast in both files.
  A contrast check belongs in the frontend wave, not in a mockup.
- **A's drawer accessibility work (focus trap, Escape, `aria-modal`) is unbuilt
  cost, not absent cost.** Recorded in case the veto restores A.

## Where the mockups live, and why not where the task said

The task record directed
`src/kiro_crew/apps/builtins/spec_engine/design/`. These files are in the
Spec_App's **other** declared root, `website/src/apps/spec-engine/design/`,
because the directed path fails a real gate:
`test_public_build_posture.py::TestBundledResourcesArePackaged::test_every_bundled_non_python_file_is_packaged`
requires every non-`.py` file under the app's Python tree to be covered by a
`setup.cfg` `package_data` glob. Verified empirically, not assumed — a probe
`design/_probe.html` was placed there and the test failed naming it.

The three ways out and why this one:

- **Add a `package_data` glob** — ships design mockups to every install as dead
  weight, and edits `setup.cfg`, which is outside both declared roots and would
  need a `BOUNDARY_ALLOWLIST` entry.
- **Put them under the app's `tests/`** — excluded from that gate, but design
  artifacts are not tests.
- **Chosen: `website/src/apps/spec-engine/design/`** — inside `DECLARED_ROOTS`,
  so the App_Boundary_Fence admits it with no allowlist change; not packaged
  into the wheel; not picked up by the provenance UI scan
  (`UI_SUFFIXES` is `.ts`/`.tsx`); and not a Vite build entry, because
  `appWindowEntries()` reads only the direct children of `src/apps/<app>/` and
  these sit one level deeper. It is also where the components they describe will
  live.

## How to look at them

Open either file directly from the filesystem; there is nothing to build and
nothing is fetched. Both carry a theme toggle in the chrome for light/dark.
Append `?firstrun` to the URL to see the no-configuration state:

- `mockup-a.html?firstrun` — full-screen setup takeover
- `mockup-b.html?firstrun` — setup pane as the landing pane
