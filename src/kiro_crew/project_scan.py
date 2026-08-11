"""Project scanner — detect the packages in a directory tree, and nothing else.

A chat folder already carries a ``project_dir`` and nests via ``parent_id``, so a
chat opened inside one is steering- and scope-correct for its package. What is
missing for a monorepo, or a directory of sibling repositories, is *population*:
assembling N sub-folders by hand is work nobody does, so per-package steering
never loads. This module supplies the detection half of that — the folder tree is
then built by composing the existing folder API, which stays the only writer.

Two properties define the module and are worth stating up front:

* **Read-only and pure.** Detection depends on the filesystem and the passed
  configuration, and on nothing else: no writes, no state, no editor workspace
  artifacts. That is what makes a scan safe to point at an unfamiliar tree, and
  what makes :class:`CandidateTree` reproducible across runs.
* **Prune beats every signal.** A name in :data:`PRUNE_DIRS` is rejected before
  it is ever classified, so a vendored ``node_modules/left-pad/package.json``
  cannot become a candidate no matter how well it matches. The precedence is
  one-directional on purpose — the alternative (classify, then filter) leaks a
  candidate the moment a new signal is added without a matching filter.

Classification is two-tiered rather than boolean because the confidence differs:
a repository or manifest at the top of the scan root is what the user pointed at,
while a directory *inside* a package may be a real sub-package or may be an
implementation detail. So the tree offers both and lets the preview decide what
is ticked by default — see :class:`Tier`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Iterable, Sequence

# Directory names that mark a package boundary by themselves.
GIT_DIR = ".git"
KIRO_DIR = ".kiro"

# Filenames recognized as a package manifest. Extended per call by the
# ``extra_signals`` argument rather than by editing this tuple, so an ecosystem
# this list has never heard of is a config change and not a code change.
MANIFESTS: tuple[str, ...] = (
    "package.json",
    "pyproject.toml",
    "Cargo.toml",
    "go.mod",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
)

# Dependency, build-output, and virtual-environment directories: never traversed
# and never classified. Dot-directories are pruned by the rule in
# :func:`is_pruned` instead of being enumerated here, because the interesting set
# is open-ended (every tool adds one) while the exception is a single name.
PRUNE_DIRS: tuple[str, ...] = (
    "node_modules",
    "dist",
    "build",
    "target",
    ".venv",
    "venv",
    "__pycache__",
)

# Depth below the scan root that traversal will not pass. A cap is what keeps a
# scan bounded on a tree whose shape is unknown; 5 covers the layouts this
# feature exists for (``root/packages/<pkg>/...``) without inviting a walk of a
# whole home directory.
DEFAULT_DEPTH_CAP = 5

# Signal names recorded on a candidate. They are part of the preview payload —
# the UI explains *why* a directory is offered — so they are constants rather
# than literals sprinkled through the walker.
SIGNAL_GIT = "git"
SIGNAL_KIRO = KIRO_DIR
SIGNAL_MEMBER = "member"
_MANIFEST_SIGNAL_PREFIX = "manifest:"


def manifest_signal(filename: str) -> str:
    """Return the signal name recording that ``filename`` was found."""

    return f"{_MANIFEST_SIGNAL_PREFIX}{filename}"


def recognized_manifests(extra_signals: Sequence[str] = ()) -> frozenset[str]:
    """Return the manifest filenames in effect for one scan.

    Configured extra signals are additive: they widen the built-in set and can
    never shrink it, so a bad config value costs a false positive rather than
    silently disabling detection of a whole ecosystem. Blank entries are dropped
    because an empty name would match no file but would still read as
    configured.
    """

    extras = (name.strip() for name in extra_signals)
    return frozenset(MANIFESTS) | frozenset(name for name in extras if name)


def is_pruned(name: str) -> bool:
    """Return whether a directory named ``name`` must be pruned.

    Covers :data:`PRUNE_DIRS` plus any dot-directory other than ``.kiro``, which
    is the one hidden directory carrying a detection signal. Callers must consult
    this *before* classifying, never after: prune precedence is only real if a
    pruned name never reaches the classifier.
    """

    if name in PRUNE_DIRS:
        return True
    return name.startswith(".") and name != KIRO_DIR


class Tier(Enum):
    """How confident detection is that a directory should become a folder.

    ``AUTO`` is ticked by default in the preview, ``OFFERED`` is shown unticked.
    A directory with no signal at all is absent from the tree entirely — that is
    the third outcome, and it is represented by omission rather than by a member
    here, so an ignored directory cannot be rendered by a surface that forgot to
    filter it out.
    """

    AUTO = "auto"
    OFFERED = "offered"


@dataclass(frozen=True)
class Candidate:
    """One detected package.

    Frozen because the tree is handed to preview, reconcile, and scaffold in
    turn: a later stage overlays its own state (``existing``, ``selected``) in
    its own payload instead of mutating detection output, which keeps the scan
    result comparable across runs.
    """

    # Absolute path, inside the scan root. Absoluteness is enforced below; being
    # inside the root is the walker's invariant, since only it knows the root.
    path: str
    # Display name — the directory's basename.
    name: str
    # Nearest ancestor that is itself a candidate; ``None`` means the candidate
    # hangs directly off the scan root.
    parent_path: str | None
    tier: Tier
    # Why this directory was detected, e.g. ``("git", "manifest:package.json")``.
    signals: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        # A relative path here would reach the folder API as a ``project_dir``
        # that resolves against whatever the process CWD happens to be, so it is
        # rejected at construction rather than several layers downstream.
        if not os.path.isabs(self.path):
            raise ValueError(f"candidate path must be absolute: {self.path!r}")
        if self.parent_path is not None and not os.path.isabs(self.parent_path):
            raise ValueError(f"candidate parent_path must be absolute: {self.parent_path!r}")


@dataclass(frozen=True)
class CandidateTree:
    """Everything one scan found, in an order that does not depend on the walk.

    Build via :meth:`build` rather than the constructor: ``candidates`` is
    documented as path-sorted, and directory iteration order is not guaranteed by
    the OS, so sorting is what makes two scans of an unchanged tree compare
    equal.
    """

    root: str
    candidates: tuple[Candidate, ...] = ()
    # Declarations skipped and subtrees that could not be read. A warning is how
    # this module reports a partial result: neither case aborts a scan, because
    # one unreadable directory should not cost the user the other twenty
    # packages.
    warnings: tuple[str, ...] = ()

    @classmethod
    def build(
        cls,
        root: str,
        candidates: Iterable[Candidate],
        warnings: Iterable[str] = (),
    ) -> CandidateTree:
        """Return a tree with ``candidates`` normalized into path-sorted order.

        Warnings keep the order they were produced in: they are emitted by the
        walk itself, which is already deterministic, and their sequence carries
        the reading order a user follows.

        Raises:
            ValueError: if a candidate path is not inside ``root``.
        """

        ordered = tuple(sorted(candidates, key=lambda candidate: candidate.path))
        for candidate in ordered:
            # Containment is the invariant every later stage relies on: a
            # candidate path becomes a folder's ``project_dir``, so one that
            # escaped the root would scope a chat outside what the user pointed
            # at. The walker cannot produce one (it only joins child names onto
            # the root), but declaration parsing resolves paths a file supplied,
            # so the guard sits at the single point every tree is built through.
            if not _is_within(candidate.path, root):
                raise ValueError(f"candidate path {candidate.path!r} is outside scan root {root!r}")
        return cls(root=root, candidates=ordered, warnings=tuple(warnings))


def _is_within(path: str, root: str) -> bool:
    """Return whether ``path`` names a strict descendant of ``root``.

    A prefix comparison, not a ``commonpath`` call: both arguments are already
    normalized absolute paths, and ``commonpath`` raises on paths from different
    Windows drives — which is a legitimate "outside the root" answer, not an
    error. The separator is re-appended so ``/srv/app2`` does not read as being
    inside ``/srv/app``.
    """

    prefix = root.rstrip(os.sep) + os.sep
    return path.startswith(prefix) and path != prefix


@dataclass(frozen=True)
class _DirContents:
    """One directory's listing, reduced to the two things the walk needs."""

    # Sub-directories worth descending into: real directories only (never a
    # symlink), never a pruned name, and never ``.kiro`` — see :func:`_read_dir`.
    subdirs: tuple[str, ...]
    # Detection signals the directory carries itself, in a fixed order so two
    # scans of an unchanged directory produce an equal candidate.
    signals: tuple[str, ...]


