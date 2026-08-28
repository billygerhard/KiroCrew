/**
 * The intake stage: where work comes from.
 *
 * Carries its own setting groups — `watch` today, whatever the engine places in
 * intake tomorrow — plus the two surfaces that define and bound the sources those
 * settings govern:
 *
 * - **The watch-source definitions form**, because the engine places both the
 *   `watch` setting group and the `watch_sources` capability in intake, so the
 *   sources themselves belong with the settings that poll them.
 * - **The autonomy grid**, on the same panel as the form that links into it. The
 *   form's enable consequence points at the matrix showing how far that source's
 *   items may run unattended, and a link that crossed panels would hide the very
 *   thing it points at.
 *
 * The document editor is reached from here when a stored source is a shape no form
 * expresses, which is a cross-stage jump this panel asks its parent to perform
 * rather than opening a second editor of its own.
 */
import { SourceForm, SourcesSection } from './ConfigPanel'
import { StageCapabilities, StageIntro, StageSettings } from './StageParts'
import { surfaceKey, type ResolvedStage } from './stages'
import type { ConfigSnapshot } from './api'

export function StageIntake({
  stage,
  config,
  project,
  gridSource,
  onGridSource,
  onOpenDocument,
  reporterFor,
}: {
  stage: ResolvedStage
  config: ConfigSnapshot
  project: string
  /** The source the autonomy grid shows, held by the pane so both halves agree. */
  gridSource: string
  onGridSource: (source: string) => void
  /** Reach the document editor, for a stored shape no form can express. */
  onOpenDocument: () => void
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
      <SourceForm
        config={config}
        onShowGrid={onGridSource}
        onOpenJson={onOpenDocument}
        onPendingCount={reporterFor(surfaceKey(stage.id, 'sources'))}
      />
      <SourcesSection chosen={gridSource} onChoose={onGridSource} />
      <StageCapabilities capabilities={stage.capabilities} />
    </>
  )
}
