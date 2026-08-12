"""The bundled analyzer: mechanical spec analysis with nothing configured.

Five deterministic checks over the native documents — glossary terms used but
never defined, qualifiers that promise a quality with no measurable bound,
criteria nothing could independently test, requirements no task claims, and
criteria inside one requirement that overlap or contradict each other. Each runs
as text and arithmetic, so the analyzer reaches no network, spawns no child, and
spends nothing.

Two properties shape the code more than the checks do.

**It is honest about its depth.** Every response declares ``structural`` and
carries a coverage block naming the defect classes this depth cannot see, on a
clean pass exactly as on a dirty one. An empty findings list here means these
checks found nothing; it never means the spec is correct. That is why the blind
spots are in the envelope rather than in a docstring: a surface renders coverage,
and a reader who sees "no findings" with nothing beside it will finish the
sentence themselves.

**It is quiet on clean documents.** A check that fires on a format-clean spec
teaches an author to ignore the analyzer, which costs more than the check earns.
So the vocabularies are narrow, a qualifier with a bound stated anywhere in its
criterion is not reported, a plural of a defined glossary term is defined, and
the overlap comparison demands that two criteria share a trigger, a subject, and
a verb before it says anything. Each of those concessions was made against the
repository's own spec.

Parsing is reused rather than repeated: :mod:`.structure` already reads
requirements into criteria and tasks into leaves, and :mod:`.cross_document`
already answers which requirements no task claims. A second parser would drift
from the validator's reading of the same line.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from . import rules
from .capabilities.contracts import (
    CapabilityRequest,
    CapabilityResponse,
    ClarifyingQuestion,
    Coverage,
    FindingSeverity,
    ProviderFinding,
    ProviderIdentity,
    ProviderNature,
    SkippedItem,
    Untrusted,
)
from .capabilities.providers import builtin_identity
from .cross_document import check_requirement_coverage
from .documents import DocumentKind, normalize_heading
from .findings import Violation
from .native_format import HEADING_RE, NUMBERED_ITEM_RE
from .structure import Criterion, RequirementsIndex, TaskPlan, parse_requirements, parse_tasks, scan

#: The capability this analyzer serves.
CAPABILITY = "analysis"

#: Name the analyzer reports itself under, in the audit log and the UI.
PROVIDER_NAME = "local-analyzer"

#: The depth every response declares. The other rungs of the ladder belong to
#: the model-backed and external providers; this one never claims either.
DEPTH_STRUCTURAL = "structural"

# --- Finding kinds ---------------------------------------------------------
#
# Stable identifiers, prefixed to keep them distinguishable from the engine's
# own ``native.`` rules: these are an analyzer's opinion, not a format rule, and
# nothing here decides a gate.

#: A glossary-shaped term is used but the glossary defines no such entry.
KIND_TERM_UNDEFINED = "analysis.glossary.term-undefined"
#: A criterion promises a quality with no measurable bound.
KIND_QUALIFIER_UNQUANTIFIED = "analysis.criterion.qualifier-unquantified"
#: A criterion carries language that leaves nothing to test against.
KIND_NOT_TESTABLE = "analysis.criterion.not-independently-testable"
#: No task claims any part of a requirement.
KIND_REQUIREMENT_UNCOVERED = "analysis.coverage.requirement-uncovered"
#: A requirement is worked on but one of its criteria is claimed by no task.
KIND_CRITERION_UNCOVERED = "analysis.coverage.criterion-uncovered"
#: Two criteria in one requirement state the same obligation under the same
#: trigger.
KIND_CRITERIA_OVERLAP = "analysis.criteria.overlapping"
#: Two criteria in one requirement demand opposite things under the same
#: trigger.
KIND_CRITERIA_CONTRADICT = "analysis.criteria.contradictory"

#: Every kind this analyzer can emit, in the order the checks run. Held as data
#: so the declared coverage is generated from the same list the checks are, and
#: a check added without a coverage entry is not possible.
ALL_KINDS: tuple[str, ...] = (
    KIND_TERM_UNDEFINED,
    KIND_QUALIFIER_UNQUANTIFIED,
    KIND_NOT_TESTABLE,
    KIND_REQUIREMENT_UNCOVERED,
    KIND_CRITERION_UNCOVERED,
    KIND_CRITERIA_OVERLAP,
    KIND_CRITERIA_CONTRADICT,
)

#: Prefix under which a processed or skipped entry names a check.
CHECK_PREFIX = "check:"
#: Prefix under which a processed or skipped entry names a document.
DOCUMENT_PREFIX = "document:"
#: Prefix under which a skipped entry names a defect class the depth cannot see.
BLIND_SPOT_PREFIX = "beyond-depth:"

#: What structural depth does not examine, declared on every response.
#:
#: Not a caveat about this implementation — a statement of what the depth is. A
#: deeper provider answers these; nothing lexical does, and a response that
#: stayed silent about them would let a clean pass be read as a clean spec.
STRUCTURAL_BLIND_SPOTS: tuple[tuple[str, str], ...] = (
    (
        "requirement intent",
        "whether a criterion states what its author meant is a question about "
        "meaning, and no lexical check reads meaning",
    ),
    (
        "design adequacy",
        "whether the design actually satisfies the requirements compares the "
        "content of two documents rather than the shape of either",
    ),
    (
        "cross-requirement consistency",
        "criteria are compared for contradiction only within one requirement; "
        "two requirements that disagree read as ordinary prose here",
    ),
    (
        "domain correctness",
        "whether a stated bound, identifier, or rule is the right one needs "
        "knowledge of the domain the spec describes",
    ),
)

# --- Lexical vocabularies --------------------------------------------------
#
# Narrow on purpose. Each entry was checked against the repository's own spec,
# which is format-clean prose: a word that occurs there innocently is not a
# defect signal, however unquantified it reads in isolation.

#: Qualifiers that promise a quality without saying how much of it. Mapped to
#: the noun the generated question asks the author to bound, because "how fast
#: is fast" and "reliable in what respect" are different questions.
QUALIFIERS: Mapping[str, str] = {
    "fast": "speed",
    "quickly": "speed",
    "slow": "speed",
    "responsive": "response time",
    "timely": "timing",
    "promptly": "timing",
    "reliable": "reliability",
    "reliably": "reliability",
    "robust": "tolerance for failure",
    "scalable": "the load it carries",
    "performant": "throughput or latency",
    "efficient": "resource use",
    "efficiently": "resource use",
    "minimal": "the amount",
    "reasonable": "the acceptable range",
    "reasonably": "the acceptable range",
    "appropriate": "the acceptable range",
    "appropriately": "the acceptable range",
    "sufficient": "how much is enough",
    "sufficiently": "how much is enough",
    "adequate": "how much is enough",
    "adequately": "how much is enough",
    "acceptable": "the acceptable range",
    "optimal": "what is being optimised, and against what",
    "user-friendly": "the usability property claimed",
    "intuitive": "the usability property claimed",
    "seamless": "the property claimed",
    "seamlessly": "the property claimed",
    "large": "the size",
    "small": "the size",
    "many": "the count",
    "few": "the count",
}

#: Words and phrases that state a bound. One of these anywhere in the criterion
#: suppresses a qualifier finding for that criterion: "up to a configured
#: concurrency cap" is quantified by its configuration, and reporting it would
#: teach an author that the check does not read the sentence it fired on.
BOUND_MARKERS: tuple[str, ...] = (
    "configured",
    "configurable",
    "configuration",
    "limit",
    "cap",
    "ceiling",
    "threshold",
    "budget",
    "deadline",
    "timeout",
    "at most",
    "at least",
    "no more than",
    "no fewer than",
    "no later than",
    "within",
    "exactly",
    "per second",
    "per minute",
)

#: Language that excuses the obligation from happening, so no run of the system
#: can fail the criterion. An obligation that holds only where convenient states
#: a preference, and a preference has no test.
ESCAPE_HATCHES: tuple[str, ...] = (
    "if possible",
    "where possible",
    "when possible",
    "wherever possible",
    "as possible",
    "if applicable",
    "where applicable",
    "as applicable",
    "if appropriate",
    "where appropriate",
    "as appropriate",
    "if needed",
    "as needed",
    "if necessary",
    "as necessary",
    "when feasible",
    "if feasible",
    "best effort",
    "best-effort",
    "on a best effort basis",
    "to the extent possible",
)

#: Enumerations that decline to end. A test suite has to enumerate what it
#: checks, so a criterion whose list trails off cannot be covered by one.
OPEN_ENDED: tuple[str, ...] = (
    "etc.",
    "etc)",
    " etc ",
    "and so on",
    "and more",
    "and others",
    "among others",
    "among other things",
    "or similar",
    "or the like",
    "and the like",
)

#: Obligations deferred to a decision nobody has taken yet. Legitimate in a
#: draft, untestable by construction: there is no stated behaviour to check.
DEFERRED: tuple[str, ...] = (
    "tbd",
    "to be determined",
    "to be defined",
    "to be decided",
    "to be specified",
    "to be documented",
    "to be confirmed",
)

#: Predicates that assert the outcome is right without saying what right is.
#: "SHALL behave correctly" asks the test author to supply the requirement.
UNOBSERVABLE: tuple[str, ...] = (
    "correctly",
    "properly",
    "as expected",
    "as intended",
    "as desired",
    "gracefully",
    "make sense",
    "works well",
    "work well",
)

#: The untestability families, each with the phrases that trigger it and the
#: engine-authored explanation the finding carries.
_UNTESTABLE_FAMILIES: tuple[tuple[str, tuple[str, ...], str], ...] = (
    (
        "escape hatch",
        ESCAPE_HATCHES,
        "the obligation applies only where something is convenient, so no run "
        "of the system can fail it",
    ),
    (
        "open-ended enumeration",
        OPEN_ENDED,
        "the enumeration does not end, so a test cannot cover what it lists",
    ),
    (
        "deferred definition",
        DEFERRED,
        "the behaviour is deferred to a decision nobody has recorded, so there "
        "is nothing yet to verify",
    ),
    (
        "unobservable outcome",
        UNOBSERVABLE,
        "the outcome is asserted to be right without saying what right is, so a "
        "test would have to supply the requirement itself",
    ),
)

# --- Lexical shapes -------------------------------------------------------

#: An inline code span. Stripped before terms are collected: a token quoted as
#: code names a symbol in some other system, not a concept this spec defines.
_CODE_SPAN_RE = re.compile(r"`[^`]*`")

#: A glossary-shaped term: underscore-joined segments each opening with a
#: capital. Filtered afterwards for at least one lowercase letter, which is what
#: separates a defined concept from a screaming-case constant.
_TERM_RE = re.compile(r"\b[A-Z][A-Za-z0-9]*(?:_[A-Z][A-Za-z0-9]*)+\b")

#: A glossary entry: a bulleted, bold term followed by its definition.
_GLOSSARY_ENTRY_RE = re.compile(r"^ *[-*+] +\*\*(?P<term>[^*]+?)\*\* *:")

#: The section that carries the definitions.
_GLOSSARY_HEADING = "glossary"

#: A criterion's leading EARS condition keyword. ``THE`` is absent on purpose:
#: it opens an unconditional obligation, which has no condition clause to read.
_CONDITION_OPENER_RE = re.compile(r"^(?P<opener>FOR ALL|WHEN|IF|WHILE|WHERE)\b(?P<rest>.*)$", re.S)

#: Where a leading condition clause ends and the obligation begins.
_CONDITION_END_RE = re.compile(r",\s*(?:THEN\b|THE\b)|\bTHEN\b")

#: One obligation: a subject, the modal, and what it must (not) do. The subject
#: is lazy so it stops at the first ``SHALL`` rather than swallowing a later one.
_OBLIGATION_RE = re.compile(
    r"\bTHE\s+(?P<subject>[\w][\w-]*(?:\s+[\w][\w-]*){0,4}?)"
    r"\s+SHALL(?P<negation>\s+NOT)?\b(?P<tail>[^.;]*)"
)

#: Tokens that carry no meaning for comparison. Negation is deliberately absent:
#: two conditions that differ only by "not" are different conditions.
_STOPWORDS: frozenset[str] = frozenset(
    {
        "a",
        "an",
        "and",
        "any",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "has",
        "have",
        "in",
        "is",
        "it",
        "its",
        "of",
        "on",
        "or",
        "own",
        "that",
        "the",
        "their",
        "this",
        "to",
        "with",
    }
)

#: Content tokens of an obligation compared as its verb. Two is enough to
#: separate "read the workflow" from "bundle editable presets" and short enough
#: that a rewording of the object does not hide a genuine duplicate.
_VERB_HEAD_TOKENS = 2

#: Cap on how many criteria one finding lists. A term used in forty criteria is
#: one defect; forty references in the message bury it.
MAX_REFS = 12

_WORD_RE = re.compile(r"[a-z0-9][a-z0-9_-]*")


@dataclass(frozen=True)
class Corpus:
    """The document text the analyzer was given, per native document.

    ``None`` means the document was not supplied, which is different from an
    empty one: absent means the checks that need it declare themselves skipped,
    while empty means they ran and found nothing to read.
    """

    requirements: str | None = None
    design: str | None = None
    tasks: str | None = None

    def text(self, kind: DocumentKind) -> str | None:
        return {
            DocumentKind.REQUIREMENTS: self.requirements,
            DocumentKind.DESIGN: self.design,
            DocumentKind.TASKS: self.tasks,
        }[kind]

    @property
    def present(self) -> tuple[DocumentKind, ...]:
        return tuple(kind for kind in DocumentKind if self.text(kind) is not None)


@dataclass(frozen=True)
class Outcome:
    """What one analysis pass found, and what it declared it did not look at."""

    findings: tuple[ProviderFinding, ...] = ()
    coverage: Coverage = field(default_factory=Coverage)

    def of_kind(self, kind: str) -> tuple[ProviderFinding, ...]:
        return tuple(finding for finding in self.findings if finding.kind == kind)

    @property
    def kinds(self) -> tuple[str, ...]:
        seen: dict[str, None] = {}
        for finding in self.findings:
            seen.setdefault(finding.kind, None)
        return tuple(seen)


# --- Shared reading -------------------------------------------------------


def _content_lines(text: str) -> dict[int, str]:
    """Map line number to text for every line outside a fenced block."""
    lines, _ = scan(text)
    return {line.number: line.text for line in lines}


def _criterion_text(lines: Mapping[int, str], criterion: Criterion) -> str:
    """The criterion's own sentence, including any lines it wraps onto.

    A wrapped criterion is one criterion. Reading only its first line would let
    a qualifier or an escape hatch on the second line go unseen, which is a
    silent miss rather than a visible one.
    """
    first = lines.get(criterion.line, "")
    item = NUMBERED_ITEM_RE.match(first)
    parts = [item.group("body") if item is not None else first.strip()]
    number = criterion.line + 1
    while number in lines:
        following = lines[number].strip()
        if not following:
            break
        if NUMBERED_ITEM_RE.match(lines[number]) or HEADING_RE.match(lines[number]):
            break
        if following.startswith(("-", "*", "+", "#")):
            break
        parts.append(following)
        number += 1
    return " ".join(part for part in parts if part).strip()


def _criteria_texts(index: RequirementsIndex, text: str) -> dict[str, str]:
    """Every criterion's sentence, keyed by its identifier."""
    lines = _content_lines(text)
    return {
        criterion.identifier: _criterion_text(lines, criterion)
        for requirement in index
        for criterion in requirement.criteria
    }


