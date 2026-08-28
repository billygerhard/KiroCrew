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

/**
 * The pipeline stages, in `_registry_payload`'s `stages` shape and with the
 * engine's own placement.
 *
 * Present even in the EMPTY registry, unlike the vocabularies beside it, because
 * the configuration pane's areas are generated from it: a payload without `stages`
 * folds every group into one advanced area, which is a real degradation the pane
 * handles but not a shape the route ever returns. A suite that is about the pane
 * would then be asserting against fiction.
 *
 * The group and capability placement is `engine/config/pipeline.py`'s, transcribed
 * rather than invented — authoring genuinely holds no setting group, and delivery
 * genuinely holds no delegable capability.
 */
export const PIPELINE_STAGES = [
  { id: "intake", setting_groups: ["watch"], capabilities: ["watch_sources"] },
  {
    id: "authoring",
    setting_groups: [],
    capabilities: ["analysis", "authoring", "validation_rules"],
  },
  {
    id: "execution",
    setting_groups: ["concurrency", "limits", "timeouts", "budget"],
    capabilities: ["review", "implementation"],
  },
  { id: "delivery", setting_groups: ["delivery", "notify"], capabilities: [] },
  { id: "advanced", setting_groups: ["telemetry"], capabilities: ["model_catalog"] },
];

/**
 * The five stages with every one of *groups* placed under *stage*.
 *
 * For a suite whose subject is a FORM rather than the pane's areas: it declares one
 * area holding everything that suite's vocabulary contains, so the suite navigates
 * once and then reads its own rows. A legitimate projection shape — the engine
 * already places four groups under execution — and not a fiction, which is what
 * omitting `stages` altogether would be.
 */
export function stagesUnder(stage: string, groups: readonly string[]) {
  return PIPELINE_STAGES.map((entry) => ({
    ...entry,
    setting_groups: entry.id === stage ? [...groups] : [],
  }));
}

/**
 * How a delegated capability may be reached, as `schema.TRANSPORTS` declares it.
 *
 * Transcribed rather than invented, and in the engine's own order: a transport
 * chooser is generated from this, so a list that disagreed with the tuple would
 * offer a transport the write door refuses.
 */
export const TRANSPORTS = ["builtin", "mcp", "command"];

/**
 * The capabilities the engine always executes itself, as
 * `schema.ENGINE_FLOOR_CAPABILITIES` declares them.
 *
 * Naming one of these in the `capabilities` section is REFUSED rather than
 * ignored, which is why a surface showing capabilities names them and offers no
 * control that would attempt one.
 */
export const ENGINE_FLOOR_CAPABILITIES = [
  "format_validation",
  "phase_gates",
  "autonomy_resolution",
  "budget_enforcement",
  "claim_ledger",
  "audit_log",
];

/** The empty setting vocabulary: enough for the generated forms to render nothing. */
const EMPTY_REGISTRY = {
  settings: [],
  source_presets: [],
  profile_presets: [],
  roles: [],
  levels: [],
  stages: PIPELINE_STAGES,
  // The extension seams' closed sets. Present even in the EMPTY registry, for
  // `stages`' reason: the route composes each from its owning tuple on every read,
  // so a payload without them is a shape it never returns — and a capability form
  // reading off one would offer no transport at all, which is a degradation the
  // form handles but not a state a suite should be asserting against.
  transports: TRANSPORTS,
  engine_floor: ENGINE_FLOOR_CAPABILITIES,
};

/**
 * Neutral answers, one per route.
 *
 * "Neutral" means the state a suite that is not about this route wants: a healthy
 * read of an empty thing. A suite that IS about a route passes its own fixture,
 * so no default here is ever the subject of an assertion.
 */
/**
 * One `/config/capabilities` row as the real route composes it for a builtin.
 *
 * Mirrors the engine's own `CapabilityRegistry.describe()` entry plus the three
 * fields the route joins on (`program`, `reachable`, `action`). Kept as a builder
 * rather than seven literals so the SHAPE is stated once: a row that gained a
 * field in the route and not here would otherwise be a divergence spread across
 * seven copies. `version` is omitted when absent, because the engine omits it
 * rather than sending an empty string.
 */
