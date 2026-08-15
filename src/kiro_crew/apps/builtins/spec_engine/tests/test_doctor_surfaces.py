"""The Doctor's two surfaces, and the seams that had no callers.

What these tests are built to catch, in the order the defects actually appeared:

* **A second aggregation.** "Every surface returns identical Findings" is only
  worth asserting if a second assembly *could* exist. So one test scans the app's
  own source and fails when anything but :mod:`~.engine.diagnosis` constructs a
  :class:`~.engine.doctor.Doctor`. That is the property behind the requirement; a
  panel and a tool that each built their own would choose their own collaborators
  and diverge on a host neither author has.
* **A comparison of a function with itself.** The equivalence test drives the
  packaged server as a **child process over stdio** and compares what it returns
  with an in-process call to the app-side panel surface, both pinned to one data
  home. Two processes, two envelopes, one set of Findings.
* **A vacuous version check.** ``minimum_versions`` used to be an empty mapping
  nothing populated, so the check reported no findings while verifying nothing.
  These drive a genuinely too-old version in through *configuration* and require
  the blocking finding, so deleting the populator fails a test rather than
  quietly restoring the vacuum.
* **A recorded state nothing reads.** The readiness state is written by the app's
  startup hook and was read by nothing, which made a half-registered app
  indistinguishable from a whole one on every surface. These assert the Doctor
  reads it, that the directory it reads is the one the host writes, and that
  absence and corruption both read as not-ready.
"""

from __future__ import annotations

import ast
import inspect
import json
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from kiro_crew.apps.builtins.spec_engine import diagnostics, readiness
from kiro_crew.apps.builtins.spec_engine.engine.autonomy import AutonomyLevel
from kiro_crew.apps.builtins.spec_engine.engine.budget.ceiling import (
    DispatchDecision,
    DispatchOutcome,
)
from kiro_crew.apps.builtins.spec_engine.engine.budget.ledger import RunSpend
from kiro_crew.apps.builtins.spec_engine.engine.config import ConfigStore
from kiro_crew.apps.builtins.spec_engine.engine.config.store import DASHBOARD_SURFACE
from kiro_crew.apps.builtins.spec_engine.engine.diagnosis import (
    diagnose,
    dispatch_block_report,
    refusal_report,
    run_gate_report,
)
from kiro_crew.apps.builtins.spec_engine.engine.doctor import (
    FINDING_PROGRAM_VERSION,
    FINDING_REGISTRATION_INCOMPLETE,
    FINDING_WATCH_CANNOT_CANCEL,
    SURFACE_AGENT,
    Doctor,
    prerequisite_finding_id,
    scoped_finding_id,
)
from kiro_crew.apps.builtins.spec_engine.engine.prerequisites import (
    CheckName,
    Prerequisite,
    RunRefusal,
    check_project,
    declared_minimum_versions,
)
from kiro_crew.apps.builtins.spec_engine.engine.state import APP_NAME
from kiro_crew.apps.builtins.spec_engine.engine.watch.lifecycle import (
    poll_reports_closed_items,
)
from kiro_crew.apps.manager import app_data_dir

from .test_engine_mcp_conformance import stdio_server

#: The one module allowed to assemble a Doctor.
_ASSEMBLY_MODULE = "engine/diagnosis.py"


@pytest.fixture()
def config(tmp_path: Path) -> ConfigStore:
    return ConfigStore(root=tmp_path / "config")


def configure(store: ConfigStore, patch: dict[str, Any]) -> None:
    store.write(patch, surface=DASHBOARD_SURFACE)


def resolver(*available: str):
    def which(program: str) -> str | None:
        return f"/usr/bin/{program}" if program in available else None

    return which


def ready() -> readiness.Readiness:
    return readiness.Readiness(ready=True, checked_at="2026-01-01T00:00:00Z")


def not_ready(*reasons: str) -> readiness.Readiness:
    return readiness.Readiness(ready=False, reasons=reasons, checked_at="2026-01-01T00:00:00Z")


def app_root() -> Path:
    return Path(diagnostics.__file__).resolve().parent


def _spend() -> RunSpend:
    """A zero-consumption spend record, so a decision can be built to report on."""
    return RunSpend(run_id="run-1", metered_credits=0.0, declared_credits=0.0, turns=0)


# --- one assembly, two surfaces ---------------------------------------------


