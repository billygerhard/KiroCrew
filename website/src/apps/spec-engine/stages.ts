/**
 * The pipeline stages the configuration pane is organised around.
 *
 * The pane used to be shaped like its own configuration document: a Settings tab,
 * a Cost profiles tab, a Watch sources tab, a JSON tab. It is shaped like the
 * pipeline now — where work comes from, how documents are authored, how tasks
 * execute, where results go — so a setting is found by thinking about what it
 * affects rather than about which container it lives in.
 *
 * ## The vocabulary is the engine's; only the words are here
 *
 * `GET /config/registry` projects `stages`: one entry per pipeline stage carrying
 * the setting GROUPS and the delegable CAPABILITIES that stage presents, composed
 * by `engine/config/pipeline.py` from the setting registry's own declaration
 * order. This module holds no copy of that placement. What it holds is the
 * stage-to-catalog-key mapping — a label and one summary sentence per stage id —
 * because a stage's WORDS cannot come off the wire and a setting the engine adds
 * to a mapped group must appear without an edit on this side.
 *
 * ## An unknown stage is folded, never dropped
 *
 * The engine can grow a stage before this pane has words for one. When it does,
 * {@link resolveStages} folds that stage's groups and capabilities into the
 * advanced area rather than rendering a nameless tab or — much worse — silently
 * discarding them: the write door still enforces every one of those settings, so a
 * dropped group is a setting in force on every run and reachable from nowhere.
 * The same fold catches a group the registry declares that no projected stage
 * claims, so the union of the rendered stages is always the whole vocabulary.
 *
 * That is the frontend half of the engine's own choice at
 * `pipeline.setting_group_stage`, which defaults an unmapped group to advanced for
 * the same stated reason rather than raising.
 */
import { i18nT } from '../../i18n/t'
import type { StageVocabulary } from './api'

/**
 * One stage as the registry projects it.
 *
 * Re-exported from `api.ts`, where every wire shape this app reads is
 * transcribed, so this module states the words for a stage and never a second
 * spelling of its payload.
 */
export type { StageVocabulary }

/** The stage that holds everything the pipeline's own steps do not. */
export const ADVANCED_STAGE = 'advanced'

/**
 * Human label per pipeline stage, as whole literal catalog keys so the
 * key-reference gate resolves every entry — the `SETTING_LABEL_KEY` idiom.
 *
 * Keys rather than resolved strings: a module-level `i18nT()` runs once at import
 * and would freeze these in whichever language happened to be active then. A
 * stage absent here is not an error and not a crash; it is folded into the
 * advanced area by {@link resolveStages}, because a tab labelled with a raw
 * engine identifier is worse than one area honestly holding the unplaced.
 */
const STAGE_LABEL_KEY: Record<string, string> = {
  intake: 'apps.specEngine.configPanel.stage_intake',
  authoring: 'apps.specEngine.configPanel.stage_authoring',
  execution: 'apps.specEngine.configPanel.stage_execution',
  delivery: 'apps.specEngine.configPanel.stage_delivery',
  advanced: 'apps.specEngine.configPanel.stage_advanced',
}

/**
 * The one sentence each stage states before any of its controls.
 *
 * Not decoration: a stage named `Execution` tells an operator nothing about
 * whether a poll interval belongs to it. Each sentence names what the stage
 * GOVERNS, echoing the engine's own docstring for that stage constant so the two
 * cannot drift into describing different groupings.
 */
const STAGE_SUMMARY_KEY: Record<string, string> = {
  intake: 'apps.specEngine.configPanel.stage_intake_summary',
  authoring: 'apps.specEngine.configPanel.stage_authoring_summary',
  execution: 'apps.specEngine.configPanel.stage_execution_summary',
  delivery: 'apps.specEngine.configPanel.stage_delivery_summary',
  advanced: 'apps.specEngine.configPanel.stage_advanced_summary',
}

/** Whether this pane has words for *stage*, and so an area to render it in. */
export function stageIsNamed(stage: string): boolean {
  return Boolean(STAGE_LABEL_KEY[stage] && STAGE_SUMMARY_KEY[stage])
}

/** The translated label for *stage*. Only ever called for a named stage. */
export function stageLabel(stage: string): string {
  // Indexed at the call site rather than through a local, so the key-reference
  // gate resolves every entry in the map — the ORIGIN_KEY idiom.
  return STAGE_LABEL_KEY[stage] ? i18nT(STAGE_LABEL_KEY[stage]) : stage
}

/** The translated summary sentence for *stage*. */
export function stageSummary(stage: string): string {
  return STAGE_SUMMARY_KEY[stage] ? i18nT(STAGE_SUMMARY_KEY[stage]) : ''
}

/** One stage as the pane renders it, after folding. */
export interface ResolvedStage {
  id: string
  /**
   * Setting groups this area presents, in the order they were projected.
   *
   * `undefined` is a third answer and not an empty one: it means no vocabulary was
   * read AT ALL, so this pane cannot place anything and the advanced area holds
   * everything — including the settings surface unfiltered, which is then the
   * surface that states why it has no rows. An empty ARRAY means the engine places
   * no setting group in this stage, which authoring genuinely does, and there the
   * settings surface renders nothing rather than reporting on the registry.
   */
  groups: readonly string[] | undefined
  /** Delegable capabilities this area presents, in projected order. */
  capabilities: readonly string[]
}

