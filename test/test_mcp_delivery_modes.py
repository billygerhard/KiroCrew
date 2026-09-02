"""Tests for per-harness MCP delivery: file-fed stubs vs wire-fed conversion.

The two modes answer different questions, and conflating them is the failure
these tests exist to prevent. A file-fed harness (kiro-cli) reads its servers
from its own agent spec, so ``session/new`` carries only the pooled broker
stubs and an empty array is ordinary. A wire-fed harness reads no file of ours,
so the array is the only channel it has: an empty one means the session has no
tools at all, and a server withheld for lack of an advertised transport has to
be named or it simply disappears.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from hypothesis import given
from hypothesis import strategies as st

from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.harness_descriptor import (
    MCP_DELIVERY_FILE_FED,
    MCP_DELIVERY_WIRE_FED,
    HarnessDescriptor,
)
from kiro_crew.acp.harness_registry import HARNESS_CLAUDE, HARNESS_KIRO
from kiro_crew.acp.harness_registry import registry as harness_registry
from kiro_crew.acp.types import ACP_BACKEND_CLAUDE, METHOD_SESSION_NEW
from kiro_crew.mcp_gateway.rewriter import _WRAPPER_MARKER, _WRAPPER_MARKER_LEGACY
from kiro_crew.mcp_gateway.session_servers import (
    REASON_DISABLED,
    SPEC_SCOPE_OVERLAY,
    SPEC_SCOPE_PROJECT,
    SPEC_SCOPE_USER,
    TRANSPORT_HTTP,
    TRANSPORT_SSE,
    TRANSPORT_STDIO,
    McpDelivery,
    McpTransports,
    authorized_servers,
    delivery_servers,
    entry_transport,
    wire_session_servers,
)

FILE_FED = HarnessDescriptor(
    id="filefed",
    executable="file-fed",
    argv=("{executable}",),
    mcp_delivery=MCP_DELIVERY_FILE_FED,
)
WIRE_FED = HarnessDescriptor(
    id="wirefed",
    executable="wire-fed",
    argv=("{executable}",),
    mcp_delivery=MCP_DELIVERY_WIRE_FED,
)

STDIO_ALL = McpTransports()
HTTP_OK = McpTransports(http=True)
SSE_OK = McpTransports(sse=True)


def _stub(**over):
    """A broker stub as the rewriter writes it into an overlay spec."""
    entry = {
        _WRAPPER_MARKER: True,
        "command": "/data/mcp-gateway/stubs/wrapper.sh",
        "args": ["--target-command=fetch", "--socket", "/data/gateway.sock"],
        "env": {},
        "autoApprove": ["fetch___fetch"],
    }
    entry.update(over)
    return entry


def _write_overlay(tmp_path: Path, agent: str, servers: dict) -> Path:
    overlay = tmp_path / "overlay"
    overlay.mkdir(parents=True, exist_ok=True)
    (overlay / f"{agent}.json").write_text(
        json.dumps({"name": agent, "mcpServers": servers}), encoding="utf-8"
    )
    return overlay


def _write_agent_spec(tmp_path: Path, monkeypatch, agent: str, servers: dict) -> Path:
    """Point ``agent_spec_path`` at a tmp agents dir holding one spec."""
    agents = tmp_path / "agents"
    agents.mkdir(parents=True, exist_ok=True)
    (agents / f"{agent}.json").write_text(
        json.dumps({"name": agent, "mcpServers": servers}), encoding="utf-8"
    )
    monkeypatch.setattr("kiro_crew.agent.kiro_agents_dir_path", lambda: agents)
    return agents


def _write_project_agent_spec(project: Path, agent: str, servers: dict) -> Path:
    """A project-scoped spec at ``<project>/.kiro/agents/<agent>.json``.

    The kiro-cli-native project location — the only one it resolves ``--agent``
    against, and therefore the only one whose names are dispatchable.
    """
    agents = project / ".kiro" / "agents"
    agents.mkdir(parents=True, exist_ok=True)
    spec = agents / f"{agent}.json"
    spec.write_text(json.dumps({"name": agent, "mcpServers": servers}), encoding="utf-8")
    return spec


# ── Transport advertisement ─────────────────────────────────────────────────


def test_stdio_needs_no_advertisement():
    """ACP requires every conformant agent to accept a stdio server."""
    assert STDIO_ALL.allows(TRANSPORT_STDIO)
    assert STDIO_ALL.advertised() == (TRANSPORT_STDIO,)


def test_http_and_sse_are_separate_opt_ins():
    assert not STDIO_ALL.allows(TRANSPORT_HTTP)
    assert not STDIO_ALL.allows(TRANSPORT_SSE)
    # Advertising one must not confer the other: they are distinct capability
    # flags, and a harness that streams SSE need not speak Streamable HTTP.
    assert HTTP_OK.allows(TRANSPORT_HTTP) and not HTTP_OK.allows(TRANSPORT_SSE)
    assert SSE_OK.allows(TRANSPORT_SSE) and not SSE_OK.allows(TRANSPORT_HTTP)


@pytest.mark.parametrize(
    "capabilities",
    [
        None,
        {},
        {"loadSession": True},
        {"mcpCapabilities": None},
        {"mcpCapabilities": []},
        # A truthy STRING must not grant a transport — the same refusal the
        # descriptor parser applies to a non-bool capability value.
        {"mcpCapabilities": {"http": "true", "sse": 1}},
    ],
)
def test_unadvertised_or_malformed_capabilities_mean_stdio_only(capabilities):
    assert McpTransports.from_agent_capabilities(capabilities) == McpTransports()


def test_advertised_capabilities_are_read_from_the_initialize_response():
    transports = McpTransports.from_agent_capabilities(
        {"loadSession": True, "mcpCapabilities": {"http": True, "sse": True}}
    )
    assert transports == McpTransports(http=True, sse=True)
    assert transports.advertised() == (TRANSPORT_STDIO, TRANSPORT_HTTP, TRANSPORT_SSE)


# ── Transport classification ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "entry,expected",
    [
        ({"command": "srv"}, TRANSPORT_STDIO),
        ({"url": "https://example.test/mcp"}, TRANSPORT_HTTP),
        ({"url": "https://example.test/mcp", "type": "sse"}, TRANSPORT_SSE),
        ({"url": "https://example.test/mcp", "type": "SSE"}, TRANSPORT_SSE),
        # A declared type wins: it is the only thing distinguishing SSE from
        # Streamable HTTP, since both are url-based.
        ({"url": "https://example.test/mcp", "type": "http"}, TRANSPORT_HTTP),
        ({"command": "srv", "type": "stdio"}, TRANSPORT_STDIO),
        ({"command": "", "url": ""}, ""),
        ({"note": "neither"}, ""),
        ({"type": "webtransport", "url": "https://x.test"}, TRANSPORT_HTTP),
        ("not-an-object", ""),
    ],
)
def test_entry_transport_classification(entry, expected):
    assert entry_transport(entry) == expected


# ── File-fed delivery ───────────────────────────────────────────────────────


def test_file_fed_delivers_only_broker_stubs(tmp_path):
    overlay = _write_overlay(
        tmp_path,
        "kirocrew",
        {"pooled": _stub(), "private": {"command": "own", "env": {"TOKEN": "s3cret"}}},
    )
    delivery = delivery_servers(FILE_FED, "kirocrew", "chan-1", overlay_dir=overlay)

    assert delivery.mode == MCP_DELIVERY_FILE_FED
    assert [e["name"] for e in delivery.servers] == ["pooled"]
    # The non-poolable server stays in the spec, so its env never leaves the
    # file it was declared in.
    assert delivery.omitted == ()
    assert "--channel-id" in delivery.servers[0]["args"]


def test_file_fed_empty_array_is_not_a_no_tools_report(tmp_path):
    """kiro-cli with the shared gateway off sends [] and still has every tool."""
    delivery = delivery_servers(FILE_FED, "kirocrew", None, overlay_dir=None)
    assert delivery.servers == ()
    assert delivery.no_mcp_tools is False


# ── Wire-fed delivery ───────────────────────────────────────────────────────


def test_wire_fed_delivers_the_whole_authorized_map(tmp_path):
    overlay = _write_overlay(
        tmp_path,
        "kirocrew",
        {
            "pooled": _stub(),
            "private": {"command": "own", "args": ["--serve"], "env": {"TOKEN": "s3cret"}},
        },
    )
    delivery = delivery_servers(WIRE_FED, "kirocrew", "chan-1", overlay_dir=overlay)

    assert delivery.mode == MCP_DELIVERY_WIRE_FED
    by_name = {e["name"]: e for e in delivery.servers}
    assert sorted(by_name) == ["pooled", "private"]
    # env converted to ACP's array-of-pairs, faithfully: a wire-fed harness
    # cannot start the server without it.
    assert by_name["private"]["env"] == [{"name": "TOKEN", "value": "s3cret"}]
    assert by_name["private"]["args"] == ["--serve"]
    assert delivery.no_mcp_tools is False


def test_channel_id_reaches_broker_stubs_only(tmp_path):
    """``--channel-id`` is a stub argument; a real server would not know it."""
    overlay = _write_overlay(
        tmp_path, "kirocrew", {"pooled": _stub(), "private": {"command": "own", "args": []}}
    )
    delivery = delivery_servers(WIRE_FED, "kirocrew", "chan-1", overlay_dir=overlay)
    by_name = {e["name"]: e for e in delivery.servers}
    assert "--channel-id" in by_name["pooled"]["args"]
    assert by_name["private"]["args"] == []


def test_legacy_marked_stub_still_counts_as_a_stub(tmp_path):
    overlay = _write_overlay(
        tmp_path,
        "kirocrew",
        {"pooled": _stub(**{_WRAPPER_MARKER: False, _WRAPPER_MARKER_LEGACY: True})},
    )
    delivery = delivery_servers(WIRE_FED, "kirocrew", "chan-9", overlay_dir=overlay)
    assert "--channel-id" in delivery.servers[0]["args"]


def test_wire_fed_reads_the_agent_spec_when_the_shared_gateway_is_off(tmp_path, monkeypatch):
    """The overlay is optional; the agent spec is where the map normally lives."""
    _write_agent_spec(tmp_path, monkeypatch, "kirocrew", {"own": {"command": "srv"}})
    delivery = delivery_servers(WIRE_FED, "kirocrew", None, overlay_dir=None)
    assert [e["name"] for e in delivery.servers] == ["own"]


def test_wire_fed_falls_back_to_the_spec_when_no_overlay_exists_for_the_agent(
    tmp_path, monkeypatch
):
    """A gateway that rewrote OTHER agents must not blank this one's servers."""
    overlay = _write_overlay(tmp_path, "other", {"pooled": _stub()})
    _write_agent_spec(tmp_path, monkeypatch, "kirocrew", {"own": {"command": "srv"}})
    delivery = delivery_servers(WIRE_FED, "kirocrew", None, overlay_dir=overlay)
    assert [e["name"] for e in delivery.servers] == ["own"]


