/**
 * The review queue's per-run panel: the docked inspector's body, and the row
 * flags beside it.
 *
 * Built to `design/mockup-b.html`'s verdict pane, with two departures the mockup
 * could not know about. Both are recorded here rather than in a commit message,
 * because a reader of this file needs them:
 *
 * ## 1. The action set is a function of the waiting reason, and lists only what
 *    this surface can actually do
 *
 * The mockup's `ACTIONS` table offers `Approve gate`, `Request changes`,
 * `Cancel run`, `Raise ceiling` and `Resume`. **None of those five has an HTTP
 * route.** `backend/routes.py` registers exactly four queue actions —
 * release-feedback, redispatch, clean-workspace, teardown — so a button for any
 * of the other five would be a control that cannot act. They are stated as
 * absent, with where the capability does live, instead: recording a verdict is
 * `record_approval`/`advance_phase` on the Engine_MCP_Server, and a ceiling is a
 * configuration value.
 *
 * `revision_exhausted` specifically: the mockup offers "raise the revision limit
 * and retry" for the gate. The engine does not support that. `limits.
 * revision_cycle_limit` is declared with app and project scopes only
 * (`engine/config/settings.py`), so raising it raises it for every gate in the
 * project — there is no per-gate ceiling to lift, and a control claiming
 * otherwise would misdescribe what it did. What the engine does have is the
 * un-marking in `_dispatch_revision`: once the limit is raised project-wide, a
 * further revision turn clears the exhausted mark. That is a configuration
 * change plus an authoring dispatch, neither of which this surface owns.
 *
 * ## 2. Three of the four wired actions need an identifier the queue withholds
 *
 * `QueueEntry` carries a COUNT of held comments and not their ids ("A count
 * rather than the ids because this projection is what a surface renders"), and it
 * carries no watched-item generation at all. The handlers refuse without them,
 * correctly: `redispatch`'s docstring records that lifting an unnamed generation
 * would lift whichever one the poller happened to be on.
 *
 * So the identifier is asked for, and each block says where the value comes from
 * (the audit trail, the watch ledger). An identifier field is not the
 * hand-authoring affordance the selection criteria exclude — that was a control
 * for composing spec prose. Pasting an id the engine already minted is the
 * opposite: it is how an operator names which of the engine's own records to act
 * on. The gap is real and belongs to the backend; a route vending held comment
 * ids and item generations would retire both fields.
 *
 * Only teardown closes its own loop: it returns the ids it KEPT, and those ids
 * are exactly what clean-workspace takes.
 *
 * ## Layout rules this file must not break
 *
 * - **Nothing overlays anything.** No drawer, no modal, no scrim — the selected
 *   design passes the "safety controls are never behind navigation" criterion
 *   only because it contains no overlay, and `SpecEngineShell.test.tsx` fails on
 *   a `position:fixed`/`absolute` declaration. The teardown confirmation is
 *   therefore an in-flow arm-then-confirm, not a dialog.
 * - **Untrusted prose goes last.** The findings block holds the only
 *   outside-authored prose in the payload, and it is the LAST block in the pane,
 *   so expanding one cannot displace any control above it. Expanded, it is a
 *   FIXED-height scroll region rather than a `max-height` cap: a cap still grows
 *   with line count until it binds.
 */
import { useEffect, useId, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, ShieldAlert } from 'lucide-react'

import { i18nT } from '../../i18n/t'
import { fmtNumber } from '../../i18n/format'
import {
  QK,
  REFUSAL,
  SpecEngineApiError,
  specEngineApi,
  type QueueEntry,
  type WaitingOn,
} from './api'

/** Separator between two identifiers on one line. Punctuation, not copy. */
const SEP = ' \u00b7 '

/**
 * The waiting reason, widened by the one distinction the enum does not carry.
 *
 * `revision_exhausted` is a boolean beside `waiting_on: review`, and the two
 * together are a different job from `review` alone: no further revision turn will
 * be dispatched, so "send it back" is not among the moves.
 */
type ActReason = WaitingOn | 'exhausted'

export function actReason(entry: QueueEntry): ActReason {
  return entry.waiting_on === 'review' && entry.revision_exhausted
    ? 'exhausted'
    : entry.waiting_on
}

/**
 * The heading and the note for each reason, as keys.
 *
 * Keys rather than resolved strings: a module-level `i18nT()` runs once at import
 * and would freeze this table in whichever language was active then.
 */
