/**
 * The configuration pane's stage switcher: the tab list and the panel wrapper.
 *
 * Replaces the schema-shaped `SectionTabs`/`SectionPanel` pair — Settings, Cost
 * profiles, Watch sources, JSON view — with one area per pipeline stage. The
 * stages themselves come off the wire (`stages.resolveStages`); this module is
 * only the switch and it decides no placement.
 *
 * ## What this preserves, and why each is load-bearing rather than polish
 *
 * Every one of these was earned by an earlier round of this pane and re-derived
 * here deliberately:
 *
 * 1. **Panels stay mounted and hide with `hidden`.** {@link StagePanel} never
 *    conditionally renders. Unmounting would drop everything a stage's forms hold
 *    that is not yet written: `useStagedEdits` staging, an armed removal and the
 *    name typed to confirm it, a half-written add, the scope each row targets, and
 *    the document draft. An operator who switched stages to check one number and
 *    came back to an emptied form would have lost work with nothing saying so.
 * 2. **Hidden work is announced.** Each stage carries the count of unwritten
 *    changes its own surfaces hold, whichever stage is showing, so confirming a
 *    patch can never happen on a pane that shows no sign of edits staged one stage
 *    over. The counts are the surfaces' own, reported out by `PendingCount`.
 * 3. **One tab stop for the whole list, with focus following selection.** Arrows
 *    move within the list; a stop per stage would put five stops between the
 *    projects table and the panel.
 * 4. **Labels are unresolved catalog keys until render.** A module-level `i18nT()`
 *    would freeze the language at import.
 *
 * ## What it adds
 *
 * `Home` and `End`, alongside the wrapping `ArrowLeft`/`ArrowRight` the previous
 * shell had. The WAI-ARIA tabs pattern expects all four, and rebuilding the shell
 * is the moment to stop reproducing the omission rather than carrying it into a
 * fifth tab.
 *
 * Visually it stays the pane's flat filter-pill idiom, in flow: no overlay, no
 * popup, nothing positioned over the surfaces it switches between, because the
 * pane's layout holds only because it contains none of those.
 */
import { fmtNumber } from '../../i18n/format'
import { i18nT } from '../../i18n/t'
import { ADVANCED_STAGE, stageLabel, stagePending, type ResolvedStage } from './stages'

/** The DOM id of *stage*'s control, so its panel can name what labels it. */
export function stageControlId(stage: string): string {
  return `se-stage-${stage}`
}

/** The DOM id of *stage*'s panel, so its control can name what it controls. */
export function stagePanelId(stage: string): string {
  return `se-stage-panel-${stage}`
}

/**
 * The stage tab list.
 *
 * `pending` is the pane's whole per-surface record rather than a per-stage total,
 * so the badge and the pane-level count are two readings of ONE table: a
 * pre-summed prop would be a second place a count lives and a second thing that
 * can disagree with the patch.
 *
 * The document editor's marks — an unsaved draft, and the engine's problem and
 * advisory counts for the saved document — ride on the advanced stage because that
 * is where this pane puts the editor. They are stated on the tab because they are
 * only RENDERED inside the editor: a pane that withheld "this document has three
 * problems" behind an unvisited area would read as a healthy configuration.
 */
export function StageTabs({
  stages,
  active,
  pending,
  dirty,
  problems,
  advisories,
  onActivate,
}: {
  stages: readonly ResolvedStage[]
  active: string
  /** Unwritten changes per `<stage>/<surface>` key, reported by the surfaces. */
  pending: Readonly<Record<string, number>>
  /** Whether the document editor is holding an unsaved draft. */
  dirty: boolean
  problems: number
  advisories: number
  onActivate: (stage: string) => void
}) {
  const move = (to: number) => {
    // Wrapping, per the tabs pattern: the ends of the list are not walls, and an
    // arrow press that does nothing reads as a dead key.
    const next = ((to % stages.length) + stages.length) % stages.length
    onActivate(stages[next].id)
    // Focus follows selection, so the keys move the reader as well as the panel.
    // Read from the document rather than held in a ref: the control being moved to
    // may not have rendered as selected yet.
    document.getElementById(stageControlId(stages[next].id))?.focus()
  }
  return (
    <div
      className="se-filters se-tabs"
      role="tablist"
      aria-label={i18nT('apps.specEngine.configPanel.configuration_stages')}
    >
      {stages.map((stage, index) => {
        const count = stagePending(pending, stage.id)
        const holdsDocument = stage.id === ADVANCED_STAGE
        return (
          <button
            key={stage.id}
            id={stageControlId(stage.id)}
            type="button"
            role="tab"
            className="se-filter"
            aria-selected={stage.id === active}
            aria-controls={stagePanelId(stage.id)}
            // One tab stop for the whole list: the arrows move between stages, so
            // a stop per stage would put five stops between the table and the
            // panel.
            tabIndex={stage.id === active ? 0 : -1}
            onClick={() => onActivate(stage.id)}
            onKeyDown={(event) => {
              if (event.key === 'ArrowRight') {
                event.preventDefault()
                move(index + 1)
              } else if (event.key === 'ArrowLeft') {
                event.preventDefault()
                move(index - 1)
              } else if (event.key === 'Home') {
                event.preventDefault()
                move(0)
              } else if (event.key === 'End') {
                event.preventDefault()
                move(stages.length - 1)
              }
            }}
          >
            {stageLabel(stage.id)}
            {count > 0 && <span className="se-filter-count">{fmtNumber(count)}</span>}
            {holdsDocument && dirty && (
              <span className="se-filter-mark">
                {i18nT('apps.specEngine.configPanel.unsaved_edits')}
              </span>
            )}
            {holdsDocument && problems > 0 && (
              <span className="se-filter-mark">
                {i18nT('apps.specEngine.configPanel.problems')}
                <span className="se-filter-count">{fmtNumber(problems)}</span>
              </span>
            )}
            {holdsDocument && advisories > 0 && (
              <span className="se-filter-mark">
                {i18nT('apps.specEngine.configPanel.advisories')}
                <span className="se-filter-count">{fmtNumber(advisories)}</span>
              </span>
            )}
          </button>
        )
      })}
    </div>
  )
}

/**
 * One stage's surfaces, mounted whether or not the stage is the active one.
 *
 * `hidden` rather than a conditional render, and that is the whole point of this
 * wrapper — see the first preserved property in this module's own note.
 */
export function StagePanel({
  stage,
  active,
  children,
}: {
  stage: string
  active: string
  children: React.ReactNode
}) {
  return (
    <div
      id={stagePanelId(stage)}
      role="tabpanel"
      aria-labelledby={stageControlId(stage)}
      hidden={stage !== active}
    >
      {children}
    </div>
  )
}
