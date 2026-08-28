/**
 * The confirmation card: plain language leads, the exact patch is one disclosure
 * away, and what is submitted is what the disclosure showed.
 *
 * Every form on the configuration pane confirms through this one card, so the
 * claims below are the pane's write discipline stated once rather than per form:
 *
 *   - **The summary is the leading content.** Sentences naming each change and what
 *     it does come first; the payload sits behind a `<details>` that starts closed.
 *     A reader who meets the JSON first is a reader approving a JSON payload, which
 *     is exactly the complaint this ordering answers.
 *   - **What is submitted is byte-identical to what the disclosure shows.** The card
 *     renders one string and confirms with the value parsed BACK from that string,
 *     so no path, key or value can reach the write door without having been
 *     displayed. Pinned here on hand-written patches and over generated edit sets in
 *     `SpecEngineFormReview.property.test.tsx`.
 *   - **Each of the four authority changes is stated before the confirm control.**
 *     Raising an untrusted class, removing a gate, binding a capability to an
 *     external program, and authorising commands to run are all invisible in a
 *     patch, and each has one wording owned by the card rather than one per form.
 *   - **A fenced path says why an operator is the one confirming.** The list comes
 *     from `GET /config`'s relay of the engine's own `CONFIG_ONLY_PATHS`; this side
 *     keeps no copy, and a read that failed marks nothing rather than asserting a
 *     refusal it could not confirm.
 *   - **Nothing is an overlay.** The disclosure expands in place. No dialog, no
 *     scrim: the strip carrying the kill switch must never be covered.
 */
import { describe, it, expect, afterEach } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import { FormReview, fencedPatchPaths } from '../apps/spec-engine/ConfigPanel'
import type { Document } from '../apps/spec-engine/configDocument'
import en from '../i18n/locales/en.json'
import {
  stubSpecEngineFetch,
  failure,
  expectEverySpecEngineRouteAnswered,
  type Responder,
} from './specEngineFetchStub'

const F = en.apps.specEngine.formReview

/** The labels a caller supplies, standing in for any one form's copy. */
const LABELS = {
  heading: 'The change that would be written',
  confirm: 'Write the change',
  writing: 'Saving…',
  discard: 'Discard the pending changes',
  exactly: 'A confirm writes exactly this patch.',
  refusalTitle: 'Could not write the change',
  retained: 'Nothing was written.',
}

/** A configuration snapshot whose only interesting field is the fenced list. */
function snapshot(configOnly: string[]): Record<string, unknown> {
  return {
    configured: true,
    path: '/home/me/.kiro/crew/apps/spec-engine/config.json',
    document: { projects: {} },
    elided: [],
    elided_marker: '<elided>',
    errors: [],
    advisories: [],
    config_only_paths: configOnly,
  }
}

/** What one mount hands back: what a confirm submitted, and the read to wait on. */
type Mounted = {
  submitted: Document[]
  discarded: number
  /**
   * Resolve once the card's own `/config` read has settled.
   *
   * A negative assertion about the fenced marks is worthless without it: the marks
   * appear only after that read lands, so "no mark is on screen" is trivially true
   * one tick after mount and would hold with a hardcoded path list too. Waiting on
   * the query's own state is what makes the absence a statement about the card
   * rather than about timing.
   */
  settled: (outcome: 'success' | 'error') => Promise<void>
  /** Ask the card's read again, for the failing-refetch case. */
  reread: () => Promise<void>
}

function mount(
  props: Partial<Parameters<typeof FormReview>[0]> & { patch: Document },
  options: { configOnly?: string[]; configFails?: boolean; config?: Responder } = {},
): Mounted {
  stubSpecEngineFetch({
    config:
      options.config ??
      (options.configFails
        ? failure(503, 'config_unreadable')
        : { body: snapshot(options.configOnly ?? []) }),
  })
  const submitted: Document[] = []
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchInterval: false } },
  })
  const state: Mounted = {
    submitted,
    discarded: 0,
    settled: async (outcome) => {
      await waitFor(() =>
        expect(client.getQueryState(['spec-engine', 'config'])?.status).toBe(outcome),
      )
    },
    reread: async () => {
      await client.refetchQueries({ queryKey: ['spec-engine', 'config'] })
    },
  }
  render(
    <QueryClientProvider client={client}>
      <FormReview
        changes={props.changes ?? []}
        patch={props.patch}
        labels={LABELS}
        authorises={props.authorises}
        consequences={props.consequences}
        writing={props.writing ?? false}
        error={props.error ?? null}
        onConfirm={(patch) => submitted.push(patch)}
        onDiscard={() => {
          state.discarded += 1
        }}
      />
    </QueryClientProvider>,
  )
  return state
}

