/**
 * The Operator_Surface shell: first-run routing, keyboard selection, and the
 * safety strip.
 *
 * Four things here are properties of the SELECTED DESIGN rather than incidental
 * rendering, and each is asserted because losing it silently is the failure mode:
 *
 *   - **Nothing overlays anything.** The selected mockup passes the
 *     "safety controls are never behind navigation" criterion only because it has
 *     no overlay, so the kill-switch strip cannot be covered. A drawer or modal
 *     added later would look like a feature and would reintroduce the rejected
 *     mockup's failure.
 *   - **First run leads with the assistant** — in the landing pane AND in the rail's
 *     order, both read off one derivation so they cannot disagree — and a config
 *     read that FAILED is not first run: an unparseable document is a repair, the
 *     assistant refuses to write over one, and React Query's retention of the last
 *     body across a failed refetch is what would otherwise keep the claim alive
 *     after nothing confirms it.
 *   - **Rows are keyboard-operable.** The mockup put `aria-selected` on a plain
 *     `<tr>` with no role, no tabindex and no key handling, which announces a
 *     selection a keyboard cannot reach.
 *   - **The waiting reason is stated in words**, and spent revision cycles get
 *     their own reason: the engine keeps such a run in `awaiting_review` with
 *     `waiting_on: review`, so a surface reading only `waiting_on` offers "approve
 *     or send it back" for a gate that will dispatch no further revision turn.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { act, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import SpecEnginePage, { __testing } from '../apps/spec-engine/SpecEnginePage'
import { QK } from '../apps/spec-engine/api'
import { SE_CSS } from '../apps/spec-engine/styles'
import { getBuiltinIcon } from '../apps/builtinIcons'
import { getBuiltinComponent } from '../apps/builtinRegistry'
import en from '../i18n/locales/en.json'

const T = en.apps.specEngine.specEnginePage

/** One queue row, with only the fields a caller overrides spelled out. */
function entry(over: Partial<Record<string, unknown>> = {}) {
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

type Answer = { status?: number; body: unknown }

/**
 * Answer each of the shell's three reads independently.
 *
 * Per-route rather than one body for all three, because the interesting states
 * are exactly the ones where the reads DISAGREE: an unreadable configuration
 * beside a healthy queue, an engaged switch beside an empty queue.
 *
 * `config` may be a QUEUE of answers, because the retained-data guard is only
 * observable across TWO reads of that route: the second one fails while React
 * Query still holds the first one's `configured === false`. The last entry
 * sticks, so a test that only cares about a steady state passes one answer.
 */
function stubReads(answers: {
  config?: Answer | Answer[]
  queue?: Answer
  killSwitch?: Answer
}) {
  const config = Array.isArray(answers.config)
    ? [...answers.config]
    : [
        answers.config ?? {
          // The configured default carries a project ENTRY, not just a file:
          // first-run is "no project entry", so a bare `document: {}` here would
          // put every unrelated test on the first-run rail.
          body: { configured: true, document: { projects: { acme: {} } }, elided: [] },
        },
      ]
  const pick = (url: string): Answer => {
    if (url.startsWith('/api/apps/spec-engine/config/registry')) {
      // The configuration pane's settings form is generated from this read. It is
      // answered BEFORE the generic '/config' prefix below, which would otherwise
      // hand it a ConfigSnapshot and crash its render, and it must not CONSUME a
      // queued answer for the same reason the resolved read must not.
      return {
        body: { settings: [], source_presets: [], profile_presets: [], roles: [], levels: [] },
      }
    }
    if (url.startsWith('/api/apps/spec-engine/config/resolved')) {
      // The resolved read shares the document read's answer (this suite is not
      // about resolution) but must not CONSUME a queued one: the queue exists to
      // sequence reads of the document route, and a resolved fetch triggered by
      // opening the configuration pane would otherwise silently advance it.
      return config[0]
    }
    if (url.startsWith('/api/apps/spec-engine/config')) {
      return config.length > 1 ? config.shift()! : config[0]
    }
    if (url.startsWith('/api/apps/spec-engine/kill-switch')) {
      return (
        answers.killSwitch ?? {
          body: { switch: { engaged: false, unreadable: false }, stoppable: [], stoppable_credits: 0 },
        }
      )
    }
    return answers.queue ?? { body: { entries: [], grouped: {}, total: 0, total_credits: 0 } }
  }
  vi.stubGlobal(
    'fetch',
    vi.fn((url: string) => {
      const answer = pick(url)
      const status = answer.status ?? 200
      return Promise.resolve({
        ok: status >= 200 && status < 300,
        status,
        text: () => Promise.resolve(JSON.stringify(answer.body)),
      })
    }),
  )
}

/** The client the last render used, so a test can force a refetch of one read. */
let client: QueryClient

function renderPage() {
  client = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchInterval: false } },
  })
  return render(
    <QueryClientProvider client={client}>
      <SpecEnginePage />
    </QueryClientProvider>,
  )
}

