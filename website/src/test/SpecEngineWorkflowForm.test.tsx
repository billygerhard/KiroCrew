/**
 * The delivery workflow form: whose commands each stage runs, and how to define a
 * preset of your own.
 *
 * Every property asserted here is a correctness claim rather than a rendering
 * preference:
 *
 *   - **Each stage names the layer that supplied its commands, and the five answers
 *     stay five answers.** A bundled preset and a user-defined one are not
 *     flattened into "preset", and a stage nobody declares says so rather than
 *     being shown as preset-supplied — both are invariants of the engine's own
 *     display path, and the reason bundled names are reserved at all.
 *   - **The commands rendered are the resolved commands, argument by argument.**
 *     From `argv`, never from `commands`, which is a count.
 *   - **A stage outside the delivery flow says when it DOES run.** Teardown is
 *     executed by archive rather than in sequence, and the row says so from the
 *     route's own `runs_at` rather than from a table kept on this side.
 *   - **A stage with no commands says it takes no action** rather than being
 *     omitted, so "runs the preset's commands" and "runs nothing" are not told
 *     apart by a stage's absence.
 *   - **There is no reorder control.** The engine's stage order is fixed and no
 *     `order` key exists, so the stage list carries no control at all.
 *   - **A definition writes per stage.** Every path is
 *     `workflow.presets.<name>.stages.<stage>`, so the fenced `workflow` key is
 *     literally present in the patch and the confirmation card can flag it.
 *   - **A reserved name is refused and the typed commands survive it.** The bundled
 *     names come from the registry projection, so the refusal is the write door's
 *     own list.
 *   - **A removal a project would feel is refused, naming the projects.** A
 *     `disabled` control with no reason leaves no next action.
 *   - **A read failure states itself** rather than rendering an empty workflow.
 */
import { describe, it, expect, afterEach } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import SpecEnginePage from '../apps/spec-engine/SpecEnginePage'
import { QK } from '../apps/spec-engine/api'
import en from '../i18n/locales/en.json'

import {
  PIPELINE_STAGES,
  stubSpecEngineFetch,
  failure,
  expectEverySpecEngineRouteAnswered,
  type Answer,
} from './specEngineFetchStub'

const T = en.apps.specEngine.workflowForm
const C = en.apps.specEngine.configPanel
const P = en.apps.specEngine.specEnginePage

/** Every request the page made, so an assertion can read the body that was sent. */
const calls: Array<{ url: string; method: string; body: unknown }> = []

/** Interpolate one catalog sentence the way `i18nT` does, for an exact match. */
function copy(template: string, values: Record<string, string> = {}): string {
  return Object.entries(values).reduce(
    (text, [name, value]) => text.split(`{{${name}}}`).join(value),
    template,
  )
}

/** The registry projection, carrying the engine's own bundled preset names. */
function registry(over: Record<string, unknown> = {}) {
  return {
    settings: [],
    source_presets: [],
    profile_presets: [],
    profile_settings: [],
    roles: [],
    efforts: [],
    levels: [],
    stages: PIPELINE_STAGES,
    workflow_presets: ['git-pull-request', 'local-only'],
    // The engine's display cap on a preset name, which is what the definition field
    // refuses past. The number is the route's, not this suite's.
    workflow_preset_name_limit: 64,
    ...over,
  }
}

/**
 * The workflow read.
 *
 * Deliberately mixed: `submit` comes from the selected BUNDLED preset, `verify`
 * from a preset the operator defined, `publish` from a project override, `isolate`
 * from an app-wide override with an EMPTY command list, and `teardown` from
 * nothing. That covers all five sources, the no-command case, and a stage the
 * delivery flow does not run.
 */
