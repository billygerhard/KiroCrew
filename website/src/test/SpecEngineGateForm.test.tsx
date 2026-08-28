/**
 * The quality-gate form: the checks this installation insists on.
 *
 * Every property below is a safety claim rather than a rendering preference, because
 * a gate is a list of commands the engine RUNS:
 *
 *   - **`[]` and `null` are different answers.** `gates: []` means no gate is
 *     configured and the empty block says so beside the fact that the engine floor
 *     still applies; `gates: null` with `gates_unreadable` means the stored list could
 *     not be parsed, the engine refuses delivery outright, and the pane must never
 *     render that as "no gate is configured". Both poles are asserted in one suite so
 *     neither claim can pass vacuously.
 *   - **The positions and severities are the engine's.** They come from the registry
 *     projection, each carries a plain-language statement of what it does to a run,
 *     and a payload that carries neither offers no control at all rather than a list
 *     this side invented.
 *   - **Gates are app-wide**, and the block says so instead of implying a scope.
 *   - **A bundled definition is the way in**, so a gate can be added without composing
 *     commands, and copying one states what it would run.
 *   - **A duplicate name is refused against the name field with the entered gate
 *     retained.**
 *   - **A removal is confirmed by typing the gate's own name**, and the confirmation
 *     names it.
 *   - **The write is one patch at `quality_gates`, composed from the DOCUMENT** — never
 *     from the route's display rows — and a stored list this form cannot express earns
 *     no write control at all.
 */
import { describe, it, expect, afterEach } from 'vitest'
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

import { PIPELINE_STAGES, stubSpecEngineFetch, type Answer } from './specEngineFetchStub'

const T = en.apps.specEngine.gateForm
const C = en.apps.specEngine.configPanel
const P = en.apps.specEngine.specEnginePage
const R = en.apps.specEngine.formReview

/** Every request the page made, so an assertion can read the body that was sent. */
const calls: Array<{ url: string; method: string; body: unknown }> = []

/** The bundled gate definitions, as `gate_presets()` deep-copies them. */
const GATE_PRESETS = [
  { name: 'tests', position: 'pre_submit', severity: 'blocking', commands: [['make', 'test']] },
  {
    name: 'coverage',
    position: 'pre_submit',
    severity: 'advisory',
    commands: [['make', 'coverage', 'BASE={base_branch}']],
  },
]

/** The registry payload, in `_registry_payload`'s shape. */
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
    // The engine's own tuples, in the engine's own order. Their own tuples rather
    // than a setting's `choices`, which no setting declares.
    gate_positions: ['pre_submit', 'post_submit', 'both'],
    gate_severities: ['blocking', 'advisory'],
    gate_presets: GATE_PRESETS,
    ...over,
  }
}

/** The stored `quality_gates` list, as a document holds it. */
function storedGates() {
  return [
    {
      name: 'tests',
      position: 'pre_submit',
      severity: 'blocking',
      commands: [['make', 'test']],
    },
    {
      name: 'audit',
      position: 'post_submit',
      severity: 'advisory',
      commands: [['make', 'audit']],
    },
  ]
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
    // The engine's own fence list, so the review card can state that only an
    // operator confirmation writes this section.
    config_only_paths: ['quality_gates'],
  }
}

/** The workflow read, whose gate rows are the engine's own reading of the list. */
function workflow(over: Record<string, unknown> = {}) {
  return {
    configured: true,
    project: null,
    preset: null,
    stages: [],
    user_presets: [],
    delivery_flow_stages: ['submit', 'verify', 'publish'],
    gates_scope_is_app: true,
    gates: storedGates().map((gate, index) => ({
      ...gate,
      blocking: gate.severity === 'blocking',
      origin: 'app_config',
      declared_at: `quality_gates[${index}]`,
    })),
    gates_unreadable: false,
    gate_errors: [],
    ...over,
  }
}

/** The resolved read. Answered so the pane beside this form renders at all. */
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

