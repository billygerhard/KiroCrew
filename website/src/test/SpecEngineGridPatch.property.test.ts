/**
 * The grid patch: a change to one cell touches nothing but that cell.
 *
 * This is the isolation guarantee Requirement 4 asks for, and it is a property of the
 * PATCH rather than of the care taken where the patch is built. `PUT /config` merges
 * recursively — nested objects merge key by key — so a document is left byte-identical
 * everywhere the patch has no leaf. Which means the whole question reduces to one
 * checkable claim: are the patch's only leaves the cells the operator edited?
 *
 * A hand-written builder gets this wrong in three ways, and each is generated here:
 *
 *   - **A second edit inside one source replaces the first.** Assigning a fresh
 *     object per edit drops every earlier cell in the same source, class or type —
 *     and the review card would still show the dropped one if the patch were built
 *     twice.
 *   - **A key that is also a name on `Object.prototype`.** `patch[name] = {}` for a
 *     source called `__proto__` sets a PROTOTYPE instead of creating a key; the patch
 *     then serialises without that edit and the write silently carries less than the
 *     review displayed.
 *   - **A leaf at the wrong depth.** A cell lives five segments down
 *     (`sources.<name>.autonomy.<class>.<type>`); a patch that wrote the class row
 *     whole, or the source's `autonomy` node whole, would REPLACE sibling cells the
 *     operator never looked at, which is exactly the cross-contamination the
 *     property forbids.
 *
 * The Python half of the same property — that merging this patch through the engine's
 * real `_merge` leaves every other path identical — lives in the spec_engine test
 * tree. Together they cover the claim end to end; neither covers it alone.
 */
import { describe, it, expect } from 'vitest'
import * as fc from 'fast-check'

import {
  AUTONOMY_KEY,
  SOURCES,
  buildGridPatch,
  isObject,
  type PendingEdit,
} from '../apps/spec-engine/configDocument'

/**
 * Names that are legal document keys and hostile to a naive builder.
 *
 * `__proto__` and `constructor` are inherited names on a plain object; `default` is
 * the engine's own wildcard key, and a builder that treated it specially would write
 * a broader rule where a specific cell was asked for; `a.b` holds the separator a
 * dotted path is rendered with, so a builder that ever round-tripped through a string
 * would split it into two segments.
 */
const NAME = fc.constantFrom(
  'gh',
  'gh.issues',
  'default',
  'autonomy',
  'sources',
  '__proto__',
  'constructor',
  'a.b',
)

const LEVEL = fc.constantFrom('authoring', 'execution', 'delivery', 'integration')

const EDIT: fc.Arbitrary<PendingEdit> = fc.record({
  source: NAME,
  klass: NAME,
  specType: NAME,
  level: LEVEL,
})

/** Every leaf in *node*, as `[segments, value]` pairs. */
function leaves(node: unknown, segments: readonly string[] = []): Array<[string[], unknown]> {
  if (!isObject(node)) return [[[...segments], node]]
  const found: Array<[string[], unknown]> = []
  // `Object.keys` rather than `for…in`, so an inherited name can never be counted as
  // a leaf the patch carries: the point of the walk is what a JSON serialisation of
  // the patch would contain.
  for (const key of Object.keys(node)) found.push(...leaves(node[key], [...segments, key]))
  return found
}

/** The cell a pending edit addresses, as the key a set can hold. */
const address = (edit: PendingEdit): string =>
  JSON.stringify([SOURCES, edit.source, AUTONOMY_KEY, edit.klass, edit.specType])

