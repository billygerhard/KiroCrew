/**
 * The setup flow: inspect, answer, review the plan, approve and apply.
 *
 * Built to `design/mockup-b.html`'s setup pane — a four-step rail beside the body,
 * and the four steps are the four backend calls, not a wizard's decoration. It is
 * the first-run landing pane (`Requirement 5.4`: an unconfigured engine is offered
 * the assistant rather than an empty form), and it drives
 * `/setup/inspect`, `/setup/plan` and `/setup/apply` rather than reimplementing any
 * part of them.
 *
 * ## What the flow guarantees, and which part of the UI carries it
 *
 * **Nothing is written before step 4.** Inspection and planning are separate routes
 * from the apply for exactly that reason, and the note on the rail says so. A reader
 * can check it: the two earlier steps have no button that writes.
 *
 * **Step 4 refuses without a named approver.** The engine refuses an empty approver
 * with `approver-required` and writes nothing, and this panel disables Apply until
 * the field holds something — the field is the primary gate a human sees, and the
 * refusal is the one that holds when something else calls the route. Both, because
 * the disabled button is not a guarantee (it is a rendering) and the refusal alone
 * would let an operator press a button that cannot work.
 *
 * **The approver is not the session.** The route takes both: the session is who
 * acted (the operator guard verified it) and the approver is who authorized the plan,
 * recorded in the store's durable write record. An operator may apply a plan a
 * colleague approved, so the field is typed rather than filled in from the login.
 *
 * **A plan is a claim about its inputs, not a token.** `plan_id` is a content hash
 * over the project subject, the answers used and the patch they produce; the apply
 * recomputes it and refuses a mismatch. So editing ANY answer discards the plan on
 * screen: sending the old id with new answers is precisely the stale apply the
 * engine refuses, and a panel that kept the id would turn a correct refusal into a
 * dead-end the operator cannot read. The refusal branch stays as the backstop for
 * the case the panel cannot see — the project's own evidence changing on disk
 * between the plan and the apply.
 *
 * ## What is asked, and what is deliberately not answerable here
 *
 * `cost_profile` and the three autonomy rungs above authoring are asked and never
 * inferred: they decide how much unattended work may spend and how far it may go.
 * A missing rung is UNANSWERED rather than "no", which is why each rung renders three
 * states and not a checkbox — a checkbox has no spelling for "not answered".
 *
 * Preset choices are offered only from the OFFERS the inspection returned. The engine
 * refuses a preset that was never offered (and therefore never had its prerequisites
 * checked), so a free-text preset field would build a request that cannot succeed.
 *
 * The `tooling` question has no answer field because `SetupAnswers` has no tooling
 * member: the engine asks it so a human knows nothing was inferred, and the commands
 * are configured later in the document. Stated in the UI rather than silently
 * dropped.
 *
 * ## Orientation, and why it is only on the first run
 *
 * An operator who has never seen this engine needs three facts before a path
 * field means anything: what the engine does, what finishing this flow produces,
 * and which step to press first. Those are stated at the top of the pane while no
 * project is configured, and each step carries a line saying what the operator
 * DOES there and what they GET back — the guard-rail sentences beside them say
 * what the flow refuses, which is a different question. Once a project exists the
 * orientation is gone: the operator returning to add a second project has read it,
 * and repeating it would push the field they came for below the fold.
 *
 * Whether this is the first run is decided ONCE, by the page, and handed down. A
 * second derivation here could disagree with the one that routes the landing pane
 * and orders the rail, and both readings would look correct on their own.
 *
 * A step the flow cannot reach yet says which step must complete first, in both
 * states: an operator looking at a disabled-looking step needs the blocker named,
 * not a greyed row to interpret.
 *
 * ## Layout rules this file must not break
 *
 * No drawer, no modal, no scrim — see `styles.ts`. The evidence excerpts are
 * outside-authored prose (a steering note, a vendored CI file), so they render
 * through the bounded untrusted block the review queue uses: same threat, same
 * treatment, one implementation.
 *
 * The directory picker is the ONE portal this pane opens, and it is the dashboard's
 * shared one rather than a second implementation of browsing. It is an anchored
 * popover with no scrim and no focus trap. Anchor placement alone does NOT keep it
 * off the safety strip — on a short viewport the picker's downward layout runs to
 * within a few pixels of the viewport bottom — so the pane passes the picker a
 * reserved bottom band (`STRIP_CLEARANCE_PX`) that its DOWNWARD layout never
 * extends into; a popover that cannot fit above the band flips upward instead. A
 * flipped popover ends above its anchor, and the anchor sits in the pane's work
 * area above the strip, which is what keeps that branch clear. The safety strip
 * therefore stays visible, focusable and clickable while the picker is open.
 */
