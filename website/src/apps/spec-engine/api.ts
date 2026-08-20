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
 * The setup routes add one level to that: every refusal of the flow arrives as
 * `setup_refused` with the engine's own refusal code in `refused`, which is the
 * SAME vocabulary the Engine_MCP_Server's setup tools return. One status for all
 * four (`approver-required`, `plan-stale`, `setup-approval-required`,
 * `inferred-subject-refused`) because they differ in what the operator must do
 * next and not in who may act: every one means the flow did not proceed and
 * nothing was written.
 *
 * **The configuration is read twice, and only one of the two is a write path.**
 * `config` is the persisted document, which `writeConfig` sends patches to.
 * `resolvedConfig` is the value in force for every setting with the origin of
 * each, resolved through the same store — a read of the document beside it, never
 * a second place to write. A surface showing only the document cannot answer
 * "what is actually in force here", which is the question every edit is about.
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
  /**
   * The engine's own refusal code, for a refusal that carries one.
   *
   * Only the setup routes do: their `code` is always `setup_refused` and the
   * actionable part is here. `''` everywhere else, so a caller reading this
   * cannot mistake "not a setup refusal" for a refusal it does not know.
   */
  readonly refused: string

  constructor(message: string, code: string, status: number, refused = '') {
    super(message)
    this.name = 'SpecEngineApiError'
    this.code = code
    this.status = status
    this.refused = refused
  }
}

/**
 * The refusal codes a caller branches on, as a lookup rather than a union of
 * string literals used inline. NOT the full set the handlers emit: the remaining
 * queue-action failure codes (`release_failed`, `redispatch_failed`,
 * `cleanup_failed`, `teardown_failed`), the kill-switch `engage_failed`, the
 * setup read/apply failures, and the malformed-request family (`bad_json`,
 * `bad_patch`, `bad_action`, `bad_reason`) have no branch yet — each is reported
 * through the refusal block by code and text without the caller deciding anything
 * on the code, and each should be added HERE when its branch is written, not
 * spelled inline.
 *
 * Four have real branches today, and each differs from its neighbours in a way a
 * status cannot express:
 *
 * - `configUnreadable` means a document exists and is broken, which is emphatically
 *   NOT "nothing is configured yet": sending that operator to the setup assistant
 *   points them at a flow that then refuses to overwrite a file it cannot parse.
 * - `releaseRefused` is a 409 the engine decided (this run's machine records the
 *   release nowhere), while `release_failed` is a 503 a retry may clear. Telling an
 *   operator to retry the first sends them back forever.
 * - `runUnknown` means the run id itself is not one the engine has, so the spend
 *   pane says so instead of offering the refusal block: a read that failed invites
 *   a retry, and there is nothing here to retry for.
 * - `setupRefused` is every refusal of the setup flow, and the panel reads
 *   {@link SETUP_REFUSAL} out of `refused` to decide which control to point at.
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
  releaseRefused: 'release_refused',
  /** Every refusal of the setup flow. Which one it was is in `refused`. */
  setupRefused: 'setup_refused',
  badAnswers: 'bad_answers',
  badProject: 'bad_project',
} as const

/**
 * The engine's refusal codes for the setup flow, as they arrive in `refused`.
 *
 * These are the four decisions the flow can report, and the panel branches on
 * three of them: an absent approver is a field the operator has not filled, a
 * stale plan means the plan on screen no longer describes what would be written
 * (so it must be recomputed rather than retried), and an approval gate names a
 * question that is still unanswered. `inferredSubject` is the engine refusing to
 * have inferred a subject it only ever asks about; it cannot arise from this
 * panel's own calls and is spelled here so a caller that meets it can name it.
 */
