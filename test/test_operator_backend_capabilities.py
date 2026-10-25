"""``capabilities_from``: a ``harnesses.json`` launcher of a builtin's adapter answers its gates."""

from __future__ import annotations

import json
import stat
from types import SimpleNamespace

import pytest

from kiro_crew.acp.harness import _reset_operator_register
from kiro_crew.acp.harness.operator_registry import (
    _reset_operator_diagnostics,
    invalid_operator_harnesses,
    load_and_register_operator_descriptors,
)
from kiro_crew.agent_sdk import backend_install
from kiro_crew.agent_sdk.backend_cards import USER_FACING_LINES, card_for, card_payload
from kiro_crew.agent_sdk.backends import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKENDS_COMPACT,
    ACP_BACKENDS_MEMBER_DISPATCH,
    ACP_BACKENDS_MODEL_VIA_CONFIG_OPTION,
    ACP_BACKENDS_SESSION_MCP_ARRAY,
    ACP_BACKENDS_SIDE_READONLY,
    ACP_BACKENDS_STEERING_REQUEST,
)
from kiro_crew.agent_sdk.capability_inheritance import (
    NOT_INHERITED,
    InheritingSet,
    capability_source,
)
from kiro_crew.agent_sdk.operator_backend_registry import _reset_registered_backends


def _seat(seat_id: str, executable: str, **over: object) -> dict:
    entry = {
        "id": seat_id,
        "display_name": seat_id.replace("-", " ").title(),
        "executable": executable,
        "argv": ["{executable}"],
        "model_source": "acp_advertised",
        "mcp_delivery": "session_array",
        "routing": "session_config",
        "permission_config": {"option": "mode", "value": "default"},
        "session_family": "claude-code",
    }
    entry.update(over)
    return entry


@pytest.fixture
def wrapper(tmp_path):
    path = tmp_path / "launcher"
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


@pytest.fixture
def load(tmp_path):
    def _load(entries: dict) -> list[str]:
        harnesses = tmp_path / "harnesses.json"
        harnesses.write_text(json.dumps(entries), encoding="utf-8")
        backend_install.clear_probe_cache()
        return load_and_register_operator_descriptors(path=harnesses)

    yield _load
    _reset_registered_backends()
    _reset_operator_register()
    _reset_operator_diagnostics()
    backend_install.clear_probe_cache()


@pytest.fixture
def seats(load, wrapper):
    return load(
        {
            "seat-explicit": _seat("seat-explicit", wrapper, capabilities_from="claude"),
            "seat-inferred": _seat("seat-inferred", wrapper),
            "seat-opted-out": _seat("seat-opted-out", wrapper, capabilities_from="none"),
            "agy": _agy(
                "agy", wrapper, capabilities=["crew_tools", "member_tools", "model_switch"]
            ),
            "agy-bare": _agy("agy-bare", wrapper),
            "seat-listed": _seat(
                "seat-listed", wrapper, capabilities_from="claude", capabilities=["crew_tools"]
            ),
            "strands": {
                "id": "strands",
                "display_name": "Strands",
                "executable": wrapper,
                "argv": ["{executable}"],
                "model_source": "static",
                "models": ["m"],
                "agent_args": ["--agent={agent}"],
                "mcp_delivery": "session_array",
                "routing": "agent_spec",
            },
        }
    )


def _agy(agy_id: str, executable: str, **over: object) -> dict:
    entry = {
        "id": agy_id,
        "display_name": agy_id.title(),
        "executable": executable,
        "argv": ["{executable}"],
        "mcp_delivery": "agent_file",
        "routing": "session_config",
        "permission_config": {"option": "mode", "value": "default"},
    }
    entry.update(over)
    return entry


def _held(backend: str) -> set[str]:
    return {line.id for line in card_for(backend).capabilities if line.available}


def _lines(backend: str) -> dict[str, bool]:
    return {line.id: line.available for line in card_for(backend).capabilities}