def _tokens(text: str) -> tuple[str, ...]:
    """Content tokens of ``text``, folded for comparison."""
    return tuple(
        word for word in _WORD_RE.findall(text.casefold()) if word and word not in _STOPWORDS
    )


def _contains(haystack: str, needles: Iterable[str]) -> str:
    """The first needle present in ``haystack``, or an empty string.

    Longest first, so a finding quotes the most specific phrase it matched: "on
    a best effort basis" is what the author wrote, and reporting the "best
    effort" inside it describes the vocabulary rather than the document.

    Word-bounded for single words so that "capacity" does not read as "cap", and
    plain substring for phrases, which already carry their own boundaries.
    """
    folded = haystack.casefold()
    for needle in sorted(needles, key=len, reverse=True):
        if " " in needle or not needle.isalnum():
            if needle in folded:
                return needle
        elif re.search(rf"\b{re.escape(needle)}\b", folded):
            return needle
    return ""


def _has_number(text: str) -> bool:
    return any(character.isdigit() for character in text)


def _question(
    question: str,
    choices: Sequence[str],
    consequences: Sequence[str],
    recommended: str,
) -> ClarifyingQuestion:
    """Build a clarifying question from engine-authored text.

    A finding that admits a human decision is worth asking about rather than
    only reporting: the analyzer can see that a bound is missing but not which
    bound was meant, and a question with the options and their costs is the
    difference between a defect list and something an author can answer.
    """
    return ClarifyingQuestion(
        question=Untrusted(question),
        choices=tuple(Untrusted(choice) for choice in choices),
        consequences=tuple(Untrusted(item) for item in consequences),
        recommended=Untrusted(recommended),
    )


