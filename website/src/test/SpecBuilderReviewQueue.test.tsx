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
    // ``entries`` says nothing about groups. A panel that grouped the flat list
    // itself would render this run under a heading of its own choosing; one that
    // renders ``grouped`` renders nothing at all, because the engine sent none.
    harness({ entries: [HELD_ROW, BUDGET_ROW], total_credits: 11 })
    mount()

    await waitFor(() => screen.getByText(/Nothing is waiting on a person/i))
    expect(screen.queryByRole('region', { name: /Waiting for review/i })).toBeNull()
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
