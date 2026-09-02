/**
 * T8 / R5.2: a session list ROW exposes its bound harness in the row tooltip —
 * but ONLY when the harness is set and differs from the configured default, so a
 * default-harness row gains no clutter. The harness id is resolved to a display
 * name via the same `/api/harnesses` listing the picker uses.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'

vi.mock('framer-motion', async () => {
  const React = await import('react')
  const FRAMER_PROPS = new Set([
    'layout', 'layoutId', 'layoutScroll', 'initial', 'animate', 'exit',
    'transition', 'variants', 'whileHover', 'whileTap', 'whileInView',
    'drag', 'dragConstraints', 'dragElastic', 'onAnimationComplete',
  ])
  const make = (tag: string) =>
    React.forwardRef((props: Record<string, unknown>, ref: React.Ref<unknown>) => {
      const clean: Record<string, unknown> = {}
      for (const k of Object.keys(props)) {
        if (k === 'children') continue
        if (FRAMER_PROPS.has(k)) continue
        clean[k] = props[k]
      }
      return React.createElement(tag, { ...clean, ref }, props.children as React.ReactNode)
    })
  const motion = new Proxy({}, { get: (_t, tag: string) => make(tag) })
  return {
    motion,
    AnimatePresence: ({ children }: { children?: React.ReactNode }) =>
      React.createElement(React.Fragment, null, children),
    LayoutGroup: ({ children }: { children?: React.ReactNode }) =>
      React.createElement(React.Fragment, null, children),
  }
})

vi.mock('../components/ProjectPicker', () => ({ default: () => null }))
vi.mock('../pages/chat/ChatSettings', () => ({
  loadChatConfig: () => ({ tagColumnsEnabled: false, confirmCloseSession: false }),
  saveChatConfig: vi.fn(),
}))

const HARNESS_LISTING = vi.hoisted(() => ({
  harnesses: [
    { id: 'kiro', display_name: 'Kiro CLI', available: true, reason: '', serviceable: true },
    { id: 'kas', display_name: 'KAS', available: true, reason: '', serviceable: true },
  ],
  invalid: [],
  default: 'kiro',
}))

// Real listing for `harnesses`, empty for everything else (mirrors the
// noSplitEntry harness's catch-all, with one method overridden).
const harnessesMock = vi.hoisted(() => vi.fn())
vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy({} as Record<string, unknown>, {
    get: (_t, prop: string) => {
      if (prop === 'harnesses') return harnessesMock
      return vi.fn().mockResolvedValue([])
    },
  }),
}))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockImplementation((q: string) => ({
    matches: false, media: q, onchange: null,
    addListener: vi.fn(), removeListener: vi.fn(),
    addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn(),
  })),
})

import ChatSidebar from '../pages/ChatSidebar'
import type { RootState } from '../store'
import type { ChatSlot } from '../types'

const SLOTS = [
  { key: 'k-kas', title: 'KAS session', messages: 1, running: false, mode: '', created: '', last_ts: '2026-01-03T00:00:00Z', harness: 'kas' },
  { key: 'k-default', title: 'Default session', messages: 1, running: false, mode: '', created: '', last_ts: '2026-01-02T00:00:00Z', harness: '' },
  { key: 'k-kiro', title: 'Kiro session', messages: 1, running: false, mode: '', created: '', last_ts: '2026-01-01T00:00:00Z', harness: 'kiro' },
]

function renderSidebar() {
  const store = createTestStore({
    dashboard: {
      status: {}, connected: true, slots: SLOTS, approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      slotsLoaded: true,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as unknown as RootState['dashboard'],
    chat: {
      activeSlot: null, slotStatusDetail: {}, subagents: {}, slotActivity: {},
      goalLoops: {}, subagentQueued: {}, workflowRuns: {}, pendingQuestions: {},
    } as unknown as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  qc.setQueryData(['chat-folders'], [])
  return render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={SLOTS as unknown as ChatSlot[]} activeSlot={null} unreadSlots={[]}
              history={[]} historyHasMore={false} defaultAgent="" installedAgents={[]}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
}

/** The title element for a row is the one whose text is the session title. */
function titleFor(container: HTMLElement, title: string): HTMLElement {
  const el = Array.from(container.querySelectorAll<HTMLElement>('[title]'))
    .find(e => (e.getAttribute('title') ?? '').startsWith(title))
  if (!el) throw new Error(`no title element starting with "${title}"`)
  return el
}

beforeEach(() => { localStorage.clear(); localStorage.setItem('mc-session-stale-collapse-ms', '0'); harnessesMock.mockReset(); harnessesMock.mockResolvedValue(HARNESS_LISTING) })
afterEach(() => vi.clearAllMocks())

describe('chat sidebar — harness in the row tooltip (R5.2)', () => {
  it('appends the harness display name to a NON-default row tooltip', async () => {
    const { container } = renderSidebar()
    await waitFor(() => {
      const t = titleFor(container, 'KAS session').getAttribute('title') ?? ''
      // Resolved display name, not the raw id, and carried in the tooltip only.
      expect(t).toContain('Running on KAS')
    })
  })

  it('does NOT add a harness tooltip for a default (empty) harness row', async () => {
    const { container } = renderSidebar()
    // Wait for the listing to LAND (display name present) before asserting the
    // absence — 'Running on' alone is also satisfied by the loading-window raw
    // id, which would let a loading-state regression pass.
    await waitFor(() => expect(titleFor(container, 'KAS session').getAttribute('title')).toContain('Running on KAS'))
    const tDefault = titleFor(container, 'Default session').getAttribute('title') ?? ''
    expect(tDefault).not.toContain('Running on')
  })

  it('does NOT add a harness tooltip when the stored harness equals the default', async () => {
    const { container } = renderSidebar()
    await waitFor(() => expect(titleFor(container, 'KAS session').getAttribute('title')).toContain('Running on KAS'))
    // `kiro` IS the default, so an explicit `kiro` row is redundant → silent.
    const tKiro = titleFor(container, 'Kiro session').getAttribute('title') ?? ''
    expect(tKiro).not.toContain('Running on')
  })

  it('stays SILENT during the loading window — no raw id flashed on any row', async () => {
    let resolveListing!: (v: unknown) => void
    harnessesMock.mockReturnValueOnce(new Promise(res => { resolveListing = res }))
    const { container } = renderSidebar()
    // Before the listing lands there is no default to compare against, so the
    // KAS row must NOT surface its harness (raw or otherwise).
    const tLoading = titleFor(container, 'KAS session').getAttribute('title') ?? ''
    expect(tLoading).not.toContain('Running on')
    expect(tLoading).not.toContain('kas')
    // Once it lands the display-name tooltip appears.
    resolveListing(HARNESS_LISTING)
    await waitFor(() => expect(titleFor(container, 'KAS session').getAttribute('title')).toContain('Running on KAS'))
  })

  it('stays SILENT when the listing fetch errors', async () => {
    harnessesMock.mockReset()
    harnessesMock.mockRejectedValue(new Error('boom'))
    const { container } = renderSidebar()
    // isError empties the listing permanently → no tooltip ever, and never the
    // raw id.
    await waitFor(() => {
      const t = titleFor(container, 'KAS session').getAttribute('title') ?? ''
      expect(t).not.toContain('Running on')
      expect(t).not.toContain('kas')
    })
  })
})
