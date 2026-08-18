"""App boundary fence: this branch changes nothing outside the Spec_App's trees.

This project once put roughly 7,700 lines inside ``spec_builder`` — a
pre-existing app owned by another team — including a deleted line in that app's
manifest that retired one of its declared skills. The restoration undid it. This
module is what stops it recurring: it lists every path this branch changed and
requires each one to lie under a root the Spec_App declares as its own or to
appear in :data:`BOUNDARY_ALLOWLIST` with a written justification.

**Fail-closed is the whole design.** The baseline is
``git merge-base origin/main HEAD``. If that cannot be answered — no
``origin/main`` ref, a shallow clone, an orphan history, a directory that is not
a repository — :func:`merge_base` RAISES and the fence FAILS with the reason. A
fence that cannot compute its baseline reporting a clean tree is worse than no
fence, because it reports the same green as a clean one. Three verifications on
the prior project could not fail (a piped exit status, a quiet formatter, and a
path comparison that always found zero overlap); the third is why every path
here is normalized to repo-relative on BOTH sides before it is matched, and why
:class:`TestNormalizationCannotSilenceTheFence` drives an absolute-form path
list through the fence and requires the identical verdict.

**What this can see.** Every path in ``git diff --name-only <merge-base>..HEAD``
(added, modified, renamed-to, and deleted), every tracked working-tree change,
and every untracked file git does not ignore. Each is classified textually, so a
declared root that does not exist on disk yet is still declared territory.

**What this cannot see**, stated plainly because it is the boundary of the
guarantee:

* **A change git ignores.** An offending file matched by ``.gitignore`` is
  invisible here, and so is one inside an ignored directory. Such a file also
  cannot reach a reviewer or a release, which is the reason this is affordable
  rather than the reason it is fine.
* **What a change DOES.** The fence reads paths, not diffs. A file inside a
  declared root that reaches into another app by import is in-bounds here; the
  library-equivalence and provenance suites are where behaviour is judged.
* **Whether an allowlisted change is genuinely shared.** An entry is admitted on
  the strength of its written justification, which is a claim a human made. The
  fence only forces the claim to exist, to be attached to a path prefix, and to
  be narrow enough not to cover another app's tree
  (:class:`TestTheAllowlistCannotSwallowAnotherApp`).
* **A rename's source path is no longer invisible**: the change list passes
  ``--no-renames``, so a file moved out of another app's tree appears as a
  deletion there and an addition here — both judged. What remains unseen is
  git-ignored content, what a change does (paths only), and whether an
  allowlisted claim is true.
* **A ``..`` segment in a scanned path.** Matching is lexical, so
  ``spec_engine/../<other-app>/x.py`` would be admitted as declared territory
  while denoting another app's file. Git never emits ``..`` segments in change
  lists, so no real scan can present one — allowlist ENTRIES are ``..``-checked
  because a human writes those.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

import kiro_crew

PKG_ROOT = Path(kiro_crew.__file__).resolve().parent
REPO_ROOT = PKG_ROOT.parents[1]
BUILTINS_ROOT = PKG_ROOT / "apps" / "builtins"

#: The ref the branch diverged from. Named once so the failure message and the
#: computation cannot describe different baselines.
DEFAULT_BRANCH_REF = "origin/main"

#: The roots the Spec_App declares as its own, as repo-relative prefixes. Every
#: one ends in ``/``: a bare ``.../spec_engine`` prefix would also admit a
#: sibling directory whose name merely starts with it, which is the near-miss
#: :class:`TestPlantedViolationsAreReported` drives.
DECLARED_ROOTS: tuple[str, ...] = (
    "src/kiro_crew/apps/builtins/spec_engine/",
    "website/src/apps/spec-engine/",
)

#: The Spec_App's share of the shared frontend test directory. A prefix rather
#: than a root because that directory belongs to no single app: each app's files
#: are identified by name there, the same convention the provenance scan uses for
#: the Prior_App's ``SpecBuilder*`` fixtures.
DECLARED_TEST_PREFIXES: tuple[str, ...] = ("website/src/test/SpecEngine",)

#: Paths outside the Spec_App's trees this branch is allowed to change, each with
#: the one-line justification that admits it. Reviewed, never inferred: a change
#: outside the declared roots is a boundary crossing until a human writes down
#: why it is shared, and the justification is what a reviewer reads.
#:
#: Every entry is an exact file path or a directory prefix ending in ``/``. None
#: may cover another app's tree — that is asserted, not trusted.
BOUNDARY_ALLOWLIST: tuple[tuple[str, str], ...] = (
    (
        ".kiro/specs/",
        "The spec records this project is executed from: requirements, design, tasks.",
    ),
    (
        "website/src/components/appstore/appManifest.ts",
        "The shared store key table; an app has no card without its own row here.",
    ),
    (
        "website/src/test/appManifest.test.ts",
        "That table's own suite: the spec-engine row is asserted where the table is.",
    ),
    (
        "website/src/i18n/",
        "The shared locale catalogs; the Spec_App's store strings have no other home.",
    ),
    (
        "website/scripts/check-app-manifest-sync.mjs",
        "The manifest-sync gate, taught to demand a page_label exactly when a page exists.",
    ),
    (
        "website/src/apps/builtinRegistry.ts",
        "The shared route table; a builtin page is unreachable without its row here.",
    ),
    (
        "website/src/apps/builtinIcons.tsx",
        "The shared nav-icon table; an unregistered icon name falls back to a generic glyph.",
    ),
    (
        "website/eslint.i18n.config.js",
        "The shared i18n lint's CSS-only path exemptions, where the sibling app's already is.",
    ),
    (
        "src/kiro_crew/apps/builtins/__init__.py",
        "The shared builtin roster; a builtin's routes register only from that list.",
    ),
    (
        "src/kiro_crew/apps/approval_grants.py",
        "App Kit platform module owning per-app approval grants for every app.",
    ),
    (
        "src/kiro_crew/apps/manifest.py",
        "Adds permissions.approvalMode to the shared manifest schema.",
    ),
    (
        "src/kiro_crew/apps/bridges.py",
        "Resolves an app's granted posture at registration, for all apps.",
    ),
    (
        "src/kiro_crew/apps/cron_sdk.py",
        "Clamps an app-registered cron's posture to its grant on the shared SDK.",
    ),
    (
        "src/kiro_crew/mcp_cron.py",
        "Re-checks a stored posture at fire time, where the job actually fires.",
    ),
    (
        "src/kiro_crew/security.py",
        "A keystone sensitive-path leaf can only be declared in the keystone module.",
    ),
    (
        "src/kiro_crew/slack/gateway.py",
        "Routes the gateway's cron env build through the one reserved-var owner.",
    ),
    (
        "test/test_app_approval_grants.py",
        "The platform suite for the grants module; platform code is tested there.",
    ),
    (
        "test/test_app_bridges.py",
        "Covers registration-time posture resolution in the platform's own suite.",
    ),
    (
        "test/test_cron_sdk.py",
        "Mirrors the SDK's new approval_mode/timeout fields onto MockCronJob.",
    ),
    (
        "test/test_security.py",
        "Covers the new sensitive-path leaf in the platform's own suite.",
    ),
    (
        "test/test_spawn_audit.py",
        "Adds a BENIGN_SPAWNS exemption for the delivery-isolation git helper.",
    ),
    (
        "docs/app-kit/manifest-reference.md",
        "Documents the new manifest permission in the same commit as the schema.",
    ),
    (
        "docs/system-specs/modules/security.md",
        "Records the new sensitive-path keystone leaf, as that spec requires.",
    ),
    (
        "docs/system-specs/common/code-style.md",
        "Indexes the engine's settings registry in the repo-wide constants index.",
    ),
    (
        ".kirocrew-spec-test/",
        "Throwaway KIROCREW_HOME for this project's harness runs; carries no source.",
    ),
    (
        "tmp/",
        "Workspace-local scratch for build and gate logs; carries no source.",
    ),
    (
        "website/tmp/",
        "The frontend's own scratch for gate logs and throwaway helpers; nothing ships from it.",
    ),
    (
        ".spec_engine_mutation_probe.lock",
        "Lock the engine's mutation-probe harness leaves beside the tree; carries no source.",
    ),
)

#: Allowlist entries that legitimately match nothing. Everything else must match a
#: path this branch actually changed, so a stale entry is reported rather than
#: quietly widening the fence for the next project that reads it as precedent.
ALLOWLIST_MAY_MATCH_NOTHING = frozenset(
    {
        ".kirocrew-spec-test/",  # created by a harness run, absent on a clean checkout
        "tmp/",  # created by a gate run, absent on a clean checkout
        "website/tmp/",  # created by a frontend gate run, absent on a clean checkout
        # Created by a mutation-probe run and deliberately never unlinked --
        # ``mutation_probe._tree_lock`` explains why removing it would reopen the
        # race the lock closes -- so it is absent until the first probe runs.
        ".spec_engine_mutation_probe.lock",
    }
)


class MergeBaseUnavailable(RuntimeError):
    """The baseline could not be computed, so the fence has no verdict to give."""


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run git in *root*, never raising on a non-zero status: callers read it."""
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def merge_base(root: Path) -> str:
    """The commit this branch diverged from, or raise :class:`MergeBaseUnavailable`.

    Raising is the point. Every way this can fail — not a repository, no
    ``origin/main``, a shallow clone whose history stops short, an orphan branch
    sharing no ancestor — produces a fence with no baseline, and a fence with no
    baseline must not return an empty violation list that reads as clean.
    """
    result = _git(root, "merge-base", DEFAULT_BRANCH_REF, "HEAD")
    base = result.stdout.strip()
    if result.returncode != 0 or not base:
        detail = (result.stderr or result.stdout).strip() or "no common ancestor"
        raise MergeBaseUnavailable(
            f"cannot compute the merge base of {DEFAULT_BRANCH_REF} and HEAD in "
            f"{root}: {detail}. The fence has no baseline, so it reports no verdict "
            "rather than a clean one."
        )
    return base


