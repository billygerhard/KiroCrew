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

The second half of the file covers taking a tree back again, which is a deletion
with a preservation guarantee beside it. "The directory is gone" is satisfied by a
teardown that deleted the branch and its commits too, so the tests lean on the
preservation direction: the branch is still there afterwards, its commits are
still reachable from the project repository, the branch can be checked out again,
and one run's teardown never reaches a sibling run's tree.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from kiro_crew.apps.builtins.spec_engine.engine.audit import AuditLog
from kiro_crew.apps.builtins.spec_engine.engine.autonomy import (
    AUTONOMY_FIELD,
    AutonomyDecision,
    AutonomyLevel,
)
from kiro_crew.apps.builtins.spec_engine.engine.config import DASHBOARD_SURFACE, ConfigStore
from kiro_crew.apps.builtins.spec_engine.engine.delivery import (
    BRANCH_PREFIX,
    DEPLOYMENT_KIND,
    ISOLATE_STAGE,
    MAX_SLUG_CHARS,
    REASON_ALREADY_GONE,
    REASON_NO_ROOT,
    REASON_NO_STAGE,
    REASON_OUTSIDE_ROOT,
    TEARDOWN_STAGE,
    TEMP_COPY_KIND,
    WORKTREE_KIND,
    CommandOutcome,
    DeliveryPipeline,
    RunContext,
    StageOutcome,
    StageResult,
    WorkspaceBroker,
    WorkspaceJanitor,
    git_isolate_commands,
    plan_workspace,
    resolve_authority,
    run_argv,
    slugify,
)
from kiro_crew.apps.builtins.spec_engine.engine.review_queue import ArchiveCause, ReviewQueue
from kiro_crew.apps.builtins.spec_engine.engine.runs import RUN_ID_PREFIX, RunMachine, RunState
from kiro_crew.apps.builtins.spec_engine.engine.state import (
    SpecRef,
    StateStore,
    WorkspaceRecord,
)

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


def commit_in(tree: Path, text: str) -> str:
    """Commit *text* inside a run's own worktree, returning the sha."""
    (tree / f"{text}.txt").write_text(f"{text}\n", encoding="utf-8")
    _git("add", "-A", cwd=tree)
    _git("commit", "-qm", f"run work {text}", cwd=tree)
    return _git("rev-parse", "HEAD", cwd=tree)


def worktrees_of(repo: Fixture) -> list[str]:
    """The paths git considers worktrees of the project repository."""
    listed = _git("worktree", "list", "--porcelain", cwd=repo.work)
    return [line.split(" ", 1)[1] for line in listed.splitlines() if line.startswith("worktree ")]


def branches_of(repo: Fixture) -> set[str]:
    listed = _git("branch", "--format=%(refname:short)", cwd=repo.work)
    return {line.strip() for line in listed.splitlines() if line.strip()}


def commit_exists(repo: Fixture, sha: str) -> bool:
    """Whether *sha* is still an object in the project repository."""
    try:
        _git("cat-file", "-e", f"{sha}^{{commit}}", cwd=repo.work)
    except subprocess.CalledProcessError:
        return False
    return True


#: Git verbs that move or destroy a ref, an object, or a working-tree edit.
#: Teardown must never issue one: removing a checkout is the only thing it does,
#: and everything here would take the run's work with it. Screened as data so a
#: later change that reaches for one of them fails a test rather than a review.
REF_MUTATING_ARGUMENTS = (
    "branch",
    "update-ref",
    "push",
    "reset",
    "gc",
    "tag",
    "reflog",
    "filter-branch",
    "stash",
    "checkout",
    "switch",
    "rebase",
    "merge",
    "clean",
    "-D",
    "-d",
    "--delete",
)


class RecordingRunner:
    """Runs removal commands for real and records every argv it was given.

    Real execution rather than a stub, because the claim under test is what
    happens to the repository. The recording is the second half: it is how a test
    can assert that teardown never *asked* for a ref to be deleted, which an
    assertion about the end state cannot show — a command that deleted the branch
    and one that did not both leave a directory gone.
    """

    def __init__(self) -> None:
        self.argvs: list[tuple[str, ...]] = []

    def __call__(self, argv: Any, *, cwd: Path, timeout_s: int) -> CommandOutcome:
        self.argvs.append(tuple(argv))
        return run_argv(argv, cwd=cwd, timeout_s=timeout_s)


