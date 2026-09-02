import { useEffect, useRef, useState } from 'react'
import { AlertTriangle, Check, Cpu } from 'lucide-react'

import { harnessRefusal, harnessRow, harnessSelectable, useHarnesses } from '../hooks/useHarnesses'
import { i18nT } from '../i18n/t'

interface HarnessSelectorProps {
  /** The session's stored harness. `''` means "inherit the configured default",
   *  and the default row is what renders as chosen for it. */
  value: string
  /** Called with the picked harness id. Never called for an unavailable or
   *  invalid row: those rows are rendered to be READ, not chosen. */
  onSelect: (id: string) => void
  /** Blocks picking while a previous pick is still being applied. The trigger
   *  stays focusable and keeps its label so the current choice is still
   *  readable. */
  disabled?: boolean
}

/**
 * Harness picker for the new-chat surface.
 *
 * Three rules, each with a failure mode the obvious alternative has:
 *
 * **An unavailable harness stays visible.** Hiding it makes a missing binary
 * indistinguishable from a harness this build does not support, so the operator
 * has nothing to act on. It is rendered, marked, carries its reason, and cannot
 * be chosen — the row is an explanation, not an option. A harness this build
 * genuinely cannot serve — registered, installed, and carrying no legacy backend
 * identifier — is marked the same way, because the two are indistinguishable to
 * someone about to click and the only alternative feedback is the error card the
 * click produces.
 *
 * **A failed listing offers nothing.** `useHarnesses` empties the rows on
 * `isError` rather than retaining the last answer, because `available` is the
 * field that goes stale: a harness whose binary was just removed would read as
 * pickable from the previous fetch and the pick would then be refused. The
 * picker says it does not know instead of guessing.
 *
 * **Invalid operator descriptors are listed apart and never selectable.** They
 * are not harnesses a session can run at all; mixing them into the selectable
 * rows would put a row that can only ever fail beside rows that work.
 */
export default function HarnessSelector({ value, onSelect, disabled }: HarnessSelectorProps) {
  const [open, setOpen] = useState(false)
  const wrapRef = useRef<HTMLDivElement>(null)
  const state = useHarnesses()
  const current = harnessRow(state, value)

  useEffect(() => {
    if (!open) return
    const handler = (e: MouseEvent) => {
      if (wrapRef.current?.contains(e.target as Node)) return
      setOpen(false)
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [open])

  // The trigger names what the session will actually run on. With no row
  // resolved it says "default" rather than inventing a harness name: an empty
  // selection genuinely is "whatever the gateway resolves", and that is also the
  // honest label while the listing is still unknown.
  const triggerLabel = current
    ? (current.display_name || current.id)
    : i18nT('components.harnessSelector.default_harness')
  // The refusal, not merely "the row says unavailable": a stored id the listing
  // does not contain at all is equally unable to serve this chat, and saying so
  // is the difference between a user who knows to pick again and one who watches
  // their first message fail.
  const refusal = harnessRefusal(state, value)

  return (
    <div ref={wrapRef} className="relative flex flex-col items-center gap-1">
      <button
        type="button"
        onClick={() => setOpen(o => !o)}
        disabled={disabled}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-label={i18nT('components.harnessSelector.harness_name', { name: triggerLabel })}
        className={`inline-flex items-center gap-1.5 h-7 px-2.5 rounded-md text-[12px] border border-border bg-transparent hover:bg-bg-hover transition-colors cursor-pointer disabled:cursor-not-allowed disabled:opacity-50 ${refusal ? 'text-warn' : 'text-muted hover:text-text'}`}
      >
        {refusal ? <AlertTriangle size={13} className="shrink-0" /> : <Cpu size={13} className="shrink-0 opacity-70" />}
        <span className="truncate min-w-0 max-w-[180px]">{triggerLabel}</span>
      </button>
      {/* The refusal is shown next to the picker, not only inside it: an
          unavailable selection blocks the first message, so the reason has to be
          readable without opening a dropdown to find it. */}
      {refusal && (
        <span className="text-[11px] text-warn max-w-[320px] text-center">
          {i18nT('components.harnessSelector.cannot_start_on_name_reason', {
            name: refusal.name,
            reason: refusal.unserviceable
              ? i18nT('components.harnessSelector.not_serviceable')
              : refusal.reason || i18nT('components.harnessSelector.not_registered'),
          })}
        </span>
      )}
      {open && (
        <div
          role="listbox"
          aria-label={i18nT('components.harnessSelector.harness_list')}
          className="absolute top-full mt-1 z-[9999] w-[300px] max-h-[320px] overflow-y-auto bg-bg-elevated border border-border rounded-xl shadow-xl p-1 flex flex-col gap-0.5"
        >
          {state.isError && (
            <div className="px-2 py-2 text-[12px] text-warn">
              {i18nT('components.harnessSelector.harness_list_unavailable')}
            </div>
          )}
          {!state.isError && state.harnesses.length === 0 && (
            <div className="px-2 py-2 text-[12px] text-muted">
              {i18nT('components.harnessSelector.no_harnesses_registered')}
            </div>
          )}
          {state.harnesses.map(h => {
            const selected = (value || state.defaultId) === h.id
            // Two independent gates, one row treatment: a harness this build
            // cannot key a provider on is as unpickable as one whose binary is
            // missing, and rendering it as an option would make the post-click
            // error card the only place it is ever explained.
            const pickable = harnessSelectable(h)
            const unserviceable = h.available && h.serviceable === false
            return (
              <button
                key={h.id}
                type="button"
                role="option"
                aria-selected={selected}
                aria-disabled={!pickable}
                disabled={!pickable}
                tabIndex={-1}
                title={h.available
                  ? (unserviceable ? i18nT('components.harnessSelector.not_serviceable') : undefined)
                  : h.reason}
                onClick={() => { if (!pickable) return; onSelect(h.id); setOpen(false) }}
                className={`w-full text-left px-2 py-1.5 rounded-lg text-[13px] flex items-start gap-1.5 bg-transparent border-none transition-colors ${pickable ? 'text-text hover:bg-bg-hover cursor-pointer' : 'text-muted cursor-not-allowed'}`}
              >
                <span className="w-3.5 shrink-0 pt-0.5">{selected && <Check size={13} />}</span>
                <span className="flex flex-col min-w-0">
                  <span className="truncate">{h.display_name || h.id}</span>
                  {!h.available && (
                    <span className="text-[11px] text-warn leading-snug">
                      {i18nT('components.harnessSelector.unavailable_reason', { reason: h.reason })}
                    </span>
                  )}
                  {unserviceable && (
                    <span className="text-[11px] text-warn leading-snug">
                      {i18nT('components.harnessSelector.not_serviceable')}
                    </span>
                  )}
                  {h.id === state.defaultId && (
                    <span className="text-[11px] text-muted/70 leading-snug">
                      {i18nT('components.harnessSelector.default')}
                    </span>
                  )}
                </span>
              </button>
            )
          })}
          {state.invalid.map(h => (
            <div
              key={`invalid-${h.id}`}
              className="w-full px-2 py-1.5 rounded-lg text-[13px] text-muted flex flex-col gap-0.5"
            >
              <span className="truncate">{h.display_name || h.id}</span>
              <span className="text-[11px] text-danger leading-snug">
                {i18nT('components.harnessSelector.invalid_reason', { reason: h.reason })}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
