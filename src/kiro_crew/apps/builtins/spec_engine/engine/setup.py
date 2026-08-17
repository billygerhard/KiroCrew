"""Agent-assisted setup: infer what the project already states, ask what it must not.

Setting this engine up by hand means answering a dozen questions whose answers are
already written down somewhere in the project -- which tracker it uses, whether it
ships through pull requests, what its build entry point is. This module reads
those places and proposes configuration from them, so the operator confirms
findings instead of composing a document.

Four properties hold here, and each one is a refusal rather than a convention:

* **Every inference carries the evidence that produced it.** :class:`Inference`
  refuses to exist without at least one :class:`Evidence`, because an inference
  shown without its evidence asks an operator to approve something they cannot
  check. The evidence is *file text* -- a steering note, a vendored CI config, a
  doc paragraph pasted from an issue -- so it is untrusted input and renders
  through the display contract: :meth:`~.capabilities.contracts.Untrusted.for_display`
  for prose and :func:`~.capabilities.contracts.sanitized` for the
  identifier-shaped fields the engine also matches on.

* **Some decisions are asked, never inferred.** The cost profile decides how much
  money unattended work may spend, and the three autonomy confirmations decide
  where the engine starts spending, starts changing code, and starts touching a
  remote. :data:`ASKED_SUBJECTS` names them and :class:`Inference` raises for any
  of them, so "infer a cost profile from project context" is not expressible here
  rather than merely discouraged.

* **One write path.** :func:`apply_setup` builds a patch and hands it to
  :meth:`~.config.ConfigStore.write` with :data:`~.config.SETUP_ASSISTANT_SURFACE`,
  so the schema validators run on what would land on disk. Nothing in this module
  opens the config file.

* **Project files alone are enough.** Memory is an input, not a requirement:
  passing none is a supported degraded mode that yields fewer inferences and says
  so through :attr:`SetupPlan.memory_consulted`, never an inference it did not
  make.

Prerequisite answers come from :mod:`.prerequisites` in both directions. Before
anything is written, an offered preset's programs are checked with that module's
own check builder, so the report an operator reads at offer time uses the same
vocabulary and the same resolving-action wording the run gate will refuse with.
After the write, :func:`~.prerequisites.check_source` is the authoritative answer
for a source that now exists in the document. There is deliberately no second
"is this program installed" answer in this file.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .autonomy import AUTONOMY_FIELD, AutonomyLevel
from .capabilities.contracts import Untrusted, sanitized
from .config import ConfigStore
from .config.advisories import ConfigWarning
from .config.agent_surface import PROJECT_PATH_FIELD
from .config.profiles import (
    COST_PROFILE_PRESET_NAMES,
    PROJECT_PROFILE_FIELD,
    cost_profile_presets,
)
from .config.schema import (
    SECTION_COST_PROFILES,
    SECTION_PROJECTS,
    SECTION_SOURCES,
    SECTION_WORKFLOW,
    WILDCARD_KEY,
)
from .config.store import SETUP_ASSISTANT_SURFACE, ConfigWriteSurface
from .delivery.workflow import (
    WORKFLOW_PRESET_NAMES,
    WORKFLOW_PRESETS,
    workflow_preset_definition,
    workflow_presets,
)
from .prerequisites import (
    Prerequisite,
    PrerequisiteReport,
    ProgramResolver,
)
from .prerequisites import _program_check as program_check
from .prerequisites import (
    check_source,
    stage_phase,
)
from .watch.sources import (
    WATCH_SOURCE_PRESET_HOSTS,
    WATCH_SOURCE_PRESET_PROGRAMS,
    watch_source_presets,
)

__all__ = [
    "ASKED_SUBJECTS",
    "SUBJECT_TOOLING",
    "SUBJECT_WATCH_SOURCE",
    "SUBJECT_WORKFLOW_PRACTICE",
    "SUBJECT_WORKFLOW_PRESET",
    "Evidence",
    "Inference",
    "InferredSubjectRefused",
    "PresetOffer",
    "Question",
    "RemoteOrigin",
    "SetupAnswers",
    "SetupApprovalRequired",
    "SetupPatch",
    "SetupPlan",
    "SetupResult",
    "inspect_project",
    "propose_setup",
    "setup_patch",
    "apply_setup",
]

#: Subject naming the delivery workflow preset the project's remote implies.
SUBJECT_WORKFLOW_PRESET = "workflow.preset"

#: Subject naming the watch source host the project's remote implies.
SUBJECT_WATCH_SOURCE = "watch.source"

#: Subject naming the build and test entry points the project already has.
SUBJECT_TOOLING = "tooling"

#: Subject naming a review practice the project states in prose.
SUBJECT_WORKFLOW_PRACTICE = "workflow.practice"

#: Subject naming the cost profile. Present only so :data:`ASKED_SUBJECTS` can
#: name it; no inference may carry it.
SUBJECT_COST_PROFILE = PROJECT_PROFILE_FIELD

#: Subjects this module asks about and never infers.
#:
#: The cost profile decides how much money autonomous work may spend, and the
#: three autonomy rungs above authoring decide where the engine begins spending,
#: begins changing code, and begins touching a remote. A heuristic that guessed
#: any of them from project context would be a defect even when the guess is
#: good, so :class:`Inference` raises for a subject in this set: the wrong
#: behavior is unrepresentable rather than merely unwritten.
ASKED_SUBJECTS: frozenset[str] = frozenset(
    {SUBJECT_COST_PROFILE}
    | {f"{AUTONOMY_FIELD}.{level.value}" for level in AutonomyLevel if level.rank > 0}
)

#: The rungs that each need their own confirmation, lowest first. Authoring is
#: absent because it neither spends beyond a prompt nor writes outside the spec
#: directory, which is what the other three do.
CONFIRMED_LEVELS: tuple[AutonomyLevel, ...] = tuple(
    level for level in sorted(AutonomyLevel, key=lambda item: item.rank) if level.rank > 0
)

#: What each confirmed rung authorizes, in the operator's terms. One prompt per
#: rung because the three are different grants: a single "yes to everything" is
#: one answer, and three grants need three.
LEVEL_PROMPTS: Mapping[AutonomyLevel, str] = {
    AutonomyLevel.EXECUTION: (
        "May the engine run implementation tasks unattended? This is the rung that "
        "starts spending credits without someone watching."
    ),
    AutonomyLevel.DELIVERY: (
        "May the engine run your delivery workflow unattended? This is the rung that "
        "commits and pushes changes and raises a review."
    ),
    AutonomyLevel.INTEGRATION: (
        "May the engine integrate approved work into a protected branch unattended? "
        "This is the one rung whose mistake cannot be undone."
    ),
}

#: Steering directory relative to the project root, and the docs and build files
#: read beside it. Read-only, and each is optional: a project with none of them
#: yields no inferences rather than an error.
STEERING_DIR = Path(".kiro") / "steering"
DOC_FILES: tuple[str, ...] = ("README.md", "CONTRIBUTING.md", "AGENTS.md")
BUILD_FILES: tuple[str, ...] = ("Makefile", "package.json", "pyproject.toml")
CI_DIR = Path(".github") / "workflows"
CI_FILES: tuple[str, ...] = (".gitlab-ci.yml", ".gitlab-ci.yaml")

#: Bytes read from any one file. A steering tree can hold a generated inventory,
#: and an inference does not get better for having read a megabyte of it.
MAX_FILE_BYTES = 64_000

#: Files read from any one directory, so a large steering or workflow tree cannot
#: turn setup into a full-tree scan.
MAX_FILES_PER_DIR = 40

#: Characters of a matching line kept as an excerpt. Long enough to show the
#: sentence that produced the inference, short enough that a file of one very
#: long line cannot fill the operator's screen.
MAX_EXCERPT_CHARS = 240

#: Public host each remote host name maps to. Keyed by the substring that appears
#: in a remote URL, valued at the bundled preset name, so the two preset tables
#: keyed by host stay reachable from one place. Derived names are checked against
#: :data:`~.watch.sources.WATCH_SOURCE_PRESET_HOSTS` at import.
REMOTE_HOSTS: Mapping[str, str] = {
    "github.com": "github",
    "gitlab.com": "gitlab",
}

#: Workflow preset applicable to each public host, plus the fallback for a
#: project with no remote at all. Applicability is what the remote states, which
#: is why this maps hosts rather than asking.
HOST_WORKFLOW_PRESETS: Mapping[str, str] = {
    "github": "git-pull-request",
    "gitlab": "git-merge-request",
}

#: The preset for a project whose repository has no remote: verify and commit
#: locally, and never try to push somewhere that does not exist.
LOCAL_WORKFLOW_PRESET = "local-only"

#: Prose that indicates a review practice, mapped to the workflow preset it
#: corroborates. Corroboration only: the remote decides, and a doc that says
#: "pull request" on a GitLab remote does not move the answer.
PRACTICE_PHRASES: Mapping[str, str] = {
    "pull request": "git-pull-request",
    "merge request": "git-merge-request",
}

#: Make targets and package scripts worth reporting as an existing entry point.
TOOLING_TARGETS: tuple[str, ...] = ("build", "test", "lint", "check")

_REMOTE_SECTION = re.compile(r'^\s*\[remote\s+"([^"]+)"\]\s*$')
_REMOTE_URL = re.compile(r"^\s*url\s*=\s*(\S+)\s*$")
_MAKE_TARGET = re.compile(r"^([A-Za-z][A-Za-z0-9_.-]*)\s*:(?!=)")

_ORIGIN_REMOTE = "origin"

for _preset_host in REMOTE_HOSTS.values():
    if _preset_host not in WATCH_SOURCE_PRESET_HOSTS:
        raise RuntimeError(
            f"remote host mapping names {_preset_host!r}, which is not a bundled watch "
            f"source preset host: {', '.join(WATCH_SOURCE_PRESET_HOSTS)}"
        )
for _preset_name in (*HOST_WORKFLOW_PRESETS.values(), LOCAL_WORKFLOW_PRESET):
    if _preset_name not in WORKFLOW_PRESET_NAMES:
        raise RuntimeError(
            f"workflow mapping names {_preset_name!r}, which is not a bundled workflow "
            f"preset: {', '.join(WORKFLOW_PRESET_NAMES)}"
        )


class InferredSubjectRefused(ValueError):
    """Raised when an inference claims a subject that must be asked about."""

    def __init__(self, subject: str) -> None:
        self.subject = subject
        super().__init__(
            f"{subject!r} is asked, never inferred: the setup assistant presents it as a "
            "question because it decides how much unattended work may spend or how far it "
            "may go, and a good guess is still a guess the operator did not make"
        )


class SetupApprovalRequired(PermissionError):
    """Raised when a write was requested without the approval that authorizes it."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class Evidence:
    """One piece of file text an inference was drawn from.

    *excerpt* is :class:`~.capabilities.contracts.Untrusted` because it is a line
    from a file this engine did not write -- a steering note, a vendored CI
    config, a paragraph pasted out of an issue -- and it is rendered rather than
    concatenated. *located_at* is a project-relative path, which is
    identifier-shaped and matched on, so it renders through
    :func:`~.capabilities.contracts.sanitized`.
    """

    located_at: str
    excerpt: Untrusted

    def render(self) -> dict[str, str]:
        """Return the display form: sanitized location, displayable excerpt."""
        return {
            "located_at": sanitized(self.located_at),
            "excerpt": self.excerpt.for_display(limit=MAX_EXCERPT_CHARS),
        }

    def describe(self) -> str:
        rendered = self.render()
        return f"{rendered['located_at']}: {rendered['excerpt']}"


