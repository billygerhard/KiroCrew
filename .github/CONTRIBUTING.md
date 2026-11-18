# Contributing to this fork

This is a fork of [kirodotdev/KiroCrew](https://github.com/kirodotdev/KiroCrew).
For the upstream project's own rules, see [CONTRIBUTING.md](../CONTRIBUTING.md).
Anything that would be useful to every Kiro Crew user should go upstream first.

## Branches

| Branch | What it is |
|---|---|
| `billy/live` | The maintained branch (default). Upstream plus a stack of fork patches. |
| `main` | A plain mirror of upstream `main`. Never commit to it. |
| `billy/next-setup-N` | A migration in progress: the stack being replayed onto newer upstream. |
| `live-YYYY-MM-DD` tags | Where `billy/live` pointed before each migration. |

## `billy/live` is rebased

`billy/live` is not a normal branch. It is upstream with our patches on top,
and each time we move to a newer upstream the whole stack is rebased and
`billy/live` is force-pushed. Commit SHAs change on every migration.

Before a migration, the old head is tagged `live-YYYY-MM-DD`. To move your
branch onto the new `billy/live`:

```bash
git fetch origin --tags
git rebase --onto origin/billy/live live-YYYY-MM-DD your-branch
```

Use the tag of the `billy/live` your branch was cut from.

## Sending a change

1. Open your PR against `billy/live`.
2. It is squash-merged, so it lands as one patch in the stack with a
   Conventional Commits subject (`feat(<area>): ...`).
3. Keep it rebase-friendly:
   - **Off by default** behind a config flag.
   - **New files** for most of the code, with small hooks into upstream files.
     Conflicts on migration only happen where you edit upstream's files.
   - **Tests** for the feature, so a migration can show it still works.

## Ownership

You own the patches you contribute. If one conflicts or its tests fail during
a migration, it is dropped from that release rather than holding the
migration up, and you send an updated version against the new `billy/live`.
The long-term home for a general feature is an upstream PR: every patch
upstream accepts is one fewer patch this fork carries.