class TestThereIsOneAggregation:
    def test_only_the_diagnosis_module_assembles_a_doctor(self) -> None:
        # The property behind "identical Findings from every surface". A second
        # construction site is the defect itself, and it is invisible until two
        # panels disagree on a host neither author has.
        offenders: list[str] = []
        for path in sorted(app_root().rglob("*.py")):
            relative = path.relative_to(app_root()).as_posix()
            if relative.startswith("tests/") or relative == "engine/doctor.py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "Doctor"
                ):
                    offenders.append(f"{relative}:{node.lineno}")
        assert all(
            offender.startswith(_ASSEMBLY_MODULE) for offender in offenders
        ), f"a second surface assembles its own Doctor: {offenders}"
        assert offenders, "the scan found no assembly at all, so it proves nothing"

    def test_the_scanner_detects_a_planted_second_assembly(self, tmp_path: Path) -> None:
        # The scan above would pass over an app that constructs nothing, so the
        # detector is driven against source that does construct one.
        planted = tmp_path / "planted.py"
        planted.write_text("report = Doctor(config=None).run()\n", encoding="utf-8")
        tree = ast.parse(planted.read_text(encoding="utf-8"))
        found = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "Doctor"
        ]
        assert len(found) == 1


class TestTheToolAndThePanelAgree:
    def test_both_surfaces_report_identical_findings(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "home"
        home.mkdir()
        data_dir = home / "apps" / APP_NAME / "data"
        data_dir.mkdir(parents=True)
        # A recorded not-ready state, so the two surfaces have something to agree
        # about beyond an empty list.
        readiness.record(not_ready("the engine MCP server did not register"), data_dir)

        # The tool path: the packaged server as a child process, driven through the
        # client init sequence and `tools/call`, with its home pinned.
        with stdio_server(home) as running:
            advertised = running.initialize()
            assert "run_doctor" in advertised
            through_the_tool = running.tool_payload("run_doctor", {})

        # The UI path: the app-side panel surface, in this process.
        monkeypatch.setenv("KIROCREW_HOME", str(home))
        through_the_panel = diagnostics.doctor_payload()

        assert through_the_tool["findings"] == through_the_panel["findings"]
        assert through_the_tool["ok"] == through_the_panel["ok"]
        # Not two empty lists agreeing: the registration finding is present on both.
        identifiers = {finding["id"] for finding in through_the_tool["findings"]}
        assert FINDING_REGISTRATION_INCOMPLETE in identifiers

    def test_the_tool_is_reached_through_the_dispatch_table(self) -> None:
        from kiro_crew.apps.builtins.spec_engine.engine_mcp.server import TOOLS, handle

        reply = handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "run_doctor", "arguments": {}},
            }
        )
        assert reply is not None and "error" not in reply
        # Declared, not just handled: a tool a client cannot see is a tool nobody
        # calls.
        assert TOOLS["run_doctor"].needs_ops is False


# --- the readiness state finally has a reader -------------------------------


