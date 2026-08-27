/**
 * The confirmation card's load-bearing claim, over generated edit sets: what is
 * submitted is byte-identical to what the disclosure showed.
 *
 * A plain-language summary that drifts from the submitted patch is worse than no
 * summary at all — it authorises something the operator did not read — so this is
 * not a claim to check on the two payloads someone happened to write a test for.
 * The generator is the pane's own staging vocabulary: the same names, values and
 * deletion sentinel `SpecEngineFormPatch.property.test.ts` generates, run through
 * the same `buildFormPatch` every form composes with, so the patches under test are
 * patches the forms can actually produce — including the hostile keys (`__proto__`,
 * `constructor`, a name holding a dot) and the explicit `null` a removal spells.
 *
 * The property is stated on the RENDERED CHARACTERS rather than on a deep equality,
 * because deep equality is exactly what a drifting card would still satisfy: a
 * display that gained a redaction, an ordering, or a "hide the elided values" pass
 * would keep every value and stop matching what the write door receives. Comparing
 * the submitted value's own serialisation against the text on screen is the only
 * form of the claim that fails in that case.
 */
import { describe, it, expect, afterEach } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import * as fc from 'fast-check'

import { FormReview } from '../apps/spec-engine/ConfigPanel'
import {
  DELETE,
  buildFormPatch,
  type Document,
  type StagedEdit,
} from '../apps/spec-engine/configDocument'
import { stubSpecEngineFetch } from './specEngineFetchStub'

/**
 * Document keys that are legal and hostile to a naive builder or renderer.
 *
 * The same set the patch property generates, for the same reasons: two are
 * inherited names on a plain object, `default` is the engine's own wildcard inside
 * an autonomy grid, and `a.b` holds the separator a dotted path is rendered with.
 */
const NAME = fc.constantFrom(
  'limits',
  'task_retry_limit',
  'capabilities',
  'quality_gates',
  'workflow',
  'default',
  '__proto__',
  'constructor',
  'a.b',
)

/** Values a form stages, with the removal sentinel that becomes an explicit null. */
const VALUE = fc.oneof(
  fc.integer({ min: -100, max: 100 }),
  fc.double({ min: -1, max: 1, noNaN: true }),
  fc.boolean(),
  fc.string({ maxLength: 8 }),
  fc.array(fc.string({ maxLength: 4 }), { maxLength: 3 }),
  fc.constant(DELETE),
)

const EDITS: fc.Arbitrary<StagedEdit[]> = fc.array(
  fc.record({ segments: fc.array(NAME, { minLength: 1, maxLength: 3 }), value: VALUE }),
  { minLength: 1, maxLength: 6 },
)

const LABELS = {
  heading: 'The change that would be written',
  confirm: 'Write the change',
  writing: 'Saving…',
  discard: 'Discard',
  exactly: 'A confirm writes exactly this patch.',
  refusalTitle: 'Could not write',
  retained: 'Nothing was written.',
}

const SNAPSHOT = {
  configured: true,
  path: '/home/me/.kiro/crew/apps/spec-engine/config.json',
  document: { projects: {} },
  elided: [],
  elided_marker: '<elided>',
  errors: [],
  advisories: [],
  config_only_paths: ['capabilities', 'workflow', 'projects.*.workflow', 'quality_gates'],
}

/** Mount the card on *patch*, confirm it, and report what each side held. */
function confirmOnce(patch: Document): { displayed: string; submitted: Document[] } {
  stubSpecEngineFetch({ config: { body: SNAPSHOT } })
  const submitted: Document[] = []
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchInterval: false } },
  })
  render(
    <QueryClientProvider client={client}>
      <FormReview
        changes={[]}
        patch={patch}
        labels={LABELS}
        writing={false}
        error={null}
        onConfirm={(sending) => submitted.push(sending)}
        onDiscard={() => {}}
      />
    </QueryClientProvider>,
  )
  const pre = document.querySelector('details.se-disc pre.se-gpatch')
  if (!pre) throw new Error('the patch disclosure is not on screen')
  const displayed = pre.textContent ?? ''
  fireEvent.click(screen.getByRole('button', { name: LABELS.confirm }))
  return { displayed, submitted }
}

afterEach(() => {
  cleanup()
})

describe('what the disclosure shows is what the confirm submits', () => {
  it('re-serialises the submitted patch to the exact characters displayed', () => {
    fc.assert(
      fc.property(EDITS, (edits) => {
        const patch = buildFormPatch(edits)
        const { displayed, submitted } = confirmOnce(patch)
        expect(submitted).toHaveLength(1)
        expect(JSON.stringify(submitted[0], null, 2)).toBe(displayed)
        cleanup()
      }),
      { numRuns: 60 },
    )
  })

  it('submits a value for every path the disclosure displayed, and no other', () => {
    fc.assert(
      fc.property(EDITS, (edits) => {
        const patch = buildFormPatch(edits)
        const { displayed, submitted } = confirmOnce(patch)
        // Parsed from the text on screen, so this side of the comparison is the
        // operator's reading of the change rather than a second derivation of it.
        expect(submitted[0]).toEqual(JSON.parse(displayed))
        cleanup()
      }),
      { numRuns: 60 },
    )
  })
})
