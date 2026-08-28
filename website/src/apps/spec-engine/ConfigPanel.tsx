/**
 * The configuration pane: forms as the write surface, the resolution beside them.
 *
 * Built to `design/mockup-b.html`'s config pane, in the same split the queue uses —
 * the stored configuration on the left where the list was, its resolved read on the
 * right where the inspector was.
 *
 * ## Forms lead, and the raw document is on request
 *
 * The left pane leads with the surfaces an operator can read and fill as a human,
 * and the JSON editor sits behind one explicit control. That inversion is the point
 * of the pane rather than a rearrangement of it: while the document WAS the pane,
 * changing anything without its own control meant hand-editing JSON, so the raw view
 * has to remain complete and reachable while no longer being the thing an operator
 * meets first. Opening it gives the whole editor back, including the engine's
 * problems and advisories for the persisted document.
 *
 * ## `config.json` is the write path, and there is only one
 *
 * Every edit on this pane — a form, a grid cell, a removal, a per-role reset, a
 * document save — funnels into `PUT /config`, which is `ConfigStore.write`: the
 * engine merges, validates the MERGED document, and persists it atomically under a
 * lock. Every rule an operator can trip — an unknown key, an out-of-range value, a
 * setting written at a scope it is not overridable at — is the engine's, reported
 * back by path, so this panel keeps no validation of its own beyond "is this JSON at
 * all". The right pane writes NOTHING; it is a read, and the only writes it offers
 * are per-role resets that go through the same PUT.
 *
 * ## Three properties of the write, none of them cosmetic
 *
 * 1. **A save sends a patch, not the document.** The write path merges, so a key the
 *    operator DELETED has to be spelled as an explicit `null` or the merge keeps the
 *    old value and the editor shows a change that did not happen. See
 *    `configDocument.ts`.
 * 2. **An elided value is never written back.** Credential-classified values are
 *    withheld from the read and can be overwritten but never displayed, so the
 *    sentinel is dropped from every patch — otherwise a save replaces a live token
 *    with the literal string `<elided>` and nothing reports it.
 * 3. **A per-role reset names the node it clears.** The mockup's disabled buttons
 *    read `Nothing to reset` with the missing node in a tooltip, and the enabled one
 *    reads `Clear <path>`; that is not decoration. Clearing a profile's role
 *    assignment and clearing a project's own override are different edits with
 *    different blast radii, and a button labelled only `Reset` is how somebody
 *    clears the wrong one.
 *
 * ## One departure from the mockup, and why
 *
 * The mockup's roles table resets `projects.<project>.roles.<role>`. **The engine has
 * no such node.** Role assignments live only inside a cost profile
 * (`engine/config/profiles.py`: `cost_profiles.<name>.roles.<role>`), and a project
 * SELECTS a profile rather than overriding roles within it. So the reset clears the
 * profile's assignment, the label says so, and the note beside the table states that
 * the profile is shared — because clearing it changes every project that selected
 * that profile, which is exactly the fact a label reading `Reset` would hide.
 *
 * ## Which configuration governs which project
 *
 * The left pane leads with a table of every entry in the document's `projects`
 * section plus a fixed app-defaults row, and the row selected there is what the
 * resolved read beside it resolves FOR. That is one flow rather than a list and
 * an unrelated picker: a pane with two controls for one reading is a pane that
 * can name one project in its heading while showing another's values.
 *
 * A row's removal is the same PUT every other edit uses, with `null` at the
 * entry — `ConfigStore._merge` deletes a key whose patch value is null, so no
 * delete route exists to review, and the removal is validated, locked and
 * recorded exactly like a save. It is armed then confirmed in flow, and the
 * confirm names the project, because "Remove" five times over five rows is how
 * somebody removes the wrong one.
 *
 * ## Layout rules this file must not break
 *
 * No drawer, no modal, no scrim: the selected design passes the "safety controls are
 * never behind navigation" criterion only because it contains no overlay, and
 * `SpecEngineShell.test.tsx` fails on a `position:fixed`/`absolute` declaration. The
 * document editor is a fixed-height region for the same reason the untrusted blocks
 * are — a document that grows with its line count would push the save controls off
 * the pane.
 */
import { Fragment, useCallback, useEffect, useId, useMemo, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle } from 'lucide-react'

import { i18nT } from '../../i18n/t'
import { fmtNumber } from '../../i18n/format'
import {
  QK,
  QK_RESOLVED_ROOT,
  SpecEngineApiError,
  specEngineApi,
  type ConfigAdvisory,
  type ConfigSnapshot,
  type EffectiveSetting,
  type ProfilePreset,
  type RegistrySetting,
  type ResolvedRole,
  type SourceGridCell,
  type SourcePreset,
  type SourcesPayload,
} from './api'
import {
  AUTONOMY_KEY,
  COST_PROFILES,
  DELETE,
  FIELD_EFFORT,
  FIELD_MODEL,
  PROJECT_PROFILE_FIELD,
  PROJECTS,
  ROLES_KEY,
  SCOPE_PROJECT,
  SCOPE_SOURCE,
  SOURCES,
  buildFormPatch,
  buildGridPatch,
  documentText,
  dotted,
  gridCellSegments,
  isDescendant,
  isObject,
  mergePatch,
  nodeAt,
  parseDocument,
  patchAt,
  profileSegments,
  profileSettingSegments,
  roleFieldSegments,
  roleSegments,
  sameCell,
  settingLeaf,
  settingSegments,
  type Document,
  type GridCellRef,
  type PendingEdit,
  type StagedEdit,
} from './configDocument'
import { useStagedEdits } from './useStagedEdits'

/** Separator between two identifiers on one line. Punctuation, not copy. */
const SEP = ' \u00b7 '

/** Stands in for a field the engine has no value for. Punctuation, not copy. */
const NONE = '\u2014'

/** The resolved read with no project named. Not a project id, so not a valid one. */
const APP_WIDE = ''

/** React key for the app-defaults row, whose id is deliberately not a project. */
const APP_DEFAULTS_ROW_KEY = 'app-defaults'

/**
 * The id the autonomy grid section carries, so the source form can link into it.
 *
 * A link rather than a second rendering of the matrix: the grid is one resolution of
 * the engine's autonomy policy, and two copies on one pane would be two answers to
 * one question. Declared here beside the pane's other identifiers because both the
 * section that carries it and the form that links to it need it.
 */
const SOURCES_GRID_ID = 'se-sources-grid'

/**
 * Report *count* to *onCount* whenever it changes, and render nothing.
 *
 * The pane's per-tab badge has to state how much unwritten work a surface holds
 * while that surface is hidden, and the surfaces hold their own staging — three
 * `useStagedEdits` hooks, one per form. This carries the number out without
 * moving the state: lifting three forms' staging into the pane is exactly the
 * drift the shared hook exists to prevent, and a second store of the count is a
 * second thing that can disagree with the patch.
 *
 * A component rather than a hook because the number is only known after each
 * form's own refusal and reading guards have returned — a hook call there would
 * sit after a conditional return. Rendered at the site that computes the count,
 * so the badge and the form's own "unwritten changes" line read one value.
 *
 * An effect rather than a call during render: `onCount` sets state in the parent,
 * and a parent setState during a child's render is a render-phase side effect
 * React refuses.
 */
function PendingCount({
  count,
  onCount,
}: {
  count: number
  onCount?: (count: number) => void
}) {
  const report = useRef<((count: number) => void) | undefined>(onCount)
  useEffect(() => {
    report.current = onCount
    onCount?.(count)
  }, [count, onCount])
  // Unmount reports ZERO, because a surface that is no longer mounted holds no
  // unwritten edits — and the pane's total is a sum over surfaces that never
  // evicted a key. Without this the pane states a count including edits it has
  // already discarded. Reachable: a refused vocabulary read collapses every
  // group into one advanced area, the operator stages an edit on a surface
  // there, then a successful refetch re-expands the five stages and unmounts
  // that surface — its edits are genuinely gone while its count would remain.
  //
  // Its own effect with an empty dependency list, NOT a cleanup on the reporting
  // effect above: that one re-runs on every count change, so its cleanup would
  // report a spurious zero between every real value.
  useEffect(() => () => report.current?.(0), [])
  return null
}

/**
 * The origin of a value in force, in words, keyed by the engine's own enum.
 *
 * Keys, not resolved strings: a module-level `i18nT()` runs once at import and would
 * freeze this table in whichever language happened to be active then.
 */
const ORIGIN_KEY: Record<EffectiveSetting['origin'], string> = {
  bundled_default: 'apps.specEngine.configPanel.origin_bundled_default',
  app_config: 'apps.specEngine.configPanel.origin_app_config',
  cost_profile: 'apps.specEngine.configPanel.origin_cost_profile',
  project_config: 'apps.specEngine.configPanel.origin_project_config',
  source_config: 'apps.specEngine.configPanel.origin_source_config',
}

/**
 * Human label per registry setting key, as whole literal catalog keys so the
 * key-reference gate can resolve every entry. The registry key itself stays on
 * screen as the detail line — it is what the document and the write log speak —
 * but prose leads. A key absent here is NOT an error: the axes belong to the
 * engine, and a setting added to its registry renders by key until a label is
 * added, rather than hiding or crashing.
 */
const SETTING_LABEL_KEY: Record<string, string> = {
  'concurrency.global_max_runs':
    'apps.specEngine.configPanel.setting_labels.concurrency_global_max_runs',
  'concurrency.project_max_runs':
    'apps.specEngine.configPanel.setting_labels.concurrency_project_max_runs',
  'concurrency.wave_max_tasks':
    'apps.specEngine.configPanel.setting_labels.concurrency_wave_max_tasks',
  'limits.task_retry_limit': 'apps.specEngine.configPanel.setting_labels.limits_task_retry_limit',
  'limits.revision_cycle_limit':
    'apps.specEngine.configPanel.setting_labels.limits_revision_cycle_limit',
  'limits.verify_retry_limit':
    'apps.specEngine.configPanel.setting_labels.limits_verify_retry_limit',
  'timeouts.authoring_s': 'apps.specEngine.configPanel.setting_labels.timeouts_authoring_s',
  'timeouts.awaiting_review_s':
    'apps.specEngine.configPanel.setting_labels.timeouts_awaiting_review_s',
  'timeouts.executing_s': 'apps.specEngine.configPanel.setting_labels.timeouts_executing_s',
  'timeouts.delivering_s': 'apps.specEngine.configPanel.setting_labels.timeouts_delivering_s',
  'timeouts.stage_command_s':
    'apps.specEngine.configPanel.setting_labels.timeouts_stage_command_s',
  'timeouts.capability_s': 'apps.specEngine.configPanel.setting_labels.timeouts_capability_s',
  'timeouts.analysis_job_s': 'apps.specEngine.configPanel.setting_labels.timeouts_analysis_job_s',
  'timeouts.poll_command_s': 'apps.specEngine.configPanel.setting_labels.timeouts_poll_command_s',
  'budget.run_ceiling_credits':
    'apps.specEngine.configPanel.setting_labels.budget_run_ceiling_credits',
  'budget.warn_fraction': 'apps.specEngine.configPanel.setting_labels.budget_warn_fraction',
  'watch.interval_s': 'apps.specEngine.configPanel.setting_labels.watch_interval_s',
  'delivery.auto_integrate':
    'apps.specEngine.configPanel.setting_labels.delivery_auto_integrate',
  'delivery.review_feedback_enabled':
    'apps.specEngine.configPanel.setting_labels.delivery_review_feedback_enabled',
  'notify.channel': 'apps.specEngine.configPanel.setting_labels.notify_channel',
  'telemetry.enabled': 'apps.specEngine.configPanel.setting_labels.telemetry_enabled',
}

/** The translated label for a registry key, or `''` for one no label names. */
function settingLabel(key: string): string {
  const catalogKey = SETTING_LABEL_KEY[key]
  return catalogKey ? i18nT(catalogKey) : ''
}

/** The refusal code behind an error, or `''` when it is not one of ours. */
function codeOf(error: unknown): string {
  return error instanceof SpecEngineApiError ? error.code : ''
}

/** A refusal block: the sentence a reader acts on, with the code underneath. */
export function Refused({ title, error }: { title: string; error: unknown }) {
  const code = codeOf(error)
  const text = error instanceof Error ? error.message : ''
  return (
    <div className="se-refusal" role="alert">
      {title}
      <code>{code ? `${code}${SEP}${text}` : text}</code>
    </div>
  )
}

/**
 * The advisories a write earned: valid configuration somebody should know about.
 *
 * `requires_acknowledgment` is rendered as its own mark because an advisory a human
 * must say "yes, I know" to is a different obligation from one they only read, and a
 * surface that showed both the same way teaches a reader to skip both.
 */
export function Advisories({ advisories }: { advisories: ConfigAdvisory[] }) {
  if (advisories.length === 0) return null
  return (
    <ul className="se-advisories">
      {advisories.map((advisory) => (
        <li key={`${advisory.code}:${advisory.path}`} data-ack={advisory.requires_acknowledgment}>
          {/* Code and path are engine identifiers an operator greps for, not copy. */}
          <span className="se-fc">{advisory.code}</span>
          <span className="se-fkind">{advisory.path}</span>
          {advisory.requires_acknowledgment && (
            <span className="se-flag" data-flag="ack">
              {i18nT('apps.specEngine.configPanel.acknowledgment_required')}
            </span>
          )}
          <span className="se-adv-text">{advisory.message}</span>
        </li>
      ))}
    </ul>
  )
}

/**
 * Which kind of declaration answered a grid cell, in words, keyed by the engine's
 * own classification.
 *
 * Keys rather than resolved strings, for `ORIGIN_KEY`'s reason. The `default` entry
 * carries the whole of the fail-closed reading: an absent declaration resolves to
 * the authoring rung, which covers no gate, so the run waits for a person. A cell
 * that showed only the word `authoring` would be indistinguishable from a rung
 * somebody chose, and only one of those two is a decision.
 */
const CELL_ORIGIN_KEY: Record<SourceGridCell['origin'], string> = {
  exact: 'apps.specEngine.sourcesSection.origin_exact',
  wildcard: 'apps.specEngine.sourcesSection.origin_wildcard',
  default: 'apps.specEngine.sourcesSection.origin_unconfigured',
}

/**
 * One resolved cell: the level, where it was declared, what it licenses, and the
 * choice made for it and not yet written.
 *
 * `cell` is optional because the axes and the matrix arrive in one payload and this
 * renders the cross product of the axes: a pair the matrix does not carry can only
 * mean the two disagree, and the honest reading of a pair with no resolution is the
 * unconfigured one — which waits for a human — rather than a blank. Such a pair is
 * not selectable for editing either: a level written for a pair the resolver did not
 * answer would be a grant nobody could read back.
 *
 * The level doubles as the button that picks the pair for the level control below the
 * table — the roles table's idiom, for its reason: a button rather than a click on the
 * cell keeps the traversal keyboard-reachable without inventing a grid pattern, and it
 * is a small fixed vocabulary of levels rather than a dropdown because a dropdown here
 * would draw a popup over the page. The strip carrying the stop is the one thing on
 * this page that must never be covered.
 *
 * The resolved level and the pending choice are BOTH shown, and they show different
 * things: the level is what the store holds, the choice is what would replace it.
 * Collapsing the two would leave a refused write displaying the submitted value as
 * though it were in force.
 */
function GridCell({
  cell,
  pending,
  selected,
  label,
  onSelect,
}: {
  cell: SourceGridCell | undefined
  /** The level chosen for this cell and not yet written, or `undefined`. */
  pending: string | undefined
  /** Whether the level control below the table is acting on this cell. */
  selected: boolean
  /** The accessible name of the cell's own button, which must name the pair. */
  label: string
  onSelect: () => void
}) {
  const origin = cell?.origin ?? 'default'
  return (
    <td data-origin={origin} data-pending={pending !== undefined}>
      {cell ? (
        <button
          type="button"
          className="se-glevel se-m se-glevelbtn"
          aria-label={label}
          aria-pressed={selected}
          onClick={onSelect}
        >
          {cell.level}
        </button>
      ) : (
        <span className="se-glevel se-m">{NONE}</span>
      )}
      {cell?.policy_covers_gates && (
        <span className="se-flag" data-flag="unattended">
          {i18nT('apps.specEngine.sourcesSection.unattended')}
        </span>
      )}
      <span className="se-note">{i18nT(CELL_ORIGIN_KEY[origin])}</span>
      {/* The declaring path, for the two origins that have one: an operator about
          to change a cell needs to know whether the level is written at this pair
          or at a broader one, because only one of those two edits is a narrowing. */}
      {cell && cell.declared_at !== '' && <span className="se-src">{cell.declared_at}</span>}
      {pending !== undefined && (
        <span className="se-note">
          <span className="se-flag" data-flag="pending">
            {i18nT('apps.specEngine.sourcesSection.not_written')}
          </span>
          {/* Engine vocabulary, rendered as the identifier it is. */}
          <span className="se-m">{pending}</span>
        </span>
      )}
    </td>
  )
}

/**
 * The level control for the cell picked in the matrix: one button per rung.
 *
 * The ladder is short and fixed, so every rung is on screen and choosing one is a
 * single act. A dropdown would be the compact alternative and is the wrong one twice
 * over: its popup is drawn over the page, and this app's layout holds because it has
 * no overlay at all.
 *
 * Choosing writes nothing. It queues the choice, which reaches the engine only through
 * the review card's confirm.
 */
function LevelChoice({
  levels,
  cell,
  pair,
  pending,
  onChoose,
}: {
  levels: readonly string[]
  cell: SourceGridCell
  pair: GridCellRef
  /** The level already chosen for the cell, or `undefined` when none is. */
  pending: string | undefined
  onChoose: (level: string) => void
}) {
  const label = i18nT('apps.specEngine.sourcesSection.level_for_pair', {
    source: pair.source,
    klass: pair.klass,
    specType: pair.specType,
  })
  return (
    <div className="se-gedit">
      <span className="se-lbl">{label}</span>
      <div className="se-acts" role="group" aria-label={label}>
        {levelOptions(levels, cell.level).map((level) => (
          <button
            key={level}
            type="button"
            className="se-btn se-sm se-m"
            // Pressed on the pending choice when there is one, otherwise on the
            // level in force: the control has to show what a write would store,
            // and after a choice that is no longer what the store holds.
            aria-pressed={level === (pending ?? cell.level)}
            onClick={() => onChoose(level)}
          >
            {level}
          </button>
        ))}
      </div>
    </div>
  )
}

/**
 * The levels a cell may be set to: the engine's ladder, plus the level in force
 * when the ladder does not contain it.
 *
 * A control that offered only the ladder would leave a hand-edited level outside the
 * vocabulary with no button pressed, so the cell in force would read as "none of
 * these" — and the operator could not tell which rung is stored from the control. The
 * out-of-vocabulary level is offered rather than hidden so what is stored is visible
 * and can be replaced with a real rung; the write door refuses the invalid one either
 * way.
 */
function levelOptions(levels: readonly string[], inForce: string): readonly string[] {
  return levels.includes(inForce) ? levels : [inForce, ...levels]
}

/** The stored cell an address names, or `undefined` if the read no longer has it. */
function cellFor(payload: SourcesPayload, pair: GridCellRef): SourceGridCell | undefined {
  const source = payload.sources.find((entry) => entry.name === pair.source)
  return source?.grid[pair.klass]?.[pair.specType]
}

/**
 * One pending edit paired with the cell it replaces, as the review reads it.
 *
 * The pair is resolved against the CURRENT answer rather than captured when the
 * choice was made, so a review sentence cannot describe a level the store stopped
 * holding while the choice sat unwritten.
 */
interface ReviewedEdit {
  edit: PendingEdit
  cell: SourceGridCell
}

/**
 * Whether *level* is further up the ladder than the cell's resolved level.
 *
 * The ladder's order is the payload's, because the engine ships it least to most
 * autonomous; a copy of that order here would be a second ranking to keep in step.
 * An unknown level on either side ranks nothing: a hand-edited document must not
 * make this read as a raise, and it must not silence a raise either — an unrankable
 * pair simply earns no claim about direction.
 */
function raisesLevel(levels: readonly string[], cell: SourceGridCell, level: string): boolean {
  const from = levels.indexOf(cell.level)
  const to = levels.indexOf(level)
  return from >= 0 && to >= 0 && to > from
}

/**
 * Which sentence describes an edit, keyed by the origin of the cell it replaces.
 *
 * Three sentences rather than one with the origin interpolated; {@link GridReview}
 * says why. Keys, not resolved strings, for `ORIGIN_KEY`'s reason.
 */
const EDIT_SENTENCE_KEY: Record<SourceGridCell['origin'], string> = {
  exact: 'apps.specEngine.sourcesSection.edit_replaces_the_pairs_own_level',
  wildcard: 'apps.specEngine.sourcesSection.edit_narrows_a_broader_rule',
  default: 'apps.specEngine.sourcesSection.edit_configures_an_unconfigured_pair',
}

/**
 * One staged change as a review card reads it: where it lands, and what it means.
 *
 * The sentence is the CALLER's, because only the caller knows what its values mean —
 * a level replacing a level, a timeout replacing a default, a source entry being
 * removed. The card owns the shape of the confirmation, never the copy inside it.
 */
interface ReviewedChange {
  /** The dotted path, for display and as the React key. Never parsed back. */
  path: string
  /** One plain-language sentence naming the old and the new state. */
  sentence: string
}

/**
 * An authority a confirm hands over, beyond the values the patch spells.
 *
 * Four kinds, because four are what a write on this pane can actually grant, and
 * each is invisible in the JSON: a level string does not say who the class catches,
 * a removed gate is an absence, a bound command is a program this app never vetted,
 * and a stage's argv is a program that will be run. A caller declares which of the
 * four its patch performs; the CARD states what each one means, so the statement
 * cannot vary between the forms that make the same grant.
 */
type ConsequenceKind =
  | 'authority'
  | 'gate_removed'
  | 'external_program'
  | 'commands_run'

/**
 * What each authority change means, in the card's own words.
 *
 * Card-owned rather than caller-supplied, which is the opposite choice from
 * {@link ReviewedChange}'s sentence and for a stated reason: a change's sentence
 * describes values only the caller knows, while a consequence describes what the
 * ENGINE does with the authority, which is the same fact whichever form granted it.
 * Two forms wording "these commands are executed" differently is two forms an
 * operator has to read separately to notice they say the same thing.
 *
 * Keys, not resolved strings, for `ORIGIN_KEY`'s reason: a module-level `i18nT()`
 * runs once at import and would freeze the table's language.
 */
const CONSEQUENCE_KEY: Record<ConsequenceKind, string> = {
  authority: 'apps.specEngine.formReview.raises_an_untrusted_class_authority',
  gate_removed: 'apps.specEngine.formReview.removes_a_gate_from_the_flow',
  external_program: 'apps.specEngine.formReview.binds_a_capability_to_an_external_program',
  commands_run: 'apps.specEngine.formReview.authorises_commands_to_run',
}

/**
 * The order the card states consequences in, most authority first.
 *
 * Fixed rather than the caller's order, so two forms granting the same pair of
 * authorities state them in the same sequence, and the widest grant is never the
 * last line of a block a reader has stopped reading.
 */
const CONSEQUENCE_ORDER: readonly ConsequenceKind[] = [
  'authority',
  'gate_removed',
  'external_program',
  'commands_run',
]

/** One authority change a patch performs, as its caller declares it. */
interface Consequence {
  /** Which of the four grants this is. Selects the card's own statement. */
  kind: ConsequenceKind
  /** The path the grant lands at. Display and React key; never parsed back. */
  path: string
  /**
   * The caller's sentence naming the subject, when it has one to name.
   *
   * Optional because the card's statement stands alone: a form that can say WHICH
   * class rises from which rung to which says so here, and a form whose subject is
   * already the change sentence above passes nothing rather than repeating it.
   */
  sentence?: string
}

/**
 * The fenced paths *patch* actually writes to, resolved against its own keys.
 *
 * The patterns are the engine's `CONFIG_ONLY_PATHS`, relayed by `GET /config` so
 * this side keeps no second copy of which sections the agent's `write_config` tool
 * refuses. A `*` segment stands for any one key, which is how
 * `projects.*.workflow` covers every project entry.
 *
 * Walked over the patch's own OBJECT keys rather than matched against a dotted
 * string, because a project key is a filesystem path and holds dots of its own: a
 * dotted rendering of `projects` + `/src/a.b` + `workflow` cannot be split back
 * into three segments, and the pattern would silently stop matching the entries it
 * exists to cover. The returned paths are dotted for display only, built from the
 * keys that actually matched.
 */
export function fencedPatchPaths(patch: Document, patterns: readonly string[]): string[] {
  const found: string[] = []
  for (const pattern of patterns) {
    const segments = pattern.split('.')
    // Breadth-first over the patch, one pattern segment per level, so a `*`
    // expands to every key present at that level rather than to a guess.
    let reached: Array<{ node: unknown; path: readonly string[] }> = [
      { node: patch, path: [] },
    ]
    for (const segment of segments) {
      const next: Array<{ node: unknown; path: readonly string[] }> = []
      for (const { node, path } of reached) {
        if (!isObject(node)) continue
        const keys = segment === '*' ? Object.keys(node) : [segment]
        for (const key of keys) {
          // `in` would answer for an inherited name; the patch's containers are
          // prototype-less, but a caller's staged object need not be.
          if (!Object.prototype.hasOwnProperty.call(node, key)) continue
          next.push({ node: node[key], path: [...path, key] })
        }
      }
      reached = next
    }
    for (const { path } of reached) found.push(dotted(path))
  }
  // Sorted so two renders of one patch state the fences in one order, and unique
  // so two patterns reaching the same path do not say it twice.
  return [...new Set(found)].sort()
}

/**
 * The copy one form's review card renders with, resolved by its caller.
 *
 * Resolved strings rather than catalog keys: every key stays a whole literal at its
 * own call site, so the key-reference gate can resolve it, and a form whose refusal
 * or confirm needs to name its own subject can say so.
 */
interface FormReviewLabels {
  /** The card's heading. */
  heading: string
  /** The confirm control. */
  confirm: string
  /** The confirm control while the write is in flight. */
  writing: string
  /** The control that drops every staged change. */
  discard: string
  /** The sentence stating a confirm sends exactly the patch above. */
  exactly: string
  /** The refusal block's title, when the write door refuses. */
  refusalTitle: string
  /** The sentence stating nothing was written, so the surface shows stored state. */
  retained: string
}

