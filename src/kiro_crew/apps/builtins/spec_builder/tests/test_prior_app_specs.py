"""A spec the PRIOR app authored must remain a usable artifact after the switch.

The prior app drove an embedded agent with a prompt: ``skills/spec-workflow/SKILL.md``
told it what to write. So that skill -- not anything composed here -- is the
authoritative description of the documents already sitting in users' projects, and
every fixture below is anchored to it by
:class:`TestTheFixtureIsThePriorAppsShapeNotTheEngines`. Without that anchor a
"prior-app spec" fixture is only a fixture, and asserting it still opens would
prove that a passing sample was written rather than that anything is compatible.

What the anchor exposes is the substantive finding: the two formats are NOT the
same. The prior prompt asked for ``# Requirements`` and a numbered list; the
engine's native format requires ``# Requirements Document``, an ``## Introduction``
section, ``### Requirement N:`` headings, a ``**User Story:**`` marker and
``#### Acceptance Criteria``. A prior-app document therefore does **not** pass
:mod:`.native_format`, and :class:`TestNativeValidationRejectsThePriorShape` pins
that with the specific rules rather than hiding it -- alongside the same call over
a native document, so the rejection is attributable to the format and not to a
mis-built call.

"Remains a valid artifact" is therefore the weaker and more useful guarantee these
tests hold the product to: the documents still OPEN, the app still discovers and
types the spec, and the engine's phase logic still JUDGES it -- reporting it as
not-yet-valid instead of crashing, losing it, or treating an unvalidated document
as approved. That has to include the artifact that was half-finished when the
switch happened, which is the direction nothing else covers.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew.apps.bridges import _register_skills
from kiro_crew.apps.builtins.spec_builder.backend import routes
from kiro_crew.apps.builtins.spec_engine.engine import native_format, phases, spec_types
from kiro_crew.apps.builtins.spec_engine.engine.documents import DocumentKind
from kiro_crew.apps.builtins.spec_engine.engine.state import SpecRef, StateStore
from kiro_crew.apps.manifest import AppManifest

from .test_routes import _READY_REQUIREMENTS

#: The prior app's own format definition, read from the shipped skill rather than
#: described here. If that skill is edited, the anchor test below fails and these
#: fixtures get revisited instead of quietly describing a format nothing produced.
_PRIOR_SKILL_PATH = (
    Path(routes.__file__).parents[1] / "skills" / "spec-workflow" / "SKILL.md"
)


def _prior_skill() -> str:
    return _PRIOR_SKILL_PATH.read_text(encoding="utf-8")


# ── the prior app's documents ────────────────────────────────────────────────
#
# Shaped by the skill's Phase 1-3 instructions: a short intro, a NUMBERED list of
# requirements, each a user story in the "As a <role>, I want <capability>, so
# that <benefit>" form followed by EARS-style acceptance bullets; a design with
# overview/architecture/error handling/testing; a checkbox task list citing
# `_Requirements:`. No native-format heading vocabulary, because the prompt that
# produced these documents never mentioned any.

_PRIOR_REQUIREMENTS = """# Requirements

Add Google login so users no longer need passwords.

1. **User story:** As a user, I want to sign in with Google, so that I don't need a password.
   - WHEN a user clicks "Sign in with Google" THE SYSTEM SHALL redirect to the consent screen.
   - IF the token exchange fails THEN the system SHALL show a retry message.

2. **User story:** As an operator, I want failed logins recorded, so that I can debug them.
   - WHEN a login fails THE SYSTEM SHALL write an audit entry.
"""

_PRIOR_DESIGN = """# Design

## Overview

A new OAuth callback route exchanges the authorization code for a token.

## Architecture / components

* `auth/google.py` -- the token exchange.
* `routes.py` -- the callback endpoint.

## Error handling

A failed exchange returns a retryable error and records an audit entry.

## Testing strategy

Unit tests for the exchange; one integration test for the callback.
"""

_PRIOR_TASKS = """# Tasks

- [ ] 1. Add the OAuth callback route
  - Wire `/auth/google/callback` and verify with a unit test.
  - _Requirements: 1.1_
- [ ] 2. Record failed logins
  - _Requirements: 2.1_
