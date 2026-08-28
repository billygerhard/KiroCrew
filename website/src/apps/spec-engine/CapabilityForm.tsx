/**
 * The capability binding form: which implementation serves each unit of work the
 * engine can delegate.
 *
 * Seven capabilities can be served either by the engine itself or by something
 * outside it — an MCP server, or a program handed structured input — so an internal
 * deployment can bind its own analyzer, its own ticket poller or its own validation
 * tools purely by configuration, with no reference to any of them in the shipped
 * product. That seam existed in the engine long before it had a surface; this is the
 * surface.
 *
 * ## What this form reads, and why it is two reads
 *
 * `GET /config/capabilities` answers what is IN FORCE: the provider each capability
 * resolves to, whether an operator declared the binding and where, the deadline one
 * call gets, and whether the program can be found on this host. `GET /config`'s
 * document answers what is STORED, which is what an edit replaces — and it has to,
 * because the bindings payload deliberately withholds two things a form would
 * otherwise need to invent: `program` is `argv[0]` only, and an environment VALUE
 * never travels at all.
 *
 * This form keeps that withholding rather than working around it. It reads the
 * stored command so a rebind starts from what is there, it reads the environment
 * NAMES so a rebind can remove the ones it drops, and it renders no environment
 * value it did not receive from the operator's own typing in this session.
 *
 * ## The one reading most likely to be got wrong
 *
 * `provider.nature` is the row's only cost-shaped field, and the engine hardcodes
 * `model_backed` for EVERY external binding — not because it knows the program
 * reasons, but because it cannot know. So mapping `nature` onto a credits badge
 * asserts a spend the engine never claimed, and would mislabel a deterministic
 * external linter. {@link costSignal} is the single place that decision is made and
 * it branches on `kind` FIRST, which is what makes `nature` unreachable for
 * anything that is not a builtin.
 *
 * ## What it states rather than claims
 *
 * That an external program's output is treated as data and never as instructions,
 * and that an unusable provider falls back to the capability's builtin rather than
 * stopping a run. Both are statements about what the engine DOES. Neither is a
 * status: a degradation is attached to one invocation's result and is never
 * persisted, so no configuration surface can say that a provider is degrading right
 * now, and this one does not.
 *
 * ## What it refuses to offer
 *
 * A control that would bind an engine-floor capability. Naming one of those in the
 * `capabilities` section is a refusal rather than an ignored key, because a
 * delegated phase gate or claim ledger would move the guarantees the engine exists
 * to make outside the engine. They are NAMED here, so an operator learns they exist
 * and that binding one is refused, and there is no chooser next to them.
 *
 * And a project scope. The engine reads bindings from one app-wide section with no
 * per-project layer, so a per-project control would imply a resolution that does not
 * exist.
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { fmtNumber } from '../../i18n/format'
import { i18nT } from '../../i18n/t'

import { QK, specEngineApi, type CapabilityBinding, type ProviderIdentity } from './api'
import { FormReview, PendingCount, Refused } from './ConfigPanel'
import { ConformancePanel } from './ConformancePanel'
import {
  DELETE,
  buildFormPatch,
  dotted,
  isObject,
  type Document,
  type StagedEdit,
} from './configDocument'
import { surfaceKey } from './stages'
import { useStagedEdits } from './useStagedEdits'

/** Separator between two identifiers on one line. Punctuation, not copy. */
const SEP = ' \u00b7 '

/** Stands in for a field the engine has no value for. Punctuation, not copy. */
const NONE = '\u2014'

/** The document section the bindings live in. One app-wide map, keyed by capability. */
const CAPABILITIES = 'capabilities'

/**
 * The transport that runs inside the engine, and the only one that takes no fields.
 *
 * Named on this side rather than derived from the projection, because the
 * projection carries transport NAMES and not which fields each accepts. That is
 * the one fact about the vocabulary this module holds, and it is held as a single
 * negative: anything that is NOT this transport reaches something outside the
 * engine and therefore takes a command, an environment and a deadline. A transport
 * the engine adds later is treated as external, which is the safe direction — it
 * gets the fields and the consequence statements rather than being silently
 * rendered as a free, in-process, instruction-safe binding.
 */
const BUILTIN = 'builtin'

/** The binding fields, as the write door names them. */
const FIELD_TRANSPORT = 'transport'
const FIELD_COMMAND = 'command'
const FIELD_ENV = 'env'
const FIELD_TIMEOUT = 'timeout_s'

/** What this pane can say about whether serving a capability spends credits. */
export type CostSignal = 'credits' | 'no_credits' | 'unknown'

/**
 * What can honestly be said about *provider*'s cost.
 *
 * `kind` is read FIRST and `nature` is not read at all unless the provider is a
 * builtin. That ordering is the whole content of this function and it is not a
 * style choice: `ProviderIdentity.nature` is hardcoded `model_backed` for every
 * external binding, so a renderer reading `nature` first would report "spends
 * credits" for a deterministic external linter the engine never claimed anything
 * about. The engine cannot tell whether an external program reasons, so the honest
 * answer for one is `unknown` — asserting either direction is a claim, and the
 * free-sounding one is the more damaging of the two.
 *
 * A `kind` this pane does not know falls to `unknown` for the same reason: the
 * only kind whose `nature` means anything is the one the engine computes it for.
 */
