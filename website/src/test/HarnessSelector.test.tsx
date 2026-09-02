/**
 * The new-chat harness picker.
 *
 * Five properties, each with a quiet failure mode the obvious implementation
 * has:
 *
 *  - the DEFAULT row reads as chosen while the session stores no selection, so
 *    the highlighted harness is the one an unselected creation really runs on.
 *  - an unavailable harness is visible, marked, carries its reason, and cannot be
 *    picked. Hiding it makes a missing binary indistinguishable from a harness
 *    this build does not support; offering it invites a creation the gateway
 *    will refuse.
 *  - a harness this BUILD cannot serve is marked the same way. It is available by
 *    every machine-level test — installed, resolvable, listed — so a picker that
 *    reads only `available` offers it, and the refusal then arrives as an error
 *    card after the click with nothing on the row having hinted at it.
 *  - a FAILED listing offers nothing, including a listing that succeeded once and
 *    then failed. Availability is exactly the field that goes stale, so rendering
 *    the previous answer would show a harness as pickable after its binary was
 *    removed.
 *  - an invalid operator descriptor is listed apart and is not an option at all.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, screen, waitFor, fireEvent } from '@testing-library/react'

import { renderWithProviders } from './helpers'
import HarnessSelector from '../components/HarnessSelector'
import {
  harnessRefusal,
  harnessRow,
  harnessSelectable,
  type HarnessListingState,
} from '../hooks/useHarnesses'

const harnesses = vi.fn()

vi.mock('../api/client', () => ({
  api: { harnesses: () => harnesses() },
}))

const KIRO = { id: 'kiro', display_name: 'Kiro CLI', available: true, reason: '' }
const KAS = { id: 'kas', display_name: 'KAS', available: false, reason: 'kas-acp was not found on PATH' }
/** Registered, installed, listed — and this build cannot key a provider on it. */
const CODEX = { id: 'codex', display_name: 'Codex', available: true, reason: '', serviceable: false }

function state(over: Partial<HarnessListingState> = {}): HarnessListingState {
  return {
    harnesses: [KIRO],
    invalid: [],
    defaultId: 'kiro',
    isError: false,
    isLoading: false,
    ...over,
  }
}

describe('HarnessSelector', () => {
  beforeEach(() => {
    harnesses.mockReset()
  })

  it('preselects the configured default while the session stores no choice', async () => {
    harnesses.mockResolvedValue({ harnesses: [KIRO, KAS], default: 'kiro' })
    renderWithProviders(<HarnessSelector value="" onSelect={() => {}} />)
    const trigger = await screen.findByLabelText('Harness: Kiro CLI')
    fireEvent.click(trigger)
    const rows = await screen.findAllByRole('option')
    // The default row, not merely the first row, is the one marked chosen.
    const chosen = rows.filter(r => r.getAttribute('aria-selected') === 'true')
    expect(chosen).toHaveLength(1)
    expect(chosen[0].textContent).toContain('Kiro CLI')
  })

  it('shows an unavailable harness with its reason and refuses to select it', async () => {
    harnesses.mockResolvedValue({ harnesses: [KIRO, KAS], default: 'kiro' })
    const onSelect = vi.fn()
    renderWithProviders(<HarnessSelector value="" onSelect={onSelect} />)
    fireEvent.click(await screen.findByLabelText('Harness: Kiro CLI'))
    const kasRow = (await screen.findAllByRole('option')).find(r => r.textContent?.includes('KAS'))!
    // Visible AND marked: the reason is on the row, not hidden in a tooltip the
    // user has no cue to look for.
    expect(kasRow.textContent).toContain('kas-acp was not found on PATH')
    expect(kasRow.getAttribute('aria-disabled')).toBe('true')
    fireEvent.click(kasRow)
    expect(onSelect).not.toHaveBeenCalled()
  })

  it('selects an available harness by id', async () => {
    harnesses.mockResolvedValue({ harnesses: [KIRO, { ...KAS, available: true, reason: '' }], default: 'kiro' })
    const onSelect = vi.fn()
    renderWithProviders(<HarnessSelector value="" onSelect={onSelect} />)
    fireEvent.click(await screen.findByLabelText('Harness: Kiro CLI'))
    const kasRow = (await screen.findAllByRole('option')).find(r => r.textContent?.includes('KAS'))!
    fireEvent.click(kasRow)
    expect(onSelect).toHaveBeenCalledWith('kas')
  })

  it('offers no harness at all when the listing fetch fails', async () => {
    harnesses.mockRejectedValue(new Error('gateway unavailable'))
    renderWithProviders(<HarnessSelector value="" onSelect={() => {}} />)
    // The trigger falls back to the neutral default label rather than naming a
    // harness it cannot vouch for.
    const trigger = await screen.findByLabelText('Harness: Default harness')
    fireEvent.click(trigger)
    await screen.findByText('Harness list unavailable — retrying')
    // No rows: a retained list is a list whose availability nobody can confirm.
    expect(screen.queryAllByRole('option')).toHaveLength(0)
  })

  it('drops the rows it already had when a refetch fails', async () => {
    // The dangerous shape, and the one the cold-failure test above cannot catch:
    // a listing that ANSWERED once and then did not. React Query retains `data`
    // across a failure, so with only a data-presence check the picker keeps
    // offering rows whose `available` nobody can confirm — a harness whose binary
    // was removed a second ago still reads as pickable.
    harnesses.mockResolvedValueOnce({ harnesses: [KIRO, KAS], default: 'kiro' })
    const { queryClient } = renderWithProviders(<HarnessSelector value="" onSelect={() => {}} />)
    fireEvent.click(await screen.findByLabelText('Harness: Kiro CLI'))
    await waitFor(() => expect(screen.queryAllByRole('option')).toHaveLength(2))
    harnesses.mockRejectedValue(new Error('gateway went away'))
    await act(async () => {
      await queryClient.refetchQueries({ queryKey: ['harnesses'] })
    })
    await screen.findByText('Harness list unavailable — retrying')
    expect(screen.queryAllByRole('option')).toHaveLength(0)
    // And the trigger stops naming the harness it can no longer vouch for.
    expect(screen.queryByLabelText('Harness: Kiro CLI')).toBeNull()
  })

  it('marks a harness this build cannot serve and refuses to select it', async () => {
    harnesses.mockResolvedValue({ harnesses: [KIRO, CODEX], default: 'kiro' })
    const onSelect = vi.fn()
    renderWithProviders(<HarnessSelector value="" onSelect={onSelect} />)
    fireEvent.click(await screen.findByLabelText('Harness: Kiro CLI'))
    const codexRow = (await screen.findAllByRole('option')).find(r => r.textContent?.includes('Codex'))!
    // Marked on the ROW, not only in the error card a click would produce.
    expect(codexRow.textContent).toContain('not supported by this build yet')
    expect(codexRow.getAttribute('aria-disabled')).toBe('true')
    fireEvent.click(codexRow)
    expect(onSelect).not.toHaveBeenCalled()
  })

  it('names a stored selection this build cannot serve', async () => {
    harnesses.mockResolvedValue({ harnesses: [KIRO, CODEX], default: 'kiro' })
    renderWithProviders(<HarnessSelector value="codex" onSelect={() => {}} />)
    // The gateway sends no reason for this one — it is the same verdict for every
    // such row — so the copy has to come from the catalog, not from the wire.
    await screen.findByText('This chat cannot start on Codex: not supported by this build yet')
  })

  it('lists an invalid operator descriptor as unselectable', async () => {
    harnesses.mockResolvedValue({
      harnesses: [KIRO],
      invalid: [{ id: 'agy', display_name: 'agy', available: false, reason: 'unknown placeholder {tool}' }],
      default: 'kiro',
    })
    renderWithProviders(<HarnessSelector value="" onSelect={() => {}} />)
    fireEvent.click(await screen.findByLabelText('Harness: Kiro CLI'))
    await screen.findByText('Invalid descriptor — unknown placeholder {tool}')
    // Never an option — a session can never run it, so it must not sit among
    // the rows that can be chosen.
    await waitFor(() => {
      expect(screen.queryAllByRole('option').map(r => r.textContent).join()).not.toContain('agy')
    })
  })

  it('names the harness and its reason when the stored selection cannot serve', async () => {
    harnesses.mockResolvedValue({ harnesses: [KIRO, KAS], default: 'kiro' })
    renderWithProviders(<HarnessSelector value="kas" onSelect={() => {}} />)
    await screen.findByText('This chat cannot start on KAS: kas-acp was not found on PATH')
  })
})