/**
 * The rail's panes in DOM order.
 *
 * Read from `data-pane` rather than from label text: the rail is translated, and
 * an ordering assertion over rendered words would break in every catalog but one
 * while saying nothing about which pane each button reaches.
 */
const railPanes = (container: HTMLElement) =>
  Array.from(container.querySelectorAll('.se-rail .se-nav')).map((b) => b.getAttribute('data-pane'))

/** The rail's setup entry, whose alarm marker claims the engine is unconfigured. */
const setupNav = (container: HTMLElement) =>
  container.querySelector('.se-rail .se-nav[data-pane="setup"]')

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('the no-overlay rule', () => {
  it('declares no fixed or absolute positioning anywhere in the app stylesheet', () => {
    // The one property the whole safety criterion rests on. Sticky is allowed and
    // is not an exception: a sticky element scrolls inside its own container and
    // cannot leave it, so it can never cover the status strip, which is a sibling
    // row of the page grid.
    //
    // The ban is deliberately WIDER than the property it protects: it also
    // rejects the standard sr-only utility (position:absolute + clip), which
    // cannot occlude anything. That is a chosen cost, not an oversight — a
    // narrower rule ("absolute is fine if clipped") is exactly the kind of
    // judgement call that erodes one exception at a time until a drawer ships.
    // If screen-reader-only text is ever needed, amend THIS test in the same
    // change, with the reviewer looking at both; the failing test is the gate.
    const declarations = SE_CSS.replace(/\s+/g, '')
    expect(declarations).not.toContain('position:fixed')
    expect(declarations).not.toContain('position:absolute')
  })

  it('keeps the status strip a row of the page grid, not a floating bar', () => {
    expect(SE_CSS).toContain('grid-template-areas:"rail work" "rail status"')
    expect(SE_CSS.replace(/\s+/g, '')).toContain('.se-status{grid-area:status')
  })
})

