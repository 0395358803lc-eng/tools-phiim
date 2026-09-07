from flow_story_studio.analysis_providers.semantic_orchestrator import normalize_semantic_scene
from flow_story_studio.engines.analyzer import analyze_story
from flow_story_studio.models import AnalyzeRequest


def test_canonical_prop_color_repairs_local_ai_drift_without_touching_other_colors() -> None:
    script = """
CHARACTERS
- KHẢI, adult man.

PROPS
- Máy ghi âm nhỏ màu bạc, có đèn LED đỏ.

SCENE 1 — NHÀ GA — ĐÊM
Khải mặc áo khoác màu đen và cầm máy ghi âm nhỏ màu bạc.
"""
    project = analyze_story(AnalyzeRequest(name="prop color", original_text=script))
    scene = project.scenes[0]
    # This mirrors SCENE_021: the local beat mentions the recorder but omits its color,
    # while the global source declaration locks the recorder to silver.
    scene.source_text = "Khải cất máy ghi âm vào túi áo."
    scene.action = "Khải mặc áo khoác màu đen rồi cất máy ghi âm nhỏ màu đen vào túi."

    normalize_semantic_scene(
        scene,
        characters=project.characters,
        props=project.props,
        previous_scene=None,
    )

    assert "áo khoác màu đen" in scene.action
    assert "máy ghi âm nhỏ màu bạc" in scene.action
    assert "máy ghi âm nhỏ màu đen" not in scene.action
