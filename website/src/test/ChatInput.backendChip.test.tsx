/**
 * Composer backend chip — names the AI backend serving this chat.
 *
 * Per-chat backend selection happens ONCE, on the welcome screen; after that
 * the binding is immutable. Without a chip an open chat gives no way to know
 * which backend is answering — indistinguishable from the global default —
 * which matters exactly when descriptor-defined backends exist. These tests
 * pin that the chip renders the bound backend's display name, that it answers
 * a click by explaining itself inline (it sits between two chips that open
 * pickers, so a silent no-op would read as broken) while never opening a
 * picker, that the tooltip carries the pinned/default distinction, and that
 * an absent label hides the chip entirely.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { fireEvent } from '@testing-library/react'
import i18next from 'i18next'
import { renderWithProviders } from './helpers'
import ChatInput from '../components/ChatInput'
import '../i18n/all'

vi.mock('../api/client', () => ({ api: {} }))

const props = (over: Record<string, unknown> = {}) => ({
  value: '',
  onChange: vi.fn(),
  onSend: vi.fn(),
  connected: true,
  // The control shelf that hosts the chip draws when the composer has a
  // project control; production always passes one. Without it the shelf
  // (and the chip) never renders, so give the chip a shelf to live in.
  onProjectClick: vi.fn(),
  ...over,
})

function backendChip(): HTMLElement | null {
  return document.querySelector('[data-testid="chat-input-backend-chip"]')
}

describe('ChatInput — backend chip', () => {
  afterEach(async () => { await i18next.changeLanguage('en') })

  it('renders the bound backend name and explains itself on click instead of opening a picker', () => {
    renderWithProviders(
      <ChatInput {...props({ backendLabel: 'Acme Agent' })} />,
    )
    const chip = backendChip()
    expect(chip).not.toBeNull()
    expect(chip!.textContent).toContain('Acme Agent')
    // Nothing is explained until asked; the caption is the click's only effect.
    expect(document.querySelector('[data-testid="chat-input-backend-chip-explained"]')).toBeNull()
    expect(chip!.getAttribute('aria-expanded')).toBe('false')
    fireEvent.click(chip!)
    const caption = document.querySelector('[data-testid="chat-input-backend-chip-explained"]')
    expect(caption).not.toBeNull()
    expect(caption!.getAttribute('role')).toBe('status')
    expect(caption!.textContent).toMatch(/Fixed when this chat was created/)
    expect(chip!.getAttribute('aria-expanded')).toBe('true')
    // No listbox/menu opened: the binding is immutable.
    expect(document.querySelector('[role="listbox"]')).toBeNull()
    fireEvent.click(chip!)
    expect(document.querySelector('[data-testid="chat-input-backend-chip-explained"]')).toBeNull()
  })

  it('an inheriting chat explains that it follows the default, not that a pin was fixed', () => {
    renderWithProviders(
      <ChatInput {...props({ backendLabel: 'Kiro CLI', backendIsInheritedDefault: true })} />,
    )
    fireEvent.click(backendChip()!)
    const caption = document.querySelector('[data-testid="chat-input-backend-chip-explained"]')
    expect(caption!.textContent).toMatch(/follows the default backend/)
    expect(caption!.textContent).not.toMatch(/Fixed when this chat was created/)
  })

  it('carries the pinned-variant tooltip on hover and for screen readers', () => {
    renderWithProviders(
      <ChatInput
        {...props({
          backendLabel: 'Acme Agent',
          backendTitle: 'AI backend serving this chat — fixed when the chat was created',
        })}
      />,
    )
    const chip = backendChip()!
    expect(chip.getAttribute('title')).toContain('fixed when the chat was created')
    expect(chip.getAttribute('aria-label')).toContain('fixed when the chat was created')
  })

  it('falls back to the label for title/aria when no variant title is given', () => {
    renderWithProviders(<ChatInput {...props({ backendLabel: 'Kiro CLI' })} />)
    const chip = backendChip()!
    expect(chip.getAttribute('title')).toBe('Kiro CLI')
    expect(chip.getAttribute('aria-label')).toBe('Kiro CLI')
  })

  it('renders no chip at all without a label', () => {
    renderWithProviders(<ChatInput {...props()} />)
    expect(backendChip()).toBeNull()
  })
})
