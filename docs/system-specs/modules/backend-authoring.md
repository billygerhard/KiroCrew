# Authoring a config-defined backend

A **backend descriptor** teaches Kiro Crew to drive an agent process it does not
bundle — any executable that speaks public ACP over stdio and drives an LLM with
its own authentication. It is pure configuration: an id, an executable, an argv
template, a model source, a routing declaration, and an
MCP-delivery mode. No code ships with it, and no capability claim either: a
descriptor cannot admit its harness to any session-path capability set (see
*Capabilities are not configurable* below). A descriptor is served by one generic
runtime class, `DescriptorHarness`, which answers every harness seam from the
descriptor data, so an ACP host an operator already runs is served with no
per-host code branch.

The line this feature draws is deliberate and load-bearing: **a descriptor names
no code.** The schema has no `adapter` key, and the bundled harnesses
(`KiroHarness`, `KasHarness`, `CodexHarness`, and the rest) are hand-written
classes that never pass through the descriptor parser — a config key that
selected a Python entry point would let configuration choose code. A harness
whose invocation genuinely cannot be expressed as an argv template belongs
upstream as a reviewed bundled class, not here.

This page is the operator reference: what `harnesses.json` is, where it lives,
why an agent cannot write it, what every field means and how it is validated,
what routing decides, and how an edit reaches a served backend. The invariants
that keep an added harness from disturbing the Kiro path are in
[harness-parity.md](harness-parity.md); the onboarding sequence a *bundled*
harness walks is [harness-onboarding.md](harness-onboarding.md); the
provider/registry model both plug into is in [providers.md](providers.md).

## Where the descriptors live, and why the agent cannot write them

Operator descriptors live in **`harnesses.json`**, a dedicated file directly
under the crew home beside `config.json` (resolved through
`descriptor.operator_harnesses_path`, which reads the same `config_dir` the
config file does, so it honours `KIROCREW_HOME` and the test data home
identically). The file is a JSON object keyed by harness id:

```jsonc
{
  "acme": {
    "display_name": "Acme Agent",
    "executable": "acme-acp",
    "argv": ["{executable}", "acp", "--workdir", "{workdir}"],
    "agent_args": ["--agent", "{agent}"],
    "model_source": "acp_advertised",
    "routing": "agent_spec"
  }
}
```

A dedicated file rather than a `config.json` key, deliberately. Each descriptor
names an `executable` and an `argv` template that the gateway **resolves and
spawns** to serve a backend, so the file IS an execution grant, not an input to a
decision about one. That puts it in the strongest class of the write-protection
set (`security._WRITE_PROTECTED_HOME_PATHS`, the same footing as `app-sources`):
an agent that could write the file could plant an attacker-chosen
`executable`/`argv` and have Kiro Crew's own trusted spawner run it — arbitrary
code laundered through the product's backend launcher, re-armed on every
listing. Nothing downstream neutralizes it, because unlike a `config.json` value
the loader clamps, a descriptor's command is simply spawned; the registry's
validation pass costs a malformed row its listing but does nothing to a
well-formed forgery pointing at an attacker binary.

The protection is two-sided and reads-open:

- **The file-edit tool gate** refuses the agent's own write to the path
  (`security._WRITE_PROTECTED_HOME_PATHS`). Kiro Crew's own writer opens the path
  directly through `atomic_write` and does not route through the gate, so an
  operator editing the file — outside an agent session — keeps working.
- **The OS sandbox** seals the leaf read-only for a sandboxed child
  (`sandbox._CREW_READONLY_LEAVES`), with a Linux absent-file pre-create
  (`sandbox._CREW_PRECREATE_READONLY_FILE_LEAVES`) so the mount seal has a target
  even on an install that has never authored a descriptor — the absent-and-
  therefore-writable default this list exists to close. This is the shell-side
  half: a command-text matcher matches no paths here (as with `config.json`), so
  a kernel write denial is what holds regardless of how a command spells its way
  there, including runtime construction like `$(printf ...)` that the deny-list's
  text tiers cannot see.

The file is **write-protected but deliberately not sensitive**: it holds no
secret and is READ on every listing (the registry loads it to enumerate
backends, and Settings reads it to render the inventory), so classifying it
sensitive would break the feature. The file-read tool stays open.

