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
 * One registry setting as the facts a generated form control is built from,
 * from `_setting_vocabulary`.
 *
 * `kind` is the type's NAME (`int`, `float`, `bool`, `str`) rather than an enum
 * member, and `scopes` are the scope value strings in broadest-first order —
 * the order a scope chooser reads, which is the reverse of the resolver's
 * precedence. Both travel as strings because the payload is JSON, and a client
 * that had to know Python's spelling of a type would be reading a language
 * detail rather than a vocabulary.
 *
 * `minimum` and `maximum` are `null` rather than absent when the setting has no
 * bound, so a numeric control branches on the value instead of on whether a key
 * came and went.
 *
 * `choices` is deliberately NOT here: the projection omits it because every
 * shipped setting's is empty, so a `str` setting is free text. A setting that
 * ever declares choices needs this field and a closed-vocabulary control added
 * together — until then neither exists, and nothing offers free text where the
 * write door would enforce a fixed set.
 */
export interface RegistrySetting {
  /** The dotted key: the leading segment is the group, the rest the leaf. */
  key: string
  kind: string
  default: unknown
  minimum: number | null
  maximum: number | null
  /** The scopes this setting may be overridden at, broadest first. */
  scopes: string[]
  summary: string
}

/**
 * One bundled Watch_Source preset, from the registry projection.
 *
 * `host` is the bundled table's own key (`github`, `gitlab`) and NOT a domain
 * name. `entry` is the deep copy `watch_source_presets` returns, which
 * deliberately carries no `enabled` key, so a fresh copy is inert until an
 * operator arms it.
 */
export interface SourcePreset {
  host: string
  /** The program the preset's commands run, derived from its own argv. */
  program: string
  entry: Record<string, unknown>
}

/**
 * One bundled cost-profile preset, from the registry projection.
 *
 * The ENTRY travels beside the name for the same reason a source preset's does:
 * adding a profile is adding a copy of one, and a surface holding only the name
 * would have to invent the role assignments it claims to copy — which is the
 * no-provenance profile the engine refuses to be useful with, since every role
 * then resolves to the session default while the project reports that a profile
 * is selected.
 */
export interface ProfilePreset {
  name: string
  /** The deep copy `cost_profile_presets` returns, ready to write. */
  entry: Record<string, unknown>
}

/**
 * One pipeline stage as `_registry_payload` projects it.
 *
 * `setting_groups` are `Setting.group` values — the leading dot-segment of a
 * registry key — in the setting registry's own declaration order, and
 * `capabilities` are delegable capability names in schema declaration order. The
 * placement itself is `engine/config/pipeline.py`'s, projected so a setting or
 * capability the engine adds is placed by the engine rather than by a table kept
 * on this side of the wire.
 *
 * These are PIPELINE stages — which part of the pipeline a knob governs — and not
 * the autonomy ladder, whose rungs share three of these names and answer how much
 * authority a run holds. Nothing derived from this may become an input to a gate,
 * prerequisite or budget decision.
 */
export interface StageVocabulary {
  id: string
  setting_groups: string[]
  capabilities: string[]
}

/**
 * The provider a capability resolves to, from `ProviderIdentity.to_json_object()`.
 *
 * `kind` and `nature` answer two different questions and only one of them is a
 * cost signal. `nature` is hardcoded `model_backed` for EVERY external binding,
 * not because the engine knows the program reasons but because it cannot know, so
 * it is a cost class for a BUILTIN only. A reader deciding whether a capability
 * spends credits must branch on `kind` first and read `nature` only for a builtin
 * — `capabilityForm.costSignal` is the one place that decision is made, and it is
 * written so `nature` is unreachable for anything else.
 *
 * `version` is omitted rather than sent empty when the provider declares none.
 */
export interface ProviderIdentity {
  name: string
  kind: 'builtin' | 'external'
  nature: 'deterministic' | 'model_backed'
  transport: string
  version?: string
}

