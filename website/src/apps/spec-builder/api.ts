// Thin fetch wrapper for the Spec Builder builtin backend (registered on the
// main gateway's aiohttp Application, base path /api/apps/spec-builder — same
// convention as issue-radar / code-review-sage). Ported from the external
// kiro-specs app's /api/apps/kiro-specs module.
const API = '/api/apps/spec-builder'

/** Shown in the rail footer. Mirrors the builtin's app.json version and the
 *  Issue Radar convention of surfacing the app version in its own rail. */
export const APP_VERSION = '0.1.0'

// ── domain types ──────────────────────────────────────────────────────────

/** One row in the specs rail (GET /specs). */
export interface SpecSummary {
  name: string
  phase: string
  /** e.g. "executing" while an agent is building the task list. */
  status?: string
  /** true while the spec's agent turn is in flight (drives the pulsing dot). */
  running?: boolean
}

export interface SpecListResponse {
  specs: SpecSummary[]
}

/** A single structured decision the agent surfaced for the user to answer. */
export interface SpecDecision {
  id: string
  title: string
  options?: string[]
  recommended?: string
  answer?: string
}

/** Phase-2 structured state the agent maintains in .spec-state.json. */
export interface SpecState {
  decisions?: SpecDecision[]
  blocking?: string
  context?: { template?: string }
}

/** Live counters exposed by the backend for the CONTEXT card. */
export interface SpecContextStats {
  turns?: number
  tool_calls?: number
  worktree_branch?: string
}

/** One document gate, as the ENGINE derived it. The browser renders these; it
 *  does not compute which one is next or whether one is approved. */
export interface SpecGate {
  gate: string
  document?: string
  present?: boolean
  approved?: boolean
  stale?: boolean
  approver?: string | null
  approved_ts?: string | null
}

/** The engine's answer to "where is this spec and what may it do next".
 *
 *  This exists because the app used to answer both questions itself: a
 *  client-side map decided which phase followed which, and the build control
 *  only checked that tasks.md existed. Every field here comes from the engine's
 *  own gate derivation, so the surface renders a decision rather than making
 *  one. `addressable: false` means the engine could not be asked — which reads as
 *  "no answer", never as "nothing blocks execution". */
export interface SpecEngineView {
  addressable?: boolean
  reason_code?: string
  gates?: SpecGate[]
  engine_phase?: string
  /** The gate a person is being asked about right now, or null when none is. */
  current_gate?: string | null
  can_execute?: boolean
  execution_blocked_by?: { code: string; message: string; gate?: string }[]
}

/** What one advance did, as the ENGINE decided it. */
export interface AdvanceResponse {
  ok?: boolean
  gate?: string
  from_phase?: string
  /** Where the spec goes next. The engine's answer; never computed here. */
  to_phase?: string | null
}

/** Full single-spec payload (GET /specs/{name}). */
export interface SpecDetail {
  name: string
  phase?: string
  status?: string
  working_dir?: string
  /** The chat slot this spec's conversation lives in. Server-assigned and
   *  per-creation, so it must never be derived client-side from the name. */
  slot_key?: string
  /** The spec's directory as the backend rendered it. Sent back with every
   *  mutation as a client-captured identity, so a stale tab cannot drive a
   *  same-name spec that was deleted and recreated elsewhere. */
  spec_dir?: string
  /** Document contents keyed by filename, e.g. { 'requirements.md': '…' }. */
  files?: Record<string, string>
  state?: SpecState
  context?: SpecContextStats
  running?: boolean
  /** The engine's gate state. Absent from an older backend, which is why every
   *  field is optional and the controls treat a missing view as "no advance
   *  offered" rather than as permission. */
  engine?: SpecEngineView
}

/** Directory listing for the project folder picker (GET /browse?path=). */
export interface BrowseEntry {
  name: string
  path: string
}

export interface BrowseResponse {
  path: string
  parent: string
  dirs: BrowseEntry[]
  /** true when `path` is (inside) a git repository — enables the worktree opt-in. */
  is_git?: boolean
  /** Recently-used project folders, returned on the initial (empty-path) browse. */
  recents?: string[]
}

export interface SettingsResponse {
  base_path?: string
}

/** Body for POST /specs. */
export interface CreateSpecBody {
  name: string
  working_dir: string
  spec_type: string
  description: string
  use_worktree: boolean
}