function stub(answers: {
  registry?: Answer
  config?: Answer
  configAfterPut?: Answer
  workflow?: Answer
  put?: Answer
} = {}) {
  return stubSpecEngineFetch(
    {
      registry: answers.registry ?? { body: registry() },
      resolved: { body: resolved() },
      workflow: answers.workflow ?? { body: workflow() },
      config: ({ written }) =>
        (written ? answers.configAfterPut : undefined) ??
        answers.config ?? { body: snapshot({ quality_gates: storedGates() }) },
      configWrite: answers.put ?? { body: { ok: true, document: {}, advisories: [] } },
    },
    { record: calls },
  )
}

/**
 * Render the page, open the configuration pane, and show the delivery stage.
 *
 * The gate form lives in the delivery area: a gate's position is defined relative to
 * raising the review artifact, which is a delivery stage. Only the active panel is
 * reachable — an inactive one carries `hidden`, which takes it out of the
 * accessibility tree the role queries read — so every case starts by activating it.
 */
async function openGates(answers: Parameters<typeof stub>[0] = {}) {
  const stubbed = stub(answers)
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
  fireEvent.click(screen.getByRole('tab', { name: new RegExp(`^${C.stage_delivery}`) }))
  await screen.findByRole('heading', { name: T.quality_gates })
  return { client, stubbed }
}

/**
 * The gate form's block.
 *
 * Every query is scoped to it: the delivery workflow form on the same stage renders
 * command lists and review-card words of its own, so an unscoped query could not tell
 * the two surfaces apart.
 */
function block(): HTMLElement {
  const heading = screen.getByRole('heading', { name: T.quality_gates })
  const found = heading.closest('.se-blk')
  expect(found).not.toBeNull()
  return found as HTMLElement
}

/** One gate's row, addressed by the name the form shows for it. */
function gateRow(name: string): HTMLElement {
  const found = block().querySelector(`.se-setting[data-gate="${name}"]`)
  expect(found, `no row for ${name}`).not.toBeNull()
  return found as HTMLElement
}