def _finding(
    kind: str,
    severity: FindingSeverity,
    message: str,
    refs: Sequence[str] = (),
    question: ClarifyingQuestion | None = None,
) -> ProviderFinding:
    return ProviderFinding(
        kind=kind,
        severity=severity,
        message=Untrusted(message),
        refs=tuple(refs[:MAX_REFS]),
        question=question,
    )


# --- Check: glossary terms used but never defined --------------------------


def _singular_forms(term: str) -> tuple[str, ...]:
    """Spellings of ``term`` that would name the same concept in the singular.

    A plural of a defined term is defined. Without this the repository's own
    spec reports nine undefined terms, every one of them the plural of an entry
    two lines above — findings that would train an author to stop reading them.
    """
    forms = [term]
    if term.endswith("ies"):
        forms.append(term[:-3] + "y")
    if term.endswith("es"):
        forms.append(term[:-2])
    if term.endswith("s"):
        forms.append(term[:-1])
    return tuple(forms)


def glossary_terms_defined(text: str) -> frozenset[str] | None:
    """The terms the glossary section of ``text`` defines.

    ``None`` when the document declares no glossary section at all. That is not
    the same as an empty glossary: a spec that never opened one has not left its
    vocabulary undefined, it has chosen not to keep one, and reporting every
    capitalised term in it would be a page of findings about a decision.
    """
    lines, _ = scan(text)
    in_glossary = False
    level = 0
    defined: set[str] = set()
    found = False
    for line in lines:
        heading = HEADING_RE.match(line.text)
        if heading is not None:
            depth = len(heading.group("hashes"))
            if in_glossary and depth <= level:
                in_glossary = False
                continue
            if normalize_heading(heading.group("text")) == _GLOSSARY_HEADING:
                in_glossary = True
                found = True
                level = depth
            continue
        if not in_glossary:
            continue
        entry = _GLOSSARY_ENTRY_RE.match(line.text)
        if entry is not None:
            defined.add(entry.group("term").strip())
    return frozenset(defined) if found else None


