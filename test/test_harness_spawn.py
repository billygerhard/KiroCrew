"""Descriptor-driven spawn: the golden argv, uniform attestation, and refusals.

``AcpRuntime.spawn`` no longer builds a per-harness argv inline. This module pins
the four things that migration must not have changed or must now guarantee:

1. **Byte-identity for kiro-cli.** The rendered argv equals the construction that
   shipped before the migration, for the same inputs. Written as the pre-migration
   FORMULA rather than as a literal list, so a change to the descriptor's
   conventions fails here instead of being copied into the expectation.
2. **The attested executable is the one that execs.** Resolution, attestation, and
   ``argv[0]`` are one chain; a descriptor or adapter that breaks it is refused at
   spawn rather than exec'ing bytes nobody checked.
3. **The sandbox waiver follows the descriptor's opt-in flag.** Flipping
   ``internal_sandbox`` flips what ``wrap_argv`` is told, and nothing else does.
4. **A harness that dies during initialize is named.** The error carries the
   harness id and the process's exit context, the failure is recorded against that
   harness alone, and no other harness is tried.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import replace
from pathlib import Path

import pytest

import kiro_crew.acp.harness_adapters as adapters_mod
import kiro_crew.acp.runtime as runtime_mod
from kiro_crew.acp.harness_adapters import (
    HarnessExecutableTrustError,
    HarnessSpawnRefused,
    adapter_for,
    checked_spawn_argv,
)
from kiro_crew.acp.harness_descriptor import (
    ADAPTER_GENERIC,
    HarnessDescriptor,
    render_argv,
    validate_descriptor,
)
from kiro_crew.acp.harness_registry import (
    HARNESS_KAS,
    HARNESS_KIRO,
    HarnessRegistry,
)
from kiro_crew.acp.harness_registry import registry as harness_registry
from kiro_crew.acp.runtime import KIRO_CLI_SUBCMD, AcpRuntime, AcpRuntimeError
from kiro_crew.acp.types import ACP_BACKEND_KAS


class _StopSpawn(Exception):
    """Aborts ``spawn`` at ``wrap_argv``, so no child is ever executed."""


def _kiro() -> HarnessDescriptor:
    return harness_registry().get(HARNESS_KIRO)


# ── 1. The golden argv ──


def _legacy_kiro_argv(kiro_bin: str, agent: str, model: str) -> list[str]:
    """kiro-cli's argv exactly as ``_resolve_spawn_argv`` built it pre-migration.

    Transcribed from the construction that shipped: base plus the ``--agent``
    flag, and ``--model`` appended ONLY when a model is pinned. Kept as code
    rather than as expected literals so each case below reads as "the same
    formula, same inputs".

    The formula emits ``--agent`` unconditionally, including for an empty agent.
    ``render_argv`` does not — see
    :func:`test_an_empty_agent_is_the_one_documented_divergence_from_the_legacy_formula`
    for the one input where the two deliberately disagree, which is why no case
    in the byte-identity parametrization below passes an empty agent.
    """
    argv = [kiro_bin, KIRO_CLI_SUBCMD, "--agent", agent]
    if model:
        argv += ["--model", model]
    return argv


@pytest.mark.parametrize(
    "agent,model",
    [
        ("kirocrew", ""),
        ("kirocrew", "auto"),
        ("my-app-agent", "claude-sonnet-4-5"),
        # A model id with characters a shell would treat as syntax: the renderer
        # never builds a command string, so these must survive as one argv element.
        ("agent with space", "model'\"$(id)"),
        # An agent name that itself looks like a placeholder must reach exec as
        # those literal bytes, not as the working directory.
        ("{workdir}", ""),
    ],
)
def test_kiro_rendered_argv_is_byte_identical_to_the_legacy_construction(agent, model):
    kiro_bin = "/opt/kiro/Kiro CLI.app/Contents/MacOS/kiro-cli"
    rendered = render_argv(
        _kiro(),
        executable=kiro_bin,
        agent=agent,
        model=model,
        workdir="/tmp/session-workdir",
    )
    assert rendered == _legacy_kiro_argv(kiro_bin, agent, model)


def test_an_empty_model_omits_the_flag_rather_than_passing_an_empty_argument():
    """The convention block is dropped whole, not rendered with an empty value.

    ``["--model", ""]`` is the failure this guards: kiro-cli would take the empty
    string as the model id and fail at the first turn, far from the cause.
    """
    rendered = render_argv(_kiro(), executable="/usr/bin/kiro-cli", agent="kirocrew", model="")
    assert "--model" not in rendered
    assert "" not in rendered


def test_an_empty_agent_is_the_one_documented_divergence_from_the_legacy_formula():
    """The byte-identity claim above is not total, and this is the exception (H9).

    The pre-migration construction appended ``--agent`` unconditionally, so an
    empty agent produced ``["--agent", ""]``. ``render_argv`` drops the whole
    convention block instead, for the reason the empty-model case documents: a
    flag carrying an empty value is accepted by the CLI and then fails at the
    first turn, far from its cause.

    Unreachable in service — every spawn path resolves an agent before rendering —
    but pinned rather than left implicit: an unrecorded divergence makes the
    parametrized golden test read as a total byte-identity claim that is false,
    and the next person to widen that parametrization would learn it from a
    confusing red instead of from here.
    """
    kiro_bin = "/usr/bin/kiro-cli"
    rendered = render_argv(_kiro(), executable=kiro_bin, agent="", model="")

    assert rendered == [kiro_bin, KIRO_CLI_SUBCMD]
    assert "--agent" not in rendered
    # The divergence is exactly this, and only this.
    assert _legacy_kiro_argv(kiro_bin, "", "") == [kiro_bin, KIRO_CLI_SUBCMD, "--agent", ""]


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["", "auto"])
async def test_spawn_hands_wrap_argv_the_legacy_kiro_argv(tmp_path, monkeypatch, model):
    """The golden pin at the spawn level, not just at the renderer.

    Rendering can be correct while the spawn path passes the wrong inputs (a
    dropped model, the crew agent instead of the kiro one), and nothing else in
    the suite would notice.
    """
    kiro_bin = "/usr/bin/kiro-cli"
    captured: dict[str, object] = {}

    def fake_wrap(argv, **kwargs):
        captured["argv"] = list(argv)
        captured["kwargs"] = kwargs
        raise _StopSpawn

    monkeypatch.setattr(runtime_mod, "resolve_spawn_executable", lambda descriptor: kiro_bin)
    monkeypatch.setattr(runtime_mod, "wrap_argv", fake_wrap)

    runtime = AcpRuntime(work_dir=tmp_path / "ws", sandbox_mode="off", model=model or None)
    with pytest.raises(_StopSpawn):
        await runtime.spawn()

    assert captured["argv"] == _legacy_kiro_argv(kiro_bin, runtime._agent, model)


@pytest.mark.asyncio
async def test_spawn_renders_the_kas_command_from_its_adapter(tmp_path, monkeypatch):
    """KAS's argv comes from its adapter, anchored on the attested kiro-cli path.

    KAS is reached through kiro-cli's own ACP relay, so the engine and auth-owner
    flags are what no argv template can express — the adapter builds them, and the
    attested executable still has to be ``argv[0]``.
    """
    from kiro_crew.acp.kas_transport import (
        KAS_RELAY_AUTH_OWNER,
        KAS_RELAY_ENGINE,
        KAS_RELAY_SUBCMD,
    )

    kiro_bin = tmp_path / "kiro-cli"
    kiro_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    kiro_bin.chmod(0o755)
    monkeypatch.setattr(
        "kiro_crew.kiro_cli.resolve_kiro_cli", lambda *a, **k: str(kiro_bin), raising=True
    )

    captured: dict[str, object] = {}

    def fake_wrap(argv, **kwargs):
        captured["argv"] = list(argv)
        captured["kwargs"] = kwargs
        raise _StopSpawn

    monkeypatch.setattr(runtime_mod, "wrap_argv", fake_wrap)
    runtime = AcpRuntime(work_dir=tmp_path / "ws", sandbox_mode="off", acp_backend=ACP_BACKEND_KAS)
    with pytest.raises(_StopSpawn):
        await runtime.spawn()

    argv = captured["argv"]
    assert isinstance(argv, list)
    assert argv[0] == str(kiro_bin)
    assert KAS_RELAY_SUBCMD in argv
    assert KAS_RELAY_ENGINE in argv
    assert KAS_RELAY_AUTH_OWNER in argv
    # KAS takes its agent over the wire (session/new's _meta.kiro.customAgents)
    # and its model per session, so its descriptor declares neither convention
    # block and neither may appear however the runtime was configured.
    assert "--agent" not in argv and "--model" not in argv


# ── 2. The attested executable is the one that execs ──


def test_an_argv_template_that_drops_the_executable_placeholder_is_invalid():
    """Validation is the first half of the guarantee.

    Without it a descriptor could name one executable (resolved and attested) and
    exec a different, PATH-resolved one.
    """
    descriptor = HarnessDescriptor(
        id="mine", executable="/opt/agy/agy-acp", argv=("agy-acp", "acp")
    )
    reasons = validate_descriptor(descriptor)
    assert any("must start with {executable}" in r for r in reasons)


def test_a_template_starting_with_the_placeholder_validates():
    """The control for the refusal above."""
    descriptor = HarnessDescriptor(
        id="mine", executable="/opt/agy/agy-acp", argv=("{executable}", "acp")
    )
    assert validate_descriptor(descriptor) == []


def test_checked_spawn_argv_refuses_an_argv0_that_was_never_attested():
    """The second half: an adapter's own ``render_argv`` bypasses the template.

    A bundled adapter builds argv in code, so validation cannot see it. Without
    this check such an adapter could exec any path it liked while attestation
    reported on another.
    """
    descriptor = _kiro()
    with pytest.raises(HarnessSpawnRefused) as exc:
        checked_spawn_argv(descriptor, ["/tmp/evil", "acp"], "/usr/bin/kiro-cli")
    assert "was attested" in str(exc.value)
    assert descriptor.id in str(exc.value)


def test_checked_spawn_argv_refuses_an_empty_argv():
    with pytest.raises(HarnessSpawnRefused, match="empty argv"):
        checked_spawn_argv(_kiro(), [], "/usr/bin/kiro-cli")


def test_checked_spawn_argv_passes_the_attested_path_through():
    argv = ["/usr/bin/kiro-cli", "acp"]
    assert checked_spawn_argv(_kiro(), argv, "/usr/bin/kiro-cli") is argv


@pytest.mark.asyncio
async def test_spawn_refuses_when_an_adapter_execs_something_else(tmp_path, monkeypatch):
    """Mutation probe for the guard above, driven through the real spawn path."""

    def _rogue_argv(self, descriptor, *, executable, agent="", model="", workdir=""):
        return ["/tmp/not-the-attested-binary", "acp"]

    monkeypatch.setattr(
        runtime_mod, "resolve_spawn_executable", lambda descriptor: "/usr/bin/kiro-cli"
    )
    monkeypatch.setattr(adapters_mod.KiroAdapter, "render_argv", _rogue_argv)

    runtime = AcpRuntime(work_dir=tmp_path / "ws", sandbox_mode="off")
    with pytest.raises(AcpRuntimeError) as exc:
        await runtime.spawn()
    assert "was attested" in str(exc.value)


def test_attestation_is_applied_to_an_operator_harness(tmp_path):
    """R2.2: an operator's executable takes the same gate kiro-cli takes.

    A zero-byte file keeps its execute bit after an interrupted download, so
    without the shared gate it would spawn and die with no ACP frame.
    """
    truncated = tmp_path / "agy-acp"
    truncated.write_text("", encoding="utf-8")
    truncated.chmod(0o755)
    descriptor = HarnessDescriptor(id="mine", executable=str(truncated), argv=("{executable}",))
    with pytest.raises(HarnessSpawnRefused) as exc:
        adapters_mod.resolve_spawn_executable(descriptor)
    assert "zero-byte" in str(exc.value)


def test_attestation_reports_a_trust_refusal_under_the_harness_neutral_lineage(
    tmp_path, monkeypatch
):
    """The trust error names the harness and is not kiro-specific.

    ``snapshot_trusted_acp_executable`` raises ``ValueError`` for a candidate it
    will not launch; that has to arrive as a refusal a caller can catch for ANY
    harness, which is what the shared lineage is for.
    """
    monkeypatch.setattr(
        "kiro_crew.kiro_prerequisite.snapshot_trusted_acp_executable",
        lambda *a, **kw: (_ for _ in ()).throw(ValueError("not a runnable executable")),
    )
    with pytest.raises(HarnessExecutableTrustError) as exc:
        adapters_mod.attest_executable("mine", str(tmp_path / "anything"))
    assert "mine" in str(exc.value)
    assert isinstance(exc.value, HarnessSpawnRefused)


def test_a_trust_refusal_does_not_tell_the_operator_to_reinstall_kiro_cli(tmp_path, monkeypatch):
    """The operator-visible text is neutral too, not just the exception class.

    The snapshot's real message names Kiro CLI, because its other caller is the
    kiro-only client path. Passed through verbatim, an operator harness refuses
    with "harness 'mine': Kiro CLI is not a runnable executable" — which sends
    the operator to reinstall a tool that is not the one that failed.
    """
    candidate = tmp_path / "mine-acp"
    candidate.write_bytes(b"")
    candidate.chmod(0o755)
    monkeypatch.setattr(
        "kiro_crew.kiro_prerequisite.snapshot_trusted_acp_executable",
        lambda *a, **kw: (_ for _ in ()).throw(
            ValueError("Kiro CLI is not a runnable executable for ACP execution")
        ),
    )
    with pytest.raises(HarnessExecutableTrustError) as exc:
        adapters_mod.attest_executable("mine", str(candidate))

    message = str(exc.value)
    assert "Kiro CLI" not in message
    assert "mine" in message
    # The shared candidate verdict names the actual defect where it is still
    # observable, so the message is more specific than the one it replaced.
    assert "zero-byte" in message
    # The original text is not lost: it stays on the chained cause for a traceback.
    assert "Kiro CLI" in str(exc.value.__cause__)


def test_a_trust_refusal_falls_back_to_a_neutral_verdict_when_the_file_looks_fine(
    tmp_path, monkeypatch
):
    """A candidate that passes the local verdict still refuses, neutrally.

    Attestation and the candidate verdict can disagree (a file replaced between
    the two reads, or a platform check only attestation makes), so the fallback
    has to be a complete sentence rather than an empty reason.
    """
    candidate = tmp_path / "mine-acp"
    candidate.write_bytes(b"#!/bin/sh\n")
    candidate.chmod(0o755)
    monkeypatch.setattr(
        "kiro_crew.kiro_prerequisite.snapshot_trusted_acp_executable",
        lambda *a, **kw: (_ for _ in ()).throw(
            ValueError("Kiro CLI is not a runnable executable for ACP execution")
        ),
    )
    with pytest.raises(HarnessExecutableTrustError) as exc:
        adapters_mod.attest_executable("mine", str(candidate))

    message = str(exc.value)
    assert "Kiro CLI" not in message
    assert "is not a runnable executable for ACP execution" in message
    assert str(candidate) in message


def test_a_resolved_executable_becomes_argv0_even_for_a_bare_path_name(tmp_path):
    """A PATH name is resolved to a concrete file before it reaches argv.

    Leaving the bare name in ``argv[0]`` would let exec re-resolve it through
    PATH after attestation, which is the TOCTOU the chain exists to close.
    """
    # PATHEXT-aware plant: Windows which() only matches a suffixed name, and
    # the descriptor still declares the bare ``agy-acp`` (see the augmented-path
    # test in test_harness_generic_adapter.py for the same shape).
    tool = tmp_path / ("agy-acp.bat" if os.name == "nt" else "agy-acp")
    tool.write_text("#!/bin/sh\n", encoding="utf-8")
    tool.chmod(0o755)
    descriptor = HarnessDescriptor(id="mine", executable="agy-acp", argv=("{executable}", "acp"))
    with_path = str(tmp_path)

    old = os.environ.get("PATH", "")
    try:
        os.environ["PATH"] = with_path + os.pathsep + old
        attested = adapters_mod.resolve_spawn_executable(descriptor)
    finally:
        os.environ["PATH"] = old
    argv = adapter_for(descriptor).render_argv(descriptor, executable=attested)
    assert argv == checked_spawn_argv(descriptor, argv, attested)
    assert argv[0] == attested
    assert argv[0] != "agy-acp"


# ── 3. The sandbox waiver follows the descriptor ──


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "harness_id,backend,expected",
    [(HARNESS_KIRO, "", True), (HARNESS_KAS, ACP_BACKEND_KAS, False)],
)
async def test_sandbox_delegation_follows_the_internal_sandbox_capability(
    tmp_path, monkeypatch, harness_id, backend, expected
):
    captured: dict[str, object] = {}

    def fake_wrap(argv, **kwargs):
        captured.update(kwargs)
        raise _StopSpawn

    monkeypatch.setattr(
        runtime_mod, "resolve_spawn_executable", lambda descriptor: "/usr/bin/harness"
    )
    monkeypatch.setattr(
        adapters_mod.KasAdapter,
        "render_argv",
        lambda self, descriptor, *, executable, **kw: [executable],
    )
    monkeypatch.setattr(runtime_mod, "wrap_argv", fake_wrap)

    runtime = AcpRuntime(work_dir=tmp_path / "ws", sandbox_mode="off", acp_backend=backend)
    with pytest.raises(_StopSpawn):
        await runtime.spawn()
    assert captured["is_kiro_cli"] is expected


@pytest.mark.asyncio
async def test_withdrawing_the_capability_withdraws_the_sandbox_waiver(tmp_path, monkeypatch):
    """Mutation probe: the flag is read from the descriptor, not from an id.

    Flipping ``internal_sandbox`` off for kiro must stop the waiver. A call site
    that still compared harness identifiers would pass ``True`` regardless and
    this test is the only thing that would notice.
    """
    kiro = _kiro()
    unwaived = replace(kiro, capabilities=replace(kiro.capabilities, internal_sandbox=False))
    captured: dict[str, object] = {}

    def fake_wrap(argv, **kwargs):
        captured.update(kwargs)
        raise _StopSpawn

    monkeypatch.setattr(
        runtime_mod, "resolve_spawn_executable", lambda descriptor: "/usr/bin/kiro-cli"
    )
    monkeypatch.setattr(runtime_mod, "wrap_argv", fake_wrap)

    runtime = AcpRuntime(work_dir=tmp_path / "ws", sandbox_mode="off")
    runtime._harness_descriptor = unwaived
    with pytest.raises(_StopSpawn):
        await runtime.spawn()
    assert captured["is_kiro_cli"] is False


# ── 4. A harness that dies during initialize is named ──


_DYING_AGENT = """
import sys
sys.stderr.write("You are not logged in\\n")
sys.exit(3)
"""


@pytest.mark.asyncio
async def test_a_harness_that_exits_during_initialize_is_named_with_its_exit_context(
    tmp_path, monkeypatch
):
    """R6.3: the operator has to learn WHICH harness needs attention.

    An unauthenticated harness exits instead of answering ``initialize``, so the
    bare error is a returncode with no subject — useless on a fleet with more than
    one harness configured.
    """
    dying = tmp_path / "dying_agent.py"
    dying.write_text(_DYING_AGENT, encoding="utf-8")

    monkeypatch.setattr(
        runtime_mod, "resolve_spawn_executable", lambda descriptor: __import__("sys").executable
    )
    monkeypatch.setattr(
        adapters_mod.KiroAdapter,
        "render_argv",
        lambda self, descriptor, *, executable, **kw: [executable, str(dying)],
    )
    monkeypatch.setattr(adapters_mod.KiroAdapter, "pre_spawn", lambda *a, **kw: None)

    runtime = AcpRuntime(work_dir=tmp_path / "ws", sandbox_mode="off")
    with pytest.raises(AcpRuntimeError) as exc:
        await runtime.spawn()

    message = str(exc.value)
    assert f"harness {HARNESS_KIRO!r}" in message
    assert "ACP initialize" in message
    # Exit context: the returncode plus the stderr tail, which is the only place
    # an authentication failure appears at all.
    assert "returncode=" in message
    assert "not logged in" in message
    # The stderr inspection callers rely on to turn this into a login prompt still
    # sees the same lines.
    assert runtime.saw_not_logged_in() is True


def _wire_failing_initialize(monkeypatch, failure: BaseException) -> tuple[list, list]:
    """Drive ``spawn`` to the ``initialize`` handshake, which then raises ``failure``.

    Returns ``(recorded, resolved)``: the ``note_probe_failure`` calls the runtime
    made, and the harness ids it resolved an executable for. No child process is
    ever created — ``create_subprocess_limited`` is a stub — so the only thing
    under test is what the failure handler does.
    """
    recorded: list[tuple[str, str]] = []
    resolved: list[str] = []

    class _Registry(HarnessRegistry):
        def note_probe_failure(self, harness_id: str, reason: str) -> None:
            recorded.append((harness_id, reason))

    substitute = _Registry()
    monkeypatch.setattr(runtime_mod, "harness_registry", lambda: substitute)

    def _resolve(descriptor):
        resolved.append(descriptor.id)
        return "/usr/bin/kiro-cli"

    async def _explode(*_a, **_kw):
        raise failure

    monkeypatch.setattr(runtime_mod, "resolve_spawn_executable", _resolve)
    monkeypatch.setattr(adapters_mod.KiroAdapter, "pre_spawn", lambda *a, **kw: None)
    monkeypatch.setattr(runtime_mod, "wrap_argv", lambda argv, mode, **kw: (list(argv), None))
    monkeypatch.setattr(runtime_mod, "cgroup_scope_argv", lambda argv: argv)
    monkeypatch.setattr(AcpRuntime, "_send_and_await", _explode)

    async def _fake_subprocess(*_a, **_kw):
        class _Proc:
            pid = 4242
            returncode = None
            stdin = None
            stdout = None
            stderr = None

            async def wait(self):
                return 3

        return _Proc()

    monkeypatch.setattr(runtime_mod, "create_subprocess_limited", _fake_subprocess)
    monkeypatch.setattr(runtime_mod, "finish_suspended_spawn", lambda *a, **kw: None)
    return recorded, resolved


@pytest.mark.asyncio
async def test_an_initialize_failure_is_recorded_against_only_that_harness(tmp_path, monkeypatch):
    """R6.4 / R6.5: the record is per harness, and it is a record, not a retry."""
    recorded, resolved = _wire_failing_initialize(
        monkeypatch, runtime_mod.AcpRuntimeDead("process exited (rc=3)")
    )

    runtime = AcpRuntime(work_dir=tmp_path / "ws", sandbox_mode="off")
    with pytest.raises(runtime_mod.AcpRuntimeDead) as exc:
        await runtime.spawn()

    # The original text survives inside the enriched message, so a caller matching
    # on either keeps working, and the class is unchanged so type-based handling is.
    assert "process exited (rc=3)" in str(exc.value)
    assert [harness_id for harness_id, _ in recorded] == [HARNESS_KIRO]
    # No retry on a different harness: exactly one harness was ever resolved.
    assert resolved == [HARNESS_KIRO]


@pytest.mark.asyncio
async def test_a_non_runtime_initialize_failure_still_logs_the_harness_and_exit_context(
    tmp_path, monkeypatch, caplog
):
    """The reason reaches the log even when the re-raise cannot carry it.

    Only an ``AcpRuntimeError`` is re-raised with the enriched message; an
    ``AcpError``, ``OSError``, or ``TimeoutError`` propagates as itself, so
    without the log the harness id and the exit context are computed and then
    dropped — which is the whole diagnosis for an unauthenticated harness.
    """
    _wire_failing_initialize(monkeypatch, OSError("broken pipe"))

    runtime = AcpRuntime(work_dir=tmp_path / "ws", sandbox_mode="off")
    with caplog.at_level(logging.WARNING, logger="kiro_crew.acp.runtime"):
        with pytest.raises(OSError):
            await runtime.spawn()

    assert f"harness {HARNESS_KIRO!r}" in caplog.text
    assert "returncode=" in caplog.text


@pytest.mark.asyncio
async def test_the_stderr_tail_is_redacted_before_it_reaches_a_log_or_a_record(
    tmp_path, monkeypatch, caplog
):
    """A harness's own stderr is untrusted text that lands in three durable sinks.

    The tail is the whole diagnosis for a failed handshake, and it is also where a
    harness echoes back what it could not authenticate with. It reaches the
    exception message, a WARNING in the gateway log, and the availability reason a
    listing renders, so the redaction has to happen where all three read from.
    """
    planted = "AKIAIOSFODNN7EXAMPLE"
    recorded, _resolved = _wire_failing_initialize(
        monkeypatch, runtime_mod.AcpRuntimeDead("process exited (rc=3)")
    )

    async def _plant_then_explode(self, *_a, **_kw):
        self._stderr_lines.append(f"not logged in: rejected key {planted}")
        raise runtime_mod.AcpRuntimeDead("process exited (rc=3)")

    monkeypatch.setattr(AcpRuntime, "_send_and_await", _plant_then_explode)

    runtime = AcpRuntime(work_dir=tmp_path / "ws", sandbox_mode="off")
    with caplog.at_level(logging.WARNING, logger="kiro_crew.acp.runtime"):
        with pytest.raises(runtime_mod.AcpRuntimeDead) as exc:
            await runtime.spawn()

    stored = [reason for _harness_id, reason in recorded]
    assert stored, "the failure was not recorded against the harness"
    for sink in (str(exc.value), caplog.text, *stored):
        assert planted not in sink
    # Redacted, not discarded: the line that makes the failure legible survives.
    assert all("not logged in" in reason for reason in stored)
    assert "not logged in" in str(exc.value)


@pytest.mark.asyncio
async def test_a_cancelled_handshake_is_not_recorded_against_the_harness(
    tmp_path, monkeypatch, caplog
):
    """A cancellation says nothing about the harness, so it costs it no availability.

    A client disconnect, a gateway shutdown, and a pool teardown all cancel this
    task mid-handshake. Recording that would mark a demonstrably healthy harness
    unavailable for the whole failure TTL, so a listing calls it unavailable and
    ``HarnessRegistry.default()`` degrades a configured default away from it —
    refusal-over-fallback inverted, by a path the operator never touched.
    """
    recorded, _resolved = _wire_failing_initialize(monkeypatch, asyncio.CancelledError())

    runtime = AcpRuntime(work_dir=tmp_path / "ws", sandbox_mode="off")
    with caplog.at_level(logging.WARNING, logger="kiro_crew.acp.runtime"):
        with pytest.raises(asyncio.CancelledError):
            await runtime.spawn()

    assert recorded == []
    # Not a harness fault, so not a WARNING either: an operator reading the log
    # must not be sent to reinstall a tool that never misbehaved.
    assert f"harness {HARNESS_KIRO!r} failed during ACP initialize" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [KeyboardInterrupt(), SystemExit(1)])
async def test_interpreter_teardown_is_not_recorded_against_the_harness(
    tmp_path, monkeypatch, failure
):
    """The other non-``Exception`` BaseExceptions take the cancellation path too."""
    recorded, _resolved = _wire_failing_initialize(monkeypatch, failure)

    runtime = AcpRuntime(work_dir=tmp_path / "ws", sandbox_mode="off")
    with pytest.raises(type(failure)):
        await runtime.spawn()

    assert recorded == []


# ── The runtime's harness binding ──


def test_the_runtime_resolves_its_harness_through_the_legacy_alias(tmp_path):
    """An ``acp_backend`` value keeps selecting the harness it names today."""
    assert AcpRuntime(work_dir=tmp_path)._harness().id == HARNESS_KIRO
    assert AcpRuntime(work_dir=tmp_path, acp_backend=ACP_BACKEND_KAS)._harness().id == HARNESS_KAS


def test_the_harness_binding_is_resolved_once_and_cached(tmp_path, monkeypatch):
    """A persisted default change must never retarget a runtime mid-life.

    Two spawn attempts on one runtime would otherwise be able to disagree about
    which harness the process is.
    """
    runtime = AcpRuntime(work_dir=tmp_path)
    first = runtime._harness()

    def _boom():
        raise AssertionError("the registry was consulted a second time")

    monkeypatch.setattr(runtime_mod, "harness_registry", _boom)
    assert runtime._harness() is first


def test_the_kiro_adapter_survives_a_materialization_failure(tmp_path, monkeypatch):
    """Materialization is a self-heal, not a spawn prerequisite.

    A read-only agent home must degrade to "the mode may be missing later", not
    to "this session cannot start".
    """

    def _explode(_agent):
        raise OSError("read-only file system")

    monkeypatch.setattr("kiro_crew.agent.ensure_agent_materialized", _explode)
    monkeypatch.setattr("kiro_crew.config.loader.inject_kiro_cli_api_key", lambda env: env)
    env: dict[str, str] = {}
    adapters_mod.KiroAdapter().pre_spawn(_kiro(), env=env, workdir=str(tmp_path), agent="kirocrew")


def test_a_foreign_harness_never_receives_kiro_clis_api_key(tmp_path):
    """The default pre-spawn hook strips it; only the kiro adapter injects it."""
    from kiro_crew.config.loader import CRED_KIRO_API_KEY

    descriptor = harness_registry().get(HARNESS_KAS)
    env = {CRED_KIRO_API_KEY: "secret"}
    adapter_for(descriptor).pre_spawn(descriptor, env=env, workdir=str(tmp_path), agent="kirocrew")
    assert CRED_KIRO_API_KEY not in env


def test_render_argv_is_shell_free_for_every_bundled_harness():
    """No bundled descriptor's argv is ever built by joining a command string.

    A single rendered element containing a space is proof the value was passed
    through as data: a shell-joined command would have split it.
    """
    for descriptor in harness_registry().list():
        rendered = render_argv(
            harness_registry().get(descriptor.id),
            executable="/path/with a space/harness",
            agent="agent with space",
            model="model with space",
            workdir="/work dir",
        )
        assert isinstance(rendered, list)
        assert rendered[0] == "/path/with a space/harness"
        assert all(isinstance(token, str) for token in rendered)


def test_spawn_is_offloaded_and_never_resolves_on_the_event_loop(tmp_path, monkeypatch):
    """Resolution, attestation, and the pre-spawn hook all block.

    Running any of them on the loop stalls every other multiplexed session, and
    the cost is invisible until a slow filesystem makes it a hang.
    """
    import threading

    loop_thread = threading.current_thread()
    seen: list[threading.Thread] = []

    def _resolve(descriptor):
        seen.append(threading.current_thread())
        return "/usr/bin/kiro-cli"

    def _pre_spawn(*_a, **_kw):
        seen.append(threading.current_thread())

    def fake_wrap(argv, **kwargs):
        raise _StopSpawn

    monkeypatch.setattr(runtime_mod, "resolve_spawn_executable", _resolve)
    monkeypatch.setattr(adapters_mod.KiroAdapter, "pre_spawn", _pre_spawn)
    monkeypatch.setattr(runtime_mod, "wrap_argv", fake_wrap)

    runtime = AcpRuntime(work_dir=tmp_path / "ws", sandbox_mode="off")
    with pytest.raises(_StopSpawn):
        asyncio.run(runtime.spawn())

    assert seen, "resolution never ran"
    for thread in seen:
        assert thread is not loop_thread


def test_every_bundled_descriptor_resolves_to_an_adapter():
    """A bundled descriptor naming an adapter that does not exist would validate
    and then fall back to the generic rules at spawn — losing its own resolution
    and pre-spawn steps silently.

    Wave 2 (R1.2): an adapter-LESS descriptor (bundled Codex, every operator
    harness) resolves to the generic adapter by design — that IS its resolution,
    carrying the augmented-PATH rule and the API-key strip — so its adapter name
    is ``ADAPTER_GENERIC`` rather than its own empty string. A descriptor that
    NAMES an adapter must still resolve to exactly that one.
    """
    for row in harness_registry().list():
        descriptor = harness_registry().get(row.id)
        adapter = adapter_for(descriptor)
        expected = descriptor.adapter or ADAPTER_GENERIC
        assert adapter.name == expected, descriptor.id


def test_the_kas_argv_starts_at_the_binary_it_was_handed(tmp_path):
    """Belt and braces on the KAS adapter's own construction.

    The relay's argv is built by ``kas_transport``, not by the template, so the
    "attested executable is argv[0]" guarantee is asserted against it directly.
    """
    descriptor = harness_registry().get(HARNESS_KAS)
    kiro_bin = str(Path(tmp_path / "kiro-cli"))
    argv = adapter_for(descriptor).render_argv(descriptor, executable=kiro_bin)
    assert checked_spawn_argv(descriptor, argv, kiro_bin)[0] == kiro_bin
