#!/usr/bin/env python3
"""push_guard.py - pre-push stale-base guard for the prepare-pr skill.

Verifies that the current HEAD is safe to force-push by checking:
1. The fetch of origin/<base> succeeds (fail closed on network error).
2. origin/<base> is an ancestor of HEAD (i.e. HEAD sits on the freshly
   fetched base tip, not on a stale fork point that would cause the squash
   to bake in reversions of newer base changes).
3. The number of commits HEAD is ahead of origin/<base> is plausibly small
   (default threshold: 5 commits for a single-commit PR workflow; configurable
   via --max-ahead).
4. None of the ahead-commits are patch-equivalent to commits in a bounded
   window (REPLAY_HISTORY_WINDOW) of origin/<base> history.  Comparison uses
   `git patch-id --stable` so renames, whitespace, and commit metadata are
   ignored — only the semantic diff matters.  The window is bounded because
   full history is unbounded cost, and replayed commits from a recent stale
   fork are by construction recent.

This prevents the catastrophic failure mode where a worktree branched from a
local integration trunk (kiki-trunk) carries 100+ unshipped commits that get
force-pushed to the remote feature branch, clobbering upstream work.

Portable: stdlib only; shells out to git via argument lists.

Usage:  python3 push_guard.py [--base <branch>] [--max-ahead <N>]
Exit:   0 SAFE | 40 REFUSED (stale base detected) | 2 environment error
"""

import argparse
import re
import shutil
import subprocess
import sys

# Single source of truth for the default max-ahead threshold.  Shared by
# preflight.py (which imports this constant) so the two scripts cannot drift.
DEFAULT_MAX_AHEAD = 5

# Maximum number of base-history commits to scan for patch-id equivalence in
# the replay-detection check.  Bounded because full history is unbounded cost,
# and replayed commits from a recent stale fork are by construction recent
# (the operator branched from a stale tip that was N commits behind; the
# replayed patches are in that window).  500 is generous — a typical PR
# workflow replays at most a few dozen commits from a stale integration trunk.
REPLAY_HISTORY_WINDOW = 500

# Test-only injection point: when set to a non-None list, run() uses it
# instead of resolving "git" from PATH.  Allows tests to monkeypatch a
# Python-based fake git directly (e.g. push_guard._GIT_CMD = [sys.executable,
# str(fake_script)]) without platform-specific PATH/shell wrappers or
# environment-variable indirection.
_GIT_CMD: list[str] | None = None

# Pattern that matches URL userinfo (user, user:pass, or token before @host)
# in scheme-bearing URLs only (requires "://" prefix).
# Covers https://user:token@host, ssh://user@host, git://token@host.
# Does NOT cover scheme-less scp-style remotes (user@host:path) — those are
# handled by _SCP_USERINFO_RE (pass 3).
_URL_USERINFO_RE = re.compile(r"://[^/@\s]+@")