class StubStage:
    """Stands in for the configured teardown stage, recording what it was asked."""

    def __init__(self, outcome: StageOutcome = StageOutcome.PASSED) -> None:
        self.outcome = outcome
        self.run_ids: list[str] = []

    def __call__(self, run_id: str) -> StageResult:
        self.run_ids.append(run_id)
        return StageResult(stage=TEARDOWN_STAGE, outcome=self.outcome)


def janitor_for(
    repo: Fixture,
    *,
    root: Path | None = None,
    runner: RecordingRunner | None = None,
    stage: StubStage | None = None,
    audit: Any = None,
) -> WorkspaceJanitor:
    return WorkspaceJanitor(
        repo.state,
        root=root,
        runner=runner if runner is not None else RecordingRunner(),
        stage=stage,
        audit=audit,
    )


def isolate_run(repo: Fixture, run_id: str, **overrides: str) -> Path:
    """Give *run_id* a real worktree through the pipeline, returning its path."""
    configure_git_preset(repo)
    broker = broker_for(repo)
    result = build_pipeline(repo, broker=broker).isolate(context(repo, **overrides), run_id=run_id)
    assert result.outcome is StageOutcome.PASSED, result.reason
    record = broker.workspace_for(run_id)
    assert record is not None
    return Path(record.location)


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

    def test_a_branch_a_released_run_held_is_free_for_another_run(self, repo: Fixture) -> None:
        """Releasing a claim has to hand the branch back, not retire it.

        A tracker-dispatched run carries the branch its review artifact expects,
        so the same name legitimately arrives again after the first attempt let
        it go. Holding it against a run that no longer exists would strand that
        branch for good, and the only thing separating the two cases is that a
        released claim is excluded from the conflict scan.
        """
        configure_git_preset(repo)
        broker = broker_for(repo)
        shared = "review/example-17"
        # A base the remote lacks: the stage fails and releases the claim, which
        # is the realistic way a branch is held and then handed back.
        first = build_pipeline(repo, broker=broker).isolate(
            context(repo, branch_name=shared, base_branch="no-such-base"), run_id=RUN_A
        )
        assert not first.ok
        assert broker.workspace_for(RUN_A) is None

        result = build_pipeline(repo, broker=broker).isolate(
            context(repo, branch_name=shared), run_id=RUN_B
        )

        assert result.outcome is StageOutcome.PASSED, result.reason

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


# --- teardown --------------------------------------------------------------
#
# Teardown is a deletion with a preservation guarantee beside it, and the two
# halves are not equally hard to get right. "The directory is gone" is satisfied
# by a teardown that deleted everything, including the branch the run's work is
# on and the commits on it. So these tests lean on the preservation direction:
# after the checkout is removed, the branch is still there, its commits are still
# reachable from the project repository, and the branch can be checked out again.


