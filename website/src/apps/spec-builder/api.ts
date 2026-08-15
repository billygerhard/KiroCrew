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

export interface EngineConfigResponse {
  scope: { project: string | null; source: string | null }
  settings: Record<string, EffectiveSetting>
  /** Container sections as stored. These are not registry settings, so what is
   *  stored IS what applies and there is no origin to report for them. */
  domains: Record<string, Record<string, unknown>>
  /** Every domain this surface knows, so an absent one reads as "none
   *  configured" rather than as a domain that does not exist. */
  domain_sections: string[]
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
}

export interface EngineQueueResponse {
  entries: QueueRow[]
  total_credits: number
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
  getKillSwitch: () => req<KillSwitchResponse>('/engine/kill-switch'),
  setKillSwitch: (action: 'engage' | 'release', reason?: string) =>
    req<KillSwitchActionResponse>('/engine/kill-switch', {
      method: 'POST',
      body: JSON.stringify({ action, reason: reason || '' }),
    }),
  getQueue: (project?: string) =>
    req<EngineQueueResponse>('/engine/queue' + (project ? '?project=' + enc(project) : '')),
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
