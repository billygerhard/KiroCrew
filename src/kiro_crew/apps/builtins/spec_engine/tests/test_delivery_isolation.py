"""Worktree isolation: a tree per run, and a refusal rather than a shared one.

These tests run real ``git`` against a real repository with a local bare remote.
The claim is not that the engine renders the argv somebody intended — it is that
the resulting working tree holds the base branch as it is *now*, that two runs
end up in two trees, and that asking for a tree another run holds is refused. A
mock could confirm the first of those and none of the rest: the exclusivity is
git's as much as it is the engine's, and a stubbed git would happily agree to
check one branch out twice.

The failure being prevented is worth restating, because both halves of it exit
zero. Two runs in one tree means run A stages and commits run B's half-written
edits, so A pushes a change nobody reviewed and B's work disappears into it.
Nothing reports an error; the artifacts just describe work that was never done.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from kiro_crew.apps.builtins.spec_engine.engine.autonomy import (
    AUTONOMY_FIELD,
    AutonomyDecision,
    AutonomyLevel,
)
from kiro_crew.apps.builtins.spec_engine.engine.config import DASHBOARD_SURFACE, ConfigStore
from kiro_crew.apps.builtins.spec_engine.engine.delivery import (
    BRANCH_PREFIX,
    ISOLATE_STAGE,
    MAX_SLUG_CHARS,
    WORKTREE_KIND,
    DeliveryPipeline,
    RunContext,
    StageOutcome,
    WorkspaceBroker,
    git_isolate_commands,
    plan_workspace,
    resolve_authority,
    slugify,
)
from kiro_crew.apps.builtins.spec_engine.engine.runs import RUN_ID_PREFIX
from kiro_crew.apps.builtins.spec_engine.engine.state import StateStore

PROJECT = "acme"
SOURCE = "tracker"
BASE = "main"

#: Identifiers for the runs under test. Real ones are random; fixed ones keep the
#: assertions about paths and branches readable.
RUN_A = f"{RUN_ID_PREFIX}aaaa1111"
RUN_B = f"{RUN_ID_PREFIX}bbbb2222"


def _git(*args: str, cwd: Path) -> str:
    """Run git for test setup and inspection, failing loudly."""
    completed = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


@dataclass(frozen=True)
class Fixture:
    """A repository with a local bare remote, plus the engine state around it."""

    remote: Path
    work: Path
    root: Path
    store: ConfigStore
    state: StateStore

    def push_upstream_commit(self, text: str) -> str:
        """Commit *text* through a second clone and push it, returning its sha.

        A second clone rather than the project tree: the point of the fetch in
        the isolate stage is that the base branch moved somewhere the project
        tree has never seen, which is what an unattended run always faces.
        """
        other = self.remote.parent / f"other-{text}"
        _git("clone", "-q", str(self.remote), str(other), cwd=self.remote.parent)
        _git("config", "user.email", "engine@example.invalid", cwd=other)
        _git("config", "user.name", "engine", cwd=other)
        (other / "upstream.txt").write_text(f"{text}\n", encoding="utf-8")
        _git("add", "upstream.txt", cwd=other)
        _git("commit", "-qm", f"upstream {text}", cwd=other)
        _git("push", "-q", "origin", BASE, cwd=other)
        return _git("rev-parse", "HEAD", cwd=other)


@pytest.fixture()
def repo(tmp_path: Path) -> Fixture:
    remote = tmp_path / "remote.git"
    work = tmp_path / "work"
    _git("init", "-q", "--bare", str(remote), cwd=tmp_path)
    _git("clone", "-q", str(remote), str(work), cwd=tmp_path)
    _git("config", "user.email", "engine@example.invalid", cwd=work)
    _git("config", "user.name", "engine", cwd=work)
    (work / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git("add", "seed.txt", cwd=work)
    _git("commit", "-qm", "seed", cwd=work)
    _git("branch", "-M", BASE, cwd=work)
    _git("push", "-q", "-u", "origin", BASE, cwd=work)
    return Fixture(
        remote=remote,
        work=work,
        root=tmp_path / "worktrees",
        store=ConfigStore(tmp_path / "config"),
        state=StateStore(tmp_path / "engine-state"),
    )


def configure_git_preset(repo: Fixture, *, stages: dict[str, Any] | None = None) -> None:
    """Configure the project with the bundled git preset's isolate stage."""
    repo.store.write(
        {
            "projects": {
                PROJECT: {
                    "path": str(repo.work),
                    "base_branch": BASE,
                    "workflow": {
                        "stages": (
                            stages
                            if stages is not None
                            else {ISOLATE_STAGE: git_isolate_commands()}
                        )
                    },
                }
            }
        },
        surface=DASHBOARD_SURFACE,
    )