def test_remote_server_is_omitted_by_name_when_http_was_not_advertised():
    delivery = wire_session_servers(
        {"remote": {"url": "https://example.test/mcp", "headers": {"Authorization": "t"}}},
        transports=STDIO_ALL,
        harness="wirefed",
    )
    assert delivery.servers == ()
    assert [(o.name, o.transport) for o in delivery.omitted] == [("remote", TRANSPORT_HTTP)]
    assert "stdio" in delivery.omitted[0].reason
    # An omission-only conversion is still a tool-less session and says so.
    assert delivery.no_mcp_tools is True


def test_remote_server_is_delivered_in_acp_shape_once_advertised():
    delivery = wire_session_servers(
        {
            "remote": {
                "url": "https://example.test/mcp",
                "headers": {"Authorization": "tok"},
                "scopes": ["read"],
            }
        },
        transports=HTTP_OK,
        harness="wirefed",
    )
    assert delivery.omitted == ()
    entry = delivery.servers[0]
    assert entry["type"] == TRANSPORT_HTTP
    assert entry["name"] == "remote"
    assert entry["url"] == "https://example.test/mcp"
    assert entry["headers"] == [{"name": "Authorization", "value": "tok"}]
    # Operator-set passthrough keys survive, as they do on the stdio element.
    assert entry["scopes"] == ["read"]


