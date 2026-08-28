/**
 * The watch-source form: preset-only creation, honest editing, named removal.
 *
 * This is the one form on the pane whose subject is something the engine RUNS, so
 * every property below is a safety claim rather than a rendering preference:
 *
 *   - **The presets are the only way in.** The picker lists what the registry read
 *     supplies — host, the program its argv names, and the item fields its field map
 *     reads — and a copy stages that entry byte for byte.
 *   - **A copy arrives inert.** The bundled entries carry no `enabled` key and the
 *     staging keeps it absent, so creating a source polls nothing until a separate,
 *     separately confirmed write arms it.
 *   - **No control anywhere takes a command or an argument.** Not directly and not
 *     indirectly: the test exercises every control the form renders and asserts that
 *     every argv the patch carries is a bundled preset's own, up to the repository
 *     slot that preset itself left open.
 *   - **The repository is a value.** The presets ship an `OWNER/REPO` placeholder the
 *     project is expected to name, so the form names it — by staging the preset's own
 *     argv with only that slot filled, and by stating the placeholder while it is
 *     still there.
 *   - **A stored shape the form cannot express gets no form.** Not a partial one —
 *     the state says what stops it and routes to the JSON view, while still offering
 *     the removal, which writes no field at all.
 *   - **An enable states what it starts**, with the program it runs and a link into
 *     the autonomy grid that decides how far its items go.
 *   - **A removal is confirmed by typing the source's own name**, patches the
 *     store's own deletion spelling, and states that ingestion stops.
 *   - **A refusal retains stored state, and a success re-reads.** This form's own
 *     mutation owns the invalidation; the shared review card cannot do it for its
 *     callers.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import SpecEnginePage from '../apps/spec-engine/SpecEnginePage'
import bn from '../i18n/locales/bn.json'
import de from '../i18n/locales/de.json'
import en from '../i18n/locales/en.json'
import es from '../i18n/locales/es.json'
import fr from '../i18n/locales/fr.json'
import hi from '../i18n/locales/hi.json'
import itIT from '../i18n/locales/it.json'
import ja from '../i18n/locales/ja.json'
import ko from '../i18n/locales/ko.json'
import pt from '../i18n/locales/pt.json'
import ru from '../i18n/locales/ru.json'
import zh from '../i18n/locales/zh-CN.json'

const T = en.apps.specEngine.sourceForm
const C = en.apps.specEngine.configPanel
const G = en.apps.specEngine.sourcesSection
const P = en.apps.specEngine.specEnginePage
const L = C.setting_labels

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

const GL_POLL = ['glab', 'issue', 'list', '--repo', 'OWNER/REPO', '--output', 'json']

/**
 * The GitHub preset's argv with its repository named, which is what a source that
 * actually polls anything holds.
 *
 * The presets ship an `OWNER/REPO` literal and the engine has no variable
 * substitution in a poll, so a working source's argv is NEVER byte-equal to its
 * preset's — the form has to express this shape or it can only edit copies that
 * cannot run.
 */
const REPO = 'acme/widgets'
const GH_POLL_NAMED = GH_POLL.map((argument) => argument.replace('OWNER/REPO', REPO))

const GL_MAP = {
  identifier: 'iid',
  title: 'title',
  body: 'description',
  state: 'state',
  address: 'web_url',
  classification: 'labels.0',
  submitter: 'author.username',
}

/** The preset entries as `watch_source_presets` deep-copies them: no `enabled`. */
const GH_ENTRY = { preset: 'github', public: true, poll: GH_POLL, field_map: GH_MAP }
const GL_ENTRY = { preset: 'gitlab', public: true, poll: GL_POLL, field_map: GL_MAP }

/**
 * The registry payload, in `_registry_payload`'s shape.
 *
 * The two source-scoped settings are the engine's own, with its own bounds, so a row
 * asserted here is a row an operator would meet. `concurrency.wave_max_tasks` is
 * there to prove the form renders only what a SOURCE may hold.
 */
