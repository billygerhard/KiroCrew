"""Test-quality criteria a review verdict is judged against.

A green suite that cannot fail reports the opposite of the truth, so a review
verdict does not stop at "the implementation is correct": it also judges whether
the tests would catch a regression. These criteria are the first-class thing the
verdict carries. A verdict that finds the implementation sound but its tests
inadequate is a changes-required verdict, not an approval — the assessment here
is what folds into :class:`~.orchestrator.ReviewVerdict` so that
``approved`` cannot read true while any test-quality criterion is unmet.

This module owns the criteria and the finding vocabulary. It does not seed a
review turn or decide a verdict: the builtin review provider that seeds a turn
against these criteria and produces the findings is a separate capability. Until
that provider is bound, the mechanism here has no producer of findings beyond
tests — which is the point of keeping the criteria, the finding shape, and the
verdict's fail-closed fold in one reviewable place before the provider arrives.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ReviewCriterion:
    """One named test-quality criterion a finding can be routed to.

    ``key`` is the stable identifier a finding references, so a finding names the
    criterion it concerns rather than restating it; ``statement`` is the criterion
    as a reviewer reads it when seeding a review turn.
    """

    key: str
    statement: str


#: Assertions must be pinned to the behavior under test, the test must actually
#: fail when that behavior is wrong, and the unhappy paths must be exercised.
#: These are the criteria a verdict judges tests against; a finding names one of
#: their keys so the audit record says which one the tests failed.
DERIVED_ASSERTIONS = ReviewCriterion(
    key="derived-assertions",
    statement=(
        "Assertions derive from the code under test, not from values the test "
        "itself constructed: the expected value is computed by the behavior being "
        "checked or a known-independent oracle, never echoed back from the input "
        "the test just handed in."
    ),
)
FAILS_ON_WRONG_BEHAVIOR = ReviewCriterion(
    key="fails-on-wrong-behavior",
    statement=(
        "The test fails when the covered behavior is wrong. An assertion that "
        "holds whether or not the behavior is correct covers nothing."
    ),
)
ERROR_AND_BOUNDARY_CASES = ReviewCriterion(
    key="error-and-boundary-cases",
    statement=(
        "Error and boundary cases are covered, not only the path that works: the "
        "empty input, the rejected input, the edge of a range, the failure the "
        "code is supposed to raise."
    ),
)

#: The criteria in the order a reviewer applies them. A finding's criterion key
#: is expected to be one of these; ``is_known_criterion`` is how a caller checks.
TEST_QUALITY_CRITERIA: tuple[ReviewCriterion, ...] = (
    DERIVED_ASSERTIONS,
    FAILS_ON_WRONG_BEHAVIOR,
    ERROR_AND_BOUNDARY_CASES,
)

#: Assertion shapes that pass regardless of the mechanism they claim to cover.
#: Each is a way a test can be green while proving nothing, drawn from real gaps
#: found in this codebase. A reviewer screens the tests against every one of them
#: before deciding the assertions actually bind to the behavior.
ASSERTION_SHAPE_SCREEN: tuple[str, ...] = (
    "a proxy the failure path also sets, so the assertion passes on the bug too",
    "a short-circuit reached before the property under test is ever evaluated",
    "only the direction a constant already satisfies, never the direction a bug moves",
    "a branch no test executes, so the guarantee inside it is never reached",
    "a fake too broken to reach the branch under test, so the branch is never exercised",
    "an assertion made vacuous by operator precedence, binding to the wrong subexpression",
    "an assertion made vacuous by a representation that escapes its own input",
    "one sanitized field asserted beside an unsanitized sibling left unchecked",
    "a condition phrased in terms of the outputs a bug moves together, so it satisfies itself",
)

#: The question a reviewer asks of every guarantee, because the defects this
#: project shipped lived in the gap between modules rather than in missing
#: coverage: a guarantee enforced at one spelling or one path is the shape that
#: has produced the security defects here. The fence is not the property; the
#: property is that nothing gets past it.
EQUIVALENT_PATH_QUESTION: str = (
    "For every guarantee the tests claim, ask what ELSE reaches the same effect: "
    "a second spelling the engine itself emits, a second config path holding the "
    "same executable content, a second comparison of the same identity, a second "
    "delivery path that skips the one under test. A guarantee proven at one "
    "spelling or one path is not proven — the property is that nothing gets past "
    "the fence, not that the fence exists at the one place the test looked."
)


def is_known_criterion(key: str) -> bool:
    """Whether *key* names one of the defined test-quality criteria."""
    return any(criterion.key == key for criterion in TEST_QUALITY_CRITERIA)


@dataclass(frozen=True)
class TestQualityFinding:
    """One way a task's tests failed a test-quality criterion.

    ``criterion`` names the criterion the tests failed — ordinarily one of
    :data:`TEST_QUALITY_CRITERIA` — so the audit record says which guarantee the
    tests did not meet. ``detail`` is the reviewer's untrusted explanation.
    """

    criterion: str
    detail: str

    # This is a domain type, not a pytest case; its name begins with "Test".
    __test__ = False

    def to_json_object(self) -> dict[str, str]:
        return {"criterion": self.criterion, "detail": self.detail}


@dataclass(frozen=True)
class TestQualityAssessment:
    """A verdict's judgement of the task's tests against the criteria.

    An assessment with no findings is satisfied — the tests met the criteria, or
    the task carried no tests to judge. Any finding makes it unsatisfied, and an
    unsatisfied assessment is what forces a verdict to changes-required: there is
    no count threshold and no severity dial, because a single test that cannot
    fail is a suite that lies about the behavior it claims to cover.
    """

    findings: tuple[TestQualityFinding, ...] = field(default_factory=tuple)

    # This is a domain type, not a pytest case; its name begins with "Test".
    __test__ = False

    @property
    def satisfied(self) -> bool:
        """True when no finding was recorded against the criteria."""
        return not self.findings

    def detail(self) -> dict[str, object]:
        """Serialise the findings for the audit log."""
        return {"findings": [finding.to_json_object() for finding in self.findings]}
