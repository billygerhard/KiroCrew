/**
 * The kill switch and the per-run spend block.
 *
 * The properties asserted here are the ones whose loss is silent, and the first is
 * the whole point of the panel:
 *
 *   - **A 200 is not a confirmation.** Engage and release are reported against the
 *     flag READ BACK afterwards. A reply that says the switch moved while the
 *     read-back says it did not is the shape every half-landed write produces —
 *     the flag is a file, and a stale read, a second writer or a filesystem that
 *     acknowledged a write it did not keep all arrive as exactly that. It must read
 *     as unconfirmed, and a read-back that FAILED must read as unknown rather than
 *     as either outcome.
 *   - **Doubt never reads as released.** A pending or failed read leaves the dot in
 *     its own third state, because the engine's own reader treats an unreadable
 *     flag as engaged. The previous strip had two states, so a failed read showed a
 *     green dot beside text saying the switch could not be read.
 *   - **A release is not offered for a state nobody read.** Releasing is a claim
 *     about what is in force. Engaging stays available in every reading, since
 *     stopping is the fail-closed direction.
 *   - **A stop records why.** The handler only refuses a non-string reason, and the
 *     engine keeps the FIRST engage's record forever, so an empty reason is a
 *     permanent unanswered question and is refused here instead.
 *   - **Spend is bound to the selected row.** The mockup's inspector was static
 *     below its header, so the budget-parked run showed the first run's figures.
 *   - **The strip's figures are unknown until they are read.** Zero is a claim.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import SpecEnginePage from '../apps/spec-engine/SpecEnginePage'
import { QK } from '../apps/spec-engine/api'
import { confirmsIntent, switchReading } from '../apps/spec-engine/SafetyPanel'
import { SE_CSS } from '../apps/spec-engine/styles'
import en from '../i18n/locales/en.json'

const T = en.apps.specEngine.safetyPanel
const P = en.apps.specEngine.specEnginePage

type Answer = { status?: number; body: unknown }

/** Every request the page made, so an assertion can read what was sent. */
const calls: Array<{ url: string; method: string; body: unknown }> = []

/** One queue row, with only the fields a caller overrides spelled out. */
function entry(over: Record<string, unknown> = {}) {
  return {
    run_id: 'run_8f2a41',
    project: '/home/me/src/checkout-svc',
    spec: 'idempotent-refunds',
    spec_type: 'feature',
    state: 'awaiting_review',
    waiting_on: 'review',
    entered_ts: '2026-08-17T10:00:00Z',
    waiting_s: 8040,
    source: null,
    item_id: null,
    cost_credits: 163.2,
    gate: 'design',
    revision_exhausted: false,
    feedback_quarantined: 0,
    feedback_needs_human: false,
    analysis: [],
    ...over,
  }
}

/** The kill-switch flag, in `KillSwitchState.to_json_object`'s shape. */
function switchState(over: Record<string, unknown> = {}) {
  return {
    engaged: false,
    initiator: '',
    reason: '',
    engaged_ts: '',
    unreadable: false,
    description: '',
    ...over,
  }
}

/** The kill-switch read, in `_kill_switch_snapshot`'s shape. */
function snapshot(over: Record<string, unknown> = {}, stoppable: unknown[] = []) {
  return {
    switch: switchState(over),
    stoppable,
    stoppable_credits: stoppable.length === 0 ? 0 : 412.6,
  }
}

/** One run's spend, in `_run_spend`'s shape. */
function spend(over: Record<string, unknown> = {}) {
  return {
    run_id: 'run_8f2a41',
    project: '/home/me/src/checkout-svc',
    spec: 'idempotent-refunds',
    state: 'awaiting_review',
    source: '',
    credits: 163.2,
    metered_credits: 163.2,
    declared_credits: 0,
    turns: 41,
    sessions: 3,
    recorded_credits: 163.2,
    ceiling: { value: 600, origin: 'app_config', declared_at: 'budget.run_ceiling_credits' },
    ...over,
  }
}

const NEVER: Answer = { body: '__never__' }

/**
 * Answer each route independently.
 *
 * `killSwitch` is a QUEUE rather than one answer, because the confirmation this
 * suite is about is a SECOND read of the same route: the interesting cases are the
 * ones where the read after the write disagrees with the reply to it. The last
 * entry sticks, so a test that only cares about the steady state passes one.
 */
