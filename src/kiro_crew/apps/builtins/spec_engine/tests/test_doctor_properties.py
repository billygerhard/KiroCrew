"""Property-based tests for the Doctor.

**No sabotaged check can take another check's Findings with it.** Whatever subset
of checks is made to raise, every Finding the remaining checks would have produced
on their own is still in the report, and each broken check appears as a Finding
naming itself. The scripted case covers one broken check; the failure this guards
against is the aggregation that keeps going for the check somebody remembered and
aborts for the one they did not.

**Read-only holds for every document, not the one that was tried.** The
configuration bytes are captured before and after an aggregation over generated
documents -- including documents that make several checks fail -- and compared.

**A stable identifier is what makes notify-once work.** Over generated repeat
counts, an unchanged regression notifies exactly once however many times the
doctor runs, which is only true if the identifier a result is remembered under
does not move between calls.

**Adversarial version output is parsed, never interpreted.** Shell metacharacters,
newlines, and quotes around a version number change neither the parse nor anything
on disk.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from kiro_crew.apps.builtins.spec_engine.engine.budget.switch import KillSwitch
from kiro_crew.apps.builtins.spec_engine.engine.config import ConfigStore
from kiro_crew.apps.builtins.spec_engine.engine.doctor import (
    FINDING_CHECK_FAILED_PREFIX,
    HEALTH_REASON_FINDINGS,
    CheckOutcome,
    Doctor,
    DoctorCheck,
    DoctorHistory,
    check_failed_finding_id,
    health_finding_id,
    parse_version,
    prerequisite_finding_id,
    version_satisfies,
)
from kiro_crew.apps.builtins.spec_engine.engine.findings import Severity
from kiro_crew.apps.builtins.spec_engine.engine.prerequisites import CheckName
from kiro_crew.apps.builtins.spec_engine.engine.watch.poll import HealthReason

#: Each example writes a configuration document and walks eight checks, so keep
#: the count modest rather than trading suite time for breadth.
MAX_EXAMPLES = 40

SETTINGS = settings(
    max_examples=MAX_EXAMPLES,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)

ABSENT_PROGRAM = "kirocrew-nonexistent-doctor-cli"

#: Names a configuration document might hold for a watch source, including the
#: ones an operator should not have written: control characters and escape
#: sequences reach the doctor through the document like any other text.
source_names = st.text(
    alphabet=st.characters(min_codepoint=1, max_codepoint=0x2FFF),
    min_size=1,
    max_size=12,
)

#: The checks that may be sabotaged, named by the aggregation itself so a check
#: added later is generated over without an edit here.
CHECK_NAMES = tuple(
    check.name for check in Doctor(config=ConfigStore(Path("/nonexistent"))).checks()
)


def no_programs(program: str) -> str | None:
    return None


def write_document(config: ConfigStore, document: dict[str, Any]) -> None:
    """Write *document* straight to disk, bypassing the validated write path.

    Deliberate: the point of these properties is what the doctor does with a
    document it did not get to approve, including one the schema would refuse.
    """
    config.path.parent.mkdir(parents=True, exist_ok=True)
    config.path.write_text(json.dumps(document), encoding="utf-8")


def sabotaged(base: Doctor, broken: frozenset[str]) -> Doctor:
    """*base* with every check in *broken* replaced by one that raises."""

    class Sabotaged(Doctor):
        def checks(self) -> tuple[DoctorCheck, ...]:
            def explode() -> CheckOutcome:
                raise RuntimeError("this check is broken")

            return tuple(
                DoctorCheck(check.name, explode if check.name in broken else check.run)
                for check in super().checks()
            )

    return Sabotaged(
        config=base.config,
        which=base.which,
        kill_switch=base.kill_switch,
        minimum_versions=base.minimum_versions,
    )


def contributed(doctor: Doctor, skip: frozenset[str]) -> set[str]:
    """Identifiers the checks outside *skip* produce when run on their own.

    A check that raises on its own contributes nothing; that is the same outcome
    the aggregation records for it, and the aggregation is what these properties
    are about.
    """
    identifiers: set[str] = set()
    for check in doctor.checks():
        if check.name in skip:
            continue
        try:
            outcome = check.run()
        except Exception:  # noqa: BLE001 - a check that cannot run contributes nothing
            continue
        identifiers.update(finding.identifier for finding in outcome.findings)
    return identifiers


class TestNoBrokenCheckTakesAnotherCheckWithIt:
    @SETTINGS
    @given(
        broken=st.frozensets(st.sampled_from(CHECK_NAMES), min_size=1, max_size=4),
        names=st.lists(source_names, min_size=0, max_size=3, unique=True),
        engaged=st.booleans(),
    )
    def test_every_surviving_check_still_reports_everything_it_would_have(
        self,
        tmp_path_factory: Any,
        broken: frozenset[str],
        names: list[str],
        engaged: bool,
    ) -> None:
        root = tmp_path_factory.mktemp("doctor")
        config = ConfigStore(root=root / "config")
        write_document(
            config,
            {
                "version": 1,
                "sources": {
                    name: {"poll": [ABSENT_PROGRAM, "list"], "enabled": True} for name in names
                },
            },
        )
        switch = KillSwitch(root / "state")
        if engaged:
            switch.engage(initiator="operator")
        base = Doctor(
            config=config,
            which=no_programs,
            kill_switch=switch,
            minimum_versions={ABSENT_PROGRAM: "1.0"},
        )

        report = sabotaged(base, broken).run()

        reported = {finding.identifier for finding in report.findings}
        expected = contributed(base, broken)
        # The claim is per-Finding survival, not a non-empty report: an
        # aggregation that swallowed the other seven checks and returned one
        # would satisfy "something came back".
        assert expected <= reported
        for name in broken:
            assert check_failed_finding_id(name) in reported

    @SETTINGS
    @given(broken=st.frozensets(st.sampled_from(CHECK_NAMES), min_size=1, max_size=8))
    def test_the_aggregation_never_raises_however_many_checks_break(
        self, tmp_path_factory: Any, broken: frozenset[str]
    ) -> None:
        root = tmp_path_factory.mktemp("doctor")
        config = ConfigStore(root=root / "config")
        base = Doctor(config=config, which=no_programs, kill_switch=KillSwitch(root / "state"))

        report = sabotaged(base, broken).run()

        failed = {
            finding.identifier
            for finding in report.findings
            if finding.identifier.startswith(FINDING_CHECK_FAILED_PREFIX)
        }
        # Exactly the sabotaged checks report as broken: no check that ran is
        # reported as having failed, and none that failed is reported as having
        # run.
        assert failed == {check_failed_finding_id(name) for name in broken}


class TestEveryFindingIsAddressable:
    @SETTINGS
    @given(
        names=st.lists(source_names, min_size=1, max_size=4, unique=True),
        engaged=st.booleans(),
    )
    def test_a_finding_names_a_severity_a_surface_a_cause_and_an_action(
        self, tmp_path_factory: Any, names: list[str], engaged: bool
    ) -> None:
        root = tmp_path_factory.mktemp("doctor")
        config = ConfigStore(root=root / "config")
        write_document(
            config,
            {
                "version": 1,
                "sources": {name: {"poll": [ABSENT_PROGRAM], "enabled": True} for name in names},
            },
        )
        switch = KillSwitch(root / "state")
        if engaged:
            switch.engage(initiator="operator")

        report = Doctor(config=config, which=no_programs, kill_switch=switch).run()

        for finding in report.findings:
            assert isinstance(finding.severity, Severity)
            assert finding.identifier.strip()
            assert finding.surface.strip()
            assert finding.cause.for_display().strip()
            assert finding.action.for_display().strip()
            # An identifier is compared, grouped, and logged on, so a control
            # character in one is a control character in a log line.
            assert not any(char in finding.identifier for char in "\r\n\x1b\t")
            assert not any(char in finding.subject for char in "\r\n\x1b\t")
            assert not any(char in finding.declared_at for char in "\r\n\x1b\t")

    @SETTINGS
    @given(names=st.lists(source_names, min_size=1, max_size=4, unique=True))
    def test_a_report_is_identical_across_calls_over_one_state(
        self, tmp_path_factory: Any, names: list[str]
    ) -> None:
        root = tmp_path_factory.mktemp("doctor")
        config = ConfigStore(root=root / "config")
        write_document(
            config,
            {
                "version": 1,
                "sources": {name: {"poll": [ABSENT_PROGRAM], "enabled": True} for name in names},
            },
        )
        doctor = Doctor(config=config, which=no_programs, kill_switch=KillSwitch(root / "state"))

        first = doctor.run()
        second = doctor.run()

        # Identical Findings from the same state is what lets an MCP tool and a UI
        # panel be two renderings of one operation rather than two answers.
        assert first.to_json_object() == second.to_json_object()


class TestTheDoctorModifiesNothing:
    @SETTINGS
    @given(
        names=st.lists(source_names, min_size=0, max_size=4, unique=True),
        junk=st.dictionaries(
            st.sampled_from(["workflow", "capabilities", "projects", "nonsense"]),
            st.one_of(st.text(max_size=6), st.integers(), st.booleans()),
            max_size=3,
        ),
    )
    def test_the_configuration_document_is_byte_identical_after_an_aggregation(
        self, tmp_path_factory: Any, names: list[str], junk: dict[str, Any]
    ) -> None:
        root = tmp_path_factory.mktemp("doctor")
        config = ConfigStore(root=root / "config")
        document: dict[str, Any] = {
            "version": 1,
            "sources": {name: {"poll": [ABSENT_PROGRAM], "enabled": True} for name in names},
        }
        document.update(junk)
        write_document(config, document)
        before = config.path.read_bytes()

        Doctor(
            config=config,
            which=no_programs,
            kill_switch=KillSwitch(root / "state"),
            history=DoctorHistory(root / "state"),
            minimum_versions={ABSENT_PROGRAM: "1.0"},
        ).run()

        # Read-only is a claim about every path, including the paths a malformed
        # document sends the checks down. The autonomy policy and the delivery
        # workflow live in this document, so byte equality covers both objects the
        # requirement names.
        assert config.path.read_bytes() == before


class TestNotifyOnceSurvivesRepetition:
    @SETTINGS
    @given(repeats=st.integers(min_value=1, max_value=6))
    def test_an_unchanged_regression_notifies_exactly_once(
        self, tmp_path_factory: Any, repeats: int
    ) -> None:
        root = tmp_path_factory.mktemp("doctor")
        config = ConfigStore(root=root / "config")
        write_document(
            config,
            {
                "version": 1,
                "sources": {"upstream": {"poll": [ABSENT_PROGRAM], "enabled": True}},
            },
        )
        history = DoctorHistory(root / "state")
        # One passing evaluation, so the later failures are regressions rather
        # than a check that was never configured.
        Doctor(config=config, which=lambda program: "/usr/bin/tool", history=history).run()

        notified = 0
        for _ in range(repeats):
            report = Doctor(config=config, which=no_programs, history=history).run()
            notified += len(report.to_notify)
            assert report.regressions

        assert notified == 1


class TestOneIdentifierForOneAbsentPollProgram:
    @SETTINGS
    @given(name=source_names)
    def test_both_representations_of_an_absent_poll_program_share_an_identifier(
        self, name: str
    ) -> None:
        # Bullet-proofing the convergence rather than the one example: whatever a
        # source is called, the prerequisite resolution and the poll health reason
        # name the same condition.
        assert health_finding_id(HealthReason.PROGRAM_UNAVAILABLE, name) == (
            prerequisite_finding_id(CheckName.WATCH_PROGRAMS, source=name)
        )

    def test_every_health_reason_resolves_to_an_identifier(self) -> None:
        for reason in HealthReason:
            assert HEALTH_REASON_FINDINGS[reason].strip()


class TestVersionOutputIsParsedNotInterpreted:
    @SETTINGS
    @given(
        parts=st.lists(st.integers(min_value=0, max_value=999), min_size=1, max_size=4),
        # Deliberately digit-free and dot-free, so the expectation is exact: the
        # shell constructs cannot contribute to the number that comes out.
        prefix=st.text(alphabet="abz ;&|`$()'\"\n\t", max_size=12),
        suffix=st.text(alphabet="abz ;&|`$()'\"\n\t", max_size=12),
    )
    def test_a_version_is_read_out_of_adversarial_output_unchanged(
        self, parts: list[int], prefix: str, suffix: str
    ) -> None:
        rendered = ".".join(str(part) for part in parts)

        found = parse_version(f"{prefix}{rendered}{suffix}")

        # The number is read, the shell constructs around it are not. Nothing here
        # spawns anything: the probe's output is data on the way to a comparison.
        assert found == tuple(parts)

    @SETTINGS
    @given(
        found=st.lists(st.integers(min_value=0, max_value=99), min_size=1, max_size=4),
        minimum=st.lists(st.integers(min_value=0, max_value=99), min_size=1, max_size=4),
    )
    def test_a_minimum_comparison_is_total_and_component_wise(
        self, found: list[int], minimum: list[int]
    ) -> None:
        assert version_satisfies(found, minimum) or version_satisfies(minimum, found)
        assert version_satisfies(found, found)
        # Padding, not lexicographic ordering: 2.10 is above 2.9 and 2.1 satisfies
        # 2.1.0, which string comparison gets wrong in both directions.
        assert version_satisfies(found, found + [0])
