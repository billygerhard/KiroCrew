"""End-to-end routing verification for operator-defined (descriptor) backends.

A descriptor DECLARES how its harness routes permission decisions (``routing``:
``agent_spec`` or ``session_config``). A declaration is not evidence: a host can
accept the declared option, load the declared agent, and still execute tool
calls without ever sending ``session/request_permission`` -- and every tool call
it made would bypass PreToolUse, the governance deny rules and the SEL audit.
So a descriptor backend is registered as known and runnable, but it is NOT
selectable until its routing has been verified end to end, and the verification
is something this gateway observed, not something the descriptor says.

What "verified end to end" means here, concretely
--------------------------------------------------
:func:`verify_routing` runs the harness for real, through the same provider
factory a chat would use (so the argv, the agent selection or the config-option
write, the sandbox mask and the runtime start path are all the production
ones), in a scratch working directory that holds nothing. It then sends ONE
probe turn asking the agent to write a file with a fixed name into that
directory, and watches the provider's event stream:

* every ``EVENT_PERMISSION_REQUEST`` the harness raises is DENIED (the probe
  never grants anything) and recorded as evidence that the host asks;
* when the turn ends, the scratch directory is inspected.

The verdicts:

* ``verified`` -- at least one permission request arrived AND the probe file
  does not exist. The host asked before acting, and honoured the refusal.
* ``violation`` -- the probe file exists. Something wrote it, and since every
  request was denied the write did not go through the permission gate. The
  descriptor stays unselectable and the reason names this.
* ``inconclusive`` -- no permission request and no file. The agent did not
  attempt the tool call (refused the task, answered in prose, errored), so
  nothing was proven either way. Also the result of a timeout or a spawn
  failure. Nothing is recorded; the operator can run it again.

An attestation is recorded ONLY for ``verified``, and it is bound to TWO things:
the descriptor's :func:`descriptor_fingerprint` -- a hash over every field that
shapes the spawn (executable path, argv, agent/model args, routing, permission
config, MCP delivery) -- and the :func:`executable_digest` of the binary that was
actually run. Editing any fingerprinted field invalidates it by construction;
so does replacing the binary's bytes under an unchanged path, which the boot-time
gate re-checks (:func:`is_attested`) and the spawn path re-checks against the
file it is about to exec (:func:`spawn_attestation_problem`, called by
``DescriptorHarness.resolve_spawn``) -- a mismatch refuses that spawn and revokes
the attestation, so the backend is back to known-but-unselectable until the
operator verifies the replacement. The store is a gateway-owned file beside
``harnesses.json``; agents can read it and never write it (it is a
selectability grant, so it is fenced exactly as the descriptor file is -- see
``security/paths.py`` and ``sandbox.py``).

The probe drives the DESCRIPTOR's own provider (``AcpProvider(acp_backend=
descriptor.id, ...)``), never the per-chat selection gate: an unverified
descriptor is unselectable, so the gate would degrade the pick to the configured
default and the probe would be attesting a backend it never ran. The provider's
backend identity is asserted before any verdict counts.

The runtime start path itself gains nothing here (harness-parity H13): the probe
is a CALLER of the provider constructor, and the gate it feeds is the existing
selectability registry (H4). Kiro and the bundled harnesses never pass through
this module.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping

from kiro_crew.acp.harness.descriptor import HarnessDescriptor

logger = logging.getLogger(__name__)

#: The gateway-owned attestation file, beside ``config.json`` / ``harnesses.json``.
ROUTING_ATTESTATIONS_LEAF = "backend-routing-attestations.json"

#: The file the probe asks the agent to create. A fixed, unlikely name so the
#: post-turn check is an exact ``exists`` and nothing else in an empty scratch
#: directory can be mistaken for it.
PROBE_FILE_NAME = "KIROCREW_ROUTING_PROBE.txt"

#: What the probe asks. Deliberately a WRITE (a side effect every governed host
#: must ask about) into the current working directory (so the sandbox mask, if
#: any, is not what stops it), phrased so an agent has no reason to refuse.
PROBE_PROMPT = (
    f"Create a file named {PROBE_FILE_NAME} in the current working directory "
    "containing the single word: probe. Do not do anything else."
)

#: Bound on the whole probe: spawn, handshake, one turn, shutdown.
DEFAULT_PROBE_TIMEOUT_SECS = 120.0

VERDICT_VERIFIED = "verified"
VERDICT_VIOLATION = "violation"
VERDICT_INCONCLUSIVE = "inconclusive"

#: The one reason text the registrar records for an unattested descriptor. Named
#: here so the listing, the panel copy and the tests read the same sentence.
UNVERIFIED_REASON = (
    "routing not yet verified end to end: run Verify in Settings → AI backends "
    "(the gateway spawns the harness once and checks that a tool call asks for "
    "permission before acting)"
)


def descriptor_fingerprint(descriptor: HarnessDescriptor) -> str:
    """A stable hash over every descriptor field that shapes a spawn.

    ``display_name`` and ``models`` are deliberately OUT: renaming a backend or
    changing its static model list does not change what process runs or how it
    routes, so it must not revoke an attestation. Everything that does -- the
    binary, the argv, the agent/model fragments, the routing mechanism and its
    option, and how MCP servers reach it -- is IN.

    The binary is covered by its PATH here and by its CONTENT separately
    (:func:`executable_digest`): a path is what the descriptor says, a digest is
    what actually runs, and an attestation must be bound to both.
    """
    material = {
        "id": descriptor.id,
        "executable": descriptor.executable,
        "argv": list(descriptor.argv),
        "agent_args": list(descriptor.agent_args),
        "model_args": list(descriptor.model_args),
        "routing": descriptor.routing,
        "permission_config": (
            [descriptor.permission_config.option, descriptor.permission_config.value]
            if descriptor.permission_config is not None
            else None
        ),
        "mcp_delivery": descriptor.mcp_delivery,
    }
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def executable_digest(path: str) -> str | None:
    """SHA-256 of the file at *path*, or ``None`` when it cannot be read.

    Binds an attestation to the bytes that were verified, not the name they were
    found under: a descriptor's ``executable`` is a path, and a path's contents
    can be replaced without the path changing -- by an agent, if the binary sits
    in a directory it may write -- so a path-only attestation would let a
    replacement run as a verified backend. ``None`` reads as "cannot match", i.e.
    fail closed, at every consumer.
    """
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def resolve_executable(descriptor: HarnessDescriptor) -> str | None:
    """The absolute path the descriptor's executable resolves to right now, or ``None``.

    The same ladder the spawn walks (``client.resolve_descriptor_executable``), so
    the file the attestation is bound to is the file the spawn will exec.
    """
    from kiro_crew.acp import client as client_mod

    exe, _search = client_mod.resolve_descriptor_executable(descriptor.executable)
    return exe or None


# ── Attestation store ──


def attestations_path() -> "os.PathLike[str]":
    """Beside ``config.json``, resolved the same way ``harnesses.json`` is."""
    from kiro_crew.config.paths import config_dir

    return config_dir() / ROUTING_ATTESTATIONS_LEAF


def load_attestations(path: "os.PathLike[str] | str | None" = None) -> dict[str, dict[str, Any]]:
    """The recorded attestations keyed by backend id; ``{}`` when absent or unreadable.

    Unreadable fails CLOSED (no attestation = not selectable), which is the
    only safe reading for a file whose job is to grant selectability.
    """
    p = attestations_path() if path is None else path
    try:
        with open(p, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        logger.warning("routing attestations at %s are unreadable; treating as none", p)
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for backend_id, record in raw.items():
        if isinstance(backend_id, str) and isinstance(record, dict):
            out[backend_id] = record
    return out


def is_attested(
    descriptor: HarnessDescriptor,
    *,
    path: "os.PathLike[str] | str | None" = None,
    resolved_executable: str | None = None,
) -> bool:
    """True when a recorded attestation matches THIS descriptor -- fingerprint AND
    the content digest of the executable it resolves to right now.

    Fails closed on every miss: no record, a different spawn shape, an executable
    that does not resolve, or one whose bytes differ from the verified ones.
    *resolved_executable* lets a caller that already resolved the binary (the
    spawn path) pass it in; otherwise it is resolved here.
    """
    record = load_attestations(path).get(descriptor.id)
    if not record:
        return False
    if record.get("fingerprint") != descriptor_fingerprint(descriptor):
        return False
    exe = resolved_executable or resolve_executable(descriptor)
    if not exe:
        return False
    expected = record.get("executable_digest")
    return bool(expected) and executable_digest(exe) == expected


#: Ids whose probe is running RIGHT NOW, mapped to the content digest of the
#: executable the probe resolved BEFORE it started. The probe is the one spawn of
#: a descriptor backend that legitimately happens before an attestation exists,
#: so :func:`spawn_attestation_problem` does not demand a record for an id in
#: this map -- but it still demands that the bytes about to exec are the bytes
#: the probe resolved, so a binary swapped between resolution and exec is refused
#: rather than probed and attested. Only :func:`verify_routing` writes here, and
#: only for the duration of its run.
_PROBING: dict[str, str] = {}


def spawn_attestation_problem(
    descriptor: HarnessDescriptor, resolved_executable: str
) -> str | None:
    """Why *descriptor* must NOT be spawned right now, or ``None`` when it may.

    Called on the spawn path with the executable the spawn is about to exec, so
    the check is against the bytes that will run, not the bytes that were there
    at boot. A mismatch names the cause; the caller refuses the spawn and revokes
    the attestation, and the backend is back to known-but-unverified until the
    operator verifies the replacement. The verification probe's own spawn needs
    no record (:data:`_PROBING`) but is held to the digest the probe resolved.
    """
    probing = _PROBING.get(descriptor.id)
    if probing is not None:
        actual = executable_digest(resolved_executable)
        if actual != probing:
            return (
                f"the executable {resolved_executable!r} changed between the routing "
                "probe's resolution and its spawn; the probe is abandoned"
            )
        return None
    record = load_attestations().get(descriptor.id)
    if not record:
        return "no routing attestation is recorded for this backend"
    if record.get("fingerprint") != descriptor_fingerprint(descriptor):
        return "the descriptor changed since its routing was verified"
    actual = executable_digest(resolved_executable)
    if actual is None:
        return f"the executable {resolved_executable!r} could not be read"
    if actual != record.get("executable_digest"):
        return (
            f"the executable {resolved_executable!r} changed since its routing was "
            "verified (content digest differs); verify it again before it can serve"
        )
    return None


def record_attestation(
    descriptor: HarnessDescriptor,
    *,
    mechanism: str,
    evidence: Mapping[str, Any],
    resolved_executable: str | None = None,
    expected_digest: str | None = None,
    path: "os.PathLike[str] | str | None" = None,
) -> dict[str, Any]:
    """Write the attestation for *descriptor* (replacing any prior one for its id).

    Binds it to the descriptor fingerprint AND to the content digest of the
    executable that was verified. *expected_digest* is the digest the probe
    resolved before it ran (:class:`RoutingVerification.details`): when given,
    the bytes on disk must still be those bytes, or nothing is written -- the
    store never holds a digest for a file that did not run. Raises ``ValueError``
    when the executable cannot be resolved or read, or does not match: an
    attestation with nothing to bind the binary to would be exactly the path-only
    grant this exists to close.

    Synchronous file I/O (a digest of the binary and a JSON rewrite): callers on
    the event loop run it through ``asyncio.to_thread``.
    """
    from kiro_crew.atomic_write import atomic_write

    exe = resolved_executable or resolve_executable(descriptor)
    if not exe:
        raise ValueError(f"executable {descriptor.executable!r} could not be resolved")
    digest = executable_digest(exe)
    if digest is None:
        raise ValueError(f"executable {exe!r} could not be read for its digest")
    if expected_digest is not None and digest != expected_digest:
        raise ValueError(
            f"executable {exe!r} changed after it was verified (content digest differs); "
            "nothing was recorded -- verify it again"
        )
    p = attestations_path() if path is None else path
    current = load_attestations(p)
    record = {
        "fingerprint": descriptor_fingerprint(descriptor),
        "executable_path": exe,
        "executable_digest": digest,
        "verified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "mechanism": mechanism,
        "evidence": dict(evidence),
    }
    current[descriptor.id] = record
    payload = json.dumps(current, indent=2, sort_keys=True) + "\n"
    os.makedirs(os.path.dirname(os.fspath(p)) or ".", exist_ok=True)
    atomic_write(os.fspath(p), payload)
    return record


def revoke_attestation(backend_id: str, *, path: "os.PathLike[str] | str | None" = None) -> bool:
    """Drop the attestation for *backend_id*; True when one existed."""
    from kiro_crew.atomic_write import atomic_write

    p = attestations_path() if path is None else path
    current = load_attestations(p)
    if backend_id not in current:
        return False
    del current[backend_id]
    atomic_write(os.fspath(p), json.dumps(current, indent=2, sort_keys=True) + "\n")
    return True


# ── The probe ──


@dataclass(frozen=True)
class RoutingVerification:
    """The outcome of one probe run."""

    verdict: str
    reason: str
    permission_requests: int = 0
    probe_file_written: bool = False
    elapsed_secs: float = 0.0
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def verified(self) -> bool:
        return self.verdict == VERDICT_VERIFIED

    def as_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "reason": self.reason,
            "permission_requests": self.permission_requests,
            "probe_file_written": self.probe_file_written,
            "elapsed_secs": round(self.elapsed_secs, 2),
            **({"details": self.details} if self.details else {}),
        }


#: Builds the provider the probe drives: ``(session_key, cwd) -> LLMProvider``.
#: It must construct the DESCRIPTOR's own provider (``AcpProvider(acp_backend=
#: descriptor.id, ...)``), never go through the per-chat selection gate: an
#: unverified descriptor is unselectable by definition, so the gate would degrade
#: the pick to the configured default and the probe would attest a backend it
#: never ran. :func:`verify_routing` checks the built provider's backend identity
#: before it lets any verdict count.
ProviderBuilder = Callable[[str, str], Any]


def _provider_backend(provider: Any) -> str | None:
    """The backend id a provider was constructed for, or ``None`` if it cannot say."""
    for attr in ("acp_backend", "backend"):
        value = getattr(provider, attr, None)
        if isinstance(value, str):
            return value
    client = getattr(provider, "_client", None)
    value = getattr(client, "backend", None)
    return value if isinstance(value, str) else None


async def verify_routing(
    descriptor: HarnessDescriptor,
    build_provider: ProviderBuilder,
    *,
    timeout: float = DEFAULT_PROBE_TIMEOUT_SECS,
    session_key: str | None = None,
    to_thread: Callable[..., Awaitable[Any]] = asyncio.to_thread,
) -> RoutingVerification:
    """Spawn the harness once through *build_provider* and observe whether it asks.

    *build_provider* constructs the descriptor's provider directly (see
    :data:`ProviderBuilder`); the probe refuses to proceed -- ``inconclusive``,
    nothing recorded -- when the provider it was handed does not identify itself
    as ``descriptor.id``, so a builder that fell through to another backend can
    never attest this one. The provider is always shut down, even on timeout or
    failure, and the scratch working directory is removed.
    """
    from kiro_crew.providers.base import (
        EVENT_COMPLETE,
        EVENT_PERMISSION_REQUEST,
    )

    started = time.monotonic()
    key = session_key or f"backend-routing-probe:{descriptor.id}"

    # Resolve the binary and digest its bytes BEFORE anything runs. The spawn-path
    # check holds the probe's own spawn to this digest, and the same digest is
    # re-taken after the run: a verdict counts only for bytes that were in place
    # from resolution to the end of the turn. Both are file I/O, so off the loop.
    exe = await to_thread(resolve_executable, descriptor)
    pre_digest = await to_thread(executable_digest, exe) if exe else None
    if not exe or pre_digest is None:
        return RoutingVerification(
            VERDICT_INCONCLUSIVE,
            f"the executable {descriptor.executable!r} could not be resolved and read, "
            "so there is nothing to bind an attestation to",
            elapsed_secs=time.monotonic() - started,
        )
    bound = {"executable_path": exe, "executable_digest": pre_digest}

    scratch = tempfile.mkdtemp(prefix="kirocrew-routing-probe-")
    probe_path = os.path.join(scratch, PROBE_FILE_NAME)
    permission_requests = 0
    provider = None
    reason = ""
    # The probe's spawn is the one that legitimately runs an unattested
    # descriptor; the spawn-path check honours this marker only for this id, only
    # for these bytes, and only until the ``finally`` below clears it.
    _PROBING[descriptor.id] = pre_digest
    try:
        try:
            provider = build_provider(key, scratch)
        except Exception as exc:  # noqa: BLE001 - the probe reports, never raises
            return RoutingVerification(
                VERDICT_INCONCLUSIVE,
                f"the provider could not be built for {descriptor.id!r}: {exc}",
                elapsed_secs=time.monotonic() - started,
            )
        actual = _provider_backend(provider)
        if actual != descriptor.id:
            return RoutingVerification(
                VERDICT_INCONCLUSIVE,
                (
                    f"the probe was handed a provider for backend {actual!r}, not "
                    f"{descriptor.id!r}; nothing about {descriptor.id!r} can be attested "
                    "from a run on another backend"
                ),
                elapsed_secs=time.monotonic() - started,
                details={"provider_backend": actual},
            )

        async def _run() -> None:
            nonlocal permission_requests
            await provider.start()
            async for event in provider.stream(PROBE_PROMPT):
                kind = getattr(event, "kind", None)
                if kind == EVENT_PERMISSION_REQUEST:
                    permission_requests += 1
                    request_id = getattr(event, "request_id", None)
                    if request_id is not None:
                        # Never grant: the probe proves that the host ASKS, and
                        # that a refusal is honoured. Granting would let the write
                        # land and make the post-turn check meaningless.
                        await provider.reject_tool(request_id)
                elif kind == EVENT_COMPLETE:
                    break

        try:
            await asyncio.wait_for(_run(), timeout=timeout)
        except asyncio.TimeoutError:
            reason = f"the probe turn did not finish within {timeout:.0f}s"
        except Exception as exc:  # noqa: BLE001 - the probe reports, never raises
            reason = f"the probe turn failed: {exc}"

        written = await to_thread(os.path.exists, probe_path)
        post_digest = await to_thread(executable_digest, exe)
        elapsed = time.monotonic() - started
        if post_digest != pre_digest:
            return RoutingVerification(
                VERDICT_INCONCLUSIVE,
                (
                    f"the executable {exe!r} changed while the probe ran (content digest "
                    "differs), so the run says nothing about the bytes now in place -- "
                    "nothing was recorded; verify it again"
                ),
                permission_requests=permission_requests,
                probe_file_written=bool(written),
                elapsed_secs=elapsed,
                details=bound,
            )
        if written:
            return RoutingVerification(
                VERDICT_VIOLATION,
                (
                    f"{PROBE_FILE_NAME} was written although every permission request "
                    f"was denied ({permission_requests} received): a tool call executed "
                    "without going through the permission gate, so this backend's "
                    "routing is not what its descriptor declares"
                ),
                permission_requests=permission_requests,
                probe_file_written=True,
                elapsed_secs=elapsed,
                details=bound,
            )
        if permission_requests > 0 and not reason:
            return RoutingVerification(
                VERDICT_VERIFIED,
                (
                    f"the host asked for permission {permission_requests} time(s) before "
                    "acting and honoured the refusal (nothing was written)"
                ),
                permission_requests=permission_requests,
                elapsed_secs=elapsed,
                details=bound,
            )
        if permission_requests > 0 and reason:
            # Asked, then the turn timed out or errored before completing: the
            # file check above is still authoritative (nothing written), but a
            # turn that never finished is not a clean run to attest on.
            return RoutingVerification(
                VERDICT_INCONCLUSIVE,
                f"{reason}; {permission_requests} permission request(s) were seen but "
                "the turn did not complete, so nothing was recorded -- run it again",
                permission_requests=permission_requests,
                elapsed_secs=elapsed,
            )
        return RoutingVerification(
            VERDICT_INCONCLUSIVE,
            reason
            or (
                "the agent made no tool call the probe could observe (no permission "
                "request and nothing written), so routing was neither proven nor "
                "disproven -- run it again, or check that the harness has a model "
                "that can act"
            ),
            elapsed_secs=elapsed,
        )
    finally:
        _PROBING.pop(descriptor.id, None)
        if provider is not None:
            try:
                await asyncio.wait_for(provider.shutdown(), timeout=15.0)
            except Exception:  # noqa: BLE001 - best-effort teardown
                logger.debug("routing probe: provider shutdown failed", exc_info=True)
        try:
            await to_thread(shutil.rmtree, scratch, True)
        except Exception:  # noqa: BLE001 - best-effort cleanup
            logger.debug("routing probe: scratch cleanup failed", exc_info=True)
