/**
 * One `fetch` stub answering the whole `/api/apps/spec-engine` read surface.
 *
 * Every full-pane suite mounts `<SpecEnginePage>`, and the page reads more routes
 * than any one suite is about: the shell reads the document, the queue and the
 * kill switch, and opening the configuration pane adds the registry, the resolved
 * values and the per-source autonomy grids. A suite interested in one of those
 * still has to answer all of them, so each suite used to hand-roll the same URL
 * router. That duplication has a specific failure mode, not just a maintenance
 * cost: a route nobody wrote a branch for was answered by the router's trailing
 * `else` with a queue payload, the consuming component threw while rendering a
 * body of the wrong shape, and the suite exited non-zero while every individual
 * test still reported as passing. A render-time throw outside an assertion is not
 * attributed to a test.
 *
 * This module answers the surface from ONE table so that
 *
 *   - adding a route the page always reads is one edit here rather than one per
 *     suite, and
 *   - a URL the table does not cover is a NAMED, VISIBLE failure: it answers
 *     {@link UNANSWERED_STATUS} with {@link UNANSWERED_CODE}, which the api
 *     client turns into an ordinary `SpecEngineApiError` the surface reports,
 *     and it is recorded in {@link SpecEngineStub.unanswered} so a test can say
 *     which route was missed.
 *
 * **Branch order is load-bearing.** `/config/registry`, `/config/resolved`,
 * `/config/sources`, `/config/capabilities`, `/config/workflow` and
 * `/config/conformance` are all prefixed by `/config`, so each specific path is
 * matched BEFORE the generic `/config` branch. Reversing two lines here hands a
 * `ConfigSnapshot` to a component expecting a different payload, which is the
 * silent-render-throw above.
 *
 * A suite keeps its own fixtures and passes them as overrides; the defaults here
 * are only the neutral answer for a route a suite is not about.
 */
import { vi } from "vitest";

/** The app's URL namespace, matching `api.ts`'s own `API` constant. */
export const SE_API = "/api/apps/spec-engine";

/**
 * One scripted reply: a body, and the status to send it with.
 *
 * `status` defaults to 200. A status outside 2xx is how a test forces a single
 * route to fail while every other route stays healthy, which is the only way to
 * exercise a surface's refusal path for one read at a time.
 */
export type Answer = { status?: number; body: unknown };

/** One observed request, in the shape the suites' `calls` arrays already use. */
export type Call = { url: string; method: string; body: unknown };

/** What a responder function is told about the request it is answering. */
export type RequestFacts = {
  url: string;
  method: string;
  /** The parsed request body, or `undefined` for a request that carried none. */
  body: unknown;
  /** The URL's query string, already parsed, for `project` / `run_id` / `source`. */
  params: URLSearchParams;
  /**
   * How many times THIS route has been asked, counting from 1.
   *
   * Per-route rather than global, because the states worth scripting are
   * "this read failed the second time" — a refetch that fails while React Query
   * still holds the first body — and a global counter cannot express that.
   */
  read: number;
  /**
   * Whether a configuration write has already landed successfully.
   *
   * True once a PUT to `/config` answered 2xx. This is what lets a read answer
   * "as the store would answer it now", which is the difference between asserting
   * a write was SENT and asserting the surface re-read what the write produced.
   */
  written: boolean;
};

/**
 * How one route answers: a fixed reply, a queue of replies, or a function.
 *
 * A queue's LAST entry sticks, so a test that only cares about a steady state
 * passes a single answer and a test about a change over reads passes two.
 * A function is answered per request and may be async, which is how a route is
 * held pending — see {@link held}.
 */
export type Responder =
  Answer | Answer[] | ((facts: RequestFacts) => Answer | Promise<Answer>);

/**
 * The answer that never settles, for exercising a surface's PENDING state.
 *
 * Compared by IDENTITY, so a body that happens to equal this one is still an
 * ordinary answer. A pending read is a real state of the surface and a distinct
 * one from a failed read: the failure invites a retry and the pending read must
 * not render a value it does not have yet.
 */
export const PENDING: Answer = { body: Symbol("spec-engine-stub-pending") };

/** The status an uncovered URL is answered with. Not a real backend status. */
export const UNANSWERED_STATUS = 599;

/** The `code` an uncovered URL's refusal carries, so a caller can name it. */
export const UNANSWERED_CODE = "test_stub_route_unanswered";

