/**
 * Property 3, in two halves: a rendered command is a stored command, and no
 * precedence is derived in TypeScript.
 *
 * `GET /config/workflow` answers which layer supplied each delivery stage's
 * commands, projected from `preset_display.stage_origins` over the very
 * `DeliveryWorkflow` a run resolves through. The pane's job is to relabel that
 * answer. What must not happen is a second implementation of the layering on this
 * side — because a re-derivation that agreed with the engine on today's documents
 * would pass every behavioural test and name the wrong layer on the first day the
 * two disagreed.
 *
 * ## Why the generator makes the layer fields DISAGREE
 *
 * A payload whose `source`, `preset`, `declared_at` and `bundled` all tell one
 * consistent story cannot falsify anything: a re-derivation would agree with the
 * pass-through on every such case, so the property would be vacuous. So the
 * generator deliberately produces rows where the fields point different ways — a
 * `declared_at` under `projects.…` beside an `app_override`, `bundled: true` beside
 * a `user_preset`, a preset NAME beside a source that did not come from a preset.
 * Every one of those is a payload the pane must render the route's `source` for,
 * and every one of them is a case where reading any other field instead gives a
 * different answer.
 *
 * The same trick is applied to the commands. `commands` is a COUNT from the
 * engine's serializer and `argv` is the resolved commands the route adds beside it;
 * the generator lets the two disagree, so a row built from the count — or from any
 * function of it — cannot pass.
 *
 * ## The structural half
 *
 * The absence of a re-derivation is not observable from behaviour alone, so the
 * second half asserts on the row builder's own SOURCE: none of the five layer names
 * appears in `workflowRows.ts`. That is the same shape of assertion task 2.3's
 * suite makes about the route, and for the same reason. The label table naming all
 * five lives in the FORM, which is presentation — it turns an answer into words and
 * decides nothing.
 *
 * ## The names a write spends
 *
 * A third group covers the other half of "a rendered command is a stored command":
 * the preset names the form uses as PATH SEGMENTS come from the document, not from
 * the route, whose own list of them is rendered through a display cap. A name past
 * that cap arrives as `<cap chars> [...]`, and a write spending that string
 * addresses a path no document holds — which for `workflow.preset` is the selection
 * the engine then refuses to resolve at all.
 *
 * ## The delimiting rule, asserted through what it changes
 *
 * The last group is about `needsDelimiting`, and it deliberately does NOT assert
 * the rule against a restatement of itself: `needsDelimiting(a) === (a === '' ||
 * /\s/.test(a))` passes against every implementation, including a wrong one,
 * because both sides move together. What the rule is FOR is that a rendered command
 * line stays an unambiguous reading of one argv, so that is what is asserted — an
 * argument holding whitespace renders differently from the several arguments its
 * whitespace would otherwise make it look like, and an argv needing no delimiter
 * gets none. Each half falsifies a rule the other cannot: never delimiting collides
 * the first, always delimiting breaks the second.
 */
import { describe, it, expect } from 'vitest'
import * as fc from 'fast-check'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

import type { StageOrigin, WorkflowState } from '../apps/spec-engine/api'
import {
  documentPresetNames,
  needsDelimiting,
  parseCommandBlock,
  parseCommandLine,
  presetStageSegments,
  workflowStageRows,
} from '../apps/spec-engine/workflowRows'

/**
 * One argv as a line an operator could have typed it on.
 *
 * The test's own quoter, not production's: nothing in the pane formats an argv into
 * an editable field, so a formatter there would be code with no caller. This one
 * exists to generate the input side of the parser's obligation — every argv has to
 * be expressible — and it is deliberately maximal, quoting whatever might not
 * survive bare.
 */
function quoted(argv: readonly string[]): string {
  return argv.map((argument) => `"${argument.replace(/([\\"])/g, '\\$1')}"`).join(' ')
}

/** The engine's own delivery stage names, plus one it could grow. */
const STAGE = fc.constantFrom('isolate', 'submit', 'verify', 'publish', 'teardown', 'notify_only')

/** Every source the route can answer, so no row is skipped by the generator. */
const SOURCE = fc.constantFrom(
  'bundled_preset',
  'user_preset',
  'app_override',
  'project_override',
  'unconfigured',
  // A layer the engine could grow. The pane must carry it across rather than
  // mapping it onto the nearest neighbour it recognises.
  'edition_override',
)

/** One argument, including the shapes that would break a naive split or escape. */
const ARGUMENT = fc.oneof(
  fc.constantFrom('git', 'add', '--all', '-m', 'a b', '', 'a"b', "a'b", 'a\\b', '  ', '\t'),
  fc.string({ maxLength: 6 }),
)