/** What the client rendered, sent back with every mutation so the server can
 *  refuse a stale control. Both fields are optional: omitting them means
 *  "unpinned", which is what an older tab does. */
export interface SpecIdentity {
  spec_dir?: string
  slot_key?: string
}

/** Drop empty fields so an absent value never reads as a claim of "". */
function identity(id?: SpecIdentity): Record<string, string> {
  const out: Record<string, string> = {}
  if (id?.spec_dir) out.spec_dir = id.spec_dir
  if (id?.slot_key) out.slot_key = id.slot_key
  return out
}

import { i18nT } from '../../i18n/t'

// ── fetch helper ────────────────────────────────────────────────────────────

async function req<T>(path: string, opts?: RequestInit): Promise<T> {
  const r = await fetch(API + path, {
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  })
  if (!r.ok) {
    let msg = i18nT('apps.specBuilder.api.something_went_wrong', { status: r.status })
    try {
      msg = ((await r.json()) as { error?: string }).error || msg
    } catch {
      /* non-JSON error body — keep the generic message */
    }
    throw new Error(msg)
  }
  if (r.status === 204) return undefined as T
  const text = await r.text()
  return (text.trim() === '' ? undefined : JSON.parse(text)) as T
}

const enc = (name: string) => encodeURIComponent(name)

export const specApi = {
  list: () => req<SpecListResponse>('/specs'),  create: (body: CreateSpecBody) => req<{ name?: string }>('/specs', { method: 'POST', body: JSON.stringify(body) }),
  get: (name: string) => req<SpecDetail>('/specs/' + enc(name)),
  // specDir is the identity the CLIENT rendered: the backend compares it against
  // the live index so a stale tab cannot drive a same-name spec that was deleted
  // and recreated pointing somewhere else.
  // identity is the pair the CLIENT rendered: the per-creation slot key plus the
  // spec_dir. The backend compares both and refuses a mismatch, because a
  // directory alone does not identify a creation -- delete leaves the documents on
  // disk, so a re-import under the same name and path is a DIFFERENT spec.
  message: (name: string, text: string, id?: SpecIdentity) =>
    req<void>('/specs/' + enc(name) + '/message', {
      method: 'POST',
      body: JSON.stringify({ text, ...identity(id) }),
    }),
  // Approve one gate WITH THE ENGINE, and advance past one. The advance records
  // the approval and then asks the engine to move: its response carries the
  // transition, which is why nothing here computes a next phase. The approver is
  // the authenticated session, never a field in this body.
  approve: (name: string, gate: string, id?: SpecIdentity) =>
    req<{ ok?: boolean; gate?: string }>('/specs/' + enc(name) + '/approve', {
      method: 'POST',
      body: JSON.stringify({ gate, ...identity(id) }),
    }),
  advance: (name: string, gate: string, id?: SpecIdentity) =>
    req<AdvanceResponse>('/specs/' + enc(name) + '/advance', {
      method: 'POST',
      body: JSON.stringify({ gate, ...identity(id) }),
    }),
  execute: (name: string, id?: SpecIdentity) =>
    req<void>('/specs/' + enc(name) + '/execute', {
      method: 'POST',
      body: JSON.stringify(identity(id)),
    }),
  stop: (name: string, id?: SpecIdentity) =>
    req<void>('/specs/' + enc(name) + '/stop', {
      method: 'POST',
      body: JSON.stringify(identity(id)),
    }),
  // DELETE has no body, so the identity rides the query string.
  remove: (name: string, id?: SpecIdentity) => {
    const q = new URLSearchParams(identity(id) as Record<string, string>).toString()
    return req<void>('/specs/' + enc(name) + (q ? '?' + q : ''), { method: 'DELETE' })
  },
  getSettings: () => req<{ base_path: string }>('/settings'),
  saveSettings: (base_path: string) =>
    req<{ ok: boolean }>('/settings', { method: 'POST', body: JSON.stringify({ base_path }) }),
  browse: (path: string) => {
    // Not copy: a URL. Built through URLSearchParams so the remaining literal has
    // the same shape as every other endpoint path in this file.
    const q = new URLSearchParams({ path: path || '' }).toString()
    return req<BrowseResponse>('/browse' + (q ? '?' + q : ''))
  },
}

