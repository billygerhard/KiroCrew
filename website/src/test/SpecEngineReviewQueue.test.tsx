/**
 * The review queue panel: per-row binding, the honest action set, the four wired
 * queue actions, and the layout rules that keep untrusted text from moving a
 * control.
 *
 * What is asserted here, and why each one is a property rather than a rendering
 * detail:
 *
 *   - **The inspector is bound to the SELECTED row.** The selected mockup's
 *     inspector was static below its header, so choosing the budget-parked run
 *     still showed the first run's detail. That is the single defect this panel
 *     exists to fix, so a test moves the selection and reads the pane.
 *   - **No control is offered that has no route.** Five of the mockup's six moves
 *     have no HTTP handler. A button for one of them would be inert, and inert
 *     controls on an operator surface teach an operator to keep clicking.
 *   - **A teardown that kept anything is not complete.** `ok: true` is not
 *     completion, the kept ids are the payload that matters, and a surface reading
 *     only `ok` would report a standing workspace as torn down.
 *   - **`release_refused` is not `release_failed`.** A 409 is the engine's rule
 *     and a 503 is a store failure a retry may clear; telling an operator to
 *     retry the first sends them back forever.
 *   - **Untrusted text is bounded and last.** Expanding it must not be able to
 *     displace anything above it, which is a fact about block ORDER as much as
 *     about height.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import SpecEnginePage from '../apps/spec-engine/SpecEnginePage'
import { __panelTesting } from '../apps/spec-engine/ReviewQueuePanel'
import { SE_CSS } from '../apps/spec-engine/styles'
import en from '../i18n/locales/en.json'

const T = en.apps.specEngine.reviewQueuePanel

/** One queue row, with only the fields a caller overrides spelled out. */
function entry(over: Record<string, unknown> = {}) {
  return {
    run_id: 'run_8f2a41',
    project: '/home/me/src/checkout-svc',
    spec: 'idempotent-refunds',
    spec_type: 'feature',
    state: 'awaiting_review',
    waiting_on: 'review',
    entered_ts: '2026-08-17T10:00:00Z',
    waiting_s: 8040,
    source: null,
    item_id: null,
    cost_credits: 163.2,
    gate: 'design',
    revision_exhausted: false,
    feedback_quarantined: 0,
    feedback_needs_human: false,
    analysis: [],
    ...over,
  }
}

/** One kept row, in `WorkspaceCleanup.to_json_object`'s shape. */
function keptRow(workspaceId: number, reason = '') {
  return cleanupRow(workspaceId, false, reason)
}

/** One cleanup verdict, in `WorkspaceCleanup.to_json_object`'s shape. */
function cleanupRow(workspaceId: number, removed: boolean, reason = '') {
  return {
    workspace_id: workspaceId,
    run_id: 'run_8f2a41',
    kind: 'worktree',
    location: `/tmp/ws/${workspaceId}`,
    address: null,
    removed,
    reason,
  }
}

/** A teardown report with nothing in it, in `TeardownReport.to_json_object`'s shape. */
function emptyReport() {
  return {
    run_id: 'run_8f2a41',
    forced: false,
    removed: [],
    kept: [],
    stage: null,
    stage_reason: '',
  }
}

type Answer = { status?: number; body: unknown }

/** Every request the page made, so an assertion can read the body that was sent. */
const calls: Array<{ url: string; method: string; body: unknown }> = []

/**
 * Answer each route independently, and answer a POST from a queue of scripted
 * replies so a wired action can be observed refusing and then succeeding.
 */
