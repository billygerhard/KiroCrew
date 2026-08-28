/**
 * The Settings tab groups its generated rows into the registry's own subsections,
 * with an in-flow jump navigation.
 *
 * The rows themselves are the settings form's — generated, staged, reviewed and
 * written exactly as `SpecEngineSettingsForm.test.tsx` pins. What this file adds is
 * the structure over them, and each claim is a correctness one rather than a
 * layout preference:
 *
 *   - **The subsections are the registry's groups.** One per distinct leading
 *     dot-segment, in first-appearance order, and every generated row appears under
 *     its own group. The pure partition behind it is property-checked in
 *     `SpecEngineSettingsGroups.property.test.tsx`; here it is checked on the shipped
 *     vocabulary an operator actually meets.
 *   - **An authored group has a human heading; an unmapped one keeps its segment.**
 *     A label leads with the raw segment as the detail line, the settings-row idiom,
 *     and a group no catalog names heads with the raw segment rather than being
 *     dropped.
 *   - **The jump navigation is present only with more than one subsection**, is in
 *     flow (no sticky or floating positioning), and scrolls to the subsection it
 *     names on activation.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import SpecEnginePage from '../apps/spec-engine/SpecEnginePage'
import en from '../i18n/locales/en.json'
import {
  stagesUnder,
  stubSpecEngineFetch,
  expectEverySpecEngineRouteAnswered,
  type Answer,
} from './specEngineFetchStub'

const T = en.apps.specEngine.settingsForm
const C = en.apps.specEngine.configPanel
const P = en.apps.specEngine.specEnginePage
const G = C.group_labels

/** One registry setting, in `_registry_payload`'s shape. */
function setting(key: string, over: Record<string, unknown> = {}) {
  return {
    key,
    kind: 'int',
    default: 1,
    minimum: 0,
    maximum: null,
    scopes: ['app'],
    summary: `Summary for ${key}.`,
    ...over,
  }
}

/**
 * One resolved setting, in `EffectiveValue.to_json_object`'s shape.
 *
 * App-configured rather than defaulted, because a settings surface renders only the
 * rows whose in-force value is not the bundled default: a fixture of defaulted
 * values would collapse every subsection this suite is about into a count. The
 * grouping is the subject here, so the vocabulary is one an operator has configured
 * — and the collapsing itself is `SpecEngineSettingsForm.test.tsx`'s.
 */
function effective(key: string, value: unknown = 1): Record<string, unknown> {
  return { key, value, origin: 'app_config', declared_at: key, is_default: false }
}

/**
 * The shipped-shape vocabulary: six settings, each in a distinct registry group,
 * so the grouped form has six subsections and a jump navigation.
 */
const SHIPPED = [
  'limits.task_retry_limit',
  'budget.warn_fraction',
  'delivery.auto_integrate',
  'notify.channel',
  'watch.interval_s',
  'concurrency.global_max_runs',
]

/**
 * The registry payload for *keys*, with every group they declare placed in ONE
 * pipeline stage.
 *
 * The pane is organised by pipeline stage now, and a stage renders only its own
 * groups — so a fixture spread across four stages would have four one-group
 * subsections and no jump navigation anywhere, which is a claim about the SHELL
 * rather than about the grouping this suite is for. One area holding all of them is
 * a projection the engine itself produces (it places four groups under execution),
 * and it keeps every claim here about the subsections.
 */
function registry(keys: string[]) {
  const groups: string[] = []
  for (const key of keys) {
    const dot = key.indexOf('.')
    const group = dot < 0 ? key : key.slice(0, dot)
    if (!groups.includes(group)) groups.push(group)
  }
  return {
    settings: keys.map((key) => setting(key)),
    source_presets: [],
    profile_presets: [],
    roles: [],
    levels: [],
    stages: stagesUnder('execution', groups),
  }
}

function resolved(keys: string[]) {
  return {
    configured: true,
    project: null,
    source: null,
    settings: keys.map((key) => effective(key)),
    roles: { profile: '', roles: {} },
    role_order: [],
  }
}

function snapshot() {
  return {
    configured: true,
    path: '/home/me/.kiro/crew/apps/spec-engine/config.json',
    document: { projects: {} },
    elided: [],
    elided_marker: '<elided>',
    errors: [],
    advisories: [],
    config_only_paths: [],
  }
}

function stub(keys: string[], over: { registry?: Answer; resolved?: Answer } = {}) {
  stubSpecEngineFetch({
    registry: over.registry ?? { body: registry(keys) },
    resolved: over.resolved ?? { body: resolved(keys) },
    sources: { body: { sources: [], submitter_classes: [], spec_types: [], levels: [] } },
    config: { body: snapshot() },
  })
}

/** Render the page, open the configuration pane, wait for the settings block. */
async function openRows(keys: string[], over: Parameters<typeof stub>[1] = {}) {
  stub(keys, over)
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchInterval: false } },
  })
  render(
    <QueryClientProvider client={client}>
      <SpecEnginePage />
    </QueryClientProvider>,
  )
  fireEvent.click(await screen.findByRole('button', { name: new RegExp(P.configuration) }))
  // One area per pipeline stage, and only the active one is in the accessibility
  // tree the role queries read: an inactive panel carries `hidden`. This suite's
  // fixture places every group under execution, so that is the area to open.
  await screen.findByRole('tablist', { name: C.configuration_stages })
  fireEvent.click(screen.getByRole('tab', { name: new RegExp(`^${C.stage_execution}`) }))
  await screen.findByRole('heading', { name: T.settings })
  await waitFor(() => expect(block().querySelectorAll('.se-setting').length).toBeGreaterThan(0))
}

