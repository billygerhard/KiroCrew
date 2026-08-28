/**
 * The conformance check: what it offers, what it says while it runs, and every way
 * it could flatter a provider.
 *
 * An operator who has just bound an external provider has bound something the
 * engine knows nothing about, and the bundled suite is how that stops being true.
 * The suite is also expensive — up to nine child processes per run, with no
 * aggregate deadline — so this surface starts a JOB and polls it, and a panel that
 * waited for a verdict would hold the pane open for minutes.
 *
 * Seven of the claims below are not arrangement:
 *
 * 1. **The check is offered only where something outside the engine is bound.** A
 *    builtin has nothing to check, and the server refuses a run for one, so a panel
 *    beside a builtin row would offer an action the POST declines.
 * 2. **The cost sentence asserts NEITHER free NOR costly.** `provider.nature` is
 *    hardcoded `model_backed` for every external binding because the engine cannot
 *    tell whether an external program reasons, so the only honest sentence about
 *    nine calls to it is that the cost is unknown. Both builtin cost sentences are
 *    asserted ABSENT, so a panel that reached for one would fail here.
 * 3. **A declared check that never ran is a failure OF THE RUN.** Leaving it out
 *    would render a run that evaluated two of five checks as a two-check run that
 *    went fine. The opposite pole is asserted in the same test: the same payload
 *    with the result present reads as a pass.
 * 4. **A declined detection is a QUALIFIED pass.** `declined_detections` does not
 *    enter the engine's `passed` — declining to look is an honest answer — so a
 *    report can carry `passed: true` beside a non-zero count, and an unqualified
 *    pass is exactly the reading that must not be shown. Opposite pole asserted.
 * 5. **No earlier outcome is presented as current.** Three paths, three tests: while
 *    a run is in flight, after the binding changed, and when a start or a read
 *    failed.
 * 6. **The verdict is shown ABOVE every check.** A completed run routinely carries a
 *    green check beside a red verdict — the transport SIGKILLs a provider's child AT
 *    its deadline, so a provider that ignored the deadline still measures as
 *    answering inside the grace period and `timeout_honoring` passes while every
 *    payload-derived check fails. A reader who meets that row first has been told
 *    the opposite of what the run found.
 * 7. **A reported reason is data.** It is engine prose that can quote a provider's
 *    own bytes, so it arrives control-stripped, length-capped, and as a text child.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import SpecEnginePage from '../apps/spec-engine/SpecEnginePage'
import {
  MAX_REASON_CHARS,
  REASON_TRUNCATION_NOTICE,
  checkRows,
  conformanceView,
  pollAfterMs,
  presentedRows,
  reasonText,
} from '../apps/spec-engine/conformanceView'
import type { ConformanceReport, ConformanceState } from '../apps/spec-engine/api'
import en from '../i18n/locales/en.json'

import {
  ENGINE_FLOOR_CAPABILITIES,
  PIPELINE_STAGES,
  TRANSPORTS,
  failure,
  stubSpecEngineFetch,
  type Answer,
  type Call,
  type Responder,
} from './specEngineFetchStub'

const K = en.apps.specEngine.conformance
const B = en.apps.specEngine.capabilityForm
const C = en.apps.specEngine.configPanel
const P = en.apps.specEngine.specEnginePage

/** Every request the page made, so an assertion can read what was sent. */
const calls: Call[] = []

// --- the readings, as pure functions ----------------------------------------

/** A report in the shape `ConformanceReport.to_json_object()` composes. */
function report(over: Partial<ConformanceReport> = {}): ConformanceReport {
  return {
    capability: 'analysis',
    candidate: 'my-analyzer',
    passed: true,
    declared_checks: ['schema_validity', 'repeatability'],
    declared_fixtures: ['minimal-request'],
    gaps: [],
    declined_detections: 0,
    results: [
      {
        check: 'schema_validity',
        fixture: 'minimal-request',
        passed: true,
        detail: 'the response satisfies the published schema',
        excused: 0,
      },
      {
        check: 'repeatability',
        fixture: 'minimal-request',
        passed: true,
        detail: 'the second response matches the first',
        excused: 0,
      },
    ],
    ...over,
  }
}