def context(repo: Fixture, **overrides: str) -> RunContext:
    values: dict[str, str] = {
        "spec_name": "example",
        "spec_type": "feature",
        "workspace_path": str(repo.work),
        "base_branch": BASE,
    }
    values.update(overrides)
    return RunContext(**values)


def build_pipeline(
    repo: Fixture,
    *,
    broker: WorkspaceBroker | None,
    level: AutonomyLevel = AutonomyLevel.DELIVERY,
) -> DeliveryPipeline:
    authority = resolve_authority(
        repo.store,
        decision=AutonomyDecision(
            level=level,
            source=SOURCE,
            spec_type="feature",
            submitter_class="maintainer",
            declared_at=f"sources.{SOURCE}.{AUTONOMY_FIELD}.maintainer.feature",
        ),
        project=PROJECT,
        base_branch=BASE,
    )
    return DeliveryPipeline(
        repo.store,
        authority=authority,
        project=PROJECT,
        isolation=broker,
    )


def broker_for(repo: Fixture, *, root: Path | None = None) -> WorkspaceBroker:
    return WorkspaceBroker(repo.state, root=root if root is not None else repo.root)


def head_of(tree: Path) -> str:
    return _git("rev-parse", "HEAD", cwd=tree)


def branch_of(tree: Path) -> str:
    return _git("rev-parse", "--abbrev-ref", "HEAD", cwd=tree)


class TestTheGitPresetIsolateStage:
    def test_the_branch_is_cut_from_the_refreshed_base_not_the_stale_one(
        self, repo: Fixture
    ) -> None:
        moved_on = repo.push_upstream_commit("later")
        stale = head_of(repo.work)
        assert stale != moved_on

        configure_git_preset(repo)
        broker = broker_for(repo)
        result = build_pipeline(repo, broker=broker).isolate(context(repo), run_id=RUN_A)

        assert result.outcome is StageOutcome.PASSED, result.reason
        record = broker.workspace_for(RUN_A)
        assert record is not None
        tree = Path(record.location)
        assert tree.is_dir()
        # The commit that landed upstream after the clone is in the run's tree.
        # Without the fetch the branch would start from the stale local base and
        # every later conflict would be attributed to the run's own change.
        assert head_of(tree) == moved_on
        assert (tree / "upstream.txt").read_text(encoding="utf-8") == "later\n"

    def test_the_run_works_on_its_own_new_branch(self, repo: Fixture) -> None:
        configure_git_preset(repo)
        broker = broker_for(repo)
        build_pipeline(repo, broker=broker).isolate(context(repo), run_id=RUN_A)

        record = broker.workspace_for(RUN_A)
        assert record is not None
        assert branch_of(Path(record.location)) == f"{BRANCH_PREFIX}example-{RUN_A}"
        # The project's own tree is left exactly as it was, still on the base
        # branch: a run that moved it would move it under whoever else is there.
        assert branch_of(repo.work) == BASE

    def test_a_branch_the_run_already_carries_is_kept(self, repo: Fixture) -> None:
        configure_git_preset(repo)
        broker = broker_for(repo)
        named = "review/example-17"
        build_pipeline(repo, broker=broker).isolate(context(repo, branch_name=named), run_id=RUN_A)

        record = broker.workspace_for(RUN_A)
        assert record is not None
        assert branch_of(Path(record.location)) == named

    def test_the_workspace_is_recorded_so_teardown_can_find_it(self, repo: Fixture) -> None:
        configure_git_preset(repo)
        broker = broker_for(repo)
        build_pipeline(repo, broker=broker).isolate(context(repo), run_id=RUN_A)

        recorded = repo.state.list_workspaces(run_id=RUN_A)
        assert len(recorded) == 1
        entry = recorded[0]
        assert entry.kind == WORKTREE_KIND
        assert Path(entry.location).is_dir()
        # The ref the tree holds: teardown removes the checkout and must leave
        # this branch alone.
        assert entry.address == f"{BRANCH_PREFIX}example-{RUN_A}"
        assert entry.disposable
        assert not entry.cleaned


