"""Property-based tests for wave ordering: readiness never precedes a dependency.

**The claim.** For any tasks plan, a task is never dispatched before every
dependency it names has completed.

That claim spans two components, and neither one carries it alone. The validator
in :mod:`.cross_document` requires every declared edge to point at an earlier
wave. The scheduler in :mod:`.orchestrator` then dispatches wave by wave and
deliberately does *not* re-read the edges, on the grounds that the wave order
already carries them. So the guarantee is a composition: the validator is what
makes wave order sufficient, and the scheduler is what turns wave order into
dispatch order. Checking either half alone leaves the other free to disagree,
which is how a plan validated under one meaning of "earlier" gets executed under
another.

Both directions are checked, because only the pair is a property:

* an accepted plan's wave order is a topological order of its declared edges, and
  replaying the wave loop's readiness rule over it never starts a task whose
  dependency is unfinished;
* a plan carrying an edge that points at its own wave or a later one is refused.

Without the second, a validator that dropped the ordering check entirely would
satisfy the first for every plan it accepted -- there would simply be fewer
edges' worth of meaning behind the acceptance. That is the failure mode this file
exists for: a readiness answer that is right for the plans someone hand-wrote.

The generator's shapes are the point. Wave assignment is drawn independently of
task numbering, so a task in section 1 routinely lands in a later wave than a
task in section 3 and "earlier" cannot be satisfied by reading the numbers.
Chains, diamonds, and fan-in are drawn explicitly rather than left to chance,
because a generator that only ever produces independent parallel sets makes the
ordering question vacuous.
"""

from __future__ import annotations

import json
from typing import Mapping, Sequence

from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from kiro_crew.apps.builtins.spec_engine.engine import rules
from kiro_crew.apps.builtins.spec_engine.engine.cross_document import check_dependency_graph
from kiro_crew.apps.builtins.spec_engine.engine.findings import Severity, Violation
from kiro_crew.apps.builtins.spec_engine.engine.orchestrator import schedule_of
from kiro_crew.apps.builtins.spec_engine.engine.structure import parse_tasks

#: Parsing and scheduling are pure and in-memory, so examples are cheap.
MAX_EXAMPLES = 200

TASKS_FILE = "tasks.md"

#: Rules that mean "this graph does not order its own edges". A refusal carrying
#: either of these is the validator doing the job the scheduler relies on.
_ORDERING_RULES = frozenset({rules.GRAPH_DEPENDENCY_ORDER, rules.GRAPH_CYCLE})


def _errors(found: Sequence[Violation]) -> tuple[Violation, ...]:
    return tuple(item for item in found if item.severity is Severity.ERROR)


def tasks_document(
    sections: Sequence[int],
    *,
    waves: Sequence[Sequence[str]],
    dependencies: Mapping[str, Sequence[str]] | None = None,
) -> str:
    """A tasks document of several numbered sections plus a dependency graph.

    Several sections rather than one, so a dependency can point across sections
    and the wave order is visibly not the document order.
    """
    parts = ["# Implementation Plan", "", "## Tasks", ""]
    for section, leaves in enumerate(sections, start=1):
        parts.append(f"- [ ] {section}. Section {section}")
        for leaf in range(1, leaves + 1):
            parts.append(f"  - [ ] {section}.{leaf} Leaf {section}.{leaf}")
            parts.append("    - _Requirements: 1.1_")
    graph: dict[str, object] = {
        "waves": [{"id": index, "tasks": list(tasks)} for index, tasks in enumerate(waves)]
    }
    if dependencies is not None:
        graph["dependencies"] = {task: list(needs) for task, needs in dependencies.items()}
    parts += ["", "## Task Dependency Graph", "", "```json", json.dumps(graph, indent=1), "```"]
    return "\n".join(parts) + "\n"


def _leaf_numbers(sections: Sequence[int]) -> list[str]:
    return [
        f"{section}.{leaf}"
        for section, leaves in enumerate(sections, start=1)
        for leaf in range(1, leaves + 1)
    ]


#: Between one and three sections holding one to three leaves each: enough tasks
#: for a diamond, and more than one section so a cross-section edge is reachable.
_SECTIONS = st.lists(st.integers(min_value=1, max_value=3), min_size=1, max_size=3)


