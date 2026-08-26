/**
 * The settings subsections are exactly the registry's groups.
 *
 * `settingGroups` is the pure partition the grouped Settings tab renders from, and
 * its correctness is not a claim about the twenty-one settings the engine ships — it
 * is a claim about any vocabulary the read can return. For every generated field
 * list:
 *
 *   - the set of subsections equals the set of distinct group segments — the leading
 *     dot-segment of each registry key — with no group invented and none dropped;
 *   - the subsections appear in first-appearance order, the same order the groups
 *     first show up in the input, never sorted and never a hard-coded list;
 *   - every field lands under exactly its own group, the partition loses no row —
 *     each input field appears exactly once across the subsections, none dropped or
 *     duplicated — and the rows keep the input's relative order within each group.
 *
 * A generator is the only way to state that. A vocabulary is data the engine owns,
 * so the interesting cases are the ones nobody wrote a fixture for: a group that
 * reappears after another, a key whose group holds no dot, a hostile group name.
 * A hard-coded group list would fail the first-appearance-order and totality
 * claims; filtering an unmapped group would fail the set-equality claim.
 */
import { describe, it, expect } from 'vitest'
import * as fc from 'fast-check'

import { settingFields, settingGroups, type SettingField } from '../apps/spec-engine/ConfigPanel'
import type { EffectiveSetting, RegistrySetting } from '../apps/spec-engine/api'

/** Group segments, including a no-dot name and a hostile one, chosen to repeat. */
const GROUP = fc.constantFrom('limits', 'budget', 'watch', 'concurrency', 'bare', '__proto__')

/** A leaf, so a full key is `group.leaf` (or the bare group when it has no dot). */
const LEAF = fc.constantFrom('a', 'b', 'c', 'x.y')

/** A registry entry whose key groups under one of a small, repeating vocabulary. */
const SETTING: fc.Arbitrary<RegistrySetting> = fc
  .record({ group: GROUP, leaf: LEAF, kind: fc.constantFrom('int', 'float', 'bool', 'str') })
  .map(({ group, leaf, kind }) => ({
    key: group === 'bare' ? group : `${group}.${leaf}`,
    kind,
    default: 0,
    minimum: null,
    maximum: null,
    scopes: ['app'],
    summary: '',
  }))

/** A vocabulary: settings with distinct keys, as the registry projects them. */
const VOCABULARY: fc.Arbitrary<RegistrySetting[]> = fc
  .array(SETTING, { minLength: 1, maxLength: 10 })
  .map((settings) => {
    const seen = new Set<string>()
    return settings.filter((setting) => {
      if (seen.has(setting.key)) return false
      seen.add(setting.key)
      return true
    })
  })

/** One resolved value per key, so a field is well-formed. */
function inForceFor(settings: readonly RegistrySetting[]): Map<string, EffectiveSetting> {
  const found = new Map<string, EffectiveSetting>()
  for (const setting of settings) {
    found.set(setting.key, {
      key: setting.key,
      value: setting.default,
      origin: 'bundled_default',
      declared_at: '',
      is_default: true,
    })
  }
  return found
}

/** The leading dot-segment of a key, or the whole key when it holds no dot. */
function groupOf(key: string): string {
  const dot = key.indexOf('.')
  return dot < 0 ? key : key.slice(0, dot)
}

function fieldsOf(settings: readonly RegistrySetting[]): SettingField[] {
  return settingFields(settings, inForceFor(settings), { project: '', source: '' }, {})
}

describe('settingGroups partitions the fields by registry group', () => {
  it('yields the distinct groups in first-appearance order, none invented or dropped', () => {
    fc.assert(
      fc.property(VOCABULARY, (settings) => {
        const groups = settingGroups(fieldsOf(settings))
        // First-appearance order: the order each group's first key shows up in.
        const expectedOrder: string[] = []
        for (const setting of settings) {
          const group = groupOf(setting.key)
          if (!expectedOrder.includes(group)) expectedOrder.push(group)
        }
        expect(groups.map((entry) => entry.group)).toEqual(expectedOrder)
        // Set equality: exactly the distinct groups, no more and no fewer.
        expect(new Set(groups.map((entry) => entry.group))).toEqual(new Set(expectedOrder))
      }),
      { numRuns: 300 },
    )
  })

  it('places every field under exactly its own group, dropping and duplicating none', () => {
    fc.assert(
      fc.property(VOCABULARY, (settings) => {
        const fields = fieldsOf(settings)
        const groups = settingGroups(fields)
        // Each field sits under its own group segment.
        for (const { group, fields: groupFields } of groups) {
          for (const field of groupFields) {
            expect(groupOf(field.setting.key)).toBe(group)
          }
          // Within a group, the rows keep the input's relative order.
          expect(groupFields).toEqual(
            fields.filter((field) => groupOf(field.setting.key) === group),
          )
        }
        // Total: every input field appears exactly once across the subsections —
        // none dropped, none duplicated — so the partition loses no row. Order
        // across groups is first-appearance, not input order, so this is a
        // multiset claim rather than an equal-sequence one.
        const flattened = groups.flatMap((entry) => entry.fields)
        expect(flattened).toHaveLength(fields.length)
        expect(new Set(flattened)).toEqual(new Set(fields))
      }),
      { numRuns: 300 },
    )
  })
})
