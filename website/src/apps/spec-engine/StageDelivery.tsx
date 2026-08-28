/**
 * The delivery stage: where results go.
 *
 * Carries the `delivery` and `notify` setting groups — whether a change integrates
 * on its own, whether review feedback is read back, and which channel is told
 * about it. The engine places no delegable capability in delivery: delivery runs
 * an ordered sequence of stage COMMANDS rather than a bound provider, so there is
 * nothing here for {@link StageCapabilities} to name and it renders nothing.
 */
import { StageCapabilities, StageIntro, StageSettings } from './StageParts'
import type { ResolvedStage } from './stages'

export function StageDelivery({
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
      <StageCapabilities capabilities={stage.capabilities} />
    </>
  )
}