# Pattern that matches everything after the authority (host[:port]) in a
# scheme-bearing URL — i.e. the path, query string, and fragment.
# Rationale: the fetch target is always `origin`, so the path carries no
# diagnostic value the operator does not already have, while it CAN carry
# path-embedded tokens (some forges/proxies put PATs in the URL path).
# Redacting the entire post-authority portion in one pass closes userinfo,
# path-token, query-string-token, and fragment-token credential shapes
# simultaneously, preventing the layered-redaction rounds that seeded
# repeated findings.  The host[:port] is preserved for diagnostic value.
# Captures: group 1 = scheme://host[:port], remainder begins at /, ?, or #.
#
# Scheme match: RFC 3986 § 3.1 generic grammar (ALPHA *( ALPHA / DIGIT /
# "+" / "-" / "." )) followed by "://".  This is scheme-agnostic — it
# covers http, https, ssh, git, ftp, ftps, svn+ssh, and any current or
# future remote-helper scheme without enumeration.
#
# Authority forms covered (RFC 3986 § 3.2):
#   - reg-name / IPv4:   host chars excluding delimiters (: / ? # and ws)
#   - Bracketed IPv6:    [<anything>] — covers hex:colon addresses, zone-ids
#     (%25eth0), and IPvFuture forms without an itemized charset.
# The remainder trigger is [/?#] (not just /) so query-only URLs
# (https://host?token=x) and fragment-only URLs are also redacted.
#
# Optional userinfo prefix: after pass 1 rewrites ``://user:tok@[::1]/path``
# to ``://<redacted>@[::1]/path``, the authority as seen by pass 2 starts
# with ``<redacted>@``.  Without accepting that prefix the bracketed-IPv6
# branch cannot match, and the path (which may carry credentials) survives.
# The prefix ``(?:[^\s/@\"']+@)?`` is deliberately broad — it accepts both
# the literal ``<redacted>@`` form AND raw userinfo (defense in depth if
# pass ordering ever changes).  It is non-capturing so group 1 semantics
# (scheme://[userinfo@]host[:port]) are preserved in the replacement.
_URL_AFTER_AUTHORITY_RE = re.compile(
    r"([a-zA-Z][a-zA-Z0-9+.\-]*://"
    r"(?:[^\s/@\"']+@)?"  # optional userinfo (raw or pass-1 <redacted>@) before host
    r"(?:\[[^\]]+\]|[^\s/:?#\"']+)"  # host: bracketed IPv6 (anything inside []) OR reg-name/IPv4
    r"(?::\d+)?)"  # optional port
    r"[/?#][^\s\"']*"  # remainder: starts at /, ?, or #
)

# Pattern that matches scheme-less SCP-style userinfo: ``user@host:path``,
# ``user@host:``, or bare ``user@host`` as they appear in git/ssh stderr.
# These shapes have NO ``://`` prefix, so passes 1 and 2 cannot reach them.
#
# Anchored to start-of-string, start-of-line, or after whitespace / quoting
# characters (git/ssh stderr wraps remotes in quotes/parens).
#
# Host alternation (group 2) matches four forms:
#   A) Dotted host — contains at least one dot, with or without colon-path.
#      Matches ``user@git.example.com:path`` and ``user@git.example.com``.
#   B) Dotless host followed by a colon — the colon-path is what identifies
#      an scp remote vs. prose like ``signed-off-by user@word``.  The colon
#      is consumed by a lookahead so it stays in the output.  Matches
#      ``user@forge:team/repo.git``, ``user@buildbox:``.
#   C) Bracketed IPv6 host — ``[<addr>]`` with optional zone-id.  Matches
#      ``user@[::1]:path``, ``user@[2001:db8::7]:path``,
#      ``user@[fe80::1%25eth0]:path``.
#   D) Un-bracketed IPv6 — as echoed by SSH after git strips brackets from
#      SCP-style remotes.  Two or more colons with hex digits between them.
#      Matches ``user@::1``, ``user@2001:db8::7``, ``user@fe80::1%eth0``.
#
# Over-redaction of non-credential usernames (git@, ssh usernames, email-like
# tokens) in a refusal diagnostic is acceptable and safe — under-redaction is
# the failure mode we guard against.
_SCP_USERINFO_RE = re.compile(
    r"(?:^|(?<=[\s\"'`(\[]))"  # anchor: start or after whitespace/quote/paren/bracket
    r"([^\s/@:]+)"  # group 1: userinfo (no slashes, @, colons, whitespace)
    r"@"
    r"("  # group 2: host — one of four alternations
    r"\[[^\]]+\]"  # C) bracketed IPv6 (anything inside [])
    r"|[0-9a-fA-F]*:[0-9a-fA-F]*:[0-9a-fA-F:%]*"  # D) un-bracketed IPv6 (≥2 colons, hex+colon+zone-id)
    r"|[^\s/:@\"'`)\]]+\.[^\s/:@\"'`)\]]+"  # A) dotted host (must contain a dot)
    r"|[^\s/:@\"'`)\]]+(?=:)"  # B) dotless host (lookahead requires colon-path)
    r")"
)


