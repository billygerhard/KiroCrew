/**
 * Property 3, frontend half: a composed source carries only preset commands.
 *
 * The write door validates an argv's SHAPE and not which program it names, so the
 * boundary on what the engine can be made to run through configuration is the
 * bundled preset tables plus this form's refusal to compose an argv path. That makes
 * two claims, and neither is a claim about the two presets shipped today:
 *
 *   - **`composeSource` copies the command.** For any preset entry, the staged
 *     entry's `poll` is byte-equal to the preset's own, its `field_map` likewise, and
 *     `enabled` is ABSENT rather than false — polling is what arms an unattended run,
 *     so a fresh copy must be inert. The copy is also deep: the read's cached object
 *     must never become the staged value, or an edit to one source's staged copy
 *     would change what the next copy is offered.
 *   - **No sequence of form actions stages an argv the presets did not supply.**
 *     `sourceEdit` is the one place this form composes a path under `sources`, so the
 *     property is stated over ARBITRARY action sequences — including a field named
 *     `poll`, a registry key whose group is `field_map`, and a settings group the
 *     schema keeps disjoint today but might not tomorrow — and checked against the
 *     patch those actions actually build.
 *
 * A generator is the only way to state either. A hard-coded case proves the two
 * shipped presets copy correctly; it cannot prove that the set of paths this form can
 * write excludes the two the engine executes.
 */
import { describe, it, expect } from 'vitest'
import * as fc from 'fast-check'

import {
  SOURCE_FORM_FIELDS,
  composeSource,
  sourceEdit,
  sourceShape,
  type SourceFormAction,
} from '../apps/spec-engine/ConfigPanel'
import { DELETE, buildFormPatch, type StagedEdit } from '../apps/spec-engine/configDocument'
import { stageEdit } from '../apps/spec-engine/useStagedEdits'
import type { RegistrySetting, SourcePreset } from '../apps/spec-engine/api'

/** The two argv-bearing fields, which are the subject of the whole property. */
const ARGV_FIELDS = ['poll', 'field_map'] as const

/** An argv: a non-empty list of strings, as the engine's own preset tables hold. */
const ARGV = fc.array(fc.string({ minLength: 1, maxLength: 8 }), { minLength: 1, maxLength: 5 })

/** A field map: engine item field to a dotted output path. */
const FIELD_MAP = fc.dictionary(
  fc.constantFrom('identifier', 'title', 'body', 'state', 'address', 'submitter'),
  fc.string({ minLength: 1, maxLength: 8 }),
  { maxKeys: 4 },
)

/**
 * A preset as the registry projects one, including shapes the bundled table does
 * not have today: an `enabled` the payload should never carry, and a host whose name
 * is a prototype key.
 */
const PRESET: fc.Arbitrary<SourcePreset> = fc
  .record({
    host: fc.constantFrom('github', 'gitlab', 'forgejo', '__proto__', ''),
    program: fc.constantFrom('gh', 'glab', 'fj'),
    poll: ARGV,
    fieldMap: FIELD_MAP,
    enabled: fc.option(fc.boolean(), { nil: undefined }),
  })
  .map(({ host, program, poll, fieldMap, enabled }) => {
    const entry: Record<string, unknown> = {
      preset: host,
      public: true,
      poll,
      field_map: fieldMap,
    }
    if (enabled !== undefined) entry.enabled = enabled
    return { host, program, entry }
  })

/** A source name, including the two names a container must never be treated as. */
const NAME = fc.constantFrom('gh', 'issues', 'a.b', '__proto__', 'constructor', ' ', '')

/**
 * A field an action can name: the three the form writes, the two the engine
 * executes, and one nothing knows.
 */
const FIELD = fc.constantFrom(...SOURCE_FORM_FIELDS, ...ARGV_FIELDS, 'autonomy', 'preset')

/** A registry key, including groups that would land on an argv field. */
const SETTING_KEY = fc.constantFrom(
  'watch.interval_s',
  'timeouts.poll_command_s',
  'poll.0',
  'field_map.title',
  'nogroup',
  'a.b.c',
)

const ACTION = (presets: readonly SourcePreset[]): fc.Arbitrary<SourceFormAction> =>
  fc.oneof(
    fc.record({
      kind: fc.constant('add' as const),
      source: NAME,
      preset: fc.constantFrom(...presets),
    }),
    fc.record({
      kind: fc.constant('field' as const),
      source: NAME,
      field: FIELD,
      value: fc.oneof(fc.boolean(), fc.string({ maxLength: 6 }), ARGV),
    }),
    fc.record({
      kind: fc.constant('setting' as const),
      source: NAME,
      key: SETTING_KEY,
      value: fc.oneof(fc.integer(), ARGV),
    }),
    fc.record({ kind: fc.constant('remove' as const), source: NAME }),
  )

/** Every `poll` and `field_map` a patch carries, at any depth, with its value. */
function argvEntries(node: unknown, found: Array<[string, unknown]> = []): Array<[string, unknown]> {
  if (node !== null && typeof node === 'object' && !Array.isArray(node)) {
    for (const [key, value] of Object.entries(node)) {
      if ((ARGV_FIELDS as readonly string[]).includes(key)) found.push([key, value])
      argvEntries(value, found)
    }
  }
  return found
}