@dataclass(frozen=True)
class Inference:
    """Something read out of the project, and the evidence that produced it.

    Refuses two things at construction, and both refusals are the point of the
    class. An inference with no evidence would ask an operator to approve a claim
    they cannot check, and an inference whose subject is in
    :data:`ASKED_SUBJECTS` would be a guess at a decision that must be asked.
    """

    subject: str
    value: str
    rationale: str
    evidence: tuple[Evidence, ...]

    def __post_init__(self) -> None:
        if self.subject in ASKED_SUBJECTS:
            raise InferredSubjectRefused(self.subject)
        if not self.evidence:
            raise ValueError(
                f"inference {self.subject!r} must carry the evidence it was drawn from: an "
                "operator cannot approve a claim whose basis is not shown"
            )
        if not self.value.strip():
            raise ValueError(f"inference {self.subject!r} must carry a value")

    def render(self) -> dict[str, Any]:
        """Return the display form, evidence included.

        The value is sanitized rather than wrapped: these are identifier-shaped
        -- a preset name, a host -- and the engine matches on them, so wrapping
        would put rendering on the matching path.
        """
        return {
            "subject": sanitized(self.subject),
            "value": sanitized(self.value),
            "rationale": self.rationale,
            "evidence": [item.render() for item in self.evidence],
        }

    def describe(self) -> str:
        rendered = self.render()
        lines = [f"{rendered['subject']} = {rendered['value']} ({rendered['rationale']})"]
        lines.extend(f"    evidence: {item.describe()}" for item in self.evidence)
        return "\n".join(lines)


