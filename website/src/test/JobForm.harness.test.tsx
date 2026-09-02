/**
 * The cron editor's per-job ACP harness override.
 *
 * Two properties carry the feature and both have a silent failure mode:
 *
 *  - a stored override ROUND-TRIPS. The picker seeds from the job and the edit
 *    body always carries the field, so saving an unrelated change cannot quietly
 *    reset a job to the default harness — and an empty value stays expressible,
 *    because clearing the override back to "inherit" is a real edit.
 *  - the option list never renders from data that may be stale. Availability is
 *    the whole point of the listing and it is exactly the part that goes out of
 *    date, so a failed fetch collapses to "inherit" plus whatever the job holds
 *    rather than showing a harness as pickable on last week's answer.
 */
import { describe, it, expect, vi } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import JobForm, { parseJobDefaults, buildBody, harnessSelectRows } from '../components/JobForm'
import type { CronJob } from '../types'

const harnesses = vi.fn()

vi.mock('../api/client', () => ({
  api: {
    updateCron: vi.fn(),
    createCron: vi.fn(),
    models: vi.fn(async () => []),
    harnesses: () => harnesses(),
  },
}))

function makeJob(overrides: Partial<CronJob> = {}): CronJob {
  return {
    id: 'h1', name: 'nightly', message: 'go', schedule: '', enabled: true,
    every_secs: 3600, ...overrides,
  } as CronJob
}

describe('JobForm harness override', () => {
  it('parseJobDefaults reads the stored harness, and absent means inherit', () => {
    expect(parseJobDefaults(makeJob({ harness: 'kas' })).harness).toBe('kas')
    expect(parseJobDefaults(makeJob()).harness).toBe('')
    // null is what the list endpoint sends for "no override"; it must not reach
    // the select as the string "null".
    expect(parseJobDefaults(makeJob({ harness: null })).harness).toBe('')
    expect(parseJobDefaults(undefined).harness).toBe('')
  })

  it('buildBody omits an empty harness on create but always sends it on edit', () => {
    const create = buildBody(parseJobDefaults(makeJob()), 'UTC', () => {})
    expect(create!.harness).toBeUndefined()
    // Edit mode sends "" so clearing an override persists — the update handler
    // assigns on presence, so an omitted field would leave the old value.
    const cleared = buildBody(parseJobDefaults(makeJob()), 'UTC', () => {}, true)
    expect(cleared!.harness).toBe('')
    const kept = buildBody(parseJobDefaults(makeJob({ harness: 'kas' })), 'UTC', () => {}, true)
    expect(kept!.harness).toBe('kas')
  })

  it('labels an unavailable harness with its reason and keeps it selectable', () => {
    const rows = harnessSelectRows(
      [
        { id: 'kiro', display_name: 'Kiro CLI', available: true, reason: '' },
        { id: 'kas', display_name: 'KAS', available: false, reason: 'kas was not found' },
      ],
      false,
      '',
    )
    expect(rows.values).toEqual(['kiro', 'kas'])
    expect(rows.labels[0]).toBe('Kiro CLI')
    // Reason in the label, not a removed row: the write is legitimate and the
    // operator needs to know why it cannot run yet.
    expect(rows.labels[1]).toContain('KAS')
    expect(rows.labels[1]).toContain('kas was not found')
  })

  it('marks an unserviceable harness, explains it, and refuses to offer it', () => {
    // The composer's treatment, mirrored here. "Not installed yet" is a state
    // that changes, so an unavailable row stays selectable; "this build cannot
    // serve it" does not, so a job stored against one could never fire.
    const rows = harnessSelectRows(
      [
        { id: 'kiro', display_name: 'Kiro CLI', available: true, reason: '', serviceable: true },
        { id: 'codex', display_name: 'Codex CLI', available: true, reason: '', serviceable: false },
      ],
      false,
      '',
    )
    // Rendered, not dropped: a missing row cannot be told from a harness that
    // does not exist, and leaves the operator nothing to act on.
    expect(rows.values).toEqual(['kiro', 'codex'])
    expect(rows.labels[1]).toContain('Codex CLI')
    expect(rows.labels[1]).not.toBe('Codex CLI')
    // ...and not selectable.
    expect(rows.disabled).toEqual(['codex'])
  })

  it('leaves a serviceable-by-omission harness selectable', () => {
    // A gateway predating the field sends no `serviceable`, which must read as
    // serviceable — the fail direction that keeps an older gateway's harnesses
    // pickable instead of disabling all of them.
    const rows = harnessSelectRows(
      [{ id: 'kiro', display_name: 'Kiro CLI', available: true, reason: '' }],
      false,
      '',
    )
    expect(rows.disabled).toEqual([])
    expect(rows.labels).toEqual(['Kiro CLI'])
  })

  it('offers nothing from a failed listing but keeps the stored override', () => {
    const listing = [{ id: 'kiro', display_name: 'Kiro CLI', available: true, reason: '' }]
    expect(harnessSelectRows(listing, true, '').values).toEqual([])
    // The job's own value survives — the form has to be able to save it back —
    // while no OTHER harness is offered off an answer that never arrived.
    expect(harnessSelectRows(listing, true, 'kas').values).toEqual(['kas'])
    expect(harnessSelectRows(listing, true, 'kas').labels).toEqual(['kas'])
  })

  it('prepends a stored override the listing no longer contains', () => {
    const rows = harnessSelectRows(
      [{ id: 'kiro', display_name: 'Kiro CLI', available: true, reason: '' }],
      false,
      'retired-harness',
    )
    expect(rows.values).toEqual(['retired-harness', 'kiro'])
  })

  it('renders the harness picker on the message-job editor', async () => {
    harnesses.mockResolvedValue({
      harnesses: [{ id: 'kiro', display_name: 'Kiro CLI', available: true, reason: '' }],
    })
    renderWithProviders(
      <JobForm job={makeJob({ harness: 'kas' })} agents={[]} defaultAgent="" onSaved={() => {}} layout="vertical" />,
    )
    const select = await screen.findByLabelText('Harness')
    // The trigger shows the job's stored override rather than "inherit".
    await waitFor(() => expect(select.textContent).toContain('kas'))
  })

  it('hides the harness picker for an LLM-less job', async () => {
    harnesses.mockResolvedValue({ harnesses: [] })
    renderWithProviders(
      <JobForm job={makeJob({ script: '~/.kiro/crew/crons/f.py:run', message: '' })} agents={[]} defaultAgent="" onSaved={() => {}} layout="vertical" />,
    )
    // A script cron runs no LLM, so it has no harness to pick — same rule the
    // agent and model rows already follow.
    await waitFor(() => expect(screen.queryByLabelText('Harness')).toBeNull())
  })
})