def test_sse_entry_needs_the_sse_capability_not_the_http_one():
    servers = {"stream": {"url": "https://example.test/sse", "type": "sse"}}
    assert wire_session_servers(servers, transports=HTTP_OK).servers == ()
    assert wire_session_servers(servers, transports=SSE_OK).servers != ()


def test_unclassifiable_and_malformed_entries_are_reported_not_dropped():
    delivery = wire_session_servers(
        {
            "nothing": {"note": "no command, no url"},
            "garbage": "not-an-object",
            "typed-but-empty": {"type": "http"},
        },
        transports=HTTP_OK,
    )
    assert delivery.servers == ()
    assert {o.name for o in delivery.omitted} == {"nothing", "garbage", "typed-but-empty"}


def test_stdio_entry_without_a_command_is_reported():
    """A stub whose command went missing must not be silently swallowed."""
    delivery = wire_session_servers(
        {"broken": {"type": "stdio", "args": ["--x"]}}, transports=STDIO_ALL
    )
    assert delivery.servers == ()
    assert delivery.omitted[0].name == "broken"


def test_empty_wire_fed_conversion_reports_a_structured_no_tools_record():
    delivery = wire_session_servers({}, transports=STDIO_ALL, harness="wirefed")
    assert delivery.no_mcp_tools is True
    assert delivery.as_dict() == {
        "harness": "wirefed",
        "delivery": MCP_DELIVERY_WIRE_FED,
        "scope": "",
        "served": [],
        "omitted": [],
        "noMcpTools": True,
    }


def test_report_names_omitted_servers_without_their_credentials():
    delivery = wire_session_servers(
        {"remote": {"url": "https://user:pw@example.test/mcp", "headers": {"A": "tok"}}},
        transports=STDIO_ALL,
        harness="wirefed",
    )
    blob = json.dumps(delivery.as_dict()) + delivery.summary()
    assert "remote" in blob
    assert "tok" not in blob and "pw@" not in blob


def test_authorized_servers_is_empty_without_an_agent():
    assert authorized_servers(None, None) == {}
    assert authorized_servers(None, "") == {}


# ── The operator's mute ─────────────────────────────────────────────────────
#
# ``disabled`` is how a mute is stored: ``POST /api/mcp/toggle enabled:false``
# writes it into the Kiro-global ``~/.kiro/settings/mcp.json``, and
# ``rebuild_agent_config``'s merge is what carries it onto the agent-spec entry
# this path reads. It is a kiro-cli concept: ACP has no field for it. A file-fed
# harness re-reads the flag from its own spec; a wire-fed one never sees it, so
# an entry passed through would be launched — with its credentials — and counted
# as delivered.


def test_a_disabled_stdio_entry_is_omitted_with_that_reason():
    delivery = wire_session_servers(
        {"muted": {"command": "srv", "disabled": True}}, transports=STDIO_ALL
    )
    assert delivery.servers == ()
    assert [(o.name, o.reason) for o in delivery.omitted] == [("muted", REASON_DISABLED)]


def test_a_disabled_url_entry_is_omitted_with_that_reason():
    """Advertised transport and all: the mute outranks a deliverable entry."""
    delivery = wire_session_servers(
        {"muted": {"url": "https://example.test/mcp", "disabled": True}}, transports=HTTP_OK
    )
    assert delivery.servers == ()
    assert [(o.name, o.reason) for o in delivery.omitted] == [("muted", REASON_DISABLED)]


def test_a_disabled_entry_puts_no_credential_on_the_wire():
    """The wire array is the leak surface: an omitted entry must not shape at all.

    A muted entry's ``env`` and ``headers`` still hold tokens, and a passthrough
    would put them in the ``session/new`` frame for a server the operator
    switched off — the credential exposure without even the tool.
    """
    delivery = wire_session_servers(
        {
            "muted-stdio": {"command": "srv", "env": {"TOKEN": "stdio-secret"}, "disabled": True},
            "muted-remote": {
                "url": "https://example.test/mcp",
                "headers": {"Authorization": "remote-secret"},
                "disabled": True,
            },
        },
        transports=HTTP_OK,
        harness="wirefed",
    )
    wire = json.dumps(delivery.servers) + json.dumps(delivery.as_dict()) + delivery.summary()
    assert delivery.servers == ()
    assert "stdio-secret" not in wire and "remote-secret" not in wire
    # The names still surface, which is what makes the mute diagnosable.
    assert {o.name for o in delivery.omitted} == {"muted-stdio", "muted-remote"}


def test_a_live_sibling_is_still_delivered_beside_a_muted_entry():
    """The mute is per entry: one muted server does not blank the session."""
    delivery = wire_session_servers(
        {"live": {"command": "srv"}, "muted": {"command": "srv", "disabled": True}},
        transports=STDIO_ALL,
    )
    assert [e["name"] for e in delivery.servers] == ["live"]
    assert [o.name for o in delivery.omitted] == ["muted"]


def test_a_falsy_disabled_flag_leaves_the_entry_deliverable():
    """``disabled: false`` is the shape a re-enabled server is written back as."""
    delivery = wire_session_servers(
        {"live": {"command": "srv", "disabled": False}}, transports=STDIO_ALL
    )
    assert [e["name"] for e in delivery.servers] == ["live"]
    assert delivery.omitted == ()


