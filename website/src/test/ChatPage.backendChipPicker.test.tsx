/**
 * The composer backend chip is a real picker, not a read-only label.
 *
 * The endpoint (`POST /api/chat/slots/{slot}/backend`) always accepted any slot —
 * its only refusals are a turn in flight and a parent with children — and the
 * welcome surface's picker already called it. What was missing was a control on a
 * chat that has turns, so the chip read as a fixed label. It used to also SAY so:
 * its aria-label claimed the backend was "fixed when the chat was created", which
 * is the claim this change makes false.
 *
 * These tests pin the live-chat contract: the chip opens the SAME listbox the
 * welcome surface renders, a pick goes through the mutation endpoint and writes
 * the store's slot row, a refused switch is visible (the chip has no ErrorNotice
 * slot, so it uses the shared notice toast its model/agent siblings use), and the
 * trigger is disabled exactly when the server would answer 409.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, act, waitFor, fireEvent, cleanup } from '@testing-library/react'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import chatReducer from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'
import { ThemeProvider } from '../hooks/useTheme'
import type { RootState } from '../store'

interface VirtuosoMockProps {
  data?: unknown[]
  itemContent: (index: number, item: unknown) => React.ReactNode
}
vi.mock('react-virtuoso', () => ({ Virtuoso: ({ data, itemContent }: VirtuosoMockProps) => <div data-testid="virtuoso">{data?.map((d: unknown, i: number) => <div key={i}>{itemContent(i, d)}</div>)}</div> }))

// The slot detail read is what tells the page a turn is in flight, so a test that
// wants the page to believe a turn is running has to say so HERE too — a
// preloaded `slotRunning: true` is overwritten by this answer the moment it
// lands. Shared through `hoisted` because the `vi.mock` factory is hoisted above
// the module body's declarations.
const h = vi.hoisted(() => ({ running: false }))

vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    chatSlotDetail: vi.fn().mockImplementation(async () => ({ messages: [{ role: 'assistant', content: 'hi', cls: '' }], running: h.running, has_more: false, total: 1 })),
    chatHistory: vi.fn().mockResolvedValue({ sessions: [] }),
    models: vi.fn().mockResolvedValue([{ model_name: 'auto', description: 'Auto' }]),
    agents: vi.fn().mockResolvedValue([]),
    agentDetail: vi.fn().mockResolvedValue({ model: 'auto' }),
    agentResolvedModel: vi.fn().mockResolvedValue({ model: 'auto' }),
    chatSlotModel: vi.fn().mockResolvedValue({ ok: true }),
    // The endpoint under test. Echoes the stored value like the server does, and
    // a no-op pick (same value) is the server's own behavior, not a reset.
    chatSlotBackend: vi.fn().mockImplementation(async (_slot: string, backend: string) => ({ ok: true, backend })),
    backends: vi.fn().mockResolvedValue({
      backends: [
        { id: '', label: 'Kiro CLI', is_global_default: true },
        { id: 'acme', label: 'Acme Agent', is_global_default: false },
        { id: 'kas', label: 'KAS', is_global_default: false },
      ],
      invalid: [],
      unroutable: [],
    }),
    workspaces: vi.fn().mockResolvedValue({ workspaces: [] }),
    slackChannels: vi.fn().mockResolvedValue([]),
    spawnList: vi.fn().mockResolvedValue({ agents: [] }),
  },
  SEARCH_MIN_CHARS: 2,
}))
vi.mock('../hooks/useVoiceInput', () => ({ useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }), voiceInputSupported: false }))
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Test', avatar: '' }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [{ name: 'kirocrew' }], defaultAgent: 'kirocrew' }) }))
vi.mock('../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <span>{content}</span> }))
vi.mock('../components/MarkdownPanel', () => ({ default: () => null }))
vi.mock('../pages/chat/ActivityViewer', () => ({ default: () => null }))
vi.mock('../components/DetailPanel', () => ({ default: () => null }))
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
})

import ChatPage from '../pages/ChatPage'

/**
 * A chat that HAS turns, which is the whole point: `messages.length > 0` is what
 * put the old chip behind its read-only behavior. `acp_backend` follows the
 * welcome suite's convention — three distinguishable states, `null` (inherit),
 * `''` (a real kiro-cli pin) and a named pin.
 */