def _term_uses(text: str) -> dict[str, list[int]]:
    """Every glossary-shaped term in ``text``, mapped to the lines using it."""
    uses: dict[str, list[int]] = {}
    lines, _ = scan(text)
    for line in lines:
        stripped = _CODE_SPAN_RE.sub(" ", line.text)
        for token in _TERM_RE.findall(stripped):
            if not any(character.islower() for character in token):
                # A screaming-case constant names a symbol, not a concept the
                # glossary owes a definition for.
                continue
            uses.setdefault(token, []).append(line.number)
    return uses


def check_glossary_terms(corpus: Corpus, index: RequirementsIndex) -> tuple[ProviderFinding, ...]:
    """Report glossary-shaped terms used in the spec but defined nowhere."""
    requirements = corpus.requirements
    if requirements is None:
        return ()
    defined = glossary_terms_defined(requirements)
    if defined is None:
        return ()
    criteria_lines = {
        criterion.line: criterion.identifier
        for requirement in index
        for criterion in requirement.criteria
    }
    findings: list[ProviderFinding] = []
    for document, text in (
        (DocumentKind.REQUIREMENTS, requirements),
        (DocumentKind.DESIGN, corpus.design),
    ):
        if text is None:
            continue
        for term, lines in sorted(_term_uses(text).items()):
            if any(form in defined for form in _singular_forms(term)):
                continue
            refs = sorted({criteria_lines[line] for line in lines if line in criteria_lines})
            where = ", ".join(str(line) for line in lines[:MAX_REFS])
            findings.append(
                _finding(
                    KIND_TERM_UNDEFINED,
                    FindingSeverity.WARNING,
                    f"{term} is used in {document.filename} (line {where}) but the "
                    f"glossary defines no such term. A term that reads as defined "
                    f"and is not leaves each reader to guess its scope.",
                    refs=refs,
                    question=_question(
                        f"What is {term}?",
                        (
                            f"Add a glossary entry defining {term}.",
                            "Replace the term with an existing glossary entry it duplicates.",
                            "Rewrite the sentence in plain words, so no defined term is implied.",
                        ),
                        (
                            "Defining it fixes the scope for every later reader and for "
                            "any provider that reasons over the document.",
                            "Replacing it removes a second name for one concept, which is "
                            "where two readers quietly disagree.",
                            "Rewriting it is right when the capitalisation was accidental "
                            "and no concept was meant.",
                        ),
                        f"Add a glossary entry for {term} unless it duplicates an "
                        f"existing entry, in which case use that entry's name.",
                    ),
                )
            )
    return tuple(findings)


