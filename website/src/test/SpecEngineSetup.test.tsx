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
 *   - **The pane orients a first-time operator**, and stops doing it once a project
 *     exists: what the engine does, what finishing produces, which step is first,
 *     and what the operator does and gets at each step. A step the flow cannot
 *     reach names its blocker, interpolated rather than assembled from fragments.
 *   - **The project path is browsable.** The shared picker fills the field with an
 *     absolute path, typing stays live, and a directory read that FAILED says so
 *     instead of looking like a host with no directories on it.
 *   - **The flow repeats for the next project.** The pane is fully usable once
 *     something is configured, heads itself honestly there, and puts the additional
 *     project behind the same named-approver gate. An apply makes the new entry
 *     visible in the projects table without a reload, and an inspection that names
 *     an already-configured project says so and frames continuing as re-inspection
 *     of that entry — with the read in doubt, that question is UNKNOWN rather than
 *     answered "no".
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import SpecEnginePage from '../apps/spec-engine/SpecEnginePage'
import { SE_CSS } from '../apps/spec-engine/styles'
import { api } from '../api/client'
import en from '../i18n/locales/en.json'

const T = en.apps.specEngine.setupFlowPanel
const P = en.apps.specEngine.specEnginePage
const KS = en.apps.specEngine.safetyPanel
const PICKER = en.components.projectPicker
const C = en.apps.specEngine.configPanel

