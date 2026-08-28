/**
 * The whole configuration pane: one area per pipeline stage, the resolution beside
 * them, and the document in the advanced area.
 *
 * Lifted out of `ConfigPanel.tsx` rather than added to it. That file is the pane's
 * forms — six thousand lines of them — and the shell that arranges them is a
 * different job with a different failure mode, so the shell, each stage's panel and
 * the stage vocabulary are their own modules and this one only wires them together.
 *
 * ## Shaped like the pipeline, and the shape comes off the wire
 *
 * The pane used to be shaped like its own configuration document: a Settings tab, a
 * Cost profiles tab, a Watch sources tab, a JSON tab. Every surface was equally
 * prominent and finding a setting meant knowing which container the engine keeps it
 * in. It is shaped like the pipeline now — intake, authoring, execution, delivery,
 * plus a separately reachable advanced area — and the placement of every setting
 * group and capability is the ENGINE's, projected by `/config/registry`. This side
 * holds the words for a stage and nothing else, so a setting the engine adds to a
 * mapped group appears under the right stage with no edit here. See `stages.ts` for
 * what happens to a stage this pane has no words for.
 *
 * ## Takes the config read rather than performing its own
 *
 * So the page's first-run detection and this pane cannot disagree about whether a
 * document exists — and a read that FAILED is rendered as the refusal it is,
 * because `config_unreadable` means a document exists and cannot be parsed, which
 * is a repair and not an empty form to fill in. The refusal is read BEFORE the
 * data: React Query retains the last successful answer across a failing refetch,
 * and a form filled from a retained answer would present values nobody re-read as
 * what is in force.
 *
 * The stage vocabulary read is gated the same way. When it refuses, the pane says
 * so and falls back to the one area every unplaced thing folds into, rather than
 * presenting a stage list nobody re-read as the engine's current one.
 *
 * ## What sits above the stages, and why it cannot sit inside one
 *
 * The projects table, because the row selected there governs every stage and the
 * resolved column beside them all: a selection inside a panel is a selection the
 * other panels could not see. Beside it, a sentence naming which project the values
 * on every stage resolve for, and the pane's single count of unwritten changes
 * across all of them — the per-stage badges answer "where is it" and that count
 * answers "is there any", which is the question an operator on a third stage has no
 * other way to ask.
 *
 * ## Layout rules this pane must not break
 *
 * No drawer, no modal, no scrim: the design passes the "safety controls are never
 * behind navigation" criterion only because it contains no overlay, and
 * `SpecEngineShell.test.tsx` fails on a `position:fixed`/`absolute` declaration.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'

import { fmtNumber } from '../../i18n/format'
import { i18nT } from '../../i18n/t'

import { QK, specEngineApi, type ConfigSnapshot } from './api'
import { StagePanel, StageTabs } from './ConfigStageShell'
import { ProjectsTable, Refused, ResolvedPane, isDirty } from './ConfigPanel'
import { isObject, PROJECTS } from './configDocument'
import { StageAdvanced } from './StageAdvanced'
import { StageAuthoring } from './StageAuthoring'
import { StageDelivery } from './StageDelivery'
import { StageExecution } from './StageExecution'
import { StageIntake } from './StageIntake'
import {
  ADVANCED_STAGE,
  declaredGroups,
  panePending,
  resolveStages,
  type ResolvedStage,
} from './stages'

/** The resolved read with no project named. Not a project id, so not a valid one. */
const APP_WIDE = ''

/** Separator between two identifiers on one line. Punctuation, not copy. */
const SEP = ' \u00b7 '

/**
 * One stage's panel contents.
 *
 * A switch on the stage id rather than a table on the stage record, because the
 * five panels take five different sets of props: intake owns the grid selection,
 * the advanced area owns the document draft, and the other three own neither. A
 * uniform table would have to pass every prop to every panel and let each ignore
 * what it does not use, which is how a panel comes to be handed state it has no
 * business holding.
 *
 * A stage id with no panel of its own is impossible by construction —
 * `resolveStages` folds one into the advanced area before it reaches here — so the
 * fall-through renders the advanced area's contents rather than nothing.
 */