describe('Property 1 (TS half): a grid patch touches only its own cells', () => {
  it('has exactly one leaf per edited cell, at that cell, holding the chosen level', () => {
    fc.assert(
      fc.property(fc.array(EDIT, { minLength: 1, maxLength: 8 }), (edits) => {
        const patch = buildGridPatch(edits)
        const found = leaves(patch)

        // Every leaf is a cell that was edited, addressed at its own five segments.
        // A leaf anywhere else is a path the merge would overwrite in a document
        // nobody asked it to touch.
        const wanted = new Set(edits.map(address))
        for (const [segments] of found) {
          expect(wanted.has(JSON.stringify(segments))).toBe(true)
        }

        // And every edited cell has exactly one leaf, holding the level chosen LAST
        // for it: an operator's final choice is the one the review card reads out,
        // so it has to be the one the patch carries.
        const last = new Map<string, string>()
        for (const edit of edits) last.set(address(edit), edit.level)
        expect(found).toHaveLength(last.size)
        for (const [segments, value] of found) {
          expect(value).toBe(last.get(JSON.stringify(segments)))
        }
      }),
      { numRuns: 300 },
    )
  })

  it('serialises every edited cell, whatever the names are called', () => {
    // The `__proto__` case does not survive a structural walk alone: an assignment
    // that set a prototype would leave the walk finding a leaf through the
    // prototype chain in some implementations. JSON is what actually crosses the
    // wire, so JSON is what has to carry the edit.
    fc.assert(
      fc.property(fc.array(EDIT, { minLength: 1, maxLength: 6 }), (edits) => {
        const round = JSON.parse(JSON.stringify(buildGridPatch(edits))) as unknown
        for (const [segments, value] of leaves(round)) {
          expect(segments).toHaveLength(5)
          expect(typeof value).toBe('string')
        }
        expect(leaves(round)).toHaveLength(new Set(edits.map(address)).size)
      }),
      { numRuns: 200 },
    )
  })
})

describe('the patch a set of choices builds', () => {
  it('is empty when there is nothing pending', () => {
    // An empty patch still WRITES: the store records every write, so a surface that
    // could send this would put a line in the durable record for a change nobody
    // made. The builder returning nothing is what lets the caller refuse to send.
    expect(Object.keys(buildGridPatch([]))).toHaveLength(0)
  })

  it('nests exactly the five segments of one cell', () => {
    expect(
      JSON.parse(
        JSON.stringify(
          buildGridPatch([
            { source: 'gh', klass: 'external', specType: 'feature', level: 'execution' },
          ]),
        ),
      ),
    ).toEqual({ sources: { gh: { autonomy: { external: { feature: 'execution' } } } } })
  })

  it('keeps both cells when two edits share a source', () => {
    // The regression a fresh object per edit produces. Two choices in one source are
    // the ordinary case — an operator lowers `external` across two spec types — and
    // losing the first would write half of an approved patch.
    expect(
      JSON.parse(
        JSON.stringify(
          buildGridPatch([
            { source: 'gh', klass: 'external', specType: 'feature', level: 'authoring' },
            { source: 'gh', klass: 'external', specType: 'bugfix', level: 'execution' },
            { source: 'gh', klass: 'member', specType: 'quick', level: 'delivery' },
          ]),
        ),
      ),
    ).toEqual({
      sources: {
        gh: {
          autonomy: {
            external: { feature: 'authoring', bugfix: 'execution' },
            member: { quick: 'delivery' },
          },
        },
      },
    })
  })

  it('never writes the wildcard key of a pair it was not asked for', () => {
    // The narrowing rule, as a property of the patch: an edit on a pair a broader
    // rule answered writes the PAIR's cell. The engine's wildcard key is the literal
    // `default`, and a patch that touched it would change cells nobody was looking at.
    const patch = JSON.parse(
      JSON.stringify(
        buildGridPatch([
          { source: 'gh', klass: 'contributor', specType: 'bugfix', level: 'authoring' },
        ]),
      ),
    ) as Record<string, Record<string, Record<string, Record<string, unknown>>>>
    expect(Object.keys(patch.sources.gh.autonomy)).toEqual(['contributor'])
    expect(Object.keys(patch.sources.gh.autonomy.contributor)).toEqual(['bugfix'])
  })

  it('carries a source named like an inherited property', () => {
    // Not a hypothetical: JSON parses `__proto__` into an ordinary key on both
    // sides, so a document can hold it, and the naive assignment would lose the
    // edit without a word.
    const patch = JSON.parse(
      JSON.stringify(
        buildGridPatch([
          { source: '__proto__', klass: 'external', specType: 'quick', level: 'authoring' },
        ]),
      ),
    ) as { sources: Record<string, unknown> }
    expect(Object.keys(patch.sources)).toEqual(['__proto__'])
  })
})