class TestRegistrationReachIsReported:
    def test_a_not_ready_state_is_a_blocking_finding(self, config: ConfigStore) -> None:
        report = diagnose(config, registration=not_ready("the discovery skill did not register"))

        found = report.for_identifier(FINDING_REGISTRATION_INCOMPLETE)
        assert found and found[0].blocking
        assert found[0].surface == SURFACE_AGENT
        assert "discovery skill" in found[0].cause.for_display()

    def test_a_ready_state_records_a_pass_rather_than_silence(self, config: ConfigStore) -> None:
        report = diagnose(config, registration=ready())

        # A pass is recorded, not merely an absent finding: without it the history
        # cannot tell a registration that broke from one never assessed.
        assert FINDING_REGISTRATION_INCOMPLETE in report.passing
        assert not report.for_identifier(FINDING_REGISTRATION_INCOMPLETE)

    def test_an_unassessed_state_is_not_operational(self, config: ConfigStore) -> None:
        report = diagnose(config, registration=readiness.not_assessed())

        found = report.for_identifier(FINDING_REGISTRATION_INCOMPLETE)
        assert found and found[0].blocking

    def test_a_corrupt_recorded_state_reads_as_not_ready(self, tmp_path: Path) -> None:
        readiness.status_path(tmp_path).write_text('{"ready": true, "reas', encoding="utf-8")

        report = diagnostics.doctor_report(data_dir=tmp_path, config=ConfigStore(tmp_path / "c"))

        assert report.for_identifier(FINDING_REGISTRATION_INCOMPLETE)

    def test_the_panel_reads_the_directory_the_host_writes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The reader and the writer have to name one directory. A reader looking
        # somewhere nothing writes reports "not assessed" forever, which is a
        # different lie from the one it was built to stop.
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        assert diagnostics.default_data_dir() == app_data_dir(APP_NAME)

    def test_the_one_operation_cannot_be_called_without_a_registration_state(self) -> None:
        # Required rather than defaulted: a surface that could omit it would show
        # an app as healthy without having asked whether its tools ever arrived.
        parameter = inspect.signature(diagnose).parameters["registration"]
        assert parameter.default is inspect.Parameter.empty
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY

    @settings(max_examples=30, deadline=None)
    @given(st.lists(st.text(min_size=1, max_size=40), min_size=1, max_size=4))
    def test_a_not_ready_state_is_blocking_whatever_it_says(self, reasons: list[str]) -> None:
        # Fail-closed regardless of the reason text, including text the display
        # contract strips to nothing: the finding's presence and severity must not
        # depend on what the reason happens to contain.
        store = ConfigStore(root=Path("/nonexistent-doctor-config-root"))
        report = diagnose(store, registration=not_ready(*reasons))

        found = report.for_identifier(FINDING_REGISTRATION_INCOMPLETE)
        assert len(found) == 1
        assert found[0].blocking
        assert FINDING_REGISTRATION_INCOMPLETE not in report.passing

    @settings(max_examples=30, deadline=None)
    @given(
        st.lists(
            st.text(alphabet=st.characters(min_codepoint=32, max_codepoint=126), min_size=3),
            min_size=1,
            max_size=4,
        )
    )
    def test_every_stated_reason_reaches_the_finding(self, reasons: list[str]) -> None:
        store = ConfigStore(root=Path("/nonexistent-doctor-config-root"))
        report = diagnose(store, registration=not_ready(*reasons))

        found = report.for_identifier(FINDING_REGISTRATION_INCOMPLETE)
        assert found, "a not-ready app reported no registration finding"
        shown = found[0].cause.for_display()
        for reason in reasons:
            if reason.strip():
                # Truncation is the only thing allowed to drop a reason, and the
                # display rendering says so when it does.
                assert reason.strip()[:20] in shown or "truncat" in shown.lower()


# --- the version check is no longer vacuous ---------------------------------


class TestDeclaredMinimumsComeFromConfiguration:
    def test_a_configured_minimum_is_read(self, config: ConfigStore) -> None:
        configure(config, {"programs": {"gh": {"min_version": "2.40.0"}}})

        assert declared_minimum_versions(config) == {"gh": "2.40.0"}

    def test_no_declaration_reads_as_no_minimum(self, config: ConfigStore) -> None:
        assert declared_minimum_versions(config) == {}

    def test_a_too_old_program_is_blocking_through_the_one_operation(
        self, config: ConfigStore
    ) -> None:
        # The test the populator exists for. It goes in through configuration, so
        # removing the populator fails here instead of silently restoring a check
        # that reports nothing while verifying nothing.
        configure(config, {"programs": {"gh": {"min_version": "2.40.0"}}})

        report = diagnose(
            config,
            registration=ready(),
            which=resolver("gh"),
            version_of=lambda path: "gh version 2.4.0 (2023-01-01)",
        )

        found = report.for_identifier(scoped_finding_id(FINDING_PROGRAM_VERSION, "gh"))
        assert found and found[0].blocking
        assert "2.40.0" in found[0].cause.for_display()

    def test_a_new_enough_program_passes_that_same_identifier(self, config: ConfigStore) -> None:
        configure(config, {"programs": {"gh": {"min_version": "2.40.0"}}})

        report = diagnose(
            config,
            registration=ready(),
            which=resolver("gh"),
            version_of=lambda path: "gh version 2.41.0",
        )

        assert scoped_finding_id(FINDING_PROGRAM_VERSION, "gh") in report.passing

    def test_a_minimum_that_is_not_a_version_is_a_configuration_error(
        self, config: ConfigStore
    ) -> None:
        # Rejected at the document rather than ignored downstream: a minimum that
        # cannot be parsed would otherwise appear to be in force while comparing
        # nothing.
        with pytest.raises(Exception):
            configure(config, {"programs": {"gh": {"min_version": ">= 2.40"}}})

    def test_a_program_minimum_is_not_writable_from_a_tool(self, config: ConfigStore) -> None:
        from kiro_crew.apps.builtins.spec_engine.engine_mcp.operations import (
            ENGINE_MCP_SURFACE,
        )

        # Lowering a minimum turns an ERROR finding into a pass, which is the same
        # shape as a blocking quality gate rewritten advisory.
        with pytest.raises(Exception):
            config.write({"programs": {"gh": {"min_version": "0.1"}}}, surface=ENGINE_MCP_SURFACE)