/** One conformance state in the shape both routes answer. */
function state(over: Partial<ConformanceState> = {}): ConformanceState {
  return {
    capability: 'analysis',
    status: 'complete',
    job_id: 'job-1',
    candidate: 'my-analyzer',
    binding_fingerprint: 'a'.repeat(64),
    binding_current: 'a'.repeat(64),
    stale: false,
    is_builtin: false,
    deadline_s: 10,
    max_invocations: 9,
    error: '',
    report: report(),
    ...over,
  }
}

describe('a verdict is never rosier than the report it came from', () => {
  it('reads a clean report as an unqualified pass', () => {
    expect(conformanceView(state()).reading).toBe('passed')
  })

  it('reads a declared check with no result as a failure of the run', () => {
    // The check is DECLARED and unevaluated. Dropping the row would render this as
    // a one-check run that went fine.
    const missing = state({
      report: report({
        declared_checks: ['schema_validity', 'repeatability', 'timeout_honoring'],
      }),
    })
    const view = conformanceView(missing)
    expect(view.reading).toBe('failed')
    const never = view.checks.filter((row) => row.outcome === 'never_ran')
    expect(never.map((row) => row.check)).toEqual(['timeout_honoring'])
    // Opposite pole, same payload otherwise: with the third result present the very
    // same report reads as a pass, so the failure above is the missing result and
    // not something else about the fixture.
    const complete = state({
      report: report({
        declared_checks: ['schema_validity', 'repeatability', 'timeout_honoring'],
        results: [
          ...report().results,
          {
            check: 'timeout_honoring',
            fixture: 'minimal-request',
            passed: true,
            detail: 'answered inside the deadline',
            excused: 0,
          },
        ],
      }),
    })
    expect(conformanceView(complete).reading).toBe('passed')
  })

  it('reads a pass beside a declined detection as a qualified pass', () => {
    // `declined_detections` does NOT enter the engine's `passed`, so this payload is
    // one the engine really composes: every check passed and nothing was detected.
    const declined = state({
      report: report({
        declined_detections: 1,
        results: report().results.map((result, index) =>
          index === 0 ? { ...result, excused: 1 } : result,
        ),
      }),
    })
    const view = conformanceView(declined)
    expect(view.reading).toBe('qualified')
    expect(view.reading).not.toBe('passed')
    expect(view.declined).toBe(1)
    // Opposite pole: the same shape with nothing declined is the unqualified pass.
    expect(conformanceView(state()).reading).toBe('passed')
  })

  it('takes the engine verdict when the parts disagree with it', () => {
    // A report the engine cannot compose — it derives `passed` from the failures and
    // gaps — so the only question is which of two answers a surface relays. It
    // relays the worse one.
    const optimistic = state({
      report: report({
        passed: true,
        results: report().results.map((result) => ({ ...result, passed: false })),
      }),
    })
    expect(conformanceView(optimistic).reading).toBe('failed')
    const pessimistic = state({ report: report({ passed: false }) })
    expect(conformanceView(pessimistic).reading).toBe('failed')
  })

  it('reads a report with no results at all as no evidence, never a pass', () => {
    // The engine's own first gap. A suite that ran nothing has produced nothing to
    // be reassured by, whatever the flag beside it says.
    const empty = state({
      report: report({ declared_checks: [], results: [], passed: true }),
    })
    expect(conformanceView(empty).reading).not.toBe('passed')
  })

  it('reads every state without a usable report as no outcome', () => {
    for (const over of [
      { status: 'running' as const, report: null },
      { status: 'failed' as const, report: null, error: 'OSError: no temporary directory' },
      { status: 'absent' as const, report: null, job_id: '', binding_fingerprint: '' },
      { status: 'not_applicable' as const, report: null, is_builtin: true, job_id: '' },
      // A finished, PASSING run whose binding has since moved. The report is real
      // and it describes something else.
      { stale: true, binding_current: 'b'.repeat(64) },
      { is_builtin: true },
    ]) {
      const view = conformanceView(state(over))
      expect(view.reading, JSON.stringify(over)).toBe('no_outcome')
    }
    // And the absence of a read is the floor too, rather than a throw.
    expect(conformanceView(undefined).reading).toBe('no_outcome')
    expect(conformanceView(null).situation).toBe('no_outcome')
  })

  it('names the situation apart from the verdict, so copy and claim cannot diverge', () => {
    expect(conformanceView(state({ status: 'running', report: null })).situation).toBe('running')
    expect(conformanceView(state({ status: 'absent', report: null })).situation).toBe('never_run')
    expect(conformanceView(state({ status: 'failed', report: null })).situation).toBe('no_outcome')
    expect(
      conformanceView(state({ status: 'not_applicable', report: null, is_builtin: true })).situation,
    ).toBe('not_applicable')
    expect(conformanceView(state({ stale: true })).situation).toBe('earlier_binding')
    expect(conformanceView(state()).situation).toBe('complete')
  })

  it('offers a start only when the server would accept one', () => {
    // Not from the status: a capability rebound to its builtin after a run still
    // polls `complete`, and the POST refuses it with `builtin_binding`.
    expect(conformanceView(state()).canStart).toBe(true)
    expect(conformanceView(state({ is_builtin: true })).canStart).toBe(false)
    expect(conformanceView(state({ status: 'running', report: null })).canStart).toBe(false)
  })

  it('puts the verdict before every check, in the presented order', () => {
    const rows = presentedRows(conformanceView(state()))
    expect(rows[0].kind).toBe('verdict')
    expect(rows.slice(1).every((row) => row.kind === 'check')).toBe(true)
    // Non-vacuous: there are checks to be above.
    expect(rows.length).toBeGreaterThan(1)
  })
})