const ACT_HEAD_KEY: Record<ActReason, string> = {
  review: 'apps.specEngine.reviewQueuePanel.act_review',
  exhausted: 'apps.specEngine.reviewQueuePanel.act_exhausted',
  budget: 'apps.specEngine.reviewQueuePanel.act_budget',
  stall: 'apps.specEngine.reviewQueuePanel.act_stall',
}

/**
 * Where the move this surface cannot make actually lives.
 *
 * Every one of these is a statement about a capability with no HTTP route, so the
 * text names the transport that does have it. A note saying only "not available"
 * would leave an operator with a run they cannot advance and no next step.
 */
const ACT_NOTE_KEY: Record<ActReason, string> = {
  review: 'apps.specEngine.reviewQueuePanel.act_note_review',
  exhausted: 'apps.specEngine.reviewQueuePanel.act_note_exhausted',
  budget: 'apps.specEngine.reviewQueuePanel.act_note_budget',
  stall: 'apps.specEngine.reviewQueuePanel.act_note_stall',
}

/** The refusal code behind an error, or `''` when it is not one of ours. */
function codeOf(error: unknown): string {
  return error instanceof SpecEngineApiError ? error.code : ''
}

/** A refusal block: the sentence a reader acts on, with the code underneath. */
function Refused({ title, error }: { title: string; error: unknown }) {
  const code = codeOf(error)
  const text = error instanceof Error ? error.message : ''
  return (
    <div className="se-refusal" role="alert">
      {title}
      <code>{code ? `${code}${SEP}${text}` : text}</code>
    </div>
  )
}

/**
 * Outside-authored prose, bounded so its line count cannot move a control.
 *
 * The engine already put this string through the display contract — prose through
 * `Untrusted.for_display`, identifier-shaped fields through `sanitized` — so it
 * is text and not markup by the time it arrives. React interpolation escapes it a
 * second time on the way into the DOM, which is why this renders `{text}` as a
 * child and never `dangerouslySetInnerHTML`: that attribute is the single way to
 * lose the guarantee, and its absence here is the guarantee.
 *
 * Collapsed, it is line-clamped. Expanded, it is a FIXED-height scroll region:
 * the difference matters because a `max-height` cap still grows with the content
 * until it binds, so a four-line comment and a forty-line one lay out differently
 * and the second one moves whatever sits below.
 */
export function UntrustedText({ text }: { text: string }) {
  const [open, setOpen] = useState(false)
  return (
    <div className="se-untrusted" data-open={open ? 'true' : 'false'}>
      <span className="se-untrusted-tag">
        <ShieldAlert className="lucide-inline" aria-hidden="true" />
        {i18nT('apps.specEngine.reviewQueuePanel.external_unverified_not_markup')}
      </span>
      <p className="se-untrusted-body">{text}</p>
      {/* Countless: the label does not name a line count, so it needs no plural
          form in any catalog and cannot disagree with the text it reveals. */}
      <button type="button" className="se-untrusted-more" onClick={() => setOpen(!open)}>
        {i18nT(
          open
            ? 'apps.specEngine.reviewQueuePanel.collapse'
            : 'apps.specEngine.reviewQueuePanel.show_the_whole_text',
        )}
      </button>
    </div>
  )
}

/**
 * One stored finding, as the engine wrote it.
 *
 * Every field is read defensively rather than typed as present: this is a
 * provider-authored body persisted as an opaque mapping, and a provider that
 * omits `message` must render as a finding with no prose rather than as the
 * string "undefined".
 */
function findingText(finding: Record<string, unknown>, key: string): string {
  const value = finding[key]
  return typeof value === 'string' ? value : ''
}

function findingRefs(finding: Record<string, unknown>): string[] {
  const refs = finding.refs
  return Array.isArray(refs) ? refs.filter((ref): ref is string => typeof ref === 'string') : []
}

/**
 * The findings under review, grouped by the criterion each concerns.
 *
 * The engine's own grouping and the engine's own order — keyed criteria in the
 * report's order, the unkeyed group last — relayed rather than re-sorted. A
 * second ordering of one analysis would drift from the report a reviewer read
 * beside it.
 *
 * This is the LAST block in the pane because it carries the only untrusted prose
 * in the payload. See the layout rule in this file's header.
 */
