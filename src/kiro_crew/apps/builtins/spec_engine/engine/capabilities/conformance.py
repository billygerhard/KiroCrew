"""The conformance runner: an executable check on a published extension point.

An extension point without a conformance check is a promise. This module is the
check: it puts a candidate provider through the bundled fixtures for one
capability and reports, per fixture and per assertion class, what held.

Five assertion classes, and each exists because a provider can satisfy the other
four while failing it.

* **schema validity** — the response satisfies the capability's published response
  schema. Nothing else in the report means anything without this one, because a
  response the engine cannot parse is a response it can make no claim about.
* **planted defect detection** — a fixture with a known defect gets either a
  finding referencing it or an explicit declaration that the document was
  skipped. What is refused is the third answer, where a provider declares the
  document processed and reports nothing: that is the answer that reads as "the
  spec is clean".
* **declared coverage** — the response says what it processed and what it left
  out, with a reason for each omission, and does not claim both about one item.
  A response carrying only findings cannot distinguish "nothing wrong" from
  "nothing looked at".
* **timeout honoring** — the answer arrives inside the deadline the request
  carried. A provider that quietly runs long is how a caller ends up blocked on
  a stream that never ends.
* **repeatability** — the same fixture through the same candidate twice produces
  the same findings, the same coverage, and the same result body. A provider
  whose answer moves between two identical calls cannot be reasoned about, and
  its clean pass is not evidence of anything.

The runner's own honesty is the thing most worth getting right here, because a
runner that passes everything is worse than no runner at all: it reports the
opposite of the truth, and it is trusted precisely because it exists. Two design
choices carry that weight.

First, a report is not passing merely for having no failures. It also has to have
*executed* every fixture and every assertion class the suite declared. An empty
result set is a failure, so a runner that lost its fixtures, short-circuited a
loop, or was handed a capability with nothing to run reports that rather than
success.

Second, detection is matched on the references a finding carries rather than on
any vocabulary of finding kinds. A candidate brings its own kinds; a runner that
demanded the bundled analyzer's spellings would be passable only by the bundled
analyzer, which is the same as not checking. What every conforming provider does
share is that a finding names the criteria or tasks it concerns, since that is
what makes a finding routable at all.

The runner talks to a candidate through :class:`Candidate`, which answers with a
wire payload rather than an engine object. That is deliberate: the payload is
what an external provider actually sends, so a builtin verified here is held to
the same contract over the same bytes. :class:`BuiltinCandidate` adapts an
in-process provider, and :class:`TransportCandidate` adapts anything reachable
over a capability transport, so the same suite judges the reference
implementation and someone else's program.
"""

from __future__ import annotations

import json
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol, Sequence

from .contracts import (
    ArtifactRef,
    CapabilityRequest,
    require_delegable,
    sanitized,
)
from .fixtures import (
    DEFECT_REPORTING_CAPABILITIES,
    DOCUMENT_CAPABILITIES,
    FIXTURE_CONTRADICTORY_CRITERIA,
    FIXTURE_COVERAGE_HOLE,
    FIXTURE_DOCUMENTS,
    FIXTURE_FILENAMES,
    FIXTURE_MALFORMED_RESPONSE,
    FIXTURE_MINIMAL_REQUEST,
    FIXTURE_OVERSIZED_DOCUMENT,
    FIXTURE_PLANTED_AMBIGUITY,
    FIXTURE_RATIONALES,
    PLANTED_DEFECTS,
    ConformanceFixture,
    PlantedDefect,
    oversized_documents,
)
from .providers import BuiltinProvider
from .schemas import REQUEST, schema_for, validate_response
from .transports import CapabilityTransport

#: The assertion classes a suite may make. Names are stable: a report is read by
#: whoever is deciding whether to bind a provider, and a renamed check makes two
#: reports incomparable.
CHECK_SCHEMA_VALIDITY = "schema_validity"
CHECK_PLANTED_DEFECT = "planted_defect"
CHECK_DECLARED_COVERAGE = "declared_coverage"
CHECK_TIMEOUT_HONORING = "timeout_honoring"
CHECK_REPEATABILITY = "repeatability"

