"""One run, end to end, on a real git repository with a local bare remote.

Everything below runs offline against real machinery: a real repository, a real
bare remote reached by path, real ``git worktree`` invocations spawned by the
stage executor, real files appearing and disappearing on disk, and the real
workspace ledger in the state store. Only two things are stood in for -- the task
worker and the reviewer, which are where a model would be, and the stage commands
themselves, which are scripts written into ``tmp_path`` with the exec bit set.

That split is the point. Every unit test of these modules substitutes the command
runner, so none of them can tell whether the worktree was ever materialized,
whether ``git worktree remove`` actually unlinks it, or whether the flow's stage
order holds against a checkout that has to still exist when submit runs. Those
are the claims here.

The failure directions are the half that earns the runtime:

* a blocking pre-submit gate that exits non-zero stops the flow *before* submit,
  and the bare remote is asked afterwards whether the branch arrived -- a real
  remote with no ref is a stronger statement than an unexecuted stage result;
* a teardown that cannot remove a row reports it KEPT, with the tree still on
  disk and the row still active, rather than closing the row out on a removal
  that did not happen;
* a run whose delivery never reached review does not read as succeeded, and ends
  in a failed state rather than a done one.

**What is not exercised, and why.** A fully live run is not reachable: there is
no production ``SessionOpener`` and no production ``TurnHost``, so nothing
constructs the worker and reviewer these tests pass in. The wave loop, the
delivery pipeline, the workspace broker, the ledger and the janitor around them
are the real ones, built through :func:`orchestrator_for` -- the same factory a
caller uses -- so the wiring is exercised even though the model seam is not.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

import pytest

from kiro_crew import platform_compat
from kiro_crew.apps.builtins.spec_engine.engine.audit import AuditLog
from kiro_crew.apps.builtins.spec_engine.engine.autonomy import (
    AUTONOMY_FIELD,
    AutonomyDecision,
    AutonomyLevel,
)
from kiro_crew.apps.builtins.spec_engine.engine.budget import (
    MeteringLedger,
    RecordingNotifier,
    RunAccounting,
)
from kiro_crew.apps.builtins.spec_engine.engine.config import (
    DASHBOARD_SURFACE,
    ConfigStore,
    ValueOrigin,
)
from kiro_crew.apps.builtins.spec_engine.engine.config.schema import (
    GATE_POSITION_PRE_SUBMIT,
    GATE_SEVERITY_BLOCKING,
    SECTION_QUALITY_GATES,
)
from kiro_crew.apps.builtins.spec_engine.engine.delivery import (
    DEPLOYMENT_KIND,
    ISOLATE_STAGE,
    PUBLISH_STAGE,
    REASON_NO_STAGE,
    SUBMIT_STAGE,
    TEARDOWN_STAGE,
    VERIFY_STAGE,
    WORKTREE_KIND,
    DeliveryOutcome,
    RunContext,
    StageExecutor,
    StageOutcome,
    StageResult,
    WorkspaceJanitor,
    plan_workspace,
    resolve_authority,
)
from kiro_crew.apps.builtins.spec_engine.engine.notify import (
    DASHBOARD_CHANNEL,
    ChannelRoute,
    Delivery,
)
from kiro_crew.apps.builtins.spec_engine.engine.orchestrator import (
    ExecutionOutcome,
    ReviewVerdict,
    TaskResult,
    WaveRunner,
    orchestrator_for,
    workspace_root,
)
from kiro_crew.apps.builtins.spec_engine.engine.review_queue import ReviewQueue
from kiro_crew.apps.builtins.spec_engine.engine.roles import Dispatch, SessionDefault
from kiro_crew.apps.builtins.spec_engine.engine.runs import RunMachine, RunState
from kiro_crew.apps.builtins.spec_engine.engine.state import SpecRef, StateStore

from .conftest import make_spec_dir
from .test_orchestrator_waves import write_tasks

pytestmark = [
    pytest.mark.skipif(
        not platform_compat.IS_POSIX,
        reason="the stage stand-ins are exec-bit shell scripts, which is a POSIX mechanism",
    ),
    pytest.mark.skipif(shutil.which("git") is None, reason="a real git is the point of this suite"),
]

PROJECT = "acme"
SOURCE = "tracker"
BASE = "main"
RUN = "run-e2e"
SPEC = "example"
BRANCH = "spec/e2e-topic"

#: Addresses the stand-in commands print. ``.invalid`` is reserved and
#: unresolvable, so a stage or an assertion that tried to reach one would fail
#: rather than quietly depend on the network.
REVIEW_URL = "https://review.invalid/artifact/7"
DEPLOY_URL = "https://deploy.invalid/environments/e2e"


def git(*argv: str, cwd: Path) -> str:
    """Run a real git command, failing the test loudly when it does not work."""
    completed = subprocess.run(
        [
            "git",
            "-c",
            "user.name=Spec Engine Test",
            "-c",
            "user.email=spec-engine@example.invalid",
            "-c",
            "commit.gpgsign=false",
            *argv,
        ],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(
            f"git {' '.join(argv)} in {cwd} exited {completed.returncode}: {completed.stderr}"
        )
    return completed.stdout


def script(directory: Path, name: str, body: str) -> Path:
    """Write an executable stand-in for a stage command.

    A file with the exec bit rather than an interpreter invocation, because that
    is what an operator configures: the stage's own program resolution and the
    sandboxed spawn are then part of what is exercised.
    """
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    # ``set -e`` so a stand-in that could not do what it was asked -- an
    # unwritable marker, a missing directory -- fails the stage instead of
    # reaching its final ``echo`` and reporting success.
    path.write_text("#!/bin/sh\nset -e\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return path


@dataclass(frozen=True)
class Repo:
    """A real repository, its local bare remote, and the engine state around it."""

    project: Path
    remote: Path
    ref: SpecRef
    config: ConfigStore
    state: StateStore
    audit: AuditLog
    notifier: RecordingNotifier
    accounting: RunAccounting
    bin_dir: Path
    marker_dir: Path

    @property
    def machine(self) -> RunMachine:
        return RunMachine(self.state, self.config, project=PROJECT, audit=self.audit)

    @property
    def workspaces(self) -> Path:
        return workspace_root(self.state)

    def worktree_for(self, run_id: str = RUN) -> Path:
        """Where the broker will plan *run_id*'s checkout.

        Derived through the production planner rather than restated, so a change
        to how a path is derived moves this with it.
        """
        return plan_workspace(self.workspaces, run_id=run_id, context=self.context()).location

    def context(self, **overrides: str) -> RunContext:
        values: dict[str, str] = {
            "spec_name": SPEC,
            "spec_type": "feature",
            "workspace_path": str(self.project),
            "base_branch": BASE,
            "branch_name": BRANCH,
            "review_title": "E2E change",
            "review_summary": "Delivered by the end-to-end suite.",
        }
        values.update(overrides)
        return RunContext(**values)

    def start_run(self, run_id: str = RUN) -> str:
        machine = self.machine
        machine.create(self.ref, run_id=run_id)
        machine.transition(self.ref, run_id, RunState.EXECUTING)
        return run_id

    def marker(self, name: str) -> Path:
        return self.marker_dir / name

    def remote_ref(self, branch: str = BRANCH) -> str:
        """The commit the bare remote holds for *branch*, empty when it holds none."""
        completed = subprocess.run(
            ["git", "--git-dir", str(self.remote), "rev-parse", "--verify", f"refs/heads/{branch}"],
            capture_output=True,
            text=True,
            check=False,
        )
        return completed.stdout.strip() if completed.returncode == 0 else ""

    def rows(self, run_id: str = RUN, *, include_cleaned: bool = False) -> list[tuple[str, bool]]:
        """``(kind, cleaned)`` for every ledger row of *run_id*, in row order."""
        return [
            (record.kind, record.cleaned)
            for record in self.state.list_workspaces(run_id=run_id, include_cleaned=include_cleaned)
        ]

    def stages(self, **stages: list[list[str]]) -> None:
        """Write *stages* as this project's delivery workflow."""
        self.config.write(
            {"projects": {PROJECT: {"path": str(self.project), "workflow": {"stages": stages}}}},
            surface=DASHBOARD_SURFACE,
        )

    def gate(self, name: str, argv: list[str]) -> None:
        """Configure one blocking pre-submit quality gate."""
        self.config.write(
            {
                SECTION_QUALITY_GATES: [
                    {
                        "name": name,
                        "position": GATE_POSITION_PRE_SUBMIT,
                        "severity": GATE_SEVERITY_BLOCKING,
                        "commands": [argv],
                    }
                ]
            },
            surface=DASHBOARD_SURFACE,
        )


