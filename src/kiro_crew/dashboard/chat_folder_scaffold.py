"""Folder scaffolding from a project tree — preview, then create.

Two endpoints sit on top of :mod:`kiro_crew.project_scan`:

* ``POST /api/chat/folders/scan`` — dry-run preview. Runs the scanner and
  returns what a scaffold WOULD create, with nothing created.
* ``POST /api/chat/folders/scaffold`` — creates the confirmed selection by
  composing the existing folder create path.

The split is what makes the feature safe to point at an unfamiliar tree: a scan
is read-only, so the destructive-sounding half of "build me twenty folders" only
happens against a selection the user has seen.

Three properties belong to this module rather than to the scanner:

* **The scan root is validated by the folder API's own validator.** ``scan``
  refuses exactly what creating a folder by hand refuses — a relative path, a
  sensitive path, a path that is not a directory — because it calls the same
  :func:`~kiro_crew.dashboard.chat_folders._validate_project_dir`. One function,
  and the message the user reads is the one they would have read anyway.
* **Reconcile marking is an overlay, not a detection rule.** The scanner's
  output depends only on the filesystem and the passed configuration, which is
  what makes two scans of an unchanged tree compare equal. Which candidates
  already have folders is store state, so it is layered on here — a candidate is
  marked ``existing`` and drops out of the default selection, but it is still
  reported, because "already set up" is information the user wants.
* **Configuration is threaded in, never read by the scanner.** The scanner takes
  ``extra_signals`` and ``depth_cap`` as arguments; this module is where they
  come from ``scaffold.*``.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any, Iterable

from aiohttp import web

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard.chat_folders import _validate_project_dir
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.executors import subprocess_executor
from kiro_crew.project_scan import Candidate, CandidateTree, Tier, scan
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

# Response status telling a preview with no candidates apart from one with some.
# Zero candidates is an answer — the tree holds no packages this scanner
# recognizes — so it is a 200 with a status a surface can branch on, not an
# error a surface has to render as a failure.
STATUS_OK = "ok"
STATUS_EMPTY = "empty"


class _RootError(ValueError):
    """A requested scan root was missing or rejected by the folder validator.

    Carries the ``error``/``code`` pair verbatim so both endpoints answer an
    unusable root identically — the message the user sees for a bad scan root is
    the one manual folder creation would have given them.
    """

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


def _resolve_root(body: object) -> str:
    """Return the validated, resolved scan root named by a request body.

    Resolution matters beyond validation: the returned path is what the scan is
    rooted at, so every candidate path is spelled the same way a folder's stored
    ``project_dir`` is (both come out of the same ``realpath``). That is what
    lets reconcile compare the two by equality rather than by re-resolving a
    stored path per candidate.

    Raises:
        _RootError: if the body is not an object, names no root, or names one the
            folder API would refuse.
    """

    if not isinstance(body, dict):
        raise _RootError("request body must be a JSON object", "invalid_json")
    raw = str(body.get("root") or "").strip()
    if not raw:
        # ``_validate_project_dir`` accepts "" — a folder is allowed to have no
        # project directory at all — so the empty case has to be caught here or a
        # rootless scan would fall through to scanning nothing.
        raise _RootError("root required", "folder_scan_root_required")
    resolved, err = _validate_project_dir(raw)
    if err:
        raise _RootError(err, "folder_scan_root_invalid")
    return resolved


def _root_error_response(exc: _RootError) -> web.Response:
    """Return the 400 for an unusable scan root."""

    # ``code`` is the contract a client branches on; ``error`` is advisory prose
    # rendered into a localized UI.
    return web.json_response({"error": str(exc), "code": exc.code}, status=400)


async def _scan_off_loop(root: str) -> CandidateTree:
    """Run one scan of ``root`` with the configured limits, off the loop thread.

    Both the config read and the walk are blocking filesystem work whose cost
    scales with a tree the user merely pointed at, so neither may run on the
    event loop: one unresponsive network mount would otherwise stall every chat,
    WS push, and heartbeat behind it. The walk goes to the subprocess pool (the
    pool for potentially-slow work) rather than the maintenance pool, whose
    periodic sweeps must stay responsive.
    """

    cfg = await asyncio.to_thread(KiroCrewConfig.load)
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        subprocess_executor(),
        lambda: scan(
            Path(root),
            extra_signals=cfg.scaffold.extra_manifest_signals,
            depth_cap=cfg.scaffold.depth_cap,
        ),
    )


def scaffolded_project_dirs(folders: Iterable[Any]) -> set[str]:
    """Return the project directories folders are already bound to.

    Skips entries without a usable ``project_dir``, and entries that are not
    dicts at all: the folder store is loaded without validation, so a
    hand-edited or legacy ``folders.json`` can hold either.
    """

    dirs: set[str] = set()
    for folder in folders:
        if not isinstance(folder, dict):
            continue
        project_dir = str(folder.get("project_dir") or "").strip()
        if project_dir:
            dirs.add(project_dir)
    return dirs


def default_selected(candidate: Candidate, *, existing: bool) -> bool:
    """Return whether a candidate starts ticked in the preview.

    An already-scaffolded candidate is never ticked whatever its tier: creation
    is additive, so the one thing a re-scan must not offer to do is duplicate a
    folder the user already has. Below that, the tier decides — ``AUTO`` is
    confident enough to tick, ``OFFERED`` is shown for the user to opt into.
    """

    return not existing and candidate.tier is Tier.AUTO


def _candidate_payload(candidate: Candidate, *, existing: bool) -> dict[str, Any]:
    """Render one candidate for a preview response."""

    return {
        # ``path`` is both the display target and the identifier the scaffold
        # request names a selection with, so it is spelled once, here.
        "path": candidate.path,
        "name": candidate.name,
        "parent_path": candidate.parent_path,
        "tier": candidate.tier.value,
        "signals": list(candidate.signals),
        "existing": existing,
        "selected": default_selected(candidate, existing=existing),
    }


def _grouped(payloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return candidate paths bucketed by parent, in candidate order.

    Grouping is computed server-side so every surface renders the same buckets in
    the same order — the preview's per-group select-all control is only coherent
    if the groups themselves are agreed on. Buckets hold paths rather than
    repeated candidate objects, so the flat list stays the single copy of each
    candidate.
    """

    groups: dict[str | None, list[str]] = {}
    for payload in payloads:
        parent = payload["parent_path"]
        groups.setdefault(parent, []).append(payload["path"])
    # ``None`` (hanging off the scan root) sorts first; the rest follow their
    # parent's path, which is the order the flat list is already in.
    ordered = sorted(groups.items(), key=lambda item: (item[0] is not None, item[0] or ""))
    return [{"parent_path": parent, "paths": paths} for parent, paths in ordered]


