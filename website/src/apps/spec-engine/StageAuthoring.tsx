/**
 * The authoring stage: how specification documents are written, analyzed, and
 * validated.
 *
 * The one stage that holds no setting group, and that is the engine's placement
 * rather than an omission here: authoring is configured entirely by WHICH
 * PROVIDERS write, analyze and validate documents — the `authoring`, `analysis`
 * and `validation_rules` capabilities — so there is no numeric knob for it to
 * carry. {@link StageSettings} renders nothing for an empty group list precisely
 * so this panel does not state "no setting is registered", which would describe
 * the engine's registry rather than this stage.
 *
 * The panel is written against the stage's projected groups anyway, not against
 * the knowledge that there are none: a group the engine places in authoring later
 * appears here with no edit to this file.
 */
import { StageCapabilities, StageIntro, StageSettings } from './StageParts'
import type { ResolvedStage } from './stages'

export function StageAuthoring({
  stage,
  project,
  reporterFor,
}: {
  stage: ResolvedStage
  project: string
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
      <StageCapabilities
        stage={stage.id}
        capabilities={stage.capabilities}
        reporterFor={reporterFor}
      />
    </>
  )
}