function registry(over: Record<string, unknown> = {}) {
  return {
    settings: [
      {
        key: 'watch.interval_s',
        kind: 'int',
        default: 300,
        minimum: 30,
        maximum: null,
        scopes: ['app', 'source'],
        summary: 'Seconds between watch-source poll ticks. Polling spends no model credits.',
      },
      {
        key: 'timeouts.poll_command_s',
        kind: 'int',
        default: 120,
        minimum: 1,
        maximum: null,
        scopes: ['app', 'source'],
        summary: "Wall clock a watch source's poll command may run before the tick is skipped.",
      },
      {
        key: 'concurrency.wave_max_tasks',
        kind: 'int',
        default: 3,
        minimum: 1,
        maximum: null,
        scopes: ['app', 'project'],
        summary: 'Leaf tasks the orchestrator dispatches in parallel within one wave.',
      },
    ],
    source_presets: [
      { host: 'github', program: 'gh', entry: GH_ENTRY },
      { host: 'gitlab', program: 'glab', entry: GL_ENTRY },
    ],
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
 * The stored document.
 *
 * `gh` sorts first in document order, so it is what the form selects with no
 * interaction: its poll is the bundled GitHub preset's with the repository named —
 * the shape every source that actually polls anything is in — and every key it
 * carries is one the form shows or the grid does. `fresh` is a preset copy whose
 * placeholder nobody has named yet, which must be said rather than shown as a
 * working command. `legacy` polls something no preset supplies, and `extra` carries a
 * field this form has no control for — the two not-expressible cases, which must
 * never render a partial form.
 */
function stored() {
  return {
    sources: {
      gh: {
        preset: 'github',
        public: true,
        poll: [...GH_POLL_NAMED],
        field_map: { ...GH_MAP },
        project: 'acme',
        maintainers: ['octocat'],
        watch: { interval_s: 600 },
        autonomy: { maintainer: { feature: 'execution' } },
      },
      fresh: { preset: 'github', public: false, poll: [...GH_POLL], field_map: { ...GH_MAP } },
      legacy: { poll: ['curl', '-s', 'https://tracker.example/api'], field_map: { title: 'name' } },
      extra: {
        preset: 'github',
        public: true,
        poll: [...GH_POLL_NAMED],
        field_map: { ...GH_MAP },
        spend_cap: { credits: 5 },
      },
    },
    projects: {
      acme: { path: '/src/acme' },
      widgets: { path: '/src/widgets' },
    },
  } as Record<string, Record<string, Record<string, unknown>>>
}

/** The config read's shape around a given document. */
function snapshot(doc: Record<string, unknown>) {
  return {
    configured: true,
    path: '/home/me/.kiro/crew/apps/spec-engine/config.json',
    document: doc,
    elided: [],
    elided_marker: '<elided>',
    errors: [],
    advisories: [],
    config_only_paths: [],
  }
}

/** The resolved read. Answered so the pane beside this form renders at all. */
function resolved() {
  return {
    configured: true,
    project: null,
    source: null,
    settings: [
      {
        key: 'watch.interval_s',
        value: 300,
        origin: 'bundled_default',
        declared_at: '',
        is_default: true,
      },
    ],
    roles: { profile: '', roles: {} },
    role_order: [],
  }
}

/** The grid read, which the section below this form renders from. */
function sources() {
  return {
    sources: [
      { name: 'gh', grid: {} },
      { name: 'fresh', grid: {} },
      { name: 'legacy', grid: {} },
      { name: 'extra', grid: {} },
    ],
    submitter_classes: ['maintainer', 'external'],
    spec_types: ['feature'],
    levels: ['authoring', 'execution'],
  }
}

function stub(answers: {
  registry?: Answer
  config?: Answer
  /** The config read once a PUT has landed, as the store would then answer it. */
  configAfterPut?: Answer
  put?: Answer
}) {
  stubSpecEngineFetch(
    {
      registry: answers.registry ?? { body: registry() },
      resolved: { body: resolved() },
      sources: { body: sources() },
      config: ({ written }) =>
        (written ? answers.configAfterPut : undefined) ??
        answers.config ?? { body: snapshot(stored()) },
      configWrite: answers.put ?? { body: { ok: true, document: {}, advisories: [] } },
    },
    { record: calls },
  )
}

/**
 * Render the page, switch to the configuration pane, and show the Watch sources tab.
 *
 * The pane's editing surfaces are tabs now, and only the active one is reachable:
 * an inactive panel carries `hidden`, which takes it out of the accessibility tree
 * the role queries read. The form under test lives on the Watch sources tab, so
 * every case here starts by activating it.
 */
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
  // The watch-source form lives in the intake area: the engine places both the
  // `watch` setting group and the `watch_sources` capability there.
  await screen.findByRole('tablist', { name: C.configuration_stages })
  // Present unless the vocabulary read was REFUSED, in which case the pane has no
  // stages to lay out and folds everything into the advanced area — the case the
  // refusal tests below are for.
  const intake = screen.queryByRole('tab', { name: new RegExp(`^${C.stage_intake}`) })
  if (intake) fireEvent.click(intake)
  await screen.findByRole('heading', { name: T.watch_source_definitions })
  return client
}

/** Open the pane and wait until the form's own rows are on screen. */
async function openForm(answers: Parameters<typeof stub>[0] = {}) {
  const client = await openConfig(answers)
  await waitFor(() => expect(block().querySelector('[data-source-field]')).not.toBeNull())
  return client
}

/**
 * The source-form block.
 *
 * Every query is scoped to it: the settings form above renders the same registry
 * labels and the same review-card words, and the grid section below renders the same
 * source names — that is the point of the pane — so an unscoped query could not tell
 * the surfaces apart.
 */
function block(): HTMLElement {
  const heading = screen.getByRole('heading', { name: T.watch_source_definitions })
  const found = heading.closest('.se-blk')
  expect(found).not.toBeNull()
  return found as HTMLElement
}

/** The grid section's block, which the form links into. */
function gridBlock(): HTMLElement {
  const heading = screen.getByRole('heading', { name: G.watch_sources })
  return heading.closest('.se-blk') as HTMLElement
}

/** One editable source field's row, addressed by the field it writes. */
function fieldRow(field: string): HTMLElement {
  const found = block().querySelector(`.se-setting[data-source-field="${field}"]`)
  expect(found, `no row for ${field}`).not.toBeNull()
  return found as HTMLElement
}

/** One per-source setting's row, addressed by the path it writes. */
function settingRow(path: string): HTMLElement {
  const found = within(block()).getByText(path, { selector: '.se-kv-path' })
  return found.closest('.se-setting') as HTMLElement
}

/** Select one source from the form's picker. */
function selectSource(name: string) {
  const picker = within(block()).getByRole('group', { name: T.select_a_watch_source_to_edit })
  fireEvent.click(within(picker).getByRole('button', { name }))
}

/** Name the source the add block would create. */
function nameTheAdd(name: string) {
  fireEvent.change(within(block()).getByLabelText(T.name_for_the_new_source), {
    target: { value: name },
  })
}

/** Name the repository the add block's copy would poll. */
function nameTheAddRepository(repository: string) {
  fireEvent.change(within(block()).getByLabelText(T.the_repository_for_the_new_source), {
    target: { value: repository },
  })
}

/** The repository parameter row of the selected source. */
function repositoryRow(): HTMLElement {
  const found = block().querySelector('.se-setting[data-source-parameter="repository"]')
  expect(found, 'no repository row').not.toBeNull()
  return found as HTMLElement
}

/** The button that copies one bundled preset. */
function presetButton(host: string): HTMLElement {
  const group = within(block()).getByRole('group', { name: T.choose_a_preset_to_copy })
  return within(group).getByRole('button', {
    name: T.copy_the_preset.replace('{{host}}', host),
  })
}

/** Open the review card, having staged at least one change. */
function review() {
  fireEvent.click(within(block()).getByRole('button', { name: T.review_the_exact_change }))
}

/** Confirm the review card, which is the only thing here that writes. */
function confirm() {
  fireEvent.click(within(block()).getByRole('button', { name: T.write_the_change }))
}

/** The patch the review card is showing, parsed from the card itself. */
function shownPatch(): unknown {
  const pre = block().querySelector('pre.se-gpatch')
  expect(pre).not.toBeNull()
  return JSON.parse((pre as HTMLElement).textContent ?? 'null')
}

/** The count of unwritten changes, or `''` when the form shows none. */
function unwritten(): string {
  const found = within(block()).queryByText(new RegExp(T.unwritten_source_changes))
  return found ? (found.textContent ?? '') : ''
}

/** The patch the one PUT carried. */
function putPatch(): unknown {
  const put = calls.filter((call) => call.method === 'PUT')
  expect(put).toHaveLength(1)
  return (put[0].body as { patch: unknown }).patch
}

/** Every `poll` and `field_map` a patch carries, at any depth, with its value. */
function argvEntries(node: unknown, found: Array<[string, unknown]> = []): Array<[string, unknown]> {
  if (node !== null && typeof node === 'object' && !Array.isArray(node)) {
    for (const [key, value] of Object.entries(node)) {
      if (key === 'poll' || key === 'field_map') found.push([key, value])
      argvEntries(value, found)
    }
  }
  return found
}

/**
 * Whether *value* is an argv a bundled preset supplied, up to its repository slot.
 *
 * The claim the form makes: the program, the argument count and every argument
 * outside the slot the preset itself left open are the preset's own. Checked here by
 * rebuilding the preset's argv with whatever the candidate holds in that slot, so a
 * changed flag or an added argument fails even when the repository looks plausible.
 */
function suppliedArgv(field: string, value: unknown): boolean {
  for (const entry of [GH_ENTRY, GL_ENTRY] as Array<Record<string, unknown>>) {
    const own = entry[field]
    if (JSON.stringify(own) === JSON.stringify(value)) return true
    if (field !== 'poll' || !Array.isArray(own) || !Array.isArray(value)) continue
    if (own.length !== value.length) continue
    const slots = own.flatMap((argument, index) =>
      typeof argument === 'string' && argument.includes('OWNER/REPO') && index > 0 ? [index] : [],
    )
    if (slots.length === 0) continue
    const named = String(value[slots[0]])
    const prefix = String(own[slots[0]]).split('OWNER/REPO')[0]
    const suffix = String(own[slots[0]]).split('OWNER/REPO')[1] ?? ''
    if (!named.startsWith(prefix) || !named.endsWith(suffix)) continue
    const repository = named.slice(prefix.length, named.length - suffix.length)
    const rebuilt = own.map((argument, index) =>
      slots.includes(index) ? String(argument).split('OWNER/REPO').join(repository) : argument,
    )
    if (JSON.stringify(rebuilt) === JSON.stringify(value)) return true
  }
  return false
}

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  calls.length = 0
})

