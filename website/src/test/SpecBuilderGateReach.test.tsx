// Every gate the engine names is leavable from the surface, and an approval can
// be recorded from the surface at all.
//
// The defect these pin, exactly as it was reachable: `GATE_LABEL_KEY` in
// SpecDetail held phrases for requirements and design only, and the advance
// control rendered only when that table had an entry. So a spec driven through
// the two advances the surface could perform landed on the engine's `tasks` gate
// and stopped there forever -- no control offered a way to leave it, no control
// recorded the approval it was missing, and "Start building" stayed disabled
// with the engine's `phase.approval-missing` as its tooltip. `specApi.approve`
// existed with a working endpoint behind it and zero callers.
//
// The existing advance tests all render `current_gate: 'requirements'`, which is
// the one gate the table had a phrase for -- the case that worked. These render
// the last gate, and a gate no phrase exists for at all, because a spec type
// with a different phase plan reaches the same dead end on its own gates.
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

/** The engine's view of a spec parked on the LAST gate of a feature plan: both
 *  earlier documents approved, tasks written and unapproved, execution refused
 *  for exactly that reason. This is the state the reviewer reached by driving the
 *  engine through every advance the surface could perform. */
const TASKS_GATE = {
  addressable: true,
  engine_phase: 'tasks',
  current_gate: 'tasks',
  can_execute: false,
  gates: [
    { gate: 'requirements', present: true, approved: true, stale: false },
    { gate: 'design', present: true, approved: true, stale: false },
    { gate: 'tasks', present: true, approved: false, stale: false },
  ],
  execution_blocked_by: [
    { code: 'phase.approval-missing', message: 'No approval is recorded for tasks.' },
  ],
}

/** The same spec once the approval is recorded: the engine reports the gate
 *  approved, names no further gate, and permits the build. */
const TASKS_APPROVED = {
  addressable: true,
  engine_phase: 'ready',
  current_gate: null,
  can_execute: true,
  gates: [
    { gate: 'requirements', present: true, approved: true, stale: false },
    { gate: 'design', present: true, approved: true, stale: false },
    { gate: 'tasks', present: true, approved: true, stale: false, approver: 'ada' },
  ],
  execution_blocked_by: [],
}

function detail(engine: Record<string, unknown>) {
  return {
    name: 'demo',
    status: 'planning',
    phase: 'tasks',
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

/**
 * Serves *before* until a POST lands, then *after*. The detail query refetches
 * on every mutation, so this is how a recorded approval becomes visible to the
 * controls that read the engine view -- which is the whole reachability claim.
 */
function harness(before: Record<string, unknown>, after?: Record<string, unknown>) {
  const calls: Call[] = []
  let engine = before
  vi.stubGlobal(
    'fetch',
    vi.fn().mockImplementation((url: string, init?: RequestInit) => {
      if ((init?.method || 'GET') === 'POST') {
        calls.push({ url, body: init?.body ? JSON.parse(String(init.body)) : null })
        if (after) engine = after
        return Promise.resolve({
          ok: true,
          status: 200,
          text: async () => '{"ok":true,"gate":"tasks"}',
          json: async () => ({ ok: true, gate: 'tasks' }),
        })
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

describe('SpecDetail on the last gate of the plan', () => {
  it('offers the approve control for the gate the engine named', async () => {
    const calls = harness(TASKS_GATE)
    mount()

    const button = await waitFor(() => screen.getByRole('button', { name: /Approve tasks/i }))
    fireEvent.click(button)

    await waitFor(() => expect(calls.some((c) => c.url.includes('/approve'))).toBe(true))
    const approve = calls.find((c) => c.url.includes('/approve'))!
    // The gate is the engine's ``current_gate``. The body names no approver: the
    // authenticated session is the actor, decided on the server.
    expect((approve.body as { gate: string }).gate).toBe('tasks')
    expect(approve.body).not.toHaveProperty('actor')
    expect(approve.body).not.toHaveProperty('user')
    expect(approve.body).not.toHaveProperty('approver')
  })

  it('offers the advance for a gate no label phrase exists for', async () => {
    harness(TASKS_GATE)
    mount()

    // A generic phrase, because this module does not know what follows tasks --
    // only the engine's ``to_phase`` does. The control's EXISTENCE must not
    // depend on having a specific phrase, which is what it used to depend on.
    await waitFor(() => screen.getByRole('button', { name: /Approve → Continue/i }))
  })

  it('makes execution reachable once the approval is recorded', async () => {
    harness(TASKS_GATE, TASKS_APPROVED)
    mount()

    const build = await waitFor(() => screen.getByRole('button', { name: /Start building/i }))
    // The dead end, as it was: the engine refuses the build for a missing
    // approval and the surface has nothing that records one.
    expect(build).toBeDisabled()
    expect(build).toHaveAttribute('title', 'No approval is recorded for tasks.')

    fireEvent.click(screen.getByRole('button', { name: /Approve tasks/i }))

    await waitFor(() =>
      expect(screen.getByRole('button', { name: /Start building/i })).not.toBeDisabled(),
    )
    // And the gate the engine now reports approved is no longer offered for
    // approval -- the control follows the engine's answer, not a local flag.
    expect(screen.queryByRole('button', { name: /Approve tasks/i })).toBeNull()
  })

  it('offers a control for a gate name this surface has never heard of', async () => {
    // A spec type whose plan names a gate no phrase exists for. The engine names
    // it, so the surface offers it; the old render condition offered nothing and
    // the spec was stuck exactly as the feature plan's tasks gate was.
    const calls = harness({
      addressable: true,
      current_gate: 'validation',
      can_execute: false,
      gates: [{ gate: 'validation', present: true, approved: false, stale: false }],
      execution_blocked_by: [
        { code: 'phase.approval-missing', message: 'No approval is recorded for validation.' },
      ],
    })
    mount()

    fireEvent.click(await waitFor(() => screen.getByRole('button', { name: /Approve validation/i })))

    await waitFor(() => expect(calls.some((c) => c.url.includes('/approve'))).toBe(true))
    expect((calls.find((c) => c.url.includes('/approve'))!.body as { gate: string }).gate).toBe(
      'validation',
    )
  })

  it('offers no approval for a gate the engine already approved', async () => {
    harness(TASKS_APPROVED)
    mount()

    await waitFor(() => screen.getByTestId('chat-column'))

    expect(screen.queryByRole('button', { name: /^Approve/i })).toBeNull()
  })
})