/**
 * One delegable capability's binding, from `GET /config/capabilities`.
 *
 * The engine's own `CapabilityRegistry.describe()` entry joined with the
 * reachability answer a run's prerequisite gate reports against, so a binding
 * this surface calls reachable is one a run would accept.
 *
 * Three fields carry contracts a renderer cannot infer from their types:
 *
 * - **`reachable` is three-valued.** `null` means NOT APPLICABLE — the binding is
 *   on its builtin, which is reachable by construction, so the engine's check
 *   skips it. Rendering `null` as a broken provider would mark every unconfigured
 *   capability as failing.
 * - **`program` is `argv[0]` only,** and the environment never travels at all.
 *   The stored command and the environment NAMES are read from the document
 *   instead; an environment VALUE is never sent to this pane by either read.
 * - **`action` is the engine's own remediation string,** naming the "or unset it
 *   to use the builtin" escape. Relayed, never composed on this side.
 *
 * `timeout_s` is the RESOLVED deadline one call gets — the binding's own override
 * when it declares one, otherwise the app's `timeouts.capability_s` — so it is
 * not the value a form writes back.
 */
export interface CapabilityBinding {
  capability: string
  transport: string
  configured: boolean
  /** Dotted path of the declaration, `''` when nothing declares the binding. */
  declared_at: string
  timeout_s: number
  provider: ProviderIdentity
  program: string
  reachable: boolean | null
  action: string
}

/**
 * Every delegable capability's binding, from `GET /config/capabilities`.
 *
 * A REFUSED document does not arrive here at all: the route answers 422 with no
 * `capabilities` key, so the read fails and a caller states the failure. That
 * matters because an all-builtin list is exactly what an UNCONFIGURED document
 * legitimately resolves to — the two are the same shape and opposite facts, and a
 * surface that fell back to one would show a refused document as a clean one.
 *
 * `configured` is about the DOCUMENT existing on disk, not about any binding
 * being declared, so `configured: true` beside seven builtin rows is an ordinary
 * state rather than a contradiction.
 */
export interface CapabilitiesPayload {
  configured: boolean
  capabilities: CapabilityBinding[]
}

/**
 * The vocabularies the configuration forms are generated FROM, from
 * `_registry_payload`.
 *
 * A pure projection of the engine's own constants: no stored value, nothing a
 * concurrent write can tear, and so none of the refusal-by-path contract the
 * document reads carry. A surface generated from it offers exactly what the
 * write door enforces against — a hard-coded field list is how a form comes to
 * offer a setting the door rejects, or to omit one it accepts.
 *
 * `profile_settings` and `efforts` are projected rather than derived from
 * `settings` because neither is derivable from a setting record: pinnability
 * inside a cost profile is not a scope, and effort is not a setting at all. Both
 * are vocabularies the write door enforces, so a form offering either from a copy
 * kept on this side would offer what the door then refuses.
 */