describe('the bundled presets are the only way a source is added', () => {
  it('describes each preset by its host, its program and what it ingests', async () => {
    await openForm()
    const group = within(block()).getByRole('group', { name: T.choose_a_preset_to_copy })
    // One offer per preset the read supplied, in its order.
    expect(group.querySelectorAll('[data-preset]')).toHaveLength(2)
    const github = block().querySelector('[data-preset="github"]') as HTMLElement
    // The program is the preset's own, derived upstream from its argv, and the
    // fields are the ones its field map reads out of that program's output.
    expect(
      within(github).getByText(
        T.preset_ingests_items
          .replace('{{host}}', 'github')
          .replace('{{program}}', 'gh')
          .replace('{{count}}', '8')
          .replace('{{fields}}', Object.keys(GH_MAP).join(', ')),
      ),
    ).toBeInTheDocument()
    expect(
      within(block().querySelector('[data-preset="gitlab"]') as HTMLElement).getByText(
        new RegExp('glab'),
      ),
    ).toBeInTheDocument()
  })

  it('offers nothing to copy until the source is named', async () => {
    await openForm()
    expect(presetButton('github')).toBeDisabled()
    expect(within(block()).getByText(T.name_the_source_first)).toBeInTheDocument()
    nameTheAdd('issues')
    expect(presetButton('github')).toBeEnabled()
  })

  it('refuses a name the document already carries, rather than merging into it', async () => {
    await openForm()
    nameTheAdd('gh')
    expect(
      within(block()).getByText(T.the_name_is_already_a_source.replace('{{source}}', 'gh')),
    ).toBeInTheDocument()
    expect(presetButton('github')).toBeDisabled()
    // And nothing is staged even if the control is reached anyway.
    fireEvent.click(presetButton('github'))
    expect(unwritten()).toBe('')
  })

  it('states that an add is always a preset copy and arrives inert', async () => {
    await openForm()
    expect(within(block()).getByText(T.an_add_is_always_a_preset_copy)).toBeInTheDocument()
  })
})

describe('a composed source carries the preset\u2019s command and nothing else', () => {
  it('stages the preset entry byte-for-byte, with no enabled key', async () => {
    await openForm()
    nameTheAdd('issues')
    fireEvent.click(presetButton('github'))
    review()
    // The bundled entry exactly, and `enabled` ABSENT rather than false: polling is
    // what arms an unattended run, so a fresh copy must be inert.
    expect(shownPatch()).toEqual({ sources: { issues: GH_ENTRY } })
    const staged = (shownPatch() as { sources: { issues: Record<string, unknown> } }).sources.issues
    expect('enabled' in staged).toBe(false)
    expect(staged.poll).toEqual(GH_POLL)
  })

  it('names the bundled preset it copied and the program that copy runs', async () => {
    await openForm()
    nameTheAdd('issues')
    fireEvent.click(presetButton('gitlab'))
    review()
    expect(
      within(block()).getByText(
        T.edit_copies_the_bundled_preset
          .replace('{{source}}', 'issues')
          .replace('{{preset}}', 'gitlab')
          .replace('{{program}}', 'glab')
          .replace('{{path}}', 'sources.issues'),
      ),
    ).toBeInTheDocument()
    confirm()
    await waitFor(() => expect(calls.some((call) => call.method === 'PUT')).toBe(true))
    expect(putPatch()).toEqual({ sources: { issues: GL_ENTRY } })
  })

  it('moves a staged copy to the new name rather than dropping it', async () => {
    await openForm()
    nameTheAdd('issues')
    fireEvent.click(presetButton('github'))
    nameTheAdd('tickets')
    review()
    expect(shownPatch()).toEqual({ sources: { tickets: GH_ENTRY } })
  })

  it('withdraws a staged copy whose name the document has taken, and says so', async () => {
    // The document can gain that name from the JSON view, from another surface, or
    // on any refetch — and the copy would then MERGE into the source of that name
    // key by key rather than add one. Silently dropping it would read as an edit
    // that was never made, so the withdrawal is stated.
    const after = stored()
    after.sources.issues = {
      preset: 'gitlab',
      public: true,
      poll: [...GL_POLL],
      field_map: { ...GL_MAP },
    }
    await openForm({ configAfterPut: { body: snapshot(after) } })
    nameTheAdd('issues')
    fireEvent.click(presetButton('github'))
    expect(unwritten()).toContain(T.unwritten_source_changes)
    // Another surface's write lands — a project removal from the table above, which
    // invalidates the document without touching this form's staged edits — and the
    // refetched document now carries `issues`.
    fireEvent.click(
      screen.getByRole('button', { name: C.remove_project.replace('{{project}}', 'widgets') }),
    )
    fireEvent.click(
      screen.getByRole('button', {
        name: C.confirm_the_removal.replace('{{project}}', 'widgets'),
      }),
    )
    expect(
      await within(block()).findByText(
        T.the_staged_copy_was_withdrawn.replace('{{source}}', 'issues'),
      ),
    ).toBeInTheDocument()
    // Withdrawn, not merely unannounced: nothing is left staged to confirm.
    expect(unwritten()).toBe('')
  })
})