def repo_relative(raw: str, root: Path) -> str:
    """*raw* as a repo-relative POSIX path, whatever form it arrived in.

    Both sides of every comparison pass through here or through
    :func:`_normalized_prefix`. Comparing an absolute path against a relative
    prefix is the shape of a check that always finds nothing and always passes;
    normalizing first is what makes a match mean what it says.

    The rewrite is LEXICAL — no ``resolve()``, no stat. Two reasons, and the
    second is why the first is not merely a preference: classification has to
    work for a declared root that does not exist yet, and ``resolve()`` raises
    on a symlink loop, so one broken link in a scratch directory would take the
    whole fence down with an error unrelated to any boundary. A path that is not
    under *root* lexically — including the same tree reached through a symlink —
    is returned unchanged, so it matches no declared root and is reported.
    """
    text = raw.strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    if not text:
        return ""
    if not text.startswith("/"):
        return text
    root_text = root.as_posix().rstrip("/")
    if text == root_text:
        return ""
    prefix = f"{root_text}/"
    if text.startswith(prefix):
        return text[len(prefix) :]
    return text  # outside the repository, or a spelling of it we cannot confirm


def _normalized_prefix(prefix: str) -> str:
    """A declared root or allowlist entry in the same form as a scanned path."""
    text = prefix.strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text.lstrip("/")


