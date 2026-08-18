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

function stub(answers: {
  config?: Answer
  resolved?: Answer
  put?: Answer
}) {
  vi.stubGlobal(
    'fetch',
    vi.fn((url: string, init?: RequestInit) => {
      const method = init?.method ?? 'GET'
      calls.push({ url, method, body: init?.body ? JSON.parse(String(init.body)) : undefined })
      let answer: Answer
      if (method === 'PUT') {
        answer = answers.put ?? { body: { ok: true, document: {}, advisories: [] } }
      } else if (url.startsWith('/api/apps/spec-engine/config/resolved')) {
        answer = answers.resolved ?? { body: resolved() }
      } else if (url.startsWith('/api/apps/spec-engine/config')) {
        answer =
          answers.config ?? {
            body: {
              configured: true,
              path: '/home/me/.kiro/crew/apps/spec-engine/config.json',
              document: document(),
              elided: [],
              elided_marker: ELIDED,
              errors: [],
              advisories: [],
              config_only_paths: [],
            },
          }
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
})
