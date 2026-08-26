/**
 * The sources view: the submitter-class autonomy grid, as a read.
 *
 * The central trust decision of outside intake — who may run how unattended — is
 * this matrix, and every property asserted here is one whose loss would mislead an
 * operator about authority rather than merely look wrong:
 *
 *   - **The axes are the payload's, not this file's.** The engine ships its
 *     submitter classes and spec types with the matrix, so a schema that grows a
 *     class renders it without a frontend edit. A hard-coded axis would silently
 *     stop showing a class the resolver answers for — the one direction of this bug
 *     that hides authority instead of inventing it.
 *   - **An unconfigured cell states that it waits for a human.** The unconfigured
 *     default is the authoring rung, which covers no gate. A blank, a zero, or the
 *     bare word `authoring` is indistinguishable from a rung somebody chose, and
 *     only one of the two is a decision.
 *   - **A cell says which declaration answered it, with the path.** An exact cell,
 *     a wildcard cell and no cell at all are three different edits to make next,
 *     and the wildcard's path is spelled `…autonomy.<class>.default` — the engine's
 *     literal wildcard key, not a `*`.
 *   - **A failed read renders no values.** React Query keeps the last successful
 *     answer across a failing refetch, so a matrix built from retained data would
 *     state who may run unattended on the strength of a read that did not happen.
 *   - **No sources is not an empty matrix.** A grid with no source to belong to
 *     reads as "no authority granted" when the fact is that nothing is ingested at
 *     all, so the state names where a source comes from instead.
 *   - **The semantics are stated where the values are read.** The fail-closed
 *     rules — unclassifiable authors, implied lower rungs, the screening cap — are
 *     not inferable from a matrix of words, and inferring one wrongly means
 *     granting authority nobody meant to grant.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import SpecEnginePage from '../apps/spec-engine/SpecEnginePage'
import en from '../i18n/locales/en.json'

const T = en.apps.specEngine.sourcesSection
const C = en.apps.specEngine.configPanel
const P = en.apps.specEngine.specEnginePage

/** The engine's wildcard key, as it appears inside a declaring path. */
const WILDCARD = 'default'

type Answer = { status?: number; body: unknown }

/** Every request the page made, so an assertion can read the body that was sent. */
const calls: Array<{ url: string; method: string; body: unknown }> = []

/** One resolved cell, in `_source_grid`'s shape. */
function cell(
  level: string,
  origin: 'exact' | 'wildcard' | 'default',
  declaredAt: string,
  covers: boolean,
) {
  return { level, origin, declared_at: declaredAt, policy_covers_gates: covers }
}

/** An all-default row: what a class nobody wrote a cell for resolves to. */
function defaultRow(specTypes: readonly string[]) {
  return Object.fromEntries(
    // `declared_at` is `''` when nothing answered — the route's own spelling,
    // never null and never absent.
    specTypes.map((specType) => [specType, cell('authoring', 'default', '', false)]),
  )
}

const CLASSES = ['maintainer', 'member', 'contributor', 'external'] as const
const TYPES = ['feature', 'bugfix', 'quick'] as const

/**
 * One source carrying all three origins at once: an exact cell for the
 * maintainer's feature work, a wildcard row for contributors, and defaults
 * everywhere else.
 */
function grid() {
  return {
    maintainer: {
      ...defaultRow(TYPES),
      feature: cell('delivery', 'exact', 'sources.gh.autonomy.maintainer.feature', true),
    },
    member: defaultRow(TYPES),
    contributor: Object.fromEntries(
      TYPES.map((specType) => [
        specType,
        cell('execution', 'wildcard', `sources.gh.autonomy.contributor.${WILDCARD}`, true),
      ]),
    ),
    external: defaultRow(TYPES),
  }
}

/** The sources read, in `_sources_snapshot`'s shape. */
function sources(over: Record<string, unknown> = {}) {
  return {
    sources: [{ name: 'gh', grid: grid() }],
    submitter_classes: [...CLASSES],
    spec_types: [...TYPES],
    levels: ['authoring', 'execution', 'delivery', 'integration'],
    ...over,
  }
}

/** The config read's shape, with one project so the pane renders its tables. */
function snapshot() {
  return {
    configured: true,
    path: '/home/me/.kiro/crew/apps/spec-engine/config.json',
    document: { projects: { acme: { path: '/src/acme' } } },
    elided: [],
    elided_marker: '<elided>',
    errors: [],
    advisories: [],
    config_only_paths: [],
  }
}

