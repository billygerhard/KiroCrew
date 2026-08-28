/**
 * The advanced area: everything that is not a step of the pipeline, plus anything
 * this pane has no stage for.
 *
 * Separately reachable rather than hidden, which is the whole point of it. Three
 * kinds of thing land here, and each for a stated reason:
 *
 * 1. **The setting groups the engine places in advanced**, `telemetry` today —
 *    the one group that governs no step of the pipeline — together with any group
 *    the registry declares that no projected stage claimed, and the contents of
 *    any stage the engine has grown that this pane has no words for. The write
 *    door still enforces every one of those settings, so folding them here is what
 *    keeps a setting in force on every run from being reachable from nowhere.
 * 2. **Cost profiles.** A profile assigns a model and an effort per ROLE, and the
 *    engine's roles — design, review, implement, analysis, setup — span authoring
 *    and execution both. No single pipeline stage owns it, and placing it under one
 *    would claim it affects only that stage. That is the engine's own reasoning for
 *    putting the `model_catalog` capability here rather than under authoring: a
 *    lookup every stage reads is not a step any one of them performs. Cost profiles
 *    and the model catalog are the same subject, so they sit together.
 * 3. **The document editor**, still the escape hatch and still complete. It edits
 *    the WHOLE document including anything the forms do not express, which is
 *    exactly why it is not a stage's surface: it is not scoped to one part of the
 *    pipeline. It is rendered only in this area, so the raw document is never on
 *    screen unless this area was chosen.
 *
 * ## When this area is the only one
 *
 * A refused vocabulary read leaves the pane no stages to lay out, and it collapses
 * to this area alone. `whole` says so, and then this area also carries the intake
 * surfaces — the watch-source form and the autonomy grid — because "everything the
 * pane has no stage for" is, in that state, everything. Without it a failed read of
 * a bundled constants projection would make the watch-source form unreachable, and
 * its own refusal branch unreachable with it. It cannot duplicate the intake area's
 * copy: `whole` is only ever true when no other area exists.
 */
import { i18nT } from '../../i18n/t'

import { DocumentEditor, ProfilesForm, SourceForm, SourcesSection } from './ConfigPanel'
import { StageCapabilities, StageIntro, StageSettings } from './StageParts'
import { surfaceKey, type ResolvedStage } from './stages'
import type { ConfigSnapshot } from './api'

export function StageAdvanced({
  stage,
  config,
  project,
  whole,
  gridSource,
  onGridSource,
  draft,
  onDraft,
  reporterFor,
}: {
  stage: ResolvedStage
  config: ConfigSnapshot
  project: string
  /** Whether this area is the pane's only one, so it holds every surface. */
  whole: boolean
  gridSource: string
  onGridSource: (source: string) => void
  /** The editor's unsaved text, held by the pane so leaving the area keeps it. */
  draft: string | null
  onDraft: (text: string | null) => void
  reporterFor: (surface: string) => (count: number) => void
}) {
  return (
    <>
      <StageIntro stage={stage.id} />
      <StageSettings
        stage={stage.id}
        project={project}
        groups={stage.groups}
        reporterFor={reporterFor}
      />
      {whole && (
        <>
          <SourceForm
            config={config}
            onShowGrid={onGridSource}
            // Already in this area: the document editor is below, on this panel.
            onOpenJson={() => {}}
            onPendingCount={reporterFor(surfaceKey(stage.id, 'sources'))}
          />
          <SourcesSection chosen={gridSource} onChoose={onGridSource} />
        </>
      )}
      <ProfilesForm
        config={config}
        onPendingCount={reporterFor(surfaceKey(stage.id, 'profiles'))}
      />
      <StageCapabilities capabilities={stage.capabilities} />
      <div className="se-blk">
        <h3>{i18nT('apps.specEngine.configPanel.tab_json_view')}</h3>
        <p className="se-note">
          {i18nT('apps.specEngine.configPanel.the_json_view_edits_what_no_form_expresses')}
        </p>
        <DocumentEditor config={config} text={draft} onText={onDraft} />
      </div>
    </>
  )
}