class TestTeardownPreservesTheWork:
    def test_the_branch_and_its_commits_survive_the_checkout_removal(self, repo: Fixture) -> None:
        tree = isolate_run(repo, RUN_A)
        branch = branch_of(tree)
        sha = commit_in(tree, "delivered")

        report = janitor_for(repo).retire_run(RUN_A)

        assert report.complete, report.kept_reasons
        # The disposable half is gone.
        assert not tree.exists()
        # The work is not. Both halves are asserted, because a teardown that
        # deleted the branch too would satisfy the first one on its own.
        assert branch in branches_of(repo)
        assert commit_exists(repo, sha)
        assert _git("rev-parse", branch, cwd=repo.work) == sha
        assert "run work delivered" in _git("log", "--format=%s", branch, cwd=repo.work)

    def test_the_branch_can_be_checked_out_again_after_teardown(self, repo: Fixture) -> None:
        """The removal has to release git's claim on the branch, not just the disk.

        Deleting the directory and leaving the worktree metadata behind looks
        identical from the file system and is not the same thing: git still
        believes the branch is checked out somewhere, so the work is on a ref
        nobody can reach without repairing the repository by hand.
        """
        tree = isolate_run(repo, RUN_A)
        branch = branch_of(tree)
        sha = commit_in(tree, "delivered")

        janitor_for(repo).retire_run(RUN_A)

        assert worktrees_of(repo) == [str(repo.work.resolve())]
        _git("checkout", "-q", branch, cwd=repo.work)
        assert head_of(repo.work) == sha
        _git("checkout", "-q", BASE, cwd=repo.work)

    def test_teardown_never_asks_git_to_move_or_delete_a_ref(self, repo: Fixture) -> None:
        """Screening the commands, not only their effect.

        The end state cannot distinguish a teardown that removed a checkout from
        one that removed a checkout and would have deleted the branch if the ref
        had been named differently. What can distinguish them is the argv.
        """
        tree = isolate_run(repo, RUN_A)
        commit_in(tree, "delivered")
        runner = RecordingRunner()

        janitor_for(repo, runner=runner).retire_run(RUN_A)

        assert runner.argvs == [("git", "worktree", "remove", str(tree))]
        for argv in runner.argvs:
            assert argv[:3] == ("git", "worktree", "remove")
            # The path is one element, so a location holding a space or a
            # metacharacter is an argument rather than syntax.
            assert argv[-1] == str(tree)
            for forbidden in REF_MUTATING_ARGUMENTS:
                assert forbidden not in argv[1:-1]

    def test_a_forced_removal_still_only_removes_the_checkout(self, repo: Fixture) -> None:
        tree = isolate_run(repo, RUN_A)
        branch = branch_of(tree)
        sha = commit_in(tree, "delivered")
        # Uncommitted, which is what makes the removal need the force flag.
        (tree / "in-progress.txt").write_text("half\n", encoding="utf-8")
        runner = RecordingRunner()

        report = janitor_for(repo, runner=runner).archive_run(RUN_A)

        assert report.removed[0].removed
        assert not tree.exists()
        assert runner.argvs == [("git", "worktree", "remove", "--force", str(tree))]
        # Force discards the working copy. It does not discard the history: that
        # distinction is the whole reason archive is allowed to force at all.
        assert branch in branches_of(repo)
        assert _git("rev-parse", branch, cwd=repo.work) == sha


class TestTeardownReachesOneRunOnly:
    def test_tearing_down_one_run_leaves_a_sibling_run_s_tree_alone(self, repo: Fixture) -> None:
        """A teardown keyed too broadly is how another run's work disappears.

        The sibling is very often still working: two runs of one spec are
        independent workspaces on independent branches, and the run whose tree
        was removed underneath it fails in the middle of work it has already
        reported as progressing.
        """
        first = isolate_run(repo, RUN_A)
        second = isolate_run(repo, RUN_B)
        second_branch = branch_of(second)
        second_sha = commit_in(second, "sibling")
        # Deliberately clean. A tree with uncommitted edits is one git refuses to
        # remove without force, so a sibling left dirty would survive a teardown
        # keyed at every run and this test would pass on the wrong mechanism.
        assert _git("status", "--porcelain", cwd=second) == ""

        janitor_for(repo).retire_run(RUN_A)

        assert not first.exists()
        assert second.is_dir()
        assert (second / "sibling.txt").read_text(encoding="utf-8") == "sibling\n"
        assert branch_of(second) == second_branch
        assert head_of(second) == second_sha
        # And the sibling's ledger row is still active, so it still holds its
        # tree against a third run.
        held = repo.state.list_workspaces(run_id=RUN_B)
        assert len(held) == 1
        assert not held[0].cleaned

    def test_a_run_with_no_recorded_workspace_removes_nothing(self, repo: Fixture) -> None:
        second = isolate_run(repo, RUN_B)
        runner = RecordingRunner()

        report = janitor_for(repo, runner=runner).retire_run(RUN_A)

        assert report.cleanups == ()
        assert runner.argvs == []
        assert second.is_dir()