## The full field reference

Every descriptor is parsed by `descriptor.descriptor_from_mapping`, which never
raises: a malformed entry returns a list of diagnosable reasons and costs that
one harness its row, never the gateway's boot. Unknown top-level keys are a
validation failure (`descriptor.DESCRIPTOR_KEYS` is closed) — a typo'd key would
otherwise be silently ignored, leaving the operator with a harness that quietly
does not do what they configured.

| Field | Required | Rule |
|---|---|---|
| `id` (map key) | yes | Non-empty; lowercase letters, digits, and hyphens only; at most `descriptor.HARNESS_ID_MAX_LEN` (32) characters; unique across all harnesses. A descriptor may also carry `id` as a field, and the two must agree. |
| `display_name` | no | Any string; falls back to the id when empty (`HarnessDescriptor.label`). |
| `executable` | yes | Non-empty. An absolute path, or a bare PATH-resolvable name. Resolution and trust attestation happen at spawn. |
| `argv` | yes | An array of string tokens (not a bare string), non-empty, whose **first token is exactly `{executable}`**. Each token may use only closed-vocabulary placeholders and carry no unbalanced brace; `{model}`/`{agent}` are refused here (see the placeholder rule). |
| `agent_args` | with `agent_spec` routing | Array of string tokens; `{agent}` is legal here and only here; emitted only when an agent is selected. Required to carry `{agent}` when `routing` is `agent_spec`; optional otherwise. |
| `model_args` | no | Array of string tokens; `{model}` is legal here and only here; emitted only when a model is pinned. |
| `model_source` | no | One of `acp_advertised` (default) or `static`. `static` **requires** a non-empty `models` list. |
| `models` | when `static` | Array of non-empty strings; consulted only when `model_source` is `static`. |
| `mcp_delivery` | no | One of `agent_file` (default) or `session_array`. |
| `routing` | no | One of `agent_spec` or `session_config`, or absent. Absent or unrecognized registers the harness known-but-**unselectable**. See routing below. |
| `permission_config` | when `session_config` | An object `{option, value}` (both non-empty strings), required when `routing` is `session_config` and forbidden otherwise. |
| `adapter` | — | **Not a key.** A descriptor never names code; the bundled harnesses are classes that never pass through this parser. |

A validation failure names the field and the fix (for example `harness 'acme':
argv template must start with {executable} so the executable that is
trust-attested is the one that runs`). The reasons stay retrievable for the
Settings surface through `operator_registry.invalid_operator_harnesses`, so a
malformed entry is shown inline with what is wrong rather than dropped silently.

### The argv template and the `{executable}` rule

`argv` is rendered to a concrete argv list by `descriptor.render_argv` through
token-wise substitution — never through a shell. The placeholder vocabulary is
closed (`descriptor.ARGV_PLACEHOLDERS`): `{executable}`, `{agent}`, `{model}`,
`{workdir}`. An unknown placeholder or an unbalanced brace (`--dir={workdir`) is
a registration-time reason, not a half-working spawn, because matching the whole
brace run is what makes an unknown token detectable instead of surviving to exec
as a literal. Every such reason names the offending token by **position**
(`argv[2] uses an unknown placeholder`), never by content: the reasons are shown
on the backends listing, which every authenticated user can read, and an argv
literal is operator text that may carry a credential — including one typed inside
braces by mistake.

The **first token must be `{executable}`.** `argv[0]` IS the program, and
`executable` is the field that is resolved and trust-attested at spawn; a
template whose first token is a literal would exec bytes nobody checked, because
a bare name is re-resolved by exec through PATH at spawn time and the file that
was attested and the file that runs need not be the same one. Requiring the
placeholder is what makes the attestation load-bearing rather than decorative.

Substitution is single-pass: a model id or agent name that happens to contain
`{workdir}` reaches exec as those literal bytes, not as the working directory.
And because rendering builds a `list[str]` for `subprocess` with no shell, every
value lands as exactly one argv element regardless of the spaces, quotes, or
metacharacters it contains.

### The placeholder-block rule