def redact_credentials(text: str) -> str:
    """Strip URL credentials from diagnostic text to prevent leaks.

    Three passes (order matters — pass 3 must run after pass 1 so it does not
    mangle the already-redacted ``://<redacted>@host`` output):

    1. Scheme-bearing userinfo: replaces ``://user:token@`` or ``://user@``
       with ``://<redacted>@`` so the host remains visible.
    2. Post-authority: replaces everything after the host[:port] (path, query
       string, and fragment) with ``/<redacted>`` for ANY scheme-bearing URL
       (RFC 3986 generic scheme grammar: http, https, ssh, git, ftp, ftps,
       svn+ssh, and any future remote-helper scheme — no enumeration).  This
       subsumes path-embedded tokens, query-string credentials
       (private_token=, access_token=, etc.), and fragment credentials in a
       single pass.  The pattern accepts an optional userinfo prefix before
       the host — this is critical for PASS COMPOSITION: after pass 1
       rewrites ``://user:tok@[::1]/path`` to ``://<redacted>@[::1]/path``,
       pass 2 must still match the bracketed-IPv6 authority to redact the
       path.  Without the prefix, credentials in the post-authority portion
       of any URL with userinfo AND a bracketed-IPv6 host would survive.
    3. Scheme-less SCP-style userinfo: replaces ``user@host`` in tokens that
       lack a ``://`` scheme with ``<redacted>@host``.  Host shapes covered:
       (A) dotted host — ``tok@git.example.com:team/Repo.git``;
       (B) dotless host with colon-path — ``tok@forge:team/repo.git`` (the
       colon-path distinguishes from prose like ``user@word``);
       (C) bracketed IPv6 — ``tok@[::1]:path``, ``tok@[2001:db8::7]:path``;
       (D) un-bracketed IPv6 — ``tok@::1``, ``tok@2001:db8::7`` (as echoed
       by SSH after git strips brackets from SCP-style remotes).
       The host is preserved for diagnostic value.  Over-redaction of
       non-credential usernames (git@, ssh user names) is acceptable and safe;
       under-redaction is the failure mode.

    The authority pattern (pass 2) covers reg-name/IPv4 hosts AND bracketed
    IPv6 authorities (``[::1]``, ``[2001:db8::7]``, with optional zone-id).
    The remainder trigger is ``/``, ``?``, or ``#`` — so query-only URLs
    (``https://host?private_token=x``) are also redacted.

    After all three passes, the output for any URL form is:
    - Scheme-bearing: at most ``scheme://host[:port]/<redacted>``
      (or ``scheme://<redacted>@host[:port]/<redacted>`` if userinfo present).
    - SCP-style: ``<redacted>@host:path`` (or ``<redacted>@host`` for dotted
      hosts without colon-path).  Host may be dotted, dotless (colon-path
      required), bracketed IPv6, or un-bracketed IPv6.
    The host is always preserved for diagnostic value.

    Shared by preflight.py (which imports this helper).
    """
    # Pass 1: scheme-bearing userinfo (requires "://").
    text = _URL_USERINFO_RE.sub("://<redacted>@", text)
    # Pass 2: post-authority path/query/fragment in scheme-bearing URLs.
    text = _URL_AFTER_AUTHORITY_RE.sub(r"\1/<redacted>", text)
    # Pass 3: scheme-less SCP-style userinfo (user@host).
    # Runs after pass 1 so "://<redacted>@host" is already neutralized and
    # won't be double-processed (the "://" prefix means _SCP_USERINFO_RE's
    # anchor — start/whitespace/quote — cannot match mid-URL).
    text = _SCP_USERINFO_RE.sub(r"<redacted>@\2", text)
    return text


def run(args):
    """Run a command; return (returncode, stdout, stderr) as stripped text.

    When the module-level _GIT_CMD is set (test monkeypatch), "git" is
    replaced with the specified command list — no PATH/shell wrappers or
    environment-variable indirection needed.  Otherwise git is resolved via
    shutil.which so PATH-injected wrappers (including .bat/.cmd on Windows)
    are found without shell=True.
    """
    try:
        if args and args[0] == "git":
            if _GIT_CMD:
                args = _GIT_CMD + list(args[1:])
            else:
                resolved = shutil.which("git")
                if resolved:
                    args = [resolved] + list(args[1:])
        p = subprocess.run(args, capture_output=True, text=True, errors="replace")
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except OSError as exc:
        return 127, "", "{}: {}".format(args[0], exc)


def err(msg):
    sys.stderr.write(msg + "\n")