export const SETUP_REFUSAL = {
  approverRequired: 'approver-required',
  planStale: 'plan-stale',
  approvalRequired: 'setup-approval-required',
  inferredSubject: 'inferred-subject-refused',
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
 * `configured` states only that the document FILE exists. First-run detection
 * deliberately does NOT read it: a file can exist while configuring no project,
 * and the surface's first-run question is "is there a project entry", answered
 * from `document.projects`. The flag stays in the payload because "no file" and
 * "a file with nothing in it" are different facts about the store even when the
 * surface treats both as unconfigured.
 */
export interface ConfigSnapshot {
  configured: boolean
  path: string
  document: Record<string, unknown>
  /** Dotted paths whose value was withheld, so an elision is never read as a literal. */
  elided: string[]
  /**
   * The value substituted for each withheld one, as the store spells it.
   *
   * Relayed rather than hardcoded here: an editor must recognise it to keep it out
   * of a patch, and a copy of the string on this side is a second spelling of one
   * constant. If the two drift, a save silently replaces a live credential with the
   * marker and the document stays valid.
   */
  elided_marker: string
  errors: Array<{ path: string; message: string }>
  advisories: ConfigAdvisory[]
  config_only_paths: string[]
}

/**
 * One setting's value in force, from `EffectiveValue.to_json_object`.
 *
 * `origin` and `declared_at` are the reason this read exists. A surface showing
 * `2` cannot tell an operator whether somebody chose 2 or whether the app ships 2,
 * and those call for opposite actions — so the origin travels and no caller infers
 * "looks like the default, must be the default", which is wrong exactly when
 * someone has pinned a value that happens to equal it.
 */
export interface EffectiveSetting {
  key: string
  value: unknown
  origin: 'bundled_default' | 'app_config' | 'cost_profile' | 'project_config' | 'source_config'
  /** Dotted path of the explicit declaration, `''` for a bundled default. */
  declared_at: string
  is_default: boolean
  default?: unknown
  summary?: string
  kind?: string
  scopes?: string[]
  minimum?: number | null
  maximum?: number | null
  choices?: unknown[]
}

/**
 * One role's resolved routing, from `ResolvedRole.detail()`.
 *
 * The engine's own resolution, relayed. Every optional field is optional in the
 * payload too — `detail()` omits what it has nothing to say about — so a reader
 * must not treat an absent `declared_at` as an empty string that means something.
 *
 * `profile` and `role` are what a per-role reset addresses, and they are read from
 * HERE rather than by splitting `declared_at`: a profile may legitimately be named
 * `thrifty.roles`, and splitting the dotted path would then name a node that does
 * not exist.
 */
export interface ResolvedRole {
  role: string
  source: 'cost_profile' | 'session_default'
  agent: string
  model: string
  effort: string
  profile?: string
  declared_at?: string
  /** Why this role fell back: four conditions, fixed in four different places. */
  fallback?:
    | 'no_cost_profile_selected'
    | 'selected_cost_profile_not_defined'
    | 'role_unassigned'
    | 'role_model_unassigned'
  report?: string
  /** An effort the profile pinned that the resolved model cannot accept. */
  dropped_effort?: string
}

/** The role plan for one project, from `RolePlan.detail()`. */
export interface RolePlanDetail {
  profile: string
  roles: Record<string, ResolvedRole>
  project?: string
  /** The name a project selected when that profile is not defined. */
  requested_profile?: string
}

/**
 * The resolved read, from `_resolved_snapshot`.
 *
 * `role_order` travels because a JSON object has no order a client may rely on and
 * the engine's role order is meaningful — it is the order the profiles declare and
 * the audit records.
 */
export interface ResolvedConfig {
  configured: boolean
  project: string | null
  source: string | null
  settings: EffectiveSetting[]
  roles: RolePlanDetail
  role_order: string[]
}

/** One piece of file text an inference was drawn from, from `Evidence.render`. */
export interface SetupEvidence {
  subject?: string
  located_at: string
  /** Outside-authored text, already through the engine's display contract. */
  excerpt: string
}

/** Something read out of the project, from `Inference.render`. */
export interface SetupInference {
  subject: string
  value: string
  rationale: string
  evidence: SetupEvidence[]
}

/**
 * Something the assistant asks rather than infers, from `render_question`.
 *
 * `answer_kind` is stated rather than inferred from an empty `options`: a caller
 * that guesses wrong asks a human to pick from nothing.
 */
export interface SetupQuestion {
  subject: string
  prompt: string
  because: string
  options: string[]
  answer_kind: 'choice' | 'confirmation'
}

/** A prerequisite report, from `render_prerequisites`. */
export interface SetupPrerequisites {
  met: boolean
  checks: Array<Record<string, unknown>>
  unmet: Array<Record<string, unknown>>
}

/**
 * A bundled preset the project's evidence makes applicable, from `render_offer`.
 *
 * `programs` and `commands` are read out of the same bundled tables the write
 * copies from, so what an operator approves is what would land in configuration.
 */
export interface SetupOffer {
  kind: string
  name: string
  inference: SetupInference
  programs: string[]
  commands: Array<{ stage: string; argv: string[] }>
  prerequisites: SetupPrerequisites
  definition?: Record<string, unknown>
  copy_note?: string
}

/** What the inspection returns, from `inspection_payload`. */
export interface SetupInspection {
  project: { name: string; root: string }
  /** Whether memory was available. False is a smaller plan, never a silent one. */
  memory_consulted: boolean
  evidence: SetupEvidence[]
  inferences: SetupInference[]
  questions: SetupQuestion[]
  offers: SetupOffer[]
  prerequisites: SetupPrerequisites
  /** Subjects that are asked and never inferred, so a retry is never the answer. */
  asked_subjects: string[]
  confirmed_levels: string[]
  autonomy_field: string
}

/** The operator's answers, as both the plan and the apply take them. */
export interface SetupAnswers {
  cost_profile: string
  /** One true/false per rung. A MISSING rung is unanswered, not "no". */
  confirmations: Record<string, boolean>
  approved_subjects: string[]
  workflow_preset: string | null
  watch_source: string | null
}

/**
 * A computed plan and its identity, from `SetupPlanEnvelope.to_json_object`.
 *
 * `config_patch` is the patch itself and not a summary: an approval given against
 * a summary is an approval of something else. `plan_id` is a content hash of the
 * subject, the answers used and that patch — the apply recomputes it and refuses
 * on a mismatch, so a plan whose inputs have moved is never applied unread.
 */
export interface SetupPlanEnvelope {
  plan_id: string
  project: { name: string; root: string }
  inferences: SetupInference[]
  answers_used: Record<string, unknown>
  config_patch: Record<string, unknown>
  written_paths: string[]
  warnings: string[]
}

/** What an apply returns, from `apply_payload`. */
export interface SetupApplied {
  applied: boolean
  plan_id: string
  approver: string
  project: { name: string; root: string }
  written_paths: string[]
  config_patch: Record<string, unknown>
  prerequisites: SetupPrerequisites
  notes: string[]
  advisories: ConfigAdvisory[]
}

/**
 * What became of one ledger row, from `WorkspaceCleanup.to_json_object`.
 *
 * `removed` here is the removal verdict — distinct from the clean-workspace
 * response's TOP-LEVEL `removed`, which only says an active row existed.
 * `reason` is always populated: why a row was left alone, or how it was removed.
 */
export interface WorkspaceCleanup {
  workspace_id: number
  run_id: string
  kind: string
  location: string
  address: string | null
  removed: boolean
  reason: string
}

/** A teardown's full accounting, from `TeardownReport.to_json_object`. */
export interface TeardownReport {
  run_id: string
  forced: boolean
  removed: WorkspaceCleanup[]
  kept: WorkspaceCleanup[]
  stage: string | null
  stage_reason: string
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
    const body = (parsed ?? {}) as { code?: unknown; error?: unknown; refused?: unknown }
    const code = typeof body.code === 'string' ? body.code : ''
    const refused = typeof body.refused === 'string' ? body.refused : ''
    const message =
      typeof body.error === 'string' && body.error !== ''
        ? body.error
        : i18nT('apps.specEngine.api.the_request_was_refused')
    throw new SpecEngineApiError(message, code, response.status, refused)
  }
  return parsed as T
}

