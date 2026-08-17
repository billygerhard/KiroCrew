/**
 * The Operator_Surface's API client, against the routes the backend registers.
 *
 * These assertions are transcribed from
 * `src/kiro_crew/apps/builtins/spec_engine/backend/routes.py` — the method, the
 * path, and where each argument travels. They exist because three of those routes
 * do NOT follow the convention a reader would assume: the two reads take a query
 * string, the kill switch takes its verb in the BODY rather than in the method,
 * and every queue action posts an identifier in the body rather than in the URL.
 * A client written from convention would be wrong in exactly those three places
 * and would fail only against a live gateway.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

import { REFUSAL, SpecEngineApiError, specEngineApi } from '../apps/spec-engine/api'

type Capture = { url: string; init?: RequestInit }

let calls: Capture[]

/** A stub answering every request with *body* at *status*. */
function stubFetch(body: unknown, status = 200) {
  const fetchMock = vi.fn((url: string, init?: RequestInit) => {
    calls.push({ url, init })
    return Promise.resolve({
      ok: status >= 200 && status < 300,
      status,
      text: () => Promise.resolve(JSON.stringify(body)),
    })
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

const bodyOf = (call: Capture): Record<string, unknown> =>
  JSON.parse(String(call.init?.body ?? '{}'))

beforeEach(() => {
  calls = []
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('reads', () => {
  it('gets the queue from the app namespace, with no project by default', async () => {
    stubFetch({ entries: [], grouped: {}, total: 0, total_credits: 0 })
    await specEngineApi.queue()
    expect(calls[0].url).toBe('/api/apps/spec-engine/queue')
  })

  it('passes a project as a query parameter, encoded', async () => {
    // The handler reads `request.query["project"]`, and the value is the engine's
    // stored posix path — so it is encoded, never rewritten.
    stubFetch({ entries: [], grouped: {}, total: 0, total_credits: 0 })
    await specEngineApi.queue('/home/me/src/checkout svc')
    expect(calls[0].url).toBe(
      '/api/apps/spec-engine/queue?project=%2Fhome%2Fme%2Fsrc%2Fcheckout%20svc',
    )
  })

  it('asks for one run\u2019s spend by query parameter, not by path segment', async () => {
    // `GET /run-spend?run_id=…`. A REST-shaped `/run-spend/<id>` would 404: the
    // route table registers no path parameter at all.
    stubFetch({ run_id: 'run_1' })
    await specEngineApi.runSpend('run_1')
    expect(calls[0].url).toBe('/api/apps/spec-engine/run-spend?run_id=run_1')
  })

  it('sends the session cookie on every request', async () => {
    // Same-origin credentials are what make these reads work at all: the routes sit
    // behind the gateway's auth middleware and answer 401 without an identity.
    stubFetch({ configured: true })
    await specEngineApi.config()
    expect(calls[0].init?.credentials).toBe('same-origin')
  })
})

describe('mutations', () => {
  it('wraps a configuration patch in {patch} on a PUT', async () => {
    // The handler accepts the patch as the whole body OR under `patch`. The wrapper
    // is the unambiguous half: a document whose own top level held a `patch` key
    // would otherwise be silently unwrapped by `body.get("patch", body)`.
    stubFetch({ ok: true, document: {}, advisories: [] })
    await specEngineApi.writeConfig({ limits: { revision_cycle_limit: 3 } })
    expect(calls[0].url).toBe('/api/apps/spec-engine/config')
    expect(calls[0].init?.method).toBe('PUT')
    expect(bodyOf(calls[0])).toEqual({ patch: { limits: { revision_cycle_limit: 3 } } })
  })

  it('puts the kill switch verb in the body, and posts to one path for both', async () => {
    // Engage and release are ONE route. A client that reached for DELETE to release
    // would hit no handler.
    stubFetch({ ok: true, action: 'engage', switch: { engaged: true } })
    await specEngineApi.setKillSwitch({ action: 'engage', reason: 'stop everything' })
    expect(calls[0].url).toBe('/api/apps/spec-engine/kill-switch')
    expect(calls[0].init?.method).toBe('POST')
    expect(bodyOf(calls[0])).toEqual({ action: 'engage', reason: 'stop everything' })
  })

  it('never sends an initiator: the handler takes it from the session', async () => {
    // A stop attributed to a name the caller typed records nothing, so the client
    // has no parameter for one.
    stubFetch({ ok: true, action: 'release', switch: { engaged: false } })
    await specEngineApi.setKillSwitch({ action: 'release' })
    expect(Object.keys(bodyOf(calls[0]))).toEqual(['action'])
  })

  it('posts each queue action to its own sub-path with its identifiers in the body', async () => {
    stubFetch({ ok: true, released: true })
    await specEngineApi.releaseFeedback({
      project: '/p',
      spec: 'idempotent-refunds',
      run_id: 'run_1',
      comment_id: 'c7',
    })
    expect(calls[0].url).toBe('/api/apps/spec-engine/queue/release-feedback')
    expect(bodyOf(calls[0])).toEqual({
      project: '/p',
      spec: 'idempotent-refunds',
      run_id: 'run_1',
      comment_id: 'c7',
    })

    stubFetch({ ok: true, lifted: true })
    await specEngineApi.redispatch({ source: 'github', item_id: '42', generation: 3 })
    expect(calls[1].url).toBe('/api/apps/spec-engine/queue/redispatch')
    // The generation travels. The handler refuses without it rather than lifting
    // whichever generation the poller happens to be on.
    expect(bodyOf(calls[1])).toEqual({ source: 'github', item_id: '42', generation: 3 })

    stubFetch({ ok: true, removed: true, cleanup: null })
    await specEngineApi.cleanWorkspace({ workspace_id: 12, force: true })
    expect(calls[2].url).toBe('/api/apps/spec-engine/queue/clean-workspace')
    expect(bodyOf(calls[2])).toEqual({ workspace_id: 12, force: true })

    stubFetch({ ok: true, complete: false, kept: [3], report: {} })
    const report = await specEngineApi.teardown({ run_id: 'run_1' })
    expect(calls[3].url).toBe('/api/apps/spec-engine/queue/teardown')
    // `ok` is not the field that matters: a teardown that kept a workspace answers
    // ok and NOT complete, and a caller reading only `ok` reports a standing tree
    // as torn down.
    expect(report.ok).toBe(true)
    expect(report.complete).toBe(false)
    expect(report.kept).toEqual([3])
  })
})

describe('refusals', () => {
  it('raises with the machine-readable code, the text and the status', async () => {
    stubFetch({ code: REFUSAL.configUnreadable, error: 'config.json line 4' }, 409)
    await expect(specEngineApi.config()).rejects.toMatchObject({
      code: REFUSAL.configUnreadable,
      status: 409,
      message: 'config.json line 4',
    })
  })

  it('carries an EMPTY code when no handler answered', async () => {
    // A dropped connection is not a refusal, and giving it a code would let a
    // caller branch on a reason nothing stated. The empty code is the signal.
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.reject(new TypeError('Failed to fetch'))),
    )
    const error = await specEngineApi.queue().catch((e: unknown) => e)
    expect(error).toBeInstanceOf(SpecEngineApiError)
    expect((error as SpecEngineApiError).code).toBe('')
    expect((error as SpecEngineApiError).status).toBe(0)
  })

  it('still reports the status when the failure body is not JSON', async () => {
    // A gateway 502 or an auth redirect is HTML. Parsing it as JSON would raise a
    // SyntaxError naming nothing an operator can act on.
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve({
          ok: false,
          status: 502,
          text: () => Promise.resolve('<html>bad gateway</html>'),
        }),
      ),
    )
    const error = await specEngineApi.queue().catch((e: unknown) => e)
    expect((error as SpecEngineApiError).status).toBe(502)
    expect((error as SpecEngineApiError).code).toBe('')
  })
})
