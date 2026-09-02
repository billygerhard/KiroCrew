# Harness parity: Kiro first, everything else adapted

A *harness* is the agent process Kiro Crew drives over ACP. Kiro Crew has one
first-class harness — `kiro-cli` (`ACP_BACKEND_KIRO`, spelled `""`) — and a
growing set of adapted ones: Claude Code (`ACP_BACKEND_CLAUDE`), `KAS`
(`ACP_BACKEND_KAS`), the dormant `ACP_BACKEND_CODEX` seam, and whatever a
bring-your-own (BYO) adapter registers next.

Kiro, Claude Code and KAS are selectable on a plain public build; Claude Code in
particular is a shipped harness and not a dormant seam: `acp/client.py` owns the
whole Claude spawn path and the adapter is a public npm package, so an earlier
revision that left it out of the baseline removed only the switch, never a
capability. Whether the binaries are INSTALLED on a given machine is a different
question, answered by `agent_sdk/backend_install.py`'s probe rather than by
selectability.

`BASELINE_SELECTABLE_BACKENDS` is otherwise `ACP_BACKENDS_KNOWN`, so an id this
core can spell is an id an operator can choose unless something states the
exception — pinned by
`test_agent_backend_editable.py::test_baseline_ships_every_known_backend`, which
guards against an undocumented NARROWING rather than a widening.
`ACP_BACKEND_CODEX` is the one exception and it is named in that test: the spawn
path is complete, but `backend_install.py` has no probe for the adapter, so a
build offering the switch could not tell an operator what was missing when the
session failed to start. It becomes selectable through
`register_selectable_backend`, or through the baseline once that probe lands.

Read the invariants below against that tree: three harnesses can serve a real
session today, so a site that spells "kiro" by exclusion is already wrong on two
of them.

*Parity* here does not mean equal treatment. It means the opposite, stated
precisely: **an added harness may only adapt itself to the seams the Kiro
harness already runs through. It may not move, widen, generalize, or add a
branch to those seams.** A harness that cannot be adapted without changing the
Kiro path is not ready to land.

The failure mode this file exists to prevent is not a broken adapter — that
fails loudly on its own first session. It is the *silent capture* of the Kiro
path: a call site that spells "kiro" as `not is_<other>_backend`, so harness
number three inherits a capability, a sandbox waiver, or a session label that
nobody granted it, and the Kiro user who never chose another harness pays for it.
Two such sites shipped before this file existed
(`AcpProvider.is_session_sharing_eligible`, `AcpRuntime.spawn`'s
`is_kiro_cli`); both read as correct until you count the backends.

The transports these invariants constrain are specified in
[acp-client.md](acp-client.md) (framing, timeouts, the backend seam) and
[providers.md](providers.md) (the `LLMProvider` surface). The edition-level
registration seam is in
[platform-context.md](platform-context.md). This file only catalogs the
invariants and names what pins each one.

## How to read a row

- **Guarantees** is the property that goes RED when broken, not the
  implementation that happens to satisfy it today.
- **Pinned by** names the test module and function. Test modules live at `test/`
  in the repo root; sources live at `src/kiro_crew/`. A row marked
  *review-only* has no deterministic test — it is enforced by the
  `harness-parity` rule in `AUTOSDE.yaml`, which every AI review lane reads.
- An invariant is *closed* by its test, not by this document. If a row
  disagrees with the named test, the test is right.
- The ids are stable. Source docstrings and review findings cite them bare
  (`H4`, `H6`), so the id is the lookup key.

## Group A: Kiro is the default and the floor

These break by *addition*: a harness lands, nothing at these sites is edited,
and Kiro stops being the guaranteed path.

