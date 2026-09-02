/**
 * Screenshots for the ACP harness-provider UI surfaces (branch feat/acp-providers):
 *
 *   harness-picker.png        the new-chat HarnessSelector dropdown OPEN — the
 *                             default row (Kiro CLI, selected), two more available
 *                             rows (Kiro Agent Server, Claude Code), one UNAVAILABLE
 *                             row with its reason (Codex CLI — codex-acp not on PATH),
 *                             and one INVALID operator descriptor with its per-reason
 *                             message (stub-echo — mcp_delivery 'wire' not one of
 *                             file_fed, wire_fed).
 *   composer-harness-chip.png the composer-shelf chip naming a non-default harness (KAS).
 *   harness-settings-panel.png the Settings HarnessPanel inventory, each row with its
 *                             availability / install / serviceability state.
 *
 * Mounts the REAL components (website/capture/acp-harness-providers.tsx) against the
 * real stylesheet and live i18n catalog, and answers every `/api/**` call this
 * machine would make from a fixture — no gateway, no token, no agent. Each frame
 * asserts its named elements before writing, so a blank or wrong frame fails the run.
 *
 * The capture entries are vite-dev-served (not in website/dist), so this script
 * starts its own vite on an ephemeral loopback port, captures, and tears it down.
 *
 * Usage: node scripts/capture-acp-harness-providers.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { spawn } from 'node:child_process'
import { fileURLToPath } from 'node:url'

const OUT = process.argv[2] || '../temp-screenshots/acp-harness-providers'
mkdirSync(OUT, { recursive: true })

/** ---- fixture: GET /api/harnesses ------------------------------------------
 * Exact shape of the shipped api_harnesses payload
 * (src/kiro_crew/dashboard/handlers/agents.py): { harnesses[], invalid[],
 * default, legacy_backend, legacy_backends }. Row fields: id, display_name,
 * available, reason, bundled, serviceable. Invalid rows carry id/display_name/
 * reason and are always unavailable.
 */
const HARNESSES = {
  harnesses: [
    { id: 'kiro', display_name: 'Kiro CLI', available: true, reason: '', bundled: true, serviceable: true },
    { id: 'kas', display_name: 'Kiro Agent Server', available: true, reason: '', bundled: true, serviceable: true },
    { id: 'claude', display_name: 'Claude Code', available: true, reason: '', bundled: true, serviceable: true },
    {
      id: 'codex',
      display_name: 'Codex CLI',
      available: false,
      reason: "'codex-acp' not found on PATH",
      bundled: true,
      serviceable: true,
    },
  ],
  invalid: [
    {
      id: 'stub-echo',
      display_name: 'Stub Echo',
      available: false,
      reason: "mcp_delivery 'wire' is not one of: file_fed, wire_fed",
      bundled: false,
    },
  ],
  default: 'kiro',
  legacy_backend: '',
  legacy_backends: ['', 'kas'],
}

/** GET /api/config/kirocrew — no default_harness set, so the panel's default
 *  picker resolves via the registry payload's `default`. */
const CONFIG = { agent: { default_harness: '', acp_backend: '' } }

/** GET /api/acp-backends — the machine probe the HarnessPanel joins by
 *  policy_id === row.id. Codex missing (names the install command); the three
 *  serviceable rows installed. Keeps every row selectable so none is hidden. */
const ACP_BACKENDS = {
  backends: [
    { id: 'kiro', policy_id: 'kiro', selectable: true, installed: 'installed', missing_components: [], install_command: '', restart_required: false },
    { id: 'kas', policy_id: 'kas', selectable: true, installed: 'installed', missing_components: [], install_command: '', restart_required: false },
    { id: 'claude', policy_id: 'claude', selectable: true, installed: 'installed', missing_components: [], install_command: '', restart_required: false },
    { id: 'codex', policy_id: 'codex', selectable: true, installed: 'missing', missing_components: ['codex-acp'], install_command: 'npm i -g @agentclientprotocol/codex-acp', restart_required: false },
  ],
}

/** Start vite dev on an ephemeral loopback port; resolve with { proc, base }. */
function startVite() {
  const cwd = fileURLToPath(new URL('..', import.meta.url))
  return new Promise((resolve, reject) => {
    const proc = spawn(
      'npx',
      ['vite', '--host', '127.0.0.1', '--port', '0', '--strictPort=false', '--clearScreen=false'],
      { cwd, env: { ...process.env }, stdio: ['ignore', 'pipe', 'pipe'] },
    )
    let out = ''
    const onData = d => {
      out += d.toString()
      const m = out.match(/Local:\s+https?:\/\/(127\.0\.0\.1|localhost):(\d+)/)
      if (m) { resolve({ proc, base: `http://127.0.0.1:${m[2]}` }); proc.stdout.off('data', onData) }
    }
    proc.stdout.on('data', onData)
    proc.stderr.on('data', () => {})
    proc.on('exit', c => reject(new Error(`vite exited early (${c})\n${out}`)))
    setTimeout(() => reject(new Error(`vite did not report a URL in 60s\n${out}`)), 60_000)
  })
}

const { proc: vite, base } = await startVite()