function stub(answers: {
  sources?: Answer
  /** The answer from the SECOND sources read onwards, for a failing refetch. */
  sourcesAgain?: Answer
  /** The answer to the sources read once a PUT has landed, as the store would then. */
  sourcesAfterPut?: Answer
  /** The write door's answer. Defaults to accepting the patch. */
  put?: Answer
}) {
  let reads = 0
  let written = false
  vi.stubGlobal(
    'fetch',
    vi.fn((url: string, init?: RequestInit) => {
      const method = init?.method ?? 'GET'
      calls.push({ url, method, body: init?.body ? JSON.parse(String(init.body)) : undefined })
      let answer: Answer
      if (method === 'PUT') {
        answer = answers.put ?? { body: { ok: true, document: {}, advisories: [] } }
        written = (answer.status ?? 200) < 300
      } else if (url.startsWith('/api/apps/spec-engine/config/registry')) {
        // The settings form is generated from this read, and it must be answered
        // BEFORE the generic '/config' prefix below, which would otherwise hand it
        // a ConfigSnapshot and crash its render. Answered with an empty vocabulary:
        // this suite is about the autonomy grid, and the generated form's own
        // properties live in `SpecEngineSettingsForm.test.tsx`.
        answer = {
          body: {
            settings: [],
            source_presets: [],
            profile_presets: [],
            roles: [],
            levels: [],
          },
        }
      } else if (url.startsWith('/api/apps/spec-engine/config/sources')) {
        reads += 1
        answer =
          (written ? answers.sourcesAfterPut : undefined) ??
          (reads > 1 ? answers.sourcesAgain : undefined) ??
          answers.sources ?? { body: sources() }
      } else if (url.startsWith('/api/apps/spec-engine/config/resolved')) {
        answer = {
          body: {
            configured: true,
            project: null,
            source: null,
            settings: [],
            roles: { profile: '', roles: {} },
            role_order: [],
          },
        }
      } else if (url.startsWith('/api/apps/spec-engine/config')) {
        answer = { body: snapshot() }
      } else if (url.startsWith('/api/apps/spec-engine/kill-switch')) {
        answer = {
          body: {
            switch: { engaged: false, unreadable: false },
            stoppable: [],
            stoppable_credits: 0,
          },
        }
      } else {
        answer = { body: { entries: [], grouped: {}, total: 0, total_credits: 0 } }
      }
      const status = answer.status ?? 200
      return Promise.resolve({
        ok: status >= 200 && status < 300,
        status,
        text: () => Promise.resolve(JSON.stringify(answer.body)),
      })
    }),
  )
  return { reads: () => reads }
}

/** Render the page, switch to the configuration pane, and wait for the section.
 *
 * The pane leads with the forms, so there is no editor to wait on: the wait is for
 * the sources read itself to have landed, which is what the pending note leaving
 * says. Waiting only for the heading would resolve while the section is still
 * reading, and the matrix would not be on screen yet.
 */
async function openConfig() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchInterval: false } },
  })
  const rendered = render(
    <QueryClientProvider client={client}>
      <SpecEnginePage />
    </QueryClientProvider>,
  )
  const nav = await screen.findByRole('button', { name: new RegExp(P.configuration) })
  fireEvent.click(nav)
  // The pane's editing surfaces are tabs now, and only the active one is reachable:
  // an inactive panel carries `hidden`, which takes it out of the accessibility tree
  // the role queries read. The grid shares the Watch sources tab with the form that
  // links into it. The section heading is found by ROLE rather than by text, because
  // that tab's label is the same words.
  fireEvent.click(await screen.findByRole('tab', { name: new RegExp(`^${C.tab_watch_sources}`) }))
  await screen.findByRole('heading', { name: T.watch_sources })
  await waitFor(() => expect(screen.queryByText(T.reading_the_watch_sources)).toBeNull())
  return { client, ...rendered }
}

/** The matrix for the selected source, by its accessible name. */
function matrix(source = 'gh'): HTMLElement {
  return screen.getByRole('table', {
    name: T.autonomy_for_source.replace('{{source}}', source),
  })
}

/**
 * The watch-sources block.
 *
 * Every query for one of the review card's controls is scoped to it, because each
 * form on the pane owns its OWN copy of that card's words — "Review the exact
 * change" is the settings form's label and the profiles form's too — so an unscoped
 * query cannot tell one form's confirm from another's.
 */
function block(): HTMLElement {
  const heading = screen.getByRole('heading', { name: T.watch_sources })
  const found = heading.closest('.se-blk')
  expect(found).not.toBeNull()
  return found as HTMLElement
}

/** One cell of the matrix, addressed by class row and spec-type column. */
function gridCell(klass: string, specType: string, source = 'gh'): HTMLElement {
  const table = matrix(source)
  const headers = within(table).getAllByRole('columnheader')
  // The first column header is the class column's own, so the data columns start
  // one past it — the same offset the row's cells have.
  const column = headers.findIndex((header) => header.textContent === specType) - 1
  const row = within(table)
    .getAllByRole('row')
    .find((candidate) => within(candidate).queryByRole('rowheader')?.textContent === klass)
  expect(row, `no row for ${klass}`).toBeTruthy()
  const cells = within(row as HTMLElement).getAllByRole('cell')
  return cells[column]
}