describe('a reported reason is treated as hostile text', () => {
  it('strips what is not text however it arrived', () => {
    // A carriage return overwrites the line printed before it; a bidi override
    // reorders what follows. Neither is text, whichever end let it through.
    expect(reasonText('a\r\nb\u202ec')).toBe('abc')
    expect(reasonText('plain')).toBe('plain')
  })

  it('caps the length after stripping, so control bytes cannot spend the cap', () => {
    const long = 'x'.repeat(MAX_REASON_CHARS + 50)
    expect(reasonText(long)).toBe('x'.repeat(MAX_REASON_CHARS) + REASON_TRUNCATION_NOTICE)
    // Stripping first: a string of control characters under the cap arrives whole
    // rather than as an empty row wearing a truncation notice.
    expect(reasonText('\u0001'.repeat(MAX_REASON_CHARS + 50) + 'tail')).toBe('tail')
  })

  it('carries the reason onto every row that has one, and none onto a check that never ran', () => {
    const rows = checkRows(
      report({
        declared_checks: ['schema_validity', 'never'],
        results: [{ ...report().results[0], detail: 'a\u0000reason' }],
      }),
    )
    expect(rows.map((row) => row.reason)).toEqual(['areason', ''])
    expect(rows[1].outcome).toBe('never_ran')
  })

  it('keeps a result for a check the suite never declared, marked as undeclared', () => {
    // Evidence, and a payload this side does not recognise is not one it may edit.
    const rows = checkRows(
      report({
        declared_checks: [],
        results: [report().results[0]],
      }),
    )
    expect(rows).toHaveLength(1)
    expect(rows[0].declared).toBe(false)
  })
})

describe('the poll stops when the run does', () => {
  it('polls only while a run is in flight', () => {
    expect(pollAfterMs(state({ status: 'running', report: null }))).toBeGreaterThan(0)
    for (const status of ['complete', 'failed', 'absent', 'not_applicable'] as const) {
      expect(pollAfterMs(state({ status })), status).toBe(false)
    }
    expect(pollAfterMs(undefined)).toBe(false)
  })
})

// --- the panel, mounted inside the pane -------------------------------------

/** One `/config/capabilities` row. Externally bound unless told otherwise. */
function boundRow(over: Record<string, unknown> = {}) {
  return {
    capability: 'analysis',
    transport: 'command',
    provider: {
      name: 'my-analyzer',
      kind: 'external',
      nature: 'model_backed',
      transport: 'command',
    },
    configured: true,
    declared_at: 'capabilities.analysis',
    timeout_s: 45,
    program: 'my-analyzer',
    reachable: true,
    action: '',
    ...over,
  }
}