describe('no control on the form accepts a command or an argument', () => {
  it('shows the poll command, the field map and the public flag read-only', async () => {
    await openForm()
    // The preset provenance, the program, and whether anyone may submit — as facts
    // rather than controls.
    const facts = block().querySelector('dl.se-kv') as HTMLElement
    expect(within(facts).getByText(T.the_preset_host)).toBeInTheDocument()
    expect(within(facts).getByText('github')).toBeInTheDocument()
    expect(within(facts).getByText(T.the_program_it_runs)).toBeInTheDocument()
    expect(within(facts).getByText('gh')).toBeInTheDocument()
    expect(within(facts).getByText(T.the_source_is_public)).toBeInTheDocument()
    expect((facts.querySelector('[data-source-shown="public"]') as HTMLElement).textContent).toBe(
      'true',
    )
    // Public is what makes the grid's submitter classes load-bearing, and the enable
    // control that arms unattended ingestion sits on this same form, so the meaning
    // is stated beside it rather than left to the word.
    expect(within(block()).getByText(T.public_items_come_from_anyone)).toBeInTheDocument()
    // The argv itself, exactly as the engine will run it.
    const argv = block().querySelector('pre.se-json')
    expect(argv).not.toBeNull()
    expect(JSON.parse((argv as HTMLElement).textContent ?? 'null')).toEqual(GH_POLL_NAMED)
    // And the mapping, which is the other half of what the poll does.
    expect(within(block()).getByText(T.the_field_map)).toBeInTheDocument()
    expect(within(block()).getByText('labels.0.name')).toBeInTheDocument()
    expect(within(block()).getByText(T.the_form_cannot_change_the_command)).toBeInTheDocument()
  })

  it('states a source is not public rather than implying screening it does not do', async () => {
    await openForm()
    selectSource('fresh')
    const facts = block().querySelector('dl.se-kv') as HTMLElement
    expect((facts.querySelector('[data-source-shown="public"]') as HTMLElement).textContent).toBe(
      'false',
    )
    // The advisory belongs to a public source; a private one earns no warning it
    // would then be read as carrying.
    expect(within(block()).queryByText(T.public_items_come_from_anyone)).toBeNull()
  })

  it('stages no argv outside a preset\u2019s own however every control is exercised', async () => {
    await openForm()
    // Every control the form renders, driven with argv-looking text: whatever the
    // form can be made to stage, none of it may be an argv the presets did not
    // supply — the repository slot they left open included.
    nameTheAdd('gh api repos/evil/repo')
    for (const box of Array.from(block().querySelectorAll('input[type="text"]'))) {
      fireEvent.change(box, { target: { value: 'gh api repos/evil/repo --jq .' } })
    }
    for (const number of Array.from(block().querySelectorAll('input[type="number"]'))) {
      fireEvent.change(number, { target: { value: '90' } })
    }
    for (const check of Array.from(block().querySelectorAll('input[type="checkbox"]'))) {
      fireEvent.click(check)
    }
    for (const button of Array.from(block().querySelectorAll('button'))) {
      if (button.textContent === T.write_the_change) continue
      if (!button.hasAttribute('disabled')) fireEvent.click(button)
    }
    review()
    const patch = shownPatch()
    // The claim is not that no `poll` key appears — a preset copy carries the
    // preset's own argv, which is the point — but that every argv path in the patch
    // is a preset's OWN up to the repository slot the preset itself left open, and
    // that nothing here can put another one there. Not the values in general: a
    // maintainer account may contain any text at all.
    for (const [key, value] of argvEntries(patch)) {
      expect(suppliedArgv(key, value), `${key} was not supplied by a bundled preset`).toBe(true)
    }
    expect(Object.keys(patch as Record<string, unknown>)).toEqual(['sources'])
  })
})