@pytest.mark.parametrize("seat", ["seat-explicit", "seat-inferred"])
def test_a_claude_launcher_card_matches_claude_on_every_inherited_row(seats, seat):
    assert capability_source(seat) == ACP_BACKEND_CLAUDE
    seat_lines, claude_lines = _lines(seat), _lines(ACP_BACKEND_CLAUDE)
    assert len(seat_lines) == len(USER_FACING_LINES) == 7
    # Side Chat is the one row held back: claude's read-only confinement is built
    # by AcpClient, and a launcher runs on AcpRuntime, so inheriting it would grant
    # read-only tools with no confinement behind them.
    assert seat_lines.pop("side_chat_tools") is False
    claude_lines.pop("side_chat_tools")
    assert seat_lines == claude_lines
    assert all(seat_lines.values())


@pytest.mark.parametrize("seat", ["seat-explicit", "seat-inferred"])
def test_a_claude_launcher_passes_the_runtime_gates(seats, seat):
    from kiro_crew.acp.steering_identity import takes_steering_request
    from kiro_crew.agent_sdk.capabilities import capabilities_for

    assert seat in ACP_BACKENDS_SESSION_MCP_ARRAY  # Crew's MCP tools on session/new
    assert seat in ACP_BACKENDS_MEMBER_DISPATCH
    assert seat in ACP_BACKENDS_COMPACT
    assert seat in ACP_BACKENDS_MODEL_VIA_CONFIG_OPTION  # model switch in the chat
    assert capabilities_for(seat).effort_via_config_option
    assert seat in ACP_BACKENDS_STEERING_REQUEST

    ready = SimpleNamespace(
        acp_backend=seat, claude_adapter=True, steering_advertised=True, agent_version="0.94.0"
    )
    assert takes_steering_request(ready)
    # Inheriting the table does not skip claude's own floor.
    assert not takes_steering_request(SimpleNamespace(**{**vars(ready), "agent_version": "0.60.0"}))
    assert not takes_steering_request(SimpleNamespace(**{**vars(ready), "claude_adapter": False}))


def test_a_claude_launcher_gets_crews_mcp_array_on_the_runtime(seats):
    from kiro_crew.providers.mirrors.registry import mirror_for

    assert mirror_for("seat-inferred") is not None


def test_side_chat_tools_are_never_inherited(seats):
    assert "ACP_BACKENDS_SIDE_READONLY" in NOT_INHERITED
    assert ACP_BACKEND_CLAUDE in ACP_BACKENDS_SIDE_READONLY
    assert "seat-explicit" not in ACP_BACKENDS_SIDE_READONLY


def test_the_explicit_field_wins_over_the_family_inference(seats):
    assert capability_source("seat-opted-out") == "seat-opted-out"
    assert "seat-opted-out" not in ACP_BACKENDS_COMPACT
    # Opted out of claude, it falls to the mcp_delivery inference.
    assert _held("seat-opted-out") == {"crew_tools", "member_thread_tools"}


def test_an_explicit_list_wins_over_capabilities_from(seats):
    assert capability_source("seat-listed") == "seat-listed"
    assert _held("seat-listed") == {"crew_tools"}
    assert "seat-listed" not in ACP_BACKENDS_COMPACT


def test_a_capability_source_must_be_a_builtin(load, wrapper):
    ids = load({"bad": _seat("bad", wrapper, capabilities_from="not-a-builtin")})
    assert "bad" not in ids
    assert "not a builtin backend" in invalid_operator_harnesses()["bad"][0]


def test_a_measured_list_admits_exactly_its_runtime_sets(seats):
    from kiro_crew.agent_sdk.capability_inheritance import declared_capabilities

    assert capability_source("agy") == "agy"
    assert _held("agy") == {"crew_tools", "member_thread_tools", "model_switch"}
    assert declared_capabilities("agy") == {"crew_tools", "member_tools", "model_switch"}
    assert "agy" in ACP_BACKENDS_SESSION_MCP_ARRAY
    assert "agy" in ACP_BACKENDS_MEMBER_DISPATCH
    assert "agy" in ACP_BACKENDS_MODEL_VIA_CONFIG_OPTION
    assert "agy" not in ACP_BACKENDS_STEERING_REQUEST
    assert "agy" not in ACP_BACKENDS_COMPACT
    assert "agy" not in ACP_BACKENDS_SIDE_READONLY