#: Every assertion class, in the order a report lists them.
CHECK_CLASSES: tuple[str, ...] = (
    CHECK_SCHEMA_VALIDITY,
    CHECK_PLANTED_DEFECT,
    CHECK_DECLARED_COVERAGE,
    CHECK_TIMEOUT_HONORING,
    CHECK_REPEATABILITY,
)

#: Deadline the runner puts on a fixture request when the fixture names none.
#: Generous, because the ceiling under test is the provider's honesty about its
#: own deadline rather than its speed, and a tight default would fail a
#: legitimately slow provider on a loaded machine.
DEFAULT_DEADLINE_S = 10

#: How far past its deadline a candidate may answer before the runner calls it a
#: failure. Non-zero because the runner measures the whole call including its own
#: request construction, and a bound with no slack would report scheduling noise
#: as a provider defect.
DEFAULT_GRACE_S = 2.0

#: Separator a provider may use to namespace a coverage token. A provider that
#: declares a document skipped names the artifact kind, optionally prefixed --
#: ``requirements`` and ``document:requirements`` both name the requirements
#: document -- so the runner reads the last segment when matching an artifact.
COVERAGE_NAMESPACE = ":"


class Candidate(Protocol):
    """A provider under test, answering with the payload it would put on the wire.

    The wire payload rather than an engine object, so that a builtin and an
    external program are judged against the same published contract over the same
    representation. A builtin that only satisfies the contract after the engine
    has helpfully rebuilt its answer does not satisfy the contract.
    """

    @property
    def name(self) -> str: ...

    def respond(self, request: CapabilityRequest) -> Any: ...


@dataclass(frozen=True)
class BuiltinCandidate:
    """An in-process provider put through the suite as if it were external."""

    provider: BuiltinProvider
    label: str = ""

    @property
    def name(self) -> str:
        return self.label or self.provider.identity.name

    def respond(self, request: CapabilityRequest) -> Any:
        return self.provider.serve(request).to_wire()


@dataclass(frozen=True)
class TransportCandidate:
    """A provider reached over a capability transport, verified before it is bound.

    The point of verifying a candidate is to learn what it does *before* it serves
    a run, so this deliberately does not route through the registry: the registry
    degrades a broken provider to the builtin and continues, which is right for a
    run and would hide exactly what a conformance report is asked to reveal.
    """

    transport: CapabilityTransport
    label: str

    @property
    def name(self) -> str:
        return self.label

    def respond(self, request: CapabilityRequest) -> Any:
        return self.transport.invoke(request, timeout_s=request.deadline_s)