@dataclass(frozen=True)
class _Frame:
    """A directory queued for classification, with the context of its ancestors."""

    path: str
    # Distance below the scan root; the root itself is 0 and is never a candidate
    # (the scaffold step creates the root's folder from the scan root directly).
    depth: int
    # Nearest ancestor that is itself a candidate; ``None`` means the scan root.
    parent_path: str | None
    # Whether any ancestor was detected as a package. This is what splits the
    # boundary rule from the nested rule: the same manifest means "this is the
    # package the user pointed at" outside a package and "this may be an
    # implementation detail of the package above" inside one.
    inside_package: bool


def _read_dir(directory: str, manifests: frozenset[str]) -> _DirContents:
    """Read ``directory`` once, returning what to descend into and what it signals.

    Raises:
        OSError: if the directory cannot be listed. The caller turns that into a
            warning and keeps scanning; one unreadable directory must not cost
            the user the packages found elsewhere.
    """

    subdirs: list[str] = []
    has_git = False
    has_kiro = False
    found_manifests: list[str] = []
    with os.scandir(directory) as entries:
        for entry in entries:
            name = entry.name
            # ``follow_symlinks=False`` is the whole of the no-symlink rule: a
            # symlinked directory fails this test, so it never enters ``subdirs``
            # and is never walked. Its target is therefore never read, whether it
            # points inside the root, outside it, or back at an ancestor.
            if entry.is_dir(follow_symlinks=False):
                if name == GIT_DIR:
                    has_git = True
                    continue
                if name == KIRO_DIR:
                    # A signal, not a container: Kiro's own directory holds
                    # steering and specs, so descending into it could only
                    # manufacture candidates out of the tool's own files.
                    has_kiro = True
                    continue
                # Prune before classify: a pruned name never reaches the
                # classifier because it never reaches the stack, so a vendored
                # manifest under a dependency directory cannot become a
                # candidate however well it matches.
                if is_pruned(name):
                    continue
                subdirs.append(name)
            elif name in manifests:
                # Anything that is not a real directory and carries a manifest
                # name counts, including a symlinked manifest: only the name is
                # read, never the target, so containment is unaffected.
                found_manifests.append(name)

    signals: list[str] = []
    if has_git:
        signals.append(SIGNAL_GIT)
    if has_kiro:
        signals.append(SIGNAL_KIRO)
    signals.extend(manifest_signal(name) for name in sorted(found_manifests))
    # Sorted rather than in ``scandir`` order: directory iteration order is not
    # guaranteed, and the traversal order fixes the order warnings are reported
    # in (candidate order comes from :meth:`CandidateTree.build`).
    return _DirContents(subdirs=tuple(sorted(subdirs)), signals=tuple(signals))


