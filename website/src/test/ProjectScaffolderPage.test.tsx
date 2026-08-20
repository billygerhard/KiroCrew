/**
 * Integration test for src/apps/project-scaffolder/ProjectScaffolderPage.tsx.
 *
 * `fetch` is mocked rather than the app's `api.ts`, so the error-shape parsing
 * (`code`, `unknown`, verbatim `error` prose) is exercised as the real thing:
 * the stale-selection branch is chosen by a `code` this test only ever supplies
 * inside a 400 body, which is where the server puts it.
 *
 * The shared `ProjectPicker` reaches the network through `api/client` rather than
 * a bare `fetch`, so its two GETs are spied there instead. That keeps the queue
 * below strictly the scan/scaffold POSTs, which is what lets a test assert on
 * `calls[n]` by index.
 *
 * Covers: the picker-driven root, preview rendering with mixed tiers, existing
 * rows disabled, per-group select-all/none, the empty status, an inline root
 * refusal, the stale-selection rescan prompt, and failed-row rendering.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import ProjectScaffolderPage from '../apps/project-scaffolder/ProjectScaffolderPage'
import { api } from '../api/client'

const ROOT = '/work/monorepo'

/** Two groups, mixed tiers, and one already-scaffolded row. */
const SCAN = {
  root: ROOT,
  root_name: 'monorepo',
  root_existing: false,
  status: 'ok',
  candidates: [
    {
      path: `${ROOT}/services`, name: 'services', parent_path: null,
      tier: 'auto', signals: ['package.json'], existing: false, selected: true,
    },
    {
      path: `${ROOT}/services/api`, name: 'api', parent_path: `${ROOT}/services`,
      tier: 'auto', signals: ['pyproject.toml'], existing: false, selected: true,
    },
    {
      path: `${ROOT}/services/legacy`, name: 'legacy', parent_path: `${ROOT}/services`,
      tier: 'offered', signals: ['Makefile'], existing: false, selected: false,
    },
    {
      path: `${ROOT}/services/done`, name: 'done', parent_path: `${ROOT}/services`,
      tier: 'auto', signals: ['package.json'], existing: true, selected: false,
    },
  ],
  groups: [
    { parent_path: null, paths: [`${ROOT}/services`] },
    {
      parent_path: `${ROOT}/services`,
      paths: [`${ROOT}/services/api`, `${ROOT}/services/legacy`, `${ROOT}/services/done`],
    },
  ],
  warnings: ['depth cap reached under /work/monorepo/vendor'],
}

const EMPTY_SCAN = {
  root: ROOT, root_name: 'monorepo', root_existing: false, status: 'empty',
  candidates: [], groups: [], warnings: [],
}

/** Queue of responses `fetch` hands out, in call order. */
let queued: { status: number; body: unknown }[] = []
let calls: { url: string; body: Record<string, unknown> }[] = []

function mockFetch() {
  return vi.fn(async (url: string, init?: RequestInit) => {
    calls.push({ url, body: JSON.parse(String(init?.body ?? '{}')) })
    const next = queued.shift() ?? { status: 500, body: { error: 'no response queued' } }
    return {
      ok: next.status < 400,
      status: next.status,
      json: async () => next.body,
      text: async () => JSON.stringify(next.body),
    } as Response
  })
}

