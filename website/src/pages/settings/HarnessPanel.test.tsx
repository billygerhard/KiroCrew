/**
 * The Settings AI Backend panel.
 *
 * Six properties carry the surface, and each has a failure mode a reader of the
 * markup would not notice:
 *
 *  - the inventory is BOTH registry listings. Valid rows come from one call and
 *    rejected operator descriptors from another, so a panel reading only the first
 *    hides the broken entry whose reason the registry recorded for exactly this
 *    surface — and a panel reading both blindly draws two rows for one id when an
 *    operator entry collides with a bundled harness.
 *  - the alias input is seeded from the STORED spelling, and its vocabulary is the
 *    one the gateway served. The config GET reports `agent.acp_backend` clamped, so
 *    a value outside the selectable set reads back there as kiro-cli; seeding from
 *    it would render a harness the operator never named and then write that back on
 *    the next change. Restating the writable set in TypeScript would put one
 *    vocabulary in two languages with no gate between them.
 *  - availability is never rendered from data that did not arrive — including in the
 *    resolved-default hint, which lives in a `title` attribute where no visible-text
 *    assertion can see it go stale.
 *  - a harness this BUILD cannot serve is marked as such, not as an install problem.
 *  - a query that FAILED is said out loud rather than rendered as an answer, and a
 *    query still in flight leaves no control that could write from it.
 *  - a refused write is shown behind a translated prefix: the reason names the
 *    harness and lists the registered ids, which is worth showing, but it is
 *    English prose in a dashboard translated into twelve languages.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor, act } from '@testing-library/react'
import { renderWithProviders } from '../../test/helpers'

const harnesses = vi.fn()
const kirocrewConfig = vi.fn()
const patchConfig = vi.fn()
const acpBackends = vi.fn()

vi.mock('../../api/client', () => ({
  api: {
    harnesses: () => harnesses(),
    kirocrewConfig: () => kirocrewConfig(),
    patchConfig: (path: string, value: unknown) => patchConfig(path, value),
    acpBackends: () => acpBackends(),
  },
}))

import { HarnessPanel, mergeInventory, defaultHarnessRows, legacyBackendRows, rowProbe, installStateLine } from './HarnessPanel'

const KIRO = { id: 'kiro', display_name: 'Kiro CLI', available: true, reason: '', bundled: true }
const CODEX = {
  id: 'codex',
  display_name: 'Codex',
  available: false,
  reason: 'codex was not found on PATH',
  bundled: true,
}
/** Registered, installed, and unusable for a different reason: this build cannot
 *  key a provider on it. Available by every machine-level test. */
const UNSERVICEABLE = {
  id: 'codex',
  display_name: 'Codex',
  available: true,
  reason: '',
  bundled: true,
  serviceable: false,
}

function payload(over: Record<string, unknown> = {}) {
  return {
    harnesses: [KIRO],
    invalid: [],
    default: 'kiro',
    legacy_backend: '',
    legacy_backends: ['', 'kas'],
    ...over,
  }
}

/** One `GET /api/acp-backends` probe row, defaulted to the uninteresting answer
 *  (installed, selectable, nothing owed) so a test overrides only what it exercises. */
function probeRow(over: Record<string, unknown> = {}) {
  return {
    id: 'kiro',
    policy_id: 'kiro',
    selectable: true,
    installed: 'installed',
    missing_components: [],
    install_command: '',
    restart_required: false,
    ...over,
  }
}

async function renderPanel(over: Record<string, unknown> = {}, defaultHarness = '', backends: unknown[] = []) {
  harnesses.mockResolvedValue(payload(over))
  kirocrewConfig.mockResolvedValue({ agent: { default_harness: defaultHarness } })
  patchConfig.mockResolvedValue({ ok: true })
  acpBackends.mockResolvedValue({ backends })
  const utils = renderWithProviders(<HarnessPanel />)
  await screen.findByTestId('harness-inventory')
  return utils
}

