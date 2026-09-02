/**
 * The model picker's per-harness catalog.
 *
 * Three properties, none of which the pre-harness single-key hook can express:
 *
 *  - a harness-scoped list is fetched FOR that harness (`/api/models?harness=`)
 *    and cached under its own key, so one harness's catalog can never render as
 *    another's. The default selection keeps the historical key and fetcher, which
 *    is what stops the six existing pickers from acquiring a second
 *    `--list-models` spawn — and an EXPLICIT pick of the default harness is that
 *    same selection, so it collapses onto the same key rather than fetching the
 *    identical catalog again through a shape that carries less of it.
 *  - a FAILED harness fetch renders no catalog. React Query retains `data`
 *    across a failure, and a retained list here is a set of models nobody can
 *    prove this harness serves — picked from, they are refused at the wire with
 *    an error the user cannot connect to their choice.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, screen, waitFor } from '@testing-library/react'

import { renderHookWithProviders, renderWithProviders } from './helpers'
import { useAvailableModels } from '../hooks/useAvailableModels'

const harnessModels = vi.fn()
const fetchAvailableModels = vi.fn()

vi.mock('../api/client', () => ({
  api: { harnessModels: (h: string) => harnessModels(h) },
}))

vi.mock('../providers', () => ({
  useProvider: () => ({
    id: 'acp',
    fetchAvailableModels: () => fetchAvailableModels(),
  }),
}))

describe('useAvailableModels harness scoping', () => {
  beforeEach(() => {
    harnessModels.mockReset()
    fetchAvailableModels.mockReset()
    fetchAvailableModels.mockResolvedValue([{ name: 'kiro-model', description: '' }])
  })

  it('asks the default provider when no harness is selected', async () => {
    const { result } = renderHookWithProviders(() => useAvailableModels())
    await waitFor(() => expect(result.current.map(m => m.name)).toContain('kiro-model'))
    // Never the harness endpoint: the default path must stay byte-for-byte the
    // request it always was.
    expect(harnessModels).not.toHaveBeenCalled()
  })

  it('routes an EXPLICIT default-harness pick through the historical fetcher', async () => {
    // `''` and `'kiro'` name the same catalog, and only the provider adapter's
    // rows carry the rest of it — the credit multiplier the badge renders, the
    // window sizes the meter seeds from, the degraded marking. Routing the
    // explicit id generically would strip all of that AND buy a second cache
    // entry with a second `--list-models` spawn behind it.
    const { result } = renderHookWithProviders(() => useAvailableModels({ harness: 'kiro' }))
    await waitFor(() => expect(result.current.map(m => m.name)).toContain('kiro-model'))
    expect(harnessModels).not.toHaveBeenCalled()
  })

  it('shares ONE cache entry between an absent and an explicit kiro selection', async () => {
    // Same key, so two pickers alive at once do not fetch twice.
    renderWithProviders(
      <>
        <Catalog harness="" testId="default-catalog" />
        <Catalog harness="kiro" />
      </>,
    )
    await waitFor(() => expect(screen.getByTestId('catalog').textContent).toContain('kiro-model'))
    expect(screen.getByTestId('default-catalog').textContent).toContain('kiro-model')
    expect(fetchAvailableModels).toHaveBeenCalledTimes(1)
  })

  it('asks the SELECTED harness for its own catalog', async () => {
    harnessModels.mockResolvedValue([
      { model_name: 'kas-fast', display_name: 'KAS Fast' },
      { model_name: '', display_name: 'nameless' },
    ])
    const { result } = renderHookWithProviders(() => useAvailableModels({ harness: 'kas' }))
    await waitFor(() => expect(result.current.map(m => m.name)).toContain('kas-fast'))
    expect(harnessModels).toHaveBeenCalledWith('kas')
    // The provider's own kiro catalog is not consulted, so no kiro model can
    // appear in a KAS picker.
    expect(fetchAvailableModels).not.toHaveBeenCalled()
    expect(result.current.map(m => m.name)).not.toContain('kiro-model')
    // A row with no id is dropped rather than offered as a nameless option.
    expect(result.current.map(m => m.name)).not.toContain('')
    // Auto stays first, as it is on every other picker.
    expect(result.current[0].name).toBe('auto')
  })

  it('renders auto only when the harness catalog fetch fails', async () => {
    harnessModels.mockRejectedValue(new Error('gateway unavailable'))
    const { result } = renderHookWithProviders(() => useAvailableModels({ harness: 'kas' }))
    await waitFor(() => expect(harnessModels).toHaveBeenCalled())
    await waitFor(() => expect(result.current.map(m => m.name)).toEqual(['auto']))
  })

  it('drops a catalog it can no longer confirm instead of retaining it', async () => {
    // The rule the spec names: a FAILED refetch must not leave the previous
    // answer rendered. React Query keeps `data` across a failure, so only an
    // explicit `isError` gate collapses the list — and this is the shape that
    // proves it, because a first-fetch failure has no data to retain and would
    // pass either way.
    harnessModels.mockResolvedValueOnce([{ model_name: 'kas-fast' }])
    const { queryClient } = renderWithProviders(<Catalog harness="kas" />)
    await screen.findByText(/kas-fast/)
    harnessModels.mockRejectedValue(new Error('gateway unavailable'))
    await act(async () => {
      await queryClient.refetchQueries({ queryKey: ['available-models'] })
    })
    await waitFor(() => expect(screen.getByTestId('catalog').textContent).toBe('auto'))
  })

  it('never serves one harness the catalog fetched for another', async () => {
    // Two pickers alive at once against ONE cache, which is the arrangement a
    // single shared query key silently breaks: the last fetcher to resolve would
    // decide what BOTH render, so a KAS picker would offer kiro's models and the
    // creation would be refused at the wire.
    harnessModels.mockResolvedValue([{ model_name: 'kas-fast' }])
    renderWithProviders(
      <>
        <Catalog harness="" testId="default-catalog" />
        <Catalog harness="kas" />
      </>,
    )
    await waitFor(() => expect(screen.getByTestId('catalog').textContent).toContain('kas-fast'))
    await waitFor(() =>
      expect(screen.getByTestId('default-catalog').textContent).toContain('kiro-model'),
    )
    expect(screen.getByTestId('catalog').textContent).not.toContain('kiro-model')
    expect(screen.getByTestId('default-catalog').textContent).not.toContain('kas-fast')
  })
})

/** Renders one picker's catalog as text, so two of them can be observed side by
 *  side against a single React Query cache. */
function Catalog({ harness, testId = 'catalog' }: { harness: string; testId?: string }) {
  const models = useAvailableModels(harness ? { harness } : {})
  return <span data-testid={testId}>{models.map(m => m.name).join(',')}</span>
}
