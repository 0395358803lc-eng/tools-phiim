from __future__ import annotations

from flow_story_studio.analysis_providers.merging import merge_analysis
from flow_story_studio.engines.analyzer import analyze_story
from flow_story_studio.models import AnalyzeRequest, VideoSettings


def test_semantic_orchestrator_rejects_ai_world_pollution_and_prop_cross_wiring() -> None:
    script = """
TARGET RUNTIME: 24 seconds

CHARACTERS
- ALEX, adult man.
- MAYA, adult woman.

PROPS
- Blue train ticket with torn corner.
- Silver recorder with red LED.

SCENE 1 — APARTMENT — NIGHT
Alex holds the blue train ticket. Maya's voice through the phone says, "Stay home."
Maya is not in the apartment.

SCENE 2 — STATION — NIGHT
Alex finds the silver recorder on a bench and presses play.

SCENE 3 — OFFICE — NIGHT
Maya sits alone at a desk. Alex is not in the office.
"""
    draft = analyze_story(
        AnalyzeRequest(
            name="semantic orchestration",
            original_text=script,
            settings=VideoSettings(scene_duration=8),
        )
    )
    alex = next(item for item in draft.characters if item.name.casefold() == "alex")
    maya = next(item for item in draft.characters if item.name.casefold() == "maya")
    ticket = next(item for item in draft.props if "ticket" in item.name.casefold())
    recorder = next(item for item in draft.props if "recorder" in item.name.casefold())

    polluted_characters = [item.model_dump() for item in draft.characters] + [
        {
            **alex.model_dump(),
            "id": "CHAR_900",
            "name": "walking",
        }
    ]
    polluted_props = [item.model_dump() for item in draft.props]
    scenes = []
    for scene in draft.scenes:
        payload = {
            "id": scene.id,
            "summary": scene.summary,
            "characters": [alex.id, maya.id, "CHAR_900"],
            "location_id": scene.location_id,
            "action": scene.action,
            "camera": (
                "Balanced multi-subject cinematic composition containing Alex, Maya and walking"
            ),
            "lighting": scene.lighting,
            "atmosphere": scene.atmosphere,
            "voiceover": scene.voiceover,
            "dialogues": [item.model_dump() for item in scene.dialogues],
            "start_state": scene.start_state.model_dump(),
            "end_state": scene.end_state.model_dump(),
        }
        # Deliberately attach recorder semantics to ticket ID and vice versa.
        payload["start_state"]["prop_positions"] = {
            ticket.id: "silver recorder on bench",
            recorder.id: "blue ticket in hand",
        }
        payload["end_state"]["prop_positions"] = dict(payload["start_state"]["prop_positions"])
        scenes.append(payload)

    project = merge_analysis(
        draft,
        {
            "characters": polluted_characters,
            "locations": [item.model_dump() for item in draft.locations],
            "props": polluted_props,
            "scenes": scenes,
        },
        "test-model",
    )

    assert [item.name.casefold() for item in project.characters] == ["alex", "maya"]
    assert all("walking" not in item.name.casefold() for item in project.characters)

    phone_scene = next(
        scene for scene in project.scenes if "through the phone" in scene.source_text
    )
    recorder_scene = next(
        scene for scene in project.scenes if "silver recorder" in scene.source_text.casefold()
    )
    office_scene = next(
        scene for scene in project.scenes if "office" in scene.source_text.casefold()
    )

    assert phone_scene.characters == [alex.id]
    assert maya.id not in phone_scene.start_state.character_positions
    assert set(phone_scene.start_state.prop_positions) == {ticket.id}
    assert "ticket" in phone_scene.start_state.prop_positions[ticket.id].casefold()

    assert set(recorder_scene.start_state.prop_positions) == {recorder.id}
    assert "recorder" in recorder_scene.start_state.prop_positions[recorder.id].casefold()

    assert office_scene.characters == [maya.id]
    assert alex.id not in office_scene.start_state.character_positions
    assert len({scene.camera for scene in project.scenes}) >= 2
    assert len({scene.flow_prompt for scene in project.scenes}) == len(project.scenes)
