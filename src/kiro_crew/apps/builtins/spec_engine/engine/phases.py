"""Phase derivation, the advancement gate, and approval staleness.

Three ideas hold this module together.

**Phase is derived, never stored.** A spec's phase is a function of the
documents on disk and the approvals recorded for them. Asking where a spec sits
is therefore a read: :func:`derive_phase` opens files and reads approval rows and
writes nothing at all. A caller that could move a spec merely by asking about it
would make the phase a property of who looked last, and the state store says as
much about its ``phase`` column -- it is a cache of this derivation, never an
authority.

**The document plan comes from the sidecar, through one table.** Which gates a
spec has follows from its recorded type, and that type is read from the
``.config.kiro`` sidecar by :mod:`.spec_types`, whose plans this module does not
restate. Both the recorded type and the plan it implies are resolved there, so
the phase machine cannot come to a different answer about the same spec than
validation or authoring did.

**A refused advancement says why.** :func:`advance` returns every reason it
refused, and a reason caused by an invalid document carries the validator's own
rule identifiers. A gate that answers only "no" makes the caller guess, and an
agent that has to guess spends a turn per guess.

**An edit stales exactly the approvals it invalidates.** An approval records the
content hash of the document it approved, so an edit to that document is
detectable, and an edit to a different document is detectably irrelevant.
Approving requirements and design and then editing design must leave the
requirements approval standing; blanket invalidation would train users to
re-approve reflexively, which is the same as having no gate.

Advancement past a gate requires every gate up to it to be settled, so a stale
approval blocks advancement anywhere later in the plan rather than only at its
own gate. That is what makes re-approval genuinely required after an edit
instead of side-steppable by advancing from a later document.

**Who may approve depends on how the run is driven, and on nothing else.** An
interactive run's approvals come from an explicit user action, never from the
Autonomy_Policy and never as a side effect of the run proceeding: there is no
code path here that records an approval without a caller asking for one, and
:func:`advance` in particular only reads them. A headless run has no user to ask,
so the policy is the approver for the gates it covers and a human is required for
the gates it does not -- and a policy approval is recorded under the policy's own
identity rather than under a person's name, because an approval attributed to
someone who never looked is worse evidence than no approval at all.

What autonomy does *not* buy is a softer gate. Every approval, whoever it is
attributed to, goes through :func:`approve` and its validation, so an unattended
run cannot approve a document an interactive run would have been refused.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Iterator, Sequence

from . import spec_types
from .audit import AuditLog
from .autonomy import AutonomyDecision, AutonomyLevel
from .documents import DocumentKind
from .findings import ValidationReport
from .native_format import validate_document_text
from .state import ApprovalRecord, SpecLock, SpecRef, StateStore

logger = logging.getLogger(__name__)

#: Prefix on a stored content hash, so a stored value declares its own algorithm
#: and a future change can be told apart from the current one rather than
#: silently comparing unequal.
CONTENT_HASH_PREFIX = "sha256:"


class Phase(str, Enum):
    """Where a spec sits in its document plan.

    The document phases are named for their documents rather than for the work
    inside them, because a bugfix spec writes its bug analysis into
    ``requirements.md`` and its fix design into ``design.md``. One vocabulary
    that matches the filenames beats two that a reader has to map between.
    """

    #: No spec type is recorded, so there is no document plan to derive against.
    UNTYPED = "untyped"
    REQUIREMENTS = "requirements"
    DESIGN = "design"
    TASKS = "tasks"
    #: Every document in the plan is written and carries a live approval.
    READY = "ready"

    @classmethod
    def of(cls, kind: DocumentKind) -> "Phase":
        return cls(kind.value)


class RunMode(str, Enum):
    """How a run is driven, which is what decides who may approve its gates.

    This is not a second workflow. Both modes walk the same document plan and
    both are judged by the same validation; the mode only answers who is allowed
    to be the approver of record at a gate. An interactive run has a person
    present, so that person approves. A headless run has nobody present, so the
    Autonomy_Policy approves the gates it covers and the rest wait for a human.
    """

    INTERACTIVE = "interactive"
    HEADLESS = "headless"


# --- Refusal identifiers ---------------------------------------------------
# Stable, like the validator's rule identifiers: a driver routes on them, the
# diagnostic aggregator addresses conditions by them, and an audit entry quotes
# them. Renaming one is a breaking change.

#: The spec has no recorded type, so no document plan applies to it.
REASON_SPEC_TYPE_UNRECORDED = "phase.spec-type-unrecorded"
#: The named gate is not part of this spec type's document plan.
REASON_GATE_NOT_IN_PLAN = "phase.gate-not-in-plan"
#: A gate's document is absent, or present with no content.
REASON_DOCUMENT_MISSING = "phase.document-missing"
#: A gate's document fails native-format validation.
REASON_DOCUMENT_INVALID = "phase.document-invalid"
#: A gate carries no recorded approval.
REASON_APPROVAL_MISSING = "phase.approval-missing"
#: A gate's approval was invalidated by a later edit to its document.
REASON_APPROVAL_STALE = "phase.approval-stale"
#: Every gate in the plan is settled; there is no further phase to enter.
REASON_ALREADY_FINAL = "phase.already-final"
#: The gate needs an explicit human action: either the Autonomy_Policy does not
#: cover it, or the run is interactive and the policy is not an approver there.
REASON_HUMAN_REQUIRED = "phase.human-required"

# --- Audit event names -----------------------------------------------------

APPROVAL_RECORDED_EVENT = "spec.gate.approved"
APPROVAL_REFUSED_EVENT = "spec.gate.approval-refused"
APPROVAL_STALED_EVENT = "spec.gate.approval-staled"
PHASE_ADVANCED_EVENT = "spec.phase.advanced"
PHASE_ADVANCE_REFUSED_EVENT = "spec.phase.advance-refused"


def content_hash(text: str) -> str:
    """Hash a document's content for approval staleness.

    Line endings are normalised first: a checkout that rewrites newlines changes
    no words, and staling every approval on a project because the working tree
    was cloned on another platform would be a gate nobody trusts. Everything
    else is significant, including whitespace, because the engine cannot know
    which edits a reviewer would have waved through.
    """
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return CONTENT_HASH_PREFIX + hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def document_plan(spec_type: str | None) -> tuple[DocumentKind, ...]:
    """The ordered gates of *spec_type*, or an empty plan when it has none.

    The plans themselves live with the spec types, so there is one table of them
    in the engine rather than one per module that consults it. This is the phase
    machine's view of that table: it answers with an empty plan for a type it
    cannot place, because a caller only asking where a spec sits gets ``UNTYPED``
    rather than an exception.
    """
    parsed = spec_types.SpecType.parse(spec_type)
    if parsed is None:
        return ()
    return parsed.plan.kinds


def read_document(spec_dir: Path, kind: DocumentKind) -> str | None:
    """Return a document's text, or ``None`` when it is not there to read.

    A file that exists but holds only whitespace counts as absent. A touched
    placeholder is not a drafted document, and treating it as one would derive a
    phase past work that has not happened.
    """
    path = spec_dir / kind.filename
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        # Unreadable and absent lead to the same refusal, so a gate is not
        # weakened by conflating them; the log preserves the difference for
        # whoever has to explain it.
        logger.warning("could not read %s: %s", path, exc)
        return None
    return text if text.strip() else None


def recorded_spec_type(ref: SpecRef) -> str | None:
    """The spec's recorded type, read from the native ``.config.kiro`` sidecar.

    The sidecar is the record, so it is the only thing consulted here. The state
    store's ``spec_type`` column is a mirror kept for listing and reporting: when
    the two disagree -- the IDE or a user retyped the spec, and the row is left
    over from before -- reading the mirror would derive the wrong document plan,
    and with it the wrong gates and the wrong advancement decision.

    ``None`` means the spec has no type this engine can act on, which is what
    turns into a refusal rather than a guessed default. A row in the store does
    not rescue that: a plan the spec never declared is not a plan.
    """
    try:
        return spec_types.recorded_spec_type(ref.spec_dir).value
    except spec_types.SpecTypeUnrecorded as exc:
        logger.debug("no usable spec type for %s: %s", ref.spec_dir, exc.reason)
        return None


@dataclass(frozen=True)
class Reason:
    """One reason an operation was refused."""

    code: str
    message: str
    gate: str | None = None
    #: Validator rule identifiers behind this reason, when it came from a
    #: validation failure. Carried so a caller can act on the rules rather than
    #: parse the message.
    rule_ids: tuple[str, ...] = ()

    def to_json_object(self) -> dict[str, Any]:
        record: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.gate is not None:
            record["gate"] = self.gate
        if self.rule_ids:
            record["rule_ids"] = list(self.rule_ids)
        return record

    def __str__(self) -> str:
        where = f" [{self.gate}]" if self.gate else ""
        return f"{self.code}{where}: {self.message}"


@dataclass(frozen=True)
class GateState:
    """One document gate, as derived from disk and the approval table."""

    gate: str
    kind: DocumentKind
    path: Path
    present: bool
    content_hash: str | None
    approval: ApprovalRecord | None
    #: True only when an approval exists and no longer covers the document: it
    #: was flagged stale, or the document's hash has moved since. A gate nobody
    #: approved is not stale, it is unapproved.
    stale: bool

    @property
    def approved(self) -> bool:
        """Whether this gate carries an approval that still covers its document."""
        return self.approval is not None and not self.stale

    @property
    def settled(self) -> bool:
        """Whether the spec may treat this gate as behind it."""
        return self.present and self.approved

    def to_json_object(self) -> dict[str, Any]:
        approval = self.approval
        return {
            "gate": self.gate,
            "document": self.kind.filename,
            "present": self.present,
            "approved": self.approved,
            "stale": self.stale,
            "approver": approval.actor if approval else None,
            # Rendered beside the identity so a surface can say "approved by
            # policy" without teaching every driver to parse an actor string.
            "approver_kind": approver_kind(approval.actor) if approval else None,
            "approved_ts": approval.approved_ts if approval else None,
        }


@dataclass(frozen=True)
class PhaseState:
    """A spec's derived phase, with the gate detail the derivation used."""

    ref: SpecRef
    spec_type: str | None
    phase: Phase
    gates: tuple[GateState, ...]

    @property
    def is_final(self) -> bool:
        return self.phase is Phase.READY

    @property
    def is_untyped(self) -> bool:
        return self.phase is Phase.UNTYPED

    @property
    def current_gate(self) -> GateState | None:
        """The gate the spec is working on, or ``None`` when it has none left."""
        for gate in self.gates:
            if not gate.settled:
                return gate
        return None

    @property
    def stale_gates(self) -> tuple[str, ...]:
        """Names of gates whose approval no longer covers their document."""
        return tuple(gate.gate for gate in self.gates if gate.stale)

    def gate_named(self, name: str) -> GateState | None:
        for gate in self.gates:
            if gate.gate == name:
                return gate
        return None

    def to_json_object(self) -> dict[str, Any]:
        return {
            "project": self.ref.project,
            "name": self.ref.name,
            "spec_type": self.spec_type,
            "phase": self.phase.value,
            "gates": [gate.to_json_object() for gate in self.gates],
        }


