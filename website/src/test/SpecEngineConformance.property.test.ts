/**
 * Property 4: a conformance verdict never reads better than the report.
 *
 * The panel is the last place a bad conformance result could be quietly upgraded,
 * and there are four ways to do it: relay a `passed` flag that the results
 * contradict, drop a declared check that never ran, report a pass whose detections
 * were declined, or present a run that produced no outcome as one that found nothing
 * wrong. Each is a different payload, so this is a property rather than four cases.
 *
 * ## Why the generator makes a report DISAGREE with itself
 *
 * A report whose flag, results, gaps and declared checks all tell one story cannot
 * falsify anything: any renderer that read any one of those fields would agree with
 * any other, and the property would pass for a renderer that read only the flag. So
 * `passed` is drawn INDEPENDENTLY of the results, the gaps and the declared-check
 * list, and `declined_detections` independently of the per-check `excused` counts.
 * The engine's own reports are of course consistent — it derives `passed` from the
 * failures and gaps — and that is exactly the point: on a payload nothing guarantees,
 * the rosier of two readings is the one that must not reach a reader.
 *
 * ## Why the ceiling is computed twice
 *
 * The best reading a payload supports is derived HERE, from the raw fields, by
 * different code from the production reduction. A property that asked the module
 * under test for both sides would be comparing a function with itself.
 *
 * ## Why an over-optimistic renderer is run through the same predicate
 *
 * A mutation probe proves a property is falsifiable at one point in time and by one
 * hand. {@link optimisticReading} keeps that evidence in the suite: it is a renderer
 * that reads the `passed` flag and nothing else — the single most plausible way to
 * write this — and the last test asserts that the property REJECTS it. If the
 * property is ever weakened into a tautology, that test starts failing too.
 */
import { describe, it, expect } from 'vitest'
import * as fc from 'fast-check'

import type { ConformanceReport, ConformanceState } from '../apps/spec-engine/api'
import {
  READING_RANK,
  checkRows,
  conformanceView,
  presentedRows,
  type Reading,
} from '../apps/spec-engine/conformanceView'

/** The assertion classes the bundled suite declares, plus one it could grow. */
const CHECK = fc.constantFrom(
  'schema_validity',
  'declared_coverage',
  'timeout_honoring',
  'repeatability',
  'planted_defect',
  'a_check_added_later',
)

/** The fixtures a document capability's suite ships, plus one it could grow. */
const FIXTURE = fc.constantFrom(
  'minimal-request',
  'planted-ambiguity',
  'contradictory-criteria',
  'coverage-hole',
  'oversized-document',
  'malformed-response',
  'a_fixture_added_later',
)

/**
 * One check result whose `passed` and `excused` are drawn independently.
 *
 * A failing result with a non-zero `excused` is not a shape the engine composes —
 * excusing happens on the passing path — and it is generated anyway: a renderer that
 * decided a row's outcome from `excused` before `passed` would call it a declined
 * pass, which reads better than the failure it is.
 */
const RESULT = fc.record({
  check: CHECK,
  fixture: FIXTURE,
  passed: fc.boolean(),
  detail: fc.oneof(
    fc.constantFrom('', 'the candidate raised TransportFailure', 'x'.repeat(600)),
    fc.string({ maxLength: 20 }),
  ),
  excused: fc.integer({ min: 0, max: 3 }),
})

/**
 * A report whose verdict, results, gaps and declared checks are all independent.
 *
 * So the generator produces, among much else: a `passed: true` beside a failing
 * result, a `passed: true` beside a gap, a declared check with no result at all
 * beside `passed: true`, and a clean pass beside a non-zero `declined_detections`.
 * Those four are the four ways this surface could flatter a provider, and none of
 * them is a special case here — they arrive as ordinary draws.
 */
const REPORT: fc.Arbitrary<ConformanceReport> = fc.record({
  capability: fc.constant('analysis'),
  candidate: fc.constantFrom('my-analyzer', 'mcp'),
  passed: fc.boolean(),
  declared_checks: fc.uniqueArray(CHECK, { maxLength: 4 }),
  declared_fixtures: fc.uniqueArray(FIXTURE, { maxLength: 3 }),
  gaps: fc.array(fc.constantFrom("check 'planted_defect' was declared but never evaluated"), {
    maxLength: 2,
  }),
  declined_detections: fc.integer({ min: 0, max: 4 }),
  results: fc.array(RESULT, { maxLength: 5 }),
})

/** Every status the routes answer, drawn independently of whether a report is attached. */
const STATUS = fc.constantFrom(
  'running' as const,
  'complete' as const,
  'failed' as const,
  'absent' as const,
  'not_applicable' as const,
)