class TestTheLedgerIsClosedOut:
    def test_the_row_is_marked_cleaned_so_the_path_is_free_again(self, repo: Fixture) -> None:
        isolate_run(repo, RUN_A)

        janitor_for(repo).retire_run(RUN_A)

        assert repo.state.list_workspaces(run_id=RUN_A) == []
        history = repo.state.list_workspaces(run_id=RUN_A, include_cleaned=True)
        assert len(history) == 1
        assert history[0].cleaned
        assert history[0].cleaned_ts

    def test_a_second_teardown_is_not_an_error(self, repo: Fixture) -> None:
        """Archive is reversible and re-archiving is fine, so this runs twice."""
        isolate_run(repo, RUN_A)
        janitor = janitor_for(repo)
        janitor.retire_run(RUN_A)

        again = janitor.retire_run(RUN_A)

        assert again.cleanups == ()
        assert again.complete

    def test_a_location_removed_by_hand_closes_its_row(self, repo: Fixture) -> None:
        tree = isolate_run(repo, RUN_A)
        shutil.rmtree(tree)

        report = janitor_for(repo).retire_run(RUN_A)

        assert [cleanup.reason for cleanup in report.removed] == [REASON_ALREADY_GONE]
        assert repo.state.list_workspaces(run_id=RUN_A) == []

    def test_a_checkout_that_will_not_remove_keeps_its_row(self, repo: Fixture) -> None:
        """A kept row is the point: it is what the manual cleanup action finds."""
        tree = isolate_run(repo, RUN_A)
        (tree / "in-progress.txt").write_text("half\n", encoding="utf-8")

        report = janitor_for(repo).retire_run(RUN_A)

        assert report.kept
        assert not report.complete
        assert tree.is_dir()
        assert len(repo.state.list_workspaces(run_id=RUN_A)) == 1


class TestUncommittedWorkAtATerminalState:
    def test_the_terminal_sweep_keeps_a_dirty_checkout_and_says_why(self, repo: Fixture) -> None:
        tree = isolate_run(repo, RUN_A)
        (tree / "why-it-failed.txt").write_text("half written\n", encoding="utf-8")
        runner = RecordingRunner()

        report = janitor_for(repo, runner=runner).retire_run(RUN_A)

        assert tree.is_dir()
        assert (tree / "why-it-failed.txt").exists()
        assert report.kept
        assert "uncommitted work is discarded only on archive" in report.kept[0].reason
        # Not forced, which is the mechanism rather than the message.
        assert runner.argvs == [("git", "worktree", "remove", str(tree))]

    def test_archive_discards_the_working_copy_and_keeps_the_history(self, repo: Fixture) -> None:
        tree = isolate_run(repo, RUN_A)
        branch = branch_of(tree)
        sha = commit_in(tree, "delivered")
        (tree / "why-it-failed.txt").write_text("half written\n", encoding="utf-8")

        report = janitor_for(repo).archive_run(RUN_A)

        assert not tree.exists()
        assert report.removed
        assert branch in branches_of(repo)
        assert _git("rev-parse", branch, cwd=repo.work) == sha


