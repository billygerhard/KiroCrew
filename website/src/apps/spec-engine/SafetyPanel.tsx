/**
 * The two safety readings: the kill switch, and what one run has spent.
 *
 * Built to `design/mockup-b.html` — the switch lives in the persistent status
 * strip, and the per-run spend figures live in the docked inspector. Both are
 * READS first: the control exists to change a state an operator can already see,
 * never to be the only place that state appears.
 *
 * ## A 200 is not a confirmation
 *
 * The load-bearing property of this file. Engaging and releasing are reported
 * against the PERSISTED flag, read back after the write, and not against the
 * status of the response that requested it:
 *
 * - The POST's own reply carries the state the handler saw (`_engage` returns the
 *   record it persisted, `_release` re-reads the flag), and that is checked.
 * - Then the flag is read AGAIN through `GET /kill-switch`, which is the same read
 *   the strip renders, so the confirmation and the display cannot disagree.
 * - A reply that arrived 200 while the read-back still shows the old state is
 *   reported as NOT confirmed. That shape is not hypothetical: the flag is a file,
 *   `release` unlinks it and `engage` writes it, and every way that can go half-way
 *   (a stale read, a second writer, a filesystem that acknowledged a write it did
 *   not keep) produces exactly a 200 with an unchanged flag.
 * - A read-back that FAILS is also not a confirmation. It is not a failure either:
 *   the operation may well have landed, and saying "it failed" would be as wrong as
 *   saying it worked. It reads as unknown, and points at the strip.
 *
 * The direction of doubt is the engine's: `KillSwitch.read` treats an unreadable
 * flag as ENGAGED, because absence of evidence is not evidence that nobody stopped
 * the engine. So this surface never renders doubt as released — not while the read
 * is pending, not when it failed — and it does not offer a release for a state it
 * could not read, because "release" is a claim about what is in force.
 *
 * ## Engaging names its reason; releasing names its initiator
 *
 * The handler refuses only a non-string `reason` (`bad_reason`), so an empty one
 * would persist a stop whose record answers nothing — and the engine keeps the
 * FIRST engage's record forever, so that emptiness is permanent. The reason is
 * required here, where the person who knows it is standing.
 *
 * The initiator is deliberately not a field: `handle_post_kill_switch` attributes
 * both directions to the authenticated session, because a stop recorded against a
 * name the caller typed records nothing. The panel says so rather than offering an
 * input that would be ignored.
 *
 * ## No overlay, here least of all
 *
 * The arm-then-confirm step and every verdict are in-flow siblings inside the
 * status strip, which is a grid row of the page. The losing mockup dimmed and
 * click-blocked its own stop control behind a drawer scrim; a dialog here would
 * reintroduce that failure at the one control that must never be occluded.
 * `styles.ts` states the rule and `SpecEngineShell.test.tsx` fails on it.
 *
 * ## What is NOT here
 *
 * The mockup's "In flight" run count and its global ceiling meter. No route vends
 * either: `/queue` returns only runs waiting on a person, and the ceiling is
 * resolved PER RUN (`_run_spend` resolves it for that run's project), so a
 * page-wide meter would be a number this surface invented. The narrowing is
 * recorded in the spec's task record rather than filled in with an estimate.
 */
import { useCallback, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle } from 'lucide-react'

import { i18nT } from '../../i18n/t'
import { fmtDateTime, fmtNumber } from '../../i18n/format'
import {
  QK,
  REFUSAL,
  SpecEngineApiError,
  specEngineApi,
  type KillSwitchState,
  type QueueEntry,
} from './api'

/** Separator between two identifiers on one line. Punctuation, not copy. */
const SEP = ' \u00b7 '
/** Stands in for a field the engine has no value for. Punctuation, not copy. */
const NONE = '\u2014'

/** Credits are money, so two places. Turns and sessions are counts. */
const CREDITS = { maximumFractionDigits: 2 } as const

/** The two directions the switch can be moved in. */
type Action = 'engage' | 'release'