@st.composite
def _plans(draw: st.DrawFn, *, sound: bool) -> tuple[str, dict[str, tuple[str, ...]], list[str]]:
    """A tasks document, the edges it declares, and its leaves in wave order.

    ``sound`` picks the half of the space under test: a plan whose every edge
    points backwards, or one carrying at least one edge that does not.
    """
    sections = draw(_SECTIONS)
    numbers = _leaf_numbers(sections)
    # Wave assignment is drawn over a shuffled leaf list, so wave order is
    # independent of task numbering and a section-3 task can precede a
    # section-1 one. A generator that partitioned the numbers in order would let
    # "depends on an earlier wave" pass for the wrong reason.
    order = draw(st.permutations(numbers))
    wave_count = draw(st.integers(min_value=1, max_value=max(1, len(numbers))))
    waves: list[list[str]] = [[] for _ in range(wave_count)]
    for position, number in enumerate(order):
        waves[position % wave_count].append(number)
    waves = [wave for wave in waves if wave]
    wave_of = {number: index for index, wave in enumerate(waves) for number in wave}

    # Backward edges only, drawn per task over everything in a strictly earlier
    # wave. Fan-in and chains both fall out of this: a task may name several
    # predecessors, and a predecessor may itself name one.
    dependencies: dict[str, tuple[str, ...]] = {}
    for number in order:
        earlier = [other for other in numbers if wave_of[other] < wave_of[number]]
        if not earlier:
            continue
        picked = draw(st.lists(st.sampled_from(earlier), max_size=3, unique=True))
        if picked:
            dependencies[number] = tuple(picked)

    if sound:
        return (
            tasks_document(sections, waves=waves, dependencies=dependencies),
            dependencies,
            [number for wave in waves for number in wave],
        )

    # One edge that the wave order cannot carry: same wave, a later wave, or the
    # task itself. Each is a distinct way for the schedule to be asked to honour
    # something it does not read.
    dependent = draw(st.sampled_from(numbers))
    forward = [other for other in numbers if wave_of[other] >= wave_of[dependent]]
    offender = draw(st.sampled_from(forward))
    broken = dict(dependencies)
    broken[dependent] = tuple({*broken.get(dependent, ()), offender})
    return (
        tasks_document(sections, waves=waves, dependencies=broken),
        broken,
        [number for wave in waves for number in wave],
    )


@settings(max_examples=MAX_EXAMPLES, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(_plans(sound=True))
def test_an_accepted_plan_never_makes_a_task_ready_before_its_dependencies(
    plan: tuple[str, dict[str, tuple[str, ...]], list[str]],
) -> None:
    text, dependencies, _ = plan
    parsed = parse_tasks(text)
    found = _errors(check_dependency_graph(parsed, tasks_file=TASKS_FILE))
    # The generator builds only well-ordered graphs, so a refusal here is the
    # validator refusing a plan it should schedule.
    assert found == (), [str(item) for item in found]

    schedule = schedule_of(parsed)
    assert schedule.usable, schedule.reason
    position = {task: index for index, wave in enumerate(schedule.waves) for task in wave.tasks}

    # Every declared edge crosses a wave boundary in the right direction, so
    # dispatching by wave satisfies it without the loop reading the edges.
    for task, needs in dependencies.items():
        for need in needs:
            assert position[need] < position[task], f"{task} runs no later than {need}"

    # Replay the loop's readiness rule: a wave starts once every earlier wave has
    # finished. Nothing a wave dispatches may still be waiting on unfinished work.
    complete: set[str] = set()
    for wave in schedule.waves:
        for task in wave.tasks:
            unmet = set(dependencies.get(task, ())) - complete
            assert not unmet, f"{task} dispatched while {sorted(unmet)} unfinished"
        complete.update(wave.tasks)
    assert complete == set(position)


@settings(max_examples=MAX_EXAMPLES, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(_plans(sound=False))
def test_a_plan_whose_edges_outrun_its_waves_is_refused(
    plan: tuple[str, dict[str, tuple[str, ...]], list[str]],
) -> None:
    text, dependencies, _ = plan
    parsed = parse_tasks(text)
    schedule = schedule_of(parsed)
    # The wave list itself is well formed; only the edges are wrong. So the
    # scheduler accepts it, which is exactly why the validator has to refuse it.
    assume(schedule.usable)

    found = _errors(check_dependency_graph(parsed, tasks_file=TASKS_FILE))

    assert found, "an edge pointing at its own or a later wave was accepted"
    assert {item.rule for item in found} <= _ORDERING_RULES, [str(item) for item in found]


@settings(max_examples=MAX_EXAMPLES, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(_plans(sound=True))
def test_the_schedule_holds_every_unfinished_leaf_exactly_once(
    plan: tuple[str, dict[str, tuple[str, ...]], list[str]],
) -> None:
    """No task is silently dropped from, or duplicated in, the dispatch order."""
    text, _dependencies, expected = plan
    schedule = schedule_of(parse_tasks(text))

    assert schedule.usable, schedule.reason
    scheduled = list(schedule.scheduled_tasks)
    assert sorted(scheduled) == sorted(expected)
    assert len(scheduled) == len(set(scheduled))
    # Wave identifiers count from zero without gaps, so position in the tuple and
    # declared identifier cannot disagree about which wave runs first.
    assert [wave.identifier for wave in schedule.waves] == list(range(len(schedule.waves)))