function workflow(over: Record<string, unknown> = {}) {
  return {
    configured: true,
    project: null,
    preset: {
      name: 'git-pull-request',
      origin: 'app_config',
      declared_at: 'workflow.preset',
      bundled: true,
    },
    stages: [
      {
        stage: 'isolate',
        source: 'app_override',
        from_preset: false,
        bundled: false,
        preset: '',
        declared_at: 'workflow.stages.isolate',
        commands: 0,
        skipped: false,
        summary: 'isolate: overridden app-wide',
        argv: [],
        runs_at: 'isolation',
      },
      {
        stage: 'submit',
        source: 'bundled_preset',
        from_preset: true,
        bundled: true,
        preset: 'git-pull-request',
        declared_at: 'workflow.preset',
        commands: 2,
        skipped: false,
        summary: "submit: from bundled preset 'git-pull-request'",
        argv: [
          ['git', 'commit', '-m', 'a b'],
          ['git', 'push', '--set-upstream', 'origin', 'HEAD'],
        ],
        runs_at: 'delivery',
      },
      {
        stage: 'verify',
        source: 'user_preset',
        from_preset: true,
        bundled: false,
        preset: 'house-style',
        declared_at: 'workflow.presets.house-style.stages.verify',
        commands: 1,
        skipped: false,
        summary: "verify: from user-defined preset 'house-style'",
        argv: [['make', 'check']],
        runs_at: 'delivery',
      },
      {
        stage: 'publish',
        source: 'project_override',
        from_preset: false,
        bundled: false,
        preset: '',
        declared_at: 'projects.acme.workflow.stages.publish',
        commands: 1,
        skipped: false,
        summary: 'publish: overridden by this project',
        argv: [['gh', 'pr', 'merge', '--squash']],
        runs_at: 'delivery',
      },
      {
        stage: 'teardown',
        source: 'unconfigured',
        from_preset: false,
        bundled: false,
        preset: '',
        declared_at: '',
        commands: 0,
        skipped: true,
        summary: 'teardown: not configured, so this stage is skipped',
        argv: [],
        runs_at: 'archive',
      },
    ],
    user_presets: ['house-style', 'unused-preset'],
    delivery_flow_stages: ['submit', 'verify', 'publish'],
    gates_scope_is_app: true,
    gates: [],
    gates_unreadable: false,
    gate_errors: [],
    ...over,
  }
}

/**
 * The stored document.
 *
 * The two user-defined presets are DEFINED here as well as projected, because that
 * is the only way they can exist: `user_presets` is the route's reading of this very
 * section. The form takes the names it writes with from here rather than from the
 * route, so a fixture that declared them on one side only would be a document no
 * engine could have produced.
 *
 * `acme` selects `house-style`, so removing that preset must be refused and name
 * it; `unused-preset` is selected by nothing, so removing it must be offered.
 */
function stored() {
  return {
    workflow: {
      preset: 'git-pull-request',
      presets: {
        'house-style': { stages: { verify: [['make', 'check']] } },
        'unused-preset': { stages: { submit: [['true']] } },
      },
    },
    projects: {
      acme: { path: '/src/acme', workflow: { preset: 'house-style' } },
      solo: { path: '/src/solo' },
    },
  }
}

/**
 * A preset name past the display cap, and the capped rendering the route sends.
 *
 * The engine's own display path is `sanitized(name, limit)`, which appends a
 * truncation notice, so the two are different strings — which is the whole point:
 * everything this form writes has to be spelled the first way.
 */
const LONG_NAME = `long-${'x'.repeat(80)}`
const CAPPED_NAME = `${LONG_NAME.slice(0, 64)} [...]`

/** The document with {@link LONG_NAME} defined, and *selected* by whatever selects it. */
function storedWithLongName(selects: 'nothing' | 'app' | 'project' = 'nothing') {
  const doc = stored()
  doc.workflow.presets = {
    ...doc.workflow.presets,
    [LONG_NAME]: { stages: { submit: [['true']] } },
  } as (typeof doc)['workflow']['presets']
  if (selects === 'app') doc.workflow.preset = LONG_NAME
  if (selects === 'project') doc.projects.acme.workflow = { preset: LONG_NAME }
  return doc
}

/** The workflow read as the route composes it for {@link storedWithLongName}. */
function workflowWithLongName(over: Record<string, unknown> = {}) {
  // Capped, exactly as `_user_preset_names` sends it. A test that put the raw name
  // here would be testing a route that does not exist.
  return workflow({ user_presets: ['house-style', 'unused-preset', CAPPED_NAME], ...over })
}

function snapshot(doc: Record<string, unknown>, over: Record<string, unknown> = {}) {
  return {
    configured: true,
    path: '/home/me/.kiro/crew/apps/spec-engine/config.json',
    document: doc,
    elided: [],
    elided_marker: '<elided>',
    errors: [],
    advisories: [],
    config_only_paths: ['workflow', 'projects.*.workflow'],
    ...over,
  }
}

function resolved() {
  return {
    configured: true,
    project: null,
    source: null,
    settings: [],
    roles: { profile: '', roles: {} },
    role_order: [],
  }
}

function stub(answers: { registry?: Answer; workflow?: Answer; config?: Answer } = {}) {
  stubSpecEngineFetch(
    {
      registry: answers.registry ?? { body: registry() },
      resolved: { body: resolved() },
      workflow: answers.workflow ?? { body: workflow() },
      config: answers.config ?? { body: snapshot(stored()) },
    },
    { record: calls },
  )
}