function makeStore(acp_backend: string | null | undefined, running = false) {
  h.running = running
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null,
        slots: [{ key: 'slot-a', messages: 2, running, mode: '', agent: 'kirocrew', model: 'auto', acp_backend, pending_approval: false, waiting_for_input: false, last_activity_ts: undefined }],
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
      chat: {
        activeSlot: 'slot-a', messages: [{ role: 'assistant', content: 'hi', cls: '' }],
        slotRunning: running, slotStopping: false, slotState: 'idle',
        history: [], historyHasMore: false, pendingInput: null,
        subagents: {}, toolLog: [], activityOpen: false, activityTab: 'tools',
        slotHasMore: false, slotOldestIndex: 0, loadingOlder: false,
        slotStatusDetail: {}, slotContextPct: {}, slotActivity: {}, slotHistory: [],
        historyOffset: 0, _wsChunkedDuringFetch: false,
        slotMessages: {}, slotLoading: false,
      } as unknown as RootState['chat'],
      notifications: { items: [] } as unknown as RootState['notifications'],
    },
  })
}

async function renderChat(acp_backend: string | null | undefined, running = false) {
  const store = makeStore(acp_backend, running)
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  await act(async () => {
    render(
      <QueryClientProvider client={qc}>
        <Provider store={store}>
          <ThemeProvider>
            <MemoryRouter><ChatPage /></MemoryRouter>
          </ThemeProvider>
        </Provider>
      </QueryClientProvider>,
    )
  })
  const chip = await screen.findByTestId('chat-input-backend-chip')
  await waitFor(() => expect(chip).toBeTruthy())
  return { store, chip: () => screen.getByTestId('chat-input-backend-chip') as HTMLButtonElement }
}

/** Open the chip and return its rows, as the user would reach them. */
async function openRows() {
  const chip = screen.getByTestId('chat-input-backend-chip') as HTMLButtonElement
  await act(async () => { fireEvent.click(chip) })
  await waitFor(() => expect(screen.getByRole('listbox')).toBeTruthy())
  return screen.getAllByRole('option') as HTMLButtonElement[]
}

const slotBackend = (store: ReturnType<typeof makeStore>) =>
  (store.getState() as RootState).dashboard.slots.find(s => s.key === 'slot-a')?.acp_backend

beforeEach(() => {
  sessionStorage.clear()
  localStorage.clear()
  vi.clearAllMocks()
})
afterEach(() => { cleanup() })