function builtinCapabilityRow(
  capability: string,
  providerName: string,
  nature: "deterministic" | "model_backed",
  version?: string,
): Record<string, unknown> {
  return {
    capability,
    transport: "builtin",
    provider: {
      name: providerName,
      kind: "builtin",
      nature,
      transport: "builtin",
      ...(version === undefined ? {} : { version }),
    },
    configured: false,
    declared_at: "",
    timeout_s: 120,
    program: "",
    // Builtin: the engine's reachability check skips it, so this is "not
    // applicable" rather than false. Coercing it to false would render every
    // unconfigured capability as a broken provider.
    reachable: null,
    action: "",
  };
}

/** A plausible binding digest. The routes always emit a sha256 hex string here. */
const STUB_FINGERPRINT = "0".repeat(64);

/**
 * The minimum a `complete` report carries, in `ConformanceReport.to_json_object()`'s
 * shape. `declined_detections` and `excused` are present because the engine's own
 * serializers emit them — a report without them is a shape no route returns, and
 * the "declined N planted defects" qualifier cannot be rendered without them.
 */
const STUB_REPORT: Record<string, unknown> = {
  capability: "review",
  candidate: "/usr/bin/true",
  passed: true,
  declared_checks: [],
  declared_fixtures: [],
  gaps: [],
  declined_detections: 0,
  results: [],
};

/**
 * One capability's conformance state, in the shape BOTH conformance routes return.
 *
 * The GET returns this object; the POST returns it with `ok: true` added. Kept as
 * a builder for the same reason as {@link builtinCapabilityRow}: the shape is
 * stated once, so a field the routes gain cannot be missing from one default and
 * present in the other. Every one of the eleven fields the routes emit is here —
 * a default carrying a subset would be a shape no route returns, and a panel
 * authored against it would render off fiction.
 *
 * The VALUES are held to the same standard as the field names. `binding_current`
 * is always a digest, never "" — the routes compute it from the resolved binding
 * on every reply, so an empty string is a state neither route can produce. The
 * job-bearing fields are derived from `status`, not from whether a caller
 * happened to pass a job id: every status except `absent` and `not_applicable`
 * MEANS a job exists, so a caller asking for `complete` cannot accidentally build
 * an empty job id beside it, nor an empty fingerprint beside a finished run. A
 * present job's fingerprint EQUALS `binding_current`, because the POST sets both
 * from the one digest it just computed, which is what makes `stale` false at the
 * moment a run starts.
 *
 * `stale` is derived server-side and is never a client's own comparison: a client
 * is not shown the binding's env, so any fingerprint it computed would digest
 * something else. `is_builtin` describes what is configured NOW, so a client can
 * tell that a re-run is refused even when a stored report reads `complete`.
 */
