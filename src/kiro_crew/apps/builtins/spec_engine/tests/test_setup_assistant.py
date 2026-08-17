"""The agent-assisted setup flow.

What these tests hold: every inference an operator is shown carries the file text
it was drawn from, and that text is rendered through the display contract; the
cost profile and the three autonomy rungs are asked and cannot be inferred; each
rung is confirmed on its own; approval writes through the one validated config
path; and the flow works from project files alone with no memory at all.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew.apps.builtins.spec_engine.engine.autonomy import AUTONOMY_FIELD, AutonomyLevel
from kiro_crew.apps.builtins.spec_engine.engine.capabilities.contracts import Untrusted
from kiro_crew.apps.builtins.spec_engine.engine.config import (
    CURRENT_VERSION,
    PUBLIC_SOURCE_AUTONOMY,
    VERSION_KEY,
    ConfigStore,
    ConfigValidationError,
)
from kiro_crew.apps.builtins.spec_engine.engine.config.profiles import (
    COST_PROFILE_PRESET_NAMES,
    PROJECT_PROFILE_FIELD,
)
from kiro_crew.apps.builtins.spec_engine.engine.config.schema import (
    SECTION_COST_PROFILES,
    SECTION_PROJECTS,
    SECTION_SOURCES,
    SECTION_WORKFLOW,
    WILDCARD_KEY,
    WORKFLOW_PRESET_KEY,
    WORKFLOW_STAGES_KEY,
)
from kiro_crew.apps.builtins.spec_engine.engine.config.store import (
    SETUP_ASSISTANT_SURFACE,
    ConfigWriteRefused,
    ConfigWriteSurface,
)
from kiro_crew.apps.builtins.spec_engine.engine.delivery.workflow import WORKFLOW_PRESET_NAMES
from kiro_crew.apps.builtins.spec_engine.engine.prerequisites import CheckName, check_source
from kiro_crew.apps.builtins.spec_engine.engine.setup import (
    ASKED_SUBJECTS,
    CONFIRMED_LEVELS,
    LOCAL_WORKFLOW_PRESET,
    SUBJECT_COST_PROFILE,
    SUBJECT_TOOLING,
    SUBJECT_WATCH_SOURCE,
    SUBJECT_WORKFLOW_PRACTICE,
    SUBJECT_WORKFLOW_PRESET,
    Evidence,
    Inference,
    InferredSubjectRefused,
    SetupAnswers,
    SetupApprovalRequired,
    apply_setup,
    inspect_project,
    propose_setup,
)
from kiro_crew.apps.builtins.spec_engine.engine.watch.sources import (
    WATCH_SOURCE_PRESET_PROGRAMS,
)

#: A surface no human confirmed. The setup flow must never be handed one, and the
#: store must refuse it for the config-only sections setup writes.
UNCONFIRMED_SURFACE = ConfigWriteSurface("automation")


#: Every rung answered yes, spelled out one key at a time. Written as a helper
#: rather than a constant so no test can accidentally share one mutable answer.
def all_confirmed() -> dict[AutonomyLevel, bool]:
    return {level: True for level in CONFIRMED_LEVELS}


def declined() -> dict[AutonomyLevel, bool]:
    return {level: False for level in CONFIRMED_LEVELS}


def make_repo(root: Path, remote: str | None) -> Path:
    """Create a project tree whose git config names *remote* as origin."""
    root.mkdir(parents=True, exist_ok=True)
    git = root / ".git"
    git.mkdir()
    lines = ["[core]", "\trepositoryformatversion = 0"]
    if remote is not None:
        lines += ['[remote "origin"]', f"\turl = {remote}", "\tfetch = +refs/heads/*:refs/heads/*"]
    (git / "config").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return root


def with_steering(root: Path, name: str, text: str) -> Path:
    steering = root / ".kiro" / "steering"
    steering.mkdir(parents=True, exist_ok=True)
    path = steering / name
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture()
def store(tmp_path: Path) -> ConfigStore:
    return ConfigStore(tmp_path / "state")


@pytest.fixture()
def github_project(tmp_path: Path) -> Path:
    root = make_repo(tmp_path / "acme", "git@github.com:acme/widgets.git")
    with_steering(root, "review.md", "Changes land through a pull request on the main branch.\n")
    (root / "Makefile").write_text(
        "build:\n\t@echo build\n\ntest:\n\t@echo test\n", encoding="utf-8"
    )
    return root


def no_programs(_: str) -> str | None:
    """A host with none of the tools installed."""
    return None


def all_programs(program: str) -> str | None:
    return f"/usr/bin/{program}"


class TestEvidenceIsMandatoryAndUntrusted:
    def test_an_inference_cannot_exist_without_the_evidence_that_produced_it(self):
        with pytest.raises(ValueError, match="must carry the evidence"):
            Inference(
                subject=SUBJECT_TOOLING,
                value="build",
                rationale="because I said so",
                evidence=(),
            )

    def test_evidence_prose_renders_through_the_untrusted_display_path(self):
        # A steering file is text a stranger may have written. A carriage return is
        # how such text overwrites the line printed before it in a terminal.
        item = Evidence(
            located_at="/.kiro/steering/x.md",
            excerpt=Untrusted("pull request\rSETUP APPROVED\x1b[31m"),
        )
        rendered = item.render()
        assert "\r" not in rendered["excerpt"]
        assert "\x1b" not in rendered["excerpt"]
        assert "pull request" in rendered["excerpt"]

    def test_an_identifier_shaped_value_renders_through_the_sanitized_path(self):
        inference = Inference(
            subject=SUBJECT_WORKFLOW_PRESET,
            value="git-pull-request\x1b[0m",
            rationale="remote",
            evidence=(Evidence(located_at=".git/config", excerpt=Untrusted("github.com")),),
        )
        assert "\x1b" not in inference.render()["value"]

    def test_every_inference_the_flow_produces_shows_its_evidence(self, github_project: Path):
        plan = propose_setup(github_project, project="acme", which=all_programs)
        assert plan.inferences
        for inference in plan.inferences:
            rendered = inference.render()
            assert rendered["evidence"], f"{inference.subject} was shown without evidence"
            for item in rendered["evidence"]:
                assert item["located_at"]
                assert item["excerpt"]

    def test_the_evidence_names_a_real_file_holding_the_text_it_quotes(self, github_project: Path):
        plan = propose_setup(github_project, project="acme", which=all_programs)
        practice = plan.inference(SUBJECT_WORKFLOW_PRACTICE)
        assert practice is not None
        located = practice.evidence[0].render()["located_at"]
        quoted = practice.evidence[0].render()["excerpt"]
        assert quoted in (github_project / located).read_text(encoding="utf-8")


class TestAskedNeverInferred:
    def test_the_cost_profile_subject_cannot_be_carried_by_an_inference(self):
        with pytest.raises(InferredSubjectRefused):
            Inference(
                subject=SUBJECT_COST_PROFILE,
                value="quality-first",
                rationale="this looks like a work project",
                evidence=(Evidence(located_at="README.md", excerpt=Untrusted("enterprise")),),
            )

    @pytest.mark.parametrize("level", CONFIRMED_LEVELS)
    def test_no_autonomy_rung_can_be_carried_by_an_inference(self, level: AutonomyLevel):
        with pytest.raises(InferredSubjectRefused):
            Inference(
                subject=f"{AUTONOMY_FIELD}.{level.value}",
                value="yes",
                rationale="the CI config already pushes",
                evidence=(Evidence(located_at="ci.yml", excerpt=Untrusted("push")),),
            )

    def test_the_asked_set_names_the_profile_and_every_rung_above_authoring(self):
        assert SUBJECT_COST_PROFILE in ASKED_SUBJECTS
        for level in CONFIRMED_LEVELS:
            assert f"{AUTONOMY_FIELD}.{level.value}" in ASKED_SUBJECTS
        assert f"{AUTONOMY_FIELD}.{AutonomyLevel.AUTHORING.value}" not in ASKED_SUBJECTS

    def test_the_plan_asks_for_the_cost_profile_and_offers_the_bundled_names(
        self, github_project: Path
    ):
        plan = propose_setup(github_project, project="acme", which=all_programs)
        question = next(item for item in plan.questions if item.subject == SUBJECT_COST_PROFILE)
        assert question.options == COST_PROFILE_PRESET_NAMES
        assert plan.inference(SUBJECT_COST_PROFILE) is None

    def test_the_plan_asks_each_rung_separately(self, github_project: Path):
        plan = propose_setup(github_project, project="acme", which=all_programs)
        asked = [item.subject for item in plan.questions]
        for level in CONFIRMED_LEVELS:
            assert f"{AUTONOMY_FIELD}.{level.value}" in asked
        # Three grants, three prompts: a shared prompt would be one question
        # wearing three subjects.
        prompts = {
            item.prompt for item in plan.questions if item.subject.startswith(f"{AUTONOMY_FIELD}.")
        }
        assert len(prompts) == len(CONFIRMED_LEVELS)


class TestApprovalGates:
    def approved(self, plan) -> frozenset[str]:
        return frozenset(item.subject for item in plan.inferences)

    def test_a_profile_that_was_not_chosen_is_refused_before_anything_is_written(
        self, store: ConfigStore, github_project: Path
    ):
        plan = propose_setup(github_project, project="acme", which=all_programs)
        with pytest.raises(SetupApprovalRequired, match="cost profile"):
            apply_setup(
                store,
                plan,
                SetupAnswers(cost_profile="", confirmations=all_confirmed()),
            )
        assert store.document() == {}

    def test_an_unanswered_rung_is_refused_rather_than_read_as_yes_or_no(
        self, store: ConfigStore, github_project: Path
    ):
        plan = propose_setup(github_project, project="acme", which=all_programs)
        partial = {AutonomyLevel.EXECUTION: True}
        with pytest.raises(SetupApprovalRequired) as caught:
            apply_setup(
                store,
                plan,
                SetupAnswers(cost_profile="budget", confirmations=partial),
            )
        assert AutonomyLevel.DELIVERY.value in str(caught.value)
        assert AutonomyLevel.INTEGRATION.value in str(caught.value)
        assert store.document() == {}

    def test_a_rung_confirmed_above_a_declined_one_is_refused(
        self, store: ConfigStore, github_project: Path
    ):
        plan = propose_setup(github_project, project="acme", which=all_programs)
        contradictory = {
            AutonomyLevel.EXECUTION: True,
            AutonomyLevel.DELIVERY: False,
            AutonomyLevel.INTEGRATION: True,
        }
        with pytest.raises(SetupApprovalRequired, match="declined"):
            apply_setup(
                store,
                plan,
                SetupAnswers(cost_profile="budget", confirmations=contradictory),
            )
        assert store.document() == {}

    def test_declining_every_rung_writes_no_autonomy_grid_and_says_so(
        self, store: ConfigStore, github_project: Path
    ):
        plan = propose_setup(github_project, project="acme", which=all_programs)
        result = apply_setup(
            store,
            plan,
            SetupAnswers(
                cost_profile="budget",
                confirmations=declined(),
                approved_subjects=self.approved(plan),
                watch_source="github",
            ),
        )
        entry = result.document[SECTION_SOURCES]["github"]
        assert AUTONOMY_FIELD not in entry

    def test_only_the_confirmed_ceiling_reaches_the_grid(
        self, store: ConfigStore, github_project: Path
    ):
        plan = propose_setup(github_project, project="acme", which=all_programs)
        result = apply_setup(
            store,
            plan,
            SetupAnswers(
                cost_profile="budget",
                confirmations={
                    AutonomyLevel.EXECUTION: True,
                    AutonomyLevel.DELIVERY: False,
                    AutonomyLevel.INTEGRATION: False,
                },
                approved_subjects=self.approved(plan),
                watch_source="github",
            ),
        )
        grid = result.document[SECTION_SOURCES]["github"][AUTONOMY_FIELD]
        assert grid[WILDCARD_KEY][WILDCARD_KEY] == AutonomyLevel.EXECUTION.value

    def test_a_preset_that_was_never_offered_is_refused(
        self, store: ConfigStore, github_project: Path
    ):
        plan = propose_setup(github_project, project="acme", which=all_programs)
        with pytest.raises(SetupApprovalRequired, match="was not offered"):
            apply_setup(
                store,
                plan,
                SetupAnswers(
                    cost_profile="budget",
                    confirmations=declined(),
                    approved_subjects=self.approved(plan),
                    workflow_preset=LOCAL_WORKFLOW_PRESET,
                ),
            )
        assert store.document() == {}

    def test_an_unapproved_inference_does_not_get_written(
        self, store: ConfigStore, github_project: Path
    ):
        plan = propose_setup(github_project, project="acme", which=all_programs)
        with pytest.raises(SetupApprovalRequired, match="has not approved"):
            apply_setup(
                store,
                plan,
                SetupAnswers(
                    cost_profile="budget",
                    confirmations=declined(),
                    approved_subjects=frozenset(),
                    workflow_preset="git-pull-request",
                ),
            )
        assert store.document() == {}


class TestValidatedWritePath:
    def test_approval_writes_a_document_the_schema_validates(
        self, store: ConfigStore, github_project: Path
    ):
        plan = propose_setup(github_project, project="acme", which=all_programs)
        result = apply_setup(
            store,
            plan,
            SetupAnswers(
                cost_profile="quality-first",
                confirmations=all_confirmed(),
                approved_subjects=frozenset(item.subject for item in plan.inferences),
                workflow_preset="git-pull-request",
                watch_source="github",
            ),
        )
        assert store.validate() == ()
        saved = json.loads(store.path.read_text(encoding="utf-8"))
        assert saved == dict(result.document)
        # The version stamp is added by ConfigStore.write and by nothing else, so
        # its presence is the evidence that the validated door was the door used.
        # Without this, a hand-rolled writer that happened to emit a valid
        # document would pass every other assertion here.
        assert saved[VERSION_KEY] == CURRENT_VERSION
        project = saved[SECTION_PROJECTS]["acme"]
        assert project[PROJECT_PROFILE_FIELD] == "quality-first"
        assert project[SECTION_WORKFLOW][WORKFLOW_PRESET_KEY] == "git-pull-request"
        assert "submit" in project[SECTION_WORKFLOW][WORKFLOW_STAGES_KEY]
        assert saved[SECTION_COST_PROFILES]["quality-first"]["roles"]
        assert saved[SECTION_SOURCES]["github"]["poll"]

    def test_the_written_paths_name_what_landed(self, store: ConfigStore, github_project: Path):
        plan = propose_setup(github_project, project="acme", which=all_programs)
        result = apply_setup(
            store,
            plan,
            SetupAnswers(
                cost_profile="budget",
                confirmations=declined(),
                approved_subjects=frozenset(item.subject for item in plan.inferences),
                watch_source="github",
            ),
        )
        assert f"{SECTION_SOURCES}.github" in result.written_paths
        assert f"{SECTION_COST_PROFILES}.budget" in result.written_paths
        assert f"{SECTION_PROJECTS}.acme.{SECTION_WORKFLOW}" not in result.written_paths

    def test_an_unconfirmed_surface_cannot_drive_the_flow_to_a_write(
        self, store: ConfigStore, github_project: Path
    ):
        # The config-only fence lives in the store, and setup writes fenced
        # sections. Handing the flow a surface no human confirmed must be refused
        # there, not accommodated here.
        plan = propose_setup(github_project, project="acme", which=all_programs)
        with pytest.raises(ConfigWriteRefused):
            apply_setup(
                store,
                plan,
                SetupAnswers(
                    cost_profile="budget",
                    confirmations=declined(),
                    approved_subjects=frozenset(item.subject for item in plan.inferences),
                    watch_source="github",
                ),
                surface=UNCONFIRMED_SURFACE,
            )
        assert store.document() == {}

    def test_a_document_the_schema_would_reject_never_lands(
        self, store: ConfigStore, github_project: Path
    ):
        # The validators run on the merged result, so a project name the schema
        # refuses -- a name that is only whitespace -- must stop the write rather
        # than be persisted and reported later. A writer that assembled its own
        # document would leave this on disk.
        plan = propose_setup(github_project, project="   ", which=all_programs)
        with pytest.raises(ConfigValidationError):
            apply_setup(
                store,
                plan,
                SetupAnswers(
                    cost_profile="budget",
                    confirmations=declined(),
                    approved_subjects=frozenset(item.subject for item in plan.inferences),
                ),
            )
        assert store.document() == {}

    def test_the_default_surface_is_the_setup_assistant(
        self, store: ConfigStore, github_project: Path
    ):
        plan = propose_setup(github_project, project="acme", which=all_programs)
        apply_setup(
            store,
            plan,
            SetupAnswers(cost_profile="budget", confirmations=declined()),
        )
        assert SETUP_ASSISTANT_SURFACE.operator_confirmed is True
        assert store.validate() == ()

    def test_an_invalid_profile_choice_never_reaches_the_store(
        self, store: ConfigStore, github_project: Path
    ):
        plan = propose_setup(github_project, project="acme", which=all_programs)
        with pytest.raises(SetupApprovalRequired):
            apply_setup(
                store,
                plan,
                SetupAnswers(cost_profile="cheap-and-fast", confirmations=all_confirmed()),
            )
        assert not store.path.exists()


class TestApplicablePresets:
    def test_a_github_remote_makes_the_pull_request_preset_applicable(self, tmp_path: Path):
        root = make_repo(tmp_path / "gh", "https://github.com/acme/widgets.git")
        plan = propose_setup(root, project="gh", which=all_programs)
        assert [item.name for item in plan.offers_of(SECTION_WORKFLOW)] == ["git-pull-request"]
        assert [item.name for item in plan.offers_of(SECTION_SOURCES)] == ["github"]

    def test_a_gitlab_remote_makes_the_merge_request_preset_applicable(self, tmp_path: Path):
        root = make_repo(tmp_path / "gl", "git@gitlab.com:acme/widgets.git")
        plan = propose_setup(root, project="gl", which=all_programs)
        assert [item.name for item in plan.offers_of(SECTION_WORKFLOW)] == ["git-merge-request"]
        assert [item.name for item in plan.offers_of(SECTION_SOURCES)] == ["gitlab"]

    def test_no_remote_makes_the_local_only_preset_applicable(self, tmp_path: Path):
        root = make_repo(tmp_path / "solo", None)
        plan = propose_setup(root, project="solo", which=all_programs)
        assert [item.name for item in plan.offers_of(SECTION_WORKFLOW)] == [LOCAL_WORKFLOW_PRESET]
        assert plan.offers_of(SECTION_SOURCES) == ()

    def test_an_unrecognized_host_offers_no_remote_preset_and_asks_instead(self, tmp_path: Path):
        root = make_repo(tmp_path / "priv", "git@git.internal.example:acme/widgets.git")
        plan = propose_setup(root, project="priv", which=all_programs)
        assert plan.offers_of(SECTION_WORKFLOW) == ()
        assert plan.inference(SUBJECT_WATCH_SOURCE) is None
        asked = [item.subject for item in plan.questions]
        assert SUBJECT_WATCH_SOURCE in asked

    def test_every_offered_workflow_name_is_a_bundled_one(self, github_project: Path):
        plan = propose_setup(github_project, project="acme", which=all_programs)
        for offer in plan.offers_of(SECTION_WORKFLOW):
            assert offer.name in WORKFLOW_PRESET_NAMES

    def test_a_workflow_offer_carries_a_renameable_definition_and_says_the_name_is_reserved(
        self, github_project: Path
    ):
        plan = propose_setup(github_project, project="acme", which=all_programs)
        offer = plan.offers_of(SECTION_WORKFLOW)[0]
        definition = offer.definition
        assert definition is not None
        # A definition is not a selection: carrying the bundled name would name
        # the preset the copy stops being at the first edit, and a user-defined
        # preset may not reuse a bundled name at all.
        assert WORKFLOW_PRESET_KEY not in definition
        assert WORKFLOW_STAGES_KEY in definition
        assert "reserved" in offer.copy_note
        assert "name of your own" in offer.copy_note

    def test_a_source_offer_has_no_workflow_definition(self, github_project: Path):
        plan = propose_setup(github_project, project="acme", which=all_programs)
        offer = plan.offers_of(SECTION_SOURCES)[0]
        assert offer.definition is None
        assert offer.copy_note == ""


class TestPrerequisiteReporting:
    def test_an_absent_program_is_reported_unmet_with_the_action_that_resolves_it(
        self, github_project: Path
    ):
        plan = propose_setup(github_project, project="acme", which=no_programs)
        unmet = plan.prerequisites.unmet
        assert unmet
        for check in unmet:
            assert check.missing.strip()
            assert check.action.strip()
        named = " ".join(check.missing for check in unmet)
        assert "gh" in named
        assert "git" in named

    def test_present_programs_leave_the_report_met(self, github_project: Path):
        plan = propose_setup(github_project, project="acme", which=all_programs)
        assert plan.prerequisites.met
        assert plan.prerequisites.checks

    def test_the_offer_report_and_the_source_check_agree_about_the_same_program(
        self, store: ConfigStore, github_project: Path
    ):
        # One answer to "is this program reachable". If the offer-time report and
        # the check the run gate reads ever disagreed, an operator would approve a
        # setup the gate then refuses.
        plan = propose_setup(github_project, project="acme", which=no_programs)
        offer = plan.offer(SECTION_SOURCES, "github")
        assert offer is not None
        offer_unmet = offer.prerequisites.unmet
        assert offer_unmet
        program = WATCH_SOURCE_PRESET_PROGRAMS["github"]
        assert program in offer_unmet[0].missing

        result = apply_setup(
            store,
            plan,
            SetupAnswers(
                cost_profile="budget",
                confirmations=declined(),
                approved_subjects=frozenset(item.subject for item in plan.inferences),
                watch_source="github",
            ),
            which=no_programs,
        )
        gate = check_source(store, "github", which=no_programs)
        assert not gate.met
        assert program in gate.unmet[0].missing
        assert gate.unmet[0].check is CheckName.WATCH_PROGRAMS
        # The post-write report is the gate's own answer, delegated rather than
        # recomputed here.
        assert [check.detail() for check in result.prerequisites.checks] == [
            check.detail() for check in gate.checks
        ]

    def test_delivery_programs_are_reported_at_the_delivery_phase(self, github_project: Path):
        plan = propose_setup(github_project, project="acme", which=no_programs)
        offer = plan.offer(SECTION_WORKFLOW, "git-pull-request")
        assert offer is not None
        phases = {check.phase for check in offer.prerequisites.checks}
        assert AutonomyLevel.DELIVERY in phases
        assert AutonomyLevel.EXECUTION in phases
        # Named against this offer specifically, not against the whole report: the
        # watch source offer also needs 'gh', so a report-wide assertion would stay
        # green with the workflow programs dropped entirely.
        named = {check.missing for check in offer.prerequisites.unmet}
        assert any("'gh'" in item for item in named)
        assert any("'git'" in item for item in named)


class TestProjectFilesAlone:
    def test_memory_is_optional_and_the_plan_records_that_none_was_consulted(
        self, github_project: Path
    ):
        plan = propose_setup(github_project, project="acme", which=all_programs)
        assert plan.memory_consulted is False
        assert "project files alone" in plan.describe()

    def test_the_same_file_inferences_are_made_with_and_without_memory(self, github_project: Path):
        without = inspect_project(github_project)
        with_memory = inspect_project(
            github_project, memory={"workflow": "We ship through a pull request."}
        )
        file_subjects = {item.subject for item in without}
        assert SUBJECT_WORKFLOW_PRESET in file_subjects
        assert SUBJECT_WATCH_SOURCE in file_subjects
        assert SUBJECT_TOOLING in file_subjects
        assert file_subjects <= {item.subject for item in with_memory}

    def test_a_project_with_no_memory_still_reaches_a_written_configuration(
        self, store: ConfigStore, github_project: Path
    ):
        plan = propose_setup(github_project, project="acme", which=all_programs)
        assert plan.memory_consulted is False
        result = apply_setup(
            store,
            plan,
            SetupAnswers(
                cost_profile="budget",
                confirmations=all_confirmed(),
                approved_subjects=frozenset(item.subject for item in plan.inferences),
                workflow_preset="git-pull-request",
                watch_source="github",
            ),
        )
        assert store.validate() == ()
        assert result.written_paths

    def test_a_bare_project_infers_nothing_and_claims_nothing(self, tmp_path: Path):
        bare = tmp_path / "bare"
        bare.mkdir()
        plan = propose_setup(bare, project="bare", which=all_programs)
        # No git directory at all: no remote, and no local-only inference either.
        # The failure this guards is a flow that reports local-only here, which is
        # an inference about a repository it never found, cited to a file that does
        # not exist.
        assert plan.inferences == ()
        assert plan.offers == ()
        assert plan.memory_consulted is False
        asked = [item.subject for item in plan.questions]
        assert SUBJECT_COST_PROFILE in asked
        assert SUBJECT_WATCH_SOURCE in asked
        assert SUBJECT_TOOLING in asked
        assert SUBJECT_WORKFLOW_PRESET in asked

    def test_memory_alone_can_corroborate_a_practice_and_is_labelled_as_memory(
        self, tmp_path: Path
    ):
        root = make_repo(tmp_path / "m", "git@github.com:acme/widgets.git")
        plan = propose_setup(
            root,
            project="m",
            memory={"habits": "This team reviews everything through a pull request."},
            which=all_programs,
        )
        assert plan.memory_consulted is True
        practice = plan.inference(SUBJECT_WORKFLOW_PRACTICE)
        assert practice is not None
        assert practice.evidence[0].render()["located_at"].startswith("memory:")


class TestTheApproverAndWhatTheyWereTold:
    """An approved apply names a human and tells them what they armed.

    Both halves used to stop at the call boundary. The approver an agent-facing
    apply demands was echoed in a reply and recorded nowhere, and the advisories
    the write earned were raised into a log line the caller never saw — including
    the one that requires an acknowledgment because a stranger can start the run it
    authorizes.
    """

    def approved(self, plan) -> frozenset[str]:
        return frozenset(item.subject for item in plan.inferences)

    def test_the_approver_is_recorded_where_it_outlives_the_call(
        self, store: ConfigStore, github_project: Path
    ):
        plan = propose_setup(github_project, project="acme", which=all_programs)
        apply_setup(
            store,
            plan,
            SetupAnswers(
                cost_profile="budget",
                confirmations=declined(),
                approved_subjects=self.approved(plan),
                watch_source="github",
            ),
            actor="ada@example",
        )
        # Read through a store built from the root alone, because "durable" means
        # the record is on disk rather than in the object that wrote it.
        records = ConfigStore(store.root).writes()
        assert [record["actor"] for record in records] == ["ada@example"]
        assert records[0]["surface"] == SETUP_ASSISTANT_SURFACE.name
        assert records[0]["operator_confirmed"] is True

    def test_the_advisories_the_write_earned_come_back_on_the_result(
        self, store: ConfigStore, github_project: Path
    ):
        # Confirming execution on a source whose items anyone may submit is the
        # advisory that requires an acknowledgment. The caller is the last thing
        # between it and the operator, so it travels rather than being logged.
        plan = propose_setup(github_project, project="acme", which=all_programs)
        result = apply_setup(
            store,
            plan,
            SetupAnswers(
                cost_profile="budget",
                confirmations={
                    AutonomyLevel.EXECUTION: True,
                    AutonomyLevel.DELIVERY: False,
                    AutonomyLevel.INTEGRATION: False,
                },
                approved_subjects=self.approved(plan),
                watch_source="github",
            ),
            actor="ada@example",
        )
        codes = [advisory.code for advisory in result.advisories]
        assert PUBLIC_SOURCE_AUTONOMY in codes
        armed = next(item for item in result.advisories if item.code == PUBLIC_SOURCE_AUTONOMY)
        assert armed.requires_acknowledgment is True
        assert "acknowledge this" in armed.message
        assert "advisory:" in result.describe()

    def test_an_apply_that_arms_nothing_carries_no_advisory(
        self, store: ConfigStore, github_project: Path
    ):
        # Non-vacuity for the case above: with every rung declined the document
        # earns nothing, so a result that always listed something would fail here.
        plan = propose_setup(github_project, project="acme", which=all_programs)
        result = apply_setup(
            store,
            plan,
            SetupAnswers(
                cost_profile="budget",
                confirmations=declined(),
                approved_subjects=self.approved(plan),
                watch_source="github",
            ),
        )
        assert result.advisories == ()
        assert ConfigStore(store.root).writes()[0]["actor"] is None


class TestReadingTheProject:
    def test_a_linked_worktree_resolves_its_remote_through_the_common_directory(
        self, tmp_path: Path
    ):
        # A worktree's ``.git`` is a file, and the remotes live in the directory it
        # points at. Reading only ``.git/config`` would infer "no remote" here and
        # offer local-only to a project that has one.
        main = make_repo(tmp_path / "main", "git@github.com:acme/widgets.git")
        tree = tmp_path / "wt"
        tree.mkdir()
        worktree_git = main / ".git" / "worktrees" / "wt"
        worktree_git.mkdir(parents=True)
        (worktree_git / "commondir").write_text("../..\n", encoding="utf-8")
        (tree / ".git").write_text(f"gitdir: {worktree_git}\n", encoding="utf-8")
        plan = propose_setup(tree, project="wt", which=all_programs)
        watched = plan.inference(SUBJECT_WATCH_SOURCE)
        assert watched is not None
        assert watched.value == "github"

    def test_a_remote_named_something_other_than_origin_is_not_read_as_origin(self, tmp_path: Path):
        root = tmp_path / "fork"
        root.mkdir()
        (root / ".git").mkdir()
        (root / ".git" / "config").write_text(
            '[remote "upstream"]\n\turl = git@github.com:acme/widgets.git\n',
            encoding="utf-8",
        )
        plan = propose_setup(root, project="fork", which=all_programs)
        # No origin means nowhere to push, which is the local-only case.
        assert plan.inference(SUBJECT_WATCH_SOURCE) is None
        workflow = plan.inference(SUBJECT_WORKFLOW_PRESET)
        assert workflow is not None
        assert workflow.value == LOCAL_WORKFLOW_PRESET

    def test_an_unreadable_steering_file_is_one_fewer_evidence_not_a_failure(
        self, github_project: Path
    ):
        broken = github_project / ".kiro" / "steering" / "binary.md"
        broken.write_bytes(b"\xff\xfe not text at all")
        plan = propose_setup(github_project, project="acme", which=all_programs)
        assert plan.inferences

    def test_tooling_comes_from_the_build_files_the_project_actually_has(self, tmp_path: Path):
        root = make_repo(tmp_path / "node", "git@github.com:acme/widgets.git")
        (root / "package.json").write_text(
            json.dumps({"scripts": {"build": "vite build", "test": "vitest --run"}}, indent=2),
            encoding="utf-8",
        )
        plan = propose_setup(root, project="node", which=all_programs)
        tooling = plan.inference(SUBJECT_TOOLING)
        assert tooling is not None
        assert "build" in tooling.value
        assert tooling.evidence[0].render()["located_at"] == "package.json"
