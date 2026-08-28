/**
 * The conformance check for one capability's configured provider: start it, poll
 * it, and read what came back.
 *
 * An operator who has just bound an external provider has bound something the
 * engine knows nothing about. The engine ships a suite that puts a candidate
 * through the fixtures and assertion classes the capability's contract declares, so
 * the question "does this actually satisfy the contract" has an answer before a run
 * asks it. This is the surface for that suite.
 *
 * ## Why it is a job and not a button that waits
 *
 * The suite invokes the provider once per fixture and again for the repeatability
 * check — up to nine calls for a document capability — spawning a child process
 * every time, and it enforces no aggregate deadline of its own. So the POST starts a
 * job and answers immediately with `running` and no outcome, and this panel polls.
 * A control that awaited a verdict would hold a request open for minutes and would
 * take the pane with it.
 *
 * ## What it will not say
 *
 * That a run was free, or that it was costly. `provider.nature` is hardcoded
 * `model_backed` for every external binding because the engine cannot tell whether
 * an external program reasons, so the only honest sentence about the cost of nine
 * calls to it is that the engine does not know. This panel therefore states the
 * COUNT — which is the engine's own, projected, because it differs by capability —
 * and states that the cost of those calls is unknown.
 *
 * That the absence of failures is a pass. A run that could not be carried out, a run
 * still in flight, a run nobody started, and a report about a binding that has since
 * changed all read as no outcome. {@link conformanceView} is where that is decided,
 * and it takes the worse of every disagreement it finds.
 *
 * ## The reason strings
 *
 * A reported reason is engine prose that can quote a provider's own bytes — the
 * `malformed-response` fixture exists to make a provider echo attacker-authored JSON
 * into a schema error. The engine narrows it where the reason is composed, this
 * module narrows it again and caps its length, and it reaches the DOM as a text
 * CHILD. The absence of `dangerouslySetInnerHTML` is the guarantee; the two
 * narrowings are so that neither end depends on the other never changing.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { fmtNumber } from '../../i18n/format'
import { i18nT } from '../../i18n/t'

import { QK, specEngineApi } from './api'
import { Refused } from './ConfigPanel'
import {
  conformanceView,
  pollAfterMs,
  presentedRows,
  type CheckOutcome,
  type ConformanceView,
  type Reading,
} from './conformanceView'

/** Separator between two identifiers on one line. Punctuation, not copy. */
const SEP = ' \u00b7 '

/**
 * What each verdict reads as. Whole literals, per the pane's key rule.
 *
 * Indexed rather than branched so that adding a reading without a sentence is a
 * type error rather than a blank verdict line.
 */
const READING_KEY: Record<Reading, string> = {
  passed: 'apps.specEngine.conformance.the_provider_conforms',
  qualified: 'apps.specEngine.conformance.the_provider_conforms_with_a_qualification',
  failed: 'apps.specEngine.conformance.the_provider_does_not_conform',
  no_outcome: 'apps.specEngine.conformance.no_verdict_was_obtained',
}

/** What each check outcome reads as. */
const OUTCOME_KEY: Record<CheckOutcome, string> = {
  passed: 'apps.specEngine.conformance.this_check_held',
  declined: 'apps.specEngine.conformance.this_check_held_by_declining',
  failed: 'apps.specEngine.conformance.this_check_failed',
  never_ran: 'apps.specEngine.conformance.this_check_never_ran',
}

/**
 * The sentence that introduces a state which is not a verdict.
 *
 * `complete` has no entry: it is the one situation whose leading sentence is the
 * verdict itself, and giving it a preamble here would put a sentence above the
 * verdict for a reader to take away instead.
 */
const SITUATION_KEY: Record<Exclude<ConformanceView['situation'], 'complete'>, string> = {
  not_applicable: 'apps.specEngine.conformance.the_engine_serves_this_so_there_is_nothing_to_check',
  never_run: 'apps.specEngine.conformance.this_provider_has_not_been_checked',
  running: 'apps.specEngine.conformance.a_check_is_running',
  no_outcome: 'apps.specEngine.conformance.no_outcome_was_obtained',
  earlier_binding: 'apps.specEngine.conformance.the_outcome_describes_an_earlier_binding',
}

