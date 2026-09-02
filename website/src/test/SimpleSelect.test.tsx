import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import SimpleSelect from '../components/SimpleSelect'

/**
 * SimpleSelect — the shared Radix Select wrapper that replaced StyledSelect
 * (PublishHub, ArtifactDeployPage, KiroCrewAgentsPage) and backs SettingsSelect.
 * Runs against REAL @radix-ui/react-select: Radix Select opens on click in
 * jsdom (unlike DropdownMenu), so no mock is needed here.
 */
describe('SimpleSelect', () => {
  it('fires onChange with the selected value and shows it in the trigger', async () => {
    const onChange = vi.fn()
    const { rerender } = render(
      <SimpleSelect options={['static', 'fullstack']} value="static" onChange={onChange} aria-label="Policy tier" />
    )
    const trigger = screen.getByRole('combobox', { name: 'Policy tier' })
    expect(trigger).toHaveTextContent('static')
    fireEvent.click(trigger)
    fireEvent.click(await screen.findByRole('option', { name: 'fullstack' }))
    expect(onChange).toHaveBeenCalledWith('fullstack')
    rerender(<SimpleSelect options={['static', 'fullstack']} value="fullstack" onChange={onChange} aria-label="Policy tier" />)
    expect(trigger).toHaveTextContent('fullstack')
  })

  it('clearLabel renders as a selectable top row that clears to empty string', async () => {
    const onChange = vi.fn()
    render(
      <SimpleSelect options={['a', 'b']} value="a" onChange={onChange} clearLabel="— none —" aria-label="Copy from" />
    )
    fireEvent.click(screen.getByRole('combobox', { name: 'Copy from' }))
    fireEvent.click(await screen.findByRole('option', { name: '— none —' }))
    expect(onChange).toHaveBeenCalledWith('')
  })

  it('shows clearLabel in the trigger while value is empty', () => {
    render(
      <SimpleSelect options={['a', 'b']} value="" onChange={() => {}} clearLabel="— none —" aria-label="Copy from" />
    )
    expect(screen.getByRole('combobox', { name: 'Copy from' })).toHaveTextContent('— none —')
  })

  it('disabledOptions render but cannot be chosen', async () => {
    // A row whose presence is the explanation (a harness this build cannot
    // serve): hiding it would make "not offered" indistinguishable from
    // "does not exist", so the row renders with the click removed.
    const onChange = vi.fn()
    render(
      <SimpleSelect options={['kiro', 'codex']} value="kiro" onChange={onChange} disabledOptions={['codex']} aria-label="Harness" />
    )
    fireEvent.click(screen.getByRole('combobox', { name: 'Harness' }))
    const row = await screen.findByRole('option', { name: 'codex' })
    expect(row).toHaveAttribute('data-disabled')
    fireEvent.click(row)
    expect(onChange).not.toHaveBeenCalled()
  })

  it('the CURRENT value is never disabled, whatever the caller passed', async () => {
    // A stored setting has to stay displayable and re-selectable: Radix will
    // not render a disabled item as the selected one, so disabling the current
    // value would silently drop it from the trigger.
    const onChange = vi.fn()
    render(
      <SimpleSelect options={['kiro', 'codex']} value="codex" onChange={onChange} disabledOptions={['codex']} aria-label="Harness" />
    )
    const trigger = screen.getByRole('combobox', { name: 'Harness' })
    expect(trigger).toHaveTextContent('codex')
    fireEvent.click(trigger)
    const row = await screen.findByRole('option', { name: 'codex' })
    expect(row).not.toHaveAttribute('data-disabled')
  })

  it('Escape closes only the select, not a window-level Escape host (modal)', async () => {
    // Regression: Radix dismisses from a document listener, so without
    // stopPropagation in ui/select the same keydown reached the workspace
    // modal's window handler and closed the modal along with the dropdown.
    const onWindowEscape = vi.fn()
    const handler = (e: KeyboardEvent) => { if (e.key === 'Escape') onWindowEscape() }
    window.addEventListener('keydown', handler)
    try {
      render(<SimpleSelect options={['a', 'b']} value="a" onChange={() => {}} aria-label="Workspace" />)
      fireEvent.click(screen.getByRole('combobox', { name: 'Workspace' }))
      expect(await screen.findByRole('option', { name: 'b' })).toBeInTheDocument()
      fireEvent.keyDown(document.activeElement || document.body, { key: 'Escape' })
      expect(onWindowEscape).not.toHaveBeenCalled()
    } finally {
      window.removeEventListener('keydown', handler)
    }
  })

  it('action row fires onSelect instead of onChange', async () => {    const onChange = vi.fn()
    const onSelect = vi.fn()
    render(
      <SimpleSelect
        options={['default']} value="default" onChange={onChange}
        action={{ label: '+ New workspace…', onSelect }} aria-label="Workspace"
      />
    )
    fireEvent.click(screen.getByRole('combobox', { name: 'Workspace' }))
    fireEvent.click(await screen.findByRole('option', { name: '+ New workspace…' }))
    expect(onSelect).toHaveBeenCalledTimes(1)
    expect(onChange).not.toHaveBeenCalled()
  })
})
