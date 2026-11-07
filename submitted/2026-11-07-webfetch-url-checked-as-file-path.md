---
issue: https://github.com/kirodotdev/KiroCrew/issues/32640
title: WebFetch of a public URL is refused because the sensitive-path resolver treats the URL as a file path and stalls
labels: bug
---

## What

A `WebFetch` of a public documentation page was refused twice in a row on 2026-11-07 (about 21:15 UTC, macOS, Claude Code backend):

```
Blocked:
  - Fetch https://docs.cloud.google.com/recaptcha/docs/interpret-assessment-website: Blocked: the path could not be
    verified against the sensitive-path list within the resolver budget (symlink resolution did not complete in time:
    the check timed out under '/Users/billy'), so it is refused fail-closed. This is NOT a match: the path is not known
    to be sensitive. Path checks that touch that prefix are refused for about 29 s more; retry the same call after that
    wait; do not re-spell it. Path: 'Fetch https://docs.cloud.google.com/recaptcha/docs/interpret-assessment-website'
```

The model saw it as `User refused permission to run tool`. A third try a few minutes later went through.

## Why it is a bug

The sensitive-path check ran on the string `Fetch https://docs.cloud.google.com/...`. That is a URL, not a filesystem path, so it can never match the sensitive-path list. It still went through symlink resolution under the home directory. When that resolution stalled, the fetch was refused fail-closed.

Two things made it worse here:
- The host was under memory pressure (macOS WARN, load about 5 per core).
- Part of the home directory is a symlink onto an external volume (`~/workspace/genius-apps` -> `/Volumes/Dev/...`), so resolution under `/Users/billy` is slow when the system is busy.

## Expected

A `WebFetch` (or any tool argument that is an `http(s)://` URL) is not run through the filesystem sensitive-path check at all, so a resolver stall cannot refuse it.

## Related

#28363: a resolver stall reported as "sensitive path" when creating a chat in a folder. This is the same stall, reached through a tool argument that is not a path.

## Also

The model was told `User refused permission to run tool`, which reads as the user declining. The agent then told the user it was refused, which looked like a user action. A fail-closed policy refusal should reach the model as a policy refusal, not as a user refusal.
