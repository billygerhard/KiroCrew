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
  /* The status row is content-sized rather than pinned to the strip's own height:
     the kill switch's confirmation and verdict are in-flow lines of that strip, so
     the row grows for them instead of a dialog opening over the page. */
  grid-template-rows:minmax(0,1fr) auto;
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
.se-btn[aria-pressed="true"]{background:var(--accent-subtle);border-color:var(--accent);
  color:var(--text-strong)}
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

/* The projects table. The queue's grid rows verbatim — same header, same roving
   focus, same selected tint — with four columns of its own, so the traversal a
   reader learned on the queue is the traversal here. Declared at higher
   specificity than the queue's own template rather than by a second class on
   every row, and the action column is sized for the button rather than clamped
   around it. */
.se-projects .se-qhead,.se-projects .se-row{
  grid-template-columns:minmax(0,1fr) 116px 84px 96px}
.se-projects .se-row>span{padding:5px 10px}

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

/* Setup pane: a stepper across the work area, and the first-run landing pane. */.se-setup{grid-area:work;display:grid;grid-template-columns:196px minmax(0,1fr);
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
/* A step behind the flow is DONE, not merely past: the four steps are four calls,
   and a step marked done is one whose call actually returned. */
.se-step[data-state="done"]{color:var(--text)}
.se-step[data-state="done"] .se-dot{border-color:var(--ok);color:var(--ok)}
.se-setup-body{overflow:auto;min-height:0;padding:16px 18px 24px}
.se-setup-body h1{font-size:17px;margin-bottom:5px}
.se-setup-lead{color:var(--muted);font-size:12.5px;margin:0 0 16px;max-width:66ch}
/* Orientation. In flow at the top of the pane, so it pushes the flow down rather
   than covering any of it — and it is bounded by measure rather than by height,
   because prose that scrolls inside its own box is prose nobody finishes. */
.se-orient{border:1px solid var(--border);border-left:2px solid var(--accent);
  border-radius:var(--radius-md);background:var(--panel);padding:11px 12px;margin:0 0 14px;
  font-size:12.5px;max-width:78ch}
.se-orient p{margin:0 0 7px}
.se-orient p:last-child{margin-bottom:0}
.se-orient-lead{color:var(--text-strong);font-weight:600}
/* The step's own lines, under its name rather than beside it: a description and a
   blocker are sentences, and a flex row would set them in the dot's column. */
.se-step .se-note{display:block;margin:3px 0 0}
.se-step .se-note[data-step-blocked="true"]{color:var(--warn)}

/* Config pane: the roles table, and the segment-wise match trace under it.
   Model and Effort are separate columns rather than one joined string — they are
   two independent decisions and a reviewer scans down one of them — which is the
   losing mockup's shape, carried over by post-selection correction 6. */
table.se-roles{width:100%;border-collapse:collapse;font-size:12px}
table.se-roles th{text-align:left;font-size:10px;text-transform:uppercase;letter-spacing:.05em;
  color:var(--muted);font-weight:600;padding:0 8px 5px 0;border-bottom:1px solid var(--border)}
table.se-roles td{padding:7px 8px 7px 0;border-bottom:1px solid var(--border);vertical-align:top}
table.se-roles tr:last-child td{border-bottom:none}
table.se-roles tr[aria-selected="true"] td{background:var(--accent-subtle)}
.se-r{font-weight:600;color:var(--text-strong)}
.se-src{font-family:var(--mono);font-size:10.5px;color:var(--muted);overflow-wrap:anywhere}
.se-rolebtn{background:none;border:none;padding:0;color:var(--text-strong);font-weight:600;
  text-align:left}
.se-rolebtn[aria-pressed="true"]{color:var(--accent);text-decoration:underline}
.se-rolebtn:focus-visible{outline:2px solid var(--ring);outline-offset:1px}
/* The match trace. One line per layer consulted, hit or miss, so the precedence is
   read rather than remembered. */
.se-seg{font-family:var(--mono);font-size:11px;line-height:1.85;color:var(--muted);
  background:var(--bg-accent);border:1px solid var(--border);border-radius:var(--radius-sm);
  padding:8px 9px;overflow-wrap:anywhere}
.se-hit{color:var(--ok);font-weight:700}
.se-miss{color:var(--muted-strong);text-decoration:line-through}
.se-seg-note{font-family:var(--font-body);color:var(--text);text-decoration:none}
/* Advisories. The acknowledgment-requiring ones are marked, because an advisory a
   human must answer for is a different obligation from one they only read. */
ul.se-advisories{margin:6px 0 0;padding:0;list-style:none;font-size:11.5px}
ul.se-advisories li{padding:5px 0;border-bottom:1px solid var(--border)}
ul.se-advisories li:last-child{border-bottom:none}
ul.se-advisories li[data-ack="true"]{color:var(--text-strong)}
.se-adv-text{display:block;margin-top:3px;color:var(--text);overflow-wrap:anywhere}
.se-flag[data-flag="ack"]{color:var(--warn)}
.se-flag[data-flag="dropped"]{color:var(--warn)}
.se-flag[data-flag="unanswered"]{color:var(--danger)}
.se-flag[data-flag="unmet"]{color:var(--warn)}
/* The document editor. A FIXED height, not a cap: a document that grew with its
   line count would push the save controls off the pane. */
textarea.se-json{width:100%;resize:vertical;min-height:220px}

/* Setup flow: one box per question, evidence rows above them. */
.se-qbox{border:1px solid var(--border);border-radius:var(--radius-md);padding:11px 12px;
  margin-bottom:12px;background:var(--panel)}
.se-qbox h3{font-size:10.5px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);
  margin-bottom:8px}
