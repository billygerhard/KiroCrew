// ReviewQueuePanel — what is waiting on a person, grouped by run state, with the
// actions a row offers.
//
// Two rules this panel keeps, both of which are the reason it looks thinner than
// it could:
//
//   * **the grouping is the engine's.** It renders `grouped` from the queue
//     response, which is `QueueSnapshot.grouped()` relayed. It does NOT re-group
//     `entries`: two groupings of one run drift, and an operator reading two
//     views of the same run cannot tell which is current. It also does not invent
//     an empty group for a state with nothing in it — a permanent empty heading
//     trains people to ignore headings.
//   * **it does not manufacture an identifier the projection withholds.** A held
//     comment's id, a watched item's generation and a workspace ledger row id are
//     all absent from a queue row, two of them deliberately: the comment ids and
//     text stay behind the watcher so a queue row cannot become a second place
//     someone else's comment is copied to. So those actions ask the operator for
//     the identifier and say where to find it, rather than guessing one.
//
// The actions are privileged — a release is the human gate on quarantined
// content — and the actor is the authenticated session, decided on the server.
// Nothing here sends an actor.
import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, Send, Trash2, Unlock } from 'lucide-react'
import { engineApi, queueStateLabel, type QueueActionResponse, type QueueRow } from '../api'
import { Btn } from './shared'
import { Input } from '../../../components/ui'
import Modal from '../../../components/Modal'
import { i18nT } from '../../../i18n/t'
import { fmtDuration } from '../../../i18n/format'

export interface ReviewQueuePanelProps {
  onClose: () => void
  setErr: (msg: string) => void
}

