/**
 * The engine operations panel, and the one thing a typecheck cannot pin: that it
 * is REACHABLE.
 *
 * A probe that severed the shell button's onClick left the panel unopenable, and
 * neither `tsc` nor `eslint` objected -- the component still compiled, still had
 * every prop typed, and would have passed every test it had while no operator
 * could get to the stop control. So the first test here opens the panel through
 * the button an operator actually clicks rather than by rendering the component
 * directly.
 *
 * The rest assert what the panel must not do: infer a value's origin, claim a
 * release resumes work, save an edit without going through the engine's write
 * path, or let a REFUSED save look like a saved one.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import React from 'react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

vi.mock('../app-sdk/ChatEmbed', () => ({ default: () => <div data-testid="chat-embed" /> }))

import SpecBuilderPage from '../apps/spec-builder/SpecBuilderPage'

let queryClient: QueryClient

/** A value whose effective number EQUALS its bundled default but which is an
 *  explicit app-level override. The one case where inferring the origin by
 *  comparing value to default gives the wrong answer. */
const CONFIG_BODY = {
  scope: { project: null, source: null },
  settings: {
    'concurrency.global_max_runs': {
      key: 'concurrency.global_max_runs',
      value: 4,
      origin: 'app_config',
      declared_at: 'concurrency.global_max_runs',
      is_default: false,
      default: 4,
      summary: 'Runs at once.',
      kind: 'int',
      scopes: ['app'],
      minimum: 1,
      maximum: null,
      choices: [],
    },
    // Overridable at PROJECT scope only, so the app-scope panel must not offer a
    // control for it: an edit collected here would be refused by the write path.
    'delivery.auto_integrate': {
      key: 'delivery.auto_integrate',
      value: false,
      origin: 'bundled_default',
      declared_at: '',
      is_default: true,
      default: false,
      summary: 'Integrate without asking.',
      kind: 'bool',
      scopes: ['project'],
      minimum: null,
      maximum: null,
      choices: [],
    },
    'notify.channel': {
      key: 'notify.channel',
      value: 'dashboard',
      origin: 'bundled_default',
      declared_at: '',
      is_default: true,
      default: 'dashboard',
      summary: 'Where notifications go.',
      kind: 'str',
      scopes: ['app', 'project'],
      minimum: null,
      maximum: null,
      choices: [],
    },
  },
  domains: {
    sources: {
      gh: {
        enabled: true,
        poll: ['gh', 'issue', 'list'],
        autonomy: { external: { feature: 'authoring' } },
      },
    },
    // Two profiles, the second named so its dotted key reads like a descendant of
    // the first's review role -- the collision a string-prefix match falls for.
    cost_profiles: {
      thrifty: { roles: { review: { model: 'auto' } } },
      'thrifty.roles.review': { roles: { review: { model: 'auto' } } },
    },
  },
  domain_sections: ['sources', 'projects'],
  domain_editors: [
    { domain: 'autonomy', path: 'sources.*.autonomy', editable: true, fields: [], reason_code: '' },
    {
      domain: 'watch_sources',
      path: 'sources',
      editable: true,
      fields: ['enabled'],
      reason_code: 'argv_read_only',
    },
    {
      domain: 'role_assignments',
      path: 'cost_profiles.*.roles',
      editable: true,
      fields: [],
      reason_code: '',
    },
    {
      domain: 'notification_channels',
      path: 'notify.channel',
      editable: true,
      fields: [],
      reason_code: '',
    },
    {
      domain: 'workflow',
      path: 'workflow',
      editable: false,
      fields: [],
      reason_code: 'executes_argv',
    },
    {
      domain: 'programs',
      path: 'programs',
      editable: false,
      fields: [],
      reason_code: 'host_assertion',
    },
  ],
  config_only_paths: ['sources', 'workflow'],
  catalogs: {
    autonomy_levels: ['authoring', 'execution', 'delivery', 'integration'],
    submitter_classes: ['maintainer', 'member', 'contributor', 'external'],
    spec_types: ['feature', 'bugfix', 'quick'],
    roles: ['review'],
    effort_levels: ['low', 'medium', 'high'],
    wildcard: '*',
  },
}

/** A MIXED workflow: one stage overridden by the project, one still the preset's,
 *  one nobody defines. The case that separates a real per-stage rendering from a
 *  single label per workflow. */