# --- Check: unquantified qualifiers ---------------------------------------


def check_qualifiers(texts: Mapping[str, str]) -> tuple[ProviderFinding, ...]:
    """Report criteria promising a quality with no measurable bound."""
    findings: list[ProviderFinding] = []
    for identifier, text in texts.items():
        qualifier = _contains(text, QUALIFIERS)
        if not qualifier:
            continue
        if _has_number(text) or _contains(text, BOUND_MARKERS):
            # The criterion states a bound somewhere. Whether it is the right
            # bound is a question about the domain, which is past this depth.
            continue
        subject = QUALIFIERS[qualifier]
        findings.append(
            _finding(
                KIND_QUALIFIER_UNQUANTIFIED,
                FindingSeverity.WARNING,
                f"Criterion {identifier} promises {qualifier!r} without stating "
                f"{subject}. Two readers will hold the system to two different "
                f"standards, and neither can write a test that settles it.",
                refs=(identifier,),
                question=_question(
                    f"What measurable bound does {qualifier!r} mean in criterion {identifier}?",
                    (
                        f"State {subject} as a number with its unit in the criterion.",
                        "Point the criterion at a configured limit, so the bound is "
                        "operator-owned and still testable.",
                        "Drop the qualifier, where the obligation is complete without it.",
                    ),
                    (
                        "A stated number makes the criterion testable and fixes the "
                        "target for whoever implements it.",
                        "A configured limit keeps the bound tunable per project while "
                        "leaving the criterion verifiable against the configuration.",
                        "Dropping the word costs nothing when it added no obligation, "
                        "and removes an argument later.",
                    ),
                    f"State {subject} as a number with its unit, or name the "
                    f"configured limit that carries it.",
                ),
            )
        )
    return tuple(findings)


# --- Check: criteria that are not independently testable -------------------


def check_testability(texts: Mapping[str, str]) -> tuple[ProviderFinding, ...]:
    """Report criteria whose own language leaves nothing to test against."""
    findings: list[ProviderFinding] = []
    for identifier, text in texts.items():
        for family, phrases, explanation in _UNTESTABLE_FAMILIES:
            phrase = _contains(text, phrases)
            if not phrase:
                continue
            findings.append(
                _finding(
                    KIND_NOT_TESTABLE,
                    FindingSeverity.WARNING,
                    f"Criterion {identifier} is not independently testable: "
                    f"{phrase.strip()!r} is {family}, and {explanation}.",
                    refs=(identifier,),
                    question=_question(
                        f"What would a passing test for criterion {identifier} check?",
                        (
                            f"Rewrite the criterion so the obligation holds "
                            f"unconditionally, removing {phrase.strip()!r}.",
                            "State the observable outcome that distinguishes a pass "
                            "from a failure.",
                            "Split the criterion, so each obligation it bundles can be "
                            "verified on its own.",
                        ),
                        (
                            "An unconditional obligation can fail, which is what makes "
                            "it verifiable at all.",
                            "A named observable turns the criterion into a test rather "
                            "than a preference.",
                            "Splitting it lets a verdict say which obligation failed "
                            "instead of only that something did.",
                        ),
                        "State the observable outcome a test would assert, and remove "
                        "the language that excuses the obligation from holding.",
                    ),
                )
            )
            break
    return tuple(findings)