export default function ReviewQueuePanel({ onClose, setErr }: ReviewQueuePanelProps) {
  const qc = useQueryClient()
  // Per-row text the projection cannot supply. Keyed by run id so two rows do
  // not share one field.
  const [commentIds, setCommentIds] = useState<Record<string, string>>({})
  const [generations, setGenerations] = useState<Record<string, string>>({})
  const [workspaceId, setWorkspaceId] = useState('')
  const [outcome, setOutcome] = useState('')

  // The SAME query key the spend table reads, so both surfaces render one
  // snapshot rather than two fetches that can disagree about a run.
  const queueQuery = useQuery({
    queryKey: ['spec-builder', 'engine-queue'],
    queryFn: () => engineApi.getQueue(),
  })

  if (queueQuery.isError) setErr((queueQuery.error as Error).message)

  /** Report the engine's own answer to "did anything change", and re-read. */
  const settled = (changed: boolean | undefined) => {
    setOutcome(
      changed === false
        ? i18nT('apps.specBuilder.reviewQueue.no_change')
        : i18nT('apps.specBuilder.reviewQueue.done'),
    )
    void qc.invalidateQueries({ queryKey: ['spec-builder', 'engine-queue'] })
  }

  const releaseMutation = useMutation({
    mutationFn: ({ row, commentId }: { row: QueueRow; commentId: string }) =>
      engineApi.releaseFeedback(row, commentId),
    onSuccess: (result: QueueActionResponse) => settled(result.released),
    onError: (e: Error) => setErr(e.message),
  })
  const redispatchMutation = useMutation({
    mutationFn: ({ row, generation }: { row: QueueRow; generation: number }) =>
      engineApi.redispatchItem(String(row.source), String(row.item_id), generation),
    onSuccess: (result: QueueActionResponse) => settled(result.lifted),
    onError: (e: Error) => setErr(e.message),
  })
  const teardownMutation = useMutation({
    mutationFn: (row: QueueRow) => engineApi.teardownRunWorkspaces(row.run_id),
    onSuccess: () => settled(undefined),
    onError: (e: Error) => setErr(e.message),
  })
  const cleanMutation = useMutation({
    mutationFn: (id: number) => engineApi.cleanWorkspace(id),
    onSuccess: (result: QueueActionResponse) => settled(result.removed),
    onError: (e: Error) => setErr(e.message),
  })

  const busy =
    releaseMutation.isPending ||
    redispatchMutation.isPending ||
    teardownMutation.isPending ||
    cleanMutation.isPending

  // Rendered in the order the engine sent them, which is its own
  // HUMAN_RESERVED_STATES order rather than an ordering chosen here.
  const groups = Object.entries(queueQuery.data?.grouped ?? {})

  return (
    <Modal open onClose={onClose} title={i18nT('apps.specBuilder.reviewQueue.title')}>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 20 }}>
        {outcome && (
          <p role="status" aria-live="polite" style={{ margin: 0 }}>
            {outcome}
          </p>
        )}

        {groups.length === 0 ? (
          <p style={{ margin: 0 }}>{i18nT('apps.specBuilder.reviewQueue.empty')}</p>
        ) : (
          groups.map(([state, rows]) => (
            <section key={state} aria-label={queueStateLabel(state)}>
              <h3 style={{ margin: '0 0 6px' }}>{queueStateLabel(state)}</h3>
              <table style={{ width: '100%', borderCollapse: 'collapse' }}>
                <thead>
                  <tr>
                    <th scope="col" style={{ textAlign: 'left' }}>
                      {i18nT('apps.specBuilder.reviewQueue.col_run')}
                    </th>
                    <th scope="col" style={{ textAlign: 'left' }}>
                      {i18nT('apps.specBuilder.engineOps.col_spec')}
                    </th>
                    <th scope="col" style={{ textAlign: 'left' }}>
                      {i18nT('apps.specBuilder.reviewQueue.col_gate')}
                    </th>
                    <th scope="col" style={{ textAlign: 'left' }}>
                      {i18nT('apps.specBuilder.reviewQueue.col_waiting')}
                    </th>
                    <th scope="col" style={{ textAlign: 'left' }}>
                      {i18nT('apps.specBuilder.reviewQueue.col_actions')}
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((row) => (
                    <tr key={row.run_id}>
                      <td>{row.run_id}</td>
                      <td>{row.spec}</td>
                      <td>{row.gate ?? ''}</td>
                      {/* Localised through fmtDuration rather than glued to a
                          unit literal, which is untranslatable and ungrouped. */}
                      <td>{fmtDuration([[row.waiting_s, 'second']])}</td>
                      <td style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                        {row.revision_exhausted && (
                          <span>
                            <AlertTriangle className="lucide-inline" aria-hidden="true" />
                            {i18nT('apps.specBuilder.reviewQueue.needs_human')}
                          </span>
                        )}
                        {row.feedback_needs_human && (
                          <span>
                            <AlertTriangle className="lucide-inline" aria-hidden="true" />
                            {i18nT('apps.specBuilder.reviewQueue.feedback_needs_human')}
                          </span>
                        )}
                        {!!row.feedback_quarantined && (
                          <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
                            <span>
                              {i18nT('apps.specBuilder.reviewQueue.held_comments', {
                                held: row.feedback_quarantined,
                              })}
                            </span>
                            {/* Why this is a field and not a button per comment:
                                the projection carries the COUNT, never the ids. */}
                            <span>{i18nT('apps.specBuilder.reviewQueue.held_ids_not_listed')}</span>
                            <Input
                              value={commentIds[row.run_id] ?? ''}
                              onChange={(e) =>
                                setCommentIds({ ...commentIds, [row.run_id]: e.target.value })
                              }
                              aria-label={i18nT('apps.specBuilder.reviewQueue.comment_id')}
                              placeholder={i18nT('apps.specBuilder.reviewQueue.comment_id')}
                            />
                            <Btn
                              label={
                                <>
                                  <Unlock className="lucide-inline" aria-hidden="true" />
                                  {i18nT('apps.specBuilder.reviewQueue.release')}
                                </>
                              }
                              ariaLabel={i18nT('apps.specBuilder.reviewQueue.release')}
                              disabled={busy || !(commentIds[row.run_id] ?? '').trim()}
                              onClick={() =>
                                releaseMutation.mutate({
                                  row,
                                  commentId: (commentIds[row.run_id] ?? '').trim(),
                                })
                              }
                            />
                          </div>
                        )}
                        {!!row.source && !!row.item_id && (
                          <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
                            <Input
                              value={generations[row.run_id] ?? ''}
                              onChange={(e) =>
                                setGenerations({ ...generations, [row.run_id]: e.target.value })
                              }
                              aria-label={i18nT('apps.specBuilder.reviewQueue.generation')}
                              placeholder={i18nT('apps.specBuilder.reviewQueue.generation')}
                            />
                            <Btn
                              label={
                                <>
                                  <Send className="lucide-inline" aria-hidden="true" />
                                  {i18nT('apps.specBuilder.reviewQueue.redispatch')}
                                </>
                              }
                              ariaLabel={i18nT('apps.specBuilder.reviewQueue.redispatch')}
                              disabled={busy || !Number.isFinite(Number(generations[row.run_id]))
                                || (generations[row.run_id] ?? '').trim() === ''}
                              onClick={() =>
                                redispatchMutation.mutate({
                                  row,
                                  generation: Number(generations[row.run_id]),
                                })
                              }
                            />
                          </div>
                        )}
                        <Btn
                          label={
                            <>
                              <Trash2 className="lucide-inline" aria-hidden="true" />
                              {i18nT('apps.specBuilder.reviewQueue.teardown')}
                            </>
                          }
                          ariaLabel={i18nT('apps.specBuilder.reviewQueue.teardown')}
                          disabled={busy}
                          onClick={() => teardownMutation.mutate(row)}
                        />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </section>
          ))
        )}

        {/* ── workspace cleanup ─────────────────────────────────────────
            Panel-level rather than per row: a ledger row id is not part of the
            queue projection and cannot be derived from a run, so there is no
            row to hang it on. Stated instead of guessed. */}
        <section aria-label={i18nT('apps.specBuilder.reviewQueue.cleanup_section')}>
          <h3 style={{ margin: '0 0 6px' }}>
            {i18nT('apps.specBuilder.reviewQueue.cleanup_section')}
          </h3>
          <p style={{ margin: '0 0 8px' }}>{i18nT('apps.specBuilder.reviewQueue.cleanup_note')}</p>
          <Input
            value={workspaceId}
            onChange={(e) => setWorkspaceId(e.target.value)}
            aria-label={i18nT('apps.specBuilder.reviewQueue.workspace_id')}
            placeholder={i18nT('apps.specBuilder.reviewQueue.workspace_id')}
          />
          <div style={{ marginTop: 8 }}>
            <Btn
              label={
                <>
                  <Trash2 className="lucide-inline" aria-hidden="true" />
                  {i18nT('apps.specBuilder.reviewQueue.clean')}
                </>
              }
              ariaLabel={i18nT('apps.specBuilder.reviewQueue.clean')}
              disabled={busy || !Number.isInteger(Number(workspaceId)) || workspaceId.trim() === ''}
              onClick={() => cleanMutation.mutate(Number(workspaceId))}
            />
          </div>
        </section>
      </div>
    </Modal>
  )
}
