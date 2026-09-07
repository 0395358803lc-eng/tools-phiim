from flow_story_studio.engines.analyzer import analyze_story
from flow_story_studio.models import AnalyzeRequest

SCRIPT = """
CHARACTERS
- KHAI, adult man.

PROPS
- Máy ghi âm nhỏ màu bạc.

SCENE 1 — SÂN GA — ĐÊM
Trời mưa nhẹ. Khai nghe thấy tiếng click rất nhỏ trên ghế kim loại.
Điện thoại của Khai rung.

SCENE 2 — SÂN GA — ĐÊM — CONTINUOUS
Khai bấm PLAY trên máy ghi âm. Một đoàn tàu chạy qua phía sau.
"""


def test_scene_audio_plan_is_source_grounded_and_embedded_in_render_contract() -> None:
    project = analyze_story(AnalyzeRequest(name="scene audio plan", original_text=SCRIPT))
    plans = [
        scene.orchestration["audio_locks"]["scene_audio_plan"]
        for scene in project.scenes
    ]
    effects = [
        effect
        for plan in plans
        for effect in plan["diegetic_effects"]
    ]

    assert any("rain ambience" in item for item in effects)
    assert "small mechanical click" in effects
    assert "phone vibration" in effects
    assert any("recorder PLAY control" in item for item in effects)
    assert any("passing train" in item for item in effects)
    assert any("Direct audio continuation" in plan["continuity"] for plan in plans[1:])

    for scene, plan in zip(project.scenes, plans, strict=True):
        assert scene.render_contract["audio_plan"] == plan
        assert "SCENE AUDIO PLAN:" in scene.render_prompt
        assert "unintentionally silent clip" in scene.render_prompt


def test_scene_without_explicit_sfx_still_gets_natural_ambience_not_invented_speech() -> None:
    project = analyze_story(
        AnalyzeRequest(
            name="ambient only",
            original_text="""
CHARACTERS
- MAYA, adult woman.

SCENE 1 — ROOM — NIGHT
Maya stands by the window and looks outside.
""",
        )
    )
    scene = project.scenes[0]
    plan = scene.orchestration["audio_locks"]["scene_audio_plan"]

    assert plan["ambience"]
    assert plan["speech"] == []
    assert plan["silence_required"] is False
    assert "never invent speech" in plan["generation_instruction"]
    assert scene.orchestration["audio_locks"]["no_unrequested_speech"] is True