/** The document store as it answers before anything is configured. */
const UNCONFIGURED: Answer = {
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

/** A configuration read that carries one project entry — the not-first-run state. */
const CONFIGURED: Answer = {
  body: {
    ...(UNCONFIGURED.body as Record<string, unknown>),
    configured: true,
    document: { projects: { acme: { cost_profile: 'budget' } } },
  },
}

/**
 * A configuration read carrying a project the inspection will NOT name, so the
 * pane is in the repeat state with no duplicate: adding a second project.
 */
const OTHER_PROJECT: Answer = {
  body: {
    ...(CONFIGURED.body as Record<string, unknown>),
    document: { projects: { widgets: { cost_profile: 'budget' } } },
  },
}

/** A catalog sentence with its one variable filled in, as the panel renders it. */
function filled(sentence: string, step: string): string {
  return sentence.replace('{{step}}', step)
}

/** The same, for the sentences whose one variable is the project's name. */
function forProject(sentence: string, project: string): string {
  return sentence.split('{{project}}').join(project)
}

import { stubSpecEngineFetch, type Answer } from './specEngineFetchStub'

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

function stub(answers: {
  inspect?: Answer
  plan?: Answer
  apply?: Answer
  /** A function is answered per request, so a read can change after a write. */
  config?: Answer | (() => Answer)
}) {
  stubSpecEngineFetch(
    {
      setupInspect: answers.inspect ?? { body: inspection() },
      setupPlan: answers.plan ?? { body: envelope() },
      setupApply: answers.apply ?? { body: applied() },
      // Answered with an empty vocabulary — this suite is about the setup flow.
      registry: {
        body: { settings: [], source_presets: [], profile_presets: [], roles: [], levels: [] },
      },
      // Answered with no source at all — these tests are about the setup flow, and
      // the grid's own properties live in `SpecEngineSources.test.tsx`.
      sources: {
        body: {
          sources: [],
          submitter_classes: ['maintainer', 'member', 'contributor', 'external'],
          spec_types: ['feature', 'bugfix', 'quick'],
          levels: ['authoring', 'execution', 'delivery', 'integration'],
        },
      },
      // Unconfigured, which is what routes the page to the assistant. A caller
      // that cares about the CONFIGURED state passes its own answer: first run is
      // "no project entry", so a document carrying one is the other state.
      config: () => {
        const given = typeof answers.config === 'function' ? answers.config() : answers.config
        return given ?? UNCONFIGURED
      },
    },
    { record: calls },
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
  // The picker's reads are spied on the shared client rather than stubbed through
  // fetch, so they have to be put back or the next test inherits them.
  vi.restoreAllMocks()
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

describe('the orientation', () => {
  it('says what the engine does, what finishing produces, and which step is first', async () => {
    stub({})
    renderPage()
    expect(await screen.findByText(T.orientation_engine)).toBeInTheDocument()
    expect(screen.getByText(T.orientation_produces)).toBeInTheDocument()
    // The first action is NAMED, and named by interpolating the step's own label —
    // a sentence glued to a fragment cannot be translated into a language that
    // orders them differently.
    expect(
      screen.getByText(filled(T.orientation_first_action, P.step_inspect_the_project)),
    ).toBeInTheDocument()
    expect(document.body.textContent).not.toContain('{{step}}')
  })

  it('says what the operator does and gets at each of the four steps', async () => {
    stub({})
    renderPage()
    await screen.findByText(T.orientation_engine)
    for (const description of [
      T.step_desc_inspect,
      T.step_desc_answer,
      T.step_desc_review,
      T.step_desc_approve,
    ]) {
      expect(screen.getByText(description)).toBeInTheDocument()
    }
  })

  it('names the step that must complete before an unreachable one', async () => {
    stub({})
    renderPage()
    await screen.findByText(T.orientation_engine)
    // Nothing is inspected yet. Answering and reviewing BOTH wait on the
    // inspection (the plan can be computed with defaults left unanswered, so
    // naming the answer step would state a blocker the plan button contradicts),
    // and approve waits on a plan being on screen.
    expect(screen.getAllByText(filled(T.blocked_until, P.step_inspect_the_project))).toHaveLength(2)
    expect(
      screen.getByText(filled(T.blocked_until, P.step_review_the_plan)),
    ).toBeInTheDocument()
    expect(
      screen.queryByText(filled(T.blocked_until, P.step_answer_what_could_not_be_inferred)),
    ).not.toBeInTheDocument()
    // The step the flow IS on is not blocked by anything.
    expect(
      screen.queryByText(filled(T.blocked_until, P.step_approve_and_apply)),
    ).not.toBeInTheDocument()
  })

  it('does not claim a step is blocked while its own control is live', async () => {
    // The state the false claim lived in: after a successful inspect, the plan
    // button is enabled and requires no answers — so neither the answer step nor
    // the review step may render a blocker the button beside them contradicts.
    // Only approve stays blocked, on the plan it genuinely waits for.
    stub({})
    await inspectProject()
    expect(await screen.findByRole('button', { name: T.show_the_exact_patch })).toBeEnabled()
    expect(
      screen.queryByText(filled(T.blocked_until, P.step_inspect_the_project)),
    ).not.toBeInTheDocument()
    expect(
      screen.getByText(filled(T.blocked_until, P.step_review_the_plan)),
    ).toBeInTheDocument()
  })

  it('is gone once a project is configured, and the pane still takes a path', async () => {
    stub({ config: CONFIGURED })
    renderPage()
    // A configured engine lands on the queue, so the assistant is reached from the
    // rail — and it opens on the field rather than on the tutorial.
    fireEvent.click(await screen.findByRole('button', { name: P.setup_assistant }))
    expect(await screen.findByLabelText(T.project_path)).toBeInTheDocument()
    expect(screen.queryByText(T.orientation_engine)).not.toBeInTheDocument()
    expect(screen.queryByText(T.orientation_produces)).not.toBeInTheDocument()
    expect(screen.queryByText(T.step_desc_inspect)).not.toBeInTheDocument()
    // The blocker statement is NOT part of the orientation: an operator adding a
    // second project needs it as much as the first one did. Two steps wait on
    // the inspection (answering and reviewing), so the sentence appears twice.
    expect(
      screen.getAllByText(filled(T.blocked_until, P.step_inspect_the_project)),
    ).toHaveLength(2)
  })
})

describe('the project path', () => {
  /** The picker's two reads, answered on the shared client it calls them through. */
  function stubPicker(options: { recent?: string[]; browse?: 'fail' | Record<string, unknown> }) {
    vi.spyOn(api, 'recentProjects').mockResolvedValue({ dirs: options.recent ?? [] })
    const browse = vi.spyOn(api, 'browseDirs')
    if (options.browse === 'fail') browse.mockRejectedValue(new Error('EACCES'))
    else {
      browse.mockResolvedValue(
        (options.browse ?? { path: '/home/me/src', parent: '/home/me', dirs: [] }) as Awaited<
          ReturnType<typeof api.browseDirs>
        >,
      )
    }
    return browse
  }

  it('fills the field with the absolute path the shared picker returns', async () => {
    stub({})
    stubPicker({ recent: ['/home/me/src/acme'] })
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: T.browse }))
    // The dashboard's own picker, not a second directory browser: these are its
    // recents list and its own labels.
    await screen.findByRole('listbox', { name: PICKER.recent_projects })
    fireEvent.mouseDown(screen.getByText('/home/me/src/acme'))
    await waitFor(() =>
      expect(screen.getByLabelText(T.project_path)).toHaveValue('/home/me/src/acme'),
    )
    // And the field it filled is the same one an operator types into: inspecting is
    // now offered against the picked path.
    expect(screen.getByRole('button', { name: T.inspect_the_project })).toBeEnabled()
  })

  it('states a failed directory read and leaves typing available', async () => {
    stub({})
    stubPicker({ browse: 'fail' })
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: T.browse }))
    // The shared picker swallows this rejection and renders an empty list, so the
    // pane says it: a browse that could not be performed must not read as a host
    // with no directories on it.
    expect(await screen.findByText(T.browse_failed)).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText(T.project_path), { target: { value: '/src/acme' } })
    expect(screen.getByLabelText(T.project_path)).toHaveValue('/src/acme')
    fireEvent.click(screen.getByRole('button', { name: T.inspect_the_project }))
    await waitFor(() => expect(lastBody('/setup/inspect')).toEqual({ project: '/src/acme' }))
  })

  it('does not reset the open picker when the pane re-renders', async () => {
    // The pane re-renders constantly — every keystroke in the path field, every
    // queue poll of the page above it. The picker's open-effect resets its tab,
    // search, directory and input and re-fires both reads, so it must not re-run
    // on a parent render: an inline `onBrowseResult` arrow is a NEW function
    // each render, and only the picker's internal latching keeps the effect
    // stable. One read per open, whatever the parent does.
    stub({})
    const browse = stubPicker({ recent: ['/home/me/src/acme'] })
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: T.browse }))
    await screen.findByRole('listbox', { name: PICKER.recent_projects })
    expect(browse).toHaveBeenCalledTimes(1)
    expect(api.recentProjects).toHaveBeenCalledTimes(1)
    fireEvent.change(screen.getByLabelText(T.project_path), { target: { value: '/re/render' } })
    fireEvent.change(screen.getByLabelText(T.project_path), { target: { value: '/re/render/x' } })
    await waitFor(() => expect(screen.getByLabelText(T.project_path)).toHaveValue('/re/render/x'))
    expect(browse).toHaveBeenCalledTimes(1)
    expect(api.recentProjects).toHaveBeenCalledTimes(1)
  })

  it('reserves the strip\u2019s measured height, not just the collapsed row', async () => {
    // An armed switch or a standing verdict grows the strip to ~100-250px (its
    // panel renders as in-flow lines of the row), and the confirm control lives
    // in that growth. The reservation handed to the picker is the strip's
    // measured height at open plus margin — a constant sized for the collapsed
    // row would let the popover cover the confirm control.
    stub({})
    stubPicker({ recent: ['/home/me/src/acme'] })
    renderPage()
    await screen.findByRole('button', { name: T.browse })
    const strip = document.querySelector('.se-status') as HTMLElement
    vi.spyOn(strip, 'getBoundingClientRect').mockReturnValue({
      height: 250, width: 900, top: 250, bottom: 500, left: 0, right: 900, x: 0, y: 250,
      toJSON: () => ({}),
    } as DOMRect)
    Object.defineProperty(window, 'innerHeight', { value: 500, configurable: true })
    fireEvent.click(screen.getByRole('button', { name: T.browse }))
    const drop = (
      await screen.findByRole('listbox', { name: PICKER.recent_projects })
    ).closest('div.fixed') as HTMLElement
    // Downward layout: bottom edge = top + height must clear the 250px strip
    // plus margin (reserve 264), i.e. stay at or above 500 - 264 = 236. The
    // collapsed-row constant (48) would put it at 448, inside the strip.
    expect(parseInt(drop.style.top) + parseInt(drop.style.height)).toBeLessThanOrEqual(236)
  })

  it('leaves the kill switch on screen and operable while the picker is open', async () => {
    stub({})
    stubPicker({ recent: ['/home/me/src/acme'] })
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: T.browse }))
    await screen.findByRole('listbox', { name: PICKER.recent_projects })
    // Operated, not just present: arming the stop while the picker is open is
    // the interaction the requirement protects. jsdom computes no layout, so
    // OCCLUSION is out of this test's reach — the geometric bound lives in the
    // picker itself (`reservedBottom`, asserted in ProjectPicker.test.tsx), and
    // this test covers the DOM half: the strip is not hidden, trapped, or
    // disabled by the open popover.
    const stop = screen.getByRole('button', { name: KS.engage_the_kill_switch })
    expect(stop).toBeEnabled()
    expect(stop.closest('.se-status')).not.toBeNull()
    expect(document.querySelector('.se-status')?.getAttribute('aria-hidden')).toBeNull()
    fireEvent.click(stop)
    expect(
      await screen.findByRole('button', { name: KS.confirm_the_stop }),
    ).toBeInTheDocument()
  })
})