export function costSignal(provider: Pick<ProviderIdentity, 'kind' | 'nature'>): CostSignal {
  if (provider.kind !== 'builtin') return 'unknown'
  return provider.nature === 'model_backed' ? 'credits' : 'no_credits'
}

/** The catalog key stating what each cost signal means. Whole literals, per pane rule. */
const COST_KEY: Record<CostSignal, string> = {
  credits: 'apps.specEngine.capabilityForm.the_builtin_asks_a_model_so_it_spends_credits',
  no_credits: 'apps.specEngine.capabilityForm.the_builtin_asks_no_model_so_it_spends_nothing',
  unknown: 'apps.specEngine.capabilityForm.whether_an_external_provider_spends_credits_is_unknown',
}

/**
 * One argument per line, which is the only split that invents no quoting rule.
 *
 * A shell-style split would have to decide what a quote and a backslash mean, and
 * this text becomes `argv` the engine executes with NO shell — so a rule invented
 * here would be a rule that applies nowhere else. Blank lines are dropped rather
 * than becoming empty arguments, which the write door refuses anyway.
 */
export function argvFromText(text: string): string[] {
  return text
    .split('\n')
    .map((line) => line.trim())
    .filter((line) => line !== '')
}

/** *argv* as one argument per line, for filling the field from what is stored. */
export function textFromArgv(argv: readonly unknown[]): string {
  return argv.map((argument) => String(argument)).join('\n')
}

/**
 * *text* as a positive integer, or `null` when it is not one.
 *
 * `null` rather than a coerced number, because the write door wants a positive
 * integer and `Number('1.5')` and `Number('x')` are both values a staged edit must
 * never carry: `NaN` would serialize as JSON `null`, which is the store's spelling
 * for DELETE, so a typo in a timeout field would silently remove the key instead
 * of being refused.
 */
export function positiveInt(text: string): number | null {
  if (!/^\d+$/.test(text.trim())) return null
  const value = Number(text.trim())
  return Number.isSafeInteger(value) && value >= 1 ? value : null
}

/** One environment entry as the operator is entering it. */
export interface EnvEntry {
  name: string
  value: string
}

/** A capability's binding as the operator is composing it, before it is staged. */
export interface BindingDraft {
  transport: string
  /** The command, one argument per line. */
  command: string
  env: readonly EnvEntry[]
  /** The per-binding deadline, or `''` to inherit the app-wide capability timeout. */
  timeout: string
}

/**
 * What the document stores for one capability, as this form needs to read it.
 *
 * The command and the timeout so a rebind starts from what is there rather than
 * from an empty field. The environment NAMES and not their values: the values are
 * in the document, and this form does not put one on screen — a name is what a
 * rebind needs in order to remove an entry it drops.
 */
export interface StoredBinding {
  present: boolean
  transport: string
  command: string
  envNames: readonly string[]
  timeout: string
}

/** The stored binding for *capability*, read out of the persisted document. */
export function storedBinding(document: Document, capability: string): StoredBinding {
  const section = document[CAPABILITIES]
  const entry = isObject(section) ? section[capability] : undefined
  if (!isObject(entry)) {
    return { present: false, transport: BUILTIN, command: '', envNames: [], timeout: '' }
  }
  const command = entry[FIELD_COMMAND]
  const env = entry[FIELD_ENV]
  const timeout = entry[FIELD_TIMEOUT]
  return {
    present: true,
    transport: typeof entry[FIELD_TRANSPORT] === 'string' ? entry[FIELD_TRANSPORT] : '',
    command: Array.isArray(command) ? textFromArgv(command) : '',
    envNames: isObject(env) ? Object.keys(env) : [],
    timeout: typeof timeout === 'number' ? String(timeout) : '',
  }
}

/** The path segments one of *capability*'s binding fields lives at. */
function segmentsAt(capability: string, ...leaf: readonly string[]): string[] {
  return [CAPABILITIES, capability, ...leaf]
}

/**
 * The staged edits that write *draft* as *capability*'s binding.
 *
 * Leaves rather than one object at `capabilities.<capability>`, and that is forced
 * by the store's merge rather than chosen: nested objects merge key by key, so an
 * object write would leave every field the draft does not mention exactly where it
 * was. Two consequences follow, and both are the reason this function exists as a
 * tested pure function instead of inline at a call site.
 *
 * **The builtin is a deletion, never `{transport: 'builtin'}`.** A merged object
 * would keep a stored command beside the new transport, and the write door refuses
 * a document whose builtin binding carries one — so an operator returning a
 * capability to its builtin would meet a refusal naming a field they thought they
 * had removed. Removing the whole entry is also what the engine's own remediation
 * string tells an operator to do, and it writes no command, environment or timeout
 * by construction rather than by care taken here.
 *
 * **An environment name the draft drops is removed explicitly.** *storedEnvNames*
 * is what makes that possible: the merge cannot express "replace this map", so each
 * stored name the draft no longer carries earns its own deletion. When the draft
 * carries none at all the whole `env` key goes instead of a map full of deletions.
 *
 * Every returned path lies under `capabilities.<capability>` and no two overlap, so
 * the composed patch addresses exactly the binding and nothing else in the document.
 */
