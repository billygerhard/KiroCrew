/**
 * The delivery stage: where results go.
 *
 * Carries the `delivery` and `notify` setting groups — whether a change integrates
 * on its own, whether review feedback is read back, and which channel is told
 * about it — plus the workflow form, because delivery is the one part of the
 * pipeline the engine drives with COMMANDS rather than with a bound provider. That
 * is also why the engine places no delegable capability here: there is no provider
 * to bind, so {@link StageCapabilities} names none and renders nothing.
 *
 * The quality gates sit here too, and for a related but distinct reason: a gate's
 * POSITION is defined relative to raising the review artifact, which is a delivery
 * stage. They are app-wide rather than per-project, unlike the workflow above.
 */
import { DeliveryWorkflowForm } from './DeliveryWorkflowForm'
import { GateForm } from './GateForm'
import { StageCapabilities, StageIntro, StageSettings } from './StageParts'
import { surfaceKey, type ResolvedStage } from './stages'
import type { ConfigSnapshot } from './api'

export function StageDelivery({
  stage,
  config,
  project,
  reporterFor,
}: {
  stage: ResolvedStage
  /** Taken from the pane's own read, so the two cannot disagree about the store. */
  config: ConfigSnapshot
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
      <DeliveryWorkflowForm
        document={config.document}
        project={project}
        onPendingCount={reporterFor(surfaceKey(stage.id, 'workflow'))}
      />
      {/* The gates sit with delivery because a gate's POSITION is defined relative to
          raising the review artifact, which is a delivery stage. They are app-wide
          rather than per-project, unlike the workflow above, and the form says so. */}
      <GateForm
        document={config.document}
        project={project}
        onPendingCount={reporterFor(surfaceKey(stage.id, 'gates'))}
      />
      <StageCapabilities
        stage={stage.id}
        capabilities={stage.capabilities}
        reporterFor={reporterFor}
      />
    </>
  )
}
