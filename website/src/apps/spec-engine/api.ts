/**
 * The Operator_Surface's client for `/api/apps/spec-engine/*`.
 *
 * Written against the handlers in
 * `src/kiro_crew/apps/builtins/spec_engine/backend/routes.py`, one function per
 * registered route, with each payload type transcribed from the object that
 * handler actually returns. Not from REST convention: three of these routes take
 * their arguments somewhere a convention would not put them — the queue and
 * spend reads take a query string, the kill switch takes its verb in the body
 * rather than in the method, and every queue action is a POST to a sub-path with
 * an identifier in the body rather than in the URL.
 *
 * The routes are registered on the GATEWAY's own aiohttp application (the
 * builtin loop in `dashboard/server.py` walks `BUILTIN_NAMES` and calls this
 * app's `register_routes`), so these are plain same-origin fetches carrying the
 * dashboard session — NOT the app-sdk hooks, which need `<AppApiProvider>` and
 * only wrap standalone apps, and NOT the `/apps/<name>/api` reverse-proxy prefix
 * used by an app that runs as its own process.
 *
 * **Every refusal carries a machine-readable `code`.** Backend-owned strings have
 * no localization catalog, so the code is what a caller branches on and the text
 * is a log line or a last-resort fallback. :class:`SpecEngineApiError` keeps both,
 * plus the status, because the three answer different questions: the code says
 * what happened, the status says who can fix it, and the text is for a human
 * reading a report. Callers MUST branch on `code`, never on the message.
 *
 * **What this module does not have a client for.** The setup assistant. Its three
 * operations (`inspect_setup`, `plan_setup`, `apply_setup`) are MCP tools with no
 * HTTP route, and the config surface here serves the PERSISTED document only —
 * there is no route vending an effective/resolved view. Both gaps are real, not
 * oversights: a function here for a route that does not exist would fail at the
 * first call, and inventing one would hide the gap from whoever has to close it.
 */
import { i18nT } from '../../i18n/t'

/** The app's URL namespace. One constant so a route string cannot drift from it. */
const API = '/api/apps/spec-engine'

/**
 * A refusal from this surface, with the `code` a caller branches on.
 *
 * `code` is `''` only when the failure happened below the application — a dropped
 * connection, a body that is not JSON at all — so an empty code means "no handler
 * answered", which is a different thing from every named refusal below.
 */
export class SpecEngineApiError extends Error {
  readonly code: string
  readonly status: number

  constructor(message: string, code: string, status: number) {
    super(message)
    this.name = 'SpecEngineApiError'
    this.code = code
    this.status = status
  }
}

/**
 * The refusal codes these handlers emit, as a lookup rather than a union of
 * string literals used inline.
 *
 * Kept because the page's behaviour differs per code in ways a status cannot
 * express: `app_disabled` and `unauthorized` are both a 403/401 the operator
 * cannot act on from this page, `config_unreadable` means a document exists and
 * is broken — which is emphatically NOT "nothing is configured yet", and sending
 * that operator to the setup assistant would point them at a flow that then
 * refuses to overwrite a file it cannot parse.
 */
export const REFUSAL = {
  appDisabled: 'app_disabled',
  unauthorized: 'unauthorized',
  dashboardUserRequired: 'dashboard_user_required',
  configUnreadable: 'config_unreadable',
  configInvalid: 'config_invalid',
  configWriteRefused: 'config_write_refused',
  configWriteUnrecorded: 'config_write_unrecorded',
  configWriteFailed: 'config_write_failed',
  queueUnreadable: 'queue_unreadable',
  killSwitchUnreadable: 'kill_switch_unreadable',
  spendUnreadable: 'spend_unreadable',
  runUnknown: 'run_unknown',
  fieldRequired: 'field_required',
} as const

// ── payload shapes, transcribed from the handlers ──────────────────────────

/**
 * What the person a queued run waits for. The engine's `WaitingOn`, whose three
 * members are three different jobs: a verdict, a spending decision, and a
 * judgement call about a run that stopped reporting.
 */
export type WaitingOn = 'review' | 'budget' | 'stall'

/** One row of the Review_Queue, from `QueueEntry.to_json_object`. */
export interface QueueEntry {
  run_id: string
  project: string
  spec: string
  spec_type: string | null
  state: string
  waiting_on: WaitingOn
  entered_ts: string
  /** How long it has waited, by the ENGINE's clock — not a browser subtraction. */
  waiting_s: number
  source: string | null
  item_id: string | null
  cost_credits: number
  gate: string | null
  /**
   * The run has spent its revision cycles at the gate it waits on. It stays in
   * `awaiting_review`, so the state alone does not say it: no further revision
   * turn will be dispatched, which makes "request changes" a control that would
   * change nothing.
   */
  revision_exhausted: boolean
  /** How many reviewer comments are held for a person to release. A count, never the text. */
  feedback_quarantined: number
  /** A review-feedback bound parked this run. Distinct from `revision_exhausted`: different loop. */
  feedback_needs_human: boolean
  analysis: Array<{ criterion: string | null; keyed: boolean; findings: Array<Record<string, unknown>> }>
}

