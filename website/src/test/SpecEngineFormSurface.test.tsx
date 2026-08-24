/**
 * The configuration pane leads with forms, and the raw document is on request.
 *
 * The pane grew around its JSON editor: the document was the first thing an operator
 * met and, for most of what is stored, the only place to change it. This suite pins
 * the inversion, and every property here is a correctness claim rather than a
 * rendering preference:
 *
 *   - **The JSON is never rendered unbidden.** Not the editor, not the engine's
 *     problems and advisories for the persisted document, not a read-only preview.
 *     A pane that showed the document anyway would have moved the raw view rather
 *     than demoted it, and the whole point is which of the two an operator meets.
 *   - **One explicit control opens it, and opening gives back the WHOLE editor.**
 *     The JSON view is the escape hatch for shapes no form expresses, so a
 *     read-only or partial version of it would re-create the dead ends this pane
 *     exists to remove.
 *   - **A closed view does not discard a draft, and says it is holding one.** Half-
 *     written JSON is a legitimate state of an editor. If closing the view dropped
 *     it silently, the control would be a data-loss button.
 *   - **Both surfaces refresh from a FRESH read after any write, in both
 *     directions.** They read one query, so a save in the JSON view re-renders the
 *     forms and a form write re-renders the document — neither adopts what it just
 *     sent, and the two can never disagree about what is stored.
 *   - **A failed read states the failure and renders no form.** React Query keeps
 *     the last successful answer across a failing refetch, so a form filled from a
 *     retained answer would present values nobody re-read as what is in force.
 *
 * The shared review card and the patch builder behind every form write are asserted
 * where they are exercised: `SpecEngineSources.test.tsx` drives the card end to end
 * through the autonomy grid, and `SpecEngineFormPatch.property.test.ts` holds the
 * minimality property both share.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import SpecEnginePage from '../apps/spec-engine/SpecEnginePage'
import en from '../i18n/locales/en.json'

const T = en.apps.specEngine.configPanel
const P = en.apps.specEngine.specEnginePage

type Answer = { status?: number; body: unknown }

/** Every request the page made, so an assertion can read the body that was sent. */
const calls: Array<{ url: string; method: string; body: unknown }> = []

/** A document with two project entries, so a removal leaves something behind. */
function document() {
  return {
    projects: { acme: { path: '/src/acme' }, widgets: { path: '/src/widgets' } },
    limits: { task_retry_limit: 7 },
  }
}

/** The same document with one entry gone, as the store answers after a removal. */
function withoutWidgets() {
  return { ...document(), projects: { acme: { path: '/src/acme' } } }
}

/** The config read's shape around a given document. */
function snapshot(doc: Record<string, unknown>, over: Record<string, unknown> = {}) {
  return {
    configured: true,
    path: '/home/me/.kiro/crew/apps/spec-engine/config.json',
    document: doc,
    elided: [],
    elided_marker: '<elided>',
    errors: [],
    advisories: [],
    config_only_paths: [],
    ...over,
  }
}

function stub(answers: {
  config?: Answer
  /** The config read once a PUT has landed, as the store would then answer it. */
  configAfterPut?: Answer
  /** The config read from the SECOND read onwards, for a failing refetch. */
  configAgain?: Answer
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
      } else if (url.startsWith('/api/apps/spec-engine/config/sources')) {
        // No source at all: the grid's own properties are asserted in
        // `SpecEngineSources.test.tsx`, and a fixture here would be a second place
        // the payload shape is spelled.
        answer = {
          body: {
            sources: [],
            submitter_classes: ['maintainer', 'external'],
            spec_types: ['feature', 'bugfix'],
            levels: ['authoring', 'execution', 'delivery', 'integration'],
          },
        }
      } else if (url.startsWith('/api/apps/spec-engine/config')) {
        reads += 1
        answer =
          (written ? answers.configAfterPut : undefined) ??
          (reads > 1 ? answers.configAgain : undefined) ??
          answers.config ?? { body: snapshot(document()) }
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
}

/** Render the page and switch to the configuration pane, waiting for the forms. */
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
  await screen.findByText(T.projects)
  return { client, ...rendered }
}

/** The control that opens the JSON view. */
function toggle(): HTMLElement {
  return screen.getByRole('button', { name: T.open_the_json_view })
}

/** The document editor's textarea. */
function editor(): HTMLTextAreaElement {
  return screen.getByRole('textbox', {
    name: T.the_configuration_document,
  }) as HTMLTextAreaElement
}

