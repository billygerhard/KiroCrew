// Transitions come from the engine, not from a map in the browser.
//
// SpecDetail used to hold an ADVANCE table keyed on the phase string: it decided
// that requirements was followed by design, and sent the chat message
// "Requirements approved — proceed to Phase 2 (Design)". Nothing had approved
// anything — the engine held no approval for the spec and would have refused the
// move at every gate — so the prompt asserted a fact that did not exist and the
// agent acted on it.
//
// What these tests pin: the control is offered only for the gate the ENGINE
// named, clicking it calls the engine's advance endpoint, and the authoring
// prompt that follows is keyed on the phase the ENGINE returned. A refused
// advance sends no prompt at all.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import React from 'react'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

vi.mock('../apps/spec-builder/components/ChatColumn', () => ({
  default: () => <div data-testid="chat-column" />,
}))

import SpecDetail from '../apps/spec-builder/components/SpecDetail'

let queryClient: QueryClient

const FILES = { 'requirements.md': '# r', 'design.md': '# d', 'tasks.md': '- [ ] one' }

function detail(engine: Record<string, unknown> | undefined) {
  return {
    name: 'demo',
    status: 'planning',
    // Deliberately the phase the OLD client-side map keyed on. Nothing may be
    // decided from it: a surface still reading it would offer an advance here
    // even when the engine named no gate.
    phase: 'requirements',
    running: false,
    spec_dir: '/p/.kiro/specs/demo',
    working_dir: '/p',
    slot_key: 'spec-builder-demo-abcd1234',
    files: FILES,
    context: { turns: 0, tool_calls: 0 },
    engine,
  }
}

interface Call {
  url: string
  body: unknown
}

function harness(
  engine: Record<string, unknown> | undefined,
  advanceResponse: { status: number; body: string },
) {
  const calls: Call[] = []
  vi.stubGlobal(
    'fetch',
    vi.fn().mockImplementation((url: string, init?: RequestInit) => {
      const method = init?.method || 'GET'
      if (method === 'POST') {
        calls.push({ url, body: init?.body ? JSON.parse(String(init.body)) : null })
        if (url.includes('/advance')) {
          return Promise.resolve({
            ok: advanceResponse.status < 400,
            status: advanceResponse.status,
            text: async () => advanceResponse.body,
            json: async () => JSON.parse(advanceResponse.body),
          })
        }
        return Promise.resolve({ ok: true, status: 200, text: async () => '{"ok":true}' })
      }
      return Promise.resolve({
        ok: true,
        status: 200,
        text: async () => JSON.stringify(detail(engine)),
      })
    }),
  )
  return calls
}

const READY_ENGINE = {
  addressable: true,
  current_gate: 'requirements',
  can_execute: false,
  gates: [{ gate: 'requirements', present: true, approved: false, stale: false }],
  execution_blocked_by: [
    { code: 'phase.approval-missing', message: 'No approval is recorded for design.' },
  ],
}

beforeEach(() => {
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  localStorage.clear()
})

afterEach(() => {
  vi.restoreAllMocks()
})

function mount() {
  render(
    <QueryClientProvider client={queryClient}>
      <SpecDetail name="demo" setErr={() => {}} />
    </QueryClientProvider>,
  )
}

describe('SpecDetail advance', () => {
  it('asks the engine to advance the gate the engine named', async () => {
    const calls = harness(READY_ENGINE, {
      status: 200,
      body: '{"ok":true,"gate":"requirements","from_phase":"design","to_phase":"design"}',
    })
    mount()

    const button = await waitFor(() => screen.getByRole('button', { name: /Approve → Design/i }))
    fireEvent.click(button)

    await waitFor(() => expect(calls.some((c) => c.url.includes('/advance'))).toBe(true))
    const advance = calls.find((c) => c.url.includes('/advance'))!
    // The gate is the engine's ``current_gate``, and the request goes to the
    // engine rather than into the chat transcript.
    expect((advance.body as { gate: string }).gate).toBe('requirements')
  })

  it('sends the authoring prompt for the phase the engine returned', async () => {
    const calls = harness(READY_ENGINE, {
      status: 200,
      body: '{"ok":true,"gate":"requirements","from_phase":"design","to_phase":"tasks"}',
    })
    mount()

    fireEvent.click(await waitFor(() => screen.getByRole('button', { name: /Approve → Design/i })))

    await waitFor(() => expect(calls.some((c) => c.url.includes('/message'))).toBe(true))
    const message = calls.find((c) => c.url.includes('/message'))!
    // ``to_phase`` was tasks, so the prompt is the one for authoring tasks --
    // even though the label said Design and the stale ``phase`` field said
    // requirements. A client-side map keyed on either would have sent the design
    // prompt here.
    expect((message.body as { text: string }).text).toContain('Phase 3 (Tasks)')
  })

  it('sends no prompt when the engine refuses the advance', async () => {
    const calls = harness(READY_ENGINE, {
      status: 409,
      body: '{"code":"approval_refused","error":"requirements.md fails validation"}',
    })
    mount()

    fireEvent.click(await waitFor(() => screen.getByRole('button', { name: /Approve → Design/i })))

    await waitFor(() => expect(calls.some((c) => c.url.includes('/advance'))).toBe(true))
    // The prompt used to be the whole action, so it went out regardless. Now it
    // is a consequence of an authorised transition, and there was none.
    expect(calls.filter((c) => c.url.includes('/message'))).toEqual([])
  })

  it('offers no advance when the engine names no gate', async () => {
    harness({ addressable: true, current_gate: null, can_execute: true }, { status: 200, body: '{}' })
    mount()

    await waitFor(() => screen.getByTestId('chat-column'))

    // ``phase`` is still "requirements" in this payload, which is exactly what
    // the removed map keyed on. Nothing offers a transition the engine did not.
    expect(screen.queryByRole('button', { name: /Approve →/i })).toBeNull()
  })

  it('offers no advance when the engine could not be asked', async () => {
    harness({ addressable: false, reason_code: 'engine_unavailable' }, { status: 200, body: '{}' })
    mount()

    await waitFor(() => screen.getByTestId('chat-column'))

    expect(screen.queryByRole('button', { name: /Approve →/i })).toBeNull()
  })
})

describe('SpecDetail build control', () => {
  it('is unavailable with the engine reason when the engine refuses execution', async () => {
    harness(READY_ENGINE, { status: 200, body: '{}' })
    mount()

    const button = await waitFor(() => screen.getByRole('button', { name: /Start building/i }))

    expect(button).toBeDisabled()
    // The engine's own reason, so a user reads why rather than clicking into a
    // refusal. tasks.md is present, which is all the old control ever checked.
    expect(button).toHaveAttribute('title', 'No approval is recorded for design.')
  })

  it('is available when the engine permits execution', async () => {
    harness(
      { addressable: true, current_gate: null, can_execute: true, execution_blocked_by: [] },
      { status: 200, body: '{}' },
    )
    mount()

    const button = await waitFor(() => screen.getByRole('button', { name: /Start building/i }))

    expect(button).not.toBeDisabled()
  })

  it('stays enabled when the engine did not answer, leaving the endpoint to decide', async () => {
    // An older backend returns no engine view. The surface must not invent a
    // verdict in either direction: the endpoint holds the gate, so the control
    // stays clickable and the refusal comes from there.
    harness(undefined, { status: 200, body: '{}' })
    mount()

    const button = await waitFor(() => screen.getByRole('button', { name: /Start building/i }))

    expect(button).not.toBeDisabled()
  })
})