# --- Check: requirements no task covers ------------------------------------

#: Engine coverage rule to the analyzer kind and severity that report it. The
#: coverage answer comes from the engine's own cross-document check rather than
#: being recomputed here: two implementations of "which requirement does no task
#: claim" would eventually disagree, and the engine's is the one a gate reads.
_COVERAGE_KINDS: Mapping[str, tuple[str, FindingSeverity]] = {
    rules.COVERAGE_REQUIREMENT_UNCOVERED: (KIND_REQUIREMENT_UNCOVERED, FindingSeverity.ERROR),
    rules.COVERAGE_CRITERION_UNCOVERED: (KIND_CRITERION_UNCOVERED, FindingSeverity.WARNING),
}


def _coverage_refs(index: RequirementsIndex, violation: Violation) -> tuple[str, ...]:
    """The requirement or criterion a coverage violation addresses.

    Recovered from the line the engine reported, so the reference is the same
    object the violation pointed at rather than a second parse of the message.
    """
    for requirement in index:
        if requirement.line == violation.location.line:
            return (str(requirement.number),)
        for criterion in requirement.criteria:
            if criterion.line == violation.location.line:
                return (criterion.identifier,)
    return ()


def check_coverage(
    index: RequirementsIndex,
    plan: TaskPlan,
    *,
    requirements_file: str,
) -> tuple[ProviderFinding, ...]:
    """Report the requirements and criteria the plan claims no work for."""
    findings: list[ProviderFinding] = []
    for violation in check_requirement_coverage(index, plan, requirements_file=requirements_file):
        mapped = _COVERAGE_KINDS.get(violation.rule)
        if mapped is None:
            continue
        kind, severity = mapped
        refs = _coverage_refs(index, violation)
        target = refs[0] if refs else "the requirement"
        whole = kind == KIND_REQUIREMENT_UNCOVERED
        subject = f"Requirement {target}" if whole else f"Criterion {target}"
        findings.append(
            _finding(
                kind,
                severity,
                f"{subject} is claimed by no task, so nothing in the plan delivers "
                f"it and nothing will report it missing.",
                refs=refs,
                question=_question(
                    f"How does the plan deliver {subject.lower()}?",
                    (
                        f"Add a task referencing {target}.",
                        f"Extend an existing task's references to include {target}.",
                        f"Remove or narrow {subject.lower()}, if it is out of scope for "
                        f"this spec.",
                    ),
                    (
                        "A new task makes the work visible in the plan and in the " "wave order.",
                        "Extending a task is right where the work is genuinely part of "
                        "one already planned; the reference is what makes it checkable.",
                        "Removing it is right where the requirement was aspirational: an "
                        "unclaimed requirement reads as forgotten, not as deferred.",
                    ),
                    f"Add or extend a task so some leaf references {target}; leave the "
                    f"requirement in place only if the plan really carries it.",
                ),
            )
        )
    return tuple(findings)


# --- Check: overlapping or contradictory criteria --------------------------


@dataclass(frozen=True)
class _Obligation:
    """One thing a criterion says a named subject must or must not do."""

    subject: tuple[str, ...]
    negated: bool
    verb: tuple[str, ...]

    @property
    def comparable(self) -> bool:
        """Whether the obligation carries enough to compare against another."""
        return bool(self.subject) and bool(self.verb)


@dataclass(frozen=True)
class _Shape:
    """A criterion reduced to what decides whether two of them collide."""

    identifier: str
    condition: tuple[str, ...]
    obligations: tuple[_Obligation, ...]


def _condition_tokens(text: str) -> tuple[str, ...]:
    """The trigger clause of a criterion, as comparable tokens.

    Empty for an unconditional criterion, which is the right answer rather than a
    missing one: two unconditional obligations on one subject collide exactly as
    two identically-triggered ones do.
    """
    opener = _CONDITION_OPENER_RE.match(text.strip())
    if opener is None:
        return ()
    rest = opener.group("rest")
    end = _CONDITION_END_RE.search(rest)
    clause = rest[: end.start()] if end is not None else rest
    return (opener.group("opener").casefold(),) + _tokens(clause)


def _obligations(text: str) -> tuple[_Obligation, ...]:
    found: list[_Obligation] = []
    for match in _OBLIGATION_RE.finditer(text):
        obligation = _Obligation(
            subject=_tokens(match.group("subject")),
            negated=bool(match.group("negation")),
            verb=_tokens(match.group("tail"))[:_VERB_HEAD_TOKENS],
        )
        if obligation.comparable:
            found.append(obligation)
    return tuple(found)


def _shape(identifier: str, text: str) -> _Shape:
    return _Shape(
        identifier=identifier,
        condition=_condition_tokens(text),
        obligations=_obligations(text),
    )