.se-evid{display:flex;flex-direction:column;gap:2px}
.se-evid-row{display:grid;grid-template-columns:150px minmax(0,1fr) auto;gap:10px;
  align-items:start;padding:7px 0;border-bottom:1px solid var(--border);font-size:12px}
.se-evid-row:last-child{border-bottom:none}
.se-evid-row[data-approved="true"]{color:var(--text-strong)}
.se-subj{font-size:11px;color:var(--muted);overflow-wrap:anywhere}
.se-evid-item{display:block;margin-top:5px}
.se-offer{display:flex;flex-wrap:wrap;align-items:center;gap:8px;padding:5px 0;
  border-bottom:1px solid var(--border)}
.se-offer:last-of-type{border-bottom:none}
.se-rung{display:flex;flex-wrap:wrap;align-items:center;gap:8px;padding:5px 0;font-size:12px}
.se-rung .se-note{flex:1 1 100%;margin:0}
/* The path field and the picker's trigger on one line. The trigger stays at its
   own width so the field takes the rest: the field is the fallback that must work
   whether or not the directory read does. */
.se-pathrow{display:flex;align-items:center;gap:6px}
.se-pathrow .se-input{flex:1 1 auto}
.se-pathrow .se-btn{flex:none;display:inline-flex;align-items:center;gap:5px}
.se-note[data-browse-error="true"]{color:var(--warn)}

/* The status strip. A grid row, never an overlay: this is the one thing on the
   page that must never be occluded, and the whole strip turns danger-coloured
   when the stop is in force so the engaged state is not a badge you can miss.

   The row is auto-sized rather than fixed at the strip's own height because the
   kill switch's arm-then-confirm step and its verdict take a full line INSIDE the
   strip. That is the only shape available: a popover, drawer or dialog would put
   the confirmation over the page, which is the failure the no-overlay rule above
   exists to prevent, and the one control it matters most for. */
.se-status{grid-area:status;background:var(--chrome);border-top:1px solid var(--border);
  display:flex;align-items:center;flex-wrap:wrap;gap:14px;padding:0 14px;min-height:34px;
  font-size:11.5px;overflow-x:auto}
.se-status .se-sep{width:1px;height:16px;background:var(--border);flex:none}
.se-lbl{color:var(--muted);text-transform:uppercase;letter-spacing:.05em;font-size:10px}
.se-val{font-family:var(--mono);color:var(--text-strong)}
.se-ks{display:flex;align-items:center;gap:7px;margin-left:auto;flex:none}
/* Inherits its colour, so one declaration works on the ordinary strip and on the
   danger-coloured one. */
.se-ks button{background:transparent;border:1px solid currentColor;color:inherit;
  border-radius:3px;padding:1px 8px;font-size:11px;font-weight:600}
.se-ks button:focus-visible{outline:2px solid var(--ring);outline-offset:1px}
.se-ks button[disabled]{opacity:.5;cursor:not-allowed}
/* Three states, because doubt must not read as released. Solid green is go, solid
   danger is stopped, and a hollow danger ring is "not read" — a pending or failed
   read, which the engine's own reader treats as engaged. All three are
   distinguishable from each other, so the dot never says what the text denies. */
.se-ks-dot{width:7px;height:7px;border-radius:50%;background:var(--ok);flex:none}
.se-ks-dot[data-state="engaged"]{background:var(--danger)}
.se-ks-dot[data-state="unknown"]{background:transparent;
  box-shadow:inset 0 0 0 2px var(--danger)}
.se-ks-text{font-weight:600;color:var(--text-strong)}
.se-status[data-engaged="true"]{background:var(--danger);border-top-color:var(--danger)}
.se-status[data-engaged="true"] .se-lbl,
.se-status[data-engaged="true"] .se-val,
.se-status[data-engaged="true"] .se-ks-text{color:var(--danger-fg)}
.se-status[data-engaged="true"] .se-ks-dot{background:var(--danger-fg)}
/* The confirmation and the verdict: a full line of the strip, in flow, above the
   reading. Its own surface colour so it stays legible when the strip goes danger,
   and the label overrides are re-stated at higher specificity for the same reason. */
.se-ks-panel{flex:1 0 100%;order:-1;margin:9px 0 3px;padding:10px 11px;
  background:var(--panel);color:var(--text);border:1px solid var(--border-strong);
  border-radius:var(--radius-md);font-size:12px;
  /* Bounded, in the strip's own defense: a long stored engage reason would
     otherwise grow this panel and shrink the work row above it — and the
     strip is the one region the design forbids being displaced. */
  max-height:200px;overflow:auto}
.se-ks-panel>*+*{margin-top:8px}
.se-ks-panel .se-arm{margin-top:0}
.se-status[data-engaged="true"] .se-ks-panel .se-lbl{color:var(--muted)}
.se-status[data-engaged="true"] .se-ks-panel .se-val{color:var(--text-strong)}
/* A release is not a teardown: the arm block keeps its shape but drops the danger
   tint, because the destructive direction here is the one that STOPS work. */
.se-ks-panel[data-armed="release"] .se-arm{background:var(--warn-subtle);
  border-color:var(--warn)}
.se-ks-panel[data-armed="release"] .se-arm svg{color:var(--warn)}
.se-ks-panel .se-idfield{margin:8px 0 0}
`
