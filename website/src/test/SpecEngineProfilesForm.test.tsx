/**
 * The cost-profile form: role assignments, pinned limits, and the honest copy
 * about an effort that cannot take effect.
 *
 * Every property here is a correctness claim rather than a rendering preference:
 *
 *   - **The rows are the engine's vocabularies.** One row per role the registry
 *     read supplies, the effort ladder from the same read, and the pinned limits
 *     from the keys the engine says a profile may pin — typed by the same registry
 *     records the settings form is generated from. A list kept here is how a form
 *     comes to offer a role or an effort the write door refuses.
 *   - **The effort-on-`auto` rule is stated where the effort is pinned.** kiro-cli
 *     accepts no reasoning effort on `auto`, so the resolver DROPS a pinned effort
 *     and records having dropped it. The sentence appears and disappears with the
 *     model value, because it is a statement about that value.
 *   - **The profile is shared, and the form says so with the document's count.**
 *     Editing a role here changes every project that selected the profile.
 *   - **An add is a copy with provenance.** The staged entry is byte-equal to the
 *     bundled preset or profile it came from, and the review sentence names which.
 *     An empty profile is never offered: it would resolve every role to the session
 *     default while reporting that a profile is selected.
 *   - **A removal a project would feel is refused, naming the projects.** A
 *     `disabled` control with no reason would leave the operator no next action,
 *     and the next action is to point those projects elsewhere.
 *   - **Staging is the shared machinery.** A staged removal and a staged edit
 *     inside the same profile cannot both reach one patch, so the second drops the
 *     first — otherwise the card would describe a change the write does not carry.
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

const T = en.apps.specEngine.profilesForm
const C = en.apps.specEngine.configPanel
const P = en.apps.specEngine.specEnginePage
const L = C.setting_labels

type Answer = { status?: number; body: unknown }

/** Every request the page made, so an assertion can read the body that was sent. */
const calls: Array<{ url: string; method: string; body: unknown }> = []

/** The bundled preset entries, in `cost_profile_presets`' own shape. */
const QUALITY_FIRST = {
  roles: {
    design: { model: 'auto', effort: 'high' },
    review: { model: 'auto', effort: 'high' },
    implement: { model: 'auto', effort: 'medium' },
  },
  concurrency: { wave_max_tasks: 5 },
  budget: { run_ceiling_credits: 20 },
}

const BUDGET = {
  roles: {
    design: { model: 'auto', effort: 'low' },
    review: { model: 'auto', effort: 'medium' },
    implement: { model: 'auto', effort: 'low' },
  },
  concurrency: { wave_max_tasks: 1 },
  budget: { run_ceiling_credits: 2 },
}

/**
 * The registry payload, in `_registry_payload`'s shape.
 *
 * The two pinnable keys are the engine's own, with its own bounds and summaries, so
 * a row asserted here is a row an operator would meet. The role and effort
 * vocabularies are trimmed: what matters is that the rows come FROM them.
 */
function registry(over: Record<string, unknown> = {}) {
  return {
    settings: [
      {
        key: 'concurrency.wave_max_tasks',
        kind: 'int',
        default: 3,
        minimum: 1,
        maximum: null,
        scopes: ['app', 'project'],
        summary: 'Leaf tasks the orchestrator dispatches in parallel within one wave.',
      },
      {
        key: 'budget.run_ceiling_credits',
        kind: 'float',
        default: 5,
        minimum: 0,
        maximum: null,
        scopes: ['app', 'project'],
        summary: 'Credits one run may consume before it is halted.',
      },
    ],
    source_presets: [],
    profile_presets: [
      { name: 'quality-first', entry: QUALITY_FIRST },
      { name: 'budget', entry: BUDGET },
    ],
    profile_settings: ['concurrency.wave_max_tasks', 'budget.run_ceiling_credits'],
    roles: ['design', 'review', 'implement'],
    efforts: ['low', 'medium', 'high'],
    levels: ['authoring', 'execution'],
    ...over,
  }
}

