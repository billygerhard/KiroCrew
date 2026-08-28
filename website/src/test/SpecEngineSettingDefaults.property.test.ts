/**
 * Which settings rows a surface may collapse — as a property, not a fixture.
 *
 * The filter decides what an operator is allowed NOT to see, so the interesting
 * cases are the ones nobody writes a fixture for: a value that equals the bundled
 * default while somebody pinned it there, a payload whose origin and value disagree
 * with each other, a row absent from the resolved read, a default that is a list or
 * a mapping rather than a number.
 *
 * Four claims, and each is a safety claim in the same direction — the filter may
 * only ever hide a row nobody configured:
 *
 * 1. **Collapsing never invents or reorders a row.** The shown rows are a
 *    subsequence of the generated ones.
 * 2. **A configured row is never hidden.** Whatever its value, a setting the engine
 *    says is not resolved from the bundled default stays on screen. This is the one
 *    that catches a filter rewritten as a value comparison: a pin to a value that
 *    happens to equal the default is a decision, and hiding it would hide the pin.
 * 3. **A staged row is never hidden.** An edit with no row is an edit no sentence
 *    describes and no confirm clears, while it still reaches the patch.
 * 4. **The count accounts for exactly what was collapsed**, with nothing staged: the
 *    rows shown plus the rows at their default are the rows generated.
 */
import { describe, it, expect } from 'vitest'
import * as fc from 'fast-check'

import {
  atBundledDefault,
  bundledDefaultCount,
  shownSettings,
  type DefaultableSetting,
} from '../apps/spec-engine/settingDefaults'

/**
 * A small pool of setting values, drawn from twice per row.
 *
 * Small on purpose. The cases that matter are the COLLISIONS — a value in force
 * that equals the declared default while the origin says somebody declared it, and
 * the reverse — and a wide generator would make both vanishingly rare, leaving the
 * properties passing over rows whose value and default never coincide.
 */
const VALUE = fc.constantFrom<unknown>(0, 1, 2, 'dashboard', 'slack', true, false, null, [], [1], {
  a: 1,
})

/** One generated row: a registry key with a declared default, and a resolved value. */
const FIELD: fc.Arbitrary<DefaultableSetting> = fc.record({
  setting: fc.record({
    key: fc.constantFrom(
      'limits.task_retry_limit',
      'budget.warn_fraction',
      'notify.channel',
      'watch.interval_s',
    ),
    default: VALUE,
  }),
  // `undefined` stands for a key the resolved read did not answer, which is a third
  // state and not an empty one.
  inForce: fc.option(fc.record({ value: VALUE, is_default: fc.boolean() }), { nil: undefined }),
})

const FIELDS = fc.array(FIELD, { maxLength: 12 })

/** Nothing is staged, which is the state the count describes. */
const nothingStaged = () => false

describe('collapsing the rows at their bundled default', () => {
  it('shows a subsequence of the generated rows, never inventing or reordering one', () => {
    fc.assert(
      fc.property(FIELDS, fc.boolean(), (fields, everySetting) => {
        const shown = shownSettings(fields, everySetting, nothingStaged)
        // Same objects, in the same relative order: a filter that rebuilt a row
        // would be a filter that can rebuild it wrongly.
        let cursor = 0
        for (const field of shown) {
          const found = fields.indexOf(field, cursor)
          expect(found).toBeGreaterThanOrEqual(cursor)
          cursor = found + 1
        }
        expect(shown.length).toBeLessThanOrEqual(fields.length)
      }),
      { numRuns: 200 },
    )
  })

  it('never hides a setting the engine did not resolve from the bundled default', () => {
    fc.assert(
      fc.property(FIELDS, (fields) => {
        const shown = new Set(shownSettings(fields, false, nothingStaged))
        for (const field of fields) {
          // Two ways a row is configured: an origin that is not the bundled default,
          // and no resolved answer at all. Neither may be collapsed — the first is a
          // decision somebody made, the second is a value nobody can account for.
          if (field.inForce === undefined || !field.inForce.is_default) {
            expect(shown.has(field)).toBe(true)
            expect(atBundledDefault(field)).toBe(false)
          }
        }
      }),
      { numRuns: 200 },
    )
  })

  it('never hides a row holding a staged edit', () => {
    fc.assert(
      fc.property(FIELDS, fc.nat(), (fields, pick) => {
        if (fields.length === 0) return
        const target = fields[pick % fields.length]
        const shown = new Set(shownSettings(fields, false, (field) => field === target))
        expect(shown.has(target)).toBe(true)
      }),
      { numRuns: 200 },
    )
  })

  it('counts exactly the rows it collapsed', () => {
    fc.assert(
      fc.property(FIELDS, (fields) => {
        const shown = shownSettings(fields, false, nothingStaged)
        const atDefault = bundledDefaultCount(fields)
        // With nothing staged the two partition the generated rows: a count that did
        // not add up would be a surface reporting a number of settings it is not
        // showing that differs from the number it is not showing.
        expect(shown.length + atDefault).toBe(fields.length)
        // And revealing shows all of them, so nothing is unreachable.
        expect(shownSettings(fields, true, nothingStaged)).toEqual([...fields])
      }),
      { numRuns: 200 },
    )
  })
})

describe('what counts as being at the bundled default', () => {
  it('collapses a row nothing declares whose value is the declared default', () => {
    expect(
      atBundledDefault({
        setting: { key: 'watch.interval_s', default: 300 },
        inForce: { value: 300, is_default: true },
      }),
    ).toBe(true)
  })

  it('keeps a row pinned to a value that happens to equal the default', () => {
    // The reason the origin is read at all: this row and the one above look
    // identical by value, and only one of them is a decision somebody made.
    expect(
      atBundledDefault({
        setting: { key: 'watch.interval_s', default: 300 },
        inForce: { value: 300, is_default: false },
      }),
    ).toBe(false)
  })

  it('keeps a row whose origin and value disagree with each other', () => {
    // A payload claiming the bundled default over a value that is not it. Shown,
    // because the in-force value is then one nobody can account for.
    expect(
      atBundledDefault({
        setting: { key: 'watch.interval_s', default: 300 },
        inForce: { value: 999, is_default: true },
      }),
    ).toBe(false)
  })

  it('keeps a row the resolved read did not answer', () => {
    expect(
      atBundledDefault({ setting: { key: 'watch.interval_s', default: 300 }, inForce: undefined }),
    ).toBe(false)
  })

  it('compares a structured default structurally rather than by identity', () => {
    // A list default arrives as a fresh array on every read, so identity would call
    // two equal lists different and collapse nothing.
    expect(
      atBundledDefault({
        setting: { key: 'notify.channel', default: ['dashboard'] },
        inForce: { value: ['dashboard'], is_default: true },
      }),
    ).toBe(true)
    expect(
      atBundledDefault({
        setting: { key: 'notify.channel', default: ['dashboard'] },
        inForce: { value: ['slack'], is_default: true },
      }),
    ).toBe(false)
  })

  it('treats an absent default and a null default as the same value', () => {
    // The wire carries `null` for a setting with no default; `undefined` is what a
    // reader holds for the same absence.
    expect(
      atBundledDefault({
        setting: { key: 'notify.channel', default: null },
        inForce: { value: undefined, is_default: true },
      }),
    ).toBe(true)
  })
})
