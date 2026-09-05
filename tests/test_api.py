from pathlib import Path

from fastapi.testclient import TestClient

from flow_story_studio.main import create_app
from flow_story_studio.storage import ProjectStorage

TEXT = (
    "Người đàn ông bước vào văn phòng và đặt điện thoại lên bàn. "
    "Anh nhìn đồng hồ rồi đi tới cửa sổ. Sau đó anh quay lại ghế và bắt đầu nói."
)


def test_project_api_and_exports(tmp_path: Path) -> None:
    app = create_app(ProjectStorage(tmp_path / "projects"))
    with TestClient(app) as client:
        response = client.post(
            "/api/projects/analyze",
            json={"name": "API Demo", "original_text": TEXT, "settings": {}},
        )
        assert response.status_code == 201
        project = response.json()
        project_id = project["id"]

        fetched = client.get(f"/api/projects/{project_id}")
        assert fetched.status_code == 200
        assert fetched.json()["scenes"]

        scene_id = project["scenes"][0]["id"]
        blocked = client.patch(
            f"/api/projects/{project_id}/scenes/{scene_id}",
            json={"camera": "Manual camera change"},
        )
        assert blocked.status_code == 423
        unlocked = client.patch(
            f"/api/projects/{project_id}/scenes/{scene_id}/lock",
            json={"locked": False},
        )
        assert unlocked.status_code == 200
        edited = client.patch(
            f"/api/projects/{project_id}/scenes/{scene_id}",
            json={"camera": "Manual camera change"},
        )
        assert edited.status_code == 200

        exported = client.get(f"/api/projects/{project_id}/flow-prompts.zip")
        assert exported.status_code == 200
        assert exported.headers["content-type"] == "application/zip"


def test_mock_render_queue(tmp_path: Path) -> None:
    app = create_app(ProjectStorage(tmp_path / "projects"))
    with TestClient(app) as client:
        project = client.post(
            "/api/projects/analyze",
            json={"name": "Render Demo", "original_text": TEXT, "settings": {}},
        ).json()
        scene_id = project["scenes"][0]["id"]
        queued = client.post(
            f"/api/projects/{project['id']}/generate", json={"scene_ids": [scene_id]}
        )
        assert queued.status_code == 202


def test_session_is_fresh_and_video_settings_are_explicit(tmp_path: Path) -> None:
    app = create_app(ProjectStorage(tmp_path / "projects"))
    with TestClient(app) as client:
        session = client.get("/api/session")
        assert session.status_code == 200
        assert session.json()["fresh_start"] is True
        assert Path(session.json()["workspace"]) == tmp_path.resolve()

        project = client.post(
            "/api/projects/analyze",
            json={"name": "Session Demo", "original_text": TEXT, "settings": {}},
        ).json()
        updated = client.patch(
            f"/api/projects/{project['id']}/video-settings",
            json={
                "provider": "google-flow",
                "video_model": "veo-3.1-lite-lower-priority",
            },
        )
        assert updated.status_code == 200
        assert updated.json()["settings"]["provider"] == "google-flow"


def test_scene_video_supports_browser_byte_ranges(tmp_path: Path) -> None:
    storage = ProjectStorage(tmp_path / "projects")
    app = create_app(storage)
    with TestClient(app) as client:
        project_data = client.post(
            "/api/projects/analyze",
            json={"name": "Video Range", "original_text": TEXT, "settings": {}},
        ).json()
        project = storage.get(project_data["id"])
        assert project is not None
        scene = project.scenes[0]
        relative_path = Path("renders") / project.id / scene.id / "result.mp4"
        target = tmp_path / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(bytes(range(256)) * 8)
        scene.status = "Completed"
        scene.progress = 100
        scene.result_file = relative_path.as_posix()
        scene.result_url = f"/api/projects/{project.id}/scenes/{scene.id}/video"
        storage.save(project)

        response = client.get(scene.result_url, headers={"Range": "bytes=100-199"})

    assert response.status_code == 206
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["content-range"] == "bytes 100-199/2048"
    assert response.headers["content-type"] == "video/mp4"
    assert response.content == target.read_bytes()[100:200]


def test_delete_project_can_purge_generated_artifacts(tmp_path: Path) -> None:
    storage = ProjectStorage(tmp_path / "projects")
    app = create_app(storage)
    with TestClient(app) as client:
        project = client.post(
            "/api/projects/analyze",
            json={"name": "Delete artifacts", "original_text": TEXT, "settings": {}},
        ).json()
        render_dir = tmp_path / "renders" / project["id"]
        reference_dir = tmp_path / "references" / project["id"]
        render_dir.mkdir(parents=True)
        reference_dir.mkdir(parents=True)
        (render_dir / "clip.mp4").write_bytes(b"video")
        (reference_dir / "frame.png").write_bytes(b"image")

        response = client.delete(f"/api/projects/{project['id']}?purge_artifacts=true")
        assert response.status_code == 204
        assert not render_dir.exists()
        assert not reference_dir.exists()


def test_analysis_job_returns_compact_project_summary(tmp_path: Path) -> None:
    app = create_app(ProjectStorage(tmp_path / "projects"))
    with TestClient(app) as client:
        started = client.post(
            "/api/analysis/jobs",
            json={"name": "Compact job", "original_text": TEXT, "settings": {}},
        )
        assert started.status_code == 202
        job_id = started.json()["id"]
        for _ in range(100):
            job = client.get(f"/api/analysis/jobs/{job_id}").json()
            if job["status"] in {"completed", "failed", "cancelled"}:
                break
        assert job["status"] == "completed"
        assert set(job["project"]) == {"id", "name", "scene_count", "continuity_score", "scenes"}
        assert all(set(scene) == {"id", "status", "progress"} for scene in job["project"]["scenes"])
        full = client.get(f"/api/projects/{job['project']['id']}")
        assert full.status_code == 200
        payload = full.json()
        assert payload["original_text"] == TEXT
        assert payload["settings"]
        assert payload["story_bible"]
        assert len(payload["scenes"]) == job["project"]["scene_count"]