/**
 * The stored document.
 *
 * `thrifty` sorts first, so it is what the form selects with no interaction. Two
 * projects select it and none selects `zesty`, which is the pair of cases the
 * removal guard turns on. `design` names a concrete model and `review` rides on
 * `auto`, which is the pair the effort-on-auto sentence turns on.
 */
function stored() {
  return {
    cost_profiles: {
      thrifty: {
        roles: {
          design: { model: 'claude-x', effort: 'low' },
          review: { model: 'auto', effort: 'high' },
        },
        concurrency: { wave_max_tasks: 2 },
      },
      zesty: { roles: { design: { model: 'auto' } } },
    },
    projects: {
      acme: { path: '/src/acme', cost_profile: 'thrifty' },
      widgets: { path: '/src/widgets', cost_profile: 'thrifty' },
      solo: { path: '/src/solo' },
    },
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

/** The resolved read. Answered so the pane beside this form renders at all. */
function resolved() {
  return {
    configured: true,
    project: null,
    source: null,
    settings: [
      {
        key: 'concurrency.wave_max_tasks',
        value: 3,
        origin: 'bundled_default',
        declared_at: '',
        is_default: true,
      },
      {
        key: 'budget.run_ceiling_credits',
        value: 5,
        origin: 'bundled_default',
        declared_at: '',
        is_default: true,
      },
    ],
    roles: { profile: '', roles: {} },
    role_order: [],
  }
}

function stub(answers: {
  registry?: Answer
  config?: Answer
  /** The config read once a PUT has landed, as the store would then answer it. */
  configAfterPut?: Answer
  put?: Answer
}) {
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
        // Answered BEFORE the generic '/config' prefix below, which would otherwise
        // hand this read a ConfigSnapshot and crash the form's render.
        answer = answers.registry ?? { body: registry() }
      } else if (url.startsWith('/api/apps/spec-engine/config/resolved')) {
        answer = { body: resolved() }
      } else if (url.startsWith('/api/apps/spec-engine/config/sources')) {
        answer = {
          body: {
            sources: [],
            submitter_classes: ['maintainer', 'external'],
            spec_types: ['feature'],
            levels: ['authoring', 'execution'],
          },
        }
      } else if (url.startsWith('/api/apps/spec-engine/config')) {
        answer =
          (written ? answers.configAfterPut : undefined) ??
          answers.config ?? { body: snapshot(stored()) }
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

/** Render the page and switch to the configuration pane. */
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
  await screen.findByRole('heading', { name: T.cost_profiles })
  return client
}

/** Open the pane and wait until the generated role rows are on screen. */
async function openRows(answers: Parameters<typeof stub>[0] = {}) {
  const client = await openConfig(answers)
  await waitFor(() => expect(block().querySelectorAll('.se-setting').length).toBeGreaterThan(1))
  return client
}

/**
 * The cost-profiles block.
 *
 * Every query is scoped to it: the settings form above renders the same registry
 * labels and the same review-card words, and the resolved pane beside it renders
 * the same role names — that is the point of the pane — so an unscoped query could
 * not tell the three surfaces apart.
 */
function block(): HTMLElement {
  const heading = screen.getByRole('heading', { name: T.cost_profiles })
  const found = heading.closest('.se-blk')
  expect(found).not.toBeNull()
  return found as HTMLElement
}

/** One role's row, addressed by the role the engine named. */
function roleRow(role: string): HTMLElement {
  const found = block().querySelector(`.se-setting[data-role="${role}"]`)
  expect(found, `no row for ${role}`).not.toBeNull()
  return found as HTMLElement
}

/** The model control on one role's row. */
function modelInput(role: string): HTMLInputElement {
  return within(roleRow(role)).getByRole('textbox') as HTMLInputElement
}

/** The effort button group on one role's row. */
function effortGroup(role: string): HTMLElement {
  return within(roleRow(role)).getByRole('group', {
    name: T.effort_for_role.replace('{{role}}', role),
  })
}

/** One pinned limit's row, addressed by the path it writes. */
function pinnedRow(key: string, profile = 'thrifty'): HTMLElement {
  // The label's detail line, which is the only place the path renders before an
  // edit is staged; the staged mark renders it again, so the selector is narrow.
  const path = within(block()).getByText(`cost_profiles.${profile}.${key}`, {
    selector: '.se-kv-path',
  })
  const found = path.closest('.se-setting')
  expect(found).not.toBeNull()
  return found as HTMLElement
}

/** Select one profile from the picker. */
function selectProfile(name: string) {
  const picker = within(block()).getByRole('group', { name: T.select_a_cost_profile })
  fireEvent.click(within(picker).getByRole('button', { name }))
}

/** Name the profile the add block would create. */
function nameTheAdd(name: string) {
  fireEvent.change(within(block()).getByLabelText(T.name_for_the_new_profile), {
    target: { value: name },
  })
}

/** The button that copies a bundled preset. */
function presetButton(name: string): HTMLElement {
  const group = within(block()).getByRole('group', { name: T.copy_a_bundled_preset })
  return within(group).getByRole('button', { name })
}

/** The button that copies an existing profile. */
function profileCopyButton(name: string): HTMLElement {
  const group = within(block()).getByRole('group', { name: T.copy_an_existing_profile })
  return within(group).getByRole('button', { name })
}

/** The control that opens the review card, whatever its state. */
function reviewControl(): HTMLElement {
  return within(block()).getByRole('button', { name: T.review_the_exact_change })
}

/** Open the review card, having staged at least one change. */
function review() {
  fireEvent.click(reviewControl())
}

/** Confirm the review card, which is the only thing here that writes. */
function confirm() {
  fireEvent.click(within(block()).getByRole('button', { name: T.write_the_change }))
}

/** The count of unwritten changes, or `''` when the form shows none. */
function unwritten(): string {
  const found = within(block()).queryByText(new RegExp(T.unwritten_profile_changes))
  return found ? (found.textContent ?? '') : ''
}

/** The patch the one PUT carried. */
function putPatch(): unknown {
  const put = calls.filter((call) => call.method === 'PUT')
  expect(put).toHaveLength(1)
  return (put[0].body as { patch: unknown }).patch
}

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  calls.length = 0
})