function stub(answers: { queue?: Answer; post?: Record<string, Answer> }) {
  vi.stubGlobal(
    'fetch',
    vi.fn((url: string, init?: RequestInit) => {
      const method = init?.method ?? 'GET'
      calls.push({
        url,
        method,
        body: init?.body ? JSON.parse(String(init.body)) : undefined,
      })
      let answer: Answer
      if (method === 'POST') {
        const path = url.replace('/api/apps/spec-engine/queue/', '')
        answer = answers.post?.[path] ?? { body: { ok: true } }
      } else if (url.startsWith('/api/apps/spec-engine/config')) {
        answer = { body: { configured: true, document: {}, elided: [] } }
      } else if (url.startsWith('/api/apps/spec-engine/kill-switch')) {
        answer = {
          body: {
            switch: { engaged: false, unreadable: false },
            stoppable: [],
            stoppable_credits: 0,
          },
        }
      } else if (url.startsWith('/api/apps/spec-engine/run-spend')) {
        // The inspector's spend block reads this per selected run. Answered with a
        // real shape rather than left to fall through to the queue reply, so a
        // panel assertion here is never reading a spend rendered from the wrong
        // payload.
        answer = {
          body: {
            run_id: 'run_8f2a41',
            project: '/home/me/src/checkout-svc',
            spec: 'idempotent-refunds',
            state: 'awaiting_review',
            source: '',
            credits: 163.2,
            metered_credits: 163.2,
            declared_credits: 0,
            turns: 41,
            sessions: 3,
            recorded_credits: 163.2,
            ceiling: { value: 600, origin: 'app_config', declared_at: 'budget.run_ceiling_credits' },
          },
        }
      } else {
        answer = answers.queue ?? { body: { entries: [], grouped: {}, total: 0, total_credits: 0 } }
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

function renderWith(entries: unknown[], post?: Record<string, Answer>) {
  stub({
    queue: { body: { entries, grouped: {}, total: entries.length, total_credits: 0 } },
    post,
  })
  const client = new QueryClient({
    defaultOptions: {
      queries: { retry: false, refetchInterval: false },
      mutations: { retry: false },
    },
  })
  return render(
    <QueryClientProvider client={client}>
      <SpecEnginePage />
    </QueryClientProvider>,
  )
}

/** The docked inspector, scoped so a query cannot match the row instead. */
const inspector = () => within(screen.getByLabelText(en.apps.specEngine.specEnginePage.selected_run))

afterEach(() => {
  vi.unstubAllGlobals()
  calls.length = 0
})

describe('row-level state words', () => {
  /** The flag element for one meaning, read by its own data attribute.
   *
   * By attribute rather than by text: each flag interpolates a count beside its
   * label, so the label alone is a partial match on a split text node — and the
   * attribute is what the stylesheet keys the per-meaning colour off anyway, so
   * asserting it also pins that one class never covers two meanings. */
  const flag = (container: HTMLElement, meaning: string) =>
    container.querySelector(`.se-q [data-flag="${meaning}"]`)

  it('renders spent revisions, held comments and a needs-a-person mark on the row', async () => {
    const { container } = renderWith([
      entry({ revision_exhausted: true, feedback_quarantined: 2, feedback_needs_human: true }),
    ])
    await screen.findByRole('grid')
    // On the ROW, not only in the inspector: a reader scanning the list must not
    // have to select a run to learn its revision cycles are spent.
    expect(flag(container, 'exhausted')).toHaveTextContent(T.flag_revisions_spent)
    expect(flag(container, 'held')).toHaveTextContent(`2 ${T.flag_held}`)
    expect(flag(container, 'human')).toHaveTextContent(T.flag_needs_a_person)
  })

  it('renders no flags for a run in none of those states', async () => {
    const { container } = renderWith([entry()])
    await screen.findByRole('grid')
    expect(flag(container, 'exhausted')).toBeNull()
    expect(flag(container, 'held')).toBeNull()
    expect(flag(container, 'human')).toBeNull()
  })

  it('does not claim kept workspaces before a teardown has reported any', async () => {
    // The count is not a queue field and cannot be: it exists only once a teardown
    // reports it. A flag rendered from a default would assert a standing workspace
    // that nobody observed.
    const { container } = renderWith([entry()])
    await screen.findByRole('grid')
    expect(flag(container, 'kept')).toBeNull()
  })
})

describe('the inspector follows the selected row', () => {
  const two = [
    entry({ run_id: 'run_a', spec: 'alpha' }),
    entry({
      run_id: 'run_b',
      spec: 'beta',
      waiting_on: 'budget',
      state: 'halted_budget',
      gate: null,
    }),
  ]

  it('shows the first row\u2019s action set, then the second row\u2019s when it is selected', async () => {
    const { container } = renderWith(two)
    await screen.findByRole('grid')
    expect(inspector().getByText(T.act_review)).toBeInTheDocument()

    // Selection follows focus in the grid, so moving down is what re-binds the pane.
    const selected = container.querySelector('.se-row[aria-selected="true"]') as HTMLElement
    selected.focus()
    selected.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true }))

    // The failure this asserts against is a pane that keeps describing run_a: it
    // would still read "waiting on a verdict" for a run parked at a ceiling.
    await waitFor(() => expect(inspector().getByText(T.act_budget)).toBeInTheDocument())
    expect(inspector().queryByText(T.act_review)).toBeNull()
  })

  it('discards the previous run\u2019s typed identifier when the selection moves', async () => {
    const held = [
      entry({ run_id: 'run_a', spec: 'alpha', feedback_quarantined: 1 }),
      entry({ run_id: 'run_b', spec: 'beta', feedback_quarantined: 1 }),
    ]
    const { container } = renderWith(held)
    await screen.findByRole('grid')
    const field = () => inspector().getByLabelText(T.comment_identifier) as HTMLInputElement
    fireEvent.change(field(), { target: { value: 'cmt_for_run_a' } })
    expect(field().value).toBe('cmt_for_run_a')

    const selected = container.querySelector('.se-row[aria-selected="true"]') as HTMLElement
    selected.focus()
    selected.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true }))

    // A comment id typed against one run must not be submitted against another.
    await waitFor(() => expect(field().value).toBe(''))
  })
})

