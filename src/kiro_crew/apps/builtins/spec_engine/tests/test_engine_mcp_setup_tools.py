"""The setup assistant, reachable by an agent that has no user interface.

Three tools bridge a two-step human flow onto a stateless protocol: inspect a
project, compute the plan an operator's answers produce, then write that exact
plan on a named human's authority. What these tests hold:

* **Inspection shows its work.** The evidence read, the values inferred with that
  evidence attached, the questions that cannot be inferred, and -- for every
  offered preset -- the commands the preset would actually run, so a caller can
  see what it is approving before anything is written.
* **Planning writes nothing.** A plan is returned with a content-hash identity and
  no file appears.
* **Applying needs a human and the right plan.** No approver, or a ``plan_id``
  that is not the identity these inputs produce now, and the write does not
  happen.
* **A refusal is a refusal, not a stack trace.** The two classes the setup module
  raises come back as structured refusal payloads. Their class chains are pinned
  here on purpose: ``InferredSubjectRefused`` derives ``ValueError`` and
  ``SetupApprovalRequired`` derives ``PermissionError``, a catch clause written
  against the wrong one of those cannot catch what is raised, and the failure is
  silent -- the dominant defect class of the prior spec.
"""

from __future__ import annotations

import itertools
import json
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from kiro_crew.apps.builtins.spec_engine.engine.autonomy import AutonomyLevel
from kiro_crew.apps.builtins.spec_engine.engine.config import ConfigStore
from kiro_crew.apps.builtins.spec_engine.engine.config.profiles import (
    COST_PROFILE_PRESET_NAMES,
)
from kiro_crew.apps.builtins.spec_engine.engine.config.schema import (
    SECTION_COST_PROFILES,
    SECTION_PROJECTS,
    SECTION_SOURCES,
    SECTION_WORKFLOW,
)
from kiro_crew.apps.builtins.spec_engine.engine.config.store import SETUP_ASSISTANT_SURFACE
from kiro_crew.apps.builtins.spec_engine.engine.setup import (
    CONFIRMED_LEVELS,
    SUBJECT_COST_PROFILE,
    SUBJECT_TOOLING,
    SUBJECT_WATCH_SOURCE,
    SUBJECT_WORKFLOW_PRESET,
    InferredSubjectRefused,
    SetupApprovalRequired,
)
from kiro_crew.apps.builtins.spec_engine.engine_mcp import operations as operations_module
from kiro_crew.apps.builtins.spec_engine.engine_mcp.operations import EngineOperations
from kiro_crew.apps.builtins.spec_engine.engine_mcp.server import TOOLS, handle
from kiro_crew.apps.builtins.spec_engine.engine_mcp.setup_surface import (
    REFUSAL_APPROVER_REQUIRED,
    REFUSAL_INFERRED_SUBJECT,
    REFUSAL_PLAN_STALE,
    REFUSAL_SETUP_APPROVAL,
    REFUSED_KEY,
    ApproverRequired,
    StalePlan,
    plan_identity,
)

APPROVER = "operator@example"

#: The three tools under test, so a list-shaped assertion cannot drift from them.
SETUP_TOOLS = ("inspect_setup", "plan_setup", "apply_setup")

_INVALID_PARAMS = -32602

#: Fresh directory names for the property tests, which cannot take a
#: function-scoped fixture per example.
_UNIQUE = itertools.count()


def make_github_project(root: Path) -> Path:
    """A project whose own files state a GitHub remote and a review practice."""
    root.mkdir(parents=True, exist_ok=True)
    git = root / ".git"
    git.mkdir()
    (git / "config").write_text(
        "[core]\n\trepositoryformatversion = 0\n"
        '[remote "origin"]\n\turl = git@github.com:acme/widgets.git\n',
        encoding="utf-8",
    )
    steering = root / ".kiro" / "steering"
    steering.mkdir(parents=True)
    (steering / "review.md").write_text(
        "Changes land through a pull request on the main branch.\n", encoding="utf-8"
    )
    (root / "Makefile").write_text("build:\n\t@echo build\n\ntest:\n\t@echo test\n", "utf-8")
    return root


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    return make_github_project(tmp_path / "acme")