describe('the rows are the engine\u2019s own vocabularies', () => {
  it('renders one row per role, the model defaulting to auto and the effort ladder beside it', async () => {
    await openRows()
    // One row per role in the vocabulary, including the role this profile has not
    // assigned — which is precisely the one an operator came here to assign.
    expect(block().querySelectorAll('.se-setting[data-role]')).toHaveLength(3)
    expect(modelInput('design').value).toBe('claude-x')
    expect(modelInput('review').value).toBe('auto')
    // Nothing stored, so the engine's own unpinned default is what a write would
    // store and therefore what the control shows.
    expect(modelInput('implement').value).toBe('auto')
    // The ladder is the read's, and the button pressed is what the profile stores.
    const efforts = within(effortGroup('review')).getAllByRole('button')
    expect(efforts.map((button) => button.textContent)).toEqual(['low', 'medium', 'high'])
    expect(within(effortGroup('review')).getByRole('button', { name: 'high' })).toHaveAttribute(
      'aria-pressed',
      'true',
    )
    // And the row states what is stored, so a staged edit can be read against it.
    expect(within(roleRow('design')).getByText('claude-x')).toBeInTheDocument()
  })

  it('renders the pinned limits from the registry, carrying its own bounds', async () => {
    await openRows()
    const wave = within(pinnedRow('concurrency.wave_max_tasks')).getByRole('spinbutton')
    expect(wave).toHaveAttribute('min', '1')
    expect(wave).not.toHaveAttribute('max')
    expect(wave).toHaveAttribute('step', '1')
    expect((wave as HTMLInputElement).value).toBe('2')
    // The engine's own summary, not a second sentence kept in the form.
    expect(
      within(pinnedRow('concurrency.wave_max_tasks')).getByText(
        'Leaf tasks the orchestrator dispatches in parallel within one wave.',
      ),
    ).toBeInTheDocument()
    // A float steps by any amount, and a limit this profile does not pin is an
    // empty control rather than the registry default presented as pinned.
    const ceiling = within(pinnedRow('budget.run_ceiling_credits')).getByRole('spinbutton')
    expect(ceiling).toHaveAttribute('step', 'any')
    expect((ceiling as HTMLInputElement).value).toBe('')
    // Prose leads, and the path stays on screen as the detail line.
    expect(within(block()).getByText(L.budget_run_ceiling_credits)).toBeInTheDocument()
  })

  it('states an empty role vocabulary rather than rendering an empty form', async () => {
    await openConfig({ registry: { body: registry({ roles: [] }) } })
    expect(await within(block()).findByText(T.no_role_is_registered)).toBeInTheDocument()
    expect(block().querySelectorAll('.se-setting[data-role]')).toHaveLength(0)
  })

  it('says no profile is defined rather than showing unassignable roles', async () => {
    await openConfig({ config: { body: snapshot({ projects: {} }) } })
    expect(await within(block()).findByText(T.no_cost_profile_is_defined)).toBeInTheDocument()
    expect(block().querySelectorAll('.se-setting[data-role]')).toHaveLength(0)
    // The add block is still there, because it is the answer to the sentence.
    expect(within(block()).getByLabelText(T.name_for_the_new_profile)).toBeInTheDocument()
  })
})