/**
 * An argument a single space could not separate from its neighbours.
 *
 * One holding whitespace of its own, or holding nothing at all. These are the two
 * shapes the delimiting rule exists for, so the property below generates them
 * directly rather than filtering them out of {@link ARGUMENT} and spending most of
 * its runs on cases it cannot say anything about.
 */
const INVISIBLE_ARGUMENT = fc.constantFrom(
  '',
  ' ',
  '  ',
  '\t',
  '\n',
  'a b',
  'a  b',
  ' lead',
  'trail ',
  'a\tb',
)

/** An argument a space already separates: non-empty, and holding no whitespace. */
const VISIBLE_ARGUMENT = fc.oneof(
  fc.constantFrom('git', 'add', '--all', '-m', 'a"b', "a'b", 'a\\b', 'HEAD~1'),
  fc.string({ minLength: 1, maxLength: 6 }).filter((argument) => !/\s/.test(argument)),
)

/**
 * One command line the way a stage row renders it.
 *
 * A mirror of `StageCommands`: one space between tokens, an argument
 * {@link needsDelimiting} wrapped in quotes, and nothing escaped INSIDE a token,
 * because a token carries the payload's own bytes and that is what the
 * byte-equality claim above is made against. The form's real rendering is pinned
 * against this same shape by name in `SpecEngineWorkflowForm.test.tsx`
 * (`git commit -m "a b"`), so the mirror cannot drift from the JSX unnoticed.
 */
function renderedLine(argv: readonly string[]): string {
  return argv.map((argument) => (needsDelimiting(argument) ? `"${argument}"` : argument)).join(' ')
}

/**
 * What a reader actually SEES of that line.
 *
 * HTML collapses a run of whitespace, so two renderings that differ only in how
 * much whitespace they contain are one line on screen. Comparing the raw strings
 * would let a delimiting rule that forgot the empty argument pass: `prog  after`
 * and `prog after` are two strings and one indistinguishable command.
 */
function visible(line: string): string {
  return line.replace(/\s+/g, ' ').trim()
}

/** One argv. Non-empty, because the write door refuses an empty command. */
const ARGV = fc.array(ARGUMENT, { minLength: 1, maxLength: 4 })

/**
 * One stage row whose layer fields deliberately DISAGREE with each other.
 *
 * `preset`, `declared_at`, `bundled` and `from_preset` are drawn independently of
 * `source`, so any of the four is a wrong answer for at least some generated rows.
 * The engine's own rows are of course consistent; the point is that this side must
 * not be reading them to reach a conclusion of its own.
 */
const STAGE_ROW: fc.Arbitrary<StageOrigin> = fc
  .record({
    stage: STAGE,
    source: SOURCE,
    preset: fc.constantFrom('', 'git-pull-request', 'local-only', 'my-preset'),
    declared_at: fc.constantFrom(
      '',
      'workflow.stages.submit',
      'projects./src/acme.workflow.stages.submit',
      'workflow.presets.my-preset.stages.submit',
    ),
    bundled: fc.boolean(),
    from_preset: fc.boolean(),
    skipped: fc.boolean(),
    summary: fc.constantFrom('', 'submit: overridden app-wide'),
    // Deliberately unrelated to `argv.length`: a row rendered from the COUNT would
    // show the right number of commands and none of their text.
    commands: fc.integer({ min: 0, max: 9 }),
    argv: fc.array(ARGV, { maxLength: 3 }),
    runs_at: fc.constantFrom('isolation', 'delivery', 'archive', ''),
  })
  .map((row) => ({ ...row, source: row.source as StageOrigin['source'] }))

/** A payload: rows with distinct stage names, and a flow list over some of them. */
const WORKFLOW: fc.Arbitrary<WorkflowState> = fc
  .record({
    stages: fc.array(STAGE_ROW, { minLength: 1, maxLength: 6 }),
    delivery_flow_stages: fc.array(STAGE, { maxLength: 4 }),
  })
  .map(({ stages, delivery_flow_stages }) => {
    const seen = new Set<string>()
    return {
      configured: true,
      project: null,
      preset: null,
      stages: stages.filter((row) => {
        if (seen.has(row.stage)) return false
        seen.add(row.stage)
        return true
      }),
      user_presets: [],
      delivery_flow_stages,
      gates_scope_is_app: true as const,
      gates: [],
      gates_unreadable: false,
      gate_errors: [],
    }
  })

/**
 * The row builder's own source, for the structural half.
 *
 * Read from the working directory rather than through `import.meta.url`, which is
 * not a `file:` URL under the test transform.
 */
