import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'

import { api } from '../../api/client'
import type { AcpBackendProbe } from '../../api/client'
import { SettingsSection, SettingsCard, SettingsSelect } from '../../components/settings'
import { Badge } from '../../components/ui'
import ErrorNotice from '../../components/ErrorNotice'
import { i18nT } from '../../i18n/t'
import type { HarnessListing } from '../../types'

/**
 * Poll interval for the machine probe, in ms. Matched to the server probe's own
 * `CACHE_TTL_SECONDS` (30s): the endpoint serves that cache, so a faster poll only
 * re-reads identical bytes, and a slower one leaves a just-installed harness marked
 * missing longer than the server would. Ported from the developer AgentBackendTab.
 */
const PROBE_REFRESH_MS = 30_000

/** Query key for the registry listing. Distinct from the composer's `['harnesses']`
 *  rows because this surface consumes the WHOLE payload — the invalid entries and
 *  the resolved default as well as the pickable rows — and a shared key would hand
 *  one shape's cache to the other's reader. */
const REGISTRY_KEY = ['harnessRegistry'] as const

/** The legacy `agent.acp_backend` spellings this build can serve. Served by
 *  `/api/harnesses` (`legacy_backends`) from `ACP_BACKENDS_SELECTABLE`, which is
 *  also the PATCH allowlist's enum — restating it here would put one vocabulary in
 *  two languages with nothing failing in between, so a spelling retired on the
 *  gateway would keep being offered and Settings would write a value session
 *  creation refuses.
 *
 *  The FALLBACK when the payload carries no list is the default spelling alone: an
 *  input whose vocabulary did not arrive offers only the value that needs no
 *  vocabulary to be valid. */
const FALLBACK_LEGACY_BACKENDS = [''] as const

/** One inventory row: a registered harness, or an operator entry that failed
 *  validation, or a registered harness whose id an operator entry also claims. */
interface InventoryRow {
  id: string
  displayName: string
  available: boolean
  reason: string
  bundled: boolean
  /** True when this row exists ONLY as a rejected operator descriptor. */
  invalid: boolean
  /** The rejection reason of an operator entry that collides with this row's id. */
  conflict: string
  /** False when the harness is registered and installed and THIS BUILD still
   *  cannot start a session on it. Independent of `available`: that one heals when
   *  the operator installs the binary, this one only in a later build. */
  serviceable: boolean
}

/**
 * The machine-probe fact for one inventory row, if `GET /api/acp-backends` carried
 * one — a small projection of `AcpBackendProbe`, ported from the developer
 * AgentBackendTab so this panel can say WHAT to install and WHEN a restart is owed,
 * not merely that a row is unavailable.
 *
 * `undefined` everywhere the probe said nothing — query in flight, 403 (non-owner),
 * 404 (older gateway), an outright failure, or a row the payload omits — so a caller
 * that reads it gates and says nothing, exactly as the panel did before the endpoint
 * existed. The endpoint keys rows by `policy_id` (`kiro`/`kas`/`claude`), which is
 * the same spelling a bundled harness carries as its registry `id`; an operator
 * descriptor has no probe row and stays annotation-free.
 */
export interface RowProbe {
  installed: 'installed' | 'missing' | 'unknown'
  missingComponents: string[]
  installCommand: string
  restartRequired: boolean
  /** BUILD/edition-and-policy fact: this gateway will not serve the backend at
   *  all. Distinct from `serviceable` (which the registry already reports) — this
   *  is the governance/edition verdict the probe carries, used to HIDE a
   *  policy-denied row the same way the developer tab did. */
  selectable: boolean
}

/**
 * Join a probe payload onto one inventory row by `policy_id === row.id`.
 *
 * Returns `undefined` when there is no probe (payload absent, or no row for this
 * id) — every caller treats that as "say nothing, gate nothing". Only meaningful
 * for bundled rows: an operator descriptor's id is never a backend `policy_id`, so
 * it correctly finds no probe and carries no install-state annotation.
 */