/**
 * What the strip may say about the switch. Three states, not two.
 *
 * `unknown` covers both a pending read and a failed one, and it exists because the
 * missing third state is what made a failed read render as a green dot: the strip
 * branched on `engaged === true`, so every other case — including "we have not
 * read it yet" and "we could not read it" — fell into the released styling beside
 * text saying the switch could not be read.
 */
export type SwitchReading = 'engaged' | 'released' | 'unknown'

/**
 * The reading for a switch state, or for the absence of one.
 *
 * Takes the state rather than the query, so the rule is one expression a test can
 * drive directly with no React Query in the way.
 */
export function switchReading(state: KillSwitchState | undefined): SwitchReading {
  if (state === undefined) return 'unknown'
  return state.engaged ? 'engaged' : 'released'
}

/**
 * Whether a persisted state confirms the action that was requested.
 *
 * The whole confirmation rule, as one predicate: an engage is confirmed by a flag
 * that reads engaged, a release by one that reads released. An absent state is
 * never a confirmation — that is the read-back that failed, and it must not
 * default to "well, it probably worked".
 *
 * `unreadable` needs no arm of its own: the engine's own reader sets `engaged` true
 * whenever it is set, so a release that left an unparseable record behind reads as
 * unconfirmed through the `engaged` field itself.
 */
export function confirmsIntent(action: Action, state: KillSwitchState | undefined): boolean {
  if (state === undefined) return false
  return action === 'engage' ? state.engaged : !state.engaged
}