describe('first run', () => {
  it('lands on the setup assistant when no configuration exists', async () => {
    stubReads({ config: { body: { configured: false, document: {}, elided: [] } } })
    renderPage()
    expect(await screen.findByText(T.nothing_is_configured_yet)).toBeInTheDocument()
    expect(screen.getByText(T.setup_lead)).toBeInTheDocument()
    // And not the queue: the assistant is the primary content, not a tab beside it.
    expect(screen.queryByText(T.sorted_by_time_waiting_longest_first)).toBeNull()
  })

  it('lands on the queue when configuration exists', async () => {
    stubReads({ queue: { body: { entries: [entry()], grouped: {}, total: 1, total_credits: 163.2 } } })
    renderPage()
    expect(await screen.findByText(T.sorted_by_time_waiting_longest_first)).toBeInTheDocument()
    expect(screen.queryByText(T.nothing_is_configured_yet)).toBeNull()
  })

  it('does NOT treat an unreadable configuration as first run', async () => {
    // `config_unreadable` means a document exists and cannot be parsed. Routing that
    // to the assistant would send the operator to a flow that then refuses to
    // overwrite the file it cannot read.
    stubReads({
      config: { status: 409, body: { code: 'config_unreadable', error: 'line 4: trailing comma' } },
      queue: { body: { entries: [entry()], grouped: {}, total: 1, total_credits: 163.2 } },
    })
    renderPage()
    expect(await screen.findByText(T.sorted_by_time_waiting_longest_first)).toBeInTheDocument()
    expect(screen.queryByText(T.nothing_is_configured_yet)).toBeNull()
  })

  it('states the refusal, with its code, on the configuration pane', async () => {
    stubReads({
      config: { status: 409, body: { code: 'config_unreadable', error: 'line 4: trailing comma' } },
    })
    const { getByRole } = renderPage()
    await screen.findByText(T.sorted_by_time_waiting_longest_first)
    getByRole('button', { name: new RegExp(T.configuration) }).click()
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent(T.could_not_read_the_configuration)
    // The code is what a reader acts on; the sentence alone does not say which
    // failure this was.
    expect(alert).toHaveTextContent('config_unreadable')
    expect(alert).toHaveTextContent('line 4: trailing comma')
  })

  it('does not re-assert first run from retained data when a later read fails', async () => {
    // The guard's whole reason for existing. React Query RETAINS the last data
    // across a failed refetch, so this sequence — a `configured === false` read,
    // then a read that failed — leaves a snapshot on hand that says "nothing is
    // configured" while nothing currently confirms it. Doubt is not absence, and
    // every channel that spends that claim has to fall silent together.
    stubReads({
      config: [
        { body: { configured: false, document: {}, elided: [] } },
        { status: 409, body: { code: 'config_unreadable', error: 'line 4: trailing comma' } },
      ],
      queue: { body: { entries: [entry()], grouped: {}, total: 1, total_credits: 163.2 } },
    })
    const { container } = renderPage()
    // Established first, so the retention under test is real rather than assumed:
    // the claim is on screen before the read that fails.
    expect(await screen.findByText(T.nothing_is_configured_yet)).toBeInTheDocument()
    expect(setupNav(container)).toHaveAttribute('data-alarm', 'true')

    await act(async () => {
      await client.invalidateQueries({ queryKey: QK.config, exact: true })
    })

    // Not the landing pane: a failed read lands on the queue.
    expect(await screen.findByText(T.sorted_by_time_waiting_longest_first)).toBeInTheDocument()
    expect(screen.queryByText(T.nothing_is_configured_yet)).toBeNull()
    // Not the alarm either. It is a positive claim that the engine is
    // unconfigured, which is exactly what is no longer known.
    await waitFor(() => expect(setupNav(container)).not.toHaveAttribute('data-alarm'))
    // And not the rail's order, which reads the same value: a rail still leading
    // with the assistant would be the two halves disagreeing.
    expect(railPanes(container)).toEqual(['queue', 'config', 'setup'])
  })
})

describe('the pane rail', () => {
  it('leads with the setup assistant while nothing is configured', async () => {
    // The rail's order is the standing answer to "what do I do here": on an
    // unconfigured engine the assistant is the only pane that can produce
    // anything, so it cannot sit third behind a queue that must be empty.
    stubReads({ config: { body: { configured: false, document: {}, elided: [] } } })
    const { container } = renderPage()
    await screen.findByText(T.nothing_is_configured_yet)
    expect(railPanes(container)).toEqual(['setup', 'queue', 'config'])
    expect(setupNav(container)).toHaveAttribute('data-alarm', 'true')
  })

  it('leads with the queue once a project is configured', async () => {
    stubReads({ queue: { body: { entries: [entry()], grouped: {}, total: 1, total_credits: 163.2 } } })
    const { container } = renderPage()
    await screen.findByText(T.sorted_by_time_waiting_longest_first)
    expect(railPanes(container)).toEqual(['queue', 'config', 'setup'])
    // The assistant stays reachable for the next project; it just stops shouting.
    expect(setupNav(container)).not.toHaveAttribute('data-alarm')
  })

  it('treats a document with no project entry as first run, not as configured', async () => {
    // `configured` says only that the FILE exists — one app-scoped save from the
    // configuration pane creates it without configuring any project. An engine
    // in that state still has nothing to run against, so the assistant still
    // leads and still carries its alarm. This is the arm of the first-run
    // definition that file-existence alone cannot see.
    stubReads({ config: { body: { configured: true, document: { limits: {} }, elided: [] } } })
    const { container } = renderPage()
    await screen.findByText(T.nothing_is_configured_yet)
    expect(railPanes(container)).toEqual(['setup', 'queue', 'config'])
    expect(setupNav(container)).toHaveAttribute('data-alarm', 'true')
  })

  it('derives its order from the first-run reading, stranding no pane in either', () => {
    expect(__testing.paneOrder(true)).toEqual(['setup', 'queue', 'config'])
    expect(__testing.paneOrder(false)).toEqual(['queue', 'config', 'setup'])
    // The order decides which pane is loudest, never which are reachable. An
    // order that dropped one would strand its content with no way in, and the
    // rendered assertions above would still pass on the two panes that remained.
    const panes = Object.keys(__testing.PANE_LABEL_KEY).sort()
    expect([...__testing.paneOrder(true)].sort()).toEqual(panes)
    expect([...__testing.paneOrder(false)].sort()).toEqual(panes)
  })
})

