"""Supplementary validation: a provider may add findings and nothing else.

Delegation is the point at which "rules as code" could quietly stop being true.
If a bound provider could mark an engine violation as resolved, lower its
severity, or answer a gate on the engine's behalf, then the validation the engine
guarantees would be whatever the last-configured provider agreed with — and an
operator who bound a permissive provider would have weakened the format contract
without editing a rule.

So extension here is additive **by construction** rather than by policy. The
engine's report is not merged, rewritten, or copied: it is held by reference, and
:attr:`SupplementedReport.engine` is the very object that was passed in. Provider
findings live in a separate field, so no existing code path that reads
``violations`` can be reached by them at all. The gate reads
:attr:`SupplementedReport.gate_ok`, which is defined as the engine report's own
verdict and has no term involving provider input.

That leaves provider findings doing what they are for: appearing beside the
engine's, marked as provider-authored, routed by the criteria they reference. A
display that wants everything calls :meth:`SupplementedReport.all_entries`, which
returns both kinds with each one's origin attached, because a reader deciding
what to fix needs to know which of the two is a rule and which is an opinion.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable, Sequence

from ..findings import Severity, ValidationReport, Violation
from .contracts import (
    CapabilityResult,
    FindingSeverity,
    ProviderFinding,
    ProviderIdentity,
    Untrusted,
)


class EntryOrigin(str, Enum):
    """Whether an entry came from the engine's rules or from a provider."""

    ENGINE = "engine"
    PROVIDER = "provider"


#: How a provider severity reads on a combined display. Provider findings never
#: reach the engine ``Severity`` enum: that type is what gates consume, and a
#: provider value converted into it would be one refactor away from being able to
#: fail a gate.
_DISPLAY_RANK: dict[FindingSeverity, int] = {
    FindingSeverity.ERROR: 0,
    FindingSeverity.WARNING: 1,
    FindingSeverity.INFO: 2,
}


@dataclass(frozen=True)
class SupplementaryFinding:
    """One provider finding, kept alongside the provider that reported it."""

    provider: ProviderIdentity
    finding: ProviderFinding
    #: Whether the result carrying this finding was a degraded fallback. A
    #: finding from a degraded call is still worth showing, and still worth
    #: labelling: it came from a different provider than the one configured.
    degraded: bool = False

    @property
    def refs(self) -> tuple[str, ...]:
        return self.finding.refs

    @property
    def severity(self) -> FindingSeverity:
        return self.finding.severity

    @property
    def message(self) -> Untrusted:
        return self.finding.message

    def to_json_object(self) -> dict[str, Any]:
        return {
            "origin": EntryOrigin.PROVIDER.value,
            "provider": self.provider.to_json_object(),
            "degraded": self.degraded,
            **self.finding.to_json_object(),
        }


@dataclass(frozen=True)
class DisplayEntry:
    """One row of a combined display: an engine violation or a provider finding."""

    origin: EntryOrigin
    #: Rule identifier for an engine violation, finding kind for a provider one.
    label: str
    severity: str
    #: Engine text is safe to render; provider text is wrapped, so a surface
    #: cannot render one where it meant the other by forgetting which is which.
    message: str
    refs: tuple[str, ...] = ()
    file: str = ""
    location: str = ""
    provider: str = ""

    @property
    def from_engine(self) -> bool:
        return self.origin is EntryOrigin.ENGINE


@dataclass(frozen=True)
class SupplementedReport:
    """An engine validation report plus the provider findings added beside it."""

    engine: ValidationReport
    supplementary: tuple[SupplementaryFinding, ...] = ()

    @property
    def gate_ok(self) -> bool:
        """Whether a phase gate may pass.

        Defined solely by the engine's report. Nothing a provider returns appears
        in this expression, which is what makes "a provider cannot open or close a
        gate" a property of the code rather than a rule someone has to remember.
        """
        return self.engine.ok

    @property
    def violations(self) -> tuple[Violation, ...]:
        """The engine's violations, unchanged and in their original order."""
        return self.engine.violations

    @property
    def errors(self) -> tuple[Violation, ...]:
        """The engine's blocking violations. Provider findings are never here."""
        return self.engine.errors

    def supplementary_for(self, ref: str) -> tuple[SupplementaryFinding, ...]:
        """Provider findings referencing acceptance criterion or task *ref*."""
        return tuple(finding for finding in self.supplementary if ref in finding.refs)

    def all_entries(self) -> tuple[DisplayEntry, ...]:
        """Engine violations first, then provider findings, for one display.

        Engine entries lead because they are the ones that decide a gate: a
        reader scanning the top of a list should be looking at what blocks them.
        """
        entries: list[DisplayEntry] = [
            DisplayEntry(
                origin=EntryOrigin.ENGINE,
                label=violation.rule,
                severity=violation.severity.value,
                message=violation.message,
                file=violation.file,
                location=str(violation.location),
            )
            for violation in self.engine.violations
        ]
        for supplementary in sorted(
            self.supplementary, key=lambda item: _DISPLAY_RANK[item.severity]
        ):
            entries.append(
                DisplayEntry(
                    origin=EntryOrigin.PROVIDER,
                    label=supplementary.finding.kind,
                    severity=supplementary.severity.value,
                    message=supplementary.message.for_display(),
                    refs=supplementary.refs,
                    provider=supplementary.provider.name,
                )
            )
        return tuple(entries)

    def to_json_object(self) -> dict[str, Any]:
        return {
            "gate_ok": self.gate_ok,
            "engine": {
                "violations": [
                    {
                        "file": violation.file,
                        "location": str(violation.location),
                        "rule": violation.rule,
                        "severity": violation.severity.value,
                        "message": violation.message,
                    }
                    for violation in self.engine.violations
                ]
            },
            "supplementary": [item.to_json_object() for item in self.supplementary],
        }


def supplement(
    report: ValidationReport,
    results: Iterable[CapabilityResult] | CapabilityResult | None = None,
) -> SupplementedReport:
    """Add the findings from *results* beside *report* without touching it.

    Accepts one result or several, because a project may bind more than one
    supplementary provider and the additive rule does not change with the count.
    """
    if results is None:
        collected: Sequence[CapabilityResult] = ()
    elif isinstance(results, CapabilityResult):
        collected = (results,)
    else:
        collected = tuple(results)
    supplementary: list[SupplementaryFinding] = []
    for result in collected:
        for finding in result.findings:
            supplementary.append(
                SupplementaryFinding(
                    provider=result.provider,
                    finding=finding,
                    degraded=result.degraded,
                )
            )
    return SupplementedReport(engine=report, supplementary=tuple(supplementary))


def engine_severities(report: ValidationReport) -> tuple[tuple[str, str, str], ...]:
    """A comparable fingerprint of a report's rules and severities.

    Exists for the tests that assert supplementation changed nothing about the
    engine's own findings — the claim is precise enough to deserve a precise
    comparison rather than an eyeballed one.
    """
    return tuple(
        (violation.file, violation.rule, violation.severity.value)
        for violation in report.violations
    )


def blocking_rules(report: ValidationReport) -> frozenset[str]:
    """Rule identifiers that block, taken from the engine's report alone."""
    return frozenset(
        violation.rule for violation in report.violations if violation.severity is Severity.ERROR
    )
