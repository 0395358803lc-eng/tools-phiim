from flow_story_studio.engines.analyzer import analyze_story
from flow_story_studio.film.state_delta import commit_accepted_runtime_state
from flow_story_studio.models import (
    AnalyzeRequest,
    AudioQCReport,
    ContinuityQCReport,
    ProductionAcceptance,
    QualityReport,
    VideoSettings,
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
    project = analyze_story(
        AnalyzeRequest(
            name="production gate",
            original_text=SCRIPT,
            settings=VideoSettings(provider="mock"),
        )
    )
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
    scene.render_provider = project.settings.provider
    scene.render_model = project.settings.video_model
    commit_accepted_runtime_state(scene)
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


def test_unified_gate_rejects_render_settings_mismatch() -> None:
    project, scene = _accepted_project()
    scene.render_provider = "other-renderer"

    blockers = _gate().scene_production_blockers(project, scene)

    assert any("render provider" in item for item in blockers)
    assert not _gate().is_scene_production_ready(project, scene)

    scene.render_provider = project.settings.provider
    scene.render_model = "different-model"
    blockers = _gate().scene_production_blockers(project, scene)
    assert any("render model" in item for item in blockers)


def test_production_gate_rejects_mismatched_selected_vision_model() -> None:
    project, scene = _accepted_project()
    project.settings.provider = "production-renderer"
    project.settings.vision_model = "vision-selected"
    scene.render_provider = "production-renderer"
    scene.visual_qc.model_id = "vision-other"
    scene.visual_qc.first_frame = "first.png"
    scene.visual_qc.quarter_frame = "quarter.png"
    scene.visual_qc.middle_frame = "middle.png"
    scene.visual_qc.three_quarter_frame = "three-quarter.png"
    scene.visual_qc.last_frame = "last.png"

    blockers = _gate().scene_production_blockers(project, scene)

    assert any("visual QC model does not match" in item for item in blockers)

    scene.visual_plan.dependency_mode = "direct"
    scene.continuity_qc = ContinuityQCReport(
        status="Passed",
        score=100,
        character_match=100,
        location_match=100,
        wardrobe_match=100,
        prop_state_match=100,
        lighting_match=100,
        screen_direction_match=100,
        model_id="vision-other",
    )
    blockers = _gate().scene_production_blockers(project, scene)
    assert any("direct continuity QC model does not match" in item for item in blockers)


def test_unified_gate_requires_direct_continuity_evidence() -> None:
    project, scene = _accepted_project()
    scene.visual_plan.dependency_mode = "direct"
    scene.continuity_qc = ContinuityQCReport(status="NotApplicable", score=100)

    blockers = _gate().scene_production_blockers(project, scene)

    assert any("direct continuity QC" in item for item in blockers)


def test_production_renderer_gate_requires_visual_boundary_evidence() -> None:
    project, scene = _accepted_project()
    project.settings.provider = "production-renderer"
    scene.render_provider = "production-renderer"
    scene.visual_qc.model_id = "vision-model"

    blockers = _gate().scene_production_blockers(project, scene)

    assert any("five-frame evidence" in item for item in blockers)


def test_production_renderer_gate_requires_passing_audio_qc() -> None:
    project, scene = _accepted_project()
    project.settings.provider = "production-renderer"
    scene.render_provider = "production-renderer"
    scene.visual_qc.first_frame = "first.png"
    scene.visual_qc.quarter_frame = "quarter.png"
    scene.visual_qc.middle_frame = "middle.png"
    scene.visual_qc.three_quarter_frame = "three-quarter.png"
    scene.visual_qc.last_frame = "last.png"
    scene.visual_qc.model_id = "vision-model"
    scene.audio_qc = AudioQCReport(
        status="Failed",
        score=20,
        audio_present=True,
        sample_rate_hz=48_000,
        channels=2,
        integrated_lufs=-30.0,
        true_peak_db=0.0,
        clipping_detected=True,
        model_id="ffmpeg-ebur128",
    )
    scene.acceptance.score = 20

    blockers = _gate().scene_production_blockers(project, scene)

    assert any("audio QC is Failed" in item for item in blockers)
    assert any("audio true peak" in item for item in blockers)
    assert not _gate().is_scene_production_ready(project, scene)



def test_master_gate_uses_stricter_character_floor() -> None:
    project, _scene = _accepted_project()
    project.settings.vision_model = "vision-selected"
    reference = project.visual_bible.references[0]
    reference.entity_type = "character"
    reference.status = "approved"
    reference.approved_reference = "references/master.jpg"
    reference.vision_model = "vision-selected"
    reference.vision_score = 89
    reference.vision_issues = []

    blockers = _gate().master_reference_qc_blockers(project, reference)

    assert any("below 90" in item for item in blockers)


def test_master_gate_escalates_blocking_warning_code() -> None:
    from flow_story_studio.models import VisualIssue

    project, _scene = _accepted_project()
    project.settings.vision_model = "vision-selected"
    reference = project.visual_bible.references[0]
    reference.entity_type = "character"
    reference.status = "approved"
    reference.approved_reference = "references/master.jpg"
    reference.vision_model = "vision-selected"
    reference.vision_score = 95
    reference.vision_issues = [
        VisualIssue(
            code="background_contamination",
            severity="warning",
            message="Character is staged in a cinematic alley.",
        )
    ]

    blockers = _gate().master_reference_qc_blockers(project, reference)

    assert any("background_contamination" in item for item in blockers)


def test_project_master_gate_requires_physical_files(tmp_path) -> None:
    project, _scene = _accepted_project()
    project.settings.vision_model = "vision-selected"
    for index, reference in enumerate(project.visual_bible.references):
        reference.status = "approved"
        reference.vision_model = "vision-selected"
        reference.vision_score = 95
        reference.vision_issues = []
        relative = f"references/master-{index}.jpg"
        reference.approved_reference = relative
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"master")

    assert _gate().project_master_blockers(project, data_root=tmp_path) == []

    (tmp_path / project.visual_bible.references[0].approved_reference).unlink()
    blockers = _gate().project_master_blockers(project, data_root=tmp_path)
    assert any("approved image file is missing" in item for item in blockers)