describe('mergeInventory', () => {
  it('renders one row per id, folding a colliding operator entry onto the bundled row', () => {
    // The registry rejects an operator entry that reuses a bundled id and keeps
    // serving the bundled harness. Two rows with one id would put a harness
    // nobody can select beside the one that works.
    const rows = mergeInventory(
      [KIRO],
      [{ id: 'kiro', display_name: 'kiro', available: false, reason: 'identifier is already registered', bundled: false }],
    )
    expect(rows).toHaveLength(1)
    expect(rows[0].available).toBe(true)
    expect(rows[0].invalid).toBe(false)
    expect(rows[0].conflict).toBe('identifier is already registered')
  })

  it('keeps a rejected operator descriptor as its own row with its reason', () => {
    const rows = mergeInventory(
      [KIRO],
      [{ id: 'mine', display_name: 'mine', available: false, reason: 'executable is empty', bundled: false }],
    )
    expect(rows.map(r => r.id)).toEqual(['kiro', 'mine'])
    expect(rows[1]).toMatchObject({ invalid: true, available: false, reason: 'executable is empty', bundled: false })
  })
})

describe('defaultHarnessRows', () => {
  it('offers every registered harness plus the unset option, reason in the label', () => {
    const rows = defaultHarnessRows([KIRO, CODEX], false, '')
    expect(rows.values).toEqual(['', 'kiro', 'codex'])
    expect(rows.labels[1]).toBe('Kiro CLI')
    // An unavailable harness stays selectable: the write gate accepts any
    // registered id because the default may be set before the tool is installed.
    expect(rows.labels[2]).toContain('Codex')
    expect(rows.labels[2]).toContain('codex was not found on PATH')
  })

  it('offers nothing but the stored value from a listing that did not answer', () => {
    expect(defaultHarnessRows([KIRO, CODEX], true, '').values).toEqual([''])
    expect(defaultHarnessRows([KIRO, CODEX], true, 'kas').values).toEqual(['', 'kas'])
  })

  it('marks a harness this build cannot serve in its label', () => {
    // Distinct copy from the unavailable case, and not the registry's reason: an
    // install-it instruction would be wrong advice here — the harness is already
    // installed and the build is what cannot use it.
    const rows = defaultHarnessRows([KIRO, UNSERVICEABLE], false, '')
    expect(rows.labels[2]).toBe('Codex — not supported by this build')
  })

  it('unions in a configured harness the listing no longer contains', () => {
    const rows = defaultHarnessRows([KIRO], false, 'retired')
    expect(rows.values).toEqual(['', 'retired', 'kiro'])
    expect(rows.labels[1]).toBe('retired')
  })
})

describe('legacyBackendRows', () => {
  it('offers the vocabulary the gateway served', () => {
    // The set lives in one language on the gateway (it is also the PATCH
    // allowlist's enum). Restating it here would put the two an edit apart with
    // nothing failing in between: a spelling retired there would keep being
    // offered, and Settings would write a value session creation refuses.
    expect(legacyBackendRows('', ['', 'kas']).values).toEqual(['', 'kas'])
    expect(legacyBackendRows('kas', ['', 'kas']).values).toEqual(['', 'kas'])
    // A spelling this dashboard has no label for still appears, named by itself:
    // the vocabulary is the gateway's, so an unlabelled entry is a missing
    // translation, not a value nobody may pick.
    const grown = legacyBackendRows('', ['', 'kas', 'newthing'])
    expect(grown.values).toEqual(['', 'kas', 'newthing'])
    expect(grown.labels[2]).toBe('newthing')
  })

  it('offers only the default spelling when no vocabulary arrived', () => {
    // A failed fetch, or a gateway predating the field. The input is disabled in
    // that state anyway; offering a guessed set would be a second source of truth
    // that outlives the disable.
    expect(legacyBackendRows('').values).toEqual([''])
    expect(legacyBackendRows('', []).values).toEqual([''])
  })

  it('unions in a stored spelling outside the selectable set', () => {
    // A hand-edited `codex` is what the operator actually wrote. Rendering the
    // clamped field instead would show Kiro CLI as their choice.
    const rows = legacyBackendRows('codex', ['', 'kas'])
    expect(rows.values).toEqual(['', 'kas', 'codex'])
    expect(rows.labels[2]).toContain('codex')
  })
})

