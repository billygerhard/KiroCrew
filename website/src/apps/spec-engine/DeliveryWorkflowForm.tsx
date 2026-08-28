/**
 * The delivery workflow, as a form: which preset is in force, what each stage
 * runs, and how to define a preset of your own.
 *
 * Delivery is the one part of the pipeline the engine drives with COMMANDS rather
 * than with a bound provider, and it bundles presets for hosts it knows. An
 * operator whose code lives somewhere it does not know needs to say how their host
 * is driven, which is why the stage commands here are operator-authored argv rather
 * than a closed set of presets — the opposite of the watch-source form's
 * constraint, and deliberately so. The form therefore states plainly that what is
 * entered will be executed, and the write travels the operator-confirmed path the
 * engine already requires: `workflow` and `projects.*.workflow` are both in
 * `CONFIG_ONLY_PATHS`, so the agent-facing write tool refuses them and a
 * confirmation here is the only way they can be written at all.
 *
 * Six properties of this surface are correctness claims rather than arrangement:
 *
 * 1. **No precedence is derived here.** Which layer supplied a stage's commands is
 *    `GET /config/workflow`'s answer, projected from `preset_display.stage_origins`
 *    over the same `DeliveryWorkflow` the run itself resolves through. This module
 *    relabels that answer and computes none of it; `workflowRows.ts` holds the pure
 *    row builder and a test asserts on its source that it names no layer.
 * 2. **A user-defined preset is never flattened onto a bundled one, and a stage
 *    nobody defines is never shown as preset-supplied.** Both are invariants of
 *    that display path, and both survive to the wire — a chooser or a row that
 *    collapsed either would give back the ambiguity bundled-name reservation exists
 *    to prevent, or would claim a stage runs when it skips.
 * 3. **The commands rendered are the commands the engine resolved.** Each argument
 *    is rendered as its own token, byte-equal to the payload's, from `argv` and
 *    never from `commands` — which is a COUNT, and a row built from it would show
 *    the right number of commands and none of their text.
 * 4. **There is no reorder control, because there is nothing to reorder.** The
 *    delivery flow's stage list is fixed in the engine and teardown is executed by
 *    archive rather than in sequence, so a stage says WHEN it runs — relayed, not
 *    inferred — and a stage outside the flow says when it runs instead of being
 *    listed as though it ran with the others.
 * 5. **A reserved name is refused before a write is composed.** The bundled names
 *    come from the registry projection, so this refusal is the write door's own
 *    list rather than a copy; the entered stage commands are held in this form's
 *    own draft state, keyed by stage, so refusing the NAME cannot discard them.
 * 6. **A removal that would strand a selection is refused, naming what selects
 *    it.** A `disabled` control with no reason leaves no next action, and the next
 *    action is to point those projects elsewhere.
 *
 * Every write is per-stage or per-leaf and never wholesale. That is load-bearing
 * for the confirmation card rather than a tidiness preference: its fence matcher
 * marks an operator-confirmed field when the fenced key is LITERALLY present in the
 * patch, so a patch that replaced all of `projects` or all of `workflow` from
 * outside would carry the fenced write with nothing flagging it. See
 * `workflowRows.presetStageSegments`.
 */
import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { fmtNumber } from '../../i18n/format'
import { i18nT } from '../../i18n/t'

import { QK, QK_RESOLVED_ROOT, specEngineApi, type WorkflowState } from './api'
import { FormReview, PendingCount, Refused, type ReviewedChange } from './ConfigPanel'
import {
  DELETE,
  buildFormPatch,
  dotted,
  isObject,
  nodeAt,
  PROJECTS,
  type Document,
  type StagedEdit,
} from './configDocument'
import { useStagedEdits } from './useStagedEdits'
import {
  parseCommandBlock,
  presetSegments,
  presetSelectionSegments,
  presetStageSegments,
  workflowStageRows,
  WORKFLOW,
  WORKFLOW_PRESET,
  type WorkflowStageRow,
} from './workflowRows'

/** Separator between two identifiers on one line. Punctuation, not copy. */
const SEP = ' \u00b7 '

/** An empty vocabulary before a read answers, so a memo sees one array. */
const NO_NAMES: readonly string[] = []

