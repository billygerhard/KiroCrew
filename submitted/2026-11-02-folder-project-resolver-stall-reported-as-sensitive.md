---
issue: https://github.com/kirodotdev/KiroCrew/issues/28363 (filed from this draft)
pr: https://github.com/kirodotdev/KiroCrew/pull/28393
title: Creating a chat in a folder fails with "project_dir refers to a sensitive path" when the path resolver stalls
labels: bug
---

## What

Pressing **+** to create a chat in a project folder failed with:

```
POST /api/chat/slots -> 400
{"error": "invalid folder project: project_dir refers to a sensitive path", "code": "folder_project_invalid"}
```

The folder's project directory is an ordinary repo under `~/workplace/...` and is not
sensitive. Pressing **+** again a minute later worked.

The gateway log shows that the sensitive-path resolver had stalled one second earlier:

```
11:54:19 WARNING kiro_crew.security.paths: sensitive-path symlink resolution did not complete in 2.0s (stalled mount?); refusing paths under the stalled prefix for the next 30s (stall #1)
```

The same window also refused the shipped `prompt.md` and a cron script. Their messages
correctly say "the path could not be verified ... within the resolver budget ... This is
NOT a match". The host was under heavy disk I/O at the time (many sub agents running).

## Why

`_validate_project_dir` in `src/kiro_crew/dashboard/chat_folders_paths.py` (upstream main
at b07aa75ac5) calls the boolean `is_sensitive_path(resolved)`. As its docstring says, a
resolver stall is a refusal and therefore `True`, so the timeout is reported as a match:

- the user gets "project_dir refers to a sensitive path" instead of the "could not be
  verified in time, retry in ~30 s" wording from `sensitive_path_refusal`;
- the audit line records `error="sensitive path"` for a path that was never matched.

The refusal is also avoidable. `_validate_project_dir` has already run
`os.path.realpath` itself, and on the slot-create path it runs off the event loop
(`asyncio.to_thread(_resolve_folder_project_dir, ...)`). This is the case
`is_sensitive_canonical_path` exists for: off the loop it answers inline without
submitting to the `mc-pathres` pool, so a saturated pool can't refuse a healthy path.

The `steering_dirs` check in the same file (`if is_sensitive_path(canonical):` ->
"steering_dirs refers to a sensitive path") has the same problem.

## Expected

- A path the caller has already canonicalised off the loop is checked with
  `is_sensitive_canonical_path`, so a resolver stall doesn't block chat creation.
- Where a bounded check can still time out (on-loop callers), the error uses the
  `sensitive_path_refusal` wording ("could not be verified ... retry") and the audit
  line distinguishes a timeout from a match.

## Repro

1. Have a chat folder whose project directory is under `$HOME`.
2. Cause a sensitive-path resolver stall (e.g. heavy disk I/O so a `realpath` under
   `$HOME` exceeds the 2 s budget).
3. Within the next 30 s, create a chat in that folder: 400 `folder_project_invalid`,
   "project_dir refers to a sensitive path".