"""

#: The structured-state sidecar the prior skill marks REQUIRED. Written by every
#: prior-app spec, and read by nothing in the engine.
_PRIOR_SIDECAR = {
    "decisions": [
        {
            "id": "transport",
            "title": "Inbound transport",
            "options": ["Hosted HTTPS listener", "Streaming extensions"],
            "recommended": "Hosted HTTPS listener",
            "answer": "Hosted HTTPS listener",
        }
    ],
    "blocking": None,
    "context": {"template": "webex"},
}


def _prior_spec(root: Path, name: str, *, documents: dict[str, str], sidecar: dict | None) -> Path:
    """Write one spec exactly as the prior app left it on disk."""
    spec_dir = root / ".kiro" / "specs" / name
    spec_dir.mkdir(parents=True, exist_ok=True)
    for filename, text in documents.items():
        (spec_dir / filename).write_text(text, encoding="utf-8")
    if sidecar is not None:
        (spec_dir / ".spec-state.json").write_text(json.dumps(sidecar), encoding="utf-8")
    return spec_dir


@pytest.fixture()
def complete(tmp_path: Path) -> Path:
    """A finished prior-app feature spec: all three documents plus the sidecar."""
    return _prior_spec(
        tmp_path,
        "google-login",
        documents={
            "requirements.md": _PRIOR_REQUIREMENTS,
            "design.md": _PRIOR_DESIGN,
            "tasks.md": _PRIOR_TASKS,
        },
        sidecar=_PRIOR_SIDECAR,
    )


@pytest.fixture()
def quick(tmp_path: Path) -> Path:
    """The prior app's ``quick`` type, which SKIPS design.md by instruction."""
    return _prior_spec(
        tmp_path,
        "rename-button",
        documents={
            "requirements.md": "# Requirements\n\nGoal: the save button should read Save.\n",
            "tasks.md": "- [ ] 1. Rename the button\n  - _Requirements: 1.1_\n",
        },
        sidecar={"blocking": None},
    )


@pytest.fixture()
def mid_phase(tmp_path: Path) -> Path:
    """Caught mid-sentence: the agent was still drafting when the switch happened."""
    return _prior_spec(
        tmp_path,
        "half-written",
        documents={"requirements.md": "# Requirements\n\nI was still writing this when\n"},
        sidecar={"blocking": "waiting on the transport decision"},
    )


@pytest.fixture()
def placeholder(tmp_path: Path) -> Path:
    """The emptiest artifact the prior app could leave: a touched placeholder."""
    return _prior_spec(
        tmp_path, "just-started", documents={"requirements.md": ""}, sidecar=None
    )


class TestTheFixtureIsThePriorAppsShapeNotTheEngines:
    """Anchor the fixtures to the prior app's own prompt.

    A fixture written to pass proves only that it was written to pass. These
    assertions fail if the documents above drift toward the native format, or if
    the skill they are derived from changes underneath them.
    """

    def test_the_prior_skill_is_still_the_shipped_format_definition(self):
        skill = _prior_skill()
        assert "As a <role>, I want <capability>, so that <benefit>" in skill
        assert "_Requirements: 1.2, 3.1_" in skill
        assert "A numbered list of requirements" in skill
        # The sidecar this app's specs carry, declared REQUIRED by that prompt.
        assert ".spec-state.json" in skill

    def test_the_prior_prompt_never_named_the_native_heading_vocabulary(self):
        """The reason the two formats differ, stated against the source."""
        skill = _prior_skill()
        for native_only in (
            "# Requirements Document",
            "### Requirement",
            "#### Acceptance Criteria",
            "**User Story:**",
            "## Introduction",
        ):
            assert native_only not in skill, (
                f"the prior prompt does mention {native_only!r}; these fixtures "
                "describe the wrong prior format"
            )

    def test_the_fixtures_use_the_prior_vocabulary_and_not_the_native_one(self):
        for text in (_PRIOR_REQUIREMENTS, _PRIOR_DESIGN, _PRIOR_TASKS):
            for native_only in (
                "# Requirements Document",
                "### Requirement ",
                "#### Acceptance Criteria",
                "**User Story:**",
            ):
                assert native_only not in text
        # And they DO carry the prior shape: a numbered requirement and a story.
        assert "1. **User story:** As a user" in _PRIOR_REQUIREMENTS
        assert "_Requirements: 1.1_" in _PRIOR_TASKS

    def test_the_native_comparison_document_really_is_native(self):
        """Non-vacuity for every comparison below that leans on this constant."""
        assert _READY_REQUIREMENTS.startswith("# Requirements Document")
        assert "**User Story:**" in _READY_REQUIREMENTS


