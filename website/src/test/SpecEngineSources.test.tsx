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
}) {
  let reads = 0
  vi.stubGlobal(
    'fetch',
    vi.fn((url: string) => {
      let answer: Answer
      if (url.startsWith('/api/apps/spec-engine/config/sources')) {
        reads += 1
        answer =
          (reads > 1 ? answers.sourcesAgain : undefined) ?? answers.sources ?? { body: sources() }
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

/** Render the page, switch to the configuration pane, and wait for the section. */
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
  await screen.findByRole('button', { name: C.validate_and_save })
  await screen.findByText(T.watch_sources)
  return { client, ...rendered }
}

/** The matrix for the selected source, by its accessible name. */
function matrix(source = 'gh'): HTMLElement {
  return screen.getByRole('table', {
    name: T.autonomy_for_source.replace('{{source}}', source),
  })
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

afterEach(() => {
  vi.unstubAllGlobals()
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
    expect(exact).toHaveTextContent('delivery')
    expect(exact).toHaveTextContent(T.origin_exact)
    expect(exact).toHaveTextContent('sources.gh.autonomy.maintainer.feature')

    const wildcard = gridCell('contributor', 'bugfix')
    expect(wildcard).toHaveTextContent('execution')
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
    expect(unset).toHaveTextContent('authoring')
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
    expect(gridCell('maintainer', 'feature')).toHaveTextContent('delivery')

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
