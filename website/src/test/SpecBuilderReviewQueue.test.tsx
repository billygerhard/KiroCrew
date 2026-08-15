// The review queue is reachable, grouped by the engine, and its row actions act.
//
// What these pin, and why each existed as a gap: the queue actions
// (release_quarantined_feedback, redispatch_item, clean_workspace,
// teardown_run_workspaces) had no route and no UI at all, and the only queue
// rendering in the app was the per-run spend table -- flat, ungrouped and
// actionless. So every one of the queue's human obligations was unreachable.
//
// Two properties beyond "the button works":
//
//   * the grouping RENDERED is the grouping the engine SENT. A panel that
//     re-grouped `entries` itself would pass a naive click test and still show a
//     second, drifting view of one run, so the payload here deliberately puts a
//     run in `entries` whose group membership only `grouped` states.
//   * no action names an actor. The actor is the authenticated session on the
//     server; a body that carried one would be a body a caller could forge.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import React from 'react'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import ReviewQueuePanel from '../apps/spec-builder/components/ReviewQueuePanel'

let queryClient: QueryClient

const HELD_ROW = {
  run_id: 'run-a',
  project: '/p',
  spec: 'demo',
  state: 'awaiting_review',
  waiting_on: 'review',
  waiting_s: 900,
  cost_credits: 2,
  gate: 'requirements',
  source: 'github',
  item_id: '42',
  revision_exhausted: true,
  feedback_quarantined: 2,
}

const BUDGET_ROW = {
  run_id: 'run-b',
  project: '/p',
  spec: 'other',
  state: 'halted_budget',
  waiting_on: 'budget',
  waiting_s: 60,
  cost_credits: 9,
  gate: null,
}

/** The engine's queue as the backend relays it: the flat list for the spend
 *  table, and `grouped` from ``QueueSnapshot.grouped()``. */
const QUEUE = {
  entries: [HELD_ROW, BUDGET_ROW],
  grouped: { awaiting_review: [HELD_ROW], halted_budget: [BUDGET_ROW] },
  total: 2,
  total_credits: 11,
}

interface Call {
  url: string
  body: unknown
}

function harness(queue: unknown = QUEUE, actionBody = '{"ok":true,"released":true}') {
  const calls: Call[] = []
  vi.stubGlobal(
    'fetch',
    vi.fn().mockImplementation((url: string, init?: RequestInit) => {
      if ((init?.method || 'GET') === 'POST') {
        calls.push({ url, body: init?.body ? JSON.parse(String(init.body)) : null })
        return Promise.resolve({ ok: true, status: 200, text: async () => actionBody })
      }
      return Promise.resolve({ ok: true, status: 200, text: async () => JSON.stringify(queue) })
    }),
  )
  return calls
}

beforeEach(() => {
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
})

afterEach(() => {
  vi.restoreAllMocks()
})

function mount() {
  render(
    <QueryClientProvider client={queryClient}>
      <ReviewQueuePanel onClose={() => {}} setErr={() => {}} />
    </QueryClientProvider>,
  )
}

