// EngineOpsPanel — the engine's operator controls: the stop switch, per-run
// spend, and every setting's effective value with the layer that produced it.
//
// The panel renders what the backend relays and derives nothing. Three rules it
// keeps deliberately:
//
//   * **origin is never inferred.** A row's layer comes from the engine's
//     `origin` field, not from comparing the value against the default. A value
//     equal to its default may still be an explicit override, and telling an
//     operator it was "shipped" would be wrong in exactly the case they are
//     inspecting.
//   * **the scopes a write is accepted at come from the registry.** A field is
//     offered read-only when the setting is not overridable at the scope on
//     screen, rather than collecting an edit the engine's write path refuses.
//   * **a run's credits are the ENGINE's attributed total.** The queue column and
//     the detail view both read a number the engine computed. Neither sums rows in
//     the browser: a browser-side total silently disagrees with the ceiling the
//     engine enforces, which is the shape of a budget defect this engine has
//     already shipped once.
//
// The stop control is a safety control, so it says what it will stop BEFORE it
// is thrown (the run count and the credits already spent) and what a release
// does NOT do (resume anything).
import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, OctagonX, Play } from 'lucide-react'
import { engineApi, originLabel } from '../api'
import { Btn } from './shared'
import EngineConfigEditor from './EngineConfigEditor'
import { Input } from '../../../components/ui'
import Modal from '../../../components/Modal'
import { i18nT } from '../../../i18n/t'
import { fmtNumber } from '../../../i18n/format'

export interface EngineOpsPanelProps {
  onClose: () => void
  setErr: (msg: string) => void
}

/** One run's spend, read from the engine per run rather than summed here.
 *
 *  `credits` is `RunAccounting.spend(run).total_credits` — the figure the ceiling
 *  compares — and the declared part is shown beside it because that is the spend
 *  a sum over turn rows would miss entirely. */
function RunSpendDetail({ runId }: { runId: string }) {
  const detail = useQuery({
    queryKey: ['spec-builder', 'engine-run-spend', runId],
    queryFn: () => engineApi.getRunSpend(runId),
  })
  if (detail.isError) {
    return (
      <p role="alert" style={{ margin: '8px 0 0' }}>
        <AlertTriangle className="lucide-inline" aria-hidden="true" />
        {(detail.error as Error).message}
      </p>
    )
  }
  const row = detail.data
  if (!row) return null
  return (
    <div aria-label={i18nT('apps.specBuilder.engineOps.detail_section')} style={{ marginTop: 8 }}>
      <h4 style={{ margin: '0 0 4px' }}>
        {i18nT('apps.specBuilder.engineOps.detail_heading', { run: row.run_id })}
      </h4>
      <p style={{ margin: 0 }}>
        {i18nT('apps.specBuilder.engineOps.detail_credits', {
          credits: fmtNumber(row.credits, { maximumFractionDigits: 2 }),
          ceiling: fmtNumber(Number(row.ceiling.value), { maximumFractionDigits: 2 }),
        })}
      </p>
      <p style={{ margin: 0 }}>
        {i18nT('apps.specBuilder.engineOps.detail_split', {
          metered: fmtNumber(row.metered_credits, { maximumFractionDigits: 2 }),
          declared: fmtNumber(row.declared_credits, { maximumFractionDigits: 2 }),
        })}
      </p>
      <p style={{ margin: 0 }}>
        {i18nT('apps.specBuilder.engineOps.detail_ceiling_origin', {
          origin: originLabel(row.ceiling.origin),
        })}
      </p>
    </div>
  )
}

