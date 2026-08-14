"""JSON-RPC conformance for the Engine_MCP_Server.

The cases that matter here are the ones a happy-path test skips: the unknown
method that must be a proper error rather than an empty success (the defect that
once made a client register zero tools with nothing logged), the malformed
`arguments`, and the unknown tool. The init-sequence methods are asserted too,
including the empty `prompts/list` and `resources/list` a client sends before it
lists tools.

Most cases here call :func:`~...engine_mcp.server.handle` directly, which is the
right level for "what does this method answer". :class:`StdioServer` is the same
surface one level lower: the packaged server as a child process, driven over the
line-delimited framing a client actually speaks. It lives here beside ``_req`` so
there is one request builder and one driver for the whole suite —
``test_engine_mcp_library_equivalence`` imports it rather than starting a second
one.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess  # nosec B404 - starts this package's own server, argv list, no shell
import sys
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from kiro_crew.apps.builtins.spec_engine.engine_mcp import server
from kiro_crew.apps.builtins.spec_engine.engine_mcp.server import TOOLS, handle

_METHOD_NOT_FOUND = -32601
_INVALID_PARAMS = -32602

#: The module a client is told to run. Spelled as the documented ``-m`` target so
#: this drives what ships, not a test-only entry point.
SERVER_MODULE = "kiro_crew.apps.builtins.spec_engine.engine_mcp.server"

#: The requests a client sends before it uses the server, in order.
#: ``notifications/initialized`` sits between the first and the second and is
#: driven separately because it expects no reply.
INIT_REQUESTS = ("initialize", "tools/list", "prompts/list", "resources/list")

#: How long to wait for one reply. Generous enough for the first one, which pays
#: the package import in the child, but bounded: a server that never answers must
#: fail the test rather than hang the suite.
REPLY_TIMEOUT_S = 90.0

#: How long to wait for the child to exit after its stdin closes.
EXIT_TIMEOUT_S = 20.0


def _req(method: str, params: dict[str, Any] | None = None, req_id: Any = 1) -> dict[str, Any]:
    request: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        request["params"] = params
    return request


class StdioServer:
    """The packaged server as a child process, spoken to over its real framing.

    Requests are built by ``_req``, so the envelope a stdio test sends is the one
    the in-process tests send. Replies are read by a pump thread rather than a
    bare ``readline``: a dead or silent child would otherwise block the suite
    forever with nothing reported.
    """

    def __init__(self, proc: "subprocess.Popen[str]", stderr_path: Path) -> None:
        self._proc = proc
        self._stderr_path = stderr_path
        self._lines: "queue.Queue[str]" = queue.Queue()
        self._next_id = 0
        self._pump = threading.Thread(target=self._drain, daemon=True)
        self._pump.start()

    def _drain(self) -> None:
        stream = self._proc.stdout
        if stream is not None:
            for line in stream:
                self._lines.put(line)
        self._lines.put("")  # sentinel: the child closed its stdout

    def _write(self, request: dict[str, Any]) -> None:
        stream = self._proc.stdin
        if stream is None:  # pragma: no cover - stdin is always a pipe here
            raise RuntimeError("the server child has no stdin")
        stream.write(json.dumps(request) + "\n")
        stream.flush()

    def _read(self, req_id: Any) -> dict[str, Any]:
        try:
            line = self._lines.get(timeout=REPLY_TIMEOUT_S)
        except queue.Empty:  # pragma: no cover - only on a wedged server
            raise AssertionError(
                f"no reply to id {req_id} within {REPLY_TIMEOUT_S}s; "
                f"server stderr:\n{self.stderr_text()}"
            ) from None
        if not line:
            raise AssertionError(f"server exited before answering; stderr:\n{self.stderr_text()}")
        reply = json.loads(line)
        assert isinstance(reply, dict), f"a reply must be a JSON object, got {line!r}"
        assert reply.get("jsonrpc") == "2.0", f"reply is not JSON-RPC 2.0: {line!r}"
        assert reply.get("id") == req_id, f"reply id {reply.get('id')!r} does not match {req_id!r}"
        return reply

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Send one request and return its reply, checking the id correlates."""
        self._next_id += 1
        req_id = self._next_id
        self._write(_req(method, params, req_id=req_id))
        return self._read(req_id)

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        """Send a notification: no id, and no reply is read."""
        notification: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            notification["params"] = params
        self._write(notification)

    def initialize(self) -> tuple[str, ...]:
        """Drive the client init sequence and return the advertised tool names.

        The whole sequence runs, in order, including the notification and the two
        empty lists a client sends before it uses anything — a divergence in the
        handshake shows up here and nowhere else.
        """
        replies: dict[str, dict[str, Any]] = {}
        for index, method in enumerate(INIT_REQUESTS):
            replies[method] = self.request(method)
            if index == 0:
                self.notify("notifications/initialized")
        init = replies["initialize"]["result"]
        assert init["protocolVersion"], "initialize must echo a protocol version"
        assert init["serverInfo"]["name"] == "spec-engine"
        assert replies["prompts/list"]["result"] == {"prompts": []}
        assert replies["resources/list"]["result"] == {"resources": []}
        return tuple(tool["name"] for tool in replies["tools/list"]["result"]["tools"])

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Invoke one tool and return the raw JSON-RPC reply."""
        return self.request("tools/call", {"name": name, "arguments": arguments})

    def tool_text(self, name: str, arguments: dict[str, Any]) -> str:
        """Invoke one tool and return its result text, failing on an error reply."""
        reply = self.call_tool(name, arguments)
        assert "error" not in reply, f"{name} failed: {reply.get('error')}"
        content = reply["result"]["content"]
        assert content and content[0]["type"] == "text", f"{name} returned no text content"
        return str(content[0]["text"])

    def tool_payload(self, name: str, arguments: dict[str, Any]) -> Any:
        """Invoke one tool and decode its JSON result."""
        return json.loads(self.tool_text(name, arguments))

    def stderr_text(self) -> str:
        if not self._stderr_path.is_file():  # pragma: no cover - written at spawn
            return ""
        return self._stderr_path.read_text(encoding="utf-8", errors="replace")

    def close(self) -> None:
        """Close stdin and wait for the child, killing it if it will not exit."""
        stream = self._proc.stdin
        if stream is not None and not stream.closed:
            stream.close()
        try:
            self._proc.wait(timeout=EXIT_TIMEOUT_S)
        except subprocess.TimeoutExpired:  # pragma: no cover - only on a wedged server
            self._proc.kill()
            self._proc.wait(timeout=EXIT_TIMEOUT_S)


@contextmanager
def stdio_server(home: Path) -> Iterator[StdioServer]:
    """Run the packaged server as a child with its data home pinned at *home*.

    The child resolves its own state root, so the home is how a test isolates it:
    the server constructs its adapter with default roots exactly as it does in
    production, which is the configuration under test.
    """
    home.mkdir(parents=True, exist_ok=True)
    # Outside the home, so the log is not mistaken for engine state by a test that
    # snapshots the state tree.
    stderr_path = home.parent / f"{home.name}-server-stderr.log"
    env = dict(os.environ, KIROCREW_HOME=str(home))
    # Put the repo's source root on the CHILD's path explicitly, derived from this
    # file's location rather than inherited. `python -m` resolves the server module
    # against the child's own sys.path, and the in-process tests import it via the
    # path pytest sets up -- which the child does not get. Inheriting PYTHONPATH
    # instead made these tests pass only in a shell that happened to export it, so
    # they were green for whoever wrote them and red everywhere else, which is the
    # one outcome a conformance test must not have.
    source_root = Path(__file__).resolve().parents[5]
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = f"{source_root}{os.pathsep}{existing}" if existing else str(source_root)
    with stderr_path.open("w", encoding="utf-8") as errors:
        child = subprocess.Popen(  # nosec B603 - fixed argv, no shell, this package's server
            [sys.executable, "-m", SERVER_MODULE],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=errors,
            text=True,
            bufsize=1,
            env=env,
        )
    running = StdioServer(child, stderr_path)
    try:
        yield running
    finally:
        running.close()


def test_initialize_reports_protocol_and_server_name() -> None:
    reply = handle(_req("initialize"))
    assert reply is not None
    result = reply["result"]
    assert result["protocolVersion"]
    assert result["serverInfo"]["name"] == "spec-engine"
    assert "tools" in result["capabilities"]


def test_notifications_initialized_is_a_notification() -> None:
    # A notification carries no id and expects no reply.
    assert handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert handle({"jsonrpc": "2.0", "method": "initialized"}) is None


def test_tools_list_advertises_every_registered_tool() -> None:
    reply = handle(_req("tools/list"))
    assert reply is not None
    listed = {t["name"] for t in reply["result"]["tools"]}
    assert listed == set(TOOLS)
    for tool in reply["result"]["tools"]:
        schema = tool["inputSchema"]
        assert schema["type"] == "object"
        # Closed schema: an unknown argument is refused, not ignored.
        assert schema["additionalProperties"] is False


def test_prompts_list_answers_an_empty_set() -> None:
    reply = handle(_req("prompts/list"))
    assert reply is not None
    assert reply["result"] == {"prompts": []}


def test_resources_list_answers_an_empty_set() -> None:
    reply = handle(_req("resources/list"))
    assert reply is not None
    assert reply["result"] == {"resources": []}


def test_unknown_method_is_a_jsonrpc_error_not_empty_success() -> None:
    reply = handle(_req("does/not/exist"))
    assert reply is not None
    # The whole point: an unknown method returns an error object with the right
    # code, never an empty `result`, which is how a client silently registers
    # zero tools.
    assert "result" not in reply
    assert reply["error"]["code"] == _METHOD_NOT_FOUND


def test_unknown_tool_is_method_not_found() -> None:
    reply = handle(_req("tools/call", {"name": "no_such_tool", "arguments": {}}))
    assert reply is not None
    assert "result" not in reply
    assert reply["error"]["code"] == _METHOD_NOT_FOUND


def test_non_dict_arguments_are_rejected() -> None:
    reply = handle(_req("tools/call", {"name": "get_orchestrator_prompt", "arguments": [1, 2]}))
    assert reply is not None
    assert "result" not in reply
    assert reply["error"]["code"] == _INVALID_PARAMS


def test_missing_required_argument_is_invalid_params() -> None:
    reply = handle(_req("tools/call", {"name": "get_authoring_prompt", "arguments": {}}))
    assert reply is not None
    assert "result" not in reply
    assert reply["error"]["code"] == _INVALID_PARAMS


def test_unknown_argument_is_refused_by_the_closed_schema() -> None:
    reply = handle(
        _req(
            "tools/call",
            {"name": "get_authoring_prompt", "arguments": {"spec_type": "feature", "x": 1}},
        )
    )
    assert reply is not None
    assert "result" not in reply
    assert reply["error"]["code"] == _INVALID_PARAMS


def test_deleting_a_registration_hides_the_tool(monkeypatch: Any) -> None:
    # Registration is what makes a tool real: with the entry removed, the tool
    # is neither advertised nor callable, and the surface test that expects it
    # fails. This pins that the registry, not the handler, is the surface.
    patched = dict(TOOLS)
    del patched["validate_spec"]
    monkeypatch.setattr(server, "TOOLS", patched)

    tools = handle(_req("tools/list"))
    assert tools is not None, "tools/list is a request and always answers"
    listed = {t["name"] for t in tools["result"]["tools"]}
    assert "validate_spec" not in listed

    reply = handle(_req("tools/call", {"name": "validate_spec", "arguments": {}}))
    assert reply is not None, "a call carrying an id always answers"
    assert reply["error"]["code"] == _METHOD_NOT_FOUND


def test_init_sequence_over_stdio_advertises_the_same_tools(tmp_path: Path) -> None:
    # The framing is part of the surface: a reply written but never flushed, or a
    # notification answered with an id, is invisible to a test that calls handle()
    # in-process. Driving the real child through the whole init sequence is what
    # pins it, and it is the same sequence the state-equivalence tests run before
    # they touch a tool.
    with stdio_server(tmp_path / "home") as running:
        advertised = running.initialize()
        assert set(advertised) == set(TOOLS)
        # The child is still usable after the notification, i.e. the notification
        # consumed no reply slot and left the stream aligned.
        second = running.request("tools/list")
        assert {tool["name"] for tool in second["result"]["tools"]} == set(TOOLS)
        unknown = running.request("does/not/exist")
        assert "result" not in unknown
        assert unknown["error"]["code"] == _METHOD_NOT_FOUND