/**
 * The level the matrix states is in force for a pair.
 *
 * Read from the level element rather than from the cell's text, because the cell
 * also holds the edit select, whose options spell every level in the ladder: a text
 * assertion over the whole cell would pass for a level the cell does not show. That
 * distinction is the point — the level is the store's, the select is a proposal.
 */
function levelInForce(klass: string, specType: string, source = 'gh'): string {
  const shown = gridCell(klass, specType, source).querySelector('.se-glevel')
  return shown?.textContent ?? ''
}

afterEach(() => {
  vi.unstubAllGlobals()
  calls.length = 0
})

describe('the grid renders every pair with the origin that answered it', () => {
  it('lists every source and names the one it is showing', async () => {
    stub({
      sources: {
        body: sources({
          sources: [
            { name: 'gh', grid: grid() },
            // A configured source with no grid at all: every cell defaults, which
            // is exactly the fail-closed case an operator most needs to see.
            {
              name: 'forgejo',
              grid: Object.fromEntries(CLASSES.map((klass) => [klass, defaultRow(TYPES)])),
            },
          ],
        }),
      },
    })
    await openConfig()
    expect(screen.getByRole('button', { name: 'gh' })).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByRole('button', { name: 'forgejo' })).toHaveAttribute('aria-pressed', 'false')
    // The matrix is named for the source it belongs to, so a reader cannot take one
    // source's authority for another's.
    expect(matrix('gh')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'forgejo' }))
    expect(matrix('forgejo')).toBeInTheDocument()
    for (const klass of CLASSES) {
      expect(gridCell(klass, 'feature', 'forgejo')).toHaveTextContent(T.origin_unconfigured)
    }
  })

  it('renders the full cross product of the axes the payload shipped', async () => {
    stub({})
    await openConfig()
    const table = matrix()
    for (const klass of CLASSES) {
      expect(within(table).getByRole('rowheader', { name: klass })).toBeInTheDocument()
    }
    for (const specType of TYPES) {
      expect(within(table).getByRole('columnheader', { name: specType })).toBeInTheDocument()
    }
    // Four classes by three spec types, and nothing else: a matrix that dropped a
    // pair would hide a rung somebody granted.
    expect(within(table).getAllByRole('cell')).toHaveLength(CLASSES.length * TYPES.length)
  })

  it('renders axes it has never seen, because they come from the payload', async () => {
    // The gate on a hard-coded axis. A schema that adds a class must appear here
    // with no frontend edit, and a surface must never render an axis of its own
    // that the resolver has no answer for.
    stub({
      sources: {
        body: {
          sources: [
            {
              name: 'gh',
              grid: {
                bot: {
                  triage: cell('execution', 'exact', 'sources.gh.autonomy.bot.triage', true),
                },
              },
            },
          ],
          submitter_classes: ['bot'],
          spec_types: ['triage'],
          levels: ['authoring', 'execution'],
        },
      },
    })
    await openConfig()
    const table = matrix()
    expect(within(table).getByRole('rowheader', { name: 'bot' })).toBeInTheDocument()
    expect(within(table).getByRole('columnheader', { name: 'triage' })).toBeInTheDocument()
    expect(within(table).getAllByRole('cell')).toHaveLength(1)
    for (const klass of CLASSES) {
      expect(within(table).queryByRole('rowheader', { name: klass })).toBeNull()
    }
    // The sentence about unclassifiable authors names the class the ENGINE puts
    // last, not one this surface believes in.
    expect(
      screen.getByText(T.an_unclassifiable_author_is_least_trusted.replace('{{klass}}', 'bot')),
    ).toBeInTheDocument()
  })

  it('distinguishes an exact cell from a wildcard cell, and shows both paths', async () => {
    stub({})
    await openConfig()
    const exact = gridCell('maintainer', 'feature')
    expect(levelInForce('maintainer', 'feature')).toBe('delivery')
    expect(exact).toHaveTextContent(T.origin_exact)
    expect(exact).toHaveTextContent('sources.gh.autonomy.maintainer.feature')

    const wildcard = gridCell('contributor', 'bugfix')
    expect(levelInForce('contributor', 'bugfix')).toBe('execution')
    expect(wildcard).toHaveTextContent(T.origin_wildcard)
    // The wildcard segment is the engine's literal key, not a `*`: an edit built
    // against the wrong spelling would write a cell the resolver never consults.
    expect(wildcard).toHaveTextContent(`sources.gh.autonomy.contributor.${WILDCARD}`)
    expect(wildcard).not.toHaveTextContent(T.origin_exact)
  })

  it('states that an unconfigured cell waits for a human rather than leaving it blank', async () => {
    stub({})
    await openConfig()
    const unset = gridCell('external', 'feature')
    // The level is still shown — the default IS a resolution — and the wording is
    // what separates it from a rung somebody chose.
    expect(levelInForce('external', 'feature')).toBe('authoring')
    expect(unset).toHaveTextContent(T.origin_unconfigured)
    expect(unset.textContent?.trim()).not.toBe('')
    // And no declaring path, because nothing declared it.
    expect(unset).not.toHaveTextContent('sources.gh.autonomy')
  })

  it('marks only the cells whose gates the policy approves without a human', async () => {
    stub({})
    await openConfig()
    expect(gridCell('maintainer', 'feature')).toHaveTextContent(T.unattended)
    expect(gridCell('contributor', 'quick')).toHaveTextContent(T.unattended)
    // The least-trusted class defaults to a rung that covers no gate, so its runs
    // park for a person: the marker must not appear there.
    expect(gridCell('external', 'feature')).not.toHaveTextContent(T.unattended)
    expect(gridCell('maintainer', 'bugfix')).not.toHaveTextContent(T.unattended)
  })
})