@dataclass(frozen=True)
class ApprovalOutcome:
    """The result of recording one gate's approval."""

    ok: bool
    gate: str
    approval: ApprovalRecord | None = None
    reasons: tuple[Reason, ...] = ()
    report: ValidationReport | None = None

    @property
    def reason_codes(self) -> tuple[str, ...]:
        return tuple(reason.code for reason in self.reasons)

    @property
    def by_policy(self) -> bool:
        """Whether the recorded approval is attributed to the Autonomy_Policy."""
        return self.approval is not None and is_policy_actor(self.approval.actor)

    def to_json_object(self) -> dict[str, Any]:
        approval = self.approval
        return {
            "ok": self.ok,
            "gate": self.gate,
            "approver": approval.actor if approval else None,
            "approver_kind": approver_kind(approval.actor) if approval else None,
            "approved_ts": approval.approved_ts if approval else None,
            "reasons": [reason.to_json_object() for reason in self.reasons],
        }


@dataclass(frozen=True)
class AdvanceResult:
    """The result of an advancement attempt.

    ``reasons`` is complete rather than first-blocking: a caller repairing a
    spec wants the whole list, and one refusal per attempt costs a turn per
    defect.
    """

    ok: bool
    from_phase: Phase
    to_phase: Phase | None
    gate: str | None
    reasons: tuple[Reason, ...] = ()
    report: ValidationReport | None = None

    @property
    def reason_codes(self) -> tuple[str, ...]:
        return tuple(reason.code for reason in self.reasons)

    @property
    def rule_ids(self) -> tuple[str, ...]:
        """Validator rule identifiers across every reason, in order, deduplicated."""
        seen: dict[str, None] = {}
        for reason in self.reasons:
            for rule in reason.rule_ids:
                seen.setdefault(rule, None)
        return tuple(seen)

    def to_json_object(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "from_phase": self.from_phase.value,
            "to_phase": self.to_phase.value if self.to_phase else None,
            "gate": self.gate,
            "reasons": [reason.to_json_object() for reason in self.reasons],
        }