/** Compose a refusal for one route: a status a caller can act on, plus a code. */
export function failure(status: number, code: string, error = code): Answer {
  return { status, body: { code, error } };
}

/**
 * A responder that waits for `release()` before answering.
 *
 * The pending read is observable only while it is unresolved, so the test needs
 * the release in its own hands rather than a delay it races against.
 */
export function held(answer: Answer): {
  responder: Responder;
  release: () => void;
} {
  let release = () => {};
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  return {
    responder: async () => {
      await gate;
      return answer;
    },
    release: () => release(),
  };
}

/** Every route key the table answers. One key per registered backend route. */
export type RouteKey =
  | "config"
  | "configWrite"
  | "resolved"
  | "registry"
  | "sources"
  | "capabilities"
  | "workflow"
  | "conformance"
  | "conformanceStart"
  | "killSwitch"
  | "killSwitchSet"
  | "runSpend"
  | "queue"
  | "queueAction"
  | "setupInspect"
  | "setupPlan"
  | "setupApply";

export type SpecEngineRoutes = Partial<Record<RouteKey, Responder>>;

/** The empty setting vocabulary: enough for the generated forms to render nothing. */
const EMPTY_REGISTRY = {
  settings: [],
  source_presets: [],
  profile_presets: [],
  roles: [],
  levels: [],
};

/**
 * Neutral answers, one per route.
 *
 * "Neutral" means the state a suite that is not about this route wants: a healthy
 * read of an empty thing. A suite that IS about a route passes its own fixture,
 * so no default here is ever the subject of an assertion.
 */
const DEFAULTS: Record<RouteKey, Responder> = {
  // The configured shell, carrying a project ENTRY rather than a bare document:
  // first run is "no project entry", so `{}` here would put unrelated suites on
  // the first-run rail.
  config: {
    body: {
      configured: true,
      path: "/home/me/.kiro/crew/apps/spec-engine/config.json",
      document: { projects: { acme: { path: "/src/acme" } } },
      elided: [],
      elided_marker: "<elided>",
      errors: [],
      advisories: [],
      config_only_paths: [],
    },
  },
  configWrite: { body: { ok: true, document: {}, advisories: [] } },
  resolved: {
    body: {
      configured: true,
      project: null,
      source: null,
      settings: [],
      roles: { profile: "", roles: {} },
      role_order: [],
    },
  },
  registry: { body: EMPTY_REGISTRY },
  // No source at all, which is a state the grid must render rather than a gap:
  // a configured source nobody wrote a grid for is the fail-closed case.
  sources: {
    body: {
      sources: [],
      submitter_classes: ["maintainer", "member", "contributor", "external"],
      spec_types: ["feature", "bugfix", "quick"],
      levels: ["authoring", "execution", "delivery", "integration"],
    },
  },
  capabilities: { body: { configured: true, unreadable: false, bindings: [] } },
  workflow: { body: { configured: true, preset: "", stages: [], gates: [] } },
  conformance: {
    body: { status: "absent", binding_fingerprint: "", report: null },
  },
  conformanceStart: { body: { ok: true, started: true } },
  killSwitch: {
    body: {
      switch: { engaged: false, unreadable: false },
      stoppable: [],
      stoppable_credits: 0,
    },
  },
  killSwitchSet: {
    body: {
      ok: true,
      action: "engage",
      switch: { engaged: true, unreadable: false },
    },
  },
  runSpend: {
    body: {
      run_id: "",
      project: "",
      spec: "",
      state: "",
      source: "",
      credits: 0,
      metered_credits: 0,
      declared_credits: 0,
      turns: 0,
      sessions: 0,
      recorded_credits: 0,
      ceiling: { value: 0, origin: "", declared_at: "" },
    },
  },
  queue: { body: { entries: [], grouped: {}, total: 0, total_credits: 0 } },
  queueAction: { body: { ok: true } },
  setupInspect: { body: {} },
  setupPlan: { body: {} },
  setupApply: { body: {} },
};

/** What {@link stubSpecEngineFetch} hands back for a test to interrogate. */
export type SpecEngineStub = {
  /** Every observed request, in order. */
  calls: Call[];
  /** How many times one route was asked. */
  reads: (route: RouteKey) => number;
  /**
   * The URLs no route in the table matched.
   *
   * Non-empty means the table is missing a route the page reads, which is a
   * harness defect and not a product one — assert it is empty in a suite that
   * wants that guarantee.
   */
  unanswered: () => string[];
};

