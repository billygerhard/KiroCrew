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

**Why a SEQUENCE of patches and not one.** A single patch onto an empty document
exercises the serializer but not the merge: deletion has nothing to delete, a
nested object has nothing to merge into, and every example lands on the same
starting document. :class:`TestSequencesOfPatchesConvergeOnOneFile` drives a
generated sequence into both surfaces step by step and compares the bytes after
EVERY step, so a divergence that only appears on the second write — a merge that
descends differently, a deletion one surface treats as a no-op — is caught at the
step that caused it rather than being masked by a later overwrite.

**What the generators can produce:** ordinary scalar settings at app scope, nested
per-project entries four levels deep (``projects.<name>.variables.<KEY>``),
non-ASCII text in project names, paths, and variable values, keys whose last
segment classifies as a credential (elided in a REPLY, written verbatim to the
FILE — the elision is a display rule, so byte-identity must still hold), deletion
of a leaf or of a whole project entry via ``None``, deletion of a key that was
never written, and the empty patch.

**What they cannot produce, stated so the property's reach is not overread:**

* **Patches either surface refuses.** Config-only sections (the route accepts
  them, the MCP door does not) are excluded by construction and their divergence
  is pinned separately by :class:`TestTheFencedHalfIsWhereTheSurfacesDiverge`.
  Documents that fail validation are refused by both, so equivalence there is the
  trivially equal absence of a write and says nothing.
* **Concurrency.** Both doors are driven sequentially. Whether two surfaces
  writing at the same instant serialize is ``ConfigStore``'s lock-file property,
  covered where the lock lives, not here.
* **Non-JSON values.** No NaN, no infinity, no bytes, no lone surrogate — the
  transports carry JSON, so a value that cannot round-trip through it never
  reaches either write path.
* **Key names carrying a ``.``**, which would make a dotted fenced-path pattern
  and a document key disagree about where a node is. Excluded from generated
  names rather than claimed safe.
* **Ordering effects.** ``sort_keys=True`` means the order keys arrived in cannot
  show up in the bytes; a generator that shuffled key order would explore nothing
  the serializer can express.
