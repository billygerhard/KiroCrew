---
title: Lessons and steering are lost after compaction or resume in long dashboard chats
labels: bug
---

## What happens

In long dashboard chats running on the Claude seat harness, standing lessons (corrections) and steering stop reaching the model after a compaction or a session resume. The model then breaks rules the user set long ago. Seen in a Shadow Flips chat on 2026-11-18: the saved lesson "arm monitor_start whenever a reply promises later action" was broken twice after several compactions.

## Evidence (src/kiro_crew/context.py at billy/current-setup)

1. **Slim resume (around line 4980):** only `_resume_rules` (critical rules), `session_ctx` (date/identity) and the member section are re-injected. The banner tells the model that memory, lessons and skills "are preserved in the restored conversation history". The code comment at line 4981 admits they "may be compacted out of the restored transcript", but only the critical rules are re-anchored.
2. **Post-compaction re-injection (`if not is_new_session and needs_reinjection`, around line 5116):** this re-injects the agent prompt, the memory activity index, the skills index, response preferences and folder steering. **Lessons are not re-injected at all.**
3. **No re-injection at all after `/compact`:** in the chat above, a `/compact` run as a local Claude Code command showed none of the `[REINJECTED AFTER COMPACTION …]` blocks on the next turn. Either `mark_needs_reinjection` isn't triggered for local `/compact` or auto-compaction on the claude-seat (claude-agent-acp) harness, or the compaction event isn't surfaced. Not yet confirmed which.

## Expected

After any compaction or resume, the model sees again:
- the critical rules;
- the standing `always` lessons;
- the project and folder steering.

## Suggested fix

- Add the `always` lessons block to the post-compaction re-injection, with the same budget as session start.
- Make slim resume re-inject the `always` lessons and the steering, instead of claiming they're preserved.
- Detect compaction on the claude-seat harness: the local `/compact` command, the PostCompact hook, or the ACP compaction status. Then call `mark_needs_reinjection`.
- Add a test: compact a session, and check that the next prompt contains the lessons and steering.
