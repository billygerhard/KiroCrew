/**
 * The setup flow: what it refuses to write, and what it will not guess.
 *
 * The properties under test, each a requirement rather than a rendering:
 *
 *   - **First run leads here.** An unconfigured engine offers the assistant rather
 *     than an empty form, and the flow's first step is a real inspection call.
 *   - **Nothing is written before the apply.** Inspecting and planning are their
 *     own routes; a test drives both and asserts no write went out.
 *   - **The apply refuses without a named approver**, twice over: the button is
 *     disabled until the field holds something, AND the engine's
 *     `approver-required` refusal is rendered as the sentence that names the
 *     missing field. The disabled button is a rendering; the refusal is the
 *     guarantee.
 *   - **The approver is not the session.** The field is typed, and what is sent is
 *     what was typed.
 *   - **A plan is a claim about its inputs.** Changing any answer discards the plan,
 *     because sending the old `plan_id` with new answers is exactly the stale apply
 *     the engine refuses — and a panel that kept it would turn a correct refusal
 *     into a dead end.
 *   - **An unanswered rung is a state**, not a false: only answered rungs travel.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import SpecEnginePage from '../apps/spec-engine/SpecEnginePage'
import en from '../i18n/locales/en.json'

const T = en.apps.specEngine.setupFlowPanel
const P = en.apps.specEngine.specEnginePage

type Answer = { status?: number; body: unknown }

/** Every request the page made, so an assertion can read the body that was sent. */
const calls: Array<{ url: string; method: string; body: unknown }> = []

/** What `inspection_payload` returns for a project with a GitHub remote. */
function inspection(over: Record<string, unknown> = {}) {
  return {
    project: { name: 'acme', root: '/src/acme' },
    memory_consulted: false,
    evidence: [],
    inferences: [
      {
        subject: 'workflow.preset',
        value: 'git-pull-request',
        rationale: 'the origin remote is hosted on github',
        evidence: [{ located_at: '.git/config', excerpt: 'git@github.com:acme/widgets.git' }],
      },
    ],
    questions: [
      {
        subject: 'cost_profile',
        prompt: 'Which cost profile should this project use?',
        because: 'it decides how much money unattended work may spend',
        options: ['quality-first', 'budget'],
        answer_kind: 'choice',
      },
      {
        subject: 'autonomy.execution',
        prompt: 'May the engine run implementation tasks unattended?',
        because: 'each level is confirmed separately',
        options: [],
        answer_kind: 'confirmation',
      },
      {
        subject: 'autonomy.delivery',
        prompt: 'May the engine run your delivery workflow unattended?',
        because: 'each level is confirmed separately',
        options: [],
        answer_kind: 'confirmation',
      },
      {
        subject: 'tooling',
        prompt: 'What commands build and test this project?',
        because: 'no build or test entry point was named',
        options: [],
        answer_kind: 'confirmation',
      },
    ],
    offers: [
      {
        kind: 'workflow',
        name: 'git-pull-request',
        inference: {
          subject: 'workflow.preset',
          value: 'git-pull-request',
          rationale: 'the origin remote is hosted on github',
          evidence: [],
        },
        programs: ['git', 'gh'],
        commands: [{ stage: 'submit', argv: ['gh', 'pr', 'create'] }],
        prerequisites: { met: true, checks: [], unmet: [] },
      },
    ],
    prerequisites: { met: true, checks: [], unmet: [] },
    asked_subjects: ['autonomy.delivery', 'autonomy.execution', 'cost_profile'],
    confirmed_levels: ['execution', 'delivery'],
    autonomy_field: 'autonomy',
    ...over,
  }
}

/** What `SetupPlanEnvelope.to_json_object` returns. */
function envelope(over: Record<string, unknown> = {}) {
  return {
    plan_id: 'a'.repeat(64),
    project: { name: 'acme', root: '/src/acme' },
    inferences: [],
    answers_used: {},
    config_patch: { cost_profiles: { budget: { roles: {} } } },
    written_paths: ['cost_profiles.budget', 'projects.acme.cost_profile'],
    warnings: [],
    ...over,
  }
}

/** What `apply_payload` returns. */
function applied(over: Record<string, unknown> = {}) {
  return {
    applied: true,
    plan_id: 'a'.repeat(64),
    approver: 'colleague@example',
    project: { name: 'acme', root: '/src/acme' },
    written_paths: ['cost_profiles.budget'],
    config_patch: {},
    prerequisites: { met: true, checks: [], unmet: [] },
    notes: [],
    advisories: [],
    ...over,
  }
}