export interface RegistryPayload {
  settings: RegistrySetting[]
  source_presets: SourcePreset[]
  profile_presets: ProfilePreset[]
  /** The dotted keys a cost profile may pin, in the engine's own order. */
  profile_settings: string[]
  roles: string[]
  /** The effort ladder a role assignment may name, least effort first. */
  efforts: string[]
  levels: string[]
  /**
   * The pipeline stages the configuration surface is organised around, each
   * carrying the setting groups and delegable capabilities it presents.
   *
   * Optional in the TYPE and not in the route: the route composes it from
   * `PIPELINE_STAGES` on every read, so a payload without it is one this pane
   * is reading from an older gateway. Named optional so that case renders the
   * whole vocabulary in one advanced area rather than throwing — see
   * `stages.resolveStages`, which folds an unclaimed group there for the same
   * reason.
   */
  stages?: StageVocabulary[]
  /**
   * How a delegated capability may be reached: `builtin`, `mcp`, `command`.
   *
   * Projected so a transport chooser offers exactly what the write door accepts.
   * Optional in the TYPE and not in the route, for `stages`' reason — a payload
   * without it is one an older gateway served, and a chooser then offers nothing
   * rather than a list this side invented.
   */
  transports?: string[]
  /**
   * The capabilities the engine always executes itself, in its own order.
   *
   * Naming one in the `capabilities` section is REFUSED rather than ignored, so a
   * surface showing capabilities names these and offers no control that would
   * attempt one. Projected rather than copied for the same reason as every other
   * vocabulary here: the refusal is the engine's list, not this side's.
   */
  engine_floor?: string[]
  /**
   * Where a quality gate may sit relative to raising the review artifact:
   * `pre_submit`, `post_submit`, or `both`.
   *
   * Its own tuple on the engine's side rather than a `Setting.choices` — no
   * setting declares choices and a test is armed to fail the moment one does — and
   * projected rather than copied here for `transports`' reason: the write door
   * enforces this exact set, so a chooser built from a list kept on this side would
   * offer what the door then refuses by path.
   */
  gate_positions?: string[]
  /** Whether a gate's failure stops the flow: `blocking` or `advisory`. */
  gate_severities?: string[]
  /**
   * The bundled gate declarations, whole and ready to write.
   *
   * Entries rather than names, for the reason the source and cost-profile presets
   * travel whole: a gate is added as a COPY of one, and a surface holding only a
   * name would have to invent the argv it claims to have copied.
   */
  gate_presets?: GatePreset[]
  /**
   * The workflow preset names the engine BUNDLES, in its own order.
   *
   * Two jobs, and both need the engine's list rather than a copy: a preset chooser
   * has to tell a bundled name from a user-defined one, and a form defining a
   * preset has to refuse a bundled name BEFORE composing a write, because the write
   * door refuses one — `'<name>' is a bundled preset name and cannot be redefined`.
   * A list kept on this side would either miss a reserved name the engine added, or
   * reserve one it never did.
   *
   * Optional in the TYPE and not in the route, for `stages`' reason: an older
   * gateway serves no such key, and the chooser then offers only what the document
   * defines rather than a set this side invented.
   */
  workflow_presets?: string[]
  /**
   * How many characters a workflow preset name may hold before it is capped.
   *
   * Every reading of a preset name on `/config/workflow` — the selection, a stage's
   * origin, the user-defined list — is rendered through the engine's display
   * truncation at this width, so a longer name is one this pane can only ever show
   * as a string no document holds. A form that DEFINES a name refuses one past it
   * rather than creating a name it would then have to misreport.
   *
   * Optional in the TYPE and not in the route, for `stages`' reason: an older
   * gateway serves no such key, and no length is then refused — a cap this side
   * invented would refuse a name the engine displays perfectly well.
   */
  workflow_preset_name_limit?: number
}

/**
 * One bundled quality-gate declaration, from `gate_presets()`.
 *
 * The engine's own deep copy, so editing what a form staged cannot reach back into
 * the bundled table. It carries no `origin` or `declared_at`: both describe a
 * declaration in a document, and a preset is not in one yet.
 */
export interface GatePreset {
  name: string
  position: string
  severity: string
  /** One argv per command, in the order the gate runs them. */
  commands: string[][]
}

/**
 * One configured quality gate, from `GET /config/workflow`.
 *
 * `blocking` travels beside `severity` because it is the ENGINE's own reading of
 * that severity. A client deciding for itself which severities stop a run is how a
 * surface comes to describe a flow the engine does not run.
 *
 * `name` and `declared_at` are document-authored, and both arrive sanitized and
 * length-capped so a hand-edited document cannot set the width of a row. That makes
 * the name a DISPLAY value rather than a key: a capped one is not what the document
 * stores, and writing it back would rename the gate. `GateForm` refuses to compose
 * a write over a name it can see was capped.
 */
export interface QualityGate {
  name: string
  /** `pre_submit`, `post_submit`, or `both`. One of `gate_positions`. */
  position: string
  /** `blocking` or `advisory`. One of `gate_severities`. */
  severity: string
  blocking: boolean
  /** The engine's `ValueOrigin` for the declaration. Never user-facing text. */
  origin: string
  /** The command templates as CONFIGURED, run-time variables unexpanded. */
  commands: string[][]
  /** Dotted path of the declaration, `''` when nothing declares it. */
  declared_at: string
}