/** Render the page, open the configuration pane, and switch to the delivery stage. */
async function openDelivery(answers: Parameters<typeof stub>[0] = {}) {
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
  // Only the ACTIVE panel is reachable: an inactive one carries `hidden`, which
  // takes it out of the accessibility tree the role queries read.
  fireEvent.click(screen.getByRole('tab', { name: new RegExp(`^${C.stage_delivery}`) }))
  await screen.findByRole('heading', { name: T.delivery_workflow })
  return client
}

/**
 * The workflow block.
 *
 * Every query is scoped to it: the settings surface above renders the same review
 * words and the resolved pane beside it renders the same paths, so an unscoped
 * query could not tell the three surfaces apart.
 */
function block(): HTMLElement {
  const heading = screen.getByRole('heading', { name: T.delivery_workflow })
  const found = heading.closest('.se-blk')
  expect(found).not.toBeNull()
  return found as HTMLElement
}

/** One stage's row, addressed by the stage the engine named. */
function stageRow(stage: string): HTMLElement {
  const found = block().querySelector(`.se-setting[data-stage="${stage}"]`)
  expect(found, `no row for ${stage}`).not.toBeNull()
  return found as HTMLElement
}

/** The argv rendered for one stage, argument by argument. */
function renderedArgv(stage: string): string[][] {
  const list = stageRow(stage).querySelector(`[data-stage-commands="${stage}"]`)
  if (!list) return []
  return [...list.querySelectorAll('li')].map((item) =>
    [...item.querySelectorAll('[data-argument]')].map((slot) => slot.textContent ?? ''),
  )
}

/**
 * What one stage's commands actually READ as, one line per command.
 *
 * The whole line's text, deliberately not each argument's own span: reading the
 * spans is blind to what separates them, so a rendering that painted
 * `gitcommit-ma b` would satisfy every per-argument assertion while showing an
 * operator a command with no visible argument boundaries at all.
 */
function renderedCommandLines(stage: string): string[] {
  const list = stageRow(stage).querySelector(`[data-stage-commands="${stage}"]`)
  if (!list) return []
  return [...list.querySelectorAll('li')].map((item) => item.textContent ?? '')
}

/** The draft command field for one stage. */
function draftField(stage: string): HTMLTextAreaElement {
  const row = block().querySelector(`.se-setting[data-draft-stage="${stage}"]`)
  expect(row, `no draft row for ${stage}`).not.toBeNull()
  return within(row as HTMLElement).getByRole('textbox') as HTMLTextAreaElement
}

/** The patch the review card would submit, parsed back from what it displays. */
function shownPatch(): Record<string, unknown> {
  const pre = block().querySelector('pre.se-gpatch')
  expect(pre).not.toBeNull()
  return JSON.parse((pre as HTMLElement).textContent ?? '{}') as Record<string, unknown>
}

/**
 * The unwritten-work count on the delivery tab, or `''` when it carries none.
 *
 * Read off the TAB rather than off the form's own line, because the tab is the only
 * place the count is visible once a read fails: the form's error branch states the
 * refusal instead of its rows. Only the pending count renders inside a stage tab —
 * the document's problem and advisory marks are the advanced stage's — so this is
 * that number and nothing else.
 */
function deliveryPending(): string {
  const tab = screen.getByRole('tab', { name: new RegExp(`^${C.stage_delivery}`) })
  return tab.querySelector('.se-filter-count')?.textContent ?? ''
}

afterEach(() => {
  cleanup()
  calls.length = 0
  // Nothing the page asked for went unanswered by the shared stub. Without this a
  // product URL can drift out from under the table and this suite still passes: the
  // stub's 599 refusal reaches the surface as an ordinary error, so a test whose
  // subject is a read failure renders the copy it asserts for either way.
  expectEverySpecEngineRouteAnswered()
})

describe('the workflow in force names its source per stage', () => {
  it('tells a bundled preset from one the operator defined', async () => {
    await openDelivery()
    // Two stages both came from a preset, and the two answers are DIFFERENT: that
    // separation is exactly what reserving bundled names buys, and flattening both
    // onto "from a preset" would give the ambiguity back.
    expect(within(stageRow('submit')).getByText(T.source_bundled_preset)).toBeTruthy()
    expect(within(stageRow('verify')).getByText(T.source_user_preset)).toBeTruthy()
    expect(stageRow('submit').dataset.source).toBe('bundled_preset')
    expect(stageRow('verify').dataset.source).toBe('user_preset')
  })

  it('names an app-wide override and a project override apart', async () => {
    await openDelivery()
    expect(within(stageRow('isolate')).getByText(T.source_app_override)).toBeTruthy()
    expect(within(stageRow('publish')).getByText(T.source_project_override)).toBeTruthy()
    // The declaring path travels with the row, because the two overrides are changed
    // in different places.
    expect(stageRow('publish').textContent).toContain('projects.acme.workflow.stages.publish')
  })

  it('never shows a stage nobody declares as preset-supplied', async () => {
    await openDelivery()
    const teardown = stageRow('teardown')
    expect(within(teardown).getByText(T.source_unconfigured)).toBeTruthy()
    expect(teardown.textContent).not.toContain(T.source_bundled_preset)
    expect(teardown.textContent).not.toContain(T.source_user_preset)
    // The selected preset's name is on the block, so the absence is asserted on the
    // ROW: an unconfigured stage must not carry it.
    expect(teardown.textContent).not.toContain('git-pull-request')
  })

  it('states whether the selected preset is bundled or the operator’s own', async () => {
    await openDelivery()
    expect(
      within(block()).getByText(
        copy(T.a_bundled_preset_is_selected, { preset: 'git-pull-request' }),
        { exact: false },
      ),
    ).toBeTruthy()
  })
})