/**
 * The change a confirm would write, in plain language, with the exact patch behind
 * a disclosure.
 *
 * Shared by every form on this pane and by the autonomy grid, because the guarantee
 * is one guarantee. What leads is the summary: sentences naming each change and what
 * it does, then the authority the confirm hands over, then the patch itself one
 * disclosure away. That ordering is the whole of this card's job — approving a
 * configuration change means approving what it DOES, and a reader who meets a JSON
 * payload first is a reader approving a payload. The patch is not demoted out of
 * reach: it is exact, complete, and reachable in one activation, because approving a
 * plan still means approving what will be written.
 *
 * Four properties belong to the card rather than to any caller:
 *
 * 1. **What is submitted is what the disclosure showed.** The card renders one
 *    string and hands `onConfirm` the value PARSED BACK from that same string, so
 *    the request cannot carry a path, a key or a value the disclosure did not
 *    display. A caller cannot hold a second object to send instead, because it does
 *    not supply one at confirm time — it receives one. That is the difference
 *    between a summary that is trustworthy and a summary that authorises something
 *    nobody read.
 * 2. **A consequence is stated before the confirm control.** Four authority changes
 *    are invisible in a patch — raising an untrusted class, removing a gate, binding
 *    a capability to an external program, and authorising commands to run — so the
 *    card states each in its own words, above the button, in a fixed order.
 * 3. **A fenced path says why an operator is the one confirming.** The paths the
 *    engine reserves to an operator-confirmed surface are read from `GET /config`'s
 *    relay of its own `CONFIG_ONLY_PATHS`, never from a copy on this side, and a
 *    patch touching one says so: the agent's write tool refuses these, which is why
 *    this confirmation is the only way they can be written.
 * 4. **A refusal retains stored state.** The engine's reason is rendered by the path
 *    it names, the staged changes stay put to be corrected, and NOTHING is
 *    invalidated — so the surface behind the card keeps stating what is persisted
 *    rather than what was submitted.
 *
 * And one that belongs to the layout: a consequence goes in flow, never in a dialog,
 * for the same reason the removal confirmation is a sibling block — a consequence
 * stated in an overlay is one that can be dismissed, and the strip carrying the kill
 * switch must never be covered. The disclosure is a `<details>` for the same reason:
 * it expands in place and draws nothing over the page.
 */
export function FormReview({
  changes,
  patch,
  labels,
  authorises,
  consequences,
  writing,
  error,
  onConfirm,
  onDiscard,
}: {
  changes: readonly ReviewedChange[]
  patch: Document
  labels: FormReviewLabels
  /** The authority this patch hands over, declared by kind. */
  authorises?: readonly Consequence[]
  /** Statements the patch cannot carry, rendered with the declared ones. */
  consequences?: React.ReactNode
  writing: boolean
  error: unknown
  /** Called with the patch the disclosure showed, which is the one to send. */
  onConfirm: (patch: Document) => void
  onDiscard: () => void
}) {
  // The engine's own list of the paths it fences to an operator-confirmed surface,
  // read here rather than threaded through every form: each form that ever composes
  // a patch would otherwise be one more place the list can be forgotten, and this
  // shares the page's cache entry for the same key rather than adding a read.
  const config = useQuery({
    queryKey: QK.config,
    queryFn: () => specEngineApi.config(),
    retry: false,
  })
  // `isError` before the data, the pane's rule everywhere: React Query keeps the
  // last successful body across a failing refetch, and a fence claimed on a read
  // that did not happen is a claim about the engine's policy made up by this side.
  // Absent marks are the safe direction — an unmarked fenced path loses one
  // explanatory line, while a marked unfenced one asserts a refusal that is not real.
  const fenced = useMemo(
    () =>
      config.isError || !config.data
        ? []
        : fencedPatchPaths(patch, config.data.config_only_paths),
    [config.isError, config.data, patch],
  )
  // Rendered once and handed back on confirm, so the two cannot be computed
  // separately. See property 1 above: this is the whole mechanism behind it.
  const shown = useMemo(() => JSON.stringify(patch, null, 2), [patch])
  const declared = useMemo(() => {
    const listed = authorises ?? []
    return CONSEQUENCE_ORDER.flatMap((kind) => {
      const matching = listed.filter((entry) => entry.kind === kind)
      return matching.length === 0 ? [] : [{ kind, entries: matching }]
    })
  }, [authorises])
  return (
    <div className="se-qbox">
      <h3>{labels.heading}</h3>
      {/* Plain language leads. Each sentence says what one line of the patch MEANS,
          which the JSON cannot, and the JSON is one disclosure below. */}
      {changes.map((change) => (
        <p className="se-note" key={change.path}>
          {change.sentence}
        </p>
      ))}
      {fenced.map((path) => (
        <p className="se-note" key={`fenced:${path}`}>
          {i18nT('apps.specEngine.formReview.only_an_operator_confirmation_writes_this', {
            path,
          })}
        </p>
      ))}
      {(declared.length > 0 || consequences) && (
        /* In flow above the confirm, never a dialog: the same rule the removal
           confirmation follows, and for the same reason. */
        <div className="se-arm">
          {declared.map(({ kind, entries }) => (
            <Fragment key={kind}>
              {entries.map((entry) =>
                entry.sentence === undefined ? null : (
                  <p key={`${kind}:${entry.path}`}>
                    <AlertTriangle className="lucide-inline" aria-hidden="true" />
                    {entry.sentence}
                  </p>
                ),
              )}
              <p>
                <AlertTriangle className="lucide-inline" aria-hidden="true" />
                {i18nT(CONSEQUENCE_KEY[kind])}
              </p>
            </Fragment>
          ))}
          {consequences}
        </div>
      )}
      <details className="se-disc">
        <summary>{i18nT('apps.specEngine.formReview.show_the_exact_patch')}</summary>
        {/* The payload itself, pretty-printed. Not a rendering of it, and not a
            second derivation for display: the confirm sends this very text parsed
            back, so a summary an operator approves cannot differ from the write. */}
        <pre className="se-json se-gpatch">{shown}</pre>
      </details>
      <p className="se-note">{labels.exactly}</p>
      <div className="se-acts" style={{ marginTop: 9 }}>
        <button
          type="button"
          className="se-btn se-danger"
          disabled={writing}
          onClick={() => onConfirm(JSON.parse(shown) as Document)}
        >
          {writing ? labels.writing : labels.confirm}
        </button>
        <button type="button" className="se-btn" disabled={writing} onClick={onDiscard}>
          {labels.discard}
        </button>
      </div>
      {/* Loose inequality on purpose: a caller handing over `mutation.error`
          before any failure passes `undefined`, and a strict null check would
          render an empty refusal beside "nothing was written" for a write that
          was never attempted. */}
      {error != null && (
        <>
          <Refused title={labels.refusalTitle} error={error} />
          {/* The refusal alone would leave open which of the two states the page is
              in. Nothing was written, so the surface above is still the store's, and
              the staged changes are still here to be corrected and sent again. */}
          <p className="se-note">{labels.retained}</p>
        </>
      )}
    </div>
  )
}

/**
 * The grid's own reading of {@link FormReview}: the sentences and the consequences.
 *
 * Which sentence describes an edit depends on the origin of the cell it replaces —
 * three sentences rather than one with the origin interpolated, because the three
 * edits are three different acts: replacing a level somebody chose for this pair,
 * NARROWING a broader rule that also answers other pairs, and configuring a pair
 * nothing had answered. The wildcard sentence carries the narrowing statement itself
 * — the write creates this pair's own cell and leaves the broader rule alone —
 * because a separate line repeating the same pair is a line a reader skips.
 *
 * One consequence gets its own statement because it is not legible in the patch:
 * raising the least-trusted class. That class is where an author the engine cannot
 * classify lands, so a rung granted there is a rung granted to anyone at all, and
 * nothing in the JSON says which class that is. It is declared as the card's
 * `authority` kind rather than rendered here, so this grant and the same grant made
 * from any other form read as one act; the per-edit sentence naming the class and
 * both rungs travels with it, because only this surface knows those.
 */
function GridReview({
  reviewed,
  patch,
  levels,
  leastTrusted,
  writing,
  error,
  onConfirm,
  onDiscard,
}: {
  reviewed: readonly ReviewedEdit[]
  patch: Document
  levels: readonly string[]
  leastTrusted: string
  writing: boolean
  error: unknown
  /** Called with the patch the card showed, which is the one to send. */
  onConfirm: (patch: Document) => void
  onDiscard: () => void
}) {
  const raising = reviewed.filter(
    ({ edit, cell }) => edit.klass === leastTrusted && raisesLevel(levels, cell, edit.level),
  )
  return (
    <FormReview
      changes={reviewed.map(({ edit, cell }) => ({
        path: dotted(gridCellSegments(edit)),
        sentence: i18nT(EDIT_SENTENCE_KEY[cell.origin], {
          source: edit.source,
          klass: edit.klass,
          specType: edit.specType,
          oldLevel: cell.level,
          newLevel: edit.level,
          declaredAt: cell.declared_at,
          path: dotted(gridCellSegments(edit)),
        }),
      }))}
      patch={patch}
      labels={{
        heading: i18nT('apps.specEngine.sourcesSection.the_change_that_would_be_written'),
        confirm: i18nT('apps.specEngine.sourcesSection.write_the_change'),
        writing: i18nT('apps.specEngine.configPanel.saving'),
        discard: i18nT('apps.specEngine.sourcesSection.discard_the_pending_changes'),
        exactly: i18nT('apps.specEngine.sourcesSection.a_confirm_writes_exactly_this_patch'),
        refusalTitle: i18nT('apps.specEngine.sourcesSection.could_not_write_the_grid_change'),
        retained: i18nT(
          'apps.specEngine.sourcesSection.nothing_was_written_so_the_matrix_is_stored_state',
        ),
      }}
      authorises={raising.map(({ edit, cell }) => ({
        kind: 'authority' as const,
        path: dotted(gridCellSegments(edit)),
        // The subject sentence: which class, which spec type, and both rungs. The
        // card adds what raising an untrusted class's authority MEANS.
        sentence: i18nT('apps.specEngine.sourcesSection.this_raises_the_least_trusted_class', {
          klass: edit.klass,
          specType: edit.specType,
          oldLevel: cell.level,
          newLevel: edit.level,
        }),
      }))}
      writing={writing}
      error={error}
      onConfirm={onConfirm}
      onDiscard={onDiscard}
    />
  )
}

/**
 * The autonomy grid of every Watch_Source: who may run how unattended.
 *
 * A read of the engine's resolver, and a guarded edit path over it. Each cell is the
 * level `AutonomyPolicy` answered for that (submitter class, spec type) pair, with
 * the origin that answered it — so the matrix here and the decision a gate makes
 * come from one code path rather than from two implementations of class-first
 * precedence.
 *
 * Three properties of the rendering are load-bearing:
 *
 * 1. **The axes are the payload's.** Rows and columns are the vocabularies the
 *    engine shipped, in its order, so a class or spec type added to the schema
 *    appears here without a frontend edit — and this surface cannot offer an axis
 *    the resolver has no answer for.
 * 2. **A failed read renders no values.** React Query keeps the last successful
 *    answer across a failing refetch, so `isError` is read BEFORE the data. A matrix
 *    rendered from a retained answer would state who may run unattended on the
 *    strength of a read that did not happen.
 * 3. **An unconfigured cell is a statement, not a blank.** The unconfigured default
 *    is the authoring rung, which covers no gate; the cell says the run waits for a
 *    human rather than leaving the reader to infer it from an empty box.
 *
 * And three of the editing:
 *
 * 4. **A choice is not a write.** Level selects accumulate in local state and reach
 *    the engine only through a confirm on a review card that shows the exact patch.
 *    Nothing on this surface writes on change.
 * 5. **The matrix always shows the store.** A cell's level and origin come from the
 *    read; the pending choice sits in the select beside them, marked as unwritten.
 *    So a refused write leaves the grid stating what is persisted rather than what
 *    was submitted, and no invalidation happens on failure.
 * 6. **A success re-reads.** The sources, document and resolved queries are
 *    invalidated and the matrix re-renders from the fresh answer, never from the
 *    values just sent — the merged document the write returns is not adopted.
 *
 * The semantics sit once in the section rather than per cell: they are properties of
 * the resolution, not of any one pair, and twelve copies of a sentence teach a
 * reader to stop reading it.
 *
 * The shown source is the PANE's, not this section's: the source form above links
 * into this matrix for the source it is editing, and a selection held here as well
 * would let the link name one source while the grid rendered another.
 */
