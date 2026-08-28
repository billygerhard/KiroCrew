/**
 * The configuration pane's pipeline-stage areas, and the two properties that make
 * splitting the surfaces safe.
 *
 * The pane was shaped like its own configuration document — a Settings tab, a Cost
 * profiles tab, a Watch sources tab, a JSON tab — so finding a control meant knowing
 * which container the engine keeps it in. It is shaped like the pipeline now: intake,
 * authoring, execution, delivery, plus a separately reachable advanced area, with the
 * projects table and the project selection above them governing all of them and the
 * resolved inspector column unchanged beside them. The placement of every setting
 * group and capability is the ENGINE's, projected by `/config/registry`; this suite
 * asserts the shell around that projection and never re-derives the placement.
 *
 * Splitting the surfaces is only safe because of two things, and neither is
 * presentation. Both were earned by the schema-shaped shell this one replaces, and
 * both are re-asserted here because the reason they exist did not change with the
 * vocabulary:
 *
 *   - **Switching costs nothing.** Every panel stays MOUNTED and hides with
 *     `hidden`, so a staged edit, a half-written add, an armed removal with its
 *     typed confirmation, and the JSON draft all survive a switch. Unmounting would
 *     drop each form's `useStagedEdits` state, and an operator who checked one
 *     number on another stage and came back to an emptied form would have lost work
 *     with nothing on screen saying so. That is `Property 1` below, over generated
 *     sequences of switches interleaved with staging, plus the named case a mutation
 *     probe pins.
 *   - **Hidden work is announced, and the badge reads ONE value.** Each stage
 *     carries the count of staged changes its own surfaces hold, whichever stage is
 *     active, so an operator can never confirm a patch while looking at a pane that
 *     shows no sign of the edits staged one stage over. The count on the tab is read
 *     from the same list the form's own "unwritten changes" line states and the patch
 *     builder consumes, so a badge can never claim a count the form does not show.
 *     Above them the pane states ONE total across every stage, which answers "is
 *     there any" for an operator standing on a third stage.
 *
 * The advanced area additionally carries the draft it is holding and the engine's
 * problem and advisory counts for the saved document, because those are only
 * RENDERED inside the editor: a pane that withheld "this document has two problems"
 * behind an unvisited area would read as a healthy configuration.
 *
 * Three more claims are here because they are about the structure rather than about
 * any one surface: the keyboard contract for the stage list including `Home` and
 * `End`, the source form's route to the document editor ACTIVATING the advanced area
 * rather than opening an editor beneath the form, and the autonomy grid sharing the
 * intake area with the form whose enable consequence links into it — a link that
 * crossed areas would hide the very thing it points at.
 *
 * The refusal and reading states render with no tablist at all, which is asserted in
 * `SpecEngineFormSurface.test.tsx` beside the rest of the pane's read-failure
 * behavior. Each form's own staging, review card and refusal retention are asserted
 * in that form's own suite; nothing here re-tests them. Which group and capability
 * the engine places in which stage is the backend's property, pinned in
 * `test_pipeline_stages.py`.
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

import { PIPELINE_STAGES, stubSpecEngineFetch, type Answer } from './specEngineFetchStub'

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
 *
 * `stages` is the engine's own projection, so the two settings land in the areas the
 * engine places them in rather than in areas this file chose: `limits` under
 * execution and `watch` under intake. That is what makes the locators below read
 * "the stage that holds the retry limit" rather than "the third tab".
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
    stages: PIPELINE_STAGES,
    ...over,
  }
}

/**
 * The stored document: one project, one cost profile, one working source.
 *
 * `legacy` polls a command no preset supplies, which is the case that routes to the
 * document editor — the cross-stage route this suite asserts.
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

/**
 * Whether the app-wide `config/resolved` read should refuse from now on.
 *
 * A flag the test flips rather than a read counter, because the pane mounts a
 * settings form per stage and they share one query key with no `staleTime`: each new
 * observer triggers its own refetch, so "the second read" lands during mount and a
 * counter would fail the read before the test had staged anything.
 */
let refuseResolved = false