class TestTheLedgerRecordsDeploymentsToo:
    def test_a_deployment_is_recorded_against_the_run(self, repo: Fixture) -> None:
        record = broker_for(repo).record_deployment(RUN_A, address="https://preview.test/pr-17")

        assert record.kind == DEPLOYMENT_KIND
        assert record.address == "https://preview.test/pr-17"
        # Not disposable: the engine cannot delete an environment, so nothing in
        # the terminal-state sweep may treat this address as a path.
        assert not record.disposable
        assert repo.state.list_workspaces(run_id=RUN_A) == [record]

    @pytest.mark.parametrize(("run_id", "address"), [(RUN_A, "  "), ("  ", "https://x.test")])
    def test_a_deployment_needs_both_a_run_and_an_address(
        self, repo: Fixture, run_id: str, address: str
    ) -> None:
        with pytest.raises(ValueError):
            broker_for(repo).record_deployment(run_id, address=address)

    def test_the_terminal_sweep_removes_the_checkout_and_leaves_the_deployment(
        self, repo: Fixture
    ) -> None:
        """A finished run's published change is still published.

        The two rows have opposite lifetimes, so a sweep that closed both out
        would drop the only record that an environment is live at exactly the
        moment nobody is looking at the run any more.
        """
        tree = isolate_run(repo, RUN_A)
        broker_for(repo).record_deployment(RUN_A, address="https://preview.test/pr-17")

        report = janitor_for(repo).retire_run(RUN_A)

        assert not tree.exists()
        remaining = repo.state.list_workspaces(run_id=RUN_A)
        assert [row.kind for row in remaining] == [DEPLOYMENT_KIND]
        assert [cleanup.kind for cleanup in report.kept] == [DEPLOYMENT_KIND]
        assert report.kept[0].reason == REASON_NO_STAGE

    def test_a_deployment_closes_out_when_the_teardown_commands_pass(self, repo: Fixture) -> None:
        broker_for(repo).record_deployment(RUN_A, address="https://preview.test/pr-17")
        stage = StubStage(StageOutcome.PASSED)

        report = janitor_for(repo, stage=stage).archive_run(RUN_A)

        assert stage.run_ids == [RUN_A]
        assert report.stage_ok
        assert report.removed[0].kind == DEPLOYMENT_KIND
        assert repo.state.list_workspaces(run_id=RUN_A) == []

    def test_a_run_with_nothing_configured_to_tear_down_is_still_complete(
        self, repo: Fixture
    ) -> None:
        """A skipped stage is an answer: the project configured no teardown."""
        broker_for(repo).record_deployment(RUN_A, address="https://preview.test/pr-17")

        report = janitor_for(repo, stage=StubStage(StageOutcome.SKIPPED)).archive_run(RUN_A)

        assert report.complete
        assert repo.state.list_workspaces(run_id=RUN_A) == []

    @pytest.mark.parametrize(
        "outcome", [StageOutcome.FAILED, StageOutcome.REFUSED, StageOutcome.TIMED_OUT]
    )
    def test_a_deployment_stands_when_its_teardown_commands_did_not(
        self, repo: Fixture, outcome: StageOutcome
    ) -> None:
        """The row is the only record the environment exists.

        Closing it out on a failed teardown loses the environment rather than the
        record of a problem: nothing afterwards knows there is anything to remove.
        """
        broker_for(repo).record_deployment(RUN_A, address="https://preview.test/pr-17")

        report = janitor_for(repo, stage=StubStage(outcome)).archive_run(RUN_A)

        assert not report.stage_ok
        assert not report.complete
        assert [row.kind for row in repo.state.list_workspaces(run_id=RUN_A)] == [DEPLOYMENT_KIND]
        assert outcome.value in report.kept[0].reason

    def test_an_unwired_teardown_runner_is_reported_rather_than_assumed_clean(
        self, repo: Fixture
    ) -> None:
        broker_for(repo).record_deployment(RUN_A, address="https://preview.test/pr-17")

        report = janitor_for(repo, stage=None).archive_run(RUN_A)

        # "Nobody ran the commands" and "the commands passed" are different
        # sentences, and only one of them means the deployment is gone.
        assert report.stage is None
        assert report.stage_reason == REASON_NO_STAGE
        assert not report.stage_ok
        assert [row.kind for row in repo.state.list_workspaces(run_id=RUN_A)] == [DEPLOYMENT_KIND]


