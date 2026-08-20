/**
 * The configuration pane: the document as the write path, and the resolved read.
 *
 * Five properties are asserted here, and each one is a correctness claim rather
 * than a rendering detail:
 *
 *   - **A save sends a PATCH.** The write path merges, so a key the operator
 *     deleted must be sent as an explicit `null` or the merge keeps the old value
 *     and the editor shows a change that did not happen.
 *   - **A withheld value is never written back.** Saving with the elision marker
 *     still in place would replace a live credential with the literal string
 *     `<elided>`, and nothing downstream would report it: the document stays
 *     valid and the write is recorded as ordinary.
 *   - **A per-role reset names the node it clears**, and offers nothing when there
 *     is no node — the mockup's disabled `Nothing to reset` with the missing path
 *     in its tooltip.
 *   - **Paths are matched SEGMENT-wise.** A profile may be named `thrifty.roles`,
 *     whose review-role node renders as `cost_profiles.thrifty.roles.roles.review`.
 *     A string-prefix match would read that as a path inside a profile named
 *     `thrifty` and offer a reset that clears a different project's profile.
 *   - **The resolved view is a read.** It writes nothing on its own, and it fails
 *     as a refusal rather than as an empty table.
 *
 * The projects surface adds four more:
 *
 *   - **One query per project.** A shared key would leave one project's answer in
 *     the cache and render it under a heading naming another, and a failed read
 *     must state itself rather than presenting the retained answer — or the app
 *     defaults — as what is in force.
 *   - **A removal is a null at exactly one entry.** The store's merge keeps every
 *     key a patch omits, so dropping the null sends a no-op the table would
 *     render as a removal that happened; widening the patch deletes the section.
 *   - **A refused removal keeps the entry.** A refused write returns no document,
 *     so the row stays and nothing reports a removal that was only asked for.
 *   - **An override count says what it counts.** A number beside a project is
 *     worth nothing if a reader cannot tell whether the column next to it is one
 *     of the things counted.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import SpecEnginePage from '../apps/spec-engine/SpecEnginePage'
import {
  dotted,
  isDescendant,
  mergePatch,
  nodeAt,
  parseDocument,
  patchAt,
  roleSegments,
} from '../apps/spec-engine/configDocument'
import en from '../i18n/locales/en.json'

const T = en.apps.specEngine.configPanel
const P = en.apps.specEngine.specEnginePage

/**
 * The value the store substitutes for a withheld one.
 *
 * Spelled here as the wire fixture spells it, because that is what this suite is
 * asserting about: the read RELAYS the marker (`test_a_credential_value_never_leaves_
 * the_surface` pins it against the store's own constant), and the editor must drop
 * whatever it was handed rather than a copy of its own.
 */
const ELIDED = '<elided>'

type Answer = { status?: number; body: unknown }

/** Every request the page made, so an assertion can read the body that was sent. */
const calls: Array<{ url: string; method: string; body: unknown }> = []

/** One resolved role, in `ResolvedRole.detail()`'s shape — optionals omitted. */
function role(
  name: string,
  over: Record<string, unknown> = {},
): Record<string, unknown> {
  return {
    role: name,
    source: 'cost_profile',
    agent: '',
    model: 'auto',
    effort: 'high',
    profile: 'thrifty',
    declared_at: `cost_profiles.thrifty.roles.${name}`,
    ...over,
  }
}

/** The resolved read, in `_resolved_snapshot`'s shape. */
function resolved(over: Record<string, unknown> = {}) {
  return {
    configured: true,
    project: 'acme',
    source: null,
    settings: [
      {
        key: 'limits.task_retry_limit',
        value: 7,
        origin: 'app_config',
        declared_at: 'limits.task_retry_limit',
        is_default: false,
      },
      {
        key: 'budget.warn_fraction',
        value: 0.8,
        origin: 'bundled_default',
        declared_at: '',
        is_default: true,
      },
    ],
    roles: {
      profile: 'thrifty',
      roles: { review: role('review'), design: role('design') },
      project: 'acme',
    },
    role_order: ['review', 'design'],
    ...over,
  }
}

/** A persisted document that declares the review role and selects the profile. */
function document() {
  return {
    cost_profiles: { thrifty: { roles: { review: { model: 'auto', effort: 'high' } } } },
    projects: { acme: { path: '/src/acme', cost_profile: 'thrifty' } },
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
    elided_marker: ELIDED,
    errors: [],
    advisories: [],
    config_only_paths: [],
  }
}

