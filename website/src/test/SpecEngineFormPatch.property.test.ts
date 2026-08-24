/**
 * The form patch: a write touches only the values the operator staged.
 *
 * This is the isolation guarantee Requirement 5 asks of every form on the
 * configuration pane, and it is a property of the PATCH rather than of the care
 * taken where the patch is built. `PUT /config` merges recursively — nested objects
 * merge key by key, and `null` deletes — so a document is left byte-identical
 * everywhere the patch has no leaf. Which reduces the whole question to one
 * checkable claim: is every leaf the patch carries at or under a path the operator
 * staged, and does every staged path carry exactly what was staged for it?
 *
 * A hand-written builder gets this wrong in four ways, and each is generated here:
 *
 *   - **A second edit under one section replaces the first.** Assigning a fresh
 *     object per edit drops every earlier value sharing a prefix — and the review
 *     card would still show the dropped one, because the card renders the staged
 *     list rather than the patch.
 *   - **A key that is also a name on `Object.prototype`.** `patch[name] = {}` for a
 *     profile or source called `__proto__` sets a PROTOTYPE instead of creating a
 *     key; the patch then serialises without that edit and the write silently
 *     carries less than the review displayed.
 *   - **A leaf at the wrong depth.** A patch that wrote an ancestor of the staged
 *     path whole would REPLACE sibling values the operator never looked at, which is
 *     exactly the cross-contamination the property forbids.
 *   - **A deletion spelled as an omission.** The merge KEEPS what a patch omits, so
 *     a removal has to arrive as an explicit `null`. `DELETE` is the only thing that
 *     produces one.
 *
 * `DELETE` is a symbol rather than `null` because a staged `null` and a staged
 * removal are indistinguishable at the write door — the merge deletes either way.
 * Making the removal a value no JSON document can hold is what keeps a form from
 * asking for one and getting the other, so `null` is deliberately absent from the
 * generated values below: the only `null` a patch may carry is a deletion's.
 *
 * The Python half of the same property — that merging this patch through the
 * engine's real `_merge` leaves every other path identical, in the deletion form as
 * well as the assignment form — lives in the spec_engine test tree. Together they
 * cover the claim end to end; neither covers it alone.
 */
import { describe, it, expect } from 'vitest'
import * as fc from 'fast-check'

import {
  DELETE,
  buildFormPatch,
  buildGridPatch,
  isObject,
  nodeAt,
  type StagedEdit,
} from '../apps/spec-engine/configDocument'

/**
 * Names that are legal document keys and hostile to a naive builder.
 *
 * `__proto__` and `constructor` are inherited names on a plain object; `default` is
 * the engine's own wildcard key inside an autonomy grid; `a.b` holds the separator a
 * dotted path is rendered with, so a builder that ever round-tripped through a
 * string would split it into two segments.
 */
const NAME = fc.constantFrom(
  'limits',
  'task_retry_limit',
  'cost_profiles',
  'thrifty.roles',
  'default',
  'sources',
  '__proto__',
  'constructor',
  'a.b',
)

/**
 * Values a form stages, and the removal sentinel beside them.
 *
 * Objects are included because a form legitimately stages a whole subtree — a watch
 * source composed from a preset is one value — so the property has to hold when a
 * staged value has leaves of its own. `null` is excluded: see the module note.
 */
const VALUE = fc.oneof(
  fc.integer({ min: -100, max: 100 }),
  fc.double({ min: -1, max: 1, noNaN: true }),
  fc.boolean(),
  fc.string({ maxLength: 8 }),
  fc.array(fc.string({ maxLength: 4 }), { maxLength: 3 }),
  fc.array(fc.tuple(NAME, fc.integer({ min: 0, max: 9 })), { maxLength: 3 }).map((pairs) =>
    Object.fromEntries(pairs),
  ),
  fc.constant(DELETE),
)

const PATH = fc.array(NAME, { minLength: 1, maxLength: 4 })

/** Whether *path* lies at or inside *other*, compared segment for segment. */
function within(path: readonly string[], other: readonly string[]): boolean {
  return path.length >= other.length && other.every((segment, index) => path[index] === segment)
}