/** Choose *label* in the group named *group*, inside *scope*. */
function choose(scope: HTMLElement, group: string, label: string) {
  const found = within(scope).getByRole('group', { name: group })
  fireEvent.click(within(found).getByRole('button', { name: label }))
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

/** The patch the one PUT carried. */
function putPatch(): unknown {
  const put = calls.filter((call) => call.method === 'PUT')
  expect(put).toHaveLength(1)
  return (put[0].body as { patch: unknown }).patch
}

/** Fill *label* inside the form's block. */
function fill(label: string, value: string) {
  fireEvent.change(within(block()).getByLabelText(label), { target: { value } })
}

afterEach(() => {
  cleanup()
  calls.length = 0
})

describe('the quality gates as the pane lists them', () => {
  it('lists every gate with its name, position, severity and commands', async () => {
    await openGates()
    const tests = gateRow('tests')
    expect(tests.textContent).toContain(T.position_pre_submit)
    expect(tests.textContent).toContain(T.severity_blocking)
    expect(within(tests).getByLabelText(T.commands_for_gate.replace('{{gate}}', 'tests'))).toHaveValue(
      'make test',
    )
    const audit = gateRow('audit')
    expect(audit.textContent).toContain(T.position_post_submit)
    expect(audit.textContent).toContain(T.severity_advisory)
    expect(within(audit).getByLabelText(T.commands_for_gate.replace('{{gate}}', 'audit'))).toHaveValue(
      'make audit',
    )
  })

  it('states that the gates apply to every project and offers no project control', async () => {
    await openGates()
    expect(within(block()).getByText(T.gates_apply_to_every_project)).toBeTruthy()
    // The pane's project selection sits above the stages and is not this block's.
    expect(block().querySelector('[data-gate-project]')).toBeNull()
  })

  it('names the declaring path beside each gate rather than an origin identifier', async () => {
    await openGates()
    expect(within(gateRow('tests')).getByText('quality_gates[0]')).toBeTruthy()
    // The engine's own `ValueOrigin` token is never user-facing text.
    expect(block().textContent).not.toContain('app_config')
  })
})

describe('the positions and severities the engine declares', () => {
  it('offers exactly the projected vocabularies and nothing this side invented', async () => {
    await openGates()
    const group = within(gateRow('tests')).getByRole('group', {
      name: T.choose_a_position_for_gate.replace('{{gate}}', 'tests'),
    })
    expect(
      within(group)
        .getAllByRole('button')
        .map((button) => button.getAttribute('data-value')),
    ).toEqual(['pre_submit', 'post_submit', 'both'])
    const severity = within(gateRow('tests')).getByRole('group', {
      name: T.choose_a_severity_for_gate.replace('{{gate}}', 'tests'),
    })
    expect(
      within(severity)
        .getAllByRole('button')
        .map((button) => button.getAttribute('data-value')),
    ).toEqual(['blocking', 'advisory'])
  })

  it('offers a position the engine declares that this pane has no words for', async () => {
    // The vocabulary is the engine's and it may grow. A position with no authored
    // label renders as its own token rather than being hidden, which is what keeps
    // a gate's actual behaviour visible.
    await openGates({
      registry: {
        body: registry({ gate_positions: ['pre_submit', 'post_submit', 'both', 'pre_isolate'] }),
      },
    })
    const group = within(gateRow('tests')).getByRole('group', {
      name: T.choose_a_position_for_gate.replace('{{gate}}', 'tests'),
    })
    expect(within(group).getByRole('button', { name: 'pre_isolate' })).toBeTruthy()
  })

  it('states what each position does to a run, in the add block, before any choice', async () => {
    await openGates()
    const text = block().textContent ?? ''
    expect(text).toContain(T.position_pre_submit_effect)
    expect(text).toContain(T.position_post_submit_effect)
    expect(text).toContain(T.position_both_effect)
    // The three consequences are distinct sentences and not one with a token
    // interpolated: never raised, not published, and both in sequence.
    expect(T.position_pre_submit_effect).not.toEqual(T.position_post_submit_effect)
    expect(T.position_both_effect).not.toEqual(T.position_pre_submit_effect)
  })

  it('states what each severity does to a run', async () => {
    await openGates()
    const text = block().textContent ?? ''
    expect(text).toContain(T.severity_blocking_effect)
    expect(text).toContain(T.severity_advisory_effect)
  })

  it('takes whether a failure stops the run from the engine, not from its own table', async () => {
    await openGates()
    // The payload's `blocking` is the ENGINE's reading of the severity, and it is
    // what the stop/not-stop fact is rendered from.
    expect(gateRow('tests').querySelector('[data-blocking="true"]')).not.toBeNull()
    expect(within(gateRow('tests')).getByText(T.a_failure_stops_the_run)).toBeTruthy()
    expect(gateRow('audit').querySelector('[data-blocking="false"]')).not.toBeNull()
    expect(within(gateRow('audit')).getByText(T.a_failure_does_not_stop_the_run)).toBeTruthy()
  })

  it('states the effect of a severity this pane has no words for', async () => {
    // The prose table is keyed by the severities this pane knows. A severity the
    // engine adds later earns no sentence from it — and the payload was carrying the
    // answer the whole time, so the row says what a failure does regardless.
    const gates = storedGates()
    gates[1].severity = 'fatal'
    await openGates({
      registry: { body: registry({ gate_severities: ['blocking', 'advisory', 'fatal'] }) },
      config: { body: snapshot({ quality_gates: gates }) },
      workflow: {
        body: workflow({
          gates: gates.map((gate, index) => ({
            ...gate,
            // The engine reads `fatal` as blocking; this pane has no prose for it.
            blocking: gate.severity !== 'advisory',
            origin: 'app_config',
            declared_at: `quality_gates[${index}]`,
          })),
        }),
      },
    })
    const row = gateRow('audit')
    expect(row.textContent).toContain('fatal')
    expect(within(row).getByText(T.a_failure_stops_the_run)).toBeTruthy()
  })

  it('withholds the engine’s reading once a draft has changed that severity', async () => {
    await openGates()
    // The stored flag describes the severity being REPLACED, so relaying it here
    // would state the effect of the choice the operator just moved away from.
    choose(
      gateRow('tests'),
      T.choose_a_severity_for_gate.replace('{{gate}}', 'tests'),
      T.severity_advisory,
    )
    expect(gateRow('tests').querySelector('[data-blocking]')).toBeNull()
    // The pane's own prose still describes the drafted severity.
    expect(within(gateRow('tests')).getByText(T.severity_advisory_effect)).toBeTruthy()
  })

  it('offers no gate control at all when the vocabularies were not read', async () => {
    // An older gateway sends neither tuple. Offering a list this side invented would
    // offer what the write door then refuses by path.
    await openGates({
      registry: { body: registry({ gate_positions: undefined, gate_severities: undefined }) },
    })
    expect(within(block()).getByText(T.the_gate_vocabularies_were_not_read)).toBeTruthy()
    expect(block().querySelector('[role="group"]')).toBeNull()
    expect(within(block()).queryByRole('heading', { name: T.add_a_quality_gate })).toBeNull()
    // The gates are still LISTED: a vocabulary that was not read is a reason not to
    // offer a choice, not a reason to hide what is configured.
    expect(gateRow('tests').textContent).toContain(T.position_pre_submit)
    expect(gateRow('tests').textContent).toContain(T.severity_blocking)
  })
})

describe('an unreadable gate list against an empty one', () => {
  it('says the stored list could not be read and never that no gate is configured', async () => {
    await openGates({
      workflow: {
        body: workflow({
          gates: null,
          gates_unreadable: true,
          gate_errors: [{ path: 'quality_gates[1].name', message: 'duplicate gate name' }],
        }),
      },
    })
    const found = block()
    expect(within(found).getByText(T.the_stored_gate_list_could_not_be_read)).toBeTruthy()
    expect(within(found).getByText(T.an_unreadable_list_refuses_delivery)).toBeTruthy()
    expect(within(found).getByText('quality_gates[1].name')).toBeTruthy()
    expect(within(found).getByText('duplicate gate name')).toBeTruthy()
    // The whole point of the distinction: an unreadable list is not an empty one.
    expect(within(found).queryByText(T.no_gate_is_configured)).toBeNull()
    expect(found.querySelector('[data-gates-state="unreadable"]')).not.toBeNull()
    expect(found.querySelector('[data-gates-state="empty"]')).toBeNull()
    // And no write is offered, because a write over a list this side cannot read
    // would be composed from a reading that does not exist.
    expect(within(found).queryByRole('heading', { name: T.add_a_quality_gate })).toBeNull()
    expect(
      within(found).getByRole('button', { name: T.review_the_exact_change }),
    ).toBeDisabled()
  })

  it('says no gate runs for an EMPTY list, and that engine-floor validation still applies', async () => {
    // The other pole of the same distinction, asserted in the same suite so neither
    // claim can pass vacuously: this payload is `[]`, not null.
    await openGates({
      workflow: { body: workflow({ gates: [] }) },
      config: { body: snapshot({}) },
    })
    const found = block()
    expect(within(found).getByText(T.no_gate_is_configured)).toBeTruthy()
    expect(within(found).getByText(T.engine_floor_validation_still_applies)).toBeTruthy()
    expect(within(found).queryByText(T.the_stored_gate_list_could_not_be_read)).toBeNull()
    expect(found.querySelector('[data-gates-state="empty"]')).not.toBeNull()
    expect(found.querySelector('[data-gates-state="unreadable"]')).toBeNull()
    // An empty list is still editable: the add block is the answer to it.
    expect(within(found).getByRole('heading', { name: T.add_a_quality_gate })).toBeTruthy()
  })

  it('states a failed read as a refusal rather than as an empty list', async () => {
    await openGates({ workflow: { status: 500, body: { code: 'boom', error: 'boom' } } })
    expect(within(block()).getByRole('alert').textContent).toContain(
      T.could_not_read_the_quality_gates,
    )
    expect(within(block()).queryByText(T.no_gate_is_configured)).toBeNull()
  })

  it('never says no gate runs while a DRAFT is what removed them all', async () => {
    await openGates()
    // Remove both gates, leaving the draft empty and the document untouched.
    for (const name of ['tests', 'audit']) {
      fireEvent.click(
        within(gateRow(name)).getByRole('button', {
          name: T.remove_the_gate.replace('{{gate}}', name),
        }),
      )
      const armed = block().querySelector(`[data-gate-armed="${name}"]`) as HTMLElement
      fireEvent.change(
        within(armed).getByLabelText(T.type_the_name_to_confirm.replace('{{gate}}', name)),
        { target: { value: name } },
      )
      fireEvent.click(
        within(armed).getByRole('button', {
          name: T.confirm_the_removal.replace('{{gate}}', name),
        }),
      )
    }
    const found = block()
    // Nothing is written until the card is confirmed, so both stored gates are
    // still in force and still running: the document-scoped sentence would be
    // false, and it is the one an operator would act on.
    expect(within(found).queryByText(T.no_gate_is_configured)).toBeNull()
    expect(found.querySelector('[data-gates-state="empty"]')).toBeNull()
    expect(within(found).getByText(T.the_draft_leaves_no_gate)).toBeTruthy()
    // And the removals are still there to be confirmed.
    expect(within(found).getByRole('button', { name: T.review_the_exact_change })).toBeEnabled()
  })
})

describe('editing a gate', () => {
  it('stages one patch at the section carrying the whole list, and only that path', async () => {
    await openGates()
    choose(
      gateRow('tests'),
      T.choose_a_severity_for_gate.replace('{{gate}}', 'tests'),
      T.severity_advisory,
    )
    review()
    // Captured BEFORE the confirm, because a successful write clears the card: what
    // the disclosure showed has to be compared against what the request carried.
    const disclosed = shownPatch()
    expect(disclosed).toEqual({
      quality_gates: [
        { name: 'tests', position: 'pre_submit', severity: 'advisory', commands: [['make', 'test']] },
        {
          name: 'audit',
          position: 'post_submit',
          severity: 'advisory',
          commands: [['make', 'audit']],
        },
      ],
    })
    confirm()
    await waitFor(() => expect(calls.some((call) => call.method === 'PUT')).toBe(true))
    // What was submitted is what the disclosure showed.
    expect(putPatch()).toEqual(disclosed)
  })

  it('names the change and its consequence in plain language before the patch', async () => {
    await openGates()
    choose(
      gateRow('audit'),
      T.choose_a_position_for_gate.replace('{{gate}}', 'audit'),
      T.position_pre_submit,
    )
    review()
    const card = block().querySelector('.se-qbox') as HTMLElement
    expect(card.textContent).toContain(
      T.edit_changes_the_position
        .replace('{{gate}}', 'audit')
        .replace('{{oldValue}}', T.position_post_submit)
        .replace('{{newValue}}', T.position_pre_submit)
        .replace('{{effect}}', T.position_pre_submit_effect),
    )
    // The commands this write authorises are stated as their own consequence, and the
    // fence says why an operator is the one confirming.
    expect(card.textContent).toContain(R.authorises_commands_to_run)
    expect(card.textContent).toContain(
      R.only_an_operator_confirmation_writes_this.replace('{{path}}', 'quality_gates'),
    )
  })

  it('withdraws an edit that writes back exactly what is stored', async () => {
    await openGates()
    const row = () => gateRow('tests')
    choose(row(), T.choose_a_severity_for_gate.replace('{{gate}}', 'tests'), T.severity_advisory)
    expect(within(block()).getByRole('button', { name: T.review_the_exact_change })).toBeEnabled()
    choose(row(), T.choose_a_severity_for_gate.replace('{{gate}}', 'tests'), T.severity_blocking)
    expect(within(block()).getByRole('button', { name: T.review_the_exact_change })).toBeDisabled()
  })

  it('edits the commands as text and writes the argv the text names', async () => {
    await openGates()
    fill(T.commands_for_gate.replace('{{gate}}', 'tests'), 'pytest -x\nmake lint')
    review()
    expect(shownPatch()).toMatchObject({
      quality_gates: [
        { name: 'tests', commands: [['pytest', '-x'], ['make', 'lint']] },
        { name: 'audit', commands: [['make', 'audit']] },
      ],
    })
  })

  it('keeps typed command text a parse would have reshaped', async () => {
    await openGates()
    const label = T.commands_for_gate.replace('{{gate}}', 'tests')
    fill(label, 'make test ')
    // The buffer is what the control shows, so a trailing space can be typed through
    // rather than snapping the caret back.
    expect(within(block()).getByLabelText(label)).toHaveValue('make test ')
  })
})

describe('adding a gate', () => {
  it('offers the engine bundled definitions and states what each would run', async () => {
    await openGates()
    const group = within(block()).getByRole('group', { name: T.choose_a_definition_to_copy })
    expect(
      within(group)
        .getAllByRole('button')
        .map((button) => button.textContent),
    ).toEqual([
      T.copy_the_definition.replace('{{gate}}', 'tests'),
      T.copy_the_definition.replace('{{gate}}', 'coverage'),
    ])
    expect(group.textContent).toContain(
      T.definition_runs_commands
        .replace('{{position}}', T.position_pre_submit)
        .replace('{{severity}}', T.severity_advisory)
        .replace('{{commands}}', 'make coverage BASE={base_branch}'),
    )
  })

  it('adds a gate from a definition without the operator composing any command', async () => {
    await openGates()
    fill(T.name_for_the_new_gate, 'coverage')
    fireEvent.click(
      within(block()).getByRole('button', {
        name: T.copy_the_definition.replace('{{gate}}', 'coverage'),
      }),
    )
    review()
    expect(shownPatch()).toMatchObject({
      quality_gates: [
        { name: 'tests' },
        { name: 'audit' },
        {
          name: 'coverage',
          position: 'pre_submit',
          severity: 'advisory',
          commands: [['make', 'coverage', 'BASE={base_branch}']],
        },
      ],
    })
  })

  it('takes the chosen position and severity over the definition own', async () => {
    await openGates()
    fill(T.name_for_the_new_gate, 'coverage')
    const add = within(block()).getByRole('heading', { name: T.add_a_quality_gate })
      .parentElement as HTMLElement
    choose(add, T.choose_a_position_for_the_new_gate, T.position_both)
    choose(add, T.choose_a_severity_for_the_new_gate, T.severity_blocking)
    fireEvent.click(
      within(block()).getByRole('button', {
        name: T.copy_the_definition.replace('{{gate}}', 'coverage'),
      }),
    )
    review()
    expect(shownPatch()).toMatchObject({
      quality_gates: [
        { name: 'tests' },
        { name: 'audit' },
        { name: 'coverage', position: 'both', severity: 'blocking' },
      ],
    })
  })

  it('refuses a duplicate name against the name field and retains the entered gate', async () => {
    await openGates()
    fill(T.name_for_the_new_gate, 'audit')
    const add = within(block()).getByRole('heading', { name: T.add_a_quality_gate })
      .parentElement as HTMLElement
    choose(add, T.choose_a_position_for_the_new_gate, T.position_both)
    fill(T.the_commands, 'make audit --strict')
    fireEvent.click(
      within(block()).getByRole('button', {
        name: T.copy_the_definition.replace('{{gate}}', 'tests'),
      }),
    )
    // Refused, and stated at the field it concerns.
    const nameRow = within(block()).getByLabelText(T.name_for_the_new_gate)
      .closest('.se-setting') as HTMLElement
    expect(
      within(nameRow).getByText(T.the_name_is_already_a_gate.replace('{{gate}}', 'audit')),
    ).toBeTruthy()
    // Nothing was staged, and the entered gate is still here to be given another name.
    expect(within(block()).getByRole('button', { name: T.review_the_exact_change })).toBeDisabled()
    expect(within(block()).getByLabelText(T.name_for_the_new_gate)).toHaveValue('audit')
    expect(within(block()).getByLabelText(T.the_commands)).toHaveValue('make audit --strict')
    expect(
      within(within(add).getByRole('group', { name: T.choose_a_position_for_the_new_gate })).getByRole(
        'button',
        { name: T.position_both },
      ),
    ).toHaveAttribute('aria-pressed', 'true')
  })

  it('says so when the engine bundles no definition to start from', async () => {
    await openGates({ registry: { body: registry({ gate_presets: [] }) } })
    expect(within(block()).getByText(T.the_engine_bundles_no_gate_definition)).toBeTruthy()
  })
})

describe('removing a gate', () => {
  it('takes a confirmation that names the gate, and refuses a name that does not match', async () => {
    await openGates()
    fireEvent.click(
      within(gateRow('audit')).getByRole('button', {
        name: T.remove_the_gate.replace('{{gate}}', 'audit'),
      }),
    )
    const armed = block().querySelector('[data-gate-armed="audit"]') as HTMLElement
    expect(within(armed).getByText(T.removing_stops_running_the_check.replace('{{gate}}', 'audit')))
      .toBeTruthy()
    // A wrong name is refused and the refusal is acknowledged, because the
    // confirmation was on screen before the click.
    fireEvent.change(within(armed).getByLabelText(T.type_the_name_to_confirm.replace('{{gate}}', 'audit')), {
      target: { value: 'tests' },
    })
    fireEvent.click(
      within(armed).getByRole('button', { name: T.confirm_the_removal.replace('{{gate}}', 'audit') }),
    )
    expect(within(block()).getByText(new RegExp(T.the_removal_was_refused))).toBeTruthy()
    expect(within(block()).getByRole('button', { name: T.review_the_exact_change })).toBeDisabled()
  })

  it('stages the removal once the gate own name is typed, and states the consequence', async () => {
    await openGates()
    fireEvent.click(
      within(gateRow('audit')).getByRole('button', {
        name: T.remove_the_gate.replace('{{gate}}', 'audit'),
      }),
    )
    const armed = block().querySelector('[data-gate-armed="audit"]') as HTMLElement
    fireEvent.change(within(armed).getByLabelText(T.type_the_name_to_confirm.replace('{{gate}}', 'audit')), {
      target: { value: 'audit' },
    })
    fireEvent.click(
      within(armed).getByRole('button', { name: T.confirm_the_removal.replace('{{gate}}', 'audit') }),
    )
    review()
    const card = block().querySelector('.se-qbox') as HTMLElement
    expect(card.textContent).toContain(T.edit_removes_the_gate.replace('{{gate}}', 'audit'))
    // The card owns the statement of what removing a gate means to the flow.
    expect(card.textContent).toContain(R.removes_a_gate_from_the_flow)
    expect(shownPatch()).toEqual({
      quality_gates: [
        { name: 'tests', position: 'pre_submit', severity: 'blocking', commands: [['make', 'test']] },
      ],
    })
  })

  it('keeps each surviving gate’s own declaring path after one is removed', async () => {
    await openGates()
    // `tests` is at index 0 and `audit` at index 1, so removing the FIRST moves the
    // survivor to index 0 in the rows while the route still describes the document.
    fireEvent.click(
      within(gateRow('tests')).getByRole('button', {
        name: T.remove_the_gate.replace('{{gate}}', 'tests'),
      }),
    )
    const armed = block().querySelector('[data-gate-armed="tests"]') as HTMLElement
    fireEvent.change(
      within(armed).getByLabelText(T.type_the_name_to_confirm.replace('{{gate}}', 'tests')),
      { target: { value: 'tests' } },
    )
    fireEvent.click(
      within(armed).getByRole('button', {
        name: T.confirm_the_removal.replace('{{gate}}', 'tests'),
      }),
    )
    // Its own path, not the removed gate's: a path indexed by the row's position
    // would send an operator to edit the wrong entry of the document.
    expect(within(gateRow('audit')).getByText('quality_gates[1]')).toBeTruthy()
    expect(gateRow('audit').textContent).not.toContain('quality_gates[0]')
  })
})

describe('a stored gate list the form cannot express', () => {
  it('offers no write for a gate carrying a field the write door refuses', async () => {
    // `load_quality_gates` IGNORES an unknown key while the write door REFUSES one,
    // so a whole-list rewrite would drop it and be accepted.
    const gates = storedGates()
    await openGates({
      config: {
        body: snapshot({
          quality_gates: [{ ...gates[0], note: 'keep me' }, gates[1]],
        }),
      },
    })
    expect(within(block()).getByText(T.the_form_cannot_express_the_gate_list)).toBeTruthy()
    expect(within(block()).getByText(T.a_gate_change_writes_the_whole_list)).toBeTruthy()
    expect(within(block()).queryByRole('heading', { name: T.add_a_quality_gate })).toBeNull()
    expect(within(block()).getByRole('button', { name: T.review_the_exact_change })).toBeDisabled()
    // What is stored is still shown, read-only, so an operator can see it.
    expect(gateRow('tests').querySelector('pre.se-json')).not.toBeNull()
  })

  it('offers no write for an argv the command text could not round-trip', async () => {
    const gates = storedGates()
    await openGates({
      config: {
        body: snapshot({
          quality_gates: [{ ...gates[0], commands: [['sh', '-c', 'make test && make lint']] }, gates[1]],
        }),
      },
    })
    expect(within(block()).getByText(T.the_form_cannot_express_the_gate_list)).toBeTruthy()
    expect(within(block()).getByRole('button', { name: T.review_the_exact_change })).toBeDisabled()
  })

  it('offers no write when the two reads disagree about the list', async () => {
    // The document read and the workflow read are separate requests and can straddle
    // a write. A list this side cannot align must not be rewritten from either half.
    await openGates({ config: { body: snapshot({ quality_gates: [storedGates()[0]] }) } })
    expect(within(block()).getByText(T.the_form_cannot_express_the_gate_list)).toBeTruthy()
    expect(within(block()).getByRole('button', { name: T.review_the_exact_change })).toBeDisabled()
  })
})

describe('after a write', () => {
  it('re-reads rather than adopting the reply, and says so', async () => {
    await openGates({
      configAfterPut: { body: snapshot({ quality_gates: [storedGates()[0]] }) },
      workflow: [
        { body: workflow() },
        { body: workflow({ gates: workflow().gates.slice(0, 1) }) },
      ],
    })
    choose(
      gateRow('tests'),
      T.choose_a_severity_for_gate.replace('{{gate}}', 'tests'),
      T.severity_advisory,
    )
    review()
    confirm()
    await waitFor(() =>
      expect(within(block()).getByText(T.wrote_the_change_and_re_read_the_gates)).toBeTruthy(),
    )
    await waitFor(() => expect(block().querySelector('[data-gate="audit"]')).toBeNull())
  })

  it('retains the staged change and the stored rows when the write is refused', async () => {
    await openGates({ put: { status: 422, body: { code: 'config_invalid', error: 'nope' } } })
    choose(
      gateRow('tests'),
      T.choose_a_severity_for_gate.replace('{{gate}}', 'tests'),
      T.severity_advisory,
    )
    review()
    confirm()
    await waitFor(() => expect(within(block()).getByRole('alert')).toBeTruthy())
    expect(within(block()).getByText(T.nothing_was_written_so_the_gates_are_stored)).toBeTruthy()
    // The staged change is still here to be corrected and sent again.
    const staged = shownPatch() as { quality_gates: Array<Record<string, unknown>> }
    expect(staged.quality_gates[0]).toMatchObject({ name: 'tests', severity: 'advisory' })
  })
})

describe('the catalogs behind this form', () => {
  it('translates every gate-form key in all twelve shipped catalogs', () => {
    const catalogs: Array<[string, Record<string, unknown>]> = [
      ['bn', bn.apps.specEngine.gateForm],
      ['de', de.apps.specEngine.gateForm],
      ['es', es.apps.specEngine.gateForm],
      ['fr', fr.apps.specEngine.gateForm],
      ['hi', hi.apps.specEngine.gateForm],
      ['it', itIT.apps.specEngine.gateForm],
      ['ja', ja.apps.specEngine.gateForm],
      ['ko', ko.apps.specEngine.gateForm],
      ['pt', pt.apps.specEngine.gateForm],
      ['ru', ru.apps.specEngine.gateForm],
      ['zh-CN', zh.apps.specEngine.gateForm],
    ]
    const keys = Object.keys(T)
    for (const [tag, catalog] of catalogs) {
      expect(Object.keys(catalog).sort(), tag).toEqual([...keys].sort())
      for (const key of keys) {
        const value = catalog[key]
        expect(typeof value, `${tag}.${key}`).toBe('string')
        // Translated, not copied: the one exception is a placeholder-only string,
        // and this form has none.
        expect(value, `${tag}.${key}`).not.toEqual((T as Record<string, string>)[key])
      }
    }
  })
})