def _resolve_base(base_arg):
    """Resolve the base branch name from arg, symbolic ref, or default."""
    base = base_arg
    if not base:
        sym = run(["git", "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"])[1]
        if sym.startswith("origin/"):
            base = sym[len("origin/") :]
        if not base:
            base = "main"
    return base


# Allowlist of known git fetch failure classes.  Each entry is a tuple of
# (compiled regex applied case-insensitively to stderr, user-facing label).
# When none match, the diagnostic withholds the raw text entirely — free-text
# stderr cannot be closed by shape enumeration alone (round-13 lesson).
_FETCH_ERROR_CLASSES: list[tuple["re.Pattern[str]", str]] = [
    (re.compile(r"could not resolve host", re.IGNORECASE), "could not resolve host"),
    (re.compile(r"name or service not known", re.IGNORECASE), "DNS resolution failed"),
    (re.compile(r"permission denied", re.IGNORECASE), "permission denied"),
    (re.compile(r"repository not found", re.IGNORECASE), "repository not found"),
    (re.compile(r"does not appear to be a git repo", re.IGNORECASE), "not a git repository"),
    (re.compile(r"not a git repository", re.IGNORECASE), "not a git repository"),
    (re.compile(r"timed? ?out", re.IGNORECASE), "connection timed out"),
    (re.compile(r"connection refused", re.IGNORECASE), "connection refused"),
    (re.compile(r"connection reset", re.IGNORECASE), "connection reset"),
    (re.compile(r"couldn't connect to server", re.IGNORECASE), "could not connect"),
    (re.compile(r"ssl|tls|certificate", re.IGNORECASE), "TLS/certificate error"),
    (re.compile(r"authentication failed", re.IGNORECASE), "authentication failed"),
    (re.compile(r"invalid credentials", re.IGNORECASE), "authentication failed"),
    (re.compile(r"remote:.*not found", re.IGNORECASE), "remote ref not found"),
    (re.compile(r"couldn't find remote ref", re.IGNORECASE), "remote ref not found"),
    (re.compile(r"no matching remote head", re.IGNORECASE), "remote ref not found"),
]


def _classify_fetch_error(stderr: str) -> str:
    """Derive a safe diagnostic from git fetch stderr.

    Returns one of:
    - A matched error class label (e.g. "could not resolve host") when stderr
      contains a recognized git/ssh/curl failure pattern.
    - A generic withholding message when no pattern matches — free-text stderr
      can carry bare tokens (e.g. from ext:: remote helpers, pre-push hook
      output, or credential-helper error messages) that no URL-shape scrubber
      can redact.  The operator can run ``git fetch`` manually to see the full
      error in their own terminal.

    Contract: the return value NEVER contains raw stderr content that did not
    match the allowlist.  Only the matched class label (a hardcoded literal)
    is surfaced.  This closes the free-text credential egress class entirely
    rather than chasing individual shapes (round-13 lesson: 13 consecutive
    rounds of shape enumeration proved the approach cannot converge).
    """
    for pattern, label in _FETCH_ERROR_CLASSES:
        if pattern.search(stderr):
            return label
    return "fetch failed (details withheld — run git fetch manually to see the error)"


def _fetch_base(base):
    """Fetch origin/<base>; return 0 on success or 40 on failure.

    Uses an explicit refspec (+refs/heads/<base>:refs/remotes/origin/<base>)
    so the remote-tracking ref is always updated regardless of the clone's
    configured remote.origin.fetch (e.g. single-branch clones, narrow CI
    checkouts).  The leading '+' ensures non-fast-forward updates are accepted
    (required after an upstream force-push of the base branch).

    On failure, prints a REFUSED diagnostic with the classified error (from
    ``_classify_fetch_error``) — never raw stderr, which may contain bare
    tokens from remote helpers or credential error messages.
    """
    print("Fetching origin/{} ...".format(base))
    refspec = "+refs/heads/{}:refs/remotes/origin/{}".format(base, base)
    fetch_rc, _, fetch_err = run(["git", "fetch", "--quiet", "origin", refspec])
    if fetch_rc != 0:
        diagnostic = _classify_fetch_error(fetch_err)
        err(
            "REFUSED: git fetch origin {} failed. Cannot verify merge-base "
            "freshness — refusing to push on a potentially stale ref.\n"
            "  error class: {}".format(base, diagnostic)
        )
        return 40
    return 0