describe('doubt about the read never renders as authority', () => {
  it('states a failed read and renders no matrix', async () => {
    stub({
      sources: {
        status: 422,
        body: { code: 'config_invalid', error: 'sources.gh.autonomy.external.feature: unknown' },
      },
    })
    await openConfig()
    await screen.findByText(T.could_not_read_the_watch_sources)
    expect(screen.queryByRole('table', { name: /Autonomy for/ })).toBeNull()
    expect(screen.queryByText(T.origin_unconfigured)).toBeNull()
    // The engine's own refusal, by code, so the repair names its path.
    expect(screen.getByText(/config_invalid/)).toBeInTheDocument()
  })

  it('drops a matrix it had already rendered when a refetch fails', async () => {
    // The guard that cannot be seen by looking at the first render: React Query
    // keeps the last successful answer, so a section that reached for the data
    // before `isError` would keep showing a stale grant as current.
    const { client } = await openConfigWith({
      sources: { body: sources() },
      sourcesAgain: { status: 503, body: { code: 'config_unreadable', error: 'disk gone' } },
    })
    expect(levelInForce('maintainer', 'feature')).toBe('delivery')

    await client.invalidateQueries({ queryKey: ['spec-engine', 'config', 'sources'] })
    await waitFor(() => {
      expect(screen.getByText(T.could_not_read_the_watch_sources)).toBeInTheDocument()
    })
    expect(screen.queryByRole('table', { name: /Autonomy for/ })).toBeNull()
    expect(screen.queryByText('delivery')).toBeNull()
  })

  it('names no source as an offer flow rather than drawing an empty matrix', async () => {
    stub({ sources: { body: sources({ sources: [] }) } })
    await openConfig()
    expect(screen.getByText(T.no_watch_source_is_configured)).toBeInTheDocument()
    expect(screen.queryByRole('table', { name: /Autonomy for/ })).toBeNull()
    // Where a source comes from, named: this section never creates one.
    expect(T.no_watch_source_is_configured).toMatch(/setup assistant/i)
    // The loading line is a different sentence, so "not read yet" cannot be read
    // as "nothing is configured".
    expect(screen.queryByText(T.reading_the_watch_sources)).toBeNull()
  })

  it('says it is reading before the answer arrives', async () => {
    let release: (() => void) | undefined
    const held = new Promise<void>((resolve) => {
      release = resolve
    })
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) => {
        if (url.startsWith('/api/apps/spec-engine/config/sources')) await held
        const body = url.startsWith('/api/apps/spec-engine/config/sources')
          ? sources()
          : url.startsWith('/api/apps/spec-engine/config/registry')
            ? {
                settings: [],
                source_presets: [],
                profile_presets: [],
                roles: [],
                levels: [],
              }
            : url.startsWith('/api/apps/spec-engine/config/resolved')
              ? {
                  configured: true,
                  project: null,
                  source: null,
                  settings: [],
                  roles: { profile: '', roles: {} },
                  role_order: [],
                }
              : url.startsWith('/api/apps/spec-engine/config')
                ? snapshot()
                : url.startsWith('/api/apps/spec-engine/kill-switch')
                  ? {
                      switch: { engaged: false, unreadable: false },
                      stoppable: [],
                      stoppable_credits: 0,
                    }
                  : { entries: [], grouped: {}, total: 0, total_credits: 0 }
        return { ok: true, status: 200, text: () => Promise.resolve(JSON.stringify(body)) }
      }),
    )
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false, refetchInterval: false } },
    })
    render(
      <QueryClientProvider client={client}>
        <SpecEnginePage />
      </QueryClientProvider>,
    )
    fireEvent.click(await screen.findByRole('button', { name: new RegExp(P.configuration) }))
    // The grid lives on the Watch sources tab, and only the active tab's panel is
    // reachable: an inactive one carries `hidden`.
    fireEvent.click(await screen.findByRole('tab', { name: new RegExp(`^${C.tab_watch_sources}`) }))
    await screen.findByText(T.reading_the_watch_sources)
    expect(screen.queryByText(T.no_watch_source_is_configured)).toBeNull()
    release?.()
    await waitFor(() => expect(matrix()).toBeInTheDocument())
  })
})