class TestNoTwoActiveRunsShareAWorkingTree:
    def test_two_runs_get_two_trees_and_neither_sees_the_other_s_edits(self, repo: Fixture) -> None:
        configure_git_preset(repo)
        broker = broker_for(repo)
        pipeline = build_pipeline(repo, broker=broker)

        first = pipeline.isolate(context(repo), run_id=RUN_A)
        second = pipeline.isolate(context(repo), run_id=RUN_B)
        assert first.outcome is StageOutcome.PASSED, first.reason
        assert second.outcome is StageOutcome.PASSED, second.reason

        a_record = broker.workspace_for(RUN_A)
        b_record = broker.workspace_for(RUN_B)
        assert a_record is not None and b_record is not None
        a_tree, b_tree = Path(a_record.location), Path(b_record.location)
        assert a_tree != b_tree
        assert branch_of(a_tree) != branch_of(b_tree)

        # The whole point, stated as the thing that used to go wrong: an edit
        # one run has not committed yet is not visible to the other, so it
        # cannot be swept into the other's commit.
        (a_tree / "half-written.txt").write_text("in progress\n", encoding="utf-8")
        assert "half-written.txt" in _git("status", "--porcelain", cwd=a_tree)
        assert _git("status", "--porcelain", cwd=b_tree) == ""

    def test_a_second_run_is_refused_the_tree_a_first_run_holds(self, repo: Fixture) -> None:
        configure_git_preset(repo)
        broker = broker_for(repo)
        build_pipeline(repo, broker=broker).isolate(context(repo), run_id=RUN_A)
        held = broker.workspace_for(RUN_A)
        assert held is not None

        # A second run pointed at the tree the first is working in, which is what
        # a mis-set workspace root looks like from here.
        nested = build_pipeline(repo, broker=broker_for(repo, root=Path(held.location)))
        result = nested.isolate(context(repo), run_id=RUN_B)

        assert result.outcome is StageOutcome.REFUSED
        assert RUN_A in result.reason
        # Refused before anything spawned: no command ran and no second claim
        # was recorded against the tree.
        assert result.commands == ()
        assert repo.state.list_workspaces(run_id=RUN_B) == []

    def test_a_second_run_is_refused_the_branch_a_first_run_holds(self, repo: Fixture) -> None:
        configure_git_preset(repo)
        broker = broker_for(repo)
        shared = "review/example-17"
        build_pipeline(repo, broker=broker).isolate(context(repo, branch_name=shared), run_id=RUN_A)

        result = build_pipeline(repo, broker=broker).isolate(
            context(repo, branch_name=shared), run_id=RUN_B
        )

        assert result.outcome is StageOutcome.REFUSED
        assert shared in result.reason
        assert RUN_A in result.reason
        assert result.commands == ()

    def test_git_refuses_the_shared_branch_when_no_broker_asked_first(self, repo: Fixture) -> None:
        """The backstop under the engine's own check.

        With no broker the workflow's literals decide, and two runs can be
        pointed at one branch. Git is what stops them, which is why the engine's
        refusal is a better message rather than the only defence.
        """
        configure_git_preset(
            repo,
            stages={
                ISOLATE_STAGE: [
                    ["git", "fetch", "--prune", "origin", "{base_branch}"],
                    [
                        "git",
                        "worktree",
                        "add",
                        "{workspace_path}/../{item_id}",
                        "-b",
                        "{branch_name}",
                        "origin/{base_branch}",
                    ],
                ]
            },
        )
        pipeline = build_pipeline(repo, broker=None)
        shared = "spec/shared"

        first = pipeline.isolate(context(repo, branch_name=shared, item_id="tree-a"))
        second = pipeline.isolate(context(repo, branch_name=shared, item_id="tree-b"))

        assert first.outcome is StageOutcome.PASSED, first.reason
        assert second.outcome is StageOutcome.FAILED
        assert not (repo.work.parent / "tree-b").exists()

    def test_the_project_s_own_tree_is_never_handed_out_as_a_workspace(self, repo: Fixture) -> None:
        configure_git_preset(repo)
        broker = broker_for(repo, root=repo.work)
        result = build_pipeline(repo, broker=broker).isolate(context(repo), run_id=RUN_A)

        assert result.outcome is StageOutcome.REFUSED
        assert str(repo.work) in result.reason
        assert result.commands == ()

    def test_a_path_holding_files_is_not_the_run_s_own_workspace(self, repo: Fixture) -> None:
        configure_git_preset(repo)
        broker = broker_for(repo)
        planned = plan_workspace(repo.root, run_id=RUN_A, context=context(repo))
        planned.location.mkdir(parents=True)
        (planned.location / "leftover.txt").write_text("from somewhere else\n", encoding="utf-8")

        result = build_pipeline(repo, broker=broker).isolate(context(repo), run_id=RUN_A)

        assert result.outcome is StageOutcome.REFUSED
        assert result.commands == ()


