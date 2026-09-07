from copy import deepcopy
from pathlib import Path

import pytest

from flow_story_studio.engines.analyzer import analyze_story
from flow_story_studio.models import (
    AnalyzeRequest,
    AudioQCReport,
    ContinuityQCReport,
    ProductionAcceptance,
    QualityReport,
    VideoSettings,
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
    effective_render_prompt,
)
from flow_story_studio.render_queue import RenderQueue
from flow_story_studio.scene_contracts import seal_scene_contract
from flow_story_studio.storage import ProjectStorage

SCRIPT = """
SCENE 1 — ROOM — NIGHT
Alex crosses the room and stops beside the table.

SCENE 2 — ROOM — NIGHT — CONTINUOUS
Alex remains beside the same table and looks toward the door.
"""


def _single_scene():
    project = analyze_story(
        AnalyzeRequest(
            name="qc repair",
            original_text=SCRIPT,
            settings=VideoSettings(provider="test-renderer", video_model="test-model"),
        )
    )
    assert project.scenes
    return project, project.scenes[0]


def _mark_visual_failure(scene, code: str = "ACTION_CONSISTENCY_BELOW_THRESHOLD") -> None:
    scene.visual_qc = VisualQCReport(
        status="Failed",
        score=30,
        character_identity=100,
        location_identity=100,
        prop_consistency=100,
        wardrobe_consistency=100,
        lighting_consistency=100,
        action_consistency=30,
        composition_consistency=100,
        model_id="vision-test",
        issues=[VisualIssue(code=code, message="repairable")],
    )
    scene.audio_qc = AudioQCReport(
        status="Passed",
        score=100,
        audio_present=True,
        sample_rate_hz=48_000,
        channels=2,
        integrated_lufs=-16.0,
        true_peak_db=-1.0,
        model_id="ffmpeg-test",
    )
    scene.continuity_qc = ContinuityQCReport(status="NotApplicable", score=100)
    scene.acceptance = ProductionAcceptance(
        status="Rejected",
        score=30,
        reasons=["Visual QC: Failed"],
    )


def _mark_passed(scene) -> None:
    scene.quality = QualityReport()
    scene.render_provider = "test-renderer"
    scene.render_model = "test-model"
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
        first_frame="first.png",
        quarter_frame="quarter.png",
        middle_frame="middle.png",
        three_quarter_frame="three-quarter.png",
        last_frame="last.png",
        model_id="vision-test",
    )
    scene.audio_qc = AudioQCReport(
        status="Passed",
        score=100,
        audio_present=True,
        sample_rate_hz=48_000,
        channels=2,
        integrated_lufs=-16.0,
        true_peak_db=-1.0,
        model_id="ffmpeg-test",
    )
    scene.continuity_qc = (
        ContinuityQCReport(status="Passed", score=100, model_id="vision-test")
        if scene.visual_plan.dependency_mode == "direct"
        else ContinuityQCReport(status="NotApplicable", score=100)
    )
    scene.acceptance = ProductionAcceptance(status="Accepted", score=100)


def test_qc_repair_classifies_and_preserves_contract_prompt() -> None:
    project, scene = _single_scene()
    original_prompt = effective_render_prompt(scene)
    original_source = scene.source_text
    original_contract = scene.render_contract_hash

    _mark_visual_failure(scene)
    scene.render_attempt = 1
    categories = classify_qc_failures(scene)
    instruction = build_repair_instruction(scene)
    scene.runtime_repair_instruction = instruction
    repaired_prompt = effective_render_prompt(scene)

    assert "ACTION_ERROR" in categories
    assert instruction.startswith("TARGETED REPAIR PASS")
    assert "immutable Render Contract" in instruction
    assert repaired_prompt.startswith(original_prompt)
    assert "RUNTIME QC REPAIR INSTRUCTION" in repaired_prompt
    assert scene.source_text == original_source
    assert scene.render_contract_hash == original_contract


def test_qc_repair_budget_is_hard_capped() -> None:
    _project, scene = _single_scene()
    _mark_visual_failure(scene)

    scene.render_attempt = 1
    assert can_auto_retry(scene) is True
    scene.render_attempt = MAX_AUTO_RENDER_ATTEMPTS - 1
    assert can_auto_retry(scene) is True
    scene.render_attempt = MAX_AUTO_RENDER_ATTEMPTS
    assert can_auto_retry(scene) is False


def test_qc_repair_does_not_retry_infrastructure_failure() -> None:
    _project, scene = _single_scene()
    scene.visual_qc = VisualQCReport(
        status="Unavailable",
        issues=[VisualIssue(code="VISION_UNAVAILABLE", message="service unavailable")],
    )
    scene.audio_qc = AudioQCReport(status="Pending")
    scene.acceptance = ProductionAcceptance(status="Rejected", reasons=["Vision unavailable"])
    scene.render_attempt = 1

    assert can_auto_retry(scene) is False