/** The verdict block: what the run as a whole may be presented as. */
function Verdict({ view }: { view: ConformanceView }) {
  return (
    <p className="se-note" role="status" data-reading={view.reading}>
      <strong>{i18nT(READING_KEY[view.reading])}</strong>
      {view.reading === 'qualified' && (
        <>
          {' '}
          {i18nT('apps.specEngine.conformance.it_declined_planted_defects', {
            declined: fmtNumber(view.declined),
          })}
        </>
      )}
    </p>
  )
}

/**
 * One check, with its own outcome and the engine's reason for it.
 *
 * A check that never ran carries no reason because there is nothing to report about
 * a call that was not made — its row exists to say the run cannot speak for that
 * part of the contract, which is a failure of the run rather than a silence.
 */
function Check({ check }: { check: ConformanceView['checks'][number] }) {
  return (
    <div className="se-pathrow" data-check={check.check} data-outcome={check.outcome}>
      <span className="se-m">
        {check.check}
        {check.fixture ? `${SEP}${check.fixture}` : ''}
      </span>
      <span className="se-lbl">{i18nT(OUTCOME_KEY[check.outcome])}</span>
      {check.reason !== '' && (
        <span className="se-note se-m" data-reason={check.check}>
          {/* A text child, control-stripped and capped upstream. Never markup, and
              never pane copy: it is what the run reported, quoted. */}
          {check.reason}
        </span>
      )}
      {!check.declared && (
        <span className="se-note">
          {i18nT('apps.specEngine.conformance.the_suite_did_not_declare_this_check')}
        </span>
      )}
    </div>
  )
}

/**
 * One capability's conformance check.
 *
 * Mounted only where the capability's binding in force reaches OUTSIDE the engine,
 * because a builtin has nothing to check — the engine verifies its own builtins in
 * its own suite. The server's `is_builtin` then gates the control itself, which is
 * not the same test: a capability rebound to its builtin after a run still polls
 * `complete`, so a panel reading only the status would offer a re-run the POST
 * refuses.
 */
