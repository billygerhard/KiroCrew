import { describe, expect, it, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import { api } from '../api/client'
import BackendSelector from './BackendSelector'

function mount(node: React.ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={client}>{node}</QueryClientProvider>)
}

const PAYLOAD = {
  backends: [
    { id: '', label: 'Kiro CLI', is_global_default: true },
    { id: 'claude', label: 'Claude Code', is_global_default: false },
  ],
  invalid: [{ id: 'bad', label: 'Bad Descriptor', reasons: ['missing executable', 'no argv'] }],
  unroutable: [{ id: 'no-route', label: 'No Route', reason: 'no recognized routing declared' }],
}

/** Open the dropdown after the listing has loaded. There is exactly one trigger
 *  button, so it is addressed as the sole button rather than by its label, which
 *  is "Default backend" until the query resolves. */
async function openList() {
  const trigger = await screen.findByRole('button')
  // Wait for the listing to resolve — the rows only render once the query lands.
  await waitFor(() => expect(within(trigger).queryByText(/Kiro CLI/)).toBeTruthy())
  fireEvent.click(trigger)
  return screen.findByRole('listbox')
}

describe('BackendSelector', () => {
  afterEach(() => { cleanup(); vi.restoreAllMocks() })

  it('is operable from the keyboard: focus enters the list, arrows rove, Enter picks', async () => {
    vi.spyOn(api, 'backends').mockResolvedValue(PAYLOAD)
    const onSelect = vi.fn()
    mount(<BackendSelector value="" onSelect={onSelect} />)
    const list = await openList()
    const options = within(list).getAllByRole('option')
    // With no filter input the hook moves focus to the selected row on open.
    await waitFor(() => expect(document.activeElement).toBe(options[0]))
    fireEvent.keyDown(list, { key: 'ArrowDown' })
    expect(document.activeElement).toBe(options[1])
    fireEvent.keyDown(list, { key: 'ArrowUp' })
    expect(document.activeElement).toBe(options[0])
    fireEvent.keyDown(list, { key: 'End' })
    expect(document.activeElement).toBe(options[1])
    // Enter on a focused option is the native button activation.
    fireEvent.click(document.activeElement as HTMLElement)
    expect(onSelect).toHaveBeenCalledWith('claude')
  })

  it('Escape closes the list and returns focus to the trigger', async () => {
    vi.spyOn(api, 'backends').mockResolvedValue(PAYLOAD)
    mount(<BackendSelector value="" onSelect={vi.fn()} />)
    const trigger = await screen.findByRole('button')
    const list = await openList()
    fireEvent.keyDown(list, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('listbox')).toBeNull())
    expect(document.activeElement).toBe(trigger)
  })

  it('the trigger names what it selects, not only the current value', async () => {
    vi.spyOn(api, 'backends').mockResolvedValue(PAYLOAD)
    mount(<BackendSelector value="" onSelect={vi.fn()} />)
    const trigger = await screen.findByRole('button')
    await waitFor(() => expect(trigger.textContent).toMatch(/Backend: Kiro CLI/))
  })

  it('renders selectable rows and fires onSelect with the picked id', async () => {
    vi.spyOn(api, 'backends').mockResolvedValue(PAYLOAD)
    const onSelect = vi.fn()
    mount(<BackendSelector value="" onSelect={onSelect} />)
    const list = await openList()
    fireEvent.click(within(list).getByRole('option', { name: /Claude Code/ }))
    expect(onSelect).toHaveBeenCalledWith('claude')
  })

  it('selecting kiro-cli fires onSelect with the empty-string id', async () => {
    vi.spyOn(api, 'backends').mockResolvedValue(PAYLOAD)
    const onSelect = vi.fn()
    mount(<BackendSelector value="claude" onSelect={onSelect} />)
    const trigger = await screen.findByRole('button')
    await waitFor(() => expect(within(trigger).queryByText(/Claude Code/)).toBeTruthy())
    fireEvent.click(trigger)
    const list = await screen.findByRole('listbox')
    fireEvent.click(within(list).getByRole('option', { name: /Kiro CLI/ }))
    // kiro-cli's id is the empty string; it must round-trip as '' (a real id),
    // not be dropped as falsy.
    expect(onSelect).toHaveBeenCalledWith('')
  })

  it('renders invalid and unroutable rows with reasons and never as options', async () => {
    vi.spyOn(api, 'backends').mockResolvedValue(PAYLOAD)
    const onSelect = vi.fn()
    mount(<BackendSelector value="" onSelect={onSelect} />)
    const list = await openList()
    // Exactly the two SELECTABLE rows are options; the diagnostic rows are not.
    await waitFor(() => expect(within(list).getAllByRole('option')).toHaveLength(2))
    // The everyday picker shows a short pointer to Settings, not the operator's
    // raw diagnostic; the full reason is kept on the row's title.
    const noRoute = within(list).getByText('No Route').closest('div')!
    const bad = within(list).getByText('Bad Descriptor').closest('div')!
    expect(within(noRoute).getByText(/Not selectable — see Settings/)).toBeTruthy()
    expect(within(bad).getByText(/Invalid — see Settings/)).toBeTruthy()
    expect(noRoute.getAttribute('title')).toBe('no recognized routing declared')
    expect(bad.getAttribute('title')).toBe('missing executable; no argv')
    expect(within(list).queryByText(/no recognized routing declared/)).toBeNull()
    // A diagnostic label is present but is NOT an option (cannot be picked).
    expect(within(list).getByText('Bad Descriptor').closest('[role="option"]')).toBeNull()
    expect(within(list).getByText('No Route').closest('[role="option"]')).toBeNull()
  })

  it('honors disabled: the trigger cannot open the list', async () => {
    vi.spyOn(api, 'backends').mockResolvedValue(PAYLOAD)
    mount(<BackendSelector value="" onSelect={vi.fn()} disabled />)
    const trigger = await screen.findByRole('button')
    expect(trigger).toBeDisabled()
    fireEvent.click(trigger)
    expect(screen.queryByRole('listbox')).toBeNull()
  })
})