/**
 * What each stage's source MEANS, keyed by the engine's own answer.
 *
 * Keys rather than resolved strings, for the `ORIGIN_KEY` idiom's reason: a
 * module-level `i18nT()` runs once at import and would freeze the language.
 *
 * This table names all five layers, and that is presentation rather than
 * resolution — it turns an answer into words and decides nothing. The row builder
 * in `workflowRows.ts` names none of them, which is what a test asserts: a branch
 * on one of these names inside a row builder would be a second resolver, while a
 * label for one is the whole job here.
 *
 * A source absent from this table renders as the engine's own token rather than as
 * nothing, so a layer the engine grows is visible and unexplained rather than
 * silently blank.
 */
const SOURCE_KEY: Record<string, string> = {
  bundled_preset: 'apps.specEngine.workflowForm.source_bundled_preset',
  user_preset: 'apps.specEngine.workflowForm.source_user_preset',
  app_override: 'apps.specEngine.workflowForm.source_app_override',
  project_override: 'apps.specEngine.workflowForm.source_project_override',
  unconfigured: 'apps.specEngine.workflowForm.source_unconfigured',
}

/**
 * What each declared stage is FOR, keyed by the engine's stage name.
 *
 * A stage is a name and a list of commands on the wire; a reader deciding whether
 * `verify` is where their test suite belongs needs the sentence. A stage absent
 * here renders its name with no sentence rather than being dropped, because the
 * engine can declare a stage before this pane has words for it and the commands it
 * runs still execute.
 */
const STAGE_PURPOSE_KEY: Record<string, string> = {
  isolate: 'apps.specEngine.workflowForm.purpose_isolate',
  submit: 'apps.specEngine.workflowForm.purpose_submit',
  verify: 'apps.specEngine.workflowForm.purpose_verify',
  publish: 'apps.specEngine.workflowForm.purpose_publish',
  teardown: 'apps.specEngine.workflowForm.purpose_teardown',
}

/**
 * When a stage OUTSIDE the delivery flow actually runs, keyed by the run point.
 *
 * Only the non-delivery points are here: a stage the flow runs needs no sentence
 * saying so, and the row already sits in the flow's own list. An unmapped or empty
 * run point falls to the unknown sentence, which says the projection has no answer
 * — never that the stage does not run, which is the reading a blank would invite.
 */
const RUN_POINT_KEY: Record<string, string> = {
  isolation: 'apps.specEngine.workflowForm.runs_when_the_workspace_is_isolated',
  archive: 'apps.specEngine.workflowForm.runs_when_the_run_is_archived',
}

/** The projects whose entry selects the workflow preset *preset*, in name order. */
export function projectsSelectingPreset(document: Document, preset: string): string[] {
  const node = document[PROJECTS]
  if (!isObject(node)) return []
  return Object.keys(node)
    .filter((name) => {
      const entry = node[name]
      if (!isObject(entry)) return false
      const workflow = entry[WORKFLOW]
      return isObject(workflow) && workflow[WORKFLOW_PRESET] === preset
    })
    .sort()
}

/** Whether the app-wide selection names *preset*. */
export function appSelectsPreset(document: Document, preset: string): boolean {
  return nodeAt(document, [WORKFLOW, WORKFLOW_PRESET]) === preset
}

/**
 * Why a typed preset name cannot be defined, or `''` when it can.
 *
 * `reserved` is the engine's own refusal, enforced from the engine's own list of
 * bundled names: redefining one is refused at the write door with `'<name>' is a
 * bundled preset name and cannot be redefined`, so offering the write and letting
 * it fail would spend a confirmation to learn something the projection already
 * says. `taken` is this form's: an existing definition would be MERGED into rather
 * than created, which is an edit to that preset and not the addition on offer.
 */
export function presetNameRefusal(
  name: string,
  bundled: readonly string[],
  defined: readonly string[],
): '' | 'reserved' | 'taken' {
  if (name === '') return ''
  if (bundled.includes(name)) return 'reserved'
  if (defined.includes(name)) return 'taken'
  return ''
}