def _check_single_on_base(base):
    """Post-squash structural guard: assert HEAD~1 == origin/<base>.

    After a squash, the single commit should sit directly on the freshly
    fetched origin/<base>.  If HEAD~1 != origin/<base>, the squash landed
    on a stale ref or the branch carries unexpected history.

    Returns: 0 safe, 40 refused.
    """
    rc, head_parent, _ = run(["git", "rev-parse", "HEAD~1"])
    if rc != 0:
        err(
            "REFUSED: cannot resolve HEAD~1. The branch may have no parent "
            "commit (single root commit with no base)."
        )
        return 40

    rc, origin_base_sha, _ = run(["git", "rev-parse", "origin/{}".format(base)])
    if rc != 0:
        err("REFUSED: cannot resolve origin/{}.".format(base))
        return 40

    head_sha = run(["git", "rev-parse", "HEAD"])[1][:12]

    print("HEAD~1:          " + head_parent[:12])
    print("origin/{}:     {}".format(base, origin_base_sha[:12]))
    print("HEAD:            " + head_sha)

    if head_parent != origin_base_sha:
        err(
            "REFUSED: HEAD~1 ({}) != origin/{} ({}). "
            "The squashed commit does not sit directly on the freshly fetched "
            "remote base — either the squash landed on a stale ref or the "
            "branch carries unexpected history.\n"
            "  To fix: rebase onto the fresh origin/{} first "
            "(git rebase origin/{}), then re-squash "
            "(git reset --soft origin/{} && git commit).".format(
                head_parent[:12], base, origin_base_sha[:12], base, base, base
            )
        )
        return 40

    print("STATUS: SAFE TO PUSH (single commit on base)")
    return 0


