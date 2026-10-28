---
title: Idle sweep expires a session whose user message never got a reply, without telling the user
labels: bug
---

## What

During a gateway stall (see the companion issue about the watchdog missing
thread-pool starvation), a user message was persisted to a dashboard session, but its
turn never produced any output. Later, the idle sweep treated those sessions as idle
and shut down their agent processes:

```
15:14:11 kiro_crew.session: Expiring idle session: dashboard:chat-246-...   (user message at 14:14, no reply)
15:34:20 kiro_crew.session: Expiring idle session: dashboard:chat-261-...   (user message at 14:30, no reply)
15:34:21 kiro_crew.session: Expiring idle session: dashboard:chat-262-...   (user message at 14:30, no reply)
```

Nothing was written to those transcripts: no error, no "turn failed" and no retry.
The user only saw the messages they had sent, with no response.

## Why

The sweep turns a stuck turn into a lost turn with no trace. The user can't tell
whether the agent is still working, and nothing retries the message.

## Suggested fix

- Before expiring a session, check whether its latest user message has a reply or a
  terminal turn record. If it has neither, don't expire it as idle. Instead, mark the
  turn failed in the transcript (for example "This message didn't get a response
  because the agent stalled. Resend or Continue."), or re-dispatch it once.
- Log those expiries at ERROR level with the age of the unanswered message.