describe('HarnessPanel', () => {
  beforeEach(() => { vi.clearAllMocks(); acpBackends.mockResolvedValue({ backends: [] }) })

  it('lists bundled and operator harnesses with their availability and reason', async () => {
    await renderPanel({
      harnesses: [
        KIRO,
        CODEX,
        { id: 'agy', display_name: 'AGY', available: true, reason: '', bundled: false },
      ],
    })
    expect(screen.getByTestId('harness-row-kiro').textContent).toContain('Available')
    const codexRow = screen.getByTestId('harness-row-codex')
    expect(codexRow.textContent).toContain('Unavailable')
    expect(codexRow.textContent).toContain('codex was not found on PATH')
    // Bundled vs operator is visible: an operator's own entry is the one they can
    // fix in config.json.
    expect(screen.getByTestId('harness-row-kiro').textContent).toContain('Bundled')
    expect(screen.getByTestId('harness-row-agy').textContent).toContain('Operator')
  })

  it('shows a rejected operator descriptor with its reason', async () => {
    await renderPanel({
      invalid: [{ id: 'broken', display_name: 'broken', available: false, reason: 'argv is empty', bundled: false }],
    })
    const row = screen.getByTestId('harness-row-broken')
    expect(row.textContent).toContain('Invalid')
    expect(row.textContent).toContain('argv is empty')
  })

  it('names the resolved default and says when it cannot run', async () => {
    await renderPanel({ harnesses: [KIRO, CODEX], default: 'codex', legacy_backend: 'codex' })
    // The resolution is the server's, composed from both keys off the RAW stored
    // spelling — the panel reports it rather than recomputing the precedence.
    await waitFor(() => expect(screen.getByText(/Codex/)).toBeTruthy())
    expect(document.body.textContent).toContain('codex was not found on PATH')
  })

  it('writes the picked default harness', async () => {
    await renderPanel({ harnesses: [KIRO, CODEX] })
    fireEvent.click(screen.getByLabelText('Default harness'))
    fireEvent.click(await screen.findByRole('option', { name: /Kiro CLI/ }))
    await waitFor(() => expect(patchConfig).toHaveBeenCalledWith('agent.default_harness', 'kiro'))
  })

  it("refetches the composer's own listing after a successful write", async () => {
    // The welcome-screen picker caches the same listing under ['harnesses'] with
    // staleTime Infinity and nothing else refetches it, so without this
    // invalidation the composer keeps preselecting the PREVIOUS default until a
    // page reload — and the harness-change model-drop comparison reads the stale
    // default too.
    const { queryClient } = await renderPanel({ harnesses: [KIRO, CODEX] })
    const invalidate = vi.spyOn(queryClient, 'invalidateQueries')
    fireEvent.click(screen.getByLabelText('Default harness'))
    fireEvent.click(await screen.findByRole('option', { name: /Kiro CLI/ }))
    await waitFor(() =>
      expect(invalidate.mock.calls.some(
        ([arg]) => JSON.stringify((arg as { queryKey?: unknown }).queryKey) === JSON.stringify(['harnesses']),
      )).toBe(true),
    )
  })

  it('does not write the legacy key when the unchanged stored spelling is re-picked', async () => {
    await renderPanel({ legacy_backend: 'codex' })
    fireEvent.click(screen.getByLabelText('Legacy ACP backend'))
    fireEvent.click(await screen.findByRole('option', { name: /codex/ }))
    // Outside the field's enum, so a PATCH would be refused — for an action that
    // changed nothing.
    await waitFor(() => expect(patchConfig).not.toHaveBeenCalled())
  })

  it('writes a changed legacy backend', async () => {
    await renderPanel({ legacy_backend: '' })
    fireEvent.click(screen.getByLabelText('Legacy ACP backend'))
    fireEvent.click(await screen.findByRole('option', { name: /KAS/ }))
    await waitFor(() => expect(patchConfig).toHaveBeenCalledWith('agent.acp_backend', 'kas'))
  })

  it('renders no availability at all when the listing fails', async () => {
    harnesses.mockRejectedValue(new Error('nope'))
    kirocrewConfig.mockResolvedValue({ agent: { default_harness: '' } })
    renderWithProviders(<HarnessPanel />)
    await screen.findByText(/Failed to load the harness list/)
    // Not "everything is fine": no row claims availability off an answer that
    // never arrived.
    expect(screen.queryByTestId('harness-inventory')).toBeNull()
    expect(document.body.textContent).not.toContain('Available')
  })

  it('drops the rows it already had when a refetch fails', async () => {
    // The dangerous case is not the cold failure — it is the one where a listing
    // succeeded once and the refetch did not. React Query RETAINS the old data
    // then, so a panel keyed on data presence would keep drawing "Available" off
    // a stat that no longer holds. Availability is gated on isError instead.
    const { queryClient } = await renderPanel({ harnesses: [KIRO, CODEX], default: 'codex' })
    expect(screen.getByTestId('harness-row-kiro').textContent).toContain('Available')
    // The resolved-default hint states availability too, and it lives in a `title`
    // attribute plus a click-opened popup — neither of which textContent reaches,
    // so a check on the visible text alone would pass with the hint still quoting
    // a stale reason no notice on the panel contradicts.
    expect(document.querySelector('[title*="codex was not found on PATH"]')).toBeTruthy()
    harnesses.mockRejectedValue(new Error('gateway went away'))
    await queryClient.refetchQueries({ queryKey: ['harnessRegistry'] })
    await waitFor(() => expect(screen.queryByTestId('harness-inventory')).toBeNull())
    expect(document.body.textContent).toContain('availability')
    expect(document.body.textContent).not.toContain('codex was not found on PATH')
    expect(document.querySelector('[title*="codex was not found on PATH"]')).toBeNull()
    // No hint at all: the resolution it would describe is composed from rows that
    // did not arrive.
    expect(document.querySelector('[title*="New sessions"]')).toBeNull()
    // The picker offers nothing either, for the same reason.
    expect(screen.queryByRole('option')).toBeNull()
  })

  it('marks a harness this build cannot serve, without calling the install broken', async () => {
    await renderPanel({ harnesses: [KIRO, UNSERVICEABLE], default: 'codex' })
    const row = screen.getByTestId('harness-row-codex')
    // Both badges: the machine is fine and the build is not, and collapsing them
    // would send the operator to fix an install that already works.
    expect(row.textContent).toContain('Available')
    expect(row.textContent).toContain('Unsupported')
    expect(row.textContent).toContain('This build cannot start a session on this harness.')
    // And the resolved-default hint says so rather than the flat "New sessions
    // start on Codex." — which is a positive claim about sessions that all refuse.
    expect(document.querySelector('[title*="cannot start a session on"]')).toBeTruthy()
  })

  it('says the settings did not load instead of reporting the default as unset', async () => {
    // A failed config GET yields exactly the empty value an unset key does, and
    // "Not set" is a claim about the operator's configuration. The difference
    // between "you have not set this" and "we could not read it" is the difference
    // between a shrug and a retry.
    harnesses.mockResolvedValue(payload())
    kirocrewConfig.mockRejectedValue(new Error('config unreadable'))
    patchConfig.mockResolvedValue({ ok: true })
    renderWithProviders(<HarnessPanel />)
    await screen.findByText(/Failed to load the current settings/)
    // And the picker cannot be written from a value nobody read.
    expect(screen.getByLabelText('Default harness')).toBeDisabled()
  })

  it('does not offer a write from a listing that has not arrived yet', async () => {
    // PENDING, not failed: the options are the stored value alone until the rows
    // land, so an enabled control would let a default be written from a vocabulary
    // that does not exist yet.
    let release: (v: unknown) => void = () => {}
    harnesses.mockReturnValue(new Promise(res => { release = res }))
    kirocrewConfig.mockResolvedValue({ agent: { default_harness: 'kas' } })
    renderWithProviders(<HarnessPanel />)
    // Wait for the CONFIG query specifically — the stored value renders only once
    // it has answered. Asserting before that would pass on the config query still
    // being in flight, which is a different disable and a race either way.
    await waitFor(() => expect(screen.getByLabelText('Default harness').textContent).toContain('kas'))
    expect(screen.getByLabelText('Default harness')).toBeDisabled()
    await act(async () => { release(payload()) })
    await waitFor(() => expect(screen.getByLabelText('Default harness')).not.toBeDisabled())
  })

  it('shows the write refusal behind a translated prefix', async () => {
    await renderPanel({ harnesses: [KIRO, CODEX] })
    patchConfig.mockRejectedValue(new Error("unknown harness 'typo'; registered harnesses are: kiro, codex"))
    fireEvent.click(screen.getByLabelText('Default harness'))
    fireEvent.click(await screen.findByRole('option', { name: /Codex/ }))
    // The reason is the diagnostic (it lists the registered ids) so it is shown —
    // but never as the WHOLE message, or a 12-language dashboard explains a
    // failure in English only.
    await screen.findByText(/registered harnesses are: kiro, codex/)
    expect(document.body.textContent).toContain('Failed to save')
  })
})