/**
 * The queue as it stood when it was taken, from `QueueSnapshot.to_json_object`
 * plus the handler's own `total_credits`.
 *
 * `grouped` is the ENGINE's grouping, keyed by run state and omitting a state
 * with nothing in it. A surface that regrouped `entries` itself would be a second
 * grouping of one queue, and a reader could not tell which was current.
 */
export interface QueueSnapshot {
  entries: QueueEntry[]
  grouped: Record<string, QueueEntry[]>
  total: number
  total_credits: number
}

/** The persisted kill-switch flag, from `KillSwitchState.to_json_object`. */
export interface KillSwitchState {
  engaged: boolean
  initiator: string
  reason: string
  engaged_ts: string
  /**
   * The flag exists but could not be read or parsed. The switch reads engaged in
   * that case — the fail-closed direction — and this says the reason was doubt
   * rather than an operator, which is what makes releasing it a repair instead of
   * a decision.
   */
  unreadable: boolean
  description: string
}

/** A run a stop would park, with the credits it has already consumed. */
export interface StoppableRun {
  run_id: string
  spec_key: string
  source: string
  state: string
  cost_credits: number
}

export interface KillSwitchSnapshot {
  switch: KillSwitchState
  stoppable: StoppableRun[]
  stoppable_credits: number
}

/** One configuration advisory, from `_advisory`. */
export interface ConfigAdvisory {
  code: string
  path: string
  message: string
  project: string | null
  /** An advisory a human must acknowledge is a different obligation from one they only read. */
  requires_acknowledgment: boolean
}

/**
 * The persisted configuration as an operator may see it, from `_config_snapshot`.
 *
 * `configured` is the field first-run detection reads, and it is deliberately not
 * derivable from `document`: an absent file and an empty one both serialize to
 * `{}`, and only one of them means "offer the setup assistant".
 */
export interface ConfigSnapshot {
  configured: boolean
  path: string
  document: Record<string, unknown>
  /** Dotted paths whose value was withheld, so an elision is never read as a literal. */
  elided: string[]
  errors: Array<{ path: string; message: string }>
  advisories: ConfigAdvisory[]
  config_only_paths: string[]
}

/** One run's attributed spend and the ceiling in force for it, from `_run_spend`. */
export interface RunSpend {
  run_id: string
  project: string | null
  spec: string
  state: string
  source: string
  /** The engine's own total, which is the figure the ceiling compares. */
  credits: number
  metered_credits: number
  /** Spend outside any host session — inside `credits`, not beside it. */
  declared_credits: number
  turns: number
  sessions: number
  recorded_credits: number
  ceiling: { value: number; origin: string; declared_at: string }
}

// ── transport ─────────────────────────────────────────────────────────────

/**
 * One same-origin request, raising :class:`SpecEngineApiError` on any refusal.
 *
 * The body is read as TEXT and parsed here rather than through `response.json()`,
 * because a refusal from a layer above the handler (a gateway 502, an auth
 * middleware redirect) is not JSON, and `response.json()` would throw a
 * `SyntaxError` that names nothing an operator can act on. Read as text, the
 * status and the first of the body still reach the caller.
 */
async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response
  try {
    response = await fetch(path, { credentials: 'same-origin', ...init })
  } catch (cause) {
    // A network failure, not a refusal: no handler answered, so there is no code.
    throw new SpecEngineApiError(
      cause instanceof Error ? cause.message : String(cause),
      '',
      0,
    )
  }
  const text = await response.text().catch(() => '')
  let parsed: unknown = undefined
  if (text.trim() !== '') {
    try {
      parsed = JSON.parse(text)
    } catch {
      parsed = undefined
    }
  }
  if (!response.ok) {
    const body = (parsed ?? {}) as { code?: unknown; error?: unknown }
    const code = typeof body.code === 'string' ? body.code : ''
    const message =
      typeof body.error === 'string' && body.error !== ''
        ? body.error
        : i18nT('apps.specEngine.api.the_request_was_refused')
    throw new SpecEngineApiError(message, code, response.status)
  }
  return parsed as T
}

const postJson = <T>(path: string, body: unknown): Promise<T> =>
  request<T>(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })

// ── the ten routes ────────────────────────────────────────────────────────

