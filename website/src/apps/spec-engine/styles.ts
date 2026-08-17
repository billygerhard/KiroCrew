/**
 * The Operator_Surface's stylesheet, ported from `design/mockup-b.html`.
 *
 * A scoped CSS string injected by the page (the same shape `file-explorer` uses),
 * rather than utility classes, because the load-bearing part of the selected
 * design is a GEOMETRY: a two-row, two-column grid whose bottom row is the safety
 * strip and whose work row is a fixed split with a permanently docked inspector.
 * Every colour, radius and shadow is a token read by CASCADE from
 * `src/index.css` — the mockups inline a COPY of that token block so they can be
 * opened from the filesystem, and `design/tokens.md` says in as many words that
 * the implementation must not carry the copy forward.
 *
 * ## The no-overlay rule is load-bearing, not a preference
 *
 * The selected mockup passes the "safety controls are never behind navigation"
 * criterion **only because it contains no overlay**. `.se-status` is a GRID ROW of
 * the page root, so the kill-switch state and the spend figure are on screen in
 * every pane at every scroll position, and nothing can be laid over them. The
 * losing mockup failed that criterion on exactly this: its detail drawer's scrim
 * covered the header, leaving the stop control dimmed and click-blocked at the
 * moment it mattered.
 *
 * So: **no `position: fixed`, no `position: absolute`, no scrim, no modal, no
 * drawer anywhere in this app's styles.** A later drawer would not look like a
 * regression — it would look like a feature, and it would silently reintroduce the
 * failure this design was chosen to avoid. `SpecEngineShell.test.tsx` asserts the
 * absence, so adding one fails a test rather than passing review.
 *
 * `position: sticky` on the table header is not an exception to that rule: a
 * sticky header scrolls within its own scroll container and cannot leave it, so it
 * can never cover a sibling grid row.
 *
 * ## Bounding, not clamping, is what keeps controls in place
 *
 * Every cell carries the ellipsis clamp including the widest one, because a long
 * project or spec name must not be the thing that introduces horizontal scroll —
 * and a run whose state you have to scroll sideways to see is a run nobody reads.
 * The regions that hold arbitrarily long text (a document under review, an outside
 * submitter's comment) are FIXED-height scroll regions rather than
 * `max-height`-capped ones, so line count cannot displace the controls above or
 * below them. Those regions land with the review-queue panel; the rule is stated
 * here because it is a property of this stylesheet, not of that panel.
 */
