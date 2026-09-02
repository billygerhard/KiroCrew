/**
 * The welcome screen's harness picker, and where it is allowed to appear.
 *
 * The picker is offered ONLY when the surface supplies a write path, and the
 * welcome screen is the only surface that has one: a harness binds when the
 * session starts and owns it for the session's whole life, so a picker on a
 * conversation already in progress could only mislabel it or destroy it. A
 * selector with no writer would read as a control that silently does nothing,
 * which is why the prop pair gates it rather than a boolean flag.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'

import { renderWithProviders } from '../test/helpers'
import WelcomeView from './WelcomeView'

const suggestions = vi.fn()
const harnesses = vi.fn()

vi.mock('../api/client', () => ({
  api: {
    suggestions: () => suggestions(),
    harnesses: () => harnesses(),
  },
}))

describe('WelcomeView harness picker', () => {
  beforeEach(() => {
    suggestions.mockReset()
    suggestions.mockResolvedValue({ suggestions: [], generated_at: 1, stale: false })
    harnesses.mockReset()
    harnesses.mockResolvedValue({
      harnesses: [
        { id: 'kiro', display_name: 'Kiro CLI', available: true, reason: '' },
        { id: 'kas', display_name: 'KAS', available: true, reason: '' },
      ],
      default: 'kiro',
    })
  })

  it('offers no picker without a write path', async () => {
    renderWithProviders(<WelcomeView setInput={vi.fn()} />)
    await waitFor(() => expect(suggestions).toHaveBeenCalled())
    expect(screen.queryByLabelText(/^Harness:/)).toBeNull()
    // Not even a listing request: a surface that cannot apply a pick has no
    // reason to ask which harnesses exist.
    expect(harnesses).not.toHaveBeenCalled()
  })

  it('reports the picked harness id to the surface that recreates the session', async () => {
    const onSelectHarness = vi.fn()
    renderWithProviders(
      <WelcomeView setInput={vi.fn()} harness="" onSelectHarness={onSelectHarness} />,
    )
    fireEvent.click(await screen.findByLabelText('Harness: Kiro CLI'))
    const kas = (await screen.findAllByRole('option')).find(r => r.textContent?.includes('KAS'))!
    fireEvent.click(kas)
    expect(onSelectHarness).toHaveBeenCalledWith('kas')
  })

  it('shows the session\'s own selection, not the default', async () => {
    renderWithProviders(
      <WelcomeView setInput={vi.fn()} harness="kas" onSelectHarness={vi.fn()} />,
    )
    await screen.findByLabelText('Harness: KAS')
  })

  it('blocks a second pick while one is being applied', async () => {
    const onSelectHarness = vi.fn()
    renderWithProviders(
      <WelcomeView setInput={vi.fn()} harness="" onSelectHarness={onSelectHarness} harnessPending />,
    )
    const trigger = await screen.findByLabelText('Harness: Kiro CLI')
    // Applying a pick recreates the session; a second one landing mid-flight
    // would create a second slot and leave one of them orphaned.
    expect(trigger).toBeDisabled()
  })
})