def branch_changed_paths(root: Path, base: str) -> list[str]:
    """Every path this branch touched, repo-relative and deduplicated.

    Three sources, because a trespass can sit in any of them: the committed range
    since *base*, the tracked working tree, and untracked files git does not
    ignore. An uncommitted new file inside another app's tree is the same
    trespass as a committed one, one commit earlier.
    """
    found: set[str] = set()
    for args in (
        # --no-renames: a detected rename reports only its destination, so a file
        # moved OUT of another app's tree into ours would be seen at its in-bounds
        # destination and the disappearance from theirs would be invisible. With
        # rename pairing off, both sides appear — the source as a deletion, which
        # is exactly the trespass the fence exists to report.
        ("diff", "--no-renames", "--name-only", f"{base}..HEAD"),
        ("diff", "--no-renames", "--name-only", "HEAD"),
        ("ls-files", "--others", "--exclude-standard"),
    ):
        result = _git(root, *args)
        if result.returncode != 0:
            raise MergeBaseUnavailable(
                f"cannot list branch changes ({' '.join(args)}) in {root}: "
                f"{(result.stderr or result.stdout).strip()}"
            )
        for line in result.stdout.splitlines():
            relative = repo_relative(line, root)
            if relative:
                found.add(relative)
    return sorted(found)


def admits(path: str) -> str | None:
    """Why *path* is in-bounds, or ``None`` when it is not.

    The returned reason is what a reader needs to audit an admission: which
    declared root or which allowlist entry did it. ``None`` is the reported case,
    including an empty path — an unreadable name is not admissible.
    """
    candidate = repo_relative(path, REPO_ROOT)
    if not candidate:
        return None
    for root in DECLARED_ROOTS:
        if candidate.startswith(_normalized_prefix(root)):
            return f"declared root {root}"
    for prefix in DECLARED_TEST_PREFIXES:
        if candidate.startswith(_normalized_prefix(prefix)):
            return f"declared test prefix {prefix}"
    for entry, justification in BOUNDARY_ALLOWLIST:
        normalized = _normalized_prefix(entry)
        if candidate == normalized or (
            normalized.endswith("/") and candidate.startswith(normalized)
        ):
            return f"allowlist {entry}: {justification}"
    return None


def _allowlist_entry_matching(path: str) -> str | None:
    """The allowlist entry that would admit *path*, ignoring declared roots."""
    candidate = repo_relative(path, REPO_ROOT)
    if not candidate:
        return None
    for entry, _ in BOUNDARY_ALLOWLIST:
        normalized = _normalized_prefix(entry)
        if candidate == normalized or (
            normalized.endswith("/") and candidate.startswith(normalized)
        ):
            return entry
    return None


def _camel(app_dir: str) -> str:
    """``spec_builder`` -> ``SpecBuilder``: how an app names its frontend files."""
    return "".join(part.capitalize() for part in app_dir.split("_"))