export function rowProbe(
  probes: AcpBackendProbe[] | undefined,
  id: string,
): RowProbe | undefined {
  const hit = probes?.find(p => p.policy_id === id)
  if (!hit) return undefined
  return {
    installed: hit.installed,
    // The server contract makes `missing_components` non-empty ONLY for a MISSING
    // verdict, so this is already clamped upstream; the `?? []` guards a gateway
    // that omitted the field.
    missingComponents: Array.isArray(hit.missing_components) ? hit.missing_components : [],
    installCommand: hit.install_command || '',
    restartRequired: hit.restart_required === true,
    selectable: hit.selectable !== false,
  }
}

/**
 * The one install-state line an inventory row carries, or `''` for none.
 *
 * Order mirrors the developer tab's `status()`, because the reasons are not equally
 * actionable: `missing` first (it is the one line that says what to DO, and names
 * the command only when the server gave one), then `unknown` (a FAILED check, never
 * read as missing — telling someone to install what they may already have costs a
 * global install for nothing), then `restart_required` (installed but this gateway
 * cached its absence). An `installed` verdict with no restart owed says nothing here
 * — the registry's `Available` badge already covers it.
 */
export function installStateLine(probe: RowProbe | undefined): string {
  if (!probe) return ''
  if (probe.installed === 'missing') {
    const components = probe.missingComponents.join(', ')
    return probe.installCommand
      ? i18nT('pages.settings.harnessPanel.missing_components_with_command', {
          components,
          command: probe.installCommand,
        })
      : i18nT('pages.settings.harnessPanel.missing_components', { components })
  }
  if (probe.installed === 'unknown') return i18nT('pages.settings.harnessPanel.install_check_failed')
  if (probe.restartRequired) return i18nT('pages.settings.harnessPanel.installed_restart_required')
  return ''
}

/**
 * Merge the registry's two listings into one row per id.
 *
 * `list()` serves the rows a session can run on; `invalid()` serves operator
 * descriptors that failed validation, which are not selectable at all — so both
 * have to be read, or a broken entry of the operator's own would be invisible
 * with its reason recorded and never shown.
 *
 * The two arrays can name the SAME id: an operator entry colliding with a bundled
 * harness is rejected (its reason says the identifier is already registered) while
 * the bundled row keeps serving. Rendering both arrays blindly would then draw two
 * rows for one id, one of them a harness nobody can select — so the collision is
 * folded onto the surviving row as a conflict instead.
 */
export function mergeInventory(
  listed: HarnessListing[],
  invalid: HarnessListing[],
): InventoryRow[] {
  const rows: InventoryRow[] = listed.map(h => ({
    id: h.id,
    displayName: h.display_name || h.id,
    available: h.available,
    reason: h.reason || '',
    bundled: !!h.bundled,
    invalid: false,
    conflict: '',
    // Absent means serviceable: that is what a gateway predating the field says
    // about every row it serves, and the refusal at creation still names it.
    serviceable: h.serviceable !== false,
  }))
  const byId = new Map(rows.map(r => [r.id, r]))
  for (const entry of invalid) {
    const existing = byId.get(entry.id)
    if (existing) {
      existing.conflict = entry.reason || ''
      continue
    }
    const row: InventoryRow = {
      id: entry.id,
      displayName: entry.display_name || entry.id,
      available: false,
      reason: entry.reason || '',
      bundled: false,
      invalid: true,
      conflict: '',
      serviceable: false,
    }
    rows.push(row)
    byId.set(row.id, row)
  }
  return rows
}

/**
 * Options for the default-harness picker, as the two parallel arrays
 * `SettingsSelect` takes.
 *
 * An unavailable harness stays pickable and carries its reason in the label. The
 * registry answers for the machine as it is right now, so an operator may name the
 * default before installing the tool and the setting heals on the next listing —
 * hiding the row would make that unexpressible, and the config write gate accepts
 * any REGISTERED id for the same reason.
 *
 * `isError` collapses the list to nothing but the stored value. Availability is the
 * whole content of a listing and exactly the part that goes stale, so a fetch that
 * did not answer must not leave a harness rendered as pickable on an older answer.
 *
 * `current` — a value already in config — is unioned in when the listing lacks it,
 * so a stored default never silently disappears from the picker and a stray change
 * event cannot overwrite it.
 */
