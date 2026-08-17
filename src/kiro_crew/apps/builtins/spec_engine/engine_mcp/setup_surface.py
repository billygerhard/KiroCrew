"""The setup assistant's shape at the MCP boundary: envelopes, identity, refusals.

The Setup_Assistant is a two-step flow -- propose, then apply what the operator
approved -- and MCP is a stateless protocol. Bridging those two without inventing
server-side sessions is the whole job of this module, and it is done with a
content hash rather than a handle:

* :func:`plan_identity` hashes the canonical JSON of the three things that decide
  what a write does -- the project subject, the answers used, and the patch they
  produce. Two calls whose canonical inputs are equal produce the same
  ``plan_id``; any difference in subject, answers, or patch produces a different
  one.
* ``apply_setup`` recomputes the plan from the arguments it was handed and
  compares. A ``plan_id`` from a plan computed against a different project, a
  different answer set, or a project whose evidence has since changed does not
  match, so the apply refuses instead of writing something the caller never saw.

There is deliberately no stored plan. A server-side plan table would be state a
second process could not see, would need eviction, and would let an apply succeed
against a plan the caller can no longer read back -- and the hash answers the same
question with none of that.

Two refusals are declared here rather than in the engine: an absent approver and
a stale ``plan_id`` are properties of *this* boundary, not of the library. Both
subclass :class:`~..engine.setup.SetupApprovalRequired`, so an existing catch of
the engine's refusal keeps catching them, and the boundary can still tell them
apart to name the right refusal code.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..engine.autonomy import AUTONOMY_FIELD, AutonomyLevel
from ..engine.config.profiles import COST_PROFILE_PRESET_NAMES
from ..engine.config.schema import (
    SECTION_SOURCES,
    SECTION_WORKFLOW,
    WORKFLOW_STAGES_KEY,
)
from ..engine.delivery.workflow import workflow_presets
from ..engine.prerequisites import PrerequisiteReport
from ..engine.setup import (
    ASKED_SUBJECTS,
    CONFIRMED_LEVELS,
    Evidence,
    Inference,
    InferredSubjectRefused,
    PresetOffer,
    Question,
    SetupAnswers,
    SetupApprovalRequired,
    SetupPatch,
    SetupPlan,
    SetupResult,
)
from ..engine.watch.sources import POLL_KEY, watch_source_presets

__all__ = [
    "ApproverRequired",
    "REFUSAL_APPROVER_REQUIRED",
    "REFUSAL_CODES",
    "REFUSAL_INFERRED_SUBJECT",
    "REFUSAL_PLAN_STALE",
    "REFUSAL_SETUP_APPROVAL",
    "REFUSED_KEY",
    "SetupPlanEnvelope",
    "StalePlan",
    "answers_from_arguments",
    "apply_payload",
    "canonical_answers",
    "inspection_payload",
    "plan_envelope",
    "plan_identity",
    "preset_programs",
    "project_subject",
    "refusal_payload",
    "render_offer",
    "render_prerequisites",
    "render_question",
    "require_approver",
    "require_plan_identity",
]

#: Key naming the refusal in a structured refusal payload.
REFUSED_KEY = "refused"

#: Refusal code for an apply with no approver identity.
REFUSAL_APPROVER_REQUIRED = "approver-required"

#: Refusal code for an apply whose ``plan_id`` is not the one the same inputs
#: produce now.
REFUSAL_PLAN_STALE = "plan-stale"

#: Refusal code for the engine's own approval gates: an unchosen cost profile, an
#: unanswered rung, a rung confirmed above a declined one, a preset that was
#: never offered or whose inference was not approved.
REFUSAL_SETUP_APPROVAL = "setup-approval-required"

#: Refusal code for an inference claiming a subject that is asked, never inferred.
REFUSAL_INFERRED_SUBJECT = "inferred-subject-refused"


class ApproverRequired(SetupApprovalRequired):
    """Raised when an apply arrives without the human approver identity.

    A subclass rather than a distinct type: everything that already refuses to
    write on :class:`SetupApprovalRequired` must refuse on this too, and a
    separate hierarchy would need every such catch clause found and widened. The
    subclass exists only so the boundary can name ``approver-required``
    specifically instead of a generic approval refusal.
    """

    def __init__(self) -> None:
        super().__init__(
            "applying a setup plan requires a non-empty approver: the plan writes the "
            "commands a project runs unattended and the autonomy it runs them at, so the "
            "human who accepted it is named rather than implied by the call"
        )


class StalePlan(SetupApprovalRequired):
    """Raised when the recomputed plan does not match the ``plan_id`` supplied."""

    def __init__(self, supplied: str, recomputed: str) -> None:
        self.supplied = supplied
        self.recomputed = recomputed
        super().__init__(
            "the plan_id does not identify the plan these inputs produce now "
            f"(supplied {supplied!r}, recomputed {recomputed!r}): call plan_setup again, read "
            "the plan it returns, and apply that plan_id -- the project's evidence or the "
            "answers have changed since the plan being applied was computed"
        )


#: Refusal classes in most-derived-first order, paired with the code each earns.
#:
#: Order is load-bearing and the reason this is a tuple rather than a mapping:
#: :class:`ApproverRequired` and :class:`StalePlan` ARE
#: :class:`SetupApprovalRequired`, so a lookup that tested the base class first
#: would report every refusal as the generic one. Traced against the classes the
#: setup module actually raises: ``InferredSubjectRefused`` derives ``ValueError``
#: and ``SetupApprovalRequired`` derives ``PermissionError``, which share no
#: ancestor below ``Exception``, so no entry here shadows another.
REFUSAL_CODES: tuple[tuple[type[Exception], str], ...] = (
    (ApproverRequired, REFUSAL_APPROVER_REQUIRED),
    (StalePlan, REFUSAL_PLAN_STALE),
    (SetupApprovalRequired, REFUSAL_SETUP_APPROVAL),
    (InferredSubjectRefused, REFUSAL_INFERRED_SUBJECT),
)


def refusal_payload(exc: Exception) -> dict[str, Any] | None:
    """Return the structured refusal for *exc*, or ``None`` if it is not one.

    ``None`` rather than a catch-all payload: an exception this boundary does not
    recognise is a tool error, and dressing it as a refusal would report a broken
    setup path as a decision the engine made.
    """
    for refusal, code in REFUSAL_CODES:
        if isinstance(exc, refusal):
            return {
                REFUSED_KEY: code,
                "reason": type(exc).__name__,
                "message": str(exc),
            }
    return None


# --- plan identity ---------------------------------------------------------


def project_subject(plan: SetupPlan) -> dict[str, str]:
    """The project the plan is about, as the two facts that identify it.

    Both, not either: the same name at a different root is a different project to
    configure, and the same root under a different name writes a different
    document section.
    """
    return {"name": plan.project, "root": str(plan.root)}


def plan_identity(
    *,
    subject: Mapping[str, Any],
    answers_used: Mapping[str, Any],
    config_patch: Mapping[str, Any],
) -> str:
    """Return the SHA-256 ``plan_id`` over the canonical JSON of the plan inputs.

    Canonical means key-sorted and separator-fixed, so two structurally equal
    inputs hash equally regardless of the order a caller's JSON object arrived in.
    Nothing environmental is in the hash -- not a timestamp, not the PATH lookups
    the offer report ran -- because an identity that changed on its own would make
    every apply stale and the two-step flow unusable.
    """
    canonical = json.dumps(
        {
            "project_subject": subject,
            "answers_used": answers_used,
            "config_patch": config_patch,
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def require_approver(approver: str) -> str:
    """Return the stripped approver, refusing an absent or blank one."""
    identity = approver.strip()
    if not identity:
        raise ApproverRequired()
    return identity


def require_plan_identity(supplied: str, recomputed: str) -> None:
    """Refuse unless *supplied* is the identity the inputs produce now."""
    if supplied.strip() != recomputed:
        raise StalePlan(supplied.strip(), recomputed)


# --- envelopes -------------------------------------------------------------


@dataclass(frozen=True)
class SetupPlanEnvelope:
    """A computed plan, its identity, and what applying it would write.

    The envelope is what ``plan_setup`` returns and what ``apply_setup`` consumes
    the identity of. It carries the patch itself, not a summary: a caller
    approving a plan is approving argv the engine will execute, and an approval
    given against a summary is an approval of something else.
    """

    plan_id: str
    subject: Mapping[str, str]
    inferences: tuple[Inference, ...]
    answers_used: Mapping[str, Any]
    config_patch: Mapping[str, Any]
    written_paths: tuple[str, ...]
    warnings: tuple[str, ...] = ()

    def to_json_object(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "project": dict(self.subject),
            "inferences": [item.render() for item in self.inferences],
            "answers_used": dict(self.answers_used),
            "config_patch": dict(self.config_patch),
            "written_paths": list(self.written_paths),
            "warnings": list(self.warnings),
        }


def plan_envelope(
    plan: SetupPlan, answers: SetupAnswers, proposed: SetupPatch
) -> SetupPlanEnvelope:
    """Assemble the envelope for a plan whose patch has already been built."""
    subject = project_subject(plan)
    answers_used = canonical_answers(answers)
    return SetupPlanEnvelope(
        plan_id=plan_identity(
            subject=subject, answers_used=answers_used, config_patch=proposed.patch
        ),
        subject=subject,
        inferences=plan.inferences,
        answers_used=answers_used,
        config_patch=proposed.patch,
        written_paths=proposed.written_paths,
        warnings=proposed.notes,
    )


def canonical_answers(answers: SetupAnswers) -> dict[str, Any]:
    """The answers as the identity hashes them: ordered, spelled one way.

    ``approved_subjects`` is a set on the way in and is sorted here, because a set
    has no order and hashing its iteration order would make the same answers
    produce different identities in different processes.
    """
    return {
        "cost_profile": answers.cost_profile,
        "confirmations": {
            level.value: bool(answers.confirmations[level])
            for level in CONFIRMED_LEVELS
            if level in answers.confirmations
        },
        "approved_subjects": sorted(answers.approved_subjects),
        "workflow_preset": answers.workflow_preset,
        "watch_source": answers.watch_source,
    }


def answers_from_arguments(raw: Mapping[str, Any]) -> SetupAnswers:
    """Build :class:`SetupAnswers` from a tool call's ``answers`` object.

    Refuses an unknown autonomy level and an unknown cost profile name here, where
    the caller learns which names exist, rather than letting an unknown key be
    silently dropped into an unanswered rung the engine then refuses as "missing".
    Both refusals are ``ValueError``, which the server reports as invalid
    arguments: they are a malformed call, not a decision the operator has to make.
    """
    profile = str(raw.get("cost_profile") or "")
    if profile and profile not in COST_PROFILE_PRESET_NAMES:
        raise ValueError(
            f"unknown cost profile {profile!r}; bundled profiles are "
            f"{', '.join(COST_PROFILE_PRESET_NAMES)}"
        )

    raw_confirmations = raw.get("confirmations") or {}
    if not isinstance(raw_confirmations, Mapping):
        raise ValueError("answers.confirmations must be an object keyed by autonomy level")
    confirmations: dict[AutonomyLevel, bool] = {}
    for key, value in raw_confirmations.items():
        try:
            level = AutonomyLevel(str(key).strip().lower())
        except ValueError:
            raise ValueError(
                f"unknown autonomy level {key!r} in answers.confirmations; the levels confirmed "
                f"separately are {', '.join(item.value for item in CONFIRMED_LEVELS)}"
            ) from None
        if level not in CONFIRMED_LEVELS:
            raise ValueError(
                f"{level.value!r} is not confirmed separately: it neither spends beyond a prompt "
                "nor writes outside the spec directory, so there is nothing to confirm"
            )
        if not isinstance(value, bool):
            raise ValueError(
                f"answers.confirmations.{level.value} must be true or false: an unanswered rung "
                "is refused rather than read as either"
            )
        confirmations[level] = value

    raw_subjects = raw.get("approved_subjects") or []
    if not isinstance(raw_subjects, Sequence) or isinstance(raw_subjects, (str, bytes)):
        raise ValueError("answers.approved_subjects must be an array of inference subjects")
    subjects = frozenset(str(item) for item in raw_subjects)
    asked = sorted(subjects & ASKED_SUBJECTS)
    if asked:
        # The operator cannot "approve an inference" for a subject no inference may
        # carry. Refused here rather than ignored, because ignoring it would accept
        # an approval that reads as covering the autonomy grid and covers nothing.
        raise ValueError(
            f"{', '.join(asked)} cannot appear in answers.approved_subjects: these subjects are "
            "asked, never inferred, so they are answered in confirmations and cost_profile"
        )

    return SetupAnswers(
        cost_profile=profile,
        confirmations=confirmations,
        approved_subjects=subjects,
        workflow_preset=_optional_name(raw.get("workflow_preset")),
        watch_source=_optional_name(raw.get("watch_source")),
    )


def _optional_name(value: Any) -> str | None:
    """Return a selected preset name, or ``None`` for "no selection".

    An empty string is a selection of nothing, which the engine would refuse as a
    preset that was never offered. Reading it as absent is the honest translation
    of a JSON caller that spelled "unset" as ``""``.
    """
    if value is None:
        return None
    name = str(value).strip()
    return name or None


# --- inspection payload ----------------------------------------------------


def preset_programs(offer: PresetOffer) -> dict[str, Any]:
    """The commands *offer*'s bundled preset would run, and the programs in them.

    Read out of the same bundled tables the write copies from, so what a caller is
    shown before approving is what would land in configuration. A second table of
    "programs this preset needs" is how a preset comes to advertise a tool its own
    command does not run.
    """
    commands: list[dict[str, Any]] = []
    if offer.kind == SECTION_WORKFLOW:
        stages = workflow_presets(offer.name)[WORKFLOW_STAGES_KEY]
        for stage, argvs in stages.items():
            commands.extend({"stage": stage, "argv": list(argv)} for argv in argvs)
    elif offer.kind == SECTION_SOURCES:
        poll = watch_source_presets(offer.name)[POLL_KEY]
        commands.append({"stage": POLL_KEY, "argv": list(poll)})

    programs: list[str] = []
    for command in commands:
        argv = command["argv"]
        if argv and str(argv[0]) not in programs:
            programs.append(str(argv[0]))
    return {"programs": programs, "commands": commands}


def render_offer(offer: PresetOffer) -> dict[str, Any]:
    """One preset offer: what it is, what it would execute, and what it needs."""
    payload: dict[str, Any] = {
        "kind": offer.kind,
        "name": offer.name,
        "inference": offer.inference.render(),
        **preset_programs(offer),
        "prerequisites": render_prerequisites(offer.prerequisites),
    }
    definition = offer.definition
    if definition is not None:
        payload["definition"] = definition
        payload["copy_note"] = offer.copy_note
    return payload


def render_prerequisites(report: PrerequisiteReport) -> dict[str, Any]:
    """A prerequisite report as the engine's own per-check detail, plus the verdict."""
    return {
        "met": report.met,
        "checks": [check.detail() for check in report.checks],
        "unmet": [check.detail() for check in report.unmet],
    }