import { useCallback, useMemo, useRef, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, FolderOpen } from 'lucide-react'

import { i18nT } from '../../i18n/t'
import { fmtNumber } from '../../i18n/format'
import ProjectPicker from '../../components/ProjectPicker'
import {
  QK,
  QK_RESOLVED_ROOT,
  SETUP_REFUSAL,
  SpecEngineApiError,
  specEngineApi,
  type SetupAnswers,
  type SetupApplied,
  type SetupInspection,
  type SetupOffer,
  type SetupPlanEnvelope,
} from './api'
import { Advisories } from './ConfigPanel'
import { UntrustedText } from './ReviewQueuePanel'

/** Separator between two identifiers on one line. Punctuation, not copy. */
const SEP = ' \u00b7 '

/** The kinds of preset an offer can be, as the engine's own section names. */
const WORKFLOW_KIND = 'workflow'
const SOURCES_KIND = 'sources'

/** The subject the engine asks about but takes no answer for. */
const TOOLING_SUBJECT = 'tooling'

/**
 * The step the orientation names as the first action, as its own binding so the
 * orientation and the rail cannot name different steps: `SETUP_STEPS` is built
 * from it below.
 */
const FIRST_STEP_KEY = 'apps.specEngine.specEnginePage.step_inspect_the_project'

/** The remaining steps' keys, named so every map below keys off the step's
 *  IDENTITY rather than its rail position — reordering `SETUP_STEPS` then
 *  cannot silently attach one step's gating condition to another's row. */
const ANSWER_STEP_KEY = 'apps.specEngine.specEnginePage.step_answer_what_could_not_be_inferred'
const REVIEW_STEP_KEY = 'apps.specEngine.specEnginePage.step_review_the_plan'
const APPROVE_STEP_KEY = 'apps.specEngine.specEnginePage.step_approve_and_apply'

/**
 * Viewport pixels above the bottom edge the directory picker may never cover:
 * the safety strip's row (~34px) plus breathing room. Handed to the picker as
 * `reservedBottom`, which subtracts it from the downward layout's available
 * space BEFORE the flip decision — the bound the pane's docstring relies on.
 */
const STRIP_CLEARANCE_PX = 48

/** The four steps, in the order the flow walks them. Shared with the page's rail. */
export const SETUP_STEPS: readonly string[] = [
  FIRST_STEP_KEY,
  ANSWER_STEP_KEY,
  REVIEW_STEP_KEY,
  APPROVE_STEP_KEY,
]

/**
 * What the operator does at each step, and what they get back — one entry per step,
 * keyed by the step's own label key.
 *
 * A map of whole literal keys rather than a parallel array, because the
 * key-reference gate resolves a non-literal index into a module-level object
 * literal by unioning its values but cannot see through an array index: an array
 * would exempt all four descriptions from every check that the key exists, and a
 * missing key renders as its own dotted path in the UI rather than failing.
 */
const STEP_DESCRIPTION_KEY: Record<string, string> = {
  'apps.specEngine.specEnginePage.step_inspect_the_project':
    'apps.specEngine.setupFlowPanel.step_desc_inspect',
  'apps.specEngine.specEnginePage.step_answer_what_could_not_be_inferred':
    'apps.specEngine.setupFlowPanel.step_desc_answer',
  'apps.specEngine.specEnginePage.step_review_the_plan':
    'apps.specEngine.setupFlowPanel.step_desc_review',
  'apps.specEngine.specEnginePage.step_approve_and_apply':
    'apps.specEngine.setupFlowPanel.step_desc_approve',
}

