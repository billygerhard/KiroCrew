# Upstream issue queue

Issue drafts for [kirodotdev/KiroCrew](https://github.com/kirodotdev/KiroCrew), written
on the Mac mini (which can't file upstream because of SSO) and submitted from a machine
that can.

## Layout

- `pending/` — drafts waiting to be filed. One markdown file per issue.
- `submitted/` — drafts that were filed. The submitter moves them here and adds the
  issue URL to the front matter.

## Draft format

```markdown
---
title: Short issue title
labels: bug            # comma-separated, existing labels only; may be empty
---

## What
...
```

Everything after the closing `---` is the issue body, passed verbatim with
`--body-file`.

## Submitting

`submit.sh` files every draft in `pending/`, moves it to `submitted/` with an
`issue:` line, then commits and pushes. Run it from a clone of
`billygerhard/KiroCrew` checked out on this branch, with `gh` signed in to an account
that can open issues on kirodotdev/KiroCrew:

```bash
git clone -b upstream-issues --single-branch https://github.com/billygerhard/KiroCrew.git kc-upstream-issues
cd kc-upstream-issues && ./submit.sh
```

Cron example (every 30 minutes):

```cron
*/30 * * * * cd ~/kc-upstream-issues && ./submit.sh >> ~/kc-upstream-issues.log 2>&1
```

If a label in `labels:` doesn't exist upstream, `gh` refuses the whole create. The
script then retries with no labels, so the issue still gets filed.