// ── misc helpers ─────────────────────────────────────────────────────────────

/** localStorage keys (renamed from the external app's kiro-specs:* namespace). */
export const LS = {
  lastOpen: 'spec-builder:last-open',
  /** Legacy: '0' meant COLLAPSED. Read once for migration, never written. */
  railOpen: 'spec-builder:rail-open',
  /** Current: '1' means collapsed (the shared hook's encoding). */
  railCollapsed: 'spec-builder:rail-collapsed',
  railWidth: 'spec-builder:rail-width',
  docPct: 'spec-builder:doc-pct',
} as const

// ── rail geometry ────────────────────────────────────────────────────────────
// The specs rail is a resizable column (same hook Issue Radar's rail uses), so
// its width is a persisted number rather than a fixed class. Dragging well past
// the minimum collapses it to an icon strip.

export const DEFAULT_RAIL_WIDTH = 250
export const MIN_RAIL_WIDTH = 190
export const MAX_RAIL_WIDTH = 420
export const COLLAPSED_RAIL_WIDTH = 44

/** Persisted rail width, clamped — a corrupt value must not wedge the layout. */
export function loadRailWidth(): number {
  try {
    const v = Number(localStorage.getItem(LS.railWidth))
    if (Number.isFinite(v) && v >= MIN_RAIL_WIDTH && v <= MAX_RAIL_WIDTH) return v
  } catch { /* private mode — fall through */ }
  return DEFAULT_RAIL_WIDTH
}

/** Persisted collapsed flag.
 *
 *  The shared hook writes '1' for collapsed; the app's previous key wrote '0'
 *  for collapsed under the opposite name (rail-OPEN). Reusing that key would
 *  invert the state for anyone who had already collapsed the rail, so the flag
 *  moved to its own key and the old one is read once as a fallback.
 */
export function loadRailCollapsed(): boolean {
  try {
    const current = localStorage.getItem(LS.railCollapsed)
    if (current !== null) return current === '1'
    return localStorage.getItem(LS.railOpen) === '0'
  } catch {
    return false
  }
}

/** Slugify a free-text description into a stable spec name. */
export function slugify(text: string): string {
  return (text || '')
    .toLowerCase()
    .replace(/[^a-z0-9\s_-]/g, '')
    .trim()
    .split(/\s+/)
    .slice(0, 5)
    .join('-')
    .replace(/-+/g, '-')
    .slice(0, 48)
}

/**
 * Catalog key per phase. A literal Record of keys is the only shape
 * ``scripts/check-i18n-keys.mjs`` can resolve statically, so the table holds
 * keys and ``phaseLabel`` translates at the point of use.
 */
const PHASE_LABEL_KEY: Record<string, string> = {
  new: 'apps.specBuilder.api.phase_new',
  requirements: 'apps.specBuilder.api.phase_requirements',
  design: 'apps.specBuilder.api.phase_design',
  tasks: 'apps.specBuilder.api.phase_tasks',
}

/** Status overrides that are not phases: shown while the agent runs, and for a
 *  finished plan in the rail. */
export const PHASE_BUILDING_KEY = 'apps.specBuilder.api.phase_building'
export const PHASE_READY_KEY = 'apps.specBuilder.api.phase_ready'

/**
 * Localised label for a spec phase, or the phase id VERBATIM when the backend
 * reports one this table does not know — better than fabricating copy.
 *
 * ``hasOwnProperty``, not ``in``: the phase comes off a backend payload, so a
 * value like ``toString`` would otherwise resolve to an inherited
 * Object.prototype member and hand a function to i18next.
 */
export function phaseLabel(phase: string): string {
  return Object.prototype.hasOwnProperty.call(PHASE_LABEL_KEY, phase)
    ? i18nT(PHASE_LABEL_KEY[phase])
    : phase
}

// ── the engine's operator surfaces ──────────────────────────────────────────
//
// Configuration, per-run spend, and the stop control. Every value here is
// resolved by the ENGINE and relayed by the backend: this client renders what it
// is handed and derives nothing. In particular it does NOT decide which layer a
// setting's value came from -- `origin` is the engine's answer, and a second
// precedence implementation in the browser that disagreed with it would show an
// operator a value the engine does not use.