/** The settings block, scoped away from the resolved pane's identical labels. */
function block(): HTMLElement {
  const heading = screen.getByRole('heading', { name: T.settings })
  const found = heading.closest('.se-blk')
  expect(found).not.toBeNull()
  return found as HTMLElement
}

/** One group's subsection element. */
function section(group: string): HTMLElement {
  const found = block().querySelector(`#se-settings-group-${group}`)
  expect(found, group).not.toBeNull()
  return found as HTMLElement
}

/** The in-flow jump navigation, or null when it is not rendered. */
function jumpNav(): HTMLElement | null {
  return within(block()).queryByRole('group', { name: C.jump_to_a_settings_section })
}

afterEach(() => {
  vi.unstubAllGlobals()
  // Nothing the page asked for went unanswered by the shared stub. Without this a
  // product URL can drift out from under the table and this suite still passes: the
  // stub's 599 refusal reaches the surface as an ordinary error, so a test whose
  // subject is a read failure renders the copy it asserts for either way.
  expectEverySpecEngineRouteAnswered()
})

describe('the rows are grouped into the registry’s subsections', () => {
  it('renders one subsection per distinct group, each holding its own rows', async () => {
    await openRows(SHIPPED)
    // Every distinct group has a subsection, and each row sits inside its own.
    for (const group of ['limits', 'budget', 'delivery', 'notify', 'watch', 'concurrency']) {
      expect(section(group)).toBeInTheDocument()
    }
    // The retry row is inside the limits subsection and nowhere else.
    expect(within(section('limits')).getAllByText('limits.task_retry_limit').length).toBeGreaterThan(0)
    expect(within(section('watch')).getAllByText('watch.interval_s').length).toBeGreaterThan(0)
    // Every generated row is still present and addressable: grouping wraps them,
    // it does not drop them.
    expect(block().querySelectorAll('.se-setting')).toHaveLength(SHIPPED.length)
  })

  it('heads an authored group with its label and the raw segment beside it', async () => {
    await openRows(SHIPPED)
    const limits = section('limits')
    // Prose leads, from the catalog.
    expect(within(limits).getByText(G.limits)).toBeInTheDocument()
    // The raw segment stays on screen as the detail line — it is what the document
    // and the write log speak.
    expect(within(limits).getByText('limits', { selector: '.se-kv-path' })).toBeInTheDocument()
    // A second authored group, so the label is the group's own rather than shared.
    expect(within(section('watch')).getByText(G.watch)).toBeInTheDocument()
  })

  it('heads an unmapped group with its raw segment rather than dropping it', async () => {
    // A group no catalog names still gets a subsection headed by its segment.
    const keys = ['exotic.window', 'limits.task_retry_limit']
    await openRows(keys)
    const exotic = section('exotic')
    expect(exotic).toBeInTheDocument()
    expect(within(exotic).getByText('exotic', { selector: '.se-m' })).toBeInTheDocument()
    // Its row is present under it.
    expect(within(exotic).getAllByText('exotic.window').length).toBeGreaterThan(0)
  })
})

describe('the jump navigation', () => {
  it('offers one button per subsection when there is more than one', async () => {
    await openRows(SHIPPED)
    const nav = jumpNav()
    expect(nav).not.toBeNull()
    const buttons = within(nav as HTMLElement).getAllByRole('button')
    expect(buttons).toHaveLength(6)
    expect(within(nav as HTMLElement).getByRole('button', { name: G.limits })).toBeInTheDocument()
    expect(within(nav as HTMLElement).getByRole('button', { name: G.concurrency })).toBeInTheDocument()
  })

  it('scrolls to the subsection it names on activation', async () => {
    const scrolled: string[] = []
    const original = Element.prototype.scrollIntoView
    Element.prototype.scrollIntoView = vi.fn(function (this: Element) {
      scrolled.push(this.id)
    }) as unknown as typeof Element.prototype.scrollIntoView
    try {
      await openRows(SHIPPED)
      fireEvent.click(within(jumpNav() as HTMLElement).getByRole('button', { name: G.watch }))
      expect(scrolled).toContain('se-settings-group-watch')
    } finally {
      Element.prototype.scrollIntoView = original
    }
  })

  it('is absent with a single-group vocabulary', async () => {
    // One group is its own heading, so there is nowhere to jump to.
    await openRows(['limits.task_retry_limit', 'limits.revision_cycle_limit'])
    expect(section('limits')).toBeInTheDocument()
    expect(jumpNav()).toBeNull()
    // The rows are still there and still grouped under the one subsection.
    expect(block().querySelectorAll('.se-setting')).toHaveLength(2)
    cleanup()
  })

  it('places the jump navigation in flow, never positioned over the rows', async () => {
    await openRows(SHIPPED)
    const nav = jumpNav() as HTMLElement
    // No inline positioning, and it carries the flat filter idiom rather than an
    // overlay class: the pane's layout holds only because nothing floats over it.
    expect(nav.style.position).toBe('')
    expect(nav.className).toContain('se-jump')
  })
})