describe('the repository is a value the preset left open', () => {
  it('names it inside the preset\u2019s own argv, changing nothing else', async () => {
    await openForm()
    const box = within(repositoryRow()).getByRole('textbox')
    // The stored repository is read back out of the argv, so the control cannot show
    // a repository the array does not carry.
    expect((box as HTMLInputElement).value).toBe(REPO)
    expect(within(repositoryRow()).getByText(T.the_repository_is_a_value_not_a_command)).toBeInTheDocument()
    fireEvent.change(box, { target: { value: '  other/repo  ' } })
    review()
    // The whole array, with only the designated slot substituted and trimmed: the
    // program, the flags and the query string are still the preset's.
    expect(shownPatch()).toEqual({
      sources: {
        gh: { poll: GH_POLL.map((argument) => argument.replace('OWNER/REPO', 'other/repo')) },
      },
    })
    expect(
      within(block()).getByText(
        T.edit_names_the_repository
          .replace('{{source}}', 'gh')
          .replace('{{preset}}', 'github')
          .replace('{{oldValue}}', REPO)
          .replace('{{newValue}}', 'other/repo')
          .replace('{{path}}', 'sources.gh.poll'),
      ),
    ).toBeInTheDocument()
  })

  it('withdraws a repository typed back to the one already stored', async () => {
    await openForm()
    const box = within(repositoryRow()).getByRole('textbox')
    fireEvent.change(box, { target: { value: 'other/repo' } })
    expect(unwritten()).toContain(T.unwritten_source_changes)
    // Writing back what the path already holds is not a change, and every write is
    // recorded: staging it would put a line in the durable record for nothing.
    fireEvent.change(box, { target: { value: REPO } })
    expect(unwritten()).toBe('')
  })

  it('says the placeholder is still there rather than showing it as a command', async () => {
    await openForm()
    selectSource('fresh')
    // Empty rather than pre-filled with the literal: the literal is not a repository,
    // and a box holding it would invite an edit around it.
    expect((within(repositoryRow()).getByRole('textbox') as HTMLInputElement).value).toBe('')
    expect(
      within(block()).getByText(
        T.the_repository_is_still_the_placeholder.replace('{{placeholder}}', 'OWNER/REPO'),
      ),
    ).toBeInTheDocument()
  })

  it('states that enabling a placeholder poll ingests nothing', async () => {
    await openForm()
    selectSource('fresh')
    fireEvent.click(within(fieldRow('enabled')).getByRole('checkbox'))
    review()
    // A `true` in the patch does not say that the command it arms cannot run, so the
    // card says it — and does not claim polling begins on a repository.
    expect(
      within(block()).getByText(
        T.enabling_polls_the_placeholder
          .replace('{{source}}', 'fresh')
          .replace('{{program}}', 'gh')
          .replace('{{placeholder}}', 'OWNER/REPO')
          .replace('{{path}}', 'sources.fresh.enabled'),
      ),
    ).toBeInTheDocument()
    expect(
      within(block()).queryByText(
        T.enabling_begins_polling
          .replace('{{source}}', 'fresh')
          .replace('{{program}}', 'gh')
          .replace('{{path}}', 'sources.fresh.enabled'),
      ),
    ).toBeNull()
  })

  it('names a repository and an enable in one review, each stated', async () => {
    await openForm()
    selectSource('fresh')
    fireEvent.change(within(repositoryRow()).getByRole('textbox'), {
      target: { value: 'acme/other' },
    })
    fireEvent.click(within(fieldRow('enabled')).getByRole('checkbox'))
    review()
    // The repository lands in the same patch, so the polling sentence must describe
    // the command as it will stand — not the placeholder it no longer holds.
    expect(
      within(block()).getByText(
        T.enabling_begins_polling
          .replace('{{source}}', 'fresh')
          .replace('{{program}}', 'gh')
          .replace('{{path}}', 'sources.fresh.enabled'),
      ),
    ).toBeInTheDocument()
    expect(shownPatch()).toEqual({
      sources: {
        fresh: {
          poll: GH_POLL.map((argument) => argument.replace('OWNER/REPO', 'acme/other')),
          enabled: true,
        },
      },
    })
  })

  it('creates a working source from the add block without opening the JSON view', async () => {
    await openForm()
    nameTheAdd('issues')
    nameTheAddRepository('acme/tickets')
    fireEvent.click(presetButton('github'))
    review()
    // One edit, and its card names BOTH decisions: which preset the entry copies and
    // which repository its command names. A silent merge of the two would be a card
    // that claimed a provenance and hid the target.
    expect(shownPatch()).toEqual({
      sources: {
        issues: {
          ...GH_ENTRY,
          poll: GH_POLL.map((argument) => argument.replace('OWNER/REPO', 'acme/tickets')),
        },
      },
    })
    expect(
      within(block()).getByText(
        T.edit_copies_the_preset_for_repository
          .replace('{{source}}', 'issues')
          .replace('{{preset}}', 'github')
          .replace('{{program}}', 'gh')
          .replace('{{repository}}', 'acme/tickets')
          .replace('{{path}}', 'sources.issues'),
      ),
    ).toBeInTheDocument()
    confirm()
    await waitFor(() => expect(calls.some((call) => call.method === 'PUT')).toBe(true))
    const written = (putPatch() as { sources: { issues: Record<string, unknown> } }).sources.issues
    // Still inert: naming a repository is not arming a source.
    expect('enabled' in written).toBe(false)
  })

  it('re-composes a staged copy when the repository is named after the preset', async () => {
    await openForm()
    nameTheAdd('issues')
    fireEvent.click(presetButton('github'))
    // The placeholder is kept while nothing is named, and the consequence is stated
    // where it is left empty rather than discovered on the next poll.
    expect(
      within(block()).getByText(
        T.a_copy_with_no_repository_keeps_the_placeholder.replace('{{placeholder}}', 'OWNER/REPO'),
      ),
    ).toBeInTheDocument()
    review()
    expect(shownPatch()).toEqual({ sources: { issues: GH_ENTRY } })
    nameTheAddRepository('acme/late')
    expect(shownPatch()).toEqual({
      sources: {
        issues: {
          ...GH_ENTRY,
          poll: GH_POLL.map((argument) => argument.replace('OWNER/REPO', 'acme/late')),
        },
      },
    })
  })

  it('says a preset leaves no repository rather than offering an inert control', async () => {
    // Two shapes with no slot this form may fill: a poll carrying no placeholder at
    // all, and one carrying it on the PROGRAM — which is refused rather than
    // substituted, because argv[0] is what the engine executes.
    for (const poll of [
      ['fj', 'issue', 'list'],
      ['OWNER/REPO', 'issue', 'list'],
    ]) {
      const entry = { preset: 'forgejo', public: true, poll, field_map: { title: 'title' } }
      const doc = { sources: { fj: { ...entry } }, projects: {} }
      await openForm({
        registry: {
          body: registry({ source_presets: [{ host: 'forgejo', program: 'fj', entry }] }),
        },
        config: { body: snapshot(doc) },
      })
      expect(
        within(block()).getByText(
          T.the_preset_has_no_repository_slot.replace('{{preset}}', 'forgejo'),
        ),
      ).toBeInTheDocument()
      // Not an empty box: there is nowhere in this argv the form is allowed to write.
      expect(block().querySelector('[data-source-parameter="repository"]')).toBeNull()
      // And the rest of the form is still offered, because the entry is expressible.
      expect(fieldRow('enabled')).not.toBeNull()
      cleanup()
      vi.unstubAllGlobals()
      calls.length = 0
    }
  })
  it('refuses a value that is not one owner and one repo, and says so', async () => {
    await openForm()
    const box = within(repositoryRow()).getByRole('textbox')
    fireEvent.change(box, { target: { value: 'acme/widgets --jq .' } })
    // Stated, and nothing staged: the argv the patch would carry is untouched by
    // text the guard refused.
    expect(within(block()).getByText(T.that_is_not_a_repository_name)).toBeInTheDocument()
    expect(unwritten()).toBe('')
    // The text is kept so it can be corrected, not snapped back mid-typing.
    expect((box as HTMLInputElement).value).toBe('acme/widgets --jq .')
    fireEvent.change(box, { target: { value: 'acme/other' } })
    expect(within(block()).queryByText(T.that_is_not_a_repository_name)).toBeNull()
    expect(unwritten()).toContain(T.unwritten_source_changes)
  })

  it('withdraws the staged repository when the box turns malformed', async () => {
    await openForm()
    const box = within(repositoryRow()).getByRole('textbox')
    fireEvent.change(box, { target: { value: 'acme/other' } })
    expect(unwritten()).toContain(T.unwritten_source_changes)
    fireEvent.change(box, { target: { value: 'acme/other#frag' } })
    // The stale staged poll is withdrawn along with the statement: leaving it would
    // put an argv in the patch that the box no longer shows.
    expect(unwritten()).toBe('')
    expect(within(block()).getByText(T.that_is_not_a_repository_name)).toBeInTheDocument()
  })

  it('drops the refusal with the rest of the pending posture on discard', async () => {
    // What was refused was part of what is being discarded: a refusal caption
    // surviving a discard would be an outcome nothing on screen still explains.
    await openForm()
    const box = within(repositoryRow()).getByRole('textbox')
    fireEvent.change(box, { target: { value: 'acme/other#frag' } })
    expect(within(block()).getByText(T.that_is_not_a_repository_name)).toBeInTheDocument()
    fireEvent.click(within(fieldRow('enabled')).getByRole('checkbox'))
    expect(unwritten()).toContain(T.unwritten_source_changes)
    review()
    fireEvent.click(
      within(block()).getByRole('button', { name: T.discard_the_pending_changes }),
    )
    expect(within(block()).queryByText(T.that_is_not_a_repository_name)).toBeNull()
    // The box re-derives from the store rather than keeping the refused text.
    expect((within(repositoryRow()).getByRole('textbox') as HTMLInputElement).value).toBe(REPO)
  })

  it('refuses a malformed repository on the add block, and says so', async () => {
    await openForm()
    nameTheAdd('mirror')
    nameTheAddRepository('$(id)')
    expect(within(block()).getByText(T.that_is_not_a_repository_name)).toBeInTheDocument()
    // The copy is refused outright rather than staged with the value dropped.
    fireEvent.click(presetButton('github'))
    expect(unwritten()).toBe('')
  })

  it('refuses the copy when the chosen preset has no slot for the typed repository', async () => {
    // A preset with no placeholder has nowhere to put the typed value; composing
    // anyway would write an entry that silently ignores what the operator typed.
    const feed = {
      preset: 'feed',
      public: true,
      poll: ['feedctl', 'list', '--json'],
      field_map: { title: 'title' },
    }
    await openForm({
      registry: {
        body: registry({
          source_presets: [
            { host: 'github', program: 'gh', entry: GH_ENTRY },
            { host: 'feed', program: 'feedctl', entry: feed },
          ],
        }),
      },
    })
    nameTheAdd('mirror')
    nameTheAddRepository('acme/widgets')
    fireEvent.click(presetButton('feed'))
    expect(unwritten()).toBe('')
    expect(
      within(block()).getByText(
        T.the_preset_has_no_repository_slot.replace('{{preset}}', 'feed'),
      ),
    ).toBeInTheDocument()
    // With the box cleared the plain inert copy is allowed again.
    nameTheAddRepository('')
    fireEvent.click(presetButton('feed'))
    expect(unwritten()).toContain(T.unwritten_source_changes)
  })
})