/** The same row on its builtin, which is the pole the offer must not reach. */
function builtinRow(capability: string) {
  return {
    capability,
    transport: 'builtin',
    provider: {
      name: 'engine-authoring-turn',
      kind: 'builtin',
      nature: 'model_backed',
      transport: 'builtin',
    },
    configured: false,
    declared_at: '',
    timeout_s: 120,
    program: '',
    reachable: null,
    action: '',
  }
}

/** The three capabilities the engine places in the authoring stage. */
const AUTHORING = ['analysis', 'authoring', 'validation_rules']

function registry() {
  return {
    settings: [],
    source_presets: [],
    profile_presets: [],
    profile_settings: [],
    roles: [],
    efforts: [],
    levels: [],
    stages: PIPELINE_STAGES,
    transports: TRANSPORTS,
    engine_floor: ENGINE_FLOOR_CAPABILITIES,
  }
}

function snapshot() {
  return {
    configured: true,
    path: '/home/me/.kiro/crew/apps/spec-engine/config.json',
    document: { capabilities: { analysis: { transport: 'command', command: ['my-analyzer'] } } },
    elided: [],
    elided_marker: '<elided>',
    errors: [],
    advisories: [],
    config_only_paths: ['capabilities'],
  }
}

/** The authoring stage's panel, whether or not it is the active one. */
function stagePanel(): HTMLElement {
  const tab = screen.getByRole('tab', { name: new RegExp(`^${C.stage_authoring}`) })
  const found = document.getElementById(String(tab.getAttribute('aria-controls')))
  expect(found).not.toBeNull()
  return found as HTMLElement
}

/** One capability's conformance panel, or null when none is offered. */
function conformancePanel(capability: string): HTMLElement | null {
  return stagePanel().querySelector(`[data-conformance="${capability}"]`)
}

/** The panel that must exist, for the tests that are about its content. */
function panelFor(capability: string): HTMLElement {
  const found = conformancePanel(capability)
  expect(found, capability).not.toBeNull()
  return found as HTMLElement
}

/**
 * The start control, once the read that decides whether to offer it has landed.
 *
 * Waited for rather than read straight after mounting: the panel renders a pending
 * branch with no control at all while the poll is in flight, so a click without
 * this races the first read.
 */
async function startButton(capability: string): Promise<HTMLButtonElement> {
  return await waitFor(() => {
    const button = panelFor(capability).querySelector('button')
    expect(button, 'the start control').not.toBeNull()
    return button as HTMLButtonElement
  })
}

async function openStage(
  answers: {
    conformance?: Responder
    conformanceStart?: Answer
    capabilities?: Answer
  } = {},
) {
  stubSpecEngineFetch(
    {
      registry: { body: registry() },
      capabilities:
        answers.capabilities ??
        {
          body: {
            configured: true,
            capabilities: [boundRow(), builtinRow('authoring'), builtinRow('validation_rules')],
          },
        },
      config: { body: snapshot() },
      ...(answers.conformance ? { conformance: answers.conformance } : {}),
      ...(answers.conformanceStart ? { conformanceStart: answers.conformanceStart } : {}),
    },
    { record: calls },
  )
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchInterval: false } },
  })
  render(
    <QueryClientProvider client={client}>
      <SpecEnginePage />
    </QueryClientProvider>,
  )
  fireEvent.click(await screen.findByRole('button', { name: new RegExp(P.configuration) }))
  await screen.findByRole('tablist', { name: C.configuration_stages })
  fireEvent.click(screen.getByRole('tab', { name: new RegExp(`^${C.stage_authoring}`) }))
  await waitFor(() => expect(stagePanel().querySelector('.se-setting')).not.toBeNull())
  return client
}

afterEach(() => {
  vi.unstubAllGlobals()
  calls.length = 0
})

