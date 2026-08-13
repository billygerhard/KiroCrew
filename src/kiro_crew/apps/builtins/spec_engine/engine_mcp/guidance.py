"""Authored workflow guidance the server returns as tool results.

The text here is the "prompt-as-tool-result": a Host_Agent that holds only this
server and knows nothing about specs reads one of these and then knows the
document formats, the phase flow, and the approval gates well enough to drive
the workflow through the other tools. It is authored for this app, describes the
native format the engine itself enforces, and copies no prompt text from any
other implementation.

Two rules keep this text trustworthy as instructions:

* It is **complete or absent**. A flow the server cannot supply guidance for
  raises :class:`GuidanceUnavailable`, which the server turns into a JSON-RPC
  error. Half a set of authoring instructions is worse than none, because the
  agent cannot tell which half is missing.
* It is **engine-authored only**. Nothing a caller passes is interpolated into
  the returned text. A guidance result is instructions; a caller argument is
  data, and the two never merge, so a crafted argument cannot rewrite the
  instructions the next agent reads.
"""

from __future__ import annotations

#: Shared vocabulary the guidance leans on, stated once so the flows agree.
_PHASE_RULES = """\
The engine enforces the phase order for you; it does not take your word that a
document is done. Work one phase at a time:

1. Author the current phase's document to the native format below and save it
   into the spec directory under `<project>/.kiro/specs/<name>/`.
2. Call `validate_spec` and fix every error it returns. Warnings are advisory.
3. Call `record_approval` for the current gate. The engine records who approved
   and the exact bytes approved; if you edit the document afterwards the
   approval goes stale and must be recorded again before you can advance.
4. Call `advance_phase` to move to the next document. The engine refuses to
   advance while the current document fails validation or lacks a live
   approval, and returns the blocking reasons when it does.

`get_phase` reports where a spec sits without changing anything, so read it
whenever you are unsure what the engine expects next. You never write engine
state into the spec directory yourself: the only files that belong there are the
native documents and the `.config.kiro` sidecar.
"""

_EARS = """\
Every acceptance criterion is one testable EARS sentence. Use the shapes:
`WHEN <trigger>, THE <system> SHALL <response>`; `IF <condition>, THEN THE
<system> SHALL <response>`; `WHILE <state>, THE <system> SHALL <response>`;
`WHERE <feature>, THE <system> SHALL <response>`; or a plain ubiquitous `THE
<system> SHALL <response>`. Number requirements sequentially from 1 and give
each its own `#### Acceptance Criteria` list, numbered from 1.
"""

_TASKS_FORMAT = """\
tasks.md is a checklist plus a dependency graph. Each task is a checkbox item
(`- [ ]` open, `- [x]` complete) numbered like `1`, `1.1`, `2`. Every leaf task
(one that groups no sub-tasks) names at least one acceptance criterion that
exists in requirements.md, and every requirement is covered by at least one
task. Close the document with a fenced ```json``` block under a dependency-graph
heading whose canonical form is a wave list: `{"waves": [{"id": 0, "tasks":
["1.1", "1.2"]}, {"id": 1, "tasks": ["2.1"]}]}`. Wave ids are sequential
integers from zero, every incomplete leaf task appears in exactly one wave, and
an optional `"dependencies"` map must place each edge in an earlier wave than
its dependent with no cycles.
"""

_FEATURE = f"""\
# Authoring a feature spec

A feature spec has three documents, authored and approved in order:
requirements.md, then design.md, then tasks.md.

## Phase flow and gates

{_PHASE_RULES}

## requirements.md

Open with `# Requirements Document`. Include an `## Introduction` section and a
`## Requirements` section. Under Requirements, write each requirement as `###
Requirement N: <title>` with a `**User Story:**` line and a `#### Acceptance
Criteria` list.

{_EARS}

## design.md

Open with `# Design Document`. Include these sections: `## Overview`, `##
Architecture`, `## Components and Interfaces`, `## Data Models`, `## Error
Handling`, and `## Testing Strategy`. Design decisions trace back to the
requirements they satisfy.

## tasks.md

Open with `# Implementation Plan` and include a `## Tasks` section.

{_TASKS_FORMAT}
"""

_BUGFIX = f"""\
# Authoring a bugfix spec

A bugfix spec reuses the three native filenames but changes what each is for:
the bug analysis is authored into requirements.md, the fix design into
design.md, then tasks.md. It is authored and approved in that order.

## Phase flow and gates

{_PHASE_RULES}

## requirements.md (bug analysis)

Open with `# Requirements Document`. Include an `## Introduction` that states the
observed defect, the expected behavior, and how to reproduce it, and a `##
Requirements` section stating what the fix must guarantee.

{_EARS}

## design.md (fix design)

Open with `# Design Document` and include `## Overview`, `## Architecture`, `##
Components and Interfaces`, `## Data Models`, `## Error Handling`, and `##
Testing Strategy`. Explain the root cause and why the change addresses it, not
only the symptom.

## tasks.md

Open with `# Implementation Plan` and include a `## Tasks` section. Include a
task that adds a test which fails before the fix and passes after it.

{_TASKS_FORMAT}
"""