function stub(answers: { registry?: Answer; config?: Answer } = {}) {
  stubSpecEngineFetch(
    {
      registry: answers.registry ?? { body: registry() },
      resolved: ({ params }) =>
        refuseResolved && (params.get('project') ?? '') === ''
          ? { status: 503, body: { code: 'config_unreadable', error: 'gone' } }
          : { body: resolved() },
      sources: { body: sources() },
      config: answers.config ?? { body: snapshot(stored()) },
    },
    { record: calls },
  )
}

/** The five stage labels, in the order the registry projects them. */
const ALL_STAGES = [
  C.stage_intake,
  C.stage_authoring,
  C.stage_execution,
  C.stage_delivery,
  C.stage_advanced,
]

/**
 * The stage that holds the retry-limit setting, which every staging case here uses.
 *
 * Named for what it HOLDS rather than by position, because the placement is the
 * engine's: `limits` is projected under execution, and a locator spelled as "the
 * third tab" would silently start reading another area if that projection moved.
 */
const SETTINGS_STAGE = C.stage_execution

/** The stage that holds the watch-source form and the autonomy grid it links into. */
const SOURCES_STAGE = C.stage_intake

/**
 * The stage that holds the cost-profile form and the document editor.
 *
 * The advanced area for both, and for the same reason in each case: a cost profile
 * assigns a model per ROLE across authoring and execution both, and the editor edits
 * the WHOLE document. Neither is scoped to one step of the pipeline.
 */
const DOCUMENT_STAGE = C.stage_advanced

/**
 * Render the page and switch to the configuration pane, on its default stage.
 *
 * *holding* is the stage whose settings rows are waited on, defaulting to the one
 * that holds the retry limit. A projection that places `limits` elsewhere — which the
 * unknown-stage case below deliberately supplies — names its own.
 */
async function openConfig(
  answers: Parameters<typeof stub>[0] = {},
  holding: string = SETTINGS_STAGE,
) {
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
  await screen.findByRole('tablist', { name: C.configuration_stages })
  // The settings rows are generated from the registry read, so waiting on one row is
  // waiting for the panels' first real render to have happened.
  await waitFor(() => expect(panelOf(holding).querySelector('.se-setting')).not.toBeNull())
  return client
}

/**
 * The stage tab named *label*.
 *
 * Matched as a prefix rather than exactly, because a tab's accessible name grows the
 * marks it is carrying — the count of staged changes, an unsaved draft, the engine's
 * problem and advisory counts — and carrying them is the point.
 */
function tab(label: string): HTMLElement {
  return screen.getByRole('tab', { name: new RegExp(`^${label}`) })
}

/** Activate the stage named *label*. */
function show(label: string): void {
  fireEvent.click(tab(label))
}

