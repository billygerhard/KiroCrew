"""Property-based tests for the delivery flow's orderings.

Two claims, both of which a scripted example can only sample.

**Publish never runs while verification is failing.** Whatever the sequence of
verify exit codes and whatever the retry limit, the publish command runs only in
a delivery whose final verification passed, and it runs at most once. The failure
this guards against is a retry loop whose bookkeeping lets one path fall through
to publish — a half-verified change reaching somewhere it is consumed, which no
later assertion about the run's outcome would catch.

**Unattended integration needs both gates and cannot exceed the ceiling.** Across
every combination of resolved ladder rung, posture switch, and workflow presence,
integration is permitted exactly when the capped rung reaches integration, the
switch is on, verification holds, and a target is named. Capping never raises a
rung, so a project that configured no workflow has no combination that reaches
integration.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

from hypothesis import given, settings
from hypothesis import strategies as st

from kiro_crew.apps.builtins.spec_engine.engine.autonomy import (
    AUTONOMY_FIELD,
    AutonomyDecision,
    AutonomyLevel,
)
from kiro_crew.apps.builtins.spec_engine.engine.config import (
    AUTONOMY_LEVELS,
    DASHBOARD_SURFACE,
    ConfigStore,
)
from kiro_crew.apps.builtins.spec_engine.engine.delivery import (
    PUBLISH_STAGE,
    REASON_LADDER,
    REASON_POSTURE,
    VERIFY_STAGE,
    CommandOutcome,
    DeliveryOutcome,
    DeliveryPipeline,
    FixDispatch,
    RunContext,
    StageResult,
    resolve_authority,
)

#: The flow runs no processes here (the runner is scripted), so examples are
#: cheap; this is far more retry/exit-code shapes than the scripted cases cover.
MAX_EXAMPLES = 150

PROJECT = "acme"
SOURCE = "tracker"
BASE = "main"

SUBMIT_PROGRAM = "raise-review"
VERIFY_PROGRAM = "run-checks"
PUBLISH_PROGRAM = "deploy"

_EXIT_CODES = st.lists(st.sampled_from([0, 1, 2, 127]), min_size=1, max_size=6)
_RETRY_LIMITS = st.integers(min_value=0, max_value=4)
_LEVELS = st.sampled_from([AutonomyLevel(name) for name in AUTONOMY_LEVELS])


class SequenceRunner:
    """Answers verify with a fixed exit-code sequence, other programs with zero."""

    def __init__(self, verify_exits: Sequence[int]) -> None:
        self._verify = list(verify_exits)
        self.programs: list[str] = []

    def __call__(self, argv: Sequence[str], *, cwd: Path, timeout_s: int) -> CommandOutcome:
        program = argv[0]
        self.programs.append(program)
        if program != VERIFY_PROGRAM:
            return CommandOutcome(exit_code=0)
        code = self._verify.pop(0) if len(self._verify) > 1 else self._verify[0]
        return CommandOutcome(exit_code=code, stdout="https://example.test/preview")


def _document(*, retry_limit: int, auto_integrate: bool, configured: bool) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "path": "/tmp/acme",
        "base_branch": BASE,
        "delivery": {"auto_integrate": auto_integrate},
    }
    if configured:
        entry["workflow"] = {
            "stages": {
                "submit": [[SUBMIT_PROGRAM]],
                VERIFY_STAGE: [[VERIFY_PROGRAM]],
                PUBLISH_STAGE: [[PUBLISH_PROGRAM]],
            }
        }
    return {"limits": {"verify_retry_limit": retry_limit}, "projects": {PROJECT: entry}}


def _decision(level: AutonomyLevel) -> AutonomyDecision:
    return AutonomyDecision(
        level=level,
        source=SOURCE,
        spec_type="feature",
        submitter_class="maintainer",
        declared_at=f"sources.{SOURCE}.{AUTONOMY_FIELD}.maintainer.feature",
    )


def _always_fixes(*, attempt: int, stage: StageResult) -> FixDispatch:
    return FixDispatch(dispatched=True, tasks=(f"fix-{attempt}",))


def _pipeline(
    tmp_path: Path,
    *,
    document: dict[str, Any],
    level: AutonomyLevel,
    runner: SequenceRunner,
) -> DeliveryPipeline:
    store = ConfigStore(tmp_path / "state")
    store.write(document, surface=DASHBOARD_SURFACE)
    authority = resolve_authority(
        store, decision=_decision(level), project=PROJECT, base_branch=BASE
    )
    return DeliveryPipeline(
        store,
        authority=authority,
        project=PROJECT,
        runner=runner,
        fix_dispatcher=_always_fixes,
    )


def _context(workspace: Path) -> RunContext:
    return RunContext(
        spec_name="example",
        spec_type="feature",
        workspace_path=str(workspace),
        base_branch=BASE,
        branch_name="spec/example",
    )


@settings(max_examples=MAX_EXAMPLES, deadline=None)
@given(verify_exits=_EXIT_CODES, retry_limit=_RETRY_LIMITS)
def test_publish_runs_only_after_verification_passes(
    tmp_path_factory: Any, verify_exits: list[int], retry_limit: int
) -> None:
    root = Path(tmp_path_factory.mktemp("flow"))
    workspace = root / "workspace"
    workspace.mkdir()
    runner = SequenceRunner(verify_exits)
    pipeline = _pipeline(
        root,
        document=_document(retry_limit=retry_limit, auto_integrate=False, configured=True),
        level=AutonomyLevel.DELIVERY,
        runner=runner,
    )

    run = pipeline.deliver(_context(workspace))
    published = runner.programs.count(PUBLISH_PROGRAM)

    assert published <= 1
    if published:
        assert run.verified
        assert run.outcome is not DeliveryOutcome.REFUSED
        # Publish is the last thing that ran: no verification followed it.
        assert runner.programs[-1] == PUBLISH_PROGRAM
    else:
        assert not run.verified
        assert run.outcome is DeliveryOutcome.FAILED
        assert PUBLISH_STAGE in run.not_reached
    # Fix rounds are bounded by the limit, so the loop cannot spend forever.
    assert runner.programs.count(VERIFY_PROGRAM) <= retry_limit + 1


@settings(max_examples=MAX_EXAMPLES, deadline=None)
@given(
    level=_LEVELS,
    auto_integrate=st.booleans(),
    configured=st.booleans(),
    verified=st.booleans(),
    target=st.sampled_from([BASE, "development", ""]),
)
def test_integration_is_permitted_only_when_every_gate_holds(
    tmp_path_factory: Any,
    level: AutonomyLevel,
    auto_integrate: bool,
    configured: bool,
    verified: bool,
    target: str,
) -> None:
    root = Path(tmp_path_factory.mktemp("gates"))
    store = ConfigStore(root / "state")
    store.write(
        _document(retry_limit=1, auto_integrate=auto_integrate, configured=configured),
        surface=DASHBOARD_SURFACE,
    )
    authority = resolve_authority(
        store, decision=_decision(level), project=PROJECT, base_branch=BASE
    )

    decision = authority.integration(verified=verified, target=target)

    # Capping only ever lowers, so a project with no workflow cannot hold the
    # rung however the policy grid was written.
    assert authority.level.rank <= level.rank
    if not configured:
        assert not authority.permits(AutonomyLevel.DELIVERY)
        assert not decision.permitted
        assert REASON_LADDER in decision.reasons
    expected = (
        authority.permits(AutonomyLevel.INTEGRATION)
        and auto_integrate
        and verified
        and bool(target.strip())
    )
    assert decision.permitted is expected
    assert decision.requires_human_action is not expected
    if not auto_integrate:
        assert REASON_POSTURE in decision.reasons