describe('the run list', () => {
  it('renders the engine\u2019s own row values rather than re-deriving them', async () => {
    stubReads({
      queue: {
        body: { entries: [entry()], grouped: {}, total: 1, total_credits: 163.2 },
      },
    })
    renderPage()
    // Scoped to the grid: the docked inspector shows the same run, so an unscoped
    // query would match twice and could pass while the ROW rendered nothing.
    const grid = within(await screen.findByRole('grid'))
    expect(grid.getByText('idempotent-refunds')).toBeInTheDocument()
    expect(grid.getByText('run_8f2a41')).toBeInTheDocument()
    expect(grid.getByText('design')).toBeInTheDocument()
    // The wait comes from the engine's clock (`waiting_s`), never from a browser
    // subtraction against `entered_ts`.
    expect(grid.getByText('2h 14m')).toBeInTheDocument()
  })

  it('says nothing is waiting when the queue is empty, and distinguishes a filter miss', async () => {
    stubReads({
      queue: { body: { entries: [], grouped: {}, total: 0, total_credits: 0 } },
    })
    renderPage()
    expect(await screen.findByText(T.nothing_is_waiting_on_a_person)).toBeInTheDocument()
  })

  it('shows a filter miss as a filter miss, not as an empty queue', async () => {
    stubReads({
      queue: { body: { entries: [entry()], grouped: {}, total: 1, total_credits: 163.2 } },
    })
    const { getByRole } = renderPage()
    await screen.findByRole('grid')
    // One review row is queued, so filtering to budget must not read as "nothing is
    // waiting on a person" — the operator would close the page.
    getByRole('button', { name: new RegExp(T.budget) }).click()
    expect(await screen.findByText(T.nothing_matches_this_filter)).toBeInTheDocument()
  })

  it('states the refusal and offers a retry when the queue cannot be read', async () => {
    stubReads({
      queue: { status: 503, body: { code: 'queue_unreadable', error: 'database is locked' } },
    })
    renderPage()
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent(T.could_not_read_the_run_queue)
    expect(alert).toHaveTextContent('queue_unreadable')
    expect(screen.getByRole('button', { name: T.retry })).toBeInTheDocument()
  })

  it('does not let the status strip read an unread queue as an empty one', async () => {
    // The strip's two figures are queue-derived. On a failed read they are
    // unknown, and "Spend 0 / Waiting 0" on the config or setup pane would be
    // the fail-open the kill-switch text deliberately refuses. The strip must
    // state the refusal instead of coalescing to zero.
    stubReads({
      queue: { status: 503, body: { code: 'queue_unreadable', error: 'database is locked' } },
    })
    const { container } = renderPage()
    await screen.findByRole('alert')
    const strip = container.querySelector('.se-status')
    expect(strip).not.toBeNull()
    expect(strip!.querySelector('[data-strip-error="queue"]')).toHaveTextContent(
      T.could_not_read_the_run_queue,
    )
    expect(strip!.textContent).not.toContain(T.spend_on_waiting_runs)
    expect(strip!.textContent).not.toContain(T.waiting_on_a_person)
  })
})