# --- Derivation ------------------------------------------------------------


def derive_phase(store: StateStore, ref: SpecRef, *, spec_type: str | None = None) -> PhaseState:
    """Derive *ref*'s phase from its documents and its recorded approvals.

    Read-only in the strong sense: nothing here writes a row, a file, or a cached
    phase. Staleness is computed by comparing each approval's recorded hash
    against the document as it is now, so an edit shows up immediately even
    before anything persists the flag.

    Validity is deliberately not an input. The phase says where the spec sits;
    whether its documents pass validation is asserted at the gate, by
    :func:`advance`, where a refusal can carry the violations.
    """
    resolved_type = spec_type if spec_type is not None else recorded_spec_type(ref)
    plan = document_plan(resolved_type)
    if not plan:
        return PhaseState(ref=ref, spec_type=resolved_type, phase=Phase.UNTYPED, gates=())

    approvals = {record.gate: record for record in store.list_approvals(ref)}
    spec_dir = ref.spec_dir
    gates: list[GateState] = []
    for kind in plan:
        text = read_document(spec_dir, kind)
        digest = content_hash(text) if text is not None else None
        approval = approvals.get(kind.value)
        gates.append(
            GateState(
                gate=kind.value,
                kind=kind,
                path=spec_dir / kind.filename,
                present=text is not None,
                content_hash=digest,
                approval=approval,
                stale=_is_stale(approval, digest),
            )
        )

    phase = next((Phase.of(gate.kind) for gate in gates if not gate.settled), Phase.READY)
    return PhaseState(ref=ref, spec_type=resolved_type, phase=phase, gates=tuple(gates))


