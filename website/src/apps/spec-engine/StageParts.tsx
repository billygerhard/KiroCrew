/**
 * The parts every pipeline-stage panel is built from.
 *
 * One module rather than five copies: the panels differ in WHICH surfaces they
 * carry, not in how a stage introduces itself or how its settings are filtered, so
 * the shared shape is stated once and each panel stays short enough to read in one
 * pass.
 */
import { CapabilityForm } from './CapabilityForm'
import { SettingsForm } from './ConfigPanel'
import { stageLabel, stageSummary, surfaceKey } from './stages'

/**
 * The stage's name and the one sentence it states before any of its controls.
 *
 * The sentence is not decoration and it comes FIRST: a heading reading `Execution`
 * tells an operator nothing about whether a poll interval belongs to it, and a
 * pane organised by stage is only navigable if each stage says what it governs.
 */
export function StageIntro({ stage }: { stage: string }) {
  return (
    <div className="se-blk">
      <h3>{stageLabel(stage)}</h3>
      <p className="se-note">{stageSummary(stage)}</p>
    </div>
  )
}

/**
 * The stage's settings, generated from the registry and filtered to its groups.
 *
 * Renders nothing when the stage holds no setting group — the authoring stage holds
 * none, by the engine's own placement, and an empty settings block there would read
 * as "no setting is registered" rather than as "this stage is configured by its
 * providers".
 *
 * `groups` being `undefined` is the opposite case and renders the form UNFILTERED:
 * no vocabulary was read, so the form is what states the refusal or the wait. The
 * two are distinct answers and collapsing them would either hide a failed read or
 * report on the registry from a stage that legitimately holds no knob.
 *
 * The staged edits belong to the form, one `useStagedEdits` per stage, and the
 * count is reported out under this stage's own surface key so the stage badge and
 * the pane-level total are two readings of one table.
 */
export function StageSettings({
  stage,
  project,
  groups,
  reporterFor,
}: {
  stage: string
  project: string
  groups: readonly string[] | undefined
  reporterFor: (surface: string) => (count: number) => void
}) {
  if (groups !== undefined && groups.length === 0) return null
  return (
    <SettingsForm
      project={project}
      groups={groups}
      onPendingCount={reporterFor(surfaceKey(stage, 'settings'))}
    />
  )
}

/**
 * The delegable capabilities the engine places in this stage, as bindable rows.
 *
 * The placement is the engine's: a capability it declares delegable is bindable
 * from here, and a stage that listed none of them would leave an operator no way to
 * see that the authoring stage is configured entirely by its providers. A stage the
 * engine places no capability in renders nothing, which delivery genuinely does —
 * delivery runs an ordered sequence of stage COMMANDS rather than a bound provider.
 *
 * The form itself lives in its own module: what each capability costs, whether its
 * program can be found, and what an external binding does and does not promise are
 * a set of claims worth reading in one place, and none of them belongs in the
 * arrangement of a stage panel.
 */
export function StageCapabilities({
  stage,
  capabilities,
  reporterFor,
}: {
  stage: string
  capabilities: readonly string[]
  reporterFor: (surface: string) => (count: number) => void
}) {
  return (
    <CapabilityForm stage={stage} capabilities={capabilities} reporterFor={reporterFor} />
  )
}