describe('the semantics are stated beside the values', () => {
  it('states the fail-closed rules once in the section', async () => {
    stub({})
    await openConfig()
    // Unclassifiable authors fall to the least-trusted class, named from the
    // payload's own order.
    expect(
      screen.getByText(T.an_unclassifiable_author_is_least_trusted.replace('{{klass}}', 'external')),
    ).toBeInTheDocument()
    // A level authorizes every level below it.
    expect(screen.getByText(T.a_level_authorizes_every_level_below_it)).toBeInTheDocument()
    // Screening caps a flagged item to authoring, and only ever lowers.
    expect(screen.getByText(T.screening_caps_a_flagged_item_to_authoring)).toBeInTheDocument()
    expect(T.screening_caps_a_flagged_item_to_authoring).toMatch(/authoring/)
    expect(T.screening_caps_a_flagged_item_to_authoring).toMatch(/lowers/i)
    // What the unattended marker means, so the flag is legible without guessing.
    expect(screen.getByText(T.execution_or_above_needs_no_human_at_a_gate)).toBeInTheDocument()
    // Each sentence once, not per cell.
    expect(screen.getAllByText(T.a_level_authorizes_every_level_below_it)).toHaveLength(1)
  })

  it('keeps the semantics on screen when there is no source to show them for', async () => {
    // The rules govern resolution, not any one source: an operator reading the
    // empty state is exactly the one about to configure a first grid.
    stub({ sources: { body: sources({ sources: [] }) } })
    await openConfig()
    expect(screen.getByText(T.a_level_authorizes_every_level_below_it)).toBeInTheDocument()
    expect(screen.getByText(T.screening_caps_a_flagged_item_to_authoring)).toBeInTheDocument()
  })
})

/** Render with a given stub, returning the query client for an invalidation. */
async function openConfigWith(answers: Parameters<typeof stub>[0]) {
  stub(answers)
  return openConfig()
}

// --- the guarded edit path ---------------------------------------------------

/**
 * Choose a level for one pair: pick the cell, then press the rung.
 *
 * Two acts rather than one because the level control is shared and sits under the
 * matrix in flow — a per-cell dropdown would draw its popup over the page, and this
 * surface holds its safety guarantees by having no overlay at all.
 */
function choose(klass: string, specType: string, level: string, source = 'gh') {
  fireEvent.click(
    within(gridCell(klass, specType, source)).getByRole('button', {
      name: T.change_the_level_for_pair
        .replace('{{klass}}', klass)
        .replace('{{specType}}', specType)
        .replace('{{source}}', source),
    }),
  )
  const control = screen.getByRole('group', {
    name: T.level_for_pair
      .replace('{{klass}}', klass)
      .replace('{{specType}}', specType)
      .replace('{{source}}', source),
  })
  fireEvent.click(within(control).getByRole('button', { name: level }))
}

/** Open the review card for whatever is pending. */
function review() {
  fireEvent.click(within(block()).getByRole('button', { name: T.review_the_exact_change }))
}

/** The patch on screen, parsed — the exact object a confirm would send. */
function shownPatch(): unknown {
  return JSON.parse(screen.getByText(/"autonomy"/).textContent ?? '')
}

/** The patch the one PUT carried. */
function putPatch(): unknown {
  const put = calls.filter((call) => call.method === 'PUT')
  expect(put).toHaveLength(1)
  return (put[0].body as { patch: unknown }).patch
}

/** Requests made after the PUT, so a refresh (or its absence) is observable. */
function readsAfterThePut(): string[] {
  const index = calls.findIndex((call) => call.method === 'PUT')
  expect(index).toBeGreaterThanOrEqual(0)
  return calls.slice(index + 1).map((call) => call.url)
}