describe('running it again for another project', () => {
  /**
   * Reach the assistant the way a returning operator does — from the rail, since a
   * configured engine lands on the queue — and inspect a project.
   */
  async function inspectFromRail(path = '/src/acme') {
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: P.setup_assistant }))
    const field = await screen.findByLabelText(T.project_path)
    fireEvent.change(field, { target: { value: path } })
    fireEvent.click(screen.getByRole('button', { name: T.inspect_the_project }))
    return screen.findByRole('button', { name: T.show_the_exact_patch })
  }

  it('heads the pane honestly once something is configured', async () => {
    stub({ config: OTHER_PROJECT })
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: P.setup_assistant }))
    // "Nothing is configured yet" is a claim about the engine, and it is false the
    // moment one project exists — an operator adding their second project must not
    // be told the first one is absent.
    expect(await screen.findByText(T.configure_another_project)).toBeInTheDocument()
    expect(screen.queryByText(P.nothing_is_configured_yet)).not.toBeInTheDocument()
  })

  it('runs the whole flow for an additional project behind the same approver gate', async () => {
    stub({ config: OTHER_PROJECT })
    const plan = await inspectFromRail()
    answerEverything()
    fireEvent.click(plan)
    await screen.findByText(new RegExp(T.recomputed_on_apply))
    // Identical gate, not a relaxed one: no "already trusted" path exists for the
    // second project, so the apply is refused on screen until a name is typed.
    expect(screen.getByRole('button', { name: T.apply_the_plan })).toBeDisabled()
    expect(screen.getByText(T.an_approver_is_required)).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText(T.approver_identity), {
      target: { value: 'colleague@example' },
    })
    fireEvent.click(screen.getByRole('button', { name: T.apply_the_plan }))
    await waitFor(() => expect(calls.some((call) => call.url.endsWith('/setup/apply'))).toBe(true))
    expect(lastBody('/setup/apply').approver).toBe('colleague@example')
  })

  it('reviews a patch scoped to the new project and sends no document of its own', async () => {
    stub({
      config: OTHER_PROJECT,
      plan: {
        body: envelope({
          config_patch: { projects: { acme: { cost_profile: 'budget' } } },
          written_paths: ['projects.acme.cost_profile'],
        }),
      },
    })
    const plan = await inspectFromRail()
    answerEverything()
    fireEvent.click(plan)
    // The patch on screen is the engine's, rendered verbatim: it touches the new
    // project's entry and names no other. Whether `widgets` truly survives the
    // merge is the ENGINE's guarantee, pinned by name in
    // test_config_write_path.py::test_writes_merge_rather_than_replace — an
    // assertion here would only re-read this test's own fixture.
    const patch = await screen.findByText(/"projects"/)
    expect(patch).toHaveTextContent('acme')
    expect(screen.getByText(/projects\.acme\.cost_profile/)).toBeInTheDocument()
    // And the panel cannot widen that write: what it sends is the project subject
    // and the answers, never a document or a patch of its own construction.
    expect(Object.keys(lastBody('/setup/plan')).sort()).toEqual(['answers', 'project'])
    fireEvent.change(screen.getByLabelText(T.approver_identity), { target: { value: 'x' } })
    fireEvent.click(screen.getByRole('button', { name: T.apply_the_plan }))
    await waitFor(() => expect(calls.some((call) => call.url.endsWith('/setup/apply'))).toBe(true))
    expect(Object.keys(lastBody('/setup/apply')).sort()).toEqual([
      'answers',
      'approver',
      'plan_id',
      'project',
    ])
  })

  it('shows the applied project in the projects list without a reload', async () => {
    // The store answers the new entry once the apply has gone through, which is
    // what the invalidation has to pick up: no reload happens in this test.
    stub({
      config: () =>
        calls.some((call) => call.url.endsWith('/setup/apply')) ? CONFIGURED : UNCONFIGURED,
    })
    const plan = await inspectProject()
    answerEverything()
    fireEvent.click(plan)
    await screen.findByText(new RegExp(T.recomputed_on_apply))
    fireEvent.change(screen.getByLabelText(T.approver_identity), { target: { value: 'x' } })
    const readsBefore = calls.filter((call) => call.url.startsWith('/api/apps/spec-engine/config'))
    fireEvent.click(screen.getByRole('button', { name: T.apply_the_plan }))
    await screen.findByText(new RegExp(T.wrote))
    // The apply invalidates the configuration read, so a fresh one goes out.
    await waitFor(() =>
      expect(
        calls.filter((call) => call.url.startsWith('/api/apps/spec-engine/config')).length,
      ).toBeGreaterThan(readsBefore.length),
    )
    // And the projects table one pane over lists the entry that write created.
    fireEvent.click(screen.getByRole('button', { name: P.configuration }))
    const grid = await screen.findByRole('grid', { name: C.configured_projects })
    await waitFor(() => expect(grid).toHaveTextContent('acme'))
  })

  it('states that an inspected project is already configured, and frames continuing as re-inspection', async () => {
    // `acme` is the name the ENGINE derived in the inspect reply and the key its
    // entry is stored under. The pane compares those two and says so; it derives no
    // name from the path itself.
    stub({ config: CONFIGURED })
    await inspectFromRail()
    expect(await screen.findByText(forProject(T.already_configured, 'acme'))).toBeInTheDocument()
    expect(
      screen.getByText(forProject(T.already_configured_reinspection, 'acme')),
    ).toBeInTheDocument()
    expect(document.body.textContent).not.toContain('{{project}}')
    // Not an error and not a block: the flow continues onto the existing entry.
    expect(screen.getByRole('button', { name: T.show_the_exact_patch })).toBeEnabled()
  })

  it('withdraws the already-configured note once the path is edited past it', async () => {
    // The note is a positive claim about the INSPECTED path. Editing the field
    // clears the plan but keeps the inspection's evidence on screen; the note
    // must not ride that evidence and keep naming the old project while the
    // next plan targets the new path.
    stub({ config: CONFIGURED })
    await inspectFromRail()
    expect(await screen.findByText(forProject(T.already_configured, 'acme'))).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText(T.project_path), {
      target: { value: '/src/somewhere-else' },
    })
    expect(screen.queryByText(forProject(T.already_configured, 'acme'))).not.toBeInTheDocument()
    expect(
      screen.queryByText(forProject(T.already_configured_reinspection, 'acme')),
    ).not.toBeInTheDocument()
  })

  it('says nothing about duplication for a project that has no entry', async () => {
    stub({ config: OTHER_PROJECT })
    await inspectFromRail()
    expect(screen.queryByText(forProject(T.already_configured, 'acme'))).not.toBeInTheDocument()
    expect(
      screen.queryByText(forProject(T.already_configured_unknown, 'acme')),
    ).not.toBeInTheDocument()
  })

  it('calls the duplicate question unknown when the configuration could not be read', async () => {
    // The failure mode this closes: an unreadable configuration read as "no entry",
    // which would present an overwrite as a new project. Doubt is not absence.
    stub({ config: { status: 500, body: { code: 'config_unreadable', error: 'broken' } } })
    await inspectFromRail()
    expect(
      await screen.findByText(forProject(T.already_configured_unknown, 'acme')),
    ).toBeInTheDocument()
    expect(screen.queryByText(forProject(T.already_configured, 'acme'))).not.toBeInTheDocument()
  })
})