function StageContents(props: {
  stage: ResolvedStage
  config: ConfigSnapshot
  project: string
  /** Whether this stage is the pane's only area, so it holds every surface. */
  whole: boolean
  gridSource: string
  onGridSource: (source: string) => void
  onOpenDocument: () => void
  draft: string | null
  onDraft: (text: string | null) => void
  reporterFor: (surface: string) => (count: number) => void
}) {
  const { stage, config, project, reporterFor } = props
  if (stage.id === 'intake') {
    return (
      <StageIntake
        stage={stage}
        config={config}
        project={project}
        gridSource={props.gridSource}
        onGridSource={props.onGridSource}
        onOpenDocument={props.onOpenDocument}
        reporterFor={reporterFor}
      />
    )
  }
  if (stage.id === 'authoring') {
    return <StageAuthoring stage={stage} project={project} reporterFor={reporterFor} />
  }
  if (stage.id === 'execution') {
    return <StageExecution stage={stage} project={project} reporterFor={reporterFor} />
  }
  if (stage.id === 'delivery') {
    return <StageDelivery stage={stage} project={project} reporterFor={reporterFor} />
  }
  return (
    <StageAdvanced
      stage={stage}
      config={config}
      project={project}
      whole={props.whole}
      gridSource={props.gridSource}
      onGridSource={props.onGridSource}
      draft={props.draft}
      onDraft={props.onDraft}
      reporterFor={reporterFor}
    />
  )
}

