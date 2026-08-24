/**
 * Property 2: the settings form is TOTAL over the registry.
 *
 * The form is generated, so its correctness is not a claim about the twenty-one
 * settings the engine ships today — it is a claim about any vocabulary the read can
 * return. For every setting, whatever its kind, bounds and permitted scopes:
 *
 *   - exactly ONE control renders for it, and it is the control its registry `kind`
 *     names — a number input for `int` and `float`, a two-state control for `bool`,
 *     free text for `str`;
 *   - a numeric control carries the registry's bounds and NOTHING it did not
 *     supply: an invented ceiling refuses a value the engine accepts, and a missing
 *     one lets a control promise a range the write door will reject;
 *   - a kind with no control renders the read-only fallback naming the kind and
 *     routing to the JSON view, rather than vanishing from the form or crashing the
 *     pane;
 *   - every scope the registry permits is offered, and one this surface cannot
 *     address is offered DISABLED rather than hidden — hiding it would quietly deny
 *     an override the engine accepts.
 *
 * A generator is the only way to state that. A vocabulary is data the engine owns,
 * so the interesting cases are the ones nobody wrote a fixture for: a kind this
 * form has never seen, a setting with a maximum and no minimum, a project-scoped
 * setting while no project is selected, a key whose group holds a dot.
 *
 * The second half of this file property-checks the shared staged-edit
 * reconciliation, which is what keeps the review card honest. `buildFormPatch` is
 * last-edit-wins over overlapping paths, so an ancestor and a descendant staged
 * together cannot both reach the store — and a review card built from a staged list
 * holding both would describe a change the write silently drops.
 */
import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/react'
import * as fc from 'fast-check'

import {
  SettingsFields,
  settingFields,
  type SettingField,
} from '../apps/spec-engine/ConfigPanel'
import {
  DELETE,
  buildFormPatch,
  isDescendant,
  nodeAt,
  samePath,
  settingSegments,
  type StagedEdit,
} from '../apps/spec-engine/configDocument'
import { stageEdit, unstageEdit } from '../apps/spec-engine/useStagedEdits'
import type { EffectiveSetting, RegistrySetting } from '../apps/spec-engine/api'
import en from '../i18n/locales/en.json'

const T = en.apps.specEngine.settingsForm

/** The control an input type is expected to be, by the registry kind it edits. */
const EXPECTED: Record<string, string> = {
  int: 'number',
  float: 'number',
  bool: 'checkbox',
  str: 'text',
}

/**
 * Kinds the registry can carry, including ones this form has no control for.
 *
 * `duration` and `list` are not hypothetical the way an impossible value would be:
 * the registry is the ENGINE's, a kind added there reaches this form before anybody
 * writes a control for it, and the fallback arm is what makes that a read-only row
 * instead of a blank one or a crash.
 */
const KIND = fc.constantFrom('int', 'float', 'bool', 'str', 'duration', 'list', '')

/**
 * Scope names, including one no path can be composed for.
 *
 * `region` stands for a scope the engine gains before this surface learns where it
 * writes. It must still be OFFERED — the registry says the setting is overridable
 * there — and it must be unusable, because a form that guessed an address would
 * write into a section nothing reads.
 */
const SCOPE = fc.constantFrom('app', 'project', 'source', 'region')

/** Dotted keys, including group and leaf names that hold a dot or a hostile name. */
const KEY = fc.constantFrom(
  'limits.task_retry_limit',
  'budget.warn_fraction',
  'delivery.auto_integrate',
  'notify.channel',
  'watch.interval_s',
  'a.b.c',
  '__proto__.constructor',
  'timeouts.stage_command_s',
)

/** A registry entry, with bounds that may be absent on either side. */
const SETTING: fc.Arbitrary<RegistrySetting> = fc
  .record({
    key: KEY,
    kind: KIND,
    default: fc.oneof(fc.integer(), fc.boolean(), fc.string({ maxLength: 6 })),
    minimum: fc.option(fc.integer({ min: -10, max: 10 }), { nil: null }),
    maximum: fc.option(fc.integer({ min: 11, max: 100 }), { nil: null }),
    scopes: fc.uniqueArray(SCOPE, { minLength: 1, maxLength: 4 }),
    summary: fc.string({ maxLength: 24 }),
  })
  .map((entry) => ({ ...entry }))