describe('the effort-on-auto rule is stated where the effort is pinned', () => {
  it('shows the inert-effort sentence for a model of auto and not for a concrete one', async () => {
    await openRows()
    expect(
      within(roleRow('review')).getByText(T.a_pinned_effort_is_inert_while_the_model_is_auto),
    ).toBeInTheDocument()
    // `design` names a concrete model, so its pinned effort DOES take effect and
    // the sentence would be false on that row.
    expect(
      within(roleRow('design')).queryByText(T.a_pinned_effort_is_inert_while_the_model_is_auto),
    ).toBeNull()
  })

  it('drops the sentence when a concrete model is typed and brings it back with auto', async () => {
    await openRows()
    fireEvent.change(modelInput('review'), { target: { value: 'claude-y' } })
    expect(
      within(roleRow('review')).queryByText(T.a_pinned_effort_is_inert_while_the_model_is_auto),
    ).toBeNull()
    // Back to the unpinned model, and the rule applies again: the sentence tracks
    // the value the control shows, not the value the store holds.
    fireEvent.change(modelInput('review'), { target: { value: 'auto' } })
    expect(
      within(roleRow('review')).getByText(T.a_pinned_effort_is_inert_while_the_model_is_auto),
    ).toBeInTheDocument()
  })

  it('states, once, that an effort with no model would be refused', async () => {
    await openRows()
    expect(
      within(block()).getByText(T.an_effort_needs_a_model.replace('{{model}}', 'auto')),
    ).toBeInTheDocument()
  })
})

describe('the profile is shared with every project that selected it', () => {
  it('states the consequence with the count from the document', async () => {
    await openRows()
    expect(
      within(block()).getByText(
        T.the_values_apply_to_every_project.replace('{{profile}}', 'thrifty').replace('{{count}}', '2'),
      ),
    ).toBeInTheDocument()
    // A profile nothing selects says so with its own count rather than borrowing
    // the previous one.
    selectProfile('zesty')
    expect(
      within(block()).getByText(
        T.the_values_apply_to_every_project.replace('{{profile}}', 'zesty').replace('{{count}}', '0'),
      ),
    ).toBeInTheDocument()
  })
})