/**
 * One conformance state, with the report's presence drawn independently of `status`.
 *
 * `running` beside a report, and `complete` beside none, are both shapes the routes
 * do not produce — the server drops the previous report when a run starts, and a
 * recorded complete job always carries one. Generated anyway, because "complete, no
 * failures" is precisely how the absence of an outcome gets read as a pass, and a
 * surface that trusted the pairing would have no answer for a payload that broke it.
 */
const STATE_FROM_INDEPENDENT_FIELDS: fc.Arbitrary<ConformanceState> = fc
  .record({
    status: STATUS,
    report: fc.option(REPORT, { nil: null }),
    stale: fc.boolean(),
    is_builtin: fc.boolean(),
    job_id: fc.constantFrom('', 'job-1'),
    error: fc.constantFrom('', 'OSError: no temporary directory'),
  })
  .map((drawn) => ({
    capability: 'analysis',
    status: drawn.status,
    job_id: drawn.job_id,
    candidate: 'my-analyzer',
    binding_fingerprint: 'a'.repeat(64),
    binding_current: drawn.stale ? 'b'.repeat(64) : 'a'.repeat(64),
    stale: drawn.stale,
    is_builtin: drawn.is_builtin,
    deadline_s: 10,
    max_invocations: 9,
    error: drawn.error,
    report: drawn.report,
  }))

/**
 * A CONSISTENT report of a run that held, with detections possibly declined.
 *
 * Independent fields alone make the interesting readings vanishingly rare: reaching
 * `passed` needs a complete status, a fresh binding, a true flag, no gaps, at least
 * one result, every result passing and every declared check evaluated, and drawing
 * all of those together happens in a handful of runs out of five hundred. A property
 * that never reaches a reading proves nothing about it — and the mutant that reports
 * a declined detection as an unqualified pass SURVIVED five hundred runs of the
 * independent generator, which is how this was found rather than assumed.
 *
 * So this arm builds reports the engine really composes, and draws only `excused`
 * freely. It is not the safe half of the generator: `excused` is what separates a
 * pass from a qualified pass, and it is the one thing a renderer is most likely to
 * drop.
 */
const COHERENT_REPORT: fc.Arbitrary<ConformanceReport> = fc
  .record({
    declared: fc.uniqueArray(CHECK, { minLength: 1, maxLength: 4 }),
    fixture: FIXTURE,
    excused: fc.array(fc.integer({ min: 0, max: 2 }), { minLength: 4, maxLength: 4 }),
  })
  .map(({ declared, fixture, excused }) => {
    const results = declared.map((check, index) => ({
      check,
      fixture,
      passed: true,
      detail: 'the check held',
      excused: excused[index % excused.length],
    }))
    return {
      capability: 'analysis',
      candidate: 'my-analyzer',
      // Exactly what the engine derives for this report: no failures and no gaps.
      passed: true,
      declared_checks: declared,
      declared_fixtures: [fixture],
      gaps: [],
      declined_detections: results.reduce((total, result) => total + result.excused, 0),
      results,
    }
  })

/** A finished run against the binding in force, carrying a consistent report. */
const STATE_FROM_A_COMPLETED_RUN: fc.Arbitrary<ConformanceState> = COHERENT_REPORT.map(
  (report) => ({
    capability: 'analysis',
    status: 'complete' as const,
    job_id: 'job-1',
    candidate: 'my-analyzer',
    binding_fingerprint: 'a'.repeat(64),
    binding_current: 'a'.repeat(64),
    stale: false,
    is_builtin: false,
    deadline_s: 10,
    max_invocations: 9,
    error: '',
    report,
  }),
)

/**
 * A finished run against the binding in force, carrying a report drawn FREELY.
 *
 * The arm that does the work. Fixing `status`, `stale` and `is_builtin` to the one
 * combination that admits a verdict is what makes the report's own contradictions
 * reachable: a `passed: true` beside a failing result, a declared check with no
 * result, a gap beside a clean flag. Without it those all hide behind
 * `no_outcome` — the independent arm reaches a verdict of any kind in about one
 * draw in twenty, and the coverage test below is what said so.
 */
const STATE_FROM_A_LANDED_RUN: fc.Arbitrary<ConformanceState> = REPORT.map((report) => ({
  capability: 'analysis',
  status: 'complete' as const,
  job_id: 'job-1',
  candidate: 'my-analyzer',
  binding_fingerprint: 'a'.repeat(64),
  binding_current: 'a'.repeat(64),
  stale: false,
  is_builtin: false,
  deadline_s: 10,
  max_invocations: 9,
  error: '',
  report,
}))

/**
 * Three arms, weighted evenly.
 *
 * The independent arm reaches every status, every pairing of a status with a report
 * that does or does not exist, and both binding-moved flags. The landed-run arm
 * reaches the report's own contradictions. The completed-run arm reaches the two
 * readings a contradiction would upgrade FROM. No arm alone falsifies every mutant
 * this suite rejects, which the coverage test asserts rather than assumes.
 */