/** What one engage or release did, as the panel needs to report it. */
interface Outcome {
  action: Action
  /** The state the POST itself reported. */
  reported: KillSwitchState | undefined
  /** The state a fresh read of the flag returned. `undefined` when that read failed. */
  persisted: KillSwitchState | undefined
  /** Why the read-back failed, when it did. */
  readBackFailure: unknown
  /** Release only: whether the switch had been engaged. */
  changed: boolean | undefined
  /** Engage only: whether it was already engaged before this call. */
  alreadyEngaged: boolean | undefined
  /** Engage only: how many runs it parked, and what they had consumed. */
  halted: number
  haltedCredits: number
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
 * The switch's state on the strip, and the control that moves it.
 *
 * Returns a FRAGMENT of two strip children rather than one wrapper: the reading
 * sits at the right-hand end of the strip's own row, and the arm-and-verdict block
 * has to be a direct flex child of the strip to take a full line of its own. A
 * wrapper would nest it inside the right-hand cluster, where a full-width child
 * cannot wrap, and the only way out of that is an overlay.
 *
 * Reads through {@link QK.killSwitch}, the key the page shell reads for the
 * strip's engaged styling, so both render one cache entry: two independent reads of
 * one flag is how a dot ends up disagreeing with the text beside it.
 */
export function KillSwitchControls() {
  const client = useQueryClient()
  const read = useQuery({
    queryKey: QK.killSwitch,
    queryFn: () => specEngineApi.killSwitch(),
    retry: false,
  })
  const [armed, setArmed] = useState<Action | null>(null)
  const [reason, setReason] = useState('')
  const [reasonMissing, setReasonMissing] = useState(false)

  const operate = useMutation<Outcome, Error, { action: Action; reason: string }>({
    mutationFn: async ({ action, reason: why }) => {
      const response = await specEngineApi.setKillSwitch(
        // No `reason` at all on a release: the field is the engage's record, and
        // sending a stale one would attach an explanation to the wrong direction.
        action === 'engage' ? { action, reason: why } : { action },
      )
      let persisted: KillSwitchState | undefined
      let readBackFailure: unknown = null
      try {
        // The confirmation, and the same read the strip renders. `fetchQuery`
        // rather than a bare call so the flag this verdict is read from is the
        // flag on screen: a private fetch could confirm one state while the strip
        // still showed another. `staleTime: 0` is stated because the whole point
        // is to go to the route again — a cached answer would confirm the write
        // against a read taken before it.
        const fresh = await client.fetchQuery({
          queryKey: QK.killSwitch,
          queryFn: () => specEngineApi.killSwitch(),
          retry: false,
          staleTime: 0,
        })
        persisted = fresh.switch
      } catch (failure) {
        // Not rethrown. The write may well have landed, so reporting this as a
        // failed operation would be as wrong as reporting it as a successful one.
        readBackFailure = failure
      }
      return {
        action,
        reported: response.switch,
        persisted,
        readBackFailure,
        changed: response.changed,
        alreadyEngaged: response.already_engaged,
        halted: response.halted?.length ?? 0,
        haltedCredits: response.total_credits ?? 0,
      }
    },
    onSuccess: () => {
      setArmed(null)
      setReason('')
      setReasonMissing(false)
      // The queue's credits and the runs it lists both move when the switch parks
      // runs, so the list is re-read rather than left showing pre-stop rows.
      void client.invalidateQueries({ queryKey: QK.queue })
    },
  })

  const arm = useCallback(
    (action: Action) => {
      // The previous verdict goes with the previous decision: a stale "the stop is
      // in force" above a fresh confirmation is how an operator reads the wrong one.
      operate.reset()
      setReasonMissing(false)
      setArmed(action)
    },
    [operate],
  )

  const state = read.data?.switch
  const reading = switchReading(state)
  const stoppable = read.data?.stoppable.length ?? 0
  const stoppableCredits = read.data?.stoppable_credits ?? 0

  const outcome = operate.data
  const confirmed = outcome ? confirmsIntent(outcome.action, outcome.persisted) : false
  // Both halves have to agree. The handler's own reply is the first place a
  // half-landed write shows up, and the read-back is the second; a verdict that
  // consulted only one of them would call the other's disagreement a success.
  const reportedAgrees = outcome ? confirmsIntent(outcome.action, outcome.reported) : false
  const settled = confirmed && reportedAgrees

  const onConfirm = useCallback(() => {
    if (armed === null) return
    if (armed === 'engage' && reason.trim() === '') {
      // The engine keeps the FIRST engage's record forever, so an empty reason is
      // not a small omission — it is the permanent answer to why the engine stopped.
      setReasonMissing(true)
      return
    }
    setReasonMissing(false)
    operate.mutate({ action: armed, reason: reason.trim() })
  }, [armed, reason, operate])

  return (
    <>
      <span className="se-ks">
        {/* Three states on the visual channel, because doubt must not read as
            released: solid green is go, solid danger is stopped, and a hollow ring
            is "this has not been read". The reading is one function so the dot and
            the text below it cannot come from two different rules. */}
        <span className="se-ks-dot" data-state={reading} />
        <span className="se-ks-text">
          {read.isError
            ? i18nT('apps.specEngine.specEnginePage.could_not_read_the_kill_switch')
            : reading === 'unknown'
              ? i18nT('apps.specEngine.specEnginePage.reading_the_kill_switch')
              : reading === 'engaged'
                ? i18nT('apps.specEngine.specEnginePage.kill_switch_engaged')
                : i18nT('apps.specEngine.specEnginePage.kill_switch_released')}
        </span>
        {/* A stop in force because its own record could not be parsed is a repair,
            not an operator's decision, and the two must not read alike. */}
        {state?.unreadable && (
          <span className="se-lbl">
            {i18nT('apps.specEngine.specEnginePage.kill_switch_record_unreadable')}
          </span>
        )}
        {reading === 'engaged' ? (
          <button
            type="button"
            disabled={operate.isPending}
            onClick={() => arm('release')}
          >
            {i18nT('apps.specEngine.safetyPanel.release_the_kill_switch')}
          </button>
        ) : (
          <>
            {/* Offered in every reading including `unknown`: stopping is the
                fail-closed direction, so it stays available when the state could
                not be read. A RELEASE is not offered there — releasing is a claim
                about what is in force, and nothing here knows what is. */}
            <button
              type="button"
              disabled={operate.isPending}
              onClick={() => arm('engage')}
            >
              {i18nT('apps.specEngine.safetyPanel.engage_the_kill_switch')}
            </button>
            {reading === 'unknown' && !read.isPending && (
              <button type="button" onClick={() => void read.refetch()}>
                {i18nT('apps.specEngine.safetyPanel.read_the_switch_again')}
              </button>
            )}
          </>
        )}
      </span>

      {(armed !== null || outcome !== undefined || operate.isError) && (
        <div className="se-ks-panel" data-armed={armed ?? undefined}>
          {operate.isError && (
            <Refused
              title={i18nT(
                operate.variables?.action === 'release'
                  ? 'apps.specEngine.safetyPanel.the_release_was_refused'
                  : 'apps.specEngine.safetyPanel.the_stop_was_refused',
              )}
              error={operate.error}
            />
          )}

          {outcome !== undefined && (
            <div className={settled ? 'se-torn' : 'se-kept'} role="status">
              {settled ? (
                <>
                  <strong>
                    {i18nT(
                      outcome.action === 'engage'
                        ? 'apps.specEngine.safetyPanel.the_stop_is_in_force'
                        : 'apps.specEngine.safetyPanel.new_work_may_start_again',
                    )}
                  </strong>
                  {outcome.action === 'engage' ? (
                    <>
                      <p className="se-note">
                        {i18nT('apps.specEngine.safetyPanel.runs_parked_and_credits', {
                          runs: fmtNumber(outcome.halted),
                          credits: fmtNumber(outcome.haltedCredits, CREDITS),
                        })}
                      </p>
                      {outcome.alreadyEngaged === true && (
                        <p className="se-note">
                          {i18nT('apps.specEngine.safetyPanel.it_was_already_engaged')}
                        </p>
                      )}
                    </>
                  ) : (
                    outcome.changed === false && (
                      <p className="se-note">
                        {i18nT('apps.specEngine.safetyPanel.it_was_not_engaged')}
                      </p>
                    )
                  )}
                </>
              ) : (
                /* The deceptive shape. A 200 whose read-back disagrees with what was
                   asked for is reported as unconfirmed, and the sentence says which
                   state is actually in force so the operator acts on the flag rather
                   than on the request. */
                <>
                  <strong>{i18nT('apps.specEngine.safetyPanel.not_confirmed')}</strong>
                  <p className="se-note" data-unconfirmed={outcome.action}>
                    {outcome.readBackFailure !== null
                      ? i18nT('apps.specEngine.safetyPanel.the_switch_could_not_be_read_back')
                      : outcome.action === 'engage'
                        ? i18nT('apps.specEngine.safetyPanel.the_flag_still_reads_released')
                        : i18nT('apps.specEngine.safetyPanel.the_flag_still_reads_engaged')}
                  </p>
                  {outcome.readBackFailure !== null && (
                    <Refused
                      title={i18nT(
                        'apps.specEngine.specEnginePage.could_not_read_the_kill_switch',
                      )}
                      error={outcome.readBackFailure}
                    />
                  )}
                </>
              )}
            </div>
          )}

          {armed === 'engage' && (
            <div className="se-arm">
              <p>
                <AlertTriangle className="lucide-inline" aria-hidden="true" />
                {i18nT('apps.specEngine.safetyPanel.engaging_stops_every_unattended_run')}
              </p>
              {/* The control names its own blast radius before it is thrown. The
                  figures are the route's (`stoppable`), not a count of the queue:
                  the queue holds only runs waiting on a person, and a stop reaches
                  every run that is neither finished nor already parked. */}
              <p className="se-note">
                {read.data === undefined
                  ? i18nT('apps.specEngine.safetyPanel.the_blast_radius_could_not_be_read')
                  : i18nT('apps.specEngine.safetyPanel.runs_this_stop_would_park', {
                      runs: fmtNumber(stoppable),
                      credits: fmtNumber(stoppableCredits, CREDITS),
                    })}
              </p>
              <div className="se-idfield">
                <label htmlFor="se-ks-reason">
                  {i18nT('apps.specEngine.safetyPanel.why_the_engine_is_being_stopped')}
                </label>
                <input
                  id="se-ks-reason"
                  className="se-input"
                  value={reason}
                  onChange={(event) => setReason(event.target.value)}
                />
              </div>
              {reasonMissing && (
                <p className="se-note" role="alert">
                  {i18nT('apps.specEngine.safetyPanel.a_stop_must_record_why')}
                </p>
              )}
              <div className="se-acts">
                <button
                  type="button"
                  className="se-btn se-danger"
                  disabled={operate.isPending}
                  onClick={onConfirm}
                >
                  {i18nT('apps.specEngine.safetyPanel.confirm_the_stop')}
                </button>
                <button type="button" className="se-btn" onClick={() => setArmed(null)}>
                  {i18nT('apps.specEngine.safetyPanel.keep_the_engine_running')}
                </button>
              </div>
            </div>
          )}

          {armed === 'release' && (
            <div className="se-arm">
              <p>
                <AlertTriangle className="lucide-inline" aria-hidden="true" />
                {i18nT('apps.specEngine.safetyPanel.releasing_lets_new_work_start')}
              </p>
              {/* Who stopped it and why, from the record itself. An operator
                  releasing a stop has to be able to read the decision they are
                  overriding without leaving the pane it is offered on. */}
              {state?.unreadable ? (
                <p className="se-note">
                  {i18nT('apps.specEngine.safetyPanel.releasing_an_unreadable_stop_is_a_repair')}
                </p>
              ) : (
                <>
                  <p className="se-note">
                    {state?.initiator
                      ? i18nT('apps.specEngine.safetyPanel.engaged_by_at', {
                          initiator: state.initiator,
                          when: state.engaged_ts ? fmtDateTime(state.engaged_ts) : NONE,
                        })
                      : i18nT('apps.specEngine.safetyPanel.engaged_by_nobody_named', {
                          when: state?.engaged_ts ? fmtDateTime(state.engaged_ts) : NONE,
                        })}
                  </p>
                  <p className="se-note">
                    {state?.reason
                      ? i18nT('apps.specEngine.safetyPanel.the_stated_reason', {
                          reason: state.reason,
                        })
                      : i18nT('apps.specEngine.safetyPanel.no_reason_was_recorded')}
                  </p>
                </>
              )}
              {/* The initiator is the session, never a typed name: the handler
                  attributes both directions to the authenticated caller, so an
                  input here would collect something nothing records. */}
              <p className="se-note">
                {i18nT('apps.specEngine.safetyPanel.recorded_against_your_session')}
              </p>
              <div className="se-acts">
                <button
                  type="button"
                  className="se-btn"
                  disabled={operate.isPending}
                  onClick={onConfirm}
                >
                  {i18nT('apps.specEngine.safetyPanel.confirm_the_release')}
                </button>
                <button type="button" className="se-btn" onClick={() => setArmed(null)}>
                  {i18nT('apps.specEngine.safetyPanel.leave_the_stop_in_force')}
                </button>
              </div>
            </div>
          )}
        </div>
      )}
    </>
  )
}

/**
 * The origin of the ceiling in force, in words.
 *
 * Reuses the configuration pane's origin vocabulary rather than restating it:
 * these are the engine's own precedence layers, one meaning per key, and a second
 * spelling of "Bundled default" would be two strings to keep in step across every
 * catalog. Typed loosely on purpose — `_run_spend` relays
 * `EffectiveValue.origin.value` as a plain string, so an origin this table does
 * not know renders as the raw value instead of as blank.
 */
const CEILING_ORIGIN_KEY: Record<string, string> = {
  bundled_default: 'apps.specEngine.configPanel.origin_bundled_default',
  app_config: 'apps.specEngine.configPanel.origin_app_config',
  cost_profile: 'apps.specEngine.configPanel.origin_cost_profile',
  project_config: 'apps.specEngine.configPanel.origin_project_config',
  source_config: 'apps.specEngine.configPanel.origin_source_config',
}

/**
 * What the selected run has spent, and the ceiling it is judged against.
 *
 * Bound to the run it describes: the query is keyed by `run_id` and the block is
 * remounted per run by the inspector's `key`, which is what makes it a per-row
 * pane rather than the mockup's static one — mockup-b's inspector was static below
 * its header, so selecting the budget-parked run still showed the first run's
 * figures, and that was recorded as unresolved design rather than as fidelity.
 *
 * Every figure is the ENGINE's. `credits` is the total the ceiling compares —
 * metered credits from stamped sessions plus credits an external capability
 * provider declared — and it is deliberately not assembled here from the rows this
 * surface happens to hold, because a browser-side sum silently disagrees with the
 * number the engine enforces against.
 */
export function RunSpendBlock({ entry }: { entry: QueueEntry }) {
  const spend = useQuery({
    queryKey: QK.runSpend(entry.run_id),
    // A refusal here is a state to read, not a spinner to sit through: an unknown
    // run means the selection is stale and no retry can change that.
    queryFn: () => specEngineApi.runSpend(entry.run_id),
    retry: false,
  })

  const data = spend.data
  const ceiling = data?.ceiling
  const originKey = ceiling ? CEILING_ORIGIN_KEY[ceiling.origin] : undefined
  // The run row's own stored figure, reported beside the total rather than instead
  // of it. They should agree; when they do not, the total is the one the ceiling
  // compares, and saying which is which is the whole reason both travel.
  const disagrees = data !== undefined && data.recorded_credits !== data.credits

  return (
    <div className="se-blk">
      <h3>{i18nT('apps.specEngine.safetyPanel.spend')}</h3>

      {spend.isError ? (
        codeOf(spend.error) === REFUSAL.runUnknown ? (
          /* A 404 is not the same failure as a 503, and it is the one an operator
             can act on: the run left the table, so the row on the left is stale and
             nothing here is worth retrying. */
          <p className="se-note" role="status">
            {i18nT('apps.specEngine.safetyPanel.no_run_has_that_id')}
          </p>
        ) : (
          <Refused
            title={i18nT('apps.specEngine.safetyPanel.could_not_read_this_runs_spend')}
            error={spend.error}
          />
        )
      ) : data === undefined ? (
        /* The pending state, written as the absence of the payload rather than as
           `isPending`: the query's status flag does not narrow the payload's type,
           and a non-null assertion to satisfy that would be the one place this
           block could read a figure that is not there. */
        <p className="se-pending">
          {i18nT('apps.specEngine.safetyPanel.reading_this_runs_spend')}
        </p>
      ) : (
        <>
          <dl className="se-kv">
            <dt>{i18nT('apps.specEngine.safetyPanel.total')}</dt>
            <dd>
              {i18nT('apps.specEngine.safetyPanel.total_of_ceiling', {
                total: fmtNumber(data.credits, CREDITS),
                ceiling: fmtNumber(ceiling?.value ?? 0, CREDITS),
              })}
            </dd>
            <dt>{i18nT('apps.specEngine.safetyPanel.metered')}</dt>
            <dd>{fmtNumber(data.metered_credits, CREDITS)}</dd>
            <dt>{i18nT('apps.specEngine.safetyPanel.declared')}</dt>
            <dd>{fmtNumber(data.declared_credits, CREDITS)}</dd>
            <dt>{i18nT('apps.specEngine.safetyPanel.turns')}</dt>
            <dd>{fmtNumber(data.turns)}</dd>
            <dt>{i18nT('apps.specEngine.safetyPanel.sessions')}</dt>
            <dd>{fmtNumber(data.sessions)}</dd>
            <dt>{i18nT('apps.specEngine.safetyPanel.ceiling')}</dt>
            <dd>
              {ceiling ? fmtNumber(ceiling.value, CREDITS) : NONE}
              {/* Where the ceiling was declared, in the same words the resolved
                  configuration uses. A surface showing only the number cannot tell
                  an operator whether somebody chose it or whether the app ships it,
                  and those call for opposite actions. */}
              <span className="se-src">
                {SEP}
                {originKey ? i18nT(originKey) : (ceiling?.origin ?? NONE)}
                {ceiling?.declared_at ? `${SEP}${ceiling.declared_at}` : ''}
              </span>
            </dd>
          </dl>
          <p className="se-note">
            {i18nT('apps.specEngine.safetyPanel.declared_credits_are_inside_the_total')}
          </p>
          {disagrees && (
            <p className="se-note" data-spend-disagrees="true">
              {i18nT('apps.specEngine.safetyPanel.the_run_row_records_a_different_figure', {
                recorded: fmtNumber(data.recorded_credits, CREDITS),
              })}
            </p>
          )}
        </>
      )}
    </div>
  )
}