describe('the action set', () => {
  it('gives spent revision cycles their own heading', async () => {
    renderWith([entry({ revision_exhausted: true })])
    await screen.findByRole('grid')
    expect(inspector().getByText(T.act_exhausted)).toBeInTheDocument()
    expect(inspector().queryByText(T.act_review)).toBeNull()
  })

  it('states where a verdict is recorded instead of offering a control with no route', async () => {
    renderWith([entry()])
    await screen.findByRole('grid')
    // The five moves the mockup drew — approve, request changes, cancel, raise
    // ceiling, resume — have no handler in backend/routes.py. The note names the
    // transport that does have the capability.
    expect(inspector().getByText(T.act_note_review)).toBeInTheDocument()
    expect(inspector().getByText(T.act_note_review).textContent).toContain('record_approval')
  })

  it('does not offer a revision-limit control for one gate, because the engine has none', async () => {
    // `limits.revision_cycle_limit` is declared with app and project scopes only,
    // so there is no per-gate limit to raise. The mockup's "raise the revision
    // limit and retry" would misdescribe what it did.
    renderWith([entry({ revision_exhausted: true })])
    await screen.findByRole('grid')
    const note = inspector().getByText(T.act_note_exhausted)
    expect(note).toBeInTheDocument()
    expect(__panelTesting.actReason(entry({ revision_exhausted: true }) as never)).toBe('exhausted')
  })

  it('keys every reason the engine can report, plus the exhausted distinction', () => {
    // A missing member renders as its own dotted key rather than failing, so the
    // tables are checked against the reasons rather than against a fixture.
    const reasons = ['budget', 'exhausted', 'review', 'stall']
    expect(Object.keys(__panelTesting.ACT_HEAD_KEY).sort()).toEqual(reasons)
    expect(Object.keys(__panelTesting.ACT_NOTE_KEY).sort()).toEqual(reasons)
  })
})

