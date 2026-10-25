import { describe, expect, it, vi, afterEach } from 'vitest'
import { renderHook, waitFor, act, cleanup } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import { api } from '../api/client'
import type { ChatSlot } from '../types'
import { useComposerBackendPick } from '../pages/chat/page/backendPick'

function wrapper({ children }: { children: React.ReactNode }) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>
}

const ROWS = {
  backends: ['claude-seat1', 'claude-seat2'].map(id => ({
    id, policy_id: id, label: id.replace('claude-seat', 'Claude Seat '), selectable: true,
    independent_setup: false, installed: 'installed', missing_components: [], install_command: '', restart_required: false,
  })),
}

function stub() {
  vi.spyOn(api, 'acpBackends').mockResolvedValue(ROWS as never)
  vi.spyOn(api, 'kirocrewConfig').mockResolvedValue({ agent: { acp_backend: 'claude-seat1' } })
}

const SLOT = { key: 'chat-1', backend: 'claude-seat1' } as ChatSlot

describe('useComposerBackendPick', () => {
  afterEach(() => { cleanup(); vi.restoreAllMocks() })

  it('switches between turns through the slot backend route and stores the pick', async () => {
    stub()
    const post = vi.spyOn(api, 'chatSlotBackend').mockResolvedValue({ ok: true, backend: 'claude-seat2' })
    const dispatch = vi.fn()
    const { result } = renderHook(
      () => useComposerBackendPick({ activeSlot: 'chat-1', currentSlot: SLOT, slotRunning: false, dispatch }),
      { wrapper },
    )
    await waitFor(() => expect(result.current.onSelectBackend).toBeTypeOf('function'))
    expect(result.current.backendValue).toBe('claude-seat1')
    expect(result.current.backendDisabled).toBe(false)
    await act(async () => { await result.current.onSelectBackend!('claude-seat2') })
    expect(post).toHaveBeenCalledWith('chat-1', 'claude-seat2')
    expect(dispatch).toHaveBeenCalledWith(expect.objectContaining({
      payload: { key: 'chat-1', backend: 'claude-seat2', model: '' },
    }))
  })

  it('is locked while a turn runs', async () => {
    stub()
    const { result } = renderHook(
      () => useComposerBackendPick({ activeSlot: 'chat-1', currentSlot: SLOT, slotRunning: true, dispatch: vi.fn() }),
      { wrapper },
    )
    expect(result.current.backendDisabled).toBe(true)
  })

  it('a refused switch reaches the notice toast and stores nothing', async () => {
    stub()
    vi.spyOn(api, 'chatSlotBackend').mockRejectedValue(new Error('turn in flight'))
    vi.spyOn(console, 'error').mockImplementation(() => {})
    const dispatch = vi.fn()
    const { result } = renderHook(
      () => useComposerBackendPick({ activeSlot: 'chat-1', currentSlot: SLOT, slotRunning: false, dispatch }),
      { wrapper },
    )
    await waitFor(() => expect(result.current.onSelectBackend).toBeTypeOf('function'))
    await act(async () => { await result.current.onSelectBackend!('claude-seat2') })
    const types = dispatch.mock.calls.map(c => (c[0] as { type: string }).type)
    expect(types.some(t => t.includes('setAgentSwitchNotice'))).toBe(true)
    expect(types.some(t => t.includes('updateSlot'))).toBe(false)
  })
})