| Id | Guarantees | Pinned by | Constrains |
|---|---|---|---|
| H1 | `agent.acp_backend` defaults to `ACP_BACKEND_KIRO`, and `ACP_BACKEND_KIRO` is in `selectable_backends()` unconditionally. An operator who configures nothing, and an operator whose configuration is unusable, both get the Kiro harness. | `test_harness_parity.py::test_kiro_is_the_default_backend`, `::test_kiro_is_always_selectable` | `config/loader.py` (`AgentConfig.acp_backend`), `acp_backends.py` (`BASELINE_SELECTABLE_BACKENDS`) |
| H2 | A harness is chosen at `agent.acp_backend`. `agent.provider` stays `enum=["acp"]`: there is one provider and it is never the harness selector, because a second provider value would route around every invariant below. | `test_harness_parity.py::test_provider_enum_is_acp_only` | `config/loader.py` (`AgentConfig.provider`, `build_provider_factory`) |
| H3 | An unknown or unselectable persisted backend degrades to Kiro with a logged reason. It never raises and never survives — including the non-string shapes a hand-edited `config.json` can hold. Startup refusing with a reason is the contract; a stack trace or a silent foreign spawn is not. There is exactly ONE gate, and it reads `selectable_backends()` per call, so registering a backend is what makes a persisted value survive; the Kiro construction path gains no second check (H13). It must never read the platform context — `current_context()`'s lazy branch loads config and would re-enter the same load. | `test_harness_parity.py::test_unselectable_backend_degrades_to_kiro`, `::test_registering_a_backend_makes_it_survive_load`, `::test_config_load_never_reads_the_platform_context` | `acp_backends.py` (`resolve_selected_backend`), `config/loader.py` (`_normalize_acp_backend`) |
| H4 | Selectability has exactly ONE gate, and it logs. `AgentConfig.acp_backend` carries no static `enum`: a literal was frozen at import, before an edition registers a backend, and `validate_config_data` *deletes* an out-of-enum value before the loader sees it — so a registered preview harness was stripped from `config.json` with no degrade log at all. `resolve_selected_backend` is the gate; `GET /api/config/schema` supplies the live values the dashboard renders. | `test_harness_parity.py::test_selectability_has_one_logged_gate` | `config/loader.py` (`AgentConfig.acp_backend` metadata), `config/validation.py` (`validate_config_data`), `dashboard/handlers/agents.py` (`_supply_live_enum`) |

## Group B: identity is tested positively

The whole group is one rule with several faces: **no call site may express
"this is the Kiro harness" as the absence of another harness.** A negative test
is correct only while one harness can start, and it fails *open* — the other
harness is treated as Kiro. Three are selectable today, so `not
is_claude_backend` is not a rule waiting on a future harness to break it: it
already reads TRUE for KAS on a plain public build.