/**
 * The step each step waits on: a blocked step's key to the key of the step that
 * ACTUALLY gates it, matching the conditions on the steps' own controls.
 *
 * Answering and reviewing both wait on the inspection alone — the plan can be
 * computed with unanswered questions left at their defaults, so naming the
 * answer step here would state a blocker the enabled plan button beside it
 * contradicts. Approve waits on the plan being on screen, which is what its
 * own control requires. Declared as whole literal keys rather than computed
 * from `SETUP_STEPS` by index, for the key-gate resolvability reason the map
 * above records — and the first step is absent because nothing precedes it.
 */
const STEP_BLOCKER_KEY: Record<string, string> = {
  'apps.specEngine.specEnginePage.step_answer_what_could_not_be_inferred': FIRST_STEP_KEY,
  'apps.specEngine.specEnginePage.step_review_the_plan': FIRST_STEP_KEY,
  'apps.specEngine.specEnginePage.step_approve_and_apply':
    'apps.specEngine.specEnginePage.step_review_the_plan',
}

/**
 * A rung's answer. `undefined` is a state, not a missing value: the engine refuses an
 * apply while any rung is unanswered, so "not answered" has to be representable.
 */
type Rung = boolean | undefined

/** What the engine refused, in words the operator can act on. */
const REFUSAL_KEY: Record<string, string> = {
  [SETUP_REFUSAL.approverRequired]: 'apps.specEngine.setupFlowPanel.refused_approver_required',
  [SETUP_REFUSAL.planStale]: 'apps.specEngine.setupFlowPanel.refused_plan_stale',
  [SETUP_REFUSAL.approvalRequired]: 'apps.specEngine.setupFlowPanel.refused_approval_required',
  [SETUP_REFUSAL.inferredSubject]: 'apps.specEngine.setupFlowPanel.refused_inferred_subject',
}

/** The engine's refusal code behind an error, or `''` when it is not a refusal. */
function refusedCode(error: unknown): string {
  return error instanceof SpecEngineApiError ? error.refused : ''
}

/**
 * A refusal, with the sentence for the specific refusal when there is one.
 *
 * Every setup refusal shares one status and one `code`, so the branch is on
 * `refused` — and an unrecognised one still renders its message and its code rather
 * than being swallowed, because the engine may refuse for a reason this build does
 * not know about.
 */
function Refused({ error }: { error: unknown }) {
  const refused = refusedCode(error)
  const sentence = REFUSAL_KEY[refused]
  const code = error instanceof SpecEngineApiError ? error.code : ''
  const text = error instanceof Error ? error.message : ''
  return (
    <div className="se-refusal" role="alert">
      {sentence
        ? i18nT(sentence)
        : i18nT('apps.specEngine.setupFlowPanel.the_assistant_refused')}
      {/* Stated on every refusal, because it is the fact that decides what to do
          next and the one an operator most often doubts. */}
      <span className="se-note">
        {i18nT('apps.specEngine.setupFlowPanel.nothing_was_written')}
      </span>
      <code>{[refused || code, text].filter(Boolean).join(SEP)}</code>
    </div>
  )
}

/** One offered preset: what it is, and the programs it would run. */
function Offer({
  offer,
  chosen,
  onChoose,
}: {
  offer: SetupOffer
  chosen: boolean
  onChoose: () => void
}) {
  const unmet = offer.prerequisites.unmet.length
  return (
    <div className="se-offer">
      <button type="button" className="se-btn se-sm" aria-pressed={chosen} onClick={onChoose}>
        {offer.name}
      </button>
      {/* The programs, from the same bundled tables the write copies from: what is
          approved here is what would land in configuration. */}
      <span className="se-note">
        {i18nT('apps.specEngine.setupFlowPanel.programs_it_would_run')}
        {SEP}
        <span className="se-m">{offer.programs.join(SEP)}</span>
      </span>
      {unmet > 0 && (
        <span className="se-flag" data-flag="unmet">
          {i18nT('apps.specEngine.setupFlowPanel.prerequisites_unmet')}
          {SEP}
          {fmtNumber(unmet)}
        </span>
      )}
      {offer.copy_note && <span className="se-note">{offer.copy_note}</span>}
    </div>
  )
}

/**
 * The evidence behind one inference, and the operator's approval of it.
 *
 * An inference the operator has not approved is not written — approving nothing
 * writes only the answers — so the toggle is the whole mechanism and not a
 * formality. The excerpt is outside-authored prose and is bounded as such.
 */