@dataclass(frozen=True)
class Question:
    """Something the assistant asks rather than infers.

    *options* is empty for a yes/no confirmation. *because* states why the
    assistant is asking, which for the asked-never-inferred subjects is the
    reason it will not guess and for everything else is what it looked for and
    did not find.
    """

    subject: str
    prompt: str
    because: str
    options: tuple[str, ...] = ()

    def describe(self) -> str:
        choices = f" [{', '.join(self.options)}]" if self.options else " [yes/no]"
        return f"{self.subject}{choices}: {self.prompt} -- {self.because}"


@dataclass(frozen=True)
class PresetOffer:
    """A bundled preset the project's evidence makes applicable.

    Bundled preset names are reserved: a user-defined preset may not reuse one,
    and the validator refuses a document that does. So an offer carries both the
    selection form -- what is written when the preset is used as-is -- and
    :attr:`definition`, the copy an organization renames and edits wholesale. The
    definition is offered *for copying under a name of the operator's own*, which
    :attr:`copy_note` says out loud.
    """

    name: str
    kind: str
    inference: Inference
    prerequisites: PrerequisiteReport = field(default_factory=PrerequisiteReport)

    @property
    def definition(self) -> dict[str, Any] | None:
        """The bundled stages as a renameable definition, workflow presets only."""
        if self.kind != SECTION_WORKFLOW:
            return None
        return workflow_preset_definition(self.name)

    @property
    def copy_note(self) -> str:
        if self.kind != SECTION_WORKFLOW:
            return ""
        return (
            f"{self.name!r} is a bundled preset name and is reserved: to change its stages "
            f"wholesale, copy the definition into {SECTION_WORKFLOW}.presets under a name of "
            "your own, because a user-defined preset that reuses a bundled name is refused"
        )

    def describe(self) -> str:
        lines = [f"offer {self.kind}/{sanitized(self.name)}", self.inference.describe()]
        lines.extend(f"    unmet: {check.describe()}" for check in self.prerequisites.unmet)
        if self.copy_note:
            lines.append(f"    note: {self.copy_note}")
        return "\n".join(lines)


