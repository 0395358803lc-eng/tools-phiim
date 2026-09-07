import pytest

from flow_story_studio.engines.analyzer import analyze_story
from flow_story_studio.models import (
    AnalyzeRequest,
    AudioQCReport,
    ContinuityQCReport,
    ProductionAcceptance,
    QualityReport,
    VisualIssue,
    VisualQCReport,
)
from flow_story_studio.providers.base import RenderResult
from flow_story_studio.providers.registry import ProviderRegistry
from flow_story_studio.qc_repair import (
    MAX_AUTO_RENDER_ATTEMPTS,
    build_repair_instruction,
    can_auto_retry,
    classify_qc_failures,
)
from flow_story_studio.render_queue import RenderQueue
from flow_story_studio.storage import ProjectStorage

SCRIPT = (
    "Alex enters a station holding a blue ticket. "
    "He walks to the bench and keeps the ticket in his hand."
)


def _scene():
    project = analyze_story(AnalyzeRequest(name="repair", original_text=SCRIPT))
    return project, project.scenes[0]


def test_qc_failure_classification_and_budget() -> None:
    _project, scene = _scene()
    scene.render_attempt = 1
    scene.acceptance = ProductionAcceptance(status="Rejected")
    scene.visual_qc = VisualQCReport(
        status="Failed",
        issues=[
            VisualIssue(code="VISUAL_CHARACTER_IDENTITY_BELOW_THRESHOLD"),
            VisualIssue(code="VISUAL_PROP_CONSISTENCY_BELOW_THRESHOLD"),
        ],
    )
    scene.audio_qc = AudioQCReport(
        status="Failed",
        issues=[VisualIssue(code="LOUDNESS_OUT_OF_RANGE")],
    )

    categories = classify_qc_failures(scene)
    assert "IDENTITY_DRIFT" in categories
    assert "PROP_STATE_ERROR" in categories
    assert "AUDIO_ERROR" in categories
    # Local loudness drift is repairable without spending another provider generation.
    assert can_auto_retry(scene) is False

    instruction = build_repair_instruction(scene)
    assert instruction.startswith("TARGETED REPAIR PASS")
    assert "immutable Render Contract" in instruction

    scene.render_attempt = 2
    assert build_repair_instruction(scene).startswith("STRICT REPAIR PASS")
    scene.render_attempt = MAX_AUTO_RENDER_ATTEMPTS
    assert not can_auto_retry(scene)


def test_infrastructure_failure_is_not_auto_retried() -> None:
    _project, scene = _scene()
    scene.render_attempt = 1
    scene.acceptance = ProductionAcceptance(status="Rejected")
    scene.visual_qc = VisualQCReport(
        status="Unavailable",
        issues=[VisualIssue(code="VISION_UNAVAILABLE")],
    )
    assert not can_auto_retry(scene)


@pytest.mark.asyncio
async def test_queue_auto_repairs_once_then_accepts(tmp_path) -> None:
    storage = ProjectStorage(tmp_path / "projects")
    project = analyze_story(AnalyzeRequest(name="repair queue", original_text=SCRIPT))
    project.settings.provider = "test-renderer"
    scene = project.scenes[0]
    scene.image_plan.status = "Ready"
    storage.save(project)
    calls: list[str] = []

    class FakeRenderer:
        configured = True

        async def generate(self, current_project, current_scene):
            calls.append(current_scene.runtime_repair_instruction)
            relative = (
                f"renders/{current_project.id}/{current_scene.id}/attempt-"
                f"{current_scene.render_attempt}.mp4"
            )
            return RenderResult(
                job_id=f"job-{current_scene.render_attempt}",
                result_url="/video",
                result_file=relative,
            )

    registry = ProviderRegistry()
    registry.register("test-renderer", FakeRenderer())
    queue = RenderQueue(storage, provider_registry=registry)

    async def fake_post_render_qc(current_project, current_scene):
        current_scene.quality = QualityReport()
        current_scene.render_provider = current_project.settings.provider
        current_scene.render_model = current_project.settings.video_model
        if current_scene.render_attempt == 1:
            current_scene.visual_qc = VisualQCReport(
                status="Failed",
                score=40,
                character_identity=100,
                location_identity=100,
                prop_consistency=40,
                wardrobe_consistency=100,
                lighting_consistency=100,
                action_consistency=100,
                composition_consistency=100,
                issues=[VisualIssue(code="VISUAL_PROP_CONSISTENCY_BELOW_THRESHOLD")],
            )
            current_scene.audio_qc = AudioQCReport(
                status="Passed",
                score=100,
                audio_present=True,
                sample_rate_hz=48_000,
                channels=2,
                integrated_lufs=-16.0,
                true_peak_db=-1.0,
                model_id="fake",
            )
            current_scene.continuity_qc = ContinuityQCReport(
                status="NotApplicable", score=100
            )
            current_scene.acceptance = ProductionAcceptance(
                status="Rejected", score=40, reasons=["Visual QC: Failed"]
            )
            return

        current_scene.visual_qc = VisualQCReport(
            status="Passed",
            score=100,
            character_identity=100,
            location_identity=100,
            prop_consistency=100,
            wardrobe_consistency=100,
            lighting_consistency=100,
            action_consistency=100,
            composition_consistency=100,
            first_frame="first.jpg",
            quarter_frame="quarter.jpg",
            middle_frame="middle.jpg",
            three_quarter_frame="three-quarter.jpg",
            last_frame="last.jpg",
            model_id="fake",
        )
        current_scene.audio_qc = AudioQCReport(
            status="Passed",
            score=100,
            audio_present=True,
            sample_rate_hz=48_000,
            channels=2,
            integrated_lufs=-16.0,
            true_peak_db=-1.0,
            model_id="fake",
        )
        current_scene.continuity_qc = ContinuityQCReport(
            status="NotApplicable", score=100
        )
        current_scene.acceptance = ProductionAcceptance(
            status="Accepted", score=100
        )

    queue._post_render_qc = fake_post_render_qc  # type: ignore[method-assign]
    await queue.enqueue(project.id, [scene.id], force_rerender=True)
    await queue._workers[project.id]

    saved = storage.get(project.id)
    assert saved is not None
    saved_scene = saved.scenes[0]
    assert len(calls) == 2
    assert calls[0] == ""
    assert "prop" in calls[1].casefold()
    assert saved_scene.render_attempt == 2
    assert len(saved_scene.repair_history) == 1
    assert saved_scene.status == "Accepted"
    assert saved_scene.accepted_end_state is not None
    assert saved_scene.accepted_state_hash
    assert saved_scene.runtime_repair_instruction == ""