export function ConformancePanel({ capability }: { capability: string }) {
  const client = useQueryClient()
  const state = useQuery({
    queryKey: QK.conformance(capability),
    queryFn: () => specEngineApi.conformance(capability),
    retry: false,
    // Polled only while a run is in flight; every other status is terminal until
    // an operator acts. The predicate lives in the view module so the interval and
    // the state it depends on are one fact.
    refetchInterval: (query) => pollAfterMs(query.state.data),
  })
  const start = useMutation({
    mutationFn: () => specEngineApi.startConformance(capability),
    onSuccess: (reply) => {
      // The reply IS the state, carrying `running` and no report, so the panel
      // shows the run has started without waiting for the next poll.
      client.setQueryData(QK.conformance(capability), reply)
    },
    onError: () => {
      // Re-read rather than keep what is cached. A refusal means the server's view
      // and this one disagree — most often because a run started elsewhere, which
      // the server answers by dropping the previous report — and the cached body is
      // the one thing that must not go on being shown as current.
      void client.invalidateQueries({ queryKey: QK.conformance(capability) })
    },
  })

  const heading = <h4>{i18nT('apps.specEngine.conformance.conformance_check')}</h4>

  // `isError` before the data, which is this pane's rule: React Query keeps the
  // last successful body across a failing refetch, so a panel reading `data` alone
  // would go on presenting a finished run's verdict after the read that would have
  // replaced it failed.
  if (state.isError) {
    return (
      <div className="se-blk" data-conformance={capability} data-situation="unreadable">
        {heading}
        <Refused
          title={i18nT('apps.specEngine.conformance.could_not_read_the_check_state')}
          error={state.error}
        />
        <p className="se-note">
          {i18nT('apps.specEngine.conformance.a_failed_read_is_not_an_outcome')}
        </p>
      </div>
    )
  }
  if (!state.data) {
    return (
      <div className="se-blk" data-conformance={capability} data-situation="pending">
        {heading}
        <p className="se-note">{i18nT('apps.specEngine.conformance.reading_the_check_state')}</p>
      </div>
    )
  }

  const view = conformanceView(state.data)
  const rows = presentedRows(view)

  return (
    <div className="se-blk" data-conformance={capability} data-situation={view.situation}>
      {heading}
      <p className="se-note">
        {i18nT('apps.specEngine.conformance.what_the_suite_does')}
      </p>
      {/* Where it sits inside the row would otherwise leave this ambiguous, and the
          ambiguity matters: a check run against a draft would report on a binding
          nobody has written. */}
      <p className="se-note">
        {i18nT('apps.specEngine.conformance.the_check_runs_against_the_binding_in_force')}
      </p>
      {/* Stated before the control, because it is what an operator is agreeing to.
          The count is the engine's own and differs by capability; the cost of those
          calls is the one thing nothing here claims in either direction. */}
      <p className="se-note">
        {i18nT('apps.specEngine.conformance.a_run_invokes_the_provider_up_to_n_times', {
          calls: fmtNumber(view.maxInvocations),
          seconds: fmtNumber(view.deadlineSeconds),
        })}
      </p>
      <p className="se-note" data-cost="unknown">
        {i18nT('apps.specEngine.conformance.what_those_calls_cost_is_unknown')}
      </p>
      {view.situation !== 'complete' && (
        <p className="se-note" data-situation-note={view.situation}>
          {i18nT(SITUATION_KEY[view.situation])}
        </p>
      )}
      {/* Alongside the running sentence rather than inside it: the obligation is
          that no earlier outcome is presented as current, and saying so out loud is
          how an operator who saw a verdict a minute ago knows it is not this one. */}
      {view.situation === 'running' && (
        <p className="se-note">
          {i18nT('apps.specEngine.conformance.no_earlier_outcome_is_shown_while_running')}
        </p>
      )}
      {view.error !== '' && (
        <p className="se-note se-m" data-run-error="true">
          {i18nT('apps.specEngine.conformance.why_the_run_did_not_happen')}
          {SEP}
          {view.error}
        </p>
      )}
      {view.candidate !== '' && (
        <p className="se-note">
          {i18nT('apps.specEngine.conformance.what_was_checked')}
          {SEP}
          <span className="se-m">{view.candidate}</span>
        </p>
      )}
      <div className="se-acts">
        <button
          type="button"
          className="se-btn se-sm"
          disabled={!view.canStart || start.isPending}
          onClick={() => start.mutate()}
        >
          {view.situation === 'never_run' || view.situation === 'not_applicable'
            ? i18nT('apps.specEngine.conformance.run_the_check')
            : i18nT('apps.specEngine.conformance.run_the_check_again')}
        </button>
      </div>
      {start.isError && (
        <>
          <Refused
            title={i18nT('apps.specEngine.conformance.could_not_start_the_check')}
            error={start.error}
          />
          {/* A start that did not happen produced nothing, and nothing is not a
              pass. Stated beside the refusal because the refusal names why the run
              was declined and not what an operator now knows about the provider. */}
          <p className="se-note">
            {i18nT('apps.specEngine.conformance.no_outcome_was_obtained')}
          </p>
        </>
      )}
      {/* The verdict FIRST, then the checks, in the order the view composed. A
          completed run routinely carries a green check beside a red verdict, and a
          reader who meets that row first has been told the opposite of what the run
          found. */}
      {(view.situation === 'complete' || view.checks.length > 0) && (
        <div className="se-settings" data-conformance-report={capability}>
          {rows.map((row) =>
            row.kind === 'verdict' ? (
              <Verdict key="verdict" view={view} />
            ) : (
              <Check key={`${row.check.check}/${row.check.fixture}`} check={row.check} />
            ),
          )}
        </div>
      )}
      {view.gaps.length > 0 && (
        <div className="se-blk" data-conformance-gaps={capability}>
          <span className="se-lbl">
            {i18nT('apps.specEngine.conformance.what_the_run_could_not_speak_for')}
          </span>
          {view.gaps.map((gap) => (
            // Engine prose, rendered as a text child like every reported reason.
            <p className="se-note se-m" key={gap}>
              {gap}
            </p>
          ))}
        </div>
      )}
    </div>
  )
}