@dataclass(frozen=True)
class CheckResult:
    """One assertion class, evaluated against one fixture."""

    check: str
    fixture: str
    passed: bool
    #: Engine-authored explanation. Provider-authored tokens reaching it pass
    #: through :func:`~.contracts.sanitized` first, for the same reason the
    #: registry keeps provider prose out of a degradation reason: this string is
    #: printed, logged, and pasted into an issue.
    detail: str

    def __str__(self) -> str:
        mark = "pass" if self.passed else "FAIL"
        return f"[{mark}] {self.fixture}/{self.check}: {self.detail}"

    def to_json_object(self) -> dict[str, Any]:
        return {
            "check": self.check,
            "fixture": self.fixture,
            "passed": self.passed,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class ConformanceReport:
    """What a candidate did with a capability's whole suite.

    :attr:`passed` is deliberately not "no failures". A suite that ran nothing,
    or that ran fixtures but never evaluated one of the assertion classes it
    declared, has produced no evidence, and reporting that as a pass is the one
    failure mode of a conformance runner that cannot be recovered from later:
    everyone downstream believes the provider was checked.
    """

    capability: str
    candidate: str
    #: Fixtures the suite declared, whether or not each produced a result.
    declared_fixtures: tuple[str, ...]
    #: Assertion classes the suite declared, likewise.
    declared_checks: tuple[str, ...]
    results: tuple[CheckResult, ...] = ()

    @property
    def failures(self) -> tuple[CheckResult, ...]:
        return tuple(result for result in self.results if not result.passed)

    @property
    def executed_fixtures(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(result.fixture for result in self.results))

    @property
    def executed_checks(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(result.check for result in self.results))

    @property
    def gaps(self) -> tuple[str, ...]:
        """What the suite declared and never evaluated.

        A gap is not a provider's failure; it is the runner reporting that it
        cannot speak for part of the contract. Kept separate from
        :attr:`failures` so the two are never read as the same thing, and folded
        into :attr:`passed` so neither can be ignored.
        """
        gaps: list[str] = []
        if not self.results:
            gaps.append("the suite produced no results at all")
        executed_fixtures = frozenset(self.executed_fixtures)
        for fixture in self.declared_fixtures:
            if fixture not in executed_fixtures:
                gaps.append(f"fixture {fixture!r} was declared but never run")
        executed_checks = frozenset(self.executed_checks)
        for check in self.declared_checks:
            if check not in executed_checks:
                gaps.append(f"check {check!r} was declared but never evaluated")
        return tuple(gaps)

    @property
    def passed(self) -> bool:
        return not self.failures and not self.gaps

    def summary(self) -> str:
        """One line naming the verdict and what produced it."""
        verdict = "conforms" if self.passed else "does not conform"
        return (
            f"{self.candidate} {verdict} for capability {self.capability}: "
            f"{len(self.results) - len(self.failures)}/{len(self.results)} checks passed "
            f"over {len(self.executed_fixtures)} fixtures, {len(self.gaps)} gaps"
        )

    def report_text(self) -> str:
        """The whole report, one line per check and per gap."""
        lines = [self.summary()]
        lines.extend(str(result) for result in self.results)
        lines.extend(f"[GAP ] {gap}" for gap in self.gaps)
        return "\n".join(lines)

    def to_json_object(self) -> dict[str, Any]:
        return {
            "capability": self.capability,
            "candidate": self.candidate,
            "passed": self.passed,
            "declared_fixtures": list(self.declared_fixtures),
            "declared_checks": list(self.declared_checks),
            "results": [result.to_json_object() for result in self.results],
            "gaps": list(self.gaps),
        }


def suite_for(capability: str, *, deadline_s: int = 0) -> tuple[ConformanceFixture, ...]:
    """The bundled fixtures a candidate for *capability* is put through.

    Assembled per call rather than held in a table because the oversized
    fixture's document is generated: one shared copy would hold a quarter of a
    megabyte for the lifetime of every process that imports this module, on
    behalf of a suite most of them never run.
    """
    require_delegable(capability)
    envelope = (CHECK_SCHEMA_VALIDITY, CHECK_DECLARED_COVERAGE, CHECK_TIMEOUT_HONORING)
    repeatable = envelope + (CHECK_REPEATABILITY,)
    detecting = (
        repeatable + (CHECK_PLANTED_DEFECT,)
        if capability in DEFECT_REPORTING_CAPABILITIES
        else repeatable
    )

    def build(
        name: str,
        checks: tuple[str, ...],
        documents: Mapping[str, str],
        planted: tuple[PlantedDefect, ...] = (),
    ) -> ConformanceFixture:
        return ConformanceFixture(
            name=name,
            capability=capability,
            documents=documents,
            checks=checks,
            planted=planted,
            deadline_s=deadline_s,
            rationale=FIXTURE_RATIONALES[name],
        )

    if capability not in DOCUMENT_CAPABILITIES:
        return (build(FIXTURE_MINIMAL_REQUEST, repeatable, {}),)
    defect_bearing = (
        FIXTURE_PLANTED_AMBIGUITY,
        FIXTURE_CONTRADICTORY_CRITERIA,
        FIXTURE_COVERAGE_HOLE,
    )
    fixtures = [
        build(name, detecting, FIXTURE_DOCUMENTS[name], (PLANTED_DEFECTS[name],))
        for name in defect_bearing
    ]
    fixtures.append(build(FIXTURE_OVERSIZED_DOCUMENT, repeatable, oversized_documents()))
    fixtures.append(
        build(FIXTURE_MALFORMED_RESPONSE, envelope, FIXTURE_DOCUMENTS[FIXTURE_MALFORMED_RESPONSE])
    )
    return tuple(fixtures)


@dataclass(frozen=True)
class _Attempt:
    """One call to a candidate: what came back, how long it took, or what broke."""

    payload: Any = None
    elapsed_s: float = 0.0
    error: str = ""

    @property
    def raised(self) -> bool:
        return bool(self.error)


@dataclass(frozen=True)
class ConformanceRunner:
    """Puts a candidate through a capability's suite and reports what held."""

    #: Deadline a fixture request carries when the fixture names none.
    deadline_s: int = DEFAULT_DEADLINE_S
    #: Slack allowed past the deadline before the timeout check fails.
    grace_s: float = DEFAULT_GRACE_S

    def __post_init__(self) -> None:
        if self.deadline_s < 1:
            raise ValueError("a conformance deadline must be at least one second")
        if self.grace_s < 0:
            raise ValueError("a conformance grace period cannot be negative")

    def run(
        self,
        candidate: Candidate,
        capability: str,
        *,
        fixtures: Sequence[ConformanceFixture] | None = None,
        root: Path | None = None,
    ) -> ConformanceReport:
        """Run *capability*'s suite against *candidate*.

        Fixture documents are materialised on disk because a request carries
        artifact *locations*: a provider is a separate program with its own
        working directory, and handing it text instead of paths would be testing
        a call it will never receive.
        """
        require_delegable(capability)
        suite = tuple(fixtures) if fixtures is not None else suite_for(capability)
        declared_fixtures = tuple(fixture.name for fixture in suite)
        declared_checks = tuple(
            check for check in CHECK_CLASSES if any(check in fx.checks for fx in suite)
        )
        if root is not None:
            results = self._run_all(candidate, suite, root)
        else:
            with tempfile.TemporaryDirectory(prefix="spec-engine-conformance-") as temporary:
                results = self._run_all(candidate, suite, Path(temporary))
        return ConformanceReport(
            capability=capability,
            candidate=candidate.name,
            declared_fixtures=declared_fixtures,
            declared_checks=declared_checks,
            results=results,
        )

    def _run_all(
        self,
        candidate: Candidate,
        suite: Sequence[ConformanceFixture],
        root: Path,
    ) -> tuple[CheckResult, ...]:
        results: list[CheckResult] = []
        for fixture in suite:
            results.extend(self._run_fixture(candidate, fixture, root))
        return tuple(results)

    # --- one fixture -------------------------------------------------------

    def _run_fixture(
        self,
        candidate: Candidate,
        fixture: ConformanceFixture,
        root: Path,
    ) -> tuple[CheckResult, ...]:
        request = self._request(fixture, root)
        # The runner's own request is validated against the published request
        # schema before it is sent. A fixture that asks a question the contract
        # does not describe would produce a failure report about the runner.
        schema_for(fixture.capability, REQUEST, request.schema_version).validate(request.to_wire())
        attempt = self._invoke(candidate, request)
        results: list[CheckResult] = []
        if CHECK_TIMEOUT_HONORING in fixture.checks:
            results.append(self._timeout_result(fixture, request, attempt))
        if attempt.raised:
            # A candidate that raised said nothing, so every payload-derived
            # check fails for one reason rather than several derived from a
            # payload that does not exist.
            reason = f"the candidate raised {attempt.error}"
            results.extend(
                CheckResult(check, fixture.name, False, reason)
                for check in fixture.checks
                if check != CHECK_TIMEOUT_HONORING
            )
            return tuple(results)
        errors = validate_response(fixture.capability, attempt.payload)
        if CHECK_SCHEMA_VALIDITY in fixture.checks:
            results.append(self._schema_result(fixture, errors))
        if errors:
            reason = (
                "the response does not satisfy the published schema, so nothing "
                "in it can be read"
            )
            results.extend(
                CheckResult(check, fixture.name, False, reason)
                for check in fixture.checks
                if check not in (CHECK_SCHEMA_VALIDITY, CHECK_TIMEOUT_HONORING)
            )
            return tuple(results)
        payload = attempt.payload
        if CHECK_DECLARED_COVERAGE in fixture.checks:
            results.append(self._coverage_result(fixture, payload))
        if CHECK_PLANTED_DEFECT in fixture.checks:
            results.append(self._defect_result(fixture, payload))
        if CHECK_REPEATABILITY in fixture.checks:
            results.append(self._repeatability_result(candidate, fixture, request, payload))
        return tuple(results)

    def _request(self, fixture: ConformanceFixture, root: Path) -> CapabilityRequest:
        directory = root / fixture.name
        directory.mkdir(parents=True, exist_ok=True)
        artifacts: list[ArtifactRef] = []
        for kind in FIXTURE_FILENAMES:
            text = fixture.documents.get(kind)
            if text is None:
                continue
            path = directory / FIXTURE_FILENAMES[kind]
            path.write_text(text, encoding="utf-8")
            artifacts.append(ArtifactRef.of(kind, path))
        return CapabilityRequest(
            capability=fixture.capability,
            spec_type=fixture.spec_type,
            artifacts=tuple(artifacts),
            parameters=dict(fixture.parameters),
            deadline_s=fixture.deadline_s or self.deadline_s,
        )

    def _invoke(self, candidate: Candidate, request: CapabilityRequest) -> _Attempt:
        started = time.monotonic()
        try:
            payload = candidate.respond(request)
        except Exception as exc:  # noqa: BLE001 - a candidate is untrusted code
            # Broad on purpose: a candidate that crashes has failed its suite,
            # and a traceback out of the runner would be reported as the
            # runner's own defect.
            return _Attempt(elapsed_s=time.monotonic() - started, error=exc.__class__.__name__)
        return _Attempt(payload=payload, elapsed_s=time.monotonic() - started)

    # --- the assertion classes ---------------------------------------------

    def _timeout_result(
        self,
        fixture: ConformanceFixture,
        request: CapabilityRequest,
        attempt: _Attempt,
    ) -> CheckResult:
        ceiling = request.deadline_s + self.grace_s
        within = attempt.elapsed_s <= ceiling
        detail = (
            f"answered in {attempt.elapsed_s:.3f}s against a {request.deadline_s}s "
            f"deadline (+{self.grace_s:g}s grace)"
        )
        if not within:
            detail = "ignored its deadline: " + detail
        return CheckResult(CHECK_TIMEOUT_HONORING, fixture.name, within, detail)

    def _schema_result(self, fixture: ConformanceFixture, errors: Sequence[Any]) -> CheckResult:
        if not errors:
            return CheckResult(
                CHECK_SCHEMA_VALIDITY,
                fixture.name,
                True,
                "the response satisfies the published response schema",
            )
        shown = "; ".join(str(error) for error in errors[:3])
        more = "" if len(errors) <= 3 else f" (and {len(errors) - 3} more)"
        return CheckResult(
            CHECK_SCHEMA_VALIDITY,
            fixture.name,
            False,
            f"the response fails the published schema: {shown}{more}",
        )

    def _coverage_result(self, fixture: ConformanceFixture, payload: Any) -> CheckResult:
        processed, skipped = _coverage_of(payload)
        problems: list[str] = []
        if not processed and not skipped:
            problems.append(
                "the coverage block declares nothing processed and nothing skipped, "
                "so the response cannot distinguish nothing-wrong from nothing-examined"
            )
        skipped_items = {item for item, _ in skipped}
        overlap = sorted(set(processed) & skipped_items)
        if overlap:
            named = ", ".join(sanitized(item, limit=64) for item in overlap[:5])
            problems.append(f"declares both processed and skipped for: {named}")
        unexplained = sorted(item for item, reason in skipped if not reason.strip())
        if unexplained:
            named = ", ".join(sanitized(item, limit=64) for item in unexplained[:5])
            problems.append(f"declares items skipped with no reason: {named}")
        if problems:
            return CheckResult(CHECK_DECLARED_COVERAGE, fixture.name, False, "; ".join(problems))
        return CheckResult(
            CHECK_DECLARED_COVERAGE,
            fixture.name,
            True,
            f"declared {len(processed)} processed and {len(skipped)} skipped, each with a reason",
        )

    def _defect_result(self, fixture: ConformanceFixture, payload: Any) -> CheckResult:
        if not fixture.planted:
            # Reaching here would mean a suite asked for detection against a
            # fixture with nothing planted, which is a vacuous pass by
            # construction. Reported as a failure of the suite, not of the
            # candidate.
            return CheckResult(
                CHECK_PLANTED_DEFECT,
                fixture.name,
                False,
                "the fixture declares no planted defect, so detection cannot be judged",
            )
        _, skipped = _coverage_of(payload)
        skipped_items = tuple(item for item, _ in skipped)
        detected: list[PlantedDefect] = []
        excused: list[PlantedDefect] = []
        missed: list[PlantedDefect] = []
        for defect in fixture.planted:
            if _finding_references(payload, defect.refs):
                detected.append(defect)
            elif _declares_skipped(skipped_items, defect.artifact):
                excused.append(defect)
            else:
                missed.append(defect)
        if missed:
            names = "; ".join(defect.label for defect in missed)
            artifacts = ", ".join(dict.fromkeys(defect.artifact for defect in missed))
            return CheckResult(
                CHECK_PLANTED_DEFECT,
                fixture.name,
                False,
                (
                    f"reported no finding referencing the planted defect and did not "
                    f"declare {artifacts} skipped: {names}"
                ),
            )
        parts = []
        if detected:
            parts.append(f"detected {len(detected)}")
        if excused:
            parts.append(f"declared skipped rather than examined for {len(excused)}")
        return CheckResult(
            CHECK_PLANTED_DEFECT, fixture.name, True, ", ".join(parts) + " planted defect(s)"
        )

    def _repeatability_result(
        self,
        candidate: Candidate,
        fixture: ConformanceFixture,
        request: CapabilityRequest,
        first: Any,
    ) -> CheckResult:
        again = self._invoke(candidate, request)
        if again.raised:
            return CheckResult(
                CHECK_REPEATABILITY,
                fixture.name,
                False,
                f"the second call raised {again.error} where the first answered",
            )
        errors = validate_response(fixture.capability, again.payload)
        if errors:
            return CheckResult(
                CHECK_REPEATABILITY,
                fixture.name,
                False,
                "the second response fails the published schema where the first passed",
            )
        before = _fingerprint(first)
        after = _fingerprint(again.payload)
        if not _fingerprint_has_content(before):
            # The comparison would hold for a provider that answers nothing at
            # all, which is not evidence of repeatability. A conforming response
            # always declares coverage, so an empty fingerprint means the
            # provider said nothing rather than that it said the same thing
            # twice.
            return CheckResult(
                CHECK_REPEATABILITY,
                fixture.name,
                False,
                (
                    "the response carries neither findings nor coverage, so two "
                    "identical answers would compare equal without saying anything"
                ),
            )
        if before != after:
            return CheckResult(
                CHECK_REPEATABILITY,
                fixture.name,
                False,
                f"two identical calls differed in: {', '.join(_differences(before, after))}",
            )
        findings, processed, skipped, _ = before
        return CheckResult(
            CHECK_REPEATABILITY,
            fixture.name,
            True,
            (
                f"two identical calls agreed on {len(findings)} findings, "
                f"{len(processed)} processed and {len(skipped)} skipped items, "
                f"and the result body"
            ),
        )


def verify(
    candidate: Candidate,
    capability: str,
    *,
    deadline_s: int = DEFAULT_DEADLINE_S,
    grace_s: float = DEFAULT_GRACE_S,
    fixtures: Sequence[ConformanceFixture] | None = None,
    root: Path | None = None,
) -> ConformanceReport:
    """Run *capability*'s bundled suite against *candidate* with the defaults."""
    runner = ConformanceRunner(deadline_s=deadline_s, grace_s=grace_s)
    return runner.run(candidate, capability, fixtures=fixtures, root=root)


def verify_builtin(
    provider: BuiltinProvider,
    capability: str,
    **kwargs: Any,
) -> ConformanceReport:
    """Run *capability*'s suite against an in-process provider."""
    return verify(BuiltinCandidate(provider=provider), capability, **kwargs)


# --- payload reading -------------------------------------------------------
#
# Every reader below is defensive about shape even though it only ever sees a
# payload that passed schema validation. The repeatability path reads a second
# response before that response has been through the schema, and a reader that
# assumed structure would raise out of the runner instead of reporting a failure.


def _coverage_of(payload: Any) -> tuple[tuple[str, ...], tuple[tuple[str, str], ...]]:
    """The processed tokens and the (item, reason) pairs a payload declares."""
    if not isinstance(payload, Mapping):
        return ((), ())
    coverage = payload.get("coverage")
    if not isinstance(coverage, Mapping):
        return ((), ())
    raw_processed = coverage.get("processed", ())
    processed = (
        tuple(str(entry) for entry in raw_processed)
        if isinstance(raw_processed, (list, tuple))
        else ()
    )
    raw_skipped = coverage.get("skipped", ())
    skipped: list[tuple[str, str]] = []
    if isinstance(raw_skipped, (list, tuple)):
        for entry in raw_skipped:
            if isinstance(entry, Mapping):
                skipped.append((str(entry.get("item", "")), str(entry.get("reason", ""))))
    return processed, tuple(skipped)


def _declares_skipped(skipped_items: Iterable[str], artifact: str) -> bool:
    """Whether a coverage token names *artifact* as skipped.

    The last namespace segment is what is compared, so a provider is free to
    qualify its tokens: ``requirements`` and ``document:requirements`` both say
    the requirements document went unexamined.
    """
    for token in skipped_items:
        if token == artifact or token.rsplit(COVERAGE_NAMESPACE, 1)[-1] == artifact:
            return True
    return False


def _finding_references(payload: Any, refs: Sequence[str]) -> bool:
    """Whether any finding references one of *refs*."""
    if not isinstance(payload, Mapping):
        return False
    findings = payload.get("findings", ())
    if not isinstance(findings, (list, tuple)):
        return False
    wanted = frozenset(refs)
    for finding in findings:
        if not isinstance(finding, Mapping):
            continue
        reported = finding.get("refs", ())
        if not isinstance(reported, (list, tuple)):
            continue
        if wanted & {str(ref) for ref in reported}:
            return True
    return False


def _fingerprint(payload: Any) -> tuple[Any, ...]:
    """What two identical calls have to agree on.

    Findings, coverage, and the result body: everything the engine acts on.
    Declared cost is deliberately excluded, because a model-backed provider's
    spend legitimately varies between two calls while its answer does not, and a
    check that failed on that would push providers toward reporting nothing.

    Order is compared as given rather than sorted. A provider that returns the
    same set in a different order each time is not reproducible in the sense a
    reader needs: two runs produce two different documents, and every diff over
    them is noise.
    """
    findings: tuple[str, ...] = ()
    result = ""
    if isinstance(payload, Mapping):
        raw = payload.get("findings", ())
        if isinstance(raw, (list, tuple)):
            findings = tuple(_canonical(finding) for finding in raw)
        result = _canonical(payload.get("result", {}))
    processed, skipped = _coverage_of(payload)
    return (findings, processed, skipped, result)


def _fingerprint_has_content(fingerprint: tuple[Any, ...]) -> bool:
    findings, processed, skipped, _ = fingerprint
    return bool(findings or processed or skipped)


def _differences(before: tuple[Any, ...], after: tuple[Any, ...]) -> tuple[str, ...]:
    """Which components of two fingerprints disagree, named and not quoted.

    Names only: the values are provider-authored, and this string is printed and
    pasted around. A reader who needs the text has both responses.
    """
    names = ("findings", "processed coverage", "skipped coverage", "result body")
    return tuple(name for name, left, right in zip(names, before, after) if left != right)


def _canonical(value: Any) -> str:
    """A stable string for any decoded JSON value."""
    return json.dumps(value, sort_keys=True, default=str)
