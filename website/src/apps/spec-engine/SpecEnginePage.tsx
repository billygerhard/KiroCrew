/**
 * The Spec Engine Operator_Surface — the page shell.
 *
 * Built to `design/mockup-b.html` ("Operator Console"), the mockup a reviewer
 * agent selected against `design/criteria.md`. That selection is recorded in
 * `design/selection.md` and is **VETO-PENDING** for the owner; overturning it
 * re-runs the frontend wave and nothing upstream.
 *
 * ## The geometry, and which parts of it are load-bearing
 *
 * `"rail work" / "rail status"`: a vertical rail on the left, the work area, and
 * a status strip that is a GRID ROW rather than a floating bar. The work area is
 * one split — an ordered table on the left, a permanently docked pane on the
 * right — and every pane reuses that split, so moving between the queue and the
 * configuration costs no re-orientation.
 *
 * Two properties are not styling choices:
 *
 * 1. **Nothing overlays anything.** The kill-switch state and the spend figure
 *    are on screen in every pane at every scroll position because the strip is a
 *    row of the grid. The losing mockup failed that criterion on precisely this:
 *    its detail drawer's scrim covered the header, so the stop control was dimmed
 *    and click-blocked exactly when it mattered. A drawer, modal or scrim added
 *    later would silently reintroduce that failure — see the rule and its test in
 *    `styles.ts`.
 * 2. **The inspector is docked, not summoned.** Detail follows selection with no
 *    open step and no dismiss step, which is what makes working a backlog down to
 *    zero cost one keystroke per run instead of three.
 *
 * ## Reviewer, not driver
 *
 * The blocking selection criterion: every control on the default path acts on
 * work the engine already produced. Nothing here asks a human to compose or edit
 * spec prose. The losing mockup's `Rewrite the gate myself` button is the
 * affordance that criterion exists to exclude, and it is deliberately not ported.
 *
 * ## What this shell owns, and what the panels own
 *
 * This shell owns the grid, the rail, the ordered table with real rows and
 * keyboard selection, the docked inspector's identity header, first-run routing,
 * and the status strip's queue-scoped figures.
 *
 * First-run routing is ONE derivation. The landing pane and the rail's order both
 * read the same `firstRun` value, so the rail cannot lead with the assistant while
 * the landing rule opens the queue; and because that value is guarded against a
 * failed read, neither the routing nor the alarm on the setup entry can keep
 * claiming "nothing is configured" from data a later read failed to confirm.
 *
 * The inspector's BODY belongs to `ReviewQueuePanel.tsx`, which is keyed by the
 * selected run so its state cannot outlive the selection. The configuration pane
 * (the document as the write path, its resolved read beside it) belongs to
 * `ConfigPanel.tsx`, and the four-step setup flow to `SetupFlowPanel.tsx` — the
 * step rail lives there rather than here because its state IS the flow's state, and
 * a rail rendered from the shell would have to be told what step it was on. The
 * kill switch — its reading, its dot and its arm-then-confirm control — and the
 * per-run spend block belong to `SafetyPanel.tsx`; this shell reads the same
 * `QK.killSwitch` cache entry only to colour the strip when the stop is in force,
 * so the dot and the words beside it cannot come from two readings of one flag.
 *
 * The mockup's inspector TAB STRIP is deliberately not built. Every pane it
 * switched between is here, stacked, because the queue panel shipped them that way
 * and moving shipped blocks behind navigation is what the selection criteria argue
 * against: an operator would have to find a tab to learn that a run's revision
 * cycles are spent.
 *
 * ## Backend contract
 *
 * `src/kiro_crew/apps/builtins/spec_engine/backend/routes.py`, through `api.ts`.
 * Three reads run in this shell: the configuration (which is also the first-run
 * signal), the review queue, and the kill switch. The configuration read is the one
 * whose FAILURE mode matters: `config_unreadable` means a document exists and cannot
 * be parsed, which is not "nothing is configured", and an operator sent to the setup
 * assistant on that signal would meet a flow that refuses to overwrite a file it
 * cannot read. The panels add their own calls — the resolved configuration read, and
 * the setup flow's three steps — and the config read is handed DOWN to the config
 * pane rather than repeated there, so first-run routing and the pane cannot disagree
 * about whether a document exists.
 */