describe('a staged edit is not a write', () => {
  it('marks the row unwritten while it keeps showing what the profile stores', async () => {
    await openRows()
    fireEvent.change(modelInput('design'), { target: { value: 'claude-z' } })
    expect(roleRow('design')).toHaveAttribute('data-staged', 'true')
    expect(within(roleRow('design')).getByText(T.not_written)).toBeInTheDocument()
    // The stored value is still on the row: collapsing the two would leave a
    // refused write showing the submitted value as though it were stored.
    expect(within(roleRow('design')).getByText('claude-x')).toBeInTheDocument()
    expect(unwritten()).toContain(T.unwritten_profile_changes)
    expect(calls.some((call) => call.method === 'PUT')).toBe(false)
  })

  it('writes only the staged leaves, at the engine\u2019s own paths', async () => {
    await openRows()
    fireEvent.change(modelInput('design'), { target: { value: 'claude-z' } })
    fireEvent.click(within(effortGroup('review')).getByRole('button', { name: 'medium' }))
    fireEvent.change(
      within(pinnedRow('concurrency.wave_max_tasks')).getByRole('spinbutton'),
      { target: { value: '4' } },
    )
    review()
    confirm()
    await waitFor(() => expect(calls.some((call) => call.method === 'PUT')).toBe(true))
    expect(putPatch()).toEqual({
      cost_profiles: {
        thrifty: {
          roles: { design: { model: 'claude-z' }, review: { effort: 'medium' } },
          concurrency: { wave_max_tasks: 4 },
        },
      },
    })
  })

  it('withdraws an edit that types back what the profile already stores', async () => {
    await openRows()
    fireEvent.change(modelInput('design'), { target: { value: 'claude-z' } })
    expect(unwritten()).toContain(T.unwritten_profile_changes)
    // Every write is recorded, so re-entering the stored value must not queue one.
    fireEvent.change(modelInput('design'), { target: { value: 'claude-x' } })
    expect(unwritten()).toBe('')
    expect(roleRow('design')).toHaveAttribute('data-staged', 'false')
  })

  it('stages the default model with an effort pinned on an unassigned role', async () => {
    await openRows()
    // `implement` has no assignment at all, and the write door refuses an
    // assignment with no model — so the model is staged with the effort, visibly,
    // rather than being written silently or refused after a confirm.
    fireEvent.click(within(effortGroup('implement')).getByRole('button', { name: 'high' }))
    review()
    expect(
      within(block()).getByText(
        T.edit_replaces_the_role_model
          .replace('{{role}}', 'implement')
          .replace('{{profile}}', 'thrifty')
          .replace('{{oldValue}}', '\u2014')
          .replace('{{newValue}}', 'auto')
          .replace('{{path}}', 'cost_profiles.thrifty.roles.implement.model'),
      ),
    ).toBeInTheDocument()
    confirm()
    await waitFor(() => expect(calls.some((call) => call.method === 'PUT')).toBe(true))
    expect(putPatch()).toEqual({
      cost_profiles: { thrifty: { roles: { implement: { effort: 'high', model: 'auto' } } } },
    })
  })

  it('shows the exact patch and one sentence naming the old and the new state', async () => {
    await openRows()
    fireEvent.click(within(effortGroup('review')).getByRole('button', { name: 'low' }))
    review()
    expect(
      within(block()).getByText(
        T.edit_replaces_the_role_effort
          .replace('{{role}}', 'review')
          .replace('{{profile}}', 'thrifty')
          .replace('{{oldValue}}', 'high')
          .replace('{{newValue}}', 'low')
          .replace('{{path}}', 'cost_profiles.thrifty.roles.review.effort'),
      ),
    ).toBeInTheDocument()
    // The payload ITSELF, pretty-printed: a summary an operator approves is a
    // summary the write can differ from without anybody noticing.
    const shown = within(block()).getByText(/"cost_profiles"/)
    expect(JSON.parse(shown.textContent ?? '')).toEqual({
      cost_profiles: { thrifty: { roles: { review: { effort: 'low' } } } },
    })
  })

  it('drops every staged change on discard, writing nothing', async () => {
    await openRows()
    fireEvent.change(modelInput('design'), { target: { value: 'claude-z' } })
    review()
    fireEvent.click(within(block()).getByRole('button', { name: T.discard_the_pending_changes }))
    expect(unwritten()).toBe('')
    expect(calls.some((call) => call.method === 'PUT')).toBe(false)
  })
})

