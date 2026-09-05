from flow_story_studio.analysis_providers.merging import merge_analysis
from flow_story_studio.engines.analyzer import analyze_story
from flow_story_studio.models import AnalyzeRequest, VideoSettings


def test_camera_is_sanitized_when_ai_composition_conflicts_with_visible_cast() -> None:
    script = """
TARGET RUNTIME: 8 seconds

CHARACTERS
- ALEX, adult man.
- MAYA, adult woman.

SCENE 1 — APARTMENT — NIGHT
Alex stands alone by the window. Maya's voice through the phone says, "Stay there."
Maya is not in the apartment.
"""
    draft = analyze_story(
        AnalyzeRequest(
            name="camera cast invariant",
            original_text=script,
            settings=VideoSettings(scene_duration=8),
        )
    )
    alex = next(item for item in draft.characters if item.name.casefold() == "alex")
    maya = next(item for item in draft.characters if item.name.casefold() == "maya")
    scene = draft.scenes[0]
    data = {
        "characters": [item.model_dump() for item in draft.characters],
        "locations": [item.model_dump() for item in draft.locations],
        "props": [item.model_dump() for item in draft.props],
        "scenes": [
            {
                "id": scene.id,
                "summary": scene.summary,
                "characters": [alex.id, maya.id],
                "location_id": scene.location_id,
                "action": scene.action,
                "camera": "Two-shot of Alex and Maya facing each other across the room",
                "lighting": "soft practical light",
                "atmosphere": "tense",
                "voiceover": scene.voiceover,
                "dialogues": [item.model_dump() for item in scene.dialogues],
                "start_state": scene.start_state.model_dump(),
                "end_state": scene.end_state.model_dump(),
            }
        ],
    }

    project = merge_analysis(draft, data, "test-model")
    merged = project.scenes[0]

    assert merged.characters == [alex.id]
    assert "single-subject" in merged.camera
    assert "Maya" not in merged.camera
    assert any("camera sanitized" in warning for warning in merged.warnings)