/** One stage's argument list, each argument its own byte-equal token. */
function StageCommands({ row }: { row: WorkflowStageRow }) {
  return (
    <ul className="se-names" data-stage-commands={row.stage}>
      {row.commands.map((command, index) => (
        <li className="se-evid-item se-m" key={`${index}:${command.join('\u0000')}`}>
          {command.map((argument, position) => (
            <span className="se-val" key={position} data-argument={position}>
              {argument}
            </span>
          ))}
        </li>
      ))}
    </ul>
  )
}

/**
 * One stage: what it is for, when it runs, where its commands came from, and them.
 *
 * No control on this row moves it. The stages arrive in the order the engine runs
 * them and there is no `order` key to write, so a reorder affordance here would
 * offer an edit the document cannot express.
 */
function StageRow({ row }: { row: WorkflowStageRow }) {
  const purposeKey = STAGE_PURPOSE_KEY[row.stage]
  const runPointKey = RUN_POINT_KEY[row.runsAt]
  return (
    <div className="se-setting" data-stage={row.stage} data-source={row.source}>
      <p className="se-setting-name">
        <span className="se-m">{row.stage}</span>
        {purposeKey && <span className="se-note">{i18nT(purposeKey)}</span>}
      </p>
      {!row.inDeliveryFlow && (
        <p className="se-note">
          {runPointKey
            ? i18nT(runPointKey)
            : i18nT('apps.specEngine.workflowForm.runs_at_a_point_this_read_does_not_name')}
        </p>
      )}
      <p className="se-note">
        {/* Its own element, so the source reads as one discrete statement rather
            than running into the preset name and the declaring path beside it.
            Indexed at the call site rather than through a local, so the
            key-reference gate resolves every entry in the table. */}
        <span className="se-flag" data-source-label={row.source}>
          {SOURCE_KEY[row.source] ? i18nT(SOURCE_KEY[row.source]) : row.source}
        </span>
        {row.preset !== '' && (
          <>
            {SEP}
            <span className="se-m">{row.preset}</span>
          </>
        )}
        {row.declaredAt !== '' && <span className="se-kv-path">{row.declaredAt}</span>}
      </p>
      {row.inert ? (
        <p className="se-note">{i18nT('apps.specEngine.workflowForm.this_stage_takes_no_action')}</p>
      ) : (
        <StageCommands row={row} />
      )}
    </div>
  )
}

/** One group of preset names to choose from, bundled or user-defined. */
function PresetChoices({
  label,
  names,
  selected,
  onChoose,
}: {
  label: string
  names: readonly string[]
  selected: string
  onChoose: (name: string) => void
}) {
  return (
    <div className="se-acts" role="group" aria-label={label}>
      {names.map((name) => (
        <button
          key={name}
          type="button"
          className="se-btn se-sm se-m"
          aria-pressed={name === selected}
          onClick={() => onChoose(name)}
        >
          {name}
        </button>
      ))}
    </div>
  )
}