describe('answer feedback', () => {
  /* Every answer control in the setup flow (cost-profile chips, the per-rung
   * yes/no pair, preset offers, subject approval) is a `se-btn` carrying
   * `aria-pressed`. jsdom applies no stylesheet, so the attribute alone proved
   * nothing about pixels: the sheet styled `.se-filter[aria-pressed="true"]`
   * and `.se-rolebtn[aria-pressed="true"]` but never `.se-btn`, leaving a
   * chosen answer visually identical to its unchosen siblings — the only
   * on-screen feedback was the UNANSWERED flag disappearing. This pins the
   * pixel channel the same way the kill-switch dot test does: against the
   * stylesheet text itself. */
  it('styles a pressed answer button apart from its siblings', () => {
    const css = SE_CSS.replace(/\s+/g, '')
    expect(css).toContain(
      '.se-btn[aria-pressed="true"]{background:var(--accent-subtle);border-color:var(--accent);color:var(--text-strong)}',
    )
  })
})

describe('the autonomy ladder', () => {
  /* The rungs are a ladder: an enabled level authorizes every level below it,
   * and the engine refuses to write a Yes above a No. The panel makes that
   * contradiction unconstructable rather than waiting for the plan's refusal,
   * and names the blocking rung beside the disabled button. */
  const rung = (level: string) =>
    screen.getByText(level).closest('.se-rung') as HTMLElement

  it('blocks granting a rung above a declined one, naming the blocker', async () => {
    stub({})
    await inspectProject()
    fireEvent.click(within(rung('execution')).getByRole('button', { name: T.no }))
    const yes = within(rung('delivery')).getByRole('button', { name: T.yes })
    expect(yes).toBeDisabled()
    expect(
      screen.getByText(T.yes_blocked_by_declined.replace('{{level}}', 'execution')),
    ).toBeInTheDocument()
  })

  it('blocks declining a rung below a granted one, naming the blocker', async () => {
    stub({})
    await inspectProject()
    fireEvent.click(within(rung('delivery')).getByRole('button', { name: T.yes }))
    const no = within(rung('execution')).getByRole('button', { name: T.no })
    expect(no).toBeDisabled()
    expect(
      screen.getByText(T.no_blocked_by_granted.replace('{{level}}', 'delivery')),
    ).toBeInTheDocument()
  })

  it('locks only the rungs a choice actually constrains, and unwinding releases them', async () => {
    stub({})
    await inspectProject()
    fireEvent.click(within(rung('execution')).getByRole('button', { name: T.yes }))
    fireEvent.click(within(rung('delivery')).getByRole('button', { name: T.yes }))
    // The top rung has nothing above it: both of its buttons stay live.
    expect(within(rung('delivery')).getByRole('button', { name: T.yes })).toBeEnabled()
    expect(within(rung('delivery')).getByRole('button', { name: T.no })).toBeEnabled()
    // The lower rung's No is the click that would put a grant above a decline,
    // so it is locked — and the lock names the granted rung above.
    expect(within(rung('execution')).getByRole('button', { name: T.no })).toBeDisabled()
    expect(
      screen.getByText(T.no_blocked_by_granted.replace('{{level}}', 'delivery')),
    ).toBeInTheDocument()
    // Unwinding from the top releases the lock: the guard never wedges a set.
    fireEvent.click(within(rung('delivery')).getByRole('button', { name: T.no }))
    expect(within(rung('execution')).getByRole('button', { name: T.no })).toBeEnabled()
    expect(document.querySelector('[data-rung-blocked]')).toBeNull()
  })
})

describe('the approver field', () => {
  it('says what belongs in it before anything is typed', async () => {
    stub({})
    await inspectProject()
    const field = screen.getByLabelText(T.approver_identity)
    expect(field).toHaveAttribute('placeholder', T.approver_placeholder)
  })
})