const ROW_MODULE = readFileSync(
  resolve(process.cwd(), 'src/apps/spec-engine/workflowRows.ts'),
  'utf8',
)

describe('a rendered delivery command is a command the engine resolved', () => {
  it('carries every argv across byte-for-byte, per stage and in payload order', () => {
    fc.assert(
      fc.property(WORKFLOW, (state) => {
        const rows = workflowStageRows(state)
        // Total and order-preserving: the engine owns the sequence, so a sort or a
        // filter here would be this side inventing one.
        expect(rows.map((row) => row.stage)).toEqual(state.stages.map((row) => row.stage))
        for (const [index, row] of rows.entries()) {
          const origin = state.stages[index]
          // Byte-equal, argument by argument. `toEqual` on nested arrays of strings
          // is exact-string comparison, so a trimmed, re-quoted or re-joined
          // argument fails.
          expect(row.commands).toEqual(origin.argv)
          for (const [position, command] of row.commands.entries()) {
            for (const [slot, argument] of command.entries()) {
              expect(argument).toBe(origin.argv[position][slot])
            }
          }
        }
      }),
      { numRuns: 300 },
    )
  })

  it('never renders the command COUNT as the commands', () => {
    fc.assert(
      fc.property(WORKFLOW, (state) => {
        for (const [index, row] of workflowStageRows(state).entries()) {
          // The count and the resolved commands are independent on the wire, so a
          // row that agreed with the count whenever they disagreed would be reading
          // the count.
          expect(row.commands.length).toBe(state.stages[index].argv.length)
          // And "takes no action" is read off the commands rather than off the
          // payload's `skipped`, which answers the narrower question of whether
          // anything DECLARES the stage.
          expect(row.inert).toBe(state.stages[index].argv.length === 0)
        }
      }),
      { numRuns: 300 },
    )
  })
})

describe('the pane derives no delivery precedence of its own', () => {
  it('carries the route source verbatim, whatever the other layer fields say', () => {
    fc.assert(
      fc.property(WORKFLOW, (state) => {
        for (const [index, row] of workflowStageRows(state).entries()) {
          const origin = state.stages[index]
          // The route's answer, not a reading of `bundled`, `from_preset`,
          // `declared_at` or `preset` — each of which the generator lets point
          // somewhere else.
          expect(row.source).toBe(origin.source)
          expect(row.preset).toBe(origin.preset)
          expect(row.declaredAt).toBe(origin.declared_at)
          expect(row.runsAt).toBe(origin.runs_at)
        }
      }),
      { numRuns: 300 },
    )
  })

  it('reads whether a stage runs in delivery off the projected flow list', () => {
    fc.assert(
      fc.property(WORKFLOW, (state) => {
        for (const row of workflowStageRows(state)) {
          // Never off `runs_at`, and never off a stage-name table kept here: the
          // engine owns which stages the flow runs, and teardown running at archive
          // is exactly the fact a local copy would get wrong after a rename.
          expect(row.inDeliveryFlow).toBe(state.delivery_flow_stages.includes(row.stage))
        }
      }),
      { numRuns: 300 },
    )
  })

  it('names no configuration layer anywhere in the row builder', () => {
    for (const layer of [
      'bundled_preset',
      'user_preset',
      'app_override',
      'project_override',
      'unconfigured',
    ]) {
      expect(
        ROW_MODULE.includes(layer),
        `${layer} is named in workflowRows.ts, which means the pane is deciding ` +
          'for itself which layer supplied a stage rather than carrying the ' +
          "route's answer across",
      ).toBe(false)
    }
  })
})

describe('a typed command expresses every argv the engine can run', () => {
  it('parses an argument holding whitespace, or none at all, back to itself', () => {
    fc.assert(
      fc.property(ARGV, (argv) => {
        // The quoter is the TEST's, and deliberately so: production has no
        // formatter, because nothing pre-fills these fields — the operator types the
        // commands. What production depends on is the other direction, that the
        // parser can express any argv the engine might be asked to run, including
        // the two shapes a whitespace split cannot reach: an argument containing a
        // space and an empty one. A parser that dropped either would make a command
        // untypeable rather than misparse a typed one.
        expect(parseCommandLine(quoted(argv))).toEqual(argv)
      }),
      { numRuns: 500 },
    )
  })

  it('drops a blank line rather than composing a command with no program', () => {
    fc.assert(
      fc.property(fc.array(ARGV, { minLength: 1, maxLength: 4 }), (commands) => {
        // A trailing newline is how a textarea reads after every finished line, and
        // the write door refuses an empty command.
        const text = `${commands.map((argv) => quoted(argv)).join('\n\n')}\n`
        expect(parseCommandBlock(text)).toEqual(commands)
      }),
      { numRuns: 300 },
    )
  })
})

