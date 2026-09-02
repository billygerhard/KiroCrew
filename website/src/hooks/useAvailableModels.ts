import { useQuery } from '@tanstack/react-query'

import { api } from '../api/client'
import { useProvider } from '../providers'
import { modelListRefetchInterval } from '../providers/modelListHealth'
import { withAutoFirst } from '../providers/modelList'
import type { ModelInfo } from '../providers/types'

/** Auto-only list used before the first fetch resolves.
 *
 *  `description: ''` for the same reason as `withAutoFirst`: Auto's short label
 *  is a catalog key resolved where it renders, not an English literal living in
 *  a data module. */
const PLACEHOLDER: ModelInfo[] = [{ name: 'auto', description: '' }]

/** The bundled default harness's id, as `/api/harnesses` spells it.
 *
 *  Named here because it decides ROUTING, not display: an explicit pick of the
 *  default harness has to land on the historical fetcher, not on the generic
 *  per-harness one. `/api/models` with no parameter already answers for this
 *  harness through the provider adapter, and that adapter is the only path that
 *  carries the rest of a kiro row — the credit multiplier the badge renders, the
 *  context/learn windows the meter seeds from, the degraded marking — none of
 *  which `/api/models?harness=` reproduces. Routing it generically would also buy
 *  a second cache entry and a second `--list-models` spawn for the same catalog. */
const DEFAULT_HARNESS_ID = 'kiro'

/**
 * THE model list. Every picker reads it through here.
 *
 * ## Why a hook and not six `useQuery` calls
 *
 * Six surfaces render a model picker (ChatPage, ChatPane, ChatSidebar's bulk
 * switcher, AgentsPage, Settings ▸ Chat, KiroCrewAgentsPage) and all six used
 * the SAME query key — deliberately, so kiro-cli's `--list-models` is spawned
 * once — while each declared its own `queryFn`. React Query stores one cache
 * entry per key and the fetching observer's options win, so with divergent
 * fetchers the array every picker reads is decided by *which surface fetched
 * last*.
 *
 * That was not theoretical. Three shapes were live at once: four surfaces
 * returned `withAutoFirst(models)`, Settings ▸ Chat returned a hand-built
 * `[{name:'auto',description:'Default'}, ...rest]` that discarded everything
 * the live Auto row carried, and KiroCrewAgentsPage returned the raw list with
 * no Auto-first ordering at all. Opening Settings ▸ Chat replaced the shared
 * cache with the stripped shape, so Auto's credit-multiplier badge vanished
 * from every other picker until one of them refetched — a flicker whose cause
 * is three files away from the symptom.
 *
 * One key with one fetcher makes that class of bug unrepresentable: a caller
 * cannot supply a shape, only read one.
 *
 * `enabled` is the one option callers still control, because it is per-observer
 * and cannot corrupt the cached value: ChatSidebar's bulk switcher passes
 * `false` until its panel opens so merely rendering the sidebar does not spawn
 * kiro-cli. Other mounted observers still fetch normally — `enabled` gates who
 * *triggers* a fetch, not what lands in the cache.
 *
 * `harness` selects WHOSE catalog: absent/empty is the default harness and keeps
 * the historical key and fetcher untouched, a non-empty id gets its own cache
 * entry fed by `/api/models?harness=<id>`. Per-harness keys are what keep one
 * harness's models from ever rendering as another's — the same reason the
 * gateway refuses to serve a catalog it was not asked for. An EXPLICIT pick of
 * the default harness is the same catalog as no pick at all, so it collapses onto
 * the historical path rather than acquiring a second entry (see
 * `DEFAULT_HARNESS_ID`).
 */
export function useAvailableModels({ enabled, harness }: { enabled?: boolean; harness?: string } = {}): ModelInfo[] {
  const provider = useProvider()
  // `''` and the default harness's own id are ONE selection: both mean "the
  // catalog `/api/models` answers with", and treating them as two would split the
  // cache and re-fetch the same list under a shape that carries less of it.
  const picked = harness || ''
  const harnessId = picked === DEFAULT_HARNESS_ID ? '' : picked
  // Per-harness cache entry, and a per-harness FETCHER: `/api/models` with no
  // parameter answers for kiro-cli, so a harness-scoped list must ask for its own
  // catalog explicitly. The default (`''`) branch keeps BOTH the historical key
  // and the historical fetcher, so every surface that never learned about
  // harnesses shares exactly the cache entry it always did — one `--list-models`
  // spawn, one shape.
  const { data, isError } = useQuery({
    queryKey: harnessId ? ['available-models', provider.id, harnessId] : ['available-models', provider.id],
    queryFn: async () => withAutoFirst(
      harnessId ? await fetchHarnessModels(harnessId) : await provider.fetchAvailableModels(),
    ),
    refetchInterval: modelListRefetchInterval,
    ...(enabled === undefined ? {} : { enabled }),
  })
  // A failed fetch must not leave ANOTHER answer rendered as this harness's
  // catalog. React Query retains `data` across a failure, and a retained list
  // here is a list of models the picker cannot prove this harness serves — picked
  // from, it fails at the wire with an error the user cannot connect to their
  // choice. Auto-only is the honest fallback: it is what "let the harness decide"
  // means, and it is the one entry that needs no catalog to be valid.
  if (isError) return PLACEHOLDER
  return data ?? PLACEHOLDER
}

/** One harness's catalog in the shape the pickers render.
 *
 *  Rows come back in `/api/models`'s own shape (`model_name` / `display_name`),
 *  which is what the kiro adapter also normalizes, so a harness-scoped list and
 *  the default list are interchangeable downstream. A row with no id is dropped
 *  rather than rendered as a nameless option. */
async function fetchHarnessModels(harness: string): Promise<ModelInfo[]> {
  const rows = await api.harnessModels(harness)
  if (!Array.isArray(rows)) return []
  return rows
    .map((r: { model_name?: string; name?: string; display_name?: string; description?: string }) => ({
      name: String(r?.model_name || r?.name || ''),
      description: String(r?.display_name || r?.description || ''),
    }))
    .filter(m => !!m.name)
}
