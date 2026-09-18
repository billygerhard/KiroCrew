import { describe, expect, it, vi, afterEach } from 'vitest'
import { render, screen, waitFor, cleanup, within, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

import { api } from '../../api/client'
import { BackendsPanel } from './BackendsPanel'

function mount(node: React.ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  // The panel navigates (cross-link to Developer > Agent Backend), so it needs a router.
  return render(
    <MemoryRouter>
      <QueryClientProvider client={client}>{node}</QueryClientProvider>
    </MemoryRouter>,
  )
}

const PAYLOAD = {
  backends: [
    { id: '', label: 'Kiro CLI', is_global_default: true },
    { id: 'claude', label: 'Claude Code', is_global_default: false },
  ],
  invalid: [{ id: 'bad', label: 'Bad Descriptor', reasons: ['missing executable', 'no argv rule'] }],
  unroutable: [{ id: 'no-route', label: 'No Route', reason: 'no recognized routing declared' }],
}

describe('BackendsPanel', () => {
  afterEach(() => { cleanup(); vi.restoreAllMocks() })

  it('lists selectable, unroutable and invalid rows with reasons and marks the default', async () => {
    vi.spyOn(api, 'backends').mockResolvedValue(PAYLOAD)
    mount(<BackendsPanel />)
    const inv = await screen.findByTestId('backend-inventory')
    // Selectable rows, keyed by id (kiro-cli's '' folds to the 'kiro' testid).
    expect(within(inv).getByTestId('backend-row-kiro')).toBeTruthy()
    expect(within(inv).getByTestId('backend-row-claude')).toBeTruthy()
    // The default is marked, and only on the default row.
    expect(within(inv).getByTestId('backend-default-kiro')).toBeTruthy()
    expect(within(inv).queryByTestId('backend-default-claude')).toBeNull()
    // Diagnostic rows carry their reasons.
    const unroutable = within(inv).getByTestId('backend-unroutable-no-route')
    expect(within(unroutable).getByText(/no recognized routing declared/)).toBeTruthy()
    const invalid = within(inv).getByTestId('backend-invalid-bad')
    expect(within(invalid).getByText(/missing executable/)).toBeTruthy()
    expect(within(invalid).getByText(/no argv rule/)).toBeTruthy()
    // Each invalid reason is the standard error surface, not a hand-written
    // line: an alert with the agent hand-off attached (read-only page, so the
    // hand-off has nothing to lose).
    const first = within(invalid).getByTestId('backend-invalid-bad-reason-0')
    expect(first.getAttribute('role')).toBe('alert')
    expect(within(invalid).getAllByRole('alert')).toHaveLength(2)
    expect(within(first).getByRole('button', { name: /ask the agent/i })).toBeTruthy()
  })

  it('offers Verify only on a row whose missing piece is the routing attestation, and reports the verdict', async () => {
    const unverified = {
      backends: [{ id: '', label: 'Kiro CLI', is_global_default: true }],
      invalid: [],
      unroutable: [
        { id: 'acme', label: 'Acme', reason: 'routing not yet verified end to end', verifiable: true },
        { id: 'no-route', label: 'No Route', reason: 'no recognized routing declared', verifiable: false },
      ],
    }
    const afterVerify = {
      backends: [
        { id: '', label: 'Kiro CLI', is_global_default: true },
        { id: 'acme', label: 'Acme', is_global_default: false },
      ],
      invalid: [],
      unroutable: [unverified.unroutable[1]],
    }
    const listing = vi.spyOn(api, 'backends').mockResolvedValueOnce(unverified).mockResolvedValue(afterVerify)
    const verify = vi.spyOn(api, 'verifyBackend').mockResolvedValue({
      verdict: 'verified', verified: true, reason: 'asked once', permission_requests: 1,
      probe_file_written: false, elapsed_secs: 12, selectable: true,
    })
    mount(<BackendsPanel />)
    const inv = await screen.findByTestId('backend-inventory')
    // The unroutable row with nothing to verify gets no action; the unverified one does.
    expect(within(inv).queryByTestId('backend-verify-no-route')).toBeNull()
    const btn = within(inv).getByTestId('backend-verify-acme')
    expect(within(within(inv).getByTestId('backend-unroutable-acme')).getByText('Routing not verified')).toBeTruthy()
    // One list, ordered independently of status: Kiro first, then by label.
    const orderBefore = Array.from(inv.querySelectorAll('li')).map(li => li.getAttribute('data-testid'))
    expect(orderBefore).toEqual(['backend-row-kiro', 'backend-unroutable-acme', 'backend-unroutable-no-route'])
    fireEvent.click(btn)
    await waitFor(() => expect(verify).toHaveBeenCalledWith('acme'))
    // A verified verdict refetches the listing: the row is now selectable.
    await waitFor(() => expect(within(inv).queryByTestId('backend-row-acme')).toBeTruthy())
    expect(listing).toHaveBeenCalledTimes(2)
    // ...and it has NOT moved: same index, badge gone, success line under it.
    const orderAfter = Array.from(inv.querySelectorAll('li')).map(li => li.getAttribute('data-testid'))
    expect(orderAfter).toEqual(['backend-row-kiro', 'backend-row-acme', 'backend-unroutable-no-route'])
    const promoted = within(inv).getByTestId('backend-row-acme')
    expect(within(promoted).queryByText('Routing not verified')).toBeNull()
    expect(within(promoted).getByTestId('backend-verify-outcome-acme').getAttribute('role')).toBe('status')
    expect(within(promoted).getByText(/^Verified:/)).toBeTruthy()
  })

  it('reports a verified-but-policy-denied outcome on the row, which stays unselectable', async () => {
    const before = {
      backends: [{ id: '', label: 'Kiro CLI', is_global_default: true }],
      invalid: [],
      unroutable: [{ id: 'acme', label: 'Acme', reason: 'routing not yet verified end to end', verifiable: true }],
    }
    const after = {
      ...before,
      unroutable: [{ id: 'acme', label: 'Acme', reason: 'routing verified, but this deployment\'s agent_backend policy does not permit the backend, so it is not selectable', verifiable: false }],
    }
    vi.spyOn(api, 'backends').mockResolvedValueOnce(before).mockResolvedValue(after)
    vi.spyOn(api, 'verifyBackend').mockResolvedValue({
      verdict: 'verified', verified: true, reason: 'asked once; routing verified, but this deployment\'s agent_backend policy does not permit the backend, so it is not selectable',
      permission_requests: 1, probe_file_written: false, elapsed_secs: 3, selectable: false, policy_denied: true,
    })
    mount(<BackendsPanel />)
    const inv = await screen.findByTestId('backend-inventory')
    fireEvent.click(within(inv).getByTestId('backend-verify-acme'))
    // The row stays unroutable (no Verify offered any more: verification is not
    // what it lacks) and the outcome names the policy through ErrorNotice.
    await waitFor(() => expect(within(inv).queryByTestId('backend-verify-acme')).toBeNull())
    const row = within(inv).getByTestId('backend-unroutable-acme')
    const outcome = within(row).getByTestId('backend-verify-outcome-acme')
    expect(outcome.getAttribute('role')).toBe('alert')
    expect(within(outcome).getByText(/agent_backend policy/)).toBeTruthy()
    expect(within(inv).queryByTestId('backend-row-acme')).toBeNull()
  })

  it('renders a failed or inconclusive verification through ErrorNotice with the reason', async () => {
    vi.spyOn(api, 'backends').mockResolvedValue({
      backends: [{ id: '', label: 'Kiro CLI', is_global_default: true }],
      invalid: [],
      unroutable: [{ id: 'acme', label: 'Acme', reason: 'routing not yet verified end to end', verifiable: true }],
    })
    vi.spyOn(api, 'verifyBackend').mockResolvedValue({
      verdict: 'violation', verified: false, reason: 'the probe file was written despite denial',
      permission_requests: 0, probe_file_written: true, elapsed_secs: 9, selectable: false,
    })
    mount(<BackendsPanel />)
    const inv = await screen.findByTestId('backend-inventory')
    fireEvent.click(within(inv).getByTestId('backend-verify-acme'))
    const outcome = await within(inv).findByTestId('backend-verify-outcome-acme')
    expect(outcome.getAttribute('role')).toBe('alert')
    expect(within(outcome).getByText(/written despite denial/)).toBeTruthy()
    expect(within(outcome).getByText('Verification failed')).toBeTruthy()
    // Still not selectable: the row stays where it was.
    expect(within(inv).queryByTestId('backend-row-acme')).toBeNull()
  })

  it('renders nothing but the unavailable notice on a failed fetch', async () => {
    vi.spyOn(api, 'backends').mockRejectedValue(new Error('offline'))
    mount(<BackendsPanel />)
    await waitFor(() => expect(screen.queryByTestId('backend-inventory')).toBeNull())
    // No stale rows are shown.
    expect(screen.queryByTestId('backend-row-kiro')).toBeNull()
  })
})