@pytest.mark.asyncio
async def test_queue_retries_at_most_three_times_and_keeps_contract(
    tmp_path: Path,
) -> None:
    storage = ProjectStorage(tmp_path / "projects")
    project, scene = _single_scene()
    project.scenes = [scene]
    scene.image_plan.status = "Ready"
    original_source = scene.source_text
    original_action = scene.action
    original_dialogues = deepcopy(scene.dialogues)
    original_contract_hash = scene.contract_hash
    original_render_contract_hash = scene.render_contract_hash
    storage.save(project)

    calls: list[tuple[int, str, str]] = []

    class FakeRenderer:
        configured = True

        async def generate(self, _project, current):
            calls.append(
                (
                    current.render_attempt,
                    current.provider_job_id,
                    current.runtime_repair_instruction,
                )
            )
            return RenderResult(
                job_id=f"job-{len(calls)}",
                result_url="/video",
                result_file=f"renders/{project.id}/{current.id}/attempt-{len(calls)}.mp4",
            )

    registry = ProviderRegistry()
    registry.register("test-renderer", FakeRenderer())
    queue = RenderQueue(storage, provider_registry=registry)

    async def reject_every_attempt(_project, current):
        current.quality = QualityReport()
        _mark_visual_failure(current)

    queue._post_render_qc = reject_every_attempt  # type: ignore[method-assign]

    await queue.enqueue(project.id, [scene.id])
    await queue._queues[project.id].join()
    await queue.shutdown()

    latest = storage.get(project.id)
    assert latest is not None
    current = latest.scenes[0]
    assert len(calls) == MAX_AUTO_RENDER_ATTEMPTS
    assert [attempt for attempt, _job, _repair in calls] == [1, 2, 3]
    assert calls[0][2] == ""
    assert calls[1][1] == ""
    assert "TARGETED REPAIR PASS" in calls[1][2]
    assert calls[2][1] == ""
    assert "STRICT REPAIR PASS" in calls[2][2]
    assert current.render_attempt == MAX_AUTO_RENDER_ATTEMPTS
    assert current.status == "FailedQC"
    assert current.acceptance.status == "Rejected"
    assert len(current.repair_history) == MAX_AUTO_RENDER_ATTEMPTS - 1
    assert current.source_text == original_source
    assert current.action == original_action
    assert current.dialogues == original_dialogues
    assert current.contract_hash == original_contract_hash
    assert current.render_contract_hash == original_render_contract_hash


@pytest.mark.asyncio
async def test_direct_scene_waits_for_predecessor_repair_then_runs(tmp_path: Path) -> None:
    storage = ProjectStorage(tmp_path / "projects")
    project = analyze_story(
        AnalyzeRequest(
            name="dependency repair",
            original_text=SCRIPT,
            settings=VideoSettings(provider="test-renderer", video_model="test-model"),
        )
    )
    assert len(project.scenes) >= 2
    previous, current = project.scenes[:2]
    current.location_id = previous.location_id
    current.characters = list(previous.characters)
    current.start_state = deepcopy(previous.end_state)
    current.visual_plan.dependency_mode = "direct"
    previous.image_plan.status = "Ready"
    current.image_plan.status = "Ready"
    seal_scene_contract(current)
    project.scenes = [previous, current]
    storage.save(project)

    calls: list[str] = []
    previous_attempts = 0

    class FakeRenderer:
        configured = True

        async def generate(self, _project, scene):
            calls.append(scene.id)
            return RenderResult(
                job_id=f"job-{len(calls)}",
                result_url="/video",
                result_file=f"renders/{project.id}/{scene.id}/attempt-{len(calls)}.mp4",
            )

    registry = ProviderRegistry()
    registry.register("test-renderer", FakeRenderer())
    queue = RenderQueue(storage, provider_registry=registry)

    async def controlled_qc(_project, scene):
        nonlocal previous_attempts
        if scene.id == previous.id:
            previous_attempts += 1
            if previous_attempts == 1:
                scene.quality = QualityReport()
                _mark_visual_failure(scene)
                return
        _mark_passed(scene)

    queue._post_render_qc = controlled_qc  # type: ignore[method-assign]

    await queue.enqueue(project.id, [previous.id, current.id])
    await queue._queues[project.id].join()
    await queue.shutdown()

    latest = storage.get(project.id)
    assert latest is not None
    first, second = latest.scenes[:2]
    assert calls == [previous.id, previous.id, current.id]
    assert first.status == "Accepted"
    assert second.status == "Accepted"
    assert first.render_attempt == 2
    assert second.render_attempt == 1
    assert second.start_state == first.accepted_end_state


def test_qc_repair_never_spends_provider_retry_on_local_audio_drift() -> None:
    _project, scene = _single_scene()
    scene.visual_qc = VisualQCReport(status="Passed", score=100)
    scene.audio_qc = AudioQCReport(
        status="Failed",
        score=40,
        audio_present=True,
        issues=[VisualIssue(code="LOUDNESS_OUT_OF_RANGE", message="technical drift")],
    )
    scene.acceptance = ProductionAcceptance(
        status="Rejected",
        score=40,
        reasons=["Audio QC: Failed"],
    )
    scene.render_attempt = 1

    assert "AUDIO_ERROR" in classify_qc_failures(scene)
    assert can_auto_retry(scene) is False