def _builtin_app_dirs() -> tuple[str, ...]:
    """Builtin app directory names, read from the tree rather than listed here."""
    return tuple(
        sorted(
            child.name
            for child in BUILTINS_ROOT.iterdir()
            if child.is_dir() and not child.name.startswith("__")
        )
    )


def owning_app(path: str) -> str:
    """Which app owns *path*, for the failure message a reviewer reads.

    Requirement 2.2 wants the file AND its owning app named, because "this file
    is not yours" is only actionable once the reader knows whose it is. A path
    that belongs to no app is the core platform's, and that is said plainly
    rather than guessed at.
    """
    candidate = repo_relative(path, REPO_ROOT)
    backend = "src/kiro_crew/apps/builtins/"
    frontend = "website/src/apps/"
    shared_tests = "website/src/test/"
    if candidate.startswith(backend):
        remainder = candidate[len(backend) :]
        if "/" in remainder:
            return f"the {remainder.split('/', 1)[0]} app"
    if candidate.startswith(frontend):
        remainder = candidate[len(frontend) :]
        if "/" in remainder:
            return f"the {remainder.split('/', 1)[0]} app's frontend"
    if candidate.startswith(shared_tests):
        name = candidate[len(shared_tests) :]
        for app_dir in _builtin_app_dirs():
            if name.startswith(_camel(app_dir)):
                return f"the {app_dir} app's frontend tests"
    return "the core platform"


def boundary_violations(paths: list[str]) -> list[str]:
    """Every path in *paths* that is neither declared territory nor allowlisted.

    Reported repo-relative so an offender reads identically whichever form it
    arrived in, and with its owning app so the reader knows whose file it is.
    """
    reported: list[str] = []
    for path in paths:
        if admits(path) is not None:
            continue
        shown = repo_relative(path, REPO_ROOT) or repr(path)
        reported.append(f"{shown} (owned by {owning_app(path)})")
    return reported


def fence_report(root: Path) -> list[str]:
    """The fence end to end: baseline, change list, verdict. Raises on no baseline."""
    return boundary_violations(branch_changed_paths(root, merge_base(root)))


# --- fixture repositories, for the paths the real repository cannot exercise ---


def _fixture_repo(tmp_path: Path, name: str = "repo") -> Path:
    root = tmp_path / name
    root.mkdir(parents=True)
    for args in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "fence@example.com"),
        ("config", "user.name", "fence"),
    ):
        result = _git(root, *args)
        assert result.returncode == 0, result.stderr
    return root


def _commit(root: Path, name: str, body: str) -> str:
    target = root / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    assert _git(root, "add", "-A").returncode == 0
    result = _git(root, "commit", "-qm", f"add {name}")
    assert result.returncode == 0, result.stderr
    return _git(root, "rev-parse", "HEAD").stdout.strip()


def _point_origin_main_at_head(root: Path) -> None:
    """Give the fixture an ``origin/main`` without a remote, as a fetch would."""
    head = _git(root, "rev-parse", "HEAD").stdout.strip()
    result = _git(root, "update-ref", "refs/remotes/origin/main", head)
    assert result.returncode == 0, result.stderr


class TestTheFenceComputesItsBaselineOrFails:
    """Requirement 2.5: no baseline, no verdict. Never a clean report.

    The positive case is asserted first and against a fixture repository as well
    as this one, so the failures below are known to be about the missing baseline
    rather than a helper that raises whatever it is handed.
    """

    def test_the_real_repository_has_a_computable_merge_base(self) -> None:
        base = merge_base(REPO_ROOT)
        assert len(base) >= 7, base
        # A merge base git will not confirm as a commit would mean the string is
        # being read out of the wrong stream.
        kind = _git(REPO_ROOT, "cat-file", "-t", base)
        assert kind.stdout.strip() == "commit", kind.stdout

    def test_a_fixture_repository_with_a_baseline_reports_one(self, tmp_path: Path) -> None:
        root = _fixture_repo(tmp_path)
        base = _commit(root, "kept.py", "x = 1\n")
        _point_origin_main_at_head(root)
        _commit(root, "added.py", "y = 2\n")
        assert merge_base(root) == base

    def test_a_repository_without_the_default_branch_ref_fails_closed(self, tmp_path: Path) -> None:
        root = _fixture_repo(tmp_path)
        _commit(root, "kept.py", "x = 1\n")
        with pytest.raises(MergeBaseUnavailable) as caught:
            merge_base(root)
        assert DEFAULT_BRANCH_REF in str(caught.value)

    def test_a_directory_that_is_not_a_repository_fails_closed(self, tmp_path: Path) -> None:
        outside = tmp_path / "not-a-repo"
        outside.mkdir()
        assert (
            _git(outside, "rev-parse", "--git-dir").returncode != 0
        ), "precondition: the temporary directory must not sit inside a repository"
        with pytest.raises(MergeBaseUnavailable):
            merge_base(outside)

    def test_a_history_sharing_no_ancestor_fails_closed(self, tmp_path: Path) -> None:
        """What a shallow clone or a grafted branch looks like: the ref resolves,
        and ``merge-base`` still has nothing to say. Git exits 1 with empty
        stdout here, so this drives the exit-status branch; the separate
        empty-answer-despite-exit-0 branch in ``merge_base`` is defensive only —
        no known git emits that shape, and it exists so a future one cannot turn
        a missing baseline into a clean report.
        """
        root = _fixture_repo(tmp_path)
        _commit(root, "kept.py", "x = 1\n")
        _point_origin_main_at_head(root)
        assert _git(root, "checkout", "-q", "--orphan", "unrelated").returncode == 0
        _commit(root, "other.py", "y = 2\n")
        with pytest.raises(MergeBaseUnavailable) as caught:
            merge_base(root)
        assert "no baseline" in str(caught.value)

    def test_the_whole_fence_raises_rather_than_reporting_clean(self, tmp_path: Path) -> None:
        """The fail-closed claim at the level that matters: the fence, not a helper.

        An earlier shape of this gate could have returned ``[]`` here, which is
        the exact byte-for-byte verdict of a clean branch.
        """
        root = _fixture_repo(tmp_path)
        _commit(root, "kept.py", "x = 1\n")
        with pytest.raises(MergeBaseUnavailable):
            fence_report(root)