def preview_payload(tree: CandidateTree, existing_dirs: set[str]) -> dict[str, Any]:
    """Return the scan response for ``tree``, overlaid with reconcile state.

    The overlay is an exact match against the project directories folders already
    hold: candidate paths and stored ``project_dir`` values are both resolved the
    same way, so equality is the whole comparison — no prefix or realpath
    guessing, which could mark a sibling directory as taken.
    """

    candidates = [
        _candidate_payload(candidate, existing=candidate.path in existing_dirs)
        for candidate in tree.candidates
    ]
    return {
        "root": tree.root,
        "root_name": os.path.basename(tree.root.rstrip(os.sep)) or tree.root,
        # The root's own folder is created by the scaffold step rather than being
        # a candidate, so the preview reports its reconcile state separately.
        "root_existing": tree.root in existing_dirs,
        "status": STATUS_EMPTY if not candidates else STATUS_OK,
        "candidates": candidates,
        "groups": _grouped(candidates),
        "warnings": list(tree.warnings),
    }


async def api_chat_folders_scan(request: web.Request) -> web.Response:
    """POST /api/chat/folders/scan — preview the folders a project would produce.

    Body: ``{"root": "<absolute path>"}``. Creates nothing.
    """

    state: DashboardState = request.app["state"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    try:
        root = _resolve_root(body)
    except _RootError as exc:
        # ``_validate_project_dir`` already SEL-logs a sensitive-path refusal;
        # the other rejections are ordinary caller error.
        return _root_error_response(exc)

    tree = await _scan_off_loop(root)
    # Read the store AFTER the scan: the scan is the long part, and a folder
    # created while it ran should be reflected rather than offered again.
    payload = preview_payload(tree, scaffolded_project_dirs(state._folders))
    sel().log_api_access(
        caller=request.get("user", "dashboard"),
        operation="chat.folder_scan",
        outcome="allowed",
        source="dashboard",
        # The root is the resource; candidate paths are not enumerated into the
        # audit log, and a count is what makes the entry useful.
        resources=f"{root} candidates={len(payload['candidates'])}",
    )
    if payload["warnings"]:
        logger.info(
            "Folder scan of %s completed with %d warning(s)", root, len(payload["warnings"])
        )
    return web.json_response(payload)
