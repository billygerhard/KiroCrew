/**
 * Property 3, frontend half: a source's argv is always a preset's argv.
 *
 * The write door validates an argv's SHAPE and not which program it names, so the
 * boundary on what the engine can be made to run through configuration is the
 * bundled preset tables plus this form's refusal to compose an argv freely. That makes
 * three claims, and none of them is a claim about the two presets shipped today:
 *
 *   - **`composeSource` copies the command.** For any preset entry, the staged
 *     entry's `poll` is byte-equal to the preset's own, its `field_map` likewise, and
 *     `enabled` is ABSENT rather than false — polling is what arms an unattended run,
 *     so a fresh copy must be inert. The copy is also deep: the read's cached object
 *     must never become the staged value, or an edit to one source's staged copy
 *     would change what the next copy is offered.
 *   - **The repository parameter changes only its own slot.** The presets ship an
 *     `OWNER/REPO` placeholder the project is expected to name, so the form names it —
 *     and every argv it stages is the preset's own array with only the positions
 *     holding that literal replaced. `argv[0]` is never such a position, and a preset
 *     whose placeholder sits there is refused rather than substituted, because that
 *     position is the PROGRAM.
 *   - **No sequence of form actions stages an argv outside those two shapes.**
 *     `sourceEdit` is the one place this form composes a path under `sources`, so the
 *     property is stated over ARBITRARY action sequences — including a field named
 *     `poll`, a registry key whose group is `field_map`, and a settings group the
 *     schema keeps disjoint today but might not tomorrow — and checked against the
 *     patch those actions actually build.
 *
 * A generator is the only way to state any of them. A hard-coded case proves the two
 * shipped presets copy correctly; it cannot prove that the set of argv this form can
 * write is exactly the preset table modulo one designated slot.
 */
import { describe, it, expect } from 'vitest'
import * as fc from 'fast-check'

import {
  SOURCE_FORM_FIELDS,
  composeSource,
  designatedSlots,
  matchPoll,
  pollForRepository,
  sourceEdit,
  sourceShape,
  type SourceFormAction,
} from '../apps/spec-engine/ConfigPanel'
import { DELETE, buildFormPatch, type StagedEdit } from '../apps/spec-engine/configDocument'
import { stageEdit } from '../apps/spec-engine/useStagedEdits'
import type { RegistrySetting, SourcePreset } from '../apps/spec-engine/api'

/** The two argv-bearing fields, which are the subject of the whole property. */
const ARGV_FIELDS = ['poll', 'field_map'] as const

/** The engine's own placeholder literal, as `WATCH_SOURCE_PRESETS` spells it. */
const PLACEHOLDER = 'OWNER/REPO'

/** An argv: a non-empty list of strings, as the engine's own preset tables hold. */
const ARGV = fc.array(fc.string({ minLength: 1, maxLength: 8 }), { minLength: 1, maxLength: 5 })

/**
 * An argv carrying the placeholder at an arbitrary position, including position 0.
 *
 * The shapes that matter are all here: the placeholder as a whole argument (GitLab's
 * form), embedded in a longer argument (GitHub's), at more than one position, and on
 * the program itself — which is the one the form has to refuse.
 */
const PLACEHOLDER_ARGV: fc.Arbitrary<string[]> = fc
  .record({
    before: fc.array(fc.string({ minLength: 1, maxLength: 5 }), { maxLength: 3 }),
    prefix: fc.constantFrom('', 'repos/', 'api/v4/projects/'),
    suffix: fc.constantFrom('', '/issues?state=all', '.git'),
    after: fc.array(fc.string({ minLength: 1, maxLength: 5 }), { maxLength: 3 }),
    extra: fc.boolean(),
  })
  .map(({ before, prefix, suffix, after, extra }) => {
    const slot = `${prefix}${PLACEHOLDER}${suffix}`
    // The placeholder-free arguments must stay placeholder-free, or the generator
    // would quietly designate positions the case did not mean to designate.
    const clean = (parts: string[]) => parts.map((part) => part.replace(/OWNER\/REPO/g, 'x'))
    return [...clean(before), slot, ...(extra ? [slot] : []), ...clean(after)]
  })

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
    poll: fc.oneof(ARGV, PLACEHOLDER_ARGV),
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

