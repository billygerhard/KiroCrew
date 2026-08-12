"""The two external transports: a plain command, and an MCP stdio child.

What both have in common matters more than what differs. Each takes a
configured argv and environment, hands the child the capability request as JSON,
reads structured output back, and answers within one wall-clock deadline or not
at all. Neither carries any provider's implementation: the app spawns what
configuration names, so an enhanced analyzer, a coding agent, or an
organization's reviewer is a config entry rather than code in this tree.

Every spawn here goes through the package's sandbox chokepoint and applies a
kernel resource ceiling. The program and its literal arguments come from
configuration an operator wrote, but the child then reads a request describing
documents whose text an anonymous stranger may have authored, and it runs
unattended. The sandbox hides credential trees the provider has no business
reading and hands it a scrubbed environment; the resource limits keep a runaway
child from taking the host with it.

Three deliberate decisions:

* **The whole exchange is one bounded call.** The request is written, stdin is
  closed, and output is read under a single timeout. A provider that trickles
  output cannot hold the engine open, which is the failure this shape exists to
  prevent.
* **stdin closes after the request.** An unattended provider has nobody to
  answer a prompt, so a child that reads further gets end-of-file rather than
  blocking until the deadline.
* **Output is capped and never executed.** It is parsed as JSON and nothing
  else: no path here evaluates, renders, or shells out with anything the child
  printed.

A failure is reported as :class:`TransportFailure` carrying the stable finding
identifier for its condition, which is what the caller turns into a degradation
the diagnostic and the audit log name identically.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from kiro_crew import platform_compat, sandbox

from .contracts import (
    FINDING_PROVIDER_TIMEOUT,
    FINDING_PROVIDER_UNAVAILABLE,
    FINDING_RESPONSE_INVALID,
    TRANSPORT_COMMAND,
    TRANSPORT_MCP,
    CapabilityRequest,
)

logger = logging.getLogger(__name__)

#: MCP protocol revision announced in the handshake.
MCP_PROTOCOL_VERSION = "2024-11-05"

#: Client identity announced to a provider's MCP server.
MCP_CLIENT_NAME = "spec-engine-capability-client"

#: Prefix of the tool a capability provider exposes over MCP. The analysis
#: capability is served by ``capability_analysis``, and so on: one naming rule so
#: a provider author needs the capability name and nothing else.
MCP_TOOL_PREFIX = "capability_"

#: Characters kept per stream. Output is data from a program the engine does not
#: control, and an unbounded read makes that program the memory ceiling.
MAX_OUTPUT_CHARS = 1024 * 1024

#: Grace period for draining a killed child's pipes before its output is given up.
_DRAIN_TIMEOUT_S = 5

#: Non-JSON lines tolerated while looking for a JSON-RPC message. A launcher that
#: prints a banner is common; one that prints only banners is broken.
_MAX_BANNER_LINES = 50


class TransportFailure(Exception):
    """A provider could not be reached, timed out, or answered unusably."""

    def __init__(self, finding_id: str, reason: str, *, detail: str = "") -> None:
        self.finding_id = finding_id
        self.reason = reason
        self.detail = detail
        super().__init__(reason)


class CapabilityTransport(Protocol):
    """Sends one capability request to an external provider and returns its payload.

    The returned mapping is unvalidated: schema validation belongs to the one
    invocation path, so a transport cannot be the thing that decides a response
    was acceptable.
    """

    @property
    def transport(self) -> str: ...

    def invoke(self, request: CapabilityRequest, *, timeout_s: int) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class ChildOutcome:
    """What running one provider child produced."""

    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    start_error: str = ""


def run_provider_child(
    argv: Sequence[str],
    *,
    stdin_text: str,
    timeout_s: int,
    env_overlay: Mapping[str, str] | None = None,
    cwd: Path | None = None,
) -> ChildOutcome:
    """Run a provider child once, feeding it *stdin_text* and capturing its output.

    The spawn is routed through the sandbox chokepoint for filesystem isolation
    and a credential-scrubbed environment, and the resource-limit preexec applies
    a kernel ceiling on top: this child is an operator-configured program that
    runs unattended over content the engine did not author.

    The configured environment is overlaid *after* the scrub rather than passed
    into it. An operator who declares a variable for their provider means it to
    arrive; scrubbing it back out would silently break the binding, while
    scrubbing first still removes everything the operator did not name.
    """
    base = dict(os.environ)
    wrapped, child_env, cleanup = sandbox.sandboxed_spawn_argv(list(argv), env=base)
    if env_overlay:
        child_env.update({str(key): str(value) for key, value in env_overlay.items()})
    started: subprocess.Popen[str]
    try:
        # start_new_session is a no-op on Windows and the creation flag a no-op on
        # POSIX; together they put the child in its own group so a timeout kills
        # the whole tree rather than orphaning grandchildren that still hold the
        # pipes.
        started = subprocess.Popen(
            wrapped,
            cwd=str(cwd) if cwd is not None else None,
            env=child_env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            preexec_fn=sandbox.resource_limit_preexec(),
            start_new_session=platform_compat.IS_POSIX,
            creationflags=platform_compat.CREATE_NEW_PROCESS_GROUP,
        )
    except (OSError, ValueError) as exc:
        _remove_launcher(cleanup)
        return ChildOutcome(start_error=f"cannot run {argv[0]!r}: {exc}")
    except BaseException:
        # Everything else the spawn can raise, which is not nothing: a failing
        # resource-limit preexec surfaces as SubprocessError rather than OSError,
        # and an interrupt arriving mid-spawn is not an error at all. Both left
        # the launcher script on disk, and the caller that would have removed it
        # is the one being unwound. Re-raised unchanged -- this is about the file,
        # not about swallowing the failure.
        _remove_launcher(cleanup)
        raise
    try:
        try:
            stdout, stderr = started.communicate(input=stdin_text, timeout=timeout_s)
        except subprocess.TimeoutExpired:
            platform_compat.kill_process_tree(started.pid, platform_compat.SIGKILL)
            try:
                stdout, stderr = started.communicate(timeout=_DRAIN_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                stdout, stderr = "", ""
            return ChildOutcome(
                stdout=_cap(stdout),
                stderr=_cap(stderr),
                timed_out=True,
            )
        except (BrokenPipeError, OSError) as exc:
            platform_compat.kill_process_tree(started.pid, platform_compat.SIGKILL)
            return ChildOutcome(start_error=f"{argv[0]!r} closed its input: {exc}")
        return ChildOutcome(
            exit_code=started.returncode,
            stdout=_cap(stdout),
            stderr=_cap(stderr),
        )
    finally:
        _remove_launcher(cleanup)


class ChildRunner(Protocol):
    """Runs one provider child. The seam the transport tests substitute."""

    def __call__(
        self,
        argv: Sequence[str],
        *,
        stdin_text: str,
        timeout_s: int,
        env_overlay: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> ChildOutcome: ...


@dataclass(frozen=True)
class CommandProviderTransport:
    """Invokes a provider as a plain program: request on stdin, response on stdout.

    The simplest transport, and the one that lets an external coding agent or any
    command-line tool serve a capability without speaking a protocol.
    """

    argv: tuple[str, ...]
    env: Mapping[str, str] = field(default_factory=dict)
    cwd: Path | None = None
    runner: ChildRunner | None = None

    @property
    def transport(self) -> str:
        return TRANSPORT_COMMAND

    def invoke(self, request: CapabilityRequest, *, timeout_s: int) -> Mapping[str, Any]:
        run = self.runner if self.runner is not None else run_provider_child
        outcome = run(
            self.argv,
            stdin_text=json.dumps(request.to_wire()) + "\n",
            timeout_s=timeout_s,
            env_overlay=dict(self.env),
            cwd=self.cwd,
        )
        _raise_for_child(self.argv[0], outcome, timeout_s)
        payload = _last_json_object(outcome.stdout)
        if payload is None:
            raise TransportFailure(
                FINDING_RESPONSE_INVALID,
                f"{self.argv[0]!r} printed no JSON object on standard output",
                detail=_tail(outcome.stdout) or _tail(outcome.stderr),
            )
        return payload


@dataclass(frozen=True)
class McpProviderTransport:
    """Invokes a provider as an MCP server child over stdio.

    The handshake, the initialized notification, and the tool call are written
    together and stdin is then closed. A stdio server reads its input in order,
    so pipelining changes nothing it observes while removing the only thing that
    could make this call unbounded: waiting on a reply before sending the next
    message. One deadline covers the entire exchange, which is the guarantee the
    caller needs.
    """

    argv: tuple[str, ...]
    env: Mapping[str, str] = field(default_factory=dict)
    cwd: Path | None = None
    runner: ChildRunner | None = None

    @property
    def transport(self) -> str:
        return TRANSPORT_MCP

    def tool_name(self, capability: str) -> str:
        """The tool this transport calls for *capability*."""
        return f"{MCP_TOOL_PREFIX}{capability}"

    def invoke(self, request: CapabilityRequest, *, timeout_s: int) -> Mapping[str, Any]:
        run = self.runner if self.runner is not None else run_provider_child
        call_id = 2
        conversation = "".join(
            json.dumps(message) + "\n"
            for message in (
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": MCP_PROTOCOL_VERSION,
                        "capabilities": {},
                        "clientInfo": {"name": MCP_CLIENT_NAME, "version": "1"},
                    },
                },
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {
                    "jsonrpc": "2.0",
                    "id": call_id,
                    "method": "tools/call",
                    "params": {
                        "name": self.tool_name(request.capability),
                        "arguments": request.to_wire(),
                    },
                },
            )
        )
        outcome = run(
            self.argv,
            stdin_text=conversation,
            timeout_s=timeout_s,
            env_overlay=dict(self.env),
            cwd=self.cwd,
        )
        # A non-zero exit is tolerated when the tool result already arrived: an
        # MCP server that answers and then exits untidily has still answered.
        if outcome.timed_out:
            raise TransportFailure(
                FINDING_PROVIDER_TIMEOUT,
                f"{self.argv[0]!r} did not answer within {timeout_s}s and was killed",
                detail=_tail(outcome.stderr),
            )
        if outcome.start_error:
            raise TransportFailure(
                FINDING_PROVIDER_UNAVAILABLE, outcome.start_error, detail=_tail(outcome.stderr)
            )
        reply = _jsonrpc_reply(outcome.stdout, call_id)
        if reply is None:
            raise TransportFailure(
                FINDING_PROVIDER_UNAVAILABLE,
                f"{self.argv[0]!r} returned no result for the capability tool call",
                detail=_tail(outcome.stdout) or _tail(outcome.stderr),
            )
        error = reply.get("error")
        if isinstance(error, Mapping):
            raise TransportFailure(
                FINDING_PROVIDER_UNAVAILABLE,
                f"{self.argv[0]!r} refused the capability tool call",
                detail=str(error.get("message", ""))[:MAX_OUTPUT_CHARS],
            )
        payload = _tool_result_payload(reply.get("result"))
        if payload is None:
            raise TransportFailure(
                FINDING_RESPONSE_INVALID,
                f"{self.argv[0]!r} returned a tool result carrying no JSON object",
                detail=_tail(outcome.stdout),
            )
        return payload


def _raise_for_child(program: str, outcome: ChildOutcome, timeout_s: int) -> None:
    """Turn a child's failure into the transport failure that names its condition."""
    if outcome.timed_out:
        raise TransportFailure(
            FINDING_PROVIDER_TIMEOUT,
            f"{program!r} did not answer within {timeout_s}s and was killed",
            detail=_tail(outcome.stderr),
        )
    if outcome.start_error:
        raise TransportFailure(
            FINDING_PROVIDER_UNAVAILABLE, outcome.start_error, detail=_tail(outcome.stderr)
        )
    if outcome.exit_code is None or outcome.exit_code != 0:
        raise TransportFailure(
            FINDING_PROVIDER_UNAVAILABLE,
            f"{program!r} exited {outcome.exit_code}",
            detail=_tail(outcome.stderr),
        )