export function bindingEdits(
  capability: string,
  draft: BindingDraft,
  storedEnvNames: readonly string[],
  storedTimeout: string,
): StagedEdit[] {
  if (draft.transport === BUILTIN) {
    return [{ segments: segmentsAt(capability), value: DELETE }]
  }
  const edits: StagedEdit[] = [
    { segments: segmentsAt(capability, FIELD_TRANSPORT), value: draft.transport },
    { segments: segmentsAt(capability, FIELD_COMMAND), value: argvFromText(draft.command) },
  ]
  const entries = draft.env
    .map((entry) => ({ name: entry.name.trim(), value: entry.value }))
    .filter((entry) => entry.name !== '')
  if (entries.length === 0) {
    if (storedEnvNames.length > 0) {
      edits.push({ segments: segmentsAt(capability, FIELD_ENV), value: DELETE })
    }
  } else {
    for (const entry of entries) {
      edits.push({ segments: segmentsAt(capability, FIELD_ENV, entry.name), value: entry.value })
    }
    const kept = new Set(entries.map((entry) => entry.name))
    for (const name of storedEnvNames) {
      if (!kept.has(name)) {
        edits.push({ segments: segmentsAt(capability, FIELD_ENV, name), value: DELETE })
      }
    }
  }
  const timeout = positiveInt(draft.timeout)
  if (timeout === null) {
    if (storedTimeout !== '') {
      edits.push({ segments: segmentsAt(capability, FIELD_TIMEOUT), value: DELETE })
    }
  } else {
    edits.push({ segments: segmentsAt(capability, FIELD_TIMEOUT), value: timeout })
  }
  return edits
}

/**
 * Whether *draft* is one the write door would accept, so the stage control can say.
 *
 * Only the two rules this form can check without guessing: a transport that reaches
 * outside the engine needs at least one argument, and a declared deadline must be a
 * positive integer. Everything else is the door's to enforce and is reported back
 * against the field it names — this is not a second validator, it is the pair of
 * refusals that would otherwise be composed into a patch and sent.
 */
export function draftProblem(draft: BindingDraft): 'command' | 'timeout' | '' {
  if (draft.transport === BUILTIN) return ''
  if (argvFromText(draft.command).length === 0) return 'command'
  if (draft.timeout.trim() !== '' && positiveInt(draft.timeout) === null) return 'timeout'
  return ''
}

/**
 * The write door's reason for *path*, out of a refusal that names several.
 *
 * The engine reports one `ConfigError` per problem, each rendered `path: message`
 * and joined with `'; '`, so a refusal over a rebind can carry a reason for the
 * transport and another for the command. Matching by path is what lets each land
 * beside the field it concerns, with the operator's entry still in it.
 *
 * A fragment whose path this does not recognise is deliberately NOT claimed: it
 * stays unattributed and is read by the review card's own refusal block instead.
 * That is the safe direction — an unattributed reason costs one well-placed line,
 * while a misattributed one points an operator at a field that is fine.
 *
 * An argv element's path is `<path>[0]`, so a prefix followed by `[` matches too:
 * the refusal is about the command, and there is no per-argument field to put it
 * beside.
 */
export function fieldRefusal(error: unknown, path: string): string {
  const text = error instanceof Error ? error.message : ''
  if (!text) return ''
  for (const fragment of text.split('; ')) {
    const at = fragment.indexOf(': ')
    if (at < 0) continue
    const named = fragment.slice(0, at)
    if (named === path || named.startsWith(`${path}[`)) return fragment.slice(at + 2)
  }
  return ''
}

/**
 * The staged edits for one capability, and the sentence they earn.
 *
 * Grouped by capability because a rebind is one act over several leaves: the review
 * reads out "this capability is now served by that program", not four paths. The
 * sentence is derived from the GROUP's own edits rather than from the draft, so the
 * account the operator reads and the patch that is sent are two readings of one
 * list.
 */
interface ReviewedBinding {
  capability: string
  edits: readonly StagedEdit[]
  /** Whether this group returns the capability to its builtin. */
  reverts: boolean
  transport: string
  program: string
}

/** *edits* grouped per capability, keeping only capabilities in *rendered*. */
function reviewBindings(  edits: readonly StagedEdit[],
  rendered: readonly string[],
): ReviewedBinding[] {
  const groups: ReviewedBinding[] = []
  const at = new Map<string, ReviewedBinding>()
  for (const edit of edits) {
    if (edit.segments[0] !== CAPABILITIES) continue
    const capability = edit.segments[1]
    if (capability === undefined || !rendered.includes(capability)) continue
    let group = at.get(capability)
    if (!group) {
      group = { capability, edits: [], reverts: false, transport: '', program: '' }
      at.set(capability, group)
      groups.push(group)
    }
    group.edits = [...group.edits, edit]
    if (edit.segments.length === 2 && edit.value === DELETE) group.reverts = true
    if (edit.segments[2] === FIELD_TRANSPORT && typeof edit.value === 'string') {
      group.transport = edit.value
    }
    if (edit.segments[2] === FIELD_COMMAND && Array.isArray(edit.value)) {
      group.program = String(edit.value[0] ?? '')
    }
  }
  return groups
}

/**
 * How many capabilities the staged edits touch, for a badge with no read behind it.
 *
 * The same QUANTITY {@link reviewBindings} reports — capabilities, one act each —
 * rather than the number of leaves those acts write. One rebind stages a transport,
 * a command, an environment entry and a timeout; counting leaves would make the
 * stage badge jump from one to four the moment a read failed and this became the
 * reachable count, with nothing staged in between.
 *
 * Computed without the reads, because the branches that use it are the ones where a
 * read is refused or has not landed: what the form HOLDS is still true then, and a
 * badge dropping to zero there would report unwritten work as gone.
 */