/** The project rows of the projects table, app-defaults row included. */
function rows(): HTMLElement[] {
  const table = screen.getByRole('grid', { name: T.configured_projects })
  return within(table)
    .getAllByRole('row')
    .filter((row) => !row.classList.contains('se-qhead'))
}

/** The patch the one PUT carried. */
function putPatch(): unknown {
  const put = calls.filter((call) => call.method === 'PUT')
  expect(put).toHaveLength(1)
  return (put[0].body as { patch: unknown }).patch
}

afterEach(() => {
  vi.unstubAllGlobals()
  calls.length = 0
})

describe('the pane opens on the forms and not on the document', () => {
  it('renders no editor, and no document text anywhere', async () => {
    stub({})
    await openConfig()
    // The forms are on screen.
    expect(screen.getByRole('grid', { name: T.configured_projects })).toBeInTheDocument()
    // And the document is not, in any form: no editor to type in, and no rendering
    // of its text either. `task_retry_limit` is a key the fixture document holds, so
    // finding it on screen means the JSON reached the page unbidden.
    expect(
      screen.queryByRole('textbox', { name: T.the_configuration_document }),
    ).toBeNull()
    expect(screen.queryByRole('button', { name: T.validate_and_save })).toBeNull()
    expect(screen.queryByText(/task_retry_limit/)).toBeNull()
  })

  it('withholds the engine\u2019s problems and advisories but states that they exist', async () => {
    // The counts are the one thing the toggle must carry: a form surface that
    // silently withheld "this document has a problem" would read as a healthy
    // configuration, which is the opposite of demoting a view.
    stub({
      config: {
        body: snapshot(document(), {
          errors: [{ path: 'limits.task_retry_limit', message: 'must be at least 1' }],
          advisories: [
            {
              code: 'unattended_integration',
              path: 'sources.gh.autonomy',
              message: 'integration runs with nothing verifying it',
              project: null,
              requires_acknowledgment: true,
            },
          ],
        }),
      },
    })
    await openConfig()
    expect(
      screen.queryByRole('heading', { name: T.problems_in_the_persisted_document }),
    ).toBeNull()
    expect(screen.queryByText('must be at least 1')).toBeNull()
    expect(screen.queryByText('unattended_integration')).toBeNull()
    expect(
      screen.getByText(new RegExp(T.the_json_view_lists_the_documents_problems)),
    ).toBeInTheDocument()
    expect(
      screen.getByText(new RegExp(T.the_json_view_lists_the_documents_advisories)),
    ).toBeInTheDocument()
  })

  it('offers exactly one control that opens the view', async () => {
    stub({})
    await openConfig()
    expect(screen.getAllByRole('button', { name: T.open_the_json_view })).toHaveLength(1)
    expect(toggle()).toHaveAttribute('aria-pressed', 'false')
    expect(screen.getByText(T.the_json_view_edits_what_no_form_expresses)).toBeInTheDocument()
  })
})

describe('the JSON view, once asked for', () => {
  it('gives back the whole editor, its problems and its advisories', async () => {
    stub({
      config: {
        body: snapshot(document(), {
          errors: [{ path: 'limits.task_retry_limit', message: 'must be at least 1' }],
          advisories: [
            {
              code: 'unattended_integration',
              path: 'sources.gh.autonomy',
              message: 'integration runs with nothing verifying it',
              project: null,
              requires_acknowledgment: true,
            },
          ],
          elided: ['sources.gh.token'],
        }),
      },
    })
    await openConfig()
    fireEvent.click(toggle())
    // The editor, holding the document the read returned.
    expect(editor().value).toContain('task_retry_limit')
    // Every control the pane offered when the editor WAS the pane.
    expect(screen.getByRole('button', { name: T.validate_and_save })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: T.revert_unsaved_edits })).toBeInTheDocument()
    // The engine's reading of the persisted document, which is the part a
    // read-only or partial view would have dropped.
    expect(
      screen.getByRole('heading', { name: T.problems_in_the_persisted_document }),
    ).toBeInTheDocument()
    expect(screen.getByText('must be at least 1')).toBeInTheDocument()
    expect(screen.getByText('unattended_integration')).toBeInTheDocument()
    expect(screen.getByText(T.acknowledgment_required)).toBeInTheDocument()
    // And the two rules about the write that nobody would guess.
    expect(screen.getByText(T.elided_values_are_never_written_back)).toBeInTheDocument()
    expect(screen.getByText(T.deletions_are_sent_as_explicit_nulls)).toBeInTheDocument()
    expect(screen.getByText(T.withheld_at)).toBeInTheDocument()
  })

  it('closes again without discarding a draft, and says it is holding one', async () => {
    stub({})
    await openConfig()
    fireEvent.click(toggle())
    fireEvent.change(editor(), { target: { value: '{ "limits": { ' } })
    // Closing is not a discard. State held inside the editor would be dropped by
    // the unmount, and the operator would have no way to know it had been.
    fireEvent.click(screen.getByRole('button', { name: T.close_the_json_view }))
    expect(screen.queryByRole('textbox', { name: T.the_configuration_document })).toBeNull()
    expect(screen.getByText(T.unsaved_edits)).toBeInTheDocument()
    fireEvent.click(toggle())
    expect(editor().value).toBe('{ "limits": { ')
    expect(calls.some((call) => call.method === 'PUT')).toBe(false)
  })
})

