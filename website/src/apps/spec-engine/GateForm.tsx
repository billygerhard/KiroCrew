/**
 * The quality gates, as a form: the checks this installation insists on.
 *
 * A gate is a named list of commands the engine RUNS against a change, with a
 * position relative to raising the review artifact and a severity that decides
 * whether a failure stops the flow. So this is the second surface on the pane whose
 * subject is something that executes, and the properties below are safety claims
 * rather than arrangement.
 *
 * ## `[]` and `null` are different answers, and one may never stand in for the other
 *
 * `GET /config/workflow` sends `gates: []` when the document configures no gate, and
 * `gates: null` with `gates_unreadable` when the stored list could not be parsed. The
 * engine refuses delivery OUTRIGHT in the second case — `load_quality_gates` raises
 * rather than returning nothing — so rendering it as "no gate is configured" would
 * tell an operator that nothing is configured when what is true is that every check
 * is off until the document is repaired. The two call for opposite actions, they are
 * two distinct blocks here, and the unreadable one offers no write at all.
 *
 * The empty answer states the other half an operator needs: the engine floor — format
 * validation, phase gates, autonomy resolution, budget enforcement, the claim ledger
 * and the audit log — is not a gate and cannot be configured off, so "no gate runs"
 * is not "no check runs".
 *
 * ## The vocabularies are the engine's; only the words are here
 *
 * Positions and severities come from `/config/registry`'s `gate_positions` and
 * `gate_severities`, which are the write door's own tuples — their own tuples rather
 * than a `Setting.choices`, because no setting declares choices and a test is armed
 * to fail the moment one does. Absent means an OLDER GATEWAY, not an empty
 * vocabulary, so the form says it cannot offer a value rather than offering a list it
 * invented.
 *
 * Each value carries a plain-language statement of what it does to a RUN, because
 * `pre_submit` and `blocking` name a mechanism while an operator is deciding a
 * consequence: a blocking failure before the artifact is raised means the artifact is
 * never raised, a blocking failure after it means the change is not published, and an
 * advisory failure is recorded without stopping the run.
 *
 * `both` is offered as its own position rather than as two gates, because a gate's
 * position is not a property of the check: an analyzer worth running before a human
 * sees the change is usually worth re-running on the artifact, and spelling that as
 * two gates would put one check in the audit record under two names with two
 * independently editable severities.
 *
 * ## Gates are app-wide, and the form says so instead of implying a scope
 *
 * `load_quality_gates` takes no project and a project cannot select a different set,
 * which the payload states in `gates_scope_is_app`. No project control is offered
 * here, and the sentence is on screen above the list rather than left to be inferred
 * from the stage the list sits under.
 *
 * ## The section is a LIST, so every write is a write of the whole list
 *
 * `quality_gates` is a JSON array and the store's merge replaces an array wholesale,
 * so there is no per-gate path to stage: ONE staged edit at `quality_gates` carries
 * the entire intended list, however many gates it touches. Three consequences shape
 * the rest of the form:
 *
 * 1. **The write is composed from the stored DOCUMENT, never from the route's rows.**
 *    The route sanitizes and length-caps a gate's name so a hand-edited document
 *    cannot set the width of a row, which makes that name a display value rather than
 *    the stored key — writing it back would RENAME the gate. So the route's payload
 *    answers the questions only it can (unreadable versus empty, the declaring path,
 *    the engine's own reading of a severity) and the editable rows come from the
 *    pane's own read of the document, handed in as `document`.
 *
 *    The two per-row answers taken from the route — the declaring path and the
 *    engine's reading of the severity — are keyed by the gate's position in the
 *    STORED list rather than by its position in the rows on screen, because a
 *    staged removal shifts every later row while the route still describes the
 *    document. The severity reading is withheld rather than relayed when a draft has
 *    changed that gate's severity, since the stored flag then describes the severity
 *    being replaced.
 * 2. **Expressibility is a property of the whole list, not of one gate.** A write
 *    carries every entry, so one entry this form cannot represent would be reshaped by
 *    a change to a different one. When any entry fails {@link storedGates} the form
 *    shows what is stored, states what stops it, and offers no write.
 * 3. **One staged edit is one unwritten change.** The badge, the form's own count and
 *    the patch all read the same list, so a review naming four gate edits still
 *    reports one unwritten change — because one path is written.
 *
 * The document read and the workflow read are separate requests and can straddle a
 * write, so they are checked against each other before any write is offered: the same
 * number of gates, and the same position and severity at each index. Those two fields
 * are closed vocabularies in both readings, so the comparison needs no copy of the
 * route's sanitizing here — and names, which differ by design, are never compared and
 * never written from the display.
 *
 * ## What an operator may change
 *
 * A gate's position, its severity and its commands are editable; a gate is added as a
 * copy of a bundled definition, so a gate can be added without composing commands at
 * all; a gate is removed behind a confirmation that names it. Commands are entered one
 * per line with arguments separated by whitespace, and a stored argv that would not
 * survive that round trip — an empty argument, or whitespace inside one — makes the
 * list not expressible rather than being quietly reshaped.
 *
 * A duplicate name is refused against the NAME field with the entered gate retained,
 * this pane's established refusal shape: the document cannot express two gates of one
 * name, and clearing the block would make an operator re-choose a position, a severity
 * and a command list in order to correct a name.
 *
 * No rename is offered, for the reason the watch-source form gives: a gate's name is
 * its identity in the audit record and in every fix task dispatched for it, and a
 * rename is indistinguishable from removing one gate and adding another.
 *
 * ## Two bounds, stated rather than hidden
 *
 * An editable row shows the DOCUMENT's own name rather than the route's capped one,
 * because this form writes that name and a control must show what it writes. The cap
 * exists to stop a hand-edited document setting the width of a read-only display; a
 * write surface has the opposite obligation.
 *
 * `sanitized()` also strips unprintable characters, and a name that lost one is
 * indistinguishable on this side from a name that never had one. It cannot reach the
 * write — the write is the document's own node — so the effect is confined to the
 * route-supplied declaring path beside a row.
 */
