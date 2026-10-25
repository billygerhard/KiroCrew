import { describe, expect, it, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

import { api } from '../api/client'
import type { AcpBackendProbe } from '../api/client/config'
import BackendSelector from './BackendSelector'

function mount(node: React.ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<MemoryRouter><QueryClientProvider client={client}>{node}</QueryClientProvider></MemoryRouter>)
}

function row(id: string, extra: Partial<AcpBackendProbe> = {}): AcpBackendProbe {
  return {
    id, policy_id: id, label: '', selectable: true, independent_setup: false, installed: 'installed',
    missing_components: [], install_command: '', restart_required: false,
    ...extra,
  } as AcpBackendProbe
}

const ROWS = {
  backends: [
    row('claude-seat1', { label: 'Claude Seat 1' }),
    row('claude-seat2', { label: 'Claude Seat 2' }),
    row('codex', { selectable: false }),
    row('pi', { installed: 'missing' }),
  ],
}

function stubListing(defaultBackend = 'claude-seat1') {
  vi.spyOn(api, 'acpBackends').mockResolvedValue(ROWS)
  vi.spyOn(api, 'kirocrewConfig').mockResolvedValue({ agent: { acp_backend: defaultBackend } })
}

async function openList() {
  const trigger = await screen.findByRole('button')
  await waitFor(() => expect(trigger.textContent).toMatch(/Claude Seat/))
  fireEvent.click(trigger)
  return screen.findByRole('listbox')
}

describe('BackendSelector', () => {
  afterEach(() => { cleanup(); vi.restoreAllMocks() })

  it('lists the selectable harnesses.json seats by their display names', async () => {
    stubListing()
    mount(<BackendSelector value={null} onSelect={vi.fn()} />)
    const list = await openList()
    const labels = within(list).getAllByRole('option').map(o => o.textContent)
    expect(labels).toEqual(['Claude Seat 1Default', 'Claude Seat 2'])
  })

  it('an unpicked chat names the configured default on its trigger', async () => {
    stubListing('claude-seat2')
    mount(<BackendSelector value={null} onSelect={vi.fn()} />)
    const trigger = await screen.findByRole('button')
    await waitFor(() => expect(trigger.textContent).toBe('Backend: Claude Seat 2'))
  })

  it('a pick reports the row id', async () => {
    stubListing()
    const onSelect = vi.fn()
    mount(<BackendSelector value="claude-seat1" onSelect={onSelect} />)
    const list = await openList()
    fireEvent.click(within(list).getByTestId('backend-selector-option-claude-seat2'))
    expect(onSelect).toHaveBeenCalledWith('claude-seat2')
    await waitFor(() => expect(screen.queryByRole('listbox')).toBeNull())
  })

  it('is locked while a turn runs and keeps naming the backend', async () => {
    stubListing()
    mount(<BackendSelector value="claude-seat2" onSelect={vi.fn()} disabled />)
    const trigger = await screen.findByRole('button')
    await waitFor(() => expect(trigger.textContent).toBe('Backend: Claude Seat 2'))
    expect((trigger as HTMLButtonElement).disabled).toBe(true)
  })

  it('a failed listing offers nothing', async () => {
    vi.spyOn(api, 'acpBackends').mockRejectedValue(new Error('down'))
    vi.spyOn(api, 'kirocrewConfig').mockResolvedValue({ agent: { acp_backend: 'claude-seat1' } })
    mount(<BackendSelector value={null} onSelect={vi.fn()} />)
    fireEvent.click(await screen.findByRole('button'))
    const list = await screen.findByRole('listbox')
    await waitFor(() => expect(within(list).getByTestId('backend-selector-list-error')).toBeTruthy())
    expect(within(list).queryAllByRole('option')).toHaveLength(0)
  })
})