class Worker:
    """A task worker that edits the run's real checkout, as a real one would.

    Stands in for the model. What it does on disk is not simulated: it writes a
    file into the worktree and, unless the test wants a dirty tree, commits it
    with real git. That commit is what the submit stage pushes to the bare remote,
    so the remote's ref afterwards is evidence the run's own work travelled.
    """

    def __init__(self, checkout: Path, *, commit: bool = True, fail: Sequence[str] = ()) -> None:
        self._checkout = checkout
        self._commit = commit
        self._fail = set(fail)
        self.dispatched: list[str] = []
        self.saw_checkout: list[bool] = []

    def __call__(self, *, task: str, dispatch: Dispatch, context: RunContext) -> TaskResult:
        self.dispatched.append(task)
        self.saw_checkout.append(self._checkout.is_dir())
        if task in self._fail:
            return TaskResult(ok=False, reason=f"task {task} did not finish")
        target = self._checkout / f"task-{task.replace('.', '-')}.txt"
        target.write_text(f"work for {task}\n", encoding="utf-8")
        if self._commit:
            git("add", "--all", cwd=self._checkout)
            git("commit", "--message", f"task {task}", cwd=self._checkout)
        return TaskResult(ok=True)


class Reviewer:
    """Approves every task and records that it was asked."""

    def __init__(self) -> None:
        self.seen: list[str] = []

    def __call__(self, *, task: str, dispatch: Dispatch, context: RunContext) -> ReviewVerdict:
        self.seen.append(task)
        return ReviewVerdict(approved=True, reason=f"{task} approved")