def _check_pre_squash(base, max_ahead):
    """Pre-squash guard: merge-base ancestry, commit count, replayed commits.

    Returns: 0 safe, 40 refused.

    Fail-closed contract: every git subprocess failure (nonzero exit code or
    OSError) refuses the push (exit 40) with a diagnostic naming the failed
    operation.  The ONLY paths that return 0 ("SAFE TO PUSH") are those where
    the git command SUCCEEDED and its output is genuinely empty (no ahead
    commits, or an empty diff for a single commit which is skipped).
    """
    # Compute merge-base of HEAD and freshly-fetched origin/<base>.
    rc, merge_base, _ = run(["git", "merge-base", "HEAD", "origin/{}".format(base)])
    if rc != 0 or not merge_base:
        err(
            "REFUSED: cannot compute merge-base between HEAD and origin/{}. "
            "The branch may have no common history with the remote base.".format(base)
        )
        return 40

    # Verify origin/<base> is an ancestor of HEAD — i.e. HEAD sits on the
    # freshly fetched base tip.  After a correct rebase (Phase 1 step 2),
    # this is always true.  If it fails, the branch forks from a stale base
    # and the squash would bake in reversions of newer base changes.
    rc, _, _ = run(["git", "merge-base", "--is-ancestor", "origin/{}".format(base), "HEAD"])
    if rc != 0:
        err(
            "REFUSED: HEAD is not based on the fresh origin/{} tip — the "
            "branch forks from a stale base and squashing would bake in "
            "reversions of newer base changes. Rebase onto origin/{} "
            "first.".format(base, base)
        )
        return 40

    # Count commits HEAD is ahead of origin/<base>.
    rc, count_str, _ = run(["git", "rev-list", "--count", "origin/{}..HEAD".format(base)])
    if rc != 0:
        err("REFUSED: cannot count commits ahead of origin/{}.".format(base))
        return 40

    try:
        ahead = int(count_str)
    except ValueError:
        err("REFUSED: unexpected rev-list output: {}".format(count_str))
        return 40

    origin_base_sha = run(["git", "rev-parse", "origin/{}".format(base)])[1][:12]
    head_sha = run(["git", "rev-parse", "HEAD"])[1][:12]

    print("merge-base:      " + merge_base[:12])
    print("origin/{}:     {}".format(base, origin_base_sha))
    print("HEAD:            " + head_sha)
    print("commits ahead:   {}".format(ahead))
    print("max allowed:     {}".format(max_ahead))

    if ahead > max_ahead:
        err(
            "REFUSED: HEAD is {} commits ahead of origin/{} (max allowed: {}). "
            "This is far too many for a squashed single-commit PR — the branch "
            "likely carries unshipped local integration commits that would "
            "clobber upstream work if force-pushed.\n"
            "  To fix (if you authored all {} commits): squash them down "
            "(git reset --soft origin/{} && git commit) so the branch carries "
            "a single deliverable commit.\n"
            "  To fix (if any ahead-commit is unfamiliar): STOP and diagnose — "
            "do not squash foreign history. The branch may have picked up "
            "commits from a local integration trunk.\n"
            "  To fix (stale fork): rebase onto origin/{} "
            "(git rebase origin/{}) so HEAD sits on the fresh remote "
            "base tip.".format(ahead, base, max_ahead, ahead, base, base, base)
        )
        return 40

    # Detect replayed commits via patch-id comparison.
    # Compare each ahead-commit's patch-id against a BOUNDED window of
    # origin/<base> history.  If any ahead-commit is patch-equivalent to a
    # base-history commit, the branch replays upstream patches (e.g. from a
    # stale fork that cherry-picked base commits back).  The window is bounded
    # (REPLAY_HISTORY_WINDOW) because full history is unbounded cost, and
    # replayed commits from a recent stale fork are by construction recent.
    #
    # Step 1: get patch-ids for ahead-commits (origin/<base>..HEAD).
    rc, ahead_revs, _ = run(["git", "rev-list", "origin/{}..HEAD".format(base)])
    if rc != 0:
        err(
            "REFUSED: git rev-list origin/{}..HEAD failed (exit {})."
            " Cannot verify replay safety — failing closed.".format(base, rc)
        )
        return 40
    if not ahead_revs:
        # rev-list SUCCEEDED with empty output — genuinely 0 ahead commits.
        print("STATUS: SAFE TO PUSH")
        return 0

    ahead_patch_ids: dict[str, str] = {}  # patch-id → commit-sha
    for commit_sha in ahead_revs.splitlines():
        # Get the patch content and pipe to patch-id.
        diff_rc, diff_out, _ = run(["git", "diff-tree", "-p", commit_sha])
        if diff_rc != 0:
            err(
                "REFUSED: git diff-tree -p {} failed (exit {})."
                " Cannot verify replay safety — failing closed.".format(commit_sha[:12], diff_rc)
            )
            return 40
        if not diff_out:
            # diff-tree SUCCEEDED with empty output — empty commit, skip.
            continue
        # Feed the diff to git patch-id --stable via stdin.
        try:
            pid_proc = subprocess.run(
                ["git", "patch-id", "--stable"],
                input=diff_out,
                capture_output=True,
                text=True,
                errors="replace",
            )
            if pid_proc.returncode != 0:
                err(
                    "REFUSED: git patch-id --stable failed (exit {}) for"
                    " commit {}. Cannot verify replay safety —"
                    " failing closed.".format(pid_proc.returncode, commit_sha[:12])
                )
                return 40
            if pid_proc.stdout.strip():
                patch_id = pid_proc.stdout.strip().split()[0]
                ahead_patch_ids[patch_id] = commit_sha
        except OSError as exc:
            err(
                "REFUSED: git patch-id --stable raised OSError for commit"
                " {}: {}. Cannot verify replay safety —"
                " failing closed.".format(commit_sha[:12], exc)
            )
            return 40

    if not ahead_patch_ids:
        print("STATUS: SAFE TO PUSH")
        return 0

    # Step 2: get patch-ids for bounded base history.
    rc, base_revs, _ = run(
        [
            "git",
            "rev-list",
            "--max-count={}".format(REPLAY_HISTORY_WINDOW),
            "origin/{}".format(base),
        ]
    )
    if rc != 0:
        err(
            "REFUSED: git rev-list --max-count={} origin/{} failed (exit {})."
            " Cannot verify replay safety — failing closed.".format(REPLAY_HISTORY_WINDOW, base, rc)
        )
        return 40
    if not base_revs:
        # rev-list SUCCEEDED with empty output — no base history to compare.
        print("STATUS: SAFE TO PUSH")
        return 0

    base_patch_ids: dict[str, str] = {}  # patch-id → commit-sha
    for commit_sha in base_revs.splitlines():
        diff_rc, diff_out, _ = run(["git", "diff-tree", "-p", commit_sha])
        if diff_rc != 0:
            err(
                "REFUSED: git diff-tree -p {} (base history) failed (exit {})."
                " Cannot verify replay safety — failing closed.".format(commit_sha[:12], diff_rc)
            )
            return 40
        if not diff_out:
            # diff-tree SUCCEEDED with empty output — empty commit, skip.
            continue
        try:
            pid_proc = subprocess.run(
                ["git", "patch-id", "--stable"],
                input=diff_out,
                capture_output=True,
                text=True,
                errors="replace",
            )
            if pid_proc.returncode != 0:
                err(
                    "REFUSED: git patch-id --stable failed (exit {}) for"
                    " base commit {}. Cannot verify replay safety —"
                    " failing closed.".format(pid_proc.returncode, commit_sha[:12])
                )
                return 40
            if pid_proc.stdout.strip():
                patch_id = pid_proc.stdout.strip().split()[0]
                base_patch_ids[patch_id] = commit_sha
        except OSError as exc:
            err(
                "REFUSED: git patch-id --stable raised OSError for base"
                " commit {}: {}. Cannot verify replay safety —"
                " failing closed.".format(commit_sha[:12], exc)
            )
            return 40

    # Step 3: find matches.
    replayed_pairs: list[tuple[str, str]] = []  # (ahead-sha, base-sha)
    for patch_id, ahead_sha in ahead_patch_ids.items():
        if patch_id in base_patch_ids:
            replayed_pairs.append((ahead_sha, base_patch_ids[patch_id]))

    if replayed_pairs:
        pair_strs = ["{} ↔ {}".format(a[:12], b[:12]) for a, b in replayed_pairs]
        err(
            "REFUSED: {} ahead-commit(s) are patch-equivalent to commits "
            "already on origin/{} — the branch replays upstream history.\n"
            "  replayed (ahead ↔ base): {}\n"
            "  To fix: STOP and diagnose. Do not squash — one or more "
            "ahead-commits duplicate patches already on the base branch. "
            "Rebase onto a fresh origin/{} so only novel changes remain, "
            "or cherry-pick your original commits onto "
            "origin/{}.".format(len(replayed_pairs), base, ", ".join(pair_strs), base, base)
        )
        return 40

    print("STATUS: SAFE TO PUSH")
    return 0