/** A vocabulary: settings with distinct keys, as the registry projects them. */
const VOCABULARY: fc.Arbitrary<RegistrySetting[]> = fc
  .array(SETTING, { minLength: 1, maxLength: 6 })
  .map((settings) => {
    const seen = new Set<string>()
    return settings.filter((setting) => {
      if (seen.has(setting.key)) return false
      seen.add(setting.key)
      return true
    })
  })

/** The names a project- and source-scoped write can target, either absent. */
const TARGETS = fc.record({
  project: fc.constantFrom('', 'acme', 'a.b'),
  source: fc.constantFrom('', 'gh'),
})

/** One resolved value for a key, so a row has something in force to show. */
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

/** Render the generated fields with nothing staged, and return the rows. */
function renderFields(fields: readonly SettingField[]): HTMLElement[] {
  const { unmount } = render(
    <SettingsFields
      fields={fields}
      stagedAt={() => undefined}
      onScope={() => {}}
      onStage={() => {}}
      onWithdraw={() => {}}
    />,
  )
  const rows = [...document.querySelectorAll('.se-setting')] as HTMLElement[]
  // Returned before the unmount would remove them: the assertions read the nodes,
  // not the live tree, and every run has to start from an empty body.
  const detached = rows.map((node) => node.cloneNode(true) as HTMLElement)
  unmount()
  return detached
}

describe('Property 2: the settings form is total over the registry', () => {
  it('renders exactly one control per setting, of the kind the registry names', () => {
    fc.assert(
      fc.property(VOCABULARY, TARGETS, (settings, targets) => {
        const fields = settingFields(settings, inForceFor(settings), targets, {})
        // One field per setting, in the registry's own order: a form that dropped
        // or reordered one would be a form that does not describe the engine.
        expect(fields.map((field) => field.setting.key)).toEqual(
          settings.map((setting) => setting.key),
        )
        const rows = renderFields(fields)
        expect(rows).toHaveLength(settings.length)
        rows.forEach((node, index) => {
          const setting = settings[index]
          const inputs = node.querySelectorAll('input')
          const expected = EXPECTED[setting.kind]
          if (expected === undefined) {
            // The fallback arm: no control at all, the kind named, and the JSON
            // view pointed at. A row that rendered nothing would hide a stored
            // value; a control of a guessed type would write the wrong shape.
            expect(inputs).toHaveLength(0)
            expect(node.textContent).toContain(
              T.the_registry_kind_is_not_editable_here.replace('{{kind}}', setting.kind),
            )
            return
          }
          expect(inputs).toHaveLength(1)
          expect(inputs[0].getAttribute('type')).toBe(expected)
          // The key is on the row whatever the kind, because it is what the
          // document and the write log speak.
          expect(node.textContent).toContain(setting.key)
          expect(node.getAttribute('data-kind')).toBe(setting.kind)
        })
      }),
      { numRuns: 60 },
    )
  })

  it('carries the registry bounds on a numeric control, and no bound it did not supply', () => {
    fc.assert(
      fc.property(VOCABULARY, TARGETS, (settings, targets) => {
        const rows = renderFields(
          settingFields(settings, inForceFor(settings), targets, {}),
        )
        rows.forEach((node, index) => {
          const setting = settings[index]
          const input = node.querySelector('input')
          if (!input || EXPECTED[setting.kind] !== 'number') return
          expect(input.getAttribute('min')).toBe(
            setting.minimum === null ? null : String(setting.minimum),
          )
          expect(input.getAttribute('max')).toBe(
            setting.maximum === null ? null : String(setting.maximum),
          )
          // Whole counts for an int, any fraction for a float: the engine refuses a
          // fractional int, and stepping a fraction by one offers only values
          // outside its own bounds.
          expect(input.getAttribute('step')).toBe(setting.kind === 'int' ? '1' : 'any')
        })
      }),
      { numRuns: 60 },
    )
  })

  it('offers every permitted scope, and disables the ones it cannot address', () => {
    fc.assert(
      fc.property(VOCABULARY, TARGETS, (settings, targets) => {
        const fields = settingFields(settings, inForceFor(settings), targets, {})
        const rows = renderFields(fields)
        rows.forEach((node, index) => {
          const setting = settings[index]
          const buttons = [...node.querySelectorAll('button')]
          if (EXPECTED[setting.kind] === undefined) {
            // Nothing to write, so nothing to choose a scope for.
            expect(buttons).toHaveLength(0)
            return
          }
          expect(buttons.map((button) => button.textContent)).toEqual(setting.scopes)
          const field = fields[index]
          buttons.forEach((button, position) => {
            const offer = field.offers[position]
            // Composable exactly when the engine's own path composition has an
            // address: app is always addressable, project and source need a name,
            // and a scope this surface has never seen has no address at all.
            expect(offer.segments === null).toBe(button.disabled)
            expect(offer.segments === null).toBe(
              settingSegments(
                setting.key,
                offer.scope,
                offer.scope === 'project'
                  ? targets.project
                  : offer.scope === 'source'
                    ? targets.source
                    : '',
              ) === null,
            )
          })
          // The chosen scope is one that can actually be written, or none is.
          if (field.segments !== null) {
            expect(field.offers.some((offer) => offer.scope === field.scope)).toBe(true)
            expect(buttons.filter((button) => button.getAttribute('aria-pressed') === 'true'))
              .toHaveLength(1)
          }
        })
      }),
      { numRuns: 60 },
    )
  })
})

