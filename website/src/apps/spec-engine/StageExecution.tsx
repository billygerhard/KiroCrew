/**
 * The execution stage: how tasks execute.
 *
 * Carries four setting groups by the engine's placement — `concurrency`, `limits`,
 * `timeouts` and `budget` — because every bound they declare is a bound on running
 * a task: how many run at once, how often a failure is retried, how long a phase
 * may take, and what a run may spend. A timeout is not an advanced knob just
 * because it is numeric, which is the reading the previous schema-shaped pane
 * invited by grouping all twenty-one settings into one list.
 *
 * Its capabilities are `review` and `implementation` — the two the engine reaches
 * while tasks are running rather than while documents are being written.
 */
import { StageCapabilities, StageIntro, StageSettings } from './StageParts'
import type { ResolvedStage } from './stages'

export function StageExecution({
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