describe('editing a stored source', () => {
  it('stages the enable, the project binding and the maintainers at their own paths', async () => {
    await openForm()
    fireEvent.click(within(fieldRow('enabled')).getByRole('checkbox'))
    const projects = within(fieldRow('project')).getByRole('group', {
      name: T.project_for_source.replace('{{source}}', 'gh'),
    })
    // The binding in force is pressed, and it is the document's own.
    expect(within(projects).getByRole('button', { name: 'acme' })).toHaveAttribute(
      'aria-pressed',
      'true',
    )
    fireEvent.click(within(projects).getByRole('button', { name: 'widgets' }))
    fireEvent.change(within(fieldRow('maintainers')).getByRole('textbox'), {
      target: { value: 'octocat, hubber' },
    })
    review()
    expect(shownPatch()).toEqual({
      sources: {
        gh: { enabled: true, project: 'widgets', maintainers: ['octocat', 'hubber'] },
      },
    })
    // Every row keeps showing the store beside the staged value, so a refused write
    // leaves the form stating what is persisted.
    expect(fieldRow('project')).toHaveAttribute('data-staged', 'true')
    expect(within(fieldRow('maintainers')).getByText('["octocat"]')).toBeInTheDocument()
  })

  it('renders the per-source settings from the registry, carrying its own bounds', async () => {
    await openForm()
    const interval = within(settingRow('sources.gh.watch.interval_s')).getByRole('spinbutton')
    expect(interval).toHaveAttribute('min', '30')
    expect(interval).toHaveAttribute('step', '1')
    expect((interval as HTMLInputElement).value).toBe('600')
    // The engine's own summary, and the label the pane already had for the key.
    expect(
      within(block()).getByText(
        'Seconds between watch-source poll ticks. Polling spends no model credits.',
      ),
    ).toBeInTheDocument()
    expect(within(block()).getByText(L.watch_interval_s)).toBeInTheDocument()
    // A setting no source may hold is not offered here at all.
    expect(
      within(block()).queryByText('sources.gh.concurrency.wave_max_tasks', {
        selector: '.se-kv-path',
      }),
    ).toBeNull()
    fireEvent.change(interval, { target: { value: '900' } })
    review()
    expect(shownPatch()).toEqual({ sources: { gh: { watch: { interval_s: 900 } } } })
  })

  it('withdraws an unchecked enable on a source that stores none', async () => {
    await openForm()
    const box = within(fieldRow('enabled')).getByRole('checkbox')
    fireEvent.click(box)
    expect(unwritten()).toContain(T.unwritten_source_changes)
    // Absent and false are one posture — the engine polls neither — so unchecking
    // withdraws rather than writing a key that changes nothing.
    fireEvent.click(box)
    expect(unwritten()).toBe('')
  })
})

describe('enabling a source states what it starts', () => {
  it('names the program it will run and links to the grid that bounds it', async () => {
    await openForm()
    fireEvent.click(within(fieldRow('enabled')).getByRole('checkbox'))
    review()
    expect(
      within(block()).getByText(
        T.enabling_begins_polling
          .replace('{{source}}', 'gh')
          .replace('{{program}}', 'gh')
          .replace('{{path}}', 'sources.gh.enabled'),
      ),
    ).toBeInTheDocument()
    // The grid is linked rather than rendered twice, and the fail-closed rule is
    // stated because a new source has no grid at all.
    expect(
      within(block()).getAllByRole('link', {
        name: T.open_the_autonomy_grid_for_source.replace('{{source}}', 'gh'),
      }).length,
    ).toBeGreaterThan(0)
    expect(within(block()).getAllByText(T.an_absent_grid_fails_closed).length).toBeGreaterThan(0)
  })

  it('selects that source in the autonomy grid when the link is followed', async () => {
    await openForm()
    selectSource('extra')
    selectSource('gh')
    fireEvent.click(
      within(block()).getAllByRole('link', {
        name: T.open_the_autonomy_grid_for_source.replace('{{source}}', 'gh'),
      })[0],
    )
    const picker = within(gridBlock()).getByRole('group', { name: G.select_a_watch_source })
    expect(within(picker).getByRole('button', { name: 'gh' })).toHaveAttribute(
      'aria-pressed',
      'true',
    )
  })
})