`{model}` is meaningful only in `model_args` and `{agent}` only in `agent_args`,
because those are the blocks `render_argv` gates on a value being present: the
`agent_args` block is emitted only when an agent is selected, the `model_args`
block only when a model is pinned. In the ungated `argv` block — or in each
other's block — those placeholders render to the empty string whenever the value
is absent, execing a silent empty argument (`--model=` or a bare `""`). Rejecting
them at validation turns that footgun into a registration-time reason.

So `argv: ["{executable}", "--model", "{model}"]` is refused — put the model flag
in `model_args`, where it is emitted only when a model is actually pinned.
`{executable}` and `{workdir}` carry no gating and stay legal in every block.

### Capabilities are not configurable

A descriptor has **no `capabilities` key**, and a file that carries one is
refused with an unknown-field reason. Every session-path capability in Kiro Crew
is a frozen, code-reviewed membership set in `agent_sdk.backends`
(`ACP_BACKENDS_SESSION_MCP_ARRAY`, `ACP_BACKENDS_HARNESS_OWNED_SESSIONS`,
`ACP_BACKENDS_LOAD_WITHOUT_MODES`, `ACP_BACKENDS_MODEL_VIA_CONFIG_OPTION`,
`ACP_BACKENDS_ADVERTISED_MODEL_SELECTION`, and the kiro-only sets such as
`ACP_BACKENDS_INTERNAL_SANDBOX` and `ACP_BACKENDS_SESSION_SHARING`), and a
registered descriptor id is a member of none of them. Every gate that reads one
of those sets therefore takes its default branch for a config-authored host:

| Frozen set | What a descriptor-backed harness gets |
|---|---|
| `SESSION_MCP_ARRAY` | The `session/new` `mcpServers` array is not the channel the client assumes the harness reads its servers from. (`mcp_delivery: session_array` still shapes what `DescriptorHarness.session_mcp_servers` does with the array -- that is a harness-seam decision, not a session-path gate.) |
| `HARNESS_OWNED_SESSIONS` | A resume is pre-checked against the Crew-side transcript before `session/load`, the default posture. |
| `LOAD_WITHOUT_MODES` | A reopened session is judged loaded by the presence of a `modes` block, the default posture. |
| `MODEL_VIA_CONFIG_OPTION` | A model change does not travel over `session/set_config_option`; pin the model through `model_args` instead. |
| `ADVERTISED_MODEL_SELECTION` | The pinned model is not resolved from the list advertised at `session/new`. |

This is harness-parity H6/H7 applied to configuration: a capability is granted
by opt-in membership that a reviewer can read in the vocabulary home, never by a
claim in a data file. A harness that needs one of these branches is admitted by
adding its id to the named set -- a core change with a test, not a line in
`harnesses.json`.

A registered descriptor id is in none of the `ACP_BACKENDS_*` sets on the
runtime side either, so `DescriptorHarness` inherits the fail-safe answers from
its `MembershipHarness` base: `internal_sandbox`, `pod_home_remap` and
`reads_markdown_agent_specs` all answer False, and the reclaim policy passes the
operator's thresholds through — exactly the posture a host that has demonstrated
nothing should get.

### Where the models come from