describe('the stages render the commands the engine resolved', () => {
  it('renders each argument byte-equal to the payload’s argv', async () => {
    await openDelivery()
    // Argument by argument, including the one holding a space: a rendering that
    // joined the argv and re-split it would lose exactly that argument.
    expect(renderedArgv('submit')).toEqual([
      ['git', 'commit', '-m', 'a b'],
      ['git', 'push', '--set-upstream', 'origin', 'HEAD'],
    ])
    expect(renderedArgv('verify')).toEqual([['make', 'check']])
    expect(renderedArgv('publish')).toEqual([['gh', 'pr', 'merge', '--squash']])
  })

  it('separates the arguments it renders, and delimits one holding a space', async () => {
    await openDelivery()
    // Asserted on the LINE and not on the argument spans, because the spans cannot
    // see what lies between them: with no separator these same spans paint
    // `gitcommit-ma b`, and the argv the engine spawns with no shell is then
    // unreadable in exactly the surface that exists to show it.
    expect(renderedCommandLines('submit')).toEqual([
      'git commit -m "a b"',
      'git push --set-upstream origin HEAD',
    ])
    // And the delimiters are punctuation AROUND the token, never inside it: the
    // argument itself is still the payload's own bytes, unquoted and unescaped.
    expect(renderedArgv('submit')[0][3]).toBe('a b')
  })

  it('delimits an empty argument, which would otherwise not be there at all', async () => {
    const one = workflow()
    one.stages = [{ ...one.stages[1], argv: [['prog', '', 'after']] }]
    await openDelivery({ workflow: { body: one } })
    expect(renderedCommandLines('submit')).toEqual(['prog "" after'])
    expect(renderedArgv('submit')).toEqual([['prog', '', 'after']])
  })

  it('keeps the engine’s declared stage order and declares no order of its own', async () => {
    await openDelivery()
    const rows = [...block().querySelectorAll('.se-setting[data-stage]')]
    expect(rows.map((row) => (row as HTMLElement).dataset.stage)).toEqual([
      'isolate',
      'submit',
      'verify',
      'publish',
      'teardown',
    ])
  })

  it('offers no control at all on a stage row, so nothing can reorder them', async () => {
    await openDelivery()
    for (const stage of ['isolate', 'submit', 'verify', 'publish', 'teardown']) {
      const row = stageRow(stage)
      // The engine's order is fixed and no `order` key exists, so a control here
      // could only offer an edit the document cannot express.
      expect(row.querySelectorAll('button')).toHaveLength(0)
      expect(row.querySelectorAll('input, textarea, select')).toHaveLength(0)
    }
  })

  it('says what each stage is for', async () => {
    await openDelivery()
    expect(within(stageRow('submit')).getByText(T.purpose_submit)).toBeTruthy()
    expect(within(stageRow('publish')).getByText(T.purpose_publish)).toBeTruthy()
  })

  it('shows a stage with no commands as taking no action rather than omitting it', async () => {
    await openDelivery()
    // Both the unconfigured stage and the override that resolved to an EMPTY list:
    // the second is declared and still runs nothing, which a reader needs told.
    expect(within(stageRow('teardown')).getByText(T.this_stage_takes_no_action)).toBeTruthy()
    expect(within(stageRow('isolate')).getByText(T.this_stage_takes_no_action)).toBeTruthy()
  })

  it('says when a stage outside the delivery flow does run instead', async () => {
    await openDelivery()
    expect(within(stageRow('teardown')).getByText(T.runs_when_the_run_is_archived)).toBeTruthy()
    expect(
      within(stageRow('isolate')).getByText(T.runs_when_the_workspace_is_isolated),
    ).toBeTruthy()
    // A stage the flow DOES run says nothing extra: it is in the flow's own list.
    expect(stageRow('submit').textContent).not.toContain(T.runs_when_the_run_is_archived)
  })

  it('says the projection has no answer rather than implying a stage never runs', async () => {
    const grown = workflow()
    grown.stages = [
      { ...grown.stages[4], stage: 'notarise', runs_at: '', source: 'app_override' },
    ]
    grown.delivery_flow_stages = []
    await openDelivery({ workflow: { body: grown } })
    expect(
      within(stageRow('notarise')).getByText(T.runs_at_a_point_this_read_does_not_name),
    ).toBeTruthy()
  })
})

