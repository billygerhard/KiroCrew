/**
 * The configuration pane leads with forms, and the raw document is one area over.
 *
 * The pane grew around its JSON editor: the document was the first thing an operator
 * met and, for most of what is stored, the only place to change it. This suite pins
 * the inversion, and every property here is a correctness claim rather than a
 * rendering preference:
 *
 *   - **The JSON is never SHOWN unbidden.** Not the editor, not the engine's
 *     problems and advisories for the persisted document, not a read-only preview.
 *     A pane that showed the document anyway would have moved the raw view rather
 *     than demoted it, and the whole point is which of the two an operator meets.
 *     Shown, not mounted: the pane's stage panels all stay mounted so switching
 *     stages cannot discard staged work, so what is asserted here is visibility.
 *   - **One area shows it, and showing it gives back the WHOLE editor.** The JSON
 *     view is the escape hatch for shapes no form expresses, so a read-only or
 *     partial version of it would re-create the dead ends this pane exists to
 *     remove. It is the advanced area rather than a pipeline stage because it edits
 *     the whole document and so is scoped to no one step.
 *   - **Leaving the area does not discard a draft, and the stage says it is holding
 *     one.** Half-written JSON is a legitimate state of an editor. If leaving the
 *     area dropped it silently, the stage tab would be a data-loss button.
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
 * minimality property both share. The stage structure itself — semantics, badges,
 * and staged-state survival across switches — is `SpecEngineConfigTabs.test.tsx`.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import SpecEnginePage from '../apps/spec-engine/SpecEnginePage'
import en from '../i18n/locales/en.json'

const T = en.apps.specEngine.configPanel
const P = en.apps.specEngine.specEnginePage

import {
  PIPELINE_STAGES,
  stubSpecEngineFetch,
  expectEverySpecEngineRouteAnswered,
  type Answer,
} from './specEngineFetchStub'

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
  stubSpecEngineFetch(
    {
      resolved: {
        body: {
          configured: true,
          project: null,
          source: null,
          settings: [],
          roles: { profile: '', roles: {} },
          role_order: [],
        },
      },
      // Answered with an empty vocabulary: this suite is about which surface
      // leads, and the generated form's own properties live in
      // `SpecEngineSettingsForm.test.tsx`.
      registry: {
        body: {
          settings: [],
          source_presets: [],
          profile_presets: [],
          roles: [],
          levels: [],
          stages: PIPELINE_STAGES,
        },
      },
      // No source at all: the grid's own properties are asserted in
      // `SpecEngineSources.test.tsx`, and a fixture here would be a second place
      // the payload shape is spelled.
      sources: {
        body: {
          sources: [],
          submitter_classes: ['maintainer', 'external'],
          spec_types: ['feature', 'bugfix'],
          levels: ['authoring', 'execution', 'delivery', 'integration'],
        },
      },
      config: ({ read, written }) =>
        (written ? answers.configAfterPut : undefined) ??
        (read > 1 ? answers.configAgain : undefined) ??
        answers.config ?? { body: snapshot(document()) },
      configWrite: answers.put ?? { body: { ok: true, document: {}, advisories: [] } },
    },
    { record: calls },
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
  // The stage list arrives from `/config/registry`, which settles after the config
  // read the projects table waits on. Awaited here so every helper below can read
  // the tabs synchronously instead of each racing the same read.
  await screen.findByRole('tablist', { name: T.configuration_stages })
  return { client, ...rendered }
}

/**
 * The stage tab named *label*.
 *
 * Matched as a prefix rather than exactly, because a tab's accessible name grows
 * the marks it is carrying — an unsaved draft, the engine's problem and advisory
 * counts — and that is the point of putting them there.
 */
function tab(label: string): HTMLElement {
  return screen.getByRole('tab', { name: new RegExp(`^${label}`) })
}

/**
 * The stage that holds the document editor.
 *
 * The advanced area, because the editor edits the WHOLE document and so is scoped
 * to no one step of the pipeline. Its marks ride here too — the unsaved draft and
 * the engine's problem and advisory counts — since this is the only area the
 * editor is rendered in.
 */