function stub(answers: {
  queue?: Answer
  config?: Answer
  killSwitch?: Answer[]
  post?: Answer
  runSpend?: Record<string, Answer>
}) {
  const killSwitch = [...(answers.killSwitch ?? [{ body: snapshot() }])]
  vi.stubGlobal(
    'fetch',
    vi.fn((url: string, init?: RequestInit) => {
      const method = init?.method ?? 'GET'
      calls.push({ url, method, body: init?.body ? JSON.parse(String(init.body)) : undefined })
      let answer: Answer
      if (url.startsWith('/api/apps/spec-engine/kill-switch')) {
        answer =
          method === 'POST'
            ? (answers.post ?? { body: { ok: true, action: 'engage', switch: switchState() } })
            : (killSwitch.length > 1 ? killSwitch.shift()! : killSwitch[0])
      } else if (url.startsWith('/api/apps/spec-engine/run-spend')) {
        const runId = new URL(url, 'http://x').searchParams.get('run_id') ?? ''
        answer = answers.runSpend?.[runId] ?? { body: spend({ run_id: runId }) }
      } else if (url.startsWith('/api/apps/spec-engine/config/registry')) {
        // The configuration pane's settings form is generated from this read, and
        // it must be answered BEFORE the generic '/config' prefix below, which
        // would otherwise hand it a ConfigSnapshot and crash its render.
        answer = {
          body: { settings: [], source_presets: [], profile_presets: [], roles: [], levels: [] },
        }
      } else if (url.startsWith('/api/apps/spec-engine/config')) {
        answer = answers.config ?? {
          body: { configured: true, document: { projects: { acme: {} } }, elided: [] },
        }
      } else {
        answer = answers.queue ?? { body: { entries: [], grouped: {}, total: 0, total_credits: 0 } }
      }
      // A read that never settles is how the PENDING state is exercised: it is a
      // real state of the surface, and it is the one that used to render zeros.
      if (answer === NEVER) return new Promise(() => {})
      const status = answer.status ?? 200
      return Promise.resolve({
        ok: status >= 200 && status < 300,
        status,
        text: () => Promise.resolve(JSON.stringify(answer.body)),
      })
    }),
  )
}

/** The client the last render used, so a test can read what the cache is keyed by. */
let client: QueryClient

function renderPage() {
  client = new QueryClient({
    defaultOptions: {
      queries: { retry: false, refetchInterval: false },
      mutations: { retry: false },
    },
  })
  return render(
    <QueryClientProvider client={client}>
      <SpecEnginePage />
    </QueryClientProvider>,
  )
}

/** GETs of the kill-switch route, which is what a read-back adds one of. */
const switchReads = () =>
  calls.filter((c) => c.method === 'GET' && c.url.startsWith('/api/apps/spec-engine/kill-switch'))

const postedSwitch = () =>
  calls.find((c) => c.method === 'POST' && c.url.startsWith('/api/apps/spec-engine/kill-switch'))

afterEach(() => {
  vi.unstubAllGlobals()
  calls.length = 0
})

describe('the reading rule', () => {
  it('reads an absent state as unknown rather than as released', () => {
    // Pending and failed both arrive here as `undefined`, and the whole defect this
    // fixes is that the two-state version treated everything that was not
    // `engaged === true` as released.
    expect(switchReading(undefined)).toBe('unknown')
    expect(switchReading(switchState({ engaged: false }))).toBe('released')
    expect(switchReading(switchState({ engaged: true }))).toBe('engaged')
    // Doubt reads engaged: the engine's own reader sets both fields that way.
    expect(switchReading(switchState({ engaged: true, unreadable: true }))).toBe('engaged')
  })

  it('confirms an action only against a state that says it happened', () => {
    expect(confirmsIntent('engage', switchState({ engaged: true }))).toBe(true)
    expect(confirmsIntent('engage', switchState({ engaged: false }))).toBe(false)
    expect(confirmsIntent('release', switchState({ engaged: false }))).toBe(true)
    expect(confirmsIntent('release', switchState({ engaged: true }))).toBe(false)
    // A release that left an unparseable record behind is not a release.
    expect(confirmsIntent('release', switchState({ engaged: true, unreadable: true }))).toBe(false)
    // No state at all is the read-back that failed. It must not default to yes.
    expect(confirmsIntent('engage', undefined)).toBe(false)
    expect(confirmsIntent('release', undefined)).toBe(false)
  })
})

