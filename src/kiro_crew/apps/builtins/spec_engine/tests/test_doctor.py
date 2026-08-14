"""The Doctor: one aggregation, one vocabulary, and the things it must not do.

Four claims here carry the requirement rather than merely exercising the code.

**A check that cannot complete does not take the others with it.** The test for
that does not assert a non-empty report -- an aggregation that swallowed seven
checks and returned one would pass that. It asserts that a *specific other*
check's Finding is still present, that the report is otherwise identical to the
one the same state produces with nothing broken, and that the broken check
appears as a Finding naming itself.

**Untrusted text is data.** A hostile watch source name and a hostile version
probe output travel through a real aggregation, and the test asserts both that
nothing executed (no marker files) and that the Finding's prose is not a ``str``
that could reach an f-string by accident.

**Read-only is asserted by bytes, not by inspection.** The configuration document
is captured before and after a full aggregation over a state where every check
fails, and compared byte for byte -- and the store the doctor is handed refuses a
write outright, so a write attempt fails the test rather than being detected
afterwards.

**One resolution for source health.** ``check_source`` and a real poll of the same
absent program are both run, and the test pins that they name the same program and
fold to the same Finding identifier. That is the assertion that fails if either
side is refactored apart from the other.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.apps.builtins.spec_engine.engine import doctor as doctor_module
from kiro_crew.apps.builtins.spec_engine.engine import findings as findings_module
from kiro_crew.apps.builtins.spec_engine.engine.autonomy import AutonomyLevel
from kiro_crew.apps.builtins.spec_engine.engine.budget.switch import KillSwitch
from kiro_crew.apps.builtins.spec_engine.engine.capabilities.contracts import (
    FINDING_PROVIDER_TIMEOUT,
    Degradation,
    Untrusted,
)
from kiro_crew.apps.builtins.spec_engine.engine.config import (
    DASHBOARD_SURFACE,
    ConfigStore,
)
from kiro_crew.apps.builtins.spec_engine.engine.config.advisories import (
    AGENT_NOT_INSTALLED,
)
from kiro_crew.apps.builtins.spec_engine.engine.config.agent_surface import (
    AgentToolSurface,
)
from kiro_crew.apps.builtins.spec_engine.engine.doctor import (
    CHECK_BUDGET,
    CHECK_CONFIGURATION,
    CHECK_PREREQUISITES,
    CHECK_PROGRAM_VERSIONS,
    CHECK_REVIEW_QUEUE,
    CHECK_SOURCE_HEALTH,
    FINDING_CONFIG_INVALID,
    FINDING_HISTORY_UNWRITABLE,
    FINDING_KILL_SWITCH_ENGAGED,
    FINDING_KILL_SWITCH_UNREADABLE,
    FINDING_PROGRAM_VERSION,
    HEALTH_REASON_FINDINGS,
    SURFACE_BUDGET,
    SURFACE_DOCTOR,
    SURFACE_REVIEW_QUEUE,
    CheckOutcome,
    Doctor,
    DoctorCheck,
    DoctorHistory,
    Finding,
    check_failed_finding_id,
    health_finding_id,
    parse_version,
    prerequisite_finding_id,
    runs_waiting_finding_id,
    scoped_finding_id,
    version_satisfies,
)
from kiro_crew.apps.builtins.spec_engine.engine.findings import Severity
from kiro_crew.apps.builtins.spec_engine.engine.prerequisites import (
    CheckName,
    check_source,
)
from kiro_crew.apps.builtins.spec_engine.engine.review_queue import (
    QueueEntry,
    QueueSnapshot,
    WaitingOn,
)
from kiro_crew.apps.builtins.spec_engine.engine.runs import RunState
from kiro_crew.apps.builtins.spec_engine.engine.state import StatePersistenceError
from kiro_crew.apps.builtins.spec_engine.engine.watch import (
    HealthReason,
    PollStatus,
    poll_source,
)

#: A program name no host has installed, so an absence test fails for the absence
#: rather than for a host that happens to have the tool.
ABSENT_PROGRAM = "kirocrew-nonexistent-doctor-cli"

#: Shell constructs that would matter if anything interpreted a source name or a
#: version probe's output. A source name is operator-authored; a probe's output is
#: whatever the program printed.
HOSTILE_TEXT = "boom; touch pwned && touch pwned2 | tee pwned3 `touch pwned4` $(touch pwned5)"

PAYLOAD_ARTEFACTS = ("pwned", "pwned2", "pwned3", "pwned4", "pwned5")


@pytest.fixture()
def config(tmp_path: Path) -> ConfigStore:
    return ConfigStore(root=tmp_path / "config")


@pytest.fixture()
def history(tmp_path: Path) -> DoctorHistory:
    return DoctorHistory(tmp_path / "state")


def configure(config: ConfigStore, patch: dict[str, Any]) -> None:
    config.write(patch, surface=DASHBOARD_SURFACE)


def resolver(*available: str):
    """A PATH lookup resolving only *available*, so a test describes a host."""

    def which(program: str) -> str | None:
        return f"/usr/bin/{program}" if program in available else None

    return which


def no_programs(program: str) -> str | None:
    return None


class Queue:
    """A Review_Queue projection over entries a test names."""

    def __init__(self, *entries: QueueEntry) -> None:
        self._entries = entries

    def snapshot(self, *, project: str | None = None) -> QueueSnapshot:
        return QueueSnapshot(entries=self._entries)


def waiting_entry(run_id: str, waiting_on: WaitingOn) -> QueueEntry:
    states = {
        WaitingOn.REVIEW: RunState.AWAITING_REVIEW,
        WaitingOn.BUDGET: RunState.HALTED_BUDGET,
        WaitingOn.STALL: RunState.STALLED,
    }
    return QueueEntry(
        run_id=run_id,
        project="proj",
        spec="spec-one",
        spec_type="feature",
        state=states[waiting_on],
        waiting_on=waiting_on,
        entered_ts="2026-01-01T00:00:00+00:00",
        waiting_s=60.0,
        source=None,
        item_id=None,
        cost_credits=0.0,
    )


def define_source(config: ConfigStore, *, name: str = "upstream", poll: list[str]) -> None:
    configure(config, {"sources": {name: {"poll": poll, "enabled": True}}})


class TestTheVocabularyIsTheOneThatAlreadyExisted:
    def test_a_finding_severity_is_the_engine_severity_enum(self) -> None:
        # Not a second severity vocabulary: the enum a Finding carries is the one
        # document validation carries, so a condition cannot read blocking on a
        # panel and advisory at a gate.
        assert doctor_module.Severity is findings_module.Severity

    def test_blocking_and_advisory_are_one_spelling_on_that_enum(self) -> None:
        assert Severity.ERROR.blocking is True
        assert Severity.ERROR.advisory is False
        assert Severity.WARNING.blocking is False
        assert Severity.WARNING.advisory is True

    def test_a_degraded_capability_finding_quotes_the_shared_identifier(
        self, config: ConfigStore
    ) -> None:
        degradation = Degradation(
            finding_id=FINDING_PROVIDER_TIMEOUT,
            reason="the provider did not answer in time",
            transport="command",
        )
        report = Doctor(config=config, which=no_programs, degradations=(degradation,)).run()

        # The identifier is the constant the engine already quotes when it marks a
        # run degraded, not a doctor-local restatement of the same condition.
        assert FINDING_PROVIDER_TIMEOUT in report.identifiers

    def test_an_agent_that_cannot_reach_the_engine_tools_uses_the_advisory_code(
        self, config: ConfigStore
    ) -> None:
        configure(
            config,
            {
                "cost_profiles": {
                    "cheap": {"roles": {"review": {"agent": "narrow", "model": "model-one"}}}
                },
                "projects": {"proj": {"path": "/w/proj", "cost_profile": "cheap"}},
            },
        )

        def agents(name: str) -> AgentToolSurface:
            return AgentToolSurface(name=name, found=False)

        report = Doctor(config=config, which=no_programs, agents=agents).run()

        assert AGENT_NOT_INSTALLED in report.identifiers

    def test_a_finding_must_say_what_is_wrong_and_what_resolves_it(self) -> None:
        with pytest.raises(ValueError):
            Finding(
                identifier="x.y",
                severity=Severity.ERROR,
                surface="config",
                cause=Untrusted(""),
                action=Untrusted("do the thing"),
            )
        with pytest.raises(ValueError):
            Finding(
                identifier="x.y",
                severity=Severity.ERROR,
                surface="config",
                cause=Untrusted("broken"),
                action=Untrusted("   "),
            )


class TestTheAggregationCoversEveryFamily:
    def test_a_broken_host_reports_prerequisites_per_phase(self, config: ConfigStore) -> None:
        configure(
            config,
            {
                "workflow": {"stages": {"submit": [[ABSENT_PROGRAM, "pr"]]}},
                "projects": {"proj": {"path": "/w/proj", "base_branch": "main"}},
            },
        )

        report = Doctor(config=config, which=no_programs).run()

        programs = report.for_identifier(prerequisite_finding_id(CheckName.PROGRAMS))
        assert programs, report.identifiers
        # A phase, not a surface name: the operator has to know which rung is
        # blocked, because a project that cannot deliver may still author.
        assert {finding.surface for finding in programs} <= {level.value for level in AutonomyLevel}

    def test_an_invalid_configuration_is_a_finding_naming_its_key(
        self, config: ConfigStore, tmp_path: Path
    ) -> None:
        config.path.parent.mkdir(parents=True, exist_ok=True)
        config.path.write_text(json.dumps({"version": 1, "sources": "nonsense"}), encoding="utf-8")

        report = Doctor(config=config, which=no_programs).run()

        invalid = report.for_identifier(FINDING_CONFIG_INVALID)
        assert invalid
        assert any(finding.declared_at.startswith("sources") for finding in invalid)

    def test_an_engaged_kill_switch_is_a_blocking_finding(
        self, config: ConfigStore, tmp_path: Path
    ) -> None:
        switch = KillSwitch(tmp_path / "state")
        switch.engage(initiator="operator", reason="spending review")

        report = Doctor(config=config, which=no_programs, kill_switch=switch).run()

        engaged = report.for_identifier(FINDING_KILL_SWITCH_ENGAGED)
        assert engaged and engaged[0].blocking
        assert engaged[0].surface == SURFACE_BUDGET

    def test_an_unreadable_kill_switch_record_is_its_own_finding(
        self, config: ConfigStore, tmp_path: Path
    ) -> None:
        switch = KillSwitch(tmp_path / "state")
        switch.path.parent.mkdir(parents=True, exist_ok=True)
        switch.path.write_text("{not json", encoding="utf-8")

        report = Doctor(config=config, which=no_programs, kill_switch=switch).run()

        # Engaged out of doubt and engaged by an operator have different fixes, so
        # they are different identifiers rather than one with a flag.
        assert report.for_identifier(FINDING_KILL_SWITCH_UNREADABLE)
        assert not report.for_identifier(FINDING_KILL_SWITCH_ENGAGED)

    def test_runs_parked_on_a_person_are_one_finding_per_reason(self, config: ConfigStore) -> None:
        queue = Queue(
            waiting_entry("run-a", WaitingOn.REVIEW),
            waiting_entry("run-b", WaitingOn.BUDGET),
        )

        report = Doctor(config=config, which=no_programs, queue=queue).run()

        review = report.for_identifier(runs_waiting_finding_id(WaitingOn.REVIEW))
        budget = report.for_identifier(runs_waiting_finding_id(WaitingOn.BUDGET))
        assert review and budget
        assert review[0].surface == SURFACE_REVIEW_QUEUE
        # A verdict is somebody's turn; a budget halt is a run nothing will move.
        assert review[0].blocking is False
        assert budget[0].blocking is True
        assert runs_waiting_finding_id(WaitingOn.STALL) in report.passing

    def test_an_unhealthy_recorded_poll_is_a_source_health_finding(
        self, config: ConfigStore
    ) -> None:
        define_source(config, poll=[ABSENT_PROGRAM, "list"])
        outcome = poll_source(config, "upstream")

        report = Doctor(
            config=config, which=resolver(ABSENT_PROGRAM), poll_outcomes=(outcome,)
        ).run()

        assert outcome.reason is HealthReason.PROGRAM_UNAVAILABLE
        assert health_finding_id(HealthReason.PROGRAM_UNAVAILABLE, "upstream") in (
            report.identifiers
        )

    def test_every_health_reason_has_a_finding_identifier(self) -> None:
        # A reason with no identifier would surface as a KeyError inside the
        # aggregation instead of as a Finding.
        assert set(HEALTH_REASON_FINDINGS) == set(HealthReason)


class TestOneResolutionForSourceProgramHealth:
    def test_an_absent_poll_program_yields_both_representations_naming_one_program(
        self, config: ConfigStore
    ) -> None:
        """The pin for the two answers to "is this source's poll program on PATH".

        ``prerequisites.check_source`` and ``watch/poll``'s
        ``PROGRAM_UNAVAILABLE`` answer the same question in two representations.
        Both fail closed today, so this is a convergence rather than a defect --
        and this test is what fails if either side is refactored away from the
        other, because a Doctor panel built on one and a watcher tick built on the
        other must not disagree about which program is missing.
        """
        define_source(config, poll=[ABSENT_PROGRAM, "list"])

        prerequisite_report = check_source(config, "upstream")
        poll_outcome = poll_source(config, "upstream")

        # Representation one: an unmet prerequisite naming the program.
        unmet = prerequisite_report.unmet
        assert len(unmet) == 1
        assert unmet[0].check is CheckName.WATCH_PROGRAMS
        assert ABSENT_PROGRAM in unmet[0].missing

        # Representation two: an unhealthy poll naming the same program.
        assert poll_outcome.status is PollStatus.UNHEALTHY
        assert poll_outcome.reason is HealthReason.PROGRAM_UNAVAILABLE
        assert poll_outcome.missing_program == ABSENT_PROGRAM

        # And both fold onto one Finding identifier, which is what makes the two
        # representations one condition with one resolving action.
        assert health_finding_id(HealthReason.PROGRAM_UNAVAILABLE, "upstream") == (
            prerequisite_finding_id(CheckName.WATCH_PROGRAMS, source="upstream")
        )

    def test_the_doctor_reports_one_identifier_for_the_one_absent_program(
        self, config: ConfigStore
    ) -> None:
        define_source(config, poll=[ABSENT_PROGRAM, "list"])
        outcome = poll_source(config, "upstream")

        report = Doctor(config=config, which=no_programs, poll_outcomes=(outcome,)).run()

        shared = health_finding_id(HealthReason.PROGRAM_UNAVAILABLE, "upstream")
        # Both producers contributed, and neither invented a second name for the
        # same broken host.
        assert len(report.for_identifier(shared)) == 2
        assert [
            identifier for identifier in report.identifiers if "watch_program" in identifier
        ] == ([shared])


class TestABrokenCheckDoesNotBreakTheDiagnostic:
    def broken(self, config: ConfigStore, *, name: str) -> Doctor:
        """A doctor whose *name* check raises, everything else untouched."""

        class Sabotaged(Doctor):
            def checks(self) -> tuple[DoctorCheck, ...]:
                def explode() -> CheckOutcome:
                    raise RuntimeError("the check itself is broken")

                return tuple(
                    DoctorCheck(check.name, explode if check.name == name else check.run)
                    for check in super().checks()
                )

        return Sabotaged(config=config, which=no_programs, kill_switch=self.switch)

    @pytest.fixture(autouse=True)
    def _switch(self, tmp_path: Path) -> None:
        self.switch = KillSwitch(tmp_path / "state")
        self.switch.engage(initiator="operator", reason="spending review")

    def test_the_other_checks_still_report_every_finding_they_would_have(
        self, config: ConfigStore
    ) -> None:
        configure(config, {"sources": {"upstream": {"poll": [ABSENT_PROGRAM], "enabled": True}}})
        healthy = Doctor(config=config, which=no_programs, kill_switch=self.switch).run()

        broken = self.broken(config, name=CHECK_CONFIGURATION).run()

        # The specific claim: not "something came back", but that every Finding
        # the other seven checks produce is still here, one for one.
        surviving = {
            finding.identifier
            for finding in broken.findings
            if not finding.identifier.startswith("doctor.")
        }
        expected = {
            finding.identifier
            for finding in healthy.findings
            if finding.identifier != FINDING_CONFIG_INVALID
        }
        assert surviving == expected
        assert FINDING_KILL_SWITCH_ENGAGED in surviving
        assert prerequisite_finding_id(CheckName.WATCH_PROGRAMS, source="upstream") in surviving

    def test_the_broken_check_appears_as_a_finding_naming_itself(self, config: ConfigStore) -> None:
        report = self.broken(config, name=CHECK_SOURCE_HEALTH).run()

        failed = report.for_identifier(check_failed_finding_id(CHECK_SOURCE_HEALTH))
        assert failed, report.identifiers
        assert failed[0].surface == SURFACE_DOCTOR
        assert failed[0].blocking
        assert failed[0].subject == CHECK_SOURCE_HEALTH
        assert "the check itself is broken" in failed[0].cause.for_display()

    @pytest.mark.parametrize(
        "name",
        [
            CHECK_CONFIGURATION,
            CHECK_PREREQUISITES,
            CHECK_SOURCE_HEALTH,
            CHECK_PROGRAM_VERSIONS,
            CHECK_BUDGET,
            CHECK_REVIEW_QUEUE,
        ],
    )
    def test_no_single_broken_check_can_abort_the_aggregation(
        self, config: ConfigStore, name: str
    ) -> None:
        report = self.broken(config, name=name).run()

        # Every check is guarded, not just the one somebody remembered.
        assert report.for_identifier(check_failed_finding_id(name))
        assert len(report.findings) > 1


class TestUntrustedTextIsDataAndIsNeverExecuted:
    def test_hostile_source_and_probe_text_executes_nothing(
        self, config: ConfigStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        define_source(config, name=HOSTILE_TEXT, poll=[HOSTILE_TEXT, "list"])

        def hostile_version(path: str) -> str:
            return HOSTILE_TEXT

        report = Doctor(
            config=config,
            which=resolver(HOSTILE_TEXT),
            minimum_versions={HOSTILE_TEXT: "2.0"},
            version_of=hostile_version,
        ).run()

        assert report.findings
        for artefact in PAYLOAD_ARTEFACTS:
            assert not (tmp_path / artefact).exists()

    def test_finding_prose_is_not_a_string_that_can_slip_into_a_template(
        self, config: ConfigStore
    ) -> None:
        define_source(config, poll=[ABSENT_PROGRAM])

        report = Doctor(config=config, which=no_programs).run()

        assert report.findings
        for finding in report.findings:
            # Untrusted has no __str__, so provider- and document-authored text
            # cannot reach an f-string, a log line, or a command template by
            # looking like a str. A display path has to ask for the characters.
            assert not isinstance(finding.cause, str)
            assert not isinstance(finding.action, str)
            assert isinstance(finding.cause.for_display(), str)

    def test_an_identifier_shaped_subject_is_sanitized_at_construction(self) -> None:
        finding = Finding(
            identifier="x.y",
            severity=Severity.WARNING,
            surface="config",
            cause=Untrusted("something"),
            action=Untrusted("do something"),
            subject="gh\r\nnext-line\x1b[2K",
        )

        # Rendered through the display contract rather than a second sanitizer:
        # control characters are what overwrite the previous line of a log.
        assert "\r" not in finding.subject
        assert "\n" not in finding.subject
        assert "\x1b" not in finding.subject

    def test_a_rendered_report_carries_no_unsanitized_control_characters(
        self, config: ConfigStore
    ) -> None:
        define_source(config, name="up\x1bstream", poll=[ABSENT_PROGRAM])

        report = Doctor(config=config, which=no_programs).run()
        rendered = json.dumps(report.to_json_object())

        assert "\\u001b" not in rendered

    def test_a_hostile_program_name_is_never_run_by_the_real_version_probe(
        self, config: ConfigStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        # The real probe, resolving to a real interpreter: argv is engine-authored
        # (the resolved path plus a literal flag) and passed as a list, so a name
        # full of shell constructs is a name rather than a command.
        report = Doctor(
            config=config,
            which=lambda program: sys.executable,
            minimum_versions={HOSTILE_TEXT: "999.0"},
        ).run()

        assert report.for_identifier(scoped_finding_id(FINDING_PROGRAM_VERSION, HOSTILE_TEXT))
        for artefact in PAYLOAD_ARTEFACTS:
            assert not (tmp_path / artefact).exists()


class TestDeclaredMinimumVersionsAreVerified:
    def test_a_program_below_its_declared_minimum_is_blocking(self, config: ConfigStore) -> None:
        report = Doctor(
            config=config,
            which=resolver("gh"),
            minimum_versions={"gh": "2.40.0"},
            version_of=lambda path: "gh version 2.4.0 (2023-01-01)",
        ).run()

        version = report.for_identifier(scoped_finding_id(FINDING_PROGRAM_VERSION, "gh"))
        assert version and version[0].blocking
        assert "2.40.0" in version[0].cause.for_display()

    def test_presence_alone_does_not_satisfy_a_declared_minimum(self, config: ConfigStore) -> None:
        present = Doctor(
            config=config,
            which=resolver("gh"),
            minimum_versions={"gh": "2.40.0"},
            version_of=lambda path: "gh version 2.41.0",
        ).run()

        # The check the requirement exists for: a policy-pushed downgrade leaves
        # the program present, so presence green and version green are different
        # answers.
        assert scoped_finding_id(FINDING_PROGRAM_VERSION, "gh") in present.passing
        assert not present.for_identifier(scoped_finding_id(FINDING_PROGRAM_VERSION, "gh"))

    def test_a_program_that_reports_no_version_is_advisory_not_a_pass(
        self, config: ConfigStore
    ) -> None:
        report = Doctor(
            config=config,
            which=resolver("gh"),
            minimum_versions={"gh": "2.40.0"},
            version_of=lambda path: "unrecognised output",
        ).run()

        version = report.for_identifier(scoped_finding_id(FINDING_PROGRAM_VERSION, "gh"))
        assert version and version[0].blocking is False
        assert scoped_finding_id(FINDING_PROGRAM_VERSION, "gh") not in report.passing

    def test_an_absent_program_is_reported_under_the_presence_identifier(
        self, config: ConfigStore
    ) -> None:
        report = Doctor(
            config=config, which=no_programs, minimum_versions={ABSENT_PROGRAM: "1.0"}
        ).run()

        # Absence already has an identifier. A second one for the same condition
        # is how a refusal and a panel end up naming one broken host twice.
        assert report.for_identifier(prerequisite_finding_id(CheckName.PROGRAMS))
        assert not report.for_identifier(scoped_finding_id(FINDING_PROGRAM_VERSION, ABSENT_PROGRAM))

    @pytest.mark.parametrize(
        "text,expected",
        [
            ("gh version 2.40.1 (2024-01-01)", (2, 40, 1)),
            ("1", (1,)),
            ("no numbers here", ()),
            ("", ()),
        ],
    )
    def test_a_version_is_parsed_out_of_command_output(
        self, text: str, expected: tuple[int, ...]
    ) -> None:
        assert parse_version(text) == expected

    @pytest.mark.parametrize(
        "found,minimum,satisfied",
        [
            ((2, 1), (2, 1, 0), True),
            ((2, 1), (2, 1, 1), False),
            ((3,), (2, 9, 9), True),
            ((2, 10), (2, 9), True),
        ],
    )
    def test_a_shorter_version_pads_rather_than_sorts_lexicographically(
        self, found: tuple[int, ...], minimum: tuple[int, ...], satisfied: bool
    ) -> None:
        assert version_satisfies(found, minimum) is satisfied


class TestTheDoctorModifiesNothingItDiagnoses:
    def test_a_full_aggregation_over_a_broken_state_leaves_the_config_bytes_identical(
        self, config: ConfigStore, tmp_path: Path, history: DoctorHistory
    ) -> None:
        configure(
            config,
            {
                "sources": {
                    "upstream": {
                        "poll": [ABSENT_PROGRAM],
                        "enabled": True,
                        "autonomy": {"default": {"default": "integration"}},
                    }
                },
                "workflow": {"stages": {"submit": [[ABSENT_PROGRAM, "pr"]]}},
                "projects": {"proj": {"path": "/w/proj", "base_branch": "main"}},
                "capabilities": {"analysis": {"transport": "command", "command": [ABSENT_PROGRAM]}},
            },
        )
        switch = KillSwitch(tmp_path / "state")
        switch.engage(initiator="operator")
        before = config.path.read_bytes()

        report = Doctor(
            config=RefusingStore(config),
            which=no_programs,
            branch_exists=lambda branch: False,
            kill_switch=switch,
            queue=Queue(waiting_entry("run-a", WaitingOn.BUDGET)),
            minimum_versions={ABSENT_PROGRAM: "1.0"},
            history=history,
            degradations=(
                Degradation(
                    finding_id=FINDING_PROVIDER_TIMEOUT, reason="slow", transport="command"
                ),
            ),
        ).run()

        # Read-only asserted by bytes rather than by inspection: the autonomy
        # policy and the delivery workflow live in this document, so byte equality
        # covers both of the objects the requirement names.
        assert config.path.read_bytes() == before
        assert report.blocking, "the state under test has to actually be broken"

    def test_the_doctor_never_calls_a_config_write(
        self, config: ConfigStore, tmp_path: Path, history: DoctorHistory
    ) -> None:
        configure(config, {"sources": {"upstream": {"poll": [ABSENT_PROGRAM], "enabled": True}}})
        store = RefusingStore(config)

        Doctor(config=store, which=no_programs, history=history).run()

        # The store refuses a write outright, so an attempt fails the test at the
        # call rather than being noticed afterwards.
        assert store.write_attempts == 0

    def test_the_only_thing_written_is_the_doctors_own_history(
        self, config: ConfigStore, tmp_path: Path
    ) -> None:
        state = tmp_path / "state"
        history = DoctorHistory(state)

        Doctor(config=config, which=no_programs, history=history).run()

        assert history.path.exists()
        assert [path.name for path in sorted(state.iterdir())] == [history.path.name]


class RefusingStore(ConfigStore):
    """A configuration store that refuses every write.

    Wraps a real store rather than replacing it, so reads behave exactly as the
    doctor's real collaborator does and only the write path differs.
    """

    def __init__(self, inner: ConfigStore) -> None:
        super().__init__(root=inner.root)
        self.write_attempts = 0

    def write(self, *args: Any, **kwargs: Any) -> Any:
        self.write_attempts += 1
        raise AssertionError("the doctor must not write configuration")


class TestARegressionIsNotAFirstFailure:
    def failing(self, config: ConfigStore, history: DoctorHistory) -> Doctor:
        return Doctor(
            config=config,
            which=no_programs,
            history=history,
            clock=lambda: self.now,
        )

    def passing(self, config: ConfigStore, history: DoctorHistory) -> Doctor:
        return Doctor(
            config=config,
            which=resolver(ABSENT_PROGRAM),
            history=history,
            clock=lambda: self.now,
        )

    @pytest.fixture(autouse=True)
    def _clock(self) -> None:
        self.now = "2026-01-01T00:00:00+00:00"

    def test_a_check_that_never_passed_is_not_a_regression(
        self, config: ConfigStore, history: DoctorHistory
    ) -> None:
        define_source(config, poll=[ABSENT_PROGRAM])

        report = self.failing(config, history).run()

        identifier = prerequisite_finding_id(CheckName.WATCH_PROGRAMS, source="upstream")
        finding = report.for_identifier(identifier)[0]
        assert finding.regressed is False
        assert finding.last_passed_ts == ""
        assert report.to_notify == ()

    def test_a_check_that_passed_and_now_fails_is_a_regression_with_its_last_pass(
        self, config: ConfigStore, history: DoctorHistory
    ) -> None:
        define_source(config, poll=[ABSENT_PROGRAM])
        self.passing(config, history).run()

        self.now = "2026-02-02T00:00:00+00:00"
        report = self.failing(config, history).run()

        identifier = prerequisite_finding_id(CheckName.WATCH_PROGRAMS, source="upstream")
        finding = report.for_identifier(identifier)[0]
        assert finding.regressed is True
        assert finding.last_passed_ts == "2026-01-01T00:00:00+00:00"
        assert [item.identifier for item in report.to_notify] == [identifier]

    def test_an_unchanged_regression_does_not_notify_again(
        self, config: ConfigStore, history: DoctorHistory
    ) -> None:
        define_source(config, poll=[ABSENT_PROGRAM])
        self.passing(config, history).run()
        first = self.failing(config, history).run()

        second = self.failing(config, history).run()
        third = self.failing(config, history).run()

        assert len(first.to_notify) == 1
        # Notify once, then stay quiet while nothing changes. Without a stable
        # identifier to key the last known result on, every call would look like a
        # new finding and notify-once would silently become notify-always.
        assert second.to_notify == ()
        assert third.to_notify == ()
        assert second.regressions and third.regressions

    def test_a_condition_that_clears_and_returns_notifies_again(
        self, config: ConfigStore, history: DoctorHistory
    ) -> None:
        define_source(config, poll=[ABSENT_PROGRAM])
        self.passing(config, history).run()
        self.failing(config, history).run()

        self.passing(config, history).run()
        again = self.failing(config, history).run()

        assert len(again.to_notify) == 1

    def test_identifiers_are_stable_across_calls_over_the_same_state(
        self, config: ConfigStore, history: DoctorHistory
    ) -> None:
        define_source(config, poll=[ABSENT_PROGRAM])

        first = self.failing(config, history).run()
        second = self.failing(config, history).run()

        assert first.identifiers == second.identifiers
        assert first.passing == second.passing

    def test_an_identifier_carries_nothing_volatile(
        self, config: ConfigStore, history: DoctorHistory
    ) -> None:
        define_source(config, poll=[ABSENT_PROGRAM])

        report = self.failing(config, history).run()

        for identifier in report.identifiers:
            assert "2026" not in identifier
            assert str(config.root) not in identifier
            assert not identifier.startswith("/")

    def test_an_unreadable_history_reports_no_regression_and_still_diagnoses(
        self, config: ConfigStore, history: DoctorHistory
    ) -> None:
        define_source(config, poll=[ABSENT_PROGRAM])
        history.path.parent.mkdir(parents=True, exist_ok=True)
        history.path.write_text("{not json", encoding="utf-8")

        report = self.failing(config, history).run()

        # Doubt is not evidence of a regression, and inventing one would notify
        # falsely -- the opposite of the drift signal this exists to give.
        assert report.findings
        assert report.regressions == ()
        assert report.to_notify == ()

    def test_a_history_that_cannot_be_written_is_a_finding_not_a_failure(
        self, config: ConfigStore, tmp_path: Path
    ) -> None:
        class Unwritable(DoctorHistory):
            def write(self, recorded: Any) -> None:
                raise StatePersistenceError("the state root is read-only")

        define_source(config, poll=[ABSENT_PROGRAM])
        report = Doctor(
            config=config, which=no_programs, history=Unwritable(tmp_path / "state")
        ).run()

        assert report.for_identifier(FINDING_HISTORY_UNWRITABLE)
        assert report.for_identifier(
            prerequisite_finding_id(CheckName.WATCH_PROGRAMS, source="upstream")
        )

    def test_history_is_not_consulted_when_none_is_supplied(self, config: ConfigStore) -> None:
        define_source(config, poll=[ABSENT_PROGRAM])

        report = Doctor(config=config, which=no_programs).run()

        # A caller that wants a pure read gets one: no history, no annotation, no
        # notification, and nothing written.
        assert report.findings
        assert report.to_notify == ()
        assert report.regressions == ()
