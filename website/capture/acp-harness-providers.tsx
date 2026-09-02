/**
 * Isolated capture entry for the ACP harness-provider UI surfaces:
 *
 *   picker   — the new-chat HarnessSelector dropdown OPEN, showing an available
 *              default, two more available rows, one unavailable row with its
 *              reason, and one INVALID operator descriptor with its per-reason
 *              message.
 *   chip     — the composer-shelf harness chip (ChatInput's harnessLabel prop)
 *              naming a non-default harness.
 *   settings — the Settings HarnessPanel inventory listing the harness rows
 *              with their availability / install / serviceability states.
 *
 * WHY ISOLATED: two of the three states are unreachable by SPA navigation on a
 * fixture host. The picker's OPEN dropdown only exists on the welcome screen and
 * behind an operator having authored an invalid descriptor; the composer chip
 * only renders once a live slot has bound a non-default harness. Rather than
 * fake a whole chat session, this mounts the REAL components against the real
 * stylesheet, theme tokens and live i18n catalog. Every scene reads its data
 * from the same `GET /api/harnesses` payload the shipped code reads — the driver
 * script (capture-acp-harness-providers.mjs) answers that call from a fixture and
 * asserts each named element is present before it writes a frame.
 *
 * Scene + theme come from the query string:
 *   ?scene=picker|chip|settings&theme=dark|light
 */
import { useEffect, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

import HarnessSelector from '../src/components/HarnessSelector'
import ChatInput from '../src/components/ChatInput'
import { HarnessPanel } from '../src/pages/settings/HarnessPanel'
import { initI18n } from '../src/i18n/all'
import { store } from '../src/store'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
const scene = params.get('scene') || 'picker'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })

/** The picker mounted on a card the size of the welcome-screen composer row, so
 *  the OPEN dropdown has room to render its rows below the trigger. Auto-opened
 *  by the driver clicking the REAL trigger, so the frame documents the shipped
 *  open/close wiring rather than a forced state. */
function PickerScene() {
  const [value, setValue] = useState('')
  return (
    <div
      data-capture-root
      className="min-h-screen bg-bg text-text flex items-start justify-center pt-24"
    >
      <div className="w-[520px] rounded-xl border border-border bg-card p-6 flex flex-col items-center gap-3">
        <div className="text-[13px] text-muted self-start">Start a new chat</div>
        <HarnessSelector value={value} onSelect={setValue} />
      </div>
    </div>
  )
}

/** The composer-shelf chip: the real ChatInput carrying a non-default
 *  harnessLabel, the exact prop ChatPane/ChatPage pass. Pinned tooltip variant,
 *  because a slot with an explicit harness was fixed at creation. */
function ChipScene() {
  const [value, setValue] = useState('')
  return (
    <div
      data-capture-root
      className="min-h-screen bg-bg text-text flex flex-col justify-end"
    >
      <div className="flex flex-col gap-2 px-3 pb-3">
        <div className="self-end max-w-[80%] rounded-xl bg-accent-subtle px-3 py-2 text-[13px]">
          Build the project and run the tests.
        </div>
        <div className="self-start max-w-[80%] rounded-xl bg-card text-card-fg px-3 py-2 text-[13px]">
          Starting with the build.
        </div>
      </div>
      {/* The harness chip lives INSIDE the context shelf, which ChatInput only
          renders when the shelf has a project or model chip to show (as every
          real ChatPane/ChatPage does). Supply those sibling chips so the shelf
          — and the harness chip in it — renders exactly as in production. */}
      <ChatInput
        value={value}
        onChange={setValue}
        onSend={() => setValue('')}
        connected
        agentName="default"
        onAgentClick={() => {}}
        modelName="Claude Sonnet 4.5"
        onModelClick={() => {}}
        project="/home/user/workspace/KiroCrew"
        projectBranch="feat/acp-providers"
        onProjectClick={() => {}}
        harnessLabel="KAS"
        harnessTitle="AI harness serving this chat — fixed when the chat was created"
      />
    </div>
  )
}

/** The Settings HarnessPanel, mounted exactly as Chat settings mounts it. Reads
 *  /api/harnesses, /api/config/kirocrew and /api/acp-backends — all answered by
 *  the driver. */
function SettingsScene() {
  return (
    <div
      data-capture-root
      className="min-h-screen bg-bg text-text"
    >
      <div className="max-w-[760px] mx-auto px-6 py-8">
        <HarnessPanel />
      </div>
    </div>
  )
}

function Root() {
  useEffect(() => {
    document.documentElement.setAttribute('data-capture-scene', scene)
  }, [])
  if (scene === 'chip') return <ChipScene />
  if (scene === 'settings') return <SettingsScene />
  return <PickerScene />
}

initI18n('en')
createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <Root />
      </MemoryRouter>
    </QueryClientProvider>
  </Provider>,
)