describe('the indicator', () => {
  const dot = (container: HTMLElement) => container.querySelector('.se-ks-dot')

  it('does not show a released dot when the switch could not be read', async () => {
    stub({ killSwitch: [{ status: 503, body: { code: 'kill_switch_unreadable', error: 'no db' } }] })
    const { container } = renderPage()
    await screen.findByText(P.could_not_read_the_kill_switch)
    // The text said "could not be read" before this and the dot stayed green
    // beside it, so the two channels contradicted each other.
    expect(dot(container)).toHaveAttribute('data-state', 'unknown')
  })

  it('does not show a released dot before the first read lands', async () => {
    stub({ killSwitch: [NEVER] })
    const { container } = renderPage()
    await screen.findByText(P.reading_the_kill_switch)
    expect(dot(container)).toHaveAttribute('data-state', 'unknown')
  })

  it('marks the three readings apart in the stylesheet', () => {
    const css = SE_CSS.replace(/\s+/g, '')
    // Released is the only one that may be the healthy colour. If `unknown` ever
    // resolves onto it again, the fixed test above would still pass on the
    // attribute while the pixel went back to green.
    expect(css).toContain('.se-ks-dot{width:7px;height:7px;border-radius:50%;background:var(--ok)')
    expect(css).toContain('.se-ks-dot[data-state="engaged"]{background:var(--danger)}')
    expect(css).toContain('.se-ks-dot[data-state="unknown"]{background:transparent')
    expect(css).not.toContain('.se-ks-dot[data-state="unknown"]{background:var(--ok)')
  })
})

describe('the strip while its reads are pending', () => {
  it('does not render zero for a spend it has not read', async () => {
    stub({ queue: NEVER })
    const { container } = renderPage()
    const strip = await waitFor(() => {
      const found = container.querySelector('.se-status')
      expect(found).not.toBeNull()
      return found!
    })
    expect(strip.querySelector('[data-strip-pending="queue"]')).toHaveTextContent(P.reading_the_queue)
    // Both figures are queue-derived, so neither label may appear with a zero
    // beside it while the read is still in flight.
    expect(strip.textContent).not.toContain(P.spend_on_waiting_runs)
    expect(strip.textContent).not.toContain(P.waiting_on_a_person)
  })

  it('holds the work area until the configuration read decides the pane', async () => {
    // Without this branch an unconfigured engine flashed the run list before
    // switching to setup, because `null` fell through to the queue pane.
    stub({ config: NEVER })
    const { container } = renderPage()
    await screen.findByText(P.reading_the_configuration)
    expect(container.querySelector('[data-pane-pending="true"]')).not.toBeNull()
    expect(screen.queryByRole('grid')).toBeNull()
    expect(screen.queryByText(P.nothing_is_configured_yet)).toBeNull()
  })
})

describe('what the control offers', () => {
  it('offers a release when the switch reads engaged', async () => {
    stub({ killSwitch: [{ body: snapshot({ engaged: true, initiator: 'me', reason: 'stop' }) }] })
    renderPage()
    // Awaited on the READING, not on a button: the engage control also renders
    // while the read is pending, so asserting on it first would let a pending
    // strip satisfy an assertion about a settled one.
    await screen.findByText(P.kill_switch_engaged)
    expect(screen.getByRole('button', { name: T.release_the_kill_switch })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: T.engage_the_kill_switch })).toBeNull()
  })

  it('offers an engage, and no release, when the switch could not be read', async () => {
    stub({ killSwitch: [{ status: 503, body: { code: 'kill_switch_unreadable', error: 'no db' } }] })
    renderPage()
    await screen.findByText(P.could_not_read_the_kill_switch)
    // Stopping is the fail-closed direction, so it stays available. Releasing is a
    // claim about what is in force, and nothing here knows what is.
    expect(screen.getByRole('button', { name: T.engage_the_kill_switch })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: T.release_the_kill_switch })).toBeNull()
    expect(screen.getByRole('button', { name: T.read_the_switch_again })).toBeInTheDocument()
  })

  it('offers no re-read while the first read is still in flight', async () => {
    // The engage stays offered even here, because a pending read is doubt and
    // stopping is what doubt licenses. Re-reading a read that has not finished is
    // not, so that control waits for the first one to settle.
    stub({ killSwitch: [NEVER] })
    renderPage()
    await screen.findByText(P.reading_the_kill_switch)
    expect(screen.getByRole('button', { name: T.engage_the_kill_switch })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: T.read_the_switch_again })).toBeNull()
  })
})

