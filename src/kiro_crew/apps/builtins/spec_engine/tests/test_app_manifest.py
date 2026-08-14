"""The app manifest, the discovery skill, and what registration actually places.

Three claims live here, and each has a way to fail loudly:

* The manifest declares the discovery skill and the Engine_MCP_Server, and
  declares no UI (the dashboard page is a later task, and a page declared before
  it exists is a broken sidebar entry).
* The skill routes to the tools and states no format rules. Enforced by
  comparing the skill against the engine's own guidance text rather than against
  a hand-typed list of forbidden words: a copied rule is caught because it
  matches the authored source, and the detector proves itself on a control
  sample so it cannot pass by silently matching nothing.
* Both registration paths place both resources. The builtin path reads the
  shipped package root; an installed app reads its own snapshot. Both are driven
  here against the same registrars the host uses, because a guarantee verified
  at one spelling while an equivalent second exists is how every shipped defect
  in this app began.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from kiro_crew.apps.builtins.spec_engine import readiness
from kiro_crew.apps.builtins.spec_engine.engine_mcp import TOOLS
from kiro_crew.apps.builtins.spec_engine.engine_mcp.guidance import GUIDANCE
from kiro_crew.apps.discovery import discover_builtin_apps
from kiro_crew.apps.execution import shipped_builtin_app_root
from kiro_crew.apps.lifecycle import LifecycleDispatcher
from kiro_crew.apps.manifest import AppManifest

APP_NAME = "spec-engine"
APP_ROOT = Path(readiness.__file__).parent
MANIFEST_PATH = APP_ROOT / "app.json"

#: How many consecutive words count as borrowed prose. Long enough that shared
#: vocabulary ("call `validate_spec`", "the spec directory") does not trip it,
#: short enough that a copied sentence cannot slip through by being reworded at
#: its edges.
_NGRAM = 6


@pytest.fixture()
def manifest_data() -> dict:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


@pytest.fixture()
def manifest() -> AppManifest:
    return AppManifest.from_json_file(MANIFEST_PATH)


def _skill_text() -> str:
    return (APP_ROOT / "skills" / "spec-engine-discovery" / "SKILL.md").read_text(encoding="utf-8")


def _frontmatter_and_body(text: str) -> tuple[str, str]:
    assert text.startswith("---\n"), "skill must open with YAML frontmatter"
    _, frontmatter, body = text.split("---\n", 2)
    return frontmatter, body


def _words(text: str) -> list[str]:
    """Normalize prose to comparable words, dropping markdown decoration."""
    keep = []
    for raw in text.lower().split():
        word = raw.strip("`*_#|-—:;,.()[]{}<>\"'!?/\\")
        if word:
            keep.append(word)
    return keep


def _ngrams(text: str, n: int = _NGRAM) -> set[tuple[str, ...]]:
    words = _words(text)
    return {tuple(words[i : i + n]) for i in range(len(words) - n + 1)}


def _guidance_ngrams() -> set[tuple[str, ...]]:
    grams: set[tuple[str, ...]] = set()
    for flow_text in GUIDANCE.values():
        grams |= _ngrams(flow_text)
    return grams


class TestManifest:
    def test_manifest_is_valid(self, manifest: AppManifest):
        assert manifest.validate(app_root=APP_ROOT) == []
        assert manifest.name == APP_NAME

    def test_declares_the_discovery_skill(self, manifest: AppManifest):
        assert manifest.skills == ["skills/spec-engine-discovery"]
        skill_dir = APP_ROOT / manifest.skills[0]
        assert (skill_dir / "SKILL.md").is_file()

    def test_declares_the_engine_mcp_server_as_a_stdio_command(self, manifest: AppManifest):
        assert list(manifest.mcpServers) == ["spec-engine"]
        cfg = manifest.mcpServers["spec-engine"]
        # stdio, not a URL: an HTTP entry without a live backend port is skipped
        # by the registrar, so it would never reach a session at all.
        assert "url" not in cfg
        assert cfg["command"] in {"python", "python3"}
        assert cfg["args"][0] == "-m"

    def test_the_declared_module_is_runnable_as_a_module(self, manifest: AppManifest):
        module = manifest.mcpServers["spec-engine"]["args"][1]
        parts = module.split(".")
        source = Path(*[Path(readiness.__file__).parents[4], *parts]).with_suffix(".py")
        assert source.is_file(), f"{module} does not resolve to a source file"
        # `python -m` runs the module body; without the guard the process exits
        # having registered nothing and the client sees the server vanish.
        assert '__name__ == "__main__"' in source.read_text(encoding="utf-8")

    def test_declares_no_ui(self, manifest_data: dict):
        # The dashboard page belongs to a later task. Declaring a route now puts
        # a sidebar entry in front of a page that does not exist.
        assert "ui" not in manifest_data

    def test_declares_no_auto_approve(self, manifest_data: dict):
        # Every tool goes through the host's approval gate. An autoApprove list
        # here would exempt them, which no requirement asks for.
        for cfg in manifest_data["mcpServers"].values():
            assert "autoApprove" not in cfg

    def test_startup_hook_resolves_to_the_readiness_reporter(self, manifest: AppManifest):
        # The manifest names a hook as a string; this is the host resolver that
        # turns that string into a callable for a builtin. A typo here means the
        # readiness state is never assessed and nothing says so.
        hook = manifest.backend.hooks.on_startup
        assert hook, "the app must declare a startup hook to assess readiness"
        resolved = LifecycleDispatcher._resolve_hook(APP_NAME, hook)
        assert resolved is readiness.on_startup


class TestDiscoverySkill:
    def test_triggers_cover_the_natural_spec_requests(self):
        frontmatter, _ = _frontmatter_and_body(_skill_text())
        triggers = ""
        for line in frontmatter.splitlines():
            if line.startswith("triggers:"):
                triggers = line.split(":", 1)[1].lower()
        assert triggers, "the skill must declare trigger phrases"
        phrases = [t.strip() for t in triggers.split(",") if t.strip()]
        for request in ("creating a spec", "planning a feature", "quick plan"):
            assert any(request in phrase for phrase in phrases), f"no trigger for {request!r}"
        # "fixing a bug as a spec" is phrased as an action; accept any trigger
        # that names both the bug and the spec.
        assert any(
            "bug" in phrase and "spec" in phrase for phrase in phrases
        ), "no trigger for fixing a bug as a spec"

    def test_directs_the_agent_to_the_guidance_tools(self):
        body = _frontmatter_and_body(_skill_text())[1]
        for tool in ("get_authoring_prompt", "get_orchestrator_prompt", "get_review_prompt"):
            assert tool in body, f"the skill never routes to {tool}"

    def test_names_no_tool_the_server_does_not_expose(self):
        # The other direction of the same drift: a skill naming a tool that was
        # renamed or never existed sends the agent to a dead call.
        body = _frontmatter_and_body(_skill_text())[1]
        for word in _words(body):
            if word.startswith(("get_", "validate_", "record_", "advance_", "list_")):
                assert word in TOOLS, f"skill names unknown tool {word!r}"

    def test_body_borrows_no_prose_from_the_engine_guidance(self):
        """The skill must point at the guidance, not restate it.

        A restated format rule becomes a second spelling that drifts from the
        engine that enforces it, and nothing notices. Overlapping prose is the
        observable form of that copy.
        """
        body = _frontmatter_and_body(_skill_text())[1]
        shared = _ngrams(body) & _guidance_ngrams()
        assert not shared, "skill body repeats engine guidance prose: " + "; ".join(
            " ".join(gram) for gram in sorted(shared)[:5]
        )

    def test_the_borrowed_prose_detector_actually_detects(self):
        """Guard against the check above passing because it can match nothing.

        An anchored comparison that matches no possible input is a clean run that
        proves nothing — the failure mode this project has hit repeatedly. Feed
        the detector real guidance text and require a hit.
        """
        control = GUIDANCE["feature"]
        assert _ngrams(control) & _guidance_ngrams()
        # And a body that quotes guidance is caught when passed through the same
        # comparison the real test makes.
        quoting_body = "Routing notes.\n\n" + "\n".join(control.splitlines()[:12])
        assert _ngrams(quoting_body) & _guidance_ngrams()

    def test_states_no_format_rule_the_guidance_owns(self):
        """No structural token whose definition belongs to the guidance.

        The tokens are EXTRACTED from the guidance rather than typed here, so the
        list cannot fall behind what the engine actually says.
        """
        body = _frontmatter_and_body(_skill_text())[1]
        tokens: set[str] = set()
        for flow_text in GUIDANCE.values():
            for line in flow_text.splitlines():
                stripped = line.strip()
                if stripped.startswith("#"):
                    tokens.add(stripped.lstrip("# ").strip())
                for word in stripped.split():
                    # EARS keywords and the native filenames: the two format
                    # vocabularies a router has no business restating.
                    if word.strip("`,.") in {"SHALL", "WHEN", "WHILE", "WHERE"}:
                        tokens.add(word.strip("`,."))
                    if word.strip("`,.") in {"requirements.md", "design.md", "tasks.md"}:
                        tokens.add(word.strip("`,."))
        # Non-vacuity: an extraction that produced nothing would leak nothing.
        assert "SHALL" in tokens and "requirements.md" in tokens
        leaked = sorted(t for t in tokens if t and t in body)
        assert not leaked, f"skill body states guidance-owned format tokens: {leaked}"


@pytest.fixture()
def app_home(tmp_path, monkeypatch):
    """An isolated data home, with the MCP map redirected out of the real one."""
    home = tmp_path / "kirocrew-home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))

    import kiro_crew.apps.bridges as bridges_mod
    import kiro_crew.apps.execution as execution_mod

    monkeypatch.setattr(bridges_mod, "_mcp_json_path", lambda: tmp_path / "mcp.json")
    monkeypatch.setattr(execution_mod, "third_party_execution_allowed", lambda: True)
    return home


def _register_from(root: Path, manifest: AppManifest):
    from kiro_crew.apps.bridges import _register_mcp_servers, _register_skills

    servers = _register_mcp_servers(APP_NAME, manifest)
    skills = _register_skills(APP_NAME, manifest, root)
    return skills, servers


class TestBothRegistrationPaths:
    """4.3's "for builtin and installed app paths alike", driven at both spellings."""

    def test_builtin_path_places_both_resources(self, app_home, manifest: AppManifest):
        # The builtin path registers from the IMMUTABLE package root — this is
        # what bridges._registration_source returns for a shipped app.
        shipped = shipped_builtin_app_root(APP_NAME)
        assert shipped == APP_ROOT, "the shipped manifest does not identify this package"

        skills, servers = _register_from(shipped, manifest)
        assert skills == [f"{APP_NAME}/spec-engine-discovery"]
        assert servers == [f"{APP_NAME}:spec-engine"]

        seen_skills, seen_servers = readiness.observe()
        assert readiness.assess(
            present_skills=seen_skills, present_servers=seen_servers
        ).ready is True

    def test_builtin_snapshot_keeps_both_declarations(self):
        """The builtin snapshot the host persists must not drop them.

        A builtin is discovered, converted to a dict, and that dict is what the
        installed record holds. A conversion that omitted skills or mcpServers
        would leave the app looking installed with nothing registered — the exact
        omission that has already shipped once for agents and skills.
        """
        discovered = {app["name"]: app for app in discover_builtin_apps()}
        assert APP_NAME in discovered, "the app is not discovered as a builtin"
        snapshot = discovered[APP_NAME]
        assert snapshot["skills"] == ["skills/spec-engine-discovery"]
        assert list(snapshot["mcpServers"]) == ["spec-engine"]
        assert snapshot["backend"]["hooks"]["on_startup"] == "readiness:on_startup"

    def test_installed_path_places_both_resources(self, app_home, tmp_path):
        # The installed path registers from the app's own snapshot directory. Same
        # registrars, different root — so a manifest that only works from the
        # package would fail here.
        installed_root = app_home / "apps" / APP_NAME
        installed_root.mkdir(parents=True)
        shutil.copy2(MANIFEST_PATH, installed_root / "app.json")
        shutil.copytree(APP_ROOT / "skills", installed_root / "skills")

        installed_manifest = AppManifest.from_json_file(installed_root / "app.json")
        assert installed_manifest.validate(app_root=installed_root) == []

        skills, servers = _register_from(installed_root, installed_manifest)
        assert skills == [f"{APP_NAME}/spec-engine-discovery"]
        assert servers == [f"{APP_NAME}:spec-engine"]

        seen_skills, seen_servers = readiness.observe()
        assert readiness.assess(
            present_skills=seen_skills, present_servers=seen_servers
        ).ready is True

    def test_a_registration_that_placed_nothing_is_observed_as_missing(self, app_home):
        # The case that must not pass: nothing registered at all. If `observe`
        # reported presence here, every readiness assertion above would be
        # meaningless.
        seen_skills, seen_servers = readiness.observe()
        assert seen_skills == set()
        assert seen_servers == set()
        verdict = readiness.assess(present_skills=seen_skills, present_servers=seen_servers)
        assert verdict.ready is False
        assert len(verdict.reasons) == 2

    def test_a_broken_skill_link_is_not_counted_as_present(self, app_home, manifest: AppManifest):
        # A symlink whose target is gone is the shape a stale registration leaves
        # behind. It must not read as registered.
        _register_from(APP_ROOT, manifest)
        link = app_home / "skills" / APP_NAME / "spec-engine-discovery"
        assert link.exists()
        link.unlink()
        link.symlink_to(app_home / "does-not-exist")

        seen_skills, seen_servers = readiness.observe()
        assert seen_skills == set()
        assert seen_servers == {"spec-engine"}
        verdict = readiness.assess(present_skills=seen_skills, present_servers=seen_servers)
        assert verdict.ready is False
        assert any("discovery skill" in reason for reason in verdict.reasons)