describe('adding a profile is always a copy', () => {
  it('stages the bundled preset byte-for-byte and names it as the provenance', async () => {
    await openRows()
    nameTheAdd('careful')
    fireEvent.click(presetButton('quality-first'))
    review()
    expect(
      within(block()).getByText(
        T.edit_copies_the_bundled_preset
          .replace('{{profile}}', 'careful')
          .replace('{{preset}}', 'quality-first')
          .replace('{{path}}', 'cost_profiles.careful'),
      ),
    ).toBeInTheDocument()
    confirm()
    await waitFor(() => expect(calls.some((call) => call.method === 'PUT')).toBe(true))
    // Byte-equal to the preset the read supplied: the form composes no assignment
    // of its own, so what is written is the engine's own table.
    expect(putPatch()).toEqual({ cost_profiles: { careful: QUALITY_FIRST } })
  })

  it('stages a copy of an existing profile and names that profile instead', async () => {
    await openRows()
    nameTheAdd('thriftier')
    fireEvent.click(profileCopyButton('thrifty'))
    review()
    expect(
      within(block()).getByText(
        T.edit_copies_the_existing_profile
          .replace('{{profile}}', 'thriftier')
          .replace('{{preset}}', 'thrifty')
          .replace('{{path}}', 'cost_profiles.thriftier'),
      ),
    ).toBeInTheDocument()
    confirm()
    await waitFor(() => expect(calls.some((call) => call.method === 'PUT')).toBe(true))
    expect(putPatch()).toEqual({
      cost_profiles: { thriftier: stored().cost_profiles.thrifty },
    })
  })

  it('refuses a name a profile already has, rather than merging into it', async () => {
    await openRows()
    nameTheAdd('thrifty')
    expect(
      within(block()).getByText(T.the_name_is_already_a_profile.replaceAll('{{profile}}', 'thrifty')),
    ).toBeInTheDocument()
    // Disabled rather than silently merging: the store's merge would fold the copy
    // INTO the existing profile, which is an edit to it and not an add.
    expect(presetButton('quality-first')).toBeDisabled()
    expect(profileCopyButton('zesty')).toBeDisabled()
    expect(unwritten()).toBe('')
  })

  it('asks for a name before it offers anything to copy', async () => {
    await openRows()
    expect(within(block()).getByText(T.name_the_profile_first)).toBeInTheDocument()
    expect(presetButton('budget')).toBeDisabled()
  })

  it('moves a staged copy to the new name rather than dropping it', async () => {
    await openRows()
    nameTheAdd('careful')
    fireEvent.click(presetButton('budget'))
    // An operator who picked a preset and then reconsidered the name meant to keep
    // the copy — and the patch must address one path, not two.
    nameTheAdd('careful-2')
    review()
    confirm()
    await waitFor(() => expect(calls.some((call) => call.method === 'PUT')).toBe(true))
    expect(putPatch()).toEqual({ cost_profiles: { 'careful-2': BUDGET } })
  })
})

