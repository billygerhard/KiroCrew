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

An attestation is recorded ONLY for ``verified``, keyed by the descriptor's
:func:`descriptor_fingerprint` -- a hash over every field that shapes the spawn
(executable, argv, agent/model args, routing, permission config, MCP delivery).
Editing any of those invalidates the attestation by construction: the next boot
computes a different fingerprint, finds no match, and the backend is back to
known-but-unselectable until verified again. The store is a gateway-owned file
beside ``harnesses.json``; agents can read it and never write it (it is a
selectability grant, so it is fenced exactly as the descriptor file is -- see
``security/paths.py`` and ``sandbox.py``).

The runtime start path itself gains nothing here (harness-parity H13): the probe
is a CALLER of the factory, and the gate it feeds is the existing selectability
registry (H4). Kiro and the bundled harnesses never pass through this module.
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
) -> bool:
    """True when a recorded attestation matches THIS descriptor's fingerprint."""
    record = load_attestations(path).get(descriptor.id)
    if not record:
        return False
    return record.get("fingerprint") == descriptor_fingerprint(descriptor)


def record_attestation(
    descriptor: HarnessDescriptor,
    *,
    mechanism: str,
    evidence: Mapping[str, Any],
    path: "os.PathLike[str] | str | None" = None,
) -> dict[str, Any]:
    """Write the attestation for *descriptor* (replacing any prior one for its id)."""
    from kiro_crew.atomic_write import atomic_write

    p = attestations_path() if path is None else path
    current = load_attestations(p)
    record = {
        "fingerprint": descriptor_fingerprint(descriptor),
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


ProviderFactory = Callable[..., Any]


async def verify_routing(
    descriptor: HarnessDescriptor,
    factory: ProviderFactory,
    *,
    timeout: float = DEFAULT_PROBE_TIMEOUT_SECS,
    session_key: str | None = None,
    to_thread: Callable[..., Awaitable[Any]] = asyncio.to_thread,
) -> RoutingVerification:
    """Spawn the harness once through *factory* and observe whether it asks.

    *factory* is the production provider factory
    (``KiroCrewConfig.create_provider_factory()``); it is called with
    ``backend_override=descriptor.id`` so the probe crosses the same single
    selection gate a chat does, and with ``cwd`` set to a fresh scratch directory
    that the probe owns and removes. The provider is always shut down, even on
    timeout or failure.
    """
    from kiro_crew.providers.base import (
        EVENT_COMPLETE,
        EVENT_PERMISSION_REQUEST,
    )

    started = time.monotonic()
    scratch = tempfile.mkdtemp(prefix="kirocrew-routing-probe-")
    probe_path = os.path.join(scratch, PROBE_FILE_NAME)
    key = session_key or f"backend-routing-probe:{descriptor.id}"
    permission_requests = 0
    provider = None
    reason = ""
    try:
        try:
            provider = factory(key, backend_override=descriptor.id, cwd=scratch)
        except Exception as exc:  # noqa: BLE001 - the probe reports, never raises
            return RoutingVerification(
                VERDICT_INCONCLUSIVE,
                f"the provider could not be built for {descriptor.id!r}: {exc}",
                elapsed_secs=time.monotonic() - started,
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
        elapsed = time.monotonic() - started
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
        if provider is not None:
            try:
                await asyncio.wait_for(provider.shutdown(), timeout=15.0)
            except Exception:  # noqa: BLE001 - best-effort teardown
                logger.debug("routing probe: provider shutdown failed", exc_info=True)
        try:
            await to_thread(shutil.rmtree, scratch, True)
        except Exception:  # noqa: BLE001 - best-effort cleanup
            logger.debug("routing probe: scratch cleanup failed", exc_info=True)
