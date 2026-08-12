"""Tests for the folder-scaffold endpoints.

The scan endpoint is a preview, so what is pinned here is mostly what it does
NOT do: it creates no folder, it refuses exactly the roots manual folder
creation refuses, and it never offers to re-create a folder the user already
has. The scanner's own detection rules are covered by the engine suites; the
layouts below are the smallest ones that exercise an endpoint concern.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew.dashboard.chat_folder_scaffold import (
    STATUS_EMPTY,
    STATUS_OK,
    api_chat_folders_scan,
)


def _make_scaffold_app(state: Any) -> web.Application:
    """Minimal aiohttp app with the folder-scaffold endpoints."""

    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/chat/folders/scan", api_chat_folders_scan)
    return app


@pytest.fixture
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path / "home")
    (tmp_path / "home").mkdir()
    return _make_state(tmp_path)


def _sibling_repos(root: Path) -> Path:
    """Two sibling repositories under a plain directory: both AUTO."""

    root.mkdir()
    for name in ("api", "web"):
        (root / name / ".git").mkdir(parents=True)
    return root


def _monorepo(root: Path) -> Path:
    """A repo whose own manifest declares two members: both OFFERED.

    The root carrying a manifest puts everything below it inside a package, which
    is the tier split worth exercising through the endpoint — a payload where
    nothing is ticked by default has to still be a usable preview.
    """

    root.mkdir()
    (root / ".git").mkdir()
    (root / "package.json").write_text(json.dumps({"workspaces": ["packages/*"]}), encoding="utf-8")
    for name in ("alpha", "beta"):
        member = root / "packages" / name
        member.mkdir(parents=True)
        (member / "package.json").write_text("{}", encoding="utf-8")
    return root


def _nested(root: Path) -> Path:
    """Two repositories, one of which holds a nested manifest.

    Spans both tiers and two group levels, which is what the selection-default
    and grouping assertions need from one layout.
    """

    root.mkdir()
    (root / "other" / ".git").mkdir(parents=True)
    (root / "repo" / ".git").mkdir(parents=True)
    # A manifest INSIDE a repository is the ambiguous case — offered, unticked.
    (root / "repo" / "sub").mkdir()
    (root / "repo" / "sub" / "pyproject.toml").write_text("", encoding="utf-8")
    return root


async def _scan(client: TestClient, root: Any) -> tuple[int, dict[str, Any]]:
    resp = await client.post("/api/chat/folders/scan", json={"root": str(root)})
    return resp.status, await resp.json()


class TestScanPreview:
    @pytest.mark.asyncio
    async def test_returns_candidates_with_tier_name_and_path(
        self, state: Any, tmp_path: Path
    ) -> None:
        root = _sibling_repos(tmp_path / "work")
        async with TestClient(TestServer(_make_scaffold_app(state))) as client:
            status, body = await _scan(client, root)

        assert status == 200
        assert body["status"] == STATUS_OK
        assert [(c["name"], c["tier"]) for c in body["candidates"]] == [
            ("api", "auto"),
            ("web", "auto"),
        ]
        assert [c["path"] for c in body["candidates"]] == [
            str(root / "api"),
            str(root / "web"),
        ]
        assert body["root"] == str(root)
        assert body["root_name"] == "work"

    @pytest.mark.asyncio
    async def test_creates_nothing(self, state: Any, tmp_path: Path) -> None:
        """A preview is a preview: no folder may exist afterwards."""

        root = _sibling_repos(tmp_path / "work")
        async with TestClient(TestServer(_make_scaffold_app(state))) as client:
            status, body = await _scan(client, root)

        assert status == 200
        assert len(body["candidates"]) == 2
        assert state._folders == []

    @pytest.mark.asyncio
    async def test_monorepo_members_are_offered_unticked(self, state: Any, tmp_path: Path) -> None:
        """Inside a package every nested manifest is ambiguous, so nothing is ticked."""

        root = _monorepo(tmp_path / "mono")
        async with TestClient(TestServer(_make_scaffold_app(state))) as client:
            _, body = await _scan(client, root)

        selection = {c["name"]: (c["tier"], c["selected"]) for c in body["candidates"]}
        assert selection == {"alpha": ("offered", False), "beta": ("offered", False)}
        # Declared membership is reported alongside the manifest that was found.
        assert body["candidates"][0]["signals"] == ["manifest:package.json", "member"]

    @pytest.mark.asyncio
    async def test_selection_default_follows_tier(self, state: Any, tmp_path: Path) -> None:
        """A tree holding both tiers: AUTO ticked, OFFERED unticked, one payload."""

        root = _nested(tmp_path / "mixed")
        async with TestClient(TestServer(_make_scaffold_app(state))) as client:
            _, body = await _scan(client, root)

        selection = {c["name"]: (c["tier"], c["selected"]) for c in body["candidates"]}
        assert selection == {
            "other": ("auto", True),
            "repo": ("auto", True),
            "sub": ("offered", False),
        }

    @pytest.mark.asyncio
    async def test_groups_bucket_candidates_by_parent(self, state: Any, tmp_path: Path) -> None:
        """Grouping is server-side so per-group select-all means the same everywhere."""

        root = _nested(tmp_path / "mixed")
        async with TestClient(TestServer(_make_scaffold_app(state))) as client:
            _, body = await _scan(client, root)

        assert body["groups"] == [
            # Candidates hanging off the scan root come first, then each parent.
            {"parent_path": None, "paths": [str(root / "other"), str(root / "repo")]},
            {"parent_path": str(root / "repo"), "paths": [str(root / "repo" / "sub")]},
        ]
        # Every candidate appears in exactly one group.
        grouped = [path for group in body["groups"] for path in group["paths"]]
        assert sorted(grouped) == sorted(c["path"] for c in body["candidates"])

    @pytest.mark.asyncio
    async def test_signals_explain_each_candidate(self, state: Any, tmp_path: Path) -> None:
        root = _sibling_repos(tmp_path / "work")
        async with TestClient(TestServer(_make_scaffold_app(state))) as client:
            _, body = await _scan(client, root)

        assert all(c["signals"] == ["git"] for c in body["candidates"])

    @pytest.mark.asyncio
    async def test_empty_root_is_a_status_not_an_error(self, state: Any, tmp_path: Path) -> None:
        """Zero candidates must be answerable — a 200 a surface can branch on."""

        root = tmp_path / "bare"
        (root / "notes").mkdir(parents=True)
        async with TestClient(TestServer(_make_scaffold_app(state))) as client:
            status, body = await _scan(client, root)

        assert status == 200
        assert body["status"] == STATUS_EMPTY
        assert body["candidates"] == []
        assert body["groups"] == []

    @pytest.mark.asyncio
    async def test_warnings_are_reported_not_raised(self, state: Any, tmp_path: Path) -> None:
        """A declaration that cannot be parsed costs that declaration, not the scan."""

        root = tmp_path / "mono"
        root.mkdir()
        (root / ".git").mkdir()
        (root / "package.json").write_text("{not json", encoding="utf-8")
        (root / "svc" / ".git").mkdir(parents=True)
        async with TestClient(TestServer(_make_scaffold_app(state))) as client:
            status, body = await _scan(client, root)

        assert status == 200
        assert [c["name"] for c in body["candidates"]] == ["svc"]
        assert len(body["warnings"]) == 1
        assert "package.json" in body["warnings"][0]


class TestScanRootValidation:
    @pytest.mark.asyncio
    async def test_relative_root_rejected(self, state: Any) -> None:
        async with TestClient(TestServer(_make_scaffold_app(state))) as client:
            status, body = await _scan(client, "relative/path")

        assert status == 400
        assert body["error"] == "project_dir must be an absolute path"
        assert body["code"] == "folder_scan_root_invalid"

    @pytest.mark.asyncio
    async def test_missing_root_rejected(self, state: Any, tmp_path: Path) -> None:
        async with TestClient(TestServer(_make_scaffold_app(state))) as client:
            status, body = await _scan(client, tmp_path / "does-not-exist")

        assert status == 400
        assert body["error"] == "project_dir must be an existing directory"
        assert body["code"] == "folder_scan_root_invalid"

    @pytest.mark.asyncio
    async def test_sensitive_root_rejected(self, state: Any) -> None:
        """The scan refuses what manual folder creation refuses, same wording."""

        async with TestClient(TestServer(_make_scaffold_app(state))) as client:
            status, body = await _scan(client, "~/.ssh")

        assert status == 400
        assert body["error"] == "project_dir refers to a sensitive path"
        assert body["code"] == "folder_scan_root_invalid"

    @pytest.mark.asyncio
    async def test_absent_root_field_rejected(self, state: Any) -> None:
        """An empty root is caller error, not a scan of nothing."""

        async with TestClient(TestServer(_make_scaffold_app(state))) as client:
            resp = await client.post("/api/chat/folders/scan", json={})
            assert resp.status == 400
            body = await resp.json()

        assert body["code"] == "folder_scan_root_required"

    @pytest.mark.asyncio
    async def test_non_object_body_rejected(self, state: Any) -> None:
        async with TestClient(TestServer(_make_scaffold_app(state))) as client:
            resp = await client.post("/api/chat/folders/scan", json=["/tmp"])
            assert resp.status == 400
            assert (await resp.json())["code"] == "invalid_json"

    @pytest.mark.asyncio
    async def test_malformed_json_rejected(self, state: Any) -> None:
        async with TestClient(TestServer(_make_scaffold_app(state))) as client:
            resp = await client.post(
                "/api/chat/folders/scan",
                data="{not json",
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 400
            assert (await resp.json())["code"] == "invalid_json"


class TestReconcileOverlay:
    @pytest.mark.asyncio
    async def test_existing_candidate_marked_and_unticked(self, state: Any, tmp_path: Path) -> None:
        """A re-scan must not offer to duplicate a folder the user already has."""

        root = _sibling_repos(tmp_path / "work")
        state._folders = [
            {
                "id": "f-api",
                "name": "api",
                "order": 0,
                "collapsed": False,
                "project_dir": str(root / "api"),
            }
        ]
        async with TestClient(TestServer(_make_scaffold_app(state))) as client:
            _, body = await _scan(client, root)

        overlay = {c["name"]: (c["existing"], c["selected"]) for c in body["candidates"]}
        assert overlay == {"api": (True, False), "web": (False, True)}
        # Still reported: "already set up" is information, not a reason to hide it.
        assert len(body["candidates"]) == 2

    @pytest.mark.asyncio
    async def test_new_package_since_a_prior_scan_is_still_offered(
        self, state: Any, tmp_path: Path
    ) -> None:
        root = _sibling_repos(tmp_path / "work")
        state._folders = [
            {"id": "f-api", "name": "api", "order": 0, "project_dir": str(root / "api")},
            {"id": "f-web", "name": "web", "order": 1, "project_dir": str(root / "web")},
        ]
        (root / "batch" / ".git").mkdir(parents=True)
        async with TestClient(TestServer(_make_scaffold_app(state))) as client:
            _, body = await _scan(client, root)

        new = [c for c in body["candidates"] if not c["existing"]]
        assert [(c["name"], c["tier"], c["selected"]) for c in new] == [("batch", "auto", True)]

    @pytest.mark.asyncio
    async def test_match_is_exact_not_by_prefix(self, state: Any, tmp_path: Path) -> None:
        """A folder on a SIBLING directory must not mark a candidate as taken."""

        root = _sibling_repos(tmp_path / "work")
        (root / "api2" / ".git").mkdir(parents=True)
        state._folders = [
            {"id": "f-api", "name": "api", "order": 0, "project_dir": str(root / "api")}
        ]
        async with TestClient(TestServer(_make_scaffold_app(state))) as client:
            _, body = await _scan(client, root)

        overlay = {c["name"]: c["existing"] for c in body["candidates"]}
        assert overlay == {"api": True, "api2": False, "web": False}

    @pytest.mark.asyncio
    async def test_root_reconcile_state_reported_separately(
        self, state: Any, tmp_path: Path
    ) -> None:
        """The root's folder is created by the scaffold step, so it gets its own flag."""

        root = _sibling_repos(tmp_path / "work")
        async with TestClient(TestServer(_make_scaffold_app(state))) as client:
            _, before = await _scan(client, root)
            assert before["root_existing"] is False

            state._folders = [{"id": "f-root", "name": "work", "project_dir": str(root)}]
            _, after = await _scan(client, root)

        assert after["root_existing"] is True

    @pytest.mark.asyncio
    async def test_unusable_folder_entries_do_not_break_the_overlay(
        self, state: Any, tmp_path: Path
    ) -> None:
        """The folder store is loaded unvalidated, so a junk entry must be skipped."""

        root = _sibling_repos(tmp_path / "work")
        state._folders = [
            "not a folder",
            {"id": "f-none", "name": "No project"},
            {"id": "f-blank", "name": "Blank", "project_dir": ""},
            {"id": "f-null", "name": "Null", "project_dir": None},
            {"id": "f-api", "name": "api", "project_dir": str(root / "api")},
        ]
        async with TestClient(TestServer(_make_scaffold_app(state))) as client:
            status, body = await _scan(client, root)

        assert status == 200
        assert {c["name"]: c["existing"] for c in body["candidates"]} == {
            "api": True,
            "web": False,
        }