# --- the refusal translators have callers ----------------------------------


class TestARefusalAndAPanelQuoteOneIdentifier:
    def test_the_run_gate_report_quotes_the_doctors_identifiers(self, config: ConfigStore) -> None:
        configure(config, {"projects": {"proj": {"path": "/tmp/proj", "base_branch": "main"}}})

        answer = run_gate_report(
            config,
            AutonomyLevel.DELIVERY,
            project="proj",
            which=resolver(),
            branch_exists=lambda ref: False,
        )

        assert answer["may_start"] is False
        assert answer["finding_ids"], "a refusal quoted no finding identifier"
        # The same identifiers the Doctor reports for the same unmet checks, taken
        # from the same evaluation rather than from a second list beside it.
        report = check_project(
            config, project="proj", which=resolver(), branch_exists=lambda ref: False
        )
        expected = {
            prerequisite_finding_id(check.check, source=check.source)
            for check in report.unmet_through(AutonomyLevel.DELIVERY)
        }
        assert expected, "nothing was unmet, so this compares two empty sets"
        assert set(answer["finding_ids"]) == expected

    def test_a_met_gate_quotes_nothing(self, config: ConfigStore) -> None:
        answer = run_gate_report(
            config, AutonomyLevel.AUTHORING, which=resolver(), branch_exists=lambda ref: True
        )

        assert answer["may_start"] is True
        assert answer["finding_ids"] == []

    def test_a_refusal_report_carries_the_unmet_checks_and_the_identifiers(self) -> None:
        refusal = RunRefusal(
            level=AutonomyLevel.DELIVERY,
            unmet=(
                Prerequisite(
                    check=CheckName.PROGRAMS,
                    phase=AutonomyLevel.EXECUTION,
                    met=False,
                    missing="program 'gh' is not on PATH",
                    action="install 'gh'",
                ),
            ),
        )

        payload = refusal_report(refusal)

        assert payload["finding_ids"] == [prerequisite_finding_id(CheckName.PROGRAMS)]
        assert payload["unmet"], "a refusal reported no unmet prerequisite"

    @pytest.mark.parametrize(
        "outcome",
        [DispatchOutcome.HALTED, DispatchOutcome.UNBOUNDED, DispatchOutcome.STOPPED],
    )
    def test_a_blocked_dispatch_quotes_a_finding_identifier(self, outcome: DispatchOutcome) -> None:
        payload = dispatch_block_report(
            DispatchDecision(
                outcome=outcome, spend=_spend(), ceiling_credits=1.0, message="halted"
            )
        )

        assert payload["finding_id"], f"{outcome.value} quoted no identifier"
        assert payload["allowed"] is False

    def test_an_allowed_dispatch_quotes_nothing(self) -> None:
        payload = dispatch_block_report(
            DispatchDecision(
                outcome=DispatchOutcome.ALLOWED, spend=_spend(), ceiling_credits=1.0, message=""
            )
        )

        # Absent rather than empty-string-as-a-reason: a caller must not be able to
        # quote "nothing" as why it stopped.
        assert payload["finding_id"] == ""
        assert payload["allowed"] is True


# --- the grouping is the gate's grouping ------------------------------------


class TestThePhaseGroupingIsNotRegrouped:
    def test_every_prerequisite_finding_carries_the_phase_by_phase_put_it_in(
        self, config: ConfigStore
    ) -> None:
        configure(config, {"projects": {"proj": {"path": "/tmp/proj"}}})

        report = diagnose(
            config,
            registration=ready(),
            project="proj",
            which=resolver(),
            branch_exists=lambda ref: False,
        )
        grouped = check_project(
            config, project="proj", which=resolver(), branch_exists=lambda ref: False
        ).by_phase()

        expected: dict[str, str] = {}
        for phase, checks in grouped.items():
            for check in checks:
                if not check.met:
                    expected[prerequisite_finding_id(check.check, source=check.source)] = (
                        phase.value
                    )
        assert expected, "no unmet prerequisite to group, so this proves nothing"
        for finding in report.findings:
            if finding.identifier in expected:
                # A second grouping would show a phase ready while the gate refuses
                # a run for it, and the divergence is invisible until a user hits it.
                assert finding.surface == expected[finding.identifier]


# --- a source that can never derive a cancellation --------------------------