function FindingsBlock({ entry }: { entry: QueueEntry }) {
  const groups = entry.analysis
  const total = groups.reduce((sum, group) => sum + group.findings.length, 0)
  return (
    <div className="se-blk">
      <h3>
        {i18nT('apps.specEngine.reviewQueuePanel.findings')}
        {SEP}
        {fmtNumber(total)}
      </h3>
      {groups.length === 0 ? (
        /* An empty analysis and an analysis that found nothing both arrive empty
           here; the audit trail is where the two are told apart, so this says
           "none recorded" rather than "no problems found". */
        <p className="se-note">
          {i18nT('apps.specEngine.reviewQueuePanel.no_findings_are_recorded_for_this_run')}
        </p>
      ) : (
        <>
          <ul className="se-findings">
            {groups.map((group, groupIndex) =>
              group.findings.map((finding, findingIndex) => {
                const severity = findingText(finding, 'severity')
                const kind = findingText(finding, 'kind')
                const message = findingText(finding, 'message')
                const refs = findingRefs(finding)
                return (
                  <li key={`${groupIndex}:${findingIndex}`}>
                    <span className="se-fc" data-keyed={group.keyed ? 'true' : 'false'}>
                      {/* The criterion identifier is the engine's, sanitized before it
                          was stored. `unkeyed` is this surface's word for the group
                          whose references resolved to no declared criterion. */}
                      {group.criterion ??
                        i18nT('apps.specEngine.reviewQueuePanel.unkeyed')}
                    </span>
                    {/* Severity and kind are engine and provider identifiers, not
                        copy: translating them would rename a value an operator
                        greps the audit log for. */}
                    {severity && <em className="se-sev">{severity}</em>}
                    {kind && <span className="se-fkind">{kind}</span>}
                    {refs.length > 0 && <span className="se-fkind">{refs.join(SEP)}</span>}
                    {message && <UntrustedText text={message} />}
                  </li>
                )
              }),
            )}
          </ul>
          <p className="se-note">
            {i18nT('apps.specEngine.reviewQueuePanel.unkeyed_findings_are_grouped_note')}
          </p>
        </>
      )}
      {/* The document bodies the mockup tabbed through are NOT in this payload and
          no route serves them. Said once, here, rather than drawn as empty tabs. */}
      <p className="se-note">
        {i18nT('apps.specEngine.reviewQueuePanel.document_bodies_have_no_route_note')}
      </p>
    </div>
  )
}

/**
 * An identifier this surface must ask for, because the queue projection omits it.
 *
 * Shared by the two blocks in that position so both state the same thing the same
 * way, and so a reader comparing them can see the omission is one decision rather
 * than two coincidences.
 */
function IdField({
  label,
  hint,
  value,
  onChange,
  inputMode,
}: {
  label: string
  hint: string
  value: string
  onChange: (next: string) => void
  inputMode?: 'numeric'
}) {
  const id = useId()
  return (
    <p className="se-idfield">
      <label htmlFor={id}>{label}</label>
      <input
        id={id}
        className="se-input se-m"
        value={value}
        inputMode={inputMode}
        onChange={(event) => onChange(event.target.value)}
      />
      <span className="se-note">{hint}</span>
    </p>
  )
}

/**
 * Held reviewer comments, and the release that hands one to the watcher.
 *
 * The comment TEXT is never here. The engine holds it deliberately, and this
 * block must not become a second place it is copied to — releasing hands it to
 * the watcher, which then decides whether a fix turn is dispatched.
 */