describe('keyboard selection', () => {
  const three = [
    entry({ run_id: 'run_a', spec: 'alpha' }),
    entry({ run_id: 'run_b', spec: 'beta' }),
    entry({ run_id: 'run_c', spec: 'gamma' }),
  ]

  const rows = () => screen.getAllByRole('row').filter((r) => r.hasAttribute('aria-selected'))

  it('puts exactly one row in the tab order and marks it selected', async () => {
    stubReads({ queue: { body: { entries: three, grouped: {}, total: 3, total_credits: 0 } } })
    renderPage()
    await screen.findByRole('grid')
    const tabbable = rows().filter((r) => r.getAttribute('tabindex') === '0')
    expect(tabbable).toHaveLength(1)
    expect(tabbable[0]).toHaveAttribute('aria-selected', 'true')
    // A roving tabindex: the rest are reachable by arrow key, not by Tab, so the
    // list costs one stop rather than one per run.
    expect(rows().filter((r) => r.getAttribute('tabindex') === '-1')).toHaveLength(2)
  })

  it('moves selection with the arrow keys and with j/k', async () => {
    stubReads({ queue: { body: { entries: three, grouped: {}, total: 3, total_credits: 0 } } })
    const { container } = renderPage()
    await screen.findByRole('grid')

    const press = (key: string) => {
      const focused = container.querySelector('.se-row[aria-selected="true"]') as HTMLElement
      focused.focus()
      focused.dispatchEvent(new KeyboardEvent('keydown', { key, bubbles: true }))
    }

    press('ArrowDown')
    await waitFor(() => expect(rows()[1]).toHaveAttribute('aria-selected', 'true'))
    press('j')
    await waitFor(() => expect(rows()[2]).toHaveAttribute('aria-selected', 'true'))
    press('k')
    await waitFor(() => expect(rows()[1]).toHaveAttribute('aria-selected', 'true'))
    press('Home')
    await waitFor(() => expect(rows()[0]).toHaveAttribute('aria-selected', 'true'))
    press('End')
    await waitFor(() => expect(rows()[2]).toHaveAttribute('aria-selected', 'true'))
  })

  it('does not step past either end of the list', async () => {
    stubReads({ queue: { body: { entries: three, grouped: {}, total: 3, total_credits: 0 } } })
    const { container } = renderPage()
    await screen.findByRole('grid')
    const press = (key: string) => {
      const focused = container.querySelector('.se-row[aria-selected="true"]') as HTMLElement
      focused.focus()
      focused.dispatchEvent(new KeyboardEvent('keydown', { key, bubbles: true }))
    }
    press('ArrowUp')
    await waitFor(() => expect(rows()[0]).toHaveAttribute('aria-selected', 'true'))
    press('End')
    // Awaited between presses: focus moves synchronously but the selection it drives
    // lands in a React update, so a second press read before that commits would move
    // from the OLD row and assert nothing about the clamp.
    await waitFor(() => expect(rows()[2]).toHaveAttribute('aria-selected', 'true'))
    press('ArrowDown')
    await waitFor(() => expect(rows()[2]).toHaveAttribute('aria-selected', 'true'))
    // Exactly one selection survives every clamp.
    expect(rows().filter((r) => r.getAttribute('aria-selected') === 'true')).toHaveLength(1)
  })
})