class TestScanConfigThreading:
    @pytest.mark.asyncio
    async def test_depth_cap_comes_from_config(
        self, state: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The scanner takes its limits as arguments; this endpoint supplies them."""

        root = tmp_path / "deep"
        (root / "a" / "b" / "c").mkdir(parents=True)
        (root / "a" / "b" / "c" / ".git").mkdir()

        loaded = _config_with_scaffold(depth_cap=2)
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_folder_scaffold.KiroCrewConfig",
            _StubConfig(loaded),
        )
        async with TestClient(TestServer(_make_scaffold_app(state))) as client:
            _, capped = await _scan(client, root)
        assert capped["status"] == STATUS_EMPTY

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_folder_scaffold.KiroCrewConfig",
            _StubConfig(_config_with_scaffold(depth_cap=5)),
        )
        async with TestClient(TestServer(_make_scaffold_app(state))) as client:
            _, deep = await _scan(client, root)
        assert [c["name"] for c in deep["candidates"]] == ["c"]

    @pytest.mark.asyncio
    async def test_extra_manifest_signals_come_from_config(
        self, state: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "work"
        (root / "svc").mkdir(parents=True)
        (root / "svc" / "composer.json").write_text("{}", encoding="utf-8")

        async with TestClient(TestServer(_make_scaffold_app(state))) as client:
            _, unconfigured = await _scan(client, root)
        assert unconfigured["status"] == STATUS_EMPTY

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_folder_scaffold.KiroCrewConfig",
            _StubConfig(_config_with_scaffold(extra_manifest_signals=["composer.json"])),
        )
        async with TestClient(TestServer(_make_scaffold_app(state))) as client:
            _, configured = await _scan(client, root)
        assert [c["name"] for c in configured["candidates"]] == ["svc"]
        assert configured["candidates"][0]["signals"] == ["manifest:composer.json"]


def _config_with_scaffold(**overrides: Any) -> Any:
    """Return a real loaded config with ``scaffold.*`` overridden.

    A real config object rather than a mock: the endpoint reads two attributes off
    it, and a mock would satisfy the read while proving nothing about the field
    names actually being the ones the loader defines.
    """

    from kiro_crew.config.loader import KiroCrewConfig, ScaffoldConfig

    cfg = KiroCrewConfig()
    cfg.scaffold = ScaffoldConfig(**overrides)
    return cfg


class _StubConfig:
    """Stands in for ``KiroCrewConfig`` so ``.load()`` returns a chosen config."""

    def __init__(self, cfg: Any) -> None:
        self._cfg = cfg

    def load(self) -> Any:
        return self._cfg


class TestRouteRegistration:
    """The handler must be reachable in the running dashboard, not just in
    the private apps these tests build. Registration lives inline in
    ``start_dashboard`` (no standalone route factory to invoke), so this
    guard inspects that function's source the same way the repo's YAML
    guard inspects call sites -- it fails if the ``add_post`` line for the
    scan route is ever dropped."""

    def test_facade_reexports_the_scan_handler(self) -> None:
        from kiro_crew.dashboard import chat

        assert chat.api_chat_folders_scan is api_chat_folders_scan

    def test_start_dashboard_registers_the_scan_route(self) -> None:
        import inspect

        from kiro_crew.dashboard.server import start_dashboard

        source = inspect.getsource(start_dashboard)
        assert (
            'add_post("/api/chat/folders/scan", chat.api_chat_folders_scan)' in source
        ), "POST /api/chat/folders/scan is not registered in start_dashboard"