describe('an edit is shown exactly before it is written', () => {
  it('offers the ladder for a picked cell and presses the rung in force', async () => {
    stub({})
    await openConfig()
    // Nothing is picked yet, so the control says how to get one rather than acting on
    // a cell nobody chose.
    expect(screen.getByText(T.choose_a_cell_to_change_its_level)).toBeInTheDocument()

    fireEvent.click(
      within(gridCell('maintainer', 'feature')).getByRole('button', {
        name: T.change_the_level_for_pair
          .replace('{{klass}}', 'maintainer')
          .replace('{{specType}}', 'feature')
          .replace('{{source}}', 'gh'),
      }),
    )
    const control = screen.getByRole('group', {
      name: T.level_for_pair
        .replace('{{klass}}', 'maintainer')
        .replace('{{specType}}', 'feature')
        .replace('{{source}}', 'gh'),
    })
    // Every rung the payload shipped, and the pressed one is the level in force: a
    // control that pressed nothing would leave the stored rung unreadable from it.
    expect(within(control).getAllByRole('button').map((button) => button.textContent)).toEqual([
      'authoring',
      'execution',
      'delivery',
      'integration',
    ])
    expect(within(control).getByRole('button', { name: 'delivery' })).toHaveAttribute(
      'aria-pressed',
      'true',
    )
    expect(screen.queryByText(T.choose_a_cell_to_change_its_level)).toBeNull()
  })

  it('shows a choice in its cell without disturbing the level in force', async () => {
    stub({})
    await openConfig()
    choose('maintainer', 'feature', 'authoring')
    // The store still says `delivery`, and the cell says so: the choice is marked as
    // unwritten beside it, never in place of it.
    expect(levelInForce('maintainer', 'feature')).toBe('delivery')
    const cellShown = gridCell('maintainer', 'feature')
    expect(within(cellShown).getByText(T.not_written)).toBeInTheDocument()
    expect(cellShown).toHaveTextContent('authoring')
    expect(cellShown).toHaveAttribute('data-pending', 'true')
    // And only that cell carries a mark.
    expect(screen.getAllByText(T.not_written)).toHaveLength(1)
  })

  it('writes nothing until a confirm, then sends the minimal cell patch', async () => {
    stub({})
    await openConfig()
    // The whole point of the flow: a choice is not a write. Nothing has been sent
    // when the select changes, and nothing has been sent when the review opens.
    choose('external', 'feature', 'execution')
    expect(calls.some((call) => call.method === 'PUT')).toBe(false)
    review()
    expect(calls.some((call) => call.method === 'PUT')).toBe(false)

    const patch = {
      sources: { gh: { autonomy: { external: { feature: 'execution' } } } },
    }
    // The patch is shown as the payload itself, so approving the review is
    // approving what will be written — and what was written is what was approved.
    expect(shownPatch()).toEqual(patch)
    fireEvent.click(within(block()).getByRole('button', { name: T.write_the_change }))
    await waitFor(() => expect(calls.some((call) => call.method === 'PUT')).toBe(true))
    expect(putPatch()).toEqual(patch)
  })

  it('names the pair, the level in force and the level replacing it', async () => {
    stub({})
    await openConfig()
    choose('maintainer', 'feature', 'integration')
    review()
    // The exact cell already holds a level somebody chose, so the sentence says so
    // and names the cell it replaces.
    expect(
      screen.getByText(
        T.edit_replaces_the_pairs_own_level
          .replace('{{klass}}', 'maintainer')
          .replace('{{specType}}', 'feature')
          .replace('{{source}}', 'gh')
          .replace('{{path}}', 'sources.gh.autonomy.maintainer.feature')
          .replace('{{oldLevel}}', 'delivery')
          .replace('{{newLevel}}', 'integration'),
      ),
    ).toBeInTheDocument()
  })

  it('says an unconfigured pair was waiting for a human rather than set to authoring', async () => {
    stub({})
    await openConfig()
    choose('member', 'quick', 'delivery')
    review()
    expect(
      screen.getByText(
        T.edit_configures_an_unconfigured_pair
          .replace('{{klass}}', 'member')
          .replace('{{specType}}', 'quick')
          .replace('{{source}}', 'gh')
          .replace('{{path}}', 'sources.gh.autonomy.member.quick')
          .replace('{{oldLevel}}', 'authoring')
          .replace('{{newLevel}}', 'delivery'),
      ),
    ).toBeInTheDocument()
  })

  it('states that a wildcard-answered pair is narrowed and the broader rule left alone', async () => {
    stub({})
    await openConfig()
    // `contributor` is answered by a wildcard row, so this edit does NOT change the
    // rule the operator can see in the cell — it writes the pair's own cell under
    // it. Somebody expecting the broader rule to move would expect the other two
    // spec types to move with it, and they do not.
    choose('contributor', 'bugfix', 'authoring')
    review()
    expect(
      screen.getByText(
        T.edit_narrows_a_broader_rule
          .replace('{{klass}}', 'contributor')
          .replace('{{specType}}', 'bugfix')
          .replace('{{source}}', 'gh')
          .replace('{{declaredAt}}', `sources.gh.autonomy.contributor.${WILDCARD}`)
          .replace('{{path}}', 'sources.gh.autonomy.contributor.bugfix')
          .replace('{{oldLevel}}', 'execution')
          .replace('{{newLevel}}', 'authoring'),
      ),
    ).toBeInTheDocument()
    // And the patch proves it: the wildcard cell is not in it. The wildcard key is
    // the engine's literal `default`, so a patch carrying it would rewrite the rule
    // for every pair it answers.
    expect(shownPatch()).toEqual({
      sources: { gh: { autonomy: { contributor: { bugfix: 'authoring' } } } },
    })
  })

  it('warns when an edit raises the class an unclassifiable author falls to', async () => {
    stub({})
    await openConfig()
    choose('external', 'quick', 'delivery')
    review()
    // The consequence nothing in the JSON states: this class is where an author the
    // engine cannot identify lands, so the rung is granted to anyone at all.
    expect(
      screen.getByText(
        T.this_raises_the_least_trusted_class
          .replace(/\{\{klass\}\}/g, 'external')
          .replace('{{specType}}', 'quick')
          .replace('{{oldLevel}}', 'authoring')
          .replace('{{newLevel}}', 'delivery'),
      ),
    ).toBeInTheDocument()
  })

  it('does not warn when the same class is lowered, or when another class is raised', async () => {
    // The other direction of the same guard: a warning on every edit is a warning
    // nobody reads, and the two edits below are the ones that must not carry it.
    stub({
      sources: {
        body: sources({
          sources: [
            {
              name: 'gh',
              grid: {
                ...grid(),
                external: {
                  ...defaultRow(TYPES),
                  feature: cell('delivery', 'exact', 'sources.gh.autonomy.external.feature', true),
                },
              },
            },
          ],
        }),
      },
    })
    await openConfig()
    choose('external', 'feature', 'authoring')
    choose('maintainer', 'bugfix', 'integration')
    review()
    expect(screen.queryByText(new RegExp(T.this_raises_the_least_trusted_class.slice(0, 16)))).toBeNull()
  })

  it('withdraws a choice that matches what the pair already stores', async () => {
    stub({})
    await openConfig()
    choose('maintainer', 'feature', 'integration')
    expect(within(block()).getByRole('button', { name: T.review_the_exact_change })).toBeEnabled()
    // Back to the level the cell itself holds. Every write is recorded, so queueing
    // this would put a line in the durable record for a change nobody made.
    choose('maintainer', 'feature', 'delivery')
    expect(within(block()).getByRole('button', { name: T.review_the_exact_change })).toBeDisabled()
    expect(screen.queryByText(T.not_written)).toBeNull()
  })

  it('keeps a choice that matches the level a broader rule gave the pair', async () => {
    stub({})
    await openConfig()
    // Not a no-op: it pins the pair at the level it happens to have now, which is
    // what keeps it there when the broader rule moves.
    choose('contributor', 'feature', 'execution')
    expect(within(block()).getByRole('button', { name: T.review_the_exact_change })).toBeEnabled()
    review()
    expect(shownPatch()).toEqual({
      sources: { gh: { autonomy: { contributor: { feature: 'execution' } } } },
    })
  })

  it('drops the whole pending change on a discard', async () => {
    stub({})
    await openConfig()
    choose('external', 'feature', 'execution')
    review()
    fireEvent.click(screen.getByRole('button', { name: T.discard_the_pending_changes }))
    expect(screen.queryByText(T.the_change_that_would_be_written)).toBeNull()
    expect(within(block()).getByRole('button', { name: T.review_the_exact_change })).toBeDisabled()
    expect(calls.some((call) => call.method === 'PUT')).toBe(false)
  })

  it('lets go of a choice the refreshed answer no longer resolves', async () => {
    // A choice is only honest while the answer still carries the pair it names: the
    // review sentence quotes the level being replaced, and the patch would otherwise
    // write a cell into a grid whose shape has moved. A choice kept past that point
    // would sit under an unclearable "not written" mark that no confirm reaches,
    // telling the operator a write is queued when none can be.
    const { client } = await openConfigWith({
      sources: { body: sources() },
      sourcesAgain: {
        body: sources({
          sources: [
            {
              name: 'gh',
              grid: {
                ...grid(),
                // The source stays, and so does the class — only the pair's own cell
                // goes, which is the case the source-level reconciliation misses.
                external: Object.fromEntries(
                  TYPES.filter((specType) => specType !== 'feature').map((specType) => [
                    specType,
                    cell('authoring', 'default', '', false),
                  ]),
                ),
              },
            },
          ],
        }),
      },
    })
    choose('external', 'feature', 'execution')
    expect(screen.getAllByText(T.not_written)).toHaveLength(1)

    await client.invalidateQueries({ queryKey: ['spec-engine', 'config', 'sources'] })

    // No mark anywhere, and nothing left to review: the section is back to reading
    // the store, which is the only state a confirm can act on.
    await waitFor(() => expect(screen.queryByText(T.not_written)).toBeNull())
    expect(within(block()).getByRole('button', { name: T.review_the_exact_change })).toBeDisabled()
    expect(calls.some((call) => call.method === 'PUT')).toBe(false)
  })
})