import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle } from 'lucide-react'

import { fmtNumber } from '../../i18n/format'
import { i18nT } from '../../i18n/t'

import {
  QK,
  QK_RESOLVED_ROOT,
  specEngineApi,
  type GatePreset,
  type QualityGate,
} from './api'
import {
  FormReview,
  PendingCount,
  Refused,
  type Consequence,
  type ReviewedChange,
} from './ConfigPanel'
import {
  QUALITY_GATES,
  buildFormPatch,
  dotted,
  isObject,
  nodeAt,
  type Document,
} from './configDocument'
import { useStagedEdits } from './useStagedEdits'

/** Separator between two identifiers on one line. Punctuation, not copy. */
const SEP = ' \u00b7 '

/** Stands in for a statement this pane has no words for. Punctuation, not copy. */
const NONE = '\u2014'

/** The four keys a gate declaration may carry. The write door refuses any other. */
const GATE_KEYS: readonly string[] = ['name', 'position', 'severity', 'commands']

/**
 * What each gate POSITION does to a run, in words, keyed by the engine's own value.
 *
 * Keys rather than resolved strings: a module-level `i18nT()` runs once at import and
 * would freeze this table in whichever language happened to be active then — the
 * `ORIGIN_KEY` idiom, and the reason the stage labels are keys too.
 *
 * The statements are about the RUN and not about the mechanism. `pre_submit` names a
 * point in the flow; what an operator is deciding is whether a failure there means the
 * review artifact is never raised at all.
 */
const POSITION_EFFECT_KEY: Record<string, string> = {
  pre_submit: 'apps.specEngine.gateForm.position_pre_submit_effect',
  post_submit: 'apps.specEngine.gateForm.position_post_submit_effect',
  both: 'apps.specEngine.gateForm.position_both_effect',
}

/** The label for each gate position. A value absent here renders as its own token. */
const POSITION_LABEL_KEY: Record<string, string> = {
  pre_submit: 'apps.specEngine.gateForm.position_pre_submit',
  post_submit: 'apps.specEngine.gateForm.position_post_submit',
  both: 'apps.specEngine.gateForm.position_both',
}

/** What each SEVERITY does to a run, in words. Keys, for the table above's reason. */
const SEVERITY_EFFECT_KEY: Record<string, string> = {
  blocking: 'apps.specEngine.gateForm.severity_blocking_effect',
  advisory: 'apps.specEngine.gateForm.severity_advisory_effect',
}

/** The label for each gate severity. */
const SEVERITY_LABEL_KEY: Record<string, string> = {
  blocking: 'apps.specEngine.gateForm.severity_blocking',
  advisory: 'apps.specEngine.gateForm.severity_advisory',
}

/**
 * The words for a position, or the engine's own token when this pane has none.
 *
 * The token rather than nothing, for the reason a setting with no authored label
 * renders by key: the vocabulary is the engine's and it may grow, and a row that hid
 * a position the engine declares would hide what a gate actually does.
 */
export function positionLabel(position: string): string {
  // Indexed at the call site rather than through a local, so the key-reference gate
  // resolves every entry in the map — the ORIGIN_KEY idiom.
  return POSITION_LABEL_KEY[position] ? i18nT(POSITION_LABEL_KEY[position]) : position
}

/** The plain-language consequence of *position*, or `''` when this pane has none. */
export function positionEffect(position: string): string {
  return POSITION_EFFECT_KEY[position] ? i18nT(POSITION_EFFECT_KEY[position]) : ''
}

/** The words for a severity, or the engine's own token when this pane has none. */
export function severityLabel(severity: string): string {
  return SEVERITY_LABEL_KEY[severity] ? i18nT(SEVERITY_LABEL_KEY[severity]) : severity
}

/** The plain-language consequence of *severity*, or `''` when this pane has none. */
export function severityEffect(severity: string): string {
  return SEVERITY_EFFECT_KEY[severity] ? i18nT(SEVERITY_EFFECT_KEY[severity]) : ''
}

/** A gate as this form holds it: exactly the four keys a declaration may carry. */
export interface GateDraft {
  name: string
  position: string
  severity: string
  commands: string[][]
}

/**
 * Whether *argv* survives the text form this pane enters commands in.
 *
 * One command per line with arguments separated by whitespace cannot express an empty
 * argument or an argument containing whitespace. Rather than reshape such a command on
 * the way through a control that cannot show it, the list carrying it is declared not
 * expressible — the rule the watch-source form applies to a poll no preset matches.
 */
function argvRoundTrips(argv: readonly unknown[]): boolean {
  return (
    argv.length > 0 &&
    argv.every(
      (argument) => typeof argument === 'string' && argument !== '' && !/\s/.test(argument),
    )
  )
}

/**
 * The stored gate list as drafts, or `null` when this form cannot express it.
 *
 * Read from the DOCUMENT rather than from the route's rows, because this is what the
 * write is composed from: the route's names are sanitized for display, and writing one
 * back would rename the gate.
 *
 * An absent section is an empty list and NOT a refusal — that is what an unconfigured
 * document legitimately holds. `null` is reserved for a shape a whole-list rewrite
 * would silently change, and the last of the four is the one a reader would not
 * predict: `load_quality_gates` IGNORES a key it does not know while the write door
 * REFUSES one, so a hand-edited gate carrying an extra field renders through the route
 * and would be written back without it — and that write would be accepted.
 */