def test_the_summary_names_a_mute_by_its_reason_rather_than_a_transport():
    """The mute is the one omission the transport clause misdirects on.

    A muted stdio server "needs stdio" is true and useless: it reads as a
    harness that could not accept the entry. The reason is the actionable half,
    and a transport refusal beside it still names the transport.
    """
    delivery = wire_session_servers(
        {
            "muted": {"command": "srv", "disabled": True},
            "remote": {"url": "https://example.test/mcp"},
        },
        transports=STDIO_ALL,
        harness="wirefed",
    )

    summary = delivery.summary()

    assert f"'muted' ({REASON_DISABLED})" in summary
    assert f"'remote' needs {TRANSPORT_HTTP}" in summary


def test_a_muted_server_in_the_agent_spec_never_reaches_a_wire_fed_harness(tmp_path, monkeypatch):
    """End to end from the stored spec, which is where a mute actually lives.

    ``agent.rebuild_agent_config`` keeps a muted entry IN ``mcpServers`` (it
    withdraws the tool refs instead of deleting the server), so the flag is what
    the delivery path reads — there is no earlier filter to rely on.
    """
    _write_agent_spec(
        tmp_path,
        monkeypatch,
        "kirocrew",
        {
            "live": {"command": "srv"},
            "muted": {"command": "srv", "env": {"TOKEN": "spec-secret"}, "disabled": True},
        },
    )

    delivery = delivery_servers(WIRE_FED, "kirocrew", None)

    assert [e["name"] for e in delivery.servers] == ["live"]
    assert [(o.name, o.reason) for o in delivery.omitted] == [("muted", REASON_DISABLED)]
    assert "spec-secret" not in json.dumps(delivery.servers)


# ── Agent scope: a project spec shadows the user-level one ──────────────────


def test_a_project_scoped_spec_shadows_the_user_level_one(tmp_path, monkeypatch):
    """kiro-cli resolves ``--agent`` against its cwd first, so delivery must too.

    Kiro Crew spawns the harness with the session's project dir as that cwd, and
    ``agent_discovery`` documents the shadowing as the resolution rule. Reading
    only ``~/.kiro/agents`` hands a wire-fed harness the wrong map — and the wrong
    map converts cleanly and is reported as a successful delivery, so nothing
    downstream can tell it apart from the right one.
    """
    project = tmp_path / "checkout"
    _write_agent_spec(tmp_path, monkeypatch, "kirocrew", {"global-only": {"command": "srv"}})
    _write_project_agent_spec(project, "kirocrew", {"project-only": {"command": "srv"}})

    delivery = delivery_servers(WIRE_FED, "kirocrew", None, project_dir=project)

    assert [e["name"] for e in delivery.servers] == ["project-only"]


def test_an_agent_the_project_does_not_declare_still_resolves_user_level(tmp_path, monkeypatch):
    """Shadowing is per agent NAME, not per session.

    A project that declares one agent must not blank the servers of every other
    agent a session on that project might run.
    """
    project = tmp_path / "checkout"
    _write_agent_spec(tmp_path, monkeypatch, "kirocrew", {"global-only": {"command": "srv"}})
    _write_project_agent_spec(project, "other", {"project-only": {"command": "srv"}})

    delivery = delivery_servers(WIRE_FED, "kirocrew", None, project_dir=project)

    assert [e["name"] for e in delivery.servers] == ["global-only"]


def test_a_project_agent_is_matched_on_its_declared_name(tmp_path):
    """The declared ``name`` wins over the filename, as it does for ``--agent``."""
    project = tmp_path / "checkout"
    agents = project / ".kiro" / "agents"
    agents.mkdir(parents=True)
    (agents / "team-spec.json").write_text(
        json.dumps({"name": "reviewer", "mcpServers": {"project-only": {"command": "srv"}}}),
        encoding="utf-8",
    )

    delivery = delivery_servers(WIRE_FED, "reviewer", None, project_dir=project)

    assert [e["name"] for e in delivery.servers] == ["project-only"]


def test_omitting_the_project_dir_resolves_user_level_only(tmp_path, monkeypatch):
    """A caller with no session context (and so no project) keeps today's answer."""
    project = tmp_path / "checkout"
    _write_agent_spec(tmp_path, monkeypatch, "kirocrew", {"global-only": {"command": "srv"}})
    _write_project_agent_spec(project, "kirocrew", {"project-only": {"command": "srv"}})

    delivery = delivery_servers(WIRE_FED, "kirocrew", None)

    assert [e["name"] for e in delivery.servers] == ["global-only"]


def test_a_project_spec_decides_membership_and_the_overlay_lends_its_stubs(tmp_path, monkeypatch):
    """Scope decides which servers EXIST; the overlay contributes only stubs.

    Overlays are written only from the user-level ``~/.kiro/agents``, so an overlay
    that won outright would deliver a wire-fed harness a rewrite of a spec its
    harness never activated — converted cleanly and reported as a successful
    delivery, which is what would make the substitution silent. Membership
    therefore comes from the scope kiro-cli would actually resolve ``--agent``
    against, while a name BOTH scopes declare still takes the overlay's broker stub
    so pooling is not the price of correct scoping.
    """
    project = tmp_path / "checkout"
    overlay = _write_overlay(tmp_path, "kirocrew", {"pooled": _stub()})
    _write_project_agent_spec(project, "kirocrew", {"project-only": {"command": "srv"}})

    delivery = delivery_servers(
        WIRE_FED, "kirocrew", "chan-1", overlay_dir=overlay, project_dir=project
    )

    assert [e["name"] for e in delivery.servers] == ["project-only"]
    assert delivery.scope == "project"


def test_a_shared_name_in_a_project_scope_keeps_its_broker_stub(tmp_path, monkeypatch):
    """Pooling survives the scoping fix for every name the two scopes share."""
    project = tmp_path / "checkout"
    overlay = _write_overlay(tmp_path, "kirocrew", {"pooled": _stub()})
    _write_project_agent_spec(project, "kirocrew", {"pooled": {"command": "unpooled-srv"}})

    delivery = delivery_servers(
        WIRE_FED, "kirocrew", "chan-1", overlay_dir=overlay, project_dir=project
    )

    assert [e["name"] for e in delivery.servers] == ["pooled"]
    # The STUB's command, not the project spec's own copy: the entry is brokered,
    # so one backend is shared instead of a second copy being spawned.
    assert delivery.servers[0]["command"] != "unpooled-srv"