export function defaultHarnessRows(
  listed: HarnessListing[],
  isError: boolean,
  current: string,
): { values: string[]; labels: string[] } {
  const rows = isError ? [] : listed
  const values = ['', ...rows.map(h => h.id)]
  const labels = [
    i18nT('pages.settings.harnessPanel.default_harness_unset'),
    ...rows.map(h => harnessOptionLabel(h)),
  ]
  if (current && !values.includes(current)) {
    values.splice(1, 0, current)
    labels.splice(1, 0, current)
  }
  return { values, labels }
}

/** One picker option's label: the name, plus why picking it will not work.
 *
 *  Unavailability quotes the registry's recorded reason, which is data about this
 *  machine. Unserviceability does not: the verdict is identical for every such row
 *  and is a property of the build, so it is a catalog string here rather than
 *  English prose arriving over the wire. */
function harnessOptionLabel(h: HarnessListing): string {
  const name = h.display_name || h.id
  if (!h.available) {
    return i18nT('pages.settings.harnessPanel.harness_unavailable', { name, reason: h.reason })
  }
  if (h.serviceable === false) {
    return i18nT('pages.settings.harnessPanel.harness_not_serviceable', { name })
  }
  return name
}

/**
 * Options for the legacy `agent.acp_backend` alias input.
 *
 * `offered` is the vocabulary the GATEWAY says it can serve, so the set this input
 * may write is never a second copy of the Python one. An absent list (a failed
 * fetch, a gateway predating the field) collapses to the default spelling alone
 * rather than a guess.
 *
 * `stored` is the spelling as WRITTEN, not the clamped field the config GET
 * reports, so a hand-edited value outside the selectable set (`codex`) is visible
 * here instead of being rendered as kiro-cli. It is unioned in for display only:
 * the field's enum is what Settings may write, and the panel does not PATCH a
 * re-selection of the unchanged value, so seeing it cannot silently persist it.
 */
export function legacyBackendRows(
  stored: string,
  offered?: string[],
): { values: string[]; labels: string[] } {
  const values: string[] = Array.isArray(offered) && offered.length
    ? [...offered]
    : [...FALLBACK_LEGACY_BACKENDS]
  const labels = values.map(legacyBackendLabel)
  if (stored && !values.includes(stored)) {
    values.push(stored)
    labels.push(i18nT('pages.settings.harnessPanel.legacy_backend_stored', { value: stored }))
  }
  return { values, labels }
}

/** The label for one legacy spelling.
 *
 *  Static keys, one per spelling this build has copy for. A spelling the gateway
 *  offers and this dashboard has no label for renders as ITSELF rather than being
 *  hidden — the vocabulary is the gateway's, so an unlabelled entry is a missing
 *  translation, not a value nobody may pick. */
function legacyBackendLabel(value: string): string {
  if (value === '') return i18nT('pages.settings.harnessPanel.legacy_backend_kiro')
  if (value === 'kas') return i18nT('pages.settings.harnessPanel.legacy_backend_kas')
  return value
}

/**
 * AI Backend — which ACP harness Kiro Crew drives, and what the registry knows.
 *
 * Two writable settings and one inventory. The settings are the composition the
 * gateway itself resolves: `agent.default_harness` when set, else the legacy
 * `agent.acp_backend` read as an alias of a bundled descriptor. The inventory is
 * the answer to "why can I not pick that one" — every registered harness with its
 * availability and reason, whether this build can start a session on it at all,
 * plus the operator descriptors that failed validation, which no selection surface
 * will ever show.
 *
 * Harness definitions themselves (`agent.harnesses`) are deliberately NOT editable
 * here: a descriptor names a binary Kiro Crew will spawn, so it is config-file-only
 * and off the Settings PATCH allowlist. This panel reads them and reports what the
 * registry made of them.
 *
 * Nothing here retargets a running session — a session binds its harness at
 * creation and keeps it for life — and nothing needs a gateway restart: the write
 * drains the warm pool and re-reads the default for the next session.
 */