`model_source` decides how the model catalog for the harness answers (read back
through `GET /api/models` under the harness's own namespace):

- **`acp_advertised`** (the default) reads what a live session on that harness
  advertised in its `session/new` response. Before any session has started the
  catalog is empty — the models appear once the harness has run once. This is the
  right default for a harness that enumerates its own models over ACP.
- **`static`** reads the descriptor's own `models` list and requires it to be
  non-empty. `static` is the declaration "I cannot enumerate my models over ACP,
  here they are instead", so an empty list is refused rather than accepted-and-
  listed-empty: it would leave the composer with no model to offer and no way to
  obtain one.

### MCP delivery

`mcp_delivery` chooses the SHAPE of `DescriptorHarness.session_mcp_servers`, and
it is only ever a transform — a descriptor can never make this seam mount a
server the caller did not request:

- **`agent_file`** (the default) is passthrough. The harness reads its MCP
  servers from its own agent-spec/config channel, so the `session/new` array is
  an override of same-named entries and returning the caller's list unchanged
  keeps the request byte-identical — the kiro-family posture, and the posture
  that touches nothing.
- **`session_array`** is narrowing. The harness learns its servers only from the
  `session/new` array, so an element whose transport it never advertised can fail
  the whole `session/new`; the array is narrowed against the `mcpCapabilities`
  this session's handshake reported, reusing the codex path's
  `drop_unadvertised_transports`. An unknown handshake (absent or non-dict
  `mcpCapabilities`) passes the array through untouched — empty means "nothing is
  known", never "nothing is supported", so narrowing to nothing there would strip
  every tool from every session.

## Routing: the selectability gate

Routing is the field that decides whether an operator can *choose* a valid
descriptor, and it is where this feature is honest about a limit. A descriptor is
registered as **known** whenever it validates — spellable in a config value,
nameable in a governance rule — but it is **selectable** (offered as a session
backend) only when it declares how a session's permission decision reaches the
harness. The two verified forms are the only two the generic OS-boundary mask
path can honour:

- **`agent_spec`** — the permission decision travels as an agent-spec selection.
  In practice: your binary reads agent specs (a `--agent`-style selection on the
  command line, carried by an `agent_args` block) and asks by construction. This
  routing **requires** an `agent_args` block that carries `{agent}`: the routing
  says the selection travels on the command line, and that block is the only
  thing that can deliver it, so an `agent_spec` descriptor with an empty
  `agent_args`, or one of fixed flags without the placeholder, is a reason at
  load (the mirror of `session_config`'s `permission_config` rule below). A
  registered `agent_spec` descriptor therefore always made a selection at spawn
  for the later activation check to confirm
  (`DescriptorHarness.verifies_agent_activation`).
- **`session_config`** — the permission decision travels over
  `session/set_config_option`. In practice: your binary advertises an ACP config
  option, Kiro Crew sets it and verifies it in force. This routing **requires**
  `permission_config: {option, value}` naming the option and the value to set;
  the coupling is enforced both ways (a `session_config` routing with no
  `permission_config` is a reason, and a `permission_config` on any other routing
  is a reason). For such a descriptor the credential mask is resolved at spawn on
  the same routing key the codex harness uses, and a tier that would drop it
  aborts the spawn — a `session_config` host's privileged tools ask over the
  config option rather than by construction, so ACP cannot make it ask about a
  passive read, and the OS-boundary mask is the compensating control.
- **neither** (absent or an unrecognized string) — your backend is listed but
  **not selectable**, because nothing establishes that its tool calls reach the
  host permission gate. It stays visible in Settings with that reason, retrievable
  through `operator_registry.unselectable_operator_harnesses`, and it is never
  silently servable. This is a valid state, not a malformed one: an empty routing
  is not a validation error.

The registration order (`operator_registry.load_and_register_operator_descriptors`)
makes this concrete for each valid descriptor: `register_known_backend` first (it
records the routing the selection step then reads), then
`register_operator_harness` (so the id resolves through `harness_for` as a
`DescriptorHarness`), then `register_selectable_backend` **only** when the
routing is recognized **and** the gateway holds a matching routing attestation
(next section). An unroutable descriptor is left known-but-unselectable with its
reason; a routed-but-unverified one is left known-but-unselectable with the
reason that it has not been verified yet.

## Routing verification: a declaration is not evidence

A `routing` value is what the descriptor *says*. A host can load the declared
agent or accept the declared config option and still execute tool calls without
ever sending `session/request_permission` — and every tool call it made would
bypass PreToolUse, the governance deny rules and the SEL audit. So a declared
routing never makes a descriptor selectable on its own. Selectability requires an
**attestation this gateway recorded after verifying the routing end to end**
(`acp/harness/routing_verification.py`).

Verification is an operator action: **Settings → AI backends → Verify routing**
on the row (`POST /api/backends/{id}/verify`, owner-gated and audited). The
gateway then:

1. spawns the harness once **through the production provider factory** — the same
   argv, agent selection or config-option write, sandbox mask and runtime start
   path a chat would use — in an empty scratch working directory;
2. sends one probe turn asking the agent to write a file with a fixed name into
   that directory;
3. **denies every permission request** the harness raises, and counts them;
4. when the turn ends, checks whether the file exists.

| Verdict | Meaning | Effect |
|---|---|---|
| `verified` | At least one permission request arrived and nothing was written: the host asked before acting and honoured the refusal. | An attestation is recorded and the backend becomes selectable immediately, no restart. |
| `violation` | The probe file exists although every request was denied: a tool call executed without going through the permission gate. | Nothing recorded; the row shows the reason. Fix the harness, then verify again. |
| `inconclusive` | No permission request and nothing written (the agent refused the task, answered in prose, errored, or the turn timed out). | Nothing recorded; run it again, or check that the harness has a model that can act. |

The attestation is bound to two things. First, the descriptor's **spawn
fingerprint** — a hash over `executable`, `argv`, `agent_args`, `model_args`,
`routing`, `permission_config` and `mcp_delivery`; editing any of those revokes it
by construction. Renaming (`display_name`) or changing a static `models` list does
not. Second, the **content digest of the executable that was verified**: a path
is what the descriptor says, a digest is what actually ran, and a binary replaced
under an unchanged path would otherwise inherit the attestation. The digest is
re-checked at boot and again **immediately before every spawn**, against the file
the spawn is about to exec (`DescriptorHarness.resolve_spawn`); a mismatch
refuses that spawn, withdraws selectability on the spot, and the row is back to
*Routing not verified* with the reason, until the operator verifies the
replacement. The store is a gateway-written file beside `harnesses.json`,
`backend-routing-attestations.json`, fenced exactly as the descriptor file is
(agents read it, never write it; the sandbox seals it read-only) because it is a
selectability grant one step downstream of the execution grant.

The probe drives **the descriptor's own provider**, constructed directly for
its id with the configured default agent and sandbox — never the per-chat
selection gate, which would degrade an unselectable pick to the configured
default and attest a backend the probe never ran. The provider's backend
identity is asserted before any verdict counts; a mismatch is `inconclusive`
and records nothing.

The bytes are pinned **around** the run, not after it: the probe resolves the
executable and digests it before anything spawns, its own spawn is held to that
digest (a file swapped between resolution and exec is refused, not probed), and
the digest is taken again when the turn ends. A file that changed while the probe
ran yields `inconclusive` and records nothing — a verdict only ever describes the
bytes that were in place from resolution to the end of the turn. The digesting
and the store rewrite are file I/O and run off the gateway's event loop.

Verified routing is necessary for selectability, not sufficient. The deployment's
`agent_backend` policy (the governance ceiling an administrator writes into
`security_policy.json`) narrows the selectable set at boot, and a backend
verified *after* boot is put through that same narrowing the moment it is
registered: a denied backend comes back **verified but not selectable**, the row
says so (`routing verified, but this deployment's agent_backend policy does not
permit the backend`), and no Verify action is offered again, because
verification is not what it lacks.

The probe costs one real turn on the harness's model and can take a couple of
minutes. It is the one place a descriptor backend runs before it is selectable,
and it runs only when the owner asks.

## Lifecycle: edit, restart, verify, listed

`harnesses.json` is read **once, at boot** — the load runs inside the
`ProviderRegistry.register_acp_backends()` seam (the public edition's
`DefaultProviderRegistry`), which `bootstrap_context` invokes before the first
config load resolves `agent.acp_backend`,
so that a persisted value naming an operator harness survives
`resolve_selected_backend` (which reads the selectable registry live). An edit to
the file therefore takes effect **on the next gateway start, not live**. This is
the same restart every boot-time registration implies, and it is why an operator
edits the file and then restarts to see the row appear — as *Routing not
verified* on its first appearance, until **Verify routing** on that row records
the attestation and moves it to the selectable list (no second restart).

The load is idempotent (a second bootstrap pass skips an id already in
`ACP_BACKENDS_KNOWN`) and total: malformed JSON costs the whole file one invalid
row, a non-dict entry costs that id its row, and everything else still registers.
One bad entry never blocks boot.

## A complete worked example: `acme`

Take a fictional in-house agent CLI, `acme`, that speaks ACP over stdio. It
reads agent specs — so it asks for permission by construction, which is the
`agent_spec` routing — advertises its models over `session/new`, and ships as
its own adapter binary `acme-acp` on `PATH`. The full descriptor:

```jsonc
{
  "acme": {
    "display_name": "Acme Agent",
    "executable": "acme-acp",
    "argv": ["{executable}", "acp", "--workdir", "{workdir}"],
    "agent_args": ["--agent", "{agent}"],
    "model_args": ["--model", "{model}"],
    "model_source": "acp_advertised",
    "routing": "agent_spec"
  }
}
```

Field by field:

- **`acme`** (the key) is the harness id — the stable handle everything
  references: the configured `agent.acp_backend`, the per-chat pick, the model
  namespace, the session-map binding.
- **`display_name: "Acme Agent"`** is the human name in the picker and Settings; drop it
  and the id is used.
- **`executable: "acme-acp"`** is resolved at spawn on the generic plain-binary
  ladder (env override, then mise, then augmented PATH — the same
  `resolve_descriptor_executable` walk opencode's and goose's binaries use). A
  bare name that does not resolve aborts the spawn with a "not found (searched
  ...)" message naming the directories walked.
- **`argv`** starts with `{executable}` (so the attested absolute path is what
  execs), then Acme's own `acp` subcommand and a `--workdir {workdir}` that
  renders to the session's working directory.
- **`agent_args: ["--agent", "{agent}"]`** is emitted only when an agent is
  selected. Because Acme is `agent_spec`-routed and carries this block, its agent
  activation is verified after spawn.
- **`model_args: ["--model", "{model}"]`** is emitted only when a model is
  pinned; with no model selected the block is dropped and Acme runs on its own
  default rather than execing an empty `--model`.
- **No `capabilities`** -- there is nothing to claim. Acme is a member of no
  session-path capability set, so the client pins its model through
  `model_args` and takes the default branch at every capability gate.
- **`model_source: "acp_advertised"`** — Acme's catalog fills from what a live
  session advertises; it is empty until Acme has run once.
- **`routing: "agent_spec"`** — the selectability gate. No `permission_config` is
  needed (that belongs only to `session_config`), and no `mcp_delivery` is set,
  so it defaults to `agent_file` passthrough.

After an operator writes this and restarts the gateway, `acme` appears as
**Acme Agent** in Settings → AI backends marked *Routing not verified*. The
operator presses **Verify routing**; the gateway spawns Acme once, asks it to
write a probe file, denies the permission request Acme raises, confirms nothing
was written, records the attestation, and the row moves to the selectable list
and into the new-chat backend picker — no second restart. Its model catalog is
empty until Acme serves its first session, then fills from the advertised list.
Had the descriptor omitted `routing`, `acme` would still appear in Settings —
under the unroutable list, with the reason that nothing establishes its tool
calls reach the permission gate, and with no Verify action — but never in the
picker.

## Choosing a backend per chat and per subagent

Two selection surfaces let a session run on a backend other than the configured
global default. Both are distinct from the global `agent.acp_backend`, and both
route every non-empty value through the single selectability gate
(`resolve_selected_backend` / `selectable_backend_values`, harness-parity H4), so
neither can offer a value session creation would refuse.

**Per chat.** `POST /api/chat/slots/{slot}/backend` sets the backend for one chat
slot; a body of `{"backend": ""}` clears the pin and inherits the global default.
Unlike a model change, there is **no live in-place switch**: a backend is a
distinct harness process, and no `session/set_model`-style call can move a
running session across harnesses. So changing the backend **always resets the
session** — the live process is torn down and the next message cold-starts on the
new harness through the provider factory. A turn in flight or a parent with
children attached answers 409 (the reset would tear down the streaming turn or
kill the runtime the children run on); a no-op (same value) returns without a
reset. A crew-bound remote slot refuses the pick — a crew-bound session's backend
is chosen on the crew, not here. The new-chat picker reads its rows from `GET
/api/backends`, which lists every selectable id with its label and which one is
the global default, plus the `invalid` and `unroutable` operator-descriptor
diagnostics for Settings.

**Per subagent.** `spawn_run`'s `backend` parameter overrides the backend for one
spawned subagent; `""` inherits the parent's. Like a per-spawn `model` or
`reasoning_effort`, a non-empty value **forces the dedicated-process path**: the
parent's shared runtime runs on its own backend and cannot switch per session, so
the override reaches the provider factory only on a fresh process (carried as
`backend_override`, validated selectable at admission). A per-spawn backend
therefore always costs a dedicated process rather than a shared-session spawn.