describe('the check is offered where there is something to check', () => {
  it('offers it for an external binding and not for a builtin one', async () => {
    await openStage({ conformance: { body: state({ status: 'absent', report: null }) } })
    await waitFor(() => expect(conformancePanel('analysis')).not.toBeNull())
    // Both poles in one assertion pair: the builtin rows are on the same panel,
    // rendered by the same component, and neither carries the offer.
    expect(conformancePanel('authoring')).toBeNull()
    expect(conformancePanel('validation_rules')).toBeNull()
  })

  it('states the invocation count and asserts neither free nor costly', async () => {
    await openStage({ conformance: { body: state({ status: 'absent', report: null }) } })
    const panel = await waitFor(() => {
      const found = panelFor('analysis')
      expect(found.getAttribute('data-situation')).toBe('never_run')
      return found
    })
    // The count is the engine's, projected: nine for a document capability and two
    // for a non-document one, so a panel holding one figure would state the wrong
    // one for four of the seven.
    expect(panel.textContent).toContain('9')
    expect(panel.textContent).toContain(K.what_those_calls_cost_is_unknown)
    // Neither claim. `provider.nature` is `model_backed` on this very row, so a
    // panel that mapped it onto a cost would say "spends credits" here.
    expect(panel.textContent).not.toContain(B.the_builtin_asks_a_model_so_it_spends_credits)
    expect(panel.textContent).not.toContain(B.the_builtin_asks_no_model_so_it_spends_nothing)
  })

  it('states that nothing is known before a run, rather than nothing being wrong', async () => {
    await openStage({ conformance: { body: state({ status: 'absent', report: null }) } })
    const panel = await waitFor(() => {
      const found = panelFor('analysis')
      expect(found.textContent).toContain(K.this_provider_has_not_been_checked)
      return found
    })
    expect(panel.textContent).not.toContain(K.the_provider_conforms)
  })
})

describe('starting a run answers without waiting for it', () => {
  it('sends the capability and shows the run as started', async () => {
    await openStage({
      conformance: { body: state({ status: 'absent', report: null }) },
      conformanceStart: {
        status: 202,
        body: {
          ok: true,
          ...state({ status: 'running', report: null, job_id: 'job-9' }),
        },
      },
    })
    fireEvent.click(await startButton('analysis'))
    await waitFor(() =>
      expect(panelFor('analysis').textContent).toContain(K.a_check_is_running),
    )
    const started = calls.find(
      (call) => call.method === 'POST' && call.url.includes('/config/conformance'),
    )
    expect(started?.body).toEqual({ capability: 'analysis' })
    // The reply carried `running` and no report, and the panel adopted exactly that
    // — no outcome is invented from a 202.
    expect(panelFor('analysis').getAttribute('data-situation')).toBe('running')
  })

  it('never presents the previous outcome once a new run starts', async () => {
    // THE path. The first read is a finished, PASSING run; the operator starts
    // another; the verdict the panel was showing describes neither the run now in
    // flight nor a binding anybody re-read.
    await openStage({
      conformance: { body: state() },
      conformanceStart: {
        status: 202,
        body: { ok: true, ...state({ status: 'running', report: null }) },
      },
    })
    await waitFor(() =>
      expect(panelFor('analysis').textContent).toContain(K.the_provider_conforms),
    )
    fireEvent.click(await startButton('analysis'))
    await waitFor(() =>
      expect(panelFor('analysis').textContent).toContain(K.a_check_is_running),
    )
    const panel = panelFor('analysis')
    expect(panel.textContent).not.toContain(K.the_provider_conforms)
    expect(panel.querySelector('[data-reading]')).toBeNull()
    expect(panel.querySelector('[data-outcome]')).toBeNull()
    expect(panel.textContent).toContain(K.no_earlier_outcome_is_shown_while_running)
  })

  it('relays the reason a second concurrent run is refused, and claims no outcome', async () => {
    await openStage({
      conformance: { body: state({ status: 'absent', report: null }) },
      conformanceStart: failure(
        409,
        'conformance_running',
        'a conformance run for analysis is already in progress as job job-7; it ' +
          'invokes the configured provider up to nine times, so a second run would ' +
          'double that load against one program',
      ),
    })
    fireEvent.click(await startButton('analysis'))
    await waitFor(() =>
      expect(panelFor('analysis').textContent).toContain(K.could_not_start_the_check),
    )
    const panel = panelFor('analysis')
    // The server's own reason, which names the load a second run would put on one
    // program. Reworded here it would name nothing.
    expect(panel.textContent).toContain('already in progress as job job-7')
    expect(panel.textContent).toContain('conformance_running')
    // A start that did not happen produced nothing, and nothing is not a pass.
    expect(panel.textContent).toContain(K.no_outcome_was_obtained)
    expect(panel.textContent).not.toContain(K.the_provider_conforms)
  })

  it('states that no outcome was obtained when a start fails for any other reason', async () => {
    await openStage({
      conformance: { body: state({ status: 'absent', report: null }) },
      conformanceStart: failure(503, 'config_unreadable', 'the configuration could not be read'),
    })
    fireEvent.click(await startButton('analysis'))
    await waitFor(() =>
      expect(panelFor('analysis').textContent).toContain(K.no_outcome_was_obtained),
    )
    expect(panelFor('analysis').querySelector('[data-reading]')).toBeNull()
  })
})