describe('a stored shape the form cannot express', () => {
  it('routes a source whose poll no preset supplies to the JSON view, with no form', async () => {
    await openForm()
    selectSource('legacy')
    expect(
      within(block()).getByText(T.the_form_cannot_express_this_source.replace('{{source}}', 'legacy')),
    ).toBeInTheDocument()
    expect(within(block()).getByText(T.the_poll_is_not_a_bundled_presets)).toBeInTheDocument()
    // Not a partial form: no control that could write a field the state did not show.
    expect(block().querySelector('[data-source-field]')).toBeNull()
    // And the route is a real one: the JSON view opens on it.
    fireEvent.click(
      within(block()).getByRole('button', {
        name: T.edit_this_source_in_the_json_view.replace('{{source}}', 'legacy'),
      }),
    )
    expect(await screen.findByLabelText(C.the_configuration_document)).toBeInTheDocument()
  })

  it('names the fields it does not show', async () => {
    await openForm()
    selectSource('extra')
    expect(
      within(block()).getByText(T.the_entry_carries_unshown_fields.replace('{{fields}}', 'spend_cap')),
    ).toBeInTheDocument()
    expect(block().querySelector('[data-source-field]')).toBeNull()
  })

  it('still offers the removal, which writes no field it did not show', async () => {
    await openForm()
    selectSource('legacy')
    expect(
      within(block()).getByText(T.a_removal_writes_no_field_it_did_not_show),
    ).toBeInTheDocument()
    // A deletion cannot rewrite a field this state withheld, so withholding it too
    // would leave a source the form cannot describe removable only by hand.
    fireEvent.click(
      within(block()).getByRole('button', {
        name: T.remove_the_source.replace('{{source}}', 'legacy'),
      }),
    )
    fireEvent.change(
      within(block()).getByLabelText(T.type_the_name_to_confirm.replace('{{source}}', 'legacy')),
      { target: { value: 'legacy' } },
    )
    fireEvent.click(
      within(block()).getByRole('button', {
        name: T.confirm_the_removal.replace('{{source}}', 'legacy'),
      }),
    )
    review()
    expect(shownPatch()).toEqual({ sources: { legacy: null } })
    // And still no editing control anywhere on the state.
    expect(block().querySelector('[data-source-field]')).toBeNull()
    expect(block().querySelector('[data-source-parameter="repository"]')).toBeNull()
    confirm()
    await waitFor(() => expect(calls.some((call) => call.method === 'PUT')).toBe(true))
    expect(putPatch()).toEqual({ sources: { legacy: null } })
  })
})

describe('removing a source takes its name', () => {
  it('refuses a confirmation whose typed name does not match, and says so', async () => {
    await openForm()
    fireEvent.click(
      within(block()).getByRole('button', { name: T.remove_the_source.replace('{{source}}', 'gh') }),
    )
    // The consequence is stated with the arm, before anything is staged.
    expect(
      within(block()).getByText(
        T.removing_stops_ingesting.replace('{{source}}', 'gh').replace('{{path}}', 'sources.gh'),
      ),
    ).toBeInTheDocument()
    fireEvent.change(within(block()).getByLabelText(T.type_the_name_to_confirm.replace('{{source}}', 'gh')), {
      target: { value: 'legacy' },
    })
    fireEvent.click(
      within(block()).getByRole('button', { name: T.confirm_the_removal.replace('{{source}}', 'gh') }),
    )
    // A refused click is acknowledged rather than left looking inert.
    expect(within(block()).getByText(T.the_removal_was_refused)).toBeInTheDocument()
    expect(
      within(block()).getByText(T.the_typed_name_does_not_match.replace('{{source}}', 'gh')),
    ).toBeInTheDocument()
    expect(unwritten()).toBe('')
  })

  it('stages the store\u2019s own deletion when the name matches, and states it stops ingesting', async () => {
    await openForm()
    fireEvent.click(
      within(block()).getByRole('button', { name: T.remove_the_source.replace('{{source}}', 'gh') }),
    )
    fireEvent.change(within(block()).getByLabelText(T.type_the_name_to_confirm.replace('{{source}}', 'gh')), {
      target: { value: 'gh' },
    })
    fireEvent.click(
      within(block()).getByRole('button', { name: T.confirm_the_removal.replace('{{source}}', 'gh') }),
    )
    review()
    expect(
      within(block()).getByText(
        T.edit_removes_the_source.replace('{{source}}', 'gh').replace('{{path}}', 'sources.gh'),
      ),
    ).toBeInTheDocument()
    confirm()
    await waitFor(() => expect(calls.some((call) => call.method === 'PUT')).toBe(true))
    // The store's own deletion spelling, at the source's own path.
    expect(putPatch()).toEqual({ sources: { gh: null } })
  })

  it('drops an edit inside the source when its removal is staged', async () => {
    await openForm()
    fireEvent.click(within(fieldRow('enabled')).getByRole('checkbox'))
    expect(unwritten()).toContain(T.unwritten_source_changes)
    fireEvent.click(
      within(block()).getByRole('button', { name: T.remove_the_source.replace('{{source}}', 'gh') }),
    )
    fireEvent.change(within(block()).getByLabelText(T.type_the_name_to_confirm.replace('{{source}}', 'gh')), {
      target: { value: 'gh' },
    })
    fireEvent.click(
      within(block()).getByRole('button', { name: T.confirm_the_removal.replace('{{source}}', 'gh') }),
    )
    review()
    // The patch is last-edit-wins over overlapping paths, so an ancestor and a
    // descendant cannot both survive it. The card must therefore describe one
    // change, not two — otherwise the operator confirms an edit that never lands.
    expect(shownPatch()).toEqual({ sources: { gh: null } })
    expect(
      within(block()).queryByText(
        T.edit_enables_the_source
          .replace('{{source}}', 'gh')
          .replace('{{path}}', 'sources.gh.enabled'),
      ),
    ).toBeNull()
    expect(unwritten()).toContain('1')
    confirm()
    await waitFor(() => expect(calls.some((call) => call.method === 'PUT')).toBe(true))
    expect(putPatch()).toEqual({ sources: { gh: null } })
  })
})