describe('engaging', () => {
  const stoppableRun = {
    run_id: 'run_1',
    spec_key: 'k',
    source: '',
    state: 'authoring',
    cost_credits: 200,
  }

  async function arm(answers: Parameters<typeof stub>[0] = {}) {
    stub({ killSwitch: [{ body: snapshot({}, [stoppableRun, stoppableRun]) }], ...answers })
    const rendered = renderPage()
    // The reading first: the engage control is offered while the read is still in
    // flight too, and arming from that moment would arm without a blast radius.
    await screen.findByText(P.kill_switch_released)
    fireEvent.click(screen.getByRole('button', { name: T.engage_the_kill_switch }))
    return rendered
  }

  it('names its own blast radius before it fires', async () => {
    await arm()
    // The route's `stoppable`, not the queue's rows: a stop reaches every run that
    // is neither finished nor already parked, and the queue holds only the ones
    // waiting on a person.
    expect(
      await screen.findByText(/2.*412\.6/),
    ).toBeInTheDocument()
  })

  it('stops quoting a blast radius the last read left behind', async () => {
    // The retained-data hazard on the figures rather than on the dot. The cache
    // keeps the previous payload across a failed refetch, so `read.data` is still
    // there after a read that failed — and a count quoted from it reads as the
    // reach of the stop the operator is about to throw. It has to fall back to the
    // doubt sentence, the same way the dot does.
    await arm()
    await screen.findByText(/2.*412\.6/)
    stub({ killSwitch: [{ status: 503, body: { code: 'kill_switch_unreadable', error: 'no db' } }] })
    await client.refetchQueries({ queryKey: QK.killSwitch })

    expect(await screen.findByText(T.the_blast_radius_could_not_be_read)).toBeInTheDocument()
    expect(screen.queryByText(/2.*412\.6/)).toBeNull()
    // The engage pane itself stays: stopping is the fail-closed direction, so doubt
    // licenses it. Only the figures beside it were retracted.
    expect(screen.getByRole('button', { name: T.confirm_the_stop })).toBeInTheDocument()
  })

  it('refuses to send a stop with no reason, and sends nothing', async () => {
    await arm()
    fireEvent.click(await screen.findByRole('button', { name: T.confirm_the_stop }))
    expect(await screen.findByText(T.a_stop_must_record_why)).toBeInTheDocument()
    expect(postedSwitch()).toBeUndefined()
  })

  it('reads the flag back after the write and reports the persisted state', async () => {
    await arm({
      killSwitch: [
        { body: snapshot({}, [stoppableRun]) },
        { body: snapshot({ engaged: true, initiator: 'me', reason: 'runaway' }) },
      ],
      post: {
        body: {
          ok: true,
          action: 'engage',
          switch: switchState({ engaged: true, initiator: 'me', reason: 'runaway' }),
          halted: [{ run_id: 'run_1', parked: true, cost_credits: 200 }],
          total_credits: 200,
        },
      },
    })
    fireEvent.change(screen.getByLabelText(T.why_the_engine_is_being_stopped), {
      target: { value: 'runaway' },
    })
    fireEvent.click(screen.getByRole('button', { name: T.confirm_the_stop }))

    expect(await screen.findByText(T.the_stop_is_in_force)).toBeInTheDocument()
    expect(postedSwitch()?.body).toEqual({ action: 'engage', reason: 'runaway' })
    // Two reads: the one that rendered the strip, and the one that confirmed the
    // write. The second is the whole property — a confirmation derived from the
    // reply alone would need only the first.
    await waitFor(() => expect(switchReads().length).toBeGreaterThanOrEqual(2))
    // The other half of the same-cache-entry claim: the STRIP's own indicator
    // flips, because the confirming read landed in the entry the strip renders.
    // A private fetch would leave the verdict right and the dot green.
    await waitFor(() => {
      const stripDot = document.querySelector('.se-status .se-ks-dot')
      expect(stripDot).toHaveAttribute('data-state', 'engaged')
    })
    expect(
      screen.getByText(en.apps.specEngine.specEnginePage.kill_switch_engaged),
    ).toBeInTheDocument()
  })

  it('treats a 200 whose read-back still shows the old state as NOT confirmed', async () => {
    // The deceptive shape. The reply is as confident as a successful one; only the
    // flag knows, and the flag says nothing was stopped.
    await arm({
      killSwitch: [
        { body: snapshot({}, [stoppableRun]) },
        { body: snapshot({ engaged: false }) },
      ],
      post: {
        body: {
          ok: true,
          action: 'engage',
          switch: switchState({ engaged: true }),
          halted: [],
          total_credits: 0,
        },
      },
    })
    fireEvent.change(screen.getByLabelText(T.why_the_engine_is_being_stopped), {
      target: { value: 'runaway' },
    })
    fireEvent.click(screen.getByRole('button', { name: T.confirm_the_stop }))

    expect(await screen.findByText(T.not_confirmed)).toBeInTheDocument()
    expect(screen.getByText(T.the_flag_still_reads_released)).toBeInTheDocument()
    expect(screen.queryByText(T.the_stop_is_in_force)).toBeNull()
  })

  it('is not confirmed by a reply that itself reports the old state', async () => {
    await arm({
      killSwitch: [
        { body: snapshot({}, [stoppableRun]) },
        { body: snapshot({ engaged: true }) },
      ],
      // The other half of the same rule: the handler's own reply is the first place
      // a half-landed write shows up, so it is checked as well as the read-back.
      post: { body: { ok: true, action: 'engage', switch: switchState({ engaged: false }) } },
    })
    fireEvent.change(screen.getByLabelText(T.why_the_engine_is_being_stopped), {
      target: { value: 'runaway' },
    })
    fireEvent.click(screen.getByRole('button', { name: T.confirm_the_stop }))

    expect(await screen.findByText(T.not_confirmed)).toBeInTheDocument()
    expect(screen.queryByText(T.the_stop_is_in_force)).toBeNull()
    // The sentence names the state the READ-BACK found — engaged — never the
    // one the requested action implies. Deriving it from the action printed
    // "still reads released, so nothing is stopped" beside a flag and a strip
    // both showing engaged: a false statement in the unsafe direction, in the
    // exact branch built to catch half-landed writes.
    expect(screen.getByText(T.the_flag_still_reads_engaged)).toBeInTheDocument()
    expect(screen.queryByText(T.the_flag_still_reads_released)).toBeNull()
  })

  it('reports a failed read-back as unknown rather than as either outcome', async () => {
    await arm({
      killSwitch: [
        { body: snapshot({}, [stoppableRun]) },
        { status: 503, body: { code: 'kill_switch_unreadable', error: 'no db' } },
      ],
      post: { body: { ok: true, action: 'engage', switch: switchState({ engaged: true }) } },
    })
    fireEvent.change(screen.getByLabelText(T.why_the_engine_is_being_stopped), {
      target: { value: 'runaway' },
    })
    fireEvent.click(screen.getByRole('button', { name: T.confirm_the_stop }))

    expect(await screen.findByText(T.the_switch_could_not_be_read_back)).toBeInTheDocument()
    // Neither a success nor a failure: the write may well have landed, and saying
    // it failed would be as wrong as saying it worked.
    expect(screen.queryByText(T.the_stop_is_in_force)).toBeNull()
    expect(screen.queryByText(T.the_stop_was_refused)).toBeNull()
  })

  it('shows doubt on the dot, not the last reading, when the read-back failed', async () => {
    // React Query keeps the previous data across a failed refetch, so the strip
    // still HOLDS the released state the first read found when the read-back
    // 503s. The dot must render the doubt the text states — not the retained
    // reading — and the re-read control must be offered: this is the one state
    // whose own words tell the operator to read the switch again.
    const { container } = await arm({
      killSwitch: [
        { body: snapshot({}, [stoppableRun]) },
        { status: 503, body: { code: 'kill_switch_unreadable', error: 'no db' } },
      ],
      post: { body: { ok: true, action: 'engage', switch: switchState({ engaged: true }) } },
    })
    fireEvent.change(screen.getByLabelText(T.why_the_engine_is_being_stopped), {
      target: { value: 'runaway' },
    })
    fireEvent.click(screen.getByRole('button', { name: T.confirm_the_stop }))

    await screen.findByText(T.the_switch_could_not_be_read_back)
    expect(container.querySelector('.se-status .se-ks-text')).toHaveTextContent(
      P.could_not_read_the_kill_switch,
    )
    expect(container.querySelector('.se-status .se-ks-dot')).toHaveAttribute(
      'data-state',
      'unknown',
    )
    expect(screen.getByRole('button', { name: T.read_the_switch_again })).toBeInTheDocument()
  })

  it('counts only the runs the engine parked, with their own credits', async () => {
    await arm({
      killSwitch: [
        { body: snapshot({}, [stoppableRun]) },
        { body: snapshot({ engaged: true }) },
      ],
      post: {
        body: {
          ok: true,
          action: 'engage',
          switch: switchState({ engaged: true }),
          // One run parked, one the engine could NOT move — another writer held
          // the spec. Both are stopped, in that no further turn may open, but the
          // sentence says "parked": it must not count the second run, and its
          // credits must be the parked run's own rather than the total across both.
          halted: [
            { run_id: 'run_1', parked: true, cost_credits: 200 },
            { run_id: 'run_2', parked: false, cost_credits: 55 },
          ],
          total_credits: 255,
        },
      },
    })
    fireEvent.change(screen.getByLabelText(T.why_the_engine_is_being_stopped), {
      target: { value: 'runaway' },
    })
    fireEvent.click(screen.getByRole('button', { name: T.confirm_the_stop }))

    await screen.findByText(T.the_stop_is_in_force)
    const note = screen.getByText(/Runs parked/)
    expect(note).toHaveTextContent('Runs parked: 1')
    expect(note).toHaveTextContent('200')
    expect(note.textContent).not.toContain('255')
  })

  it('states the refusal when the stop is refused', async () => {
    await arm({
      post: { status: 503, body: { code: 'engage_failed', error: 'cannot persist the flag' } },
    })
    fireEvent.change(screen.getByLabelText(T.why_the_engine_is_being_stopped), {
      target: { value: 'runaway' },
    })
    fireEvent.click(screen.getByRole('button', { name: T.confirm_the_stop }))

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent(T.the_stop_was_refused)
    expect(alert).toHaveTextContent('engage_failed')
  })
})