class TestTheBranchStaysInsideTheDeclaredTerritory:
    """Requirements 2.1, 2.2: the gate itself, plus its non-vacuity controls."""

    def test_the_branch_changed_a_substantial_number_of_files(self) -> None:
        """Non-vacuity: an empty change list would pass the gate below reading clean."""
        paths = branch_changed_paths(REPO_ROOT, merge_base(REPO_ROOT))
        assert len(paths) > 50, f"too few changed paths to prove anything: {paths}"

    def test_the_change_list_reads_committed_working_tree_and_untracked_changes(
        self, tmp_path: Path
    ) -> None:
        """All three sources reach the classifier, proven one file per source.

        Driven through a fixture repository because the real one cannot be made
        to hold a known dirty file. If only the committed range were read, an
        uncommitted trespass would never be seen and the gate would stay green
        until the moment it was committed — which is one push too late.
        """
        root = _fixture_repo(tmp_path)
        _commit(root, "tracked.py", "x = 1\n")
        _point_origin_main_at_head(root)
        base = merge_base(root)
        _commit(root, "committed.py", "y = 2\n")
        (root / "tracked.py").write_text("x = 3\n", encoding="utf-8")
        (root / "untracked.py").write_text("z = 4\n", encoding="utf-8")

        paths = branch_changed_paths(root, base)
        assert "committed.py" in paths, paths
        assert "tracked.py" in paths, paths
        assert "untracked.py" in paths, paths

    def test_an_ignored_file_is_outside_the_change_list(self, tmp_path: Path) -> None:
        """The boundary the docstring claims, asserted rather than asserted-in-prose."""
        root = _fixture_repo(tmp_path)
        _commit(root, ".gitignore", "ignored/\n")
        _point_origin_main_at_head(root)
        base = merge_base(root)
        (root / "ignored").mkdir()
        (root / "ignored" / "scratch.py").write_text("x = 1\n", encoding="utf-8")

        assert branch_changed_paths(root, base) == []

    def test_the_real_branch_changed_the_app_and_the_shared_catalogs(self) -> None:
        """The change list is the real one: named files, not a count alone."""
        paths = set(branch_changed_paths(REPO_ROOT, merge_base(REPO_ROOT)))
        assert "src/kiro_crew/apps/builtins/spec_engine/app.json" in paths
        assert any(path.startswith("website/src/i18n/") for path in paths)

    def test_every_branch_changed_file_is_declared_or_allowlisted(self) -> None:
        violations = fence_report(REPO_ROOT)
        assert violations == [], (
            "this branch changes files outside the Spec_App's declared trees: "
            f"{violations}. Each is either a boundary crossing to undo or a "
            "genuinely shared file to add to BOUNDARY_ALLOWLIST with a written "
            "justification — inferring it is not an option."
        )

    def test_a_declared_root_that_does_not_exist_yet_is_still_declared(self) -> None:
        """A declared root with no files must not narrow the territory.

        ``website/src/apps/spec-engine/`` is empty until the Operator_Surface
        lands. Classification is textual for exactly this reason, and the
        assertion is written so it keeps holding once the directory appears.
        """
        unborn = "website/src/apps/spec-engine/never/created/Panel.tsx"
        assert not (REPO_ROOT / unborn).exists()
        assert admits(unborn) == "declared root website/src/apps/spec-engine/"
        assert boundary_violations([unborn]) == []

    def test_a_future_frontend_test_name_is_declared(self) -> None:
        candidate = "website/src/test/SpecEnginePage.test.tsx"
        assert admits(candidate) == "declared test prefix website/src/test/SpecEngine"


