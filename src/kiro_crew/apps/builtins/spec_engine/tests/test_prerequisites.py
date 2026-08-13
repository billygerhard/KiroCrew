"""Phase-scoped prerequisites: refused up front, per phase, at zero cost."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from kiro_crew.apps.builtins.spec_engine.engine.audit import AuditLog
from kiro_crew.apps.builtins.spec_engine.engine.autonomy import AutonomyLevel
from kiro_crew.apps.builtins.spec_engine.engine.budget.ceiling import (
    CEILING_SETTING,
    Budget,
)
from kiro_crew.apps.builtins.spec_engine.engine.config.schema import (
    DELEGABLE_CAPABILITIES,
    DELIVERY_STAGES,
)
from kiro_crew.apps.builtins.spec_engine.engine.config.store import (
    DASHBOARD_SURFACE,
    ConfigStore,
)
from kiro_crew.apps.builtins.spec_engine.engine.prerequisites import (
    AUDIT_PREREQUISITE_UNMET,
    CAPABILITY_PHASES,
    STAGE_PHASES,
    CheckName,
    Prerequisite,
    PrerequisiteReport,
    RunRefusal,
    check_project,
    check_source,
    gate_run,
    stage_phase,
)
from kiro_crew.apps.builtins.spec_engine.engine.state import SpecRef

PROJECT = "proj"
PRESENT = "/usr/bin/present-tool"


def resolver(*available: str):
    """A PATH lookup that resolves only *available*, so tests describe a host."""

    def which(program: str) -> str | None:
        return f"/usr/bin/{program}" if program in available else None

    return which


def always_present(program: str) -> str | None:
    return PRESENT


def no_programs(program: str) -> str | None:
    return None


@pytest.fixture()
def config(tmp_path: Path) -> ConfigStore:
    return ConfigStore(root=tmp_path / "config")


@pytest.fixture()
def audit(tmp_path: Path) -> AuditLog:
    return AuditLog(tmp_path / "audit")


@pytest.fixture()
def ref(tmp_path: Path) -> SpecRef:
    return SpecRef.of(tmp_path / PROJECT, "spec-one")


def configure(config: ConfigStore, patch: dict[str, Any]) -> None:
    config.write(patch, surface=DASHBOARD_SURFACE)


def with_bounded_ceiling(config: ConfigStore, credits: float = 50.0) -> None:
    configure(config, {"budget": {"run_ceiling_credits": credits}})


UNBOUNDED = Budget(ceiling_credits=0.0)
"""A budget with no ceiling in force.