class TestAPriorAppSpecStillOpens:
    """The documents are readable through the engine's single read path."""

    def test_every_document_the_prior_app_wrote_opens(self, complete: Path):
        for kind in DocumentKind:
            text = phases.read_document(complete, kind)
            assert text, f"{kind.filename} did not open"
        assert "Add Google login" in (phases.read_document(complete, DocumentKind.REQUIREMENTS) or "")

    def test_the_app_discovers_it_as_a_spec(self, complete: Path):
        assert routes._looks_like_a_spec(complete) is True

    def test_it_is_typed_by_fallback_because_the_engines_sidecar_is_absent(self, complete: Path):
        """A prior-app spec carries ``.spec-state.json`` but no ``.config.kiro``.

        Pinned in both directions: the engine genuinely has no record to read, and
        the app answers anyway rather than propagating the error to a surface.
        """
        assert not (complete / ".config.kiro").exists()
        with pytest.raises(spec_types.SpecTypeError):
            spec_types.recorded_spec_type(complete)
        assert routes._discovered_spec_type(complete) == "feature"

    def test_the_surface_opens_it_and_still_reads_the_prior_sidecar(self, complete: Path):
        """``.spec-state.json`` is the prior app's file, and it is not lost.

        The engine never reads it; this surface does, so the decisions and the
        blocking sentence a prior spec recorded still render.
        """
        phase, files, state = routes._collect_spec_documents(complete)
        assert phase.phase == "tasks"  # furthest drafted document wins
        assert set(files) >= {"requirements.md", "design.md", "tasks.md"}
        assert "Add Google login" in files["requirements.md"]
        assert state is not None
        assert state["decisions"][0]["answer"] == "Hosted HTTPS listener"
        assert state["context"]["template"] == "webex"


class TestNativeValidationRejectsThePriorShape:
    """The compatibility limit, pinned rather than papered over."""

    def test_a_prior_app_document_does_not_pass_the_native_validator(self, complete: Path):
        report = native_format.validate_documents(
            [complete / kind.filename for kind in DocumentKind]
        )
        assert report.ok is False
        rules = set(native_format.iter_rule_ids(report))
        # The prior prompt asked for `# Requirements`, not `# Requirements Document`,
        # and for a numbered list in place of the native sections.
        assert "native.document.title-mismatch" in rules
        assert "native.section.missing" in rules

    def test_the_same_call_passes_a_native_document(self, tmp_path: Path):
        """So the rejection above is the format, not a mis-built call.

        Without this the assertion would also pass if ``validate_documents`` were
        being handed paths it could never accept.
        """
        native_dir = tmp_path / "native"
        native_dir.mkdir()
        (native_dir / "requirements.md").write_text(_READY_REQUIREMENTS, encoding="utf-8")
        report = native_format.validate_documents([native_dir / "requirements.md"])
        assert report.ok is True
        assert list(native_format.iter_rule_ids(report)) == []


class TestTheEnginesPhaseLogicStillJudgesIt:
    """A prior-app spec is judged, not crashed on, and never assumed approved."""

    @staticmethod
    def _state(tmp_path: Path, spec_dir: Path):
        store = StateStore(tmp_path / "engine.db")
        project = spec_dir.parents[2]  # <project>/.kiro/specs/<name>
        return phases.derive_phase(store, SpecRef.of(project, spec_dir.name))

    def test_a_complete_prior_spec_derives_its_furthest_phase(
        self, tmp_path: Path, complete: Path
    ):
        state = self._state(tmp_path, complete)
        assert state.phase is not None

    def test_execution_is_refused_rather_than_permitted(self, tmp_path: Path, complete: Path):
        """The documents were never validated or approved under the engine.

        The prior app executed on the existence of ``tasks.md``. The engine must
        not inherit that: a prior spec's task list is not an approval.
        """
        state = self._state(tmp_path, complete)
        reasons, _report = phases.execution_blocking_reasons(state)
        assert reasons, "a never-approved prior-app spec was cleared for execution"

    @pytest.mark.parametrize("fixture", ["complete", "quick", "mid_phase", "placeholder"])
    def test_every_prior_shape_is_judged_without_raising(
        self, tmp_path: Path, fixture: str, request: pytest.FixtureRequest
    ):
        """Including the two nothing else covers: mid-phase, and the placeholder.

        "Remains a valid artifact" has to hold for the spec that was half-finished
        when the switch happened, so each shape is driven through the same
        derivation and the same execution gate.
        """
        spec_dir = request.getfixturevalue(fixture)
        state = self._state(tmp_path, spec_dir)
        reasons, _report = phases.execution_blocking_reasons(state)
        assert reasons, f"{fixture} was cleared for execution"


