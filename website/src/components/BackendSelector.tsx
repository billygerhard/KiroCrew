import { useCallback, useEffect, useRef, useState, type CSSProperties } from 'react'
import { createPortal } from 'react-dom'
import { Check, ChevronDown, Cpu } from 'lucide-react'

import { backendRow, useBackends } from '../hooks/useBackends'
import { useListboxKeyboard } from '../hooks/useListboxKeyboard'
import ErrorNotice from './ErrorNotice'
import { i18nT } from '../i18n/t'

interface BackendSelectorProps {
  /** The chat's stored pick. `null` (or `''`) follows the configured default,
   *  and the default row is what renders as chosen for it. */
  value: string | null
  /** Called with the picked row's id. */
  onSelect: (id: string) => void
  /** Blocks picking while a turn runs or a pick is applying. The trigger keeps
   *  its label, so the backend serving the chat stays readable. */
  disabled?: boolean
  /** `data-testid` on the trigger. */
  triggerTestId?: string
}

/**
 * The composer's backend chip: which backend this chat runs on (D8 vocabulary:
 * "backend"), and the picker for it.
 *
 * Rows come from `GET /api/acp-backends` through `useBackends`, so built-ins and
 * harnesses.json backends ("Claude Seat 1") are listed alike, under the names the
 * gateway gives them. A pick applies between turns: the next message starts its
 * session on the new backend, and a backend in the same session family resumes
 * the conversation itself.
 */
export default function BackendSelector({ value, onSelect, disabled, triggerTestId }: BackendSelectorProps) {
  const [open, setOpen] = useState(false)
  const wrapRef = useRef<HTMLDivElement>(null)
  const btnRef = useRef<HTMLButtonElement>(null)
  const listRef = useRef<HTMLDivElement>(null)
  // No filter input in this picker; the hook still needs the ref slot.
  const inputRef = useRef<HTMLElement | null>(null)
  const state = useBackends()
  const current = backendRow(state, value)

  useEffect(() => {
    if (!open) return
    const handler = (e: MouseEvent) => {
      const target = e.target as Node
      // The list is portaled out of wrapRef, so it needs its own check or a tap
      // on an option closes the menu before the option's click lands.
      if (wrapRef.current?.contains(target) || listRef.current?.contains(target)) return
      setOpen(false)
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [open])

  // A turn starting while the list is open closes it: a pick made then would be
  // refused, and the trigger is about to lock.
  useEffect(() => { if (disabled) setOpen(false) }, [disabled])

  const closeToTrigger = useCallback(() => {
    setOpen(false)
    btnRef.current?.focus()
  }, [])
  const { onListKeyDown } = useListboxKeyboard({
    open,
    dropdownRef: listRef,
    inputRef,
    hasFilterInput: false,
    filteredCount: state.backends.length,
    onEnterSingleMatch: () => {},
    closeToTrigger,
  })

  // Names what the chat will run on; with no row resolved it says "default"
  // rather than inventing a name, which is also honest while the list loads.
  const triggerLabel = current ? current.label : (value || i18nT('components.backendSelector.default_backend'))
  const triggerText = i18nT('components.backendSelector.backend_name', { name: triggerLabel })
  const chosenId = value || state.defaultId

  return (
    <div ref={wrapRef} className="relative flex items-center shrink-0 min-w-0">
      <button
        ref={btnRef}
        type="button"
        onClick={() => setOpen(o => !o)}
        disabled={disabled}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-label={triggerText}
        data-testid={triggerTestId}
        className="inline-flex items-center gap-1.5 h-7 px-2.5 rounded-md text-[12px] border border-border bg-transparent hover:bg-bg-hover transition-colors cursor-pointer disabled:cursor-not-allowed disabled:opacity-50 text-muted hover:text-text max-w-full"
      >
        <Cpu size={13} className="shrink-0 opacity-70" />
        <span className="truncate min-w-0 max-w-[220px]" title={triggerText}>
          {triggerText}
        </span>
        <ChevronDown size={12} className="shrink-0 opacity-70" aria-hidden />
      </button>
      {open && createPortal(
        <div
          ref={listRef}
          role="listbox"
          tabIndex={-1}
          aria-label={i18nT('components.backendSelector.backend_list')}
          onKeyDown={onListKeyDown}
          // Portaled + `fixed` from the trigger's live rect, opening on whichever
          // side has more room and clamped inside the viewport.
          className="fixed z-[9999] overflow-y-auto bg-bg-elevated border border-border rounded-xl shadow-xl p-1 flex flex-col gap-0.5"
          style={listStyle(btnRef.current)}
        >
          {state.isError && (
            <div className="px-2 py-2">
              <ErrorNotice
                variant="inline"
                askAgent
                testId="backend-selector-list-error"
                message={i18nT('components.backendSelector.backend_list_unavailable')}
              />
            </div>
          )}
          {/* Only after the listing answered: the hook returns no rows while the
              first fetch is in flight. */}
          {!state.isError && !state.isLoading && state.backends.length === 0 && (
            <div className="px-2 py-2 text-[12px] text-muted">
              {i18nT('components.backendSelector.no_backends_registered')}
            </div>
          )}
          {state.backends.map(b => {
            const selected = chosenId === b.id
            return (
              <button
                key={b.id}
                type="button"
                role="option"
                aria-selected={selected}
                tabIndex={-1}
                data-testid={`backend-selector-option-${b.id}`}
                onClick={() => { onSelect(b.id); setOpen(false) }}
                className="w-full text-left px-2 py-1.5 rounded-lg text-[13px] flex items-start gap-1.5 bg-transparent border-none transition-colors text-text hover:bg-bg-hover cursor-pointer"
              >
                <span className="w-3.5 shrink-0 pt-0.5">{selected && <Check size={13} />}</span>
                <span className="flex flex-col min-w-0">
                  <span className="truncate">{b.label}</span>
                  {b.id === state.defaultId && (
                    <span className="text-[11px] text-muted/70 leading-snug">
                      {i18nT('components.backendSelector.default')}
                    </span>
                  )}
                </span>
              </button>
            )
          })}
        </div>,
        document.body,
      )}
    </div>
  )
}

const LIST_MAX_W = 300
const LIST_MAX_H = 320
const EDGE = 8

function listStyle(trigger: HTMLElement | null): CSSProperties {
  const vw = window.innerWidth
  const vh = window.innerHeight
  const width = Math.min(LIST_MAX_W, vw - EDGE * 2)
  if (!trigger) return { width, left: (vw - width) / 2, top: EDGE, maxHeight: LIST_MAX_H }
  const r = trigger.getBoundingClientRect()
  const left = Math.min(Math.max(r.left + r.width / 2 - width / 2, EDGE), vw - width - EDGE)
  const below = vh - r.bottom - EDGE - 4
  const above = r.top - EDGE - 4
  if (below >= Math.min(LIST_MAX_H, above)) {
    return { width, left, top: r.bottom + 4, maxHeight: Math.min(LIST_MAX_H, below) }
  }
  return { width, left, bottom: vh - r.top + 4, maxHeight: Math.min(LIST_MAX_H, above) }
}