@dataclass(frozen=True)
class SetupPlan:
    """What the assistant inferred, what it offers, and what it still has to ask.

    :attr:`memory_consulted` is recorded rather than assumed. A flow that works
    with memory present and silently produces nothing without it is the failure
    this field exists to make visible: with no memory the plan is smaller and
    says so, and it never reports an inference it could not make.
    """

    project: str
    root: Path
    memory_consulted: bool
    inferences: tuple[Inference, ...] = ()
    offers: tuple[PresetOffer, ...] = ()
    questions: tuple[Question, ...] = ()

    def inference(self, subject: str) -> Inference | None:
        """Return the inference for *subject*, or ``None`` when none was made."""
        for item in self.inferences:
            if item.subject == subject:
                return item
        return None

    def offer(self, kind: str, name: str) -> PresetOffer | None:
        for item in self.offers:
            if item.kind == kind and item.name == name:
                return item
        return None

    def offers_of(self, kind: str) -> tuple[PresetOffer, ...]:
        return tuple(item for item in self.offers if item.kind == kind)

    @property
    def prerequisites(self) -> PrerequisiteReport:
        """Every offered preset's checks, in one report."""
        checks: list[Prerequisite] = []
        for offer in self.offers:
            checks.extend(offer.prerequisites.checks)
        return PrerequisiteReport(checks=tuple(checks))

    def describe(self) -> str:
        lines = [f"project {sanitized(self.project)} at {self.root}"]
        if not self.memory_consulted:
            lines.append(
                "no memory was available, so these inferences come from project files alone"
            )
        lines.extend(item.describe() for item in self.inferences)
        lines.extend(item.describe() for item in self.offers)
        lines.extend(item.describe() for item in self.questions)
        return "\n".join(lines)


@dataclass(frozen=True)
class SetupAnswers:
    """The operator's answers: one per asked question, none of them inferred.

    *confirmations* needs an explicit entry for every rung in
    :data:`CONFIRMED_LEVELS`. A missing entry is not "no", it is unanswered, and
    :func:`apply_setup` refuses rather than choosing which way to read it.

    *approved_subjects* names the inferences the operator accepted. An offer whose
    inference was not approved is not written, so approving nothing writes only
    the answers -- never a preset the operator did not look at.
    """

    cost_profile: str
    confirmations: Mapping[AutonomyLevel, bool]
    approved_subjects: frozenset[str] = frozenset()
    workflow_preset: str | None = None
    watch_source: str | None = None

    @property
    def granted(self) -> tuple[AutonomyLevel, ...]:
        """The confirmed rungs, lowest first."""
        return tuple(level for level in CONFIRMED_LEVELS if self.confirmations.get(level) is True)


@dataclass(frozen=True)
class SetupResult:
    """What was written, and what the written configuration still needs."""

    document: Mapping[str, Any]
    written_paths: tuple[str, ...]
    prerequisites: PrerequisiteReport = field(default_factory=PrerequisiteReport)
    notes: tuple[str, ...] = ()
    #: Advisories the persisted document earned, as the config store raised them.
    #: Carried out of the write rather than dropped at it: an apply that arms
    #: execution autonomy on a publicly submittable source earns one, and the
    #: surface relaying the result is where a human can still be told.
    advisories: tuple[ConfigWarning, ...] = ()

    def describe(self) -> str:
        lines = [
            f"wrote {', '.join(self.written_paths)}" if self.written_paths else "wrote nothing"
        ]
        lines.extend(f"unmet: {check.describe()}" for check in self.prerequisites.unmet)
        lines.extend(f"note: {note}" for note in self.notes)
        lines.extend(f"advisory: {advisory}" for advisory in self.advisories)
        return "\n".join(lines)


@dataclass(frozen=True)
class SetupPatch:
    """The configuration patch an approved plan would write, before it is written.

    Exists so a surface that has to *show* a write before performing it -- a
    configuration preview, an agent that must return a plan and apply it in a
    second call -- reads the same object :func:`apply_setup` writes. A second
    builder for display is how a preview comes to disagree with the write it
    previews, and here the disagreement would be about which commands a project
    executes unattended.
    """

    patch: Mapping[str, Any]
    written_paths: tuple[str, ...]
    notes: tuple[str, ...] = ()
    #: The rungs the answers confirmed, lowest first. Carried because it is the
    #: reason the patch holds an autonomy grid (or the note saying why it does
    #: not), and recomputing it beside the patch would be a second reading of the
    #: same answers.
    granted: tuple[AutonomyLevel, ...] = ()


# --- inspection ------------------------------------------------------------

#: Reads a project's git origin. Injectable so a test describes a repository
#: rather than building one, and so the default stays a file read: a subprocess
#: here would make inspection depend on git being installed to answer a question
#: the repository already writes down.
RemoteResolver = Callable[[], "RemoteOrigin"]


@dataclass(frozen=True)
class RemoteOrigin:
    """What a repository states about its origin remote, and where it states it.

    :attr:`located_at` is empty exactly when no repository config was found, which
    is a different fact from a repository that has one and names no origin. Only
    the second supports an inference: "there is nowhere to push" is something a
    found config says, while an absent config says nothing at all, and citing a
    file that does not exist as evidence is how a flow comes to report an
    inference it could not make.
    """

    url: str = ""
    located_at: str = ""

    @property
    def found(self) -> bool:
        return bool(self.located_at)


