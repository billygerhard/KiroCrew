/**
 * Picking a harness on the welcome screen recreates the session — and a model id
 * must not ride along across the change.
 *
 * A model id belongs to ONE harness's catalog. The create stores whatever it is
 * given verbatim, nothing validates a model against a harness, and the composer's
 * own picker (correctly re-fetched for the new harness) would not even list it — so
 * carrying it forward binds the new session to a model the picked harness never
 * advertised, and the first turn fails at the wire with an error the user cannot
 * connect to their choice. Dropping it inherits the harness's own default, which is
 * what "I picked a backend, not a model" means.
 *
 * Re-picking the SAME harness is not a change and keeps the model, including when
 * the slot stores no harness and the pick names the harness it already resolves to.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { render, screen, fireEvent, act, waitFor } from '@testing-library/react'
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

const createChatSlot = vi.fn()
const harnesses = vi.fn()

vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [], running: false, has_more: false, total: 0 }),
    chatHistory: vi.fn().mockResolvedValue({ sessions: [] }),
    models: vi.fn().mockResolvedValue([]),
    harnessModels: vi.fn().mockResolvedValue([]),
    harnesses: () => harnesses(),
    agents: vi.fn().mockResolvedValue([]),
    agentDetail: vi.fn().mockResolvedValue({}),
    workspaces: vi.fn().mockResolvedValue({ workspaces: [] }),
    slackChannels: vi.fn().mockResolvedValue([]),
    spawnList: vi.fn().mockResolvedValue({ agents: [] }),
    suggestions: vi.fn().mockResolvedValue({ suggestions: [], generated_at: 1, stale: false }),
    createChatSlot: (...a: unknown[]) => createChatSlot(...a),
    deleteChatSlot: vi.fn().mockResolvedValue({ ok: true }),
  },
  SEARCH_MIN_CHARS: 2,
}))
vi.mock('../hooks/useVoiceInput', () => ({ useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }), voiceInputSupported: false }))
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Test', avatar: '' }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [], defaultAgent: 'default' }) }))
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

/** One empty slot (so the welcome screen with the harness picker renders) bound
 *  to `harness` and pinned to `model`. */
function makeStore(harness: string, model: string) {
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null, connected: true, slotsLoaded: true,
        slots: [
          {
            key: 'slot-a', messages: 0, running: false, mode: '', pending_approval: false,
            waiting_for_input: false, last_activity_ts: undefined, harness, model, agent: 'default',
          },
        ],
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
      chat: {
        activeSlot: 'slot-a', messages: [],
        slotRunning: false, slotStopping: false, slotState: 'idle',
        history: [], historyHasMore: false, pendingInput: '',
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

async function renderPage(store: ReturnType<typeof makeStore>) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  await act(async () => {
    render(
      <QueryClientProvider client={qc}>
        <Provider store={store}>
          <ThemeProvider>
            <MemoryRouter initialEntries={['/chat']}><ChatPage /></MemoryRouter>
          </ThemeProvider>
        </Provider>
      </QueryClientProvider>,
    )
  })
}

/** Open the harness dropdown and click the row whose label contains `name`. */
async function pickHarness(name: string) {
  const trigger = await screen.findByLabelText(/^Harness:/)
  fireEvent.click(trigger)
  const row = (await screen.findAllByRole('option')).find(r => r.textContent?.includes(name))!
  await act(async () => { fireEvent.click(row) })
}

/** The `model` argument of the create call — positional arg 2 of createChatSlot. */
function createdModel(): unknown {
  return createChatSlot.mock.calls[0]?.[2]
}

/** The `harness` argument — positional arg 9. */
function createdHarness(): unknown {
  return createChatSlot.mock.calls[0]?.[9]
}

describe('harness pick recreating the session', { timeout: 20_000 }, () => {
  beforeEach(() => {
    sessionStorage.clear()
    localStorage.clear()
    createChatSlot.mockReset()
    createChatSlot.mockResolvedValue({ key: 'slot-b', messages: 0, running: false })
    harnesses.mockReset()
    harnesses.mockResolvedValue({
      harnesses: [
        { id: 'kiro', display_name: 'Kiro CLI', available: true, reason: '' },
        { id: 'kas', display_name: 'KAS', available: true, reason: '' },
      ],
      default: 'kiro',
    })
  })

  it('drops the model when the picked harness differs', async () => {
    await renderPage(makeStore('kiro', 'claude-opus-4.8'))
    await pickHarness('KAS')
    await waitFor(() => expect(createChatSlot).toHaveBeenCalled())
    expect(createdHarness()).toBe('kas')
    // Not 'claude-opus-4.8': KAS never advertised it, and the picker rendered
    // beside this session would not list it either.
    expect(createdModel()).toBeUndefined()
  })

  it('keeps the model when the same harness is re-picked', async () => {
    await renderPage(makeStore('kas', 'kas-fast'))
    await pickHarness('KAS')
    await waitFor(() => expect(createChatSlot).toHaveBeenCalled())
    expect(createdHarness()).toBe('kas')
    // Same catalog, so the pin survives — dropping it here would silently discard
    // a choice the user made for an action that changed nothing.
    expect(createdModel()).toBe('kas-fast')
  })

  it('treats naming the resolved default as the same harness', async () => {
    // The slot stores no harness, which means "inherit the configured default" —
    // and the listing says that default IS kiro. Comparing the raw stored value
    // would read this as a change and drop a model from the very catalog still
    // in force.
    await renderPage(makeStore('', 'claude-opus-4.8'))
    await pickHarness('Kiro CLI')
    await waitFor(() => expect(createChatSlot).toHaveBeenCalled())
    expect(createdHarness()).toBe('kiro')
    expect(createdModel()).toBe('claude-opus-4.8')
  })
})