def _tier_for(signals: Sequence[str], *, inside_package: bool) -> Tier:
    """Return the tier for a directory carrying ``signals``.

    ``.git`` and ``.kiro`` are unambiguous at any depth — a nested repository is
    its own package, and a directory the user has already used with Kiro is one
    they have already treated as a project root. A manifest is the ambiguous
    case, and position decides it: outside any package it names the package
    itself, inside one it may just as easily name a build fixture, so it is
    offered unticked.
    """

    if SIGNAL_GIT in signals or SIGNAL_KIRO in signals:
        return Tier.AUTO
    return Tier.OFFERED if inside_package else Tier.AUTO


def scan(
    root: Path,
    *,
    extra_signals: Sequence[str] = (),
    depth_cap: int = DEFAULT_DEPTH_CAP,
) -> CandidateTree:
    """Walk ``root`` read-only and return the packages found beneath it.

    Args:
        root: Absolute directory to scan. Callers validate it with the folder
            API's own path validation first, so the scan refuses exactly what
            manual folder creation refuses.
        extra_signals: Additional manifest filenames to recognize, on top of
            :data:`MANIFESTS`.
        depth_cap: Maximum depth below ``root`` to descend.

    Returns:
        A :class:`CandidateTree`; empty candidates is a valid answer, not an
        error.
    """

    # Not ``resolve()``: the recorded paths stay the ones the caller named, so a
    # root reached through a symlinked parent yields folders whose ``project_dir``
    # matches what the user typed. Containment holds regardless, because every
    # deeper path is this string with child names joined onto it.
    root_path = os.path.abspath(os.fspath(root))
    manifests = recognized_manifests(extra_signals)

    candidates: list[Candidate] = []
    warnings: list[str] = []
    stack = [_Frame(path=root_path, depth=0, parent_path=None, inside_package=False)]

    while stack:
        frame = stack.pop()
        try:
            contents = _read_dir(frame.path, manifests)
        except OSError as exc:
            # A subtree we cannot list is a partial result, not a failed scan.
            warnings.append(f"skipped unreadable directory {frame.path}: {exc.strerror or exc}")
            continue

        parent_path = frame.parent_path
        # The root's own signals count even though the root is never a candidate:
        # pointing at a monorepo whose top level holds a manifest means every
        # package below it is nested inside that package, which is the case
        # Requirement 2's "shown unticked" tier exists for. Requirement 1's
        # boundary tier is for the other shape — a root that is just a directory
        # holding unrelated repositories.
        inside_package = frame.inside_package or bool(contents.signals)
        if frame.depth > 0 and contents.signals:
            candidates.append(
                Candidate(
                    path=frame.path,
                    name=os.path.basename(frame.path),
                    parent_path=frame.parent_path,
                    tier=_tier_for(contents.signals, inside_package=frame.inside_package),
                    signals=contents.signals,
                )
            )
            # Children hang off this candidate, not off whatever is above it.
            parent_path = frame.path

        # A directory at exactly the cap is still classified — it is within the
        # depth the caller allowed — but its children are one step too deep.
        if frame.depth >= depth_cap:
            continue

        # Reversed, because a stack pops last-in first: pushing in reverse walks
        # children alphabetically. Candidate order does not depend on this, but
        # warning order does, and a scan that reports its problems in a different
        # sequence each run is not reproducible.
        for name in reversed(contents.subdirs):
            stack.append(
                _Frame(
                    path=os.path.join(frame.path, name),
                    depth=frame.depth + 1,
                    parent_path=parent_path,
                    inside_package=inside_package,
                )
            )

    return CandidateTree.build(root_path, candidates, warnings)