class TestTheHalfFinishedArtifact:
    """The direction the switch actually leaves behind."""

    def test_a_mid_phase_spec_opens_and_keeps_what_it_had(self, mid_phase: Path):
        phase, files, state = routes._collect_spec_documents(mid_phase)
        assert phase.phase == "requirements"
        assert "I was still writing this when" in files["requirements.md"]
        # The unanswered question survives the switch, which is the whole value of
        # the sidecar for a spec caught mid-conversation.
        assert state is not None and state["blocking"] == "waiting on the transport decision"

    def test_a_mid_phase_spec_is_still_discovered(self, mid_phase: Path):
        assert routes._looks_like_a_spec(mid_phase) is True
        assert routes._discovered_spec_type(mid_phase) == "feature"

    def test_a_placeholder_document_counts_as_absent_not_as_drafted(self, placeholder: Path):
        """An empty file is not a drafted document.

        Treating it as one would derive a phase past work that never happened --
        the reason ``read_document`` reads whitespace as absent.
        """
        assert phases.read_document(placeholder, DocumentKind.REQUIREMENTS) is None
        assert routes._looks_like_a_spec(placeholder) is True  # the file IS there

    def test_a_placeholder_spec_opens_without_raising(self, placeholder: Path):
        phase, files, state = routes._collect_spec_documents(placeholder)
        assert phase is not None
        assert files.get("requirements.md") == ""
        assert state is None  # no sidecar was ever written


class TestThePriorFormatPromptNoLongerReachesASession:
    """One format authority, made structurally true rather than documented.

    The prior app's prompt is a second, DISAGREEING statement of the spec format --
    the tests above prove documents written to it are refused by the engine's
    validator. While the manifest declared it, the host linked it into every
    user's ``~/.kiro/crew/skills`` and any session could load it by trigger, so an
    agent could be instructed to write documents the engine would then reject.
    Nothing referenced it: ``_seed_prompt`` is deliberately self-contained, and
    ``test_seed_prompt_is_self_contained_and_type_aware`` pins that. So it was a
    registered second spelling with no caller.

    Undeclaring it is what removes it: registration reads the manifest's ``skills``
    list, so a skill absent from that list is never linked and cannot be loaded.
    The FILE stays in the repo on purpose -- it is the authoritative record of what
    the specs already on users' disks look like, and the anchor the compatibility
    fixtures above depend on.
    """

    APP_NAME = "spec-builder"

    @property
    def _manifest_path(self) -> Path:
        return Path(routes.__file__).parents[1] / "app.json"

    def test_the_manifest_declares_no_skill_at_all(self):
        manifest = AppManifest.from_json_file(self._manifest_path)
        assert manifest.skills == []
        assert manifest.validate(app_root=self._manifest_path.parent) == []

    def test_registration_places_no_skill_for_this_app(self, tmp_path, monkeypatch):
        """Driven through the host's own registrar, not a reading of the JSON.

        A manifest field can look right while the registrar still places a
        directory from somewhere else; this asserts the destination is empty.
        """
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
        root = self._manifest_path.parent
        manifest = AppManifest.from_json_file(self._manifest_path)

        placed = _register_skills(self.APP_NAME, manifest, root)

        assert placed == []

    def test_the_same_registrar_does_place_a_declared_skill(self, tmp_path, monkeypatch):
        """Non-vacuity: otherwise "places nothing" passes for a broken registrar.

        The engine app declares one, so the identical call over its manifest must
        place it. Without this, the assertion above would also hold if
        ``_register_skills`` had stopped working entirely.
        """
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
        engine_root = Path(spec_types.__file__).parents[1]
        engine_manifest = AppManifest.from_json_file(engine_root / "app.json")

        placed = _register_skills("spec-engine", engine_manifest, engine_root)

        assert placed == ["spec-engine/spec-engine-discovery"]

    def test_the_prior_definition_is_kept_as_the_record_of_prior_specs(self):
        """Retired, not deleted: the compatibility fixtures are anchored to it."""
        assert _PRIOR_SKILL_PATH.is_file()

    def test_the_retired_prompt_really_did_state_the_format(self):
        """Why it had to stop being registered, asserted against its own text.

        If this prompt stated no format rules it would have been harmless to
        declare, and undeclaring it would be churn rather than a fix.
        """
        skill = _prior_skill()
        for owned_by_the_engine in ("requirements.md", "tasks.md", "SHALL", "_Requirements:"):
            assert owned_by_the_engine in skill
