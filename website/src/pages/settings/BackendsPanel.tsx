import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'

import { BookOpen, ExternalLink } from 'lucide-react'
import { useNavigate } from 'react-router-dom'

import { api } from '../../api/client'
import type { BackendVerifyResult } from '../../api/client'
import { SettingsSection, SettingsCard } from '../../components/settings'
import ErrorNotice from '../../components/ErrorNotice'
import { Badge, Btn } from '../../components/ui'
import { i18nT } from '../../i18n/t'

/** The operator's path from "I want to add a backend" to a working descriptor.
 *  Same repo-doc link convention as the Connections and Discord panels. */
const AUTHORING_GUIDE =
  'https://github.com/kirodotdev/KiroCrew/blob/main/docs/system-specs/modules/backend-authoring.md'

/** Where the GLOBAL default is changed: the Developer page's Agent Backend tab
 *  (same route the Kiro sign-in deep link uses, `kiroSignInLink.ts`). */
const AGENT_BACKEND_TAB_PATH = '/developer?tab=agent-backend'

/** Query key for the panel's full listing. Distinct from the composer's
 *  `['backends']` rows so the two readers cannot hand each other a shape: this
 *  surface consumes the invalid/unroutable diagnostics too, and a shared key
 *  would let one shape's cache decide the other's content. */
const REGISTRY_KEY = ['backendsRegistry'] as const

/**
 * AI Backends — the inventory the descriptor-harness registry made of what this
 * build offers and what an operator wrote.
 *
 * Read-only, by design (D3/D5). Three groups:
 *
 * - **Selectable** backends — every id a new chat may run on (id + display
 *   label), with the global default marked. This is the same set the composer's
 *   picker offers, from the single selectability owner.
 * - **Unroutable** operator descriptors — spellable, but they declared no
 *   verified routing, so the gateway refused them selectability. Shown with the
 *   reason so the operator sees WHY their entry is not offered.
 * - **Invalid** operator descriptors — failed validation, so they are not even
 *   spellable. Shown with every reason they were rejected.
 *
 * Backend DEFINITIONS are deliberately not editable here: a descriptor names a
 * binary Kiro Crew will spawn, so it is config-file-only (`harnesses.json`,
 * write-protected — see D5) and off the Settings PATCH allowlist. Editing it
 * needs a gateway restart to re-run the boot-load. This panel reads the registry
 * and reports what it made of those definitions.
 *
 * The GLOBAL default selector is NOT duplicated here — it lives in its own
 * surface (the developer AgentBackendTab) and stays there; this panel only
 * REPORTS which backend is currently the default.
 */
