from flow_story_studio.engines.analyzer import analyze_story
from flow_story_studio.models import (
    AnalyzeRequest,
    ContinuityQCReport,
    ProductionAcceptance,
    QualityReport,
    VisualQCReport,
)


def _gate():
    from flow_story_studio import production_gate

    return production_gate


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

    assert _gate().scene_production_blockers(project, scene) == []
    assert _gate().is_scene_production_ready(project, scene)


def test_unified_gate_rejects_mutable_accepted_flag_when_visual_qc_failed() -> None:
    project, scene = _accepted_project()
    scene.visual_qc.status = "Failed"

    blockers = _gate().scene_production_blockers(project, scene)

    assert any("visual QC" in item for item in blockers)
    assert not _gate().is_scene_production_ready(project, scene)


def test_unified_gate_rejects_low_component_even_with_high_aggregate_score() -> None:
    project, scene = _accepted_project()
    scene.visual_qc.score = 99
    scene.visual_qc.action_consistency = 10

    blockers = _gate().scene_production_blockers(project, scene)

    assert any("action_consistency=10" in item for item in blockers)


def test_acceptance_score_must_equal_strict_component_floor() -> None:
    project, scene = _accepted_project()
    scene.visual_qc.score = 98
    scene.visual_qc.action_consistency = 91
    scene.quality.score = 95

    assert _gate().scene_production_score_floor(scene) == 91

    blockers = _gate().scene_production_blockers(project, scene)
    assert any("strict component floor" in item for item in blockers)

    scene.acceptance.score = 91
    assert _gate().scene_production_blockers(project, scene) == []


def test_unified_gate_requires_direct_continuity_evidence() -> None:
    project, scene = _accepted_project()
    scene.visual_plan.dependency_mode = "direct"
    scene.continuity_qc = ContinuityQCReport(status="NotApplicable", score=100)

    blockers = _gate().scene_production_blockers(project, scene)

    assert any("direct continuity QC" in item for item in blockers)


def test_google_flow_gate_requires_visual_boundary_evidence() -> None:
    project, scene = _accepted_project()
    project.settings.provider = "google-flow"
    scene.visual_qc.model_id = "vision-model"

    blockers = _gate().scene_production_blockers(project, scene)

    assert any("boundary evidence" in item for item in blockers)