describe('removing a profile a project still selects', () => {
  it('refuses the removal and names the projects that select it', async () => {
    await openRows()
    fireEvent.click(
      within(block()).getByRole('button', {
        name: T.remove_the_profile.replace('{{profile}}', 'thrifty'),
      }),
    )
    expect(
      within(block()).getByText(
        T.a_project_still_selects_the_profile
          .replace('{{projects}}', 'acme, widgets')
          .replace('{{profile}}', 'thrifty'),
      ),
    ).toBeInTheDocument()
    // Nothing staged, so nothing to confirm: a removal that stranded a project
    // would leave it resolving every role to the session default.
    expect(unwritten()).toBe('')
    expect(reviewControl()).toBeDisabled()
  })

  it('stages the deletion and states what it deletes when no project selects it', async () => {
    await openRows()
    selectProfile('zesty')
    fireEvent.click(
      within(block()).getByRole('button', {
        name: T.remove_the_profile.replace('{{profile}}', 'zesty'),
      }),
    )
    review()
    // The blast radius is not legible in a `null`, so it is stated under the patch.
    expect(
      within(block()).getByText(
        T.removing_deletes_the_profile
          .replace('{{path}}', 'cost_profiles.zesty')
          .replace('{{profile}}', 'zesty'),
      ),
    ).toBeInTheDocument()
    confirm()
    await waitFor(() => expect(calls.some((call) => call.method === 'PUT')).toBe(true))
    // The store's own deletion spelling, at the profile's own path.
    expect(putPatch()).toEqual({ cost_profiles: { zesty: null } })
  })

  it('drops an edit inside the profile when its removal is staged', async () => {
    await openRows()
    selectProfile('zesty')
    fireEvent.change(modelInput('design'), { target: { value: 'claude-z' } })
    expect(unwritten()).toContain(T.unwritten_profile_changes)
    fireEvent.click(
      within(block()).getByRole('button', {
        name: T.remove_the_profile.replace('{{profile}}', 'zesty'),
      }),
    )
    review()
    // The patch is last-edit-wins over overlapping paths, so an ancestor and a
    // descendant cannot both survive it. The card must therefore describe one
    // change, not two — otherwise the operator confirms an edit that never lands.
    expect(within(block()).getAllByText(/cost_profiles/, { selector: 'p.se-note' })).toHaveLength(1)
    confirm()
    await waitFor(() => expect(calls.some((call) => call.method === 'PUT')).toBe(true))
    expect(putPatch()).toEqual({ cost_profiles: { zesty: null } })
  })
})

describe('what a refusal and a success each leave behind', () => {
  it('keeps the staged change and the stored values when the write door refuses', async () => {
    await openRows({
      put: {
        status: 422,
        body: {
          code: 'config_invalid',
          error: 'cost_profiles.thrifty.roles.design.model: expected a non-empty string',
        },
      },
    })
    fireEvent.change(modelInput('design'), { target: { value: 'claude-z' } })
    review()
    confirm()
    expect(
      await within(block()).findByText(T.could_not_write_the_profile_change),
    ).toBeInTheDocument()
    // The engine's own reason, against the path it names.
    expect(
      within(block()).getByText(/cost_profiles\.thrifty\.roles\.design\.model: expected/),
    ).toBeInTheDocument()
    expect(
      within(block()).getByText(T.nothing_was_written_so_the_profile_is_stored_state),
    ).toBeInTheDocument()
    // Nothing was written, so the row still states what the store holds — and the
    // staged change is still here to be corrected and sent again.
    expect(within(roleRow('design')).getByText('claude-x')).toBeInTheDocument()
    expect(unwritten()).toContain(T.unwritten_profile_changes)
    expect(within(block()).queryByText(T.wrote_the_change_and_re_read_the_profiles)).toBeNull()
  })

  it('re-renders every row from a fresh read after a successful write', async () => {
    // The obligation this form's mutation owns: `FormReview` is presentational and
    // cannot invalidate for its callers. The document is where every row here comes
    // from, and the resolved read beside it renders the roles this write changes.
    const after = stored()
    after.cost_profiles.thrifty.roles.design.model = 'claude-z'
    await openRows({ configAfterPut: { body: snapshot(after) } })
    fireEvent.change(modelInput('design'), { target: { value: 'claude-z' } })
    review()
    const before = calls.length
    confirm()
    await within(block()).findByText(T.wrote_the_change_and_re_read_the_profiles)
    // The row is not told what was sent; it re-renders from what the store now
    // answers, which is the only way the form and the store cannot disagree.
    await waitFor(() => expect(modelInput('design').value).toBe('claude-z'))
    expect(roleRow('design')).toHaveAttribute('data-staged', 'false')
    const urls = calls.slice(before).map((call) => call.url)
    expect(urls.some((url) => url === '/api/apps/spec-engine/config')).toBe(true)
    expect(urls.some((url) => url.startsWith('/api/apps/spec-engine/config/resolved'))).toBe(true)
    expect(urls.some((url) => url.startsWith('/api/apps/spec-engine/config/sources'))).toBe(true)
  })
})

