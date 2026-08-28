/**
 * Which generated settings rows a surface shows, and which it collapses.
 *
 * An operator who changed three settings wants to see those three, not the
 * eighteen nobody touched. So a settings surface renders the rows whose in-force
 * value is not the bundled default, states how many are at it, and offers those
 * behind one control that does not leave the stage.
 *
 * ## Why this is a module and not four lines in the render
 *
 * "Is this row at its bundled default" is a decision about what an operator is
 * ALLOWED not to see, so it is stated once, purely, and over a structural type
 * both settings surfaces satisfy. Two spellings of it would be two chances for one
 * surface to hide a configured value the other shows.
 *
 * ## The answer is the engine's, twice over
 *
 * Nothing here holds a default table. `is_default` is the engine's own reading of
 * its precedence chain — true exactly when no layer declared the setting, so the
 * bundled default is what answers — and `setting.default` is the registry's
 * declared default projected on the same wire. Both are read; neither is derived.
 *
 * The two are checked TOGETHER rather than either alone, and the conjunction is
 * deliberate in one direction: a row is hidden only when the engine says nothing
 * declares it AND the value in force equals the declared default. When the two
 * disagree — a payload whose origin says "bundled default" over a value that is not
 * the bundled default — the row is SHOWN. A disagreement means an in-force value
 * nobody can account for, which is the last thing to hide from the person
 * configuring the engine. The conjunction therefore never hides more than
 * `is_default` alone would.
 *
 * And it is emphatically not a value comparison alone. A setting somebody pinned to
 * a value that happens to equal the default is a DECISION, and it is
 * distinguishable from an untouched setting only by its origin — the reason the
 * resolved read carries one at all. Hiding it would hide the pin, and a later
 * change to the bundled default would then move a value the operator believed they
 * had fixed.
 */

/**
 * The shape of one generated row this module needs, and no more.
 *
 * Structural rather than an import of `SettingField`: the settings form owns that
 * type, and a type import back from it would tie this module to a 6,000-line
 * component to read two fields. Anything carrying a registry setting and an
 * optional resolved value satisfies it.
 */
export interface DefaultableSetting {
  setting: { key: string; default: unknown }
  /** The value in force and whether the engine resolved it FROM the default. */
  inForce: { value: unknown; is_default: boolean } | undefined
}

/**
 * Whether two setting values are the same value.
 *
 * Structural comparison through JSON, the idiom this pane already uses to decide
 * whether a staged edit types back what a path stores: a setting's value can be a
 * list or a mapping, so `===` would call two equal lists different and hide
 * nothing. `undefined` and `null` are normalised together because the wire has only
 * one of them — an absent default arrives as `null`.
 */
function sameValue(left: unknown, right: unknown): boolean {
  return JSON.stringify(left ?? null) === JSON.stringify(right ?? null)
}

/**
 * Whether *field* is at its bundled default, and so may be collapsed.
 *
 * A field with no resolved value is NOT at its default: nothing was read for it, so
 * there is no answer to hide behind, and a row absent from the resolved payload is
 * the one an operator most needs to see.
 */
export function atBundledDefault(field: DefaultableSetting): boolean {
  const inForce = field.inForce
  if (inForce === undefined) return false
  if (!inForce.is_default) return false
  return sameValue(inForce.value, field.setting.default)
}

/** How many of *fields* resolve to their bundled default. */
export function bundledDefaultCount(fields: readonly DefaultableSetting[]): number {
  return fields.filter(atBundledDefault).length
}

/**
 * The rows to render, in the order they were generated.
 *
 * Three reasons a row survives the filter, and the third is not a nicety:
 *
 * 1. *everySetting* is set, so the operator asked for all of them.
 * 2. The row is not at its bundled default, which is the common case.
 * 3. The row holds a STAGED EDIT. An edit whose row is not rendered is an edit no
 *    sentence describes, no confirm clears and no reconciliation drops — it would
 *    still reach the patch. The path is real: reveal every setting, stage an edit on
 *    a default-valued row, then collapse again. So a staged row is pinned visible
 *    until the edit is withdrawn or written.
 *
 * *staged* is a predicate rather than a list of paths because only the caller knows
 * which scope a row currently writes at, and therefore which path to look under.
 */
export function shownSettings<Field extends DefaultableSetting>(
  fields: readonly Field[],
  everySetting: boolean,
  staged: (field: Field) => boolean,
): Field[] {
  if (everySetting) return [...fields]
  return fields.filter((field) => !atBundledDefault(field) || staged(field))
}
