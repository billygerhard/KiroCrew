"""The Doctor: one read-only aggregation over everything that can be wrong.

A user whose runs are not working needs one answer, not eight panels. This module
is the single operation every surface calls -- an MCP tool and a UI panel are
renderings of the *same* :class:`DoctorReport`, because a diagnostic that
disagrees with the gate is worse than no diagnostic.

Five properties are load-bearing.

**One severity vocabulary.** A Finding's severity is :class:`~.findings.Severity`,
the same enum document validation uses, read through
:attr:`~.findings.Severity.blocking`. A second enum meaning blocking-versus-advisory
would let one condition read blocking on a panel and advisory at the gate.

**One identifier vocabulary, shared with the refusals.** The identifiers a
degraded capability call quotes are the constants in
:mod:`.capabilities.contracts`, and the identifiers a configuration advisory
quotes are the codes in :mod:`.config.advisories`. This module reuses both rather
than restating them, and mints new identifiers only for conditions nothing else
names yet. That is what makes "run refused" and "doctor says" the same sentence.

**A check that cannot complete becomes a Finding.** Every check runs inside
:meth:`Doctor.run`'s guard, so a check that raises contributes one Finding naming
its own failure and the remaining checks still report. The doctor's whole value is
being callable when the app is broken, and an aggregation that aborts on the first
exception is exactly the one that is unavailable when it is needed.

**Prose is untrusted data.** A Finding's cause and action are
:class:`~.capabilities.contracts.Untrusted`, and its subject goes through
:func:`~.capabilities.contracts.sanitized`. Both carry program names, provider
names, watched-source names, and command output, all of which are influenced by
whoever writes to the tracker or the configuration. Wrapping them means the text
cannot reach an f-string, a log line, or a command template by looking like a
``str``; a display path has to ask for the characters.

The one thing this module executes is a version probe: ``[resolved_path,
"--version"]``, an **engine-authored argv** with no shell, run only for a program a
declared minimum names. What comes back is untrusted data like any other command
output -- stored, compared against a parsed version, never interpreted.

**Read-only where it matters.** Nothing here writes configuration, the autonomy
policy, or the delivery workflow. The one write is the doctor's own history file,
which is what makes a regression distinguishable from a check that never passed,
and a failure to write it becomes a Finding rather than a failed diagnostic.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess  # nosec B404 - engine-authored argv version probes, never a shell
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol

from kiro_crew.atomic_write import atomic_write

from .autonomy import AutonomyLevel
from .budget.ceiling import DispatchOutcome
from .budget.switch import KillSwitch, KillSwitchState
from .capabilities.contracts import Degradation, Untrusted, sanitized
from .config import ConfigStore
from .config.advisories import ConfigWarning, document_warnings
from .config.agent_surface import AgentSurfaceLookup
from .findings import Severity
from .prerequisites import (
    BranchResolver,
    CheckName,
    Prerequisite,
    ProgramResolver,
    RunRefusal,
    check_project,
    check_source,
)
from .review_queue import QueueSnapshot, WaitingOn
from .state import (
    StatePersistenceError,
    reject_spec_tree_path,
    state_root,
    utc_now_iso,
)
from .watch.poll import HealthReason, PollOutcome
from .watch.sources import source_names

logger = logging.getLogger(__name__)

__all__ = [
    "CHECK_ADVISORIES",
    "CHECK_BUDGET",
    "CHECK_CONFIGURATION",
    "CHECK_PREREQUISITES",
    "CHECK_PROGRAM_VERSIONS",
    "CHECK_PROVIDERS",
    "CHECK_REVIEW_QUEUE",
    "CHECK_SOURCE_HEALTH",
    "DISPATCH_FINDINGS",
    "DOCTOR_HISTORY_FILENAME",
    "FINDING_CHECK_FAILED_PREFIX",
    "FINDING_CONFIG_INVALID",
    "FINDING_HISTORY_UNWRITABLE",
    "FINDING_KILL_SWITCH_ENGAGED",
    "FINDING_KILL_SWITCH_UNREADABLE",
    "FINDING_PROGRAM_VERSION",
    "FINDING_RUNS_WAITING_PREFIX",
    "HEALTH_REASON_FINDINGS",
    "SURFACE_AGENT",
    "SURFACE_BUDGET",
    "SURFACE_CAPABILITIES",
    "SURFACE_CONFIG",
    "SURFACE_DOCTOR",
    "SURFACE_REVIEW_QUEUE",
    "SURFACE_WATCH",
    "CheckHistory",
    "CheckOutcome",
    "Doctor",
    "DoctorCheck",
    "DoctorHistory",
    "DoctorReport",
    "Finding",
    "QueueProjection",
    "VersionReader",
    "check_failed_finding_id",
    "dispatch_finding_id",
    "health_finding_id",
    "prerequisite_finding_id",
    "parse_version",
    "refusal_finding_ids",
    "runs_waiting_finding_id",
    "scoped_finding_id",
    "version_satisfies",
]

# --- surfaces ---------------------------------------------------------------

#: Surfaces a Finding can be about when it is not about an autonomy phase. A
#: Finding names one or the other, so a panel can group by the thing an operator
#: would go and look at.
SURFACE_CONFIG = "config"
SURFACE_WATCH = "watch"
SURFACE_BUDGET = "budget"
SURFACE_REVIEW_QUEUE = "review_queue"
SURFACE_CAPABILITIES = "capabilities"
SURFACE_AGENT = "agent"
SURFACE_DOCTOR = "doctor"

# --- finding identifiers ----------------------------------------------------

#: Prefix for the identifiers derived from a prerequisite check. Derived from
#: :class:`~.prerequisites.CheckName` rather than listed, so a prerequisite added
#: there gets a Finding identifier without an edit here -- and cannot get a
#: *second*, differently spelled one.
FINDING_PREREQUISITE_PREFIX = "prerequisite."

#: A configuration document that does not validate. Scoped by nothing: the dotted
#: path of the offending key rides in ``declared_at`` so the identifier stays the
#: condition rather than the location, which is what a regression keys on.
FINDING_CONFIG_INVALID = "config.invalid"

#: A required program is present but older than a declared minimum. Presence and
#: version are separate conditions because the fixes differ: install it versus
#: upgrade it, and a policy-pushed downgrade leaves presence green.
FINDING_PROGRAM_VERSION = "prerequisite.program_version"

#: The kill switch is engaged, so nothing unattended will run.
FINDING_KILL_SWITCH_ENGAGED = "budget.kill_switch_engaged"

#: The kill switch's record could not be read. Distinct from engaged-by-an-operator
#: because the switch reads engaged out of doubt, and the resolving action is to
#: repair or remove the file rather than to release the switch.
FINDING_KILL_SWITCH_UNREADABLE = "budget.kill_switch_unreadable"

#: Prefix for runs parked on a person, completed with the
#: :class:`~.review_queue.WaitingOn` value so the three reasons stay three
#: identifiers.
FINDING_RUNS_WAITING_PREFIX = "runs.waiting_"

#: Prefix for a check that could not complete, completed with the check's name.
#: One identifier per check rather than one shared identifier, so the notify-once
#: rule does not fold two broken checks into one remembered result.
FINDING_CHECK_FAILED_PREFIX = "doctor.check_failed."

#: The doctor's own history could not be persisted, so a regression may be
#: reported or notified twice. Reported rather than raised: a diagnostic that
#: fails because it could not write its notebook is a diagnostic unavailable
#: exactly when it matters.
FINDING_HISTORY_UNWRITABLE = "doctor.history_unwritable"

#: Where each poll health reason lands in the Finding vocabulary.
#:
#: ``PROGRAM_UNAVAILABLE`` deliberately maps onto the *prerequisite* identifier
#: for the same question. ``prerequisites.check_source`` and a watch tick both
#: answer "is this source's poll program on PATH", and the two are one condition
#: with one resolving action; giving each its own identifier would let a Doctor
#: panel built on one and a watcher built on the other name the same broken host
#: differently. The agreement is pinned by a test that an absent poll program
#: yields both representations naming the same program.
HEALTH_REASON_FINDINGS: Mapping[HealthReason, str] = {
    HealthReason.PROGRAM_UNAVAILABLE: (
        FINDING_PREREQUISITE_PREFIX + CheckName.WATCH_PROGRAMS.value
    ),
    HealthReason.CONFIG_INVALID: "watch.config_invalid",
    HealthReason.COMMAND_FAILED: "watch.command_failed",
    HealthReason.TIMED_OUT: "watch.timed_out",
    HealthReason.OUTPUT_TRUNCATED: "watch.output_truncated",
    HealthReason.UNREADABLE_OUTPUT: "watch.unreadable_output",
    HealthReason.FIELD_MAP_MISMATCH: "watch.field_map_mismatch",
}

#: What a run parked on a person costs. A verdict is somebody's turn to take; a
#: budget halt and a stall are runs nothing in the engine will ever move on, which
#: is the condition an operator has to know about before they walk away.
WAITING_SEVERITIES: Mapping[WaitingOn, Severity] = {
    WaitingOn.REVIEW: Severity.WARNING,
    WaitingOn.BUDGET: Severity.ERROR,
    WaitingOn.STALL: Severity.ERROR,
}

#: A blocked dispatch's Finding identifier, per budget outcome.
#:
#: The requirement is that a blocked dispatch quotes the identifier the doctor
#: reports for the same condition. A halted run and an unbounded headless run are
#: the ceiling condition the ``budget_ceiling`` prerequisite names; a stopped one
#: is the kill switch. ``ALLOWED`` has no identifier because nothing is wrong, and
#: is absent rather than mapped to an empty string so a caller cannot quote
#: "nothing" as a reason.
DISPATCH_FINDINGS: Mapping[DispatchOutcome, str] = {
    DispatchOutcome.HALTED: FINDING_PREREQUISITE_PREFIX + CheckName.BUDGET_CEILING.value,
    DispatchOutcome.UNBOUNDED: FINDING_PREREQUISITE_PREFIX + CheckName.BUDGET_CEILING.value,
    DispatchOutcome.STOPPED: FINDING_KILL_SWITCH_ENGAGED,
}

# --- check names ------------------------------------------------------------

CHECK_CONFIGURATION = "configuration"
CHECK_ADVISORIES = "advisories"
CHECK_PREREQUISITES = "prerequisites"
CHECK_SOURCE_HEALTH = "source_health"
CHECK_PROGRAM_VERSIONS = "program_versions"
CHECK_BUDGET = "budget"
CHECK_REVIEW_QUEUE = "review_queue"
CHECK_PROVIDERS = "providers"

#: File under the state root holding the last known result per identifier.
DOCTOR_HISTORY_FILENAME = "doctor_history.json"

#: Owner-only: the history names this host's programs, providers, and sources.
_FILE_MODE = 0o600

#: Schema version of the persisted history document.
HISTORY_VERSION = 1

#: How long a version probe may take. A program that does not answer ``--version``
#: promptly is reported as unreadable rather than waited on: the doctor is called
#: when things are broken, and a hung probe would hang the whole diagnostic.
VERSION_PROBE_TIMEOUT_S = 10


def scoped_finding_id(base: str, subject: str) -> str:
    """Complete *base* with the *subject* it is about.

    Used where the subject domain is enumerable -- the configured sources, the
    programs a minimum is declared for -- because that is what lets the doctor
    record a *pass* per subject and so tell a regression from a first failure.
    Conditions whose subjects cannot be enumerated stay unscoped and carry the
    location in ``declared_at`` instead.

    The subject is sanitized because it comes from the configuration document,
    which an operator or a preset wrote and which the identifier is then compared
    and logged by.
    """
    return f"{base}[{sanitized(subject, limit=_SUBJECT_LIMIT)}]" if subject else base


#: Length cap for the subject inside an identifier. Long enough for a source or
#: program name, short enough that an identifier stays an identifier.
_SUBJECT_LIMIT = 120


def prerequisite_finding_id(check: CheckName, *, source: str = "") -> str:
    """The Finding identifier for one prerequisite check."""
    return scoped_finding_id(FINDING_PREREQUISITE_PREFIX + check.value, source)


def health_finding_id(reason: HealthReason, source: str) -> str:
    """The Finding identifier for one unhealthy poll.

    Derived from :data:`HEALTH_REASON_FINDINGS`, which is why an unavailable poll
    program reported by a watch tick and the same absence reported by
    :func:`~.prerequisites.check_source` land on one identifier.
    """
    return scoped_finding_id(HEALTH_REASON_FINDINGS[reason], source)


def runs_waiting_finding_id(waiting_on: WaitingOn) -> str:
    """The Finding identifier for runs parked on a person for one reason."""
    return FINDING_RUNS_WAITING_PREFIX + waiting_on.value


def check_failed_finding_id(check: str) -> str:
    """The Finding identifier for a check that could not complete."""
    return FINDING_CHECK_FAILED_PREFIX + sanitized(check, limit=_SUBJECT_LIMIT)


def refusal_finding_ids(refusal: RunRefusal) -> tuple[str, ...]:
    """The Finding identifiers a run refusal names, deduplicated, in refusal order.

    The seam that makes "run refused" and "doctor says" one sentence. A refusal
    already carries the unmet prerequisites, so the identifier is *derived* from
    the same :class:`~.prerequisites.CheckName` the doctor derives it from rather
    than restated beside it -- there is no second list to fall out of step. A
    surface reporting a refusal quotes these.
    """
    seen: dict[str, None] = {}
    for check in refusal.unmet:
        seen.setdefault(prerequisite_finding_id(check.check, source=check.source), None)
    return tuple(seen)


def dispatch_finding_id(outcome: DispatchOutcome) -> str:
    """The Finding identifier a blocked dispatch quotes, empty when it is allowed."""
    return DISPATCH_FINDINGS.get(outcome, "")


# --- the finding ------------------------------------------------------------


@dataclass(frozen=True)
class Finding:
    """One thing wrong, addressed by a stable identifier.

    *cause* and *action* are separate for the same reason a prerequisite splits
    them: what the engine looked for and did not find, and what the operator does
    about it. Both are :class:`Untrusted` because both interpolate names the
    configuration or a tracker supplied.

    *identifier* is stable by construction: it is composed from an enum member and,
    where the subject domain is enumerable, a configured name. Nothing volatile --
    no path, no timestamp, no position in a list -- reaches it, because the
    notify-once rule remembers a result *per identifier* and an identifier that
    changed every run would turn notify-once into notify-always.
    """

    identifier: str
    severity: Severity
    #: The autonomy phase this is about, or one of the ``SURFACE_*`` names.
    surface: str
    cause: Untrusted
    action: Untrusted
    #: Dotted configuration path involved, for a surface to link to.
    declared_at: str = ""
    #: What the finding is about: a program, a provider, a source, a run.
    subject: str = ""
    #: Untrusted detail a provider or a command produced, when there is any.
    evidence: Untrusted | None = None
    #: True when this identifier is recorded as having passed before.
    regressed: bool = False
    #: When it last passed, empty unless *regressed*.
    last_passed_ts: str = ""

    def __post_init__(self) -> None:
        if not self.identifier.strip():
            raise ValueError("a finding must carry an identifier")
        # Emptiness is read off the raw text rather than the display rendering:
        # ``for_display`` appends a truncation notice, which would make a blank
        # string look non-blank.
        if not self.cause.text.strip():
            raise ValueError(f"finding {self.identifier} must say what is wrong")
        if not self.action.text.strip():
            raise ValueError(f"finding {self.identifier} must state the resolving action")
        if self.last_passed_ts and not self.regressed:
            raise ValueError(
                f"finding {self.identifier} carries a last-passed time without being a regression"
            )
        # Sanitized at construction rather than at each display site: both fields
        # are document-authored and are compared, grouped, and logged on, so no
        # construction path may skip the contract's rendering.
        object.__setattr__(self, "subject", sanitized(self.subject, limit=_SUBJECT_LIMIT))
        object.__setattr__(self, "declared_at", sanitized(self.declared_at, limit=_SUBJECT_LIMIT))

    @property
    def blocking(self) -> bool:
        return self.severity.blocking

    def describe(self) -> str:
        """One line for a human. Every untrusted part rendered for display."""
        marker = "regression" if self.regressed else self.severity.value
        return (
            f"{self.identifier} ({marker}, {self.surface}): "
            f"{self.cause.for_display()} -- {self.action.for_display()}"
        )

    def to_json_object(self) -> dict[str, Any]:
        record: dict[str, Any] = {
            "id": self.identifier,
            "severity": self.severity.value,
            "blocking": self.blocking,
            "surface": self.surface,
            "cause": self.cause.for_display(),
            "action": self.action.for_display(),
            "declared_at": self.declared_at,
            "subject": self.subject,
            "regressed": self.regressed,
        }
        if self.evidence is not None:
            record["evidence"] = self.evidence.for_display()
        if self.last_passed_ts:
            record["last_passed_ts"] = self.last_passed_ts
        return record


@dataclass(frozen=True)
class CheckOutcome:
    """What one check contributed.

    *passing* is as important as *findings*: an identifier reported passing is how
    the next evaluation can tell a check that broke from one that was never
    configured. A check whose subject domain cannot be enumerated reports no
    passing identifiers rather than guessing at them.
    """

    findings: tuple[Finding, ...] = ()
    passing: tuple[str, ...] = ()


@dataclass(frozen=True)
class DoctorCheck:
    """One aggregated check, named so its failure can name itself."""

    name: str
    run: Callable[[], CheckOutcome]


@dataclass(frozen=True)
class DoctorReport:
    """Every Finding the doctor found, plus what it saw pass."""

    findings: tuple[Finding, ...] = ()
    passing: tuple[str, ...] = ()
    #: Regressions worth a notification now. Empty when nothing changed, which is
    #: what keeps an unchanged failure quiet.
    to_notify: tuple[Finding, ...] = ()

    @property
    def ok(self) -> bool:
        """True when nothing blocking was found. Advisories do not fail a report."""
        return not self.blocking

    @property
    def blocking(self) -> tuple[Finding, ...]:
        return tuple(finding for finding in self.findings if finding.blocking)

    @property
    def advisory(self) -> tuple[Finding, ...]:
        return tuple(finding for finding in self.findings if not finding.blocking)

    @property
    def regressions(self) -> tuple[Finding, ...]:
        return tuple(finding for finding in self.findings if finding.regressed)

    @property
    def identifiers(self) -> tuple[str, ...]:
        """The identifiers present, deduplicated, in report order."""
        seen: dict[str, None] = {}
        for finding in self.findings:
            seen.setdefault(finding.identifier, None)
        return tuple(seen)

    def for_identifier(self, identifier: str) -> tuple[Finding, ...]:
        return tuple(finding for finding in self.findings if finding.identifier == identifier)

    def for_surface(self, surface: str) -> tuple[Finding, ...]:
        return tuple(finding for finding in self.findings if finding.surface == surface)

    def __iter__(self) -> Iterator[Finding]:
        return iter(self.findings)

    def __len__(self) -> int:
        return len(self.findings)

    def to_json_object(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "findings": [finding.to_json_object() for finding in self.findings],
            "blocking": len(self.blocking),
            "advisory": len(self.advisory),
            "passing": list(self.passing),
            "notify": [finding.identifier for finding in self.to_notify],
        }


# --- history: the last known result per identifier --------------------------


@dataclass(frozen=True)
class CheckHistory:
    """What the last evaluation of one identifier concluded."""

    identifier: str
    passing: bool
    last_passed_ts: str = ""
    last_seen_ts: str = ""
    #: Whether the current failure has already been notified. Reset when the
    #: identifier passes again, so a condition that comes back is announced again.
    notified: bool = False

    def to_json_object(self) -> dict[str, Any]:
        return {
            "passing": self.passing,
            "last_passed_ts": self.last_passed_ts,
            "last_seen_ts": self.last_seen_ts,
            "notified": self.notified,
        }


class DoctorHistory:
    """The doctor's notebook: last known result per Finding identifier.

    Deliberately the only thing the doctor writes, and deliberately not in the
    configuration document. A history kept in configuration would put the
    diagnostic's own bookkeeping behind the config-only fence the autonomy policy
    and the delivery workflow sit behind, which is the fence that stops the
    diagnostic from ever growing a fix button.

    An unreadable history reads as *empty*. Nothing is lost that was not already
    lost, and inventing a regression out of a corrupt file would notify falsely --
    the opposite of the drift signal this exists to give.
    """

    def __init__(self, root: str | Path | None = None) -> None:
        resolved = Path(root) if root is not None else state_root()
        reject_spec_tree_path(resolved)
        self._root = resolved

    @property
    def root(self) -> Path:
        return self._root

    @property
    def path(self) -> Path:
        return self._root / DOCTOR_HISTORY_FILENAME

    def read(self) -> dict[str, CheckHistory]:
        """Every recorded result, keyed by identifier."""
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except OSError as exc:
            logger.error(
                "doctor history %s cannot be read (%s); treating it as empty", self.path, exc
            )
            return {}
        try:
            document = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            logger.error("doctor history %s is not readable JSON; treating it as empty", self.path)
            return {}
        checks = document.get("checks") if isinstance(document, dict) else None
        if not isinstance(checks, dict):
            return {}
        recorded: dict[str, CheckHistory] = {}
        for identifier, entry in checks.items():
            if not isinstance(identifier, str) or not isinstance(entry, dict):
                continue
            recorded[identifier] = CheckHistory(
                identifier=identifier,
                passing=bool(entry.get("passing", False)),
                last_passed_ts=str(entry.get("last_passed_ts", "")),
                last_seen_ts=str(entry.get("last_seen_ts", "")),
                notified=bool(entry.get("notified", False)),
            )
        return recorded

    def write(self, recorded: Mapping[str, CheckHistory]) -> None:
        """Persist *recorded*. Raises when it cannot land."""
        payload = {
            "version": HISTORY_VERSION,
            "checks": {
                identifier: entry.to_json_object() for identifier, entry in sorted(recorded.items())
            },
        }
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            atomic_write(self.path, json.dumps(payload, indent=2, sort_keys=True), mode=_FILE_MODE)
        except OSError as exc:
            raise StatePersistenceError(
                f"cannot write the doctor history at {self.path}: {exc}"
            ) from exc


def _annotate(
    findings: Sequence[Finding],
    passing: Sequence[str],
    recorded: Mapping[str, CheckHistory],
    *,
    now: str,
) -> tuple[tuple[Finding, ...], tuple[Finding, ...], dict[str, CheckHistory]]:
    """Mark regressions, choose what to notify, and return the next history.

    A finding is a *regression* when its identifier is recorded as having passed
    at some point: that is materially different from a check that never passed,
    both in what it means (drift, not absence of setup) and in what fixes it.
    It is *notified* only on the evaluation that discovers it, so an unchanged
    failure stays quiet however often the doctor is called.
    """
    updated = {identifier: entry for identifier, entry in recorded.items()}
    annotated: list[Finding] = []
    notify: list[Finding] = []
    failing_ids: set[str] = set()
    for finding in findings:
        previous = recorded.get(finding.identifier)
        passed_before = bool(previous and previous.last_passed_ts)
        marked = (
            replace(finding, regressed=True, last_passed_ts=previous.last_passed_ts)
            if passed_before and previous is not None
            else finding
        )
        annotated.append(marked)
        first_time = finding.identifier not in failing_ids
        failing_ids.add(finding.identifier)
        already_notified = bool(previous and previous.notified)
        if marked.regressed and not already_notified and first_time:
            notify.append(marked)
        updated[finding.identifier] = CheckHistory(
            identifier=finding.identifier,
            passing=False,
            last_passed_ts=previous.last_passed_ts if previous else "",
            last_seen_ts=now,
            notified=already_notified or marked.regressed,
        )
    for identifier in passing:
        if identifier in failing_ids:
            # A check that reported the same identifier both passing and failing
            # is reporting a failure; the failing read wins so a partial pass
            # cannot clear a regression.
            continue
        updated[identifier] = CheckHistory(
            identifier=identifier,
            passing=True,
            last_passed_ts=now,
            last_seen_ts=now,
            notified=False,
        )
    return tuple(annotated), tuple(notify), updated


# --- collaborators the doctor reads -----------------------------------------


class QueueProjection(Protocol):
    """The part of the Review_Queue the doctor reads."""

    def snapshot(self, *, project: str | None = None) -> QueueSnapshot: ...


#: Reads a program's self-reported version, given its resolved path. Injectable so
#: a test describes a host rather than arranging one, and so the one thing the
#: doctor executes is replaceable at the boundary.
VersionReader = Callable[[str], str]

_VERSION_NUMBER = re.compile(r"(\d+(?:\.\d+)*)")


def parse_version(text: str) -> tuple[int, ...]:
    """The first dotted number in *text*, as a comparable tuple.

    *text* is command output, so it is parsed rather than trusted: anything that
    is not a dotted number is ignored, and a version that cannot be found yields
    an empty tuple, which the caller reports as unreadable rather than as
    satisfying or violating a minimum.
    """
    found = _VERSION_NUMBER.search(text)
    if found is None:
        return ()
    return tuple(int(part) for part in found.group(1).split("."))


def version_satisfies(found: Sequence[int], minimum: Sequence[int]) -> bool:
    """Whether *found* is at least *minimum*, comparing component by component.

    Shorter sequences are padded with zeros, so ``2.1`` satisfies ``2.1.0`` and
    ``2.1`` fails ``2.1.1``.
    """
    width = max(len(found), len(minimum))
    padded_found = tuple(found) + (0,) * (width - len(found))
    padded_minimum = tuple(minimum) + (0,) * (width - len(minimum))
    return padded_found >= padded_minimum


def read_program_version(path: str) -> str:
    """Run ``[path, "--version"]`` and return what it printed.

    The argv is engine-authored: two elements, the resolved path and a literal
    flag, passed as a list with no shell anywhere. A configured program name
    reaches this only as ``argv[0]`` -- the same treatment a watch poll gives it --
    so a name containing shell metacharacters is a name, not a command. What comes
    back is untrusted data: it is parsed for a dotted number and otherwise only
    displayed.
    """
    try:
        completed = subprocess.run(  # nosec B603 - engine-authored argv, no shell
            [path, "--version"],
            capture_output=True,
            text=True,
            timeout=VERSION_PROBE_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.info("version probe for %r could not run: %s", path, exc)
        return ""
    return completed.stdout or completed.stderr


# --- the aggregation --------------------------------------------------------


@dataclass
class Doctor:
    """The single read-only aggregation. Every surface renders this one result.

    Collaborators are injected rather than constructed here for two reasons: the
    doctor has to be callable on a host where half of them are broken, and a test
    has to be able to describe a broken host without arranging one. Anything not
    supplied is read from the real environment.
    """

    config: ConfigStore
    project: str | None = None
    which: ProgramResolver | None = None
    branch_exists: BranchResolver | None = None
    base_branch: str = ""
    kill_switch: KillSwitch | None = None
    queue: QueueProjection | None = None
    #: Degradations recorded for this project's recent calls, as the engine
    #: reported them. Passed in because the identifiers already exist on the
    #: degradation: the doctor quotes them rather than re-deriving them.
    degradations: Sequence[Degradation] = ()
    #: Poll outcomes already recorded by a watch tick, when the caller has them.
    poll_outcomes: Sequence[PollOutcome] = ()
    agents: AgentSurfaceLookup | None = None
    #: Declared minimum versions, keyed by program name. Supplied by the caller
    #: that resolved the workflow preset or configuration declaring them.
    minimum_versions: Mapping[str, str] = field(default_factory=dict)
    version_of: VersionReader | None = None
    history: DoctorHistory | None = None
    clock: Callable[[], str] = utc_now_iso

    def run(self) -> DoctorReport:
        """Every check, each one failing into a Finding rather than out of the run.

        The guard is the requirement: a check that raises contributes a Finding
        naming its own failure, and every other check still reports. An
        aggregation that propagated the first exception would be unavailable on
        exactly the broken host it exists to diagnose.
        """
        findings: list[Finding] = []
        passing: list[str] = []
        for check in self.checks():
            try:
                outcome = check.run()
            except Exception as exc:  # noqa: BLE001 - a broken check is a Finding
                logger.warning("doctor check %r could not complete: %s", check.name, exc)
                findings.append(self._check_failed(check.name, exc))
                continue
            findings.extend(outcome.findings)
            passing.extend(outcome.passing)
        return self._with_history(findings, passing)

    def checks(self) -> tuple[DoctorCheck, ...]:
        """The checks, in the order a report reads best: setup, then state."""
        return (
            DoctorCheck(CHECK_CONFIGURATION, self._configuration),
            DoctorCheck(CHECK_ADVISORIES, self._advisories),
            DoctorCheck(CHECK_PREREQUISITES, self._prerequisites),
            DoctorCheck(CHECK_SOURCE_HEALTH, self._source_health),
            DoctorCheck(CHECK_PROGRAM_VERSIONS, self._program_versions),
            DoctorCheck(CHECK_BUDGET, self._budget),
            DoctorCheck(CHECK_REVIEW_QUEUE, self._review_queue),
            DoctorCheck(CHECK_PROVIDERS, self._providers),
        )

    # --- individual checks --------------------------------------------------

    def _configuration(self) -> CheckOutcome:
        errors = self.config.validate()
        if not errors:
            return CheckOutcome(passing=(FINDING_CONFIG_INVALID,))
        findings = tuple(
            Finding(
                identifier=FINDING_CONFIG_INVALID,
                severity=Severity.ERROR,
                surface=SURFACE_CONFIG,
                cause=Untrusted(f"{error.path}: {error.message}"),
                action=Untrusted(f"correct {error.path} in the app configuration"),
                declared_at=error.path,
            )
            for error in errors
        )
        return CheckOutcome(findings=findings)

    def _advisories(self) -> CheckOutcome:
        warnings = document_warnings(self.config.document(), agents=self.agents)
        findings = tuple(self._advisory_finding(warning) for warning in warnings)
        return CheckOutcome(findings=findings)

    @staticmethod
    def _advisory_finding(warning: ConfigWarning) -> Finding:
        # The advisory codes are already the shared identifier vocabulary: a
        # refusal, an audit entry, and this Finding quote the same string.
        severity = Severity.ERROR if warning.requires_acknowledgment else Severity.WARNING
        surface = SURFACE_AGENT if warning.code.startswith("cost_profiles.") else SURFACE_CONFIG
        return Finding(
            identifier=warning.code,
            severity=severity,
            surface=surface,
            cause=Untrusted(warning.message),
            action=Untrusted(
                f"review {warning.path}"
                + (
                    " and acknowledge it, or narrow the autonomy it arms"
                    if warning.requires_acknowledgment
                    else ""
                )
            ),
            declared_at=warning.path,
            subject=warning.project or "",
        )

    def _prerequisites(self) -> CheckOutcome:
        report = check_project(
            self.config,
            project=self.project,
            base_branch=self.base_branch,
            which=self.which,
            branch_exists=self.branch_exists,
        )
        findings: list[Finding] = []
        passing: list[str] = []
        for phase, checks in report.by_phase().items():
            for check in checks:
                self._fold_prerequisite(check, phase, findings, passing)
        return CheckOutcome(findings=tuple(findings), passing=tuple(passing))

    def _source_health(self) -> CheckOutcome:
        findings: list[Finding] = []
        passing: list[str] = []
        for name in source_names(self.config):
            report = check_source(self.config, name, which=self.which)
            for check in report.checks:
                self._fold_prerequisite(check, check.phase, findings, passing)
        findings.extend(self._recorded_health_findings())
        return CheckOutcome(findings=tuple(findings), passing=tuple(passing))

    def _recorded_health_findings(self) -> tuple[Finding, ...]:
        """Findings for poll outcomes a watch tick already recorded.

        Every reason resolves through :func:`health_finding_id`, so an unavailable
        poll program reported here carries the *same* identifier the prerequisite
        resolution above produces for the same host. There is one condition and
        one identifier, whichever side observed it.
        """
        findings: list[Finding] = []
        for outcome in self.poll_outcomes:
            if outcome.reason is None:
                continue
            findings.append(
                Finding(
                    identifier=health_finding_id(outcome.reason, outcome.source),
                    severity=Severity.ERROR,
                    surface=SURFACE_WATCH,
                    cause=Untrusted(f"watch source {outcome.source!r}: {outcome.detail}"),
                    action=Untrusted(
                        f"repair watch source {outcome.source!r} so a poll can report its items"
                    ),
                    subject=outcome.program or outcome.source,
                    evidence=Untrusted(outcome.detail),
                )
            )
        return tuple(findings)

    def _fold_prerequisite(
        self,
        check: Prerequisite,
        phase: AutonomyLevel,
        findings: list[Finding],
        passing: list[str],
    ) -> None:
        identifier = prerequisite_finding_id(check.check, source=check.source)
        if check.met:
            passing.append(identifier)
            return
        findings.append(
            Finding(
                identifier=identifier,
                severity=Severity.ERROR,
                surface=phase.value,
                cause=Untrusted(check.missing),
                action=Untrusted(check.action),
                declared_at=check.declared_at,
                subject=check.source,
            )
        )

    def _program_versions(self) -> CheckOutcome:
        resolve = self.which or _default_which
        read = self.version_of or read_program_version
        findings: list[Finding] = []
        passing: list[str] = []
        for program, declared in sorted(self.minimum_versions.items()):
            identifier = scoped_finding_id(FINDING_PROGRAM_VERSION, program)
            minimum = parse_version(str(declared))
            resolved = resolve(program)
            if resolved is None:
                # Absence is the presence check's condition, not this one's, so it
                # is reported under that identifier rather than a second one.
                findings.append(
                    Finding(
                        identifier=prerequisite_finding_id(CheckName.PROGRAMS),
                        severity=Severity.ERROR,
                        surface=AutonomyLevel.EXECUTION.value,
                        cause=Untrusted(f"program {program!r} is not on PATH"),
                        action=Untrusted(f"install {program!r} or remove its declared minimum"),
                        subject=program,
                    )
                )
                continue
            reported = read(resolved)
            found = parse_version(reported)
            if not found:
                findings.append(
                    Finding(
                        identifier=identifier,
                        severity=Severity.WARNING,
                        surface=AutonomyLevel.EXECUTION.value,
                        cause=Untrusted(
                            f"program {program!r} did not report a version, so the declared "
                            f"minimum {declared} cannot be verified"
                        ),
                        action=Untrusted(
                            f"check {program!r} by hand, or remove its declared minimum"
                        ),
                        subject=program,
                        evidence=Untrusted(reported),
                    )
                )
                continue
            if not version_satisfies(found, minimum):
                findings.append(
                    Finding(
                        identifier=identifier,
                        severity=Severity.ERROR,
                        surface=AutonomyLevel.EXECUTION.value,
                        cause=Untrusted(
                            f"program {program!r} reports "
                            f"{'.'.join(str(part) for part in found)}, below the declared "
                            f"minimum {declared}"
                        ),
                        action=Untrusted(f"upgrade {program!r} to {declared} or later"),
                        subject=program,
                        evidence=Untrusted(reported),
                    )
                )
                continue
            passing.append(identifier)
        return CheckOutcome(findings=tuple(findings), passing=tuple(passing))

    def _budget(self) -> CheckOutcome:
        switch = self.kill_switch or KillSwitch()
        state = switch.read()
        if not state.engaged:
            return CheckOutcome(
                passing=(FINDING_KILL_SWITCH_ENGAGED, FINDING_KILL_SWITCH_UNREADABLE)
            )
        return CheckOutcome(findings=(_kill_switch_finding(state),))

    def _review_queue(self) -> CheckOutcome:
        queue = self.queue
        if queue is None:
            return CheckOutcome()
        snapshot = queue.snapshot(project=self.project)
        grouped: dict[WaitingOn, list[str]] = {reason: [] for reason in WAITING_SEVERITIES}
        for entry in snapshot:
            grouped[entry.waiting_on].append(entry.run_id)
        findings: list[Finding] = []
        passing: list[str] = []
        for reason, run_ids in grouped.items():
            identifier = runs_waiting_finding_id(reason)
            if not run_ids:
                passing.append(identifier)
                continue
            findings.append(
                Finding(
                    identifier=identifier,
                    severity=WAITING_SEVERITIES[reason],
                    surface=SURFACE_REVIEW_QUEUE,
                    cause=Untrusted(
                        f"{len(run_ids)} run(s) waiting on a person for {reason.value}: "
                        f"{', '.join(run_ids)}"
                    ),
                    action=Untrusted(_WAITING_ACTIONS[reason]),
                    subject=run_ids[0],
                )
            )
        return CheckOutcome(findings=tuple(findings), passing=tuple(passing))

    def _providers(self) -> CheckOutcome:
        findings = tuple(
            Finding(
                identifier=degradation.finding_id,
                severity=Severity.WARNING,
                surface=SURFACE_CAPABILITIES,
                cause=Untrusted(f"{degradation.reason} (transport {degradation.transport})"),
                action=Untrusted(
                    f"check the {degradation.transport} provider binding, or accept the "
                    f"builtin's depth"
                ),
                subject=degradation.transport,
                evidence=degradation.detail,
            )
            for degradation in self.degradations
        )
        # No passing identifiers: this reads recorded degradations rather than
        # probing a provider, and "nothing degraded on the calls I was handed" is
        # not evidence that a provider is reachable. The reachability question is
        # the prerequisite PROVIDERS check, which does resolve a program.
        return CheckOutcome(findings=findings)

    # --- plumbing ----------------------------------------------------------

    def _check_failed(self, name: str, exc: BaseException) -> Finding:
        return Finding(
            identifier=check_failed_finding_id(name),
            severity=Severity.ERROR,
            surface=SURFACE_DOCTOR,
            cause=Untrusted(f"the {name} check could not complete: {exc}"),
            action=Untrusted(
                f"read the gateway log for the {name} check's failure, then re-run the doctor"
            ),
            subject=name,
            evidence=Untrusted(f"{type(exc).__name__}: {exc}"),
        )

    def _with_history(self, findings: Sequence[Finding], passing: Sequence[str]) -> DoctorReport:
        history = self.history
        if history is None:
            return DoctorReport(findings=tuple(findings), passing=tuple(passing))
        annotated, notify, updated = _annotate(findings, passing, history.read(), now=self.clock())
        try:
            history.write(updated)
        except StatePersistenceError as exc:
            logger.error("doctor history could not be persisted: %s", exc)
            annotated = annotated + (
                Finding(
                    identifier=FINDING_HISTORY_UNWRITABLE,
                    severity=Severity.WARNING,
                    surface=SURFACE_DOCTOR,
                    cause=Untrusted(f"the doctor's history could not be written: {exc}"),
                    action=Untrusted(
                        "make the engine's state root writable so regressions are "
                        "reported once rather than every time"
                    ),
                ),
            )
        return DoctorReport(findings=annotated, passing=tuple(passing), to_notify=notify)


#: What resolves each parked-run reason.
_WAITING_ACTIONS: Mapping[WaitingOn, str] = {
    WaitingOn.REVIEW: "record a verdict at the outstanding review gate",
    WaitingOn.BUDGET: "raise the run ceiling or let the halted run go",
    WaitingOn.STALL: "resume or abandon the run that stopped reporting",
}


def _kill_switch_finding(state: KillSwitchState) -> Finding:
    if state.unreadable:
        return Finding(
            identifier=FINDING_KILL_SWITCH_UNREADABLE,
            severity=Severity.ERROR,
            surface=SURFACE_BUDGET,
            cause=Untrusted(
                "the kill switch record could not be read, so unattended work is "
                "stopped out of doubt"
            ),
            action=Untrusted("repair or remove the kill switch record under the state root"),
        )
    return Finding(
        identifier=FINDING_KILL_SWITCH_ENGAGED,
        severity=Severity.ERROR,
        surface=SURFACE_BUDGET,
        cause=Untrusted(state.describe()),
        action=Untrusted("release the kill switch when the reason it was thrown is resolved"),
        subject=state.initiator,
    )


def _default_which(program: str) -> str | None:
    return shutil.which(program)