/**
 * One delivery stage and which layer supplied its commands, from
 * `preset_display.stage_origins()` plus the two fields the route joins on.
 *
 * `source` is deliberately NOT a `ValueOrigin`: `bundled_preset` and `user_preset`
 * are not configuration layers, and flattening them onto one answer would lose the
 * distinction a preset chooser exists to show.
 */
export interface StageOrigin {
  stage: string
  source: 'bundled_preset' | 'user_preset' | 'app_override' | 'project_override' | 'unconfigured'
  preset: string
  declared_at: string
  bundled: boolean
  from_preset: boolean
  skipped: boolean
  summary: string
  /** How many commands the stage runs. The engine's serializer sends a count. */
  commands: number
  /** The resolved commands, added by the route so a form can render them. */
  argv: string[][]
  /** `isolation`, `delivery`, or `archive`; `''` for a stage nothing places. */
  runs_at: string
}

/**
 * The delivery workflow in force for one project, and the app-wide gate list.
 *
 * Both readings arrive in one payload because they come from ONE read of the
 * document: a write landing between two requests could otherwise produce stages and
 * gates that describe different documents.
 *
 * `gates: []` and `gates: null` are DIFFERENT ANSWERS and neither may stand in for
 * the other. Null with `gates_unreadable` means the stored list could not be
 * parsed, and the engine then refuses delivery OUTRIGHT — so rendering it as "no
 * gates configured" would tell an operator that nothing is configured when what is
 * true is that every check is off until the document is repaired.
 */
export interface WorkflowState {
  configured: boolean
  project: string | null
  preset: { name: string; origin: string; declared_at: string; bundled: boolean } | null
  /** One row per DECLARED stage, in the engine's own order. */
  stages: StageOrigin[]
  /**
   * The user-defined preset names, each rendered through the display cap.
   *
   * A DISPLAY reading, like `QualityGate.name` and for the same reason: a name
   * longer than the cap arrives truncated, so it is not the key the document holds
   * and cannot be used as a path segment or compared against a stored selection.
   * `DeliveryWorkflowForm` reads those names from the document instead, and uses
   * this list to state nothing it would then write.
   */
  user_presets: string[]
  /** Which stages the delivery flow itself runs, in flow order. */
  delivery_flow_stages: string[]
  /** Always true: `load_quality_gates` takes no project. */
  gates_scope_is_app: boolean
  gates: QualityGate[] | null
  gates_unreadable: boolean
  gate_errors: Array<{ path: string; message: string }>
}

/**
 * One assertion class evaluated against one fixture, from
 * `CheckResult.to_json_object()`.
 *
 * `detail` is ENGINE prose that may quote a provider's own bytes — the
 * `malformed-response` fixture exists precisely to make a provider echo attacker
 * authored JSON into a schema error. The engine narrows it at the point the reason
 * is composed, so a surface never depends on its own escaping for safety; a
 * surface nonetheless treats it as hostile text, renders it as a text child and
 * caps its length, because two independent narrowings is the difference between a
 * guarantee and an assumption.
 *
 * `excused` is what `passed` cannot say. A check that passed by FINDING a planted
 * defect and one that passed because the candidate declared it did not look are the
 * same boolean, and only this number tells them apart — which is why a pass beside
 * a non-zero count is a qualified pass and never an unqualified one.
 */
export interface ConformanceCheckResult {
  check: string
  fixture: string
  passed: boolean
  detail: string
  excused: number
}

/**
 * What a candidate did with a capability's whole suite, from
 * `ConformanceReport.to_json_object()`.
 *
 * `passed` is deliberately NOT "no failures": the engine computes it as no
 * failures AND no gaps, so a suite that ran nothing, or that never evaluated an
 * assertion class it declared, reports false. A surface must therefore never
 * recompute a verdict from the results alone — and must never read better than
 * this flag.
 *
 * It is also not the whole story in the other direction. `declined_detections` does
 * NOT enter `passed` — declining to look is an honest answer and does not fail the
 * suite — so a report can carry `passed: true` beside a non-zero count. The
 * engine's own summary line qualifies its verdict in that case, and so must any
 * surface: an unqualified pass about a candidate that examined nothing is the one
 * reading this report exists to prevent.
 *
 * `gaps` and `declared_checks` are the same obligation from the other side: a
 * declared check with no result is a failure OF THE RUN, not an absent check.
 */
