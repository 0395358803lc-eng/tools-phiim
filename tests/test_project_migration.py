import json
from pathlib import Path

from flow_story_studio.engines.analyzer import analyze_story
from flow_story_studio.models import AnalyzeRequest, Project


def test_legacy_project_without_lock_metadata_remains_editable(tmp_path: Path) -> None:
    project = analyze_story(
        AnalyzeRequest(
            name="Legacy",
            original_text=(
                "Người đàn ông bước vào căn phòng và đặt điện thoại lên bàn. "
                "Sau đó anh đi tới cửa sổ và nhìn ra ngoài."
            ),
        )
    )
    payload = project.model_dump()
    for scene in payload["scenes"]:
        scene.pop("ai_locked", None)
        scene.pop("ai_lock_reason", None)
    path = tmp_path / "legacy.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    restored = Project.model_validate_json(path.read_text(encoding="utf-8"))
    assert all(not scene.ai_locked for scene in restored.scenes)
    assert all(scene.ai_locked for scene in project.scenes)