function HeldBlock({ entry }: { entry: QueueEntry }) {
  const client = useQueryClient()
  const [commentId, setCommentId] = useState('')
  const release = useMutation({
    mutationFn: () =>
      specEngineApi.releaseFeedback({
        project: entry.project,
        spec: entry.spec,
        run_id: entry.run_id,
        comment_id: commentId.trim(),
      }),
    onSuccess: () => {
      setCommentId('')
      void client.invalidateQueries({ queryKey: QK.queue })
    },
  })

  // A different run is selected, so a half-typed id and a result about the
  // previous run must not survive into it.
  useEffect(() => {
    setCommentId('')
    release.reset()
    // The reset is keyed to the run, not to the mutation object's identity.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [entry.run_id])

  const held = entry.feedback_quarantined
  return (
    <div className="se-blk">
      <h3>
        {i18nT('apps.specEngine.reviewQueuePanel.held_for_a_person')}
        {SEP}
        {fmtNumber(held)}
      </h3>
      {held === 0 && !entry.feedback_needs_human ? (
        <p className="se-note">
          {i18nT('apps.specEngine.reviewQueuePanel.no_comments_are_held_for_this_run')}
        </p>
      ) : (
        <>
          {held > 0 && (
            <div className="se-held">
              <div>
                <strong>
                  {i18nT('apps.specEngine.reviewQueuePanel.comments_are_held_for_release')}
                </strong>
                <p className="se-note">
                  {i18nT('apps.specEngine.reviewQueuePanel.releasing_hands_it_to_the_watcher')}
                </p>
              </div>
            </div>
          )}
          {/* A different loop from `revision_exhausted`, so it is stated
              separately: a reviewer acting on one is not acting on the other. */}
          {entry.feedback_needs_human && (
            <div className="se-held">
              <div>
                <strong>
                  {i18nT('apps.specEngine.reviewQueuePanel.a_feedback_bound_parked_this_run')}
                </strong>
              </div>
            </div>
          )}
          <IdField
            label={i18nT('apps.specEngine.reviewQueuePanel.comment_identifier')}
            hint={i18nT('apps.specEngine.reviewQueuePanel.the_queue_carries_a_count_not_the_ids')}
            value={commentId}
            onChange={setCommentId}
          />
          <div className="se-acts">
            <button
              type="button"
              className="se-btn"
              disabled={commentId.trim() === '' || release.isPending}
              onClick={() => release.mutate()}
            >
              {i18nT('apps.specEngine.reviewQueuePanel.release_the_comment')}
            </button>
          </div>
          {release.isError && (
            <Refused
              // A 409 from the engine and a 503 from the store are different
              // answers: the first is a rule (this run's machine records the
              // release nowhere), the second is a failure that may succeed on a
              // retry. Presenting both as "could not release" would tell an
              // operator to retry something that will refuse again forever.
              title={i18nT(
                codeOf(release.error) === REFUSAL.releaseRefused
                  ? 'apps.specEngine.reviewQueuePanel.the_engine_refused_the_release'
                  : 'apps.specEngine.reviewQueuePanel.the_release_failed',
              )}
              error={release.error}
            />
          )}
          {release.isSuccess && (
            <p className="se-note" data-released={release.data.released ? 'true' : 'false'}>
              {i18nT(
                release.data.released
                  ? 'apps.specEngine.reviewQueuePanel.the_comment_was_released'
                  : 'apps.specEngine.reviewQueuePanel.nobody_held_that_comment',
              )}
            </p>
          )}
        </>
      )}
    </div>
  )
}

/**
 * Lift the suppression on the watched item this run came from.
 *
 * Offered only when the row names a source and an item: a run nothing watched has
 * nothing to re-offer, and a control that refuses on every click is worse than an
 * absent one.
 */
function RedispatchBlock({ entry }: { entry: QueueEntry }) {
  const client = useQueryClient()
  const [generation, setGeneration] = useState('')
  const redispatch = useMutation({
    mutationFn: () =>
      specEngineApi.redispatch({
        source: entry.source ?? '',
        item_id: entry.item_id ?? '',
        // Parsed here so the handler receives a number and not a numeric string;
        // its `_whole` reader refuses anything else, which would surface as
        // `field_required` naming a field the operator did fill in.
        generation: Number.parseInt(generation, 10),
      }),
    onSuccess: () => void client.invalidateQueries({ queryKey: QK.queue }),
  })

  useEffect(() => {
    setGeneration('')
    redispatch.reset()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [entry.run_id])

  if (!entry.source || !entry.item_id) return null
  const parsed = Number.parseInt(generation, 10)
  const usable = Number.isSafeInteger(parsed) && parsed >= 0

  return (
    <div className="se-blk">
      <h3>{i18nT('apps.specEngine.reviewQueuePanel.redispatch')}</h3>
      <dl className="se-kv">
        <dt>{i18nT('apps.specEngine.reviewQueuePanel.source')}</dt>
        <dd>{entry.source}</dd>
        <dt>{i18nT('apps.specEngine.reviewQueuePanel.item')}</dt>
        <dd>{entry.item_id}</dd>
      </dl>
      <IdField
        label={i18nT('apps.specEngine.reviewQueuePanel.generation')}
        hint={i18nT('apps.specEngine.reviewQueuePanel.the_generation_is_not_in_this_projection')}
        value={generation}
        onChange={setGeneration}
        inputMode="numeric"
      />
      <div className="se-acts">
        <button
          type="button"
          className="se-btn"
          disabled={!usable || redispatch.isPending}
          onClick={() => redispatch.mutate()}
        >
          {i18nT('apps.specEngine.reviewQueuePanel.lift_the_suppression')}
        </button>
      </div>
      {redispatch.isError && (
        <Refused
          title={i18nT('apps.specEngine.reviewQueuePanel.the_redispatch_failed')}
          error={redispatch.error}
        />
      )}
      {redispatch.isSuccess && (
        <p className="se-note">
          {i18nT(
            redispatch.data.lifted
              ? 'apps.specEngine.reviewQueuePanel.the_suppression_was_lifted'
              : 'apps.specEngine.reviewQueuePanel.nothing_was_suppressed',
          )}
        </p>
      )}
    </div>
  )
}

/**
 * Teardown, and the kept ids it is not allowed to hide.
 *
 * The requirement this block exists to satisfy: a teardown that keeps workspaces
 * surfaces the kept ids and does NOT report itself complete. `ok: true` is not
 * completion — the handler answers `ok: true, complete: false` with the kept ids
 * in `kept`, and a caller reading only `ok` would report a standing workspace as
 * torn down. So `complete` is the field read, and the kept ids are rendered as a
 * list with a per-id retry rather than summarised as a count.
 *
 * Armed before it fires, in flow. Tearing down the workspaces of a run that is
 * waiting for a verdict destroys the checkout under review, and every run in this
 * queue is waiting for something, so a single click is the wrong cost. The
 * confirmation is a sibling element and not a dialog: an overlay would violate
 * the no-overlay rule the whole layout rests on.
 */
function TeardownBlock({
  entry,
  onKept,
}: {
  entry: QueueEntry
  onKept: (runId: string, kept: number[]) => void
}) {
  const client = useQueryClient()
  const [armed, setArmed] = useState(false)
  const teardown = useMutation({
    mutationFn: () => specEngineApi.teardown({ run_id: entry.run_id }),
    onSuccess: (result) => {
      setArmed(false)
      onKept(entry.run_id, result.kept)
      void client.invalidateQueries({ queryKey: QK.queue })
    },
  })
  const clean = useMutation({
    mutationFn: (args: { workspace_id: number; force: boolean }) =>
      specEngineApi.cleanWorkspace(args),
    onSuccess: () => void client.invalidateQueries({ queryKey: QK.queue }),
  })

  useEffect(() => {
    setArmed(false)
    teardown.reset()
    clean.reset()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [entry.run_id])

  const result = teardown.data
  const kept = result?.kept ?? []

  return (
    <div className="se-blk">
      <h3>{i18nT('apps.specEngine.reviewQueuePanel.teardown')}</h3>

      {result && (
        // `complete` decides which of these two this is. Never `ok`.
        <div className={result.complete ? 'se-torn' : 'se-kept'} role="status">
          <strong>
            {i18nT(
              result.complete
                ? 'apps.specEngine.reviewQueuePanel.teardown_complete'
                : 'apps.specEngine.reviewQueuePanel.teardown_incomplete',
            )}
          </strong>
          {!result.complete && (
            <>
              <p className="se-note">
                {i18nT('apps.specEngine.reviewQueuePanel.kept_workspaces_are_still_standing')}
              </p>
              <ul>
                {kept.map((workspaceId) => (
                  <li key={workspaceId}>
                    <span className="se-m">{fmtNumber(workspaceId)}</span>
                    <span className="se-acts">
                      <button
                        type="button"
                        className="se-btn se-sm"
                        disabled={clean.isPending}
                        onClick={() => clean.mutate({ workspace_id: workspaceId, force: false })}
                      >
                        {i18nT('apps.specEngine.reviewQueuePanel.remove')}
                      </button>
                      <button
                        type="button"
                        className="se-btn se-sm se-danger"
                        disabled={clean.isPending}
                        onClick={() => clean.mutate({ workspace_id: workspaceId, force: true })}
                      >
                        {i18nT('apps.specEngine.reviewQueuePanel.force_remove')}
                      </button>
                    </span>
                  </li>
                ))}
              </ul>
            </>
          )}
        </div>
      )}

      {clean.isError && (
        <Refused
          title={i18nT('apps.specEngine.reviewQueuePanel.the_cleanup_failed')}
          error={clean.error}
        />
      )}
      {clean.isSuccess && (
        <p className="se-note">
          {i18nT(
            clean.data.removed
              ? 'apps.specEngine.reviewQueuePanel.the_workspace_was_removed'
              : 'apps.specEngine.reviewQueuePanel.no_active_workspace_has_that_id',
          )}
        </p>
      )}

      {teardown.isError && (
        <Refused
          title={i18nT('apps.specEngine.reviewQueuePanel.the_teardown_failed')}
          error={teardown.error}
        />
      )}

      {armed ? (
        <div className="se-arm">
          <p>
            <AlertTriangle className="lucide-inline" aria-hidden="true" />
            {i18nT('apps.specEngine.reviewQueuePanel.teardown_destroys_the_runs_checkouts')}
          </p>
          <div className="se-acts">
            <button
              type="button"
              className="se-btn se-danger"
              disabled={teardown.isPending}
              onClick={() => teardown.mutate()}
            >
              {i18nT('apps.specEngine.reviewQueuePanel.confirm_the_teardown')}
            </button>
            <button type="button" className="se-btn" onClick={() => setArmed(false)}>
              {i18nT('apps.specEngine.reviewQueuePanel.keep_them')}
            </button>
          </div>
        </div>
      ) : (
        <div className="se-acts">
          <button type="button" className="se-btn" onClick={() => setArmed(true)}>
            {i18nT('apps.specEngine.reviewQueuePanel.tear_down_this_runs_workspaces')}
          </button>
        </div>
      )}
    </div>
  )
}

/**
 * What this surface can and cannot do about the reason the run is waiting.
 *
 * Deliberately not a row of buttons. Five of the mockup's six moves have no
 * route, and a disabled button with a tooltip is still a button: it teaches an
 * operator to keep clicking. A sentence naming the transport that does have the
 * capability is the honest shape.
 */
function ActBlock({ entry }: { entry: QueueEntry }) {
  const reason = actReason(entry)
  return (
    <div className="se-blk">
      <h3>{i18nT(ACT_HEAD_KEY[reason])}</h3>
      <p className="se-note" data-act-reason={reason}>
        {i18nT(ACT_NOTE_KEY[reason])}
      </p>
    </div>
  )
}

/**
 * The row-level state words, from the corrected mockup.
 *
 * Words, not codes, and one class per meaning: each licenses a different action,
 * and one shared style for two meanings is how a reader learns to ignore both.
 *
 * `keptCount` is not a `QueueEntry` field — the queue projection has no notion of
 * a kept workspace, and the count only exists once a teardown has reported one.
 * It is threaded from the teardown result rather than invented, so the flag says
 * something true or does not appear.
 */
export function RowFlags({ entry, keptCount }: { entry: QueueEntry; keptCount: number }) {
  return (
    <>
      {entry.revision_exhausted && (
        <span className="se-flag" data-flag="exhausted">
          {i18nT('apps.specEngine.reviewQueuePanel.flag_revisions_spent')}
        </span>
      )}
      {entry.feedback_quarantined > 0 && (
        <span className="se-flag" data-flag="held">
          {fmtNumber(entry.feedback_quarantined)}
          {' '}
          {i18nT('apps.specEngine.reviewQueuePanel.flag_held')}
        </span>
      )}
      {entry.feedback_needs_human && (
        <span className="se-flag" data-flag="human">
          {i18nT('apps.specEngine.reviewQueuePanel.flag_needs_a_person')}
        </span>
      )}
      {keptCount > 0 && (
        <span className="se-flag" data-flag="kept">
          {fmtNumber(keptCount)}
          {' '}
          {i18nT('apps.specEngine.reviewQueuePanel.flag_workspaces_kept')}
        </span>
      )}
    </>
  )
}

/**
 * The docked inspector's body for the selected run.
 *
 * Bound to the row by `key={entry.run_id}` at the call site, which is what makes
 * every piece of local state here — an armed teardown, a typed identifier, a
 * previous result — belong to the run on screen. The mockup's inspector was
 * static below its header, so selecting a different run left the first run's
 * detail in place; remounting per run is the fix, and it is a stronger one than
 * clearing state in an effect because it cannot miss a field added later.
 *
 * Block order is load-bearing: the untrusted prose is last. See the header.
 */
export function RunInspectorBody({
  entry,
  onKept,
}: {
  entry: QueueEntry
  onKept: (runId: string, kept: number[]) => void
}) {
  return (
    <>
      <ActBlock entry={entry} />
      <HeldBlock entry={entry} />
      <RedispatchBlock entry={entry} />
      <TeardownBlock entry={entry} onKept={onKept} />
      {/* Last, because it is the only block holding outside-authored prose. */}
      <FindingsBlock entry={entry} />
    </>
  )
}

/** Exported for the panel's own tests, which assert the reading rather than re-deriving it. */
export const __panelTesting = { ACT_HEAD_KEY, ACT_NOTE_KEY, actReason }