function stub(answers: { inspect?: Answer; plan?: Answer; apply?: Answer }) {
  vi.stubGlobal(
    'fetch',
    vi.fn((url: string, init?: RequestInit) => {
      const method = init?.method ?? 'GET'
      calls.push({ url, method, body: init?.body ? JSON.parse(String(init.body)) : undefined })
      let answer: Answer
      if (url.endsWith('/setup/inspect')) {
        answer = answers.inspect ?? { body: inspection() }
      } else if (url.endsWith('/setup/plan')) {
        answer = answers.plan ?? { body: envelope() }
      } else if (url.endsWith('/setup/apply')) {
        answer = answers.apply ?? { body: applied() }
      } else if (url.startsWith('/api/apps/spec-engine/config')) {
        // Unconfigured, which is what routes the page to the assistant.
        answer = {
          body: {
            configured: false,
            path: '/home/me/.kiro/crew/apps/spec-engine/config.json',
            document: {},
            elided: [],
            elided_marker: '<elided>',
            errors: [],
            advisories: [],
            config_only_paths: [],
          },
        }
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

function renderPage() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchInterval: false } },
  })
  return render(
    <QueryClientProvider client={client}>
      <SpecEnginePage />
    </QueryClientProvider>,
  )
}

/** Inspect a project, which is step 1 and the gate on everything after it. */
async function inspectProject() {
  renderPage()
  const path = await screen.findByLabelText(T.project_path)
  fireEvent.change(path, { target: { value: '/src/acme' } })
  fireEvent.click(screen.getByRole('button', { name: T.inspect_the_project }))
  return screen.findByRole('button', { name: T.show_the_exact_patch })
}

/** A complete, consistent answer set, entered through the controls. */
function answerEverything() {
  fireEvent.click(screen.getByRole('button', { name: 'budget' }))
  for (const rung of ['execution', 'delivery']) {
    const row = screen.getByText(rung).closest('.se-rung') as HTMLElement
    fireEvent.click(within(row).getByRole('button', { name: T.no }))
  }
  fireEvent.click(screen.getByRole('button', { name: T.approve }))
}

/** Scoped query, so a rung's own Yes/No is not confused with its neighbour's. */
function within(element: HTMLElement) {
  return {
    getByRole: (role: string, options: { name: string }) => {
      const matches = Array.from(element.querySelectorAll('button')).filter(
        (node) => node.textContent === options.name,
      )
      expect(matches.length, `${role} ${options.name}`).toBeGreaterThan(0)
      return matches[0]
    },
  }
}

/** The body of the last request sent to *path*. */
function lastBody(path: string): Record<string, unknown> {
  const sent = calls.filter((call) => call.url.endsWith(path))
  expect(sent.length).toBeGreaterThan(0)
  return sent[sent.length - 1].body as Record<string, unknown>
}

afterEach(() => {
  vi.unstubAllGlobals()
  calls.length = 0
})