@dataclass
class SendRecorder:
    """Collects the delivery notice instead of handing it to a channel.

    Returns a real :class:`Delivery` over a real :class:`ChannelRoute`, so the
    pipeline's own reading of the route -- which channel it reached, whether the
    router substituted one -- is exercised rather than answered by a stub shape.
    """

    sent: list[Delivery] = field(default_factory=list)

    def send(
        self,
        title: str,
        body: str = "",
        *,
        quoted: str = "",
        channel: str = "",
        priority: str | None = None,
        group_key: str = "",
        detail: Mapping[str, Any] | None = None,
    ) -> Delivery:
        delivered = Delivery(
            route=ChannelRoute(channel_id=DASHBOARD_CHANNEL, origin=ValueOrigin.BUNDLED_DEFAULT),
            title=title,
            body=body,
            priority=priority or "",
            group_key=group_key,
            detail=dict(detail or {}),
        )
        self.sent.append(delivered)
        return delivered


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Repo:
    """A repository with one commit, a local bare remote, and a spec to run."""
    # A hermetic git: the user's global configuration must not decide whether
    # this suite passes, and a stage command spawned by the executor inherits the
    # process environment.
    empty_config = tmp_path / "gitconfig"
    empty_config.write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty_config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    monkeypatch.delenv("GIT_DIR", raising=False)
    monkeypatch.delenv("GIT_WORK_TREE", raising=False)

    remote = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch", BASE, str(remote)],
        check=True,
        capture_output=True,
    )
    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(
        ["git", "init", "--initial-branch", BASE, str(project)], check=True, capture_output=True
    )
    spec_dir = make_spec_dir(project, SPEC)
    write_tasks(spec_dir, [["1.1"]])
    (project / "README.md").write_text("base\n", encoding="utf-8")
    git("add", "--all", cwd=project)
    git("commit", "--message", "base", cwd=project)
    # A path, not a URL: the whole suite is offline, and a bare repository on
    # disk is a real remote in every way that matters to git.
    git("remote", "add", "origin", str(remote), cwd=project)
    git("push", "origin", BASE, cwd=project)

    state = StateStore(root=tmp_path / "engine-state")
    config = ConfigStore(tmp_path / "config")
    config.write(
        {"projects": {PROJECT: {"path": str(project), "base_branch": BASE}}},
        surface=DASHBOARD_SURFACE,
    )
    ledger_path = tmp_path / "usage" / "tokens"
    markers = tmp_path / "markers"
    markers.mkdir()
    return Repo(
        project=project,
        remote=remote,
        ref=SpecRef.of(project, SPEC),
        config=config,
        state=state,
        audit=AuditLog(tmp_path / "audit"),
        notifier=RecordingNotifier(),
        accounting=RunAccounting(state, ledger=MeteringLedger(ledger_path)),
        bin_dir=tmp_path / "bin",
        marker_dir=markers,
    )


