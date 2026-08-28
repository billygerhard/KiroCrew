/**
 * The delivery workflow's rows, and the argv text a definition is typed as.
 *
 * Three jobs, all pure, all deliberately kept out of the form module:
 *
 * 1. **Turning `GET /config/workflow` into rows to render.** This is where the
 *    guarantee lives that the pane derives NO precedence of its own. The route
 *    answers which layer supplied each stage's commands and what those commands
 *    resolved to; every field here is carried across untouched. Nothing in this
 *    module names a layer, compares a `declared_at` against a project path, or
 *    reads `bundled` to decide what a stage's source is — so there is no second
 *    implementation of `DeliveryWorkflow`'s layering to drift from the engine's.
 *    A test asserts that on the SOURCE of this file, because an absence is not
 *    otherwise observable: a re-derivation that happened to agree with the engine
 *    on today's documents would pass every behavioural test and name the wrong
 *    layer on the first day the two disagreed.
 * 2. **Parsing one typed command line.** The engine runs argv with no shell, so an
 *    argument is not split by anything the argument itself contains; an operator
 *    still has to be able to type an argument that holds a space. The quoting the
 *    parser honours is the minimum that makes that expressible, and it is the same
 *    shape a rendered command delimits an otherwise-invisible argument in.
 * 3. **Naming the paths a write lands at, and reading the preset names a write uses
 *    as path segments out of the document** rather than off the route, whose names
 *    are capped for display.
 *
 * The WORDS for a stage, a run point and a source all live in the form, because
 * they are catalog keys and a module-level `i18nT()` would freeze the language at
 * import. Keeping them there is also what keeps the no-layer-literal assertion
 * above meaningful: a label table naming all five sources is presentation, and a
 * branch on one of those names inside a row builder is a second resolver.
 */
import type { StageOrigin, WorkflowState } from './api'
import { PROJECTS, isObject, nodeAt, type Document } from './configDocument'

/** The `workflow` section. The engine's `SECTION_WORKFLOW`. */
export const WORKFLOW = 'workflow'

/** The key holding the selected preset's name. The engine's `WORKFLOW_PRESET_KEY`. */
export const WORKFLOW_PRESET = 'preset'

/** The key holding per-stage command overrides. The engine's `WORKFLOW_STAGES_KEY`. */
export const WORKFLOW_STAGES = 'stages'

/** The key holding user-defined preset definitions. The engine's `WORKFLOW_PRESETS_KEY`. */
export const WORKFLOW_PRESETS = 'presets'

/**
 * One delivery stage as the pane renders it.
 *
 * `source`, `preset`, `declaredAt` and `runsAt` are the payload's own answers,
 * relabelled and not recomputed. The two derived fields are derived from the
 * COMMANDS rather than from any layer:
 *
 * - `inert` is "this stage runs nothing", read off the resolved commands. It is
 *   not the payload's `skipped`, which answers the narrower question of whether
 *   anything DECLARES the stage: a declaration resolving to an empty command list
 *   also runs nothing, and an operator reading the row needs the wider answer.
 * - `inDeliveryFlow` is membership of the flow's own stage list, which the route
 *   projects so this side never encodes that teardown runs at archive.
 */
export interface WorkflowStageRow {
  stage: string
  /** Which layer supplied the commands, verbatim from the route. Never derived. */
  source: string
  /** The preset that supplied them, empty when no preset did. Verbatim. */
  preset: string
  /** Dotted path of the declaration, empty when nothing declares the stage. */
  declaredAt: string
  /**
   * The resolved commands, one argv array each, byte-equal to the payload's.
   *
   * From `argv` and never from `commands`, which is a COUNT: a row rendered from
   * the count would show the right NUMBER of commands and none of their text.
   */
  commands: readonly (readonly string[])[]
  /** Whether this stage runs nothing at all. */
  inert: boolean
  /** Where in a run the stage executes, verbatim from the route. */
  runsAt: string
  /** Whether the delivery flow itself runs this stage. */
  inDeliveryFlow: boolean
}

/**
 * One row per stage the route declared, in the order it declared them.
 *
 * Total over the payload and order-preserving: the engine runs the stages in a
 * fixed order it alone owns, so a sort, a filter or a reorder here would be this
 * side inventing a sequence. That is also why nothing in the pane offers a
 * reorder control — there is no such thing to offer.
 */
export function workflowStageRows(state: WorkflowState): WorkflowStageRow[] {
  const flow = state.delivery_flow_stages ?? []
  return (state.stages ?? []).map((origin) => stageRow(origin, flow))
}

function stageRow(origin: StageOrigin, flow: readonly string[]): WorkflowStageRow {
  // Copied one level down as well, so a row cannot hand a caller an array the
  // payload still holds — a form that mutated a rendered argv in place would
  // change what the byte-equality claim is made against.
  const commands = (origin.argv ?? []).map((command) => [...command])
  return {
    stage: origin.stage,
    source: origin.source,
    preset: origin.preset,
    declaredAt: origin.declared_at,
    commands,
    inert: commands.length === 0,
    runsAt: origin.runs_at,
    inDeliveryFlow: flow.includes(origin.stage),
  }
}

