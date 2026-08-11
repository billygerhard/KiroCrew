"""The integration floor: protected branches, the two gates, and the config warning.

Integration is the one delivery action nothing can take back, so the claims here
are about refusal. Each test asks whether a specific way of arriving at an
unattended merge is closed: a policy grid that names integration on its own, a
posture switch flipped on its own, a project that configured nothing at all, and
a project that armed the switch with nothing verifying the change.
"""

from __future__ import annotations

from typing import Any

import pytest

from kiro_crew.apps.builtins.spec_engine.engine.autonomy import (
    AUTONOMY_FIELD,
    AutonomyDecision,
    AutonomyLevel,
    AutonomyPolicy,
)
from kiro_crew.apps.builtins.spec_engine.engine.config import (
    AUTO_INTEGRATE_SETTING,
    AUTO_INTEGRATE_WITHOUT_VERIFY,
    DASHBOARD_SURFACE,
    ConfigStore,
    ConfigWarning,
    ValueOrigin,
    document_warnings,
)
from kiro_crew.apps.builtins.spec_engine.engine.delivery import (
    REASON_LADDER,
    REASON_NO_TARGET,
    REASON_POSTURE,
    REASON_VERIFY,
    DeliveryWorkflow,
    ProtectedBranches,
    evaluate_integration,
    resolve_authority,
    resolve_protected_branches,
)

PROJECT = "acme"
SOURCE = "tracker"
BASE = "main"

#: A workflow with every stage the flow uses, so a test that varies one gate is
#: not also varying whether the machinery exists.
FULL_WORKFLOW: dict[str, Any] = {
    "stages": {
        "isolate": [["make-worktree", "{branch_name}"]],
        "submit": [["raise-review", "{review_title}"]],
        "verify": [["run-checks"]],
        "publish": [["deploy"]],
    }
}


@pytest.fixture()
def store(tmp_path: Any) -> ConfigStore:
    return ConfigStore(tmp_path / "state")


def configure(store: ConfigStore, document: dict[str, Any]) -> None:
    store.write(document, surface=DASHBOARD_SURFACE)


def project_document(
    *,
    workflow: dict[str, Any] | None = None,
    auto_integrate: bool | None = None,
    protected: list[str] | None = None,
    base_branch: str | None = BASE,
) -> dict[str, Any]:
    entry: dict[str, Any] = {"path": "/tmp/acme"}
    if workflow is not None:
        entry["workflow"] = workflow
    if auto_integrate is not None:
        entry["delivery"] = {"auto_integrate": auto_integrate}
    if protected is not None:
        entry["protected_branches"] = protected
    if base_branch is not None:
        entry["base_branch"] = base_branch
    return {"projects": {PROJECT: entry}}


def decision_at(level: AutonomyLevel, *, configured: bool = True) -> AutonomyDecision:
    return AutonomyDecision(
        level=level,
        source=SOURCE,
        spec_type="feature",
        submitter_class="maintainer",
        declared_at=f"sources.{SOURCE}.{AUTONOMY_FIELD}.maintainer.feature" if configured else "",
    )


class TestProtectedBranches:
    def test_the_base_branch_is_protected_when_no_set_is_configured(self) -> None:
        protected = resolve_protected_branches({}, project=PROJECT, base_branch=BASE)

        assert protected.protects(BASE)
        assert protected.from_base_branch
        assert protected.origin is ValueOrigin.BUNDLED_DEFAULT

    def test_a_configured_set_replaces_the_base_branch_fallback(self) -> None:
        document = project_document(protected=["main", "release/1.x"])

        protected = resolve_protected_branches(document, project=PROJECT, base_branch="develop")

        assert protected.protects("main")
        assert protected.protects("release/1.x")
        assert not protected.protects("develop")
        assert not protected.from_base_branch
        assert protected.declared_at == f"projects.{PROJECT}.protected_branches"

    def test_a_publish_target_outside_the_set_is_not_an_integration(self) -> None:
        # A development branch feeding a test pipeline is an ordinary publish
        # destination; treating every push as an integration would make the safe
        # case impossible to configure.
        protected = resolve_protected_branches(
            project_document(protected=["main"]), project=PROJECT, base_branch=BASE
        )

        assert not protected.protects("development")
        assert not protected.protects("env/test")

    def test_an_unnamed_target_is_treated_as_protected(self) -> None:
        protected = resolve_protected_branches({}, project=PROJECT, base_branch=BASE)

        assert protected.protects("")
        assert protected.protects("   ")

    def test_the_base_branch_falls_back_to_the_project_declaration(self) -> None:
        # A run that did not carry a base branch still resolves the project's.
        protected = resolve_protected_branches(project_document(), project=PROJECT)

        assert protected.protects(BASE)

    def test_blank_entries_do_not_create_a_protected_empty_name(self) -> None:
        document = project_document(protected=["  ", "main"])

        protected = resolve_protected_branches(document, project=PROJECT, base_branch="")

        assert protected.branches == frozenset({"main"})