def check_criteria_collisions(
    index: RequirementsIndex, texts: Mapping[str, str]
) -> tuple[ProviderFinding, ...]:
    """Report criteria inside one requirement that overlap or contradict.

    Comparison is deliberately demanding: two criteria collide only when they
    share a trigger, a subject, and the head of what they oblige that subject to
    do. A looser rule reports every requirement that discusses one component
    twice, which is most of them.

    Scoped to one requirement because a criterion's trigger is written in the
    context of its own requirement. Two requirements using the same words are
    frequently talking about different situations, so comparing across them
    would be guessing.
    """
    findings: list[ProviderFinding] = []
    for requirement in index:
        shapes = [
            _shape(criterion.identifier, texts[criterion.identifier])
            for criterion in requirement.criteria
            if criterion.identifier in texts
        ]
        for position, first in enumerate(shapes):
            for second in shapes[position + 1 :]:
                if first.condition != second.condition:
                    continue
                collision = _collision(first, second)
                if collision is None:
                    continue
                findings.append(collision)
    return tuple(findings)


def _collision(first: _Shape, second: _Shape) -> ProviderFinding | None:
    for left in first.obligations:
        for right in second.obligations:
            if left.subject != right.subject or left.verb != right.verb:
                continue
            subject = " ".join(left.subject)
            verb = " ".join(left.verb)
            refs = (first.identifier, second.identifier)
            trigger = "under the same condition" if first.condition else "unconditionally"
            if left.negated != right.negated:
                return _finding(
                    KIND_CRITERIA_CONTRADICT,
                    FindingSeverity.ERROR,
                    f"Criteria {first.identifier} and {second.identifier} contradict: "
                    f"{trigger} one requires {subject} to {verb} and the other "
                    f"forbids it. No implementation satisfies both, so whichever is "
                    f"built will fail a reading of the other.",
                    refs=refs,
                    question=_question(
                        f"Which of criteria {first.identifier} and {second.identifier} "
                        f"holds when both triggers apply?",
                        (
                            f"Keep {first.identifier} and delete or restate "
                            f"{second.identifier}.",
                            f"Keep {second.identifier} and delete or restate "
                            f"{first.identifier}.",
                            "Narrow one criterion's condition so the two no longer "
                            "describe the same situation.",
                        ),
                        (
                            "Keeping one leaves a single obligation, which is what an "
                            "implementer and a test can both act on.",
                            "The same, with the opposite behaviour: the choice is a "
                            "product decision, not an editing one.",
                            "Narrowing keeps both behaviours where they genuinely apply "
                            "to different cases, at the cost of a more specific trigger.",
                        ),
                        "Decide which behaviour is intended and narrow the other's "
                        "condition; leaving both is a defect no implementation can fix.",
                    ),
                )
            return _finding(
                KIND_CRITERIA_OVERLAP,
                FindingSeverity.WARNING,
                f"Criteria {first.identifier} and {second.identifier} overlap: "
                f"{trigger} both oblige {subject} to {verb}. A duplicated "
                f"obligation is edited in one place and left stale in the other.",
                refs=refs,
                question=_question(
                    f"Are criteria {first.identifier} and {second.identifier} one "
                    f"obligation or two?",
                    (
                        "Merge them into one criterion.",
                        "Distinguish them, so each states an obligation the other " "does not.",
                        "Leave both, where the repetition is deliberate emphasis.",
                    ),
                    (
                        "Merging leaves one place to edit and one criterion for a task "
                        "to reference.",
                        "Distinguishing them is right when two behaviours were meant and "
                        "the wording collapsed them.",
                        "Leaving both costs a stale copy the next time either is " "reworded.",
                    ),
                    "Merge them unless each states something the other does not.",
                ),
            )
    return None


# --- The pass -------------------------------------------------------------

#: Documents each check has to read to run at all.
_CHECK_INPUTS: Mapping[str, tuple[DocumentKind, ...]] = {
    KIND_TERM_UNDEFINED: (DocumentKind.REQUIREMENTS,),
    KIND_QUALIFIER_UNQUANTIFIED: (DocumentKind.REQUIREMENTS,),
    KIND_NOT_TESTABLE: (DocumentKind.REQUIREMENTS,),
    KIND_REQUIREMENT_UNCOVERED: (DocumentKind.REQUIREMENTS, DocumentKind.TASKS),
    KIND_CRITERION_UNCOVERED: (DocumentKind.REQUIREMENTS, DocumentKind.TASKS),
    KIND_CRITERIA_OVERLAP: (DocumentKind.REQUIREMENTS,),
    KIND_CRITERIA_CONTRADICT: (DocumentKind.REQUIREMENTS,),
}

#: Reason a check declares when the glossary it needs is absent.
NO_GLOSSARY_REASON = (
    "requirements.md declares no glossary section, so no term in it is undefined "
    "by omission and the check has nothing to compare against"
)


def _missing_reason(missing: Sequence[DocumentKind]) -> str:
    names = ", ".join(kind.filename for kind in missing)
    return f"the check needs {names}, which was not supplied"