/** The panel a stage tab controls, whether or not it is the active one. */
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
  const path = within(panelOf(SETTINGS_STAGE)).getByText('limits.task_retry_limit', {
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
  const found = panelOf(SOURCES_STAGE).querySelector(`.se-setting[data-source-field="${field}"]`)
  expect(found, field).not.toBeNull()
  return found as HTMLElement
}

afterEach(() => {
  vi.unstubAllGlobals()
  calls.length = 0
  refuseResolved = false
})

describe('the pane presents the pipeline stages with the shared context above them', () => {
  it('names exactly the projected stages, in order, with the first one active', async () => {
    await openConfig()
    const list = screen.getByRole('tablist', { name: C.configuration_stages })
    const tabs = within(list).getAllByRole('tab')
    // In the order the ENGINE projected them, and the advanced area last because the
    // projection puts it last — this pane appends it only when the projection omits
    // it, and never reorders what it was handed.
    expect(tabs.map((element) => element.textContent)).toEqual(ALL_STAGES)
    expect(tabs.map((element) => element.getAttribute('aria-selected'))).toEqual([
      'true',
      'false',
      'false',
      'false',
      'false',
    ])
  })

  it('states in one sentence what each stage governs, before any of its controls', async () => {
    await openConfig()
    // A heading reading `Execution` tells an operator nothing about whether a poll
    // interval belongs to it, so the sentence is not decoration — and it comes first,
    // ahead of every control on the panel.
    const summaries: Array<[string, string]> = [
      [C.stage_intake, C.stage_intake_summary],
      [C.stage_authoring, C.stage_authoring_summary],
      [C.stage_execution, C.stage_execution_summary],
      [C.stage_delivery, C.stage_delivery_summary],
      [C.stage_advanced, C.stage_advanced_summary],
    ]
    for (const [label, summary] of summaries) {
      const panel = panelOf(label)
      const heading = within(panel).getByRole('heading', { name: label, hidden: true })
      const sentence = within(panel).getByText(summary)
      // Both inside the panel, and the sentence ahead of anything else in it: the
      // heading and its sentence are the panel's first block.
      expect(heading.closest('.se-blk')).toBe(sentence.closest('.se-blk'))
      expect(panel.firstElementChild).toBe(heading.closest('.se-blk'))
    }
  })

  it('keeps the projects table and the resolved column outside the stages', async () => {
    await openConfig()
    const table = screen.getByRole('grid', { name: C.configured_projects })
    // The row selected here governs every stage, so it cannot live inside one: a
    // selection on one panel is a selection the others could not see.
    expect(table.closest('[role="tabpanel"]')).toBeNull()
    const inspector = screen.getByRole('region', { name: P.resolved_configuration })
    expect(inspector.closest('[role="tabpanel"]')).toBeNull()
    // And the selection still reaches the resolved read across a stage switch.
    const row = within(table)
      .getAllByRole('row')
      .find((element) => element.textContent?.includes('acme'))
    expect(row).not.toBeUndefined()
    fireEvent.click(row as HTMLElement)
    show(DOCUMENT_STAGE)
    expect(
      await within(inspector).findByText(C.resolved_for_project.replace('{{project}}', 'acme')),
    ).toBeInTheDocument()
  })

  it('says which project every stage resolves for, above them all', async () => {
    await openConfig()
    // One selection for the whole pane, so the sentence naming what it resolves for
    // has to sit outside the stages too: stated once, not per area.
    const appWide = screen.getByText(C.every_stage_resolves_app_wide)
    expect(appWide.closest('[role="tabpanel"]')).toBeNull()
    const table = screen.getByRole('grid', { name: C.configured_projects })
    const row = within(table)
      .getAllByRole('row')
      .find((element) => element.textContent?.includes('acme'))
    fireEvent.click(row as HTMLElement)
    const named = await screen.findByText(
      C.every_stage_resolves_for_project.replace('{{project}}', 'acme'),
    )
    expect(named.closest('[role="tabpanel"]')).toBeNull()
    expect(screen.queryByText(C.every_stage_resolves_app_wide)).toBeNull()
  })

  it('pairs every stage with the panel it controls, and hides the rest', async () => {
    await openConfig()
    for (const label of ALL_STAGES) {
      const control = tab(label)
      const panel = panelOf(label)
      // The two halves of the tab-list contract: the tab says what it controls and
      // the panel says what labels it, so a reader on the panel knows which tab it
      // came from.
      expect(panel).toHaveAttribute('aria-labelledby', control.id)
      expect(panel).toHaveAttribute('role', 'tabpanel')
      expect(control.id).not.toBe('')
    }
    expect(panelOf(ALL_STAGES[0])).not.toHaveAttribute('hidden')
    for (const label of ALL_STAGES.slice(1)) {
      expect(panelOf(label)).toHaveAttribute('hidden')
    }
  })

  it('shows exactly one surface at a time', async () => {
    await openConfig()
    const sourceForm = () =>
      screen.getByRole('heading', { name: SF.watch_source_definitions, hidden: true })
    const profiles = () => screen.getByRole('heading', { name: PR.cost_profiles, hidden: true })
    expect(sourceForm()).toBeVisible()
    expect(profiles()).not.toBeVisible()
    show(DOCUMENT_STAGE)
    expect(sourceForm()).not.toBeVisible()
    expect(profiles()).toBeVisible()
  })

  it('moves the selection with the arrow keys, wrapping at both ends', async () => {
    await openConfig()
    const first = tab(ALL_STAGES[0])
    first.focus()
    fireEvent.keyDown(first, { key: 'ArrowRight' })
    expect(tab(ALL_STAGES[1])).toHaveAttribute('aria-selected', 'true')
    // Focus follows the selection, so the arrows move the reader too rather than
    // leaving them on a tab that is no longer the selected one.
    expect(tab(ALL_STAGES[1])).toHaveFocus()
    fireEvent.keyDown(tab(ALL_STAGES[1]), { key: 'ArrowLeft' })
    expect(tab(ALL_STAGES[0])).toHaveAttribute('aria-selected', 'true')
    // The ends of the list are not walls: an arrow press that did nothing would read
    // as a dead key.
    fireEvent.keyDown(tab(ALL_STAGES[0]), { key: 'ArrowLeft' })
    expect(tab(ALL_STAGES[4])).toHaveAttribute('aria-selected', 'true')
    fireEvent.keyDown(tab(ALL_STAGES[4]), { key: 'ArrowRight' })
    expect(tab(ALL_STAGES[0])).toHaveAttribute('aria-selected', 'true')
  })

  it('jumps to the first and last stage with Home and End', async () => {
    await openConfig()
    // Five areas is enough that walking the list one arrow at a time is a chore, and
    // the tabs pattern answers it with these two keys. The previous shell handled
    // only the arrows; rebuilding it is the moment to stop carrying that omission.
    show(C.stage_execution)
    const middle = tab(C.stage_execution)
    middle.focus()
    fireEvent.keyDown(middle, { key: 'End' })
    expect(tab(ALL_STAGES[4])).toHaveAttribute('aria-selected', 'true')
    expect(tab(ALL_STAGES[4])).toHaveFocus()
    fireEvent.keyDown(tab(ALL_STAGES[4]), { key: 'Home' })
    expect(tab(ALL_STAGES[0])).toHaveAttribute('aria-selected', 'true')
    expect(tab(ALL_STAGES[0])).toHaveFocus()
    // And neither key wrapped past its own end, which an off-by-one in the shared
    // move helper would do silently.
    fireEvent.keyDown(tab(ALL_STAGES[0]), { key: 'Home' })
    expect(tab(ALL_STAGES[0])).toHaveAttribute('aria-selected', 'true')
    show(ALL_STAGES[4])
    fireEvent.keyDown(tab(ALL_STAGES[4]), { key: 'End' })
    expect(tab(ALL_STAGES[4])).toHaveAttribute('aria-selected', 'true')
  })

  it('offers one tab stop for the whole list', async () => {
    await openConfig()
    // Five stops between the projects table and the panel would make the tab row a
    // detour rather than a switch; the arrows are how a reader moves within it.
    expect(tab(ALL_STAGES[0])).toHaveAttribute('tabindex', '0')
    for (const label of ALL_STAGES.slice(1)) {
      expect(tab(label)).toHaveAttribute('tabindex', '-1')
    }
  })

  it('draws nothing over the surfaces it switches between', async () => {
    await openConfig()
    // The pane's standing rule: no overlay, no popup, nothing positioned over a
    // surface that carries a kill switch or a consequence statement.
    const list = screen.getByRole('tablist', { name: C.configuration_stages })
    for (const element of [list, ...within(list).getAllByRole('tab')]) {
      const position = getComputedStyle(element).position
      expect(['', 'static'], element.tagName).toContain(position)
    }
  })
})

describe('a stage states the unwritten work its own surfaces hold', () => {
  it('counts a staged settings edit on its own stage, from any stage', async () => {
    await openConfig()
    fireEvent.change(retryRow(), { target: { value: '9' } })
    await waitFor(() => expect(tab(SETTINGS_STAGE)).toHaveTextContent(/1/))
    // The number the form's own unwritten-changes line states, read off the tab.
    // Badge and patch read ONE value: this is the list `buildFormPatch` consumes,
    // so a badge cannot claim a count the form does not show.
    expect(
      within(panelOf(SETTINGS_STAGE)).getByText(new RegExp(S.unwritten_setting_changes)),
    ).toBeInTheDocument()
    // And it stays legible from another stage, which is the whole reason it is there:
    // an operator must never confirm a patch while looking at a pane showing no sign
    // of the edits staged one stage over.
    show(DOCUMENT_STAGE)
    expect(tab(SETTINGS_STAGE)).toHaveTextContent(/1/)
  })

  it('states one pane-level count across every stage while any holds work', async () => {
    await openConfig()
    // Nothing staged, nothing claimed: a standing "0 unwritten changes" line would be
    // noise on the pane an operator opens most.
    expect(screen.queryByText(new RegExp(C.unwritten_changes_across_every_stage))).toBeNull()
    fireEvent.change(retryRow(), { target: { value: '9' } })
    const total = await screen.findByText(new RegExp(C.unwritten_changes_across_every_stage))
    expect(total).toHaveTextContent(/1/)
    // Above the stages and outside them: the per-stage badges answer "where is it",
    // and this answers "is there any" for an operator standing on a third stage who
    // has no other way to ask.
    expect(total.closest('[role="tabpanel"]')).toBeNull()
    // A second surface's staging adds to the same total rather than replacing it.
    show(SOURCES_STAGE)
    fireEvent.click(within(sourceField('enabled')).getByRole('checkbox'))
    await waitFor(() =>
      expect(
        screen.getByText(new RegExp(C.unwritten_changes_across_every_stage)),
      ).toHaveTextContent(/2/),
    )
  })

  it('counts a staged profile edit on its own stage, and nowhere else', async () => {
    await openConfig()
    show(DOCUMENT_STAGE)
    const effort = within(
      within(panelOf(DOCUMENT_STAGE)).getByRole('group', {
        name: PR.effort_for_role.replace('{{role}}', 'design'),
      }),
    ).getByRole('button', { name: 'high' })
    fireEvent.click(effort)
    await waitFor(() => expect(tab(DOCUMENT_STAGE)).toHaveTextContent(/1/))
    // The forms stage separately, so one form's count must not appear on another
    // stage: a badge is an observation of one stage's surfaces, not a pane-wide
    // total — that total is stated once, above the stages.
    expect(tab(SETTINGS_STAGE).textContent).toBe(SETTINGS_STAGE)
    expect(tab(SOURCES_STAGE).textContent).toBe(SOURCES_STAGE)
  })

  it('counts a staged source edit on the intake stage', async () => {
    await openConfig()
    show(SOURCES_STAGE)
    fireEvent.click(within(sourceField('enabled')).getByRole('checkbox'))
    await waitFor(() => expect(tab(SOURCES_STAGE)).toHaveTextContent(/1/))
    expect(tab(SETTINGS_STAGE).textContent).toBe(SETTINGS_STAGE)
  })

  it('carries the draft it is holding and the engine\u2019s counts on the advanced stage', async () => {
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
    // The counts are on the tab before the area is ever activated: they are only
    // RENDERED inside the editor, so a pane that withheld them behind an unvisited
    // area would read as a healthy configuration.
    expect(tab(DOCUMENT_STAGE)).toHaveTextContent(new RegExp(`${C.problems}\\s*2`))
    expect(tab(DOCUMENT_STAGE)).toHaveTextContent(new RegExp(`${C.advisories}\\s*1`))
    expect(tab(DOCUMENT_STAGE)).not.toHaveTextContent(C.unsaved_edits)
    show(DOCUMENT_STAGE)
    const editor = screen.getByRole('textbox', { name: C.the_configuration_document })
    fireEvent.change(editor, { target: { value: '{ "limits": { ' } })
    // A draft the operator has not saved survives leaving the area, so the tab has to
    // SAY it is holding one; otherwise the pane looks clean and the draft reappears
    // later with no explanation.
    show(SETTINGS_STAGE)
    expect(tab(DOCUMENT_STAGE)).toHaveTextContent(C.unsaved_edits)
  })

  it('keeps reporting a stage\u2019s count while that stage\u2019s read is refused', async () => {
    // The reason `PendingCount` is called from each form's error and pending guards
    // as well as its main return. A form whose read fails stops rendering its rows,
    // but the edits it staged are still there and still headed for a patch: a badge
    // that dropped to zero on a failed refetch would report unwritten work as gone,
    // and the operator's next act would be to walk away from it.
    const client = await openConfig()
    fireEvent.change(retryRow(), { target: { value: '9' } })
    await waitFor(() => expect(tab(SETTINGS_STAGE)).toHaveTextContent(/1/))
    refuseResolved = true
    await client.invalidateQueries({ queryKey: ['spec-engine', 'config', 'resolved'] })
    show(SETTINGS_STAGE)
    // The refusal is stated, so this is the failed-read state and not a stale render.
    await within(panelOf(SETTINGS_STAGE)).findByText(C.could_not_resolve_the_configuration)
    // And the count survived it, on the tab and in the pane-level total both.
    expect(tab(SETTINGS_STAGE)).toHaveTextContent(/1/)
    expect(
      screen.getByText(new RegExp(C.unwritten_changes_across_every_stage)),
    ).toHaveTextContent(/1/)
  })

  it('drops a stage\u2019s count when its surface discards the staged changes', async () => {
    await openConfig()
    fireEvent.change(retryRow(), { target: { value: '9' } })
    await waitFor(() => expect(tab(SETTINGS_STAGE)).toHaveTextContent(/1/))
    // A badge that outlived the edits it counted would send an operator looking for
    // work that is not there. Activated first because the review and discard controls
    // are read by role, and a hidden panel is out of the accessibility tree — which is
    // the same property the staging cases above rely on from the other direction.
    show(SETTINGS_STAGE)
    fireEvent.click(
      within(panelOf(SETTINGS_STAGE)).getByRole('button', { name: S.review_the_exact_change }),
    )
    fireEvent.click(
      within(panelOf(SETTINGS_STAGE)).getByRole('button', {
        name: S.discard_the_pending_changes,
      }),
    )
    await waitFor(() => expect(tab(SETTINGS_STAGE).textContent).toBe(SETTINGS_STAGE))
    // And the pane-level total goes with it rather than outliving the badge it sums.
    expect(screen.queryByText(new RegExp(C.unwritten_changes_across_every_stage))).toBeNull()
  })
})

describe('the surfaces that link to each other are reachable from where they link', () => {
  it('activates the advanced stage when the source form routes an inexpressible source', async () => {
    await openConfig()
    show(SOURCES_STAGE)
    const panel = panelOf(SOURCES_STAGE)
    // Scoped to the form's own picker: the grid below it lists the same source names,
    // which is what the shared selection exists to keep in agreement.
    const picker = within(panel).getByRole('group', { name: SF.select_a_watch_source_to_edit })
    fireEvent.click(within(picker).getByRole('button', { name: 'legacy' }))
    // The form's own route: a source whose poll no preset supplies gets no partial
    // form, it gets the escape hatch — and the escape hatch is a stage away, so the
    // route has to move the pane rather than opening a second editor under the form.
    fireEvent.click(
      within(panel).getByRole('button', {
        name: SF.edit_this_source_in_the_json_view.replace('{{source}}', 'legacy'),
      }),
    )
    expect(tab(DOCUMENT_STAGE)).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByRole('textbox', { name: C.the_configuration_document })).toBeVisible()
  })

  it('keeps the autonomy grid on the same stage as the form that links into it', async () => {
    await openConfig()
    show(SOURCES_STAGE)
    const panel = panelOf(SOURCES_STAGE)
    // Both surfaces on one panel: the form's enable consequence links to the matrix
    // showing how far that source's items may run unattended, and a link that
    // crossed areas would hide the very thing it points at. The engine places both
    // the `watch` setting group and the `watch_sources` capability in intake, which
    // is why this is the area they share.
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

describe('switching stages never loses staged state', () => {
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
    show(SOURCES_STAGE)
    const sourcePanel = panelOf(SOURCES_STAGE)
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
    fireEvent.change(confirmName, { target: { value: 'g' } })
    // A half-written JSON draft.
    show(DOCUMENT_STAGE)
    fireEvent.change(screen.getByRole('textbox', { name: C.the_configuration_document }), {
      target: { value: '{ "limits": { ' },
    })
    // Walk every stage, then come back and read all four states again.
    for (const label of ALL_STAGES) show(label)
    show(SETTINGS_STAGE)
    expect(retryRow().value).toBe('9')
    expect(tab(SETTINGS_STAGE)).toHaveTextContent(/1/)
    show(SOURCES_STAGE)
    expect(
      (
        within(panelOf(SOURCES_STAGE)).getByRole('textbox', {
          name: SF.name_for_the_new_source,
        }) as HTMLInputElement
      ).value,
    ).toBe('half-typed')
    expect(
      (
        within(panelOf(SOURCES_STAGE)).getByRole('textbox', {
          name: SF.type_the_name_to_confirm.replace('{{source}}', 'gh'),
        }) as HTMLInputElement
      ).value,
    ).toBe('g')
    show(DOCUMENT_STAGE)
    expect(
      (screen.getByRole('textbox', { name: C.the_configuration_document }) as HTMLTextAreaElement)
        .value,
    ).toBe('{ "limits": { ')
    // And nothing was written along the way: a switch is not a save.
    expect(calls.some((call) => call.method === 'PUT')).toBe(false)
  })

  /**
   * Property 1: for all sequences of stage switches interleaved with staging actions,
   * every staged edit, typed draft and typed confirmation present before a switch is
   * present after it, and each stage's badge equals what its own surfaces report.
   *
   * A generator rather than a fixture because the interesting sequences are the ones
   * nobody would write down: staging on one stage, switching twice, staging on a
   * second, switching back through a third. `numRuns` is small and the shrinker is
   * off the critical path here — each run mounts the whole pane, so this is a
   * component-level property and its cost is real.
   */
  it('holds over generated sequences of switches interleaved with staging', async () => {
    const LABEL = fc.constantFrom(...ALL_STAGES)
    /**
     * Stage a settings edit, type a draft, type a removal confirmation, or switch.
     *
     * Three kinds of unwritten state and not one, because they are held in three
     * different places and only one of them is `useStagedEdits`: the draft lives
     * above the panels, the confirmation is a form's own component state, and the
     * settings edit is the staged-edit hook. A property over the hook alone would
     * pass on a shell that dropped the other two.
     */
    const STEP = fc.oneof(
      fc.record({ kind: fc.constant('retry' as const), value: fc.integer({ min: 1, max: 99 }) }),
      fc.record({ kind: fc.constant('draft' as const), value: fc.integer({ min: 1, max: 9 }) }),
      fc.record({ kind: fc.constant('confirm' as const), value: fc.constant(0) }),
      fc.record({ kind: fc.constant('switch' as const), value: LABEL }),
    )
    /** The removal-confirmation box, once the removal is armed. */
    const confirmBox = () =>
      within(panelOf(SOURCES_STAGE)).queryByRole('textbox', {
        name: SF.type_the_name_to_confirm.replace('{{source}}', 'gh'),
        hidden: true,
      }) as HTMLInputElement | null
    await fc.assert(
      fc.asyncProperty(fc.array(STEP, { minLength: 1, maxLength: 8 }), async (steps) => {
        await openConfig()
        let retry: string | null = null
        let draft: string | null = null
        let confirm: string | null = null
        for (const step of steps) {
          if (step.kind === 'retry') {
            show(SETTINGS_STAGE)
            retry = String(step.value)
            fireEvent.change(retryRow(), { target: { value: retry } })
          } else if (step.kind === 'draft') {
            show(DOCUMENT_STAGE)
            draft = `{ "limits": { ${'x'.repeat(step.value)}`
            fireEvent.change(screen.getByRole('textbox', { name: C.the_configuration_document }), {
              target: { value: draft },
            })
          } else if (step.kind === 'confirm') {
            show(SOURCES_STAGE)
            if (!confirmBox()) {
              fireEvent.click(
                within(panelOf(SOURCES_STAGE)).getByRole('button', {
                  name: SF.remove_the_source.replace('{{source}}', 'gh'),
                }),
              )
            }
            confirm = 'gh'.slice(0, 1 + (confirm?.length ?? 0))
            fireEvent.change(confirmBox() as HTMLInputElement, { target: { value: confirm } })
          } else {
            show(step.value)
          }
          // Read all three states back after EVERY step, whichever stage is showing:
          // the claim is that no switch loses anything, not that the end state
          // survives.
          if (retry !== null) expect(retryRow().value).toBe(retry)
          if (draft !== null) {
            expect(
              (
                within(panelOf(DOCUMENT_STAGE)).getByRole('textbox', {
                  name: C.the_configuration_document,
                  hidden: true,
                }) as HTMLTextAreaElement
              ).value,
            ).toBe(draft)
          }
          if (confirm !== null) expect(confirmBox()?.value).toBe(confirm)
        }
        // Every panel is still mounted, on whichever stage the sequence ended.
        for (const label of ALL_STAGES) {
          expect(panelOf(label)).toBeInTheDocument()
        }
        // And each badge reports what its own surfaces say, rather than a total.
        const staged = retry !== null && retry !== '7' ? 1 : 0
        await waitFor(() =>
          expect(tab(SETTINGS_STAGE).textContent).toBe(
            staged > 0 ? `${SETTINGS_STAGE}${staged}` : SETTINGS_STAGE,
          ),
        )
        expect(tab(DOCUMENT_STAGE).textContent?.includes(C.unsaved_edits)).toBe(draft !== null)
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

describe('an unknown stage is folded rather than dropped', () => {
  it('routes a stage this pane has no words for into the advanced area', async () => {
    // The engine can grow a stage before this pane has words for one. The write door
    // still enforces every setting in it, so dropping the stage would leave a setting
    // in force on every run and reachable from nowhere.
    await openConfig(
      {
        registry: {
          body: registry({
            stages: [
              { id: 'intake', setting_groups: ['watch'], capabilities: ['watch_sources'] },
              { id: 'provisioning', setting_groups: ['limits'], capabilities: ['brand_new'] },
            ],
          }),
        },
      },
      C.stage_advanced,
    )
    const tabs = within(
      screen.getByRole('tablist', { name: C.configuration_stages }),
    ).getAllByRole('tab')
    // No tab labelled with the raw engine identifier, and no tab lost either.
    expect(tabs.map((element) => element.textContent)).toEqual([
      C.stage_intake,
      C.stage_advanced,
    ])
    const advanced = panelOf(C.stage_advanced)
    // The unknown stage's setting group is rendered in the advanced area, along with
    // the capability it declared.
    expect(within(advanced).getByText('limits.task_retry_limit')).toBeInTheDocument()
    expect(within(advanced).getByText('brand_new')).toBeInTheDocument()
  })
})

describe('the operator-facing strings', () => {
  it('ship the stage labels, summaries and badge copy in all thirteen catalogs', async () => {
    const added = [
      'configuration_stages',
      'problems',
      'stage_intake',
      'stage_intake_summary',
      'stage_authoring',
      'stage_authoring_summary',
      'stage_execution',
      'stage_execution_summary',
      'stage_delivery',
      'stage_delivery_summary',
      'stage_advanced',
      'stage_advanced_summary',
      'unwritten_changes_across_every_stage',
      'every_stage_resolves_app_wide',
      'every_stage_resolves_for_project',
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
        // A string left in English is a string nobody translated. Every key here is
        // words rather than a format name, so none of them is legitimately identical.
        if (locale !== 'en') expect(value, `${locale}.${key}`).not.toBe(C[key])
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

  it('keeps the stage labels free of interpolation', () => {
    // A stage label is a fixed name, so a placeholder in one would render as
    // `{{name}}` on screen with nothing to fill it. The summaries are exempt: they
    // are sentences, not names, and none of them interpolates today either.
    const keys = [
      'stage_intake',
      'stage_authoring',
      'stage_execution',
      'stage_delivery',
      'stage_advanced',
    ] as const
    for (const key of keys) {
      expect(C[key], key).not.toContain('{{')
    }
  })
})
