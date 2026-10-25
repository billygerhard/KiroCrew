import { useCallback, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'

import { api } from '../../../api/client'
import { backendChoiceExists, useBackends } from '../../../hooks/useBackends'
import { performSlotSwitch } from '../../../lib/slotSwitch'
import type { AppDispatch } from '../../../store'
import { setAgentSwitchNotice } from '../../../store/chatSlice'
import { updateSlot } from '../../../store/dashboardSlice'
import type { ChatSlot } from '../../../types'
import { agentSwitchFailureMessage } from '../../../utils/agentSwitchFeedback'

interface ComposerBackendPickOptions {
  activeSlot: string
  currentSlot: ChatSlot | undefined
  slotRunning: boolean
  dispatch: AppDispatch
}

/**
 * The composer's backend chip: the chat's stored pick, and the switch.
 *
 * A pick goes through `POST /api/chat/slots/{slot}/backend`, which stores it and
 * starts the next turn's session on it; the transcript is kept. It is offered
 * between turns only (`disabled` while one runs). On a new, empty chat the same
 * chip is the pick made at creation. A refusal reaches the shared notice toast,
 * as a refused model or agent pick does.
 *
 * `onSelectBackend` is `undefined` when there is nothing to choose (one backend
 * and no pin), which hides the chip.
 */
export function useComposerBackendPick({ activeSlot, currentSlot, slotRunning, dispatch }: ComposerBackendPickOptions) {
  const queryClient = useQueryClient()
  const backends = useBackends()
  const [applying, setApplying] = useState(false)
  const backendValue = currentSlot?.backend || null
  const switchBackend = useCallback(async (backendId: string) => {
    if (!activeSlot) return
    setApplying(true)
    try {
      await performSlotSwitch('backend', activeSlot, backendId,
        async () => {
          const r = await api.chatSlotBackend(activeSlot, backendId)
          if (r && r.ok === false) throw new Error(r.error || 'backend switch refused')
          return r?.backend ?? backendId
        },
        // The model is cleared server-side to the new backend's default, so the
        // stored model goes with it.
        (value) => dispatch(updateSlot({ key: activeSlot, backend: value, model: '' })))
      queryClient.invalidateQueries({ queryKey: ['slot-selection-capabilities', activeSlot] })
    } catch (e) {
      dispatch(setAgentSwitchNotice(agentSwitchFailureMessage(e)))
      // eslint-disable-next-line no-console -- surface switchBackend failures for debugging
      console.error('switchBackend failed', e)
    } finally {
      setApplying(false)
    }
  }, [activeSlot, dispatch, queryClient])
  return {
    backendValue,
    onSelectBackend: activeSlot && backendChoiceExists(backends, backendValue) ? switchBackend : undefined,
    backendDisabled: slotRunning || applying,
  }
}