describe('harnessRow / harnessRefusal', () => {
  it('resolves an empty selection to the default row', () => {
    expect(harnessRow(state(), '')?.id).toBe('kiro')
    expect(harnessRow(state(), 'kiro')?.id).toBe('kiro')
    expect(harnessRow(state(), 'nope')).toBeUndefined()
  })

  it('refuses an unavailable selection with the harness named', () => {
    const r = harnessRefusal(state({ harnesses: [KIRO, KAS] }), 'kas')
    expect(r).toEqual({ name: 'KAS', reason: 'kas-acp was not found on PATH', unserviceable: false })
  })

  it('refuses an available harness this build cannot serve, with no wire reason', () => {
    // Two independent gates. `available` describes the machine and heals when the
    // operator installs the binary; serviceability describes the build. A refusal
    // that read only the first would call this row pickable.
    expect(harnessSelectable(CODEX)).toBe(false)
    expect(harnessSelectable(KIRO)).toBe(true)
    // An older gateway says nothing about the field, and "nothing" must read as
    // serviceable rather than blocking every row it serves.
    expect(harnessSelectable({ ...CODEX, serviceable: undefined })).toBe(true)
    expect(harnessRefusal(state({ harnesses: [KIRO, CODEX] }), 'codex')).toEqual({
      name: 'Codex', reason: '', unserviceable: true,
    })
  })

  it('refuses a selection the listing does not contain, with no invented reason', () => {
    expect(harnessRefusal(state(), 'retired')).toEqual({
      name: 'retired', reason: '', unserviceable: false,
    })
  })

  it('invents no refusal from an answer it does not have', () => {
    // A failed or in-flight listing must not declare a harness unusable: the
    // gateway re-resolves the same selection at creation and refuses there with
    // the harness named, so silence here costs an error card, while a wrong
    // refusal costs the user a harness that works.
    expect(harnessRefusal(state({ isError: true, harnesses: [] }), 'kas')).toBeNull()
    expect(harnessRefusal(state({ isLoading: true, harnesses: [] }), 'kas')).toBeNull()
    // Nor for the pre-harness world: no selection, nothing resolved, no refusal.
    expect(harnessRefusal(state({ harnesses: [], defaultId: '' }), '')).toBeNull()
  })

  it('permits an available selection', () => {
    expect(harnessRefusal(state(), 'kiro')).toBeNull()
    expect(harnessRefusal(state(), '')).toBeNull()
  })
})