describe('releasing', () => {
  async function armRelease(over: Record<string, unknown>, answers: Parameters<typeof stub>[0] = {}) {
    stub({ killSwitch: [{ body: snapshot(over) }], ...answers })
    const rendered = renderPage()
    await screen.findByText(P.kill_switch_engaged)
    fireEvent.click(screen.getByRole('button', { name: T.release_the_kill_switch }))
    return rendered
  }

  it('shows the decision being overridden, and whose session records the release', async () => {
    await armRelease({
      engaged: true,
      initiator: 'billy',
      reason: 'runaway watch loop',
      engaged_ts: '2026-08-17T10:00:00Z',
    })
    expect(await screen.findByText(/billy/)).toBeInTheDocument()
    expect(screen.getByText(/runaway watch loop/)).toBeInTheDocument()
    // The initiator is the session, never a typed name: the handler attributes
    // both directions to the authenticated caller.
    expect(screen.getByText(T.recorded_against_your_session)).toBeInTheDocument()
    expect(screen.queryByLabelText(T.why_the_engine_is_being_stopped)).toBeNull()
  })

  it('calls a stop nobody chose a repair rather than a decision', async () => {
    await armRelease({ engaged: true, unreadable: true })
    expect(await screen.findByText(T.releasing_an_unreadable_stop_is_a_repair)).toBeInTheDocument()
    expect(screen.queryByText(T.no_reason_was_recorded)).toBeNull()
  })

  it('confirms a release from the flag, and sends no reason with it', async () => {
    await armRelease(
      { engaged: true, initiator: 'billy' },
      {
        killSwitch: [
          { body: snapshot({ engaged: true, initiator: 'billy' }) },
          { body: snapshot({ engaged: false }) },
        ],
        post: {
          body: {
            ok: true,
            action: 'release',
            changed: true,
            switch: switchState({ engaged: false }),
            resumed: [],
          },
        },
      },
    )
    fireEvent.click(screen.getByRole('button', { name: T.confirm_the_release }))

    expect(await screen.findByText(T.new_work_may_start_again)).toBeInTheDocument()
    // No `reason` field: it belongs to the engage record, and sending a stale one
    // would attach an explanation to the wrong direction.
    expect(postedSwitch()?.body).toEqual({ action: 'release' })
  })

  it('treats a 200 whose read-back still shows the stop as NOT confirmed', async () => {
    await armRelease(
      { engaged: true },
      {
        killSwitch: [
          { body: snapshot({ engaged: true }) },
          { body: snapshot({ engaged: true }) },
        ],
        post: {
          body: {
            ok: true,
            action: 'release',
            changed: true,
            switch: switchState({ engaged: false }),
            resumed: [],
          },
        },
      },
    )
    fireEvent.click(screen.getByRole('button', { name: T.confirm_the_release }))

    expect(await screen.findByText(T.not_confirmed)).toBeInTheDocument()
    expect(screen.getByText(T.the_flag_still_reads_engaged)).toBeInTheDocument()
    expect(screen.queryByText(T.new_work_may_start_again)).toBeNull()
  })

  it('stops asserting the stop on the strip when the read-back failed', async () => {
    // The retained-data hazard in the other direction: the strip was tinted by an
    // engaged read, the release's read-back failed, and the cache still holds
    // "engaged". The tint is a positive claim that a stop is in force, and after
    // that failed read nothing on this surface knows what is — so the tint must
    // drop and the dot must show doubt, rather than keep rendering the reading
    // the failed read left behind.
    const { container } = await armRelease(
      { engaged: true },
      {
        killSwitch: [
          { body: snapshot({ engaged: true }) },
          { status: 503, body: { code: 'kill_switch_unreadable', error: 'no db' } },
        ],
        post: {
          body: {
            ok: true,
            action: 'release',
            changed: true,
            switch: switchState({ engaged: false }),
            resumed: [],
          },
        },
      },
    )
    const strip = container.querySelector('.se-status')
    expect(strip).toHaveAttribute('data-engaged', 'true')
    fireEvent.click(screen.getByRole('button', { name: T.confirm_the_release }))

    await screen.findByText(T.the_switch_could_not_be_read_back)
    await waitFor(() => expect(strip).toHaveAttribute('data-engaged', 'false'))
    expect(container.querySelector('.se-status .se-ks-dot')).toHaveAttribute(
      'data-state',
      'unknown',
    )
  })

  it('withdraws an open release pane when the reading stops being engaged', async () => {
    // The offer rule applied for as long as the pane is up. A release is a claim
    // about what is in force, so it is offered only from an `engaged` reading — but
    // an already-open pane kept rendering the engaged record (initiator, reason,
    // timestamp) from retained data after the read failed, with a confirm beside it.
    await armRelease({
      engaged: true,
      initiator: 'billy',
      reason: 'runaway watch loop',
      engaged_ts: '2026-08-17T10:00:00Z',
    })
    await screen.findByText(/billy/)
    stub({ killSwitch: [{ status: 503, body: { code: 'kill_switch_unreadable', error: 'no db' } }] })
    await client.refetchQueries({ queryKey: QK.killSwitch })

    await screen.findByText(P.could_not_read_the_kill_switch)
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: T.confirm_the_release })).toBeNull(),
    )
    // The record it was rendering goes with it: the stop it described is no longer
    // a reading, so naming its author is a claim nothing here can make.
    expect(screen.queryByText(/runaway watch loop/)).toBeNull()
  })

  it('withdraws an open release pane when the switch is already released', async () => {
    // The same rule for the other degradation, which is a successful read rather
    // than a failure: another operator released it, so there is no stop to release
    // and the pane's record describes a state that has ended.
    await armRelease({ engaged: true, initiator: 'billy', reason: 'runaway watch loop' })
    await screen.findByText(/billy/)
    stub({ killSwitch: [{ body: snapshot({ engaged: false }) }] })
    await client.refetchQueries({ queryKey: QK.killSwitch })

    await screen.findByText(P.kill_switch_released)
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: T.confirm_the_release })).toBeNull(),
    )
    expect(screen.queryByText(/runaway watch loop/)).toBeNull()
  })

  it('says a release changed nothing when the switch was not engaged', async () => {
    await armRelease(
      { engaged: true },
      {
        killSwitch: [
          { body: snapshot({ engaged: true }) },
          { body: snapshot({ engaged: false }) },
        ],
        post: {
          body: {
            ok: true,
            action: 'release',
            changed: false,
            switch: switchState({ engaged: false }),
            resumed: [],
          },
        },
      },
    )
    fireEvent.click(screen.getByRole('button', { name: T.confirm_the_release }))
    expect(await screen.findByText(T.it_was_not_engaged)).toBeInTheDocument()
  })
})