def isolate_commands() -> list[list[str]]:
    """The local-only isolate step: cut the run's branch from the local base ref.

    No fetch, because there is nothing to fetch from that is not already on this
    disk -- the bundled ``local-only`` preset makes the same choice for the same
    reason.
    """
    return [["git", "worktree", "add", "{isolated_path}", "-b", "{branch_name}", "{base_branch}"]]


def submit_commands(repo: Repo) -> list[list[str]]:
    """Submit: prove the checkout is still there, then push it to the remote.

    The checkout path arrives as a literal rather than as ``{isolated_path}``
    because only the isolate stage is handed that variable; the delivery stages
    run in the project's own tree. The literal is computed from the production
    planner, so it is the path the broker really claimed.
    """
    probe = script(
        repo.bin_dir,
        "submit-probe",
        # Exits non-zero when the run's checkout is gone, which is the assertion
        # that delivery is sequenced before the retire that removes it.
        'if [ ! -d "$1" ]; then echo "the run checkout is gone: $1" >&2; exit 9; fi\n'
        'ls "$1" > "$2"\n'
        'echo "$3"\n',
    )
    return [
        [str(probe), str(repo.worktree_for()), str(repo.marker("submit")), REVIEW_URL],
        ["git", "push", "origin", "{branch_name}"],
    ]


def marker_command(repo: Repo, name: str, *, exit_code: int = 0, prints: str = "") -> list[str]:
    """A stand-in that records that it ran, prints *prints*, and exits *exit_code*."""
    body = 'echo "ran" > "$1"\n'
    if prints:
        body += f'echo "{prints}"\n'
    body += f"exit {exit_code}\n"
    return [str(script(repo.bin_dir, f"stage-{name}", body)), str(repo.marker(name))]


def runner_for(
    repo: Repo,
    worker: Worker,
    *,
    reviewer: Reviewer | None = None,
    notifier: SendRecorder | None = None,
    run_id: str = RUN,
) -> WaveRunner:
    """Build the run through the factory a real caller uses, with a real runner.

    No command runner is passed, so the executor and the janitor both spawn real
    processes through the package's own spawn chokepoint.
    """
    return orchestrator_for(
        repo.ref,
        run_id,
        state=repo.state,
        config=repo.config,
        authority=resolve_authority(
            repo.config,
            decision=AutonomyDecision(
                level=AutonomyLevel.DELIVERY,
                source=SOURCE,
                spec_type="feature",
                submitter_class="maintainer",
                declared_at=f"sources.{SOURCE}.{AUTONOMY_FIELD}.maintainer.feature",
            ),
            project=PROJECT,
            base_branch=BASE,
        ),
        worker=worker,
        reviewer=reviewer if reviewer is not None else Reviewer(),
        project=PROJECT,
        session_default=SessionDefault(model="session-model"),
        audit=repo.audit,
        notifier=repo.notifier,
        delivery_notifier=notifier,
        accounting=repo.accounting,
    )


def teardown_stage(repo: Repo, commands: list[list[str]]) -> WorkspaceJanitor:
    """A janitor whose teardown stage really runs *commands*.

    The default build wires no stage runner into the janitor, so a deployment row
    is reported rather than closed out. A driver that configured teardown commands
    supplies one; this is that supply, through the same stage executor the
    delivery pipeline uses.
    """
    repo.stages(**{TEARDOWN_STAGE: commands})
    executor = StageExecutor(repo.config, project=PROJECT)

    def run_stage(run_id: str) -> StageResult:
        return executor.run(TEARDOWN_STAGE, repo.context())

    return WorkspaceJanitor(
        repo.state,
        root=repo.workspaces,
        stage=run_stage,
        audit=None,
    )