/** Paths a form can stage, chosen to overlap each other often. */
const PATH = fc.constantFrom<readonly string[]>(
  ['sources'],
  ['sources', 'gh'],
  ['sources', 'gh', 'enabled'],
  ['sources', 'gh', 'watch', 'interval_s'],
  ['sources', 'forgejo'],
  ['limits'],
  ['limits', 'task_retry_limit'],
  ['projects', 'acme', 'limits', 'task_retry_limit'],
  ['__proto__'],
  ['__proto__', 'enabled'],
)

const STAGE_VALUE = fc.oneof(
  fc.integer({ min: 0, max: 9 }),
  fc.boolean(),
  fc.string({ maxLength: 4 }),
  fc.constant(DELETE),
)

/** A sequence of stage and withdraw acts, as an operator makes them. */
const ACTS = fc.array(
  fc.oneof(
    fc.record({ act: fc.constant('stage' as const), path: PATH, value: STAGE_VALUE }),
    fc.record({ act: fc.constant('unstage' as const), path: PATH }),
  ),
  { minLength: 1, maxLength: 12 },
)

/** Replay a sequence of acts through the shared reducers. */
function replay(acts: ReturnType<typeof ACTS.generate>['value']): StagedEdit[] {
  let edits: StagedEdit[] = []
  for (const act of acts) {
    edits =
      act.act === 'stage'
        ? stageEdit(edits, act.path, act.value)
        : unstageEdit(edits, act.path)
  }
  return edits
}