def test_a_harness_given_crews_mcp_array_never_reports_zero(seats):
    # agent_file passes the array through; only session_array declares it is read.
    assert _held("agy-bare") == {"crew_tools"}
    assert _held("strands") == {"crew_tools", "member_thread_tools"}


@pytest.mark.parametrize(
    "names, fragment",
    [
        (["crew_tools", "telepathy"], "capabilities[1] is not one of"),
        (["side_chat_tools"], "only the builtin harnesses' own drivers build"),
        ("crew_tools", "must be an array of strings"),
    ],
)
def test_a_capability_list_is_validated(load, wrapper, names, fragment):
    ids = load({"bad": _agy("bad", wrapper, capabilities=names)})
    assert "bad" not in ids
    # An invalid row is listed by file position, never by its operator-written id.
    reasons = [r for rows in invalid_operator_harnesses().values() for r in rows]
    assert any(fragment in reason for reason in reasons), reasons


def test_an_empty_list_declares_none(load, wrapper):
    load({"quiet": _agy("quiet", wrapper, capabilities=[])})
    assert _held("quiet") == set()


def test_enumerating_a_set_still_lists_builtins_only(seats):
    assert isinstance(ACP_BACKENDS_COMPACT, InheritingSet)
    assert "seat-explicit" in ACP_BACKENDS_COMPACT
    assert "seat-explicit" not in set(ACP_BACKENDS_COMPACT)
    assert ACP_BACKENDS_COMPACT == frozenset(ACP_BACKENDS_COMPACT)


def test_every_launch_and_sign_in_set_answers_by_id(seats):
    from kiro_crew.agent_sdk import backends

    for name in NOT_INHERITED:
        if name == "ACP_BACKENDS_KNOWN":
            continue  # every registered id is known in its own right
        assert "seat-explicit" not in getattr(backends, name), name


def test_a_claude_launcher_states_its_own_sign_in(seats):
    from kiro_crew.dashboard.handlers.acp_backend_status import _sign_in_remedy

    assert "CLAUDE_CONFIG_DIR" in _sign_in_remedy("seat-inferred", "generic")
    assert _sign_in_remedy("agy", "generic") == "generic"


def test_the_card_payload_counts_six_of_seven_for_a_seat(seats):
    payload = card_payload("seat-explicit")
    available = [line["id"] for line in payload["capabilities"] if line["available"]]
    assert len(payload["capabilities"]) == 7 and len(available) == 6


@pytest.mark.parametrize(
    "enabled, stubs, withheld",
    [(False, [], True), (True, [], True), (True, ["kirocrew-core"], False)],
)
def test_crew_tool_lines_follow_whether_crews_servers_reach_a_masked_child(
    seats, monkeypatch, enabled, stubs, withheld
):
    from kiro_crew import acp_tool_gate
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.dashboard.handlers import acp_backend_status

    cfg = KiroCrewConfig()
    cfg.mcp_gateway.enabled = enabled
    cfg.mcp_gateway.stub_servers = stubs
    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls, *a, **k: cfg))
    # agy is session_config-routed (masked); strands is agent_spec-routed (not).
    monkeypatch.setattr(
        acp_tool_gate,
        "adapter_hidden_credential_dirs",
        lambda backend: ("/masked",) if backend != "strands" else (),
    )
    for backend, expect_withheld in (("agy", withheld), ("strands", False), ("claude", False)):
        card = acp_backend_status._with_crew_server_reach(backend, card_payload(backend))
        held = {line["id"] for line in card["capabilities"] if line["available"]}
        if expect_withheld:
            assert not held & {"crew_tools", "member_thread_tools"}, backend
        else:
            assert held == {
                line["id"] for line in card_payload(backend)["capabilities"] if line["available"]
            }, backend