def main():
    parser = argparse.ArgumentParser(description="Pre-push stale-base guard")
    parser.add_argument(
        "--base",
        default="",
        help="Base branch name (without origin/ prefix). "
        "Auto-detected from PR or origin/HEAD if omitted.",
    )
    parser.add_argument(
        "--max-ahead",
        type=int,
        default=DEFAULT_MAX_AHEAD,
        help="Maximum commits HEAD may be ahead of origin/<base> (default: {}).".format(
            DEFAULT_MAX_AHEAD
        ),
    )
    parser.add_argument(
        "--require-single-on-base",
        action="store_true",
        default=False,
        help="Post-squash mode: assert HEAD~1 == origin/<base> after a fresh "
        "fetch (the single squashed commit sits directly on the remote base).",
    )
    args = parser.parse_args()

    # Must be in a git repo.
    if run(["git", "rev-parse", "--is-inside-work-tree"])[0] != 0:
        err("ERROR: not inside a git repository.")
        return 2

    base = _resolve_base(args.base)

    # Fetch origin/<base> — MUST succeed (fail closed) for both modes.
    fetch_result = _fetch_base(base)
    if fetch_result != 0:
        return fetch_result

    if args.require_single_on_base:
        return _check_single_on_base(base)
    else:
        return _check_pre_squash(base, args.max_ahead)


if __name__ == "__main__":
    sys.exit(main())