describe('the flow', () => {
  it('is where an unconfigured engine lands, and its first step is a real call', async () => {
    stub({})
    await inspectProject()
    expect(screen.getByText(P.nothing_is_configured_yet)).toBeInTheDocument()
    expect(lastBody('/setup/inspect')).toEqual({ project: '/src/acme' })
    // The inferences arrive with the evidence behind them, which is what makes an
    // approval an approval of something checkable.
    expect(screen.getByText('.git/config')).toBeInTheDocument()
    expect(screen.getByText('git@github.com:acme/widgets.git')).toBeInTheDocument()
  })

  it('writes nothing while inspecting and planning', async () => {
    stub({})
    const plan = await inspectProject()
    answerEverything()
    fireEvent.click(plan)
    await waitFor(() => expect(calls.some((call) => call.url.endsWith('/setup/plan'))).toBe(true))
    expect(calls.some((call) => call.url.endsWith('/setup/apply'))).toBe(false)
    // And the plan shows the patch itself, not a summary of it.
    expect(await screen.findByText(/"cost_profiles"/)).toBeInTheDocument()
    expect(screen.getByText(/projects\.acme\.cost_profile/)).toBeInTheDocument()
  })

  it('sends only the rungs that were answered', async () => {
    stub({})
    const plan = await inspectProject()
    fireEvent.click(screen.getByRole('button', { name: 'budget' }))
    const row = screen.getByText('execution').closest('.se-rung') as HTMLElement
    fireEvent.click(within(row).getByRole('button', { name: T.yes }))
    fireEvent.click(plan)
    await waitFor(() => expect(calls.some((call) => call.url.endsWith('/setup/plan'))).toBe(true))
    // `delivery` is UNANSWERED, so it must be absent rather than false: the engine
    // refuses on absence and would read a false as a declined grant.
    const body = lastBody('/setup/plan') as { answers: { confirmations: object } }
    expect(body.answers.confirmations).toEqual({ execution: true })
    expect(screen.getAllByText(T.unanswered).length).toBe(1)
  })

  it('keeps the apply disabled until a plan and an approver both exist', async () => {
    stub({})
    const plan = await inspectProject()
    answerEverything()
    const apply = screen.getByRole('button', { name: T.apply_the_plan })
    expect(apply).toBeDisabled()
    expect(screen.getByText(T.compute_a_plan_first)).toBeInTheDocument()

    fireEvent.click(plan)
    await screen.findByText(new RegExp(T.recomputed_on_apply))
    expect(screen.getByRole('button', { name: T.apply_the_plan })).toBeDisabled()
    expect(screen.getByText(T.an_approver_is_required)).toBeInTheDocument()

    fireEvent.change(screen.getByLabelText(T.approver_identity), {
      target: { value: 'colleague@example' },
    })
    expect(screen.getByRole('button', { name: T.apply_the_plan })).toBeEnabled()
  })

  it('sends the approver that was typed, with the plan that was read', async () => {
    stub({})
    const plan = await inspectProject()
    answerEverything()
    fireEvent.click(plan)
    await screen.findByText(new RegExp(T.recomputed_on_apply))
    fireEvent.change(screen.getByLabelText(T.approver_identity), {
      target: { value: '  colleague@example  ' },
    })
    fireEvent.click(screen.getByRole('button', { name: T.apply_the_plan }))
    await waitFor(() => expect(calls.some((call) => call.url.endsWith('/setup/apply'))).toBe(true))
    const body = lastBody('/setup/apply') as { approver: string; plan_id: string }
    // The approver is who authorized the plan, not the session — and it is trimmed
    // rather than sent with the whitespace a field collects.
    expect(body.approver).toBe('colleague@example')
    expect(body.plan_id).toBe('a'.repeat(64))
    expect(await screen.findByText(new RegExp(T.wrote))).toBeInTheDocument()
  })

  it('discards the plan when an answer changes', async () => {
    stub({})
    const plan = await inspectProject()
    answerEverything()
    fireEvent.click(plan)
    await screen.findByText(new RegExp(T.recomputed_on_apply))
    // Changing an answer changes the plan identity, so the id on screen no longer
    // identifies what would be written. Keeping it would send an apply the engine
    // must refuse for a reason the operator could do nothing about.
    fireEvent.click(screen.getByRole('button', { name: 'quality-first' }))
    expect(screen.getByText(T.no_plan_has_been_computed_yet)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: T.apply_the_plan })).toBeDisabled()
  })

  it('names the refusal when the engine refuses the apply, and says nothing was written', async () => {
    stub({
      apply: {
        status: 409,
        body: {
          code: 'setup_refused',
          refused: 'approver-required',
          reason: 'ApproverRequired',
          error: 'applying a setup plan requires a non-empty approver',
        },
      },
    })
    const plan = await inspectProject()
    answerEverything()
    fireEvent.click(plan)
    await screen.findByText(new RegExp(T.recomputed_on_apply))
    fireEvent.change(screen.getByLabelText(T.approver_identity), { target: { value: 'x' } })
    fireEvent.click(screen.getByRole('button', { name: T.apply_the_plan }))
    const alert = await screen.findByRole('alert')
    // The branch is on the engine's own refusal code, which is the actionable part:
    // every setup refusal shares one status and one `code`.
    expect(alert).toHaveTextContent(T.refused_approver_required)
    expect(alert).toHaveTextContent(T.nothing_was_written)
    expect(alert).toHaveTextContent('approver-required')
  })

  it('tells an operator to recompute a stale plan rather than to retry it', async () => {
    stub({
      apply: {
        status: 409,
        body: {
          code: 'setup_refused',
          refused: 'plan-stale',
          reason: 'StalePlan',
          error: 'the plan_id does not identify the plan these inputs produce now',
        },
      },
    })
    const plan = await inspectProject()
    answerEverything()
    fireEvent.click(plan)
    await screen.findByText(new RegExp(T.recomputed_on_apply))
    fireEvent.change(screen.getByLabelText(T.approver_identity), { target: { value: 'x' } })
    fireEvent.click(screen.getByRole('button', { name: T.apply_the_plan }))
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent(T.refused_plan_stale)
  })

  it('renders an unanswered-gate refusal from the plan step', async () => {
    stub({
      plan: {
        status: 409,
        body: {
          code: 'setup_refused',
          refused: 'setup-approval-required',
          reason: 'SetupApprovalRequired',
          error: 'the delivery rung is unanswered',
        },
      },
    })
    const plan = await inspectProject()
    fireEvent.click(plan)
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent(T.refused_approval_required)
    expect(alert).toHaveTextContent(T.nothing_was_written)
  })

  it('offers only the presets the inspection offered, with the programs each runs', async () => {
    stub({})
    await inspectProject()
    // The engine refuses a preset that was never offered, so a free-text field here
    // would build a request that cannot succeed.
    expect(screen.getByRole('button', { name: 'git-pull-request' })).toBeInTheDocument()
    expect(screen.getByText(/git . gh/)).toBeInTheDocument()
    expect(screen.getByText(T.only_offered_presets_may_be_written)).toBeInTheDocument()
  })

  it('states that the tooling question takes no answer here', async () => {
    stub({})
    await inspectProject()
    // `SetupAnswers` has no tooling member: the engine asks so a human knows nothing
    // was inferred, and the commands are configured in the document.
    expect(screen.getByText(T.the_tooling_question_takes_no_answer)).toBeInTheDocument()
  })

  it('says memory was unavailable rather than reporting a smaller plan silently', async () => {
    stub({})
    await inspectProject()
    expect(screen.getByText(T.memory_was_not_consulted)).toBeInTheDocument()
  })

  it('states that nothing is written until the fourth step', async () => {
    stub({})
    await inspectProject()
    expect(screen.getByText(T.nothing_is_written_until_step_four)).toBeInTheDocument()
  })
})