/** One setting: the value in force, where it came from, and what a write may set. */
export interface EffectiveSetting {
  key: string
  value: number | string | boolean
  /** bundled_default | app_config | cost_profile | project_config | source_config */
  origin: string
  /** Dotted path of the explicit declaration; empty for a bundled default. */
  declared_at: string
  is_default: boolean
  default?: number | string | boolean
  summary?: string
  kind?: string
  /** Scopes a write would be accepted at, so the form cannot offer a field the
   *  engine's write path then refuses. */
  scopes?: string[]
  minimum?: number | null
  maximum?: number | null
  choices?: string[]
}

/** Whether this surface writes one configuration domain, and why not when it
 *  does not. `reason_code` is a code because backend strings have no catalog: the
 *  wording is this app's. A domain with `editable: false` is rendered read-only
 *  WITH its reason rather than given a control the write path would refuse. */
export interface ConfigDomainEditor {
  domain: string
  /** Dotted path the domain lives at, so the panel can point at it. */
  path: string
  editable: boolean
  /** Fields the editor offers, when it deliberately offers only some. */
  fields: string[]
  reason_code: string
}

export interface EngineConfigResponse {
  scope: { project: string | null; source: string | null }
  settings: Record<string, EffectiveSetting>
  /** Container sections as stored. These are not registry settings, so what is
   *  stored IS what applies and there is no origin to report for them. */
  domains: Record<string, Record<string, unknown>>
  /** Every domain this surface knows, so an absent one reads as "none
   *  configured" rather than as a domain that does not exist. */
  domain_sections: string[]
  /** What this surface will and will not write, decided by the backend. A panel
   *  deciding for itself would drift from what the write path accepts. */
  domain_editors?: ConfigDomainEditor[]
  /** Paths the engine fences to an operator-confirmed surface. */
  config_only_paths?: string[]
  /** The ENGINE's own vocabularies. A picker built from a hardcoded copy would
   *  offer a level or a role the validator refuses the day either list grows. */
  catalogs?: {
    autonomy_levels: string[]
    submitter_classes: string[]
    spec_types: string[]
    roles: string[]
    wildcard: string
  }
}

/** One delivery stage and the layer whose commands it runs, as the ENGINE
 *  derived it (`preset_display.stage_origins`). Never derived here: a
 *  byte-identical override is still an override, so comparing a stage's commands
 *  against the preset's would report it as inherited on exactly the stage an
 *  operator is inspecting. */
export interface StageOriginRow {
  stage: string
  /** bundled_preset | user_preset | app_override | project_override | unconfigured */
  source: string
  from_preset: boolean
  bundled: boolean
  preset: string
  declared_at: string
  commands: number
  /** True when nothing defines the stage, so it SKIPS at execution. Not the same
   *  as a stage that runs the preset's commands. */
  skipped: boolean
  /** The engine's own one-line description of the row. */
  summary: string
}

export interface WorkflowOriginsResponse {
  scope: { project: string | null }
  /** The preset in force, or null when none is selected. */
  preset: { name: string; origin: string; declared_at: string; bundled: boolean } | null
  stages: StageOriginRow[]
}

/** One run's spend as the ENGINE attributes it. `credits` is the number the
 *  ceiling compares -- never a browser-side sum over fetched rows, which would
 *  silently disagree with the limit the engine enforces. */
export interface RunSpendResponse {
  run_id: string
  project: string | null
  spec: string
  state: string
  source: string | null
  credits: number
  metered_credits: number
  /** Spend an external capability provider declared OUTSIDE any host session.
   *  Reported separately because it is the half a sum over turn rows misses. */
  declared_credits: number
  turns: number
  sessions: number
  /** The run row's own stored figure, for showing the two agree. */
  recorded_credits: number
  ceiling: { value: number | string | boolean; origin: string; declared_at: string }
}

export interface KillSwitchView {
  engaged: boolean
  initiator: string
  reason: string
  engaged_ts: string
  /** The flag is in force because its record could not be read, not because an
   *  operator threw it. Releasing that is a repair, not a decision. */
  unreadable: boolean
  description: string
}

export interface KillSwitchResponse {
  switch: KillSwitchView
  stoppable: { run_id: string; spec_key: string; source: string | null; state: string; cost_credits: number }[]
  stoppable_credits: number
}