describe('untrusted submitter text', () => {
  const withFinding = (message: string) =>
    entry({
      analysis: [
        {
          criterion: '3.2',
          keyed: true,
          findings: [{ kind: 'gap', severity: 'blocking', message, refs: ['AC 3.2'] }],
        },
      ],
    })

  it('renders a markup payload as text rather than parsing it', async () => {
    const payload = 'refund double-charges. <img src=x onerror="alert(1)">'
    const { container } = renderWith([withFinding(payload)])
    await screen.findByRole('grid')
    const body = container.querySelector('.se-untrusted-body')
    expect(body).not.toBeNull()
    // The whole string survives as text content, and no element was created from it.
    expect(body!.textContent).toBe(payload)
    expect(container.querySelector('.se-untrusted-body img')).toBeNull()
  })

  it('bounds the expanded form to a fixed height rather than capping it', async () => {
    // A `max-height` cap still grows with line count until it binds, so a
    // four-line comment and a forty-line one would lay out differently.
    const declarations = SE_CSS.replace(/\s+/g, '')
    expect(declarations).toContain('-webkit-line-clamp:2')
    expect(declarations).toContain('.se-untrusted[data-open="true"].se-untrusted-body{display:block;height:104px;overflow:auto}')
    expect(declarations).not.toContain('.se-untrusted[data-open="true"].se-untrusted-body{max-height')
  })

  it('expands and collapses in flow, with no overlay', async () => {
    const { container } = renderWith([withFinding('line one\nline two\nline three')])
    await screen.findByRole('grid')
    const box = () => container.querySelector('.se-untrusted') as HTMLElement
    expect(box().dataset.open).toBe('false')
    screen.getByRole('button', { name: T.show_the_whole_text }).click()
    await waitFor(() => expect(box().dataset.open).toBe('true'))
    screen.getByRole('button', { name: T.collapse }).click()
    await waitFor(() => expect(box().dataset.open).toBe('false'))
  })

  it('puts the untrusted block last in the pane, after every control', async () => {
    const { container } = renderWith([withFinding('anything')])
    await screen.findByRole('grid')
    const blocks = Array.from(container.querySelectorAll('.se-insp-body .se-blk'))
    const untrustedBlock = container.querySelector('.se-untrusted')!.closest('.se-blk')
    // Ordering, not styling: a bounded block that sits ABOVE the controls still
    // moves them every time its height changes.
    expect(blocks.indexOf(untrustedBlock as Element)).toBe(blocks.length - 1)
  })

  it('says findings are absent rather than rendering an empty list', async () => {
    renderWith([entry()])
    await screen.findByRole('grid')
    expect(inspector().getByText(T.no_findings_are_recorded_for_this_run)).toBeInTheDocument()
  })

  it('groups findings the engine could not key as unkeyed', async () => {
    renderWith([
      entry({
        analysis: [
          { criterion: null, keyed: false, findings: [{ severity: 'advisory', message: 'stray' }] },
        ],
      }),
    ])
    await screen.findByRole('grid')
    expect(inspector().getByText(T.unkeyed)).toBeInTheDocument()
  })
})

