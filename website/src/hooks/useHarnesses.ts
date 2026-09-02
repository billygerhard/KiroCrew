import { useQuery } from '@tanstack/react-query'

import { api } from '../api/client'
import type { HarnessListing } from '../types'

/** What every harness picker reads: the registered rows, the ids that failed
 *  validation, and which harness an unselected creation actually lands on. */
export interface HarnessListingState {
  /** Selectable rows, unavailable ones INCLUDED and marked. A surface renders
   *  those visible with their reason rather than hiding them, so one missing
   *  binary looks like a harness that needs installing instead of one this build
   *  does not support. */
  harnesses: HarnessListing[]
  /** Operator descriptors that failed validation. Never selectable — served
   *  apart from `harnesses` so a row a session can never run cannot be picked by
   *  a surface that forgot to check `available`. */
  invalid: HarnessListing[]
  /** The harness a session with no selection is created on. Preselect THIS, so
   *  the highlighted row cannot disagree with what an unselected creation does. */
  defaultId: string
  /** The listing fetch did not answer. Both arrays are empty in this state. */
  isError: boolean
  isLoading: boolean
}

const EMPTY: HarnessListing[] = []

/**
 * THE harness listing. Every selection surface reads it through here.
 *
 * ## Why one hook and not a `useQuery` per surface
 *
 * The same reason `useAvailableModels` exists: React Query stores one cache
 * entry per key and the fetching observer's options win, so two surfaces sharing
 * the `['harnesses']` key while declaring their own `queryFn` let *whichever
 * fetched last* decide the shape everyone reads. The cron editor's fetcher
 * returned only the rows array; a composer that also needed `default` would have
 * silently emptied it for the editor on every refetch, in a file three
 * directories away from the symptom.
 *
 * ## Why `isError` empties the list instead of keeping the last one
 *
 * React Query retains `data` across a failed refetch, and for most resources
 * that is the right call. Not for this one: `available` is a statement about the
 * machine *right now* and it is exactly the field that goes stale — a harness
 * whose binary was just removed reads as pickable from the previous answer, and
 * the surface then offers a creation it knows will be refused. An empty list is
 * an honest "we do not know yet"; a retained one is a confident wrong answer.
 */
export function useHarnesses(): HarnessListingState {
  const { data, isError, isLoading } = useQuery({
    queryKey: ['harnesses'],
    queryFn: async () => {
      const r = await api.harnesses()
      return {
        harnesses: Array.isArray(r?.harnesses) ? r.harnesses : [],
        invalid: Array.isArray(r?.invalid) ? r.invalid : [],
        // `default` is resolved by the gateway from the same two config keys
        // session creation reads, so it is never derived here from the rows.
        defaultId: typeof r?.default === 'string' ? r.default : '',
      }
    },
  })
  if (isError || !data) {
    return { harnesses: EMPTY, invalid: EMPTY, defaultId: '', isError, isLoading }
  }
  return { ...data, isError, isLoading }
}

/** The row for `id`, or undefined. `id` empty means "inherit the default", which
 *  resolves to the default row — that is what the surface must display, because
 *  it is what an unselected creation will really run on. */
export function harnessRow(
  state: HarnessListingState,
  id: string,
): HarnessListing | undefined {
  const wanted = id || state.defaultId
  if (!wanted) return undefined
  return state.harnesses.find(h => h.id === wanted)
}

/** Whether a chat can be STARTED on this row.
 *
 *  Two independent gates, and a surface that checks only the first offers a row
 *  the gateway refuses: `available` is about the machine (binary present, recent
 *  start clean) and `serviceable` is about this build (it can key a provider on
 *  this harness at all). A gateway that predates the second field says nothing
 *  about it, and "nothing" reads as serviceable — the same posture as before it
 *  existed, and the gateway refuses at creation either way. */
export function harnessSelectable(row: HarnessListing): boolean {
  return row.available && row.serviceable !== false
}

/**
 * Why a chat cannot start on `id`, or `null` when nothing stands in the way.
 *
 * `null` is also the answer while the listing is unknown (loading, or a failed
 * fetch): this must not invent a refusal from an answer it does not have. The
 * gateway resolves the same selection again at creation and refuses with the
 * harness named, so a missed pre-emptive notice costs a server-side error card
 * rather than a session on the wrong harness. The reverse — declaring a harness
 * unusable because a listing did not arrive — would make a perfectly good
 * harness look broken with nothing the user could do about it.
 *
 * `reason` is empty exactly when the id is not in the listing at all, which the
 * caller renders as "not registered": the registry said nothing about it, so
 * there is no recorded reason to quote.
 *
 * `unserviceable` says the row is registered and the machine is fine and THIS
 * BUILD still cannot start a session on it. It is flagged rather than described
 * because the explanation is the same for every such row, so it belongs in the
 * caller's catalog instead of arriving as English prose from the gateway.
 */
export function harnessRefusal(
  state: HarnessListingState,
  id: string,
): { name: string; reason: string; unserviceable: boolean } | null {
  if (state.isError || state.isLoading) return null
  const row = harnessRow(state, id)
  if (!row) {
    // An empty selection with no default row resolved is the pre-harness world:
    // there is nothing to refuse, and the spawn reports its own failures exactly
    // as it always has.
    return id ? { name: id, reason: '', unserviceable: false } : null
  }
  const name = row.display_name || row.id
  if (!row.available) return { name, reason: row.reason, unserviceable: false }
  if (row.serviceable === false) return { name, reason: '', unserviceable: true }
  return null
}