/** A repository a parameter control could be handed, argv-looking text included. */
const REPOSITORY = fc.constantFrom(
  'acme/widgets',
  '',
  '   ',
  'a/b --jq .',
  '$(id)',
  'OWNER/REPO',
  '../../etc/passwd',
)

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
      repository: REPOSITORY,
    }),
    fc.record({
      kind: fc.constant('repository' as const),
      source: NAME,
      preset: fc.constantFrom(...presets),
      repository: REPOSITORY,
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

/**
 * Whether *value* is an argv one of *presets* supplied, modulo the repository slot.
 *
 * The whole claim of the form, as a predicate: an argv it stages is either a
 * preset's own bytes or a preset's own bytes with the designated slots — and only
 * those — holding something else. `matchPoll` is what decides that, so the property
 * exercises the same rule the form's expressibility gate turns on.
 */
function suppliedArgv(field: string, value: unknown, presets: readonly SourcePreset[]): boolean {
  return presets.some((preset) => {
    const own = (preset.entry as Record<string, unknown>)[field]
    if (JSON.stringify(own) === JSON.stringify(value)) return true
    return field === 'poll' && matchPoll(own, value) !== null
  })
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

describe('the repository parameter changes only its own slot', () => {
  it('never designates the program position, and refuses a preset that would', () => {
    fc.assert(
      fc.property(PLACEHOLDER_ARGV, (poll) => {
        const slots = designatedSlots(poll)
        // Position zero is the PROGRAM the engine executes. A form that substituted
        // there would let a data field decide what runs, so a preset whose
        // placeholder sits there gets no designated slot at all rather than a
        // narrower one.
        expect(slots).not.toContain(0)
        if (poll[0].includes(PLACEHOLDER)) expect(slots).toEqual([])
        else expect(slots.length).toBeGreaterThan(0)
        for (const slot of slots) expect(poll[slot]).toContain(PLACEHOLDER)
      }),
    )
  })

  it('stages the preset\u2019s own argv with only the designated slots filled', () => {
    fc.assert(
      fc.property(PRESET, NAME, REPOSITORY, (preset, source, repository) => {
        const edit = sourceEdit({ kind: 'repository', source, preset, repository })
        const template = preset.entry.poll as string[]
        const slots = designatedSlots(template)
        if (slots.length === 0 || source.trim() === '') {
          // No slot to fill is a refusal, not a substitution: there is nowhere in
          // this argv the form is allowed to write.
          expect(edit).toBeNull()
          return
        }
        expect(edit).not.toBeNull()
        const staged = (edit as StagedEdit).value as unknown[]
        expect((edit as StagedEdit).segments).toEqual(['sources', source, 'poll'])
        // Same length, same program, same everything outside a designated slot.
        expect(staged).toHaveLength(template.length)
        expect(staged[0]).toBe(template[0])
        for (let index = 0; index < template.length; index += 1) {
          if (slots.includes(index)) continue
          expect(staged[index]).toBe(template[index])
        }
        // And the slots hold the trimmed value, or the placeholder when it is empty:
        // a poll naming the literal is refused loudly, which is safer than one
        // naming a repository nobody meant.
        const named = repository.trim() === '' ? PLACEHOLDER : repository.trim()
        const match = matchPoll(template, staged)
        expect(match).not.toBeNull()
        expect((match as { repository: string }).repository).toBe(named)
      }),
    )
  })

  it('accepts exactly the polls that differ from a preset only at its slots', () => {
    fc.assert(
      fc.property(
        PRESET,
        REPOSITORY,
        fc.nat(6),
        fc.string({ minLength: 1, maxLength: 5 }),
        (preset, repository, index, noise) => {
          const template = preset.entry.poll as string[]
          const slots = designatedSlots(template)
          const filled = pollForRepository(preset, repository)
          if (slots.length === 0) {
            expect(filled).toBeNull()
            // With no slot, expressibility collapses back to byte-equality.
            expect(matchPoll(template, template)).not.toBeNull()
            expect(matchPoll(template, [...template, noise])).toBeNull()
            return
          }
          // A substituted poll matches. That is the whole repair: the engine's own
          // presets ship a placeholder the project replaces, so a poll that actually
          // polls something is still one this form can account for.
          expect(matchPoll(template, filled)).not.toBeNull()
          // A poll differing anywhere OUTSIDE a slot does not match, whatever the
          // slots hold — a changed flag, a changed query string, another program.
          const at = index % template.length
          const changed = [...(filled as string[])]
          changed[at] = `${changed[at]}${noise}`
          const stillFramed = slots.includes(at) && matchPoll(template, changed) !== null
          expect(matchPoll(template, changed) === null || stillFramed).toBe(true)
          if (!slots.includes(at)) expect(matchPoll(template, changed)).toBeNull()
          // And a poll of a different length never matches.
          expect(matchPoll(template, (filled as string[]).slice(1))).toBeNull()
        },
      ),
    )
  })
})

describe('no sequence of form actions stages an argv outside a preset\u2019s own', () => {
  it('builds a patch whose every argv path is a preset\u2019s, modulo the slot', () => {
    fc.assert(
      fc.property(
        fc.array(PRESET, { minLength: 1, maxLength: 3 }),
        fc.array(fc.nat(6), { maxLength: 6 }),
        (presets, seeds) => {
          // The actions are drawn against the presets that exist, exactly as the form
          // draws them: a copy is always a copy of something the read supplied, and a
          // repository is always named into a preset the read supplied.
          const actions = seeds.map((seed) =>
            fc.sample(ACTION(presets), { numRuns: 1, seed })[0],
          )
          let edits: readonly StagedEdit[] = []
          for (const action of actions) {
            const edit = sourceEdit(action)
            // `null` is the refusal, and the form drops it rather than guessing a
            // path — a field outside the three, a setting group that would land on
            // an argv field, or a preset with no slot to name a repository in.
            if (edit) edits = stageEdit(edits, edit.segments, edit.value)
          }
          const patch = buildFormPatch(edits.filter((edit) => edit.value !== DELETE))
          for (const [field, value] of argvEntries(patch)) {
            expect(
              suppliedArgv(field, value, presets),
              `${field} was not a bundled preset\u2019s own`,
            ).toBe(true)
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

  it('calls an entry expressible when a preset supplied its poll, slot aside', () => {
    fc.assert(
      fc.property(
        fc.array(PRESET, { minLength: 1, maxLength: 3 }),
        fc.oneof(ARGV, PLACEHOLDER_ARGV),
        REPOSITORY,
        (presets, poll, repository) => {
          const shape = sourceShape({ poll }, presets, SETTINGS)
          const supplied = presets.some((preset) => matchPoll(preset.entry.poll, poll) !== null)
          // Expressible is exactly "a preset supplied this argv, up to its own
          // repository slot" for an entry carrying nothing else: the form's one
          // arming control is `enabled`, so an argv it cannot account for argument by
          // argument must not reach a form that offers it.
          expect(shape.expressible).toBe(supplied)
          expect(shape.preset !== null).toBe(supplied)
          // And a preset's poll with a repository named in it stays expressible,
          // which is the case every source that actually polls anything is in.
          for (const preset of presets) {
            const filled = pollForRepository(preset, repository)
            if (filled === null) continue
            const named = sourceShape({ poll: filled }, presets, SETTINGS)
            expect(named.expressible).toBe(true)
            expect(named.slots.length).toBeGreaterThan(0)
          }
        },
      ),
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