export interface ConformanceReport {
  capability: string
  candidate: string
  passed: boolean
  declared_checks: string[]
  declared_fixtures: string[]
  gaps: string[]
  declined_detections: number
  results: ConformanceCheckResult[]
}

/**
 * One capability's conformance run, from either conformance route.
 *
 * The suite invokes a provider once per fixture and again for the repeatability
 * check — up to nine calls for a document capability — spawning a child process
 * each time, with no aggregate deadline of its own. So this is a JOB: the POST
 * starts one and answers `202` with `status: 'running'` and no report, and the GET
 * polls. A surface that waited for an outcome would hold a request open for
 * minutes.
 *
 * Four fields carry contracts a renderer cannot infer from their types:
 *
 * - **`status` is five-valued and two of them are not "nothing was wrong".**
 *   `failed` means the run could not be carried out and produced NO report, which
 *   is why it is not `complete` with an empty one. `not_applicable` means the
 *   capability is on its builtin, so no run can be started at all.
 * - **`stale` is derived server-side and is never a client's own comparison.** A
 *   client is not shown the binding's environment values, so any fingerprint it
 *   computed would digest something else — and comparing the wrong two things is
 *   how an earlier outcome goes on being presented as describing the current
 *   binding.
 * - **`is_builtin` describes what is configured NOW, not what the run was
 *   against.** A capability rebound to its builtin after a run keeps its report,
 *   so it polls `complete` while the POST refuses it: `status` alone cannot answer
 *   whether a re-run is possible.
 * - **`deadline_s` and `max_invocations` describe what STARTING a run would do,**
 *   so both arrive with no run recorded. `max_invocations` is an upper bound — a
 *   fixture whose first call raised never reaches its repeatability call — and it
 *   differs by capability, which is why it is projected rather than held here.
 */
export interface ConformanceState {
  capability: string
  status: 'running' | 'complete' | 'failed' | 'absent' | 'not_applicable'
  job_id: string
  candidate: string
  binding_fingerprint: string
  binding_current: string
  stale: boolean
  is_builtin: boolean
  /** The server's per-invocation cap. Never the binding's own `timeout_s`. */
  deadline_s: number
  /** The most calls a run makes against the provider. An upper bound. */
  max_invocations: number
  /** Why a run did not happen, `''` for every other status. */
  error: string
  report: ConformanceReport | null
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

/**
 * One (submitter class, spec type) pair's resolved autonomy, from `_source_grid`.
 *
 * `origin` is the field a reader branches on, and it is NOT derivable here: the
 * engine resolves class-first with a wildcard fallback, so the level in a cell may
 * have been declared for this exact pair, for a broader one, or nowhere at all.
 * Re-deriving that from `declared_at` would mean re-implementing the resolver's
 * path composition on this side — and a source name holding a dot defeats every
 * split, which is why the backend classifies whole strings and sends the verdict.
 *
 * `declared_at` is `''` when nothing stored answered the pair, never `null` or
 * absent, matching the resolved read's spelling of the same idea. An empty one is
 * only meaningful together with `origin === 'default'`; a caller must not read
 * emptiness as the classification.
 *
 * `policy_covers_gates` is the engine's own `permits(execution)` — what
 * `gate_is_policy_covered` reduces to for a document gate — so the marker beside
 * a cell cannot drift from what the gates actually do with it.
 */
export interface SourceGridCell {
  /** The resolved level, in the engine's vocabulary. Rendered, never mapped. */
  level: string
  /** Dotted path of the stored cell that answered the pair, `''` when none did. */
  declared_at: string
  origin: 'exact' | 'wildcard' | 'default'
  policy_covers_gates: boolean
}

/** One Watch_Source's full matrix, keyed submitter class then spec type. */
export interface SourceGrid {
  name: string
  grid: Record<string, Record<string, SourceGridCell>>
}

/**
 * Every Watch_Source's resolved autonomy matrix, from `_sources_snapshot`.
 *
 * The three vocabulary arrays are the ENGINE's axes, shipped so a surface renders
 * them rather than carrying a copy: a class or spec type the schema adds shows up
 * without a client edit, and a client cannot render an axis the resolver has no
 * answer for. `submitter_classes` is in the schema's order, which runs from most
 * to least trusted — so the last entry is the class an unclassifiable author
 * falls to.
 */
export interface SourcesPayload {
  sources: SourceGrid[]
  submitter_classes: readonly string[]
  spec_types: readonly string[]
  levels: readonly string[]
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

// ── the app's routes ──────────────────────────────────────────────────────
//
// Uncounted on purpose: the number was already wrong before the extension seams
// added to it, and a count in a banner restales on the next route either way. The
// registered set is pinned where it can be enforced, in the backend's own
// registered-surface test.

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