def _last_json_object(text: str) -> dict[str, Any] | None:
    """Return the response object printed on stdout, tolerating a leading banner.

    The whole stream is tried first, so a pretty-printed multi-line response
    parses. Failing that, lines are scanned from the end: a launcher that prints
    a version banner before the real output is common enough that refusing it
    would reject working providers, while taking the *last* object keeps a banner
    that happens to be JSON from being mistaken for the answer.
    """
    stripped = text.strip()
    if not stripped:
        return None
    with contextlib.suppress(json.JSONDecodeError):
        loaded = json.loads(stripped)
        if isinstance(loaded, dict):
            return loaded
    lines = [line for line in stripped.splitlines() if line.strip()]
    for line in reversed(lines[-_MAX_BANNER_LINES:]):
        with contextlib.suppress(json.JSONDecodeError):
            loaded = json.loads(line)
            if isinstance(loaded, dict):
                return loaded
    return None


def _jsonrpc_reply(text: str, call_id: int) -> dict[str, Any] | None:
    """Return the JSON-RPC reply with *call_id* from line-delimited output."""
    banners = 0
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            message = json.loads(stripped)
        except json.JSONDecodeError:
            banners += 1
            if banners > _MAX_BANNER_LINES:
                return None
            continue
        if isinstance(message, dict) and message.get("id") == call_id:
            return message
    return None


def _tool_result_payload(result: Any) -> dict[str, Any] | None:
    """Extract the capability response object from an MCP tool result.

    Both shapes a compliant server may use are accepted: a structured content
    object, or a text content block holding the JSON. Refusing one of them would
    make the transport depend on a server's choice between two equally valid
    encodings.
    """
    if not isinstance(result, Mapping):
        return None
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        return structured
    content = result.get("content")
    if not isinstance(content, Sequence) or isinstance(content, (str, bytes)):
        return None
    for block in content:
        if not isinstance(block, Mapping) or block.get("type") != "text":
            continue
        text = block.get("text")
        if not isinstance(text, str):
            continue
        payload = _last_json_object(text)
        if payload is not None:
            return payload
    return None


def _remove_launcher(path: str | None) -> None:
    """Delete the sandbox launcher the chokepoint wrote for this spawn."""
    if not path:
        return
    with contextlib.suppress(OSError):
        os.unlink(path)


def _cap(text: str | None) -> str:
    if not text:
        return ""
    return text[:MAX_OUTPUT_CHARS]


def _tail(text: str, *, limit: int = 512) -> str:
    """The last of a stream, for a failure detail. Provider-authored text."""
    stripped = (text or "").strip()
    return stripped[-limit:]