export interface KillSwitchActionResponse {
  ok?: boolean
  action: string
  already_engaged?: boolean
  changed?: boolean
  switch: KillSwitchView
  halted?: { run_id: string; parked: boolean; cost_credits: number }[]
  total_credits?: number
  /** Always empty on a release: releasing lets new work start and resumes
   *  nothing that was parked. */
  resumed?: string[]
}

export interface QueueRow {
  run_id: string
  project: string
  spec: string
  state: string
  waiting_on: string
  waiting_s: number
  /** Credits this run consumed, the same number the ceiling accounts against. */
  cost_credits: number
  gate: string | null
  /** The watch source and item this run came from, when it came from one. Both
   *  are needed to name a re-dispatch, and both are absent for a run a person
   *  started. */
  source?: string | null
  item_id?: string | null
  entered_ts?: string
  /** The run used up its revision cycles at the gate it waits on, so no further
   *  revision turn will be dispatched and a person has to act. */
  revision_exhausted?: boolean
  /** How MANY reviewer comments are held for release — not which ones. The ids
   *  and the comment text live behind the watcher, deliberately: a queue row must
   *  not become a second place someone else's comment is copied to. So a release
   *  needs an id from the audit trail, and this surface cannot supply it. */
  feedback_quarantined?: number
  /** A review-feedback bound parked this run. Separate from
   *  `revision_exhausted`: they bound different loops, and acting on one is not
   *  acting on the other. */
  feedback_needs_human?: boolean
  /** The run's stored analysis findings, grouped by the criterion they concern,
   *  in the engine's order (keyed criteria first, then the unkeyed group). An
   *  EMPTY array is meaningful: no analysis was recorded for the run, which is
   *  not the same as an analysis that found nothing — that records a group with
   *  no findings. Every string here has already been through the engine's display
   *  contract, so a surface renders it as text and never re-escapes it. */
  analysis?: CriterionFindings[]
}

/** One finding as the engine stored it, already display-safe. */
export interface AnalysisFinding {
  kind: string
  severity: string
  /** Prose. It may contain newlines the engine deliberately preserved, so a
   *  surface that lays them out must BOUND them to this finding's own block —
   *  otherwise a crafted message reflows the rows around it. */
  message: string
  refs?: string[]
}

/** The findings concerning one acceptance criterion. */
export interface CriterionFindings {
  /** `null` for findings whose references matched no declared criterion. They
   *  are grouped rather than dropped: a reviewer still needs to read them. */
  criterion: string | null
  keyed: boolean
  findings: AnalysisFinding[]
}

export interface EngineQueueResponse {
  entries: QueueRow[]
  /** The same runs, grouped by run state as the ENGINE groups them
   *  (`QueueSnapshot.grouped`), in its order. A state with nothing waiting is
   *  absent rather than empty. Nothing here re-groups `entries`: two groupings of
   *  one run drift, and an operator cannot tell which is current. */
  grouped?: Record<string, QueueRow[]>
  total?: number
  total_credits: number
}

/** What one queue action did. Each flag is the ENGINE's answer to "did anything
 *  actually change", so a click on a stale row reads as "nothing to do" rather
 *  than as a change that did not happen. */
/** One workspace row a teardown either removed or deliberately kept. */
export interface WorkspaceCleanupRow {
  workspace_id: number
  run_id: string
  kind: string
  location: string
  removed: boolean
  reason?: string
}

/** What a teardown did, per workspace. `kept` carries the ids a retry needs. */
export interface TeardownReportBody {
  run_id?: string
  forced?: boolean
  removed?: WorkspaceCleanupRow[]
  kept?: WorkspaceCleanupRow[]
  stage?: string | null
  stage_reason?: string | null
}

export interface QueueActionResponse {
  ok?: boolean
  released?: boolean
  lifted?: boolean
  removed?: boolean
  complete?: boolean
  report?: TeardownReportBody
}

