"""The engine's own paths, registered as the builtin providers.

Every delegable capability already answers from a shipped default the moment a
registry exists (see :mod:`.providers`). This module registers the *deeper*
builtins over those defaults: the paths the engine itself runs when no external
provider is bound. Three of them are model-backed and one is deterministic, and
that difference is the whole reason the module is worth its own file.

**Authoring, review, and implementation are model-backed.** The engine authors a
document by seeding an agent turn, reaches a review verdict by seeding another,
and implements a leaf task by dispatching a subagent. Each spends credits inside
the run's budget and under the kill switch. Registering them as
:class:`~.contracts.ProviderNature.MODEL_BACKED` is not cosmetic: it is how a
surface tells an operator which capabilities cost money, and marking a turn-
seeding path deterministic would read "the checks found nothing" where the truth
is "a model reported nothing".

**The model catalog is deterministic.** It resolves the identifiers the host
advertises and returns them; no model is asked, and nothing is spent. It carries
a resolver seam rather than importing the host directly, so the set it reports is
the set the *session doing the asking* sees, which only the caller knows, and so
this module stays a leaf of the graph the way the analyzer does.

What this module does NOT do, deliberately:

* **Analysis** is registered elsewhere. The structural analyzer binds through
  :func:`..local_analyzer.register` when an :class:`~..analysis.AnalysisEngine`
  is constructed, and the model-backed semantic analyzer is its own capability
  with a depth ladder. Registering analysis here would give it two owners.
* **Watch sources** and **supplementary validation** keep their shipped defaults.
  Watch sources poll a configured command — deterministic, no model — and the
  app ships no supplementary spec-document validation rules on purpose, so the
  honest no-coverage default is the final answer for both rather than a
  placeholder.

The serve() call of a model-backed builtin is still free and synchronous: it
declares the engine path and no coverage of the delegated call, which is the
right answer when it is reached as a fallback for a broken external provider. The
turn that actually spends credits is dispatched by the orchestrator, not from
inside a capability response — the engine moved analysis onto a dispatched turn
precisely so the spend lands inside the run's budget, and every model-backed
builtin stays on that side of the line.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Mapping, Sequence

from .contracts import (
    CapabilityRequest,
    CapabilityResponse,
    Coverage,
    ProviderIdentity,
    ProviderNature,
)
from .providers import DeclaredSkipProvider, builtin_identity

logger = logging.getLogger(__name__)

#: Resolves the model identifiers the host advertises to the asking session.
#: A callable rather than an import so the catalog reports what the session doing
#: the asking sees, which is the only correct answer, and so this module does not
#: reach into the host from a leaf of the capability graph.
ModelResolver = Callable[[], Sequence[str]]

#: Provider names for the deeper builtins. Distinct from the shipped defaults'
#: ``engine-<capability>`` names so a surface — and a test — can tell the engine's
#: own path apart from the no-coverage placeholder it registers over.
AUTHORING_PROVIDER = "engine-authoring-turn"
REVIEW_PROVIDER = "engine-review-turn"
IMPLEMENTATION_PROVIDER = "engine-implementation-dispatch"
MODEL_CATALOG_PROVIDER = "engine-model-catalog-host"

#: What each model-backed builtin declares about the engine path it stands for.
#: Quoted in a coverage reason, so a reader learns where the work happens.
_AUTHORING_REASON = (
    "the engine authors documents by seeding an agent turn with the app's authoring "
    "guidance; native-format validation and the phase gate accept or reject the result"
)
_REVIEW_REASON = (
    "the engine reaches a review verdict by seeding a review turn on the review role's "
    "assignment and applying the review and test-quality criteria"
)
_IMPLEMENTATION_REASON = (
    "the engine implements a leaf task by dispatching a subagent per task in wave order "
    "under the run's retry policy"
)


def _model_backed(capability: str, provider_name: str, reason: str, result: Mapping[str, object]):
    """A model-backed engine-path builtin declaring its fallback answer."""
    return DeclaredSkipProvider(
        capability=capability,
        reason=reason,
        provider_name=provider_name,
        result=dict(result),
        nature=ProviderNature.MODEL_BACKED,
    )


@dataclass(frozen=True)
class HostModelCatalog:
    """The builtin model catalog provider: the identifiers the host advertises.

    Deterministic by construction — it asks a model for nothing and spends
    nothing — so its declared cost is zero. It reports what the host advertises
    through :attr:`resolver` and nothing it invented: a picker lists what the host
    serves, and a static list here would be the thing this capability exists to
    avoid. The resolver's output is coerced to a de-duplicated tuple of non-empty
    identifiers, because it crosses from the host into a schema-validated response
    and a blank or repeated id is neither a model nor an error worth failing on.
    """

    resolver: ModelResolver

    @property
    def identity(self) -> ProviderIdentity:
        return builtin_identity(MODEL_CATALOG_PROVIDER, nature=ProviderNature.DETERMINISTIC)

    def serve(self, request: CapabilityRequest) -> CapabilityResponse:
        models = _distinct_ids(self.resolver())
        return CapabilityResponse(
            capability="model_catalog",
            provider_name=MODEL_CATALOG_PROVIDER,
            # The host catalog is the one thing this provider processed. Declaring
            # it keeps a response that legitimately carries no findings from
            # reading as "nothing examined", and gives a repeatability check
            # something to compare.
            coverage=Coverage(processed=("model_catalog",)),
            findings=(),
            cost_credits=0.0,
            result={"models": list(models)},
        )


def _distinct_ids(values: Sequence[str]) -> tuple[str, ...]:
    """De-duplicate model identifiers, dropping blanks, preserving host order."""
    seen: dict[str, None] = {}
    for value in values:
        text = str(value).strip()
        if text:
            seen.setdefault(text, None)
    return tuple(seen)


def register_builtins(registry: object, *, model_resolver: ModelResolver) -> None:
    """Register the engine's own paths as the builtins for the deeper capabilities.

    Takes the registry structurally, the way :func:`..local_analyzer.register`
    does, so this module does not import the invocation path the providers it
    registers are reached through. Registers authoring, review, and
    implementation as model-backed engine-path builtins, and the model catalog as
    the deterministic host resolver; analysis, watch sources, and supplementary
    validation are left to their own owners.

    This is the construction that makes requirement "the UI identifies each
    builtin as deterministic or model-backed" true of a running engine: until it
    is called, those three capabilities resolve to the shipped deterministic
    no-coverage default, which mislabels a path that spends credits.
    """
    register_builtin = getattr(registry, "register_builtin")
    register_builtin(
        "authoring",
        _model_backed("authoring", AUTHORING_PROVIDER, _AUTHORING_REASON, {"documents": []}),
    )
    register_builtin(
        "review",
        _model_backed("review", REVIEW_PROVIDER, _REVIEW_REASON, {"verdict": "none"}),
    )
    register_builtin(
        "implementation",
        _model_backed(
            "implementation", IMPLEMENTATION_PROVIDER, _IMPLEMENTATION_REASON, {"tasks": []}
        ),
    )
    register_builtin("model_catalog", HostModelCatalog(resolver=model_resolver))