def inspect_project(
    root: Path,
    *,
    memory: Mapping[str, str] | None = None,
    remote_url: RemoteResolver | None = None,
) -> tuple[Inference, ...]:
    """Infer what *root* states about its workflow, tracker, and tooling.

    Reads steering files, docs, CI and build configs, and the repository's own
    remote. *memory* is an optional mapping of memory entry name to text; passing
    ``None`` is the supported degraded mode, and the result is then simply the
    subset of inferences project files support.

    Every returned inference carries its evidence, because :class:`Inference`
    cannot be built without it -- so an empty result means nothing was found, and
    never that something was assumed.
    """
    resolve_remote = remote_url or _git_remote_reader(root)
    inferences: list[Inference] = []
    origin = resolve_remote()
    host = _host_of(origin.url) if origin.found else None

    if origin.found:
        remote_evidence = Evidence(
            located_at=origin.located_at,
            excerpt=Untrusted(origin.url or "no origin remote is configured"),
        )
        if host is not None:
            inferences.append(
                Inference(
                    subject=SUBJECT_WATCH_SOURCE,
                    value=host,
                    rationale=f"the origin remote names {host}, which has a bundled watch source",
                    evidence=(remote_evidence,),
                )
            )
            inferences.append(
                Inference(
                    subject=SUBJECT_WORKFLOW_PRESET,
                    value=HOST_WORKFLOW_PRESETS[host],
                    rationale=f"the origin remote is hosted on {host}",
                    evidence=(remote_evidence,),
                )
            )
        elif not origin.url:
            inferences.append(
                Inference(
                    subject=SUBJECT_WORKFLOW_PRESET,
                    value=LOCAL_WORKFLOW_PRESET,
                    rationale="the repository has no origin remote, so there is nowhere to push",
                    evidence=(remote_evidence,),
                )
            )

    practice = _practice_inference(root, memory)
    if practice is not None:
        inferences.append(practice)
    tooling = _tooling_inference(root)
    if tooling is not None:
        inferences.append(tooling)
    return tuple(inferences)


def propose_setup(
    root: Path,
    *,
    project: str,
    memory: Mapping[str, str] | None = None,
    remote_url: RemoteResolver | None = None,
    which: ProgramResolver | None = None,
) -> SetupPlan:
    """Build the plan an operator approves: inferences, offers, and questions.

    *which* resolves a program name on PATH and is injectable for the same reason
    :func:`~.prerequisites.check_project` injects it: the offer-time checks are a
    read of the environment, and a test should describe one rather than arrange
    one. The checks themselves are built by :mod:`.prerequisites`, so the action
    an operator reads here is the action the run gate would refuse with.
    """
    inferences = inspect_project(root, memory=memory, remote_url=remote_url)
    offers = _preset_offers(inferences, which=which)
    questions = _questions(inferences)
    return SetupPlan(
        project=project,
        root=root,
        memory_consulted=bool(memory),
        inferences=inferences,
        offers=offers,
        questions=questions,
    )


def setup_patch(plan: SetupPlan, answers: SetupAnswers) -> SetupPatch:
    """Return the patch *answers* would write for *plan*, applying nothing.

    Every refusal :func:`apply_setup` makes happens here, before a patch exists:
    a cost profile that was not chosen from the bundled names, a rung left
    unanswered, a rung confirmed above a declined one, or a selected preset that
    was never offered and therefore never checked. Each refusal names what is
    missing.

    Pure: it reads the plan and the answers and touches neither the filesystem nor
    the config store. That is what lets a two-step surface show the operator the
    same patch the write will use.
    """
    _require_cost_profile(answers)
    granted = _require_confirmations(answers)

    patch: dict[str, Any] = {}
    written: list[str] = []
    notes: list[str] = []

    project_entry: dict[str, Any] = {
        PROJECT_PATH_FIELD: str(plan.root),
        PROJECT_PROFILE_FIELD: answers.cost_profile,
    }
    patch[SECTION_COST_PROFILES] = {
        answers.cost_profile: cost_profile_presets(answers.cost_profile)
    }
    written.append(f"{SECTION_COST_PROFILES}.{answers.cost_profile}")
    written.append(f"{SECTION_PROJECTS}.{plan.project}.{PROJECT_PROFILE_FIELD}")

    if answers.workflow_preset is not None:
        offer = _approved_offer(plan, answers, SECTION_WORKFLOW, answers.workflow_preset)
        project_entry[SECTION_WORKFLOW] = workflow_presets(offer.name)
        written.append(f"{SECTION_PROJECTS}.{plan.project}.{SECTION_WORKFLOW}")

    patch[SECTION_PROJECTS] = {plan.project: project_entry}

    if answers.watch_source is not None:
        offer = _approved_offer(plan, answers, SECTION_SOURCES, answers.watch_source)
        entry = watch_source_presets(offer.name)
        if granted:
            # The ladder lives on the source, so the confirmations become the
            # broadest grid cell: they were given for the project as a whole and
            # narrowing them to one class or spec type would grant less than was
            # confirmed under a name that reads like more.
            entry[AUTONOMY_FIELD] = {WILDCARD_KEY: {WILDCARD_KEY: granted[-1].value}}
        patch[SECTION_SOURCES] = {offer.name: entry}
        written.append(f"{SECTION_SOURCES}.{offer.name}")
    elif granted:
        notes.append(
            "the confirmed autonomy levels were not written: the ladder is declared per watch "
            f"source at {SECTION_SOURCES}.<name>.{AUTONOMY_FIELD}, and no source was selected"
        )

    return SetupPatch(
        patch=patch,
        written_paths=tuple(written),
        notes=tuple(notes),
        granted=granted,
    )