def _is_stale(approval: ApprovalRecord | None, digest: str | None) -> bool:
    """Whether *approval* still covers the document hashing to *digest*.

    A persisted stale flag is sticky: once :func:`sync_staleness` or
    :func:`advance` has recorded that a document moved, reverting it to the exact
    bytes that were approved makes the hashes agree again but does not revive the
    approval, which was given without sight of the intervening edit. Re-approval
    is cheap next to a gate that can be reopened by a well-chosen undo.

    Stickiness reaches only as far as what was observed. An edit made and undone
    with no derivation persisting the flag in between leaves the document hashing
    to what was approved, and nothing here can tell that apart from a document
    nobody touched -- hash comparison is the only evidence there is.
    """
    if approval is None:
        return False
    if approval.stale:
        return True
    return approval.doc_hash != digest


def sync_staleness(
    store: StateStore,
    ref: SpecRef,
    *,
    spec_type: str | None = None,
    audit: AuditLog | None = None,
) -> tuple[str, ...]:
    """Persist the staleness that :func:`derive_phase` computes; return what changed.

    Exactly the gates whose own document moved are flagged. A gate with no
    recorded approval is left completely alone: an edit must not invent approval
    state for a gate nobody approved, and the state store's update touches no row
    when there is none.

    Correctness does not depend on this call -- derivation compares hashes live --
    so it is safe to skip and safe to repeat. It exists so the rows a queue or a
    dashboard reads agree with the disk. It also needs no lock: setting a flag
    that is already monotone is idempotent, and the per-spec lock is there to
    serialise conflicting state changes, not converging ones.
    """
    state = derive_phase(store, ref, spec_type=spec_type)
    changed: list[str] = []
    for gate in state.gates:
        approval = gate.approval
        if approval is None or approval.stale or not gate.stale:
            continue
        if store.set_approval_stale(ref, gate.gate):
            changed.append(gate.gate)
    if audit is not None:
        for gate_name in changed:
            audit.append(
                ref,
                APPROVAL_STALED_EVENT,
                detail={"gate": gate_name, "reason": REASON_APPROVAL_STALE},
            )
    return tuple(changed)


# --- Gate checks -----------------------------------------------------------


def _validate_gate(gate: GateState) -> ValidationReport | None:
    """Validate one gate's document, or ``None`` when there is nothing to read."""
    text = read_document(gate.path.parent, gate.kind)
    if text is None:
        return None
    return validate_document_text(text, kind=gate.kind, file=str(gate.path))


def _gate_reasons(gate: GateState) -> tuple[tuple[Reason, ...], ValidationReport | None]:
    """Every reason *gate* is not settled, with the report that produced any."""
    if not gate.present:
        return (
            (
                Reason(
                    code=REASON_DOCUMENT_MISSING,
                    gate=gate.gate,
                    message=f"{gate.kind.filename} has not been written yet.",
                ),
            ),
            None,
        )

    reasons: list[Reason] = []
    report = _validate_gate(gate)
    if report is not None and not report.ok:
        errors = report.errors
        reasons.append(
            Reason(
                code=REASON_DOCUMENT_INVALID,
                gate=gate.gate,
                message=(
                    f"{gate.kind.filename} fails native-format validation with "
                    f"{len(errors)} error{'s' if len(errors) != 1 else ''}."
                ),
                rule_ids=tuple(dict.fromkeys(violation.rule for violation in errors)),
            )
        )

    approval = gate.approval
    if approval is None:
        reasons.append(
            Reason(
                code=REASON_APPROVAL_MISSING,
                gate=gate.gate,
                message=f"No approval is recorded for {gate.gate}.",
            )
        )
    elif gate.stale:
        reasons.append(
            Reason(
                code=REASON_APPROVAL_STALE,
                gate=gate.gate,
                message=(
                    f"The {gate.gate} approval by {approval.actor} at "
                    f"{approval.approved_ts} no longer covers "
                    f"{gate.kind.filename}; it needs re-approval."
                ),
            )
        )
    return tuple(reasons), report


def blocking_reasons(
    state: PhaseState, gates: Sequence[GateState]
) -> tuple[tuple[Reason, ...], ValidationReport | None]:
    """Reasons *gates* are not all settled, oldest gate first.

    Every gate up to the target is checked, not just the target itself. That is
    what makes a stale approval block advancement anywhere later in the plan: a
    spec whose requirements were edited after approval must not slip past by
    advancing from its tasks document.
    """
    if state.is_untyped:
        return (
            (
                Reason(
                    code=REASON_SPEC_TYPE_UNRECORDED,
                    message=(
                        "No spec type is recorded for this spec, so it has no "
                        "document plan to advance through."
                    ),
                ),
            ),
            None,
        )
    reasons: list[Reason] = []
    first_report: ValidationReport | None = None
    for gate in gates:
        gate_reasons, report = _gate_reasons(gate)
        reasons.extend(gate_reasons)
        if first_report is None and report is not None and not report.ok:
            first_report = report
    return tuple(reasons), first_report


# --- Locking helper --------------------------------------------------------


