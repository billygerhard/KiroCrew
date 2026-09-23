# Bugfix Requirements Document

## Introduction

A dashboard chat the user has trusted (per-slot Trust, which writes
`approval_policy="auto"` on that chat's session key) auto-approves the spawn and
every tool call of the subagents it spawns directly. When one of those subagents
spawns its own subagent, the grandchild's spawn gate and tool-permission prompts
are NOT auto-approved: the grandchild's parent key is `subagent:<id>`, which has
no `approval_policy` in the session store, so the gateway falls through to the
interactive approver. That approver cannot map `subagent:<id>` to a dashboard
tab, so the prompt is posted only to the global approvals feed, nobody sees it,
and the grandchild stalls until the approval timeout. The user-visible symptom:
a trusted chat's nested subagents hang "waiting for permission" while the chat
itself and its direct children run unattended.

The defect is reproducible with unit tests against `SubagentManager` (spawn
gate) and the subagent run loop (tool-permission path); no live gateway is
needed.

## Bug Analysis

### Current Behavior (Defect)

1.1 WHEN a subagent whose parent chat has `approval_policy="auto"` calls
`spawn_run` THEN the spawn gate reads `get_approval_policy("subagent:<id>")`,
gets `""`, treats the parent as untrusted, and routes the spawn to the
interactive approval callback.

1.2 WHEN a depth-2 (or deeper) subagent under a trusted chat emits a
`session/request_permission` THEN the run loop resolves `parent_policy` as `""`
for the `subagent:<id>` parent key and falls through to the interactive tool
approver instead of the `parent_policy_auto` branch.

1.3 WHEN the interactive approver receives a prompt whose parent key is
`subagent:<id>` THEN it resolves no dashboard slot for that key, so the slot
trust check is skipped and the prompt is surfaced only on the global approvals
surface.

### Expected Behavior (Correct)

2.1 WHEN a subagent at any depth calls `spawn_run` and the ROOT chat session
reached by following `parent_session_key` links has `approval_policy="auto"`
THEN the system SHALL auto-approve the spawn with reason `parent_trusted`.

2.2 WHEN a subagent at any depth emits a tool permission request and the root
chat session has `approval_policy="auto"` THEN the system SHALL resolve
`parent_policy="auto"` and approve through the existing `parent_policy_auto`
branch (subject to the unchanged low-fidelity child grant-eligibility rule).

2.3 WHEN the interactive approver receives a prompt whose parent key is
`subagent:<id>` THEN the system SHALL resolve the approval slot from the root
chat session so the prompt lands in that chat's tab and its slot trust applies.

2.4 WHEN the `parent_session_key` chain contains a cycle or references an
unknown subagent id THEN the system SHALL terminate the walk and treat the
policy as the last resolvable key's policy (never loop, never raise).

2.5 WHEN a subagent at depth two or deeper emits a file card THEN the system
SHALL route the card to the root chat's tab (the same tab its approval prompts
land in) instead of suppressing it because `subagent:<id>` names no tab.

