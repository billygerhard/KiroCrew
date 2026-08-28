/**
 * The capability binding form: what it says about a binding, and what it writes.
 *
 * Seven capabilities can be served by the engine itself or delegated outside it, and
 * this is the surface that binds them. Five of the claims here are not arrangement:
 *
 * 1. **The cost signal branches on `kind`, never on `nature` alone.** The engine
 *    hardcodes `model_backed` for EVERY external binding — not because it knows the
 *    program reasons, but because it cannot know — so a renderer that read `nature`
 *    first would report "spends credits" for a deterministic external linter. The
 *    honest answer for an external provider is UNKNOWN, and the unit case below is
 *    written so a `nature`-first implementation fails it while a `kind`-first one
 *    passes.
 * 2. **`reachable: null` is not applicable, not unreachable.** A builtin is reachable
 *    by construction, so the engine's own check skips it. Coercing that to false
 *    would mark every unconfigured capability as a broken provider.
 * 3. **The builtin is written as a deletion.** The store merges nested objects key by
 *    key, so `{transport: 'builtin'}` would leave a stored command in place and the
 *    write door would then refuse the document for carrying one. Removing the whole
 *    entry writes no command, environment or timeout by construction.
 * 4. **A failed read states itself and is never shown as all-builtin.** Those two
 *    payloads are the same shape and opposite facts: all-builtin is what an
 *    UNCONFIGURED document legitimately resolves to, so falling back to it would
 *    present a refused `capabilities` section as a clean one.
 * 5. **A refusal lands on the field it names, with the entry still in it.** The
 *    engine reports one problem per path; a rebind can earn several, and each has to
 *    reach the control it concerns.
 *
 * Two more are about what the form must NOT do: it offers no control that would bind
 * an engine-floor capability, and it makes no claim that a provider is degrading
 * right now — a degradation is attached to one invocation and never persisted, so no
 * configuration surface can know.
 *
 * The placement of a capability under a stage is the ENGINE's, projected by
 * `/config/registry` and pinned in `test_pipeline_stages.py`; nothing here
 * re-derives it.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import SpecEnginePage from '../apps/spec-engine/SpecEnginePage'
import { QK } from '../apps/spec-engine/api'
import {
  argvFromText,
  bindingEdits,
  costSignal,
  draftProblem,
  fieldRefusal,
  positiveInt,
  storedBinding,
  textFromArgv,
} from '../apps/spec-engine/CapabilityForm'
import { DELETE, buildFormPatch } from '../apps/spec-engine/configDocument'
import en from '../i18n/locales/en.json'

import {
  ENGINE_FLOOR_CAPABILITIES,
  PIPELINE_STAGES,
  TRANSPORTS,
  stubSpecEngineFetch,
  expectEverySpecEngineRouteAnswered,
  type Answer,
  type Call,
} from './specEngineFetchStub'

const B = en.apps.specEngine.capabilityForm
const C = en.apps.specEngine.configPanel
const P = en.apps.specEngine.specEnginePage
const R = en.apps.specEngine.formReview

/** Every request the page made, so an assertion can read the body that was sent. */
const calls: Call[] = []

// --- the readings, as pure functions ----------------------------------------

describe('what can honestly be said about a provider cost', () => {
  it('reads nature as a cost class for a builtin', () => {
    expect(costSignal({ kind: 'builtin', nature: 'model_backed' })).toBe('credits')
    expect(costSignal({ kind: 'builtin', nature: 'deterministic' })).toBe('no_credits')
  })

  it('answers unknown for an external provider whatever its nature says', () => {
    // THE case. `nature` is hardcoded `model_backed` for every external binding, so
    // an implementation that read it before `kind` would answer `credits` here and
    // assert a spend the engine never claimed. `deterministic` is included on the
    // same footing: neither value licenses a claim about an external program, so
    // both must land on `unknown` rather than one of them happening to.
    expect(costSignal({ kind: 'external', nature: 'model_backed' })).toBe('unknown')
    expect(costSignal({ kind: 'external', nature: 'deterministic' })).toBe('unknown')
  })

  it('answers unknown for a kind it does not know, rather than guessing from nature', () => {
    // The only kind whose `nature` the engine computes is `builtin`, so a kind added
    // later must not inherit a builtin's reading by falling through to it.
    expect(costSignal({ kind: 'proxied' as 'builtin', nature: 'deterministic' })).toBe('unknown')
  })
})