describe('the preset chooser offers every projected preset, bundled apart', () => {
  it('lists the engine’s bundled names and the operator’s own in separate groups', async () => {
    await openDelivery()
    const bundled = within(block()).getByRole('group', { name: T.presets_bundled_with_the_app })
    const mine = within(block()).getByRole('group', { name: T.presets_you_defined })
    expect(
      [...bundled.querySelectorAll('button')].map((button) => button.textContent),
    ).toEqual(['git-pull-request', 'local-only'])
    expect([...mine.querySelectorAll('button')].map((button) => button.textContent)).toEqual([
      'house-style',
      'unused-preset',
    ])
  })

  it('stages the selection at one leaf path, never a wholesale replacement', async () => {
    await openDelivery()
    const bundled = within(block()).getByRole('group', { name: T.presets_bundled_with_the_app })
    fireEvent.click(within(bundled).getByRole('button', { name: 'local-only' }))
    fireEvent.click(within(block()).getByRole('button', { name: T.review_the_exact_change }))
    // `workflow` is present as a KEY holding one leaf, which is what lets the
    // confirmation card's fence matcher find it. A patch replacing the whole
    // `workflow` object would carry the same write with nothing flagging it.
    expect(shownPatch()).toEqual({ workflow: { preset: 'local-only' } })
    expect(
      within(block()).getByText(copy(T.edit_selects_the_preset, {
        preset: 'local-only',
        path: 'workflow.preset',
      })),
    ).toBeTruthy()
  })

  it('states that only an operator confirmation can write the fenced section', async () => {
    await openDelivery()
    const bundled = within(block()).getByRole('group', { name: T.presets_bundled_with_the_app })
    fireEvent.click(within(bundled).getByRole('button', { name: 'local-only' }))
    fireEvent.click(within(block()).getByRole('button', { name: T.review_the_exact_change }))
    const fenced = en.apps.specEngine.formReview.only_an_operator_confirmation_writes_this
    expect(within(block()).getByText(copy(fenced, { path: 'workflow' }))).toBeTruthy()
  })

  it('withdraws a selection that is already what the document stores', async () => {
    await openDelivery()
    const bundled = within(block()).getByRole('group', { name: T.presets_bundled_with_the_app })
    fireEvent.click(within(bundled).getByRole('button', { name: 'local-only' }))
    // Back to what is stored: not a change, and every write is recorded, so staging
    // it would put a line in the durable write record for an edit nobody made.
    fireEvent.click(within(bundled).getByRole('button', { name: 'git-pull-request' }))
    expect(
      (within(block()).getByRole('button', { name: T.review_the_exact_change }) as HTMLButtonElement)
        .disabled,
    ).toBe(true)
  })
})

describe('defining a preset', () => {
  it('states that the commands execute and that a definition is app-wide', async () => {
    await openDelivery()
    expect(within(block()).getByText(T.these_commands_will_be_executed)).toBeTruthy()
    expect(within(block()).getByText(T.a_definition_applies_to_every_project)).toBeTruthy()
  })

  it('composes one path per stage, and the argv it parsed from what was typed', async () => {
    await openDelivery()
    fireEvent.change(within(block()).getByLabelText(T.the_preset_name), {
      target: { value: 'my-flow' },
    })
    fireEvent.change(draftField('submit'), {
      target: { value: 'git commit -m "a b"\ngit push origin HEAD' },
    })
    fireEvent.change(draftField('verify'), { target: { value: 'make check' } })
    fireEvent.click(within(block()).getByRole('button', { name: T.review_the_exact_change }))
    expect(shownPatch()).toEqual({
      workflow: {
        presets: {
          'my-flow': {
            stages: {
              submit: [
                ['git', 'commit', '-m', 'a b'],
                ['git', 'push', 'origin', 'HEAD'],
              ],
              verify: [['make', 'check']],
            },
          },
        },
      },
    })
  })

  it('declares that the confirm authorises commands to run', async () => {
    await openDelivery()
    fireEvent.change(within(block()).getByLabelText(T.the_preset_name), {
      target: { value: 'my-flow' },
    })
    fireEvent.change(draftField('submit'), { target: { value: 'make ship' } })
    fireEvent.click(within(block()).getByRole('button', { name: T.review_the_exact_change }))
    expect(
      within(block()).getByText(en.apps.specEngine.formReview.authorises_commands_to_run),
    ).toBeTruthy()
  })

  it('refuses a bundled name, says it is reserved, and keeps the typed commands', async () => {
    await openDelivery()
    fireEvent.change(draftField('submit'), { target: { value: 'make ship' } })
    fireEvent.change(within(block()).getByLabelText(T.the_preset_name), {
      target: { value: 'git-pull-request' },
    })
    expect(
      within(block()).getByText(
        copy(T.the_name_is_reserved_for_a_bundled_preset, { name: 'git-pull-request' }),
      ),
    ).toBeTruthy()
    // The commands survive the refusal: the name is part of every path a definition
    // writes, so a refused name has nowhere to hold a draft and the draft is held
    // here instead.
    expect(draftField('submit').value).toBe('make ship')
    // And nothing is offered for review, because there is no path to write it at.
    expect(
      (within(block()).getByRole('button', { name: T.review_the_exact_change }) as HTMLButtonElement)
        .disabled,
    ).toBe(true)
  })

  it('refuses a name the document already defines', async () => {
    await openDelivery()
    fireEvent.change(within(block()).getByLabelText(T.the_preset_name), {
      target: { value: 'house-style' },
    })
    expect(
      within(block()).getByText(
        copy(T.a_preset_of_that_name_is_already_defined, { name: 'house-style' }),
      ),
    ).toBeTruthy()
  })
})