/**
 * *edits* with every path that overlaps an earlier one dropped.
 *
 * A form stages LEAVES, so its paths are pairwise non-overlapping — one edit's path
 * never lies inside another's — and that is the contract `buildFormPatch` documents.
 * Two edits at the SAME path are kept: they are an operator changing their mind, and
 * the last one is what the review reads out.
 */
function nonOverlapping(edits: readonly StagedEdit[]): StagedEdit[] {
  const kept: StagedEdit[] = []
  for (const edit of edits) {
    const clashes = kept.some(
      (other) =>
        other.segments.length !== edit.segments.length &&
        (within(edit.segments, other.segments) || within(other.segments, edit.segments)),
    )
    if (!clashes) kept.push(edit)
  }
  return kept
}

const EDITS: fc.Arbitrary<StagedEdit[]> = fc
  .array(fc.record({ segments: PATH, value: VALUE }), { minLength: 1, maxLength: 8 })
  .map(nonOverlapping)

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

/** The value a staged edit asks the store to hold, as the patch spells it. */
function stored(edit: StagedEdit): unknown {
  return edit.value === DELETE ? null : edit.value
}

/** The last edit staged at each distinct path, which is the one that survives. */
function surviving(edits: readonly StagedEdit[]): Map<string, StagedEdit> {
  const last = new Map<string, StagedEdit>()
  for (const edit of edits) last.set(JSON.stringify(edit.segments), edit)
  return last
}

describe('Property 1 (TS half): a form patch touches only its staged paths', () => {
  it('carries each staged path exactly, and no leaf outside one', () => {
    fc.assert(
      fc.property(EDITS, (edits) => {
        const patch = buildFormPatch(edits)
        const last = surviving(edits)

        // Every staged path holds what was staged for it, and a removal holds the
        // `null` the store's merge reads as a deletion.
        for (const edit of last.values()) {
          expect(JSON.stringify(nodeAt(patch, edit.segments) ?? null)).toBe(
            JSON.stringify(stored(edit) ?? null),
          )
        }

        // And every leaf lies at or under one staged path. A leaf anywhere else is a
        // path the merge would overwrite in a document nobody asked it to touch.
        const paths = [...last.values()].map((edit) => edit.segments)
        for (const [segments] of leaves(patch)) {
          expect(paths.some((path) => within(segments, path))).toBe(true)
        }
      }),
      { numRuns: 400 },
    )
  })

  it('serialises every staged path, whatever the names are called', () => {
    // The `__proto__` case does not survive a structural walk alone: an assignment
    // that set a prototype would leave the walk finding a leaf through the prototype
    // chain in some implementations. JSON is what actually crosses the wire, so JSON
    // is what has to carry the edit.
    fc.assert(
      fc.property(EDITS, (edits) => {
        const round = JSON.parse(JSON.stringify(buildFormPatch(edits))) as unknown
        for (const edit of surviving(edits).values()) {
          expect(JSON.stringify(nodeAt(round, edit.segments) ?? null)).toBe(
            JSON.stringify(stored(edit) ?? null),
          )
        }
      }),
      { numRuns: 300 },
    )
  })

  it('spells a removal as null and never emits one otherwise', () => {
    fc.assert(
      fc.property(EDITS, (edits) => {
        const round = JSON.parse(JSON.stringify(buildFormPatch(edits))) as unknown
        const removals = new Set(
          [...surviving(edits).values()]
            .filter((edit) => edit.value === DELETE)
            .map((edit) => JSON.stringify(edit.segments)),
        )
        for (const [segments, value] of leaves(round)) {
          if (value === null) expect(removals.has(JSON.stringify(segments))).toBe(true)
        }
        // The other direction: a staged removal is never omitted, which is the
        // failure the merge turns into a change that silently did not happen.
        for (const key of removals) {
          expect(nodeAt(round, JSON.parse(key) as string[])).toBeNull()
        }
      }),
      { numRuns: 300 },
    )
  })
})

