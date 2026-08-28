/**
 * What a conformance run may be presented as, and in what order.
 *
 * Pure, and deliberately outside the panel: this module is the only place a
 * verdict is decided, so "the presentation never reads better than the report" is
 * one function's contract rather than a property of however many branches a
 * renderer happens to have.
 *
 * ## The four ways a surface could quietly upgrade a bad result
 *
 * Each is closed here, and each is closed by taking the WORSE of two answers
 * rather than by trusting one:
 *
 * 1. **A pass beside a failing check.** The engine computes `passed` as no
 *    failures and no gaps, so the two cannot disagree in a report it produced.
 *    {@link reading} still reads both and answers `failed` if either says so,
 *    because a payload from a newer gateway, a proxy, or a fixture is not a
 *    payload the engine produced.
 * 2. **A declared check with no result.** That is a failure OF THE RUN — the
 *    runner reporting it cannot speak for part of the contract — and not an absent
 *    row. It gets a row of its own, marked as never run, and it forces the verdict
 *    down.
 * 3. **A declined detection.** `declined_detections` does NOT enter the engine's
 *    `passed`, because a provider that says it did not look is not lying. So a
 *    report can carry `passed: true` beside a non-zero count, and the honest
 *    reading is a QUALIFIED pass — the same qualifier the engine's own summary
 *    line puts in its verdict.
 * 4. **No outcome at all.** Running, refused, never started, or describing a
 *    binding that has since changed: each answers `no_outcome`, never a pass. The
 *    absence of failures is not the absence of problems, it is the absence of
 *    evidence.
 *
 * ## Why the order is data and not markup
 *
 * {@link presentedRows} puts the verdict first and the checks after it, and the
 * panel iterates that array rather than laying the two out itself. A completed run
 * routinely has a green check beside a red verdict — the transport SIGKILLs a
 * provider's child AT its deadline, so a provider that ignored the deadline still
 * measures as answering inside the grace period and `timeout_honoring` PASSES while
 * every payload-derived check fails — and a reader who meets that green row first
 * has been told the opposite of what the run found. Keeping the order here means
 * one array, checkable without a DOM.
 */
import type { ConformanceReport, ConformanceState } from './api'

/** What one check did, once the two ways of passing are told apart. */
export type CheckOutcome = 'passed' | 'declined' | 'failed' | 'never_ran'

/**
 * What the run as a whole may be presented as.
 *
 * Ordered by how reassuring it is, which {@link READING_RANK} makes explicit:
 * `passed` is a claim that everything the suite declared was evaluated and held,
 * `qualified` is the same with detections the candidate declined, `failed` is a
 * verdict against the provider, and `no_outcome` is the floor — no verdict of any
 * kind is supported.
 */
export type Reading = 'passed' | 'qualified' | 'failed' | 'no_outcome'

/**
 * How reassuring each reading is. Higher reads better.
 *
 * `failed` sits ABOVE `no_outcome` because both are claims and only one of them is
 * free: stating that a provider failed when no run produced a report is asserting a
 * verdict without evidence, in the same way stating a pass is. The floor is
 * therefore "nothing is claimed", and every step above it has to be earned.
 */
export const READING_RANK: Record<Reading, number> = {
  passed: 3,
  qualified: 2,
  failed: 1,
  no_outcome: 0,
}

/**
 * Which state the panel is in, which is a different question from the reading.
 *
 * The reading answers "what may be claimed about the provider"; this answers "what
 * happened", which decides the copy. Every situation but `complete` reads
 * `no_outcome`.
 */
export type Situation =
  /** The capability is on its builtin, so there is nothing to check. */
  | 'not_applicable'
  /** Nobody has started a run for this binding. */
  | 'never_run'
  /** A run is in flight. */
  | 'running'
  /** A run happened and produced no report, or the state is unreadable. */
  | 'no_outcome'
  /** A report exists and describes a binding that is no longer configured. */
  | 'earlier_binding'
  /** A report exists and describes the binding in force. */
  | 'complete'

/** One check's outcome as a row, with the engine's reason already narrowed. */
export interface CheckRow {
  check: string
  /** The fixture it was evaluated against, `''` for a check that never ran. */
  fixture: string
  outcome: CheckOutcome
  /** The engine's reported reason, control-stripped and length-capped. */
  reason: string
  /** Planted defects this check passed by accepting a declared skip. */
  excused: number
  /** Whether the suite declared this check. A result for one it did not still shows. */
  declared: boolean
}