const STATE: fc.Arbitrary<ConformanceState> = fc.oneof(
  STATE_FROM_INDEPENDENT_FIELDS,
  STATE_FROM_A_LANDED_RUN,
  STATE_FROM_A_COMPLETED_RUN,
)

/**
 * The best reading *state* supports, derived from the raw payload.
 *
 * Deliberately written from the fields rather than by calling the production
 * reduction, and deliberately structured differently from it: this walks the
 * evidence and takes the first disqualification it meets, where production reduces
 * over rows it has already built. Two spellings of one rule is the only way a
 * property about that rule is not a comparison of a function with itself.
 */
function ceiling(state: ConformanceState): Reading {
  const report = state.report
  // No evidence at all about the binding in force. Every one of these is a state in
  // which NOTHING may be claimed — not a pass, and not a failure either, because a
  // verdict without a report is a verdict nobody reached.
  if (report === null) return 'no_outcome'
  if (state.status !== 'complete') return 'no_outcome'
  if (state.stale || state.is_builtin) return 'no_outcome'
  // A suite that produced nothing has produced no evidence, which is the engine's
  // own first gap.
  if (report.results.length === 0) return 'failed'
  if (!report.passed) return 'failed'
  if (report.gaps.length > 0) return 'failed'
  for (const result of report.results) {
    if (!result.passed) return 'failed'
  }
  const evaluated = new Set(report.results.map((result) => result.check))
  for (const check of report.declared_checks) {
    // Declared and never evaluated: the run cannot speak for that part of the
    // contract, which is a failure OF THE RUN.
    if (!evaluated.has(check)) return 'failed'
  }
  if (report.declined_detections > 0) return 'qualified'
  for (const result of report.results) {
    if (result.excused > 0) return 'qualified'
  }
  return 'passed'
}

/**
 * The renderer this property exists to reject: it reads the flag and nothing else.
 *
 * Not a straw man. It is the shape a first implementation takes — the payload has a
 * `passed` boolean, so a panel renders it — and every one of the four upgrades this
 * property guards against is reachable through it.
 */
function optimisticReading(state: ConformanceState): Reading {
  if (state.report === null) return 'no_outcome'
  return state.report.passed ? 'passed' : 'failed'
}

/** Property 4 over one reading function, so the same predicate can judge two. */
function neverReadsBetter(reading: (state: ConformanceState) => Reading) {
  return fc.property(STATE, (state) => {
    expect(READING_RANK[reading(state)]).toBeLessThanOrEqual(READING_RANK[ceiling(state)])
  })
}