describe('teardown', () => {
  const arm = async () => {
    screen.getByRole('button', { name: T.tear_down_this_runs_workspaces }).click()
    return screen.findByRole('button', { name: T.confirm_the_teardown })
  }

  it('asks for confirmation before it sends anything', async () => {
    renderWith([entry()])
    await screen.findByRole('grid')
    screen.getByRole('button', { name: T.tear_down_this_runs_workspaces }).click()
    await screen.findByRole('button', { name: T.confirm_the_teardown })
    // Armed is not fired: every run in this queue is waiting on something, so a
    // single click on a control that destroys the checkout under review is the
    // wrong cost.
    expect(calls.filter((call) => call.url.endsWith('/teardown'))).toHaveLength(0)
  })

  it('surfaces the kept ids and does NOT report itself complete', async () => {
    renderWith([entry()], {
      teardown: {
        // The handler's own shape for a teardown that could not finish: ok is
        // true, complete is false, and the report's kept rows are what matters.
        body: {
          ok: true,
          complete: false,
          kept: [703, 707],
          report: {
            run_id: 'run_1',
            forced: false,
            removed: [],
            kept: [keptRow(703, 'a worktree whose removal was refused'), keptRow(707)],
            stage: null,
            stage_reason: '',
          },
        },
      },
    })
    await screen.findByRole('grid')
    ;(await arm()).click()

    await waitFor(() => expect(screen.getByText(T.teardown_incomplete)).toBeInTheDocument())
    // Every kept id is named, WITH the engine's reason: a bare id would not
    // tell an operator which retry can possibly succeed.
    expect(screen.getByText('703')).toBeInTheDocument()
    expect(screen.getByText('707')).toBeInTheDocument()
    expect(screen.getByText('a worktree whose removal was refused')).toBeInTheDocument()
    // The completion sentence must be absent, not merely de-emphasised.
    expect(screen.queryByText(T.teardown_complete)).toBeNull()
  })

  it('names the stage failure when a teardown is incomplete with nothing kept', async () => {
    // complete is `not kept and (stage is None or stage.ok)`, so a wired stage
    // that fails on a run with no kept rows yields complete:false, kept:[].
    // The emittable shape: stage carries the outcome (failed / timed_out /
    // refused) and stage_reason is EMPTY — the engine only populates the
    // reason when no stage ran at all, and that report is complete. The
    // outcome is appended only when it says more than the sentence (a bare
    // 'failed' would stutter), so the fixture uses timed_out.
    renderWith([entry()], {
      teardown: {
        body: {
          ok: true,
          complete: false,
          kept: [],
          report: {
            run_id: 'run_1',
            forced: false,
            removed: [],
            kept: [],
            stage: 'timed_out',
            stage_reason: '',
          },
        },
      },
    })
    await screen.findByRole('grid')
    ;(await arm()).click()
    await waitFor(() => expect(screen.getByText(T.teardown_incomplete)).toBeInTheDocument())
    expect(screen.getByText(new RegExp(T.the_teardown_stage_failed))).toBeInTheDocument()
    // The cause the payload actually carries: the stage outcome.
    expect(screen.getByText('timed_out')).toBeInTheDocument()
    // The kept sentence must be absent — nothing was kept, and the neutral
    // headline no longer asserts otherwise.
    expect(screen.queryByText(T.kept_workspaces_are_still_standing)).toBeNull()
  })

  it('reports completion only when nothing was kept', async () => {
    renderWith([entry()], {
      teardown: { body: { ok: true, complete: true, kept: [], report: emptyReport() } },
    })
    await screen.findByRole('grid')
    ;(await arm()).click()
    await waitFor(() => expect(screen.getByText(T.teardown_complete)).toBeInTheDocument())
    expect(screen.queryByText(T.teardown_incomplete)).toBeNull()
  })

  it('shows the kept count on the row once a teardown has reported one', async () => {
    const { container } = renderWith([entry()], {
      teardown: {
        body: {
          ok: true,
          complete: false,
          kept: [703, 707],
          report: { ...emptyReport(), kept: [keptRow(703), keptRow(707)] },
        },
      },
    })
    await screen.findByRole('grid')
    ;(await arm()).click()
    await waitFor(() =>
      expect(container.querySelector('.se-q [data-flag="kept"]')).toHaveTextContent(
        `2 ${T.flag_workspaces_kept}`,
      ),
    )
  })

  it('retries one kept workspace through the cleanup route, by id', async () => {
    renderWith([entry()], {
      teardown: {
        body: {
          ok: true,
          complete: false,
          kept: [703],
          report: { ...emptyReport(), kept: [keptRow(703)] },
        },
      },
      'clean-workspace': {
        body: { ok: true, removed: true, cleanup: cleanupRow(703, true, 'removed the worktree') },
      },
    })
    await screen.findByRole('grid')
    ;(await arm()).click()
    await waitFor(() => expect(screen.getByText(T.teardown_incomplete)).toBeInTheDocument())
    screen.getByRole('button', { name: T.remove }).click()

    await waitFor(() => expect(screen.getByText(T.the_workspace_was_removed)).toBeInTheDocument())
    const cleanup = calls.find((call) => call.url.endsWith('/clean-workspace'))
    expect(cleanup?.body).toEqual({ workspace_id: 703, force: false })
  })

  it('does not read a declined removal as a removal', async () => {
    // The handler's top-level `removed` means "an active row with that id
    // existed", NOT "it came down": the engine declines a deployment row, a
    // failed `git worktree remove`, or a tree outside the disposable root with
    // a populated cleanup whose own `removed` is false. The canonical kept row
    // answers exactly this shape, so reading the top-level field would report
    // a standing workspace as removed.
    renderWith([entry()], {
      teardown: {
        body: {
          ok: true,
          complete: false,
          kept: [703],
          report: { ...emptyReport(), kept: [keptRow(703)] },
        },
      },
      'clean-workspace': {
        body: {
          ok: true,
          removed: true,
          cleanup: cleanupRow(703, false, 'the ledger records this as a deployment'),
        },
      },
    })
    await screen.findByRole('grid')
    ;(await arm()).click()
    await waitFor(() => expect(screen.getByText(T.teardown_incomplete)).toBeInTheDocument())
    screen.getByRole('button', { name: T.remove }).click()

    await waitFor(() =>
      expect(
        screen.getByText(
          T.the_workspace_was_kept_because.replace(
            '{{reason}}',
            'the ledger records this as a deployment',
          ),
        ),
      ).toBeInTheDocument(),
    )
    expect(screen.queryByText(T.the_workspace_was_removed)).toBeNull()
  })

  it('reads a cleanup that removed nothing as nothing to do, not as a failure', async () => {
    renderWith([entry()], {
      teardown: {
        body: {
          ok: true,
          complete: false,
          kept: [703],
          report: { ...emptyReport(), kept: [keptRow(703)] },
        },
      },
      'clean-workspace': { body: { ok: true, removed: false, cleanup: null } },
    })
    await screen.findByRole('grid')
    ;(await arm()).click()
    await waitFor(() => expect(screen.getByText(T.teardown_incomplete)).toBeInTheDocument())
    screen.getByRole('button', { name: T.remove }).click()
    await waitFor(() =>
      expect(screen.getByText(T.no_active_workspace_has_that_id)).toBeInTheDocument(),
    )
  })

  it('retires a row from the kept list only when its removal was confirmed', async () => {
    // A confirmed removal (cleanup.removed true) must stop the row offering a
    // live Remove button. The diagnosis must NOT change: the teardown still
    // kept workspaces, so the stage-failure sentence must not appear after the
    // last row is retired — deriving the diagnosis from the filtered list was
    // a real regression a review caught.
    renderWith([entry()], {
      teardown: {
        body: {
          ok: true,
          complete: false,
          kept: [703],
          report: { ...emptyReport(), kept: [keptRow(703)] },
        },
      },
      'clean-workspace': {
        body: { ok: true, removed: true, cleanup: cleanupRow(703, true, 'removed the worktree') },
      },
    })
    await screen.findByRole('grid')
    ;(await arm()).click()
    await waitFor(() => expect(screen.getByText(T.teardown_incomplete)).toBeInTheDocument())
    screen.getByRole('button', { name: T.remove }).click()
    await waitFor(() => expect(screen.getByText(T.the_workspace_was_removed)).toBeInTheDocument())
    // The row is gone from the kept list…
    expect(screen.queryByRole('button', { name: T.remove })).toBeNull()
    // …and the empty rendered list is NOT misread as a stage failure.
    expect(screen.queryByText(new RegExp(T.the_teardown_stage_failed))).toBeNull()
  })

  it('keeps offering the retry when the removal was declined', async () => {
    renderWith([entry()], {
      teardown: {
        body: {
          ok: true,
          complete: false,
          kept: [703],
          report: { ...emptyReport(), kept: [keptRow(703)] },
        },
      },
      'clean-workspace': {
        body: {
          ok: true,
          removed: true,
          cleanup: cleanupRow(703, false, 'the ledger records this as a deployment'),
        },
      },
    })
    await screen.findByRole('grid')
    ;(await arm()).click()
    await waitFor(() => expect(screen.getByText(T.teardown_incomplete)).toBeInTheDocument())
    screen.getByRole('button', { name: T.remove }).click()
    await waitFor(() =>
      expect(
        screen.getByText(
          T.the_workspace_was_kept_because.replace(
            '{{reason}}',
            'the ledger records this as a deployment',
          ),
        ),
      ).toBeInTheDocument(),
    )
    // A declined removal leaves the row standing, so the retry stays live.
    expect(screen.getByRole('button', { name: T.remove })).toBeInTheDocument()
  })

  it('sends force through the force-remove button', async () => {
    renderWith([entry()], {
      teardown: {
        body: {
          ok: true,
          complete: false,
          kept: [703],
          report: { ...emptyReport(), kept: [keptRow(703)] },
        },
      },
      'clean-workspace': {
        body: { ok: true, removed: true, cleanup: cleanupRow(703, true, 'forced') },
      },
    })
    await screen.findByRole('grid')
    ;(await arm()).click()
    await waitFor(() => expect(screen.getByText(T.teardown_incomplete)).toBeInTheDocument())
    screen.getByRole('button', { name: T.force_remove }).click()
    await waitFor(() => expect(screen.getByText(T.the_workspace_was_removed)).toBeInTheDocument())
    const cleanup = calls.find((call) => call.url.endsWith('/clean-workspace'))
    expect(cleanup?.body).toEqual({ workspace_id: 703, force: true })
  })

  it('names the failed cleanup by its workspace id', async () => {
    renderWith([entry()], {
      teardown: {
        body: {
          ok: true,
          complete: false,
          kept: [703],
          report: { ...emptyReport(), kept: [keptRow(703)] },
        },
      },
      'clean-workspace': {
        status: 503,
        body: { code: 'cleanup_failed', error: 'the store is locked' },
      },
    })
    await screen.findByRole('grid')
    ;(await arm()).click()
    await waitFor(() => expect(screen.getByText(T.teardown_incomplete)).toBeInTheDocument())
    screen.getByRole('button', { name: T.remove }).click()
    // The failure is named by the raw id, like the success verdict: with
    // several kept rows an operator must know which retry failed.
    await waitFor(() =>
      expect(screen.getByText(`703 — ${T.the_cleanup_failed}`)).toBeInTheDocument(),
    )
  })

  it('states a teardown refusal with its code', async () => {
    renderWith([entry()], {
      teardown: { status: 503, body: { code: 'teardown_failed', error: 'janitor is absent' } },
    })
    await screen.findByRole('grid')
    ;(await arm()).click()
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent(T.the_teardown_failed)
    expect(alert).toHaveTextContent('teardown_failed')
  })
})