@contextlib.contextmanager
def _held(store: StateStore, ref: SpecRef, lock: SpecLock | None, owner: str) -> Iterator[SpecLock]:
    """Hold *ref*'s lock, reusing one the caller already holds.

    The store's lock is deliberately not re-entrant, so an operation running
    inside a longer one must pass the handle it already has rather than take the
    lock a second time and be rejected by itself.
    """
    if lock is not None:
        store.verify_lock(lock)
        yield lock
        return
    with store.lock(ref, owner=owner) as handle:
        yield handle


# --- The policy as an approver ---------------------------------------------


#: Scheme on the approver identity recorded when the Autonomy_Policy is the
#: approver of record. A scheme rather than a name for two reasons: an operator
#: reading the audit trail can see at a glance that no person approved this gate,
#: and the colon makes the identity unmistakably not a username, so a policy
#: approval and a human approval can never be confused for one another in either
#: direction.
POLICY_ACTOR_SCHEME = "autonomy-policy"

#: What an approval is attributed to, recorded alongside the identity so a reader
#: does not have to know how to parse an actor string to tell the two apart.
APPROVER_USER = "user"
APPROVER_POLICY = "policy"

#: The autonomy rung each document gate stands in front of. Clearing the document
#: plan is what lets a run enter execution, so the policy covers those gates
#: exactly when it authorizes execution unattended. A gate absent from this table
#: is not policy-coverable at all: an unknown gate resolving to some rung would
#: mean a new gate silently inheriting authority from configuration written before
#: it existed.
_POLICY_GATE_LEVELS: dict[str, AutonomyLevel] = {
    kind.value: AutonomyLevel.EXECUTION for kind in DocumentKind
}


def policy_level_for_gate(gate: str) -> AutonomyLevel | None:
    """The autonomy level a policy must permit to approve *gate*, if any.

    ``None`` means no level covers this gate, so it always needs a human.
    """
    return _POLICY_GATE_LEVELS.get(gate)


def gate_is_policy_covered(decision: AutonomyDecision, gate: str) -> bool:
    """Whether *decision* authorizes the policy to approve *gate* unattended.

    An unconfigured triple resolves to authoring only, which covers no gate --
    so a source nobody configured produces a run that waits for a reviewer rather
    than one that approves itself.
    """
    needed = policy_level_for_gate(gate)
    return needed is not None and decision.permits(needed)


def policy_actor(decision: AutonomyDecision) -> str:
    """The approver identity for an approval attributed to *decision*.

    The identity carries the dotted config path the level was declared at, so the
    audit trail names the declaration that authorized the gate instead of leaving
    a reader to work out which of the policy grid's cells matched. An unconfigured
    decision has no declaration and covers no gate, so it has no identity here.
    """
    if not decision.is_configured:
        raise ValueError("an unconfigured autonomy decision cannot approve a gate")
    return f"{POLICY_ACTOR_SCHEME}:{decision.declared_at}"


def is_policy_actor(actor: str) -> bool:
    """Whether *actor* is the Autonomy_Policy rather than a person."""
    return actor.startswith(POLICY_ACTOR_SCHEME + ":")


def policy_declaration(actor: str) -> str | None:
    """The config path behind a policy approver, or ``None`` for a person."""
    if not is_policy_actor(actor):
        return None
    return actor[len(POLICY_ACTOR_SCHEME) + 1 :]


def approver_kind(actor: str) -> str:
    """Whether *actor* names a person or the policy."""
    return APPROVER_POLICY if is_policy_actor(actor) else APPROVER_USER


# --- Approval --------------------------------------------------------------