describe('a conformance verdict never reads better than the report', () => {
  it('generates every reading often enough for the properties below to mean anything', () => {
    // The anti-vacuity guard, and it earned its place: with the independent arm
    // alone, `qualified` appeared so rarely that a renderer reporting a declined
    // detection as an unqualified pass passed five hundred runs. A property that
    // never reaches a reading says nothing about it.
    //
    // SEEDED, and that is the whole difference between a coverage claim and a coin
    // toss. Unseeded, `fc.sample` draws from a fresh generator every run and this
    // assertion samples a random variable whose `passed` count sits close to the
    // boundary it is compared against: measured over twenty consecutive runs it
    // landed on exactly 20 once, failing a test nothing had changed. Raising the
    // sample size or lowering the threshold would only widen a margin that is still
    // probabilistic — the defect is that the claim was not reproducible, so the fix
    // is to make the draw fixed. The counts then move only when the GENERATOR moves,
    // which is the thing this guard is actually about.
    const seen = new Map<Reading, number>()
    for (const state of fc.sample(STATE, { numRuns: 600, seed: 20260828 })) {
      const reading = ceiling(state)
      seen.set(reading, (seen.get(reading) ?? 0) + 1)
    }
    for (const reading of ['passed', 'qualified', 'failed', 'no_outcome'] as const) {
      expect(seen.get(reading) ?? 0, `${reading} was drawn ${seen.get(reading) ?? 0} times`)
        .toBeGreaterThan(20)
    }
  })

  it('never presents a reading above what the payload supports', () => {
    fc.assert(neverReadsBetter((state) => conformanceView(state).reading), { numRuns: 500 })
  })

  it('presents exactly the reading the payload supports, not merely one no better', () => {
    // The stronger claim, kept separate because its failure means something else: a
    // reading BELOW the ceiling breaks no promise about flattery, and it does hide a
    // verdict the run earned.
    fc.assert(
      fc.property(STATE, (state) => {
        expect(conformanceView(state).reading).toBe(ceiling(state))
      }),
      { numRuns: 500 },
    )
  })

  it('presents a pass only when every declared check was evaluated and held', () => {
    fc.assert(
      fc.property(STATE, (state) => {
        const view = conformanceView(state)
        if (view.reading !== 'passed') return
        const report = state.report
        expect(report).not.toBeNull()
        if (report === null) return
        expect(state.status).toBe('complete')
        expect(state.stale).toBe(false)
        expect(state.is_builtin).toBe(false)
        expect(report.passed).toBe(true)
        expect(report.gaps).toEqual([])
        expect(report.declined_detections).toBe(0)
        expect(report.results.length).toBeGreaterThan(0)
        const evaluated = new Set(report.results.map((result) => result.check))
        for (const check of report.declared_checks) expect(evaluated.has(check)).toBe(true)
        for (const result of report.results) {
          expect(result.passed).toBe(true)
          expect(result.excused).toBe(0)
        }
      }),
      { numRuns: 500 },
    )
  })

  it('shows no check above the run verdict, whatever the payload holds', () => {
    fc.assert(
      fc.property(STATE, (state) => {
        const rows = presentedRows(conformanceView(state))
        expect(rows[0].kind).toBe('verdict')
        expect(rows.slice(1).some((row) => row.kind === 'verdict')).toBe(false)
      }),
      { numRuns: 300 },
    )
  })

  it('carries no check row while a run is in flight', () => {
    // The rows and the running sentence are ONE answer. Flooring only the reading
    // would leave a payload that arrived `running` beside an earlier report printing
    // that run's per-check outcomes directly beneath the sentence promising no
    // earlier outcome is shown — the verdict withheld and the evidence for it
    // displayed. The generator draws the report's presence independently of `status`
    // precisely so this pairing is reachable here even though the routes never emit
    // it.
    //
    // Scoped to `running` on purpose: `earlier_binding` floors its VERDICT while
    // still relaying the rows, because a report about a since-changed binding is
    // evidence that binding produced and the panel says which binding it describes.
    fc.assert(
      fc.property(STATE_FROM_INDEPENDENT_FIELDS, (state) => {
        const view = conformanceView(state)
        if (view.situation === 'running') {
          expect(view.checks).toEqual([])
          expect(view.declined).toBe(0)
          expect(view.gaps).toEqual([])
        }
      }),
      { numRuns: 500 },
    )
  })

  it('rejects a view that keeps the earlier run’s rows while a run is in flight', () => {
    // Falsifiability for the property above, in the shape the defect actually took:
    // rows derived from the report's mere presence rather than from the situation.
    expect(() =>
      fc.assert(
        fc.property(STATE_FROM_INDEPENDENT_FIELDS, (state) => {
          const view = conformanceView(state)
          const keptRows = state.report === null ? [] : checkRows(state.report)
          if (view.situation === 'running') expect(keptRows).toEqual([])
        }),
        { numRuns: 500 },
      ),
    ).toThrow()
  })

  it('rejects a renderer that reads the passed flag and nothing else', () => {
    // The falsifiability evidence, kept in the suite rather than in a probe log. If
    // the property above is ever weakened into something a flag-reader satisfies,
    // this test fails and says so.
    expect(() => fc.assert(neverReadsBetter(optimisticReading), { numRuns: 500 })).toThrow()
  })

  it('rejects a renderer that ignores a declared check with no result', () => {
    // The narrower upgrade: everything the engine reported held, and a check it
    // declared was never evaluated. A renderer reading only the results agrees with
    // the honest one on every OTHER payload, which is why it needs its own case.
    const resultsOnly = (state: ConformanceState): Reading => {
      const report = state.report
      if (report === null || state.status !== 'complete' || state.stale || state.is_builtin) {
        return 'no_outcome'
      }
      if (!report.passed || report.results.some((result) => !result.passed)) return 'failed'
      return report.declined_detections > 0 ? 'qualified' : 'passed'
    }
    expect(() => fc.assert(neverReadsBetter(resultsOnly), { numRuns: 500 })).toThrow()
  })

  it('rejects a renderer that reports a declined detection as an unqualified pass', () => {
    const ignoresDeclines = (state: ConformanceState): Reading => {
      const honest = conformanceView(state).reading
      return honest === 'qualified' ? 'passed' : honest
    }
    expect(() => fc.assert(neverReadsBetter(ignoresDeclines), { numRuns: 500 })).toThrow()
  })

  it('rejects a renderer that reports an earlier binding as current', () => {
    const ignoresStale = (state: ConformanceState): Reading => {
      const honest = conformanceView({ ...state, stale: false, is_builtin: false }).reading
      return honest
    }
    expect(() => fc.assert(neverReadsBetter(ignoresStale), { numRuns: 500 })).toThrow()
  })
})