def apply_setup(
    store: ConfigStore,
    plan: SetupPlan,
    answers: SetupAnswers,
    *,
    surface: ConfigWriteSurface = SETUP_ASSISTANT_SURFACE,
    actor: str | None = None,
    which: ProgramResolver | None = None,
) -> SetupResult:
    """Write *plan*'s approved parts through the validated config path.

    The patch, and every refusal that precedes it, comes from :func:`setup_patch`,
    so what lands is what a caller could have been shown first.

    The patch goes to :meth:`~.config.ConfigStore.write`, which validates the
    merged document and persists it under the lock. Nothing here touches the file.

    *actor* is the human the calling surface says approved the plan; the store
    records it, so the approver an agent-facing apply demands survives the call
    rather than being echoed back and forgotten. The advisories the write earns
    are carried on the result for the same reason: the caller is the last thing
    standing between them and the person who should read them.
    """
    proposed = setup_patch(plan, answers)
    advisories: list[ConfigWarning] = []
    document = store.write(proposed.patch, surface=surface, actor=actor, warn=advisories.append)

    checks: list[Prerequisite] = []
    if answers.watch_source is not None:
        # The authoritative answer for a source that now exists in the document.
        # Offer-time checks looked at a preset nothing had written yet; this looks
        # at configuration, which is what the run gate will read.
        checks.extend(check_source(store, answers.watch_source, which=which).checks)
    return SetupResult(
        document=document,
        written_paths=proposed.written_paths,
        prerequisites=PrerequisiteReport(checks=tuple(checks)),
        notes=proposed.notes,
        advisories=tuple(advisories),
    )


# --- approval gates --------------------------------------------------------


def _require_cost_profile(answers: SetupAnswers) -> None:
    """Refuse a cost profile that was not chosen from the bundled names.

    There is no default and no inference: an unanswered profile is refused rather
    than filled in, because filling it in is exactly the guess this module does
    not make.
    """
    if answers.cost_profile not in COST_PROFILE_PRESET_NAMES:
        raise SetupApprovalRequired(
            f"cost profile must be chosen by the operator from "
            f"{', '.join(COST_PROFILE_PRESET_NAMES)}; got {answers.cost_profile!r}. This is "
            "asked rather than inferred because it decides how much unattended work may spend"
        )


def _require_confirmations(answers: SetupAnswers) -> tuple[AutonomyLevel, ...]:
    """Return the confirmed rungs, refusing an unanswered or inconsistent set.

    Every rung needs its own answer, so an unanswered one is refused rather than
    read as either yes or no. A rung confirmed above a declined one is refused
    too: an enabled level implies every level below it, so writing it would grant
    the rung that was declined.
    """
    missing = [level.value for level in CONFIRMED_LEVELS if level not in answers.confirmations]
    if missing:
        raise SetupApprovalRequired(
            "each autonomy level is confirmed separately and "
            f"{', '.join(missing)} {'is' if len(missing) == 1 else 'are'} unanswered"
        )
    declined: list[str] = []
    for level in CONFIRMED_LEVELS:
        if answers.confirmations.get(level) is True and declined:
            raise SetupApprovalRequired(
                f"{level.value!r} was confirmed while {declined[0]!r} was declined, and an "
                "enabled level authorizes every level below it, so writing it would grant "
                "the declined one"
            )
        if answers.confirmations.get(level) is not True:
            declined.append(level.value)
    return answers.granted


def _approved_offer(plan: SetupPlan, answers: SetupAnswers, kind: str, name: str) -> PresetOffer:
    """Return the offer for *name*, refusing one never offered or not approved."""
    offer = plan.offer(kind, name)
    if offer is None:
        offered = ", ".join(item.name for item in plan.offers_of(kind)) or "none"
        raise SetupApprovalRequired(
            f"{kind} preset {name!r} was not offered for this project, so its prerequisites "
            f"were never checked; offered: {offered}"
        )
    if offer.inference.subject not in answers.approved_subjects:
        raise SetupApprovalRequired(
            f"{kind} preset {name!r} rests on the inference {offer.inference.subject!r}, which "
            "the operator has not approved"
        )
    return offer


# --- offers and questions --------------------------------------------------


def _preset_offers(
    inferences: Sequence[Inference], *, which: ProgramResolver | None
) -> tuple[PresetOffer, ...]:
    """Build an offer per applicable preset, each with its programs checked."""
    offers: list[PresetOffer] = []
    for inference in inferences:
        if inference.subject == SUBJECT_WORKFLOW_PRESET:
            offers.append(
                PresetOffer(
                    name=inference.value,
                    kind=SECTION_WORKFLOW,
                    inference=inference,
                    prerequisites=_workflow_preset_checks(inference.value, which=which),
                )
            )
        elif inference.subject == SUBJECT_WATCH_SOURCE:
            offers.append(
                PresetOffer(
                    name=inference.value,
                    kind=SECTION_SOURCES,
                    inference=inference,
                    prerequisites=_watch_preset_checks(inference.value, which=which),
                )
            )
    return tuple(offers)


def _workflow_preset_checks(name: str, *, which: ProgramResolver | None) -> PrerequisiteReport:
    """Check every program the bundled workflow *name* would invoke.

    Built by :mod:`.prerequisites`, at the phase that module already assigns each
    delivery stage, so the unmet report an operator reads before approving uses
    the same wording and the same resolving action the run gate refuses with.
    The input differs from :func:`~.prerequisites.check_project` -- a preset
    nothing has written yet rather than a configured workflow -- which is why the
    argv comes from here and the verdict does not.
    """
    resolve = which or _default_program_resolver()
    stages = WORKFLOW_PRESETS[name]
    checks: list[Prerequisite] = []
    seen: set[tuple[str, str]] = set()
    for stage, commands in stages.items():
        for argv in commands:
            if not argv:
                continue
            program = str(argv[0])
            if (stage, program) in seen:
                continue
            seen.add((stage, program))
            checks.append(
                program_check(
                    program,
                    phase=stage_phase(stage),
                    declared_at=f"{SECTION_WORKFLOW}.{name}.{stage}",
                    used_for=f"workflow preset {name!r} stage {stage!r}",
                    which=resolve,
                )
            )
    return PrerequisiteReport(checks=tuple(checks))