export function storedGates(document: Document): GateDraft[] | null {
  const node = nodeAt(document, [QUALITY_GATES])
  if (node === undefined || node === null) return []
  if (!Array.isArray(node)) return null
  const drafts: GateDraft[] = []
  for (const entry of node) {
    if (!isObject(entry)) return null
    for (const key of Object.keys(entry)) {
      if (!GATE_KEYS.includes(key)) return null
    }
    const { name, position, severity, commands } = entry
    if (typeof name !== 'string' || name.trim() === '') return null
    if (typeof position !== 'string' || typeof severity !== 'string') return null
    if (!Array.isArray(commands) || commands.length === 0) return null
    const argvs: string[][] = []
    for (const argv of commands) {
      if (!Array.isArray(argv) || !argvRoundTrips(argv)) return null
      argvs.push(argv.map((argument) => String(argument)))
    }
    drafts.push({ name, position, severity, commands: argvs })
  }
  return drafts
}

/** The commands of one gate as text: one command per line. */
export function commandsText(commands: readonly string[][]): string {
  return commands.map((argv) => argv.join(' ')).join('\n')
}

/**
 * The commands a typed block names: one command per line, arguments on whitespace.
 *
 * Blank lines are dropped rather than becoming empty commands, so an operator pressing
 * return twice does not compose a command with no program.
 */
export function parseCommands(text: string): string[][] {
  return text
    .split('\n')
    .map((line) =>
      line
        .trim()
        .split(/\s+/)
        .filter((argument) => argument !== ''),
    )
    .filter((argv) => argv.length > 0)
}

/** A gate draft as the document stores it: the four keys, in the engine's order. */
function gateValue(draft: GateDraft): Document {
  return {
    name: draft.name,
    position: draft.position,
    severity: draft.severity,
    commands: draft.commands.map((argv) => [...argv]),
  }
}

/** One gate list as a comparable string, for deciding whether a draft is a change. */
function listSignature(gates: readonly GateDraft[]): string {
  return JSON.stringify(gates.map(gateValue))
}

/**
 * Whether the document read and the workflow read describe one document.
 *
 * They are two requests and can straddle a write. Compared on COUNT and on each
 * index's position and severity — closed vocabularies in both readings, so the
 * comparison needs no copy of the route's sanitizing here. Names are deliberately not
 * compared: they differ by design, because one reading caps them for display.
 */
export function readsAgree(
  stored: readonly GateDraft[],
  shown: readonly QualityGate[],
): boolean {
  if (stored.length !== shown.length) return false
  return stored.every(
    (draft, index) =>
      draft.position === shown[index].position && draft.severity === shown[index].severity,
  )
}

/**
 * A button group over a closed vocabulary, each option stating what it does.
 *
 * A group rather than a dropdown, for the level control's reason: a popup would be
 * drawn over a page whose kill-switch strip must never be covered, and a native
 * `<select>` is an eslint error here for the same reason.
 *
 * The label leads and the engine's own token is the row's `data-value` rather than the
 * button's text, because an operator choosing a position is choosing a consequence and
 * `pre_submit` states a mechanism. The consequence rides on the control itself as its
 * title, not only in a note beside the group: a reader choosing between two buttons
 * should not have to match a label to a sentence somewhere else on the block.
 */
function VocabularyChoice({
  label,
  values,
  chosen,
  optionLabel,
  detail,
  onChoose,
}: {
  label: string
  values: readonly string[]
  chosen: string
  optionLabel: (value: string) => string
  /** The plain-language consequence of a value, or `''` when this pane has none. */
  detail: (value: string) => string
  onChoose: (value: string) => void
}) {
  return (
    <div className="se-acts" role="group" aria-label={label}>
      {values.map((value) => (
        <button
          key={value}
          type="button"
          className="se-btn se-sm"
          data-value={value}
          aria-pressed={value === chosen}
          title={detail(value)}
          onClick={() => onChoose(value)}
        >
          {optionLabel(value)}
        </button>
      ))}
    </div>
  )
}

/**
 * The quality gates, listed and editable, with the engine floor named beside them.
 *
 * `document` is the pane's own read rather than a second one of this form's, so the
 * two cannot disagree about the store — and the pane already states the refusal when
 * that read fails, which is why there is no read-failure arm for it here.
 *
 * `project` is passed only so this form shares the cache entry the delivery workflow
 * surface reads: one request rather than two for one payload. It does NOT narrow the
 * gate list — gates are app-wide, which the block says on screen.
 */