/**
 * The stages to render, in the order the registry projected them.
 *
 * Three things happen here and nothing else — no placement of its own, and no
 * re-derivation of which group belongs where:
 *
 * 1. A projected stage this pane has words for keeps its own area, carrying
 *    exactly the groups and capabilities the engine placed in it.
 * 2. A projected stage this pane has NO words for is folded into the advanced
 *    area, contents and all.
 * 3. Any group in *groups* that no projected stage claimed is appended to the
 *    advanced area, so the union of the rendered areas is the whole registry
 *    vocabulary even when the projection is empty or partial.
 *
 * The advanced area always exists, appended last when the projection omits it,
 * because rules 2 and 3 need somewhere to fold into. Every other stage appears
 * only when projected: this pane does not invent a stage the engine has stopped
 * declaring.
 *
 * *groups* is the caller's list of every group the registry declares — read off
 * the projected settings rather than passed as a constant, which is what makes
 * rule 3 hold for a group added after this file was written.
 */
export function resolveStages(
  projected: readonly StageVocabulary[] | undefined,
  groups: readonly string[],
): ResolvedStage[] {
  // Nothing was read: not one stage and not one setting. The single advanced area
  // then presents the settings surface UNFILTERED — `groups: undefined` — so the
  // surface itself says why it has no rows, rather than the pane rendering an area
  // that silently holds nothing.
  if ((projected === undefined || projected.length === 0) && groups.length === 0) {
    return [{ id: ADVANCED_STAGE, groups: undefined, capabilities: [] }]
  }
  const order: string[] = []
  const areas = new Map<string, { groups: string[]; capabilities: string[] }>()
  const area = (stage: string) => {
    let found = areas.get(stage)
    if (!found) {
      found = { groups: [], capabilities: [] }
      areas.set(stage, found)
      order.push(stage)
    }
    return found
  }
  // The advanced area is claimed first so it lands in projected order when the
  // projection lists it, and last when it does not.
  const advanced = () => area(ADVANCED_STAGE)
  for (const stage of projected ?? []) {
    const target = stageIsNamed(stage.id) ? area(stage.id) : advanced()
    for (const group of stage.setting_groups ?? []) target.groups.push(group)
    for (const capability of stage.capabilities ?? []) target.capabilities.push(capability)
  }
  const placed = new Set<string>()
  for (const entry of areas.values()) for (const group of entry.groups) placed.add(group)
  const unplaced = groups.filter((group) => !placed.has(group))
  if (unplaced.length > 0 || !areas.has(ADVANCED_STAGE)) {
    const target = advanced()
    for (const group of unplaced) {
      // A group can be declared twice by two settings; the area lists it once.
      if (!target.groups.includes(group)) target.groups.push(group)
    }
  }
  // A vocabulary that places no group ANYWHERE — stages projected, no setting
  // registered — puts the unfiltered settings surface in the advanced area for the
  // same reason as the no-read case above: with nothing to filter by, the surface
  // is what states that the engine registers no setting, and every other area
  // legitimately shows none. `placed` is read before the unplaced fold, which adds
  // only groups already counted in *groups*.
  const empty = placed.size === 0 && groups.length === 0
  return order.map((id) => {
    const entry = areas.get(id) as { groups: string[]; capabilities: string[] }
    const unfiltered = empty && id === ADVANCED_STAGE
    return { id, groups: unfiltered ? undefined : entry.groups, capabilities: entry.capabilities }
  })
}

/**
 * The distinct groups *keys* declare, in first-appearance order.
 *
 * The leading dot-segment is `Setting.group` on the engine's side, and a key with
 * no dot is its own whole group rather than being dropped — total by
 * construction, which is what lets {@link resolveStages} account for every
 * setting the registry projects.
 */
export function declaredGroups(keys: readonly string[]): string[] {
  const seen: string[] = []
  for (const key of keys) {
    const dot = key.indexOf('.')
    const group = dot < 0 ? key : key.slice(0, dot)
    if (!seen.includes(group)) seen.push(group)
  }
  return seen
}

/**
 * The key a form reports its unwritten count under.
 *
 * `<stage>/<surface>` rather than a flat name, so the pane can sum a stage's
 * badge and its own pane-wide total from the same record without holding a second
 * table saying which surface sits on which stage. The forms own their staging —
 * lifting it here is the drift the shared `useStagedEdits` hook exists to prevent
 * — so this record is an OBSERVATION of them and never a second place a count
 * lives.
 */
export function surfaceKey(stage: string, surface: string): string {
  return `${stage}/${surface}`
}

/** How much unwritten work the surfaces on *stage* are holding, in total. */
export function stagePending(pending: Readonly<Record<string, number>>, stage: string): number {
  const prefix = `${stage}/`
  let total = 0
  for (const [key, count] of Object.entries(pending)) {
    if (key.startsWith(prefix)) total += count
  }
  return total
}

/**
 * How much unwritten work the whole pane is holding.
 *
 * One number across every stage, because the per-stage badges answer "where is
 * it" and this answers "is there any" — and an operator who has staged edits on
 * two stages and is looking at a third needs the second question answered
 * without walking the tabs.
 */
export function panePending(pending: Readonly<Record<string, number>>): number {
  let total = 0
  for (const count of Object.values(pending)) total += count
  return total
}
