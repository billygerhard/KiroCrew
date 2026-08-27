/**
 * The shared spec-engine fetch stub's own guarantees.
 *
 * The stub is harness code, so a defect in it does not fail a test — it fails the
 * RENDER of a component reading a body of the wrong shape, outside any assertion.
 * That is why this file exists: the hand-rolled routers this stub replaced each
 * ended in a trailing `else` that answered any unmatched URL with the queue
 * payload, and a route nobody wrote a branch for reached its consumer as a
 * `QueueSnapshot`, threw during render, and produced a suite that exited non-zero
 * while every individual test reported as passing. A missed route has to be a
 * NAMED failure, and these tests are what pin that.
 */
import { describe, it, expect, afterEach, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import SpecEnginePage from "../apps/spec-engine/SpecEnginePage";
import {
  SE_API,
  UNANSWERED_CODE,
  UNANSWERED_STATUS,
  failure,
  held,
  stubSpecEngineFetch,
  PENDING,
} from "./specEngineFetchStub";

afterEach(() => {
  vi.unstubAllGlobals();
});

/** Ask the stubbed `fetch` directly, the way `api.ts`'s `request` does. */
async function ask(url: string, init?: RequestInit) {
  const response = await (globalThis.fetch as unknown as typeof fetch)(
    url,
    init,
  );
  const text = await response.text();
  return { status: response.status, ok: response.ok, body: JSON.parse(text) };
}

function renderPage() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchInterval: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <SpecEnginePage />
    </QueryClientProvider>,
  );
}

describe("an unanswered route", () => {
  it("refuses with a nameable code instead of answering with another route body", async () => {
    const stub = stubSpecEngineFetch();
    // A route on the app's own prefix that the table does not cover. The old
    // routers answered exactly this with a QueueSnapshot.
    const answered = await ask(`${SE_API}/not-a-route`);
    expect(answered.ok).toBe(false);
    expect(answered.status).toBe(UNANSWERED_STATUS);
    expect(answered.body).toMatchObject({ code: UNANSWERED_CODE });
    // The queue payload is what a fall-through would have handed back, and the
    // shape a consumer would then have thrown on.
    expect(answered.body).not.toHaveProperty("entries");
    // And it is reported by name, so a suite is told WHICH route is missing
    // rather than being left to infer it from a render stack.
    expect(stub.unanswered()).toEqual([`${SE_API}/not-a-route`]);
  });

  it("leaves the covered routes answered, so the miss is attributable to one route", async () => {
    const stub = stubSpecEngineFetch();
    await ask(`${SE_API}/config`);
    await ask(`${SE_API}/config/registry`);
    await ask(`${SE_API}/queue`);
    expect(stub.unanswered()).toEqual([]);
  });

  it("reports a URL outside the app prefix rather than silently answering it", async () => {
    const stub = stubSpecEngineFetch();
    const answered = await ask("/api/sessions");
    expect(answered.status).toBe(UNANSWERED_STATUS);
    expect(stub.unanswered()).toEqual(["/api/sessions"]);
  });
});

