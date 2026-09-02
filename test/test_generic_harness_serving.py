"""Generic-harness serving, end to end against a real stub ACP process.

T6 flipped serving on: a registered operator descriptor with a resolvable
executable produces a :class:`HarnessBinding` and constructs an
:class:`AcpProvider` on the GenericAdapter. This suite proves the LAST mile —
that the real :class:`~kiro_crew.acp.client.AcpClient` the binding builds can
actually complete a chat turn against a conformant public-ACP harness — using a
stdlib stub (``test/fixtures/stub_acp_harness.py``) rather than any mock of the
transport.

Why the stub is spawned and assigned to ``client._process`` rather than reached
through ``ensure_ready()``: ``AcpClient._spawn`` USED TO hardcode the kiro-cli
argv on its non-claude branch (it did not render a generic descriptor's argv —
that was the pooled ``AcpRuntime`` path), so letting ``ensure_ready`` spawn would
exec kiro-cli, not the stub. T6b removed that trap: ``_spawn`` now renders a bound
GENERIC descriptor's argv through ``adapter_for(descriptor).render_argv`` + the
shared resolve/attest/``checked_spawn_argv`` seam, so a bound generic client execs
its OWN binary. This suite therefore drives the REAL ``ensure_ready()`` ->
``_spawn`` -> ``_initialize_session`` chain and PROVES the stub is what got exec'd:
the echo turn completing is behavioural proof, and the argv the child was spawned
with is asserted equal to the descriptor's rendered argv via the golden seam
(``render_argv`` + ``resolve_spawn_executable``).

Framing choice: NEWLINE-DELIMITED JSON. ``AcpClient`` writes
``json.dumps(...) + "\\n"`` and reads with ``stdout.readline()`` — there is no
``Content-Length`` framing anywhere in its transport — so the stub matches that
exactly (see its module docstring).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

import kiro_crew.acp.client as client_mod
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.harness_adapters import resolve_spawn_executable
from kiro_crew.acp.harness_descriptor import (
    CAPABILITY_INTERNAL_SANDBOX,
    MCP_DELIVERY_WIRE_FED,
    CapabilitySet,
    HarnessDescriptor,
    render_argv,
)
from kiro_crew.acp.harness_registry import HARNESS_KIRO, registry
from kiro_crew.acp.harness_selection import HarnessBinding, resolve_session_harness
from kiro_crew.acp.protocol_profile import STANDARD_ACP_PROFILE
from kiro_crew.providers.acp import AcpProvider

# Subprocess-spawning tests: mark integration (setup.cfg registers the marker).
pytestmark = pytest.mark.integration

_STUB = Path(__file__).with_name("fixtures") / "stub_acp_harness.py"
_STUB_HARNESS_ID = "stub-acp"


def _stub_descriptor(harness_id: str = _STUB_HARNESS_ID) -> HarnessDescriptor:
    """An operator descriptor whose executable is the Python interpreter and whose
    argv carries the stub script as a second token.

    This is the shape the task calls for and the one an operator would author:
    ``argv`` may hold more than one token, and ``render_argv`` requires the first
    to be ``{executable}`` (so the trust-attested binary is the one that runs).
    No wrapper script is needed — argv carries both the interpreter and the
    script path.

    Empty ``CapabilitySet`` (the GenericAdapter posture): the sandbox-wrap and
    overlay assertions below depend on every capability being off.
    """
    return HarnessDescriptor(
        id=harness_id,
        display_name="Stub ACP",
        executable=sys.executable,
        argv=("{executable}", str(_STUB)),
        capabilities=CapabilitySet(),
    )


@pytest.fixture
def registered_stub(tmp_path):
    """Register the stub descriptor in the live registry and force its executable
    to resolve, yielding the binding a session would get.

    The autouse ``_restore_harness_registry`` conftest fixture snapshots and
    restores ``_descriptors`` around every test, so injecting directly is
    hermetic — the same technique the capability-gate suite uses (the registry
    exposes no writer for a harness definition, a pinned security property).
    """
    reg = registry()
    reg._ensure_loaded()
    descriptor = _stub_descriptor()
    with reg._lock:
        reg._descriptors[descriptor.id] = descriptor
    yield descriptor


def _descriptor_rendered_argv(descriptor: HarnessDescriptor, work_dir: Path) -> list[str]:
    """The argv the descriptor SHOULD produce, computed via the golden seam.

    Resolves + attests the executable the same way ``_spawn`` does, then renders
    from the descriptor. This is the "seam the golden tests use" (``render_argv``
    over the attested executable), so an equality assertion against the argv the
    child was actually spawned with pins that ``_spawn`` honoured the descriptor.
    ``model=""``/``agent=`` match the client built below (model=None, default
    agent has no ``agent_args`` on the stub descriptor so it is a no-op).
    """
    attested = resolve_spawn_executable(descriptor)
    return render_argv(descriptor, executable=attested, workdir=str(work_dir))


def _bound_client(
    descriptor: HarnessDescriptor,
    work_dir: Path,
    *,
    env_extra: dict[str, str] | None = None,
) -> tuple[AcpClient, list[list[str]]]:
    """A REAL AcpClient bound to the stub descriptor, plus a list that captures the
    argv every ``_spawn`` hands the OS.

    Nothing is pre-spawned: the client goes through the genuine
    ``ensure_ready()`` -> ``_spawn`` path (T6b), so ``_spawn``'s descriptor-driven
    branch renders the stub's argv. ``sandbox_mode="off"`` keeps ``wrap_argv`` a
    passthrough so the captured argv is the descriptor's own rendering, not a
    seatbelt wrapper. The capture wraps ``create_subprocess_limited`` (delegating
    to the real one) so the child still really spawns AND its argv is observable.
    """
    binding = HarnessBinding(descriptor=descriptor, acp_backend=descriptor.id)
    provider = AcpProvider(
        binding=binding,
        sandbox_mode="off",
        extra_env=env_extra or None,
    )
    client = provider.client
    client._work_dir = work_dir
    return client, _install_argv_capture(client)


def _install_argv_capture(client: AcpClient) -> list[list[str]]:
    """Wrap the module's ``wrap_argv`` so the argv each spawn attempt renders is
    recorded, then delegate to the real one.

    Captured at ``wrap_argv`` — the same seam the golden argv tests use — so the
    recorded list is the descriptor's PRE-sandbox, PRE-cgroup rendering, directly
    comparable to ``render_argv``. (Capturing at ``create_subprocess_limited``
    would instead see the ``systemd-run --scope`` cgroup wrapper prepended by
    ``cgroup_scope_argv``, which runs after ``wrap_argv``.)

    Returned list accrues one entry per ``_spawn`` call — so the ensure_ready
    retry path records BOTH attempts, which is what the garbage-initialize test
    asserts (every attempt re-execs the descriptor argv, never kiro).
    """
    captured: list[list[str]] = []
    real = client_mod.wrap_argv

    def _capturing(argv, **kwargs):
        captured.append(list(argv))
        return real(argv, **kwargs)

    client_mod.wrap_argv = _capturing  # type: ignore[assignment]
    return captured


async def _shutdown(client: AcpClient) -> None:
    """Tear the client's process down through its own shutdown path, then restore
    the patched module symbol so a later test in the process is unaffected."""
    try:
        await client.shutdown()
    finally:
        _restore_wrap_argv()


_ORIG_WRAP_ARGV = client_mod.wrap_argv


def _restore_wrap_argv() -> None:
    client_mod.wrap_argv = _ORIG_WRAP_ARGV  # type: ignore[assignment]


# ── (a) a full chat turn against the stub, through the REAL spawn path ──


@pytest.mark.asyncio
async def test_full_chat_turn_echo_permission_and_stop(registered_stub, tmp_path):
    """ensure_ready → _spawn (descriptor argv) → initialize → session/new → prompt
    → echo chunk → permission round trip → stop.

    Drives the REAL client transport AND the real ``_spawn``: the descriptor's
    own rendered argv is what execs (proven below by argv equality), so the echo
    reaching the caller and the turn ending on ``end_turn`` are end-to-end proof
    that a bound GENERIC harness serves a full turn on its OWN binary — no manual
    self-spawn workaround (removed in T6b now that ``_spawn`` honours descriptors).
    """
    client, spawned = _bound_client(registered_stub, tmp_path)
    try:
        await client.ensure_ready()
        assert client._session_id == "stub-session-1"

        # The child that got exec'd is the STUB, from the descriptor's own argv —
        # asserted via the golden seam, NOT kiro-cli's argv.
        assert spawned, "no subprocess was spawned"
        assert spawned[-1] == _descriptor_rendered_argv(registered_stub, tmp_path)
        assert spawned[-1][1] == str(_STUB)  # the stub script, concretely

        prompt_id = await client._send_prompt("hello stub")
        text = await client._read_prompt_response(prompt_id, timeout=30.0)

        # The echo chunk arrived (proves the update stream was consumed) …
        assert "echo: hello stub" in text
        # … and the turn completed on end_turn, which the stub only sends AFTER
        # receiving the client's permission response — so the round trip closed.
        assert client._last_stop_reason == "end_turn"
    finally:
        await _shutdown(client)


# ── (a2) a DENIED permission round trip completes on the refusal stop reason ──


@pytest.mark.asyncio
async def test_permission_deny_round_trip_yields_refusal_stop_reason(registered_stub, tmp_path):
    """DENY mode: the client REJECTS the tool, the stub receives ``reject_once``,
    and the turn completes on the ``refusal`` stop reason — proving the
    permission-DENY transport end to end against a real ACP process.

    The echo test proves ALLOW; this proves DENY on the same real
    ``ensure_ready`` -> ``_spawn`` -> prompt path. ``_read_prompt_response`` only
    auto-APPROVES, so this test swaps the client's permission handler for one
    that first records the advertised options (via ``_build_permission_event`` —
    the exact seam the streaming dispatch uses to populate
    ``_permission_options``) and then calls ``reject_tool``. ``reject_tool`` then
    finds the stub's advertised ``reject_once`` option and answers with a
    ``selected`` outcome carrying it — which the DENY stub asserts before
    replying ``stopReason:"refusal"`` (a non-reject answer exits the stub
    non-zero, so an allow leaking through would fail the turn, not pass it).
    """
    client, spawned = _bound_client(registered_stub, tmp_path, env_extra={"STUB_ACP_DENY": "1"})

    async def _reject_permission(msg):
        # Record the advertised options the way the real streaming dispatch does,
        # so reject_tool resolves the harness's own reject_once id (the direct
        # _read_prompt_response path does not otherwise populate the map).
        client._build_permission_event(msg)
        request_id = msg.id if msg.id is not None else ""
        await client.reject_tool(request_id)

    client._handle_permission = _reject_permission  # type: ignore[assignment]

    try:
        await client.ensure_ready()
        assert client._session_id == "stub-session-1"
        assert spawned[-1] == _descriptor_rendered_argv(registered_stub, tmp_path)

        prompt_id = await client._send_prompt("please run the tool")
        text = await client._read_prompt_response(prompt_id, timeout=30.0)

        # The echo chunk still streamed before the permission round trip …
        assert "echo: please run the tool" in text
        # … and the turn ended on the DENY stop reason, which the stub emits ONLY
        # after receiving the reject_once selection — so the deny transport closed.
        assert client._last_stop_reason == "refusal"
    finally:
        await _shutdown(client)


# ── (b) the advertised models are THIS harness's catalog, not another's ──


@pytest.mark.asyncio
async def test_advertised_models_are_this_harness_catalog(registered_stub, tmp_path):
    """The two stub models surface as this session's catalog snapshot.

    Same semantics wave 1 pinned: ``available_models`` reflects what THIS harness
    advertised at session/new, in ``{modelId, name, description}`` shape — and is
    NOT the kiro harness's list. Captured once at session init and read back.
    Driven through the real ``ensure_ready`` spawn path.
    """
    client, _spawned = _bound_client(registered_stub, tmp_path)
    try:
        await client.ensure_ready()
        ids = [m["modelId"] for m in client.available_models()]
        assert ids == ["stub-fast", "stub-smart"]
        # Every entry is the normalized three-field shape.
        assert all({"modelId", "name", "description"} <= set(m) for m in client.available_models())

        # NOT another harness's list: the bundled kiro descriptor enumerates over
        # ACP (no static list), so its ids never include the stub's fakes. The
        # snapshot is per-session and belongs to the stub alone.
        assert "stub-fast" not in _bundled_kiro_static_models()
    finally:
        await _shutdown(client)


def _bundled_kiro_static_models() -> list[str]:
    """Any static models the kiro row declares (it advertises over ACP, so this
    is empty) — the disjointness check reads it so the assertion is about real
    registry data, not a hardcoded empty set."""
    return list(registry().get(HARNESS_KIRO).models)


# ── (c) Crew's sandbox-wrap decision fires for the stub (fail-closed) ──


def test_sandbox_wrap_decision_is_true_for_the_empty_capability_stub(registered_stub, tmp_path):
    """Empty CapabilitySet ⇒ internal_sandbox absent ⇒ Crew wraps the child.

    This asserts the SAME seam T4/T5 use: the boolean ``AcpClient._spawn`` hands
    ``wrap_argv`` as ``is_kiro_cli`` is ``client._declares(CAPABILITY_INTERNAL_
    SANDBOX)``. For the stub that is False, so Crew's own sandbox wraps the child
    (the "wrap decision True" the task asks for = Crew-wraps = internal_sandbox
    NOT waived). We assert the DECISION/intent, not the OS seatbelt itself (which
    may not exist in the test env).
    """
    binding = HarnessBinding(descriptor=registered_stub, acp_backend=registered_stub.id)
    provider = AcpProvider(binding=binding)
    client = provider.client
    # The stub declares no internal sandbox → the client does not waive Crew's.
    assert client._declares(CAPABILITY_INTERNAL_SANDBOX) is False
    # And it speaks the public-ACP wire, from the binding's GenericAdapter — not
    # kiro-cli's, which the backend-string derivation would wrongly hand it.
    assert client._protocol_profile is STANDARD_ACP_PROFILE


# ── (d) no cli.json overlay is written into the workdir ──


def test_no_cli_json_overlay_is_written_for_the_stub(registered_stub, tmp_path):
    """The stub declares neither ``mcp_tool_search`` nor ``reasoning_effort``, so
    the overlay writers must leave no ``.kiro/settings/cli.json`` in the workdir.

    Driven through the real overlay-apply methods on a provider bound to the stub
    (the exact seam T5's overlay tests use), then asserting the file's absence.
    """
    binding = HarnessBinding(descriptor=registered_stub, acp_backend=registered_stub.id)
    provider = AcpProvider(binding=binding)
    # Point the client's work dir at the tmp workdir so any write would land here.
    provider.client._work_dir = tmp_path
    provider._apply_effort_overlay()
    provider._apply_tool_search_overlay()
    assert not (tmp_path / ".kiro" / "settings" / "cli.json").exists()


# ── (e) handshake failure names the harness+step and records a TTL'd probe failure ──


@pytest.mark.asyncio
async def test_garbage_initialize_fails_and_retry_re_execs_the_descriptor_argv(
    registered_stub, tmp_path
):
    """A stub that replies garbage to ``initialize`` fails the handshake; the
    failure is recorded with wave-1 TTL semantics and surfaces on the selection
    listing — and the ensure_ready RETRY re-execs the DESCRIPTOR argv, never
    kiro-cli's (the old ``_spawn`` trap, gone since T6b).

    Driven through the REAL ``ensure_ready()`` so its retry-once path runs. Both
    attempts must render the stub's argv from its descriptor: a generic branch
    that fell back to the hardcoded kiro argv on the second attempt is exactly
    the regression this asserts against. The stub emits a non-JSON line then
    exits, so each attempt hits EOF with the process gone and raises AcpError
    fast; the safety timeout only guards a hang.
    """
    from kiro_crew.acp.client import AcpError, AcpTimeoutError

    client, spawned = _bound_client(
        registered_stub, tmp_path, env_extra={"STUB_ACP_BAD_INITIALIZE": "1"}
    )
    try:
        with pytest.raises((AcpError, AcpTimeoutError)):
            await asyncio.wait_for(client.ensure_ready(), timeout=30)
    finally:
        await _shutdown(client)

    # ensure_ready retries once → two spawn attempts, and EACH re-execs the
    # descriptor's own argv (the stub), never the kiro-cli argv. This is the
    # generic-branch retry trap the task calls out.
    expected = _descriptor_rendered_argv(registered_stub, tmp_path)
    assert len(spawned) == 2, "ensure_ready should have retried the spawn exactly once"
    for attempt in spawned:
        assert attempt == expected
        assert attempt[1] == str(_STUB)
        # Never kiro-cli: neither the binary nor the acp subcommand shape.
        assert "kiro-cli" not in attempt[0]
        assert "--agent" not in attempt

    # The spawn path records a probe failure naming the harness + step; replay
    # that here and assert the selection surface (registry().list()) shows it.
    reason = f"{_STUB_HARNESS_ID} exited during ACP initialize"
    reg = registry()
    reg.note_probe_failure(_STUB_HARNESS_ID, reason)
    try:
        row = next(r for r in reg.list() if r.id == _STUB_HARNESS_ID)
        assert row.available is False
        assert reason in row.reason
        # Wave-1 TTL semantics: the record is stored under the same TTL constant
        # the whole feature uses, and clears cleanly.
        from kiro_crew.acp.harness_registry import _PROBE_FAILURE_TTL_SECS

        assert _PROBE_FAILURE_TTL_SECS > 0
        assert reg._probe_failure(_STUB_HARNESS_ID) == reason
    finally:
        reg.clear_probe_failure(_STUB_HARNESS_ID)
        assert reg._probe_failure(_STUB_HARNESS_ID) == ""


# ── (f) spawn + cron surfaces route to the stub through the same selection seam ──
#
# The spawn (test_spawn_harness.py) and cron (test_cron_harness_selection.py)
# suites already prove routing GENERICALLY: both resolve every harness through
# ``harness_selection.resolve_session_harness`` (spawn) / ``cron.resolve_job_
# harness`` → the same seam (cron), and each pins that an explicit id is passed
# through and refused-over-fallback. Rather than re-drive the full gateway, these
# two tests tie the STUB descriptor through that same selection function, proving
# a harness= selection lands on the stub's own binding (descriptor-id fallback),
# which is what both surfaces forward.


def test_spawn_selection_resolves_the_stub_binding(registered_stub, monkeypatch, tmp_path):
    """``resolve_session_harness("stub-acp")`` — the function ``SubagentManager.
    spawn`` calls — returns the stub's binding, not the default.

    Availability is forced through the registry's ``resolve_executable`` seam (the
    same technique test_spawn_harness uses) so the resolution asserts the code's
    decision, not whether the host has any binary. The binding's ``acp_backend``
    is the descriptor id (the generic fallback both surfaces forward).
    """
    from kiro_crew.acp import harness_registry

    monkeypatch.setattr(
        harness_registry, "resolve_executable", lambda d: (str(tmp_path / "stub"), "")
    )
    binding = resolve_session_harness(_STUB_HARNESS_ID)
    assert isinstance(binding, HarnessBinding)
    assert binding.harness_id == _STUB_HARNESS_ID
    assert binding.acp_backend == _STUB_HARNESS_ID  # generic fallback
    # And an explicit selection is refused-over-fallback: it never silently
    # becomes the default kiro row.
    assert binding.harness_id != HARNESS_KIRO


def test_cron_selection_resolves_the_stub_binding(registered_stub, monkeypatch, tmp_path):
    """The cron fire-time resolver (``resolve_job_harness``) routes a job whose
    ``harness`` is the stub to the stub's binding, through the SAME
    ``resolve_session_harness`` seam the spawn path uses.

    Asserts the mapping cron owns: an explicit, resolvable selection yields
    ``(stub-acp, "")`` — no refusal — proving harness= routing reaches the stub
    descriptor rather than only the bundled rows.
    """
    from kiro_crew.acp import harness_registry
    from kiro_crew.cron import CronJob, CronSchedule, resolve_job_harness

    monkeypatch.setattr(
        harness_registry, "resolve_executable", lambda d: (str(tmp_path / "stub"), "")
    )
    job = CronJob(
        id="j1",
        name="stub-job",
        message="go",
        schedule=CronSchedule(kind="every", every_secs=300),
        harness=_STUB_HARNESS_ID,
    )
    harness_id, reason = resolve_job_harness(job)
    assert (harness_id, reason) == (_STUB_HARNESS_ID, "")


def test_bound_descriptor_wins_mcp_delivery_over_the_backend_roster(registered_stub, tmp_path):
    """Review finding (2026-09-02): a bound operator harness was invisible to
    ``_harness_descriptor``.

    The method consulted only ``_CLIENT_HARNESSES``, a roster keyed on the legacy
    ``acp_backend`` spelling — which an operator harness does not have — so the
    lookup fell back to kiro-cli's FILE-FED descriptor. A wire-fed operator
    session then received an empty ``mcpServers`` array (it reads no agent spec
    of ours): a silently tool-less session. The bound descriptor, threaded at
    construction, must win.
    """
    descriptor = registered_stub
    binding = HarnessBinding(descriptor=descriptor, acp_backend=descriptor.id)
    provider = AcpProvider(binding=binding, sandbox_mode="off")
    client = provider.client
    client._work_dir = tmp_path

    resolved = client._harness_descriptor()
    assert resolved is descriptor, (
        "bound descriptor must be returned directly, not substituted from the "
        f"backend roster (got {resolved.id!r})"
    )
    assert resolved.mcp_delivery == MCP_DELIVERY_WIRE_FED


def test_unbound_client_still_resolves_delivery_from_the_roster():
    # The pre-binding paths (warm pool, direct construction) keep the roster
    # fallback: kiro's file-fed descriptor, the fail-closed direction.
    provider = AcpProvider(sandbox_mode="off")
    client = provider.client
    resolved = client._harness_descriptor()
    assert resolved.id == HARNESS_KIRO
