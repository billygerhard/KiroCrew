"""The validator against real spec artifacts, and its behaviour on arbitrary text.

Two guarantees are checked here that per-rule fixtures cannot reach.

Real documents pass. Hand-authored specs use shapes a minimal fixture never
exercises -- fenced diagrams, tables, blocks reordered during editing, sections
the format does not name -- and a rule that fires on those is a false positive
that would block authoring. The repository's own spec is the corpus.

Arbitrary text does not break it. Validation is on the path of every phase gate,
so it must be a total function: any input returns a report, every violation names
a registered rule, and every location points inside the document.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from kiro_crew.apps.builtins.spec_engine.engine import (
    DocumentKind,
    rules,
    validate_document,
    validate_document_text,
)
from kiro_crew.apps.builtins.spec_engine.engine.documents import REQUIRED_SECTIONS

#: The repository's own spec. It is format-clean, so it doubles as the corpus
#: the validator must not fire on.
_SPEC_DIR = (
    # tests -> spec_engine -> builtins -> apps -> kiro_crew -> src -> repository
    Path(__file__).resolve().parents[6]
    / ".kiro"
    / "specs"
    / "agent-agnostic-spec-engine"
)


def _spec_document(kind: DocumentKind) -> Path:
    path = _SPEC_DIR / kind.filename
    if not path.is_file():
        pytest.skip(f"{path} is not present in this checkout")
    return path


@pytest.mark.parametrize("kind", list(DocumentKind), ids=lambda k: k.value)
def test_the_repositorys_own_spec_validates_clean(kind):
    report = validate_document(_spec_document(kind))
    assert not report.violations, "\n".join(str(v) for v in report)


@pytest.mark.parametrize("kind", list(DocumentKind), ids=lambda k: k.value)
def test_dropping_a_required_section_from_a_real_document_is_caught(kind):
    """Mutating a real document, not a fixture, still reports the defect."""
    path = _spec_document(kind)
    text = path.read_text(encoding="utf-8")
    required = REQUIRED_SECTIONS[kind][0]
    heading = f"\n## {required}\n"
    assert text.count(heading) == 1, f"{path.name} does not declare '{heading.strip()}' once"
    # Demote the heading to prose: the content stays, the section stops existing.
    mutated = text.replace(heading, f"\n{required}\n")
    report = validate_document_text(mutated, kind=kind, file=path.name)
    missing = report.for_rule(rules.SECTION_MISSING)
    assert len(missing) == 1
    assert required in missing[0].message


_TEXT_FRAGMENTS = st.sampled_from(
    [
        "",
        "#",
        "# ",
        "## Requirements",
        "#### Acceptance Criteria",
        "### Requirement 1: A",
        "### Requirement 0:",
        "**User Story:** As a user, I want a thing, so that it helps.",
        "1. WHEN x, THE Y SHALL z.",
        "1. IF x, THE Y SHALL z.",
        "2. nonsense",
        "- [ ] 1. Parent",
        "  - [ ] 1.1 Leaf",
        "  - [x] 1.1.1 Deep",
        "    - _Requirements: 1.1_",
        "    - _Requirements: nope",
        "```",
        "````",
        "~~~",
        "|a|b|",
        "\t- [ ] 1. Tabbed",
        "   ",
    ]
)


@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    fragments=st.lists(_TEXT_FRAGMENTS, max_size=25),
    kind=st.sampled_from(list(DocumentKind)),
)
def test_validation_is_total_and_self_describing(fragments, kind):
    text = "\n".join(fragments)
    report = validate_document_text(text, kind=kind, file=kind.filename)
    line_count = len(text.splitlines())
    for violation in report:
        assert violation.rule in rules.ALL_RULES, violation.rule
        assert violation.file == kind.filename
        # A violation about the document as a whole is pinned to line 1; every
        # other one must address a line that exists.
        assert 1 <= violation.location.line <= max(line_count, 1)
        assert violation.message.strip()
    assert list(report) == sorted(report, key=lambda v: v.sort_key)


@settings(max_examples=100, deadline=None)
@given(text=st.text(max_size=400))
def test_arbitrary_text_never_raises(text):
    for kind in DocumentKind:
        validate_document_text(text, kind=kind, file=kind.filename)


@settings(max_examples=50, deadline=None)
@given(padding=st.integers(min_value=0, max_value=20))
def test_trailing_blank_lines_do_not_change_the_verdict(padding):
    """Whitespace at the end of a document is not a defect."""
    text = _spec_document(DocumentKind.DESIGN).read_text(encoding="utf-8")
    report = validate_document_text(
        text + "\n" * padding, kind=DocumentKind.DESIGN, file="design.md"
    )
    assert not report.violations
