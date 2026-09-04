"""Import boundary: only ``host.py`` may reach gateway internals.

The Spec Engine is meant to be portable to an external-app SDK repository. The
:mod:`...host` seam exists so that porting rewrites exactly one file: it is the
sole module allowed to import ``kiro_crew.*`` gateway internals, and every other
module in the app reaches those symbols *through* it. This gate is what keeps
that true. It walks every shipped ``.py`` file under the app package -- source
only, excluding ``host.py`` itself and the ``tests`` package -- parses each with
the AST, and FAILS if any of them imports ``kiro_crew`` outside the app's own
package ``kiro_crew.apps.builtins.spec_engine``.

A companion path-level fence (``test_app_boundary_fence.py``) already asserts the
branch changes nothing outside the app's trees; this is the orthogonal import
fence, asserting the app reaches nothing outside the seam.

**Non-emptiness is pinned outside the parametrization.** An empty file list must
FAIL, not vacuously pass -- a gate that walks nothing and reports clean is the
recurring false-pass this project guards against. :func:`test_the_walk_finds_the_app`
asserts the walk found a substantial, named set of files before any per-file
check can be read as meaningful.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import kiro_crew.apps.builtins.spec_engine as spec_engine_pkg

#: The app package root on disk.
APP_ROOT = Path(spec_engine_pkg.__file__).resolve().parent

#: The one module allowed to import gateway internals, and the test tree, which
#: is not shipped app code and legitimately patches gateway modules directly.
EXEMPT_RELATIVE = frozenset({"host.py"})
EXEMPT_TOP_DIRS = frozenset({"tests"})

#: The gateway package. Any import of it is a boundary crossing unless it targets
#: the app's OWN subpackage below.
GATEWAY_ROOT = "kiro_crew"

#: The app's own package. Imports of names under this are in-bounds -- they are
#: the app importing itself, not the gateway.
APP_PACKAGE = "kiro_crew.apps.builtins.spec_engine"


def _shipped_source_files() -> list[Path]:
    """Every shipped app ``.py`` file, excluding ``host.py`` and the tests tree."""
    found: list[Path] = []
    for path in sorted(APP_ROOT.rglob("*.py")):
        relative = path.relative_to(APP_ROOT)
        if relative.parts[0] in EXEMPT_TOP_DIRS:
            continue
        if relative.as_posix() in EXEMPT_RELATIVE:
            continue
        if "__pycache__" in relative.parts:
            continue
        found.append(path)
    return found


def _is_gateway_target(module: str | None) -> bool:
    """True when *module* names a gateway import that is NOT the app's own package.

    ``kiro_crew`` and ``kiro_crew.anything`` are gateway targets; anything under
    :data:`APP_PACKAGE` (including the bare package itself) is the app importing
    itself and is in-bounds.
    """
    if not module:
        return False
    if module == APP_PACKAGE or module.startswith(APP_PACKAGE + "."):
        return False
    return module == GATEWAY_ROOT or module.startswith(GATEWAY_ROOT + ".")


def _gateway_imports(source: str, filename: str) -> list[str]:
    """Every gateway module a file imports, by walking its AST.

    Only absolute imports can reach the gateway: a relative import (``from ..
    host``) has ``level > 0`` and no ``kiro_crew`` prefix, so it is never a
    crossing. ``import kiro_crew.x`` and ``from kiro_crew.x import y`` are both
    inspected.
    """
    tree = ast.parse(source, filename=filename)
    crossings: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _is_gateway_target(alias.name):
                    crossings.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            # A relative import (level > 0) cannot name the gateway absolutely.
            if node.level == 0 and _is_gateway_target(node.module):
                crossings.append(node.module or "")
    return crossings


SHIPPED_SOURCE_FILES = _shipped_source_files()


class TestOnlyTheSeamReachesTheGateway:
    def test_the_walk_finds_the_app(self) -> None:
        """Non-emptiness, pinned outside any parametrization.

        An empty or implausibly small file list would let every per-file check
        below pass on nothing. The app ships far more than this floor; the
        assertion only has to be immune to a walk that silently found nothing.
        """
        assert len(SHIPPED_SOURCE_FILES) > 40, (
            "the boundary walk found too few app source files to be meaningful: "
            f"{[p.name for p in SHIPPED_SOURCE_FILES]}"
        )
        names = {p.name for p in SHIPPED_SOURCE_FILES}
        # Named anchors from each shipped subpackage, so a walk that missed a
        # whole tree is caught rather than merely counted.
        for expected in ("routes.py", "state.py", "server.py", "readiness.py", "tick.py"):
            assert expected in names, f"{expected} missing from the boundary walk"
        assert "host.py" not in names, "host.py must be exempt from the walk"

    @pytest.mark.parametrize(
        "path",
        SHIPPED_SOURCE_FILES,
        ids=[p.relative_to(APP_ROOT).as_posix() for p in SHIPPED_SOURCE_FILES],
    )
    def test_a_shipped_file_imports_no_gateway_internal(self, path: Path) -> None:
        crossings = _gateway_imports(path.read_text(encoding="utf-8"), str(path))
        relative = path.relative_to(APP_ROOT).as_posix()
        assert crossings == [], (
            f"{relative} imports gateway internals directly: {crossings}. "
            "Route these through the host seam (`from ..host import ...`, minding "
            "the relative depth) -- host.py is the only module allowed to import "
            "kiro_crew.* gateway internals."
        )

    def test_the_seam_itself_does_import_the_gateway(self) -> None:
        """The control that proves the detector detects.

        If the walker reported no crossings for host.py it would be blind to
        every crossing, so the one file that legitimately imports the gateway is
        confirmed to be seen as such -- and then confirmed exempt from the walk.
        """
        host = APP_ROOT / "host.py"
        crossings = _gateway_imports(host.read_text(encoding="utf-8"), str(host))
        assert crossings, "host.py should import gateway internals; the walker sees none"
        assert host not in SHIPPED_SOURCE_FILES, "host.py must be exempt from the enforced walk"