def test_a_sensitive_project_dir_contributes_no_project_scope(tmp_path, monkeypatch):
    """The project dir arrives from a session field, so the deny check must hold.

    ``project_agent_files`` refuses a sensitive root before touching the
    filesystem; delivery inherits that rather than re-implementing it, and falls
    back to the user-level spec instead of failing the session.
    """
    project = tmp_path / "checkout"
    _write_agent_spec(tmp_path, monkeypatch, "kirocrew", {"global-only": {"command": "srv"}})
    _write_project_agent_spec(project, "kirocrew", {"project-only": {"command": "srv"}})
    # Scoped to the project root: ``_read_agent_spec`` consults the same predicate
    # for the file it opens, so a blanket True would block the user-level read too
    # and the fallback under test would never be reached.
    monkeypatch.setattr(
        "kiro_crew.agent_discovery.is_sensitive_path", lambda path: str(path) == str(project)
    )

    delivery = delivery_servers(WIRE_FED, "kirocrew", None, project_dir=project)

    assert [e["name"] for e in delivery.servers] == ["global-only"]


def test_authorized_servers_survives_an_unreadable_spec(tmp_path, monkeypatch):
    agents = tmp_path / "agents"
    agents.mkdir()
    (agents / "kirocrew.json").write_text("{not json", encoding="utf-8")
    monkeypatch.setattr("kiro_crew.agent.kiro_agents_dir_path", lambda: agents)
    assert authorized_servers(None, "kirocrew") == {}


# ── Which scope supplied the map ────────────────────────────────────────────
#
# Two scopes can declare the same agent name, and the wire entries they produce
# are indistinguishable — so the scope is recorded on the report, and a scope
# winning over another is logged rather than merely happening.


def test_the_report_names_the_scope_that_supplied_the_map(tmp_path, monkeypatch):
    project = tmp_path / "checkout"
    _write_agent_spec(tmp_path, monkeypatch, "kirocrew", {"global-only": {"command": "srv"}})
    _write_project_agent_spec(project, "kirocrew", {"project-only": {"command": "srv"}})
    overlay = _write_overlay(tmp_path, "kirocrew", {"pooled": _stub()})

    assert delivery_servers(WIRE_FED, "kirocrew", None).scope == SPEC_SCOPE_USER
    assert (
        delivery_servers(WIRE_FED, "kirocrew", None, project_dir=project).scope
        == SPEC_SCOPE_PROJECT
    )
    scoped = delivery_servers(WIRE_FED, "kirocrew", None, overlay_dir=overlay)
    assert scoped.scope == SPEC_SCOPE_OVERLAY
    assert scoped.as_dict()["scope"] == SPEC_SCOPE_OVERLAY


def test_no_scope_is_claimed_when_nothing_supplied_a_map(tmp_path, monkeypatch):
    """An agent no scope declares reports no scope, not a scope it never read."""
    monkeypatch.setattr("kiro_crew.agent.kiro_agents_dir_path", lambda: tmp_path / "absent")
    assert delivery_servers(WIRE_FED, "nobody", None).scope == ""


def test_a_project_spec_shadowing_a_user_level_one_is_logged(tmp_path, monkeypatch, caplog):
    """Shadowing is correct; silent shadowing is not.

    The two specs can declare different servers, so the session's tools change
    with the cwd. Both paths are named because the fix is to edit one of them.
    """
    project = tmp_path / "checkout"
    _write_agent_spec(tmp_path, monkeypatch, "kirocrew", {"global-only": {"command": "srv"}})
    winner = _write_project_agent_spec(project, "kirocrew", {"project-only": {"command": "srv"}})

    with caplog.at_level(logging.WARNING, logger="kiro_crew.mcp_gateway.session_servers"):
        delivery = delivery_servers(WIRE_FED, "kirocrew", None, project_dir=project)

    assert [e["name"] for e in delivery.servers] == ["project-only"]
    assert "shadows" in caplog.text
    # The message logs the paths with ``%r``; on Windows repr DOUBLES the
    # backslashes in a path, so ``str(winner)`` (single backslash) is not a
    # substring of the formatted ``caplog.text``. Match against the record's raw
    # args instead — the exact strings the code passed to the logger.
    shadow_args = next(r.args for r in caplog.records if r.args and "shadows" in r.getMessage())
    assert str(winner) in shadow_args
    assert str(tmp_path / "agents" / "kirocrew.json") in shadow_args


def test_shadowing_nothing_logs_nothing(tmp_path, monkeypatch, caplog):
    """A project-only agent is the ordinary case and must stay quiet."""
    project = tmp_path / "checkout"
    monkeypatch.setattr("kiro_crew.agent.kiro_agents_dir_path", lambda: tmp_path / "absent")
    _write_project_agent_spec(project, "kirocrew", {"project-only": {"command": "srv"}})

    with caplog.at_level(logging.WARNING, logger="kiro_crew.mcp_gateway.session_servers"):
        delivery = delivery_servers(WIRE_FED, "kirocrew", None, project_dir=project)

    assert [e["name"] for e in delivery.servers] == ["project-only"]
    assert "shadows" not in caplog.text


def test_two_project_specs_declaring_one_name_are_warned_about(tmp_path, caplog):
    """First-sorted-stem is a tie-break, not a resolution — so it is announced.

    kiro-cli iterates the directory itself, so which spec it would activate is
    undefined. Delivery still proceeds: blanking a session's whole server map
    over a duplicated file is the worse of the two outcomes here.
    """
    project = tmp_path / "checkout"
    agents = project / ".kiro" / "agents"
    agents.mkdir(parents=True)
    (agents / "a-first.json").write_text(
        json.dumps({"name": "reviewer", "mcpServers": {"from-a": {"command": "srv"}}}),
        encoding="utf-8",
    )
    (agents / "b-second.json").write_text(
        json.dumps({"name": "reviewer", "mcpServers": {"from-b": {"command": "srv"}}}),
        encoding="utf-8",
    )

    with caplog.at_level(logging.WARNING, logger="kiro_crew.mcp_gateway.session_servers"):
        delivery = delivery_servers(WIRE_FED, "reviewer", None, project_dir=project)

    assert [e["name"] for e in delivery.servers] == ["from-a"]
    assert "undefined" in caplog.text
    # The paths are repr'd into the message (``repr(str(p))``), which DOUBLES
    # backslashes on Windows, so ``str(path)`` (single backslash) is not a
    # substring of the raw text. Collapse the repr escaping before matching so
    # the assertion holds on both separators.
    logged = caplog.text.replace("\\\\", "\\")
    assert str(agents / "a-first.json") in logged
    assert str(agents / "b-second.json") in logged


