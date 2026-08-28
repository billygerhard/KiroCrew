/**
 * The settings form: generated from the engine's registry, written through the
 * one guarded door.
 *
 * Every property here is a correctness claim rather than a rendering preference:
 *
 *   - **The rows are GENERATED.** One per registry entry, in the order the read
 *     supplies them, with the control chosen by the registry's own `kind` and the
 *     bounds carried by that control. A hard-coded field list is how a form comes
 *     to offer a setting the write door rejects, or omit one it accepts — and
 *     neither failure shows up until somebody tries to change a value.
 *   - **A scope is offered only where the registry permits it AND a target
 *     exists.** Offering a scope the registry forbids invites a refusal the
 *     operator cannot act on; offering one with no target would compose
 *     `projects..limits.x`, a write into a project named the empty string.
 *   - **The nested path is the engine's own.** App scope writes the top-level
 *     `<group>.<leaf>`, project scope `projects.<name>.<group>.<leaf>`, source
 *     scope `sources.<name>.<group>.<leaf>` — the paths `stored_value` reads.
 *   - **A staged edit is visibly distinct from the value in force**, and the value
 *     in force stays on the row. Collapsing the two would leave a refused write
 *     showing the submitted value as though it were stored.
 *   - **A refusal retains stored state.** The engine's reason is rendered, the
 *     staged edits stay put to be corrected, and nothing is invalidated.
 *   - **A success re-reads.** This form's own mutation owns the invalidation,
 *     because the shared review card is presentational and cannot do it for its
 *     callers — the property `re-renders every row from a fresh read` pins it.
 *   - **An unknown kind is a read-only row, never a crash.** The vocabulary is the
 *     engine's; a type this form has no control for must state itself and route to
 *     the JSON view rather than disappearing from the form.
 *
 * The generated-vocabulary half of the totality claim — one control per setting for
 * ANY vocabulary, not just the shipped one — is
 * `SpecEngineSettingsForm.property.test.tsx`.
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

const T = en.apps.specEngine.settingsForm
const C = en.apps.specEngine.configPanel
const P = en.apps.specEngine.specEnginePage
const L = C.setting_labels

import { declaredGroups } from '../apps/spec-engine/stages'
import { stagesUnder, stubSpecEngineFetch, type Answer } from './specEngineFetchStub'

/** Every request the page made, so an assertion can read the body that was sent. */
const calls: Array<{ url: string; method: string; body: unknown }> = []

/**
 * The registry payload, in `_registry_payload`'s shape.
 *
 * Six settings covering every projected kind and every scope combination the
 * shipped registry uses: app-only, app plus project, app plus source. Their
 * summaries and bounds are the engine's own, so a row asserted here is a row an
 * operator would meet.
 */
