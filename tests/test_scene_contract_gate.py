from pathlib import Path

import pytest

from flow_story_studio.engines.analyzer import analyze_story
from flow_story_studio.models import AnalyzeRequest, SceneUpdate
from flow_story_studio.render_queue import RenderQueue
from flow_story_studio.scene_contracts import verify_scene_contract
from flow_story_studio.service import StudioService
from flow_story_studio.storage import ProjectStorage
from flow_story_studio.video_merger import VideoMergeError, VideoMerger

SCRIPT = (
    "Alex bước vào nhà ga, cầm chiếc vé màu xanh và nhìn đồng hồ. "
    "Anh đi tới ghế chờ, đặt túi xuống rồi gọi điện cho Maya. "
    "Maya trả lời qua điện thoại và yêu cầu Alex đứng yên tại chỗ."
)


def test_analyzer_seals_scene_packet_contracts() -> None:
    project = analyze_story(AnalyzeRequest(name="contract", original_text=SCRIPT))

    assert project.scenes
    assert all(scene.contract_hash for scene in project.scenes)
    assert all(verify_scene_contract(scene) for scene in project.scenes)

    project.scenes[0].action += " Semantic drift"
    assert not verify_scene_contract(project.scenes[0])


def test_old_scene_contract_version_is_rejected() -> None:
    project = analyze_story(AnalyzeRequest(name="old contract", original_text=SCRIPT))
    scene = project.scenes[0]

    assert verify_scene_contract(scene)
    scene.contract_version = 1
    assert not verify_scene_contract(scene)


def test_edit_invalidates_contract_until_continuity_recompile(tmp_path: Path) -> None:
    storage = ProjectStorage(tmp_path / "projects")
    service = StudioService(storage)
    project = service.analyze(AnalyzeRequest(name="edit contract", original_text=SCRIPT))
    scene = project.scenes[0]

    service.set_scene_lock(project.id, scene.id, False)
    edited = service.update_scene(
        project.id,
        scene.id,
        SceneUpdate(action=scene.action + " rồi dừng lại"),
    )
    edited_scene = edited.scenes[0]

    assert edited_scene.contract_hash == ""
    assert not verify_scene_contract(edited_scene)
    with pytest.raises(PermissionError, match="Scene Packet"):
        service.set_scene_lock(project.id, scene.id, True)

    rebuilt = service.check_continuity(project.id)
    rebuilt_scene = rebuilt.scenes[0]
    assert verify_scene_contract(rebuilt_scene)

    relocked = service.set_scene_lock(project.id, scene.id, True)
    assert relocked.scenes[0].ai_locked is True


def test_render_queue_blocks_stale_contract(tmp_path: Path) -> None:
    storage = ProjectStorage(tmp_path / "projects")
    project = StudioService(storage).analyze(
        AnalyzeRequest(name="render contract", original_text=SCRIPT)
    )
    scene = project.scenes[0]

    assert RenderQueue._contract_block_reason(scene) == ""
    scene.camera += " drift"
    assert "contract" in RenderQueue._contract_block_reason(scene).casefold()


def test_final_merger_rejects_clip_with_stale_scene_contract(tmp_path: Path) -> None:
    storage = ProjectStorage(tmp_path / "projects")
    project = StudioService(storage).analyze(
        AnalyzeRequest(name="merge contract", original_text=SCRIPT)
    )
    for index, scene in enumerate(project.scenes, start=1):
        clip = tmp_path / "renders" / project.id / scene.id / f"clip-{index}.mp4"
        clip.parent.mkdir(parents=True, exist_ok=True)
        clip.write_bytes(b"video")
        scene.status = "Accepted"
        scene.acceptance.status = "Accepted"
        scene.result_file = clip.relative_to(tmp_path).as_posix()

    project.scenes[0].lighting += " drift"
    with pytest.raises(VideoMergeError, match="chưa có tệp video hoàn chỉnh"):
        VideoMerger(tmp_path).clips_for(project)
