"""The two external transports, exercised at the process boundary.

These tests spawn real children. The claims are about what happens between two
processes — that a request reaches a program's standard input, that a response
comes back parsed, that a provider which never answers is killed rather than
holding the engine open, and that the spawn goes through the package's sandbox
chokepoint. A stubbed subprocess would confirm the arguments the engine intended
and say nothing about any of that.

Every fake provider here is written by the test. Nothing in this tree ships or
embeds a provider implementation: an external provider is a command configuration
names, whichever transport carries it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

import pytest

from kiro_crew import sandbox
from kiro_crew.apps.builtins.spec_engine.engine.capabilities import (
    FINDING_PROVIDER_TIMEOUT,
    FINDING_PROVIDER_UNAVAILABLE,
    FINDING_RESPONSE_INVALID,
    MAX_OUTPUT_CHARS,
    MCP_TOOL_PREFIX,
    ArtifactRef,
    CapabilityRequest,
    ChildOutcome,
    CommandProviderTransport,
    McpProviderTransport,
    TransportFailure,
    run_provider_child,
)
from kiro_crew.apps.builtins.spec_engine.engine.capabilities.transports import (
    MCP_PROTOCOL_VERSION,
    _last_json_object,
)

from .test_capability_schemas import response_payload


def request_for(capability: str = "analysis") -> CapabilityRequest:
    return CapabilityRequest(
        capability=capability,
        spec_type="feature",
        artifacts=(ArtifactRef(kind="requirements", path="/p/requirements.md"),),
        run="run-1",
    )


@pytest.fixture()
def command_provider(tmp_path: Path) -> list[str]:
    """A provider that echoes a valid response built from the request it read."""
    script = tmp_path / "command_provider.py"
    script.write_text(
        "import json, sys\n"
        "request = json.load(sys.stdin)\n"
        "print(json.dumps({\n"
        "    'schema_version': 1,\n"
        "    'capability': request['capability'],\n"
        "    'provider': {'name': 'test-provider', 'version': '9'},\n"
        "    'coverage': {'processed': [a['kind'] for a in request['artifacts']],\n"
        "                 'skipped': []},\n"
        "    'findings': [],\n"
        "    'result': {'depth': 'structural'},\n"
        "}))\n",
        encoding="utf-8",
    )
    return [sys.executable, str(script)]


@pytest.fixture()
def mcp_provider(tmp_path: Path) -> list[str]:
    """An MCP stdio provider answering the capability tool call from stdin."""
    script = tmp_path / "mcp_provider.py"
    script.write_text(
        "import json, sys\n"
        "for line in sys.stdin:\n"
        "    line = line.strip()\n"
        "    if not line:\n"
        "        continue\n"
        "    message = json.loads(line)\n"
        "    method = message.get('method')\n"
        "    if method == 'initialize':\n"
        "        print(json.dumps({'jsonrpc': '2.0', 'id': message['id'],\n"
        "                          'result': {'protocolVersion': '" + MCP_PROTOCOL_VERSION + "',\n"
        "                                     'capabilities': {'tools': {}},\n"
        "                                     'serverInfo': {'name': 'test', 'version': '1'}}}))\n"
        "    elif method == 'tools/call':\n"
        "        arguments = message['params']['arguments']\n"
        "        body = {'schema_version': 1, 'capability': arguments['capability'],\n"
        "                'provider': {'name': message['params']['name']},\n"
        "                'coverage': {'processed': ['requirements'], 'skipped': []},\n"
        "                'findings': [], 'result': {'depth': 'extended'}}\n"
        "        print(json.dumps({'jsonrpc': '2.0', 'id': message['id'],\n"
        "                          'result': {'content': [{'type': 'text',\n"
        "                                                  'text': json.dumps(body)}]}}))\n"
        "    sys.stdout.flush()\n",
        encoding="utf-8",
    )
    return [sys.executable, str(script)]


class TestChildSpawn:
    def test_the_request_reaches_the_child_on_standard_input(self, tmp_path: Path) -> None:
        recorder = tmp_path / "seen.json"
        script = tmp_path / "recorder.py"
        script.write_text(
            "import json, sys\n"
            f"open({str(recorder)!r}, 'w').write(sys.stdin.read())\n"
            "print('{}')\n",
            encoding="utf-8",
        )
        outcome = run_provider_child(
            [sys.executable, str(script)], stdin_text='{"hello": "world"}', timeout_s=30
        )
        assert outcome.exit_code == 0
        assert json.loads(recorder.read_text(encoding="utf-8")) == {"hello": "world"}

    def test_the_spawn_is_routed_through_the_sandbox_chokepoint(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A provider child runs an operator-configured program unattended over
        # content the engine did not author. Losing the chokepoint would lose both
        # the filesystem isolation and the credential scrub in one edit, so the
        # route is pinned.
        seen: list[list[str]] = []
        original = sandbox.sandboxed_spawn_argv

        def recording(argv: list[str], *args: Any, **kwargs: Any) -> Any:
            seen.append(list(argv))
            return original(argv, *args, **kwargs)

        monkeypatch.setattr(sandbox, "sandboxed_spawn_argv", recording)
        run_provider_child([sys.executable, "-c", "pass"], stdin_text="", timeout_s=30)
        assert seen and seen[0][0] == sys.executable

    def test_a_configured_environment_variable_reaches_the_child(self, tmp_path: Path) -> None:
        outcome = run_provider_child(
            [sys.executable, "-c", "import os; print(os.environ.get('PROVIDER_MODE', 'unset'))"],
            stdin_text="",
            timeout_s=30,
            env_overlay={"PROVIDER_MODE": "deep"},
        )
        assert outcome.stdout.strip() == "deep"

    def test_a_child_that_never_answers_is_killed(self) -> None:
        outcome = run_provider_child(
            [sys.executable, "-c", "import time; time.sleep(30)"], stdin_text="", timeout_s=1
        )
        assert outcome.timed_out
        assert outcome.exit_code is None

    def test_a_missing_program_fails_without_raising(self) -> None:
        outcome = run_provider_child(
            ["definitely-not-on-this-path-4718"], stdin_text="", timeout_s=5
        )
        # Which of the two failure shapes appears depends on where the exec is
        # refused: the interpreter reports a start error when it cannot spawn at
        # all, while a sandbox launcher that did start reports a non-zero exit.
        # Both are unavailability, and neither raises — that is the claim the
        # transport's classification rests on.
        assert not outcome.timed_out
        assert outcome.start_error or outcome.exit_code not in (0, None)

    def test_a_child_that_ignores_its_input_still_completes(self) -> None:
        # stdin closes after the request, so a provider that reads no further gets
        # end-of-file instead of blocking until the deadline.
        outcome = run_provider_child(
            [sys.executable, "-c", "print('done')"], stdin_text="x" * 1024, timeout_s=30
        )
        assert outcome.exit_code == 0
        assert outcome.stdout.strip() == "done"

    def test_captured_output_is_capped(self) -> None:
        oversized = MAX_OUTPUT_CHARS + 4096
        outcome = run_provider_child(
            [sys.executable, "-c", f"print('x' * {oversized})"], stdin_text="", timeout_s=60
        )
        assert len(outcome.stdout) <= MAX_OUTPUT_CHARS


class TestCommandTransport:
    def test_a_round_trip_returns_the_providers_payload(self, command_provider: list[str]) -> None:
        transport = CommandProviderTransport(argv=tuple(command_provider))
        payload = transport.invoke(request_for(), timeout_s=30)
        assert payload["capability"] == "analysis"
        assert payload["provider"]["name"] == "test-provider"
        assert payload["coverage"]["processed"] == ["requirements"]

    def test_a_timeout_is_reported_with_the_timeout_finding(self, tmp_path: Path) -> None:
        transport = CommandProviderTransport(
            argv=(sys.executable, "-c", "import time; time.sleep(30)")
        )
        with pytest.raises(TransportFailure) as raised:
            transport.invoke(request_for(), timeout_s=1)
        assert raised.value.finding_id == FINDING_PROVIDER_TIMEOUT

    def test_a_missing_program_is_reported_as_unavailable(self) -> None:
        transport = CommandProviderTransport(argv=("definitely-not-on-this-path-4718",))
        with pytest.raises(TransportFailure) as raised:
            transport.invoke(request_for(), timeout_s=5)
        assert raised.value.finding_id == FINDING_PROVIDER_UNAVAILABLE

    def test_a_non_zero_exit_is_reported_as_unavailable(self) -> None:
        transport = CommandProviderTransport(
            argv=(sys.executable, "-c", "import sys; sys.stderr.write('boom'); sys.exit(3)")
        )
        with pytest.raises(TransportFailure) as raised:
            transport.invoke(request_for(), timeout_s=30)
        assert raised.value.finding_id == FINDING_PROVIDER_UNAVAILABLE
        assert "boom" in raised.value.detail

    def test_output_that_is_not_a_json_object_is_reported_as_invalid(self) -> None:
        transport = CommandProviderTransport(argv=(sys.executable, "-c", "print('not json')"))
        with pytest.raises(TransportFailure) as raised:
            transport.invoke(request_for(), timeout_s=30)
        assert raised.value.finding_id == FINDING_RESPONSE_INVALID

    def test_a_leading_banner_line_does_not_hide_the_response(self) -> None:
        body = json.dumps(response_payload("analysis"))
        transport = CommandProviderTransport(
            argv=(
                sys.executable,
                "-c",
                f"print('analyzer v3 starting'); print({body!r})",
            )
        )
        payload = transport.invoke(request_for(), timeout_s=30)
        assert payload["provider"]["name"] == "candidate"

    def test_the_transport_names_itself(self) -> None:
        assert CommandProviderTransport(argv=("x",)).transport == "command"

    def test_a_substituted_runner_receives_the_wire_request(self) -> None:
        seen: dict[str, Any] = {}

        def runner(
            argv: Sequence[str],
            *,
            stdin_text: str,
            timeout_s: int,
            env_overlay: Mapping[str, str] | None = None,
            cwd: Path | None = None,
        ) -> ChildOutcome:
            seen["request"] = json.loads(stdin_text)
            seen["timeout"] = timeout_s
            return ChildOutcome(exit_code=0, stdout=json.dumps(response_payload("analysis")))

        transport = CommandProviderTransport(argv=("analyzer",), runner=runner)
        transport.invoke(request_for(), timeout_s=42)
        assert seen["timeout"] == 42
        assert seen["request"]["capability"] == "analysis"
        assert seen["request"]["format_version"]


class TestMcpTransport:
    def test_a_tool_call_round_trip_returns_the_providers_payload(
        self, mcp_provider: list[str]
    ) -> None:
        transport = McpProviderTransport(argv=tuple(mcp_provider))
        payload = transport.invoke(request_for(), timeout_s=30)
        assert payload["capability"] == "analysis"
        assert payload["result"]["depth"] == "extended"
        # The tool name follows one rule, so a provider author needs the
        # capability name and nothing else.
        assert payload["provider"]["name"] == f"{MCP_TOOL_PREFIX}analysis"

    def test_the_tool_name_is_derived_from_the_capability(self) -> None:
        transport = McpProviderTransport(argv=("x",))
        assert transport.tool_name("review") == "capability_review"

    def test_a_structured_content_result_is_accepted(self) -> None:
        body = response_payload("analysis")

        def runner(
            argv: Sequence[str],
            *,
            stdin_text: str,
            timeout_s: int,
            env_overlay: Mapping[str, str] | None = None,
            cwd: Path | None = None,
        ) -> ChildOutcome:
            reply = {"jsonrpc": "2.0", "id": 2, "result": {"structuredContent": body}}
            return ChildOutcome(exit_code=0, stdout=json.dumps(reply) + "\n")

        payload = McpProviderTransport(argv=("x",), runner=runner).invoke(
            request_for(), timeout_s=10
        )
        assert payload == body

    def test_a_json_rpc_error_reply_is_reported_as_unavailable(self) -> None:
        def runner(
            argv: Sequence[str],
            *,
            stdin_text: str,
            timeout_s: int,
            env_overlay: Mapping[str, str] | None = None,
            cwd: Path | None = None,
        ) -> ChildOutcome:
            reply = {
                "jsonrpc": "2.0",
                "id": 2,
                "error": {"code": -32601, "message": "no such tool"},
            }
            return ChildOutcome(exit_code=0, stdout=json.dumps(reply) + "\n")

        with pytest.raises(TransportFailure) as raised:
            McpProviderTransport(argv=("x",), runner=runner).invoke(request_for(), timeout_s=10)
        assert raised.value.finding_id == FINDING_PROVIDER_UNAVAILABLE
        assert "no such tool" in raised.value.detail

    def test_a_missing_reply_is_reported_as_unavailable(self) -> None:
        def runner(
            argv: Sequence[str],
            *,
            stdin_text: str,
            timeout_s: int,
            env_overlay: Mapping[str, str] | None = None,
            cwd: Path | None = None,
        ) -> ChildOutcome:
            # Answers the handshake and nothing else.
            return ChildOutcome(
                exit_code=0, stdout=json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}) + "\n"
            )

        with pytest.raises(TransportFailure) as raised:
            McpProviderTransport(argv=("x",), runner=runner).invoke(request_for(), timeout_s=10)
        assert raised.value.finding_id == FINDING_PROVIDER_UNAVAILABLE

    def test_a_tool_result_with_no_json_is_reported_as_invalid(self) -> None:
        def runner(
            argv: Sequence[str],
            *,
            stdin_text: str,
            timeout_s: int,
            env_overlay: Mapping[str, str] | None = None,
            cwd: Path | None = None,
        ) -> ChildOutcome:
            reply = {
                "jsonrpc": "2.0",
                "id": 2,
                "result": {"content": [{"type": "text", "text": "sorry"}]},
            }
            return ChildOutcome(exit_code=0, stdout=json.dumps(reply) + "\n")

        with pytest.raises(TransportFailure) as raised:
            McpProviderTransport(argv=("x",), runner=runner).invoke(request_for(), timeout_s=10)
        assert raised.value.finding_id == FINDING_RESPONSE_INVALID

    def test_a_provider_that_never_answers_is_reported_as_a_timeout(self) -> None:
        transport = McpProviderTransport(argv=(sys.executable, "-c", "import time; time.sleep(30)"))
        with pytest.raises(TransportFailure) as raised:
            transport.invoke(request_for(), timeout_s=1)
        assert raised.value.finding_id == FINDING_PROVIDER_TIMEOUT

    def test_an_untidy_exit_after_answering_is_still_an_answer(self) -> None:
        body = json.dumps(response_payload("analysis"))

        def runner(
            argv: Sequence[str],
            *,
            stdin_text: str,
            timeout_s: int,
            env_overlay: Mapping[str, str] | None = None,
            cwd: Path | None = None,
        ) -> ChildOutcome:
            reply = {
                "jsonrpc": "2.0",
                "id": 2,
                "result": {"content": [{"type": "text", "text": body}]},
            }
            return ChildOutcome(exit_code=1, stdout=json.dumps(reply) + "\n", stderr="teardown")

        payload = McpProviderTransport(argv=("x",), runner=runner).invoke(
            request_for(), timeout_s=10
        )
        assert payload["capability"] == "analysis"

    def test_the_handshake_and_the_call_are_written_together(self) -> None:
        seen: dict[str, Any] = {}

        def runner(
            argv: Sequence[str],
            *,
            stdin_text: str,
            timeout_s: int,
            env_overlay: Mapping[str, str] | None = None,
            cwd: Path | None = None,
        ) -> ChildOutcome:
            seen["messages"] = [json.loads(line) for line in stdin_text.splitlines() if line]
            return ChildOutcome(
                exit_code=0,
                stdout=json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "result": {"structuredContent": response_payload("analysis")},
                    }
                ),
            )

        McpProviderTransport(argv=("x",), runner=runner).invoke(request_for(), timeout_s=10)
        methods = [message.get("method") for message in seen["messages"]]
        assert methods == ["initialize", "notifications/initialized", "tools/call"]
        assert seen["messages"][0]["params"]["protocolVersion"] == MCP_PROTOCOL_VERSION


class TestOutputParsing:
    def test_a_whole_pretty_printed_object_parses(self) -> None:
        assert _last_json_object('{\n  "a": 1\n}') == {"a": 1}

    def test_the_last_object_wins_over_an_earlier_one(self) -> None:
        # A banner that happens to be JSON must not be mistaken for the answer.
        assert _last_json_object('{"banner": true}\n{"answer": 2}') == {"answer": 2}

    def test_an_array_is_not_a_response(self) -> None:
        assert _last_json_object("[1, 2]") is None

    def test_empty_output_parses_to_nothing(self) -> None:
        assert _last_json_object("   \n") is None