describe('a completed run is presented verdict first', () => {
  it('shows the run verdict above every individual check', async () => {
    // The real shape of the case this ordering exists for: the run FAILED, and one
    // check inside it passed. The transport SIGKILLs a provider's child at its
    // deadline, so `timeout_honoring` passes while every payload-derived check
    // fails — a reader who meets the green row first has it backwards.
    await openStage({
      conformance: {
        body: state({
          report: report({
            passed: false,
            declared_checks: ['timeout_honoring', 'schema_validity'],
            results: [
              {
                check: 'timeout_honoring',
                fixture: 'minimal-request',
                passed: true,
                detail: 'answered within the deadline and its grace period',
                excused: 0,
              },
              {
                check: 'schema_validity',
                fixture: 'minimal-request',
                passed: false,
                detail: 'the candidate raised TransportFailure',
                excused: 0,
              },
            ],
          }),
        }),
      },
    })
    const panel = await waitFor(() => panelFor('analysis'))
    const ordered = [...panel.querySelectorAll('[data-reading],[data-outcome]')]
    expect(ordered.length).toBe(3)
    expect(ordered[0].getAttribute('data-reading')).toBe('failed')
    expect(ordered.slice(1).map((node) => node.getAttribute('data-outcome'))).toEqual([
      'passed',
      'failed',
    ])
    // Each check with its OWN outcome rather than one pass or fail for the run.
    expect(panel.textContent).toContain(K.this_check_held)
    expect(panel.textContent).toContain(K.this_check_failed)
    expect(panel.textContent).toContain(K.the_provider_does_not_conform)
  })

  it('shows a declared check that never ran as a failure of the run', async () => {
    await openStage({
      conformance: {
        body: state({
          report: report({
            passed: false,
            declared_checks: ['schema_validity', 'repeatability', 'planted_defect'],
            gaps: ["check 'planted_defect' was declared but never evaluated"],
          }),
        }),
      },
    })
    const panel = await waitFor(() => panelFor('analysis'))
    const row = panel.querySelector('[data-check="planted_defect"]')
    expect(row).not.toBeNull()
    expect(row?.getAttribute('data-outcome')).toBe('never_ran')
    expect(row?.textContent).toContain(K.this_check_never_ran)
    // And the run's verdict is a failure, not a two-check pass with a gap beside it.
    expect(panel.querySelector('[data-reading]')?.getAttribute('data-reading')).toBe('failed')
    // The engine's own words for what it could not speak for, relayed.
    expect(panel.textContent).toContain("check 'planted_defect' was declared but never evaluated")
  })

  it('qualifies a pass whose detections were declined', async () => {
    await openStage({
      conformance: {
        body: state({
          report: report({
            declined_detections: 2,
            results: report().results.map((result) => ({ ...result, excused: 1 })),
          }),
        }),
      },
    })
    const panel = await waitFor(() => panelFor('analysis'))
    expect(panel.querySelector('[data-reading]')?.getAttribute('data-reading')).toBe('qualified')
    expect(panel.textContent).toContain(K.the_provider_conforms_with_a_qualification)
    expect(panel.textContent).toContain('2')
    // The unqualified sentence is the one this case must not produce.
    expect(panel.textContent).not.toContain(K.the_provider_conforms)
    expect(panel.textContent).toContain(K.this_check_held_by_declining)
  })

  it('renders a reported reason as capped text and never as markup', async () => {
    const hostile = `<img src=x onerror="alert(1)">${'y'.repeat(MAX_REASON_CHARS)}`
    await openStage({
      conformance: {
        body: state({
          report: report({
            passed: false,
            declared_checks: ['schema_validity'],
            results: [
              {
                check: 'schema_validity',
                fixture: 'malformed-response',
                passed: false,
                detail: hostile,
                excused: 0,
              },
            ],
          }),
        }),
      },
    })
    const panel = await waitFor(() => panelFor('analysis'))
    const reason = panel.querySelector('[data-reason="schema_validity"]') as HTMLElement
    expect(reason).not.toBeNull()
    // Text, not markup: the tag is in the text content and no element was created.
    expect(reason.textContent).toContain('<img')
    expect(reason.querySelector('img')).toBeNull()
    // Capped, with the cut marked rather than silent.
    expect(reason.textContent?.length).toBe(MAX_REASON_CHARS + REASON_TRUNCATION_NOTICE.length)
    expect(reason.textContent?.endsWith(REASON_TRUNCATION_NOTICE)).toBe(true)
  })
})