"""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence

import pytest
from hypothesis import HealthCheck, find, given, settings
from hypothesis import strategies as st
from hypothesis.errors import NoSuchExample

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


#: Characters generated names and values are drawn from. Non-ASCII is in reach
#: deliberately — the serializer escapes it and the two surfaces must escape it
#: identically. Excluded on purpose: ``.``, which would put a document key and a
#: dotted fenced-path pattern in disagreement about where a node sits, and
#: surrogates and control characters, which are not JSON values a transport
#: carries.
_NAME_ALPHABET = "abcXY19-_éüñ日本語Ω"

#: Names for projects, branches and variable keys: non-empty after stripping,
#: which is what the schema requires of every one of them.
_NAMES = st.text(alphabet=_NAME_ALPHABET, min_size=1, max_size=8).filter(
    lambda text: bool(text.strip())
)

#: Variable keys, with the credential-classified ones present on purpose: a
#: value under ``api_key`` or ``GITHUB_TOKEN`` is elided in a REPLY and written
#: verbatim to the FILE, so byte-identity has to hold for exactly the keys whose
#: displayed form differs from their stored form.
_VARIABLE_KEYS = st.one_of(
    st.sampled_from(["api_key", "GITHUB_TOKEN", "region", "REPO_SLUG", "credentials"]),
    _NAMES,
)

#: Variable values: any string the map accepts, including the empty one.
_VARIABLE_VALUES = st.text(alphabet=_NAME_ALPHABET, max_size=12)


@st.composite
def project_patches(draw: st.DrawFn) -> dict[str, Any]:
    """A nested per-project patch: four levels deep at its deepest.

    ``path`` is always present because the schema requires a project to be
    locatable; a generated entry without it would be refused by BOTH surfaces and
    the example would prove nothing about equivalence.
    """
    entry: dict[str, Any] = {"path": "/" + draw(_NAMES)}
    if draw(st.booleans()):
        entry["variables"] = draw(
            st.dictionaries(_VARIABLE_KEYS, _VARIABLE_VALUES, min_size=1, max_size=3)
        )
    if draw(st.booleans()):
        entry["protected_branches"] = draw(st.lists(_NAMES, min_size=1, max_size=3))
    if draw(st.booleans()):
        entry["limits"] = {"task_retry_limit": draw(st.integers(min_value=0, max_value=20))}
    if draw(st.booleans()):
        entry["base_branch"] = draw(_NAMES)
    return {"projects": {draw(_NAMES): entry}}


#: Deletion targets. ``None`` at a key removes it, which is how a setting returns
#: to its bundled default. Deliberately absent: ``projects.<name>.path``, whose
#: removal leaves a project the schema refuses — a patch both surfaces reject
#: says nothing about whether they agree.
_DELETABLE = (
    "limits.task_retry_limit",
    "concurrency.wave_max_tasks",
    "timeouts.executing_s",
    "notify.channel",
    "budget.warn_fraction",
    "projects",
)


@st.composite
def deletion_patches(draw: st.DrawFn) -> dict[str, Any]:
    """A patch that removes a key, whether or not the key is currently there.

    Deleting an absent key is a no-op both surfaces must reach the same way: one
    that wrote a ``null`` where the other popped nothing would produce different
    bytes for a patch each surface reported as accepted.
    """
    target = draw(st.sampled_from(_DELETABLE))
    if target == "projects" and draw(st.booleans()):
        return {"projects": {draw(_NAMES): None}}
    return _nest(target, None)


def patch_sequences() -> st.SearchStrategy[list[dict[str, Any]]]:
    """One to four patches, applied in order to both surfaces.

    The empty patch is drawable: it still writes a document (the version key is
    defaulted in), so "nothing to merge" is a real case with a real file, not a
    skipped step.
    """
    return st.lists(
        st.one_of(
            unfenced_patches(),
            project_patches(),
            deletion_patches(),
            st.just({}),
        ),
        min_size=1,
        max_size=4,
    )


# --- what a drawn patch contains, for the reach assertion --------------------


def _contains_none(node: Any) -> bool:
    """Whether *node* carries a ``None`` anywhere: a deletion."""
    if node is None:
        return True
    if isinstance(node, dict):
        return any(_contains_none(value) for value in node.values())
    return False


def _has_project_entry(patch: dict[str, Any]) -> bool:
    """Whether *patch* nests an object under ``projects``: the deep shape."""
    projects = patch.get("projects")
    return isinstance(projects, dict) and any(
        isinstance(entry, dict) for entry in projects.values()
    )


def _contains_non_ascii(node: Any) -> bool:
    """Whether any key or string value in *node* is outside ASCII."""
    if isinstance(node, str):
        return not node.isascii()
    if isinstance(node, dict):
        return any(
            _contains_non_ascii(key) or _contains_non_ascii(value) for key, value in node.items()
        )
    if isinstance(node, list):
        return any(_contains_non_ascii(item) for item in node)
    return False


# --- the two write paths, driven ---------------------------------------------


def _mcp_operations(root: Path) -> EngineOperations:
    """An adapter whose config store is rooted at *root*."""
    return EngineOperations(
        config_root=root,
        state_root=root.parent / "state",
        audit_root=root.parent / "audit",
    )


def _apply_through_the_tool(ops: EngineOperations, patch: dict[str, Any], actor: str) -> None:
    """One ``tools/call`` of ``write_config``, asserted to have been accepted."""
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


def _through_the_mcp_tool(root: Path, patch: dict[str, Any], actor: str) -> None:
    """Apply *patch* through the Engine_MCP_Server's own tool dispatch.

    The TOOL rather than the library method underneath it: the property is about
    what the two surfaces produce, and a test that called the shared method from
    both sides would prove the shared method is deterministic while saying nothing
    about either surface reaching it.
    """
    _apply_through_the_tool(_mcp_operations(root), patch, actor)


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


@contextmanager
def _data_home(path: Path) -> Iterator[None]:
    """Point the route's store resolution at *path* for the duration.

    The route resolves its root through ``default_root()`` at call time, which is
    the real resolution path and the one worth driving. ``monkeypatch`` cannot do
    it here: it is function-scoped, and a hypothesis loop needs a fresh home per
    example rather than one shared across the whole test.
    """
    previous = os.environ.get("KIROCREW_HOME")
    os.environ["KIROCREW_HOME"] = str(path)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("KIROCREW_HOME", None)
        else:
            os.environ["KIROCREW_HOME"] = previous


@contextmanager
def _app_enabled() -> Iterator[None]:
    """Report the app as enabled for the duration.

    Every route is behind an enabled-app check, and the throwaway data home each
    example gets has no enabled app in it. Swapped rather than worked around by
    calling the handler directly: the check is middleware, and a test that skipped
    the middleware would not be driving the route.
    """
    original = routes.is_app_enabled
    routes.is_app_enabled = lambda name: True  # type: ignore[assignment]
    try:
        yield
    finally:
        routes.is_app_enabled = original  # type: ignore[assignment]


def _drive_both_surfaces(patches: Sequence[dict[str, Any]], base: Path) -> None:
    """Apply *patches* to both surfaces in order, comparing bytes after each step.

    The MCP side is the tool dispatch; the route side is a real ``PUT`` over a
    real aiohttp application, so what is compared is what each SURFACE produces
    rather than what the shared method underneath both produces. Interleaved step
    by step, and compared at every step: a divergence introduced by the second
    write is otherwise masked the moment a third write overwrites the same key.
    """
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    mcp_root = base / "mcp" / "config"
    route_home = base / "route-home"
    route_home.mkdir(parents=True)
    ops = _mcp_operations(mcp_root)

    @web.middleware
    async def _signed_in_operator(request: web.Request, handler: Any) -> web.StreamResponse:
        request["user"] = "an-operator"
        return await handler(request)

    with _data_home(route_home), _app_enabled():
        route_root = default_root()

        async def _drive() -> None:
            application = web.Application(middlewares=[_signed_in_operator])
            routes.register_routes(application)
            async with TestClient(TestServer(application)) as client:
                for step, patch in enumerate(patches):
                    _apply_through_the_tool(ops, patch, "an-agent")
                    response = await client.put(f"{routes.PREFIX}/config", json={"patch": patch})
                    body = await response.json()
                    assert response.status == 200, (step, patch, body)
                    assert _bytes_at(mcp_root) == _bytes_at(route_root), (
                        f"the two surfaces diverged at step {step} of {len(patches)} "
                        f"applying {patch!r}; one of them has grown a serializer, a "
                        "merge, or a deletion rule of its own"
                    )

        asyncio.run(_drive())


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

        with _data_home(route_home):
            route_root = default_root()
            _through_the_route(patch, "an-operator")

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


class TestSequencesOfPatchesConvergeOnOneFile:
    """Property 1 over SEQUENCES, driven through both surfaces for real.

    The single-patch loop above lands every example on an empty document through
    ``routes._write_config``. This one drives the real ``PUT`` handler and applies
    a generated sequence, so the merge, the deletion rule, the nested descent and
    the escaping of non-ASCII text are all in reach — and the bytes are compared
    after every step, not only at the end.
    """

    @settings(
        max_examples=60,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    @given(patches=patch_sequences())
    def test_both_surfaces_agree_after_every_step(
        self, patches: list[dict[str, Any]], tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        _drive_both_surfaces(patches, tmp_path_factory.mktemp("sequence"))

    @pytest.mark.parametrize(
        "shape,patches",
        [
            pytest.param(
                "empty-patch",
                [{}],
                id="empty-patch",
            ),
            pytest.param(
                "non-ascii-everywhere",
                [
                    {
                        "projects": {
                            "日本語-プロジェクト": {
                                "path": "/srv/工作區/répertoire",
                                "variables": {"RÉGION": "eu-øst-1", "sufixo": "ñ"},
                                "protected_branches": ["main", "släpp/Ω"],
                            }
                        }
                    }
                ],
                id="non-ascii-names-paths-and-values",
            ),
            pytest.param(
                "credential-classified-key",
                [{"projects": {"p": {"path": "/p", "variables": {"GITHUB_TOKEN": "hunter2"}}}}],
                id="credential-key-elided-in-reply-verbatim-in-file",
            ),
            pytest.param(
                "nested-merge-then-deletion",
                [
                    {"projects": {"p": {"path": "/p", "limits": {"task_retry_limit": 4}}}},
                    {"projects": {"p": {"variables": {"region": "eu"}}}},
                    {"projects": {"p": {"limits": {"task_retry_limit": None}}}},
                    {"projects": {"p": None}},
                ],
                id="nested-merge-then-leaf-then-entry-deletion",
            ),
            pytest.param(
                "deleting-what-was-never-written",
                [{"limits": {"task_retry_limit": None}}, {"projects": None}],
                id="deleting-an-absent-key",
            ),
            pytest.param(
                "empty-patch-after-content",
                [{"limits": {"task_retry_limit": 7}}, {}],
                id="empty-patch-onto-an-existing-document",
            ),
        ],
    )
    def test_each_generated_shape_is_accepted_by_both_and_agrees(
        self, shape: str, patches: list[dict[str, Any]], tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        """Every shape the generators can draw, pinned as a named case.

        Non-vacuity for the generator: hypothesis chooses, so a shape it can draw
        may be drawn in no run of a 25-example loop, and a shape it can NO LONGER
        draw would silently narrow the property while the loop stayed green. Each
        of these fails if that shape stops being accepted by both surfaces or
        stops producing one file.
        """
        _drive_both_surfaces(patches, tmp_path_factory.mktemp(shape.replace("-", "")[:20]))

    @settings(max_examples=50, deadline=None)
    @given(patches=patch_sequences())
    def test_no_generated_patch_touches_a_fenced_path(self, patches: list[dict[str, Any]]) -> None:
        """The restriction the property carries, over the generated corpus.

        A sequence containing one config-only path would be refused on the MCP
        door and accepted on the route, so the equivalence test would fail for a
        reason that is not the property. Asserted over the generators rather than
        trusted from how they are written.
        """
        from kiro_crew.apps.builtins.spec_engine.engine.config import config_only_paths

        for patch in patches:
            assert config_only_paths(patch) == (), patch

    def test_the_generators_really_can_draw_the_shapes_the_docstring_claims(self) -> None:
        """The generator's reach, measured rather than described.

        The module docstring lists what the generators produce; if a strategy were
        edited into producing only scalar app-scope settings, every equivalence
        test above would keep passing on a narrower property and the docstring
        would be the only thing left claiming otherwise. ``hypothesis.find``
        searches for one example of each shape and raises when the strategy cannot
        produce it, which is the assertion this needs — the loops above choose
        their own examples and so cannot make this claim.
        """
        reaches = {
            "the empty patch": lambda seq: any(patch == {} for patch in seq),
            "a deletion": lambda seq: any(_contains_none(patch) for patch in seq),
            "a nested project entry": lambda seq: any(_has_project_entry(patch) for patch in seq),
            "non-ASCII text": lambda seq: any(_contains_non_ascii(patch) for patch in seq),
        }
        for shape, predicate in reaches.items():
            try:
                find(patch_sequences(), predicate)
            except NoSuchExample as exc:  # pragma: no cover - the failure path
                raise AssertionError(f"the generators can no longer draw {shape}") from exc


class TestTheFencedHalfIsWhereTheSurfacesDiverge:
    """The restriction, shown to be a real boundary rather than a hedge.

    If both surfaces accepted everything, "patches accepted by both" would be
    every patch and the property's scope statement would be noise. These two show
    the fence separating them, so the scope means something.
    """

    def test_the_fence_is_not_empty(self) -> None:
        """Pinned outside the parametrization below, because an empty
        parametrize SKIPS under this repo's pytest config (no
        ``empty_parameter_set_mark`` override) — it does not fail. A fence that
        emptied out would otherwise pass this class on nothing."""
        assert CONFIG_ONLY_PATHS

    @pytest.mark.parametrize("path", sorted(CONFIG_ONLY_PATHS))
    def test_the_fenced_paths_are_the_documented_ones(self, path: str) -> None:
        """Read from the engine, so a path added to the fence is covered here
        without an edit."""
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