export function ConfigPane({
  config,
  error,
  pending,
}: {
  config: ConfigSnapshot | undefined
  error: unknown
  pending: boolean
}) {
  // The selected project lives here, above both halves of the pane: the table on
  // the left is what selects it and the resolved read on the right is what it is
  // read FOR, and a copy on either side is how the two come to disagree.
  const [chosenProject, setChosenProject] = useState<string>(APP_WIDE)
  // The source the autonomy grid shows, held here rather than in the panel: the
  // source form links into that matrix for the source it is editing, and a copy of
  // the selection on either side is how the two come to disagree.
  const [gridSource, setGridSource] = useState('')
  // The stage an operator CHOSE, or null while nobody has chosen one. Null rather
  // than a hard-coded first stage, because the stage list arrives from a read: a
  // literal here would name a stage before this pane knows the engine declares it.
  const [chosenStage, setChosenStage] = useState<string | null>(null)
  // How much unwritten work each surface is holding, keyed `<stage>/<surface>` and
  // reported by the surfaces themselves. Held here rather than derived, because the
  // forms own their own staging: the pane observes the counts and never becomes a
  // second place they live.
  const [pendingBySurface, setPendingBySurface] = useState<Record<string, number>>({})
  // The editor's unsaved text, held here so leaving the advanced area keeps it.
  const [draft, setDraft] = useState<string | null>(null)

  // The stage vocabulary. The same key and the same request the forms use, so this
  // costs no second fetch — and deliberately OUTSIDE the config prefix, because it
  // is a projection of the engine's own constants that no write can move.
  const registry = useQuery({
    queryKey: QK.registry,
    queryFn: () => specEngineApi.configRegistry(),
    retry: false,
    staleTime: Infinity,
  })

  // `isError` is NOT read before the data here, and the departure from this pane's
  // own rule is deliberate. Everywhere else a retained answer is a hazard because it
  // describes a stored value a later read failed to confirm. This read is a
  // projection of the engine's own CONSTANTS — no write can move it — and what it
  // supplies is the pane's STRUCTURE: which areas exist. Collapsing that on a failed
  // refetch would unmount every panel and take each form's staged edits, armed
  // removals and half-typed confirmations with it, which is the exact loss the
  // always-mounted panels exist to prevent. So the structure survives, the failure
  // is stated below, and each form's own `isError` guard keeps its ROWS from being
  // filled out of a retained vocabulary.
  const vocabulary = registry.data
  const stages = useMemo(
    () =>
      resolveStages(
        vocabulary?.stages,
        declaredGroups((vocabulary?.settings ?? []).map((setting) => setting.key)),
      ),
    [vocabulary],
  )
  // The chosen stage normalized against the list that actually exists: a stage the
  // engine has stopped declaring must not leave the pane with no visible panel and
  // no tab stop.
  const known = stages.some((stage) => stage.id === chosenStage)
  const active = known ? String(chosenStage) : (stages[0]?.id ?? ADVANCED_STAGE)
  useEffect(() => {
    if (chosenStage !== null && !known) setChosenStage(null)
  }, [chosenStage, known])

  // Normalized against the document itself, not against how the entry left it: a
  // selection whose entry is gone — removed through its row, deleted in the
  // document editor, or dropped by an external write picked up on refetch — falls
  // back to app defaults. Without this, no row matches, the grid loses its only tab
  // stop, and the resolved view renders the app-wide layers under a heading naming
  // a project the document no longer lists, which reads as "this project inherits
  // everything" rather than "this project is gone".
  const documentProjects = config?.document[PROJECTS]
  const chosenKnown =
    chosenProject === APP_WIDE ||
    (isObject(documentProjects) &&
      Object.prototype.hasOwnProperty.call(documentProjects, chosenProject))
  const project = chosenKnown ? chosenProject : APP_WIDE
  // The stored state collapses too, so a later re-add of the same name cannot
  // silently snap the selection back to it with no operator action.
  useEffect(() => {
    if (!chosenKnown) setChosenProject(APP_WIDE)
  }, [chosenKnown])

  // One stable reporter per surface key, so a form's count effect fires on the
  // count changing rather than on every render of this pane. Cached in a ref
  // because the keys are data — a stage id from the wire crossed with a surface
  // name — so they cannot be enumerated in a memo written here. Returning the
  // cached FUNCTION rather than taking the count is what makes the identity
  // stable: an inline `(count) => report(key, count)` at each call site would be
  // a new function every render, and `PendingCount`'s effect depends on it.
  const reporters = useRef(new Map<string, (count: number) => void>())
  const reporterFor = useCallback((surface: string) => {
    let reporter = reporters.current.get(surface)
    if (!reporter) {
      reporter = (count: number) => {
        setPendingBySurface((current) =>
          current[surface] === count ? current : { ...current, [surface]: count },
        )
      }
      reporters.current.set(surface, reporter)
    }
    return reporter
  }, [])
  const unwritten = panePending(pendingBySurface)

  return (
    <>
      <section className="se-cfg">
        <div className="se-cfg-head">
          {/* A filename, not copy: translating it would name a file that does not exist. */}
          <h1 className="se-m">config.json</h1>
          <span className="se-sort">
            {i18nT('apps.specEngine.specEnginePage.the_write_path_validated_on_save')}
          </span>
        </div>
        <div className="se-cfg-body">
          {error ? (
            <Refused
              title={i18nT('apps.specEngine.specEnginePage.could_not_read_the_configuration')}
              error={error}
            />
          ) : pending || !config ? (
            <p className="se-note">
              {i18nT('apps.specEngine.specEnginePage.reading_the_configuration')}
            </p>
          ) : (
            <>
              {/* The question this pane is opened with is "which configuration
                  governs which project", and the answer must not sit under a
                  fixed-height editor. Above the stages and outside them, because
                  the row selected here governs every one of them. */}
              <ProjectsTable config={config} project={project} onSelect={setChosenProject} />
              <p className="se-note">
                {project
                  ? i18nT('apps.specEngine.configPanel.every_stage_resolves_for_project', {
                      project,
                    })
                  : i18nT('apps.specEngine.configPanel.every_stage_resolves_app_wide')}
              </p>
              {!registry.isFetched ? (
                // Before the vocabulary has answered ONCE there is no stage list,
                // and the areas are NOT rendered from a guess: a shell that opened
                // on one area and re-pointed itself when the read landed would move
                // the panel under a reader mid-sentence, and every suite that
                // mounts this pane would race the read.
                //
                // `isFetched` and NOT `isPending`, and the difference is a bug this
                // gate had: React Query reports `pending` again during any refetch
                // of a query that holds no data, so a REFUSED vocabulary read
                // oscillated — the panels unmounted, their mount refetched the
                // failed query, that refetch read as pending, and the panels
                // unmounted again. `isFetched` is sticky once the read has settled
                // either way, which is the actual question being asked here.
                <p className="se-note">
                  {i18nT('apps.specEngine.configPanel.reading_the_pipeline_stages')}
                </p>
              ) : (
                <>
                  {registry.isError && (
                    <Refused
                      title={i18nT(
                        'apps.specEngine.configPanel.could_not_read_the_pipeline_stages',
                      )}
                      error={registry.error}
                    />
                  )}
                  <StageTabs
                    stages={stages}
                    active={active}
                    pending={pendingBySurface}
                    dirty={isDirty(draft, config.document)}
                    problems={config.errors.length}
                    advisories={config.advisories.length}
                    onActivate={setChosenStage}
                  />
                  {unwritten > 0 && (
                    <p className="se-lbl">
                      {i18nT('apps.specEngine.configPanel.unwritten_changes_across_every_stage')}
                      {SEP}
                      <span className="se-m">{fmtNumber(unwritten)}</span>
                    </p>
                  )}
                  {stages.map((stage) => (
                    <StagePanel key={stage.id} stage={stage.id} active={active}>
                      <StageContents
                        stage={stage}
                        config={config}
                        project={project}
                        whole={stages.length === 1}
                        gridSource={gridSource}
                        onGridSource={setGridSource}
                        onOpenDocument={() => setChosenStage(ADVANCED_STAGE)}
                        draft={draft}
                        onDraft={setDraft}
                        reporterFor={reporterFor}
                      />
                    </StagePanel>
                  ))}
                </>
              )}
            </>
          )}
        </div>
      </section>
      <section
        className="se-inspector"
        aria-label={i18nT('apps.specEngine.specEnginePage.resolved_configuration')}
      >
        {error || pending || !config ? (
          <div className="se-insp-body">
            <p className="se-note">
              {i18nT('apps.specEngine.configPanel.the_resolved_read_needs_the_document_first')}
            </p>
          </div>
        ) : (
          <ResolvedPane config={config} project={project} />
        )}
      </section>
    </>
  )
}