describe('the staged-edit reconciliation keeps the review honest', () => {
  it('leaves no two staged paths overlapping, however they were staged', () => {
    fc.assert(
      fc.property(ACTS, (acts) => {
        const edits = replay(acts)
        for (const one of edits) {
          for (const other of edits) {
            if (one === other) continue
            expect(samePath(one.segments, other.segments)).toBe(false)
            expect(isDescendant(one.segments, other.segments)).toBe(false)
          }
        }
      }),
      { numRuns: 400 },
    )
  })

  it('carries every staged edit into the patch, at its own path and value', () => {
    // The claim the review card rests on: it renders one sentence per staged edit,
    // so an edit the patch drops is a sentence describing a change that will not
    // happen. `buildFormPatch` is last-edit-wins, which is exactly why staging has
    // to reconcile instead of appending.
    fc.assert(
      fc.property(ACTS, (acts) => {
        const edits = replay(acts)
        const patch = JSON.parse(JSON.stringify(buildFormPatch(edits))) as unknown
        for (const edit of edits) {
          const stored = edit.value === DELETE ? null : edit.value
          expect(JSON.stringify(nodeAt(patch, edit.segments) ?? null)).toBe(
            JSON.stringify(stored ?? null),
          )
        }
      }),
      { numRuns: 400 },
    )
  })

  it('keeps the last act at a path, and keeps the order of the others', () => {
    fc.assert(
      fc.property(ACTS, (acts) => {
        const edits = replay(acts)
        const last = acts[acts.length - 1]
        if (last.act === 'stage') {
          // The newest act always survives: it is what the operator just did and
          // what the review is about to read out.
          const staged = edits.find((edit) => samePath(edit.segments, last.path))
          expect(staged).toBeDefined()
          // Compared by identity, because the removal sentinel is a symbol: a
          // JSON comparison would silently call every DELETE equal to every other
          // unserialisable value.
          expect(staged?.value).toBe(last.value)
        } else {
          expect(edits.some((edit) => samePath(edit.segments, last.path))).toBe(false)
        }
      }),
      { numRuns: 400 },
    )
  })

  it('replaces an edit where it sits rather than moving it to the end', () => {
    // The review reads in the order the changes were first made, so correcting one
    // must not reorder the account of the others.
    const first = stageEdit([], ['limits', 'task_retry_limit'], 3)
    const two = stageEdit(first, ['budget', 'warn_fraction'], 0.5)
    const corrected = stageEdit(two, ['limits', 'task_retry_limit'], 9)
    expect(corrected.map((edit) => edit.segments.join('.'))).toEqual([
      'limits.task_retry_limit',
      'budget.warn_fraction',
    ])
    expect(corrected[0].value).toBe(9)
  })

  it('drops an ancestor when a descendant is staged, and the other way round', () => {
    // Either order, because the patch builder loses one of the pair either way and
    // the staged list must not claim otherwise.
    const ancestorFirst = stageEdit(
      stageEdit([], ['sources', 'gh'], { enabled: true }),
      ['sources', 'gh', 'enabled'],
      false,
    )
    expect(ancestorFirst.map((edit) => edit.segments.join('.'))).toEqual(['sources.gh.enabled'])
    const descendantFirst = stageEdit(
      stageEdit([], ['sources', 'gh', 'enabled'], false),
      ['sources', 'gh'],
      { enabled: true },
    )
    expect(descendantFirst.map((edit) => edit.segments.join('.'))).toEqual(['sources.gh'])
  })

  it('refuses a staged edit that addresses nothing', () => {
    // A patch with no segments would be the whole document, which is the one write
    // no form ever means to make — refused where the caller still is, rather than
    // later inside the builder.
    expect(() => stageEdit([], [], 1)).toThrow()
  })

  it('copies the path it was handed', () => {
    // A caller's array that changed later would silently move a staged edit to
    // another path, and the review card would describe the old one.
    const segments = ['limits', 'task_retry_limit']
    const [edit] = stageEdit([], segments, 3)
    segments[1] = 'verify_retry_limit'
    expect(edit.segments).toEqual(['limits', 'task_retry_limit'])
  })
})

describe('the path a scope writes', () => {
  it('is the engine’s own composition, and null when there is no address', () => {
    // Not a property but the table the property leans on: these are the four paths
    // `stored_value` reads, spelled once.
    expect(settingSegments('limits.task_retry_limit', 'app', '')).toEqual([
      'limits',
      'task_retry_limit',
    ])
    expect(settingSegments('limits.task_retry_limit', 'project', 'acme')).toEqual([
      'projects',
      'acme',
      'limits',
      'task_retry_limit',
    ])
    expect(settingSegments('watch.interval_s', 'source', 'gh')).toEqual([
      'sources',
      'gh',
      'watch',
      'interval_s',
    ])
    // The key splits at its FIRST dot only, which is the engine's own split: the
    // leaf is one segment however many dots it holds.
    expect(settingSegments('a.b.c', 'app', '')).toEqual(['a', 'b.c'])
    // A name holding a dot is one segment, so the composed path has four.
    expect(settingSegments('limits.task_retry_limit', 'project', 'a.b')).toEqual([
      'projects',
      'a.b',
      'limits',
      'task_retry_limit',
    ])
    // No target, no address: `projects..limits.x` would write into a project named
    // the empty string.
    expect(settingSegments('limits.task_retry_limit', 'project', '')).toBeNull()
    expect(settingSegments('watch.interval_s', 'source', '')).toBeNull()
    // A scope this surface has no composition for, and a key with no group.
    expect(settingSegments('limits.task_retry_limit', 'region', 'eu')).toBeNull()
    expect(settingSegments('bare', 'app', '')).toBeNull()
    expect(settingSegments('trailing.', 'app', '')).toBeNull()
  })
})