describe('releasing a held comment', () => {
  const withHeld = entry({ feedback_quarantined: 2 })

  const type = (value: string) => {
    fireEvent.change(screen.getByLabelText(T.comment_identifier), { target: { value } })
  }

  it('sends the identifier and the run\u2019s own project and spec, never the text', async () => {
    renderWith([withHeld], { 'release-feedback': { body: { ok: true, released: true } } })
    await screen.findByRole('grid')
    type('cmt_41')
    await waitFor(() =>
      expect(screen.getByRole('button', { name: T.release_the_comment })).toBeEnabled(),
    )
    screen.getByRole('button', { name: T.release_the_comment }).click()

    await waitFor(() => expect(screen.getByText(T.the_comment_was_released)).toBeInTheDocument())
    const release = calls.find((call) => call.url.endsWith('/release-feedback'))
    expect(release?.body).toEqual({
      project: '/home/me/src/checkout-svc',
      spec: 'idempotent-refunds',
      run_id: 'run_8f2a41',
      comment_id: 'cmt_41',
    })
  })

  it('distinguishes a 409 engine refusal from a 503 failure', async () => {
    renderWith([withHeld], {
      'release-feedback': {
        status: 409,
        body: { code: 'release_refused', error: 'this run records no release' },
      },
    })
    await screen.findByRole('grid')
    type('cmt_41')
    await waitFor(() =>
      expect(screen.getByRole('button', { name: T.release_the_comment })).toBeEnabled(),
    )
    screen.getByRole('button', { name: T.release_the_comment }).click()

    const alert = await screen.findByRole('alert')
    // A rule, not a failure: retrying it refuses again forever, so the two must
    // not read alike.
    expect(alert).toHaveTextContent(T.the_engine_refused_the_release)
    expect(alert).not.toHaveTextContent(T.the_release_failed)
  })

  it('calls a store failure a failure', async () => {
    renderWith([withHeld], {
      'release-feedback': {
        status: 503,
        body: { code: 'release_failed', error: 'database is locked' },
      },
    })
    await screen.findByRole('grid')
    type('cmt_41')
    await waitFor(() =>
      expect(screen.getByRole('button', { name: T.release_the_comment })).toBeEnabled(),
    )
    screen.getByRole('button', { name: T.release_the_comment }).click()
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent(T.the_release_failed)
  })

  it('answers a release nobody was holding rather than reporting a success', async () => {
    renderWith([withHeld], { 'release-feedback': { body: { ok: true, released: false } } })
    await screen.findByRole('grid')
    type('cmt_41')
    await waitFor(() =>
      expect(screen.getByRole('button', { name: T.release_the_comment })).toBeEnabled(),
    )
    screen.getByRole('button', { name: T.release_the_comment }).click()
    await waitFor(() => expect(screen.getByText(T.nobody_held_that_comment)).toBeInTheDocument())
  })

  it('will not send an empty identifier', async () => {
    renderWith([withHeld])
    await screen.findByRole('grid')
    expect(screen.getByRole('button', { name: T.release_the_comment })).toBeDisabled()
  })

  it('says nothing is held when nothing is', async () => {
    renderWith([entry()])
    await screen.findByRole('grid')
    expect(inspector().getByText(T.no_comments_are_held_for_this_run)).toBeInTheDocument()
  })
})