def test_an_ambiguous_user_level_scope_does_not_defeat_a_project_answer(tmp_path, monkeypatch):
    """The shadow notice is diagnostics, so its own failure changes no delivery.

    ``agent_spec_path`` REFUSES an ambiguous user-level name with ``ValueError``.
    Letting that propagate out of the notice would turn a duplicate file in a
    directory the session never read into a tool-less session.
    """
    project = tmp_path / "checkout"
    agents = tmp_path / "agents"
    agents.mkdir()
    for stem in ("one", "two"):
        (agents / f"{stem}.json").write_text(
            json.dumps({"name": "kirocrew", "mcpServers": {}}), encoding="utf-8"
        )
    monkeypatch.setattr("kiro_crew.agent.kiro_agents_dir_path", lambda: agents)
    _write_project_agent_spec(project, "kirocrew", {"project-only": {"command": "srv"}})

    delivery = delivery_servers(WIRE_FED, "kirocrew", None, project_dir=project)

    assert [e["name"] for e in delivery.servers] == ["project-only"]
    assert delivery.scope == SPEC_SCOPE_PROJECT


# ── Property 3: faithful conversion, transport safety, omission reporting ───

_NAMES = st.text(
    alphabet=st.characters(min_codepoint=97, max_codepoint=122), min_size=1, max_size=6
)
_VALUES = st.text(alphabet=st.characters(exclude_categories=("Cs",)), max_size=6)
_PAIRS = st.dictionaries(_NAMES, _VALUES, max_size=3)

# The operator's mute belongs in the generated space, because it is an omission
# class the transport axis cannot reach: a muted entry is deliverable in every
# other respect. ``_MUTE_ABSENT`` leaves the key off entirely, which is the
# ordinary shape and is drawn half the time so the faithful-conversion
# assertions keep seeing deliverable entries; ``False`` is what a re-enabled
# server is written back as; and the truthy non-``True`` spellings are here
# because the flag is read truthily — a ``1`` or a ``"yes"`` that slipped through
# would silently un-mute a server.
_MUTE_ABSENT = object()
_MUTE_FLAGS = st.one_of(st.just(_MUTE_ABSENT), st.sampled_from([True, False, 1, 0, "yes", ""]))


def _with_mute(entry: dict, flag: object) -> dict:
    """*entry* carrying the generated ``disabled`` flag, or without the key at all."""
    return entry if flag is _MUTE_ABSENT else {**entry, "disabled": flag}


_STDIO_ENTRIES = st.builds(
    lambda command, args, env, mute: _with_mute(
        {"command": command, "args": args, "env": env}, mute
    ),
    command=st.text(min_size=1, max_size=8),
    args=st.lists(st.text(max_size=6), max_size=3),
    env=_PAIRS,
    mute=_MUTE_FLAGS,
)
_REMOTE_ENTRIES = st.builds(
    lambda url, headers, kind, mute: _with_mute(
        {"url": url, "headers": headers, "type": kind}, mute
    ),
    url=st.text(min_size=1, max_size=10),
    headers=_PAIRS,
    kind=st.sampled_from([TRANSPORT_HTTP, TRANSPORT_SSE]),
    mute=_MUTE_FLAGS,
)
_UNTYPED_REMOTE = st.builds(lambda url: {"url": url}, url=st.text(min_size=1, max_size=10))
_JUNK_ENTRIES = st.sampled_from([{}, {"note": "x"}, {"type": "http"}, "string", 7, None])
_ENTRIES = st.one_of(_STDIO_ENTRIES, _REMOTE_ENTRIES, _UNTYPED_REMOTE, _JUNK_ENTRIES)


@given(
    servers=st.dictionaries(_NAMES, _ENTRIES, max_size=5),
    http=st.booleans(),
    sse=st.booleans(),
)
def test_property_wire_conversion_is_faithful_and_transport_safe(servers, http, sse):
    transports = McpTransports(http=http, sse=sse)
    # T ∪ {stdio}, computed from the advertised flags rather than read back
    # through ``McpTransports.allows``: an oracle that reuses the predicate under
    # test cannot observe that predicate widening.
    permitted = {TRANSPORT_STDIO}
    if http:
        permitted.add(TRANSPORT_HTTP)
    if sse:
        permitted.add(TRANSPORT_SSE)
    delivery = wire_session_servers(servers, transports=transports, harness="h")
    emitted = {entry["name"]: entry for entry in delivery.servers}
    omitted = {o.name for o in delivery.omitted}
    reasons = {o.name: o.reason for o in delivery.omitted}

    # Every authorized server is either delivered or reported, never both and
    # never neither: a member that is silently absent is the failure mode a
    # wire-fed harness cannot detect for itself.
    assert set(emitted) | omitted == set(servers)
    assert not (set(emitted) & omitted)
    assert len(delivery.servers) == len(emitted), "a name was emitted twice"
    # The operator's mute partitions with the rest: a truthy ``disabled`` outranks
    # an otherwise deliverable entry on an advertised transport, and it is
    # reported as the mute rather than as a transport problem — including the
    # truthy spellings (``1``, ``"yes"``) the flag is read for everywhere else.
    for name, entry in servers.items():
        if isinstance(entry, dict) and entry.get("disabled"):
            assert name not in emitted
            assert reasons[name] == REASON_DISABLED
    # Transport safety, stated over the INPUT as well as the output: a member
    # needing an unadvertised transport is never among the delivered entries.
    for name, entry in servers.items():
        if entry_transport(entry) not in permitted:
            assert name not in emitted

    for name, entry in servers.items():
        if name not in emitted:
            continue
        wire = emitted[name]
        transport = entry_transport(entry)
        # Transport safety: nothing is sent over a transport outside T ∪ {stdio}.
        assert transport in permitted
        assert wire["name"] == name
        if transport == TRANSPORT_STDIO:
            assert wire["command"] == entry["command"]
            assert wire["args"] == list(entry.get("args") or [])
            assert wire["env"] == [{"name": k, "value": v} for k, v in entry.get("env", {}).items()]
        else:
            assert wire["type"] == transport
            assert wire["url"] == entry["url"]
            assert wire["headers"] == [
                {"name": k, "value": v} for k, v in entry.get("headers", {}).items()
            ]

    for omission in delivery.omitted:
        assert omission.name in servers
        assert omission.reason
        # A mute is not a capability gap, so it reports no transport clause;
        # every other omission names what the entry would have needed, which is
        # the whole fix for the reader.
        if omission.reason == REASON_DISABLED:
            assert omission.transport_clause() == ""
        else:
            assert omission.transport_clause()
        # A transport the harness never advertised is refused BEFORE shaping, so
        # an omission that names an ADVERTISED transport must be a shaping
        # failure (nothing to launch or dial) rather than a transport refusal —
        # the two reasons send an operator to different fixes.
        if omission.transport and omission.transport in permitted:
            assert "advertised" not in omission.reason


