"""Tests for the project scanner's traversal and classification.

The walker is the part of the feature a user cannot inspect before trusting it:
it is pointed at an unfamiliar tree and its answer decides which folders get
created. So the tests here pin the four things that make that trustworthy —

1. **Bounded and read-only.** Symlinks are never followed, dependency and
   build directories are never entered, the depth cap holds, and the tree on
   disk is byte-for-byte unchanged afterwards.
2. **Prune beats every signal.** A manifest under a pruned directory, or on a
   pruned directory itself, yields no candidate.
3. **Tier rules.** A repository or top-level manifest is auto-selected; a
   manifest nested inside a package is only offered; a directory the user has
   already used with Kiro is auto-selected wherever it sits.
4. **Determinism.** Two scans of an unchanged tree return equal trees, and a
   directory that cannot be read costs a warning rather than the whole scan.

Fixtures are built as literal directory layouts because the bugs this code can
have are layout-shaped; a mocked filesystem would hide exactly the cases
(symlink, permission, iteration order) worth testing.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from kiro_crew.project_scan import (
    Candidate,
    CandidateTree,
    Tier,
    manifest_signal,
    scan,
)

_IS_WINDOWS = sys.platform == "win32"
# A root user reads a mode-000 directory regardless, so the unreadable-subtree
# case cannot be staged there.
_IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0


def _make(root: Path, *layout: str) -> None:
    """Create the given layout under ``root``.

    An entry ending in ``/`` is a directory; anything else is an empty file with
    its parent directories created for it. Keeps a fixture readable as the tree
    it represents.
    """

    for entry in layout:
        target = root / entry
        if entry.endswith("/"):
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("", encoding="utf-8")


def _by_path(tree: CandidateTree) -> dict[str, Candidate]:
    return {candidate.path: candidate for candidate in tree.candidates}


def _relative_paths(tree: CandidateTree, root: Path) -> list[str]:
    """Candidate paths as POSIX-style paths relative to ``root``, in tree order."""

    return [Path(candidate.path).relative_to(root).as_posix() for candidate in tree.candidates]


def _snapshot(root: Path) -> dict[str, tuple[int, int]]:
    """Size and modification time of every entry under ``root``."""

    snapshot: dict[str, tuple[int, int]] = {}
    for current, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(dirnames) + sorted(filenames):
            path = os.path.join(current, name)
            info = os.stat(path, follow_symlinks=False)
            snapshot[path] = (info.st_size, info.st_mtime_ns)
    return snapshot


class TestPackageBoundaries:
    def test_sibling_repositories_are_auto_selected(self, tmp_path: Path) -> None:
        _make(tmp_path, "api/.git/", "web/.git/", "notes/")

        tree = scan(tmp_path)

        assert _relative_paths(tree, tmp_path) == ["api", "web"]
        assert {candidate.tier for candidate in tree.candidates} == {Tier.AUTO}

    def test_top_level_manifest_is_a_boundary(self, tmp_path: Path) -> None:
        _make(tmp_path, "service/pyproject.toml", "docs/")

        tree = scan(tmp_path)

        candidate = _by_path(tree)[str(tmp_path / "service")]
        assert candidate.tier is Tier.AUTO
        assert candidate.signals == (manifest_signal("pyproject.toml"),)

    def test_candidate_records_its_basename_and_absolute_path(self, tmp_path: Path) -> None:
        _make(tmp_path, "api/.git/")

        candidate = tree_single(scan(tmp_path))

        assert candidate.name == "api"
        assert candidate.path == str(tmp_path / "api")
        assert os.path.isabs(candidate.path)

    def test_signal_free_directory_is_omitted_but_still_traversed(self, tmp_path: Path) -> None:
        # "packages" carries nothing, so it is not offered — but the scan has to
        # walk through it or the packages a monorepo keeps there are all missed.
        _make(tmp_path, "packages/app/package.json", "packages/lib/package.json")

        tree = scan(tmp_path)

        assert _relative_paths(tree, tmp_path) == ["packages/app", "packages/lib"]

    def test_a_manifest_below_an_unmarked_directory_is_still_a_boundary(
        self, tmp_path: Path
    ) -> None:
        # Nothing above it was detected, so it is the package the user pointed
        # at, not a detail nested inside one — depth alone does not demote it.
        _make(tmp_path, "packages/app/package.json")

        assert tree_single(scan(tmp_path)).tier is Tier.AUTO

    def test_extra_signals_are_recognized_without_code_changes(self, tmp_path: Path) -> None:
        _make(tmp_path, "tool/BUILD.bazel")

        assert scan(tmp_path).candidates == ()

        tree = scan(tmp_path, extra_signals=["BUILD.bazel"])

        candidate = tree_single(tree)
        assert candidate.tier is Tier.AUTO
        assert candidate.signals == (manifest_signal("BUILD.bazel"),)

    def test_multiple_signals_are_all_recorded_in_a_fixed_order(self, tmp_path: Path) -> None:
        _make(tmp_path, "app/.git/", "app/.kiro/", "app/package.json", "app/pyproject.toml")

        candidate = tree_single(scan(tmp_path))

        assert candidate.signals == (
            "git",
            ".kiro",
            manifest_signal("package.json"),
            manifest_signal("pyproject.toml"),
        )


class TestNestedDetection:
    def test_nested_manifest_inside_a_package_is_offered(self, tmp_path: Path) -> None:
        _make(tmp_path, "repo/.git/", "repo/package.json", "repo/packages/ui/package.json")

        tiers = {path: candidate.tier for path, candidate in _by_path(scan(tmp_path)).items()}

        assert tiers == {
            str(tmp_path / "repo"): Tier.AUTO,
            str(tmp_path / "repo" / "packages" / "ui"): Tier.OFFERED,
        }

    def test_nested_kiro_directory_is_auto_selected(self, tmp_path: Path) -> None:
        # A directory already used with Kiro is one the user has themselves
        # treated as a project root, so it is ticked even when nested.
        _make(tmp_path, "repo/.git/", "repo/crates/engine/Cargo.toml", "repo/crates/engine/.kiro/")

        candidate = _by_path(scan(tmp_path))[str(tmp_path / "repo" / "crates" / "engine")]

        assert candidate.tier is Tier.AUTO
        assert ".kiro" in candidate.signals

    def test_nested_repository_is_auto_selected(self, tmp_path: Path) -> None:
        _make(tmp_path, "repo/.git/", "repo/vendor/forked/.git/")

        candidate = _by_path(scan(tmp_path))[str(tmp_path / "repo" / "vendor" / "forked")]

        assert candidate.tier is Tier.AUTO

    def test_nesting_is_recorded_against_the_nearest_candidate_ancestor(
        self, tmp_path: Path
    ) -> None:
        _make(
            tmp_path,
            "repo/.git/",
            "repo/packages/ui/package.json",
            "repo/packages/ui/tools/gen/package.json",
        )

        parents = {candidate.name: candidate.parent_path for candidate in scan(tmp_path).candidates}

        # "packages" and "tools" carry no signal, so they are not anyone's parent:
        # the folder tree mirrors detected packages, not the directory tree.
        assert parents == {
            "repo": None,
            "ui": str(tmp_path / "repo"),
            "gen": str(tmp_path / "repo" / "packages" / "ui"),
        }

    def test_a_deeper_manifest_under_an_offered_package_is_also_offered(
        self, tmp_path: Path
    ) -> None:
        _make(
            tmp_path, "repo/.git/", "repo/packages/ui/package.json", "repo/packages/ui/e2e/go.mod"
        )

        candidate = _by_path(scan(tmp_path))[str(tmp_path / "repo" / "packages" / "ui" / "e2e")]

        assert candidate.tier is Tier.OFFERED

    def test_a_monorepo_root_makes_its_packages_nested(self, tmp_path: Path) -> None:
        # The scan root is never a candidate itself (the scaffold step creates its
        # folder from the root path), but its signals still decide the shape: a
        # root holding a manifest is a package, so what is below it is nested in
        # it and offered unticked rather than ticked by default.
        _make(tmp_path, "package.json", "packages/app/package.json", "packages/lib/package.json")

        tree = scan(tmp_path)

        assert _relative_paths(tree, tmp_path) == ["packages/app", "packages/lib"]
        assert {candidate.tier for candidate in tree.candidates} == {Tier.OFFERED}

    def test_a_repository_inside_a_monorepo_root_is_still_auto_selected(
        self, tmp_path: Path
    ) -> None:
        # ``.git`` and ``.kiro`` are unambiguous wherever they sit, so nesting
        # demotes a bare manifest and nothing else.
        _make(tmp_path, "Cargo.toml", "vendor/forked/.git/", "vendor/patched/Cargo.toml")

        tiers = {candidate.name: candidate.tier for candidate in scan(tmp_path).candidates}

        assert tiers == {"forked": Tier.AUTO, "patched": Tier.OFFERED}

    def test_a_directory_used_with_kiro_makes_its_packages_nested(self, tmp_path: Path) -> None:
        # A ``.kiro`` directory is the user having already treated that directory
        # as a project root, so a manifest below it is nested in a package.
        _make(tmp_path, "project/.kiro/", "project/service/pyproject.toml")

        candidate = _by_path(scan(tmp_path))[str(tmp_path / "project" / "service")]

        assert candidate.tier is Tier.OFFERED


class TestPruning:
    def test_dependency_directory_contents_never_become_candidates(self, tmp_path: Path) -> None:
        _make(
            tmp_path,
            "app/package.json",
            "app/node_modules/left-pad/package.json",
            "app/node_modules/.package-lock.json",
        )

        assert _relative_paths(scan(tmp_path), tmp_path) == ["app"]

    @pytest.mark.parametrize(
        "pruned",
        [
            "node_modules",
            "dist",
            "build",
            "target",
            "env",
            "venv",
            ".venv",
            "__pycache__",
            "DerivedData",
        ],
    )
    def test_a_pruned_directory_carrying_a_manifest_is_still_pruned(
        self, tmp_path: Path, pruned: str
    ) -> None:
        # Prune wins over detection: classifying first and filtering afterwards
        # leaks a candidate the moment a new signal is added without a matching
        # filter, so a pruned name never reaches the classifier at all.
        _make(tmp_path, f"app/{pruned}/package.json", f"app/{pruned}/.kiro/", "app/.git/")

        assert _relative_paths(scan(tmp_path), tmp_path) == ["app"]

    def test_hidden_directories_are_pruned(self, tmp_path: Path) -> None:
        _make(tmp_path, ".cache/pkg/package.json", ".idea/workspace/package.json", "app/.git/")

        assert _relative_paths(scan(tmp_path), tmp_path) == ["app"]

    def test_the_kiro_directory_is_a_signal_and_not_a_container(self, tmp_path: Path) -> None:
        # ``.kiro`` holds steering and specs; descending into it could only
        # manufacture candidates out of the tool's own files.
        _make(tmp_path, "app/.kiro/specs/demo/package.json")

        assert _relative_paths(scan(tmp_path), tmp_path) == ["app"]

    def test_the_git_directory_is_a_signal_and_not_a_container(self, tmp_path: Path) -> None:
        _make(tmp_path, "app/.git/modules/sub/package.json")

        assert _relative_paths(scan(tmp_path), tmp_path) == ["app"]


class TestGitignorePruning:
    """The project's own ``.gitignore`` prunes with the same precedence as names.

    The shape that motivated this is Xcode/SwiftPM: ``tmp/derived_data/
    SourcePackages/checkouts/`` holds every dependency as a full git clone, so
    each carries the strongest (repository) signal while being exactly what the
    project's ``.gitignore`` already disowns.
    """

    def test_a_gitignored_dependency_store_of_git_clones_is_pruned(
        self, tmp_path: Path
    ) -> None:
        _make(
            tmp_path,
            "tmp/derived_data/SourcePackages/checkouts/Alamofire/.git/",
            "tmp/derived_data/SourcePackages/checkouts/BigInt/.git/",
            "Sources/App/.git/",
        )
        (tmp_path / ".gitignore").write_text("tmp/\n", encoding="utf-8")

        assert _relative_paths(scan(tmp_path), tmp_path) == ["Sources/App"]

    def test_without_the_gitignore_the_same_clones_are_offered(self, tmp_path: Path) -> None:
        # The control that keeps the test above honest: the clones DO carry the
        # strongest signal, and only the .gitignore removes them.
        _make(
            tmp_path,
            "tmp/derived_data/SourcePackages/checkouts/Alamofire/.git/",
            "Sources/App/.git/",
        )

        assert "tmp/derived_data/SourcePackages/checkouts/Alamofire" in _relative_paths(
            scan(tmp_path), tmp_path
        )

    def test_negation_re_includes_a_sibling(self, tmp_path: Path) -> None:
        _make(tmp_path, "vendor/keep/.git/", "vendor/other/.git/")
        (tmp_path / ".gitignore").write_text("vendor/*\n!vendor/keep\n", encoding="utf-8")

        assert _relative_paths(scan(tmp_path), tmp_path) == ["vendor/keep"]

    def test_a_nested_gitignore_prunes_only_its_own_subtree(self, tmp_path: Path) -> None:
        _make(tmp_path, "a/sub/.git/", "b/sub/.git/")
        (tmp_path / "a" / ".gitignore").write_text("sub/\n", encoding="utf-8")

        assert _relative_paths(scan(tmp_path), tmp_path) == ["b/sub"]

    def test_a_gitignore_above_the_scan_root_has_no_effect(self, tmp_path: Path) -> None:
        # The scanner never reads outside the tree the user pointed at, so a
        # parent directory's exclusions are invisible by construction.
        _make(tmp_path, "repo/pkg/.git/")
        (tmp_path / ".gitignore").write_text("pkg/\n", encoding="utf-8")

        assert _relative_paths(scan(tmp_path / "repo"), tmp_path / "repo") == ["pkg"]

    def test_an_ignored_manifest_is_no_signal_and_no_declaration(self, tmp_path: Path) -> None:
        # `generated/` holds a package.json that both signals a package and
        # declares members; ignoring the FILE (not the directory) must silence
        # both roles while the directory itself stays walkable.
        _make(tmp_path, "generated/lib/.git/", "app/.git/")
        (tmp_path / "generated" / "package.json").write_text(
            '{"workspaces": ["member"]}', encoding="utf-8"
        )
        (tmp_path / "generated" / "member").mkdir()
        (tmp_path / ".gitignore").write_text("generated/package.json\n", encoding="utf-8")

        paths = _relative_paths(scan(tmp_path), tmp_path)
        assert "generated" not in paths  # no manifest signal
        assert "generated/member" not in paths  # no declaration read
        assert set(paths) == {"app", "generated/lib"}  # the subtree still walks

    def test_a_declared_member_inside_an_ignored_directory_is_dropped(
        self, tmp_path: Path
    ) -> None:
        # Members arrive by name rather than by walking, so they need the same
        # judgement: a root declaration naming a directory the project has
        # disowned must not resurrect it.
        _make(tmp_path, "app/.git/", "out/pkg/")
        (tmp_path / "package.json").write_text(
            '{"workspaces": ["out/pkg"]}', encoding="utf-8"
        )
        (tmp_path / ".gitignore").write_text("out/\n", encoding="utf-8")

        assert "out/pkg" not in _relative_paths(scan(tmp_path), tmp_path)

    def test_an_unreadable_gitignore_costs_the_layer_not_the_scan(
        self, tmp_path: Path
    ) -> None:
        # Same recovery rule as declarations: the file is refused with a warning
        # and the scan continues as if it were absent (oversized = unreadable).
        _make(tmp_path, "tmp/pkg/.git/", "app/.git/")
        (tmp_path / ".gitignore").write_text("tmp/\n" + "#" * (512 * 1024), encoding="utf-8")

        tree = scan(tmp_path)
        assert set(_relative_paths(tree, tmp_path)) == {"app", "tmp/pkg"}
        assert any(".gitignore" in warning for warning in tree.warnings)


class TestSymlinks:
    @pytest.mark.skipif(_IS_WINDOWS, reason="directory symlinks need elevation on Windows")
    def test_symlinked_directory_is_not_traversed(self, tmp_path: Path) -> None:
        outside = tmp_path / "outside"
        _make(outside, "secret/package.json")
        root = tmp_path / "root"
        _make(root, "app/.git/")
        (root / "link").symlink_to(outside, target_is_directory=True)

        tree = scan(root)

        assert _relative_paths(tree, root) == ["app"]

    @pytest.mark.skipif(_IS_WINDOWS, reason="directory symlinks need elevation on Windows")
    def test_symlink_loop_back_to_the_root_terminates(self, tmp_path: Path) -> None:
        _make(tmp_path, "app/.git/")
        (tmp_path / "app" / "self").symlink_to(tmp_path, target_is_directory=True)

        assert _relative_paths(scan(tmp_path), tmp_path) == ["app"]

    @pytest.mark.skipif(_IS_WINDOWS, reason="file symlinks need elevation on Windows")
    def test_symlinked_manifest_still_signals(self, tmp_path: Path) -> None:
        # Only the filename is read, never the target, so honouring a symlinked
        # manifest cannot reach outside the root.
        _make(tmp_path, "shared/package.json", "app/src/")
        (tmp_path / "app" / "package.json").symlink_to(tmp_path / "shared" / "package.json")

        assert _by_path(scan(tmp_path))[str(tmp_path / "app")].signals == (
            manifest_signal("package.json"),
        )


class TestDepthCap:
    def test_directories_beyond_the_cap_are_not_reached(self, tmp_path: Path) -> None:
        _make(tmp_path, "a/b/c/d/package.json")

        assert scan(tmp_path, depth_cap=3).candidates == ()

    def test_a_directory_at_exactly_the_cap_is_classified(self, tmp_path: Path) -> None:
        _make(tmp_path, "a/b/c/package.json")

        assert _relative_paths(scan(tmp_path, depth_cap=3), tmp_path) == ["a/b/c"]

    def test_default_cap_reaches_five_levels_down(self, tmp_path: Path) -> None:
        _make(tmp_path, "a/b/c/d/e/package.json", "a/b/c/d/e/f/package.json")

        assert _relative_paths(scan(tmp_path), tmp_path) == ["a/b/c/d/e"]

    def test_a_zero_cap_yields_nothing(self, tmp_path: Path) -> None:
        _make(tmp_path, "app/.git/")

        assert scan(tmp_path, depth_cap=0).candidates == ()

    def test_the_cap_bounds_depth_not_candidate_count(self, tmp_path: Path) -> None:
        _make(tmp_path, "a/.git/", "b/.git/", "c/.git/")

        assert _relative_paths(scan(tmp_path, depth_cap=1), tmp_path) == ["a", "b", "c"]


class TestDeterminismAndSafety:
    def test_two_scans_of_an_unchanged_tree_are_equal(self, tmp_path: Path) -> None:
        _make(
            tmp_path,
            "zeta/.git/",
            "alpha/package.json",
            "alpha/packages/one/package.json",
            "alpha/packages/two/pyproject.toml",
            "middle/crates/engine/Cargo.toml",
            "middle/node_modules/dep/package.json",
        )

        first = scan(tmp_path)
        second = scan(tmp_path)

        assert first == second
        assert [candidate.path for candidate in first.candidates] == sorted(
            candidate.path for candidate in first.candidates
        )

    def test_scanning_writes_nothing(self, tmp_path: Path) -> None:
        _make(
            tmp_path,
            "app/.git/",
            "app/package.json",
            "app/packages/ui/package.json",
            "app/node_modules/dep/package.json",
        )
        before = _snapshot(tmp_path)

        scan(tmp_path)

        assert _snapshot(tmp_path) == before

    def test_empty_root_is_an_empty_answer_not_an_error(self, tmp_path: Path) -> None:
        tree = scan(tmp_path)

        assert tree == CandidateTree(root=str(tmp_path))

    def test_missing_root_is_reported_as_a_warning(self, tmp_path: Path) -> None:
        tree = scan(tmp_path / "absent")

        assert tree.candidates == ()
        assert len(tree.warnings) == 1
        assert "absent" in tree.warnings[0]

    def test_relative_root_is_recorded_as_an_absolute_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _make(tmp_path, "app/.git/")
        monkeypatch.chdir(tmp_path)

        tree = scan(Path("."))

        assert tree.root == str(tmp_path)
        assert tree_single(tree).path == str(tmp_path / "app")

    @pytest.mark.skipif(_IS_WINDOWS, reason="chmod does not remove directory read access")
    @pytest.mark.skipif(_IS_ROOT, reason="root reads an unreadable directory anyway")
    def test_unreadable_subtree_costs_a_warning_not_the_scan(self, tmp_path: Path) -> None:
        _make(tmp_path, "readable/.git/", "locked/inner/")
        locked = tmp_path / "locked"
        os.chmod(locked, 0o000)
        try:
            tree = scan(tmp_path)
        finally:
            os.chmod(locked, 0o755)

        assert _relative_paths(tree, tmp_path) == ["readable"]
        assert len(tree.warnings) == 1
        assert str(locked) in tree.warnings[0]


def tree_single(tree: CandidateTree) -> Candidate:
    """Return the tree's only candidate, asserting there is exactly one."""

    assert len(tree.candidates) == 1, [candidate.path for candidate in tree.candidates]
    return tree.candidates[0]