describe('redispatch', () => {
  const watched = entry({ source: 'github', item_id: 'issue-91', waiting_on: 'stall' })

  it('is absent for a run no watcher produced', async () => {
    renderWith([entry()])
    await screen.findByRole('grid')
    // A control that refuses on every click is worse than an absent one.
    expect(inspector().queryByText(T.redispatch)).toBeNull()
  })

  it('sends the row\u2019s source and item with the generation the operator named', async () => {
    renderWith([watched], { redispatch: { body: { ok: true, lifted: true } } })
    await screen.findByRole('grid')
    fireEvent.change(screen.getByLabelText(T.generation), { target: { value: '7' } })
    await waitFor(() =>
      expect(screen.getByRole('button', { name: T.lift_the_suppression })).toBeEnabled(),
    )
    screen.getByRole('button', { name: T.lift_the_suppression }).click()

    await waitFor(() => expect(screen.getByText(T.the_suppression_was_lifted)).toBeInTheDocument())
    const sent = calls.find((call) => call.url.endsWith('/redispatch'))
    // A NUMBER, not a numeric string: the handler's whole-number reader refuses
    // anything else and would answer `field_required` for a field that was filled.
    expect(sent?.body).toEqual({ source: 'github', item_id: 'issue-91', generation: 7 })
  })

  it('refuses to send without a generation, rather than defaulting one', async () => {
    renderWith([watched])
    await screen.findByRole('grid')
    // Lifting an unnamed generation would lift whichever one the poller was on.
    expect(screen.getByRole('button', { name: T.lift_the_suppression })).toBeDisabled()
  })
})