describe('ChatPage — the composer backend chip is a picker on a chat with turns', { timeout: 20_000 }, () => {
  it('opens a listbox of the selectable backends, rather than only explaining itself', async () => {
    await renderChat('acme')
    const chip = screen.getByTestId('chat-input-backend-chip') as HTMLButtonElement
    // The affordance says so up front: this is a control, not a label.
    expect(chip.tagName).toBe('BUTTON')
    expect(chip.getAttribute('aria-haspopup')).toBe('listbox')
    expect(chip.getAttribute('aria-expanded')).toBe('false')
    // The read-only chip answered a click by toggling an explanation. That is
    // gone: a picker opens its list instead.
    expect(screen.queryByTestId('chat-input-backend-chip-explained')).toBeNull()

    const rows = await openRows()
    expect(screen.getByTestId('chat-input-backend-chip').getAttribute('aria-expanded')).toBe('true')
    // The SAME rows the welcome picker offers — one implementation, so the two
    // surfaces cannot drift on what is selectable.
    expect(rows.map(r => r.textContent)).toEqual(
      expect.arrayContaining([expect.stringContaining('Kiro CLI'), expect.stringContaining('Acme Agent'), expect.stringContaining('KAS')]),
    )
  })

  it('writes a pick to the existing slot through the mutation endpoint', async () => {
    const { api } = await import('../api/client')
    const { store } = await renderChat('')
    const rows = await openRows()
    const kas = rows.find(r => r.textContent?.includes('KAS'))!
    await act(async () => { fireEvent.click(kas) })

    await waitFor(() => expect(api.chatSlotBackend).toHaveBeenCalledWith('slot-a', 'kas'))
    // The store's slot row carries the stored value, so the chip and the
    // backend-keyed model list follow the pick without waiting for a rebroadcast.
    await waitFor(() => expect(slotBackend(store)).toBe('kas'))
  })

  it("writes a kiro-cli pick as '' (a real pin), not as a clear", async () => {
    const { api } = await import('../api/client')
    const { store } = await renderChat('acme')
    const rows = await openRows()
    const kiro = rows.find(r => r.textContent?.includes('Kiro CLI'))!
    await act(async () => { fireEvent.click(kiro) })

    // `null` would CLEAR the pin and hand the chat back to the global default;
    // `''` is kiro-cli's own id, so this holds the chat on Kiro.
    await waitFor(() => expect(api.chatSlotBackend).toHaveBeenCalledWith('slot-a', ''))
    await waitFor(() => expect(slotBackend(store)).toBe(''))
  })

  it('shows the slot\'s STORED backend as the chosen row, so a pin set elsewhere is visible', async () => {
    await renderChat('acme')
    const rows = await openRows()
    const chosen = rows.filter(r => r.getAttribute('aria-selected') === 'true').map(r => r.textContent)
    expect(chosen).toHaveLength(1)
    expect(chosen[0]).toContain('Acme Agent')
  })

  it('shows the global default as chosen for a slot with no pin', async () => {
    await renderChat(undefined)
    const rows = await openRows()
    const chosen = rows.filter(r => r.getAttribute('aria-selected') === 'true').map(r => r.textContent)
    expect(chosen).toHaveLength(1)
    expect(chosen[0]).toContain('Kiro CLI')
  })

  it('disables the trigger while a turn is in flight, and does not call the endpoint', async () => {
    const { api } = await import('../api/client')
    // The server answers 409 for a turn in flight (the session reset would tear
    // down the streaming turn), so the control is disabled instead of offering a
    // pick that is guaranteed to be refused.
    await renderChat('acme', true)
    const chip = screen.getByTestId('chat-input-backend-chip') as HTMLButtonElement
    await waitFor(() => expect(chip.disabled).toBe(true))
    // Still readable while disabled: the serving backend must not vanish.
    expect(chip.textContent).toContain('Acme Agent')

    await act(async () => { fireEvent.click(chip) })
    expect(api.chatSlotBackend).not.toHaveBeenCalled()
    expect(screen.queryByRole('listbox')).toBeNull()
  })

  it('surfaces a refused switch, and leaves the running backend untouched', async () => {
    const { api } = await import('../api/client')
    ;(api.chatSlotBackend as unknown as ReturnType<typeof vi.fn>).mockImplementationOnce(
      async () => ({ ok: false, error: 'a turn is in flight' }),
    )
    const { store } = await renderChat('')
    const rows = await openRows()
    const acme = rows.find(r => r.textContent?.includes('Acme Agent'))!
    await act(async () => { fireEvent.click(acme) })

    // The chip has no ErrorNotice slot of its own, so a refusal goes to the
    // shared notice toast that the model and agent switches already use. Silent
    // refusal would leave the chip claiming a control that does nothing.
    await waitFor(() => expect((store.getState() as RootState).chat.agentSwitchNotice?.message).toMatch(/turn is in flight/))
    // And the stored value is unchanged: the chip keeps showing what still runs.
    expect(slotBackend(store)).toBe('')
  })
})
