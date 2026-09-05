from flow_story_studio.engines.analyzer import analyze_story
from flow_story_studio.models import (
    AnalyzeRequest,
    ContinuityQCReport,
    ProductionAcceptance,
    QualityReport,
    VisualQCReport,
)
from flow_story_studio.production_gate import (
    is_scene_production_ready,
    scene_production_blockers,
)


SCRIPT = (
    "Alex bước vào căn phòng, đặt chiếc vé lên bàn rồi nhìn về phía cửa. "
    "Anh cầm lại chiếc vé và bước tới cửa sổ."
)


def _accepted_project():
    project = analyze_story(AnalyzeRequest(name="production gate", original_text=SCRIPT))
    scene = project.scenes[0]
    scene.status = "Accepted"
    scene.acceptance = ProductionAcceptance(status="Accepted", score=100)
    scene.quality = QualityReport()
    scene.visual_qc = VisualQCReport(
        status="Passed",
        score=100,
        character_identity=100,
        location_identity=100,
        prop_consistency=100,
        wardrobe_consistency=100,
        lighting_consistency=100,
        action_consistency=100,
        composition_consistency=100,
        model_id="mock",
    )
    scene.continuity_qc = ContinuityQCReport(status="NotApplicable", score=100)
    scene.result_file = "renders/scene.mp4"
    return project, scene


def test_unified_gate_accepts_complete_mock_evidence() -> None:
    project, scene = _accepted_project()

    assert scene_production_blockers(project, scene) == []
    assert is_scene_production_ready(project, scene)


def test_unified_gate_rejects_mutable_accepted_flag_when_visual_qc_failed() -> None:
    project, scene = _accepted_project()
    scene.visual_qc.status = "Failed"

    blockers = scene_production_blockers(project, scene)

    assert any("visual QC" in item for item in blockers)
    assert not is_scene_production_ready(project, scene)


def test_unified_gate_rejects_low_component_even_with_high_aggregate_score() -> None:
    project, scene = _accepted_project()
    scene.visual_qc.score = 99
    scene.visual_qc.action_consistency = 10

    blockers = scene_production_blockers(project, scene)

    assert any("action_consistency=10" in item for item in blockers)


def test_unified_gate_requires_direct_continuity_evidence() -> None:
    project, scene = _accepted_project()
    scene.visual_plan.dependency_mode = "direct"
    scene.continuity_qc = ContinuityQCReport(status="NotApplicable", score=100)

    blockers = scene_production_blockers(project, scene)

    assert any("direct continuity QC" in item for item in blockers)


def test_google_flow_gate_requires_visual_boundary_evidence() -> None:
    project, scene = _accepted_project()
    project.settings.provider = "google-flow"
    scene.visual_qc.model_id = "vision-model"

    blockers = scene_production_blockers(project, scene)

    assert any("boundary evidence" in item for item in blockers)