export function SourcesSection({
  chosen,
  onChoose,
}: {
  /** The source the pane has selected, `''` when it has selected none. */
  chosen: string
  onChoose: (source: string) => void
}) {
  const client = useQueryClient()
  // The operator's choices, keyed by cell rather than by screen position, so
  // switching the shown source does not lose them and cannot silently move one.
  const [edits, setEdits] = useState<readonly PendingEdit[]>([])
  // The cell the level control acts on. A cell, not a level: the level control is
  // shared, and a copy of the choice on either side is how the two come to disagree.
  const [picked, setPicked] = useState<GridCellRef | null>(null)
  const [reviewing, setReviewing] = useState(false)
  const [wrote, setWrote] = useState(false)
  const sources = useQuery({
    queryKey: QK.sources,
    queryFn: () => specEngineApi.sources(),
    retry: false,
  })

  const write = useMutation({
    // The engine's one write door, the same one a document save and a project
    // removal go through: merged, schema-validated against the MERGED document,
    // locked, and recorded. There is no grid-specific write path to review.
    mutationFn: (patch: Document) => specEngineApi.writeConfig(patch),
    onSuccess: () => {
      setEdits([])
      setReviewing(false)
      setWrote(true)
      // The reply's merged document is NOT adopted: the read is this pane's
      // authority on what is persisted, and the grid is a resolution OF that
      // document rather than a copy of the patch. `QK.sources` and the resolved
      // keys sit under `QK.config`'s prefix, so the document invalidation already
      // reaches them; all three are named because they are three readings a
      // reader would otherwise have to know the key layout to see refreshed.
      void client.invalidateQueries({ queryKey: QK.sources })
      void client.invalidateQueries({ queryKey: QK.config })
      void client.invalidateQueries({ queryKey: QK_RESOLVED_ROOT })
    },
    // No `onError`: a refusal must leave the choices in place and the queries
    // untouched, so the matrix keeps showing the store's own state.
  })

  const payload = sources.data
  const names = useMemo(
    () => (payload ? payload.sources.map((source) => source.name) : []),
    [payload],
  )

  // A choice the current answer cannot resolve is dropped rather than carried, so a
  // cell marked pending is always a cell the review accounts for and the patch
  // writes. Two ways a choice stops resolving, both handled here:
  //
  // A source that has left the document would be RE-CREATED by the patch: it writes
  // `sources.<name>.autonomy.<class>.<type>`, and the merge would resurrect a source
  // entry carrying an autonomy grid and none of the fields that make it a source.
  //
  // A pair the answer no longer resolves under a source it still lists cannot be
  // reviewed — the review needs the level being replaced — so keeping the choice
  // would leave a "not written" mark on a cell no confirm could ever clear.
  //
  // Either removal can arrive from the editor below, from another surface, or on any
  // refetch, which is why this reconciles against the answer instead of trusting the
  // document to hold still between a choice and its confirm.
  useEffect(() => {
    if (!payload) return
    setEdits((current) => {
      const kept = current.filter((edit) => cellFor(payload, edit) !== undefined)
      return kept.length === current.length ? current : kept
    })
  }, [payload])

  // Plain functions rather than `useCallback`: both close over the mutation object,
  // which React Query hands back fresh on every render, so a memo here would
  // advertise a stability it cannot have.
  const choose = (pair: GridCellRef, level: string, stored: SourceGridCell) => {
    setWrote(false)
    write.reset()
    const edit: PendingEdit = { ...pair, level }
    setEdits((current) => {
      const index = current.findIndex((other) => sameCell(other, edit))
      // Choosing the level the pair's OWN cell already stores is not a change, so
      // it withdraws the pending one instead of queueing a write that would record
      // an edit nobody made. For a wildcard-answered or unconfigured pair the same
      // level IS a change: it pins the pair, which is what keeps it where it is
      // when the broader rule moves.
      if (stored.origin === 'exact' && stored.level === level) {
        return index < 0 ? current : [...current.slice(0, index), ...current.slice(index + 1)]
      }
      if (index < 0) return [...current, edit]
      // Replaced in place: the review reads in the order the choices were made,
      // and re-choosing one cell must not reorder the account of the others.
      const next = [...current]
      next[index] = edit
      return next
    })
  }

  const discard = () => {
    setEdits([])
    setReviewing(false)
    setWrote(false)
    write.reset()
  }

  // `isError` first, then the data: see property 2 above.
  if (sources.isError) {
    return (
      <div className="se-blk" id={SOURCES_GRID_ID}>
        <h3>{i18nT('apps.specEngine.sourcesSection.watch_sources')}</h3>
        <Refused
          title={i18nT('apps.specEngine.sourcesSection.could_not_read_the_watch_sources')}
          error={sources.error}
        />
      </div>
    )
  }
  if (sources.isPending || !payload) {
    // Distinct from the empty state on purpose: "nothing is configured" is a fact
    // about the document, and "not read yet" is a fact about this request.
    return (
      <div className="se-blk" id={SOURCES_GRID_ID}>
        <h3>{i18nT('apps.specEngine.sourcesSection.watch_sources')}</h3>
        <p className="se-note">{i18nT('apps.specEngine.sourcesSection.reading_the_watch_sources')}</p>
      </div>
    )
  }

  // Normalized against the answer rather than trusted: a source removed by another
  // surface (or in the editor below) must not leave the section rendering a matrix
  // under a name the document no longer lists.
  const selected = names.includes(chosen) ? chosen : (names[0] ?? '')
  const source = payload.sources.find((entry) => entry.name === selected)
  const classes = payload.submitter_classes
  // The schema orders the classes most to least trusted, so the last is the one an
  // author who cannot be classified falls to. Read from the payload rather than
  // spelled here, so the sentence names whatever class the engine puts last.
  const leastTrusted = classes.length > 0 ? classes[classes.length - 1] : ''
  // The choices the current answer can still account for, and the patch built from
  // exactly those. One list for both, because a review that showed a patch line it
  // could not explain — or a write that carried one — is the failure this card
  // exists to prevent.
  const reviewed: ReviewedEdit[] = []
  for (const edit of edits) {
    const cell = cellFor(payload, edit)
    if (cell) reviewed.push({ edit, cell })
  }
  const patch = buildGridPatch(reviewed.map((entry) => entry.edit))
  // The picked cell, resolved against the current answer rather than trusted: a pick
  // whose source or pair the document no longer carries would otherwise leave a level
  // control on screen acting on a cell nothing resolves, and a choice made there
  // would be a grant for a pair the engine has no answer for.
  const pickedShown = picked !== null && picked.source === selected ? picked : null
  const pickedResolved = pickedShown === null ? undefined : cellFor(payload, pickedShown)
  const pickedCell =
    pickedShown !== null && pickedResolved ? { pair: pickedShown, cell: pickedResolved } : null

  return (
    <div className="se-blk" id={SOURCES_GRID_ID}>
      <h3>{i18nT('apps.specEngine.sourcesSection.watch_sources')}</h3>
      {names.length === 0 ? (
        /* Not an empty matrix: a grid with no source to belong to reads as "no
           authority is granted", when the fact is that nothing is being ingested at
           all. The copy names the source form above, which is where a source is
           created — this section edits grids and never creates one. */
        <p className="se-note">
          {i18nT('apps.specEngine.sourcesSection.no_watch_source_is_configured')}
        </p>
      ) : (
        <>
          <div
            className="se-acts"
            role="group"
            aria-label={i18nT('apps.specEngine.sourcesSection.select_a_watch_source')}
          >
            {names.map((name) => (
              <button
                key={name}
                type="button"
                className="se-btn se-sm se-m"
                aria-pressed={name === selected}
                onClick={() => onChoose(name)}
              >
                {name}
              </button>
            ))}
          </div>
          <table
            className="se-grid"
            aria-label={i18nT('apps.specEngine.sourcesSection.autonomy_for_source', {
              source: selected,
            })}
          >
            <thead>
              <tr>
                <th>{i18nT('apps.specEngine.sourcesSection.col_submitter_class')}</th>
                {payload.spec_types.map((specType) => (
                  /* Engine vocabulary, rendered as the identifier it is: a
                     translated axis would name a spec type no document holds. */
                  <th key={specType} className="se-m">
                    {specType}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {classes.map((klass) => (
                <tr key={klass}>
                  <th scope="row" className="se-m">
                    {klass}
                  </th>
                  {payload.spec_types.map((specType) => {
                    const pair = { source: selected, klass, specType }
                    const cell = source?.grid[klass]?.[specType]
                    return (
                      <GridCell
                        key={specType}
                        cell={cell}
                        pending={edits.find((edit) => sameCell(edit, pair))?.level}
                        selected={picked !== null && sameCell(picked, pair)}
                        // The pair, in the accessible name: twelve buttons reading
                        // only their level are twelve announcements of a word, and
                        // the pair is the whole identity of the decision.
                        label={i18nT('apps.specEngine.sourcesSection.change_the_level_for_pair', {
                          source: selected,
                          klass,
                          specType,
                        })}
                        onSelect={() => setPicked(pair)}
                      />
                    )
                  })}
                </tr>
              ))}
            </tbody>
          </table>
          {pickedCell ? (
            <LevelChoice
              levels={payload.levels}
              cell={pickedCell.cell}
              pair={pickedCell.pair}
              pending={edits.find((edit) => sameCell(edit, pickedCell.pair))?.level}
              onChoose={(level) => choose(pickedCell.pair, level, pickedCell.cell)}
            />
          ) : (
            <p className="se-note">
              {i18nT('apps.specEngine.sourcesSection.choose_a_cell_to_change_its_level')}
            </p>
          )}
          <div className="se-acts" style={{ marginTop: 9 }}>
            <button
              type="button"
              className="se-btn"
              disabled={reviewed.length === 0}
              onClick={() => setReviewing(true)}
            >
              {i18nT('apps.specEngine.sourcesSection.review_the_exact_change')}
            </button>
            {reviewed.length > 0 && (
              <span className="se-lbl">
                {i18nT('apps.specEngine.sourcesSection.unwritten_cell_changes')}
                {SEP}
                <span className="se-m">{fmtNumber(reviewed.length)}</span>
              </span>
            )}
          </div>
          {reviewing && reviewed.length > 0 && (
            <GridReview
              reviewed={reviewed}
              patch={patch}
              levels={payload.levels}
              leastTrusted={leastTrusted}
              writing={write.isPending}
              error={write.isError ? write.error : null}
              onConfirm={(sending) => write.mutate(sending)}
              onDiscard={discard}
            />
          )}
          {wrote && (
            <p className="se-note" role="status">
              {i18nT('apps.specEngine.sourcesSection.wrote_the_change_and_re_read_the_matrix')}
            </p>
          )}
        </>
      )}
      {/* The semantics, beside the values they govern. Each of these is a rule an
          operator would otherwise have to infer from a matrix, and inferring the
          wrong one means granting authority nobody meant to grant. */}
      {leastTrusted !== '' && (
        <p className="se-note">
          {i18nT('apps.specEngine.sourcesSection.an_unclassifiable_author_is_least_trusted', {
            klass: leastTrusted,
          })}
        </p>
      )}
      <p className="se-note">
        {i18nT('apps.specEngine.sourcesSection.a_level_authorizes_every_level_below_it')}
      </p>
      <p className="se-note">
        {i18nT('apps.specEngine.sourcesSection.screening_caps_a_flagged_item_to_authoring')}
      </p>
      <p className="se-note">
        {i18nT('apps.specEngine.sourcesSection.execution_or_above_needs_no_human_at_a_gate')}
      </p>
      <p className="se-note">
        {i18nT('apps.specEngine.sourcesSection.the_matrix_is_the_engines_own_resolution')}
      </p>
      {/* Stated where the edit is made, because the alternative reading is the
          natural one: an operator changing a cell a broader rule answered would
          otherwise expect that rule to move. */}
      <p className="se-note">
        {i18nT('apps.specEngine.sourcesSection.an_edit_writes_the_pairs_own_cell')}
      </p>
    </div>
  )
}

// --- the settings form, generated from the engine's registry ------------------

/**
 * Which control edits a registry setting, keyed by the type NAME the registry
 * projects.
 *
 * A table rather than a chain of comparisons, because the interesting case is the
 * one that is NOT in it: a kind this form has no control for resolves to
 * `undefined` and renders the read-only fallback, so a type the engine's registry
 * gains shows its value and routes to the JSON view instead of crashing the pane
 * or silently disappearing from it.
 *
 * A `str` setting is free text, and that is safe for exactly one reason worth
 * writing down: the registry's `choices` are not part of the projection because no
 * shipped setting declares any. A setting that ever does needs the vocabulary and
 * a closed-choice control added in one change — free text against an enforced set
 * would offer values the write door refuses.
 */
const CONTROL_BY_KIND: Record<string, string> = {
  int: 'number',
  float: 'number',
  bool: 'checkbox',
  str: 'text',
}

/**
 * The granularity a numeric control steps in, by kind.
 *
 * Whole counts for `int` and any fraction for `float`: the engine refuses a
 * fractional value for an int setting, so a control that stepped one by halves
 * would hand the operator a value the write door then rejects.
 */
const STEP_BY_KIND: Record<string, string> = { int: '1', float: 'any' }

/** The registry vocabulary before the read answers. One constant, so a memo over
 *  it does not see a fresh array on every render. */
const NO_SETTINGS: readonly RegistrySetting[] = []

/** A value as a row shows it, with the stand-in for one the read has not got. */
function shownValue(value: unknown): string {
  return value === undefined ? NONE : settingValue(value)
}

/** Where a value in force was decided, in words, or the stand-in for nowhere. */
function originText(inForce: EffectiveSetting | undefined): string {
  // Indexed at the call site rather than through a local, so the key-reference
  // gate resolves every entry in the map. The guard keeps the lookup total: an
  // origin the payload gains before this table does earns the stand-in rather
  // than an untranslated key.
  if (!inForce || !ORIGIN_KEY[inForce.origin]) return NONE
  return i18nT(ORIGIN_KEY[inForce.origin])
}

/**
 * Whether *value* is what the document already stores AT *segments*.
 *
 * Both sides are composed the same way and neither is split, so the comparison is
 * exact even for a project or source whose name holds a dot: the engine renders a
 * stored setting's path as `<section>.<name>.<group>.<leaf>` and
 * {@link settingSegments} builds those same segments. For one fixed registry key,
 * two different names cannot render to one string.
 *
 * The declaring path is part of the question rather than the value alone, because
 * the same value at a DIFFERENT path is a real change: it pins the setting where
 * the layer that currently answers it can no longer move it.
 */
function storedAt(
  inForce: EffectiveSetting | undefined,
  segments: readonly string[],
  value: unknown,
): boolean {
  if (!inForce || inForce.declared_at !== dotted(segments)) return false
  return JSON.stringify(inForce.value ?? null) === JSON.stringify(value ?? null)
}

/** One scope a setting may be written at, with the path it would write. */
interface ScopeOffer {
  /** The scope name, in the registry's own vocabulary. */
  scope: string
  /** The path a write at this scope targets, or `null` when it has none. */
  segments: readonly string[] | null
}

/**
 * One generated row: the registry entry, everything resolved for it, and where a
 * write would land.
 *
 * Built by {@link settingFields} rather than assembled in the render, so the
 * mapping from a vocabulary to a form is a pure function a property can be stated
 * over — the claim being that the form is TOTAL over the registry, which is not a
 * claim about any one row.
 */
export interface SettingField {
  setting: RegistrySetting
  /** The value in force and its origin, or `undefined` when the read has none. */
  inForce: EffectiveSetting | undefined
  /** The scope a write targets. `''` when no permitted scope has a path. */
  scope: string
  /** The path the chosen scope writes, or `null` when there is none. */
  segments: readonly string[] | null
  /** Every scope the REGISTRY permits, writable or not. */
  offers: readonly ScopeOffer[]
}

/** The names a project- or source-scoped write targets. */
interface ScopeTargets {
  /** The project selected on the pane, `''` when none is. */
  project: string
  /** The watch source the form writes source-scoped values into, `''` for none. */
  source: string
}

/** The target name *scope* writes into, `''` when the scope names none. */
function scopeTarget(scope: string, targets: ScopeTargets): string {
  if (scope === SCOPE_PROJECT) return targets.project
  if (scope === SCOPE_SOURCE) return targets.source
  return ''
}

/**
 * One row per registry setting, in the order the registry supplies them.
 *
 * Total by construction: every setting produces exactly one field whatever its
 * kind, and a kind with no control is a field whose row renders read-only rather
 * than a setting that vanishes. The scope offered is normalized against what is
 * writable NOW — a scope held in state stops being writable when its target is
 * deselected, and a chooser still pointing at it would compose
 * `projects..limits.x`, a write into a project named the empty string.
 */
export function settingFields(
  settings: readonly RegistrySetting[],
  inForce: ReadonlyMap<string, EffectiveSetting>,
  targets: ScopeTargets,
  chosen: Readonly<Record<string, string>>,
): SettingField[] {
  return settings.map((setting) => {
    const offers: ScopeOffer[] = setting.scopes.map((scope) => ({
      scope,
      segments: settingSegments(setting.key, scope, scopeTarget(scope, targets)),
    }))
    const writable = offers.filter((offer) => offer.segments !== null)
    const held = chosen[setting.key]
    const offer = writable.find((other) => other.scope === held) ?? writable[0]
    return {
      setting,
      inForce: inForce.get(setting.key),
      scope: offer?.scope ?? '',
      segments: offer?.segments ?? null,
      offers,
    }
  })
}

/**
 * The one control a registry kind is edited with.
 *
 * Shared by the settings form and by a cost profile's pinned limits, because
 * "which control edits an int, and with which bounds" is a property of the
 * REGISTRY rather than of either form: two copies of it would be two chances to
 * offer a value the write door then refuses, and only one of them would be found
 * when the registry gains a kind.
 *
 * The value shown is the caller's — the staged one when there is one, otherwise
 * what is stored — because only the caller knows which of the two it is holding.
 * Nothing here writes: a change stages, and staging reaches the store only
 * through the review card's confirm.
 */
function SettingControl({
  id,
  setting,
  control,
  value,
  disabled,
  onStage,
  onWithdraw,
}: {
  id: string
  setting: RegistrySetting
  /** The control kind, resolved from {@link CONTROL_BY_KIND} by the caller. */
  control: string
  value: unknown
  disabled: boolean
  onStage: (value: unknown) => void
  onWithdraw: () => void
}) {
  const numeric = control === 'number'
  const twoState = control === 'checkbox'
  return (
    <input
      id={id}
      type={control}
      className={twoState ? 'se-check' : 'se-input'}
      disabled={disabled}
      // The registry's bounds, carried by the control itself rather than
      // restated: the engine refuses an out-of-range value by path either way,
      // and a second copy of a bound here is one that can drift from it.
      min={numeric && setting.minimum !== null ? setting.minimum : undefined}
      max={numeric && setting.maximum !== null ? setting.maximum : undefined}
      step={numeric ? STEP_BY_KIND[setting.kind] : undefined}
      checked={twoState ? value === true : undefined}
      value={
        twoState ? undefined : typeof value === 'number' || typeof value === 'string'
          ? String(value)
          : ''
      }
      onChange={(event) => {
        if (twoState) {
          onStage(event.target.checked)
          return
        }
        const raw = event.target.value
        // A number control reports an entry it cannot parse as the empty
        // string, and an empty one has no value to stage: the edit is withdrawn
        // rather than written as some number the operator did not type.
        if (numeric && raw.trim() === '') {
          onWithdraw()
          return
        }
        onStage(numeric ? Number(raw) : raw)
      }}
    />
  )
}

/**
 * One setting: its meaning, the value in force, the control, and the scope a
 * write would land at.
 *
 * Four things are on the row and none of them is decoration:
 *
 * 1. **The label leads and the registry key follows as the detail line.** The key
 *    is what the document and the write log speak, so it stays on screen, but a
 *    reader should not have to think in registry keys to change a timeout. A key
 *    no label names renders as itself — the vocabulary is the engine's, and a
 *    setting it adds must appear here without a frontend edit.
 * 2. **The registry's own summary is the help text.** It is the sentence the
 *    engine wrote about the setting; a second sentence maintained here would be a
 *    second description to drift.
 * 3. **The value in force and its ORIGIN.** A control showing `2` cannot tell an
 *    operator whether somebody chose 2 or the app ships 2, and those call for
 *    opposite actions.
 * 4. **A staged edit is marked as unwritten, beside the value still in force.**
 *    Collapsing the two would leave a refused write displaying the submitted value
 *    as though it were stored.
 *
 * The scope chooser is a button group rather than a dropdown, for the level
 * control's reason: the vocabulary is tiny and fixed, and a popup would be drawn
 * over a page whose safety strip must never be covered. A scope the registry
 * permits but this form cannot address is shown DISABLED rather than hidden, so
 * the row does not quietly deny an override the engine accepts.
 */
function SettingRow({
  field,
  staged,
  onScope,
  onStage,
  onWithdraw,
}: {
  field: SettingField
  /** The edit staged at this row's path, or `undefined` when none is. */
  staged: StagedEdit | undefined
  onScope: (scope: string) => void
  onStage: (value: unknown) => void
  onWithdraw: () => void
}) {
  const id = useId()
  const { setting, inForce, scope, segments, offers } = field
  const control = CONTROL_BY_KIND[setting.kind]
  const label = settingLabel(setting.key)
  const named = label ? (
    <>
      {label}
      <span className="se-kv-path">{setting.key}</span>
    </>
  ) : (
    <span className="se-m">{setting.key}</span>
  )
  const value = staged ? staged.value : inForce?.value
  return (
    <div className="se-setting" data-kind={setting.kind} data-staged={staged !== undefined}>
      {control === undefined ? (
        // No control to name, so no label element: a `for` pointing at nothing
        // announces a form field that is not there.
        <span className="se-setting-name">{named}</span>
      ) : (
        <label className="se-setting-name" htmlFor={id}>
          {named}
        </label>
      )}
      {control === undefined ? (
        // The value is NOT repeated here: the in-force line below already states it
        // with the origin that decided it, which is the honest reading of a row
        // this form can only read.
        <p className="se-note">
          {i18nT('apps.specEngine.settingsForm.the_registry_kind_is_not_editable_here', {
            kind: setting.kind,
          })}
        </p>
      ) : (
        <SettingControl
          id={id}
          setting={setting}
          control={control}
          value={value}
          // A row whose chosen scope has no path can be read but not written.
          disabled={segments === null}
          onStage={onStage}
          onWithdraw={onWithdraw}
        />
      )}
      <p className="se-note">{setting.summary}</p>
      <p className="se-note">
        {i18nT('apps.specEngine.settingsForm.in_force')}
        {SEP}
        <span className="se-m">{shownValue(inForce?.value)}</span>
        {SEP}
        {/* Its own element, because the origin is the half of this line a reader
            acts on: a value of 2 somebody chose and a 2 the app ships call for
            opposite actions, and only the origin distinguishes them. */}
        <span>{originText(inForce)}</span>
        {inForce && inForce.declared_at !== '' && (
          <span className="se-src">
            {SEP}
            {inForce.declared_at}
          </span>
        )}
      </p>
      {control !== undefined && offers.length > 0 && (
        <div
          className="se-acts"
          role="group"
          aria-label={i18nT('apps.specEngine.settingsForm.scope_to_write_setting_at', {
            setting: setting.key,
          })}
        >
          {offers.map((offer) => (
            <button
              key={offer.scope}
              type="button"
              className="se-btn se-sm se-m"
              aria-pressed={offer.scope === scope}
              disabled={offer.segments === null}
              // The path a write at this scope lands at, or why there is none. An
              // operator choosing between app and project scope is choosing a blast
              // radius, and the path is what states it.
              title={
                offer.segments === null
                  ? i18nT('apps.specEngine.settingsForm.this_scope_cannot_be_written_here')
                  : dotted(offer.segments)
              }
              onClick={() => onScope(offer.scope)}
            >
              {offer.scope}
            </button>
          ))}
        </div>
      )}
      {staged !== undefined && segments !== null && (
        <p className="se-note">
          <span className="se-flag" data-flag="pending">
            {i18nT('apps.specEngine.settingsForm.not_written')}
          </span>
          <span className="se-m">
            {shownValue(staged.value)}
            {SEP}
            {dotted(segments)}
          </span>
        </p>
      )}
    </div>
  )
}

/**
 * Every generated row.
 *
 * Presentational and exported for one reason: the property that the form is total
 * over the registry is a property of the RENDER, so it has to be stated over a
 * generated vocabulary rendered synchronously rather than over a pane waiting on
 * three reads.
 */
export function SettingsFields({
  fields,
  stagedAt,
  onScope,
  onStage,
  onWithdraw,
}: {
  fields: readonly SettingField[]
  stagedAt: (segments: readonly string[]) => StagedEdit | undefined
  onScope: (field: SettingField, scope: string) => void
  onStage: (field: SettingField, value: unknown) => void
  onWithdraw: (segments: readonly string[]) => void
}) {
  return (
    <div className="se-settings">
      {fields.map((field) => (
        <SettingRow
          key={field.setting.key}
          field={field}
          staged={field.segments ? stagedAt(field.segments) : undefined}
          onScope={(scope) => onScope(field, scope)}
          onStage={(value) => onStage(field, value)}
          onWithdraw={() => {
            if (field.segments) onWithdraw(field.segments)
          }}
        />
      ))}
    </div>
  )
}

/**
 * The leading dot-segment of a registry key — the engine's own group.
 *
 * `Setting.group` on the engine's side is everything before the FIRST dot, so this
 * is that split and nothing else: `limits.task_retry_limit` groups under `limits`,
 * and a key with no dot is its own whole group rather than being dropped. Total by
 * construction, which is what lets {@link settingGroups} account for every field.
 */
function settingGroup(key: string): string {
  const dot = key.indexOf('.')
  return dot < 0 ? key : key.slice(0, dot)
}

/**
 * The generated fields partitioned by their registry group, in first-appearance
 * order.
 *
 * Pure and exported because the claim is about the PARTITION, not the render: every
 * field lands in exactly one group, no field is dropped or duplicated, and the
 * group order is the order the groups first appear in the input — the same
 * generated-not-hard-coded rule the fields themselves follow, so a group the engine
 * adds to its registry gets its own subsection with no edit here. A property states
 * that over generated vocabularies; a hard-coded group list would fail it.
 */
export function settingGroups(
  fields: readonly SettingField[],
): Array<{ group: string; fields: SettingField[] }> {
  const order: string[] = []
  const byGroup = new Map<string, SettingField[]>()
  for (const field of fields) {
    const group = settingGroup(field.setting.key)
    const bucket = byGroup.get(group)
    if (bucket) {
      bucket.push(field)
    } else {
      byGroup.set(group, [field])
      order.push(group)
    }
  }
  return order.map((group) => ({ group, fields: byGroup.get(group) as SettingField[] }))
}

/**
 * Human label per registry group, as whole literal catalog keys so the
 * key-reference gate can resolve every entry — the {@link SETTING_LABEL_KEY} idiom.
 * The raw group segment stays on screen as the detail line, because it is what the
 * document and the write log speak. A group absent here is NOT an error: the groups
 * are the engine's, and one it adds to its registry heads its subsection with the
 * raw segment until a label is authored, rather than being dropped.
 */
const GROUP_LABEL_KEY: Record<string, string> = {
  concurrency: 'apps.specEngine.configPanel.group_labels.concurrency',
  limits: 'apps.specEngine.configPanel.group_labels.limits',
  timeouts: 'apps.specEngine.configPanel.group_labels.timeouts',
  budget: 'apps.specEngine.configPanel.group_labels.budget',
  watch: 'apps.specEngine.configPanel.group_labels.watch',
  delivery: 'apps.specEngine.configPanel.group_labels.delivery',
  notify: 'apps.specEngine.configPanel.group_labels.notify',
  telemetry: 'apps.specEngine.configPanel.group_labels.telemetry',
}

/** The translated label for a registry group, or `''` for one no label names. */
function groupLabel(group: string): string {
  // Indexed at the call site rather than through a local, so the key-reference gate
  // resolves every entry in the map — the ORIGIN_KEY idiom.
  return GROUP_LABEL_KEY[group] ? i18nT(GROUP_LABEL_KEY[group]) : ''
}

/** The DOM id of one group's subsection, so the jump nav can scroll to it. */
function groupAnchorId(group: string): string {
  return `se-settings-group-${group}`
}

/**
 * The generated rows, grouped into the registry's own subsections with a jump nav.
 *
 * The rows themselves are unchanged — the same {@link SettingsFields}, the same
 * scope offering, staging and review — so the write machinery is untouched and only
 * the visible structure differs. Two things this adds, both statable:
 *
 * 1. **The subsections are exactly the registry's groups**, in first-appearance
 *    order, via the pure {@link settingGroups}. A group with an authored label heads
 *    its subsection with it and shows the raw segment as the detail line; an
 *    unmapped group heads with the raw segment rather than being dropped.
 * 2. **The jump navigation is in flow**, above the rows — a row of `se-filter`
 *    buttons that scroll to a subsection, with no sticky or floating positioning
 *    (the app's layout holds only because it has no overlay). It renders only when
 *    there is more than one subsection, because a single group is its own heading.
 */
function GroupedSettings({
  fields,
  stagedAt,
  onScope,
  onStage,
  onWithdraw,
}: {
  fields: readonly SettingField[]
  stagedAt: (segments: readonly string[]) => StagedEdit | undefined
  onScope: (field: SettingField, scope: string) => void
  onStage: (field: SettingField, value: unknown) => void
  onWithdraw: (segments: readonly string[]) => void
}) {
  const groups = useMemo(() => settingGroups(fields), [fields])
  return (
    <>
      {groups.length > 1 && (
        <div
          className="se-filters se-jump"
          role="group"
          aria-label={i18nT('apps.specEngine.configPanel.jump_to_a_settings_section')}
        >
          {groups.map(({ group }) => {
            const label = groupLabel(group)
            return (
              <button
                key={group}
                type="button"
                className="se-filter"
                // In flow, so the scroll is the only movement: `scrollIntoView`
                // reads from the DOM by id rather than a ref, because the subsection
                // it targets is rendered below in the same pass and a ref would only
                // be a second handle on the same node.
                onClick={() => document.getElementById(groupAnchorId(group))?.scrollIntoView()}
              >
                {label || <span className="se-m">{group}</span>}
              </button>
            )
          })}
        </div>
      )}
      {groups.map(({ group, fields: groupFields }) => {
        const label = groupLabel(group)
        return (
          <section key={group} id={groupAnchorId(group)} className="se-setting-group">
            <h4 className="se-setting-group-head">
              {label ? (
                <>
                  {label}
                  <span className="se-kv-path">{group}</span>
                </>
              ) : (
                <span className="se-m">{group}</span>
              )}
            </h4>
            <SettingsFields
              fields={groupFields}
              stagedAt={stagedAt}
              onScope={onScope}
              onStage={onStage}
              onWithdraw={onWithdraw}
            />
          </section>
        )
      })}
    </>
  )
}

/**
 * Every setting the engine registers, as a form.
 *
 * The fields are GENERATED from the registry the read supplies — key, type,
 * bounds, permitted scopes, summary — rather than listed here. That is the whole
 * point of the read: a hard-coded field list is how a form comes to offer a
 * setting the write door rejects, or to omit one it accepts, and neither failure
 * shows up until somebody tries to change a value.
 *
 * Four properties of the editing, each mirroring the autonomy grid's:
 *
 * 1. **A change is not a write.** Every edit accumulates in the shared staged-edit
 *    state and reaches the engine only through a confirm on the review card that
 *    shows the exact patch. Nothing here writes on change.
 * 2. **The rows always show the store.** The value in force and its origin come
 *    from the resolved read; a staged edit sits in the control beside them, marked
 *    unwritten. So a refused write leaves the form stating what is persisted, and
 *    nothing is invalidated on failure.
 * 3. **A success re-reads.** This mutation owns that: the review card is shared
 *    and presentational, so it cannot invalidate for its callers. The document,
 *    every resolved read and the grid all describe what is now stored, and one
 *    settings write can move all three — `watch.interval_s` at source scope is
 *    read by the sources section too.
 * 4. **A failed read is doubt, not an empty form.** `isError` is read BEFORE the
 *    data, because React Query keeps the last successful answer across a failing
 *    refetch: rows filled from a retained answer would present values nobody
 *    re-read as what is in force, and the registry's own defaults are emphatically
 *    not that.
 *
 * ## One instance per pipeline stage
 *
 * `groups` narrows the generated fields to the setting GROUPS one stage presents —
 * the leading dot-segment of a registry key, which is `Setting.group` on the
 * engine's side. The pane mounts one of these per stage, each filtered to the
 * groups the ENGINE placed in that stage, so a stage shows the settings that govern
 * it and no others.
 *
 * The filter is over projected group names and never over a list of keys, which is
 * the whole reason the stage projection carries groups: a setting the engine adds
 * to a mapped group appears on its stage with no edit here. Omitting `groups`
 * generates every setting the registry declares, which is what a caller wants when
 * it is not one stage's surface.
 *
 * Each instance keeps its OWN staging, and that is correct rather than a
 * compromise: a stage's review card shows and its patch carries exactly the edits
 * staged on that stage, so confirming one stage's change cannot write another's.
 */
export function SettingsForm({
  project,
  groups,
  onPendingCount,
}: {
  project: string
  /** The setting groups to generate, or every group when omitted. */
  groups?: readonly string[]
  /** Report how many staged changes this form would review, for the tab badge. */
  onPendingCount?: (count: number) => void
}) {
  const client = useQueryClient()
  const edits = useStagedEdits()
  // The scope each row writes at, keyed by registry key. Normalized on read
  // rather than trusted: a target can be deselected while a choice sits here.
  const [scopeChosen, setScopeChosen] = useState<Record<string, string>>({})
  const [source, setSource] = useState('')
  const [reviewing, setReviewing] = useState(false)
  const [wrote, setWrote] = useState(false)

  const registry = useQuery({
    queryKey: QK.registry,
    queryFn: () => specEngineApi.configRegistry(),
    retry: false,
    // Bundled vocabulary: it is a projection of the engine's own constants, so it
    // cannot change while the page is open and no write can move it.
    staleTime: Infinity,
  })
  // The SAME key and the same request as the resolved pane beside this form, so
  // the two read one answer: a second cache entry for one reading is how a
  // control comes to edit a value the pane says is not in force.
  const resolved = useQuery({
    queryKey: QK.resolved(project),
    queryFn: () => specEngineApi.resolvedConfig(project || undefined),
    retry: false,
  })
  // Only for the NAMES a source-scoped write can target; the grid below reads the
  // same query, so this costs no second request.
  const sources = useQuery({
    queryKey: QK.sources,
    queryFn: () => specEngineApi.sources(),
    retry: false,
  })

  const write = useMutation({
    mutationFn: (patch: Document) => specEngineApi.writeConfig(patch),
    onSuccess: () => {
      edits.clear()
      setReviewing(false)
      setWrote(true)
      // The reply's merged document is NOT adopted: the reads are this pane's
      // authority on what is persisted. All three are named because they are three
      // readings a reader would otherwise have to know the key layout to see
      // refreshed, even though the resolved and sources keys sit under the
      // document key's prefix.
      void client.invalidateQueries({ queryKey: QK.config })
      void client.invalidateQueries({ queryKey: QK_RESOLVED_ROOT })
      void client.invalidateQueries({ queryKey: QK.sources })
    },
    // No `onError`: a refusal must leave the staged edits in place and the queries
    // untouched, so the rows keep showing the store's own state.
  })

  const projected = registry.data?.settings ?? NO_SETTINGS
  // Narrowed to the stage's groups when the caller named any. `settingGroup` is
  // total -- a key with no dot is its own whole group -- so nothing is dropped by
  // the split itself, only by not belonging to the groups asked for.
  const settings = useMemo(
    () =>
      groups === undefined
        ? projected
        : projected.filter((setting) => groups.includes(settingGroup(setting.key))),
    [projected, groups],
  )
  const inForce = useMemo(() => {
    const found = new Map<string, EffectiveSetting>()
    for (const value of resolved.data?.settings ?? []) found.set(value.key, value)
    return found
  }, [resolved.data])
  // `isError` before the data for property 4: a retained source list would name a
  // write target nobody re-read.
  const names = sources.isError ? [] : (sources.data?.sources.map((entry) => entry.name) ?? [])
  const sourceTarget = names.includes(source) ? source : (names[0] ?? '')
  // Whether ANY rendered setting can be written at source scope. The picker
  // chooses where source-scoped writes land, so on a vocabulary with no such
  // setting it would be a chooser that targets nothing.
  const sourceScoped = settings.some((setting) => setting.scopes.includes(SCOPE_SOURCE))
  const fields = useMemo(
    () => settingFields(settings, inForce, { project, source: sourceTarget }, scopeChosen),
    [settings, inForce, project, sourceTarget, scopeChosen],
  )
  // Every path a row can address right now, so a staged edit that no longer has a
  // row can be dropped. Three ways an edit stops having one: the selected project
  // moved, the source target moved, and the vocabulary changed under it — each
  // from this pane, from another surface, or on any refetch. An edit no row shows
  // is an edit no sentence describes and no confirm clears, and leaving it staged
  // would put a path in the patch that the review card never accounted for.
  const addressable = useMemo(() => {
    const paths = new Set<string>()
    for (const field of fields) {
      for (const offer of field.offers) {
        if (offer.segments) paths.add(JSON.stringify(offer.segments))
      }
    }
    return paths
  }, [fields])
  const { reconcile } = edits
  useEffect(() => {
    reconcile((edit) => addressable.has(JSON.stringify(edit.segments)))
  }, [addressable, reconcile])

  // `isError` first, then the data: see property 4 above. Both reads are named
  // because they fail for different reasons and only one of them is repairable
  // from this pane.
  if (registry.isError || resolved.isError) {
    return (
      <div className="se-blk">
        {/* What it HOLDS, not what it can review: with no vocabulary the form
            cannot say what any staged edit means, and a tab badge that dropped to
            zero here would report unwritten work as gone. */}
        <PendingCount count={edits.edits.length} onCount={onPendingCount} />
        <h3>{i18nT('apps.specEngine.settingsForm.settings')}</h3>
        {registry.isError && (
          <Refused
            title={i18nT('apps.specEngine.settingsForm.could_not_read_the_setting_registry')}
            error={registry.error}
          />
        )}
        {resolved.isError && (
          <Refused
            title={i18nT('apps.specEngine.configPanel.could_not_resolve_the_configuration')}
            error={resolved.error}
          />
        )}
      </div>
    )
  }
  if (registry.isPending || resolved.isPending || !registry.data || !resolved.data) {
    // Distinct from the empty vocabulary on purpose: "the engine registers no
    // setting" is a fact about the engine, and "not read yet" is a fact about this
    // request.
    return (
      <div className="se-blk">
        <PendingCount count={edits.edits.length} onCount={onPendingCount} />
        <h3>{i18nT('apps.specEngine.settingsForm.settings')}</h3>
        <p className="se-note">
          {i18nT('apps.specEngine.settingsForm.reading_the_setting_registry')}
        </p>
      </div>
    )
  }

  // Plain functions rather than `useCallback`: all three close over the mutation
  // object, which React Query hands back fresh on every render, so a memo here
  // would advertise a stability it cannot have.
  const stage = (field: SettingField, value: unknown) => {
    if (!field.segments) return
    setWrote(false)
    write.reset()
    // Typing back exactly what THIS path already stores is not a change, and every
    // write is recorded: staging it would put a line in the durable write record
    // for an edit nobody made. The same value at another path is a real change, so
    // the withdrawal is conditioned on the declaring path and not on the value.
    if (storedAt(field.inForce, field.segments, value)) edits.unstage(field.segments)
    else edits.stage(field.segments, value)
  }

  const withdraw = (segments: readonly string[]) => {
    setWrote(false)
    write.reset()
    edits.unstage(segments)
  }

  const chooseScope = (field: SettingField, scope: string) => {
    setWrote(false)
    write.reset()
    const moving = field.segments ? edits.stagedAt(field.segments) : undefined
    const target = field.offers.find((offer) => offer.scope === scope)?.segments ?? null
    if (field.segments) edits.unstage(field.segments)
    // The staged value MOVES with the scope rather than being dropped: an operator
    // who typed a value and then decided it belongs to one project meant to keep
    // the value. It is withdrawn instead when the scope it moved to already stores
    // it, for `stage`'s reason.
    if (moving && target && !storedAt(field.inForce, target, moving.value)) {
      edits.stage(target, moving.value)
    }
    setScopeChosen((current) => ({ ...current, [field.setting.key]: scope }))
  }

  const discard = () => {
    edits.clear()
    setReviewing(false)
    setWrote(false)
    write.reset()
  }

  // The staged edits paired with the row that accounts for each, and the patch
  // built from exactly those. One list for both, because a review that showed a
  // patch line it could not explain — or a write that carried one — is the failure
  // the review card exists to prevent.
  const reviewed: Array<{ field: SettingField; edit: StagedEdit }> = []
  for (const field of fields) {
    if (!field.segments) continue
    const staged = edits.stagedAt(field.segments)
    if (staged) reviewed.push({ field, edit: staged })
  }
  const patch = buildFormPatch(reviewed.map((entry) => entry.edit))

  return (
    <div className="se-blk">
      {/* The same number the "unwritten setting changes" line below states, read
          from the same list, so the tab badge cannot claim a count this form does
          not show. */}
      <PendingCount count={reviewed.length} onCount={onPendingCount} />
      <h3>{i18nT('apps.specEngine.settingsForm.settings')}</h3>
      {fields.length === 0 ? (
        <p className="se-note">{i18nT('apps.specEngine.settingsForm.no_setting_is_registered')}</p>
      ) : (
        <>
          {sourceScoped && names.length > 0 && (
            <div
              className="se-acts"
              role="group"
              aria-label={i18nT('apps.specEngine.settingsForm.select_a_watch_source_to_write_at')}
            >
              {names.map((name) => (
                <button
                  key={name}
                  type="button"
                  className="se-btn se-sm se-m"
                  aria-pressed={name === sourceTarget}
                  onClick={() => setSource(name)}
                >
                  {name}
                </button>
              ))}
            </div>
          )}
          <GroupedSettings
            fields={fields}
            stagedAt={edits.stagedAt}
            onScope={chooseScope}
            onStage={stage}
            onWithdraw={withdraw}
          />
          <div className="se-acts" style={{ marginTop: 9 }}>
            <button
              type="button"
              className="se-btn"
              disabled={reviewed.length === 0}
              onClick={() => setReviewing(true)}
            >
              {i18nT('apps.specEngine.settingsForm.review_the_exact_change')}
            </button>
            {reviewed.length > 0 && (
              <span className="se-lbl">
                {i18nT('apps.specEngine.settingsForm.unwritten_setting_changes')}
                {SEP}
                <span className="se-m">{fmtNumber(reviewed.length)}</span>
              </span>
            )}
          </div>
          {reviewing && reviewed.length > 0 && (
            <FormReview
              changes={reviewed.map(({ field, edit }) => ({
                path: dotted(edit.segments),
                sentence: i18nT(
                  'apps.specEngine.settingsForm.edit_replaces_the_value_in_force',
                  {
                    setting: settingLabel(field.setting.key) || field.setting.key,
                    path: dotted(edit.segments),
                    oldValue: shownValue(field.inForce?.value),
                    newValue: shownValue(edit.value),
                    origin: originText(field.inForce),
                  },
                ),
              }))}
              patch={patch}
              labels={{
                heading: i18nT('apps.specEngine.settingsForm.the_change_that_would_be_written'),
                confirm: i18nT('apps.specEngine.settingsForm.write_the_change'),
                writing: i18nT('apps.specEngine.configPanel.saving'),
                discard: i18nT('apps.specEngine.settingsForm.discard_the_pending_changes'),
                exactly: i18nT('apps.specEngine.settingsForm.a_confirm_writes_exactly_this_patch'),
                refusalTitle: i18nT(
                  'apps.specEngine.settingsForm.could_not_write_the_setting_change',
                ),
                retained: i18nT(
                  'apps.specEngine.settingsForm.nothing_was_written_so_the_rows_are_stored_state',
                ),
              }}
              writing={write.isPending}
              error={write.isError ? write.error : null}
              onConfirm={(sending) => write.mutate(sending)}
              onDiscard={discard}
            />
          )}
          {wrote && (
            <p className="se-note" role="status">
              {i18nT('apps.specEngine.settingsForm.wrote_the_change_and_re_read_the_settings')}
            </p>
          )}
          {/* The two facts a reader would otherwise have to infer from the
              controls: where the fields come from, and what a scope's write
              actually targets. */}
          <p className="se-note">
            {i18nT('apps.specEngine.settingsForm.every_field_comes_from_the_engines_registry')}
          </p>
          <p className="se-note">
            {i18nT('apps.specEngine.settingsForm.a_scope_targets_the_selection_above')}
          </p>
        </>
      )}
    </div>
  )
}

// --- the cost profiles form ---------------------------------------------------

/**
 * The model an assignment names when nothing pins one, and the engine's own
 * spelling of "let the served backend choose".
 *
 * It is what every bundled preset assigns (`PRESET_MODEL` in `profiles.py`) and
 * what this form defaults a new assignment to, for the reason the engine gives:
 * accounts differ in entitlement, so a concrete model chosen on a reader's behalf
 * fails at runtime — silently until the first prompt — for anyone not entitled to
 * it.
 */
const AUTO_MODEL = 'auto'

/** An empty vocabulary before the read answers, so a memo sees one array. */
const NO_NAMES: readonly string[] = []

/** An empty preset list before the read answers, for `NO_NAMES`'s reason. */
const NO_PROFILE_PRESETS: readonly ProfilePreset[] = []

/** The profiles the document declares, in name order. */
export function profileNames(document: Document): string[] {
  const node = document[COST_PROFILES]
  return isObject(node) ? Object.keys(node).sort() : []
}

/**
 * Every project whose entry selects *profile*, in name order.
 *
 * Read from the DOCUMENT rather than from a resolution, because the question is
 * which entries NAME this profile — including a project whose resolution fell
 * back for some other reason. That is exactly the set a removal would strand.
 */
export function projectsSelecting(document: Document, profile: string): string[] {
  const node = document[PROJECTS]
  if (!isObject(node)) return []
  return Object.keys(node)
    .filter((name) => {
      const entry = node[name]
      return isObject(entry) && entry[PROJECT_PROFILE_FIELD] === profile
    })
    .sort()
}

/**
 * The efforts a row may pin: the engine's ladder, plus a stored value the ladder
 * does not contain.
 *
 * {@link levelOptions}' reason, for the same shape of control: a hand-edited
 * effort outside the vocabulary would otherwise leave no button pressed, so the
 * row could not say what is stored. The out-of-vocabulary value is offered so it
 * is visible and replaceable; the write door refuses it either way.
 */
function effortOptions(efforts: readonly string[], stored: unknown): readonly string[] {
  if (typeof stored !== 'string' || stored === '' || efforts.includes(stored)) return efforts
  return [stored, ...efforts]
}

/** One role's row: where its two fields are written, and what is stored there. */
interface RoleField {
  role: string
  /** The path the model control writes. */
  modelSegments: readonly string[]
  /** The path the effort control writes. */
  effortSegments: readonly string[]
  /** The model stored in the profile, `undefined` when the role has none. */
  storedModel: unknown
  /** The effort stored in the profile, `undefined` when the role pins none. */
  storedEffort: unknown
}

/**
 * One row per role in the vocabulary, in the order the read supplies them.
 *
 * Total over that vocabulary by construction: a role the profile has no
 * assignment for is a row with nothing stored rather than an absent row, because
 * a role nobody has assigned is precisely the one an operator came here to assign.
 */
function roleFields(profile: string, roles: readonly string[], document: Document): RoleField[] {
  return roles.map((role) => {
    const assignment = nodeAt(document, roleSegments(profile, role))
    const stored = isObject(assignment) ? assignment : {}
    return {
      role,
      modelSegments: roleFieldSegments(profile, role, FIELD_MODEL),
      effortSegments: roleFieldSegments(profile, role, FIELD_EFFORT),
      storedModel: stored[FIELD_MODEL],
      storedEffort: stored[FIELD_EFFORT],
    }
  })
}

/**
 * One stored setting's row: its registry record, its path, and what is there.
 *
 * Shared by a cost profile's pinned limits and a watch source's own settings,
 * because both are the same thing — one registry setting stored at one path the
 * document holds directly, rather than resolved through the scope precedence the
 * settings form reads. A second copy of this for sources would be a second place
 * the control, the bounds and the staged-vs-stored mark could drift from the
 * registry.
 */
interface StoredSettingField {
  setting: RegistrySetting
  segments: readonly string[]
  /** The value stored at the path, `undefined` when nothing is. */
  stored: unknown
}

/**
 * One row per key a profile may pin, for the keys the registry also describes.
 *
 * Both vocabularies arrive in ONE read, so a pinnable key with no registry record
 * means the payload disagrees with itself rather than that a setting is new: there
 * would be no kind, no bounds and no summary to generate a control from. Such a
 * key is skipped rather than rendered as an untyped text box that would stage a
 * string into a numeric limit.
 */
function profileSettingFields(
  profile: string,
  keys: readonly string[],
  settings: readonly RegistrySetting[],
  document: Document,
): StoredSettingField[] {
  const fields: StoredSettingField[] = []
  for (const key of keys) {
    const setting = settings.find((entry) => entry.key === key)
    const segments = profileSettingSegments(profile, key)
    if (!setting || !segments) continue
    fields.push({ setting, segments, stored: nodeAt(document, segments) })
  }
  return fields
}

/**
 * One role assignment: the model it routes to, the effort it pins, and the rule
 * that decides whether that effort does anything at all.
 *
 * Three things on this row are load-bearing:
 *
 * 1. **The model is free text, defaulting to `auto`.** The engine deliberately
 *    does not validate entitlement — a picker here would promise a check nobody
 *    performs — so this is a text field, and the honest statement about it sits
 *    with the section rather than being implied by a closed list.
 * 2. **The effort is one button per rung.** The ladder is short and fixed, and a
 *    dropdown would draw a popup over a page whose kill-switch strip must never be
 *    covered: the autonomy grid's level control is buttons for the same reason.
 * 3. **While the model is `auto`, the row states that the effort is inert.** Not a
 *    style note: kiro-cli accepts no reasoning effort on `auto`, so the resolver
 *    DROPS a pinned effort and records having dropped it. An operator who pins
 *    `high` here would otherwise have to work out from a `dropped` flag on another
 *    pane why nothing changed. The sentence appears and disappears with the model
 *    value, because it is a statement about that value rather than about the
 *    profile.
 */
function RoleAssignmentRow({
  field,
  efforts,
  stagedModel,
  stagedEffort,
  onModel,
  onEffort,
}: {
  field: RoleField
  efforts: readonly string[]
  /** The edit staged at the model path, or `undefined` when none is. */
  stagedModel: StagedEdit | undefined
  /** The edit staged at the effort path, or `undefined` when none is. */
  stagedEffort: StagedEdit | undefined
  onModel: (value: string) => void
  onEffort: (effort: string) => void
}) {
  const id = useId()
  const staged = stagedModel !== undefined || stagedEffort !== undefined
  // What a write would store, which is what the controls have to show: the staged
  // value when there is one, the stored value when there is not, and the engine's
  // own default for a role nothing has assigned.
  const model = stagedModel
    ? String(stagedModel.value ?? '')
    : typeof field.storedModel === 'string'
      ? field.storedModel
      : AUTO_MODEL
  const effort = stagedEffort
    ? String(stagedEffort.value ?? '')
    : typeof field.storedEffort === 'string'
      ? field.storedEffort
      : ''
  const effortLabel = i18nT('apps.specEngine.profilesForm.effort_for_role', { role: field.role })
  return (
    <div className="se-setting" data-role={field.role} data-staged={staged}>
      <label className="se-setting-name" htmlFor={id}>
        {i18nT('apps.specEngine.profilesForm.model_for_role', { role: field.role })}
        {/* The path the write lands at, as the detail line: it is what the document
            and the write log speak, and a role assignment lives on a SHARED
            profile, so the node being changed is the whole blast radius. */}
        <span className="se-kv-path">{dotted(field.modelSegments)}</span>
      </label>
      <input
        id={id}
        type="text"
        className="se-input"
        value={model}
        onChange={(event) => onModel(event.target.value)}
      />
      {efforts.length > 0 && (
        <div className="se-acts" role="group" aria-label={effortLabel}>
          {effortOptions(efforts, field.storedEffort).map((level) => (
            <button
              key={level}
              type="button"
              className="se-btn se-sm se-m"
              // Pressed on what a write would store, which after a staged choice is
              // no longer what the profile holds.
              aria-pressed={level === effort}
              onClick={() => onEffort(level)}
            >
              {level}
            </button>
          ))}
        </div>
      )}
      {model.trim() === AUTO_MODEL && (
        <p className="se-note" data-effort-inert="true">
          {i18nT('apps.specEngine.profilesForm.a_pinned_effort_is_inert_while_the_model_is_auto')}
        </p>
      )}
      <p className="se-note">
        {i18nT('apps.specEngine.profilesForm.stored_in_the_profile')}
        {SEP}
        <span className="se-m">{shownValue(field.storedModel)}</span>
        {SEP}
        <span className="se-m">{shownValue(field.storedEffort)}</span>
      </p>
      {stagedModel !== undefined && (
        <p className="se-note">
          <span className="se-flag" data-flag="pending">
            {i18nT('apps.specEngine.profilesForm.not_written')}
          </span>
          <span className="se-m">
            {shownValue(stagedModel.value)}
            {SEP}
            {dotted(field.modelSegments)}
          </span>
        </p>
      )}
      {stagedEffort !== undefined && (
        <p className="se-note">
          <span className="se-flag" data-flag="pending">
            {i18nT('apps.specEngine.profilesForm.not_written')}
          </span>
          <span className="se-m">
            {shownValue(stagedEffort.value)}
            {SEP}
            {dotted(field.effortSegments)}
          </span>
        </p>
      )}
    </div>
  )
}

/**
 * One setting stored at one path: the registry's own control for it, and what is
 * stored there.
 *
 * The control comes from the shared {@link SettingControl}, so a limit pinned in a
 * profile, a setting a source holds, and the same setting written at app scope are
 * all edited by one control carrying one set of bounds. What differs is only where
 * the write lands — `cost_profiles.<name>` or `sources.<name>` rather than a scope
 * — and the row states that path for the settings rows' reason: the path IS the
 * blast radius, and a profile is shared by every project that selected it.
 *
 * The three sentences are the CALLER's, resolved at its own call site: "stored in
 * the profile" and "stored on the source" are different facts, and a shared row
 * that invented one wording for both would say the wrong one half the time.
 */
interface StoredSettingLabels {
  /** Leads the line stating what is stored at the path. */
  stored: string
  /** Marks a staged value as unwritten. */
  notWritten: string
  /** States that this registry kind has no control here, naming the kind. */
  notEditable: string
}

function StoredSettingRow({
  field,
  labels,
  staged,
  onStage,
  onWithdraw,
}: {
  field: StoredSettingField
  labels: StoredSettingLabels
  staged: StagedEdit | undefined
  onStage: (value: unknown) => void
  onWithdraw: () => void
}) {
  const id = useId()
  const { setting, segments, stored } = field
  const control = CONTROL_BY_KIND[setting.kind]
  const label = settingLabel(setting.key)
  const named = label ? (
    <>
      {label}
      <span className="se-kv-path">{dotted(segments)}</span>
    </>
  ) : (
    <span className="se-m">{dotted(segments)}</span>
  )
  return (
    <div className="se-setting" data-kind={setting.kind} data-staged={staged !== undefined}>
      {control === undefined ? (
        // No control to name, so no label element: a `for` pointing at nothing
        // announces a form field that is not there.
        <span className="se-setting-name">{named}</span>
      ) : (
        <label className="se-setting-name" htmlFor={id}>
          {named}
        </label>
      )}
      {control === undefined ? (
        <p className="se-note">{labels.notEditable}</p>
      ) : (
        <SettingControl
          id={id}
          setting={setting}
          control={control}
          value={staged ? staged.value : stored}
          disabled={false}
          onStage={onStage}
          onWithdraw={onWithdraw}
        />
      )}
      {/* The registry's OWN summary, not a second sentence maintained here. */}
      <p className="se-note">{setting.summary}</p>
      <p className="se-note">
        {labels.stored}
        {SEP}
        <span className="se-m">{shownValue(stored)}</span>
      </p>
      {staged !== undefined && (
        <p className="se-note">
          <span className="se-flag" data-flag="pending">
            {labels.notWritten}
          </span>
          <span className="se-m">
            {shownValue(staged.value)}
            {SEP}
            {dotted(segments)}
          </span>
        </p>
      )}
    </div>
  )
}

/**
 * Which sentence describes a role-field edit, keyed by the assignment field it
 * addresses.
 *
 * Keys rather than resolved strings, for `ORIGIN_KEY`'s reason — a module-level
 * `i18nT()` runs once at import and would freeze the table in whichever language
 * happened to be active then. Whole literal values indexed at the call site, so
 * the key-reference gate can resolve every entry rather than seeing a key this
 * module composed.
 *
 * A field with no entry earns no sentence, and therefore never reaches a patch:
 * the two fields here are the two this form has controls for, and a third one
 * would need its own control and its own sentence in the same change.
 */
const ROLE_FIELD_SENTENCE_KEY: Record<string, string> = {
  model: 'apps.specEngine.profilesForm.edit_replaces_the_role_model',
  effort: 'apps.specEngine.profilesForm.edit_replaces_the_role_effort',
}

/** Where a staged profile copy came from, as the review card names it. */
interface CopySource {
  name: string
  /** Whether the copy came from a bundled preset rather than another profile. */
  bundled: boolean
}

/**
 * Which preset or profile a staged entry is a copy OF, or `null` for neither.
 *
 * Derived from the staged bytes rather than remembered from the click, and that is
 * the point: the review card's sentence CLAIMS a provenance, and a claim checked
 * against what is actually staged cannot describe a copy of something else. An
 * entry matching nothing earns no sentence and therefore never reaches a patch —
 * an empty profile reports that a profile is selected while resolving every role
 * to the session default, so it must not be writable from here even if some later
 * control staged one.
 *
 * Compared as JSON text: both sides are objects a read handed over — a preset
 * entry, or a node of the document — so a copy and its origin serialize
 * identically. Two vocabularies holding byte-identical entries name whichever
 * comes first, which is honest either way, since the bytes about to be written are
 * both of theirs.
 */
function copySourceOf(
  value: unknown,
  presets: readonly ProfilePreset[],
  profiles: readonly string[],
  document: Document,
): CopySource | null {
  const staged = JSON.stringify(value)
  for (const preset of presets) {
    if (JSON.stringify(preset.entry) === staged) return { name: preset.name, bundled: true }
  }
  for (const name of profiles) {
    if (JSON.stringify(nodeAt(document, profileSegments(name))) === staged) {
      return { name, bundled: false }
    }
  }
  return null
}

/**
 * The cost profiles, as forms: role assignments, pinned limits, add and remove.
 *
 * A cost profile is the one place role assignments live — a project SELECTS a
 * profile rather than overriding roles within it — so this form is where a model
 * or an effort is changed at all. Five properties of it are claims rather than
 * arrangement:
 *
 * 1. **The rows are the engine's vocabularies.** Roles, efforts and the keys a
 *    profile may pin all come from the registry read, and a pinned limit's type,
 *    bounds and summary come from the same registry the settings form is generated
 *    from. Nothing here lists a role or an effort of its own.
 * 2. **A change is not a write.** Every edit accumulates in the shared staged-edit
 *    state and reaches the engine only through a confirm on the review card that
 *    shows the exact patch.
 * 3. **The consequence is stated where the edit is made.** A profile is shared by
 *    every project that selected it, so the form says so with the count from the
 *    document. A form that showed a role table without it would let somebody
 *    retune four projects believing they had tuned one.
 * 4. **An add is a copy, and a remove that would strand a project is refused.** An
 *    empty profile resolves every role to the session default while reporting that
 *    a profile IS selected, and a removed profile leaves its projects in exactly
 *    that state — so one is not offered, and the other names the projects that
 *    block it, because pointing them elsewhere is the action that unblocks it.
 * 5. **A success re-reads.** This mutation owns the invalidation: the review card
 *    is presentational and cannot do it for its callers. The document is where
 *    every row here comes from, and the resolved pane beside it renders the very
 *    roles this write changes.
 */
export function ProfilesForm({
  config,
  onPendingCount,
}: {
  config: ConfigSnapshot
  /** Report how many staged changes this form would review, for the tab badge. */
  onPendingCount?: (count: number) => void
}) {
  const client = useQueryClient()
  const edits = useStagedEdits()
  const [chosen, setChosen] = useState('')
  const [addName, setAddName] = useState('')
  const [reviewing, setReviewing] = useState(false)
  const [wrote, setWrote] = useState(false)
  // The last removal click that was refused, and the staged copy the form had to
  // withdraw. Both exist so an action's outcome is STATED rather than inferred
  // from the absence of a change: a refused click with no new feedback looks
  // inert, and a withdrawn edit with no sentence looks like it was never made.
  const [removalRefused, setRemovalRefused] = useState(false)
  const [orphanedCopy, setOrphanedCopy] = useState('')

  const registry = useQuery({
    queryKey: QK.registry,
    queryFn: () => specEngineApi.configRegistry(),
    retry: false,
    // Bundled vocabulary: a projection of the engine's own constants, so it cannot
    // change while the page is open. The same key the settings form reads, so the
    // two share one answer and one request.
    staleTime: Infinity,
  })

  const write = useMutation({
    mutationFn: (patch: Document) => specEngineApi.writeConfig(patch),
    onSuccess: () => {
      edits.clear()
      setAddName('')
      setReviewing(false)
      setWrote(true)
      // The reply's merged document is NOT adopted: the read is this pane's
      // authority on what is persisted, and every row here is a reading OF that
      // document. All three keys are named because they are three readings a
      // reader would otherwise have to know the key layout to see refreshed.
      void client.invalidateQueries({ queryKey: QK.config })
      void client.invalidateQueries({ queryKey: QK_RESOLVED_ROOT })
      void client.invalidateQueries({ queryKey: QK.sources })
    },
    // No `onError`: a refusal must leave the staged edits in place and the queries
    // untouched, so the rows keep showing the store's own state.
  })

  const document = config.document
  const names = useMemo(() => profileNames(document), [document])
  const presets = registry.data?.profile_presets ?? NO_PROFILE_PRESETS
  const roles = registry.data?.roles ?? NO_NAMES
  const efforts = registry.data?.efforts ?? NO_NAMES
  const pinnable = registry.data?.profile_settings ?? NO_NAMES
  const settings = registry.data?.settings ?? NO_SETTINGS
  // Normalized against the document rather than trusted: a profile removed here,
  // in the JSON view, or by another surface must not leave the form editing a name
  // the document no longer carries.
  const selected = names.includes(chosen) ? chosen : (names[0] ?? '')
  const pending = addName.trim()

  // Every profile a staged edit may address: one in the document, or the one the
  // add block is naming. An edit whose profile has left the document is dropped
  // rather than carried, because its patch would RESURRECT that profile carrying
  // one field — a `cost_profiles` entry with a model and no roles object — and no
  // sentence on the card would say so. That removal can arrive from this pane, from
  // the JSON view, or on any refetch, which is why this reconciles against the
  // current answer instead of trusting the document to hold still between an edit
  // and its confirm.
  const { reconcile } = edits
  useEffect(() => {
    reconcile(
      (edit) =>
        edit.segments.length >= 2 &&
        edit.segments[0] === COST_PROFILES &&
        (names.includes(edit.segments[1]) || edit.segments[1] === pending),
    )
  }, [names, pending, reconcile])

  // A staged copy is reviewable only while its bytes still match the preset or
  // profile it copied: provenance is derived from the bytes, and an edit with no
  // provenance gets no sentence and reaches no patch. A write from another
  // surface can change the source UNDER a staged copy, and leaving the edit
  // staged would make it silently absent from both the card and the write — so
  // it is withdrawn, and the withdrawal is stated where the edit was made.
  const { edits: stagedNow, unstage } = edits
  useEffect(() => {
    const presetsNow = registry.data?.profile_presets
    if (!presetsNow) return
    for (const edit of stagedNow) {
      if (edit.segments.length !== 2 || edit.segments[0] !== COST_PROFILES) continue
      if (edit.value === DELETE) continue
      if (copySourceOf(edit.value, presetsNow, names, document) === null) {
        unstage(edit.segments)
        setOrphanedCopy(edit.segments[1])
      }
    }
  }, [stagedNow, unstage, registry.data, names, document])

  // `isError` before the data: React Query keeps the last successful answer across
  // a failing refetch, and rows generated from a retained vocabulary would offer
  // roles and limits nobody re-read.
  if (registry.isError) {
    return (
      <div className="se-blk">
        {/* What it HOLDS, not what it can review: with no vocabulary the form
            cannot say what any staged edit means, and a tab badge that dropped to
            zero here would report unwritten work as gone. */}
        <PendingCount count={edits.edits.length} onCount={onPendingCount} />
        <h3>{i18nT('apps.specEngine.profilesForm.cost_profiles')}</h3>
        <Refused
          title={i18nT('apps.specEngine.profilesForm.could_not_read_the_profile_vocabulary')}
          error={registry.error}
        />
      </div>
    )
  }
  if (registry.isPending || !registry.data) {
    // Distinct from an empty vocabulary on purpose: "the engine registers no role"
    // is a fact about the engine, and "not read yet" is a fact about this request.
    return (
      <div className="se-blk">
        <PendingCount count={edits.edits.length} onCount={onPendingCount} />
        <h3>{i18nT('apps.specEngine.profilesForm.cost_profiles')}</h3>
        <p className="se-note">
          {i18nT('apps.specEngine.profilesForm.reading_the_profile_vocabulary')}
        </p>
      </div>
    )
  }

  const fields = selected === '' ? [] : roleFields(selected, roles, document)
  const pinned =
    selected === '' ? [] : profileSettingFields(selected, pinnable, settings, document)
  const selecting = selected === '' ? [] : projectsSelecting(document, selected)

  // Plain functions rather than `useCallback`: each closes over the mutation
  // object, which React Query hands back fresh on every render, so a memo here
  // would advertise a stability it cannot have.
  const touched = () => {
    setWrote(false)
    setRemovalRefused(false)
    setOrphanedCopy('')
    write.reset()
  }

  /** Whether *value* is what the document already stores AT *segments*. */
  const storedHere = (segments: readonly string[], value: unknown) =>
    JSON.stringify(nodeAt(document, segments) ?? null) === JSON.stringify(value ?? null)

  const stageField = (segments: readonly string[], value: unknown) => {
    touched()
    // Typing back exactly what this path already stores is not a change, and every
    // write is recorded: staging it would put a line in the durable write record
    // for an edit nobody made. Compared against the DOCUMENT node at this path
    // rather than against a resolution, because a profile's assignment is stored
    // where it is read — there is no precedence between the two to disagree about.
    if (storedHere(segments, value)) edits.unstage(segments)
    else edits.stage(segments, value)
  }

  const stageEffort = (field: RoleField, effort: string) => {
    stageField(field.effortSegments, effort)
    // An effort with no model is an assignment the write door refuses
    // (`roles.<role>.model: expected a non-empty string`), so pinning an effort on
    // an unassigned role stages the default model with it. Staged rather than
    // implied: it becomes its own line in the patch and its own sentence on the
    // card, because a value written under an operator's confirm has to be one they
    // could see before confirming.
    if (typeof field.storedModel !== 'string' || field.storedModel.trim() === '') {
      if (!edits.stagedAt(field.modelSegments)) edits.stage(field.modelSegments, AUTO_MODEL)
    }
  }

  const stageCopy = (entry: unknown) => {
    touched()
    // Refused rather than merged: the store's merge would fold the copy INTO the
    // existing profile key by key, which is an edit to that profile rather than the
    // addition this control offers.
    if (pending === '' || names.includes(pending)) return
    edits.stage(profileSegments(pending), entry)
  }

  const renameAdd = (next: string) => {
    touched()
    const staged = pending === '' ? undefined : edits.stagedAt(profileSegments(pending))
    if (staged) edits.unstage(profileSegments(pending))
    const trimmed = next.trim()
    // A staged copy MOVES with the name rather than being dropped: an operator who
    // picked a preset and then reconsidered the name meant to keep the copy.
    if (staged && trimmed !== '' && !names.includes(trimmed)) {
      edits.stage(profileSegments(trimmed), staged.value)
    }
    setAddName(next)
  }

  const stageRemoval = (profile: string) => {
    touched()
    // Refused, and not by a silent disable: the operator has to know WHICH projects
    // block the removal, because pointing them at another profile is the action
    // that unblocks it. The refusal is also ACKNOWLEDGED — the naming note is on
    // screen before the click, so without this flag the click would look inert.
    if (projectsSelecting(document, profile).length > 0) {
      setRemovalRefused(true)
      return
    }
    edits.stage(profileSegments(profile), DELETE)
  }

  const discard = () => {
    edits.clear()
    setReviewing(false)
    setWrote(false)
    write.reset()
  }

  /**
   * One staged edit as the review card reads it, or `null` when this form cannot
   * say what the edit means.
   *
   * The patch is built from exactly the edits this returns a sentence for, so the
   * card can never show a line it cannot explain and a write can never carry one.
   */
  const describe = (edit: StagedEdit): ReviewedChange | null => {
    const path = dotted(edit.segments)
    const profile = edit.segments[1]
    const rest = edit.segments.slice(2)
    if (rest.length === 0) {
      if (edit.value === DELETE) {
        return {
          path,
          sentence: i18nT('apps.specEngine.profilesForm.edit_removes_the_profile', {
            profile,
            path,
          }),
        }
      }
      const from = copySourceOf(edit.value, presets, names, document)
      if (!from) return null
      return {
        path,
        // Two sentences rather than one with the kind interpolated: copying a
        // bundled preset and copying a profile somebody already tuned are two
        // different provenances, and the second one is shared with live projects.
        sentence: from.bundled
          ? i18nT('apps.specEngine.profilesForm.edit_copies_the_bundled_preset', {
              profile,
              preset: from.name,
              path,
            })
          : i18nT('apps.specEngine.profilesForm.edit_copies_the_existing_profile', {
              profile,
              preset: from.name,
              path,
            }),
      }
    }
    if (rest.length === 3 && rest[0] === ROLES_KEY && ROLE_FIELD_SENTENCE_KEY[rest[2]]) {
      return {
        path,
        // Indexed at the call site rather than through a local, so the
        // key-reference gate resolves every entry in the table.
        sentence: i18nT(ROLE_FIELD_SENTENCE_KEY[rest[2]], {
          profile,
          role: rest[1],
          oldValue: shownValue(nodeAt(document, edit.segments)),
          newValue: shownValue(edit.value),
          path,
        }),
      }
    }
    if (rest.length === 2) {
      const setting = `${rest[0]}.${rest[1]}`
      return {
        path,
        sentence: i18nT('apps.specEngine.profilesForm.edit_replaces_the_pinned_limit', {
          setting: settingLabel(setting) || setting,
          profile,
          oldValue: shownValue(nodeAt(document, edit.segments)),
          newValue: shownValue(edit.value),
          path,
        }),
      }
    }
    return null
  }

  // The staged edits this form can account for, and the patch built from exactly
  // those. One list for both, for the reason above.
  const reviewed: Array<{ edit: StagedEdit; change: ReviewedChange }> = []
  for (const edit of edits.edits) {
    const change = describe(edit)
    if (change) reviewed.push({ edit, change })
  }
  const patch = buildFormPatch(reviewed.map((entry) => entry.edit))
  const removing = reviewed.filter(({ edit }) => edit.value === DELETE)
  const addBlocked = pending === '' || names.includes(pending)

  return (
    <div className="se-blk">
      {/* The same number the "unwritten profile changes" line below states, read
          from the same list, so the tab badge cannot claim a count this form does
          not show. */}
      <PendingCount count={reviewed.length} onCount={onPendingCount} />
      <h3>{i18nT('apps.specEngine.profilesForm.cost_profiles')}</h3>
      {names.length === 0 ? (
        /* Not an empty role table: rows of unassignable roles read as "these roles
           have no model", when the fact is that no profile exists to assign them
           in. The add block below is the answer, so the sentence points at it. */
        <p className="se-note">
          {i18nT('apps.specEngine.profilesForm.no_cost_profile_is_defined')}
        </p>
      ) : (
        <>
          <div
            className="se-acts"
            role="group"
            aria-label={i18nT('apps.specEngine.profilesForm.select_a_cost_profile')}
          >
            {names.map((name) => (
              <button
                key={name}
                type="button"
                className="se-btn se-sm se-m"
                aria-pressed={name === selected}
                // The refusal acknowledgment is about a click on THIS profile's
                // remove control; carried across a switch it would caption another
                // profile's note with a refusal that never happened to it.
                onClick={() => {
                  setChosen(name)
                  setRemovalRefused(false)
                }}
              >
                {name}
              </button>
            ))}
          </div>
          {/* The consequence of every edit below, stated once above them all: the
              profile is shared, and the count is the document's own. */}
          <p className="se-note">
            {i18nT('apps.specEngine.profilesForm.the_values_apply_to_every_project', {
              profile: selected,
              count: fmtNumber(selecting.length),
            })}
          </p>
          {fields.length === 0 ? (
            <p className="se-note">{i18nT('apps.specEngine.profilesForm.no_role_is_registered')}</p>
          ) : (
            <div className="se-settings">
              {fields.map((field) => (
                <RoleAssignmentRow
                  key={field.role}
                  field={field}
                  efforts={efforts}
                  stagedModel={edits.stagedAt(field.modelSegments)}
                  stagedEffort={edits.stagedAt(field.effortSegments)}
                  onModel={(value) => stageField(field.modelSegments, value)}
                  onEffort={(effort) => stageEffort(field, effort)}
                />
              ))}
            </div>
          )}
          <p className="se-note">{i18nT('apps.specEngine.profilesForm.the_model_is_free_text')}</p>
          <p className="se-note">
            {i18nT('apps.specEngine.profilesForm.an_effort_needs_a_model', { model: AUTO_MODEL })}
          </p>
          {pinned.length > 0 && (
            <>
              <h3>{i18nT('apps.specEngine.profilesForm.limits_this_profile_pins')}</h3>
              <div className="se-settings">
                {pinned.map((field) => (
                  <StoredSettingRow
                    key={field.setting.key}
                    field={field}
                    labels={{
                      stored: i18nT('apps.specEngine.profilesForm.stored_in_the_profile'),
                      notWritten: i18nT('apps.specEngine.profilesForm.not_written'),
                      notEditable: i18nT(
                        'apps.specEngine.profilesForm.the_registry_kind_is_not_editable_here',
                        { kind: field.setting.kind },
                      ),
                    }}
                    staged={edits.stagedAt(field.segments)}
                    onStage={(value) => stageField(field.segments, value)}
                    onWithdraw={() => {
                      touched()
                      edits.unstage(field.segments)
                    }}
                  />
                ))}
              </div>
              <p className="se-note">
                {i18nT('apps.specEngine.profilesForm.a_profile_may_pin_only_these_limits')}
              </p>
            </>
          )}
          <div className="se-acts" style={{ marginTop: 9 }}>
            <button
              type="button"
              className="se-btn se-sm se-danger"
              // The accessible name carries the target even though the visible
              // label is one word: a bare "Remove" is how somebody removes the
              // wrong profile.
              aria-label={i18nT('apps.specEngine.profilesForm.remove_the_profile', {
                profile: selected,
              })}
              onClick={() => stageRemoval(selected)}
            >
              {i18nT('apps.specEngine.configPanel.remove')}
            </button>
          </div>
          {selecting.length > 0 && (
            /* The refusal names the projects, in flow beside the control. A
               `disabled` button with no reason would leave an operator with no next
               action, and the next action is precisely to point these projects at
               another profile. The acknowledgment leads only after a refused click:
               the note is on screen BEFORE the click, and a `role="status"` region
               re-announces on content change, so the lead sentence is what makes
               the refusal an event rather than a standing caption. */
            <p className="se-note" role="status">
              {removalRefused && (
                <span>{i18nT('apps.specEngine.profilesForm.the_removal_was_refused')}</span>
              )}
              {removalRefused && SEP}
              <span>
                {i18nT('apps.specEngine.profilesForm.a_project_still_selects_the_profile', {
                  projects: selecting.join(', '),
                  profile: selected,
                })}
              </span>
            </p>
          )}
        </>
      )}

      <h3>{i18nT('apps.specEngine.profilesForm.add_a_cost_profile')}</h3>
      <div className="se-setting">
        <label className="se-setting-name" htmlFor="se-profile-add-name">
          {i18nT('apps.specEngine.profilesForm.name_for_the_new_profile')}
        </label>
        <input
          id="se-profile-add-name"
          type="text"
          className="se-input"
          value={addName}
          onChange={(event) => renameAdd(event.target.value)}
        />
        {pending !== '' && names.includes(pending) && (
          <p className="se-note" role="status">
            {i18nT('apps.specEngine.profilesForm.the_name_is_already_a_profile', {
              profile: pending,
            })}
          </p>
        )}
        {pending === '' && (
          <p className="se-note">{i18nT('apps.specEngine.profilesForm.name_the_profile_first')}</p>
        )}
      </div>
      {presets.length > 0 && (
        <div
          className="se-acts"
          role="group"
          aria-label={i18nT('apps.specEngine.profilesForm.copy_a_bundled_preset')}
        >
          {presets.map((preset) => (
            <button
              key={preset.name}
              type="button"
              className="se-btn se-sm se-m"
              disabled={addBlocked}
              onClick={() => stageCopy(preset.entry)}
            >
              {preset.name}
            </button>
          ))}
        </div>
      )}
      {names.length > 0 && (
        <div
          className="se-acts"
          role="group"
          aria-label={i18nT('apps.specEngine.profilesForm.copy_an_existing_profile')}
        >
          {names.map((name) => (
            <button
              key={name}
              type="button"
              className="se-btn se-sm se-m"
              disabled={addBlocked}
              onClick={() => stageCopy(nodeAt(document, profileSegments(name)))}
            >
              {name}
            </button>
          ))}
        </div>
      )}
      <p className="se-note">{i18nT('apps.specEngine.profilesForm.an_add_is_always_a_copy')}</p>
      {orphanedCopy !== '' && (
        /* The withdrawal is an event this form caused, so it is stated: a staged
           copy that silently stopped being counted would read as an edit that was
           never made. `role="status"` so the announcement reaches a reader who is
           not looking at the add block when the refetch lands. */
        <p className="se-note" role="status">
          {i18nT('apps.specEngine.profilesForm.the_staged_copy_was_withdrawn', {
            profile: orphanedCopy,
          })}
        </p>
      )}

      <div className="se-acts" style={{ marginTop: 9 }}>
        <button
          type="button"
          className="se-btn"
          disabled={reviewed.length === 0}
          onClick={() => setReviewing(true)}
        >
          {i18nT('apps.specEngine.profilesForm.review_the_exact_change')}
        </button>
        {reviewed.length > 0 && (
          <span className="se-lbl">
            {i18nT('apps.specEngine.profilesForm.unwritten_profile_changes')}
            {SEP}
            <span className="se-m">{fmtNumber(reviewed.length)}</span>
          </span>
        )}
      </div>
      {reviewing && reviewed.length > 0 && (
        <FormReview
          changes={reviewed.map((entry) => entry.change)}
          patch={patch}
          labels={{
            heading: i18nT('apps.specEngine.profilesForm.the_change_that_would_be_written'),
            confirm: i18nT('apps.specEngine.profilesForm.write_the_change'),
            writing: i18nT('apps.specEngine.configPanel.saving'),
            discard: i18nT('apps.specEngine.profilesForm.discard_the_pending_changes'),
            exactly: i18nT('apps.specEngine.profilesForm.a_confirm_writes_exactly_this_patch'),
            refusalTitle: i18nT('apps.specEngine.profilesForm.could_not_write_the_profile_change'),
            retained: i18nT(
              'apps.specEngine.profilesForm.nothing_was_written_so_the_profile_is_stored_state',
            ),
          }}
          consequences={
            removing.length > 0 && (
              /* In flow under the patch, never a dialog: a deletion's blast radius
                 is not legible in a `null`, and a consequence stated in an overlay
                 is one that can be dismissed. */
              <div className="se-arm">
                {removing.map(({ edit }) => (
                  <p key={dotted(edit.segments)}>
                    <AlertTriangle className="lucide-inline" aria-hidden="true" />
                    {i18nT('apps.specEngine.profilesForm.removing_deletes_the_profile', {
                      profile: edit.segments[1],
                      path: dotted(edit.segments),
                    })}
                  </p>
                ))}
              </div>
            )
          }
          writing={write.isPending}
          error={write.isError ? write.error : null}
          onConfirm={(sending) => write.mutate(sending)}
          onDiscard={discard}
        />
      )}
      {wrote && (
        <p className="se-note" role="status">
          {i18nT('apps.specEngine.profilesForm.wrote_the_change_and_re_read_the_profiles')}
        </p>
      )}
    </div>
  )
}

// --- the watch source form ----------------------------------------------------

/** Field of a source entry deciding whether the engine polls it at all. */
const SOURCE_ENABLED = 'enabled'

/** Field of a source entry naming the project its items are filed into. */
const SOURCE_PROJECT = 'project'

/** Field of a source entry listing the accounts classified as maintainers. */
const SOURCE_MAINTAINERS = 'maintainers'

/** Field holding the poll argv the engine EXECUTES. */
const SOURCE_POLL = 'poll'

/** Field holding where each engine item field sits in the poll's output. */
const SOURCE_FIELD_MAP = 'field_map'

/** Field naming the bundled preset an entry was copied from. */
const SOURCE_PRESET = 'preset'

/** Field declaring the source's items publicly submittable. */
const SOURCE_PUBLIC = 'public'

/**
 * The literal a bundled preset's poll argv holds where the repository belongs.
 *
 * The engine's own placeholder, spelled in `WATCH_SOURCE_PRESETS`: a poll has no run
 * context to substitute from, so the preset tables carry this literal rather than a
 * variable, precisely so a copy nobody parameterized is refused loudly instead of
 * polling somewhere unintended. Every preset the engine bundles ships it, which is
 * why a source that actually polls anything has an argv that is NOT byte-equal to
 * its preset's.
 *
 * Named here rather than derived, because a literal cannot be derived — but the
 * POSITIONS it sits at are never spelled: {@link designatedSlots} finds them in the
 * preset the read supplied, so a preset whose placeholder moves, or a preset the
 * engine adds, needs no change here.
 */
const SOURCE_REPO_PLACEHOLDER = 'OWNER/REPO'

/**
 * The two fields carrying what the engine runs and how it reads the output.
 *
 * Named as a pair because they are the pair no path this form composes FREELY.
 * `poll` is argv the engine executes, and the write door validates its SHAPE rather
 * than which program it names — so the boundary on what the engine runs is the
 * preset tables plus this form's refusal to compose either path from operator text,
 * and nothing downstream of that.
 *
 * The one exception is the repository parameter, and it is an exception in address
 * only: it stages `poll` as the matched preset's own argv with the placeholder in
 * {@link designatedSlots} filled, so the program, the argument COUNT and every other
 * argument stay the preset's. See {@link sourceEdit}.
 */
const SOURCE_ARGV_FIELDS: readonly string[] = [SOURCE_POLL, SOURCE_FIELD_MAP]

/**
 * The fields of a source entry this form writes. Deliberately these three.
 *
 * Exported because the claim is about the LIST: every write this form can make is
 * one of these three fields, a registry-typed per-source setting, a whole preset
 * copy, or a deletion — and none of those can carry an argument the engine runs. A
 * name added here without a control is a path nothing stages; a name added here
 * that the engine executes is the failure this whole form exists to prevent.
 */
export const SOURCE_FORM_FIELDS: readonly string[] = [
  SOURCE_ENABLED,
  SOURCE_PROJECT,
  SOURCE_MAINTAINERS,
]

/**
 * The fields this form displays without writing them.
 *
 * Shown rather than hidden: an operator deciding whether to arm a source is
 * deciding to run the command in `poll`, so the command is on screen in full.
 * `public` is on screen for the same reason and beside it: the enable control lives
 * on this form, and whether items arrive from anyone is what decides how much the
 * grid's submitter classes are actually load-bearing. `autonomy` is displayed by the
 * grid section this form links to, which is one resolution of it rather than a
 * second rendering here.
 */
const SOURCE_SHOWN_FIELDS: readonly string[] = [
  SOURCE_POLL,
  SOURCE_FIELD_MAP,
  SOURCE_PRESET,
  SOURCE_PUBLIC,
  AUTONOMY_KEY,
]

/** The preset vocabulary before the read answers. One constant, for `NO_SETTINGS`'
 *  reason: a memo over it must not see a fresh array on every render. */
const NO_SOURCE_PRESETS: readonly SourcePreset[] = []
/**
 * The watch sources the document declares, in the order it holds them.
 *
 * The document's own order rather than sorted, matching `source_names` in the
 * engine: the picker then lists sources in the order the file does, which is the
 * order an operator reading the JSON view sees.
 */
export function sourceNames(document: Document): string[] {
  const node = document[SOURCES]
  return isObject(node) ? Object.keys(node) : []
}

/**
 * The entry a preset copy stages: the preset's own bytes, still inert.
 *
 * Two properties, and both are why this is a function rather than a spread at a
 * call site:
 *
 * 1. **The commands are the preset's, byte for byte.** `poll` and `field_map` come
 *    out exactly as the engine's own table holds them, because this is a deep copy
 *    of the entry the read supplied and nothing here composes an argument. Since
 *    the write door checks an argv's shape and not which program it names, this is
 *    where "the engine runs only what it bundled" is actually decided.
 * 2. **`enabled` is ABSENT, not false.** The bundled table carries no such key on
 *    purpose — polling is the step that decides an unattended run may start at all,
 *    and a fresh copy still names the preset's placeholder repository. Removing the
 *    key rather than trusting the payload makes inert-by-default a property of this
 *    function, so a payload that ever carried one cannot arm a copy.
 *
 * Copied through JSON, which is what the entry is: a value a read handed over. The
 * read's own object must never become the staged value — an edit to the staged copy
 * would then change what the next copy is offered, and the cache is shared with
 * every other surface reading the registry.
 */
export function composeSource(preset: SourcePreset): Document {
  const entry = JSON.parse(JSON.stringify(preset.entry)) as Document
  delete entry[SOURCE_ENABLED]
  return entry
}

/**
 * One act the source form can perform, in the only vocabulary it has.
 *
 * A closed vocabulary rather than a free `(segments, value)` call, because that is
 * what makes the argv guarantee statable at all: {@link sourceEdit} is the ONE place
 * this form composes a path under `sources`, so the set of paths it can write is
 * exactly the set these five kinds produce.
 */
export type SourceFormAction =
  | { kind: 'add'; source: string; preset: SourcePreset; repository: string }
  | { kind: 'repository'; source: string; preset: SourcePreset; repository: string }
  | { kind: 'field'; source: string; field: string; value: unknown }
  | { kind: 'setting'; source: string; key: string; value: unknown }
  | { kind: 'remove'; source: string }

/**
 * The argv positions of *poll* that hold the repository placeholder.
 *
 * Derived from the preset the read supplied, never spelled as an index: the GitHub
 * preset carries the placeholder INSIDE a longer argument
 * (`repos/OWNER/REPO/issues?…`) while GitLab's is a whole argument of its own, and a
 * form holding either position as a number would substitute into the wrong argument
 * the first time a preset changed.
 *
 * **`argv[0]` is never a slot, and a placeholder there refuses the whole preset.**
 * Position zero is the PROGRAM the engine executes. Substituting there would let a
 * data field decide what runs, which is the one thing this form exists to prevent —
 * so such a preset gets no designated slot at all, its poll is expressible only byte
 * for byte, and it is offered no repository control. Defensive rather than
 * hypothetical: no bundled preset does this, and the refusal is what keeps it true.
 */
export function designatedSlots(poll: unknown): number[] {
  if (!Array.isArray(poll)) return []
  const slots: number[] = []
  for (let index = 0; index < poll.length; index += 1) {
    const argument = poll[index]
    if (typeof argument !== 'string' || !argument.includes(SOURCE_REPO_PLACEHOLDER)) continue
    if (index === 0) return []
    slots.push(index)
  }
  return slots
}

/**
 * Whether *repository* is shaped like the one thing a designated slot leaves
 * open: `owner/repo`.
 *
 * Letters, digits, dot, dash and underscore on each side of exactly ONE slash,
 * with no leading dash on either half and no whitespace. This is not hostname
 * validation — its job is to make the control's own promise ("a value, not a
 * command") structurally true: every character that could rewrite the argument
 * AROUND the slot is outside the set. A `?`, `#` or `&` would rewrite the query
 * the preset composed, a second `/` would move the endpoint, a leading `-` reads
 * as a flag to the program, and whitespace invites reading one argument as two.
 * The placeholder itself is a well-formed repository, so a fresh copy needs no
 * special case.
 */
export function wellFormedRepository(repository: string): boolean {
  if (!/^[A-Za-z0-9._][A-Za-z0-9._-]*\/[A-Za-z0-9._][A-Za-z0-9._-]*$/.test(repository)) {
    return false
  }
  // A half made ENTIRELY of dots re-targets the endpoint by path normalization —
  // `repos/../../issues` is not a repository under `repos/` — and no host permits
  // an owner or repo named `.` or `..`, so refusing them costs nothing.
  const [owner, repo] = repository.split('/')
  return !/^\.+$/.test(owner) && !/^\.+$/.test(repo)
}

/**
 * *argument* with every placeholder occurrence replaced by *repository*.
 *
 * The surrounding text is kept, which is the whole reason substitution is per
 * argument rather than per position: `repos/OWNER/REPO/issues?state=all` becomes
 * `repos/acme/widgets/issues?state=all`, and the query string the preset chose is
 * still the preset's. That sentence is enforced, not assumed: every caller passes
 * a value {@link wellFormedRepository} accepted, so the substitution cannot carry
 * the characters that would rewrite the argument around the slot.
 */
function fillSlot(argument: string, repository: string): string {
  return argument.split(SOURCE_REPO_PLACEHOLDER).join(repository)
}

/**
 * The repository *stored* holds where *template* holds the placeholder, or `null`
 * when *stored* is not *template* with one value filled in.
 *
 * The literal frame around the placeholder has to match byte for byte — an argument
 * that merely happens to be a string is not this preset's argument with a repository
 * named in it. The value itself is free text, because a repository name is the one
 * thing the preset deliberately left open.
 *
 * Stated over any number of occurrences rather than assuming one: the value's length
 * is fixed by what is left after the literal parts, so a template repeating the
 * placeholder matches only when every occurrence holds the SAME repository.
 */
function slotValue(template: string, stored: string): string | null {
  const parts = template.split(SOURCE_REPO_PLACEHOLDER)
  const gaps = parts.length - 1
  if (gaps <= 0) return null
  const literal = parts.reduce((sum, part) => sum + part.length, 0)
  const span = stored.length - literal
  if (span < 0 || span % gaps !== 0) return null
  const value = stored.slice(parts[0].length, parts[0].length + span / gaps)
  if (parts.join(value) !== stored) return null
  // The frame matching is not enough on its own: a stored value carrying `?`, `#`
  // or a second `/` reassembles into the frame perfectly while naming a DIFFERENT
  // endpoint or query than the preset composed. Such a poll is not "the preset's
  // argv with a repository named in it" — it is a hand-edited command, and the
  // JSON view owns it.
  if (value !== SOURCE_REPO_PLACEHOLDER && !wellFormedRepository(value)) return null
  return value
}

/** What a stored poll and a bundled preset's poll agree on. */
export interface PollMatch {
  /** The template's designated slots, empty when it has none. */
  readonly slots: readonly number[]
  /** The repository those slots hold; `''` when there are no slots. */
  readonly repository: string
}

/**
 * Whether *poll* is *template* with only its designated slots filled, and by what.
 *
 * This is the expressibility rule the form's edit surface turns on, and it is
 * MODULO the placeholder rather than byte-equal on purpose. Byte-equality was the
 * stricter rule and it was also the wrong one: the engine's presets ship a
 * placeholder that the project edits, there is no variable substitution in a poll,
 * and setup writes the placeholder verbatim — so under byte-equality every source
 * that actually polls anything is inexpressible, and the form could edit only copies
 * that cannot poll.
 *
 * What it still refuses is everything else: a different program, a different
 * argument count, a changed flag, a changed query string. The form therefore
 * describes an argv it can account for argument by argument, which is what a preset
 * name on screen is claiming.
 */
export function matchPoll(template: unknown, poll: unknown): PollMatch | null {
  if (!Array.isArray(template) || !Array.isArray(poll)) return null
  if (template.length !== poll.length) return null
  const slots = designatedSlots(template)
  let repository: string | null = null
  for (let index = 0; index < template.length; index += 1) {
    if (!slots.includes(index)) {
      if (JSON.stringify(template[index]) !== JSON.stringify(poll[index])) return null
      continue
    }
    const stored = poll[index]
    if (typeof stored !== 'string') return null
    const value = slotValue(String(template[index]), stored)
    if (value === null || (repository !== null && value !== repository)) return null
    repository = value
  }
  return { slots, repository: repository ?? '' }
}

/** The first bundled preset whose poll *poll* is, modulo the repository slots. */
function presetForPoll(
  poll: unknown,
  presets: readonly SourcePreset[],
): { preset: SourcePreset; match: PollMatch } | null {
  for (const preset of presets) {
    const match = matchPoll(preset.entry[SOURCE_POLL], poll)
    if (match) return { preset, match }
  }
  return null
}

/** Whether *repository* is the placeholder still, which is a poll that cannot run. */
function isPlaceholder(repository: string): boolean {
  return repository === SOURCE_REPO_PLACEHOLDER
}

/**
 * *preset*'s own poll argv with its designated slots holding *repository*.
 *
 * The value staged for the one argv path this form writes, and the reason that path
 * is safe to write: the array is built from the PRESET's argv rather than from
 * anything on screen, so the program is the preset's, the argument count is the
 * preset's, and every position outside a designated slot is byte-equal to the
 * preset's. A repository containing spaces, a flag, or a shell metacharacter stays
 * one argument — the engine runs argv with no shell, so text inside an argument
 * cannot become a new argument.
 *
 * An empty repository keeps the placeholder rather than emptying the argument: a
 * poll naming the literal is refused loudly, and a poll naming `repos//issues` is a
 * request against a repository that is not the one anybody meant.
 */
export function pollForRepository(preset: SourcePreset, repository: string): unknown[] | null {
  const template = preset.entry[SOURCE_POLL]
  if (!Array.isArray(template)) return null
  const slots = designatedSlots(template)
  if (slots.length === 0) return null
  const named = repository.trim() === '' ? SOURCE_REPO_PLACEHOLDER : repository.trim()
  // Refused, not substituted: a value that is not owner/repo could rewrite the
  // argument around the slot — the exact thing the designated-slot rule exists to
  // make impossible. The placeholder passes the shape check, so an empty value
  // still composes the preset's own inert copy.
  if (!wellFormedRepository(named)) return null
  return template.map((argument, index) =>
    slots.includes(index) ? fillSlot(String(argument), named) : argument,
  )
}

/**
 * The entry an add stages: *preset*'s own bytes with its repository named.
 *
 * One edit rather than a copy followed by a parameter edit, because the staged list
 * is pairwise non-overlapping by construction — `sources.<name>` and
 * `sources.<name>.poll` cannot both survive into one patch — and an edit silently
 * dropped by the reconciliation is the failure the shared machinery exists to
 * prevent. The review card states BOTH facts about this one edit: which preset it
 * copies, and which repository it names.
 */
function composeSourceFor(preset: SourcePreset, repository: string): Document {
  const entry = composeSource(preset)
  const poll = pollForRepository(preset, repository)
  if (poll !== null) entry[SOURCE_POLL] = poll
  return entry
}

/**
 * The one staged edit an action makes, or `null` for an action with no address.
 *
 * `null` rather than a guessed path for every way an action cannot be composed: an
 * unnamed source, a field outside {@link SOURCE_FORM_FIELDS}, a registry key with
 * no group — and a setting whose group would land on one of the argv fields. That
 * last guard is not hypothetical caution: the schema keeps setting groups and
 * source fields disjoint, but a group named `poll` would address the command the
 * engine executes, and the guard belongs where the path is composed rather than
 * resting on the schema's promise.
 *
 * The `repository` action is the only one that addresses an argv field, and it is
 * still not a command control: its value is {@link pollForRepository}'s — the
 * preset's own argv with the placeholder filled — so a `null` template, a preset
 * with no designated slot, and a preset whose placeholder sits on the program are
 * all refusals rather than a substitution.
 */
export function sourceEdit(action: SourceFormAction): StagedEdit | null {
  if (action.source.trim() === '') return null
  if (action.kind === 'add') {
    return {
      segments: [SOURCES, action.source],
      value: composeSourceFor(action.preset, action.repository),
    }
  }
  if (action.kind === 'remove') {
    return { segments: [SOURCES, action.source], value: DELETE }
  }
  if (action.kind === 'repository') {
    const poll = pollForRepository(action.preset, action.repository)
    if (poll === null) return null
    return { segments: [SOURCES, action.source, SOURCE_POLL], value: poll }
  }
  if (action.kind === 'field') {
    if (!SOURCE_FORM_FIELDS.includes(action.field)) return null
    return { segments: [SOURCES, action.source, action.field], value: action.value }
  }
  const segments = settingSegments(action.key, SCOPE_SOURCE, action.source)
  if (segments === null || SOURCE_ARGV_FIELDS.includes(segments[2])) return null
  return { segments, value: action.value }
}

/** The setting leaves a source may hold, keyed by the group holding them. */
function sourceSettingGroups(settings: readonly RegistrySetting[]): Map<string, Set<string>> {
  const groups = new Map<string, Set<string>>()
  for (const setting of settings) {
    if (!setting.scopes.includes(SCOPE_SOURCE)) continue
    const leaf = settingLeaf(setting.key)
    if (!leaf) continue
    const found = groups.get(leaf[0]) ?? new Set<string>()
    found.add(leaf[1])
    groups.set(leaf[0], found)
  }
  return groups
}

/** What this form can say about one stored source entry. */
export interface SourceShape {
  /** The bundled preset whose poll argv the entry carries, or `null` for none. */
  preset: SourcePreset | null
  /** That preset's designated repository slots, empty when it has none. */
  slots: readonly number[]
  /** The repository the entry's poll names; the placeholder when unsubstituted. */
  repository: string
  /** The entry's own keys this form neither writes nor displays, in order. */
  unexpressed: readonly string[]
  /** Whether the form can express the WHOLE entry. */
  expressible: boolean
}

/**
 * Whether the form can express a stored entry, and what stops it when it cannot.
 *
 * Both halves have to hold.
 *
 * A `poll` no bundled preset supplied cannot be described as a preset's — and
 * describing what the engine runs is the whole of what this form offers in place of
 * a command field. It is also the arming risk: the one edit here that starts
 * execution is `enabled`, so a form that offered it over argv nobody bundled would
 * be a way to arm a command this surface never constrained.
 *
 * Supplied means {@link matchPoll}'s rule — the preset's argv with only its
 * designated repository slots filled. Not byte-equality: the presets ship a
 * placeholder the project is expected to replace, so byte-equality would call every
 * source that actually polls anything inexpressible and leave the form editing only
 * copies that cannot run.
 *
 * And a key the form neither edits nor displays is a field a partial form would
 * hide while the operator confirmed a write. The requirement's own words are that
 * such an entry routes to the JSON view rather than rendering a form that rewrites
 * fields it did not show.
 *
 * A setting group is expressible LEAF BY LEAF: a group holding a leaf no registry
 * record describes has no kind, no bounds and no summary to generate a control
 * from, so showing its siblings does not express it.
 *
 * The residual, stated because it is a real cost rather than an oversight: an entry
 * whose poll was hand-edited beyond its repository — a changed flag, an added
 * argument, another program — is owned by the JSON view from then on. That is the
 * honest direction to fail in, since the alternative is a form that arms sources
 * whose commands it cannot vouch for argument by argument.
 */
export function sourceShape(
  entry: unknown,
  presets: readonly SourcePreset[],
  settings: readonly RegistrySetting[],
): SourceShape {
  const none = { preset: null, slots: [], repository: '', unexpressed: [], expressible: false }
  if (!isObject(entry)) return none
  const found = presetForPoll(entry[SOURCE_POLL], presets)
  const groups = sourceSettingGroups(settings)
  const unexpressed: string[] = []
  for (const key of Object.keys(entry)) {
    if (SOURCE_FORM_FIELDS.includes(key) || SOURCE_SHOWN_FIELDS.includes(key)) continue
    const leaves = groups.get(key)
    const value = entry[key]
    if (leaves && isObject(value) && Object.keys(value).every((leaf) => leaves.has(leaf))) continue
    unexpressed.push(key)
  }
  return {
    preset: found ? found.preset : null,
    slots: found ? found.match.slots : [],
    repository: found ? found.match.repository : '',
    unexpressed,
    expressible: found !== null && unexpressed.length === 0,
  }
}

/**
 * The preset a staged entry is a copy OF and the repository it names, or `null`
 * when it is a copy of nothing.
 *
 * Derived from the staged bytes rather than remembered from the click, for
 * {@link copySourceOf}'s reason: the review card CLAIMS a provenance, and a claim
 * checked against what is actually staged cannot describe a copy of something else.
 * An entry matching no preset earns no sentence and therefore reaches no patch —
 * the second place, after {@link sourceEdit}, where an entry carrying argv nobody
 * bundled is stopped before it can be written.
 *
 * The check is byte-equality against the preset's own composed entry with the
 * repository this entry's poll names filled in — so a copy is recognized whatever
 * repository it was staged for, and an entry differing anywhere ELSE is not
 * recognized at all.
 */
function sourcePresetOf(
  value: unknown,
  presets: readonly SourcePreset[],
): { preset: SourcePreset; repository: string } | null {
  if (!isObject(value)) return null
  const staged = JSON.stringify(value)
  for (const preset of presets) {
    const match = matchPoll(preset.entry[SOURCE_POLL], value[SOURCE_POLL])
    const repository = match ? match.repository : ''
    if (JSON.stringify(composeSourceFor(preset, repository)) === staged) {
      return { preset, repository }
    }
  }
  return null
}

/** One row per registry setting a source may hold, in the registry's order. */
function sourceSettingFields(
  source: string,
  settings: readonly RegistrySetting[],
  document: Document,
): StoredSettingField[] {
  const fields: StoredSettingField[] = []
  for (const setting of settings) {
    if (!setting.scopes.includes(SCOPE_SOURCE)) continue
    const segments = settingSegments(setting.key, SCOPE_SOURCE, source)
    if (segments === null) continue
    fields.push({ setting, segments, stored: nodeAt(document, segments) })
  }
  return fields
}

/**
 * Which sentence describes an enablement edit, keyed by what it would store.
 *
 * Keys rather than resolved strings, for `ORIGIN_KEY`'s reason. Two sentences
 * rather than one with the value interpolated, because they are opposite acts: one
 * starts unattended ingestion and the other stops it.
 */
const SOURCE_ENABLED_SENTENCE_KEY: Record<string, string> = {
  true: 'apps.specEngine.sourceForm.edit_enables_the_source',
  false: 'apps.specEngine.sourceForm.edit_disables_the_source',
}

/** Which sentence describes a field edit, keyed by the field it addresses. */
const SOURCE_FIELD_SENTENCE_KEY: Record<string, string> = {
  project: 'apps.specEngine.sourceForm.edit_replaces_the_project_binding',
  maintainers: 'apps.specEngine.sourceForm.edit_replaces_the_maintainers',
}

/** A maintainer list as one line of text, and back. Commas, because the values
 *  are account names and the engine stores them as a list of strings. */
function maintainerText(stored: unknown): string {
  return Array.isArray(stored) ? stored.map((entry) => String(entry)).join(', ') : ''
}

/** The accounts a typed line names, empty entries dropped. */
function maintainerList(text: string): string[] {
  return text
    .split(',')
    .map((entry) => entry.trim())
    .filter((entry) => entry !== '')
}

/**
 * What a source runs, how it reads the output, and who may submit to it, read-only.
 *
 * On screen rather than folded away, because this is the whole substitute for a
 * command field: an operator arming a source is deciding to run this argv, and a
 * form that hid it while offering an enable control would be asking for a decision
 * with the subject withheld. Rendered as the JSON array it is, for the review
 * card's reason — the payload itself, not a shell-looking rendering of it that
 * would imply a shell the engine does not use.
 *
 * `public` is here for the same reason and not one row further away: it is the
 * difference between a feed whose authors are known and one anyone can file into,
 * and the enable control that arms unattended ingestion sits on this same form.
 */
function SourceCommand({ entry, preset }: { entry: Document; preset: SourcePreset }) {
  const map = entry[SOURCE_FIELD_MAP]
  const fields = isObject(map) ? Object.entries(map) : []
  return (
    <>
      <dl className="se-kv">
        <dt>{i18nT('apps.specEngine.sourceForm.the_preset_host')}</dt>
        <dd>{preset.host}</dd>
        <dt>{i18nT('apps.specEngine.sourceForm.the_program_it_runs')}</dt>
        <dd>{preset.program}</dd>
        <dt>{i18nT('apps.specEngine.sourceForm.the_source_is_public')}</dt>
        {/* The stored value verbatim, absent included: `public` is a field the form
            displays without writing, so it says what is there rather than resolving
            a default the engine owns. */}
        <dd data-source-shown={SOURCE_PUBLIC}>{shownValue(entry[SOURCE_PUBLIC])}</dd>
      </dl>
      {entry[SOURCE_PUBLIC] === true && (
        <p className="se-note">
          {i18nT('apps.specEngine.sourceForm.public_items_come_from_anyone')}
        </p>
      )}
      <p className="se-note">{i18nT('apps.specEngine.sourceForm.the_poll_command')}</p>
      <pre className="se-json">{JSON.stringify(entry[SOURCE_POLL] ?? null, null, 2)}</pre>
      {fields.length > 0 && (
        <>
          <p className="se-note">{i18nT('apps.specEngine.sourceForm.the_field_map')}</p>
          <dl className="se-kv">
            {fields.map(([field, path]) => (
              <Fragment key={field}>
                <dt>{field}</dt>
                <dd>{String(path)}</dd>
              </Fragment>
            ))}
          </dl>
        </>
      )}
      <p className="se-note">
        {i18nT('apps.specEngine.sourceForm.the_form_cannot_change_the_command')}
      </p>
    </>
  )
}

/**
 * The watch sources, as forms: create from a preset, edit, and remove.
 *
 * A source is a poll command the engine EXECUTES plus a map from its output to the
 * engine's item fields, so this form is the one surface on the pane whose subject
 * is something that runs. Six properties of it are claims rather than arrangement:
 *
 * 1. **No control here accepts a command or an argument.** Not the add flow, not
 *    the edit flow, not indirectly: {@link sourceEdit} is the only path composer,
 *    and the paths it can produce are three named fields, a registry-typed
 *    per-source setting, a whole preset copy, the repository slot of a matched
 *    preset's own argv, and a deletion. The commands come from the engine's own
 *    preset tables, because the write door checks an argv's shape and not which
 *    program it names.
 * 2. **The repository is a value, not a command.** The presets ship an `OWNER/REPO`
 *    placeholder the project is expected to name, so the form names it — by staging
 *    the PRESET's argv with {@link designatedSlots} filled. The program, the
 *    argument count and every other argument stay the preset's, and the engine runs
 *    argv with no shell, so text inside one argument cannot become another.
 * 3. **A copy arrives inert.** The preset entries carry no `enabled` key and
 *    {@link composeSource} keeps it absent, so creating a source ingests nothing
 *    until a separate, separately confirmed write arms it.
 * 4. **An enable states what it starts.** Arming a source is arming unattended
 *    ingestion under whatever its autonomy grid resolves, so the review card says
 *    so and links to that grid, and the form states that a source with no grid
 *    grants nothing at all. A poll still naming the placeholder gets its own
 *    sentence, because what that arms is a command that cannot run.
 * 5. **A shape the form cannot express gets no form.** Not a partial one: a form
 *    rendering three of an entry's eight fields invites a confirm over the five it
 *    never showed. The state says which fields those are and routes to the JSON
 *    view — but it still offers the REMOVAL, because a deletion writes no field and
 *    so cannot rewrite one the state did not show.
 * 6. **A removal names its source.** The confirmation is the source's own name,
 *    typed, because a column of identical Remove controls is how the wrong source
 *    goes — and the review card then states that ingestion stops.
 * 7. **A success re-reads.** This mutation owns the invalidation: the review card
 *    is presentational and cannot do it for its callers. The document is where
 *    every row here comes from, and the grid beside it is a resolution of the very
 *    entries this write changes.
 *
 * What it deliberately does NOT offer is a rename. Renaming an entry is deleting one
 * key and writing another — the whole entry rewritten, including every field this
 * form does not show — so the name is displayed as the key it is and a rename is the
 * JSON view's.
 */
export function SourceForm({
  config,
  onShowGrid,
  onOpenJson,
  onPendingCount,
}: {
  config: ConfigSnapshot
  /** Select *source* in the autonomy grid section this form links to. */
  onShowGrid: (source: string) => void
  /** Open the JSON view, for a stored shape no form can express. */
  onOpenJson: () => void
  /** Report how many staged changes this form would review, for the tab badge. */
  onPendingCount?: (count: number) => void
}) {
  const client = useQueryClient()
  const edits = useStagedEdits()
  const [chosen, setChosen] = useState('')
  const [addName, setAddName] = useState('')
  // The repository the add block would name inside the copied preset's own argv.
  // Component state rather than a staged edit: an add stages ONE edit at
  // `sources.<name>`, because a second edit at `sources.<name>.poll` overlaps it and
  // the shared staging drops one of two overlapping paths rather than letting the
  // patch decide which survives.
  const [addRepo, setAddRepo] = useState('')
  // The source whose removal is armed, and the name typed to confirm it. A name
  // rather than a boolean: an arm that outlived a selection change would offer a
  // confirmation captioned with one source and staged against another.
  const [armed, setArmed] = useState<string | null>(null)
  const [typedName, setTypedName] = useState('')
  const [reviewing, setReviewing] = useState(false)
  const [wrote, setWrote] = useState(false)
  // The last removal confirmation that was refused, and the staged copy the form
  // had to withdraw. Both exist so an outcome this form CAUSED is stated rather
  // than inferred from the absence of a change: a refused confirm with no new
  // feedback looks inert, and a withdrawn edit with no sentence looks like it was
  // never made.
  const [removalRefused, setRemovalRefused] = useState(false)
  const [orphanedCopy, setOrphanedCopy] = useState('')
  // The repository text as typed, and the refusals the two repository controls
  // have stated. The buffer exists because the edit-flow control otherwise
  // derives its value from the STAGED argv, and a refused keystroke would snap
  // the input back — an operator cannot correct text they cannot finish typing.
  // `null` mirrors the staged/stored reading; a string is mid-edit text.
  const [repoInput, setRepoInput] = useState<string | null>(null)
  const [repoRefused, setRepoRefused] = useState(false)
  // '' = no refusal; 'shape' = the typed value is not owner/repo; otherwise the
  // host of a chosen preset that has no slot to put the typed repository in.
  const [addRefused, setAddRefused] = useState('')

  const registry = useQuery({
    queryKey: QK.registry,
    queryFn: () => specEngineApi.configRegistry(),
    retry: false,
    // Bundled vocabulary: a projection of the engine's own constants, so it cannot
    // change while the page is open. The same key the forms above read, so all
    // three share one answer and one request.
    staleTime: Infinity,
  })

  const write = useMutation({
    mutationFn: (patch: Document) => specEngineApi.writeConfig(patch),
    onSuccess: () => {
      edits.clear()
      setAddName('')
      setAddRepo('')
      setArmed(null)
      setTypedName('')
      // The refusal and buffer states go with the text they were about: after a
      // successful write the form re-derives everything from the fresh read, and a
      // refusal caption over an emptied box would be an outcome nothing caused.
      setRepoInput(null)
      setRepoRefused(false)
      setAddRefused('')
      setReviewing(false)
      setWrote(true)
      // The reply's merged document is NOT adopted: the read is this pane's
      // authority on what is persisted, and every row here is a reading OF that
      // document. All three keys are named because they are three readings a
      // reader would otherwise have to know the key layout to see refreshed — and
      // the grid below is one of them, since a source this form creates or removes
      // appears in or leaves that matrix.
      void client.invalidateQueries({ queryKey: QK.config })
      void client.invalidateQueries({ queryKey: QK_RESOLVED_ROOT })
      void client.invalidateQueries({ queryKey: QK.sources })
    },
    // No `onError`: a refusal must leave the staged edits in place and the queries
    // untouched, so the form keeps showing the store's own state.
  })

  const document = config.document
  const names = useMemo(() => sourceNames(document), [document])
  const presets = registry.data?.source_presets ?? NO_SOURCE_PRESETS
  const settings = registry.data?.settings ?? NO_SETTINGS
  const projects = useMemo(() => {
    const node = document[PROJECTS]
    return isObject(node) ? Object.keys(node) : []
  }, [document])
  // Normalized against the document rather than trusted: a source removed here, in
  // the JSON view, or by another surface must not leave the form editing a name the
  // document no longer carries.
  const selected = names.includes(chosen) ? chosen : (names[0] ?? '')
  const pending = addName.trim()

  // Every source a staged edit may address: one the document holds, or the one the
  // add block is naming. An edit whose source has left the document is dropped
  // rather than carried, because its patch would RESURRECT that source carrying one
  // field — a `sources` entry with an `enabled` and no poll command — and no
  // sentence on the card would say so. That removal can arrive from this pane, from
  // the JSON view, or on any refetch, which is why this reconciles against the
  // current answer instead of trusting the document to hold still between an edit
  // and its confirm.
  const { reconcile } = edits
  useEffect(() => {
    reconcile(
      (edit) =>
        edit.segments.length >= 2 &&
        edit.segments[0] === SOURCES &&
        (names.includes(edit.segments[1]) || edit.segments[1] === pending),
    )
  }, [names, pending, reconcile])

  // A staged copy stays reviewable only while it is still an ADD of a preset: its
  // provenance is derived from its bytes, and its being an add depends on the name
  // still being free. Both can stop holding under it — the document can gain that
  // name from the JSON view, from another surface, or on any refetch, and the copy
  // would then MERGE into the source of that name key by key rather than add one.
  // Leaving the edit staged would make it silently absent from both the card and
  // the write, so it is withdrawn, and the withdrawal is stated where the copy was
  // made.
  const { edits: stagedNow, unstage } = edits
  useEffect(() => {
    const presetsNow = registry.data?.source_presets
    if (!presetsNow) return
    for (const edit of stagedNow) {
      if (edit.segments.length !== 2 || edit.segments[0] !== SOURCES) continue
      if (edit.value === DELETE) continue
      if (sourcePresetOf(edit.value, presetsNow) === null || names.includes(edit.segments[1])) {
        unstage(edit.segments)
        setOrphanedCopy(edit.segments[1])
      }
    }
  }, [stagedNow, unstage, registry.data, names])

  // An arm outlives the entry it names unless it is withdrawn: another surface, or
  // the JSON view beside this form, can delete the entry while the confirmation
  // sits on screen — and a confirm then stages a deletion for a key that is gone.
  useEffect(() => {
    if (armed !== null && !names.includes(armed)) setArmed(null)
  }, [armed, names])

  // The repository buffer and its refusal are about the SELECTED source's control;
  // carried across a switch they would caption another source's input with text —
  // and a refusal — that was never typed at it.
  useEffect(() => {
    setRepoInput(null)
    setRepoRefused(false)
  }, [selected])

  // `isError` before the data: React Query keeps the last successful answer across
  // a failing refetch, and a preset list nobody re-read would describe commands
  // this form then claims the engine bundles.
  if (registry.isError) {
    return (
      <div className="se-blk">
        {/* What it HOLDS, not what it can review: with no presets the form cannot
            say what any staged edit means, and a tab badge that dropped to zero
            here would report unwritten work as gone. */}
        <PendingCount count={edits.edits.length} onCount={onPendingCount} />
        <h3>{i18nT('apps.specEngine.sourceForm.watch_source_definitions')}</h3>
        <Refused
          title={i18nT('apps.specEngine.sourceForm.could_not_read_the_source_presets')}
          error={registry.error}
        />
      </div>
    )
  }
  if (registry.isPending || !registry.data) {
    // Distinct from an empty preset list on purpose: "the engine bundles no preset"
    // is a fact about the engine, and "not read yet" is a fact about this request.
    return (
      <div className="se-blk">
        <PendingCount count={edits.edits.length} onCount={onPendingCount} />
        <h3>{i18nT('apps.specEngine.sourceForm.watch_source_definitions')}</h3>
        <p className="se-note">{i18nT('apps.specEngine.sourceForm.reading_the_source_presets')}</p>
      </div>
    )
  }

  const entry = nodeAt(document, [SOURCES, selected])
  const shape = sourceShape(entry, presets, settings)
  const stored = isObject(entry) ? entry : {}
  const sourceSettings = selected === '' ? [] : sourceSettingFields(selected, settings, document)

  // Plain functions rather than `useCallback`: each closes over the mutation
  // object, which React Query hands back fresh on every render, so a memo here
  // would advertise a stability it cannot have.
  const touched = () => {
    setWrote(false)
    setRemovalRefused(false)
    setOrphanedCopy('')
    write.reset()
  }

  /** Stage *action*, or withdraw it when it would write what is already there. */
  const act = (action: SourceFormAction) => {
    touched()
    const edit = sourceEdit(action)
    if (!edit) return
    if (edit.value === DELETE) {
      edits.stage(edit.segments, edit.value)
      return
    }
    // Writing back exactly what this path already stores is not a change, and every
    // write is recorded: staging it would put a line in the durable write record for
    // an edit nobody made. Compared against the DOCUMENT node at the path rather
    // than a resolution, because a source's own fields are stored where they are
    // read — there is no precedence between the two to disagree about.
    const at = nodeAt(document, edit.segments)
    if (JSON.stringify(at ?? null) === JSON.stringify(edit.value ?? null)) {
      edits.unstage(edit.segments)
      return
    }
    edits.stage(edit.segments, edit.value)
  }

  const stageEnabled = (next: boolean) => {
    const at = stored[SOURCE_ENABLED]
    // Absent and false are one posture — the engine polls neither — so unchecking a
    // source that stores no `enabled` withdraws rather than writing a key that
    // changes nothing about whether it is polled.
    if (next === false && at === undefined) {
      touched()
      edits.unstage([SOURCES, selected, SOURCE_ENABLED])
      return
    }
    act({ kind: 'field', source: selected, field: SOURCE_ENABLED, value: next })
  }

  const stageCopy = (preset: SourcePreset) => {
    touched()
    // Refused rather than merged: the store's merge would fold the copy INTO the
    // existing source key by key, which is an edit to that source — and one that
    // could leave its poll command half from one preset and half from another.
    if (pending === '' || names.includes(pending)) return
    const trimmed = addRepo.trim()
    // A typed repository that cannot reach the copy is refused with a statement,
    // never dropped: composing anyway would write an entry that ignores what the
    // operator just typed, with a review sentence that never mentions it.
    if (trimmed !== '' && !wellFormedRepository(trimmed)) {
      setAddRefused('shape')
      return
    }
    const template = preset.entry[SOURCE_POLL]
    if (
      trimmed !== '' &&
      (!Array.isArray(template) || designatedSlots(template).length === 0)
    ) {
      setAddRefused(preset.host)
      return
    }
    setAddRefused('')
    const edit = sourceEdit({ kind: 'add', source: pending, preset, repository: addRepo })
    if (edit) edits.stage(edit.segments, edit.value)
  }

  /**
   * Name the repository the add block's copy would poll.
   *
   * A staged copy is re-composed rather than left alone: the operator is looking at
   * one pending add, and a repository typed after the preset was picked has to reach
   * the entry that will actually be written. The preset comes from the STAGED bytes,
   * so the re-compose cannot silently switch which preset the copy is of.
   */
  const nameAddRepository = (next: string) => {
    touched()
    setAddRepo(next)
    const trimmed = next.trim()
    // Refused and SAID, before any re-compose: a malformed value never reaches the
    // staged copy, so whatever repository the copy already names is still the one
    // its review sentence describes.
    if (trimmed !== '' && !wellFormedRepository(trimmed)) {
      setAddRefused('shape')
      return
    }
    setAddRefused('')
    const staged = pending === '' ? undefined : edits.stagedAt([SOURCES, pending])
    if (!staged || staged.value === DELETE) return
    const copy = sourcePresetOf(staged.value, presets)
    if (!copy) return
    const edit = sourceEdit({
      kind: 'add',
      source: pending,
      preset: copy.preset,
      repository: next,
    })
    if (edit) edits.stage(edit.segments, edit.value)
  }

  /** Name the repository the SELECTED source polls, inside its preset's own argv. */
  const nameRepository = (next: string) => {
    if (shape.preset === null) return
    setRepoInput(next)
    const trimmed = next.trim()
    // Refused and SAID: sourceEdit would refuse this value anyway, but a silent
    // refusal under a live input reads as a control that does nothing, and the
    // previously staged repository would keep sitting in the patch unexplained —
    // so the stale staged poll is withdrawn along with the statement.
    if (trimmed !== '' && !wellFormedRepository(trimmed)) {
      touched()
      edits.unstage([SOURCES, selected, SOURCE_POLL])
      setRepoRefused(true)
      return
    }
    setRepoRefused(false)
    act({ kind: 'repository', source: selected, preset: shape.preset, repository: next })
  }

  const renameAdd = (next: string) => {
    touched()
    const staged = pending === '' ? undefined : edits.stagedAt([SOURCES, pending])
    if (staged) edits.unstage([SOURCES, pending])
    const trimmed = next.trim()
    // A staged copy MOVES with the name rather than being dropped: an operator who
    // picked a preset and then reconsidered the name meant to keep the copy.
    if (staged && trimmed !== '' && !names.includes(trimmed)) {
      edits.stage([SOURCES, trimmed], staged.value)
    }
    setAddName(next)
  }

  const confirmRemoval = () => {
    if (armed === null) return
    touched()
    // The name has to match, and a mismatch is ACKNOWLEDGED rather than ignored:
    // the confirmation is on screen before the click, so without the statement a
    // refused confirm would look like a control that does nothing.
    if (typedName.trim() !== armed) {
      setRemovalRefused(true)
      return
    }
    act({ kind: 'remove', source: armed })
    setArmed(null)
    setTypedName('')
  }

  const discard = () => {
    edits.clear()
    // Discard abandons the whole pending posture, refusals included: what was
    // refused was part of what is being discarded.
    setRepoInput(null)
    setRepoRefused(false)
    setAddRefused('')
    setReviewing(false)
    setWrote(false)
    write.reset()
  }

  /**
   * One staged edit as the review card reads it, or `null` when this form cannot
   * say what the edit means.
   *
   * The patch is built from exactly the edits this returns a sentence for, so the
   * card can never show a line it cannot explain and a write can never carry one.
   * That is also the last gate on a whole-entry stage: an entry matching no
   * bundled preset earns no sentence, so it reaches no patch.
   */
  const describe = (edit: StagedEdit): ReviewedChange | null => {
    const path = dotted(edit.segments)
    const source = edit.segments[1]
    const rest = edit.segments.slice(2)
    if (rest.length === 0) {
      if (edit.value === DELETE) {
        return {
          path,
          sentence: i18nT('apps.specEngine.sourceForm.edit_removes_the_source', { source, path }),
        }
      }
      const from = sourcePresetOf(edit.value, presets)
      if (!from) return null
      // Two sentences for one edit, because the edit is two decisions: which preset
      // the entry copies, and which repository its command names. A copy staged for
      // a real repository described only as "a copy of the github preset" would be a
      // card that named the provenance and hid the target.
      if (from.repository === '' || isPlaceholder(from.repository)) {
        return {
          path,
          sentence: i18nT('apps.specEngine.sourceForm.edit_copies_the_bundled_preset', {
            source,
            preset: from.preset.host,
            program: from.preset.program,
            path,
          }),
        }
      }
      return {
        path,
        sentence: i18nT('apps.specEngine.sourceForm.edit_copies_the_preset_for_repository', {
          source,
          preset: from.preset.host,
          program: from.preset.program,
          repository: from.repository,
          path,
        }),
      }
    }
    if (rest.length === 1 && rest[0] === SOURCE_POLL) {
      // The one argv path this form writes, and the sentence names the two things
      // that make it safe: the preset whose argv it still is, and the repository
      // that is the only part of it this edit changed.
      const was = presetForPoll(nodeAt(document, edit.segments), presets)
      const now = presetForPoll(edit.value, presets)
      if (!now) return null
      return {
        path,
        sentence: i18nT('apps.specEngine.sourceForm.edit_names_the_repository', {
          source,
          preset: now.preset.host,
          oldValue: was ? was.match.repository : NONE,
          newValue: now.match.repository,
          path,
        }),
      }
    }
    if (rest.length === 1 && rest[0] === SOURCE_ENABLED) {
      // Indexed at the call site rather than through a local, so the
      // key-reference gate resolves every entry in the table.
      return {
        path,
        sentence: i18nT(SOURCE_ENABLED_SENTENCE_KEY[String(edit.value === true)], {
          source,
          path,
        }),
      }
    }
    if (rest.length === 1 && SOURCE_FIELD_SENTENCE_KEY[rest[0]]) {
      return {
        path,
        sentence: i18nT(SOURCE_FIELD_SENTENCE_KEY[rest[0]], {
          source,
          oldValue: shownValue(nodeAt(document, edit.segments)),
          newValue: shownValue(edit.value),
          path,
        }),
      }
    }
    if (rest.length === 2) {
      const setting = `${rest[0]}.${rest[1]}`
      return {
        path,
        sentence: i18nT('apps.specEngine.sourceForm.edit_replaces_the_source_limit', {
          setting: settingLabel(setting) || setting,
          source,
          oldValue: shownValue(nodeAt(document, edit.segments)),
          newValue: shownValue(edit.value),
          path,
        }),
      }
    }
    return null
  }

  // The staged edits this form can account for, and the patch built from exactly
  // those. One list for both, for the reason above.
  const reviewed: Array<{ edit: StagedEdit; change: ReviewedChange }> = []
  for (const edit of edits.edits) {
    const change = describe(edit)
    if (change) reviewed.push({ edit, change })
  }
  const patch = buildFormPatch(reviewed.map((entry) => entry.edit))
  const removing = reviewed.filter(({ edit }) => edit.value === DELETE)
  const enabling = reviewed.filter(
    ({ edit }) =>
      edit.segments.length === 3 && edit.segments[2] === SOURCE_ENABLED && edit.value === true,
  )
  const addBlocked = pending === '' || names.includes(pending)
  const enabledStaged = edits.stagedAt([SOURCES, selected, SOURCE_ENABLED])
  const enabled = enabledStaged ? enabledStaged.value === true : stored[SOURCE_ENABLED] === true
  const pollStaged = edits.stagedAt([SOURCES, selected, SOURCE_POLL])
  // The repository the poll would name as the form now stands. Read back OUT of the
  // argv rather than held in a second state: a control showing a repository the
  // staged array does not actually carry is the drift the review card exists to
  // prevent, and the argv is the thing that gets written.
  const stagedMatch =
    pollStaged && shape.preset ? matchPoll(shape.preset.entry[SOURCE_POLL], pollStaged.value) : null
  const repository = stagedMatch ? stagedMatch.repository : shape.repository
  const projectStaged = edits.stagedAt([SOURCES, selected, SOURCE_PROJECT])
  const boundProject = String(
    (projectStaged ? projectStaged.value : stored[SOURCE_PROJECT]) ?? '',
  )
  const maintainersStaged = edits.stagedAt([SOURCES, selected, SOURCE_MAINTAINERS])
  const maintainers = maintainerText(
    maintainersStaged ? maintainersStaged.value : stored[SOURCE_MAINTAINERS],
  )

  /** The link into the autonomy grid for *source*, which is the resolution this
   *  form links to rather than rendering a second time. */
  const gridLink = (source: string) => (
    <a
      href={`#${SOURCES_GRID_ID}`}
      onClick={() => onShowGrid(source)}
    >
      {i18nT('apps.specEngine.sourceForm.open_the_autonomy_grid_for_source', { source })}
    </a>
  )

  return (
    <div className="se-blk">
      {/* The same number the "unwritten source changes" line below states, read
          from the same list, so the tab badge cannot claim a count this form does
          not show. */}
      <PendingCount count={reviewed.length} onCount={onPendingCount} />
      <h3>{i18nT('apps.specEngine.sourceForm.watch_source_definitions')}</h3>
      {names.length === 0 ? (
        /* Not an empty form: fields for a source that does not exist read as a
           source with nothing configured, when the fact is that nothing is being
           ingested at all. The add block below is the answer. */
        <p className="se-note">{i18nT('apps.specEngine.sourceForm.no_watch_source_is_defined')}</p>
      ) : (
        <>
          <div
            className="se-acts"
            role="group"
            aria-label={i18nT('apps.specEngine.sourceForm.select_a_watch_source_to_edit')}
          >
            {names.map((name) => (
              <button
                key={name}
                type="button"
                className="se-btn se-sm se-m"
                aria-pressed={name === selected}
                // The refusal acknowledgment is about a confirmation typed for THIS
                // source; carried across a switch it would caption another source's
                // block with a refusal that never happened to it.
                onClick={() => {
                  setChosen(name)
                  setArmed(null)
                  setTypedName('')
                  setRemovalRefused(false)
                }}
              >
                {name}
              </button>
            ))}
          </div>
          {!shape.expressible ? (
            /* The honest state, and no controls: a form over three of an entry's
               fields invites a confirm over the ones it never showed. */
            <div className="se-arm" data-not-expressible="true">
              <p>
                <AlertTriangle className="lucide-inline" aria-hidden="true" />
                {i18nT('apps.specEngine.sourceForm.the_form_cannot_express_this_source', {
                  source: selected,
                })}
              </p>
              {shape.preset === null && (
                <p className="se-note">
                  {i18nT('apps.specEngine.sourceForm.the_poll_is_not_a_bundled_presets')}
                </p>
              )}
              {shape.unexpressed.length > 0 && (
                <p className="se-note">
                  {i18nT('apps.specEngine.sourceForm.the_entry_carries_unshown_fields', {
                    fields: shape.unexpressed.join(', '),
                  })}
                </p>
              )}
              <div className="se-acts">
                <button type="button" className="se-btn" onClick={onOpenJson}>
                  {i18nT('apps.specEngine.sourceForm.edit_this_source_in_the_json_view', {
                    source: selected,
                  })}
                </button>
                {/* Removal is offered here too, and it is the one control that can
                    be: a deletion writes no field, so it cannot rewrite one this
                    state did not show. Withholding it would leave a source the form
                    cannot describe removable only by hand-editing the document. */}
                <button
                  type="button"
                  className="se-btn se-sm se-danger"
                  aria-label={i18nT('apps.specEngine.sourceForm.remove_the_source', {
                    source: selected,
                  })}
                  onClick={() => {
                    touched()
                    setTypedName('')
                    setArmed(selected)
                  }}
                >
                  {i18nT('apps.specEngine.configPanel.remove')}
                </button>
              </div>
              <p className="se-note">
                {i18nT('apps.specEngine.sourceForm.a_removal_writes_no_field_it_did_not_show')}
              </p>
            </div>
          ) : (
            <>
              {/* What the engine runs, before any control that could arm it. */}
              <SourceCommand entry={stored} preset={shape.preset as SourcePreset} />
              <div className="se-settings">
                {shape.slots.length === 0 ? (
                  /* Stated rather than absent: a preset with no repository slot — or
                     one whose placeholder sits on the program, which this form
                     refuses to substitute — has no parameter for this form to offer,
                     and silence would read as a form that simply forgot. */
                  <p className="se-note">
                    {i18nT('apps.specEngine.sourceForm.the_preset_has_no_repository_slot', {
                      preset: shape.preset ? shape.preset.host : NONE,
                    })}
                  </p>
                ) : (
                  <div
                    className="se-setting"
                    data-source-parameter="repository"
                    data-staged={pollStaged !== undefined}
                  >
                    <label className="se-setting-name" htmlFor="se-source-repository">
                      {i18nT('apps.specEngine.sourceForm.the_repository_parameter')}
                      <span className="se-kv-path">{dotted([SOURCES, selected, SOURCE_POLL])}</span>
                    </label>
                    <input
                      id="se-source-repository"
                      type="text"
                      className="se-input"
                      value={repoInput ?? (isPlaceholder(repository) ? '' : repository)}
                      onChange={(event) => nameRepository(event.target.value)}
                    />
                    <p className="se-note">
                      {i18nT('apps.specEngine.sourceForm.the_repository_is_a_value_not_a_command')}
                    </p>
                    {repoRefused && (
                      /* The refusal is an event this control caused, stated where it
                         happened: with the staged poll withdrawn, silence would read
                         as an input that does nothing. */
                      <p className="se-note" role="status">
                        {i18nT('apps.specEngine.sourceForm.that_is_not_a_repository_name')}
                      </p>
                    )}
                    {isPlaceholder(repository) && (
                      <p className="se-note">
                        {i18nT('apps.specEngine.sourceForm.the_repository_is_still_the_placeholder', {
                          placeholder: SOURCE_REPO_PLACEHOLDER,
                        })}
                      </p>
                    )}
                    <p className="se-note">
                      {i18nT('apps.specEngine.sourceForm.stored_on_the_source')}
                      {SEP}
                      <span className="se-m">{shape.repository}</span>
                    </p>
                    {pollStaged !== undefined && (
                      <p className="se-note">
                        <span className="se-flag" data-flag="pending">
                          {i18nT('apps.specEngine.sourceForm.not_written')}
                        </span>
                        <span className="se-m">
                          {shownValue(pollStaged.value)}
                          {SEP}
                          {dotted([SOURCES, selected, SOURCE_POLL])}
                        </span>
                      </p>
                    )}
                  </div>
                )}
                <div
                  className="se-setting"
                  data-source-field={SOURCE_ENABLED}
                  data-staged={enabledStaged !== undefined}
                >
                  <label className="se-setting-name" htmlFor="se-source-enabled">
                    {i18nT('apps.specEngine.sourceForm.poll_this_source')}
                    <span className="se-kv-path">
                      {dotted([SOURCES, selected, SOURCE_ENABLED])}
                    </span>
                  </label>
                  <input
                    id="se-source-enabled"
                    type="checkbox"
                    className="se-check"
                    checked={enabled}
                    onChange={(event) => stageEnabled(event.target.checked)}
                  />
                  <p className="se-note">
                    {i18nT('apps.specEngine.sourceForm.stored_on_the_source')}
                    {SEP}
                    <span className="se-m">{shownValue(stored[SOURCE_ENABLED])}</span>
                  </p>
                  {enabledStaged !== undefined && (
                    <p className="se-note">
                      <span className="se-flag" data-flag="pending">
                        {i18nT('apps.specEngine.sourceForm.not_written')}
                      </span>
                      <span className="se-m">
                        {shownValue(enabledStaged.value)}
                        {SEP}
                        {dotted([SOURCES, selected, SOURCE_ENABLED])}
                      </span>
                    </p>
                  )}
                </div>
                <div
                  className="se-setting"
                  data-source-field={SOURCE_PROJECT}
                  data-staged={projectStaged !== undefined}
                >
                  <span className="se-setting-name">
                    {i18nT('apps.specEngine.sourceForm.the_project_binding')}
                    <span className="se-kv-path">
                      {dotted([SOURCES, selected, SOURCE_PROJECT])}
                    </span>
                  </span>
                  {projects.length === 0 ? (
                    <p className="se-note">
                      {i18nT('apps.specEngine.sourceForm.no_project_is_configured_to_bind')}
                    </p>
                  ) : (
                    <div
                      className="se-acts"
                      role="group"
                      aria-label={i18nT('apps.specEngine.sourceForm.project_for_source', {
                        source: selected,
                      })}
                    >
                      {/* A button group rather than a dropdown, for the level
                          control's reason: a popup would be drawn over a page whose
                          kill-switch strip must never be covered. A stored name the
                          document no longer lists is offered too, so the row can
                          show what is actually bound. */}
                      {(projects.includes(boundProject) || boundProject === ''
                        ? projects
                        : [boundProject, ...projects]
                      ).map((name) => (
                        <button
                          key={name}
                          type="button"
                          className="se-btn se-sm se-m"
                          aria-pressed={name === boundProject}
                          onClick={() =>
                            act({
                              kind: 'field',
                              source: selected,
                              field: SOURCE_PROJECT,
                              value: name,
                            })
                          }
                        >
                          {name}
                        </button>
                      ))}
                    </div>
                  )}
                  <p className="se-note">
                    {i18nT('apps.specEngine.sourceForm.stored_on_the_source')}
                    {SEP}
                    <span className="se-m">{shownValue(stored[SOURCE_PROJECT])}</span>
                  </p>
                  {projectStaged !== undefined && (
                    <p className="se-note">
                      <span className="se-flag" data-flag="pending">
                        {i18nT('apps.specEngine.sourceForm.not_written')}
                      </span>
                      <span className="se-m">
                        {shownValue(projectStaged.value)}
                        {SEP}
                        {dotted([SOURCES, selected, SOURCE_PROJECT])}
                      </span>
                    </p>
                  )}
                </div>
                <div
                  className="se-setting"
                  data-source-field={SOURCE_MAINTAINERS}
                  data-staged={maintainersStaged !== undefined}
                >
                  <label className="se-setting-name" htmlFor="se-source-maintainers">
                    {i18nT('apps.specEngine.sourceForm.the_maintainer_accounts')}
                    <span className="se-kv-path">
                      {dotted([SOURCES, selected, SOURCE_MAINTAINERS])}
                    </span>
                  </label>
                  <input
                    id="se-source-maintainers"
                    type="text"
                    className="se-input"
                    value={maintainers}
                    onChange={(event) =>
                      act({
                        kind: 'field',
                        source: selected,
                        field: SOURCE_MAINTAINERS,
                        value: maintainerList(event.target.value),
                      })
                    }
                  />
                  <p className="se-note">
                    {i18nT('apps.specEngine.sourceForm.maintainers_are_the_most_trusted_class')}
                  </p>
                  <p className="se-note">
                    {i18nT('apps.specEngine.sourceForm.stored_on_the_source')}
                    {SEP}
                    <span className="se-m">{shownValue(stored[SOURCE_MAINTAINERS])}</span>
                  </p>
                  {maintainersStaged !== undefined && (
                    <p className="se-note">
                      <span className="se-flag" data-flag="pending">
                        {i18nT('apps.specEngine.sourceForm.not_written')}
                      </span>
                      <span className="se-m">
                        {shownValue(maintainersStaged.value)}
                        {SEP}
                        {dotted([SOURCES, selected, SOURCE_MAINTAINERS])}
                      </span>
                    </p>
                  )}
                </div>
              </div>
              {sourceSettings.length > 0 && (
                <>
                  <h3>{i18nT('apps.specEngine.sourceForm.settings_this_source_holds')}</h3>
                  <div className="se-settings">
                    {sourceSettings.map((field) => (
                      <StoredSettingRow
                        key={field.setting.key}
                        field={field}
                        labels={{
                          stored: i18nT('apps.specEngine.sourceForm.stored_on_the_source'),
                          notWritten: i18nT('apps.specEngine.sourceForm.not_written'),
                          notEditable: i18nT(
                            'apps.specEngine.sourceForm.the_registry_kind_is_not_editable_here',
                            { kind: field.setting.kind },
                          ),
                        }}
                        staged={edits.stagedAt(field.segments)}
                        onStage={(value) =>
                          act({
                            kind: 'setting',
                            source: selected,
                            key: field.setting.key,
                            value,
                          })
                        }
                        onWithdraw={() => {
                          touched()
                          edits.unstage(field.segments)
                        }}
                      />
                    ))}
                  </div>
                  <p className="se-note">
                    {i18nT('apps.specEngine.sourceForm.a_source_may_hold_only_these_settings')}
                  </p>
                </>
              )}
              {/* The grid is a resolution, linked rather than rendered twice, and
                  the fail-closed rule is stated here because it is the answer for
                  a source whose grid is absent — which is every new source. */}
              <p className="se-note">
                {i18nT('apps.specEngine.sourceForm.the_grid_decides_how_far_a_run_goes')}
                {SEP}
                {gridLink(selected)}
              </p>
              <p className="se-note">
                {i18nT('apps.specEngine.sourceForm.an_absent_grid_fails_closed')}
              </p>
              <div className="se-acts" style={{ marginTop: 9 }}>
                <button
                  type="button"
                  className="se-btn se-sm se-danger"
                  // The accessible name carries the target even though the visible
                  // label is one word: a bare "Remove" is how the wrong source goes.
                  aria-label={i18nT('apps.specEngine.sourceForm.remove_the_source', {
                    source: selected,
                  })}
                  onClick={() => {
                    touched()
                    setTypedName('')
                    setArmed(selected)
                  }}
                >
                  {i18nT('apps.specEngine.configPanel.remove')}
                </button>
              </div>
            </>
          )}
          {armed !== null && (
            /* In flow under the form, never a dialog: the confirmation for a
               destructive edit is a sibling block for the same reason the kill
               switch's is. The source's own name, TYPED, because a column of
               identical Remove controls is how the wrong source goes — and the
               name is the one thing that cannot be clicked by accident. */
            <div className="se-arm">
              <p>
                <AlertTriangle className="lucide-inline" aria-hidden="true" />
                {i18nT('apps.specEngine.sourceForm.removing_stops_ingesting', {
                  source: armed,
                  path: dotted([SOURCES, armed]),
                })}
              </p>
              <div className="se-setting">
                <label className="se-setting-name" htmlFor="se-source-remove-name">
                  {i18nT('apps.specEngine.sourceForm.type_the_name_to_confirm', { source: armed })}
                </label>
                <input
                  id="se-source-remove-name"
                  type="text"
                  className="se-input"
                  value={typedName}
                  onChange={(event) => setTypedName(event.target.value)}
                />
              </div>
              <div className="se-acts">
                <button type="button" className="se-btn se-danger" onClick={confirmRemoval}>
                  {i18nT('apps.specEngine.sourceForm.confirm_the_removal', { source: armed })}
                </button>
                <button
                  type="button"
                  className="se-btn"
                  onClick={() => {
                    setArmed(null)
                    setTypedName('')
                    setRemovalRefused(false)
                  }}
                >
                  {i18nT('apps.specEngine.sourceForm.keep_the_source')}
                </button>
              </div>
              {removalRefused && (
                <p className="se-note" role="status">
                  <span>{i18nT('apps.specEngine.sourceForm.the_removal_was_refused')}</span>
                  {SEP}
                  <span>
                    {i18nT('apps.specEngine.sourceForm.the_typed_name_does_not_match', {
                      source: armed,
                    })}
                  </span>
                </p>
              )}
            </div>
          )}
        </>
      )}

      <h3>{i18nT('apps.specEngine.sourceForm.add_a_watch_source')}</h3>
      <div className="se-setting">
        <label className="se-setting-name" htmlFor="se-source-add-name">
          {i18nT('apps.specEngine.sourceForm.name_for_the_new_source')}
        </label>
        <input
          id="se-source-add-name"
          type="text"
          className="se-input"
          value={addName}
          onChange={(event) => renameAdd(event.target.value)}
        />
        {pending !== '' && names.includes(pending) && (
          <p className="se-note" role="status">
            {i18nT('apps.specEngine.sourceForm.the_name_is_already_a_source', { source: pending })}
          </p>
        )}
        {pending === '' && (
          <p className="se-note">{i18nT('apps.specEngine.sourceForm.name_the_source_first')}</p>
        )}
      </div>
      <div className="se-setting" data-source-parameter="add-repository">
        <label className="se-setting-name" htmlFor="se-source-add-repository">
          {i18nT('apps.specEngine.sourceForm.the_repository_for_the_new_source')}
        </label>
        <input
          id="se-source-add-repository"
          type="text"
          className="se-input"
          value={addRepo}
          onChange={(event) => nameAddRepository(event.target.value)}
        />
        <p className="se-note">
          {i18nT('apps.specEngine.sourceForm.the_repository_is_a_value_not_a_command')}
        </p>
        {addRefused === 'shape' && (
          <p className="se-note" role="status">
            {i18nT('apps.specEngine.sourceForm.that_is_not_a_repository_name')}
          </p>
        )}
        {addRefused !== '' && addRefused !== 'shape' && (
          /* The chosen preset has nowhere to put the typed repository, so the copy
             was refused rather than staged with the value silently dropped. */
          <p className="se-note" role="status">
            {i18nT('apps.specEngine.sourceForm.the_preset_has_no_repository_slot', {
              preset: addRefused,
            })}
          </p>
        )}
        {addRepo.trim() === '' && (
          /* The consequence of leaving it empty, stated where it is left empty: the
             copy is still written, and it still cannot poll. */
          <p className="se-note">
            {i18nT('apps.specEngine.sourceForm.a_copy_with_no_repository_keeps_the_placeholder', {
              placeholder: SOURCE_REPO_PLACEHOLDER,
            })}
          </p>
        )}
      </div>
      {presets.length === 0 ? (
        <p className="se-note">{i18nT('apps.specEngine.sourceForm.the_engine_bundles_no_preset')}</p>
      ) : (
        <div
          role="group"
          aria-label={i18nT('apps.specEngine.sourceForm.choose_a_preset_to_copy')}
        >
          {presets.map((preset) => {
            const map = preset.entry[SOURCE_FIELD_MAP]
            const fields = isObject(map) ? Object.keys(map) : []
            return (
              <div className="se-offer" key={preset.host} data-preset={preset.host}>
                <span className="se-m">{preset.host}</span>
                {/* What it ingests and which tool it needs, from the preset's own
                    bytes: the program is derived from its argv upstream, and the
                    fields are the ones its field map reads out of the output. */}
                <span className="se-note">
                  {i18nT('apps.specEngine.sourceForm.preset_ingests_items', {
                    host: preset.host,
                    program: preset.program,
                    count: fmtNumber(fields.length),
                    fields: fields.join(', '),
                  })}
                </span>
                <button
                  type="button"
                  className="se-btn se-sm"
                  disabled={addBlocked}
                  onClick={() => stageCopy(preset)}
                >
                  {i18nT('apps.specEngine.sourceForm.copy_the_preset', { host: preset.host })}
                </button>
              </div>
            )
          })}
        </div>
      )}
      <p className="se-note">
        {i18nT('apps.specEngine.sourceForm.an_add_is_always_a_preset_copy')}
      </p>
      {orphanedCopy !== '' && (
        /* The withdrawal is an event this form caused, so it is stated: a staged
           copy that silently stopped being counted would read as an edit that was
           never made. `role="status"` so the announcement reaches a reader who is
           not looking at the add block when a refetch lands. */
        <p className="se-note" role="status">
          {i18nT('apps.specEngine.sourceForm.the_staged_copy_was_withdrawn', {
            source: orphanedCopy,
          })}
        </p>
      )}

      <div className="se-acts" style={{ marginTop: 9 }}>
        <button
          type="button"
          className="se-btn"
          disabled={reviewed.length === 0}
          onClick={() => setReviewing(true)}
        >
          {i18nT('apps.specEngine.sourceForm.review_the_exact_change')}
        </button>
        {reviewed.length > 0 && (
          <span className="se-lbl">
            {i18nT('apps.specEngine.sourceForm.unwritten_source_changes')}
            {SEP}
            <span className="se-m">{fmtNumber(reviewed.length)}</span>
          </span>
        )}
      </div>
      {reviewing && reviewed.length > 0 && (
        <FormReview
          changes={reviewed.map((entry) => entry.change)}
          patch={patch}
          labels={{
            heading: i18nT('apps.specEngine.sourceForm.the_change_that_would_be_written'),
            confirm: i18nT('apps.specEngine.sourceForm.write_the_change'),
            writing: i18nT('apps.specEngine.configPanel.saving'),
            discard: i18nT('apps.specEngine.sourceForm.discard_the_pending_changes'),
            exactly: i18nT('apps.specEngine.sourceForm.a_confirm_writes_exactly_this_patch'),
            refusalTitle: i18nT('apps.specEngine.sourceForm.could_not_write_the_source_change'),
            retained: i18nT(
              'apps.specEngine.sourceForm.nothing_was_written_so_the_source_is_stored_state',
            ),
          }}
          consequences={
            (enabling.length > 0 || removing.length > 0) && (
              /* In flow under the patch, never a dialog: neither consequence is
                 legible in the JSON — a `true` does not say that a program starts
                 running on a timer, and a `null` does not say that ingestion
                 stops — and a consequence stated in an overlay is one that can be
                 dismissed. */
              <div className="se-arm">
                {enabling.map(({ edit }) => {
                  const source = edit.segments[1]
                  // The poll as it will stand once this patch lands: a repository
                  // named in the same review must not be described as unnamed, and a
                  // placeholder still in place must not be described as a repository.
                  const staged = edits.stagedAt([SOURCES, source, SOURCE_POLL])
                  const poll = staged ? staged.value : nodeAt(document, [SOURCES, source, SOURCE_POLL])
                  const from = presetForPoll(poll, presets)
                  const program = from ? from.preset.program : NONE
                  const unnamed = from === null || isPlaceholder(from.match.repository)
                  return (
                    <Fragment key={dotted(edit.segments)}>
                      <p>
                        <AlertTriangle className="lucide-inline" aria-hidden="true" />
                        {unnamed
                          ? i18nT('apps.specEngine.sourceForm.enabling_polls_the_placeholder', {
                              source,
                              program,
                              placeholder: SOURCE_REPO_PLACEHOLDER,
                              path: dotted(edit.segments),
                            })
                          : i18nT('apps.specEngine.sourceForm.enabling_begins_polling', {
                              source,
                              program,
                              path: dotted(edit.segments),
                            })}
                      </p>
                      <p className="se-note">{gridLink(source)}</p>
                      <p className="se-note">
                        {i18nT('apps.specEngine.sourceForm.an_absent_grid_fails_closed')}
                      </p>
                    </Fragment>
                  )
                })}
                {removing.map(({ edit }) => (
                  <p key={dotted(edit.segments)}>
                    <AlertTriangle className="lucide-inline" aria-hidden="true" />
                    {i18nT('apps.specEngine.sourceForm.removing_stops_ingesting', {
                      source: edit.segments[1],
                      path: dotted(edit.segments),
                    })}
                  </p>
                ))}
              </div>
            )
          }
          writing={write.isPending}
          error={write.isError ? write.error : null}
          onConfirm={(sending) => write.mutate(sending)}
          onDiscard={discard}
        />
      )}
      {wrote && (
        <p className="se-note" role="status">
          {i18nT('apps.specEngine.sourceForm.wrote_the_change_and_re_read_the_sources')}
        </p>
      )}
    </div>
  )
}

/**
 * Whether unsaved text differs from the document the read returned.
 *
 * Shared by the editor and the control that opens it, so the pane cannot claim
 * unsaved edits the editor would call clean — or, worse, close over a draft
 * without saying it is holding one.
 */
export function isDirty(text: string | null, document: unknown): boolean {
  return text !== null && text !== documentText(document)
}
/**
 * The document, edited and saved through the engine's one write path.
 *
 * The editor holds text rather than a parsed object, because half-typed JSON is a
 * legitimate state of an editor and a parsed model cannot represent it. `text ===
 * null` means "showing what the read returned", which is what makes the revert
 * exact: it drops local text and shows the document again rather than reconstructing
 * a copy of it.
 *
 * That text lives ABOVE this component, in the pane, because this view can be closed
 * and reopened: state held here would be discarded by the unmount, so closing the
 * view would silently throw away a half-written document. The rest of the state is
 * local on purpose — a parse error, an empty-patch note and a save's advisories all
 * describe the last attempt, and there is nothing to lose by asking again.
 */
export function DocumentEditor({
  config,
  text,
  onText,
}: {
  config: ConfigSnapshot
  /** The unsaved text, or `null` while the editor shows the read. */
  text: string | null
  onText: (text: string | null) => void
}) {
  const client = useQueryClient()
  const [localError, setLocalError] = useState('')
  const [saved, setSaved] = useState<ConfigAdvisory[] | null>(null)
  const [empty, setEmpty] = useState(false)

  const baseline = config.document
  const shown = text ?? documentText(baseline)
  const dirty = isDirty(text, baseline)

  const save = useMutation({
    mutationFn: (patch: Document) => specEngineApi.writeConfig(patch),
    onSuccess: (result) => {
      setSaved(result.advisories)
      // Dropped rather than replaced with the merged document: the read is the
      // authority on what is persisted (and on what is elided), so the editor goes
      // back to showing it. Every surface on the pane reads the same query, so the
      // invalidation below is what refreshes the forms beside this view too.
      onText(null)
      void client.invalidateQueries({ queryKey: QK.config })
      void client.invalidateQueries({ queryKey: QK_RESOLVED_ROOT })
    },
  })

  const onSave = useCallback(() => {
    setSaved(null)
    setEmpty(false)
    const parsed = parseDocument(
      shown,
      i18nT('apps.specEngine.configPanel.a_document_must_be_a_json_object'),
    )
    if (!parsed.ok) {
      setLocalError(parsed.error)
      return
    }
    setLocalError('')
    const patch = mergePatch(baseline, parsed.document, config.elided_marker)
    if (Object.keys(patch).length === 0) {
      // Every write is recorded, so sending an empty patch would put a line in the
      // durable write record for a change nobody made.
      setEmpty(true)
      return
    }
    save.mutate(patch)
  }, [baseline, config.elided_marker, save, shown])

  return (
    <>
      <textarea
        aria-label={i18nT('apps.specEngine.configPanel.the_configuration_document')}
        className="se-json se-m"
        spellCheck={false}
        value={shown}
        onChange={(event) => {
          onText(event.target.value)
          setLocalError('')
          setEmpty(false)
          setSaved(null)
        }}
      />
      <div className="se-acts" style={{ marginTop: 11 }}>
        <button
          type="button"
          className="se-btn"
          disabled={save.isPending}
          onClick={onSave}
        >
          {save.isPending
            ? i18nT('apps.specEngine.configPanel.saving')
            : i18nT('apps.specEngine.configPanel.validate_and_save')}
        </button>
        <button
          type="button"
          className="se-btn"
          disabled={!dirty || save.isPending}
          onClick={() => {
            onText(null)
            setLocalError('')
            setEmpty(false)
          }}
        >
          {i18nT('apps.specEngine.configPanel.revert_unsaved_edits')}
        </button>
        {dirty && (
          <span className="se-lbl">{i18nT('apps.specEngine.configPanel.unsaved_edits')}</span>
        )}
      </div>

      {localError !== '' && (
        <div className="se-refusal" role="alert">
          {i18nT('apps.specEngine.configPanel.the_document_is_not_valid_json')}
          <code>{localError}</code>
        </div>
      )}
      {empty && (
        <p className="se-note">{i18nT('apps.specEngine.configPanel.nothing_to_save')}</p>
      )}
      {save.isError && (
        <Refused
          title={i18nT('apps.specEngine.configPanel.could_not_save_the_configuration')}
          error={save.error}
        />
      )}
      {saved !== null && (
        <div className="se-torn">
          {i18nT('apps.specEngine.configPanel.saved_the_document')}
          <Advisories advisories={saved} />
        </div>
      )}

      {config.errors.length > 0 && (
        <div className="se-blk">
          <h3>{i18nT('apps.specEngine.configPanel.problems_in_the_persisted_document')}</h3>
          <ul className="se-findings">
            {config.errors.map((error) => (
              <li key={`${error.path}:${error.message}`}>
                <span className="se-fkind">{error.path || NONE}</span>
                {error.message}
              </li>
            ))}
          </ul>
        </div>
      )}
      {config.advisories.length > 0 && (
        <div className="se-blk">
          <h3>{i18nT('apps.specEngine.configPanel.advisories')}</h3>
          <Advisories advisories={config.advisories} />
        </div>
      )}

      <p className="se-note">
        {i18nT('apps.specEngine.specEnginePage.secret_values_are_withheld_from_this_read')}
      </p>
      {config.elided.length > 0 && (
        <p className="se-note">
          {i18nT('apps.specEngine.configPanel.withheld_at')}
          <span className="se-m">{SEP}{config.elided.join(SEP)}</span>
        </p>
      )}
      {/* Both properties of the write, stated where the operator is about to make
          one. The second is the one nobody would guess: a merge keeps what a patch
          omits, so a deletion has to be sent as a deletion. */}
      <p className="se-note">
        {i18nT('apps.specEngine.configPanel.elided_values_are_never_written_back')}
      </p>
      <p className="se-note">
        {i18nT('apps.specEngine.configPanel.deletions_are_sent_as_explicit_nulls')}
      </p>
    </>
  )
}

/**
 * Fields of a project entry presented as the entry's identity rather than as
 * configuration: the pinned profile has its own column, and the path names the
 * project rather than overriding a setting.
 *
 * Excluded from the override count for that reason, not because they are less
 * important: a reader who can see the pinned profile in its own column and then
 * counts it again under "overrides" learns that the number means nothing.
 */
const ENTRY_OWN_COLUMNS: readonly string[] = ['path', 'cost_profile']

/**
 * How many values a document node declares, counting leaves.
 *
 * An array is one declaration rather than one per element: `protected_branches`
 * is a single decision an operator made, and counting its elements would make a
 * project look more configured the longer its branch list is.
 */
function declaredValues(node: unknown): number {
  if (!isObject(node)) return 1
  return Object.values(node).reduce((total: number, child) => total + declaredValues(child), 0)
}

/** The values a project entry declares beyond the fields shown beside the count. */
function overrideCount(entry: unknown): number {
  if (!isObject(entry)) return 0
  let total = 0
  for (const [key, value] of Object.entries(entry)) {
    if (ENTRY_OWN_COLUMNS.includes(key)) continue
    total += declaredValues(value)
  }
  return total
}

/** One row of the projects table: a stored entry, or the app-wide resolution. */
interface ProjectRow {
  /** The `projects` key, or `APP_WIDE` for the fixed app-defaults row. */
  id: string
  /** The cost profile the entry pins, `''` when it pins none. */
  profile: string
  /** Values the entry declares, apart from the ones with their own column. */
  overrides: number
  /** Whether the row addresses a stored entry, so a removal has a target. */
  stored: boolean
}

/** The app-defaults row, then one row per stored entry in name order. */
function projectRows(document: Document): ProjectRow[] {
  const node = document[PROJECTS]
  const entries = isObject(node) ? node : {}
  return [
    { id: APP_WIDE, profile: '', overrides: 0, stored: false },
    ...Object.keys(entries)
      .sort()
      .map((name) => {
        const entry = entries[name]
        const profile = isObject(entry) ? entry.cost_profile : undefined
        return {
          id: name,
          profile: typeof profile === 'string' ? profile : '',
          overrides: overrideCount(entry),
          stored: true,
        }
      }),
  ]
}

/**
 * Every configured project, and the removal for one.
 *
 * The table is the surface's answer to "which configuration governs which
 * project": the document beside it holds the same facts, but reading a project's
 * pinned profile out of a JSON blob is not the same as seeing it in a column
 * next to the other projects'. Selecting a row is what the resolved read beside
 * it resolves FOR, so the two are one flow rather than a list and an unrelated
 * picker.
 *
 * The app-defaults row is a row rather than a header link because it IS one of
 * the resolutions an operator compares against: it is what a project's values
 * fall back to, and what resolution looks like for a project this document has
 * never heard of.
 *
 * Keyboard traversal is the queue table's: a roving tabindex over `role="row"`
 * elements with selection following focus, so the rail's advertised `j`/`k` keys
 * work here too and no row is reachable only by pointer.
 */
export function ProjectsTable({
  config,
  project,
  onSelect,
}: {
  config: ConfigSnapshot
  project: string
  onSelect: (project: string) => void
}) {
  const client = useQueryClient()
  const [armed, setArmed] = useState<string | null>(null)
  const [removed, setRemoved] = useState<string>('')
  const rowRefs = useRef(new Map<string, HTMLDivElement>())

  const rows = useMemo(() => projectRows(config.document), [config.document])

  const remove = useMutation({
    // A removal is an ordinary configuration write: the entry's own path staged as
    // a deletion, through the same single write path a save uses and the same
    // patch builder every form write uses. The engine's merge deletes a key whose
    // patch value is null, so no delete route has to exist — and the write is
    // validated, locked and recorded exactly like every other one.
    mutationFn: (name: string) =>
      specEngineApi.writeConfig(
        buildFormPatch([{ segments: [PROJECTS, name], value: DELETE }]),
      ),
    onSuccess: (_reply, name) => {
      setArmed(null)
      setRemoved(name)
      // The read is the pane's one source for the document: the editor's
      // baseline, this table and the resolved view all come from it, and the
      // reply carries no elision list or validation errors to rebuild a snapshot
      // from. So the reply is not adopted — the query is invalidated and the
      // table re-renders from what the store now returns.
      void client.invalidateQueries({ queryKey: QK.config })
      void client.invalidateQueries({ queryKey: QK_RESOLVED_ROOT })
      // A selection pointing at a deleted entry would resolve to the app-wide
      // layers and be LABELLED as that project, which reads as "this project
      // inherits everything" rather than "this project is gone".
      if (project === name) onSelect(APP_WIDE)
    },
  })

  // An arm outlives the entry it names unless it is withdrawn: another surface
  // (or a document edit in the pane beside this one) can delete the entry while
  // the confirm sits on screen, and a confirm then sends a deletion for a key
  // that no longer exists.
  useEffect(() => {
    if (armed !== null && !rows.some((row) => row.id === armed)) setArmed(null)
  }, [armed, rows])

  const onRowKeyDown = useCallback(
    (event: React.KeyboardEvent<HTMLDivElement>, index: number) => {
      if (event.metaKey || event.ctrlKey || event.altKey) return
      let next = -1
      if (event.key === 'ArrowDown' || event.key === 'j') next = index + 1
      else if (event.key === 'ArrowUp' || event.key === 'k') next = index - 1
      else if (event.key === 'Home') next = 0
      else if (event.key === 'End') next = rows.length - 1
      else return
      event.preventDefault()
      const target = rows[Math.min(Math.max(next, 0), rows.length - 1)]
      if (target) rowRefs.current.get(target.id)?.focus()
    },
    [rows],
  )

  return (
    <div className="se-blk">
      <h3>{i18nT('apps.specEngine.configPanel.projects')}</h3>
      <div
        className="se-q se-projects"
        role="grid"
        aria-label={i18nT('apps.specEngine.configPanel.configured_projects')}
      >
        <div className="se-qhead" role="row">
          <span role="columnheader">{i18nT('apps.specEngine.configPanel.col_project')}</span>
          <span role="columnheader">{i18nT('apps.specEngine.configPanel.col_cost_profile')}</span>
          <span role="columnheader">{i18nT('apps.specEngine.configPanel.col_overrides')}</span>
          <span role="columnheader">{i18nT('apps.specEngine.configPanel.remove')}</span>
        </div>
        {rows.map((row, index) => {
          const selected = row.id === project
          return (
            <div
              // Disjoint key namespaces: a project literally named
              // "app-defaults" must not collide with the defaults row's key.
              key={row.id === APP_WIDE ? APP_DEFAULTS_ROW_KEY : `project:${row.id}`}
              ref={(node) => {
                if (node) rowRefs.current.set(row.id, node)
                else rowRefs.current.delete(row.id)
              }}
              className="se-row"
              role="row"
              aria-selected={selected}
              tabIndex={selected ? 0 : -1}
              onFocus={(event) => {
                // The ROW's own focus, not a control's inside it. React's `onFocus`
                // rides `focusin`, which bubbles, so a click on the removal button
                // would otherwise re-select the row and silently re-resolve for a
                // project the operator was only about to delete.
                if (event.target === event.currentTarget) onSelect(row.id)
              }}
              onClick={() => rowRefs.current.get(row.id)?.focus()}
              onKeyDown={(event) => onRowKeyDown(event, index)}
            >
              <span role="gridcell" className={row.stored ? 'se-m' : undefined}>
                {row.id || i18nT('apps.specEngine.configPanel.no_project_app_wide')}
              </span>
              <span role="gridcell" className="se-m">
                {row.profile || NONE}
              </span>
              <span role="gridcell" className="se-cost">
                {row.stored ? fmtNumber(row.overrides) : NONE}
              </span>
              <span role="gridcell">
                {row.stored && (
                  <button
                    type="button"
                    className="se-btn se-sm se-danger"
                    // The accessible name carries the target even though the
                    // visible label is one word: five rows of identical
                    // "Remove" buttons are five identical announcements.
                    aria-label={i18nT('apps.specEngine.configPanel.remove_project', {
                      project: row.id,
                    })}
                    disabled={remove.isPending}
                    onClick={(event) => {
                      // Arming is not a request to resolve for this project: the
                      // row click would select it, and a reader watching the pane
                      // beside the table would see it change under a confirm.
                      event.stopPropagation()
                      setRemoved('')
                      remove.reset()
                      setArmed(row.id)
                    }}
                  >
                    {i18nT('apps.specEngine.configPanel.remove')}
                  </button>
                )}
              </span>
            </div>
          )
        })}
      </div>
      {rows.length === 1 && (
        <p className="se-note">
          {i18nT('apps.specEngine.configPanel.no_project_is_configured_yet')}
        </p>
      )}
      <p className="se-note">
        {i18nT('apps.specEngine.configPanel.overrides_counts_declared_values')}
      </p>

      {armed !== null && (
        /* In flow under the table, never a dialog: the confirmation for a
           destructive edit is a sibling block for the same reason the kill
           switch's is, and a browser `confirm()` would state the blast radius in
           a string no catalog holds. */
        <div className="se-arm">
          <p>
            <AlertTriangle className="lucide-inline" aria-hidden="true" />
            {i18nT('apps.specEngine.configPanel.removing_deletes_the_entry', { project: armed })}
          </p>
          <p className="se-note">
            {i18nT('apps.specEngine.configPanel.after_removal_work_falls_back_to_app_wide')}
          </p>
          <div className="se-acts">
            <button
              type="button"
              className="se-btn se-danger"
              disabled={remove.isPending}
              onClick={() => remove.mutate(armed)}
            >
              {i18nT('apps.specEngine.configPanel.confirm_the_removal', { project: armed })}
            </button>
            <button type="button" className="se-btn" onClick={() => setArmed(null)}>
              {i18nT('apps.specEngine.configPanel.keep_the_project_entry')}
            </button>
          </div>
        </div>
      )}
      {remove.isError && (
        <Refused
          title={i18nT('apps.specEngine.configPanel.could_not_remove_the_project_entry')}
          error={remove.error}
        />
      )}
      {removed !== '' && (
        <p className="se-note" role="status">
          {i18nT('apps.specEngine.configPanel.removed_the_project_entry', { project: removed })}
        </p>
      )}
    </div>
  )
}

/** The value in force for one setting, rendered as JSON so a type is visible. */
function settingValue(value: unknown): string {
  return typeof value === 'string' ? value : JSON.stringify(value)
}

/**
 * One role's row: its resolution, where it was decided, and the reset for it.
 *
 * Model and effort are separate columns rather than one joined string, per the
 * losing mockup's shape which the reviewer preferred and correction 6 carried over:
 * they are two independent decisions and a reader scans down one of them.
 */
function RoleRow({
  role,
  profileInForce,
  document,
  onReset,
  onSelect,
  selected,
  resetting,
}: {
  role: ResolvedRole
  profileInForce: string
  document: Document
  onReset: (segments: string[]) => void
  onSelect: () => void
  selected: boolean
  resetting: boolean
}) {
  const profile = role.profile ?? ''
  // Segments, from the read's own fields. The declaring path is displayed but never
  // parsed: a profile named `thrifty.roles` has no recoverable split.
  const segments = profile ? roleSegments(profile, role.role) : []
  // And the node must lie inside the profile actually in force, compared SEGMENT for
  // segment. String matching would read `cost_profiles.thrifty.roles.roles.review`
  // as a path inside a profile named `thrifty`, so a reset offered on that row would
  // clear the role assignment of a DIFFERENT profile — one that some other project
  // selected. As segments, `thrifty.roles` and `thrifty` are two sibling names and
  // neither contains the other.
  const inForce = segments.length > 0 && isDescendant(segments, [COST_PROFILES, profileInForce])
  const present = inForce && nodeAt(document, segments) !== undefined
  const path = dotted(segments)
  return (
    <tr aria-selected={selected}>
      <td className="se-r">
        {/* The role name selects it for the match trace below. A button rather than a
            row click, so the traversal is keyboard-reachable without inventing a
            grid pattern for a five-row table. */}
        <button type="button" className="se-rolebtn" aria-pressed={selected} onClick={onSelect}>
          {role.role}
        </button>
      </td>
      <td className="se-m">
        {role.model || i18nT('apps.specEngine.configPanel.inherited_from_the_provider')}
      </td>
      <td>
        {role.effort || NONE}
        {role.dropped_effort && (
          <span className="se-flag" data-flag="dropped">
            {i18nT('apps.specEngine.configPanel.effort_dropped')}
          </span>
        )}
      </td>
      <td className="se-src">
        {role.declared_at ? (
          <b className="se-m">{role.declared_at}</b>
        ) : (
          i18nT('apps.specEngine.configPanel.session_default')
        )}
      </td>
      <td>
        {present ? (
          <button
            type="button"
            className="se-btn se-sm se-danger"
            disabled={resetting}
            onClick={() => onReset(segments)}
          >
            {/* Prose leads, but the node it clears stays in the button itself as
                the detail line, so nobody clears a profile believing they cleared
                something narrower — the path is part of the accessible name. */}
            {i18nT('apps.specEngine.configPanel.clear_the_role_assignment', { role: role.role })}
            <span className="se-btn-detail">{path}</span>
          </button>
        ) : (
          <button
            type="button"
            className="se-btn se-sm"
            disabled
            title={
              path
                ? i18nT('apps.specEngine.configPanel.no_node_exists_at', { path })
                : i18nT('apps.specEngine.configPanel.no_profile_declares_this_role')
            }
          >
            {i18nT('apps.specEngine.configPanel.nothing_to_reset')}
          </button>
        )}
      </td>
    </tr>
  )
}

/**
 * How one role's assignment was matched, segment by segment.
 *
 * The mockup showed a precedence trace for `roles.implement.effort`. Roles are not
 * registry settings, so the layers here are the ones that actually exist: the
 * profile's assignment for this role, and the session default under it. The point
 * the block makes is the one the mockup made — the match is SEGMENT-wise, so a
 * profile whose name contains a dot is one segment and not two — and it names the
 * fallback the engine reported rather than a rule restated here.
 */
function MatchTrace({ role, document }: { role: ResolvedRole; document: Document }) {
  const profile = role.profile ?? ''
  const segments = profile ? roleSegments(profile, role.role) : []
  const declared = segments.length > 0 && nodeAt(document, segments) !== undefined
  return (
    <div className="se-blk">
      <h3>
        {i18nT('apps.specEngine.configPanel.segment_wise_match')}
        {SEP}
        <span className="se-m">{role.role}</span>
      </h3>
      <div className="se-seg">
        {segments.length > 0 && (
          <div>
            <span className={declared ? 'se-hit' : 'se-miss'}>{dotted(segments)}</span>
            {SEP}
            <span className="se-m">
              {declared
                ? settingValue(nodeAt(document, segments))
                : i18nT('apps.specEngine.configPanel.unset')}
            </span>
          </div>
        )}
        <div>
          <span className={role.source === 'session_default' ? 'se-hit' : 'se-miss'}>
            {i18nT('apps.specEngine.configPanel.session_default')}
          </span>
          {SEP}
          <span className="se-m">
            {role.model || i18nT('apps.specEngine.configPanel.inherited_from_the_provider')}
          </span>
        </div>
        {/* The engine's own sentence about the fallback, not a paraphrase: the four
            fallback conditions are fixed in four different places, and the report is
            what says which one this is. */}
        {role.report && <div className="se-seg-note">{role.report}</div>}
        <div className="se-seg-note">
          {i18nT('apps.specEngine.configPanel.most_specific_segment_wins')}
        </div>
      </div>
    </div>
  )
}

/**
 * The resolved read: what is in force, and where each value was decided.
 *
 * Keyed by project, because most of the precedence only exists once a project is
 * named — without one, a project-scoped value and the profile a project selected are
 * both invisible, and a table that showed the app-wide resolution as "the" answer
 * would be wrong for every project.
 *
 * The project is the table's selection rather than this pane's own state: two
 * controls for one reading is how a pane comes to claim a resolution for a
 * project other than the one whose row is highlighted.
 */
export function ResolvedPane({ config, project }: { config: ConfigSnapshot; project: string }) {
  const client = useQueryClient()
  const [selectedRole, setSelectedRole] = useState<string>('')
  const [everySetting, setEverySetting] = useState(false)

  const resolved = useQuery({
    queryKey: QK.resolved(project),
    queryFn: () => specEngineApi.resolvedConfig(project || undefined),
    retry: false,
  })

  const reset = useMutation({
    // A reset is an ordinary configuration write: `null` at the node, through the
    // same single write path a save uses, so the engine validates the document a
    // clear produces exactly as it validates any other.
    mutationFn: (segments: string[]) => specEngineApi.writeConfig(patchAt(segments, null)),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: QK.config })
      void client.invalidateQueries({ queryKey: QK_RESOLVED_ROOT })
    },
  })

  const order = resolved.data?.role_order ?? []
  const roles = resolved.data?.roles
  const settings = resolved.data?.settings ?? []
  const configured = settings.filter((value) => !value.is_default)
  const defaults = settings.length - configured.length
  const shownSettings = everySetting ? settings : configured
  const shownRole = roles?.roles[selectedRole || order[0] || '']

  return (
    <>
      <div className="se-insp-head">
        <span className="se-insp-title">
          {project
            ? i18nT('apps.specEngine.configPanel.resolved_for_project', { project })
            : i18nT('apps.specEngine.configPanel.resolved_app_wide')}
        </span>
        <span className="se-insp-sub">
          {i18nT('apps.specEngine.configPanel.a_read_never_a_second_write_path')}
        </span>
      </div>
      <div className="se-insp-body">
        {/* `isError` is read BEFORE the data, because React Query keeps the last
            successful answer across a failed refetch. A reading that reached for
            the data first would render the previous project's values, or the
            app-wide ones, as this project's resolution — with the head above
            naming this project. Doubt has to look like doubt. */}
        {resolved.isError ? (
          <Refused
            title={i18nT('apps.specEngine.configPanel.could_not_resolve_the_configuration')}
            error={resolved.error}
          />
        ) : resolved.isPending ? (
          <p className="se-note">{i18nT('apps.specEngine.configPanel.resolving')}</p>
        ) : (
          <>
            <div className="se-blk">
              <h3>{i18nT('apps.specEngine.configPanel.per_role_model_and_effort')}</h3>
              {roles && roles.profile === '' && (
                <p className="se-note">
                  {roles.requested_profile
                    ? i18nT('apps.specEngine.configPanel.the_selected_profile_is_not_defined', {
                        profile: roles.requested_profile,
                      })
                    : i18nT('apps.specEngine.configPanel.no_cost_profile_is_selected')}
                </p>
              )}
              <table className="se-roles">
                <thead>
                  <tr>
                    <th>{i18nT('apps.specEngine.configPanel.col_role')}</th>
                    <th>{i18nT('apps.specEngine.configPanel.col_model')}</th>
                    <th>{i18nT('apps.specEngine.configPanel.col_effort')}</th>
                    <th>{i18nT('apps.specEngine.configPanel.col_from')}</th>
                    <th>{i18nT('apps.specEngine.configPanel.col_reset')}</th>
                  </tr>
                </thead>
                <tbody>
                  {order.map((name) => {
                    const role = roles?.roles[name]
                    if (!role) return null
                    return (
                      <RoleRow
                        key={name}
                        role={role}
                        profileInForce={roles?.profile ?? ''}
                        document={config.document}
                        resetting={reset.isPending}
                        selected={name === (selectedRole || order[0])}
                        onSelect={() => setSelectedRole(name)}
                        onReset={(segments) => {
                          setSelectedRole(name)
                          reset.mutate(segments)
                        }}
                      />
                    )
                  })}
                </tbody>
              </table>
              {/* The departure from the mockup, and the fact a bare `Reset` label
                  would hide: the node is the PROFILE's, and a profile is shared by
                  every project that selected it. */}
              <p className="se-note">
                {i18nT('apps.specEngine.configPanel.a_role_lives_on_the_shared_profile')}
              </p>
              {reset.isError && (
                <Refused
                  title={i18nT('apps.specEngine.configPanel.could_not_clear_the_role')}
                  error={reset.error}
                />
              )}
            </div>

            {shownRole && <MatchTrace role={shownRole} document={config.document} />}

            <div className="se-blk">
              <h3>
                {everySetting
                  ? i18nT('apps.specEngine.configPanel.every_setting_in_force')
                  : i18nT('apps.specEngine.configPanel.values_not_at_their_default')}
              </h3>
              {shownSettings.length === 0 ? (
                <p className="se-note">
                  {i18nT('apps.specEngine.configPanel.every_setting_is_at_its_bundled_default')}
                </p>
              ) : (
                <dl className="se-kv">
                  {shownSettings.map((value) => {
                    const label = settingLabel(value.key)
                    return (
                      <Fragment key={value.key}>
                        {/* Prose leads and the registry key follows as the detail
                            line: the key is what the document and the write log
                            speak, so it stays visible, but a reader should not
                            need to think in registry keys to scan the list. A key
                            without a label renders as it always has — the axes
                            are the engine's, and a setting added there must show
                            up here without a frontend edit. */}
                        <dt>
                          {label ? (
                            <>
                              {label}
                              <span className="se-kv-path">{value.key}</span>
                            </>
                          ) : (
                            <span className="se-m">{value.key}</span>
                          )}
                        </dt>
                        <dd>
                          {settingValue(value.value)}
                          <span className="se-note">
                            {SEP}
                            {i18nT(ORIGIN_KEY[value.origin])}
                            {value.declared_at ? `${SEP}${value.declared_at}` : ''}
                          </span>
                        </dd>
                      </Fragment>
                    )
                  })}
                </dl>
              )}
              <p className="se-note">
                {i18nT('apps.specEngine.configPanel.settings_at_their_bundled_default')}
                {SEP}
                <span className="se-m">{fmtNumber(Math.max(defaults, 0))}</span>
              </p>
              {/* The default-valued settings are collapsed rather than absent: the
                  origin is the whole point of this read, and a setting whose value
                  equals the default because somebody PINNED it there is only
                  distinguishable from an untouched one by reading its origin. In
                  flow, so nothing is drawn over the page to show them. */}
              <div className="se-acts" style={{ marginTop: 8 }}>
                <button
                  type="button"
                  className="se-btn se-sm"
                  aria-pressed={everySetting}
                  onClick={() => setEverySetting((shown) => !shown)}
                >
                  {everySetting
                    ? i18nT('apps.specEngine.configPanel.show_only_values_not_at_their_default')
                    : i18nT('apps.specEngine.configPanel.show_every_setting', {
                        count: fmtNumber(settings.length),
                      })}
                </button>
              </div>
            </div>
          </>
        )}
      </div>
    </>
  )
}