/** The card itself, so a query cannot stray outside it. */
function card(): HTMLElement {
  const heading = screen.getByRole('heading', { name: LABELS.heading })
  const found = heading.closest('.se-qbox')
  if (!found) throw new Error('the review card is not on screen')
  return found as HTMLElement
}

/** The disclosure holding the exact patch. */
function disclosure(): HTMLDetailsElement {
  const found = card().querySelector('details.se-disc')
  if (!found) throw new Error('the patch disclosure is not on screen')
  return found as HTMLDetailsElement
}

/** The patch text the disclosure is showing, exactly as rendered. */
function shownText(): string {
  const pre = disclosure().querySelector('pre.se-gpatch')
  if (!pre) throw new Error('the patch is not in the disclosure')
  return pre.textContent ?? ''
}

/** The confirm control. */
function confirmButton(): HTMLElement {
  return within(card()).getByRole('button', { name: LABELS.confirm })
}

/** Whether *first* comes before *second* in document order. */
function precedes(first: Node, second: Node): boolean {
  return (first.compareDocumentPosition(second) & Node.DOCUMENT_POSITION_FOLLOWING) !== 0
}

/**
 * The fenced-path notes on screen, as their full sentences.
 *
 * Matched on the copy's own tail — everything the sentence says after the path it
 * names — so the assertion is about the card's statement rather than about a
 * substring somebody could change the meaning of.
 */
const FENCED_TAIL = F.only_an_operator_confirmation_writes_this.split('{{path}}')[1].trim()

function fencedMarks(): string[] {
  return [...card().querySelectorAll('p.se-note')]
    .map((node) => node.textContent ?? '')
    .filter((text) => text.includes(FENCED_TAIL))
}

afterEach(() => {
  cleanup()
  // Nothing the page asked for went unanswered by the shared stub. Without this a
  // product URL can drift out from under the table and this suite still passes: the
  // stub's 599 refusal reaches the surface as an ordinary error, so a test whose
  // subject is a read failure renders the copy it asserts for either way.
  expectEverySpecEngineRouteAnswered()
})

describe('what the card leads with', () => {
  it('renders the plain-language summary before the patch disclosure', async () => {
    mount({
      patch: { limits: { task_retry_limit: 5 } },
      changes: [{ path: 'limits.task_retry_limit', sentence: 'Retry limit becomes 5.' }],
    })
    const sentence = within(card()).getByText('Retry limit becomes 5.')
    // Leading content, not a caption under a payload: the summary comes first in
    // document order and the disclosure after it.
    expect(precedes(sentence, disclosure())).toBe(true)
    expect(precedes(disclosure(), confirmButton())).toBe(true)
  })

  it('starts the disclosure closed while still carrying the whole payload', async () => {
    mount({ patch: { limits: { task_retry_limit: 5 } } })
    expect(disclosure().open).toBe(false)
    // Closed is not truncated. The exact payload is present, complete, and one
    // activation away — approving a plan still means approving what is written.
    expect(shownText()).toBe(JSON.stringify({ limits: { task_retry_limit: 5 } }, null, 2))
    expect(
      within(disclosure()).getByText(F.show_the_exact_patch),
    ).toBeInTheDocument()
  })

  it('expands in place rather than over the page', async () => {
    mount({ patch: { limits: { task_retry_limit: 5 } } })
    fireEvent.click(within(disclosure()).getByText(F.show_the_exact_patch))
    // No dialog, no scrim, no overlay of any kind: the disclosure is a `<details>`
    // inside the card, so the kill-switch strip is never covered.
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(disclosure().querySelector('pre.se-gpatch')).not.toBeNull()
  })
})