@pytest.fixture()
def config_root(tmp_path: Path) -> Path:
    return tmp_path / "config"


@pytest.fixture()
def ops(tmp_path: Path, config_root: Path) -> EngineOperations:
    return EngineOperations(
        state_root=tmp_path / "state",
        audit_root=tmp_path / "audit",
        config_root=config_root,
    )


def reply(name: str, arguments: dict[str, Any], engine: EngineOperations) -> dict[str, Any]:
    """The raw JSON-RPC reply for one tool call, over the server's own dispatch."""
    answer = handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
        ops=engine,
    )
    assert answer is not None, f"{name} produced no reply"
    return answer


def payload(name: str, arguments: dict[str, Any], engine: EngineOperations) -> dict[str, Any]:
    """One tool's decoded result, failing on a protocol error reply."""
    answer = reply(name, arguments, engine)
    assert "error" not in answer, f"{name} failed: {answer.get('error')}"
    decoded = json.loads(answer["result"]["content"][0]["text"])
    assert isinstance(decoded, dict)
    return decoded


def answers_for(inspection: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    """A complete, consistent answer set for an inspected project."""
    answers: dict[str, Any] = {
        "cost_profile": "budget",
        "confirmations": {level.value: False for level in CONFIRMED_LEVELS},
        "approved_subjects": [item["subject"] for item in inspection["inferences"]],
        "workflow_preset": "git-pull-request",
        "watch_source": "github",
    }
    answers.update(overrides)
    return answers


# --- inspection ------------------------------------------------------------


class TestInspection:
    def test_it_returns_the_evidence_the_inferences_the_questions(
        self, ops: EngineOperations, project: Path
    ) -> None:
        found = payload("inspect_setup", {"project": str(project)}, ops)

        assert found["project"] == {"name": "acme", "root": str(project.resolve())}
        subjects = {item["subject"] for item in found["inferences"]}
        assert {SUBJECT_WATCH_SOURCE, SUBJECT_WORKFLOW_PRESET, SUBJECT_TOOLING} <= subjects
        # Every inference carries its evidence, and the gathered evidence is also
        # its own list keyed by the subject it supported.
        for inference in found["inferences"]:
            assert inference["evidence"], f"{inference['subject']} came back without evidence"
        assert {item["subject"] for item in found["evidence"]} == subjects
        for item in found["evidence"]:
            assert item["located_at"] and item["excerpt"]

        asked = {item["subject"] for item in found["questions"]}
        assert SUBJECT_COST_PROFILE in asked
        for level in CONFIRMED_LEVELS:
            assert f"autonomy.{level.value}" in asked
        # A rung is a yes/no; the cost profile is a choice among named options.
        by_subject = {item["subject"]: item for item in found["questions"]}
        assert by_subject[SUBJECT_COST_PROFILE]["answer_kind"] == "choice"
        assert by_subject[SUBJECT_COST_PROFILE]["options"] == list(COST_PROFILE_PRESET_NAMES)
        assert by_subject[f"autonomy.{CONFIRMED_LEVELS[0].value}"]["answer_kind"] == "confirmation"
        assert set(found["asked_subjects"]) >= asked - {SUBJECT_WATCH_SOURCE, SUBJECT_TOOLING}

    def test_every_offer_names_the_programs_the_preset_would_run(
        self, ops: EngineOperations, project: Path
    ) -> None:
        found = payload("inspect_setup", {"project": str(project)}, ops)
        offers = {(item["kind"], item["name"]): item for item in found["offers"]}
        assert set(offers) == {
            (SECTION_WORKFLOW, "git-pull-request"),
            (SECTION_SOURCES, "github"),
        }

        workflow = offers[(SECTION_WORKFLOW, "git-pull-request")]
        # Named against this offer specifically: the source offer also runs 'gh',
        # so a payload-wide assertion would stay green with the workflow commands
        # dropped entirely.
        assert "git" in workflow["programs"]
        assert "gh" in workflow["programs"]
        assert workflow["commands"], "an offer with no commands shows nothing to approve"
        for command in workflow["commands"]:
            assert command["stage"] and command["argv"]
            assert command["argv"][0] in workflow["programs"]
        # A workflow offer also carries the renameable definition and says the
        # bundled name is reserved.
        assert workflow["definition"]["stages"]
        assert "reserved" in workflow["copy_note"]

        source = offers[(SECTION_SOURCES, "github")]
        assert source["programs"] == ["gh"]
        assert [command["stage"] for command in source["commands"]] == ["poll"]
        assert "definition" not in source

    def test_the_programs_reported_are_the_ones_the_write_would_configure(
        self, ops: EngineOperations, project: Path
    ) -> None:
        # The offer report and the patch must name one set of commands. If they
        # could disagree, a caller would approve one pipeline and configure
        # another -- which is the whole reason the programs are read out of the
        # bundled tables rather than restated.
        found = payload("inspect_setup", {"project": str(project)}, ops)
        planned = payload(
            "plan_setup", {"project": str(project), "answers": answers_for(found)}, ops
        )
        offered = next(
            item
            for item in found["offers"]
            if (item["kind"], item["name"]) == (SECTION_WORKFLOW, "git-pull-request")
        )
        configured = planned["config_patch"][SECTION_PROJECTS]["acme"][SECTION_WORKFLOW]["stages"]
        from_patch = sorted(
            {str(argv[0]) for argvs in configured.values() for argv in argvs if argv}
        )
        assert from_patch == sorted(set(offered["programs"]))

    def test_inspection_writes_no_configuration(
        self, ops: EngineOperations, project: Path, config_root: Path
    ) -> None:
        payload("inspect_setup", {"project": str(project)}, ops)
        assert not (config_root / "config.json").exists()

    def test_a_bare_project_asks_rather_than_inventing_offers(
        self, ops: EngineOperations, tmp_path: Path
    ) -> None:
        bare = tmp_path / "bare"
        bare.mkdir()
        found = payload("inspect_setup", {"project": str(bare)}, ops)
        assert found["inferences"] == []
        assert found["offers"] == []
        assert found["memory_consulted"] is False
        assert SUBJECT_WORKFLOW_PRESET in {item["subject"] for item in found["questions"]}

    def test_an_explicit_name_is_used_and_the_directory_is_the_fallback(
        self, ops: EngineOperations, project: Path
    ) -> None:
        assert payload("inspect_setup", {"project": str(project)}, ops)["project"]["name"] == "acme"
        named = payload("inspect_setup", {"project": str(project), "name": "widgets"}, ops)
        assert named["project"]["name"] == "widgets"


# --- planning applies nothing ----------------------------------------------


class TestPlanning:
    def test_a_plan_is_returned_and_nothing_is_written(
        self, ops: EngineOperations, project: Path, config_root: Path
    ) -> None:
        found = payload("inspect_setup", {"project": str(project)}, ops)
        planned = payload(
            "plan_setup", {"project": str(project), "answers": answers_for(found)}, ops
        )

        assert planned["plan_id"]
        assert planned["config_patch"][SECTION_COST_PROFILES]["budget"]["roles"]
        assert planned["config_patch"][SECTION_SOURCES]["github"]["poll"]
        assert f"{SECTION_SOURCES}.github" in planned["written_paths"]
        assert planned["answers_used"]["cost_profile"] == "budget"
        # The evidence that nothing was applied: no document at all, not merely a
        # document without these sections.
        assert not (config_root / "config.json").exists()
        assert ConfigStore(config_root).document() == {}

    def test_the_same_inputs_always_identify_the_same_plan(
        self, ops: EngineOperations, project: Path
    ) -> None:
        found = payload("inspect_setup", {"project": str(project)}, ops)
        answers = answers_for(found)
        first = payload("plan_setup", {"project": str(project), "answers": answers}, ops)
        # Key order differs, structure does not: a caller's JSON object order must
        # not change the identity.
        reordered = dict(reversed(list(answers.items())))
        second = payload("plan_setup", {"project": str(project), "answers": reordered}, ops)
        assert first["plan_id"] == second["plan_id"]

    @pytest.mark.parametrize(
        "overrides",
        [
            {"cost_profile": "quality-first"},
            {"watch_source": None},
            {"workflow_preset": None},
            {"confirmations": {level.value: True for level in CONFIRMED_LEVELS}},
        ],
    )
    def test_a_different_answer_identifies_a_different_plan(
        self, ops: EngineOperations, project: Path, overrides: dict[str, Any]
    ) -> None:
        found = payload("inspect_setup", {"project": str(project)}, ops)
        base = payload("plan_setup", {"project": str(project), "answers": answers_for(found)}, ops)
        changed = payload(
            "plan_setup", {"project": str(project), "answers": answers_for(found, **overrides)}, ops
        )
        assert changed["plan_id"] != base["plan_id"]

    def test_a_different_project_name_identifies_a_different_plan(
        self, ops: EngineOperations, project: Path
    ) -> None:
        found = payload("inspect_setup", {"project": str(project)}, ops)
        answers = answers_for(found)
        base = payload("plan_setup", {"project": str(project), "answers": answers}, ops)
        renamed = payload(
            "plan_setup", {"project": str(project), "answers": answers, "name": "widgets"}, ops
        )
        assert renamed["plan_id"] != base["plan_id"]

    def test_an_unanswered_rung_is_refused_before_a_plan_exists(
        self, ops: EngineOperations, project: Path
    ) -> None:
        found = payload("inspect_setup", {"project": str(project)}, ops)
        partial = answers_for(found, confirmations={AutonomyLevel.EXECUTION.value: True})
        refused = payload("plan_setup", {"project": str(project), "answers": partial}, ops)
        assert refused[REFUSED_KEY] == REFUSAL_SETUP_APPROVAL
        assert AutonomyLevel.DELIVERY.value in refused["message"]
        assert "plan_id" not in refused

    def test_a_preset_that_was_never_offered_is_refused(
        self, ops: EngineOperations, project: Path
    ) -> None:
        found = payload("inspect_setup", {"project": str(project)}, ops)
        refused = payload(
            "plan_setup",
            {"project": str(project), "answers": answers_for(found, workflow_preset="local-only")},
            ops,
        )
        assert refused[REFUSED_KEY] == REFUSAL_SETUP_APPROVAL
        assert "was not offered" in refused["message"]

    def test_an_unapproved_inference_is_refused(self, ops: EngineOperations, project: Path) -> None:
        found = payload("inspect_setup", {"project": str(project)}, ops)
        refused = payload(
            "plan_setup",
            {"project": str(project), "answers": answers_for(found, approved_subjects=[])},
            ops,
        )
        assert refused[REFUSED_KEY] == REFUSAL_SETUP_APPROVAL
        assert "has not approved" in refused["message"]

    @pytest.mark.parametrize(
        "answers",
        [
            {"confirmations": {"omniscience": True}},
            {"confirmations": {"authoring": True}},
            {"confirmations": {"execution": "yes"}},
            {"cost_profile": "cheap-and-fast"},
            {"approved_subjects": [SUBJECT_COST_PROFILE]},
            {"approved_subjects": "workflow.preset"},
        ],
    )
    def test_a_malformed_answer_is_a_client_error_not_a_refusal(
        self, ops: EngineOperations, project: Path, answers: dict[str, Any]
    ) -> None:
        # These are not decisions an operator has to make -- they are calls the
        # caller got wrong -- so they come back as invalid arguments naming what
        # exists, not as a refusal a caller might retry unchanged.
        found = payload("inspect_setup", {"project": str(project)}, ops)
        answer = reply(
            "plan_setup", {"project": str(project), "answers": answers_for(found, **answers)}, ops
        )
        assert answer["error"]["code"] == _INVALID_PARAMS


# --- applying requires a human and the plan they read ----------------------


class TestApplying:
    def _plan(self, ops: EngineOperations, project: Path) -> dict[str, Any]:
        found = payload("inspect_setup", {"project": str(project)}, ops)
        answers = answers_for(found)
        planned = payload("plan_setup", {"project": str(project), "answers": answers}, ops)
        return {"project": str(project), "answers": answers, "plan_id": planned["plan_id"]}

    def test_an_approved_plan_lands_through_the_validated_write_path(
        self, ops: EngineOperations, project: Path, config_root: Path
    ) -> None:
        applied = payload("apply_setup", {**self._plan(ops, project), "approver": APPROVER}, ops)

        assert applied["applied"] is True
        assert applied["approver"] == APPROVER
        store = ConfigStore(config_root)
        saved = json.loads(store.path.read_text(encoding="utf-8"))
        # The version stamp is added by ConfigStore.write and by nothing else, so
        # it is the evidence the validated door was the door used.
        assert saved["version"] >= 1
        assert store.validate() == ()
        assert saved[SECTION_PROJECTS]["acme"]["cost_profile"] == "budget"
        assert saved[SECTION_PROJECTS]["acme"][SECTION_WORKFLOW]["preset"] == "git-pull-request"
        assert saved[SECTION_SOURCES]["github"]["poll"]

    def test_the_write_path_is_the_setup_assistants_own_surface(self) -> None:
        # The setup patch necessarily touches config-only paths (a project's
        # workflow, a source's autonomy grid), so the write cannot go through the
        # unconfirmed engine-MCP surface. The approver argument is what authorizes
        # the confirmed one, which is why an apply without an approver is refused
        # before the store is reached.
        assert SETUP_ASSISTANT_SURFACE.operator_confirmed is True
        assert operations_module.ENGINE_MCP_SURFACE.operator_confirmed is False

    def test_no_caller_supplied_patch_reaches_the_confirmed_surface(self) -> None:
        # The narrowing that keeps the confirmed surface honest: the tool takes a
        # project, answers, an identity and an approver, and the patch is built by
        # the engine from an offered plan. A `patch`-shaped argument here would
        # turn the approver into a key that unlocks arbitrary configuration.
        for name in SETUP_TOOLS:
            declared = set(TOOLS[name].properties)
            assert not declared & {"patch", "config", "document", "surface"}
            assert declared <= {"project", "name", "answers", "plan_id", "approver"}

    @pytest.mark.parametrize("approver", ["", "   ", "\t\n"])
    def test_an_apply_without_an_approver_refuses_and_writes_nothing(
        self, ops: EngineOperations, project: Path, config_root: Path, approver: str
    ) -> None:
        refused = payload("apply_setup", {**self._plan(ops, project), "approver": approver}, ops)
        assert refused[REFUSED_KEY] == REFUSAL_APPROVER_REQUIRED
        assert not (config_root / "config.json").exists()

    def test_a_missing_approver_argument_is_refused_by_the_schema(
        self, ops: EngineOperations, project: Path, config_root: Path
    ) -> None:
        call = self._plan(ops, project)
        answer = reply("apply_setup", call, ops)
        assert answer["error"]["code"] == _INVALID_PARAMS
        assert not (config_root / "config.json").exists()

    def test_a_stale_plan_id_refuses_and_writes_nothing(
        self, ops: EngineOperations, project: Path, config_root: Path
    ) -> None:
        call = self._plan(ops, project)
        refused = payload("apply_setup", {**call, "plan_id": "0" * 64, "approver": APPROVER}, ops)
        assert refused[REFUSED_KEY] == REFUSAL_PLAN_STALE
        assert call["plan_id"] in refused["message"]
        assert not (config_root / "config.json").exists()

    def test_a_plan_id_for_different_answers_refuses(
        self, ops: EngineOperations, project: Path, config_root: Path
    ) -> None:
        # The realistic stale case: the caller read one plan and applies it with
        # the answers of another. Nothing about the identity is a secret, so this
        # is what the check is actually for.
        call = self._plan(ops, project)
        other = dict(call["answers"], cost_profile="quality-first")
        refused = payload("apply_setup", {**call, "answers": other, "approver": APPROVER}, ops)
        assert refused[REFUSED_KEY] == REFUSAL_PLAN_STALE
        assert not (config_root / "config.json").exists()

    def test_a_plan_computed_before_the_project_changed_refuses(
        self, ops: EngineOperations, project: Path, config_root: Path
    ) -> None:
        # The identity covers the patch, and the patch carries the project root, so
        # a plan computed for one checkout cannot be applied to another. This is
        # the case a stored plan handle would have gotten wrong.
        call = self._plan(ops, project)
        moved = make_github_project(project.parent / "acme-clone")
        refused = payload("apply_setup", {**call, "project": str(moved), "approver": APPROVER}, ops)
        assert refused[REFUSED_KEY] == REFUSAL_PLAN_STALE
        assert not (config_root / "config.json").exists()

    def test_the_engines_own_approval_gates_still_refuse_at_apply(
        self, ops: EngineOperations, project: Path, config_root: Path
    ) -> None:
        # An apply is not a way around the gates plan_setup enforces: the plan is
        # recomputed, so the same refusal arrives even with an approver present.
        found = payload("inspect_setup", {"project": str(project)}, ops)
        partial = answers_for(found, confirmations={AutonomyLevel.EXECUTION.value: True})
        refused = payload(
            "apply_setup",
            {
                "project": str(project),
                "answers": partial,
                "plan_id": "irrelevant",
                "approver": APPROVER,
            },
            ops,
        )
        assert refused[REFUSED_KEY] == REFUSAL_SETUP_APPROVAL
        assert not (config_root / "config.json").exists()

    def test_the_applied_document_is_what_the_library_would_have_written(
        self, ops: EngineOperations, project: Path, config_root: Path, tmp_path: Path
    ) -> None:
        # One engine, two front doors. The tool must not be a second
        # implementation of the flow, so the file it produces is compared byte for
        # byte with the file the library produces for the same answers.
        from kiro_crew.apps.builtins.spec_engine.engine.setup import (
            SetupAnswers,
            apply_setup,
            propose_setup,
        )

        payload("apply_setup", {**self._plan(ops, project), "approver": APPROVER}, ops)

        library_root = tmp_path / "library-config"
        plan = propose_setup(project.resolve(), project="acme")
        apply_setup(
            ConfigStore(library_root),
            plan,
            SetupAnswers(
                cost_profile="budget",
                confirmations={level: False for level in CONFIRMED_LEVELS},
                approved_subjects=frozenset(item.subject for item in plan.inferences),
                workflow_preset="git-pull-request",
                watch_source="github",
            ),
        )
        assert (config_root / "config.json").read_bytes() == (
            library_root / "config.json"
        ).read_bytes()


# --- the refusal classes, traced by their real chains ----------------------


class TestRefusalsAreStructuredNotStackTraces:
    def arguments_for(self, tool: str, project: Path) -> dict[str, Any]:
        """The declared arguments *tool* needs, so one case can drive all three."""
        every = {
            "project": str(project),
            "answers": answers_for({"inferences": []}, approved_subjects=[]),
            "plan_id": "0" * 64,
            "approver": APPROVER,
        }
        return {key: value for key, value in every.items() if key in TOOLS[tool].properties}

    def test_the_class_chains_the_catch_clauses_are_written_against(self) -> None:
        # Pinned because a catch tuple that cannot catch what is raised fails
        # silently: the refusal becomes an internal error, the message says
        # otherwise, and no test notices. If either chain moves, the clauses in
        # the server and the code table have to be re-read.
        assert issubclass(InferredSubjectRefused, ValueError)
        assert issubclass(SetupApprovalRequired, PermissionError)
        assert issubclass(SetupApprovalRequired, OSError)
        assert not issubclass(SetupApprovalRequired, ValueError)
        # The boundary's own two refusals are the engine's refusal, so a catch of
        # the engine class keeps catching them.
        assert issubclass(ApproverRequired, SetupApprovalRequired)
        assert issubclass(StalePlan, SetupApprovalRequired)

    @pytest.mark.parametrize("tool", SETUP_TOOLS)
    def test_a_raised_inferred_subject_refusal_surfaces_as_a_refusal(
        self, ops: EngineOperations, project: Path, monkeypatch: pytest.MonkeyPatch, tool: str
    ) -> None:
        # The real class, raised from the real seam every setup tool goes through.
        # No inspection this engine performs can produce it -- Inference refuses
        # the subject at construction -- so the only way to prove the catch clause
        # engages is to make the setup path raise it.
        def refuse(*_args: Any, **_kwargs: Any) -> None:
            raise InferredSubjectRefused(SUBJECT_COST_PROFILE)

        monkeypatch.setattr(operations_module, "propose_setup", refuse)
        refused = payload(tool, self.arguments_for(tool, project), ops)
        assert refused[REFUSED_KEY] == REFUSAL_INFERRED_SUBJECT
        assert refused["reason"] == "InferredSubjectRefused"
        assert SUBJECT_COST_PROFILE in refused["message"]

    @pytest.mark.parametrize("tool", ["plan_setup", "apply_setup"])
    def test_a_raised_setup_approval_refusal_surfaces_as_a_refusal(
        self, ops: EngineOperations, project: Path, monkeypatch: pytest.MonkeyPatch, tool: str
    ) -> None:
        # A PermissionError subclass: it is NOT caught by a (ValueError, KeyError)
        # clause, so without an explicit clause this arrives as an internal error
        # with a class name in it.
        def refuse(*_args: Any, **_kwargs: Any) -> None:
            raise SetupApprovalRequired("the operator has not chosen a cost profile")

        monkeypatch.setattr(operations_module, "setup_patch", refuse)
        refused = payload(tool, self.arguments_for(tool, project), ops)
        assert refused[REFUSED_KEY] == REFUSAL_SETUP_APPROVAL
        assert refused["reason"] == "SetupApprovalRequired"
        assert "cost profile" in refused["message"]

    def test_an_unexpected_failure_is_a_tool_error_not_a_refusal(
        self, ops: EngineOperations, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The other direction: a broken setup path must not be dressed up as a
        # decision the engine made, or a caller reads "refused" and asks a human to
        # answer differently while the real fault goes unreported.
        def explode(*_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("the steering directory is on fire")

        monkeypatch.setattr(operations_module, "propose_setup", explode)
        answer = reply("inspect_setup", {"project": str(project)}, ops)
        assert "error" in answer
        assert "RuntimeError" in answer["error"]["message"]


# --- the tools are advertised ---------------------------------------------


def test_every_setup_tool_is_advertised_with_a_closed_schema() -> None:
    listed = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert listed is not None
    advertised = {tool["name"]: tool for tool in listed["result"]["tools"]}
    for name in SETUP_TOOLS:
        assert name in advertised, f"{name} is not advertised"
        schema = advertised[name]["inputSchema"]
        assert schema["additionalProperties"] is False
        assert "project" in schema["required"]
        assert advertised[name]["description"].strip()
    assert set(advertised["apply_setup"]["inputSchema"]["required"]) == {
        "project",
        "answers",
        "plan_id",
        "approver",
    }


# --- Property 2: plan identity is total over its inputs -------------------

_SCALARS = st.one_of(st.text(max_size=6), st.integers(min_value=-50, max_value=50), st.none())
_VALUES = st.one_of(_SCALARS, st.lists(_SCALARS, max_size=3))
_OBJECTS = st.dictionaries(st.text(min_size=1, max_size=5), _VALUES, max_size=4)
_INPUTS = st.tuples(_OBJECTS, _OBJECTS, _OBJECTS)


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _identity(inputs: tuple[Any, Any, Any]) -> str:
    subject, answers, patch = inputs
    return plan_identity(subject=subject, answers_used=answers, config_patch=patch)


@settings(max_examples=250, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(first=_INPUTS, second=_INPUTS)
def test_plan_identity_is_equal_exactly_when_the_canonical_inputs_are(
    first: tuple[Any, Any, Any], second: tuple[Any, Any, Any]
) -> None:
    # Both directions, which is what "total" means here: equal inputs may not
    # produce two identities (the apply would always be stale), and different
    # inputs may not produce one (an apply would write a plan nobody read).
    same_inputs = _canonical(first) == _canonical(second)
    assert (_identity(first) == _identity(second)) is same_inputs


@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(inputs=_INPUTS)
def test_plan_identity_ignores_the_order_keys_arrived_in(inputs: tuple[Any, Any, Any]) -> None:
    def reorder(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: reorder(item) for key, item in reversed(list(value.items()))}
        return value

    shuffled = tuple(reorder(part) for part in inputs)
    assert _identity(shuffled) == _identity(inputs)  # type: ignore[arg-type]


#: Answer sets a caller can legitimately submit: a bundled profile, a ladder
#: prefix of confirmations (a rung confirmed above a declined one is refused), and
#: either preset selected or not.
_VALID_ANSWERS = st.builds(
    lambda profile, granted, workflow, source: {
        "cost_profile": profile,
        "confirmations": {
            level.value: index < granted for index, level in enumerate(CONFIRMED_LEVELS)
        },
        "approved_subjects": [SUBJECT_WATCH_SOURCE, SUBJECT_WORKFLOW_PRESET, SUBJECT_TOOLING],
        "workflow_preset": "git-pull-request" if workflow else None,
        "watch_source": "github" if source else None,
    },
    profile=st.sampled_from(COST_PROFILE_PRESET_NAMES),
    granted=st.integers(min_value=0, max_value=len(CONFIRMED_LEVELS)),
    workflow=st.booleans(),
    source=st.booleans(),
)


@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(first=_VALID_ANSWERS, second=_VALID_ANSWERS)
def test_two_answer_sets_identify_one_plan_exactly_when_they_are_the_same_answers(
    tmp_path_factory: pytest.TempPathFactory, first: dict[str, Any], second: dict[str, Any]
) -> None:
    # The property at the tool boundary rather than on the hash alone: real
    # answers, the real plan builder, and the identity the apply will compare.
    root = tmp_path_factory.mktemp("identity")
    engine = EngineOperations(
        state_root=root / "state", audit_root=root / "audit", config_root=root / "config"
    )
    project = make_github_project(root / "acme")
    one = payload("plan_setup", {"project": str(project), "answers": first}, engine)
    other = payload("plan_setup", {"project": str(project), "answers": second}, engine)
    assert (one["plan_id"] == other["plan_id"]) is (
        _canonical(one["answers_used"]) == _canonical(other["answers_used"])
    )


@settings(
    max_examples=60,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(supplied=st.text(max_size=70))
def test_a_stale_plan_id_always_refuses_and_never_writes(
    tmp_path: Path, project: Path, supplied: str
) -> None:
    # Whatever a caller quotes, it either is the identity these inputs produce or
    # the apply refuses. A fresh config root per example, so "nothing was written"
    # is a claim about this call and not about a directory an earlier example left
    # empty.
    root = tmp_path / f"stale-{next(_UNIQUE)}"
    engine = EngineOperations(
        state_root=root / "state", audit_root=root / "audit", config_root=root / "config"
    )
    found = payload("inspect_setup", {"project": str(project)}, engine)
    answers = answers_for(found)
    real = payload("plan_setup", {"project": str(project), "answers": answers}, engine)["plan_id"]
    assume(supplied.strip() != real)

    refused = payload(
        "apply_setup",
        {
            "project": str(project),
            "answers": answers,
            "plan_id": supplied,
            "approver": APPROVER,
        },
        engine,
    )
    assert refused[REFUSED_KEY] == REFUSAL_PLAN_STALE
    assert not (root / "config" / "config.json").exists()