const postJson = <T>(path: string, body: unknown): Promise<T> =>
  request<T>(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })

// ── the fourteen routes ───────────────────────────────────────────────────

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
   * Two `removed` fields with DIFFERENT meanings, from the handler's own shape.
   * The top-level `removed` is `cleanup is not null` — an ACTIVE row with that
   * id existed, so a second click reads as "nothing to do". Whether the
   * workspace actually came down is `cleanup.removed`: the engine returns a
   * populated cleanup with `removed: false` when it DECLINES (a deployment row,
   * a failed `git worktree remove`, a tree outside the disposable root), and
   * `cleanup.reason` says why. A caller reading only the top-level field
   * reports a standing workspace as removed.
   */
  cleanWorkspace: (args: {
    workspace_id: number
    force?: boolean
  }): Promise<{ ok: boolean; removed: boolean; cleanup: WorkspaceCleanup | null }> =>
    postJson(`${API}/queue/clean-workspace`, args),

  /**
   * Tear down every workspace a run recorded.
   *
   * `complete` is the field that matters and `ok` is not it: a teardown that kept
   * anything answers `ok: true, complete: false` with the kept ids in `kept`, and
   * a caller reading only `ok` would report a standing workspace as torn down.
   * `report.kept` carries the same rows with their kind and the reason each was
   * kept; `complete: false` with an empty `kept` means the teardown STAGE failed
   * (`report.stage`, `report.stage_reason`), not that workspaces stand.
   */
  teardown: (args: {
    run_id: string
  }): Promise<{
    ok: boolean
    complete: boolean
    kept: number[]
    report: TeardownReport
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

  /**
   * GET the value in force for every setting, with the origin of each.
   *
   * A READ of the document `config` returns and `writeConfig` writes — never a
   * second write path. `project` and `source` narrow the resolution, because most
   * of the precedence only exists once a project is named: without one, a
   * project-scoped value and a profile a project selected are both invisible, and
   * the reply says so by resolving to the wider layers rather than by omitting
   * them.
   */
  resolvedConfig: (project?: string, source?: string): Promise<ResolvedConfig> => {
    const query = new URLSearchParams()
    if (project) query.set('project', project)
    if (source) query.set('source', source)
    const suffix = query.toString()
    return request<ResolvedConfig>(`${API}/config/resolved${suffix ? `?${suffix}` : ''}`)
  },

  /**
   * Inspect a project: the evidence read, the values inferred, the questions left.
   *
   * Writes nothing, and is a POST anyway: the project path is the CALLER's, so the
   * route is operator-guarded rather than a general-purpose read. `name` overrides
   * the configuration name, which otherwise falls back to the directory's — and it
   * is part of the plan identity, so passing it later changes the `plan_id`.
   */
  setupInspect: (args: { project: string; name?: string }): Promise<SetupInspection> =>
    postJson(`${API}/setup/inspect`, args),

  /**
   * Compute the plan a set of answers produces. Writes nothing.
   *
   * Every gate the apply would fail is evaluated here, so an operator learns that a
   * rung is unanswered or a preset was never offered BEFORE they put their name to
   * it. A refusal arrives as `setup_refused` with the specific code in `refused`.
   */
  setupPlan: (args: {
    project: string
    answers: SetupAnswers
    name?: string
  }): Promise<SetupPlanEnvelope> => postJson(`${API}/setup/plan`, args),

  /**
   * Apply a plan by its identity, on a named human approver's authority.
   *
   * `approver` is required and is NOT the session: an operator may apply a plan a
   * colleague approved, and the engine records the approver in its durable write
   * record while the session is recorded in the security event. An empty one is
   * refused with `approver-required` and writes nothing.
   *
   * `plan_id` must be the one `setupPlan` returned for these same answers. The
   * server recomputes the plan and refuses a mismatch with `plan-stale` — so a
   * caller that changed an answer after planning must plan again rather than
   * re-sending; the id is not a token to hold, it is a claim about the inputs.
   */
  setupApply: (args: {
    project: string
    answers: SetupAnswers
    plan_id: string
    approver: string
    name?: string
  }): Promise<SetupApplied> => postJson(`${API}/setup/apply`, args),
}

/** React Query keys, shared so two panels reading one route share one cache entry. */
export const QK = {
  queue: ['spec-engine', 'queue'] as const,
  config: ['spec-engine', 'config'] as const,
  killSwitch: ['spec-engine', 'kill-switch'] as const,
  runSpend: (runId: string) => ['spec-engine', 'run-spend', runId] as const,
  /**
   * The resolved read, keyed by the project it was resolved FOR.
   *
   * The project is part of the key rather than a filter applied after the fact: two
   * projects resolve two different documents' worth of precedence, and one cache
   * entry for both would show a value in force under a project it is not in force
   * for.
   */
  resolved: (project: string) => ['spec-engine', 'config', 'resolved', project] as const,
}

/** The prefix every resolved-read key shares, for invalidating them together. */
export const QK_RESOLVED_ROOT = ['spec-engine', 'config', 'resolved'] as const
