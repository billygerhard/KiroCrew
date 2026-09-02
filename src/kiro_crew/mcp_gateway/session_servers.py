"""Session-scoped MCP server delivery over ACP ``session/new``.

Two harness populations reach this module, and the ``mcpServers`` array means a
different thing to each:

- **File-fed** (kiro-cli): the harness loads its servers from its own agent
  spec, so the array carries ONLY the pooled broker stubs. Every paragraph below
  describes that mode; :func:`pooled_session_servers` is its entry point.
- **Wire-fed** (every other harness, and the default for an operator descriptor
  that declares nothing): the harness reads no configuration file of ours, so the
  array is the ONLY channel through which it learns of any MCP server at all. An
  empty array there is not "the spec already has them" but "this session has no
  tools", which is why :func:`delivery_servers` reports that case instead of
  letting it pass as normal.

Wire-fed delivery costs something file-fed does not, and it is stated here
because it cannot be undone at a call site: an entry's ``env`` and ``headers``
routinely hold tokens, and sending the server list on the wire puts them in the
``session/new`` frame rather than leaving them in the file they were declared in.
That is inherent to a harness that reads no file of ours — without them the
server cannot start — so the mode is chosen per harness rather than per server.
It also moves one decision from the harness to us: a server the operator muted
(``disabled``) is skipped by kiro-cli when it reads its own spec, while a wire-fed
harness never sees that flag at all (ACP has no field for it), so the mute is
applied here or not at all — see :func:`wire_session_servers`.

kiro-cli's ACP ``session/new`` accepts an ``mcpServers`` array, and a
session-injected server takes precedence over the same-named entry in the
resolved agent spec: the spec's own copy is never launched. That makes pooling
a protocol-level operation. The broker stubs replace an agent's poolable
servers for the lifetime of one session, and nothing is written to the user's
project, to their ``~/.kiro/agents/``, or through a bind mount — so pooling
works with ``agent.sandbox`` set to ``off`` (the default) and on macOS and
Windows, neither of which can bind-mount.

Only stub entries are injected. A non-poolable server is left entirely to the
agent spec, so its ``env`` — which routinely holds tokens and API keys — never
leaves the file it was declared in. Stub entries carry ``env: {}`` by
construction (``rewriter._build_stub_entry``): the pooled backend is spawned by
gatewayd, not by kiro-cli, so no credential is transmitted here either.

Precedence caveat: same-name override is verified against the shipped binary
(``test_mcp_gateway_session_inject.py`` pins it, including a live check when
kiro-cli is on PATH) but is NOT documented by kiro-cli. The documented
hierarchy covers only the three *file* tiers (agent config > workspace
``mcp.json`` > global ``mcp.json``). If a future release made injection purely
additive, an agent's own copy would launch alongside the stub, which is worse
than not pooling — hence the pinning test.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

from kiro_crew.mcp_gateway.rewriter import _WRAPPER_MARKER, _WRAPPER_MARKER_LEGACY

if TYPE_CHECKING:  # pragma: no cover - typing only
    from kiro_crew.agent_sdk.harness import HarnessDescriptor

logger = logging.getLogger(__name__)

# Keys that are positional in the ACP element shape (``name``) or that we
# always re-derive (``env``), so they must not be copied verbatim.
_ACP_RESERVED = frozenset({"name", "env", _WRAPPER_MARKER, _WRAPPER_MARKER_LEGACY})

# ── Transports ──
# ACP requires every conformant agent to accept a stdio server, and makes HTTP
# and SSE opt-in capabilities defaulting to false. So stdio needs no permission
# and the other two need an explicit claim in the initialize response.

TRANSPORT_STDIO = "stdio"
TRANSPORT_HTTP = "http"
TRANSPORT_SSE = "sse"
_TRANSPORTS = frozenset({TRANSPORT_STDIO, TRANSPORT_HTTP, TRANSPORT_SSE})

# ── Scopes ──
# Which store supplied the map that was delivered. Recorded on the report because
# the three can disagree about the same agent name and the delivered entries look
# identical either way, so an operator debugging "why did this session get those
# servers" has nothing else to read.

SPEC_SCOPE_OVERLAY = "overlay"
SPEC_SCOPE_PROJECT = "project"
SPEC_SCOPE_USER = "user"

#: Why an entry the operator muted is not delivered. One spelling, shared with
#: the tests that pin the guard.
REASON_DISABLED = "disabled by the operator"

# Keys positional in the ACP remote element, or re-derived (``headers``).
_ACP_REMOTE_RESERVED = frozenset(
    {"name", "type", "url", "headers", _WRAPPER_MARKER, _WRAPPER_MARKER_LEGACY}
)


@dataclass(frozen=True)
class McpTransports:
    """The MCP transports a harness advertised at ACP ``initialize``.

    Both flags default to False, which is ACP's own default and the only safe
    direction: an entry sent over a transport the harness never claimed does not
    fail loudly, it simply never comes up — which reaches the user as a tool that
    silently vanished rather than as a transport mismatch. Refusing to send it
    and naming the omission is what turns that into something readable.
    """

    http: bool = False
    sse: bool = False

    @classmethod
    def from_agent_capabilities(cls, capabilities: Any) -> "McpTransports":
        """Read ``agentCapabilities.mcpCapabilities`` from an initialize response.

        Total over any input: the response is the harness's, not ours, so a
        missing key, a non-object, or a truthy STRING (``"true"``) all mean "not
        advertised". Strict ``is True`` rather than truthiness for the same
        reason the descriptor parser refuses a truthy string — guessing here
        grants a transport nobody claimed.
        """
        caps = capabilities if isinstance(capabilities, Mapping) else {}
        mcp = caps.get("mcpCapabilities")
        if not isinstance(mcp, Mapping):
            return cls()
        return cls(http=mcp.get("http") is True, sse=mcp.get("sse") is True)

    def allows(self, transport: str) -> bool:
        """True when an entry using ``transport`` may be sent to this harness."""
        if transport == TRANSPORT_STDIO:
            return True
        if transport == TRANSPORT_HTTP:
            return self.http
        if transport == TRANSPORT_SSE:
            return self.sse
        # An unclassifiable entry (neither command nor url) is not sendable on
        # any transport, so it is refused here rather than shaped into an
        # element the harness would reject.
        return False

    def advertised(self) -> tuple[str, ...]:
        """Every transport this harness accepts, stdio first, for reporting."""
        out = [TRANSPORT_STDIO]
        if self.http:
            out.append(TRANSPORT_HTTP)
        if self.sse:
            out.append(TRANSPORT_SSE)
        return tuple(out)


@dataclass(frozen=True)
class OmittedServer:
    """One authorized server that was NOT sent, and why.

    Recorded per server rather than counted: "3 servers omitted" tells an
    operator nothing they can act on, whereas the name plus the transport it
    needed is the whole fix.
    """

    name: str
    transport: str
    reason: str

    @property
    def muted(self) -> bool:
        """True when the operator switched this server off.

        The mute is the one omission class that is not a capability gap, and the
        reports read differently because of it — see :meth:`transport_clause`.
        """
        return self.reason == REASON_DISABLED

    def transport_clause(self) -> str:
        """The transport to name in a report, or ``""`` when naming one misdirects.

        Every other omission is something the harness could not accept, so the
        transport it needed is the fix. A mute needs nothing: the operator turned
        the server off, and "needs stdio" beside that reason sends them looking
        for a transport gap that is not there.
        """
        if self.muted:
            return ""
        return self.transport or "an unknown transport"


def _omission_phrase(omission: OmittedServer) -> str:
    """One omission rendered for a human-readable summary line."""
    clause = omission.transport_clause()
    return (
        f"{omission.name!r} needs {clause}" if clause else f"{omission.name!r} ({omission.reason})"
    )


@dataclass(frozen=True)
class McpDelivery:
    """What was sent to one session's ``session/new``, and what was not.

    The structured record behind the delivery report: a caller sends
    ``servers``, and reports ``omitted`` / :attr:`no_mcp_tools` instead of
    discovering later that a session has no tools.

    ``scope`` names the store the map came from (``SPEC_SCOPE_*``, empty when
    nothing supplied one). Two scopes can declare the same agent name and the
    resulting wire entries are indistinguishable, so without it a substituted
    map is unreadable after the fact.
    """

    harness: str
    mode: str
    servers: tuple[dict[str, Any], ...] = ()
    omitted: tuple[OmittedServer, ...] = ()
    scope: str = ""

    @property
    def no_mcp_tools(self) -> bool:
        """True when this session reaches its harness with no MCP server at all.

        Wire-fed only. An empty array on a file-fed harness says nothing about
        the session's tools — the harness loads them from its own spec — so
        reporting it would train the reader to ignore the report.
        """
        return self.mode == _wire_fed_mode() and not self.servers

    def as_dict(self) -> dict[str, Any]:
        """The report as plain data, for a log record or an API payload.

        Names only: an entry's ``env``, ``headers``, and ``url`` can carry
        credentials, and a report is exactly the kind of thing that gets copied
        into a bug tracker.
        """
        return {
            "harness": self.harness,
            "delivery": self.mode,
            "scope": self.scope,
            "served": [str(entry.get("name") or "") for entry in self.servers],
            "omitted": [
                {"name": o.name, "transport": o.transport, "reason": o.reason}
                for o in self.omitted
            ],
            "noMcpTools": self.no_mcp_tools,
        }

    def summary(self) -> str:
        """One human-readable line naming what was omitted and why."""
        parts = [f"{len(self.servers)} MCP server(s) delivered ({self.mode})"]
        if self.omitted:
            parts.append("omitted: " + ", ".join(_omission_phrase(o) for o in self.omitted))
        return "; ".join(parts)


def report_delivery(delivery: McpDelivery) -> None:
    """Log everything one session's MCP delivery did NOT give it.

    Two classes are reported, neither of which the session can discover for
    itself afterwards: a server withheld for lack of an advertised transport, and
    a wire-fed session that ends up with no MCP server at all. A tool-less
    session is a WARNING rather than an info note because it is
    indistinguishable, from the chat, from a harness that silently ignored the
    servers it was sent.

    Lives here rather than on each caller so ``AcpClient`` and ``AcpRuntime``
    report a delivery in exactly the same words: the two compose the same array
    from the same resolver, and two copies of this wording would drift into two
    different answers to "why does my session have no tools".

    Server names come from a user-writable agent spec, so they are repr'd — the
    report reaches the gateway log and, through it, the dashboard. The transport
    clause is omitted for an operator's mute
    (:meth:`OmittedServer.transport_clause`): that row is not a capability gap,
    and "needs stdio" beside "disabled by the operator" reads as a harness that
    could not take the server rather than one the operator switched off.
    """
    for omission in delivery.omitted:
        clause = omission.transport_clause()
        logger.warning(
            "ACP: MCP server %r not delivered to harness %r — %s%s",
            omission.name,
            delivery.harness,
            omission.reason,
            f" (needs {clause})" if clause else "",
        )
    if delivery.no_mcp_tools:
        logger.warning(
            "ACP: session on harness %r starts with NO MCP tools: %s (report=%s)",
            delivery.harness,
            delivery.summary(),
            delivery.as_dict(),
        )


def _delivery_modes() -> tuple[str, str]:
    """``(file_fed, wire_fed)`` from the descriptor module.

    Imported per call, not at module scope: ``kiro_crew.acp.__init__`` imports
    the ACP client, which imports THIS module, so a module-level import of
    anything under ``kiro_crew.acp`` would close a cycle — and it would pull the
    whole ACP client into any gateway-side process that imports this package.
    The vocabulary still has exactly one owner (the descriptor module); nothing
    here restates a mode as a literal.
    """
    from kiro_crew.agent_sdk.harness import MCP_DELIVERY_FILE_FED, MCP_DELIVERY_WIRE_FED

    return MCP_DELIVERY_FILE_FED, MCP_DELIVERY_WIRE_FED


def _wire_fed_mode() -> str:
    return _delivery_modes()[1]


def _is_broker_stub(entry: Mapping[str, Any]) -> bool:
    """True for an entry the rewriter produced (a pooled broker stub).

    Both markers, matching :func:`pooled_session_servers`: an overlay written by
    an older build carries the legacy one, and treating it as a plain server
    would append the stub-only ``--channel-id`` flag to nothing, or worse, send
    a real server's env where a stub's empty env was expected.
    """
    return bool(entry.get(_WRAPPER_MARKER) or entry.get(_WRAPPER_MARKER_LEGACY))


def _acp_pairs(raw: Any) -> list[dict[str, str]]:
    """Convert a JSON mapping to ACP's array-of-pairs form.

    ACP spells both an entry's environment and a remote entry's HTTP headers as
    ``[{"name": ..., "value": ...}]``, so one conversion serves both axes and
    neither can drift into the object form kiro-agent JSON uses.
    """
    if not isinstance(raw, dict):
        return []
    return [{"name": str(k), "value": str(v)} for k, v in raw.items()]


def _acp_env(raw: Any) -> list[dict[str, str]]:
    """Convert a kiro-agent-JSON ``env`` mapping to ACP's array-of-pairs form.

    Stub entries always carry an empty mapping, so this normally returns ``[]``.
    It is still a faithful conversion rather than a hardcoded empty list so a
    future caller that injects a non-stub entry cannot silently drop its env.
    """
    return _acp_pairs(raw)


def _acp_server_entry(
    name: str, entry: dict[str, Any], channel_id: str | None = None
) -> dict[str, Any] | None:
    """Shape one rewritten ``mcpServers`` entry into an ACP array element.

    Operator-set passthrough keys (``timeout``, ``type``, ``disabledTools``,
    ``autoApprove``, vendor keys) are preserved: kiro-cli tolerates them on the
    session-injected element, and dropping ``autoApprove`` in particular would
    re-prompt for tools the agent spec had already auto-approved.

    ``channel_id`` is APPENDED as ``--channel-id <value>`` rather than
    prepended: the overlay entry runs the interpreter, so ``args`` opens with
    ``-m kiro_crew.mcp_gateway.stub`` and anything inserted ahead of that would
    be eaten by the interpreter instead of the stub. argparse does not care
    about order. The channel value is known here, at the one place that runs
    per session, so the stub does not need to recover it by walking its
    ancestors' ``/proc/<pid>/environ`` from a bash launcher.
    """
    command = entry.get("command")
    if not isinstance(command, str) or not command:
        # A stub without a command cannot be launched; injecting it would
        # shadow the agent's working entry with a broken one. Skip instead,
        # leaving the spec's own server in place.
        return None
    args = [a if isinstance(a, str) else json.dumps(a, sort_keys=True, default=str)
            for a in (entry.get("args") or [])]
    if channel_id and "--channel-id" not in args:
        args.extend(["--channel-id", channel_id])
    shaped: dict[str, Any] = {
        k: v for k, v in entry.items() if k not in _ACP_RESERVED and k != "command"
    }
    shaped.update({
        "name": name,
        "command": command,
        "args": args,
        "env": _acp_env(entry.get("env")),
    })
    return shaped


def entry_transport(entry: Any) -> str:
    """The ACP transport ``entry`` needs, or ``""`` when it names none.

    A DECLARED ``type`` wins, because that is the only thing that distinguishes
    SSE from Streamable HTTP: both are url-based, and kiro-agent JSON marks the
    former with ``type: "sse"``. With no declaration, a ``command`` is stdio and
    a bare ``url`` is Streamable HTTP — the same reading
    ``mcp_discovery.McpServerInfo.is_remote`` gives.

    ``""`` is returned rather than a guess for an entry that declares neither, so
    the caller reports it by name instead of shaping it into an element the
    harness would reject.
    """
    if not isinstance(entry, Mapping):
        return ""
    declared = entry.get("type")
    if isinstance(declared, str):
        normalized = declared.strip().lower()
        if normalized in _TRANSPORTS:
            return normalized
    command = entry.get("command")
    if isinstance(command, str) and command:
        return TRANSPORT_STDIO
    url = entry.get("url")
    if isinstance(url, str) and url:
        return TRANSPORT_HTTP
    return ""


def _acp_remote_entry(
    name: str, entry: Mapping[str, Any], transport: str
) -> dict[str, Any] | None:
    """Shape one url-based server into an ACP HTTP/SSE array element.

    ``headers`` becomes ACP's array-of-pairs, mirroring ``env`` on the stdio
    element. Operator-set passthrough keys (the OAuth hints ``scopes`` /
    ``clientId``, ``disabledTools``, ``autoApprove``) are preserved for the same
    reason the stdio shaper preserves them: dropping ``autoApprove`` re-prompts
    for tools the spec had already approved, and the OAuth hints are the only
    copy of configuration a user wrote.

    ``None`` when there is no url to dial, which the caller reports by name — an
    element with a transport but no target would register a server that can
    never answer.
    """
    url = entry.get("url")
    if not isinstance(url, str) or not url:
        return None
    shaped: dict[str, Any] = {k: v for k, v in entry.items() if k not in _ACP_REMOTE_RESERVED}
    shaped.update(
        {
            "type": transport,
            "name": name,
            "url": url,
            "headers": _acp_pairs(entry.get("headers")),
        }
    )
    return shaped


def _load_overlay_for_agent(overlay_dir: Path, agent: str) -> dict[str, Any] | None:
    """Locate the rewritten overlay spec for *agent*, or ``None``.

    Package-installed agents are written to the overlay directory under a
    package-qualified filename (e.g. ``Pkg-gpu-dev.json``) while the session
    requests them by bare name (``gpu-dev``). A filename-only lookup therefore
    silently misses and disables pooling for every packaged agent. Match the
    bare filename first (fast path for unprefixed agents), then fall back to a
    filename-qualified overlay (``*<agent>.json``) whose parsed ``name`` equals
    *agent*. (#925)

    Fail-soft: an unreadable/malformed overlay yields ``None`` (unpooled), never
    an exception.
    """
    direct = overlay_dir / f"{agent}.json"
    try:
        return json.loads(direct.read_text(encoding="utf-8"))
    except FileNotFoundError:
        pass  # not emitted under the bare name — try a name-field match below
    except (OSError, ValueError):
        logger.warning("MCP-gateway: cannot read overlay spec %s", direct, exc_info=True)
        return None
    # Fallback: a package-installed agent's overlay keeps its package-qualified
    # source filename (e.g. ``Pkg-gpu-dev.json``) while the session requests it
    # by bare name. Restrict the scan to filenames that END with the agent name
    # so at most a handful of plausible candidates are read on the async
    # session-creation path — never the whole directory — then confirm each
    # against the authoritative ``name`` field so a coincidental filename suffix
    # can't mismatch.
    try:
        candidates = sorted(overlay_dir.glob(f"*{agent}.json"))
    except (OSError, ValueError):
        # ValueError: an agent name carrying glob metacharacters (e.g. ``*`` ->
        # ``**.json``) is an invalid pattern; fail soft to unpooled rather than
        # aborting session creation.
        return None
    for path in candidates:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("name") == agent:
            return data
    if candidates:
        # Distinguish "packaged agent not found" from "gateway disabled" / "agent
        # declared nothing poolable" for an operator debugging a low backend count.
        logger.debug(
            "MCP-gateway: no overlay with name %r among %d filename-qualified "
            "candidate(s) in %s; session runs unpooled",
            agent, len(candidates), overlay_dir,
        )
    return None


def pooled_session_servers(
    overlay_dir: str | Path | None,
    agent: str | None,
    channel_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return ACP ``session/new`` entries for *agent*'s broker stubs.

    ``overlay_dir`` is the rewriter's output directory (usually
    ``<config_dir>/mcp-gateway/agents/``); it is ``None`` when the shared
    gateway is disabled, which is the natural off switch — this returns ``[]``
    and the session runs entirely on the agent's own servers.

    ``channel_id`` reaches the stub so it can report the channel in its caller
    identity, which ``gatewayd`` stamps onto every forwarded ``tools/call`` as
    ``_meta.kirocrew.caller``. It is deliberately NOT a pool dimension (see
    :mod:`kiro_crew.mcp_gateway.pool`), so passing it does not split backends —
    two channels reaching the same agent still share one. It is passed here
    rather than baked into the overlay because the overlay is written once at
    gateway startup and is session-agnostic, while this function runs per
    session. ``None`` simply leaves the flag off and the channel unreported.

    Fail-soft by design: any unreadable or malformed overlay yields ``[]``, so a
    bad rewrite degrades to unpooled operation rather than breaking the spawn.
    """
    if not overlay_dir or not agent:
        return []
    spec = _load_overlay_for_agent(Path(overlay_dir), agent)
    if not isinstance(spec, dict):
        return []
    servers = spec.get("mcpServers")
    if not isinstance(servers, dict):
        return []
    out: list[dict[str, Any]] = []
    for name, entry in sorted(servers.items()):
        if not isinstance(entry, dict) or not (
            entry.get(_WRAPPER_MARKER) or entry.get(_WRAPPER_MARKER_LEGACY)
        ):
            # Not a broker stub: leave it to the agent spec entirely.
            continue
        shaped = _acp_server_entry(str(name), entry, channel_id)
        if shaped is not None:
            out.append(shaped)
    return out


def injection_server_names(
    overlay_dir: str | Path | None,
    agent: str | None,
) -> frozenset[str]:
    """Return the set of server names that WILL be injected for *agent*.

    Callers use this to detect an additive-injection regression: if a launched
    session reports MCP servers whose names overlap with this set, injection has
    become additive rather than overriding and every pooled server is running
    twice. See #927.

    This is deliberately cheap (one file read, no shaping) so it can be called
    as a post-launch health check without adding latency to the session path.
    """
    if not overlay_dir or not agent:
        return frozenset()
    spec = _load_overlay_for_agent(Path(overlay_dir), agent)
    if not isinstance(spec, dict):
        return frozenset()
    servers = spec.get("mcpServers")
    if not isinstance(servers, dict):
        return frozenset()
    return frozenset(
        name for name, entry in servers.items()
        if isinstance(entry, dict) and (
            entry.get(_WRAPPER_MARKER) or entry.get(_WRAPPER_MARKER_LEGACY)
        )
    )


def _project_spec_path(agent: str, project_dir: str | Path | None) -> Path | None:
    """The project-scoped spec declaring *agent*, or ``None``.

    Project scope is checked FIRST by :func:`_agent_spec_servers` because a
    project agent SHADOWS a same-named user-level one — ``agent_discovery`` states
    that rule and kiro-cli enforces it, resolving ``--agent`` against its cwd,
    which Kiro Crew sets to the session's project dir. Reading only the user-level
    directory therefore hands a wire-fed harness the WRONG server map for a
    project-scoped session: a map that converts cleanly and is reported as a
    successful delivery, so the substitution is silent.

    Only ``<project>/.kiro/agents/*.json`` contributes (``project_agent_files``
    with the legacy convention off), because those are the only project names
    kiro-cli can activate — and the name is the DECLARED one where a spec
    declares one, matching what kiro-cli accepts for ``--agent``.

    Two project specs declaring one name are AMBIGUOUS: kiro-cli iterates the
    directory itself, so which of them it would activate is undefined, and the
    first-sorted-stem answer here is a tie-break rather than a resolution. It is
    warned about rather than raised on — ``agent_discovery`` warns for the same
    decision, and refusing would blank the session's whole server map over a
    duplicate file, which is the worse of the two outcomes on a delivery path.
    """
    if not project_dir:
        return None
    from kiro_crew.agent_discovery import project_agent_files, project_agent_name

    matches = [
        spec for spec in project_agent_files(project_dir) if project_agent_name(spec) == agent
    ]
    if not matches:
        return None
    if len(matches) > 1:
        # Paths are repr'd: these filenames come from the user's checkout and the
        # line reaches the gateway log.
        logger.warning(
            "MCP delivery: %d project specs in %s declare the agent %r (%s); which one "
            "kiro-cli activates is undefined — delivering %r. Remove or rename one.",
            len(matches),
            project_dir,
            agent,
            ", ".join(repr(str(p)) for p in matches),
            str(matches[0]),
        )
    return matches[0]


def _warn_if_shadowing(agent: str, project_spec: Path) -> None:
    """Log the shadowing when *project_spec* wins over a user-level spec.

    Shadowing is the correct outcome (it is what kiro-cli itself does), but a
    silent one is not: the two specs can declare different servers, so the
    session's tools change with the cwd and nothing says so. Mirrors
    ``agent_discovery``'s notice for the same decision.

    Diagnostics only, so every failure is swallowed: the user-level resolver
    scans a shared directory and REFUSES an ambiguous name with ``ValueError``,
    and letting that abort a delivery the project scope already answered would
    turn a warning into a tool-less session.
    """
    try:
        from kiro_crew.agent import agent_spec_path

        shadowed = agent_spec_path(agent)
    except Exception:
        logger.debug(
            "MCP delivery: cannot tell whether %s shadows a user-level spec",
            project_spec,
            exc_info=True,
        )
        return
    if shadowed is None:
        return
    logger.warning(
        "MCP delivery: project agent spec %r shadows the user-level %r for agent %r; "
        "the session's MCP servers come from the project copy",
        str(project_spec),
        str(shadowed),
        agent,
    )


def _project_scope_servers(
    agent: str, project_dir: str | Path | None
) -> dict[str, Any] | None:
    """*agent*'s server map from the PROJECT scope, or ``None`` when it has none.

    ``None`` and ``{}`` are different answers and both are load-bearing: ``None``
    means the project declares no such agent, so the user-level scope decides;
    ``{}`` means a project spec declares the agent and lists no servers, which is
    a real (tool-less) map and must not fall back to a user-level one kiro-cli
    would never have activated.

    Fail-soft: a read failure answers ``None``, which degrades to the user-level
    scope rather than blanking a session's whole map over one bad file.
    """
    try:
        from kiro_crew.agent_discovery import _read_agent_spec

        project = _project_spec_path(agent, project_dir)
        if project is None:
            return None
        _warn_if_shadowing(agent, project)
        spec = _read_agent_spec(
            project, operation="mcp_gateway_session_servers", source="unknown"
        )
    except (OSError, ValueError, ImportError):
        logger.warning(
            "MCP delivery: cannot read the project agent spec for %r; falling back to "
            "the user-level scope",
            agent,
            exc_info=True,
        )
        return None
    servers = spec.get("mcpServers") if isinstance(spec, dict) else None
    return servers if isinstance(servers, dict) else {}


def _agent_spec_servers(
    agent: str, project_dir: str | Path | None = None
) -> tuple[dict[str, Any], str]:
    """``(mcpServers, scope)`` from *agent*'s own spec on disk, or ``({}, "")``.

    The authorized map's source when there is no rewriter overlay — which is the
    common case, since the shared gateway is opt-in (``mcp_gateway.enabled``
    defaults off). Without this a wire-fed harness would get an empty array on
    every ordinary install and never see a single tool.

    Resolved in the scope order kiro-cli itself uses: the project checkout first
    (see :func:`_project_spec_path`), then the user-level ``~/.kiro/agents``. The
    winning scope is returned rather than inferred by the caller, because the two
    produce indistinguishable maps.

    Deferred imports for the reason :func:`_delivery_modes` documents (the ACP
    package imports this module), and because the agent module is heavy. The
    read goes through ``agent_discovery._read_agent_spec``, which that module
    documents as the one reader for both agent scopes: it is size-capped and
    refuses AppleDouble sidecars, non-UTF-8 bytes, and a symlink resolving onto
    a sensitive path. The agents directory is user-writable and shared with other
    tools, so none of those are hypothetical.

    Fail-soft: any resolution or read failure yields ``{}``, and the caller then
    reports a tool-less session rather than failing session creation.
    """
    project = _project_scope_servers(agent, project_dir)
    if project is not None:
        return project, SPEC_SCOPE_PROJECT
    try:
        from kiro_crew.agent import agent_spec_path
        from kiro_crew.agent_discovery import _read_agent_spec

        path = agent_spec_path(agent)
        if path is None:
            return {}, ""
        spec = _read_agent_spec(
            path, operation="mcp_gateway_session_servers", source="unknown"
        )
        scope = SPEC_SCOPE_USER
    except (OSError, ValueError, ImportError):
        # ValueError: two specs declare this agent, so which one is live is
        # undefined (``agent_spec_path`` refuses rather than guessing).
        logger.warning(
            "MCP delivery: cannot resolve the agent spec for %r; session runs with "
            "no wire-fed MCP servers",
            agent,
            exc_info=True,
        )
        return {}, ""
    servers = spec.get("mcpServers") if isinstance(spec, dict) else None
    return (servers, scope) if isinstance(servers, dict) else ({}, scope)


def authorized_servers(
    overlay_dir: str | Path | None,
    agent: str | None,
    project_dir: str | Path | None = None,
) -> dict[str, Any]:
    """The full MCP server map *agent* is authorized to use, as stored.

    See :func:`authorized_servers_scoped`, which this delegates to; callers that
    do not report the supplying scope use this shorter form.
    """
    return authorized_servers_scoped(overlay_dir, agent, project_dir)[0]


def authorized_servers_scoped(
    overlay_dir: str | Path | None,
    agent: str | None,
    project_dir: str | Path | None = None,
) -> tuple[dict[str, Any], str]:
    """``(server map, scope)`` for *agent*, as stored.

    Scope precedence, and it is not simply "overlay wins":

    - **A project spec declaring *agent* decides which servers EXIST.** That is
      the resolution order kiro-cli applies to ``--agent`` itself, and Kiro Crew
      spawns the harness with the project as its cwd. The rewriter writes overlays
      from the user-level directory ONLY, so an overlay that won outright would
      deliver a wire-fed harness a rewrite of a spec its harness never activated —
      converted cleanly and reported as a successful delivery, which makes the
      substitution silent. The overlay still contributes its BROKER STUBS for names
      the two scopes share, so pooling stays in effect for a project-scoped session
      instead of being lost as the price of correct scoping.
    - **Otherwise the overlay wins**, as it always has: it is the user-level map
      with the poolable entries replaced by stubs, so preferring it is what keeps
      pooling in effect. Under wire-fed delivery the stubs are simply the entries
      sent — there is no same-name spec copy left for them to shadow, which is the
      degenerate case of the precedence rule the module docstring describes.
    - **Then the agent's own spec**, which is where the map lives when the shared
      gateway is off.

    What this map IS, precisely, because the delivery path relies on it: the
    entries the agent's own store lists, read rather than re-decided. For a spec
    Kiro Crew wrote to ``~/.kiro/agents`` that means the ceiling-filtered set
    (``agent._ceiling_filtered_spec`` did the filtering when the file was
    written). A project spec carries no such guarantee — nothing in Kiro Crew
    writes ``<project>/.kiro/agents``, so that file is whatever the checkout
    ships — and neither scope's file has the operator's ``disabled`` mutes
    applied, because a mute is stored ON the entry rather than by removing it.
    So this answers "what does the store list", and the delivery step is what
    honors the mute (:func:`wire_session_servers`).
    """
    if not agent:
        return {}, ""
    overlay_servers: dict[str, Any] = {}
    if overlay_dir:
        spec = _load_overlay_for_agent(Path(overlay_dir), agent)
        if isinstance(spec, dict):
            candidate = spec.get("mcpServers")
            if isinstance(candidate, dict):
                overlay_servers = candidate
    project_servers = _project_scope_servers(agent, project_dir)
    if project_servers is not None:
        return _with_broker_stubs(project_servers, overlay_servers), SPEC_SCOPE_PROJECT
    if overlay_servers:
        return overlay_servers, SPEC_SCOPE_OVERLAY
    return _agent_spec_servers(agent, project_dir)


def _with_broker_stubs(
    servers: Mapping[str, Any], overlay_servers: Mapping[str, Any]
) -> dict[str, Any]:
    """*servers* with each shared name replaced by the overlay's broker stub.

    Membership comes from ``servers`` and nothing else: a stub for a name that
    scope does not declare would deliver a server the active spec never listed,
    which is the additive-injection regression ``injection_server_names`` exists
    to detect. Only a real stub substitutes — a non-stub overlay entry is the
    user-level spec's own copy of the server, and preferring it over the active
    scope's is the substitution this precedence rule exists to prevent.
    """
    if not overlay_servers:
        return dict(servers)
    merged = dict(servers)
    for name, entry in overlay_servers.items():
        if name in merged and isinstance(entry, Mapping) and _is_broker_stub(entry):
            merged[name] = entry
    return merged


def wire_session_servers(
    servers: Mapping[str, Any],
    *,
    transports: McpTransports | None = None,
    harness: str = "",
    channel_id: str | None = None,
    scope: str = "",
) -> McpDelivery:
    """Convert an authorized server map to the ACP wire array for one session.

    Every member of ``servers`` is either emitted or reported: an entry is
    omitted when the operator muted it, when its transport was not advertised
    (stdio always is), when it names no transport at all, or when it carries
    nothing launchable. Nothing is dropped quietly, because a wire-fed harness
    has no other way to learn the server existed.

    The mute is honored HERE, and it has to be: ``disabled`` is a kiro-cli
    concept with no ACP field, so an entry carrying it would go out as an ignored
    vendor key and the harness would launch the server the operator switched off
    — with its ``env`` and ``headers``, and counted as delivered. The rewriter
    refuses the same entry for the same reason, but it may pass the flag through
    because kiro-cli re-reads it from the overlay; a wire-fed harness cannot.

    Truthiness, not ``is True``: over-omitting costs availability the operator
    can see and fix, while under-omitting silently un-mutes a server. Every
    other reader of this flag (``agent``, ``mcp_discovery``) reads it the same
    way, so a spelling one of them treats as muted is never delivered here.

    ``channel_id`` reaches broker stubs only. It is a stub argument (the stub
    reports the channel in its caller identity), so appending it to a real
    server's argv would hand that server a flag it does not understand.
    """
    allowed = transports or McpTransports()
    emitted: list[dict[str, Any]] = []
    omitted: list[OmittedServer] = []
    for raw_name, entry in sorted(servers.items()):
        name = str(raw_name)
        if not isinstance(entry, Mapping):
            omitted.append(OmittedServer(name, "", "entry is not an object"))
            continue
        if entry.get("disabled"):
            # Reported with the transport it WOULD have used, so the row reads as
            # a live server that is switched off rather than as a broken one.
            omitted.append(OmittedServer(name, entry_transport(entry), REASON_DISABLED))
            continue
        transport = entry_transport(entry)
        if not transport:
            omitted.append(
                OmittedServer(name, "", "entry declares neither a command nor a url")
            )
            continue
        if not allowed.allows(transport):
            omitted.append(
                OmittedServer(
                    name,
                    transport,
                    f"the harness advertised only {', '.join(allowed.advertised())}",
                )
            )
            continue
        if transport == TRANSPORT_STDIO:
            shaped = _acp_server_entry(
                name,
                dict(entry),
                channel_id if _is_broker_stub(entry) else None,
            )
        else:
            shaped = _acp_remote_entry(name, entry, transport)
        if shaped is None:
            omitted.append(
                OmittedServer(name, transport, f"no {transport} target to launch or dial")
            )
            continue
        emitted.append(shaped)
    return McpDelivery(
        harness=harness,
        mode=_wire_fed_mode(),
        servers=tuple(emitted),
        omitted=tuple(omitted),
        scope=scope,
    )


def delivery_servers(
    descriptor: "HarnessDescriptor",
    agent: str | None,
    channel_id: str | None = None,
    *,
    overlay_dir: str | Path | None = None,
    transports: McpTransports | None = None,
    project_dir: str | Path | None = None,
) -> McpDelivery:
    """The ``mcpServers`` array for one session, per its harness's delivery mode.

    File-fed: the pooled broker stubs, unchanged — the harness reads the rest
    from its own spec, and an empty array there is ordinary rather than a report.

    Wire-fed: the whole authorized map converted to the ACP wire shape, gated on
    the transports the harness advertised at initialize. ``transports`` defaults
    to stdio-only, which is what ACP guarantees; a caller that has not read the
    initialize response yet therefore under-sends rather than sending a URL to a
    harness that never claimed HTTP.

    ``project_dir`` is the session's working directory — the cwd the harness is
    spawned with. Passing it is what lets a project-scoped agent spec shadow a
    same-named user-level one, which is the resolution order kiro-cli applies to
    ``--agent`` itself; omitting it resolves user-level specs only, which is
    correct for a caller with no session context. The scope that won is recorded
    on the report either way.

    Blocking (it stats and reads files), so an event-loop caller routes it
    through ``asyncio.to_thread`` the way the pooled resolver already is.
    """
    file_fed, _wire_fed = _delivery_modes()
    if descriptor.mcp_delivery == file_fed:
        return McpDelivery(
            harness=descriptor.id,
            mode=file_fed,
            servers=tuple(pooled_session_servers(overlay_dir, agent, channel_id)),
            # The stubs exist only in the rewriter overlay; with no overlay dir
            # there is no scope to name, because nothing was read.
            scope=SPEC_SCOPE_OVERLAY if overlay_dir else "",
        )
    servers, scope = authorized_servers_scoped(overlay_dir, agent, project_dir)
    return wire_session_servers(
        servers,
        transports=transports,
        harness=descriptor.id,
        channel_id=channel_id,
        scope=scope,
    )
