/**
 * The engine operations panel, and the one thing a typecheck cannot pin: that it
 * is REACHABLE.
 *
 * A probe that severed the shell button's onClick left the panel unopenable, and
 * neither `tsc` nor `eslint` objected -- the component still compiled, still had
 * every prop typed, and would have passed every test it had while no operator
 * could get to the stop control. So the first test here opens the panel through
 * the button an operator actually clicks rather than by rendering the component
 * directly.
 *
 * The rest assert what the panel must not do: infer a value's origin, or claim a
 * release resumes work.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import React from 'react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

vi.mock('../app-sdk/ChatEmbed', () => ({ default: () => <div data-testid="chat-embed" /> }))

import SpecBuilderPage from '../apps/spec-builder/SpecBuilderPage'

let queryClient: QueryClient

/** A value whose effective number EQUALS its bundled default but which is an
 *  explicit app-level override. The one case where inferring the origin by
 *  comparing value to default gives the wrong answer. */
const CONFIG_BODY = {
  scope: { project: null, source: null },
  settings: {
    'concurrency.global_max_runs': {
      key: 'concurrency.global_max_runs',
      value: 4,
      origin: 'app_config',
      declared_at: 'concurrency.global_max_runs',
      is_default: false,
      default: 4,
      summary: 'Runs at once.',
      kind: 'int',
      scopes: ['app'],
      minimum: 1,
      maximum: null,
      choices: [],
    },
  },
  domains: {},
  domain_sections: ['sources', 'projects'],
}

const RELEASED_SWITCH = {
  switch: {
    engaged: false,
    initiator: '',
    reason: '',
    engaged_ts: '',
    unreadable: false,
    description: 'kill switch: released',
  },
  stoppable: [],
  stoppable_credits: 0,
}

const ENGAGED_SWITCH = {
  switch: {
    engaged: true,
    initiator: 'dashboard',
    reason: 'runaway wave',
    engaged_ts: '2026-01-01T00:00:00+00:00',
    unreadable: false,
    description: 'kill switch: engaged',
  },
  stoppable: [],
  stoppable_credits: 0,
}

const QUEUE_BODY = {
  entries: [
    {
      run_id: 'r1',
      project: 'web',
      spec: 'checkout-flow',
      state: 'awaiting_review',
      waiting_on: 'requirements_review',
      waiting_s: 60,
      cost_credits: 12.5,
      gate: 'requirements',
    },
  ],
  total_credits: 12.5,
}

/** Route each engine GET to its body; everything else is an empty spec list. */
function stubEngineFetch(switchBody: unknown = RELEASED_SWITCH) {
  const fetchMock = vi.fn((url: string) => {
    const body = url.includes('/engine/config')
      ? CONFIG_BODY
      : url.includes('/engine/kill-switch')
        ? switchBody
        : url.includes('/engine/queue')
          ? QUEUE_BODY
          : { specs: [] }
    return Promise.resolve({
      ok: true,
      status: 200,
      text: () => Promise.resolve(JSON.stringify(body)),
    })
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

function renderPage() {
  return render(
    <MemoryRouter>
      <QueryClientProvider client={queryClient}>
        <SpecBuilderPage />
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

beforeEach(() => {
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  localStorage.clear()
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe('EngineOpsPanel', () => {
  it('is reachable from the app shell', async () => {
    stubEngineFetch()
    renderPage()
    const open = await screen.findByRole('button', { name: 'Engine operations' })
    await userEvent.click(open)
    // The panel's own heading, not the button's label: the button exists either
    // way, and asserting on it would pass with the panel wired to nothing.
    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'Stop control' })).toBeInTheDocument()
    })
  })

  it('reports the origin the engine gave, not one inferred from the default', async () => {
    stubEngineFetch()
    renderPage()
    await userEvent.click(await screen.findByRole('button', { name: 'Engine operations' }))
    await waitFor(() => {
      expect(screen.getByText('concurrency.global_max_runs')).toBeInTheDocument()
    })
    // value === default here, so a panel that inferred the origin would label
    // this "shipped default" and tell the operator the override does not exist.
    expect(
      screen.getByText(/app configuration \(concurrency\.global_max_runs\)/),
    ).toBeInTheDocument()
  })

  it('names the blast radius before the stop is thrown', async () => {
    stubEngineFetch()
    renderPage()
    await userEvent.click(await screen.findByRole('button', { name: 'Engine operations' }))
    await waitFor(() => {
      expect(screen.getByText(/Engaging parks 0 run\(s\)/)).toBeInTheDocument()
    })
    expect(screen.getByRole('button', { name: 'Stop all unattended work' })).toBeInTheDocument()
  })

  it('says a release resumes nothing when the switch is engaged', async () => {
    stubEngineFetch(ENGAGED_SWITCH)
    renderPage()
    await userEvent.click(await screen.findByRole('button', { name: 'Engine operations' }))
    await waitFor(() => {
      expect(screen.getByText(/Unattended work is stopped, by dashboard/)).toBeInTheDocument()
    })
    // The sentence an operator most often assumes the other way round.
    expect(screen.getByText(/It does not resume anything that was parked/)).toBeInTheDocument()
    expect(
      screen.getByRole('button', { name: 'Allow unattended work again' }),
    ).toBeInTheDocument()
  })

  it('shows the credits each waiting run consumed', async () => {
    stubEngineFetch()
    renderPage()
    await userEvent.click(await screen.findByRole('button', { name: 'Engine operations' }))
    await waitFor(() => {
      expect(screen.getByText('checkout-flow')).toBeInTheDocument()
    })
    expect(screen.getByText('12.5')).toBeInTheDocument()
  })

  it('names a domain it has no editor for rather than hiding it', async () => {
    stubEngineFetch()
    renderPage()
    await userEvent.click(await screen.findByRole('button', { name: 'Engine operations' }))
    await waitFor(() => {
      expect(screen.getByText(/sources: none configured/)).toBeInTheDocument()
    })
  })
})