describe('rowProbe', () => {
  it('joins a probe onto a row by policy_id === id', () => {
    const probes = [
      probeRow({ id: 'kas', policy_id: 'kas', installed: 'missing', missing_components: ['kiro-agent'], install_command: 'npm i -g kiro-agent' }),
      probeRow(),
    ]
    const p = rowProbe(probes, 'kas')
    expect(p?.installed).toBe('missing')
    expect(p?.missingComponents).toEqual(['kiro-agent'])
    expect(p?.installCommand).toBe('npm i -g kiro-agent')
  })

  it('returns undefined when the payload is absent or has no matching row', () => {
    // Absent payload (403/404/in flight) and an operator id with no backend row
    // both mean "say nothing, gate nothing".
    expect(rowProbe(undefined, 'kiro')).toBeUndefined()
    expect(rowProbe([probeRow()], 'agy')).toBeUndefined()
  })

  it('guards a gateway that omitted missing_components', () => {
    const p = rowProbe([probeRow({ missing_components: undefined as unknown as string[] })], 'kiro')
    expect(p?.missingComponents).toEqual([])
  })
})

describe('installStateLine', () => {
  it('names the missing components AND the install command when the server gave one', () => {
    const line = installStateLine({ installed: 'missing', missingComponents: ['codex'], installCommand: 'brew install codex', restartRequired: false, selectable: true })
    expect(line).toContain('codex')
    expect(line).toContain('brew install codex')
  })

  it('names only the components when there is no command', () => {
    const line = installStateLine({ installed: 'missing', missingComponents: ['codex'], installCommand: '', restartRequired: false, selectable: true })
    expect(line).toContain('codex')
    expect(line).not.toContain('Install with')
  })

  it('reports a failed check as its own line, never as missing', () => {
    const line = installStateLine({ installed: 'unknown', missingComponents: [], installCommand: '', restartRequired: false, selectable: true })
    expect(line).toContain('Could not check')
  })

  it('says a restart is owed for an installed-but-cached-absent backend', () => {
    const line = installStateLine({ installed: 'installed', missingComponents: [], installCommand: '', restartRequired: true, selectable: true })
    expect(line).toContain('restart')
  })

  it('says nothing for a clean install verdict, and nothing when there is no probe', () => {
    expect(installStateLine({ installed: 'installed', missingComponents: [], installCommand: '', restartRequired: false, selectable: true })).toBe('')
    expect(installStateLine(undefined)).toBe('')
  })
})