class TestAWatchSourceThatCannotCancel:
    def test_an_open_items_only_poll_is_advised(self, config: ConfigStore) -> None:
        configure(config, {"sources": {"upstream": {"poll": ["gh", "issue", "list"]}}})

        report = diagnose(config, registration=ready(), which=resolver("gh"))

        found = report.for_identifier(scoped_finding_id(FINDING_WATCH_CANNOT_CANCEL, "upstream"))
        assert found, "a poll that cannot report a closure was not advised about"
        # Advisory: the evidence is the argv an operator wrote, not an answer from
        # the tracker, so a false read must not stop a run.
        assert found[0].blocking is False
        assert found[0].declared_at == "sources.upstream.poll"

    def test_a_widened_poll_passes(self, config: ConfigStore) -> None:
        configure(
            config,
            {"sources": {"upstream": {"poll": ["gh", "issue", "list", "--state", "all"]}}},
        )

        report = diagnose(config, registration=ready(), which=resolver("gh"))

        identifier = scoped_finding_id(FINDING_WATCH_CANNOT_CANCEL, "upstream")
        assert identifier in report.passing
        assert not report.for_identifier(identifier)

    @pytest.mark.parametrize(
        "argv,widened",
        [
            (["gh", "issue", "list", "--state", "all"], True),
            (["gh", "issue", "list", "--state=all"], True),
            (["gh", "issue", "list", "--json", "state,number"], False),
            (["gh", "issue", "list", "--state", "closed"], True),
            (["glab", "issue", "list", "--output", "json"], False),
            (["gh", "issue", "list", "--STATE=ALL"], True),
            (["tracker", "list", "--include-closed"], True),
            (["gh", "issue", "list", "--state", "open"], False),
            (["gh", "issue", "list", "--state", "all", "--limit", "50"], True),
        ],
    )
    def test_the_predicate_reads_a_state_filter_wherever_it_is_written(
        self, argv: list[str], widened: bool
    ) -> None:
        assert poll_reports_closed_items(argv) is widened

    def test_the_advisory_reads_the_bundled_presets_the_way_the_asymmetry_says(self) -> None:
        """The bundled presets, judged by the predicate rather than by hand.

        ``test_only_github_can_derive_a_cancellation_and_that_asymmetry_is_deliberate``
        pins the same gap from the presets' side. This pins it from the advisory's:
        the GitHub preset asks for ``state=all`` and reads widened, the GitLab one
        does not and reads narrow, so an operator who copies it is told. Both fail
        the day the GitLab argv gains a state filter -- at which point the flag has
        been verified against a real ``glab`` and both claims are what to correct.
        """
        from kiro_crew.apps.builtins.spec_engine.engine.watch.sources import (
            WATCH_SOURCE_PRESETS,
        )

        assert poll_reports_closed_items(list(WATCH_SOURCE_PRESETS["github"]["poll"])) is True
        assert poll_reports_closed_items(list(WATCH_SOURCE_PRESETS["gitlab"]["poll"])) is False

    def test_the_advisory_does_not_widen_anything(self, config: ConfigStore) -> None:
        # It never guesses a flag: a plausible flag the real CLI rejects fails every
        # poll rather than degrading one, which is a worse defect than the one being
        # advised about.
        poll = ["glab", "issue", "list", "--output", "json"]
        configure(config, {"sources": {"upstream": {"poll": poll}}})

        diagnose(config, registration=ready(), which=resolver("glab"))

        stored = config.document()["sources"]["upstream"]["poll"]
        assert list(stored) == poll


# --- the guard the doctor's history rests on --------------------------------


class TestTheDoctorStillFailsClosedOnItsOwnChecks:
    def test_a_check_that_raises_becomes_a_finding_and_the_rest_still_report(
        self, config: ConfigStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def explode(self: Doctor) -> Any:
            raise RuntimeError("the registration check blew up")

        monkeypatch.setattr(Doctor, "_registration", explode)

        report = diagnose(config, registration=ready())

        assert any(
            finding.identifier.startswith("doctor.check_failed.") for finding in report.findings
        )
        # The remaining checks still reported: an aggregation that aborted on the
        # first exception is unavailable on exactly the broken host it is for.
        assert report.passing


def test_the_panel_serializes_exactly_what_the_report_says(tmp_path: Path) -> None:
    store = ConfigStore(tmp_path / "config")
    report = diagnostics.doctor_report(data_dir=tmp_path, config=store)
    payload = diagnostics.doctor_payload(data_dir=tmp_path, config=store)

    # One serialization, so a panel and a tool cannot render one report into two
    # shapes and disagree about what the host said.
    assert json.loads(json.dumps(payload)) == report.to_json_object()