Constructed rather than configured because the schema refuses to write a
non-positive ceiling -- that refusal is the second layer, and this test exercises
the first one on its own. Asserting through a config write would make the
preflight check untestable and, worse, indistinguishable from a check that cannot
fail.
"""


def with_delivery_workflow(config: ConfigStore, program: str = "shipit") -> None:
    configure(
        config,
        {
            "workflow": {"stages": {"submit": [[program, "--push"]]}},
            "projects": {PROJECT: {"path": f"/w/{PROJECT}", "base_branch": "main"}},
        },
    )


def exists(_branch: str) -> bool:
    return True


def missing(_branch: str) -> bool:
    return False


class TestPhaseScoping:
    def test_every_delivery_stage_has_a_phase(self) -> None:
        """A stage with no phase would be checked at the wrong rung, or not at all."""
        assert set(STAGE_PHASES) == set(DELIVERY_STAGES)

    def test_every_delegable_capability_has_a_phase(self) -> None:
        """The mirror of the stage ratchet, so the map cannot drift unnoticed.

        The default for an unmapped capability is authoring, the lowest rung, so
        drift over-checks rather than exempts -- a false refusal, not an authority
        leak. This test is what makes the drift visible to whoever adds the
        capability instead of to a confused operator.
        """
        assert set(CAPABILITY_PHASES) == set(DELEGABLE_CAPABILITIES)

    def test_an_unknown_stage_raises_rather_than_defaulting(self) -> None:
        with pytest.raises(ValueError):
            stage_phase("deploy-to-prod")

    def test_isolate_is_executions_and_the_artifact_stages_are_deliverys(self) -> None:
        assert stage_phase("isolate") is AutonomyLevel.EXECUTION
        for stage in ("submit", "verify", "publish", "teardown"):
            assert stage_phase(stage) is AutonomyLevel.DELIVERY

    def test_a_delivery_program_is_not_checked_for_an_execution_run(
        self, config: ConfigStore
    ) -> None:
        """The point of phase scoping: a project that only authors is not blocked."""
        with_bounded_ceiling(config)
        with_delivery_workflow(config)
        report = check_project(
            config, project=PROJECT, which=no_programs, branch_exists=exists
        )

        assert report.unmet_through(AutonomyLevel.EXECUTION) == ()
        assert report.unmet_through(AutonomyLevel.DELIVERY)

    def test_findings_are_grouped_by_phase_in_ladder_order(
        self, config: ConfigStore
    ) -> None:
        with_bounded_ceiling(config)
        with_delivery_workflow(config)
        grouped = check_project(
            config, project=PROJECT, which=always_present, branch_exists=exists
        ).by_phase()

        ranks = [phase.rank for phase in grouped]
        assert ranks == sorted(ranks)

    def test_unmet_checks_are_ordered_lowest_phase_first(
        self, config: ConfigStore
    ) -> None:
        """An operator fixes the earliest blocking phase first."""
        with_delivery_workflow(config)
        unmet = check_project(
            config, project=PROJECT, which=no_programs, branch_exists=missing
        ).unmet_through(AutonomyLevel.INTEGRATION)

        ranks = [check.phase.rank for check in unmet]
        assert ranks == sorted(ranks)


class TestChecks:
    def test_a_missing_stage_program_is_unmet_at_its_stages_phase(
        self, config: ConfigStore
    ) -> None:
        with_bounded_ceiling(config)
        with_delivery_workflow(config, program="shipit")
        report = check_project(
            config, project=PROJECT, which=no_programs, branch_exists=exists
        )

        programs = [c for c in report.unmet if c.check is CheckName.PROGRAMS]
        assert [c.phase for c in programs] == [AutonomyLevel.DELIVERY]
        assert "shipit" in programs[0].missing

    def test_a_present_stage_program_is_met(self, config: ConfigStore) -> None:
        with_bounded_ceiling(config)
        with_delivery_workflow(config, program="shipit")
        report = check_project(
            config, project=PROJECT, which=resolver("shipit"), branch_exists=exists
        )
        assert report.unmet_through(AutonomyLevel.DELIVERY) == ()

    def test_a_quality_gate_program_is_checked_too(self, config: ConfigStore) -> None:
        """Gates hold argv the delivery flow runs, so an absent one stops delivery."""
        with_bounded_ceiling(config)
        with_delivery_workflow(config, program="shipit")
        configure(
            config,
            {
                "quality_gates": [
                    {
                        "name": "tests",
                        "position": "pre_submit",
                        "severity": "blocking",
                        "commands": [["pytest", "-q"]],
                    }
                ]
            },
        )
        report = check_project(
            config, project=PROJECT, which=resolver("shipit"), branch_exists=exists
        )

        unmet = [c for c in report.unmet if "pytest" in c.missing]
        assert len(unmet) == 1
        assert unmet[0].phase is AutonomyLevel.DELIVERY

    def test_a_delegated_provider_program_that_is_absent_is_unmet(
        self, config: ConfigStore
    ) -> None:
        with_bounded_ceiling(config)
        configure(
            config,
            {"capabilities": {"analysis": {"transport": "command", "command": ["analyze-it"]}}},
        )
        report = check_project(config, project=PROJECT, which=no_programs, branch_exists=exists)

        providers = [c for c in report.unmet if c.check is CheckName.PROVIDERS]
        assert len(providers) == 1
        assert "analyze-it" in providers[0].missing

    def test_builtin_providers_are_not_reported_as_unreachable(
        self, config: ConfigStore
    ) -> None:
        """A builtin binding is this engine, so no PATH lookup can fail it."""
        with_bounded_ceiling(config)
        report = check_project(config, project=PROJECT, which=no_programs, branch_exists=exists)
        assert [c for c in report.checks if c.check is CheckName.PROVIDERS] == []

    def test_an_empty_protected_set_is_unmet_at_integration(
        self, config: ConfigStore
    ) -> None:
        """The one check whose failure nothing demonstrated.

        Reachable: resolve_protected_branches returns an empty set when neither
        protected_branches nor a base branch is configured, and integration is the
        one stage a mistake cannot undo.
        """
        with_bounded_ceiling(config)
        configure(config, {"projects": {PROJECT: {"path": f"/w/{PROJECT}"}}})
        report = check_project(
            config, project=PROJECT, which=always_present, branch_exists=exists
        )

        protected = [c for c in report.unmet if c.check is CheckName.PROTECTED_BRANCHES]
        assert len(protected) == 1
        assert protected[0].phase is AutonomyLevel.INTEGRATION
        assert protected[0].action

    def test_a_configured_protected_set_is_met(self, config: ConfigStore) -> None:
        with_bounded_ceiling(config)
        configure(
            config,
            {
                "projects": {
                    PROJECT: {"path": f"/w/{PROJECT}", "protected_branches": ["main"]}
                }
            },
        )
        report = check_project(
            config, project=PROJECT, which=always_present, branch_exists=exists
        )
        assert [c for c in report.unmet if c.check is CheckName.PROTECTED_BRANCHES] == []

    def test_the_runs_own_base_is_verified_not_the_projects(
        self, config: ConfigStore
    ) -> None:
        """A watch-source run integrates into a branch the project never names.

        Verifying the project's base here would report readiness for a branch this
        run does not touch, and leave the one it does touch unchecked.
        """
        with_bounded_ceiling(config)
        with_delivery_workflow(config)
        asked: list[str] = []

        def only_main(branch: str) -> bool:
            asked.append(branch)
            return branch == "main"

        report = check_project(
            config,
            project=PROJECT,
            base_branch="release-2",
            which=always_present,
            branch_exists=only_main,
        )

        assert asked == ["release-2"]
        base = [c for c in report.unmet if c.check is CheckName.BASE_BRANCH]
        assert len(base) == 1
        assert "release-2" in base[0].missing

    def test_a_source_supplied_base_makes_the_check_run_at_all(
        self, config: ConfigStore
    ) -> None:
        """With no project base, the check used to be skipped entirely."""
        with_bounded_ceiling(config)
        configure(config, {"projects": {PROJECT: {"path": f"/w/{PROJECT}"}}})

        report = check_project(
            config,
            project=PROJECT,
            base_branch="from-the-source",
            which=always_present,
            branch_exists=missing,
        )

        assert [c.check for c in report.unmet if c.check is CheckName.BASE_BRANCH] == [
            CheckName.BASE_BRANCH
        ]

    def test_an_absent_base_branch_is_unmet_at_delivery(self, config: ConfigStore) -> None:
        with_bounded_ceiling(config)
        with_delivery_workflow(config)
        report = check_project(
            config, project=PROJECT, which=always_present, branch_exists=missing
        )

        base = [c for c in report.unmet if c.check is CheckName.BASE_BRANCH]
        assert len(base) == 1
        assert base[0].phase is AutonomyLevel.DELIVERY
        assert "main" in base[0].missing

    def test_an_unresolvable_notification_channel_is_unmet(self, config: ConfigStore) -> None:
        """resolve_channel never raises, so the check must ask whether it substituted."""
        with_bounded_ceiling(config)
        configure(config, {"notify": {"channel": "nowhere.at.all"}})
        report = check_project(config, project=PROJECT, which=always_present, branch_exists=exists)

        channel = [c for c in report.unmet if c.check is CheckName.NOTIFY_CHANNEL]
        assert len(channel) == 1
        assert channel[0].phase is AutonomyLevel.AUTHORING

    def test_a_declared_channel_is_met(self, config: ConfigStore) -> None:
        with_bounded_ceiling(config)
        report = check_project(config, project=PROJECT, which=always_present, branch_exists=exists)
        assert [c for c in report.unmet if c.check is CheckName.NOTIFY_CHANNEL] == []

    def test_an_unbounded_ceiling_is_unmet_for_every_level_above_authoring(
        self, config: ConfigStore
    ) -> None:
        report = check_project(
            config,
            project=PROJECT,
            which=always_present,
            branch_exists=exists,
            budget=UNBOUNDED,
        )

        ceilings = [c for c in report.unmet if c.check is CheckName.BUDGET_CEILING]
        assert {c.phase for c in ceilings} == {
            AutonomyLevel.EXECUTION,
            AutonomyLevel.DELIVERY,
            AutonomyLevel.INTEGRATION,
        }

    def test_authoring_needs_no_ceiling(self, config: ConfigStore) -> None:
        """Authoring cannot start an unattended run, so it is exempt by design."""
        report = check_project(
            config,
            project=PROJECT,
            which=always_present,
            branch_exists=exists,
            budget=UNBOUNDED,
        )
        assert report.unmet_through(AutonomyLevel.AUTHORING) == ()

    def test_the_bundled_default_ceiling_is_already_bounded(
        self, config: ConfigStore
    ) -> None:
        """The second layer: an install that configures nothing is still bounded."""
        report = check_project(config, project=PROJECT, which=always_present, branch_exists=exists)
        assert [c for c in report.unmet if c.check is CheckName.BUDGET_CEILING] == []

    def test_the_ceiling_action_names_the_setting_an_operator_edits(
        self, config: ConfigStore
    ) -> None:
        report = check_project(
            config,
            project=PROJECT,
            which=always_present,
            branch_exists=exists,
            budget=UNBOUNDED,
        )
        ceilings = [c for c in report.unmet if c.check is CheckName.BUDGET_CEILING]
        assert ceilings
        assert all(CEILING_SETTING in c.action for c in ceilings)


class TestWatchSourceChecks:
    def test_an_absent_poll_program_is_unmet_and_never_reads_as_no_items(
        self, config: ConfigStore
    ) -> None:
        configure(config, {"sources": {"tracker": {"poll": ["list-issues", "--json"]}}})
        report = check_source(config, "tracker", which=no_programs)

        assert not report.met
        unmet = report.unmet[0]
        assert unmet.check is CheckName.WATCH_PROGRAMS
        assert unmet.source == "tracker"
        assert "list-issues" in unmet.missing
        # The distinction that matters: this is a source that cannot look, not a
        # source that looked and found nothing.
        assert "no items" not in unmet.missing.lower()

    def test_a_present_poll_program_is_met(self, config: ConfigStore) -> None:
        configure(config, {"sources": {"tracker": {"poll": ["list-issues"]}}})
        assert check_source(config, "tracker", which=resolver("list-issues")).met

    def test_a_source_that_is_not_configured_at_all_is_unmet(
        self, config: ConfigStore
    ) -> None:
        """Asking about an unknown source must not read as a healthy empty source."""
        configure(config, {"sources": {"tracker": {"poll": ["list-issues"]}}})
        report = check_source(config, "never-declared", which=always_present)
        assert not report.met
        assert "no poll command" in report.unmet[0].missing
        assert report.unmet[0].source == "never-declared"


class TestRunGate:
    def test_a_run_is_refused_before_any_credit_when_a_later_phase_is_unmet(
        self, config: ConfigStore, audit: AuditLog, ref: SpecRef
    ) -> None:
        """The task's property: an absent delivery program stops the run up front."""
        with_bounded_ceiling(config)
        with_delivery_workflow(config, program="shipit")

        refusal = gate_run(
            config,
            AutonomyLevel.DELIVERY,
            audit,
            ref,
            project=PROJECT,
            run="run-1",
            which=no_programs,
            branch_exists=exists,
        )

        assert refusal is not None
        assert refusal.level is AutonomyLevel.DELIVERY
        assert any("shipit" in check.missing for check in refusal.unmet)

    def test_the_refusal_costs_zero_credits(
        self, config: ConfigStore, audit: AuditLog, ref: SpecRef
    ) -> None:
        with_bounded_ceiling(config)
        with_delivery_workflow(config, program="shipit")
        gate_run(
            config,
            AutonomyLevel.DELIVERY,
            audit,
            ref,
            project=PROJECT,
            which=no_programs,
            branch_exists=exists,
        )

        events = audit.read(ref)
        assert [event.event for event in events] == [AUDIT_PREREQUISITE_UNMET]
        assert events[0].cost == 0.0

    def test_the_refusal_is_recorded_in_the_audit_log(
        self, config: ConfigStore, audit: AuditLog, ref: SpecRef
    ) -> None:
        with_bounded_ceiling(config)
        with_delivery_workflow(config, program="shipit")
        gate_run(
            config,
            AutonomyLevel.DELIVERY,
            audit,
            ref,
            project=PROJECT,
            run="run-7",
            which=no_programs,
            branch_exists=exists,
        )

        detail = audit.read(ref)[0].detail or {}
        assert detail["autonomy"] == "delivery"
        assert detail["unmet"]
        assert all("action" in item and item["action"] for item in detail["unmet"])

    def test_a_permitted_run_returns_none_and_records_nothing(
        self, config: ConfigStore, audit: AuditLog, ref: SpecRef
    ) -> None:
        with_bounded_ceiling(config)
        with_delivery_workflow(config, program="shipit")

        assert (
            gate_run(
                config,
                AutonomyLevel.DELIVERY,
                audit,
                ref,
                project=PROJECT,
                which=resolver("shipit"),
                branch_exists=exists,
            )
            is None
        )
        assert audit.read(ref) == []

    def test_an_execution_run_is_not_stopped_by_a_delivery_gap(
        self, config: ConfigStore, audit: AuditLog, ref: SpecRef
    ) -> None:
        """Scoping is what makes the gate usable: it refuses what it must, only."""
        with_bounded_ceiling(config)
        with_delivery_workflow(config, program="shipit")

        assert (
            gate_run(
                config,
                AutonomyLevel.EXECUTION,
                audit,
                ref,
                project=PROJECT,
                which=no_programs,
                branch_exists=exists,
            )
            is None
        )

    def test_the_gate_reports_every_unmet_prerequisite_not_only_the_first(
        self, config: ConfigStore, audit: AuditLog, ref: SpecRef
    ) -> None:
        with_delivery_workflow(config, program="shipit")

        refusal = gate_run(
            config,
            AutonomyLevel.DELIVERY,
            audit,
            ref,
            project=PROJECT,
            which=no_programs,
            branch_exists=missing,
            budget=UNBOUNDED,
        )

        assert refusal is not None
        assert {check.check for check in refusal.unmet} >= {
            CheckName.PROGRAMS,
            CheckName.BASE_BRANCH,
            CheckName.BUDGET_CEILING,
        }


class TestResultShape:
    def test_an_unmet_check_must_say_what_is_missing_and_what_resolves_it(self) -> None:
        """R32.2 in the type: an unmet finding without an action cannot exist."""
        with pytest.raises(ValueError):
            Prerequisite(
                check=CheckName.PROGRAMS, phase=AutonomyLevel.DELIVERY, met=False, missing="gone"
            )
        with pytest.raises(ValueError):
            Prerequisite(
                check=CheckName.PROGRAMS,
                phase=AutonomyLevel.DELIVERY,
                met=False,
                action="install it",
            )

    def test_a_met_check_needs_neither(self) -> None:
        check = Prerequisite(
            check=CheckName.PROGRAMS, phase=AutonomyLevel.DELIVERY, met=True
        )
        assert check.describe().endswith("met")

    def test_a_refusal_must_carry_its_causes(self) -> None:
        with pytest.raises(ValueError):
            RunRefusal(level=AutonomyLevel.DELIVERY, unmet=())

    def test_an_empty_report_is_met(self) -> None:
        assert PrerequisiteReport().met