function toggle(): HTMLElement {
  return tab(T.stage_advanced)
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
  // Nothing the page asked for went unanswered by the shared stub. Without this a
  // product URL can drift out from under the table and this suite still passes: the
  // stub's 599 refusal reaches the surface as an ordinary error, so a test whose
  // subject is a read failure renders the copy it asserts for either way.
  expectEverySpecEngineRouteAnswered()
})

describe('the pane opens on the forms and not on the document', () => {
  it('shows no editor, and no document text, until the tab is chosen', async () => {
    stub({})
    await openConfig()
    // The forms are on screen.
    expect(screen.getByRole('grid', { name: T.configured_projects })).toBeInTheDocument()
    // And the document is not, in any form: nothing to type in, no save control, and
    // no rendering of its text either. `task_retry_limit` is a key the fixture
    // document holds, so SEEING it means the JSON reached the operator unbidden.
    //
    // Not-visible rather than not-present, and the difference is deliberate: the
    // pane keeps every stage panel mounted so switching stages cannot discard a
    // staged edit or a half-written draft. What is demoted is what an operator
    // meets, and that is exactly what visibility states.
    const name = T.the_configuration_document
    expect(screen.getByRole('textbox', { name, hidden: true })).not.toBeVisible()
    expect(
      screen.getByRole('button', { name: T.validate_and_save, hidden: true }),
    ).not.toBeVisible()
    expect(screen.getByText(/task_retry_limit/)).not.toBeVisible()
    // And nothing on the accessible surface offers either one, which is what a
    // reader navigating by role would find.
    expect(screen.queryByRole('textbox', { name: T.the_configuration_document })).toBeNull()
    expect(screen.queryByRole('button', { name: T.validate_and_save })).toBeNull()
  })

  it('withholds the engine\u2019s problems and advisories but states that they exist', async () => {
    // The counts are the one thing the JSON tab must carry: a form surface that
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
    expect(screen.getByText('must be at least 1')).not.toBeVisible()
    expect(screen.getByText('unattended_integration')).not.toBeVisible()
    // The counts themselves, on the tab, where they are readable without opening it.
    expect(toggle()).toHaveTextContent(new RegExp(`${T.problems}\\s*1`))
    expect(toggle()).toHaveTextContent(new RegExp(`${T.advisories}\\s*1`))
  })

  it('offers exactly one area that shows the view', async () => {
    stub({})
    await openConfig()
    // One area, not one per stage: the editor edits the whole document, so a second
    // copy on a pipeline stage would be a second draft of the same text.
    expect(screen.getAllByRole('tab', { name: new RegExp(`^${T.stage_advanced}`) })).toHaveLength(1)
    expect(screen.getAllByRole('heading', { name: T.tab_json_view, hidden: true })).toHaveLength(1)
    expect(toggle()).toHaveAttribute('aria-selected', 'false')
    // The sentence saying what the view is FOR travels with the view itself now, so
    // it is on the panel rather than beside a toggle.
    expect(screen.getByText(T.the_json_view_edits_what_no_form_expresses)).not.toBeVisible()
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

  it('leaves the area without discarding a draft, and says it is holding one', async () => {
    stub({})
    await openConfig()
    fireEvent.click(toggle())
    fireEvent.change(editor(), { target: { value: '{ "limits": { ' } })
    // Leaving the area is not a discard. State held inside the editor would be
    // dropped by an unmount, and the operator would have no way to know it had been.
    fireEvent.click(tab(T.stage_execution))
    expect(screen.queryByRole('textbox', { name: T.the_configuration_document })).toBeNull()
    // And the stage that holds it says so, so a pane showing no editor is not read as
    // a pane holding no draft.
    expect(toggle()).toHaveTextContent(T.unsaved_edits)
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
    // And no stages at all: a refusal is not a set of surfaces with one of them
    // showing, and the advanced area in particular would offer an editor over values
    // that are not there.
    expect(screen.queryByRole('tablist')).toBeNull()
    expect(screen.queryByRole('tab', { name: new RegExp(`^${T.stage_advanced}`) })).toBeNull()
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