describe('a command is one argument per line, inventing no quoting rule', () => {
  it('drops blank lines and trims, so no empty argument reaches the door', () => {
    expect(argvFromText('  gh \n\n api\n  ')).toEqual(['gh', 'api'])
    expect(argvFromText('   ')).toEqual([])
  })

  it('round-trips a stored argv back into the field', () => {
    expect(argvFromText(textFromArgv(['a b', 'c']))).toEqual(['a b', 'c'])
  })

  it('refuses a timeout that is not a positive whole number of seconds', () => {
    // `Number('1.5')` and `Number('x')` are both values a staged edit must never
    // carry: NaN serializes as JSON null, which is the store's spelling for DELETE,
    // so a typo would silently remove the key instead of being refused.
    expect(positiveInt('30')).toBe(30)
    expect(positiveInt('1.5')).toBeNull()
    expect(positiveInt('0')).toBeNull()
    expect(positiveInt('-1')).toBeNull()
    expect(positiveInt('x')).toBeNull()
  })
})

describe('the staged edits a binding composes', () => {
  const external = {
    transport: 'command',
    command: 'my-analyzer\n--json',
    env: [{ name: 'TOKEN', value: 'abc' }],
    timeout: '45',
  }

  it('writes the builtin as a deletion of the whole entry', () => {
    // Not `{transport: 'builtin'}`: the store merges nested objects key by key, so a
    // stored command would survive and the door refuses a builtin binding carrying
    // one. The deletion writes no command, environment or timeout by construction.
    const edits = bindingEdits(
      'review',
      { transport: 'builtin', command: 'x', env: [{ name: 'A', value: 'b' }], timeout: '9' },
      ['A'],
      '9',
    )
    expect(edits).toEqual([{ segments: ['capabilities', 'review'], value: DELETE }])
    expect(buildFormPatch(edits)).toEqual({ capabilities: { review: null } })
  })

  it('writes the transport, the command, the environment and the deadline', () => {
    const patch = buildFormPatch(bindingEdits('review', external, [], ''))
    expect(patch).toEqual({
      capabilities: {
        review: {
          transport: 'command',
          command: ['my-analyzer', '--json'],
          env: { TOKEN: 'abc' },
          timeout_s: 45,
        },
      },
    })
  })

  it('removes a stored environment name the draft dropped', () => {
    // The merge cannot express "replace this map", so a name the draft no longer
    // carries earns its own deletion. Without this the write would keep an entry the
    // form does not show, and no other path would ever remove it.
    const patch = buildFormPatch(
      bindingEdits('review', external, ['TOKEN', 'STALE'], '45'),
    )
    expect(patch.capabilities).toEqual({
      review: {
        transport: 'command',
        command: ['my-analyzer', '--json'],
        env: { TOKEN: 'abc', STALE: null },
        timeout_s: 45,
      },
    })
  })

  it('removes the whole environment key when the draft carries none', () => {
    const patch = buildFormPatch(
      bindingEdits('review', { ...external, env: [] }, ['TOKEN'], '45'),
    )
    expect((patch.capabilities as Record<string, Record<string, unknown>>).review.env).toBeNull()
  })

  it('writes no environment key at all when neither the draft nor the store has one', () => {
    const patch = buildFormPatch(bindingEdits('review', { ...external, env: [] }, [], '45'))
    const entry = (patch.capabilities as Record<string, Record<string, unknown>>).review
    expect(Object.prototype.hasOwnProperty.call(entry, 'env')).toBe(false)
  })

  it('removes a stored deadline when the field is cleared, and writes none when there was none', () => {
    const cleared = bindingEdits('review', { ...external, timeout: '' }, [], '45')
    expect(cleared).toContainEqual({
      segments: ['capabilities', 'review', 'timeout_s'],
      value: DELETE,
    })
    const never = bindingEdits('review', { ...external, timeout: '' }, [], '')
    expect(never.some((edit) => edit.segments[2] === 'timeout_s')).toBe(false)
  })

  it('addresses nothing outside the capability it binds', () => {
    for (const edit of bindingEdits('review', external, ['TOKEN', 'STALE'], '45')) {
      expect(edit.segments.slice(0, 2)).toEqual(['capabilities', 'review'])
    }
  })

  it('refuses to stage a command-bearing transport with no argument', () => {
    expect(draftProblem({ ...external, command: '  \n ' })).toBe('command')
    expect(draftProblem({ ...external, timeout: 'soon' })).toBe('timeout')
    expect(draftProblem(external)).toBe('')
    // The builtin takes neither, so neither can be a problem for it.
    expect(draftProblem({ transport: 'builtin', command: '', env: [], timeout: 'soon' })).toBe('')
  })
})

describe('the stored binding a rebind starts from', () => {
  it('reads the command and the deadline, and the environment NAMES only', () => {
    const stored = storedBinding(
      {
        capabilities: {
          review: {
            transport: 'mcp',
            command: ['srv', '--stdio'],
            env: { TOKEN: 'secret', REGION: 'eu' },
            timeout_s: 90,
          },
        },
      },
      'review',
    )
    expect(stored.present).toBe(true)
    expect(stored.transport).toBe('mcp')
    expect(stored.command).toBe('srv\n--stdio')
    expect(stored.timeout).toBe('90')
    expect(stored.envNames).toEqual(['TOKEN', 'REGION'])
    // The VALUES are not carried out of this reader at all, so no surface built on
    // it can echo one back.
    expect(JSON.stringify(stored)).not.toContain('secret')
  })

  it('reports absent rather than throwing on a document with no binding', () => {
    expect(storedBinding({}, 'review').present).toBe(false)
    expect(storedBinding({ capabilities: 'oops' }, 'review').present).toBe(false)
  })
})