function InferenceRow({
  subject,
  value,
  rationale,
  evidence,
  approved,
  onToggle,
}: {
  subject: string
  value: string
  rationale: string
  evidence: Array<{ located_at: string; excerpt: string }>
  approved: boolean
  onToggle: () => void
}) {
  return (
    <div className="se-evid-row" data-approved={approved}>
      <span className="se-subj se-m">{subject}</span>
      <span>
        <span className="se-m">{value}</span>
        <span className="se-note">{SEP}{rationale}</span>
        {evidence.map((item) => (
          <span key={`${item.located_at}:${item.excerpt}`} className="se-evid-item">
            <span className="se-m se-note">{item.located_at}</span>
            <UntrustedText text={item.excerpt} />
          </span>
        ))}
      </span>
      <button type="button" className="se-btn se-sm" aria-pressed={approved} onClick={onToggle}>
        {approved
          ? i18nT('apps.specEngine.setupFlowPanel.approved')
          : i18nT('apps.specEngine.setupFlowPanel.approve')}
      </button>
    </div>
  )
}

/**
 * The panel. One flow, four steps, and no state that outlives the project it is for.
 *
 * `firstRun` is the page's single derivation, not a second reading of the
 * configuration: it decides only what this pane SAYS, never what it can do.
 */
export function SetupFlowPanel({ firstRun }: { firstRun: boolean }) {
  const client = useQueryClient()
  const [project, setProject] = useState('')
  const [approver, setApprover] = useState('')
  const [profile, setProfile] = useState('')
  const [rungs, setRungs] = useState<Record<string, Rung>>({})
  const [approved, setApproved] = useState<string[]>([])
  const [workflow, setWorkflow] = useState<string | null>(null)
  const [source, setSource] = useState<string | null>(null)
  const [plan, setPlan] = useState<SetupPlanEnvelope | null>(null)
  const [applied, setApplied] = useState<SetupApplied | null>(null)
  const [pickerOpen, setPickerOpen] = useState(false)
  const [browseFailed, setBrowseFailed] = useState(false)
  const browseRef = useRef<HTMLButtonElement>(null)

  /**
   * Discard the plan on screen.
   *
   * Called from every answer change. The plan identifies the answers it was computed
   * from, so keeping it after an edit would send an id the server recomputes to
   * something else — a refusal the operator could do nothing about, in place of a
   * plan button they can press.
   */
  const invalidatePlan = useCallback(() => {
    setPlan(null)
    setApplied(null)
  }, [])

  /**
   * Open the shared directory picker with a clean failure slate.
   *
   * The pane performs no directory read of its own: the picker reports the
   * outcome of every read it makes through `onBrowseResult` (a fence-allowlisted
   * addition to the shared component), so a failed drill-in states itself
   * exactly like a failed first read, and there is no second probe whose result
   * could disagree with the list actually on screen. The statement is beside the
   * field, and the field is never touched from here — typing the path stays the
   * fallback whether the read worked or not.
   */
  const openPicker = useCallback(() => {
    // The failure statement resets on open and is then driven by the picker's
    // own reads through `onBrowseResult` — every read it makes (initial,
    // drill-in, parent), not a separate probe that could disagree with the list
    // actually on screen.
    setBrowseFailed(false)
    setPickerOpen(true)
  }, [])

  const inspect = useMutation({
    mutationFn: (path: string) => specEngineApi.setupInspect({ project: path }),
    onSuccess: () => {
      // Answers are reset to the new project's, never carried across: an approval is
      // an approval of THIS project's inference, and a rung confirmed for one project
      // is not a rung confirmed for another.
      setProfile('')
      setRungs({})
      setApproved([])
      setWorkflow(null)
      setSource(null)
      invalidatePlan()
    },
  })

  const inspection: SetupInspection | undefined = inspect.data

  const answers: SetupAnswers = useMemo(
    () => ({
      cost_profile: profile,
      // Only the rungs actually answered travel. An unanswered rung must be ABSENT
      // rather than false: the engine refuses on absence and would silently accept a
      // false as a declined grant.
      confirmations: Object.fromEntries(
        Object.entries(rungs).filter(([, value]) => value !== undefined),
      ) as Record<string, boolean>,
      approved_subjects: approved,
      workflow_preset: workflow,
      watch_source: source,
    }),
    [approved, profile, rungs, source, workflow],
  )

  const computePlan = useMutation({
    mutationFn: () => specEngineApi.setupPlan({ project, answers }),
    onSuccess: (envelope) => setPlan(envelope),
  })

  const apply = useMutation({
    mutationFn: () =>
      specEngineApi.setupApply({
        project,
        answers,
        plan_id: plan?.plan_id ?? '',
        approver: approver.trim(),
      }),
    onSuccess: (result) => {
      setApplied(result)
      // The page's first-run detection and the config pane both read these, and the
      // apply is exactly the moment "nothing is configured" stops being true.
      void client.invalidateQueries({ queryKey: QK.config })
      void client.invalidateQueries({ queryKey: QK_RESOLVED_ROOT })
    },
  })

  const questions = inspection?.questions ?? []
  const profileOptions = questions.find((question) => question.subject === 'cost_profile')?.options
  const levels = inspection?.confirmed_levels ?? []
  const offers = inspection?.offers ?? []
  const workflowOffers = offers.filter((offer) => offer.kind === WORKFLOW_KIND)
  const sourceOffers = offers.filter((offer) => offer.kind === SOURCES_KIND)
  const unanswered = levels.filter((level) => rungs[level] === undefined).length

  // The step the flow is on, derived from what has actually happened rather than
  // tracked: a counter and the real state disagree the first time a call fails.
  const step = !inspection ? 0 : plan === null ? 1 : applied ? 4 : 3
  const canApply = plan !== null && approver.trim() !== '' && !apply.isPending

  // Whether a step is genuinely unreachable, from the SAME state that gates its
  // own controls — never from rail position. `index > step` claimed the review
  // step was blocked on answering while the enabled plan button beside it
  // required no answers: a rail statement an adjacent control contradicts is a
  // false statement in the exact mechanism built to prevent grey-row mystery.
  const stepBlocked: Record<string, boolean> = {
    [FIRST_STEP_KEY]: false,
    [ANSWER_STEP_KEY]: !inspection,
    [REVIEW_STEP_KEY]: !inspection,
    [APPROVE_STEP_KEY]: plan === null,
  }

  return (
    <>
      <aside
        className="se-steps"
        aria-label={i18nT('apps.specEngine.specEnginePage.setup_progress')}
      >
        <h2>{i18nT('apps.specEngine.specEnginePage.setup_assistant')}</h2>
        {SETUP_STEPS.map((key, index) => (
          <div
            key={key}
            className="se-step"
            data-state={index < step ? 'done' : index === step ? 'now' : 'todo'}
          >
            <span className="se-dot" aria-hidden="true">
              {fmtNumber(index + 1)}
            </span>
            <span>
              {i18nT(key)}
              {/* What the operator does here and gets back. Part of the
                  orientation, so it leaves with it: a returning operator already
                  knows the four steps and needs the pane's controls, not its
                  tutorial. */}
              {firstRun && (
                <span className="se-note" data-step-desc="true">
                  {i18nT(STEP_DESCRIPTION_KEY[key])}
                </span>
              )}
              {/* A step the flow cannot reach names its blocker rather than
                  rendering as a grey row with no reason. Driven by the same
                  conditions that gate the step's own controls, so the rail can
                  never claim a blocker an enabled button contradicts — and it is
                  interpolated, because a sentence assembled from a fragment plus
                  a name cannot be translated. */}
              {stepBlocked[key] && (
                <span className="se-note" data-step-blocked="true">
                  {i18nT('apps.specEngine.setupFlowPanel.blocked_until', {
                    step: i18nT(STEP_BLOCKER_KEY[key]),
                  })}
                </span>
              )}
            </span>
          </div>
        ))}
        <p className="se-keys">
          {i18nT('apps.specEngine.setupFlowPanel.nothing_is_written_until_step_four')}
        </p>
      </aside>

      <section className="se-setup-body">
        <h1>{i18nT('apps.specEngine.specEnginePage.nothing_is_configured_yet')}</h1>
        <p className="se-setup-lead">{i18nT('apps.specEngine.specEnginePage.setup_lead')}</p>

        {/* Orientation: the three facts a first-time reader needs before a path
            field means anything — what the engine does, what finishing this
            produces, and which step to press first. Absent once a project exists,
            so the pane a returning operator opens starts at the field. */}
        {firstRun && (
          <section
            className="se-orient"
            aria-label={i18nT('apps.specEngine.setupFlowPanel.orientation_label')}
          >
            <p>{i18nT('apps.specEngine.setupFlowPanel.orientation_engine')}</p>
            <p>{i18nT('apps.specEngine.setupFlowPanel.orientation_produces')}</p>
            <p className="se-orient-lead">
              {i18nT('apps.specEngine.setupFlowPanel.orientation_first_action', {
                step: i18nT(FIRST_STEP_KEY),
              })}
            </p>
          </section>
        )}

        {/* Step 1 */}
        <div className="se-qbox">
          <h3>{i18nT('apps.specEngine.specEnginePage.step_inspect_the_project')}</h3>
          <p className="se-idfield">
            <label htmlFor="se-setup-project">
              {i18nT('apps.specEngine.setupFlowPanel.project_path')}
            </label>
            {/* The field and the picker's trigger on one line, and the field stays
                editable: browsing is an alternative to typing a host path from
                memory, never a replacement for it. */}
            <span className="se-pathrow">
              <input
                id="se-setup-project"
                className="se-input se-m"
                value={project}
                onChange={(event) => {
                  setProject(event.target.value)
                  invalidatePlan()
                }}
              />
              <button
                type="button"
                ref={browseRef}
                className="se-btn se-sm"
                onClick={openPicker}
              >
                <FolderOpen className="lucide-inline" aria-hidden="true" />
                {i18nT('apps.specEngine.setupFlowPanel.browse')}
              </button>
            </span>
            <span className="se-note">
              {i18nT('apps.specEngine.setupFlowPanel.the_path_is_read_on_the_gateway_host')}
            </span>
            {browseFailed && (
              <span className="se-note" data-browse-error="true" role="status">
                {i18nT('apps.specEngine.setupFlowPanel.browse_failed')}
              </span>
            )}
          </p>
          {/* The dashboard's picker, reused rather than reimplemented, anchored to
              the button above. Selection fills the field with the absolute path,
              which is an answer change like any other and so discards the plan. */}
          {pickerOpen && (
            <ProjectPicker
              open={true}
              onOpenChange={(open) => {
                if (!open) setPickerOpen(false)
              }}
              anchorRef={browseRef}
              // The popover may never extend into the strip's band at the bottom
              // of the viewport; the picker flips upward when it cannot fit
              // above it. The reservation, not anchor placement, is the bound.
              reservedBottom={STRIP_CLEARANCE_PX}
              // Every read the picker makes reports here, so a failed drill-in
              // states itself exactly like a failed first read.
              onBrowseResult={(ok) => setBrowseFailed(!ok)}
              onSelect={(path) => {
                setProject(path)
                invalidatePlan()
                setPickerOpen(false)
              }}
            />
          )}
          <div className="se-acts">
            <button
              type="button"
              className="se-btn"
              disabled={project.trim() === '' || inspect.isPending}
              onClick={() => inspect.mutate(project.trim())}
            >
              {inspect.isPending
                ? i18nT('apps.specEngine.setupFlowPanel.inspecting')
                : i18nT('apps.specEngine.setupFlowPanel.inspect_the_project')}
            </button>
          </div>
          {inspect.isError && <Refused error={inspect.error} />}
          {inspection && !inspection.memory_consulted && (
            <p className="se-note">
              {i18nT('apps.specEngine.setupFlowPanel.memory_was_not_consulted')}
            </p>
          )}
        </div>

        {inspection && (
          <>
            {/* Step 2 */}
            <div className="se-qbox">
              <h3>
                {i18nT('apps.specEngine.setupFlowPanel.what_the_assistant_read')}
                {SEP}
                <span className="se-m">{inspection.project.name}</span>
              </h3>
              {inspection.inferences.length === 0 ? (
                <p className="se-note">
                  {i18nT('apps.specEngine.setupFlowPanel.nothing_could_be_inferred')}
                </p>
              ) : (
                <div className="se-evid">
                  {inspection.inferences.map((inference) => (
                    <InferenceRow
                      key={inference.subject}
                      subject={inference.subject}
                      value={inference.value}
                      rationale={inference.rationale}
                      evidence={inference.evidence}
                      approved={approved.includes(inference.subject)}
                      onToggle={() => {
                        setApproved((current) =>
                          current.includes(inference.subject)
                            ? current.filter((item) => item !== inference.subject)
                            : [...current, inference.subject],
                        )
                        invalidatePlan()
                      }}
                    />
                  ))}
                </div>
              )}
            </div>

            <div className="se-qbox">
              <h3>{i18nT('apps.specEngine.setupFlowPanel.cost_profile')}</h3>
              <div className="se-acts">
                {(profileOptions ?? []).map((option) => (
                  <button
                    key={option}
                    type="button"
                    className="se-btn se-sm"
                    aria-pressed={profile === option}
                    onClick={() => {
                      setProfile(option)
                      invalidatePlan()
                    }}
                  >
                    {option}
                  </button>
                ))}
              </div>
              <p className="se-note">
                {i18nT('apps.specEngine.setupFlowPanel.the_profile_decides_what_may_be_spent')}
              </p>
            </div>

            <div className="se-qbox">
              <h3>{i18nT('apps.specEngine.setupFlowPanel.autonomy_rungs_you_grant')}</h3>
              {levels.map((level) => (
                <div className="se-rung" key={level}>
                  <span className="se-m">{level}</span>
                  {rungs[level] === undefined && (
                    <span className="se-flag" data-flag="unanswered">
                      {i18nT('apps.specEngine.setupFlowPanel.unanswered')}
                    </span>
                  )}
                  <span className="se-acts">
                    <button
                      type="button"
                      className="se-btn se-sm"
                      aria-pressed={rungs[level] === true}
                      onClick={() => {
                        setRungs((current) => ({ ...current, [level]: true }))
                        invalidatePlan()
                      }}
                    >
                      {i18nT('apps.specEngine.setupFlowPanel.yes')}
                    </button>
                    <button
                      type="button"
                      className="se-btn se-sm"
                      aria-pressed={rungs[level] === false}
                      onClick={() => {
                        setRungs((current) => ({ ...current, [level]: false }))
                        invalidatePlan()
                      }}
                    >
                      {i18nT('apps.specEngine.setupFlowPanel.no')}
                    </button>
                  </span>
                  {/* The prompt for this rung, as the engine words it: each rung
                      grants something different, and a shared sentence would let one
                      answer stand for three. */}
                  <span className="se-note">
                    {
                      questions.find(
                        (question) =>
                          question.subject === `${inspection.autonomy_field}.${level}`,
                      )?.prompt
                    }
                  </span>
                </div>
              ))}
              <p className="se-note">
                {i18nT('apps.specEngine.setupFlowPanel.a_missing_answer_is_unanswered')}
              </p>
            </div>

            {(workflowOffers.length > 0 || sourceOffers.length > 0) && (
              <div className="se-qbox">
                <h3>{i18nT('apps.specEngine.setupFlowPanel.offered_presets')}</h3>
                {workflowOffers.map((offer) => (
                  <Offer
                    key={`${offer.kind}:${offer.name}`}
                    offer={offer}
                    chosen={workflow === offer.name}
                    onChoose={() => {
                      setWorkflow(workflow === offer.name ? null : offer.name)
                      invalidatePlan()
                    }}
                  />
                ))}
                {sourceOffers.map((offer) => (
                  <Offer
                    key={`${offer.kind}:${offer.name}`}
                    offer={offer}
                    chosen={source === offer.name}
                    onChoose={() => {
                      setSource(source === offer.name ? null : offer.name)
                      invalidatePlan()
                    }}
                  />
                ))}
                <p className="se-note">
                  {i18nT('apps.specEngine.setupFlowPanel.only_offered_presets_may_be_written')}
                </p>
              </div>
            )}

            {questions.some((question) => question.subject === TOOLING_SUBJECT) && (
              <p className="se-note">
                {i18nT('apps.specEngine.setupFlowPanel.the_tooling_question_takes_no_answer')}
              </p>
            )}

            {/* Step 3 */}
            <div className="se-qbox">
              <h3>{i18nT('apps.specEngine.specEnginePage.step_review_the_plan')}</h3>
              <div className="se-acts">
                <button
                  type="button"
                  className="se-btn"
                  disabled={computePlan.isPending}
                  onClick={() => computePlan.mutate()}
                >
                  {computePlan.isPending
                    ? i18nT('apps.specEngine.setupFlowPanel.computing_the_plan')
                    : i18nT('apps.specEngine.setupFlowPanel.show_the_exact_patch')}
                </button>
                {unanswered > 0 && (
                  <span className="se-note">
                    {i18nT('apps.specEngine.setupFlowPanel.rungs_still_unanswered')}
                    {SEP}
                    <span className="se-m">{fmtNumber(unanswered)}</span>
                  </span>
                )}
              </div>
              {computePlan.isError && <Refused error={computePlan.error} />}
              {plan ? (
                <>
                  <pre className="se-json">{JSON.stringify(plan.config_patch, null, 2)}</pre>
                  <p className="se-note">
                    {i18nT('apps.specEngine.setupFlowPanel.would_write')}
                    {SEP}
                    <span className="se-m">{plan.written_paths.join(SEP)}</span>
                  </p>
                  {plan.warnings.map((warning) => (
                    <p className="se-note" key={warning}>
                      {warning}
                    </p>
                  ))}
                </>
              ) : (
                <p className="se-note">
                  {i18nT('apps.specEngine.setupFlowPanel.no_plan_has_been_computed_yet')}
                </p>
              )}
            </div>

            {/* Step 4 */}
            <div className="se-qbox">
              <h3>{i18nT('apps.specEngine.specEnginePage.step_approve_and_apply')}</h3>
              <p className="se-idfield">
                <label htmlFor="se-setup-approver">
                  {i18nT('apps.specEngine.setupFlowPanel.approver_identity')}
                </label>
                <input
                  id="se-setup-approver"
                  className="se-input se-m"
                  value={approver}
                  onChange={(event) => setApprover(event.target.value)}
                />
                <span className="se-note">
                  {i18nT('apps.specEngine.setupFlowPanel.the_approver_is_recorded_not_the_session')}
                </span>
              </p>
              <div className="se-acts">
                <button
                  type="button"
                  className="se-btn se-danger"
                  disabled={!canApply}
                  onClick={() => apply.mutate()}
                >
                  {apply.isPending
                    ? i18nT('apps.specEngine.setupFlowPanel.applying')
                    : i18nT('apps.specEngine.setupFlowPanel.apply_the_plan')}
                </button>
                {plan === null && (
                  <span className="se-note">
                    {i18nT('apps.specEngine.setupFlowPanel.compute_a_plan_first')}
                  </span>
                )}
                {plan !== null && approver.trim() === '' && (
                  <span className="se-note">
                    {i18nT('apps.specEngine.setupFlowPanel.an_approver_is_required')}
                  </span>
                )}
              </div>
              {plan && (
                <p className="se-note">
                  <span className="se-m">{plan.plan_id}</span>
                  {SEP}
                  {i18nT('apps.specEngine.setupFlowPanel.recomputed_on_apply')}
                </p>
              )}
              {apply.isError && <Refused error={apply.error} />}
              {applied && (
                <div className="se-torn">
                  <p>
                    <AlertTriangle className="lucide-inline" aria-hidden="true" />
                    {i18nT('apps.specEngine.setupFlowPanel.applied_by', {
                      approver: applied.approver,
                    })}
                  </p>
                  <p className="se-note">
                    {i18nT('apps.specEngine.setupFlowPanel.wrote')}
                    {SEP}
                    <span className="se-m">{applied.written_paths.join(SEP)}</span>
                  </p>
                  {applied.notes.map((note) => (
                    <p className="se-note" key={note}>
                      {note}
                    </p>
                  ))}
                  {/* Advisories from the write, not dropped at it: an apply that armed
                      execution autonomy on a publicly submittable source earns one, and
                      this is where a human can still read it. */}
                  <Advisories advisories={applied.advisories} />
                  {applied.prerequisites.unmet.length > 0 && (
                    <p className="se-note">
                      {i18nT('apps.specEngine.setupFlowPanel.prerequisites_unmet')}
                      {SEP}
                      <span className="se-m">
                        {fmtNumber(applied.prerequisites.unmet.length)}
                      </span>
                    </p>
                  )}
                </div>
              )}
            </div>
          </>
        )}
      </section>
    </>
  )
}