def approve(
    store: StateStore,
    ref: SpecRef,
    gate: str,
    *,
    actor: str,
    mode: RunMode | None = None,
    decision: AutonomyDecision | None = None,
    spec_type: str | None = None,
    lock: SpecLock | None = None,
    audit: AuditLog | None = None,
) -> ApprovalOutcome:
    """Record *actor*'s approval of one gate, with the hash of what was approved.

    The document is validated first and an invalid one is refused. Approval is
    what lets a phase move, so accepting an approval for a document that does not
    validate would advance a spec that the advancement gate would have stopped --
    the same hole from the other side. This is the only place an approval is
    written, so that check is unconditional: every mode and every approver reaches
    the gate through here, and none of them can reach a softer one.

    The recorded hash is the whole staleness mechanism: it is what a later edit
    is compared against, and what makes an unrelated edit provably unrelated.

    *actor* is a person's identity, or the Autonomy_Policy's own identity from
    :func:`policy_actor`. The policy's identity is accepted only against the
    ``decision`` that produced it, on a gate that decision covers, and never in an
    interactive run -- so no caller can attribute an approval to a policy that did
    not authorize it. *mode* is recorded for the audit trail; the checks it drives
    are the ones above.
    """
    if not actor:
        raise ValueError("an approval needs an actor")
    if is_policy_actor(actor):
        refusal = _policy_actor_refusal(gate, actor, mode=mode, decision=decision)
        if refusal is not None:
            return _refuse_approval(ref, gate, (refusal,), actor=actor, mode=mode, audit=audit)
    with _held(store, ref, lock, owner=actor) as handle:
        state = derive_phase(store, ref, spec_type=spec_type)
        if state.is_untyped:
            return _refuse_approval(
                ref,
                gate,
                (
                    Reason(
                        code=REASON_SPEC_TYPE_UNRECORDED,
                        message=(
                            "No spec type is recorded for this spec, so "
                            f"{gate!r} is not a gate it has."
                        ),
                    ),
                ),
                actor=actor,
                mode=mode,
                audit=audit,
            )

        target = state.gate_named(gate)
        if target is None:
            planned = ", ".join(item.gate for item in state.gates)
            return _refuse_approval(
                ref,
                gate,
                (
                    Reason(
                        code=REASON_GATE_NOT_IN_PLAN,
                        gate=gate,
                        message=(
                            f"A {state.spec_type} spec has no {gate!r} gate; "
                            f"its gates are: {planned}."
                        ),
                    ),
                ),
                actor=actor,
                mode=mode,
                audit=audit,
            )

        if not target.present:
            return _refuse_approval(
                ref,
                gate,
                (
                    Reason(
                        code=REASON_DOCUMENT_MISSING,
                        gate=gate,
                        message=(
                            f"{target.kind.filename} has not been written yet, "
                            "so there is nothing to approve."
                        ),
                    ),
                ),
                actor=actor,
                mode=mode,
                audit=audit,
            )

        report = _validate_gate(target)
        if report is not None and not report.ok:
            errors = report.errors
            return _refuse_approval(
                ref,
                gate,
                (
                    Reason(
                        code=REASON_DOCUMENT_INVALID,
                        gate=gate,
                        message=(
                            f"{target.kind.filename} fails native-format "
                            f"validation with {len(errors)} "
                            f"error{'s' if len(errors) != 1 else ''}; "
                            "it cannot be approved as it stands."
                        ),
                        rule_ids=tuple(dict.fromkeys(v.rule for v in errors)),
                    ),
                ),
                actor=actor,
                mode=mode,
                audit=audit,
                report=report,
            )

        digest = target.content_hash
        if digest is None:  # pragma: no cover - present implies a hash
            raise RuntimeError(f"gate {gate!r} is present but carries no content hash")

        # Validation read the document again, so re-confirm the lock before the
        # write: a lock that expired underneath a slow check must not persist an
        # approval that a second writer has already superseded.
        store.verify_lock(handle)
        record = store.record_approval(ref, gate=gate, actor=actor, doc_hash=digest)

    if audit is not None:
        detail: dict[str, Any] = {
            "gate": gate,
            "document": target.kind.filename,
            "content_hash": digest,
            "approved_ts": record.approved_ts,
            "approver": approver_kind(actor),
        }
        detail.update(_context_detail(mode, actor))
        audit.append(ref, APPROVAL_RECORDED_EVENT, initiator=actor, detail=detail)
    return ApprovalOutcome(ok=True, gate=gate, approval=record, report=report)


def _policy_actor_refusal(
    gate: str,
    actor: str,
    *,
    mode: RunMode | None,
    decision: AutonomyDecision | None,
) -> Reason | None:
    """Why the policy may not approve *gate* as *actor*, or ``None`` when it may.

    Three ways this refuses, and each closes a route by which an approval could be
    credited to a policy that never authorized it: the run is interactive, where a
    present user is the only approver; no decision was supplied, so there is
    nothing to check the claim against; or the decision does not reach this gate,
    either because it names another declaration or because its level does not
    cover the gate.
    """
    if mode is RunMode.INTERACTIVE:
        return Reason(
            code=REASON_HUMAN_REQUIRED,
            gate=gate,
            message=(
                "This run is interactive, so its approvals come from an explicit "
                "user action; the Autonomy_Policy is not an approver here."
            ),
        )
    if decision is None or actor != policy_actor(decision):
        return Reason(
            code=REASON_HUMAN_REQUIRED,
            gate=gate,
            message=(
                f"{actor!r} claims the Autonomy_Policy as approver without the "
                "decision that authorizes it, so this gate needs a human."
            ),
        )
    if not gate_is_policy_covered(decision, gate):
        return _uncovered_gate_reason(gate, decision)
    return None


def _uncovered_gate_reason(gate: str, decision: AutonomyDecision) -> Reason:
    """The refusal for a gate the resolved policy does not cover."""
    needed = policy_level_for_gate(gate)
    if needed is None:
        return Reason(
            code=REASON_HUMAN_REQUIRED,
            gate=gate,
            message=(
                f"The {gate!r} gate is not one the Autonomy_Policy can approve; "
                "it needs an explicit human action."
            ),
        )
    where = decision.declared_at or "nothing configured for this run"
    return Reason(
        code=REASON_HUMAN_REQUIRED,
        gate=gate,
        message=(
            f"The Autonomy_Policy resolves to {decision.level.value!r} "
            f"({where}), which does not authorize {needed.value!r}, so the "
            f"{gate!r} gate needs an explicit human action."
        ),
    )