export const engineApi = {
  getConfig: (scope?: { project?: string; source?: string }) => {
    const q = new URLSearchParams()
    if (scope?.project) q.set('project', scope.project)
    if (scope?.source) q.set('source', scope.source)
    const suffix = q.toString() ? '?' + q.toString() : ''
    return req<EngineConfigResponse>('/engine/config' + suffix)
  },
  putConfig: (patch: Record<string, unknown>) =>
    req<{ ok?: boolean }>('/engine/config', { method: 'PUT', body: JSON.stringify({ patch }) }),
  /** Per-stage command origin, as the ENGINE derived it. */
  getWorkflowOrigins: (project?: string) =>
    req<WorkflowOriginsResponse>(
      '/engine/workflow-origins' + (project ? '?project=' + enc(project) : ''),
    ),
  /** One run's attributed spend, for a detail view. */
  getRunSpend: (runId: string) => req<RunSpendResponse>('/engine/run-spend?run_id=' + enc(runId)),
  getKillSwitch: () => req<KillSwitchResponse>('/engine/kill-switch'),
  setKillSwitch: (action: 'engage' | 'release', reason?: string) =>
    req<KillSwitchActionResponse>('/engine/kill-switch', {
      method: 'POST',
      body: JSON.stringify({ action, reason: reason || '' }),
    }),
  getQueue: (project?: string) =>
    req<EngineQueueResponse>('/engine/queue' + (project ? '?project=' + enc(project) : '')),
  // The queue row's actions. None of them names an actor: every one is a
  // privileged override attributed to the authenticated session on the server,
  // for the same reason an approval is.
  releaseFeedback: (row: { project: string; spec: string; run_id: string }, commentId: string) =>
    req<QueueActionResponse>('/engine/queue/release-feedback', {
      method: 'POST',
      body: JSON.stringify({
        project: row.project,
        spec: row.spec,
        run_id: row.run_id,
        comment_id: commentId,
      }),
    }),
  redispatchItem: (source: string, itemId: string, generation: number) =>
    req<QueueActionResponse>('/engine/queue/redispatch', {
      method: 'POST',
      body: JSON.stringify({ source, item_id: itemId, generation }),
    }),
  cleanWorkspace: (workspaceId: number) =>
    req<QueueActionResponse>('/engine/queue/clean-workspace', {
      method: 'POST',
      body: JSON.stringify({ workspace_id: workspaceId }),
    }),
  teardownRunWorkspaces: (runId: string) =>
    req<QueueActionResponse>('/engine/queue/teardown', {
      method: 'POST',
      body: JSON.stringify({ run_id: runId }),
    }),
}

/**
 * Catalog key per run state the queue groups by. Copy only, and the same shape
 * as ORIGIN_LABEL_KEY for the same reason: a literal Record is what
 * ``scripts/check-i18n-keys.mjs`` can resolve statically, and a state the engine
 * reports that this table does not know renders as its own id rather than as a
 * missing group. The engine decides which states hold a person's work; this
 * table only spells them.
 */
const QUEUE_STATE_LABEL_KEY: Record<string, string> = {
  awaiting_review: 'apps.specBuilder.reviewQueue.state_awaiting_review',
  halted_budget: 'apps.specBuilder.reviewQueue.state_halted_budget',
  stalled: 'apps.specBuilder.reviewQueue.state_stalled',
}

/** Localised heading for a queue group, or the run state id VERBATIM when this
 *  table has no phrase for it. */
export function queueStateLabel(state: string): string {
  return Object.prototype.hasOwnProperty.call(QUEUE_STATE_LABEL_KEY, state)
    ? i18nT(QUEUE_STATE_LABEL_KEY[state])
    : state
}

/**
 * Catalog key per value origin. A literal Record is the only shape
 * ``scripts/check-i18n-keys.mjs`` resolves statically, and the labels are the
 * whole point of the origin: "4" tells an operator nothing that "4 (shipped
 * default)" and "4 (this project)" do not.
 */
const ORIGIN_LABEL_KEY: Record<string, string> = {
  bundled_default: 'apps.specBuilder.engineOps.origin_bundled_default',
  app_config: 'apps.specBuilder.engineOps.origin_app_config',
  cost_profile: 'apps.specBuilder.engineOps.origin_cost_profile',
  project_config: 'apps.specBuilder.engineOps.origin_project_config',
  source_config: 'apps.specBuilder.engineOps.origin_source_config',
}

/**
 * Localised label for a value's origin, or the origin id VERBATIM when the
 * engine reports one this table does not know — better than fabricating copy
 * that claims the wrong layer.
 */
export function originLabel(origin: string): string {
  return Object.prototype.hasOwnProperty.call(ORIGIN_LABEL_KEY, origin)
    ? i18nT(ORIGIN_LABEL_KEY[origin])
    : origin
}