class TestTheAllowlistIsExplicitAndReviewed:
    """Requirement 2.3: an out-of-tree change is admitted by a written claim."""

    def test_every_entry_carries_a_one_line_justification(self) -> None:
        for entry, justification in BOUNDARY_ALLOWLIST:
            assert entry.strip() == entry and entry, f"malformed allowlist path: {entry!r}"
            assert "\n" not in justification, f"{entry}: justification is not one line"
            assert len(justification) >= 30, f"{entry}: justification says too little"

    def test_no_entry_is_duplicated(self) -> None:
        entries = [entry for entry, _ in BOUNDARY_ALLOWLIST]
        assert len(entries) == len(set(entries)), f"duplicate allowlist entries: {entries}"

    def test_every_entry_is_already_repo_relative(self) -> None:
        """Both sides normalized: an entry needing normalization would be a hole.

        A prefix written ``/src/...`` or ``./src/...`` or with a backslash matches
        nothing once a scanned path is normalized, so the fence would report a
        legitimate file — or, worse for a declared root, report the app's own
        files and get the whole gate switched off.
        """
        for prefix in (
            [entry for entry, _ in BOUNDARY_ALLOWLIST]
            + list(DECLARED_ROOTS)
            + list(DECLARED_TEST_PREFIXES)
        ):
            assert prefix == _normalized_prefix(prefix), f"{prefix!r} is not normalized"
            assert ".." not in prefix.split("/"), f"{prefix!r} escapes upward"

    def test_every_directory_entry_ends_in_a_separator(self) -> None:
        """``website/src/i18n`` without the slash would also admit ``i18n-extra/``."""
        for entry, _ in BOUNDARY_ALLOWLIST:
            if entry.endswith("/"):
                continue
            assert not (
                REPO_ROOT / entry
            ).is_dir(), f"{entry} names a directory but not as a prefix; add the trailing /"

    def test_no_entry_has_gone_stale(self) -> None:
        """An entry matching nothing is precedent the next project would read.

        The two scratch roots are exempt by name, with the reason recorded on
        :data:`ALLOWLIST_MAY_MATCH_NOTHING`: they exist only after a harness or
        gate run.
        """
        paths = branch_changed_paths(REPO_ROOT, merge_base(REPO_ROOT))
        matched = {_allowlist_entry_matching(path) for path in paths}
        stale = [
            entry
            for entry, _ in BOUNDARY_ALLOWLIST
            if entry not in ALLOWLIST_MAY_MATCH_NOTHING and entry not in matched
        ]
        assert stale == [], f"allowlist entries matching nothing on this branch: {stale}"


class TestTheAllowlistCannotSwallowAnotherApp:
    """The allowlist is the fence's own soft spot, so it is fenced in turn."""

    def test_no_entry_admits_a_path_inside_another_app(self) -> None:
        """Judge the ENTRIES, not a sample path.

        The first version planted one filename per app (``app.json``) and asked
        whether it was admitted — which an exact-file entry naming any OTHER
        file inside another app's tree slipped straight past. A review attack
        demonstrated it with an entry for a single module inside the Prior_App.
        So the check is now structural: no allowlist entry's PREFIX may lie
        inside, equal, or contain another app's territory, which no choice of
        planted filename can miss. Backend and frontend app directories are
        assembled at runtime; the Prior_App's shared-test namespace is the one
        literal, matched only against entries a human wrote, never against
        scanned paths.
        """
        territories = [
            "/".join(("src", "kiro_crew", "apps", "builtins", app_dir)) + "/"
            for app_dir in _builtin_app_dirs()
            if app_dir != "spec_engine"
        ]
        # The frontend territories are assembled from the real listing, exactly
        # like the backend ones: a review probe showed the first version named
        # only ONE frontend tree, so an entry covering any other app's frontend
        # wholesale passed both guards. Non-directories in website/src/apps/
        # (shared router/registry modules) are not app territory.
        frontend_root = REPO_ROOT / "website" / "src" / "apps"
        territories += [
            f"website/src/apps/{entry.name}/"
            for entry in sorted(frontend_root.iterdir())
            if entry.is_dir() and entry.name != "spec-engine"
        ]
        # The Prior_App's shared-directory test namespace is territory too.
        territories.append("website/src/test/SpecBuilder")
        # A moved-away apps directory fails closed (iterdir raises), but one that
        # SURVIVES with no app subdirectories would yield an empty list and a
        # guard that passes over nothing — a review demonstrated exactly that
        # with a fixture holding only a registry module. The suite's convention:
        # a guard over an assembled list asserts the list is real.
        assert len(territories) > 10, f"territory list implausibly small: {territories}"
        for prefix, _justification in BOUNDARY_ALLOWLIST:
            for territory in territories:
                inside = prefix.startswith(territory)
                covers = territory.startswith(prefix)
                assert not (inside or covers), (
                    f"allowlist entry {prefix!r} reaches into another app's "
                    f"territory {territory!r}"
                )

    def test_no_entry_admits_the_shared_app_directories_wholesale(self) -> None:
        for wide in (
            "src/",
            "src/kiro_crew/",
            "src/kiro_crew/apps/",
            "src/kiro_crew/apps/builtins/",
            "website/src/apps/",
            "website/src/",
        ):
            assert admits(wide + "anything.py") is None, f"{wide} is admitted wholesale"


