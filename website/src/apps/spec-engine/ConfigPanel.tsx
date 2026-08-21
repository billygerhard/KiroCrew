/**
 * The configuration pane: the document as the write path, the resolution beside it.
 *
 * Built to `design/mockup-b.html`'s config pane, in the same split the queue uses —
 * the document on the left where the list was, its resolved read on the right where
 * the inspector was.
 *
 * ## `config.json` is the write path, and there is only one
 *
 * The left pane edits the document and saves it through `PUT /config`, which is
 * `ConfigStore.write`: the engine merges, validates the MERGED document, and
 * persists it atomically under a lock. Every rule an operator can trip — an unknown
 * key, an out-of-range value, a setting written at a scope it is not overridable at
 * — is the engine's, reported back by path, so this panel keeps no validation of its
 * own beyond "is this JSON at all". The right pane writes NOTHING; it is a read, and
 * the only writes it offers are per-role resets that go through the same PUT.
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
import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from 'react'
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
  type ResolvedRole,
  type SourceGridCell,
  type SourcesPayload,
} from './api'
import {
  COST_PROFILES,
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
  roleSegments,
  sameCell,
  type Document,
  type GridCellRef,
  type PendingEdit,
} from './configDocument'

/** Separator between two identifiers on one line. Punctuation, not copy. */
const SEP = ' \u00b7 '

/** Stands in for a field the engine has no value for. Punctuation, not copy. */
const NONE = '\u2014'

/** The resolved read with no project named. Not a project id, so not a valid one. */
const APP_WIDE = ''

/** React key for the app-defaults row, whose id is deliberately not a project. */
const APP_DEFAULTS_ROW_KEY = 'app-defaults'

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

/** The refusal code behind an error, or `''` when it is not one of ours. */
function codeOf(error: unknown): string {
  return error instanceof SpecEngineApiError ? error.code : ''
}

/** A refusal block: the sentence a reader acts on, with the code underneath. */
function Refused({ title, error }: { title: string; error: unknown }) {
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
 * Three sentences rather than one with the origin interpolated, because the three
 * edits are three different acts: replacing a level somebody chose for this pair,
 * NARROWING a broader rule that also answers other pairs, and configuring a pair
 * nothing had answered. The wildcard sentence carries the narrowing statement
 * itself — the write creates this pair's own cell and leaves the broader rule
 * alone — because a separate line repeating the same pair is a line a reader skips.
 *
 * Keys, not resolved strings, for `ORIGIN_KEY`'s reason.
 */
const EDIT_SENTENCE_KEY: Record<SourceGridCell['origin'], string> = {
  exact: 'apps.specEngine.sourcesSection.edit_replaces_the_pairs_own_level',
  wildcard: 'apps.specEngine.sourcesSection.edit_narrows_a_broader_rule',
  default: 'apps.specEngine.sourcesSection.edit_configures_an_unconfigured_pair',
}

/**
 * The exact change a confirm would write, before it is written.
 *
 * The patch is shown as the payload itself, pretty-printed: approving a plan means
 * approving what will be written, which is the setup flow's rule and the reason this
 * card exists at all. The sentences beside it are not a second description of the
 * patch — they say what each line MEANS, which the JSON cannot: the level that is in
 * force now, the declaration that put it there, and the level replacing it.
 *
 * Two consequences get their own statement because neither is legible in the patch:
 *
 * 1. **Raising the least-trusted class.** That class is where an author the engine
 *    cannot classify lands, so a rung granted there is a rung granted to anyone at
 *    all. Nothing in the JSON says which class that is.
 * 2. **Narrowing rather than modifying.** An edit on a pair a broader rule answered
 *    writes the pair's own cell; the broader rule stays, and keeps answering the
 *    pairs it answered before. A reader who assumed the broader rule was being
 *    edited would expect other cells to move, and they do not.
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
  onConfirm: () => void
  onDiscard: () => void
}) {
  const raising = reviewed.filter(
    ({ edit, cell }) => edit.klass === leastTrusted && raisesLevel(levels, cell, edit.level),
  )
  return (
    <div className="se-qbox">
      <h3>{i18nT('apps.specEngine.sourcesSection.the_change_that_would_be_written')}</h3>
      {/* The payload itself. Not a rendering of it: a summary an operator approves
          is a summary the write can differ from without anybody noticing. */}
      <pre className="se-json se-gpatch">{JSON.stringify(patch, null, 2)}</pre>
      {reviewed.map(({ edit, cell }) => (
        <p className="se-note" key={dotted(gridCellSegments(edit))}>
          {i18nT(EDIT_SENTENCE_KEY[cell.origin], {
            source: edit.source,
            klass: edit.klass,
            specType: edit.specType,
            oldLevel: cell.level,
            newLevel: edit.level,
            declaredAt: cell.declared_at,
            path: dotted(gridCellSegments(edit)),
          })}
        </p>
      ))}
      {raising.length > 0 && (
        /* In flow under the patch, never a dialog: the same rule the removal
           confirmation follows, and for the same reason — a consequence stated in
           an overlay is a consequence stated where it can be dismissed. */
        <div className="se-arm">
          {raising.map(({ edit, cell }) => (
            <p key={dotted(gridCellSegments(edit))}>
              <AlertTriangle className="lucide-inline" aria-hidden="true" />
              {i18nT('apps.specEngine.sourcesSection.this_raises_the_least_trusted_class', {
                klass: edit.klass,
                specType: edit.specType,
                oldLevel: cell.level,
                newLevel: edit.level,
              })}
            </p>
          ))}
        </div>
      )}
      <div className="se-acts" style={{ marginTop: 9 }}>
        <button type="button" className="se-btn se-danger" disabled={writing} onClick={onConfirm}>
          {writing
            ? i18nT('apps.specEngine.configPanel.saving')
            : i18nT('apps.specEngine.sourcesSection.write_the_change')}
        </button>
        <button type="button" className="se-btn" disabled={writing} onClick={onDiscard}>
          {i18nT('apps.specEngine.sourcesSection.discard_the_pending_changes')}
        </button>
      </div>
      <p className="se-note">
        {i18nT('apps.specEngine.sourcesSection.a_confirm_writes_exactly_this_patch')}
      </p>
      {error !== null && (
        <>
          <Refused
            title={i18nT('apps.specEngine.sourcesSection.could_not_write_the_grid_change')}
            error={error}
          />
          {/* The refusal alone would leave open which of the two states the page is
              in. Nothing was written, so the matrix above is still the store's, and
              the choices are still here to be corrected and sent again. */}
          <p className="se-note">
            {i18nT('apps.specEngine.sourcesSection.nothing_was_written_so_the_matrix_is_stored_state')}
          </p>
        </>
      )}
    </div>
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
 */