export function DeliveryWorkflowForm({
  document,
  project,
  onPendingCount,
}: {
  /** The stored document, for the selections a removal would strand. */
  document: Document
  /** The project the pane resolved for, `''` for app-wide. */
  project: string
  /** Report how many staged changes this form would review, for the stage badge. */
  onPendingCount?: (count: number) => void
}) {
  const client = useQueryClient()
  const edits = useStagedEdits()
  // The preset being defined, and its per-stage command text. Held HERE rather than
  // as staged edits keyed by the name, because a refused name must not discard what
  // was typed: the name is part of every path a definition writes, so a rejected
  // name has no path to hold a draft at.
  const [draftName, setDraftName] = useState('')
  const [draftStages, setDraftStages] = useState<Record<string, string>>({})
  const [reviewing, setReviewing] = useState(false)
  const [wrote, setWrote] = useState(false)
  // The removal click that was refused, so its outcome is STATED rather than
  // inferred from the absence of a change: a refused click with no feedback is inert.
  const [removalRefused, setRemovalRefused] = useState('')

  const registry = useQuery({
    queryKey: QK.registry,
    queryFn: () => specEngineApi.configRegistry(),
    retry: false,
    // Bundled vocabulary: a projection of the engine's own constants, so no write
    // can move it. The same key every other form reads, so this costs no request.
    staleTime: Infinity,
  })
  const workflow = useQuery({
    queryKey: QK.workflow(project),
    queryFn: () => specEngineApi.configWorkflow(project || undefined),
    retry: false,
  })

  const write = useMutation({
    mutationFn: (patch: Document) => specEngineApi.writeConfig(patch),
    onSuccess: () => {
      edits.clear()
      setDraftName('')
      setDraftStages({})
      setReviewing(false)
      setWrote(true)
      // The reply's merged document is NOT adopted: the reads are this pane's
      // authority on what is persisted. The workflow key sits under the config
      // prefix, so invalidating the document refreshes it — named anyway, because a
      // reader should not have to know the key layout to see that it refreshes.
      void client.invalidateQueries({ queryKey: QK.config })
      void client.invalidateQueries({ queryKey: QK_RESOLVED_ROOT })
    },
    // No `onError`: a refusal must leave the staged edits in place and the queries
    // untouched, so the rows keep showing the store's own state.
  })

  const bundledPresets = registry.data?.workflow_presets ?? NO_NAMES
  const state: WorkflowState | undefined = workflow.isError ? undefined : workflow.data
  const userPresets = state?.user_presets ?? NO_NAMES
  const rows = useMemo(() => (state ? workflowStageRows(state) : []), [state])
  const declaredStages = useMemo(() => rows.map((row) => row.stage), [rows])

  const trimmed = draftName.trim()
  const refusal = presetNameRefusal(trimmed, bundledPresets, userPresets)
  // The stages the draft actually declares, parsed from what was typed. Blank lines
  // and blank stages drop out: the write door refuses an empty command, and a
  // preset entry must declare at least one stage.
  const draftCommands = useMemo(() => {
    const found: Array<{ stage: string; commands: string[][] }> = []
    for (const stage of declaredStages) {
      const commands = parseCommandBlock(draftStages[stage] ?? '')
      if (commands.length > 0) found.push({ stage, commands })
    }
    return found
  }, [declaredStages, draftStages])

  // The staged edits hold ONLY the preset selection and a preset removal. The
  // definition edits are DERIVED from the draft below rather than accumulated,
  // because the name is part of every path a definition writes: accumulating them
  // would mean a rename had to move every staged stage, and a refused name would
  // have no path to hold a draft at. Deriving makes the draft and the patch one
  // value by construction.
  const definitionEdits = useMemo<StagedEdit[]>(() => {
    if (trimmed === '' || refusal !== '') return []
    return draftCommands.map(({ stage, commands }) => ({
      segments: presetStageSegments(trimmed, stage),
      value: commands,
    }))
  }, [trimmed, refusal, draftCommands])

  // A derived definition path cannot overlap a staged one, so no reconciliation is
  // owed between the two lists: a selection is `workflow.preset` (or a project's),
  // a removal is `workflow.presets.<name>`, and a definition is
  // `workflow.presets.<name>.stages.<stage>` for a name `presetNameRefusal` has
  // already confirmed is NOT among the defined presets — which is the only set a
  // removal can name.

  // A staged removal whose preset has left the document is dropped: it would
  // resurrect nothing, and no sentence on the card would describe it. That
  // departure can arrive from this form, from the document editor, or on any
  // refetch, so it reconciles against the current answer rather than trusting the
  // document to hold still between a click and its confirm.
  const { reconcile } = edits
  useEffect(() => {
    reconcile(
      (edit) =>
        edit.value !== DELETE ||
        edit.segments.length !== 3 ||
        userPresets.includes(edit.segments[2]),
    )
  }, [userPresets, reconcile])

  // `isError` before the data, this pane's rule: React Query keeps the last
  // successful body across a failing refetch, so a workflow rendered from a
  // retained answer would state which preset is in force on a read that did not
  // happen.
  if (registry.isError || workflow.isError) {
    return (
      <div className="se-blk">
        {/* What it HOLDS, not what it can review: with no read the form cannot say
            what a staged edit means, and a badge that dropped to zero here would
            report unwritten work as gone. */}
        <PendingCount
          count={edits.edits.length + definitionEdits.length}
          onCount={onPendingCount}
        />
        <h3>{i18nT('apps.specEngine.workflowForm.delivery_workflow')}</h3>
        {registry.isError && (
          <Refused
            title={i18nT('apps.specEngine.workflowForm.could_not_read_the_workflow_vocabulary')}
            error={registry.error}
          />
        )}
        {workflow.isError && (
          <Refused
            title={i18nT('apps.specEngine.workflowForm.could_not_read_the_delivery_workflow')}
            error={workflow.error}
          />
        )}
      </div>
    )
  }
  if (registry.isPending || workflow.isPending || !registry.data || !workflow.data) {
    return (
      <div className="se-blk">
        <PendingCount
          count={edits.edits.length + definitionEdits.length}
          onCount={onPendingCount}
        />
        <h3>{i18nT('apps.specEngine.workflowForm.delivery_workflow')}</h3>
        <p className="se-note">
          {i18nT('apps.specEngine.workflowForm.reading_the_delivery_workflow')}
        </p>
      </div>
    )
  }

  const selection = workflow.data.preset
  const selectionSegments = presetSelectionSegments(project)
  const stagedSelection = edits.stagedAt(selectionSegments)
  const chosen = stagedSelection ? String(stagedSelection.value ?? '') : (selection?.name ?? '')

  // Plain functions rather than `useCallback`: each closes over the mutation
  // object, which React Query hands back fresh on every render, so a memo here
  // would advertise a stability it cannot have.
  const touched = () => {
    setWrote(false)
    setRemovalRefused('')
    write.reset()
  }

  const choosePreset = (name: string) => {
    touched()
    // Selecting back exactly what this path already stores is not a change, and
    // every write is recorded: staging it would put a line in the durable write
    // record for an edit nobody made.
    if (nodeAt(document, selectionSegments) === name) edits.unstage(selectionSegments)
    else edits.stage(selectionSegments, name)
  }

  const removePreset = (name: string) => {
    touched()
    // Refused, and not by a silent disable: the operator has to know WHAT selects
    // the preset, because pointing those selections elsewhere is the action that
    // unblocks the removal.
    if (projectsSelectingPreset(document, name).length > 0 || appSelectsPreset(document, name)) {
      setRemovalRefused(name)
      return
    }
    edits.stage(presetSegments(name), DELETE)
  }

  const editStage = (stage: string, text: string) => {
    touched()
    setDraftStages((current) => ({ ...current, [stage]: text }))
  }

  const discard = () => {
    edits.clear()
    setDraftName('')
    setDraftStages({})
    setReviewing(false)
    setWrote(false)
    write.reset()
  }

  /**
   * One staged edit as the review card reads it, or `null` when this form cannot
   * say what the edit means.
   *
   * The patch is built from exactly the edits this returns a sentence for, so the
   * card can never show a line it cannot explain and a write can never carry one.
   */
  const describe = (edit: StagedEdit): ReviewedChange | null => {
    const path = dotted(edit.segments)
    if (edit.segments.length === 2) {
      return {
        path,
        sentence: i18nT('apps.specEngine.workflowForm.edit_selects_the_preset', {
          preset: String(edit.value ?? ''),
          path,
        }),
      }
    }
    if (edit.segments.length === 4) {
      return {
        path,
        sentence: i18nT('apps.specEngine.workflowForm.edit_selects_the_preset_for_the_project', {
          preset: String(edit.value ?? ''),
          project: edit.segments[1],
          path,
        }),
      }
    }
    if (edit.segments.length === 3 && edit.value === DELETE) {
      return {
        path,
        sentence: i18nT('apps.specEngine.workflowForm.edit_removes_the_preset', {
          preset: edit.segments[2],
          path,
        }),
      }
    }
    if (edit.segments.length === 5) {
      return {
        path,
        sentence: i18nT('apps.specEngine.workflowForm.edit_defines_the_stage_commands', {
          preset: edit.segments[2],
          stage: edit.segments[4],
          count: fmtNumber(Array.isArray(edit.value) ? edit.value.length : 0),
          path,
        }),
      }
    }
    return null
  }

  const reviewed: Array<{ change: ReviewedChange; edit: StagedEdit }> = []
  for (const edit of [...edits.edits, ...definitionEdits]) {
    const change = describe(edit)
    if (change) reviewed.push({ change, edit })
  }
  const patch = buildFormPatch(reviewed.map((entry) => entry.edit))
  // Anything that puts commands into force authorises them to run: a definition
  // spells the argv, and a selection puts a whole preset's stages into force. Both
  // are declared to the card by kind, so this grant and the same grant made from
  // any other form read as one act.
  const authorising = reviewed.filter(
    ({ edit }) => edit.value !== DELETE && edit.segments.length !== 3,
  )

  return (
    <div className="se-blk">
      {/* The same number the "unwritten workflow changes" line below states, read
          from the same list, so the badge cannot claim a count this form does not
          show. */}
      <PendingCount count={reviewed.length} onCount={onPendingCount} />
      <h3>{i18nT('apps.specEngine.workflowForm.delivery_workflow')}</h3>
      <p className="se-note">
        {project
          ? i18nT('apps.specEngine.workflowForm.the_workflow_in_force_for_project', { project })
          : i18nT('apps.specEngine.workflowForm.the_workflow_in_force_app_wide')}
      </p>
      {selection === null ? (
        <p className="se-note">{i18nT('apps.specEngine.workflowForm.no_preset_is_selected')}</p>
      ) : (
        <p className="se-note" data-selected-preset={selection.name}>
          {selection.bundled
            ? i18nT('apps.specEngine.workflowForm.a_bundled_preset_is_selected', {
                preset: selection.name,
              })
            : i18nT('apps.specEngine.workflowForm.a_preset_you_defined_is_selected', {
                preset: selection.name,
              })}
          {selection.declared_at !== '' && (
            <span className="se-kv-path">{selection.declared_at}</span>
          )}
        </p>
      )}

      <h3>{i18nT('apps.specEngine.workflowForm.presets_bundled_with_the_app')}</h3>
      {bundledPresets.length === 0 ? (
        <p className="se-note">{i18nT('apps.specEngine.workflowForm.no_preset_is_bundled')}</p>
      ) : (
        <PresetChoices
          label={i18nT('apps.specEngine.workflowForm.presets_bundled_with_the_app')}
          names={bundledPresets}
          selected={chosen}
          onChoose={choosePreset}
        />
      )}
      <h3>{i18nT('apps.specEngine.workflowForm.presets_you_defined')}</h3>
      {userPresets.length === 0 ? (
        <p className="se-note">
          {i18nT('apps.specEngine.workflowForm.no_preset_of_your_own_is_defined')}
        </p>
      ) : (
        <>
          <PresetChoices
            label={i18nT('apps.specEngine.workflowForm.presets_you_defined')}
            names={userPresets}
            selected={chosen}
            onChoose={choosePreset}
          />
          <div className="se-acts">
            {userPresets.map((name) => (
              <button
                key={name}
                type="button"
                className="se-btn se-sm se-danger"
                // The preset is named in the accessible label because the visible
                // label is one word: a bare "Remove" is how somebody removes the
                // wrong one.
                aria-label={i18nT('apps.specEngine.workflowForm.remove_the_preset', {
                  preset: name,
                })}
                onClick={() => removePreset(name)}
              >
                {i18nT('apps.specEngine.configPanel.remove')}
              </button>
            ))}
          </div>
        </>
      )}
      {removalRefused !== '' && (
        <div className="se-arm">
          {projectsSelectingPreset(document, removalRefused).length > 0 && (
            <p>
              {i18nT('apps.specEngine.workflowForm.removal_is_refused_projects_select_it', {
                preset: removalRefused,
                projects: projectsSelectingPreset(document, removalRefused).join(SEP),
              })}
            </p>
          )}
          {appSelectsPreset(document, removalRefused) && (
            <p>
              {i18nT('apps.specEngine.workflowForm.removal_is_refused_the_app_selects_it', {
                preset: removalRefused,
              })}
            </p>
          )}
        </div>
      )}

      <h3>{i18nT('apps.specEngine.workflowForm.the_stages_this_workflow_runs')}</h3>
      <p className="se-note">
        {i18nT('apps.specEngine.workflowForm.the_engine_runs_these_stages_in_a_fixed_order')}
      </p>
      {rows.length === 0 ? (
        <p className="se-note">{i18nT('apps.specEngine.workflowForm.no_stage_is_declared')}</p>
      ) : (
        <div className="se-settings">
          {rows.map((row) => (
            <StageRow key={row.stage} row={row} />
          ))}
        </div>
      )}

      <h3>{i18nT('apps.specEngine.workflowForm.define_a_workflow_preset')}</h3>
      <p className="se-note">
        {i18nT('apps.specEngine.workflowForm.these_commands_will_be_executed')}
      </p>
      <p className="se-note">
        {i18nT('apps.specEngine.workflowForm.a_definition_applies_to_every_project')}
      </p>
      <div className="se-setting">
        <label className="se-setting-name" htmlFor="se-workflow-preset-name">
          {i18nT('apps.specEngine.workflowForm.the_preset_name')}
        </label>
        <input
          id="se-workflow-preset-name"
          type="text"
          className="se-input"
          value={draftName}
          onChange={(event) => {
            touched()
            setDraftName(event.target.value)
          }}
        />
        {refusal === 'reserved' && (
          <p className="se-note" role="alert">
            {i18nT('apps.specEngine.workflowForm.the_name_is_reserved_for_a_bundled_preset', {
              name: trimmed,
            })}
          </p>
        )}
        {refusal === 'taken' && (
          <p className="se-note" role="alert">
            {i18nT('apps.specEngine.workflowForm.a_preset_of_that_name_is_already_defined', {
              name: trimmed,
            })}
          </p>
        )}
      </div>
      <div className="se-settings">
        {rows.map((row) => (
          <div className="se-setting" key={row.stage} data-draft-stage={row.stage}>
            <label className="se-setting-name" htmlFor={`se-workflow-draft-${row.stage}`}>
              {i18nT('apps.specEngine.workflowForm.commands_for_stage', { stage: row.stage })}
            </label>
            <textarea
              id={`se-workflow-draft-${row.stage}`}
              className="se-input"
              rows={2}
              value={draftStages[row.stage] ?? ''}
              onChange={(event) => editStage(row.stage, event.target.value)}
            />
            <p className="se-note">
              {i18nT('apps.specEngine.workflowForm.one_command_per_line')}
            </p>
          </div>
        ))}
      </div>

      <div className="se-acts" style={{ marginTop: 9 }}>
        <button
          type="button"
          className="se-btn"
          disabled={reviewed.length === 0}
          onClick={() => setReviewing(true)}
        >
          {i18nT('apps.specEngine.workflowForm.review_the_exact_change')}
        </button>
        {reviewed.length > 0 && (
          <span className="se-lbl">
            {i18nT('apps.specEngine.workflowForm.unwritten_workflow_changes')}
            {SEP}
            <span className="se-m">{fmtNumber(reviewed.length)}</span>
          </span>
        )}
      </div>
      {reviewing && reviewed.length > 0 && (
        <FormReview
          changes={reviewed.map((entry) => entry.change)}
          patch={patch}
          labels={{
            heading: i18nT('apps.specEngine.workflowForm.the_change_that_would_be_written'),
            confirm: i18nT('apps.specEngine.workflowForm.write_the_change'),
            writing: i18nT('apps.specEngine.configPanel.saving'),
            discard: i18nT('apps.specEngine.workflowForm.discard_the_pending_changes'),
            exactly: i18nT('apps.specEngine.workflowForm.a_confirm_writes_exactly_this_patch'),
            refusalTitle: i18nT('apps.specEngine.workflowForm.could_not_write_the_workflow_change'),
            retained: i18nT(
              'apps.specEngine.workflowForm.nothing_was_written_so_the_stages_are_stored_state',
            ),
          }}
          authorises={authorising.map(({ edit }) => ({
            kind: 'commands_run' as const,
            path: dotted(edit.segments),
            // The subject sentence: which path puts commands into force. The card
            // adds what authorising commands to run MEANS.
            sentence: i18nT('apps.specEngine.workflowForm.this_puts_commands_into_force', {
              path: dotted(edit.segments),
            }),
          }))}
          writing={write.isPending}
          error={write.isError ? write.error : null}
          onConfirm={(sending) => write.mutate(sending)}
          onDiscard={discard}
        />
      )}
      {wrote && (
        <p className="se-note" role="status">
          {i18nT('apps.specEngine.workflowForm.wrote_the_change_and_re_read_the_workflow')}
        </p>
      )}
      <p className="se-note">
        {i18nT('apps.specEngine.workflowForm.the_source_of_each_stage_is_the_engines_answer')}
      </p>
    </div>
  )
}
