---
name: spec-engine-discovery
description: Router for spec work handled by the Spec Engine app — creating a spec, planning a feature, fixing a bug as a spec, making a quick plan, resuming a spec already under way, or executing one. Names the engine tool to call for each request and carries no workflow or formatting rules of its own, because the tools vend those.
always: false
triggers: create a spec, creating a spec, new spec, write a spec, spec this out, spec-driven, plan a feature, planning a feature, feature spec, fix a bug as a spec, bugfix spec, log this bug as a spec, make a quick plan, quick plan, quick spec, resume a spec, continue a spec, execute a spec, run the spec tasks, review a spec task
---

# Spec Engine — ask the engine first

The Spec Engine app handles this work. This file is only a router: it names the
tool that answers each kind of request. It states no formatting rules, no
ordering rules and no rules about when work may proceed, on purpose. The tools
vend all of that, the engine enforces exactly what its tools describe, and a
paraphrase kept here would quietly disagree with the engine the moment either
side changed.

## The one rule

**Get your instructions from a tool before you act.** Do not work from a
remembered idea of how specs look somewhere else, and do not copy the shape of
whatever files you happen to find on disk. Read the result the tool returns and
follow it. If a tool call fails, say so and stop — guessing is worse than
reporting that the engine is unreachable, because a guess produces artifacts
that look right and validate wrong.

## Which tool answers which request

| The request | Call |
|---|---|
| Any request to start new spec work | `get_authoring_prompt` |
| A user asking to carry out work already specified | `get_orchestrator_prompt` |
| A verdict wanted on one finished piece of that work | `get_review_prompt` |
| "Where did we get to?" on something already begun | `get_phase` |

`get_authoring_prompt` takes the kind of spec the request implies — a feature
being planned, a bug being fixed, or a quick plan wanted in a hurry — and its
own tool listing states the accepted values. Pick from those rather than
inventing a label.

The result of any of these names the further tools to use and when. Follow that,
not this file: everything past routing is the engine's to say.

## What this skill never does

- Never tells you what a document should contain, or in what order documents
  come. Ask the tool.
- Never asserts on the engine's behalf that a piece of work may proceed. The
  engine answers that itself and gives its reasons when the answer is no.
- Never replaces a failed tool call with a plausible substitute of its own.
