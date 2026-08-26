/**
 * The configuration pane's four section tabs, and the two properties that make
 * splitting the surfaces safe.
 *
 * The pane used to stack five dense editing surfaces in one scroll, so an operator
 * looking for one control read past everything else to reach it. They are tabs now:
 * Settings, Cost profiles, Watch sources, JSON view — with the projects table and
 * the project selection above them, governing all four, and the resolved inspector
 * column unchanged beside them.
 *
 * Splitting them is only safe because of two things, and neither is presentation:
 *
 *   - **Switching costs nothing.** Every panel stays MOUNTED and hides with
 *     `hidden`, so a staged edit, a half-written add, an armed removal with its
 *     typed confirmation, and the JSON draft all survive a switch. Unmounting would
 *     drop each form's `useStagedEdits` state, and an operator who checked one
 *     number on another tab and came back to an emptied form would have lost work
 *     with nothing on screen saying so. That is `Property 1` below, over generated
 *     sequences of switches interleaved with staging, plus the named case a mutation
 *     probe pins.
 *   - **Hidden work is announced.** Each tab carries the count of staged changes its
 *     own surfaces hold, whichever tab is active, so an operator can never confirm a
 *     patch while looking at a pane that shows no sign of the edits staged one tab
 *     over. The JSON tab carries the draft it is holding and the engine's problem
 *     and advisory counts for the saved document, which is what the demoted toggle
 *     carried before it.
 *
 * Two more claims are here because they are about the structure rather than about
 * any one surface: the source form's route to the JSON view now ACTIVATES that tab
 * rather than opening an editor beneath the form, and the autonomy grid shares the
 * Watch sources tab with the form whose enable consequence links into it — a link
 * that crossed tabs would hide the very thing it points at.
 *
 * The refusal and reading states render with no tablist at all, which is asserted in
 * `SpecEngineFormSurface.test.tsx` beside the rest of the pane's read-failure
 * behavior. Each form's own staging, review card and refusal retention are asserted
 * in that form's own suite; nothing here re-tests them.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import * as fc from 'fast-check'

import SpecEnginePage from '../apps/spec-engine/SpecEnginePage'
import en from '../i18n/locales/en.json'
import bn from '../i18n/locales/bn.json'
import de from '../i18n/locales/de.json'
import es from '../i18n/locales/es.json'
import fr from '../i18n/locales/fr.json'
import hi from '../i18n/locales/hi.json'
import itIT from '../i18n/locales/it.json'
import ja from '../i18n/locales/ja.json'
import ko from '../i18n/locales/ko.json'
import pt from '../i18n/locales/pt.json'
import ru from '../i18n/locales/ru.json'
import zh from '../i18n/locales/zh-CN.json'

const C = en.apps.specEngine.configPanel
const S = en.apps.specEngine.settingsForm
const PR = en.apps.specEngine.profilesForm
const SF = en.apps.specEngine.sourceForm
const G = en.apps.specEngine.sourcesSection
const P = en.apps.specEngine.specEnginePage

type Answer = { status?: number; body: unknown }

/** Every request the page made, so an assertion can read the body that was sent. */
const calls: Array<{ url: string; method: string; body: unknown }> = []

/** The bundled GitHub preset's argv, as `WATCH_SOURCE_PRESETS` holds it. */
const GH_POLL = [
  'gh',
  'api',
  'repos/OWNER/REPO/issues?state=all&per_page=100',
  '--jq',
  'map(select(.pull_request == null))',
]

const GH_MAP = {
  identifier: 'number',
  title: 'title',
  body: 'body',
  state: 'state',
  address: 'html_url',
  classification: 'labels.0.name',
  submitter: 'user.login',
  association: 'author_association',
}

const REPO = 'acme/widgets'
const GH_POLL_NAMED = GH_POLL.map((argument) => argument.replace('OWNER/REPO', REPO))
const GH_ENTRY = { preset: 'github', public: true, poll: GH_POLL, field_map: GH_MAP }

/**
 * The registry payload, in `_registry_payload`'s shape.
 *
 * One app-scoped setting the settings form can stage at, one role and one effort so
 * the profiles form has an assignment to change, and one bundled source preset so
 * the source form has something to copy. Small on purpose: what is under test is
 * the structure the three forms sit in, not any form's generation.
 */