class TestPlantedViolationsAreReported:
    """Requirements 2.1, 2.2: the detector detects, one planted case per shape.

    Every path is assembled from fragments at runtime. The Prior_App's directory
    name never appears in this file as a literal, so this module cannot be the
    thing that trips its own gate — the convention the provenance suite set.
    """

    PRIOR_BACKEND = "/".join(
        ("src", "kiro_crew", "apps", "builtins", "spec" + "_builder", "engine_ops.py")
    )
    PRIOR_FRONTEND = "/".join(("website", "src", "apps", "spec" + "-builder", "RunPanel.tsx"))
    PRIOR_FRONTEND_TEST = "website/src/test/" + "Spec" + "Builder" + "Panel.test.ts"
    UNDECLARED_ROOT = "/".join(
        ("src", "kiro_crew", "apps", "builtins", "spec" + "_operator", "app.json")
    )
    NEAR_MISS_ROOT = "/".join(
        ("src", "kiro_crew", "apps", "builtins", "spec_engine" + "_extra", "helper.py")
    )
    NEIGHBOUR_FRONTEND = "/".join(("website", "src", "apps", "work" + "flows", "Panel.tsx"))
    PLATFORM_FILE = "/".join(("src", "kiro_crew", "dash" + "board", "server.py"))

    @pytest.mark.parametrize(
        "planted",
        [
            pytest.param(PRIOR_BACKEND, id="prior-app-backend"),
            pytest.param(PRIOR_FRONTEND, id="prior-app-frontend"),
            pytest.param(PRIOR_FRONTEND_TEST, id="prior-app-frontend-test"),
            pytest.param(UNDECLARED_ROOT, id="undeclared-new-root"),
            pytest.param(NEAR_MISS_ROOT, id="declared-root-prefix-near-miss"),
            pytest.param(NEIGHBOUR_FRONTEND, id="neighbouring-app-frontend"),
            pytest.param(PLATFORM_FILE, id="unjustified-platform-file"),
        ],
    )
    def test_a_planted_path_is_reported(self, planted: str) -> None:
        assert admits(planted) is None, f"{planted} was admitted"
        reported = boundary_violations([planted])
        assert len(reported) == 1, reported
        assert planted in reported[0]

    def test_the_report_names_the_owning_app(self) -> None:
        """Requirement 2.2: the file AND whose it is."""
        prior = "spec" + "_builder"
        assert owning_app(self.PRIOR_BACKEND) == f"the {prior} app"
        assert owning_app(self.PRIOR_FRONTEND).endswith("app's frontend")
        assert owning_app(self.PRIOR_FRONTEND_TEST).endswith("app's frontend tests")
        assert owning_app(self.PLATFORM_FILE) == "the core platform"
        assert prior in boundary_violations([self.PRIOR_BACKEND])[0]

    def test_a_planted_violation_survives_the_real_change_list(self) -> None:
        """The gate above is green; adding one planted path must turn it red.

        This is the control that separates "no violations" from "no detection".
        """
        real = branch_changed_paths(REPO_ROOT, merge_base(REPO_ROOT))
        assert boundary_violations(real) == []
        assert len(boundary_violations(real + [self.PRIOR_BACKEND])) == 1