def _watch_preset_checks(host: str, *, which: ProgramResolver | None) -> PrerequisiteReport:
    """Check the program the bundled watch source for *host* would poll with."""
    resolve = which or _default_program_resolver()
    program = WATCH_SOURCE_PRESET_PROGRAMS[host]
    return PrerequisiteReport(
        checks=(
            program_check(
                program,
                phase=AutonomyLevel.AUTHORING,
                declared_at=f"{SECTION_SOURCES}.{host}",
                used_for=f"watch source preset {host!r}",
                which=resolve,
            ),
        )
    )


def _default_program_resolver() -> ProgramResolver:
    """Return ``shutil.which``, imported here so this module holds no PATH logic."""
    import shutil

    return shutil.which


def _questions(inferences: Sequence[Inference]) -> tuple[Question, ...]:
    """Build the questions: the asked-never-inferred ones, plus what was not found."""
    questions: list[Question] = [
        Question(
            subject=SUBJECT_COST_PROFILE,
            prompt=(
                "Which cost profile should this project use? 'quality-first' runs more of a "
                "wave at once and allows a run more credits; 'budget' runs one task at a time "
                "and holds a run to fewer."
            ),
            because=(
                "this decides how much money unattended work may spend, so it is asked rather "
                "than inferred from the project"
            ),
            options=COST_PROFILE_PRESET_NAMES,
        )
    ]
    questions.extend(
        Question(
            subject=f"{AUTONOMY_FIELD}.{level.value}",
            prompt=LEVEL_PROMPTS[level],
            because="each level is confirmed separately because each grants something different",
        )
        for level in CONFIRMED_LEVELS
    )
    subjects = {item.subject for item in inferences}
    if SUBJECT_WORKFLOW_PRESET not in subjects:
        questions.append(
            Question(
                subject=SUBJECT_WORKFLOW_PRESET,
                prompt=(
                    "How should completed work be delivered? Bundled presets: "
                    f"{', '.join(WORKFLOW_PRESET_NAMES)}."
                ),
                because=(
                    "no repository remote was found to infer from, and delivery runs commands "
                    "that must not be guessed at"
                ),
                options=WORKFLOW_PRESET_NAMES,
            )
        )
    if SUBJECT_WATCH_SOURCE not in subjects:
        questions.append(
            Question(
                subject=SUBJECT_WATCH_SOURCE,
                prompt=(
                    "Which tracker should the engine watch for work, if any? Bundled presets "
                    f"exist for {', '.join(WATCH_SOURCE_PRESET_HOSTS)}; another tracker is "
                    "configured by writing its poll command and field map."
                ),
                because=(
                    "the repository's remote does not name a host with a bundled preset, so "
                    "there is nothing to infer from"
                ),
                options=WATCH_SOURCE_PRESET_HOSTS,
            )
        )
    if SUBJECT_TOOLING not in subjects:
        questions.append(
            Question(
                subject=SUBJECT_TOOLING,
                prompt="What commands build and test this project?",
                because=(
                    "no Makefile target, package script, or CI job named a build or test entry "
                    "point, so none was inferred"
                ),
            )
        )
    return tuple(questions)


# --- reading the project ---------------------------------------------------


def _git_remote_reader(root: Path) -> RemoteResolver:
    """Return a reader for *root*'s origin remote URL, empty when there is none.

    Reads the repository's own config file rather than running git: the answer is
    written down, and a subprocess would make inspection depend on a program being
    installed to learn something the file states. Follows the ``gitdir:`` pointer
    a linked worktree leaves behind, because a worktree's ``.git`` is a file and
    the remotes live in the common directory it points at.
    """

    def read() -> RemoteOrigin:
        config = _git_config_path(root)
        if config is None or not config.is_file():
            return RemoteOrigin()
        located_at = _relative_to(config, root)
        in_origin = False
        for line in _lines(config):
            section = _REMOTE_SECTION.match(line)
            if section is not None:
                in_origin = section.group(1) == _ORIGIN_REMOTE
                continue
            if line.lstrip().startswith("["):
                in_origin = False
                continue
            if not in_origin:
                continue
            url = _REMOTE_URL.match(line)
            if url is not None:
                return RemoteOrigin(url=url.group(1), located_at=located_at)
        return RemoteOrigin(located_at=located_at)

    return read


def _relative_to(path: Path, root: Path) -> str:
    """Return *path* relative to *root* when it is inside it, else the full path.

    A linked worktree's config lives outside the tree being set up, and reporting
    it as an absolute path is more honest than a chain of parent references.
    """
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _git_config_path(root: Path) -> Path | None:
    """Return the config file holding *root*'s remotes, or ``None``."""
    git = root / ".git"
    if git.is_dir():
        return git / "config"
    if not git.is_file():
        return None
    for line in _lines(git):
        if not line.startswith("gitdir:"):
            continue
        pointer = Path(line.split(":", 1)[1].strip())
        gitdir = pointer if pointer.is_absolute() else (root / pointer)
        common = gitdir / "commondir"
        if common.is_file():
            relative = "".join(_lines(common)).strip()
            if relative:
                return (gitdir / relative).resolve() / "config"
        return gitdir / "config"
    return None