def _context_detail(mode: RunMode | None, actor: str) -> dict[str, Any]:
    """Audit fields describing how a run was driven and who approved."""
    detail: dict[str, Any] = {}
    if mode is not None:
        detail["mode"] = mode.value
    declaration = policy_declaration(actor)
    if declaration is not None:
        detail["policy_declaration"] = declaration
    return detail


def approve_interactive(
    store: StateStore,
    ref: SpecRef,
    gate: str,
    *,
    user: str,
    spec_type: str | None = None,
    lock: SpecLock | None = None,
    audit: AuditLog | None = None,
) -> ApprovalOutcome:
    """Record a gate approval from an explicit user action in an interactive run.

    Every approval in an interactive run arrives this way, which is what makes
    "only from an explicit user action" true rather than intended: nothing else in
    the engine records an approval, and this needs a named person to record one.
    The policy is refused here even when it would cover the gate -- a user is
    present, and asking them is the point of an interactive run.
    """
    return approve(
        store,
        ref,
        gate,
        actor=user,
        mode=RunMode.INTERACTIVE,
        spec_type=spec_type,
        lock=lock,
        audit=audit,
    )


def approve_by_policy(
    store: StateStore,
    ref: SpecRef,
    gate: str,
    *,
    decision: AutonomyDecision,
    spec_type: str | None = None,
    lock: SpecLock | None = None,
    audit: AuditLog | None = None,
) -> ApprovalOutcome:
    """Record a headless run's gate approval from the Autonomy_Policy.

    A gate the resolved level covers is approved under the policy's own identity.
    A gate it does not cover records nothing and is refused with
    ``REASON_HUMAN_REQUIRED``, which is the signal a driver turns into a queued
    review and a notification: the run stops here until a person acts, and the
    absence of an approval row is what keeps :func:`advance` stopped too.
    """
    if not gate_is_policy_covered(decision, gate):
        return _refuse_approval(
            ref,
            gate,
            (_uncovered_gate_reason(gate, decision),),
            actor=_unauthorized_policy_initiator(decision),
            mode=RunMode.HEADLESS,
            audit=audit,
        )
    return approve(
        store,
        ref,
        gate,
        actor=policy_actor(decision),
        mode=RunMode.HEADLESS,
        decision=decision,
        spec_type=spec_type,
        lock=lock,
        audit=audit,
    )


def _unauthorized_policy_initiator(decision: AutonomyDecision) -> str:
    """The initiator to audit for a policy that was asked and had no authority.

    A configured-but-insufficient level is named by its declaration, so the entry
    points at what would have to change. An unconfigured decision has no
    declaration to name, and must not borrow the approver scheme: it approved
    nothing, and an audit reader scanning for policy approvals should not find it.
    """
    if decision.is_configured:
        return policy_actor(decision)
    return f"{POLICY_ACTOR_SCHEME}(unconfigured)"


def approve_for_run(
    store: StateStore,
    ref: SpecRef,
    gate: str,
    *,
    mode: RunMode,
    user: str | None = None,
    decision: AutonomyDecision | None = None,
    spec_type: str | None = None,
    lock: SpecLock | None = None,
    audit: AuditLog | None = None,
) -> ApprovalOutcome:
    """Record a gate approval for a run being driven in *mode*.

    The mode table in one place, for a driver that holds a run rather than a
    known approver:

    * interactive -- *user* is required and is the only approver. A caller passing
      a policy decision instead is a bug in the driver, not a refusal to report to
      an operator, so it raises.
    * headless with a *user* -- a reviewer acting on a gate the policy left for a
      human. Recorded as that person, because that is who looked.
    * headless without a *user* -- the policy approves the gates it covers and
      refuses the rest.
    """
    if mode is RunMode.INTERACTIVE:
        if decision is not None:
            raise ValueError("an interactive run's approvals come from a user, not the policy")
        if not user:
            raise ValueError("an interactive approval needs the approving user")
        return approve_interactive(
            store, ref, gate, user=user, spec_type=spec_type, lock=lock, audit=audit
        )
    if user:
        return approve(
            store,
            ref,
            gate,
            actor=user,
            mode=RunMode.HEADLESS,
            spec_type=spec_type,
            lock=lock,
            audit=audit,
        )
    if decision is None:
        raise ValueError(
            "a headless approval needs either the approving user or an autonomy decision"
        )
    return approve_by_policy(
        store, ref, gate, decision=decision, spec_type=spec_type, lock=lock, audit=audit
    )


def _refuse_approval(
    ref: SpecRef,
    gate: str,
    reasons: tuple[Reason, ...],
    *,
    actor: str,
    audit: AuditLog | None,
    mode: RunMode | None = None,
    report: ValidationReport | None = None,
) -> ApprovalOutcome:
    if audit is not None:
        detail: dict[str, Any] = {
            "gate": gate,
            "reasons": [reason.to_json_object() for reason in reasons],
        }
        detail.update(_context_detail(mode, actor))
        audit.append(ref, APPROVAL_REFUSED_EVENT, initiator=actor, detail=detail)
    return ApprovalOutcome(ok=False, gate=gate, reasons=reasons, report=report)