  /**
   * GET the vocabularies the configuration forms are generated from.
   *
   * Bundled constants only — the setting registry, the watch-source presets, the
   * cost-profile preset names, the role and level names — so it reads no
   * document and cannot refuse by path. A form built from this offers what the
   * write door accepts, which a hard-coded field list cannot promise.
   */
  configRegistry: (): Promise<RegistryPayload> =>
    request<RegistryPayload>(`${API}/config/registry`),

  /**
   * GET every Watch_Source's autonomy grid, resolved cell by cell.
   *
   * A read of the same document `config` returns, resolved through the same policy
   * the gates read — not a second view of the grid a caller could compute itself.
   * The matrix arrives already resolved BECAUSE class-first precedence and the
   * wildcard fallback live in one place: a TS re-derivation would be a second
   * resolver, and the copy that drifted would be the one an operator reads before
   * deciding who may run unattended.
   *
   * Takes no source parameter: every configured source arrives, including one with
   * no grid at all, because a configured source nobody wrote a grid for is exactly
   * the fail-closed case an operator most needs to see.
   */
  sources: (): Promise<SourcesPayload> => request<SourcesPayload>(`${API}/config/sources`),

  /**
   * GET every delegable capability's bound provider, and whether it can be reached.
   *
   * A document read, so it can refuse: a stored `capabilities` section the engine
   * will not resolve answers 422 with no `capabilities` key at all rather than
   * degrading to the all-builtin map an unconfigured document returns. A caller
   * must therefore state the failure and NOT fall back — those two payloads are
   * the same shape and opposite facts.
   *
   * Takes no project. The engine reads bindings from ONE app-wide section with no
   * per-project layer, so a project parameter would imply a scope that does not
   * exist.
   */
  configCapabilities: (): Promise<CapabilitiesPayload> =>
    request<CapabilitiesPayload>(`${API}/config/capabilities`),

  /**
   * GET the delivery workflow in force for a project, and the app-wide gate list.
   *
   * Project-scoped like {@link resolvedConfig}, because a project selects its own
   * workflow preset and may override a stage. The gate list is NOT project-scoped —
   * `load_quality_gates` takes no project — and the payload says so in
   * `gates_scope_is_app` rather than leaving a caller to infer it from the heading
   * the stages sit under.
   *
   * One request for both because the route reads the document ONCE: two requests
   * could straddle a write and return stages and gates from different documents.
   *
   * An unparseable gate list arrives as `gates: null` with `gates_unreadable`, never
   * as an empty list, and a caller must keep the two apart: the engine refuses
   * delivery outright on an unreadable list, so "no gates" would report every check
   * as absent by choice when the truth is that they are off until a repair.
   */
  configWorkflow: (project?: string): Promise<WorkflowState> => {
    const query = project ? `?project=${encodeURIComponent(project)}` : ''
    return request<WorkflowState>(`${API}/config/workflow${query}`)
  },