export function HarnessPanel() {
  const qc = useQueryClient()

  const regQ = useQuery({
    queryKey: REGISTRY_KEY,
    queryFn: () => api.harnesses(),
  })
  const listed = regQ.data?.harnesses ?? []
  const invalid = regQ.data?.invalid ?? []
  const resolvedDefault = regQ.data?.default ?? ''
  const storedLegacy = regQ.data?.legacy_backend ?? ''
  const offeredLegacy = regQ.data?.legacy_backends

  const cfgQ = useQuery<{ agent?: { default_harness?: string } }>({
    queryKey: ['kirocrewConfig'],
    queryFn: () => api.kirocrewConfig(),
  })
  const configuredDefault = cfgQ.data?.agent?.default_harness ?? ''

  /**
   * The machine probe, ported from the developer AgentBackendTab. `retry: false`
   * because its two expected failures — 403 (non-owner) and 404 (a gateway
   * predating the endpoint) — are permanent answers, and a rejection is never
   * surfaced: the absence of probe information is not something the reader can act
   * on, so the panel simply falls back to registry availability alone. `staleTime:
   * 0` + `refetchInterval` are load-bearing against this app's global
   * `staleTime: Infinity`: without them an operator who follows an install hint
   * would see the row stay "missing" until a full reload.
   */
  const probeQ = useQuery<{ backends: AcpBackendProbe[] }>({
    queryKey: ['acpBackends'],
    queryFn: () => api.acpBackends(),
    retry: false,
    staleTime: 0,
    refetchInterval: PROBE_REFRESH_MS,
  })
  const probes = probeQ.data?.backends

  const saveMut = useMutation({
    mutationFn: ({ path, value }: { path: string; value: string }) =>
      api.patchConfig(path, value),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['kirocrewConfig'] })
      // The resolved default is composed server-side from both keys, so a write to
      // either one changes it — refetch rather than recomputing the precedence in
      // the client, where it could disagree with what session creation does.
      void qc.invalidateQueries({ queryKey: REGISTRY_KEY })
      // The composer's picker caches the same listing under its own key with
      // staleTime Infinity, and nothing else refetches it — without this, the
      // welcome screen keeps preselecting the PREVIOUS default until a page
      // reload, and the harness-change model-drop comparison reads the stale
      // default too.
      void qc.invalidateQueries({ queryKey: ['harnesses'] })
    },
  })
  // The refusal's own prose is the diagnostic — it names the harness and lists the
  // registered ids — so it is shown, behind a translated prefix rather than as the
  // whole message: a 12-language dashboard must not render an English sentence as
  // its only explanation of a failure.
  const saveError = saveMut.isError
    ? (saveMut.error instanceof Error && saveMut.error.message
      ? `${i18nT('pages.settings.harnessPanel.failed_to_save')}: ${saveMut.error.message}`
      : i18nT('pages.settings.harnessPanel.failed_to_save'))
    : ''

  const mergedInventory = mergeInventory(listed, invalid)
  /**
   * A backend this build/policy will not serve at all is HIDDEN, not shown marked —
   * the same rule the developer AgentBackendTab applies. A greyed row invites the
   * reader to find out how to enable it, and under a managed policy there is
   * nothing they can do. The verdict is the probe's `selectable === false`; the
   * resolved default is always kept, whatever the verdict, so the panel never
   * renders an inventory with the running default missing. Probe absent (403/404/in
   * flight) hides nothing — `rowProbe` returns undefined and the row stays.
   */
  const inventory = mergedInventory.filter(row => {
    if (row.id === resolvedDefault) return true
    return rowProbe(probes, row.id)?.selectable !== false
  })
  const defaultRows = defaultHarnessRows(listed, regQ.isError, configuredDefault)
  const legacyRows = legacyBackendRows(storedLegacy, offeredLegacy)
  // The hint states what NEW SESSIONS will do, so it is gated on the listing the
  // same way the picker and the inventory are. React Query retains data across a
  // failed refetch, and this hint's own home is a `title` attribute plus a popup —
  // neither of which a reader would connect to the "list did not load" notice
  // elsewhere on the panel, so a retained availability verdict would read as
  // current with nothing contradicting it.
  const resolvedId = regQ.isError ? '' : resolvedDefault
  const resolvedRow = regQ.isError ? undefined : listed.find(h => h.id === resolvedId)
  const resolvedLabel = resolvedRow?.display_name || resolvedId
  const resolvedHint = !resolvedLabel
    ? undefined
    : resolvedRow && !resolvedRow.available
      ? i18nT('pages.settings.harnessPanel.resolves_to_unavailable', {
        name: resolvedLabel,
        reason: resolvedRow.reason,
      })
      : resolvedRow && resolvedRow.serviceable === false
        ? i18nT('pages.settings.harnessPanel.resolves_to_not_serviceable', { name: resolvedLabel })
        : i18nT('pages.settings.harnessPanel.resolves_to', { name: resolvedLabel })

  return (
    <>
      <ErrorNotice message={saveError} onDismiss={() => saveMut.reset()} className="mb-4 animate-rise" />
      {regQ.isError && (
        <div className="mb-4 text-[13px] text-danger">
          {i18nT('pages.settings.harnessPanel.failed_to_load_harnesses')}{' '}
          {/* An aria-label, not just the visible word: this panel renders inside
              Chat settings, which has retry buttons of its own, and "Retry" alone
              gives two controls the same accessible name. */}
          <button
            className="underline cursor-pointer bg-transparent border-none text-danger"
            aria-label={i18nT('pages.settings.harnessPanel.retry_harness_list')}
            onClick={() => { void regQ.refetch() }}
          >
            {i18nT('pages.settings.harnessPanel.retry')}
          </button>
        </div>
      )}

      {cfgQ.isError && (
        // "Not set" is a positive claim about the operator's configuration, and a
        // failed config GET produces exactly the same empty value as an unset key.
        // Said out loud, because the picker below is disabled either way and the
        // difference between "you have not set this" and "we could not read it" is
        // the difference between a shrug and a retry.
        <div className="mb-4 text-[13px] text-danger">
          {i18nT('pages.settings.harnessPanel.failed_to_load_config')}{' '}
          <button
            className="underline cursor-pointer bg-transparent border-none text-danger"
            aria-label={i18nT('pages.settings.harnessPanel.retry_config')}
            onClick={() => { void cfgQ.refetch() }}
          >
            {i18nT('pages.settings.harnessPanel.retry')}
          </button>
        </div>
      )}

      <SettingsSection title={i18nT('pages.settings.harnessPanel.ai_backend')}>
        <SettingsCard>
          <SettingsSelect
            label={i18nT('pages.settings.harnessPanel.default_harness')}
            description={i18nT('pages.settings.harnessPanel.which_harness_new_sessions_start_on')}
            hint={resolvedHint}
            value={configuredDefault}
            options={defaultRows.values}
            optionLabels={defaultRows.labels}
            onChange={v => saveMut.mutate({ path: 'agent.default_harness', value: v })}
            // Both queries must have ANSWERED, not merely not-failed: while the
            // listing is still in flight the options are the stored value alone, so
            // an enabled control would let a write be issued from a vocabulary that
            // has not arrived.
            disabled={!cfgQ.isSuccess || !regQ.isSuccess}
            configKey="agent.default_harness"
          />
          <SettingsSelect
            label={i18nT('pages.settings.harnessPanel.legacy_backend')}
            description={i18nT('pages.settings.harnessPanel.the_older_key_read_as_an_alias_of_a_harness')}
            hint={
              configuredDefault
                ? i18nT('pages.settings.harnessPanel.legacy_backend_outranked')
                : undefined
            }
            value={storedLegacy}
            options={legacyRows.values}
            optionLabels={legacyRows.labels}
            onChange={v => {
              // A stored spelling outside the enum is display-only. Re-selecting it
              // would be refused by the write gate, so an unchanged value writes
              // nothing rather than showing the operator an error for touching a
              // control without changing it.
              if (v === storedLegacy) return
              saveMut.mutate({ path: 'agent.acp_backend', value: v })
            }}
            disabled={!regQ.isSuccess}
            configKey="agent.acp_backend"
          />
        </SettingsCard>
      </SettingsSection>

      <SettingsSection title={i18nT('pages.settings.harnessPanel.registered_harnesses')}>
        <SettingsCard>
          <div className="text-[12px] text-muted">
            {i18nT('pages.settings.harnessPanel.harnesses_are_defined_in_config_and_authenticate')}
          </div>
          {regQ.isError ? (
            // Availability is the content of every row here, so a listing that did
            // not answer renders nothing rather than a stale verdict.
            <div className="text-[13px] text-muted py-2">
              {i18nT('pages.settings.harnessPanel.harness_list_unavailable')}
            </div>
          ) : !regQ.isSuccess ? null : inventory.length === 0 ? (
            // Only after a successful listing: "none registered" is a claim, and
            // making it while the fetch is still in flight states it of every
            // install for the first frame.
            <div className="text-[13px] text-muted py-2">
              {i18nT('pages.settings.harnessPanel.no_harnesses_registered')}
            </div>
          ) : (
            <ul className="list-none p-0 m-0 flex flex-col gap-2" data-testid="harness-inventory">
              {inventory.map(row => {
                // The machine probe for this row, if the endpoint carried one.
                // Bundled rows join by policy_id === id; operator descriptors find
                // none and render exactly as before.
                const probe = rowProbe(probes, row.id)
                const installLine = installStateLine(probe)
                return (
                <li key={row.id} className="flex flex-col gap-1 py-1.5 border-b border-border last:border-b-0" data-testid={`harness-row-${row.id}`}>
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className="text-[13px] font-semibold text-text-strong">{row.displayName}</span>
                    <span className="text-[12px] text-muted font-mono">{row.id}</span>
                    <Badge variant={row.bundled ? 'muted' : 'aim'}>
                      {row.bundled
                        ? i18nT('pages.settings.harnessPanel.badge_bundled')
                        : i18nT('pages.settings.harnessPanel.badge_operator')}
                    </Badge>
                    <Badge variant={row.invalid ? 'err' : row.available ? 'ok' : 'warn'}>
                      {row.invalid
                        ? i18nT('pages.settings.harnessPanel.badge_invalid')
                        : row.available
                          ? i18nT('pages.settings.harnessPanel.badge_available')
                          : i18nT('pages.settings.harnessPanel.badge_unavailable')}
                    </Badge>
                    {/* Install-state badges from the machine probe, ported from the
                        developer tab. `missing` is the one that tells the reader
                        what to DO; `restart_required` says the binary IS present but
                        this gateway cached its absence. `unknown` (a failed check)
                        carries no badge — it is not a verdict, only a line below. */}
                    {probe?.installed === 'missing' && (
                      <Badge variant="warn" data-testid={`harness-missing-${row.id}`}>
                        {i18nT('pages.settings.harnessPanel.badge_missing')}
                      </Badge>
                    )}
                    {probe?.installed === 'installed' && probe.restartRequired && (
                      <Badge variant="warn" data-testid={`harness-restart-${row.id}`}>
                        {i18nT('pages.settings.harnessPanel.badge_restart_required')}
                      </Badge>
                    )}
                    {/* A second badge rather than a different availability word:
                        the harness IS available — installed, resolvable, listed —
                        and what stops it is this build. Collapsing the two would
                        tell the operator to fix an install that is already fine. */}
                    {!row.invalid && row.available && !row.serviceable && (
                      <Badge variant="warn">
                        {i18nT('pages.settings.harnessPanel.badge_not_serviceable')}
                      </Badge>
                    )}
                  </div>
                  {row.reason && (
                    <div className="text-[12px] text-muted">{row.reason}</div>
                  )}
                  {/* The machine-probe line: which components are absent (with the
                      install command when the server gave one), or that the check
                      failed, or that a restart is owed. Says only what the server
                      measured on THIS machine — never a claim about what the harness
                      supports. */}
                  {installLine && (
                    <div className="text-[12px] text-warn" data-testid={`harness-install-${row.id}`}>
                      {installLine}
                    </div>
                  )}
                  {!row.invalid && row.available && !row.serviceable && (
                    <div className="text-[12px] text-warn">
                      {i18nT('pages.settings.harnessPanel.not_serviceable_note')}
                    </div>
                  )}
                  {row.conflict && (
                    <div className="text-[12px] text-warn">
                      {i18nT('pages.settings.harnessPanel.operator_entry_conflicts', { reason: row.conflict })}
                    </div>
                  )}
                </li>
                )
              })}
            </ul>
          )}
        </SettingsCard>
      </SettingsSection>
    </>
  )
}