def render_question(question: Question) -> dict[str, Any]:
    return {
        "subject": question.subject,
        "prompt": question.prompt,
        "because": question.because,
        "options": list(question.options),
        # A question with no options is a yes/no confirmation. Said in the payload
        # rather than left to a caller to infer from an empty array, because the
        # caller that infers it wrong asks a human to pick from nothing.
        "answer_kind": "choice" if question.options else "confirmation",
    }


def render_evidence(subject: str, item: Evidence) -> dict[str, Any]:
    rendered = item.render()
    return {"subject": subject, **rendered}


def inspection_payload(plan: SetupPlan, *, root: Path) -> dict[str, Any]:
    """Everything the inspection tool returns for *plan*.

    ``evidence`` is the gathered file text as its own list, keyed by the subject it
    supported, as well as inside each inference: a caller that wants to show an
    operator "here is what was read" should not have to reassemble it from the
    inferences, and a caller checking one inference should not have to search a
    flat list.
    """
    return {
        "project": {"name": plan.project, "root": str(root)},
        "memory_consulted": plan.memory_consulted,
        "evidence": [
            render_evidence(inference.subject, item)
            for inference in plan.inferences
            for item in inference.evidence
        ],
        "inferences": [inference.render() for inference in plan.inferences],
        "questions": [render_question(question) for question in plan.questions],
        "offers": [render_offer(offer) for offer in plan.offers],
        "prerequisites": render_prerequisites(plan.prerequisites),
        # Named on the wire so a caller can tell "no inference was made" from "no
        # inference is allowed": the second is the answer for these subjects
        # forever, and a caller that retries expecting the first never stops.
        "asked_subjects": sorted(ASKED_SUBJECTS),
        "confirmed_levels": [level.value for level in CONFIRMED_LEVELS],
        "autonomy_field": AUTONOMY_FIELD,
    }


def apply_payload(
    envelope: SetupPlanEnvelope, result: SetupResult, approver: str
) -> dict[str, Any]:
    """What the apply tool returns: the identity applied, by whom, and the effect.

    Deliberately not the merged document. The document can hold values the
    Config_Store classifies as secret, and eliding those by key name is the
    configuration read tool's job; returning the whole document here would put an
    unelided copy on a path that never learned the classification. The patch and
    the written paths say what changed, which is what an approver needs to check.
    """
    return {
        "applied": True,
        "plan_id": envelope.plan_id,
        "approver": approver,
        "project": dict(envelope.subject),
        "written_paths": list(result.written_paths),
        "config_patch": dict(envelope.config_patch),
        "prerequisites": render_prerequisites(result.prerequisites),
        "notes": list(result.notes),
    }
