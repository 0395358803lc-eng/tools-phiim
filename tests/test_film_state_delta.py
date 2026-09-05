import pytest

from flow_story_studio.engines.analyzer import analyze_story
from flow_story_studio.film.canonical import (
    CanonicalFilmModel,
    CharacterDelta,
    CharacterState,
    EntityLock,
    GlobalFilmState,
    PropDelta,
    PropState,
    StateDelta,
)
from flow_story_studio.film.state_delta import (
    StateDeltaEngine,
    clear_accepted_runtime_state,
    commit_accepted_runtime_state,
    continuity_state_hash,
    derive_scene_state_delta,
    direct_start_state,
)
from flow_story_studio.models import AnalyzeRequest


def _film_model() -> CanonicalFilmModel:
    return CanonicalFilmModel(
        film_id="film",
        characters={
            "CHAR_001": EntityLock(entity_id="CHAR_001", source_name="Alex"),
            "CHAR_002": EntityLock(entity_id="CHAR_002", source_name="Maya"),
        },
        locations={
            "LOC_001": EntityLock(entity_id="LOC_001", source_name="Station"),
            "LOC_002": EntityLock(entity_id="LOC_002", source_name="Hall"),
        },
        props={
            "PROP_001": EntityLock(entity_id="PROP_001", source_name="Ticket"),
        },
    )


def _state() -> GlobalFilmState:
    return GlobalFilmState(
        scene_number=1,
        scene_id="SCENE_001",
        active_location="LOC_001",
        character_states={
            "CHAR_001": CharacterState(
                entity_id="CHAR_001",
                location_id="LOC_001",
                position="bench",
                wardrobe_version="V1",
                possessions=["PROP_001"],
            )
        },
        prop_states={
            "PROP_001": PropState(
                entity_id="PROP_001",
                location_id="CHAR_001.hand",
                owner_id="CHAR_001",
                visibility="onscreen",
            )
        },
    )


def test_state_delta_applies_valid_character_prop_environment_audio_changes() -> None:
    engine = StateDeltaEngine(_film_model())
    current = _state()
    delta = StateDelta(
        source_scene_id="SCENE_002",
        target_scene=2,
        character_deltas=[
            CharacterDelta(
                entity_id="CHAR_001",
                field="position",
                old_value="bench",
                new_value="door",
            ),
            CharacterDelta(
                entity_id="CHAR_001",
                field="location",
                old_value="LOC_001",
                new_value="LOC_002",
            ),
        ],
        prop_deltas=[
            PropDelta(
                entity_id="PROP_001",
                field="owner",
                old_value="CHAR_001",
                new_value="CHAR_002",
            ),
            PropDelta(
                entity_id="PROP_001",
                field="location",
                old_value="CHAR_001.hand",
                new_value="CHAR_002.hand",
            ),
        ],
        active_location="LOC_002",
        timeline_branch="present",
        environment_changes={"weather": "rain", "lighting": "warm"},
        audio_changes={"ambience_id": "AMBIENCE-LOC_002-V1"},
    )

    verdict = engine.validate_delta(delta, current)
    assert verdict.is_valid
    updated = engine.apply_delta(delta, current)

    assert updated.scene_number == 2
    assert updated.scene_id == "SCENE_002"
    assert updated.active_location == "LOC_002"
    assert updated.character_states["CHAR_001"].position == "door"
    assert updated.character_states["CHAR_001"].location_id == "LOC_002"
    assert updated.prop_states["PROP_001"].owner_id == "CHAR_002"
    assert updated.prop_states["PROP_001"].location_id == "CHAR_002.hand"
    assert updated.environment.weather == "rain"
    assert updated.environment.lighting == "warm"
    assert updated.audio.ambience_id == "AMBIENCE-LOC_002-V1"
    assert current.active_location == "LOC_001"


def test_state_delta_rejects_unknown_entities_and_stale_values() -> None:
    engine = StateDeltaEngine(_film_model())
    current = _state()
    delta = StateDelta(
        source_scene_id="SCENE_002",
        target_scene=2,
        character_deltas=[
            CharacterDelta(
                entity_id="CHAR_001",
                field="position",
                old_value="wrong",
                new_value="door",
            ),
            CharacterDelta(
                entity_id="CHAR_UNKNOWN",
                field="position",
                new_value="door",
            ),
        ],
        prop_deltas=[
            PropDelta(
                entity_id="PROP_001",
                field="owner",
                new_value="CHAR_UNKNOWN",
            ),
            PropDelta(
                entity_id="PROP_UNKNOWN",
                field="location",
                new_value="table",
            ),
        ],
        active_location="LOC_UNKNOWN",
    )

    verdict = engine.validate_delta(delta, current)
    assert not verdict.is_valid
    joined = " | ".join(verdict.errors)
    assert "Stale character delta" in joined
    assert "Unknown character" in joined
    assert "Unknown owner" in joined
    assert "Unknown prop" in joined
    assert "Unknown active location" in joined
    with pytest.raises(ValueError):
        engine.apply_delta(delta, current)


def test_state_delta_rejects_stale_prop_value() -> None:
    engine = StateDeltaEngine(_film_model())
    current = _state()
    delta = StateDelta(
        source_scene_id="SCENE_002",
        target_scene=2,
        prop_deltas=[
            PropDelta(
                entity_id="PROP_001",
                field="location",
                old_value="table",
                new_value="pocket",
            )
        ],
    )
    verdict = engine.validate_delta(delta, current)
    assert not verdict.is_valid
    assert any("Stale prop delta" in item for item in verdict.errors)


def test_state_delta_rejects_unsupported_mutation_field() -> None:
    engine = StateDeltaEngine(_film_model())
    current = _state()
    delta = StateDelta(
        source_scene_id="SCENE_002",
        target_scene=2,
        character_deltas=[
            CharacterDelta(
                entity_id="CHAR_001",
                field="unsupported",
                new_value="x",
            )
        ],
    )
    with pytest.raises(ValueError, match="Unsupported character state field"):
        engine.apply_delta(delta, current)


def test_direct_start_state_is_deep_copy() -> None:
    current = _state()
    next_state = direct_start_state(current, "SCENE_002")
    assert next_state.scene_number == 2
    assert next_state.scene_id == "SCENE_002"
    next_state.character_states["CHAR_001"].position = "changed"
    assert current.character_states["CHAR_001"].position == "bench"


def test_runtime_acceptance_commit_clear_and_scene_delta() -> None:
    project = analyze_story(
        AnalyzeRequest(
            name="runtime-state",
            original_text=(
                "Alex enters the station holding a ticket, then walks to the door."
            ),
        )
    )
    scene = project.scenes[0]
    scene.acceptance.status = "Accepted"
    commit_accepted_runtime_state(scene)
    assert scene.accepted_end_state is not None
    assert scene.accepted_state_hash == continuity_state_hash(scene.accepted_end_state)

    delta = derive_scene_state_delta(scene)
    assert isinstance(delta, dict)

    clear_accepted_runtime_state(scene)
    assert scene.accepted_end_state is None
    assert scene.accepted_state_hash == ""


def test_runtime_state_cannot_commit_before_acceptance() -> None:
    project = analyze_story(
        AnalyzeRequest(
            name="runtime-state-reject",
            original_text="Alex enters the station holding a ticket and waits by the bench.",
        )
    )
    scene = project.scenes[0]
    with pytest.raises(ValueError, match="Production Acceptance"):
        commit_accepted_runtime_state(scene)
