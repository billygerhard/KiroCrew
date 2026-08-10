"""Shared fixtures and helpers for the spec engine suite.

The one helper every module here wants is a spec directory plus a way to prove
nothing wrote into it: the engine's interop guarantee is that a spec directory
holds only the native documents and the sidecar, so asserting on a snapshot of
its contents is how most of these tests express their real claim.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew.apps.builtins.spec_engine.engine.state import SpecRef, StateStore

#: The native document set plus the sidecar. Anything else appearing under a
#: spec directory is engine state that leaked out of the state store.
NATIVE_SPEC_FILES = ("requirements.md", "design.md", "tasks.md", ".config.kiro")


def make_spec_dir(project: Path, name: str) -> Path:
    """Create a spec directory holding only its native files."""
    spec_dir = project / ".kiro" / "specs" / name
    spec_dir.mkdir(parents=True, exist_ok=True)
    (spec_dir / "requirements.md").write_text("# Requirements Document\n", encoding="utf-8")
    (spec_dir / "design.md").write_text("# Design Document\n", encoding="utf-8")
    (spec_dir / "tasks.md").write_text("# Implementation Plan\n", encoding="utf-8")
    (spec_dir / ".config.kiro").write_text(
        json.dumps({"specId": name, "specType": "feature"}), encoding="utf-8"
    )
    return spec_dir


def spec_dir_snapshot(spec_dir: Path) -> dict[str, str]:
    """Map every file under *spec_dir* to its contents, relative paths as keys."""
    return {
        str(path.relative_to(spec_dir)): path.read_text(encoding="utf-8")
        for path in sorted(spec_dir.rglob("*"))
        if path.is_file()
    }


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    """A project tree with one spec directory in it."""
    root = tmp_path / "project"
    root.mkdir()
    make_spec_dir(root, "example")
    return root


@pytest.fixture()
def state_dir(tmp_path: Path) -> Path:
    """A state root outside every project tree."""
    return tmp_path / "state"


@pytest.fixture()
def store(state_dir: Path) -> StateStore:
    return StateStore(root=state_dir)


@pytest.fixture()
def ref(project: Path) -> SpecRef:
    return SpecRef.of(project, "example")