describe('the docked inspector', () => {
  it('states the waiting reason in words for the selected run', async () => {
    stubReads({
      queue: { body: { entries: [entry()], grouped: {}, total: 1, total_credits: 0 } },
    })
    renderPage()
    expect(await screen.findByText(T.why_review)).toBeInTheDocument()
  })

  it('gives spent revision cycles their own reason, not the plain verdict one', async () => {
    // Same state, same `waiting_on`. Only `revision_exhausted` distinguishes a gate
    // that will dispatch another revision turn from one that will not.
    stubReads({
      queue: {
        body: {
          entries: [entry({ revision_exhausted: true })],
          grouped: {},
          total: 1,
          total_credits: 0,
        },
      },
    })
    renderPage()
    expect(await screen.findByText(T.why_review_exhausted)).toBeInTheDocument()
    expect(screen.queryByText(T.why_review)).toBeNull()
  })

  it('offers a budget-parked run the budget reason, never the gate\u2019s', async () => {
    stubReads({
      queue: {
        body: {
          entries: [entry({ waiting_on: 'budget', state: 'halted_budget', gate: null })],
          grouped: {},
          total: 1,
          total_credits: 0,
        },
      },
    })
    renderPage()
    expect(await screen.findByText(T.why_budget)).toBeInTheDocument()
    expect(screen.queryByText(T.why_review)).toBeNull()
  })

  it('keys every waiting reason the engine can report', () => {
    // A missing member would render as its own dotted key rather than fail, so the
    // table is checked against the enum instead of against what a fixture happened
    // to exercise.
    expect(Object.keys(__testing.WHY_KEY).sort()).toEqual(['budget', 'review', 'stall'])
    expect(Object.keys(__testing.WAIT_LABEL_KEY).sort()).toEqual(['budget', 'review', 'stall'])
  })
})

describe('the safety strip', () => {
  it('reads the kill switch and turns the whole strip on when it is engaged', async () => {
    stubReads({
      killSwitch: {
        body: {
          switch: { engaged: true, unreadable: false, initiator: 'me', reason: 'stop' },
          stoppable: [],
          stoppable_credits: 0,
        },
      },
    })
    const { container } = renderPage()
    expect(await screen.findByText(T.kill_switch_engaged)).toBeInTheDocument()
    // Not a badge: the engaged state colours the entire strip, so it cannot be
    // read past.
    await waitFor(() =>
      expect(container.querySelector('.se-status')).toHaveAttribute('data-engaged', 'true'),
    )
  })

  it('separates a stop nobody chose from a stop an operator chose', async () => {
    // `unreadable` means the flag is in force because its record could not be
    // parsed. Releasing that is a repair; releasing an operator's stop is a
    // decision, and the two must not read alike.
    stubReads({
      killSwitch: {
        body: {
          switch: { engaged: true, unreadable: true, initiator: '', reason: '' },
          stoppable: [],
          stoppable_credits: 0,
        },
      },
    })
    renderPage()
    expect(await screen.findByText(T.kill_switch_record_unreadable)).toBeInTheDocument()
  })

  it('says the switch could not be read rather than showing it as released', async () => {
    stubReads({
      killSwitch: { status: 503, body: { code: 'kill_switch_unreadable', error: 'no database' } },
    })
    renderPage()
    expect(await screen.findByText(T.could_not_read_the_kill_switch)).toBeInTheDocument()
    expect(screen.queryByText(T.kill_switch_released)).toBeNull()
  })
})

describe('registration', () => {
  it('resolves /spec-engine to a component instead of redirecting to chat', () => {
    // Without this row `BuiltinAppRoute` finds nothing and navigates to /chat, so the
    // app's card would open the chat page.
    expect(getBuiltinComponent('/spec-engine')).toBeDefined()
  })

  it('resolves the manifest\u2019s Cog icon', () => {
    // Unregistered, the nav rail falls back to the generic package glyph and the
    // builtin reads as a third-party install.
    expect(getBuiltinIcon('Cog')).toBeDefined()
  })
})

describe('the wait reading', () => {
  it('reports days and hours past a day, hours and minutes past an hour', () => {
    expect(__testing.waitedParts(97200)).toEqual([[1, 'day'], [3, 'hour']])
    expect(__testing.waitedParts(8040)).toEqual([[2, 'hour'], [14, 'minute']])
    expect(__testing.waitedParts(180)).toEqual([[3, 'minute']])
  })

  it('never reports a negative wait', () => {
    // A clock skew between the engine's host and this process would otherwise
    // render "-1 minute", which reads as a defect in the queue rather than in a
    // clock.
    expect(__testing.waitedParts(-5)).toEqual([[0, 'minute']])
  })
})