class TestIsolateThroughTeardown:
    """The whole delivery lifecycle over one real repository."""

    def test_a_run_materializes_a_worktree_publishes_and_has_it_swept_away(
        self, repo: Repo
    ) -> None:
        repo.stages(
            **{
                ISOLATE_STAGE: isolate_commands(),
                SUBMIT_STAGE: submit_commands(repo),
                VERIFY_STAGE: [marker_command(repo, VERIFY_STAGE)],
                PUBLISH_STAGE: [marker_command(repo, PUBLISH_STAGE, prints=DEPLOY_URL)],
            }
        )
        checkout = repo.worktree_for()
        repo.start_run()
        worker = Worker(checkout)
        notifier = SendRecorder()
        runner = runner_for(repo, worker, notifier=notifier)

        report = runner.run(repo.context())

        # --- the tree was really there, and the work was really in it ---------
        assert worker.saw_checkout == [True], "the worker ran before the checkout existed"
        assert report.isolation is not None
        assert report.isolation.outcome is StageOutcome.PASSED
        listing = repo.marker("submit").read_text(encoding="utf-8")
        assert "task-1-1.txt" in listing, "submit did not see the run's own work in the checkout"

        # --- the delivery ran, in order, and reached the real remote ----------
        assert report.outcome is ExecutionOutcome.COMPLETED, report.reason
        assert report.delivery is not None
        delivery = report.delivery.delivery
        assert delivery is not None
        assert delivery.outcome is DeliveryOutcome.PASSED, delivery.reason
        assert [stage for stage, _ in delivery.stage_outcomes()] == [
            ISOLATE_STAGE,
            SUBMIT_STAGE,
            VERIFY_STAGE,
            PUBLISH_STAGE,
        ]
        pushed = repo.remote_ref()
        assert pushed, "the run's branch never reached the bare remote"
        assert pushed != repo.remote_ref(BASE), "the branch pushed nothing the run did"
        assert delivery.deployment_addresses == (DEPLOY_URL,)
        assert notifier.sent, "a delivery that finished told nobody"
        assert DEPLOY_URL in notifier.sent[0].body
        assert delivery.notice is not None and delivery.notice.delivered is True

        # --- the sweep took the checkout and left the deployment --------------
        assert not checkout.exists(), "the run's checkout survived the terminal sweep"
        assert repo.rows() == [
            (DEPLOYMENT_KIND, False)
        ], "the sweep must close the worktree row and leave the deployment standing"
        assert repo.rows(include_cleaned=True) == [
            (WORKTREE_KIND, True),
            (DEPLOYMENT_KIND, False),
        ]
        # The branch and its commits are what the run produced, and removing a
        # checkout must not touch them.
        assert git("rev-parse", "--verify", f"refs/heads/{BRANCH}", cwd=repo.project).strip()
        record = repo.state.get_run(RUN)
        assert record is not None
        assert record.state == RunState.DONE.value

    def test_the_published_address_is_recorded_against_the_run_that_published_it(
        self, repo: Repo
    ) -> None:
        repo.stages(
            **{
                ISOLATE_STAGE: isolate_commands(),
                SUBMIT_STAGE: submit_commands(repo),
                PUBLISH_STAGE: [marker_command(repo, PUBLISH_STAGE, prints=DEPLOY_URL)],
            }
        )
        repo.start_run()

        runner_for(repo, Worker(repo.worktree_for())).run(repo.context())

        deployments = [
            record
            for record in repo.state.list_workspaces(run_id=RUN, include_cleaned=True)
            if record.kind == DEPLOYMENT_KIND
        ]
        assert [record.address for record in deployments] == [DEPLOY_URL]
        assert deployments[0].disposable is False
        assert deployments[0].run_id == RUN

    def test_the_configured_teardown_commands_close_the_deployment_out(self, repo: Repo) -> None:
        repo.stages(
            **{
                ISOLATE_STAGE: isolate_commands(),
                SUBMIT_STAGE: submit_commands(repo),
                PUBLISH_STAGE: [marker_command(repo, PUBLISH_STAGE, prints=DEPLOY_URL)],
            }
        )
        repo.start_run()
        runner_for(repo, Worker(repo.worktree_for())).run(repo.context())
        assert repo.rows() == [(DEPLOYMENT_KIND, False)]

        janitor = teardown_stage(repo, [marker_command(repo, TEARDOWN_STAGE)])
        report = ReviewQueue(repo.machine, janitor=janitor).teardown_run_workspaces(RUN)

        assert repo.marker(TEARDOWN_STAGE).exists(), "the teardown commands never ran"
        assert report.stage is not None and report.stage.outcome is StageOutcome.PASSED
        assert [cleanup.kind for cleanup in report.removed] == [DEPLOYMENT_KIND]
        assert report.complete is True
        assert repo.rows() == []