export const SE_CSS = `
.se-root{
  height:100%;min-height:0;
  display:grid;
  grid-template-columns:186px minmax(0,1fr);
  grid-template-rows:minmax(0,1fr) 34px;
  grid-template-areas:"rail work" "rail status";
  background:var(--bg);color:var(--text);
  font-family:var(--font-body);font-size:13px;line-height:1.45;
}
.se-root code,.se-root pre,.se-m{font-family:var(--mono)}
.se-root h1,.se-root h2,.se-root h3{margin:0;font-weight:600;color:var(--text-strong)}
.se-root button{font-family:inherit;font-size:inherit;cursor:pointer}

/* Left rail. Vertical, so the panes cost no horizontal room and the work split
   geometry is identical in every one of them. */
.se-rail{grid-area:rail;background:var(--panel-strong);border-right:1px solid var(--border);
  display:flex;flex-direction:column;padding:12px 10px;gap:2px;overflow:auto}
.se-brand{display:flex;align-items:center;gap:8px;font-weight:600;color:var(--text-strong);
  font-size:14px;padding:0 6px 14px}
.se-brand svg{width:17px;height:17px;color:var(--accent);flex:none}
.se-nav{display:flex;align-items:center;gap:9px;background:transparent;border:none;color:var(--muted);
  padding:7px 9px;border-radius:var(--radius-sm);text-align:left;width:100%;font-weight:500}
.se-nav svg{width:15px;height:15px;flex:none}
.se-nav:hover{background:var(--bg-hover);color:var(--text)}
.se-nav[aria-current="page"]{background:var(--accent-subtle);color:var(--text-strong);
  box-shadow:inset 2px 0 0 var(--accent)}
.se-nav:focus-visible{outline:2px solid var(--ring);outline-offset:-2px}
.se-badge{margin-left:auto;font-family:var(--mono);font-size:11px;background:var(--bg);
  border:1px solid var(--border);border-radius:999px;padding:0 6px;color:var(--muted)}
.se-nav[data-alarm="true"] .se-badge{border-color:var(--danger);color:var(--danger)}
.se-rail-foot{margin-top:auto;padding-top:12px;border-top:1px solid var(--border)}
.se-keys{font-family:var(--mono);font-size:10.5px;color:var(--muted);line-height:1.9;padding:0 6px}
.se-keys kbd{background:var(--bg-elevated);border:1px solid var(--border-strong);border-radius:3px;
  padding:0 4px;color:var(--text)}

/* Work area: one ordered list on the left, the docked inspector on the right.
   The config and setup panes reuse this same split, so moving between panes
   never re-flows the reader's mental map. */
.se-work{grid-area:work;display:grid;grid-template-columns:minmax(0,1.35fr) minmax(360px,1fr);
  min-height:0;overflow:hidden}
.se-list{border-right:1px solid var(--border);display:flex;flex-direction:column;min-width:0}
.se-list-head{display:flex;align-items:center;gap:10px;padding:9px 14px;
  border-bottom:1px solid var(--border);background:var(--chrome)}
.se-list-head h1{font-size:13px}
.se-sort{margin-left:auto;font-size:11.5px;color:var(--muted)}
.se-filters{display:flex;gap:4px;padding:8px 14px;border-bottom:1px solid var(--border);flex-wrap:wrap}
.se-filter{background:var(--bg-elevated);border:1px solid var(--border);color:var(--muted);
  border-radius:999px;padding:2px 10px;font-size:11.5px}
.se-filter[aria-pressed="true"]{background:var(--accent-subtle);border-color:var(--accent);
  color:var(--text-strong)}
.se-filter:focus-visible{outline:2px solid var(--ring);outline-offset:1px}
.se-filter-count{font-family:var(--mono);margin-left:5px}
.se-rows{overflow:auto;min-height:0;flex:1}
/* One ordered list, laid out as a grid rather than as a <table>.
   The columns are the mockup's and the density is unchanged, but every row is a
   focusable \`role="row"\` with \`role="gridcell"\` children, which is the ARIA grid
   pattern the keyboard traversal needs.

   The header and every row declare the SAME explicit template rather than
   inheriting one through \`subgrid\`: subgrid would let the columns size
   themselves to content, and it is the one property here that a shipped Electron
   Chromium can be too old to support — which would collapse the whole list, not
   degrade it. Fixed track widths are the trade: the identifier columns are sized
   for their content and only the spec-and-project column flexes, which is also
   the column a long value belongs in. */
.se-q{display:grid;grid-template-columns:1fr}
.se-qhead,.se-row{display:grid;
  grid-template-columns:130px minmax(0,1fr) 110px 120px 92px 82px}
.se-qhead>span{position:sticky;top:0;background:var(--bg-accent);font-size:10.5px;
  text-transform:uppercase;letter-spacing:.05em;color:var(--muted);font-weight:600;
  padding:6px 10px;border-bottom:1px solid var(--border);z-index:1;white-space:nowrap;
  overflow:hidden;text-overflow:ellipsis}
.se-row>span{padding:6px 10px;border-bottom:1px solid var(--border);white-space:nowrap;
  overflow:hidden;text-overflow:ellipsis;min-width:0}
.se-row{cursor:pointer}
.se-row:hover>span{background:var(--bg-hover)}
.se-row[aria-selected="true"]>span{background:var(--accent-subtle)}
.se-row[aria-selected="true"]>span:first-child{box-shadow:inset 2px 0 0 var(--accent)}
.se-row:focus-visible{outline:2px solid var(--ring);outline-offset:-2px}
/* The widest column keeps the clamp rather than opting out of it. */
.se-spec{font-weight:600;color:var(--text-strong)}
.se-id,.se-age,.se-cost{font-family:var(--mono);font-size:11.5px;color:var(--muted)}
.se-wait{display:inline-flex;align-items:center;gap:5px;font-size:11px;font-weight:600}
.se-wait::before{content:"";width:7px;height:7px;border-radius:50%;background:currentColor;flex:none}
.se-wait[data-wait="review"]{color:var(--accent)}
.se-wait[data-wait="budget"]{color:var(--warn)}
.se-wait[data-wait="stall"]{color:var(--danger)}
.se-empty{padding:22px 14px;color:var(--muted);font-size:12.5px}

/* Inspector: a fixture of the layout. No summon, no dismiss, nothing to close. */
.se-inspector{display:flex;flex-direction:column;min-width:0;background:var(--panel)}
.se-insp-head{padding:9px 14px;border-bottom:1px solid var(--border);background:var(--chrome)}
.se-insp-title{font-size:13.5px;font-weight:600;color:var(--text-strong);display:block;
  overflow-wrap:anywhere}
.se-insp-sub{font-family:var(--mono);font-size:11px;color:var(--muted);margin-top:2px;display:block;
  overflow-wrap:anywhere}
/* The waiting reason in words, because the reason is what decides which actions
   are legitimate: a run parked at a ceiling and a run parked on a verdict are
   different jobs, and a coloured dot alone does not say so. */
.se-insp-why{display:block;margin-top:4px;font-size:11.5px;color:var(--text)}
.se-insp-body{overflow:auto;min-height:0;flex:1;padding:12px 14px 18px}
.se-blk{margin-bottom:16px}
.se-blk h3{font-size:10.5px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);
  margin-bottom:7px}
.se-note{font-size:11.5px;color:var(--muted);margin:7px 0 0}
.se-pending{border:1px dashed var(--border-strong);border-radius:var(--radius-md);
  padding:10px;color:var(--muted);font-size:11.5px}
.se-refusal{border:1px solid var(--danger);background:var(--danger-subtle);
  border-radius:var(--radius-md);padding:10px;font-size:12px;color:var(--text-strong)}
.se-refusal code{font-size:11px;color:var(--muted);display:block;margin-top:5px;
  overflow-wrap:anywhere}
.se-btn{border-radius:var(--radius-sm);padding:5px 10px;font-size:12px;font-weight:600;
  border:1px solid var(--border-strong);background:var(--bg-elevated);color:var(--text)}
.se-btn:hover{background:var(--bg-hover);border-color:var(--border-hover)}
.se-btn:focus-visible{outline:2px solid var(--ring);outline-offset:1px}
.se-btn[disabled]{opacity:.45;cursor:not-allowed}
.se-btn.se-sm{padding:3px 8px;font-size:11px}
.se-btn.se-danger{background:var(--danger-subtle);border-color:var(--danger);color:var(--text-strong)}
.se-acts{display:flex;gap:6px;flex-wrap:wrap;align-items:center}
dl.se-kv{display:grid;grid-template-columns:118px minmax(0,1fr);gap:4px 10px;margin:0;font-size:12px}
dl.se-kv dt{color:var(--muted)}
dl.se-kv dd{margin:0;font-family:var(--mono);color:var(--text-strong);overflow-wrap:anywhere}

/* Row-level state words. One class per meaning, keyed by a data attribute rather
   than by colour alone: each flag licenses a different action, and one shared
   style for two meanings is how a reader learns to ignore both. */
.se-flag{display:inline-block;font-size:10px;font-weight:700;padding:0 5px;border-radius:3px;
  margin-left:6px;border:1px solid currentColor;text-transform:uppercase;letter-spacing:.03em}
.se-flag[data-flag="exhausted"]{color:var(--danger)}
.se-flag[data-flag="held"]{color:var(--warn)}
.se-flag[data-flag="human"]{color:var(--warn)}
.se-flag[data-flag="kept"]{color:var(--danger)}

/* An identifier the queue projection withholds, asked for rather than invented. */
.se-idfield{display:flex;flex-direction:column;gap:3px;margin:8px 0}
.se-idfield label{font-size:10.5px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);
  font-weight:600}
.se-input{background:var(--bg-accent);border:1px solid var(--border);border-radius:var(--radius-sm);
  padding:4px 7px;font-size:12px;color:var(--text);min-width:0}
.se-input:focus-visible{outline:2px solid var(--ring);outline-offset:1px}

/* Findings. Every message is arbitrarily long outside-authored prose, so each one
   is bounded by the untrusted block below rather than by this list. */
ul.se-findings{margin:0;padding:0;list-style:none;font-size:12px}
ul.se-findings li{padding:7px 0;border-bottom:1px solid var(--border);min-width:0}
ul.se-findings li:last-child{border-bottom:none}
.se-fc{display:inline-block;font-family:var(--mono);font-size:10.5px;font-weight:700;
  color:var(--accent);border:1px solid var(--accent);border-radius:3px;padding:0 5px;margin-right:6px}
.se-fc[data-keyed="false"]{color:var(--muted);border-color:var(--border-strong)}
.se-sev{font-style:normal;font-size:10px;font-weight:700;text-transform:uppercase;
  letter-spacing:.04em;color:var(--warn);margin-right:6px}
.se-fkind{font-family:var(--mono);font-size:10.5px;color:var(--muted);margin-right:6px;
  overflow-wrap:anywhere}

/* Outside-authored prose.
   Collapsed to two lines by clamp; expanded to a FIXED-height scroll region, not
   a max-height cap — a cap still grows with line count until it binds, so a long
   comment would move whatever sits below it. Nothing here is parsed as markup:
   the text arrives through the engine's display contract and is rendered as a
   text child, never through dangerouslySetInnerHTML. */
.se-untrusted{background:var(--bg-accent);border:1px solid var(--border);
  border-left:2px solid var(--muted-strong);border-radius:var(--radius-sm);padding:7px 9px;
  margin-top:6px}
.se-untrusted-tag{display:flex;align-items:center;gap:5px;font-size:10px;text-transform:uppercase;
  letter-spacing:.05em;color:var(--muted);margin-bottom:4px}
.se-untrusted-tag svg{width:11px;height:11px;flex:none}
.se-untrusted-body{font-size:11.5px;white-space:pre-wrap;overflow-wrap:anywhere;color:var(--text);
  margin:0;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.se-untrusted[data-open="true"] .se-untrusted-body{display:block;height:104px;overflow:auto}
.se-untrusted-more{background:none;border:none;color:var(--accent);font-size:11px;font-weight:600;
  padding:3px 0 0;text-decoration:underline}
.se-untrusted-more:focus-visible{outline:2px solid var(--ring);outline-offset:1px}

/* Held feedback, a kept teardown, and the arm step before a destructive one. */
.se-held{display:flex;gap:8px;align-items:flex-start;background:var(--warn-subtle);
  border:1px solid var(--warn);border-radius:var(--radius-sm);padding:8px;margin-bottom:7px;
  font-size:11.5px}
.se-kept{background:var(--danger-subtle);border:1px solid var(--danger);
  border-radius:var(--radius-sm);padding:9px;font-size:12px}
.se-kept ul{margin:6px 0 0;padding:0;list-style:none}
.se-kept li{display:flex;align-items:center;gap:8px;padding:3px 0;font-family:var(--mono);
  font-size:11.5px;flex-wrap:wrap}
.se-kept li .se-acts{flex:none}
.se-torn{background:var(--ok-subtle);border:1px solid var(--ok);border-radius:var(--radius-sm);
  padding:9px;font-size:12px}
/* The confirmation is a sibling block, never an overlay: a dialog here would
   reintroduce exactly the failure the no-overlay rule above exists to prevent. */
.se-arm{background:var(--danger-subtle);border:1px solid var(--danger);
  border-radius:var(--radius-sm);padding:9px;font-size:12px;margin-top:7px}
.se-arm p{margin:0 0 8px;display:flex;gap:6px;align-items:flex-start}
.se-arm svg{width:14px;height:14px;flex:none;color:var(--danger)}

/* Config pane, in the same split: the document on the left where the list was,
   its read on the right where the inspector was. */
.se-cfg{display:flex;flex-direction:column;min-height:0}
.se-cfg-head{padding:9px 14px;border-bottom:1px solid var(--border);background:var(--chrome);
  display:flex;align-items:center;gap:10px}
.se-cfg-head h1{font-size:13px}
.se-cfg-body{overflow:auto;min-height:0;flex:1;padding:12px 14px 18px}
.se-json{background:var(--bg-accent);border:1px solid var(--border);border-radius:var(--radius-md);
  padding:11px;font-size:12px;line-height:1.6;overflow:auto;margin:0;tab-size:2;color:var(--text);
  white-space:pre-wrap;overflow-wrap:anywhere;height:320px}

/* Setup pane: a stepper across the work area, and the first-run landing pane. */
.se-setup{grid-area:work;display:grid;grid-template-columns:196px minmax(0,1fr);
  min-height:0;overflow:hidden}
.se-steps{border-right:1px solid var(--border);padding:14px 12px;background:var(--panel-strong);
  overflow:auto}
.se-steps h2{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);
  margin-bottom:11px}
.se-step{display:flex;gap:9px;align-items:flex-start;padding:7px 0;font-size:12px;color:var(--muted)}
.se-step .se-dot{flex:none;width:18px;height:18px;border-radius:50%;
  border:1px solid var(--border-strong);display:grid;place-items:center;font-size:10px;font-weight:700}
.se-step[data-state="now"]{color:var(--text-strong);font-weight:600}
.se-step[data-state="now"] .se-dot{border-color:var(--accent);color:var(--accent)}
.se-setup-body{overflow:auto;min-height:0;padding:16px 18px 24px}
.se-setup-body h1{font-size:17px;margin-bottom:5px}
.se-setup-lead{color:var(--muted);font-size:12.5px;margin:0 0 16px;max-width:66ch}

/* The status strip. A grid row, never an overlay: this is the one thing on the
   page that must never be occluded, and the whole strip turns danger-coloured
   when the stop is in force so the engaged state is not a badge you can miss. */
.se-status{grid-area:status;background:var(--chrome);border-top:1px solid var(--border);
  display:flex;align-items:center;gap:14px;padding:0 14px;font-size:11.5px;overflow-x:auto}
.se-status .se-sep{width:1px;height:16px;background:var(--border);flex:none}
.se-lbl{color:var(--muted);text-transform:uppercase;letter-spacing:.05em;font-size:10px}
.se-val{font-family:var(--mono);color:var(--text-strong)}
.se-ks{display:flex;align-items:center;gap:7px;margin-left:auto;flex:none}
.se-ks-dot{width:7px;height:7px;border-radius:50%;background:var(--ok);flex:none}
.se-ks-text{font-weight:600;color:var(--text-strong)}
.se-status[data-engaged="true"]{background:var(--danger);border-top-color:var(--danger)}
.se-status[data-engaged="true"] .se-lbl,
.se-status[data-engaged="true"] .se-val,
.se-status[data-engaged="true"] .se-ks-text{color:var(--danger-fg)}
.se-status[data-engaged="true"] .se-ks-dot{background:var(--danger-fg)}
`