describe('removing a user-defined preset', () => {
  it('is refused when a project selects it, naming that project', async () => {
    await openDelivery()
    fireEvent.click(
      within(block()).getByRole('button', {
        name: copy(T.remove_the_preset, { preset: 'house-style' }),
      }),
    )
    expect(
      within(block()).getByText(
        copy(T.removal_is_refused_projects_select_it, {
          preset: 'house-style',
          projects: 'acme',
        }),
      ),
    ).toBeTruthy()
    // Refused means nothing staged: the next action is to point acme elsewhere.
    expect(
      (within(block()).getByRole('button', { name: T.review_the_exact_change }) as HTMLButtonElement)
        .disabled,
    ).toBe(true)
  })

  it('is offered for a preset nothing selects, and stages one named path', async () => {
    await openDelivery()
    fireEvent.click(
      within(block()).getByRole('button', {
        name: copy(T.remove_the_preset, { preset: 'unused-preset' }),
      }),
    )
    fireEvent.click(within(block()).getByRole('button', { name: T.review_the_exact_change }))
    // JSON null is how the shared patch builder spells a deletion.
    expect(shownPatch()).toEqual({ workflow: { presets: { 'unused-preset': null } } })
  })

  it('is refused when the app-wide selection names it', async () => {
    const doc = stored()
    doc.workflow.preset = 'unused-preset'
    await openDelivery({ config: { body: snapshot(doc) } })
    fireEvent.click(
      within(block()).getByRole('button', {
        name: copy(T.remove_the_preset, { preset: 'unused-preset' }),
      }),
    )
    expect(
      within(block()).getByText(
        copy(T.removal_is_refused_the_app_selects_it, { preset: 'unused-preset' }),
      ),
    ).toBeTruthy()
  })
})

/**
 * A preset name the route had to cap for display.
 *
 * Every case here is the same defect with a different consequence: the route sends
 * `<64 chars> [...]`, and any of these paths that spent that string would spend a
 * name no document holds. The first is the severe one — it takes the whole workflow
 * read down and leaves no in-form way back, because `DeliveryWorkflow` refuses to
 * resolve a selection naming a preset nothing defines.
 */