export default function EngineOpsPanel({ onClose, setErr }: EngineOpsPanelProps) {
  const qc = useQueryClient()
  const [reason, setReason] = useState('')
  // The run whose spend detail is open. One at a time: the detail is a read of
  // ONE run's attribution, and a page of them would invite adding them up.
  const [detailRun, setDetailRun] = useState('')

  const configQuery = useQuery({
    queryKey: ['spec-builder', 'engine-config'],
    queryFn: () => engineApi.getConfig(),
  })
  const switchQuery = useQuery({
    queryKey: ['spec-builder', 'engine-kill-switch'],
    queryFn: () => engineApi.getKillSwitch(),
  })
  const queueQuery = useQuery({
    queryKey: ['spec-builder', 'engine-queue'],
    queryFn: () => engineApi.getQueue(),
  })

  const switchMutation = useMutation({
    mutationFn: (action: 'engage' | 'release') => engineApi.setKillSwitch(action, reason),
    onSuccess: () => {
      setReason('')
      // Both, because engaging parks runs: the queue an operator is looking at
      // is stale the moment the switch lands.
      void qc.invalidateQueries({ queryKey: ['spec-builder', 'engine-kill-switch'] })
      void qc.invalidateQueries({ queryKey: ['spec-builder', 'engine-queue'] })
    },
    onError: (e: Error) => setErr(e.message),
  })

  const failed = configQuery.isError || switchQuery.isError
  if (failed) {
    const error = (configQuery.error || switchQuery.error) as Error | undefined
    if (error) setErr(error.message)
  }

  const view = switchQuery.data?.switch
  const engaged = view?.engaged === true
  const stoppable = switchQuery.data?.stoppable ?? []
  const queue = queueQuery.data?.entries ?? []

  return (
    <Modal open onClose={onClose} title={i18nT('apps.specBuilder.engineOps.title')}>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 20 }}>
        {/* ── the stop control ─────────────────────────────────────────── */}
        <section aria-label={i18nT('apps.specBuilder.engineOps.stop_section')}>
          <h3 style={{ margin: '0 0 6px' }}>{i18nT('apps.specBuilder.engineOps.stop_section')}</h3>
          <p style={{ margin: '0 0 8px' }}>
            {engaged
              ? i18nT('apps.specBuilder.engineOps.stop_engaged', {
                  who: view?.initiator || i18nT('apps.specBuilder.engineOps.an_operator'),
                })
              : i18nT('apps.specBuilder.engineOps.stop_released')}
          </p>
          {engaged && view?.unreadable && (
            <p style={{ margin: '0 0 8px' }}>
              <AlertTriangle className="lucide-inline" aria-hidden="true" />
              {i18nT('apps.specBuilder.engineOps.stop_unreadable')}
            </p>
          )}
          {engaged && view?.reason && (
            <p style={{ margin: '0 0 8px' }}>
              {i18nT('apps.specBuilder.engineOps.stop_reason', { reason: view.reason })}
            </p>
          )}
          {!engaged && (
            <>
              {/* The blast radius, stated before the control is thrown. */}
              <p style={{ margin: '0 0 8px' }}>
                {i18nT('apps.specBuilder.engineOps.stop_would_park', {
                  runs: stoppable.length,
                  credits: fmtNumber(switchQuery.data?.stoppable_credits ?? 0, {
                    maximumFractionDigits: 2,
                  }),
                })}
              </p>
              <Input
                value={reason}
                onChange={(e) => setReason(e.target.value)}
                placeholder={i18nT('apps.specBuilder.engineOps.stop_reason_placeholder')}
                aria-label={i18nT('apps.specBuilder.engineOps.stop_reason_placeholder')}
              />
            </>
          )}
          <div style={{ display: 'flex', gap: 8, marginTop: 8 }}>
            {engaged ? (
              <Btn
                label={
                  <>
                    <Play className="lucide-inline" aria-hidden="true" />
                    {i18nT('apps.specBuilder.engineOps.release')}
                  </>
                }
                ariaLabel={i18nT('apps.specBuilder.engineOps.release')}
                onClick={() => switchMutation.mutate('release')}
                disabled={switchMutation.isPending}
              />
            ) : (
              <Btn
                label={
                  <>
                    <OctagonX className="lucide-inline" aria-hidden="true" />
                    {i18nT('apps.specBuilder.engineOps.engage')}
                  </>
                }
                ariaLabel={i18nT('apps.specBuilder.engineOps.engage')}
                onClick={() => switchMutation.mutate('engage')}
                disabled={switchMutation.isPending}
              />
            )}
          </div>
          {engaged && (
            // Stated on the panel rather than only in the response, because it is
            // the thing an operator most often assumes otherwise.
            <p style={{ margin: '8px 0 0' }}>{i18nT('apps.specBuilder.engineOps.release_resumes_nothing')}</p>
          )}
        </section>

        {/* ── per-run spend ────────────────────────────────────────────── */}
        <section aria-label={i18nT('apps.specBuilder.engineOps.spend_section')}>
          <h3 style={{ margin: '0 0 6px' }}>{i18nT('apps.specBuilder.engineOps.spend_section')}</h3>
          {queue.length === 0 ? (
            <p style={{ margin: 0 }}>{i18nT('apps.specBuilder.engineOps.spend_empty')}</p>
          ) : (
            <>
              <table style={{ width: '100%', borderCollapse: 'collapse' }}>
                <caption style={{ textAlign: 'left' }}>
                  {i18nT('apps.specBuilder.engineOps.spend_total', {
                    credits: fmtNumber(queueQuery.data?.total_credits ?? 0, {
                      maximumFractionDigits: 2,
                    }),
                  })}
                </caption>
                <thead>
                  <tr>
                    <th scope="col" style={{ textAlign: 'left' }}>
                      {i18nT('apps.specBuilder.engineOps.col_spec')}
                    </th>
                    <th scope="col" style={{ textAlign: 'left' }}>
                      {i18nT('apps.specBuilder.engineOps.col_waiting_on')}
                    </th>
                    <th scope="col" style={{ textAlign: 'right' }}>
                      {i18nT('apps.specBuilder.engineOps.col_credits')}
                    </th>
                    <th scope="col" style={{ textAlign: 'left' }}>
                      {i18nT('apps.specBuilder.engineOps.col_detail')}
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {queue.map((row) => (
                    <tr key={row.run_id}>
                      <td>{row.spec}</td>
                      <td>{row.waiting_on}</td>
                      <td style={{ textAlign: 'right' }}>
                        {fmtNumber(row.cost_credits, { maximumFractionDigits: 2 })}
                      </td>
                      <td>
                        <Btn
                          label={i18nT('apps.specBuilder.engineOps.open_detail')}
                          ariaLabel={i18nT('apps.specBuilder.engineOps.open_detail_run', {
                            run: row.run_id,
                          })}
                          onClick={() => setDetailRun(row.run_id)}
                        />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {detailRun && <RunSpendDetail runId={detailRun} />}
            </>
          )}
        </section>

        {/* ── configuration: effective values, origins, and the editor ─── */}
        <section aria-label={i18nT('apps.specBuilder.engineOps.config_section')}>
          <h3 style={{ margin: '0 0 6px' }}>{i18nT('apps.specBuilder.engineOps.config_section')}</h3>
          <p style={{ margin: '0 0 8px' }}>{i18nT('apps.specBuilder.engineOps.config_origin_note')}</p>
          {/* Domains that are containers rather than registry settings. Named
              even when empty, so an operator can tell "nothing configured" from
              "this panel has no view of it". */}
          <h4 style={{ margin: '12px 0 4px' }}>{i18nT('apps.specBuilder.engineOps.domains_heading')}</h4>
          <ul style={{ margin: 0, paddingLeft: 18 }}>
            {(configQuery.data?.domain_sections ?? []).map((section) => {
              const entries = Object.keys(configQuery.data?.domains?.[section] ?? {})
              return (
                <li key={section}>
                  {section}
                  {': '}
                  {entries.length === 0
                    ? i18nT('apps.specBuilder.engineOps.domain_none')
                    : entries.join(', ')}
                </li>
              )
            })}
          </ul>
          {configQuery.data && <EngineConfigEditor config={configQuery.data} />}
        </section>
      </div>
    </Modal>
  )
}