export const specEngineApi = {
  /**
   * GET the Review_Queue, optionally narrowed to one project.
   *
   * `project` is the engine's stored posix path for the project, so it is passed
   * through verbatim; re-resolving it here against a browser's idea of a path is
   * not possible and would be wrong if it were.
   */
  queue: (project?: string): Promise<QueueSnapshot> => {
    const query = project ? `?project=${encodeURIComponent(project)}` : ''
    return request<QueueSnapshot>(`${API}/queue${query}`)
  },

  /**
   * Release one held reviewer comment.
   *
   * The comment IDENTIFIER is all that crosses the boundary — the text is an
   * outside submitter's data and this client must not become a second place it is
   * copied to. All four fields are required; the handler refuses with
   * `field_required` and names the ones missing.
   */
  releaseFeedback: (args: {
    project: string
    spec: string
    run_id: string
    comment_id: string
  }): Promise<{ ok: boolean; released: boolean }> =>
    postJson(`${API}/queue/release-feedback`, args),

  /**
   * Lift the suppression on one watched item so the next poll dispatches it.
   *
   * `generation` is required and is NOT optional-with-a-default: the handler
   * refuses when it is absent, because lifting an unnamed generation would lift
   * whichever one the poller happened to be on.
   */
  redispatch: (args: {
    source: string
    item_id: string
    generation: number
  }): Promise<{ ok: boolean; lifted: boolean }> => postJson(`${API}/queue/redispatch`, args),

  /**
   * Remove one ledger-recorded workspace: the retry for a kept teardown.
   *
   * `removed` is false when no ACTIVE row has that id, so a second click reads as
   * "nothing to do" rather than as a removal that failed.
   */
  cleanWorkspace: (args: {
    workspace_id: number
    force?: boolean
  }): Promise<{ ok: boolean; removed: boolean; cleanup: Record<string, unknown> | null }> =>
    postJson(`${API}/queue/clean-workspace`, args),

  /**
   * Tear down every workspace a run recorded.
   *
   * `complete` is the field that matters and `ok` is not it: a teardown that kept
   * anything answers `ok: true, complete: false` with the kept ids in `kept`, and
   * a caller reading only `ok` would report a standing workspace as torn down.
   */
  teardown: (args: {
    run_id: string
  }): Promise<{
    ok: boolean
    complete: boolean
    kept: number[]
    report: Record<string, unknown>
  }> => postJson(`${API}/queue/teardown`, args),

  /** GET the persisted configuration, credential values elided. */
  config: (): Promise<ConfigSnapshot> => request<ConfigSnapshot>(`${API}/config`),

  /**
   * Persist a configuration patch through the engine's single write path.
   *
   * Sent as `{patch}` rather than as the bare body. The handler accepts either
   * (`body.get("patch", body)`), and the wrapper is the unambiguous half: a
   * document whose own top level held a `patch` key would otherwise be read as
   * the wrapper and silently unwrapped.
   */
  writeConfig: (
    patch: Record<string, unknown>,
  ): Promise<{ ok: boolean; document: Record<string, unknown>; advisories: ConfigAdvisory[] }> =>
    request(`${API}/config`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ patch }),
    }),

  /** GET the kill switch's state and the runs a stop would park. */
  killSwitch: (): Promise<KillSwitchSnapshot> => request<KillSwitchSnapshot>(`${API}/kill-switch`),

  /**
   * Engage or release the kill switch.
   *
   * The initiator is NOT a parameter: the handler attributes both directions to
   * the authenticated session, because a stop recorded against a name the caller
   * typed records nothing.
   *
   * Releasing lets new work START and resumes nothing — `resumed` comes back
   * empty by design, and a caller must not present a release as a restart.
   */
  setKillSwitch: (args: {
    action: 'engage' | 'release'
    reason?: string
  }): Promise<{
    ok: boolean
    action: 'engage' | 'release'
    switch: KillSwitchState
    changed?: boolean
    resumed?: string[]
    already_engaged?: boolean
    halted?: Array<{ run_id: string; parked: boolean; cost_credits: number }>
    total_credits?: number
    description?: string
  }> => postJson(`${API}/kill-switch`, args),

  /** GET one run's attributed spend, with the ceiling it is judged against. */
  runSpend: (runId: string): Promise<RunSpend> =>
    request<RunSpend>(`${API}/run-spend?run_id=${encodeURIComponent(runId)}`),
}

/** React Query keys, shared so two panels reading one route share one cache entry. */
export const QK = {
  queue: ['spec-engine', 'queue'] as const,
  config: ['spec-engine', 'config'] as const,
  killSwitch: ['spec-engine', 'kill-switch'] as const,
  runSpend: (runId: string) => ['spec-engine', 'run-spend', runId] as const,
}
