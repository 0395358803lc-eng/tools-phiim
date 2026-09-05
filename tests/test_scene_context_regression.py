from flow_story_studio.analysis_providers.normalization import chain_scene_states
from flow_story_studio.engines.analyzer import analyze_story
from flow_story_studio.engines.continuity import is_direct_continuation
from flow_story_studio.engines.segmenter import SCENE_CONTEXT_PREFIX, narrative_text
from flow_story_studio.models import AnalyzeRequest, VideoSettings

SCREENPLAY = """## KỊCH BẢN CHI TIẾT
### CẢNH 1 – CĂN HỘ – ĐÊM
Minh ngồi trước laptop trong căn hộ. Điện thoại rung trên bàn.

### CẢNH 2 – QUÁN CÀ PHÊ – CHIỀU – MỘT NĂM TRƯỚC
Lan ngồi đối diện Minh trong quán cà phê. Hai người im lặng.

### FLASHBACK
Lan đứng dậy rời bàn.

### CẢNH 3 – CĂN HỘ – ĐÊM
Minh trở lại căn hộ và nhìn điện thoại.
"""


def test_scene_context_headings_are_preserved() -> None:
    cleaned = narrative_text(SCREENPLAY)
    assert f"{SCENE_CONTEXT_PREFIX}CẢNH 1 – CĂN HỘ – ĐÊM [END CONTEXT]" in cleaned
    assert (
        f"{SCENE_CONTEXT_PREFIX}CẢNH 2 – QUÁN CÀ PHÊ – CHIỀU – MỘT NĂM TRƯỚC "
        "[END CONTEXT]" in cleaned
    )
    assert f"{SCENE_CONTEXT_PREFIX}FLASHBACK [END CONTEXT]" in cleaned
    assert f"{SCENE_CONTEXT_PREFIX}CẢNH 3 – CĂN HỘ – ĐÊM [END CONTEXT]" in cleaned


def test_analyzer_uses_scene_context_to_switch_locations() -> None:
    project = analyze_story(
        AnalyzeRequest(name="Context", original_text=SCREENPLAY, settings=VideoSettings())
    )
    names = {location.id: location.name for location in project.locations}
    context_scenes = [
        scene for scene in project.scenes if scene.source_text.startswith(SCENE_CONTEXT_PREFIX)
    ]
    assert [names[scene.location_id] for scene in context_scenes[:3]] == [
        "Căn hộ",
        "Quán cà phê",
        "Quán cà phê",
    ]
    assert names[context_scenes[-1].location_id] == "Căn hộ"


def test_scene_cut_does_not_force_previous_end_state() -> None:
    project = analyze_story(
        AnalyzeRequest(name="Cuts", original_text=SCREENPLAY, settings=VideoSettings())
    )
    context_indexes = [
        index
        for index, scene in enumerate(project.scenes)
        if scene.source_text.startswith(SCENE_CONTEXT_PREFIX)
    ]
    assert len(context_indexes) >= 3
    second_index = context_indexes[1]
    assert second_index > 0
    assert project.scenes[second_index].start_state != project.scenes[second_index - 1].end_state


def test_xkiro_normalization_preserves_explicit_start_state() -> None:
    previous = {"time": "present", "camera": "wide"}
    ordered = [
        {
            "start_state": {"time": "one year earlier", "camera": "close"},
            "end_state": {"time": "one year earlier", "camera": "medium"},
        }
    ]
    chain_scene_states(ordered, previous)
    assert ordered[0]["start_state"]["time"] == "one year earlier"
    assert ordered[0]["start_state"]["camera"] == "close"


def test_dialogue_mentions_do_not_move_the_camera_location() -> None:
    screenplay = """## KỊCH BẢN CHI TIẾT
### CẢNH 1 – CĂN HỘ – ĐÊM
Minh ngồi cạnh điện thoại trong căn hộ.

### CẢNH 2 – LIÊN TỤC
Minh bật loa ngoài.
LAN
Em đang ngoài đường, cạnh một cửa hàng.
MINH
Anh vẫn ở đây và đang nghe em.
"""
    project = analyze_story(
        AnalyzeRequest(name="Dialogue location", original_text=screenplay, settings=VideoSettings())
    )
    names = {location.id: location.name for location in project.locations}
    context_scenes = [
        scene for scene in project.scenes if scene.source_text.startswith(SCENE_CONTEXT_PREFIX)
    ]
    assert len(context_scenes) >= 2
    assert names[context_scenes[0].location_id] == "Căn hộ"
    assert names[context_scenes[1].location_id] == "Căn hộ"


def test_return_from_flashback_restores_present_location() -> None:
    screenplay = """## KỊCH BẢN CHI TIẾT
### CẢNH 1 – CĂN HỘ – ĐÊM
Minh đứng trong căn hộ nhìn điện thoại.

### FLASHBACK
Đường phố ban đêm. Minh chạy giữa mưa.

### TRỞ LẠI HIỆN TẠI
Minh run rẩy và nhìn điện thoại trên bàn.
"""
    project = analyze_story(
        AnalyzeRequest(name="Flashback", original_text=screenplay, settings=VideoSettings())
    )
    names = {location.id: location.name for location in project.locations}
    context_scenes = [
        scene for scene in project.scenes if scene.source_text.startswith(SCENE_CONTEXT_PREFIX)
    ]
    assert [names[scene.location_id] for scene in context_scenes[:3]] == [
        "Căn hộ",
        "Đường phố",
        "Căn hộ",
    ]


def test_authored_continuous_heading_uses_direct_dependency() -> None:
    screenplay = """
TARGET RUNTIME: 16 seconds

CHARACTERS
- ALEX, adult man.

SCENE 1 — APARTMENT — NIGHT — PRESENT
Alex sits beside the table.

SCENE 2 — APARTMENT — NIGHT — CONTINUOUS
Alex stands and walks to the door.
"""
    project = analyze_story(
        AnalyzeRequest(
            name="continuous dependency",
            original_text=screenplay,
            settings=VideoSettings(scene_duration=8),
        )
    )
    context_scenes = [
        scene for scene in project.scenes if scene.source_text.startswith(SCENE_CONTEXT_PREFIX)
    ]
    assert len(context_scenes) == 2
    assert is_direct_continuation(context_scenes[0], context_scenes[1])
    assert context_scenes[1].visual_plan.dependency_mode == "direct"
    assert context_scenes[1].visual_plan.anchor_scene_id == context_scenes[0].id