const ORIGINS_BODY = {
  scope: { project: null },
  preset: {
    name: 'git-pull-request',
    origin: 'app_config',
    declared_at: 'workflow.preset',
    bundled: true,
  },
  stages: [
    {
      stage: 'isolate',
      source: 'bundled_preset',
      from_preset: true,
      bundled: true,
      preset: 'git-pull-request',
      declared_at: 'workflow.preset',
      commands: 2,
      skipped: false,
      summary: "isolate: from bundled preset 'git-pull-request' (2 command(s), at workflow.preset)",
    },
    {
      stage: 'submit',
      source: 'project_override',
      from_preset: false,
      bundled: false,
      preset: '',
      declared_at: 'projects.web.workflow.stages.submit',
      commands: 1,
      skipped: false,
      summary:
        'submit: overridden by this project (1 command(s), at projects.web.workflow.stages.submit)',
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
    },
  ],
}

const RUN_SPEND_BODY = {
  run_id: 'r1',
  project: 'web',
  spec: 'checkout-flow',
  state: 'awaiting_review',
  source: 'github',
  credits: 12.5,
  metered_credits: 10,
  declared_credits: 2.5,
  turns: 4,
  sessions: 2,
  recorded_credits: 12.5,
  ceiling: { value: 20, origin: 'app_config', declared_at: 'budget.run_ceiling_credits' },
}

const RELEASED_SWITCH = {
  switch: {
    engaged: false,
    initiator: '',
    reason: '',
    engaged_ts: '',
    unreadable: false,
    description: 'kill switch: released',
  },
  stoppable: [],
  stoppable_credits: 0,
}

const ENGAGED_SWITCH = {
  switch: {
    engaged: true,
    initiator: 'dashboard',
    reason: 'runaway wave',
    engaged_ts: '2026-01-01T00:00:00+00:00',
    unreadable: false,
    description: 'kill switch: engaged',
  },
  stoppable: [],
  stoppable_credits: 0,
}

const QUEUE_BODY = {
  entries: [
    {
      run_id: 'r1',
      project: 'web',
      spec: 'checkout-flow',
      state: 'awaiting_review',
      waiting_on: 'requirements_review',
      waiting_s: 60,
      cost_credits: 12.5,
      gate: 'requirements',
    },
  ],
  total_credits: 12.5,
}

/** Route each engine GET to its body; everything else is an empty spec list.
 *
 *  `writes` collects every non-GET so a test can assert not just that a save
 *  happened but that it went to the engine's config PUT with the patch shape the
 *  write path merges. */