describe('a write refusal reaches the field it names', () => {
  const error = new Error(
    'capabilities.review.transport: expected one of: builtin, mcp, command; ' +
      'capabilities.review.command[0]: expected a non-empty string',
  )

  it('matches a field by its own path, and an argv element by its command', () => {
    expect(fieldRefusal(error, 'capabilities.review.transport')).toBe(
      'expected one of: builtin, mcp, command',
    )
    expect(fieldRefusal(error, 'capabilities.review.command')).toBe(
      'expected a non-empty string',
    )
  })

  it('claims nothing for a path it does not recognise', () => {
    // An unattributed reason costs one well-placed line and still reaches the review
    // card's own refusal block. A misattributed one points an operator at a field
    // that is fine, so a near miss must NOT match.
    expect(fieldRefusal(error, 'capabilities.review.env')).toBe('')
    expect(fieldRefusal(error, 'capabilities.analysis.transport')).toBe('')
    expect(fieldRefusal(error, 'capabilities.review.transport_s')).toBe('')
    expect(fieldRefusal(null, 'capabilities.review.transport')).toBe('')
  })
})

// --- the form, mounted inside the pane --------------------------------------

/** One `/config/capabilities` row, in the shape the route composes. */
function row(over: Record<string, unknown> = {}) {
  return {
    capability: 'review',
    transport: 'builtin',
    provider: {
      name: 'engine-review-turn',
      kind: 'builtin',
      nature: 'model_backed',
      transport: 'builtin',
    },
    configured: false,
    declared_at: '',
    timeout_s: 120,
    program: '',
    reachable: null,
    action: '',
    ...over,
  }
}

/** The three capabilities the engine places in the authoring stage. */
const AUTHORING = ['analysis', 'authoring', 'validation_rules']

function capabilities(rows: Array<Record<string, unknown>>) {
  return { configured: true, capabilities: rows }
}

/** The registry, carrying the two vocabularies this form is generated from. */
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
    transports: TRANSPORTS,
    engine_floor: ENGINE_FLOOR_CAPABILITIES,
    ...over,
  }
}

function snapshot(document: Record<string, unknown>) {
  return {
    configured: true,
    path: '/home/me/.kiro/crew/apps/spec-engine/config.json',
    document,
    elided: [],
    elided_marker: '<elided>',
    errors: [],
    advisories: [],
    config_only_paths: ['capabilities'],
  }
}

/** The authoring stage's panel, whether or not it is the active one. */
function panel(): HTMLElement {
  const tab = screen.getByRole('tab', { name: new RegExp(`^${C.stage_authoring}`) })
  const found = document.getElementById(String(tab.getAttribute('aria-controls')))
  expect(found).not.toBeNull()
  return found as HTMLElement
}

/** One capability's row on the authoring panel. */
function capabilityRow(capability: string): HTMLElement {
  const found = panel().querySelector(`.se-setting[data-capability="${capability}"]`)
  expect(found, capability).not.toBeNull()
  return found as HTMLElement
}

