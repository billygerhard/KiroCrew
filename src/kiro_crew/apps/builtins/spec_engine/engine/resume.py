"""The authority a run resumes under: read off its row, never re-resolved.

A run is decided once. The dispatcher resolves the Autonomy_Policy for the
(source, spec type, submitter class) triple that produced the item, writes the
resulting rung to the run row's ``posture`` column, and the intake screener caps
that rung to authoring — on the same column — when it suspects the item's text of
prompt injection. Every later question about how far *that* run may go has an
answer already, and this module is where it is read back.

**Why it is read rather than re-asked.** Re-resolving the policy for a run that
already exists produces a *different* decision from the one the run was gated
under, and the difference is silent and always in the dangerous direction:

* A quarantined run resolves to its configured rung again, because the
  quarantine is recorded on the run, not in configuration. Requirement 25.4's
  "capped regardless of policy" would then hold for the first pass and fail on
  resume — exactly when never-screened text is already sitting in the spec.
* Configuration is live. An operator who widens a source's grid, or a defaults
  change that ships in an upgrade, would retroactively raise the authority of a
  run that was admitted under the narrower one.

**How the wrong source is kept unreachable.** :func:`request_execution_for_run`
accepts no decision, no level, and no policy: there is no parameter through
which a re-resolved :class:`~.autonomy.AutonomyPolicy` answer can enter, and this
module does not import that class, so it cannot ask. The authority is
reconstructed here and nowhere else, by :func:`authority_for`, from the row.

**Two persisted fields, and the narrower one wins.** The rung lives in the
``posture`` column and is mirrored into the ``autonomy`` detail key by both
writers (the dispatcher's ``RunSeed.detail()`` and the screener's quarantine).
When they disagree — a writer that updated one and not the other — the lower rung
is taken. A disagreement can then only narrow authority, never widen it, which
is the one direction a bookkeeping mistake here must never move.

An absent, unknown, or unparseable posture resolves to
:data:`~.autonomy.UNCONFIGURED_LEVEL`, with no declaration behind it, which is
the answer that reserves execution for a person.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .audit import AuditLog
from .autonomy import UNCONFIGURED_LEVEL, AutonomyDecision, AutonomyLevel
from .config import LEAST_TRUSTED_CLASS, SPEC_TYPES, SUBMITTER_CLASSES
from .phases import ExecutionOutcome, request_execution
from .runs import UnknownRun
from .state import RunRecord, SpecLock, SpecRef, StateStore

#: Run-row ``detail`` keys the persisted authority is carried in. Written by the
#: dispatcher's ``RunSeed.detail()`` and, for the quarantine, by the intake
#: screener; read here. This module owns the names because it is the reader that
#: turns them back into an authority, and a reader and a writer on two spellings
#: of one field is a hold that silently stops holding. ``test_resume_authority``
#: pins them against what the dispatcher actually writes rather than against this
#: comment.
DETAIL_AUTONOMY = "autonomy"
DETAIL_SCREENING_QUARANTINED = "screening_quarantined"
DETAIL_SPEC_TYPE = "spec_type"
DETAIL_SUBMITTER_CLASS = "submitter_class"

#: Declaration recorded behind a resumed run's authority. Deliberately not a
#: configuration path: naming ``sources.<x>.autonomy...`` here would tell an
#: audit reader that configuration was consulted, and the whole point is that it
#: was not. The declaration names the row the rung was read from.
POSTURE_DECLARATION = "runs.{run}.posture"

#: Declaration recorded when the intake screening quarantine is what caps the
#: run. Distinct from :data:`POSTURE_DECLARATION` so the refusal an operator
#: reads names the quarantine instead of leaving them to infer it from a rung.
QUARANTINE_DECLARATION = "runs.{run}.screening-quarantine"


@dataclass(frozen=True)
class ResumeAuthority:
    """The authority one existing run may act under, reconstructed from its row.

    ``decision`` is shaped like the dispatcher's resolved decision so every gate
    that already takes one takes this without a translation layer — which is the
    point, because a translation layer is a second place for the rung to change.
    """

    run_id: str
    decision: AutonomyDecision
    #: Whether intake screening is holding this run at authoring. Kept beside the
    #: rung rather than inferred from it: authoring is also an ordinary
    #: configured rung, and a surface explaining the hold needs to tell them apart.
    quarantined: bool
    #: The rung the ``posture`` column named, ``""`` when it named none.
    recorded_posture: str
    #: Whether the row's two authority fields disagreed and the lower one won.
    narrowed: bool

    @property
    def level(self) -> AutonomyLevel:
        return self.decision.level

    def to_json_object(self) -> dict[str, Any]:
        return {
            "run": self.run_id,
            "level": self.level.value,
            "declared_at": self.decision.declared_at,
            "quarantined": self.quarantined,
            "recorded_posture": self.recorded_posture,
            "narrowed": self.narrowed,
        }


def authority_for(record: RunRecord) -> ResumeAuthority:
    """Reconstruct the authority *record* was admitted under.

    The one reconstruction. A second one would be a second answer to "how far may
    this run go", and the two would disagree on exactly the rows that matter: a
    quarantined run, and a row whose two authority fields were written by
    different hands.
    """
    detail: Mapping[str, Any] = record.detail or {}
    quarantined = bool(detail.get(DETAIL_SCREENING_QUARANTINED))

    column = _level_or_none(record.posture)
    mirrored = _level_or_none(detail.get(DETAIL_AUTONOMY))
    recorded = [level for level in (column, mirrored) if level is not None]
    level = min(recorded, key=lambda rung: rung.rank) if recorded else UNCONFIGURED_LEVEL
    narrowed = len(recorded) == 2 and column is not mirrored

    declared_at = POSTURE_DECLARATION.format(run=record.run_id) if recorded else ""
    if quarantined:
        # The cap is applied here as well as at the column the screener wrote,
        # because "regardless of policy" must not rest on one field of one row
        # staying written. A row marked quarantined can name any rung it likes
        # and still authorize nothing beyond authoring.
        level = UNCONFIGURED_LEVEL
        declared_at = QUARANTINE_DECLARATION.format(run=record.run_id)

    return ResumeAuthority(
        run_id=record.run_id,
        decision=AutonomyDecision(
            level=level,
            source=record.source,
            spec_type=_known(detail.get(DETAIL_SPEC_TYPE), SPEC_TYPES, ""),
            submitter_class=_known(
                detail.get(DETAIL_SUBMITTER_CLASS), SUBMITTER_CLASSES, LEAST_TRUSTED_CLASS
            ),
            declared_at=declared_at,
        ),
        quarantined=quarantined,
        recorded_posture=column.value if column is not None else "",
        narrowed=narrowed,
    )


def request_execution_for_run(
    store: StateStore,
    ref: SpecRef,
    *,
    run: str,
    audit: AuditLog,
    user: str | None = None,
    spec_type: str | None = None,
    lock: SpecLock | None = None,
) -> ExecutionOutcome:
    """Ask the execution gate whether *run* may proceed, under its own authority.

    The gate for a run that already exists, and the only one such a caller
    should reach: it takes no ``decision``, so the authority cannot arrive from a
    fresh policy resolution, and the reconstruction it uses instead is
    :func:`authority_for`.

    Everything else is :func:`~.phases.request_execution`'s, unchanged — the
    executability questions, the human-initiator rule, and both audit records.
    A named *user* still starts execution at any rung, because releasing a run a
    person has looked at is the human action a quarantine is waiting for; with
    nobody asking, only a persisted rung reaching execution proceeds.
    """
    record = store.get_run(run)
    if record is None:
        raise UnknownRun(f"no such run: {run!r}")
    authority = authority_for(record)
    return request_execution(
        store,
        ref,
        decision=authority.decision,
        audit=audit,
        user=user,
        spec_type=spec_type,
        lock=lock,
    )


def _level_or_none(raw: Any) -> AutonomyLevel | None:
    """The rung *raw* names, or ``None`` when it names none the ladder knows.

    A hand-edited row, a column written by an older schema, and an absent value
    all land here, and all three mean the same thing: nothing on this row says
    how far the run may go. Substituting a rung would be inventing authority.
    """
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return AutonomyLevel(raw)
    except ValueError:
        return None


def _known(raw: Any, vocabulary: tuple[str, ...], fallback: str) -> str:
    """*raw* when the schema's vocabulary knows it, *fallback* otherwise.

    These two fields are descriptive rather than load-bearing — the rung is the
    authority — but a value outside the vocabulary would be a decision no
    resolver could have produced, and passing it on would put it in front of an
    operator as if it had been.
    """
    return raw if isinstance(raw, str) and raw in vocabulary else fallback