/** A conformance run as the panel presents it. */
export interface ConformanceView {
  situation: Situation
  reading: Reading
  /** One row per declared check, plus any result for a check nobody declared. */
  checks: CheckRow[]
  /** Detections the candidate declined across the whole run. */
  declined: number
  /** What the suite declared and never evaluated, in the engine's own words. */
  gaps: string[]
  /** Why a run did not happen, `''` when that is not what occurred. */
  error: string
  /** What was checked: the program, or the transport when it names none. */
  candidate: string
  /** Whether a run may be started now. */
  canStart: boolean
  /** The most calls a run would make against the provider. */
  maxInvocations: number
  /** The per-invocation deadline the server will impose. */
  deadlineSeconds: number
}

/** One block of the presentation, in the order it must be shown. */
export type PresentedRow =
  | { kind: 'verdict'; reading: Reading }
  | { kind: 'check'; check: CheckRow }

/**
 * How long to wait before polling again, or `false` to stop polling.
 *
 * A run takes minutes and has no aggregate deadline, so the interval is a
 * compromise between an operator watching the panel and a poll that resolves the
 * binding on every tick. Polling stops the moment the run is not running — every
 * other status is terminal until an operator acts.
 */
export const CONFORMANCE_POLL_MS = 3000

/** Whether to poll again given the state just read, and after how long. */
export function pollAfterMs(state: ConformanceState | null | undefined): number | false {
  return state?.status === 'running' ? CONFORMANCE_POLL_MS : false
}

/**
 * How much of one reason is shown before it is cut.
 *
 * The engine already caps this at its own display limit, which is four kilobytes —
 * right for a log line and far past what a row beside a control can hold without
 * the row deciding the page's layout.
 */
export const MAX_REASON_CHARS = 240

/** What a cut reason ends with, matching the engine's own truncation notice. */
export const REASON_TRUNCATION_NOTICE = ' \u2026'

/**
 * Characters that are not text however they arrived: C0, DEL, and the bidi
 * overrides and isolates.
 *
 * The same class the engine strips when it composes a reason, applied again here.
 * That repetition is the point rather than a redundancy: a carriage return
 * overwrites the line printed before it and a bidi override reorders what follows
 * it, so neither may reach a surface — and a surface that assumed the other end had
 * removed them would be relying on the other end never changing.
 */
const NOT_TEXT = /[\u0000-\u001f\u007f\u202a-\u202e\u2066-\u2069]/g

/**
 * One reported reason as text: control-stripped, then length-capped.
 *
 * In that order, so a string of control characters cannot spend the whole cap and
 * arrive as an empty row with a truncation notice.
 *
 * This does not neutralise markup and does not try to. A reason is rendered as a
 * text CHILD, which is what makes it text rather than markup, and no amount of
 * escaping here would help a surface that used `dangerouslySetInnerHTML` instead.
 * The rule the panel keeps is the absence of that attribute.
 */
export function reasonText(detail: string): string {
  const cleaned = detail.replace(NOT_TEXT, '')
  if (cleaned.length <= MAX_REASON_CHARS) return cleaned
  return cleaned.slice(0, MAX_REASON_CHARS) + REASON_TRUNCATION_NOTICE
}

/** What one result did, telling a found defect apart from a declined one. */
function outcomeOf(result: { passed: boolean; excused: number }): CheckOutcome {
  if (!result.passed) return 'failed'
  return result.excused > 0 ? 'declined' : 'passed'
}

/**
 * One row per check the suite declared, in the order it declared them.
 *
 * A declared check with no result gets a row saying it never ran, because the
 * alternative — leaving it out — renders a run that evaluated two of five checks as
 * a two-check run that went fine. A result for a check the suite did NOT declare is
 * appended rather than dropped, for the mirror-image reason: it is evidence, and a
 * payload this side does not recognise is not a payload it may edit.
 */
export function checkRows(report: ConformanceReport): CheckRow[] {
  const results = report.results ?? []
  const declared = report.declared_checks ?? []
  const rows: CheckRow[] = []
  for (const check of declared) {
    const matching = results.filter((result) => result.check === check)
    if (matching.length === 0) {
      rows.push({
        check,
        fixture: '',
        outcome: 'never_ran',
        reason: '',
        excused: 0,
        declared: true,
      })
      continue
    }
    for (const result of matching) {
      rows.push({
        check,
        fixture: result.fixture,
        outcome: outcomeOf(result),
        reason: reasonText(result.detail ?? ''),
        excused: result.excused ?? 0,
        declared: true,
      })
    }
  }
  const known = new Set(declared)
  for (const result of results) {
    if (known.has(result.check)) continue
    rows.push({
      check: result.check,
      fixture: result.fixture,
      outcome: outcomeOf(result),
      reason: reasonText(result.detail ?? ''),
      excused: result.excused ?? 0,
      declared: false,
    })
  }
  return rows
}