@given(servers=st.dictionaries(_NAMES, _ENTRIES, max_size=4))
def test_property_conversion_is_deterministic(servers):
    first = wire_session_servers(servers, transports=HTTP_OK, harness="h")
    second = wire_session_servers(servers, transports=HTTP_OK, harness="h")
    assert first == second


# ── Client wiring ───────────────────────────────────────────────────────────


def _client(tmp_path, **kwargs):
    client = AcpClient(work_dir=tmp_path, **kwargs)
    proc = MagicMock()
    proc.returncode = None
    proc.stdin = MagicMock()
    proc.stdin.write = MagicMock()
    proc.stdin.drain = AsyncMock()
    client._process = proc
    client._next_req_id = MagicMock(side_effect=range(1, 100))
    return client


def _drive_initialize(client, init_capabilities):
    """Wire ``_initialize_session`` onto canned responses, recording requests."""
    sent: list[tuple[str, dict]] = []

    async def fake_send(method, params=None):
        sent.append((method, params or {}))
        return len(sent)

    responses = {
        1: {"protocolVersion": "2025-08-22", "agentCapabilities": init_capabilities},
        2: {"sessionId": "sess-1"},
    }

    # Signature mirrors the real ``_wait_for_response``, keyword-only extras
    # included: a narrower stub raises TypeError the moment the caller starts
    # passing one, which reads as a delivery bug rather than a stale double.
    async def fake_wait(req_id, timeout=50.0, *, method="", expected_mcp=None):
        return responses.get(req_id, {})

    client._send_request = fake_send
    client._wait_for_response = AsyncMock(side_effect=fake_wait)
    client._drain_notifications = AsyncMock()
    return sent


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path, monkeypatch):
    """Keep session-file probes off the build host's real ``~/.kiro``."""
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "home")


@pytest.mark.asyncio
async def test_initialize_records_advertised_mcp_transports(tmp_path):
    client = _client(tmp_path)
    _drive_initialize(client, {"loadSession": False, "mcpCapabilities": {"http": True}})

    await client._initialize_session()

    assert client._mcp_transports == McpTransports(http=True)
    # The pre-existing read at the same site keeps working.
    assert client._can_load_session is False


@pytest.mark.asyncio
async def test_initialize_leaves_transports_stdio_only_when_nothing_advertised(tmp_path):
    client = _client(tmp_path)
    _drive_initialize(client, {"loadSession": True})

    await client._initialize_session()

    assert client._mcp_transports == McpTransports()
    assert client._can_load_session is True


@pytest.mark.asyncio
async def test_session_new_carries_the_delivered_servers(tmp_path, monkeypatch):
    overlay = _write_overlay(tmp_path, "kirocrew", {"pooled": _stub()})
    client = _client(tmp_path, mcp_gateway_overlay=overlay)
    sent = _drive_initialize(client, {})

    await client._initialize_session()

    params = dict(sent)[METHOD_SESSION_NEW]
    assert [e["name"] for e in params["mcpServers"]] == ["pooled"]
    assert client.mcp_delivery is not None
    assert client.mcp_delivery.mode == MCP_DELIVERY_FILE_FED


def test_kiro_client_resolves_the_file_fed_kiro_descriptor(tmp_path):
    client = _client(tmp_path)
    descriptor = client._harness_descriptor()
    assert descriptor.id == HARNESS_KIRO
    assert descriptor.mcp_delivery == MCP_DELIVERY_FILE_FED


def test_claude_client_resolves_a_wire_fed_descriptor(tmp_path):
    """The dormant seam is wire-fed: it reads no configuration file of ours."""
    client = _client(tmp_path, acp_backend=ACP_BACKEND_CLAUDE)
    descriptor = client._harness_descriptor()
    assert descriptor.id == HARNESS_CLAUDE
    assert descriptor.mcp_delivery == MCP_DELIVERY_WIRE_FED


def test_unmapped_backend_falls_back_to_file_fed_delivery(tmp_path, caplog):
    """Fail closed: an unrecognized harness gets stubs, not a credential map.

    The degrade is logged as well as taken. A wire-fed harness silently switched
    onto kiro-cli's file-fed mode reads no file of ours, so it starts with no
    tools and reports an ordinary empty array — indistinguishable, from the chat
    and from the delivery report, from a session that genuinely has no servers.
    """
    client = _client(tmp_path)
    client._acp_backend = "not-a-backend"

    with caplog.at_level(logging.WARNING, logger="kiro_crew.acp.client"):
        assert client._harness_descriptor().mcp_delivery == MCP_DELIVERY_FILE_FED

    assert "not-a-backend" in caplog.text
    assert "file-fed" in caplog.text