function conformanceState(
  over: {
    status?: "absent" | "not_applicable" | "running" | "complete" | "failed";
    jobId?: string;
    candidate?: string;
    report?: unknown;
    isBuiltin?: boolean;
  } = {},
): Record<string, unknown> {
  const status = over.status ?? "absent";
  // A job exists for every status except the two that mean "no run happened".
  const hasJob = status !== "absent" && status !== "not_applicable";
  return {
    capability: "review",
    status,
    job_id: hasJob ? over.jobId ?? "job-stub-0001" : "",
    candidate: over.candidate ?? (hasJob ? "/usr/bin/true" : ""),
    binding_fingerprint: hasJob ? STUB_FINGERPRINT : "",
    binding_current: STUB_FINGERPRINT,
    stale: false,
    is_builtin: over.isBuiltin ?? false,
    // The server's per-invocation cap, not the binding's timeout_s. Present even
    // with no run, because a panel must state the cost before offering to start.
    deadline_s: 10,
    // Derived from `status` for the same reason the job fields are: `failed` MEANS
    // the suite could not be carried out, and the worker always records a reason
    // as "ClassName: message", so an empty error beside it is a state no route
    // returns. Every other status carries no error.
    error: status === "failed" ? "TransportFailure: the candidate could not be run" : "",
    // Likewise `complete` MEANS a report exists — a recorded complete job always
    // carries a serialized one — so a null report beside it is unreachable. A
    // caller may pass its own; only the no-report statuses default to null.
    report: over.report ?? (status === "complete" ? STUB_REPORT : null),
  };
}

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
  // One row per delegable capability, each on its builtin — the shape the route
  // returns when nothing has bound an external provider. `configured: true` beside
  // unconfigured BINDINGS is a real combination, not a contradiction: it means a
  // document exists on disk with no `capabilities` section. The route cannot
  // return an empty list — `resolve_bindings` pre-seeds all seven — so an empty
  // default would be a shape no route ever serves, and a suite rendering off it
  // would be asserting against fiction. `reachable` is null on every row because a
  // builtin is reachable by construction and the engine's check skips it; null
  // means "not applicable", NOT "unreachable". Only the analysis builtin carries a
  // `version`. A suite that is about this pane passes its own fixture.
  capabilities: {
    body: {
      configured: true,
      capabilities: [
        builtinCapabilityRow("analysis", "local-analyzer", "deterministic", "1"),
        builtinCapabilityRow("authoring", "engine-authoring-turn", "model_backed"),
        builtinCapabilityRow("review", "engine-review-turn", "model_backed"),
        builtinCapabilityRow("implementation", "engine-implementation-dispatch", "model_backed"),
        builtinCapabilityRow("validation_rules", "engine-validation-rules", "deterministic"),
        builtinCapabilityRow("watch_sources", "engine-watch-sources", "deterministic"),
        builtinCapabilityRow("model_catalog", "engine-model-catalog-host", "deterministic"),
      ],
    },
  },
  // The shape `GET /config/workflow` really returns: one row per DECLARED stage
  // (so an unconfigured stage is present and says so), the selection separately
  // from the stages it supplied, and `gates` NULL only when the stored list
  // cannot be read. `[]` and null are different answers, so neither default may
  // stand in for the other.
  workflow: {
    body: {
      configured: true,
      project: null,
      preset: null,
      stages: [
        {
          stage: "isolate",
          source: "unconfigured",
          from_preset: false,
          bundled: false,
          preset: "",
          declared_at: "",
          commands: 0,
          skipped: true,
          summary: "isolate: not configured, so this stage is skipped",
          argv: [],
          runs_at: "isolation",
        },
        {
          stage: "submit",
          source: "unconfigured",
          from_preset: false,
          bundled: false,
          preset: "",
          declared_at: "",
          commands: 0,
          skipped: true,
          summary: "submit: not configured, so this stage is skipped",
          argv: [],
          runs_at: "delivery",
        },
        {
          stage: "verify",
          source: "unconfigured",
          from_preset: false,
          bundled: false,
          preset: "",
          declared_at: "",
          commands: 0,
          skipped: true,
          summary: "verify: not configured, so this stage is skipped",
          argv: [],
          runs_at: "delivery",
        },
        {
          stage: "publish",
          source: "unconfigured",
          from_preset: false,
          bundled: false,
          preset: "",
          declared_at: "",
          commands: 0,
          skipped: true,
          summary: "publish: not configured, so this stage is skipped",
          argv: [],
          runs_at: "delivery",
        },
        {
          stage: "teardown",
          source: "unconfigured",
          from_preset: false,
          bundled: false,
          preset: "",
          declared_at: "",
          commands: 0,
          skipped: true,
          summary: "teardown: not configured, so this stage is skipped",
          argv: [],
          runs_at: "archive",
        },
      ],
      user_presets: [],
      delivery_flow_stages: ["submit", "verify", "publish"],
      gates_scope_is_app: true,
      gates: [],
      gates_unreadable: false,
      gate_errors: [],
    },
  },
  // Both conformance routes answer the SAME eleven-field shape — the GET directly,
  // the POST with `ok: true` added — so both defaults are built from one builder.
  // `absent` is the neutral state: no run has been started for this capability on
  // this gateway. `deadline_s` is present and non-zero even with no run, because
  // it is the cap the server will apply and a panel has to state it BEFORE
  // starting a run.
  conformance: { body: conformanceState() },
  conformanceStart: {
    body: {
      ok: true,
      ...conformanceState({ status: "running", jobId: "job-stub-0001", candidate: "/usr/bin/true" }),
    },
  },
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