function stagedCapabilityCount(edits: readonly StagedEdit[]): number {
  const seen = new Set<string>()
  for (const edit of edits) {
    if (edit.segments[0] !== CAPABILITIES) continue
    const capability = edit.segments[1]
    if (capability !== undefined) seen.add(capability)
  }
  return seen.size
}

/**
 * Which of the three reachability answers a row carries.
 *
 * Three and not two: `null` means NOT APPLICABLE — the binding is on its builtin,
 * which is this engine, so the engine's own check skips it entirely. Reporting that
 * as a broken provider would mark every unconfigured capability as failing, which is
 * every capability on a fresh install.
 */
function reachability(reachable: boolean | null): 'found' | 'missing' | 'not_applicable' {
  if (reachable === null) return 'not_applicable'
  return reachable ? 'found' : 'missing'
}

/** What each reachability answer reads as. Whole literals, per the pane's rule. */
const REACHABILITY_KEY: Record<ReturnType<typeof reachability>, string> = {
  found: 'apps.specEngine.capabilityForm.the_program_was_found_on_this_host',
  missing: 'apps.specEngine.capabilityForm.the_program_was_not_found_on_this_host',
  not_applicable: 'apps.specEngine.capabilityForm.reachability_does_not_apply_to_a_builtin',
}


/** One transport chooser: the engine's transports, and nothing else. */
function TransportChoice({
  capability,
  transports,
  chosen,
  onChoose,
}: {
  capability: string
  transports: readonly string[]
  chosen: string
  onChoose: (transport: string) => void
}) {
  return (
    <div
      className="se-acts"
      role="group"
      aria-label={i18nT('apps.specEngine.capabilityForm.how_this_capability_is_reached', {
        capability,
      })}
    >
      {transports.map((transport) => (
        <button
          key={transport}
          type="button"
          className="se-btn se-sm se-m"
          aria-pressed={transport === chosen}
          onClick={() => onChoose(transport)}
        >
          {transport}
        </button>
      ))}
    </div>
  )
}

/** The environment entries an external binding carries, as name and value pairs. */
function EnvFields({
  capability,
  entries,
  storedNames,
  onChange,
}: {
  capability: string
  entries: readonly EnvEntry[]
  storedNames: readonly string[]
  onChange: (entries: readonly EnvEntry[]) => void
}) {
  const replace = (index: number, entry: EnvEntry) =>
    onChange(entries.map((current, at) => (at === index ? entry : current)))
  return (
    <div className="se-blk">
      <span className="se-lbl">{i18nT('apps.specEngine.capabilityForm.environment_entries')}</span>
      {storedNames.length > 0 && (
        <p className="se-note">
          {i18nT('apps.specEngine.capabilityForm.environment_names_already_declared', {
            names: storedNames.join(', '),
          })}
        </p>
      )}
      {entries.map((entry, index) => (
        // The index is the key because a name is what the operator is TYPING: keying
        // on it would remount the field on every character and take the caret.
        <div className="se-pathrow" key={`env-${index}`}>
          <input
            className="se-input se-m"
            type="text"
            value={entry.name}
            aria-label={i18nT('apps.specEngine.capabilityForm.an_environment_name', { capability })}
            onChange={(event) => replace(index, { ...entry, name: event.target.value })}
          />
          <input
            className="se-input se-m"
            type="text"
            value={entry.value}
            aria-label={i18nT('apps.specEngine.capabilityForm.an_environment_value', { capability })}
            onChange={(event) => replace(index, { ...entry, value: event.target.value })}
          />
          <button
            type="button"
            className="se-btn se-sm"
            onClick={() => onChange(entries.filter((_, at) => at !== index))}
          >
            {i18nT('apps.specEngine.capabilityForm.remove_this_environment_entry')}
          </button>
        </div>
      ))}
      <div className="se-acts">
        <button
          type="button"
          className="se-btn se-sm"
          onClick={() => onChange([...entries, { name: '', value: '' }])}
        >
          {i18nT('apps.specEngine.capabilityForm.add_an_environment_entry')}
        </button>
      </div>
      <p className="se-note">
        {i18nT('apps.specEngine.capabilityForm.an_environment_value_is_written_and_never_read_back')}
      </p>
    </div>
  )
}

/**
 * One capability: what serves it now, and the controls that rebind it.
 *
 * The facts come first and unconditionally — a reader deciding whether to rebind
 * needs to know what is bound, who declared it, what it costs and whether it can
 * even be found before meeting a chooser. The fields for a command, an environment
 * and a deadline appear only for a transport that accepts them, so a capability on
 * its builtin shows a chooser and nothing else.
 */