function SourcesSection() {
  const client = useQueryClient()
  const [chosen, setChosen] = useState('')
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

  // A choice whose source has left the document would RE-CREATE it: the patch
  // writes `sources.<name>.autonomy.<class>.<type>`, and the merge would resurrect
  // a source entry carrying an autonomy grid and none of the fields that make it a
  // source. The removal can arrive from the editor below, from another surface, or
  // on any refetch, so the choice is dropped rather than carried.
  useEffect(() => {
    setEdits((current) => {
      const kept = current.filter((edit) => names.includes(edit.source))
      return kept.length === current.length ? current : kept
    })
  }, [names])

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
      <div className="se-blk">
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
      <div className="se-blk">
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
    <div className="se-blk">
      <h3>{i18nT('apps.specEngine.sourcesSection.watch_sources')}</h3>
      {names.length === 0 ? (
        /* Not an empty matrix: a grid with no source to belong to reads as "no
           authority is granted", when the fact is that nothing is being ingested at
           all. The offer flow is named because that is the only place a source comes
           from — this section edits grids and never creates one. */
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
                onClick={() => setChosen(name)}
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
              onConfirm={() => write.mutate(patch)}
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

/**
 * The document, edited and saved through the engine's one write path.
 *
 * The editor holds text rather than a parsed object, because half-typed JSON is a
 * legitimate state of an editor and a parsed model cannot represent it. `text ===
 * null` means "showing what the read returned", which is what makes the revert
 * exact: it drops local text and shows the document again rather than reconstructing
 * a copy of it.
 */
function DocumentEditor({ config }: { config: ConfigSnapshot }) {
  const client = useQueryClient()
  const [text, setText] = useState<string | null>(null)
  const [localError, setLocalError] = useState('')
  const [saved, setSaved] = useState<ConfigAdvisory[] | null>(null)
  const [empty, setEmpty] = useState(false)

  const baseline = config.document
  const shown = text ?? documentText(baseline)
  const dirty = text !== null && text !== documentText(baseline)

  const save = useMutation({
    mutationFn: (patch: Document) => specEngineApi.writeConfig(patch),
    onSuccess: (result) => {
      setSaved(result.advisories)
      // Dropped rather than replaced with the merged document: the read is the
      // authority on what is persisted (and on what is elided), so the editor goes
      // back to showing it.
      setText(null)
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
          setText(event.target.value)
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
            setText(null)
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
  const node = document.projects
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
function ProjectsTable({
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
    // A removal is an ordinary configuration write: `null` at the entry, through
    // the same single write path a save uses. The engine's merge deletes a key
    // whose patch value is null, so no delete route has to exist — and the write
    // is validated, locked and recorded exactly like every other one.
    mutationFn: (name: string) => specEngineApi.writeConfig({ projects: { [name]: null } }),
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
            {/* Named with the node it clears, so nobody clears a profile believing
                they cleared something narrower. */}
            {i18nT('apps.specEngine.configPanel.clear_node', { path })}
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
function ResolvedPane({ config, project }: { config: ConfigSnapshot; project: string }) {
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
                  {shownSettings.map((value) => (
                    <Fragment key={value.key}>
                      <dt className="se-m">{value.key}</dt>
                      <dd>
                        {settingValue(value.value)}
                        <span className="se-note">
                          {SEP}
                          {i18nT(ORIGIN_KEY[value.origin])}
                          {value.declared_at ? `${SEP}${value.declared_at}` : ''}
                        </span>
                      </dd>
                    </Fragment>
                  ))}
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

/**
 * The whole configuration pane: the document, and the resolution beside it.
 *
 * Takes the config read rather than performing its own, so the page's first-run
 * detection and this pane cannot disagree about whether a document exists — and a
 * read that FAILED is rendered here as the refusal it is, because `config_unreadable`
 * means a document exists and cannot be parsed, which is a repair and not an empty
 * form to fill in.
 */
export function ConfigPane({
  config,
  error,
  pending,
}: {
  config: ConfigSnapshot | undefined
  error: unknown
  pending: boolean
}) {
  // The selected project lives here, above both halves of the pane: the table on
  // the left is what selects it and the resolved read on the right is what it is
  // read FOR, and a copy on either side is how the two come to disagree.
  const [chosenProject, setChosenProject] = useState<string>(APP_WIDE)
  // Normalized against the document itself, not against how the entry left it:
  // a selection whose entry is gone — removed through its row, deleted in the
  // JSON editor beside the table, or dropped by an external write picked up on
  // refetch — falls back to app defaults. Without this, no row matches, the
  // grid loses its only tab stop, and the resolved view renders the app-wide
  // layers under a heading naming a project the document no longer lists —
  // which reads as "this project inherits everything" rather than "this
  // project is gone".
  const documentProjects = config?.document.projects
  const chosenKnown =
    chosenProject === APP_WIDE ||
    (isObject(documentProjects) &&
      Object.prototype.hasOwnProperty.call(documentProjects, chosenProject))
  const project = chosenKnown ? chosenProject : APP_WIDE
  // The stored state collapses too, so a later re-add of the same name cannot
  // silently snap the selection back to it with no operator action.
  useEffect(() => {
    if (!chosenKnown) setChosenProject(APP_WIDE)
  }, [chosenKnown])
  return (
    <>
      <section className="se-cfg">
        <div className="se-cfg-head">
          {/* A filename, not copy: translating it would name a file that does not exist. */}
          <h1 className="se-m">config.json</h1>
          <span className="se-sort">
            {i18nT('apps.specEngine.specEnginePage.the_write_path_validated_on_save')}
          </span>
        </div>
        <div className="se-cfg-body">
          {error ? (
            <Refused
              title={i18nT('apps.specEngine.specEnginePage.could_not_read_the_configuration')}
              error={error}
            />
          ) : pending || !config ? (
            <p className="se-note">
              {i18nT('apps.specEngine.specEnginePage.reading_the_configuration')}
            </p>
          ) : (
            <>
              {/* Above the document rather than below it: the question this pane
                  is opened with is "which configuration governs which project",
                  and the answer must not sit under a fixed-height editor. */}
              <ProjectsTable config={config} project={project} onSelect={setChosenProject} />
              {/* Beside the projects table for the same reason it is above the
                  editor: who may run how unattended is a reading, and reading it
                  out of the JSON below is not the same as seeing the matrix. */}
              <SourcesSection />
              <DocumentEditor config={config} />
            </>
          )}
        </div>
      </section>
      <section
        className="se-inspector"
        aria-label={i18nT('apps.specEngine.specEnginePage.resolved_configuration')}
      >
        {error || pending || !config ? (
          <div className="se-insp-body">
            <p className="se-note">
              {i18nT('apps.specEngine.configPanel.the_resolved_read_needs_the_document_first')}
            </p>
          </div>
        ) : (
          <ResolvedPane config={config} project={project} />
        )}
      </section>
    </>
  )
}