function stubEngineFetch(
  switchBody: unknown = RELEASED_SWITCH,
  options: { writes?: { url: string; init?: RequestInit }[]; refuseWrite?: string } = {},
) {
  const fetchMock = vi.fn((url: string, init?: RequestInit) => {
    if (init && init.method && init.method !== 'GET') {
      options.writes?.push({ url, init })
      if (options.refuseWrite && url.includes('/engine/config')) {
        return Promise.resolve({
          ok: false,
          status: 422,
          json: () => Promise.resolve({ code: 'config_invalid', error: options.refuseWrite }),
          text: () => Promise.resolve(''),
        })
      }
      return Promise.resolve({
        ok: true,
        status: 200,
        text: () => Promise.resolve(JSON.stringify({ ok: true })),
      })
    }
    const body = url.includes('/engine/workflow-origins')
      ? ORIGINS_BODY
      : url.includes('/engine/run-spend')
        ? RUN_SPEND_BODY
        : url.includes('/engine/config')
          ? CONFIG_BODY
          : url.includes('/engine/kill-switch')
            ? switchBody
            : url.includes('/engine/queue')
              ? QUEUE_BODY
              : { specs: [] }
    return Promise.resolve({
      ok: true,
      status: 200,
      text: () => Promise.resolve(JSON.stringify(body)),
    })
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

function renderPage() {
  return render(
    <MemoryRouter>
      <QueryClientProvider client={queryClient}>
        <SpecBuilderPage />
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

beforeEach(() => {
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  localStorage.clear()
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe('EngineOpsPanel', () => {
  it('is reachable from the app shell', async () => {
    stubEngineFetch()
    renderPage()
    const open = await screen.findByRole('button', { name: 'Engine operations' })
    await userEvent.click(open)
    // The panel's own heading, not the button's label: the button exists either
    // way, and asserting on it would pass with the panel wired to nothing.
    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'Stop control' })).toBeInTheDocument()
    })
  })

  it('reports the origin the engine gave, not one inferred from the default', async () => {
    stubEngineFetch()
    renderPage()
    await userEvent.click(await screen.findByRole('button', { name: 'Engine operations' }))
    await waitFor(() => {
      expect(screen.getByText('concurrency.global_max_runs')).toBeInTheDocument()
    })
    // value === default here, so a panel that inferred the origin would label
    // this "shipped default" and tell the operator the override does not exist.
    expect(
      screen.getByText(/app configuration \(concurrency\.global_max_runs\)/),
    ).toBeInTheDocument()
  })

  it('names the blast radius before the stop is thrown', async () => {
    stubEngineFetch()
    renderPage()
    await userEvent.click(await screen.findByRole('button', { name: 'Engine operations' }))
    await waitFor(() => {
      expect(screen.getByText(/Engaging parks 0 run\(s\)/)).toBeInTheDocument()
    })
    expect(screen.getByRole('button', { name: 'Stop all unattended work' })).toBeInTheDocument()
  })

  it('says a release resumes nothing when the switch is engaged', async () => {
    stubEngineFetch(ENGAGED_SWITCH)
    renderPage()
    await userEvent.click(await screen.findByRole('button', { name: 'Engine operations' }))
    await waitFor(() => {
      expect(screen.getByText(/Unattended work is stopped, by dashboard/)).toBeInTheDocument()
    })
    // The sentence an operator most often assumes the other way round.
    expect(screen.getByText(/It does not resume anything that was parked/)).toBeInTheDocument()
    expect(
      screen.getByRole('button', { name: 'Allow unattended work again' }),
    ).toBeInTheDocument()
  })

  it('shows the credits each waiting run consumed', async () => {
    stubEngineFetch()
    renderPage()
    await userEvent.click(await screen.findByRole('button', { name: 'Engine operations' }))
    await waitFor(() => {
      expect(screen.getByText('checkout-flow')).toBeInTheDocument()
    })
    expect(screen.getByText('12.5')).toBeInTheDocument()
  })

  it('names a domain it has no editor for rather than hiding it', async () => {
    stubEngineFetch()
    renderPage()
    await userEvent.click(await screen.findByRole('button', { name: 'Engine operations' }))
    await waitFor(() => {
      expect(screen.getByText(/projects: none configured/)).toBeInTheDocument()
    })
  })
})

describe('EngineConfigEditor', () => {
  /** Open the panel and wait for the editor's own save control. */
  async function openEditor() {
    renderPage()
    await userEvent.click(await screen.findByRole('button', { name: 'Engine operations' }))
    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Save configuration' })).toBeInTheDocument()
    })
  }

  it('sends one edit through the engine config PUT', async () => {
    const writes: { url: string; init?: RequestInit }[] = []
    stubEngineFetch(RELEASED_SWITCH, { writes })
    await openEditor()

    const field = screen.getByLabelText('concurrency.global_max_runs')
    await userEvent.clear(field)
    await userEvent.type(field, '7')
    await userEvent.click(screen.getByRole('button', { name: 'Save configuration' }))

    // The write path, and the patch shape the engine merges. A panel that posted
    // somewhere else, or flattened the dotted key into one string, would leave
    // putConfig with no caller -- which is what it had.
    await waitFor(() => expect(writes.length).toBe(1))
    expect(writes[0].url).toContain('/engine/config')
    expect(writes[0].init?.method).toBe('PUT')
    expect(JSON.parse(String(writes[0].init?.body))).toEqual({
      patch: { concurrency: { global_max_runs: 7 } },
    })
  })

  it('holds an emptied role model rather than sending a write the engine refuses', async () => {
    // The engine requires a non-empty model for a declared role, and clearing the
    // field is how an operator reaches for "stop pinning this" -- which is what
    // the reset control does. So the panel must not offer the empty save at all:
    // offering a value the write path refuses is the defect that made role effort
    // a picker over the engine's own levels.
    const writes: { url: string; init?: RequestInit }[] = []
    stubEngineFetch(RELEASED_SWITCH, { writes })
    await openEditor()

    const model = screen.getByLabelText('cost_profiles.thrifty.roles.review.model')
    await userEvent.clear(model)

    const save = screen.getByRole('button', { name: 'Save configuration' })
    expect(save).toBeDisabled()
    screen.getByText(/A role needs a model/i)

    // And typing one re-enables it, so the guard is about the value and not a
    // one-way latch that strands the form.
    await userEvent.type(model, 'claude-sonnet-5')
    expect(screen.getByRole('button', { name: 'Save configuration' })).toBeEnabled()
    expect(writes.length).toBe(0)
  })

  it('lets this role be reset away, which is what the blank-model guard advises', async () => {
    // The guard tells the operator to reset the role, so that has to be reachable
    // AND has to resolve the guard: the blank edit is superseded by the delete
    // rather than left pending, or the advice would leave the save still blocked.
    const writes: { url: string; init?: RequestInit }[] = []
    stubEngineFetch(RELEASED_SWITCH, { writes })
    await openEditor()

    const model = screen.getByLabelText('cost_profiles.thrifty.roles.review.model')
    await userEvent.clear(model)
    expect(screen.getByRole('button', { name: 'Save configuration' })).toBeDisabled()

    await userEvent.click(
      screen.getByRole('button', { name: 'Stop pinning thrifty.review, so the role inherits' }),
    )
    await userEvent.click(screen.getByRole('button', { name: 'Save configuration' }))

    await waitFor(() => expect(writes.length).toBe(1))
    // null DELETES the assignment so the role inherits, rather than pinning a
    // value nobody chose. And the superseded blank model is not in the patch.
    expect(JSON.parse(String(writes[0].init?.body))).toEqual({
      patch: { cost_profiles: { thrifty: { roles: { review: null } } } },
    })
  })

  it('supersedes only edits genuinely inside the deleted role', async () => {
    // The case the two matchers disagree on. A profile NAMED "thrifty.roles.review"
    // produces a dotted key that reads like a descendant of the deleted role's
    // path, so a string-prefix match drops its pending edit; comparing path
    // SEGMENTS does not. Nothing in the schema forbids a dot in a profile name,
    // and a normal sibling would not show this -- both matchers handle that.
    const writes: { url: string; init?: RequestInit }[] = []
    stubEngineFetch(RELEASED_SWITCH, { writes })
    await openEditor()

    const lookalike = screen.getByLabelText(
      'cost_profiles.thrifty.roles.review.roles.review.model',
    )
    await userEvent.clear(lookalike)
    await userEvent.type(lookalike, 'kept-model')

    await userEvent.click(
      screen.getByRole('button', { name: 'Stop pinning thrifty.review, so the role inherits' }),
    )
    await userEvent.click(screen.getByRole('button', { name: 'Save configuration' }))

    await waitFor(() => expect(writes.length).toBe(1))
    const body = JSON.parse(String(writes[0].init?.body)) as {
      patch: { cost_profiles: Record<string, unknown> }
    }
    // The delete landed, and the lookalike profile's edit was NOT swallowed by it.
    expect(body.patch.cost_profiles.thrifty).toEqual({ roles: { review: null } })
    expect(body.patch.cost_profiles['thrifty.roles.review']).toEqual({
      roles: { review: { model: 'kept-model' } },
    })
  })

  it('resets a setting by deleting its key rather than writing the default', async () => {
    const writes: { url: string; init?: RequestInit }[] = []
    stubEngineFetch(RELEASED_SWITCH, { writes })
    await openEditor()

    await userEvent.click(
      screen.getByRole('button', {
        name: 'Return concurrency.global_max_runs to its shipped default',
      }),
    )
    await userEvent.click(screen.getByRole('button', { name: 'Save configuration' }))

    // null DELETES the key, which returns the setting to its bundled default.
    // Writing the default's CURRENT value would pin it, so the two are not
    // interchangeable -- and this setting's value already equals its default,
    // which is exactly where the difference is invisible in the UI.
    await waitFor(() => expect(writes.length).toBe(1))
    expect(JSON.parse(String(writes[0].init?.body))).toEqual({
      patch: { concurrency: { global_max_runs: null } },
    })
  })

  it('writes an autonomy level at the source path the engine validates', async () => {
    const writes: { url: string; init?: RequestInit }[] = []
    stubEngineFetch(RELEASED_SWITCH, { writes })
    await openEditor()

    // A themed combobox rather than a native select, so the pick is a click on the
    // trigger and then on the option.
    fireEvent.click(
      screen.getByRole('combobox', { name: 'sources.gh.autonomy.external.feature' }),
    )
    fireEvent.click(await screen.findByRole('option', { name: 'delivery' }))
    await userEvent.click(screen.getByRole('button', { name: 'Save configuration' }))

    await waitFor(() => expect(writes.length).toBe(1))
    expect(JSON.parse(String(writes[0].init?.body))).toEqual({
      patch: { sources: { gh: { autonomy: { external: { feature: 'delivery' } } } } },
    })
  })

  it('shows a refused write with the engine reason and does not look saved', async () => {
    const writes: { url: string; init?: RequestInit }[] = []
    stubEngineFetch(RELEASED_SWITCH, {
      writes,
      refuseWrite: 'sources.gh.screening: a per-class opt-out may not disable screening for all',
    })
    await openEditor()

    const field = screen.getByLabelText('concurrency.global_max_runs')
    await userEvent.clear(field)
    await userEvent.type(field, '9')
    await userEvent.click(screen.getByRole('button', { name: 'Save configuration' }))

    // The ENGINE's own reason, naming the path it objected to.
    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toContain('may not disable screening for all')
    // And the form still reads as unsaved: a refusal that cleared the edits would
    // send the operator away believing the value landed.
    expect(screen.getByText('Unsaved edits are pending.')).toBeInTheDocument()
    expect(screen.queryByText(/Saved\./)).not.toBeInTheDocument()
    expect((screen.getByLabelText('concurrency.global_max_runs') as HTMLInputElement).value).toBe(
      '9',
    )
  })

  it('offers effort as a pick from the engine levels, not free text', async () => {
    const writes: { url: string; init?: RequestInit }[] = []
    stubEngineFetch(RELEASED_SWITCH, { writes })
    await openEditor()

    // The write path validates effort against a fixed list, so a text field here
    // would collect a value the engine refuses -- and it did, until the levels
    // started travelling with the config read.
    fireEvent.click(screen.getByRole('combobox', { name: 'cost_profiles.thrifty.roles.review.effort' }))
    fireEvent.click(await screen.findByRole('option', { name: 'high' }))
    await userEvent.click(screen.getByRole('button', { name: 'Save configuration' }))

    await waitFor(() => expect(writes.length).toBe(1))
    expect(JSON.parse(String(writes[0].init?.body))).toEqual({
      patch: { cost_profiles: { thrifty: { roles: { review: { effort: 'high' } } } } },
    })
  })

  it('renders each workflow stage at its own layer, from the engine summary', async () => {
    stubEngineFetch()
    await openEditor()

    // A MIXED workflow. A single label per workflow would get two of these three
    // rows wrong, and a value comparison would call the override inherited.
    await waitFor(() => {
      expect(screen.getByText(/overridden by this project/)).toBeInTheDocument()
    })
    expect(screen.getByText(/from bundled preset 'git-pull-request'/)).toBeInTheDocument()
    // A stage nobody defines SKIPS. Omitting it would let an operator assume it
    // runs the preset's commands.
    expect(screen.getByText(/not configured, so this stage is skipped/)).toBeInTheDocument()
  })

  it('says why a domain it will not write is read-only', async () => {
    stubEngineFetch()
    await openEditor()
    await waitFor(() => {
      expect(
        screen.getAllByText(/Command lines the engine runs on a run's workspace/).length,
      ).toBeGreaterThan(0)
    })
    // The reason travels per domain, so the program minimums do not borrow the
    // workflow's explanation.
    expect(screen.getByText(/Assertions about this host that the Doctor checks/)).toBeInTheDocument()
  })

  it('offers no control for a setting this scope cannot write', async () => {
    stubEngineFetch()
    await openEditor()
    // The registry says delivery.auto_integrate is overridable at PROJECT scope
    // only. A control here would collect an edit the engine's write path refuses,
    // so the row is labelled instead -- and the label is what tells the operator
    // why the field they expected is missing.
    expect(screen.queryByLabelText('delivery.auto_integrate')).not.toBeInTheDocument()
    expect(screen.getByText('(not writable at this scope)')).toBeInTheDocument()
    // The one beside it IS writable, so the absence above is a decision rather
    // than a panel that renders no controls at all.
    expect(screen.getByLabelText('notify.channel')).toBeInTheDocument()
  })
})

describe('run spend detail', () => {
  it('shows one run credits from the engine attribution, not a browser sum', async () => {
    stubEngineFetch()
    renderPage()
    await userEvent.click(await screen.findByRole('button', { name: 'Engine operations' }))
    await userEvent.click(
      await screen.findByRole('button', { name: 'Spend detail for run r1' }),
    )
    await waitFor(() => {
      expect(screen.getByText(/12.5 credits consumed, against a ceiling of 20/)).toBeInTheDocument()
    })
    // The split is what shows out-of-session provider spend is INSIDE the total
    // rather than beside it: 10 metered + 2.5 declared is the 12.5 above, and a
    // browser summing only session turns would have reported 10.
    expect(
      screen.getByText(/10 metered in sessions, 2.5 declared by providers outside a session/),
    ).toBeInTheDocument()
    // The ceiling's own origin, from the engine.
    expect(screen.getByText(/The ceiling comes from app configuration/)).toBeInTheDocument()
  })
})