class TestNormalizationCannotSilenceTheFence:
    """The prior project shipped a path check that compared absolute against
    relative and therefore always found zero overlap and always passed. This is
    the proof that this one does not: the same paths in absolute form produce the
    same verdict, in both directions.
    """

    def test_an_absolute_in_bounds_path_is_still_admitted(self) -> None:
        relative = "src/kiro_crew/apps/builtins/spec_engine/app.json"
        absolute = str(REPO_ROOT / relative)
        assert admits(relative) is not None
        assert admits(absolute) == admits(relative)

    def test_an_absolute_out_of_bounds_path_is_still_reported(self) -> None:
        absolute = str(REPO_ROOT / TestPlantedViolationsAreReported.PRIOR_BACKEND)
        reported = boundary_violations([absolute])
        assert len(reported) == 1, reported
        # Reported repo-relative, so an offender reads the same whichever form
        # the path arrived in.
        assert TestPlantedViolationsAreReported.PRIOR_BACKEND in reported[0]

    def test_the_whole_change_list_in_absolute_form_gives_the_same_verdict(self) -> None:
        relative = branch_changed_paths(REPO_ROOT, merge_base(REPO_ROOT))
        absolute = [str(REPO_ROOT / p) for p in relative]
        assert boundary_violations(absolute) == boundary_violations(relative) == []
        # And the absolute form of a planted violation is caught too, so the
        # equality above is not two empty lists meeting.
        planted = str(REPO_ROOT / TestPlantedViolationsAreReported.PRIOR_BACKEND)
        assert len(boundary_violations(absolute + [planted])) == 1

    def test_a_path_outside_the_repository_is_never_admitted(self) -> None:
        assert admits("/etc/hosts") is None
        assert admits("../sibling-checkout/src/kiro_crew/apps/builtins/spec_engine/x.py") is None

    def test_a_path_the_filesystem_cannot_resolve_is_still_classified(self, tmp_path: Path) -> None:
        """Classification is lexical, so an unresolvable path gets a verdict.

        A symlink loop is the case that proved this: resolving one raises, and an
        earlier draft of :func:`repo_relative` took the entire fence down with a
        ``RuntimeError`` because a scratch directory happened to contain one. A
        gate that errors out on an unrelated broken link is a gate someone
        switches off.
        """
        loop = tmp_path / "loop"
        loop.symlink_to(loop)
        assert admits(str(loop / "inside" / "x.py")) is None
        assert len(boundary_violations([str(loop / "inside" / "x.py")])) == 1

    def test_a_symlinked_spelling_of_the_repository_is_not_admitted(self, tmp_path: Path) -> None:
        """The cost of lexical matching, stated as a test rather than as a hope.

        A declared file reached through a symlink to the checkout does not match
        the declared root and is REPORTED. That is the fail-closed direction: the
        fence over-reports a spelling it cannot confirm rather than admitting a
        path it never checked.
        """
        link = tmp_path / "checkout"
        link.symlink_to(REPO_ROOT, target_is_directory=True)
        through_link = str(link / "src/kiro_crew/apps/builtins/spec_engine/app.json")
        assert admits(through_link) is None
        assert len(boundary_violations([through_link])) == 1


#: Enough examples to explore the segment shapes that matter (a bare prefix, a
#: prefix plus one character, a near-miss) without making a pure-string property
#: the slowest test in the suite.
MAX_EXAMPLES = 200

_SEGMENT = st.text(alphabet="abcdefgSE_-.", min_size=0, max_size=6)
_ARBITRARY_PATH = st.lists(_SEGMENT, min_size=1, max_size=5).map("/".join)
_DECLARED_PREFIX = st.sampled_from(
    list(DECLARED_ROOTS) + list(DECLARED_TEST_PREFIXES) + [e for e, _ in BOUNDARY_ALLOWLIST]
)
_NEAR_PREFIX_PATH = st.tuples(_DECLARED_PREFIX, _ARBITRARY_PATH).map(lambda pair: pair[0] + pair[1])
_PATHS = st.one_of(_ARBITRARY_PATH, _NEAR_PREFIX_PATH, _DECLARED_PREFIX)


class TestTheFenceAdmitsExactlyTheDeclaredTerritory:
    """Design Property 3, over paths rather than over the few someone wrote down.

    A partition is the claim that makes the gate meaningful: a path that is
    neither admitted nor reported is a file the fence saw and said nothing about,
    and a path that is both is a report a reader cannot act on.
    """

    @settings(max_examples=MAX_EXAMPLES)
    @given(path=_PATHS)
    def test_every_path_is_admitted_or_reported_and_never_both(self, path: str) -> None:
        admitted = admits(path)
        reported = boundary_violations([path])
        if admitted is None:
            assert len(reported) == 1, f"{path!r}: neither admitted nor reported"
        else:
            assert reported == [], f"{path!r}: admitted as {admitted} yet reported"

    @settings(max_examples=MAX_EXAMPLES)
    @given(path=_PATHS)
    def test_an_admitted_path_always_names_its_reason(self, path: str) -> None:
        """An admission with no stated reason is an admission no one can review."""
        admitted = admits(path)
        if admitted is not None:
            assert admitted.startswith(("declared root ", "declared test prefix ", "allowlist "))

    @settings(max_examples=MAX_EXAMPLES)
    @given(path=_PATHS)
    def test_the_verdict_does_not_depend_on_the_form_the_path_arrived_in(self, path: str) -> None:
        assert admits(path) == admits("./" + path) == admits(path.replace("/", "\\"))