class TestIntegrationGates:
    def _decide(
        self,
        *,
        level: AutonomyLevel,
        auto_integrate: bool,
        verified: bool = True,
        target: str = BASE,
    ) -> Any:
        return evaluate_integration(
            decision=decision_at(level),
            auto_integrate=auto_integrate,
            verified=verified,
            protected=ProtectedBranches(
                branches=frozenset({BASE}), origin=ValueOrigin.BUNDLED_DEFAULT
            ),
            target=target,
        )

    def test_both_gates_are_required(self) -> None:
        assert self._decide(level=AutonomyLevel.INTEGRATION, auto_integrate=True).permitted

    def test_the_ladder_alone_does_not_authorize_integration(self) -> None:
        decision = self._decide(level=AutonomyLevel.INTEGRATION, auto_integrate=False)

        assert not decision.permitted
        assert decision.requires_human_action
        assert decision.reasons == (REASON_POSTURE,)
        assert decision.ladder_permits

    def test_the_posture_switch_alone_does_not_authorize_integration(self) -> None:
        decision = self._decide(level=AutonomyLevel.DELIVERY, auto_integrate=True)

        assert not decision.permitted
        assert decision.requires_human_action
        assert decision.reasons == (REASON_LADDER,)
        assert decision.auto_integrate

    def test_a_refusal_names_every_gate_that_was_shut(self) -> None:
        decision = self._decide(
            level=AutonomyLevel.EXECUTION, auto_integrate=False, verified=False, target=""
        )

        assert decision.reasons == (REASON_LADDER, REASON_POSTURE, REASON_VERIFY, REASON_NO_TARGET)

    def test_an_unverified_change_is_never_integrated(self) -> None:
        decision = self._decide(
            level=AutonomyLevel.INTEGRATION, auto_integrate=True, verified=False
        )

        assert not decision.permitted
        assert REASON_VERIFY in decision.reasons

    def test_a_lower_rung_never_reaches_integration(self) -> None:
        for level in (AutonomyLevel.AUTHORING, AutonomyLevel.EXECUTION, AutonomyLevel.DELIVERY):
            decision = self._decide(level=level, auto_integrate=True)
            assert not decision.permitted
            assert REASON_LADDER in decision.reasons


class TestResolvedAuthority:
    def test_a_zero_config_project_is_capped_at_execution_and_never_integrates(
        self, store: ConfigStore
    ) -> None:
        # Nothing configured at all, and a policy grid that nevertheless names
        # integration: the ceiling has to hold, because the project described no
        # way to isolate a workspace, raise a review, or verify anything.
        authority = resolve_authority(
            store, decision=decision_at(AutonomyLevel.INTEGRATION), project=PROJECT
        )

        assert authority.level is AutonomyLevel.EXECUTION
        assert authority.capped
        assert not authority.workflow_configured
        assert authority.permits(AutonomyLevel.EXECUTION)
        assert not authority.permits(AutonomyLevel.DELIVERY)
        assert not authority.isolates_before_execution
        assert not authority.integration(verified=True, target=BASE).permitted

    def test_a_zero_config_project_cannot_integrate_even_with_the_switch_on(
        self, store: ConfigStore
    ) -> None:
        configure(store, {"delivery": {"auto_integrate": True}})

        authority = resolve_authority(
            store, decision=decision_at(AutonomyLevel.INTEGRATION), project=PROJECT
        )
        decision = authority.integration(verified=True, target=BASE)

        assert authority.auto_integrate
        assert not decision.permitted
        assert decision.reasons == (REASON_LADDER,)

    def test_a_configured_workflow_leaves_the_resolved_level_alone(
        self, store: ConfigStore
    ) -> None:
        configure(store, project_document(workflow=FULL_WORKFLOW, auto_integrate=True))

        authority = resolve_authority(
            store, decision=decision_at(AutonomyLevel.INTEGRATION), project=PROJECT
        )

        assert authority.level is AutonomyLevel.INTEGRATION
        assert not authority.capped
        assert authority.isolates_before_execution
        assert authority.integration(verified=True, target=BASE).permitted

    def test_capping_never_raises_a_resolved_level(self, store: ConfigStore) -> None:
        configure(store, project_document(workflow=FULL_WORKFLOW))

        for level in AutonomyLevel:
            authority = resolve_authority(store, decision=decision_at(level), project=PROJECT)
            assert authority.level.rank <= level.rank

    def test_the_posture_switch_reports_where_it_was_declared(self, store: ConfigStore) -> None:
        configure(store, project_document(workflow=FULL_WORKFLOW, auto_integrate=True))

        authority = resolve_authority(
            store, decision=decision_at(AutonomyLevel.INTEGRATION), project=PROJECT
        )

        assert authority.auto_integrate_declared_at == (
            f"projects.{PROJECT}.{AUTO_INTEGRATE_SETTING}"
        )

    def test_an_unconfigured_policy_stays_at_authoring(self, store: ConfigStore) -> None:
        configure(store, project_document(workflow=FULL_WORKFLOW, auto_integrate=True))
        policy = AutonomyPolicy.from_document(store.document())
        resolved = policy.resolve(source=SOURCE, spec_type="feature", submitter_class=None)

        authority = resolve_authority(store, decision=resolved, project=PROJECT)

        assert authority.level is AutonomyLevel.AUTHORING
        assert not authority.isolates_before_execution
        assert not authority.integration(verified=True, target=BASE).permitted