function CapabilityRow({
  row,
  stored,
  transports,
  draft,
  staged,
  error,
  onDraft,
  onStage,
  onWithdraw,
}: {
  row: CapabilityBinding
  stored: StoredBinding
  transports: readonly string[]
  draft: BindingDraft
  staged: boolean
  error: unknown
  onDraft: (draft: BindingDraft) => void
  onStage: () => void
  onWithdraw: () => void
}) {
  const capability = row.capability
  const external = draft.transport !== BUILTIN
  const problem = draftProblem(draft)
  const entryPath = dotted(segmentsAt(capability))
  const cost = costSignal(row.provider)
  return (
    <div className="se-setting" data-capability={capability} data-staged={staged}>
      <span className="se-setting-name">{capability}</span>
      <dl className="se-kv">
        <dt>{i18nT('apps.specEngine.capabilityForm.the_transport_in_force')}</dt>
        <dd className="se-m">{row.transport}</dd>
        <dt>{i18nT('apps.specEngine.capabilityForm.the_provider_in_force')}</dt>
        <dd className="se-m">
          {row.provider.name}
          {row.provider.version ? `${SEP}${row.provider.version}` : ''}
        </dd>
        <dt>{i18nT('apps.specEngine.capabilityForm.where_the_binding_is_declared')}</dt>
        <dd className="se-m">{row.declared_at || NONE}</dd>
      </dl>
      <p className="se-note">
        {row.configured
          ? i18nT('apps.specEngine.capabilityForm.an_operator_declared_this_binding')
          : i18nT('apps.specEngine.capabilityForm.nothing_declares_this_so_the_builtin_serves_it')}
      </p>
      <p className="se-note">
        {i18nT('apps.specEngine.capabilityForm.one_call_may_take_this_long', {
          seconds: fmtNumber(row.timeout_s),
        })}
      </p>
      <p className="se-note" data-reachable={String(row.reachable)}>
        {/* Indexed at the call site rather than through a local, so the
            key-reference gate resolves every entry — the ORIGIN_KEY idiom. */}
        {i18nT(REACHABILITY_KEY[reachability(row.reachable)])}
      </p>
      {/* The engine's own remediation string, relayed. It names the "or unset it to
          use the builtin" escape, and a sentence composed here would be a second
          opinion about a check this pane did not run. */}
      {row.reachable === false && row.action && <p className="se-note se-m">{row.action}</p>}
      <p className="se-note" data-cost={cost}>
        {i18nT(COST_KEY[cost])}
      </p>
      {/* Rendered whatever the transport in force is, because it describes what the
          engine does with an external provider and the operator is deciding whether
          to bind one. Neither sentence is a status: a degradation is attached to one
          invocation and never persisted, so nothing here says a provider IS falling
          back right now. */}
      <p className="se-note">
        {i18nT('apps.specEngine.capabilityForm.output_is_data_and_never_instructions')}
      </p>
      <p className="se-note">
        {i18nT('apps.specEngine.capabilityForm.an_unusable_provider_falls_back_to_the_builtin')}
      </p>
      {/* Only where the binding IN FORCE reaches outside the engine. A builtin has
          nothing to check — the engine verifies its own builtins in its own suite —
          and this is the resolved transport rather than the draft's, because the
          suite runs against what is bound and not against what is being typed. */}
      {row.transport !== BUILTIN && <ConformancePanel capability={capability} />}
      <TransportChoice
        capability={capability}
        transports={transports}
        chosen={draft.transport}
        onChoose={(transport) => onDraft({ ...draft, transport })}
      />
      {fieldRefusal(error, dotted(segmentsAt(capability, FIELD_TRANSPORT))) && (
        <p className="se-note" role="alert" data-refusal={FIELD_TRANSPORT}>
          {fieldRefusal(error, dotted(segmentsAt(capability, FIELD_TRANSPORT)))}
        </p>
      )}
      {fieldRefusal(error, entryPath) && (
        <p className="se-note" role="alert" data-refusal={CAPABILITIES}>
          {fieldRefusal(error, entryPath)}
        </p>
      )}
      {!external ? (
        <p className="se-note">
          {i18nT('apps.specEngine.capabilityForm.the_builtin_takes_no_command_or_environment')}
        </p>
      ) : (
        <>
          <div className="se-idfield">
            <label htmlFor={`se-cap-cmd-${capability}`}>
              {i18nT('apps.specEngine.capabilityForm.the_command_to_run')}
            </label>
            <textarea
              id={`se-cap-cmd-${capability}`}
              className="se-input se-m"
              rows={3}
              value={draft.command}
              onChange={(event) => onDraft({ ...draft, command: event.target.value })}
            />
            <span className="se-note">
              {i18nT('apps.specEngine.capabilityForm.one_argument_per_line')}
            </span>
          </div>
          {problem === 'command' && (
            <p className="se-note" role="alert">
              {i18nT('apps.specEngine.capabilityForm.this_transport_needs_at_least_one_argument')}
            </p>
          )}
          {fieldRefusal(error, dotted(segmentsAt(capability, FIELD_COMMAND))) && (
            <p className="se-note" role="alert" data-refusal={FIELD_COMMAND}>
              {fieldRefusal(error, dotted(segmentsAt(capability, FIELD_COMMAND)))}
            </p>
          )}
          <EnvFields
            capability={capability}
            entries={draft.env}
            storedNames={stored.envNames}
            onChange={(env) => onDraft({ ...draft, env })}
          />
          {fieldRefusal(error, dotted(segmentsAt(capability, FIELD_ENV))) && (
            <p className="se-note" role="alert" data-refusal={FIELD_ENV}>
              {fieldRefusal(error, dotted(segmentsAt(capability, FIELD_ENV)))}
            </p>
          )}
          <div className="se-idfield">
            <label htmlFor={`se-cap-timeout-${capability}`}>
              {i18nT('apps.specEngine.capabilityForm.the_timeout_in_seconds')}
            </label>
            <input
              id={`se-cap-timeout-${capability}`}
              className="se-input se-m"
              type="text"
              inputMode="numeric"
              value={draft.timeout}
              onChange={(event) => onDraft({ ...draft, timeout: event.target.value })}
            />
            <span className="se-note">
              {i18nT('apps.specEngine.capabilityForm.blank_inherits_the_app_wide_timeout')}
            </span>
          </div>
          {problem === 'timeout' && (
            <p className="se-note" role="alert">
              {i18nT('apps.specEngine.capabilityForm.a_timeout_must_be_a_positive_whole_number')}
            </p>
          )}
          {fieldRefusal(error, dotted(segmentsAt(capability, FIELD_TIMEOUT))) && (
            <p className="se-note" role="alert" data-refusal={FIELD_TIMEOUT}>
              {fieldRefusal(error, dotted(segmentsAt(capability, FIELD_TIMEOUT)))}
            </p>
          )}
        </>
      )}
      <div className="se-acts">
        <button
          type="button"
          className="se-btn se-sm"
          // A return to the builtin is the REMOVAL of a stored entry, so with nothing
          // stored there is nothing to stage: the patch would be `{<capability>: null}`
          // over a key that does not exist, and the card's sentence would say a
          // command, environment entries and a timeout are removed when none were ever
          // declared. Refused with the reason beside it rather than silently inert.
          disabled={problem !== '' || (!external && !stored.present)}
          onClick={onStage}
        >
          {external
            ? i18nT('apps.specEngine.capabilityForm.stage_this_binding')
            : i18nT('apps.specEngine.capabilityForm.stage_the_return_to_the_builtin')}
        </button>
        {staged && (
          <button type="button" className="se-btn se-sm" onClick={onWithdraw}>
            {i18nT('apps.specEngine.capabilityForm.withdraw_this_binding_change')}
          </button>
        )}
      </div>
      {!external && !stored.present && (
        <p className="se-note">
          {i18nT('apps.specEngine.capabilityForm.nothing_is_declared_so_there_is_nothing_to_return')}
        </p>
      )}
    </div>
  )
}