// chromiumSandbox:false — the seccomp-confined agent shell cannot start
// Chromium's own sandbox (documented host quirk on this Cloud Desktop).
const browser = await chromium.launch({ chromiumSandbox: false })
let failed = false
const errors = []

function check(name, ok, detail) {
  console.log(`${name}: ${ok ? 'OK' : 'MISMATCH'} ${detail || ''}`)
  if (!ok) failed = true
  return ok
}

/** New page with every /api call answered from the fixtures above. Predicate on
 *  the pathname — a **\/api\/** glob would also swallow vite's own /src modules. */
async function scenePage(viewport) {
  const page = await browser.newPage({ viewport, deviceScaleFactor: 2 })
  page.on('pageerror', e => errors.push(`PAGEERROR: ${e.message}`))
  page.on('console', m => { if (m.type() === 'error') errors.push(m.text().slice(0, 200)) })
  await page.route(u => new URL(u).pathname.startsWith('/api/'), route => {
    const path = new URL(route.request().url()).pathname
    const body =
      path === '/api/harnesses' ? HARNESSES
      : path === '/api/config/kirocrew' ? CONFIG
      : path === '/api/acp-backends' ? ACP_BACKENDS
      : /commands|skills|agents|sessions|files|history|models|instances/.test(path) ? []
      : {}
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })
  })
  return page
}

try {
  // ---- Scene 1: harness picker OPEN --------------------------------------
  {
    const page = await scenePage({ width: 720, height: 640 })
    await page.goto(`${base}/capture/acp-harness-providers.html?scene=picker&theme=dark`)
    await page.waitForSelector('[data-capture-root]')
    // Open the REAL dropdown by clicking the trigger (default label = "Kiro CLI").
    await page.getByRole('button', { name: /Harness:/ }).click()
    await page.getByRole('listbox').waitFor({ timeout: 10_000 })
    const rows = page.getByRole('option')
    const kiro = await page.getByRole('option', { name: /Kiro CLI/ }).count()
    const kas = await page.getByRole('option', { name: /Kiro Agent Server/ }).count()
    const claude = await page.getByRole('option', { name: /Claude Code/ }).count()
    const codexReason = await page.getByText("'codex-acp' not found on PATH", { exact: false }).count()
    const invalidReason = await page.getByText("mcp_delivery 'wire' is not one of: file_fed, wire_fed", { exact: false }).count()
    const optCount = await rows.count()
    if (check('harness-picker', optCount >= 4 && kiro && kas && claude && codexReason === 1 && invalidReason === 1,
      `options=${optCount} kiro=${kiro} kas=${kas} claude=${claude} codexReason=${codexReason} invalid=${invalidReason}`)) {
      await page.screenshot({ path: `${OUT}/harness-picker.png` })
    }
    await page.close()
  }

  // ---- Scene 2: composer harness chip ------------------------------------
  {
    const page = await scenePage({ width: 760, height: 420 })
    await page.goto(`${base}/capture/acp-harness-providers.html?scene=chip&theme=dark`)
    await page.waitForSelector('[data-capture-root]')
    const chip = page.getByTestId('chat-input-harness-chip')
    await chip.waitFor({ timeout: 10_000 })
    const text = (await chip.textContent()) || ''
    const title = await chip.getAttribute('title')
    if (check('composer-harness-chip', text.includes('KAS') && (title || '').includes('AI harness serving this chat'),
      `text=${JSON.stringify(text)} title=${JSON.stringify(title)}`)) {
      // Frame the composer, not the whole viewport, so the chip is legible.
      const shelf = page.locator('[data-capture-root]')
      await shelf.screenshot({ path: `${OUT}/composer-harness-chip.png` })
    }
    await page.close()
  }

  // ---- Scene 3: settings HarnessPanel ------------------------------------
  {
    const page = await scenePage({ width: 900, height: 900 })
    await page.goto(`${base}/capture/acp-harness-providers.html?scene=settings&theme=dark`)
    await page.waitForSelector('[data-capture-root]')
    await page.getByTestId('harness-inventory').waitFor({ timeout: 10_000 })
    const kiroRow = await page.getByTestId('harness-row-kiro').count()
    const kasRow = await page.getByTestId('harness-row-kas').count()
    const claudeRow = await page.getByTestId('harness-row-claude').count()
    const codexRow = await page.getByTestId('harness-row-codex').count()
    const stubRow = await page.getByTestId('harness-row-stub-echo').count()
    const missingBadge = await page.getByTestId('harness-missing-codex').count()
    const invalidMsg = await page.getByText("mcp_delivery 'wire' is not one of: file_fed, wire_fed", { exact: false }).count()
    if (check('harness-settings-panel',
      kiroRow && kasRow && claudeRow && codexRow && stubRow && missingBadge === 1 && invalidMsg === 1,
      `kiro=${kiroRow} kas=${kasRow} claude=${claudeRow} codex=${codexRow} stub=${stubRow} missing=${missingBadge} invalid=${invalidMsg}`)) {
      await page.screenshot({ path: `${OUT}/harness-settings-panel.png` })
    }
    await page.close()
  }
} finally {
  await browser.close()
  vite.kill('SIGTERM')
}

if (errors.length) console.error('console/page errors:\n' + errors.join('\n'))
if (failed) process.exit(1)
console.log(`wrote 3 frames to ${OUT}`)