/**
 * The best reading *report* supports, taking the worse of every disagreement.
 *
 * The engine's `passed` is read AND the parts are read, and either one saying
 * "failed" is enough. That is not defensive duplication of the engine's arithmetic:
 * the two cannot disagree in a report the engine composed, so the only payloads
 * this ordering changes the answer for are the ones nothing guarantees — and on
 * those, the rosier of two readings is exactly the one that must not be shown.
 */
export function reading(report: ConformanceReport, rows: readonly CheckRow[]): Reading {
  // The engine's own first gap: a suite that produced no results at all has
  // produced no evidence. It folds that into `passed`, so a report claiming a pass
  // with an empty result list is one nothing composed — and the claim is exactly
  // the one that must not be relayed.
  if ((report.results ?? []).length === 0) return 'failed'
  const unevaluated = rows.some((row) => row.outcome === 'never_ran')
  const failed = rows.some((row) => row.outcome === 'failed')
  if (!report.passed || failed || unevaluated || (report.gaps ?? []).length > 0) return 'failed'
  const declined =
    (report.declined_detections ?? 0) > 0 || rows.some((row) => row.outcome === 'declined')
  return declined ? 'qualified' : 'passed'
}

/** Which state *state* describes, given whether it carries a report. */
function situationOf(
  state: ConformanceState | null | undefined,
  report: ConformanceReport | null,
): Situation {
  if (!state) return 'no_outcome'
  // Before anything else: a run in flight has no outcome, whatever is cached
  // beside it. The server drops the previous report when a new run starts, and
  // this branch is the second guarantee of the same thing.
  if (state.status === 'running') return 'running'
  if (report === null) {
    if (state.status === 'absent') return 'never_run'
    if (state.status === 'not_applicable') return 'not_applicable'
    // `failed`, or a `complete` that arrived with no report — a shape the route
    // does not produce, and one that must never read as "complete, no failures".
    return 'no_outcome'
  }
  // `is_builtin` before `stale`, though a rebind to the builtin moves the
  // fingerprint too and so is already stale: this reads the binding as it IS
  // rather than a comparison, so a fingerprint that stopped changing would not
  // quietly restore a verdict about a provider nothing is bound to.
  if (state.is_builtin || state.stale) return 'earlier_binding'
  if (state.status !== 'complete') return 'no_outcome'
  return 'complete'
}

/**
 * *state* as the panel presents it.
 *
 * Total over the payload including its absence: a state this side has not read yet
 * answers `no_outcome`, which is the floor rather than a special case.
 */
export function conformanceView(state: ConformanceState | null | undefined): ConformanceView {
  const report = state?.report ?? null
  const checks = report === null ? [] : checkRows(report)
  const situation = situationOf(state, report)
  return {
    situation,
    // A reading only where a report describes the binding in force. Every other
    // situation is the absence of evidence, and the absence of evidence has no
    // verdict in it — not a pass, and not a failure either.
    reading: situation === 'complete' && report !== null ? reading(report, checks) : 'no_outcome',
    checks,
    declined: report?.declined_detections ?? 0,
    gaps: report?.gaps ?? [],
    error: state?.error ?? '',
    candidate: state?.candidate ?? '',
    // The server's own answer about the binding as it is now, not a reading of the
    // run's status: a capability rebound to its builtin polls `complete` while the
    // start is refused, so offering the run its status implies would offer
    // something the server declines.
    canStart: state !== null && state !== undefined && !state.is_builtin && state.status !== 'running',
    maxInvocations: state?.max_invocations ?? 0,
    deadlineSeconds: state?.deadline_s ?? 0,
  }
}

/**
 * The blocks of the presentation in the order they must appear: verdict, then
 * checks.
 *
 * Consumed by the panel for its own iteration, so the order is this array's and
 * not a property of how the markup happens to be nested.
 */
export function presentedRows(view: ConformanceView): PresentedRow[] {
  return [
    { kind: 'verdict', reading: view.reading },
    ...view.checks.map((check) => ({ kind: 'check' as const, check })),
  ]
}