class TestConfigTimeWarning:
    def test_auto_integration_without_a_verify_stage_warns(self, store: ConfigStore) -> None:
        document = project_document(
            workflow={"stages": {"submit": [["raise-review"]], "publish": [["deploy"]]}},
            auto_integrate=True,
        )

        warnings = document_warnings(document)

        assert [warning.code for warning in warnings] == [AUTO_INTEGRATE_WITHOUT_VERIFY]
        assert warnings[0].path == f"projects.{PROJECT}.{AUTO_INTEGRATE_SETTING}"
        assert warnings[0].project == PROJECT
        assert "verify" in warnings[0].message

    def test_a_configured_verify_stage_earns_no_warning(self, store: ConfigStore) -> None:
        document = project_document(workflow=FULL_WORKFLOW, auto_integrate=True)

        assert document_warnings(document) == ()

    def test_the_switch_off_earns_no_warning(self, store: ConfigStore) -> None:
        document = project_document(
            workflow={"stages": {"publish": [["deploy"]]}}, auto_integrate=False
        )

        assert document_warnings(document) == ()

    def test_an_app_wide_switch_warns_once_per_project(self) -> None:
        document: dict[str, Any] = {
            "delivery": {"auto_integrate": True},
            "projects": {
                "verified": {
                    "path": "/tmp/verified",
                    "workflow": {"stages": {"verify": [["run-checks"]]}},
                },
                "unverified": {"path": "/tmp/unverified"},
            },
        }

        warnings = document_warnings(document)

        assert [warning.project for warning in warnings] == ["unverified"]
        assert warnings[0].path == AUTO_INTEGRATE_SETTING

    def test_an_app_wide_verify_stage_covers_a_project_that_declares_none(self) -> None:
        document: dict[str, Any] = {
            "delivery": {"auto_integrate": True},
            "workflow": {"stages": {"verify": [["run-checks"]]}},
            "projects": {"acme": {"path": "/tmp/acme"}},
        }

        assert document_warnings(document) == ()

    def test_the_warning_reaches_the_surface_that_wrote_the_configuration(
        self, store: ConfigStore
    ) -> None:
        # The write path is the moment a human is present and looking at this
        # switch; the run that acts on it happens hours later with nobody there.
        seen: list[ConfigWarning] = []

        store.write(
            project_document(workflow={"stages": {"publish": [["deploy"]]}}, auto_integrate=True),
            surface=DASHBOARD_SURFACE,
            warn=seen.append,
        )

        assert [warning.code for warning in seen] == [AUTO_INTEGRATE_WITHOUT_VERIFY]
        assert store.advisories() == tuple(seen)

    def test_a_warning_carries_audit_detail_with_its_identifier(self, store: ConfigStore) -> None:
        configure(
            store,
            project_document(workflow={"stages": {"publish": [["deploy"]]}}, auto_integrate=True),
        )

        recorded: list[dict[str, Any]] = []
        store.advisories(recorder=lambda warning: recorded.append(warning.detail))

        assert recorded[0]["code"] == AUTO_INTEGRATE_WITHOUT_VERIFY
        assert recorded[0]["project"] == PROJECT
        assert recorded[0]["path"] == f"projects.{PROJECT}.{AUTO_INTEGRATE_SETTING}"

    def test_a_valid_document_with_no_advisory_reports_nothing(self, store: ConfigStore) -> None:
        configure(store, project_document(workflow=FULL_WORKFLOW))

        assert store.advisories() == ()
        assert store.validate() == ()

    @pytest.mark.parametrize(
        "workflow",
        [
            None,
            {"stages": {"verify": [["run-checks"]]}},
            {"stages": {"publish": [["deploy"]]}},
            FULL_WORKFLOW,
        ],
    )
    def test_the_advisory_agrees_with_the_workflow_resolver_on_verify_presence(
        self, store: ConfigStore, workflow: dict[str, Any] | None
    ) -> None:
        # The advisory asks its own narrow question about the verify stage while
        # the workflow resolver owns stage semantics. Pinning the two together is
        # what keeps that duplication from drifting into disagreement.
        document = project_document(workflow=workflow, auto_integrate=True)
        configure(store, document)

        resolver_sees_verify = (
            DeliveryWorkflow(document, project=PROJECT).stage("verify") is not None
        )
        advisory_raised = bool(document_warnings(document))

        assert resolver_sees_verify is not advisory_raised