describe('the patch a set of staged edits builds', () => {
  it('is empty when there is nothing staged', () => {
    // An empty patch still WRITES: the store records every write, so a surface that
    // could send this would put a line in the durable record for a change nobody
    // made. The builder returning nothing is what lets the caller refuse to send.
    expect(Object.keys(buildFormPatch([]))).toHaveLength(0)
  })

  it('nests one object per segment of one path', () => {
    expect(
      JSON.parse(
        JSON.stringify(
          buildFormPatch([{ segments: ['limits', 'task_retry_limit'], value: 5 }]),
        ),
      ),
    ).toEqual({ limits: { task_retry_limit: 5 } })
  })

  it('keeps both values when two edits share a prefix', () => {
    // The regression a fresh object per edit produces. Two fields of one section are
    // the ordinary case on a form, and losing the first would write half of an
    // approved patch.
    expect(
      JSON.parse(
        JSON.stringify(
          buildFormPatch([
            { segments: ['limits', 'task_retry_limit'], value: 3 },
            { segments: ['limits', 'verify_retry_limit'], value: 4 },
            { segments: ['budget', 'warn_fraction'], value: 0.5 },
          ]),
        ),
      ),
    ).toEqual({
      limits: { task_retry_limit: 3, verify_retry_limit: 4 },
      budget: { warn_fraction: 0.5 },
    })
  })

  it('spells a removal as the null the store deletes on', () => {
    expect(
      JSON.parse(
        JSON.stringify(buildFormPatch([{ segments: ['sources', 'gh'], value: DELETE }])),
      ),
    ).toEqual({ sources: { gh: null } })
  })

  it('removes one entry without touching the section around it', () => {
    expect(
      JSON.parse(
        JSON.stringify(
          buildFormPatch([
            { segments: ['sources', 'gh'], value: DELETE },
            { segments: ['sources', 'forgejo', 'enabled'], value: true },
          ]),
        ),
      ),
    ).toEqual({ sources: { gh: null, forgejo: { enabled: true } } })
  })

  it('lets a later edit at one path win over an earlier one', () => {
    expect(
      JSON.parse(
        JSON.stringify(
          buildFormPatch([
            { segments: ['limits', 'task_retry_limit'], value: 3 },
            { segments: ['limits', 'task_retry_limit'], value: 9 },
          ]),
        ),
      ),
    ).toEqual({ limits: { task_retry_limit: 9 } })
  })

  it('carries a name that is also an inherited property', () => {
    // Not a hypothetical: JSON parses `__proto__` into an ordinary key on both
    // sides, so a document can hold it, and the naive assignment would lose the edit
    // without a word.
    const patch = JSON.parse(
      JSON.stringify(
        buildFormPatch([{ segments: ['sources', '__proto__', 'enabled'], value: false }]),
      ),
    ) as { sources: Record<string, unknown> }
    expect(Object.keys(patch.sources)).toEqual(['__proto__'])
    expect(patch.sources.__proto__).toEqual({ enabled: false })
  })

  it('never mutates a value the caller staged', () => {
    // The builder descends only into containers it made itself. Descending into a
    // staged object would edit the operator's own value while building a patch of
    // it, and the review card reads that same object.
    const entry = { enabled: false }
    buildFormPatch([
      { segments: ['sources', 'gh'], value: entry },
      { segments: ['sources', 'gh', 'enabled'], value: true },
    ])
    expect(entry).toEqual({ enabled: false })
  })

  it('refuses a staged edit that addresses nothing', () => {
    // A patch with no segments would be the whole document, which is the one write
    // no form ever means to make.
    expect(() => buildFormPatch([{ segments: [], value: 1 }])).toThrow()
  })
})

describe('the grid patch is the form patch', () => {
  it('builds a cell edit through the shared builder', () => {
    // One mechanism, not two: the grid's isolation and a form's are the same
    // property, so a divergence here would be two proofs of one claim.
    expect(
      JSON.parse(
        JSON.stringify(
          buildGridPatch([
            { source: 'gh', klass: 'external', specType: 'feature', level: 'execution' },
          ]),
        ),
      ),
    ).toEqual(
      JSON.parse(
        JSON.stringify(
          buildFormPatch([
            {
              segments: ['sources', 'gh', 'autonomy', 'external', 'feature'],
              value: 'execution',
            },
          ]),
        ),
      ),
    )
  })
})