describe('ReviewQueuePanel', () => {
  it('renders one section per run state the engine grouped', async () => {
    harness()
    mount()

    await waitFor(() => screen.getByRole('region', { name: /Waiting for review/i }))
    screen.getByRole('region', { name: /Stopped by the budget/i })
    // A state with nothing waiting is absent from the engine's grouping, and the
    // panel must not invent an empty heading for it.
    expect(screen.queryByRole('region', { name: /Stalled/i })).toBeNull()
  })

  it('reads the grouping the engine sent rather than re-grouping the flat list', async () => {
    // Two runs in ``entries``, but the engine grouped only one of them. That
    // asymmetry is what makes the assertion mean something, and it separates all
    // three outcomes: a panel reading ``grouped`` renders exactly one section, a
    // panel re-grouping ``entries`` renders two, and a panel rendering nothing
    // renders none. An earlier version asserted only the empty state, which the
    // loading frame satisfies before any data arrives -- so it passed while the
    // panel re-grouped the list, and while the panel was blank.
    harness({
      entries: [HELD_ROW, BUDGET_ROW],
      grouped: { awaiting_review: [HELD_ROW] },
      total: 2,
      total_credits: 11,
    })
    mount()

    await waitFor(() => screen.getByRole('region', { name: /Waiting for review/i }))
    expect(screen.queryByRole('region', { name: /Stopped by the budget/i })).toBeNull()
  })

  it('names a run state it has no phrase for verbatim instead of dropping the group', async () => {
    harness({
      entries: [HELD_ROW],
      grouped: { some_future_state: [{ ...HELD_ROW, state: 'some_future_state' }] },
      total_credits: 2,
    })
    mount()

    // The engine decides which states hold a person's work. A label table that
    // gated the SECTION on having a phrase would hide the whole group.
    await waitFor(() => screen.getByRole('region', { name: 'some_future_state' }))
  })

  it('releases a held comment by the id the operator supplies', async () => {
    const calls = harness()
    mount()

    await waitFor(() => screen.getByText(/2 reviewer comment\(s\) are held/i))
    // The count comes from the projection; the ids never do, so the surface asks
    // rather than guessing one.
    const release = screen.getByRole('button', { name: /Release comment/i })
    expect(release).toBeDisabled()

    fireEvent.change(screen.getByLabelText(/Held comment id/i), { target: { value: 'c-7' } })
    fireEvent.click(screen.getByRole('button', { name: /Release comment/i }))

    await waitFor(() => expect(calls.length).toBe(1))
    expect(calls[0].url).toContain('/engine/queue/release-feedback')
    expect(calls[0].body).toEqual({
      project: '/p',
      spec: 'demo',
      run_id: 'run-a',
      comment_id: 'c-7',
    })
    // No actor, under any spelling: the server takes it from the session.
    for (const field of ['actor', 'user', 'approver', 'initiator']) {
      expect(calls[0].body).not.toHaveProperty(field)
    }
  })

  it('reports the engine answer when a release changed nothing', async () => {
    harness(QUEUE, '{"ok":true,"released":false}')
    mount()

    await waitFor(() => screen.getByLabelText(/Held comment id/i))
    fireEvent.change(screen.getByLabelText(/Held comment id/i), { target: { value: 'gone' } })
    fireEvent.click(screen.getByRole('button', { name: /Release comment/i }))

    // A release for a comment nobody held is answered, not reported as a release.
    await waitFor(() => screen.getByText(/Nothing matched, so nothing changed/i))
  })

  it('re-dispatches a watched item at the generation the operator names', async () => {
    const calls = harness(QUEUE, '{"ok":true,"lifted":true}')
    mount()

    await waitFor(() => screen.getByLabelText(/Item generation/i))
    // The generation is not on the queue row, so the control stays unavailable
    // until it is named rather than defaulting to one.
    expect(screen.getByRole('button', { name: /Re-dispatch item/i })).toBeDisabled()

    fireEvent.change(screen.getByLabelText(/Item generation/i), { target: { value: '3' } })
    fireEvent.click(screen.getByRole('button', { name: /Re-dispatch item/i }))

    await waitFor(() => expect(calls.length).toBe(1))
    expect(calls[0].url).toContain('/engine/queue/redispatch')
    expect(calls[0].body).toEqual({ source: 'github', item_id: '42', generation: 3 })
  })

  it('tears down a run’s workspaces from its own row', async () => {
    const calls = harness(QUEUE, '{"ok":true,"complete":true}')
    mount()

    const buttons = await waitFor(() => screen.getAllByRole('button', { name: /Tear down workspaces/i }))
    fireEvent.click(buttons[1])

    await waitFor(() => expect(calls.length).toBe(1))
    expect(calls[0].url).toContain('/engine/queue/teardown')
    // The second row's run, so the action acts on the row it was clicked from.
    expect(calls[0].body).toEqual({ run_id: 'run-b' })
  })

  it('reports an incomplete teardown as incomplete and shows the ids a retry needs', async () => {
    // The failure the cleanup control exists for. Reporting it as done, and
    // discarding the kept rows, left the panel telling the operator to take an
    // id from a teardown report it never displayed.
    harness(
      QUEUE,
      JSON.stringify({
        ok: true,
        complete: false,
        report: {
          run_id: 'run-b',
          kept: [
            { workspace_id: 12, run_id: 'run-b', kind: 'worktree', location: '/w/12', removed: false, reason: 'a deployment still points at it' },
          ],
          removed: [],
        },
      }),
    )
    mount()

    const buttons = await waitFor(() => screen.getAllByRole('button', { name: /Tear down workspaces/i }))
    fireEvent.click(buttons[1])

    await waitFor(() => screen.getByText(/could not remove every workspace/i))
    // The id is the retry key, so it has to be on screen, not just in the response.
    screen.getByText(/12/)
    expect(screen.queryByText(/^Done\.$/)).toBeNull()
  })

  it('shows the analysis findings the engine stored, grouped by criterion', async () => {
    harness({
      entries: [HELD_ROW],
      grouped: {
        awaiting_review: [
          {
            ...HELD_ROW,
            analysis: [
              {
                criterion: '3.2',
                keyed: true,
                findings: [
                  { kind: 'ambiguity', severity: 'warning', message: 'Two readings of "promptly".' },
                ],
              },
              {
                criterion: null,
                keyed: false,
                findings: [{ kind: 'coverage', severity: 'info', message: 'No test names this.' }],
              },
            ],
          },
        ],
      },
      total: 1,
      total_credits: 2,
    })
    mount()

    await waitFor(() => screen.getByText(/Analysis findings/i))
    screen.getByText('3.2')
    // The unkeyed group is named, not dropped -- a finding the provider could not
    // key is still one a reviewer has to read.
    screen.getByText(/name no declared criterion/i)
    screen.getByText(/Two readings of "promptly"/i)
    screen.getByText(/No test names this/i)
  })

  it('says nothing about analysis for a run that has none', async () => {
    // The engine distinguishes "no analysis recorded" from "recorded no
    // findings", so an absent array must not render as a clean bill of health.
    harness()
    mount()

    await waitFor(() => screen.getByRole('region', { name: /Waiting for review/i }))
    expect(screen.queryByText(/Analysis findings/i)).toBeNull()
  })

  it('names a stage failure as such rather than pointing at an empty kept list', async () => {
    // complete:false covers two different failures. Every row went, but the
    // teardown stage failed -- so the kept-rows wording would send the operator
    // to a list with nothing in it.
    harness(
      QUEUE,
      JSON.stringify({
        ok: true,
        complete: false,
        report: { run_id: 'run-b', kept: [], removed: [{ workspace_id: 4 }], stage: 'failed', stage_reason: 'the cleanup command exited 1' },
      }),
    )
    mount()

    const buttons = await waitFor(() => screen.getAllByRole('button', { name: /Tear down workspaces/i }))
    fireEvent.click(buttons[1])

    await waitFor(() => screen.getByText(/the cleanup command exited 1/i))
    expect(screen.queryByText(/could not remove every workspace/i)).toBeNull()
    expect(screen.queryByText(/^Done\.$/)).toBeNull()
  })

  it('lays finding prose out without letting it reflow the rows around it', async () => {
    // The bullet names this threat directly: the display path KEEPS the line
    // breaks prose is entitled to, so a surface that lays them out can be
    // reflowed by a crafted message. pre-wrap honours the breaks; anywhere (not
    // break-word) is the value that contributes break opportunities to
    // min-content sizing, so a long unbroken token cannot widen this table.
    // jsdom cannot measure layout, so the inline styles are what gets pinned --
    // without this, deleting both properties left every test green.
    const hostile = 'line one\nline two\n' + 'A'.repeat(400)
    harness({
      entries: [HELD_ROW],
      grouped: {
        awaiting_review: [
          {
            ...HELD_ROW,
            analysis: [
              {
                criterion: '1.1',
                keyed: true,
                findings: [{ kind: 'injection', severity: 'warning', message: hostile }],
              },
            ],
          },
        ],
      },
      total: 1,
      total_credits: 2,
    })
    mount()

    const prose = await waitFor(() => screen.getByText(/line one/))
    expect(prose).toHaveStyle({ whiteSpace: 'pre-wrap' })
    expect(prose).toHaveStyle({ overflowWrap: 'anywhere' })
    // The newlines survive as text rather than being collapsed or stripped: the
    // engine preserved them deliberately, so losing them here would discard
    // information a reviewer is meant to read.
    expect(prose.textContent).toContain('line one\nline two')
  })

  it('cleans a workspace row by ledger id, which no queue row carries', async () => {
    const calls = harness(QUEUE, '{"ok":true,"removed":true}')
    mount()

    await waitFor(() => screen.getByLabelText(/Workspace row id/i))
    screen.getByText(/not part of the queue projection/i)

    fireEvent.change(screen.getByLabelText(/Workspace row id/i), { target: { value: '12' } })
    fireEvent.click(screen.getByRole('button', { name: /Remove workspace/i }))

    await waitFor(() => expect(calls.length).toBe(1))
    expect(calls[0].url).toContain('/engine/queue/clean-workspace')
    expect(calls[0].body).toEqual({ workspace_id: 12 })
  })

  it('says which runs no automatic revision will move', async () => {
    harness()
    mount()

    // ``revision_exhausted`` is the engine's flag, surfaced on the row rather
    // than as a separate state, because the run is still waiting in the queue.
    await waitFor(() => screen.getByText(/Revision cycles are used up/i))
  })
})