describe('composeSource copies the preset\u2019s command and leaves it inert', () => {
  it('carries poll and field_map byte-for-byte', () => {
    fc.assert(
      fc.property(PRESET, (preset) => {
        const composed = composeSource(preset)
        expect(JSON.stringify(composed.poll)).toBe(JSON.stringify(preset.entry.poll))
        expect(JSON.stringify(composed.field_map)).toBe(JSON.stringify(preset.entry.field_map))
      }),
    )
  })

  it('leaves enabled absent, whatever the payload carried', () => {
    fc.assert(
      fc.property(PRESET, (preset) => {
        const composed = composeSource(preset)
        // Absent, not false: the engine polls neither, but a `false` written into the
        // document is a claim the operator did not make, and the preset contract is
        // that a fresh copy carries no such key at all.
        expect(Object.prototype.hasOwnProperty.call(composed, 'enabled')).toBe(false)
      }),
    )
  })

  it('copies deeply, so a staged entry cannot reach back into the read', () => {
    fc.assert(
      fc.property(PRESET, (preset) => {
        const before = JSON.stringify(preset.entry)
        const composed = composeSource(preset)
        ;(composed.poll as string[]).push('--evil')
        delete composed.field_map
        // The registry answer is shared with every other surface reading it, so a
        // mutation of one staged copy must not change what the next copy is offered.
        expect(JSON.stringify(preset.entry)).toBe(before)
      }),
    )
  })
})

describe('no sequence of form actions stages an argv the presets did not supply', () => {
  it('builds a patch whose every argv path is a preset\u2019s own', () => {
    fc.assert(
      fc.property(
        fc.array(PRESET, { minLength: 1, maxLength: 3 }),
        fc.array(fc.nat(6), { maxLength: 6 }),
        (presets, seeds) => {
          // The actions are drawn against the presets that exist, exactly as the form
          // draws them: a copy is always a copy of something the read supplied.
          const actions = seeds.map((seed) =>
            fc.sample(ACTION(presets), { numRuns: 1, seed })[0],
          )
          let edits: readonly StagedEdit[] = []
          for (const action of actions) {
            const edit = sourceEdit(action)
            // `null` is the refusal, and the form drops it rather than guessing a
            // path — a field outside the three, or a setting group that would land on
            // an argv field.
            if (edit) edits = stageEdit(edits, edit.segments, edit.value)
          }
          const patch = buildFormPatch(edits.filter((edit) => edit.value !== DELETE))
          for (const [, value] of argvEntries(patch)) {
            const supplied = presets.some((preset) =>
              ARGV_FIELDS.some(
                (field) =>
                  JSON.stringify((preset.entry as Record<string, unknown>)[field]) ===
                  JSON.stringify(value),
              ),
            )
            expect(supplied).toBe(true)
          }
        },
      ),
    )
  })

  it('refuses every field outside the three the form writes', () => {
    fc.assert(
      fc.property(NAME, FIELD, fc.string({ maxLength: 6 }), (source, field, value) => {
        const edit = sourceEdit({ kind: 'field', source, field, value })
        const allowed = SOURCE_FORM_FIELDS.includes(field) && source.trim() !== ''
        expect(edit !== null).toBe(allowed)
        // And when it does compose, it composes exactly that field's own path.
        if (edit) expect(edit.segments).toEqual(['sources', source, field])
      }),
    )
  })

  it('refuses a registry key whose group would land on an argv field', () => {
    fc.assert(
      fc.property(NAME, SETTING_KEY, fc.integer(), (source, key, value) => {
        const edit = sourceEdit({ kind: 'setting', source, key, value })
        if (edit === null) return
        expect((ARGV_FIELDS as readonly string[]).includes(edit.segments[2])).toBe(false)
        expect(edit.segments.slice(0, 2)).toEqual(['sources', source])
      }),
    )
  })
})

describe('sourceShape refuses what it cannot express', () => {
  /** A registry vocabulary describing the two source-scoped settings. */
  const SETTINGS: readonly RegistrySetting[] = [
    {
      key: 'watch.interval_s',
      kind: 'int',
      default: 300,
      minimum: 30,
      maximum: null,
      scopes: ['app', 'source'],
      summary: '',
    },
  ]

  it('calls an entry expressible only when a preset supplied its poll', () => {
    fc.assert(
      fc.property(fc.array(PRESET, { minLength: 1, maxLength: 3 }), ARGV, (presets, poll) => {
        const shape = sourceShape({ poll }, presets, SETTINGS)
        const supplied = presets.some(
          (preset) => JSON.stringify(preset.entry.poll) === JSON.stringify(poll),
        )
        // Expressible is exactly "a preset supplied this argv" for an entry carrying
        // nothing else: the form's one arming control is `enabled`, so an argv no
        // preset supplied must not reach a form that offers it.
        expect(shape.expressible).toBe(supplied)
        expect(shape.preset !== null).toBe(supplied)
      }),
    )
  })

  it('names every key it neither writes nor displays, and refuses on any', () => {
    fc.assert(
      fc.property(
        PRESET,
        fc.uniqueArray(fc.constantFrom('spend_cap', 'feedback', 'echo', 'spec_types'), {
          maxLength: 3,
        }),
        (preset, extras) => {
          const entry: Record<string, unknown> = { ...preset.entry }
          for (const key of extras) entry[key] = {}
          const shape = sourceShape(entry, [preset], SETTINGS)
          expect([...shape.unexpressed].sort()).toEqual([...extras].sort())
          expect(shape.expressible).toBe(extras.length === 0)
        },
      ),
    )
  })

  it('refuses a setting group holding a leaf no registry record describes', () => {
    fc.assert(
      fc.property(PRESET, fc.boolean(), (preset, known) => {
        const entry = {
          ...preset.entry,
          watch: known ? { interval_s: 60 } : { interval_s: 60, unknown_leaf: 1 },
        }
        const shape = sourceShape(entry, [preset], SETTINGS)
        // Leaf by leaf: a group with a leaf nothing describes has no kind, no bounds
        // and no summary to generate a control from, so showing its siblings does not
        // express it.
        expect(shape.expressible).toBe(known)
      }),
    )
  })
})