  /**
   * GET whether a capability's provider has been through the conformance suite.
   *
   * A poll, and the only way to read an outcome: the run is a job. It answers
   * `running` while one is in flight, `complete` with a report when one finished,
   * `failed` when the suite could not be carried out at all, `absent` when nobody
   * has run one, and `not_applicable` when the capability is on its builtin.
   *
   * The binding is resolved on EVERY poll rather than only when a run starts,
   * because `stale` answers "does this report still describe what is configured"
   * and that changes when the document does, not when the run does.
   */
  conformance: (capability: string): Promise<ConformanceState> =>
    request<ConformanceState>(`${API}/config/conformance/${encodeURIComponent(capability)}`),

  /**
   * Start a conformance run against a capability's configured provider.
   *
   * Returns as soon as the job is recorded, with `status: 'running'` and no
   * report — never an outcome. The suite spawns a child process per invocation and
   * caps nothing in aggregate, so a caller that awaited a verdict would be holding
   * a request open for minutes.
   *
   * Refused with `conformance_running` when a run for that capability is already in
   * flight, and with `builtin_binding` when the capability is on its builtin. Both
   * reasons are the server's own and are relayed rather than reworded, because they
   * name the load a second run would put on one program.
   */
  startConformance: (capability: string): Promise<{ ok: boolean } & ConformanceState> =>
    postJson(`${API}/config/conformance`, { capability }),

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
  /**
   * The per-source autonomy matrices.
   *
   * Under the config key's prefix on purpose: React Query matches keys by prefix,
   * so invalidating the document after a write refreshes the grid too. The grid is
   * a read OF that document, and a stale matrix beside a fresh document would tell
   * an operator that a class may run unattended when the write they just made says
   * otherwise.
   */
  sources: ['spec-engine', 'config', 'sources'] as const,
  /**
   * The capability bindings and their reachability.
   *
   * Under the config key's prefix, for {@link QK.sources}' reason: it is a read OF
   * the document, so a write that rebinds a capability must refresh it. A stale
   * binding beside a fresh document would name the provider an operator just
   * replaced.
   */
  capabilities: ['spec-engine', 'config', 'capabilities'] as const,
  /**
   * The delivery workflow in force, keyed by the project it resolves FOR.
   *
   * Keyed by project for {@link QK.resolved}'s reason: a project selects its own
   * preset and may override a stage, so one cache entry for two projects would show
   * a workflow under a project it is not in force for. The app-wide gate list rides
   * along in the same payload and is the same for every key, which is what the
   * payload's own `gates_scope_is_app` says out loud.
   *
   * Under the config key's prefix, for {@link QK.sources}' reason: it is a read OF
   * the document, so a write that changes a stage or a gate must refresh it.
   */
  workflow: (project: string) => ['spec-engine', 'config', 'workflow', project] as const,
  /**
   * One capability's conformance run, keyed by the capability it checked.
   *
   * Per capability because a run is per capability: the server refuses a second
   * concurrent run for the same one and keeps at most one outcome for each, so a
   * shared entry would show one provider's verdict beside another's binding.
   *
   * Under the config key's prefix, which is what makes a binding change invalidate
   * the outcome. The payload's `stale` and `is_builtin` are both derived from the
   * binding as it is NOW, so a rebind has to refetch this — an entry left behind
   * would keep answering `complete` and `stale: false` about a provider that is no
   * longer bound.
   */
  conformance: (capability: string) =>
    ['spec-engine', 'config', 'conformance', capability] as const,
  /**
   * The form vocabularies.
   *
   * Deliberately OUTSIDE the config key's prefix, which is the opposite choice
   * from {@link QK.sources} and for the same reason stated the other way round:
   * the grid is a read OF the document and must refresh when the document
   * changes, while this payload is a projection of constants no write can move.
   * Under the prefix, every configuration write would refetch a vocabulary that
   * cannot have changed.
   */
  registry: ['spec-engine', 'config-registry'] as const,
}

/** The prefix every resolved-read key shares, for invalidating them together. */
export const QK_RESOLVED_ROOT = ['spec-engine', 'config', 'resolved'] as const