/**
 * The capabilities the engine always executes itself, named and not offered.
 *
 * Behind a disclosure because it is the same list on every stage that shows
 * capabilities, and repeating six names above the controls four times teaches a
 * reader to skip the block. It is not hidden: R3.6's obligation is that an operator
 * can learn these exist and that naming one is REFUSED rather than quietly ignored,
 * which is what the sentence inside says. There is no chooser in here.
 */
function EngineFloor({ capabilities }: { capabilities: readonly string[] }) {
  if (capabilities.length === 0) return null
  return (
    <details className="se-disc">
      <summary>{i18nT('apps.specEngine.capabilityForm.capabilities_the_engine_always_runs')}</summary>
      <p className="se-note">
        {i18nT('apps.specEngine.capabilityForm.binding_one_of_these_is_refused', {
          names: capabilities.join(', '),
        })}
      </p>
    </details>
  )
}

/** The draft a row starts from: the binding as it is stored, ready to be changed. */
function initialDraft(row: CapabilityBinding, stored: StoredBinding): BindingDraft {
  return {
    // The row's resolved transport rather than the stored string, so a capability
    // with no stored entry starts on its builtin rather than on `''`.
    transport: row.transport,
    command: stored.command,
    // Never seeded from the document: an environment VALUE is not put on screen by
    // this form, so a draft entry exists only once the operator types one.
    env: [],
    timeout: stored.timeout,
  }
}

/**
 * Bind the delegable capabilities the engine places in one pipeline stage.
 *
 * *capabilities* is the stage's own list, projected by `/config/registry`, so the
 * placement is the engine's and the union across the pane's stages is the whole
 * delegable vocabulary. A capability the projection places here but the bindings
 * read does not answer for is NAMED rather than dropped, because the union has to
 * hold for a vocabulary the engine grows before this read catches up.
 */
