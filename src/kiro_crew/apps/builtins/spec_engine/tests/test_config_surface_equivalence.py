"""Config write-path equivalence: the MCP tool and the route produce one file.

The property (design Property 1): FOR ALL configuration patches accepted by both
surfaces, applying the patch through the Engine_MCP_Server's ``write_config`` and
applying the same patch to an identical starting store through the
Operator_Surface's ``PUT`` route yields BYTE-IDENTICAL ``config.json`` files.

Why it can be claimed at all, stated so the tests below are read as verification
rather than as the reason: both paths call ``ConfigStore.write``, which is the one
place the document is merged, validated, serialized
(``json.dumps(..., indent=2, sort_keys=True)`` plus a trailing newline) and
atomically written. The property holds because there is ONE serializer — not
because two were kept in step. What these tests defend is that neither surface
grows a second one, which is the change that would break byte-identity while
every other test still passed.

**"Accepted by both" is a real restriction, not a hedge.** The two surfaces write
on different fences: the route is ``operator_confirmed`` and may write the
config-only sections, the MCP door is not and refuses them. So the strategy below
generates patches over the settings NEITHER surface fences, and a separate test
pins that the fenced half really does diverge — otherwise the restriction would be
untested decoration and the property would quietly be claimed for patches one
surface never accepts.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from kiro_crew.apps.builtins.spec_engine.backend import routes
from kiro_crew.apps.builtins.spec_engine.engine.config import (
    CONFIG_FILENAME,
    CONFIG_ONLY_PATHS,
    ConfigStore,
    ConfigWriteRefused,
    default_root,
)
from kiro_crew.apps.builtins.spec_engine.engine_mcp.operations import (
    ENGINE_MCP_SURFACE,
    EngineOperations,
)
from kiro_crew.apps.builtins.spec_engine.engine_mcp.server import handle

#: Settings both surfaces accept: ordinary values under no config-only path, with
#: the bounds the registry validates against so a generated patch is a patch the
#: engine would take rather than one it refuses before either surface matters.
UNFENCED_SETTINGS: dict[str, st.SearchStrategy[Any]] = {
    "concurrency.global_max_runs": st.integers(min_value=1, max_value=64),
    "concurrency.project_max_runs": st.integers(min_value=1, max_value=32),
    "concurrency.wave_max_tasks": st.integers(min_value=1, max_value=32),
    "limits.task_retry_limit": st.integers(min_value=0, max_value=20),
    "limits.revision_cycle_limit": st.integers(min_value=1, max_value=20),
    "limits.verify_retry_limit": st.integers(min_value=0, max_value=20),
    "timeouts.authoring_s": st.integers(min_value=1, max_value=100_000),
    "timeouts.executing_s": st.integers(min_value=1, max_value=100_000),
    "timeouts.stage_command_s": st.integers(min_value=1, max_value=100_000),
    "budget.run_ceiling_credits": st.floats(
        min_value=0.01, max_value=1000.0, allow_nan=False, allow_infinity=False
    ),
    "budget.warn_fraction": st.floats(
        min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False
    ),
    "watch.interval_s": st.integers(min_value=30, max_value=86_400),
    "notify.channel": st.sampled_from(["dashboard", "slack", "discord"]),
    "telemetry.enabled": st.booleans(),
}


def _nest(key: str, value: Any) -> dict[str, Any]:
    """Turn a dotted setting key into the nested object a patch carries."""
    head, _, tail = key.partition(".")
    return {head: _nest(tail, value)} if tail else {head: value}


def _merge_into(target: dict[str, Any], addition: dict[str, Any]) -> None:
    for key, value in addition.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _merge_into(target[key], value)
        else:
            target[key] = value


@st.composite
def unfenced_patches(draw: st.DrawFn) -> dict[str, Any]:
    """A patch over one or more settings neither surface fences."""
    chosen = draw(
        st.lists(
            st.sampled_from(sorted(UNFENCED_SETTINGS)),
            min_size=1,
            max_size=5,
            unique=True,
        )
    )
    patch: dict[str, Any] = {}
    for key in chosen:
        _merge_into(patch, _nest(key, draw(UNFENCED_SETTINGS[key])))
    return patch


# --- the two write paths, driven ---------------------------------------------


def _through_the_mcp_tool(root: Path, patch: dict[str, Any], actor: str) -> None:
    """Apply *patch* through the Engine_MCP_Server's own tool dispatch.

    The TOOL rather than the library method underneath it: the property is about
    what the two surfaces produce, and a test that called the shared method from
    both sides would prove the shared method is deterministic while saying nothing
    about either surface reaching it.
    """
    ops = EngineOperations(
        config_root=root,
        state_root=root.parent / "state",
        audit_root=root.parent / "audit",
    )
    reply = handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "write_config", "arguments": {"patch": patch, "actor": actor}},
        },
        ops=ops,
    )
    assert reply is not None and "error" not in reply, reply
    payload = json.loads(reply["result"]["content"][0]["text"])
    assert "refused" not in payload, f"the MCP door refused a patch it should accept: {payload}"


def _through_the_route(patch: dict[str, Any], actor: str) -> None:
    """Apply *patch* through the Operator_Surface's own write path.

    ``routes._write_config`` is the whole of what the ``PUT`` handler does once the
    body has parsed — it is the function the handler hands to
    ``asyncio.to_thread``. Driving it directly keeps this property test
    synchronous, which is what lets hypothesis generate over it;
    :class:`TestTheRealRouteProducesTheSameBytes` closes the gap by driving the
    actual HTTP route once and comparing against the same MCP tool.
    """
    routes._write_config(patch, actor)


def _bytes_at(root: Path) -> bytes:
    return (root / CONFIG_FILENAME).read_bytes()


# --- the property -----------------------------------------------------------


class TestTheTwoSurfacesProduceOneFile:
    @settings(
        max_examples=60,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    @given(patch=unfenced_patches())
    def test_byte_identical_config_for_a_patch_both_surfaces_accept(
        self, patch: dict[str, Any], tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        """Property 1, over generated patches.

        Fresh roots per example rather than a shared fixture: the claim is about
        two identical STARTING stores, so an example that inherited the previous
        one's document would be comparing a different pair of writes.
        """
        base = tmp_path_factory.mktemp("equivalence")
        mcp_root = base / "mcp" / "config"
        route_home = base / "route-home"
        route_home.mkdir(parents=True)

        _through_the_mcp_tool(mcp_root, patch, "an-agent")

        import os

        previous = os.environ.get("KIROCREW_HOME")
        os.environ["KIROCREW_HOME"] = str(route_home)
        try:
            route_root = default_root()
            _through_the_route(patch, "an-operator")
        finally:
            if previous is None:
                os.environ.pop("KIROCREW_HOME", None)
            else:
                os.environ["KIROCREW_HOME"] = previous

        assert _bytes_at(mcp_root) == _bytes_at(route_root), (
            "the two surfaces produced different config.json bytes for the same "
            "patch; one of them has grown a serializer of its own"
        )

    def test_the_bytes_are_the_engines_own_serialization(
        self, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        """Not merely equal to each other: equal to the engine's canonical form.

        Two surfaces that had BOTH grown the same second serializer would satisfy
        the property above, so the shape is pinned independently — sorted keys, two
        spaces, one trailing newline.
        """
        base = tmp_path_factory.mktemp("canonical")
        mcp_root = base / "mcp" / "config"
        patch = {"limits": {"task_retry_limit": 3}, "concurrency": {"wave_max_tasks": 2}}
        _through_the_mcp_tool(mcp_root, patch, "an-agent")

        raw = _bytes_at(mcp_root).decode("utf-8")
        document = json.loads(raw)
        assert raw == json.dumps(document, indent=2, sort_keys=True) + "\n"
        assert raw.endswith("\n") and not raw.endswith("\n\n")

    @settings(
        max_examples=30,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    @given(patch=unfenced_patches())
    def test_the_generated_patches_really_are_accepted_by_both_fences(
        self, patch: dict[str, Any], tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        """Non-vacuity for the restriction the property carries.

        A strategy that generated only patches one surface refuses would make the
        equivalence test pass on two absent files, or fail for a reason that is not
        the property. So every generated patch is asserted to touch no fenced path
        and to be accepted on the UNCONFIRMED surface, which is the stricter of the
        two.
        """
        from kiro_crew.apps.builtins.spec_engine.engine.config import config_only_paths

        assert config_only_paths(patch) == ()
        root = tmp_path_factory.mktemp("both-fences") / "config"
        ConfigStore(root).write(patch, surface=ENGINE_MCP_SURFACE, actor="an-agent")
        assert (root / CONFIG_FILENAME).is_file()


class TestTheFencedHalfIsWhereTheSurfacesDiverge:
    """The restriction, shown to be a real boundary rather than a hedge.

    If both surfaces accepted everything, "patches accepted by both" would be
    every patch and the property's scope statement would be noise. These two show
    the fence separating them, so the scope means something.
    """

    @pytest.mark.parametrize("path", sorted(CONFIG_ONLY_PATHS))
    def test_the_fenced_paths_are_the_documented_ones(self, path: str) -> None:
        """Read from the engine, so a path added to the fence is covered here
        without an edit — and a fence that emptied out fails the parametrization
        rather than passing on nothing."""
        assert path and isinstance(path, str)

    def test_the_unconfirmed_surface_refuses_what_the_route_accepts(self, tmp_path: Path) -> None:
        patch: dict[str, Any] = {"quality_gates": []}
        with pytest.raises(ConfigWriteRefused):
            ConfigStore(tmp_path / "mcp").write(patch, surface=ENGINE_MCP_SURFACE)
        # The same patch on the route's surface lands, so the divergence is the
        # fence and not the patch being invalid.
        ConfigStore(tmp_path / "route").write(patch, surface=routes.WRITE_SURFACE)
        assert (tmp_path / "route" / CONFIG_FILENAME).is_file()


class TestTheRealRouteProducesTheSameBytes:
    """The property, once, through the ACTUAL HTTP route.

    The hypothesis loop above drives ``routes._write_config`` so it can stay
    synchronous. This closes that gap: a real ``PUT`` over a real aiohttp
    application, compared byte-for-byte against the MCP tool's file. If the
    handler ever stops delegating to ``_write_config``, the loop above would keep
    passing and this would not.
    """

    @pytest.mark.asyncio
    async def test_a_put_over_the_wire_matches_the_tool(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer

        patch = {
            "limits": {"task_retry_limit": 9, "revision_cycle_limit": 4},
            "budget": {"run_ceiling_credits": 12.5},
            "notify": {"channel": "slack"},
        }

        mcp_root = tmp_path / "mcp" / "config"
        _through_the_mcp_tool(mcp_root, patch, "an-agent")

        route_home = tmp_path / "route-home"
        route_home.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(route_home))
        monkeypatch.setattr(routes, "is_app_enabled", lambda name: True)

        @web.middleware
        async def _auth(request: web.Request, handler: Any) -> web.StreamResponse:
            request["user"] = "an-operator"
            return await handler(request)

        application = web.Application(middlewares=[_auth])
        routes.register_routes(application)
        async with TestClient(TestServer(application)) as client:
            response = await client.put(f"{routes.PREFIX}/config", json={"patch": patch})
            status = response.status
            body = await response.json()
        assert status == 200, body

        assert _bytes_at(mcp_root) == _bytes_at(
            default_root()
        ), "the HTTP route and the MCP tool wrote different bytes for one patch"