describe('what a confirm submits', () => {
  it('hands back exactly the patch the disclosure showed', async () => {
    const patch = {
      quality_gates: [{ name: 'tests', commands: [['make', 'test']] }],
      capabilities: { review: { transport: 'mcp', env: { K: 'V' }, timeout_s: 0 } },
    }
    const state = mount({ patch })
    const displayed = shownText()
    fireEvent.click(confirmButton())
    expect(state.submitted).toHaveLength(1)
    // Byte-identical, not merely equivalent: the submitted value re-serialises to
    // the very characters the operator was shown. A card that computed the display
    // and the submission separately could pass a deep-equality check and still send
    // something else once one of the two grew a transformation.
    expect(JSON.stringify(state.submitted[0], null, 2)).toBe(displayed)
    expect(state.submitted[0]).toEqual(patch)
  })

  it('carries a key named on Object.prototype through as data', async () => {
    // A profile or source can legitimately be called `__proto__`. It must reach the
    // write door as an ordinary key, and the disclosure must show it. Built through
    // `JSON.parse` because an object LITERAL of the same shape would set a prototype
    // instead of creating the key — the trap `buildFormPatch` builds prototype-less
    // containers to avoid.
    const patch = JSON.parse('{"cost_profiles":{"__proto__":{"roles":{}}}}') as Document
    const state = mount({ patch })
    const displayed = shownText()
    expect(displayed).toContain('__proto__')
    fireEvent.click(confirmButton())
    expect(JSON.stringify(state.submitted[0], null, 2)).toBe(displayed)
  })

  it('submits nothing on discard', async () => {
    const state = mount({ patch: { limits: { task_retry_limit: 5 } } })
    fireEvent.click(within(card()).getByRole('button', { name: LABELS.discard }))
    expect(state.submitted).toHaveLength(0)
    expect(state.discarded).toBe(1)
  })

  it('offers no control while a write is in flight', async () => {
    mount({ patch: {}, writing: true })
    expect(within(card()).getByRole('button', { name: LABELS.writing })).toBeDisabled()
    expect(within(card()).getByRole('button', { name: LABELS.discard })).toBeDisabled()
  })
})

describe('the four authority changes, each stated before the confirm', () => {
  const cases = [
    { kind: 'authority' as const, text: F.raises_an_untrusted_class_authority },
    { kind: 'gate_removed' as const, text: F.removes_a_gate_from_the_flow },
    { kind: 'external_program' as const, text: F.binds_a_capability_to_an_external_program },
    { kind: 'commands_run' as const, text: F.authorises_commands_to_run },
  ]

  for (const { kind, text } of cases) {
    it(`states what ${kind} means, above the confirm control`, async () => {
      mount({ patch: { quality_gates: [] }, authorises: [{ kind, path: 'quality_gates' }] })
      const stated = within(card()).getByText(text)
      expect(precedes(stated, confirmButton())).toBe(true)
      // Before the disclosure too: the consequence is the reason to read the patch,
      // not a footnote to it.
      expect(precedes(stated, disclosure())).toBe(true)
    })
  }

  it('states every declared kind when one change makes several grants', async () => {
    mount({
      patch: { workflow: { stages: { submit: [['git', 'push']] } } },
      authorises: [
        { kind: 'commands_run', path: 'workflow.stages.submit' },
        { kind: 'external_program', path: 'workflow.stages.submit' },
      ],
    })
    const program = within(card()).getByText(F.binds_a_capability_to_an_external_program)
    const commands = within(card()).getByText(F.authorises_commands_to_run)
    // The card's order, not the caller's: the two were declared the other way round,
    // and two forms granting the same pair must read in the same sequence.
    expect(precedes(program, commands)).toBe(true)
  })

  it('keeps the caller subject sentence above the card own statement', async () => {
    // The division of labour: the caller names the subject it alone knows, the card
    // says what the grant means, and the two are not merged into one paragraph.
    mount({
      patch: { sources: { gh: { autonomy: { external: { quick: 'delivery' } } } } },
      authorises: [
        {
          kind: 'authority',
          path: 'sources.gh.autonomy.external.quick',
          sentence: 'This raises external from authoring to delivery.',
        },
      ],
    })
    const subject = within(card()).getByText('This raises external from authoring to delivery.')
    const meaning = within(card()).getByText(F.raises_an_untrusted_class_authority)
    expect(precedes(subject, meaning)).toBe(true)
  })

  it('states nothing when a patch grants no authority', async () => {
    mount({ patch: { limits: { task_retry_limit: 5 } } })
    for (const { text } of cases) {
      expect(within(card()).queryByText(text)).toBeNull()
    }
    expect(card().querySelector('.se-arm')).toBeNull()
  })
})