/**
 * Two project entries, one of them carrying overrides beyond its own columns.
 *
 * `acme` declares a limit and a branch list; `widgets` declares nothing but its
 * path. The branch list is what pins the count as a count of DECLARATIONS: it
 * holds two elements and is one decision, so a count of leaves-in-arrays would
 * make `acme` look more configured for having named a second branch.
 */
function twoProjects() {
  return {
    ...document(),
    projects: {
      acme: {
        path: '/src/acme',
        cost_profile: 'thrifty',
        limits: { task_retry_limit: 3 },
        protected_branches: ['main', 'release'],
      },
      widgets: { path: '/src/widgets' },
    },
  }
}

function stub(answers: {
  config?: Answer
  /** The config read after a PUT lands, as the store would then answer it. */
  configAfterPut?: Answer
  resolved?: Answer
  /** Per-project resolved answers, keyed by the `project` parameter (`''` = none). */
  resolvedFor?: Record<string, Answer>
  /** The answer from the SECOND read of a project onwards, for a failed refetch. */
  resolvedAgain?: Record<string, Answer>
  put?: Answer
}) {
  let written = false
  const reads = new Map<string, number>()
  vi.stubGlobal(
    'fetch',
    vi.fn((url: string, init?: RequestInit) => {
      const method = init?.method ?? 'GET'
      calls.push({ url, method, body: init?.body ? JSON.parse(String(init.body)) : undefined })
      let answer: Answer
      if (method === 'PUT') {
        answer = answers.put ?? { body: { ok: true, document: {}, advisories: [] } }
        written = (answer.status ?? 200) < 300
      } else if (url.startsWith('/api/apps/spec-engine/config/resolved')) {
        const project = new URL(url, 'http://gateway.invalid').searchParams.get('project') ?? ''
        const seen = (reads.get(project) ?? 0) + 1
        reads.set(project, seen)
        answer =
          (seen > 1 ? answers.resolvedAgain?.[project] : undefined) ??
          answers.resolvedFor?.[project] ??
          answers.resolved ?? { body: resolved() }
      } else if (url.startsWith('/api/apps/spec-engine/config')) {
        answer =
          (written ? answers.configAfterPut : undefined) ??
          answers.config ?? { body: snapshot(document()) }
      } else if (url.startsWith('/api/apps/spec-engine/kill-switch')) {
        answer = {
          body: { switch: { engaged: false, unreadable: false }, stoppable: [], stoppable_credits: 0 },
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
async function openConfig() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchInterval: false } },
  })
  render(
    <QueryClientProvider client={client}>
      <SpecEnginePage />
    </QueryClientProvider>,
  )
  const nav = await screen.findByRole('button', { name: new RegExp(P.configuration) })
  fireEvent.click(nav)
  return screen.findByRole('button', { name: T.validate_and_save })
}

/** The body of the one PUT the page sent. */
function putBody(): Record<string, unknown> {
  const put = calls.filter((call) => call.method === 'PUT')
  expect(put).toHaveLength(1)
  return (put[0].body as { patch: Record<string, unknown> }).patch
}

afterEach(() => {
  vi.unstubAllGlobals()
  calls.length = 0
})

describe('the merge patch a save sends', () => {
  it('sends only what changed', () => {
    const patch = mergePatch({ a: 1, b: { c: 2, d: 3 } }, { a: 1, b: { c: 9, d: 3 } }, ELIDED)
    expect(patch).toEqual({ b: { c: 9 } })
  })

  it('spells a deleted key as an explicit null', () => {
    // The store's merge KEEPS what a patch omits, so an omitted key is not a
    // deletion — it is a no-op the editor would render as a change that landed.
    expect(mergePatch({ a: 1, b: 2 }, { a: 1 }, ELIDED)).toEqual({ b: null })
    expect(mergePatch({ a: { b: 1, c: 2 } }, { a: { c: 2 } }, ELIDED)).toEqual({ a: { b: null } })
  })

  it('is empty when the document was only reformatted', () => {
    // Key order is not a change. Every write is recorded, so a patch for a
    // reordering would put a line in the durable write record for nothing.
    expect(mergePatch({ a: 1, b: { c: 2, d: 3 } }, { b: { d: 3, c: 2 }, a: 1 }, ELIDED)).toEqual({})
  })

  it('replaces an array whole rather than merging it', () => {
    expect(mergePatch({ gates: ['a', 'b'] }, { gates: ['a'] }, ELIDED)).toEqual({ gates: ['a'] })
  })

  it('never writes the elision marker back, at any depth', () => {
    // The read withholds a credential and shows the marker. Saving it back would
    // replace a live token with the literal string, and the document would stay
    // valid — so nothing else in the stack can catch this.
    const base = { projects: { acme: { variables: { api_key: ELIDED } } } }
    expect(mergePatch(base, base, ELIDED)).toEqual({})
    const edited = { projects: { acme: { variables: { api_key: ELIDED, other: 'x' } } } }
    expect(mergePatch(base, edited, ELIDED)).toEqual({
      projects: { acme: { variables: { other: 'x' } } },
    })
    const added = { fresh: { token: ELIDED, name: 'x' } }
    expect(mergePatch({}, added, ELIDED)).toEqual({ fresh: { name: 'x' } })
    // The case a path-list rule cannot cover, and the reason the rule is on the
    // VALUE: the operator renamed a key while its value was still withheld, so the
    // marker arrives at a path the read never listed as elided. It must not be
    // written, and the old key must still be deleted.
    const renamed = { projects: { acme: { variables: { apiKey: ELIDED } } } }
    expect(mergePatch(base, renamed, ELIDED)).toEqual({
      projects: { acme: { variables: { api_key: null } } },
    })
  })

  it('sends a typed replacement for a withheld value', () => {
    const base = { projects: { acme: { variables: { api_key: ELIDED } } } }
    const edited = { projects: { acme: { variables: { api_key: 'new-token' } } } }
    expect(mergePatch(base, edited, ELIDED)).toEqual({
      projects: { acme: { variables: { api_key: 'new-token' } } },
    })
  })
})

describe('paths are segments, not dotted strings', () => {
  it('addresses a role node through the profile and role names', () => {
    expect(roleSegments('thrifty', 'review')).toEqual([
      'cost_profiles',
      'thrifty',
      'roles',
      'review',
    ])
    expect(patchAt(roleSegments('thrifty', 'review'), null)).toEqual({
      cost_profiles: { thrifty: { roles: { review: null } } },
    })
  })

  it('addresses a profile whose NAME holds a dot', () => {
    // The dotted rendering of this node is
    // `cost_profiles.thrifty.roles.roles.review`, which no split recovers. The
    // segments do, because they never went through a string.
    const segments = roleSegments('thrifty.roles', 'review')
    const doc = { cost_profiles: { 'thrifty.roles': { roles: { review: { model: 'auto' } } } } }
    expect(nodeAt(doc, segments)).toEqual({ model: 'auto' })
    expect(dotted(segments)).toBe('cost_profiles.thrifty.roles.roles.review')
    // And the same path read as dots finds nothing, which is the bug being avoided.
    expect(nodeAt(doc, dotted(segments).split('.'))).toBeUndefined()
  })

  it('compares descendancy segment for segment', () => {
    expect(isDescendant(['a', 'b', 'c'], ['a', 'b'])).toBe(true)
    // `thrifty.roles` is ONE segment, so it is not inside a profile named
    // `thrifty` — a string-prefix rule says the opposite.
    expect(isDescendant(['cost_profiles', 'thrifty.roles'], ['cost_profiles', 'thrifty'])).toBe(
      false,
    )
    // A node is not its own ancestor: clearing a node and clearing something under
    // it are different edits.
    expect(isDescendant(['a', 'b'], ['a', 'b'])).toBe(false)
  })

  it('refuses a document that is not a JSON object', () => {
    expect(parseDocument('[1, 2]', 'not an object')).toEqual({ ok: false, error: 'not an object' })
    expect(parseDocument('{ nope', 'not an object').ok).toBe(false)
    expect(parseDocument('{"a": 1}', 'not an object')).toEqual({ ok: true, document: { a: 1 } })
  })
})

describe('the document editor', () => {
  it('sends a patch, with a deletion spelled as null', async () => {
    stub({})
    const save = await openConfig()
    const editor = screen.getByRole('textbox', { name: T.the_configuration_document })
    const edited = document() as Record<string, unknown>
    delete (edited.limits as Record<string, unknown>).task_retry_limit
    ;(edited as { budget?: unknown }).budget = { warn_fraction: 0.5 }
    fireEvent.change(editor, { target: { value: JSON.stringify(edited, null, 2) } })
    fireEvent.click(save)
    await waitFor(() => expect(calls.some((call) => call.method === 'PUT')).toBe(true))
    expect(putBody()).toEqual({
      limits: { task_retry_limit: null },
      budget: { warn_fraction: 0.5 },
    })
  })

  it('sends nothing when the document matches what is saved', async () => {
    stub({})
    const save = await openConfig()
    fireEvent.click(save)
    expect(await screen.findByText(T.nothing_to_save)).toBeInTheDocument()
    expect(calls.some((call) => call.method === 'PUT')).toBe(false)
  })

  it('refuses invalid JSON locally, before any request', async () => {
    stub({})
    const save = await openConfig()
    const editor = screen.getByRole('textbox', { name: T.the_configuration_document })
    fireEvent.change(editor, { target: { value: '{ "a": ' } })
    fireEvent.click(save)
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent(T.the_document_is_not_valid_json)
    expect(calls.some((call) => call.method === 'PUT')).toBe(false)
  })

  it('states the engine refusal with its code, and keeps the edit', async () => {
    stub({
      put: {
        status: 422,
        body: { code: 'config_invalid', error: 'limits.task_retry_limit: must be at least 1' },
      },
    })
    const save = await openConfig()
    const editor = screen.getByRole('textbox', { name: T.the_configuration_document })
    fireEvent.change(editor, {
      target: { value: JSON.stringify({ limits: { task_retry_limit: -1 } }, null, 2) },
    })
    fireEvent.click(save)
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent(T.could_not_save_the_configuration)
    expect(alert).toHaveTextContent('config_invalid')
    // The operator's text survives a refusal: they have to fix it, and a pane that
    // reverted to the saved document would discard the edit they were asked about.
    expect((editor as HTMLTextAreaElement).value).toContain('-1')
  })

  it('relays the advisories a write earned', async () => {
    stub({
      put: {
        body: {
          ok: true,
          document: {},
          advisories: [
            {
              code: 'unattended_integration',
              path: 'sources.github.autonomy',
              message: 'integration runs with nothing verifying it',
              project: null,
              requires_acknowledgment: true,
            },
          ],
        },
      },
    })
    const save = await openConfig()
    const editor = screen.getByRole('textbox', { name: T.the_configuration_document })
    fireEvent.change(editor, { target: { value: JSON.stringify({ limits: {} }, null, 2) } })
    fireEvent.click(save)
    expect(await screen.findByText(T.saved_the_document)).toBeInTheDocument()
    expect(screen.getByText('unattended_integration')).toBeInTheDocument()
    // An advisory a human must answer for is marked as such, because it is a
    // different obligation from one they only read.
    expect(screen.getByText(T.acknowledgment_required)).toBeInTheDocument()
  })

  it('says a withheld value can be overwritten but never redisplayed', async () => {
    stub({})
    await openConfig()
    expect(screen.getByText(P.secret_values_are_withheld_from_this_read)).toBeInTheDocument()
    expect(screen.getByText(T.elided_values_are_never_written_back)).toBeInTheDocument()
    expect(screen.getByText(T.deletions_are_sent_as_explicit_nulls)).toBeInTheDocument()
  })
})

describe('the resolved read beside the document', () => {
  it('renders the engine\u2019s own role plan, in the engine\u2019s order', async () => {
    stub({})
    await openConfig()
    const table = await screen.findByRole('table')
    const rows = within(table).getAllByRole('row').slice(1)
    expect(rows).toHaveLength(2)
    // The engine's order, relayed: a JSON object has none a client may rely on.
    expect(rows[0]).toHaveTextContent('review')
    expect(rows[1]).toHaveTextContent('design')
    expect(within(table).getAllByText('auto').length).toBe(2)
  })

  it('labels the reset with the node it clears, and clears exactly that node', async () => {
    stub({})
    await openConfig()
    const table = await screen.findByRole('table')
    const clear = await within(table).findByRole('button', {
      name: T.clear_node.replace('{{path}}', 'cost_profiles.thrifty.roles.review'),
    })
    fireEvent.click(clear)
    await waitFor(() => expect(calls.some((call) => call.method === 'PUT')).toBe(true))
    expect(putBody()).toEqual({ cost_profiles: { thrifty: { roles: { review: null } } } })
  })

  it('offers nothing to reset for a role no node declares', async () => {
    // `design` resolves from the profile in the fixture but the DOCUMENT has no
    // node for it, which is the mockup's disabled button with the missing path in
    // its tooltip. Reading the resolution alone would offer a reset for a node
    // that does not exist, and the write would then create-then-delete nothing.
    stub({})
    await openConfig()
    const table = await screen.findByRole('table')
    const disabled = within(table).getByRole('button', { name: T.nothing_to_reset })
    expect(disabled).toBeDisabled()
    expect(disabled).toHaveAttribute(
      'title',
      T.no_node_exists_at.replace('{{path}}', 'cost_profiles.thrifty.roles.design'),
    )
  })

  it('offers no reset when the declaring profile is not the one in force', async () => {
    // The segment-wise rule, at the control it protects. The role's node is under a
    // profile NAMED `thrifty.roles`, which EXISTS in the document; the profile in
    // force is `thrifty`. A string-prefix match reads the first as a path inside the
    // second, and the reset it then offers would clear the role assignment of a
    // profile some other project selected.
    stub({
      config: {
        body: {
          configured: true,
          path: '/home/me/.kiro/crew/apps/spec-engine/config.json',
          document: {
            cost_profiles: {
              'thrifty.roles': { roles: { review: { model: 'auto', effort: 'high' } } },
            },
            projects: { acme: { path: '/src/acme', cost_profile: 'thrifty' } },
          },
          elided: [],
          elided_marker: ELIDED,
          errors: [],
          advisories: [],
          config_only_paths: [],
        },
      },
      resolved: {
        body: resolved({
          roles: {
            profile: 'thrifty',
            roles: {
              review: role('review', {
                profile: 'thrifty.roles',
                declared_at: 'cost_profiles.thrifty.roles.roles.review',
              }),
            },
            project: 'acme',
          },
          role_order: ['review'],
        }),
      },
    })
    await openConfig()
    const table = await screen.findByRole('table')
    expect(within(table).getByRole('button', { name: T.nothing_to_reset })).toBeDisabled()
    expect(within(table).queryByText(/^Clear /)).toBeNull()
  })

  it('states that the node is the shared profile\u2019s, not the project\u2019s', async () => {
    // The one departure from the mockup, which reset a per-project role override
    // the engine does not have. A label reading `Reset` would hide the blast radius.
    stub({})
    await openConfig()
    expect(await screen.findByText(T.a_role_lives_on_the_shared_profile)).toBeInTheDocument()
  })

  it('shows the values that are not at their default, with the origin of each', async () => {
    stub({})
    await openConfig()
    expect(await screen.findByText('limits.task_retry_limit')).toBeInTheDocument()
    expect(screen.getByText(new RegExp(T.origin_app_config))).toBeInTheDocument()
    // The default-valued setting is counted, not listed: the interesting rows are
    // the decisions somebody made.
    expect(screen.queryByText('budget.warn_fraction')).toBeNull()
    expect(screen.getByText(new RegExp(T.settings_at_their_bundled_default))).toBeInTheDocument()
  })

  it('reports a failed resolution as a refusal, not as an empty table', async () => {
    stub({ resolved: { status: 503, body: { code: 'config_unreadable', error: 'disk gone' } } })
    await openConfig()
    const alerts = await screen.findAllByRole('alert')
    const refusal = alerts.find((node) =>
      node.textContent?.includes(T.could_not_resolve_the_configuration),
    )
    expect(refusal).toBeDefined()
    expect(refusal).toHaveTextContent('config_unreadable')
    expect(screen.queryByRole('table')).toBeNull()
  })

  it('makes no request of its own that writes', async () => {
    stub({})
    await openConfig()
    await screen.findByRole('table')
    // The resolved pane is a READ. Its only write is the reset, which is not
    // clicked here.
    expect(calls.filter((call) => call.method !== 'GET')).toEqual([])
  })

  it('shows a default-valued setting with its origin when every setting is asked for', async () => {
    // A setting whose value equals the default because somebody PINNED it there is
    // only distinguishable from an untouched one by its origin, so every setting
    // has to be reachable — collapsed by default, never absent.
    stub({})
    await openConfig()
    const toggle = await screen.findByRole('button', {
      name: T.show_every_setting.replace('{{count}}', '2'),
    })
    fireEvent.click(toggle)
    expect(await screen.findByText(T.every_setting_in_force)).toBeInTheDocument()
    expect(screen.getByText('budget.warn_fraction')).toBeInTheDocument()
    expect(screen.getByText(new RegExp(T.origin_bundled_default))).toBeInTheDocument()
  })
})

describe('the projects table', () => {
  /** The projects grid, which is not the roles table beside it. */
  const grid = () => screen.getByRole('grid', { name: T.configured_projects })

  /** Every row of it but the header. */
  const rows = () => within(grid()).getAllByRole('row').slice(1)

  /** The row-level removal control for one project, named by its target. */
  const removeButton = (project: string) =>
    within(grid()).getByRole('button', {
      name: T.remove_project.replace('{{project}}', project),
    })

  /** The confirm inside the armed block, which names the entry it deletes. */
  const confirmButton = (project: string) =>
    screen.getByRole('button', { name: T.confirm_the_removal.replace('{{project}}', project) })

  /** The document after `acme` is gone, as the store would answer it. */
  function withoutAcme() {
    return { ...twoProjects(), projects: { widgets: { path: '/src/widgets' } } }
  }

  /** The document after `widgets` is gone, as the store would answer it. */
  function withoutWidgets() {
    const doc = twoProjects()
    return { ...doc, projects: { acme: doc.projects.acme } }
  }

  it('lists every entry with its pinned profile and override count, under an app-defaults row', async () => {
    stub({ config: { body: snapshot(twoProjects()) } })
    await openConfig()
    const listed = rows()
    expect(listed).toHaveLength(3)
    // The app-wide resolution is a ROW, because it is one of the resolutions an
    // operator compares against: it is what a project's values fall back to.
    expect(listed[0]).toHaveTextContent(T.no_project_app_wide)
    expect(within(listed[0]).queryByRole('button')).toBeNull()
    expect(listed[1]).toHaveTextContent('acme')
    expect(listed[1]).toHaveTextContent('thrifty')
    // One limit and one branch list: the list is ONE declaration, so a longer
    // branch list does not make the project look more configured.
    expect(listed[1]).toHaveTextContent('2')
    expect(listed[2]).toHaveTextContent('widgets')
    expect(listed[2]).toHaveTextContent('0')
  })

  it('resolves for the project whose row is selected, and app-wide for the app-defaults row', async () => {
    stub({
      config: { body: snapshot(twoProjects()) },
      resolvedFor: {
        widgets: {
          body: resolved({
            project: 'widgets',
            settings: [
              {
                key: 'budget.run_credit_ceiling',
                value: 12,
                origin: 'project_config',
                declared_at: 'projects.widgets.budget.run_credit_ceiling',
                is_default: false,
              },
            ],
          }),
        },
      },
    })
    await openConfig()
    // The app-defaults row is selected first, and it names no project at all —
    // the parameter is absent rather than empty.
    await waitFor(() =>
      expect(
        calls.some((call) => call.url === '/api/apps/spec-engine/config/resolved'),
      ).toBe(true),
    )
    expect(await screen.findByText(T.resolved_app_wide)).toBeInTheDocument()

    fireEvent.click(within(grid()).getByText('widgets'))
    // The heading follows the selection immediately; the values arrive with the
    // read, so the assertion waits for the value rather than the label.
    const key = await screen.findByText('budget.run_credit_ceiling')
    expect(
      screen.getByText(T.resolved_for_project.replace('{{project}}', 'widgets')),
    ).toBeInTheDocument()
    // The value and its origin scope on the same entry: a value without its
    // origin cannot answer whether somebody chose it or the app ships it.
    expect(key.nextElementSibling).toHaveTextContent('12')
    expect(key.nextElementSibling).toHaveTextContent(T.origin_project_config)
    expect(key.nextElementSibling).toHaveTextContent(
      'projects.widgets.budget.run_credit_ceiling',
    )
    expect(
      calls.some(
        (call) => call.url === '/api/apps/spec-engine/config/resolved?project=widgets',
      ),
    ).toBe(true)
  })

  it('moves between rows with the arrow keys and with j/k', async () => {
    stub({ config: { body: snapshot(twoProjects()) } })
    await openConfig()
    const press = (key: string) => {
      const focused = rows().find(
        (row) => row.getAttribute('aria-selected') === 'true',
      ) as HTMLElement
      focused.focus()
      focused.dispatchEvent(new KeyboardEvent('keydown', { key, bubbles: true }))
    }
    // Exactly one row in the tab order, as the queue table does it: the rest are
    // reachable by arrow key rather than costing a tab stop each.
    expect(rows().filter((row) => row.getAttribute('tabindex') === '0')).toHaveLength(1)
    press('ArrowDown')
    await waitFor(() => expect(rows()[1]).toHaveAttribute('aria-selected', 'true'))
    press('j')
    await waitFor(() => expect(rows()[2]).toHaveAttribute('aria-selected', 'true'))
    press('k')
    await waitFor(() => expect(rows()[1]).toHaveAttribute('aria-selected', 'true'))
    press('Home')
    await waitFor(() => expect(rows()[0]).toHaveAttribute('aria-selected', 'true'))
    press('End')
    await waitFor(() => expect(rows()[2]).toHaveAttribute('aria-selected', 'true'))
    // Selection follows focus, so traversal alone resolves for the row reached.
    expect(
      calls.some(
        (call) => call.url === '/api/apps/spec-engine/config/resolved?project=widgets',
      ),
    ).toBe(true)
  })

  it('states a failed per-project resolution instead of another project\u2019s values', async () => {
    stub({
      config: { body: snapshot(twoProjects()) },
      resolvedFor: {
        acme: { body: resolved() },
        widgets: { status: 503, body: { code: 'config_unreadable', error: 'disk gone' } },
      },
    })
    await openConfig()
    fireEvent.click(within(grid()).getByText('acme'))
    expect(await screen.findByText('limits.task_retry_limit')).toBeInTheDocument()

    fireEvent.click(within(grid()).getByText('widgets'))
    const alerts = await screen.findAllByRole('alert')
    const refusal = alerts.find((node) =>
      node.textContent?.includes(T.could_not_resolve_the_configuration),
    )
    expect(refusal).toBeDefined()
    expect(refusal).toHaveTextContent('config_unreadable')
    // One query per project. A single shared key would leave `acme`'s answer in
    // the cache and render it under a heading naming `widgets`.
    expect(screen.queryByText('limits.task_retry_limit')).toBeNull()
  })

  it('states a failed refetch instead of the values it had before', async () => {
    // The retained-data trap, at the read most exposed to it: React Query keeps
    // the last successful answer across a failed refetch, so a view that reached
    // for the data before checking `isError` would keep presenting a resolution
    // the gateway can no longer produce as the one in force.
    stub({
      config: { body: snapshot(twoProjects()) },
      configAfterPut: { body: snapshot(withoutAcme()) },
      resolvedFor: { acme: { body: resolved() } },
      resolvedAgain: {
        acme: { status: 503, body: { code: 'config_unreadable', error: 'disk gone' } },
      },
    })
    await openConfig()
    fireEvent.click(within(grid()).getByText('acme'))
    expect(await screen.findByText('limits.task_retry_limit')).toBeInTheDocument()

    // A landed removal invalidates the resolved read; this refetch fails while
    // the previous answer is still cached under the same key.
    fireEvent.click(removeButton('widgets'))
    fireEvent.click(confirmButton('widgets'))
    await waitFor(() => expect(screen.queryByText('limits.task_retry_limit')).toBeNull())
    const alerts = await screen.findAllByRole('alert')
    expect(
      alerts.some((node) =>
        node.textContent?.includes(T.could_not_resolve_the_configuration),
      ),
    ).toBe(true)
  })

  it('arms before it removes, and sends a null at exactly the one entry', async () => {
    stub({
      config: { body: snapshot(twoProjects()) },
      configAfterPut: { body: snapshot(withoutAcme()) },
    })
    await openConfig()
    fireEvent.click(removeButton('acme'))
    // Arming writes nothing: the destructive step is two steps, and the first one
    // is not a request.
    expect(calls.some((call) => call.method === 'PUT')).toBe(false)

    fireEvent.click(confirmButton('acme'))
    await waitFor(() => expect(calls.some((call) => call.method === 'PUT')).toBe(true))
    expect(putBody()).toEqual({ projects: { acme: null } })
    // Spelled out, because both halves of this are load-bearing: the null IS the
    // deletion (the store's merge keeps every key a patch omits, so dropping it
    // sends a no-op the table would render as a removal that happened), and the
    // patch names ONE entry (a wider one would delete the whole section).
    expect(JSON.stringify(putBody())).toBe('{"projects":{"acme":null}}')
  })

  it('re-renders from the document the store answers after the write', async () => {
    stub({
      config: { body: snapshot(twoProjects()) },
      configAfterPut: { body: snapshot(withoutAcme()) },
    })
    await openConfig()
    fireEvent.click(removeButton('acme'))
    fireEvent.click(confirmButton('acme'))
    await waitFor(() => expect(rows()).toHaveLength(2))
    expect(within(grid()).queryByText('acme')).toBeNull()
    expect(
      screen.getByText(T.removed_the_project_entry.replace('{{project}}', 'acme')),
    ).toBeInTheDocument()
  })

  it('keeps the entry and states the refusal when the write is refused', async () => {
    stub({
      config: { body: snapshot(twoProjects()) },
      put: {
        status: 403,
        body: { code: 'config_write_refused', error: 'projects is not writable here' },
      },
    })
    await openConfig()
    fireEvent.click(removeButton('acme'))
    fireEvent.click(confirmButton('acme'))
    const alerts = await screen.findAllByRole('alert')
    const refusal = alerts.find((node) =>
      node.textContent?.includes(T.could_not_remove_the_project_entry),
    )
    expect(refusal).toBeDefined()
    expect(refusal).toHaveTextContent('config_write_refused')
    // A refused write returns no document, so the entry is still there — and the
    // pane must not report a removal it only asked for.
    expect(within(grid()).getByText('acme')).toBeInTheDocument()
    expect(
      screen.queryByText(T.removed_the_project_entry.replace('{{project}}', 'acme')),
    ).toBeNull()
  })

  it('stops resolving for an entry it just removed', async () => {
    // A selection pointing at a deleted entry resolves through the app-wide
    // layers and would be LABELLED as that project, which reads as "this project
    // inherits everything" rather than "this project is gone".
    stub({
      config: { body: snapshot(twoProjects()) },
      configAfterPut: { body: snapshot(withoutAcme()) },
    })
    await openConfig()
    fireEvent.click(within(grid()).getByText('acme'))
    expect(
      await screen.findByText(T.resolved_for_project.replace('{{project}}', 'acme')),
    ).toBeInTheDocument()
    fireEvent.click(removeButton('acme'))
    fireEvent.click(confirmButton('acme'))
    expect(await screen.findByText(T.resolved_app_wide)).toBeInTheDocument()
    expect(
      screen.queryByText(T.resolved_for_project.replace('{{project}}', 'acme')),
    ).toBeNull()
  })

  it('withdraws an arm whose entry left the document', async () => {
    // The editor beside the table can delete the same entry while the confirm
    // sits on screen, and a confirm would then send a deletion for a key that is
    // no longer there — a recorded write for a change nobody made.
    stub({
      config: { body: snapshot(twoProjects()) },
      configAfterPut: { body: snapshot(withoutWidgets()) },
    })
    await openConfig()
    fireEvent.click(removeButton('widgets'))
    expect(confirmButton('widgets')).toBeInTheDocument()

    const editor = screen.getByRole('textbox', { name: T.the_configuration_document })
    fireEvent.change(editor, { target: { value: JSON.stringify(withoutWidgets(), null, 2) } })
    fireEvent.click(screen.getByRole('button', { name: T.validate_and_save }))
    await waitFor(() => expect(rows()).toHaveLength(2))
    expect(
      screen.queryByRole('button', {
        name: T.confirm_the_removal.replace('{{project}}', 'widgets'),
      }),
    ).toBeNull()
  })

  it('says what the override count counts', async () => {
    // A number beside a project is worth nothing if a reader cannot tell whether
    // the pinned profile in the column next to it is one of the things counted.
    stub({ config: { body: snapshot(twoProjects()) } })
    await openConfig()
    expect(await screen.findByText(T.overrides_counts_declared_values)).toBeInTheDocument()
  })

  it('says so when the document holds no project at all', async () => {
    stub({ config: { body: snapshot({ limits: { task_retry_limit: 7 } }) } })
    await openConfig()
    expect(rows()).toHaveLength(1)
    expect(await screen.findByText(T.no_project_is_configured_yet)).toBeInTheDocument()
  })
})