export function CapabilityForm({
  stage,
  capabilities,
  reporterFor,
}: {
  stage: string
  capabilities: readonly string[]
  reporterFor?: (surface: string) => (count: number) => void
}) {
  const client = useQueryClient()
  const edits = useStagedEdits()
  const [drafts, setDrafts] = useState<Record<string, BindingDraft>>({})
  const [reviewing, setReviewing] = useState(false)
  const [wrote, setWrote] = useState(false)
  const onPendingCount = reporterFor?.(surfaceKey(stage, 'capabilities'))

  // The vocabulary: which transports may be offered, and which capabilities are
  // the engine's own floor. Shared cache entry with every other surface reading
  // the registry, so this costs no second request.
  const registry = useQuery({
    queryKey: QK.registry,
    queryFn: () => specEngineApi.configRegistry(),
    retry: false,
    staleTime: Infinity,
  })
  // What is in force. Under the config prefix, so a rebind refreshes it.
  const bindings = useQuery({
    queryKey: QK.capabilities,
    queryFn: () => specEngineApi.configCapabilities(),
    retry: false,
  })
  // What is STORED, which is what an edit replaces. The same key and request the
  // rest of the pane reads the document with.
  const config = useQuery({
    queryKey: QK.config,
    queryFn: () => specEngineApi.config(),
    retry: false,
  })

  const write = useMutation({
    mutationFn: (patch: Document) => specEngineApi.writeConfig(patch),
    onSuccess: () => {
      edits.clear()
      setReviewing(false)
      setWrote(true)
      // Re-read rather than adopt the reply's merged document: the reads are this
      // pane's authority on what is persisted. The bindings key sits under the
      // config key's prefix, so one invalidation would cover it — both are named
      // because a reader should not have to know the key layout to see that the
      // provider identities and the reachability answer are re-resolved too.
      void client.invalidateQueries({ queryKey: QK.config })
      void client.invalidateQueries({ queryKey: QK.capabilities })
    },
    // No `onError`: a refusal must leave the staged edits and the typed draft in
    // place, and the queries untouched, so the rows keep showing stored state.
  })

  const transports = registry.isError ? [] : (registry.data?.transports ?? [])
  const engineFloor = registry.isError ? [] : (registry.data?.engine_floor ?? [])
  const document = config.isError ? undefined : config.data?.document
  // `isError` before the data everywhere, which is this pane's rule: React Query
  // keeps the last successful body across a failing refetch, so a row filled from
  // a retained answer would state a binding nobody re-read as the one in force.
  const rows = useMemo(() => {
    if (bindings.isError) return []
    const answered = new Map<string, CapabilityBinding>()
    for (const row of bindings.data?.capabilities ?? []) answered.set(row.capability, row)
    return capabilities.map((capability) => ({ capability, row: answered.get(capability) }))
  }, [bindings.isError, bindings.data, capabilities])

  const rendered = useMemo(
    () => rows.filter((entry) => entry.row !== undefined).map((entry) => entry.capability),
    [rows],
  )
  // A staged edit for a capability this stage no longer renders is dropped: the
  // projection can move a capability to another stage, and an edit no row shows is
  // one no sentence describes and no confirm clears.
  //
  // Only against an ANSWERED read, though. A refused read renders no row at all, so
  // reconciling against it would drop every staged edit on the pane — which is the
  // work the error branch's own badge exists to keep reporting, and a transient
  // refetch failure would silently discard it.
  const { reconcile } = edits
  useEffect(() => {
    if (bindings.isError || !bindings.data) return
    reconcile((edit) => edit.segments[0] === CAPABILITIES && rendered.includes(edit.segments[1]))
  }, [bindings.isError, bindings.data, rendered, reconcile])

  const draftFor = useCallback(
    (row: CapabilityBinding, stored: StoredBinding) =>
      drafts[row.capability] ?? initialDraft(row, stored),
    [drafts],
  )

  const setDraft = (capability: string, draft: BindingDraft) => {
    setWrote(false)
    write.reset()
    setDrafts((current) => ({ ...current, [capability]: draft }))
  }

  const stageBinding = (capability: string, draft: BindingDraft, stored: StoredBinding) => {
    setWrote(false)
    write.reset()
    // Every path under this capability is dropped before the new set is staged.
    // Re-staging is not additive: an environment name a previous press staged and
    // this draft dropped has no stored counterpart to delete, so nothing else
    // would remove it and the patch would carry an entry the form no longer shows.
    reconcile(
      (edit) => !(edit.segments[0] === CAPABILITIES && edit.segments[1] === capability),
    )
    for (const edit of bindingEdits(capability, draft, stored.envNames, stored.timeout)) {
      edits.stage(edit.segments, edit.value)
    }
  }

  const withdraw = (capability: string) => {
    setWrote(false)
    write.reset()
    reconcile(
      (edit) => !(edit.segments[0] === CAPABILITIES && edit.segments[1] === capability),
    )
  }

  const discard = () => {
    edits.clear()
    setReviewing(false)
    setWrote(false)
    write.reset()
  }

  // The stage places no delegable capability here, which delivery genuinely does.
  // Nothing rendered, rather than a heading over an empty list: an empty block
  // would read as "no capability is delegable" instead of "this stage is not
  // configured by providers".
  if (capabilities.length === 0) return null

  const heading = <h3>{i18nT('apps.specEngine.capabilityForm.capability_bindings')}</h3>

  if (bindings.isError || registry.isError || config.isError) {
    return (
      <div className="se-blk">
        {/* What this form HOLDS, not what it can review: with no read it cannot say
            what a staged edit means, and a badge dropping to zero here would report
            unwritten work as gone. Capabilities and not leaves, the same quantity
            the main return reports, so a failing read cannot move the badge on its
            own. */}
        <PendingCount count={stagedCapabilityCount(edits.edits)} onCount={onPendingCount} />
        {heading}
        {bindings.isError && (
          <>
            <Refused
              title={i18nT('apps.specEngine.capabilityForm.could_not_read_the_capability_bindings')}
              error={bindings.error}
            />
            {/* The one thing this failure must not be shown as. An all-builtin list
                is what an UNCONFIGURED document legitimately resolves to, so
                falling back to it would present a refused `capabilities` section as
                a clean one. */}
            <p className="se-note">
              {i18nT('apps.specEngine.capabilityForm.a_failed_read_is_not_every_capability_builtin')}
            </p>
          </>
        )}
        {registry.isError && (
          <Refused
            title={i18nT('apps.specEngine.capabilityForm.could_not_read_the_transports')}
            error={registry.error}
          />
        )}
        {config.isError && (
          <Refused
            title={i18nT('apps.specEngine.specEnginePage.could_not_read_the_configuration')}
            error={config.error}
          />
        )}
      </div>
    )
  }
  if (!bindings.data || !registry.data || !config.data) {
    return (
      <div className="se-blk">
        <PendingCount count={stagedCapabilityCount(edits.edits)} onCount={onPendingCount} />
        {heading}
        <p className="se-note">
          {i18nT('apps.specEngine.capabilityForm.reading_the_capability_bindings')}
        </p>
      </div>
    )
  }

  const reviewed = reviewBindings(edits.edits, rendered)
  const patch = buildFormPatch(reviewed.flatMap((group) => group.edits))
  const refusal = write.isError ? write.error : null

  return (
    <div className="se-blk">
      {/* The same number the "unwritten binding changes" line states, from the same
          list the patch is built from, so the stage badge cannot claim a count this
          form does not show. */}
      <PendingCount count={reviewed.length} onCount={onPendingCount} />
      {heading}
      {/* Stated before any control, because it changes what a chooser MEANS: the
          engine reads bindings from one app-wide section with no per-project layer,
          so a rebind made while a project is selected is not a rebind for that
          project. */}
      <p className="se-note">
        {i18nT('apps.specEngine.capabilityForm.bindings_apply_to_every_project')}
      </p>
      <EngineFloor capabilities={engineFloor} />
      <div className="se-settings">
        {rows.map(({ capability, row }) => {
          if (!row) {
            return (
              <div className="se-setting" key={capability} data-capability={capability}>
                <span className="se-setting-name">{capability}</span>
                <p className="se-note">
                  {i18nT('apps.specEngine.capabilityForm.the_read_answered_no_binding_for_this', {
                    capability,
                  })}
                </p>
              </div>
            )
          }
          const stored = storedBinding(document ?? {}, capability)
          return (
            <CapabilityRow
              key={capability}
              row={row}
              stored={stored}
              transports={transports}
              draft={draftFor(row, stored)}
              staged={reviewed.some((group) => group.capability === capability)}
              error={refusal}
              onDraft={(draft) => setDraft(capability, draft)}
              onStage={() => stageBinding(capability, draftFor(row, stored), stored)}
              onWithdraw={() => withdraw(capability)}
            />
          )
        })}
      </div>
      {transports.length === 0 && (
        <p className="se-note">
          {i18nT('apps.specEngine.capabilityForm.the_engine_declared_no_transport')}
        </p>
      )}
      <div className="se-acts" style={{ marginTop: 9 }}>
        <button
          type="button"
          className="se-btn"
          disabled={reviewed.length === 0}
          onClick={() => setReviewing(true)}
        >
          {i18nT('apps.specEngine.capabilityForm.review_the_exact_binding_change')}
        </button>
        {reviewed.length > 0 && (
          <span className="se-lbl">
            {i18nT('apps.specEngine.capabilityForm.unwritten_binding_changes')}
            {SEP}
            <span className="se-m">{fmtNumber(reviewed.length)}</span>
          </span>
        )}
      </div>
      {reviewing && reviewed.length > 0 && (
        <FormReview
          changes={reviewed.map((group) => ({
            path: dotted(segmentsAt(group.capability)),
            sentence: group.reverts
              ? i18nT('apps.specEngine.capabilityForm.edit_returns_the_capability_to_its_builtin', {
                  capability: group.capability,
                })
              : i18nT('apps.specEngine.capabilityForm.edit_binds_the_capability_to_a_program', {
                  capability: group.capability,
                  transport: group.transport,
                  program: group.program,
                }),
          }))}
          patch={patch}
          // Declared by KIND rather than worded here, so binding a capability to an
          // external program reads as the same act whichever surface performed it.
          // A revert declares neither: it removes a binding rather than granting
          // one, and stating "authorises commands to run" over a deletion would
          // teach a reader to discount the sentence when it is true.
          authorises={reviewed.flatMap((group) =>
            group.reverts
              ? []
              : [
                  { kind: 'external_program' as const, path: dotted(segmentsAt(group.capability)) },
                  { kind: 'commands_run' as const, path: dotted(segmentsAt(group.capability)) },
                ],
          )}
          labels={{
            heading: i18nT('apps.specEngine.capabilityForm.the_binding_that_would_be_written'),
            confirm: i18nT('apps.specEngine.capabilityForm.write_the_binding'),
            writing: i18nT('apps.specEngine.configPanel.saving'),
            discard: i18nT('apps.specEngine.capabilityForm.discard_the_pending_binding_changes'),
            exactly: i18nT('apps.specEngine.capabilityForm.a_confirm_writes_exactly_this_patch'),
            refusalTitle: i18nT('apps.specEngine.capabilityForm.could_not_write_the_binding'),
            retained: i18nT('apps.specEngine.capabilityForm.nothing_was_written_so_rows_are_stored'),
          }}
          writing={write.isPending}
          error={refusal}
          onConfirm={(sending) => write.mutate(sending)}
          onDiscard={discard}
        />
      )}
      {wrote && (
        <p className="se-note" role="status">
          {i18nT('apps.specEngine.capabilityForm.wrote_the_binding_and_re_read_it')}
        </p>
      )}
    </div>
  )
}