describe('every path a definition writes addresses one stage', () => {
  it('never emits a wholesale workflow or projects replacement', () => {
    fc.assert(
      fc.property(fc.string({ minLength: 1, maxLength: 8 }), STAGE, (preset, stage) => {
        const segments = presetStageSegments(preset, stage)
        // Five segments, the last of them the stage. That is what keeps the fenced
        // `workflow` key literally present in every patch this form composes: the
        // confirmation card's fence matcher looks for the key itself, so a patch
        // replacing an ancestor object would carry the fenced write unflagged.
        expect(segments).toEqual(['workflow', 'presets', preset, 'stages', stage])
      }),
      { numRuns: 200 },
    )
  })
})

describe('a preset name a write spends is the document’s own', () => {
  it('returns the stored keys byte-for-byte, however long, in document order', () => {
    fc.assert(
      fc.property(
        fc.array(
          fc.oneof(
            fc.constantFrom('house-style', 'my-flow'),
            // Past the route's display cap, which is the case the whole thing is
            // about: the route sends `<cap chars> [...]` for this name, and a write
            // spending that string addresses a path no document holds.
            fc.string({ minLength: 65, maxLength: 90 }),
            fc.string({ minLength: 1, maxLength: 8 }),
          ),
          { minLength: 1, maxLength: 4 },
        ),
        (names) => {
          const presets: Record<string, unknown> = {}
          for (const name of names) presets[name] = { stages: { submit: [['true']] } }
          const document = { workflow: { presets } }
          // Object key order IS document order for string keys that are not array
          // indices, and the route projects the same order, so a chooser does not
          // reorder itself when a definition lands.
          expect(documentPresetNames(document)).toEqual(Object.keys(presets))
          for (const name of documentPresetNames(document)) {
            // A name reshaped here would write somewhere else, so each one is a key
            // the document actually holds — and never the display path's rendering
            // of it, which for a long name ends in a truncation notice.
            expect(presets[name]).toBeDefined()
            expect(name.endsWith(' [...]')).toBe(false)
          }
        },
      ),
      { numRuns: 200 },
    )
  })

  it('answers nothing for a document that declares no preset section', () => {
    fc.assert(
      fc.property(
        fc.oneof(
          fc.constant({}),
          fc.constant({ workflow: {} }),
          // A stored shape the form cannot read as a preset map. An empty answer is
          // right here and a THROW would not be: the pane renders this document.
          fc.constant({ workflow: { presets: [] } }),
          fc.constant({ workflow: { presets: 'nonsense' } }),
          fc.constant({ workflow: 7 }),
        ),
        (document) => {
          expect(documentPresetNames(document as Record<string, unknown>)).toEqual([])
        },
      ),
      { numRuns: 50 },
    )
  })
})

describe('a rendered argument that would vanish is delimited', () => {
  it('keeps an argument holding whitespace apart from the arguments it looks like', () => {
    fc.assert(
      fc.property(
        fc.array(ARGUMENT, { maxLength: 2 }),
        INVISIBLE_ARGUMENT,
        fc.array(ARGUMENT, { maxLength: 2 }),
        (before, argument, after) => {
          const one = [...before, argument, ...after]
          // The same characters with whitespace doing the separating instead: the
          // one argument becomes however many its own whitespace splits it into,
          // and none at all when it holds nothing but whitespace. These are two
          // DIFFERENT commands — the engine spawns argv with no shell, so
          // `git commit -m` plus one argument holding `a b` runs differently from
          // the same words as two arguments — and an operator reading the row has
          // no other way to tell which one is configured.
          const many = [
            ...before,
            ...argument.split(/\s+/).filter((part) => part !== ''),
            ...after,
          ]
          expect(visible(renderedLine(one))).not.toBe(visible(renderedLine(many)))
        },
      ),
      { numRuns: 500 },
    )
  })

  it('adds no quotation to a command whose every argument is already visible', () => {
    fc.assert(
      fc.property(fc.array(VISIBLE_ARGUMENT, { minLength: 1, maxLength: 4 }), (argv) => {
        // The other half, and not a symmetry preference: quotes the engine does not
        // apply are a claim about what runs. A rule that delimited everything would
        // keep every command above distinguishable and still show an operator
        // `"git" "add"` for an argv holding neither quote.
        expect(renderedLine(argv)).toBe(argv.join(' '))
      }),
      { numRuns: 300 },
    )
  })
})