def analyze(corpus: Corpus) -> Outcome:
    """Run every structural check the supplied documents allow.

    The coverage block is built from the same table the checks are driven by, so
    a check that could not run says so instead of contributing silence. That
    distinction is the whole point: silence from a check that ran is evidence,
    and silence from one that did not is not.
    """
    present = frozenset(corpus.present)
    processed: list[str] = [f"{DOCUMENT_PREFIX}{kind.value}" for kind in corpus.present]
    skipped: list[SkippedItem] = []
    findings: list[ProviderFinding] = []

    requirements_text = corpus.requirements
    index: RequirementsIndex | None = None
    texts: dict[str, str] = {}
    glossary_present = False
    if requirements_text is not None:
        index = parse_requirements(requirements_text)
        texts = _criteria_texts(index, requirements_text)
        glossary_present = glossary_terms_defined(requirements_text) is not None
    plan = parse_tasks(corpus.tasks) if corpus.tasks is not None else None

    for kind in ALL_KINDS:
        missing = [needed for needed in _CHECK_INPUTS[kind] if needed not in present]
        if missing:
            skipped.append(
                SkippedItem(
                    item=f"{CHECK_PREFIX}{kind}",
                    reason=Untrusted(_missing_reason(missing)),
                )
            )
            continue
        if kind == KIND_TERM_UNDEFINED and not glossary_present:
            skipped.append(
                SkippedItem(item=f"{CHECK_PREFIX}{kind}", reason=Untrusted(NO_GLOSSARY_REASON))
            )
            continue
        processed.append(f"{CHECK_PREFIX}{kind}")

    if index is not None and glossary_present:
        findings.extend(check_glossary_terms(corpus, index))
    if texts:
        findings.extend(check_qualifiers(texts))
        findings.extend(check_testability(texts))
    if index is not None and plan is not None:
        findings.extend(
            check_coverage(index, plan, requirements_file=DocumentKind.REQUIREMENTS.filename)
        )
    if index is not None:
        findings.extend(check_criteria_collisions(index, texts))

    # Declared on every pass, clean or not: these are what the depth does not
    # examine, and a response silent about them invites reading no findings as
    # correctness.
    skipped.extend(
        SkippedItem(item=f"{BLIND_SPOT_PREFIX}{item}", reason=Untrusted(reason))
        for item, reason in STRUCTURAL_BLIND_SPOTS
    )
    return Outcome(
        findings=tuple(findings),
        coverage=Coverage(processed=tuple(processed), skipped=tuple(skipped)),
    )


def read_corpus(paths: Mapping[str, str | Path]) -> tuple[Corpus, tuple[SkippedItem, ...]]:
    """Read the documents at ``paths``, keyed by artifact kind.

    An unreadable document becomes a skipped entry rather than an exception: the
    analyzer is the fallback every other provider degrades to, so it answers
    with what it could read and declares the rest.
    """
    text: dict[str, str | None] = {}
    unread: list[SkippedItem] = []
    for kind in DocumentKind:
        location = paths.get(kind.value)
        if location is None:
            continue
        try:
            text[kind.value] = Path(location).read_text(encoding="utf-8")
        except OSError as error:
            unread.append(
                SkippedItem(
                    item=f"{DOCUMENT_PREFIX}{kind.value}",
                    reason=Untrusted(f"{kind.filename} could not be read: {error.strerror}"),
                )
            )
    return (
        Corpus(
            requirements=text.get(DocumentKind.REQUIREMENTS.value),
            design=text.get(DocumentKind.DESIGN.value),
            tasks=text.get(DocumentKind.TASKS.value),
        ),
        tuple(unread),
    )


@dataclass(frozen=True)
class LocalAnalyzer:
    """The bundled analysis provider, at structural depth.

    Deterministic by construction. It reads the documents it was pointed at,
    runs text and arithmetic over them, and returns; there is no transport, no
    child process, and no model, so the response's declared cost is zero and
    stays zero however large the spec is.
    """

    version: str = "1"

    @property
    def identity(self) -> ProviderIdentity:
        return builtin_identity(
            PROVIDER_NAME,
            nature=ProviderNature.DETERMINISTIC,
            version=self.version,
        )

    def serve(self, request: CapabilityRequest) -> CapabilityResponse:
        paths = {
            artifact.kind: artifact.path
            for artifact in request.artifacts
            if artifact.kind != "config"
        }
        corpus, unread = read_corpus(paths)
        outcome = analyze(corpus)
        coverage = Coverage(
            processed=outcome.coverage.processed,
            skipped=unread + outcome.coverage.skipped,
        )
        return CapabilityResponse(
            capability=CAPABILITY,
            provider_name=PROVIDER_NAME,
            coverage=coverage,
            findings=outcome.findings,
            cost_credits=0.0,
            result={"depth": DEPTH_STRUCTURAL},
            provider_version=self.version,
        )


def register(registry: object) -> LocalAnalyzer:
    """Bind the analyzer as the builtin serving the analysis capability.

    Takes the registry structurally rather than by import so that the analyzer
    stays a leaf of the module graph: it is the provider every other one falls
    back to, and a fallback that imports the invocation path it is reached
    through is a cycle waiting for someone to add a top-level call.
    """
    analyzer = LocalAnalyzer()
    register_builtin = getattr(registry, "register_builtin")
    register_builtin(CAPABILITY, analyzer)
    return analyzer