describe('a preset name too long to display is still the name that is written', () => {
  it('selects the name the document holds, never the capped rendering', async () => {
    await openDelivery({
      config: { body: snapshot(storedWithLongName()) },
      workflow: { body: workflowWithLongName() },
    })
    const mine = within(block()).getByRole('group', { name: T.presets_you_defined })
    // What is marked as chosen is read from the document too, so the pressed state
    // and the write agree on one spelling: `house-style` is what `workflow.preset`
    // holds in this fixture, and the capped rendering would match no button at all.
    expect(
      within(block())
        .getByRole('group', { name: T.presets_bundled_with_the_app })
        .querySelector('[aria-pressed="true"]')?.textContent,
    ).toBe('git-pull-request')
    fireEvent.click(within(mine).getByRole('button', { name: LONG_NAME }))
    expect(mine.querySelector('[aria-pressed="true"]')?.textContent).toBe(LONG_NAME)
    fireEvent.click(within(block()).getByRole('button', { name: T.review_the_exact_change }))
    // The whole point: `workflow.preset` naming a preset nothing defines is a
    // ConfigValidationError, the route answers 422, and the workflow read fails —
    // so this form would have written the one value that takes itself off the air.
    expect(shownPatch()).toEqual({ workflow: { preset: LONG_NAME } })
    expect(JSON.stringify(shownPatch())).not.toContain(CAPPED_NAME)
  })

  it('removes the entry the document holds, so the removal removes something', async () => {
    await openDelivery({
      config: { body: snapshot(storedWithLongName()) },
      workflow: { body: workflowWithLongName() },
    })
    fireEvent.click(
      within(block()).getByRole('button', {
        name: copy(T.remove_the_preset, { preset: LONG_NAME }),
      }),
    )
    fireEvent.click(within(block()).getByRole('button', { name: T.review_the_exact_change }))
    // A deletion at the capped path would delete NOTHING while the card said the
    // preset was removed.
    expect(shownPatch()).toEqual({ workflow: { presets: { [LONG_NAME]: null } } })
  })

  it('refuses the removal a project’s selection would strand', async () => {
    await openDelivery({
      config: { body: snapshot(storedWithLongName('project')) },
      workflow: { body: workflowWithLongName() },
    })
    fireEvent.click(
      within(block()).getByRole('button', {
        name: copy(T.remove_the_preset, { preset: LONG_NAME }),
      }),
    )
    // The comparison is against the document's own selection values, so the
    // refusal holds for a name the display had to cap too.
    expect(
      within(block()).getByText(
        copy(T.removal_is_refused_projects_select_it, { preset: LONG_NAME, projects: 'acme' }),
      ),
    ).toBeTruthy()
    expect(
      (
        within(block()).getByRole('button', {
          name: T.review_the_exact_change,
        }) as HTMLButtonElement
      ).disabled,
    ).toBe(true)
  })

  it('refuses the removal the app-wide selection would strand', async () => {
    // The APP half of the same comparison, and the half a short name proves nothing
    // about: the defect being guarded is a comparison made against the route's
    // capped rendering, which is only a different string past the cap.
    expect(LONG_NAME.length).toBeGreaterThan(registry().workflow_preset_name_limit)
    await openDelivery({
      config: { body: snapshot(storedWithLongName('app')) },
      workflow: {
        body: workflowWithLongName({
          preset: {
            name: CAPPED_NAME,
            origin: 'app_config',
            declared_at: 'workflow.preset',
            bundled: false,
          },
        }),
      },
    })
    fireEvent.click(
      within(block()).getByRole('button', {
        name: copy(T.remove_the_preset, { preset: LONG_NAME }),
      }),
    )
    // `workflow.preset` holds the document's own spelling, so the refusal has to be
    // read from there: compared against the capped rendering this removal would be
    // OFFERED, and confirming it would leave the app-wide selection naming a preset
    // nothing defines — which the engine answers by refusing to resolve the workflow
    // at all, taking the read down with no in-form way back.
    expect(
      within(block()).getByText(
        copy(T.removal_is_refused_the_app_selects_it, { preset: LONG_NAME }),
      ),
    ).toBeTruthy()
    expect(
      (
        within(block()).getByRole('button', {
          name: T.review_the_exact_change,
        }) as HTMLButtonElement
      ).disabled,
    ).toBe(true)
  })

  it('refuses a second definition of that name rather than merging into the first', async () => {
    await openDelivery({
      config: { body: snapshot(storedWithLongName()) },
      workflow: { body: workflowWithLongName() },
    })
    fireEvent.change(draftField('submit'), { target: { value: 'make ship' } })
    fireEvent.change(within(block()).getByLabelText(T.the_preset_name), {
      target: { value: LONG_NAME },
    })
    // Against the DOCUMENT's names: a taken check that only knew the capped
    // rendering would let this through, and the write would merge into the existing
    // definition instead of creating one.
    expect(
      within(block()).getByText(copy(T.a_preset_of_that_name_is_already_defined, { name: LONG_NAME })),
    ).toBeTruthy()
    expect(draftField('submit').value).toBe('make ship')
  })

  it('refuses a typed name past the cap, keeping the typed stage commands', async () => {
    await openDelivery()
    fireEvent.change(draftField('submit'), { target: { value: 'make ship' } })
    fireEvent.change(within(block()).getByLabelText(T.the_preset_name), {
      target: { value: LONG_NAME },
    })
    // The other half of keeping one spelling of every name: this form must not be
    // able to CREATE a name it could then only ever show truncated.
    expect(
      within(block()).getByText(copy(T.the_name_is_longer_than_a_name_can_be_shown, { limit: '64' })),
    ).toBeTruthy()
    expect(draftField('submit').value).toBe('make ship')
    expect(
      (
        within(block()).getByRole('button', {
          name: T.review_the_exact_change,
        }) as HTMLButtonElement
      ).disabled,
    ).toBe(true)
  })

  it('marks the preset in force as chosen under the name the document spells it', async () => {
    await openDelivery({
      config: { body: snapshot(storedWithLongName('app')) },
      // The route resolves the selection and caps its name, so the payload's
      // `preset.name` is the truncated one. Marking the chooser from THAT would mark
      // nothing: no button carries a name the document does not hold.
      workflow: {
        body: workflowWithLongName({
          preset: {
            name: CAPPED_NAME,
            origin: 'app_config',
            declared_at: 'workflow.preset',
            bundled: false,
          },
        }),
      },
    })
    const mine = within(block()).getByRole('group', { name: T.presets_you_defined })
    expect(mine.querySelector('[aria-pressed="true"]')?.textContent).toBe(LONG_NAME)
  })

  it('refuses no length at all when the projection does not carry the cap', async () => {
    // An older gateway serves no such key, and a cap invented on this side would
    // refuse a name the engine would have displayed perfectly well.
    const older = registry()
    delete (older as { workflow_preset_name_limit?: number }).workflow_preset_name_limit
    await openDelivery({ registry: { body: older } })
    fireEvent.change(within(block()).getByLabelText(T.the_preset_name), {
      target: { value: LONG_NAME },
    })
    expect(
      within(block()).queryByText(
        copy(T.the_name_is_longer_than_a_name_can_be_shown, { limit: '64' }),
      ),
    ).toBeNull()
  })
})