export function GateForm({
  document,
  project,
  onPendingCount,
}: {
  document: Document
  project: string
  /** Report how many staged changes this form would review, for the stage badge. */
  onPendingCount?: (count: number) => void
}) {
  const client = useQueryClient()
  const edits = useStagedEdits()
  // The intended list while the operator is composing one, or null while the form
  // shows exactly what is stored. Null rather than an eager copy of the stored list,
  // so a refetch that changes the stored list is not silently overwritten by a draft
  // nobody has touched.
  const [draft, setDraft] = useState<GateDraft[] | null>(null)
  // The stored list the open draft was composed against, as a signature. The draft is
  // withdrawn when the two stop matching; see the effect below.
  const [draftBase, setDraftBase] = useState('')
  // Command text as TYPED, keyed by gate name, because the control's value is
  // otherwise derived from the parsed argv: a trailing space would be parsed away and
  // the caret would jump, and an operator cannot correct text they cannot finish
  // typing. The watch-source form's `repoInput` buffer, one per gate.
  const [text, setText] = useState<Record<string, string>>({})
  // The gate whose removal is armed, and the name typed to confirm it. A NAME rather
  // than an index: an arm that outlived a refetch would offer a confirmation captioned
  // with one gate and staged against another.
  const [armed, setArmed] = useState<string | null>(null)
  const [typedName, setTypedName] = useState('')
  const [removalRefused, setRemovalRefused] = useState(false)
  const [addName, setAddName] = useState('')
  const [addText, setAddText] = useState('')
  const [addPosition, setAddPosition] = useState('')
  const [addSeverity, setAddSeverity] = useState('')
  // The name an add was refused for, so the refusal is stated AT the name field. Held
  // rather than derived from "this name is taken", because the operator must be able
  // to keep the entered gate and correct only the name — and a derived refusal would
  // caption a name they have not tried to add yet.
  const [nameRefused, setNameRefused] = useState('')
  // A draft withdrawn because the stored list changed under it, stated where it was
  // composed: an edit that silently stopped counting reads as one never made.
  const [withdrawn, setWithdrawn] = useState(false)
  const [reviewing, setReviewing] = useState(false)
  const [wrote, setWrote] = useState(false)

  // The document, read through the key the whole pane shares rather than threaded in
  // as a prop — the review card's own idiom, and for its reason: this costs no second
  // request, and the alternative is one more place the read can be forgotten.
  const workflow = useQuery({
    queryKey: QK.workflow(project),
    queryFn: () => specEngineApi.configWorkflow(project || undefined),
    retry: false,
  })
  const registry = useQuery({
    queryKey: QK.registry,
    queryFn: () => specEngineApi.configRegistry(),
    retry: false,
    // A projection of the engine's own constants, so it cannot change while the page
    // is open. The same key every other form reads, so all of them share one answer.
    staleTime: Infinity,
  })

  const write = useMutation({
    mutationFn: (patch: Document) => specEngineApi.writeConfig(patch),
    onSuccess: () => {
      edits.clear()
      setDraft(null)
      setText({})
      setArmed(null)
      setTypedName('')
      setRemovalRefused(false)
      setAddName('')
      setAddText('')
      setAddPosition('')
      setAddSeverity('')
      setNameRefused('')
      setWithdrawn(false)
      setReviewing(false)
      setWrote(true)
      // The reply's merged document is NOT adopted: the read is this pane's authority
      // on what is persisted, and every row here is a reading OF it. The workflow key
      // is named as well as the document's, because the engine's reading of the gates
      // comes from that route and a stale list beside a fresh document would still
      // show the gate an operator just removed.
      void client.invalidateQueries({ queryKey: QK.config })
      void client.invalidateQueries({ queryKey: QK.workflow(project) })
      void client.invalidateQueries({ queryKey: QK_RESOLVED_ROOT })
    },
    // No `onError`: a refusal must leave the draft in place and the queries untouched,
    // so the form keeps showing the store's own state.
  })

  // `isError` before the data everywhere below, this pane's rule: React Query keeps
  // the last successful body across a failing refetch, so a list read from retained
  // data would state gates nobody re-read as the ones in force.
  const shown = workflow.isError ? undefined : workflow.data
  const vocabulary = registry.isError ? undefined : registry.data
  const positions = vocabulary?.gate_positions ?? []
  const severities = vocabulary?.gate_severities ?? []
  const presets: readonly GatePreset[] = vocabulary?.gate_presets ?? []

  const stored = useMemo(() => storedGates(document), [document])
  const storedKey = stored === null ? '' : listSignature(stored)

  // A draft outlives the answer it was made against: a write from the document editor,
  // from another surface, or an external edit picked up on refetch changes the stored
  // list, and a draft composed against the old one would write back gates somebody
  // else deleted. Withdrawn against the CURRENT answer rather than trusted to hold
  // still between a choice and its confirm.
  const { clear } = edits
  useEffect(() => {
    if (draft !== null && draftBase !== storedKey) {
      setDraft(null)
      setText({})
      setWithdrawn(true)
      clear()
    }
  }, [draft, draftBase, storedKey, clear])

  // The gates a confirm could stage a removal against. An arm outlives its gate for
  // the reason above, so it is dropped when the gate is no longer one of them.
  const present = useMemo(() => draft ?? stored ?? [], [draft, stored])
  const presentNames = useMemo(() => present.map((gate) => gate.name), [present])
  useEffect(() => {
    if (armed !== null && !presentNames.includes(armed)) setArmed(null)
  }, [armed, presentNames])

  /** The list an edit starts from: the open draft, or a copy of the stored one. */
  const working = (): GateDraft[] =>
    draft !== null
      ? draft
      : (stored ?? []).map((gate) => ({ ...gate, commands: gate.commands.map((a) => [...a]) }))

  const touched = () => {
    setWrote(false)
    setRemovalRefused(false)
    setWithdrawn(false)
    write.reset()
  }

  /** Replace the draft, staging it as ONE edit at the section, or withdrawing it. */
  const stageList = (next: GateDraft[]) => {
    touched()
    setDraft(next)
    setDraftBase(storedKey)
    if (stored !== null && listSignature(next) === storedKey) {
      // Writing back exactly what is stored is not a change, and every write is
      // recorded: staging it would put a line in the durable write record for an edit
      // nobody made.
      edits.unstage([QUALITY_GATES])
      return
    }
    edits.stage([QUALITY_GATES], next.map(gateValue))
  }

  const setPosition = (index: number, position: string) => {
    stageList(working().map((gate, at) => (at === index ? { ...gate, position } : gate)))
  }
  const setSeverity = (index: number, severity: string) => {
    stageList(working().map((gate, at) => (at === index ? { ...gate, severity } : gate)))
  }
  const setCommands = (index: number, name: string, typed: string) => {
    setText((current) => ({ ...current, [name]: typed }))
    const commands = parseCommands(typed)
    // An empty block would compose a gate with no command, which the write door
    // refuses. The typed text is kept so the operator can finish the line, and the
    // draft keeps the last command list that was a list.
    if (commands.length === 0) return
    stageList(working().map((gate, at) => (at === index ? { ...gate, commands } : gate)))
  }

  const confirmRemoval = () => {
    if (armed === null) return
    touched()
    // The typed name has to match, and a mismatch is ACKNOWLEDGED rather than
    // ignored: the confirmation is on screen before the click, so without the
    // statement a refused confirm would look like a control that does nothing.
    if (typedName.trim() !== armed) {
      setRemovalRefused(true)
      return
    }
    stageList(working().filter((gate) => gate.name !== armed))
    setArmed(null)
    setTypedName('')
  }

  /** Add a copy of *preset*, under the name and choices the add block holds. */
  const addPreset = (preset: GatePreset) => {
    touched()
    const name = addName.trim() === '' ? preset.name : addName.trim()
    const current = working()
    // Refused against the NAME field with the entered gate retained: the document
    // cannot express two gates of one name, and clearing the block would make the
    // operator re-choose a position, a severity and a command list to correct a name.
    if (current.some((gate) => gate.name === name)) {
      setNameRefused(name)
      return
    }
    setNameRefused('')
    const commands =
      addText.trim() === '' ? preset.commands.map((argv) => [...argv]) : parseCommands(addText)
    if (commands.length === 0) return
    stageList([
      ...current,
      {
        name,
        position: addPosition || preset.position,
        severity: addSeverity || preset.severity,
        commands,
      },
    ])
    setAddName('')
    setAddText('')
    setAddPosition('')
    setAddSeverity('')
  }

  const discard = () => {
    edits.clear()
    setDraft(null)
    setText({})
    setNameRefused('')
    setReviewing(false)
    setWrote(false)
    write.reset()
  }

  // What the draft would change, gate by gate, derived from the draft AGAINST the
  // stored list rather than accumulated as the operator works — so a change made and
  // then undone earns no sentence. Keyed by NAME because no rename is offered, which
  // makes the name the stable identity across the two lists.
  const changes: ReviewedChange[] = []
  if (draft !== null && stored !== null) {
    const before = new Map(stored.map((gate) => [gate.name, gate]))
    const after = new Set(draft.map((gate) => gate.name))
    for (const [index, gate] of draft.entries()) {
      const was = before.get(gate.name)
      const path = `${QUALITY_GATES}[${index}]`
      if (!was) {
        changes.push({
          path,
          sentence: i18nT('apps.specEngine.gateForm.edit_adds_the_gate', {
            gate: gate.name,
            position: positionLabel(gate.position),
            severity: severityLabel(gate.severity),
            commands: commandsText(gate.commands),
            path,
          }),
        })
        continue
      }
      if (was.position !== gate.position) {
        changes.push({
          path: `${path}.position`,
          sentence: i18nT('apps.specEngine.gateForm.edit_changes_the_position', {
            gate: gate.name,
            oldValue: positionLabel(was.position),
            newValue: positionLabel(gate.position),
            effect: positionEffect(gate.position),
          }),
        })
      }
      if (was.severity !== gate.severity) {
        changes.push({
          path: `${path}.severity`,
          sentence: i18nT('apps.specEngine.gateForm.edit_changes_the_severity', {
            gate: gate.name,
            oldValue: severityLabel(was.severity),
            newValue: severityLabel(gate.severity),
            effect: severityEffect(gate.severity),
          }),
        })
      }
      if (commandsText(was.commands) !== commandsText(gate.commands)) {
        changes.push({
          path: `${path}.commands`,
          sentence: i18nT('apps.specEngine.gateForm.edit_changes_the_commands', {
            gate: gate.name,
            oldValue: commandsText(was.commands),
            newValue: commandsText(gate.commands),
          }),
        })
      }
    }
    for (const gate of stored) {
      if (after.has(gate.name)) continue
      changes.push({
        // Not an index path: the entry is gone, so any index would name a gate that
        // is still there. The name is what the removal is about.
        path: `${QUALITY_GATES}:${gate.name}`,
        sentence: i18nT('apps.specEngine.gateForm.edit_removes_the_gate', { gate: gate.name }),
      })
    }
  }
  const removed = changes.filter((change) =>
    change.path.startsWith(`${QUALITY_GATES}:`),
  )
  // The staged edits the form can account for, and the patch built from exactly those
  // — one list for both, so the badge, the count on screen and the write cannot
  // disagree. A whole-list write is ONE unwritten change however many gates it
  // touches, because one path is written.
  const reviewed = changes.length > 0 ? edits.edits : []
  const patch = buildFormPatch(reviewed)
  // The card's declared consequences. Neither is legible in the JSON — a shorter array
  // does not say that a check stopped running, and an argv does not say that it is
  // executed on this machine — so each is declared by KIND and the card owns the
  // statement, which is what keeps two forms from wording one grant two ways.
  const authorises: Consequence[] = removed.map((change) => ({
    kind: 'gate_removed',
    path: change.path,
  }))
  if (reviewed.length > 0) authorises.push({ kind: 'commands_run', path: QUALITY_GATES })

  const heading = <h3>{i18nT('apps.specEngine.gateForm.quality_gates')}</h3>

  if (workflow.isError) {
    return (
      <div className="se-blk">
        {/* What it HOLDS, not what it can review: with no read the form cannot say
            what a staged change means, and a badge dropping to zero here would
            report unwritten work as gone. */}
        <PendingCount count={edits.edits.length} onCount={onPendingCount} />
        {heading}
        <Refused
          title={i18nT('apps.specEngine.gateForm.could_not_read_the_quality_gates')}
          error={workflow.error}
        />
      </div>
    )
  }
  if (!workflow.isFetched || !shown) {
    return (
      <div className="se-blk">
        <PendingCount count={edits.edits.length} onCount={onPendingCount} />
        {heading}
        {/* Distinct from an empty list on purpose: "no gate is configured" is a fact
            about the document, and "not read yet" is a fact about this request. */}
        <p className="se-note">{i18nT('apps.specEngine.gateForm.reading_the_quality_gates')}</p>
      </div>
    )
  }

  const list = shown.gates
  const expressible = stored !== null && list !== null && readsAgree(stored, list)
  const editable = expressible && positions.length > 0 && severities.length > 0
  const rows: ReadonlyArray<GateDraft | QualityGate> = expressible ? present : (list ?? [])
  // How many gates the DOCUMENT holds, which is what "no gate is configured" is a
  // claim about. Never `rows.length`: a draft that removes every gate has written
  // nothing, so the stored gates are still in force and still running, and saying
  // no gate runs would be false for exactly as long as the draft is unconfirmed.
  const configured = stored !== null ? stored.length : (list?.length ?? 0)

  /**
   * Where a row's declaration lives, or the section a new one would land in.
   *
   * Keyed by the gate's position in the STORED list rather than by its position in
   * the rows: a staged removal shifts every later row, and the route's answers
   * describe the document, so an index-for-index read would show a surviving gate
   * the declaring path of the gate that used to sit where it now sits. `readsAgree`
   * has already established that the two readings line up index for index, and no
   * rename is offered, so the name is a stable identity between them. A gate the
   * draft ADDS is declared nowhere yet and falls back to the section.
   */
  const declaringPath = (gate: GateDraft | QualityGate, index: number): string => {
    const at =
      expressible && stored !== null ? stored.findIndex((entry) => entry.name === gate.name) : index
    return (at >= 0 ? list?.[at]?.declared_at : '') || dotted([QUALITY_GATES])
  }

  /**
   * Whether a failure of this gate stops the run, as the ENGINE reads its severity.
   *
   * The payload's own `blocking`, so a severity this pane has no words for still
   * says what it does to a run — the table below it is prose and would fall silent.
   * `null` when the payload cannot answer for the severity on screen: a draft that
   * changed the severity is asking about a reading the engine has not made, and the
   * stored flag describes the severity being replaced rather than the new one.
   */
  const blockingFact = (gate: GateDraft | QualityGate, index: number): boolean | null => {
    const at =
      expressible && stored !== null ? stored.findIndex((entry) => entry.name === gate.name) : index
    const row = at >= 0 ? list?.[at] : undefined
    if (!row || row.severity !== gate.severity) return null
    return row.blocking
  }

  return (
    <div className="se-blk" data-gates-unreadable={shown.gates_unreadable}>
      {/* The same number the "unwritten gate changes" line below states, read from the
          same list, so the stage badge cannot claim a count this form does not show. */}
      <PendingCount count={reviewed.length} onCount={onPendingCount} />
      {heading}
      {shown.gates_scope_is_app && (
        <p className="se-note">{i18nT('apps.specEngine.gateForm.gates_apply_to_every_project')}</p>
      )}
      {shown.gates_unreadable ? (
        /* NOT the empty state, and the two are never collapsed: the engine refuses
           delivery outright on a list it cannot parse, so this says every check is off
           until the document is repaired rather than saying none is configured. No
           control is offered either — a form write over a list this side cannot read
           would be a write composed from a reading that does not exist. */
        <div className="se-arm" data-gates-state="unreadable">
          <p>
            <AlertTriangle className="lucide-inline" aria-hidden="true" />
            {i18nT('apps.specEngine.gateForm.the_stored_gate_list_could_not_be_read')}
          </p>
          <p>
            <AlertTriangle className="lucide-inline" aria-hidden="true" />
            {i18nT('apps.specEngine.gateForm.an_unreadable_list_refuses_delivery')}
          </p>
          {shown.gate_errors.length > 0 && (
            <ul className="se-advisories">
              {shown.gate_errors.map((error) => (
                <li key={`${error.path}:${error.message}`}>
                  {/* The path is an engine identifier an operator greps the document
                      for, not copy. */}
                  <span className="se-fkind">{error.path}</span>
                  <span className="se-adv-text">{error.message}</span>
                </li>
              ))}
            </ul>
          )}
          <p className="se-note">
            {i18nT('apps.specEngine.gateForm.repair_the_list_in_the_document')}
          </p>
        </div>
      ) : configured === 0 && rows.length === 0 ? (
        <div data-gates-state="empty">
          <p className="se-note">{i18nT('apps.specEngine.gateForm.no_gate_is_configured')}</p>
          {/* The other half an operator needs, and the reason the empty state is not
              simply "nothing checks this change": the engine floor is not a gate and
              cannot be configured off at all. */}
          <p className="se-note">
            {i18nT('apps.specEngine.gateForm.engine_floor_validation_still_applies')}
          </p>
        </div>
      ) : (
        <>
          {rows.length === 0 && (
            /* Gates ARE stored and the draft would leave none. Not the empty state:
               nothing is written yet, so what runs today is still the stored list. */
            <p className="se-note" role="status" data-gates-state="drafted-empty">
              {i18nT('apps.specEngine.gateForm.the_draft_leaves_no_gate')}
            </p>
          )}
          {!expressible && (
            /* The honest state, and no controls: a change to one gate rewrites the
               whole list, so one entry this form cannot represent would be reshaped by
               an edit to a different one. */
            <div className="se-arm" data-not-expressible="true">
              <p>
                <AlertTriangle className="lucide-inline" aria-hidden="true" />
                {i18nT('apps.specEngine.gateForm.the_form_cannot_express_the_gate_list')}
              </p>
              <p className="se-note">
                {i18nT('apps.specEngine.gateForm.a_gate_change_writes_the_whole_list')}
              </p>
              <p className="se-note">
                {i18nT('apps.specEngine.gateForm.repair_the_list_in_the_document')}
              </p>
            </div>
          )}
          {expressible && !editable && (
            <p className="se-note" role="status">
              {i18nT('apps.specEngine.gateForm.the_gate_vocabularies_were_not_read')}
            </p>
          )}
          <div className="se-settings">
            {rows.map((gate, index) => (
              <div className="se-setting" key={`${gate.name}:${index}`} data-gate={gate.name}>
                <span className="se-setting-name">
                  {gate.name}
                  {/* The declaring path is the route's, which is the only reading that
                      has one: a draft entry nobody has written yet is declared
                      nowhere, so it falls back to the section it will land in. */}
                  <span className="se-kv-path">{declaringPath(gate, index)}</span>
                </span>
                <p className="se-note">
                  {i18nT('apps.specEngine.gateForm.the_position')}
                  {SEP}
                  <span className="se-m">{positionLabel(gate.position)}</span>
                </p>
                <p className="se-note">{positionEffect(gate.position) || NONE}</p>
                {editable && (
                  <VocabularyChoice
                    label={i18nT('apps.specEngine.gateForm.choose_a_position_for_gate', {
                      gate: gate.name,
                    })}
                    values={positions}
                    chosen={gate.position}
                    optionLabel={positionLabel}
                    detail={positionEffect}
                    onChoose={(value) => setPosition(index, value)}
                  />
                )}
                <p className="se-note">
                  {i18nT('apps.specEngine.gateForm.the_severity')}
                  {SEP}
                  <span className="se-m">{severityLabel(gate.severity)}</span>
                </p>
                <p className="se-note">{severityEffect(gate.severity) || NONE}</p>
                {blockingFact(gate, index) !== null && (
                  /* The engine's own reading of the severity, beside this pane's
                     prose for it: the prose is a table keyed by the severities this
                     pane knows, and a severity the engine adds later would earn no
                     sentence at all while the payload was already answering. */
                  <p className="se-note" data-blocking={String(blockingFact(gate, index))}>
                    {blockingFact(gate, index)
                      ? i18nT('apps.specEngine.gateForm.a_failure_stops_the_run')
                      : i18nT('apps.specEngine.gateForm.a_failure_does_not_stop_the_run')}
                  </p>
                )}
                {editable && (
                  <VocabularyChoice
                    label={i18nT('apps.specEngine.gateForm.choose_a_severity_for_gate', {
                      gate: gate.name,
                    })}
                    values={severities}
                    chosen={gate.severity}
                    optionLabel={severityLabel}
                    detail={severityEffect}
                    onChoose={(value) => setSeverity(index, value)}
                  />
                )}
                <p className="se-note">{i18nT('apps.specEngine.gateForm.the_commands')}</p>
                {editable ? (
                  <textarea
                    className="se-input"
                    aria-label={i18nT('apps.specEngine.gateForm.commands_for_gate', {
                      gate: gate.name,
                    })}
                    rows={Math.max(2, gate.commands.length)}
                    value={text[gate.name] ?? commandsText(gate.commands)}
                    onChange={(event) => setCommands(index, gate.name, event.target.value)}
                  />
                ) : (
                  /* The payload itself rather than a shell-looking rendering of it:
                     the engine runs argv with no shell, and a rendering that implied
                     one would imply quoting rules that do not exist. */
                  <pre className="se-json">{JSON.stringify(gate.commands, null, 2)}</pre>
                )}
                {editable && (
                  <div className="se-acts">
                    <button
                      type="button"
                      className="se-btn se-sm se-danger"
                      // The accessible name carries the target even though the visible
                      // label is one word: a column of identical Remove controls is how
                      // the wrong gate goes.
                      aria-label={i18nT('apps.specEngine.gateForm.remove_the_gate', {
                        gate: gate.name,
                      })}
                      onClick={() => {
                        touched()
                        setTypedName('')
                        setArmed(gate.name)
                      }}
                    >
                      {i18nT('apps.specEngine.configPanel.remove')}
                    </button>
                  </div>
                )}
              </div>
            ))}
          </div>
          {editable && (
            <p className="se-note">{i18nT('apps.specEngine.gateForm.one_command_per_line')}</p>
          )}
        </>
      )}
      {armed !== null && (
        /* In flow under the list, never a dialog: the confirmation for a destructive
           edit is a sibling block for the same reason the kill switch's is. The gate's
           own name, TYPED, because a column of identical Remove controls is how the
           wrong gate goes — and the name is the one thing that cannot be clicked by
           accident. */
        <div className="se-arm" data-gate-armed={armed}>
          <p>
            <AlertTriangle className="lucide-inline" aria-hidden="true" />
            {i18nT('apps.specEngine.gateForm.removing_stops_running_the_check', { gate: armed })}
          </p>
          <div className="se-setting">
            <label className="se-setting-name" htmlFor="se-gate-remove-name">
              {i18nT('apps.specEngine.gateForm.type_the_name_to_confirm', { gate: armed })}
            </label>
            <input
              id="se-gate-remove-name"
              type="text"
              className="se-input"
              value={typedName}
              onChange={(event) => setTypedName(event.target.value)}
            />
          </div>
          <div className="se-acts">
            <button type="button" className="se-btn se-danger" onClick={confirmRemoval}>
              {i18nT('apps.specEngine.gateForm.confirm_the_removal', { gate: armed })}
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
              {i18nT('apps.specEngine.gateForm.keep_the_gate')}
            </button>
          </div>
          {removalRefused && (
            <p className="se-note" role="status">
              <span>{i18nT('apps.specEngine.gateForm.the_removal_was_refused')}</span>
              {SEP}
              <span>
                {i18nT('apps.specEngine.gateForm.the_typed_name_does_not_match', { gate: armed })}
              </span>
            </p>
          )}
        </div>
      )}

      {editable && (
        <>
          <h3>{i18nT('apps.specEngine.gateForm.add_a_quality_gate')}</h3>
          <div className="se-setting">
            <label className="se-setting-name" htmlFor="se-gate-add-name">
              {i18nT('apps.specEngine.gateForm.name_for_the_new_gate')}
            </label>
            <input
              id="se-gate-add-name"
              type="text"
              className="se-input"
              value={addName}
              onChange={(event) => {
                touched()
                setNameRefused('')
                setAddName(event.target.value)
              }}
            />
            {nameRefused !== '' && (
              /* Rendered against the field it concerns, with the entered gate still
                 here to be added under another name. */
              <p className="se-note" role="status">
                {i18nT('apps.specEngine.gateForm.the_name_is_already_a_gate', {
                  gate: nameRefused,
                })}
              </p>
            )}
          </div>
          <div className="se-setting">
            <span className="se-setting-name">
              {i18nT('apps.specEngine.gateForm.the_position')}
            </span>
            <VocabularyChoice
              label={i18nT('apps.specEngine.gateForm.choose_a_position_for_the_new_gate')}
              values={positions}
              chosen={addPosition}
              optionLabel={positionLabel}
              detail={positionEffect}
              onChoose={(value) => {
                touched()
                setAddPosition(value)
              }}
            />
            {/* Every position's consequence in flow, not only the chosen one's: the
                decision is between them, and a statement that appears after the click
                is a statement read too late. */}
            {positions.map((position) => (
              <p className="se-note" key={`position-effect:${position}`}>
                <span className="se-m">{positionLabel(position)}</span>
                {SEP}
                {positionEffect(position)}
              </p>
            ))}
          </div>
          <div className="se-setting">
            <span className="se-setting-name">
              {i18nT('apps.specEngine.gateForm.the_severity')}
            </span>
            <VocabularyChoice
              label={i18nT('apps.specEngine.gateForm.choose_a_severity_for_the_new_gate')}
              values={severities}
              chosen={addSeverity}
              optionLabel={severityLabel}
              detail={severityEffect}
              onChoose={(value) => {
                touched()
                setAddSeverity(value)
              }}
            />
            {severities.map((severity) => (
              <p className="se-note" key={`severity-effect:${severity}`}>
                <span className="se-m">{severityLabel(severity)}</span>
                {SEP}
                {severityEffect(severity)}
              </p>
            ))}
          </div>
          <div className="se-setting">
            <label className="se-setting-name" htmlFor="se-gate-add-commands">
              {i18nT('apps.specEngine.gateForm.the_commands')}
            </label>
            <textarea
              id="se-gate-add-commands"
              className="se-input"
              rows={2}
              value={addText}
              onChange={(event) => {
                touched()
                setAddText(event.target.value)
              }}
            />
            <p className="se-note">{i18nT('apps.specEngine.gateForm.one_command_per_line')}</p>
          </div>
          {presets.length === 0 ? (
            <p className="se-note">
              {i18nT('apps.specEngine.gateForm.the_engine_bundles_no_gate_definition')}
            </p>
          ) : (
            <div
              role="group"
              aria-label={i18nT('apps.specEngine.gateForm.choose_a_definition_to_copy')}
            >
              {presets.map((preset) => (
                <div className="se-offer" key={preset.name} data-gate-preset={preset.name}>
                  <span className="se-m">{preset.name}</span>
                  {/* What it would run and how it would be positioned, from the
                      definition's own bytes: something offered as a starting point has
                      to state what starting from it means. */}
                  <span className="se-note">
                    {i18nT('apps.specEngine.gateForm.definition_runs_commands', {
                      position: positionLabel(preset.position),
                      severity: severityLabel(preset.severity),
                      commands: commandsText(preset.commands),
                    })}
                  </span>
                  <button
                    type="button"
                    className="se-btn se-sm"
                    onClick={() => addPreset(preset)}
                  >
                    {i18nT('apps.specEngine.gateForm.copy_the_definition', { gate: preset.name })}
                  </button>
                </div>
              ))}
            </div>
          )}
          <p className="se-note">
            {i18nT('apps.specEngine.gateForm.unset_fields_take_the_definitions')}
          </p>
        </>
      )}
      {withdrawn && (
        /* The withdrawal is an event this form caused, so it is stated: a draft that
           silently stopped being counted reads as an edit never made. `role="status"`
           so it reaches a reader who is not looking here when a refetch lands. */
        <p className="se-note" role="status">
          {i18nT('apps.specEngine.gateForm.the_draft_was_withdrawn')}
        </p>
      )}
      <div className="se-acts" style={{ marginTop: 9 }}>
        <button
          type="button"
          className="se-btn"
          disabled={reviewed.length === 0}
          onClick={() => setReviewing(true)}
        >
          {i18nT('apps.specEngine.gateForm.review_the_exact_change')}
        </button>
        {reviewed.length > 0 && (
          <span className="se-lbl">
            {i18nT('apps.specEngine.gateForm.unwritten_gate_changes')}
            {SEP}
            <span className="se-m">{fmtNumber(reviewed.length)}</span>
          </span>
        )}
      </div>
      {reviewing && reviewed.length > 0 && (
        <FormReview
          changes={changes}
          patch={patch}
          labels={{
            heading: i18nT('apps.specEngine.gateForm.the_change_that_would_be_written'),
            confirm: i18nT('apps.specEngine.gateForm.write_the_change'),
            writing: i18nT('apps.specEngine.configPanel.saving'),
            discard: i18nT('apps.specEngine.gateForm.discard_the_pending_changes'),
            exactly: i18nT('apps.specEngine.gateForm.a_confirm_writes_exactly_this_patch'),
            refusalTitle: i18nT('apps.specEngine.gateForm.could_not_write_the_gate_change'),
            retained: i18nT('apps.specEngine.gateForm.nothing_was_written_so_the_gates_are_stored'),
          }}
          authorises={authorises}
          consequences={
            /* The one fact the sentences above cannot carry: the patch replaces the
               list, so a gate nobody mentioned is still re-declared by this write. */
            <p className="se-note">
              {i18nT('apps.specEngine.gateForm.the_whole_list_is_replaced')}
            </p>
          }
          writing={write.isPending}
          error={write.isError ? write.error : null}
          onConfirm={(sending) => write.mutate(sending)}
          onDiscard={discard}
        />
      )}
      {wrote && (
        <p className="se-note" role="status">
          {i18nT('apps.specEngine.gateForm.wrote_the_change_and_re_read_the_gates')}
        </p>
      )}
    </div>
  )
}
