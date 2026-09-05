import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from flow_story_studio.engines.analyzer import analyze_story
from flow_story_studio.logging_config import configure_logging, get_logger
from flow_story_studio.main import create_app
from flow_story_studio.migrations import CURRENT_PROJECT_SCHEMA_VERSION, migrate_project_payload
from flow_story_studio.models import AnalyzeRequest
from flow_story_studio.storage import ProjectStorage
from flow_story_studio.workspace_lock import WorkspaceLock, WorkspaceLockError

TEXT = (
    "Người đàn ông bước vào văn phòng và đặt điện thoại lên bàn. "
    "Anh nhìn đồng hồ rồi đi tới cửa sổ trước khi quay lại ghế."
)


def test_persistent_logging_writes_rotating_log(tmp_path: Path) -> None:
    log_path = configure_logging(tmp_path / "logs", level=logging.INFO)
    logger = get_logger("production-test")
    logger.info("production-log-probe")
    for handler in logging.getLogger("flow_story_studio").handlers:
        handler.flush()

    assert log_path.is_file()
    assert "production-log-probe" in log_path.read_text(encoding="utf-8")


def test_project_storage_creates_bounded_backups(tmp_path: Path) -> None:
    storage = ProjectStorage(tmp_path / "projects", backup_retention=2, backup_interval_seconds=0)
    project = analyze_story(AnalyzeRequest(name="Backup", original_text=TEXT))
    storage.save(project)
    project.name = "Backup v2"
    storage.save(project)
    project.name = "Backup v3"
    storage.save(project)
    project.name = "Backup v4"
    storage.save(project)

    backups = storage.backups(project.id)
    assert len(backups) == 2
    assert all(path.is_file() for path in backups)


def test_legacy_project_payload_migrates_to_current_schema() -> None:
    project = analyze_story(AnalyzeRequest(name="Legacy", original_text=TEXT))
    payload = project.model_dump()
    payload.pop("schema_version", None)
    for scene in payload["scenes"]:
        scene.pop("ai_locked", None)
        scene.pop("ai_lock_reason", None)

    migrated = migrate_project_payload(payload)

    assert migrated["schema_version"] == CURRENT_PROJECT_SCHEMA_VERSION
    assert all("ai_locked" in scene for scene in migrated["scenes"])


def test_newer_project_schema_is_rejected() -> None:
    with pytest.raises(ValueError, match="newer than supported"):
        migrate_project_payload({"schema_version": CURRENT_PROJECT_SCHEMA_VERSION + 1})


def test_desktop_session_token_protects_mutations(tmp_path: Path) -> None:
    app = create_app(ProjectStorage(tmp_path / "projects"), session_token="test-session-token")
    with TestClient(app) as client:
        assert client.get("/api/health").status_code == 200
        unauthorized = client.post(
            "/api/projects/analyze",
            json={"name": "Blocked", "original_text": TEXT, "settings": {}},
        )
        assert unauthorized.status_code == 401
        authorized = client.post(
            "/api/projects/analyze",
            headers={"X-Flow-Studio-Session": "test-session-token"},
            json={"name": "Allowed", "original_text": TEXT, "settings": {}},
        )
        assert authorized.status_code == 201


def test_workspace_lock_prevents_second_owner(tmp_path: Path) -> None:
    first = WorkspaceLock(tmp_path)
    second = WorkspaceLock(tmp_path)
    first.acquire()
    try:
        with pytest.raises(WorkspaceLockError):
            second.acquire()
    finally:
        first.release()
    second.acquire()
    second.release()


def test_backup_restore_api_round_trip(tmp_path: Path) -> None:
    storage = ProjectStorage(tmp_path / "projects", backup_interval_seconds=0)
    app = create_app(storage)
    with TestClient(app) as client:
        project = client.post(
            "/api/projects/analyze",
            json={"name": "Restore API", "original_text": TEXT, "settings": {}},
        ).json()
        project_id = project["id"]
        changed = client.patch(
            f"/api/projects/{project_id}/video-settings",
            json={"provider": "google-flow", "video_model": "veo-3.1-lite-lower-priority"},
        )
        assert changed.status_code == 200
        backups = client.get(f"/api/projects/{project_id}/backups")
        assert backups.status_code == 200
        backup_rows = backups.json()
        assert backup_rows
        restored = client.post(
            f"/api/projects/{project_id}/backups/{backup_rows[0]['name']}/restore"
        )
        assert restored.status_code == 200
        assert restored.json()["settings"]["provider"] == "mock"
