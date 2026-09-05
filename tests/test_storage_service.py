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


def test_reorder_preserves_scene_identity_and_media_artifacts(tmp_path: Path) -> None:
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
    assert moved.result_file.endswith(f"/{media_scene.id}/result.mp4")
    assert moved.result_url.endswith(f"/scenes/{media_scene.id}/video")


def test_reorder_rejects_duplicate_scene_ids(tmp_path: Path) -> None:
    service = StudioService(ProjectStorage(tmp_path))
    project = service.analyze(AnalyzeRequest(name="Duplicate order", original_text=TEXT))
    ids = [scene.id for scene in project.scenes]
    assert ids
    with pytest.raises(ValueError):
        service.reorder(project.id, ReorderRequest(scene_ids=ids + [ids[-1]]))