describe('a write re-reads, and both surfaces read the same answer', () => {
  it('re-renders the forms from a fresh read after a save in the JSON view', async () => {
    stub({
      config: { body: snapshot(document()) },
      configAfterPut: { body: snapshot(withoutWidgets()) },
    })
    await openConfig()
    expect(rows()).toHaveLength(3)
    fireEvent.click(toggle())
    fireEvent.change(editor(), {
      target: { value: `${JSON.stringify(withoutWidgets(), null, 2)}\n` },
    })
    fireEvent.click(screen.getByRole('button', { name: T.validate_and_save }))
    // The table is not told what was sent; it re-renders from what the store now
    // answers, which is the only way the two surfaces cannot disagree.
    await waitFor(() => expect(rows()).toHaveLength(2))
    expect(editor().value).toBe(`${JSON.stringify(withoutWidgets(), null, 2)}\n`)
  })

  it('re-renders the document from a fresh read after a write on the forms', async () => {
    stub({
      config: { body: snapshot(document()) },
      configAfterPut: { body: snapshot(withoutWidgets()) },
    })
    await openConfig()
    fireEvent.click(toggle())
    expect(editor().value).toContain('widgets')
    fireEvent.click(
      screen.getByRole('button', { name: T.remove_project.replace('{{project}}', 'widgets') }),
    )
    fireEvent.click(
      screen.getByRole('button', {
        name: T.confirm_the_removal.replace('{{project}}', 'widgets'),
      }),
    )
    await waitFor(() => expect(editor().value).not.toContain('widgets'))
    // The removal is the shared patch builder's deletion form: one null at exactly
    // the entry, which is what the store's merge deletes on.
    expect(putPatch()).toEqual({ projects: { widgets: null } })
  })
})

describe('a failed read is doubt, not an empty form', () => {
  it('states the refusal and renders no form at all', async () => {
    stub({ config: { status: 500, body: { code: 'config_unreadable', error: 'not parseable' } } })
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false, refetchInterval: false } },
    })
    render(
      <QueryClientProvider client={client}>
        <SpecEnginePage />
      </QueryClientProvider>,
    )
    fireEvent.click(await screen.findByRole('button', { name: new RegExp(P.configuration) }))
    expect(await screen.findByText(P.could_not_read_the_configuration)).toBeInTheDocument()
    expect(screen.queryByRole('grid', { name: T.configured_projects })).toBeNull()
    // And no door to a document nothing could read: the toggle would open an editor
    // over values that are not there.
    expect(screen.queryByRole('button', { name: T.open_the_json_view })).toBeNull()
  })

  it('drops a form it had already rendered when a refetch fails', async () => {
    // React Query keeps the last successful answer across a failing refetch. A
    // surface that reached for the data before the error would keep offering rows
    // and a removal control for entries nobody re-read.
    const { client } = await openConfigWith({
      config: { body: snapshot(document()) },
      configAgain: { status: 503, body: { code: 'config_unreadable', error: 'gone' } },
    })
    expect(rows()).toHaveLength(3)
    await client.invalidateQueries({ queryKey: ['spec-engine', 'config'] })
    await screen.findByText(P.could_not_read_the_configuration)
    expect(screen.queryByRole('grid', { name: T.configured_projects })).toBeNull()
    expect(screen.queryByText(/task_retry_limit/)).toBeNull()
  })
})

/** Render with a given set of answers, for the tests that need the query client. */
async function openConfigWith(answers: Parameters<typeof stub>[0]) {
  stub(answers)
  return openConfig()
}
