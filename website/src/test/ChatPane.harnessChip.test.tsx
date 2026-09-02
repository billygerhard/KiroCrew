/**
 * T8 / R5.1: the ChatPane header renders a read-only chip naming the bound
 * harness's DISPLAY NAME, sourced from the same `/api/harnesses` listing the
 * welcome-screen picker uses (reused query, no new endpoint).
 *
 * Two cases the chip must get right:
 *  - a slot with an explicit harness → its display name, not the raw id.
 *  - a DEFAULT slot (`harness: ''`) → the listing's default row's display name,
 *    resolved from the same payload's `default` field, NOT left blank.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { render, screen, waitFor } from '@testing-library/react'
import type { RootState } from '../store'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from '../hooks/useTheme'
import chatReducer from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'

vi.mock('react-virtuoso', () => ({
  Virtuoso: ({ data, itemContent }: { data?: unknown[]; itemContent: (index: number, item: unknown) => ReactNode }) => (
    <div data-testid="virtuoso">{data?.map((d: unknown, i: number) => <div key={i}>{itemContent(i, d)}</div>)}</div>
  ),
}))

// The listing the picker + the chip both read. Two available harnesses, `kiro`
// the default (mirrors the wave-1 payload: rows + `default`). Hoisted so the
// vi.mock factory below can reference it.
const HARNESS_LISTING = vi.hoisted(() => ({
  harnesses: [
    { id: 'kiro', display_name: 'Kiro CLI', available: true, reason: '', serviceable: true },
    { id: 'kas', display_name: 'KAS', available: true, reason: '', serviceable: true },
  ],
  invalid: [],
  default: 'kiro',
}))

vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [], running: false, has_more: false, total: 0 }),
    chatHistory: vi.fn().mockResolvedValue({ sessions: [] }),
    models: vi.fn().mockResolvedValue([{ model_name: 'auto', description: 'Models chosen by task' }]),
    harnessModels: vi.fn().mockResolvedValue([{ model_name: 'auto', description: '' }]),
    harnesses: vi.fn().mockResolvedValue(HARNESS_LISTING),
    agents: vi.fn().mockResolvedValue([]),
    agentDetail: vi.fn().mockResolvedValue({}),
    workspaces: vi.fn().mockResolvedValue({ workspaces: [] }),
    spawnList: vi.fn().mockResolvedValue({ agents: [] }),
    dashboardConfig: vi.fn().mockResolvedValue({}),
  },
  SEARCH_MIN_CHARS: 2,
}))
vi.mock('../hooks/useVoiceInput', () => ({ useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }), voiceInputSupported: false }))
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Test', avatar: '' }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [{ name: 'default' }], defaultAgent: 'default' }) }))
vi.mock('../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <span>{content}</span> }))
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
})

import ChatPane from '../components/ChatPane'
import { api } from '../api/client'

function renderPane(slotKey: string, harness: string) {
  const store = configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null, connected: true,
        slots: [{ key: slotKey, messages: 0, running: false, mode: '', agent: 'default', model: '', harness, pending_approval: false, waiting_for_input: false, last_activity_ts: undefined }],
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
    } as Partial<RootState>,
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <Provider store={store}>
      <QueryClientProvider client={qc}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatPane slotKey={slotKey} />
          </MemoryRouter>
        </ThemeProvider>
      </QueryClientProvider>
    </Provider>,
  )
}

beforeEach(() => { vi.clearAllMocks() })

describe('ChatPane — harness chip (R5.1)', () => {
  it('shows the bound harness display name for a slot with an explicit harness', async () => {
    renderPane('pane-kas', 'kas')
    const chip = await waitFor(() => screen.getByTestId('chat-pane-harness-chip'))
    // Display name, not the raw id.
    await waitFor(() => expect(chip.textContent).toContain('KAS'))
    expect(chip.textContent).not.toContain('kas')
    // A PINNED slot keeps the fixity clause — the binding really was fixed at
    // creation, so both tooltip and aria-label carry the pinned variant.
    expect(chip.getAttribute('title')).toBe('AI harness serving this chat — fixed when the chat was created')
    expect(chip.getAttribute('aria-label')).toBe('AI harness serving this chat — fixed when the chat was created')
  })

  it('resolves the DEFAULT (empty) harness to the listing default row display name', async () => {
    renderPane('pane-default', '')
    const chip = await waitFor(() => screen.getByTestId('chat-pane-harness-chip'))
    // '' inherits the configured default, which the listing names `kiro` →
    // "Kiro CLI"; the chip must name the real default harness, never blank.
    await waitFor(() => expect(chip.textContent).toContain('Kiro CLI'))
    // A resolved-default slot was NOT pinned at creation, so it must use the
    // base string WITHOUT the fixity clause.
    await waitFor(() => expect(chip.getAttribute('title')).toBe('AI harness serving this chat'))
    expect(chip.getAttribute('aria-label')).toBe('AI harness serving this chat')
    expect(chip.getAttribute('title')).not.toContain('fixed when the chat')
  })

  it('does not flash the raw id during the loading window — neutral label, never the stored id', async () => {
    // A slow listing: the chip mounts before rows/default arrive.
    let resolveListing!: (v: unknown) => void
    ;(api.harnesses as ReturnType<typeof vi.fn>).mockReturnValueOnce(
      new Promise(res => { resolveListing = res }),
    )
    renderPane('pane-kas', 'kas')
    const chip = await waitFor(() => screen.getByTestId('chat-pane-harness-chip'))
    // While loading: the neutral default-harness label, NOT the raw stored id.
    expect(chip.textContent).toContain('Default harness')
    expect(chip.textContent).not.toContain('kas')
    expect(chip.textContent).not.toContain('KAS')
    // Once it lands the display name replaces the neutral label.
    resolveListing(HARNESS_LISTING)
    await waitFor(() => expect(chip.textContent).toContain('KAS'))
  })

  it('shows the neutral label when the listing fetch errors (no raw id)', async () => {
    ;(api.harnesses as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new Error('boom'))
    renderPane('pane-kas', 'kas')
    const chip = await waitFor(() => screen.getByTestId('chat-pane-harness-chip'))
    // isError empties the listing, so there is no row to name and no default to
    // resolve; the chip must not fall back to the raw stored id.
    await waitFor(() => expect(chip.textContent).toContain('Default harness'))
    expect(chip.textContent).not.toContain('kas')
  })

  it('falls back to the raw stored id (post-load) for an id the listing does not carry', async () => {
    // `ghost` is registered on no row and is not the default → after the listing
    // lands, the only honest label left is the stored id itself.
    renderPane('pane-ghost', 'ghost')
    const chip = await waitFor(() => screen.getByTestId('chat-pane-harness-chip'))
    await waitFor(() => expect(chip.textContent).toContain('ghost'))
    // A pinned unknown id still carries the pinned tooltip.
    expect(chip.getAttribute('title')).toBe('AI harness serving this chat — fixed when the chat was created')
  })

  it('renders a non-interactive chip (no role, onClick, or tabIndex)', async () => {
    renderPane('pane-kas', 'kas')
    const chip = await waitFor(() => screen.getByTestId('chat-pane-harness-chip'))
    await waitFor(() => expect(chip.textContent).toContain('KAS'))
    expect(chip.tagName).toBe('SPAN')
    expect(chip.getAttribute('role')).toBeNull()
    expect(chip.getAttribute('tabindex')).toBeNull()
    expect(chip.onclick).toBeNull()
  })
})

// REGRESSION (found by hands-on testing 2026-09-02): the header chip only
// exists in the pane's own header bar, which the main single-chat layout
// (frameless ChatPane / ChatPage) never renders — so the harness was
// invisible exactly where users live. The chip now ALSO renders in the
// composer shelf via ChatInput's harnessLabel prop, which every layout uses.
it('renders the harness chip in the composer shelf (frameless layout)', async () => {
  renderFramelessPane('slot-shelf', 'kas')
  const chip = await screen.findByTestId('chat-input-harness-chip')
  await waitFor(() => expect(chip).toHaveTextContent('KAS'))
  expect(screen.queryByTestId('chat-pane-harness-chip')).toBeNull()
})

/** Same store/providers as renderPane, but frameless — the main single-chat
 *  layout, which renders NO pane header and therefore no header chip. */
function renderFramelessPane(slotKey: string, harness: string) {
  const store = configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null, connected: true,
        slots: [{ key: slotKey, messages: 0, running: false, mode: '', agent: 'default', model: '', harness, pending_approval: false, waiting_for_input: false, last_activity_ts: undefined }],
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
    } as Partial<RootState>,
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <Provider store={store}>
      <QueryClientProvider client={qc}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatPane slotKey={slotKey} frameless />
          </MemoryRouter>
        </ThemeProvider>
      </QueryClientProvider>
    </Provider>,
  )
}