describe("the route table", () => {
  it("answers each specific config path rather than letting the prefix swallow it", async () => {
    // The ordering guarantee, asserted as a property of the table instead of as a
    // comment: every one of these is prefixed by `/config`, and a reversed branch
    // hands the caller a ConfigSnapshot.
    stubSpecEngineFetch({
      config: { body: { marker: "config" } },
      registry: { body: { marker: "registry" } },
      resolved: { body: { marker: "resolved" } },
      sources: { body: { marker: "sources" } },
      capabilities: { body: { marker: "capabilities" } },
      workflow: { body: { marker: "workflow" } },
      conformance: { body: { marker: "conformance" } },
    });
    const seen = await Promise.all(
      [
        `${SE_API}/config`,
        `${SE_API}/config/registry`,
        `${SE_API}/config/resolved?project=%2Fsrc%2Facme`,
        `${SE_API}/config/sources`,
        `${SE_API}/config/capabilities`,
        `${SE_API}/config/workflow`,
        `${SE_API}/config/conformance/review`,
      ].map((url) => ask(url)),
    );
    expect(
      seen.map((answer) => (answer.body as { marker: string }).marker),
    ).toEqual([
      "config",
      "registry",
      "resolved",
      "sources",
      "capabilities",
      "workflow",
      "conformance",
    ]);
  });

  it("separates a write from a read on the routes that share a path", async () => {
    stubSpecEngineFetch({
      config: { body: { marker: "read" } },
      configWrite: { body: { marker: "write" } },
      killSwitch: { body: { marker: "read" } },
      killSwitchSet: { body: { marker: "write" } },
    });
    const read = await ask(`${SE_API}/config`);
    const wrote = await ask(`${SE_API}/config`, { method: "PUT", body: "{}" });
    const readSwitch = await ask(`${SE_API}/kill-switch`);
    const setSwitch = await ask(`${SE_API}/kill-switch`, {
      method: "POST",
      body: "{}",
    });
    expect([read.body, wrote.body, readSwitch.body, setSwitch.body]).toEqual([
      { marker: "read" },
      { marker: "write" },
      { marker: "read" },
      { marker: "write" },
    ]);
  });

  it("counts reads per route and sticks on the last of a queue", async () => {
    const stub = stubSpecEngineFetch({
      config: [{ body: { n: 1 } }, { body: { n: 2 } }],
    });
    const first = await ask(`${SE_API}/config`);
    const second = await ask(`${SE_API}/config`);
    const third = await ask(`${SE_API}/config`);
    expect([first.body, second.body, third.body]).toEqual([
      { n: 1 },
      { n: 2 },
      { n: 2 },
    ]);
    expect(stub.reads("config")).toBe(3);
    expect(stub.reads("queue")).toBe(0);
  });

  it("tells a responder that a write has landed, and only after a successful one", async () => {
    stubSpecEngineFetch({
      config: ({ written }) => ({ body: { written } }),
      configWrite: failure(409, "config_write_refused"),
    });
    expect((await ask(`${SE_API}/config`)).body).toEqual({ written: false });
    await ask(`${SE_API}/config`, { method: "PUT", body: "{}" });
    // The refused write must not read as a landed one: a surface that re-read on
    // the strength of it would report a document the store never took.
    expect((await ask(`${SE_API}/config`)).body).toEqual({ written: false });
  });
});

describe("a forced single-route failure", () => {
  it("surfaces as a reported refusal rather than as a thrown render", async () => {
    // One route fails and every other stays healthy, which is the only way to
    // exercise a surface's refusal path for one read at a time.
    stubSpecEngineFetch({ config: failure(500, "config_unreadable") });
    renderPage();
    // The page still renders — the refusal reached a component as a refusal, not
    // as a body of the wrong shape.
    await waitFor(() =>
      expect(screen.getByRole("navigation")).toBeInTheDocument(),
    );
  });

  it("holds one route pending without holding the rest", async () => {
    const { responder, release } = held({ body: { marker: "late" } });
    stubSpecEngineFetch({ sources: responder });
    let settled = false;
    const pending = ask(`${SE_API}/config/sources`).then((answer) => {
      settled = true;
      return answer;
    });
    // A different route answers while the held one is still outstanding.
    expect((await ask(`${SE_API}/config`)).ok).toBe(true);
    expect(settled).toBe(false);
    release();
    expect((await pending).body).toEqual({ marker: "late" });
  });

  it("never settles the PENDING answer, which is a different state from a failure", async () => {
    stubSpecEngineFetch({ queue: PENDING });
    let settled = false;
    void ask(`${SE_API}/queue`).then(() => {
      settled = true;
    });
    // Two macrotask turns is more than enough for a resolved promise to land.
    await new Promise((resolve) => setTimeout(resolve, 0));
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(settled).toBe(false);
  });
});