export function BackendsPanel() {
  const navigate = useNavigate()
  const qc = useQueryClient()
  const regQ = useQuery({
    queryKey: REGISTRY_KEY,
    queryFn: () => api.backends(),
  })
  // The last verify outcome, kept per row so the message sits under the row it
  // is about. A verified verdict also refetches the listing: the row leaves the
  // unroutable list and appears among the selectable ones.
  const [verifyOutcome, setVerifyOutcome] = useState<
    { id: string; verdict: BackendVerifyResult['verdict'] | 'error'; reason: string } | null
  >(null)
  const verify = useMutation({
    mutationFn: (id: string) => api.verifyBackend(id),
    onSuccess: (result, id) => {
      if (result.error && !result.verdict) {
        setVerifyOutcome({ id, verdict: 'error', reason: result.error })
        return
      }
      setVerifyOutcome({ id, verdict: result.verdict, reason: result.reason })
      if (result.verified) void qc.invalidateQueries({ queryKey: REGISTRY_KEY })
    },
    onError: (err, id) => {
      setVerifyOutcome({ id, verdict: 'error', reason: err instanceof Error ? err.message : String(err) })
    },
  })
  const selectable = regQ.data?.backends ?? []
  const unroutable = regQ.data?.unroutable ?? []
  const invalid = regQ.data?.invalid ?? []
  const total = selectable.length + unroutable.length + invalid.length

  return (
    <>
      {/* The tab header already reads "AI Backends"; the section names what the
          card lists so the two headings are not the same line twice. */}
      <SettingsSection title={i18nT('pages.settings.backendsPanel.registered_backends')}>
        <SettingsCard>
          <div
            className="text-[12px] text-muted flex flex-col gap-1"
            data-setting-label={i18nT('pages.settings.backendsPanel.ai_backends')}
          >
            <span>
              {i18nT('pages.settings.backendsPanel.backends_are_defined_in_config_and_need_a_restart')}
            </span>
            <a
              href={AUTHORING_GUIDE}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-1 text-accent hover:underline self-start"
            >
              <BookOpen className="w-3.5 h-3.5" aria-hidden="true" />
              {i18nT('pages.settings.backendsPanel.authoring_guide')}
            </a>
            {/* This panel REPORTS the default; the switch that changes it is the
                Developer page's Agent Backend tab (always routed, sidebar entry
                behind Developer Mode). Without the pointer a user who lands
                here to change the default dead-ends on a badge. */}
            <button
              type="button"
              onClick={() => navigate(AGENT_BACKEND_TAB_PATH)}
              className="inline-flex items-center gap-1 text-accent bg-transparent border-none cursor-pointer p-0 hover:underline self-start"
            >
              {i18nT('pages.settings.backendsPanel.change_default_in_developer')}
              <ExternalLink size={13} className="lucide-inline" aria-hidden="true" />
            </button>
          </div>
          {regQ.isError ? (
            // Selectability + the diagnostics are the whole content of every row,
            // so a listing that did not answer renders nothing rather than a
            // stale verdict. The failure routes through the shared error surface
            // (structured recovery context + Ask Agent), not a hand-written line.
            <ErrorNotice
              variant="inline"
              askAgent
              className="py-2"
              testId="backends-panel-list-error"
              message={i18nT('pages.settings.backendsPanel.backend_list_unavailable')}
            />
          ) : !regQ.isSuccess ? null : total === 0 ? (
            // Only after a successful listing: "none registered" is a claim, and
            // stating it while the fetch is still in flight would assert it of
            // every install for the first frame.
            <div className="text-[13px] text-muted py-2">
              {i18nT('pages.settings.backendsPanel.no_backends_registered')}
            </div>
          ) : (
            <ul className="list-none p-0 m-0 flex flex-col gap-2" data-testid="backend-inventory">
              {selectable.map(row => (
                <li
                  key={row.id || '__kiro__'}
                  className="flex flex-col gap-1 py-1.5 border-b border-border last:border-b-0"
                  data-testid={`backend-row-${row.id || 'kiro'}`}
                >
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className="text-[13px] font-semibold text-text-strong">{row.label || row.id}</span>
                    {row.id && <span className="text-[12px] text-muted font-mono">{row.id}</span>}
                    {/* Selectable is the normal state and carries no badge; only
                        Default (informative) and the two exceptional states below
                        (Not selectable / Invalid) are badged, so the loud row is
                        the one that needs attention. */}
                    {row.is_global_default && (
                      <Badge variant="aim" data-testid={`backend-default-${row.id || 'kiro'}`}>
                        {i18nT('pages.settings.backendsPanel.badge_default')}
                      </Badge>
                    )}
                  </div>
                </li>
              ))}
              {unroutable.map(row => (
                <li
                  key={`unroutable-${row.id}`}
                  className="flex flex-col gap-1 py-1.5 border-b border-border last:border-b-0"
                  data-testid={`backend-unroutable-${row.id}`}
                >
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className="text-[13px] font-semibold text-text-strong">{row.label || row.id}</span>
                    <span className="text-[12px] text-muted font-mono">{row.id}</span>
                    <Badge variant="warn">
                      {row.verifiable
                        ? i18nT('pages.settings.backendsPanel.badge_unverified')
                        : i18nT('pages.settings.backendsPanel.badge_unroutable')}
                    </Badge>
                    {row.verifiable && (
                      /* The one action on this row: a declared routing is a claim,
                         and this runs the gateway's own end-to-end probe (spawn the
                         harness, ask for one file write, deny every permission
                         request, check nothing was written). Success records the
                         attestation and the row moves to the selectable list. */
                      <Btn
                        onClick={() => verify.mutate(row.id)}
                        disabled={verify.isPending}
                        aria-busy={verify.isPending && verify.variables === row.id}
                        data-testid={`backend-verify-${row.id}`}
                      >
                        {verify.isPending && verify.variables === row.id
                          ? i18nT('pages.settings.backendsPanel.verifying')
                          : i18nT('pages.settings.backendsPanel.verify_routing')}
                      </Btn>
                    )}
                  </div>
                  <div className="text-[12px] text-warn">{row.reason}</div>
                  {verifyOutcome?.id === row.id && (
                    verifyOutcome.verdict === 'verified' ? (
                      <div className="text-[12px] text-ok" role="status" data-testid={`backend-verify-outcome-${row.id}`}>
                        {i18nT('pages.settings.backendsPanel.verify_passed')}
                      </div>
                    ) : (
                      <ErrorNotice
                        variant="inline"
                        message={verifyOutcome.reason}
                        title={verifyOutcome.verdict === 'violation'
                          ? i18nT('pages.settings.backendsPanel.verify_violation')
                          : i18nT('pages.settings.backendsPanel.verify_inconclusive')}
                        askAgent
                        testId={`backend-verify-outcome-${row.id}`}
                      />
                    )
                  )}
                </li>
              ))}
              {invalid.map(row => (
                <li
                  key={`invalid-${row.id}`}
                  className="flex flex-col gap-1 py-1.5 border-b border-border last:border-b-0"
                  data-testid={`backend-invalid-${row.id}`}
                >
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className="text-[13px] font-semibold text-text-strong">{row.label || row.id}</span>
                    <span className="text-[12px] text-muted font-mono">{row.id}</span>
                    <Badge variant="err">
                      {i18nT('pages.settings.backendsPanel.badge_invalid')}
                    </Badge>
                  </div>
                  {row.reasons.map((reason, i) => (
                    <ErrorNotice
                      key={i}
                      variant="inline"
                      message={reason}
                      messageClassName="font-mono"
                      askAgent
                      testId={`backend-invalid-${row.id}-reason-${i}`}
                    />
                  ))}
                </li>
              ))}
            </ul>
          )}
        </SettingsCard>
      </SettingsSection>
    </>
  )
}