describe('an outcome that no longer describes the binding is not presented as current', () => {
  it('stops presenting a verdict once the binding has changed', async () => {
    await openStage({
      conformance: {
        body: state({ stale: true, binding_current: 'b'.repeat(64) }),
      },
    })
    const panel = await waitFor(() => panelFor('analysis'))
    expect(panel.getAttribute('data-situation')).toBe('earlier_binding')
    expect(panel.textContent).toContain(K.the_outcome_describes_an_earlier_binding)
    // The report was a PASS. It is still relayed — it is evidence about a binding
    // that existed — and it is no longer a verdict about this one.
    expect(panel.querySelector('[data-reading]')?.getAttribute('data-reading')).toBe('no_outcome')
    expect(panel.textContent).not.toContain(K.the_provider_conforms)
  })

  it('offers no run for a capability the server would refuse, whatever its status says', async () => {
    // The state `status` alone cannot describe: rebound to its builtin AFTER a run,
    // so it polls `complete` while the POST answers `builtin_binding`. The row is
    // still external in this fixture, which is what keeps the panel mounted.
    await openStage({
      conformance: {
        body: state({ is_builtin: true, stale: true, binding_current: 'b'.repeat(64) }),
      },
    })
    const panel = await waitFor(() => panelFor('analysis'))
    expect((panel.querySelector('button') as HTMLButtonElement).disabled).toBe(true)
  })

  it('states a failed read and never presents the retained outcome as current', async () => {
    // React Query keeps the last successful body across a failing refetch, so a
    // panel reading `data` alone would go on showing this verdict after the read
    // that would have replaced it failed.
    const client = await openStage({
      conformance: [
        { body: state() },
        failure(503, 'config_unreadable', 'the configuration could not be read'),
      ],
    })
    await waitFor(() =>
      expect(panelFor('analysis').textContent).toContain(K.the_provider_conforms),
    )
    await client.invalidateQueries({ queryKey: ['spec-engine', 'config', 'conformance'] })
    await waitFor(() =>
      expect(panelFor('analysis').textContent).toContain(K.could_not_read_the_check_state),
    )
    const panel = panelFor('analysis')
    expect(panel.textContent).not.toContain(K.the_provider_conforms)
    expect(panel.textContent).toContain(K.a_failed_read_is_not_an_outcome)
    expect(panel.querySelector('[data-reading]')).toBeNull()
  })

  it('reports a run that could not be carried out as no outcome, with its reason', async () => {
    await openStage({
      conformance: {
        body: state({
          status: 'failed',
          report: null,
          error: 'OSError: no room for a temporary directory',
        }),
      },
    })
    const panel = await waitFor(() => panelFor('analysis'))
    expect(panel.getAttribute('data-situation')).toBe('no_outcome')
    expect(panel.textContent).toContain(K.no_outcome_was_obtained)
    expect(panel.textContent).toContain('OSError: no room for a temporary directory')
    expect(panel.textContent).not.toContain(K.the_provider_conforms)
  })
})