beforeEach(() => {
  queued = []
  calls = []
  vi.stubGlobal('fetch', mockFetch())
  // The picker's own directory listings. Spied on the shared client so they never
  // consume from the POST queue above.
  vi.spyOn(api, 'recentProjects').mockResolvedValue({ dirs: [ROOT, '/work/other'] })
  vi.spyOn(api, 'browseDirs').mockResolvedValue({
    path: '/work', parent: '/', dirs: [{ name: 'monorepo', path: ROOT }],
  })
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

/** Type a root and press Scan, then wait for the preview (or empty state) to land. */
async function scan(user: ReturnType<typeof userEvent.setup>, root = ROOT) {
  await user.type(screen.getByLabelText('Project directory'), root)
  await user.click(screen.getByRole('button', { name: 'Scan' }))
}

/** Choose the root from the picker instead of typing it, then press Scan. */
async function scanViaPicker(user: ReturnType<typeof userEvent.setup>, root = ROOT) {
  await user.click(screen.getByTestId('scaffolder-browse'))
  await user.click(await screen.findByRole('option', { name: new RegExp(root) }))
  await waitFor(() => expect(screen.getByLabelText('Project directory')).toHaveValue(root))
  await user.click(screen.getByRole('button', { name: 'Scan' }))
}

describe('ProjectScaffolderPage', () => {
  it('fills the root from the shared project picker without scanning on selection', async () => {
    const user = userEvent.setup()
    queued.push({ status: 200, body: SCAN })
    render(<ProjectScaffolderPage />)

    await user.click(screen.getByTestId('scaffolder-browse'))
    // The same picker the sidebar's folder settings launches — recent + browse.
    // Two "Browse" texts while it is open: the launcher and the picker's own tab,
    // so the tab is counted rather than fetched by text alone.
    expect(await screen.findByText('Recent')).toBeInTheDocument()
    expect(screen.getAllByText('Browse')).toHaveLength(2)

    await user.click(await screen.findByRole('option', { name: new RegExp(ROOT) }))

    // A pick stages the path and stops: the field holds it, the picker is gone,
    // and no scan has been requested yet.
    const field = screen.getByLabelText('Project directory')
    await waitFor(() => expect(field).toHaveValue(ROOT))
    expect(screen.queryByText('Recent')).not.toBeInTheDocument()
    expect(calls).toHaveLength(0)
    // Focus lands back on the field, so the path is editable and Enter scans it.
    expect(field).toHaveFocus()

    await user.keyboard('{Enter}')
    await waitFor(() => expect(screen.getAllByTestId('preview-group')).toHaveLength(2))
    expect(calls[0]).toEqual({ url: '/api/chat/folders/scan', body: { root: ROOT } })
  })

  it('opens the picker from the keyboard and leaves the root untouched on Escape', async () => {
    const user = userEvent.setup()
    render(<ProjectScaffolderPage />)

    const browse = screen.getByTestId('scaffolder-browse')
    await user.tab()
    await user.tab()
    expect(browse).toHaveFocus()

    await user.keyboard('{Enter}')
    expect(await screen.findByText('Recent')).toBeInTheDocument()

    // Escape abandons the pick: nothing is chosen and focus is not stranded in a
    // dropdown that no longer exists.
    await user.keyboard('{Escape}')
    await waitFor(() => expect(screen.queryByText('Recent')).not.toBeInTheDocument())
    expect(screen.getByLabelText('Project directory')).toHaveValue('')
    expect(calls).toHaveLength(0)

    // Re-opening and choosing with the keyboard alone commits the highlighted row.
    await user.click(browse)
    expect(await screen.findByText('Recent')).toBeInTheDocument()
    await user.keyboard('{Enter}')
    await waitFor(() => expect(screen.getByLabelText('Project directory')).toHaveValue(ROOT))
  })

  it('scans a picked root end to end, same as a typed one', async () => {
    const user = userEvent.setup()
    queued.push({ status: 200, body: SCAN })
    render(<ProjectScaffolderPage />)
    await scanViaPicker(user)

    await waitFor(() => expect(screen.getAllByTestId('preview-group')).toHaveLength(2))
    expect(calls[0]).toEqual({ url: '/api/chat/folders/scan', body: { root: ROOT } })
    expect(screen.getByTestId('selected-count')).toHaveTextContent('2 selected')
  })

  it('renders a grouped preview with tiers, signals, and the server default selection', async () => {
    const user = userEvent.setup()
    queued.push({ status: 200, body: SCAN })
    render(<ProjectScaffolderPage />)
    await scan(user)

    await waitFor(() => expect(screen.getAllByTestId('preview-group')).toHaveLength(2))
    expect(calls[0].url).toBe('/api/chat/folders/scan')
    expect(calls[0].body).toEqual({ root: ROOT })

    // Both tiers are visible and distinguishable, and each row shows its signals.
    expect(screen.getAllByText('Confident')).toHaveLength(3)
    expect(screen.getByText('Offered')).toBeInTheDocument()
    expect(screen.getByText('pyproject.toml')).toBeInTheDocument()

    // The server's own default selection is honored: the two non-existing AUTO rows.
    expect(screen.getByTestId('selected-count')).toHaveTextContent('2 selected')
    // Warnings are surfaced rather than swallowed.
    expect(screen.getByTestId('scan-warnings')).toHaveTextContent('depth cap reached')
  })

  it('shows an already-scaffolded row with a disabled checkbox it cannot tick', async () => {
    const user = userEvent.setup()
    queued.push({ status: 200, body: SCAN })
    render(<ProjectScaffolderPage />)
    await scan(user)

    await waitFor(() => expect(screen.getAllByTestId('candidate-row')).toHaveLength(4))
    const existing = screen.getByLabelText(`${ROOT}/services/done (Already set up)`)
    expect(existing).toBeDisabled()
    expect(existing).not.toBeChecked()
    expect(screen.getByTestId('already-set-up')).toBeInTheDocument()
  })

  it('select-all adds only the tickable rows of its own group, select-none clears them', async () => {
    const user = userEvent.setup()
    queued.push({ status: 200, body: SCAN })
    render(<ProjectScaffolderPage />)
    await scan(user)

    await waitFor(() => expect(screen.getAllByTestId('preview-group')).toHaveLength(2))
    const nested = screen.getAllByTestId('preview-group')[1]

    // The nested group holds api + legacy + done; select-all must reach the first
    // two and leave the already-scaffolded one alone.
    await user.click(within(nested).getByRole('button', { name: 'Select all' }))
    expect(screen.getByTestId('selected-count')).toHaveTextContent('3 selected')
    expect(screen.getByLabelText(`${ROOT}/services/done (Already set up)`)).not.toBeChecked()

    await user.click(within(nested).getByRole('button', { name: 'Select none' }))
    // The root-level group keeps its own selection, proving the bulk action is scoped.
    expect(screen.getByTestId('selected-count')).toHaveTextContent('1 selected')
    expect(screen.getByLabelText(`${ROOT}/services`)).toBeChecked()
  })

  it('posts exactly the ticked paths and reports created, skipped, and failed rows', async () => {
    const user = userEvent.setup()
    queued.push({ status: 200, body: SCAN })
    queued.push({
      status: 200,
      body: {
        root: ROOT, root_folder_id: 'f0',
        created: [{ path: `${ROOT}/services`, folder_id: 'f1', name: 'services' }],
        skipped_existing: [`${ROOT}/services/done`],
        failed: [{
          path: `${ROOT}/services/api`,
          error: 'color must be one of the folder palette values',
          code: 'color_invalid',
        }],
        warnings: [],
      },
    })
    render(<ProjectScaffolderPage />)
    await scan(user)
    await waitFor(() => expect(screen.getAllByTestId('preview-group')).toHaveLength(2))

    // Untick one of the two defaults, so the posted set is provably the live one.
    await user.click(screen.getByLabelText(`${ROOT}/services/api`))
    await user.click(screen.getByRole('button', { name: 'Create folders' }))

    await waitFor(() => expect(screen.getByTestId('scaffold-results')).toBeInTheDocument())
    expect(calls[1].url).toBe('/api/chat/folders/scaffold')
    expect(calls[1].body).toEqual({ root: ROOT, selected: [`${ROOT}/services`] })

    expect(screen.getByTestId('result-created')).toHaveTextContent('1 created')
    expect(screen.getByTestId('result-skipped')).toHaveTextContent('1 already existed')
    expect(screen.getByTestId('result-failed')).toHaveTextContent('1 failed')
    // A failed row carries the server's prose and its machine-readable code.
    const failed = screen.getByTestId('failed-rows')
    expect(failed).toHaveTextContent('color must be one of the folder palette values')
    expect(failed).toHaveTextContent('color_invalid')
  })

  it('distinguishes an empty scan from a populated one and still offers the root folder', async () => {
    const user = userEvent.setup()
    queued.push({ status: 200, body: EMPTY_SCAN })
    render(<ProjectScaffolderPage />)
    await scan(user)

    await waitFor(() => expect(screen.getByTestId('scan-empty')).toBeInTheDocument())
    expect(screen.getByTestId('scan-empty-title')).toHaveTextContent('No sub-projects found')
    // Not a preview: no checklist is rendered at all.
    expect(screen.queryAllByTestId('preview-group')).toHaveLength(0)
    expect(screen.getByRole('button', { name: 'Create the root folder only' })).toBeEnabled()
  })

  it('renders a refused root verbatim against the field', async () => {
    const user = userEvent.setup()
    queued.push({
      status: 400,
      body: { error: 'project_dir must be an existing directory', code: 'folder_scan_root_invalid' },
    })
    render(<ProjectScaffolderPage />)
    await scan(user, '/no/such/place')

    await waitFor(() => expect(screen.getByTestId('root-error')).toBeInTheDocument())
    // The server's own sentence, not a re-worded local one.
    expect(screen.getByTestId('root-error')).toHaveTextContent('project_dir must be an existing directory')
    expect(screen.getByLabelText('Project directory')).toHaveAttribute('aria-invalid', 'true')
    expect(screen.queryAllByTestId('preview-group')).toHaveLength(0)
  })

  it('turns a stale-selection refusal into a rescan prompt naming the dropped paths', async () => {
    const user = userEvent.setup()
    queued.push({ status: 200, body: SCAN })
    queued.push({
      status: 400,
      body: {
        error: 'selection is out of date — re-scan before creating folders',
        code: 'folder_scaffold_selection_stale',
        unknown: [`${ROOT}/services/api`],
      },
    })
    // The rescan returns a tree that no longer holds the vanished directory.
    queued.push({
      status: 200,
      body: { ...SCAN, candidates: [SCAN.candidates[0]], groups: [SCAN.groups[0]], warnings: [] },
    })
    render(<ProjectScaffolderPage />)
    await scan(user)
    await waitFor(() => expect(screen.getAllByTestId('preview-group')).toHaveLength(2))

    await user.click(screen.getByRole('button', { name: 'Create folders' }))
    await waitFor(() => expect(screen.getByTestId('stale-selection')).toBeInTheDocument())
    const stale = screen.getByTestId('stale-selection')
    expect(stale).toHaveTextContent('Scan again before creating folders')
    expect(stale).toHaveTextContent(`${ROOT}/services/api`)
    // A stale selection is not an outcome, so no results card is shown.
    expect(screen.queryByTestId('scaffold-results')).not.toBeInTheDocument()

    // The prompt's own action re-scans the same root and replaces the preview.
    await user.click(within(stale).getByRole('button', { name: 'Re-scan' }))
    await waitFor(() => expect(screen.getAllByTestId('candidate-row')).toHaveLength(1))
    expect(calls[2]).toEqual({ url: '/api/chat/folders/scan', body: { root: ROOT } })
    expect(screen.queryByTestId('stale-selection')).not.toBeInTheDocument()
  })
})