| Id | Guarantees | Pinned by | Constrains |
|---|---|---|---|
| H5 | Harness identity is a positive comparison against a named constant, or membership in a named set. `not is_claude_backend`, `!= ACP_BACKEND_KAS`, and `== "kas"` (bare literal) are all forbidden; `is_kiro_backend` and `backend in ACP_BACKENDS_<CAP>` are the forms. Enforced on the lines a change ADDS, not whole-tree — see the gate doc for why. A line the rule models wrongly opts out with a trailing `# harness-ok: <reason>` comment (the token, a colon, and a non-empty reason — a bare `harness-ok` does not suppress), so a genuine wire branch such as KAS's is silenced with its justification recorded inline. | `scripts/check_harness_parity.py` (rules, self-tested), `test_harness_parity.py::test_added_line_gate_self_test_passes`, `::test_added_line_gate_flags_a_planted_negative_test` | every module reading `AcpClient.backend` / `AcpProvider.is_*_backend` |
| H6 | A capability is granted by opt-in membership, never by negation, and the grant lives in one of two registers by consumer shape. PER-SESSION behavior gates (`is_session_sharing_eligible`, `supports_steer`, the internal-sandbox and identity-store reads) read the flag off the session's bound harness descriptor **fail-closed** (an undeclared flag reads `false`), declared on the bundled descriptor's `CapabilitySet` (`acp/harness_registry.py`) — reaching a harness by its legacy `acp_backend` spelling instead of its descriptor is the silent-capture shape this row prevents. TUNING CHANNELS keep one opt-in register per channel because a harness can implement one and not another: the adapter config-option wire for model/effort is the harness's `ProtocolProfile` (`supports_set_config_option`, equal by construction to `ACP_BACKENDS_MODEL_VIA_CONFIG_OPTION` / `ACP_BACKENDS_EFFORT_VIA_CONFIG_OPTION` for every bundled harness), and `ACP_BACKENDS_KIRO_SLASH_COMMANDS` membership decides who is sent `_kiro.dev/commands/execute` and who gets the workspace `cli.json` overlay written for them. A harness in no register must not inherit a channel that answers `-32601`, nor collect an overlay it never reads and the membership-gated clear can never remove. | `test_harness_parity.py::test_session_sharing_is_opt_in`, `::test_steer_is_opt_in`, `::test_model_switch_channel_is_opt_in`, `::test_effort_channel_is_opt_in`, `::test_only_overlay_readers_are_written_to`, `test_harness_capability_views.py::test_leaf_sets_descriptors_and_shipped_literals_all_agree` | `providers/acp.py` (`AcpProvider.is_session_sharing_eligible`, `change_effort`, `clear_effort`, `_apply_effort_overlay`, `_apply_tool_search_overlay`, `stream_command`), `acp/client.py` (`AcpClient.supports_steer`), `acp/harness_registry.py` (`BUNDLED_DESCRIPTORS`), `acp_backends.py` |
| H7 | `is_kiro_cli` is a positive Kiro test at every call site. It drives internal-sandbox delegation: macOS skips Kiro Crew's seatbelt because Kiro's sandbox cannot nest inside it, and Windows permits the official Kiro backend to run despite having no Kiro Crew OS wrapper. Passed for a harness with no internal sandbox, it hands isolation to a layer that never starts; this is the only Group B row that is also a security invariant. **Windows requires `is_kiro_cli is True` exactly** — `None` and `_spawns_kiro_cli` basename inference can never grant the backend-less-host exception. On macOS a site may grant membership explicitly or pass `None` to defer to the positive basename test. The membership itself is the `internal_sandbox` flag on the bundled descriptor. | `test_harness_parity.py::test_is_kiro_cli_is_positive`, `test_sandbox_argv.py::TestKiroInternalSandboxExclusion` | `acp/runtime.py` (`AcpRuntime.spawn`), `acp/client.py` (`AcpClient.ensure_ready`), `sandbox.py` (`wrap_argv`, `_spawns_kiro_cli`), `acp/harness_registry.py` (`internal_sandbox`) |
| H8 | New harness identifiers live in `acp_backends.py` — a LEAF module, so every consumer can name the constants rather than copy them — and are added to `ACP_BACKENDS_KNOWN`; every capability set is a subset of it; and `AcpProvider.__init__` rejects anything outside it. `ACP_BACKEND_KIRO` is the empty string, so a value that falls through every identity check spawns `kiro-cli` under a foreign label. `acp/types.py` re-exports the vocabulary and remains the import site for existing callers. | `test_harness_parity.py::test_capability_sets_are_subsets_of_known_backends`, `::test_unknown_backend_rejected_at_construction`, `::test_codex_is_known_but_not_shipped_selectable` | `acp_backends.py` (`ACP_BACKENDS_KNOWN`), `providers/acp.py` (`AcpProvider.__init__`), `scripts/check_harness_parity.py` (`VOCABULARY_PATH`) |

## Group C: the Kiro path keeps its own machinery

An adapter that lands by *generalizing* a Kiro-specific step to a
lowest-common-denominator one has degraded the Kiro session even when every
test still passes.