# --- Advancement -----------------------------------------------------------


def advance(
    store: StateStore,
    ref: SpecRef,
    *,
    actor: str,
    gate: str | None = None,
    spec_type: str | None = None,
    lock: SpecLock | None = None,
    audit: AuditLog | None = None,
) -> AdvanceResult:
    """Ask whether *ref* may move past a gate, and record the answer.

    ``gate`` names the gate being left. It defaults to the last document in the
    plan that is written, which is the document whoever calls this has just
    finished: the answer then reads as "you may now author the next one". With no
    name on a spec whose every gate is settled there is no phase left to enter,
    which is reported rather than answered with a phase that does not exist.

    Passing a gate is not the same question as passing the plan: advancement past
    a gate requires every gate up to it to be settled, so an edited earlier
    document blocks here too.

    A refusal carries every reason, including the validator's rule identifiers
    for an invalid document and the identity behind a stale approval. On success
    the derived phase is cached on the spec row -- a cache, not a cursor: the next
    derivation still reads the disk.
    """
    if not actor:
        raise ValueError("an advancement needs an actor")
    with _held(store, ref, lock, owner=actor) as handle:
        # Persist drift first, so the rows a queue reads agree with the decision
        # taken here rather than only with the next derivation.
        sync_staleness(store, ref, spec_type=spec_type, audit=audit)
        state = derive_phase(store, ref, spec_type=spec_type)

        if state.is_untyped:
            return _refuse_advance(
                ref,
                state,
                None,
                (
                    Reason(
                        code=REASON_SPEC_TYPE_UNRECORDED,
                        message=(
                            "No spec type is recorded for this spec, so it has "
                            "no document plan to advance through."
                        ),
                    ),
                ),
                actor=actor,
                audit=audit,
            )

        if gate is None and state.is_final:
            return _refuse_advance(
                ref,
                state,
                None,
                (
                    Reason(
                        code=REASON_ALREADY_FINAL,
                        message=(
                            "Every document in the plan is written and approved; "
                            "authoring has no further phase to enter."
                        ),
                    ),
                ),
                actor=actor,
                audit=audit,
            )

        target = _advance_target(state, gate)
        if target is None:
            return _refuse_advance(
                ref, state, gate, (_no_target_reason(state, gate),), actor=actor, audit=audit
            )

        index = state.gates.index(target)
        reasons, report = blocking_reasons(state, state.gates[: index + 1])
        if reasons:
            return _refuse_advance(
                ref, state, target.gate, reasons, actor=actor, audit=audit, report=report
            )

        remaining = state.gates[index + 1 :]
        to_phase = Phase.of(remaining[0].kind) if remaining else Phase.READY
        store.verify_lock(handle)
        store.record_phase(ref, to_phase.value)

    if audit is not None:
        audit.append(
            ref,
            PHASE_ADVANCED_EVENT,
            initiator=actor,
            detail={
                "gate": target.gate,
                "from_phase": state.phase.value,
                "to_phase": to_phase.value,
            },
        )
    return AdvanceResult(
        ok=True,
        from_phase=state.phase,
        to_phase=to_phase,
        gate=target.gate,
        report=report,
    )


def _advance_target(state: PhaseState, gate: str | None) -> GateState | None:
    """Resolve which gate an advancement is leaving.

    A named gate must belong to the plan. With no name, the target is the last
    document in the plan that is written -- the one whoever called this has just
    finished. When nothing is written at all the target is the first gate, whose
    absent document is the real blocker and reads better than a bare refusal.
    """
    if gate is not None:
        return state.gate_named(gate)
    written = [item for item in state.gates if item.present]
    if written:
        return written[-1]
    return state.gates[0] if state.gates else None


def _no_target_reason(state: PhaseState, gate: str | None) -> Reason:
    """The refusal for an advancement whose gate could not be resolved."""
    if gate is not None:
        planned = ", ".join(item.gate for item in state.gates)
        return Reason(
            code=REASON_GATE_NOT_IN_PLAN,
            gate=gate,
            message=f"A {state.spec_type} spec has no {gate!r} gate; its gates are: {planned}.",
        )
    return Reason(  # pragma: no cover - a planned spec type always declares gates
        code=REASON_ALREADY_FINAL,
        message="This spec type declares no document gates.",
    )


def _refuse_advance(
    ref: SpecRef,
    state: PhaseState,
    gate: str | None,
    reasons: tuple[Reason, ...],
    *,
    actor: str,
    audit: AuditLog | None,
    report: ValidationReport | None = None,
) -> AdvanceResult:
    if audit is not None:
        audit.append(
            ref,
            PHASE_ADVANCE_REFUSED_EVENT,
            initiator=actor,
            detail={
                "gate": gate,
                "from_phase": state.phase.value,
                "reasons": [reason.to_json_object() for reason in reasons],
            },
        )
    return AdvanceResult(
        ok=False,
        from_phase=state.phase,
        to_phase=None,
        gate=gate,
        reasons=reasons,
        report=report,
    )