describe('what a refusal and a success each leave behind', () => {
  it('keeps the staged change and the stored values when the write door refuses', async () => {
    await openForm({
      put: {
        status: 422,
        body: {
          code: 'config_invalid',
          error: 'sources.gh.enabled: expected true or false',
        },
      },
    })
    fireEvent.click(within(fieldRow('enabled')).getByRole('checkbox'))
    review()
    confirm()
    expect(await within(block()).findByText(T.could_not_write_the_source_change)).toBeInTheDocument()
    expect(
      within(block()).getByText(/sources\.gh\.enabled: expected true or false/),
    ).toBeInTheDocument()
    expect(
      within(block()).getByText(T.nothing_was_written_so_the_source_is_stored_state),
    ).toBeInTheDocument()
    // Nothing was written, so the row still states what the store holds — and the
    // staged change is still here to be corrected and sent again.
    expect(unwritten()).toContain(T.unwritten_source_changes)
    expect(within(block()).queryByText(T.wrote_the_change_and_re_read_the_sources)).toBeNull()
  })

  it('re-renders the form from a fresh read after a successful write', async () => {
    // The obligation this form's mutation owns: `FormReview` is presentational and
    // cannot invalidate for its callers. The document is where every row comes from,
    // the resolved read beside it renders per-source settings, and the grid below is
    // a resolution of the very entries this write changes.
    const after = stored()
    after.sources.gh.enabled = true
    await openForm({ configAfterPut: { body: snapshot(after) } })
    fireEvent.click(within(fieldRow('enabled')).getByRole('checkbox'))
    review()
    const before = calls.length
    confirm()
    await within(block()).findByText(T.wrote_the_change_and_re_read_the_sources)
    // The row is not told what was sent; it re-renders from what the store now
    // answers, which is the only way the form and the store cannot disagree.
    await waitFor(() =>
      expect((within(fieldRow('enabled')).getByRole('checkbox') as HTMLInputElement).checked).toBe(
        true,
      ),
    )
    expect(fieldRow('enabled')).toHaveAttribute('data-staged', 'false')
    const urls = calls.slice(before).map((call) => call.url)
    expect(urls.some((url) => url === '/api/apps/spec-engine/config')).toBe(true)
    expect(urls.some((url) => url.startsWith('/api/apps/spec-engine/config/resolved'))).toBe(true)
    expect(urls.some((url) => url.startsWith('/api/apps/spec-engine/config/sources'))).toBe(true)
  })
})

describe('a failed read is doubt, not a form', () => {
  it('states the preset refusal and offers no control', async () => {
    await openConfig({
      registry: { status: 503, body: { code: 'app_disabled', error: 'the app is disabled' } },
    })
    expect(await within(block()).findByText(T.could_not_read_the_source_presets)).toBeInTheDocument()
    // Not one control: the preset vocabulary IS the form.
    expect(block().querySelectorAll('input')).toHaveLength(0)
    expect(within(block()).queryByRole('button', { name: T.review_the_exact_change })).toBeNull()
  })

  it('says so rather than offering an empty picker when the engine bundles none', async () => {
    await openConfig({ registry: { body: registry({ source_presets: [] }) } })
    expect(await within(block()).findByText(T.the_engine_bundles_no_preset)).toBeInTheDocument()
  })
})

describe('the operator-facing strings', () => {
  it('ship in all thirteen catalogs with the same keys', async () => {
    const catalogs: Array<[string, Record<string, unknown>]> = [
      ['bn', bn.apps.specEngine.sourceForm],
      ['de', de.apps.specEngine.sourceForm],
      ['en', en.apps.specEngine.sourceForm],
      ['es', es.apps.specEngine.sourceForm],
      ['fr', fr.apps.specEngine.sourceForm],
      ['hi', hi.apps.specEngine.sourceForm],
      ['it', itIT.apps.specEngine.sourceForm],
      ['ja', ja.apps.specEngine.sourceForm],
      ['ko', ko.apps.specEngine.sourceForm],
      ['pt', pt.apps.specEngine.sourceForm],
      ['ru', ru.apps.specEngine.sourceForm],
      ['zh-CN', zh.apps.specEngine.sourceForm],
    ]
    const expected = Object.keys(T).sort()
    for (const [locale, catalog] of catalogs) {
      expect(Object.keys(catalog).sort(), locale).toEqual(expected)
      for (const [key, value] of Object.entries(catalog)) {
        expect(typeof value, `${locale}.${key}`).toBe('string')
        expect((value as string).trim(), `${locale}.${key}`).not.toBe('')
      }
    }
    // The pseudolocale is generated from English, so it is checked for presence
    // rather than for content: a key missing there means it was never regenerated.
    const pseudo = (await import('../i18n/locales/en-XA.json')).default as {
      apps: { specEngine: { sourceForm: Record<string, unknown> } }
    }
    expect(Object.keys(pseudo.apps.specEngine.sourceForm).sort()).toEqual(expected)
  })

  it('interpolates every placeholder its call site supplies', () => {
    // A placeholder nobody fills renders as `{{name}}` on screen, and a call site
    // filling one the string does not carry silently drops the value.
    for (const key of [
      'confirm_the_removal',
      'edit_this_source_in_the_json_view',
      'open_the_autonomy_grid_for_source',
      'project_for_source',
      'remove_the_source',
      'the_form_cannot_express_this_source',
      'the_name_is_already_a_source',
      'the_staged_copy_was_withdrawn',
      'the_typed_name_does_not_match',
      'type_the_name_to_confirm',
    ] as const) {
      expect(T[key], key).toContain('{{source}}')
    }
    expect(T.copy_the_preset).toContain('{{host}}')
    expect(T.the_entry_carries_unshown_fields).toContain('{{fields}}')
    expect(T.the_registry_kind_is_not_editable_here).toContain('{{kind}}')
    expect(T.the_preset_has_no_repository_slot).toContain('{{preset}}')
    for (const key of [
      'a_copy_with_no_repository_keeps_the_placeholder',
      'the_repository_is_still_the_placeholder',
    ] as const) {
      expect(T[key], key).toContain('{{placeholder}}')
    }
    for (const name of ['source', 'program', 'placeholder', 'path']) {
      expect(T.enabling_polls_the_placeholder).toContain(`{{${name}}}`)
    }
    for (const name of ['source', 'preset', 'oldValue', 'newValue', 'path']) {
      expect(T.edit_names_the_repository).toContain(`{{${name}}}`)
    }
    for (const name of ['source', 'preset', 'program', 'repository', 'path']) {
      expect(T.edit_copies_the_preset_for_repository).toContain(`{{${name}}}`)
    }
    for (const name of ['host', 'program', 'count', 'fields']) {
      expect(T.preset_ingests_items).toContain(`{{${name}}}`)
    }
    for (const name of ['source', 'path']) {
      expect(T.edit_enables_the_source).toContain(`{{${name}}}`)
      expect(T.edit_disables_the_source).toContain(`{{${name}}}`)
      expect(T.edit_removes_the_source).toContain(`{{${name}}}`)
      expect(T.removing_stops_ingesting).toContain(`{{${name}}}`)
    }
    for (const name of ['source', 'program', 'path']) {
      expect(T.enabling_begins_polling).toContain(`{{${name}}}`)
    }
    for (const name of ['source', 'preset', 'program', 'path']) {
      expect(T.edit_copies_the_bundled_preset).toContain(`{{${name}}}`)
    }
    for (const name of ['source', 'oldValue', 'newValue', 'path']) {
      expect(T.edit_replaces_the_project_binding).toContain(`{{${name}}}`)
      expect(T.edit_replaces_the_maintainers).toContain(`{{${name}}}`)
    }
    for (const name of ['setting', 'source', 'oldValue', 'newValue', 'path']) {
      expect(T.edit_replaces_the_source_limit).toContain(`{{${name}}}`)
    }
  })
})