describe('a refused write leaves the grid showing the store', () => {
  it('renders the refusal by path and keeps the stored level and origin on screen', async () => {
    stub({
      put: {
        status: 422,
        body: {
          code: 'config_invalid',
          error: 'sources.gh.autonomy.external.feature: unknown autonomy level',
        },
      },
    })
    await openConfig()
    choose('external', 'feature', 'execution')
    review()
    fireEvent.click(within(block()).getByRole('button', { name: T.write_the_change }))
    await screen.findByText(T.could_not_write_the_grid_change)
    // The engine's own words, against the path it named: this panel keeps no
    // validation of its own to paraphrase them with.
    expect(
      screen.getByText(/config_invalid.*sources\.gh\.autonomy\.external\.feature/),
    ).toBeInTheDocument()

    // The matrix is still the store's. The cell reads `authoring`, unconfigured —
    // NOT the `execution` that was submitted and refused.
    const refusedCell = gridCell('external', 'feature')
    expect(levelInForce('external', 'feature')).toBe('authoring')
    expect(refusedCell).toHaveTextContent(T.origin_unconfigured)
    expect(refusedCell).not.toHaveTextContent(T.unattended)
    // And nothing was re-read, because nothing changed: a refetch here would be a
    // request whose only purpose is to hide that the write did not happen.
    expect(readsAfterThePut()).toEqual([])
    // The choice is kept so it can be corrected and sent again, and it is marked as
    // unwritten rather than presented as the level in force.
    expect(screen.getByText(T.nothing_was_written_so_the_matrix_is_stored_state)).toBeInTheDocument()
    expect(within(refusedCell).getByText(T.not_written)).toBeInTheDocument()
    expect(shownPatch()).toEqual({
      sources: { gh: { autonomy: { external: { feature: 'execution' } } } },
    })
  })
})