def _host_of(remote: str) -> str | None:
    """Return the bundled preset host *remote* names, or ``None``.

    Matched on the URL text because both forms a git remote takes -- the SSH
    ``git@host:owner/repo`` and the HTTPS URL -- name the host in it, and parsing
    each form separately would be two answers to one question.
    """
    if not remote:
        return None
    lowered = remote.lower()
    for marker, host in REMOTE_HOSTS.items():
        if marker in lowered:
            return host
    return None


def _practice_inference(root: Path, memory: Mapping[str, str] | None) -> Inference | None:
    """Infer a review practice from steering, docs, and memory prose.

    Corroboration for the remote's answer rather than a competing one: it reports
    what the project *says* it does, with the sentence that says it, so an
    operator sees whether the practice the workflow preset implements is the one
    written down.
    """
    hits: dict[str, list[Evidence]] = {}
    for located_at, text in _prose_sources(root, memory):
        lowered = text.lower()
        for phrase, preset in PRACTICE_PHRASES.items():
            if phrase not in lowered:
                continue
            excerpt = _excerpt_containing(text, phrase)
            if excerpt is None:
                continue
            hits.setdefault(preset, []).append(
                Evidence(located_at=located_at, excerpt=Untrusted(excerpt))
            )
    if not hits:
        return None
    preset, evidence = max(hits.items(), key=lambda item: len(item[1]))
    return Inference(
        subject=SUBJECT_WORKFLOW_PRACTICE,
        value=preset,
        rationale="the project's own documentation describes this review practice",
        evidence=tuple(evidence[:MAX_FILES_PER_DIR]),
    )


def _tooling_inference(root: Path) -> Inference | None:
    """Infer the build and test entry points the project already has."""
    found: list[str] = []
    evidence: list[Evidence] = []
    makefile = root / BUILD_FILES[0]
    for line in _lines(makefile):
        matched = _MAKE_TARGET.match(line)
        if matched is None:
            continue
        target = matched.group(1)
        if target not in TOOLING_TARGETS or target in found:
            continue
        found.append(target)
        evidence.append(Evidence(located_at=BUILD_FILES[0], excerpt=Untrusted(line.rstrip())))
    for name in BUILD_FILES[1:]:
        path = root / name
        if not path.is_file():
            continue
        text = _read(path)
        for declared in TOOLING_TARGETS:
            quoted = f'"{declared}"'
            if quoted not in text or declared in found:
                continue
            excerpt = _excerpt_containing(text, quoted)
            if excerpt is None:
                continue
            found.append(declared)
            evidence.append(Evidence(located_at=name, excerpt=Untrusted(excerpt)))
    if not evidence:
        return None
    return Inference(
        subject=SUBJECT_TOOLING,
        value=", ".join(found),
        rationale="these entry points are declared in the project's own build files",
        evidence=tuple(evidence),
    )


def _prose_sources(root: Path, memory: Mapping[str, str] | None) -> Iterator[tuple[str, str]]:
    """Yield (location, text) for every prose source, memory last.

    Memory comes last so a project file's evidence is what an operator reads
    first, and its absence changes nothing about the files: this is the seam that
    makes "operate from project files alone" the same code path with one input
    missing rather than a second flow.
    """
    for path in _files_in(root / STEERING_DIR):
        yield str(path.relative_to(root)), _read(path)
    for name in DOC_FILES:
        path = root / name
        if path.is_file():
            yield name, _read(path)
    for path in _files_in(root / CI_DIR):
        yield str(path.relative_to(root)), _read(path)
    for name in CI_FILES:
        path = root / name
        if path.is_file():
            yield name, _read(path)
    for name, text in (memory or {}).items():
        yield f"memory:{name}", text


def _files_in(directory: Path) -> tuple[Path, ...]:
    """Return up to :data:`MAX_FILES_PER_DIR` files in *directory*, sorted."""
    if not directory.is_dir():
        return ()
    try:
        entries = sorted(item for item in directory.iterdir() if item.is_file())
    except OSError:
        return ()
    return tuple(entries[:MAX_FILES_PER_DIR])


def _read(path: Path) -> str:
    """Return up to :data:`MAX_FILE_BYTES` of *path*, empty when unreadable.

    Unreadable is not an error: setup reads whatever the project happens to have,
    and a permission-denied steering file is one fewer piece of evidence rather
    than a failed setup.
    """
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            return handle.read(MAX_FILE_BYTES)
    except OSError:
        return ""


def _lines(path: Path) -> Iterable[str]:
    return _read(path).splitlines()


def _excerpt_containing(text: str, needle: str) -> str | None:
    """Return the first line holding *needle*, or ``None``.

    A line rather than the whole file: the excerpt is what an operator reads to
    check the inference, and the sentence that produced it is the part that
    checks it.
    """
    lowered_needle = needle.lower()
    for line in text.splitlines():
        if lowered_needle in line.lower():
            stripped = line.strip()
            if stripped:
                return stripped
    return None