| Id | Guarantees | Pinned by | Constrains |
|---|---|---|---|
| H9 | `kiro-cli` keeps its pre-spawn agent materialization (`kiro-cli` discovers selectable modes from `~/.kiro/agents/*.json` at startup, so a later `set_mode` fails with "Mode not found" without it) and its `--model` pin (the only way to run a model outside the agent's provider), and the argv it spawns with is byte-identical to the construction that shipped before spawn became descriptor-driven. `AcpRuntime.spawn` now renders every harness from its descriptor, which is exactly the dict-of-builders shape that drops both silently — so both live where a harness declares them (the `agent_args` / `model_args` convention blocks on the kiro descriptor, `ensure_agent_materialized` in `KiroAdapter.pre_spawn`) and the golden argv is pinned against the pre-migration formula rather than against a literal. The formula and the render diverge in exactly one place, deliberately: the pre-migration construction emitted `--agent ''` unconditionally, while `render_argv` drops the whole `agent_args` block for an empty agent rather than passing an empty flag value. Every spawn path supplies an agent, so the divergence is unreachable in service; it is recorded because an unrecorded one reads as a byte-identity claim that is simply false. The dormant codex seam keeps its own `AcpClient` spawn branch too (`_is_codex` → `_resolve_codex_acp_bin`), pinned the same way. | `test_harness_parity.py::test_kiro_spawn_argv_keeps_its_own_branch`, `::test_codex_spawn_keeps_its_own_branch`, `test_harness_spawn.py::test_kiro_rendered_argv_is_byte_identical_to_the_legacy_construction`, `::test_an_empty_agent_is_the_one_documented_divergence_from_the_legacy_formula`, `::test_spawn_hands_wrap_argv_the_legacy_kiro_argv` | `acp/harness_registry.py` (kiro descriptor), `acp/harness_adapters.py` (`KiroAdapter`), `acp/runtime.py` (`AcpRuntime._render_spawn_argv`), `acp/client.py` (`_resolve_codex_acp_bin`) |
| H10 | Protocol version and client capabilities stay per-harness literals. Collapsing them to one handshake that every harness accepts silently downgrades the Kiro session's declared capabilities. | `test_harness_parity.py::test_handshake_is_per_backend` | `acp/runtime.py` (`AcpRuntime.spawn`), `acp/types.py` (`ACP_CLIENT_CAPABILITIES`, `KAS_CLIENT_CAPABILITIES`) |
| H11 | The provider label is a closed mapping and an absent label means Kiro. It indexes resume compatibility, session-map persistence, and session-file cleanup routing, so a harness with no `PROVIDER_LABEL_*` of its own persists as a Kiro session and its transcript is pruned for want of a Kiro session file. | `test_harness_parity.py::test_every_known_backend_has_a_label`, `::test_codex_carries_its_own_provider_label` | `acp/types.py` (`PROVIDER_LABEL_*`), `providers/acp.py` (`provider_label`, `cleanup_session`), `session.py` (`detect_provider_switch`) |
| H12 | Model pre-flight keeps "empty or unknown advertised set means allow", and never compares ids across harness namespaces. Harnesses advertise ids in their own spelling; one shared membership test across two namespaces calls every legitimate model unusable and withholds the model. | `test_harness_parity.py::test_model_preflight_allows_unknown_advertised_set` | `acp/client.py` (`model_is_unusable`, `advertised_model_ids`) |

## Group D: review-only invariants

Deterministically un-pinnable — they are properties of a change, not of a tree,
and the absence of a mechanism is not something a source scan can see. The
`harness-parity` rule in `AUTOSDE.yaml` carries them to every AI review lane.

| Id | Guarantees | Pinned by | Constrains |
|---|---|---|---|
| H13 | Harness support is additive at the `ProviderRegistry` seam: a v1 addition, no `CONTRACT_VERSION` bump. The Kiro construction path gains no conditional, no new required argument, and no new failure mode in service of an adapter. | review-only (`AUTOSDE.yaml` → `harness-parity`) | `platform/interfaces.py` (`ProviderRegistry`), `config/loader.py` (`create_provider_factory`) |
| H14 | A capability the session layer reads off a provider is declared on `LLMProvider` with a safe default. An adapter never forces a `hasattr` / `getattr` probe onto the Kiro path, and never leaves a Kiro-only attribute reachable through the ABC where a missing one reads as `False`. | review-only (`AUTOSDE.yaml` → `harness-parity`) | `providers/base.py` (`LLMProvider`), `providers/acp.py` (`AcpProvider`) |

## The CI half

The added-line gate that enforces Group B on a diff is
[../../ci/harness-parity-gate.md](../../ci/harness-parity-gate.md). The
structural invariants (Groups A and C) are pinned by
`test/test_harness_parity.py` and therefore fail in the ordinary test job, not
in a separate gate. Group D reaches the four AI review lanes through
`AUTOSDE.yaml`'s `harness-parity` rule, which every lane's prompt treats as the
source of truth for what blocks.

## The bundled-adapter surface

A bundled descriptor may name an `adapter` — a `HarnessAdapter` subclass in
`acp/harness_adapters.py`, selected by data but written in reviewed code.
Operator descriptors cannot name one, so this surface is reachable only through
review. Four methods are overridable, and an adapter overriding one does not
thereby take over the others (which is what keeps the number of behaviours a
harness can accidentally change bounded by what it actually overrode):

| Hook | Default | Bundled overrides |
|---|---|---|
| `resolve_executable` | absolute path as-is, bare name through `shutil.which`, then the runnable / non-empty candidate check | `KasAdapter` (Node interpreter **and** the extracted server script, either half reportable on its own) |
| `render_argv` | pure template rendering from the descriptor's `argv` / `agent_args` / `model_args` | `KasAdapter` |
| `pre_spawn` | strip kiro-cli's own API key from the child environment | `KiroAdapter` (`ensure_agent_materialized`, then inject the API key) |
| `post_initialize` | nothing | none yet |

`render_argv` is on that list deliberately, and KAS is the case that makes it
load-bearing: both halves of its command live on disk (the interpreter and the
script extracted from kiro-cli's bundle), which argv data cannot express, so
KAS's argv is **code rather than descriptor data** and its descriptor's
`argv=('{executable}',)` never reaches exec. That is a documented extension of
the adapter surface, not a leak: an override is still bound by the attestation
chain, because `checked_spawn_argv` refuses any `argv[0]` that is not the
attested path (which is exactly the producer a template's `{executable}`
requirement cannot reach). A harness whose invocation IS expressible as data —
kiro-cli, Codex, every operator harness — must not override it, because the
override moves that harness's convention out of the data every other consumer
reads.

## Adding or changing an invariant

1. Write the test first: an invariant is its test, and this table is the index.
   A row whose *Pinned by* cell names nothing is a wish.
2. Cite the id in the source docstring it constrains, and add the row here in
   the same change.
3. Never relax a check to make a red invariant green. A parity failure that
   flips GREEN because the Kiro path was made to match the adapter is the
   regression this file exists to catch, not a fix. If a harness genuinely
   cannot be adapted within these invariants, the correct outcome is that the
   harness does not land yet — say so in the PR instead of widening a seam.
4. A new harness adds rows to `ACP_BACKENDS_KNOWN`, a `PROVIDER_LABEL_*`, and
   an explicit decision for every Group B membership set. "Inherited the
   default" is not a decision. `BASELINE_SELECTABLE_BACKENDS` is otherwise
   `ACP_BACKENDS_KNOWN`, so leaving a known id out of the baseline is a
   NARROWING that `test_baseline_ships_every_known_backend` fails on **unless**
   the id is named in that test's `NOT_SHIPPED_SELECTABLE` allowlist together
   with the reason it cannot be offered yet: the id becomes spellable but
   unreachable, and that state needs a stated reason rather than a default.
   `ACP_BACKEND_CODEX` is the only member today. The full sequence a new
   harness walks, and which stage decides whether it lands dormant or
   selectable, is [harness-onboarding.md](harness-onboarding.md).
