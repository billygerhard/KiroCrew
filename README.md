# Upstream issue queue

Issue drafts for [kirodotdev/KiroCrew](https://github.com/kirodotdev/KiroCrew), written
on the Mac mini (which can't file or search upstream because of SSO) and reviewed and
filed from a machine that can.

## Layout

- `pending/` — drafts waiting for review. One markdown file per issue.
- `submitted/` — drafts that were filed upstream. The reviewer moves them here and adds
  an `issue:` line to the front matter with the upstream URL.
- `declined/` — drafts that were not filed: already fixed on main, already reported, or
  not a bug. The reviewer moves them here and adds a `declined:` line saying why.

## Draft format

```markdown
---
title: Short issue title
labels: bug            # comma-separated, existing labels only; may be empty
---

## What
...
```

Everything after the closing `---` is the issue body.

## Review

Nothing on this branch files anything by itself. Drafts are reviewed by hand (or by an
agent on a machine with upstream access) before they go anywhere. For each draft the
reviewer:

1. Checks the claim against current upstream `main`: is the code path still as
   described, or did a later change fix or move it?
2. Searches upstream issues and PRs for an existing report. A related open issue
   gets a comment instead of a duplicate.
3. Decides: file it as written, fold it into another draft, send the fix as a PR
   (when the fork already carries a small, tested fix), or decline it.
4. Moves the file to `submitted/` or `declined/` with the outcome in the front matter,
   then commits and pushes to this branch.

A draft that references a fix on the fork should link the commit so the reviewer can
cherry-pick it onto a fresh branch from upstream `main`.