import { useCallback, useMemo, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  AlertTriangle,
  Cog,
  ListOrdered,
  ShieldCheck,
  Wand2,
  type LucideIcon,
} from 'lucide-react'

import { i18nT } from '../../i18n/t'
import { fmtDuration, fmtNumber, type FormatUnit } from '../../i18n/format'
import {
  QK,
  SpecEngineApiError,
  specEngineApi,
  type QueueEntry,
  type WaitingOn,
} from './api'
import { SE_CSS } from './styles'
import { ConfigPane } from './ConfigPanel'
import { RowFlags, RunInspectorBody } from './ReviewQueuePanel'
import { KillSwitchControls, switchReading } from './SafetyPanel'
import { SetupFlowPanel } from './SetupFlowPanel'

/** Which pane the work area shows. Panes, not destinations: one list, one document. */
type Pane = 'queue' | 'config' | 'setup'

/**
 * The rail's label key per pane, indexed at the call site so the key-reference
 * gate can resolve every key this map can produce. A flat `Record<Pane, string>`
 * rather than a field on `PANE_ICON`, because the checker resolves a non-literal
 * index into a module-level string map by unioning its values — a destructured
 * field off a nested map is invisible to it, which would exempt these three keys
 * from every key check. Holds the KEY, not the resolved string, for the same
 * reason `WHY_KEY` does: a module-level `i18nT()` would run once at import and
 * freeze the rail in whatever language was active then.
 */
const PANE_LABEL_KEY: Record<Pane, string> = {
  queue: 'apps.specEngine.specEnginePage.queue',
  config: 'apps.specEngine.specEnginePage.configuration',
  setup: 'apps.specEngine.specEnginePage.setup_assistant',
}

/** The rail's glyph per pane. A table, so the ordered rail render stays one loop. */
const PANE_ICON: Record<Pane, LucideIcon> = {
  queue: ListOrdered,
  config: Cog,
  setup: Wand2,
}

/**
 * The rail's order while first run: the assistant leads, because it is the only
 * pane that can produce anything on an unconfigured engine.
 */
const FIRST_RUN_PANE_ORDER: readonly Pane[] = Object.freeze<Pane[]>(['setup', 'queue', 'config'])

/**
 * The rail's order once a project is configured: the work the operator came for
 * leads, and the assistant drops to last, where it stays reachable for the next
 * project.
 */
const CONFIGURED_PANE_ORDER: readonly Pane[] = Object.freeze<Pane[]>(['queue', 'config', 'setup'])

/**
 * The rail's order, from the SAME `firstRun` value the landing rule reads.
 *
 * One derivation feeding both is the point: a rail computed from its own
 * condition could put the assistant first while the landing rule sent the
 * operator to the queue, and neither half would look wrong on its own. Every
 * pane appears in both orders — the order changes which is loudest, never which
 * are reachable.
 */
function paneOrder(firstRun: boolean): readonly Pane[] {
  return firstRun ? FIRST_RUN_PANE_ORDER : CONFIGURED_PANE_ORDER
}

/** The queue filter. `all` plus one per `WaitingOn`, because those are three jobs. */
type Filter = 'all' | WaitingOn

/**
 * The queue read is a snapshot of work waiting on a person, so it goes stale
 * quietly — a run leaves the queue when somebody else acts on it. Polled rather
 * than pushed: this surface has no event stream, and a stale queue that looks
 * live is how two operators act on the same row.
 */
const QUEUE_POLL_MS = 15000

/** Separator between two identifiers on one line. Punctuation, not copy. */
const SEP = ' \u00b7 '
/** Stands in for a field the engine has no value for. Punctuation, not copy. */
const NONE = '\u2014'

/**
 * A frozen empty list, so a pending or failed read yields a referentially STABLE
 * fallback. `?? []` allocates a new array per render, which makes it a changing
 * dependency of the memos below and defeats them silently.
 */
const NO_ENTRIES: readonly QueueEntry[] = Object.freeze([])

