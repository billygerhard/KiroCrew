"""The pipeline-stage tables: a total partition of the engine's own vocabularies.

The design states the property this file checks. Every setting the registry
declares and every capability the schema declares delegable reaches exactly one
pipeline stage, and the union of the stages equals those vocabularies with
nothing dropped, duplicated, or invented -- including for a vocabulary the engine
grows later, which is what the advanced-stage default is for.

The claims are asserted against the OWNING tables rather than against a literal
list written here. A second spelling of the setting registry or of the capability
list in this file would keep passing after the engine moved on, which is the
whole drift the projection exists to prevent.

The last class is a guard rather than a property: three of the five pipeline
stage names also name autonomy rungs, and the engine already carries three
phase-shaped mappings its own source warns must not be conflated. So the
separation is pinned here as a fact, not left to the comments asking for it.
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from kiro_crew.apps.builtins.spec_engine.engine.autonomy import AutonomyLevel
from kiro_crew.apps.builtins.spec_engine.engine.config import (
    AUTONOMY_LEVELS,
    CAPABILITY_STAGES,
    DELEGABLE_CAPABILITIES,
    ENGINE_FLOOR_CAPABILITIES,
    PIPELINE_STAGE_ADVANCED,
    PIPELINE_STAGE_EXECUTION,
    PIPELINE_STAGES,
    SETTING_GROUP_ORDER,
    SETTING_GROUP_STAGES,
    SETTINGS,
    capability_stage,
    setting_group_stage,
    stage_capabilities,
    stage_setting_groups,
)
from kiro_crew.apps.builtins.spec_engine.engine.config.settings import SETTING_GROUPS
from kiro_crew.apps.builtins.spec_engine.engine.prerequisites import (
    CAPABILITY_PHASES,
    STAGE_PHASES,
)
from kiro_crew.apps.builtins.spec_engine.engine.runs import PHASE_TIMEOUT_SETTINGS

#: Names the tables do not declare, drawn wide enough to include the shapes a
#: caller could plausibly pass: a stage name, a dotted key mistaken for a group,
#: the empty string.
_UNMAPPED = st.text(max_size=12).filter(
    lambda name: name not in SETTING_GROUP_STAGES and name not in CAPABILITY_STAGES
)


class TestEverySettingAndCapabilityReachesExactlyOneStage:
    """Property 1, over the vocabularies the engine actually declares."""

    def test_every_setting_group_the_registry_declares_is_placed(self) -> None:
        assert set(SETTING_GROUP_STAGES) == set(SETTING_GROUPS), (
            "the group-to-stage table and the setting registry disagree about "
            "which groups exist; an unplaced group still resolves, to the "
            "advanced stage, but placing it deliberately is the point of the table"
        )

    def test_every_delegable_capability_is_placed(self) -> None:
        assert set(CAPABILITY_STAGES) == set(DELEGABLE_CAPABILITIES)

    def test_no_engine_floor_capability_is_placed(self) -> None:
        """The floor is not bindable, so it is not a stage's content.

        Naming a floor capability in ``capabilities`` is a refusal rather than an
        ignored key. A surface listing one among a stage's bindable capabilities
        would offer a control whose every use the write door refuses.
        """
        assert set(CAPABILITY_STAGES).isdisjoint(ENGINE_FLOOR_CAPABILITIES)

    def test_each_group_appears_under_exactly_one_stage(self) -> None:
        placements = [
            (group, stage) for stage in PIPELINE_STAGES for group in stage_setting_groups(stage)
        ]
        placed = [group for group, _ in placements]
        assert sorted(placed) == sorted(set(placed)), f"a group appears twice: {placed}"
        assert set(placed) == set(SETTING_GROUP_ORDER)

    def test_each_capability_appears_under_exactly_one_stage(self) -> None:
        placements = [
            capability for stage in PIPELINE_STAGES for capability in stage_capabilities(stage)
        ]
        assert sorted(placements) == sorted(set(placements))
        assert set(placements) == set(DELEGABLE_CAPABILITIES)

    def test_the_union_of_the_stages_reaches_every_registered_setting(self) -> None:
        """Groups, not settings, are what the table places -- so the claim worth
        making is about the SETTINGS: every one of the 21 is reachable by walking
        the stages and expanding each group back into its keys."""
        reachable = {
            key
            for stage in PIPELINE_STAGES
            for group in stage_setting_groups(stage)
            for key, setting in SETTINGS.items()
            if setting.group == group
        }
        assert reachable == set(SETTINGS)

    def test_stage_contents_carry_no_name_the_engine_never_declared(self) -> None:
        """The invented half of the property. A stage listing a group the registry
        does not have would put a control in front of an operator whose every
        write the door rejects as an unknown key."""
        for stage in PIPELINE_STAGES:
            assert set(stage_setting_groups(stage)) <= set(SETTING_GROUPS)
            assert set(stage_capabilities(stage)) <= set(DELEGABLE_CAPABILITIES)

    def test_group_order_follows_the_registry_and_not_the_frozenset(self) -> None:
        """``SETTING_GROUPS`` is a frozenset, so grouping by it would reorder a
        surface's rows between two reads while nothing had changed. Each stage's
        groups must therefore run in registry declaration order."""
        for stage in PIPELINE_STAGES:
            positions = [SETTING_GROUP_ORDER.index(group) for group in stage_setting_groups(stage)]
            assert positions == sorted(positions), f"{stage} reordered its groups"

    def test_the_registry_order_tuple_is_the_frozensets_members_once_each(self) -> None:
        """The ordering source, checked against the set it orders: a duplicate or a
        missing member would silently drop or double a whole group of settings."""
        assert set(SETTING_GROUP_ORDER) == set(SETTING_GROUPS)
        assert len(SETTING_GROUP_ORDER) == len(SETTING_GROUPS)
        assert SETTING_GROUP_ORDER == tuple(
            dict.fromkeys(setting.group for setting in SETTINGS.values())
        )


class TestAStageAlwaysResolvesAndAlwaysResolvesToExactlyOne:
    """The same property over names the tables do not declare.

    This is the half that has to hold for a vocabulary the engine grows LATER: a
    setting added tomorrow under a group nobody placed must still be visible, and
    must be visible in exactly one place.
    """

    @given(name=_UNMAPPED)
    def test_an_unmapped_group_resolves_to_the_advanced_stage(self, name: str) -> None:
        assert setting_group_stage(name) == PIPELINE_STAGE_ADVANCED

    @given(name=_UNMAPPED)
    def test_an_unmapped_capability_resolves_to_the_advanced_stage(self, name: str) -> None:
        assert capability_stage(name) == PIPELINE_STAGE_ADVANCED

    @given(name=st.text(max_size=12))
    def test_a_group_resolves_to_one_declared_stage_whatever_it_is_called(self, name: str) -> None:
        """Never a raise, never an empty answer, never a stage outside the set.

        Raising would take the whole vocabulary read down over one unplaced group;
        answering nothing would hide a setting the write door still enforces.
        """
        assert setting_group_stage(name) in PIPELINE_STAGES

    @given(name=st.text(max_size=12))
    def test_a_capability_resolves_to_one_declared_stage_whatever_it_is_called(
        self, name: str
    ) -> None:
        assert capability_stage(name) in PIPELINE_STAGES

    @given(stage=st.text(max_size=12).filter(lambda name: name not in PIPELINE_STAGES))
    def test_an_unknown_stage_holds_nothing_rather_than_everything(self, stage: str) -> None:
        assert stage_setting_groups(stage) == ()
        assert stage_capabilities(stage) == ()

    def test_the_advanced_stage_is_one_of_the_declared_stages(self) -> None:
        """The precondition the two defaults above rest on: the fallback must be a
        stage a surface actually renders, or an unplaced setting would resolve to
        a panel that does not exist."""
        assert PIPELINE_STAGE_ADVANCED in PIPELINE_STAGES


class TestPipelineStagesAreNotTheAutonomyLadder:
    """Guarding the conflation the tables' own docstring warns about.

    Three of the five pipeline stage names -- authoring, execution, delivery --
    also name autonomy rungs, and the engine already carries three phase-shaped
    mappings. A pipeline stage grants nothing; an autonomy rung grants
    everything below it. These pin the separation so a later edit that fed one
    into the other fails here rather than in a run.
    """

    def test_the_stage_names_overlap_the_ladder_which_is_why_this_class_exists(self) -> None:
        shared = set(PIPELINE_STAGES) & set(AUTONOMY_LEVELS)
        assert shared, (
            "the names no longer overlap, so the conflation risk this class "
            "guards has changed shape; re-read engine/config/pipeline.py before "
            "deleting the class"
        )
        # And the vocabularies are still different sets: the ladder has no intake
        # rung and no advanced rung, and the stages have no integration rung.
        assert set(PIPELINE_STAGES) != set(AUTONOMY_LEVELS)

    def test_no_stage_table_value_is_an_autonomy_level(self) -> None:
        """``AutonomyLevel`` is deliberately not a ``str`` enum, so a table value
        that had drifted into holding a ladder member is visible as a type: a
        stage id is a plain string and stays one."""
        for value in (*SETTING_GROUP_STAGES.values(), *CAPABILITY_STAGES.values()):
            assert type(value) is str, f"{value!r} is not a plain stage id"
        assert PIPELINE_STAGE_ADVANCED not in {level.value for level in AutonomyLevel}

    def test_the_capability_table_is_not_a_copy_of_the_capability_phase_map(self) -> None:
        """Same keys, different answers -- so a caller cannot substitute one.

        ``CAPABILITY_PHASES`` puts ``model_catalog`` and ``watch_sources`` at the
        authoring RUNG because that is the lowest rung that can reach them. The
        pipeline table puts them elsewhere because the question is which part of
        the pipeline they govern.
        """
        assert set(CAPABILITY_STAGES) == set(CAPABILITY_PHASES)
        as_phases = {name: phase.value for name, phase in CAPABILITY_PHASES.items()}
        assert dict(CAPABILITY_STAGES) != as_phases

    def test_the_group_table_is_not_the_delivery_stage_phase_map(self) -> None:
        """``STAGE_PHASES`` is keyed by DELIVERY stage (isolate, submit, verify,
        publish, teardown), which is a different vocabulary from a setting
        group."""
        assert set(SETTING_GROUP_STAGES).isdisjoint(STAGE_PHASES)

    def test_the_group_table_is_not_the_run_state_timeout_map(self) -> None:
        """``PHASE_TIMEOUT_SETTINGS`` maps four run states onto four setting KEYS,
        so it is keyed by neither a group nor a stage. Every key it names lands in
        one single pipeline stage, which is what makes it a bound on executing a
        run rather than a grouping that rivals this one."""
        stages = {
            setting_group_stage(SETTINGS[key].group) for key in PHASE_TIMEOUT_SETTINGS.values()
        }
        assert stages == {PIPELINE_STAGE_EXECUTION}
        assert set(SETTING_GROUP_STAGES).isdisjoint(state.value for state in PHASE_TIMEOUT_SETTINGS)