function registry(over: Record<string, unknown> = {}) {
  return {
    settings: [
      {
        key: 'limits.task_retry_limit',
        kind: 'int',
        default: 2,
        minimum: 0,
        maximum: null,
        scopes: ['app', 'project'],
        summary: 'Retries for one task before it fails.',
      },
      {
        key: 'budget.warn_fraction',
        kind: 'float',
        default: 0.8,
        minimum: 0,
        maximum: 1,
        scopes: ['app', 'project'],
        summary: 'Fraction of the ceiling at which a run notifies without halting.',
      },
      {
        key: 'delivery.auto_integrate',
        kind: 'bool',
        default: false,
        minimum: null,
        maximum: null,
        scopes: ['app', 'project'],
        summary: 'Whether a run may integrate without human action.',
      },
      {
        key: 'notify.channel',
        kind: 'str',
        default: 'dashboard',
        minimum: null,
        maximum: null,
        scopes: ['app', 'project'],
        summary: 'Host gateway channel notifications route to.',
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
      {
        key: 'concurrency.global_max_runs',
        kind: 'int',
        default: 4,
        minimum: 1,
        maximum: null,
        scopes: ['app'],
        summary: 'Runs the engine executes at once across every project.',
      },
    ],
    source_presets: [],
    profile_presets: [],
    roles: [],
    levels: [],
    ...over,
  }
}

/**
 * The registry payload with every group its settings declare under ONE stage.
 *
 * The pane renders only a stage's own groups, so a fixture spread across four areas
 * would put this suite's rows behind four navigations for no gain: what is under
 * test is the form, and the areas are `SpecEngineConfigTabs.test.tsx`. Derived
 * from the FINAL settings list rather than from a literal, so a case that replaces
 * `settings` — the unknown-kind row, whose group nothing else declares — is placed
 * on the same stage as the rest instead of folding into the advanced area.
 */
function registryOneStage(over: Record<string, unknown> = {}) {
  const payload = registry(over)
  const keys = (payload.settings as Array<{ key: string }>).map((entry) => entry.key)
  return { ...payload, stages: stagesUnder('execution', declaredGroups(keys)) }
}

/** One resolved setting, in `EffectiveValue.to_json_object`'s shape. */
function effective(
  key: string,
  value: unknown,
  over: Record<string, unknown> = {},
): Record<string, unknown> {
  return {
    key,
    value,
    origin: 'bundled_default',
    declared_at: '',
    is_default: true,
    ...over,
  }
}

/** The resolved read: every registry key answered, one of them app-configured. */
function resolved(over: Record<string, unknown> = {}) {
  return {
    configured: true,
    project: null,
    source: null,
    settings: [
      effective('limits.task_retry_limit', 7, {
        origin: 'app_config',
        declared_at: 'limits.task_retry_limit',
        is_default: false,
      }),
      effective('budget.warn_fraction', 0.8),
      effective('delivery.auto_integrate', false),
      effective('notify.channel', 'dashboard'),
      effective('watch.interval_s', 300),
      effective('concurrency.global_max_runs', 4),
    ],
    roles: { profile: '', roles: {} },
    role_order: [],
    ...over,
  }
}

/** The stored document: two projects, so a project-scoped write has a name. */
function stored() {
  return {
    projects: { acme: { path: '/src/acme' }, widgets: { path: '/src/widgets' } },
    limits: { task_retry_limit: 7 },
  }
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

/** One watch source, so a source-scoped write has a name to target. */
function sources(names: string[] = ['gh']) {
  return {
    sources: names.map((name) => ({ name, grid: {} })),
    submitter_classes: ['maintainer', 'external'],
    spec_types: ['feature'],
    levels: ['authoring', 'execution'],
  }
}

function stub(answers: {
  registry?: Answer
  resolved?: Answer
  /** The resolved answer once a PUT has landed, as the store would then answer. */
  resolvedAfterPut?: Answer
  sources?: Answer
  put?: Answer
}) {
  stubSpecEngineFetch(
    {
      registry: answers.registry ?? { body: registryOneStage() },
      resolved: ({ written }) =>
        (written ? answers.resolvedAfterPut : undefined) ??
        answers.resolved ?? { body: resolved() },
      sources: answers.sources ?? { body: sources() },
      config: { body: snapshot(stored()) },
      configWrite: answers.put ?? { body: { ok: true, document: {}, advisories: [] } },
    },
    { record: calls },
  )
}

/** Render the page, switch to the configuration pane, wait for the settings. */
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
  // One area per pipeline stage, and only the active one is in the accessibility
  // tree the role queries read: an inactive panel carries `hidden`. This fixture
  // places every group it declares under execution, so that is the area to open —
  // falling back to the advanced area, which is where the settings surface lands
  // when there is no vocabulary to place at all: a refused read, or an engine that
  // registers no setting. Both are cases this suite covers.
  await screen.findByRole('tablist', { name: C.configuration_stages })
  for (const label of [C.stage_execution, C.stage_advanced]) {
    const stage = screen.queryByRole('tab', { name: new RegExp(`^${label}`) })
    if (!stage) continue
    fireEvent.click(stage)
    if (screen.queryByRole('heading', { name: T.settings })) break
  }
  await screen.findByRole('heading', { name: T.settings })
  return client
}

/**
 * Open the pane, reveal every setting, and wait until the generated rows are on
 * screen.
 *
 * The heading renders in all three of the block's states — reading, refused, and
 * generated — so waiting on it alone would let an assertion run against the
 * reading state. A test about a refusal or an empty vocabulary waits on its own
 * text instead.
 *
 * Every setting is REVEALED here because the surface renders only the rows whose
 * in-force value is not the bundled default, and five of this fixture's six are
 * defaulted. The cases in this file are about generation, scoping, staging and
 * writing, each of which has to hold for a defaulted row too — a form that could
 * only edit already-configured settings would be a form that cannot configure
 * anything. The filter itself has its own describe block below, which does not
 * reveal.
 */
async function openRows(answers: Parameters<typeof stub>[0] = {}) {
  const client = await openConfig(answers)
  revealEverySetting()
  await waitFor(() =>
    expect(settingRows().length).toBeGreaterThan(0),
  )
  return client
}

/**
 * Show the rows at their bundled default too, if the control is on screen.
 *
 * Guarded rather than asserted, because `openConfig` also reaches states with no
 * rows and therefore no control: a refused read, and a vocabulary the engine
 * registers nothing in.
 */
function revealEverySetting() {
  const control = within(block()).queryByRole('button', { name: showEverySettingLabel() })
  if (control) fireEvent.click(control)
}

/** The reveal control's label for a vocabulary of *count* settings. */
function showEverySettingLabel(count = 6): string {
  return C.show_every_setting.replace('{{count}}', String(count))
}

/**
 * The settings block.
 *
 * Every query is scoped to it, because the resolved pane beside the form renders
 * the SAME labels and the same registry keys — that is the point of the pane — so
 * an unscoped query would find either surface and could not tell them apart.
 */
function block(): HTMLElement {
  const heading = screen.getByRole('heading', { name: T.settings })
  const found = heading.closest('.se-blk')
  expect(found).not.toBeNull()
  return found as HTMLElement
}

/**
 * The generated rows of the SETTINGS form.
 *
 * Scoped to the block for the reason {@link block} gives, and here it is load
 * bearing rather than tidy: the profiles form below renders `.se-setting` rows of
 * its own — role assignments and a profile's pinned limits — so an unscoped count
 * would count another form's rows as this one's.
 */
function settingRows(): HTMLElement[] {
  return [...block().querySelectorAll('.se-setting')] as HTMLElement[]
}

/**
 * The row for one registry key.
 *
 * Found through the key rather than the label, because the key is what the
 * document and the write log speak — and a row whose label is missing must still
 * be addressable.
 */
function row(key: string): HTMLElement {
  const path = within(block()).getByText(key, { selector: '.se-kv-path, .se-m' })
  const found = path.closest('.se-setting')
  expect(found).not.toBeNull()
  return found as HTMLElement
}

/** The one control on a row. */
function control(key: string): HTMLInputElement {
  // Every control, whatever its kind: the claim is that there is exactly ONE per
  // row, so the query must not be able to miss a second one of another type.
  const inputs = row(key).querySelectorAll('input')
  expect(inputs).toHaveLength(1)
  return inputs[0] as HTMLInputElement
}

/** The scope button for one key, whatever its state. */
function scope(key: string, name: string): HTMLElement {
  return within(row(key)).getByRole('button', { name })
}

/** The control that opens the review card, whatever its state. */
function reviewControl(): HTMLElement {
  // Scoped to the block: the autonomy grid below the form offers a control with
  // the SAME label, because the two forms own their own copy of the review card's
  // words. An unscoped query cannot tell one form's confirm from another's.
  return within(block()).getByRole('button', { name: T.review_the_exact_change })
}

/**
 * The count of unwritten changes, or `''` when the form shows none.
 *
 * A substring match rather than an exact one: the label is one span holding the
 * words and the count, so its text is never the sentence alone.
 */
function unwritten(): string {
  const found = within(block()).queryByText(new RegExp(T.unwritten_setting_changes))
  return found ? (found.textContent ?? '') : ''
}

/** Open the review card, having staged at least one edit. */
function review() {
  fireEvent.click(reviewControl())
}

/** Confirm the review card, which is the only thing here that writes. */
function confirm() {
  fireEvent.click(within(block()).getByRole('button', { name: T.write_the_change }))
}

/** The patch the one PUT carried. */
function putPatch(): unknown {
  const put = calls.filter((call) => call.method === 'PUT')
  expect(put).toHaveLength(1)
  return (put[0].body as { patch: unknown }).patch
}

/** Select a project row, which is what a project-scoped write targets. */
function selectProject(name: string) {
  const table = screen.getByRole('grid', { name: C.configured_projects })
  const rows = within(table)
    .getAllByRole('row')
    .filter((entry) => !entry.classList.contains('se-qhead'))
  const found = rows.find((entry) => entry.textContent?.includes(name))
  expect(found).toBeDefined()
  fireEvent.focus(found as HTMLElement)
}

afterEach(() => {
  vi.unstubAllGlobals()
  calls.length = 0
})

describe('the rows are generated from the registry', () => {
  it('renders one row per registry entry, prose leading and the key beside it', async () => {
    await openRows()
    expect(settingRows()).toHaveLength(6)
    // Prose leads: the label the catalog names for the key.
    expect(within(block()).getByText(L.limits_task_retry_limit)).toBeInTheDocument()
    expect(within(block()).getByText(L.watch_interval_s)).toBeInTheDocument()
    // And the registry key stays on screen as the detail line, because it is what
    // the document and the write log speak.
    expect(row('limits.task_retry_limit')).toBeInTheDocument()
    // The registry's OWN summary is the help text, not a second sentence kept here.
    expect(
      within(row('watch.interval_s')).getByText('Seconds between watch-source poll ticks.'),
    ).toBeInTheDocument()
  })

  it('gives each kind its own control, carrying the registry bounds', async () => {
    await openRows()
    const retries = control('limits.task_retry_limit')
    expect(retries.type).toBe('number')
    expect(retries).toHaveAttribute('min', '0')
    // No ceiling in the registry, so none on the control: a bound invented here
    // would refuse a value the engine accepts.
    expect(retries).not.toHaveAttribute('max')
    expect(retries).toHaveAttribute('step', '1')

    const fraction = control('budget.warn_fraction')
    expect(fraction.type).toBe('number')
    expect(fraction).toHaveAttribute('min', '0')
    expect(fraction).toHaveAttribute('max', '1')
    // A float steps by any amount; stepping a fraction by one would offer only
    // values outside its own bounds.
    expect(fraction).toHaveAttribute('step', 'any')

    // A two-state control for a boolean, and free text for a string.
    expect(control('delivery.auto_integrate').type).toBe('checkbox')
    expect(control('notify.channel').type).toBe('text')
  })

  it('shows the value in force with the origin that decided it', async () => {
    await openRows()
    const retries = row('limits.task_retry_limit')
    expect(within(retries).getByText('7')).toBeInTheDocument()
    expect(within(retries).getByText(C.origin_app_config)).toBeInTheDocument()
    // A default-valued row says so, which is the distinction the whole resolved
    // read exists for: 4 chosen and 4 shipped call for opposite actions.
    expect(
      within(row('concurrency.global_max_runs')).getByText(C.origin_bundled_default),
    ).toBeInTheDocument()
    // The control opens on the value in force, never on the registry default: the
    // two differ here, and only one of them is what is stored.
    expect(control('limits.task_retry_limit').value).toBe('7')
  })

  it('states the empty vocabulary rather than an empty form', async () => {
    await openConfig({ registry: { body: registryOneStage({ settings: [] }) } })
    expect(await screen.findByText(T.no_setting_is_registered)).toBeInTheDocument()
    expect(settingRows()).toHaveLength(0)
  })
})

describe('the scope a write targets', () => {
  it('offers only the scopes the registry permits for the setting', async () => {
    await openRows()
    // App and project for this one, and no source: a source-scoped write the
    // engine would refuse must not be offered at all.
    expect(scope('limits.task_retry_limit', 'app')).toBeInTheDocument()
    expect(scope('limits.task_retry_limit', 'project')).toBeInTheDocument()
    expect(
      within(row('limits.task_retry_limit')).queryByRole('button', { name: 'source' }),
    ).toBeNull()
    // App and source for the poll interval, and no project.
    expect(scope('watch.interval_s', 'source')).toBeInTheDocument()
    expect(within(row('watch.interval_s')).queryByRole('button', { name: 'project' })).toBeNull()
    // App only, so one button and nothing else.
    expect(
      within(row('concurrency.global_max_runs')).getAllByRole('button', { name: /app|project|source/ }),
    ).toHaveLength(1)
  })

  it('refuses a scope with no target rather than composing an empty name', async () => {
    // The pane opens on the app-wide row, so no project is selected. Project scope
    // is permitted by the registry and has nowhere to land.
    await openRows()
    expect(scope('limits.task_retry_limit', 'project')).toBeDisabled()
    expect(scope('limits.task_retry_limit', 'app')).toHaveAttribute('aria-pressed', 'true')
    // With no source configured, source scope has nowhere to land either.
    await waitFor(() => expect(screen.getByRole('heading', { name: T.settings })).toBeVisible())
    // Selecting a project gives project scope a target.
    selectProject('acme')
    await waitFor(() => expect(scope('limits.task_retry_limit', 'project')).toBeEnabled())
  })

  it('writes the top-level path at app scope', async () => {
    await openRows()
    fireEvent.change(control('limits.task_retry_limit'), { target: { value: '5' } })
    review()
    confirm()
    await waitFor(() => expect(putPatch()).toEqual({ limits: { task_retry_limit: 5 } }))
  })

  it('writes the nested project path at project scope', async () => {
    await openRows()
    selectProject('acme')
    await waitFor(() => expect(scope('limits.task_retry_limit', 'project')).toBeEnabled())
    fireEvent.click(scope('limits.task_retry_limit', 'project'))
    fireEvent.change(control('limits.task_retry_limit'), { target: { value: '5' } })
    review()
    confirm()
    await waitFor(() =>
      expect(putPatch()).toEqual({ projects: { acme: { limits: { task_retry_limit: 5 } } } }),
    )
  })

  it('writes the nested source path at source scope', async () => {
    await openRows()
    fireEvent.click(scope('watch.interval_s', 'source'))
    fireEvent.change(control('watch.interval_s'), { target: { value: '600' } })
    review()
    confirm()
    await waitFor(() => expect(putPatch()).toEqual({ sources: { gh: { watch: { interval_s: 600 } } } }))
  })

  it('offers the source picker only while a setting can be written at source scope', async () => {
    // The picker chooses where source-scoped writes land, so it is gated on the
    // registry rather than on sources merely existing: on a vocabulary with no
    // source-scoped setting it would be a chooser that targets nothing.
    await openRows()
    expect(
      within(block()).getByRole('group', { name: T.select_a_watch_source_to_write_at }),
    ).toBeInTheDocument()
    cleanup()
    const vocabulary = registry()
    vocabulary.settings = vocabulary.settings.filter(
      (setting) => !setting.scopes.includes('source'),
    )
    await openRows({ registry: { body: vocabulary } })
    expect(
      within(block()).queryByRole('group', { name: T.select_a_watch_source_to_write_at }),
    ).not.toBeInTheDocument()
  })

  it('moves a staged value with the scope, leaving one path in the patch', async () => {
    // Two paths for one row would be two changes the operator made once, and the
    // patch builder can only carry one of any overlapping pair.
    await openRows()
    selectProject('acme')
    await waitFor(() => expect(scope('limits.task_retry_limit', 'project')).toBeEnabled())
    fireEvent.change(control('limits.task_retry_limit'), { target: { value: '5' } })
    fireEvent.click(scope('limits.task_retry_limit', 'project'))
    review()
    expect(within(block()).getAllByText(/limits\.task_retry_limit/)).not.toHaveLength(0)
    confirm()
    await waitFor(() =>
      expect(putPatch()).toEqual({ projects: { acme: { limits: { task_retry_limit: 5 } } } }),
    )
  })

  it('drops a project-scoped edit when the project it named stops being selected', async () => {
    // The edit's path is no longer one any row shows, so no sentence could describe
    // it and no confirm could clear it — and leaving it staged would put a path in
    // the patch the review card never accounted for.
    await openRows()
    selectProject('acme')
    await waitFor(() => expect(scope('limits.task_retry_limit', 'project')).toBeEnabled())
    fireEvent.click(scope('limits.task_retry_limit', 'project'))
    fireEvent.change(control('limits.task_retry_limit'), { target: { value: '5' } })
    expect(unwritten()).toContain(T.unwritten_setting_changes)
    selectProject('widgets')
    // The resolution is read per project, so the form states it is reading before
    // it renders the other project's rows: doubt has to look like doubt.
    await waitFor(() => expect(row('limits.task_retry_limit')).toBeInTheDocument())
    expect(unwritten()).toBe('')
    expect(reviewControl()).toBeDisabled()
  })
})

describe('a staged edit is not a write', () => {
  it('marks the row unwritten while it keeps showing the value in force', async () => {
    await openRows()
    fireEvent.change(control('limits.task_retry_limit'), { target: { value: '5' } })
    const retries = row('limits.task_retry_limit')
    expect(retries).toHaveAttribute('data-staged', 'true')
    expect(within(retries).getByText(T.not_written)).toBeInTheDocument()
    // The value in force is STILL on the row: collapsing the two would leave a
    // refused write showing the submitted value as though it were stored.
    expect(within(retries).getByText('7')).toBeInTheDocument()
    expect(within(retries).getByText(C.origin_app_config)).toBeInTheDocument()
    // And nothing has been written.
    expect(calls.some((call) => call.method === 'PUT')).toBe(false)
    expect(row('budget.warn_fraction')).toHaveAttribute('data-staged', 'false')
  })

  it('withdraws an edit that types back what the path already stores', async () => {
    // Every write is recorded, so staging the stored value would put a line in the
    // durable write record for a change nobody made.
    await openRows()
    const retries = control('limits.task_retry_limit')
    fireEvent.change(retries, { target: { value: '5' } })
    expect(unwritten()).toContain(T.unwritten_setting_changes)
    fireEvent.change(retries, { target: { value: '7' } })
    expect(unwritten()).toBe('')
    expect(row('limits.task_retry_limit')).toHaveAttribute('data-staged', 'false')
  })

  it('stages the same value at another scope, because that pins it', async () => {
    // The mirror of the withdrawal above: at a DIFFERENT path the stored value is a
    // real change, since it fixes the setting where the layer above cannot move it.
    await openRows()
    selectProject('acme')
    await waitFor(() => expect(scope('limits.task_retry_limit', 'project')).toBeEnabled())
    fireEvent.click(scope('limits.task_retry_limit', 'project'))
    // Through another value and back, because a control already showing 7 fires no
    // change event for a 7 — the browser's own behavior, not this form's.
    fireEvent.change(control('limits.task_retry_limit'), { target: { value: '9' } })
    fireEvent.change(control('limits.task_retry_limit'), { target: { value: '7' } })
    expect(unwritten()).toContain(T.unwritten_setting_changes)
    review()
    expect(
      within(block()).getByRole('heading', { name: T.the_change_that_would_be_written }),
    ).toBeVisible()
  })

  it('shows the exact patch and one sentence naming the old and new state', async () => {
    await openRows()
    fireEvent.change(control('limits.task_retry_limit'), { target: { value: '5' } })
    review()
    // The payload ITSELF, pretty-printed: a summary an operator approves is a
    // summary the write can differ from without anybody noticing.
    const patch = block().querySelector('pre.se-gpatch')
    expect(patch?.textContent).toBe(
      JSON.stringify({ limits: { task_retry_limit: 5 } }, null, 2),
    )
    expect(
      within(block()).getByText(
        T.edit_replaces_the_value_in_force
          .replace('{{setting}}', L.limits_task_retry_limit)
          .replace('{{oldValue}}', '7')
          .replace('{{origin}}', C.origin_app_config)
          .replace('{{newValue}}', '5')
          .replace('{{path}}', 'limits.task_retry_limit'),
      ),
    ).toBeInTheDocument()
  })

  it('stages a boolean and a string through their own controls', async () => {
    await openRows()
    fireEvent.click(control('delivery.auto_integrate'))
    fireEvent.change(control('notify.channel'), { target: { value: 'slack' } })
    review()
    confirm()
    await waitFor(() =>
      expect(putPatch()).toEqual({
        delivery: { auto_integrate: true },
        notify: { channel: 'slack' },
      }),
    )
  })

  it('drops every staged edit on discard, writing nothing', async () => {
    await openRows()
    fireEvent.change(control('limits.task_retry_limit'), { target: { value: '5' } })
    review()
    fireEvent.click(within(block()).getByRole('button', { name: T.discard_the_pending_changes }))
    expect(unwritten()).toBe('')
    expect(calls.some((call) => call.method === 'PUT')).toBe(false)
  })
})

describe('what a refusal and a success each leave behind', () => {
  it('keeps the staged edit and the stored values when the write door refuses', async () => {
    await openRows({
      put: {
        status: 422,
        body: { code: 'config_invalid', error: 'limits.task_retry_limit: must be at least 0' },
      },
    })
    fireEvent.change(control('limits.task_retry_limit'), { target: { value: '5' } })
    review()
    confirm()
    // The engine's own reason, against the path it names.
    expect(
      await within(block()).findByText(T.could_not_write_the_setting_change),
    ).toBeInTheDocument()
    expect(
      within(block()).getByText(/limits\.task_retry_limit: must be at least 0/),
    ).toBeInTheDocument()
    // Nothing was written, so the row still states what the store holds — and the
    // staged edit is still here to be corrected and sent again.
    expect(
      within(block()).getByText(T.nothing_was_written_so_the_rows_are_stored_state),
    ).toBeInTheDocument()
    expect(within(row('limits.task_retry_limit')).getByText('7')).toBeInTheDocument()
    expect(unwritten()).toContain(T.unwritten_setting_changes)
    expect(within(block()).queryByText(T.wrote_the_change_and_re_read_the_settings)).toBeNull()
  })

  it('re-renders every row from a fresh read after a successful write', async () => {
    // The obligation this form's mutation owns: `FormReview` is presentational and
    // cannot invalidate for its callers. Three readings describe what is now
    // stored — the document, the resolution, and the sources grid — and one
    // settings write can move all three.
    await openRows({
      resolvedAfterPut: {
        body: resolved({
          settings: [
            effective('limits.task_retry_limit', 5, {
              origin: 'app_config',
              declared_at: 'limits.task_retry_limit',
              is_default: false,
            }),
            effective('budget.warn_fraction', 0.8),
            effective('delivery.auto_integrate', false),
            effective('notify.channel', 'dashboard'),
            effective('watch.interval_s', 300),
            effective('concurrency.global_max_runs', 4),
          ],
        }),
      },
    })
    fireEvent.change(control('limits.task_retry_limit'), { target: { value: '5' } })
    review()
    const before = calls.length
    confirm()
    await within(block()).findByText(T.wrote_the_change_and_re_read_the_settings)
    // The row is not told what was sent; it re-renders from what the store now
    // answers, which is the only way the form and the store cannot disagree.
    await waitFor(() => expect(control('limits.task_retry_limit').value).toBe('5'))
    expect(row('limits.task_retry_limit')).toHaveAttribute('data-staged', 'false')
    const after = calls.slice(before).map((call) => call.url)
    expect(after.some((url) => url.startsWith('/api/apps/spec-engine/config/resolved'))).toBe(true)
    expect(after.some((url) => url === '/api/apps/spec-engine/config')).toBe(true)
    expect(after.some((url) => url.startsWith('/api/apps/spec-engine/config/sources'))).toBe(true)
  })
})

describe('a vocabulary this form cannot edit', () => {
  it('renders the read-only fallback and routes to the JSON view', async () => {
    await openRows({
      registry: {
        body: registryOneStage({
          settings: [
            {
              key: 'exotic.window',
              kind: 'duration',
              default: null,
              minimum: null,
              maximum: null,
              scopes: ['app'],
              summary: 'A kind this form has no control for.',
            },
          ],
        }),
      },
      resolved: {
        body: resolved({ settings: [effective('exotic.window', 'P1D')] }),
      },
    })
    // The row exists, states the kind it cannot edit, and says where to edit it.
    const exotic = row('exotic.window')
    expect(exotic).toBeInTheDocument()
    expect(
      within(exotic).getByText(
        T.the_registry_kind_is_not_editable_here.replace('{{kind}}', 'duration'),
      ),
    ).toBeInTheDocument()
    // Its stored value is still shown, with the origin that decided it, so the row
    // is a reading rather than a blank.
    expect(within(exotic).getByText('P1D')).toBeInTheDocument()
    // And no control at all: neither an input to type in nor a scope to write at,
    // because a form must never write a field it did not show.
    expect(within(exotic).queryByRole('textbox')).toBeNull()
    expect(within(exotic).queryByRole('spinbutton')).toBeNull()
    expect(within(exotic).queryByRole('checkbox')).toBeNull()
    expect(within(exotic).queryByRole('button')).toBeNull()
    expect(reviewControl()).toBeDisabled()
  })
})

describe('a failed read is doubt, not a form', () => {
  it('states the registry refusal and renders no control', async () => {
    await openConfig({
      registry: { status: 503, body: { code: 'app_disabled', error: 'the app is disabled' } },
    })
    expect(await screen.findByText(T.could_not_read_the_setting_registry)).toBeInTheDocument()
    expect(settingRows()).toHaveLength(0)
    expect(within(block()).queryByRole('button', { name: T.review_the_exact_change })).toBeNull()
  })

  it('states the resolution refusal rather than filling rows from defaults', async () => {
    // The registry carries every setting's DEFAULT. A form that fell back to it
    // would show a bundled value as what is in force, which is the one thing the
    // resolved read exists to distinguish.
    await openConfig({
      resolved: { status: 422, body: { code: 'config_invalid', error: 'out of range' } },
      registry: { body: registryOneStage() },
    })
    await waitFor(() =>
      expect(screen.getAllByText(C.could_not_resolve_the_configuration).length).toBeGreaterThan(0),
    )
    expect(settingRows()).toHaveLength(0)
  })
})

describe('the rows at their bundled default are collapsed', () => {
  it('renders only the settings that are not at their bundled default', async () => {
    // The whole point of the filter: an operator who changed one setting meets one
    // row, not six. Five of this fixture's six resolve to the bundled default.
    await openConfig()
    await waitFor(() => expect(settingRows().length).toBeGreaterThan(0))
    expect(settingRows()).toHaveLength(1)
    expect(row('limits.task_retry_limit')).toBeInTheDocument()
    // And the five are counted rather than silently absent, so the surface says
    // what it is not showing.
    const counted = within(block()).getByText(new RegExp(C.settings_at_their_bundled_default))
    expect(counted).toHaveTextContent('5')
    // Named to make the count non-vacuous: a defaulted key is nowhere on the row
    // list, not merely uncounted.
    expect(
      within(block()).queryByText('budget.warn_fraction', { selector: '.se-kv-path, .se-m' }),
    ).toBeNull()
  })

  it('shows every setting on request, without leaving the stage', async () => {
    await openConfig()
    await waitFor(() => expect(settingRows().length).toBeGreaterThan(0))
    const stage = screen.getByRole('tab', { name: new RegExp(`^${C.stage_execution}`) })
    expect(stage).toHaveAttribute('aria-selected', 'true')
    fireEvent.click(within(block()).getByRole('button', { name: showEverySettingLabel() }))
    expect(settingRows()).toHaveLength(6)
    expect(row('budget.warn_fraction')).toBeInTheDocument()
    // The same stage is still the active one: revealing is not a navigation, and a
    // control that moved the operator elsewhere to show six rows would have cost
    // them every staged edit on the stage they left.
    expect(screen.getByRole('tab', { name: new RegExp(`^${C.stage_execution}`) })).toHaveAttribute(
      'aria-selected',
      'true',
    )
    // And it goes back, so the collapsed reading is reachable again in place.
    fireEvent.click(
      within(block()).getByRole('button', { name: C.show_only_values_not_at_their_default }),
    )
    expect(settingRows()).toHaveLength(1)
  })

  it('keeps a row whose value equals the default because somebody pinned it there', async () => {
    // The reason the filter reads the ORIGIN and not only the value. A setting
    // pinned to a value that happens to equal the bundled default is a decision,
    // and it is distinguishable from an untouched setting by nothing else — hiding
    // it would hide the pin, and a later change to the bundled default would then
    // move a value the operator believed they had fixed.
    await openConfig({
      resolved: {
        body: resolved({
          settings: [
            effective('limits.task_retry_limit', 7, {
              origin: 'app_config',
              declared_at: 'limits.task_retry_limit',
              is_default: false,
            }),
            effective('budget.warn_fraction', 0.8, {
              origin: 'app_config',
              declared_at: 'budget.warn_fraction',
              is_default: false,
            }),
            effective('delivery.auto_integrate', false),
            effective('notify.channel', 'dashboard'),
            effective('watch.interval_s', 300),
            effective('concurrency.global_max_runs', 4),
          ],
        }),
      },
    })
    await waitFor(() => expect(settingRows().length).toBeGreaterThan(1))
    expect(settingRows()).toHaveLength(2)
    expect(row('budget.warn_fraction')).toBeInTheDocument()
    expect(
      within(block()).getByText(new RegExp(C.settings_at_their_bundled_default)),
    ).toHaveTextContent('4')
  })

  it('keeps a row whose in-force value disagrees with the declared default', async () => {
    // A payload claiming the bundled default over a value that is not the bundled
    // default is a payload disagreeing with itself, and the value in force is then
    // one nobody can account for. Shown, because that is the last thing to hide
    // from the person configuring the engine.
    await openConfig({
      resolved: {
        body: resolved({
          settings: [
            // Registry default is 300; the read claims 999 is the default.
            effective('watch.interval_s', 999),
            effective('limits.task_retry_limit', 2),
            effective('budget.warn_fraction', 0.8),
            effective('delivery.auto_integrate', false),
            effective('notify.channel', 'dashboard'),
            effective('concurrency.global_max_runs', 4),
          ],
        }),
      },
    })
    await waitFor(() => expect(settingRows().length).toBeGreaterThan(0))
    expect(settingRows()).toHaveLength(1)
    expect(row('watch.interval_s')).toBeInTheDocument()
  })

  it('keeps a row holding a staged edit after the defaults are hidden again', async () => {
    // An edit whose row is not rendered is an edit no sentence describes, no confirm
    // clears and no reconciliation drops — and it would still reach the patch. So a
    // staged row is pinned visible until the edit is withdrawn or written.
    await openRows()
    fireEvent.change(control('watch.interval_s'), { target: { value: '90' } })
    expect(unwritten()).toContain(T.unwritten_setting_changes)
    fireEvent.click(
      within(block()).getByRole('button', { name: C.show_only_values_not_at_their_default }),
    )
    // The configured row and the staged one, and nothing else.
    expect(settingRows()).toHaveLength(2)
    expect(row('watch.interval_s')).toHaveAttribute('data-staged', 'true')
    expect(unwritten()).toContain(T.unwritten_setting_changes)
    // Withdrawn, the row goes back to being one of the collapsed defaults. The
    // withdrawal is emptying the control rather than typing 300 back: at app scope
    // the bundled default is not what the path stores, so typing it PINS the
    // setting there, which is a real change and stays staged.
    fireEvent.change(control('watch.interval_s'), { target: { value: '' } })
    expect(settingRows()).toHaveLength(1)
    expect(unwritten()).toBe('')
  })

  it('states that every setting is at its default rather than showing nothing', async () => {
    await openConfig({
      resolved: {
        body: resolved({
          settings: [
            effective('limits.task_retry_limit', 2),
            effective('budget.warn_fraction', 0.8),
            effective('delivery.auto_integrate', false),
            effective('notify.channel', 'dashboard'),
            effective('watch.interval_s', 300),
            effective('concurrency.global_max_runs', 4),
          ],
        }),
      },
    })
    expect(
      await within(block()).findByText(C.every_setting_is_at_its_bundled_default),
    ).toBeInTheDocument()
    expect(settingRows()).toHaveLength(0)
    // Distinct from the empty vocabulary: the engine registers six settings here.
    expect(within(block()).queryByText(T.no_setting_is_registered)).toBeNull()
    // And they are all reachable, which is what makes this a collapse and not a loss.
    fireEvent.click(within(block()).getByRole('button', { name: showEverySettingLabel() }))
    expect(settingRows()).toHaveLength(6)
  })
})

describe('where a value came from is a per-row disclosure', () => {
  it('puts the origin and the declaring path behind a disclosure, value outside', async () => {
    await openRows()
    const retries = row('limits.task_retry_limit')
    const disclosure = retries.querySelector('details.se-disc')
    expect(disclosure).not.toBeNull()
    // Closed by default: three resolution lines on every one of twenty-one rows is
    // what made this pane read as a registry dump.
    expect((disclosure as HTMLDetailsElement).open).toBe(false)
    expect(within(disclosure as HTMLElement).getByText(C.origin_app_config)).toBeInTheDocument()
    // The declaring path, which shares its element with the separator, so it is read
    // off the element rather than matched as whole text.
    const declared = (disclosure as HTMLElement).querySelector('.se-src')
    expect(declared).not.toBeNull()
    expect(declared).toHaveTextContent('limits.task_retry_limit')
    // The bundled default is in there too, so "at its bundled default" is a claim a
    // reader can check rather than one this surface merely asserts.
    expect(disclosure?.textContent).toContain(C.origin_bundled_default)
    expect(
      within(disclosure as HTMLElement).getByText('2', { selector: '.se-m' }),
    ).toBeInTheDocument()
    // The value in force stays OUTSIDE the disclosure: once a staged edit occupies
    // the control it is the only thing on screen distinguishing 5 staged from 5
    // stored.
    const inForce = within(retries).getByText(new RegExp(T.in_force))
    expect(inForce).toHaveTextContent('7')
    expect(disclosure?.contains(inForce)).toBe(false)
  })

  it('expands in place, drawing nothing over the page', async () => {
    await openRows()
    const disclosure = row('limits.task_retry_limit').querySelector(
      'details.se-disc',
    ) as HTMLDetailsElement
    // A `<details>` rather than a popup, for the reason every disclosure on this
    // pane is one: the strip carrying the kill switch must never be covered.
    expect(disclosure.tagName).toBe('DETAILS')
    expect(disclosure.style.position).toBe('')
    const summary = within(disclosure).getByText(T.where_the_value_comes_from)
    expect(summary.tagName).toBe('SUMMARY')
  })

  it('never renders the engine’s own origin identifier as user-facing text', async () => {
    await openRows()
    const text = block().textContent ?? ''
    for (const identifier of [
      'bundled_default',
      'app_config',
      'cost_profile',
      'project_config',
      'source_config',
    ]) {
      expect(text, identifier).not.toContain(identifier)
    }
    // Non-vacuous: the plain-language readings of two of those origins ARE on
    // screen, so the absence above is about the wire tokens and not about an empty
    // surface.
    expect(text).toContain(C.origin_app_config)
    expect(text).toContain(C.origin_bundled_default)
  })
})

describe('the operator-facing strings', () => {
  it('ship in all thirteen catalogs with the same keys', async () => {
    const catalogs: Array<[string, Record<string, unknown>]> = [
      ['bn', bn.apps.specEngine.settingsForm],
      ['de', de.apps.specEngine.settingsForm],
      ['en', en.apps.specEngine.settingsForm],
      ['es', es.apps.specEngine.settingsForm],
      ['fr', fr.apps.specEngine.settingsForm],
      ['hi', hi.apps.specEngine.settingsForm],
      ['it', itIT.apps.specEngine.settingsForm],
      ['ja', ja.apps.specEngine.settingsForm],
      ['ko', ko.apps.specEngine.settingsForm],
      ['pt', pt.apps.specEngine.settingsForm],
      ['ru', ru.apps.specEngine.settingsForm],
      ['zh-CN', zh.apps.specEngine.settingsForm],
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
      apps: { specEngine: { settingsForm: Record<string, unknown> } }
    }
    expect(Object.keys(pseudo.apps.specEngine.settingsForm).sort()).toEqual(expected)
  })

  it('interpolates every placeholder its call site supplies', () => {
    // A placeholder nobody fills renders as `{{name}}` on screen, and a call site
    // filling one the string does not carry silently drops the value.
    expect(T.scope_to_write_setting_at).toContain('{{setting}}')
    expect(T.the_registry_kind_is_not_editable_here).toContain('{{kind}}')
    for (const name of ['setting', 'oldValue', 'newValue', 'origin', 'path']) {
      expect(T.edit_replaces_the_value_in_force).toContain(`{{${name}}}`)
    }
  })
})
