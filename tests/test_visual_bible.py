from copy import deepcopy

from flow_story_studio.engines.analyzer import analyze_story
from flow_story_studio.migrations import migrate_project_payload
from flow_story_studio.models import AnalyzeRequest, VideoSettings
from flow_story_studio.visual_bible import build_visual_bible

SCRIPT = """
TARGET RUNTIME: 24 seconds

CHARACTERS
- ALEX, adult man, dark coat.

PROPS
- Blue paper ticket.

SCENE 1 — STATION — NIGHT
Alex holds the blue paper ticket near the bench.

Alex steps closer to the bench and keeps holding the ticket.

SCENE 2 — STREET — NIGHT
Alex exits the station and walks into the rain.
"""


def _project():
    return analyze_story(
        AnalyzeRequest(
            name="visual bible",
            original_text=SCRIPT,
            settings=VideoSettings(scene_duration=8),
        )
    )


def test_visual_bible_contains_canonical_entity_locks_and_scene_plans() -> None:
    project = build_visual_bible(_project())
    refs = {item.entity_id: item for item in project.visual_bible.references}

    assert set(refs) == {
        *(item.id for item in project.characters),
        *(item.id for item in project.locations),
        *(item.id for item in project.props),
    }
    character = project.characters[0]
    assert character.name in refs[character.id].lock_text

    for scene in project.scenes:
        assert scene.visual_plan.location_reference_id
        assert scene.visual_plan.lock_prompt
        assert scene.visual_plan.anchor_scene_id
        assert scene.visual_plan.location_reference_id.startswith("VIS-")

    assert project.scenes[0].visual_plan.dependency_mode == "opening"
    assert "VISUAL BIBLE LOCKS:" in project.scenes[0].flow_prompt


def test_visual_plan_uses_direct_only_for_actual_direct_continuation() -> None:
    project = build_visual_bible(_project())
    direct_scenes = [
        scene for scene in project.scenes if scene.visual_plan.dependency_mode == "direct"
    ]
    canonical_scenes = [
        scene for scene in project.scenes if scene.visual_plan.dependency_mode == "canonical"
    ]
    assert all(scene.order > 1 for scene in direct_scenes)
    assert canonical_scenes
    for scene in canonical_scenes:
        assert scene.visual_plan.anchor_scene_id == scene.id


def test_dependency_notes_match_visual_plan_and_next_scene_boundary() -> None:
    project = build_visual_bible(_project())

    assert "Opening scene" in project.scenes[0].start_state.notes

    for index, scene in enumerate(project.scenes[1:], start=1):
        previous = project.scenes[index - 1]
        if scene.visual_plan.dependency_mode == "direct":
            assert "Direct continuation from" in scene.start_state.notes
            assert scene.id in previous.end_state.notes
            assert "direct continuation" in previous.end_state.notes.casefold()
            assert "may anchor" in previous.end_state.notes.casefold()
        else:
            assert scene.visual_plan.dependency_mode == "canonical"
            assert "Canonical cut/new beat" in scene.start_state.notes
            assert scene.id in previous.end_state.notes
            assert "canonical cut/new beat" in previous.end_state.notes.casefold()
            assert "may anchor" not in previous.end_state.notes.casefold()

    assert project.scenes[-1].end_state.notes == "Final scene; no downstream frame anchor."


def test_schema_v2_migrates_visual_bible_defaults() -> None:
    project = _project()
    payload = deepcopy(project.model_dump())
    payload["schema_version"] = 2
    payload.pop("visual_bible", None)
    for scene in payload["scenes"]:
        scene.pop("visual_plan", None)

    migrated = migrate_project_payload(payload)
    assert migrated["schema_version"] == 4
    assert migrated["visual_bible"] == {"version": 1, "references": []}
    assert all("visual_plan" in scene for scene in migrated["scenes"])
    assert all("visual_qc" in scene for scene in migrated["scenes"])
    assert all("continuity_qc" in scene for scene in migrated["scenes"])
    assert all("acceptance" in scene for scene in migrated["scenes"])