async function openStage(
  answers: {
    capabilities?: Answer
    registry?: Answer
    config?: Answer
    configWrite?: Answer
  } = {},
) {
  stubSpecEngineFetch(
    {
      registry: answers.registry ?? { body: registry() },
      capabilities:
        answers.capabilities ??
        { body: capabilities(AUTHORING.map((capability) => row({ capability }))) },
      config: answers.config ?? { body: snapshot({}) },
      ...(answers.configWrite ? { configWrite: answers.configWrite } : {}),
    },
    { record: calls },
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
  await screen.findByRole('tablist', { name: C.configuration_stages })
  fireEvent.click(screen.getByRole('tab', { name: new RegExp(`^${C.stage_authoring}`) }))
  await waitFor(() => expect(panel().querySelector('.se-setting')).not.toBeNull())
  return client
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

describe('the form states what serves each capability the stage holds', () => {
  it('lists every capability the engine placed here, with its binding facts', async () => {
    await openStage({
      capabilities: {
        body: capabilities([
          row({
            capability: 'analysis',
            transport: 'command',
            provider: {
              name: 'my-analyzer',
              kind: 'external',
              nature: 'model_backed',
              transport: 'command',
              version: '2.1',
            },
            configured: true,
            declared_at: 'capabilities.analysis',
            timeout_s: 45,
            program: 'my-analyzer',
            reachable: true,
            action: '',
          }),
          row({ capability: 'authoring' }),
          row({ capability: 'validation_rules' }),
        ]),
      },
    })
    // Derived from the projection, not from a list on this side: the union of the
    // stages is the whole delegable vocabulary.
    const rows = panel().querySelectorAll('.se-setting[data-capability]')
    expect([...rows].map((element) => element.getAttribute('data-capability'))).toEqual(AUTHORING)
    const analysis = capabilityRow('analysis')
    expect(analysis.textContent).toContain('command')
    expect(analysis.textContent).toContain('my-analyzer')
    expect(analysis.textContent).toContain('2.1')
    expect(analysis.textContent).toContain('capabilities.analysis')
    expect(analysis.textContent).toContain(B.an_operator_declared_this_binding)
    expect(analysis.textContent).toContain('45')
    expect(analysis.textContent).toContain(B.the_program_was_found_on_this_host)
  })

  it('says a builtin binding is undeclared rather than showing an empty declaration', async () => {
    await openStage()
    const authoring = capabilityRow('authoring')
    expect(authoring.textContent).toContain(B.nothing_declares_this_so_the_builtin_serves_it)
    expect(authoring.textContent).not.toContain(B.an_operator_declared_this_binding)
  })

  it('states that the bindings apply to every project, before any control', async () => {
    await openStage()
    // The engine reads them from ONE app-wide section with no per-project layer, so
    // a rebind made while a project is selected is not a rebind for that project.
    const block = capabilityRow('authoring').closest('.se-blk') as HTMLElement
    expect(block.textContent).toContain(B.bindings_apply_to_every_project)
  })
})

describe('reachability is three-valued and a builtin is not a broken provider', () => {
  it('says reachability does not apply to a builtin, rather than reporting a failure', async () => {
    await openStage()
    const authoring = capabilityRow('authoring')
    // `null` means the engine's own check SKIPPED this binding, because a builtin is
    // reachable by construction. Coercing it to false would mark every unconfigured
    // capability as failing, which is every capability on a fresh install.
    expect(authoring.querySelector('[data-reachable="null"]')).not.toBeNull()
    expect(authoring.textContent).toContain(B.reachability_does_not_apply_to_a_builtin)
    expect(authoring.textContent).not.toContain(B.the_program_was_not_found_on_this_host)
  })

  it('relays the engine own remediation for a program it could not find', async () => {
    const action = "install jq, or unset it to use the builtin"
    await openStage({
      capabilities: {
        body: capabilities([
          row({
            capability: 'analysis',
            transport: 'command',
            configured: true,
            declared_at: 'capabilities.analysis',
            program: 'jq',
            reachable: false,
            action,
          }),
          row({ capability: 'authoring' }),
          row({ capability: 'validation_rules' }),
        ]),
      },
    })
    const analysis = capabilityRow('analysis')
    expect(analysis.textContent).toContain(B.the_program_was_not_found_on_this_host)
    // Relayed and not composed here: it names the "or unset it to use the builtin"
    // escape, and a sentence written on this side would be a second opinion about a
    // check this pane did not run.
    expect(analysis.textContent).toContain(action)
  })
})

describe('what the form says a binding costs', () => {
  it('marks a model-backed builtin as spending credits and a deterministic one as not', async () => {
    await openStage({
      capabilities: {
        body: capabilities([
          row({ capability: 'analysis', provider: { name: 'local-analyzer', kind: 'builtin', nature: 'deterministic', transport: 'builtin' } }),
          row({ capability: 'authoring', provider: { name: 'engine-authoring-turn', kind: 'builtin', nature: 'model_backed', transport: 'builtin' } }),
          row({ capability: 'validation_rules' }),
        ]),
      },
    })
    expect(capabilityRow('authoring').querySelector('[data-cost="credits"]')).not.toBeNull()
    expect(capabilityRow('authoring').textContent).toContain(
      B.the_builtin_asks_a_model_so_it_spends_credits,
    )
    expect(capabilityRow('analysis').querySelector('[data-cost="no_credits"]')).not.toBeNull()
  })

  it('states an external provider cost as unknown even though nature says model_backed', async () => {
    // The rendered half of the unit case above, and the failure this bullet exists to
    // prevent: the engine hardcodes `model_backed` for every external binding, so a
    // renderer reading `nature` first would put a credits sentence on a row the
    // engine made no cost claim about at all.
    await openStage({
      capabilities: {
        body: capabilities([
          row({
            capability: 'analysis',
            transport: 'command',
            configured: true,
            declared_at: 'capabilities.analysis',
            program: 'my-linter',
            reachable: true,
            provider: {
              name: 'my-linter',
              kind: 'external',
              nature: 'model_backed',
              transport: 'command',
            },
          }),
          row({ capability: 'authoring' }),
          row({ capability: 'validation_rules' }),
        ]),
      },
    })
    const analysis = capabilityRow('analysis')
    expect(analysis.querySelector('[data-cost="unknown"]')).not.toBeNull()
    expect(analysis.textContent).toContain(
      B.whether_an_external_provider_spends_credits_is_unknown,
    )
    expect(analysis.textContent).not.toContain(B.the_builtin_asks_a_model_so_it_spends_credits)
    expect(analysis.textContent).not.toContain(B.the_builtin_asks_no_model_so_it_spends_nothing)
  })
})

describe('what the form states about an external program, and what it refuses to claim', () => {
  it('states that output is data and that an unusable provider falls back', async () => {
    await openStage()
    const authoring = capabilityRow('authoring')
    expect(authoring.textContent).toContain(B.output_is_data_and_never_instructions)
    expect(authoring.textContent).toContain(B.an_unusable_provider_falls_back_to_the_builtin)
    // And says so as BEHAVIOUR rather than as status: a degradation is attached to
    // one invocation's result and is never persisted, so no configuration surface
    // can report that a provider is degrading now. The sentence carries that
    // qualifier itself, which is what keeps the claim honest.
    expect(B.an_unusable_provider_falls_back_to_the_builtin).toContain('not a report')
  })

  it('names the engine-floor capabilities and offers no control that would bind one', async () => {
    await openStage()
    const block = capabilityRow('authoring').closest('.se-blk') as HTMLElement
    const floor = within(block).getByText(B.capabilities_the_engine_always_runs)
    const disclosure = floor.closest('details') as HTMLElement
    expect(disclosure).not.toBeNull()
    for (const capability of ENGINE_FLOOR_CAPABILITIES) {
      expect(disclosure.textContent).toContain(capability)
    }
    // Refused rather than ignored, which is the whole reason they are named — and
    // nothing inside offers to try it.
    expect(disclosure.textContent).toContain('refused')
    expect(disclosure.querySelectorAll('button, input, textarea')).toHaveLength(0)
    // Nor is any floor capability given a row of its own.
    for (const capability of ENGINE_FLOOR_CAPABILITIES) {
      expect(panel().querySelector(`.se-setting[data-capability="${capability}"]`)).toBeNull()
    }
  })
})

describe('the transport chooser and the fields each transport accepts', () => {
  it('offers exactly the transports the engine projected', async () => {
    await openStage()
    const group = within(capabilityRow('authoring')).getByRole('group', {
      name: B.how_this_capability_is_reached.replace('{{capability}}', 'authoring'),
    })
    expect(within(group).getAllByRole('button').map((b) => b.textContent)).toEqual(TRANSPORTS)
  })

  it('offers no transport at all when the registry projected none', async () => {
    // Rather than a list this side invented. A chooser over a hard-coded vocabulary
    // is how a form comes to offer what the write door refuses.
    await openStage({ registry: { body: registry({ transports: [] }) } })
    const authoring = capabilityRow('authoring')
    expect(within(authoring).queryAllByRole('button', { name: 'mcp' })).toHaveLength(0)
    const block = authoring.closest('.se-blk') as HTMLElement
    expect(block.textContent).toContain(B.the_engine_declared_no_transport)
  })

  it('offers none either when the payload carries no transports key at all', async () => {
    // The distinct case, and the one a hard-coded FALLBACK would slip through: an
    // older gateway omits the key rather than sending it empty, and `?? [...]` at the
    // read site would then quietly offer a vocabulary this pane made up. Asserted
    // separately because an empty array is present and a missing key is not, so a
    // test over the first cannot fail for the second.
    const payload = registry()
    delete (payload as { transports?: unknown }).transports
    await openStage({ registry: { body: payload } })
    const authoring = capabilityRow('authoring')
    for (const transport of TRANSPORTS) {
      expect(within(authoring).queryAllByRole('button', { name: transport })).toHaveLength(0)
    }
    expect((authoring.closest('.se-blk') as HTMLElement).textContent).toContain(
      B.the_engine_declared_no_transport,
    )
  })

  it('collects no command, environment or deadline while the transport is the builtin', async () => {
    await openStage()
    const authoring = capabilityRow('authoring')
    expect(authoring.querySelector('textarea')).toBeNull()
    expect(authoring.querySelectorAll('input')).toHaveLength(0)
    expect(authoring.textContent).toContain(B.the_builtin_takes_no_command_or_environment)
  })

  it('collects them once a transport that runs a program is chosen', async () => {
    await openStage()
    fireEvent.click(within(capabilityRow('authoring')).getByRole('button', { name: 'command' }))
    const authoring = capabilityRow('authoring')
    expect(authoring.querySelector('textarea')).not.toBeNull()
    expect(authoring.textContent).toContain(B.the_command_to_run)
    expect(authoring.textContent).toContain(B.environment_entries)
    expect(authoring.textContent).toContain(B.the_timeout_in_seconds)
    // And the fields belong to THIS capability only.
    expect(capabilityRow('validation_rules').querySelector('textarea')).toBeNull()
  })

  it('names the stored environment entries without ever showing a value', async () => {
    await openStage({
      config: {
        body: snapshot({
          capabilities: {
            authoring: { transport: 'mcp', command: ['srv'], env: { TOKEN: 'secret-value' } },
          },
        }),
      },
      capabilities: {
        body: capabilities([
          row({ capability: 'analysis' }),
          row({
            capability: 'authoring',
            transport: 'mcp',
            configured: true,
            declared_at: 'capabilities.authoring',
            program: 'srv',
            reachable: true,
          }),
          row({ capability: 'validation_rules' }),
        ]),
      },
    })
    const authoring = capabilityRow('authoring')
    expect(authoring.textContent).toContain('TOKEN')
    // The value is in the document and is deliberately not put on screen: nothing
    // here echoes back a credential an operator wrote once.
    expect(authoring.textContent).not.toContain('secret-value')
    for (const field of authoring.querySelectorAll('input, textarea')) {
      expect((field as HTMLInputElement).value).not.toContain('secret-value')
    }
  })
})

/** Fill *capability*'s row for an external binding, and stage it. */
function bind(capability: string, command: string, env?: [string, string], timeout?: string) {
  fireEvent.click(within(capabilityRow(capability)).getByRole('button', { name: 'command' }))
  const textarea = capabilityRow(capability).querySelector('textarea') as HTMLTextAreaElement
  fireEvent.change(textarea, { target: { value: command } })
  if (env) {
    fireEvent.click(
      within(capabilityRow(capability)).getByRole('button', { name: B.add_an_environment_entry }),
    )
    const name = within(capabilityRow(capability)).getByLabelText(
      B.an_environment_name.replace('{{capability}}', capability),
    )
    fireEvent.change(name, { target: { value: env[0] } })
    fireEvent.change(
      within(capabilityRow(capability)).getByLabelText(
        B.an_environment_value.replace('{{capability}}', capability),
      ),
      { target: { value: env[1] } },
    )
  }
  if (timeout !== undefined) {
    fireEvent.change(
      within(capabilityRow(capability)).getByLabelText(B.the_timeout_in_seconds),
      { target: { value: timeout } },
    )
  }
  fireEvent.click(within(capabilityRow(capability)).getByRole('button', { name: B.stage_this_binding }))
}

/** The patch the review card is displaying, parsed out of the disclosure. */
function shownPatch(): Record<string, unknown> {
  const shown = panel().querySelector('.se-gpatch')
  expect(shown).not.toBeNull()
  return JSON.parse(String(shown?.textContent)) as Record<string, unknown>
}

function review() {
  fireEvent.click(
    within(panel()).getByRole('button', { name: B.review_the_exact_binding_change }),
  )
}

describe('staging a binding, and what the confirmation says it would write', () => {
  it('composes a patch addressing only the capability, and states the consequence', async () => {
    await openStage()
    bind('authoring', 'my-writer\n--json', ['REGION', 'eu'], '60')
    expect(capabilityRow('authoring').getAttribute('data-staged')).toBe('true')
    // The count the stage badge reads and the count this form states are one value.
    const block = capabilityRow('authoring').closest('.se-blk') as HTMLElement
    expect(block.textContent).toContain(B.unwritten_binding_changes)
    review()
    expect(shownPatch()).toEqual({
      capabilities: {
        authoring: {
          transport: 'command',
          command: ['my-writer', '--json'],
          env: { REGION: 'eu' },
          timeout_s: 60,
        },
      },
    })
    // Plain language first, and the two consequences a patch cannot carry: binding a
    // capability to an external program, and authorising commands to run. Declared
    // by KIND, so the same grant from any other form reads as the same act.
    expect(panel().textContent).toContain(
      B.edit_binds_the_capability_to_a_program
        .replace('{{capability}}', 'authoring')
        .replace('{{transport}}', 'command')
        .replace('{{program}}', 'my-writer'),
    )
    expect(panel().textContent).toContain(R.binds_a_capability_to_an_external_program)
    expect(panel().textContent).toContain(R.authorises_commands_to_run)
    // And the section is one the agent's write tool is fenced out of, which is why an
    // operator is the one confirming.
    expect(panel().textContent).toContain(
      R.only_an_operator_confirmation_writes_this.replace('{{path}}', 'capabilities'),
    )
  })

  it('writes a return to the builtin as a deletion, with neither consequence claimed', async () => {
    await openStage({
      config: {
        body: snapshot({
          capabilities: { authoring: { transport: 'command', command: ['old'], timeout_s: 30 } },
        }),
      },
      capabilities: {
        body: capabilities([
          row({ capability: 'analysis' }),
          row({
            capability: 'authoring',
            transport: 'command',
            configured: true,
            declared_at: 'capabilities.authoring',
            program: 'old',
            reachable: true,
          }),
          row({ capability: 'validation_rules' }),
        ]),
      },
    })
    fireEvent.click(within(capabilityRow('authoring')).getByRole('button', { name: 'builtin' }))
    fireEvent.click(
      within(capabilityRow('authoring')).getByRole('button', {
        name: B.stage_the_return_to_the_builtin,
      }),
    )
    review()
    // A deletion of the whole entry, which is what writes no command, environment or
    // timeout for a builtin — an object write would merge and leave the stored
    // command in place for the door to refuse.
    expect(shownPatch()).toEqual({ capabilities: { authoring: null } })
    expect(panel().textContent).toContain(
      B.edit_returns_the_capability_to_its_builtin.replace('{{capability}}', 'authoring'),
    )
    // Removing a binding grants nothing, so stating "authorises commands to run" here
    // would teach a reader to discount the sentence when it is true.
    expect(panel().textContent).not.toContain(R.authorises_commands_to_run)
    expect(panel().textContent).not.toContain(R.binds_a_capability_to_an_external_program)
  })

  it('offers no return to the builtin for a capability nothing declares', async () => {
    // With no stored entry the "return" is a deletion of a key that is not there: the
    // patch would be a no-op and the card's sentence would say a command, environment
    // entries and a timeout are removed when none were ever declared. Refused with
    // the reason on screen, because a disabled control with no reason leaves an
    // operator with no next act.
    await openStage()
    const row = capabilityRow('authoring')
    expect(
      within(row).getByRole('button', { name: B.stage_the_return_to_the_builtin }),
    ).toBeDisabled()
    expect(
      within(row).getByText(B.nothing_is_declared_so_there_is_nothing_to_return),
    ).toBeTruthy()
    expect(
      within(panel()).getByRole('button', { name: B.review_the_exact_binding_change }),
    ).toBeDisabled()
  })

  it('offers it for a capability that IS declared, so the refusal is not universal', async () => {
    await openStage({
      config: {
        body: snapshot({
          capabilities: { authoring: { transport: 'command', command: ['old'] } },
        }),
      },
      capabilities: {
        body: capabilities([
          row({ capability: 'analysis' }),
          row({
            capability: 'authoring',
            transport: 'command',
            configured: true,
            declared_at: 'capabilities.authoring',
            program: 'old',
            reachable: true,
          }),
          row({ capability: 'validation_rules' }),
        ]),
      },
    })
    fireEvent.click(within(capabilityRow('authoring')).getByRole('button', { name: 'builtin' }))
    const control = within(capabilityRow('authoring')).getByRole('button', {
      name: B.stage_the_return_to_the_builtin,
    })
    expect(control).toBeEnabled()
    expect(
      within(capabilityRow('authoring')).queryByText(
        B.nothing_is_declared_so_there_is_nothing_to_return,
      ),
    ).toBeNull()
  })

  it('reports the same quantity on the badge when the read behind it is refused', async () => {
    // One rebind writes four leaves and is ONE act. The count reported while a read
    // is refused has to be the same quantity the main return reports, or the stage
    // badge jumps from one to four the moment a refetch fails, with nothing staged in
    // between. And the edits have to still be THERE to report: a refused read renders
    // no row, so reconciling the staged edits against the rendered rows would discard
    // them, and every count would be a truthful zero over destroyed work.
    const client = await openStage({
      capabilities: [
        { body: capabilities(AUTHORING.map((capability) => row({ capability }))) },
        { status: 503, body: { code: 'unavailable', error: 'gone' } },
      ],
    })
    bind('authoring', 'my-writer\n--json', ['REGION', 'eu'], '60')
    const authoringTab = screen.getByRole('tab', { name: new RegExp(`^${C.stage_authoring}`) })
    await waitFor(() => expect(authoringTab).toHaveTextContent(/1/))
    await client.invalidateQueries({ queryKey: QK.capabilities })
    await within(panel()).findByText(B.could_not_read_the_capability_bindings)
    expect(authoringTab).toHaveTextContent(/1/)
    expect(authoringTab.textContent).not.toMatch(/4/)
  })

  it('sends exactly the patch the disclosure showed, and re-reads rather than adopting it', async () => {
    await openStage()
    bind('authoring', 'my-writer')
    review()
    const shown = shownPatch()
    fireEvent.click(within(panel()).getByRole('button', { name: B.write_the_binding }))
    await waitFor(() =>
      expect(calls.some((call) => call.method === 'PUT')).toBe(true),
    )
    const put = calls.filter((call) => call.method === 'PUT').at(-1)
    expect((put?.body as { patch: unknown }).patch).toEqual(shown)
    // The reply's merged document is not adopted: both reads are re-issued, because
    // this pane's authority on what is persisted is a fresh read.
    await waitFor(() =>
      expect(panel().textContent).toContain(B.wrote_the_binding_and_re_read_it),
    )
    const reads = calls.filter((call) => call.url.includes('/config/capabilities')).length
    expect(reads).toBeGreaterThan(1)
  })

  it('re-staging replaces the capability paths rather than adding to them', async () => {
    await openStage()
    bind('authoring', 'first', ['GONE', 'x'])
    // The environment name is dropped and the command changed, then staged again.
    fireEvent.click(
      within(capabilityRow('authoring')).getByRole('button', {
        name: B.remove_this_environment_entry,
      }),
    )
    const textarea = capabilityRow('authoring').querySelector('textarea') as HTMLTextAreaElement
    fireEvent.change(textarea, { target: { value: 'second' } })
    fireEvent.click(
      within(capabilityRow('authoring')).getByRole('button', { name: B.stage_this_binding }),
    )
    review()
    // `GONE` was never stored, so nothing else would ever remove it: an additive
    // re-stage would leave the patch carrying an entry the form no longer shows.
    expect(shownPatch()).toEqual({
      capabilities: { authoring: { transport: 'command', command: ['second'] } },
    })
  })

  it('withdraws one capability change without touching another', async () => {
    await openStage()
    bind('authoring', 'writer')
    bind('validation_rules', 'checker')
    fireEvent.click(
      within(capabilityRow('authoring')).getByRole('button', {
        name: B.withdraw_this_binding_change,
      }),
    )
    review()
    expect(Object.keys(shownPatch().capabilities as object)).toEqual(['validation_rules'])
  })
})

describe('a refusal lands on the field it names, with the entry retained', () => {
  it('renders the engine reason beside the command and keeps what was typed', async () => {
    await openStage({
      configWrite: {
        status: 422,
        body: {
          code: 'config_invalid',
          error: 'capabilities.authoring.command[0]: expected a non-empty string',
        },
      },
    })
    bind('authoring', 'my-writer')
    review()
    fireEvent.click(within(panel()).getByRole('button', { name: B.write_the_binding }))
    await waitFor(() =>
      expect(
        capabilityRow('authoring').querySelector('[data-refusal="command"]'),
      ).not.toBeNull(),
    )
    expect(
      capabilityRow('authoring').querySelector('[data-refusal="command"]')?.textContent,
    ).toBe('expected a non-empty string')
    // Nothing was written, so the row keeps the operator's entry to be corrected and
    // the staged change is still there to send again.
    const textarea = capabilityRow('authoring').querySelector('textarea') as HTMLTextAreaElement
    expect(textarea.value).toBe('my-writer')
    expect(capabilityRow('authoring').getAttribute('data-staged')).toBe('true')
    expect(panel().textContent).toContain(B.nothing_was_written_so_rows_are_stored)
    // And the refusal is not attributed to a field it does not name.
    expect(capabilityRow('authoring').querySelector('[data-refusal="transport"]')).toBeNull()
    expect(capabilityRow('validation_rules').querySelector('[data-refusal]')).toBeNull()
  })
})

describe('a read the engine refused', () => {
  it('states the failure and does not present every capability as builtin', async () => {
    // The route answers 422 with NO `capabilities` key when the stored section cannot
    // be resolved. An all-builtin list is what an UNCONFIGURED document legitimately
    // returns — the same shape, the opposite fact — so falling back to it would show
    // a refused section as a clean one.
    stubSpecEngineFetch(
      {
        registry: { body: registry() },
        capabilities: {
          status: 422,
          body: {
            code: 'capabilities_unreadable',
            error: 'capabilities.phase_gates: engine-floor capability cannot be bound',
          },
        },
        config: { body: snapshot({}) },
      },
      { record: calls },
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
    await screen.findByRole('tablist', { name: C.configuration_stages })
    fireEvent.click(screen.getByRole('tab', { name: new RegExp(`^${C.stage_authoring}`) }))
    await waitFor(() =>
      expect(panel().textContent).toContain(B.could_not_read_the_capability_bindings),
    )
    expect(panel().textContent).toContain(B.a_failed_read_is_not_every_capability_builtin)
    expect(panel().textContent).toContain('capabilities_unreadable')
    // Not one row, for any capability: a row would be a reading nobody obtained.
    expect(panel().querySelectorAll('.se-setting[data-capability]')).toHaveLength(0)
    for (const capability of AUTHORING) {
      expect(panel().querySelector(`.se-setting[data-capability="${capability}"]`)).toBeNull()
    }
  })

  it('names a capability the projection places here that the read did not answer for', async () => {
    // The union of the stages must stay the whole delegable vocabulary even when the
    // two reads disagree, so an unanswered capability is stated rather than dropped.
    await openStage({
      capabilities: { body: capabilities([row({ capability: 'analysis' })]) },
    })
    expect(capabilityRow('authoring').textContent).toContain(
      B.the_read_answered_no_binding_for_this.replace('{{capability}}', 'authoring'),
    )
    expect(capabilityRow('authoring').querySelector('button')).toBeNull()
  })
})