describe('the run spend block', () => {
  /** The docked inspector, scoped so a query cannot match the row instead. */
  const inspector = () => within(screen.getByLabelText(P.selected_run))

  function renderWithRuns(entries: unknown[], runSpend?: Record<string, Answer>) {
    stub({
      queue: { body: { entries, grouped: {}, total: entries.length, total_credits: 163.2 } },
      runSpend,
    })
    return renderPage()
  }

  it('shows the engine\u2019s own total against the ceiling in force', async () => {
    renderWithRuns([entry()])
    // One string, interpolated: the total and the ceiling it is compared against.
    // The ceiling's origin travels with it, because a surface showing only `600`
    // cannot say whether somebody chose it or the app ships it.
    expect(await screen.findByText('163.2 of 600')).toBeInTheDocument()
    expect(inspector().getByText(/App-wide/)).toBeInTheDocument()
    expect(inspector().getByText(/budget\.run_ceiling_credits/)).toBeInTheDocument()
  })

  it('binds the pane to the selected row rather than to the first one', async () => {
    // The mockup's inspector was static below its header, so selecting the
    // budget-parked run still showed the first run's figures.
    renderWithRuns(
      [
        entry({ run_id: 'run_a', spec: 'alpha' }),
        entry({ run_id: 'run_b', spec: 'beta', waiting_on: 'budget' }),
      ],
      {
        run_a: { body: spend({ run_id: 'run_a', credits: 10, recorded_credits: 10 }) },
        run_b: { body: spend({ run_id: 'run_b', credits: 599.5, recorded_credits: 599.5 }) },
      },
    )
    await screen.findByText('10 of 600')

    const rows = screen.getAllByRole('row').filter((r) => r.hasAttribute('aria-selected'))
    rows[1].focus()
    await waitFor(() => expect(screen.getByText('599.5 of 600')).toBeInTheDocument())
    expect(screen.queryByText('10 of 600')).toBeNull()

    // And the two reads are cached APART, keyed by run. One entry for both runs
    // would still render correctly here — the query function closes over the
    // selected id, so a refetch would land the right figures a moment later — but
    // until it did, one run's spend would be on screen under another run's name,
    // which is the static-inspector defect wearing a different mechanism.
    const keys = client.getQueryCache().getAll().map((query) => query.queryKey)
    expect(keys).toContainEqual(QK.runSpend('run_a'))
    expect(keys).toContainEqual(QK.runSpend('run_b'))
  })

  it('reports a stale row as stale rather than as a broken read', async () => {
    // A 404 is the one spend failure an operator can act on: the run left the
    // table, so the row on the left is what is wrong and no retry can help.
    renderWithRuns([entry()], {
      run_8f2a41: { status: 404, body: { code: 'run_unknown', error: 'no run has that id' } },
    })
    expect(await screen.findByText(T.no_run_has_that_id)).toBeInTheDocument()
    expect(inspector().queryByText(T.could_not_read_this_runs_spend)).toBeNull()
  })

  it('states the refusal for a spend read that failed', async () => {
    renderWithRuns([entry()], {
      run_8f2a41: { status: 503, body: { code: 'spend_unreadable', error: 'database is locked' } },
    })
    expect(await screen.findByText(T.could_not_read_this_runs_spend)).toBeInTheDocument()
    expect(inspector().getByText(/spend_unreadable/)).toBeInTheDocument()
  })

  it('says so when the run row and the engine total disagree', async () => {
    // Both figures travel so a surface can show them agreeing rather than
    // silently choosing one. When they do not agree, which one the ceiling
    // compares is the fact that matters.
    const { container } = renderWithRuns([entry()], {
      run_8f2a41: { body: spend({ credits: 163.2, recorded_credits: 12 }) },
    })
    await screen.findByText('163.2 of 600')
    await waitFor(() =>
      expect(container.querySelector('[data-spend-disagrees="true"]')).toHaveTextContent('12'),
    )
  })
})