describe('HarnessPanel — ported install-state display', () => {
  beforeEach(() => { vi.clearAllMocks(); acpBackends.mockResolvedValue({ backends: [] }) })

  it('shows the remedy command on a MISSING bundled row', async () => {
    // kas is registered+available per the registry, but the machine probe says
    // its components are missing — the panel must say WHAT to install, ported
    // from the developer tab.
    await renderPanel(
      { harnesses: [KIRO, { id: 'kas', display_name: 'KAS', available: true, reason: '', bundled: true }] },
      '',
      [probeRow({ id: 'kas', policy_id: 'kas', installed: 'missing', missing_components: ['kiro-agent'], install_command: 'npm i -g @amzn/kiro-agent' })],
    )
    await waitFor(() => {
      const line = screen.getByTestId('harness-install-kas')
      expect(line.textContent).toContain('kiro-agent')
      expect(line.textContent).toContain('npm i -g @amzn/kiro-agent')
    })
    // And a Missing badge, not merely the registry's Available.
    expect(screen.getByTestId('harness-missing-kas')).toBeTruthy()
  })

  it('shows a restart-required badge and line when the binary is present but the gateway cached its absence', async () => {
    await renderPanel(
      { harnesses: [KIRO, { id: 'kas', display_name: 'KAS', available: true, reason: '', bundled: true }] },
      '',
      [probeRow({ id: 'kas', policy_id: 'kas', installed: 'installed', restart_required: true })],
    )
    await waitFor(() => expect(screen.getByTestId('harness-restart-kas')).toBeTruthy())
    expect(screen.getByTestId('harness-install-kas').textContent).toContain('restart')
  })

  it('hides a policy-denied row (selectable=false), matching the developer tab', async () => {
    // The probe says this build/policy will not serve `kas` at all — HIDDEN, not
    // shown marked, exactly as AgentBackendTab did. kiro (the resolved default)
    // stays.
    await renderPanel(
      { harnesses: [KIRO, { id: 'kas', display_name: 'KAS', available: true, reason: '', bundled: true }] },
      '',
      [probeRow({ id: 'kas', policy_id: 'kas', selectable: false })],
    )
    await waitFor(() => expect(screen.getByTestId('harness-row-kiro')).toBeTruthy())
    expect(screen.queryByTestId('harness-row-kas')).toBeNull()
  })

  it('KEEPS a policy-denied row when it is the resolved default, never rendering the running default missing', async () => {
    await renderPanel(
      { harnesses: [KIRO, { id: 'kas', display_name: 'KAS', available: true, reason: '', bundled: true }], default: 'kas' },
      '',
      [probeRow({ id: 'kas', policy_id: 'kas', selectable: false })],
    )
    await waitFor(() => expect(screen.getByTestId('harness-row-kas')).toBeTruthy())
  })

  it('annotates nothing when the probe endpoint answered 403/404 (empty), falling back to registry availability alone', async () => {
    await renderPanel(
      { harnesses: [KIRO, { id: 'kas', display_name: 'KAS', available: true, reason: '', bundled: true }] },
      '',
      [], // probe carried no rows — absent information, not a verdict
    )
    await waitFor(() => expect(screen.getByTestId('harness-row-kas')).toBeTruthy())
    expect(screen.queryByTestId('harness-install-kas')).toBeNull()
    expect(screen.queryByTestId('harness-missing-kas')).toBeNull()
  })
})
