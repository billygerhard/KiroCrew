import { useQuery } from '@tanstack/react-query'

import { api } from '../api/client'
import { acpBackendName, configuredAcpBackend, type AcpBackendConfig } from '../api/acpBackend'
import type { AcpBackendProbe } from '../api/client/config'

/** One backend a chat can be put on. `id` is the stored spelling a chat's pick
 *  carries (Kiro CLI is `'kiro'`); `label` is its display name. */
export interface BackendRow {
  id: string
  label: string
}

export interface BackendListingState {
  backends: BackendRow[]
  /** The configured default's id: what an unpicked chat runs on. */
  defaultId: string
  isError: boolean
  isLoading: boolean
}

const EMPTY: BackendRow[] = []

/** Offered rows: the build and policy allow it, and this machine is not known to
 *  lack it. An `unknown` install verdict is offered, since starting is what
 *  settles it. */
export function selectableBackendRows(rows: AcpBackendProbe[]): BackendRow[] {
  return rows
    .filter(r => r.selectable && r.installed !== 'missing')
    .map(r => ({ id: r.id, label: acpBackendName(r) }))
}

/**
 * The backends a chat may run on, from `GET /api/acp-backends` (built-ins and
 * harnesses.json backends alike), with the configured default.
 *
 * A failed listing offers nothing rather than the last answer: selectability is
 * a statement about the build and policy right now, and a retained list could
 * offer a pick the gateway now refuses.
 */
export function useBackends(): BackendListingState {
  const listing = useQuery({
    queryKey: ['acpBackends'],
    queryFn: () => api.acpBackends(),
  })
  const cfg = useQuery<AcpBackendConfig>({
    queryKey: ['kirocrewConfig'],
    queryFn: () => api.kirocrewConfig(),
  })
  const rows = listing.data?.backends
  return {
    backends: !listing.isError && Array.isArray(rows) ? selectableBackendRows(rows) : EMPTY,
    defaultId: cfg.data ? configuredAcpBackend(cfg.data) : '',
    isError: listing.isError,
    isLoading: listing.isLoading,
  }
}

/** The row a chat's stored pick names; `null` (or `''`) follows the default. */
export function backendRow(state: BackendListingState, pick: string | null | undefined): BackendRow | undefined {
  const wanted = pick || state.defaultId
  return state.backends.find(b => b.id === wanted)
}

/** Whether there is anything to pick: more than one backend, or a pin to read. */
export function backendChoiceExists(state: BackendListingState, pick: string | null | undefined): boolean {
  return state.backends.length > 1 || !!pick
}