class TestTheFailureDirections:
    """The half of a lifecycle suite that is worth its runtime."""

    def test_a_blocking_gate_that_exits_non_zero_stops_the_flow_before_submit(
        self, repo: Repo
    ) -> None:
        repo.stages(
            **{
                ISOLATE_STAGE: isolate_commands(),
                SUBMIT_STAGE: submit_commands(repo),
                PUBLISH_STAGE: [marker_command(repo, PUBLISH_STAGE, prints=DEPLOY_URL)],
            }
        )
        repo.gate("tests", marker_command(repo, "gate", exit_code=4))
        checkout = repo.worktree_for()
        repo.start_run()

        report = runner_for(repo, Worker(checkout)).run(repo.context())

        assert repo.marker("gate").exists(), "the gate never ran, so nothing was proved"
        assert report.delivery is not None
        delivery = report.delivery.delivery
        assert delivery is not None
        assert delivery.outcome is DeliveryOutcome.FAILED
        assert delivery.not_reached == (SUBMIT_STAGE, VERIFY_STAGE, PUBLISH_STAGE)
        # Not "the stage result says skipped": the stand-ins leave markers, and a
        # real remote is asked whether the change arrived.
        assert not repo.marker("submit").exists()
        assert not repo.marker(PUBLISH_STAGE).exists()
        assert repo.remote_ref() == "", "a change that failed its gate reached the remote"
        assert delivery.deployment_addresses == ()
        assert repo.rows(include_cleaned=True) == [(WORKTREE_KIND, True)]

    def test_a_failing_verify_stops_publish_and_records_no_deployment(self, repo: Repo) -> None:
        repo.stages(
            **{
                ISOLATE_STAGE: isolate_commands(),
                SUBMIT_STAGE: submit_commands(repo),
                VERIFY_STAGE: [marker_command(repo, VERIFY_STAGE, exit_code=1)],
                PUBLISH_STAGE: [marker_command(repo, PUBLISH_STAGE, prints=DEPLOY_URL)],
            }
        )
        repo.start_run()

        report = runner_for(repo, Worker(repo.worktree_for())).run(repo.context())

        assert repo.marker(VERIFY_STAGE).exists()
        assert not repo.marker(PUBLISH_STAGE).exists(), "publish ran on an unverified change"
        assert report.delivery is not None
        delivery = report.delivery.delivery
        assert delivery is not None
        assert delivery.outcome is DeliveryOutcome.FAILED
        assert delivery.not_reached == (PUBLISH_STAGE,)
        # The submit did happen, so the branch is on the remote: the claim is
        # about publish, not about undoing what already left the host.
        assert repo.remote_ref()
        assert not any(kind == DEPLOYMENT_KIND for kind, _ in repo.rows(include_cleaned=True))

    def test_a_delivery_that_never_reached_review_does_not_read_as_succeeded(
        self, repo: Repo
    ) -> None:
        repo.stages(
            **{
                ISOLATE_STAGE: isolate_commands(),
                SUBMIT_STAGE: [marker_command(repo, SUBMIT_STAGE, exit_code=3)],
            }
        )
        repo.start_run()

        report = runner_for(repo, Worker(repo.worktree_for())).run(repo.context())

        assert repo.marker(SUBMIT_STAGE).exists()
        assert report.delivery is not None
        assert report.delivery.submitted is False
        assert report.outcome is ExecutionOutcome.FAILED
        assert "did not reach review" in report.reason
        record = repo.state.get_run(RUN)
        assert record is not None
        assert record.state == RunState.FAILED.value
        assert report.completion is not None
        assert report.completion.final_state is RunState.FAILED

    def test_a_checkout_that_cannot_be_removed_is_reported_kept_not_swept(self, repo: Repo) -> None:
        """A dirty worktree: git refuses the removal, so the row stays open.

        The whole failure mode this asserts against is a teardown that reports
        success on a removal that did not happen -- the disk fills up and the
        ledger says everything is clean. Real git is what makes it a genuine
        refusal rather than a stubbed exit code.
        """
        repo.stages(
            **{
                ISOLATE_STAGE: isolate_commands(),
                SUBMIT_STAGE: [marker_command(repo, SUBMIT_STAGE)],
            }
        )
        checkout = repo.worktree_for()
        repo.start_run()
        # Uncommitted edits are the evidence of why a run failed, so the sweep
        # deliberately does not force. This worker leaves them.
        runner = runner_for(repo, Worker(checkout, commit=False))

        report = runner.run(repo.context())

        assert report.completion is not None
        assert checkout.is_dir(), "the sweep discarded uncommitted work"
        assert (checkout / "task-1-1.txt").exists()
        assert repo.rows() == [(WORKTREE_KIND, False)], "a kept tree must keep its ledger row"
        kept = [
            event
            for event in repo.audit.read(repo.ref)
            if event.detail is not None and event.detail.get("kept")
        ]
        assert kept, "a teardown that kept a tree left no record of it"
        reasons = [row["reason"] for row in kept[-1].detail["kept"]]  # type: ignore[index]
        assert any("kept" in reason for reason in reasons)
        assert any("uncommitted work is discarded only on archive" in reason for reason in reasons)

        # And the archive is the authority that finally releases it.
        release = ReviewQueue(
            repo.machine,
            janitor=WorkspaceJanitor(repo.state, root=repo.workspaces),
        ).teardown_run_workspaces(RUN)
        assert [cleanup.kind for cleanup in release.removed] == [WORKTREE_KIND]
        assert not checkout.exists()
        assert repo.rows() == []

    def test_a_deployment_nobody_can_tear_down_is_kept_and_the_report_is_not_complete(
        self, repo: Repo
    ) -> None:
        repo.stages(
            **{
                ISOLATE_STAGE: isolate_commands(),
                SUBMIT_STAGE: submit_commands(repo),
                PUBLISH_STAGE: [marker_command(repo, PUBLISH_STAGE, prints=DEPLOY_URL)],
            }
        )
        repo.start_run()
        runner_for(repo, Worker(repo.worktree_for())).run(repo.context())

        # A teardown stage that fails, which is the case a live environment
        # outlives every record of itself if the row is closed out anyway.
        janitor = teardown_stage(repo, [marker_command(repo, "teardown-fails", exit_code=2)])
        failed = ReviewQueue(repo.machine, janitor=janitor).teardown_run_workspaces(RUN)

        assert failed.stage is not None and failed.stage.outcome is StageOutcome.FAILED
        assert [cleanup.kind for cleanup in failed.kept] == [DEPLOYMENT_KIND]
        assert failed.complete is False
        assert repo.rows() == [(DEPLOYMENT_KIND, False)]

        # And with no runner wired at all -- the default build -- the row is kept
        # for a named reason rather than silently closed.
        unwired = ReviewQueue(
            repo.machine,
            janitor=WorkspaceJanitor(repo.state, root=repo.workspaces),
        ).teardown_run_workspaces(RUN)
        assert unwired.stage is None
        assert unwired.stage_reason == REASON_NO_STAGE
        assert [cleanup.kind for cleanup in unwired.kept] == [DEPLOYMENT_KIND]
        assert unwired.complete is False
        assert repo.rows() == [(DEPLOYMENT_KIND, False)]


class TestTheEnvironmentTheseClaimsRestOn:
    """Two facts the rest of the file would silently pass without."""

    def test_the_remote_is_a_local_bare_repository_and_nothing_reaches_a_network(
        self, repo: Repo
    ) -> None:
        assert (repo.remote / "HEAD").is_file()
        assert not (repo.remote / ".git").exists()
        configured = git("remote", "get-url", "origin", cwd=repo.project).strip()
        assert configured == str(repo.remote)
        assert "://" not in configured
        assert os.environ.get("GIT_CONFIG_NOSYSTEM") == "1"

    def test_the_worktree_root_is_outside_the_project_tree(self, repo: Repo) -> None:
        # A worktree inside the project tree reads as untracked files in the
        # parent checkout, and the broker refuses it -- so a suite whose root
        # happened to sit inside would be asserting on refusals.
        assert not repo.workspaces.is_relative_to(repo.project)
