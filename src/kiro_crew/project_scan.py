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
        """

        return cls(
            root=root,
            candidates=tuple(sorted(candidates, key=lambda candidate: candidate.path)),
            warnings=tuple(warnings),
        )


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

    raise NotImplementedError("the walker lands with the traversal implementation")