class TestCopiedWorkingTrees:
    def make_copy(self, repo: Fixture, location: Path) -> int:
        location.mkdir(parents=True, exist_ok=True)
        (location / "copied.txt").write_text("copy\n", encoding="utf-8")
        record = repo.state.record_workspace(
            RUN_A, kind=TEMP_COPY_KIND, location=str(location), disposable=True
        )
        return record.workspace_id

    def test_a_copy_under_the_disposable_root_is_deleted(self, repo: Fixture) -> None:
        location = repo.root / "copy-a"
        self.make_copy(repo, location)

        report = janitor_for(repo, root=repo.root).retire_run(RUN_A)

        assert report.removed
        assert not location.exists()
        assert repo.state.list_workspaces(run_id=RUN_A) == []

    def test_a_location_outside_the_root_is_kept_however_the_ledger_names_it(
        self, repo: Fixture
    ) -> None:
        """A ledger row is a location the engine wrote down, not a licence.

        This is the case where a bug would look ordinary from the inside and be
        catastrophic outside it: the row here points at the project's own working
        tree, which is what a mis-set root or a bad record produces.
        """
        self.make_copy(repo, repo.work)

        report = janitor_for(repo, root=repo.root).retire_run(RUN_A)

        assert report.kept
        assert report.kept[0].reason == REASON_OUTSIDE_ROOT
        assert (repo.work / "seed.txt").exists()
        assert (repo.work / ".git").exists()
        assert len(repo.state.list_workspaces(run_id=RUN_A)) == 1

    def test_a_symlink_pointing_out_of_the_root_does_not_get_in(self, repo: Fixture) -> None:
        """The check resolves both sides, so a link inside the root is still out."""
        target = repo.remote.parent / "elsewhere"
        target.mkdir()
        (target / "keep.txt").write_text("keep\n", encoding="utf-8")
        repo.root.mkdir(parents=True, exist_ok=True)
        link = repo.root / "linked"
        link.symlink_to(target, target_is_directory=True)
        repo.state.record_workspace(RUN_A, kind=TEMP_COPY_KIND, location=str(link))

        report = janitor_for(repo, root=repo.root).retire_run(RUN_A)

        assert report.kept[0].reason == REASON_OUTSIDE_ROOT
        assert (target / "keep.txt").exists()

    def test_the_root_itself_is_never_deleted(self, repo: Fixture) -> None:
        """Deleting the root would take every other run's workspace with it."""
        self.make_copy(repo, repo.root)

        report = janitor_for(repo, root=repo.root).retire_run(RUN_A)

        assert report.kept[0].reason == REASON_OUTSIDE_ROOT
        assert repo.root.is_dir()

    def test_no_configured_root_means_no_deletion_at_all(self, repo: Fixture) -> None:
        location = repo.root / "copy-a"
        self.make_copy(repo, location)

        report = janitor_for(repo, root=None).retire_run(RUN_A)

        assert report.kept[0].reason == REASON_NO_ROOT
        assert location.is_dir()


class TestTheManualCleanupAction:
    def test_it_removes_a_checkout_the_sweep_kept(self, repo: Fixture) -> None:
        tree = isolate_run(repo, RUN_A)
        branch = branch_of(tree)
        sha = commit_in(tree, "delivered")
        (tree / "in-progress.txt").write_text("half\n", encoding="utf-8")
        janitor = janitor_for(repo)
        kept = janitor.retire_run(RUN_A)
        assert kept.kept
        workspace_id = kept.kept[0].workspace_id

        cleanup = janitor.clean_workspace(workspace_id, force=True)

        assert cleanup is not None and cleanup.removed
        assert not tree.exists()
        # Still a removal of the checkout only.
        assert branch in branches_of(repo)
        assert _git("rev-parse", branch, cwd=repo.work) == sha

    def test_an_unknown_workspace_is_answered_rather_than_removed(self, repo: Fixture) -> None:
        isolate_run(repo, RUN_A)

        assert janitor_for(repo).clean_workspace(9999) is None
        assert len(repo.state.list_workspaces(run_id=RUN_A)) == 1

    def test_a_row_already_cleaned_is_not_cleaned_twice(self, repo: Fixture) -> None:
        tree = isolate_run(repo, RUN_A)
        janitor = janitor_for(repo)
        recorded = repo.state.list_workspaces(run_id=RUN_A)[0]
        janitor.retire_run(RUN_A)
        assert not tree.exists()

        assert janitor.clean_workspace(recorded.workspace_id) is None