describe('a failed read is doubt, not a form', () => {
  it('states the vocabulary refusal and renders no role row', async () => {
    await openConfig({
      registry: { status: 503, body: { code: 'app_disabled', error: 'the app is disabled' } },
    })
    expect(
      await within(block()).findByText(T.could_not_read_the_profile_vocabulary),
    ).toBeInTheDocument()
    // Not one row, and no control that could write: the vocabulary IS the form.
    expect(block().querySelectorAll('.se-setting')).toHaveLength(0)
    expect(within(block()).queryByRole('button', { name: T.review_the_exact_change })).toBeNull()
  })
})

describe('the operator-facing strings', () => {
  it('ship in all thirteen catalogs with the same keys', async () => {
    const catalogs: Array<[string, Record<string, unknown>]> = [
      ['bn', bn.apps.specEngine.profilesForm],
      ['de', de.apps.specEngine.profilesForm],
      ['en', en.apps.specEngine.profilesForm],
      ['es', es.apps.specEngine.profilesForm],
      ['fr', fr.apps.specEngine.profilesForm],
      ['hi', hi.apps.specEngine.profilesForm],
      ['it', itIT.apps.specEngine.profilesForm],
      ['ja', ja.apps.specEngine.profilesForm],
      ['ko', ko.apps.specEngine.profilesForm],
      ['pt', pt.apps.specEngine.profilesForm],
      ['ru', ru.apps.specEngine.profilesForm],
      ['zh-CN', zh.apps.specEngine.profilesForm],
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
      apps: { specEngine: { profilesForm: Record<string, unknown> } }
    }
    expect(Object.keys(pseudo.apps.specEngine.profilesForm).sort()).toEqual(expected)
  })

  it('interpolates every placeholder its call site supplies', () => {
    // A placeholder nobody fills renders as `{{name}}` on screen, and a call site
    // filling one the string does not carry silently drops the value.
    for (const name of ['role']) {
      expect(T.model_for_role).toContain(`{{${name}}}`)
      expect(T.effort_for_role).toContain(`{{${name}}}`)
    }
    expect(T.an_effort_needs_a_model).toContain('{{model}}')
    expect(T.the_registry_kind_is_not_editable_here).toContain('{{kind}}')
    expect(T.remove_the_profile).toContain('{{profile}}')
    for (const name of ['profile', 'count']) {
      expect(T.the_values_apply_to_every_project).toContain(`{{${name}}}`)
    }
    for (const name of ['projects', 'profile']) {
      expect(T.a_project_still_selects_the_profile).toContain(`{{${name}}}`)
    }
    for (const name of ['profile', 'path']) {
      expect(T.removing_deletes_the_profile).toContain(`{{${name}}}`)
      expect(T.edit_removes_the_profile).toContain(`{{${name}}}`)
    }
    for (const name of ['profile', 'preset', 'path']) {
      expect(T.edit_copies_the_bundled_preset).toContain(`{{${name}}}`)
      expect(T.edit_copies_the_existing_profile).toContain(`{{${name}}}`)
    }
    for (const name of ['profile', 'role', 'oldValue', 'newValue', 'path']) {
      expect(T.edit_replaces_the_role_model).toContain(`{{${name}}}`)
      expect(T.edit_replaces_the_role_effort).toContain(`{{${name}}}`)
    }
    for (const name of ['profile', 'setting', 'oldValue', 'newValue', 'path']) {
      expect(T.edit_replaces_the_pinned_limit).toContain(`{{${name}}}`)
    }
    expect(T.the_name_is_already_a_profile).toContain('{{profile}}')
  })
})
