"""The five setup and configuration tools, driven end to end over real stdio.

The behaviour of these tools is held by ``test_engine_mcp_setup_tools`` and
``test_engine_mcp_config_tools``, which dispatch in process. What is left, and
what this module is for, is the part in-process dispatch cannot see: whether the
tools exist on the wire. A tool can be registered, correct, and still unusable --
advertised with an open schema, answering a refusal as a protocol error, or
reachable only because the test called the handler that the child never routes
to. Every case here therefore goes through the packaged server as a child
process, over the line-delimited framing a client actually speaks.

The driver is imported, not rebuilt: :func:`stdio_server` and
:class:`StdioServer` live in ``test_engine_mcp_conformance`` beside the one
request builder, and a second harness would be a second definition of what "the
server" means.

Four claims:

* **Advertised with a closed schema.** ``tools/list`` from the child names all
  five, and each declares the arguments the design tables say it declares, with
  ``additionalProperties`` false at the argument object AND inside ``answers``, so
  an unknown key is refused rather than ignored.
* **Every one round-trips.** Each tool is called over the wire and answers with
  decodable JSON content -- including the two that write, whose effect is then
  read back off the filesystem.
* **A refusal arrives as a refusal.** The payloads ``setup_surface`` and
  ``config_surface`` produce come back as results carrying a ``refused`` code, not
  as protocol errors and not as text with a traceback in it. Malformed calls, by
  contrast, come back as the JSON-RPC codes the server documents.
* **Non-vacuity, per tool.** Each positive control observes something only a real
  execution can produce: evidence quoting text planted in a fixture project, a
  ``plan_id`` that recomputes from the reply's own contents, a ``config.json`` read
  back off disk, and a planted credential that is present in that file and absent
  from the whole serialized reply.
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import pytest

from kiro_crew.apps.builtins.spec_engine.engine.config import ELIDED, ConfigStore
from kiro_crew.apps.builtins.spec_engine.engine.setup import (
    CONFIRMED_LEVELS,
    SUBJECT_COST_PROFILE,
    SUBJECT_TOOLING,
    SUBJECT_WATCH_SOURCE,
    SUBJECT_WORKFLOW_PRACTICE,
    SUBJECT_WORKFLOW_PRESET,
)
from kiro_crew.apps.builtins.spec_engine.engine_mcp import config_surface
from kiro_crew.apps.builtins.spec_engine.engine_mcp.operations import ENGINE_MCP_SURFACE
from kiro_crew.apps.builtins.spec_engine.engine_mcp.setup_surface import (
    REFUSAL_APPROVER_REQUIRED,
    REFUSAL_PLAN_STALE,
    REFUSAL_SETUP_APPROVAL,
    REFUSED_KEY,
    plan_identity,
)
from kiro_crew.security import redact

from .test_engine_mcp_conformance import StdioServer, stdio_server

_METHOD_NOT_FOUND = -32601
_INVALID_PARAMS = -32602

#: The tools this module is responsible for.
NEW_TOOLS = ("inspect_setup", "plan_setup", "apply_setup", "get_config", "write_config")

#: What each tool declares, restated from the design's tool table rather than read
#: back out of ``TOOLS``. Comparing the wire against the registry would pass with
#: both wrong together; comparing it against an independent statement of the
#: contract is what makes a silently widened argument list fail.
DECLARED_PROPERTIES: dict[str, frozenset[str]] = {
    "inspect_setup": frozenset({"project", "name"}),
    "plan_setup": frozenset({"project", "name", "answers"}),
    "apply_setup": frozenset({"project", "name", "answers", "plan_id", "approver"}),
    "get_config": frozenset(),
    "write_config": frozenset({"patch", "actor"}),
}

#: The arguments each tool requires, in the order the schema lists them.
DECLARED_REQUIRED: dict[str, tuple[str, ...]] = {
    "inspect_setup": ("project",),
    "plan_setup": ("project", "answers"),
    "apply_setup": ("project", "answers", "plan_id", "approver"),
    "get_config": (),
    "write_config": ("patch",),
}

#: The answer object's own keys, closed for the same reason the argument object is.
DECLARED_ANSWER_KEYS = frozenset(
    {"cost_profile", "confirmations", "approved_subjects", "workflow_preset", "watch_source"}
)

#: Planted in the fixture project's git config. The evidence for a watch source is
#: the origin URL itself, so this string appearing in a reply is proof the reply
#: was derived from the project on disk.
REMOTE_URL = "git@github.com:acme/sentinel-widgets.git"

#: Planted in a steering file. The practice inference quotes the line it matched,
#: so this marker travels the same way.
PRACTICE_LINE = "Changes land through a pull request, per SENTINEL-PRACTICE-NOTE."

#: Planted as a project variable under a key the store classifies as a credential.
#: Two constraints meet in this value. It is NOT credential-shaped -- no digits --
#: because the provenance posture gate reports a credential-named symbol bound to
#: a key-shaped literal, and it must not trip on a test sentinel. It is also
#: transparent to the result path's exfiltration scan, pinned by
#: :func:`test_the_planted_credential_is_transparent_to_the_result_scan`: a
#: sentinel that scan would scrub would vanish from a reply for a reason that has
#: nothing to do with elision, and the elision test would pass with the elision
#: removed.
SECRET = "conformance-sentinel-not-a-real-credential"

#: A limit value nothing else in the suite writes, so finding it in a file read
#: back off disk cannot be another test's leftover.
RETRY_LIMIT = 7

#: The approver an apply is made on the authority of.
APPROVER = "operator@example"

#: Substrings that would mean a stack trace reached the caller. Deliberately only
#: the unambiguous ones: a JSON parse error legitimately says "line 1 column 2",
#: so a marker like ``"line "`` would fail an honest refusal message.
TRACEBACK_MARKERS = ("Traceback (most recent call last)", 'File "', ", in <module>")


def make_project(root: Path) -> Path:
    """A project whose own files carry the markers the evidence must quote."""
    root.mkdir(parents=True, exist_ok=True)
    git = root / ".git"
    git.mkdir()
    (git / "config").write_text(
        f'[core]\n\trepositoryformatversion = 0\n[remote "origin"]\n\turl = {REMOTE_URL}\n',
        encoding="utf-8",
    )
    steering = root / ".kiro" / "steering"
    steering.mkdir(parents=True)
    (steering / "review.md").write_text(f"{PRACTICE_LINE}\n", encoding="utf-8")
    (root / "Makefile").write_text("build:\n\t@echo build\n\ntest:\n\t@echo test\n", "utf-8")
    return root


def answers_for(inspection: dict[str, Any]) -> dict[str, Any]:
    """A complete, consistent answer set for an inspected project.

    Every inference the inspection actually made is approved and every rung is
    declined, so the plan the answers produce depends on the project rather than
    on an autonomy grant.
    """
    return {
        "cost_profile": "budget",
        "confirmations": {level.value: False for level in CONFIRMED_LEVELS},
        "approved_subjects": [item["subject"] for item in inspection["inferences"]],
        "workflow_preset": "git-pull-request",
        "watch_source": "github",
    }


@contextmanager
def home_pinned(home: Path) -> Iterator[None]:
    """Resolve the engine's default roots under *home* for the duration.

    The child resolves its own roots from ``KIROCREW_HOME``, so this is how a test
    in this process finds the file the child wrote instead of restating the
    directory layout and drifting from it.
    """
    previous = os.environ.get("KIROCREW_HOME")
    os.environ["KIROCREW_HOME"] = str(home)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("KIROCREW_HOME", None)
        else:
            os.environ["KIROCREW_HOME"] = previous


def config_document(home: Path) -> Path:
    """The path the child's own root resolution puts ``config.json`` at under *home*."""
    with home_pinned(home):
        return ConfigStore().path