class TestClaimsThatWereNeverMaterialized:
    def test_a_failed_isolate_stage_does_not_leave_the_path_claimed(self, repo: Fixture) -> None:
        configure_git_preset(repo)
        broker = broker_for(repo)
        # A base branch the remote does not have: the fetch fails, so no tree is
        # created and the claim is holding a path for nothing.
        result = build_pipeline(repo, broker=broker).isolate(
            context(repo, base_branch="no-such-base"), run_id=RUN_A
        )

        assert not result.ok
        assert broker.workspace_for(RUN_A) is None
        recorded = repo.state.list_workspaces(run_id=RUN_A, include_cleaned=True)
        assert len(recorded) == 1
        assert recorded[0].cleaned

    def test_a_released_claim_does_not_block_the_run_trying_again(self, repo: Fixture) -> None:
        configure_git_preset(repo)
        broker = broker_for(repo)
        pipeline = build_pipeline(repo, broker=broker)
        pipeline.isolate(context(repo, base_branch="no-such-base"), run_id=RUN_A)

        again = pipeline.isolate(context(repo), run_id=RUN_A)

        assert again.outcome is StageOutcome.PASSED, again.reason
        # One active claim, not two: the abandoned one was released rather than
        # left holding the path this attempt needs.
        assert len(repo.state.list_workspaces(run_id=RUN_A)) == 1

    def test_a_workflow_with_no_isolate_stage_claims_nothing(self, repo: Fixture) -> None:
        configure_git_preset(repo, stages={"verify": [["git", "status", "--porcelain"]]})
        broker = broker_for(repo)
        result = build_pipeline(repo, broker=broker).isolate(context(repo), run_id=RUN_A)

        assert result.outcome is StageOutcome.SKIPPED
        assert repo.state.list_workspaces(run_id=RUN_A) == []

    def test_a_run_without_delivery_authority_is_not_given_a_workspace(self, repo: Fixture) -> None:
        configure_git_preset(repo)
        broker = broker_for(repo)
        result = build_pipeline(repo, broker=broker, level=AutonomyLevel.EXECUTION).isolate(
            context(repo), run_id=RUN_A
        )

        assert result.outcome is StageOutcome.SKIPPED
        assert repo.state.list_workspaces(run_id=RUN_A) == []
        assert not repo.root.exists()


class TestPlanning:
    def test_the_run_identifier_is_in_both_the_path_and_the_branch(self, tmp_path: Path) -> None:
        plan = plan_workspace(
            tmp_path,
            run_id=RUN_A,
            context=RunContext(spec_name="example", spec_type="feature", workspace_path="/w"),
        )
        assert plan.location.name == f"example-{RUN_A}"
        assert plan.branch_name == f"{BRANCH_PREFIX}example-{RUN_A}"

    def test_a_run_needs_an_identifier_before_it_can_be_planned(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            plan_workspace(
                tmp_path,
                run_id="  ",
                context=RunContext(spec_name="s", spec_type="feature", workspace_path="/w"),
            )

    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("Add Login Flow", "add-login-flow"),
            ("../../etc/passwd", "etc-passwd"),
            ("feature.lock", "feature-lock"),
            ("...", "spec"),
            ("", "spec"),
            ("a" * 200, "a" * MAX_SLUG_CHARS),
        ],
    )
    def test_a_spec_name_becomes_a_segment_a_path_and_a_ref_both_accept(
        self, name: str, expected: str
    ) -> None:
        assert slugify(name) == expected


class TestPlanUniquenessProperty:
    """Distinct runs never plan one working tree, for any spec names.

    The property behind "no two active runs share a working tree": uniqueness
    that holds by construction, so two runs starting in the same instant cannot
    both pass a check and then collide.
    """

    @settings(max_examples=200, deadline=None)
    @given(
        st.lists(
            st.tuples(
                st.text(min_size=1, max_size=12),
                st.text(alphabet="0123456789abcdef", min_size=16, max_size=16),
            ),
            min_size=2,
            max_size=6,
            unique_by=lambda pair: pair[1],
        )
    )
    def test_distinct_runs_plan_distinct_non_nesting_trees(
        self, runs: list[tuple[str, str]]
    ) -> None:
        root = Path("/state/worktrees")
        planned = [
            plan_workspace(
                root,
                run_id=f"{RUN_ID_PREFIX}{token}",
                context=RunContext(
                    spec_name=spec_name, spec_type="feature", workspace_path="/project"
                ),
            )
            for spec_name, token in runs
        ]
        locations = [plan.location for plan in planned]
        assert len(set(locations)) == len(locations)
        for left in locations:
            for right in locations:
                if left != right:
                    assert not left.is_relative_to(right)
        assert len({plan.branch_name for plan in planned}) == len(planned)
