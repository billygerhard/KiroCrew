import { describe, expect, it, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

import { api } from '../api/client'
import BackendSelector from './BackendSelector'

function mount(node: React.ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<MemoryRouter><QueryClientProvider client={client}>{node}</QueryClientProvider></MemoryRouter>)
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

  it('on a phone the list escapes the composer, fits the screen, and opens upward from a bottom chip', async () => {
    // The composer chip sits at the bottom of a phone screen: an `absolute
    // top-full` list hung below it, off-screen, and was clipped by the scrolling
    // container. The list is portaled to <body>, `fixed`, clamped to the
    // viewport, and opens on the side with room.
    vi.spyOn(window, 'innerWidth', 'get').mockReturnValue(320)
    vi.spyOn(window, 'innerHeight', 'get').mockReturnValue(640)
    vi.spyOn(api, 'backends').mockResolvedValue(PAYLOAD)
    const onSelect = vi.fn()
    mount(<BackendSelector value="" onSelect={onSelect} variant="chip" />)
    const trigger = await screen.findByRole('button')
    vi.spyOn(trigger, 'getBoundingClientRect').mockReturnValue(
      { left: 250, right: 310, width: 60, top: 590, bottom: 618, height: 28, x: 250, y: 590, toJSON: () => ({}) } as DOMRect,
    )
    const list = await openList()
    expect(list.parentElement).toBe(document.body)
    expect(list.className.split(/\s+/)).toContain('fixed')
    expect(list.style.width).toBe('300px')
    expect(list.style.left).toBe('12px')
    expect(list.style.top).toBe('')
    expect(list.style.bottom).toBe('54px')
    // A tap on a portaled option must not be eaten by the outside-click closer.
    const option = within(list).getByRole('option', { name: /Claude Code/ })
    fireEvent.mouseDown(option)
    fireEvent.click(option)
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
    // The pointer to Settings is a real LINK to the AI Backends panel, and the
    // reason is visible text (a hover title is unreachable by keyboard/touch).
    const noRoute = within(list).getByTestId('backend-selector-unroutable-no-route')
    const bad = within(list).getByTestId('backend-selector-invalid-bad')
    const noRouteLink = within(noRoute).getByRole('link', { name: /Not selectable — see Settings/ })
    const badLink = within(bad).getByRole('link', { name: /Invalid — see Settings/ })
    expect(noRouteLink.getAttribute('href')).toMatch(/\/settings\/backends/)
    expect(badLink.getAttribute('href')).toMatch(/\/settings\/backends/)
    expect(within(noRoute).getByText('no recognized routing declared')).toBeTruthy()
    expect(within(bad).getByText('missing executable')).toBeTruthy()
    expect(within(bad).getByText('no argv')).toBeTruthy()
    // Each invalid reason is the standard error surface (an alert with the agent
    // hand-off), not a bare span -- the same surface the Settings panel uses.
    const firstReason = within(bad).getByTestId('backend-selector-invalid-bad-reason-0')
    expect(firstReason.getAttribute('role')).toBe('alert')
    expect(within(bad).getAllByRole('alert')).toHaveLength(2)
    expect(within(firstReason).getByRole('button', { name: /ask the agent/i })).toBeTruthy()
    expect(noRoute.getAttribute('title')).toBeNull()
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

  it('claims "no backends registered" only after the listing answered, never while it loads', async () => {
    let resolve!: (v: typeof PAYLOAD) => void
    vi.spyOn(api, 'backends').mockReturnValue(new Promise(r => { resolve = r }))
    mount(<BackendSelector value="" onSelect={vi.fn()} />)
    fireEvent.click(await screen.findByRole('button'))
    const list = await screen.findByRole('listbox')
    // In flight: the hook hands back empty arrays, which is not "none registered".
    expect(within(list).queryByText('No backends registered')).toBeNull()
    expect(within(list).queryByTestId('backend-selector-list-error')).toBeNull()
    resolve({ backends: [], invalid: [], unroutable: [] })
    await waitFor(() => expect(within(list).getByText('No backends registered')).toBeTruthy())
  })

  it('renders the unavailable notice, and no empty claim, when the listing fails', async () => {
    vi.spyOn(api, 'backends').mockRejectedValue(new Error('boom'))
    mount(<BackendSelector value="" onSelect={vi.fn()} />)
    fireEvent.click(await screen.findByRole('button'))
    const list = await screen.findByRole('listbox')
    await waitFor(() => expect(within(list).getByTestId('backend-selector-list-error')).toBeTruthy())
    expect(within(list).queryByText('No backends registered')).toBeNull()
  })
})