def test_a_mapped_backend_degrades_silently_because_it_did_not_degrade(tmp_path, caplog):
    """The roster's own backends log nothing — kiro-cli's id is the empty string.

    ``ACP_BACKEND_KIRO`` is ``""``, so a truthiness test on the BACKEND rather
    than on the lookup result would report the default harness as unmapped on
    every single session.
    """
    client = _client(tmp_path)

    with caplog.at_level(logging.WARNING, logger="kiro_crew.acp.client"):
        assert client._harness_descriptor().id == HARNESS_KIRO

    assert "roster" not in caplog.text


@pytest.mark.asyncio
async def test_the_descriptor_is_resolved_off_the_event_loop(tmp_path, monkeypatch):
    """Registry resolution is blocking, so it belongs INSIDE the offloaded call.

    ``registry().get()`` loads the config — it stats and may read
    ``config.json`` — so computing it as an argument to ``to_thread`` puts that
    read back on the loop it was moved off, once per session creation.
    """
    loop_thread = threading.get_ident()
    resolved_on: list[int] = []
    real_descriptor = AcpClient._harness_descriptor

    def _record(self):
        resolved_on.append(threading.get_ident())
        return real_descriptor(self)

    monkeypatch.setattr(AcpClient, "_harness_descriptor", _record)
    client = _client(tmp_path)

    await client._session_mcp_servers()

    assert resolved_on and loop_thread not in resolved_on


@pytest.mark.asyncio
async def test_the_client_delivers_the_project_scoped_spec_for_its_work_dir(tmp_path, monkeypatch):
    """The session's cwd is the project scope, so the client must pass it through.

    ``AcpClient._work_dir`` is the directory the harness is spawned with, which
    is what makes a project agent the one kiro-cli would resolve. Without it a
    project-scoped session receives the user-level agent's servers.
    """
    project = tmp_path / "session-ws"
    _write_agent_spec(tmp_path, monkeypatch, "kirocrew", {"global-only": {"command": "srv"}})
    _write_project_agent_spec(project, "kirocrew", {"project-only": {"command": "srv"}})
    client = _client(project, acp_backend=ACP_BACKEND_CLAUDE)

    servers = await client._session_mcp_servers()

    assert [e["name"] for e in servers] == ["project-only"]


@pytest.mark.asyncio
async def test_wire_fed_client_delivers_the_authorized_map(tmp_path, monkeypatch):
    _write_agent_spec(tmp_path, monkeypatch, "kirocrew", {"own": {"command": "srv"}})
    client = _client(tmp_path, acp_backend=ACP_BACKEND_CLAUDE)

    servers = await client._session_mcp_servers()

    assert [e["name"] for e in servers] == ["own"]
    assert client.mcp_delivery is not None
    assert client.mcp_delivery.no_mcp_tools is False


def test_no_mcp_tools_is_logged_as_a_warning_with_the_structured_report(tmp_path, caplog):
    client = _client(tmp_path)
    delivery = McpDelivery(harness="wirefed", mode=MCP_DELIVERY_WIRE_FED)

    with caplog.at_level(logging.WARNING, logger="kiro_crew.acp.client"):
        client._report_mcp_delivery(delivery)

    assert client.mcp_delivery is delivery
    assert "NO MCP tools" in caplog.text
    assert "noMcpTools" in caplog.text


def test_each_omitted_server_is_logged_by_name_with_its_transport(tmp_path, caplog):
    client = _client(tmp_path)
    delivery = wire_session_servers(
        {"remote": {"url": "https://example.test/mcp"}},
        transports=STDIO_ALL,
        harness="wirefed",
    )

    with caplog.at_level(logging.WARNING, logger="kiro_crew.acp.client"):
        client._report_mcp_delivery(delivery)

    assert "remote" in caplog.text
    assert TRANSPORT_HTTP in caplog.text
    assert "needs" in caplog.text


def test_a_muted_server_is_logged_without_a_transport_clause(tmp_path, caplog):
    """Nothing is needed for a mute — the operator switched the server off.

    The entry is deliverable on an advertised transport, so a "(needs stdio)"
    clause beside "disabled by the operator" would send the reader looking for a
    capability gap that is not there. The name and the reason are what they act
    on.

    Asserted on the omission RECORD, not on the whole capture: the structured
    no-MCP-tools report that follows carries the transport as a data field, which
    is a payload rather than a clause and stays.
    """
    client = _client(tmp_path)
    delivery = wire_session_servers(
        {"muted": {"command": "srv", "disabled": True}},
        transports=STDIO_ALL,
        harness="wirefed",
    )

    with caplog.at_level(logging.WARNING, logger="kiro_crew.acp.client"):
        client._report_mcp_delivery(delivery)

    omission_lines = [
        record.getMessage() for record in caplog.records if "not delivered" in record.getMessage()
    ]
    assert len(omission_lines) == 1
    line = omission_lines[0]
    assert "muted" in line
    assert REASON_DISABLED in line
    assert "needs" not in line
    assert TRANSPORT_STDIO not in line


def test_bundled_kiro_is_the_only_file_fed_harness():
    """Delivery mode is descriptor data; this pins what the bundle declares.

    kiro-cli is file-fed because it loads its servers from its own agent spec.
    Every other harness — including one an operator registers with no delivery
    declaration at all — reads no file of ours, so ACP's session/new array is
    the only channel it has.
    """
    modes = {
        row.id: harness_registry().get(row.id).mcp_delivery for row in harness_registry().list()
    }
    assert modes[HARNESS_KIRO] == MCP_DELIVERY_FILE_FED
    assert {harness: mode for harness, mode in modes.items() if harness != HARNESS_KIRO} == {
        harness: MCP_DELIVERY_WIRE_FED for harness in modes if harness != HARNESS_KIRO
    }