_QUICK = f"""\
# Authoring a quick spec

A quick spec is the lightest plan: requirements.md and tasks.md only, with no
design document. It is authored and approved in that order.

## Phase flow and gates

{_PHASE_RULES}

## requirements.md

Open with `# Requirements Document`, include an `## Introduction` and a `##
Requirements` section, and keep it short: state only what the change must do.

{_EARS}

## tasks.md

Open with `# Implementation Plan` and include a `## Tasks` section.

{_TASKS_FORMAT}
"""

_ORCHESTRATOR = f"""\
# Orchestrating spec execution

Execution starts only through the review gate, and only after tasks.md
validates and every required gate carries a live approval. The engine refuses
the request and returns the blocking reasons when it does not, regardless of how
autonomous the run is configured to be.

## Wave order

Read tasks.md's dependency graph and dispatch leaf tasks wave by wave: never
start a task in a later wave until every task in every earlier wave has reached
a terminal state. Tasks within one wave may run in parallel up to the configured
concurrency cap.

## Per task

For each leaf task, implement exactly its own scope, then obtain a review
verdict before marking it complete. A task is complete only when its review
verdict is an approval; an implementation failure, a changes-required verdict,
or an infrastructure failure sends the task to retry up to the configured limit
and then to failure, without abandoning independent tasks in the remaining
waves. Persist task status after every change so an interrupted run resumes from
where it stopped rather than restarting.

## Format the graph must hold

{_TASKS_FORMAT}
"""

_REVIEW = """\
# Reviewing a task implementation

Return a verdict of `approve` or `request-changes`. Approve only when the
implementation matches the task's scope and its tests would actually catch a
regression. Judge the tests explicitly against these criteria, and treat any
failure as request-changes rather than a comment:

* Assertions derive from the code under test, not from values the test itself
  constructed, so the assertion cannot pass by restating its own input.
* The test fails when the covered behavior is wrong — a test that passes under a
  broken implementation proves nothing.
* Error cases and boundary cases are covered, not only the path that works.

When you request changes, state which criterion failed and what would satisfy
it, so the next revision turn has something concrete to act on.
"""

#: Authoring flows, keyed by the spec type they author for. A flow absent from
#: this map has no guidance and is refused rather than answered with a default.
_AUTHORING: dict[str, str] = {
    "feature": _FEATURE,
    "bugfix": _BUGFIX,
    "quick": _QUICK,
}

#: Every guidance flow the server can supply. Authoring flows are addressed by
#: spec type; the orchestrator and review flows stand alone.
GUIDANCE: dict[str, str] = {
    **_AUTHORING,
    "orchestrator": _ORCHESTRATOR,
    "review": _REVIEW,
}

#: The flow names, sorted, for advertising and tests.
FLOWS: tuple[str, ...] = tuple(sorted(GUIDANCE))

#: The authoring spec types, sorted.
AUTHORING_FLOWS: tuple[str, ...] = tuple(sorted(_AUTHORING))


class GuidanceUnavailable(Exception):
    """Raised when guidance for a requested flow cannot be supplied.

    The server turns this into a JSON-RPC error. It never returns part of a
    guidance set: an agent that receives half the authoring instructions cannot
    tell which half is missing, so absence is reported as an error and the whole
    text is returned or none of it is.
    """

    def __init__(self, flow: str, available: tuple[str, ...]) -> None:
        self.flow = flow
        self.available = available
        super().__init__(
            f"no guidance for flow {flow!r}; available flows: {', '.join(available)}"
        )


def get_guidance(flow: str) -> str:
    """Return the complete authored guidance for *flow*.

    Raises :class:`GuidanceUnavailable` when the flow is unknown or its text is
    empty, so a caller never receives partial instructions.
    """
    text = GUIDANCE.get(flow)
    if not text or not text.strip():
        raise GuidanceUnavailable(flow, FLOWS)
    return text


def get_authoring_guidance(spec_type: str) -> str:
    """Return the complete authoring guidance for one spec type.

    Restricted to the authoring flows (feature, bugfix, quick): the orchestrator
    and review flows are reached through their own tools, so naming one here is
    an unavailable authoring flow rather than a redirect. Raises
    :class:`GuidanceUnavailable` for anything else, never partial text.
    """
    if spec_type not in _AUTHORING:
        raise GuidanceUnavailable(spec_type, AUTHORING_FLOWS)
    return get_guidance(spec_type)