describe('the paths only an operator can write', () => {
  it('marks each fenced path the patch touches, naming the engine own list', async () => {
    const state = mount(
      { patch: { quality_gates: [], limits: { task_retry_limit: 5 } } },
      { configOnly: ['capabilities', 'workflow', 'projects.*.workflow', 'quality_gates'] },
    )
    await state.settled('success')
    // Only what the patch actually writes: a fenced path the patch does not touch
    // earns no line, or every card would carry all five.
    expect(fencedMarks()).toEqual([
      F.only_an_operator_confirmation_writes_this.replace('{{path}}', 'quality_gates'),
    ])
    expect(state.submitted).toHaveLength(0)
  })

  it('resolves a wildcard segment to the project key the patch names', async () => {
    const state = mount(
      { patch: { projects: { '/src/a.b': { workflow: { preset: 'local-only' } } } } },
      { configOnly: ['projects.*.workflow'] },
    )
    await state.settled('success')
    // The project key holds a dot and a slash. A dotted rendering could not be split
    // back into segments, which is why the match walks the patch's own keys.
    expect(fencedMarks()).toEqual([
      F.only_an_operator_confirmation_writes_this.replace(
        '{{path}}',
        'projects./src/a.b.workflow',
      ),
    ])
  })

  it('marks nothing when the relay lists nothing', async () => {
    const state = mount({ patch: { quality_gates: [] } }, { configOnly: [] })
    await state.settled('success')
    expect(fencedMarks()).toEqual([])
  })

  it('marks nothing when the read that carries the list failed', async () => {
    // The pane's rule: a failed read states nothing rather than asserting a policy
    // this side made up. An unmarked fenced path loses one explanatory line; a
    // marked unfenced one claims a refusal that does not exist.
    const state = mount({ patch: { quality_gates: [] } }, { configFails: true })
    await state.settled('error')
    expect(fencedMarks()).toEqual([])
  })

  it('stops marking when a later read of the list fails', async () => {
    // The React Query trap this branch has been bitten by twice: a failed refetch
    // keeps the last successful body, so a card reading `data` alone would keep
    // asserting a fence on the strength of a read that did not happen.
    const state = mount(
      { patch: { quality_gates: [] } },
      { config: [{ body: snapshot(['quality_gates']) }, failure(503, 'config_unreadable')] },
    )
    await state.settled('success')
    expect(fencedMarks()).toHaveLength(1)
    await state.reread()
    await state.settled('error')
    expect(fencedMarks()).toEqual([])
  })
})

describe('fencedPatchPaths, on its own', () => {
  it('matches an exact section and a path under it', () => {
    expect(fencedPatchPaths({ quality_gates: [] }, ['quality_gates'])).toEqual(['quality_gates'])
    expect(
      fencedPatchPaths({ capabilities: { review: { transport: 'mcp' } } }, ['capabilities']),
    ).toEqual(['capabilities'])
  })

  it('expands a wildcard over the keys actually present', () => {
    const patch = {
      projects: {
        '/src/one': { workflow: { preset: 'a' } },
        '/src/two': { path: '/src/two' },
      },
    }
    // Walked over object keys rather than a dotted string, which is why a project
    // key holding a dot or a slash still matches.
    expect(fencedPatchPaths(patch, ['projects.*.workflow'])).toEqual(['projects./src/one.workflow'])
  })

  it('answers nothing for a patch that touches no fenced path', () => {
    expect(fencedPatchPaths({ limits: { task_retry_limit: 5 } }, ['capabilities', 'workflow'])).toEqual(
      [],
    )
  })

  it('names a path once even when two patterns reach it', () => {
    expect(fencedPatchPaths({ workflow: { preset: 'a' } }, ['workflow', 'workflow'])).toEqual([
      'workflow',
    ])
  })

  it('does not count a name inherited from Object.prototype as a fenced write', () => {
    // `{}.constructor` exists on every plain object. A membership test written with
    // `in` would report the patch as writing a fenced section it never mentions.
    expect(fencedPatchPaths({}, ['constructor'])).toEqual([])
  })
})