class TestArchiveTriggersTeardown:
    """Archive is where the ledger is closed out, so it has to trigger teardown.

    Wired into :class:`ReviewQueue` rather than offered beside it: a cleanup that
    only happens when a driver remembered to ask is a cleanup that does not
    happen, and the surface that forgot leaks a worktree per archived spec with
    nothing saying so.
    """

    def machinery(self, repo: Fixture, **kwargs: Any) -> tuple[RunMachine, ReviewQueue]:
        machine = RunMachine(
            repo.state,
            repo.store,
            audit=AuditLog(root=repo.remote.parent / "audit"),
        )
        return machine, ReviewQueue(machine, janitor=janitor_for(repo, **kwargs))

    def spec_of(self, repo: Fixture) -> SpecRef:
        return SpecRef.of(repo.work, "example")

    def test_archiving_removes_the_run_s_checkout_and_keeps_its_branch(self, repo: Fixture) -> None:
        tree = isolate_run(repo, RUN_A)
        branch = branch_of(tree)
        sha = commit_in(tree, "delivered")
        machine, queue = self.machinery(repo)
        ref = self.spec_of(repo)
        machine.create(ref, run_id=RUN_A, source=SOURCE)
        machine.transition(ref, RUN_A, RunState.AUTHORING)
        machine.transition(ref, RUN_A, RunState.DONE)

        result = queue.archive(ref, cause=ArchiveCause.USER, actor="dana")

        assert result.archived
        assert not tree.exists()
        assert repo.state.list_workspaces(run_id=RUN_A) == []
        # The work the run did is still in the repository, which is the half a
        # teardown that deleted everything would also have satisfied the first
        # assertion without.
        assert branch in branches_of(repo)
        assert _git("rev-parse", branch, cwd=repo.work) == sha

    def test_archiving_runs_the_configured_teardown_commands(self, repo: Fixture) -> None:
        stage = StubStage(StageOutcome.PASSED)
        broker_for(repo).record_deployment(RUN_A, address="https://preview.test/pr-17")
        machine, queue = self.machinery(repo, stage=stage)
        ref = self.spec_of(repo)
        machine.create(ref, run_id=RUN_A, source=SOURCE)

        result = queue.archive(ref, cause=ArchiveCause.USER, actor="dana")

        assert stage.run_ids == [RUN_A]
        assert [report.run_id for report in result.teardown] == [RUN_A]
        assert repo.state.list_workspaces(run_id=RUN_A) == []

    def test_archiving_one_spec_leaves_another_spec_s_run_untouched(self, repo: Fixture) -> None:
        """The ledger is keyed by run, and archive follows that key.

        A teardown keyed on anything broader — a project, a workspace root — would
        reach the workspace of a run belonging to a spec nobody archived, and that
        run is still working in it.
        """
        archived_tree = isolate_run(repo, RUN_A)
        other_tree = isolate_run(repo, RUN_B)
        other_sha = commit_in(other_tree, "sibling")
        machine, queue = self.machinery(repo)
        archived = self.spec_of(repo)
        other = SpecRef.of(repo.work, "other")
        machine.create(archived, run_id=RUN_A, source=SOURCE)
        machine.create(other, run_id=RUN_B, source=SOURCE)

        queue.archive(archived, cause=ArchiveCause.USER, actor="dana")

        assert not archived_tree.exists()
        assert other_tree.is_dir()
        assert head_of(other_tree) == other_sha
        assert len(repo.state.list_workspaces(run_id=RUN_B)) == 1

    def test_a_teardown_that_could_not_finish_does_not_block_the_archival(
        self, repo: Fixture
    ) -> None:
        """A person putting a spec down is not blocked by a command's exit code.

        Refusing the archival would leave the spec live and the person without a
        way to put it down; what the report and the audit entry carry instead is
        what was left standing, which is what the manual cleanup action retries.
        """
        broker_for(repo).record_deployment(RUN_A, address="https://preview.test/pr-17")
        machine, queue = self.machinery(repo, stage=StubStage(StageOutcome.FAILED))
        ref = self.spec_of(repo)
        machine.create(ref, run_id=RUN_A, source=SOURCE)

        result = queue.archive(ref, cause=ArchiveCause.USER, actor="dana")

        assert result.archived and queue.is_archived(ref)
        assert not result.teardown[0].complete
        assert [row.kind for row in repo.state.list_workspaces(run_id=RUN_A)] == [DEPLOYMENT_KIND]

    def test_the_archival_audit_entry_records_what_teardown_did(self, repo: Fixture) -> None:
        tree = isolate_run(repo, RUN_A)
        log = AuditLog(root=repo.remote.parent / "audit")
        machine = RunMachine(repo.state, repo.store, audit=log)
        queue = ReviewQueue(machine, janitor=janitor_for(repo))
        ref = self.spec_of(repo)
        machine.create(ref, run_id=RUN_A, source=SOURCE)

        queue.archive(ref, cause=ArchiveCause.USER, actor="dana")

        archived = [entry for entry in log.read(ref) if entry.event == "spec.archived"]
        assert len(archived) == 1
        recorded = (archived[0].detail or {})["teardown"]
        assert [report["run_id"] for report in recorded] == [RUN_A]
        assert [removed["location"] for removed in recorded[0]["removed"]] == [str(tree)]

    def test_an_archival_with_no_janitor_supplied_still_cleans_up(self, repo: Fixture) -> None:
        """The default janitor is the point: teardown is not opt-in.

        A surface that constructs a queue without thinking about workspaces still
        gets the checkout removed, because the alternative is a leak nobody
        notices until the disk fills.
        """
        tree = isolate_run(repo, RUN_A)
        branch = branch_of(tree)
        machine = RunMachine(repo.state, repo.store, audit=AuditLog(root=repo.work.parent / "aud"))
        queue = ReviewQueue(machine)
        ref = self.spec_of(repo)
        machine.create(ref, run_id=RUN_A, source=SOURCE)

        queue.archive(ref, cause=ArchiveCause.USER, actor="dana")

        assert not tree.exists()
        assert branch in branches_of(repo)


