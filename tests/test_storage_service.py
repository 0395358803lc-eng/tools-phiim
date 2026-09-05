from pathlib import Path

import pytest

from flow_story_studio.models import AnalyzeRequest, ReorderRequest, SceneUpdate
from flow_story_studio.service import StudioService
from flow_story_studio.storage import ProjectStorage

TEXT = (
    "Cô gái bước vào quán cà phê và đặt chiếc túi lên ghế. "
    "Cô nhìn ra cửa sổ, nơi trời đang mưa rất lớn. "
    "Sau đó cô mở điện thoại và đọc một tin nhắn quan trọng."
)


def test_storage_and_scene_edit(tmp_path: Path) -> None:
    storage = ProjectStorage(tmp_path)
    service = StudioService(storage)
    project = service.analyze(AnalyzeRequest(name="Stored", original_text=TEXT))
    loaded = storage.get(project.id)
    assert loaded and loaded.name == "Stored"

    with pytest.raises(PermissionError, match="AI Continuity Lock"):
        service.update_scene(
            project.id,
            project.scenes[0].id,
            SceneUpdate(action="Không được ghi đè khi còn khóa."),
        )
    service.set_scene_lock(project.id, project.scenes[0].id, False)
    updated = service.update_scene(
        project.id,
        project.scenes[0].id,
        SceneUpdate(action="Cô gái dừng lại, hít sâu rồi mới bước tiếp."),
    )
    assert "hít sâu" in updated.scenes[0].flow_prompt
    if len(updated.scenes) > 1:
        assert any("có thể ảnh hưởng" in item for item in updated.scenes[1].warnings)


def test_reorder_preserves_identity_but_invalidates_stale_media(tmp_path: Path) -> None:
    service = StudioService(ProjectStorage(tmp_path))
    project = service.analyze(AnalyzeRequest(name="Order", original_text=TEXT))
    ids = [scene.id for scene in project.scenes]
    assert ids
    media_scene = project.scenes[-1]
    media_scene.status = "Completed"
    media_scene.progress = 100
    media_scene.result_file = f"renders/{project.id}/{media_scene.id}/result.mp4"
    media_scene.result_url = f"/api/projects/{project.id}/scenes/{media_scene.id}/video"
    service.storage.save(project)
    requested = list(reversed(ids))
    reordered = service.reorder(project.id, ReorderRequest(scene_ids=requested))
    assert [scene.id for scene in reordered.scenes] == requested
    assert [scene.order for scene in reordered.scenes] == list(range(1, len(ids) + 1))
    moved = next(scene for scene in reordered.scenes if scene.id == media_scene.id)
    assert moved.result_file == ""
    assert moved.result_url == ""
    assert moved.status == "Waiting"
    assert moved.acceptance.status == "Pending"
    assert moved.visual_qc.status == "Pending"


def test_semantic_mutations_are_blocked_while_render_is_in_flight(tmp_path: Path) -> None:
    service = StudioService(ProjectStorage(tmp_path))
    project = service.analyze(AnalyzeRequest(name="Render lock", original_text=TEXT))
    scene = project.scenes[0]
    scene.status = "Generating"
    service.storage.save(project)

    service.set_scene_lock(project.id, scene.id, False)
    with pytest.raises(PermissionError, match="render đang chạy"):
        service.update_scene(
            project.id,
            scene.id,
            SceneUpdate(action="Không được đổi trong lúc render."),
        )
    with pytest.raises(PermissionError, match="render đang chạy"):
        service.reorder(
            project.id,
            ReorderRequest(scene_ids=[item.id for item in project.scenes]),
        )
    with pytest.raises(PermissionError, match="render đang chạy"):
        service.check_continuity(project.id)


def test_reorder_rejects_duplicate_scene_ids(tmp_path: Path) -> None:
    service = StudioService(ProjectStorage(tmp_path))
    project = service.analyze(AnalyzeRequest(name="Duplicate order", original_text=TEXT))
    ids = [scene.id for scene in project.scenes]
    assert ids
    with pytest.raises(ValueError):
        service.reorder(project.id, ReorderRequest(scene_ids=ids + [ids[-1]]))