/**
 * The waiting reason in words, keyed by what the run waits on.
 *
 * A coloured dot says a run is waiting; it does not say what for, and the reason
 * is what decides which actions are legitimate. A run parked at a budget ceiling
 * offered "approve this gate" is the confusion the selection criteria forbid, so
 * the reason is stated in prose beside the identity rather than left to a hue.
 *
 * Holds KEYS, not resolved strings: a module-level `i18nT()` would run once at
 * import and freeze this table in the language that happened to be active then.
 */
const WHY_KEY: Record<WaitingOn, string> = {
  review: 'apps.specEngine.specEnginePage.why_review',
  budget: 'apps.specEngine.specEnginePage.why_budget',
  stall: 'apps.specEngine.specEnginePage.why_stall',
}

/**
 * Spent revision cycles are their own reason, not a footnote on `review`.
 *
 * The run stays in `awaiting_review` and keeps `waiting_on: review`, so the
 * engine's two fields together are the only thing that distinguishes "waiting for
 * a verdict" from "waiting for a verdict with no further revision turn coming".
 */
const WHY_EXHAUSTED_KEY = 'apps.specEngine.specEnginePage.why_review_exhausted'

/**
 * The waiting reason as a cell value, keyed the same way.
 *
 * Every key here is a whole literal string. A key assembled from the enum member
 * (`` `…${entry.waiting_on}` ``) would resolve at runtime, which puts it beyond
 * every gate that checks a key exists — and a missing key renders as its own
 * dotted path in the UI rather than failing.
 */
const WAIT_LABEL_KEY: Record<WaitingOn, string> = {
  review: 'apps.specEngine.specEnginePage.verdict',
  budget: 'apps.specEngine.specEnginePage.budget',
  stall: 'apps.specEngine.specEnginePage.stall',
}

/** The filter chips, in the order a reader scans them. */
const FILTERS: ReadonlyArray<{ id: Filter; labelKey: string }> = [
  { id: 'all', labelKey: 'apps.specEngine.specEnginePage.all' },
  { id: 'review', labelKey: WAIT_LABEL_KEY.review },
  { id: 'budget', labelKey: WAIT_LABEL_KEY.budget },
  { id: 'stall', labelKey: WAIT_LABEL_KEY.stall },
]

/**
 * A wait, split into the two coarsest units that carry information.
 *
 * Split here rather than inside the formatter because granularity is a product
 * decision: a run that has waited a day and a half is a different problem from
 * one that has waited four minutes, and seconds never change that reading.
 */
function waitedParts(seconds: number): Array<[number, FormatUnit]> {
  const whole = Math.max(0, Math.floor(seconds))
  const days = Math.floor(whole / 86400)
  const hours = Math.floor((whole % 86400) / 3600)
  const minutes = Math.floor((whole % 3600) / 60)
  if (days > 0) return [[days, 'day'], [hours, 'hour']]
  if (hours > 0) return [[hours, 'hour'], [minutes, 'minute']]
  return [[minutes, 'minute']]
}

/** The refusal code behind an error, or `''` when it is not one of ours. */
function refusalCode(error: unknown): string {
  return error instanceof SpecEngineApiError ? error.code : ''
}

/** A refusal's human text, for the one line under the code. */
function refusalText(error: unknown): string {
  return error instanceof Error ? error.message : ''
}

/** A refusal block: the sentence a reader acts on, with the code underneath. */
function Refusal({ title, error }: { title: string; error: unknown }) {
  const code = refusalCode(error)
  return (
    <div className="se-refusal" role="alert">
      {title}
      <code>{code ? `${code}${SEP}${refusalText(error)}` : refusalText(error)}</code>
    </div>
  )
}