2.6 WHEN a conversation started under one chat is continued from a chat with a
different root THEN the system SHALL NOT resolve requests keyed by that
conversation to either chat's trust; the conversation SHALL resolve to a
contested marker (untrusted, shown by no tab) that is stamped into every run
admitted beneath it, so the refusal outlives the records that established it.
The continuation's OWN run is included: the turn it executes was authored
under the founding chat's key, so the requests that run issues SHALL be
governed by the contested marker, not by the trust of the chat that continued
it (whose card the run's tab still shows). A continuation from the same root
inherits that root for its own requests as before.

2.7 WHEN a run re-enters admission with a root captured earlier THEN the
system SHALL govern it by that captured root and SHALL NOT re-walk its parent
link, whose conversation may since have been continued from another chat. The
re-entry paths are exhaustive: every re-entry that resumes an admission (the
store-accepted re-entry of `spawn_async`, the queue drain, the stagger drain)
reuses the parameters captured on the first gate pass, which carry the root;
the dashboard retry of a failed run is the one re-admission built from a
finished record and SHALL pass that record's stamp explicitly -- and, when the
failed run's conversation root is a contested marker, that marker too, so a
retried contested continuation is governed by the marker rather than founding
a fresh conversation at the chat whose tab held its card; and the runtime's
automatic follow-up (`spawn_steer` `mode="follow_up"`), which is a run's OWN
next turn dispatched after it finished, SHALL pass that run's stamp explicitly
(its chat root, or for a parentless run the conversation root it founded in its
own name), so a follow-up whose parent has since been evicted -- or that never
had one -- is governed by the root the run was admitted with and SHALL NOT be
marked contested by a re-walk of a parent key that has no record.

2.8 WHEN a durable queued row written before roots were stamped names a
`subagent:` caller and re-enters admission after a restart THEN the system
SHALL fail closed through the gate's resumed-admission rule (2.9): the run
faces an interactive prompt and SHALL NOT inherit the trust of a chat that has
since continued the caller's conversation.

2.9 WHEN any resumed admission (a queue drain or a store-accepted re-entry)
names a `subagent:` caller and carries no captured root THEN the gate SHALL
resolve it to a contested root rather than re-walk the caller, so a re-entry
path that omits the parameter fails closed to an interactive prompt.

2.10 WHEN a run whose root is contested raises a tool prompt THEN the prompt's
purpose SHALL open with the same state the conversation's spawn prompt
names and a one-clause why (the user's own act, not the system's model: the
task was continued from a chat that did not start it), ahead of the tool's own
purpose, which SHALL be attributed to the run and set apart from the system's
sentences so an untrusted run's claim never reads with their authority, and
SHALL close with the same remedy the spawn prompt names, so a user who meets
only tool prompts still finds the way out where they decide; the feed card SHALL show
that purpose before the command, so the entry says why it has no chat on its
face -- not only once opened -- while still saying what the tool is for.

2.11 WHEN a continuation arrives for a conversation of which no in-memory
record remains (a gateway restart, or the founder evicted) THEN the system
SHALL read the conversation's founding root from the founder's durable run
record, where admission persisted it -- read OFF the event loop by the async
admission entry and handed to the gate as a value, the gate itself reading
nothing -- and SHALL refuse every chat's trust when that root cannot be read
(no record, or a nested record written before the stamp existed; a depth-one
pre-stamp record's chat parent IS the root admission would have stamped and
SHALL be used) or names a different root. One marker (`contested:<key>`) and
one state sentence cover every such case -- two chats with a claim, no
readable founder, a founder that was no chat (a parentless run founds in its
own name) -- so the sentence SHALL be true for all of them and SHALL NOT
assert a second chat ("This run has no single owning chat"); a contest established
while records were live SHALL be written onto the founder's durable record
(and onto the retained founder record in memory) before the contested
continuation runs, so it survives the restart too, and a persisted contest
SHALL outrank any founding root still retained in memory, since the record
that contested the conversation may be evicted while the founder's is not; a
contested continuation whose contest could not be written SHALL NOT run; the
write SHALL reach the founder's durable file even when the founder's record is
held live-only at the time (a continuation in incognito or temporary mode
tightens the founder into memory), since a merge absorbed in memory alone
would report success while a restart read the untouched file's founding root
back. The
spawn gate, the spawn prompt and the gateway's approval-slot resolver SHALL
read the same trust root the run loop reads (the contested marker beats the
routing root), so a run admitted into a contested conversation is interactive
at admission as well as mid-run and no tab's Trust setting answers its prompt.

### Unchanged Behavior (Regression Prevention)

3.1 WHEN a depth-1 subagent's parent chat has `approval_policy="auto"` THEN the
system SHALL CONTINUE TO auto-approve its spawn and tool calls exactly as today.

3.2 WHEN the root chat is NOT trusted (`approval_policy=""`) THEN the system
SHALL CONTINUE TO require interactive approval for spawns and tool calls at
every depth; no policy is invented for an untrusted root.

3.3 WHEN a spawn carries an explicit `approval_mode="auto"`, or dashboard YOLO
is active, or `agent.approval_mode="auto"` applies, or
`hooks.auto_approve_subagent_tools`/`auto_approve_subagent_spawn` is set THEN
the system SHALL CONTINUE TO honor those grants in their existing precedence
order.

3.4 WHEN a parent key is a chat, cron, hook or channel key (not
`subagent:<id>`) THEN the system SHALL CONTINUE TO read its policy directly from
the session store with no walk.

3.5 WHEN the `agent.approval_mode="auto"` config fallback evaluates whether the
parent session is alive or garbage-collected THEN the system SHALL CONTINUE TO
evaluate `has_session` against the literal `parent_session_key`, unchanged.