describe('a failed read states itself', () => {
  it('reports a refused workflow read rather than rendering an empty workflow', async () => {
    await openDelivery({ workflow: failure(409, 'config_unreadable') })
    await waitFor(() =>
      expect(screen.getByText(T.could_not_read_the_delivery_workflow)).toBeTruthy(),
    )
    // No stage row at all, and in particular no row claiming a stage is
    // unconfigured: that is a fact about the document, and this read did not land.
    expect(block().querySelectorAll('.se-setting[data-stage]')).toHaveLength(0)
  })

  it('reports a refused vocabulary read, so no chooser offers an invented list', async () => {
    // The pane deliberately does NOT collapse its stage areas on a failed
    // vocabulary REFETCH — unmounting the panels would take every form's staged
    // edits with them — so the reachable path is: the first read lands, the delivery
    // area exists, a refetch is refused, and each form's own guard keeps its rows
    // from being filled out of a retained vocabulary.
    const client = await openDelivery({
      registry: [{ body: registry() }, failure(503, 'registry_unavailable')],
    })
    await within(block()).findByRole('group', { name: T.presets_bundled_with_the_app })
    void client.invalidateQueries({ queryKey: QK.registry })
    await waitFor(() =>
      expect(screen.getByText(T.could_not_read_the_workflow_vocabulary)).toBeTruthy(),
    )
    // And no chooser is left offering names nobody re-read.
    expect(within(block()).queryByRole('group', { name: T.presets_bundled_with_the_app })).toBeNull()
  })

  it('keeps reporting a typed definition once the workflow read is refused', async () => {
    const client = await openDelivery({
      workflow: [{ body: workflow() }, failure(409, 'config_unreadable')],
    })
    fireEvent.change(within(block()).getByLabelText(T.the_preset_name), {
      target: { value: 'my-flow' },
    })
    fireEvent.change(draftField('submit'), { target: { value: 'make ship' } })
    await waitFor(() => expect(deliveryPending()).toBe('1'))
    void client.invalidateQueries({ queryKey: QK.workflow('') })
    await waitFor(() =>
      expect(screen.getByText(T.could_not_read_the_delivery_workflow)).toBeTruthy(),
    )
    // The stage rows go with the read and the typed definition does NOT: it is held
    // in this form's own state, because the name is part of every path a definition
    // writes and a refused name would have nowhere to hold a draft. So a count taken
    // from the rows reports the definition as gone the moment a refetch fails, on
    // the one branch that exists to say what the form is still holding — and the
    // operator reads "no unwritten changes" over work that is sitting right there.
    expect(deliveryPending()).toBe('1')
  })

  it('reports nothing when the read it refused was holding nothing', async () => {
    // The opposite pole, and the test above cannot do without it: a count wired to
    // any constant, or stuck at its last value, would satisfy that assertion alone.
    const client = await openDelivery({
      workflow: [{ body: workflow() }, failure(409, 'config_unreadable')],
    })
    expect(deliveryPending()).toBe('')
    void client.invalidateQueries({ queryKey: QK.workflow('') })
    await waitFor(() =>
      expect(screen.getByText(T.could_not_read_the_delivery_workflow)).toBeTruthy(),
    )
    expect(deliveryPending()).toBe('')
  })
})