export default function SpecEnginePage() {
  // `retry: false` on all three: every failure here is a refusal the operator
  // has to read (a disabled app, an unparseable document, an unreadable
  // database), and retrying one silently turns a stated reason into a spinner.
  const config = useQuery({
    queryKey: QK.config,
    queryFn: () => specEngineApi.config(),
    retry: false,
  })
  const queue = useQuery({
    queryKey: QK.queue,
    queryFn: () => specEngineApi.queue(),
    retry: false,
    refetchInterval: QUEUE_POLL_MS,
  })
  const killSwitch = useQuery({
    queryKey: QK.killSwitch,
    queryFn: () => specEngineApi.killSwitch(),
    retry: false,
  })

  // `null` means the operator has not chosen a pane, so the landing pane is still
  // the configuration read's to decide. Once they click, their choice pins.
  const [chosenPane, setChosenPane] = useState<Pane | null>(null)
  const [filter, setFilter] = useState<Filter>('all')
  const [selectedRunId, setSelectedRunId] = useState<string>('')
  const rowRefs = useRef(new Map<string, HTMLDivElement>())

  /**
   * Workspace ids a teardown reported it KEPT, by run.
   *
   * Lifted to the page because it is read in two places that are not nested: the
   * row's state words and the inspector's teardown block. It is not a `QueueEntry`
   * field and cannot be — the queue projection has no notion of a kept workspace,
   * and the count only comes into existence when a teardown reports one. Held for
   * the life of the page rather than persisted: it says "this session tore down
   * that run and these ids survived it", which is exactly the claim the operator
   * needs and the only one this data supports.
   */
  const [keptByRun, setKeptByRun] = useState<Record<string, number[]>>({})
  const noteKept = useCallback((runId: string, kept: number[]) => {
    setKeptByRun((current) => ({ ...current, [runId]: kept }))
  }, [])

  /**
   * First run: the configuration read succeeded and holds no project entry.
   *
   * Project entries, not the `configured` flag: `configured` says only that the
   * FILE exists, and a document can exist while configuring no project — one
   * app-scoped save from the configuration pane creates the file. An engine in
   * that state still has nothing to run against, so the assistant still leads.
   * An absent file trivially holds no project entry, so both arms of the
   * definition are one rule.
   *
   * The `isError` half is not redundant: React Query RETAINS the last data
   * across a failed refetch, so a projectless snapshot followed by a failed
   * read would go on asserting first run from a reading nothing currently
   * confirms — the same defect class the kill-switch dot closed one component
   * over. Doubt is not absence. A read that FAILED is not first run — a
   * document that cannot be parsed is a repair, and the assistant would refuse
   * to write over it.
   *
   * This is the single derivation. The landing rule below and the rail's order
   * both read THIS value, so the two cannot disagree about whether the engine is
   * unconfigured; and because the guard sits here, it covers the setup pane's
   * alarm marker as well, which asserts "unconfigured" and must fall silent on a
   * read nobody could complete.
   */
  const projects = config.data?.document.projects
  const projectCount =
    projects !== null && typeof projects === 'object' ? Object.keys(projects).length : 0
  const firstRun = !config.isError && config.data !== undefined && projectCount === 0

  const pane: Pane | null =
    chosenPane ?? (config.isPending ? null : firstRun ? 'setup' : 'queue')

  const entries = queue.data?.entries ?? NO_ENTRIES
  const counts = useMemo(() => {
    const tally: Record<Filter, number> = { all: entries.length, review: 0, budget: 0, stall: 0 }
    for (const entry of entries) tally[entry.waiting_on] += 1
    return tally
  }, [entries])

  const rows = useMemo(
    () => (filter === 'all' ? entries : entries.filter((e) => e.waiting_on === filter)),
    [entries, filter],
  )

  // Selection follows the list: a row that left the queue (somebody else acted on
  // it) must not keep a docked pane describing a run nobody can act on.
  const selected: QueueEntry | undefined =
    rows.find((entry) => entry.run_id === selectedRunId) ?? rows[0]

  /**
   * Keyboard traversal over the rows: roving tabindex, selection follows focus.
   *
   * The mockup marked the selected row with `aria-selected` on a plain `<tr>` with
   * no role, no tabindex and no key handling, which announces a selection to a
   * screen reader while making it unreachable without a pointer. The grid pattern
   * is what makes the advertised `j`/`k` traversal real: exactly one row is in the
   * tab order, the arrow keys move focus within the grid, and selection follows
   * focus so there is no second "commit" step to discover.
   */
  const focusRow = useCallback((runId: string) => {
    rowRefs.current.get(runId)?.focus()
  }, [])

  const onRowKeyDown = useCallback(
    (event: React.KeyboardEvent<HTMLDivElement>, index: number) => {
      if (event.metaKey || event.ctrlKey || event.altKey) return
      let next = -1
      if (event.key === 'ArrowDown' || event.key === 'j') next = index + 1
      else if (event.key === 'ArrowUp' || event.key === 'k') next = index - 1
      else if (event.key === 'Home') next = 0
      else if (event.key === 'End') next = rows.length - 1
      else return
      event.preventDefault()
      const target = rows[Math.min(Math.max(next, 0), rows.length - 1)]
      if (target) focusRow(target.run_id)
    },
    [rows, focusRow],
  )

  // The same guard the panel applies to its own reading: React Query keeps the
  // last data across a failed refetch, so an unguarded reading would keep
  // asserting whatever the previous read found. The tint is a positive claim
  // that a stop is in force — doubt un-tints, and the doubt itself is carried
  // by the dot and text the strip renders through KillSwitchControls.
  const ksReading = killSwitch.isError
    ? 'unknown'
    : switchReading(killSwitch.data?.switch)
  const engaged = ksReading === 'engaged'

  return (
    <div className="se-root">
      <style>{SE_CSS}</style>

      <nav className="se-rail" aria-label={i18nT('apps.specEngine.specEnginePage.panes')}>
        <span className="se-brand">
          <ShieldCheck className="lucide-inline" aria-hidden="true" />
          {i18nT('apps.specEngine.manifest.page_label')}
        </span>

        {/* Rendered from the ordered pane list rather than written out three times,
            so the first-run rail and the configured rail are one sequence with one
            source of truth. `data-pane` is what the shell's tests read the order
            from: a label-text ordering assertion would break on translation and
            says nothing about which pane a button reaches. */}
        {paneOrder(firstRun).map((id) => {
          const Icon = PANE_ICON[id]
          return (
            <button
              key={id}
              type="button"
              className="se-nav"
              data-pane={id}
              aria-current={pane === id ? 'page' : undefined}
              // The first-run alarm marks the pane an unconfigured engine has to
              // visit, so the entry is the loudest thing in the rail rather than
              // one of three equals. It rides `firstRun`, which is false whenever
              // the configuration read is in error — an alarm is a positive claim
              // that nothing is configured, and a failed read is not that claim.
              data-alarm={id === 'setup' && firstRun ? 'true' : undefined}
              onClick={() => setChosenPane(id)}
            >
              <Icon className="lucide-inline" aria-hidden="true" />
              {i18nT(PANE_LABEL_KEY[id])}
              {id === 'queue' && <span className="se-badge">{fmtNumber(entries.length)}</span>}
              {id === 'setup' && firstRun && (
                <span className="se-badge">
                  <AlertTriangle className="lucide-inline" aria-hidden="true" />
                </span>
              )}
            </button>
          )
        })}

        <div className="se-rail-foot">
          <div className="se-keys">
            <kbd>j</kbd> <kbd>k</kbd> {i18nT('apps.specEngine.specEnginePage.move_between_runs')}
          </div>
        </div>
      </nav>

      {pane === null ? (
        /* The landing pane is still the configuration read's to decide. Without
           this branch, null fell through to the queue pane, so an unconfigured
           engine flashed the run list before switching to setup. */
        <div className="se-work" data-pane-pending="true">
          <section className="se-rows" aria-busy="true">
            <p className="se-lbl">
              {i18nT('apps.specEngine.specEnginePage.reading_the_configuration')}
            </p>
          </section>
        </div>
      ) : pane === 'setup' ? (
        <div className="se-setup">
          <SetupFlowPanel />
        </div>
      ) : pane === 'config' ? (
        <div className="se-work">
          <ConfigPane config={config.data} error={config.error} pending={config.isPending} />
        </div>
      ) : (
        <div className="se-work">
          <section className="se-list">
            <div className="se-list-head">
              <h1>{i18nT('apps.specEngine.specEnginePage.runs')}</h1>
              <span className="se-sort">
                {i18nT('apps.specEngine.specEnginePage.sorted_by_time_waiting_longest_first')}
              </span>
            </div>
            {/* Filters over one list, never columns: the engine's waiting reason is a
                cell value here, so a run cannot be missed by looking in the wrong
                container. */}
            <div className="se-filters">
              {FILTERS.map(({ id, labelKey }) => (
                <button
                  key={id}
                  type="button"
                  className="se-filter"
                  aria-pressed={filter === id}
                  onClick={() => setFilter(id)}
                >
                  {i18nT(labelKey)}
                  <span className="se-filter-count">{fmtNumber(counts[id])}</span>
                </button>
              ))}
            </div>
            <div className="se-rows">
              {queue.isError ? (
                <div className="se-empty">
                  <Refusal
                    title={i18nT('apps.specEngine.specEnginePage.could_not_read_the_run_queue')}
                    error={queue.error}
                  />
                  <button
                    type="button"
                    className="se-btn"
                    style={{ marginTop: 10 }}
                    onClick={() => void queue.refetch()}
                  >
                    {i18nT('apps.specEngine.specEnginePage.retry')}
                  </button>
                </div>
              ) : queue.isPending ? (
                <div className="se-empty">
                  {i18nT('apps.specEngine.specEnginePage.reading_the_queue')}
                </div>
              ) : rows.length === 0 ? (
                <div className="se-empty">
                  {entries.length === 0
                    ? i18nT('apps.specEngine.specEnginePage.nothing_is_waiting_on_a_person')
                    : i18nT('apps.specEngine.specEnginePage.nothing_matches_this_filter')}
                </div>
              ) : (
                <div
                  className="se-q"
                  role="grid"
                  aria-label={i18nT('apps.specEngine.specEnginePage.runs')}
                >
                  <div className="se-qhead" role="row">
                    <span role="columnheader">
                      {i18nT('apps.specEngine.specEnginePage.col_waiting_on')}
                    </span>
                    <span role="columnheader">
                      {i18nT('apps.specEngine.specEnginePage.col_spec_and_project')}
                    </span>
                    <span role="columnheader">
                      {i18nT('apps.specEngine.specEnginePage.col_gate')}
                    </span>
                    <span role="columnheader">
                      {i18nT('apps.specEngine.specEnginePage.col_run')}
                    </span>
                    <span role="columnheader">
                      {i18nT('apps.specEngine.specEnginePage.col_waited')}
                    </span>
                    <span role="columnheader">
                      {i18nT('apps.specEngine.specEnginePage.col_credits')}
                    </span>
                  </div>
                  {rows.map((entry, index) => {
                    const isSelected = selected?.run_id === entry.run_id
                    return (
                      <div
                        key={entry.run_id}
                        ref={(node) => {
                          if (node) rowRefs.current.set(entry.run_id, node)
                          else rowRefs.current.delete(entry.run_id)
                        }}
                        className="se-row"
                        role="row"
                        aria-selected={isSelected}
                        tabIndex={isSelected ? 0 : -1}
                        onFocus={() => setSelectedRunId(entry.run_id)}
                        onClick={() => focusRow(entry.run_id)}
                        onKeyDown={(event) => onRowKeyDown(event, index)}
                      >
                        <span role="gridcell">
                          <span className="se-wait" data-wait={entry.waiting_on}>
                            {i18nT(WAIT_LABEL_KEY[entry.waiting_on])}
                          </span>
                        </span>
                        <span role="gridcell">
                          <span className="se-spec">{entry.spec}</span>
                          <span className="se-id">{SEP}{entry.project}</span>
                          {/* The row's own state words. Absent from the shell as
                              shipped, and the reason they belong on the ROW is
                              that they change which actions are legitimate: a
                              reader scanning the list must not have to select a
                              run to learn its revision cycles are spent. */}
                          <RowFlags
                            entry={entry}
                            keptCount={keptByRun[entry.run_id]?.length ?? 0}
                          />
                        </span>
                        <span role="gridcell">{entry.gate || NONE}</span>
                        <span role="gridcell" className="se-id">{entry.run_id}</span>
                        <span role="gridcell" className="se-age">
                          {fmtDuration(waitedParts(entry.waiting_s))}
                        </span>
                        <span role="gridcell" className="se-cost">
                          {fmtNumber(entry.cost_credits, { maximumFractionDigits: 1 })}
                        </span>
                      </div>
                    )
                  })}
                </div>
              )}
            </div>
          </section>

          <section
            className="se-inspector"
            aria-label={i18nT('apps.specEngine.specEnginePage.selected_run')}
          >
            {selected ? (
              <>
                <div className="se-insp-head">
                  <span className="se-insp-title">{selected.spec}</span>
                  <span className="se-insp-sub">
                    {selected.run_id}
                    {SEP}
                    {selected.state}
                    {selected.gate ? `${SEP}${selected.gate}` : ''}
                  </span>
                  <span className="se-insp-why">
                    {i18nT(
                      selected.waiting_on === 'review' && selected.revision_exhausted
                        ? WHY_EXHAUSTED_KEY
                        : WHY_KEY[selected.waiting_on],
                    )}
                  </span>
                </div>
                <div className="se-insp-body">
                  {/* Keyed by run, so every piece of the panel's local state — an
                      armed teardown, a typed identifier, a previous result —
                      belongs to the run on screen. The mockup's inspector was
                      static below its header, which left the first run's detail in
                      place when a different row was selected. */}
                  <RunInspectorBody key={selected.run_id} entry={selected} onKept={noteKept} />
                </div>
              </>
            ) : (
              <div className="se-insp-body">
                <p className="se-note">
                  {i18nT('apps.specEngine.specEnginePage.select_a_run_to_see_it_here')}
                </p>
              </div>
            )}
          </section>
        </div>
      )}

      {/* The status strip. A grid row in every pane, at every scroll position,
          because a stop control you have to navigate to is not a stop control. */}
      <div
        className="se-status"
        data-engaged={engaged ? 'true' : 'false'}
        aria-label={i18nT('apps.specEngine.specEnginePage.safety_and_spend')}
      >
        {queue.isError ? (
          /* An unread queue must not read as an empty one. The two figures on
             this strip are queue-derived, so when that read failed they are
             unknown — rendering 0 here would be the fail-open the kill-switch
             text two spans down deliberately refuses. */
          <span className="se-lbl" data-strip-error="queue">
            {i18nT('apps.specEngine.specEnginePage.could_not_read_the_run_queue')}
          </span>
        ) : queue.isPending ? (
          /* The same argument one state earlier. Before the read lands the figures
             are not zero, they are unknown, and "Spend 0 / Waiting 0" is a
             confident claim this surface has no basis for — on the config and setup
             panes it would be the only spend figure on screen. */
          <span className="se-lbl" data-strip-pending="queue">
            {i18nT('apps.specEngine.specEnginePage.reading_the_queue')}
          </span>
        ) : (
          <>
            {/* Scoped label: total_credits sums ONLY runs waiting on a person
                (the queue route's population), not runs working or closed. An
                unqualified "Spend" would under-report by an unbounded amount.
                The per-run figure, with the ceiling it is judged against, is in
                the inspector's spend block. */}
            <span className="se-lbl">
              {i18nT('apps.specEngine.specEnginePage.spend_on_waiting_runs')}
            </span>
            <span className="se-val">
              {fmtNumber(queue.data?.total_credits ?? 0, { maximumFractionDigits: 1 })}
            </span>
            <span className="se-lbl">{i18nT('apps.specEngine.specEnginePage.credits')}</span>
            <span className="se-sep" />
            <span className="se-lbl">
              {i18nT('apps.specEngine.specEnginePage.waiting_on_a_person')}
            </span>
            <span className="se-val">{fmtNumber(entries.length)}</span>
          </>
        )}
        {/* The reading AND the control, from one component reading one cache entry.
            The shell keeps the strip's engaged styling and nothing else about the
            switch, so the dot, the words and the button cannot come from two
            different readings of one flag. */}
        <KillSwitchControls />
      </div>
    </div>
  )
}

/** Exported for the shell's own tests, which assert the reading rather than re-deriving it. */
export const __testing = {
  waitedParts,
  WHY_KEY,
  WHY_EXHAUSTED_KEY,
  WAIT_LABEL_KEY,
  paneOrder,
  PANE_LABEL_KEY,
}
