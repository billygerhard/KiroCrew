"""Analysis capability wiring: one engine call, findings keyed to criteria.

This module is the analysis capability's engine-side entry point. It does not
resolve providers, validate responses, attribute cost, degrade to a fallback, or
write the audit record itself — the capability registry owns all of that behind
its one invocation path, and duplicating any of it here would be a second place
the guarantees could drift. What lives here is the part that is specific to
analysis rather than to capabilities in general:

* **Building the request.** Analysis needs the three native documents plus the
  sidecar located on disk, the spec's recorded type, and the format version, so
  a provider judges the document it was pointed at rather than one it inferred.
  Each artifact carries the content hash of the bytes at request time, so a
  decision taken on a finding can be checked against the document that finding
  was about.

* **Binding the local analyzer as the fallback.** Constructing an
  :class:`AnalysisEngine` registers the bundled :class:`~.local_analyzer.LocalAnalyzer`
  as the analysis builtin. That is what makes an unavailable, timed-out, or
  schema-invalid external provider degrade to real structural analysis rather
  than to the shipped no-coverage default. The registration is the construction
  that wires the fallback: without it, a broken provider would fall back to a
  response that declares no coverage and reports nothing.

* **Keying findings to acceptance criteria.** A provider's findings reference
  criteria by identifier, and those identifiers are attacker-controlled in the
  MCP-child case. The routing here keys a finding to a criterion only when the
  criterion is one the engine's own parse of ``requirements.md`` found, so a
  finding cannot conjure a criterion that does not exist, and the keys the
  Review_Queue surface renders are engine identifiers rather than provider text.
  A finding whose references resolve to no real criterion is surfaced unkeyed
  rather than dropped or attached to a criterion it named but the document does
  not contain.

The finding text stays untrusted throughout: it travels as :class:`~.contracts.Untrusted`
and reaches a surface only through the display path, so keying and rendering
never let provider prose forge the structure the engine authored around it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from . import local_analyzer, phases, spec_types
from .capabilities.contracts import (
    NATIVE_FORMAT_VERSION,
    ArtifactRef,
    CapabilityRequest,
    CapabilityResult,
    Coverage,
    Degradation,
    ProviderFinding,
    ProviderIdentity,
    SkippedItem,
)
from .capabilities.registry import CapabilityRegistry
from .documents import DocumentKind
from .state import SpecRef
from .structure import parse_requirements

logger = logging.getLogger(__name__)

#: The capability this module wires. Taken from the analyzer rather than restated
#: so the request the engine builds and the builtin it falls back to name the
#: same capability by one definition.
ANALYSIS_CAPABILITY = local_analyzer.CAPABILITY

#: Artifact kind for the native sidecar. Not a document kind — it records the
#: spec type and identity rather than spec content — so it is named here where
#: the request that carries it is built.
CONFIG_ARTIFACT_KIND = "config"


class AnalysisError(Exception):
    """Base class for analysis-wiring failures the engine raises rather than degrades."""


class SpecTypeUnrecorded(AnalysisError):
    """The spec carries no recorded type, so an analysis request cannot be built.

    A request the schema would accept needs a non-empty spec type, and the type
    decides the document plan a provider judges against. Guessing a default here
    would hand the provider a plan the spec never declared, so this is a refusal
    rather than a fallback — the same stance the phase gates take on an
    unrecorded type.
    """

    def __init__(self, ref: SpecRef) -> None:
        self.ref = ref
        super().__init__(
            f"spec {ref.name!r} has no recorded spec type; analysis is refused until one "
            "is recorded, because the type decides which document plan a provider judges"
        )


@dataclass(frozen=True)
class AnalysisReport:
    """The engine's answer for one analysis call, with findings keyed to criteria.

    Wraps the raw :class:`~.contracts.CapabilityResult` — which already carries
    provider identity, transport, declared coverage, degraded status, and cost —
    and adds the analysis-specific routing: which criterion each finding concerns.
    The keys of :attr:`by_criterion` are engine identifiers taken from the
    document, never provider-authored strings, so the map a surface renders
    cannot be made to show a criterion the requirements do not declare.
    """

    result: CapabilityResult
    #: Findings keyed to a real acceptance criterion, keyed by engine identifier.
    by_criterion: Mapping[str, tuple[ProviderFinding, ...]]
    #: Findings whose references resolved to no criterion the document declares.
    #: Surfaced rather than dropped: a finding the provider could not key is
    #: still a finding, and one that named a criterion the document lacks is
    #: reported as unkeyed rather than attached to an identifier that would
    #: forge a place in the document for it.
    unkeyed: tuple[ProviderFinding, ...]

    @property
    def capability(self) -> str:
        return self.result.capability

    @property
    def degraded(self) -> bool:
        """Whether the call fell back to the builtin analyzer."""
        return self.result.degraded

    @property
    def degradation(self) -> Degradation | None:
        """Why the call fell back, or ``None`` when a bound provider answered."""
        return self.result.degradation

    @property
    def provider(self) -> ProviderIdentity:
        """Identity of whoever answered — the external provider or the analyzer."""
        return self.result.provider

    @property
    def coverage(self) -> Coverage:
        return self.result.coverage

    @property
    def skipped(self) -> tuple[SkippedItem, ...]:
        """What the provider declared it did not process, surfaced not dropped."""
        return self.result.coverage.skipped

    @property
    def cost_credits(self) -> float:
        return self.result.cost_credits

    @property
    def findings(self) -> tuple[ProviderFinding, ...]:
        return self.result.findings

    def to_review_items(self) -> tuple[dict[str, Any], ...]:
        """Findings grouped by criterion for the Review_Queue surface to render.

        Each item's ``criterion`` is an engine identifier and each finding is
        rendered through :meth:`~.contracts.ProviderFinding.to_json_object`,
        which sanitizes the identifier-shaped fields and puts the prose through
        the display path. So neither the key of an item nor the body of a finding
        can carry provider text that forges the structure the engine built.
        Ordered by criterion so a re-render is stable.
        """
        items: list[dict[str, Any]] = []
        for criterion in sorted(self.by_criterion, key=_criterion_sort_key):
            findings = self.by_criterion[criterion]
            items.append(
                {
                    "criterion": criterion,
                    "findings": [finding.to_json_object() for finding in findings],
                }
            )
        return tuple(items)


def _criterion_sort_key(identifier: str) -> tuple[int, int, str]:
    """Order criteria numerically by requirement then criterion, then by text.

    An identifier that is not the ``N.M`` shape sorts last on the numeric keys
    and then by its own text, so a stable order survives an identifier the parse
    did not produce (which cannot happen for a key here, but the ordering does
    not depend on that being true)."""
    requirement, _, criterion = identifier.partition(".")
    try:
        return (int(requirement), int(criterion), identifier)
    except ValueError:
        return (1 << 30, 1 << 30, identifier)


def declared_criteria(ref: SpecRef) -> frozenset[str]:
    """Every acceptance-criterion identifier the spec's requirements declare.

    Read from disk and parsed with the same reader the validator uses, so the
    set a finding is keyed against is the set the engine recognises elsewhere. An
    absent or empty requirements document yields an empty set, which routes every
    finding as unkeyed rather than raising: a spec with no requirements to key
    against is a legitimate state, not an error in the analysis path.
    """
    text = phases.read_document(ref.spec_dir, DocumentKind.REQUIREMENTS)
    if text is None:
        return frozenset()
    index = parse_requirements(text)
    return frozenset(
        criterion.identifier for requirement in index for criterion in requirement.criteria
    )


def route_findings(result: CapabilityResult, criteria: frozenset[str]) -> AnalysisReport:
    """Key each finding to the criteria it concerns, using engine identifiers.

    A finding is keyed under every criterion in *criteria* that its references
    name, and the key stored is the engine identifier rather than the provider's
    reference string — equal by construction here, but taken from the trusted set
    so the map's keys are provably engine-authored. A finding that names no
    criterion the document declares is surfaced unkeyed.
    """
    by_criterion: dict[str, list[ProviderFinding]] = {}
    unkeyed: list[ProviderFinding] = []
    for finding in result.findings:
        refs = set(finding.refs)
        matched = [criterion for criterion in criteria if criterion in refs]
        if matched:
            for criterion in matched:
                by_criterion.setdefault(criterion, []).append(finding)
        else:
            unkeyed.append(finding)
    frozen = {criterion: tuple(findings) for criterion, findings in by_criterion.items()}
    return AnalysisReport(result=result, by_criterion=frozen, unkeyed=tuple(unkeyed))


class AnalysisEngine:
    """The one engine call for analysis, over the capability registry.

    Constructing it binds the bundled analyzer as the analysis builtin, so the
    registry's degrade-to-builtin path lands on real structural analysis. Every
    other guarantee — schema validation, cost attribution, the degraded marker,
    the audit record — is the registry's, reached through its single invocation
    path, so this class adds the analysis-specific request and the criterion
    keying and nothing that the registry already owns.
    """

    def __init__(self, registry: CapabilityRegistry) -> None:
        self._registry = registry
        # The construction that wires the fallback. Registering the analyzer as
        # the builtin is what makes a broken external provider degrade to
        # structural analysis rather than to the shipped no-coverage default.
        self._analyzer = local_analyzer.register(registry)

    @property
    def analyzer(self) -> local_analyzer.LocalAnalyzer:
        return self._analyzer

    def build_request(
        self,
        ref: SpecRef,
        *,
        run: str = "",
        parameters: Mapping[str, Any] | None = None,
    ) -> CapabilityRequest:
        """Build the analysis request for *ref*.

        Carries the location of every document that exists, the sidecar, the
        recorded spec type, and the format version. Each artifact's revision is
        the content hash of its bytes now, so a finding can later be checked
        against the document it was about. Raises :class:`SpecTypeUnrecorded`
        when the spec has no recorded type, because the request the schema
        accepts needs one and the type decides the plan a provider judges.
        """
        spec_type = phases.recorded_spec_type(ref)
        if spec_type is None:
            raise SpecTypeUnrecorded(ref)
        artifacts: list[ArtifactRef] = []
        for kind in DocumentKind:
            text = phases.read_document(ref.spec_dir, kind)
            if text is None:
                continue
            artifacts.append(
                ArtifactRef.of(
                    kind.value,
                    ref.spec_dir / kind.filename,
                    revision=phases.content_hash(text),
                )
            )
        sidecar_ref = _sidecar_artifact(ref.spec_dir)
        if sidecar_ref is not None:
            artifacts.append(sidecar_ref)
        return CapabilityRequest(
            capability=ANALYSIS_CAPABILITY,
            spec_type=spec_type,
            artifacts=tuple(artifacts),
            parameters=dict(parameters or {}),
            format_version=NATIVE_FORMAT_VERSION,
            run=run,
        )

    def analyze(
        self,
        ref: SpecRef,
        *,
        run: str = "",
        initiator: str | None = None,
        parameters: Mapping[str, Any] | None = None,
    ) -> AnalysisReport:
        """Analyze *ref* through the bound provider and key the findings.

        The registry validates the response, attributes any declared cost to the
        run, records the call in the audit log, and degrades to the bound builtin
        — the analyzer — on any provider failure. The criteria are read before
        the call so the keys come from the document the request described.
        """
        request = self.build_request(ref, run=run, parameters=parameters)
        criteria = declared_criteria(ref)
        result = self._registry.invoke(request, ref=ref, initiator=initiator)
        return route_findings(result, criteria)


def _sidecar_artifact(spec_dir: Path) -> ArtifactRef | None:
    """A ref to the ``.config.kiro`` sidecar, or ``None`` when it cannot be read.

    The sidecar records the spec type and identity the request also carries, so a
    provider can read it directly rather than trusting the request's copy. An
    unreadable sidecar is left out rather than raising: the recorded type has
    already been resolved by the time this runs, so a request without the sidecar
    artifact is still a complete request.
    """
    path = spec_dir / spec_types.SIDECAR_FILENAME
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    if not text.strip():
        return None
    return ArtifactRef.of(CONFIG_ARTIFACT_KIND, path, revision=phases.content_hash(text))