function registry(over: Record<string, unknown> = {}) {
  return {
    settings: [
      {
        key: 'limits.task_retry_limit',
        kind: 'int',
        default: 2,
        minimum: 1,
        maximum: null,
        scopes: ['app', 'project'],
        summary: 'Times a failed task is retried before the run stops.',
      },
      {
        key: 'watch.interval_s',
        kind: 'int',
        default: 300,
        minimum: 30,
        maximum: null,
        scopes: ['app', 'source'],
        summary: 'Seconds between watch-source poll ticks.',
      },
    ],
    source_presets: [{ host: 'github', program: 'gh', entry: GH_ENTRY }],
    profile_presets: [],
    profile_settings: [],
    roles: ['design'],
    efforts: ['low', 'high'],
    levels: ['authoring', 'execution'],
    ...over,
  }
}

/**
 * The stored document: one project, one cost profile, one working source.
 *
 * `legacy` polls a command no preset supplies, which is the case that routes to the
 * JSON view — the cross-tab route this suite asserts.
 */
function stored() {
  return {
    projects: { acme: { path: '/src/acme', cost_profile: 'thrifty' } },
    cost_profiles: { thrifty: { roles: { design: { model: 'auto', effort: 'low' } } } },
    sources: {
      gh: {
        preset: 'github',
        public: true,
        poll: [...GH_POLL_NAMED],
        field_map: { ...GH_MAP },
        project: 'acme',
      },
      legacy: { poll: ['curl', '-s', 'https://tracker.example/api'], field_map: { title: 'name' } },
    },
    limits: { task_retry_limit: 7 },
  } as Record<string, unknown>
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

/** The resolved read, so the settings rows have something in force to show. */
function resolved() {
  return {
    configured: true,
    project: null,
    source: null,
    settings: [
      {
        key: 'limits.task_retry_limit',
        value: 7,
        origin: 'app_config',
        declared_at: 'limits.task_retry_limit',
        is_default: false,
      },
      {
        key: 'watch.interval_s',
        value: 300,
        origin: 'bundled_default',
        declared_at: '',
        is_default: true,
      },
    ],
    roles: { profile: 'thrifty', roles: {} },
    role_order: ['design'],
  }
}

/** The grid read, which the autonomy section renders from. */
function sources() {
  return {
    sources: [
      { name: 'gh', grid: {} },
      { name: 'legacy', grid: {} },
    ],
    submitter_classes: ['maintainer', 'external'],
    spec_types: ['feature'],
    levels: ['authoring', 'execution'],
  }
}

function stub(answers: { registry?: Answer; config?: Answer } = {}) {
  vi.stubGlobal(
    'fetch',
    vi.fn((url: string, init?: RequestInit) => {
      const method = init?.method ?? 'GET'
      calls.push({ url, method, body: init?.body ? JSON.parse(String(init.body)) : undefined })
      let answer: Answer
      if (method === 'PUT') {
        answer = { body: { ok: true, document: {}, advisories: [] } }
      } else if (url.startsWith('/api/apps/spec-engine/config/registry')) {
        // Answered BEFORE the generic '/config' prefix below, which would otherwise
        // hand this read a ConfigSnapshot and crash the forms' render.
        answer = answers.registry ?? { body: registry() }
      } else if (url.startsWith('/api/apps/spec-engine/config/resolved')) {
        answer = { body: resolved() }
      } else if (url.startsWith('/api/apps/spec-engine/config/sources')) {
        answer = { body: sources() }
      } else if (url.startsWith('/api/apps/spec-engine/config')) {
        answer = answers.config ?? { body: snapshot(stored()) }
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

/** The four tab labels, in the order the pane renders them. */
const ALL_TABS = [C.tab_settings, C.tab_cost_profiles, C.tab_watch_sources, C.tab_json_view]

/** Render the page and switch to the configuration pane, on its default tab. */
async function openConfig(answers: Parameters<typeof stub>[0] = {}) {
  stub(answers)
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchInterval: false } },
  })
  render(
    <QueryClientProvider client={client}>
      <SpecEnginePage />
    </QueryClientProvider>,
  )
  fireEvent.click(await screen.findByRole('button', { name: new RegExp(P.configuration) }))
  await screen.findByRole('tablist', { name: C.configuration_sections })
  // The settings rows are generated from the registry read, so waiting on one row is
  // waiting for the panels' first real render to have happened.
  await waitFor(() =>
    expect(panelOf(C.tab_settings).querySelector('.se-setting')).not.toBeNull(),
  )
  return client
}

/**
 * The tab named *label*.
 *
 * Matched as a prefix rather than exactly, because a tab's accessible name grows the
 * marks it is carrying — the count of staged changes, an unsaved draft, the engine's
 * problem and advisory counts — and carrying them is the point.
 */
function tab(label: string): HTMLElement {
  return screen.getByRole('tab', { name: new RegExp(`^${label}`) })
}

/** Activate the tab named *label*. */
function show(label: string): void {
  fireEvent.click(tab(label))
}

/** The panel a tab controls, whether or not it is the active one. */
function panelOf(label: string): HTMLElement {
  const id = tab(label).getAttribute('aria-controls')
  expect(id).not.toBeNull()
  const found = document.getElementById(String(id))
  expect(found).not.toBeNull()
  return found as HTMLElement
}

/**
 * The settings control for the retry limit, which every staging case here uses.
 *
 * Found through the DOM rather than by accessible role, because a hidden panel is
 * out of the accessibility tree the role queries read — and reading a control on a
 * hidden panel is exactly what these cases are for.
 */
function retryRow(): HTMLInputElement {
  const path = within(panelOf(C.tab_settings)).getByText('limits.task_retry_limit', {
    selector: '.se-kv-path, .se-m',
  })
  const row = path.closest('.se-setting')
  expect(row).not.toBeNull()
  const inputs = (row as HTMLElement).querySelectorAll('input')
  expect(inputs).toHaveLength(1)
  return inputs[0] as HTMLInputElement
}

/** One of the source form's field rows, by the field it writes. */
function sourceField(field: string): HTMLElement {
  const found = panelOf(C.tab_watch_sources).querySelector(
    `.se-setting[data-source-field="${field}"]`,
  )
  expect(found, field).not.toBeNull()
  return found as HTMLElement
}

afterEach(() => {
  vi.unstubAllGlobals()
  calls.length = 0
})

describe('the pane presents four section tabs with the shared context above them', () => {
  it('names exactly the four surfaces, in order, with Settings active', async () => {
    await openConfig()
    const list = screen.getByRole('tablist', { name: C.configuration_sections })
    const tabs = within(list).getAllByRole('tab')
    expect(tabs.map((element) => element.textContent)).toEqual(ALL_TABS)
    // Settings leads: it is the most-visited surface, and the JSON view stays
    // demoted at the far end.
    expect(tabs.map((element) => element.getAttribute('aria-selected'))).toEqual([
      'true',
      'false',
      'false',
      'false',
    ])
  })

  it('keeps the projects table and the resolved column outside the tabs', async () => {
    await openConfig()
    const table = screen.getByRole('grid', { name: C.configured_projects })
    // The row selected here governs every tab, so it cannot live inside one: a
    // selection on one panel is a selection the other three could not see.
    expect(table.closest('[role="tabpanel"]')).toBeNull()
    const inspector = screen.getByRole('region', { name: P.resolved_configuration })
    expect(inspector.closest('[role="tabpanel"]')).toBeNull()
    // And the selection still reaches the resolved read across a tab switch.
    const row = within(table)
      .getAllByRole('row')
      .find((element) => element.textContent?.includes('acme'))
    expect(row).not.toBeUndefined()
    fireEvent.click(row as HTMLElement)
    show(C.tab_cost_profiles)
    expect(
      await within(inspector).findByText(C.resolved_for_project.replace('{{project}}', 'acme')),
    ).toBeInTheDocument()
  })

  it('pairs every tab with the panel it controls, and hides the rest', async () => {
    await openConfig()
    for (const label of ALL_TABS) {
      const control = tab(label)
      const panel = panelOf(label)
      // The two halves of the tab-list contract: the tab says what it controls and
      // the panel says what labels it, so a reader on the panel knows which tab it
      // came from.
      expect(panel).toHaveAttribute('aria-labelledby', control.id)
      expect(panel).toHaveAttribute('role', 'tabpanel')
      expect(control.id).not.toBe('')
    }
    expect(panelOf(C.tab_settings)).not.toHaveAttribute('hidden')
    for (const label of [C.tab_cost_profiles, C.tab_watch_sources, C.tab_json_view]) {
      expect(panelOf(label)).toHaveAttribute('hidden')
    }
  })

  it('shows exactly one surface at a time', async () => {
    await openConfig()
    expect(
      screen.getByRole('heading', { name: S.settings, hidden: true }),
    ).toBeVisible()
    expect(screen.getByRole('heading', { name: PR.cost_profiles, hidden: true })).not.toBeVisible()
    show(C.tab_cost_profiles)
    expect(screen.getByRole('heading', { name: S.settings, hidden: true })).not.toBeVisible()
    expect(screen.getByRole('heading', { name: PR.cost_profiles, hidden: true })).toBeVisible()
  })

  it('moves the selection with the arrow keys, wrapping at both ends', async () => {
    await openConfig()
    const first = tab(C.tab_settings)
    first.focus()
    fireEvent.keyDown(first, { key: 'ArrowRight' })
    expect(tab(C.tab_cost_profiles)).toHaveAttribute('aria-selected', 'true')
    // Focus follows the selection, so the arrows move the reader too rather than
    // leaving them on a tab that is no longer the selected one.
    expect(tab(C.tab_cost_profiles)).toHaveFocus()
    fireEvent.keyDown(tab(C.tab_cost_profiles), { key: 'ArrowLeft' })
    expect(tab(C.tab_settings)).toHaveAttribute('aria-selected', 'true')
    // The ends of the list are not walls: an arrow press that did nothing would read
    // as a dead key.
    fireEvent.keyDown(tab(C.tab_settings), { key: 'ArrowLeft' })
    expect(tab(C.tab_json_view)).toHaveAttribute('aria-selected', 'true')
    fireEvent.keyDown(tab(C.tab_json_view), { key: 'ArrowRight' })
    expect(tab(C.tab_settings)).toHaveAttribute('aria-selected', 'true')
  })

  it('offers one tab stop for the whole list', async () => {
    await openConfig()
    // Four stops between the projects table and the panel would make the tab row a
    // detour rather than a switch; the arrows are how a reader moves within it.
    expect(tab(C.tab_settings)).toHaveAttribute('tabindex', '0')
    for (const label of [C.tab_cost_profiles, C.tab_watch_sources, C.tab_json_view]) {
      expect(tab(label)).toHaveAttribute('tabindex', '-1')
    }
  })

  it('draws nothing over the surfaces it switches between', async () => {
    await openConfig()
    // The pane's standing rule: no overlay, no popup, nothing positioned over a
    // surface that carries a kill switch or a consequence statement.
    const list = screen.getByRole('tablist', { name: C.configuration_sections })
    for (const element of [list, ...within(list).getAllByRole('tab')]) {
      const position = getComputedStyle(element).position
      expect(['', 'static'], element.tagName).toContain(position)
    }
  })
})

describe('a tab states the unwritten work its own surfaces hold', () => {
  it('counts a staged settings edit on the Settings tab, from any tab', async () => {
    await openConfig()
    fireEvent.change(retryRow(), { target: { value: '9' } })
    await waitFor(() => expect(tab(C.tab_settings)).toHaveTextContent(/1/))
    // The number the form's own unwritten-changes line states, read off the tab.
    expect(
      within(panelOf(C.tab_settings)).getByText(new RegExp(S.unwritten_setting_changes)),
    ).toBeInTheDocument()
    // And it stays legible from another tab, which is the whole reason it is there:
    // an operator must never confirm a patch while looking at a pane showing no sign
    // of the edits staged one tab over.
    show(C.tab_json_view)
    expect(tab(C.tab_settings)).toHaveTextContent(/1/)
  })

  it('counts a staged profile edit on the Cost profiles tab, and nowhere else', async () => {
    await openConfig()
    show(C.tab_cost_profiles)
    const effort = within(
      within(panelOf(C.tab_cost_profiles)).getByRole('group', {
        name: PR.effort_for_role.replace('{{role}}', 'design'),
      }),
    ).getByRole('button', { name: 'high' })
    fireEvent.click(effort)
    await waitFor(() => expect(tab(C.tab_cost_profiles)).toHaveTextContent(/1/))
    // The three forms stage separately, so one form's count must not appear on
    // another form's tab: a badge is an observation of one surface, not a pane-wide
    // total.
    expect(tab(C.tab_settings).textContent).toBe(C.tab_settings)
    expect(tab(C.tab_watch_sources).textContent).toBe(C.tab_watch_sources)
  })

  it('counts a staged source edit on the Watch sources tab', async () => {
    await openConfig()
    show(C.tab_watch_sources)
    fireEvent.click(within(sourceField('enabled')).getByRole('checkbox'))
    await waitFor(() => expect(tab(C.tab_watch_sources)).toHaveTextContent(/1/))
    expect(tab(C.tab_settings).textContent).toBe(C.tab_settings)
  })

  it('carries the draft it is holding and the engine\u2019s counts on the JSON tab', async () => {
    await openConfig({
      config: {
        body: snapshot(stored(), {
          errors: [
            { path: 'limits.task_retry_limit', message: 'must be at least 1' },
            { path: 'sources.legacy.poll', message: 'not a known program' },
          ],
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
    // The counts are on the tab before it is ever activated: they are only RENDERED
    // inside the view, so a pane that withheld them behind an unvisited tab would
    // read as a healthy configuration.
    expect(tab(C.tab_json_view)).toHaveTextContent(new RegExp(`${C.problems}\\s*2`))
    expect(tab(C.tab_json_view)).toHaveTextContent(new RegExp(`${C.advisories}\\s*1`))
    expect(tab(C.tab_json_view)).not.toHaveTextContent(C.unsaved_edits)
    show(C.tab_json_view)
    const editor = screen.getByRole('textbox', { name: C.the_configuration_document })
    fireEvent.change(editor, { target: { value: '{ "limits": { ' } })
    // A draft the operator has not saved survives leaving the tab, so the tab has to
    // SAY it is holding one; otherwise the pane looks clean and the draft reappears
    // later with no explanation.
    show(C.tab_settings)
    expect(tab(C.tab_json_view)).toHaveTextContent(C.unsaved_edits)
  })

  it('drops a tab\u2019s count when its surface discards the staged changes', async () => {
    await openConfig()
    fireEvent.change(retryRow(), { target: { value: '9' } })
    await waitFor(() => expect(tab(C.tab_settings)).toHaveTextContent(/1/))
    // A badge that outlived the edits it counted would send an operator looking for
    // work that is not there.
    fireEvent.click(
      within(panelOf(C.tab_settings)).getByRole('button', { name: S.review_the_exact_change }),
    )
    fireEvent.click(
      within(panelOf(C.tab_settings)).getByRole('button', {
        name: S.discard_the_pending_changes,
      }),
    )
    await waitFor(() => expect(tab(C.tab_settings).textContent).toBe(C.tab_settings))
  })
})

describe('the surfaces that link to each other are reachable from where they link', () => {
  it('activates the JSON tab when the source form routes an inexpressible source', async () => {
    await openConfig()
    show(C.tab_watch_sources)
    const panel = panelOf(C.tab_watch_sources)
    // Scoped to the form's own picker: the grid below it lists the same source names,
    // which is what the shared selection exists to keep in agreement.
    const picker = within(panel).getByRole('group', { name: SF.select_a_watch_source_to_edit })
    fireEvent.click(within(picker).getByRole('button', { name: 'legacy' }))
    // The form's own route: a source whose poll no preset supplies gets no partial
    // form, it gets the escape hatch.
    fireEvent.click(
      within(panel).getByRole('button', {
        name: SF.edit_this_source_in_the_json_view.replace('{{source}}', 'legacy'),
      }),
    )
    expect(tab(C.tab_json_view)).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByRole('textbox', { name: C.the_configuration_document })).toBeVisible()
  })

  it('keeps the autonomy grid on the same tab as the form that links into it', async () => {
    await openConfig()
    show(C.tab_watch_sources)
    const panel = panelOf(C.tab_watch_sources)
    // Both surfaces on one panel: the form's enable consequence links to the matrix
    // showing how far that source's items may run unattended, and a link that
    // crossed tabs would hide the very thing it points at.
    expect(within(panel).getByRole('heading', { name: SF.watch_source_definitions })).toBeVisible()
    expect(within(panel).getByRole('heading', { name: G.watch_sources })).toBeVisible()
    const link = within(panel).getByRole('link', {
      name: SF.open_the_autonomy_grid_for_source.replace('{{source}}', 'gh'),
    })
    fireEvent.click(link)
    expect(
      within(panel).getByRole('table', {
        name: G.autonomy_for_source.replace('{{source}}', 'gh'),
      }),
    ).toBeVisible()
  })
})

describe('switching tabs never loses staged state', () => {
  /**
   * The named case a mutation probe pins.
   *
   * Planting a conditional render in place of the `hidden` attribute makes this
   * fail: every one of the four states below lives in a component inside a panel,
   * and an unmount takes all of them with it.
   */
  it('keeps staged edits, a typed removal, a half-written add and the draft', async () => {
    await openConfig()
    // One staged settings edit.
    fireEvent.change(retryRow(), { target: { value: '9' } })
    // A typed add name and an armed removal with its typed confirmation, both of
    // which are component state rather than staged edits — and both of which an
    // unmount would drop without a word.
    show(C.tab_watch_sources)
    const sourcePanel = panelOf(C.tab_watch_sources)
    const addName = within(sourcePanel).getByRole('textbox', { name: SF.name_for_the_new_source })
    fireEvent.change(addName, { target: { value: 'half-typed' } })
    fireEvent.click(
      within(sourcePanel).getByRole('button', {
        name: SF.remove_the_source.replace('{{source}}', 'gh'),
      }),
    )
    const confirmName = within(sourcePanel).getByRole('textbox', {
      name: SF.type_the_name_to_confirm.replace('{{source}}', 'gh'),
    })
    fireEvent.change(confirmName, { target: { value: 'g' } })    // A half-written JSON draft.
    show(C.tab_json_view)
    fireEvent.change(screen.getByRole('textbox', { name: C.the_configuration_document }), {
      target: { value: '{ "limits": { ' },
    })
    // Walk every tab, then come back and read all four states again.
    show(C.tab_settings)
    show(C.tab_cost_profiles)
    show(C.tab_watch_sources)
    show(C.tab_json_view)
    show(C.tab_settings)
    expect(retryRow().value).toBe('9')
    expect(tab(C.tab_settings)).toHaveTextContent(/1/)
    show(C.tab_watch_sources)
    expect(
      (
        within(panelOf(C.tab_watch_sources)).getByRole('textbox', {
          name: SF.name_for_the_new_source,
        }) as HTMLInputElement
      ).value,
    ).toBe('half-typed')
    expect(
      (
        within(panelOf(C.tab_watch_sources)).getByRole('textbox', {
          name: SF.type_the_name_to_confirm.replace('{{source}}', 'gh'),
        }) as HTMLInputElement
      ).value,
    ).toBe('g')
    show(C.tab_json_view)
    expect(
      (screen.getByRole('textbox', { name: C.the_configuration_document }) as HTMLTextAreaElement)
        .value,
    ).toBe('{ "limits": { ')
    // And nothing was written along the way: a switch is not a save.
    expect(calls.some((call) => call.method === 'PUT')).toBe(false)
  })

  /**
   * Property 1: for all sequences of tab switches interleaved with staging actions,
   * every staged edit and draft present before a switch is present after it, and
   * each tab's badge equals what its own surface reports.
   *
   * A generator rather than a fixture because the interesting sequences are the ones
   * nobody would write down: staging on one tab, switching twice, staging on a
   * second, switching back through a third. `numRuns` is small and the shrinker is
   * off the critical path here — each run mounts the whole pane, so this is a
   * component-level property and its cost is real.
   */
  it('holds over generated sequences of switches interleaved with staging', async () => {
    const LABEL = fc.constantFrom(...ALL_TABS)
    /** Stage on the settings tab, stage on the JSON tab, or just switch. */
    const STEP = fc.oneof(
      fc.record({ kind: fc.constant('retry' as const), value: fc.integer({ min: 1, max: 99 }) }),
      fc.record({ kind: fc.constant('draft' as const), value: fc.integer({ min: 1, max: 9 }) }),
      fc.record({ kind: fc.constant('switch' as const), value: LABEL }),
    )
    await fc.assert(
      fc.asyncProperty(fc.array(STEP, { minLength: 1, maxLength: 8 }), async (steps) => {
        await openConfig()
        let retry: string | null = null
        let draft: string | null = null
        for (const step of steps) {
          if (step.kind === 'retry') {
            show(C.tab_settings)
            retry = String(step.value)
            fireEvent.change(retryRow(), { target: { value: retry } })
          } else if (step.kind === 'draft') {
            show(C.tab_json_view)
            draft = `{ "limits": { ${'x'.repeat(step.value)}`
            fireEvent.change(
              screen.getByRole('textbox', { name: C.the_configuration_document }),
              { target: { value: draft } },
            )
          } else {
            show(step.value)
          }
          // Read both states back after EVERY step, whichever tab is showing: the
          // claim is that no switch loses anything, not that the end state survives.
          if (retry !== null) expect(retryRow().value).toBe(retry)
          if (draft !== null) {
            expect(
              (
                within(panelOf(C.tab_json_view)).getByRole('textbox', {
                  name: C.the_configuration_document,
                  hidden: true,
                }) as HTMLTextAreaElement
              ).value,
            ).toBe(draft)
          }
        }
        // Every panel is still mounted, on whichever tab the sequence ended.
        for (const label of ALL_TABS) {
          expect(panelOf(label)).toBeInTheDocument()
        }
        // And each badge reports what its own surface says, rather than a total.
        const staged = retry !== null && retry !== '7' ? 1 : 0
        await waitFor(() =>
          expect(tab(C.tab_settings).textContent).toBe(
            staged > 0 ? `${C.tab_settings}${staged}` : C.tab_settings,
          ),
        )
        expect(tab(C.tab_json_view).textContent?.includes(C.unsaved_edits)).toBe(draft !== null)
        // Torn down inside the property: each run mounts its own page, and a leaked
        // one would let the next run's queries find two panes.
        document.body.innerHTML = ''
        vi.unstubAllGlobals()
        calls.length = 0
      }),
      { numRuns: 12 },
    )
  })
})

describe('the operator-facing strings', () => {
  it('ship the tab labels and badge copy in all thirteen catalogs', async () => {
    const added = [
      'configuration_sections',
      'problems',
      'tab_cost_profiles',
      'tab_json_view',
      'tab_settings',
      'tab_watch_sources',
    ] as const
    const catalogs: Array<[string, Record<string, unknown>]> = [
      ['bn', bn.apps.specEngine.configPanel],
      ['de', de.apps.specEngine.configPanel],
      ['en', en.apps.specEngine.configPanel],
      ['es', es.apps.specEngine.configPanel],
      ['fr', fr.apps.specEngine.configPanel],
      ['hi', hi.apps.specEngine.configPanel],
      ['it', itIT.apps.specEngine.configPanel],
      ['ja', ja.apps.specEngine.configPanel],
      ['ko', ko.apps.specEngine.configPanel],
      ['pt', pt.apps.specEngine.configPanel],
      ['ru', ru.apps.specEngine.configPanel],
      ['zh-CN', zh.apps.specEngine.configPanel],
    ]
    for (const [locale, catalog] of catalogs) {
      for (const key of added) {
        const value = catalog[key]
        expect(typeof value, `${locale}.${key}`).toBe('string')
        expect((value as string).trim(), `${locale}.${key}`).not.toBe('')
      }
      // A label left in English is a label nobody translated, apart from the ones
      // that are a format name rather than words.
      if (locale !== 'en') {
        expect(catalog.configuration_sections, locale).not.toBe(C.configuration_sections)
        expect(catalog.problems, locale).not.toBe(C.problems)
      }
    }
    // The pseudolocale is generated from English, so it is checked for presence: a
    // key missing there means it was never regenerated.
    const pseudo = (await import('../i18n/locales/en-XA.json')).default as {
      apps: { specEngine: { configPanel: Record<string, unknown> } }
    }
    for (const key of added) {
      expect(typeof pseudo.apps.specEngine.configPanel[key], key).toBe('string')
    }
  })

  it('keeps the tab labels free of interpolation', () => {
    // A tab label is a fixed name, so a placeholder in one would render as
    // `{{name}}` on screen with nothing to fill it.
    const keys = ['tab_settings', 'tab_cost_profiles', 'tab_watch_sources', 'tab_json_view']
    for (const key of keys as Array<keyof typeof C>) {
      expect(C[key], key).not.toContain('{{')
    }
  })
})