/**
 * Which route key a request belongs to, or `null` when the table does not cover it.
 *
 * Specific `/config/...` paths are tested BEFORE the bare `/config` prefix; that
 * ordering is the whole reason this lives in one function.
 */
function routeFor(path: string, method: string): RouteKey | null {
  if (!path.startsWith(SE_API)) return null;
  const rest = path.slice(SE_API.length).split("?")[0];

  if (rest === "/setup/inspect") return "setupInspect";
  if (rest === "/setup/plan") return "setupPlan";
  if (rest === "/setup/apply") return "setupApply";

  if (rest.startsWith("/config/conformance")) {
    return method === "POST" ? "conformanceStart" : "conformance";
  }
  if (rest.startsWith("/config/capabilities")) return "capabilities";
  if (rest.startsWith("/config/workflow")) return "workflow";
  if (rest.startsWith("/config/registry")) return "registry";
  if (rest.startsWith("/config/resolved")) return "resolved";
  if (rest.startsWith("/config/sources")) return "sources";
  // Last of the `/config` family on purpose: every branch above is prefixed by
  // this one, so moving it up swallows all of them. An exact comparison is
  // enough because `rest` already had its query stripped above.
  if (rest === "/config") {
    return method === "PUT" ? "configWrite" : "config";
  }

  if (rest.startsWith("/kill-switch")) {
    return method === "POST" ? "killSwitchSet" : "killSwitch";
  }
  if (rest.startsWith("/run-spend")) return "runSpend";
  if (rest.startsWith("/queue/")) return "queueAction";
  if (rest === "/queue") return "queue";
  return null;
}

/**
 * Install the stub for one test, returning the handles a test asserts through.
 *
 * `record` exists so a suite can keep the module-level `calls` array its
 * assertions already read; the same calls are also on the returned stub.
 */
export function stubSpecEngineFetch(
  routes: SpecEngineRoutes = {},
  options: { record?: Call[] } = {},
): SpecEngineStub {
  const calls: Call[] = options.record ?? [];
  const unanswered: string[] = [];
  const reads = new Map<RouteKey, number>();
  // Queues are consumed, so each install gets its own copy: a suite that builds
  // its answers once at module scope must not find them drained by the last test.
  const queues = new Map<RouteKey, Answer[]>();
  let written = false;

  const answerFor = async (
    route: RouteKey,
    facts: Omit<RequestFacts, "read" | "written">,
  ): Promise<Answer> => {
    const read = (reads.get(route) ?? 0) + 1;
    reads.set(route, read);
    const responder = routes[route] ?? DEFAULTS[route];
    if (typeof responder === "function") {
      return responder({ ...facts, read, written });
    }
    if (Array.isArray(responder)) {
      let queue = queues.get(route);
      if (!queue) {
        queue = [...responder];
        queues.set(route, queue);
      }
      // The last entry sticks rather than the queue running dry, so a steady
      // state does not need an entry per read.
      return queue.length > 1 ? queue.shift()! : queue[0];
    }
    return responder;
  };

  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      let body: unknown = undefined;
      if (init?.body) {
        try {
          body = JSON.parse(String(init.body));
        } catch {
          body = String(init.body);
        }
      }
      calls.push({ url, method, body });

      const route = routeFor(url, method);
      let answer: Answer;
      if (route === null) {
        // Recorded AND refused. Falling through to any real payload is what let a
        // missing branch reach a component as the wrong shape and throw in render.
        unanswered.push(url);
        answer = failure(
          UNANSWERED_STATUS,
          UNANSWERED_CODE,
          `the spec-engine fetch stub has no route for ${url}`,
        );
      } else {
        const query = url.includes("?") ? url.slice(url.indexOf("?") + 1) : "";
        answer = await answerFor(route, {
          url,
          method,
          body,
          params: new URLSearchParams(query),
        });
        if (route === "configWrite" && (answer.status ?? 200) < 300)
          written = true;
      }

      if (answer === PENDING) return new Promise<never>(() => {});
      const status = answer.status ?? 200;
      return {
        ok: status >= 200 && status < 300,
        status,
        text: () => Promise.resolve(JSON.stringify(answer.body)),
      };
    }),
  );

  return {
    calls,
    reads: (route: RouteKey) => reads.get(route) ?? 0,
    unanswered: () => [...unanswered],
  };
}
