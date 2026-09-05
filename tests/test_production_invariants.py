from __future__ import annotations

from flow_story_studio.analysis_providers.merging import merge_analysis
from flow_story_studio.engines.analyzer import analyze_story
from flow_story_studio.models import AnalyzeRequest, VideoSettings

SCRIPT = """
TARGET RUNTIME: 32 seconds

CHARACTERS
- ALEX, adult man.
- MAYA, adult woman.

PROPS
- Brass key.
- Red notebook.

SCENE 1 — APARTMENT — NIGHT
Alex holds the brass key beside the door. Maya's voice through the phone says, "Do not leave."
Alex puts the brass key in his pocket.

SCENE 2 — HALLWAY — CONTINUOUS
Alex walks down the hallway. He pauses at the elevator.

SCENE 3 — OFFICE — NIGHT
Maya sits at a desk and opens the red notebook. Alex is not in the room.
"""


def _draft():
    return analyze_story(
        AnalyzeRequest(
            name="production invariants",
            original_text=SCRIPT,
            settings=VideoSettings(scene_duration=8),
        )
    )


def _scene_payload(scene, *, characters, props=None):
    prop_positions = props or {}
    return {
        "id": scene.id,
        "summary": scene.summary,
        "characters": characters,
        "location_id": scene.location_id,
        "action": scene.action,
        "camera": f"camera for {scene.id}",
        "lighting": f"lighting for {scene.id}",
        "atmosphere": f"atmosphere for {scene.id}",
        "voiceover": scene.voiceover,
        "dialogues": [item.model_dump() for item in scene.dialogues],
        "start_state": {
            "character_positions": {cid: "in frame" for cid in characters},
            "character_wardrobe": {cid: "locked" for cid in characters},
            "prop_positions": dict(prop_positions),
            "time": "night",
            "weather": "clear",
            "camera": "medium",
            "notes": "test",
        },
        "end_state": {
            "character_positions": {cid: "end frame" for cid in characters},
            "character_wardrobe": {cid: "locked" for cid in characters},
            "prop_positions": dict(prop_positions),
            "time": "night",
            "weather": "clear",
            "camera": "medium",
            "notes": "test",
        },
    }


def test_merge_enforces_canonical_visual_and_prop_invariants() -> None:
    draft = _draft()
    alex = next(item for item in draft.characters if item.name.casefold() == "alex")
    maya = next(item for item in draft.characters if item.name.casefold() == "maya")
    key = next(item for item in draft.props if "brass key" in item.name.casefold())
    notebook = next(item for item in draft.props if "red notebook" in item.name.casefold())

    scenes = []
    for scene in draft.scenes:
        source = scene.source_text.casefold()
        characters = ["CHAR_900", "CHAR_902", "CHAR_903"]
        props = {"PROP_900": "hallucinated", "PROP_902": "hallucinated"}
        if "hallway" in source:
            characters = ["CHAR_901", "CHAR_903"]
        if "office" in source:
            characters = ["CHAR_900", "CHAR_902"]
        payload = _scene_payload(scene, characters=characters, props=props)
        if "voice through the phone" in scene.source_text:
            payload["camera"] = "two-shot showing both characters across from each other"
        scenes.append(payload)

    data = {
        "characters": [
            {**alex.model_dump(), "id": "CHAR_900"},
            {**alex.model_dump(), "id": "CHAR_901", "face": "same Alex refined"},
            {**maya.model_dump(), "id": "CHAR_902"},
            {**maya.model_dump(), "id": "CHAR_903", "face": "same Maya refined"},
        ],
        "locations": [item.model_dump() for item in draft.locations],
        "props": [
            {**key.model_dump(), "id": "PROP_900"},
            {**key.model_dump(), "id": "PROP_901"},
            {**notebook.model_dump(), "id": "PROP_902"},
        ],
        "scenes": scenes,
    }

    project = merge_analysis(draft, data, "test-model")

    assert [item.name.casefold() for item in project.characters] == ["alex", "maya"]
    assert len(project.props) == 2

    remote = next(
        scene for scene in project.scenes if "voice through the phone" in scene.source_text
    )
    hallway = next(scene for scene in project.scenes if "hallway" in scene.source_text.casefold())
    office = next(scene for scene in project.scenes if "office" in scene.source_text.casefold())

    assert remote.characters == [alex.id]
    assert "two-shot" not in remote.camera.casefold()
    assert maya.id not in remote.start_state.character_positions
    assert set(remote.start_state.prop_positions) == {key.id}
    assert set(remote.end_state.prop_positions) == {key.id}

    assert hallway.characters == [alex.id]
    assert hallway.start_state.prop_positions == {}
    assert hallway.end_state.prop_positions == {}

    assert office.characters == [maya.id]
    assert alex.id not in office.start_state.character_positions
    assert set(office.start_state.prop_positions) == {notebook.id}
    assert set(office.end_state.prop_positions) == {notebook.id}

    assert len({scene.flow_prompt for scene in project.scenes}) == len(project.scenes)


def test_deterministic_analyzer_does_not_seed_every_prop_into_every_scene() -> None:
    project = _draft()
    key = next(item for item in project.props if "brass key" in item.name.casefold())
    notebook = next(item for item in project.props if "red notebook" in item.name.casefold())

    key_scenes = [scene for scene in project.scenes if key.id in scene.start_state.prop_positions]
    notebook_scenes = [
        scene for scene in project.scenes if notebook.id in scene.start_state.prop_positions
    ]
    hallway = next(scene for scene in project.scenes if "hallway" in scene.source_text.casefold())

    assert key_scenes
    assert all("brass key" in scene.source_text.casefold() for scene in key_scenes)
    assert notebook_scenes
    assert all("red notebook" in scene.source_text.casefold() for scene in notebook_scenes)
    assert hallway.start_state.prop_positions == {}


def test_finalization_rejects_ai_clock_drift_and_stale_voiceover_penalty() -> None:
    script = """
TARGET RUNTIME: 24 seconds

CHARACTERS
- ALEX, adult man, dark jacket.

SCENE 1 — STATION — NIGHT — PRESENT
Alex stands by the platform.

SCENE 2 — STREET — AFTERNOON — FLASHBACK
Alex remembers walking in the street.

SCENE 3 — STATION — NIGHT — PRESENT
Alex returns to the station and looks toward the exit.
"""
    draft = analyze_story(
        AnalyzeRequest(
            name="timeline source truth",
            original_text=script,
            settings=VideoSettings(scene_duration=8),
        )
    )
    alex = draft.characters[0]
    scenes = []
    for scene in draft.scenes:
        payload = _scene_payload(scene, characters=[alex.id])
        payload["voiceover"] = "AI invented narration that is not in the screenplay."
        payload["start_state"]["time"] = "18:30"
        payload["end_state"]["time"] = "18:45"
        scenes.append(payload)

    project = merge_analysis(
        draft,
        {
            "characters": [item.model_dump() for item in draft.characters],
            "locations": [item.model_dump() for item in draft.locations],
            "props": [item.model_dump() for item in draft.props],
            "scenes": scenes,
        },
        "test-model",
    )

    assert project.scenes[0].start_state.time.startswith("Present")
    assert project.scenes[1].start_state.time.startswith("Flashback")
    assert project.scenes[2].start_state.time.startswith("Present")
    assert all(scene.voiceover == "" for scene in project.scenes)
    assert all("Voiceover" not in item for item in project.continuity_warnings)
    assert project.continuity_score == 100