class FakeLedger:
    """A ledger of rows a test declares, without a database behind it."""

    def __init__(self, rows: list[WorkspaceRecord]) -> None:
        self.rows = rows
        self.cleaned: list[int] = []

    def list_workspaces(
        self, *, run_id: str | None = None, include_cleaned: bool = False
    ) -> list[WorkspaceRecord]:
        return [
            row
            for row in self.rows
            if (run_id is None or row.run_id == run_id) and (include_cleaned or not row.cleaned)
        ]

    def mark_workspace_cleaned(self, workspace_id: int) -> bool:
        self.cleaned.append(workspace_id)
        return True


class RefusingRunner:
    """Records argvs and refuses every removal, so nothing is actually deleted."""

    def __init__(self) -> None:
        self.argvs: list[tuple[str, ...]] = []

    def __call__(self, argv: Any, *, cwd: Path, timeout_s: int) -> CommandOutcome:
        self.argvs.append(tuple(argv))
        return CommandOutcome(exit_code=1, stderr="refused by the test")


class TestNoTeardownCommandCanReachARef:
    """Whatever a ledger row says, the command issued removes a checkout.

    The property behind "preserves all branches and commits": a location is
    attacker-shaped free text as far as this module is concerned — it can come
    from a spec name on a public tracker — and the guarantee has to hold for every
    one of them rather than for the paths a fixture happens to produce.
    """

    @settings(max_examples=100, deadline=None)
    @given(
        segment=st.text(
            alphabet=" -_.$;|&`'\"()*?[]{}<>!#~^%+=,:@abcXYZ019", min_size=1, max_size=24
        ),
        force=st.booleans(),
    )
    def test_every_issued_argv_is_a_worktree_removal_of_one_path(
        self, tmp_path_factory: pytest.TempPathFactory, segment: str, force: bool
    ) -> None:
        root = tmp_path_factory.mktemp("ledger")
        location = root / segment
        try:
            location.mkdir()
        except OSError:
            return
        rows = [
            WorkspaceRecord(
                workspace_id=1,
                run_id=RUN_A,
                kind=WORKTREE_KIND,
                location=str(location),
                address=f"{BRANCH_PREFIX}{segment}",
                disposable=True,
                cleaned=False,
                created_ts="2026-03-01T00:00:00Z",
                cleaned_ts=None,
            )
        ]
        runner = RefusingRunner()
        janitor = WorkspaceJanitor(FakeLedger(rows), root=root, runner=runner)

        if force:
            janitor.archive_run(RUN_A)
        else:
            janitor.retire_run(RUN_A)

        assert len(runner.argvs) == 1
        argv = runner.argvs[0]
        expected = ("git", "worktree", "remove", *(("--force",) if force else ()), str(location))
        assert argv == expected
        # The location is one element however it is spelled, and no element
        # between the verb and the path can name a ref.
        assert argv.count(str(location)) == 1
        for element in argv[:-1]:
            assert element not in REF_MUTATING_ARGUMENTS