/** The path the selected preset is written at, for the app or for one project. */
export function presetSelectionSegments(project: string): string[] {
  return project === ''
    ? [WORKFLOW, WORKFLOW_PRESET]
    : [PROJECTS, project, WORKFLOW, WORKFLOW_PRESET]
}

/**
 * The path one preset definition is written at.
 *
 * App-level with no project variant, because `_check_workflow` admits definitions
 * app-wide alone: a project naming `presets` is refused as an unknown workflow
 * field, so a per-project spelling of this path could only produce a refusal.
 */
export function presetSegments(preset: string): string[] {
  return [WORKFLOW, WORKFLOW_PRESETS, preset]
}

/**
 * The user-defined preset names the DOCUMENT holds, in declaration order.
 *
 * The route projects the same set, but it renders each name through `sanitized` at
 * the display cap, so a name longer than that arrives as `<cap chars> [...]` — a
 * string no document holds. That is fine for a label and wrong for everything else
 * this form does with a name: it is a path segment in every write, and the value
 * `workflow.preset` and `projects.*.workflow.preset` are compared against to
 * decide whether a removal would strand a selection. Reading them here from the
 * one document the rest of the form already resolves against keeps the name that
 * is displayed and the name that is written from being able to disagree — the rule
 * the gate form states for the same reason: a control must show what it writes.
 *
 * Declaration order rather than sorted, matching the route, so a chooser does not
 * reorder itself when a definition is added.
 */
export function documentPresetNames(document: Document): string[] {
  const node = nodeAt(document, [WORKFLOW, WORKFLOW_PRESETS])
  return isObject(node) ? Object.keys(node) : []
}

/**
 * The path one stage's commands inside a definition are written at.
 *
 * PER STAGE, and that is the point rather than a detail. A patch replacing the
 * whole `workflow` object — or the whole `projects` object — would carry the
 * fenced `workflow` / `projects.*.workflow` write inside an ancestor the
 * confirmation card's fence matcher looks for a literal key at, so the operator
 * would confirm a fenced write with nothing flagging it. Every path this form
 * emits addresses one stage of one preset, so the fenced key is always literally
 * present in the patch.
 */
export function presetStageSegments(preset: string, stage: string): string[] {
  return [...presetSegments(preset), WORKFLOW_STAGES, stage]
}

/**
 * Whether one argument has to be delimited for a reader to see where it begins.
 *
 * An argument holding whitespace — or holding nothing at all — is invisible beside
 * its neighbours in a rendered command line, and the engine spawns argv with NO
 * shell, so nothing downstream would split or rejoin it. The delimiters are how a
 * display keeps the boundary an operator has to see; the argument's own bytes are
 * carried untouched beside them.
 */
export function needsDelimiting(argument: string): boolean {
  return argument === '' || /\s/.test(argument)
}

/**
 * One typed line as argv, splitting on whitespace outside quotes.
 *
 * Quote-aware because the engine spawns argv with NO shell: nothing downstream
 * will split or join these again, so an argument holding a space has to be
 * expressible here or it is not expressible at all. Single and double quotes both
 * group; a backslash escapes the next character inside double quotes and outside
 * them. That is also the shape the resolved commands are DISPLAYED in — an argument
 * {@link needsDelimiting} is shown between quotes — so the text an operator copies
 * off a stage row parses back to the argv it was rendered from.
 *
 * An unterminated quote yields the argument it opened rather than an error: this
 * runs on every keystroke, and refusing a half-typed line would make the field
 * unusable while it is being typed.
 */
export function parseCommandLine(line: string): string[] {
  const argv: string[] = []
  let current = ''
  let open = false
  let quote = ''
  for (let index = 0; index < line.length; index += 1) {
    const character = line[index]
    if (character === '\\' && quote !== "'" && index + 1 < line.length) {
      current += line[index + 1]
      open = true
      index += 1
      continue
    }
    if (quote !== '') {
      if (character === quote) quote = ''
      else current += character
      continue
    }
    if (character === '"' || character === "'") {
      quote = character
      // An empty quoted argument is still an argument, so the run is marked open
      // before any character lands in it.
      open = true
      continue
    }
    if (/\s/.test(character)) {
      if (open) argv.push(current)
      current = ''
      open = false
      continue
    }
    current += character
    open = true
  }
  if (open) argv.push(current)
  return argv
}

/**
 * The argv a block of typed lines describes, blank lines dropped.
 *
 * Blank lines are dropped rather than becoming empty commands because a trailing
 * newline is how a textarea reads after every line an operator finishes typing,
 * and the write door refuses an empty command.
 */
export function parseCommandBlock(text: string): string[][] {
  return text
    .split('\n')
    .map((line) => parseCommandLine(line))
    .filter((argv) => argv.length > 0)
}