describe('an accepted write is re-read rather than assumed', () => {
  it('re-renders the matrix from a fresh read, and re-reads the document beside it', async () => {
    stub({
      // The store's answer once the write has landed. Deliberately NOT the level
      // that was submitted: what the grid shows afterwards has to come from this
      // read, so a panel that adopted its own patch would show `execution` here.
      sourcesAfterPut: {
        body: sources({
          sources: [
            {
              name: 'gh',
              grid: {
                ...grid(),
                external: {
                  ...defaultRow(TYPES),
                  feature: cell(
                    'delivery',
                    'exact',
                    'sources.gh.autonomy.external.feature',
                    true,
                  ),
                },
              },
            },
          ],
        }),
      },
    })
    await openConfig()
    choose('external', 'feature', 'execution')
    review()
    fireEvent.click(within(block()).getByRole('button', { name: T.write_the_change }))
    await screen.findByText(T.wrote_the_change_and_re_read_the_matrix)

    await waitFor(() => {
      expect(gridCell('external', 'feature')).toHaveTextContent(T.origin_exact)
    })
    expect(levelInForce('external', 'feature')).toBe('delivery')
    expect(gridCell('external', 'feature')).toHaveTextContent('sources.gh.autonomy.external.feature')
    // The review card and the unwritten marks are gone: there is nothing pending,
    // and a card left on screen would invite a second write of the same patch.
    expect(screen.queryByText(T.the_change_that_would_be_written)).toBeNull()
    expect(screen.queryByText(T.not_written)).toBeNull()

    // The grid is a resolution OF the document, and the projects table beside it
    // reads the same document: both are invalidated, so the two views cannot
    // disagree on their next read.
    const after = readsAfterThePut()
    expect(after.some((url) => url.startsWith('/api/apps/spec-engine/config/sources'))).toBe(true)
    expect(after.some((url) => url === '/api/apps/spec-engine/config')).toBe(true)
    expect(after.some((url) => url.startsWith('/api/apps/spec-engine/config/resolved'))).toBe(true)
  })

  it('sends both cells when two pairs of one source are changed together', async () => {
    stub({})
    await openConfig()
    choose('external', 'feature', 'execution')
    choose('external', 'bugfix', 'execution')
    review()
    // One patch, two leaves under one source: a builder that replaced the source's
    // node per choice would send half of what the card displayed.
    expect(shownPatch()).toEqual({
      sources: { gh: { autonomy: { external: { feature: 'execution', bugfix: 'execution' } } } },
    })
    fireEvent.click(within(block()).getByRole('button', { name: T.write_the_change }))
    await waitFor(() => expect(calls.some((call) => call.method === 'PUT')).toBe(true))
    expect(putPatch()).toEqual({
      sources: { gh: { autonomy: { external: { feature: 'execution', bugfix: 'execution' } } } },
    })
  })

  it('keeps a choice against the source it was made on when another is shown', async () => {
    stub({
      sources: {
        body: sources({
          sources: [
            { name: 'gh', grid: grid() },
            {
              name: 'forgejo',
              grid: Object.fromEntries(CLASSES.map((klass) => [klass, defaultRow(TYPES)])),
            },
          ],
        }),
      },
    })
    await openConfig()
    choose('external', 'feature', 'execution')
    fireEvent.click(screen.getByRole('button', { name: 'forgejo' }))
    // The choice belongs to a cell, not to a screen position: switching the shown
    // source must not silently move it onto the source now on screen.
    expect(within(gridCell('external', 'feature', 'forgejo')).queryByText(T.not_written)).toBeNull()
    review()
    expect(shownPatch()).toEqual({
      sources: { gh: { autonomy: { external: { feature: 'execution' } } } },
    })
  })
})