def saved_document(home: Path) -> dict[str, Any]:
    """The configuration the child persisted, read as raw JSON off the filesystem.

    Read with :func:`json.loads` rather than through ``ConfigStore``: an accessor
    can normalise, and the claim is about what landed in the file.
    """
    path = config_document(home)
    assert path.is_file(), f"no configuration document was written at {path}"
    parsed = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict)
    return parsed


def assert_no_traceback(text: str) -> None:
    """Fail when *text* carries a stack trace rather than a message."""
    found = [marker for marker in TRACEBACK_MARKERS if marker in text]
    assert not found, f"a stack trace reached the caller: {found} in {text[:400]!r}"


# --- one child for everything that neither writes nor is written to --------


@pytest.fixture(scope="module")
def fixture_project(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The inspected project. Read-only to every tool, so it is shared."""
    return make_project(tmp_path_factory.mktemp("conformance") / "sentinel-acme")


@pytest.fixture(scope="module")
def reading_home(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("conformance-home") / "home"


@pytest.fixture(scope="module")
def reading_server(reading_home: Path) -> Iterator[StdioServer]:
    """One initialized child for every case that must leave no configuration.

    Shared because starting the packaged server pays the package import each time.
    Every test using it either reads or is refused, and each asserts for itself
    that no document exists rather than relying on the order they run in.
    """
    with stdio_server(reading_home) as running:
        advertised = running.initialize()
        missing = [name for name in NEW_TOOLS if name not in advertised]
        assert not missing, f"the child does not advertise {missing}"
        yield running


@pytest.fixture(scope="module")
def inspection(reading_server: StdioServer, fixture_project: Path) -> dict[str, Any]:
    """One inspection of the fixture project, over the wire."""
    found = reading_server.tool_payload("inspect_setup", {"project": str(fixture_project)})
    assert isinstance(found, dict)
    return found


# --- advertised, with a schema that bounds the call ------------------------


class TestTheChildAdvertisesThem:
    def test_every_new_tool_is_listed_with_the_arguments_it_declares(
        self, reading_server: StdioServer
    ) -> None:
        listed = reading_server.request("tools/list")
        advertised = {tool["name"]: tool for tool in listed["result"]["tools"]}
        for name in NEW_TOOLS:
            assert name in advertised, f"{name} is not advertised over stdio"
            tool = advertised[name]
            assert tool["description"].strip(), f"{name} is advertised without a description"
            schema = tool["inputSchema"]
            assert schema["type"] == "object"
            assert set(schema["properties"]) == DECLARED_PROPERTIES[name], name
            assert tuple(schema["required"]) == DECLARED_REQUIRED[name], name
            # Closed: an argument the tool never declared is refused, not ignored.
            assert schema["additionalProperties"] is False, name

    def test_the_answer_object_is_closed_and_the_patch_is_not(
        self, reading_server: StdioServer
    ) -> None:
        # The two exceptions the tables state, and they run in opposite
        # directions. The answers a caller sends are an enumerated set, so an
        # unrecognised rung must be refused rather than dropped into an
        # "unanswered" the engine then reports as missing. A patch IS the
        # configuration document, so enumerating its shape here would be a second
        # schema to drift from the validator that decides what is accepted.
        listed = reading_server.request("tools/list")
        advertised = {tool["name"]: tool for tool in listed["result"]["tools"]}
        for name in ("plan_setup", "apply_setup"):
            answers = advertised[name]["inputSchema"]["properties"]["answers"]
            assert answers["type"] == "object", name
            assert set(answers["properties"]) == DECLARED_ANSWER_KEYS, name
            assert answers["additionalProperties"] is False, name
        patch = advertised["write_config"]["inputSchema"]["properties"]["patch"]
        assert patch["type"] == "object"
        assert patch["additionalProperties"] is True

    def test_no_tool_offers_a_caller_supplied_patch_onto_the_setup_path(
        self, reading_server: StdioServer
    ) -> None:
        # The narrowing that keeps the confirmed setup surface honest, asserted
        # against the wire: the only patch-shaped argument on the whole advertised
        # surface belongs to write_config, which writes on the unconfirmed surface.
        listed = reading_server.request("tools/list")
        for tool in listed["result"]["tools"]:
            if tool["name"] == "write_config":
                continue
            declared = set(tool["inputSchema"]["properties"])
            assert not declared & {"patch", "surface", "document"}, tool["name"]
        assert ENGINE_MCP_SURFACE.operator_confirmed is False


# --- the malformed call, in the codes the server documents ----------------


class TestMalformedCallsAreProtocolErrors:
    @pytest.mark.parametrize("name", NEW_TOOLS)
    def test_an_unknown_argument_is_invalid_params(
        self, reading_server: StdioServer, fixture_project: Path, name: str
    ) -> None:
        every = {
            "project": str(fixture_project),
            "answers": {"cost_profile": "budget"},
            "plan_id": "0" * 64,
            "approver": APPROVER,
            "patch": {"limits": {"task_retry_limit": 4}},
        }
        arguments = {key: value for key, value in every.items() if key in DECLARED_PROPERTIES[name]}
        reply = reading_server.call_tool(name, {**arguments, "unknown_argument": "x"})
        assert "result" not in reply, f"{name} accepted an argument it never declared"
        assert reply["error"]["code"] == _INVALID_PARAMS

    def test_an_unknown_key_inside_answers_is_invalid_params(
        self, reading_server: StdioServer, fixture_project: Path, inspection: dict[str, Any]
    ) -> None:
        # The nested half of the closed-schema claim: a misspelled answer key is a
        # rung nobody answered, and accepting it silently would let a caller
        # believe it confirmed something it did not.
        answers = {**answers_for(inspection), "confirmation": {"execution": True}}
        reply = reading_server.call_tool(
            "plan_setup", {"project": str(fixture_project), "answers": answers}
        )
        assert "result" not in reply
        assert reply["error"]["code"] == _INVALID_PARAMS

    @pytest.mark.parametrize("name", ["inspect_setup", "plan_setup", "apply_setup", "write_config"])
    def test_a_missing_required_argument_is_invalid_params(
        self, reading_server: StdioServer, name: str
    ) -> None:
        reply = reading_server.call_tool(name, {})
        assert "result" not in reply
        assert reply["error"]["code"] == _INVALID_PARAMS

    @pytest.mark.parametrize("name", NEW_TOOLS)
    def test_a_non_object_arguments_member_is_invalid_params(
        self, reading_server: StdioServer, name: str
    ) -> None:
        reply = reading_server.request("tools/call", {"name": name, "arguments": ["project"]})
        assert "result" not in reply
        assert reply["error"]["code"] == _INVALID_PARAMS

    def test_a_misspelled_tool_name_is_method_not_found(
        self, reading_server: StdioServer, fixture_project: Path
    ) -> None:
        reply = reading_server.call_tool("inspect_project", {"project": str(fixture_project)})
        assert "result" not in reply
        assert reply["error"]["code"] == _METHOD_NOT_FOUND

    def test_a_line_that_is_not_json_leaves_the_session_usable(
        self, reading_server: StdioServer
    ) -> None:
        # The framing case only a raw write can produce. The server documents that
        # a malformed line is skipped rather than answered, so the proof is that
        # the next real request is answered on an aligned stream -- a server that
        # died here, or replied to the garbage, shows up as a failure on the reply
        # after it rather than as a silent hang.
        reading_server.send_raw("{not json at all")
        reading_server.send_raw("[]")
        listed = reading_server.request("tools/list")
        assert {tool["name"] for tool in listed["result"]["tools"]} >= set(NEW_TOOLS)


# --- refusals are refusals -------------------------------------------------


class TestRefusalsTravelAsRefusals:
    def _refusal(self, reply: dict[str, Any], code: str) -> dict[str, Any]:
        """The refusal payload from *reply*, checked to be a result and structured."""
        assert "error" not in reply, f"a refusal arrived as a protocol error: {reply.get('error')}"
        text = str(reply["result"]["content"][0]["text"])
        assert_no_traceback(text)
        payload = json.loads(text)
        assert payload[REFUSED_KEY] == code, payload
        assert payload["reason"], "a refusal must name what refused"
        assert payload["message"].strip(), "a refusal must carry a message a human can act on"
        return dict(payload)

    def test_an_apply_without_an_approver_refuses(
        self,
        reading_server: StdioServer,
        reading_home: Path,
        fixture_project: Path,
        inspection: dict[str, Any],
    ) -> None:
        reply = reading_server.call_tool(
            "apply_setup",
            {
                "project": str(fixture_project),
                "answers": answers_for(inspection),
                "plan_id": "0" * 64,
                "approver": "   ",
            },
        )
        self._refusal(reply, REFUSAL_APPROVER_REQUIRED)
        assert not config_document(reading_home).is_file()

    def test_an_apply_quoting_a_plan_nobody_computed_refuses(
        self,
        reading_server: StdioServer,
        reading_home: Path,
        fixture_project: Path,
        inspection: dict[str, Any],
    ) -> None:
        reply = reading_server.call_tool(
            "apply_setup",
            {
                "project": str(fixture_project),
                "answers": answers_for(inspection),
                "plan_id": "0" * 64,
                "approver": APPROVER,
            },
        )
        refusal = self._refusal(reply, REFUSAL_PLAN_STALE)
        assert "0" * 64 in refusal["message"]
        assert not config_document(reading_home).is_file()

    def test_an_unanswered_rung_refuses_before_a_plan_exists(
        self,
        reading_server: StdioServer,
        reading_home: Path,
        fixture_project: Path,
        inspection: dict[str, Any],
    ) -> None:
        answers = {**answers_for(inspection), "confirmations": {CONFIRMED_LEVELS[0].value: True}}
        reply = reading_server.call_tool(
            "plan_setup", {"project": str(fixture_project), "answers": answers}
        )
        refusal = self._refusal(reply, REFUSAL_SETUP_APPROVAL)
        assert "plan_id" not in refusal, "a refused plan must not carry an identity to apply"
        assert not config_document(reading_home).is_file()

    def test_a_config_only_patch_refuses_and_names_the_fenced_paths(
        self, reading_server: StdioServer, reading_home: Path
    ) -> None:
        reply = reading_server.call_tool(
            "write_config", {"patch": {"delivery": {"auto_integrate": True}}}
        )
        refusal = self._refusal(reply, config_surface.REFUSAL_CONFIG_REFUSED)
        assert refusal["surface"] == ENGINE_MCP_SURFACE.name
        assert refusal["config_only_paths"], "a fence refusal must name what it refused"
        assert not config_document(reading_home).is_file()

    def test_a_patch_that_would_not_validate_refuses_and_names_the_key(
        self, reading_server: StdioServer, reading_home: Path
    ) -> None:
        reply = reading_server.call_tool(
            "write_config", {"patch": {"limits": {"task_retry_limit": -1}}}
        )
        refusal = self._refusal(reply, config_surface.REFUSAL_CONFIG_INVALID)
        assert [error["path"] for error in refusal["errors"]] == ["limits.task_retry_limit"]
        assert not config_document(reading_home).is_file()

    def test_a_corrupt_document_refuses_naming_the_file(self, tmp_path: Path) -> None:
        # Its own child, because the document has to be corrupt before the server
        # starts. This is get_config's error shape: the refusal a caller can relay
        # as "your configuration file is unreadable, here it is" rather than an
        # exception class with a traceback under it.
        home = tmp_path / "corrupt-home"
        path = config_document(home)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        with stdio_server(home) as running:
            running.initialize()
            reply = running.call_tool("get_config", {})
        refusal = self._refusal(reply, config_surface.REFUSAL_CONFIG_UNREADABLE)
        assert path.name in refusal["message"]


# --- non-vacuity: each tool observed doing its real work ------------------


class TestTheReadingToolsAnswerFromTheProject:
    def test_the_inspection_quotes_the_project_files_it_read(
        self, fixture_project: Path, inspection: dict[str, Any]
    ) -> None:
        # The positive control for inspect_setup. Two markers planted in two files
        # come back inside the evidence, so the reply is derived from this project
        # on disk rather than assembled from defaults that would look the same.
        assert inspection["project"] == {
            "name": "sentinel-acme",
            "root": str(fixture_project.resolve()),
        }
        inferences = {item["subject"]: item for item in inspection["inferences"]}
        assert {
            SUBJECT_WATCH_SOURCE,
            SUBJECT_WORKFLOW_PRESET,
            SUBJECT_WORKFLOW_PRACTICE,
            SUBJECT_TOOLING,
        } <= set(inferences)

        excerpts = {
            item["subject"]: [evidence["excerpt"] for evidence in item["evidence"]]
            for item in inspection["inferences"]
        }
        assert REMOTE_URL in excerpts[SUBJECT_WATCH_SOURCE]
        assert PRACTICE_LINE in excerpts[SUBJECT_WORKFLOW_PRACTICE]
        located = {
            evidence["located_at"]
            for item in inspection["inferences"]
            for evidence in item["evidence"]
        }
        assert {".git/config", "Makefile"} <= located
        assert any(name.endswith("review.md") for name in located)
        # Every inference carries the evidence it was drawn from, and the flat
        # evidence list is the same set keyed by subject.
        assert {item["subject"] for item in inspection["evidence"]} == set(inferences)

    def test_the_inspection_asks_what_it_may_not_infer(self, inspection: dict[str, Any]) -> None:
        asked = {item["subject"] for item in inspection["questions"]}
        assert SUBJECT_COST_PROFILE in asked
        for level in CONFIRMED_LEVELS:
            assert f"autonomy.{level.value}" in asked
        assert SUBJECT_COST_PROFILE in set(inspection["asked_subjects"])

    def test_a_project_with_nothing_to_read_infers_nothing(
        self, reading_server: StdioServer, tmp_path: Path
    ) -> None:
        # The other direction of the same measurement: with the markers absent the
        # evidence is absent too, so the assertions above are reading the project
        # rather than a payload the tool always returns.
        bare = tmp_path / "bare"
        bare.mkdir()
        found = reading_server.tool_payload("inspect_setup", {"project": str(bare)})
        assert found["inferences"] == []
        assert found["evidence"] == []
        assert found["offers"] == []

    def test_every_offer_names_the_commands_the_preset_would_run(
        self, inspection: dict[str, Any]
    ) -> None:
        offers = {(item["kind"], item["name"]): item for item in inspection["offers"]}
        assert offers, "a project with a known host was offered nothing"
        for key, offer in offers.items():
            assert offer["commands"], f"{key} shows nothing to approve"
            for command in offer["commands"]:
                assert command["stage"] and command["argv"]
                assert command["argv"][0] in offer["programs"]

    def test_the_returned_plan_identity_recomputes_from_the_plan_itself(
        self, reading_server: StdioServer, fixture_project: Path, inspection: dict[str, Any]
    ) -> None:
        # The positive control for plan_setup. The identity is recomputed here from
        # the three fields the reply carries, so a plan_id that was a random token,
        # a constant, or a hash over something else fails -- and the recomputation
        # is what apply_setup will do, which is what makes the two-step flow work
        # without server-side state.
        answers = answers_for(inspection)
        planned = reading_server.tool_payload(
            "plan_setup", {"project": str(fixture_project), "answers": answers}
        )
        assert planned["config_patch"], "a plan with an empty patch approves nothing"
        recomputed = plan_identity(
            subject=planned["project"],
            answers_used=planned["answers_used"],
            config_patch=planned["config_patch"],
        )
        assert planned["plan_id"] == recomputed
        # And it identifies THESE inputs: one different answer, one different id.
        other = reading_server.tool_payload(
            "plan_setup",
            {
                "project": str(fixture_project),
                "answers": {**answers, "cost_profile": "quality-first"},
            },
        )
        assert other["plan_id"] != planned["plan_id"]

    def test_planning_leaves_no_configuration_behind(self, reading_home: Path) -> None:
        assert not config_document(reading_home).is_file()


class TestTheEffectIsReadBackOffDisk:
    """Positive controls for the three tools whose effect is a file.

    Each reads ``config.json`` back off the filesystem rather than trusting the
    reply, plus the read tool's opposite case: with nothing written, the read says
    the engine is unconfigured instead of returning an empty form.
    """

    def test_an_approved_plan_is_readable_back_out_of_the_file(
        self, tmp_path: Path, fixture_project: Path
    ) -> None:
        # The positive control for apply_setup: the plan the child returned is
        # applied by identity, and the effect is read out of config.json rather
        # than believed from the reply. The project name in the document comes from
        # the fixture directory, so a canned reply cannot produce it.
        home = tmp_path / "apply-home"
        with stdio_server(home) as running:
            running.initialize()
            found = running.tool_payload("inspect_setup", {"project": str(fixture_project)})
            answers = answers_for(found)
            planned = running.tool_payload(
                "plan_setup", {"project": str(fixture_project), "answers": answers}
            )
            applied = running.tool_payload(
                "apply_setup",
                {
                    "project": str(fixture_project),
                    "answers": answers,
                    "plan_id": planned["plan_id"],
                    "approver": APPROVER,
                },
            )
        assert applied["applied"] is True
        assert applied["approver"] == APPROVER

        saved = saved_document(home)
        # The version stamp is added by ConfigStore.write and by nothing else, so
        # it is the evidence the validated door was the one used.
        assert saved["version"] >= 1
        project_entry = saved["projects"]["sentinel-acme"]
        assert project_entry["cost_profile"] == "budget"
        assert project_entry["workflow"]["preset"] == "git-pull-request"
        assert saved["sources"]["github"]["poll"]
        # The reply's own account of what it wrote agrees with the file.
        assert applied["written_paths"], "an apply that reports no path reports nothing"

    def test_a_patch_is_readable_back_out_of_the_file(self, tmp_path: Path) -> None:
        # The positive control for write_config, with a value nothing else writes.
        home = tmp_path / "write-home"
        with stdio_server(home) as running:
            running.initialize()
            written = running.tool_payload(
                "write_config",
                {"patch": {"limits": {"task_retry_limit": RETRY_LIMIT}}, "actor": APPROVER},
            )
        assert written["written"] is True
        assert written["keys"] == ["limits"]
        assert saved_document(home)["limits"]["task_retry_limit"] == RETRY_LIMIT

        # And who wrote it outlived the process that wrote it.
        with home_pinned(home):
            records = ConfigStore().writes()
        assert [record["actor"] for record in records] == [APPROVER]
        assert records[0]["surface"] == ENGINE_MCP_SURFACE.name

    def test_the_planted_credential_is_transparent_to_the_result_scan(self) -> None:
        # Non-vacuity for the elision case below. The result path also runs the
        # credential scan, so a sentinel that scan would scrub would disappear from
        # a reply whether or not anything was elided, and the test would pass with
        # the elision removed.
        assert redact(SECRET) == SECRET

    def test_the_configured_document_comes_back_with_the_credential_elided(
        self, tmp_path: Path
    ) -> None:
        # The positive control for get_config: a credential planted through the
        # write tool, then read back. Elided in the reply, present in the file --
        # both halves, because a read that returned nothing would satisfy the first
        # alone and a write that dropped the value would satisfy it too.
        home = tmp_path / "read-home"
        patch = {
            "projects": {
                "acme": {
                    "path": "/w/acme",
                    "variables": {"api_key": SECRET, "token_bucket_size": "9"},
                }
            }
        }
        with stdio_server(home) as running:
            running.initialize()
            running.tool_payload("write_config", {"patch": patch, "actor": APPROVER})
            reply = running.call_tool("get_config", {})
        assert "error" not in reply, f"the configuration read failed: {reply.get('error')}"

        serialized = json.dumps(reply)
        assert SECRET not in serialized, "the configuration read emitted a credential"
        payload = json.loads(reply["result"]["content"][0]["text"])
        assert payload["configured"] is True
        variables = payload["document"]["projects"]["acme"]["variables"]
        assert variables["api_key"] == ELIDED
        assert payload["elided"] == ["projects.acme.variables.api_key"]
        # The setting beside it is not elided: once everything reads <elided> the
        # marker stops meaning anything.
        assert variables["token_bucket_size"] == "9"
        # And the value itself is in the file, so elision is a read-path concern
        # rather than a write that dropped what a capability needs.
        assert saved_document(home)["projects"]["acme"]["variables"]["api_key"] == SECRET

    def test_an_unconfigured_engine_says_so(self, reading_server: StdioServer) -> None:
        # The first-run answer, from the child that wrote nothing: an absent file
        # and an empty document both serialize to {}, and only one of them means
        # "run the setup assistant".
        payload = reading_server.tool_payload("get_config", {})
        assert payload["configured"] is False
        assert payload["document"] == {}
        assert payload["elided"] == []
